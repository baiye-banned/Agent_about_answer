import pytest
from fastapi import HTTPException
from jose import JWTError, jwt

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


# ---------------------------------------------------------------------------
# 底层实现链路（issue #98）
#
# 本服务 ALGORITHM 固定为 HS256，容易被误以为「走 HMAC、与 cryptography 无关」。
# 实际 python-jose 的 HMACKey 就是 jose.backends.cryptography_backend.CryptographyHMACKey，
# 签名落到 cryptography.hazmat.primitives.hmac.HMAC 上——也就是说登录签发/校验这条路
# 真的压在 cryptography 上。升级 cryptography 跨 6 个大版本后，这里必须仍然成立：
# 后端被换掉或该 API 被移除时，下面这条会直接失败，而不是等到线上登录报错。
# ---------------------------------------------------------------------------


def test_hs256_signing_goes_through_the_cryptography_backend():
    from jose.backends import HMACKey

    assert HMACKey.__module__ == "jose.backends.cryptography_backend"
    assert HMACKey.__name__ == "CryptographyHMACKey"

    token = create_token("alice")
    assert decode_token(f"Bearer {token}") == "alice"


def test_decode_token_rejects_alg_none_forgery():
    """alg=none 伪造：头部声明无签名，签名为空，必须被拒。"""
    import base64
    import json

    def segment(payload: dict) -> bytes:
        return base64.urlsafe_b64encode(json.dumps(payload).encode()).rstrip(b"=")

    forged = (segment({"alg": "none", "typ": "JWT"}) + b"." + segment({"sub": "alice"}) + b".").decode()

    with pytest.raises(HTTPException) as exc_info:
        decode_token(f"Bearer {forged}")

    assert exc_info.value.status_code == 401


def test_decode_token_rejects_tampered_signature():
    token = create_token("alice")
    header, payload, _signature = token.split(".")
    tampered = f"{header}.{payload}.{'A' * 43}"

    with pytest.raises(HTTPException) as exc_info:
        decode_token(f"Bearer {tampered}")

    assert exc_info.value.status_code == 401


def test_es256_round_trip_through_cryptography_backend():
    """非对称路径：由 cryptography 生成 EC 密钥，经 python-jose 签发/校验，
    并以「换一把公钥必须验不过」确认校验委实依赖密钥材料而非只看格式。
    """
    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.asymmetric import ec

    def public_pem(private_key) -> str:
        return private_key.public_key().public_bytes(
            encoding=serialization.Encoding.PEM,
            format=serialization.PublicFormat.SubjectPublicKeyInfo,
        ).decode()

    private_key = ec.generate_private_key(ec.SECP256R1())
    private_pem = private_key.private_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PrivateFormat.PKCS8,
        encryption_algorithm=serialization.NoEncryption(),
    )

    token = jwt.encode({"sub": "alice", "exp": 4102444800}, private_pem, algorithm="ES256")

    assert jwt.decode(token, public_pem(private_key), algorithms=["ES256"])["sub"] == "alice"

    with pytest.raises(JWTError):
        jwt.decode(
            token,
            public_pem(ec.generate_private_key(ec.SECP256R1())),
            algorithms=["ES256"],
        )
