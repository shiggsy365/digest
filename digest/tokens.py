from sqlalchemy.orm import Session

from .models import ApiToken, AuditEvent, User, now
from .security import new_api_token, token_digest


class TokenError(ValueError):
    pass


def create_token(db: Session, actor: User, owner: User, name: str) -> tuple[ApiToken, str]:
    name = name.strip()
    if not name or len(name) > 100:
        raise TokenError("Token name must contain between 1 and 100 characters.")
    plain_token = new_api_token()
    if not owner.is_active:
        raise TokenError("Tokens can only be assigned to active users.")
    item = ApiToken(
        name=name,
        token_hash=token_digest(plain_token),
        user_id=owner.id,
        created_by=actor.id,
    )
    db.add(item)
    db.flush()
    db.add(
        AuditEvent(
            event="api_token_created",
            user_id=actor.id,
            message=f"Created API token {item.name} for {owner.username} (token {item.id})",
        )
    )
    db.commit()
    return item, plain_token


def revoke_token(db: Session, actor: User, item: ApiToken) -> None:
    if item.revoked_at is None:
        item.revoked_at = now()
        db.add(
            AuditEvent(
                event="api_token_revoked",
                user_id=actor.id,
                message=f"Revoked API token {item.name} (token {item.id})",
            )
        )
        db.commit()
