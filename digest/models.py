import enum
import uuid
from datetime import UTC, datetime

from sqlalchemy import (
    BigInteger,
    Boolean,
    DateTime,
    Enum,
    Float,
    ForeignKey,
    Integer,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from .db import Base


def now() -> datetime:
    return datetime.now(UTC)


class Role(str, enum.Enum):
    ADMIN = "admin"
    USER = "user"


class ReviewState(str, enum.Enum):
    READY = "ready"
    REVIEW = "review"
    REJECTED = "rejected"
    ERROR = "error"


class JobStatus(str, enum.Enum):
    QUEUED = "queued"
    RUNNING = "running"
    COMPLETE = "complete"
    FAILED = "failed"


class WantedStatus(str, enum.Enum):
    WANTED = "wanted"
    SEARCHING = "searching"
    DOWNLOADING = "downloading"
    IMPORTING = "importing"
    AVAILABLE = "available"
    FAILED = "failed"
    CANCELLED = "cancelled"


class User(Base):
    __tablename__ = "users"
    id: Mapped[int] = mapped_column(primary_key=True)
    username: Mapped[str] = mapped_column(String(80), unique=True, index=True)
    password_hash: Mapped[str] = mapped_column(String(255))
    role: Mapped[Role] = mapped_column(Enum(Role), default=Role.USER)
    kindle_email: Mapped[str | None] = mapped_column(String(255))
    kobo_sync_shelf_id: Mapped[int | None] = mapped_column(
        ForeignKey("shelves.id", ondelete="SET NULL"), index=True
    )
    kobo_sync_all_books: Mapped[bool] = mapped_column(Boolean, default=False)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)


class TrustedDevice(Base):
    __tablename__ = "trusted_devices"
    id: Mapped[int] = mapped_column(primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), index=True)
    token_hash: Mapped[str] = mapped_column(String(64), unique=True)
    user_agent: Mapped[str] = mapped_column(Text, default="")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)
    last_used_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=now, onupdate=now
    )
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class Book(Base):
    __tablename__ = "books"
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    title: Mapped[str] = mapped_column(String(500), index=True)
    sort_title: Mapped[str] = mapped_column(String(500), default="")
    primary_author: Mapped[str] = mapped_column(String(300), index=True)
    authors_json: Mapped[str] = mapped_column(Text, default="[]")
    series: Mapped[str | None] = mapped_column(String(300), index=True)
    series_number: Mapped[float | None] = mapped_column(Float)
    isbns_json: Mapped[str] = mapped_column(Text, default="[]")
    language: Mapped[str | None] = mapped_column(String(20))
    description: Mapped[str | None] = mapped_column(Text)
    publication_date: Mapped[str | None] = mapped_column(String(40))
    page_count: Mapped[int | None] = mapped_column(Integer)
    cover_path: Mapped[str | None] = mapped_column(Text)
    review_state: Mapped[ReviewState] = mapped_column(Enum(ReviewState), default=ReviewState.REVIEW)
    review_reason: Mapped[str | None] = mapped_column(Text)
    match_confidence: Mapped[float] = mapped_column(Float, default=0)
    metadata_source: Mapped[str] = mapped_column(String(80), default="embedded")
    locked_fields_json: Mapped[str] = mapped_column(Text, default="[]")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now, onupdate=now)
    files: Mapped[list["BookFile"]] = relationship(
        back_populates="book", cascade="all, delete-orphan"
    )


class BookFile(Base):
    __tablename__ = "book_files"
    id: Mapped[int] = mapped_column(primary_key=True)
    book_id: Mapped[str] = mapped_column(ForeignKey("books.id", ondelete="CASCADE"), index=True)
    path: Mapped[str] = mapped_column(Text, unique=True)
    sha256: Mapped[str] = mapped_column(String(64), index=True)
    format: Mapped[str] = mapped_column(String(12))
    size_bytes: Mapped[int] = mapped_column(BigInteger)
    modified_ns: Mapped[int] = mapped_column(BigInteger)
    drm_rejected: Mapped[bool] = mapped_column(Boolean, default=False)
    metadata_json: Mapped[str] = mapped_column(Text, default="{}")
    book: Mapped[Book] = relationship(back_populates="files")


