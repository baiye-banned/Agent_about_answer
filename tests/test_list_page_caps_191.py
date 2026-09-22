"""issue #191 回归：三个列表读口的页大小上限与键集翻页。

修复前 `list_knowledge_bases` / `list_conversations` / `list_knowledge_files` 都是「取全量」：
SQL 以 `.all()` 收尾、没有 `limit` 形参，HTTP 层也没有页大小参数，响应体随该用户的数据量
线性增长。这是 #61 明确留下的取舍（PR #81「后续计划」承诺另开跟踪单，但跟踪单从未建立）。

本文件锁四件事：
1. 三层各有上限——路由的 `Query(ge/le)` 拦越界、service 对直接调用者再兜一次、
   CRUD 层兜底夹取（负值在 SQLite 上等价于「不设上限」）；
2. 翻页语义是键集游标（不是 offset）：翻完全部页恰好等于全量顺序，不重复、不遗漏——
   其中会话列表的 `updated_at` 是秒级 DATETIME，用例专门造了同秒数据来压末位键；
3. 「加数据不加返回条数」：数据集翻倍，单次响应条数不变；
4. 页大小口径本身：列表链路与消息链路必须是同一组数值，防止两套常量各自漂移。
"""

import ast
import inspect
import textwrap
from contextlib import contextmanager
from datetime import datetime, timedelta
from types import SimpleNamespace

import pytest
from fastapi import FastAPI, HTTPException
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, event
from sqlalchemy.dialects.mysql import LONGTEXT
from sqlalchemy.ext.compiler import compiles
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from crud import chat as crud_chat
from crud import knowledge_base as crud_knowledge_base
from crud import knowledge_file as crud_knowledge_file
from crud import pagination as crud_pagination
from database import session as db_session
from database.session import Base
from model.models import Conversation, KnowledgeBase, KnowledgeFile, Message, RevokedToken, User
from router import chat as chat_router
from router import knowledge as knowledge_router
from service import auth_service, chat_service, knowledge_service, pagination_service


# 接口契约值：默认页大小 50、单页上限 200。这里写字面量而不是从实现里读，
# 否则实现把默认页大小改小/改没，用例会跟着一起「通过」。
PAGE_LIMIT = 50
MAX_LIMIT = 200

@compiles(LONGTEXT, "sqlite")
def _compile_longtext_as_text(_type, _compiler, **_kwargs):
    return "TEXT"


@pytest.fixture()
def api():
    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(
        bind=engine,
        tables=[
            User.__table__, RevokedToken.__table__,
            KnowledgeBase.__table__,
            KnowledgeFile.__table__,
            Conversation.__table__,
            Message.__table__,
        ],
    )
    db = sessionmaker(bind=engine, autoflush=False, autocommit=False, expire_on_commit=False)()

    alice = User(username="alice", password_hash="x")
    bob = User(username="bob", password_hash="x")
    db.add_all([alice, bob])
    db.commit()

    statements: list[str] = []

    @event.listens_for(engine, "before_cursor_execute")
    def _record_statement(conn, cursor, statement, parameters, context, executemany):
        if not executemany and statement.strip().upper().startswith("SELECT"):
            statements.append(" ".join(statement.split()))

    app = FastAPI()
    app.include_router(knowledge_router.router)
    app.include_router(chat_router.router)
    app.dependency_overrides[db_session.get_db] = lambda: db
    app.dependency_overrides[auth_service.get_current_user] = lambda: alice

    try:
        yield SimpleNamespace(
            app=app,
            client=TestClient(app),
            db=db,
            alice=alice,
            statements=statements,
        )
    finally:
        db.close()


@contextmanager
def recorded_selects(api):
    """记录请求期间引擎实际发出的 SELECT 语句（identity map 先清空，避免懒加载被掩盖）。"""
    api.db.expunge_all()
    api.statements.clear()
    yield api.statements


def _add_knowledge_base(api, name, files=0):
    base = KnowledgeBase(name=name, user_id=api.alice.id)
    api.db.add(base)
    api.db.flush()
    for index in range(files):
        api.db.add(KnowledgeFile(
            knowledge_base_id=base.id,
            user_id=api.alice.id,
            name=f"{name}-{index}.txt",
            size=10,
            content="正文",
        ))
    api.db.commit()
    return base


