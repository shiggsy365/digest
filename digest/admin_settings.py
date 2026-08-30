import json
import re

from sqlalchemy.orm import Session

from .models import AppSetting, AuditEvent, User

PROVIDERS = ("hardcover", "google_books", "openlibrary", "isbndb")
SECRET_KEYS = {
    "hardcover_api_key",
    "google_books_api_key",
    "isbndb_api_key",
    "nytimes_api_key",
    "smtp_password",
    "prowlarr_api_key",
    "sabnzbd_api_key",
}


class SettingsError(ValueError):
    pass


def parse_aliases(value: str) -> dict[str, str]:
    aliases = {}
    for line in value.splitlines():
        line = line.strip()
        if not line:
            continue
        if "=" not in line:
            raise SettingsError("Each alias must use alias = canonical name syntax.")
        alias, canonical = (item.strip() for item in line.split("=", 1))
        if not alias or not canonical:
            raise SettingsError("Alias and canonical names cannot be blank.")
        aliases[alias.casefold()] = canonical
    return aliases


def store_setting(db: Session, key: str, value: str, secret: bool = False) -> None:
    item = db.get(AppSetting, key)
    if item is None:
        item = AppSetting(key=key)
        db.add(item)
    item.value = value
    item.secret = secret


def save_admin_settings(db: Session, actor: User, values: dict[str, str]) -> None:
    order = [item.strip() for item in values["metadata_provider_order"].split(",")]
    if len(order) != len(PROVIDERS) or set(order) != set(PROVIDERS):
        raise SettingsError("Provider order must list each configured provider exactly once.")
    try:
        threshold = float(values["auto_match_threshold"])
    except ValueError as exc:
        raise SettingsError("Automatic-match threshold must be a number.") from exc
    if not 0.8 <= threshold <= 1:
        raise SettingsError("Automatic-match threshold must be between 0.80 and 1.00.")
    try:
        smtp_port = int(values["smtp_port"])
    except ValueError as exc:
        raise SettingsError("SMTP port must be a number.") from exc
    if not 1 <= smtp_port <= 65535:
        raise SettingsError("SMTP port must be between 1 and 65535.")
    try:
        refresh_hours = int(values["metadata_refresh_hours"])
    except ValueError as exc:
        raise SettingsError("Metadata refresh interval must be a whole number of hours.") from exc
    if not 1 <= refresh_hours <= 8760:
        raise SettingsError("Metadata refresh interval must be between 1 and 8760 hours.")
    try:
        discovery_hours = int(values["discovery_refresh_hours"])
    except ValueError as exc:
        raise SettingsError("Discovery refresh interval must be a whole number of hours.") from exc
    if not 6 <= discovery_hours <= 8760:
        raise SettingsError("Discovery refresh interval must be between 6 and 8760 hours.")
    language = values["default_language"].strip().lower()
    if not re.fullmatch(r"[a-z]{2,3}(?:-[a-z0-9]{2,8})?", language):
        raise SettingsError("Default language must be a language tag such as en or en-gb.")

    ordinary = {
        "metadata_provider_order": json.dumps(order),
        "auto_match_threshold": f"{threshold:.4f}",
        "default_language": language,
        "metadata_refresh_hours": str(refresh_hours),
        "discovery_refresh_hours": str(discovery_hours),
        "author_aliases": json.dumps(parse_aliases(values.get("author_aliases", ""))),
        "series_aliases": json.dumps(parse_aliases(values.get("series_aliases", ""))),
        "smtp_host": values["smtp_host"].strip(),
        "smtp_port": str(smtp_port),
        "smtp_user": values["smtp_user"].strip(),
        "smtp_starttls": "true" if values.get("smtp_starttls") == "on" else "false",
        "shelfmark_enabled": "true" if values.get("shelfmark_enabled") == "on" else "false",
        "shelfmark_url": values.get("shelfmark_url", "").strip().rstrip("/"),
        "usenet_enabled": "true" if values.get("usenet_enabled") == "on" else "false",
        "prowlarr_url": values.get("prowlarr_url", "").strip().rstrip("/"),
        "sabnzbd_url": values.get("sabnzbd_url", "").strip().rstrip("/"),
        "sabnzbd_category": values.get("sabnzbd_category", "ebooks").strip() or "ebooks",
    }
    shelfmark_url = ordinary["shelfmark_url"]
    if ordinary["shelfmark_enabled"] == "true" and not re.fullmatch(r"https?://[^\s]+", shelfmark_url):
        raise SettingsError("Shelfmark URL must be an http:// or https:// address.")
    if ordinary["usenet_enabled"] == "true":
        for label, key in (("Prowlarr", "prowlarr_url"), ("SABnzbd", "sabnzbd_url")):
            if not re.fullmatch(r"https?://[^\s]+", ordinary[key]):
                raise SettingsError(f"{label} URL must be an http:// or https:// address.")
    for key, value in ordinary.items():
        store_setting(db, key, value)
    for key in SECRET_KEYS:
        value = values.get(key, "").strip()
        if value:
            store_setting(db, key, value, secret=True)
    db.add(
        AuditEvent(
            event="settings_updated",
            user_id=actor.id,
            message="Updated metadata and SMTP configuration",
        )
    )
    db.commit()
