"""Desktop control window (PySide6, Chinese UI).

Single window that lets the user pick a project, configure permission mode /
connection method, start/stop the *service* (CodexPro engine + optional
Windows bridge + optional public tunnel, all orchestrated by
``ServiceCoordinator``), view status and URL, manage the bearer token and run
a local connection self-test.

All blocking work (process lifecycle, engine readiness, HTTP health,
self-test) runs on QThreadPool workers so the UI never freezes.
"""

from __future__ import annotations

import datetime
import os
import sys
from collections.abc import Callable
from pathlib import Path
from typing import Any

from PySide6.QtCore import QObject, Qt, QThreadPool, QTimer, Signal
from PySide6.QtGui import QFont
from PySide6.QtWidgets import (
    QApplication,
    QCheckBox,
    QComboBox,
    QFileDialog,
    QFormLayout,
    QGroupBox,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QLineEdit,
    QMainWindow,
    QMessageBox,
    QPushButton,
    QTableWidget,
    QTableWidgetItem,
    QTabWidget,
    QVBoxLayout,
    QWidget,
)

from . import APP_NAME, __version__
from .app_state import ServiceCoordinator, StartOptions
from .audit import AuditQuery, available_tool_names, query_logs
from .backend_manager import current_access_token, regenerate_access_token
from .config_store import (
    load_app_config,
    load_projects,
    save_app_config,
    suggest_commands,
    upsert_project,
)
from .engines import CODEXPRO_LOCAL_PORT, EngineState
from .models import ProjectConfig, git_field_error
from .oauth_provider import get_or_create_gemini_client
from .secrets import SecretsStore, generate_token
from .selftest import SelftestResult, run_selftest
from .tunnel_manager import ConnectionMethod

PERMISSION_MODES = [("workspace", "项目全权限（默认）"), ("read_only", "只读模式"), ("system", "完全访问（危险）")]
CONNECTION_METHODS = [
    ConnectionMethod.LOCAL,
    ConnectionMethod.CLOUDFLARE,
    ConnectionMethod.NGROK,
    ConnectionMethod.QUICK,
]
BRIDGE_TOKEN_CRED_NAME = "LocalDevMCPBridge/WindowsBridgeToken"
TUNNEL_TOKEN_CRED_NAME = "LocalDevMCPBridge/CloudflareTunnelToken"
GEMINI_LAST_URI_CRED_NAME = "LocalDevMCPBridge/OAuthGeminiLastUri"


class _Signals(QObject):
    done = Signal(object)
    coord_event = Signal(object, object)  # state, message


def _run_async(fn: Callable[[], Any], callback: Callable[[Any], None]) -> None:
    """Run fn on the global thread pool; callback(result) on the GUI thread."""
    signals = _Signals()
    signals.done.connect(callback)

    def target() -> None:
        try:
            result = fn()
        except Exception as exc:  # noqa: BLE001 - surface any failure to the UI
            result = exc
        signals.done.emit(result)

    QThreadPool.globalInstance().start(target)


def _bridge_token(ensure: bool = False) -> str:
    """Windows bridge token: persisted in the secret store, auto-created."""
    store = SecretsStore()
    value = store.get(BRIDGE_TOKEN_CRED_NAME)
    if not value and ensure:
        value = generate_token(256)
        store.set(BRIDGE_TOKEN_CRED_NAME, value)
    return value or ""


def _tunnel_token_default() -> str:
    """Last-used Cloudflare tunnel token (remembered across starts)."""
    return SecretsStore().get(TUNNEL_TOKEN_CRED_NAME) or ""


def _remember_tunnel_token(token: str) -> None:
    """Persist the tunnel token as the default for future launches."""
    token = token.strip()
    if token:
        SecretsStore().set(TUNNEL_TOKEN_CRED_NAME, token)


