"""issue #186：`/uploads` 匿名可读 + 头像文件名可枚举。

修复前 `main.py` 是 `app.mount("/uploads", StaticFiles(directory=UPLOAD_DIR))`——整棵目录
无鉴权，而文件名是 `user_{user.id}_{int(now.timestamp())}{ext}`。用户 id 是连续小整数、
时间戳是秒级，两个分量都可预测，所以别人的头像可以按「id × 时间窗」枚举取走
（issue 的探针在最近 60 秒的窗口里第 38 次就命中了）。

这里锁的是两条独立的东西，任何一条单独修都过不了这个文件：

- **读取面**：匿名（不带 `Authorization`）请求一个确实存在的头像路径不得返回 200；
- **命名面**：文件名不得再由 `user.id` 与时间戳推出来，旧模板枚举一个窗口必须全军覆没。

断言全部打在**路由层的响应状态码**上（验收第 4 条），并且跑在**真实的 `main.app`** 上，
不是测试里另搭一个小 app：这样「有人把挂载加回来」或「有人把路由摘掉」都会在这里变红。
匿名用例尤其如此——`Mount("/uploads", StaticFiles(...))` 会先匹配掉整个前缀，只要挂载回来，
`GET /uploads/avatars/<真实存在的那一个>` 就又是 200，本文件立刻红。

阳性对照（验收第 1 条要求「防止修复把功能一起挡掉」）：本人带有效 token 取自己的头像必须
200 且字节一致。授权判据是「请求的路径逐字等于请求方 `users.avatar` 列里的那一条」，因此
**旧命名的历史头像继续对其本人可读**（验收第 5 条选的是「保留兼容读取」，不搬迁、不改库）——
这一条也单独有用例钉住，否则「一刀切只认新文件名」的实现会把所有存量头像变成 404 而没人发现。
"""

from types import SimpleNamespace
from urllib.parse import quote

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

import main
from database import session as db_session
from database.session import Base
from model.models import User
from service import auth_service, user_service


PNG_BYTES = b"\x89PNG\r\n\x1a\n-alice-avatar-bytes"
VICTIM_BYTES = b"\x89PNG\r\n\x1a\n-victim-avatar-bytes"

# 枚举窗口：按旧模板造一个「一分钟内的某秒」，再把这个窗口整个扫一遍。
# 起点写成字面量而不是从实现里取，实现换了时间基准也不会让用例跟着一起「通过」。
VICTIM_TIMESTAMP = 1_700_000_038
ENUMERATION_WINDOW = 120

# 旧模板。用例故意写字面量，因为它正是要被证伪的那个假设。
LEGACY_TEMPLATE = "user_{user_id}_{timestamp}.png"


def _legacy_name(user_id: int, timestamp: int) -> str:
    return LEGACY_TEMPLATE.format(user_id=user_id, timestamp=timestamp)


def _avatar_path(filename: str) -> str:
    return f"/uploads/avatars/{filename}"


@pytest.fixture()
def api(monkeypatch, tmp_path):
    """真实 `main.app` + 内存库 + 临时头像目录；**不**替换 `get_current_user`。

    与 `test_attachment_lifecycle.py` 的 fixture 相比，这里刻意不覆盖依赖注入里的当前用户：
    本 issue 的核心正是「有没有身份、是不是本人」，把 `get_current_user` 换成常量就等于把待测
    的那一层整个短路掉，匿名用例会永远绿。token 走真实的 `create_token` / `_decode_token`。
    """
    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(bind=engine, tables=[User.__table__])
    db = sessionmaker(bind=engine, autoflush=False, autocommit=False, expire_on_commit=False)()

    alice = User(username="alice", password_hash="x")
    bob = User(username="bob", password_hash="x")
    db.add_all([alice, bob])
    db.commit()

    # 头像目录换成临时目录：写盘的是真实文件，但不碰仓库里的 backend/uploads/。
    avatar_dir = tmp_path / "avatars"
    avatar_dir.mkdir()
    monkeypatch.setattr(user_service, "AVATAR_DIR", avatar_dir)

    main.app.dependency_overrides[db_session.get_db] = lambda: db
    try:
        yield SimpleNamespace(
            client=TestClient(main.app),
            db=db,
            alice=alice,
            bob=bob,
            avatar_dir=avatar_dir,
            token_of=lambda user: auth_service.create_token(user.username),
        )
    finally:
        main.app.dependency_overrides.pop(db_session.get_db, None)
        db.close()


