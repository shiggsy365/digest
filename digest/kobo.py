import hashlib
import json
import secrets
import uuid
from datetime import UTC, datetime
from pathlib import Path

from fastapi import HTTPException
from sqlalchemy import or_, select
from sqlalchemy.orm import Session

from .models import (
    ApiToken,
    Book,
    BookFile,
    KoboSyncedBook,
    KoboSyncedShelf,
    ReadingState,
    ReviewState,
    Role,
    Shelf,
    ShelfBook,
    User,
    now,
)
from .security import KOBO_TOKEN_NAME, token_digest


def kobo_user(db: Session, token: str) -> User:
    item = db.scalar(
        select(ApiToken).where(
            ApiToken.name == KOBO_TOKEN_NAME,
            ApiToken.token_hash == token_digest(token),
            ApiToken.revoked_at.is_(None),
        )
    )
    user = db.get(User, item.user_id) if item else None
    if not user or not user.is_active:
        raise HTTPException(401, "Invalid Kobo device token")
    return user


def active_kobo_token(db: Session, user: User) -> ApiToken | None:
    return db.scalar(
        select(ApiToken)
        .where(
            ApiToken.name == KOBO_TOKEN_NAME,
            ApiToken.user_id == user.id,
            ApiToken.revoked_at.is_(None),
        )
        .order_by(ApiToken.created_at.desc())
    )


def shelf_books(db: Session, user: User) -> list[Book]:
    if user.kobo_sync_all_books:
        return list(db.scalars(
            select(Book).where(Book.review_state == ReviewState.READY)
            .order_by(Book.created_at, Book.id)
        ))
    if user.kobo_sync_shelf_id is None:
        return []
    return list(
        db.scalars(
            select(Book)
            .join(ShelfBook, ShelfBook.book_id == Book.id)
            .where(ShelfBook.shelf_id == user.kobo_sync_shelf_id)
            .order_by(Book.created_at, Book.id)
        ).unique()
    )


def shelf_book(db: Session, user: User, book_id: str) -> Book:
    book = next((book for book in shelf_books(db, user) if book.id == book_id), None)
    if not book:
        raise HTTPException(404, "Book is not on this device's sync shelf")
    return book


def accessible_kobo_shelves(db: Session, user: User) -> list[Shelf]:
    return list(
        db.scalars(
            select(Shelf)
            .where(or_(Shelf.owner_id == user.id, Shelf.shared.is_(True)))
            .order_by(Shelf.id)
        )
    )


def shelf_tag_id(shelf_id: int) -> str:
    return str(uuid.uuid5(uuid.NAMESPACE_URL, f"digest:shelf:{shelf_id}"))


def shelf_for_tag(db: Session, user: User, tag_id: str) -> Shelf:
    shelf = next(
        (
            item
            for item in accessible_kobo_shelves(db, user)
            if secrets.compare_digest(shelf_tag_id(item.id), tag_id)
        ),
        None,
    )
    if not shelf:
        raise HTTPException(404, "Unknown Kobo collection")
    return shelf


def can_edit_shelf(user: User, shelf: Shelf) -> bool:
    return shelf.owner_id == user.id or user.role == Role.ADMIN


def preferred_file(book: Book) -> BookFile | None:
    supported = [item for item in book.files if item.format in {"kepub", "epub"}]
    supported.sort(key=lambda item: (item.format != "kepub", item.id))
    return next((item for item in supported if Path(item.path).is_file()), None)


def timestamp(value: datetime | str | None) -> str:
    if isinstance(value, datetime):
        if value.tzinfo is None:
            value = value.replace(tzinfo=UTC)
        return value.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
    if value:
        try:
            parsed = datetime.fromisoformat(str(value))
            return timestamp(parsed)
        except ValueError:
            pass
    return "1970-01-01T00:00:00Z"


def revision(value: datetime | None) -> str:
    if value is None:
        return ""
    if value.tzinfo is None:
        value = value.replace(tzinfo=UTC)
    return value.astimezone(UTC).isoformat(timespec="microseconds")


