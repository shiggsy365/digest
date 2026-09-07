from types import SimpleNamespace
import json

import httpx
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session
from starlette.requests import Request

from digest import main
from digest.db import Base
from digest.models import AcquisitionRelease, Book, BookFile, ReviewState, Role, User, WantedItem
from digest.security import hash_password


def opds_session() -> Session:
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    return Session(engine, expire_on_commit=False)


def request_for(user: User, path: str = "/opds", accept: str = "") -> Request:
    headers = []
    if accept:
        headers.append((b"accept", accept.encode()))
    return Request(
        {
            "type": "http",
            "method": "GET",
            "path": path,
            "headers": headers,
            "query_string": b"",
            "session": {"user_id": user.id},
        }
    )


def add_user(db: Session) -> User:
    user = User(
        username="reader",
        password_hash=hash_password("long-test-password"),
        role=Role.USER,
    )
    db.add(user)
    db.commit()
    return user


def add_admin(db: Session) -> User:
    user = User(
        username="admin",
        password_hash=hash_password("long-test-password"),
        role=Role.ADMIN,
    )
    db.add(user)
    db.commit()
    return user


def test_opds_library_exposes_acquisition_links() -> None:
    with opds_session() as db:
        user = add_user(db)
        book = Book(
            title="Alpha",
            primary_author="A Writer",
            authors_json='["A Writer", "Co Author"]',
            series="Letters",
            series_number=1,
            isbns_json='["9780000000001"]',
            language="en",
            description="<p>A described book.</p>",
            review_state=ReviewState.READY,
        )
        db.add(book)
        db.flush()
        db.add(
            BookFile(
                book_id=book.id,
                path="/library/alpha.epub",
                sha256="0" * 64,
                format="epub",
                size_bytes=123,
                modified_ns=1,
            )
        )
        db.commit()

        response = main.opds_title(request_for(user, "/opds/title"), db)

        xml = response.body.decode()
        assert "Digest Library - By Title" in xml
        assert "Alpha" in xml
        assert "Co Author" in xml
        assert "Letters" in xml
        assert "urn:isbn:9780000000001" in xml
        assert "<dc:language>en</dc:language>" in xml
        assert "A described book." in xml
        assert 'rel="http://opds-spec.org/acquisition"' in xml
        assert 'type="application/epub+zip"' in xml
        assert 'length="123"' in xml


def test_opds_library_json_exposes_metadata() -> None:
    with opds_session() as db:
        user = add_user(db)
        book = Book(
            title="Alpha",
            primary_author="A Writer",
            authors_json='["A Writer", "Co Author"]',
            series="Letters",
            series_number=1,
            isbns_json='["9780000000001"]',
            language="en",
            description="<p>A described book.</p>",
            review_state=ReviewState.READY,
        )
        db.add(book)
        db.flush()
        db.add(
            BookFile(
                book_id=book.id,
                path="/library/alpha.epub",
                sha256="0" * 64,
                format="epub",
                size_bytes=123,
                modified_ns=1,
            )
        )
        db.commit()

        response = main.opds_title(
            request_for(user, "/opds/title", "application/opds+json"), db
        )
        data = response.body.decode()

        assert response.media_type == "application/opds+json"
        assert '"publications"' in data
        assert '"title":"Alpha"' in data
        assert '"author":"A Writer, Co Author"' in data
        assert '"language":"en"' in data
        assert '"description":"A described book."' in data
        assert '"name":"Letters"' in data
        assert '"numberOfBytes":123' in data


