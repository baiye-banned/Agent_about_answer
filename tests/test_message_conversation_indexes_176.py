"""issue #176：三列热查询缺索引（`messages.conversation_id`、
`conversations.user_id`、`conversations.knowledge_base_id`）。

这三列既没有 `index=True`，也不在启动期补建名单里——`_ensure_single_column_index`
全仓只被调用过一次，补的是兄弟列 `knowledge_files.knowledge_base_id`（issue #83 第 5 项）。
缺索引的后果由两处读路径承担：`list_messages` 只能沿主键倒序往回走，走多少行由「这一页
要往回多远」决定而不是由本会话有多少条消息决定；`list_conversations` 除整表扫描外还要
再付一次临时 B 树排序。

与 `tests/test_knowledge_files_index.py` 同范式：模型声明断言 + 执行计划并排（有索引 /
摘掉索引）+ 迁移侧补建幂等。区别在于这里的计划取的是 ORM **真正发出**的那条语句——
用 `before_cursor_execute` 捕获语句与绑定参数后原样重放，而不是另写一条形状相同的查询，
这样改动过的 ORM 查询一旦换了形状，这里的断言会跟着失效而不是继续自说自话。

迁移侧覆盖的是 `_ensure_schema_columns()` 这个入口本身（而不是只测 `_ensure_single_column_index`）：
模型上的 `index=True` 只对 `create_all` 新建的表生效，早先建好的库要靠启动期补建，
断言必须落在真正会在启动时被调用的那个函数上。
"""

import logging

import pytest
from sqlalchemy import create_engine, event, inspect, text
from sqlalchemy.dialects.mysql import LONGTEXT
from sqlalchemy.ext.compiler import compiles
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from crud.chat import list_conversations, list_messages
from database import session as db_session
from database.session import Base
from model.models import Conversation, Message, User

MESSAGE_INDEX = "ix_messages_conversation_id"
CONVERSATION_USER_INDEX = "ix_conversations_user_id"
CONVERSATION_KB_INDEX = "ix_conversations_knowledge_base_id"

# 一次探针会话：400 个会话、4000 条消息，每个会话 10 条。行数足够多，SQLite 才会
# 在没有索引时选择整表扫描（也就让「摘掉索引」那一臂真的能显示退化形状）。
CONVERSATIONS = 400
MESSAGES = 4000
TARGET_CID = "conv-0007"


@compiles(LONGTEXT, "sqlite")
def _compile_longtext_as_text(_type, _compiler, **_kwargs):
    """messages.content / retrieval_trace 是 MySQL 的 LONGTEXT，SQLite 建表时降级成 TEXT。"""
    return "TEXT"


@pytest.fixture()
def sqlite_engine():
    engine = create_engine(
        "sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool
    )
    Base.metadata.create_all(bind=engine)
    yield engine
    engine.dispose()


def _seed(session):
    session.add(User(id=1, username="索引探针", password_hash="x"))
    session.add_all(
        [Conversation(id=f"conv-{index:04d}", user_id=1, title=f"会话{index}") for index in range(CONVERSATIONS)]
    )
    session.add_all(
        [
            Message(
                id=index,
                conversation_id=f"conv-{(index % CONVERSATIONS):04d}",
                role="user",
                content="索引探针正文",
            )
            for index in range(1, MESSAGES + 1)
        ]
    )
    session.commit()


def _capture(session, call):
    """跑一次真实调用，返回它发出的语句 (SQL, 绑定参数) 列表。"""
    captured = []

    def _record(_conn, _cursor, statement, parameters, _context, _executemany):
        captured.append((statement, parameters))

    engine = session.get_bind()
    event.listen(engine, "before_cursor_execute", _record)
    try:
        result = call()
    finally:
        event.remove(engine, "before_cursor_execute", _record)
    return result, captured


def _explain(session, statement, parameters):
    """对捕获到的真实语句取执行计划。

    走 DBAPI 游标而不是 `text()`：捕获到的语句用的是驱动自己的参数风格（qmark），
    交给 `text()` 会被当成另一种占位符再解析一次。SQLite 的 EXPLAIN 允许带绑定参数。
    """
    cursor = session.connection().connection.cursor()
    try:
        cursor.execute("EXPLAIN QUERY PLAN " + statement, parameters)
        return " | ".join(str(row[-1]) for row in cursor.fetchall())
    finally:
        cursor.close()


