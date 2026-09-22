from datetime import datetime

from sqlalchemy import and_, literal, or_
from sqlalchemy.orm import Session, selectinload

from crud import trace as crud_trace
from crud.pagination import LIST_DEFAULT_LIMIT, clamp_limit, datetime_cursor_value, seconds_text
from model.models import ChatAttachmentUpload, ChatTraceSession, Conversation, Message
from service.json_utils import load_json_value


# 消息历史默认页大小与单页上限：不传分页参数时也必须有上限，否则会话越长单次响应越大。
CHAT_MESSAGE_DEFAULT_LIMIT = 50
CHAT_MESSAGE_MAX_LIMIT = 200


def serialize_message(message: Message) -> dict:
    retrieval_trace = load_json_value(message.retrieval_trace, {})
    if not isinstance(retrieval_trace, dict):
        retrieval_trace = {}

    return {
        "id": message.id,
        "role": message.role,
        "content": message.content,
        "sources": load_json_value(message.sources, []),
        "attachments": load_json_value(message.attachments, []),
        "ragas_status": message.ragas_status or "",
        "ragas_scores": load_json_value(message.ragas_scores, {}),
        "ragas_error": message.ragas_error or "",
        "retrieval_trace": retrieval_trace,
        "image_analysis_status": retrieval_trace.get("image_analysis_status", ""),
        "image_analysis_error": retrieval_trace.get("image_analysis_error", ""),
        "image_description": retrieval_trace.get("image_description", ""),
        "created_at": message.created_at.isoformat() if message.created_at else "",
    }


def list_conversations(
    db: Session,
    user_id: int,
    *,
    limit: int = LIST_DEFAULT_LIMIT,
    before: tuple[datetime, str] | None = None,
) -> list[Conversation]:
    """按 user_id 取一页会话，按「最近活动」倒序，与旧接口的数组顺序一致。

    翻页语义（issue #191）：**键集游标**，不用 offset——offset 在翻页期间有新会话插入时
    会整体位移，同一行会被翻到两次、另有一行永远翻不到。游标落在排序键本身上，
    取「排在这条记录之后」的那一页，插入多少新行都不影响已经翻过的区间。

    会话的排序键是 `(updated_at, 自增主键)` 的**复合键**，两个理由：
    1) 会话主键是 uuid（`model/models.py` 的 `_new_id`），不是自增整数，按它排序等于乱序，
       既不能当序键也不能当游标——这是会话列表与知识库/文件列表（都是自增整数主键）
       在游标形态上不一样的原因；
    2) `updated_at` 是秒级 DATETIME（`func.now()` 在 MySQL 上只到秒），同一秒内改动的多个
       会话按它排序不稳定，翻页会重复或漏行。补一个唯一且不重复的主键做末位键，
       `(updated_at, id)` 就是全序，游标比较才有确定答案。

    已知边界：`updated_at` 是可变的（新消息、重命名都会改写它）。翻页途中被改写的那个会话
    会跳到游标之前（用户已经翻过的区间），它要么已经在上一次响应里、要么要等下一次整表刷新
    才出现——侧栏每次回答结束都会整表刷新，收敛得掉。反过来把序键换成不可变的 `created_at`
    能彻底消掉这个边界，但侧栏会从「最近活动优先」变成「创建时间优先」，
    属于本单之外的可见行为变更，不做。

    `before` 是 (updated_at, id) 复合游标，两者必须同时给出（service 层负责拦半个游标）。
    """
    limit = clamp_limit(limit)
    # 排序与游标过滤共用同一个秒级表达式：两处必须同一口径，否则游标落不到排序位置上
    # （列里存的文本形状不唯一，见 seconds_text）。排序用整列时，带小数的行会排在
    # 秒级行之间，而游标只按秒级比较，两者对不齐。
    timestamp_key = seconds_text(Conversation.updated_at)
    query = (
        db.query(Conversation)
        # 预加载知识库：序列化时每个会话都要读 conv.knowledge_base，逐行懒加载会让查询数随会话数增长。
        .options(selectinload(Conversation.knowledge_base))
        .filter_by(user_id=user_id)
    )
    if before is not None:
        before_updated_at, before_id = before
        # 等价于 (updated_at, id) < (before_updated_at, before_id) 的行值比较，写成展开式是为了
        # SQLite 与 MySQL 共用同一段 SQL（两者都支持行值比较，但展开式在两边都不依赖方言支持）。
        # 两档都要归一后再比：时间戳截到秒级文本（理由见 seconds_text），主键本来就是文本。
        cursor_value = literal(datetime_cursor_value(before_updated_at))
        query = query.filter(or_(
            timestamp_key < cursor_value,
            and_(timestamp_key == cursor_value, Conversation.id < before_id),
        ))
    return (
        query.order_by(timestamp_key.desc(), Conversation.id.desc())
        .limit(limit)
        .all()
    )