def _upload_avatar(api, payload: bytes, user=None, content_type="image/png"):
    return api.client.post(
        "/api/user/avatar",
        files={"file": ("avatar.png", payload, content_type)},
        headers=_auth(api, user if user is not None else api.alice),
    )


def _auth(api, user) -> dict:
    return {"Authorization": f"Bearer {api.token_of(user)}"}


def _plant(api, user, filename: str, payload: bytes) -> str:
    """按给定的文件名把一份头像直接落到盘上并写进 avatar 列，模拟修复前留下的存量数据。"""
    (api.avatar_dir / filename).write_bytes(payload)
    user.avatar = _avatar_path(filename)
    api.db.commit()
    return _avatar_path(filename)


# ---------------------------------------------------------------------------
# 验收 1：匿名请求已存在的头像路径不是 200；阳性对照是本人能取到
# ---------------------------------------------------------------------------

def test_anonymous_request_for_an_existing_avatar_is_not_200(api):
    """已存在的头像路径，不带 Authorization 头请求不得返回 200。"""
    avatar_path = _upload_avatar(api, PNG_BYTES).json()["avatar"]
    assert (api.avatar_dir / avatar_path.rsplit("/", 1)[-1]).read_bytes() == PNG_BYTES, (
        "前提不成立：文件没落到盘上，下面的 401 就说明不了任何事"
    )

    response = api.client.get(avatar_path)

    # 验收第 1 条要求的是「不是 200」；具体到实现是 401——缺 Authorization 头。
    assert response.status_code != 200
    assert response.status_code == 401
    assert PNG_BYTES not in response.content


def test_owner_with_a_valid_token_still_gets_their_own_avatar(api):
    """阳性对照：修复不得把功能一起挡掉——本人带有效 token 取自己的头像必须成功。"""
    avatar_path = _upload_avatar(api, PNG_BYTES).json()["avatar"]

    response = api.client.get(avatar_path, headers=_auth(api, api.alice))

    assert response.status_code == 200
    assert response.content == PNG_BYTES


def test_a_valid_token_for_someone_else_does_not_open_the_file(api):
    """身份有了但归属不对：另一个已登录用户拿不到别人的头像，且拿不到存在性。

    bob **自己也有一张头像**是有意的：只让 bob 空着手来问，一个「不比对请求路径、直接把自己
    名下那一个文件发回去」的实现也会返回 404（bob 的 avatar 列是空的），用例就绿在了错误的
    理由上——变异探针（去掉 `avatar_file_for_owner` 里那行逐字比对）实测就是这样：
    原版用例不变红。让 bob 先上传一张之后，那种实现会把 bob 自己的字节以 alice 的 URL 发出去，
    这里立刻红。断言里因此同时钉了「不是 404」与「发回来的不是请求方自己的那份」。
    """
    avatar_path = _upload_avatar(api, PNG_BYTES).json()["avatar"]
    bob_path = _upload_avatar(api, VICTIM_BYTES, user=api.bob).json()["avatar"]
    assert bob_path != avatar_path

    response = api.client.get(avatar_path, headers=_auth(api, api.bob))

    assert response.status_code == 404
    assert PNG_BYTES not in response.content
    assert VICTIM_BYTES not in response.content, "把请求方自己名下的头像当成被请求的那一份发了出去"

    # 阳性对照：同一次请求换成 bob 自己的路径就必须成功，否则上面的 404 可能只是「谁都取不到」。
    assert api.client.get(bob_path, headers=_auth(api, api.bob)).content == VICTIM_BYTES


