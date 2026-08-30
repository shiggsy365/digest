import hashlib
import json
import re
import shutil
import struct
import unicodedata
import zipfile
from dataclasses import dataclass, field
from pathlib import Path
from xml.etree import ElementTree

from sqlalchemy import select
from sqlalchemy.orm import Session

from .config import get_settings
from .models import (
    AppSetting,
    AuditEvent,
    Book,
    BookFile,
    ReadingState,
    ReviewState,
    ShelfBook,
)
from .text import plain_text

SUPPORTED = {".epub", ".mobi", ".azw3", ".kepub"}
DC_NS = "http://purl.org/dc/elements/1.1/"
OPF_NS = "http://www.idpf.org/2007/opf"
FONT_OBFUSCATION_ALGORITHMS = {
    "http://www.idpf.org/2008/embedding",
    "http://ns.adobe.com/pdf/enc#RC",
}
FONT_EXTENSIONS = {".otf", ".ttf", ".woff", ".woff2"}


@dataclass
class EmbeddedMetadata:
    title: str
    authors: list[str] = field(default_factory=list)
    language: str | None = None
    identifiers: list[str] = field(default_factory=list)
    description: str | None = None
    series: str | None = None
    series_number: float | None = None
    publication_date: str | None = None
    page_count: int | None = None
    drm: bool = False


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def clean_segment(value: str, fallback: str = "Unknown") -> str:
    value = unicodedata.normalize("NFC", value)
    value = re.sub(r"[\x00-\x1f<>:\"/\\|?*]", "-", value)
    value = re.sub(r"\s+", " ", value).strip(" .")
    return (value or fallback)[:180]


def _text(root: ElementTree.Element, suffix: str) -> str | None:
    node = next((node for node in root.iter() if node.tag.endswith(suffix)), None)
    return node.text.strip() if node is not None and node.text else None


def encryption_is_drm(data: bytes) -> bool:
    root = ElementTree.fromstring(data)
    encrypted_items = [node for node in root.iter() if node.tag.endswith("EncryptedData")]
    for encrypted in encrypted_items:
        method = next(
            (node for node in encrypted.iter() if node.tag.endswith("EncryptionMethod")), None
        )
        reference = next(
            (node for node in encrypted.iter() if node.tag.endswith("CipherReference")), None
        )
        algorithm = method.attrib.get("Algorithm") if method is not None else None
        uri = reference.attrib.get("URI") if reference is not None else None
        extension = Path(uri.split("?", 1)[0]).suffix.casefold() if uri else None
        if algorithm not in FONT_OBFUSCATION_ALGORITHMS or extension not in FONT_EXTENSIONS:
            return True
    return False


def metadata_from_opf(root: ElementTree.Element, fallback_title: str) -> EmbeddedMetadata:
    authors = [
        node.text.strip() for node in root.iter() if node.tag.endswith("creator") and node.text
    ]
    identifiers = [
        node.text.strip() for node in root.iter() if node.tag.endswith("identifier") and node.text
    ]
    metadata = EmbeddedMetadata(
        title=_text(root, "title") or fallback_title,
        authors=authors,
        language=_text(root, "language"),
        identifiers=identifiers,
        description=_text(root, "description"),
        publication_date=_text(root, "date"),
    )
    for node in root.iter():
        if not node.tag.endswith("meta"):
            continue
        name, content = node.attrib.get("name"), node.attrib.get("content")
        if name == "calibre:series":
            metadata.series = content
        elif name == "calibre:series_index" and content:
            try:
                metadata.series_number = float(content)
            except ValueError:
                pass
        elif name in {"calibre:pages", "calibre:page_count"} and content:
            try:
                metadata.page_count = int(content)
            except ValueError:
                pass
    return metadata


def read_opf(path: Path) -> EmbeddedMetadata:
    return metadata_from_opf(ElementTree.fromstring(path.read_bytes()), path.parent.name)