def _add_conversation(api, cid, updated_at=None):
    conversation = Conversation(id=cid, user_id=api.alice.id, title=f"会话-{cid}")
    if updated_at is not None:
        # 显式给同一秒的时间戳：server_default 的 func.now() 只到秒，真实数据里同秒改动
        # 的会话很常见，游标的末位键（uuid 主键）必须能把它们排出确定顺序。
        conversation.updated_at = updated_at
        conversation.created_at = updated_at
    api.db.add(conversation)
    api.db.commit()
    return conversation


def _all_knowledge_bases(api):
    return api.db.query(KnowledgeBase).filter_by(user_id=api.alice.id).order_by(KnowledgeBase.id.asc()).all()


def _all_conversations(api):
    return (
        api.db.query(Conversation)
        .filter_by(user_id=api.alice.id)
        .order_by(Conversation.updated_at.desc(), Conversation.id.desc())
        .all()
    )


def _all_files(api, knowledge_base_id):
    return (
        api.db.query(KnowledgeFile)
        .filter_by(knowledge_base_id=knowledge_base_id, user_id=api.alice.id)
        .order_by(KnowledgeFile.id.desc())
        .all()
    )


# ---------------------------------------------------------------- 口径常量

def test_list_page_limits_match_the_message_interface_values():
    """列表链路另立了常量（不反向依赖 chat），但取值必须与消息接口一致。"""
    assert crud_pagination.LIST_DEFAULT_LIMIT == PAGE_LIMIT
    assert crud_pagination.LIST_MAX_LIMIT == MAX_LIMIT
    assert crud_pagination.LIST_DEFAULT_LIMIT == crud_chat.CHAT_MESSAGE_DEFAULT_LIMIT
    assert crud_pagination.LIST_MAX_LIMIT == crud_chat.CHAT_MESSAGE_MAX_LIMIT


# ---------------------------------------------------------------- 默认页大小

def test_knowledge_base_list_defaults_to_one_page(api):
    created = [_add_knowledge_base(api, f"kb-{index}").id for index in range(PAGE_LIMIT + 20)]

    response = api.client.get("/api/knowledge-bases")

    assert response.status_code == 200
    body = response.json()
    assert len(body) == PAGE_LIMIT
    # 不传参时返回第一页，且顺序不变：按创建顺序（旧 -> 新）取前 PAGE_LIMIT 个。
    assert [item["id"] for item in body] == created[:PAGE_LIMIT]


def test_conversation_list_defaults_to_one_page(api):
    # 逐条给上递增的 updated_at：把「顺序」与「主键字典序」解耦，断言才真的在量顺序
    # （第二页起 uuid 主键是字典序，不是创建序；同秒数据的形态由下面的并列键用例覆盖）。
    start = datetime(2026, 9, 1, 10, 0, 0)
    for index in range(PAGE_LIMIT + 20):
        _add_conversation(api, f"conv-{index}", updated_at=start + timedelta(seconds=index))
    expected = [item.id for item in _all_conversations(api)]

    response = api.client.get("/api/chat/conversations")

    assert response.status_code == 200
    body = response.json()
    assert len(body) == PAGE_LIMIT
    # 不传参时返回最新一页：最近活动的在前，取到的正是倒序的前 PAGE_LIMIT 个。
    assert [item["id"] for item in body] == expected[:PAGE_LIMIT]


def test_knowledge_file_list_defaults_to_one_page(api):
    base = _add_knowledge_base(api, "制度库", files=PAGE_LIMIT + 20)
    created = [item.id for item in _all_files(api, base.id)]

    response = api.client.get("/api/knowledge", params={"knowledge_base_id": base.id})

    assert response.status_code == 200
    body = response.json()
    assert len(body) == PAGE_LIMIT
    # 不传参时返回最新一页（上传时间倒序），同样取倒序的前 PAGE_LIMIT 个。
    assert [item["id"] for item in body] == created[:PAGE_LIMIT]


def test_adding_rows_beyond_the_cap_does_not_grow_the_response(api):
    """「加数据不加返回条数」：数据集翻倍后单次响应仍是同一页大小。"""
    base = _add_knowledge_base(api, "制度库", files=3)
    _add_knowledge_base(api, "零号库", files=0)
    _add_conversation(api, "conv-0")

    def measure():
        return {
            "知识库列表": len(api.client.get("/api/knowledge-bases").json()),
            "会话列表": len(api.client.get("/api/chat/conversations").json()),
            "文件列表": len(
                api.client.get("/api/knowledge", params={"knowledge_base_id": base.id}).json()
            ),
        }

    before = measure()
    assert before["文件列表"] == 3  # 不足一页时如实返回全部

    for index in range(PAGE_LIMIT * 2):
        _add_knowledge_base(api, f"kb-{index}")
        _add_conversation(api, f"conv-more-{index}")
        api.db.add(KnowledgeFile(
            knowledge_base_id=base.id, user_id=api.alice.id,
            name=f"more-{index}.txt", size=10, content="正文",
        ))
    api.db.commit()

    after = measure()
    assert after["知识库列表"] == PAGE_LIMIT
    assert after["会话列表"] == PAGE_LIMIT
    assert after["文件列表"] == PAGE_LIMIT


