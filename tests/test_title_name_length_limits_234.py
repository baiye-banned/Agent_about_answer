"""回归（issue #234）：会话标题与知识库名称的**入口**长度上限。

修复前 `RenameRequest.title` 与 `KnowledgeBaseRequest.name` 都是裸 `str`，入口没有任何上限，
超长值一路穿过路由写到 DB。SQLite 静默接受，生产 MySQL 严格模式下抛 `DataError(1406)`，
而全局异常兜底只接 `IntegrityError` ⇒ 推断为 500。修法是在字段上加 `Field(max_length=…)`。

**本套用例判的是入口契约**——「超长值到不了服务层 / DB，且既有短值不受影响」。
它**不是**「MySQL 严格模式下 `DataError(1406) → 500` 已被消除」的回归证据：后者在 SQLite
上结构性不可测（SQLite 静默接受超长写入，实测未打补丁的代码对超长请求回 200，永远复现不出
500），只能靠手工冒烟或生产观察。两个命题不可互相替代，别把这里的绿当成那件事的证据。

断言只取稳定面：`status_code`、`detail[0]["type"|"loc"|"ctx"]`。刻意不比对 `msg` 文案
（pydantic 版本间会变）、不比对 `input`（回显、且可能巨大），**也不断言 `url` 键、不对
`detail[0]` 做键集全等比对**——FastAPI 构造 `RequestValidationError` 响应时会剥掉 pydantic
`errors()` 里的 `url`（实测 `detail[0]` 恰为 `ctx/input/loc/msg/type` 五键），照 pydantic
文档写那条断言会得到一条假红。
"""

import re
from pathlib import Path
from types import SimpleNamespace

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.dialects.mysql import LONGTEXT
from sqlalchemy.ext.compiler import compiles
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from database import session as db_session
from database.session import Base
from model.models import Conversation, KnowledgeBase, KnowledgeFile, RevokedToken, User
from router import chat as chat_router
from router import knowledge as knowledge_router
from schema import schemas
from service import auth_service

ROOT = Path(__file__).resolve().parents[1]
CHAT_VUE = ROOT / "src" / "views" / "Chat.vue"
KNOWLEDGE_VUE = ROOT / "src" / "views" / "Knowledge.vue"

# 字面量而不是从实现里读：实现把上限调大调小，用例得跟着红，而不是跟着一起「通过」。
TITLE_LIMIT = 40
NAME_LIMIT = 100
# 列宽（model/models.py：conversations.title String(200)、knowledge_bases.name String(100)）。
# 超长样例取「列宽 + 1」：它同时越过接口上限与列宽，正是修复前写到这里触发 1406 的那类值。
TITLE_COLUMN_WIDTH = 200
NAME_COLUMN_WIDTH = 100


@compiles(LONGTEXT, "sqlite")
def _compile_longtext_as_text(_type, _compiler, **_kwargs):
    """把 MySQL 的 LONGTEXT 在 SQLite 下渲染成 TEXT（与 tests/test_knowledge_base_name_race.py 同款垫片）。

    KnowledgeFile.content 是 LONGTEXT，SQLite 方言不认这个类型名；不加垫片直接把它塞进
    `create_all` 会得到 `CompileError: ... can't render element`。
    """
    return "TEXT"


