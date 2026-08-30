"""JSON API used by the flag-gated e-reader single-page client."""

import json
import secrets
from typing import Annotated, Any

import httpx
from fastapi import APIRouter, Depends, HTTPException, Request
from sqlalchemy import func, or_, select
from sqlalchemy.orm import Session

from .acquisition import cancel_acquisition, create_wanted, queue_release, retry_acquisition
from .config import get_settings
from .db import get_db
from .discovery import (
    GENRES,
    HARDCOVER_TRENDING_PERIODS,
    NYT_FALLBACK_LISTS,
    build_discovery,
    hardcover_books,
    nyt_bestsellers,
    nyt_weekly_lists,
    nyt_weeks,
    search_discovery_books,
)
from .kobo import active_kobo_token
from .metadata import settings_map
from .models import (
    AcquisitionRelease,
    Book,
    KoboSyncedBook,
    KoboSyncedShelf,
    ReadingState,
    ReviewState,
    Role,
    Shelf,
    ShelfBook,
    User,
    WantedItem,
    WantedStatus,
)
from .security import KOBO_TOKEN_NAME, current_user
from .tokens import create_token, revoke_token

router = APIRouter(prefix="/api/ereader", tags=["ereader"])
Db = Annotated[Session, Depends(get_db)]


def _user(request: Request, db: Session) -> User:
    return current_user(request, db)


def _csrf(request: Request) -> None:
    supplied = request.headers.get("x-csrf-token", "")
    if not secrets.compare_digest(request.session.get("csrf", ""), supplied):
        raise HTTPException(403, "Invalid form token")


def _book(book: Book, state: ReadingState | None = None) -> dict[str, Any]:
    return {
        "id": book.id,
        "title": book.title,
        "author": book.primary_author,
        "series": book.series,
        "series_number": book.series_number,
        "description": book.description or "",
        "publication_date": book.publication_date,
        "page_count": book.page_count,
        "cover_url": f"/books/{book.id}/cover" if book.cover_path else None,
        "files": [
            {
                "id": item.id,
                "format": item.format,
                "size_bytes": item.size_bytes,
                "download_url": f"/books/{book.id}/file/{item.id}",
            }
            for item in book.files
        ],
        "reading": None
        if state is None
        else {
            "state": state.state,
            "rating": state.rating,
            "favourite": state.favourite,
            "progress_percent": state.progress_percent,
        },
    }


def _external(item: Any) -> dict[str, Any]:
    if isinstance(item, dict):
        return item
    try:
        authors = json.loads(item.authors_json or "[]")
    except (TypeError, ValueError):
        authors = []
    return {
        "source": item.provider,
        "source_id": item.source_id,
        "title": item.title,
        "author": authors[0] if authors else "",
        "authors": authors,
        "cover_url": item.cover_url,
        "published": item.publication_date,
    }


def _accessible_shelves(db: Session, user: User) -> list[Shelf]:
    return list(db.scalars(select(Shelf).where(
        or_(Shelf.owner_id == user.id, Shelf.shared.is_(True))
    ).order_by(Shelf.shared.desc(), func.lower(Shelf.name))))


def _book_query(user: User, *, view: str, q: str, author: str, series: str, metadata: str):
    query = select(Book).where(Book.review_state == ReviewState.READY)
    if view in {"reading", "favourites", "rated"}:
        query = query.join(ReadingState, ReadingState.book_id == Book.id).where(
            ReadingState.user_id == user.id
        )
        if view == "reading":
            query = query.where(ReadingState.state == "reading")
        elif view == "favourites":
            query = query.where(ReadingState.favourite.is_(True))
        else:
            query = query.where(ReadingState.rating.is_not(None))
    if q.strip():
        term = f"%{q.strip()}%"
        query = query.where(or_(Book.title.ilike(term), Book.primary_author.ilike(term),
                                Book.series.ilike(term)))
    elif author:
        query = query.where(func.lower(Book.primary_author) == author.casefold())
    elif series:
        query = query.where(func.lower(Book.series) == series.casefold())
    if metadata == "missing":
        query = query.where(or_(Book.cover_path.is_(None), Book.cover_path == "",
                                Book.description.is_(None), Book.description == ""))
    return query


