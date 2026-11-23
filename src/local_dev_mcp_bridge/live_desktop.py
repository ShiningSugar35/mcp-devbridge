"""Small Windows-friendly interface for the portable MCP Live client."""

from __future__ import annotations

import asyncio
import socket
import threading
import tkinter as tk
from collections import deque
from concurrent.futures import Future
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from queue import Empty, SimpleQueue
from tkinter import filedialog, messagebox, ttk
from typing import TYPE_CHECKING

import httpx

from .live_agent import _connection_error, _pairing_code, _service_url, serve_agent
from .live_diagnostics import check_connection, diagnostic_log_path, load_events, save_events
from .live_profile import load_profile, save_profile
from .live_protocol import MAX_FOLDERS, folder_alias
from .models import PermissionMode
from .platform_support import IS_WINDOWS

if TYPE_CHECKING:
    from .live_tray import LiveTray

DEFAULT_ENDPOINT = "https://live.shiningsugar.shop"
_PERMISSION_NAMES: dict[PermissionMode, str] = {
    "read_only": "只读", "workspace": "项目工作区", "system": "完全访问",
}
_PERMISSION_BY_NAME = {label: mode for mode, label in _PERMISSION_NAMES.items()}  # type: dict[str, PermissionMode]

_BACKGROUND = "#F1F5FA"
_SURFACE = "#FFFFFF"
_INK = "#17243A"
_MUTED = "#52627A"
_BORDER = "#D6E0ED"
_BLUE = "#245BD7"
_GREEN = "#17744C"
_RED = "#AC2B37"

_Notice = tuple[str, str]


@dataclass(frozen=True, slots=True)
class _Connection:
    endpoint: str
    folders: dict[str, Path]
    name: str
    code: str = field(repr=False)
    permissions: dict[str, PermissionMode] | None = None
    windows_enabled: bool = False


def _run_session(
    options: _Connection, stop_request: Future[None], notices: SimpleQueue[_Notice]
) -> None:

    async def run() -> None:
        stop = asyncio.Event()
        request = asyncio.wrap_future(stop_request)
        request.add_done_callback(lambda _: stop.set())
        if stop_request.done():
            return
        await serve_agent(
            options.endpoint,
            options.code,
            options.name,
            options.folders,
            stop=stop,
            on_connected=lambda: notices.put(("connected", "")),
            on_device_key=lambda: notices.put(("key", "")),
            on_progress=lambda message: notices.put(("progress", message)),
            bridge_permissions=options.permissions,
            windows_enabled=options.windows_enabled,
        )

    error = ""
    try:
        asyncio.run(run())
    except httpx.HTTPStatusError as exc:
        error = _connection_error(exc)
    except httpx.TransportError:
        error = "无法连接服务，请检查网络和服务地址。"
    except (OSError, ValueError):
        error = "无法接入，请确认所选文件夹可以访问。"
    except RuntimeError as exc:
        error = str(exc) or "0.8.9 工具引擎启动失败。"
    except (KeyError, TypeError):
        error = "服务响应异常，请检查服务地址后重试。"
    finally:
        notices.put(("finished", error))


