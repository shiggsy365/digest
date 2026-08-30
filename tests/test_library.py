import struct
from pathlib import Path
from xml.etree import ElementTree

import pytest

from digest.library import _replace_metadata_values, clean_segment, encryption_is_drm, read_mobi


def make_mobi(path: Path, *, encryption: int = 0, title: str = "Test Book") -> None:
    record_zero = 86
    title_bytes = title.encode()
    record = bytearray(108 + len(title_bytes))
    struct.pack_into(">H", record, 12, encryption)
    record[16:20] = b"MOBI"
    struct.pack_into(">I", record, 28, 65001)
    struct.pack_into(">II", record, 84, 108, len(title_bytes))
    record[108:] = title_bytes

    header = bytearray(record_zero)
    header[60:68] = b"BOOKMOBI"
    struct.pack_into(">H", header, 76, 1)
    struct.pack_into(">I", header, 78, record_zero)
    path.write_bytes(header + record)


@pytest.mark.parametrize("suffix", [".mobi", ".azw3"])
def test_mobi_header_reads_title_and_accepts_unencrypted_files(tmp_path: Path, suffix: str) -> None:
    path = tmp_path / f"book{suffix}"
    make_mobi(path, title="A Proper Title")

    metadata = read_mobi(path)

    assert metadata.title == "A Proper Title"
    assert metadata.drm is False


def test_mobi_header_detects_drm(tmp_path: Path) -> None:
    path = tmp_path / "protected.azw3"
    make_mobi(path, encryption=2)

    assert read_mobi(path).drm is True


def test_mobi_header_rejects_unrelated_files(tmp_path: Path) -> None:
    path = tmp_path / "not-a-book.mobi"
    path.write_bytes(b"not really a mobi")

    with pytest.raises(ValueError, match="BOOKMOBI"):
        read_mobi(path)


def test_clean_segment_preserves_unicode_and_removes_reserved_characters() -> None:
    assert clean_segment("  Léonie: A / B?  ") == "Léonie- A - B-"


def test_epub_metadata_writer_normalises_structured_provider_values() -> None:
    metadata = ElementTree.Element("metadata")
    _replace_metadata_values(metadata, "description", [["First paragraph", "Second paragraph"]])
    assert metadata[0].text == "First paragraph\n\nSecond paragraph"


@pytest.mark.parametrize(
    "algorithm",
    ["http://www.idpf.org/2008/embedding", "http://ns.adobe.com/pdf/enc#RC"],
)
def test_font_obfuscation_is_not_treated_as_drm(algorithm: str) -> None:
    encryption = f"""<encryption xmlns="urn:oasis:names:tc:opendocument:xmlns:container"
        xmlns:enc="http://www.w3.org/2001/04/xmlenc#">
      <enc:EncryptedData>
        <enc:EncryptionMethod Algorithm="{algorithm}"/>
        <enc:CipherData><enc:CipherReference URI="fonts/book.ttf"/></enc:CipherData>
      </enc:EncryptedData>
    </encryption>""".encode()

    assert encryption_is_drm(encryption) is False


def test_encrypted_content_is_treated_as_drm() -> None:
    encryption = b"""<encryption xmlns:enc="http://www.w3.org/2001/04/xmlenc#">
      <enc:EncryptedData>
        <enc:EncryptionMethod Algorithm="http://www.w3.org/2001/04/xmlenc#aes256-cbc"/>
        <enc:CipherData><enc:CipherReference URI="text/chapter.xhtml"/></enc:CipherData>
      </enc:EncryptedData>
    </encryption>"""

    assert encryption_is_drm(encryption) is True


def test_mixed_font_and_content_encryption_is_treated_as_drm() -> None:
    encryption = b"""<encryption xmlns:enc="http://www.w3.org/2001/04/xmlenc#">
      <enc:EncryptedData>
        <enc:EncryptionMethod Algorithm="http://www.idpf.org/2008/embedding"/>
        <enc:CipherData><enc:CipherReference URI="fonts/book.otf"/></enc:CipherData>
      </enc:EncryptedData>
      <enc:EncryptedData>
        <enc:EncryptionMethod Algorithm="unknown"/>
        <enc:CipherData><enc:CipherReference URI="text/chapter.xhtml"/></enc:CipherData>
      </enc:EncryptedData>
    </encryption>"""

    assert encryption_is_drm(encryption) is True
