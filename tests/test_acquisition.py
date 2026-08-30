import json

import httpx
import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session
from starlette.requests import Request

from digest.acquisition import (
    ProwlarrAdapter,
    SabnzbdAdapter,
    ShelfmarkAdapter,
    _queue_state,
    automatic_release,
    cancel_acquisition,
    create_wanted,
    queue_release,
    reconcile_importing,
    run_acquisition_download,
    run_acquisition_monitor,
    run_acquisition_search,
    retry_acquisition,
    store_releases,
)
from digest.db import Base
from digest.jobs import claim, complete
from digest.main import remove_download_request, request_download
from digest.models import (
    AcquisitionRelease,
    AppSetting,
    Book,
    Job,
    JobStatus,
    ReviewState,
    User,
    WantedItem,
    WantedStatus,
)


def test_shelfmark_dictionary_status_detects_completed_download() -> None:
    external_id = "shelfmark-release-id"
    state, entry = _queue_state(
        {
            "complete": {
                external_id: {
                    "id": external_id,
                    "status": "complete",
                    "title": "Downloaded Book",
                }
            },
            "downloading": {},
        },
        external_id,
    )

    assert state == "complete"
    assert entry is not None and entry["id"] == external_id


def test_completed_download_can_be_removed_without_deleting_book() -> None:
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    with Session(engine, expire_on_commit=False) as db:
        user = User(username="reader", password_hash="hash")
        book = Book(
            title="Available Book",
            primary_author="Writer",
            review_state=ReviewState.READY,
        )
        db.add_all([user, book])
        db.commit()
        item = create_wanted(
            db,
            user_id=user.id,
            source="hardcover",
            source_id="available-book",
            title=book.title,
            author=book.primary_author,
            isbn="",
            cover_url="",
        )
        item.status = WantedStatus.AVAILABLE
        item.acquired_book_id = book.id
        db.commit()
        request = Request(
            {
                "type": "http",
                "method": "POST",
                "path": f"/wanted/{item.id}/remove",
                "headers": [],
                "query_string": b"",
                "session": {"user_id": user.id, "csrf": "token"},
            }
        )

        response = remove_download_request(item.id, request, db, form_csrf="token")

        assert response.status_code == 303
        assert db.get(WantedItem, item.id) is None
        assert db.get(Book, book.id) is not None


def test_download_request_accepts_missing_optional_provider_fields() -> None:
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    with Session(engine, expire_on_commit=False) as db:
        user = User(username="reader", password_hash="hash")
        db.add(user)
        db.commit()
        request = Request(
            {
                "type": "http",
                "method": "POST",
                "path": "/wanted",
                "headers": [],
                "query_string": b"",
                "session": {"user_id": user.id, "csrf": "token"},
            }
        )

        response = request_download(
            request,
            db,
            source="hardcover",
            title="Book Without ISBN",
            form_csrf="token",
        )

        item = db.scalar(select(WantedItem))
        assert response.status_code == 303
        assert item is not None
        assert item.isbn == ""
        assert item.author == ""


def test_import_reconciliation_accepts_series_prefix_and_completed_cancel() -> None:
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    with Session(engine, expire_on_commit=False) as db:
        user = User(username="reader", password_hash="hash")
        book = Book(
            title="Hunger Games 2 - Catching Fire",
            primary_author="Suzanne Collins",
            review_state=ReviewState.REVIEW,
        )
        db.add_all([user, book])
        db.commit()
        item = create_wanted(
            db,
            user_id=user.id,
            source="hardcover",
            source_id="catching-fire",
            title="Catching Fire",
            author="Suzanne Collins",
            isbn="",
            cover_url="",
        )
        item.status = WantedStatus.CANCELLED
        item.external_download_id = "completed-download"
        db.commit()

        assert reconcile_importing(db) == 1
        db.refresh(item)
        assert item.status == WantedStatus.AVAILABLE
        assert item.acquired_book_id == book.id


def test_wanted_requests_deduplicate_and_queue_search() -> None:
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    with Session(engine, expire_on_commit=False) as db:
        user = User(username="reader", password_hash="hash")
        db.add(user)
        db.commit()

        first = create_wanted(
            db,
            user_id=user.id,
            source="hardcover",
            source_id="book-1",
            title="A Book",
            author="An Author",
            isbn="9781234567897",
            cover_url="https://example.test/cover.jpg",
        )
        second = create_wanted(
            db,
            user_id=user.id,
            source="hardcover",
            source_id="book-1",
            title="A Book",
            author="An Author",
            isbn="9781234567897",
            cover_url="",
        )

        assert first.id == second.id
        assert len(db.scalars(select(WantedItem)).all()) == 1
        assert len(db.scalars(select(Job)).all()) == 1