class MainWindow(QMainWindow):
    def __init__(self) -> None:
        super().__init__()
        self.coord = ServiceCoordinator()
        self._signals = _Signals()
        self._signals.coord_event.connect(self._on_coord_event)
        self.coord.listen(self._emit_coord_event)
        self._app_config = load_app_config()
        self._projects = load_projects()
        self._current_token = current_access_token() or ""
        self._bridge_token = _bridge_token()
        self._tunnel_token_default = _tunnel_token_default()
        self._timer = QTimer(self)
        self._timer.timeout.connect(self._poll_status)
        self._build_ui()
        self._refresh_project_list()
        self._load_active_project()
        self._sync_token_ui()
        self._poll_status()
        self._timer.start(3000)

    # ---------------------------------------------------------------- UI
    def _build_ui(self) -> None:
        self.setWindowTitle(f"{APP_NAME} v{__version__}")
        self.resize(820, 820)

        central = QWidget()
        self.setCentralWidget(central)
        root = QVBoxLayout(central)
        self.tabs = QTabWidget()
        root.addWidget(self.tabs)
        self.ctrl_tab = QWidget()
        self.ctrl_layout = QVBoxLayout(self.ctrl_tab)
        self.tabs.addTab(self.ctrl_tab, "控制")

        # --- project
        proj_box = QGroupBox("项目")
        proj_form = QFormLayout(proj_box)
        self.project_combo = QComboBox()
        self.project_combo.currentIndexChanged.connect(self._on_project_selected)
        self.browse_btn = QPushButton("添加/浏览…")
        self.browse_btn.setFixedWidth(110)
        self.browse_btn.clicked.connect(self._browse_project)
        row = QHBoxLayout()
        row.addWidget(self.project_combo, 1)
        row.addWidget(self.browse_btn)
        proj_form.addRow("选择项目:", row)
        self.root_label = QLabel("（尚未选择项目）")
        self.root_label.setWordWrap(True)
        proj_form.addRow("根目录:", self.root_label)
        self.ctrl_layout.addWidget(proj_box)

        # --- config: permission + connection + bridge
        cfg_box = QGroupBox("服务配置")
        cfg_form = QFormLayout(cfg_box)
        self.permission_combo = QComboBox()
        for _value, label in PERMISSION_MODES:
            self.permission_combo.addItem(label)
        cfg_form.addRow("权限模式:", self.permission_combo)

        self.connection_combo = QComboBox()
        for method in CONNECTION_METHODS:
            self.connection_combo.addItem(method.label(), method.value)
        self.connection_combo.currentIndexChanged.connect(self._on_connection_changed)
        cfg_form.addRow("连接方式:", self.connection_combo)

        self.hostname_edit = QLineEdit()
        self.hostname_edit.setPlaceholderText("例如 bridge.example.com")
        cfg_form.addRow("公网域名:", self.hostname_edit)

        self.cf_token_edit = QLineEdit()
        self.cf_token_edit.setEchoMode(QLineEdit.Password)
        self.cf_token_edit.setPlaceholderText("（可选）Cloudflare 隧道令牌；留空使用上次保存的令牌")
        if self._tunnel_token_default:
            self.cf_token_edit.setText(self._tunnel_token_default)
        self.cf_token_edit.textEdited.connect(self._on_tunnel_token_edited)
        cfg_form.addRow("隧道令牌:", self.cf_token_edit)
        token_hint = QLabel("新令牌输入后自动保存，下次启动自动填入（加密存储）。")
        token_hint.setStyleSheet("color: gray;")
        cfg_form.addRow("", token_hint)

        gemini_box = QGroupBox("Gemini OAuth 配置（静态客户端；不影响 DCR/Bearer）")
        gemini_form = QFormLayout(gemini_box)
        self._gemini_store = SecretsStore()
        self._gemini_secret = ""
        self.gemini_uri_edit = QLineEdit()
        self.gemini_uri_edit.setPlaceholderText("从 Gemini「Custom Connected App → Advanced Settings → Copy redirect URI」粘贴到此处")
        self.gemini_uri_edit.setText(self._gemini_store.get(GEMINI_LAST_URI_CRED_NAME) or "")
        gemini_form.addRow("Gemini Redirect URI:", self.gemini_uri_edit)

        self.gemini_gen_btn = QPushButton("生成Gemini凭证")
        self.gemini_gen_btn.clicked.connect(self._generate_gemini_credentials)
        gemini_form.addRow("", self.gemini_gen_btn)

        self.gemini_id_label = QLabel("—")
        self.gemini_id_label.setTextInteractionFlags(Qt.TextSelectableByMouse)
        self.gemini_id_copy = QPushButton("复制")
        id_row = QHBoxLayout()
        id_row.addWidget(self.gemini_id_label, 1)
        id_row.addWidget(self.gemini_id_copy)
        gemini_form.addRow("Client ID:", id_row)

        self.gemini_secret_label = QLabel("—")
        self.gemini_secret_label.setTextInteractionFlags(Qt.TextSelectableByMouse)
        self.gemini_secret_copy = QPushButton("复制")
        secret_row = QHBoxLayout()
        secret_row.addWidget(self.gemini_secret_label, 1)
        secret_row.addWidget(self.gemini_secret_copy)
        gemini_form.addRow("Client Secret:", secret_row)

        gemini_hint = QLabel(
            "每次点击「生成Gemini凭证」都会更新 client_secret（旧值立即失效）；client_id 复用不变。"
            "Secret 加密保存、掩码显示，仅「复制」进剪贴板。"
        )
        gemini_hint.setWordWrap(True)
        gemini_hint.setStyleSheet("color: gray;")
        gemini_form.addRow("", gemini_hint)

        self.gemini_id_copy.clicked.connect(lambda: self._copy_text(self.gemini_id_label.text()))
        self.gemini_secret_copy.clicked.connect(lambda: self._copy_text(self._gemini_secret))
        cfg_form.addRow("", gemini_box)

        if self.gemini_uri_edit.text():
            try:
                client_id, secret = get_or_create_gemini_client(self.gemini_uri_edit.text(), rotate_secret=False)
                self.gemini_id_label.setText(client_id)
                self._gemini_secret = secret
                self.gemini_secret_label.setText("•" * 16)
            except ValueError:
                pass

        self.bridge_check = QCheckBox("启用 Windows 控制桥接（uvx 子进程，令牌自动生成并加密保存）")
        cfg_form.addRow("", self.bridge_check)
        self.ctrl_layout.addWidget(cfg_box)

        # --- git settings (Phase 5)
        git_box = QGroupBox("Git 参数（可空）")
        git_form = QFormLayout(git_box)
        self.git_name_edit = QLineEdit()
        self.git_name_edit.setPlaceholderText("例如: johndoe（不含空格，可空）")
        git_form.addRow("user.name:", self.git_name_edit)
        self.git_email_edit = QLineEdit()
        self.git_email_edit.setPlaceholderText("例如: name@example.com")
        git_form.addRow("user.email:", self.git_email_edit)
        self.git_remote_edit = QLineEdit()
        self.git_remote_edit.setPlaceholderText("例如: origin（默认推送远程，可空）")
        git_form.addRow("默认推送远程:", self.git_remote_edit)
        self.git_branch_edit = QLineEdit()
        self.git_branch_edit.setPlaceholderText("例如: main（默认推送分支，可空）")
        git_form.addRow("默认推送分支:", self.git_branch_edit)
        self.git_save_btn = QPushButton("保存 Git 设置")
        self.git_save_btn.clicked.connect(self._save_git_settings)
        git_form.addRow("", self.git_save_btn)
        self.ctrl_layout.addWidget(git_box)

        # --- service control
        ctrl_box = QGroupBox("服务控制")
        ctrl_row = QHBoxLayout(ctrl_box)
        self.start_btn = QPushButton("启动服务")
        self.stop_btn = QPushButton("停止服务")
        self.restart_btn = QPushButton("重启服务")
        self.start_btn.clicked.connect(self._start_service)
        self.stop_btn.clicked.connect(self._stop_service)
        self.restart_btn.clicked.connect(self._restart_service)
        ctrl_row.addWidget(self.start_btn)
        ctrl_row.addWidget(self.stop_btn)
        ctrl_row.addWidget(self.restart_btn)
        ctrl_row.addStretch(1)
        self.ctrl_layout.addWidget(ctrl_box)

        # --- status
        self.status_label = QLabel("状态：未启动")
        self.status_label.setWordWrap(True)
        self.ctrl_layout.addWidget(self.status_label)

        # --- token / URL
        tok_box = QGroupBox("访问令牌与 MCP 地址")
        tok_layout = QVBoxLayout(tok_box)
        self.token_label = QLabel("令牌：未生成")
        self.token_label.setWordWrap(True)
        self.token_label.setTextInteractionFlags(Qt.TextSelectableByMouse)
        self.url_label = QLabel("MCP 地址：http://127.0.0.1:8765/mcp（仅本机）")
        self.url_label.setWordWrap(True)
        self.url_label.setTextInteractionFlags(Qt.TextSelectableByMouse)
        tok_row = QHBoxLayout()
        self.token_copy_btn = QPushButton("复制令牌")
        self.token_regenerate_btn = QPushButton("重新生成令牌")
        self.url_copy_btn = QPushButton("复制 MCP 地址")
        self.token_copy_btn.clicked.connect(lambda: self._copy_to_clipboard(self._current_token))
        self.token_regenerate_btn.clicked.connect(self._regenerate_token)
        self.url_copy_btn.clicked.connect(lambda: self._copy_to_clipboard(self._display_url()))
        tok_row.addWidget(self.token_copy_btn)
        tok_row.addWidget(self.token_regenerate_btn)
        tok_row.addWidget(self.url_copy_btn)
        tok_row.addStretch(1)
        tok_layout.addWidget(self.token_label)
        tok_layout.addWidget(self.url_label)
        tok_layout.addLayout(tok_row)
        self.ctrl_layout.addWidget(tok_box)

        # --- self test
        test_group = QGroupBox("连接自测")
        test_box = QVBoxLayout(test_group)
        self.test_btn = QPushButton("运行连接自测")
        self.test_btn.clicked.connect(self._run_selftest)
        self.test_output = QLabel("（尚未运行）")
        self.test_output.setWordWrap(True)
        self.test_output.setAlignment(Qt.AlignTop)
        self.test_output.setFont(QFont("Consolas", 9))
        self.test_output.setMinimumHeight(160)
        test_box.addWidget(self.test_btn)
        test_box.addWidget(self.test_output)
        self.ctrl_layout.addWidget(test_group)

