"""issue #238 回归：清扫的「墓碑前移」——删除意图必须在碰对象之前落库。

#142 让未发送的附件有了回收路径，但那条路径的顺序是「先删对象、再倒推账」：

    claim（条件删行）→ commit → DELETE 对象 → …

窄窗口里的两种错序各踩坏一件事。发送侧在 claim 与 DELETE 之间赢下这一行时，对象照样被
删掉——落库的消息引用一个已经不存在的对象；反过来，claim 之后进程死在半路上，那一行已经
从表里消失，下一轮清扫再也不知道要去补删哪把键（对象存储没有回收站，泄漏不可逆）。

#238 把这个顺序倒过来：清扫先在**同一句条件 UPDATE** 里写下 `claimed_at`（租约）与
`reclaimed_at`（删除意图的墓碑）并提交，之后才去外呼 DELETE。于是

- 谁先提交谁赢，且赢家是谁**只看 rowcount**：发送侧的消费（`confirm_attachment_uploads`）
  与清扫侧的领取（`claim_attachment_upload`）带同一个「墓碑为空」条件，不可能同时命中；
- 墓碑一旦写下就不回退——删除成功只清 `claimed_at`，行作为「这把键已被判死」的既成事实
  留下，让该键的发送被拒（判据只读 `reclaimed_at`，绝不把租约超时当成放行条）；
- 行因此成了一张四态状态机：pending（三列皆空）/ deleting（租约与墓碑皆非空）/ reclaimed
  （只剩墓碑）/ consumed（`consumed_at` 非空）。

断言的都是外部可见结果：DELETE 请求本身、库里那一行的四列状态、落库的消息。时间条件靠
直接改写行上的时间列摆出来（不 sleep），「另一条会话」用同一引擎上独立的 Session 开——
生产里发送与清扫本来就各持一个会话。文件里有几条是「护栏」用例（无行的键不许被拒、租约
超时不构成放行、GC 不吃掉正在删除的行、外来键不进 DELETE）：它们在修复前也「通过」，锁的
是新判据不得扩大拒签面、不得把删除面铺到不该删的对象上。
"""

import asyncio
import ast
import inspect as py_inspect
import json
from datetime import datetime, timedelta
from pathlib import Path
from types import SimpleNamespace

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, inspect, text, update
from sqlalchemy.dialects.mysql import LONGTEXT
from sqlalchemy.ext.compiler import compiles
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from crud import chat as crud_chat
from database import session as db_session
from database.session import Base
from model.models import ChatAttachmentUpload, ChatTraceSession, Conversation, Message, RevokedToken, User
from router import chat as chat_router
from schema.schemas import ChatRequest
from service import auth_service, chat_service, oss_service


OSS_CONFIG = {
    "OSS_ACCESS_KEY_ID": "test-id",
    "OSS_ACCESS_KEY_SECRET": "test-secret",
    "OSS_BUCKET": "demo",
    "OSS_ENDPOINT": "https://oss-cn-hangzhou.aliyuncs.com",
}
OSS_HOST = "demo.oss-cn-hangzhou.aliyuncs.com"

# 接口契约值写字面量，不从实现里读：实现把窗口/租约/墓碑保留期改小或改没时，用例要跟着
# 变红，而不是跟着一起「通过」。
TTL_SECONDS = 24 * 60 * 60
LEASE_SECONDS = 300
TOMBSTONE_TTL_SECONDS = 7 * 24 * 60 * 60

# 清扫链路用的对象键写字面量，形态照抄上传路径实际铸出来的样子
# （rag-chat/<年>/<月>/<日>/<uuid4().hex><扩展名>）。
UPLOAD_A = "rag-chat/2026/09/21/aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa.png"
UPLOAD_B = "rag-chat/2026/09/21/bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb.jpg"
UPLOAD_C = "rag-chat/2026/09/21/cccccccccccccccccccccccccccccccc.webp"
UPLOAD_NO_ROW = "rag-chat/2026/09/21/dddddddddddddddddddddddddddddddd.png"

# 本服务从没铸过的键（客户端回带的附件列是自由 JSON，桶里别的东西都可能出现在这个位置）。
FOREIGN_UPLOAD = "finance-archive/2026/q3/payroll.sql"

# 已经退役的两个辅助函数：行状态压在三个列上之后，「删行 + 按原时间戳重插」这条路径整个
# 不需要了（#233 让发送不再删行，这里的墓碑让清扫也不再删行）。
RETIRED_HELPERS = ("restore_attachment_upload", "drop_attachment_upload")


@compiles(LONGTEXT, "sqlite")
def _compile_longtext_as_text(_type, _compiler, **_kwargs):
    return "TEXT"


class _FakeResponse:
    def __init__(self, status_code, text=""):
        self.status_code = status_code
        self.text = text


class _RecordingOssClient:
    """对象存储出口的统一替身：请求按方法记全，所以「对象真被删了」是请求级证据。

    替身若把被测函数整个换掉，就断不出「请求根本没发出去」——这正是本 issue 要钉的东西。
    """

    def __init__(self):
        self.requests = []
        self.status_by_key = {}
        self.default_put_status = 200
        self.default_delete_status = 204

    def __enter__(self):
        return self

    def __exit__(self, *_exc):
        return False

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_exc):
        return False

    def _status_for(self, url, fallback):
        for upload, code in self.status_by_key.items():
            if upload in url:
                return code
        return fallback

    async def put(self, url, content=None, headers=None, **_kwargs):
        self.requests.append({"method": "PUT", "url": url, "headers": dict(headers or {})})
        status = self._status_for(url, self.default_put_status)
        return _FakeResponse(status, "InternalError" if status >= 400 else "")

    def delete(self, url, headers=None, **_kwargs):
        self.requests.append({"method": "DELETE", "url": url, "headers": dict(headers or {})})
        status = self._status_for(url, self.default_delete_status)
        return _FakeResponse(status, "AccessDenied" if status >= 400 else "")

    def urls(self, method):
        return [request["url"] for request in self.requests if request["method"] == method]


@pytest.fixture()
def oss_requests(monkeypatch):
    """替换 OSS 的两个 HTTP 出口；同时补齐 OSS 配置。"""
    client = _RecordingOssClient()
    for name, value in OSS_CONFIG.items():
        monkeypatch.setattr(oss_service, name, value)
    monkeypatch.setattr(oss_service.httpx, "AsyncClient", lambda **_kwargs: client)
    monkeypatch.setattr(oss_service.httpx, "Client", lambda **_kwargs: client)
    return client


