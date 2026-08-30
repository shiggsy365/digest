import json
from io import BytesIO
from pathlib import Path

import httpx
from PIL import Image, UnidentifiedImageError
from sqlalchemy import select
from sqlalchemy.orm import Session

from .library import organise_book, write_approved_metadata, write_sidecars
from .models import AppSetting, AuditEvent, Book, ReviewState, now
from .providers import (
    Candidate,
    available_providers,
    language_matches,
    normalise_author,
    score,
)
from .text import plain_text

DEFAULT_ORDER = ["hardcover", "google_books", "openlibrary", "isbndb"]
MAX_COVER_UPLOAD_BYTES = 10 * 1024 * 1024


def settings_map(db: Session) -> dict[str, str]:
    return {item.key: item.value for item in db.scalars(select(AppSetting)).all()}


def candidate_to_dict(candidate: Candidate) -> dict:
    return candidate.__dict__.copy()


def find_candidates(
    db: Session,
    book: Book,
    *,
    title: str | None = None,
    author: str | None = None,
    isbns: list[str] | None = None,
) -> list[dict]:
    config = settings_map(db)
    order = json.loads(config.get("metadata_provider_order", json.dumps(DEFAULT_ORDER)))
    providers = available_providers(config)
    wanted_title = title.strip() if title is not None else book.title
    wanted_author = author.strip() if author is not None else book.primary_author
    wanted_isbns = isbns if isbns is not None else json.loads(book.isbns_json or "[]")
    candidates: list[Candidate] = []
    for provider_name in order:
        provider = providers.get(provider_name)
        if not provider:
            continue
        try:
            found = provider.search(
                wanted_title, normalise_author(wanted_author), wanted_isbns
            )
            found = [
                item
                for item in found
                if language_matches(item.language, config.get("default_language", "en"))
            ]
            for item in found:
                item.confidence = score(item, wanted_title, wanted_author, wanted_isbns)
            candidates.extend(found)
            if any(item.confidence == 1.0 for item in found):
                break
        except (httpx.HTTPError, ValueError) as exc:
            db.add(
                AuditEvent(
                    level="error",
                    event="metadata_provider",
                    message=f"{provider_name}: {type(exc).__name__}: {exc}",
                )
            )
            db.commit()
    candidates.sort(key=lambda item: item.confidence, reverse=True)
    return [candidate_to_dict(item) for item in candidates[:20]]


def apply_candidate(
    db: Session,
    book: Book,
    data: dict,
    organise: bool = True,
    replace_existing: bool = False,
) -> None:
    locked = set(json.loads(book.locked_fields_json or "[]"))
    authors = data.get("authors") or []
    values = [
        ("title", "title", data.get("title")),
        ("authors", "primary_author", authors[0] if authors else None),
        ("authors", "authors_json", json.dumps(authors) if authors else None),
        (
            "isbns",
            "isbns_json",
            json.dumps(data.get("isbns") or [])
            if replace_existing or "isbns" in data
            else None,
        ),
        ("language", "language", data.get("language")),
        ("description", "description", plain_text(data.get("description"))),
        ("publication_date", "publication_date", data.get("publication_date")),
        ("page_count", "page_count", data.get("page_count")),
        ("series", "series", data.get("series")),
        ("series_number", "series_number", data.get("series_number")),
    ]
    for field, attribute, value in values:
        required = attribute in {"title", "primary_author", "authors_json"}
        has_value = value not in (None, "", [])
        explicit_replacement = replace_existing and (not required or has_value)
        automatic_update = not replace_existing and field not in locked and has_value
        if explicit_replacement or automatic_update:
            setattr(book, attribute, value)
    book.sort_title = book.title.casefold()
    book.metadata_source = data.get("source", "manual")
    book.match_confidence = float(data.get("confidence") or 1)
    book.review_state = ReviewState.READY
    db.commit()
    if organise:
        organise_book(db, book)
    if data.get("cover_url"):
        download_cover(book, data["cover_url"])
        write_approved_metadata(book)
        write_sidecars(book)
        db.commit()


EDITABLE_FIELDS = {
    "title",
    "authors",
    "isbns",
    "language",
    "description",
    "publication_date",
    "page_count",
    "series",
    "series_number",
}


def apply_manual_metadata(
    db: Session, book: Book, data: dict[str, str], locked_fields: list[str]
) -> None:
    title = data.get("title", "").strip() or book.title
    authors = [item.strip() for item in data.get("authors", "").split(",") if item.strip()]
    if not authors:
        authors = json.loads(book.authors_json or "[]") or [book.primary_author]
    try:
        series_number = (
            float(data["series_number"]) if data.get("series_number", "").strip() else None
        )
        page_count = int(data["page_count"]) if data.get("page_count", "").strip() else None
    except ValueError as exc:
        raise ValueError("Series number and page count must be numbers.") from exc
    if page_count is not None and page_count < 1:
        raise ValueError("Page count must be positive.")
    book.title = title
    book.sort_title = title.casefold()
    book.primary_author = authors[0]
    book.authors_json = json.dumps(authors)
    book.isbns_json = json.dumps(
        [item.strip() for item in data.get("isbns", "").split(",") if item.strip()]
    )
    for field in ("language", "publication_date", "series"):
        setattr(book, field, data.get(field, "").strip() or None)
    book.description = plain_text(data.get("description", ""))
    book.series_number = series_number
    book.page_count = page_count
    book.locked_fields_json = json.dumps(sorted(set(locked_fields) & EDITABLE_FIELDS))
    book.metadata_source = "manual"
    book.match_confidence = 1
    book.review_state = ReviewState.READY
    book.review_reason = None
    db.commit()
    organise_book(db, book)


