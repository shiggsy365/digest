from fastapi import HTTPException
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session
from starlette.requests import Request
from starlette.responses import Response

from digest.db import Base
from digest.main import (
    _session_cookie_lifetime,
    login,
    set_trusted_device_cookie,
    trusted_device_landing,
)
from digest.models import Role, TrustedDevice, User
from digest.security import (
    TRUSTED_DEVICE_COOKIE,
    create_trusted_device,
    current_user,
    hash_password,
    revoke_trusted_device,
)


def test_remembered_session_has_legacy_compatible_expiry() -> None:
    cookie = b"session=value; Max-Age=2592000; Path=/; SameSite=lax; Secure"

    result = _session_cookie_lifetime(cookie, True)

    assert b"Max-Age=2592000" in result
    assert b"; Expires=" in result
    assert result.endswith(b" GMT")


def test_browser_session_removes_persistent_cookie_attributes() -> None:
    cookie = (
        b"session=value; Max-Age=2592000; Path=/; SameSite=lax; "
        b"Expires=Tue, 29 Sep 2026 12:00:00 GMT; Secure"
    )

    result = _session_cookie_lifetime(cookie, False)

    assert b"Max-Age" not in result
    assert b"Expires" not in result


def test_trusted_device_cookie_restores_session() -> None:
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    with Session(engine, expire_on_commit=False) as db:
        user = User(
            username="reader",
            password_hash=hash_password("long-test-password"),
            role=Role.USER,
        )
        db.add(user)
        db.commit()
        _, token = create_trusted_device(db, user, "Kobo Touch")
        db.commit()
        request = Request({
            "type": "http",
            "method": "GET",
            "path": "/",
            "headers": [(b"cookie", f"{TRUSTED_DEVICE_COOKIE}={token}".encode())],
            "query_string": b"",
            "session": {},
        })

        restored = current_user(request, db)

        assert restored.id == user.id
        assert request.session["user_id"] == user.id
        assert request.session["remember_me"] is True


def test_revoked_trusted_device_cookie_does_not_restore_session() -> None:
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    with Session(engine, expire_on_commit=False) as db:
        user = User(
            username="reader",
            password_hash=hash_password("long-test-password"),
            role=Role.USER,
        )
        db.add(user)
        db.commit()
        device, token = create_trusted_device(db, user, "Kobo Touch")
        revoke_trusted_device(db, device)
        db.commit()
        request = Request({
            "type": "http",
            "method": "GET",
            "path": "/",
            "headers": [(b"cookie", f"{TRUSTED_DEVICE_COOKIE}={token}".encode())],
            "query_string": b"",
            "session": {},
        })

        try:
            current_user(request, db)
        except HTTPException as exc:
            assert exc.status_code == 401
        else:
            raise AssertionError("Revoked trusted device authenticated")


def test_login_with_remember_me_reuses_the_current_trusted_device() -> None:
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    with Session(engine, expire_on_commit=False) as db:
        user = User(
            username="reader",
            password_hash=hash_password("long-test-password"),
            role=Role.USER,
        )
        db.add(user)
        db.commit()
        device, token = create_trusted_device(db, user, "Kobo Touch")
        db.commit()

        request = Request({
            "type": "http",
            "method": "POST",
            "path": "/login",
            "headers": [(b"cookie", f"{TRUSTED_DEVICE_COOKIE}={token}".encode())],
            "query_string": b"",
            "session": {"csrf": "csrf-token"},
        })

        login(
            request,
            db,
            username="reader",
            password="long-test-password",
            form_csrf="csrf-token",
            remember_me="true",
        )

        active = db.scalars(
            select(TrustedDevice).where(
                TrustedDevice.user_id == user.id, TrustedDevice.revoked_at.is_(None)
            )
        ).all()
        assert [item.id for item in active] == [device.id]


def _cookie_request(user_agent: bytes) -> Request:
    return Request({
        "type": "http",
        "method": "POST",
        "path": "/login",
        "headers": [(b"user-agent", user_agent)],
        "query_string": b"",
        "session": {},
    })


def _set_cookie_header(response: Response) -> bytes:
    (value,) = [
        value for name, value in response.raw_headers if name.lower() == b"set-cookie"
    ]
    return value


def test_trusted_device_cookie_has_legacy_compatible_expiry() -> None:
    response = Response()

    set_trusted_device_cookie(_cookie_request(b"Mozilla/5.0 Chrome"), response, "dtd_example-token")

    value = _set_cookie_header(response)
    assert b"Max-Age=" in value
    assert b"expires=" in value.lower()


def test_trusted_device_cookie_drops_samesite_for_ereader_browsers() -> None:
    response = Response()

    set_trusted_device_cookie(
        _cookie_request(b"Mozilla/5.0 (Kobo Touch 0390/4.45.23697)"), response, "dtd_example-token"
    )

    value = _set_cookie_header(response)
    assert b"samesite" not in value.lower()


def test_trusted_device_cookie_keeps_samesite_for_regular_browsers() -> None:
    response = Response()

    set_trusted_device_cookie(_cookie_request(b"Mozilla/5.0 Chrome"), response, "dtd_example-token")

    value = _set_cookie_header(response)
    assert b"samesite=lax" in value.lower()


def test_login_on_ereader_redirects_to_bookmarkable_landing_page() -> None:
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    with Session(engine, expire_on_commit=False) as db:
        user = User(
            username="reader",
            password_hash=hash_password("long-test-password"),
            role=Role.USER,
        )
        db.add(user)
        db.commit()

        request = Request({
            "type": "http",
            "method": "POST",
            "path": "/login",
            "headers": [(b"user-agent", b"Mozilla/5.0 (Kobo Touch 0390/4.45.23697)")],
            "query_string": b"",
            "session": {"csrf": "csrf-token"},
        })

        response = login(
            request,
            db,
            username="reader",
            password="long-test-password",
            form_csrf="csrf-token",
            remember_me="true",
        )

        assert response.status_code == 303
        location = response.headers["location"]
        assert location.startswith("/trusted-device/dtd_")


def test_trusted_device_landing_authenticates_and_shows_bookmark_url() -> None:
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    with Session(engine, expire_on_commit=False) as db:
        user = User(
            username="reader",
            password_hash=hash_password("long-test-password"),
            role=Role.USER,
        )
        db.add(user)
        db.commit()
        _, token = create_trusted_device(db, user, "Kobo Touch")
        db.commit()

        request = Request({
            "type": "http",
            "method": "GET",
            "path": f"/trusted-device/{token}",
            "headers": [(b"user-agent", b"Mozilla/5.0 (Kobo Touch 0390/4.45.23697)")],
            "query_string": b"",
            "session": {},
        })

        response = trusted_device_landing(token, request, db)
        html = response.body.decode()

        assert response.status_code == 200
        assert request.session["user_id"] == user.id
        assert token in html
        assert _set_cookie_header(response).startswith(f"{TRUSTED_DEVICE_COOKIE}={token}".encode())


def test_trusted_device_landing_redirects_invalid_token_to_login() -> None:
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    with Session(engine, expire_on_commit=False) as db:
        request = Request({
            "type": "http",
            "method": "GET",
            "path": "/trusted-device/dtd_does-not-exist",
            "headers": [],
            "query_string": b"",
            "session": {},
        })

        response = trusted_device_landing("dtd_does-not-exist", request, db)

        assert response.status_code == 303
        assert response.headers["location"] == "/login"