def test_unconfigured_search_remains_wanted_and_requeues_daily() -> None:
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    with Session(engine, expire_on_commit=False) as db:
        user = User(username="reader", password_hash="hash")
        db.add(user)
        db.commit()
        item = create_wanted(
            db,
            user_id=user.id,
            source="openlibrary",
            source_id="work-1",
            title="Wanted Book",
            author="Writer",
            isbn="",
            cover_url="",
        )
        job = claim(db, "test-worker")
        assert job is not None
        run_acquisition_search(db, job)
        complete(db, job)

        db.refresh(item)
        assert item.status == WantedStatus.WANTED
        assert item.attempts == 1
        assert "adapter" in (item.last_error or "")
        jobs = db.scalars(select(Job).order_by(Job.id)).all()
        assert [entry.status for entry in jobs] == [JobStatus.COMPLETE, JobStatus.QUEUED]
        assert json.loads(jobs[1].payload_json) == {"wanted_id": item.id}


def test_shelfmark_search_uses_staged_fallback_and_filters_formats() -> None:
    calls = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(dict(request.url.params))
        if request.url.params.get("isbn"):
            return httpx.Response(200, json=[])
        return httpx.Response(
            200,
            json=[
                {"id": "good", "title": "Wanted Book - Writer.epub", "format": "epub"},
                {"id": "torrent", "title": "Wanted Book torrent", "format": "torrent"},
            ],
        )

    item = WantedItem(title="Wanted Book", author="Writer", isbn="9781234567897")
    client = httpx.Client(transport=httpx.MockTransport(handler))
    stage, releases = ShelfmarkAdapter("http://shelfmark:8084", client).search(item)

    assert stage == "author_title"
    assert [release["id"] for release in releases] == ["good"]
    assert calls[0]["query"] == "9781234567897"
    assert calls[1]["author"] == "Writer"


def test_release_results_are_normalized_and_scored() -> None:
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    with Session(engine, expire_on_commit=False) as db:
        user = User(username="reader", password_hash="hash")
        db.add(user)
        db.commit()
        item = create_wanted(
            db, user_id=user.id, source="hardcover", source_id="1", title="Wanted Book",
            author="Writer", isbn="", cover_url="",
        )
        count = store_releases(
            db,
            item,
            "shelfmark",
            "title",
            [{"id": "release-1", "title": "Writer - Wanted Book.epub", "format": "EPUB"}],
        )
        release = db.scalar(select(AcquisitionRelease))
        assert count == 1
        assert release is not None
        assert release.format == "epub"
        assert release.match_score == 1


def test_automatic_release_requires_one_exact_lossless_match() -> None:
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    with Session(engine, expire_on_commit=False) as db:
        user = User(username="reader", password_hash="hash")
        db.add(user)
        db.commit()
        item = create_wanted(db, user_id=user.id, source="hardcover", source_id="1",
                             title="Wanted Book", author="Writer", isbn="", cover_url="")
        store_releases(db, item, "shelfmark", "title", [
            {"id": "exact", "title": "Writer Wanted Book EPUB", "format": "epub"},
            {"id": "weak", "title": "Wanted EPUB", "format": "epub"},
        ])
        assert automatic_release(db, item).source_id == "exact"
        store_releases(db, item, "prowlarr", "title", [
            {"id": "another", "title": "Writer Wanted Book EPUB", "format": "epub"}
        ], replace=False)
        assert automatic_release(db, item) is None


def test_failed_download_can_return_to_release_selection() -> None:
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    with Session(engine, expire_on_commit=False) as db:
        item = WantedItem(user_id=1, request_key="key", source="hardcover", source_id="1",
                          title="Wanted", author="Writer", isbn="", status=WantedStatus.FAILED,
                          selected_release_id=5, external_download_id="old", last_error="failed")
        db.add(item)
        db.commit()
        retry_acquisition(db, item)
        assert item.status == WantedStatus.WANTED
        assert item.selected_release_id is None
        assert item.external_download_id is None
        assert item.last_error is None


def test_active_sab_download_is_cancelled_at_provider(monkeypatch) -> None:
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    with Session(engine, expire_on_commit=False) as db:
        db.add_all([AppSetting(key="sabnzbd_url", value="http://sab:8080"),
                    AppSetting(key="sabnzbd_api_key", value="key")])
        item = WantedItem(user_id=1, request_key="key", source="hardcover", source_id="1",
                          title="Wanted", author="Writer", isbn="",
                          status=WantedStatus.DOWNLOADING, download_adapter="sabnzbd",
                          external_download_id="nzo-1")
        db.add(item)
        db.commit()
        cancelled = []
        monkeypatch.setattr(SabnzbdAdapter, "cancel", lambda self, value: cancelled.append(value))
        cancel_acquisition(db, item)
        assert cancelled == ["nzo-1"]
        assert item.status == WantedStatus.CANCELLED


