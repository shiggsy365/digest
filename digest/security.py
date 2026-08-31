import base64
import binascii
import hashlib
import secrets
from datetime import UTC, datetime
from typing import Annotated

from fastapi import Depends, HTTPException, Request, status
from pwdlib import PasswordHash
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from .db import get_db
from .models import ApiToken, Role, TrustedDevice, User

passwords = PasswordHash.recommended()
KOBO_TOKEN_NAME = "Kobo device"
TRUSTED_DEVICE_COOKIE = "digest_trusted_device"


def hash_password(password: str) -> str:
    return passwords.hash(password)


def verify_password(password: str, digest: str) -> bool:
    return passwords.verify(password, digest)


def setup_required(db: Session) -> bool:
    return (db.scalar(select(func.count(User.id))) or 0) == 0


def token_digest(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


def new_api_token() -> str:
    return "dgt_" + secrets.token_urlsafe(32)


def new_trusted_device_token() -> str:
    return "dtd_" + secrets.token_urlsafe(32)


def create_trusted_device(db: Session, user: User, user_agent: str) -> tuple[TrustedDevice, str]:
    token = new_trusted_device_token()
    device = TrustedDevice(
        user_id=user.id,
        token_hash=token_digest(token),
        user_agent=user_agent[:1000],
    )
    db.add(device)
    db.flush()
    return device, token


def trusted_device_from_request(request: Request, db: Session) -> TrustedDevice | None:
    token = request.cookies.get(TRUSTED_DEVICE_COOKIE, "")
    if not token.startswith("dtd_"):
        return None
    device = db.scalar(
        select(TrustedDevice).where(
            TrustedDevice.token_hash == token_digest(token),
            TrustedDevice.revoked_at.is_(None),
        )
    )
    if not device:
        return None
    user = db.get(User, device.user_id)
    if not user or not user.is_active:
        return None
    device.last_used_at = datetime.now(UTC)
    request.session["user_id"] = user.id
    request.session["remember_me"] = True
    db.commit()
    return device


def revoke_trusted_device(db: Session, device: TrustedDevice) -> None:
    device.revoked_at = datetime.now(UTC)


def current_user(request: Request, db: Annotated[Session, Depends(get_db)]) -> User:
    user_id = request.session.get("user_id")
    if user_id:
        user = db.get(User, user_id)
        if user and user.is_active:
            return user
    device = trusted_device_from_request(request, db)
    if device:
        user = db.get(User, device.user_id)
        if user and user.is_active:
            return user
    auth = request.headers.get("Authorization", "")
    if auth.startswith("Bearer "):
        wanted = token_digest(auth.removeprefix("Bearer ").strip())
        item = db.scalar(
            select(ApiToken).where(
                ApiToken.token_hash == wanted,
                ApiToken.name != KOBO_TOKEN_NAME,
                ApiToken.revoked_at.is_(None),
            )
        )
        if item:
            user = db.get(User, item.user_id)
            if user and user.is_active:
                return user
    if auth.startswith("Basic "):
        try:
            username, token = (
                base64.b64decode(auth.removeprefix("Basic ").strip(), validate=True)
                .decode("utf-8")
                .split(":", 1)
            )
        except (ValueError, UnicodeDecodeError, binascii.Error):
            username = token = ""
        wanted = token_digest(token)
        item = db.scalar(
            select(ApiToken).where(
                ApiToken.token_hash == wanted,
                ApiToken.name != KOBO_TOKEN_NAME,
                ApiToken.revoked_at.is_(None),
            )
        )
        if item:
            user = db.get(User, item.user_id)
            if user and user.is_active and secrets.compare_digest(user.username, username):
                return user
    raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Authentication required")


def admin_user(user: Annotated[User, Depends(current_user)]) -> User:
    if user.role != Role.ADMIN:
        raise HTTPException(status_code=403, detail="Administrator access required")
    return user
