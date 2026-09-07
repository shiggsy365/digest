import json
import logging
import math
import mimetypes
import re
import secrets
import smtplib
from datetime import UTC, datetime, timedelta
from email.message import EmailMessage
from email.utils import format_datetime
from pathlib import Path
from typing import Annotated
from urllib.parse import parse_qs, quote, urlencode, urlsplit
from xml.etree.ElementTree import Element, SubElement, register_namespace, tostring

import httpx
from fastapi import Body, Depends, FastAPI, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, RedirectResponse, Response
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from sqlalchemy import case, func, or_, select
from sqlalchemy.orm import Session
from starlette.middleware.sessions import SessionMiddleware

from .accounts import AccountError, create_account, reset_password, update_account
from .acquisition import (
    cancel_acquisition,
    create_wanted,
    find_wanted,
    queue_release,
    retry_acquisition,
)
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
    hardcover_genre_query_label,
    hardcover_genres,
    nyt_bestsellers,
    nyt_weekly_lists,
    nyt_weeks,
    search_discovery_books,
)
from .ereader_api import router as ereader_api_router
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
    sync_pending,
    sync_token,
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
    EDITABLE_FIELDS,
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
    TrustedDevice,
    User,
    WantedItem,
    WantedStatus,
)
from .security import (
    KOBO_TOKEN_NAME,
    TRUSTED_DEVICE_COOKIE,
    create_trusted_device,
    current_user,
    hash_password,
    revoke_trusted_device,
    setup_required,
    token_digest,
    trusted_device_from_request,
    verify_password,
)
from .text import plain_text
from .tokens import TokenError, create_token, revoke_token

logger = logging.getLogger(__name__)
_nyt_opds_cache: dict[tuple[str, str], tuple[datetime, list[dict]]] = {}
NYT_OPDS_CACHE_TTL = timedelta(hours=6)
NYT_OPDS_EMPTY_CACHE_TTL = timedelta(minutes=15)

settings = get_settings()
app = FastAPI(title="Digest", version="0.1.0")
app.include_router(ereader_api_router)
app.add_middleware(
    SessionMiddleware,
    secret_key=settings.secret_key,
    max_age=settings.session_days * 86400,
    same_site="lax",
    https_only=settings.public_url.startswith("https://"),
)


@app.middleware("http")
async def choose_session_lifetime(request: Request, call_next):
    """Use a browser-session cookie unless the user explicitly asks to be remembered."""
    response = await call_next(request)
    response.raw_headers = [
        (
            name,
            _session_cookie_lifetime(value, bool(request.session.get("remember_me")))
            if name.lower() == b"set-cookie" and value.startswith(b"session=")
            else value,
        )
        for name, value in response.raw_headers
    ]
    return response


def _session_cookie_lifetime(value: bytes, remember: bool) -> bytes:
    """Make persistent sessions compatible with older e-reader browsers."""
    if not remember:
        return re.sub(
            rb"; (?:Max-Age=\d+|Expires=[^;]+)", b"", value, flags=re.IGNORECASE
        )
    if not re.search(rb"; Expires=", value, flags=re.IGNORECASE):
        expires = format_datetime(datetime.now(UTC) + timedelta(days=settings.session_days), usegmt=True)
        value += b"; Expires=" + expires.encode("ascii")
    return value


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

OPDS_MEDIA_TYPES = {
    "epub": "application/epub+zip",
    "kepub": "application/epub+zip",
    "mobi": "application/x-mobipocket-ebook",
    "azw3": "application/vnd.amazon.ebook",
    "pdf": "application/pdf",
}
DC_NS = "http://purl.org/dc/terms/"
OPDS_NS = "http://opds-spec.org/2010/catalog"
ATOM_NS = "http://www.w3.org/2005/Atom"
OS_NS = "http://a9.com/-/spec/opensearch/1.1/"
register_namespace("", ATOM_NS)
register_namespace("dc", DC_NS)
register_namespace("opds", OPDS_NS)
register_namespace("os", OS_NS)
OPDS_DISCOVER_GENRES = (
    "fantasy",
    "science_fiction",
    "mystery_and_detective_stories",
    "thriller",
    "romance",
    "historical_fiction",
    "horror",
    "biography",
    "history",
    "self_help",
    "true_crime",
)
OPDS_NEW_RELEASE_PERIODS = {
    "30d": ("Past 30 Days", 30),
    "90d": ("Past 90 Days", 90),
    "180d": ("Past 6 Months", 180),
    "365d": ("Past Year", 365),
}


@app.on_event("startup")
def startup() -> None:
    settings.data_root.mkdir(parents=True, exist_ok=True)
    initialise_database()


def user_or_none(request: Request, db: Session) -> User | None:
    user_id = request.session.get("user_id")
    user = db.get(User, user_id) if user_id else None
    if not user or not user.is_active:
        device = trusted_device_from_request(request, db)
        user = db.get(User, device.user_id) if device else None
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


def trusted_device_cookie_options(request: Request) -> dict:
    # Some e-reader browsers ignore Max-Age and treat the cookie as session-only
    # unless an explicit Expires date is also present (see _session_cookie_lifetime).
    # Ancient e-reader WebKit builds predate the SameSite attribute and can drop a
    # cookie outright rather than ignore an attribute they don't recognise, so leave
    # it off for them -- the app's CSRF token already covers what SameSite would.
    seconds = settings.trusted_device_days * 86400
    return {
        "max_age": seconds,
        "expires": seconds,
        "httponly": True,
        "samesite": None if is_ereader_request(request) else "lax",
        "secure": settings.public_url.startswith("https://"),
    }


def set_trusted_device_cookie(request: Request, response: Response, token: str) -> None:
    response.set_cookie(TRUSTED_DEVICE_COOKIE, token, **trusted_device_cookie_options(request))


def clear_trusted_device_cookie(request: Request, response: Response) -> None:
    response.delete_cookie(
        TRUSTED_DEVICE_COOKIE,
        path="/",
        samesite=None if is_ereader_request(request) else "lax",
        secure=settings.public_url.startswith("https://"),
    )


def current_trusted_device(request: Request, db: Session, user: User) -> TrustedDevice | None:
    token = request.cookies.get(TRUSTED_DEVICE_COOKIE, "")
    if not token.startswith("dtd_"):
        return None
    return db.scalar(
        select(TrustedDevice).where(
            TrustedDevice.user_id == user.id,
            TrustedDevice.token_hash == token_digest(token),
            TrustedDevice.revoked_at.is_(None),
        )
    )


def trusted_devices_for_settings(request: Request, db: Session, user: User) -> list[dict]:
    current = current_trusted_device(request, db, user)
    devices = db.scalars(
        select(TrustedDevice)
        .where(TrustedDevice.user_id == user.id, TrustedDevice.revoked_at.is_(None))
        .order_by(TrustedDevice.last_used_at.desc(), TrustedDevice.id.desc())
    ).all()
    return [
        {
            "id": device.id,
            "user_agent": device.user_agent or "Unknown device",
            "created_at": device.created_at,
            "last_used_at": device.last_used_at,
            "current": bool(current and current.id == device.id),
        }
        for device in devices
    ]


def settings_context(request: Request, db: Session, user: User, **extra) -> dict:
    context = {
        "settings": settings_map(db),
        "shelves": accessible_shelves(db, user),
        "kobo_configured": active_kobo_token(db, user) is not None,
        "trusted_devices": trusted_devices_for_settings(request, db, user),
        "trusted_device_days": settings.trusted_device_days,
    }
    context.update(extra)
    return context


def safe_return_to(value: str | None, fallback: str = "/") -> str:
    value = (value or "").strip()
    has_local_path = (
        value.startswith("/")
        and not value.startswith("//")
        and not re.match(r"^/\w[\w+.-]*:", value)
    )
    return (
        value
        if has_local_path
        else fallback
    )


METADATA_LOCK_FIELDS = sorted(EDITABLE_FIELDS)


def set_all_metadata_locked(book: Book, locked: bool) -> None:
    book.locked_fields_json = json.dumps(METADATA_LOCK_FIELDS if locked else [])


def all_metadata_locked(book: Book) -> bool:
    try:
        locked = set(json.loads(book.locked_fields_json or "[]"))
    except (TypeError, ValueError):
        return False
    return set(METADATA_LOCK_FIELDS).issubset(locked)


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


def is_ereader_request(request: Request) -> bool:
    user_agent = request.headers.get("user-agent", "").casefold()
    return "kindle" in user_agent or "kobo" in user_agent


def render(request: Request, name: str, context: dict, user: User | None = None) -> HTMLResponse:
    if user is not None and is_ereader_request(request) and settings.ereader_spa:
        return templates.TemplateResponse(
            request,
            "ereader/app.html",
            {"user": user, "csrf": csrf(request)},
        )
    family = "ereader" if is_ereader_request(request) else "modern"
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
    remember_me: Annotated[str | None, Form()] = None,
):
    check_csrf(request, form_csrf)
    user = db.scalar(select(User).where(func.lower(User.username) == username.strip().lower()))
    if not user or not user.is_active or not verify_password(password, user.password_hash):
        db.add(
            AuditEvent(level="warning", event="login", message=f"Failed login for {username[:80]}")
        )
        db.commit()
        return render(
            request,
            "login.html",
            {"error": "Invalid username or password.", "remember_me": bool(remember_me)},
        )
    request.session.clear()
    request.session["user_id"] = user.id
    bookmark_token: str | None = None
    if remember_me:
        request.session["remember_me"] = True
        existing_token = request.cookies.get(TRUSTED_DEVICE_COOKIE, "")
        existing_device = current_trusted_device(request, db, user) if existing_token else None
        if existing_device:
            existing_device.last_used_at = datetime.now(UTC)
            bookmark_token = existing_token
        else:
            trusted_device, trusted_token = create_trusted_device(
                db, user, request.headers.get("user-agent", "")
            )
            bookmark_token = trusted_token
            db.add(
                AuditEvent(
                    event="trusted_device_created",
                    user_id=user.id,
                    message=f"Remembered trusted device {trusted_device.id}",
                )
            )
    db.add(AuditEvent(event="login", user_id=user.id, message="Login successful"))
    db.commit()
    if bookmark_token and is_ereader_request(request):
        # Cookies don't reliably survive a full restart on some e-reader browsers, so
        # send them to the bookmarkable magic-link landing page instead of "/" -- its
        # own URL is the credential, so it works even with no cookie storage at all.
        # welcome=1 marks this as the first visit so the landing page shows the
        # bookmark instructions once; opening the bookmarked URL plain afterwards
        # (e.g. as a browser home page) skips straight to the library.
        response = RedirectResponse(f"/trusted-device/{bookmark_token}?welcome=1", 303)
    else:
        response = RedirectResponse(safe_return_to(request.query_params.get("next"), "/"), 303)
    if remember_me:
        set_trusted_device_cookie(request, response, bookmark_token)
    else:
        existing_device = current_trusted_device(request, db, user)
        if existing_device:
            revoke_trusted_device(db, existing_device)
        clear_trusted_device_cookie(request, response)
    return response


