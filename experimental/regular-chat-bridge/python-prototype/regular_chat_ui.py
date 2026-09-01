"""User-facing Regular Chat control tab.

Keep provider internals (selectors, cookies, protocol details) out of this UI.  The
widget only reports actionable browser/login/session state and delegates all
blocking work to the global Qt thread pool.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from PySide6.QtCore import QObject, QThreadPool, Signal
from PySide6.QtWidgets import (
    QCheckBox,
    QComboBox,
    QGroupBox,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QMessageBox,
    QPushButton,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
)

from .regular_chat import (
    RegularChatClient,
    install_managed_browser,
    managed_browser_ready,
    reset_profile,
    resolve_regular_chat_paths,
)


class _Signals(QObject):
    done = Signal(object)


_SIGNAL_GUARDS: set[_Signals] = set()


def _friendly_error(value: object) -> str:
    if isinstance(value, BaseException):
        message = str(value).strip()
        return message or type(value).__name__
    return str(value)


def _run_async(fn: Callable[[], Any], callback: Callable[[Any], None]) -> None:
    signals = _Signals()
    _SIGNAL_GUARDS.add(signals)

    def finish(result: Any) -> None:
        try:
            callback(result)
        finally:
            _SIGNAL_GUARDS.discard(signals)

    signals.done.connect(finish)

    def target() -> None:
        try:
            result = fn()
        except Exception as exc:  # noqa: BLE001 - displayed as an actionable UI failure
            result = exc
        signals.done.emit(result)

    QThreadPool.globalInstance().start(target)


_ENGINE_ITEMS = [
    ("managed-chromium", "独立 Chromium（推荐）"),
    ("msedge", "独立 Edge 环境"),
    ("chrome", "独立 Chrome 环境"),
]


class RegularChatWidget(QWidget):
    """Small, optional UI surface for the independent ChatGPT browser profile."""

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._client = RegularChatClient()
        self._busy = False
        self._managed_ready: bool | None = None
        self._build_ui()
        self._refresh_local_state()

    @property
    def is_running(self) -> bool:
        return self._client.is_running

    def stop_controller(self) -> None:
        self._client.stop()

    def _build_ui(self) -> None:
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 8, 0, 8)
        layout.setSpacing(10)

        profile_box = QGroupBox("独立登录环境")
        profile_layout = QVBoxLayout(profile_box)
        profile_layout.setSpacing(8)

        engine_row = QHBoxLayout()
        engine_row.addWidget(QLabel("浏览器:"))
        self.engine_combo = QComboBox()
        for value, label in _ENGINE_ITEMS:
            self.engine_combo.addItem(label, value)
        self.engine_combo.currentIndexChanged.connect(self._engine_changed)
        engine_row.addWidget(self.engine_combo)
        engine_row.addStretch(1)
        profile_layout.addLayout(engine_row)

        self.browser_status = QLabel()
        self.login_status = QLabel("登录状态：尚未检查")
        self.controller_status = QLabel("控制服务：未启动")
        profile_layout.addWidget(self.browser_status)
        profile_layout.addWidget(self.login_status)
        profile_layout.addWidget(self.controller_status)
        self.auto_resume_checkbox = QCheckBox("自动恢复已绑定的未完成长任务")
        self.auto_resume_checkbox.setChecked(True)
        self.auto_resume_checkbox.setToolTip(
            "仅恢复已经存在 provider-session 的 durable long-run；不会为其它任务静默新建聊天。"
        )
        profile_layout.addWidget(self.auto_resume_checkbox)

        buttons = QHBoxLayout()
        self.login_button = QPushButton("打开登录浏览器")
        self.login_button.clicked.connect(self._open_login_browser)
        self.install_button = QPushButton("安装/修复独立浏览器")
        self.install_button.clicked.connect(self._install_browser)
        self.doctor_button = QPushButton("诊断")
        self.doctor_button.clicked.connect(self._doctor)
        self.reset_button = QPushButton("重置独立登录环境")
        self.reset_button.clicked.connect(self._reset_profile)
        for button in (
            self.login_button,
            self.install_button,
            self.doctor_button,
            self.reset_button,
        ):
            buttons.addWidget(button)
        buttons.addStretch(1)
        profile_layout.addLayout(buttons)
        layout.addWidget(profile_box)

        session_box = QGroupBox("当前自动续接会话")
        session_layout = QVBoxLayout(session_box)
        self.sessions = QTableWidget(0, 4)
        self.sessions.setHorizontalHeaderLabels(["任务", "工作区", "阶段", "会话标识"])
        self.sessions.verticalHeader().setVisible(False)
        self.sessions.setEditTriggers(QTableWidget.EditTrigger.NoEditTriggers)
        self.sessions.horizontalHeader().setSectionResizeMode(0, QHeaderView.ResizeMode.Stretch)
        self.sessions.horizontalHeader().setSectionResizeMode(1, QHeaderView.ResizeMode.ResizeToContents)
        self.sessions.horizontalHeader().setSectionResizeMode(2, QHeaderView.ResizeMode.ResizeToContents)
        self.sessions.horizontalHeader().setSectionResizeMode(3, QHeaderView.ResizeMode.ResizeToContents)
        session_layout.addWidget(self.sessions)
        self.refresh_button = QPushButton("刷新状态")
        self.refresh_button.clicked.connect(self._refresh_controller_status)
        session_layout.addWidget(self.refresh_button)
        layout.addWidget(session_box)
        layout.addStretch(1)

    def _engine(self) -> str:
        return str(self.engine_combo.currentData() or "managed-chromium")

    def _set_busy(self, value: bool) -> None:
        self._busy = value
        for button in (
            self.login_button,
            self.install_button,
            self.doctor_button,
            self.reset_button,
            self.refresh_button,
        ):
            button.setEnabled(not value)
        self.engine_combo.setEnabled(not value)

    def _engine_changed(self) -> None:
        old = self._client
        self._client = RegularChatClient(engine=self._engine())
        self._managed_ready = None
        self._refresh_local_state()
        if old.is_running:
            _run_async(old.abort, lambda _result: None)

    def _refresh_local_state(self) -> None:
        engine = self._engine()
        if engine == "managed-chromium":
            if self._managed_ready is True:
                text = "独立浏览器：已安装"
            elif self._managed_ready is False:
                text = "独立浏览器：尚未安装（首次启用时下载固定版本）"
            else:
                text = "独立浏览器：尚未检查（打开登录浏览器时自动确认）"
            self.browser_status.setText(text)
            self.install_button.setVisible(True)
        else:
            self.browser_status.setText("独立浏览器：使用系统浏览器程序 + DevBridge 独立登录环境")
            self.install_button.setVisible(False)
        self.controller_status.setText(
            "控制服务：正在运行" if self._client.is_running else "控制服务：未启动"
        )

    def _install_browser(self) -> None:
        if self._busy:
            return
        self._set_busy(True)
        self.browser_status.setText("独立浏览器：正在安装/校验…")

        def done(result: Any) -> None:
            self._set_busy(False)
            if isinstance(result, Exception):
                self._managed_ready = False
                self.browser_status.setText("独立浏览器：安装失败，可点击“诊断”查看下一步")
                QMessageBox.warning(self, "独立浏览器安装失败", _friendly_error(result))
            else:
                self._managed_ready = True
                self._refresh_local_state()

        _run_async(install_managed_browser, done)

    def _open_login_browser(self) -> None:
        if self._busy:
            return
        if self._engine() != "managed-chromium":
            self._launch_login()
            return
        if self._managed_ready is True:
            self._launch_login()
            return
        self._set_busy(True)
        self.browser_status.setText("独立浏览器：正在确认运行环境…")

        def done(result: Any) -> None:
            self._set_busy(False)
            self._managed_ready = bool(result) if not isinstance(result, Exception) else False
            if self._managed_ready:
                self._refresh_local_state()
                self._launch_login()
            else:
                self._install_browser_then_login()

        _run_async(managed_browser_ready, done)

    def _install_browser_then_login(self) -> None:
        self._set_busy(True)
        self.browser_status.setText("独立浏览器：正在首次安装…")

        def done(result: Any) -> None:
            self._set_busy(False)
            if isinstance(result, Exception):
                self._managed_ready = False
                self.browser_status.setText("独立浏览器：安装失败")
                QMessageBox.warning(self, "独立浏览器安装失败", _friendly_error(result))
                return
            self._managed_ready = True
            self._refresh_local_state()
            self._launch_login()

        _run_async(install_managed_browser, done)

    def _launch_login(self) -> None:
        self._set_busy(True)
        self.login_status.setText("登录状态：正在打开独立登录环境…")

        def work() -> Any:
            return self._client.request("profile.login", timeout_seconds=60.0)

        def done(result: Any) -> None:
            self._set_busy(False)
            if isinstance(result, Exception):
                self.login_status.setText("登录状态：打开失败")
                QMessageBox.warning(self, "无法打开登录浏览器", _friendly_error(result))
                return
            data = result if isinstance(result, dict) else {}
            authenticated = str(data.get("authenticatedUi", "unknown"))
            if authenticated == "yes":
                self.login_status.setText("登录状态：可用")
            elif authenticated == "no":
                self.login_status.setText("登录状态：请在已打开的窗口中本人完成登录")
            else:
                self.login_status.setText("登录状态：请在已打开的窗口中确认账号状态")
            self.controller_status.setText("控制服务：正在运行")
            self._populate_sessions(data.get("activeRuns", []))

        _run_async(work, done)

    def _refresh_controller_status(self) -> None:
        if self._busy:
            return
        if not self._client.is_running:
            self.controller_status.setText("控制服务：未启动")
            self._populate_sessions([])
            return
        self._set_busy(True)

        def done(result: Any) -> None:
            self._set_busy(False)
            if isinstance(result, Exception):
                self.controller_status.setText("控制服务：状态异常，已保留可恢复会话")
                return
            data = result if isinstance(result, dict) else {}
            self.controller_status.setText("控制服务：正在运行")
            self._populate_sessions(data.get("activeRuns", []))

        _run_async(lambda: self._client.request("controller.status", timeout_seconds=15.0), done)

    def _populate_sessions(self, rows: Any) -> None:
        records = rows if isinstance(rows, list) else []
        self.sessions.setRowCount(len(records))
        for row_index, record in enumerate(records):
            item = record if isinstance(record, dict) else {}
            values = [
                str(item.get("runId", "")),
                str(item.get("workspaceId", ""))[:12],
                str(item.get("state", "")),
                str(item.get("conversationRefHash", ""))[:12],
            ]
            for column, value in enumerate(values):
                self.sessions.setItem(row_index, column, QTableWidgetItem(value))

    def _doctor(self) -> None:
        if self._busy:
            return
        self._set_busy(True)

        def work() -> dict[str, Any]:
            paths = resolve_regular_chat_paths()
            result: dict[str, Any] = {
                "controller": paths.controller_entry.is_file(),
                "browser": managed_browser_ready() if self._engine() == "managed-chromium" else True,
                "running": self._client.is_running,
            }
            if self._client.is_running:
                result["status"] = self._client.request("controller.status", timeout_seconds=15.0)
            return result

        def done(result: Any) -> None:
            self._set_busy(False)
            if isinstance(result, Exception):
                QMessageBox.warning(self, "Regular Chat 诊断", _friendly_error(result))
                return
            data = result if isinstance(result, dict) else {}
            lines = [
                f"控制组件：{'正常' if data.get('controller') else '需要修复'}",
                f"浏览器环境：{'正常' if data.get('browser') else '尚未安装'}",
                f"控制服务：{'正在运行' if data.get('running') else '未启动'}",
            ]
            QMessageBox.information(self, "Regular Chat 诊断", "\n".join(lines))
            self._refresh_local_state()

        _run_async(work, done)

    def _reset_profile(self) -> None:
        if self._busy:
            return
        answer = QMessageBox.question(
            self,
            "重置独立登录环境",
            "这会删除当前 DevBridge 独立登录环境，并需要重新登录 ChatGPT。不会删除你的日常浏览器资料。是否继续？",
        )
        if answer != QMessageBox.StandardButton.Yes:
            return
        self._set_busy(True)
        old = self._client

        def work() -> Any:
            old.stop()
            return reset_profile(self._engine())

        def done(result: Any) -> None:
            self._client = RegularChatClient(engine=self._engine())
            self._set_busy(False)
            if isinstance(result, Exception):
                QMessageBox.warning(self, "重置失败", _friendly_error(result))
            else:
                self.login_status.setText("登录状态：需要重新登录")
                self._populate_sessions([])
                self._refresh_local_state()

        _run_async(work, done)