# ---------------------------------------------------------------- 翻页不重不漏

def test_knowledge_base_cursor_pages_cover_every_row_once(api):
    created = [_add_knowledge_base(api, f"kb-{index}").id for index in range(123)]

    collected: list[int] = []
    cursor = None
    for _ in range(10):
        params = {"limit": 25}
        if cursor is not None:
            params["after_id"] = cursor
        body = api.client.get("/api/knowledge-bases", params=params).json()
        if not body:
            break
        collected.extend(item["id"] for item in body)
        cursor = body[-1]["id"]
    else:
        pytest.fail("游标翻页没有终止")

    assert collected == created  # 不重复、不遗漏，且整体仍是升序


def test_knowledge_file_cursor_pages_cover_every_row_once(api):
    base = _add_knowledge_base(api, "制度库", files=123)
    expected = [item.id for item in _all_files(api, base.id)]

    collected: list[int] = []
    cursor = None
    for _ in range(10):
        params = {"knowledge_base_id": base.id, "limit": 25}
        if cursor is not None:
            params["before_id"] = cursor
        body = api.client.get("/api/knowledge", params=params).json()
        if not body:
            break
        collected.extend(item["id"] for item in body)
        cursor = body[-1]["id"]
    else:
        pytest.fail("游标翻页没有终止")

    assert collected == expected  # 不重复、不遗漏，且整体仍是降序


def test_conversation_cursor_pages_cover_every_row_once(api):
    for index in range(123):
        _add_conversation(api, f"conv-{index}")
    expected = [item.id for item in _all_conversations(api)]

    collected: list[str] = []
    cursor = None
    for _ in range(10):
        params = {"limit": 25}
        if cursor is not None:
            params["before_updated_at"], params["before_id"] = cursor
        body = api.client.get("/api/chat/conversations", params=params).json()
        if not body:
            break
        collected.extend(item["id"] for item in body)
        cursor = (body[-1]["updated_at"], body[-1]["id"])
    else:
        pytest.fail("游标翻页没有终止")

    assert collected == expected


def test_conversation_cursor_is_total_when_updated_at_ties(api):
    """`updated_at` 是秒级 DATETIME：同秒数据必须靠 uuid 主键排出确定顺序。

    会话主键是 uuid（不是自增整数），所以这里的游标是 (updated_at, id) 复合键。
    没有末位键时同秒行按数据库返回顺序排，翻页会重复或漏行。
    """
    same_second = datetime(2026, 9, 1, 10, 0, 0)
    for index in range(23):
        _add_conversation(api, f"conv-tie-{index}", updated_at=same_second)
    expected = [item.id for item in _all_conversations(api)]
    assert len(set(expected)) == 23

    collected: list[str] = []
    cursor = None
    for _ in range(10):
        params = {"limit": 7}
        if cursor is not None:
            params["before_updated_at"], params["before_id"] = cursor
        body = api.client.get("/api/chat/conversations", params=params).json()
        if not body:
            break
        collected.extend(item["id"] for item in body)
        cursor = (body[-1]["updated_at"], body[-1]["id"])
    else:
        pytest.fail("游标翻页没有终止")

    assert collected == expected


