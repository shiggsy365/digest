import json
from pathlib import Path

import pytest
from fastapi import HTTPException
from sqlalchemy import create_engine
from sqlalchemy.orm import Session
from starlette.requests import Request

from digest.db import Base
from digest.kobo import (
    archive_from_device,
    create_tag,
    initialization,
    kobo_user,
    metadata,
    preferred_file,
    reading_state_payload,
    remove_tag_items,
    shelf_books,
    shelf_tag_id,
    sync_payload,
    sync_pending,
    update_reading_state,
    update_tag,
)
from digest.models import (
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
)
from digest.security import KOBO_TOKEN_NAME, current_user, hash_password
from digest.tokens import create_token


def kobo_session() -> Session:
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    return Session(engine, expire_on_commit=False)


def test_all_books_can_be_used_as_kobo_sync_source() -> None:
    with kobo_session() as db:
        user = User(username="reader", password_hash="hash", kobo_sync_all_books=True)
        ready = Book(title="Ready", primary_author="Writer", review_state=ReviewState.READY)
        review = Book(title="Review", primary_author="Writer", review_state=ReviewState.REVIEW)
        db.add_all([user, ready, review])
        db.commit()

        assert [book.id for book in shelf_books(db, user)] == [ready.id]


def make_user(db: Session) -> User:
    user = User(
        username="reader",
        password_hash=hash_password("a-long-test-password"),
        role=Role.USER,
    )
    db.add(user)
    db.commit()
    return user


def test_kobo_path_token_authenticates_only_an_active_device_token() -> None:
    with kobo_session() as db:
        user = make_user(db)
        item, plain = create_token(db, user, user, KOBO_TOKEN_NAME)

        assert kobo_user(db, plain).id == user.id
        request = Request(
            {
                "type": "http",
                "method": "GET",
                "path": "/api/books",
                "headers": [(b"authorization", f"Bearer {plain}".encode())],
                "query_string": b"",
                "session": {},
            }
        )
        with pytest.raises(HTTPException) as api_exc:
            current_user(request, db)
        assert api_exc.value.status_code == 401

        item.revoked_at = item.created_at
        db.commit()
        with pytest.raises(HTTPException) as exc:
            kobo_user(db, plain)
        assert exc.value.status_code == 401


def test_initialization_routes_library_operations_to_configured_public_url() -> None:
    resources = initialization("https://digest.example", "secret")["Resources"]

    assert resources["library_sync"] == (
        "https://digest.example/kobo/secret/v1/library/sync"
    )
    assert resources["delete_entitlement"].endswith("/v1/library/{Ids}")
    assert resources["reading_state"].endswith("/v1/library/{Ids}/state")
    assert resources["tags"].endswith("/v1/library/tags")
    assert resources["device_auth"].endswith("/v1/auth/device")
    assert resources["configuration_data"].startswith("https://storeapi.kobo.com/")