def test_selected_release_is_downloaded_and_monitored(monkeypatch) -> None:
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    with Session(engine, expire_on_commit=False) as db:
        user = User(username="reader", password_hash="hash")
        db.add_all([user, AppSetting(key="shelfmark_url", value="http://shelfmark:8084")])
        db.commit()
        item = create_wanted(
            db, user_id=user.id, source="hardcover", source_id="1", title="Wanted Book",
            author="Writer", isbn="", cover_url="",
        )
        store_releases(
            db, item, "shelfmark", "title",
            [{"id": "release-1", "title": "Writer - Wanted Book.epub", "format": "epub"}],
        )
        release = db.scalar(select(AcquisitionRelease))
        assert release is not None
        queue_release(db, item, release)
        download_job = db.scalar(
            select(Job).where(Job.kind == "acquisition_download", Job.status == JobStatus.QUEUED)
        )
        assert download_job is not None

        monkeypatch.setattr(
            ShelfmarkAdapter,
            "download",
            lambda self, selected: {"job_id": "download-123", "status": "queued"},
        )
        run_acquisition_download(db, download_job)
        db.refresh(item)
        assert item.external_download_id == "download-123"

        monitor_job = db.scalar(
            select(Job).where(Job.kind == "acquisition_monitor", Job.status == JobStatus.QUEUED)
        )
        assert monitor_job is not None
        monkeypatch.setattr(
            ShelfmarkAdapter,
            "status",
            lambda self: {"complete": [{"job_id": "download-123", "path": "/downloads/book.epub"}]},
        )
        run_acquisition_monitor(db, monitor_job)
        db.refresh(item)
        assert item.status == WantedStatus.IMPORTING
        assert db.scalar(select(Job).where(Job.kind == "library_scan")) is not None


def test_prowlarr_returns_only_usenet_ebooks() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.headers["X-Api-Key"] == "secret"
        assert request.url.params["categories"] == "7020"
        return httpx.Response(
            200,
            json=[
                {"guid": "nzb", "title": "Writer Wanted Book EPUB", "protocol": "usenet",
                 "downloadUrl": "http://prowlarr/1/download"},
                {"guid": "torrent", "title": "Writer Wanted Book EPUB", "protocol": "torrent",
                 "downloadUrl": "magnet:test", "magnetUrl": "magnet:test"},
                {"guid": "pdf", "title": "Writer Wanted Book PDF", "protocol": "usenet",
                 "downloadUrl": "http://prowlarr/2/download"},
            ],
        )

    client = httpx.Client(transport=httpx.MockTransport(handler))
    item = WantedItem(title="Wanted Book", author="Writer", isbn="")
    stage, releases = ProwlarrAdapter("http://prowlarr:9696", "secret", client).search(item)
    assert stage == "author_title"
    assert [release["guid"] for release in releases] == ["nzb"]


def test_sabnzbd_rejects_torrents_and_tracks_nzo_id() -> None:
    requests = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        if request.method == "POST":
            return httpx.Response(200, json={"status": True, "nzo_ids": ["SABnzbd_nzo_123"]})
        mode = request.url.params["mode"]
        payload = {mode: {"slots": [{"nzo_id": "SABnzbd_nzo_123", "status": "Completed"}]}}
        return httpx.Response(200, json=payload)

    client = httpx.Client(transport=httpx.MockTransport(handler))
    adapter = SabnzbdAdapter("http://sabnzbd:8080", "sab-key", "ebooks", client)
    release = AcquisitionRelease(
        wanted_id=1, adapter="prowlarr", source_id="nzb", title="Book EPUB", format="epub",
        search_stage="title",
        download_payload_json=json.dumps(
            {"protocol": "usenet", "downloadUrl": "http://prowlarr/1/download"}
        ),
    )
    result = adapter.download(release)
    assert result["nzo_id"] == "SABnzbd_nzo_123"
    assert "mode=addurl" in requests[0].content.decode()
    status = adapter.status("SABnzbd_nzo_123")
    assert status["history"][0]["status"] == "Completed"

    release.download_payload_json = json.dumps(
        {"protocol": "torrent", "downloadUrl": "magnet:test", "magnetUrl": "magnet:test"}
    )
    with pytest.raises(ValueError, match="Usenet"):
        adapter.download(release)