def test_paged_request_query_count_stays_constant(api):
    """翻页请求的 SELECT 条数不随数据量增长（列表查询数上界，见 test_list_query_counts.py）。"""
    base = _add_knowledge_base(api, "制度库", files=3)
    _add_knowledge_base(api, "零号库", files=0)
    _add_conversation(api, "conv-first")

    def measured(url, params):
        with recorded_selects(api) as statements:
            assert api.client.get(url, params=params).status_code == 200
            return len(list(statements))

    first = {
        "知识库列表": measured("/api/knowledge-bases", {}),
        "会话列表": measured("/api/chat/conversations", {}),
        "文件列表": measured("/api/knowledge", {"knowledge_base_id": base.id}),
    }

    for index in range(PAGE_LIMIT * 2):
        _add_knowledge_base(api, f"kb-{index}", files=0)
        _add_conversation(api, f"conv-more-{index}")
        api.db.add(KnowledgeFile(
            knowledge_base_id=base.id, user_id=api.alice.id,
            name=f"more-{index}.txt", size=10, content="正文",
        ))
    api.db.commit()

    again = {
        "知识库列表": measured("/api/knowledge-bases", {"after_id": 1}),
        "会话列表": measured("/api/chat/conversations", {"limit": 10}),
        "文件列表": measured("/api/knowledge", {"knowledge_base_id": base.id, "limit": 10}),
    }

    assert again == first
    assert all(count <= 2 for count in again.values())


# ---------------------------------------------------------------- 越界 422

@pytest.mark.parametrize("url,params", [
    ("/api/knowledge-bases", {}),
    ("/api/chat/conversations", {}),
    ("/api/knowledge", {}),
])
def test_page_size_out_of_range_is_rejected(api, url, params):
    for bad in (0, -1, MAX_LIMIT + 1, "abc"):
        response = api.client.get(url, params={**params, "limit": bad})
        assert response.status_code == 422, f"{url} limit={bad}"


def test_cursor_params_are_validated(api):
    base = _add_knowledge_base(api, "制度库", files=3)
    _add_conversation(api, "conv-0")

    # 游标同样是键值，非法值不能悄悄退回第一页（那会让调用方把第一页当成第二页）。
    assert api.client.get("/api/knowledge-bases", params={"after_id": 0}).status_code == 422
    assert api.client.get("/api/knowledge-bases", params={"after_id": "abc"}).status_code == 422
    assert api.client.get(
        "/api/knowledge", params={"knowledge_base_id": base.id, "before_id": 0}
    ).status_code == 422
    # 会话的复合游标必须成对出现。
    assert api.client.get("/api/chat/conversations", params={"before_id": "conv-0"}).status_code == 422
    assert api.client.get(
        "/api/chat/conversations", params={"before_updated_at": "2026-09-01T10:00:00"}
    ).status_code == 422
    assert api.client.get(
        "/api/chat/conversations", params={"before_updated_at": "not-a-time", "before_id": "x"}
    ).status_code == 422

    # 两个都空 = 没给游标（取最新一页）；只给一个（或给了空白 id）才是残缺游标。
    assert pagination_service.resolve_conversation_cursor(None, "  ") is None
    assert pagination_service.resolve_conversation_cursor(None, None) is None
    with pytest.raises(HTTPException) as excinfo:
        pagination_service.resolve_conversation_cursor(None, "conv-0")
    assert excinfo.value.status_code == 422
    with pytest.raises(HTTPException) as excinfo:
        pagination_service.resolve_conversation_cursor(datetime(2026, 9, 1), "")
    assert excinfo.value.status_code == 422


# ---------------------------------------------------------------- service 兜底

def test_service_layer_rejects_out_of_range_limit_for_direct_callers(api):
    """直接调用 service（脚本、内部调用）的路径同样拿不到超限页大小。"""
    assert pagination_service.resolve_list_limit(None) == PAGE_LIMIT
    assert pagination_service.resolve_list_limit(MAX_LIMIT) == MAX_LIMIT

    with pytest.raises(HTTPException) as limit_error:
        pagination_service.resolve_list_limit(MAX_LIMIT + 1)
    assert limit_error.value.status_code == 422

    with pytest.raises(HTTPException) as bases_error:
        knowledge_service.list_knowledge_bases(limit=MAX_LIMIT + 1, user=api.alice, db=api.db)
    assert bases_error.value.status_code == 422

    with pytest.raises(HTTPException) as conversations_error:
        chat_service.list_conversations(limit=0, user=api.alice, db=api.db)
    assert conversations_error.value.status_code == 422

    with pytest.raises(HTTPException) as files_error:
        knowledge_service.list_knowledge(limit=MAX_LIMIT + 1, user=api.alice, db=api.db)
    assert files_error.value.status_code == 422


def test_service_layer_defaults_to_one_page_for_direct_callers(api):
    for index in range(PAGE_LIMIT + 5):
        _add_knowledge_base(api, f"kb-{index}")
        _add_conversation(api, f"conv-{index}")

    assert len(knowledge_service.list_knowledge_bases(user=api.alice, db=api.db)) == PAGE_LIMIT
    assert len(chat_service.list_conversations(user=api.alice, db=api.db)) == PAGE_LIMIT