@app.get("/trusted-device/{token}", response_class=HTMLResponse)
def trusted_device_landing(token: str, request: Request, db: Annotated[Session, Depends(get_db)]):
    """Bookmarkable sign-in link for e-reader browsers that don't retain cookies."""
    if not token.startswith("dtd_"):
        return RedirectResponse("/login", 303)
    device = db.scalar(
        select(TrustedDevice).where(
            TrustedDevice.token_hash == token_digest(token), TrustedDevice.revoked_at.is_(None)
        )
    )
    user = db.get(User, device.user_id) if device else None
    if not device or not user or not user.is_active:
        return RedirectResponse("/login", 303)
    device.last_used_at = datetime.now(UTC)
    db.commit()
    request.session.clear()
    request.session["user_id"] = user.id
    request.session["remember_me"] = True
    if request.query_params.get("welcome") == "1":
        response = templates.TemplateResponse(
            request,
            "ereader/trusted_device_bookmark.html",
            {
                "user": user,
                "csrf": csrf(request),
                "bookmark_url": f"{settings.public_url}/trusted-device/{token}",
            },
        )
    else:
        response = RedirectResponse(safe_return_to(request.query_params.get("next"), "/"), 303)
    set_trusted_device_cookie(request, response, token)
    return response


@app.post("/logout")
def logout(
    request: Request, db: Annotated[Session, Depends(get_db)], form_csrf: Annotated[str, Form()]
):
    user = require_user(request, db)
    check_csrf(request, form_csrf)
    device = current_trusted_device(request, db, user)
    if device:
        revoke_trusted_device(db, device)
    db.add(AuditEvent(event="login", user_id=user.id, message="Logout"))
    db.commit()
    request.session.clear()
    response = RedirectResponse("/login", 303)
    clear_trusted_device_cookie(request, response)
    return response


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
    if is_ereader_request(request) and not (q or author or series or view):
        # The e-reader layout always shows one paginated section at a time
        # instead of the stacked dashboard the modern layout uses, so land on
        # "Recent" rather than the unfiltered (dashboard) branch below.
        view = "latest"
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
    page_size = 6 if is_ereader_request(request) else 24
    last_page = 1
    if filtered:
        result_count = db.scalar(select(func.count()).select_from(query.subquery())) or 0
        last_page = max(1, math.ceil(result_count / page_size))
        results = db.scalars(
            query.order_by(*book_order(selected_sort, selected_direction))
            .offset((page - 1) * page_size)
            .limit(page_size + 1)
        ).all()
        books = results[:page_size]
        has_next = len(results) > page_size
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
    directory_page_size = 24 if is_ereader_request(request) else 48
    if directory_view == "authors":
        author_results = db.execute(
            author_query.offset((page - 1) * directory_page_size).limit(directory_page_size + 1)
        ).all()
        authors = author_results[:directory_page_size]
        series_rows = []
        has_next = len(author_results) > directory_page_size
    elif directory_view == "series":
        series_results = db.execute(
            series_query.offset((page - 1) * directory_page_size).limit(directory_page_size + 1)
        ).all()
        series_rows = series_results[:directory_page_size]
        authors = []
        has_next = len(series_results) > directory_page_size
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
            "last_page": last_page,
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
            selected_genre = (
                hardcover_genre_query_label(api_key, GENRES[genre_slug])
                if genre else ""
            )
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
                bestseller_weeks = [
                    {"date": "current", "title": "Current"},
                    *nyt_weeks(selected),
                ]
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
        genre_label = hardcover_genre_query_label(config["hardcover_api_key"], GENRES[genre_slug])
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
            "week": week,
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
    return {"title": item["title"], "weeks": [{"date": "current", "title": "Current"}, *nyt_weeks(item)]}


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
            "metadata_all_locked": all_metadata_locked(book),
        },
        user,
    )


@app.post("/books/{book_id}/metadata-lock")
def update_book_metadata_lock(
    book_id: str,
    request: Request,
    db: Annotated[Session, Depends(get_db)],
    form_csrf: Annotated[str, Form()],
    return_to: Annotated[str | None, Form()] = None,
    lock_all_metadata: Annotated[str | None, Form()] = None,
):
    require_admin(request, db)
    check_csrf(request, form_csrf)
    book = db.get(Book, book_id)
    if not book:
        raise HTTPException(404)
    set_all_metadata_locked(book, lock_all_metadata is not None)
    db.commit()
    return RedirectResponse(safe_return_to(return_to, f"/books/{book.id}"), 303)


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


@app.get("/review/grid", response_class=HTMLResponse)
def metadata_grid(
    request: Request,
    db: Annotated[Session, Depends(get_db)],
    q: str = "",
    page: int = 1,
):
    user = require_admin(request, db)
    page = max(page, 1)
    query = select(Book).where(Book.review_state == ReviewState.READY)
    if q.strip():
        term = f"%{q.strip()}%"
        query = query.where(
            or_(Book.title.ilike(term), Book.primary_author.ilike(term), Book.series.ilike(term))
        )
    results = db.scalars(
        query.order_by(func.lower(Book.primary_author), func.lower(Book.title))
        .offset((page - 1) * 100)
        .limit(101)
    ).all()
    return render(
        request,
        "metadata_grid.html",
        {
            "books": results[:100],
            "q": q.strip(),
            "page": page,
            "has_next": len(results) > 100,
            "metadata_lock_all": {book.id: all_metadata_locked(book) for book in results[:100]},
        },
        user,
    )


@app.post("/review/grid")
def update_metadata_grid(
    request: Request,
    db: Annotated[Session, Depends(get_db)],
    form_csrf: Annotated[str, Form()],
    book_ids: Annotated[list[str], Form()],
    titles: Annotated[list[str], Form()],
    authors: Annotated[list[str], Form()],
    series_values: Annotated[list[str], Form()],
    series_numbers: Annotated[list[str], Form()],
    return_to: Annotated[str | None, Form()] = None,
    lock_all_metadata_ids: Annotated[list[str] | None, Form()] = None,
):
    require_admin(request, db)
    check_csrf(request, form_csrf)
    books = {book.id: book for book in db.scalars(select(Book).where(Book.id.in_(book_ids)))}
    updates: list[tuple[Book, str, list[str], str | None, float | None]] = []
    locked_book_ids = set(lock_all_metadata_ids or [])
    try:
        rows = list(zip(book_ids, titles, authors, series_values, series_numbers, strict=True))
        if not rows:
            raise ValueError("No books were submitted.")
        for book_id, title, author_value, series, number in rows:
            book = books.get(book_id)
            if not book:
                raise ValueError("A selected book no longer exists.")
            author_list = [value.strip() for value in author_value.split(",") if value.strip()]
            if not title.strip() or not author_list:
                raise ValueError("Every book must retain a title and at least one author.")
            series_number = float(number) if number.strip() else None
            updates.append(
                (book, title.strip(), author_list, series.strip() or None, series_number)
            )
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc
    for book, title, author_list, series, series_number in updates:
        book.title = title
        book.sort_title = title.casefold()
        book.primary_author = author_list[0]
        book.authors_json = json.dumps(author_list)
        book.series = series
        book.series_number = series_number
        set_all_metadata_locked(book, book.id in locked_book_ids)
        book.metadata_source = "manual"
        book.match_confidence = 1
    db.commit()
    for book, *_ in updates:
        organise_book(db, book)
    destination = safe_return_to(return_to, "/review/grid")
    separator = "&" if "?" in destination else "?"
    return RedirectResponse(f"{destination}{separator}saved={len(updates)}", 303)


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
    page: int = 1,
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
    page = max(page, 1)
    ordered_query = query.order_by(*book_order(selected_sort, selected_direction), Book.id)
    total = db.scalar(select(func.count()).select_from(query.subquery())) or 0
    page_size = 6 if is_ereader_request(request) else 24
    if is_ereader_request(request):
        results = list(db.scalars(ordered_query.offset((page - 1) * page_size).limit(page_size + 1)))
        books, has_next = results[:page_size], len(results) > page_size
    else:
        books, has_next = list(db.scalars(ordered_query)), False
    return_to = f"/shelves/{shelf.id}?{urlencode({'sort': selected_sort, 'direction': selected_direction, 'metadata': metadata, 'page': page})}"
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
            "page": page,
            "last_page": max(1, math.ceil(total / page_size)),
            "has_next": has_next,
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
        settings_context(request, db, user),
        user,
    )


@app.post("/settings/profile")
def profile_settings(
    request: Request,
    db: Annotated[Session, Depends(get_db)],
    form_csrf: Annotated[str, Form()],
    kindle_email: Annotated[str | None, Form()] = None,
    kobo_sync_shelf_id: Annotated[str | None, Form()] = None,
):
    user = require_user(request, db)
    check_csrf(request, form_csrf)
    user.kindle_email = (kindle_email or "").strip() or None
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
        settings_context(request, db, user, kobo_configured=True, kobo_endpoint=endpoint),
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


@app.post("/settings/trusted-devices/{device_id}/revoke")
def revoke_trusted_device_route(
    device_id: int,
    request: Request,
    db: Annotated[Session, Depends(get_db)],
    form_csrf: Annotated[str, Form()],
):
    user = require_user(request, db)
    check_csrf(request, form_csrf)
    device = db.get(TrustedDevice, device_id)
    if not device or device.user_id != user.id:
        raise HTTPException(404, "Trusted device not found")
    revoke_trusted_device(db, device)
    db.add(
        AuditEvent(
            event="trusted_device_revoked",
            user_id=user.id,
            message=f"Revoked trusted device {device.id}",
        )
    )
    db.commit()
    response = RedirectResponse("/settings", 303)
    current = request.cookies.get(TRUSTED_DEVICE_COOKIE, "")
    if current and token_digest(current) == device.token_hash:
        clear_trusted_device_cookie(request, response)
    return response


@app.get("/kobo/{token}/v1/initialization")
def kobo_initialize(token: str, request: Request, db: Annotated[Session, Depends(get_db)]):
    kobo_user(db, token)
    proxy_headers = {
        name: value
        for name, value in request.headers.items()
        if name in {"authorization", "user-agent", "accept", "accept-language"}
        or (name.startswith("x-kobo-") and name != "x-kobo-synctoken")
    }
    upstream_resources = None
    try:
        upstream = httpx.get(
            "https://storeapi.kobo.com/v1/initialization",
            headers=proxy_headers,
            params=request.query_params,
            timeout=20,
        )
        upstream.raise_for_status()
        upstream_body = upstream.json()
        if isinstance(upstream_body, dict) and isinstance(
            upstream_body.get("Resources"), dict
        ):
            upstream_resources = upstream_body["Resources"]
    except (httpx.HTTPError, ValueError):
        pass
    response = JSONResponse(
        kobo_initialization(settings.public_url, token, upstream_resources)
    )
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
    response = JSONResponse(sync_payload(db, user, settings.public_url.rstrip("/"), token))
    pending = sync_pending(db, user)
    response.headers["x-kobo-sync"] = "continue" if pending else ""
    response.headers["x-kobo-synctoken"] = sync_token(
        request.headers.get("x-kobo-synctoken")
    )
    return response