def test_an_invalid_token_is_not_a_way_in(api):
    """乱填的 token 与匿名同档：不得因为「带了 Authorization 头」就放行。"""
    avatar_path = _upload_avatar(api, PNG_BYTES).json()["avatar"]

    response = api.client.get(avatar_path, headers={"Authorization": "Bearer not-a-real-token"})

    assert response.status_code == 401


# ---------------------------------------------------------------------------
# 验收 2：按 user_{id}_{ts} 模板枚举一个窗口，全部失败
# ---------------------------------------------------------------------------

def test_enumerating_the_legacy_template_window_never_yields_an_avatar(api):
    """按旧模板枚举最近一分钟的候选名：匿名全 401，换成另一个登录用户全 404。

    修复前这条会红：`StaticFiles` 对每个候选名直接读盘，窗口里那一个真实文件名返回 200 与
    原图字节（issue 的探针第 38 个命中）。所以这个用例是「命名不可预测」这条验收的正面证据，
    而不只是「新代码没崩」。
    """
    victim_path = _plant(api, api.bob, _legacy_name(api.bob.id, VICTIM_TIMESTAMP), VICTIM_BYTES)
    assert victim_path in {_avatar_path(_legacy_name(api.bob.id, ts)) for ts in _window()}, (
        "前提不成立：受害者文件名不在待枚举窗口里，扫过去全 404 是必然的、证明不了枚举被挡住"
    )

    anonymous_hits, alice_hits = [], []
    for timestamp in _window():
        candidate = _avatar_path(_legacy_name(api.bob.id, timestamp))
        if api.client.get(candidate).status_code != 401:
            anonymous_hits.append(candidate)
        if api.client.get(candidate, headers=_auth(api, api.alice)).status_code != 404:
            alice_hits.append(candidate)

    assert anonymous_hits == [], f"匿名枚举命中了 {anonymous_hits}"
    assert alice_hits == [], f"换个登录用户就枚举命中了 {alice_hits}"


def _window():
    return range(VICTIM_TIMESTAMP, VICTIM_TIMESTAMP + ENUMERATION_WINDOW)


def test_the_stored_filename_is_not_derivable_from_the_user_id_and_a_timestamp(api):
    """落库的文件名不得再由 `user.id` + 秒级时间戳推出来，且两次上传不能重名。"""
    first = _upload_avatar(api, b"first").json()["avatar"]
    second = _upload_avatar(api, b"second").json()["avatar"]

    first_name = first.rsplit("/", 1)[-1]
    second_name = second.rsplit("/", 1)[-1]

    # 与 OSS 侧对齐：uuid4().hex（32 位十六进制）+ 扩展名。
    stem, _, ext = first_name.rpartition(".")
    assert len(stem) == 32, first_name
    assert set(stem) <= set("0123456789abcdef"), first_name
    assert ext in {"png", "jpg", "jpeg", "webp"}, first_name

    # 正面证伪旧假设：把 `user.id` × 时间窗枚举出来的候选名整批拿来比，落库的那个不在里面。
    # 不写成 `assert str(user.id) not in name`——id=1 时那是个单字符，任何十六进制串都"包含"它，
    # 恒真的断言看起来在防这件事，其实什么都没防。
    legacy_candidates = {_legacy_name(api.alice.id, ts) for ts in _window()}
    assert first_name not in legacy_candidates
    assert not first_name.startswith("user_")

    assert first_name != second_name, "两次上传落到了同一个名字上，说明名字仍由某个确定性输入决定"


def test_the_legacy_path_shape_no_longer_resolves_for_the_uploader(api):
    """上传自己的新头像后，按旧模板猜自己的路径也取不到东西（新名字不是旧形状）。"""
    _upload_avatar(api, PNG_BYTES)

    response = api.client.get(
        _avatar_path(_legacy_name(api.alice.id, VICTIM_TIMESTAMP)),
        headers=_auth(api, api.alice),
    )

    assert response.status_code == 404


# ---------------------------------------------------------------------------
# 验收 5：迁移面——存量旧命名头像保留兼容读取（不搬迁、不改库）
# ---------------------------------------------------------------------------

