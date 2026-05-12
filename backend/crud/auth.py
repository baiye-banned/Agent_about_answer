from sqlalchemy.orm import Session

from model.models import User


def get_user_by_username(db: Session, username: str) -> User | None:
    return db.query(User).filter_by(username=username).first()