@pytest.fixture()
def api(monkeypatch):
    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(
        bind=engine,
        tables=[
            User.__table__, RevokedToken.__table__,
            Conversation.__table__,
            Message.__table__,
            ChatTraceSession.__table__,
            ChatAttachmentUpload.__table__,
        ],
    )
    # 会话配置照抄生产的 database.session.SessionLocal（注意 expire_on_commit 用**默认的 True**）：
    # 写成 expire_on_commit=False 的话，commit 之后读 ORM 行属性就不会触发刷新，「另一条
    # 会话在这个窗口里改了同一行」这类缺陷会被这个更宽松的替身悄悄盖掉。
    db = sessionmaker(bind=engine, autoflush=False, autocommit=False)()

    alice = User(username="alice", password_hash="x")
    bob = User(username="bob", password_hash="x")
    db.add_all([alice, bob])
    db.commit()
    # 主键先取成普通 int：读 alice/bob 属性的地方可能发生在一次 commit 之后，那时会话里的
    # 实例已过期，现读会触发属性刷新，没必要让用例依赖会话还开着。
    alice_id, bob_id = alice.id, bob.id

    checkpointer_calls = []
    monkeypatch.setattr(chat_service, "delete_thread_checkpoints", checkpointer_calls.append)

    app = FastAPI()
    app.include_router(chat_router.router)
    app.dependency_overrides[db_session.get_db] = lambda: db
    app.dependency_overrides[auth_service.get_current_user] = lambda: db.get(User, alice_id)

    try:
        yield SimpleNamespace(
            client=TestClient(app),
            db=db,
            engine=engine,
            alice=alice,
            alice_id=alice_id,
            bob=bob,
            bob_id=bob_id,
            checkpointer_calls=checkpointer_calls,
        )
    finally:
        db.close()


# ---------------------------------------------------------------------------
# 摆条件的工具：直接落行、直接改状态列、另开一个会话
# ---------------------------------------------------------------------------

def _row_kwargs(api, *, user_id=None, created_at=None):
    return {
        "user_id": api.alice_id if user_id is None else user_id,
        "created_at": datetime.now() if created_at is None else created_at,
    }


def _add_row(api, object_key, *, user_id=None, created_at=None,
             claimed_at=None, reclaimed_at=None, consumed_at=None):
    """直接落一条指定状态的行：把墓碑 / 租约 / 已消费这些条件摆出来，不经过被测路径。"""
    row = ChatAttachmentUpload(
        object_key=object_key,
        claimed_at=claimed_at,
        reclaimed_at=reclaimed_at,
        consumed_at=consumed_at,
        **_row_kwargs(api, user_id=user_id, created_at=created_at),
    )
    api.db.add(row)
    api.db.commit()
    return row


def _add_pending_row(api, object_key, *, age_seconds=0, user_id=None):
    """落一条 pending 行，用来把「上传很久以前发生」这个时间条件摆出来。"""
    return _add_row(
        api,
        object_key,
        user_id=user_id,
        created_at=datetime.now() - timedelta(seconds=age_seconds),
    )


def _set_columns(api, object_key, **values):
    """直接改写行上的状态列（不经任何被测函数），用来把某个时间条件摆出来。"""
    api.db.execute(
        update(ChatAttachmentUpload)
        .where(ChatAttachmentUpload.object_key == object_key)
        .values(**values)
    )
    api.db.commit()


def _state(api, object_key):
    """把这一行的四列读成一份普通值快照；返回 None 表示这一行已经不在了。

    每次现查：会话是 expire_on_commit=True 的生产口径，提交之后 ORM 实例上的旧属性不再
    可信，用例要读的是此刻库里的值。
    """
    row = (
        api.db.query(ChatAttachmentUpload)
        .filter(ChatAttachmentUpload.object_key == object_key)
        .first()
    )
    if row is None:
        return None
    return SimpleNamespace(
        created_at=row.created_at,
        claimed_at=row.claimed_at,
        reclaimed_at=row.reclaimed_at,
        consumed_at=row.consumed_at,
    )


def _pending_rows(api):
    return crud_chat.list_pending_attachment_uploads(api.db)


def _new_session(api):
    """在同一引擎上另开一个会话：生产里发送与清扫各持一个会话，不是同一个。"""
    return sessionmaker(bind=api.db.get_bind(), autoflush=False, autocommit=False)()


def _send_once(api, cid, object_key, *, user_id=None):
    """走一次真实发送路径的落库段：返回落库消息的 id；被拒时抛 AttachmentReclaimedError。

    这是 `stream_chat` 里那段真实的落库逻辑（`_save_user_message`），拒签判定（消费的
    rowcount + 同事务的墓碑分类）全在里面。`stream_chat` 另外那层「只处理本服务铸过的键」
    的过滤在它上游，与这里的判据无关。
    """
    return chat_service._save_user_message(
        cid, api.alice_id if user_id is None else user_id, "这张图是什么",
        [{"object_key": object_key, "name": "a.png"}],
    )


def _install_pausable_delete(monkeypatch, step):
    """把 `_delete_oss_object` 换成「可暂停 / 可失败」的同一个钩子，返回它记下的异常。

    钩子先执行用例给的步骤（在这一次外呼进行中发送、回拨租约……），再原样调用真实的
    `_delete_oss_object`——替身若把删除整个短路掉，「对象已删」就没有请求级证据了。
    步骤里抛异常时就等价于「外呼失败」：真实删除不再发生。
    """
    real_delete = chat_service._delete_oss_object
    errors = []

    def hook(object_key):
        try:
            step(object_key)
        except BaseException as exc:  # noqa: BLE001 - 记下来再原样抛出，交给清扫的失败分支
            errors.append(exc)
            raise
        return real_delete(object_key)

    monkeypatch.setattr(chat_service, "_delete_oss_object", hook)
    return errors


def _reraise_step_errors(errors):
    """步骤里的断言炸了时，把原始异常抛出去。

    不清掉它的话，「回调里的断言失败」会被清扫的失败分支吞成 `failed=1`，用例接着在
    `failed == 1` 上「通过」——失败会被自己制造的条件盖掉。
    """
    if errors:
        raise errors[0]


def _keys_referenced_by_messages(api) -> set[str]:
    """库里所有消息引用到的对象键。"""
    keys = set()
    for (payload,) in api.db.query(Message.attachments).all():
        try:
            items = json.loads(payload or "[]")
        except (TypeError, ValueError):
            continue
        for item in items or []:
            if isinstance(item, dict) and isinstance(item.get("object_key"), str):
                keys.add(item["object_key"])
    return keys


