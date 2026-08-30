from io import BytesIO
from pathlib import Path

import pytest
from PIL import Image
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from digest.db import Base
from digest.metadata import (
    MAX_COVER_UPLOAD_BYTES,
    apply_candidate,
    apply_manual_metadata,
    auto_scrape_book,
    download_cover,
    normalise_uploaded_cover,
    refresh_book,
    save_uploaded_cover,
)
from digest.models import Book, BookFile, ReviewState
from digest.models import AppSetting
from digest.providers import Candidate


class ImageResponse:
    def __init__(self, content: bytes):
        self.headers = {"content-type": "image/png"}
        self.content = content

    def raise_for_status(self) -> None:
        return None


def test_downloaded_cover_is_verified_and_normalised_to_jpeg(tmp_path: Path, monkeypatch) -> None:
    ebook = tmp_path / "book.epub"
    ebook.touch()
    book = Book(title="Book", primary_author="Author")
    book.files = [
        BookFile(
            path=str(ebook),
            sha256="0" * 64,
            format="epub",
            size_bytes=0,
            modified_ns=0,
        )
    ]
    source = BytesIO()
    Image.new("RGBA", (16, 24), (120, 40, 80, 128)).save(source, format="PNG")
    monkeypatch.setattr(
        "digest.metadata.httpx.get",
        lambda *args, **kwargs: ImageResponse(source.getvalue()),
    )

    download_cover(book, "https://covers.example.test/book.png")

    cover = tmp_path / "cover.jpg"
    assert book.cover_path == str(cover)
    assert book.updated_at is not None
    with Image.open(cover) as image:
        assert image.format == "JPEG"
        assert image.mode == "RGB"


def test_uploaded_cover_is_normalised_saved_and_assigned(tmp_path: Path, monkeypatch) -> None:
    ebook = tmp_path / "book.epub"
    ebook.touch()
    source = BytesIO()
    Image.new("RGBA", (20, 30), (10, 20, 200, 100)).save(source, format="PNG")
    book = Book(title="Book", primary_author="Author")
    book.files = [
        BookFile(
            path=str(ebook),
            sha256="0" * 64,
            format="epub",
            size_bytes=0,
            modified_ns=0,
        )
    ]
    monkeypatch.setattr("digest.metadata.write_approved_metadata", lambda book: None)
    monkeypatch.setattr("digest.metadata.write_sidecars", lambda book: None)

    save_uploaded_cover(book, normalise_uploaded_cover(source.getvalue()))

    cover = tmp_path / "cover.jpg"
    assert book.cover_path == str(cover)
    assert book.updated_at is not None
    with Image.open(cover) as image:
        assert image.format == "JPEG"
        assert image.size == (20, 30)


def test_uploaded_cover_rejects_invalid_or_oversized_files() -> None:
    with pytest.raises(ValueError, match="valid image"):
        normalise_uploaded_cover(b"not an image")
    with pytest.raises(ValueError, match="10 MB"):
        normalise_uploaded_cover(b"x" * (MAX_COVER_UPLOAD_BYTES + 1))


def metadata_session() -> Session:
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    return Session(engine, expire_on_commit=False)


def test_auto_scrape_rejects_low_confidence_or_unknown_language(monkeypatch) -> None:
    with metadata_session() as db:
        book = Book(title="The Right Book", primary_author="Correct Author")
        db.add_all([book, AppSetting(key="default_language", value="en")])
        db.commit()
        applied = []
        monkeypatch.setattr("digest.metadata.apply_candidate",
                            lambda *args, **kwargs: applied.append(kwargs))
        monkeypatch.setattr("digest.metadata.find_candidates", lambda db, book: [{
            "title": "Unrelated Foreign Edition", "authors": ["Someone Else"],
            "language": None, "confidence": 0.51, "source": "test",
        }])

        assert auto_scrape_book(db, book) is False
        assert applied == []


