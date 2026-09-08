"""Bounded blocking I/O for the shared Gateway, independent of HTTP waiters.

Admission is non-waiting and process-wide, including during Gateway replacement.
Only an actual concurrent Future terminal state releases its permit. Cancelling
an asyncio waiter does not cancel, duplicate, or hide an accepted operation.
"""
from __future__ import annotations

import asyncio
import threading
from collections.abc import Callable
from concurrent.futures import Future, ThreadPoolExecutor
from typing import Any, TypeVar

LOCAL_TOOL_LIMIT = 5
_PROCESS_SLOTS = threading.BoundedSemaphore(LOCAL_TOOL_LIMIT)
_T = TypeVar("_T")


class LocalToolBusy(RuntimeError):
    """Admission rejected; no operation was submitted."""


class LocalToolWorkers:
    """A lazy executor with no backlog and explicit, non-destructive shutdown."""

    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._executor: ThreadPoolExecutor | None = None
        self._workers: set[Future[Any]] = set()
        self._closed = False

    @property
    def active(self) -> tuple[Future[Any], ...]:
        with self._lock:
            return tuple(self._workers)

    def submit(self, operation: Callable[[], _T]) -> Future[_T]:
        with self._lock:
            if self._closed:
                raise LocalToolBusy("连接服务正在停止；本次命令未提交。")
            if len(self._workers) >= LOCAL_TOOL_LIMIT or not _PROCESS_SLOTS.acquire(blocking=False):
                raise LocalToolBusy("短命令并发已满；本次命令未提交，请稍后显式重试。")
            try:
                if self._executor is None:
                    self._executor = ThreadPoolExecutor(
                        max_workers=LOCAL_TOOL_LIMIT, thread_name_prefix="gateway-local"
                    )
                future = self._executor.submit(operation)
            except BaseException:
                _PROCESS_SLOTS.release()
                raise
            self._workers.add(future)
            # add_done_callback may run inline for an already finished future.
            future.add_done_callback(self._finished)
            return future

    def _finished(self, future: Future[Any]) -> None:
        with self._lock:
            if future in self._workers:
                self._workers.remove(future)
                _PROCESS_SLOTS.release()

    async def run(self, operation: Callable[[], _T]) -> _T:
        future = asyncio.wrap_future(self.submit(operation))

        def consume_exception(done: asyncio.Future[_T]) -> None:
            # A disconnected HTTP caller may no longer observe the exception.
            if not done.cancelled():
                done.exception()

        future.add_done_callback(consume_exception)
        return await asyncio.shield(future)

    def close(self) -> None:
        with self._lock:
            self._closed = True
            executor = self._executor
        if executor is not None:
            # Accepted operations retain their permits until actually finished.
            # Never kill a subprocess merely because its HTTP caller went away.
            executor.shutdown(wait=False, cancel_futures=False)