def metadata(book: Book, base_url: str, token: str) -> dict:
    item = preferred_file(book)
    download_urls = []
    if item:
        kobo_formats = ["KEPUB"] if item.format == "kepub" else ["EPUB3", "EPUB"]
        download_urls = [
            {
                "Format": book_format,
                "Size": item.size_bytes,
                "Url": f"{base_url}/kobo/{token}/download/{book.id}",
                "Platform": "Generic",
            }
            for book_format in kobo_formats
        ]
    try:
        authors = json.loads(book.authors_json)
    except (TypeError, json.JSONDecodeError):
        authors = []
    authors = [str(author) for author in authors if author] or [book.primary_author]
    result = {
        "Categories": ["00000000-0000-0000-0000-000000000001"],
        "ContributorRoles": [{"Name": author} for author in authors],
        "Contributors": authors,
        "CoverImageId": book.id,
        "CrossRevisionId": book.id,
        "CurrentDisplayPrice": {"CurrencyCode": "GBP", "TotalAmount": 0},
        "CurrentLoveDisplayPrice": {"TotalAmount": 0},
        "Description": book.description,
        "DownloadUrls": download_urls,
        "EntitlementId": book.id,
        "ExternalIds": [],
        "Genre": "00000000-0000-0000-0000-000000000001",
        "IsEligibleForKoboLove": False,
        "IsInternetArchive": False,
        "IsPreOrder": False,
        "IsSocialEnabled": False,
        "Language": book.language or "en",
        "PhoneticPronunciations": {},
        "PublicationDate": timestamp(book.publication_date),
        "Publisher": {"Imprint": "", "Name": None},
        "RevisionId": book.id,
        "Title": book.title,
        "WorkId": book.id,
    }
    if book.series:
        number = book.series_number or 1
        result["Series"] = {
            "Name": book.series,
            "Number": number,
            "NumberFloat": float(number),
            "Id": str(uuid.uuid3(uuid.NAMESPACE_DNS, book.series)),
        }
    return result


def entitlement(book: Book, removed: bool = False) -> dict:
    return {
        "Accessibility": "Full",
        "ActivePeriod": {"From": timestamp(book.created_at)},
        "Created": timestamp(book.created_at),
        "CrossRevisionId": book.id,
        "Id": book.id,
        "IsRemoved": removed,
        "IsHiddenFromArchive": False,
        "IsLocked": False,
        "LastModified": timestamp(book.updated_at),
        "OriginCategory": "Imported",
        "RevisionId": book.id,
        "Status": "Active",
    }


def get_reading_state(db: Session, user: User, book: Book) -> ReadingState:
    state = db.scalar(
        select(ReadingState).where(
            ReadingState.user_id == user.id,
            ReadingState.book_id == book.id,
        )
    )
    if state is None:
        state = ReadingState(user_id=user.id, book_id=book.id)
        db.add(state)
        db.flush()
    return state


def kobo_status(state: str) -> str:
    return {"reading": "Reading", "finished": "Finished"}.get(state, "ReadyToRead")


def reading_state_payload(book: Book, state: ReadingState) -> dict:
    modified = timestamp(state.updated_at)
    try:
        location = json.loads(state.location_json or "{}")
    except (TypeError, json.JSONDecodeError):
        location = {}
    bookmark = {"LastModified": modified}
    if state.progress_percent is not None:
        progress = state.progress_percent
        progress = int(progress) if progress.is_integer() else progress
        bookmark.update(
            {
                "ProgressPercent": progress,
                "ContentSourceProgressPercent": progress,
            }
        )
    if location:
        bookmark["Location"] = location
    statistics = {"LastModified": modified}
    if state.spent_reading_minutes is not None:
        statistics["SpentReadingMinutes"] = state.spent_reading_minutes
    if state.remaining_time_minutes is not None:
        statistics["RemainingTimeMinutes"] = state.remaining_time_minutes
    return {
        "EntitlementId": book.id,
        "Created": timestamp(book.created_at),
        "LastModified": modified,
        "PriorityTimestamp": modified,
        "StatusInfo": {
            "LastModified": modified,
            "Status": kobo_status(state.state),
            "TimesStartedReading": 1 if state.state in {"reading", "finished"} else 0,
        },
        "Statistics": statistics,
        "CurrentBookmark": bookmark,
    }