# ---------------------------------------------------------------- CRUD 兜底夹取

def test_crud_layer_clamps_the_page_size(api):
    """CRUD 是更底层入口：负值 LIMIT 在 SQLite 上等于不设上限，必须在最里层夹住。"""
    for index in range(MAX_LIMIT + 10):
        _add_knowledge_base(api, f"kb-{index}")
        _add_conversation(api, f"conv-{index}")
    base = _add_knowledge_base(api, "制度库", files=MAX_LIMIT + 10)

    for limit in (-1, 0, MAX_LIMIT * 10):
        bases = crud_knowledge_base.list_knowledge_bases(api.db, api.alice.id, limit=limit)
        conversations = crud_chat.list_conversations(api.db, api.alice.id, limit=limit)
        files = crud_knowledge_file.list_knowledge_files(api.db, base.id, api.alice.id, limit=limit)
        assert 0 < len(bases) <= MAX_LIMIT
        assert 0 < len(conversations) <= MAX_LIMIT
        assert 0 < len(files) <= MAX_LIMIT
        # 夹取只改页大小，不改取哪一页：仍然是第一页。
        assert bases[0].id == _all_knowledge_bases(api)[0].id
        assert files[0].id == _all_files(api, base.id)[0].id

    # 缺省（None）表示「按默认页大小」，不等于「不设上限」。
    assert len(crud_knowledge_base.list_knowledge_bases(api.db, api.alice.id, limit=None)) == PAGE_LIMIT
    assert len(crud_chat.list_conversations(api.db, api.alice.id, limit=None)) == PAGE_LIMIT
    assert len(
        crud_knowledge_file.list_knowledge_files(api.db, base.id, api.alice.id, limit=None)
    ) == PAGE_LIMIT


def test_crud_cursor_is_keyset_not_offset(api):
    """游标取的是「排在这条之后」的行：往数据集里插新行不改变下一页的内容。"""
    ids = [_add_knowledge_base(api, f"kb-{index}").id for index in range(6)]

    first = crud_knowledge_base.list_knowledge_bases(api.db, api.alice.id, limit=3)
    assert [row.id for row in first] == ids[:3]

    # 翻页途中新建知识库（真实场景里随时会发生）：它排在游标之后，不影响当前这一页。
    fresh = _add_knowledge_base(api, "翻页途中建的库")
    second = crud_knowledge_base.list_knowledge_bases(
        api.db, api.alice.id, limit=3, after_id=first[-1].id
    )
    assert [row.id for row in second] == ids[3:6]
    assert fresh.id not in [row.id for row in second]


# ---------------------------------------------------------------- 探针口径做成门禁

def _capped(func) -> bool:
    """与 `_hpsd_c_probe.py` 同一判据：函数里出现 `.limit()` 调用或带 `limit` 形参。"""
    node = ast.parse(textwrap.dedent(inspect.getsource(func))).body[0]
    has_limit_call = any(
        isinstance(item, ast.Call) and isinstance(item.func, ast.Attribute) and item.func.attr == "limit"
        for item in ast.walk(node)
    )
    params = {arg.arg for arg in node.args.args} | {arg.arg for arg in node.args.kwonlyargs}
    return has_limit_call or "limit" in params


def _uncapped_sample(rows):  # 阳性/阴性对照：本探针必须能区分这两种形状
    return rows


def _capped_sample(rows, limit=None):
    return rows[:limit]


def test_probe_criterion_discriminates_and_pins_the_three_reads(api):
    """把 #191 的静态探针口径固化成门禁用例：三个读口必须一直是 CAPPED。

    先证明判据本身有区分力（两个自造样本一正一反），再钉住四个真实函数的判定：
    三个目标函数 CAPPED，`list_files_for_knowledge_base` UNCAPPED——它不是读口，
    删除链路要拿它清空知识库下的全部文件，**故意不能有上限**，写在这里避免以后被
    「顺手统一一下」改坏。
    """
    assert _capped(_capped_sample) is True
    assert _capped(_uncapped_sample) is False

    assert _capped(crud_knowledge_base.list_knowledge_bases) is True
    assert _capped(crud_chat.list_conversations) is True
    assert _capped(crud_knowledge_file.list_knowledge_files) is True
    assert _capped(crud_knowledge_base.list_files_for_knowledge_base) is False