def read_epub(path: Path) -> EmbeddedMetadata:
    with zipfile.ZipFile(path) as archive:
        if "META-INF/encryption.xml" in archive.namelist() and encryption_is_drm(
            archive.read("META-INF/encryption.xml")
        ):
            return EmbeddedMetadata(path.stem, drm=True)
        container = ElementTree.fromstring(archive.read("META-INF/container.xml"))
        opf_path = next(
            node.attrib["full-path"] for node in container.iter() if node.tag.endswith("rootfile")
        )
        return metadata_from_opf(ElementTree.fromstring(archive.read(opf_path)), path.stem)


def read_mobi(path: Path) -> EmbeddedMetadata:
    """Read the safe subset of PalmDB/MOBI metadata needed during scanning.

    MOBI and AZW3 files use a Palm database header. Record zero begins with a
    16-byte PalmDOC header whose encryption type is stored at byte 12.
    """
    with path.open("rb") as handle:
        header = handle.read(86)
        if len(header) < 86 or header[60:68] != b"BOOKMOBI":
            raise ValueError("Not a BOOKMOBI Palm database")
        record_count = struct.unpack_from(">H", header, 76)[0]
        if record_count < 1:
            raise ValueError("MOBI file has no records")
        record_zero = struct.unpack_from(">I", header, 78)[0]
        size = path.stat().st_size
        if record_zero < 86 or record_zero + 20 > size:
            raise ValueError("Invalid MOBI record-zero offset")
        handle.seek(record_zero)
        palm_doc = handle.read(108)

    if len(palm_doc) < 20 or palm_doc[16:20] != b"MOBI":
        raise ValueError("MOBI header is missing from record zero")
    encryption_type = struct.unpack_from(">H", palm_doc, 12)[0]
    if encryption_type not in {0, 1, 2}:
        raise ValueError(f"Unknown MOBI encryption type {encryption_type}")

    title = path.stem
    if len(palm_doc) >= 92:
        title_offset, title_length = struct.unpack_from(">II", palm_doc, 84)
        available = size - record_zero
        if title_length and title_offset <= available and title_length <= available - title_offset:
            with path.open("rb") as handle:
                handle.seek(record_zero + title_offset)
                raw_title = handle.read(title_length)
            text_encoding = struct.unpack_from(">I", palm_doc, 28)[0]
            encoding = "utf-8" if text_encoding == 65001 else "cp1252"
            decoded = raw_title.decode(encoding, "replace").strip("\x00 \t\r\n")
            if decoded:
                title = decoded
    return EmbeddedMetadata(title=title, drm=encryption_type != 0)


def read_embedded(path: Path) -> EmbeddedMetadata:
    if path.suffix.lower() in {".epub", ".kepub"} or path.name.lower().endswith(".kepub.epub"):
        return read_epub(path)
    if path.suffix.lower() in {".mobi", ".azw3"}:
        return read_mobi(path)
    raise ValueError(f"Unsupported ebook format: {path.suffix}")


def edition_metadata(metadata: EmbeddedMetadata) -> str:
    return json.dumps(
        {
            "title": metadata.title,
            "authors": metadata.authors,
            "language": metadata.language,
            "identifiers": metadata.identifiers,
            "description": metadata.description,
            "series": metadata.series,
            "series_number": metadata.series_number,
            "publication_date": metadata.publication_date,
            "page_count": metadata.page_count,
        },
        ensure_ascii=False,
    )


def preferred_metadata(path: Path, embedded: EmbeddedMetadata) -> EmbeddedMetadata:
    sidecar = path.parent / "metadata.opf"
    if sidecar.is_file() and not embedded.drm:
        try:
            return read_opf(sidecar)
        except (OSError, ElementTree.ParseError):
            pass
    return embedded


def apply_embedded_metadata(book: Book, metadata: EmbeddedMetadata, default_language: str) -> None:
    book.title = metadata.title
    book.sort_title = metadata.title.casefold()
    book.primary_author = metadata.authors[0] if metadata.authors else "Unknown Author"
    book.authors_json = json.dumps(metadata.authors)
    book.isbns_json = json.dumps(metadata.identifiers)
    book.language = metadata.language or default_language
    book.description = plain_text(metadata.description)
    book.series = metadata.series
    book.series_number = metadata.series_number
    book.publication_date = metadata.publication_date
    book.page_count = metadata.page_count


