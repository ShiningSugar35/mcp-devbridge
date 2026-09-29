"""Pure-Python bounded batch lifecycle helpers.

The desktop owns UI state and ServiceCoordinator. This module only coordinates
per-project lifecycle calls so it stays testable without Qt.
"""

from __future__ import annotations

from collections.abc import Callable, Sequence
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass

DEFAULT_BATCH_START_WORKERS = 2
MAX_BATCH_START_WORKERS = 4


@dataclass(frozen=True)
class BatchFailure[T]:
    item: T
    error: Exception


@dataclass(frozen=True)
class BatchStartResult[T, R]:
    started: tuple[tuple[T, R], ...]
    failures: tuple[BatchFailure[T], ...]
    first_success_hook_error: Exception | None = None


def run_start_batch[T, R](
    items: Sequence[T],
    start_one: Callable[[T], R],
    *,
    on_first_success: Callable[[], None] | None = None,
    max_workers: int = DEFAULT_BATCH_START_WORKERS,
) -> BatchStartResult[T, R]:
    """Start independent items with bounded concurrency.

    The first-success hook runs once as soon as the first project succeeds; it
    is deliberately independent from later project failures. Its failure is
    reported separately and never rolls back already-started projects.
    """

    if not items:
        return BatchStartResult(started=(), failures=())
    workers = max(1, min(int(max_workers), MAX_BATCH_START_WORKERS, len(items)))
    started: list[tuple[T, R]] = []
    failures: list[BatchFailure[T]] = []
    hook_error: Exception | None = None
    hook_attempted = False

    with ThreadPoolExecutor(max_workers=workers, thread_name_prefix="mcpdb-start") as executor:
        futures = {executor.submit(start_one, item): item for item in items}
        for future in as_completed(futures):
            item = futures[future]
            try:
                value = future.result()
            except Exception as exc:  # noqa: BLE001 - isolate each project failure
                failures.append(BatchFailure(item=item, error=exc))
                continue
            started.append((item, value))
            if on_first_success is not None and not hook_attempted:
                hook_attempted = True
                try:
                    on_first_success()
                except Exception as exc:  # noqa: BLE001 - transport must not roll back roots
                    hook_error = exc

    return BatchStartResult(
        started=tuple(started),
        failures=tuple(failures),
        first_success_hook_error=hook_error,
    )
