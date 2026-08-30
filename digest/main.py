import json
import mimetypes
import re
import secrets
import smtplib
from email.message import EmailMessage
from pathlib import Path
from typing import Annotated
from urllib.parse import parse_qs, urlencode, urlsplit
from xml.etree.ElementTree import Element, SubElement, tostring

import httpx
from fastapi import Body, Depends, FastAPI, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, RedirectResponse, Response
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from sqlalchemy import case, func, or_, select
from sqlalchemy.orm import Session
from starlette.middleware.sessions import SessionMiddleware

from .accounts import AccountError, create_account, reset_password, update_account
from .acquisition import cancel_acquisition, create_wanted, find_wanted, queue_release, retry_acquisition
from .admin_settings import PROVIDERS, SettingsError, save_admin_settings
from .config import get_settings
from .db import get_db, initialise_database
from .discovery import (
    GENRES,
    HARDCOVER_FALLBACK_GENRES,
    HARDCOVER_TRENDING_PERIODS,
    NYT_FALLBACK_LISTS,
    author_bibliography,
    build_discovery,
    find_library_book,
    hardcover_books,
    hardcover_genres,
    nyt_bestsellers,
    nyt_weekly_lists,
    nyt_weeks,
    search_discovery_books,
)
from .jobs import enqueue
from .kobo import (
    active_kobo_token,
    add_tag_items,
    archive_from_device,
    create_tag,
    delete_tag,
    dummy_auth,
    get_reading_state,
    kobo_user,
    preferred_file,
    reading_state_payload,
    remove_tag_items,
    shelf_for_tag,
    shelf_tag_id,
    sync_payload,
    update_tag,
)
from .kobo import (
    initialization as kobo_initialization,
)
from .kobo import (
    metadata as kobo_metadata,
)
from .kobo import (
    shelf_book as kobo_shelf_book,
)
from .kobo import (
    update_reading_state as update_kobo_reading_state,
)
from .library import delete_book, organise_book, scan_library
from .metadata import (
    apply_candidate,
    apply_manual_metadata,
    find_candidates,
    normalise_uploaded_cover,
    save_uploaded_cover,
    settings_map,
)
from .models import (
    AcquisitionRelease,
    ApiToken,
    AuditEvent,
    Book,
    BookFile,
    Job,
    JobStatus,
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
from .security import KOBO_TOKEN_NAME, current_user, hash_password, setup_required, verify_password
from .text import plain_text
from .tokens import TokenError, create_token, revoke_token

settings = get_settings()
app = FastAPI(title="Digest", version="0.1.0")
app.add_middleware(
    SessionMiddleware,
    secret_key=settings.secret_key,
    max_age=settings.session_days * 86400,
    same_site="lax",
    https_only=settings.public_url.startswith("https://"),
)
base = Path(__file__).parent
app.mount("/static", StaticFiles(directory=base / "static"), name="static")
templates = Jinja2Templates(directory=base / "templates")
templates.env.filters["fromjson"] = json.loads
templates.env.filters["plaintext"] = lambda value: plain_text(value) or ""
templates.env.filters["coverversion"] = (
    lambda value: str(int(value.timestamp() * 1_000_000)) if value else "0"
)


def discovery_book_url(item, return_to: str = "/discover") -> str:
    if isinstance(item, dict):
        values = item
        authors = values.get("authors") or []
        source = values.get("source") or ""
        publication = values.get("published_year") or ""
        genres = values.get("genres") or []
    else:
        try:
            authors = json.loads(item.authors_json or "[]")
        except (TypeError, ValueError):
            authors = []
        values = {
            "source_id": item.source_id,
            "title": item.title,
            "cover_url": item.cover_url,
        }
        source = item.provider
        publication = item.publication_date or ""
        genres = []
    query = {
        "source": source,
        "source_id": values.get("source_id") or "",
        "title": values.get("title") or "",
        "author": values.get("author") or (authors[0] if authors else ""),
        "isbn": values.get("isbn") or "",
        "cover_url": values.get("cover_url") or "",
        "description": str(values.get("description") or "")[:4000],
        "published": publication,
        "genres": ",".join(str(value) for value in genres),
        "return_to": return_to,
    }
    return f"/discover/book?{urlencode(query)}"


templates.env.globals["discovery_book_url"] = discovery_book_url


@app.on_event("startup")
def startup() -> None:
    settings.data_root.mkdir(parents=True, exist_ok=True)
    initialise_database()


def user_or_none(request: Request, db: Session) -> User | None:
    user_id = request.session.get("user_id")
    user = db.get(User, user_id) if user_id else None
    return user if user and user.is_active else None


def require_user(request: Request, db: Session) -> User:
    return current_user(request, db)


def require_admin(request: Request, db: Session) -> User:
    user = require_user(request, db)
    if user.role != Role.ADMIN:
        raise HTTPException(status_code=403, detail="Administrator access required")
    return user


def csrf(request: Request) -> str:
    if "csrf" not in request.session:
        request.session["csrf"] = secrets.token_urlsafe(24)
    return request.session["csrf"]


def check_csrf(request: Request, value: str) -> None:
    if not secrets.compare_digest(request.session.get("csrf", ""), value):
        raise HTTPException(status_code=403, detail="Invalid form token")


def safe_return_to(value: str | None, fallback: str = "/") -> str:
    value = (value or "").strip()
    return value if value.startswith("/") and not value.startswith("//") else fallback


SORT_KEYS = {"title", "author", "release_date", "series", "added"}


def default_sort_direction(sort: str) -> str:
    return "desc" if sort in {"release_date", "added"} else "asc"


def book_order(sort: str, direction: str | None = None):
    direction = direction if direction in {"asc", "desc"} else default_sort_direction(sort)

    def ordered(value):
        return (value.desc() if direction == "desc" else value.asc()).nullslast()

    title = ordered(func.lower(Book.title))
    orders = {
        "title": (ordered(func.lower(Book.sort_title)), title),
        "author": (ordered(func.lower(Book.primary_author)), title),
        "release_date": (ordered(Book.publication_date), title),
        "series": (ordered(Book.series), ordered(Book.series_number), title),
        "added": (ordered(Book.created_at), title),
    }
    return orders.get(sort, orders["added"])


def sort_controls(selected_sort: str, direction: str) -> list[tuple[str, str, str]]:
    labels = {
        "title": "Title",
        "author": "Author",
        "release_date": "Release date",
        "series": "Series",
        "added": "Added date",
    }
    return [
        (
            key,
            label,
            ("desc" if direction == "asc" else "asc")
            if key == selected_sort
            else default_sort_direction(key),
        )
        for key, label in labels.items()
    ]


def listing_book_query(db: Session, listing_url: str, user_id: int | None = None):
    parsed = urlsplit(safe_return_to(listing_url))
    params = parse_qs(parsed.query)
    query = select(Book).where(Book.review_state == ReviewState.READY)
    if parsed.path == "/discover" and user_id is not None:
        discovery = build_discovery(db, user_id)
        groups = {
            "recommended": discovery.recommended,
            "series": discovery.continue_series,
            "popular": discovery.popular,
            "new": discovery.new_releases,
        }
        ordered_ids = [book.id for book in groups.get(params.get("view", [""])[0], [])]
        if not ordered_ids:
            return query.where(Book.id.is_(None))
        position = case({book_id: index for index, book_id in enumerate(ordered_ids)}, value=Book.id)
        return query.where(Book.id.in_(ordered_ids)).order_by(position)
    if parsed.path.startswith("/shelves/") and user_id is not None:
        try:
            shelf_id = int(parsed.path.removeprefix("/shelves/").split("/", 1)[0])
        except ValueError:
            shelf_id = 0
        query = (
            query.join(ShelfBook, ShelfBook.book_id == Book.id)
            .join(Shelf, Shelf.id == ShelfBook.shelf_id)
            .where(Shelf.id == shelf_id, or_(Shelf.shared.is_(True), Shelf.owner_id == user_id))
        )
    q = params.get("q", [""])[0]
    author = params.get("author", [""])[0]
    series = params.get("series", [""])[0]
    view = params.get("view", [""])[0]
    sort = params.get("sort", [""])[0]
    direction = params.get("direction", [""])[0]
    metadata = params.get("metadata", [""])[0]
    if user_id is not None and view in {"reading", "favourites", "rated"}:
        query = query.join(ReadingState, ReadingState.book_id == Book.id).where(
            ReadingState.user_id == user_id
        )
        if view == "reading":
            query = query.where(ReadingState.state == "reading")
        elif view == "favourites":
            query = query.where(ReadingState.favourite.is_(True))
        else:
            query = query.where(ReadingState.rating.is_not(None))
    if q:
        term = f"%{q}%"
        query = query.where(
            or_(Book.title.ilike(term), Book.primary_author.ilike(term), Book.series.ilike(term))
        )
    elif author:
        query = query.where(func.lower(Book.primary_author) == author.casefold())
    elif series:
        query = query.where(func.lower(Book.series) == series.casefold())
    if metadata == "missing":
        query = query.where(
            or_(
                Book.cover_path.is_(None),
                Book.cover_path == "",
                Book.description.is_(None),
                Book.description == "",
            )
        )
    if sort not in SORT_KEYS:
        sort = "added" if view == "latest" or q else "title"
    if direction not in {"asc", "desc"}:
        direction = default_sort_direction(sort)
    return query.order_by(*book_order(sort, direction), Book.id)


def book_url(book_id: str, return_to: str, navigation: str) -> str:
    return f"/books/{book_id}?{urlencode({'return_to': return_to, 'navigation': navigation})}"


def metadata_suggestions(db: Session) -> dict[str, list[str]]:
    author_values = db.scalars(
        select(Book.primary_author)
        .where(Book.primary_author != "")
        .order_by(func.lower(Book.primary_author))
    )
    series_values = db.scalars(
        select(Book.series)
        .where(Book.series.is_not(None), Book.series != "")
        .order_by(func.lower(Book.series))
    )

    def unique(values) -> list[str]:
        found: dict[str, str] = {}
        for value in values:
            if value:
                found.setdefault(value.casefold(), value)
        return list(found.values())

    return {
        "author_suggestions": unique(author_values),
        "series_suggestions": unique(series_values),
    }


def accessible_shelves(db: Session, user: User) -> list[Shelf]:
    return list(
        db.scalars(
            select(Shelf)
            .where(or_(Shelf.owner_id == user.id, Shelf.shared.is_(True)))
            .order_by(Shelf.shared.desc(), func.lower(Shelf.name))
        )
    )


def render(request: Request, name: str, context: dict, user: User | None = None) -> HTMLResponse:
    user_agent = request.headers.get("user-agent", "").casefold()
    family = "ereader" if "kindle" in user_agent or "kobo" in user_agent else "modern"
    template = f"{family}/{name}"
    # The modern templates are also the safe fallback while an e-reader-specific
    # version of a screen is not present.
    if not (base / "templates" / template).is_file():
        template = f"modern/{name}"
    return templates.TemplateResponse(
        request, template, {**context, "user": user, "csrf": csrf(request)}
    )


@app.get("/healthz")
def health() -> dict:
    return {"status": "ok"}


@app.get("/api/navigation")
def navigation_data(request: Request, db: Annotated[Session, Depends(get_db)]):
    user = require_user(request, db)
    ready = Book.review_state == ReviewState.READY
    counts = {
        "books": db.scalar(select(func.count(Book.id)).where(ready)) or 0,
        "authors": db.scalar(
            select(func.count(func.distinct(Book.primary_author))).where(ready)
        )
        or 0,
        "series": db.scalar(
            select(func.count(func.distinct(Book.series))).where(
                ready, Book.series.is_not(None), Book.series != ""
            )
        )
        or 0,
        "reading": db.scalar(
            select(func.count(ReadingState.id))
            .join(Book, Book.id == ReadingState.book_id)
            .where(ready, ReadingState.user_id == user.id, ReadingState.state == "reading")
        )
        or 0,
        "favourites": db.scalar(
            select(func.count(ReadingState.id))
            .join(Book, Book.id == ReadingState.book_id)
            .where(ready, ReadingState.user_id == user.id, ReadingState.favourite.is_(True))
        )
        or 0,
        "review": db.scalar(
            select(func.count(Book.id)).where(Book.review_state != ReviewState.READY)
        )
        or 0,
        "downloads": db.scalar(
            select(func.count(func.distinct(WantedItem.id)))
            .join(AcquisitionRelease, AcquisitionRelease.wanted_id == WantedItem.id)
            .where(
                WantedItem.user_id == user.id,
                WantedItem.status == WantedStatus.WANTED,
                WantedItem.selected_release_id.is_(None),
                AcquisitionRelease.match_score > 0.8,
            )
        )
        or 0,
    }
    shelves = [{"id": "all", "name": "All Books"}] + [
        {"id": item.id, "name": item.name}
        for item in accessible_shelves(db, user)[:8]
    ]
    return {"counts": counts, "shelves": shelves}


@app.get("/setup", response_class=HTMLResponse)
def setup_page(request: Request, db: Annotated[Session, Depends(get_db)]):
    if not setup_required(db):
        return RedirectResponse("/login", 303)
    return render(request, "setup.html", {})


@app.post("/setup")
def setup_submit(
    request: Request,
    db: Annotated[Session, Depends(get_db)],
    username: Annotated[str, Form()],
    password: Annotated[str, Form()],
    confirm: Annotated[str, Form()],
    form_csrf: Annotated[str, Form()],
):
    if not setup_required(db):
        raise HTTPException(409, "Setup already completed")
    check_csrf(request, form_csrf)
    if len(username.strip()) < 3 or len(password) < 12 or password != confirm:
        return render(
            request,
            "setup.html",
            {
                "error": "Use a username of at least 3 characters and matching password of at least 12 characters."
            },
        )
    user = User(username=username.strip(), password_hash=hash_password(password), role=Role.ADMIN)
    db.add(user)
    db.flush()
    db.add(AuditEvent(event="login", user_id=user.id, message="Initial administrator created"))
    db.commit()
    request.session["user_id"] = user.id
    return RedirectResponse("/", 303)


@app.get("/login", response_class=HTMLResponse)
def login_page(request: Request, db: Annotated[Session, Depends(get_db)]):
    if setup_required(db):
        return RedirectResponse("/setup", 303)
    return render(request, "login.html", {})


@app.post("/login")
def login(
    request: Request,
    db: Annotated[Session, Depends(get_db)],
    username: Annotated[str, Form()],
    password: Annotated[str, Form()],
    form_csrf: Annotated[str, Form()],
):
    check_csrf(request, form_csrf)
    user = db.scalar(select(User).where(func.lower(User.username) == username.strip().lower()))
    if not user or not user.is_active or not verify_password(password, user.password_hash):
        db.add(
            AuditEvent(level="warning", event="login", message=f"Failed login for {username[:80]}")
        )
        db.commit()
        return render(request, "login.html", {"error": "Invalid username or password."})
    request.session.clear()
    request.session["user_id"] = user.id
    db.add(AuditEvent(event="login", user_id=user.id, message="Login successful"))
    db.commit()
    return RedirectResponse("/", 303)


@app.post("/logout")
def logout(
    request: Request, db: Annotated[Session, Depends(get_db)], form_csrf: Annotated[str, Form()]
):
    user = require_user(request, db)
    check_csrf(request, form_csrf)
    db.add(AuditEvent(event="login", user_id=user.id, message="Logout"))
    db.commit()
    request.session.clear()
    return RedirectResponse("/login", 303)


@app.get("/", response_class=HTMLResponse)
def library(
    request: Request,
    db: Annotated[Session, Depends(get_db)],
    q: str = "",
    author: str = "",
    series: str = "",
    view: str = "",
    sort: str = "",
    direction: str = "",
    metadata: str = "",
    state: str = "",
    shelf: str = "",
    file_format: str = "",
    page: int = 1,
):
    user = user_or_none(request, db)
    if not user:
        return RedirectResponse("/setup" if setup_required(db) else "/login", 303)
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
    if q:
        term = f"%{q}%"
        query = query.where(
            or_(Book.title.ilike(term), Book.primary_author.ilike(term), Book.series.ilike(term))
        )
    elif author:
        query = query.where(func.lower(Book.primary_author) == author.casefold())
    elif series:
        query = query.where(func.lower(Book.series) == series.casefold())
    if metadata == "missing":
        query = query.where(
            or_(
                Book.cover_path.is_(None),
                Book.cover_path == "",
                Book.description.is_(None),
                Book.description == "",
            )
        )
    if state in {"unread", "reading", "finished", "abandoned", "want-to-read"}:
        if view not in {"reading", "favourites", "rated"}:
            query = query.join(ReadingState, ReadingState.book_id == Book.id).where(
                ReadingState.user_id == user.id
            )
        query = query.where(ReadingState.state == state)
    shelf_id = int(shelf) if shelf.isdigit() else None
    if shelf_id is not None:
        query = query.join(ShelfBook, ShelfBook.book_id == Book.id).where(
            ShelfBook.shelf_id == shelf_id
        )
    if file_format:
        query = query.where(
            Book.files.any(func.lower(BookFile.format) == file_format.casefold())
        )
    page = max(page, 1)
    directory_view = view if view in {"authors", "series"} else ""
    filtered = bool(
        q
        or author
        or series
        or metadata == "missing"
        or state
        or shelf_id is not None
        or file_format
        or view in {"latest", "all", "reading", "favourites", "rated"}
    )
    selected_sort = sort if sort in SORT_KEYS else ""
    if not selected_sort:
        selected_sort = "added" if view == "latest" or q else "title"
    selected_direction = (
        direction if direction in {"asc", "desc"} else default_sort_direction(selected_sort)
    )
    if filtered:
        result_count = db.scalar(select(func.count()).select_from(query.subquery())) or 0
        results = db.scalars(
            query.order_by(*book_order(selected_sort, selected_direction))
            .offset((page - 1) * 24)
            .limit(25)
        ).all()
        books = results[:24]
        has_next = len(results) > 24
    else:
        result_count = 0
        books = db.scalars(query.order_by(Book.created_at.desc()).limit(12)).all()
        has_next = False
    all_books = (
        db.scalars(query.order_by(*book_order("title")).limit(12)).all() if not filtered else []
    )
    reading_books = (
        db.scalars(
            select(Book)
            .join(ReadingState, ReadingState.book_id == Book.id)
            .where(
                Book.review_state == ReviewState.READY,
                ReadingState.user_id == user.id,
                ReadingState.state == "reading",
            )
            .order_by(Book.updated_at.desc())
            .limit(12)
        ).all()
        if not filtered
        else []
    )
    favourite_books = (
        db.scalars(
            select(Book)
            .join(ReadingState, ReadingState.book_id == Book.id)
            .where(
                Book.review_state == ReviewState.READY,
                ReadingState.user_id == user.id,
                ReadingState.favourite.is_(True),
            )
            .order_by(func.lower(Book.title))
            .limit(12)
        ).all()
        if not filtered
        else []
    )
    rated_books = (
        db.scalars(
            select(Book)
            .join(ReadingState, ReadingState.book_id == Book.id)
            .where(
                Book.review_state == ReviewState.READY,
                ReadingState.user_id == user.id,
                ReadingState.rating.is_not(None),
            )
            .order_by(ReadingState.rating.desc(), func.lower(Book.title))
            .limit(12)
        ).all()
        if not filtered
        else []
    )
    author_query = (
        select(Book.primary_author, func.count(Book.id))
        .where(Book.review_state == ReviewState.READY)
        .group_by(Book.primary_author)
        .order_by(func.lower(Book.primary_author))
    )
    series_query = (
        select(Book.series, func.count(Book.id))
        .where(
            Book.review_state == ReviewState.READY,
            Book.series.is_not(None),
            Book.series != "",
        )
        .group_by(Book.series)
        .order_by(func.lower(Book.series))
    )
    if directory_view == "authors":
        author_results = db.execute(author_query.offset((page - 1) * 48).limit(49)).all()
        authors = author_results[:48]
        series_rows = []
        has_next = len(author_results) > 48
    elif directory_view == "series":
        series_results = db.execute(series_query.offset((page - 1) * 48).limit(49)).all()
        series_rows = series_results[:48]
        authors = []
        has_next = len(series_results) > 48
    else:
        authors = db.execute(author_query.limit(12)).all()
        series_rows = db.execute(series_query.limit(12)).all()
    query_string = request.url.query
    return_to = request.url.path + (f"?{query_string}" if query_string else "")
    return render(
        request,
        "library.html",
        {
            "books": books,
            "all_books": all_books,
            "reading_books": reading_books,
            "favourite_books": favourite_books,
            "rated_books": rated_books,
            "authors": authors,
            "series_rows": series_rows,
            "q": q,
            "selected_author": author,
            "selected_series": series,
            "view": view,
            "directory_view": directory_view,
            "filtered": filtered,
            "sort": selected_sort,
            "direction": selected_direction,
            "sort_controls": sort_controls(selected_sort, selected_direction),
            "metadata_filter": metadata == "missing",
            "state_filter": state,
            "shelf_filter": shelf_id,
            "format_filter": file_format,
            "filter_shelves": accessible_shelves(db, user),
            "result_count": result_count,
            "return_to": return_to,
            "filtered_navigation": return_to,
            "latest_navigation": "/?view=latest",
            "all_navigation": "/?view=all",
            "reading_navigation": "/?view=reading",
            "favourites_navigation": "/?view=favourites",
            "rated_navigation": "/?view=rated",
            "page": page,
            "has_next": has_next,
        },
        user,
    )


@app.get("/discover", response_class=HTMLResponse)
def discover(
    request: Request,
    db: Annotated[Session, Depends(get_db)],
    genre: str = "",
    mode: str = "",
    period: str = "now",
    slug: str = "",
    week: str = "current",
    q: str = "",
    hide_owned: bool = False,
):
    user = require_user(request, db)
    genre_slug = genre if genre in GENRES else next(
        (slug for slug, label in GENRES.items() if label.casefold() == genre.casefold()),
        "fantasy",
    )
    results = build_discovery(db, user.id, genre=genre_slug)
    provider_books: list[dict] = []
    provider_title = ""
    provider_error = ""
    hardcover_genre_books: list[dict] = []
    bestseller_lists: list[dict] = []
    bestseller_weeks: list[dict] = []
    selected_list = ""
    config = settings_map(db)
    q = q.strip()
    effective_mode = "search" if q else mode or (
        "hardcover-trending" if config.get("hardcover_api_key") else ""
    )
    try:
        if effective_mode == "search":
            provider_books = search_discovery_books(
                q,
                hardcover_api_key=config.get("hardcover_api_key", ""),
                language=config.get("default_language", "en"),
            )
            provider_title = f"Search results for “{q}”"
        elif effective_mode in {"hardcover-trending", "hardcover-new-releases"}:
            api_key = config.get("hardcover_api_key", "")
            if not api_key:
                raise ValueError("Hardcover discovery requires an API key in Administration.")
            if period not in HARDCOVER_TRENDING_PERIODS:
                period = "now"
            selected_genre = GENRES[genre_slug] if genre else ""
            if effective_mode == "hardcover-trending":
                label, days = HARDCOVER_TRENDING_PERIODS[period]
                provider_books = hardcover_books(api_key, days=days, genre=selected_genre)
                provider_title = f"Trending - {label}"
            else:
                provider_books = hardcover_books(
                    api_key,
                    days=120,
                    genre=selected_genre,
                    new_releases=True,
                )
                provider_title = "New Releases"
            if selected_genre:
                provider_title += f" - {selected_genre}"
        elif effective_mode in {"bestseller-lists", "bestseller-weeks", "bestsellers"}:
            api_key, bestseller_lists = configured_nyt_lists(db)
            if not api_key:
                raise ValueError("Best Sellers requires an NYT Books API key in Administration.")
            if effective_mode != "bestseller-lists":
                selected = next((item for item in bestseller_lists if item["slug"] == slug), None)
                if selected is None:
                    raise ValueError("Choose a valid bestseller list.")
                selected_list = selected["title"]
                bestseller_weeks = nyt_weeks(selected)
                if effective_mode == "bestsellers":
                    if week != "current" and not re.fullmatch(r"\d{4}-\d{2}-\d{2}", week):
                        raise ValueError("Choose a valid bestseller week.")
                    provider_books = nyt_bestsellers(api_key, slug, week)
                    provider_title = selected_list
                    if week != "current":
                        provider_title += f" - {week}"
    except (httpx.HTTPError, TypeError, ValueError) as exc:
        provider_error = str(exc) or "The discovery provider is temporarily unavailable."
    if config.get("hardcover_api_key") and effective_mode != "search":
        genre_label = GENRES[genre_slug]
        try:
            hardcover_genre_books = hardcover_books(
                config["hardcover_api_key"],
                days=None,
                genre=genre_label,
            )
        except (httpx.HTTPError, TypeError, ValueError) as exc:
            if not provider_error:
                provider_error = str(exc) or "The discovery provider is temporarily unavailable."
    available_discovery_ids: set[str] = set()
    for item in [*provider_books, *hardcover_genre_books]:
        if find_library_book(
            db, title=item["title"], author=item.get("author", ""), isbn=item.get("isbn", "")
        ):
            available_discovery_ids.add(f"{item['source']}:{item['source_id']}")
    for item in [*results.trending, *results.genre_items]:
        authors = json.loads(item.authors_json or "[]")
        if find_library_book(
            db, title=item.title, author=authors[0] if authors else "", isbn=""
        ):
            available_discovery_ids.add(f"{item.provider}:{item.source_id}")
    if hide_owned:
        provider_books = [
            item
            for item in provider_books
            if f"{item['source']}:{item['source_id']}" not in available_discovery_ids
        ]
        hardcover_genre_books = [
            item
            for item in hardcover_genre_books
            if f"{item['source']}:{item['source_id']}" not in available_discovery_ids
        ]
    return_to = request.url.path
    if request.url.query:
        return_to += f"?{request.url.query}"
    return render(
        request,
        "discover.html",
        {
            "recommended": results.recommended,
            "recommendation_reasons": results.recommendation_reasons,
            "continue_series": results.continue_series,
            "popular": results.popular,
            "new_releases": results.new_releases,
            "trending": results.trending if not config.get("hardcover_api_key") else [],
            "genre_items": (
                results.genre_items if not config.get("hardcover_api_key") else []
            ),
            "hardcover_genre_books": hardcover_genre_books,
            "genres": results.genres,
            "selected_genre": results.selected_genre,
            "external_updated_at": results.external_updated_at,
            "mode": effective_mode,
            "period": period,
            "provider_books": provider_books,
            "provider_title": provider_title,
            "provider_error": provider_error,
            "q": q,
            "hide_owned": hide_owned,
            "available_discovery_ids": available_discovery_ids,
            "bestseller_lists": bestseller_lists,
            "bestseller_weeks": bestseller_weeks,
            "selected_list": selected_list,
            "selected_slug": slug,
            "hardcover_genres": HARDCOVER_FALLBACK_GENRES,
            "return_to": return_to,
        },
        user,
    )


@app.get("/discover/book", response_class=HTMLResponse)
def discovery_book_detail(
    request: Request,
    db: Annotated[Session, Depends(get_db)],
    source: str = "",
    source_id: str = "",
    title: str = "",
    author: str = "",
    isbn: str = "",
    cover_url: str = "",
    description: str = "",
    published: str = "",
    genres: str = "",
    return_to: str = "/discover",
):
    user = require_user(request, db)
    if source not in {"hardcover", "nytimes", "openlibrary"} or not title.strip():
        raise HTTPException(400, "Invalid discovery book")
    book = find_library_book(db, title=title, author=author, isbn=isbn)
    wanted = find_wanted(
        db,
        user_id=user.id,
        source=source,
        source_id=source_id,
        title=title,
        author=author,
        isbn=isbn,
    )
    item = {
        "source": source,
        "source_id": source_id,
        "title": title.strip(),
        "author": author.strip(),
        "isbn": isbn.strip(),
        "cover_url": cover_url.strip(),
        "description": description,
        "published": published.strip(),
        "genres": [value.strip() for value in genres.split(",") if value.strip()],
    }
    detail_url = request.url.path
    if request.url.query:
        detail_url += f"?{request.url.query}"
    return render(
        request,
        "discover_book.html",
        {
            "item": item,
            "book": book,
            "wanted": wanted,
            "return_to": safe_return_to(return_to, "/discover"),
            "detail_url": detail_url,
        },
        user,
    )


@app.post("/wanted")
def request_download(
    request: Request,
    db: Annotated[Session, Depends(get_db)],
    source: Annotated[str, Form()],
    title: Annotated[str, Form()],
    form_csrf: Annotated[str, Form()],
    source_id: Annotated[str, Form()] = "",
    author: Annotated[str, Form()] = "",
    isbn: Annotated[str, Form()] = "",
    cover_url: Annotated[str, Form()] = "",
    return_to: Annotated[str, Form()] = "/discover",
):
    user = require_user(request, db)
    check_csrf(request, form_csrf)
    if source not in {"hardcover", "nytimes", "openlibrary"} or not title.strip():
        raise HTTPException(400, "Invalid wanted item")
    create_wanted(
        db,
        user_id=user.id,
        source=source,
        source_id=source_id,
        title=title,
        author=author,
        isbn=isbn,
        cover_url=cover_url,
    )
    destination = safe_return_to(return_to, "/discover")
    separator = "&" if "?" in destination else "?"
    return RedirectResponse(destination + separator + "requested=1", 303)


@app.get("/wanted", response_class=HTMLResponse)
def wanted_page(request: Request, db: Annotated[Session, Depends(get_db)]):
    user = require_user(request, db)
    items = db.scalars(
        select(WantedItem)
        .where(WantedItem.user_id == user.id, WantedItem.status != WantedStatus.CANCELLED)
        .order_by(WantedItem.created_at.desc())
    ).all()
    releases = db.scalars(
        select(AcquisitionRelease)
        .where(
            AcquisitionRelease.wanted_id.in_([item.id for item in items]),
            AcquisitionRelease.match_score > 0.8,
        )
        .order_by(AcquisitionRelease.match_score.desc(), AcquisitionRelease.id)
    ).all() if items else []
    grouped: dict[int, list[AcquisitionRelease]] = {}
    for release in releases:
        grouped.setdefault(release.wanted_id, []).append(release)
    refresh_pending = any(
        item.status in {WantedStatus.SEARCHING, WantedStatus.DOWNLOADING, WantedStatus.IMPORTING}
        or (item.status == WantedStatus.WANTED and item.attempts == 0 and not item.last_error)
        for item in items)
    return render(
        request,
        "wanted.html",
        {"items": items, "releases": grouped, "refresh_pending": refresh_pending},
        user,
    )


@app.post("/wanted/{wanted_id}/cancel")
def cancel_download_request(
    wanted_id: int,
    request: Request,
    db: Annotated[Session, Depends(get_db)],
    return_to: Annotated[str, Form()],
    form_csrf: Annotated[str, Form()],
):
    user = require_user(request, db)
    check_csrf(request, form_csrf)
    wanted = db.get(WantedItem, wanted_id)
    if wanted is None or wanted.user_id != user.id:
        raise HTTPException(404, "Wanted item not found")
    try:
        cancel_acquisition(db, wanted)
    except (httpx.HTTPError, ValueError) as exc:
        raise HTTPException(409, str(exc)) from exc
    return RedirectResponse(safe_return_to(return_to, "/discover"), 303)


@app.post("/wanted/{wanted_id}/retry")
def retry_download_request(
    wanted_id: int, request: Request, db: Annotated[Session, Depends(get_db)],
    form_csrf: Annotated[str, Form()],
):
    user = require_user(request, db)
    check_csrf(request, form_csrf)
    wanted = db.get(WantedItem, wanted_id)
    if wanted is None or wanted.user_id != user.id:
        raise HTTPException(404, "Wanted item not found")
    try:
        retry_acquisition(db, wanted)
    except ValueError as exc:
        raise HTTPException(409, str(exc)) from exc
    return RedirectResponse("/wanted?retry=ready", 303)


@app.post("/wanted/{wanted_id}/remove")
def remove_download_request(
    wanted_id: int,
    request: Request,
    db: Annotated[Session, Depends(get_db)],
    form_csrf: Annotated[str, Form()],
):
    user = require_user(request, db)
    check_csrf(request, form_csrf)
    wanted = db.get(WantedItem, wanted_id)
    if wanted is None or wanted.user_id != user.id:
        raise HTTPException(404, "Wanted item not found")
    if wanted.status not in {WantedStatus.AVAILABLE, WantedStatus.FAILED}:
        raise HTTPException(409, "Only completed or failed downloads can be removed")
    db.delete(wanted)
    db.commit()
    return RedirectResponse("/wanted?removed=1", 303)


@app.post("/wanted/{wanted_id}/releases/{release_id}")
def select_acquisition_release(
    wanted_id: int,
    release_id: int,
    request: Request,
    db: Annotated[Session, Depends(get_db)],
    form_csrf: Annotated[str, Form()],
):
    user = require_user(request, db)
    check_csrf(request, form_csrf)
    wanted = db.get(WantedItem, wanted_id)
    release = db.get(AcquisitionRelease, release_id)
    if wanted is None or wanted.user_id != user.id or release is None:
        raise HTTPException(404, "Acquisition release not found")
    try:
        queue_release(db, wanted, release)
    except ValueError as exc:
        raise HTTPException(409, str(exc)) from exc
    return RedirectResponse("/wanted?download=queued", 303)


@app.get("/discover/author", response_class=HTMLResponse)
def discovery_author(
    request: Request,
    db: Annotated[Session, Depends(get_db)],
    author: str = "",
    return_to: str = "/discover",
):
    user = require_user(request, db)
    author = author.strip()
    if not author:
        raise HTTPException(400, "Author is required")
    error = ""
    try:
        config = settings_map(db)
        books = author_bibliography(
            author,
            hardcover_api_key=config.get("hardcover_api_key", ""),
            language=config.get("default_language", "en"),
        )
    except (httpx.HTTPError, TypeError, ValueError) as exc:
        books = []
        error = str(exc) or "The discovery provider is temporarily unavailable."
    author_url = f"/discover/author?{urlencode({'author': author, 'return_to': return_to})}"
    rows = [
        {
            "item": item,
            "available": find_library_book(
                db, title=item["title"], author=item["author"], isbn=item["isbn"]
            ),
        }
        for item in books
    ]
    return render(
        request,
        "discover_author.html",
        {
            "author": author,
            "rows": rows,
            "provider_error": error,
            "return_to": safe_return_to(return_to, "/discover"),
            "author_url": author_url,
        },
        user,
    )


@app.get("/api/discovery/hardcover-genres")
def api_hardcover_genres(request: Request, db: Annotated[Session, Depends(get_db)]):
    require_user(request, db)
    api_key = settings_map(db).get("hardcover_api_key", "")
    if not api_key:
        return {"enabled": False, "genres": []}
    try:
        genres = hardcover_genres(api_key)
    except (httpx.HTTPError, TypeError, ValueError):
        genres = HARDCOVER_FALLBACK_GENRES
    return {"enabled": True, "genres": genres}


@app.get("/api/discovery/hardcover-trending")
def api_hardcover_trending(
    request: Request,
    db: Annotated[Session, Depends(get_db)],
    period: str = "now",
    genre: str = "",
):
    require_user(request, db)
    api_key = settings_map(db).get("hardcover_api_key", "")
    if not api_key:
        raise HTTPException(503, "Hardcover API key is not configured")
    if period not in HARDCOVER_TRENDING_PERIODS:
        raise HTTPException(400, "Invalid trending period")
    title, days = HARDCOVER_TRENDING_PERIODS[period]
    try:
        books = hardcover_books(api_key, days=days, genre=genre.strip())
    except (httpx.HTTPError, TypeError, ValueError) as exc:
        raise HTTPException(502, "Unable to load Hardcover trending books") from exc
    label = f"Trending - {title}"
    if genre.strip():
        label += f" - {genre.strip()}"
    return {"title": label, "books": books}


@app.get("/api/discovery/hardcover-new-releases")
def api_hardcover_new_releases(
    request: Request,
    db: Annotated[Session, Depends(get_db)],
    genre: str = "",
):
    require_user(request, db)
    api_key = settings_map(db).get("hardcover_api_key", "")
    if not api_key:
        raise HTTPException(503, "Hardcover API key is not configured")
    try:
        books = hardcover_books(
            api_key,
            days=120,
            genre=genre.strip(),
            new_releases=True,
        )
    except (httpx.HTTPError, TypeError, ValueError) as exc:
        raise HTTPException(502, "Unable to load Hardcover new releases") from exc
    title = "New Releases" + (f" - {genre.strip()}" if genre.strip() else "")
    return {"title": title, "books": books}


def configured_nyt_lists(db: Session) -> tuple[str, list[dict]]:
    api_key = settings_map(db).get("nytimes_api_key", "")
    if not api_key:
        return "", []
    try:
        lists = nyt_weekly_lists(api_key)
    except (httpx.HTTPError, TypeError, ValueError):
        lists = []
    if not lists:
        lists = [{"slug": slug, "title": title} for slug, title in NYT_FALLBACK_LISTS.items()]
    return api_key, lists


@app.get("/api/discovery/bestseller-lists")
def api_bestseller_lists(request: Request, db: Annotated[Session, Depends(get_db)]):
    require_user(request, db)
    api_key, lists = configured_nyt_lists(db)
    return {"enabled": bool(api_key), "lists": lists}


@app.get("/api/discovery/bestseller-weeks")
def api_bestseller_weeks(
    request: Request,
    db: Annotated[Session, Depends(get_db)],
    slug: str = "",
):
    require_user(request, db)
    api_key, lists = configured_nyt_lists(db)
    if not api_key:
        raise HTTPException(503, "NYT Books API key is not configured")
    item = next((value for value in lists if value["slug"] == slug), None)
    if item is None:
        raise HTTPException(400, "Invalid bestseller list")
    return {"title": item["title"], "weeks": nyt_weeks(item)}


@app.get("/api/discovery/bestsellers")
def api_bestsellers(
    request: Request,
    db: Annotated[Session, Depends(get_db)],
    slug: str = "",
    date: str = "current",
):
    require_user(request, db)
    api_key, lists = configured_nyt_lists(db)
    if not api_key:
        raise HTTPException(503, "NYT Books API key is not configured")
    item = next((value for value in lists if value["slug"] == slug), None)
    if item is None:
        raise HTTPException(400, "Invalid bestseller list")
    if date != "current" and not re.fullmatch(r"\d{4}-\d{2}-\d{2}", date):
        raise HTTPException(400, "Invalid bestseller week")
    try:
        books = nyt_bestsellers(api_key, slug, date)
    except (httpx.HTTPError, TypeError, ValueError) as exc:
        raise HTTPException(502, "Unable to load bestsellers") from exc
    return {"title": item["title"], "date": date, "books": books}


@app.get("/books/{book_id}", response_class=HTMLResponse)
def book_detail(
    book_id: str,
    request: Request,
    db: Annotated[Session, Depends(get_db)],
    return_to: str = "/",
    navigation: str = "",
):
    user = require_user(request, db)
    book = db.get(Book, book_id)
    if not book:
        raise HTTPException(404)
    return_to = safe_return_to(return_to)
    navigation = safe_return_to(navigation, return_to)
    ordered_ids = list(
        db.scalars(listing_book_query(db, navigation, user.id).with_only_columns(Book.id))
    )
    try:
        position = ordered_ids.index(book.id)
    except ValueError:
        position = -1
    previous_url = (
        book_url(ordered_ids[position - 1], return_to, navigation) if position > 0 else None
    )
    next_url = (
        book_url(ordered_ids[position + 1], return_to, navigation)
        if position >= 0 and position + 1 < len(ordered_ids)
        else None
    )
    detail_url = book_url(book.id, return_to, navigation)
    reading_state = db.scalar(
        select(ReadingState).where(
            ReadingState.user_id == user.id,
            ReadingState.book_id == book.id,
        )
    )
    shelves = accessible_shelves(db, user)
    shelf_ids = set(db.scalars(select(ShelfBook.shelf_id).where(ShelfBook.book_id == book.id)))
    return render(
        request,
        "book.html",
        {
            "book": book,
            "return_to": return_to,
            "previous_url": previous_url,
            "next_url": next_url,
            "detail_url": detail_url,
            "reading_state": reading_state,
            "shelves": shelves,
            "shelf_ids": shelf_ids,
        },
        user,
    )


@app.post("/books/{book_id}/reading-state")
def update_reading_state(
    book_id: str,
    request: Request,
    db: Annotated[Session, Depends(get_db)],
    form_csrf: Annotated[str, Form()],
    state: Annotated[str, Form()],
    return_to: Annotated[str | None, Form()] = None,
    favourite: Annotated[str | None, Form()] = None,
    rating: Annotated[int, Form()] = 0,
):
    user = require_user(request, db)
    check_csrf(request, form_csrf)
    if state not in {"unread", "reading", "finished", "abandoned", "want-to-read"}:
        raise HTTPException(400, "Unknown reading state")
    if rating not in range(6):
        raise HTTPException(400, "Rating must be between 1 and 5")
    book = db.get(Book, book_id)
    if not book:
        raise HTTPException(404)
    reading_state = db.scalar(
        select(ReadingState).where(
            ReadingState.user_id == user.id,
            ReadingState.book_id == book.id,
        )
    )
    if reading_state is None:
        reading_state = ReadingState(user_id=user.id, book_id=book.id)
        db.add(reading_state)
    reading_state.state = state
    reading_state.favourite = favourite == "true"
    reading_state.rating = rating or None
    db.commit()
    return RedirectResponse(safe_return_to(return_to, f"/books/{book.id}"), 303)


@app.get("/shelves", response_class=HTMLResponse)
def shelves_page(request: Request, db: Annotated[Session, Depends(get_db)]):
    user = require_user(request, db)
    shelves = accessible_shelves(db, user)
    counts = {
        shelf.id: db.scalar(select(func.count(ShelfBook.id)).where(ShelfBook.shelf_id == shelf.id))
        or 0
        for shelf in shelves
    }
    all_books_count = db.scalar(
        select(func.count(Book.id)).where(Book.review_state == ReviewState.READY)
    ) or 0
    return render(request, "shelves.html", {"shelves": shelves, "counts": counts,
                                             "all_books_count": all_books_count}, user)


@app.post("/library/bulk")
def library_bulk(
    request: Request,
    db: Annotated[Session, Depends(get_db)],
    action: Annotated[str, Form()],
    form_csrf: Annotated[str, Form()],
    book_ids: Annotated[list[str] | None, Form()] = None,
    shelf_id: Annotated[int | None, Form()] = None,
    return_to: Annotated[str | None, Form()] = None,
):
    user = require_user(request, db)
    check_csrf(request, form_csrf)
    selected_ids = list(dict.fromkeys(book_ids or []))
    if not selected_ids:
        raise HTTPException(400, "Select at least one book")
    books = db.scalars(
        select(Book).where(Book.id.in_(selected_ids), Book.review_state == ReviewState.READY)
    ).all()
    if action == "auto_scrape":
        if user.role != Role.ADMIN:
            raise HTTPException(403, "Only administrators can update library metadata")
        for book in books:
            enqueue(db, "metadata_auto_scrape", payload_json=json.dumps({"book_id": book.id}))
    elif action == "add_to_shelf":
        shelf = db.get(Shelf, shelf_id) if shelf_id is not None else None
        if not shelf or (not shelf.shared and shelf.owner_id != user.id):
            raise HTTPException(404, "Shelf not found")
        existing = set(db.scalars(
            select(ShelfBook.book_id).where(ShelfBook.shelf_id == shelf.id,
                                             ShelfBook.book_id.in_([book.id for book in books]))
        ))
        db.add_all(ShelfBook(shelf_id=shelf.id, book_id=book.id)
                   for book in books if book.id not in existing)
        db.commit()
    else:
        raise HTTPException(400, "Unknown bulk action")
    destination = safe_return_to(return_to, "/?view=all")
    separator = "&" if "?" in destination else "?"
    return RedirectResponse(f"{destination}{separator}bulk=queued", 303)


@app.post("/shelves")
def create_shelf(
    request: Request,
    db: Annotated[Session, Depends(get_db)],
    form_csrf: Annotated[str, Form()],
    name: Annotated[str, Form()],
    shared: Annotated[str | None, Form()] = None,
):
    user = require_user(request, db)
    check_csrf(request, form_csrf)
    name = name.strip()
    if not name:
        raise HTTPException(400, "Shelf name is required")
    make_shared = shared == "true" and user.role == Role.ADMIN
    duplicate = db.scalar(
        select(Shelf.id).where(
            func.lower(Shelf.name) == name.casefold(),
            Shelf.shared.is_(make_shared),
            Shelf.owner_id == (None if make_shared else user.id),
        )
    )
    if duplicate is not None:
        raise HTTPException(409, "A shelf with that name already exists")
    db.add(Shelf(name=name, owner_id=None if make_shared else user.id, shared=make_shared))
    db.commit()
    return RedirectResponse("/shelves", 303)


@app.get("/shelves/{shelf_id}", response_class=HTMLResponse)
def shelf_detail(
    shelf_id: int,
    request: Request,
    db: Annotated[Session, Depends(get_db)],
    sort: str = "title",
    direction: str = "",
    metadata: str = "",
):
    user = require_user(request, db)
    shelf = db.get(Shelf, shelf_id)
    if not shelf or (not shelf.shared and shelf.owner_id != user.id):
        raise HTTPException(404)
    selected_sort = sort if sort in SORT_KEYS else "title"
    selected_direction = (
        direction if direction in {"asc", "desc"} else default_sort_direction(selected_sort)
    )
    query = (
        select(Book)
        .join(ShelfBook, ShelfBook.book_id == Book.id)
        .where(
            ShelfBook.shelf_id == shelf.id,
            Book.review_state == ReviewState.READY,
        )
    )
    if metadata == "missing":
        query = query.where(
            or_(
                Book.cover_path.is_(None),
                Book.cover_path == "",
                Book.description.is_(None),
                Book.description == "",
            )
        )
    books = list(
        db.scalars(query.order_by(*book_order(selected_sort, selected_direction), Book.id))
    )
    return_to = f"/shelves/{shelf.id}?{urlencode({'sort': selected_sort, 'direction': selected_direction, 'metadata': metadata})}"
    return render(
        request,
        "shelf.html",
        {
            "shelf": shelf,
            "books": books,
            "sort": selected_sort,
            "direction": selected_direction,
            "sort_controls": sort_controls(selected_sort, selected_direction),
            "metadata_filter": metadata == "missing",
            "return_to": return_to,
            "can_manage": shelf.shared or shelf.owner_id == user.id,
        },
        user,
    )


@app.post("/books/{book_id}/shelves")
def add_book_to_shelf(
    book_id: str,
    request: Request,
    db: Annotated[Session, Depends(get_db)],
    form_csrf: Annotated[str, Form()],
    shelf_id: Annotated[int, Form()],
    return_to: Annotated[str | None, Form()] = None,
):
    user = require_user(request, db)
    check_csrf(request, form_csrf)
    book = db.get(Book, book_id)
    shelf = db.get(Shelf, shelf_id)
    if not book or not shelf or (not shelf.shared and shelf.owner_id != user.id):
        raise HTTPException(404)
    existing = db.scalar(
        select(ShelfBook.id).where(
            ShelfBook.shelf_id == shelf.id,
            ShelfBook.book_id == book.id,
        )
    )
    if existing is None:
        db.add(ShelfBook(shelf_id=shelf.id, book_id=book.id))
        db.commit()
    return RedirectResponse(safe_return_to(return_to, f"/books/{book.id}"), 303)


@app.post("/shelves/{shelf_id}/books/{book_id}/remove")
def remove_book_from_shelf(
    shelf_id: int,
    book_id: str,
    request: Request,
    db: Annotated[Session, Depends(get_db)],
    form_csrf: Annotated[str, Form()],
    return_to: Annotated[str | None, Form()] = None,
):
    user = require_user(request, db)
    check_csrf(request, form_csrf)
    shelf = db.get(Shelf, shelf_id)
    if not shelf or not (shelf.shared or shelf.owner_id == user.id):
        raise HTTPException(404)
    membership = db.scalar(
        select(ShelfBook).where(
            ShelfBook.shelf_id == shelf.id,
            ShelfBook.book_id == book_id,
        )
    )
    if membership:
        db.delete(membership)
        db.commit()
    return RedirectResponse(safe_return_to(return_to, f"/shelves/{shelf.id}"), 303)


@app.post("/shelves/{shelf_id}/delete")
def delete_shelf(
    shelf_id: int,
    request: Request,
    db: Annotated[Session, Depends(get_db)],
    form_csrf: Annotated[str, Form()],
):
    user = require_user(request, db)
    check_csrf(request, form_csrf)
    shelf = db.get(Shelf, shelf_id)
    if not shelf or not (shelf.owner_id == user.id or user.role == Role.ADMIN):
        raise HTTPException(404)
    db.execute(ShelfBook.__table__.delete().where(ShelfBook.shelf_id == shelf.id))
    db.delete(shelf)
    db.commit()
    return RedirectResponse("/shelves", 303)


@app.get("/books/{book_id}/cover")
def book_cover(book_id: str, request: Request, db: Annotated[Session, Depends(get_db)]):
    require_user(request, db)
    book = db.get(Book, book_id)
    if not book or not book.cover_path:
        raise HTTPException(404)
    cover = Path(book.cover_path)
    if not cover.is_file():
        raise HTTPException(404)
    return FileResponse(cover, headers={"Cache-Control": "no-cache, must-revalidate"})


@app.get("/books/{book_id}/file/{file_id}")
def download_file(
    book_id: str, file_id: int, request: Request, db: Annotated[Session, Depends(get_db)]
):
    require_user(request, db)
    item = db.get(BookFile, file_id)
    if not item or item.book_id != book_id or not Path(item.path).is_file():
        raise HTTPException(404)
    media_types = {
        "epub": "application/epub+zip",
        "kepub": "application/epub+zip",
        "mobi": "application/x-mobipocket-ebook",
        "azw3": "application/vnd.amazon.ebook",
    }
    return FileResponse(
        item.path,
        filename=Path(item.path).name,
        media_type=media_types.get(item.format, "application/octet-stream"),
    )


@app.post("/books/{book_id}/delete")
def delete_book_route(
    book_id: str,
    request: Request,
    db: Annotated[Session, Depends(get_db)],
    form_csrf: Annotated[str, Form()],
):
    user = require_admin(request, db)
    check_csrf(request, form_csrf)
    book = db.get(Book, book_id)
    if not book:
        raise HTTPException(404)
    title = book.title
    try:
        delete_book(db, book)
    except OSError as exc:
        db.rollback()
        raise HTTPException(500, f"Could not delete ebook files: {exc}") from exc
    db.add(AuditEvent(event="book_deleted", user_id=user.id, message=f"Deleted {title}"))
    db.commit()
    return RedirectResponse("/", 303)


@app.post("/books/{book_id}/kindle")
def send_to_kindle(
    book_id: str,
    request: Request,
    db: Annotated[Session, Depends(get_db)],
    form_csrf: Annotated[str, Form()],
):
    user = require_user(request, db)
    check_csrf(request, form_csrf)
    book = db.get(Book, book_id)
    config = settings_map(db)
    if not book or not book.files or not user.kindle_email:
        raise HTTPException(400, "A book file and Kindle address are required")
    item = next((item for item in book.files if item.format == "epub"), None)
    if item is None:
        raise HTTPException(409, "Send to Kindle requires an EPUB edition")
    if item.size_bytes > settings.max_kindle_attachment_mb * 1024 * 1024:
        raise HTTPException(413, "Book exceeds the configured mail attachment limit")
    required = [config.get(key) for key in ("smtp_host", "smtp_user", "smtp_password")]
    if not all(required):
        raise HTTPException(503, "SMTP is not configured")
    message = EmailMessage()
    message["From"], message["To"], message["Subject"] = (
        config["smtp_user"],
        user.kindle_email,
        "Digest delivery",
    )
    message.set_content("Sent from Digest.")
    path = Path(item.path)
    mime = mimetypes.guess_type(path.name)[0] or "application/epub+zip"
    maintype, subtype = mime.split("/", 1)
    message.add_attachment(
        path.read_bytes(), maintype=maintype, subtype=subtype, filename=path.name
    )
    try:
        with smtplib.SMTP(
            config["smtp_host"], int(config.get("smtp_port", "587")), timeout=30
        ) as smtp:
            if config.get("smtp_starttls", "true") == "true":
                smtp.starttls()
            smtp.login(config["smtp_user"], config["smtp_password"])
            smtp.send_message(message)
    except Exception as exc:
        db.add(
            AuditEvent(
                level="error",
                event="kindle_error",
                user_id=user.id,
                message=f"{type(exc).__name__}: {exc}",
            )
        )
        db.commit()
        raise HTTPException(502, "Kindle delivery failed") from exc
    db.add(AuditEvent(event="kindle_sent", user_id=user.id, message=f"Sent {book.title} to Kindle"))
    db.commit()
    return RedirectResponse(f"/books/{book_id}?sent=1", 303)


@app.get("/review", response_class=HTMLResponse)
def review(request: Request, db: Annotated[Session, Depends(get_db)]):
    user = require_admin(request, db)
    books = db.scalars(
        select(Book)
        .where(Book.review_state.in_([ReviewState.REVIEW, ReviewState.REJECTED, ReviewState.ERROR]))
        .order_by(Book.created_at.desc())
    ).all()
    return render(request, "review.html", {"books": books}, user)


@app.get("/review/{book_id}", response_class=HTMLResponse)
def review_book(
    book_id: str,
    request: Request,
    db: Annotated[Session, Depends(get_db)],
    search_title: str = "",
    search_author: str = "",
    return_to: str = "/review",
):
    user = require_admin(request, db)
    book = db.get(Book, book_id)
    if not book:
        raise HTTPException(404)
    manual_search = bool(search_title.strip() or search_author.strip())
    candidates = (
        find_candidates(
            db,
            book,
            title=search_title,
            author=search_author,
            isbns=[],
        )
        if manual_search and book.review_state != ReviewState.REJECTED
        else []
    )
    return render(
        request,
        "review_book.html",
        {
            "book": book,
            "candidates": candidates,
            "candidate_json": [json.dumps(item) for item in candidates],
            "search_title": search_title,
            "search_author": search_author,
            "manual_search": manual_search,
            "return_to": safe_return_to(return_to, "/review"),
            **metadata_suggestions(db),
        },
        user,
    )


@app.post("/review/{book_id}/apply")
def review_apply(
    book_id: str,
    request: Request,
    db: Annotated[Session, Depends(get_db)],
    candidate: Annotated[str, Form()],
    form_csrf: Annotated[str, Form()],
    return_to: Annotated[str | None, Form()] = None,
):
    require_admin(request, db)
    check_csrf(request, form_csrf)
    book = db.get(Book, book_id)
    if not book:
        raise HTTPException(404)
    apply_candidate(
        db,
        book,
        json.loads(candidate),
        organise=True,
        replace_existing=True,
    )
    return RedirectResponse(safe_return_to(return_to, "/review"), 303)


@app.post("/review/{book_id}/embedded")
def review_embedded(
    book_id: str,
    request: Request,
    db: Annotated[Session, Depends(get_db)],
    form_csrf: Annotated[str, Form()],
    return_to: Annotated[str | None, Form()] = None,
):
    require_admin(request, db)
    check_csrf(request, form_csrf)
    book = db.get(Book, book_id)
    if not book:
        raise HTTPException(404)
    organise_book(db, book)
    return RedirectResponse(safe_return_to(return_to, "/review"), 303)


@app.post("/review/{book_id}/manual")
def review_manual(
    book_id: str,
    request: Request,
    db: Annotated[Session, Depends(get_db)],
    form_csrf: Annotated[str, Form()],
    title: Annotated[str | None, Form()] = None,
    authors: Annotated[str | None, Form()] = None,
    isbns: Annotated[str | None, Form()] = None,
    language: Annotated[str | None, Form()] = None,
    description: Annotated[str | None, Form()] = None,
    publication_date: Annotated[str | None, Form()] = None,
    page_count: Annotated[str | None, Form()] = None,
    series: Annotated[str | None, Form()] = None,
    series_number: Annotated[str | None, Form()] = None,
    locked_fields: Annotated[list[str] | None, Form()] = None,
    return_to: Annotated[str | None, Form()] = None,
    custom_cover: Annotated[UploadFile | None, File()] = None,
):
    user = require_admin(request, db)
    check_csrf(request, form_csrf)
    book = db.get(Book, book_id)
    if not book:
        raise HTTPException(404)
    data = {
        "title": title or "",
        "authors": authors or "",
        "isbns": isbns or "",
        "language": language or "",
        "description": description or "",
        "publication_date": publication_date or "",
        "page_count": page_count or "",
        "series": series or "",
        "series_number": series_number or "",
    }
    try:
        cover_content = None
        if custom_cover and custom_cover.filename:
            cover_content = normalise_uploaded_cover(custom_cover.file.read())
        apply_manual_metadata(db, book, data, locked_fields or [])
        if cover_content is not None:
            save_uploaded_cover(book, cover_content)
            db.commit()
    except (TypeError, ValueError) as exc:
        return render(
            request,
            "review_book.html",
            {
                "book": book,
                "candidates": [],
                "candidate_json": [],
                "error": str(exc),
                "return_to": safe_return_to(return_to, "/review"),
                "search_title": "",
                "search_author": "",
                "manual_search": False,
                **metadata_suggestions(db),
            },
            user,
        )
    return RedirectResponse(safe_return_to(return_to, f"/books/{book.id}"), 303)


@app.post("/review/bulk")
def review_bulk(
    request: Request,
    db: Annotated[Session, Depends(get_db)],
    action: Annotated[str, Form()],
    form_csrf: Annotated[str, Form()],
    book_ids: Annotated[list[str] | None, Form()] = None,
):
    require_admin(request, db)
    check_csrf(request, form_csrf)
    books = db.scalars(select(Book).where(Book.id.in_(book_ids or []))).all()
    if action == "approve":
        for book in books:
            if book.review_state != ReviewState.REJECTED:
                organise_book(db, book)
    elif action == "refresh":
        for book in books:
            enqueue(db, "metadata_refresh", payload_json=json.dumps({"book_id": book.id}))
    else:
        raise HTTPException(400, "Unknown bulk action")
    return RedirectResponse("/review", 303)


@app.get("/settings", response_class=HTMLResponse)
def settings_page(request: Request, db: Annotated[Session, Depends(get_db)]):
    user = require_user(request, db)
    return render(
        request,
        "settings.html",
        {
            "settings": settings_map(db),
            "shelves": accessible_shelves(db, user),
            "kobo_configured": active_kobo_token(db, user) is not None,
        },
        user,
    )


@app.post("/settings/profile")
def profile_settings(
    request: Request,
    db: Annotated[Session, Depends(get_db)],
    kindle_email: Annotated[str, Form()],
    form_csrf: Annotated[str, Form()],
    kobo_sync_shelf_id: Annotated[str | None, Form()] = None,
):
    user = require_user(request, db)
    check_csrf(request, form_csrf)
    user.kindle_email = kindle_email.strip() or None
    sync_all = kobo_sync_shelf_id == "all"
    try:
        shelf_id = int(kobo_sync_shelf_id) if kobo_sync_shelf_id and not sync_all else None
    except (TypeError, ValueError) as exc:
        raise HTTPException(400, "Invalid Kobo sync shelf") from exc
    if shelf_id is not None:
        allowed = {shelf.id for shelf in accessible_shelves(db, user)}
        if shelf_id not in allowed:
            raise HTTPException(400, "Invalid Kobo sync shelf")
    user.kobo_sync_shelf_id = shelf_id
    user.kobo_sync_all_books = sync_all
    db.commit()
    return RedirectResponse("/settings", 303)


@app.post("/settings/kobo-token", response_class=HTMLResponse)
def create_kobo_token(
    request: Request,
    db: Annotated[Session, Depends(get_db)],
    form_csrf: Annotated[str, Form()],
):
    user = require_user(request, db)
    check_csrf(request, form_csrf)
    current = active_kobo_token(db, user)
    if current:
        revoke_token(db, user, current)
    db.execute(KoboSyncedBook.__table__.delete().where(KoboSyncedBook.user_id == user.id))
    db.execute(KoboSyncedShelf.__table__.delete().where(KoboSyncedShelf.user_id == user.id))
    db.commit()
    _, plain_token = create_token(db, user, user, KOBO_TOKEN_NAME)
    endpoint = f"{settings.public_url.rstrip('/')}/kobo/{plain_token}"
    return render(
        request,
        "settings.html",
        {
            "settings": settings_map(db),
            "shelves": accessible_shelves(db, user),
            "kobo_configured": True,
            "kobo_endpoint": endpoint,
        },
        user,
    )


@app.post("/settings/kobo-token/revoke")
def revoke_kobo_token(
    request: Request,
    db: Annotated[Session, Depends(get_db)],
    form_csrf: Annotated[str, Form()],
):
    user = require_user(request, db)
    check_csrf(request, form_csrf)
    current = active_kobo_token(db, user)
    if current:
        revoke_token(db, user, current)
    return RedirectResponse("/settings", 303)


@app.get("/kobo/{token}/v1/initialization")
def kobo_initialize(token: str, request: Request, db: Annotated[Session, Depends(get_db)]):
    kobo_user(db, token)
    response = JSONResponse(kobo_initialization(request, token))
    response.headers["x-kobo-apitoken"] = "e30="
    return response


@app.post("/kobo/{token}/v1/auth/device")
@app.post("/kobo/{token}/v1/auth/refresh")
def kobo_auth(
    token: str,
    db: Annotated[Session, Depends(get_db)],
    payload: Annotated[dict | None, Body()] = None,
):
    kobo_user(db, token)
    return dummy_auth(payload)


@app.get("/kobo/{token}/v1/library/sync")
def kobo_sync(token: str, request: Request, db: Annotated[Session, Depends(get_db)]):
    user = kobo_user(db, token)
    base_url = str(request.base_url).rstrip("/")
    return JSONResponse(sync_payload(db, user, base_url, token))


@app.get("/kobo/{token}/v1/library/{book_id}/metadata")
def kobo_book_metadata(
    token: str, book_id: str, request: Request, db: Annotated[Session, Depends(get_db)]
):
    user = kobo_user(db, token)
    book = kobo_shelf_book(db, user, book_id)
    return [kobo_metadata(book, str(request.base_url).rstrip("/"), token)]


@app.api_route("/kobo/{token}/v1/library/{book_id}/state", methods=["GET", "PUT"])
def kobo_reading_state(
    token: str,
    book_id: str,
    request: Request,
    db: Annotated[Session, Depends(get_db)],
    payload: Annotated[dict | None, Body()] = None,
):
    user = kobo_user(db, token)
    book = kobo_shelf_book(db, user, book_id)
    if request.method == "GET":
        state = get_reading_state(db, user, book)
        db.commit()
        return [reading_state_payload(book, state)]
    try:
        _, result = update_kobo_reading_state(db, user, book, payload or {})
    except (TypeError, ValueError) as exc:
        raise HTTPException(400, str(exc)) from exc
    return {"RequestResult": "Success", "UpdateResults": [result]}


@app.delete("/kobo/{token}/v1/library/{book_id}", status_code=204)
def kobo_archive_book(
    token: str, book_id: str, db: Annotated[Session, Depends(get_db)]
):
    user = kobo_user(db, token)
    archive_from_device(db, user, book_id)
    return Response(status_code=204)


@app.post("/kobo/{token}/v1/library/tags", status_code=201)
def kobo_create_collection(
    token: str,
    db: Annotated[Session, Depends(get_db)],
    payload: Annotated[dict, Body()],
):
    user = kobo_user(db, token)
    try:
        shelf = create_tag(db, user, payload)
    except (TypeError, ValueError) as exc:
        raise HTTPException(400, str(exc)) from exc
    return JSONResponse(shelf_tag_id(shelf.id), status_code=201)


@app.api_route(
    "/kobo/{token}/v1/library/tags/{tag_id}", methods=["PUT", "DELETE"]
)
def kobo_update_collection(
    token: str,
    tag_id: str,
    request: Request,
    db: Annotated[Session, Depends(get_db)],
    payload: Annotated[dict | None, Body()] = None,
):
    user = kobo_user(db, token)
    shelf = shelf_for_tag(db, user, tag_id)
    if request.method == "DELETE":
        delete_tag(db, user, shelf)
        return Response(status_code=204)
    try:
        update_tag(db, user, shelf, payload or {})
    except (TypeError, ValueError) as exc:
        raise HTTPException(400, str(exc)) from exc
    return Response(status_code=200)


@app.post("/kobo/{token}/v1/library/tags/{tag_id}/items", status_code=201)
def kobo_add_collection_items(
    token: str,
    tag_id: str,
    db: Annotated[Session, Depends(get_db)],
    payload: Annotated[dict, Body()],
):
    user = kobo_user(db, token)
    try:
        add_tag_items(db, user, shelf_for_tag(db, user, tag_id), payload)
    except (TypeError, ValueError) as exc:
        raise HTTPException(400, str(exc)) from exc
    return Response(status_code=201)


@app.post("/kobo/{token}/v1/library/tags/{tag_id}/items/delete")
def kobo_remove_collection_items(
    token: str,
    tag_id: str,
    db: Annotated[Session, Depends(get_db)],
    payload: Annotated[dict, Body()],
):
    user = kobo_user(db, token)
    try:
        remove_tag_items(db, user, shelf_for_tag(db, user, tag_id), payload)
    except (TypeError, ValueError) as exc:
        raise HTTPException(400, str(exc)) from exc
    return Response(status_code=200)


@app.get("/kobo/{token}/download/{book_id}")
def kobo_download(token: str, book_id: str, db: Annotated[Session, Depends(get_db)]):
    user = kobo_user(db, token)
    item = preferred_file(kobo_shelf_book(db, user, book_id))
    if not item:
        raise HTTPException(404, "No Kobo-compatible file")
    return FileResponse(item.path, filename=Path(item.path).name, media_type="application/epub+zip")


@app.get("/kobo/{token}/cover/{book_id}/{width}/{height}/{rest:path}")
def kobo_cover(
    token: str,
    book_id: str,
    width: str,
    height: str,
    rest: str,
    db: Annotated[Session, Depends(get_db)],
):
    user = kobo_user(db, token)
    book = kobo_shelf_book(db, user, book_id)
    cover = Path(book.cover_path) if book.cover_path else None
    if not cover or not cover.is_file():
        raise HTTPException(404)
    return FileResponse(cover, media_type="image/jpeg")


@app.get("/kobo/{token}")
def kobo_root(token: str, db: Annotated[Session, Depends(get_db)]):
    kobo_user(db, token)
    return {}


@app.get("/kobo/{token}/v1/user/loyalty/benefits")
def kobo_benefits(token: str, db: Annotated[Session, Depends(get_db)]):
    kobo_user(db, token)
    return {"Benefits": {}}


@app.api_route(
    "/kobo/{token}/v1/analytics/gettests", methods=["GET", "POST"]
)
def kobo_analytics_tests(token: str, db: Annotated[Session, Depends(get_db)]):
    kobo_user(db, token)
    return {"Result": "Success", "TestKey": "", "Tests": {}}


@app.api_route(
    "/kobo/{token}/v1/{path:path}", methods=["GET", "POST", "PUT", "DELETE"]
)
def kobo_compatibility_fallback(
    token: str, path: str, db: Annotated[Session, Depends(get_db)]
):
    kobo_user(db, token)
    return {}


def user_admin_context(db: Session, error: str | None = None) -> dict:
    users = db.scalars(select(User).order_by(func.lower(User.username))).all()
    return {"users": users, "roles": list(Role), "error": error}


@app.get("/admin/users", response_class=HTMLResponse)
def users_page(request: Request, db: Annotated[Session, Depends(get_db)]):
    user = require_admin(request, db)
    return render(request, "users.html", user_admin_context(db), user)


@app.post("/admin/users")
def users_create(
    request: Request,
    db: Annotated[Session, Depends(get_db)],
    username: Annotated[str, Form()],
    password: Annotated[str, Form()],
    confirm: Annotated[str, Form()],
    role: Annotated[str, Form()],
    form_csrf: Annotated[str, Form()],
):
    actor = require_admin(request, db)
    check_csrf(request, form_csrf)
    try:
        create_account(db, actor, username, password, confirm, Role(role))
    except (AccountError, ValueError) as exc:
        return render(request, "users.html", user_admin_context(db, str(exc)), actor)
    return RedirectResponse("/admin/users?created=1", 303)


@app.post("/admin/users/{user_id}/update")
def users_update(
    user_id: int,
    request: Request,
    db: Annotated[Session, Depends(get_db)],
    username: Annotated[str, Form()],
    role: Annotated[str, Form()],
    form_csrf: Annotated[str, Form()],
    is_active: Annotated[str | None, Form()] = None,
):
    actor = require_admin(request, db)
    check_csrf(request, form_csrf)
    account = db.get(User, user_id)
    if account is None:
        raise HTTPException(404)
    try:
        update_account(db, actor, account, username, Role(role), is_active == "on")
    except (AccountError, ValueError) as exc:
        return render(request, "users.html", user_admin_context(db, str(exc)), actor)
    return RedirectResponse("/admin/users?updated=1", 303)


@app.post("/admin/users/{user_id}/password")
def users_password(
    user_id: int,
    request: Request,
    db: Annotated[Session, Depends(get_db)],
    password: Annotated[str, Form()],
    confirm: Annotated[str, Form()],
    form_csrf: Annotated[str, Form()],
):
    actor = require_admin(request, db)
    check_csrf(request, form_csrf)
    account = db.get(User, user_id)
    if account is None:
        raise HTTPException(404)
    try:
        reset_password(db, actor, account, password, confirm)
    except AccountError as exc:
        return render(request, "users.html", user_admin_context(db, str(exc)), actor)
    return RedirectResponse("/admin/users?password_reset=1", 303)


def token_admin_context(
    db: Session, error: str | None = None, plain_token: str | None = None
) -> dict:
    items = db.scalars(select(ApiToken).order_by(ApiToken.created_at.desc())).all()
    users = db.scalars(select(User).where(User.is_active.is_(True)).order_by(User.username)).all()
    owners = {user.id: user.username for user in db.scalars(select(User)).all()}
    return {
        "tokens": items,
        "users": users,
        "owners": owners,
        "error": error,
        "plain_token": plain_token,
    }


@app.get("/admin/tokens", response_class=HTMLResponse)
def tokens_page(request: Request, db: Annotated[Session, Depends(get_db)]):
    user = require_admin(request, db)
    return render(request, "tokens.html", token_admin_context(db), user)


@app.post("/admin/tokens")
def tokens_create(
    request: Request,
    db: Annotated[Session, Depends(get_db)],
    name: Annotated[str, Form()],
    user_id: Annotated[int, Form()],
    form_csrf: Annotated[str, Form()],
):
    user = require_admin(request, db)
    check_csrf(request, form_csrf)
    try:
        owner = db.get(User, user_id)
        if owner is None:
            raise TokenError("Select a valid token owner.")
        _, plain_token = create_token(db, user, owner, name)
    except TokenError as exc:
        return render(request, "tokens.html", token_admin_context(db, str(exc)), user)
    return render(request, "tokens.html", token_admin_context(db, plain_token=plain_token), user)


@app.post("/admin/tokens/{token_id}/revoke")
def tokens_revoke(
    token_id: int,
    request: Request,
    db: Annotated[Session, Depends(get_db)],
    form_csrf: Annotated[str, Form()],
):
    user = require_admin(request, db)
    check_csrf(request, form_csrf)
    item = db.get(ApiToken, token_id)
    if item is None:
        raise HTTPException(404)
    revoke_token(db, user, item)
    return RedirectResponse("/admin/tokens?revoked=1", 303)


def admin_config_context(db: Session, error: str | None = None) -> dict:
    config = settings_map(db)
    try:
        provider_order = ", ".join(json.loads(config.get("metadata_provider_order", "[]")))
    except (json.JSONDecodeError, TypeError):
        provider_order = ""
    if not provider_order:
        provider_order = ", ".join(PROVIDERS)
    aliases = {}
    for key in ("author_aliases", "series_aliases"):
        try:
            aliases[key] = "\n".join(
                f"{alias} = {canonical}"
                for alias, canonical in json.loads(config.get(key, "{}")).items()
            )
        except (TypeError, ValueError):
            aliases[key] = ""
    return {
        "config": config,
        "aliases": aliases,
        "providers": PROVIDERS,
        "provider_order": provider_order,
        "error": error,
        "secret_configured": {
            key: bool(config.get(key))
            for key in (
                "hardcover_api_key",
                "google_books_api_key",
                "isbndb_api_key",
                "nytimes_api_key",
                "smtp_password",
                "prowlarr_api_key",
                "sabnzbd_api_key",
            )
        },
    }


@app.get("/admin/config", response_class=HTMLResponse)
def admin_config_page(request: Request, db: Annotated[Session, Depends(get_db)]):
    user = require_admin(request, db)
    return render(request, "admin_config.html", admin_config_context(db), user)


@app.post("/admin/config")
def admin_config_save(
    request: Request,
    db: Annotated[Session, Depends(get_db)],
    metadata_provider_order: Annotated[str, Form()],
    auto_match_threshold: Annotated[str, Form()],
    metadata_refresh_hours: Annotated[str, Form()],
    discovery_refresh_hours: Annotated[str, Form()],
    default_language: Annotated[str, Form()],
    smtp_host: Annotated[str, Form()],
    smtp_port: Annotated[str, Form()],
    smtp_user: Annotated[str, Form()],
    form_csrf: Annotated[str, Form()],
    author_aliases: Annotated[str | None, Form()] = None,
    series_aliases: Annotated[str | None, Form()] = None,
    hardcover_api_key: Annotated[str | None, Form()] = None,
    google_books_api_key: Annotated[str | None, Form()] = None,
    isbndb_api_key: Annotated[str | None, Form()] = None,
    nytimes_api_key: Annotated[str | None, Form()] = None,
    smtp_password: Annotated[str | None, Form()] = None,
    smtp_starttls: Annotated[str | None, Form()] = None,
    shelfmark_enabled: Annotated[str | None, Form()] = None,
    shelfmark_url: Annotated[str | None, Form()] = None,
    usenet_enabled: Annotated[str | None, Form()] = None,
    prowlarr_url: Annotated[str | None, Form()] = None,
    prowlarr_api_key: Annotated[str | None, Form()] = None,
    sabnzbd_url: Annotated[str | None, Form()] = None,
    sabnzbd_api_key: Annotated[str | None, Form()] = None,
    sabnzbd_category: Annotated[str | None, Form()] = None,
):
    user = require_admin(request, db)
    check_csrf(request, form_csrf)
    values = {
        "metadata_provider_order": metadata_provider_order,
        "auto_match_threshold": auto_match_threshold,
        "metadata_refresh_hours": metadata_refresh_hours,
        "discovery_refresh_hours": discovery_refresh_hours,
        "default_language": default_language,
        "author_aliases": author_aliases or "",
        "series_aliases": series_aliases or "",
        "hardcover_api_key": hardcover_api_key or "",
        "google_books_api_key": google_books_api_key or "",
        "isbndb_api_key": isbndb_api_key or "",
        "nytimes_api_key": nytimes_api_key or "",
        "smtp_host": smtp_host,
        "smtp_port": smtp_port,
        "smtp_user": smtp_user,
        "smtp_password": smtp_password or "",
        "smtp_starttls": smtp_starttls or "",
        "shelfmark_enabled": shelfmark_enabled or "",
        "shelfmark_url": shelfmark_url or "",
        "usenet_enabled": usenet_enabled or "",
        "prowlarr_url": prowlarr_url or "",
        "prowlarr_api_key": prowlarr_api_key or "",
        "sabnzbd_url": sabnzbd_url or "",
        "sabnzbd_api_key": sabnzbd_api_key or "",
        "sabnzbd_category": sabnzbd_category or "ebooks",
    }
    try:
        save_admin_settings(db, user, values)
    except SettingsError as exc:
        return render(request, "admin_config.html", admin_config_context(db, str(exc)), user)
    return RedirectResponse("/admin/config?saved=1", 303)


@app.post("/admin/discovery/refresh")
def admin_discovery_refresh(
    request: Request,
    db: Annotated[Session, Depends(get_db)],
    form_csrf: Annotated[str, Form()],
):
    require_admin(request, db)
    check_csrf(request, form_csrf)
    active = db.scalar(
        select(Job.id).where(
            Job.kind == "discovery_refresh",
            Job.status.in_([JobStatus.QUEUED, JobStatus.RUNNING]),
        )
    )
    if active is None:
        enqueue(db, "discovery_refresh")
    return RedirectResponse("/admin/config?discovery_refresh=queued", 303)


@app.post("/admin/scan")
def manual_scan(
    request: Request, db: Annotated[Session, Depends(get_db)], form_csrf: Annotated[str, Form()]
):
    require_admin(request, db)
    check_csrf(request, form_csrf)
    scan_library(db, initial=False)
    return RedirectResponse("/review", 303)


@app.get("/opds")
def opds(request: Request, db: Annotated[Session, Depends(get_db)]):
    try:
        require_user(request, db)
    except HTTPException as exc:
        if exc.status_code == 401:
            raise HTTPException(
                401,
                "Use an API token as the password",
                headers={"WWW-Authenticate": 'Basic realm="Digest OPDS"'},
            ) from exc
        raise
    feed = Element("feed", xmlns="http://www.w3.org/2005/Atom")
    SubElement(feed, "title").text = "Digest Library"
    SubElement(feed, "id").text = settings.public_url + "/opds"
    for book in db.scalars(
        select(Book).where(Book.review_state == ReviewState.READY).order_by(Book.title)
    ).all():
        entry = SubElement(feed, "entry")
        SubElement(entry, "id").text = book.id
        SubElement(entry, "title").text = book.title
        author = SubElement(entry, "author")
        SubElement(author, "name").text = book.primary_author
        media_types = {
            "epub": "application/epub+zip",
            "kepub": "application/epub+zip",
            "mobi": "application/x-mobipocket-ebook",
            "azw3": "application/vnd.amazon.ebook",
        }
        for item in book.files:
            SubElement(
                entry,
                "link",
                rel="http://opds-spec.org/acquisition",
                href=f"{settings.public_url}/books/{book.id}/file/{item.id}",
                type=media_types.get(item.format, "application/octet-stream"),
            )
    return Response(
        tostring(feed, encoding="utf-8", xml_declaration=True), media_type="application/atom+xml"
    )