def _deleted_keys(oss_requests) -> set[str]:
    prefix = f"https://{OSS_HOST}/"
    return {url[len(prefix):] for url in oss_requests.urls("DELETE") if url.startswith(prefix)}


async def _collect_stream(iterator):
    chunks = []
    async for chunk in iterator:
        chunks.append(chunk.decode() if isinstance(chunk, bytes) else chunk)
    return "".join(chunks)


def _add_conversation(api, cid):
    api.db.add(Conversation(id=cid, user_id=api.alice_id, title=f"会话-{cid}"))
    api.db.commit()


# ---------------------------------------------------------------------------
# (a) 清扫赢下这一行时，发送必须被拒，且对象已被删
# ---------------------------------------------------------------------------

def test_the_sweep_wins_the_race_against_a_send(api, oss_requests, monkeypatch):
    """T1：清扫在这一次外呼进行中赢下这一行 ⇒ 发送必须被拒、消息零新增、DELETE 恰好一条。

    这是墓碑前移的全部意义所在。修复前（先删行、后删对象）这里会落库一条消息，而对象已经
    被删掉——对象存储没有回收站，那是不可逆的内容丢失。
    """
    upload = UPLOAD_A
    cid = "conv-sweep-wins"
    _add_conversation(api, cid)
    _add_pending_row(api, upload, age_seconds=TTL_SECONDS + 60)
    monkeypatch.setattr(chat_service, "SessionLocal", lambda: _new_session(api))

    def step(object_key):
        assert object_key == upload, f"清扫去处理了别的键：{object_key}"
        # 外呼还没返回，此刻另一条会话上走一次发送：墓碑已经提交，必须拒签。
        with pytest.raises(chat_service.AttachmentReclaimedError):
            _send_once(api, cid, upload)

    errors = _install_pausable_delete(monkeypatch, step)
    report = chat_service.reclaim_orphan_chat_attachments(api.db, now=datetime.now())
    _reraise_step_errors(errors)

    assert report == {"candidates": 1, "reclaimed": 1, "failed": 0, "unclaimable": 0, "skipped": 0}
    # 判据是 DELETE 请求本身：危害是「对象留在桶里」，不是「断了引用」。
    assert oss_requests.urls("DELETE") == [f"https://{OSS_HOST}/{upload}"]
    assert api.db.query(Message).count() == 0, "被拒的发送仍然把消息写进了库"
    state = _state(api, upload)
    assert state is not None and state.reclaimed_at is not None, "删除成功之后墓碑必须留下"
    assert state.claimed_at is None, "这一次领取已经用完，租约必须清掉"
    assert state.consumed_at is None


def test_a_settled_tombstone_refuses_a_later_send(api, monkeypatch):
    """T2：墓碑已经落定（删除早就成功了）⇒ 之后任何一次发送都被拒。

    与 T3 相对：被拒的原因必须是「有墓碑」，不是「清扫没扫到」。少了这一条，把拒签条件
    写反成「必须正在删除中」也照样通过。
    """
    upload = UPLOAD_A
    cid = "conv-settled-tombstone"
    _add_conversation(api, cid)
    _add_row(api, upload, reclaimed_at=datetime.now() - timedelta(days=1))
    monkeypatch.setattr(chat_service, "SessionLocal", lambda: _new_session(api))

    with pytest.raises(chat_service.AttachmentReclaimedError):
        _send_once(api, cid, upload)

    assert api.db.query(Message).count() == 0
    state = _state(api, upload)
    assert state is not None and state.consumed_at is None, "被拒的发送不该消费掉这一行"


def test_a_key_with_no_row_is_not_refused(api, monkeypatch):
    """T3（I9 回归）：库里根本没有这一行的键照常发送，且整条流里没有错误帧。

    早于 #142 的键本来就没有登记行，消费不到是正常的。拒签面一旦铺到「消费不到就拒」，
    线上所有历史消息都会被拒——这条用例锁的是修复不得扩大拒签面。
    """
    attachments = [{"object_key": UPLOAD_NO_ROW, "name": "a.png"}]
    assert _state(api, UPLOAD_NO_ROW) is None

    cid, text = _run_stream_chat_text(api, monkeypatch, attachments)

    assert '"type": "error"' not in text, f"无行的键被拒了：{text}"
    assert text.rstrip().endswith("data: [DONE]")
    referenced = _keys_referenced_by_messages(api)
    assert referenced == {UPLOAD_NO_ROW}
    assert [m.role for m in api.db.query(Message).filter_by(conversation_id=cid).all()] == [
        "user",
        "assistant",
    ]


# ---------------------------------------------------------------------------
# (b) 墓碑不可消费；租约超时不构成放行
# ---------------------------------------------------------------------------

def test_a_tombstone_cannot_be_consumed(api):
    """T4：`confirm` 对墓碑行的 rowcount 恒为 0，且分类器能把它认出来。

    两种形态都要试：正在删除（租约未到期）与已经删成（只剩墓碑）。第一条钉「租约不是
    通行证」，第二条钉「删除成功不等于放行」。
    """
    deleting = UPLOAD_A
    settled = UPLOAD_B
    _add_row(api, deleting, claimed_at=datetime.now(), reclaimed_at=datetime.now())
    _add_row(api, settled, reclaimed_at=datetime.now() - timedelta(days=1))

    for upload in (deleting, settled):
        assert crud_chat.confirm_attachment_uploads(api.db, [upload], api.alice_id) == 0
        assert crud_chat.list_blocked_attachment_uploads(api.db, [upload]) == [upload]

    # `confirm` 不自己提交：回滚之后这一行必须是原样（墓碑还在、消费列仍空）。
    api.db.rollback()
    for upload in (deleting, settled):
        state = _state(api, upload)
        assert state is not None and state.consumed_at is None
        assert state.reclaimed_at is not None


