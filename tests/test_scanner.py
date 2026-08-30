import json
import zipfile
from pathlib import Path
from types import SimpleNamespace
from xml.etree import ElementTree

from sqlalchemy import create_engine, func, select
from sqlalchemy.orm import Session

from digest.db import Base
from digest.library import (
    delete_book,
    group_logical_books,
    organise_book,
    read_epub,
    reconcile_sidecars,
    scan_library,
    write_approved_metadata,
)
from digest.models import Book, BookFile, ReviewState


def make_epub(path: Path, *, title: str = "Test Book", author: str = "Test Author") -> None:
    container = """<?xml version="1.0"?>
    <container xmlns="urn:oasis:names:tc:opendocument:xmlns:container">
      <rootfiles><rootfile full-path="content.opf"/></rootfiles>
    </container>"""
    package = f"""<?xml version="1.0"?>
    <package xmlns="http://www.idpf.org/2007/opf">
      <metadata xmlns:dc="http://purl.org/dc/elements/1.1/">
        <dc:title>{title}</dc:title><dc:creator>{author}</dc:creator>
      </metadata>
      <manifest></manifest><spine></spine>
    </package>"""
    with zipfile.ZipFile(path, "w") as archive:
        archive.writestr("META-INF/container.xml", container)
        archive.writestr("content.opf", package)


def add_font_obfuscation(path: Path) -> None:
    encryption = """<encryption xmlns:enc="http://www.w3.org/2001/04/xmlenc#">
      <enc:EncryptedData>
        <enc:EncryptionMethod Algorithm="http://ns.adobe.com/pdf/enc#RC"/>
        <enc:CipherData><enc:CipherReference URI="fonts/book.ttf"/></enc:CipherData>
      </enc:EncryptedData>
    </encryption>"""
    with zipfile.ZipFile(path, "a") as archive:
        archive.writestr("META-INF/encryption.xml", encryption)


def write_calibre_sidecars(directory: Path) -> None:
    opf = """<package xmlns="http://www.idpf.org/2007/opf">
      <metadata xmlns:dc="http://purl.org/dc/elements/1.1/">
        <dc:title>Sidecar Title</dc:title>
        <dc:creator>Sidecar Author</dc:creator>
        <dc:identifier>9781234567890</dc:identifier>
        <dc:language>en-gb</dc:language>
        <dc:date>2025-04-03</dc:date>
        <meta name="calibre:series" content="Sidecar Series"/>
        <meta name="calibre:series_index" content="4"/>
        <meta name="calibre:pages" content="321"/>
      </metadata>
    </package>"""
    (directory / "metadata.opf").write_text(opf)
    (directory / "cover.jpg").write_bytes(b"sidecar-cover")


def scanner_session() -> Session:
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    return Session(engine, expire_on_commit=False)


def use_library(monkeypatch, root: Path) -> None:
    monkeypatch.setattr("digest.library.get_settings", lambda: SimpleNamespace(library_root=root))


def test_initial_scan_retains_and_indexes_exact_duplicates(tmp_path: Path, monkeypatch) -> None:
    use_library(monkeypatch, tmp_path)
    first = tmp_path / "first.epub"
    second = tmp_path / "second.epub"
    make_epub(first)
    second.write_bytes(first.read_bytes())

    with scanner_session() as db:
        stats = scan_library(db, initial=True)

        assert first.exists() and second.exists()
        assert stats == {"added": 2, "removed": 0, "duplicates": 1, "rejected": 0}
        assert db.scalar(select(func.count(BookFile.id))) == 2
        assert db.scalar(select(func.count(Book.id))) == 2
        assert set(db.scalars(select(Book.review_state))) == {ReviewState.READY}
        assert {Path(item.path) for item in db.scalars(select(BookFile))} == {first, second}


def test_post_baseline_addition_enters_review(tmp_path: Path, monkeypatch) -> None:
    use_library(monkeypatch, tmp_path)
    baseline = tmp_path / "baseline.epub"
    make_epub(baseline)

    with scanner_session() as db:
        scan_library(db, initial=True)
        addition = tmp_path / "addition.epub"
        make_epub(addition, title="Later Addition")

        scan_library(db, initial=False)

        added_book = db.scalar(select(Book).where(Book.title == "Later Addition"))
        assert added_book is not None
        assert added_book.review_state == ReviewState.REVIEW
        assert added_book.review_reason == "Metadata match required"


def test_old_font_obfuscation_rejection_is_reclassified_as_ready(
    tmp_path: Path, monkeypatch
) -> None:
    use_library(monkeypatch, tmp_path)
    path = tmp_path / "font-obfuscated.epub"
    make_epub(path)
    add_font_obfuscation(path)

    with scanner_session() as db:
        scan_library(db, initial=True)
        book = db.scalar(select(Book))
        book_file = db.scalar(select(BookFile))
        assert book is not None and book_file is not None
        book.review_state = ReviewState.REJECTED
        book.review_reason = "DRM or unsupported container"
        book_file.drm_rejected = True
        db.commit()

        scan_library(db, initial=False)

        assert book.review_state == ReviewState.READY
        assert book.review_reason is None
        assert book_file.drm_rejected is False
        assert path.exists()