def test_opds_library_search_matches_title_author_and_series() -> None:
    with opds_session() as db:
        user = add_user(db)
        db.add_all(
            [
                Book(
                    title="The Left Hand of Darkness",
                    primary_author="Ursula K. Le Guin",
                    series="Hainish Cycle",
                    review_state=ReviewState.READY,
                ),
                Book(
                    title="A Wizard of Earthsea",
                    primary_author="Ursula K. Le Guin",
                    review_state=ReviewState.READY,
                ),
                Book(
                    title="Hidden Match",
                    primary_author="Someone Else",
                    series="Hainish Cycle",
                    review_state=ReviewState.READY,
                ),
                Book(
                    title="Draft Book",
                    primary_author="Ursula K. Le Guin",
                    review_state=ReviewState.REVIEW,
                ),
            ]
        )
        db.commit()

        response = main.opds_search(request_for(user, "/opds/search"), db, query="hainish")

        xml = response.body.decode()
        assert "Digest Library - Search: hainish" in xml
        assert "The Left Hand of Darkness" in xml
        assert "Hidden Match" in xml
        assert "A Wizard of Earthsea" not in xml
        assert "Draft Book" not in xml
        assert 'href="/opds/search.xml"' in xml
        assert 'href="/opds/search?query={searchTerms}"' in xml


def test_opds_library_search_descriptor_exposes_atom_template() -> None:
    with opds_session() as db:
        user = add_user(db)

        response = main.opds_search_descriptor(request_for(user, "/opds/search.xml"), db)

        xml = response.body.decode()
        assert response.media_type == "application/opensearchdescription+xml"
        assert "OpenSearchDescription" in xml
        assert 'type="application/atom+xml;profile=opds-catalog;kind=acquisition"' in xml
        assert 'template="/opds/search?query={searchTerms}"' in xml


def test_opds_library_root_exposes_catalogs() -> None:
    with opds_session() as db:
        user = add_user(db)

        response = main.opds(request_for(user), db)

        xml = response.body.decode()
        assert "Digest Library Catalog" in xml
        assert "By Title" in xml
        assert 'href="/opds/catalog/title"' in xml
        assert "By Author" in xml
        assert 'href="/opds/catalog/authors-v2"' in xml
        assert "Latest" in xml
        assert 'href="/opds/catalog/latest"' in xml
        assert "By Series" in xml
        assert 'href="/opds/catalog/series"' in xml
        assert "Discover" in xml
        assert 'href="/opds/discover"' in xml
        assert 'rel="subsection"' in xml


def test_opds_library_root_json_exposes_navigation() -> None:
    with opds_session() as db:
        user = add_user(db)

        response = main.opds(request_for(user, "/opds", "application/opds+json"), db)

        data = response.body.decode()
        assert response.media_type == "application/opds+json"
        assert '"navigation"' in data
        assert '"title":"By Title"' in data
        assert '"href":"/opds/catalog/title"' in data
        assert '"href":"/opds/catalog/authors-v2"' in data
        assert '"title":"Discover"' in data
        assert '"metadata":{"title":"By Title"' not in data


def test_opds_author_and_series_catalogs_link_to_book_feeds() -> None:
    with opds_session() as db:
        user = add_user(db)
        db.add(
            Book(
                title="Alpha",
                primary_author="A Writer",
                series="Letters",
                review_state=ReviewState.READY,
            )
        )
        db.commit()

        authors = main.opds_authors(request_for(user, "/opds/authors"), db).body.decode()
        series = main.opds_series(request_for(user, "/opds/series"), db).body.decode()

        assert "A Writer (1)" in authors
        assert "/opds/author-group/A%20Writer" in authors
        assert "Letters (1)" in series
        assert "/opds/series-folder/Letters" in series