@app.get("/kobo/{token}/v1/library/{book_id}/metadata")
def kobo_book_metadata(
    token: str, book_id: str, request: Request, db: Annotated[Session, Depends(get_db)]
):
    user = kobo_user(db, token)
    book = kobo_shelf_book(db, user, book_id)
    return [kobo_metadata(book, settings.public_url.rstrip("/"), token)]


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
    try:
        shelf = shelf_for_tag(db, user, tag_id)
    except HTTPException as exc:
        if exc.status_code != 404:
            raise
        # Old sync servers leave collection operations queued on the device.
        # Treat their unknown identifiers as already removed/unchanged.
        return Response(status_code=204 if request.method == "DELETE" else 200)
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
        shelf = shelf_for_tag(db, user, tag_id)
    except HTTPException as exc:
        if exc.status_code == 404:
            return Response(status_code=201)
        raise
    try:
        add_tag_items(db, user, shelf, payload)
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
        shelf = shelf_for_tag(db, user, tag_id)
    except HTTPException as exc:
        if exc.status_code == 404:
            return Response(status_code=200)
        raise
    try:
        remove_tag_items(db, user, shelf, payload)
    except (TypeError, ValueError) as exc:
        raise HTTPException(400, str(exc)) from exc
    return Response(status_code=200)


@app.get("/kobo/{token}/download/{book_id}")
@app.get("/kobo/{token}/download/{book_id}/{book_format}")
@app.get("/kobo/{token}/{book_id}/{book_format}")
@app.get("/kobo/{token}/v1/books/{book_id}/download")
def kobo_download(
    token: str,
    book_id: str,
    db: Annotated[Session, Depends(get_db)],
    book_format: str = "",
):
    user = kobo_user(db, token)
    item = preferred_file(kobo_shelf_book(db, user, book_id))
    if not item:
        raise HTTPException(404, "No Kobo-compatible file")
    if book_format and book_format.casefold() != item.format.casefold():
        raise HTTPException(404, "Kobo file format not found")
    return FileResponse(
        item.path,
        filename=Path(item.path).name,
        media_type="application/octet-stream",
    )


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


def _require_opds_user(request: Request, db: Session) -> User:
    try:
        return require_user(request, db)
    except HTTPException as exc:
        if exc.status_code == 401:
            raise HTTPException(
                401,
                "Use an API token as the password",
                headers={"WWW-Authenticate": 'Basic realm="Digest OPDS"'},
            ) from exc
        raise


def _opds_feed(title: str, feed_id: str) -> Element:
    feed = Element(f"{{{ATOM_NS}}}feed")
    SubElement(feed, "title").text = title
    SubElement(feed, "id").text = feed_id
    SubElement(feed, "updated").text = datetime.now(UTC).isoformat()
    return feed


def _opds_response(feed: Element) -> Response:
    headers = _opds_headers()
    return Response(
        tostring(feed, encoding="utf-8", xml_declaration=True),
        media_type="application/atom+xml",
        headers=headers,
    )


def _opensearch_response(description: Element) -> Response:
    return Response(
        tostring(description, encoding="utf-8", xml_declaration=True),
        media_type="application/opensearchdescription+xml",
        headers=_opds_headers(),
    )


def _opds_headers() -> dict[str, str]:
    return {
        "Cache-Control": "no-store, max-age=0",
        "Pragma": "no-cache",
        "Last-Modified": format_datetime(datetime.now(UTC), usegmt=True),
    }


def _wants_opds_json(request: Request) -> bool:
    return "application/opds+json" in request.headers.get("accept", "")


def _opds_json_response(data: dict) -> JSONResponse:
    return JSONResponse(data, media_type="application/opds+json", headers=_opds_headers())


def _opds_json_link(title: str, href: str, count: int | None = None) -> dict:
    link = {
        "title": title,
        "rel": "subsection",
        "href": _opds_href(href),
        "type": "application/opds+json",
    }
    if count is not None:
        link["numberOfItems"] = count
        link["properties"] = {"numberOfItems": count}
    return link


def _opds_href(path_or_url: str | None) -> str:
    value = path_or_url or ""
    if value.startswith(("http://", "https://")):
        return value
    if not value.startswith("/"):
        value = "/" + value
    return value


def _book_authors(book: Book) -> list[str]:
    try:
        authors = json.loads(book.authors_json or "[]")
    except (TypeError, ValueError):
        authors = []
    return [str(author) for author in (authors or [book.primary_author]) if str(author)]


def _book_isbns(book: Book) -> list[str]:
    try:
        isbns = json.loads(book.isbns_json or "[]")
    except (TypeError, ValueError):
        isbns = []
    return [str(isbn) for isbn in isbns if str(isbn)]


def _opds_json_publication(book: Book) -> dict:
    authors = _book_authors(book)
    metadata: dict = {
        "title": book.title,
        "author": ", ".join(authors),
        "identifier": book.id,
    }
    if book.description:
        metadata["description"] = plain_text(book.description)
    if book.language:
        metadata["language"] = book.language
    if book.publication_date:
        metadata["published"] = book.publication_date
    if book.series:
        metadata["belongsTo"] = {
            "series": [
                {
                    "name": book.series,
                    **({"position": book.series_number} if book.series_number is not None else {}),
                }
            ]
        }
    isbns = _book_isbns(book)
    if isbns:
        metadata["identifier"] = [book.id, *[f"urn:isbn:{isbn}" for isbn in isbns]]
    publication = {
        "metadata": metadata,
        "links": [
            {
                "rel": "http://opds-spec.org/acquisition",
                "href": _opds_href(f"/books/{book.id}/file/{item.id}"),
                "type": OPDS_MEDIA_TYPES.get(item.format, "application/octet-stream"),
                "title": item.format.upper(),
                "properties": {"numberOfBytes": item.size_bytes},
            }
            for item in book.files
        ],
    }
    if book.cover_path:
        publication["images"] = [
            {
                "rel": "http://opds-spec.org/image",
                "href": _opds_href(f"/books/{book.id}/cover"),
                "type": "image/jpeg",
            },
            {
                "rel": "http://opds-spec.org/image/thumbnail",
                "href": _opds_href(f"/books/{book.id}/cover"),
                "type": "image/jpeg",
            },
        ]
    return publication


def _opds_json_virtual_publication(title: str, author: str = "", description: str = "") -> dict:
    metadata = {"title": title}
    if author:
        metadata["author"] = author
    if description:
        metadata["description"] = description
    return {"metadata": metadata, "links": []}


def _opds_discovery_request_url(values: dict) -> str:
    return "/opds/discover/request?" + urlencode(
        {
            "source": values.get("source") or "openlibrary",
            "source_id": values.get("source_id") or "",
            "title": values.get("title") or "",
            "author": values.get("author") or "",
            "isbn": values.get("isbn") or "",
            "cover_url": values.get("cover_url") or "",
        }
    )


def _opds_add_book_entry(feed: Element, book: Book) -> None:
    entry = SubElement(feed, "entry")
    SubElement(entry, "id").text = book.id
    SubElement(entry, "title").text = book.title
    SubElement(entry, "updated").text = book.updated_at.isoformat()
    SubElement(entry, f"{{{DC_NS}}}identifier").text = book.id
    try:
        authors = json.loads(book.authors_json or "[]")
    except (TypeError, ValueError):
        authors = []
    authors = authors or [book.primary_author]
    for name in authors:
        author = SubElement(entry, "author")
        SubElement(author, "name").text = str(name)
        SubElement(entry, f"{{{DC_NS}}}creator").text = str(name)
    if book.publication_date:
        SubElement(entry, "published").text = book.publication_date
        SubElement(entry, f"{{{DC_NS}}}issued").text = book.publication_date
    if book.language:
        SubElement(entry, f"{{{DC_NS}}}language").text = book.language
    if book.series:
        category = SubElement(entry, "category", term=book.series, label=book.series)
        category.set(f"{{{OPDS_NS}}}facetGroup", "Series")
        if book.series_number is not None:
            SubElement(entry, f"{{{DC_NS}}}extent").text = f"Series #{book.series_number:g}"
    try:
        isbns = json.loads(book.isbns_json or "[]")
    except (TypeError, ValueError):
        isbns = []
    for isbn in isbns:
        SubElement(entry, f"{{{DC_NS}}}identifier").text = f"urn:isbn:{isbn}"
    if book.description:
        description = plain_text(book.description)
        SubElement(entry, "summary", type="text").text = description
        SubElement(entry, "content", type="text").text = description
    if book.cover_path:
        href = _opds_href(f"/books/{book.id}/cover")
        SubElement(entry, "link", rel="http://opds-spec.org/image", href=href, type="image/jpeg")
        SubElement(
            entry,
            "link",
            rel="http://opds-spec.org/image/thumbnail",
            href=href,
            type="image/jpeg",
        )
    for item in book.files:
        SubElement(
            entry,
            "link",
            rel="http://opds-spec.org/acquisition",
            href=_opds_href(f"/books/{book.id}/file/{item.id}"),
            type=OPDS_MEDIA_TYPES.get(item.format, "application/octet-stream"),
            title=item.format.upper(),
            length=str(item.size_bytes),
        )


def _opds_add_navigation_entry(feed: Element, title: str, href: str, entry_id: str = "") -> None:
    entry = SubElement(feed, "entry")
    SubElement(entry, "id").text = entry_id or href
    SubElement(entry, "title").text = title
    SubElement(entry, "updated").text = datetime.now(UTC).isoformat()
    SubElement(
        entry,
        "link",
        rel="subsection",
        href=_opds_href(href),
        type="application/atom+xml;profile=opds-catalog",
    )


def _opds_navigation_response(
    request: Request,
    title: str,
    feed_id: str,
    navigation: list[dict],
    *,
    searchable: bool = False,
) -> Response | JSONResponse:
    if _wants_opds_json(request):
        links = [{"rel": "self", "href": _opds_href(feed_id), "type": "application/opds+json"}]
        if searchable:
            links.extend(_opds_discovery_search_links())
        return _opds_json_response(
            {
                "metadata": {"title": title, "numberOfItems": len(navigation)},
                "links": links,
                "navigation": navigation,
            }
        )
    feed = _opds_feed(title, feed_id)
    if searchable:
        _opds_add_search_link(feed)
    for item in navigation:
        _opds_add_navigation_entry(
            feed,
            item["title"],
            item["href"],
            entry_id=item.get("id", ""),
        )
    return _opds_response(feed)


def _opds_empty_response(request: Request, title: str, feed_id: str) -> Response | JSONResponse:
    if _wants_opds_json(request):
        return _opds_json_response(
            {
                "metadata": {"title": title, "numberOfItems": 0},
                "links": [{"rel": "self", "href": _opds_href(feed_id), "type": "application/opds+json"}],
                "publications": [],
            }
        )
    return _opds_response(_opds_feed(title, feed_id))


def _opds_books_response(
    request: Request, title: str, feed_id: str, books: list[Book]
) -> Response | JSONResponse:
    if _wants_opds_json(request):
        return _opds_json_response(
            {
                "metadata": {"title": title, "numberOfItems": len(books)},
                "links": [
                    {"rel": "self", "href": _opds_href(feed_id), "type": "application/opds+json"},
                    *_opds_library_search_links(),
                ],
                "publications": [_opds_json_publication(book) for book in books],
            }
        )
    feed = _opds_feed(title, feed_id)
    _opds_add_library_search_link(feed)
    for book in books:
        _opds_add_book_entry(feed, book)
    return _opds_response(feed)


