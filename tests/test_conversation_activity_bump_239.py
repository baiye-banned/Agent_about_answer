"""回归（issue #239）：新消息要把会话行 `updated_at` 顶到最新。

修复前：侧栏按「最近活动」倒序（`crud/chat.py` 的 `list_conversations`），但写用户消息的
`_save_user_message` 只 INSERT `messages`，全程没有一条语句落在 `conversations` 行上；而
`Conversation.updated_at` 的推进完全依赖 ORM 的 `onupdate`（全仓 `ON UPDATE CURRENT_TIMESTAMP`
零命中），`onupdate` 只在「这一行确实被 UPDATE」时才触发 ⇒ 发消息不碰会话行，会话一旦不是
最近创建的，就永远沉在侧栏里。修法是在 `_save_user_message` 的同一次提交里 touch 会话行
（`crud_chat.touch_conversation`）。

本文件锁两件事：

1. **新消息 bump**：`_save_user_message` 之后 `updated_at` 前进、侧栏顺序翻转；顺带用
   `messages` 表的行数把「只做了该做的事」钉住（没有顺手多写别的行）。
2. **翻页边界刻画**：bump 会把「本轮还没翻到」的会话顶到游标之前，本轮翻页于是跳过它，
   由下一次刷新回收。这是排序键可变带来的**已知边界，不是缺陷**——用例存在的意义是让
   「行为是已知的」可被 CI 证明；新消息 bump 只是提高了它的触发频率，形状没有变
   （不重复、不静默丢行）。要彻底消掉它得把序键换成不可变的 `created_at`，那是本单之外的
   可见行为变更，不做。

夹具骨架取自 `tests/test_list_page_caps_191.py:48-101`（内存 SQLite + `StaticPool`），其中
`:49-51` 的 `@compiles(LONGTEXT, "sqlite")` 是**硬前置**：`Message.content` /
`retrieval_trace` 是 MySQL 方言的 `LONGTEXT`，缺了这条 shim，`create_all` 会在 SQLite 上
直接 `CompileError: Compiler <SQLiteTypeCompiler> can't render element of type LONGTEXT`。
本套用例直接打 CRUD 与 service 层，不走 HTTP，故不需要 `before_cursor_execute` 录制器，
也不需要覆盖路由依赖。
"""

from datetime import datetime, timedelta
from types import SimpleNamespace

import pytest
from sqlalchemy import create_engine
from sqlalchemy.dialects.mysql import LONGTEXT
from sqlalchemy.ext.compiler import compiles
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from crud import chat as crud_chat
from database.session import Base
from model.models import Conversation, KnowledgeBase, Message, User
from service import chat_service


# 播种用的时间戳必须落在过去：bump 写入的是 `func.now()`，若播种值在未来，now 反而更小，
# 顺序不会翻转、断言会在「修复没生效」时假绿。取一个足够久远的常量，不依赖「今天是哪天」。
T0 = datetime(2020, 1, 1)


@compiles(LONGTEXT, "sqlite")
def _compile_longtext_as_text(_type, _compiler, **_kwargs):
    return "TEXT"


@pytest.fixture()
def api(monkeypatch):
    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    # 只建这几张表：KnowledgeBase 是必须的——`list_conversations` 用 selectinload 预加载
    # `Conversation.knowledge_base`，即便关系为空，SQLAlchemy 也会对 knowledge_bases 发一次
    # SELECT，表不存在直接报错。
    Base.metadata.create_all(
        bind=engine,
        tables=[
            User.__table__,
            KnowledgeBase.__table__,
            Conversation.__table__,
            Message.__table__,
        ],
    )
    db = sessionmaker(bind=engine, autoflush=False, autocommit=False, expire_on_commit=False)()

    alice = User(username="alice", password_hash="x")
    db.add(alice)
    db.commit()

    api = SimpleNamespace(db=db, alice=alice)

    # `_save_user_message` 内部自己 `SessionLocal()` 开 session；把它接到同一个内存库上。
    # StaticPool 保证两次连接是同一条，写与读看得到同一份数据。
    # 必须是 `lambda: api.db`（返回 session 的**可调用对象**）：直接写 `api.db` 会变成
    # 「把 Session 实例当函数调用」，`_save_user_message` 里那句 `SessionLocal()` 抛
    # TypeError: 'Session' object is not callable。
    monkeypatch.setattr(chat_service, "SessionLocal", lambda: api.db)

    try:
        yield api
    finally:
        db.close()


