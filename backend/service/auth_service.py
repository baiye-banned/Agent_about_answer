
from datetime import datetime, timedelta, timezone

from fastapi import Depends, Header, HTTPException, Request
from jose import JWTError, jwt
from passlib.context import CryptContext
from sqlalchemy.orm import Session

from config import ACCESS_TOKEN_EXPIRE_MINUTES, ALGORITHM, SECRET_KEY
from crud import auth as crud_auth
from database.session import get_db
from model.models import User
from schema.schemas import LoginRequest, LoginResponse
from service import rate_limit


pwd_context = CryptContext(schemes=["bcrypt"], deprecated="auto")


def create_token(username: str) -> str:
    payload = {
        "sub": username,
        "exp": datetime.now(timezone.utc) + timedelta(minutes=ACCESS_TOKEN_EXPIRE_MINUTES),
    }
    return jwt.encode(payload, SECRET_KEY, algorithm=ALGORITHM)



def decode_token(authorization: str) -> str:
    return _decode_token(authorization)


def _decode_token(authorization: str) -> str:
    """Extract and validate the token, return username."""
    if not authorization:
        raise HTTPException(401, "Missing authorization header")
    scheme, _, token = authorization.partition(" ")
    if scheme.lower() != "bearer" or not token:
        raise HTTPException(401, "Invalid authorization header")
    try:
        payload = jwt.decode(token, SECRET_KEY, algorithms=[ALGORITHM])
        username = payload.get("sub")
        if not isinstance(username, str) or not username.strip():
            raise HTTPException(401, "Invalid token")
        return username
    except JWTError:
        raise HTTPException(401, "Invalid token")


def get_current_user(authorization: str = Header(""), db: Session = Depends(get_db)) -> User:
    username = _decode_token(authorization)
    user = db.query(User).filter_by(username=username).first()
    if not user:
        raise HTTPException(401, "User not found")
    return user


# ---------------------------------------------------------------------------
# schemas
# ---------------------------------------------------------------------------


def _login_throttle_key(request: Request, username: str):
    """登录节流的键：规范化后的账号 + 来源地址（issue #183 的「同一账号 + 同一来源」）。

    - 用 ASGI scope 里的对端地址，**不读 X-Forwarded-For**：那个头由客户端自由伪造，拿它做键
      等于把「换个值就能继续撞」的开关交出去。反代之后取到的是反代地址，等价于按账号全局限流，
      只会更严，不会更松。
    - 账号按小写归一：MySQL 的默认排序规则对用户名大小写不敏感，只按原样做键可以用大小写绕开。
    """
    client = getattr(request, "client", None)
    host = getattr(client, "host", "") or "unknown"
    return (username.strip().lower(), host)


def login(body: LoginRequest, request: Request, db: Session = Depends(get_db)):
    key = _login_throttle_key(request, body.username)
    retry_after = rate_limit.login_failures.retry_after(key)
    if retry_after:
        # 先判阈值再验口令：拒绝路径不跑 bcrypt，撞库的算力成本才真的被掐掉。
        raise HTTPException(
            429,
            f"登录失败次数过多，请在 {retry_after} 秒后重试。",
            headers={"Retry-After": str(retry_after)},
        )
    user = crud_auth.get_user_by_username(db, body.username)
    if not user or not pwd_context.verify(body.password, user.password_hash):
        rate_limit.login_failures.record_failure(key)
        raise HTTPException(401, "用户名或密码错误")
    rate_limit.login_failures.reset(key)
    token = create_token(body.username)
    return LoginResponse(token=token, username=body.username)


def logout(_user: User = Depends(get_current_user)):
    return {"message": "ok"}


# ---------------------------------------------------------------------------
# user endpoints
# ---------------------------------------------------------------------------
