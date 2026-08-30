import json

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session

from digest.admin_settings import SettingsError, save_admin_settings
from digest.db import Base
from digest.models import AppSetting, AuditEvent, Role, User
from digest.security import hash_password


def settings_session() -> Session:
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    return Session(engine, expire_on_commit=False)


def add_admin(db: Session) -> User:
    admin = User(
        username="admin",
        password_hash=hash_password("initial-password"),
        role=Role.ADMIN,
    )
    db.add(admin)
    db.commit()
    return admin


def valid_values() -> dict[str, str]:
    return {
        "metadata_provider_order": "hardcover, google_books, openlibrary, isbndb",
        "auto_match_threshold": "0.96",
        "metadata_refresh_hours": "336",
        "discovery_refresh_hours": "24",
        "default_language": "en-GB",
        "author_aliases": "J. R. R. Tolkien = J.R.R. Tolkien",
        "series_aliases": "LOTR = The Lord of the Rings",
        "hardcover_api_key": "hardcover-secret",
        "google_books_api_key": "google-secret",
        "isbndb_api_key": "isbn-secret",
        "nytimes_api_key": "nyt-secret",
        "smtp_host": "smtp.example.test",
        "smtp_port": "587",
        "smtp_user": "digest@example.test",
        "smtp_password": "smtp-secret",
        "smtp_starttls": "on",
        "shelfmark_enabled": "on",
        "shelfmark_url": "http://shelfmark:8084/",
        "usenet_enabled": "on",
        "prowlarr_url": "http://prowlarr:9696/",
        "prowlarr_api_key": "prowlarr-secret",
        "sabnzbd_url": "http://sabnzbd:8080/",
        "sabnzbd_api_key": "sab-secret",
        "sabnzbd_category": "ebooks",
    }


def test_admin_settings_are_validated_stored_and_audited() -> None:
    with settings_session() as db:
        admin = add_admin(db)

        save_admin_settings(db, admin, valid_values())

        settings = {item.key: item for item in db.scalars(select(AppSetting))}
        assert json.loads(settings["metadata_provider_order"].value) == [
            "hardcover",
            "google_books",
            "openlibrary",
            "isbndb",
        ]
        assert settings["auto_match_threshold"].value == "0.9600"
        assert settings["default_language"].value == "en-gb"
        assert settings["metadata_refresh_hours"].value == "336"
        assert settings["discovery_refresh_hours"].value == "24"
        assert json.loads(settings["author_aliases"].value) == {
            "j. r. r. tolkien": "J.R.R. Tolkien"
        }
        assert settings["smtp_starttls"].value == "true"
        assert settings["shelfmark_enabled"].value == "true"
        assert settings["shelfmark_url"].value == "http://shelfmark:8084"
        assert settings["usenet_enabled"].value == "true"
        assert settings["prowlarr_api_key"].secret is True
        assert settings["sabnzbd_api_key"].secret is True
        assert settings["smtp_password"].secret is True
        assert (
            db.scalar(select(AuditEvent).where(AuditEvent.event == "settings_updated")) is not None
        )


def test_blank_secret_fields_retain_existing_values() -> None:
    with settings_session() as db:
        admin = add_admin(db)
        save_admin_settings(db, admin, valid_values())
        changed = valid_values()
        changed["smtp_password"] = ""
        changed["google_books_api_key"] = ""

        save_admin_settings(db, admin, changed)

        assert db.get(AppSetting, "smtp_password").value == "smtp-secret"
        assert db.get(AppSetting, "google_books_api_key").value == "google-secret"


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("metadata_provider_order", "google_books, openlibrary"),
        ("auto_match_threshold", "0.5"),
        ("metadata_refresh_hours", "0"),
        ("discovery_refresh_hours", "5"),
        ("author_aliases", "invalid alias"),
        ("default_language", "English"),
        ("smtp_port", "70000"),
        ("shelfmark_url", "not a URL"),
        ("prowlarr_url", "not a URL"),
        ("sabnzbd_url", "not a URL"),
    ],
)
def test_invalid_admin_settings_are_rejected(field: str, value: str) -> None:
    with settings_session() as db:
        admin = add_admin(db)
        values = valid_values()
        values[field] = value

        with pytest.raises(SettingsError):
            save_admin_settings(db, admin, values)

        assert db.scalar(select(AppSetting)) is None