def test_lease_timeout_still_refuses_the_send_but_re_arms_the_sweep(api, oss_requests, monkeypatch):
    """T5（I13）：租约超时**不放行**发送，但允许下一轮清扫重领并重试删除。

    租约只说明「上一次删除外呼没做完」，不说明「这个对象还在」。把它当成放行条，正好是
    这次修复要消灭的误判：外呼超时但服务端其实已经删掉了对象，放行就会落库一条引用死对象的
    消息。重试的方向只能是「再删一次」，不能是「放行一次」。
    """
    upload = UPLOAD_A
    cid = "conv-lease-timeout"
    _add_conversation(api, cid)
    # 租约早已过期（上一轮清扫死在了外呼里），墓碑还在。
    _add_row(
        api,
        upload,
        claimed_at=datetime.now() - timedelta(seconds=LEASE_SECONDS + 60),
        reclaimed_at=datetime.now() - timedelta(seconds=10),
    )
    monkeypatch.setattr(chat_service, "SessionLocal", lambda: _new_session(api))

    with pytest.raises(chat_service.AttachmentReclaimedError):
        _send_once(api, cid, upload)
    assert api.db.query(Message).count() == 0

    report = chat_service.reclaim_orphan_chat_attachments(api.db, now=datetime.now())

    assert report == {"candidates": 1, "reclaimed": 1, "failed": 0, "unclaimable": 0, "skipped": 0}
    assert oss_requests.urls("DELETE") == [f"https://{OSS_HOST}/{upload}"], "超期租约没有被重领重试"
    state = _state(api, upload)
    assert state is not None and state.reclaimed_at is not None, "重试之后墓碑仍然在（I12）"
    assert state.claimed_at is None


# ---------------------------------------------------------------------------
# (c) 外呼失败：双清回 pending，不 re-arm，也不算回收
# ---------------------------------------------------------------------------

def test_a_failed_delete_returns_the_row_to_pending_without_re_arming(api, oss_requests, monkeypatch):
    """T6：外呼失败 ⇒ 双清回 pending（不是留在 deleting 等租约过期），且不 re-arm。

    留在 deleting 的话，这一行要等满一个租约才有下一次机会，而它其实什么都没做成；更糟的是
    把失败当成功结算，那条键的发送会被永久拒掉。`created_at` 逐字节不动：重跑的方向是
    「下一轮再试」，不是把这一行排到队尾。
    """
    upload = UPLOAD_A
    created = datetime.now() - timedelta(seconds=TTL_SECONDS + 60)
    _add_row(api, upload, created_at=created)

    def step(object_key):
        assert object_key == upload
        raise ConnectionError("oss delete timed out")

    _install_pausable_delete(monkeypatch, step)
    report = chat_service.reclaim_orphan_chat_attachments(api.db, now=datetime.now())

    assert report == {"candidates": 1, "reclaimed": 0, "failed": 1, "unclaimable": 0, "skipped": 0}
    assert oss_requests.urls("DELETE") == []
    state = _state(api, upload)
    assert state is not None
    assert state.reclaimed_at is None, "失败被当成回收结算了：这把键的发送会被永久拒掉"
    assert state.claimed_at is None, "失败之后留在 deleting：要白等一个租约才有下一次机会"
    assert state.consumed_at is None
    assert state.created_at == created, "双清重写了 created_at：这一行被排到了队尾"
    # 回到 pending 的直接证据：下一轮它又是候选，且不需要等租约。
    again = chat_service.reclaim_orphan_chat_attachments(api.db, now=datetime.now())
    assert again["candidates"] == 1


# ---------------------------------------------------------------------------
# (d) 退役的两个辅助函数：符号已经不在，调用点也必须清零
# ---------------------------------------------------------------------------

def test_the_retired_helpers_have_no_call_sites_left(api):
    """T7：`restore_attachment_upload` / `drop_attachment_upload` 从 `crud.chat` 退役。

    只断言「属性不在了」不够：调用点若还在别处，改名之后会变成运行时的 AttributeError。
    全仓按**语法**找一遍（注释与 docstring 不算——它们只是说明，不是调用）。
    """
    for name in RETIRED_HELPERS:
        assert not hasattr(crud_chat, name), f"{name} 还在 crud.chat 上"

    assert _call_sites(RETIRED_HELPERS) == []


def _call_sites(names) -> list[str]:
    root = Path(__file__).resolve().parents[1]
    skip = {
        ".git", ".venv", "node_modules", "__pycache__", ".mypy_cache",
        ".pytest_cache", "dist", "build",
    }
    hits = []
    for path in sorted(root.rglob("*.py")):
        if skip & set(path.parts):
            continue
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            found = None
            if isinstance(node, ast.Attribute) and node.attr in names:
                found = node.attr
            elif isinstance(node, ast.Name) and node.id in names:
                found = node.id
            elif isinstance(node, ast.ImportFrom):
                for alias in node.names:
                    imported = alias.asname or alias.name
                    if imported in names:
                        found = imported
                        break
            if found:
                hits.append(f"{path.relative_to(root)}:{node.lineno} {found}")
    return hits


# ---------------------------------------------------------------------------
# (e) 墓碑 GC：只吃「已结算且过期」的墓碑
# ---------------------------------------------------------------------------

def test_the_tombstone_gc_reclaims_only_settled_and_expired_rows(api, oss_requests):
    """T8：GC 删超期的**已结算**墓碑；未超期的不删；仍在 deleting 的即便墓碑超期也不删。

    最后一条是 A1，不是防御性代码：清扫每轮重领都会刷新 `claimed_at`，一个反复失败的墓碑
    可以让 `reclaimed_at` 老过保留期而 `claimed_at` 还新鲜——只按 `reclaimed_at` 判，就会把
    一个正在删除中的行的墓碑 GC 掉，那把键的发送从此失去拦阻。
    """
    expired = UPLOAD_A
    fresh = UPLOAD_B
    deleting = UPLOAD_C
    now = datetime.now()
    _add_row(api, expired, created_at=now - timedelta(days=30),
             reclaimed_at=now - timedelta(seconds=TOMBSTONE_TTL_SECONDS + 60))
    _add_row(api, fresh, created_at=now - timedelta(days=30),
             reclaimed_at=now - timedelta(seconds=60))
    # 墓碑早已超期，但租约还在有效期内：此刻可能正在外呼，绝不能碰。
    _add_row(api, deleting, created_at=now - timedelta(days=30),
             claimed_at=now - timedelta(seconds=60),
             reclaimed_at=now - timedelta(seconds=TOMBSTONE_TTL_SECONDS + 60))

    report = chat_service.reclaim_orphan_chat_attachments(api.db, now=now)

    # 三行都不是候选（两路候选按定义排除墓碑），本轮只可能动 GC。
    assert report == {"candidates": 0, "reclaimed": 0, "failed": 0, "unclaimable": 0, "skipped": 0}
    assert oss_requests.urls("DELETE") == [], "GC 不该走对象存储"
    assert _state(api, expired) is None, "超期且已结算的墓碑没有被回收"
    assert _state(api, fresh) is not None, "未超期的墓碑被提前回收了"
    assert _state(api, deleting) is not None, "正在删除中的行被 GC 吃掉了（A1 反面）"