def logical_key(book: Book) -> tuple[str, str]:
    return (
        re.sub(r"\W+", "", book.title.casefold()),
        re.sub(r"\W+", "", book.primary_author.casefold()),
    )


def group_logical_books(db: Session) -> int:
    grouped = 0
    winners: dict[tuple[str, str], Book] = {}
    for item in db.scalars(select(BookFile).order_by(BookFile.id)).all():
        book = item.book
        key = logical_key(book)
        winner = winners.get(key)
        if winner is None:
            winners[key] = book
            continue
        if winner.id == book.id:
            continue
        item.book = winner
        db.flush()
        remaining = db.scalar(select(BookFile.id).where(BookFile.book_id == book.id).limit(1))
        if remaining is None:
            for state in db.scalars(
                select(ReadingState).where(ReadingState.book_id == book.id)
            ).all():
                winner_state = db.scalar(
                    select(ReadingState).where(
                        ReadingState.book_id == winner.id,
                        ReadingState.user_id == state.user_id,
                    )
                )
                if winner_state is None:
                    state.book_id = winner.id
                else:
                    winner_state.favourite = winner_state.favourite or state.favourite
                    winner_state.rating = winner_state.rating or state.rating
                    db.delete(state)
            for shelf_book in db.scalars(
                select(ShelfBook).where(ShelfBook.book_id == book.id)
            ).all():
                already_present = db.scalar(
                    select(ShelfBook.id).where(
                        ShelfBook.book_id == winner.id,
                        ShelfBook.shelf_id == shelf_book.shelf_id,
                    )
                )
                if already_present is None:
                    shelf_book.book_id = winner.id
                else:
                    db.delete(shelf_book)
            db.delete(book)
        grouped += 1
    db.commit()
    return grouped


def reconcile_sidecars(db: Session, default_language: str) -> int:
    changed = 0
    for item in db.scalars(select(BookFile)).all():
        path = Path(item.path)
        sidecar = path.parent / "metadata.opf"
        if not sidecar.is_file() or item.drm_rejected:
            continue
        try:
            metadata = read_opf(sidecar)
        except (OSError, ElementTree.ParseError) as exc:
            db.add(
                AuditEvent(
                    level="error",
                    event="sidecar_error",
                    message=f"{sidecar}: {type(exc).__name__}: {exc}",
                )
            )
            continue
        apply_embedded_metadata(item.book, metadata, default_language)
        item.metadata_json = edition_metadata(metadata)
        cover = path.parent / "cover.jpg"
        if cover.is_file():
            item.book.cover_path = str(cover)
        changed += 1
    db.commit()
    return changed


def delete_catalogue_file(db: Session, book_file: BookFile) -> None:
    book_id = book_file.book_id
    db.delete(book_file)
    db.flush()
    remaining = db.scalar(select(BookFile.id).where(BookFile.book_id == book_id).limit(1))
    if remaining is None:
        book = db.get(Book, book_id)
        if book is not None:
            db.delete(book)


def delete_book(db: Session, book: Book) -> None:
    root = get_settings().library_root.resolve()
    directories = set()
    deleting_paths = {Path(item.path).resolve() for item in book.files}
    other_paths = {
        Path(path).resolve()
        for path in db.scalars(select(BookFile.path).where(BookFile.book_id != book.id)).all()
    }
    for item in list(book.files):
        path = Path(item.path).resolve()
        if not path.is_relative_to(root):
            raise ValueError(f"Refusing to delete a file outside the library: {path}")
        if path.is_file():
            path.unlink()
        directories.add(path.parent)
    cover = Path(book.cover_path).resolve() if book.cover_path else None
    if (
        cover
        and cover.is_relative_to(root)
        and cover.is_file()
        and not any(path.parent == cover.parent for path in other_paths)
    ):
        cover.unlink()
    for directory in directories:
        if any(path.parent == directory and path not in deleting_paths for path in other_paths):
            continue
        for sidecar_name in ("metadata.json", "metadata.opf"):
            sidecar = directory / sidecar_name
            if sidecar.is_file():
                sidecar.unlink()
    db.delete(book)
    db.commit()


