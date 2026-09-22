"""列表读口的页大小口径与页内夹取（issue #191）。

三个列表接口（知识库 / 会话 / 知识库文件）原先都是「取全量」：SQL 以 `.all()` 收尾，
路由、service、CRUD 三层都没有任何上限，响应体随该用户的数据量线性增长。这里把默认页
大小、单页上限与夹取收到一处，数值与消息接口（`crud/chat.py` 的 `CHAT_MESSAGE_DEFAULT_LIMIT`
/ `CHAT_MESSAGE_MAX_LIMIT`）保持同一组——两套常量各写一遍是为了让列表链路不必反向依赖
chat 的常量，但取值必须一致，`tests/test_list_page_caps_191.py` 有配对断言钉住。

夹取放在 CRUD 层而不是只依赖路由的 `Query(ge/le)`：CRUD 是更底层的入口，脚本或内部调用
可能绕开接口层，负值在 SQLite 上等价于「不设上限」（`LIMIT -1` 就是无限制），会把整张表
一次读出来。`crud/chat.py` 的 `list_messages` 早于本模块，行为等价（同一套 `max(1, min(...))`
写法），且其口径已被现有用例钉住，故保持原样不动。
"""

from datetime import datetime

from sqlalchemy import func

LIST_DEFAULT_LIMIT = 50
LIST_MAX_LIMIT = 200


def clamp_limit(limit: int | None, *, default: int = LIST_DEFAULT_LIMIT, maximum: int = LIST_MAX_LIMIT) -> int:
    """把页大小夹进 [1, maximum]；None 走默认页大小。

    与 `list_messages` 同一口径：None 只表示「调用方没给页大小」，不等于「不设上限」；
    小于 1 的值夹到 1 而不是报错——CRUD 层是兜底不是校验入口，越界的报错由 service 层
    的 `resolve_list_limit` 给出（那里才能让直接调用者拿到可读的 422）。
    """
    if limit is None:
        return default
    return max(1, min(int(limit), maximum))


# 秒级时间戳的文本宽度：'YYYY-MM-DD HH:MM:SS'。见 seconds_text。
SECONDS_TEXT_LENGTH = 19


def seconds_text(column):
    """把时间戳列归一到「秒级文本」再参与复合游标比较。

    会话列表按 `(updated_at, id)` 翻页，时间戳这一档必须比得出「相等」，否则键集游标
    退化成「同秒的行全部重来」或「同秒的行整批漏掉」。而 `updated_at` 在两种写入路径下
    的**存储形状并不一致**：
    - 服务端默认值（`CURRENT_TIMESTAMP` / MySQL 的 `DATETIME`）：`2026-09-22 10:09:21`；
    - 应用侧显式赋值：SQLite 方言按固定格式绑成 `2026-09-22 10:09:21.000000`，原样入库。

    SQLite 把两者都当文本，直接拿一个 Python datetime 去绑（SQLAlchemy 固定带 6 位小数）
    就会与第一种形状比不出相等：`=` 永不成立、`<` 对同一秒的行恒真，下一页把刚翻过的
    行原样再返回一遍（实测：同秒 5 行时「下一页」仍返回这 5 行，翻页原地打转）。
    截到 19 个字符后两种形状落回同一个值，比较才成立。

    `substr` 在两种后端上语义一致：SQLite 直接切文本；MySQL 把 DATETIME 转成
    `'YYYY-MM-DD HH:MM:SS[.ffffff]'` 再切（`SUBSTR(datetime, 1, 19)` 是合法写法）。

    代价是该表达式不走索引。这里可以接受：会话列表本来就没有 `(user_id, updated_at)`
    复合索引（#176），今天的查询已经是「按 user_id 过滤 + filesort」，
    多一个逐行判定不改变访问路径。
    """
    return func.substr(column, 1, SECONDS_TEXT_LENGTH)


def datetime_cursor_value(value: datetime) -> str:
    """复合游标里时间戳那一档的绑定值：与 seconds_text 归一后的形状对齐（秒级文本）。

    MySQL 会把该常量按 DATETIME 解析后再比，无需额外转换；微秒一律截掉——
    列里的写入方只有服务端默认值，取不到秒以下的值。
    """
    return value.replace(microsecond=0).strftime("%Y-%m-%d %H:%M:%S")