def get_conversation(db: Session, cid: str, user_id: int) -> Conversation | None:
    return db.query(Conversation).filter_by(id=cid, user_id=user_id).first()


def list_messages(
    db: Session,
    cid: str,
    user_id: int,
    *,
    limit: int = CHAT_MESSAGE_DEFAULT_LIMIT,
    before_id: int | None = None,
) -> list[Message] | None:
    """按会话取一页消息，返回「旧 -> 新」顺序，与旧接口的数组顺序一致。

    游标用自增主键而不是 created_at：created_at 是秒级 DATETIME，同一秒内的消息按它排序
    不稳定，翻页会重复或漏行。先倒序取下 limit 条再反转，取到的就是 cursor 之前最新的那页。

    页大小在这里兜底夹取（而不是只依赖服务层的 422 校验）：CRUD 是更底层的入口，脚本或内部
    调用可能绕开接口层，负值在 SQLite 上等价于「不设上限」，会把整段历史一次读出来。
    """
    conversation = get_conversation(db, cid, user_id)
    if not conversation:
        return None
    limit = CHAT_MESSAGE_DEFAULT_LIMIT if limit is None else max(1, min(limit, CHAT_MESSAGE_MAX_LIMIT))
    query = db.query(Message).filter(Message.conversation_id == cid)
    if before_id is not None:
        query = query.filter(Message.id < before_id)
    rows = query.order_by(Message.id.desc()).limit(limit).all()
    rows.reverse()
    return rows


def list_conversation_attachment_keys(db: Session, cid: str, user_id: int) -> list[str] | None:
    """会话内所有消息引用到的 OSS 对象键，去重后按「旧 -> 新」返回。

    必须在会话行被删除之前调用：`messages` 随会话级联删除，附件列里的对象键会一起消失，
    删完之后就再也说不清这个会话扔下过哪些对象。只看 attachments 一列，不把正文
    （LONGTEXT）读进内存；也不走 list_messages——那个有分页上限，翻不到的消息会漏收。

    会话不存在或不属于该用户时返回 None，与 list_messages 的约定一致。
    """
    if not get_conversation(db, cid, user_id):
        return None

    rows = (
        db.query(Message.attachments)
        .filter(Message.conversation_id == cid)
        .order_by(Message.id.asc())
        .all()
    )
    keys: list[str] = []
    seen: set[str] = set()
    for (raw_attachments,) in rows:
        for item in load_json_value(raw_attachments, []):
            # 历史脏数据与上传中断留下的记录都可能不是 dict、或缺 object_key。
            if not isinstance(item, dict):
                continue
            object_key = item.get("object_key")
            if not isinstance(object_key, str) or not object_key or object_key in seen:
                continue
            seen.add(object_key)
            keys.append(object_key)
    return keys


def register_attachment_upload(db: Session, object_key: str, user_id: int) -> ChatAttachmentUpload:
    """登记一次上传铸出的对象键，并立刻提交。

    提交必须发生在**写对象之前**（调用方负责顺序）：反过来时，一次「对象已经进桶、进程
    随即退出」就留下一个库里没有任何痕迹的对象——正是 issue #142 的形状。先落库之后，
    写对象失败最坏只是一条指向不存在对象的登记行，清扫任务顺手抹掉即可。

    对象键是主键，重复登记会撞唯一约束；上传接口每次铸的都是新的 uuid4().hex，正常路径
    不会重复。
    """
    upload = ChatAttachmentUpload(object_key=object_key, user_id=user_id)
    db.add(upload)
    db.commit()
    return upload


def confirm_attachment_uploads(db: Session, object_keys: list[str], user_id: int) -> int:
    """消费已经转为正式引用的登记行，返回消费掉的条数。**故意不提交**。

    调用方必须把它和消息行放进同一次 commit：分开提交时，「消息已落库、登记行还在」的
    中间态会让清扫任务把一条活消息引用的对象当成孤儿删掉；反过来则是消息没落库却把登记行
    消费掉，对象从此脱离所有回收路径。

    只消费 user_id 自己的行：附件列由客户端回带，一条消息可以引用一把别人铸的键（形态校验
    只认键的形状，不认归属）。若发送方也能替别人消费，那把键就被这条消息永久钉住，
    原主「上传了没发送」的对象再也回不到清扫任务手里。
    """
    keys = [key for key in dict.fromkeys(object_keys or []) if isinstance(key, str) and key]
    if not keys:
        return 0
    return (
        db.query(ChatAttachmentUpload)
        .filter(
            ChatAttachmentUpload.user_id == user_id,
            ChatAttachmentUpload.object_key.in_(keys),
        )
        .delete(synchronize_session=False)
    )


