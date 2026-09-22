from sqlalchemy import func
from sqlalchemy.orm import Session, load_only

from crud.pagination import LIST_DEFAULT_LIMIT, clamp_limit
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


def list_knowledge_bases(
    db: Session,
    user_id: int,
    *,
    limit: int = LIST_DEFAULT_LIMIT,
    after_id: int | None = None,
) -> list[KnowledgeBase]:
    """按 user_id 取一页知识库，返回「旧 -> 新」顺序（与旧接口的 created_at 升序一致）。

    翻页语义（issue #191）：**键集游标**，不用 offset——offset 在翻页期间新建知识库时
    会整体位移，同一行被翻到两次、另一行永远翻不到。

    序键从 `created_at` 换成自增主键 `id`，可见顺序不变（自增主键与插入顺序严格一致，
    后建的库 created_at 不会更早），换来的是可翻页：`created_at` 是秒级 DATETIME，
    同一秒建出来的多个库按它排序不稳定，做游标会重复或漏行。游标取 `id > after_id`，
    即「接着上一页往新的一页翻」，翻页途中新建的库排在游标之后，已经翻过的区间不位移。
    """
    limit = clamp_limit(limit)
    query = db.query(KnowledgeBase).filter_by(user_id=user_id)
    if after_id is not None:
        query = query.filter(KnowledgeBase.id > after_id)
    return (
        query.order_by(KnowledgeBase.id.asc())
        .limit(limit)
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
    """知识库下的全部文件（只取列表元数据，不含正文）。

    按知识库 id 取，不再叠加 user_id 过滤：文件既可能带 user_id，也可能是回填前的历史行。
    调用方必须先用 get_knowledge_base(db, kid, user_id) 校验归属；删除知识库时需要清空
    其下所有文件，否则外键会悬挂。

    列集与 list_knowledge_files 一致（id/knowledge_base_id/name/size/created_at），
    显式排除 content（LONGTEXT）：调用方只要 id 或删行，没有一处需要正文，整行取会把
    每个文件的正文搬进内存。本函数的返回值禁止用于正文读取——访问 content 会让
    SQLAlchemy 按行补查，等于把这里省下的正文又读回来。
    """
    return (
        db.query(KnowledgeFile)
        .options(load_only(
            KnowledgeFile.id,
            KnowledgeFile.knowledge_base_id,
            KnowledgeFile.name,
            KnowledgeFile.size,
            KnowledgeFile.created_at,
        ))
        .filter_by(knowledge_base_id=kid)
        .all()
    )


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