@pytest.fixture()
def api():
    """真路由 + 内存 SQLite 的最小应用（建表口径同 tests/test_knowledge_base_name_race.py）。

    建这五张表：User / RevokedToken / KnowledgeBase / Conversation，**外加 KnowledgeFile**。

    KnowledgeFile 不是可选项，哪怕本套用例的 T1/T2/T3 打的是「超长必被拒」：T3 命中的 KB
    重命名在**被接受**时会调 `crud_knowledge_base.count_knowledge_files` 给响应体算文件数
    （service/knowledge_service.py:432），那条 SQL 打在 knowledge_files 上。表不在就抛
    `OperationalError: no such table: knowledge_files`——用例会炸成异常红，看着像「超长没被
    拒」的反面，其实是 fixture 缺表（实测：合法短名的 rename 走通到这一行即抛）。
    `create_knowledge_base` 走的是「新库名下不可能有文件、直接给 0」的短路（同文件 :405），
    所以借道 create 的 T2 不会暴露这个缺口，只有 rename 这条被接受路径会。

    KnowledgeFile 带 LONGTEXT 列，故需上面的 `@compiles` 垫片；只加表不加垫片会 `CompileError`。
    """
    engine = create_engine(
        "sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool
    )
    Base.metadata.create_all(
        bind=engine,
        tables=[
            User.__table__,
            RevokedToken.__table__,
            KnowledgeBase.__table__,
            KnowledgeFile.__table__,
            Conversation.__table__,
        ],
    )
    db = sessionmaker(bind=engine, autoflush=False, autocommit=False, expire_on_commit=False)()
    alice = User(username="alice", password_hash="x")
    db.add(alice)
    db.commit()

    app = FastAPI()
    app.include_router(knowledge_router.router)
    app.include_router(chat_router.router)
    app.dependency_overrides[db_session.get_db] = lambda: db
    app.dependency_overrides[auth_service.get_current_user] = lambda: alice
    try:
        yield SimpleNamespace(app=app, client=TestClient(app), db=db, alice=alice)
    finally:
        db.close()


@pytest.fixture()
def anonymous_client(api):
    """**只**覆盖 get_db、不覆盖 get_current_user 的 client（T7 的构造陷阱）。

    `api` fixture 把 get_current_user 覆盖成了 alice，用它永远问不出「未认证时先回哪个错」：
    真实鉴权分支（Missing / Invalid authorization header）根本不会被执行。这里另起一个 app，
    保留依赖链原样，才看得见「依赖先于请求体校验」这个顺序。
    """
    app = FastAPI()
    app.include_router(knowledge_router.router)
    app.include_router(chat_router.router)
    app.dependency_overrides[db_session.get_db] = lambda: api.db
    return TestClient(app)


def _seed_conversation(api, title):
    conv = Conversation(user_id=api.alice.id, title=title)
    api.db.add(conv)
    api.db.commit()
    return conv


def _seed_knowledge_base(api, name):
    base = KnowledgeBase(user_id=api.alice.id, name=name)
    api.db.add(base)
    api.db.commit()
    return base


def _reload(api):
    """丢掉身份映射后重新查库，确保读到的是落库值而不是会话里那份对象。"""
    api.db.expunge_all()
    return api.db


def _stored_title(api, cid):
    return _reload(api).query(Conversation).filter_by(id=cid).one().title


def _stored_name(api, kid):
    return _reload(api).query(KnowledgeBase).filter_by(id=kid).one().name


def _assert_string_too_long(response, loc, limit):
    """422 的稳定面：类型、字段位置、上限值。文案/回显/键集一律不碰（见文件头）。"""
    assert response.status_code == 422, (
        f"期望 422（入口拒收超长值），实得 {response.status_code}：{response.text[:200]}"
    )
    detail = response.json()["detail"]
    assert isinstance(detail, list) and detail, f"detail 不是非空列表：{detail!r}"
    assert detail[0]["type"] == "string_too_long", f"detail[0]={detail[0]!r}"
    assert detail[0]["loc"] == loc, f"detail[0]={detail[0]!r}"
    assert detail[0]["ctx"]["max_length"] == limit, f"detail[0]={detail[0]!r}"


# --- T1：会话改名 -------------------------------------------------------------------


def test_oversized_conversation_title_is_rejected_with_422(api):
    conv = _seed_conversation(api, "原标题")
    response = api.client.put(
        f"/api/chat/conversations/{conv.id}",
        json={"title": "a" * (TITLE_COLUMN_WIDTH + 1)},
    )
    _assert_string_too_long(response, ["body", "title"], TITLE_LIMIT)
    assert _stored_title(api, conv.id) == "原标题", "被拒的请求不该动到库里那一行"


