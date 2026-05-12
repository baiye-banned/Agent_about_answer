from sqlalchemy.orm import Session

from model.models import Conversation, KnowledgeBase, KnowledgeFile


def serialize_knowledge_base(item: KnowledgeBase) -> dict:
    return {
        "id": item.id,
        "name": item.name,
        "file_count": len(item.files),
        "created_at": item.created_at.isoformat() if item.created_at else "",
        "updated_at": item.updated_at.isoformat() if item.updated_at else "",
    }


def get_default_knowledge_base(db: Session) -> KnowledgeBase | None:
    return db.query(KnowledgeBase).order_by(KnowledgeBase.id.asc()).first()


def resolve_knowledge_base(db: Session, knowledge_base_id: int | None) -> KnowledgeBase | None:
    if knowledge_base_id:
        return db.query(KnowledgeBase).filter_by(id=knowledge_base_id).first()
    return get_default_knowledge_base(db)


def list_knowledge_bases(db: Session) -> list[KnowledgeBase]:
    return db.query(KnowledgeBase).order_by(KnowledgeBase.created_at.asc()).all()


def get_knowledge_base(db: Session, kid: int) -> KnowledgeBase | None:
    return db.query(KnowledgeBase).filter_by(id=kid).first()


def count_knowledge_bases(db: Session) -> int:
    return db.query(KnowledgeBase).count()


def get_fallback_knowledge_base(db: Session, deleted_id: int) -> KnowledgeBase | None:
    return (
        db.query(KnowledgeBase)
        .filter(KnowledgeBase.id != deleted_id)
        .order_by(KnowledgeBase.id.asc())
        .first()
    )


def list_files_for_knowledge_base(db: Session, kid: int) -> list[KnowledgeFile]:
    return db.query(KnowledgeFile).filter_by(knowledge_base_id=kid).all()


def knowledge_base_name_exists(db: Session, name: str, exclude_id: int | None = None) -> bool:
    query = db.query(KnowledgeBase).filter_by(name=name)
    if exclude_id is not None:
        query = query.filter(KnowledgeBase.id != exclude_id)
    return db.query(query.exists()).scalar()


def create_knowledge_base(db: Session, name: str) -> KnowledgeBase:
    entry = KnowledgeBase(name=name)
    db.add(entry)
    db.commit()
    db.refresh(entry)
    return entry


def rename_knowledge_base(db: Session, kid: int, name: str) -> KnowledgeBase | None:
    entry = get_knowledge_base(db, kid)
    if not entry:
        return None
    entry.name = name
    db.commit()
    db.refresh(entry)
    return entry


def delete_knowledge_base(db: Session, kid: int) -> tuple[KnowledgeBase | None, KnowledgeBase | None]:
    entry = get_knowledge_base(db, kid)
    if not entry:
        return None, None
    target = get_fallback_knowledge_base(db, kid)
    if not target:
        return entry, None
    delete_knowledge_base_with_files(db, kid, target.id)
    return entry, target


def delete_knowledge_base_with_files(db: Session, kid: int, fallback_id: int) -> KnowledgeBase | None:
    entry = get_knowledge_base(db, kid)
    if not entry:
        return None
    for conversation in db.query(Conversation).filter_by(knowledge_base_id=kid).all():
        conversation.knowledge_base_id = fallback_id
    for file_entry in list_files_for_knowledge_base(db, kid):
        db.delete(file_entry)
    db.delete(entry)
    db.commit()
    return entry
