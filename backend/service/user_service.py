
import logging
import os
import secrets
from datetime import datetime
from pathlib import Path

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


def _avatar_file_from_path(avatar_path: str) -> Path | None:
    """把库里存的头像路径映射回头像目录里的文件；不是本服务写下的路径就返回 None。

    avatar 列由上传接口写入、只在 profile 里读出去，但它是数据库里的值：历史数据、手工
    订正或将来某个写接口都可能把它塞成 `../` 之类的路径，那条路径会直接变成删除目标，
    所以这里只认「头像目录下的单个文件名」。
    """
    prefix = "/uploads/avatars/"
    if not avatar_path or not avatar_path.startswith(prefix):
        return None
    filename = avatar_path[len(prefix):]
    if not filename or filename in (".", "..") or filename != Path(filename).name:
        return None
    return AVATAR_DIR / filename


def _remove_replaced_avatar(previous_avatar: str, new_avatar: str) -> None:
    """删除被替换下来的旧头像文件；删不掉只记日志，不影响新头像已经生效。

    先落库、后删文件：库里指向新文件、磁盘上最多多留一个旧文件（可回收）；反过来一旦
    落库失败，avatar 列会指向一个已经被删掉的文件，用户头像直接 404，而旧文件已经没了。
    """
    # 首次上传没有旧文件；同一秒内重复上传同扩展名会落到同一个文件名，
    # 那个路径此刻就是刚写进去的新文件，不能当旧文件删掉。
    if not previous_avatar or previous_avatar == new_avatar:
        return
    target = _avatar_file_from_path(previous_avatar)
    if target is None:
        return
    try:
        target.unlink()
    except FileNotFoundError:
        # 文件已经不在了：旧头像不可达这个目标状态已经满足。
        return
    except OSError as exc:
        logger.warning(
            "replaced avatar cleanup failed: path=%s error=%s",
            previous_avatar,
            exc,
            exc_info=exc,
        )


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

    previous_avatar = user.avatar or ""
    new_avatar = f"/uploads/avatars/{filename}"
    crud_user.update_avatar_path(db, user, new_avatar)
    _remove_replaced_avatar(previous_avatar, new_avatar)
    return {"avatar": user.avatar}