def test_the_tombstone_gc_off_switch_keeps_every_tombstone(api, oss_requests, monkeypatch):
    """T8 续：`CHAT_ATTACHMENT_TOMBSTONE_TTL_SECONDS = 0` ⇒ 一行都不删。

    墓碑是拒签的唯一凭据，删早了就是「被删对象的键又能发出去了」。留一个总开关，出问题时
    可以先把回收停掉，代价只是行留着。
    """
    upload = UPLOAD_A
    now = datetime.now()
    _add_row(api, upload, created_at=now - timedelta(days=30),
             reclaimed_at=now - timedelta(seconds=TOMBSTONE_TTL_SECONDS + 60))
    monkeypatch.setattr(chat_service, "CHAT_ATTACHMENT_TOMBSTONE_TTL_SECONDS", 0)

    report = chat_service.reclaim_orphan_chat_attachments(api.db, now=now)

    assert report["candidates"] == 0
    assert _state(api, upload) is not None, "开关置 0 之后 GC 仍然删了墓碑"


def test_a_settled_tombstone_is_never_a_pending_candidate(api, oss_requests):
    """T9：墓碑不参选 pending 候选——哪怕它的 `created_at` 早就过了保留窗口。

    这是 P 判据里 `reclaimed_at IS NULL` 的正面证明。少了它，两行老墓碑会被当成普通待回收
    行，再签一次 DELETE——而那条键的发送已经被拒，重删毫无意义，还会把批次名额吃掉。
    """
    now = datetime.now()
    for upload in (UPLOAD_A, UPLOAD_B):
        _add_row(api, upload, created_at=now - timedelta(seconds=TTL_SECONDS + 60),
                 reclaimed_at=now - timedelta(seconds=60))

    report = chat_service.reclaim_orphan_chat_attachments(api.db, now=now)

    assert report == {"candidates": 0, "reclaimed": 0, "failed": 0, "unclaimable": 0, "skipped": 0}
    assert _pending_rows(api) == []
    assert oss_requests.urls("DELETE") == []
    assert _state(api, UPLOAD_A) is not None and _state(api, UPLOAD_B) is not None


def test_no_message_can_reference_an_object_the_sweep_deleted(api, oss_requests, monkeypatch):
    """T10（I7）：不存在「消息已落库 ∧ 该消息引用的对象已被清扫删掉」的静默终态。

    构造一段真实历史：一把键被正常发送消费掉（对象必须留着），另一把从没被发送（对象该删）。
    判据写成两条集合的交集为空，而不是逐个键对答案——它对将来新增的键形态同样成立。
    """
    orphan = UPLOAD_A
    used = UPLOAD_B
    cid = "conv-no-silent-terminal-state"
    _add_conversation(api, cid)
    monkeypatch.setattr(chat_service, "SessionLocal", lambda: _new_session(api))
    _add_pending_row(api, used, age_seconds=TTL_SECONDS + 60)
    _send_once(api, cid, used)
    _add_pending_row(api, orphan, age_seconds=TTL_SECONDS + 60)

    report = chat_service.reclaim_orphan_chat_attachments(api.db, now=datetime.now())

    assert report == {"candidates": 1, "reclaimed": 1, "failed": 0, "unclaimable": 0, "skipped": 0}
    referenced = _keys_referenced_by_messages(api)
    assert referenced == {used}, f"落库的消息引用了意料之外的键：{referenced}"
    deleted = _deleted_keys(oss_requests)
    assert deleted == {orphan}, f"删除面铺到了不该删的键上：{deleted}"
    assert referenced & deleted == set(), "落库的消息引用了一个已经被删掉的对象"
    state = _state(api, used)
    assert state is not None and state.consumed_at is not None, "被发送引用过的行不该被清扫"


# ---------------------------------------------------------------------------
# (f) 外来键：墓碑化但不进 DELETE；跨用户墓碑同样拦
# ---------------------------------------------------------------------------

def test_a_foreign_key_is_tombstoned_and_never_deleted(api, oss_requests, monkeypatch):
    """T11：本服务没铸过的键 ⇒ 不签发 DELETE、行墓碑化、`unclaimable == 1`。

    墓碑留着的意义在这一条上最直白：旧代码把这种行**删掉**，于是「这把键该被拒」这个判断
    跟着行一起消失了。留着，才有东西可拒。
    """
    _add_pending_row(api, FOREIGN_UPLOAD, age_seconds=TTL_SECONDS + 60)

    report = chat_service.reclaim_orphan_chat_attachments(api.db, now=datetime.now())

    assert report == {"candidates": 1, "reclaimed": 0, "failed": 0, "unclaimable": 1, "skipped": 0}
    assert oss_requests.urls("DELETE") == [], "为一把没铸过的键签发了带服务端凭据的 DELETE"
    state = _state(api, FOREIGN_UPLOAD)
    assert state is not None, "签不出 DELETE 的行被删掉了：这把键从此可以照发"
    assert state.reclaimed_at is not None and state.claimed_at is None
    assert state.consumed_at is None

    # 发送侧同样拦：拒签判据落在键域上，与这把键是谁铸的、谁的会话引用它都无关。
    cid = "conv-foreign-key"
    _add_conversation(api, cid)
    monkeypatch.setattr(chat_service, "SessionLocal", lambda: _new_session(api))
    with pytest.raises(chat_service.AttachmentReclaimedError):
        _send_once(api, cid, FOREIGN_UPLOAD)


def test_a_send_referencing_another_users_tombstone_is_refused(api, oss_requests, monkeypatch):
    """T14：跨用户的墓碑必须拦（M4/I7），对照组证明拦的是墓碑而不是「不是我的行」。

    归属不是放行条：对象存储没有回收站，一把别人铸、正在被回收的键，我照样发不出去。
    对照组同样重要——同一把键在 pending 时不被拦，否则「所有别人的键都拒」也能通过，
    而那会把跨用户引用合法附件的用法一并打死。
    """
    upload = UPLOAD_A
    cid = "conv-cross-user"
    _add_conversation(api, cid)
    monkeypatch.setattr(chat_service, "SessionLocal", lambda: _new_session(api))

    _add_row(api, upload, user_id=api.bob_id, reclaimed_at=datetime.now())
    with pytest.raises(chat_service.AttachmentReclaimedError):
        _send_once(api, cid, upload, user_id=api.alice_id)
    assert api.db.query(Message).count() == 0

    # 对照组：同一把键、同属 bob，但没有墓碑 ⇒ alice 引用它照常发送。
    _set_columns(api, upload, reclaimed_at=None, claimed_at=None)
    message_id = _send_once(api, cid, upload, user_id=api.alice_id)
    assert message_id is not None
    state = _state(api, upload)
    assert state is not None
    assert state.consumed_at is None, "消费按 user_id 过滤，别人的行不该被我消费掉"
    assert state.reclaimed_at is None


