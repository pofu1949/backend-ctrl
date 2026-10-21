"""后台任务队列与处理器。

只有 worker 进程会用到这里的处理器；API 进程只做 ``service.enqueue``。
处理器单独放一层，是为了让「队列怎么领」与「领到后做什么」分开改。
"""

from __future__ import annotations

__all__ = ["handlers", "service"]