class LiveWindow:
    def __init__(self, root: tk.Tk, *, bridge_enabled: bool = True) -> None:
        self.root = root
        profile = load_profile()
        self.folders: dict[str, Path] = {}
        self.permissions: dict[str, PermissionMode] = {}
        self.bridge_enabled = bridge_enabled
        saved_folders = profile.get("folders")
        if isinstance(saved_folders, dict):
            for name, path in list(saved_folders.items())[:MAX_FOLDERS]:
                if isinstance(path, str) and path:
                    try:
                        self.folders[folder_alias(name)] = Path(path)
                    except ValueError:
                        continue
        saved_permissions = profile.get("permissions")
        if isinstance(saved_permissions, dict):
            self.permissions = {
                name: mode for name, mode in saved_permissions.items()
                if name in self.folders and mode in _PERMISSION_NAMES
            }
        self.permission_choice = tk.StringVar(master=root, value="只读")
        self.windows_enabled = tk.BooleanVar(master=root, value=bool(profile.get("windows_enabled")))
        self.code = tk.StringVar(master=root)
        self.endpoint = tk.StringVar(master=root, value=profile.get("endpoint") or DEFAULT_ENDPOINT)
        self.device_name = tk.StringVar(
            master=root, value=profile.get("device_name") or socket.gethostname()
        )
        self.key_hint = tk.StringVar(master=root)
        self.show_advanced = tk.BooleanVar(master=root)
        self.status = tk.StringVar(master=root, value="未连接")
        self.detail = tk.StringVar(master=root, value="添加要共享的文件夹，启动后 ChatGPT 可以读取其中的文本。")
        self._notices: SimpleQueue[_Notice] = SimpleQueue()
        self._stop_request: Future[None] | None = None
        self._pending_key: tuple[str, str] | None = None
        self._session_key: tuple[str, str] | None = None
        self._closing = False
        self.close_to_tray = tk.BooleanVar(master=root, value=IS_WINDOWS and profile.get("close_to_tray", True) is True)
        self._tray: LiveTray | None = None
        self._events: deque[str] = deque(load_events(), maxlen=200)
        self._diagnostic_window: tk.Toplevel | None = None
        self._diagnostic_view: tk.Text | None = None
        self._diagnostic_pending = False

        root.title("MCP Live")
        root.configure(background=_BACKGROUND)
        root.option_add("*Font", ("Microsoft YaHei UI", 10))
        root.protocol("WM_DELETE_WINDOW", self._close_window)
        self._build()
        root.update_idletasks()
        root.minsize(590, root.winfo_reqheight())
        self._refresh_folders()
        self._refresh_key_hint()
        self.browse_button.focus_set()
        root.after(50, self._consume_notices)

    def _build(self) -> None:
        style = ttk.Style(self.root)
        style.theme_use("clam")
        style.configure("Surface.TFrame", background=_SURFACE)
        style.configure("Surface.TLabel", background=_SURFACE, foreground=_INK)
        style.configure("Field.TEntry", padding=(8, 6), fieldbackground=_SURFACE)
        style.configure("Action.TButton", padding=(11, 7))
        style.configure(
            "Primary.TButton", padding=(20, 8), foreground="white", background=_BLUE, borderwidth=0
        )
        style.map(
            "Primary.TButton",
            background=[("disabled", "#D8E2F1"), ("active", "#184ABE")],
            foreground=[("disabled", "#5A6F8B"), ("active", "white")],
        )
        style.configure(
            "Advanced.TCheckbutton", background=_SURFACE, foreground=_MUTED, padding=(0, 3)
        )
        style.map("Advanced.TCheckbutton", background=[("active", _SURFACE)])

        page = tk.Frame(self.root, background=_BACKGROUND, padx=26, pady=22)
        page.pack(fill="both", expand=True)
        tk.Label(
            page,
            text="MCP Live",
            background=_BACKGROUND,
            foreground=_INK,
            font=("Microsoft YaHei UI", 22, "bold"),
        ).pack(anchor="w")
        tk.Label(
            page,
            text="选择这台电脑要共享给 ChatGPT 的文件夹。",
            background=_BACKGROUND,
            foreground=_MUTED,
        ).pack(anchor="w", pady=(3, 17))

        card = tk.Frame(page, background=_SURFACE, highlightbackground=_BORDER, highlightthickness=1)
        card.pack(fill="x")
        form = ttk.Frame(card, style="Surface.TFrame", padding=(18, 15))
        form.pack(fill="x")
        form.columnconfigure(0, weight=1)

        ttk.Label(form, text="授权文件夹", style="Surface.TLabel").grid(row=0, column=0, sticky="w")
        tools = ttk.Frame(form, style="Surface.TFrame")
        tools.grid(row=0, column=1, sticky="e")
        self.browse_button = ttk.Button(
            tools, text="添加文件夹…", command=self.choose_folder, style="Action.TButton"
        )
        self.browse_button.pack(side="left")
        self.remove_button = ttk.Button(
            tools, text="移除", command=self.remove_folders, style="Action.TButton"
        )
        self.remove_button.pack(side="left", padx=(6, 0))

        listing = ttk.Frame(form, style="Surface.TFrame")
        listing.grid(row=1, column=0, columnspan=2, sticky="ew", pady=(8, 13))
        listing.columnconfigure(0, weight=1)
        self.folder_table = ttk.Treeview(
            listing, columns=("name", "path", "permission"), show="headings", height=5, selectmode="extended"
        )
        self.folder_table.heading("name", text="名称", anchor="w")
        self.folder_table.heading("path", text="本机路径", anchor="w")
        self.folder_table.column("name", width=130, minwidth=90, stretch=False)
        self.folder_table.column("path", width=270, minwidth=150)
        self.folder_table.heading("permission", text="权限", anchor="w")
        self.folder_table.column("permission", width=105, stretch=False)
        self.folder_table.bind("<<TreeviewSelect>>", self._selection_changed)
        self.folder_table.grid(row=0, column=0, sticky="ew")
        scroll = ttk.Scrollbar(listing, orient="vertical", command=self.folder_table.yview)
        scroll.grid(row=0, column=1, sticky="ns")
        self.folder_table.configure(yscrollcommand=scroll.set)

        permission_row = ttk.Frame(form, style="Surface.TFrame")
        permission_row.grid(row=2, column=0, columnspan=2, sticky="w", pady=(0, 9))
        self.permission_picker = ttk.Combobox(
            permission_row, values=tuple(_PERMISSION_NAMES.values()),
            textvariable=self.permission_choice, state="readonly", width=12,
        )
        self.permission_picker.pack(side="left")
        self.permission_button = ttk.Button(
            permission_row, text="设置所选文件夹权限",
            command=self._set_selected_permissions, style="Action.TButton",
        )
        self.permission_button.pack(side="left", padx=(8, 0))
        ttk.Label(form, text="设备密钥", style="Surface.TLabel").grid(row=3, column=0, sticky="w")
        self.code_entry = ttk.Entry(form, textvariable=self.code, show="•", style="Field.TEntry")
        self.code_entry.grid(row=4, column=0, columnspan=2, sticky="ew", pady=(5, 4))
        ttk.Label(form, textvariable=self.key_hint, style="Surface.TLabel").grid(
            row=5, column=0, columnspan=2, sticky="w", pady=(0, 7)
        )

        self.advanced_button = ttk.Checkbutton(
            form,
            text="高级设置",
            variable=self.show_advanced,
            command=self._toggle_advanced,
            style="Advanced.TCheckbutton",
        )
        self.advanced_button.grid(row=6, column=0, columnspan=2, sticky="w")
        self.advanced = ttk.Frame(form, style="Surface.TFrame")
        self.advanced.grid(row=7, column=0, columnspan=2, sticky="ew", pady=(4, 6))
        self.advanced.columnconfigure(0, weight=1)
        self.endpoint_entry = self._advanced_field("服务地址", self.endpoint, 0)
        self.name_entry = self._advanced_field("设备名称", self.device_name, 2)
        self.forget_button = ttk.Button(
            self.advanced, text="清除当前窗口的密钥", command=self.forget_key, style="Action.TButton"
        )
        self.forget_button.grid(row=4, column=0, sticky="w", pady=(4, 0))
        self.windows_button = ttk.Checkbutton(
            self.advanced, text="启用原版 Windows 控制",
            variable=self.windows_enabled, style="Advanced.TCheckbutton",
        )
        self.windows_button.grid(row=5, column=0, sticky="w", pady=(4, 0))
        if IS_WINDOWS:
            ttk.Checkbutton(
                self.advanced, text="关闭窗口时最小化到托盘", variable=self.close_to_tray,
                command=self._store_profile, style="Advanced.TCheckbutton",
            ).grid(row=6, column=0, sticky="w", pady=(4, 0))
        self.endpoint_entry.bind("<FocusOut>", lambda _: self._refresh_key_hint())
        self.advanced.grid_remove()

        footer = tk.Frame(page, background=_BACKGROUND, pady=12)
        footer.pack(fill="x")
        self.status_label = tk.Label(
            footer,
            textvariable=self.status,
            background=_BACKGROUND,
            foreground=_MUTED,
            font=("Microsoft YaHei UI", 11, "bold"),
        )
        self.status_label.pack(side="left")
        ttk.Button(footer, text="测试与日志", command=self._open_diagnostics).pack(side="left", padx=8)
        self.start_button = ttk.Button(
            footer, text="启动共享", command=self.start, style="Primary.TButton"
        )
        self.start_button.pack(side="right")
        self.stop_button = ttk.Button(
            footer, text="断开", command=self.stop, style="Action.TButton", state="disabled"
        )
        self.stop_button.pack(side="right", padx=(0, 8))
        note = tk.Label(
            page,
            textvariable=self.detail,
            background=_BACKGROUND,
            foreground=_MUTED,
            justify="left",
            anchor="w",
            wraplength=464,
        )
        note.pack(fill="x")
        page.bind("<Configure>", lambda event: note.configure(wraplength=max(300, event.width - 52)))

    def _advanced_field(self, label: str, value: tk.StringVar, row: int) -> ttk.Entry:
        ttk.Label(self.advanced, text=label, style="Surface.TLabel").grid(
            row=row, column=0, sticky="w"
        )
        entry = ttk.Entry(self.advanced, textvariable=value, style="Field.TEntry")
        entry.grid(row=row + 1, column=0, sticky="ew", pady=(4, 8))
        return entry

    def _toggle_advanced(self) -> None:
        if self.show_advanced.get():
            self.advanced.grid()
        else:
            self.advanced.grid_remove()

    def _store_profile(self) -> None:
        try:
            endpoint = _service_url(self.endpoint.get())
        except ValueError:
            endpoint = DEFAULT_ENDPOINT
        try:
            save_profile({
                "folders": {name: str(path) for name, path in self.folders.items()},
                "endpoint": endpoint,
                "device_name": self.device_name.get().strip(),
                "permissions": self.permissions,
                "windows_enabled": self.windows_enabled.get(),
                "close_to_tray": self.close_to_tray.get(),
            })
        except OSError:
            self._show("设置未保存", "当前选择可用，但无法保存到本机配置。", _RED)

    def _refresh_folders(self) -> None:
        self.folder_table.delete(*self.folder_table.get_children())
        for name, path in self.folders.items():
            mode = self.permissions.get(name, "read_only")
            self.folder_table.insert("", "end", iid=name, values=(name, str(path), _PERMISSION_NAMES[mode]))
        self.remove_button.configure(state="normal" if self.folders else "disabled")
        self._selection_changed()

    def _selection_changed(self, _event: tk.Event | None = None) -> None:
        selected = self.folder_table.selection()
        if len(selected) == 1:
            self.permission_choice.set(_PERMISSION_NAMES[self.permissions.get(selected[0], "read_only")])
        self.permission_button.configure(
            state="normal" if selected and self._stop_request is None else "disabled"
        )

    def _set_selected_permissions(self) -> None:
        if self._stop_request is not None:
            return
        mode = _PERMISSION_BY_NAME.get(self.permission_choice.get(), "read_only")
        selected = self.folder_table.selection()
        for alias in selected:
            self.permissions[alias] = mode
        self._refresh_folders()
        if selected:
            self.folder_table.selection_set(*selected)
        self._store_profile()

    def _refresh_key_hint(self) -> None:
        try:
            endpoint = _service_url(self.endpoint.get())
        except ValueError:
            endpoint = ""
        verified = self._session_key is not None and self._session_key[0] == endpoint
        self.key_hint.set("当前窗口已验证，可直接启动。" if verified else "密钥仅在当前窗口使用。")

    def choose_folder(self) -> None:
        if self._stop_request is not None:
            return
        if len(self.folders) >= MAX_FOLDERS:
            self._show("目录已达上限", "最多可以添加 32 个文件夹。", _RED)
            return
        selected = filedialog.askdirectory(parent=self.root, title="选择授权文件夹", mustexist=True)
        if not selected:
            return
        try:
            path = Path(selected).resolve(strict=True)
        except (OSError, RuntimeError):
            self._show("无法添加文件夹", "请确认该目录仍然可访问。", _RED)
            return
        for name, previous in self.folders.items():
            if previous == path:
                self.folder_table.selection_set(name)
                return
        basename = path.name.strip()[:73] or path.drive.rstrip(":") or "文件夹"
        try:
            basename = folder_alias(basename)
        except ValueError:
            basename = "文件夹"
        existing = {name.casefold() for name in self.folders}
        alias = basename
        suffix = 2
        while alias.casefold() in existing:
            alias = f"{basename}-{suffix}"
            suffix += 1
        self.folders[alias] = path
        self._refresh_folders()
        self.folder_table.selection_set(alias)
        self._store_profile()

    def remove_folders(self) -> None:
        if self._stop_request is not None:
            return
        for alias in self.folder_table.selection():
            self.folders.pop(alias, None)
            self.permissions.pop(alias, None)
        self._refresh_folders()
        self._store_profile()

    def forget_key(self) -> None:
        if self._stop_request is not None:
            return
        self._session_key = None
        self.code.set("")
        self._refresh_key_hint()

    def start(self) -> None:
        if self._stop_request is not None:
            return
        name = self.device_name.get().strip()
        try:
            if not self.folders:
                raise ValueError("请先添加要共享的文件夹。")
            if not name or len(name) > 80:
                raise ValueError("设备名称须为 1 至 80 个字符。")
            endpoint = _service_url(self.endpoint.get())
            supplied = self.code.get().strip()
            if not supplied and self._session_key is not None and self._session_key[0] == endpoint:
                supplied = self._session_key[1]
            permissions = dict(self.permissions) if self.bridge_enabled else None
            if permissions and "system" in permissions.values() and not messagebox.askokcancel(
                "启用完全访问",
                "完全访问允许按原 DevBridge 权限在本机执行系统级操作。是否继续启动这些项目？",
                parent=self.root,
                default=messagebox.CANCEL,
            ):
                return
            options = _Connection(
                endpoint, dict(self.folders), name, _pairing_code(supplied),
                permissions, self.windows_enabled.get(),
            )
        except ValueError as exc:
            self._show("请补全连接信息", str(exc), _RED)
            return

        self._pending_key = (options.endpoint, options.code)
        self.code.set("")
        self._store_profile()
        self._stop_request = Future()
        self._editing(False)
        detail = "正在启动本机工作区和 Live 连接。"
        if options.windows_enabled and options.permissions and any(
            mode != "read_only" for mode in options.permissions.values()
        ):
            detail = "正在准备本机引擎。首次启用 Windows 控制时可能需要下载运行组件。"
        self._show("正在连接", detail, _BLUE)
        threading.Thread(
            target=_run_session,
            args=(options, self._stop_request, self._notices),
            name="mcp-live-agent",
        ).start()

    def stop(self) -> None:
        if self._stop_request is None or self._stop_request.done():
            return
        self._stop_request.set_result(None)
        self.stop_button.configure(state="disabled")
        self._show("正在断开", "正在关闭本机接入。", _MUTED)

    def _close_window(self) -> None:
        if not self.close_to_tray.get() or not IS_WINDOWS:
            self.close()
            return
        try:
            from .live_tray import LiveTray

            if self._tray is None:
                self._tray = LiveTray(
                    lambda: self.root.after(0, self._restore_window),
                    lambda: self.root.after(0, self.close),
                )
            self._tray.show()
        except Exception:
            self._show("托盘不可用", "窗口保持打开。可在高级设置中关闭托盘选项。", _RED)
            return
        if self._diagnostic_window is not None and self._diagnostic_window.winfo_exists():
            self._diagnostic_window.withdraw()
        self.root.withdraw()

    def _restore_window(self) -> None:
        self.root.deiconify()
        self.root.lift()
        self.root.focus_force()

    def _record(self, message: str) -> None:
        keys = (self.code.get(), *(item[1] for item in (self._pending_key, self._session_key) if item))
        for key in keys:
            if key:
                message = message.replace(key, "[已隐藏]")
        self._events.append(f"{datetime.now().astimezone():%m-%d %H:%M:%S}  {message[:800]}")
        save_events(self._events)
        self._refresh_diagnostics()

    def _refresh_diagnostics(self) -> None:
        if self._diagnostic_view is None or not self._diagnostic_view.winfo_exists():
            return
        self._diagnostic_view.configure(state="normal")
        self._diagnostic_view.delete("1.0", "end")
        self._diagnostic_view.insert("1.0", "\n".join(self._events) or "暂无记录。")
        self._diagnostic_view.configure(state="disabled")
        self._diagnostic_view.see("end")

    def _open_diagnostics(self) -> None:
        if self._diagnostic_window is not None and self._diagnostic_window.winfo_exists():
            self._diagnostic_window.deiconify()
            self._diagnostic_window.lift()
            return
        dialog = tk.Toplevel(self.root)
        self._diagnostic_window = dialog
        dialog.title("连接测试与日志")
        dialog.geometry("630x340")
        bar = ttk.Frame(dialog, padding=8)
        bar.pack(fill="x")
        ttk.Button(bar, text="一键连接测试", command=self._test_connection).pack(side="left")
        ttk.Button(bar, text="复制日志", command=self._copy_diagnostics).pack(side="left", padx=8)
        ttk.Label(dialog, text=str(diagnostic_log_path()), wraplength=600).pack(fill="x", padx=8)
        self._diagnostic_view = tk.Text(dialog, wrap="word", padx=8, pady=8)
        self._diagnostic_view.pack(fill="both", expand=True, padx=8, pady=(0, 8))
        self._refresh_diagnostics()

    def _copy_diagnostics(self) -> None:
        self.root.clipboard_clear()
        self.root.clipboard_append("\n".join(self._events))

    def _test_connection(self) -> None:
        if self._diagnostic_pending:
            return
        self._diagnostic_pending = True
        endpoint = self.endpoint.get()
        self._record("开始测试本机依赖与公网中转。")

        def run() -> None:
            results = check_connection(endpoint)
            self._notices.put(("diagnostic", "\n".join(results)))

        threading.Thread(target=run, name="mcp-live-diagnostics", daemon=True).start()

    def _destroy(self) -> None:
        if self._tray is not None:
            self._tray.close()
            self._tray = None
        self.root.destroy()

    def close(self) -> None:
        if self._stop_request is None:
            self._session_key = None
            self._destroy()
            return
        self._closing = True
        self.stop()

    def _show(self, status: str, detail: str, color: str) -> None:
        self.status.set(status)
        self.detail.set(detail)
        self.status_label.configure(foreground=color)
        self._record(f"{status}。{detail}")

    def _editing(self, allow: bool) -> None:
        fields = (
            self.browse_button, self.remove_button, self.code_entry, self.advanced_button,
            self.endpoint_entry, self.name_entry, self.forget_button, self.start_button,
            self.windows_button,
        )
        state = "normal" if allow else "disabled"
        for widget in fields:
            widget.configure(state=state)
        if allow and not self.folders:
            self.remove_button.configure(state="disabled")
        self.permission_picker.configure(state="readonly" if allow else "disabled")
        self._selection_changed()
        self.stop_button.configure(state="disabled" if allow else "normal")

    def _consume_notices(self) -> None:
        while True:
            try:
                kind, message = self._notices.get_nowait()
            except Empty:
                break
            if kind == "diagnostic":
                self._diagnostic_pending = False
                for line in message.splitlines():
                    self._record(line)
                continue
            if self._stop_request is None:
                continue
            if kind == "progress":
                self._record(message)
                continue
            if kind == "key":
                self._session_key = self._pending_key
                self._pending_key = None
                self._refresh_key_hint()
                continue
            if kind == "connected":
                if not self._stop_request.done():
                    self._show("已连接", "现在可以在 ChatGPT 里选 @MCP Live。", _GREEN)
                continue

            stopped = self._stop_request.done()
            self._stop_request = None
            self._pending_key = None
            if self._closing:
                self._session_key = None
                self._destroy()
                return
            self._editing(True)
            if stopped:
                self._show("已断开", "所有文件夹已停止共享。", _MUTED)
            elif message:
                self._show("连接失败", message, _RED)
            else:
                self._show("连接已结束", "如需继续使用，请重新启动共享。", _MUTED)
        self.root.after(50, self._consume_notices)


def main() -> int:
    root = tk.Tk()
    LiveWindow(root)
    root.mainloop()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
