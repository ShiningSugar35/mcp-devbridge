"""Windows notification-area integration for the portable Tk window."""

from __future__ import annotations

import os
from collections.abc import Callable
from contextlib import suppress
from typing import TYPE_CHECKING, Any

import win32api  # pyright: ignore[reportMissingImports]
import win32con  # pyright: ignore[reportMissingImports]

if TYPE_CHECKING:
    # Pywin32 accepts tuples and mutable WNDCLASS values at runtime.
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
        self._shown = False
        self._taskbar_created = win32gui.RegisterWindowMessage("TaskbarCreated")
        window = win32gui.WNDCLASS()
        window.hInstance = win32api.GetModuleHandle(None)
        window.lpszClassName = f"MCP-Live-Tray-{os.getpid()}-{id(self)}"
        window.lpfnWndProc = self._receive
        self._class_name = window.lpszClassName
        self._instance = window.hInstance
        atom = win32gui.RegisterClass(window)
        self._window = win32gui.CreateWindow(
            atom, "MCP Live tray", 0, 0, 0, 0, 0, 0, 0, self._instance, None,
        )

    def _icon(self) -> tuple:
        icon = win32gui.LoadIcon(0, win32con.IDI_APPLICATION)
        flags = win32gui.NIF_ICON | win32gui.NIF_MESSAGE | win32gui.NIF_TIP
        return self._window, 0, flags, _TRAY_EVENT, icon, "MCP Live"

    def show(self) -> None:
        if not self._shown:
            win32gui.Shell_NotifyIcon(win32gui.NIM_ADD, self._icon())
            self._shown = True

    def _receive(self, hwnd: int, message: int, wparam: int, lparam: int) -> int:
        if message == self._taskbar_created and self._shown:
            self._shown = False
            with suppress(win32gui.error):
                self.show()
        elif message == _TRAY_EVENT:
            if lparam in (win32con.WM_LBUTTONUP, win32con.WM_LBUTTONDBLCLK):
                self.show_window()
            elif lparam == win32con.WM_RBUTTONUP:
                self._menu()
        else:
            return win32gui.DefWindowProc(hwnd, message, wparam, lparam)
        return 0

    def _menu(self) -> None:
        menu = win32gui.CreatePopupMenu()
        try:
            win32gui.AppendMenu(menu, win32con.MF_STRING, _SHOW, "显示主窗口")
            win32gui.AppendMenu(menu, win32con.MF_STRING, _EXIT, "退出 MCP Live")
            x, y = win32gui.GetCursorPos()
            win32gui.SetForegroundWindow(self._window)
            command = win32gui.TrackPopupMenu(
                menu, win32con.TPM_RETURNCMD | win32con.TPM_NONOTIFY, x, y, 0, self._window, None,
            )
            win32gui.PostMessage(self._window, win32con.WM_NULL, 0, 0)
        finally:
            win32gui.DestroyMenu(menu)
        if command == _SHOW:
            self.show_window()
        elif command == _EXIT:
            self.quit_application()

    def close(self) -> None:
        with suppress(win32gui.error):
            win32gui.Shell_NotifyIcon(win32gui.NIM_DELETE, (self._window, 0))
        win32gui.DestroyWindow(self._window)
        win32gui.UnregisterClass(self._class_name, self._instance)