def _discovery_item_values(item) -> dict:
    if isinstance(item, Book):
        try:
            isbns = json.loads(item.isbns_json or "[]")
        except (TypeError, ValueError):
            isbns = []
        return {
            "title": item.title,
            "author": item.primary_author,
            "authors": [item.primary_author],
            "isbn": isbns[0] if isbns else "",
            "cover_url": f"/books/{item.id}/cover" if item.cover_path else "",
            "description": item.description or "",
            "source": "digest",
            "source_id": item.id,
            "published": item.publication_date or "",
            "language": item.language or "",
        }
    if isinstance(item, dict):
        authors = item.get("authors") or []
        author = item.get("author") or (authors[0] if authors else "")
        return {
            "title": str(item.get("title") or ""),
            "author": str(author or ""),
            "authors": authors,
            "isbn": str(item.get("isbn") or ""),
            "cover_url": str(item.get("cover_url") or ""),
            "description": str(item.get("description") or ""),
            "source": str(item.get("source") or ""),
            "source_id": str(item.get("source_id") or ""),
            "published": str(item.get("published") or item.get("published_year") or ""),
            "language": str(item.get("language") or ""),
        }
    try:
        authors = json.loads(item.authors_json or "[]")
    except (TypeError, ValueError):
        authors = []
    return {
        "title": item.title,
        "author": authors[0] if authors else "",
        "authors": authors,
        "isbn": "",
        "cover_url": item.cover_url or "",
        "description": "",
        "source": item.provider,
        "source_id": item.source_id,
        "published": item.publication_date or "",
        "language": "",
    }


def _opds_add_discovery_entry(feed: Element, db: Session, item) -> None:
    if isinstance(item, Book):
        return
    values = _discovery_item_values(item)
    title = values["title"] or "Untitled"
    author_name = values["author"]
    owned = find_library_book(db, title=title, author=author_name, isbn=values["isbn"])
    if owned is not None:
        return

    entry = SubElement(feed, "entry")
    SubElement(entry, "id").text = ":".join(
        part for part in ("digest", values["source"], values["source_id"], title) if part
    )
    SubElement(entry, "title").text = title
    SubElement(entry, "updated").text = datetime.now(UTC).isoformat()
    for name in values["authors"] or [author_name]:
        author = SubElement(entry, "author")
        SubElement(author, "name").text = str(name)
        SubElement(entry, f"{{{DC_NS}}}creator").text = str(name)
    if values["isbn"]:
        SubElement(entry, f"{{{DC_NS}}}identifier").text = f"urn:isbn:{values['isbn']}"
    if values["source_id"]:
        SubElement(entry, f"{{{DC_NS}}}identifier").text = values["source_id"]
    if values["published"]:
        SubElement(entry, "published").text = values["published"]
        SubElement(entry, f"{{{DC_NS}}}issued").text = values["published"]
    if values["language"]:
        SubElement(entry, f"{{{DC_NS}}}language").text = values["language"]
    if values["description"]:
        description = plain_text(values["description"])
        SubElement(entry, "summary", type="text").text = description
        SubElement(entry, "content", type="text").text = description
    if values["cover_url"]:
        href = _opds_href(values["cover_url"])
        SubElement(entry, "link", rel="http://opds-spec.org/image", href=href, type="image/jpeg")
        SubElement(
            entry,
            "link",
            rel="http://opds-spec.org/image/thumbnail",
            href=href,
            type="image/jpeg",
        )
    SubElement(
        entry,
        "link",
        rel="alternate",
        href=_opds_href(discovery_book_url(values)),
        type="text/html",
    )
    SubElement(
        entry,
        "link",
        rel="http://opds-spec.org/acquisition",
        href=_opds_discovery_request_url(values),
        type="text/plain",
        title="Request download",
    )


def _opds_json_discovery_publication(db: Session, item) -> dict:
    if isinstance(item, Book):
        return {}
    values = _discovery_item_values(item)
    title = values["title"] or "Untitled"
    author_name = values["author"]
    owned = find_library_book(db, title=title, author=author_name, isbn=values["isbn"])
    if owned is not None:
        return {}
    metadata: dict = {
        "title": title,
        "author": ", ".join(str(name) for name in (values["authors"] or [author_name]) if str(name)),
    }
    identifiers = []
    if values["isbn"]:
        identifiers.append(f"urn:isbn:{values['isbn']}")
    if values["source_id"]:
        identifiers.append(values["source_id"])
    if identifiers:
        metadata["identifier"] = identifiers[0] if len(identifiers) == 1 else identifiers
    if values["description"]:
        metadata["description"] = plain_text(values["description"])
    if values["published"]:
        metadata["published"] = values["published"]
    if values["language"]:
        metadata["language"] = values["language"]
    publication = {
        "metadata": metadata,
        "links": [
            {
                "rel": "alternate",
                "href": _opds_href(discovery_book_url(values)),
                "type": "text/html",
                "title": "View in Digest",
            },
            {
                "rel": "http://opds-spec.org/acquisition",
                "href": _opds_discovery_request_url(values),
                "type": "text/plain",
                "title": "Request download",
            }
        ],
    }
    if values["cover_url"]:
        publication["images"] = [
            {
                "rel": "http://opds-spec.org/image",
                "href": _opds_href(values["cover_url"]),
                "type": "image/jpeg",
            },
            {
                "rel": "http://opds-spec.org/image/thumbnail",
                "href": _opds_href(values["cover_url"]),
                "type": "image/jpeg",
            },
        ]
    return publication


def _opds_available_discovery_items(db: Session, items) -> list:
    available = []
    owned_count = 0
    for item in items:
        if isinstance(item, Book):
            continue
        values = _discovery_item_values(item)
        if find_library_book(
            db,
            title=values["title"],
            author=values["author"],
            isbn=values["isbn"],
        ) is None:
            available.append(item)
        else:
            owned_count += 1
    source_count = len(items or [])
    if source_count and not available:
        logger.warning(
            "OPDS discovery availability filtered all items source_items=%d owned_filtered=%d",
            source_count,
            owned_count,
        )
    else:
        logger.info(
            "OPDS discovery availability source_items=%d available=%d owned_filtered=%d",
            source_count,
            len(available),
            owned_count,
        )
    return available


def _opds_dedupe_discovery_items(items) -> list:
    seen: set[tuple[str, str, str, str]] = set()
    results = []
    for item in items:
        values = _discovery_item_values(item)
        key = (
            values["source"].casefold(),
            values["source_id"].casefold(),
            values["title"].casefold(),
            values["author"].casefold(),
        )
        if key in seen:
            continue
        seen.add(key)
        results.append(item)
    return results


def _opds_genre_navigation(base_href: str) -> list[dict]:
    return [
        _opds_json_link(
            GENRES.get(genre_slug, genre_slug.replace("_", " ").title()),
            f"{base_href}/{quote(genre_slug, safe='')}",
        )
        for genre_slug in OPDS_DISCOVER_GENRES
    ]


def _opds_period_navigation(base_href: str, periods: dict[str, tuple[str, int | None]]) -> list[dict]:
    return [
        _opds_json_link(label, f"{base_href}/{quote(key, safe='')}")
        for key, (label, _) in periods.items()
    ]


def _opds_wanted_status_publication(item: WantedItem) -> dict:
    status = item.status.value if hasattr(item.status, "value") else str(item.status)
    metadata = {
        "title": item.title,
        "author": item.author or "",
        "subtitle": f"Download status: {status}",
        "identifier": f"digest:wanted:{item.id}",
    }
    if item.isbn:
        metadata["identifier"] = [metadata["identifier"], f"urn:isbn:{item.isbn}"]
    publication = {
        "metadata": metadata,
        "links": [
            {
                "rel": "alternate",
                "href": "/wanted",
                "type": "text/html",
                "title": "View download queue",
            }
        ],
    }
    if item.cover_url:
        publication["images"] = [
            {
                "rel": "http://opds-spec.org/image",
                "href": _opds_href(item.cover_url),
                "type": "image/jpeg",
            },
            {
                "rel": "http://opds-spec.org/image/thumbnail",
                "href": _opds_href(item.cover_url),
                "type": "image/jpeg",
            },
        ]
    return publication


def _opds_wanted_payload(db: Session, item: WantedItem) -> dict:
    releases = list(
        db.scalars(
            select(AcquisitionRelease)
            .where(AcquisitionRelease.wanted_id == item.id)
            .order_by(AcquisitionRelease.match_score.desc())
        )
    )
    return {
        "id": item.id,
        "title": item.title,
        "author": item.author or "",
        "status": item.status.value if hasattr(item.status, "value") else str(item.status),
        "last_error": item.last_error or "",
        "acquired_book_id": item.acquired_book_id,
        "selected_release_id": item.selected_release_id,
        "releases": [
            {
                "id": release.id,
                "title": release.title,
                "format": release.format,
                "size_bytes": release.size_bytes,
                "score": release.match_score,
            }
            for release in releases
        ],
    }


def _opds_review_books(db: Session) -> list[Book]:
    return list(
        db.scalars(
            select(Book)
            .where(Book.review_state.in_([ReviewState.REVIEW, ReviewState.REJECTED, ReviewState.ERROR]))
            .order_by(Book.created_at.desc(), Book.id)
        )
    )


def _opds_review_payload(book: Book) -> dict:
    return {
        "id": book.id,
        "title": book.title,
        "author": book.primary_author,
        "status": book.review_state.value if hasattr(book.review_state, "value") else str(book.review_state),
        "reason": book.review_reason or "",
    }


def _require_opds_admin(request: Request, db: Session) -> User:
    user = _require_opds_user(request, db)
    if user.role != Role.ADMIN:
        raise HTTPException(403)
    return user


def _opds_add_download_status_entry(feed: Element, db: Session, item: WantedItem) -> None:
    entry = SubElement(feed, "entry")
    status = item.status.value if hasattr(item.status, "value") else str(item.status)
    SubElement(entry, "id").text = f"digest:wanted:{item.id}"
    SubElement(entry, "title").text = item.title
    SubElement(entry, "updated").text = item.updated_at.isoformat()
    if item.author:
        author = SubElement(entry, "author")
        SubElement(author, "name").text = item.author
    summary = f"Download status: {status}"
    if item.last_error:
        summary += f"\n\n{item.last_error}"
    book = db.get(Book, item.acquired_book_id) if item.acquired_book_id else None
    if book is not None and book.review_state != ReviewState.READY:
        summary += "\n\nMetadata review required."
        SubElement(
            entry,
            "link",
            rel="subsection",
            href=f"/opds/downloads/review/{book.id}",
            type="application/atom+xml;profile=opds-catalog",
            title="Review metadata",
        )
    SubElement(entry, "summary", type="text").text = summary
    SubElement(entry, "link", rel="alternate", href="/wanted", type="text/html")


def _opds_json_download_status_publication(db: Session, item: WantedItem) -> dict:
    payload = _opds_wanted_payload(db, item)
    metadata = {
        "title": payload["title"],
        "author": payload["author"],
        "subtitle": f"Download status: {payload['status']}",
        "identifier": f"digest:wanted:{item.id}",
    }
    if payload["last_error"]:
        metadata["description"] = payload["last_error"]
    links = [{"rel": "alternate", "href": "/wanted", "type": "text/html", "title": "View downloads"}]
    book = db.get(Book, item.acquired_book_id) if item.acquired_book_id else None
    if book is not None and book.review_state != ReviewState.READY:
        metadata["description"] = (metadata.get("description", "") + "\n\nMetadata review required.").strip()
        links.append(
            {
                "rel": "subsection",
                "href": _opds_href(f"/opds/downloads/review/{book.id}"),
                "type": "application/opds+json",
                "title": "Review metadata",
            }
        )
    return {"metadata": metadata, "links": links}


