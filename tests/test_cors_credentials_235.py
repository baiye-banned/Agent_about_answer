"""issue #235 回归：CORS 不得把「任意源」与「允许凭据」绑在一起。

修复前 `main.py` 是 `allow_origins=["*"]` + `allow_credentials=True`。这个组合在浏览器里
无法共存，Starlette 于是改为**回显请求里的任意 Origin** 并附上 `Access-Control-Allow-Credentials:
true`——拿 `Origin: https://evil.example` 探测（见 issue #235），回的就是这两行。修复后都不该再出现：
`*` 照旧（本服务没有 cookie 凭据），但不再伴随允许凭据的头。

用例全部打在**真实的 `main.app`** 上，走真实的中间件栈：

- 预检由 `CORSMiddleware` 在路由之前短路返回，不经过任何路由，因此既不需要
  `get_db` / `get_current_user` 的 dependency_overrides，也不会碰数据库；
- 用 `TestClient(main.app)` 而**不进入 `with` 上下文**，避免触发 lifespan 尾部的
  `schedule_orphan_attachment_sweep()`——它会在后台起线程扫孤儿附件，与本节无关。

T5 是回归锁：将来有人把 `allow_credentials` 改回 `True`，T1/T2 会红，而 T5 直接指名道姓地红，
省去从响应头反推配置的时间。
"""

from fastapi.middleware.cors import CORSMiddleware
from fastapi.testclient import TestClient

import main


CROSS_ORIGIN = "https://evil.example"
LOCAL_DEV_ORIGIN = "http://localhost:5173"
PREFLIGHT_HEADERS = {
    "Origin": CROSS_ORIGIN,
    "Access-Control-Request-Method": "GET",
    "Access-Control-Request-Headers": "authorization",
}


def _client() -> TestClient:
    # 不进 with：不触发 lifespan（见模块 docstring）。
    return TestClient(main.app)


def _cors_headers(response) -> list[str]:
    return [name for name in response.headers if name.lower().startswith("access-control-")]


def test_preflight_does_not_echo_an_arbitrary_origin_or_allow_credentials():
    response = _client().options("/api/chat/conversations", headers=PREFLIGHT_HEADERS)

    assert response.status_code == 200
    assert response.headers["access-control-allow-origin"] == "*"
    assert "access-control-allow-credentials" not in response.headers


def test_actual_request_does_not_echo_an_arbitrary_origin_or_allow_credentials():
    response = _client().get("/health", headers={"Origin": CROSS_ORIGIN})

    assert response.status_code == 200
    assert response.json() == {"status": "ok"}
    assert response.headers["access-control-allow-origin"] == "*"
    assert "access-control-allow-credentials" not in response.headers


def test_simple_request_from_the_local_dev_origin_is_untouched():
    response = _client().get("/health", headers={"Origin": LOCAL_DEV_ORIGIN})

    assert response.status_code == 200
    assert response.json() == {"status": "ok"}
    assert response.headers["access-control-allow-origin"] == "*"


def test_requests_without_an_origin_get_no_cors_headers():
    """不带 `Origin` 的请求与没有中间件时表现一致。

    `CORSMiddleware` 只在看到 `Origin` 时才介入；没有它时请求原样落到路由上——`GET /health`
    是 200，`OPTIONS /api/chat/conversations` 因为没有任何路由注册 OPTIONS，是 405。
    两种情况下都不该出现任何 `access-control-*` 响应头。
    """
    client = _client()

    simple = client.get("/health")
    options = client.options("/api/chat/conversations")

    assert simple.status_code == 200
    assert simple.json() == {"status": "ok"}
    assert options.status_code == 405
    assert _cors_headers(simple) == []
    assert _cors_headers(options) == []


def test_cors_middleware_is_configured_without_credentials():
    """回归锁：配置本身，而不是它的响应头表现。"""
    cors = [entry for entry in main.app.user_middleware if entry.cls is CORSMiddleware]

    assert len(cors) == 1
    kwargs = cors[0].kwargs
    assert kwargs["allow_credentials"] is False
    assert kwargs["allow_origins"] == ["*"]