class ReadingState(Base):
    __tablename__ = "reading_states"
    __table_args__ = (UniqueConstraint("user_id", "book_id"),)
    id: Mapped[int] = mapped_column(primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"))
    book_id: Mapped[str] = mapped_column(ForeignKey("books.id", ondelete="CASCADE"))
    state: Mapped[str] = mapped_column(String(30), default="unread")
    rating: Mapped[int | None] = mapped_column(Integer)
    favourite: Mapped[bool] = mapped_column(Boolean, default=False)
    progress_percent: Mapped[float | None] = mapped_column(Float)
    location_json: Mapped[str] = mapped_column(Text, default="{}")
    spent_reading_minutes: Mapped[int | None] = mapped_column(Integer)
    remaining_time_minutes: Mapped[int | None] = mapped_column(Integer)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now, onupdate=now)


class KoboSyncedBook(Base):
    __tablename__ = "kobo_synced_books"
    __table_args__ = (UniqueConstraint("user_id", "book_id"),)
    id: Mapped[int] = mapped_column(primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), index=True)
    book_id: Mapped[str] = mapped_column(String(36), index=True)
    book_revision: Mapped[str] = mapped_column(String(32))
    reading_revision: Mapped[str] = mapped_column(String(32))
    archived: Mapped[bool] = mapped_column(Boolean, default=False)
    synced_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)


class KoboSyncedShelf(Base):
    __tablename__ = "kobo_synced_shelves"
    __table_args__ = (UniqueConstraint("user_id", "shelf_id"),)
    id: Mapped[int] = mapped_column(primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), index=True)
    shelf_id: Mapped[int] = mapped_column(Integer, index=True)
    revision: Mapped[str] = mapped_column(String(64))
    synced_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)


class DiscoveryItem(Base):
    __tablename__ = "discovery_items"
    __table_args__ = (
        UniqueConstraint("provider", "kind", "category", "source_id"),
    )
    id: Mapped[int] = mapped_column(primary_key=True)
    provider: Mapped[str] = mapped_column(String(80), index=True)
    kind: Mapped[str] = mapped_column(String(40), index=True)
    category: Mapped[str] = mapped_column(String(100), default="", index=True)
    source_id: Mapped[str] = mapped_column(String(160))
    title: Mapped[str] = mapped_column(String(500))
    authors_json: Mapped[str] = mapped_column(Text, default="[]")
    publication_date: Mapped[str | None] = mapped_column(String(40))
    cover_url: Mapped[str | None] = mapped_column(Text)
    source_url: Mapped[str] = mapped_column(Text)
    rank: Mapped[int] = mapped_column(Integer)
    fetched_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now, index=True)


class WantedItem(Base):
    __tablename__ = "wanted_items"
    __table_args__ = (UniqueConstraint("user_id", "request_key"),)
    id: Mapped[int] = mapped_column(primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), index=True)
    request_key: Mapped[str] = mapped_column(String(64))
    source: Mapped[str] = mapped_column(String(40))
    source_id: Mapped[str] = mapped_column(String(160), default="")
    title: Mapped[str] = mapped_column(String(500))
    author: Mapped[str] = mapped_column(String(300), default="")
    isbn: Mapped[str] = mapped_column(String(40), default="")
    cover_url: Mapped[str | None] = mapped_column(Text)
    status: Mapped[WantedStatus] = mapped_column(
        Enum(WantedStatus), default=WantedStatus.WANTED, index=True
    )
    attempts: Mapped[int] = mapped_column(Integer, default=0)
    next_search_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now, index=True)
    last_error: Mapped[str | None] = mapped_column(Text)
    acquired_book_id: Mapped[str | None] = mapped_column(
        ForeignKey("books.id", ondelete="SET NULL"), index=True
    )
    selected_release_id: Mapped[int | None] = mapped_column(
        ForeignKey("acquisition_releases.id", ondelete="SET NULL"), index=True
    )
    download_adapter: Mapped[str | None] = mapped_column(String(40))
    external_download_id: Mapped[str | None] = mapped_column(String(300), index=True)
    status_payload_json: Mapped[str] = mapped_column(Text, default="{}")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now, onupdate=now)


