"""SECRET_KEY 缺失或仍是仓库公开占位值时的启动门禁与鉴权行为（issue #8）。

核心不变量：仓库内置的 `change-this-secret-key-in-production` 永远不能成为进程实际使用的
签名密钥，因此用它签发的 token 必须被拒绝；未显式配置 SECRET_KEY 时服务必须拒绝启动。
"""

import os
import subprocess
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
from fastapi import FastAPI, HTTPException
from fastapi.testclient import TestClient
from jose import jwt
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

import config
from database import session as db_session
from database.session import Base
from model.models import User
from router import user as user_router
from service import auth_service


ROOT = Path(__file__).resolve().parents[1]
BACKEND = ROOT / "backend"
STRONG_SECRET_KEY = "test-only-strong-secret-key-4f8c2a1d9e7b3c5a6d0f2e8b1a7c4d9e"


def _forge_token(secret_key: str, subject: str = "admin") -> str:
    return jwt.encode(
        {
            "sub": subject,
            "exp": datetime.now(timezone.utc) + timedelta(minutes=60),
        },
        secret_key,
        algorithm=config.ALGORITHM,
    )


def _client_with_admin() -> TestClient:
    engine = create_engine(
        "sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool
    )
    Base.metadata.create_all(bind=engine, tables=[User.__table__])
    db = sessionmaker(bind=engine, autoflush=False, autocommit=False)()
    db.add(
        User(
            username="admin",
            password_hash=auth_service.pwd_context.hash("Admin-Real-Password-2026"),
        )
    )
    db.commit()

    app = FastAPI()
    app.include_router(user_router.router)
    app.dependency_overrides[db_session.get_db] = lambda: db
    return TestClient(app)


def _run_in_subprocess(script: str, **env_overrides):
    env = dict(os.environ)
    env.pop("SECRET_KEY", None)
    env.pop("ALLOW_INSECURE_DEFAULT_SECRET", None)
    # 显式置空：空值等价于「未配置」，同时因为 load_dotenv(override=False) 不会覆盖已存在的键，
    # 可以挡住开发机本地 .env 里的 SECRET_KEY / ALLOW_INSECURE_DEFAULT_SECRET，保证用例可复现。
    env["SECRET_KEY"] = ""
    env["ALLOW_INSECURE_DEFAULT_SECRET"] = ""
    env.update(env_overrides)
    return subprocess.run(
        [sys.executable, "-c", script],
        cwd=str(ROOT),
        env=env,
        capture_output=True,
        text=True,
        timeout=180,
    )


# ---------------------------------------------------------------------------
# key resolution
# ---------------------------------------------------------------------------


def test_resolve_secret_key_uses_explicit_env_value():
    key, source = config.resolve_secret_key(STRONG_SECRET_KEY, False)

    assert (key, source) == (STRONG_SECRET_KEY, config.SECRET_KEY_SOURCE_ENV)


def test_resolve_secret_key_never_returns_the_repo_placeholder():
    key, source = config.resolve_secret_key(config.DEFAULT_SECRET_KEY, False)

    assert key != config.DEFAULT_SECRET_KEY
    assert source == config.SECRET_KEY_SOURCE_UNCONFIGURED
    assert len(key) >= 32


@pytest.mark.parametrize("value", [None, "", "   "])
def test_resolve_secret_key_treats_blank_value_as_missing(value):
    key, source = config.resolve_secret_key(value, False)

    assert key != config.DEFAULT_SECRET_KEY
    assert source == config.SECRET_KEY_SOURCE_UNCONFIGURED


def test_resolve_secret_key_insecure_switch_uses_random_key_not_the_placeholder():
    first, source = config.resolve_secret_key(config.DEFAULT_SECRET_KEY, True)
    second, _ = config.resolve_secret_key(None, True)

    assert source == config.SECRET_KEY_SOURCE_INSECURE_DEV
    assert first != config.DEFAULT_SECRET_KEY
    assert first != second  # 每个进程各自随机，绝不是固定默认值


def test_runtime_secret_key_is_not_the_public_placeholder():
    assert config.SECRET_KEY != config.DEFAULT_SECRET_KEY
    assert config.SECRET_KEY.strip() != ""


# ---------------------------------------------------------------------------
# startup gate
# ---------------------------------------------------------------------------


def test_startup_check_raises_with_actionable_message_when_unconfigured(monkeypatch):
    monkeypatch.setattr(config, "SECRET_KEY_SOURCE", config.SECRET_KEY_SOURCE_UNCONFIGURED)

    with pytest.raises(config.SecretKeyError) as exc_info:
        config.ensure_secret_key_configured()

    message = str(exc_info.value)
    assert "SECRET_KEY" in message
    assert "ALLOW_INSECURE_DEFAULT_SECRET" in message
    assert config.DEFAULT_SECRET_KEY not in message  # 不把占位值当成可用密钥回显


