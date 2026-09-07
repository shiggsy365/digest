from sqlalchemy import create_engine
from sqlalchemy.orm import Session
from starlette.requests import Request

from digest.db import Base
import pytest
import httpx
from fastapi import HTTPException

from digest.ereader_api import (
    _csrf,
    authors,
    bestseller_lists,
    discover_author,
    discover_search,
    library,
    shelf,
)
from digest.main import render, settings
from digest.models import AppSetting, Book, ReviewState, Role, Shelf, ShelfBook, User
from digest.security import hash_password


def request_for(user: User, path: str = "/api/ereader/library") -> Request:
    return Request({
        "type": "http",
        "method": "GET",
        "path": path,
        "headers": [(b"user-agent", b"Kobo Touch")],
        "query_string": b"",
        "session": {"user_id": user.id, "csrf": "test-token"},
    })


def test_ereader_api_allows_bearer_clients_without_csrf_header() -> None:
    request = Request({
        "type": "http",
        "method": "POST",
        "path": "/api/ereader/downloads",
        "headers": [(b"authorization", b"Bearer dgt_test")],
        "query_string": b"",
        "session": {},
    })

    _csrf(request)


def test_ereader_api_still_requires_csrf_for_session_clients() -> None:
    request = Request({
        "type": "http",
        "method": "POST",
        "path": "/api/ereader/downloads",
        "headers": [],
        "query_string": b"",
        "session": {"csrf": "expected"},
    })

    with pytest.raises(HTTPException) as exc:
        _csrf(request)
    assert exc.value.status_code == 403


def test_library_api_paginates_and_exposes_directories() -> None:
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    with Session(engine, expire_on_commit=False) as db:
        user = User(username="reader", password_hash=hash_password("long-test-password"),
                    role=Role.USER)
        db.add(user)
        db.add_all([
            Book(title="Alpha", primary_author="A Writer", review_state=ReviewState.READY),
            Book(title="Beta", primary_author="B Writer", review_state=ReviewState.READY),
        ])
        db.commit()
        request = request_for(user)

        result = library(request, db, view="all", page_size=1)

        assert result["total"] == 2
        assert result["has_more"] is True
        assert len(result["items"]) == 1
        assert authors(request, db)["items"] == [
            {"name": "A Writer", "count": 1},
            {"name": "B Writer", "count": 1},
        ]


def test_flagged_ereader_render_uses_spa_shell() -> None:
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    with Session(engine, expire_on_commit=False) as db:
        user = User(username="reader", password_hash=hash_password("long-test-password"),
                    role=Role.USER)
        db.add(user)
        db.commit()
        request = request_for(user, "/")
        previous = settings.ereader_spa
        settings.ereader_spa = True
        try:
            response = render(request, "library.html", {}, user)
        finally:
            settings.ereader_spa = previous

        html = response.body.decode()
        assert 'id="spa-shell"' in html
        assert "/static/ereader-app.js" in html
        assert '>Menu</button>' in html


def test_ereader_shell_keeps_secondary_routes_in_menu() -> None:
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    with Session(engine, expire_on_commit=False) as db:
        user = User(username="reader", password_hash=hash_password("long-test-password"),
                    role=Role.USER)
        db.add(user)
        db.commit()
        request = request_for(user, "/")
        previous = settings.ereader_spa
        settings.ereader_spa = False
        try:
            response = render(request, "setup.html", {}, user)
        finally:
            settings.ereader_spa = previous

        html = response.body.decode()
        nav = html.split('<div id="burger-menu"', 1)[0]
        assert 'href="/shelves"' not in nav
        assert '<button type="button" id="nav-burger-btn"' in nav
        assert 'href="/shelves" role="menuitem">Shelves</a>' in html


def test_shelf_api_paginates_every_book_in_six_row_batches() -> None:
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    with Session(engine, expire_on_commit=False) as db:
        user = User(username="reader", password_hash=hash_password("long-test-password"),
                    role=Role.USER)
        found = Shelf(name="Large", owner_id=None, shared=True)
        books = [Book(title=f"Book {index}", primary_author="Author",
                      review_state=ReviewState.READY) for index in range(7)]
        db.add_all([user, found, *books])
        db.flush()
        found.owner_id = user.id
        db.add_all([ShelfBook(shelf_id=found.id, book_id=book.id) for book in books])
        db.commit()

        first = shelf(found.id, request_for(user), db, page=1, page_size=6)
        second = shelf(found.id, request_for(user), db, page=2, page_size=6)

        assert len(first["items"]) == 6
        assert first["has_more"] is True
        assert len(second["items"]) == 1
        assert second["has_more"] is False


def test_discovery_author_is_available_to_the_spa(monkeypatch) -> None:
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    with Session(engine, expire_on_commit=False) as db:
        user = User(username="reader", password_hash=hash_password("long-test-password"),
                    role=Role.USER)
        db.add(user)
        db.commit()
        monkeypatch.setattr(
            "digest.ereader_api.author_bibliography",
            lambda author, **kwargs: [{
                "source": "hardcover", "source_id": "1", "title": "Another Book",
                "author": author, "authors": [author], "isbn": "", "cover_url": "",
            }],
        )

        result = discover_author(request_for(user), db, author="Known Author")

        assert result["author"] == "Known Author"
        assert result["items"][0]["title"] == "Another Book"
        assert result["items"][0]["in_library"] is False


def test_discover_search_returns_empty_when_providers_fail(monkeypatch) -> None:
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    with Session(engine, expire_on_commit=False) as db:
        user = User(username="reader", password_hash=hash_password("long-test-password"),
                    role=Role.USER)
        db.add(user)
        db.commit()
        monkeypatch.setattr(
            "digest.ereader_api.search_discovery_books",
            lambda *args, **kwargs: (_ for _ in ()).throw(httpx.ConnectError("offline")),
        )
        monkeypatch.setattr(
            "digest.ereader_api.author_bibliography",
            lambda *args, **kwargs: (_ for _ in ()).throw(httpx.ConnectError("offline")),
        )

        result = discover_search(request_for(user), db, q="anything")

        assert result == {"items": []}


def test_bestseller_lists_fall_back_when_provider_fails(monkeypatch) -> None:
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    with Session(engine, expire_on_commit=False) as db:
        user = User(username="reader", password_hash=hash_password("long-test-password"),
                    role=Role.USER)
        db.add_all([user, AppSetting(key="nytimes_api_key", value="secret", secret=True)])
        db.commit()
        monkeypatch.setattr(
            "digest.ereader_api.nyt_weekly_lists",
            lambda *args, **kwargs: (_ for _ in ()).throw(httpx.ConnectError("offline")),
        )

        result = bestseller_lists(request_for(user), db)

        assert result["configured"] is True
        assert result["items"]
        assert result["items"][0]["slug"]