def test_opds_author_json_keeps_single_book_authors_as_folders() -> None:
    with opds_session() as db:
        user = add_user(db)
        db.add(Book(title="Alpha", primary_author="A Writer", review_state=ReviewState.READY))
        db.commit()

        response = main.opds_authors(
            request_for(user, "/opds/authors", "application/opds+json"), db
        )

        data = json.loads(response.body)
        assert data["navigation"] == [
            {
                "title": "A Writer",
                "rel": "subsection",
                "href": "/opds/author-group/A%20Writer",
                "type": "application/opds+json",
                "numberOfItems": 1,
                "properties": {"numberOfItems": 1},
            }
        ]

        folder = main.opds_author_group(
            request_for(user, "/opds/author-group/A%20Writer", "application/opds+json"),
            db,
            "A Writer",
        )
        folder_data = json.loads(folder.body)
        assert folder_data["metadata"]["title"] == "A Writer"
        assert folder_data["navigation"] == [
            {
                "title": "Books by A Writer",
                "rel": "subsection",
                "href": "/opds/author/A%20Writer",
                "type": "application/opds+json",
            },
            {
                "title": "Author info",
                "rel": "subsection",
                "href": "/opds/author-group/A%20Writer/about",
                "type": "application/opds+json",
            },
        ]
        about = main.opds_author_group_about(
            request_for(user, "/opds/author-group/A%20Writer/about", "application/opds+json"),
            db,
            "A Writer",
        )
        about_data = json.loads(about.body)
        assert about_data["publications"][0]["metadata"]["title"] == "Author info"


def test_opds_author_json_links_multi_book_authors_directly() -> None:
    with opds_session() as db:
        user = add_user(db)
        db.add_all(
            [
                Book(title="Alpha", primary_author="A Writer", review_state=ReviewState.READY),
                Book(title="Beta", primary_author="A Writer", review_state=ReviewState.READY),
            ]
        )
        db.commit()

        response = main.opds_authors(
            request_for(user, "/opds/authors", "application/opds+json"), db
        )

        data = json.loads(response.body)
        assert data["navigation"][0]["href"] == "/opds/author/A%20Writer"
        assert data["navigation"][0]["numberOfItems"] == 2
        assert data["navigation"][0]["properties"]["numberOfItems"] == 2


def test_opds_series_json_exposes_book_counts() -> None:
    with opds_session() as db:
        user = add_user(db)
        db.add_all(
            [
                Book(
                    title="Alpha",
                    primary_author="A Writer",
                    series="Letters",
                    review_state=ReviewState.READY,
                ),
                Book(
                    title="Beta",
                    primary_author="A Writer",
                    series="Letters",
                    review_state=ReviewState.READY,
                ),
            ]
        )
        db.commit()

        response = main.opds_series(
            request_for(user, "/opds/series", "application/opds+json"), db
        )

        data = json.loads(response.body)
        assert data["navigation"][0]["title"] == "Letters"
        assert data["navigation"][0]["href"] == "/opds/series/Letters"
        assert data["navigation"][0]["numberOfItems"] == 2
        assert data["navigation"][0]["properties"]["numberOfItems"] == 2


def test_opds_root_dispatches_catalog_queries() -> None:
    with opds_session() as db:
        user = add_user(db)
        db.add(Book(title="Alpha", primary_author="A Writer", review_state=ReviewState.READY))
        db.commit()

        title = main.opds(request_for(user), db, catalog="title").body.decode()
        authors = main.opds(request_for(user), db, catalog="authors").body.decode()

        assert "Digest Library - By Title" in title
        assert "Alpha" in title
        assert "Digest Library - By Author" in authors
        assert "A Writer (1)" in authors


def test_opds_discover_root_exposes_browsable_sections() -> None:
    with opds_session() as db:
        user = add_user(db)

        response = main.opds_discover(request_for(user, "/opds/discover"), db)

        xml = response.body.decode()
        assert "Digest Discover" in xml
        assert "Search" in xml
        assert "Trending" in xml
        assert "NYT Bestsellers" in xml
        assert "New Releases" in xml
        assert 'href="/opds/discover/search"' in xml
        assert 'rel="subsection"' in xml
        assert 'rel="search"' in xml
        assert 'href="/opds/discover/search.xml"' in xml
        assert 'href="/opds/discover/search?query={searchTerms}"' in xml