# --- T2 / T3：知识库新建与重命名（同一个 KnowledgeBaseRequest 模型，两个端点）--------


def test_oversized_knowledge_base_name_is_rejected_with_422(api):
    before = _reload(api).query(KnowledgeBase).filter_by(user_id=api.alice.id).count()
    response = api.client.post(
        "/api/knowledge-bases", json={"name": "a" * (NAME_COLUMN_WIDTH + 1)}
    )
    _assert_string_too_long(response, ["body", "name"], NAME_LIMIT)
    after = _reload(api).query(KnowledgeBase).filter_by(user_id=api.alice.id).count()
    assert after == before, "被拒的请求不该建出知识库"


def test_oversized_knowledge_base_rename_is_rejected_with_422(api):
    base = _seed_knowledge_base(api, "旧名")
    response = api.client.put(
        f"/api/knowledge-bases/{base.id}", json={"name": "a" * (NAME_COLUMN_WIDTH + 1)}
    )
    _assert_string_too_long(response, ["body", "name"], NAME_LIMIT)
    assert _stored_name(api, base.id) == "旧名", "被拒的请求不该改到库里那一行"


# --- T4：边界阳性对照（上限值必须可用，越一格必须被拒）------------------------------


def test_length_boundary_keeps_the_usable_values(api):
    conv = _seed_conversation(api, "原标题")

    at_limit = "a" * TITLE_LIMIT
    ok = api.client.put(f"/api/chat/conversations/{conv.id}", json={"title": at_limit})
    assert ok.status_code == 200, f"恰好 40 字符的标题必须能存：{ok.status_code} {ok.text[:200]}"
    stored = _stored_title(api, conv.id)
    assert stored == at_limit and len(stored) == TITLE_LIMIT

    over = api.client.put(
        f"/api/chat/conversations/{conv.id}", json={"title": "a" * (TITLE_LIMIT + 1)}
    )
    assert over.status_code == 422, "41 字符必须被拒，否则上限形同虚设"

    at_limit_name = "a" * NAME_LIMIT
    ok = api.client.post("/api/knowledge-bases", json={"name": at_limit_name})
    assert ok.status_code == 200, f"恰好 100 字符的名称必须能建：{ok.status_code} {ok.text[:200]}"
    created = (
        _reload(api).query(KnowledgeBase).filter_by(user_id=api.alice.id, name=at_limit_name).one()
    )
    assert len(created.name) == NAME_LIMIT

    over = api.client.post("/api/knowledge-bases", json={"name": "a" * (NAME_LIMIT + 1)})
    assert over.status_code == 422, "101 字符必须被拒，否则上限形同虚设"

    # 口径是**字符**不是**字节**：40 个汉字是 40 字符 / 120 字节，按字节收会误杀合法标题。
    cjk_title = "中" * TITLE_LIMIT
    result = api.client.put(f"/api/chat/conversations/{conv.id}", json={"title": cjk_title})
    assert result.status_code == 200, (
        f"{TITLE_LIMIT} 个汉字的标题（{len(cjk_title.encode('utf-8'))} 字节）被误杀："
        f"{result.status_code} {result.text[:200]}"
    )
    assert _stored_title(api, conv.id) == cjk_title

    cjk_name = "中" * NAME_LIMIT
    result = api.client.post("/api/knowledge-bases", json={"name": cjk_name})
    assert result.status_code == 200, (
        f"{NAME_LIMIT} 个汉字的名称（{len(cjk_name.encode('utf-8'))} 字节）被误杀："
        f"{result.status_code} {result.text[:200]}"
    )


# --- T5：入口上限不得超过列宽 -------------------------------------------------------