class AcquisitionRelease(Base):
    __tablename__ = "acquisition_releases"
    __table_args__ = (UniqueConstraint("wanted_id", "adapter", "source_id"),)
    id: Mapped[int] = mapped_column(primary_key=True)
    wanted_id: Mapped[int] = mapped_column(
        ForeignKey("wanted_items.id", ondelete="CASCADE"), index=True
    )
    adapter: Mapped[str] = mapped_column(String(40))
    source: Mapped[str] = mapped_column(String(80), default="")
    source_id: Mapped[str] = mapped_column(String(300))
    title: Mapped[str] = mapped_column(String(500))
    format: Mapped[str] = mapped_column(String(20), default="")
    size_bytes: Mapped[int | None] = mapped_column(BigInteger)
    seeders: Mapped[int | None] = mapped_column(Integer)
    download_payload_json: Mapped[str] = mapped_column(Text, default="{}")
    match_score: Mapped[float] = mapped_column(Float, default=0)
    search_stage: Mapped[str] = mapped_column(String(30))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)


class Shelf(Base):
    __tablename__ = "shelves"
    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(160))
    owner_id: Mapped[int | None] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"))
    shared: Mapped[bool] = mapped_column(Boolean, default=False)
    kobo_sync: Mapped[bool] = mapped_column(Boolean, default=False)


class ShelfBook(Base):
    __tablename__ = "shelf_books"
    __table_args__ = (UniqueConstraint("shelf_id", "book_id"),)
    id: Mapped[int] = mapped_column(primary_key=True)
    shelf_id: Mapped[int] = mapped_column(ForeignKey("shelves.id", ondelete="CASCADE"))
    book_id: Mapped[str] = mapped_column(ForeignKey("books.id", ondelete="CASCADE"))


class AppSetting(Base):
    __tablename__ = "settings"
    key: Mapped[str] = mapped_column(String(160), primary_key=True)
    value: Mapped[str] = mapped_column(Text, default="")
    secret: Mapped[bool] = mapped_column(Boolean, default=False)


class AuditEvent(Base):
    __tablename__ = "audit_events"
    id: Mapped[int] = mapped_column(primary_key=True)
    occurred_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now, index=True)
    level: Mapped[str] = mapped_column(String(20), default="info")
    event: Mapped[str] = mapped_column(String(100), index=True)
    user_id: Mapped[int | None] = mapped_column(ForeignKey("users.id", ondelete="SET NULL"))
    message: Mapped[str] = mapped_column(Text)


class ApiToken(Base):
    __tablename__ = "api_tokens"
    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(100))
    token_hash: Mapped[str] = mapped_column(String(64), unique=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), index=True)
    created_by: Mapped[int] = mapped_column(ForeignKey("users.id"))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class Job(Base):
    __tablename__ = "jobs"
    id: Mapped[int] = mapped_column(primary_key=True)
    kind: Mapped[str] = mapped_column(String(80), index=True)
    payload_json: Mapped[str] = mapped_column(Text, default="{}")
    status: Mapped[JobStatus] = mapped_column(Enum(JobStatus), default=JobStatus.QUEUED, index=True)
    attempts: Mapped[int] = mapped_column(Integer, default=0)
    max_attempts: Mapped[int] = mapped_column(Integer, default=5)
    run_after: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now, index=True)
    claimed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    claimed_by: Mapped[str | None] = mapped_column(String(160))
    last_error: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now, onupdate=now)
