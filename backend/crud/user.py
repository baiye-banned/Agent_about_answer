from sqlalchemy.orm import Session

from model.models import User


def serialize_user_profile(user: User) -> dict:
    return {
        "username": user.username,
        "avatar": user.avatar or "",
        "created_at": user.created_at.isoformat() if user.created_at else "",
    }


def update_password_hash(db: Session, user: User, password_hash: str) -> User:
    user.password_hash = password_hash
    db.commit()
    db.refresh(user)
    return user


def update_avatar_path(db: Session, user: User, avatar_path: str) -> User:
    user.avatar = avatar_path
    db.commit()
    db.refresh(user)
    return user