# ---------------------------------------------------------------------------
# (g) 契约：领取钩子的位置签名、判定只看 rowcount
# ---------------------------------------------------------------------------

def test_the_claim_hook_keeps_its_positional_signature(api, oss_requests, monkeypatch):
    """T12（I10）：`claim_attachment_upload(db, object_key, older_than)` 的位置签名不许变。

    清扫按位置调用它，测试与运维脚本也按位置猴补它；签名一变，猴补会在运行时以 TypeError
    炸掉，而且只在真跑清扫时才炸。
    """
    real_claim = crud_chat.claim_attachment_upload
    assert list(py_inspect.signature(real_claim).parameters)[:3] == [
        "db", "object_key", "older_than",
    ]

    calls = []

    def claim_positionally(db, object_key, older_than):
        calls.append((object_key, older_than))
        return real_claim(db, object_key, older_than)

    monkeypatch.setattr(crud_chat, "claim_attachment_upload", claim_positionally)
    _add_pending_row(api, UPLOAD_A, age_seconds=TTL_SECONDS + 60)

    report = chat_service.reclaim_orphan_chat_attachments(api.db, now=datetime.now())

    assert [upload for upload, _ in calls] == [UPLOAD_A]
    assert report["reclaimed"] == 1
    assert oss_requests.urls("DELETE") == [f"https://{OSS_HOST}/{UPLOAD_A}"]


def test_the_consume_rowcount_tells_the_three_states_apart(api):
    """D1-A：`confirm` 的 rowcount 是唯一的判定信号——pending 1、墓碑 0、无行 0。

    它必须把「墓碑挡住了」与「本来就没有这一行」区分开（后者由分类器接管），所以这里
    连着三种形态一起断。
    """
    cid = "conv-rowcount"
    _add_conversation(api, cid)
    pending = UPLOAD_A
    tombstoned = UPLOAD_B
    _add_pending_row(api, pending)
    _add_row(api, tombstoned, reclaimed_at=datetime.now())

    assert crud_chat.confirm_attachment_uploads(api.db, [pending], api.alice_id) == 1
    assert crud_chat.confirm_attachment_uploads(api.db, [tombstoned], api.alice_id) == 0
    assert crud_chat.confirm_attachment_uploads(api.db, [UPLOAD_NO_ROW], api.alice_id) == 0
    # 同一个键再来一次也是 0：消费是幂等的，不会数出第二行。
    assert crud_chat.confirm_attachment_uploads(api.db, [pending], api.alice_id) == 0

    # 分类器只认墓碑的那一个：pending 与无行都不算「被挡」。分类不提交。
    assert crud_chat.list_blocked_attachment_uploads(api.db, [pending, tombstoned, UPLOAD_NO_ROW]) == [
        tombstoned
    ]
    api.db.rollback()
    assert _state(api, pending).consumed_at is None, "confirm 不该自己提交"
    assert _state(api, tombstoned).consumed_at is None


def test_the_refused_send_never_commits(api, monkeypatch):
    """D1-B：被拒的发送一次提交都没有发生——消费与消息行一起回滚。

    只看外部结果分不出「没提交」与「提交了又删掉」，而提交次数是直接可辨的。少了这一条，
    一个「先提交消息、再判拒签」的实现会留下一条引用死对象的孤儿消息。
    """
    upload = UPLOAD_A
    cid = "conv-no-commit"
    _add_conversation(api, cid)
    _add_row(api, upload, reclaimed_at=datetime.now())
    counting = _CountingSession(_new_session(api))
    monkeypatch.setattr(chat_service, "SessionLocal", lambda: counting)

    with pytest.raises(chat_service.AttachmentReclaimedError):
        _send_once(api, cid, upload)

    assert counting.commits == 0, "被拒的发送提交了事务：消息或消费被留下来了"
    assert api.db.query(Message).count() == 0
    assert _state(api, upload).consumed_at is None


def test_the_tombstone_classifier_only_runs_after_a_shortfall(api, monkeypatch):
    """D1-C：判据只能来自 rowcount，不许退化成「先查一遍再写」。

    读-再-写会在两次快照之间漏掉变化，而这里要挡的恰是「清扫刚刚赢下这一行」这种瞬时窗口。
    调用顺序是可直接观察的：消费没短少时，分类器根本不该被调用。
    """
    order = []
    real_confirm = crud_chat.confirm_attachment_uploads
    real_blocked = crud_chat.list_blocked_attachment_uploads

    def spy_confirm(db, object_keys, user_id):
        order.append("confirm")
        return real_confirm(db, object_keys, user_id)

    def spy_blocked(db, object_keys):
        order.append("blocked")
        return real_blocked(db, object_keys)

    monkeypatch.setattr(crud_chat, "confirm_attachment_uploads", spy_confirm)
    monkeypatch.setattr(crud_chat, "list_blocked_attachment_uploads", spy_blocked)
    monkeypatch.setattr(chat_service, "SessionLocal", lambda: _new_session(api))

    normal = "conv-classifier-normal"
    _add_conversation(api, normal)
    _add_pending_row(api, UPLOAD_A)
    _send_once(api, normal, UPLOAD_A)
    assert order == ["confirm"], f"没有短少也去查了墓碑：{order}"

    blocked = "conv-classifier-blocked"
    _add_conversation(api, blocked)
    _add_row(api, UPLOAD_B, reclaimed_at=datetime.now())
    with pytest.raises(chat_service.AttachmentReclaimedError):
        _send_once(api, blocked, UPLOAD_B)
    assert order == ["confirm", "confirm", "blocked"], f"分类器与消费的顺序不对：{order}"


# ---------------------------------------------------------------------------
# (h) 三条时间线（U1）：发送被拒必须发生在墓碑生效的时刻，而不是事后补判
# ---------------------------------------------------------------------------

