
import logging
import os
import secrets
from datetime import datetime

from fastapi import Depends, File, HTTPException, UploadFile
from sqlalchemy.orm import Session

from config import SEED_DEFAULT_USERS
from crud import user as crud_user
from database.session import SessionLocal, get_db
from model.models import User
from paths import AVATAR_DIR
from schema.schemas import PasswordUpdate
from service.auth_service import get_current_user, pwd_context
from service.utils_service import AVATAR_MAX_BYTES, resolve_image_upload_type


logger = logging.getLogger(__name__)

# Bootstrap accounts created on startup when SEED_DEFAULT_USERS is enabled.
# No password is hardcoded: each one comes from SEED_<USERNAME>_PASSWORD, and an
# unguessable random value is generated when that variable is unset.
DEFAULT_USERNAMES = ("admin", "demo")


def seed_password_env_var(username: str) -> str:
    return f"SEED_{username.upper()}_PASSWORD"


def seed_default_users() -> list[str]:
    """Create the bootstrap accounts, unless seeding is disabled by config."""
    if not SEED_DEFAULT_USERS:
        return []

    created = []
    db = SessionLocal()
    try:
        for username in DEFAULT_USERNAMES:
            existing_user = db.query(User).filter_by(username=username).first()
            if existing_user:
                continue

            env_var = seed_password_env_var(username)
            password = os.getenv(env_var, "").strip()
            if password:
                db.add(User(username=username, password_hash=pwd_context.hash(password)))
                created.append(username)
                continue

            db.add(User(username=username, password_hash=pwd_context.hash(secrets.token_urlsafe(32))))
            created.append(username)
            logger.warning(
                "%s is not set: seeded account '%s' with a random password that is never logged. "
                "See README for the reset procedure before exposing this service.",
                env_var,
                username,
            )
        if db.new:
            db.commit()
    finally:
        db.close()
    return created


def get_profile(user: User = Depends(get_current_user)):
    return crud_user.serialize_user_profile(user)


def update_password(body: PasswordUpdate, user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    if not pwd_context.verify(body.old_password, user.password_hash):
        raise HTTPException(400, "当前密码不正确")
    crud_user.update_password_hash(db, user, pwd_context.hash(body.new_password))
    return {"message": "密码修改成功"}


async def upload_avatar(file: UploadFile = File(...), user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    image_type = resolve_image_upload_type(file.content_type)
    if not image_type:
        raise HTTPException(400, "仅支持 png、jpg、jpeg、webp 格式头像")
    _content_type, ext = image_type

    content = await file.read()
    if len(content) > AVATAR_MAX_BYTES:
        raise HTTPException(400, "头像文件不能超过 2MB")

    filename = f"user_{user.id}_{int(datetime.now().timestamp())}{ext}"
    target = AVATAR_DIR / filename
    target.write_bytes(content)

    crud_user.update_avatar_path(db, user, f"/uploads/avatars/{filename}")
    return {"avatar": user.avatar}