def test_opds_discover_root_json_exposes_search_and_navigation() -> None:
    with opds_session() as db:
        user = add_user(db)

        response = main.opds_discover(
            request_for(user, "/opds/discover", "application/opds+json"), db
        )

        data = json.loads(response.body)
        assert data["metadata"]["title"] == "Digest Discover"
        assert {"rel": "search", "href": "/opds/discover/search?query={searchTerms}",
                "type": "application/opds+json", "templated": True} in data["links"]
        assert [item["title"] for item in data["navigation"]] == [
            "Search",
            "Trending",
            "NYT Bestsellers",
            "New Releases",
            "Downloads",
        ]


def test_opds_discover_trending_browses_genre_then_period(monkeypatch) -> None:
    with opds_session() as db:
        user = add_user(db)
        monkeypatch.setattr(
            main,
            "settings_map",
            lambda db: {"hardcover_api_key": "key", "default_language": "en"},
        )
        monkeypatch.setattr(
            main,
            "hardcover_books",
            lambda api_key, **kwargs: [
                {
                    "source": "hardcover",
                    "source_id": "1",
                    "title": f"{kwargs['genre']} Book",
                    "author": "Trend Writer",
                }
            ],
        )

        genres = main.opds_discover_trending(request_for(user, "/opds/discover/trending"), db)
        periods = main.opds_discover_trending_periods(
            request_for(user, "/opds/discover/trending/fantasy"), db, "fantasy"
        )
        books = main.opds_discover_trending_books(
            request_for(user, "/opds/discover/trending/fantasy/3m"),
            db,
            "fantasy",
            "3m",
        )

        assert "Fantasy" in genres.body.decode()
        assert 'href="/opds/discover/trending/fantasy"' in genres.body.decode()
        for label, slug in [
            ("Science Fiction", "science_fiction"),
            ("Mystery", "mystery_and_detective_stories"),
            ("Thriller", "thriller"),
            ("Romance", "romance"),
            ("Historical Fiction", "historical_fiction"),
            ("Horror", "horror"),
            ("Biography &amp; Memoirs", "biography"),
            ("History", "history"),
            ("Self Help", "self_help"),
            ("True Crime", "true_crime"),
        ]:
            body = genres.body.decode()
            assert label in body
            assert f'href="/opds/discover/trending/{slug}"' in body
        assert "Past 3 Months" in periods.body.decode()
        assert 'href="/opds/discover/trending/fantasy/3m"' in periods.body.decode()
        assert "Fantasy Book" in books.body.decode()


def test_opds_discover_nyt_bestsellers_browses_list_then_week(monkeypatch) -> None:
    main._nyt_opds_cache.clear()
    with opds_session() as db:
        user = add_user(db)
        lists = [
            {
                "slug": "hardcover-fiction",
                "title": "Hardcover Fiction",
                "newest_published_date": "2026-09-06",
                "oldest_published_date": "2026-08-30",
            }
        ]
        monkeypatch.setattr(main, "configured_nyt_lists", lambda db: ("key", lists))
        seen_weeks = []
        monkeypatch.setattr(
            main,
            "nyt_bestsellers",
            lambda api_key, slug, week: seen_weeks.append(week) or [
                {
                    "source": "nytimes",
                    "source_id": "9780000000004",
                    "title": "Bestseller",
                    "author": "List Writer",
                    "isbn": "9780000000004",
                }
            ],
        )

        genres = main.opds_discover_nyt_bestsellers(
            request_for(user, "/opds/discover/nyt-bestsellers"), db
        )
        weeks = main.opds_discover_nyt_bestseller_weeks(
            request_for(user, "/opds/discover/nyt-bestsellers/hardcover-fiction"),
            db,
            "hardcover-fiction",
        )
        books = main.opds_discover_nyt_bestseller_books(
            request_for(user, "/opds/discover/nyt-bestsellers/hardcover-fiction/current"),
            db,
            "hardcover-fiction",
            "current",
        )

        assert "Hardcover Fiction" in genres.body.decode()
        assert 'href="/opds/discover/nyt-bestsellers/hardcover-fiction"' in genres.body.decode()
        assert "Current" in weeks.body.decode()
        assert "06 Sep 2026" in weeks.body.decode()
        assert seen_weeks == ["current"]
        assert "Bestseller" in books.body.decode()
        assert "List Writer" in books.body.decode()


