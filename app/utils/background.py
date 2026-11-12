"""
进程内后台任务。

保留任务引用避免被 GC 回收，并记录异常。注意：这不是持久化任务队列，
进程重启后未完成的任务会丢失（摘要压缩、记忆提取都允许丢失后下次重试）。
"""

import asyncio
import logging
from collections.abc import Coroutine
from typing import Any

logger = logging.getLogger(__name__)

_tasks: set[asyncio.Task] = set()


def _on_done(task: asyncio.Task) -> None:
    _tasks.discard(task)
    if task.cancelled():
        return
    exc = task.exception()
    if exc is not None:
        logger.error("后台任务 %s 失败", task.get_name(), exc_info=exc)


def spawn(coro: Coroutine[Any, Any, Any], name: str) -> asyncio.Task:
    task = asyncio.create_task(coro, name=name)
    _tasks.add(task)
    task.add_done_callback(_on_done)
    return task


async def drain(timeout: float = 30.0) -> None:
    """关闭前等待后台任务完成。"""
    if not _tasks:
        return
    pending = list(_tasks)
    done, still_pending = await asyncio.wait(pending, timeout=timeout)
    for task in still_pending:
        task.cancel()