def test_later_exact_duplicate_is_deleted(tmp_path: Path, monkeypatch) -> None:
    use_library(monkeypatch, tmp_path)
    original = tmp_path / "original.epub"
    make_epub(original)

    with scanner_session() as db:
        scan_library(db, initial=True)
        duplicate = tmp_path / "later-copy.epub"
        duplicate.write_bytes(original.read_bytes())

        stats = scan_library(db, initial=False)

        assert not duplicate.exists()
        assert original.exists()
        assert stats["duplicates"] == 1
        assert db.scalar(select(func.count(BookFile.id))) == 1


def test_removing_final_file_deletes_its_book_record(tmp_path: Path, monkeypatch) -> None:
    use_library(monkeypatch, tmp_path)
    path = tmp_path / "book.epub"
    make_epub(path)

    with scanner_session() as db:
        scan_library(db, initial=True)
        path.unlink()

        stats = scan_library(db, initial=False)

        assert stats["removed"] == 1
        assert db.scalar(select(func.count(BookFile.id))) == 0
        assert db.scalar(select(func.count(Book.id))) == 0


def test_file_replaced_in_place_is_rehashed_and_returned_to_review(
    tmp_path: Path, monkeypatch
) -> None:
    use_library(monkeypatch, tmp_path)
    path = tmp_path / "book.epub"
    make_epub(path, title="Old Title")

    with scanner_session() as db:
        scan_library(db, initial=True)
        book_file = db.scalar(select(BookFile))
        book = db.scalar(select(Book))
        assert book_file is not None and book is not None
        old_hash = book_file.sha256
        book.review_state = ReviewState.READY
        db.commit()

        make_epub(path, title="Replacement Title")
        scan_library(db, initial=False)
        db.refresh(book_file)
        db.refresh(book)

        assert book_file.sha256 != old_hash
        assert book.title == "Replacement Title"
        assert book.review_state == ReviewState.REVIEW
        assert book.review_reason == "File changed; metadata match required"


def test_file_replaced_with_existing_content_is_deleted_as_duplicate(
    tmp_path: Path, monkeypatch
) -> None:
    use_library(monkeypatch, tmp_path)
    first = tmp_path / "first.epub"
    second = tmp_path / "second.epub"
    make_epub(first, title="First")
    make_epub(second, title="Second")

    with scanner_session() as db:
        scan_library(db, initial=True)
        second.write_bytes(first.read_bytes())

        stats = scan_library(db, initial=False)

        assert first.exists()
        assert not second.exists()
        assert stats["duplicates"] == 1
        assert db.scalar(select(func.count(BookFile.id))) == 1
        assert db.scalar(select(func.count(Book.id))) == 1


def test_approval_organises_file_and_writes_atomic_sidecar(tmp_path: Path, monkeypatch) -> None:
    use_library(monkeypatch, tmp_path)
    source = tmp_path / "incoming.epub"
    make_epub(source, title="A/B", author="Léonie: Writer")

    with scanner_session() as db:
        scan_library(db, initial=True)
        book = db.scalar(select(Book))
        assert book is not None

        organise_book(db, book)

        expected = tmp_path / "Léonie- Writer" / "A-B" / "Léonie- Writer - A-B.epub"
        sidecar = expected.parent / "metadata.json"
        assert expected.exists()
        assert not source.exists()
        assert sidecar.exists()
        assert not (expected.parent / ".metadata.json.tmp").exists()
        metadata = json.loads(sidecar.read_text())
        assert metadata["title"] == "A/B"
        assert metadata["authors"] == ["Léonie: Writer"]
        assert metadata["files"][0]["sha256"] == book.files[0].sha256


def test_approval_rewrites_epub_metadata_and_embeds_cover(tmp_path: Path, monkeypatch) -> None:
    use_library(monkeypatch, tmp_path)
    source = tmp_path / "incoming.epub"
    make_epub(source, title="Unmatched", author="Unknown")

    with scanner_session() as db:
        scan_library(db, initial=True)
        book = db.scalar(select(Book))
        assert book is not None
        book.title = "Approved Title"
        book.primary_author = "First Author"
        book.authors_json = json.dumps(["First Author", "Second Author"])
        book.isbns_json = json.dumps(["9781234567890"])
        book.language = "en"
        book.description = "Approved description"
        book.publication_date = "2026-08-27"
        book.series = "A Series"
        book.series_number = 3
        organise_book(db, book)

        path = Path(book.files[0].path)
        embedded = read_epub(path)
        assert embedded.title == "Approved Title"
        assert embedded.authors == ["First Author", "Second Author"]
        assert embedded.identifiers == ["9781234567890"]
        assert embedded.language == "en"
        assert embedded.series == "A Series"
        assert embedded.series_number == 3

        cover = path.parent / "cover.jpg"
        cover.write_bytes(b"\xff\xd8\xff\xe0digest-test-cover\xff\xd9")
        book.cover_path = str(cover)
        previous_hash = book.files[0].sha256
        write_approved_metadata(book)
        db.commit()

        assert book.files[0].sha256 != previous_hash
        assert not path.with_name(f".{path.name}.digest.tmp").exists()
        with zipfile.ZipFile(path) as archive:
            container = ElementTree.fromstring(archive.read("META-INF/container.xml"))
            opf_path = next(
                node.attrib["full-path"]
                for node in container.iter()
                if node.tag.endswith("rootfile")
            )
            package = ElementTree.fromstring(archive.read(opf_path))
            assert archive.read("digest-cover.jpg") == cover.read_bytes()
            assert any(
                node.attrib.get("id") == "digest-cover"
                and node.attrib.get("href") == "digest-cover.jpg"
                for node in package.iter()
            )
            assert any(
                node.attrib.get("name") == "cover" and node.attrib.get("content") == "digest-cover"
                for node in package.iter()
            )


