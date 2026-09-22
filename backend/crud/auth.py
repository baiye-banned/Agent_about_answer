from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from model.models import RevokedToken, User


def get_user_by_username(db: Session, username: str) -> User | None:
    return db.query(User).filter_by(username=username).first()


def is_token_revoked(db: Session, jti: str) -> bool:
    return db.get(RevokedToken, jti) is not None


def revoke_token(db: Session, jti: str) -> None:
    """把一枚 token 记入吊销表；重复登记同一枚 token 不报错。

    同一枚 token 并发登出（双击按钮、重试）会让两个请求同时走到 INSERT，其中一个撞主键
    唯一约束——那是「已经吊销了」的另一种说法，不是错误，所以回滚后正常返回。
    """
    if not jti or is_token_revoked(db, jti):
        return
    db.add(RevokedToken(jti=jti))
    try:
        db.commit()
    except IntegrityError:
        db.rollback()

