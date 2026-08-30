from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session
from starlette.requests import Request

from digest.db import Base
from digest.main import (
    book_detail,
    library,
    metadata_suggestions,
    review_book,
    update_reading_state,
)
from digest.models import Book, ReadingState, ReviewState, Role, User
from digest.security import hash_password


def test_library_home_has_book_author_and_series_rows() -> None:
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    with Session(engine, expire_on_commit=False) as db:
        user = User(
            username="reader",
            password_hash=hash_password("a-long-test-password"),
            role=Role.USER,
        )
        db.add(user)
        db.add_all(
            [
                Book(
                    title="First Book",
                    primary_author="Author One",
                    series="Series One",
                    series_number=2,
                    cover_path="/library/first/cover.jpg",
                    review_state=ReviewState.READY,
                ),
                Book(
                    title="Second Book",
                    primary_author="Author One",
                    series="Series One",
                    review_state=ReviewState.READY,
                ),
                Book(
                    title="Standalone",
                    primary_author="Author Two",
                    review_state=ReviewState.READY,
                ),
            ]
        )
        db.commit()
        request = Request(
            {
                "type": "http",
                "method": "GET",
                "path": "/",
                "headers": [],
                "query_string": b"",
                "session": {"user_id": user.id},
            }
        )

        home = library(request, db)
        html = home.body.decode()
        assert "<h2>Latest</h2>" in html
        assert "<h2>All Books</h2>" in html
        assert "<h2>Authors</h2>" in html
        assert "<h2>Series</h2>" in html
        assert "/?view=all" in html
        assert "/?view=authors" in html
        assert "/?view=series" in html
        assert "/?author=Author%20One" in html
        assert "/?series=Series%20One" in html
        assert "2 books" in html
        assert "Series One – 2" in html

        author = library(request, db, author="Author One")
        html = author.body.decode()
        assert "Books by Author One" in html
        assert "First Book" in html and "Second Book" in html
        assert "Standalone" not in html
        assert "Release date" in html
        assert "Added date" in html

        all_books = library(request, db, view="all", sort="author")
        html = all_books.body.decode()
        assert "All books" in html
        assert "sort=author" in html
        assert "Add to shelf" in html
        assert html.count('name="book_ids"') == 3

        all_authors = library(request, db, view="authors")
        assert "All Authors" in all_authors.body.decode()

        all_series = library(request, db, view="series")
        assert "All Series" in all_series.body.decode()


def test_book_detail_navigation_follows_originating_sort_and_edit_returns_to_book() -> None:
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    with Session(engine, expire_on_commit=False) as db:
        user = User(
            username="admin",
            password_hash=hash_password("a-long-test-password"),
            role=Role.ADMIN,
        )
        books = [
            Book(title="Zulu", primary_author="Author A", review_state=ReviewState.READY),
            Book(title="Alpha", primary_author="Author B", review_state=ReviewState.READY),
            Book(title="Mike", primary_author="Author C", review_state=ReviewState.READY),
        ]
        db.add_all([user, *books])
        db.commit()
        request = Request(
            {
                "type": "http",
                "method": "GET",
                "path": f"/books/{books[1].id}",
                "headers": [],
                "query_string": b"",
                "session": {"user_id": user.id},
            }
        )
        listing = "/?view=all&sort=author"

        response = book_detail(
            books[1].id,
            request,
            db,
            return_to=listing,
            navigation=listing,
        )
        html = response.body.decode()

        assert "Previous book" in html and books[0].id in html
        assert "Next book" in html and books[2].id in html
        assert "Edit metadata" in html
        assert f"return_to=/books/{books[1].id}%3Freturn_to" in html


def test_book_detail_displays_progress_received_from_kobo() -> None:
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    with Session(engine, expire_on_commit=False) as db:
        user = User(
            username="reader",
            password_hash=hash_password("a-long-test-password"),
            role=Role.USER,
        )
        book = Book(title="Book", primary_author="Author", review_state=ReviewState.READY)
        db.add_all([user, book])
        db.flush()
        db.add(
            ReadingState(
                user_id=user.id,
                book_id=book.id,
                state="reading",
                progress_percent=42.5,
                spent_reading_minutes=50,
                remaining_time_minutes=70,
                location_json='{"Value": "chapter-5", "Type": "KoboSpan"}',
            )
        )
        db.commit()
        request = Request(
            {
                "type": "http",
                "method": "GET",
                "path": f"/books/{book.id}",
                "headers": [],
                "query_string": b"",
                "session": {"user_id": user.id},
            }
        )

        html = book_detail(book.id, request, db).body.decode()

        assert "Kobo progress: 42.5%" in html
        assert "50 minutes read" in html
        assert "70 minutes remaining" in html
        assert "Location: chapter-5" in html