@router.get("/library")
def library(request: Request, db: Db, view: str = "latest", author: str = "", series: str = "",
            q: str = "", sort: str = "", direction: str = "", metadata: str = "",
            page: int = 1, page_size: int = 40):
    user = _user(request, db)
    page, page_size = max(page, 1), min(max(page_size, 1), 100)
    query = _book_query(user, view=view, q=q, author=author, series=series, metadata=metadata)
    columns = {
        "title": func.lower(Book.sort_title), "author": func.lower(Book.primary_author),
        "release_date": Book.publication_date, "series": func.lower(Book.series),
        "added": Book.created_at,
    }
    sort = sort if sort in columns else ("added" if view == "latest" or q else "title")
    direction = direction if direction in {"asc", "desc"} else (
        "desc" if sort in {"added", "release_date"} else "asc"
    )
    order = columns[sort].desc() if direction == "desc" else columns[sort].asc()
    total = db.scalar(select(func.count()).select_from(query.subquery())) or 0
    books = list(db.scalars(query.order_by(order, func.lower(Book.title), Book.id)
                            .offset((page - 1) * page_size).limit(page_size)))
    states = {item.book_id: item for item in db.scalars(select(ReadingState).where(
        ReadingState.user_id == user.id, ReadingState.book_id.in_([book.id for book in books])
    ))} if books else {}
    return {"items": [_book(book, states.get(book.id)) for book in books], "page": page,
            "page_size": page_size, "total": total, "has_more": page * page_size < total,
            "sort": sort, "direction": direction}


@router.get("/library/authors")
def authors(request: Request, db: Db):
    _user(request, db)
    rows = db.execute(select(Book.primary_author, func.count(Book.id)).where(
        Book.review_state == ReviewState.READY
    ).group_by(Book.primary_author).order_by(func.lower(Book.primary_author))).all()
    return {"items": [{"name": name, "count": count} for name, count in rows]}


@router.get("/library/series")
def series(request: Request, db: Db):
    _user(request, db)
    rows = db.execute(select(Book.series, func.count(Book.id)).where(
        Book.review_state == ReviewState.READY, Book.series.is_not(None), Book.series != ""
    ).group_by(Book.series).order_by(func.lower(Book.series))).all()
    return {"items": [{"name": name, "count": count} for name, count in rows]}


@router.get("/books/{book_id}")
def book_detail(book_id: str, request: Request, db: Db, navigation: str = ""):
    user = _user(request, db)
    book = db.get(Book, book_id)
    if not book or book.review_state != ReviewState.READY:
        raise HTTPException(404, "Book not found")
    state = db.scalar(select(ReadingState).where(ReadingState.user_id == user.id,
                                                 ReadingState.book_id == book.id))
    shelf_ids = list(db.scalars(select(ShelfBook.shelf_id).where(ShelfBook.book_id == book.id)))
    ordered = list(db.scalars(_book_query(user, view="all", q=navigation, author="", series="",
                                          metadata="").with_only_columns(Book.id)
                              .order_by(func.lower(Book.title), Book.id)))
    pos = ordered.index(book.id) if book.id in ordered else -1
    data = _book(book, state)
    data.update({"previous_id": ordered[pos - 1] if pos > 0 else None,
                 "next_id": ordered[pos + 1] if 0 <= pos < len(ordered) - 1 else None,
                 "shelf_ids": shelf_ids,
                 "shelves": [{"id": item.id, "name": item.name}
                             for item in _accessible_shelves(db, user)]})
    return data


