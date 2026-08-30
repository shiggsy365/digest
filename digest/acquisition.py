import hashlib
import json
import re
from datetime import timedelta
from typing import Any
from urllib.parse import urljoin, urlsplit

import httpx
from sqlalchemy import delete, select
from sqlalchemy.orm import Session

from .jobs import enqueue
from .models import AcquisitionRelease, AppSetting, Job, WantedItem, WantedStatus, now

SUPPORTED_FORMATS = {"epub", "kepub", "mobi", "azw3"}


def _text_key(value: str) -> set[str]:
    return set(re.findall(r"[a-z0-9]+", value.casefold()))


def release_score(item: WantedItem, release_title: str, release_format: str) -> float:
    wanted = _text_key(f"{item.title} {item.author}")
    found = _text_key(release_title)
    overlap = len(wanted & found) / len(wanted) if wanted else 0
    format_bonus = 0.08 if release_format in {"epub", "kepub"} else 0
    return min(overlap + format_bonus, 1.0)


class ShelfmarkAdapter:
    def __init__(self, base_url: str, client: httpx.Client | None = None):
        self.base_url = base_url.rstrip("/")
        self.client = client or httpx.Client(timeout=120)

    def search(self, item: WantedItem) -> tuple[str, list[dict[str, Any]]]:
        stages = []
        if item.isbn:
            stages.append(
                (
                    "isbn",
                    [("source", "direct_download"), ("query", item.isbn), ("isbn", item.isbn),
                     ("content_type", "ebook")],
                )
            )
        if item.author:
            stages.append(
                (
                    "author_title",
                    {"provider": "manual", "book_id": "manual-author_title", "title": item.title,
                     "author": item.author, "content_type": "ebook"},
                )
            )
        stages.append(
            ("title", {"provider": "manual", "book_id": "manual-title", "title": item.title,
                       "content_type": "ebook"})
        )
        for stage, params in stages:
            response = self.client.get(f"{self.base_url}/api/releases", params=params)
            response.raise_for_status()
            payload = response.json()
            releases = payload if isinstance(payload, list) else payload.get("releases", payload.get("results", []))
            usable = [entry for entry in releases if isinstance(entry, dict) and _release_format(entry)]
            if usable:
                return stage, usable
        return stages[-1][0], []

    def download(self, release: AcquisitionRelease) -> dict[str, Any]:
        payload = json.loads(release.download_payload_json or "{}")
        response = self.client.post(f"{self.base_url}/api/releases/download", json=payload)
        response.raise_for_status()
        result = response.json()
        if not isinstance(result, dict):
            raise TypeError("Shelfmark returned an invalid download response")
        return result

    def status(self) -> dict[str, Any]:
        response = self.client.get(f"{self.base_url}/api/status")
        response.raise_for_status()
        result = response.json()
        if not isinstance(result, dict):
            raise TypeError("Shelfmark returned an invalid status response")
        return result

    def cancel(self, external_id: str) -> None:
        response = self.client.delete(f"{self.base_url}/api/download/{external_id}/cancel")
        response.raise_for_status()


