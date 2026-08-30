from sqlalchemy import func, select
from sqlalchemy.orm import Session

from .models import AuditEvent, Role, User
from .security import hash_password


class AccountError(ValueError):
    pass


def validate_username(username: str) -> str:
    username = username.strip()
    if len(username) < 3 or len(username) > 80:
        raise AccountError("Username must contain between 3 and 80 characters.")
    return username


def ensure_unique_username(db: Session, username: str, excluding: int | None = None) -> None:
    query = select(User.id).where(func.lower(User.username) == username.casefold())
    if excluding is not None:
        query = query.where(User.id != excluding)
    if db.scalar(query) is not None:
        raise AccountError("That username is already in use.")


def validate_password(password: str, confirm: str) -> None:
    if len(password) < 12:
        raise AccountError("Password must contain at least 12 characters.")
    if password != confirm:
        raise AccountError("The password confirmation does not match.")


def create_account(
    db: Session,
    actor: User,
    username: str,
    password: str,
    confirm: str,
    role: Role,
) -> User:
    username = validate_username(username)
    validate_password(password, confirm)
    ensure_unique_username(db, username)
    account = User(username=username, password_hash=hash_password(password), role=role)
    db.add(account)
    db.flush()
    db.add(
        AuditEvent(
            event="account_created",
            user_id=actor.id,
            message=f"Created {role.value} account {account.username} (user {account.id})",
        )
    )
    db.commit()
    return account


def update_account(
    db: Session,
    actor: User,
    account: User,
    username: str,
    role: Role,
    is_active: bool,
) -> None:
    username = validate_username(username)
    ensure_unique_username(db, username, excluding=account.id)
    removes_active_admin = (
        account.role == Role.ADMIN and account.is_active and (role != Role.ADMIN or not is_active)
    )
    if removes_active_admin:
        active_admins = db.scalar(
            select(func.count(User.id)).where(User.role == Role.ADMIN, User.is_active.is_(True))
        )
        if (active_admins or 0) <= 1:
            raise AccountError("Digest must retain at least one active administrator.")
    previous = f"{account.username}/{account.role.value}/active={account.is_active}"
    account.username = username
    account.role = role
    account.is_active = is_active
    db.add(
        AuditEvent(
            event="account_updated",
            user_id=actor.id,
            message=(
                f"Updated user {account.id} from {previous} to "
                f"{username}/{role.value}/active={is_active}"
            ),
        )
    )
    db.commit()


def reset_password(db: Session, actor: User, account: User, password: str, confirm: str) -> None:
    validate_password(password, confirm)
    account.password_hash = hash_password(password)
    db.add(
        AuditEvent(
            event="password_reset",
            user_id=actor.id,
            message=f"Reset password for {account.username} (user {account.id})",
        )
    )
    db.commit()