def scan_library(db: Session, initial: bool = False) -> dict[str, int]:
    root = get_settings().library_root
    root.mkdir(parents=True, exist_ok=True)
    discovered = {
        p.resolve() for p in root.rglob("*") if p.is_file() and p.suffix.lower() in SUPPORTED
    }
    known = {Path(item.path): item for item in db.scalars(select(BookFile)).all()}
    language_setting = db.get(AppSetting, "default_language")
    default_language = language_setting.value if language_setting else "en"
    stats = {"added": 0, "removed": 0, "duplicates": 0, "rejected": 0}

    for missing in set(known) - discovered:
        book_file = known[missing]
        delete_catalogue_file(db, book_file)
        stats["removed"] += 1

    for path in sorted(discovered & set(known)):
        book_file = known[path]
        try:
            stat = path.stat()
            recheck_rejection = (
                book_file.drm_rejected
                and book_file.book.review_reason == "DRM or unsupported container"
            )
            if (
                stat.st_mtime_ns == book_file.modified_ns
                and stat.st_size == book_file.size_bytes
                and not recheck_rejection
            ):
                continue
            checksum = sha256(path)
            duplicate = db.scalar(
                select(BookFile).where(
                    BookFile.sha256 == checksum,
                    BookFile.id != book_file.id,
                )
            )
            if duplicate and not initial:
                path.unlink()
                delete_catalogue_file(db, book_file)
                stats["duplicates"] += 1
                continue

            embedded = read_embedded(path)
            selected = preferred_metadata(path, embedded)
            book_file.sha256 = checksum
            book_file.size_bytes = stat.st_size
            book_file.modified_ns = stat.st_mtime_ns
            book_file.drm_rejected = embedded.drm
            book_file.metadata_json = edition_metadata(selected)
            book = book_file.book
            apply_embedded_metadata(book, selected, default_language)
            if embedded.drm:
                book.review_state = ReviewState.REJECTED
                book.review_reason = "Encrypted ebook content detected"
            elif recheck_rejection:
                book.review_state = ReviewState.READY
                book.review_reason = None
            else:
                book.review_state = ReviewState.REVIEW
                book.review_reason = "File changed; metadata match required"
        except (
            OSError,
            KeyError,
            StopIteration,
            ValueError,
            struct.error,
            zipfile.BadZipFile,
            ElementTree.ParseError,
        ) as exc:
            db.add(
                AuditEvent(
                    level="error",
                    event="scan_error",
                    message=f"{path}: {type(exc).__name__}: {exc}",
                )
            )

    for path in sorted(discovered - set(known)):
        try:
            checksum = sha256(path)
            duplicate = db.scalar(select(BookFile).where(BookFile.sha256 == checksum))
            if duplicate:
                if not initial:
                    path.unlink()
                stats["duplicates"] += 1
                if not initial:
                    continue
            embedded = read_embedded(path)
            selected = preferred_metadata(path, embedded)
            author = selected.authors[0] if selected.authors else "Unknown Author"
            state = (
                ReviewState.REJECTED
                if embedded.drm
                else ReviewState.READY
                if initial
                else ReviewState.REVIEW
            )
            book = Book(
                title=selected.title,
                sort_title=selected.title.casefold(),
                primary_author=author,
                authors_json=json.dumps(selected.authors),
                series=selected.series,
                series_number=selected.series_number,
                isbns_json=json.dumps(selected.identifiers),
                language=selected.language or default_language,
                description=selected.description,
                publication_date=selected.publication_date,
                page_count=selected.page_count,
                cover_path=str(path.parent / "cover.jpg")
                if (path.parent / "cover.jpg").is_file()
                else None,
                review_state=state,
                review_reason=(
                    "DRM or unsupported container"
                    if embedded.drm
                    else None
                    if initial
                    else "Metadata match required"
                ),
            )
            db.add(book)
            db.flush()
            stat = path.stat()
            db.add(
                BookFile(
                    book_id=book.id,
                    path=str(path),
                    sha256=checksum,
                    format=path.suffix.lower().lstrip("."),
                    size_bytes=stat.st_size,
                    modified_ns=stat.st_mtime_ns,
                    drm_rejected=embedded.drm,
                    metadata_json=edition_metadata(selected),
                )
            )
            stats["rejected" if embedded.drm else "added"] += 1
        except (
            OSError,
            KeyError,
            StopIteration,
            ValueError,
            struct.error,
            zipfile.BadZipFile,
            ElementTree.ParseError,
        ) as exc:
            db.add(
                AuditEvent(
                    level="error",
                    event="scan_error",
                    message=f"{path}: {type(exc).__name__}: {exc}",
                )
            )
    db.commit()
    return stats