def test_opds_discover_nyt_bestsellers_falls_back_to_current(monkeypatch) -> None:
    main._nyt_opds_cache.clear()
    with opds_session() as db:
        user = add_user(db)
        lists = [
            {
                "slug": "hardcover-fiction",
                "title": "Hardcover Fiction",
                "newest_published_date": "2026-09-06",
                "oldest_published_date": "2026-08-30",
            }
        ]
        monkeypatch.setattr(main, "configured_nyt_lists", lambda db: ("key", lists))
        seen_weeks = []

        def fake_bestsellers(api_key, slug, week):
            seen_weeks.append(week)
            if week != "current":
                return []
            return [
                {
                    "source": "nytimes",
                    "source_id": "9780000000004",
                    "title": "Current Bestseller",
                    "author": "List Writer",
                    "isbn": "9780000000004",
                }
            ]

        monkeypatch.setattr(main, "nyt_bestsellers", fake_bestsellers)

        books = main.opds_discover_nyt_bestseller_books(
            request_for(user, "/opds/discover/nyt-bestsellers/hardcover-fiction/2026-09-06"),
            db,
            "hardcover-fiction",
            "2026-09-06",
        )

        assert seen_weeks == ["2026-09-06", "current"]
        assert "Current Bestseller" in books.body.decode()


def test_opds_discover_nyt_weeks_json_declares_multiple_items(monkeypatch) -> None:
    with opds_session() as db:
        user = add_user(db)
        lists = [
            {
                "slug": "hardcover-fiction",
                "title": "Hardcover Fiction",
                "newest_published_date": "2026-09-06",
                "oldest_published_date": "2026-09-06",
            }
        ]
        monkeypatch.setattr(main, "configured_nyt_lists", lambda db: ("key", lists))

        response = main.opds_discover_nyt_bestseller_weeks(
            request_for(
                user,
                "/opds/discover/nyt-bestsellers/hardcover-fiction",
                "application/opds+json",
            ),
            db,
            "hardcover-fiction",
        )

        data = json.loads(response.body)
        assert data["navigation"][0]["title"] == "Current"
        assert data["navigation"][0]["numberOfItems"] == 15
        assert data["navigation"][1]["numberOfItems"] == 15


def test_opds_discover_new_releases_browses_genre_then_period(monkeypatch) -> None:
    with opds_session() as db:
        user = add_user(db)
        monkeypatch.setattr(
            main,
            "settings_map",
            lambda db: {"hardcover_api_key": "key", "default_language": "en"},
        )
        monkeypatch.setattr(
            main,
            "hardcover_books",
            lambda api_key, **kwargs: [
                {
                    "source": "hardcover",
                    "source_id": "2",
                    "title": "Fresh Book",
                    "author": "New Writer",
                }
            ],
        )

        genres = main.opds_discover_new_releases(
            request_for(user, "/opds/discover/new-releases"), db
        )
        periods = main.opds_discover_new_release_periods(
            request_for(user, "/opds/discover/new-releases/science_fiction"),
            db,
            "science_fiction",
        )
        books = main.opds_discover_new_release_books(
            request_for(user, "/opds/discover/new-releases/science_fiction/30d"),
            db,
            "science_fiction",
            "30d",
        )

        assert "Science Fiction" in genres.body.decode()
        assert "Past 30 Days" in periods.body.decode()
        assert "Fresh Book" in books.body.decode()


