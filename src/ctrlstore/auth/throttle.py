"""登录失败限速。

计数放在进程内存：只有两个 API 进程，足以挡住在线暴力猜测，也不必为此加一张表。
代价是两个进程各自计数，真实上限是「每进程」的额度；密码是 Argon2id，单次尝试
本身就要几十毫秒，这个量级够用。真要全局限速时再换成共享存储，接口不变。

键取「账号 + 来源 IP」：同一个账号从别处试，不会连累正在登录的本人；
同一个 IP 换账号试，则各自计数。
"""

from __future__ import annotations

import time
from collections import deque
from collections.abc import Callable

from ..errors import ApiError

#: 窗口内允许的失败次数。
DEFAULT_MAX_FAILURES = 5

#: 窗口长度（秒）。
DEFAULT_WINDOW_SECONDS = 300


class LoginThrottle:
    """滑动窗口计数器。"""

    def __init__(
        self,
        *,
        max_failures: int = DEFAULT_MAX_FAILURES,
        window_seconds: int = DEFAULT_WINDOW_SECONDS,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self.max_failures = max_failures
        self.window_seconds = window_seconds
        self._clock = clock
        self._failures: dict[tuple[str, str], deque[float]] = {}

    def check(self, username: str, ip: str) -> None:
        """超过阈值就抛 429。"""
        key = self._key(username, ip)
        if len(self._within_window(key, self._clock())) >= self.max_failures:
            raise ApiError.rate_limited("登录尝试过于频繁，请稍后再试")

    def record_failure(self, username: str, ip: str) -> None:
        key = self._key(username, ip)
        now = self._clock()
        stamps = self._within_window(key, now)
        stamps.append(now)
        self._failures[key] = stamps

    def reset(self, username: str, ip: str) -> None:
        """登录成功后清空该键的计数。"""
        self._failures.pop(self._key(username, ip), None)

    def _within_window(self, key: tuple[str, str], now: float) -> deque[float]:
        """取窗口内的失败时刻；顺手丢掉过期记录，避免长期占用内存。"""
        stamps = self._failures.get(key)
        if stamps is None:
            return deque()
        while stamps and now - stamps[0] > self.window_seconds:
            stamps.popleft()
        if not stamps:
            self._failures.pop(key, None)
            return deque()
        return stamps

    @staticmethod
    def _key(username: str, ip: str) -> tuple[str, str]:
        # 用户名大小写不敏感，避免靠变换大小写绕过计数。
        return username.strip().lower(), ip