class ProwlarrAdapter:
    def __init__(self, base_url: str, api_key: str, client: httpx.Client | None = None):
        self.base_url = base_url.rstrip("/")
        self.client = client or httpx.Client(timeout=120)
        self.headers = {"X-Api-Key": api_key}

    def search(self, item: WantedItem) -> tuple[str, list[dict[str, Any]]]:
        stages = []
        if item.isbn:
            stages.append(("isbn", item.isbn))
        if item.author:
            stages.append(("author_title", f"{item.author} {item.title}"))
        stages.append(("title", item.title))
        for stage, query in stages:
            response = self.client.get(
                f"{self.base_url}/api/v1/search",
                params={"query": query, "type": "search", "categories": 7020, "limit": 100},
                headers=self.headers,
            )
            response.raise_for_status()
            payload = response.json()
            if not isinstance(payload, list):
                raise TypeError("Prowlarr returned an invalid search response")
            releases = []
            for entry in payload:
                if not isinstance(entry, dict) or str(entry.get("protocol", "")).casefold() != "usenet":
                    continue
                if entry.get("magnetUrl") or entry.get("infoHash") or not entry.get("downloadUrl"):
                    continue
                format_name = _release_format(entry)
                if not format_name:
                    continue
                normalized = dict(entry)
                normalized["format"] = format_name
                normalized["source"] = "usenet"
                normalized["source_id"] = entry.get("guid") or entry.get("id")
                releases.append(normalized)
            if releases:
                return stage, releases
        return stages[-1][0], []

    def fetch_nzb(self, release: AcquisitionRelease) -> bytes:
        payload = json.loads(release.download_payload_json or "{}")
        if str(payload.get("protocol", "")).casefold() != "usenet":
            raise ValueError("Only Usenet releases can be fetched from Prowlarr")
        raw_url = str(payload.get("downloadUrl") or "").strip()
        url = urljoin(f"{self.base_url}/", raw_url)
        expected = urlsplit(self.base_url)
        actual = urlsplit(url)
        if (actual.scheme, actual.netloc) != (expected.scheme, expected.netloc):
            raise ValueError("Prowlarr returned a download URL outside its configured origin")
        response = self.client.get(url, headers=self.headers, follow_redirects=True)
        response.raise_for_status()
        if response.history and response.url.scheme != "https":
            raise ValueError("Prowlarr redirected the NZB download to an insecure origin")
        if not response.content or len(response.content) > 20 * 1024 * 1024:
            raise ValueError("Prowlarr returned an invalid NZB file")
        return response.content


class SabnzbdAdapter:
    def __init__(
        self, base_url: str, api_key: str, category: str, client: httpx.Client | None = None
    ):
        self.base_url = base_url.rstrip("/")
        self.api_key = api_key
        self.category = category
        self.client = client or httpx.Client(timeout=120)

    def download(self, release: AcquisitionRelease) -> dict[str, Any]:
        payload = json.loads(release.download_payload_json or "{}")
        if str(payload.get("protocol", "")).casefold() != "usenet":
            raise ValueError("Only Usenet releases can be sent to SABnzbd")
        url = str(payload.get("downloadUrl") or "").strip()
        if not url or payload.get("magnetUrl") or payload.get("infoHash"):
            raise ValueError("The selected release is not a safe Usenet download")
        response = self.client.post(
            f"{self.base_url}/api",
            data={
                "mode": "addurl",
                "name": url,
                "apikey": self.api_key,
                "cat": self.category,
                "output": "json",
            },
        )
        response.raise_for_status()
        result = response.json()
        if not isinstance(result, dict) or result.get("status") is False:
            raise ValueError(str(result.get("error") if isinstance(result, dict) else "Invalid response"))
        ids = result.get("nzo_ids") or []
        if ids:
            result["nzo_id"] = ids[0]
        return result

    def download_nzb(self, nzb: bytes, filename: str) -> dict[str, Any]:
        response = self.client.post(
            f"{self.base_url}/api",
            data={
                "mode": "addfile",
                "apikey": self.api_key,
                "cat": self.category,
                "output": "json",
            },
            files={"name": (f"{filename}.nzb", nzb, "application/x-nzb")},
        )
        response.raise_for_status()
        result = response.json()
        if not isinstance(result, dict) or result.get("status") is False:
            raise ValueError(str(result.get("error") if isinstance(result, dict) else "Invalid response"))
        ids = result.get("nzo_ids") or []
        if ids:
            result["nzo_id"] = ids[0]
        return result

    def status(self, nzo_id: str) -> dict[str, Any]:
        common = {"apikey": self.api_key, "output": "json", "nzo_ids": nzo_id}
        queue = self.client.get(
            f"{self.base_url}/api", params={**common, "mode": "queue", "limit": 100}
        )
        queue.raise_for_status()
        history = self.client.get(
            f"{self.base_url}/api", params={**common, "mode": "history", "limit": 100}
        )
        history.raise_for_status()
        return {"queue": queue.json().get("queue", {}).get("slots", []),
                "history": history.json().get("history", {}).get("slots", [])}

    def cancel(self, nzo_id: str) -> None:
        response = self.client.post(
            f"{self.base_url}/api",
            data={"mode": "queue", "name": "delete", "value": nzo_id,
                  "del_files": 1, "apikey": self.api_key, "output": "json"},
        )
        response.raise_for_status()
        result = response.json()
        if not isinstance(result, dict) or result.get("status") is False:
            raise ValueError(str(result.get("error") if isinstance(result, dict) else "Invalid response"))