def test_legacy_named_avatars_stay_readable_for_their_owner_only(api):
    """存量旧命名文件：本人可读，别人与匿名都不行。

    选定行为是「保留兼容读取」而不是一次性迁移：授权判据绑在 `users.avatar` 列上，与文件名
    形状无关，所以旧文件不需要改名也不需要改库就继续可用；而它们不再对匿名开放，正是本 issue
    要堵的那一面。一刀切只认新文件名（例如按 uuid4 形状过滤）会让所有存量头像 404——
    那是本条要挡住的回归。
    """
    legacy_path = _plant(api, api.alice, _legacy_name(api.alice.id, VICTIM_TIMESTAMP), VICTIM_BYTES)

    assert api.client.get(legacy_path, headers=_auth(api, api.alice)).content == VICTIM_BYTES
    assert api.client.get(legacy_path).status_code == 401
    assert api.client.get(legacy_path, headers=_auth(api, api.bob)).status_code == 404


def test_replacing_a_legacy_avatar_leaves_no_readable_trace_of_the_old_file(api):
    """换掉旧头像之后：新文件是随机键，旧文件在盘上消失、路径也取不到内容。

    与 `test_attachment_lifecycle.py` 的回收用例互相印证——那条断言的是「文件被删」，
    这条断言的是「删掉之后从路由上也拿不到」，即旧路径不会变成一个 200 的空壳。
    """
    legacy_path = _plant(api, api.alice, _legacy_name(api.alice.id, VICTIM_TIMESTAMP), VICTIM_BYTES)

    new_path = _upload_avatar(api, PNG_BYTES).json()["avatar"]

    assert new_path != legacy_path
    assert not (api.avatar_dir / legacy_path.rsplit("/", 1)[-1]).exists()
    assert api.client.get(legacy_path, headers=_auth(api, api.alice)).status_code == 404
    assert api.client.get(new_path, headers=_auth(api, api.alice)).content == PNG_BYTES


# ---------------------------------------------------------------------------
# 路由层护栏：路径形状与目录边界
# ---------------------------------------------------------------------------

def test_the_uploads_prefix_is_no_longer_a_static_mount(api):
    """`/uploads` 上不得再有匿名静态挂载；只有带鉴权的那一条路由。

    这条读的是应用自己的路由表，防的是「路由还在、但旁边又多挂了一个 StaticFiles」——
    那种写法下 Mount 会先匹配掉整个前缀，上面所有匿名用例会一起变红，这里给出更直接的定位。
    """
    from starlette.routing import Mount
    from starlette.staticfiles import StaticFiles

    static_mounts = [
        route.path
        for route in main.app.routes
        if isinstance(route, Mount) and isinstance(route.app, StaticFiles)
    ]

    assert not any(path.startswith("/uploads") for path in static_mounts), static_mounts


@pytest.mark.parametrize(
    "raw_path",
    [
        "/uploads/avatars/..%2F..%2Fsecret.png",
        "/uploads/avatars/%2e%2e%2fsecret.png",
        "/uploads/avatars/",
    ],
)
def test_paths_that_are_not_a_single_filename_do_not_reach_the_file_system(api, raw_path):
    """多段路径（编码过的 `../`、空文件名）进不了这条路由，任何身份都拿不到 200。"""
    _upload_avatar(api, PNG_BYTES)

    assert api.client.get(raw_path, headers=_auth(api, api.alice)).status_code == 404


def test_an_avatar_column_pointing_outside_the_directory_is_not_served(api, tmp_path):
    """avatar 列是库里的值：被订正成目录外的路径时，路由不得跟着把它读出去。

    目录边界的判据与删除侧共用 `_avatar_file_from_path`，这条钉住读取侧也真的走它。
    """
    outsider = tmp_path / "outsider.png"
    outsider.write_bytes(VICTIM_BYTES)
    api.alice.avatar = f"/uploads/avatars/../{quote('outsider.png')}"
    api.db.commit()

    response = api.client.get(
        "/uploads/avatars/outside.png",
        headers=_auth(api, api.alice),
    )

    assert response.status_code == 404
    assert VICTIM_BYTES not in response.content
