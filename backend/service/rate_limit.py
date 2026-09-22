"""进程内限流设施（issue #183）：登录失败节流 + SSE 聊天流并发闸门。

边界（写给后来人）：

- 状态在进程内存里。单进程 uvicorn 下就是全局闸门；多 worker / 多实例部署时每个进程各持一份
  计数，真实上限 = 配置值 × 进程数。需要跨进程一致的上限时，把这两个类换成 Redis 计数器的
  实现即可——调用方只依赖 `retry_after` / `record_failure` / `reset` / `try_acquire` 这几个动作。
- 两者都**不排队**：超阈值立即拒绝（调用方回 429 + Retry-After）。排队会把请求拖到上游超时才
  释放，既挡不住资源耗尽，也让调用方分不清「被拒」和「很慢」。
- 计数不落库、不跨重启保留：重启即清空，这本身就是「到期自动恢复」的一部分。

时钟用 `time.monotonic`（不受系统时间调整影响）；测试可以注入自己的 clock，不必真的等待。
"""

import math
import threading
import time
from collections import deque

from config import (
    CHAT_STREAM_MAX_CONCURRENCY,
    LOGIN_RATE_LIMIT_MAX_FAILURES,
    LOGIN_RATE_LIMIT_WINDOW_SECONDS,
)


class FailureWindow:
    """滑动窗口失败计数：同一 key 在 window_seconds 内的失败达到 max_failures 次即拒绝。

    窗口滑出后计数自动回落，不做永久锁定——这是「合法用户到期自动恢复」的实现口径。
    只记失败、不记成功：成功由调用方 `reset()` 清账，偶发输错不会攒成锁定。
    """

    def __init__(self, max_failures, window_seconds, *, max_keys=50_000, clock=time.monotonic):
        # 0 / 负数会被读成「永远拒绝」，对运维是陷阱（以为关掉了限流，其实锁死了所有人）：
        # 统一收敛到至少 1。
        self.max_failures = max(1, int(max_failures))
        self.window_seconds = max(1, int(window_seconds))
        # 跟踪的 key 有硬上界：key 里带用户名等客户端可控内容，不设上界时一个扫号脚本
        # 就能把内存顶起来。达到上界先清过期项，再按「最后一次失败最早」淘汰。
        self._max_keys = max(1, int(max_keys))
        self._clock = clock
        self._lock = threading.Lock()
        self._failures = {}

    def retry_after(self, key) -> int:
        """0 = 放行；> 0 = 还需要等待的秒数（向上取整，至少 1 秒）。"""
        now = self._clock()
        with self._lock:
            stamps = self._live_stamps(key, now)
            if stamps is None or len(stamps) < self.max_failures:
                return 0
            return max(1, math.ceil(stamps[0] + self.window_seconds - now))

    def record_failure(self, key) -> None:
        now = self._clock()
        with self._lock:
            stamps = self._live_stamps(key, now)
            if stamps is None:
                stamps = self._failures.setdefault(key, deque())
            stamps.append(now)
            if len(self._failures) > self._max_keys:
                self._evict(now)

    def reset(self, key) -> None:
        """一次成功就把这个 key 的失败账清掉。"""
        with self._lock:
            self._failures.pop(key, None)

    def clear(self) -> None:
        with self._lock:
            self._failures.clear()

    def _live_stamps(self, key, now):
        """丢掉滑出窗口的时间戳；一条不剩时把 key 一起删掉（否则字典只增不减）。"""
        stamps = self._failures.get(key)
        if stamps is None:
            return None
        cutoff = now - self.window_seconds
        while stamps and stamps[0] <= cutoff:
            stamps.popleft()
        if not stamps:
            self._failures.pop(key, None)
            return None
        return stamps

    def _evict(self, now) -> None:
        for key in [key for key, stamps in self._failures.items() if stamps[-1] <= now - self.window_seconds]:
            self._failures.pop(key, None)
        overflow = len(self._failures) - self._max_keys
        if overflow > 0:
            oldest = sorted(self._failures.items(), key=lambda item: item[1][-1])[:overflow]
            for key, _stamps in oldest:
                self._failures.pop(key, None)


class ConcurrencyGate:
    """非阻塞并发闸门：满员时立刻返回 None，绝不排队等待。

    用它而不是 `asyncio.Semaphore`：闸门要在**一个请求的生命周期**上持有（流结束、抛异常、
    客户端断开才释放），而 `asyncio.Semaphore` 会绑定第一个用到它的事件循环——进程级单例
    跨事件循环（测试里的 `asyncio.run`、多端口/多 loop 部署）会直接抛 "bound to a different
    event loop"。这里只用一个受锁保护的计数，既不绑 loop，也能被非 async 的收尾路径调用。
    """

    def __init__(self, limit):
        # 同 FailureWindow：0 / 负数一律收敛到 1，避免「配置写 0 = 全部拒绝」的陷阱。
        self.limit = max(1, int(limit))
        self._lock = threading.Lock()
        self._in_flight = 0

    def try_acquire(self):
        """占到一个槽返回 Slot，满员返回 None（调用方据此立刻回 4xx）。"""
        with self._lock:
            if self._in_flight >= self.limit:
                return None
            self._in_flight += 1
        return Slot(self)

    @property
    def in_flight(self) -> int:
        with self._lock:
            return self._in_flight


class Slot:
    """占位凭据。`release()` 幂等：正常结束、异常、断开三条路径都调一次也不会把计数压负。"""

    __slots__ = ("_gate", "_released")

    def __init__(self, gate):
        self._gate = gate
        self._released = False

    def release(self) -> None:
        with self._gate._lock:
            if self._released:
                return
            self._released = True
            if self._gate._in_flight > 0:
                self._gate._in_flight -= 1


# 进程级单例。阈值来自 config（= 环境变量），改配置即改行为，调用方不再持有第二套数字。
login_failures = FailureWindow(LOGIN_RATE_LIMIT_MAX_FAILURES, LOGIN_RATE_LIMIT_WINDOW_SECONDS)
chat_stream_slots = ConcurrencyGate(CHAT_STREAM_MAX_CONCURRENCY)