def test_the_send_that_loses_the_race_is_refused_inside_the_delete_call(api, oss_requests, monkeypatch):
    """T13（U1）：在**外呼进行中**把租约回拨到超期，再发送 ⇒ 被拒；此后外呼成功。

    与 T1 的区别是这里额外回拨了租约：拒签必须只认墓碑（I13），不能因为「租约已经过期、
    看起来没人管了」而放行。终态三者不可同时成立：消息落库 ∧ 对象已删 ∧ 行记为已消费。
    """
    upload = UPLOAD_A
    cid = "conv-timeline-refused"
    _add_conversation(api, cid)
    _add_pending_row(api, upload, age_seconds=TTL_SECONDS + 60)
    monkeypatch.setattr(chat_service, "SessionLocal", lambda: _new_session(api))

    def step(object_key):
        # ① 回拨租约：这一行此刻看起来像「上一轮清扫死在半路、租约早已过期」。
        _set_columns(
            api, object_key,
            claimed_at=datetime.now() - timedelta(seconds=LEASE_SECONDS + 60),
        )
        # ② 另开一个会话走发送路径；③ 断言被拒。
        with pytest.raises(chat_service.AttachmentReclaimedError):
            _send_once(api, cid, upload)
        # ④ 回调正常返回 ⇒ 真实外呼照做、成功。

    errors = _install_pausable_delete(monkeypatch, step)
    report = chat_service.reclaim_orphan_chat_attachments(api.db, now=datetime.now())
    _reraise_step_errors(errors)

    assert report == {"candidates": 1, "reclaimed": 1, "failed": 0, "unclaimable": 0, "skipped": 0}
    assert oss_requests.urls("DELETE") == [f"https://{OSS_HOST}/{upload}"]
    assert api.db.query(Message).count() == 0
    state = _state(api, upload)
    assert state is not None
    assert not (state.consumed_at is not None and state.reclaimed_at is not None), (
        "同一行同时被记为已消费与已回收"
    )


def test_the_send_refusal_holds_when_the_delete_then_fails(api, oss_requests, monkeypatch):
    """T13b（U1）：同 T13，但外呼随后**抛异常**——被拒的断言必须发生在回调里。

    断言若挪到回调之外，这条用例就退化成 T6（只证明「失败会双清」），把「墓碑在失败路径上
    同样拦得住发送」这条漏掉。
    """
    upload = UPLOAD_A
    cid = "conv-timeline-failed-delete"
    _add_conversation(api, cid)
    _add_pending_row(api, upload, age_seconds=TTL_SECONDS + 60)
    monkeypatch.setattr(chat_service, "SessionLocal", lambda: _new_session(api))

    def step(object_key):
        _set_columns(
            api, object_key,
            claimed_at=datetime.now() - timedelta(seconds=LEASE_SECONDS + 60),
        )
        with pytest.raises(chat_service.AttachmentReclaimedError):
            _send_once(api, cid, upload)
        # ④ 外呼失败。
        raise ConnectionError("oss delete timed out")

    errors = _install_pausable_delete(monkeypatch, step)
    report = chat_service.reclaim_orphan_chat_attachments(api.db, now=datetime.now())

    # 回调里只允许出现那个**故意**抛出的外呼异常：别的异常说明被拒的断言根本没成立，
    # 而它会被清扫的失败分支吞成 failed=1，让下面那条断言「通过」。
    assert len(errors) == 1 and isinstance(errors[0], ConnectionError), errors
    assert report == {"candidates": 1, "reclaimed": 0, "failed": 1, "unclaimable": 0, "skipped": 0}
    assert oss_requests.urls("DELETE") == []
    assert api.db.query(Message).count() == 0


def test_the_row_returned_to_pending_after_a_failed_delete_is_sendable(api, oss_requests, monkeypatch):
    """T13c（U1）：承接 T13b 走完整轮清扫，**在轮次之外**发送 ⇒ 这次必须落库。

    双清的语义是「什么都没发生过」：墓碑撤了，这一行又是普通的待回收行。此时发送照常——
    若不落库，一次外呼失败就会永久毁掉一把好好的键。
    """
    upload = UPLOAD_A
    created = datetime.now() - timedelta(seconds=TTL_SECONDS + 60)
    _add_row(api, upload, created_at=created)
    monkeypatch.setattr(chat_service, "SessionLocal", lambda: _new_session(api))

    def step(object_key):
        _set_columns(api, object_key, claimed_at=datetime.now())
        raise ConnectionError("oss delete timed out")

    errors = _install_pausable_delete(monkeypatch, step)
    report = chat_service.reclaim_orphan_chat_attachments(api.db, now=datetime.now())

    # 先断言行确实回到了 pending（而不是留在 deleting 等租约）。
    assert len(errors) == 1 and isinstance(errors[0], ConnectionError), errors
    assert (report["failed"], report["reclaimed"]) == (1, 0)
    state = _state(api, upload)
    assert state is not None
    assert state.reclaimed_at is None and state.claimed_at is None
    assert state.created_at == created

    # 然后，**整轮清扫已经返回之后**，在另一条会话上发送：必须落库、不抛。
    seen_rowcounts = []
    real_confirm = crud_chat.confirm_attachment_uploads

    def spy_confirm(db, object_keys, user_id):
        rowcount = real_confirm(db, object_keys, user_id)
        seen_rowcounts.append(rowcount)
        return rowcount

    monkeypatch.setattr(crud_chat, "confirm_attachment_uploads", spy_confirm)
    _stub_stream_chat_dependencies(api, monkeypatch)
    monkeypatch.setattr(chat_service, "SessionLocal", lambda: _new_session(api))

    cid, text = _run_stream_chat_text(
        api, monkeypatch, [{"object_key": upload, "name": "a.png"}],
        session_factory=lambda: _new_session(api),
    )

    assert seen_rowcounts == [1], f"这条键没能被消费掉：rowcount={seen_rowcounts}"
    assert '"type": "error"' not in text, f"双清之后发送仍被拒：{text}"
    assert _keys_referenced_by_messages(api) == {upload}
    state = _state(api, upload)
    assert state is not None
    assert state.consumed_at is not None and state.reclaimed_at is None


# ---------------------------------------------------------------------------
# (i) 老库补列：三列都能加上，老行照读
# ---------------------------------------------------------------------------

