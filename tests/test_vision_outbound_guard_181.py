"""issue #181 回归：vision 出网口与另外两个出口共用同一条 object_key 形态判据。

同一份「客户端可回带任意 object_key」的输入（聊天附件列是 `/api/chat/stream` 的 body
原样落库的），在本服务里有三个出口：写库（`chat_service._service_minted_attachments`）、
删除（`oss_service._delete_oss_object`）、以及**出网**（`_build_image_urls` 把键拼成
公开读 URL 塞进发往第三方视觉服务的请求体）。修复前只有出网口没有护栏：坏键会被
`quote(key, safe="/")` 原样拼进 URL，httpx 再把路径里的 `..` 规范化成桶里另一个对象。

断言全部打在**请求有没有真的发出去**这一层：替换的是真实请求出口
（`vision_service.httpx.AsyncClient` / `oss_service.httpx.Client`），不是被测函数。
把被测函数整个换掉的替身断不出「请求根本没发出去」——那正是这条 issue 的要害。

另有两条「护栏」用例在修复前也通过（正向对照）：服务端自己铸造的键必须照常构造 URL
并照常发出请求，混合列表里只有坏键那一条被丢弃。它们挡住「一律拒绝」这种把正常链路
一起打死的实现。

对象键写字面量，形态照抄上传路径实际铸出来的样子
（`chat_service.upload_chat_attachment`：`rag-chat/<年>/<月>/<日>/<uuid4().hex><扩展名>`），
不从被测实现里取，避免实现改前缀时用例跟着一起「通过」。
"""

import asyncio
import json
import logging

import pytest
from fastapi import HTTPException

from rag import vision_service
from service import chat_service, oss_service


OSS_CONFIG = {
    "OSS_ACCESS_KEY_ID": "test-id",
    "OSS_ACCESS_KEY_SECRET": "test-secret",
    "OSS_BUCKET": "demo",
    "OSS_ENDPOINT": "https://oss-cn-hangzhou.aliyuncs.com",
}
OSS_HOST = "demo.oss-cn-hangzhou.aliyuncs.com"

MINTED_KEY = "rag-chat/2026/09/21/aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa.png"
MINTED_KEY_JPG = "rag-chat/2026/09/21/bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb.jpg"

# 非本服务铸过的键：既包括完全不相干的前缀，也包括前缀对得上而结构不对的近失形状。
# 第二条与第三条是这条 issue 的重点——`quote(key, safe="/")` 会原样留下 `..`，
# httpx 规范化后请求打到桶里另一个对象上，只查前缀的实现在这里会放行。
FOREIGN_KEYS = [
    "finance-archive/2026/q3/payroll.png",
    "rag-chat/../../../finance-archive/secret.png",
    "rag-chat/2026/09/21/../../finance-archive/2026/q3/payroll.png",
    "rag-chat/2026/09/21/",
    "rag-chat/2026/09/21/not-a-uuid.png",
    "rag-chat/2026/09/21/AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA.png",
    "rag-chat/2026/09/21/aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa.sh",
    "rag-chat/2026/09/21/aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa.jpeg",
    "other-bucket/2026/09/21/aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa.png",
]

TRAVERSAL_KEY = FOREIGN_KEYS[1]


def _patch_oss_config(monkeypatch):
    for name, value in OSS_CONFIG.items():
        monkeypatch.setattr(oss_service, name, value)


def _patch_vision(monkeypatch):
    """让 vision 出网口拿到「看起来配好了」的服务端配置。"""
    _patch_oss_config(monkeypatch)
    monkeypatch.setattr(vision_service, "VISION_API_KEY", "sk-vision")
    monkeypatch.setattr(vision_service, "VISION_MODEL", "vision-model")
    monkeypatch.setattr(vision_service, "VISION_BASE_URL", "https://vision.example.com/v1")


class _VisionResponse:
    def __init__(self, content):
        self.status_code = 200
        self._content = content

    def json(self):
        return {"choices": [{"message": {"content": self._content}}]}


class _VisionEgressRecorder:
    """`vision_service.httpx.AsyncClient` 的替身：只记录请求，不联网。"""

    def __init__(self, description="图中显示：1+1=2，选项 A"):
        self.description = description
        self.requests = []
        self.constructed = 0
        self.kwargs = None

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_exc_info):
        return False

    async def post(self, url, json=None, headers=None):
        self.requests.append({"url": url, "json": json, "headers": headers})
        return _VisionResponse(self.description)


