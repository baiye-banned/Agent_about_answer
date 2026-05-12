
from datetime import datetime

from fastapi import Depends, File, HTTPException, UploadFile
from sqlalchemy.orm import Session

from crud import user as crud_user
from database.session import SessionLocal, get_db
from model.models import User
from paths import AVATAR_DIR
from schema.schemas import PasswordUpdate
from service.auth_service import get_current_user, pwd_context


def seed_default_users() -> None:
    db = SessionLocal()
    try:
        if not db.query(User).first():
            for username, password in [("admin", "admin123"), ("demo", "demo123")]:
                db.add(User(username=username, password_hash=pwd_context.hash(password)))
            db.commit()
    finally:
        db.close()


def get_profile(user: User = Depends(get_current_user)):
    return crud_user.serialize_user_profile(user)


def update_password(body: PasswordUpdate, user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    if not pwd_context.verify(body.old_password, user.password_hash):
        raise HTTPException(400, "当前密码不正确")
    crud_user.update_password_hash(db, user, pwd_context.hash(body.new_password))
    return {"message": "密码修改成功"}


async def upload_avatar(file: UploadFile = File(...), user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    allowed_types = {
        "image/png": ".png",
        "image/jpeg": ".jpg",
        "image/webp": ".webp",
    }
    content_type = file.content_type or ""
    ext = allowed_types.get(content_type)
    if not ext:
        raise HTTPException(400, "仅支持 png、jpg、jpeg、webp 格式头像")

    content = await file.read()
    if len(content) > 2 * 1024 * 1024:
        raise HTTPException(400, "头像文件不能超过 2MB")

    filename = f"user_{user.id}_{int(datetime.now().timestamp())}{ext}"
    target = AVATAR_DIR / filename
    target.write_bytes(content)

    crud_user.update_avatar_path(db, user, f"/uploads/avatars/{filename}")
    return {"avatar": user.avatar}