def _opds_add_review_entry(feed: Element, book: Book) -> None:
    entry = SubElement(feed, "entry")
    SubElement(entry, "id").text = f"digest:review:{book.id}"
    SubElement(entry, "title").text = book.title
    SubElement(entry, "updated").text = book.updated_at.isoformat()
    if book.primary_author:
        author = SubElement(entry, "author")
        SubElement(author, "name").text = book.primary_author
    SubElement(entry, "summary", type="text").text = (
        f"Metadata review: {book.review_state.value}\n\n{book.review_reason or ''}".strip()
    )
    SubElement(
        entry,
        "link",
        rel="subsection",
        href=f"/opds/downloads/review/{book.id}",
        type="application/atom+xml;profile=opds-catalog",
        title="Review metadata",
    )


def _opds_json_action_response(title: str, message: str) -> JSONResponse:
    return _opds_json_response(
        {
            "metadata": {"title": title, "numberOfItems": 1},
            "navigation": [_opds_json_link(message, "/opds/downloads")],
        }
    )


def _opds_json_review_publication(book: Book) -> dict:
    return {
        "metadata": {
            "title": book.title,
            "author": book.primary_author,
            "subtitle": f"Metadata review: {book.review_state.value}",
            "identifier": f"digest:review:{book.id}",
            "description": book.review_reason or "",
        },
        "links": [
            {
                "rel": "subsection",
                "href": _opds_href(f"/opds/downloads/review/{book.id}"),
                "type": "application/opds+json",
                "title": "Review metadata",
            }
        ],
    }


def _opds_add_wanted_status_entry(feed: Element, item: WantedItem) -> None:
    status = item.status.value if hasattr(item.status, "value") else str(item.status)
    entry = SubElement(feed, "entry")
    SubElement(entry, "id").text = f"digest:wanted:{item.id}"
    SubElement(entry, "title").text = item.title
    SubElement(entry, "updated").text = item.updated_at.isoformat()
    if item.author:
        author = SubElement(entry, "author")
        SubElement(author, "name").text = item.author
        SubElement(entry, f"{{{DC_NS}}}creator").text = item.author
    if item.isbn:
        SubElement(entry, f"{{{DC_NS}}}identifier").text = f"urn:isbn:{item.isbn}"
    SubElement(entry, "summary", type="text").text = f"Download status: {status}"
    if item.cover_url:
        href = _opds_href(item.cover_url)
        SubElement(entry, "link", rel="http://opds-spec.org/image", href=href, type="image/jpeg")
        SubElement(
            entry,
            "link",
            rel="http://opds-spec.org/image/thumbnail",
            href=href,
            type="image/jpeg",
        )
    SubElement(entry, "link", rel="alternate", href="/wanted", type="text/html")


def _opds_search_status_response(
    request: Request,
    db: Session,
    user: User,
    title: str,
    feed_id: str,
    search_results=None,
) -> Response | JSONResponse:
    wanted_items = list(
        db.scalars(
            select(WantedItem)
            .where(
                WantedItem.user_id == user.id,
                WantedItem.status != WantedStatus.CANCELLED,
            )
            .order_by(WantedItem.updated_at.desc(), WantedItem.id.desc())
            .limit(25)
        )
    )
    results = _opds_available_discovery_items(db, search_results or [])
    if _wants_opds_json(request):
        publications = [
            publication
            for publication in (_opds_json_discovery_publication(db, item) for item in results)
            if publication
        ]
        publications.extend(_opds_wanted_status_publication(item) for item in wanted_items)
        return _opds_json_response(
            {
                "metadata": {"title": title, "numberOfItems": len(publications)},
                "links": [
                    {"rel": "self", "href": _opds_href(feed_id), "type": "application/opds+json"},
                    *_opds_discovery_search_links(),
                ],
                "publications": publications,
            }
        )
    feed = _opds_feed(title, feed_id)
    _opds_add_search_link(feed)
    for item in results:
        _opds_add_discovery_entry(feed, db, item)
    for item in wanted_items:
        _opds_add_wanted_status_entry(feed, item)
    return _opds_response(feed)


def _opds_discovery_search_links() -> list[dict]:
    return [
        {
            "rel": "search",
            "href": "/opds/discover/search?query={searchTerms}",
            "type": "application/opds+json",
            "templated": True,
        }
    ]


def _opds_download_status_links() -> list[dict]:
    return [
        {
            "rel": "subsection",
            "href": "/opds/downloads",
            "type": "application/opds+json",
            "title": "Download Status",
        }
    ]


def _opds_library_search_links() -> list[dict]:
    return [
        {
            "rel": "search",
            "href": "/opds/search?query={searchTerms}",
            "type": "application/opds+json",
            "templated": True,
        }
    ]


def _opds_add_search_link(feed: Element) -> None:
    SubElement(
        feed,
        "link",
        rel="search",
        href="/opds/discover/search.xml",
        type="application/opensearchdescription+xml",
        title="Digest Discover Search",
    )
    SubElement(
        feed,
        "link",
        rel="search",
        href="/opds/discover/search?query={searchTerms}",
        type="application/atom+xml;profile=opds-catalog",
    )


def _opds_add_library_search_link(feed: Element) -> None:
    SubElement(
        feed,
        "link",
        rel="search",
        href="/opds/search.xml",
        type="application/opensearchdescription+xml",
        title="Digest Library Search",
    )
    SubElement(
        feed,
        "link",
        rel="search",
        href="/opds/search?query={searchTerms}",
        type="application/atom+xml;profile=opds-catalog",
    )


def _opds_discovery_response(
    request: Request, db: Session, title: str, feed_id: str, items
) -> Response | JSONResponse:
    items = _opds_available_discovery_items(db, items)
    if _wants_opds_json(request):
        return _opds_json_response(
            {
                "metadata": {"title": title, "numberOfItems": len(items)},
                "links": [
                    {"rel": "self", "href": _opds_href(feed_id), "type": "application/opds+json"},
                    *_opds_discovery_search_links(),
                ],
                "publications": [
                    publication
                    for publication in (_opds_json_discovery_publication(db, item) for item in items)
                    if publication
                ],
            }
        )
    feed = _opds_feed(title, feed_id)
    _opds_add_search_link(feed)
    for item in items:
        _opds_add_discovery_entry(feed, db, item)
    return _opds_response(feed)


@app.get("/opds/", include_in_schema=False)
@app.get("/opds")
def opds(
    request: Request,
    db: Annotated[Session, Depends(get_db)],
    catalog: str = "",
    author: str = "",
    series: str = "",
    genre: str = "fantasy",
    period: str = "now",
    q: str = "",
    query: str = "",
):
    if catalog == "title":
        return opds_title(request, db)
    if catalog == "latest":
        return opds_latest(request, db)
    if catalog == "authors":
        return opds_authors(request, db)
    if catalog == "author":
        return opds_author(request, db, author)
    if catalog == "series":
        return opds_series(request, db)
    if catalog == "series-books":
        return opds_series_books(request, db, series)
    if catalog == "discover":
        return opds_discover(request, db)
    if catalog == "discover-trending":
        return opds_discover_trending(request, db, period=period, genre=genre)
    if catalog == "discover-new-releases":
        return opds_discover_new_releases(request, db, genre=genre)
    if catalog == "discover-search":
        return opds_discover_search(request, db, q=q, query=query)
    if catalog == "search":
        return opds_search(request, db, q=q, query=query)

    _require_opds_user(request, db)
    navigation = [
        _opds_json_link("By Title", "/opds/catalog/title"),
        _opds_json_link("By Author", "/opds/catalog/authors-v2"),
        _opds_json_link("Latest", "/opds/catalog/latest"),
        _opds_json_link("By Series", "/opds/catalog/series"),
        _opds_json_link("Discover", "/opds/discover"),
        _opds_json_link("Downloads", "/opds/downloads"),
    ]
    if _wants_opds_json(request):
        return _opds_json_response(
            {
                "metadata": {"title": "Digest Library Catalog"},
                "links": [{"rel": "self", "href": "/opds", "type": "application/opds+json"}],
                "navigation": navigation,
            }
        )
    feed = _opds_feed("Digest Library Catalog", settings.public_url + "/opds")
    _opds_add_navigation_entry(feed, "By Title", "/opds/catalog/title")
    _opds_add_navigation_entry(feed, "By Author", "/opds/catalog/authors-v2")
    _opds_add_navigation_entry(feed, "Latest", "/opds/catalog/latest")
    _opds_add_navigation_entry(feed, "By Series", "/opds/catalog/series")
    _opds_add_navigation_entry(feed, "Discover", "/opds/discover")
    _opds_add_navigation_entry(feed, "Downloads", "/opds/downloads")
    return _opds_response(feed)


@app.get("/opds/search/", include_in_schema=False)
@app.get("/opds/search")
def opds_search(
    request: Request,
    db: Annotated[Session, Depends(get_db)],
    q: str = "",
    query: str = "",
):
    _require_opds_user(request, db)
    search_query = (q or query).strip()
    if not search_query:
        return _opds_books_response(
            request,
            "Digest Library - Search",
            settings.public_url + "/opds/search",
            [],
        )
    like = f"%{search_query.casefold()}%"
    books = list(
        db.scalars(
            select(Book)
            .where(
                Book.review_state == ReviewState.READY,
                or_(
                    func.lower(Book.title).like(like),
                    func.lower(Book.sort_title).like(like),
                    func.lower(Book.primary_author).like(like),
                    func.lower(Book.series).like(like),
                ),
            )
            .order_by(func.lower(Book.sort_title), func.lower(Book.title), Book.id)
        )
    )
    return _opds_books_response(
        request,
        f"Digest Library - Search: {search_query}",
        settings.public_url + f"/opds/search?{urlencode({'query': search_query})}",
        books,
    )


@app.get("/opds/search.xml", include_in_schema=False)
def opds_search_descriptor(request: Request, db: Annotated[Session, Depends(get_db)]):
    _require_opds_user(request, db)
    description = Element(f"{{{OS_NS}}}OpenSearchDescription")
    SubElement(description, "ShortName").text = "Digest Library"
    SubElement(description, "Description").text = "Search Digest Library by title or author"
    SubElement(
        description,
        "Url",
        type="application/atom+xml;profile=opds-catalog;kind=acquisition",
        template=_opds_href("/opds/search?query={searchTerms}"),
    )
    SubElement(
        description,
        "Url",
        type="application/opds+json",
        template=_opds_href("/opds/search?query={searchTerms}"),
    )
    return _opensearch_response(description)


@app.get("/opds/catalog/title/", include_in_schema=False)
@app.get("/opds/catalog/title")
@app.get("/opds/title/", include_in_schema=False)
@app.get("/opds/title")
def opds_title(request: Request, db: Annotated[Session, Depends(get_db)]):
    _require_opds_user(request, db)
    books = list(
        db.scalars(
            select(Book)
            .where(Book.review_state == ReviewState.READY)
            .order_by(func.lower(Book.sort_title), func.lower(Book.title), Book.id)
        )
    )
    return _opds_books_response(
        request, "Digest Library - By Title", settings.public_url + "/opds/title", books
    )


@app.get("/opds/catalog/latest/", include_in_schema=False)
@app.get("/opds/catalog/latest")
@app.get("/opds/latest/", include_in_schema=False)
@app.get("/opds/latest")
def opds_latest(request: Request, db: Annotated[Session, Depends(get_db)]):
    _require_opds_user(request, db)
    books = list(
        db.scalars(
            select(Book)
            .where(Book.review_state == ReviewState.READY)
            .order_by(Book.created_at.desc(), func.lower(Book.title), Book.id)
        )
    )
    return _opds_books_response(
        request, "Digest Library - Latest", settings.public_url + "/opds/latest", books
    )