def _release_format(release: dict[str, Any]) -> str:
    value = str(release.get("format") or release.get("extension") or "").lower().lstrip(".")
    if value in SUPPORTED_FORMATS:
        return value
    title = str(release.get("title") or release.get("name") or "").lower()
    match = re.search(r"\b(epub|kepub|mobi|azw3)\b", title)
    return match.group(1) if match else ""


def _integer(value: Any) -> int | None:
    try:
        return int(value) if value is not None else None
    except (TypeError, ValueError):
        return None


def store_releases(
    db: Session,
    item: WantedItem,
    adapter: str,
    stage: str,
    releases: list[dict[str, Any]],
    *,
    replace: bool = True,
) -> int:
    if replace:
        db.execute(delete(AcquisitionRelease).where(AcquisitionRelease.wanted_id == item.id))
    seen: set[str] = set()
    for release in releases:
        title = str(release.get("title") or release.get("name") or item.title).strip()
        source_id = str(release.get("source_id") or release.get("id") or release.get("download_url") or "")
        if not source_id:
            source_id = hashlib.sha256(json.dumps(release, sort_keys=True).encode()).hexdigest()
        if source_id in seen:
            continue
        seen.add(source_id)
        format_name = _release_format(release)
        db.add(
            AcquisitionRelease(
                wanted_id=item.id,
                adapter=adapter,
                source=str(release.get("source") or "direct_download"),
                source_id=source_id[:300],
                title=title,
                format=format_name,
                size_bytes=_integer(release.get("size_bytes") or release.get("size")),
                seeders=_integer(release.get("seeders")),
                download_payload_json=json.dumps(release),
                match_score=release_score(item, title, format_name),
                search_stage=stage,
            )
        )
    db.commit()
    return len(seen)


def queue_release(db: Session, item: WantedItem, release: AcquisitionRelease) -> None:
    if release.wanted_id != item.id or item.status != WantedStatus.WANTED:
        raise ValueError("This release cannot be downloaded")
    item.selected_release_id = release.id
    item.download_adapter = release.adapter
    item.status = WantedStatus.DOWNLOADING
    item.last_error = None
    db.commit()
    enqueue(db, "acquisition_download", payload_json=json.dumps({"wanted_id": item.id}))


def automatic_release(db: Session, item: WantedItem) -> AcquisitionRelease | None:
    """Return one unambiguous, lossless, exact-match release for automatic selection."""
    candidates = db.scalars(
        select(AcquisitionRelease)
        .where(AcquisitionRelease.wanted_id == item.id,
               AcquisitionRelease.match_score >= 0.98,
               AcquisitionRelease.format.in_(("epub", "kepub")))
        .order_by(AcquisitionRelease.match_score.desc(), AcquisitionRelease.format.asc(),
                  AcquisitionRelease.adapter.desc(), AcquisitionRelease.id)
    ).all()
    if not candidates:
        return None
    best = candidates[0]
    equally_ranked = [release for release in candidates
                      if release.match_score == best.match_score and release.format == best.format]
    return best if len(equally_ranked) == 1 else None