def update_reading_state(
    db: Session, user: User, book: Book, payload: dict
) -> tuple[ReadingState, dict]:
    try:
        requested = payload["ReadingStates"][0]
    except (KeyError, IndexError, TypeError) as exc:
        raise ValueError("Malformed Kobo reading-state request") from exc
    if not isinstance(requested, dict):
        raise TypeError("Malformed Kobo reading-state request")
    bookmark = requested.get("CurrentBookmark")
    progress = None
    location = None
    if bookmark:
        try:
            progress = float(bookmark["ProgressPercent"])
        except (KeyError, TypeError, ValueError) as exc:
            raise ValueError("Invalid Kobo reading progress") from exc
        if not 0 <= progress <= 100:
            raise ValueError("Invalid Kobo reading progress")
        location = bookmark.get("Location")
    statistics = requested.get("Statistics")
    spent_minutes = None
    remaining_minutes = None
    if statistics:
        try:
            if "SpentReadingMinutes" in statistics:
                spent_minutes = max(0, int(statistics["SpentReadingMinutes"]))
            if "RemainingTimeMinutes" in statistics:
                remaining_minutes = max(0, int(statistics["RemainingTimeMinutes"]))
        except (TypeError, ValueError) as exc:
            raise ValueError("Invalid Kobo reading statistics") from exc
    status_info = requested.get("StatusInfo")
    digest_status = None
    if status_info:
        statuses = {"ReadyToRead": "unread", "Reading": "reading", "Finished": "finished"}
        try:
            digest_status = statuses[status_info["Status"]]
        except (KeyError, TypeError) as exc:
            raise ValueError("Invalid Kobo reading status") from exc

    state = get_reading_state(db, user, book)
    result = {"EntitlementId": book.id}
    if bookmark:
        state.progress_percent = progress
        state.location_json = json.dumps(location if isinstance(location, dict) else {})
        result["CurrentBookmarkResult"] = {"Result": "Success"}
    if statistics:
        if spent_minutes is not None:
            state.spent_reading_minutes = spent_minutes
        if remaining_minutes is not None:
            state.remaining_time_minutes = remaining_minutes
        result["StatisticsResult"] = {"Result": "Success"}
    if status_info:
        state.state = digest_status
        result["StatusInfoResult"] = {"Result": "Success"}
    state.updated_at = now()
    db.commit()
    result["LastModified"] = timestamp(state.updated_at)
    result["PriorityTimestamp"] = timestamp(state.updated_at)
    return state, result


def tag_payload(db: Session, shelf: Shelf, available_book_ids: set[str]) -> tuple[dict, str]:
    book_ids = sorted(
        set(
            db.scalars(
                select(ShelfBook.book_id).where(
                    ShelfBook.shelf_id == shelf.id,
                    ShelfBook.book_id.in_(available_book_ids),
                )
            )
        )
    )
    fingerprint = json.dumps(
        {"name": shelf.name, "shared": shelf.shared, "books": book_ids},
        sort_keys=True,
    )
    revision_value = hashlib.sha256(fingerprint.encode()).hexdigest()
    return (
        {
            "Created": "1970-01-01T00:00:00Z",
            "Id": shelf_tag_id(shelf.id),
            "Items": [
                {"RevisionId": book_id, "Type": "ProductRevisionTagItem"}
                for book_id in book_ids
            ],
            "LastModified": timestamp(now()),
            "Name": shelf.name,
            "Type": "UserTag",
        },
        revision_value,
    )


def sync_tags(
    db: Session, user: User, available_book_ids: set[str], results: list[dict]
) -> None:
    shelves = {shelf.id: shelf for shelf in accessible_kobo_shelves(db, user)}
    tracked = {
        item.shelf_id: item
        for item in db.scalars(
            select(KoboSyncedShelf).where(KoboSyncedShelf.user_id == user.id)
        ).all()
    }
    for shelf_id in sorted(set(tracked) - set(shelves)):
        results.append(
            {
                "DeletedTag": {
                    "Tag": {
                        "Id": shelf_tag_id(shelf_id),
                        "LastModified": timestamp(now()),
                    }
                }
            }
        )
        db.delete(tracked[shelf_id])
    for shelf in shelves.values():
        tag, tag_revision = tag_payload(db, shelf, available_book_ids)
        item = tracked.get(shelf.id)
        if item is None:
            results.append({"NewTag": {"Tag": tag}})
            db.add(
                KoboSyncedShelf(
                    user_id=user.id,
                    shelf_id=shelf.id,
                    revision=tag_revision,
                )
            )
        elif item.revision != tag_revision:
            results.append({"ChangedTag": {"Tag": tag}})
            item.revision = tag_revision
            item.synced_at = now()


