import logging
import secrets
from pathlib import Path

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from database.session import Base
from model.models import User
from service.auth_service import pwd_context
import service.user_service as user_service


LEGACY_PASSWORDS = ("admin123", "demo123")
BACKEND_DIR = Path(__file__).resolve().parents[1] / "backend"


def _bind_temp_session(monkeypatch):
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(bind=engine, tables=[User.__table__])
    session_factory = sessionmaker(autocommit=False, autoflush=False, bind=engine)
    monkeypatch.setattr(user_service, "SessionLocal", session_factory)
    return session_factory


def _seed_env(monkeypatch, seeding_enabled: bool = True):
    monkeypatch.setattr(user_service, "SEED_DEFAULT_USERS", seeding_enabled)
    monkeypatch.delenv("SEED_ADMIN_PASSWORD", raising=False)
    monkeypatch.delenv("SEED_DEMO_PASSWORD", raising=False)


def _users(session_factory):
    db = session_factory()
    try:
        return {user.username: user for user in db.query(User).all()}
    finally:
        db.close()


def test_seed_is_skipped_when_disabled(monkeypatch):
    session_factory = _bind_temp_session(monkeypatch)
    _seed_env(monkeypatch, seeding_enabled=False)
    monkeypatch.setenv("SEED_ADMIN_PASSWORD", "configured-admin-password")
    monkeypatch.setenv("SEED_DEMO_PASSWORD", "configured-demo-password")

    assert user_service.seed_default_users() == []
    assert _users(session_factory) == {}


def test_seed_creates_accounts_with_env_passwords(monkeypatch):
    session_factory = _bind_temp_session(monkeypatch)
    _seed_env(monkeypatch)
    monkeypatch.setenv("SEED_ADMIN_PASSWORD", "configured-admin-password")
    monkeypatch.setenv("SEED_DEMO_PASSWORD", "configured-demo-password")

    assert user_service.seed_default_users() == ["admin", "demo"]

    users = _users(session_factory)
    assert sorted(users) == ["admin", "demo"]
    assert pwd_context.verify("configured-admin-password", users["admin"].password_hash)
    assert pwd_context.verify("configured-demo-password", users["demo"].password_hash)
    for username in ("admin", "demo"):
        for legacy in LEGACY_PASSWORDS:
            assert not pwd_context.verify(legacy, users[username].password_hash)


def test_seed_without_env_passwords_uses_fresh_random_password(monkeypatch):
    session_factory = _bind_temp_session(monkeypatch)
    _seed_env(monkeypatch)

    real_token_urlsafe = secrets.token_urlsafe
    issued = []

    def spying_token_urlsafe(nbytes=32):
        value = real_token_urlsafe(nbytes)
        issued.append(value)
        return value

    monkeypatch.setattr(user_service.secrets, "token_urlsafe", spying_token_urlsafe)

    user_service.seed_default_users()

    users = _users(session_factory)
    assert sorted(users) == ["admin", "demo"]
    # 每个账号都拿到一个独立生成的随机口令，且落库口令与生成值一致
    assert len(issued) == 2
    assert len(set(issued)) == 2
    assert pwd_context.verify(issued[0], users["admin"].password_hash)
    assert pwd_context.verify(issued[1], users["demo"].password_hash)
    # 仓库里的历史固定口令不再可用
    for username in ("admin", "demo"):
        for legacy in LEGACY_PASSWORDS:
            assert not pwd_context.verify(legacy, users[username].password_hash)


def test_seed_does_not_reset_existing_user(monkeypatch):
    session_factory = _bind_temp_session(monkeypatch)
    _seed_env(monkeypatch)
    monkeypatch.setenv("SEED_ADMIN_PASSWORD", "configured-admin-password")
    custom_demo_hash = pwd_context.hash("custom-demo-password")

    db = session_factory()
    try:
        db.add(User(username="demo", password_hash=custom_demo_hash))
        db.commit()
    finally:
        db.close()

    assert user_service.seed_default_users() == ["admin"]

    users = _users(session_factory)
    assert sorted(users) == ["admin", "demo"]
    assert pwd_context.verify("configured-admin-password", users["admin"].password_hash)
    assert users["demo"].password_hash == custom_demo_hash
    assert pwd_context.verify("custom-demo-password", users["demo"].password_hash)


def test_seed_logs_hint_once_without_leaking_generated_password(monkeypatch, caplog):
    session_factory = _bind_temp_session(monkeypatch)
    _seed_env(monkeypatch)
    generated_password = "sentinel-generated-password-value"
    monkeypatch.setattr(user_service.secrets, "token_urlsafe", lambda _nbytes=32: generated_password)

    with caplog.at_level(logging.WARNING, logger="service.user_service"):
        user_service.seed_default_users()
        user_service.seed_default_users()

    messages = [record.getMessage() for record in caplog.records]
    assert len(messages) == 2
    assert any("SEED_ADMIN_PASSWORD" in message for message in messages)
    assert any("SEED_DEMO_PASSWORD" in message for message in messages)
    assert all(generated_password not in message for message in messages)

    users = _users(session_factory)
    assert pwd_context.verify(generated_password, users["admin"].password_hash)


def test_backend_sources_have_no_hardcoded_default_passwords():
    offenders = sorted(
        path.relative_to(BACKEND_DIR).as_posix()
        for path in BACKEND_DIR.rglob("*.py")
        for legacy in LEGACY_PASSWORDS
        if legacy in path.read_text(encoding="utf-8")
    )
    assert offenders == []