def cancel_acquisition(db: Session, item: WantedItem) -> None:
    if item.status == WantedStatus.IMPORTING:
        raise ValueError("The download has completed and is being imported")
    if item.status == WantedStatus.DOWNLOADING and item.external_download_id:
        if item.download_adapter == "shelfmark":
            url = db.get(AppSetting, "shelfmark_url")
            if not url or not url.value.strip():
                raise ValueError("Shelfmark is unavailable")
            ShelfmarkAdapter(url.value).cancel(item.external_download_id)
        elif item.download_adapter == "sabnzbd":
            url = db.get(AppSetting, "sabnzbd_url")
            key = db.get(AppSetting, "sabnzbd_api_key")
            category = db.get(AppSetting, "sabnzbd_category")
            if not url or not key or not url.value.strip() or not key.value.strip():
                raise ValueError("SABnzbd is unavailable")
            SabnzbdAdapter(url.value, key.value, category.value if category else "ebooks").cancel(
                item.external_download_id
            )
    if item.status != WantedStatus.AVAILABLE:
        item.status = WantedStatus.CANCELLED
        item.last_error = None
        db.commit()


def retry_acquisition(db: Session, item: WantedItem) -> None:
    if item.status != WantedStatus.FAILED:
        raise ValueError("Only failed downloads can be retried")
    item.status = WantedStatus.WANTED
    item.selected_release_id = None
    item.download_adapter = None
    item.external_download_id = None
    item.status_payload_json = "{}"
    item.last_error = None
    db.commit()


def _external_id(payload: dict[str, Any], fallback: str) -> str:
    for key in ("download_id", "job_id", "queue_id", "nzo_id", "id"):
        value = payload.get(key)
        if value is not None and str(value).strip():
            return str(value).strip()[:300]
    return fallback[:300]


def run_acquisition_download(db: Session, job: Job) -> None:
    wanted_id = json.loads(job.payload_json or "{}").get("wanted_id")
    item = db.get(WantedItem, wanted_id) if wanted_id else None
    if item is None or item.status != WantedStatus.DOWNLOADING or not item.selected_release_id:
        return
    release = db.get(AcquisitionRelease, item.selected_release_id)
    if release is None:
        raise ValueError("The selected acquisition release is unavailable")
    if release.adapter == "shelfmark":
        url = db.get(AppSetting, "shelfmark_url")
        if not url or not url.value.strip():
            raise ValueError("Shelfmark is unavailable")
        result = ShelfmarkAdapter(url.value).download(release)
        item.download_adapter = "shelfmark"
    elif release.adapter == "prowlarr":
        prowlarr_url = db.get(AppSetting, "prowlarr_url")
        prowlarr_key = db.get(AppSetting, "prowlarr_api_key")
        url = db.get(AppSetting, "sabnzbd_url")
        key = db.get(AppSetting, "sabnzbd_api_key")
        category = db.get(AppSetting, "sabnzbd_category")
        if not all(
            setting and setting.value.strip()
            for setting in (prowlarr_url, prowlarr_key, url, key)
        ):
            raise ValueError("Prowlarr or SABnzbd is unavailable")
        nzb = ProwlarrAdapter(prowlarr_url.value, prowlarr_key.value).fetch_nzb(release)
        result = SabnzbdAdapter(
            url.value, key.value, category.value if category else "ebooks"
        ).download_nzb(
            nzb, release.title
        )
        item.download_adapter = "sabnzbd"
    else:
        raise ValueError("The selected acquisition adapter is unavailable")
    item.external_download_id = _external_id(
        result, str(result.get("nzo_id") or release.source_id)
    )
    item.status_payload_json = json.dumps(result)
    db.commit()
    enqueue(
        db,
        "acquisition_monitor",
        payload_json=json.dumps({"wanted_id": item.id}),
        run_after=now() + timedelta(seconds=30),
    )


