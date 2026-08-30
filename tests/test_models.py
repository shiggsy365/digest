from sqlalchemy import BigInteger

from digest.models import BookFile


def test_book_file_uses_64_bit_size_and_nanosecond_columns() -> None:
    table = BookFile.__table__
    assert isinstance(table.c.size_bytes.type, BigInteger)
    assert isinstance(table.c.modified_ns.type, BigInteger)
