import pytest
from fastapi import HTTPException
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session
from starlette.requests import Request

from digest.db import Base
from digest.models import ApiToken, AuditEvent, Role, User
from digest.security import current_user, hash_password
from digest.tokens import create_token, revoke_token


def token_session() -> Session:
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    return Session(engine, expire_on_commit=False)


def bearer_request(token: str) -> Request:
    return Request(
        {
            "type": "http",
            "method": "GET",
            "path": "/opds",
            "headers": [(b"authorization", f"Bearer {token}".encode())],
            "session": {},
        }
    )


def basic_request(username: str, token: str) -> Request:
    encoded = base64.b64encode(f"{username}:{token}".encode())
    return Request(
        {
            "type": "http",
            "method": "GET",
            "path": "/opds",
            "headers": [(b"authorization", b"Basic " + encoded)],
            "session": {},
        }
    )


def add_admin(db: Session) -> User:
    admin = User(
        username="admin",
        password_hash=hash_password("initial-password"),
        role=Role.ADMIN,
    )
    db.add(admin)
    db.commit()
    return admin


def test_api_token_is_only_stored_as_hash_and_authenticates() -> None:
    with token_session() as db:
        admin = add_admin(db)

        item, plain_token = create_token(db, admin, admin, "OPDS reader")

        assert plain_token.startswith("dgt_")
        assert plain_token not in item.token_hash
        assert len(item.token_hash) == 64
        assert current_user(bearer_request(plain_token), db).id == admin.id
        event = db.scalar(select(AuditEvent).where(AuditEvent.event == "api_token_created"))
        assert event is not None
        assert current_user(basic_request("admin", plain_token), db).id == admin.id

        with pytest.raises(HTTPException):
            current_user(basic_request("wrong-user", plain_token), db)


def test_revoked_token_no_longer_authenticates() -> None:
    with token_session() as db:
        admin = add_admin(db)
        item, plain_token = create_token(db, admin, admin, "Temporary client")

        revoke_token(db, admin, item)

        assert item.revoked_at is not None
        assert db.scalar(select(ApiToken).where(ApiToken.revoked_at.is_not(None))) is item
        with pytest.raises(HTTPException) as error:
            current_user(bearer_request(plain_token), db)
        assert error.value.status_code == 401


def test_token_for_disabled_user_is_rejected() -> None:
    with token_session() as db:
        admin = add_admin(db)
        _, plain_token = create_token(db, admin, admin, "Disabled account token")
        admin.is_active = False
        db.commit()

        with pytest.raises(HTTPException) as error:
            current_user(bearer_request(plain_token), db)
        assert error.value.status_code == 401


def test_admin_can_create_token_owned_by_another_user() -> None:
    with token_session() as db:
        admin = add_admin(db)
        reader = User(
            username="reader",
            password_hash=hash_password("reader-password"),
            role=Role.USER,
        )
        db.add(reader)
        db.commit()

        item, plain_token = create_token(db, admin, reader, "Reader device")

        assert item.created_by == admin.id
        assert item.user_id == reader.id
        assert current_user(basic_request("reader", plain_token), db).id == reader.id
        with pytest.raises(HTTPException):
            current_user(basic_request("admin", plain_token), db)


import base64