def _queue_state(payload: dict[str, Any], external_id: str) -> tuple[str, dict[str, Any] | None]:
    for bucket, values in payload.items():
        entries = (
            values
            if isinstance(values, list)
            else list(values.values())
            if isinstance(values, dict)
            else []
        )
        for entry in entries:
            if not isinstance(entry, dict):
                continue
            identifiers = {
                str(entry.get(key) or "")
                for key in ("download_id", "job_id", "queue_id", "nzo_id", "id")
            }
            if external_id not in identifiers:
                continue
            entry_status = str(entry.get("status") or "").casefold()
            if entry_status in {"failed", "error"}:
                return "failed", entry
            if entry_status in {"completed", "complete"}:
                return "complete", entry
            name = bucket.casefold()
            if any(value in name for value in ("complete", "done", "history")):
                return "complete", entry
            if any(value in name for value in ("fail", "error")):
                return "failed", entry
            return "downloading", entry
    return "downloading", None


def run_acquisition_monitor(db: Session, job: Job) -> None:
    wanted_id = json.loads(job.payload_json or "{}").get("wanted_id")
    item = db.get(WantedItem, wanted_id) if wanted_id else None
    if item is None or item.status != WantedStatus.DOWNLOADING or not item.external_download_id:
        return
    if item.download_adapter == "sabnzbd":
        url = db.get(AppSetting, "sabnzbd_url")
        key = db.get(AppSetting, "sabnzbd_api_key")
        category = db.get(AppSetting, "sabnzbd_category")
        if not url or not key or not url.value.strip() or not key.value.strip():
            raise ValueError("SABnzbd is unavailable")
        payload = SabnzbdAdapter(
            url.value, key.value, category.value if category else "ebooks"
        ).status(item.external_download_id)
    else:
        url = db.get(AppSetting, "shelfmark_url")
        if not url or not url.value.strip():
            raise ValueError("Shelfmark is unavailable")
        payload = ShelfmarkAdapter(url.value).status()
    state, entry = _queue_state(payload, item.external_download_id)
    item.status_payload_json = json.dumps(entry or payload)
    if state == "failed":
        item.status = WantedStatus.FAILED
        item.last_error = str((entry or {}).get("error") or "Acquisition download failed")
    elif state == "complete":
        item.status = WantedStatus.IMPORTING
        enqueue(db, "library_scan")
    else:
        enqueue(
            db,
            "acquisition_monitor",
            payload_json=json.dumps({"wanted_id": item.id}),
            run_after=now() + timedelta(seconds=30),
        )
    db.commit()


def reconcile_importing(db: Session) -> int:
    from .discovery import find_library_book
    from .library import organise_book

    updated = 0
    items = db.scalars(
        select(WantedItem).where(
            (WantedItem.status == WantedStatus.IMPORTING)
            | (
                (WantedItem.status == WantedStatus.CANCELLED)
                & WantedItem.external_download_id.is_not(None)
            )
        )
    ).all()
    for item in items:
        book = find_library_book(
            db,
            title=item.title,
            author=item.author,
            isbn=item.isbn,
            include_review=True,
        )
        if book is None:
            continue
        # Acquisition imports use the same canonical author/title path rules as approved books,
        # while preserving metadata-review state for incomplete downloads.
        organise_book(db, book, approve=False)
        item.acquired_book_id = book.id
        item.status = WantedStatus.AVAILABLE
        item.last_error = None
        updated += 1
    db.commit()
    return updated


def mark_acquisition_failed(db: Session, job: Job, error: Exception) -> None:
    wanted_id = json.loads(job.payload_json or "{}").get("wanted_id")
    item = db.get(WantedItem, wanted_id) if wanted_id else None
    if item is None:
        return
    item.status = WantedStatus.FAILED
    item.last_error = f"{type(error).__name__}: {error}"[:4000]
    db.commit()


def request_key(source: str, source_id: str, title: str, author: str, isbn: str) -> str:
    identity = isbn.strip() or source_id.strip() or f"{title.strip()}|{author.strip()}"
    identity = re.sub(r"[^a-z0-9]+", "", identity.casefold())
    return hashlib.sha256(f"{source.casefold()}:{identity}".encode()).hexdigest()


