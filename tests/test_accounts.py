from sqlalchemy import create_engine, func, select
from sqlalchemy.orm import Session

from digest.accounts import AccountError, create_account, reset_password, update_account
from digest.db import Base
from digest.models import AuditEvent, Role, User
from digest.security import hash_password, verify_password


def account_session() -> Session:
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    return Session(engine, expire_on_commit=False)


def add_admin(db: Session, username: str = "admin") -> User:
    admin = User(
        username=username,
        password_hash=hash_password("initial-password"),
        role=Role.ADMIN,
    )
    db.add(admin)
    db.commit()
    return admin


def test_admin_can_create_user_and_action_is_audited() -> None:
    with account_session() as db:
        admin = add_admin(db)

        account = create_account(
            db,
            admin,
            "reader",
            "a-secure-password",
            "a-secure-password",
            Role.USER,
        )

        assert account.role == Role.USER
        assert account.is_active is True
        assert verify_password("a-secure-password", account.password_hash)
        event = db.scalar(select(AuditEvent).where(AuditEvent.event == "account_created"))
        assert event is not None
        assert event.user_id == admin.id


def test_usernames_are_unique_without_case_sensitivity() -> None:
    with account_session() as db:
        admin = add_admin(db)
        create_account(db, admin, "Reader", "a-secure-password", "a-secure-password", Role.USER)

        try:
            create_account(db, admin, "reader", "another-password", "another-password", Role.USER)
        except AccountError as exc:
            assert str(exc) == "That username is already in use."
        else:
            raise AssertionError("case-insensitive duplicate username was accepted")


def test_last_active_administrator_cannot_be_demoted_or_disabled() -> None:
    with account_session() as db:
        admin = add_admin(db)

        for role, active in [(Role.USER, True), (Role.ADMIN, False)]:
            try:
                update_account(db, admin, admin, admin.username, role, active)
            except AccountError as exc:
                assert "at least one active administrator" in str(exc)
            else:
                raise AssertionError("last active administrator protection failed")

        assert admin.role == Role.ADMIN
        assert admin.is_active is True


def test_password_reset_replaces_hash_and_is_audited() -> None:
    with account_session() as db:
        admin = add_admin(db)
        account = create_account(
            db, admin, "reader", "a-secure-password", "a-secure-password", Role.USER
        )
        old_hash = account.password_hash

        reset_password(db, admin, account, "replacement-password", "replacement-password")

        assert account.password_hash != old_hash
        assert verify_password("replacement-password", account.password_hash)
        assert (
            db.scalar(select(func.count(AuditEvent.id)).where(AuditEvent.event == "password_reset"))
            == 1
        )