@router.put("/books/{book_id}/reading-state")
async def set_reading_state(book_id: str, request: Request, db: Db):
    user = _user(request, db)
    _csrf(request)
    book = db.get(Book, book_id)
    if not book:
        raise HTTPException(404, "Book not found")
    data = await request.json()
    value = str(data.get("state", "unread"))
    rating = int(data.get("rating") or 0)
    if value not in {"unread", "reading", "finished", "abandoned", "want-to-read"}:
        raise HTTPException(400, "Unknown reading state")
    if rating not in range(6):
        raise HTTPException(400, "Rating must be between 0 and 5")
    state = db.scalar(select(ReadingState).where(
        ReadingState.user_id == user.id, ReadingState.book_id == book.id
    ))
    if state is None:
        state = ReadingState(user_id=user.id, book_id=book.id)
        db.add(state)
    state.state = value
    state.rating = rating or None
    state.favourite = bool(data.get("favourite", False))
    db.commit()
    return _book(book, state)["reading"]


def _discovery_group(request: Request, db: Session, name: str):
    user = _user(request, db)
    result = build_discovery(db, user.id)
    return {"items": [_book(item) for item in getattr(result, name)]}


@router.get("/discover/for-you")
def for_you(request: Request, db: Db): return _discovery_group(request, db, "recommended")


@router.get("/discover/trending")
def trending(request: Request, db: Db, period: str = "now", genre: str = ""):
    _user(request, db)
    config = settings_map(db)
    if config.get("hardcover_api_key"):
        _, days = HARDCOVER_TRENDING_PERIODS.get(period, HARDCOVER_TRENDING_PERIODS["now"])
        return {"items": hardcover_books(config["hardcover_api_key"], days=days, genre=genre)}
    return {"items": [_external(item) for item in build_discovery(db, _user(request, db).id).trending]}


@router.get("/discover/new-releases")
def new_releases(request: Request, db: Db): return _discovery_group(request, db, "new_releases")


@router.get("/discover/genre")
def genre(request: Request, db: Db, genre: str = "fantasy"):
    user = _user(request, db)
    key = genre if genre in GENRES else "fantasy"
    result = build_discovery(db, user.id, genre=key)
    return {"genre": key, "genres": GENRES, "items": [_external(item) for item in result.genre_items]}


@router.get("/discover/search")
def discover_search(request: Request, db: Db, q: str = ""):
    _user(request, db); config = settings_map(db)
    return {"items": search_discovery_books(q, hardcover_api_key=config.get("hardcover_api_key", ""),
                                               language=config.get("default_language", "en"))}


@router.get("/discover/bestsellers/lists")
def bestseller_lists(request: Request, db: Db):
    _user(request, db); key = settings_map(db).get("nytimes_api_key", "")
    items = nyt_weekly_lists(key) if key else []
    if key and not items:
        items = [{"slug": slug, "title": title} for slug, title in NYT_FALLBACK_LISTS.items()]
    return {"items": items, "configured": bool(key)}


@router.get("/discover/bestsellers/weeks")
def bestseller_weeks(request: Request, db: Db, slug: str):
    _user(request, db); key = settings_map(db).get("nytimes_api_key", "")
    lists = nyt_weekly_lists(key) if key else []
    if key and not lists:
        lists = [{"slug": slug, "title": title} for slug, title in NYT_FALLBACK_LISTS.items()]
    item = next((value for value in lists if value["slug"] == slug), None)
    if not item: raise HTTPException(404, "Bestseller list not found")
    return {"items": nyt_weeks(item)}


@router.get("/discover/bestsellers")
def bestsellers(request: Request, db: Db, slug: str, week: str = "current"):
    _user(request, db); key = settings_map(db).get("nytimes_api_key", "")
    if not key: return {"items": [], "configured": False}
    return {"items": nyt_bestsellers(key, slug, week), "configured": True}


@router.get("/shelves")
def shelves(request: Request, db: Db):
    user = _user(request, db); items = _accessible_shelves(db, user)
    return {"items": [{"id": shelf.id, "name": shelf.name, "shared": shelf.shared,
                        "count": db.scalar(select(func.count(ShelfBook.id)).where(
                            ShelfBook.shelf_id == shelf.id)) or 0} for shelf in items]}


