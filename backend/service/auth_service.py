
import unicodedata
import uuid
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


def _normalize_login_account(username: str) -> str:
    """把一个账号折叠到「库侧认不出区别」的那一档：去首尾空白、去大小写、去重音。

    为什么不能只做 `strip().lower()`：用户名的查库比对走的是库的排序规则，本仓在
    `database/session.py` 里把 users 表钉成 `utf8mb4_unicode_ci`——它大小写不敏感、**重音也
    不敏感**，`café` 与 `cafe` 查的是同一行。键若比库的等价类更细，攻击者只要换重音写法就能
    各开一个桶，把撞库预算乘以变体数。

    做法是 NFKD 分解后丢掉组合记号（ccc != 0），`café`/`cafe`/`cafe\\u0301`/`CAFÉ` 因此同键。
    只丢组合记号，**不丢非 ASCII 字符**：若改用 `encode("ascii", "ignore")`，所有中文账号会
    一起压成空串，把互不相干的账号并成一桶，限流就变成了互相误锁。

    代价写清楚：折叠只会让键**变粗**（合并桶），不会变细，所以关不掉的是「误锁」而不是
    「绕过」。而键粗于库的等价类时才会误锁不同账号，本仓库侧就是 `utf8mb4_unicode_ci`，
    被并到一起的写法在库里本来就是同一个账号，故不引入新的误锁面。

    仍有残余：`ø`/`đ`/`ł`/`ß` 这类「不做分解分解、靠次级权重区分」的字母，NFKD 拆不开，与
    库侧的等价类仍有缝。要彻底对齐得改用查库后的规范标识（用户主键），代价是把查库挪到
    闸门之前——本函数是能覆盖常见重音写法的收敛口径。
    """
    decomposed = unicodedata.normalize("NFKD", username.strip())
    return "".join(ch for ch in decomposed if not unicodedata.combining(ch)).lower()


def _login_throttle_key(request: Request, username: str):
    """登录节流的键：规范化后的账号 + 来源地址（issue #183 的「同一账号 + 同一来源」）。

    - 用 ASGI scope 里的对端地址，**不读 X-Forwarded-For**：那个头由客户端自由伪造，拿它做键
      等于把「换个值就能继续撞」的开关交出去。反代之后取到的是反代地址，等价于按账号全局限流，
      只会更严，不会更松。
    - 账号按 `_normalize_login_account` 折叠：库侧对用户名大小写与重音都不敏感，只按原样做键
      可以用大小写、也可以用重音变体绕开。
    """
    client = getattr(request, "client", None)
    host = getattr(client, "host", "") or "unknown"
    return (_normalize_login_account(username), host)


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