def test_sync_contains_only_compatible_books_on_selected_shelf(tmp_path: Path) -> None:
    with kobo_session() as db:
        user = make_user(db)
        shelf = Shelf(name="Kobo", owner_id=user.id)
        included = Book(
            title="Bee Speaker",
            primary_author="Adrian Tchaikovsky",
            authors_json=json.dumps(["Adrian Tchaikovsky"]),
            series="Dogs of War",
            series_number=3,
            review_state=ReviewState.READY,
        )
        excluded = Book(
            title="Elsewhere", primary_author="Somebody", review_state=ReviewState.READY
        )
        path = tmp_path / "bee-speaker.kepub"
        path.write_bytes(b"book")
        db.add_all([shelf, included, excluded])
        db.flush()
        db.add_all(
            [
                ShelfBook(shelf_id=shelf.id, book_id=included.id),
                BookFile(
                    book_id=included.id,
                    path=str(path),
                    sha256="a" * 64,
                    format="kepub",
                    size_bytes=4,
                    modified_ns=1,
                ),
            ]
        )
        user.kobo_sync_shelf_id = shelf.id
        db.commit()

        result = sync_payload(db, user, "https://digest.example", "secret")

        payload = next(item["NewEntitlement"] for item in result if "NewEntitlement" in item)
        assert payload["BookMetadata"]["Title"] == "Bee Speaker"
        assert payload["BookMetadata"]["Series"]["Number"] == 3
        assert payload["BookMetadata"]["DownloadUrls"][0]["Format"] == "KEPUB"
        assert payload["BookMetadata"]["DownloadUrls"][0]["Url"].endswith(
            f"/download/{included.id}/kepub"
        )
        assert payload["ReadingState"]["StatusInfo"]["Status"] == "ReadyToRead"
        assert excluded.id not in json.dumps(result)
        assert sync_payload(db, user, "https://digest.example", "secret") == []
        assert db.query(KoboSyncedBook).count() == 1
        assert db.query(KoboSyncedShelf).count() == 1

        included.title = "Bee Speaker Updated"
        db.commit()
        changed = sync_payload(db, user, "https://digest.example", "secret")
        assert changed[0]["ChangedEntitlement"]["BookMetadata"]["Title"] == (
            "Bee Speaker Updated"
        )

        state = db.query(ReadingState).filter_by(user_id=user.id, book_id=included.id).one()
        state.state = "reading"
        state.progress_percent = 12
        db.commit()
        state_change = sync_payload(db, user, "https://digest.example", "secret")
        assert state_change[0]["ChangedReadingState"]["ReadingState"]["StatusInfo"][
            "Status"
        ] == "Reading"

        db.query(ShelfBook).filter_by(shelf_id=shelf.id, book_id=included.id).delete()
        db.commit()
        removed = sync_payload(db, user, "https://digest.example", "secret")
        removed_entitlement = removed[0]["ChangedEntitlement"]["BookEntitlement"]
        assert removed_entitlement["Id"] == included.id
        assert removed_entitlement["IsRemoved"] is True
        assert db.query(KoboSyncedBook).count() == 0


def test_large_backlog_is_spread_across_multiple_syncs(tmp_path: Path) -> None:
    with kobo_session() as db:
        user = make_user(db)
        user.kobo_sync_all_books = True
        book_count = 120
        for index in range(book_count):
            path = tmp_path / f"book-{index}.kepub"
            path.write_bytes(b"book")
            book = Book(
                title=f"Book {index}",
                primary_author="Author",
                review_state=ReviewState.READY,
            )
            db.add(book)
            db.flush()
            db.add(
                BookFile(
                    book_id=book.id,
                    path=str(path),
                    sha256=f"{index:064x}",
                    format="kepub",
                    size_bytes=4,
                    modified_ns=1,
                )
            )
        db.commit()

        first = sync_payload(db, user, "https://digest.example", "secret")
        assert len(first) == 50
        assert db.query(KoboSyncedBook).count() == 50
        assert sync_pending(db, user) is True

        second = sync_payload(db, user, "https://digest.example", "secret")
        assert len(second) == 50
        assert db.query(KoboSyncedBook).count() == 100

        third = sync_payload(db, user, "https://digest.example", "secret")
        assert len(third) == book_count - 100
        assert db.query(KoboSyncedBook).count() == book_count
        assert sync_pending(db, user) is False

        assert sync_payload(db, user, "https://digest.example", "secret") == []


def test_kepub_is_preferred_and_metadata_falls_back_to_epub(tmp_path: Path) -> None:
    epub = tmp_path / "book.epub"
    kepub = tmp_path / "book.kepub"
    epub.write_bytes(b"epub")
    kepub.write_bytes(b"kepub")
    book = Book(
        title="A Book",
        primary_author="An Author",
        authors_json="[]",
        review_state=ReviewState.READY,
    )
    book.files = [
        BookFile(path=str(epub), sha256="a" * 64, format="epub", size_bytes=4, modified_ns=1),
        BookFile(path=str(kepub), sha256="b" * 64, format="kepub", size_bytes=5, modified_ns=2),
    ]

    assert preferred_file(book).format == "kepub"
    assert metadata(book, "https://digest.example", "secret")["Contributors"] == ["An Author"]


