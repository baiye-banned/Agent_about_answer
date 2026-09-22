
import uuid
from datetime import datetime, timedelta, timezone

from fastapi import Depends, Header, HTTPException
from jose import JWTError, jwt
from passlib.context import CryptContext
from sqlalchemy.orm import Session

from config import ACCESS_TOKEN_EXPIRE_MINUTES, ALGORITHM, SECRET_KEY
from crud import auth as crud_auth
from database.session import get_db
from model.models import User
from schema.schemas import LoginRequest, LoginResponse


pwd_context = CryptContext(schemes=["bcrypt"], deprecated="auto")


def create_token(username: str, token_version: int = 0) -> str:
    """签发一枚 token，并带上两种吊销抓手（issue #184）。

    - `ver`：用户的令牌世代。改密时 users.token_version 递增，改密前签发的全部 token
      在这个字段上和当前世代对不上，一次作废。
    - `jti`：这一枚 token 自己的随机 id。登出只登记它，同一用户的其他会话不受影响。

    只带一个是不够的：只有 `ver` 会让一次登出踢掉该用户所有设备，只有 `jti` 则无法在
    改密时批量作废已经发出去的 token。
    """
    payload = {
        "sub": username,
        "ver": token_version,
        "jti": uuid.uuid4().hex,
        "exp": datetime.now(timezone.utc) + timedelta(minutes=ACCESS_TOKEN_EXPIRE_MINUTES),
    }
    return jwt.encode(payload, SECRET_KEY, algorithm=ALGORITHM)


def decode_token_claims(authorization: str) -> dict:
    """验签（含 exp）并返回 claims；全仓唯一的一处 jwt.decode。"""
    if not authorization:
        raise HTTPException(401, "Missing authorization header")
    scheme, _, token = authorization.partition(" ")
    if scheme.lower() != "bearer" or not token:
        raise HTTPException(401, "Invalid authorization header")
    try:
        return jwt.decode(token, SECRET_KEY, algorithms=[ALGORITHM])
    except JWTError:
        raise HTTPException(401, "Invalid token")


def _subject_of(claims: dict) -> str:
    username = claims.get("sub")
    if not isinstance(username, str) or not username.strip():
        raise HTTPException(401, "Invalid token")
    return username


def _token_version_of(claims: dict) -> int | None:
    """取 token 声明的世代；不是整数（缺失、字符串、bool）时返回 None。

    bool 要单独挡掉：Python 里 `True == 1`，用 `isinstance(ver, int)` 单独放行的实现
    会让 `"ver": true` 冒充世代 1。
    """
    version = claims.get("ver")
    if isinstance(version, bool) or not isinstance(version, int):
        return None
    return version


def decode_token(authorization: str) -> str:
    """只验签、只回用户名。判「这枚 token 现在还能不能用」请走 authenticate。"""
    return _subject_of(decode_token_claims(authorization))


def _authenticated_user(db: Session, claims: dict) -> User:
    """已经验过签的 claims → 用户对象；世代与吊销登记都在这里判定。

    与验签分开，是为了让 logout 在同一份 claims 上「先确认有效、再登记吊销」，不必
    解码两次——两次解码意味着两处判定，早晚会分叉。
    """
    user = db.query(User).filter_by(username=_subject_of(claims)).first()
    if not user:
        raise HTTPException(401, "User not found")
    # 不带 ver 的历史 token 在这里算出 None，同样对不上，需要重新登录。
    if _token_version_of(claims) != (user.token_version or 0):
        raise HTTPException(401, "Token has been revoked")
    if crud_auth.is_token_revoked(db, claims.get("jti") or ""):
        raise HTTPException(401, "Token has been revoked")
    return user


def authenticate(db: Session, authorization: str) -> User:
    """完整鉴权链：验签 → 回查用户 → 校验世代 → 查吊销登记。

    所有入口都必须走这一条。FastAPI 依赖注入路（get_current_user）和不经依赖注入、
    自己开会话的流式聊天（chat_service.stream_chat）共用它；吊销判定只挂在其中一条
    链上，就等于另一条链还认旧 token。
    """
    return _authenticated_user(db, decode_token_claims(authorization))


def get_current_user(authorization: str = Header(""), db: Session = Depends(get_db)) -> User:
    return authenticate(db, authorization)


# ---------------------------------------------------------------------------
# schemas
# ---------------------------------------------------------------------------


def login(body: LoginRequest, db: Session = Depends(get_db)):
    user = crud_auth.get_user_by_username(db, body.username)
    if not user or not pwd_context.verify(body.password, user.password_hash):
        raise HTTPException(401, "用户名或密码错误")
    # 新 token 必须带上用户当前世代，否则用户一改密就再也登不回来。
    token = create_token(body.username, user.token_version or 0)
    return LoginResponse(token=token, username=body.username)


def logout(authorization: str = Header(""), db: Session = Depends(get_db)):
    """使当前这一枚 token 失效，其他会话不受影响。

    先按完整鉴权链确认它现在有效（已登出 / 改密前的 token 在这里就 401），再登记它的
    jti。响应体保持原来的形状，前端不感知这次改动。
    """
    claims = decode_token_claims(authorization)
    _authenticated_user(db, claims)
    crud_auth.revoke_token(db, claims.get("jti") or "")
    return {"message": "ok"}


# ---------------------------------------------------------------------------
# user endpoints
# ---------------------------------------------------------------------------