def _patch_vision_egress(monkeypatch, description="图中显示：1+1=2，选项 A"):
    recorder = _VisionEgressRecorder(description)

    def factory(**kwargs):
        recorder.constructed += 1
        recorder.kwargs = kwargs
        return recorder

    monkeypatch.setattr(vision_service.httpx, "AsyncClient", factory)
    return recorder


class _OssDeleteResponse:
    status_code = 204
    text = ""


class _OssDeleteEgressRecorder:
    """`oss_service.httpx.Client` 的替身：只记录 DELETE，不联网。"""

    def __init__(self):
        self.requests = []
        self.constructed = 0

    def __enter__(self):
        return self

    def __exit__(self, *_exc_info):
        return False

    def delete(self, url, headers=None):
        self.requests.append({"url": url, "headers": headers})
        return _OssDeleteResponse()


def _patch_oss_delete_egress(monkeypatch):
    recorder = _OssDeleteEgressRecorder()

    def factory(**kwargs):
        recorder.constructed += 1
        recorder.kwargs = kwargs
        return recorder

    monkeypatch.setattr(oss_service.httpx, "Client", factory)
    return recorder


def _spy_on_url_construction(monkeypatch):
    """记录「有没有真的把键拼成桶内路径」——URL 构造必经 `_oss_object_path`。

    正向对照里这条探针必须自己先响，否则「零次构造」可能是探针根本没接上得出的。
    """
    original = oss_service._oss_object_path
    seen = []

    def spy(object_key):
        seen.append(object_key)
        return original(object_key)

    monkeypatch.setattr(oss_service, "_oss_object_path", spy)
    return seen


# ---------------------------------------------------------------------------
# 出口 3/3：vision 出网口
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("object_key", FOREIGN_KEYS)
def test_foreign_object_key_never_reaches_the_vision_egress(monkeypatch, caplog, object_key):
    """非本服务铸造的键：不构造 URL、不发请求，整条丢弃并留一条可对账的 warning。"""
    _patch_vision(monkeypatch)
    recorder = _patch_vision_egress(monkeypatch)
    constructed_paths = _spy_on_url_construction(monkeypatch)

    with caplog.at_level(logging.WARNING):
        question, analysis = asyncio.run(
            vision_service._build_effective_question("这是什么", [{"object_key": object_key}])
        )

    # 先断出网口本身：坏键一旦出去，这条断言会把发出去的那条请求原样打印出来。
    assert recorder.requests == [], "坏键被送进了发往第三方视觉服务的请求体"
    assert recorder.constructed == 0, "出网客户端被构造了"
    assert constructed_paths == [], "键被拼成了桶内路径"
    assert vision_service._build_image_urls([{"object_key": object_key}]) == []
    assert question == "这是什么"
    assert analysis["status"] == "failed"
    assert analysis["error"] == "图片附件缺少可访问的 OSS object_key，请重新上传后再试。"
    assert "vision attachments dropped" in caplog.text
    assert object_key[:120] in caplog.text


def test_foreign_object_key_is_dropped_but_the_minted_one_still_goes_out(monkeypatch):
    """混合列表：只有坏键那一条被丢弃，其余条目照常发出去。"""
    _patch_vision(monkeypatch)
    recorder = _patch_vision_egress(monkeypatch)

    urls = vision_service._build_image_urls([
        {"object_key": TRAVERSAL_KEY},
        {"object_key": MINTED_KEY},
        {"file_name": "无 key.png"},
    ])

    assert urls == [f"https://{OSS_HOST}/{MINTED_KEY}"]

    asyncio.run(vision_service._build_effective_question("这是什么", [
        {"object_key": TRAVERSAL_KEY},
        {"object_key": MINTED_KEY},
        {"file_name": "无 key.png"},
    ]))

    assert len(recorder.requests) == 1, "只有一条合法附件，请求数应为 1"
    sent = json.dumps(recorder.requests, ensure_ascii=False)
    assert f"https://{OSS_HOST}/{MINTED_KEY}" in sent
    assert "finance-archive" not in sent, "坏键的路径进了发往第三方视觉服务的请求体"