def archive_from_device(db: Session, user: User, book_id: str) -> None:
    if user.kobo_sync_shelf_id is None and not user.kobo_sync_all_books:
        return
    shelf = db.get(Shelf, user.kobo_sync_shelf_id) if user.kobo_sync_shelf_id else None
    if not shelf and not user.kobo_sync_all_books:
        return
    membership = db.scalar(
        select(ShelfBook).where(
            ShelfBook.shelf_id == shelf.id,
            ShelfBook.book_id == book_id,
        )
    ) if shelf else None
    tracked = db.scalar(
        select(KoboSyncedBook).where(
            KoboSyncedBook.user_id == user.id,
            KoboSyncedBook.book_id == book_id,
        )
    )
    if not membership and not tracked:
        # Kobo may still be cleaning up entitlements created by a previous
        # sync server. DELETE is idempotent, so an unknown book is already in
        # the requested state and must not abort the device's incremental sync.
        return
    if membership and shelf and can_edit_shelf(user, shelf):
        db.delete(membership)
        if tracked:
            db.delete(tracked)
    elif tracked:
        tracked.archived = True
        tracked.synced_at = now()
    db.commit()


def requested_tag_book_ids(payload: dict) -> list[str]:
    items = payload.get("Items")
    if not isinstance(items, list):
        raise TypeError("Malformed Kobo collection items")
    return [
        str(item["RevisionId"])
        for item in items
        if isinstance(item, dict)
        and item.get("Type") == "ProductRevisionTagItem"
        and item.get("RevisionId")
    ]


def add_tag_items(db: Session, user: User, shelf: Shelf, payload: dict) -> None:
    if not can_edit_shelf(user, shelf):
        raise HTTPException(403, "This Kobo collection is read-only")
    synced_ids = set(
        db.scalars(
            select(KoboSyncedBook.book_id).where(
                KoboSyncedBook.user_id == user.id,
                KoboSyncedBook.archived.is_(False),
            )
        )
    )
    existing = set(
        db.scalars(select(ShelfBook.book_id).where(ShelfBook.shelf_id == shelf.id))
    )
    for book_id in requested_tag_book_ids(payload):
        if book_id in synced_ids and book_id not in existing:
            db.add(ShelfBook(shelf_id=shelf.id, book_id=book_id))
    db.commit()


def remove_tag_items(db: Session, user: User, shelf: Shelf, payload: dict) -> None:
    if not can_edit_shelf(user, shelf):
        raise HTTPException(403, "This Kobo collection is read-only")
    book_ids = requested_tag_book_ids(payload)
    if book_ids:
        db.execute(
            ShelfBook.__table__.delete().where(
                ShelfBook.shelf_id == shelf.id,
                ShelfBook.book_id.in_(book_ids),
            )
        )
    db.commit()


def create_tag(db: Session, user: User, payload: dict) -> Shelf:
    name = str(payload.get("Name") or "").strip()
    if not name or len(name) > 160:
        raise ValueError("Kobo collection name must contain 1 to 160 characters")
    shelf = db.scalar(
        select(Shelf).where(Shelf.owner_id == user.id, Shelf.name == name)
    )
    if shelf is None:
        shelf = Shelf(name=name, owner_id=user.id, shared=False)
        db.add(shelf)
        db.flush()
    add_tag_items(db, user, shelf, payload)
    return shelf


def update_tag(db: Session, user: User, shelf: Shelf, payload: dict) -> None:
    if not can_edit_shelf(user, shelf):
        raise HTTPException(403, "This Kobo collection is read-only")
    name = str(payload.get("Name") or "").strip()
    if not name or len(name) > 160:
        raise ValueError("Kobo collection name must contain 1 to 160 characters")
    shelf.name = name
    db.commit()


def delete_tag(db: Session, user: User, shelf: Shelf) -> None:
    if not can_edit_shelf(user, shelf):
        raise HTTPException(403, "This Kobo collection is read-only")
    if user.kobo_sync_shelf_id == shelf.id:
        user.kobo_sync_shelf_id = None
    db.execute(ShelfBook.__table__.delete().where(ShelfBook.shelf_id == shelf.id))
    db.delete(shelf)
    db.commit()


