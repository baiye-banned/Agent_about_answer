
import base64
import hashlib
import hmac
import time
from email.utils import formatdate
from urllib.parse import quote, urlencode

from fastapi import HTTPException
import httpx

from config import OSS_ACCESS_KEY_ID, OSS_ACCESS_KEY_SECRET, OSS_BUCKET, OSS_ENDPOINT


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

    404 不算失败：删除的目标状态是「对象不存在」，对象本来就不在时该状态已经满足，
    重跑一次回收（或两个会话引用了同一个对象键）不应该报错。

    同步实现：调用点是同步的删除接口（FastAPI 用线程池跑），不为了回收附件把整条
    删除链路改成 async；同模块的上传路径保持 async 不变。
    """
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