def test_auto_scrape_applies_strong_match_in_default_language(monkeypatch) -> None:
    with metadata_session() as db:
        book = Book(title="The Right Book", primary_author="Correct Author")
        db.add_all([book, AppSetting(key="default_language", value="en")])
        db.commit()
        applied = []
        monkeypatch.setattr("digest.metadata.apply_candidate",
                            lambda *args, **kwargs: applied.append((args, kwargs)))
        monkeypatch.setattr("digest.metadata.find_candidates", lambda db, book: [{
            "title": "The Right Book", "authors": ["Correct Author"],
            "language": "eng", "confidence": 0.98, "source": "test",
        }])

        assert auto_scrape_book(db, book) is True
        assert applied[0][1] == {"organise": True, "replace_existing": True}


def test_manual_metadata_is_validated_saved_and_locked(monkeypatch) -> None:
    monkeypatch.setattr("digest.metadata.organise_book", lambda db, book: None)
    with metadata_session() as db:
        book = Book(title="Old", primary_author="Old Author", review_state=ReviewState.REVIEW)
        db.add(book)
        db.commit()

        apply_manual_metadata(
            db,
            book,
            {
                "title": "New Title",
                "authors": "First Author, Second Author",
                "isbns": "9781234567890",
                "language": "en-gb",
                "description": "Description",
                "publication_date": "2025-04-03",
                "page_count": "321",
                "series": "A Series",
                "series_number": "4.5",
            },
            ["title", "authors", "not-a-field"],
        )

        assert book.title == "New Title"
        assert book.primary_author == "First Author"
        assert book.page_count == 321
        assert book.series_number == 4.5
        assert book.locked_fields_json == '["authors", "title"]'
        assert book.review_state == ReviewState.READY


def test_manual_metadata_allows_blank_optional_fields(monkeypatch) -> None:
    monkeypatch.setattr("digest.metadata.organise_book", lambda db, book: None)
    with metadata_session() as db:
        book = Book(
            title="Existing Title",
            primary_author="Existing Author",
            authors_json='["Existing Author"]',
            description="Old description",
            review_state=ReviewState.REVIEW,
        )
        db.add(book)
        db.commit()

        apply_manual_metadata(db, book, {}, [])

        assert book.title == "Existing Title"
        assert book.primary_author == "Existing Author"
        assert book.description is None
        assert book.page_count is None
        assert book.review_state == ReviewState.READY


def test_provider_refresh_preserves_locked_author_fields() -> None:
    with metadata_session() as db:
        book = Book(
            title="Old",
            primary_author="Locked Author",
            authors_json='["Locked Author"]',
            locked_fields_json='["authors"]',
        )
        db.add(book)
        db.commit()

        apply_candidate(
            db,
            book,
            {
                "title": "Updated",
                "authors": ["Provider Author"],
                "source": "test",
                "confidence": 1,
            },
            organise=False,
        )

        assert book.title == "Updated"
        assert book.primary_author == "Locked Author"
        assert book.authors_json == '["Locked Author"]'


def test_explicit_provider_match_replaces_locked_and_stale_metadata(monkeypatch) -> None:
    monkeypatch.setattr("digest.metadata.organise_book", lambda db, book: None)
    with metadata_session() as db:
        book = Book(
            title="Old Title",
            primary_author="Old Author",
            authors_json='["Old Author"]',
            isbns_json='["old-isbn"]',
            description="Old description",
            language="en",
            series="Old Series",
            series_number=2,
            locked_fields_json='["authors", "title"]',
        )
        db.add(book)
        db.commit()

        apply_candidate(
            db,
            book,
            {
                "title": "Selected Title",
                "authors": ["Selected Author"],
                "isbns": [],
                "description": "Selected description",
                "language": None,
                "series": None,
                "series_number": None,
                "source": "test",
            },
            organise=True,
            replace_existing=True,
        )

        assert book.title == "Selected Title"
        assert book.primary_author == "Selected Author"
        assert book.authors_json == '["Selected Author"]'
        assert book.isbns_json == "[]"
        assert book.description == "Selected description"
        assert book.language is None
        assert book.series is None
        assert book.series_number is None