def download_cover(book: Book, url: str) -> None:
    if not book.files:
        return
    book_dir = Path(book.files[0].path).parent
    cover = book_dir / "cover.jpg"
    try:
        response = httpx.get(url, timeout=15, follow_redirects=True)
        response.raise_for_status()
        if not response.headers.get("content-type", "").casefold().startswith("image/"):
            return
        with Image.open(BytesIO(response.content)) as source:
            source.verify()
        with Image.open(BytesIO(response.content)) as source:
            image = source.convert("RGB")
            image.save(cover, format="JPEG", quality=90, optimize=True)
        book.cover_path = str(cover)
        book.updated_at = now()
    except (httpx.HTTPError, OSError, UnidentifiedImageError, Image.DecompressionBombError):
        return


def normalise_uploaded_cover(content: bytes) -> bytes:
    if not content:
        raise ValueError("Choose an image to upload.")
    if len(content) > MAX_COVER_UPLOAD_BYTES:
        raise ValueError("Cover images must be 10 MB or smaller.")
    try:
        with Image.open(BytesIO(content)) as source:
            source.verify()
        with Image.open(BytesIO(content)) as source:
            image = source.convert("RGB")
            output = BytesIO()
            image.save(output, format="JPEG", quality=90, optimize=True)
            return output.getvalue()
    except (OSError, UnidentifiedImageError, Image.DecompressionBombError) as exc:
        raise ValueError("The uploaded cover is not a valid image.") from exc


def save_uploaded_cover(book: Book, content: bytes) -> None:
    if not book.files:
        raise ValueError("This book has no library file to store a cover beside.")
    cover = Path(book.files[0].path).parent / "cover.jpg"
    temporary = cover.with_name(".cover.jpg.digest.tmp")
    try:
        temporary.write_bytes(content)
        temporary.replace(cover)
    finally:
        temporary.unlink(missing_ok=True)
    book.cover_path = str(cover)
    book.updated_at = now()
    write_approved_metadata(book)
    write_sidecars(book)


def enrich_pending(db: Session) -> int:
    changed = 0
    config = settings_map(db)
    try:
        threshold = float(config.get("auto_match_threshold", "0.94"))
    except ValueError:
        threshold = 0.94
    for book in db.scalars(select(Book).where(Book.review_state == ReviewState.REVIEW)).all():
        candidates = find_candidates(db, book)
        if candidates and candidates[0]["confidence"] >= threshold:
            apply_candidate(db, book, candidates[0], organise=True)
            changed += 1
    return changed


def refresh_book(db: Session, book: Book) -> bool:
    candidates = find_candidates(db, book)
    if not candidates:
        db.add(
            AuditEvent(
                level="warning",
                event="metadata_refresh",
                message=f"No metadata refresh match found for {book.title} ({book.id})",
            )
        )
        db.commit()
        return False
    config = settings_map(db)
    try:
        threshold = float(config.get("auto_match_threshold", "0.94"))
    except ValueError:
        threshold = 0.94
    if candidates[0]["confidence"] < threshold:
        db.add(
            AuditEvent(
                level="warning",
                event="metadata_refresh",
                message=f"Metadata refresh match needs review for {book.title} ({book.id})",
            )
        )
        db.commit()
        return False
    apply_candidate(db, book, candidates[0], organise=False)
    write_approved_metadata(book)
    write_sidecars(book)
    db.commit()
    return True


def auto_scrape_book(db: Session, book: Book) -> bool:
    """Apply the highest-scoring provider match only when it is safe to automate."""
    candidates = find_candidates(db, book)
    if not candidates:
        db.add(AuditEvent(level="warning", event="metadata_auto_scrape",
                          message=f"No metadata match found for {book.title} ({book.id})"))
        db.commit()
        return False
    candidate = candidates[0]
    config = settings_map(db)
    try:
        threshold = max(float(config.get("auto_match_threshold", "0.94")), 0.94)
    except ValueError:
        threshold = 0.94
    language = config.get("default_language", "en")
    if (
        float(candidate.get("confidence") or 0) < threshold
        or not language_matches(candidate.get("language"), language, allow_unknown=False)
    ):
        db.add(AuditEvent(level="warning", event="metadata_auto_scrape",
                          message=(f"Metadata match needs review for {book.title} ({book.id}); "
                                   f"best confidence {float(candidate.get('confidence') or 0):.2f}")))
        db.commit()
        return False
    apply_candidate(db, book, candidate, organise=True, replace_existing=True)
    return True