def sync_payload(db: Session, user: User, base_url: str, token: str) -> list[dict]:
    results = []
    books = {book.id: book for book in shelf_books(db, user) if preferred_file(book)}
    tracked = {
        item.book_id: item
        for item in db.scalars(
            select(KoboSyncedBook).where(KoboSyncedBook.user_id == user.id)
        ).all()
    }
    for book_id in sorted(set(tracked) - set(books)):
        item = tracked[book_id]
        if item.archived:
            db.delete(item)
            continue
        removed_at = timestamp(now())
        results.append(
            {
                "ChangedEntitlement": {
                    "BookEntitlement": {
                        "Accessibility": "Full",
                        "CrossRevisionId": book_id,
                        "Id": book_id,
                        "IsRemoved": True,
                        "IsHiddenFromArchive": False,
                        "IsLocked": False,
                        "LastModified": removed_at,
                        "OriginCategory": "Imported",
                        "RevisionId": book_id,
                        "Status": "Active",
                    }
                }
            }
        )
        db.delete(item)
    for book in books.values():
        state = get_reading_state(db, user, book)
        book_revision = revision(book.updated_at)
        reading_revision = revision(state.updated_at)
        item = tracked.get(book.id)
        if item is not None and item.archived:
            continue
        if item is None:
            results.append(
                {
                    "NewEntitlement": {
                        "BookEntitlement": entitlement(book),
                        "BookMetadata": metadata(book, base_url, token),
                        "ReadingState": reading_state_payload(book, state),
                    }
                }
            )
            db.add(
                KoboSyncedBook(
                    user_id=user.id,
                    book_id=book.id,
                    book_revision=book_revision,
                    reading_revision=reading_revision,
                )
            )
            continue
        if item.book_revision != book_revision:
            results.append(
                {
                    "ChangedEntitlement": {
                        "BookEntitlement": entitlement(book),
                        "BookMetadata": metadata(book, base_url, token),
                        "ReadingState": reading_state_payload(book, state),
                    }
                }
            )
        elif item.reading_revision != reading_revision:
            results.append(
                {
                    "ChangedReadingState": {
                        "ReadingState": reading_state_payload(book, state),
                    }
                }
            )
        item.book_revision = book_revision
        item.reading_revision = reading_revision
        item.synced_at = now()
    available_book_ids = {
        book_id
        for book_id in books
        if tracked.get(book_id) is None or not tracked[book_id].archived
    }
    sync_tags(db, user, available_book_ids, results)
    db.commit()
    return results


def initialization(base_url: str, token: str) -> dict:
    """Return Kobo endpoints rooted at Digest's configured public URL.

    Kobo continues to use these URLs after the initial request. Building them
    from the incoming request is unsafe behind a TLS-terminating proxy: the
    application commonly sees HTTP even though the device connected by HTTPS.
    """
    base_url = base_url.rstrip("/")
    device_base = f"{base_url}/kobo/{token}"
    return {
        "Resources": {
            "account_page": "https://www.kobo.com/account/settings",
            "add_device": "https://storeapi.kobo.com/v1/user/add-device",
            "add_entitlement": "https://storeapi.kobo.com/v1/library/{RevisionIds}",
            "assets": "https://storeapi.kobo.com/v1/assets",
            "book": "https://storeapi.kobo.com/v1/products/books/{ProductId}",
            "configuration_data": "https://storeapi.kobo.com/v1/configuration",
            "delete_entitlement": f"{device_base}/v1/library/{{Ids}}",
            "delete_tag": f"{device_base}/v1/library/tags/{{TagId}}",
            "delete_tag_items": f"{device_base}/v1/library/tags/{{TagId}}/items/delete",
            "device_auth": f"{device_base}/v1/auth/device",
            "device_refresh": f"{device_base}/v1/auth/refresh",
            "dictionary_host": "https://ereaderfiles.kobo.com",
            "discovery_host": "https://discovery.kobobooks.com",
            "image_host": base_url,
            "image_url_template": f"{device_base}/cover/{{ImageId}}/{{width}}/{{height}}/false/image.jpg",
            "image_url_quality_template": f"{device_base}/cover/{{ImageId}}/{{width}}/{{height}}/{{Quality}}/{{isGreyscale}}/image.jpg",
            "library_book": f"{device_base}/v1/user/library/books/{{LibraryItemId}}",
            "library_items": f"{device_base}/v1/user/library",
            "library_sync": f"{device_base}/v1/library/sync",
            "library_metadata": f"{device_base}/v1/library/{{Ids}}/metadata",
            "reading_state": f"{device_base}/v1/library/{{Ids}}/state",
            "rename_tag": f"{device_base}/v1/library/tags/{{TagId}}",
            "store_host": "www.kobo.com",
            "tag_items": f"{device_base}/v1/library/tags/{{TagId}}/items",
            "tags": f"{device_base}/v1/library/tags",
            "use_one_store": "True",
            "user_loyalty_benefits": f"{device_base}/v1/user/loyalty/benefits",
            "user_profile": "https://storeapi.kobo.com/v1/user/profile",
        }
    }


def dummy_auth(payload: dict | None) -> dict:
    payload = payload or {}
    return {
        "AccessToken": secrets.token_urlsafe(24),
        "RefreshToken": secrets.token_urlsafe(24),
        "TokenType": "Bearer",
        "TrackingId": str(uuid.uuid4()),
        "UserKey": payload.get("UserKey", ""),
    }
