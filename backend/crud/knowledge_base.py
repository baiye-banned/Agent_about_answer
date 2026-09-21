from sqlalchemy import func
from sqlalchemy.orm import Session

from model.models import Conversation, KnowledgeBase, KnowledgeFile


def serialize_knowledge_base(item: KnowledgeBase, file_count: int) -> dict:
    """序列化知识库；file_count 由调用方以聚合查询给出。

    这里不再用 len(item.files)：关系属性会把该库下所有 KnowledgeFile 行整行实例化，
    其中 content 是 LONGTEXT，列表接口为了一个计数字段把全部文件正文搬进内存。
    """
    return {
        "id": item.id,
        "name": item.name,
        "file_count": file_count,
        "created_at": item.created_at.isoformat() if item.created_at else "",
        "updated_at": item.updated_at.isoformat() if item.updated_at else "",
    }


def count_knowledge_files(db: Session, kid: int) -> int:
    """单个知识库的文件数：只查 COUNT，不取文件行。"""
    return db.query(func.count(KnowledgeFile.id)).filter(KnowledgeFile.knowledge_base_id == kid).scalar() or 0


def count_knowledge_files_by_base(db: Session, kids: list[int]) -> dict[int, int]:
    """批量文件数：一条 GROUP BY 聚合替代「每个知识库查一次」。

    返回 {knowledge_base_id: 文件数}，没有文件的知识库不出现在结果里，调用方按 0 兜底。
    计数不叠加 user_id 过滤，与 len(item.files) 的旧语义一致（含回填前的历史行）。
    """
    if not kids:
        return {}
    rows = (
        db.query(KnowledgeFile.knowledge_base_id, func.count(KnowledgeFile.id))
        .filter(KnowledgeFile.knowledge_base_id.in_(kids))
        .group_by(KnowledgeFile.knowledge_base_id)
        .all()
    )
    return {base_id: count for base_id, count in rows}


def get_default_knowledge_base(db: Session, user_id: int) -> KnowledgeBase | None:
    return (
        db.query(KnowledgeBase)
        .filter_by(user_id=user_id)
        .order_by(KnowledgeBase.id.asc())
        .first()
    )


def resolve_knowledge_base(db: Session, knowledge_base_id: int | None, user_id: int) -> KnowledgeBase | None:
    if knowledge_base_id:
        return get_knowledge_base(db, knowledge_base_id, user_id)
    return get_default_knowledge_base(db, user_id)


def list_knowledge_bases(db: Session, user_id: int) -> list[KnowledgeBase]:
    return (
        db.query(KnowledgeBase)
        .filter_by(user_id=user_id)
        .order_by(KnowledgeBase.created_at.asc())
        .all()
    )


def get_knowledge_base(db: Session, kid: int, user_id: int) -> KnowledgeBase | None:
    return db.query(KnowledgeBase).filter_by(id=kid, user_id=user_id).first()


def count_knowledge_bases(db: Session, user_id: int) -> int:
    return db.query(KnowledgeBase).filter_by(user_id=user_id).count()


def get_fallback_knowledge_base(db: Session, deleted_id: int, user_id: int) -> KnowledgeBase | None:
    return (
        db.query(KnowledgeBase)
        .filter(KnowledgeBase.id != deleted_id, KnowledgeBase.user_id == user_id)
        .order_by(KnowledgeBase.id.asc())
        .first()
    )


def list_files_for_knowledge_base(db: Session, kid: int) -> list[KnowledgeFile]:
    """知识库下的全部文件。

    按知识库 id 取，不再叠加 user_id 过滤：文件既可能带 user_id，也可能是回填前的历史行。
    调用方必须先用 get_knowledge_base(db, kid, user_id) 校验归属；删除知识库时需要清空
    其下所有文件，否则外键会悬挂。
    """
    return db.query(KnowledgeFile).filter_by(knowledge_base_id=kid).all()


def knowledge_base_name_exists(db: Session, name: str, user_id: int, exclude_id: int | None = None) -> bool:
    query = db.query(KnowledgeBase).filter_by(name=name, user_id=user_id)
    if exclude_id is not None:
        query = query.filter(KnowledgeBase.id != exclude_id)
    return db.query(query.exists()).scalar()


def create_knowledge_base(db: Session, name: str, user_id: int) -> KnowledgeBase:
    entry = KnowledgeBase(name=name, user_id=user_id)
    db.add(entry)
    db.commit()
    db.refresh(entry)
    return entry


def rename_knowledge_base(db: Session, kid: int, name: str, user_id: int) -> KnowledgeBase | None:
    entry = get_knowledge_base(db, kid, user_id)
    if not entry:
        return None
    entry.name = name
    db.commit()
    db.refresh(entry)
    return entry


def delete_knowledge_base(db: Session, kid: int, user_id: int) -> tuple[KnowledgeBase | None, KnowledgeBase | None]:
    entry = get_knowledge_base(db, kid, user_id)
    if not entry:
        return None, None
    target = get_fallback_knowledge_base(db, kid, user_id)
    if not target:
        return entry, None
    delete_knowledge_base_with_files(db, kid, target.id, user_id)
    return entry, target


def delete_knowledge_base_with_files(
    db: Session, kid: int, fallback_id: int, user_id: int
) -> KnowledgeBase | None:
    """删除归属 user_id 的知识库及其文件。

    会话绑定按归属分别处理：本人的会话改绑到其兜底知识库，其他用户的悬挂引用置空，
    避免删除操作改动他人会话数据，也避免外键阻塞删除。
    """
    entry = get_knowledge_base(db, kid, user_id)
    if not entry:
        return None
    owner_id = entry.user_id
    conversations = db.query(Conversation).filter_by(knowledge_base_id=kid).all()
    for file_entry in list_files_for_knowledge_base(db, kid):
        db.delete(file_entry)
    db.delete(entry)
    # 必须先 flush 掉删除：SQLAlchemy 删除知识库时会把已加载会话的 knowledge_base_id 置空，
    # 若在删除前改绑，赋值会被这次置空覆盖（生产 SessionLocal 的 autoflush=False 下尤为明显）。
    db.flush()
    for conversation in conversations:
        conversation.knowledge_base_id = fallback_id if conversation.user_id == owner_id else None
    db.commit()
    return entry
