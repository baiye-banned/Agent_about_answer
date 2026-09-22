"""列表页参数的服务层兜底（issue #191）。

HTTP 请求的页大小已经由路由签名上的 `Query(ge/le)` 拦下（`Annotated[int, Query(...)]`
直接写在 service 函数上，`router.add_api_route` 注册的就是它）。这里再兜一次，是为了让
**绕过接口层的直接调用**（脚本、内部调用、测试）也拿不到越界页大小——与
`chat_service.resolve_message_limit` 同一个设计理由：CRUD 层的夹取是静默的，直接调用者
会以为自己拿到了请求的页大小；service 层要把越界说出来。

会话列表的游标是复合的（`updated_at` + uuid 主键），两个参数必须成对出现：只给一个时
按「没有游标」处理会让调用方拿到第一页——他以为在翻第二页，两边对不上账却都不报错。
"""

from datetime import datetime

from fastapi import HTTPException

from crud.pagination import LIST_DEFAULT_LIMIT, LIST_MAX_LIMIT


def resolve_list_limit(limit: int | None) -> int:
    """页大小兜底校验：None 走默认页大小，越界直接 422。"""
    if limit is None:
        return LIST_DEFAULT_LIMIT
    if limit < 1 or limit > LIST_MAX_LIMIT:
        raise HTTPException(422, f"limit 必须在 1 到 {LIST_MAX_LIMIT} 之间")
    return limit


def resolve_conversation_cursor(
    before_updated_at: datetime | None,
    before_id: str | None,
) -> tuple[datetime, str] | None:
    """把会话列表的两个游标参数收成一个复合游标；都不给表示「取最新一页」。

    空串按「没给」处理：前端从列表项上取 updated_at/id 拼参数，字段缺失时拼出的是空值，
    这种残缺游标与半个游标同源，一并按 422 报出来，而不是静默退回第一页。
    """
    cursor_id = (before_id or "").strip()
    if before_updated_at is None and not cursor_id:
        return None
    if before_updated_at is None or not cursor_id:
        raise HTTPException(422, "游标必须同时给出 before_updated_at 与 before_id")
    return before_updated_at, cursor_id