@app.get("/opds/catalog/authors-v2/", include_in_schema=False)
@app.get("/opds/catalog/authors-v2")
@app.get("/opds/catalog/authors/", include_in_schema=False)
@app.get("/opds/catalog/authors")
@app.get("/opds/authors/", include_in_schema=False)
@app.get("/opds/authors")
def opds_authors(request: Request, db: Annotated[Session, Depends(get_db)]):
    _require_opds_user(request, db)
    feed = _opds_feed("Digest Library - By Author", settings.public_url + "/opds/authors")
    rows = db.execute(
        select(Book.primary_author, func.count(Book.id))
        .where(Book.review_state == ReviewState.READY)
        .group_by(Book.primary_author)
        .order_by(func.lower(Book.primary_author))
    ).all()
    if _wants_opds_json(request):
        return _opds_json_response(
            {
                "metadata": {"title": "Digest Library - By Author", "numberOfItems": len(rows)},
                "links": [
                    {"rel": "self", "href": "/opds?catalog=authors", "type": "application/opds+json"}
                ],
                "navigation": [
                    _opds_json_link(
                        author,
                        f"/opds/author-group/{quote(author, safe='')}"
                        if count == 1 else f"/opds/author/{quote(author, safe='')}",
                        count=count,
                    )
                    for author, count in rows
                ],
            }
        )
    for author, count in rows:
        label = f"{author} ({count})"
        _opds_add_navigation_entry(
            feed,
            label,
            f"/opds/author-group/{quote(author, safe='')}"
            if count == 1 else f"/opds/author/{quote(author, safe='')}",
            entry_id=f"digest:author:{author}",
        )
    return _opds_response(feed)


@app.get("/opds/author-group/{author}/", include_in_schema=False)
@app.get("/opds/author-group/{author}")
def opds_author_group(request: Request, db: Annotated[Session, Depends(get_db)], author: str):
    _require_opds_user(request, db)
    book_href = f"/opds/author/{quote(author, safe='')}"
    info_href = f"/opds/author-group/{quote(author, safe='')}/about"
    if _wants_opds_json(request):
        return _opds_json_response(
            {
                "metadata": {"title": author},
                "links": [
                    {
                        "rel": "self",
                        "href": f"/opds/author-group/{quote(author, safe='')}",
                        "type": "application/opds+json",
                    }
                ],
                "navigation": [
                    _opds_json_link(f"Books by {author}", book_href),
                    _opds_json_link("Author info", info_href),
                ],
            }
        )
    feed = _opds_feed(author, settings.public_url + f"/opds/author-group/{quote(author, safe='')}")
    _opds_add_navigation_entry(feed, f"Books by {author}", book_href)
    _opds_add_navigation_entry(feed, "Author info", info_href)
    return _opds_response(feed)


@app.get("/opds/author-group/{author}/about/", include_in_schema=False)
@app.get("/opds/author-group/{author}/about")
def opds_author_group_about(
    request: Request, db: Annotated[Session, Depends(get_db)], author: str
):
    _require_opds_user(request, db)
    if _wants_opds_json(request):
        return _opds_json_response(
            {
                "metadata": {"title": f"Author info - {author}", "numberOfItems": 1},
                "links": [
                    {
                        "rel": "self",
                        "href": f"/opds/author-group/{quote(author, safe='')}/about",
                        "type": "application/opds+json",
                    }
                ],
                "publications": [
                    _opds_json_virtual_publication(
                        "Author info",
                        author,
                        "This entry keeps the author available as a folder in Bookshelf.",
                    )
                ],
            }
        )
    feed = _opds_feed(
        f"Author info - {author}",
        settings.public_url + f"/opds/author-group/{quote(author, safe='')}/about",
    )
    entry = SubElement(feed, "entry")
    SubElement(entry, "id").text = f"digest:author-info:{author}"
    SubElement(entry, "title").text = "Author info"
    SubElement(entry, "updated").text = datetime.now(UTC).isoformat()
    person = SubElement(entry, "author")
    SubElement(person, "name").text = author
    SubElement(entry, "summary", type="text").text = (
        "This entry keeps the author available as a folder in Bookshelf."
    )
    return _opds_response(feed)


@app.get("/opds/author-folder/{author}/", include_in_schema=False)
@app.get("/opds/author-folder/{author}")
def opds_author_folder(request: Request, db: Annotated[Session, Depends(get_db)], author: str):
    _require_opds_user(request, db)
    book_href = f"/opds/author/{quote(author, safe='')}"
    info_href = f"/opds/author-folder/{quote(author, safe='')}/info"
    if _wants_opds_json(request):
        return _opds_json_response(
            {
                "metadata": {"title": author},
                "links": [
                    {
                        "rel": "self",
                        "href": f"/opds/author-folder/{quote(author, safe='')}",
                        "type": "application/opds+json",
                    }
                ],
                "navigation": [
                    _opds_json_link(f"Books by {author}", book_href),
                    _opds_json_link("About this author", info_href),
                ],
            }
        )
    feed = _opds_feed(author, settings.public_url + f"/opds/author-folder/{quote(author, safe='')}")
    _opds_add_navigation_entry(feed, f"Books by {author}", book_href)
    _opds_add_navigation_entry(feed, "About this author", info_href)
    return _opds_response(feed)


@app.get("/opds/author-folder/{author}/info/", include_in_schema=False)
@app.get("/opds/author-folder/{author}/info")
def opds_author_folder_info(request: Request, db: Annotated[Session, Depends(get_db)], author: str):
    _require_opds_user(request, db)
    return _opds_empty_response(
        request,
        f"About {author}",
        settings.public_url + f"/opds/author-folder/{quote(author, safe='')}/info",
    )


@app.get("/opds/author/{author}/index/", include_in_schema=False)
@app.get("/opds/author/{author}/index")
def opds_author_index(request: Request, db: Annotated[Session, Depends(get_db)], author: str):
    _require_opds_user(request, db)
    href = f"/opds/author/{quote(author, safe='')}"
    if _wants_opds_json(request):
        return _opds_json_response(
            {
                "metadata": {"title": author},
                "links": [
                    {
                        "rel": "self",
                        "href": f"/opds/author/{quote(author, safe='')}/index",
                        "type": "application/opds+json",
                    }
                ],
                "navigation": [_opds_json_link(f"All books by {author}", href)],
            }
        )
    feed = _opds_feed(author, settings.public_url + f"/opds/author/{quote(author, safe='')}/index")
    _opds_add_navigation_entry(feed, f"All books by {author}", href)
    return _opds_response(feed)


@app.get("/opds/author/{author}/", include_in_schema=False)
@app.get("/opds/author/{author}")
@app.get("/opds/author/", include_in_schema=False)
@app.get("/opds/author")
def opds_author(request: Request, db: Annotated[Session, Depends(get_db)], author: str):
    _require_opds_user(request, db)
    books = list(
        db.scalars(
            select(Book)
            .where(
                Book.review_state == ReviewState.READY,
                func.lower(Book.primary_author) == author.casefold(),
            )
            .order_by(func.lower(Book.sort_title), func.lower(Book.title), Book.id)
        )
    )
    return _opds_books_response(
        request,
        f"Digest Library - {author}",
        settings.public_url + f"/opds/author?{urlencode({'author': author})}",
        books,
    )


@app.get("/opds/catalog/series/", include_in_schema=False)
@app.get("/opds/catalog/series")
@app.get("/opds/series/", include_in_schema=False)
@app.get("/opds/series")
def opds_series(request: Request, db: Annotated[Session, Depends(get_db)]):
    _require_opds_user(request, db)
    feed = _opds_feed("Digest Library - By Series", settings.public_url + "/opds/series")
    rows = db.execute(
        select(Book.series, func.count(Book.id))
        .where(Book.review_state == ReviewState.READY, Book.series.is_not(None), Book.series != "")
        .group_by(Book.series)
        .order_by(func.lower(Book.series))
    ).all()
    if _wants_opds_json(request):
        return _opds_json_response(
            {
                "metadata": {"title": "Digest Library - By Series", "numberOfItems": len(rows)},
                "links": [
                    {"rel": "self", "href": "/opds?catalog=series", "type": "application/opds+json"}
                ],
                "navigation": [
                    _opds_json_link(
                        series,
                        f"/opds/series-folder/{quote(series, safe='')}"
                        if count == 1 else f"/opds/series/{quote(series, safe='')}",
                        count=count,
                    )
                    for series, count in rows
                ],
            }
        )
    for series, count in rows:
        label = f"{series} ({count})"
        _opds_add_navigation_entry(
            feed,
            label,
            f"/opds/series-folder/{quote(series, safe='')}"
            if count == 1 else f"/opds/series/{quote(series, safe='')}",
            entry_id=f"digest:series:{series}",
        )
    return _opds_response(feed)


@app.get("/opds/series-folder/{series}/", include_in_schema=False)
@app.get("/opds/series-folder/{series}")
def opds_series_folder(request: Request, db: Annotated[Session, Depends(get_db)], series: str):
    _require_opds_user(request, db)
    book_href = f"/opds/series/{quote(series, safe='')}"
    info_href = f"/opds/series-folder/{quote(series, safe='')}/info"
    if _wants_opds_json(request):
        return _opds_json_response(
            {
                "metadata": {"title": series},
                "links": [
                    {
                        "rel": "self",
                        "href": f"/opds/series-folder/{quote(series, safe='')}",
                        "type": "application/opds+json",
                    }
                ],
                "navigation": [
                    _opds_json_link(f"Books in {series}", book_href),
                    _opds_json_link("About this series", info_href),
                ],
            }
        )
    feed = _opds_feed(series, settings.public_url + f"/opds/series-folder/{quote(series, safe='')}")
    _opds_add_navigation_entry(feed, f"Books in {series}", book_href)
    _opds_add_navigation_entry(feed, "About this series", info_href)
    return _opds_response(feed)


@app.get("/opds/series-folder/{series}/info/", include_in_schema=False)
@app.get("/opds/series-folder/{series}/info")
def opds_series_folder_info(request: Request, db: Annotated[Session, Depends(get_db)], series: str):
    _require_opds_user(request, db)
    return _opds_empty_response(
        request,
        f"About {series}",
        settings.public_url + f"/opds/series-folder/{quote(series, safe='')}/info",
    )


@app.get("/opds/series/{series}/index/", include_in_schema=False)
@app.get("/opds/series/{series}/index")
def opds_series_index(request: Request, db: Annotated[Session, Depends(get_db)], series: str):
    _require_opds_user(request, db)
    href = f"/opds/series/{quote(series, safe='')}"
    if _wants_opds_json(request):
        return _opds_json_response(
            {
                "metadata": {"title": series},
                "links": [
                    {
                        "rel": "self",
                        "href": f"/opds/series/{quote(series, safe='')}/index",
                        "type": "application/opds+json",
                    }
                ],
                "navigation": [_opds_json_link(f"All books in {series}", href)],
            }
        )
    feed = _opds_feed(series, settings.public_url + f"/opds/series/{quote(series, safe='')}/index")
    _opds_add_navigation_entry(feed, f"All books in {series}", href)
    return _opds_response(feed)