def test_service_minted_object_key_still_builds_a_url_and_sends_the_request(monkeypatch):
    """正向对照：服务端自己铸造的键照常构造 URL、照常发出视网请求。"""
    _patch_vision(monkeypatch)
    recorder = _patch_vision_egress(monkeypatch)
    constructed_paths = _spy_on_url_construction(monkeypatch)

    expected_url = f"https://{OSS_HOST}/{MINTED_KEY}"
    assert vision_service._build_image_urls([{"object_key": MINTED_KEY}]) == [expected_url]

    question, analysis = asyncio.run(
        vision_service._build_effective_question("这是什么", [{"object_key": MINTED_KEY}])
    )

    # 两条路径各构造过一次（`_build_image_urls` 一次、`_build_effective_question` 一次）；
    # 这里只要求探针确实会响——它证明得了上面那些「零次构造」的断言不是探针没接上。
    assert constructed_paths and set(constructed_paths) == {MINTED_KEY}, "URL 构造探针没响"
    assert len(recorder.requests) == 1
    request = recorder.requests[0]
    assert request["url"] == "https://vision.example.com/v1/chat/completions"
    assert request["headers"]["Authorization"] == "Bearer sk-vision"
    assert request["json"]["messages"][1]["content"][1] == {
        "type": "image_url",
        "image_url": {"url": expected_url},
    }
    assert question == "这是什么\n\n图片内容：图中显示：1+1=2，选项 A"
    assert analysis["status"] == "success"


# ---------------------------------------------------------------------------
# 三个出口对同一个坏键判定一致
# ---------------------------------------------------------------------------

def test_the_three_exits_agree_on_the_same_foreign_key(monkeypatch, caplog):
    """同一个坏键：写库丢弃 / 删除拒绝（不签 DELETE）/ 出网不出（不构造 URL、不发请求）。"""
    _patch_vision(monkeypatch)
    vision_recorder = _patch_vision_egress(monkeypatch)
    delete_recorder = _patch_oss_delete_egress(monkeypatch)
    constructed_paths = _spy_on_url_construction(monkeypatch)

    # 出口 1/3：写库——整条丢弃。
    with caplog.at_level(logging.WARNING):
        accepted = chat_service._service_minted_attachments(
            [{"object_key": TRAVERSAL_KEY}, {"object_key": MINTED_KEY}], "c-181"
        )
    assert [item["object_key"] for item in accepted] == [MINTED_KEY]
    assert "chat attachments dropped" in caplog.text

    # 出口 2/3：删除——拒绝签发，且拒绝发生在构造请求之前。
    with pytest.raises(oss_service.ForeignObjectKeyError):
        oss_service._delete_oss_object(TRAVERSAL_KEY)
    assert delete_recorder.constructed == 0
    assert delete_recorder.requests == []

    # 出口 3/3：出网——不构造 URL、不发请求。
    assert vision_service._build_image_urls([{"object_key": TRAVERSAL_KEY}]) == []
    assert vision_recorder.constructed == 0
    assert vision_recorder.requests == []

    # 三个出口同一条判据、同一个答案。
    assert oss_service.is_service_minted_key(TRAVERSAL_KEY) is False
    assert constructed_paths == [], "坏键在 vision 侧被拼成了桶内路径"


def test_only_the_minted_key_shape_is_accepted_by_the_shared_predicate():
    """判据本身：铸造形态放行，近失形状一律不放行（三个出口共用这一条）。"""
    assert oss_service.is_service_minted_key(MINTED_KEY) is True
    assert oss_service.is_service_minted_key(MINTED_KEY_JPG) is True
    for object_key in FOREIGN_KEYS:
        assert oss_service.is_service_minted_key(object_key) is False, object_key
    assert oss_service.is_service_minted_key(None) is False
    assert oss_service.is_service_minted_key(123) is False


def test_public_oss_url_refuses_foreign_keys_before_touching_the_oss_config(monkeypatch):
    """构造口自己就拒：判键在查配置之前，调用方分得清「键不合规」与「服务没配好」。"""
    # 故意不补 OSS 配置：坏键仍然报键的问题，而不是 HTTPException(500) 那条配置错误。
    with pytest.raises(oss_service.ForeignObjectKeyError):
        oss_service._public_oss_url(TRAVERSAL_KEY)

    monkeypatch.setattr(oss_service, "OSS_BUCKET", "")
    with pytest.raises(oss_service.ForeignObjectKeyError):
        oss_service._public_oss_url(TRAVERSAL_KEY)

    # 换成铸造形态的键，同一个空配置才轮到配置错误说话。
    with pytest.raises(HTTPException) as excinfo:
        oss_service._public_oss_url(MINTED_KEY)
    assert (excinfo.value.status_code, excinfo.value.detail) == (500, "OSS 环境变量未完整配置")