def list_pending_attachment_uploads(
    db: Session,
    *,
    older_than=None,
    user_id: int | None = None,
    limit: int | None = None,
) -> list[ChatAttachmentUpload]:
    """「桶里有、库里没有」的对账入口：本服务铸过、还没有被任何消息消费的对象键。

    这是 #142 要补的那块——上传即登记之后，行还在这张表里就等于「服务端写过一个对象，
    但没有任何消息引用它」。清扫任务用 older_than 取超期的那批，排查/导出可以不加时间
    条件看全量，也可以按 user_id 收窄到单个账号。

    按 created_at 升序（同刻按 object_key 兜底定序）：一批超过 limit 时，先被回收的
    是积压更久的那批，且顺序稳定可复现。
    """
    query = db.query(ChatAttachmentUpload)
    if older_than is not None:
        query = query.filter(ChatAttachmentUpload.created_at < older_than)
    if user_id is not None:
        query = query.filter(ChatAttachmentUpload.user_id == user_id)
    query = query.order_by(
        ChatAttachmentUpload.created_at.asc(),
        ChatAttachmentUpload.object_key.asc(),
    )
    if limit is not None:
        query = query.limit(max(1, int(limit)))
    return query.all()


def claim_attachment_upload(db: Session, object_key: str, older_than) -> dict | None:
    """原子地把一条「已超期且仍未被消费」的登记行领走；领不到返回 None。

    领的动作是**条件删除 + 提交**：`WHERE object_key = ? AND created_at < ?`。并发的发送
    （消费）与另一条清扫（领）竞争的是同一行，数据库的行级原子性保证只有一个能拿到
    rowcount=1。领不到的一方据此知道「这个对象已经归别人管了」，于是**不去碰对象本身**。

    这是「清扫取到候选之后、动手之前，用户正好把这条附件发送成功」那一臂的解药：清扫不再
    按自己先前那份快照动手，而是每一步都要先赢下这一行；赢不下来就说明这条键已经成了正式
    引用（或已被另一条清扫处理），对象必须留着。对象存储没有回收站，删错没有第二遍。

    返回的行数据交给调用方在删除对象失败时**放回队列**（restore_attachment_upload），所以
    带上 user_id 与 created_at。
    """
    row = (
        db.query(ChatAttachmentUpload)
        .filter(
            ChatAttachmentUpload.object_key == object_key,
            ChatAttachmentUpload.created_at < older_than,
        )
        .first()
    )
    if row is None:
        return None
    claimed = {"object_key": row.object_key, "user_id": row.user_id, "created_at": row.created_at}
    deleted = (
        db.query(ChatAttachmentUpload)
        .filter(
            ChatAttachmentUpload.object_key == object_key,
            ChatAttachmentUpload.created_at < older_than,
        )
        .delete(synchronize_session=False)
    )
    db.commit()
    return claimed if deleted else None


def restore_attachment_upload(db: Session, *, object_key: str, user_id: int, created_at) -> None:
    """把「领走了但对象没删成」的登记行按原时间戳放回队列。

    原时间戳（而不是 now）是刻意的：这条行还没被回收，回合后仍按上传时刻排队，下一轮清扫
    会立刻再试它，不会因为失败过一次就被排到所有新孤儿后面去。
    """
    db.add(ChatAttachmentUpload(object_key=object_key, user_id=user_id, created_at=created_at))
    db.commit()


def drop_attachment_upload(db: Session, object_key: str) -> bool:
    """丢弃一条登记行并提交（清扫用它处理「永远签不出 DELETE」的行）。幂等：重跑返回 False。"""
    deleted = (
        db.query(ChatAttachmentUpload)
        .filter(ChatAttachmentUpload.object_key == object_key)
        .delete(synchronize_session=False)
    )
    db.commit()
    return bool(deleted)


def delete_conversation(db: Session, cid: str, user_id: int) -> Conversation | None:
    conversation = get_conversation(db, cid, user_id)
    if not conversation:
        return None
    # 学习轨迹按 conversation_id 关联，列上没有外键约束（model/models.py），数据库不会级联，
    # 必须显式清理。与会话删除共用同一次提交：分两次提交时中间失败就会留下
    # 「会话已删、轨迹仍能读」的残留（issue #127）。不按 LEARNING_TRACE_ENABLED 分流——
    # 关掉开关只是不再写新轨迹，已经写下的行仍然要随会话删除。
    crud_trace.delete_trace_sessions_for_conversation(db, cid)
    db.delete(conversation)
    db.commit()
    return conversation


def rename_conversation(db: Session, cid: str, user_id: int, title: str) -> Conversation | None:
    conversation = get_conversation(db, cid, user_id)
    if not conversation:
        return None
    conversation.title = title
    db.commit()
    db.refresh(conversation)
    return conversation


def get_message_by_user(db: Session, message_id: int, user_id: int) -> Message | None:
    return (
        db.query(Message)
        .join(Conversation, Message.conversation_id == Conversation.id)
        .filter(Message.id == message_id, Conversation.user_id == user_id)
        .first()
    )


def get_trace_session_by_message(db: Session, message_id: int, user_id: int) -> ChatTraceSession | None:
    return db.query(ChatTraceSession).filter_by(message_id=message_id, user_id=user_id).first()