@app.get("/opds/series/{series}/", include_in_schema=False)
@app.get("/opds/series/{series}")
@app.get("/opds/series/books/", include_in_schema=False)
@app.get("/opds/series/books")
def opds_series_books(request: Request, db: Annotated[Session, Depends(get_db)], series: str):
    _require_opds_user(request, db)
    books = list(
        db.scalars(
            select(Book)
            .where(
                Book.review_state == ReviewState.READY,
                func.lower(Book.series) == series.casefold(),
            )
            .order_by(Book.series_number, func.lower(Book.sort_title), func.lower(Book.title), Book.id)
        )
    )
    return _opds_books_response(
        request,
        f"Digest Library - {series}",
        settings.public_url + f"/opds/series/books?{urlencode({'series': series})}",
        books,
    )


@app.get("/opds/downloads/", include_in_schema=False)
@app.get("/opds/downloads")
def opds_downloads(request: Request, db: Annotated[Session, Depends(get_db)]):
    user = _require_opds_user(request, db)
    wanted_items = list(
        db.scalars(
            select(WantedItem)
            .where(WantedItem.user_id == user.id, WantedItem.status != WantedStatus.CANCELLED)
            .order_by(WantedItem.updated_at.desc(), WantedItem.id.desc())
            .limit(50)
        )
    )
    review_books = _opds_review_books(db) if user.role == Role.ADMIN else []
    if _wants_opds_json(request):
        publications = [
            _opds_json_download_status_publication(db, item)
            for item in wanted_items
        ]
        publications.extend(_opds_json_review_publication(book) for book in review_books)
        return _opds_json_response(
            {
                "metadata": {"title": "Digest Downloads", "numberOfItems": len(publications)},
                "links": [{"rel": "self", "href": "/opds/downloads", "type": "application/opds+json"}],
                "publications": publications,
            }
        )
    feed = _opds_feed("Digest Downloads", settings.public_url + "/opds/downloads")
    for item in wanted_items:
        _opds_add_download_status_entry(feed, db, item)
    for book in review_books:
        _opds_add_review_entry(feed, book)
    return _opds_response(feed)


@app.get("/opds/downloads/review/{book_id}/", include_in_schema=False)
@app.get("/opds/downloads/review/{book_id}")
def opds_download_review(
    book_id: str,
    request: Request,
    db: Annotated[Session, Depends(get_db)],
):
    _require_opds_admin(request, db)
    book = db.get(Book, book_id)
    if book is None:
        raise HTTPException(404)
    navigation = [
        _opds_json_link(
            "Search matching metadata",
            f"/opds/downloads/review/{book.id}/search?{urlencode({'title': book.title, 'author': book.primary_author})}",
        ),
        _opds_json_link("Approve embedded metadata", f"/opds/downloads/review/{book.id}/embedded"),
        _opds_json_link("Back to downloads", "/opds/downloads"),
    ]
    return _opds_navigation_response(
        request,
        f"Review metadata - {book.title}",
        settings.public_url + f"/opds/downloads/review/{book.id}",
        navigation,
    )


@app.get("/opds/downloads/review/{book_id}/search/", include_in_schema=False)
@app.get("/opds/downloads/review/{book_id}/search")
def opds_download_review_search(
    book_id: str,
    request: Request,
    db: Annotated[Session, Depends(get_db)],
    title: str = "",
    author: str = "",
):
    _require_opds_admin(request, db)
    book = db.get(Book, book_id)
    if book is None:
        raise HTTPException(404)
    candidates = find_candidates(
        db,
        book,
        title=title or book.title,
        author=author or book.primary_author,
        isbns=[],
    )
    navigation = [
        _opds_json_link(
            f"{item.get('title') or 'Untitled'} - {', '.join(item.get('authors') or []) or 'Unknown'} ({round(float(item.get('confidence') or 0) * 100)}%)",
            f"/opds/downloads/review/{book.id}/apply/{index}?{urlencode({'title': title or book.title, 'author': author or book.primary_author})}",
        )
        for index, item in enumerate(candidates)
    ]
    if not navigation:
        navigation.append(_opds_json_link("No provider matches found", f"/opds/downloads/review/{book.id}"))
    navigation.append(_opds_json_link("Back to review", f"/opds/downloads/review/{book.id}"))
    return _opds_navigation_response(
        request,
        f"Metadata matches - {book.title}",
        settings.public_url + f"/opds/downloads/review/{book.id}/search",
        navigation,
    )


@app.get("/opds/downloads/review/{book_id}/apply/{index}/", include_in_schema=False)
@app.get("/opds/downloads/review/{book_id}/apply/{index}")
def opds_download_review_apply(
    book_id: str,
    index: int,
    request: Request,
    db: Annotated[Session, Depends(get_db)],
    title: str = "",
    author: str = "",
):
    _require_opds_admin(request, db)
    book = db.get(Book, book_id)
    if book is None:
        raise HTTPException(404)
    candidates = find_candidates(
        db,
        book,
        title=title or book.title,
        author=author or book.primary_author,
        isbns=[],
    )
    if index < 0 or index >= len(candidates):
        raise HTTPException(404)
    apply_candidate(db, book, candidates[index], organise=True, replace_existing=True)
    return _opds_json_action_response("Metadata approved", f"Approved {book.title}")


@app.get("/opds/downloads/review/{book_id}/embedded/", include_in_schema=False)
@app.get("/opds/downloads/review/{book_id}/embedded")
def opds_download_review_embedded(
    book_id: str,
    request: Request,
    db: Annotated[Session, Depends(get_db)],
):
    _require_opds_admin(request, db)
    book = db.get(Book, book_id)
    if book is None:
        raise HTTPException(404)
    organise_book(db, book)
    return _opds_json_action_response("Metadata approved", f"Approved embedded metadata for {book.title}")


@app.get("/opds/discover/", include_in_schema=False)
@app.get("/opds/discover")
def opds_discover(request: Request, db: Annotated[Session, Depends(get_db)]):
    _require_opds_user(request, db)
    navigation = [
        _opds_json_link("Search", "/opds/discover/search"),
        _opds_json_link("Trending", "/opds/discover/trending"),
        _opds_json_link("NYT Bestsellers", "/opds/discover/nyt-bestsellers"),
        _opds_json_link("New Releases", "/opds/discover/new-releases"),
        _opds_json_link("Downloads", "/opds/downloads"),
    ]
    return _opds_navigation_response(
        request,
        "Digest Discover",
        settings.public_url + "/opds/discover",
        navigation,
        searchable=True,
    )


@app.get("/opds/discover/for-you/", include_in_schema=False)
@app.get("/opds/discover/for-you")
def opds_discover_for_you(request: Request, db: Annotated[Session, Depends(get_db)]):
    user = _require_opds_user(request, db)
    return _opds_discovery_response(
        request,
        db,
        "Digest Discover - For you",
        settings.public_url + "/opds/discover/for-you",
        build_discovery(db, user.id).recommended,
    )


@app.get("/opds/discover/trending/", include_in_schema=False)
@app.get("/opds/discover/trending")
def opds_discover_trending(
    request: Request,
    db: Annotated[Session, Depends(get_db)],
    period: str = "now",
    genre: str = "",
):
    _require_opds_user(request, db)
    if not genre:
        return _opds_navigation_response(
            request,
            "Digest Discover - Trending",
            settings.public_url + "/opds/discover/trending",
            _opds_genre_navigation("/opds/discover/trending"),
        )
    user = _require_opds_user(request, db)
    config = settings_map(db)
    if config.get("hardcover_api_key"):
        _, days = HARDCOVER_TRENDING_PERIODS.get(period, HARDCOVER_TRENDING_PERIODS["now"])
        genre_label = hardcover_genre_query_label(
            config["hardcover_api_key"], GENRES.get(genre, genre)
        )
        items = hardcover_books(config["hardcover_api_key"], days=days, genre=genre_label)
    else:
        items = build_discovery(db, user.id).trending
    return _opds_discovery_response(
        request,
        db,
        "Digest Discover - Trending",
        settings.public_url + f"/opds/discover/trending/{quote(genre, safe='')}/{quote(period, safe='')}",
        items,
    )


@app.get("/opds/discover/trending/{genre}/", include_in_schema=False)
@app.get("/opds/discover/trending/{genre}")
def opds_discover_trending_periods(
    request: Request,
    db: Annotated[Session, Depends(get_db)],
    genre: str,
):
    _require_opds_user(request, db)
    key = genre if genre in GENRES else "fantasy"
    return _opds_navigation_response(
        request,
        f"Trending - {GENRES[key]}",
        settings.public_url + f"/opds/discover/trending/{quote(key, safe='')}",
        _opds_period_navigation(
            f"/opds/discover/trending/{quote(key, safe='')}",
            HARDCOVER_TRENDING_PERIODS,
        ),
    )


@app.get("/opds/discover/trending/{genre}/{period}/", include_in_schema=False)
@app.get("/opds/discover/trending/{genre}/{period}")
def opds_discover_trending_books(
    request: Request,
    db: Annotated[Session, Depends(get_db)],
    genre: str,
    period: str,
):
    return opds_discover_trending(request, db, period=period, genre=genre)


@app.get("/opds/discover/new-releases/", include_in_schema=False)
@app.get("/opds/discover/new-releases")
def opds_discover_new_releases(
    request: Request,
    db: Annotated[Session, Depends(get_db)],
    genre: str = "",
    period: str = "90d",
):
    _require_opds_user(request, db)
    if not genre:
        return _opds_navigation_response(
            request,
            "Digest Discover - New Releases",
            settings.public_url + "/opds/discover/new-releases",
            _opds_genre_navigation("/opds/discover/new-releases"),
        )
    user = _require_opds_user(request, db)
    config = settings_map(db)
    if config.get("hardcover_api_key"):
        _, days = OPDS_NEW_RELEASE_PERIODS.get(period, OPDS_NEW_RELEASE_PERIODS["90d"])
        items = hardcover_books(
            config["hardcover_api_key"],
            days=days,
            genre=hardcover_genre_query_label(
                config["hardcover_api_key"], GENRES.get(genre, genre)
            ),
            new_releases=True,
        )
    else:
        items = build_discovery(db, user.id).new_releases
    return _opds_discovery_response(
        request,
        db,
        "Digest Discover - New releases",
        settings.public_url + f"/opds/discover/new-releases/{quote(genre, safe='')}/{quote(period, safe='')}",
        items,
    )


@app.get("/opds/discover/new-releases/{genre}/", include_in_schema=False)
@app.get("/opds/discover/new-releases/{genre}")
def opds_discover_new_release_periods(
    request: Request,
    db: Annotated[Session, Depends(get_db)],
    genre: str,
):
    _require_opds_user(request, db)
    key = genre if genre in GENRES else "fantasy"
    return _opds_navigation_response(
        request,
        f"New Releases - {GENRES[key]}",
        settings.public_url + f"/opds/discover/new-releases/{quote(key, safe='')}",
        _opds_period_navigation(
            f"/opds/discover/new-releases/{quote(key, safe='')}",
            OPDS_NEW_RELEASE_PERIODS,
        ),
    )


@app.get("/opds/discover/new-releases/{genre}/{period}/", include_in_schema=False)
@app.get("/opds/discover/new-releases/{genre}/{period}")
def opds_discover_new_release_books(
    request: Request,
    db: Annotated[Session, Depends(get_db)],
    genre: str,
    period: str,
):
    return opds_discover_new_releases(request, db, genre=genre, period=period)