def test_opds_discover_for_you_renders_recommendations(monkeypatch) -> None:
    with opds_session() as db:
        user = add_user(db)

        monkeypatch.setattr(
            main,
            "build_discovery",
            lambda db, user_id, **kwargs: SimpleNamespace(
                recommended=[
                    {
                        "source": "openlibrary",
                        "source_id": "OL1W",
                        "title": "Found Book",
                        "author": "Finder",
                        "cover_url": "https://example.test/cover.jpg",
                        "description": "A discovery item.",
                    }
                ]
            ),
        )

        response = main.opds_discover_for_you(request_for(user, "/opds/discover/for-you"), db)

        xml = response.body.decode()
        assert "Found Book" in xml
        assert "Finder" in xml
        assert "https://example.test/cover.jpg" in xml
        assert 'rel="alternate"' in xml
        assert 'rel="http://opds-spec.org/acquisition"' in xml
        assert 'title="Request download"' in xml


def test_opds_discover_filters_books_already_in_library(monkeypatch) -> None:
    with opds_session() as db:
        user = add_user(db)
        db.add(
            Book(
                title="Owned Book",
                primary_author="Known Author",
                isbns_json='["9780000000002"]',
                review_state=ReviewState.READY,
            )
        )
        db.commit()

        monkeypatch.setattr(
            main,
            "build_discovery",
            lambda db, user_id, **kwargs: SimpleNamespace(
                recommended=[
                    {
                        "source": "openlibrary",
                        "source_id": "owned",
                        "title": "Owned Book",
                        "author": "Known Author",
                        "isbn": "9780000000002",
                    },
                    {
                        "source": "openlibrary",
                        "source_id": "new",
                        "title": "New Book",
                        "author": "New Author",
                    },
                ]
            ),
        )

        response = main.opds_discover_for_you(request_for(user, "/opds/discover/for-you"), db)

        xml = response.body.decode()
        assert "Owned Book" not in xml
        assert "New Book" in xml


def test_opds_discover_request_queues_wanted_item() -> None:
    with opds_session() as db:
        user = add_user(db)

        response = main.opds_discover_request(
            request_for(user, "/opds/discover/request"),
            db,
            source="openlibrary",
            source_id="OL1W",
            title="Requested Book",
            author="Request Author",
            isbn="9780000000003",
            cover_url="https://example.test/cover.jpg",
        )

        wanted = db.scalar(select(WantedItem).where(WantedItem.user_id == user.id))
        assert response.status_code == 202
        assert wanted is not None
        assert wanted.title == "Requested Book"
        assert wanted.author == "Request Author"
        assert wanted.source == "openlibrary"


def test_opds_discover_request_status_and_release_selection() -> None:
    with opds_session() as db:
        user = add_user(db)
        response = main.opds_discover_request(
            request_for(user, "/opds/discover/request", accept="application/json"),
            db,
            source="hardcover",
            source_id="1",
            title="Requested Book",
            author="Request Author",
        )
        wanted = db.scalar(select(WantedItem).where(WantedItem.user_id == user.id))
        assert response.status_code == 202
        assert wanted is not None
        release = AcquisitionRelease(
            wanted_id=wanted.id,
            adapter="prowlarr",
            source="prowlarr",
            source_id="rel-1",
            title="Requested Book EPUB",
            format="epub",
            size_bytes=1234,
            seeders=4,
            match_score=0.91,
            search_stage="title",
            download_payload_json='{"downloadUrl":"https://example.test/release"}',
        )
        db.add(release)
        db.commit()

        status = main.opds_discover_download_status(
            wanted.id,
            request_for(user, f"/opds/discover/status/{wanted.id}", accept="application/json"),
            db,
        )
        payload = json.loads(status.body)
        assert payload["id"] == wanted.id
        assert payload["releases"][0]["title"] == "Requested Book EPUB"

        selected = main.opds_discover_select_download(
            wanted.id,
            release.id,
            request_for(user, f"/opds/discover/download/{wanted.id}/{release.id}", accept="application/json"),
            db,
        )
        payload = json.loads(selected.body)
        assert payload["selected_release_id"] == release.id
        assert payload["status"] == "downloading"


