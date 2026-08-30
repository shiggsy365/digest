from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session
from starlette.requests import Request

from digest.db import Base
from digest.main import (
    add_book_to_shelf,
    create_shelf,
    library_bulk,
    profile_settings,
    shelf_detail,
    shelves_page,
)
from digest.models import Book, Job, ReviewState, Role, Shelf, ShelfBook, User
from digest.security import hash_password


def request_for(user: User, method: str = "GET", path: str = "/shelves") -> Request:
    return Request(
        {
            "type": "http",
            "method": method,
            "path": path,
            "headers": [],
            "query_string": b"",
            "session": {"user_id": user.id, "csrf": "token"},
        }
    )


def shelf_session() -> Session:
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    return Session(engine, expire_on_commit=False)


def test_private_and_shared_shelves_are_visible_to_the_right_users() -> None:
    with shelf_session() as db:
        admin = User(
            username="admin",
            password_hash=hash_password("a-long-test-password"),
            role=Role.ADMIN,
        )
        reader = User(
            username="reader",
            password_hash=hash_password("a-long-test-password"),
            role=Role.USER,
        )
        db.add_all([admin, reader])
        db.commit()

        create_shelf(request_for(admin, "POST"), db, "token", "Everyone", "true")
        create_shelf(request_for(reader, "POST"), db, "token", "My shelf")

        admin_html = shelves_page(request_for(admin), db).body.decode()
        reader_html = shelves_page(request_for(reader), db).body.decode()
        assert "All Books" in admin_html and "All Books" in reader_html
        assert "Everyone" in admin_html and "My shelf" not in admin_html
        assert "Everyone" in reader_html and "My shelf" in reader_html


def test_bulk_library_adds_selected_books_to_user_shelf() -> None:
    with shelf_session() as db:
        reader = User(username="reader", password_hash=hash_password("a-long-test-password"))
        books = [Book(title=f"Book {number}", primary_author="Writer",
                      review_state=ReviewState.READY) for number in range(2)]
        db.add_all([reader, *books])
        db.commit()
        shelf = Shelf(name="Selected", owner_id=reader.id)
        db.add(shelf)
        db.commit()

        response = library_bulk(request_for(reader, "POST", "/library/bulk"), db,
                                action="add_to_shelf", form_csrf="token",
                                book_ids=[book.id for book in books], shelf_id=shelf.id,
                                return_to="/?view=all")

        assert response.status_code == 303
        assert len(list(db.scalars(select(ShelfBook)))) == 2


def test_bulk_auto_scrape_queues_each_selected_book() -> None:
    with shelf_session() as db:
        admin = User(username="admin", password_hash=hash_password("a-long-test-password"),
                     role=Role.ADMIN)
        books = [Book(title=f"Book {number}", primary_author="Writer",
                      review_state=ReviewState.READY) for number in range(2)]
        db.add_all([admin, *books])
        db.commit()

        library_bulk(request_for(admin, "POST", "/library/bulk"), db,
                     action="auto_scrape", form_csrf="token",
                     book_ids=[book.id for book in books], return_to="/?view=all")

        jobs = list(db.scalars(select(Job).where(Job.kind == "metadata_auto_scrape")))
        assert len(jobs) == 2


def test_book_can_be_added_to_shelf_and_browsed_in_shelf_order() -> None:
    with shelf_session() as db:
        reader = User(
            username="reader",
            password_hash=hash_password("a-long-test-password"),
            role=Role.USER,
        )
        first = Book(title="Zulu", primary_author="Author A", review_state=ReviewState.READY)
        second = Book(title="Alpha", primary_author="Author B", review_state=ReviewState.READY)
        shelf = Shelf(name="Read next", owner_id=None, shared=True)
        db.add_all([reader, first, second, shelf])
        db.commit()

        add_book_to_shelf(
            first.id,
            request_for(reader, "POST"),
            db,
            "token",
            shelf.id,
            "/shelves",
        )
        add_book_to_shelf(
            second.id,
            request_for(reader, "POST"),
            db,
            "token",
            shelf.id,
            "/shelves",
        )

        assert len(list(db.scalars(select(ShelfBook)))) == 2
        html = shelf_detail(shelf.id, request_for(reader), db, sort="title").body.decode()
        assert html.index("Alpha") < html.index("Zulu")
        assert f"/shelves/{shelf.id}%3Fsort%3Dtitle" in html


def test_user_can_choose_one_accessible_kobo_sync_shelf() -> None:
    with shelf_session() as db:
        reader = User(
            username="reader",
            password_hash=hash_password("a-long-test-password"),
            role=Role.USER,
        )
        private = Shelf(name="Kobo", owner_id=None, shared=False)
        db.add_all([reader, private])
        db.commit()
        private.owner_id = reader.id
        db.commit()

        response = profile_settings(
            request_for(reader, "POST", "/settings/profile"),
            db,
            kindle_email="reader@example.test",
            form_csrf="token",
            kobo_sync_shelf_id=str(private.id),
        )

        assert response.status_code == 303
        assert reader.kobo_sync_shelf_id == private.id
        assert reader.kindle_email == "reader@example.test"

        profile_settings(
            request_for(reader, "POST", "/settings/profile"),
            db,
            kindle_email="",
            form_csrf="token",
            kobo_sync_shelf_id="",
        )
        assert reader.kobo_sync_shelf_id is None

        profile_settings(
            request_for(reader, "POST", "/settings/profile"), db,
            form_csrf="token", kobo_sync_shelf_id="all",
        )
        assert reader.kobo_sync_shelf_id is None
        assert reader.kobo_sync_all_books is True
        assert reader.kindle_email is None