def test_metadata_suggestions_use_existing_authors_and_series_case_insensitively() -> None:
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    with Session(engine) as db:
        db.add_all(
            [
                Book(title="One", primary_author="Adrian Tchaikovsky", series="Dogs of War"),
                Book(title="Two", primary_author="adrian tchaikovsky", series="dogs of war"),
                Book(title="Three", primary_author="Martha Wells", series=None),
            ]
        )
        db.commit()

        suggestions = metadata_suggestions(db)

        assert suggestions["author_suggestions"] == ["Adrian Tchaikovsky", "Martha Wells"]
        assert suggestions["series_suggestions"] == ["Dogs of War"]


def test_metadata_providers_are_only_queried_after_search(monkeypatch) -> None:
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    with Session(engine, expire_on_commit=False) as db:
        admin = User(
            username="admin",
            password_hash=hash_password("a-long-test-password"),
            role=Role.ADMIN,
        )
        book = Book(title="Book", primary_author="Author", review_state=ReviewState.READY)
        db.add_all([admin, book])
        db.commit()
        request = Request(
            {
                "type": "http",
                "method": "GET",
                "path": f"/review/{book.id}",
                "headers": [],
                "query_string": b"",
                "session": {"user_id": admin.id},
            }
        )
        searches = []
        monkeypatch.setattr(
            "digest.main.find_candidates",
            lambda *args, **kwargs: searches.append(kwargs) or [],
        )

        initial = review_book(book.id, request, db).body.decode()

        assert searches == []
        assert "Search results" not in initial
        assert "Suggested provider" not in initial

        searched = review_book(
            book.id,
            request,
            db,
            search_title="Different title",
            search_author="Different author",
        ).body.decode()
        assert searches == [{"title": "Different title", "author": "Different author", "isbns": []}]
        assert "Search results" in searched


def test_sort_controls_reverse_and_missing_metadata_filter_is_composable() -> None:
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    with Session(engine, expire_on_commit=False) as db:
        user = User(
            username="reader",
            password_hash=hash_password("a-long-test-password"),
            role=Role.USER,
        )
        db.add_all(
            [
                user,
                Book(
                    title="Alpha",
                    primary_author="Author",
                    cover_path="/cover.jpg",
                    description="Complete",
                    review_state=ReviewState.READY,
                ),
                Book(
                    title="Zulu",
                    primary_author="Author",
                    description=None,
                    review_state=ReviewState.READY,
                ),
            ]
        )
        db.commit()
        request = Request(
            {
                "type": "http",
                "method": "GET",
                "path": "/",
                "headers": [],
                "query_string": b"view=all&sort=title&direction=asc",
                "session": {"user_id": user.id},
            }
        )

        ascending = library(request, db, view="all", sort="title", direction="asc").body.decode()
        assert ascending.index("Alpha") < ascending.index("Zulu")
        assert "sort=title&direction=desc" in ascending
        assert "Title ↑" in ascending

        descending = library(request, db, view="all", sort="title", direction="desc").body.decode()
        assert descending.index("Zulu") < descending.index("Alpha")
        assert "sort=title&direction=asc" in descending

        incomplete = library(
            request,
            db,
            view="all",
            sort="title",
            direction="asc",
            metadata="missing",
        ).body.decode()
        assert "Zulu" in incomplete and "Alpha" not in incomplete
        assert "Missing metadata" in incomplete


def test_reading_state_and_favourites_are_personal_library_views() -> None:
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    with Session(engine, expire_on_commit=False) as db:
        user = User(
            username="reader",
            password_hash=hash_password("a-long-test-password"),
            role=Role.USER,
        )
        book = Book(title="Personal Book", primary_author="Author", review_state=ReviewState.READY)
        db.add_all([user, book])
        db.commit()
        request = Request(
            {
                "type": "http",
                "method": "POST",
                "path": f"/books/{book.id}/reading-state",
                "headers": [],
                "query_string": b"",
                "session": {"user_id": user.id, "csrf": "token"},
            }
        )

        response = update_reading_state(
            book.id,
            request,
            db,
            form_csrf="token",
            state="reading",
            favourite="true",
            rating=4,
            return_to=f"/books/{book.id}",
        )

        saved = db.scalar(select(ReadingState))
        assert response.status_code == 303
        assert saved is not None
        assert saved.state == "reading" and saved.favourite is True and saved.rating == 4

        request.scope["method"] = "GET"
        request.scope["path"] = "/"
        home = library(request, db).body.decode()
        assert "Currently Reading" in home and "Favourites" in home and "Rated Books" in home

        reading = library(request, db, view="reading").body.decode()
        assert "Currently reading" in reading and "Personal Book" in reading

        rated = library(request, db, view="rated").body.decode()
        assert "Rated books" in rated and "Personal Book" in rated