def _messages_sql(captured):
    """挑出真实调用里对 messages 的那条 SELECT（list_messages 还会先查一次 conversations）。"""
    found = [
        (statement, parameters)
        for statement, parameters in captured
        if statement.lstrip().upper().startswith("SELECT") and "FROM messages" in statement
    ]
    assert found, "list_messages 没有发出对 messages 的查询，用例的前提不成立"
    return found


def _conversations_sql(captured):
    found = [
        (statement, parameters)
        for statement, parameters in captured
        if statement.lstrip().upper().startswith("SELECT") and "FROM conversations" in statement
    ]
    assert found, "list_conversations 没有发出对 conversations 的查询，用例的前提不成立"
    return found


def _plans(*, drop_indexes):
    """在各自独立的库上取三条查询的计划。

    两次测量必须用两个库：同一个 SQLite 连接在 DROP INDEX 之后仍复用已缓存的预处理
    语句，第二次 EXPLAIN 会拿到旧计划，测出来的不是无索引的真实行为。索引必须在跑
    任何查询之前摘掉，连接上才不会留下带索引的计划缓存。

    摘索引用 `IF EXISTS`：这条用例要在「修法被还原」的副本上也跑到断言，缺索引本就
    是该副本的预期状态。若这里因索引不存在而直接抛错，红是红了，但红在准备阶段，
    证明不了断言本身盯的是索引——那样「有索引 / 无索引」两种计划就没被真正并排过。
    """
    engine = create_engine("sqlite://", poolclass=StaticPool)
    Base.metadata.create_all(bind=engine)
    Session = sessionmaker(bind=engine)
    try:
        with Session() as session:
            if drop_indexes:
                for name in (MESSAGE_INDEX, CONVERSATION_USER_INDEX, CONVERSATION_KB_INDEX):
                    session.execute(text(f"DROP INDEX IF EXISTS {name}"))
                session.commit()
            _seed(session)

            rows, captured = _capture(session, lambda: list_messages(session, TARGET_CID, user_id=1, limit=50))
            # 索引只影响计划，不影响结果：这个会话的 10 条消息照常取回。
            assert [row.id for row in rows] == [index for index in range(7, MESSAGES + 1, CONVERSATIONS)]
            first_page = _explain(session, *_messages_sql(captured)[0])

            _, captured = _capture(
                session,
                lambda: list_messages(session, TARGET_CID, user_id=1, limit=50, before_id=MESSAGES),
            )
            with_cursor = _explain(session, *_messages_sql(captured)[0])

            # issue #191：列表读口有页大小上限（默认一页、上限 LIST_MAX_LIMIT），一次调用取不回
            # 全部种子行。本文件量的是执行计划，不是「返回全量」那条契约——后者的验收在
            # tests/test_list_page_caps_191.py。这里显式取满一页（200 写死字面量，不跟实现同步漂移），
            # 断言仍要咬住「索引只影响计划、不影响结果」：返回的正是从这 400 行里取出的一整页。
            conversations, captured = _capture(
                session, lambda: list_conversations(session, user_id=1, limit=200)
            )
            assert len(conversations) == 200
            conversation_list = _explain(session, *_conversations_sql(captured)[0])

        return {"first_page": first_page, "with_cursor": with_cursor, "conversation_list": conversation_list}
    finally:
        engine.dispose()


def test_models_declare_the_hot_query_indexes(sqlite_engine):
    assert Message.__table__.c.conversation_id.index is True
    assert MESSAGE_INDEX in {index.name for index in Message.__table__.indexes}

    assert Conversation.__table__.c.user_id.index is True
    assert CONVERSATION_USER_INDEX in {index.name for index in Conversation.__table__.indexes}

    assert Conversation.__table__.c.knowledge_base_id.index is True
    assert CONVERSATION_KB_INDEX in {index.name for index in Conversation.__table__.indexes}