@router.post("/shelves")
async def create_shelf(request: Request, db: Db):
    user = _user(request, db)
    _csrf(request)
    data = await request.json()
    name = str(data.get("name", "")).strip()
    if not name:
        raise HTTPException(400, "Shelf name is required")
    shared = bool(data.get("shared", False)) and user.role == Role.ADMIN
    duplicate = db.scalar(select(Shelf.id).where(
        func.lower(Shelf.name) == name.casefold(), Shelf.shared.is_(shared),
        Shelf.owner_id == (None if shared else user.id)
    ))
    if duplicate is not None:
        raise HTTPException(409, "A shelf with that name already exists")
    found = Shelf(name=name, owner_id=None if shared else user.id, shared=shared)
    db.add(found)
    db.commit()
    return {"id": found.id, "name": found.name, "shared": found.shared, "count": 0}


@router.get("/shelves/{shelf_id}")
def shelf(shelf_id: int, request: Request, db: Db, page: int = 1, page_size: int = 40):
    user = _user(request, db); found = db.get(Shelf, shelf_id)
    if not found or (not found.shared and found.owner_id != user.id): raise HTTPException(404)
    query = select(Book).join(ShelfBook).where(ShelfBook.shelf_id == shelf_id,
                                                Book.review_state == ReviewState.READY)
    total = db.scalar(select(func.count()).select_from(query.subquery())) or 0
    books = db.scalars(query.order_by(func.lower(Book.title)).offset((max(page, 1)-1)*page_size)
                       .limit(min(page_size, 100))).all()
    return {"shelf": {"id": found.id, "name": found.name, "shared": found.shared},
            "items": [_book(item) for item in books], "page": max(page, 1), "total": total}


@router.post("/shelves/{shelf_id}/books/{book_id}")
def add_to_shelf(shelf_id: int, book_id: str, request: Request, db: Db):
    user = _user(request, db); _csrf(request); found = db.get(Shelf, shelf_id)
    if not found or (not found.shared and found.owner_id != user.id) or not db.get(Book, book_id):
        raise HTTPException(404)
    exists = db.scalar(select(ShelfBook.id).where(ShelfBook.shelf_id == shelf_id,
                                                   ShelfBook.book_id == book_id))
    if exists is None: db.add(ShelfBook(shelf_id=shelf_id, book_id=book_id)); db.commit()
    return {"ok": True}


@router.delete("/shelves/{shelf_id}/books/{book_id}")
def remove_from_shelf(shelf_id: int, book_id: str, request: Request, db: Db):
    user = _user(request, db); _csrf(request); found = db.get(Shelf, shelf_id)
    if not found or (not found.shared and found.owner_id != user.id): raise HTTPException(404)
    member = db.scalar(select(ShelfBook).where(ShelfBook.shelf_id == shelf_id,
                                                ShelfBook.book_id == book_id))
    if member: db.delete(member); db.commit()
    return {"ok": True}


@router.delete("/shelves/{shelf_id}")
def remove_shelf(shelf_id: int, request: Request, db: Db):
    user = _user(request, db)
    _csrf(request)
    found = db.get(Shelf, shelf_id)
    if not found or not (found.owner_id == user.id or user.role == Role.ADMIN):
        raise HTTPException(404)
    db.execute(ShelfBook.__table__.delete().where(ShelfBook.shelf_id == found.id))
    db.delete(found)
    db.commit()
    return {"ok": True}


def _wanted(item: WantedItem, releases: list[AcquisitionRelease] | None = None):
    return {"id": item.id, "title": item.title, "author": item.author,
            "cover_url": item.cover_url, "status": item.status.value, "attempts": item.attempts,
            "last_error": item.last_error, "acquired_book_id": item.acquired_book_id,
            "releases": [{"id": release.id, "title": release.title, "format": release.format,
                           "size_bytes": release.size_bytes, "score": release.match_score}
                          for release in releases or []]}


@router.get("/downloads")
def downloads(request: Request, db: Db):
    user = _user(request, db); items = db.scalars(select(WantedItem).where(
        WantedItem.user_id == user.id, WantedItem.status != WantedStatus.CANCELLED
    ).order_by(WantedItem.created_at.desc())).all()
    return {"items": [_wanted(item, list(db.scalars(select(AcquisitionRelease).where(
        AcquisitionRelease.wanted_id == item.id).order_by(AcquisitionRelease.match_score.desc()))))
                      for item in items]}


