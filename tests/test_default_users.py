from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from database.session import Base
from model.models import User
from service.auth_service import pwd_context
import service.user_service as user_service


def _bind_temp_session(monkeypatch):
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(bind=engine, tables=[User.__table__])
    session_factory = sessionmaker(autocommit=False, autoflush=False, bind=engine)
    monkeypatch.setattr(user_service, "SessionLocal", session_factory)
    return session_factory


def test_seed_default_users_creates_missing_defaults(monkeypatch):
    session_factory = _bind_temp_session(monkeypatch)

    user_service.seed_default_users()

    db = session_factory()
    try:
        users = {user.username: user for user in db.query(User).all()}
        assert sorted(users) == ["admin", "demo"]
        assert pwd_context.verify("admin123", users["admin"].password_hash)
        assert pwd_context.verify("demo123", users["demo"].password_hash)
    finally:
        db.close()


def test_seed_default_users_does_not_reset_existing_user(monkeypatch):
    session_factory = _bind_temp_session(monkeypatch)
    custom_demo_hash = pwd_context.hash("custom-demo-password")

    db = session_factory()
    try:
        db.add(User(username="demo", password_hash=custom_demo_hash))
        db.commit()
    finally:
        db.close()

    user_service.seed_default_users()

    db = session_factory()
    try:
        users = {user.username: user for user in db.query(User).all()}
        assert sorted(users) == ["admin", "demo"]
        assert pwd_context.verify("admin123", users["admin"].password_hash)
        assert users["demo"].password_hash == custom_demo_hash
        assert pwd_context.verify("custom-demo-password", users["demo"].password_hash)
    finally:
        db.close()
