"""访问上报限速。

计数与登录限速一样放在进程内存：API 只有两个进程，每个站点每分钟的转发量
远低于额度，加一张表反而让每次上报都多一次写。代价是额度按进程算，
文档里已写明；以后要全局限速时只换实现，调用点不变。

键取站点 ID：同一站点的多个令牌共用一份额度，避免多发一把令牌就绕过限制。
"""

from __future__ import annotations

import time
from collections import deque
from collections.abc import Callable

from ..errors import ApiError

#: 窗口长度（秒）。
WINDOW_SECONDS = 60


class PageviewThrottle:
    """滑动窗口计数器，达到额度就 429。"""

    def __init__(
        self,
        *,
        max_per_window: int,
        window_seconds: int = WINDOW_SECONDS,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self.max_per_window = max_per_window
        self.window_seconds = window_seconds
        self._clock = clock
        self._hits: dict[str, deque[float]] = {}

    def check(self, site_id: str) -> None:
        """记一次上报并检查额度。超限抛 429，不记这一次。"""
        now = self._clock()
        stamps = self._within_window(site_id, now)
        if len(stamps) >= self.max_per_window:
            raise ApiError.rate_limited("页面访问上报过于频繁，请稍后再试")
        stamps.append(now)
        self._hits[site_id] = stamps

    def _within_window(self, site_id: str, now: float) -> deque[float]:
        stamps = self._hits.get(site_id)
        if stamps is None:
            return deque()
        while stamps and now - stamps[0] > self.window_seconds:
            stamps.popleft()
        if not stamps:
            self._hits.pop(site_id, None)
            return deque()
        return stamps