def test_kobo_progress_updates_personal_reading_state() -> None:
    with kobo_session() as db:
        user = make_user(db)
        book = Book(title="Book", primary_author="Author", review_state=ReviewState.READY)
        db.add(book)
        db.commit()

        state, result = update_reading_state(
            db,
            user,
            book,
            {
                "ReadingStates": [
                    {
                        "CurrentBookmark": {
                            "ProgressPercent": 37.5,
                            "ContentSourceProgressPercent": 37.5,
                            "Location": {
                                "Value": "chapter-4",
                                "Type": "KoboSpan",
                                "Source": "book",
                            },
                        },
                        "Statistics": {
                            "SpentReadingMinutes": 42,
                            "RemainingTimeMinutes": 90,
                        },
                        "StatusInfo": {"Status": "Reading"},
                    }
                ]
            },
        )

        assert state.user_id == user.id
        assert state.state == "reading"
        assert state.progress_percent == 37.5
        assert state.spent_reading_minutes == 42
        assert json.loads(state.location_json)["Value"] == "chapter-4"
        assert result["StatusInfoResult"] == {"Result": "Success"}
        response = reading_state_payload(book, state)
        assert response["CurrentBookmark"]["ProgressPercent"] == 37.5
        assert response["StatusInfo"]["Status"] == "Reading"


def test_kobo_progress_rejects_invalid_values_without_creating_state() -> None:
    with kobo_session() as db:
        user = make_user(db)
        book = Book(title="Book", primary_author="Author", review_state=ReviewState.READY)
        db.add(book)
        db.commit()

        with pytest.raises(ValueError, match="progress"):
            update_reading_state(
                db,
                user,
                book,
                {"ReadingStates": [{"CurrentBookmark": {"ProgressPercent": 120}}]},
            )

        assert db.query(ReadingState).count() == 0


def test_device_archive_removes_owned_shelf_item_but_not_shared_item() -> None:
    with kobo_session() as db:
        user = make_user(db)
        owner = User(
            username="owner",
            password_hash=hash_password("a-long-test-password"),
            role=Role.ADMIN,
        )
        owned = Shelf(name="Mine", owner_id=user.id)
        shared = Shelf(name="Shared", owner_id=None, shared=True)
        first = Book(title="First", primary_author="Author")
        second = Book(title="Second", primary_author="Author")
        db.add_all([owner, owned, shared, first, second])
        db.flush()
        shared.owner_id = owner.id
        db.add_all(
            [
                ShelfBook(shelf_id=owned.id, book_id=first.id),
                ShelfBook(shelf_id=shared.id, book_id=second.id),
                KoboSyncedBook(
                    user_id=user.id,
                    book_id=first.id,
                    book_revision="book",
                    reading_revision="state",
                ),
                KoboSyncedBook(
                    user_id=user.id,
                    book_id=second.id,
                    book_revision="book",
                    reading_revision="state",
                ),
            ]
        )
        user.kobo_sync_shelf_id = owned.id
        db.commit()

        archive_from_device(db, user, first.id)
        assert db.query(ShelfBook).filter_by(shelf_id=owned.id).count() == 0
        assert db.query(KoboSyncedBook).filter_by(book_id=first.id).count() == 0

        user.kobo_sync_shelf_id = shared.id
        db.commit()
        archive_from_device(db, user, second.id)
        assert db.query(ShelfBook).filter_by(shelf_id=shared.id).count() == 1
        assert db.query(KoboSyncedBook).filter_by(book_id=second.id).one().archived is True


def test_device_archive_ignores_books_from_a_previous_sync_server() -> None:
    with kobo_session() as db:
        user = make_user(db)
        user.kobo_sync_all_books = True
        db.commit()

        archive_from_device(db, user, "123")

        assert db.query(KoboSyncedBook).count() == 0


def test_kobo_collections_create_rename_and_remove_items() -> None:
    with kobo_session() as db:
        user = make_user(db)
        book = Book(title="Book", primary_author="Author")
        db.add(book)
        db.flush()
        db.add(
            KoboSyncedBook(
                user_id=user.id,
                book_id=book.id,
                book_revision="book",
                reading_revision="state",
            )
        )
        db.commit()
        item = {"RevisionId": book.id, "Type": "ProductRevisionTagItem"}

        shelf = create_tag(db, user, {"Name": "On Kobo", "Items": [item]})
        assert shelf.owner_id == user.id
        assert db.query(ShelfBook).filter_by(shelf_id=shelf.id, book_id=book.id).count() == 1
        assert shelf_tag_id(shelf.id)

        update_tag(db, user, shelf, {"Name": "Renamed"})
        assert shelf.name == "Renamed"
        remove_tag_items(db, user, shelf, {"Items": [item]})
        assert db.query(ShelfBook).filter_by(shelf_id=shelf.id).count() == 0