def organised_path(book: Book, source: Path) -> Path:
    root = get_settings().library_root
    author = clean_segment(book.primary_author, "Unknown Author")
    title = clean_segment(book.title, "Unknown Title")
    filename = clean_segment(f"{book.primary_author} - {book.title}") + source.suffix.lower()
    if book.series:
        number = f"{book.series_number:g}" if book.series_number is not None else "0"
        folder = f"{number.zfill(2)} - {title}"
        return root / author / clean_segment(book.series) / folder / filename
    return root / author / title / filename


def sidecar_data(book: Book) -> dict:
    return {
        "version": 1,
        "book_id": book.id,
        "title": book.title,
        "authors": json.loads(book.authors_json or "[]"),
        "primary_author": book.primary_author,
        "series": book.series,
        "series_number": book.series_number,
        "isbns": json.loads(book.isbns_json or "[]"),
        "language": book.language,
        "description": book.description,
        "publication_date": book.publication_date,
        "page_count": book.page_count,
        "cover": Path(book.cover_path).name if book.cover_path else None,
        "metadata_source": book.metadata_source,
        "locked_fields": json.loads(book.locked_fields_json or "[]"),
        "files": [
            {
                "name": Path(item.path).name,
                "format": item.format,
                "sha256": item.sha256,
                "size_bytes": item.size_bytes,
            }
            for item in book.files
        ],
    }


def write_sidecars(book: Book) -> None:
    payload = json.dumps(sidecar_data(book), ensure_ascii=False, indent=2) + "\n"
    directories = {Path(item.path).parent for item in book.files}
    for directory in directories:
        sidecar = directory / "metadata.json"
        temporary = directory / ".metadata.json.tmp"
        temporary.write_text(payload, encoding="utf-8")
        temporary.replace(sidecar)


def _replace_metadata_values(
    metadata: ElementTree.Element, local_name: str, values: list[object]
) -> None:
    for node in list(metadata):
        if node.tag.endswith(f"}}{local_name}") or node.tag == local_name:
            metadata.remove(node)
    for value in values:
        if isinstance(value, list):
            value = "\n\n".join(str(part) for part in value if part is not None)
        elif isinstance(value, dict):
            value = json.dumps(value, ensure_ascii=False)
        elif value is not None:
            value = str(value)
        if not value:
            continue
        node = ElementTree.SubElement(metadata, f"{{{DC_NS}}}{local_name}")
        node.text = value


def _updated_opf(root: ElementTree.Element, book: Book, cover_id: str | None) -> bytes:
    metadata = next((node for node in root.iter() if node.tag.endswith("metadata")), None)
    if metadata is None:
        raise ValueError("EPUB package has no metadata element")

    authors = json.loads(book.authors_json or "[]") or [book.primary_author]
    values = {
        "title": [book.title],
        "creator": authors,
        "identifier": json.loads(book.isbns_json or "[]"),
        "language": [book.language] if book.language else [],
        "description": [book.description] if book.description else [],
        "date": [book.publication_date] if book.publication_date else [],
    }
    for local_name, items in values.items():
        _replace_metadata_values(metadata, local_name, items)

    for node in list(metadata):
        if not node.tag.endswith("meta"):
            continue
        if node.attrib.get("name") in {
            "calibre:series",
            "calibre:series_index",
            "cover",
        }:
            metadata.remove(node)
    if book.series:
        ElementTree.SubElement(
            metadata, f"{{{OPF_NS}}}meta", name="calibre:series", content=book.series
        )
    if book.series_number is not None:
        ElementTree.SubElement(
            metadata,
            f"{{{OPF_NS}}}meta",
            name="calibre:series_index",
            content=f"{book.series_number:g}",
        )
    if cover_id:
        ElementTree.SubElement(metadata, f"{{{OPF_NS}}}meta", name="cover", content=cover_id)
    return ElementTree.tostring(root, encoding="utf-8", xml_declaration=True)