def test_list_messages_plan_uses_the_conversation_index():
    """并排给出两种执行计划：这正是「补索引」这一项的验收证据。"""
    with_index = _plans(drop_indexes=False)
    without_index = _plans(drop_indexes=True)

    assert MESSAGE_INDEX in with_index["first_page"]
    assert "SCAN messages" not in with_index["first_page"]
    # 带 before_id 游标时基线给的是 SEARCH ... USING INTEGER PRIMARY KEY：它不是整表扫描，
    # 但按主键倒序往回走的行数仍随全站消息总量增长，走到表头为止。
    assert MESSAGE_INDEX in with_index["with_cursor"]

    # 先证红：没有索引时第一页就是对整张 messages 表扫描，也正是 issue 记录的现状。
    assert "SCAN messages" in without_index["first_page"]
    assert MESSAGE_INDEX not in without_index["first_page"]
    assert "INTEGER PRIMARY KEY" in without_index["with_cursor"]


def test_list_conversations_plan_uses_the_user_index():
    with_index = _plans(drop_indexes=False)
    without_index = _plans(drop_indexes=True)

    assert CONVERSATION_USER_INDEX in with_index["conversation_list"]
    assert "SCAN conversations" not in with_index["conversation_list"]

    assert "SCAN conversations" in without_index["conversation_list"]
    assert CONVERSATION_USER_INDEX not in without_index["conversation_list"]

    # 单列索引去掉的是全表扫描；`updated_at` 上的排序仍要建临时 B 树，这一点本单不做
    # （复合索引 (user_id, updated_at) 属可选，见 issue #176 验收标准最后一项）。
    assert "TEMP B-TREE" in with_index["conversation_list"]


def _index_names(engine, table_name):
    return {index["name"] for index in inspect(engine).get_indexes(table_name)}


def _indexed_columns(engine, table_name):
    return {
        tuple(index.get("column_names") or []) for index in inspect(engine).get_indexes(table_name)
    }


def test_schema_migration_backfills_the_new_indexes(sqlite_engine, monkeypatch, caplog):
    """迁移侧：已经建好的库（create_all 不会再跑）要能补上这三条索引，且重复调用安全。"""
    monkeypatch.setattr(db_session, "engine", sqlite_engine)
    with sqlite_engine.begin() as conn:
        for name in (MESSAGE_INDEX, CONVERSATION_USER_INDEX, CONVERSATION_KB_INDEX):
            conn.execute(text(f"DROP INDEX IF EXISTS {name}"))

    assert ("conversation_id",) not in _indexed_columns(sqlite_engine, "messages")
    assert ("user_id",) not in _indexed_columns(sqlite_engine, "conversations")

    db_session._ensure_schema_columns()

    assert MESSAGE_INDEX in _index_names(sqlite_engine, "messages")
    assert CONVERSATION_USER_INDEX in _index_names(sqlite_engine, "conversations")
    assert CONVERSATION_KB_INDEX in _index_names(sqlite_engine, "conversations")

    # 幂等：第二次调用发现索引已在，既不该抛错也不该重复建（重复建会让索引数增长）。
    counts = (
        len(_index_names(sqlite_engine, "messages")),
        len(_index_names(sqlite_engine, "conversations")),
    )
    caplog.clear()
    with caplog.at_level(logging.WARNING, logger="database.session"):
        db_session._ensure_schema_columns()
    assert (
        len(_index_names(sqlite_engine, "messages")),
        len(_index_names(sqlite_engine, "conversations")),
    ) == counts

    # 光看「索引数没变」还不够：去掉 helper 里「已存在就返回」的短路后，第二次调用会
    # 改成 CREATE INDEX 失败再被 except 吞掉，索引数同样不变，从结果看一样是幂等的。
    # 那种路径每次启动都白抛一条警告、还把真实错误藏进吞异常的兜底里，与 helper 自述的
    # 实现（按 inspector 查到的列组合判断是否已存在）不是一回事。这里把机制也钉住：
    # 短路真的生效时，第二次调用不该产生任何警告。
    assert [record for record in caplog.records if record.levelno >= logging.WARNING] == []
