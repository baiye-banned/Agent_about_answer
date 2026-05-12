
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
        return payload.get("sub", "")
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


def login(body: LoginRequest, db: Session = Depends(get_db)):
    user = crud_auth.get_user_by_username(db, body.username)
    if not user or not pwd_context.verify(body.password, user.password_hash):
        raise HTTPException(401, "用户名或密码错误")
    token = create_token(body.username)
    return LoginResponse(token=token, username=body.username)


def logout(_user: User = Depends(get_current_user)):
    return {"message": "ok"}


# ---------------------------------------------------------------------------
# user endpoints
# ---------------------------------------------------------------------------