@router.post("/downloads")
async def queue_download(request: Request, db: Db):
    user = _user(request, db); _csrf(request); data = await request.json()
    try:
        item = create_wanted(db, user_id=user.id, source=str(data.get("source", "openlibrary")),
            source_id=str(data.get("source_id", "")), title=str(data["title"]),
            author=str(data.get("author", "")), isbn=str(data.get("isbn", "")),
            cover_url=str(data.get("cover_url", "")))
    except (KeyError, ValueError) as exc: raise HTTPException(400, str(exc)) from exc
    return _wanted(item)


def _owned_wanted(db: Session, user: User, wanted_id: int) -> WantedItem:
    item = db.get(WantedItem, wanted_id)
    if not item or item.user_id != user.id: raise HTTPException(404)
    return item


@router.post("/downloads/{wanted_id}/{action}")
def download_action(wanted_id: int, action: str, request: Request, db: Db):
    user = _user(request, db); _csrf(request); item = _owned_wanted(db, user, wanted_id)
    try:
        if action == "retry": retry_acquisition(db, item)
        elif action == "cancel": cancel_acquisition(db, item)
        elif action == "remove" and item.status in {WantedStatus.AVAILABLE, WantedStatus.FAILED}:
            db.delete(item); db.commit(); return {"ok": True}
        else: raise HTTPException(409, "Action is not valid for this download")
    except (httpx.HTTPError, ValueError) as exc: raise HTTPException(409, str(exc)) from exc
    return _wanted(item)


@router.post("/downloads/{wanted_id}/releases/{release_id}")
def choose_release(wanted_id: int, release_id: int, request: Request, db: Db):
    user = _user(request, db); _csrf(request); item = _owned_wanted(db, user, wanted_id)
    release = db.get(AcquisitionRelease, release_id)
    if not release or release.wanted_id != item.id: raise HTTPException(404)
    try: queue_release(db, item, release)
    except ValueError as exc: raise HTTPException(409, str(exc)) from exc
    return _wanted(item)


@router.get("/settings")
def profile(request: Request, db: Db):
    user = _user(request, db)
    return {"kindle_email": user.kindle_email or "", "kobo_sync_shelf_id": user.kobo_sync_shelf_id,
            "kobo_sync_all_books": user.kobo_sync_all_books,
            "kobo_configured": active_kobo_token(db, user) is not None,
            "shelves": [{"id": item.id, "name": item.name} for item in _accessible_shelves(db, user)]}


@router.put("/settings")
async def update_profile(request: Request, db: Db):
    user = _user(request, db); _csrf(request); data = await request.json()
    user.kindle_email = str(data.get("kindle_email", "")).strip() or None
    value = data.get("kobo_sync_shelf_id")
    user.kobo_sync_all_books = value == "all"
    user.kobo_sync_shelf_id = None if value in {None, "", "all"} else int(value)
    if user.kobo_sync_shelf_id is not None and user.kobo_sync_shelf_id not in {
        item.id for item in _accessible_shelves(db, user)
    }:
        raise HTTPException(400, "Invalid Kobo sync shelf")
    db.commit(); return profile(request, db)


@router.post("/settings/kobo-token")
def issue_kobo_token(request: Request, db: Db):
    user = _user(request, db); _csrf(request); current = active_kobo_token(db, user)
    if current: revoke_token(db, user, current)
    db.execute(KoboSyncedBook.__table__.delete().where(KoboSyncedBook.user_id == user.id))
    db.execute(KoboSyncedShelf.__table__.delete().where(KoboSyncedShelf.user_id == user.id)); db.commit()
    _, token = create_token(db, user, user, KOBO_TOKEN_NAME)
    return {"endpoint": f"{get_settings().public_url.rstrip('/')}/kobo/{token}"}


@router.delete("/settings/kobo-token")
def delete_kobo_token(request: Request, db: Db):
    user = _user(request, db); _csrf(request); current = active_kobo_token(db, user)
    if current: revoke_token(db, user, current)
    return {"ok": True}
