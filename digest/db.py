from collections.abc import Generator
from pathlib import Path

from alembic.config import Config
from alembic.runtime.migration import MigrationContext
from alembic.script import ScriptDirectory
from sqlalchemy import create_engine
from sqlalchemy.orm import DeclarativeBase, Session, sessionmaker

from .config import get_settings


class Base(DeclarativeBase):
    pass


settings = get_settings()
connect_args = {"check_same_thread": False} if settings.database_url.startswith("sqlite") else {}
engine = create_engine(settings.database_url, pool_pre_ping=True, connect_args=connect_args)
SessionLocal = sessionmaker(bind=engine, expire_on_commit=False)


def get_db() -> Generator[Session, None, None]:
    with SessionLocal() as session:
        yield session


def initialise_database() -> None:
    """Refuse to run against an absent or outdated production schema."""
    config = Config(Path(__file__).parents[1] / "alembic.ini")
    scripts = ScriptDirectory.from_config(config)
    expected = set(scripts.get_heads())
    with engine.connect() as connection:
        current = set(MigrationContext.configure(connection).get_current_heads())

    if current != expected:
        found = ", ".join(sorted(current)) or "no migration"
        wanted = ", ".join(sorted(expected))
        raise RuntimeError(
            f"Database schema is not current (found {found}; expected {wanted}). "
            "Run `alembic upgrade head` before starting Digest."
        )