def test_startup_check_raises_when_a_placeholder_key_is_configured(monkeypatch):
    key, source = config.resolve_secret_key(config.DEFAULT_SECRET_KEY, False)
    monkeypatch.setattr(config, "SECRET_KEY", key)
    monkeypatch.setattr(config, "SECRET_KEY_SOURCE", source)

    with pytest.raises(config.SecretKeyError):
        config.ensure_secret_key_configured()


def test_startup_check_passes_for_explicit_secret_key(monkeypatch):
    key, source = config.resolve_secret_key(STRONG_SECRET_KEY, False)
    monkeypatch.setattr(config, "SECRET_KEY", key)
    monkeypatch.setattr(config, "SECRET_KEY_SOURCE", source)

    assert config.ensure_secret_key_configured() is None


def test_startup_check_passes_with_insecure_dev_switch(monkeypatch):
    key, source = config.resolve_secret_key(config.DEFAULT_SECRET_KEY, True)
    monkeypatch.setattr(config, "SECRET_KEY", key)
    monkeypatch.setattr(config, "SECRET_KEY_SOURCE", source)

    assert config.ensure_secret_key_configured() is None


# ---------------------------------------------------------------------------
# forgery attempts
# ---------------------------------------------------------------------------


def test_profile_rejects_token_signed_with_repo_default_secret():
    client = _client_with_admin()

    response = client.get(
        "/api/user/profile",
        headers={"Authorization": "Bearer " + _forge_token(config.DEFAULT_SECRET_KEY)},
    )

    assert response.status_code == 401


def test_profile_rejects_token_signed_with_another_guess():
    client = _client_with_admin()

    response = client.get(
        "/api/user/profile",
        headers={"Authorization": "Bearer " + _forge_token("any-other-guess")},
    )

    assert response.status_code == 401


def test_profile_accepts_token_signed_with_the_active_secret():
    client = _client_with_admin()

    token = auth_service.create_token("admin")
    response = client.get("/api/user/profile", headers={"Authorization": f"Bearer {token}"})

    assert response.status_code == 200
    assert response.json()["username"] == "admin"


def test_decode_token_rejects_token_signed_with_repo_default_secret():
    with pytest.raises(HTTPException) as exc_info:
        auth_service.decode_token("Bearer " + _forge_token(config.DEFAULT_SECRET_KEY))

    assert exc_info.value.status_code == 401


# ---------------------------------------------------------------------------
# end-to-end startup behaviour
# ---------------------------------------------------------------------------


def test_service_startup_exits_without_secret_key():
    script = (
        "import sys\n"
        f"sys.path.insert(0, {str(BACKEND)!r})\n"
        "from fastapi.testclient import TestClient\n"
        "import main\n"
        "with TestClient(main.app):\n"
        "    print('STARTUP_SUCCEEDED')\n"
    )

    result = _run_in_subprocess(script)

    output = result.stdout + result.stderr
    assert result.returncode != 0
    assert "STARTUP_SUCCEEDED" not in result.stdout
    assert "SECRET_KEY" in output
    # 必须死于门禁本身：少了这一条，把 lifespan 里的 ensure_secret_key_configured() 删掉后
    # 用例仍会通过（此时进程是连不上数据库才退出的），门禁被移除的回归就没人拦得住。
    assert "SecretKeyError" in output


def test_service_startup_check_passes_with_explicit_secret_key():
    script = (
        "import sys\n"
        f"sys.path.insert(0, {str(BACKEND)!r})\n"
        "from config import ensure_secret_key_configured\n"
        "ensure_secret_key_configured()\n"
        "print('STARTUP_CHECK_PASSED')\n"
    )

    result = _run_in_subprocess(script, SECRET_KEY=STRONG_SECRET_KEY)

    assert result.returncode == 0, result.stderr
    assert "STARTUP_CHECK_PASSED" in result.stdout


def test_service_startup_allowed_by_insecure_switch_with_random_key():
    script = (
        "import sys\n"
        f"sys.path.insert(0, {str(BACKEND)!r})\n"
        "from config import (\n"
        "    DEFAULT_SECRET_KEY,\n"
        "    SECRET_KEY,\n"
        "    SECRET_KEY_SOURCE_INSECURE_DEV,\n"
        "    SECRET_KEY_SOURCE,\n"
        "    ensure_secret_key_configured,\n"
        ")\n"
        "ensure_secret_key_configured()\n"
        "assert SECRET_KEY != DEFAULT_SECRET_KEY, SECRET_KEY\n"
        "assert SECRET_KEY_SOURCE == SECRET_KEY_SOURCE_INSECURE_DEV, SECRET_KEY_SOURCE\n"
        "assert len(SECRET_KEY) >= 32, len(SECRET_KEY)\n"
        "print('INSECURE_DEV_STARTUP_OK')\n"
    )

    result = _run_in_subprocess(script, ALLOW_INSECURE_DEFAULT_SECRET="true")

    assert result.returncode == 0, result.stderr
    assert "INSECURE_DEV_STARTUP_OK" in result.stdout
