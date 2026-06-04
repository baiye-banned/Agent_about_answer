import pytest
from fastapi import HTTPException
from jose import jwt

from config import ALGORITHM, SECRET_KEY
from service.auth_service import create_token, decode_token


def test_decode_token_accepts_bearer_token():
    token = create_token("alice")

    assert decode_token(f"Bearer {token}") == "alice"


def test_decode_token_rejects_token_without_subject():
    token = jwt.encode({"role": "user"}, SECRET_KEY, algorithm=ALGORITHM)

    with pytest.raises(HTTPException) as exc_info:
        decode_token(f"Bearer {token}")

    assert exc_info.value.status_code == 401


@pytest.mark.parametrize("subject", ["", "   ", 123])
def test_decode_token_rejects_invalid_subject(subject):
    token = jwt.encode({"sub": subject}, SECRET_KEY, algorithm=ALGORITHM)

    with pytest.raises(HTTPException) as exc_info:
        decode_token(f"Bearer {token}")

    assert exc_info.value.status_code == 401


@pytest.mark.parametrize(
    "authorization",
    [
        "",
        "Token abc",
        "Bearer",
        "Bearer ",
        "Bearer invalid-token",
    ],
)
def test_decode_token_rejects_invalid_authorization_header(authorization):
    with pytest.raises(HTTPException) as exc_info:
        decode_token(authorization)

    assert exc_info.value.status_code == 401
