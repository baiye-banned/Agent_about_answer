from sqlalchemy.orm import Session

from model.models import User


def serialize_user_profile(user: User) -> dict:
    return {
        "username": user.username,
        "avatar": user.avatar or "",
        "created_at": user.created_at.isoformat() if user.created_at else "",
    }


def update_password_hash(db: Session, user: User, password_hash: str) -> User:
    """换掉口令哈希，并把令牌世代 +1。

    两件事必须在同一个事务里落库：只改哈希，改密前签发的 token 在剩余有效期内继续可用
    （issue #184 的原始缺陷）；只加世代不改哈希，则用户以为改掉的口令其实没改。
    """
    user.password_hash = password_hash
    user.token_version = (user.token_version or 0) + 1
    db.commit()
    db.refresh(user)
    return user


def update_avatar_path(db: Session, user: User, avatar_path: str) -> User:
    user.avatar = avatar_path
    db.commit()
    db.refresh(user)
    return user

