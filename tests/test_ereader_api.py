from sqlalchemy import create_engine
from sqlalchemy.orm import Session
from starlette.requests import Request

from digest.db import Base
from digest.ereader_api import authors, library
from digest.main import render, settings
from digest.models import Book, ReviewState, Role, User
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