def test_sidecars_are_imported_and_formats_are_grouped_without_moving_baseline(
    tmp_path: Path, monkeypatch
) -> None:
    use_library(monkeypatch, tmp_path)
    directory = tmp_path / "Existing Author" / "Existing Book"
    directory.mkdir(parents=True)
    epub = directory / "book.epub"
    kepub = directory / "book.kepub"
    make_epub(epub, title="Embedded Title", author="Embedded Author")
    make_epub(kepub, title="Other Embedded Title", author="Other Embedded Author")
    write_calibre_sidecars(directory)

    with scanner_session() as db:
        scan_library(db, initial=True)
        imported = reconcile_sidecars(db, "en")
        grouped = group_logical_books(db)
        book = db.scalar(select(Book))

        assert imported == 2
        assert grouped == 1
        assert db.scalar(select(func.count(Book.id))) == 1
        assert db.scalar(select(func.count(BookFile.id))) == 2
        assert epub.exists() and kepub.exists()
        assert book is not None
        assert book.title == "Sidecar Title"
        assert book.primary_author == "Sidecar Author"
        assert book.series == "Sidecar Series"
        assert book.series_number == 4
        assert book.page_count == 321
        assert book.cover_path == str(directory / "cover.jpg")
        assert all(
            json.loads(item.metadata_json)["title"] == "Sidecar Title"
            for item in db.scalars(select(BookFile))
        )


def test_matching_title_and_author_are_grouped_across_directories(
    tmp_path: Path, monkeypatch
) -> None:
    use_library(monkeypatch, tmp_path)
    epub_dir = tmp_path / "Adrian Tchaikovsky" / "Bee Speaker"
    kepub_dir = tmp_path / "Adrian Tchaikovsky" / "Bee Speaker, Dogs of War 3"
    epub_dir.mkdir(parents=True)
    kepub_dir.mkdir(parents=True)
    epub = epub_dir / "Bee Speaker.epub"
    kepub = kepub_dir / "Bee Speaker.kepub"
    make_epub(epub, title="Bee Speaker", author="Adrian Tchaikovsky")
    make_epub(kepub, title="Bee Speaker", author="Adrian Tchaikovsky")

    with scanner_session() as db:
        scan_library(db, initial=True)
        grouped = group_logical_books(db)

        assert grouped == 1
        assert db.scalar(select(func.count(Book.id))) == 1
        assert db.scalar(select(func.count(BookFile.id))) == 2
        assert {item.format for item in db.scalars(select(BookFile))} == {"epub", "kepub"}


def test_catalogue_deletion_removes_files_sidecars_cover_and_record(
    tmp_path: Path, monkeypatch
) -> None:
    use_library(monkeypatch, tmp_path)
    directory = tmp_path / "Author" / "Book"
    directory.mkdir(parents=True)
    path = directory / "book.epub"
    make_epub(path)
    (directory / "metadata.json").write_text("{}")
    (directory / "metadata.opf").write_text("<package/>")
    (directory / "cover.jpg").write_bytes(b"cover")

    with scanner_session() as db:
        scan_library(db, initial=True)
        book = db.scalar(select(Book))
        assert book is not None

        delete_book(db, book)

        assert not path.exists()
        assert not (directory / "metadata.json").exists()
        assert not (directory / "metadata.opf").exists()
        assert not (directory / "cover.jpg").exists()
        assert db.scalar(select(Book)) is None


def test_catalogue_deletion_preserves_shared_directory_sidecars(
    tmp_path: Path, monkeypatch
) -> None:
    use_library(monkeypatch, tmp_path)
    directory = tmp_path / "Mixed"
    directory.mkdir()
    first = directory / "first.epub"
    second = directory / "second.epub"
    make_epub(first, title="First")
    make_epub(second, title="Second")

    with scanner_session() as db:
        scan_library(db, initial=True)
        (directory / "metadata.json").write_text("{}")
        (directory / "metadata.opf").write_text("<package/>")
        (directory / "cover.jpg").write_bytes(b"cover")
        book = db.scalar(select(Book).where(Book.title == "First"))
        assert book is not None
        book.cover_path = str(directory / "cover.jpg")
        db.commit()

        delete_book(db, book)

        assert not first.exists()
        assert second.exists()
        assert (directory / "metadata.json").exists()
        assert (directory / "metadata.opf").exists()
        assert (directory / "cover.jpg").exists()
