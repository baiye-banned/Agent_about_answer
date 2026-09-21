
import base64
import hashlib
import hmac
import re
import time
from email.utils import formatdate
from urllib.parse import quote, urlencode

from fastapi import HTTPException
import httpx

from config import OSS_ACCESS_KEY_ID, OSS_ACCESS_KEY_SECRET, OSS_BUCKET, OSS_ENDPOINT
from service.utils_service import IMAGE_UPLOAD_TYPES


# 本服务自己铸造的聊天附件键：rag-chat/<年>/<月>/<日>/<对象 id><扩展名>。
# 唯一铸造点是 chat_service.upload_chat_attachment（:201），扩展名取自 IMAGE_UPLOAD_TYPES；
# 这里从同一张表推导而不是写死一份，上传侧将来多一种图片格式时回收侧跟着放行，不会悄悄漏删。
#
# 为什么是形态校验而不是 `object_key.startswith("rag-chat/")`：键会被 quote(key, safe="/")
# 原样拼进 URL 路径，而 httpx 会规范化路径里的 `..`——`rag-chat/../../finance-archive/x`
# 以 `/finance-archive/x` 发出去，仅仅查前缀挡不住这种穿越。分段受约束（年/月/日是纯数字、
# 文件名段不含 `/`）才排得出 `..`，因此这里的约束是**结构**而不是前缀。
#
# 对象 id 段放宽到 16~64 位十六进制：uuid4().hex 是 32 位，放宽是为了将来换 id 生成方式时
# 不至于悄悄停止回收（少删只是泄漏，错删不可逆）。
_MINTED_EXTENSIONS = sorted({ext.lstrip(".").lower() for ext in IMAGE_UPLOAD_TYPES.values()})
SERVICE_MINTED_KEY_PATTERN = re.compile(
    r"^rag-chat/\d{4}/\d{2}/\d{2}/[0-9a-f]{16,64}\.(?:" + "|".join(_MINTED_EXTENSIONS) + r")$"
)


class ForeignObjectKeyError(ValueError):
    """对象键不是本服务铸造的：拒绝用它签发任何带服务端凭据的请求。"""


def is_service_minted_key(object_key: object) -> bool:
    """这个键是不是本服务写下的。只回答「能不能对它用服务端凭据」，不做任何寻址。"""
    return isinstance(object_key, str) and SERVICE_MINTED_KEY_PATTERN.match(object_key) is not None


def _ensure_oss_config():
    if not all([OSS_ACCESS_KEY_ID, OSS_ACCESS_KEY_SECRET, OSS_BUCKET, OSS_ENDPOINT]):
        raise HTTPException(500, "OSS 环境变量未完整配置")


def _oss_host() -> str:
    endpoint = OSS_ENDPOINT.replace("https://", "").replace("http://", "").rstrip("/")
    if endpoint.startswith(f"{OSS_BUCKET}."):
        return endpoint
    return f"{OSS_BUCKET}.{endpoint}"


def _oss_object_path(object_key: str) -> str:
    return "/" + quote(object_key, safe="/")


def _oss_signature(string_to_sign: str) -> str:
    digest = hmac.new(
        OSS_ACCESS_KEY_SECRET.encode("utf-8"),
        string_to_sign.encode("utf-8"),
        hashlib.sha1,
    ).digest()
    return base64.b64encode(digest).decode("utf-8")


async def _put_oss_object(object_key: str, content: bytes, content_type: str):
    _ensure_oss_config()
    host = _oss_host()
    date = formatdate(usegmt=True)
    resource = f"/{OSS_BUCKET}/{object_key}"
    string_to_sign = f"PUT\n\n{content_type}\n{date}\nx-oss-object-acl:public-read\n{resource}"
    signature = _oss_signature(string_to_sign)
    url = f"https://{host}{_oss_object_path(object_key)}"
    headers = {
        "Authorization": f"OSS {OSS_ACCESS_KEY_ID}:{signature}",
        "Content-Type": content_type,
        "Date": date,
        "Host": host,
        "x-oss-object-acl": "public-read",
    }

    async with httpx.AsyncClient(timeout=60) as client:
        response = await client.put(url, content=content, headers=headers)
    if response.status_code >= 400:
        raise RuntimeError(f"{response.status_code} {response.text[:200]}")


def _delete_oss_object(object_key: str) -> None:
    """删除一个对象。

    只为本服务铸造的键签发 DELETE，其余一律拒绝：对象键来自客户端（附件列是
    `/api/chat/stream` 的 body 原样落库的），用服务端凭据为它签名，就等于让任何登录用户
    借服务的 AK 删桶里任意已知 key 的对象。护栏放在签发口而不是调用点：这是全仓唯一
    一处用服务端凭据发 DELETE 的地方，将来多出别的调用方也自动继承这条约束。

    拒绝是抛异常而不是静默跳过：调用方据此落一条可对账的 warning，也不会把「没删」
    当成删成功。对象留着的代价是泄漏，删错了没有回收站。

    404 不算失败：删除的目标状态是「对象不存在」，对象本来就不在时该状态已经满足，
    重跑一次回收（或两个会话引用了同一个对象键）不应该报错。

    同步实现：调用点是同步的删除接口（FastAPI 用线程池跑），不为了回收附件把整条
    删除链路改成 async；同模块的上传路径保持 async 不变。
    """
    if not is_service_minted_key(object_key):
        raise ForeignObjectKeyError(
            f"refuse to sign DELETE for an object key this service never minted: {object_key!r}"
        )
    _ensure_oss_config()
    host = _oss_host()
    date = formatdate(usegmt=True)
    resource = f"/{OSS_BUCKET}/{object_key}"
    string_to_sign = f"DELETE\n\n\n{date}\n{resource}"
    signature = _oss_signature(string_to_sign)
    url = f"https://{host}{_oss_object_path(object_key)}"
    headers = {
        "Authorization": f"OSS {OSS_ACCESS_KEY_ID}:{signature}",
        "Date": date,
        "Host": host,
    }

    with httpx.Client(timeout=30) as client:
        response = client.delete(url, headers=headers)
    if response.status_code >= 400 and response.status_code != 404:
        raise RuntimeError(f"{response.status_code} {response.text[:200]}")


def _sign_oss_url(object_key: str, expires: int = 3600) -> str:
    _ensure_oss_config()
    expires_at = int(time.time()) + expires
    resource = f"/{OSS_BUCKET}/{object_key}"
    string_to_sign = f"GET\n\n\n{expires_at}\n{resource}"
    signature = _oss_signature(string_to_sign)
    query = urlencode({
        "OSSAccessKeyId": OSS_ACCESS_KEY_ID,
        "Expires": str(expires_at),
        "Signature": signature,
    })
    return f"https://{_oss_host()}{_oss_object_path(object_key)}?{query}"


def _public_oss_url(object_key: str) -> str:
    _ensure_oss_config()
    return f"https://{_oss_host()}{_oss_object_path(object_key)}"