def _add_conversation(api, cid, updated_at):
    """按显式时间戳播种一个会话（沿用 `tests/test_list_page_caps_191.py:127` 的形状）。"""
    conversation = Conversation(id=cid, user_id=api.alice.id, title=f"会话-{cid}")
    conversation.updated_at = updated_at
    conversation.created_at = updated_at
    api.db.add(conversation)
    api.db.commit()
    return conversation


def _reload(api):
    """丢掉身份映射后重新查库，确保读到的是落库值而不是会话里那份对象。"""
    api.db.expunge_all()
    return api.db


def _stored_updated_at(api, cid):
    return _reload(api).query(Conversation).filter_by(id=cid).one().updated_at


def _conversation_ids(api, **kwargs):
    return [c.id for c in crud_chat.list_conversations(api.db, api.alice.id, **kwargs)]


# --- 一：新消息 bump ---------------------------------------------------------------


def test_new_message_bumps_conversation_updated_at(api):
    """发一条消息 ⇒ 该会话的 updated_at 前进、侧栏顺序翻转，且只多写了一条消息。"""
    _add_conversation(api, "conv-old", T0)
    _add_conversation(api, "conv-new", T0 + timedelta(seconds=1))

    # 前置：最近活动优先，conv-new 在前。
    assert _conversation_ids(api, limit=50) == ["conv-new", "conv-old"]

    before = _stored_updated_at(api, "conv-old")
    assert before == T0, f"播种值应原样落库，实得 {before!r}"

    message_id = chat_service._save_user_message("conv-old", api.alice.id, "hello", [])

    after = _stored_updated_at(api, "conv-old")
    # 断言 A：bump 了。不与某个具体时刻比较，只要求「前进」且「越过原最新值」——
    # 后者才是「顶到最前面」的充分条件，只断言不等式的话，bump 到两秒之间的值也能蒙混过关。
    assert after != before, "新消息之后 updated_at 没动，会话不会被顶到「最近活动」最前面"
    assert after > T0 + timedelta(seconds=1), f"bump 后的值没有越过原最新值：{after!r}"

    # 断言 B：侧栏读口的顺序翻转（真的走 `list_conversations`，不是另写一条排序 SQL）。
    assert _conversation_ids(api, limit=50) == ["conv-old", "conv-new"]

    # 断言 C：只写了这一条 user 消息，没有顺手多写别的行。
    rows = _reload(api).query(Message).all()
    assert len(rows) == 1, f"messages 行数应为 1，实得 {len(rows)}"
    assert rows[0].id == message_id
    assert rows[0].role == "user"
    assert rows[0].conversation_id == "conv-old"


# --- 二：翻页边界刻画 ---------------------------------------------------------------


def test_paging_skips_a_conversation_bumped_behind_the_cursor(api):
    """翻页途中被 bump 的会话跳出本轮翻页，由下一次刷新回收（已知边界，非缺陷）。

    步骤：三会话 c1/c2/c3（时间戳递增）→ 取第一页 `limit=2` ⇒ `[c3, c2]`，游标落在 c2 上
    → 给尚未翻到的 c1 发消息（它的键跳到游标之前）→ 同一游标取下一页：c1 不再出现
    → 侧栏下一次刷新（重取最新一页）：c1 排在最前，被回收。
    """
    _add_conversation(api, "c1", T0)
    _add_conversation(api, "c2", T0 + timedelta(seconds=1))
    _add_conversation(api, "c3", T0 + timedelta(seconds=2))

    first_page = crud_chat.list_conversations(api.db, api.alice.id, limit=2)
    assert [c.id for c in first_page] == ["c3", "c2"], "前置：第一页应是最新的两条"

    cursor = (_stored_updated_at(api, "c2"), "c2")

    # c1 尚未被翻到，此刻它被 bump 到游标之前。
    chat_service._save_user_message("c1", api.alice.id, "hello", [])

    next_page = crud_chat.list_conversations(api.db, api.alice.id, limit=2, before=cursor)
    assert "c1" not in [c.id for c in next_page], (
        "被顶走的会话已经跳到游标之前，本轮翻页不该再返回它"
    )
    # 同一个游标上，还剩在这条键之后的都已经翻过了，本轮到此为止——不重复，也不静默丢行
    # （丢的那一行在下一次整表刷新里回来）。
    assert [c.id for c in next_page] == []

    # 侧栏下一次刷新：重取最新一页并重置游标（`src/stores/chat.js` 的 fetchConversations）。
    refreshed = _conversation_ids(api, limit=50)
    assert refreshed[0] == "c1", f"下一次刷新没有回收被跳过的会话：{refreshed}"
