
import logging
import os
import secrets
from pathlib import Path
from uuid import uuid4

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

# 头像对外路径的前缀。库里 `users.avatar` 列存的就是 `前缀 + 文件名`，读取路由挂在同一路径上，
# 写入侧、读取侧、删除侧三处共用这一个常量：三份字面量只要漂移一份，用户头像就会在某一侧
# 静默 404（写进去的名字读不出来，或者读出来的路径删不掉）。
AVATAR_URL_PREFIX = "/uploads/avatars/"


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
    if not avatar_path or not avatar_path.startswith(AVATAR_URL_PREFIX):
        return None
    filename = avatar_path[len(AVATAR_URL_PREFIX):]
    if not filename or filename in (".", "..") or filename != Path(filename).name:
        return None
    return AVATAR_DIR / filename


def avatar_file_for_owner(user: User, filename: str) -> Path | None:
    """请求方自己的头像文件；请求的不是「他 avatar 列里记着的那一个」就返回 None。

    读取面的授权判据（issue #186）。归属不是从文件名里解析出来的，而是与库里的 `users.avatar`
    逐字比对：文件名是请求方给的，库里的值是本服务写下的，两者相等才说明这一份确实是他的。

    为什么不按「文件名里的 user id 等于请求方 id」判：那要求文件名里始终带着 id，而 id 是
    小整数、可枚举，正是这条 issue 要消掉的东西。绑定到 avatar 列之后，下一节把文件名换成
    随机键时判据不用跟着改，历史遗留的旧命名文件也自动继续可读（迁移面见 issue 验收第 5 条：
    这里选的是「保留兼容读取」，不搬迁旧文件）。

    返回路径前仍走 `_avatar_file_from_path`，因为 avatar 列是库里的值、可能被手工订正成
    `../` 之类；能否越出目录不取决于谁在请求。
    """
    if not filename:
        return None
    if user.avatar != f"{AVATAR_URL_PREFIX}{filename}":
        return None
    target = _avatar_file_from_path(user.avatar)
    if target is None or not target.is_file():
        return None
    return target


def _remove_replaced_avatar(previous_avatar: str, new_avatar: str) -> None:
    """删除被替换下来的旧头像文件；删不掉只记日志，不影响新头像已经生效。

    先落库、后删文件：库里指向新文件、磁盘上最多多留一个旧文件（可回收）；反过来一旦
    落库失败，avatar 列会指向一个已经被删掉的文件，用户头像直接 404，而旧文件已经没了。
    """
    # 首次上传没有旧文件；`previous_avatar == new_avatar` 这一支在随机键（issue #186）之后
    # 正常链路里已经够不着了（要 uuid4 撞上同一个值才可能），保留的是「绝不删掉刚写进去的
    # 那一个」这条不变量——名字生成方式哪天又变回确定性时，它挡的是「刚上传的头像立刻 404」。
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

    # 文件名必须是不可枚举的随机键（issue #186）。旧模板 `user_{id}_{ts}` 的两个分量都可预测，
    # 拿 id 范围乘一个时间窗就能把别人的头像逐个取走；这里对齐 OSS 侧的 uuid4().hex，
    # 122 位随机，按名字猜不出来。文件名里不再带 user id 是有意的：id 是连续小整数，
    # 留在名字里等于继续对外暴露「这份头像属于第几号用户」。
    filename = f"{uuid4().hex}{ext}"
    target = AVATAR_DIR / filename
    target.write_bytes(content)

    previous_avatar = user.avatar or ""
    new_avatar = f"{AVATAR_URL_PREFIX}{filename}"
    crud_user.update_avatar_path(db, user, new_avatar)
    _remove_replaced_avatar(previous_avatar, new_avatar)
    return {"avatar": user.avatar}