@app.get("/opds/discover/nyt-bestsellers/", include_in_schema=False)
@app.get("/opds/discover/nyt-bestsellers")
def opds_discover_nyt_bestsellers(
    request: Request,
    db: Annotated[Session, Depends(get_db)],
):
    _require_opds_user(request, db)
    _, lists = configured_nyt_lists(db)
    navigation = [
        _opds_json_link(item["title"], f"/opds/discover/nyt-bestsellers/{quote(item['slug'], safe='')}")
        for item in lists
    ]
    return _opds_navigation_response(
        request,
        "Digest Discover - NYT Bestsellers",
        settings.public_url + "/opds/discover/nyt-bestsellers",
        navigation,
    )


@app.get("/opds/discover/nyt-bestsellers/{slug}/", include_in_schema=False)
@app.get("/opds/discover/nyt-bestsellers/{slug}")
def opds_discover_nyt_bestseller_weeks(
    request: Request,
    db: Annotated[Session, Depends(get_db)],
    slug: str,
):
    _require_opds_user(request, db)
    _, lists = configured_nyt_lists(db)
    item = next((value for value in lists if value["slug"] == slug), None)
    if item is None:
        return _opds_empty_response(
            request,
            "Digest Discover - NYT Bestsellers",
            settings.public_url + f"/opds/discover/nyt-bestsellers/{quote(slug, safe='')}",
        )
    navigation = [
        _opds_json_link(
            "Current",
            f"/opds/discover/nyt-bestsellers/{quote(slug, safe='')}/current",
            count=15,
        ),
        *[
            _opds_json_link(
                week["title"],
                f"/opds/discover/nyt-bestsellers/{quote(slug, safe='')}/{quote(week['date'], safe='')}",
                count=15,
            )
            for week in nyt_weeks(item)
        ],
    ]
    return _opds_navigation_response(
        request,
        f"NYT Bestsellers - {item['title']}",
        settings.public_url + f"/opds/discover/nyt-bestsellers/{quote(slug, safe='')}",
        navigation,
    )


def _cached_nyt_bestsellers(api_key: str, slug: str, date_value: str) -> list[dict]:
    cache_key = (slug, date_value)
    now_value = datetime.now(UTC)
    cached = _nyt_opds_cache.get(cache_key)
    if cached and cached[0] > now_value:
        logger.info(
            "OPDS NYT cache hit slug=%s date=%s items=%d",
            slug,
            date_value,
            len(cached[1]),
        )
        return cached[1]
    items = nyt_bestsellers(api_key, slug, date_value)
    ttl = NYT_OPDS_CACHE_TTL if items else NYT_OPDS_EMPTY_CACHE_TTL
    _nyt_opds_cache[cache_key] = (now_value + ttl, items)
    logger.info(
        "OPDS NYT cache store slug=%s date=%s items=%d ttl_seconds=%d",
        slug,
        date_value,
        len(items),
        int(ttl.total_seconds()),
    )
    return items


@app.get("/opds/discover/nyt-bestsellers/{slug}/{week}/", include_in_schema=False)
@app.get("/opds/discover/nyt-bestsellers/{slug}/{week}")
def opds_discover_nyt_bestseller_books(
    request: Request,
    db: Annotated[Session, Depends(get_db)],
    slug: str,
    week: str,
):
    _require_opds_user(request, db)
    api_key, lists = configured_nyt_lists(db)
    item = next((value for value in lists if value["slug"] == slug), None)
    if not api_key or item is None:
        logger.warning(
            "OPDS NYT unavailable slug=%s week=%s has_api_key=%s list_found=%s list_count=%d",
            slug,
            week,
            bool(api_key),
            item is not None,
            len(lists),
        )
        return _opds_empty_response(
            request,
            "Digest Discover - NYT Bestsellers",
            settings.public_url + f"/opds/discover/nyt-bestsellers/{quote(slug, safe='')}/{quote(week, safe='')}",
    )
    date_value = week if re.fullmatch(r"\d{4}-\d{2}-\d{2}", week) else "current"
    try:
        items = _cached_nyt_bestsellers(api_key, slug, date_value)
        logger.info(
            "OPDS NYT fetched slug=%s requested_week=%s effective_week=%s items=%d",
            slug,
            week,
            date_value,
            len(items),
        )
    except (httpx.HTTPError, TypeError, ValueError) as exc:
        status_code = getattr(getattr(exc, "response", None), "status_code", None)
        logger.error(
            "OPDS NYT fetch failed slug=%s requested_week=%s effective_week=%s status=%s error=%s",
            slug,
            week,
            date_value,
            status_code,
            type(exc).__name__,
        )
        items = []
    if not items and date_value != "current":
        logger.info(
            "OPDS NYT empty for dated week; trying current slug=%s requested_week=%s",
            slug,
            week,
        )
        try:
            items = _cached_nyt_bestsellers(api_key, slug, "current")
            date_value = "current"
            logger.info(
                "OPDS NYT current fallback fetched slug=%s items=%d",
                slug,
                len(items),
            )
        except (httpx.HTTPError, TypeError, ValueError) as exc:
            status_code = getattr(getattr(exc, "response", None), "status_code", None)
            logger.error(
                "OPDS NYT current fallback failed slug=%s status=%s error=%s",
                slug,
                status_code,
                type(exc).__name__,
            )
            items = []
    logger.info(
        "OPDS NYT rendering slug=%s requested_week=%s rendered_week=%s items=%d",
        slug,
        week,
        date_value,
        len(items),
    )
    return _opds_discovery_response(
        request,
        db,
        f"NYT Bestsellers - {item['title']} - {date_value}",
        settings.public_url + f"/opds/discover/nyt-bestsellers/{quote(slug, safe='')}/{quote(date_value, safe='')}",
        items,
    )


@app.get("/opds/discover/genre/{genre}/", include_in_schema=False)
@app.get("/opds/discover/genre/{genre}")
@app.get("/opds/discover/genre/", include_in_schema=False)
@app.get("/opds/discover/genre")
def opds_discover_genre(
    request: Request,
    db: Annotated[Session, Depends(get_db)],
    genre: str = "fantasy",
):
    user = _require_opds_user(request, db)
    key = genre if genre in GENRES else "fantasy"
    config = settings_map(db)
    if config.get("hardcover_api_key"):
        items = hardcover_books(
            config["hardcover_api_key"],
            days=None,
            genre=hardcover_genre_query_label(config["hardcover_api_key"], GENRES[key]),
        )
    else:
        items = build_discovery(db, user.id, genre=key).genre_items
    return _opds_discovery_response(
        request,
        db,
        f"Digest Discover - {GENRES[key]}",
        settings.public_url + f"/opds/discover/genre?genre={key}",
        items,
    )


@app.get("/opds/discover/search/", include_in_schema=False)
@app.get("/opds/discover/search")
def opds_discover_search(
    request: Request,
    db: Annotated[Session, Depends(get_db)],
    q: str = "",
    query: str = "",
):
    user = _require_opds_user(request, db)
    search_query = (q or query).strip()
    if not search_query:
        return _opds_search_status_response(
            request,
            db,
            user,
            "Digest Discover - Search",
            settings.public_url + "/opds/discover/search",
    )
    config = settings_map(db)
    try:
        items = search_discovery_books(
            search_query,
            hardcover_api_key=config.get("hardcover_api_key", ""),
            language=config.get("default_language", "en"),
        )
    except (httpx.HTTPError, TypeError, ValueError):
        items = []
    try:
        author_items = author_bibliography(
            search_query,
            hardcover_api_key=config.get("hardcover_api_key", ""),
            language=config.get("default_language", "en"),
        )
    except (httpx.HTTPError, TypeError, ValueError):
        author_items = []
    return _opds_search_status_response(
        request,
        db,
        user,
        f"Digest Discover - Search: {search_query}",
        settings.public_url + f"/opds/discover/search?{urlencode({'query': search_query})}",
        search_results=_opds_dedupe_discovery_items([*items, *author_items]),
    )


@app.get("/opds/discover/search.xml", include_in_schema=False)
def opds_discover_search_descriptor(request: Request, db: Annotated[Session, Depends(get_db)]):
    _require_opds_user(request, db)
    description = Element(f"{{{OS_NS}}}OpenSearchDescription")
    SubElement(description, "ShortName").text = "Digest Discover"
    SubElement(description, "Description").text = "Search Digest Discover by title or author"
    SubElement(
        description,
        "Url",
        type="application/atom+xml;profile=opds-catalog;kind=acquisition",
        template=_opds_href("/opds/discover/search?query={searchTerms}"),
    )
    SubElement(
        description,
        "Url",
        type="application/opds+json",
        template=_opds_href("/opds/discover/search?query={searchTerms}"),
    )
    return _opensearch_response(description)


def _owned_opds_wanted(db: Session, user: User, wanted_id: int) -> WantedItem:
    item = db.get(WantedItem, wanted_id)
    if item is None or item.user_id != user.id:
        raise HTTPException(404)
    return item


@app.get("/opds/discover/status/{wanted_id}", include_in_schema=False)
def opds_discover_download_status(
    wanted_id: int,
    request: Request,
    db: Annotated[Session, Depends(get_db)],
):
    user = _require_opds_user(request, db)
    return JSONResponse(_opds_wanted_payload(db, _owned_opds_wanted(db, user, wanted_id)))


@app.get("/opds/discover/download/{wanted_id}/{release_id}", include_in_schema=False)
def opds_discover_select_download(
    wanted_id: int,
    release_id: int,
    request: Request,
    db: Annotated[Session, Depends(get_db)],
):
    user = _require_opds_user(request, db)
    item = _owned_opds_wanted(db, user, wanted_id)
    release = db.get(AcquisitionRelease, release_id)
    if release is None or release.wanted_id != item.id:
        raise HTTPException(404)
    try:
        queue_release(db, item, release)
    except ValueError as exc:
        raise HTTPException(409, str(exc)) from exc
    return JSONResponse(_opds_wanted_payload(db, item))


@app.get("/opds/discover/request/", include_in_schema=False)
@app.get("/opds/discover/request")
def opds_discover_request(
    request: Request,
    db: Annotated[Session, Depends(get_db)],
    source: str = "",
    title: str = "",
    source_id: str = "",
    author: str = "",
    isbn: str = "",
    cover_url: str = "",
):
    user = _require_opds_user(request, db)
    source = source.strip()
    title = title.strip()
    author = author.strip()
    isbn = isbn.strip()
    if source not in {"hardcover", "nytimes", "openlibrary"} or not title:
        raise HTTPException(400, "Invalid discovery book")
    if find_library_book(db, title=title, author=author, isbn=isbn) is not None:
        return Response(
            f"{title} is already in your Digest library.",
            200,
            media_type="text/plain; charset=utf-8",
            headers=_opds_headers(),
        )
    item = create_wanted(
        db,
        user_id=user.id,
        source=source,
        source_id=source_id,
        title=title,
        author=author,
        isbn=isbn,
        cover_url=cover_url,
    )
    if "application/json" in request.headers.get("accept", ""):
        return JSONResponse(_opds_wanted_payload(db, item), status_code=202)
    return Response(
        f"Digest queued {item.title} for download.",
        202,
        media_type="text/plain; charset=utf-8",
        headers=_opds_headers(),
    )
