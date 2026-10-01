"""回归（issue #236）：`/api/checkpointer/threads` 端点已移除。

该端点自注册起就只返回 `{"threads": []}`——`checkpoints` 表在生产链路上没有任何写入方，
`thread_id` 也无从归属到用户，于是它今天既无内容、又没有可依据的访问控制。处置是**移除
端点**，而不是给它补一层按用户过滤：那会把一段推断出来的归属固化进访问控制。

本文件锁住三件事：

- T1 路由表里不再有这条路径；
- T2 生产 `main.app` 上请求它是 404，且响应体里没有任何 thread_id；
- T3 反向锁：`checkpoints` 表的读写 API 仍在——「删端点」不等于「删模块」。

T4/T5 是既有的 `test_trace_purge_on_conversation_delete_127.py`（改完后整文件通过）与
`test_checkpointer.py`（原样通过），不在这份文件里。

为什么 T1 用 `openapi()["paths"]` 而不是遍历 `main.app.routes`：`fastapi==0.141.1` 下
`include_router` 不把子路由展平，每个被包含的 router 是一个 `path=None` 的实例，于是
`{getattr(r, "path", None) for r in main.app.routes}` 只含根应用自己的几条内置路径、**没有
任何 `/api` 路径**——「目标不在集合里」永真，端点还在也照绿。`openapi()["paths"]` 走同一份
注册表但已展开，才是一条真断言。

T2 必须打在**真实的 `main.app`** 上：手工自建的 `FastAPI()` 夹具里本就没有这条路由，
那种 404 是必然的，证明不了「生产 app 上它没了」。
"""

from fastapi.testclient import TestClient

import main
from database import checkpointer

ENDPOINT = "/api/checkpointer/threads"


def test_endpoint_is_absent_from_the_openapi_route_table():
    assert ENDPOINT not in main.app.openapi()["paths"]


def test_endpoint_returns_404_on_the_real_app():
    # 不用 with：lifespan 会建表、播种默认用户并起后台清扫线程；本用例只关心路由表。
    response = TestClient(main.app).get(ENDPOINT)

    assert response.status_code == 404, (
        f"端点应已移除，实际 {response.status_code} {response.text}"
    )
    assert "thread_id" not in response.text


def test_a_sibling_api_route_still_answers_on_the_real_app():
    """阳性对照：同一 app 上别的 /api 路由仍然可达，404 不是「整个 app 起不来」。

    不带凭据时这条路径返回 401（`backend/service/auth_service.py`），关键在它不是 404。
    """
    response = TestClient(main.app).get("/api/chat/conversations")

    assert response.status_code != 404, (
        f"/api/chat/conversations 不该是 404，实际 {response.status_code} {response.text}"
    )


def test_checkpoint_read_write_api_survives_the_endpoint_removal(monkeypatch, tmp_path):
    """反向锁：「删端点」不能被执行成「删模块」。

    三个函数都真调一次（而不是只断言可导入），否则把函数体掏空也照样绿。
    """
    monkeypatch.setattr(checkpointer, "CHECKPOINTER_DB_PATH", str(tmp_path / "checkpointer.db"))

    checkpointer.save_checkpoint("thread-1", "state", {"step": 1})
    assert checkpointer.load_checkpoint("thread-1", "state") == {"step": 1}

    checkpointer.delete_thread_checkpoints("thread-1")
    assert checkpointer.load_checkpoint("thread-1", "state") is None
