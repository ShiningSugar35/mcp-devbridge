"""Windows notification-area integration for the portable Tk window."""

from __future__ import annotations

import os
from collections.abc import Callable
from concurrent.futures import Future
from contextlib import suppress
from threading import Thread
from typing import TYPE_CHECKING, Any

import win32api  # pyright: ignore[reportMissingImports]
import win32con  # pyright: ignore[reportMissingImports]

if TYPE_CHECKING:
    win32gui: Any
else:
    import win32gui

_TRAY_EVENT = win32con.WM_APP + 1
_SHOW = 1
_EXIT = 2


class LiveTray:
    def __init__(self, show_window: Callable[[], object], quit_application: Callable[[], object]) -> None:
        self.show_window = show_window
        self.quit_application = quit_application
        self._taskbar_created = win32gui.RegisterWindowMessage("TaskbarCreated")
        self._ready: Future[int] = Future()
        self._thread = Thread(target=self._run, name="mcp-live-tray", daemon=True)
        self._thread.start()

    def _run(self) -> None:
        window = win32gui.WNDCLASS()
        window.hInstance = win32api.GetModuleHandle(None)
        window.lpszClassName = f"MCP-Live-Tray-{os.getpid()}-{id(self)}"
        window.lpfnWndProc = self._receive
        atom = hwnd = 0
        try:
            atom = win32gui.RegisterClass(window)
            hwnd = win32gui.CreateWindow(
                atom, "MCP Live tray", 0, 0, 0, 0, 0, 0, 0, window.hInstance, None,
            )
            self._add_icon(hwnd)
            self._ready.set_result(hwnd)
            win32gui.PumpMessages()
        except win32gui.error as exc:
            if not self._ready.done():
                self._ready.set_exception(RuntimeError(f"Windows 托盘注册失败（{exc.winerror}）。"))
            else:
                self.show_window()
        finally:
            if hwnd and win32gui.IsWindow(hwnd):
                win32gui.DestroyWindow(hwnd)
            if atom:
                win32gui.UnregisterClass(window.lpszClassName, window.hInstance)

    def _add_icon(self, hwnd: int) -> None:
        icon = win32gui.LoadIcon(0, win32con.IDI_APPLICATION)
        flags = win32gui.NIF_ICON | win32gui.NIF_MESSAGE | win32gui.NIF_TIP
        win32gui.Shell_NotifyIcon(win32gui.NIM_ADD, (hwnd, 0, flags, _TRAY_EVENT, icon, "MCP Live"))

    def show(self) -> None:
        self._ready.result(timeout=4)
        if not self._thread.is_alive():
            raise RuntimeError("Windows 托盘已退出。")

    def _receive(self, hwnd: int, message: int, wparam: int, lparam: int) -> int:
        if message == self._taskbar_created and self._ready.done():
            try:
                self._add_icon(hwnd)
            except win32gui.error:
                self.show_window()
        elif message == _TRAY_EVENT:
            if lparam in (win32con.WM_LBUTTONUP, win32con.WM_LBUTTONDBLCLK):
                self.show_window()
            elif lparam == win32con.WM_RBUTTONUP:
                self._menu(hwnd)
        elif message == win32con.WM_DESTROY:
            with suppress(win32gui.error):
                win32gui.Shell_NotifyIcon(win32gui.NIM_DELETE, (hwnd, 0))
            win32gui.PostQuitMessage(0)
        else:
            return win32gui.DefWindowProc(hwnd, message, wparam, lparam)
        return 0

    def _menu(self, hwnd: int) -> None:
        menu = win32gui.CreatePopupMenu()
        try:
            win32gui.AppendMenu(menu, win32con.MF_STRING, _SHOW, "显示主窗口")
            win32gui.AppendMenu(menu, win32con.MF_STRING, _EXIT, "退出 MCP Live")
            x, y = win32gui.GetCursorPos()
            with suppress(win32gui.error):
                win32gui.SetForegroundWindow(hwnd)
            command = win32gui.TrackPopupMenu(
                menu, win32con.TPM_RETURNCMD | win32con.TPM_NONOTIFY, x, y, 0, hwnd, None,
            )
            win32gui.PostMessage(hwnd, win32con.WM_NULL, 0, 0)
        finally:
            win32gui.DestroyMenu(menu)
        if command == _SHOW:
            self.show_window()
        elif command == _EXIT:
            self.quit_application()

    def close(self) -> None:
        def post_close(ready: Future[int]) -> None:
            with suppress(RuntimeError, win32gui.error):
                win32gui.PostMessage(ready.result(), win32con.WM_CLOSE, 0, 0)

        self._ready.add_done_callback(post_close)
