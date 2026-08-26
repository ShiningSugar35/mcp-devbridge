"""Windows idle-sleep guard for a running public MCP Hub.

The public MCP endpoint is only useful while the local origin stays runnable.
Windows Modern Standby can enter low-power idle after the user stops interacting
with the machine, so a desktop/network-service process must explicitly declare
that the system is still required.  We deliberately do *not* keep the display
on and do not request Away Mode.
"""

from __future__ import annotations

import contextlib
import ctypes
import threading
from collections.abc import Callable

from .platform_support import IS_WINDOWS

ES_SYSTEM_REQUIRED = 0x00000001
ES_CONTINUOUS = 0x80000000
DEFAULT_REFRESH_SECONDS = 30.0

ExecutionStateSetter = Callable[[int], int]


def _windows_execution_state_setter(flags: int) -> int:
    win_dll = getattr(ctypes, "WinDLL", None)
    if win_dll is None:
        raise OSError("Windows execution-state API is unavailable on this platform.")
    kernel32 = win_dll("kernel32", use_last_error=True)
    func = kernel32.SetThreadExecutionState
    func.argtypes = [ctypes.c_uint]
    func.restype = ctypes.c_uint
    return int(func(flags))


class SystemAwakeGuard:
    """Hold a system-required execution state on one dedicated Windows thread.

    ``SetThreadExecutionState`` is thread-scoped.  A dedicated long-lived thread
    guarantees the same thread both establishes and clears the continuous state.
    Non-Windows platforms are an intentional no-op.
    """

    def __init__(
        self,
        *,
        enabled: bool | None = None,
        setter: ExecutionStateSetter | None = None,
        refresh_seconds: float = DEFAULT_REFRESH_SECONDS,
    ) -> None:
        self.enabled = IS_WINDOWS if enabled is None else bool(enabled)
        self._setter = setter or _windows_execution_state_setter
        self._refresh_seconds = max(1.0, float(refresh_seconds))
        self._stop = threading.Event()
        self._ready = threading.Event()
        self._thread: threading.Thread | None = None
        self._lock = threading.Lock()
        self._active = False
        self._last_error = ""

    @property
    def active(self) -> bool:
        with self._lock:
            return self._active

    @property
    def last_error(self) -> str:
        with self._lock:
            return self._last_error

    @property
    def running(self) -> bool:
        thread = self._thread
        return bool(thread is not None and thread.is_alive())

    def _set_result(self, *, active: bool, error: str = "") -> None:
        with self._lock:
            self._active = active
            self._last_error = error

    def start(self) -> bool:
        """Start/refresh the guard and return whether the first request succeeded."""
        if not self.enabled:
            return True
        if self.running:
            return self.active
        self._stop = threading.Event()
        self._ready = threading.Event()
        self._thread = threading.Thread(
            target=self._run,
            name="MCPDevBridge-system-awake",
            daemon=True,
        )
        self._thread.start()
        self._ready.wait(timeout=2.0)
        return self.active

    def stop(self) -> None:
        thread = self._thread
        if thread is None:
            return
        self._stop.set()
        if thread is not threading.current_thread():
            thread.join(timeout=3.0)
        self._thread = None
        self._set_result(active=False)

    def _run(self) -> None:
        flags = ES_CONTINUOUS | ES_SYSTEM_REQUIRED
        first = True
        try:
            while not self._stop.is_set():
                try:
                    previous = self._setter(flags)
                    if previous == 0:
                        get_last_error = getattr(ctypes, "get_last_error", None)
                        if callable(get_last_error):
                            raw_error = get_last_error()
                            err = raw_error if isinstance(raw_error, int) else 0
                        else:
                            err = 0
                        self._set_result(
                            active=False,
                            error=f"SetThreadExecutionState failed (winerror={err})",
                        )
                    else:
                        self._set_result(active=True)
                except Exception as exc:  # noqa: BLE001 - OS boundary
                    self._set_result(active=False, error=f"{type(exc).__name__}: {exc}")
                finally:
                    if first:
                        self._ready.set()
                        first = False
                if self._stop.wait(self._refresh_seconds):
                    break
        finally:
            with contextlib.suppress(Exception):
                self._setter(ES_CONTINUOUS)
            self._set_result(active=False)
            self._ready.set()


__all__ = [
    "DEFAULT_REFRESH_SECONDS",
    "ES_CONTINUOUS",
    "ES_SYSTEM_REQUIRED",
    "SystemAwakeGuard",
]