def write_epub_metadata(path: Path, book: Book) -> None:
    """Atomically replace approved metadata and an optional cover in an EPUB."""
    temporary = path.with_name(f".{path.name}.digest.tmp")
    original_mode = path.stat().st_mode
    completed = False
    try:
        with zipfile.ZipFile(path, "r") as source:
            container = ElementTree.fromstring(source.read("META-INF/container.xml"))
            opf_path = next(
                node.attrib["full-path"]
                for node in container.iter()
                if node.tag.endswith("rootfile")
            )
            root = ElementTree.fromstring(source.read(opf_path))
            cover = Path(book.cover_path) if book.cover_path else None
            cover_bytes = cover.read_bytes() if cover and cover.is_file() else None
            opf_directory = Path(opf_path).parent.as_posix()
            cover_path = (
                f"{opf_directory}/digest-cover.jpg"
                if opf_directory not in {"", "."}
                else "digest-cover.jpg"
            )
            cover_id = "digest-cover" if cover_bytes else None

            if cover_bytes:
                manifest = next(
                    (node for node in root.iter() if node.tag.endswith("manifest")), None
                )
                if manifest is None:
                    raise ValueError("EPUB package has no manifest element")
                for node in list(manifest):
                    if node.attrib.get("id") == cover_id:
                        manifest.remove(node)
                ElementTree.SubElement(
                    manifest,
                    f"{{{OPF_NS}}}item",
                    id=cover_id,
                    href="digest-cover.jpg",
                    **{"media-type": "image/jpeg"},
                )

            opf_bytes = _updated_opf(root, book, cover_id)
            with zipfile.ZipFile(temporary, "w") as target:
                for info in source.infolist():
                    if info.filename == opf_path or (cover_bytes and info.filename == cover_path):
                        continue
                    target.writestr(info, source.read(info.filename))
                target.writestr(opf_path, opf_bytes, compress_type=zipfile.ZIP_DEFLATED)
                if cover_bytes:
                    target.writestr(cover_path, cover_bytes, compress_type=zipfile.ZIP_DEFLATED)
        temporary.chmod(original_mode)
        temporary.replace(path)
        completed = True
    finally:
        if not completed:
            temporary.unlink(missing_ok=True)


def write_approved_metadata(book: Book) -> None:
    for item in book.files:
        path = Path(item.path)
        if item.drm_rejected or path.suffix.lower() != ".epub":
            continue
        write_epub_metadata(path, book)
        stat = path.stat()
        item.sha256 = sha256(path)
        item.size_bytes = stat.st_size
        item.modified_ns = stat.st_mtime_ns


def organise_book(db: Session, book: Book, *, approve: bool = True) -> None:
    for setting_key, attribute in (
        ("author_aliases", "primary_author"),
        ("series_aliases", "series"),
    ):
        setting = db.get(AppSetting, setting_key)
        if setting is None or not getattr(book, attribute):
            continue
        try:
            aliases = json.loads(setting.value)
        except (TypeError, ValueError):
            aliases = {}
        current = getattr(book, attribute)
        canonical = aliases.get(current.casefold())
        if canonical:
            setattr(book, attribute, canonical)
            if attribute == "primary_author":
                authors = json.loads(book.authors_json or "[]")
                if authors:
                    authors[0] = canonical
                    book.authors_json = json.dumps(authors)
    for item in book.files:
        source = Path(item.path)
        target = organised_path(book, source)
        target.parent.mkdir(parents=True, exist_ok=True)
        if target.exists() and target != source:
            counter = 2
            while target.exists():
                target = target.with_name(f"{target.stem} ({counter}){target.suffix}")
                counter += 1
        if source != target:
            shutil.move(str(source), str(target))
            item.path = str(target)
    if approve:
        book.review_state = ReviewState.READY
        book.review_reason = None
    write_approved_metadata(book)
    write_sidecars(book)
    db.commit()