def test_prowlarr_nzb_is_authenticated_then_uploaded_to_sab() -> None:
    def prowlarr_handler(request: httpx.Request) -> httpx.Response:
        assert request.headers["X-Api-Key"] == "prowlarr-key"
        return httpx.Response(200, content=b"<?xml version='1.0'?><nzb></nzb>")

    sab_requests = []

    def sab_handler(request: httpx.Request) -> httpx.Response:
        sab_requests.append(request)
        return httpx.Response(200, json={"status": True, "nzo_ids": ["SABnzbd_nzo_456"]})

    release = AcquisitionRelease(
        wanted_id=1, adapter="prowlarr", source_id="nzb", title="Wanted Book", format="epub",
        search_stage="title",
        download_payload_json=json.dumps(
            {"protocol": "usenet", "downloadUrl": "/api/v1/indexer/1/download"}
        ),
    )
    prowlarr = ProwlarrAdapter(
        "http://prowlarr:9696",
        "prowlarr-key",
        httpx.Client(transport=httpx.MockTransport(prowlarr_handler)),
    )
    sab = SabnzbdAdapter(
        "http://sabnzbd:8080",
        "sab-key",
        "ebooks",
        httpx.Client(transport=httpx.MockTransport(sab_handler)),
    )
    result = sab.download_nzb(prowlarr.fetch_nzb(release), release.title)
    assert result["nzo_id"] == "SABnzbd_nzo_456"
    assert b'name="mode"\r\n\r\naddfile' in sab_requests[0].content
    assert b"<nzb></nzb>" in sab_requests[0].content

    release.download_payload_json = json.dumps(
        {"protocol": "usenet", "downloadUrl": "https://indexer.example/nzb"}
    )
    with pytest.raises(ValueError, match="configured origin"):
        prowlarr.fetch_nzb(release)


def test_prowlarr_nzb_follows_secure_indexer_redirect() -> None:
    calls = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(str(request.url))
        if request.url.host == "prowlarr":
            return httpx.Response(
                301,
                headers={"location": "https://indexer.example/download/book.nzb"},
            )
        return httpx.Response(200, content=b"<?xml version='1.0'?><nzb></nzb>")

    release = AcquisitionRelease(
        wanted_id=1,
        adapter="prowlarr",
        source_id="nzb",
        title="Wanted Book",
        format="epub",
        search_stage="title",
        download_payload_json=json.dumps(
            {"protocol": "usenet", "downloadUrl": "/1/download?release=book"}
        ),
    )
    adapter = ProwlarrAdapter(
        "http://prowlarr:9696",
        "prowlarr-key",
        httpx.Client(transport=httpx.MockTransport(handler)),
    )

    assert adapter.fetch_nzb(release).startswith(b"<?xml")
    assert calls == [
        "http://prowlarr:9696/1/download?release=book",
        "https://indexer.example/download/book.nzb",
    ]


def test_failed_shelfmark_does_not_block_prowlarr_results(monkeypatch) -> None:
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    with Session(engine, expire_on_commit=False) as db:
        user = User(username="reader", password_hash="hash")
        db.add_all(
            [
                user,
                AppSetting(key="shelfmark_enabled", value="true"),
                AppSetting(key="shelfmark_url", value="http://missing-shelfmark:8084"),
                AppSetting(key="usenet_enabled", value="true"),
                AppSetting(key="prowlarr_url", value="http://prowlarr:9696"),
                AppSetting(key="prowlarr_api_key", value="secret", secret=True),
            ]
        )
        db.commit()
        item = create_wanted(
            db, user_id=user.id, source="hardcover", source_id="1", title="Wanted Book",
            author="Writer", isbn="", cover_url="",
        )
        job = claim(db, "worker")
        assert job is not None
        monkeypatch.setattr(
            ShelfmarkAdapter,
            "search",
            lambda self, wanted: (_ for _ in ()).throw(httpx.ConnectError("unavailable")),
        )
        monkeypatch.setattr(
            ProwlarrAdapter,
            "search",
            lambda self, wanted: (
                "title",
                [{"guid": "nzb", "source_id": "nzb", "source": "usenet",
                  "title": "Writer Wanted Book EPUB", "format": "epub",
                  "protocol": "usenet", "downloadUrl": "/download"}],
            ),
        )
        run_acquisition_search(db, job)
        db.refresh(item)
        assert item.status == WantedStatus.DOWNLOADING
        assert item.last_error is None
        assert item.selected_release_id is not None
        assert db.scalar(select(AcquisitionRelease).where(AcquisitionRelease.adapter == "prowlarr"))
