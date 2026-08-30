from digest.main import _session_cookie_lifetime


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