def test_entry_limits_do_not_exceed_column_widths():
    """上限一旦超过列宽，超长值会在 DB 那一层复活 1406——入口约束就白加了。"""
    assert schemas.CONVERSATION_TITLE_MAX_LENGTH == TITLE_LIMIT
    assert schemas.KNOWLEDGE_BASE_NAME_MAX_LENGTH == NAME_LIMIT
    assert schemas.CONVERSATION_TITLE_MAX_LENGTH <= Conversation.__table__.c.title.type.length
    assert schemas.KNOWLEDGE_BASE_NAME_MAX_LENGTH <= KnowledgeBase.__table__.c.name.type.length


# --- T6：前端 maxlength 与入口上限同值 ---------------------------------------------


_EL_INPUT = re.compile(r"<el-input\b.*?/>", re.S)


def _input_bound_to(path, binding):
    """定位**具体那个**输入框：靠 v-model 绑定认人，而不是全文件搜 `maxlength=`。

    裸 grep 会把任何一个新增的、无关的 `maxlength="40"` 当成合格证据；锚定绑定之后，
    改名框被换掉 / 绑定改名，这条立刻红。
    """
    text = path.read_text(encoding="utf-8")
    blocks = [block for block in _EL_INPUT.findall(text) if binding in block]
    assert len(blocks) == 1, (
        f"{path.relative_to(ROOT)} 里绑定 {binding} 的 <el-input> 应恰有一个，实得 {len(blocks)}"
    )
    found = re.findall(r'maxlength="([^"]*)"', blocks[0])
    assert len(found) == 1, f"该 input 上的 maxlength 应恰有一个，实得 {found!r}"
    return found[0]


def test_frontend_maxlength_matches_schema_limits():
    """前端 maxlength 与入口上限必须同值——这是**刻意**钉住的强不变式，不是巧合。

    UI 放宽（比如 maxlength=200）而接口仍收 40：用户能在框里敲出 41+ 个字符、看上去一切正常，
    点保存才吃 422，而且怎么删都救不回来（多余字符本就不该存在）。
    UI 收紧（比如 maxlength=20）而接口仍收 40：31–40 字符的既有标题在改名框里显示不全、
    也改不回去，等于把合法数据变成只读。
    两头都咬人，所以取值必须三层归一（列宽 ⊇ 接口 = UI ⊇ 自生成 33）。改任何一层都要同时改这里。
    """
    assert int(_input_bound_to(CHAT_VUE, 'v-model.trim="renameTitle"')) == (
        schemas.CONVERSATION_TITLE_MAX_LENGTH
    )
    assert int(_input_bound_to(KNOWLEDGE_VUE, 'v-model="knowledgeBaseForm.name"')) == (
        schemas.KNOWLEDGE_BASE_NAME_MAX_LENGTH
    )


# --- T7：依赖先于请求体校验（401 而不是 422）---------------------------------------


def test_oversized_request_without_token_is_401_not_422(api, anonymous_client):
    """未认证 + 超长请求体，回的是 401 而不是 422。

    这不是断言「哪种更对」，而是把现在的顺序钉下来：FastAPI 先解依赖（get_current_user），
    再校验请求体，所以两类错同时出现时鉴权先说话。哪天顺序变了（比如给路由挂了 body 级中间件），
    这条会红，提示重新确认对外契约——未认证调用方不该从 422 的 `loc`/`ctx` 里读到字段上限。
    """
    conv = _seed_conversation(api, "原标题")
    cases = [
        ("put", f"/api/chat/conversations/{conv.id}", {"title": "a" * (TITLE_LIMIT + 1)}),
        ("post", "/api/knowledge-bases", {"name": "a" * (NAME_LIMIT + 1)}),
    ]
    headers_variants = [{}, {"Authorization": "Bearer not-a-token"}]
    for method, url, body in cases:
        for headers in headers_variants:
            response = getattr(anonymous_client, method)(url, json=body, headers=headers)
            assert response.status_code == 401, (
                f"{method.upper()} {url} headers={headers}: 期望 401，实得 "
                f"{response.status_code}：{response.text[:200]}"
            )
            assert "detail" in response.json()