def create_wanted(
    db: Session,
    *,
    user_id: int,
    source: str,
    source_id: str,
    title: str,
    author: str,
    isbn: str,
    cover_url: str,
) -> WantedItem:
    key = request_key(source, source_id, title, author, isbn)
    item = db.scalar(
        select(WantedItem).where(WantedItem.user_id == user_id, WantedItem.request_key == key)
    )
    should_enqueue = False
    if item is None:
        item = WantedItem(
            user_id=user_id,
            request_key=key,
            source=source.strip(),
            source_id=source_id.strip(),
            title=title.strip(),
            author=author.strip(),
            isbn=isbn.strip(),
            cover_url=cover_url.strip() or None,
        )
        db.add(item)
        db.flush()
        should_enqueue = True
    elif item.status in {WantedStatus.CANCELLED, WantedStatus.FAILED}:
        item.status = WantedStatus.WANTED
        item.attempts = 0
        item.last_error = None
        should_enqueue = True
    db.commit()
    if should_enqueue:
        enqueue(db, "acquisition_search", payload_json=f'{{"wanted_id":{item.id}}}')
    return item


def find_wanted(
    db: Session, *, user_id: int, source: str, source_id: str, title: str, author: str, isbn: str
) -> WantedItem | None:
    key = request_key(source, source_id, title, author, isbn)
    return db.scalar(
        select(WantedItem).where(WantedItem.user_id == user_id, WantedItem.request_key == key)
    )


def run_acquisition_search(db: Session, job: Job) -> None:
    wanted_id = json.loads(job.payload_json or "{}").get("wanted_id")
    item = db.get(WantedItem, wanted_id) if wanted_id else None
    if item is None or item.status in {WantedStatus.CANCELLED, WantedStatus.AVAILABLE}:
        return
    item.attempts += 1
    adapters: list[tuple[str, Any]] = []
    shelfmark_enabled = db.get(AppSetting, "shelfmark_enabled")
    shelfmark_url = db.get(AppSetting, "shelfmark_url")
    if (
        shelfmark_enabled
        and shelfmark_enabled.value == "true"
        and shelfmark_url
        and shelfmark_url.value.strip()
    ):
        adapters.append(("shelfmark", ShelfmarkAdapter(shelfmark_url.value)))
    usenet_enabled = db.get(AppSetting, "usenet_enabled")
    prowlarr_url = db.get(AppSetting, "prowlarr_url")
    prowlarr_key = db.get(AppSetting, "prowlarr_api_key")
    if (
        usenet_enabled
        and usenet_enabled.value == "true"
        and prowlarr_url
        and prowlarr_key
        and prowlarr_url.value.strip()
        and prowlarr_key.value.strip()
    ):
        adapters.append(
            ("prowlarr", ProwlarrAdapter(prowlarr_url.value, prowlarr_key.value))
        )
    if adapters:
        item.status = WantedStatus.SEARCHING
        db.commit()
        db.execute(delete(AcquisitionRelease).where(AcquisitionRelease.wanted_id == item.id))
        db.commit()
        count = 0
        adapter_errors = []
        for adapter_name, adapter in adapters:
            try:
                stage, releases = adapter.search(item)
                count += store_releases(
                    db, item, adapter_name, stage, releases, replace=False
                )
            except (httpx.HTTPError, TypeError, ValueError) as exc:
                adapter_errors.append(f"{adapter_name}: {type(exc).__name__}: {exc}")
        item.status = WantedStatus.WANTED
        if count:
            item.last_error = None
        elif adapter_errors:
            item.last_error = "; ".join(adapter_errors)[:4000]
        else:
            item.last_error = "No matching ebook releases found"
        if count:
            selected = automatic_release(db, item)
            if selected is not None:
                queue_release(db, item, selected)
    else:
        item.last_error = "Waiting for an enabled acquisition adapter"
    item.next_search_at = now() + timedelta(days=1)
    if item.status != WantedStatus.DOWNLOADING:
        item.status = WantedStatus.WANTED
    db.commit()
    if item.status == WantedStatus.WANTED:
        enqueue(db, "acquisition_search", payload_json=json.dumps({"wanted_id": item.id}),
                run_after=item.next_search_at)