def test_opds_downloads_feed_includes_download_status_and_review_actions() -> None:
    with opds_session() as db:
        user = add_admin(db)
        book = Book(
            title="Review Me",
            primary_author="Metadata Author",
            authors_json='["Metadata Author"]',
            review_state=ReviewState.REVIEW,
            review_reason="Imported download needs review",
        )
        db.add(book)
        db.commit()
        db.add(
            WantedItem(
                user_id=user.id,
                request_key="download-review",
                source="hardcover",
                source_id="1",
                title="Review Me",
                author="Metadata Author",
                status=main.WantedStatus.AVAILABLE,
                acquired_book_id=book.id,
            )
        )
        db.commit()

        response = main.opds_downloads(
            request_for(user, "/opds/downloads", "application/opds+json"),
            db,
        )

        data = json.loads(response.body)
        titles = [item["metadata"]["title"] for item in data["publications"]]
        assert "Review Me" in titles
        assert any(
            link["href"].endswith(f"/opds/downloads/review/{book.id}")
            for publication in data["publications"]
            for link in publication["links"]
        )


def test_opds_download_review_search_and_apply_candidate(monkeypatch) -> None:
    with opds_session() as db:
        user = add_admin(db)
        book = Book(
            title="Old Title",
            primary_author="Old Author",
            authors_json='["Old Author"]',
            review_state=ReviewState.REVIEW,
        )
        db.add(book)
        db.commit()
        candidate = {
            "source": "hardcover",
            "source_id": "hc1",
            "title": "New Title",
            "authors": ["New Author"],
            "isbns": [],
            "language": "en",
            "description": "",
            "publication_date": "",
            "page_count": None,
            "series": "",
            "series_number": None,
            "confidence": 0.95,
        }
        monkeypatch.setattr(main, "find_candidates", lambda *args, **kwargs: [candidate])
        monkeypatch.setattr(main, "organise_book", lambda db, book: None)

        search = main.opds_download_review_search(
            book.id,
            request_for(user, f"/opds/downloads/review/{book.id}/search", "application/opds+json"),
            db,
            title="New Title",
            author="New Author",
        )
        data = json.loads(search.body)
        assert "New Title - New Author (95%)" in data["navigation"][0]["title"]

        main.opds_download_review_apply(
            book.id,
            0,
            request_for(user, f"/opds/downloads/review/{book.id}/apply/0", "application/opds+json"),
            db,
            title="New Title",
            author="New Author",
        )
        db.refresh(book)
        assert book.title == "New Title"
        assert book.primary_author == "New Author"
        assert book.review_state == ReviewState.READY


def test_opds_discover_search_uses_title_and_author_sources(monkeypatch) -> None:
    with opds_session() as db:
        user = add_user(db)

        monkeypatch.setattr(
            main,
            "search_discovery_books",
            lambda query, **kwargs: [
                {
                    "source": "openlibrary",
                    "source_id": "title",
                    "title": "Title Match",
                    "author": "One Writer",
                }
            ],
        )
        monkeypatch.setattr(
            main,
            "author_bibliography",
            lambda author, **kwargs: [
                {
                    "source": "openlibrary",
                    "source_id": "author",
                    "title": "Author Match",
                    "author": "Searched Writer",
                }
            ],
        )

        response = main.opds_discover_search(
            request_for(user, "/opds/discover/search", "application/opds+json"),
            db,
            q="Searched Writer",
        )

        data = json.loads(response.body)
        titles = [item["metadata"]["title"] for item in data["publications"]]
        assert titles == ["Title Match", "Author Match"]