def test_explicit_provider_match_overwrites_an_existing_cover(
    tmp_path: Path, monkeypatch
) -> None:
    ebook = tmp_path / "book.epub"
    ebook.touch()
    cover = tmp_path / "cover.jpg"
    Image.new("RGB", (16, 24), "red").save(cover)
    replacement = BytesIO()
    Image.new("RGB", (16, 24), "blue").save(replacement, format="PNG")
    monkeypatch.setattr(
        "digest.metadata.httpx.get",
        lambda *args, **kwargs: ImageResponse(replacement.getvalue()),
    )
    monkeypatch.setattr("digest.metadata.organise_book", lambda db, book: None)
    monkeypatch.setattr("digest.metadata.write_approved_metadata", lambda book: None)
    monkeypatch.setattr("digest.metadata.write_sidecars", lambda book: None)
    with metadata_session() as db:
        book = Book(title="Book", primary_author="Author", cover_path=str(cover))
        book.files = [
            BookFile(
                path=str(ebook),
                sha256="0" * 64,
                format="epub",
                size_bytes=0,
                modified_ns=0,
            )
        ]
        db.add(book)
        db.commit()

        apply_candidate(
            db,
            book,
            {"title": "Book", "authors": ["Author"], "cover_url": "https://covers/new"},
            replace_existing=True,
        )

        with Image.open(cover) as image:
            red, _green, blue = image.getpixel((0, 0))
        assert blue > red


def test_refresh_applies_a_confident_match_without_reorganising(monkeypatch) -> None:
    monkeypatch.setattr(
        "digest.metadata.find_candidates",
        lambda db, book: [
            {
                "title": "Refreshed",
                "authors": ["Author"],
                "source": "test",
                "confidence": 0.99,
            }
        ],
    )
    monkeypatch.setattr("digest.metadata.write_approved_metadata", lambda book: None)
    monkeypatch.setattr("digest.metadata.write_sidecars", lambda book: None)
    with metadata_session() as db:
        book = Book(title="Old", primary_author="Author", review_state=ReviewState.READY)
        db.add(book)
        db.commit()

        changed = refresh_book(db, book)

        assert changed is True
        assert book.title == "Refreshed"
        assert book.review_state == ReviewState.READY


def test_failed_scheduled_refresh_does_not_hide_a_ready_book(monkeypatch) -> None:
    monkeypatch.setattr("digest.metadata.find_candidates", lambda db, book: [])
    with metadata_session() as db:
        book = Book(title="Keep Me", primary_author="Author", review_state=ReviewState.READY)
        db.add(book)
        db.commit()

        changed = refresh_book(db, book)

        assert changed is False
        assert book.review_state == ReviewState.READY


def test_manual_provider_search_uses_supplied_isbn_title_and_author(monkeypatch) -> None:
    calls = []

    class Provider:
        def search(self, title, author, isbns):
            calls.append((title, author, isbns))
            return [
                Candidate(
                    source="test",
                    source_id="1",
                    title="Different Book",
                    authors=["Different Author"],
                    isbns=["9781234567890"],
                )
            ]

    monkeypatch.setattr(
        "digest.metadata.available_providers", lambda config: {"openlibrary": Provider()}
    )
    with metadata_session() as db:
        book = Book(title="Original", primary_author="Original Author")
        db.add(book)
        db.commit()

        from digest.metadata import find_candidates

        results = find_candidates(
            db,
            book,
            title="Different Book",
            author="Different Author",
            isbns=["9781234567890"],
        )

        assert calls == [("Different Book", "Different Author", ["9781234567890"])]
        assert results[0]["title"] == "Different Book"
        assert results[0]["confidence"] == 1


def test_metadata_search_normalises_punctuated_author_initials(monkeypatch) -> None:
    calls = []

    class Provider:
        def search(self, title, author, isbns):
            calls.append((title, author, isbns))
            return [Candidate(source="test", source_id="1", title=title, authors=["JB Turner"])]

    monkeypatch.setattr(
        "digest.metadata.available_providers", lambda config: {"openlibrary": Provider()}
    )
    with metadata_session() as db:
        book = Book(title="A Book", primary_author="J. B. Turner")
        db.add(book)
        db.commit()

        from digest.metadata import find_candidates

        results = find_candidates(db, book)

    assert calls == [("A Book", "JB Turner", [])]
    assert results[0]["confidence"] == 1