# --- log (process tail, moved to its own tab below)
        # --- 最近消息（控制页）
        msg_group = QGroupBox("最近消息")
        msg_v = QVBoxLayout(msg_group)
        self.log_view = QTableWidget(0, 3)
        self.log_view.setHorizontalHeaderLabels(["时间", "类型", "内容"])
        self.log_view.horizontalHeader().setSectionResizeMode(2, QHeaderView.Stretch)
        self.log_view.verticalHeader().setVisible(False)
        self.log_view.setEditTriggers(QTableWidget.NoEditTriggers)
        msg_v.addWidget(self.log_view)
        self.ctrl_layout.addWidget(msg_group)

        self._build_process_log_tab()
        self._build_audit_tab()
        self.tabs.currentChanged.connect(self._on_tab_changed)

    def _build_process_log_tab(self) -> None:
        proc_tab = QWidget()
        proc_v = QVBoxLayout(proc_tab)
        row = QHBoxLayout()
        self.proc_combo = QComboBox()
        self.proc_combo.addItem("Codex 引擎", "codex")
        self.proc_combo.addItem("Windows 控制桥", "windows")
        self.proc_combo.addItem("隧道（cloudflared/ngrok）", "tunnel")
        self.proc_refresh_btn = QPushButton("刷新")
        self.proc_refresh_btn.clicked.connect(self._refresh_process_log)
        row.addWidget(QLabel("进程:"))
        row.addWidget(self.proc_combo, 1)
        row.addWidget(self.proc_refresh_btn)
        proc_v.addLayout(row)
        self.proc_view = QTableWidget(0, 3)
        self.proc_view.setHorizontalHeaderLabels(["时间", "类型", "内容"])
        self.proc_view.horizontalHeader().setSectionResizeMode(2, QHeaderView.Stretch)
        self.proc_view.verticalHeader().setVisible(False)
        self.proc_view.setEditTriggers(QTableWidget.NoEditTriggers)
        proc_v.addWidget(self.proc_view)
        self.tabs.addTab(proc_tab, "进程日志")

    def _build_audit_tab(self) -> None:
        audit_tab = QWidget()
        audit_v = QVBoxLayout(audit_tab)
        row = QHBoxLayout()
        self.audit_day_combo = QComboBox()
        self.audit_day_combo.addItems(["全部日期", "今天", "最近 3 天", "最近 7 天"])
        self.audit_tool_combo = QComboBox()
        self.audit_tool_combo.addItem("全部工具")
        self.audit_success_combo = QComboBox()
        self.audit_success_combo.addItems(["全部", "成功", "失败"])
        self.audit_refresh_btn = QPushButton("查询")
        self.audit_refresh_btn.clicked.connect(self._refresh_audit_log)
        row.addWidget(QLabel("日期:"))
        row.addWidget(self.audit_day_combo)
        row.addWidget(QLabel("工具:"))
        row.addWidget(self.audit_tool_combo, 1)
        row.addWidget(QLabel("结果:"))
        row.addWidget(self.audit_success_combo)
        row.addWidget(self.audit_refresh_btn)
        audit_v.addLayout(row)
        self.audit_view = QTableWidget(0, 6)
        self.audit_view.setHorizontalHeaderLabels(["时间", "工具", "结果", "耗时 ms", "客户端", "参数摘要"])
        self.audit_view.horizontalHeader().setSectionResizeMode(5, QHeaderView.Stretch)
        self.audit_view.verticalHeader().setVisible(False)
        self.audit_view.setEditTriggers(QTableWidget.NoEditTriggers)
        audit_v.addWidget(self.audit_view)
        self.tabs.addTab(audit_tab, "审计日志")
        self._refresh_audit_tool_combo()

    def _on_tab_changed(self, index: int) -> None:
        if index == 1:
            self._refresh_process_log()
        elif index == 2:
            self._refresh_audit_tool_combo()
            self._refresh_audit_log()

    # ---------------------------------------------------------- helpers
    def _refresh_project_list(self) -> None:
        self._projects = load_projects()
        self.project_combo.clear()
        if not self._projects:
            self.project_combo.addItem("（尚无项目，点击“添加/浏览…”）")
            return
        for p in self._projects:
            self.project_combo.addItem(f"{p.display_name}  ·  {p.root_path}", p.root_path)

    def _load_active_project(self) -> None:
        active = self._app_config.active_workspace
        if active:
            idx = self.project_combo.findData(active)
            if idx >= 0:
                self.project_combo.setCurrentIndex(idx)
                return
        if self._projects:
            latest = max(self._projects, key=lambda p: p.last_used_at or "")
            idx = self.project_combo.findData(latest.root_path)
            if idx >= 0:
                self.project_combo.setCurrentIndex(idx)
                return
        self._apply_selected_project()

    def _apply_selected_project(self) -> None:
        root = self._selected_root()
        if not root:
            return
        project = next((p for p in load_projects() if p.root_path == root), None)
        if project is None:
            return
        self.root_label.setText(project.root_path)
        modes = [m[0] for m in PERMISSION_MODES]
        idx = modes.index(project.permission_mode) if project.permission_mode in modes else 0
        self.permission_combo.setCurrentIndex(idx)
        try:
            method = ConnectionMethod(project.connection) if project.connection else None
        except ValueError:
            method = None
        if method and method in CONNECTION_METHODS:
            self.connection_combo.setCurrentIndex(CONNECTION_METHODS.index(method))
        else:
            self.connection_combo.setCurrentIndex(CONNECTION_METHODS.index(ConnectionMethod.LOCAL))
        self.hostname_edit.setText(project.public_hostname or "")
        self.git_name_edit.setText(project.git_user_name or "")
        self.git_email_edit.setText(project.git_user_email or "")
        self.git_remote_edit.setText(project.default_push_remote or "")
        self.git_branch_edit.setText(project.default_push_branch or "")
        self._sync_connection_fields()

    def _selected_root(self) -> str:
        idx = self.project_combo.currentIndex()
        data = self.project_combo.itemData(idx)
        return str(data) if data else ""

    def _selected_permission_mode(self) -> str:
        return PERMISSION_MODES[self.permission_combo.currentIndex()][0]

    def _selected_connection(self) -> ConnectionMethod:
        data = self.connection_combo.currentData()
        try:
            return ConnectionMethod(str(data))
        except ValueError:
            return ConnectionMethod.LOCAL

    def _on_project_selected(self) -> None:
        self._apply_selected_project()

    def _on_connection_changed(self) -> None:
        self._sync_connection_fields()

    def _generate_gemini_credentials(self) -> None:
        uri = self.gemini_uri_edit.text().strip()
        if not uri:
            QMessageBox.warning(self, "缺少 URI", "请先粘贴 Gemini 的 redirect URI。")
            return
        try:
            client_id, secret = get_or_create_gemini_client(uri, rotate_secret=True)
        except ValueError as exc:
            QMessageBox.warning(self, "URI 无效", str(exc))
            return
        self._gemini_store.set(GEMINI_LAST_URI_CRED_NAME, uri)
        self.gemini_id_label.setText(client_id)
        self._gemini_secret = secret
        self.gemini_secret_label.setText("•" * 16)

    @staticmethod
    def _copy_text(text: str) -> None:
        QApplication.clipboard().setText(text)

    def _sync_connection_fields(self) -> None:
        method = self._selected_connection()
        need_domain = method in (ConnectionMethod.CLOUDFLARE, ConnectionMethod.NGROK)
        self.hostname_edit.setEnabled(need_domain)
        self.cf_token_edit.setEnabled(method == ConnectionMethod.CLOUDFLARE)

    def _browse_project(self) -> None:
        chosen = QFileDialog.getExistingDirectory(self, "选择项目目录")
        if not chosen:
            return
        self._add_project_dir(chosen)

    def _add_project_dir(self, path: str) -> None:
        root = Path(path).expanduser().resolve()
        if not root.is_dir():
            QMessageBox.warning(self, "无法添加项目", f"目录不存在：{root}")
            return
        suggestions = suggest_commands(root)
        existing = next((p for p in load_projects() if p.root_path == str(root)), None)
        project = ProjectConfig(
            display_name=root.name,
            root_path=str(root),
            permission_mode=existing.permission_mode if existing else "workspace",
            test_command=(existing.test_command if existing else "")
            or suggestions.get("test_command", ""),
            lint_command=(existing.lint_command if existing else "")
            or suggestions.get("lint_command", ""),
            typecheck_command=(existing.typecheck_command if existing else "")
            or suggestions.get("typecheck_command", ""),
            build_command=(existing.build_command if existing else "") or suggestions.get("build_command", ""),
        )
        upsert_project(project)
        self._app_config.active_workspace = str(root)
        save_app_config(self._app_config)
        self._refresh_project_list()
        idx = self.project_combo.findData(str(root))
        if idx >= 0:
            self.project_combo.setCurrentIndex(idx)
        self._apply_selected_project()
        self._append_log(f"已添加项目：{root}")

    # -------------------------------------------------- service control
    def _current_options(self) -> StartOptions:
        return StartOptions(
            project_root=self._selected_root(),
            permission_mode=self._selected_permission_mode(),
            codex_token=self._current_token,
            windows_enabled=self.bridge_check.isChecked(),
            windows_token=self._bridge_token,
            connection=self._selected_connection(),
            public_hostname=self.hostname_edit.text().strip(),
            tunnel_token=self._tunnel_token_value(),
        )

    def _tunnel_token_value(self) -> str | None:
        """Field text, falling back to the remembered default when empty."""
        field = self.cf_token_edit.text().strip()
        return field or self._tunnel_token_default or None

    def _on_tunnel_token_edited(self, text: str) -> None:
        if text.strip():
            _remember_tunnel_token(text)
            self._tunnel_token_default = text.strip()

    def _require_start_confirmations(self) -> bool:
        """True when the user declined a mandatory warning."""
        if self._selected_permission_mode() == "system" and not self._app_config.first_system_risk_accepted:
            answer = QMessageBox.question(
                self,
                "系统权限风险确认",
                "“完全访问”模式允许读写项目目录之外的文件、执行任意命令等高风险操作。\n"
                "请确认您理解风险后继续（仅首次确认，之后不再提示）。",
                QMessageBox.Yes | QMessageBox.No,
            )
            if answer == QMessageBox.No:
                return True
            self._app_config.first_system_risk_accepted = True
            save_app_config(self._app_config)
        if self._selected_connection() == ConnectionMethod.QUICK:
            answer = QMessageBox.question(
                self,
                "Quick Tunnel 临时测试",
                "Quick Tunnel 的公开地址每次启动都会变化，仅适合临时调试。\n"
                "正式使用请选择 Cloudflare 固定地址或 ngrok 固定地址。\n\n是否继续？",
                QMessageBox.Yes | QMessageBox.No,
            )
            if answer == QMessageBox.No:
                return True
        return False

    def _save_git_settings(self) -> None:
        """Read the Git fields, validate, persist; rejects invalid values with a
        Chinese message before anything is saved."""
        root = self._selected_root()
        if not root:
            QMessageBox.warning(self, "未选择项目", "请先选择项目目录。")
            return
        values = {
            "git_user_name": self.git_name_edit.text().strip(),
            "git_user_email": self.git_email_edit.text().strip(),
            "default_push_remote": self.git_remote_edit.text().strip(),
            "default_push_branch": self.git_branch_edit.text().strip(),
        }
        for kind, value in values.items():
            error = git_field_error(kind, value)
            if error is not None:
                QMessageBox.warning(self, "Git 参数不合法", error)
                return
        projects = load_projects()
        project = next((p for p in projects if p.root_path == root), None)
        if project is None:
            project = ProjectConfig(root_path=root, display_name=Path(root).name)
        project.git_user_name = values["git_user_name"]
        project.git_user_email = values["git_user_email"]
        project.default_push_remote = values["default_push_remote"]
        project.default_push_branch = values["default_push_branch"]
        upsert_project(project)
        self._append_log("Git 参数已保存（user.name / user.email / 默认推送远程 / 默认推送分支）。")

    def _save_project_settings(self) -> None:
        root = self._selected_root()
        if not root:
            return
        projects = load_projects()
        project = next((p for p in projects if p.root_path == root), None)
        if project is None:
            project = ProjectConfig(root_path=root, display_name=Path(root).name)
        git_vals = {
            "git_user_name": self.git_name_edit.text().strip(),
            "git_user_email": self.git_email_edit.text().strip(),
            "default_push_remote": self.git_remote_edit.text().strip(),
            "default_push_branch": self.git_branch_edit.text().strip(),
        }
        for kind, value in git_vals.items():
            error = git_field_error(kind, value)
            if error is not None:
                QMessageBox.warning(self, "Git 参数不合法", error)
                return
        project.permission_mode = self._selected_permission_mode()
        project.connection = self._selected_connection().value
        project.public_hostname = self.hostname_edit.text().strip()
        project.git_user_name = git_vals["git_user_name"]
        project.git_user_email = git_vals["git_user_email"]
        project.default_push_remote = git_vals["default_push_remote"]
        project.default_push_branch = git_vals["default_push_branch"]
        upsert_project(project)
        self._app_config.active_workspace = root
        save_app_config(self._app_config)

    def _start_service(self) -> None:
        if self._require_start_confirmations():
            return
        if self.coord.running:
            self._append_log("服务已在启动或运行中")
            return
        if not self._selected_root():
            QMessageBox.warning(self, "未选择项目", "请先选择项目目录。")
            return
        self._save_project_settings()
        options = self._current_options()
        if not options.codex_token:
            QMessageBox.warning(self, "缺少令牌", "请先点击“重新生成令牌”创建访问令牌。")
            return
        self._bridge_token = _bridge_token(ensure=True)
        self._set_busy(True)
        self.status_label.setText(f"状态：正在启动（{options.connection.label()}）…")
        self._append_log(f"正在启动服务（{options.connection.label()}）…")

        def run() -> str:
            self.coord.start(options)
            return f"服务已启动：{self.coord.public_url or '仅本机'}"

        def done(result: Any) -> None:
            self._set_busy(False)
            if isinstance(result, Exception):
                self._append_log(f"启动失败:{result}")
            else:
                self._append_log(result)
            self._poll_status()

        _run_async(run, done)

    def _stop_service(self) -> None:
        self._set_busy(True)
        self.status_label.setText("状态：正在停止…")
        self._append_log("正在停止服务…")

        def run() -> str:
            self.coord.stop()
            return "服务已停止"

        def done(result: Any) -> None:
            self._set_busy(False)
            if isinstance(result, Exception):
                self._append_log(f"停止服务出错:{result}")
            else:
                self._append_log(result)
            self._poll_status()

        _run_async(run, done)

    def _restart_service(self) -> None:
        if not self.coord.running:
            self._start_service()
            return
        self._save_project_settings()
        options = self._current_options()
        self._set_busy(True)
        self.status_label.setText("状态：正在重启…")
        self._append_log("正在重启服务…")

        def run() -> str:
            self.coord.stop()
            self.coord.start(options)
            return f"已重启：{self.coord.public_url or '仅本机'}"

        def done(result: Any) -> None:
            self._set_busy(False)
            if isinstance(result, Exception):
                self._append_log(f"重启失败:{result}")
            else:
                self._append_log(result)
            self._poll_status()

        _run_async(run, done)

    def _set_busy(self, busy: bool) -> None:
        for btn in (self.start_btn, self.stop_btn, self.restart_btn):
            btn.setEnabled(not busy)

    # -------------------------------------------------- coordinator events
    def _emit_coord_event(self, state: EngineState, message: str | None) -> None:
        self._signals.coord_event.emit(state, message)

    def _on_coord_event(self, state: EngineState, message: str | None) -> None:
        if message:
            self._append_log(f"[{state.value}] {message}")

    # ------------------------------------------------------- status / URL
    def _local_url(self) -> str:
        return f"http://127.0.0.1:{CODEXPRO_LOCAL_PORT}/mcp"

    def _display_url(self) -> str:
        return self.coord.public_url or self._local_url()

    def _refresh_url_ui(self) -> None:
        url = self.coord.public_url
        if url:
            suffix = "（固定地址，重启不变）" if not self.coord.url_mutable else "（临时地址，重启会变）"
        else:
            url = self._local_url()
            suffix = "（仅本机）"
        self.url_label.setText(f"MCP 地址：{url} {suffix}")

    def _poll_status(self) -> None:
        state = self.coord.state
        if state == EngineState.ERROR:
            self.status_label.setText(f"状态：失败（{self.coord.message or ''}）")
        else:
            self.status_label.setText(f"状态：{state.value}")
        running = self.coord.running or state == EngineState.STARTING
        self.start_btn.setEnabled(not running)
        self.stop_btn.setEnabled(running)
        self.restart_btn.setEnabled(not running)
        self.test_btn.setEnabled(running)
        self._refresh_url_ui()

    # ------------------------------------------------------ token helpers
    def _sync_token_ui(self) -> None:
        if self._current_token:
            self.token_label.setText(f"令牌（Bearer）：{self._current_token}")
        else:
            self.token_label.setText("令牌：未生成（点击“重新生成令牌”）")

    def _copy_to_clipboard(self, text: str) -> None:
        if not text:
            return
        QApplication.clipboard().setText(text)

    def _regenerate_token(self) -> None:
        self._append_log("正在重新生成令牌…")

        def run() -> str:
            return regenerate_access_token()

        def done(result: Any) -> None:
            if isinstance(result, Exception):
                self._append_log(f"令牌生成失败:{result}")
                return
            self._current_token = result
            self._bridge_token = _bridge_token(ensure=True)
            self._sync_token_ui()
            self._append_log("已重新生成访问令牌（旧令牌立即失效）")

        _run_async(run, done)

    # ----------------------------------------------------------- selftest
    def _run_selftest(self) -> None:
        if not self.coord.running:
            self.test_output.setText("（请先启动服务）")
            return
        url = self._display_url()
        self.test_btn.setEnabled(False)
        self.test_output.setText(f"正在自测 {url} …")

        def run() -> SelftestResult:
            return run_selftest(url, self._current_token or None)

        def done(result: Any) -> None:
            self.test_btn.setEnabled(True)
            if isinstance(result, Exception):
                self.test_output.setText(f"自测异常:{result}")
                return
            lines = [f"{'✔' if s['ok'] else '✘'}  {s['step']}：{s['detail']}" for s in result.steps]
            text = "\n".join(lines) if lines else "（无步骤）"
            self.test_output.setText(text)
            if result.ok:
                self._append_log("连接自测通过")
            else:
                self._append_log(f"连接自测未通过:{result.error or '有步骤失败'}")

    # -------------------------------------------------------------- log
    def _append_log(self, text: str) -> None:
        self.log_view.insertRow(0)
        now = datetime.datetime.now().strftime("%H:%M:%S")
        self.log_view.setItem(0, 0, QTableWidgetItem(now))
        self.log_view.setItem(0, 1, QTableWidgetItem("信息"))
        self.log_view.setItem(0, 2, QTableWidgetItem(text))
        self.log_view.setColumnWidth(0, 90)
        self.log_view.setColumnWidth(1, 70)
        if self.log_view.rowCount() > 300:
            self.log_view.removeRow(self.log_view.rowCount() - 1)

    # ------------------------------------------------- logs: process tail
    def _engine_log_source(self) -> list[str]:
        """Select the active engine's log buffer (newest first)."""
        key = self.proc_combo.currentData()
        manager = {"codex": self.coord.codex, "windows": self.coord.windows, "tunnel": self.coord.tunnel}.get(key)
        if manager is None:
            return []
        proc = getattr(manager, "_proc", None)
        if proc is None or getattr(proc, "log", None) is None:
            return []
        return list(proc.log)  # type: ignore[union-attr]

    def _refresh_process_log(self) -> None:
        lines = self._engine_log_source()[-400:]
        self.proc_view.setRowCount(len(lines))
        for row, line in enumerate(lines):
            self.proc_view.setItem(row, 0, QTableWidgetItem("-"))
            self.proc_view.setItem(row, 1, QTableWidgetItem(self.proc_combo.currentText()))
            self.proc_view.setItem(row, 2, QTableWidgetItem(line))
        self.proc_view.setColumnWidth(0, 80)
        self.proc_view.setColumnWidth(1, 120)

    # ------------------------------------------------- logs: audit page
    def _refresh_audit_tool_combo(self) -> None:
        current = self.audit_tool_combo.currentText()
        names = available_tool_names()
        self.audit_tool_combo.blockSignals(True)
        self.audit_tool_combo.clear()
        self.audit_tool_combo.addItem("全部工具")
        self.audit_tool_combo.addItems(names)
        if current in names:
            self.audit_tool_combo.setCurrentText(current)
        self.audit_tool_combo.blockSignals(False)

    def _refresh_audit_log(self) -> None:
        day_mode = self.audit_day_combo.currentIndex()
        tool = "" if self.audit_tool_combo.currentIndex() <= 0 else self.audit_tool_combo.currentText()
        mode = self.audit_success_combo.currentIndex()
        success = None if mode == 0 else mode == 1
        records = query_logs(AuditQuery(tool_name=tool, success=success, limit=2000))
        today = datetime.date.today()
        cutoff = None
        if day_mode == 1:
            cutoff = today
        elif day_mode == 2:
            cutoff = today - datetime.timedelta(days=2)
        elif day_mode == 3:
            cutoff = today - datetime.timedelta(days=6)
        if cutoff is not None:
            cutoff_day = cutoff.strftime("%Y-%m-%d")
            records = [r for r in records if (r.get("timestamp") or "")[:10] >= cutoff_day]
        records = records[:500]
        self.audit_view.setRowCount(len(records))
        for row, record in enumerate(records):
            summary = record.get("parameter_summary") or {}
            summary_text = (
                str(summary)[:220] if isinstance(summary, dict) else str(summary)[:220]
            )
            self.audit_view.setItem(row, 0, QTableWidgetItem(record.get("timestamp", "")[11:19]))
            self.audit_view.setItem(row, 1, QTableWidgetItem(str(record.get("tool_name", ""))))
            self.audit_view.setItem(row, 2, QTableWidgetItem("成功" if record.get("success") else "失败"))
            self.audit_view.setItem(row, 3, QTableWidgetItem(str(record.get("duration_ms", ""))))
            self.audit_view.setItem(row, 4, QTableWidgetItem(str(record.get("client_name", ""))))
            self.audit_view.setItem(row, 5, QTableWidgetItem(summary_text))
        self.audit_view.setColumnWidth(0, 70)
        self.audit_view.setColumnWidth(1, 110)
        self.audit_view.setColumnWidth(2, 55)
        self.audit_view.setColumnWidth(3, 70)
        self.audit_view.setColumnWidth(4, 90)

    # --------------------------------------------------------------- end
    def closeEvent(self, event: Any) -> None:  # noqa: N802 - Qt naming
        if self.coord.running:
            self.coord.stop()
        super().closeEvent(event)


def main() -> int:
    os.environ.setdefault("PYTHONIOENCODING", "utf-8")
    app = QApplication(sys.argv)
    window = MainWindow()
    window.show()
    return app.exec()


if __name__ == "__main__":
    sys.exit(main())