def test_opds_discover_search_accepts_koreader_query_parameter(monkeypatch) -> None:
    with opds_session() as db:
        user = add_user(db)
        seen = {}
        monkeypatch.setattr(
            main,
            "search_discovery_books",
            lambda query, **kwargs: (seen.setdefault("query", query), [])[1],
        )
        monkeypatch.setattr(main, "author_bibliography", lambda author, **kwargs: [])

        main.opds_discover_search(
            request_for(user, "/opds/discover/search"),
            db,
            query="Le Guin",
        )

        assert seen["query"] == "Le Guin"


def test_opds_discover_search_descriptor_exposes_atom_template() -> None:
    with opds_session() as db:
        user = add_user(db)

        response = main.opds_discover_search_descriptor(
            request_for(user, "/opds/discover/search.xml"),
            db,
        )

        xml = response.body.decode()
        assert response.media_type == "application/opensearchdescription+xml"
        assert "OpenSearchDescription" in xml
        assert 'type="application/atom+xml;profile=opds-catalog;kind=acquisition"' in xml
        assert 'template="/opds/discover/search?query={searchTerms}"' in xml


def test_opds_discover_search_returns_statuses_when_providers_fail(monkeypatch) -> None:
    with opds_session() as db:
        user = add_user(db)
        db.add(
            WantedItem(
                user_id=user.id,
                request_key="failed-provider-status",
                source="openlibrary",
                source_id="OL1W",
                title="Queued Book",
                author="Queue Writer",
            )
        )
        db.commit()
        monkeypatch.setattr(
            main,
            "search_discovery_books",
            lambda *args, **kwargs: (_ for _ in ()).throw(httpx.ConnectError("offline")),
        )
        monkeypatch.setattr(
            main,
            "author_bibliography",
            lambda *args, **kwargs: (_ for _ in ()).throw(httpx.ConnectError("offline")),
        )

        response = main.opds_discover_search(
            request_for(user, "/opds/discover/search", "application/opds+json"),
            db,
            q="anything",
        )

        data = json.loads(response.body)
        assert data["publications"][0]["metadata"]["title"] == "Queued Book"


def test_opds_discover_search_shows_download_statuses() -> None:
    with opds_session() as db:
        user = add_user(db)
        db.add(
            WantedItem(
                user_id=user.id,
                request_key="status-key",
                source="openlibrary",
                source_id="OL1W",
                title="Queued Book",
                author="Queue Writer",
                isbn="9780000000005",
            )
        )
        db.commit()

        response = main.opds_discover_search(
            request_for(user, "/opds/discover/search", "application/opds+json"),
            db,
        )

        data = json.loads(response.body)
        assert data["publications"][0]["metadata"]["title"] == "Queued Book"
        assert data["publications"][0]["metadata"]["subtitle"] == "Download status: wanted"


def test_opds_registers_trailing_slash_aliases() -> None:
    paths = {route.path for route in main.app.routes if hasattr(route, "path")}

    assert "/opds/" in paths
    assert "/opds/title/" in paths
    assert "/opds/authors/" in paths
    assert "/opds/catalog/authors-v2" in paths
    assert "/opds/latest/" in paths
    assert "/opds/series/" in paths
    assert "/opds/author-group/{author}" in paths
    assert "/opds/author-group/{author}/about" in paths
    assert "/opds/author-folder/{author}" in paths
    assert "/opds/author-folder/{author}/info" in paths
    assert "/opds/author/{author}/index" in paths
    assert "/opds/series-folder/{series}" in paths
    assert "/opds/series-folder/{series}/info" in paths
    assert "/opds/series/{series}/index" in paths
    assert "/opds/discover/" in paths
    assert "/opds/discover/trending/{genre}" in paths
    assert "/opds/discover/trending/{genre}/{period}" in paths
    assert "/opds/discover/nyt-bestsellers/{slug}" in paths
    assert "/opds/discover/nyt-bestsellers/{slug}/{week}" in paths
    assert "/opds/discover/new-releases/{genre}" in paths
    assert "/opds/discover/new-releases/{genre}/{period}" in paths
    assert "/opds/discover/search/" in paths
    assert "/opds/discover/request/" in paths