def test_schema_column_migration_adds_the_tombstone_columns(monkeypatch):
    """§4.3：没有这三列的老库跑一次补列迁移之后，三列都在、老行可读、再跑一次幂等。

    直接调 `_ensure_schema_columns()` 而不是 `init_db()`：后者会连带建库、灌默认知识库，
    把「补列是否真的发生」这件事埋在一堆副作用里。engine 换成临时库——迁移函数读的是模块级
    engine，换掉它才能对着一个真的缺列的表跑。
    """
    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    with engine.begin() as conn:
        conn.execute(text(
            "CREATE TABLE chat_attachment_uploads ("
            "id INTEGER PRIMARY KEY, "
            "object_key VARCHAR(255) NOT NULL, "
            "user_id INTEGER NOT NULL, "
            "created_at DATETIME NOT NULL)"
        ))
        conn.execute(
            text(
                "INSERT INTO chat_attachment_uploads (id, object_key, user_id, created_at) "
                "VALUES (1, :key, 7, :created_at)"
            ),
            {"key": UPLOAD_A, "created_at": "2026-01-01 00:00:00"},
        )

    before = {column["name"] for column in inspect(engine).get_columns("chat_attachment_uploads")}
    assert before == {"id", "object_key", "user_id", "created_at"}, before

    monkeypatch.setattr(db_session, "engine", engine)
    db_session._ensure_schema_columns()

    after = {column["name"] for column in inspect(engine).get_columns("chat_attachment_uploads")}
    assert {"consumed_at", "claimed_at", "reclaimed_at"} <= after, after

    with engine.connect() as conn:
        row = conn.execute(
            text(
                "SELECT object_key, user_id, consumed_at, claimed_at, reclaimed_at "
                "FROM chat_attachment_uploads WHERE id = 1"
            )
        ).mappings().first()
    assert row["object_key"] == UPLOAD_A
    assert row["user_id"] == 7
    # 存量行落成「三列皆空」＝ pending，正是新代码眼里的「尚未被处理过的待回收行」。
    assert row["consumed_at"] is None and row["claimed_at"] is None and row["reclaimed_at"] is None

    # 幂等：再跑一次不报错，也不多出列。
    db_session._ensure_schema_columns()
    assert {
        column["name"] for column in inspect(engine).get_columns("chat_attachment_uploads")
    } == after


# ---------------------------------------------------------------------------
# 真实 stream_chat 的最小夹具（模型、检索、轨迹短路）
# ---------------------------------------------------------------------------

class _FakeChatTrace:
    def __init__(self, user_id=None):
        self.user_id = user_id
        self.trace_id = "trace-test"

    async def add(self, *_args, **_kwargs):
        pass

    async def attach(self, **_kwargs):
        pass

    async def finish(self, *_args, **_kwargs):
        pass

    def snapshot(self):
        return {"trace_id": self.trace_id, "events": []}


class _CountingSession:
    """包一层真实会话，只数 commit 次数，其余原样转发。"""

    def __init__(self, db):
        self._db = db
        self.commits = 0

    def __getattr__(self, name):
        return getattr(self._db, name)

    def commit(self):
        self.commits += 1
        return self._db.commit()


def _stub_stream_chat_dependencies(api, monkeypatch):
    """把模型、检索、轨迹三块短路掉，只留落库与附件消费这段真实逻辑。"""
    alice_username = api.alice.username

    async def fake_build_effective_question(question, attachments):
        return question, {"status": "skipped"}

    async def fake_recent_memory_text(*_args, **_kwargs):
        return ""

    async def fake_decide_need_rag(*_args, **_kwargs):
        return {"need_rag": False, "route": "direct", "confidence": 1.0, "reason": "test"}

    async def fake_stream_rag_answer(*_args, **_kwargs):
        yield "回答"

    monkeypatch.setattr(
        chat_service,
        "resolve_knowledge_base",
        lambda db, knowledge_base_id, user_id: SimpleNamespace(id=1, name="kb", user_id=user_id),
    )
    monkeypatch.setattr(chat_service, "SessionLocal", lambda: api.db)
    monkeypatch.setattr(
        chat_service,
        "authenticate",
        lambda db, authorization: db.query(User).filter_by(username=alice_username).first(),
    )
    monkeypatch.setattr(chat_service, "TraceRecorder", _FakeChatTrace)
    monkeypatch.setattr(chat_service, "_build_effective_question", fake_build_effective_question)
    monkeypatch.setattr(chat_service, "_build_recent_memory_text", fake_recent_memory_text)
    monkeypatch.setattr(chat_service, "_build_memory_context", lambda *args, **kwargs: "")
    monkeypatch.setattr(
        chat_service, "_build_memory_aware_retrieval_question", lambda question, memory_context: question
    )
    monkeypatch.setattr(chat_service, "decide_need_rag", fake_decide_need_rag)
    monkeypatch.setattr(chat_service, "stream_rag_answer", fake_stream_rag_answer)
    monkeypatch.setattr(chat_service, "_trace_sse_payloads", lambda trace: [])
    monkeypatch.setattr(chat_service, "_build_sources", lambda chunks: [])

    async def _noop_trace(*args, **kwargs):
        return None

    monkeypatch.setattr(chat_service, "_attach_grounding_trace", _noop_trace)
    monkeypatch.setattr(chat_service, "_safe_trace_attach", _noop_trace)
    monkeypatch.setattr(chat_service, "_safe_trace_finish", _noop_trace)
    monkeypatch.setattr(chat_service, "_schedule_memory_summary_update", lambda *args, **kwargs: None)
    monkeypatch.setattr(chat_service, "schedule_ragas_evaluation", lambda *args, **kwargs: None)


def _run_stream_chat_text(api, monkeypatch, attachments, session_factory=None):
    """跑一次真实聊天流，返回 (新建会话 id, SSE 文本)。"""
    _stub_stream_chat_dependencies(api, monkeypatch)
    if session_factory is not None:
        # 「在另一条会话上发送」：同一引擎、独立的 Session（生产里发送与清扫各持一个）。
        monkeypatch.setattr(chat_service, "SessionLocal", session_factory)

    # 用取好的整数主键而不是 api.alice.id：`stream_chat` 在流结束时会 close() 掉夹具借给它的
    # 那个会话实例，之后 api.alice 已经脱管，再读 .id 会抛 DetachedInstanceError。
    alice_id = api.alice_id

    def _conversation_ids():
        return {cid for (cid,) in api.db.query(Conversation.id).filter_by(user_id=alice_id).all()}

    before = _conversation_ids()
    response = asyncio.run(
        chat_service.stream_chat(
            ChatRequest(question="这张图是什么", attachments=attachments),
            authorization="Bearer token",
        )
    )
    text = asyncio.run(_collect_stream(response.body_iterator))

    created = _conversation_ids() - before
    assert len(created) == 1, f"这次聊天没有新建出唯一一个会话：{created}"
    return created.pop(), text
