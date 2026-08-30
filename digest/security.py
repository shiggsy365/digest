import base64
import binascii
import hashlib
import secrets
from typing import Annotated

from fastapi import Depends, HTTPException, Request, status
from pwdlib import PasswordHash
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from .db import get_db
from .models import ApiToken, Role, User

passwords = PasswordHash.recommended()
KOBO_TOKEN_NAME = "Kobo device"


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


def current_user(request: Request, db: Annotated[Session, Depends(get_db)]) -> User:
    user_id = request.session.get("user_id")
    if user_id:
        user = db.get(User, user_id)
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
