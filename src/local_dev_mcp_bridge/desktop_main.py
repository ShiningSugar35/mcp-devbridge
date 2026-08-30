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

import contextlib
import datetime
import json
import os
import shutil
import socket
import sys
import time
import uuid
from collections.abc import Callable
from pathlib import Path
from typing import Any, cast
from urllib.parse import urlsplit

import httpx
from PySide6.QtCore import QLockFile, QObject, Qt, QThreadPool, QTimer, Signal
from PySide6.QtGui import QAction, QFont, QTextOption
from PySide6.QtWidgets import (
    QAbstractItemView,
    QApplication,
    QCheckBox,
    QComboBox,
    QDialog,
    QDialogButtonBox,
    QFileDialog,
    QFormLayout,
    QGroupBox,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QLineEdit,
    QListWidget,
    QMainWindow,
    QMenu,
    QMessageBox,
    QPlainTextEdit,
    QPushButton,
    QScrollArea,
    QSizePolicy,
    QSpinBox,
    QStyle,
    QSystemTrayIcon,
    QTableWidget,
    QTableWidgetItem,
    QTabWidget,
    QTextBrowser,
    QToolButton,
    QToolTip,
    QVBoxLayout,
    QWidget,
)

from . import APP_NAME, __version__, constants
from .app_state import ServiceCoordinator, StartOptions
from .audit import AuditQuery, available_tool_names, query_logs
from .backend_manager import port_in_use
from .config_store import (
    load_app_config,
    load_projects,
    save_app_config,
    save_projects,
)
from .device_hub import HUB_PEER_SECRET_KEY, DeviceRegistry, mcp_base_url, normalize_mcp_url
from .engines import EngineState, find_node, find_uvx
from .help_content import (
    HELP_CONNECTION_INFO,
    HELP_CONNECTION_METHOD,
    HELP_GATEWAY_PORT,
    HELP_PUBLIC_HOSTNAME,
    HELP_TUNNEL_TOKEN,
    recommend_connection,
    search_topics,
)
from .models import PermissionMode, ProjectConfig, gateway_service_url, git_field_error
from .oauth_provider import get_or_create_gemini_client
from .platform_support import IS_WINDOWS, open_in_file_manager
from .project_manager import ProjectManager
from .project_secrets import (
    clear_project_tunnel_token,
    ensure_project_access_token,
    get_project_access_token,  # noqa: F401  # pyright: ignore[reportUnusedImport] - compatibility hook
    get_project_tunnel_token,
    remember_project_tunnel_token,
)
from .secrets import SecretsStore, generate_token
from .selftest import SelftestResult, run_selftest
from .shell import get_shell_info
from .shell import run_program as _run_program
from .tunnel_manager import ConnectionMethod
from .update_manager import (
    ReleaseInfo,
    download_installer,
    fetch_latest_release,
    is_newer,
    launch_update,
)

# 权限模式（与命令执行档位合二为一）：
#   只读     = read_only  + safe        （只读安全操作）
#   默认     = workspace  + developer   （项目内开发工具白名单）
#   完全访问 = system     + full_system （任意命令，首次启动需风险确认）
PERMISSION_MODES = [
    ("read_only", "只读"),
    ("workspace", "项目工作区"),
    ("system", "完全访问（危险，默认）"),
]
PERMISSION_PROFILE = {"read_only": "safe", "workspace": "developer", "system": "full_system"}
ADMIN_SETUP_TITLE = "管理员权限设置未完成"
ADMIN_SETUP_TEXT = (
    "Windows 管理员授权没有成功完成，因此“完全访问”暂时无法启用。\n\n"
    "你可以先以普通权限启动，只访问项目目录。稍后可在“项目设置”中重新启用“完全访问”。"
)
CLIENT_TARGETS = [("chatgpt", "ChatGPT 网页端"), ("gemini", "Gemini Spark")]
CONNECTION_METHODS = [
    ConnectionMethod.CLOUDFLARE,
    ConnectionMethod.NGROK,
    ConnectionMethod.QUICK,
    ConnectionMethod.LOCAL,
]
BRIDGE_TOKEN_CRED_NAME = "LocalDevMCPBridge/WindowsBridgeToken"
TUNNEL_TOKEN_CRED_NAME = "LocalDevMCPBridge/CloudflareTunnelToken"
GEMINI_LAST_URI_CRED_NAME = "LocalDevMCPBridge/OAuthGeminiLastUri"


class NoWheelComboBox(QComboBox):
    """Ignore wheel events so page scrolling never changes a selection."""

    def wheelEvent(self, event: Any) -> None:  # noqa: N802 - Qt API name
        event.ignore()


class HelpButton(QToolButton):
    """Small non-modal contextual help trigger; hover/click shows a tooltip card."""

    def __init__(self, html: str, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setText("?")
        self.setToolTip(html)
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self.setAutoRaise(True)
        self.setFixedSize(20, 20)
        self.clicked.connect(self._show_help)

    def _show_help(self) -> None:
        QToolTip.showText(self.mapToGlobal(self.rect().bottomLeft()), self.toolTip(), self)

    def leaveEvent(self, event: Any) -> None:  # noqa: N802 - Qt API
        QToolTip.hideText()
        super().leaveEvent(event)


class _Signals(QObject):
    done = Signal(object)
    coord_event = Signal(object, object)  # state, message


# Keep worker signal bridges alive until the queued GUI callback has executed.
# Without a strong reference PySide may destroy the QObject immediately after
# the worker returns, losing the queued completion signal while the underlying
# engine has already reached READY.
_ASYNC_SIGNAL_GUARDS: set[_Signals] = set()


def _run_async(fn: Callable[[], Any], callback: Callable[[Any], None]) -> None:
    """Run fn on the global thread pool; callback(result) on the GUI thread."""
    signals = _Signals()
    _ASYNC_SIGNAL_GUARDS.add(signals)

    def finish(result: Any) -> None:
        try:
            callback(result)
        finally:
            _ASYNC_SIGNAL_GUARDS.discard(signals)

    signals.done.connect(finish)

    def target() -> None:
        try:
            result = fn()
        except Exception as exc:  # noqa: BLE001 - surface any failure to the UI
            result = exc
        signals.done.emit(result)

    QThreadPool.globalInstance().start(target)


def _same_root(a: str, b: str) -> bool:
    """Compare two root paths in a case-insensitive, resolved way (Windows)."""
    try:
        return Path(a).expanduser().resolve() == Path(b).expanduser().resolve()
    except OSError:
        return a.casefold() == b.casefold()


def _bridge_token(ensure: bool = False) -> str:
    """Windows bridge token: persisted in the secret store, auto-created."""
    store = SecretsStore()
    value = store.get(BRIDGE_TOKEN_CRED_NAME)
    if not value and ensure:
        value = generate_token(256)
        store.set(BRIDGE_TOKEN_CRED_NAME, value)
    return value or ""


def _hub_access_token(*, ensure: bool = False, regenerate: bool = False) -> str:
    """One Hub-scoped client bearer, independent from project credentials."""
    store = SecretsStore()
    value = "" if regenerate else (store.get(constants.ACCESS_TOKEN_CRED_NAME) or "")
    if not value and (ensure or regenerate):
        value = generate_token(256)
        store.set(constants.ACCESS_TOKEN_CRED_NAME, value)
    return value


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
        self._app_config = load_app_config()
        identity_changed = False
        if not self._app_config.device_id:
            self._app_config.device_id = uuid.uuid4().hex[:12]
            identity_changed = True
        if not self._app_config.device_name:
            self._app_config.device_name = socket.gethostname() or "本机"
            identity_changed = True
        if identity_changed:
            save_app_config(self._app_config)
        self.device_registry = DeviceRegistry(
            local_device_id=self._app_config.device_id,
            local_device_name=self._app_config.device_name,
        )
        self.pm = ProjectManager()
        self._workspace_credentials: dict[str, str] = {}
        self.coord = ServiceCoordinator(
            workspace_registry=self._lookup_workspace,
            workspace_project_registry=self._lookup_configured_workspace,
            workspace_credential_registry=self._lookup_workspace_credential,
            project_runtime_registry=self._project_runtime_snapshot,
            device_registry=self.device_registry,
            local_device_id=self._app_config.device_id,
        )
        self._signals = _Signals()
        self._signals.coord_event.connect(self._on_coord_event)
        self.coord.listen(self._emit_coord_event)
        self._current_token = _hub_access_token(ensure=True)
        self._bridge_token = _bridge_token()
        self._loading_project = False
        self._loaded_project_root = ""
        self._busy_project_ids: set[str] = set()
        self._bulk_project_action: str | None = None
        self._closing = False
        self._force_exit = False
        self._tray_hint_shown = False
        self._device_heartbeat_busy = False
        self._latest_release: ReleaseInfo | None = None
        self._update_check_busy = False
        self._update_notice_shown = False
        self._test_outputs: dict[str, str] = {}
        self._diag_outputs: dict[str, str] = {}
        self._tunnel_token_default = ""
        self._timer = QTimer(self)
        self._timer.timeout.connect(self._poll_status)
        self._build_ui()
        self._setup_tray()
        # New projects default to fully open; persisted per-project settings override this.
        self.permission_combo.setCurrentIndex(2)
        self._refresh_project_list()
        self._load_active_project()
        self._sync_token_ui()
        self._poll_status()
        self._timer.start(1000)
        self._device_timer = QTimer(self)
        self._device_timer.timeout.connect(self._send_device_heartbeat)
        self._device_timer.start(15000)
        QTimer.singleShot(1500, self._resume_upgrade_if_requested)
        QTimer.singleShot(3000, self._send_device_heartbeat)
        QTimer.singleShot(5000, self._check_for_updates)
        self._update_timer = QTimer(self)
        self._update_timer.timeout.connect(self._check_for_updates)
        self._update_timer.start(12 * 60 * 60 * 1000)
        QTimer.singleShot(1000, self._sanitize_remote_replica_configuration)
        QTimer.singleShot(2000, self._runtime_preflight)

    def _help_label(self, text: str, html: str) -> QWidget:
        holder = QWidget()
        row = QHBoxLayout(holder)
        row.setContentsMargins(0, 0, 0, 0)
        row.setSpacing(4)
        row.addWidget(QLabel(text))
        row.addWidget(HelpButton(html))
        row.addStretch(1)
        return holder

    # ---------------------------------------------------------------- UI
    def _build_ui(self) -> None:
        self.setWindowTitle(f"{APP_NAME} v{__version__}")
        self.resize(1200, 850)
        self.setMinimumSize(900, 650)

        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setVerticalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAsNeeded)
        scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        central = QWidget()
        scroll.setWidget(central)
        self.setCentralWidget(scroll)

        root = QVBoxLayout(central)
        root.setContentsMargins(20, 18, 20, 20)
        root.setSpacing(14)

        header = QHBoxLayout()
        title_col = QVBoxLayout()
        title_col.setSpacing(2)
        title = QLabel("MCP DevBridge")
        title.setObjectName("PageTitle")
        platform_suffix = " · Linux/SteamOS" if not IS_WINDOWS else ""
        subtitle = QLabel(f"把本地开发项目连接到 ChatGPT 或 Gemini{platform_suffix}")
        subtitle.setObjectName("PageSubtitle")
        title_col.addWidget(title)
        title_col.addWidget(subtitle)
        header.addLayout(title_col, 1)
        self.update_btn = QToolButton()
        self.update_btn.setText("↑")
        self.update_btn.setObjectName("UpdateButton")
        self.update_btn.setToolTip("发现新版本")
        self.update_btn.setFixedSize(30, 30)
        self.update_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self.update_btn.setVisible(False)
        self.update_btn.clicked.connect(self._show_update_dialog)
        header.addWidget(self.update_btn)
        version = QLabel(f"v{__version__}")
        version.setObjectName("VersionBadge")
        version.setAlignment(Qt.AlignmentFlag.AlignCenter)
        header.addWidget(version)
        root.addLayout(header)

        self.tabs = QTabWidget()
        self.tabs.setDocumentMode(True)
        root.addWidget(self.tabs)
        self.ctrl_tab = QWidget()
        self.ctrl_layout = QVBoxLayout(self.ctrl_tab)
        self.ctrl_layout.setContentsMargins(0, 4, 0, 4)
        self.ctrl_layout.setSpacing(8)
        self.tabs.addTab(self.ctrl_tab, "工作台")

        # --- project list (多项目并行)
        proj_box = QGroupBox("项目")
        proj_v = QVBoxLayout(proj_box)
        proj_v.setContentsMargins(12, 12, 12, 12)
        proj_v.setSpacing(8)
        self.project_table = QTableWidget(0, 5)
        self.project_table.setHorizontalHeaderLabels(["名称", "路径", "状态", "端口", "操作"])
        self.project_table.setColumnHidden(3, True)
        self.project_table.horizontalHeader().setSectionResizeMode(
            1, QHeaderView.ResizeMode.Stretch
        )
        self.project_table.verticalHeader().setVisible(False)
        self.project_table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.project_table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.project_table.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
        self.project_table.itemSelectionChanged.connect(self._on_project_selected)
        proj_v.addWidget(self.project_table)

        proj_btns = QHBoxLayout()
        proj_btns.setSpacing(8)
        self.add_project_btn = QPushButton("添加项目")
        self.add_project_btn.setProperty("role", "primary")
        self.add_project_btn.setFixedWidth(110)
        self.add_project_btn.clicked.connect(self._browse_project)
        self.remove_project_btn = QPushButton("移除项目")
        self.remove_project_btn.clicked.connect(self._remove_project)
        self.all_projects_btn = QPushButton("启动所有项目")
        self.all_projects_btn.clicked.connect(self._toggle_all_projects)
        proj_btns.addWidget(self.add_project_btn)
        proj_btns.addWidget(self.remove_project_btn)
        proj_btns.addWidget(self.all_projects_btn)
        proj_btns.addStretch(1)
        proj_v.addLayout(proj_btns)
        self.project_table.setToolTip("所有运行中的项目根目录同时可用，工具调用按路径自动路由")
        self.ctrl_layout.addWidget(proj_box)

        # --- config: permission + connection + bridge
        cfg_box = QGroupBox("连接与权限")
        cfg_form = QFormLayout(cfg_box)
        cfg_form.setContentsMargins(12, 12, 12, 12)
        cfg_form.setSpacing(8)
        self.permission_combo = NoWheelComboBox()
        for _value, label in PERMISSION_MODES:
            self.permission_combo.addItem(label)
        self.permission_combo.currentIndexChanged.connect(self._autosave_project_settings)
        cfg_form.addRow("权限模式:", self.permission_combo)
        self.permission_combo.setToolTip(
            "只读：只允许读取；项目工作区：仅操作当前项目；完全访问：允许系统级操作。"
        )

        self.client_combo = NoWheelComboBox()
        for value, label in CLIENT_TARGETS:
            self.client_combo.addItem(label, value)
        self.client_combo.currentIndexChanged.connect(self._on_client_changed)
        cfg_form.addRow("连接客户端:", self.client_combo)

        self.connection_combo = NoWheelComboBox()
        for method in CONNECTION_METHODS:
            self.connection_combo.addItem(method.label(), method.value)
        self.connection_combo.currentIndexChanged.connect(self._on_connection_changed)
        self.connection_combo.setToolTip("长期使用建议固定地址；Quick Tunnel 仅适合临时测试。")
        cfg_form.addRow(self._help_label("连接方式", HELP_CONNECTION_METHOD), self.connection_combo)

        self.hostname_edit = QLineEdit()
        self.hostname_edit.setPlaceholderText("例如 mcp.example.com")
        self.hostname_edit.editingFinished.connect(self._autosave_project_settings)
        cfg_form.addRow(self._help_label("公网域名", HELP_PUBLIC_HOSTNAME), self.hostname_edit)

        self.cf_token_edit = QLineEdit()
        self.cf_token_edit.setEchoMode(QLineEdit.EchoMode.Password)
        self.cf_token_edit.setPlaceholderText("粘贴 Cloudflare 提供的访问码")
        self.cf_token_edit.setToolTip("加密保存到当前项目，不写入项目配置文件。")
        if self._tunnel_token_default:
            self.cf_token_edit.setText(self._tunnel_token_default)
        self.cf_token_edit.textEdited.connect(self._on_tunnel_token_edited)
        cfg_form.addRow(self._help_label("Cloudflare 访问码", HELP_TUNNEL_TOKEN), self.cf_token_edit)

        self.gemini_box = QGroupBox("Gemini 授权")
        gemini_form = QFormLayout(self.gemini_box)
        gemini_form.setContentsMargins(12, 12, 12, 12)
        gemini_form.setSpacing(8)
        self._gemini_store = SecretsStore()
        self._gemini_secret = ""
        self.gemini_uri_edit = QLineEdit()
        self.gemini_uri_edit.setPlaceholderText("粘贴 Gemini 提供的回调地址")
        self.gemini_uri_edit.editingFinished.connect(self._on_gemini_uri_edited)
        gemini_form.addRow("Gemini 回调地址:", self.gemini_uri_edit)

        self.gemini_gen_btn = QPushButton("生成 / 更新授权信息")
        self.gemini_gen_btn.clicked.connect(self._generate_gemini_credentials)
        gemini_form.addRow("", self.gemini_gen_btn)

        self.gemini_id_edit = QLineEdit("—")
        self.gemini_id_edit.setReadOnly(True)
        self.gemini_id_edit.setToolTip("应用编号：只读，可选中复制")
        self.gemini_id_copy = QPushButton("复制")
        id_row = QHBoxLayout()
        id_row.setSpacing(8)
        id_row.addWidget(self.gemini_id_edit, 1)
        id_row.addWidget(self.gemini_id_copy)
        gemini_form.addRow("应用编号:", id_row)

        self.gemini_secret_edit = QLineEdit("—")
        self.gemini_secret_edit.setReadOnly(True)
        self.gemini_secret_edit.setToolTip("应用密钥：掩码显示，仅可复制")
        self.gemini_secret_copy = QPushButton("复制")
        secret_row = QHBoxLayout()
        secret_row.setSpacing(8)
        secret_row.addWidget(self.gemini_secret_edit, 1)
        secret_row.addWidget(self.gemini_secret_copy)
        gemini_form.addRow("应用密钥:", secret_row)

        self.gemini_id_copy.clicked.connect(
            lambda: self._copy_with_feedback(self.gemini_id_copy, self.gemini_id_edit.text())
        )
        self.gemini_secret_copy.clicked.connect(
            lambda: self._copy_with_feedback(self.gemini_secret_copy, self._gemini_secret)
        )
        cfg_form.addRow("", self.gemini_box)

        if self.gemini_uri_edit.text():
            try:
                client_id, secret = get_or_create_gemini_client(
                    self.gemini_uri_edit.text(), rotate_secret=False
                )
                self.gemini_id_edit.setText(client_id)
                self._gemini_secret = secret
                self.gemini_secret_edit.setText("•" * 16)
            except ValueError:
                pass

        self.bridge_check = QCheckBox("启用 Windows 控制桥接")
        self.bridge_check.setToolTip("启用后提供额外的 Windows 桌面控制工具。")
        self.bridge_check.toggled.connect(self._autosave_project_settings)
        if not IS_WINDOWS:
            self.bridge_check.setChecked(False)
            self.bridge_check.setEnabled(False)
            self.bridge_check.setText("Windows 控制桥接（Linux/SteamOS 不适用）")
            self.bridge_check.setToolTip(
                "Linux/SteamOS 使用系统自带的文件与程序控制能力，不启用 Windows 控制组件。"
            )
        cfg_form.addRow("", self.bridge_check)

        cfg_actions = QHBoxLayout()
        cfg_actions.addStretch(1)
        self.save_all_project_settings_btn = QPushButton("保存为所有项目设置")
        self.save_all_project_settings_btn.clicked.connect(self._save_settings_for_all_projects)
        cfg_actions.addWidget(self.save_all_project_settings_btn)
        cfg_form.addRow("", cfg_actions)
        self.ctrl_layout.addWidget(cfg_box)

        # --- git settings (Phase 5)
        git_box = QGroupBox("Git（可选）")
        git_form = QFormLayout(git_box)
        git_form.setContentsMargins(12, 12, 12, 12)
        git_form.setSpacing(8)
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
        for field in (
            self.git_name_edit,
            self.git_email_edit,
            self.git_remote_edit,
            self.git_branch_edit,
        ):
            field.editingFinished.connect(self._autosave_project_settings)
        self.git_save_btn = QPushButton("保存 Git 设置")
        self.git_save_btn.clicked.connect(self._save_git_settings)
        git_form.addRow("", self.git_save_btn)
        self.ctrl_layout.addWidget(git_box)

        # --- status
        self.status_label = QLabel("状态：未启动")
        self.status_label.setWordWrap(True)
        self.ctrl_layout.addWidget(self.status_label)
        self.component_status = QLabel(
            "运行状态：项目服务未启动 · 连接服务未启动 · 外网连接未启动 · Windows 控制未启动"
        )
        self.component_status.setWordWrap(True)
        self.component_status.setStyleSheet("color: #666666;")

        # --- token / URL
        tok_box = QGroupBox("连接信息")
        tok_layout = QVBoxLayout(tok_box)
        tok_layout.setContentsMargins(12, 12, 12, 12)
        tok_layout.setSpacing(8)
        info_help_row = QHBoxLayout()
        info_help_row.addStretch(1)
        info_help_row.addWidget(HelpButton(HELP_CONNECTION_INFO))
        tok_layout.addLayout(info_help_row)
        self.token_edit = QLineEdit(_hub_access_token(ensure=True))
        self.token_edit.setReadOnly(True)
        self.url_edit = QLineEdit("选择项目后显示")
        self.url_edit.setReadOnly(True)
        tok_row = QHBoxLayout()
        tok_row.setSpacing(8)
        self.token_copy_btn = QPushButton("复制访问码")
        self.token_regenerate_btn = QPushButton("重新生成")
        self.url_copy_btn = QPushButton("复制地址")
        self.token_copy_btn.clicked.connect(
            lambda: self._copy_with_feedback(self.token_copy_btn, self._current_token)
        )
        self.token_regenerate_btn.clicked.connect(self._regenerate_token)
        self.url_copy_btn.clicked.connect(
            lambda: self._copy_with_feedback(self.url_copy_btn, self._display_url())
        )
        tok_row.addWidget(self.token_copy_btn)
        tok_row.addWidget(self.token_regenerate_btn)
        tok_row.addWidget(self.url_copy_btn)
        tok_row.addStretch(1)
        tok_layout.addWidget(self.token_edit)
        tok_layout.addWidget(self.url_edit)
        tok_layout.addLayout(tok_row)

        # --- shared Hub Gateway port
        port_row = QHBoxLayout()
        port_row.setSpacing(8)
        port_row.addWidget(self._help_label("本机连接编号", HELP_GATEWAY_PORT))
        self.gateway_port_spin = QSpinBox()
        self.gateway_port_spin.setRange(1, 65535)
        self.gateway_port_spin.setValue(self._app_config.gateway_port)
        self.gateway_port_spin.setFixedWidth(90)
        self.gateway_port_spin.valueChanged.connect(self._on_gateway_port_changed)
        port_row.addWidget(self.gateway_port_spin)
        port_check_btn = QPushButton("检查连接")
        port_check_btn.clicked.connect(self._check_gateway_port)
        port_row.addWidget(port_check_btn)
        port_default_btn = QPushButton("恢复默认")
        port_default_btn.clicked.connect(self._restore_default_gateway_port)
        port_row.addWidget(port_default_btn)
        port_row.addStretch(1)
        tok_layout.addLayout(port_row)

        self.service_url_edit = QLineEdit(self._service_url_text())
        self.service_url_edit.setReadOnly(True)
        self.service_url_edit.setStyleSheet("color: #555555;")
        service_row = QHBoxLayout()
        service_row.setSpacing(8)
        service_row.addWidget(self.service_url_edit, 1)
        self.service_url_copy_btn = QPushButton("复制本机连接地址")
        self.service_url_copy_btn.clicked.connect(
            lambda: self._copy_with_feedback(self.service_url_copy_btn, self._service_url_text())
        )
        service_row.addWidget(self.service_url_copy_btn)
        tok_layout.addLayout(service_row)

        self.port_warn_label = QLabel("")
        self.port_warn_label.setWordWrap(True)
        self.port_warn_label.setStyleSheet("color: #c62828; font-weight: bold;")
        self.port_warn_label.setVisible(False)
        tok_layout.addWidget(self.port_warn_label)

        self.ctrl_layout.addWidget(tok_box)

        # --- self test
        test_group = QGroupBox("单项检查（可选）")
        self.test_group = test_group
        test_box = QVBoxLayout(test_group)
        test_box.setContentsMargins(12, 12, 12, 12)
        test_box.setSpacing(8)
        btn_row = QHBoxLayout()
        btn_row.setSpacing(8)
        self.test_btn = QPushButton("运行连接自测")
        self.test_btn.clicked.connect(self._run_selftest)
        self.env_btn = QPushButton("开发环境检测")
        self.env_btn.setToolTip("检测默认 Shell 以及 python/git/pytest/pyright 是否可调用")
        self.env_btn.clicked.connect(self._run_env_check)
        btn_row.addWidget(self.test_btn)
        btn_row.addWidget(self.env_btn)
        btn_row.addStretch(1)
        self.test_output = QLabel("（尚未运行）")
        self.test_output.setWordWrap(True)
        self.test_output.setAlignment(Qt.AlignmentFlag.AlignTop)
        self.test_output.setFont(QFont("Consolas", 9))
        self.test_output.setMinimumHeight(120)
        test_box.addLayout(btn_row)
        test_box.addWidget(self.test_output)
        # 此卡片由“诊断”页面承载，工作台不重复展示。

        # --- 最近消息（控制页）
        msg_group = QGroupBox("运行记录")
        msg_v = QVBoxLayout(msg_group)
        msg_v.setContentsMargins(12, 12, 12, 12)
        msg_v.setSpacing(8)
        self.log_text = QPlainTextEdit()
        self.log_text.setReadOnly(True)
        self.log_text.setMinimumHeight(150)
        self.log_text.setMaximumBlockCount(300)
        self.log_text.setWordWrapMode(QTextOption.WrapMode.NoWrap)
        self.log_text.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Expanding)
        msg_v.addWidget(self.log_text)
        self.ctrl_layout.addWidget(msg_group)

        self._build_process_log_tab()
        self._build_audit_tab()
        self._build_gateway_log_tab()
        self._build_diagnostics_tab()

        self.project_settings_tab = QWidget()
        project_settings_v = QVBoxLayout(self.project_settings_tab)
        project_settings_v.setContentsMargins(0, 8, 0, 4)
        project_settings_v.setSpacing(12)
        project_settings_v.addWidget(cfg_box)
        project_settings_v.addWidget(git_box)
        project_settings_actions = QHBoxLayout()
        project_settings_actions.addStretch(1)
        self.advanced_btn = QPushButton("高级设置…")
        self.advanced_btn.clicked.connect(self._open_advanced_settings)
        self.advanced_btn.setSizePolicy(QSizePolicy.Policy.Minimum, QSizePolicy.Policy.Fixed)
        self.advanced_btn.setMinimumHeight(28)
        project_settings_actions.addWidget(self.advanced_btn)
        project_settings_v.addLayout(project_settings_actions)
        project_settings_v.addStretch(1)
        self.tabs.addTab(self.project_settings_tab, "项目设置")

        self._build_app_settings_tab()
        self._build_devices_tab()
        self._build_manual_tab()
        self._organize_top_level_tabs()
        self.tabs.currentChanged.connect(self._on_tab_changed)

    def _build_diagnostics_tab(self) -> None:
        tab = QWidget()
        layout = QVBoxLayout(tab)
        layout.setContentsMargins(0, 4, 0, 4)
        layout.setSpacing(8)
        row = QHBoxLayout()
        self.diag_btn = QPushButton("一键连接诊断")
        self.diag_btn.clicked.connect(self._run_diagnostics)
        row.addWidget(self.diag_btn)
        row.addStretch(1)
        layout.addLayout(row)
        self.diag_output = QPlainTextEdit("（尚未诊断）")
        self.diag_output.setReadOnly(True)
        self.diag_output.setMinimumHeight(360)
        self.diag_output.setFont(QFont("Consolas", 9))
        self.diag_output.setWordWrapMode(QTextOption.WrapMode.NoWrap)
        layout.addWidget(self.diag_output)
        layout.addWidget(self.test_group)
        tech_box = QGroupBox("技术详情（通常不需要看）")
        tech_layout = QVBoxLayout(tech_box)
        tech_layout.setContentsMargins(12, 18, 12, 12)
        tech_layout.addWidget(self.component_status)
        layout.addWidget(tech_box)
        self.tabs.addTab(tab, "连接诊断")

    def _run_diagnostics(self) -> None:
        project = self._project_config()
        if project is None:
            self.diag_output.setPlainText(
                "需要先做一件事\n\n"
                "你还没有选择项目。回到“工作台”添加或选中一个项目，然后再运行诊断。"
            )
            return
        project_id = project.id
        self.diag_btn.setEnabled(False)
        self.diag_output.setPlainText("正在检查项目、连接方式和网页端可用性…")

        def work() -> str:
            problems: list[tuple[str, str]] = []
            ok_items: list[str] = []
            details: list[str] = []
            layer_titles = [
                "项目引擎", "本机连接服务", "公网入口", "MCP 初始化",
                "公开工具数量", "Schema 指纹", "只读调用", "流式连接",
                "访问授权", "工作区连续性", "最近自动恢复", "ChatGPT 会话边界",
            ]
            layer_values: list[tuple[str, str]] = [("未检查", "") for _ in layer_titles]

            def set_layer(index: int, status: str, detail: str = "") -> None:
                layer_values[index - 1] = (status, detail)

            def step_ok(result: SelftestResult | None, *names: str) -> bool:
                return bool(result) and any(
                    str(step.get("step") or "") in names and bool(step.get("ok"))
                    for step in result.steps
                )
            root_path = Path(project.root_path)

            if root_path.is_dir():
                ok_items.append("项目文件夹可以正常访问")
            else:
                problems.append(
                    (
                        "找不到项目文件夹",
                        "回到工作台移除这个项目，再重新添加正确的项目目录。",
                    )
                )

            node_exe = find_node()
            if node_exe:
                ok_items.append("项目引擎运行组件已经就绪")
                details.append(f"Node.js：{node_exe}")
            else:
                problems.append(
                    (
                        "安装包缺少项目引擎运行组件",
                        "请重新安装 MCP DevBridge 正式版；正式安装包会自带 Node.js，不需要单独安装或修改 PATH。",
                    )
                )
            uvx_exe = find_uvx()
            if uvx_exe:
                details.append(f"uvx：{uvx_exe}")
            elif project.windows_enabled:
                problems.append(
                    (
                        "Windows 控制运行组件不完整",
                        "请重新安装 MCP DevBridge 正式版；安装包会自带 uv/uvx。首次启用 Windows 控制时仍需要联网获取锁定的 Windows-MCP 组件。",
                    )
                )

            access_value = _hub_access_token(ensure=True)
            if access_value:
                ok_items.append("连接访问码已经准备好")
            else:
                problems.append(
                    (
                        "还没有连接访问码",
                        "回到工作台，在“连接信息”里点击“重新生成”。",
                    )
                )

            try:
                method = ConnectionMethod(project.connection)
            except ValueError:
                method = ConnectionMethod.LOCAL
            details.append(f"连接方式：{method.label()}")

            if method == ConnectionMethod.CLOUDFLARE:
                if not project.public_hostname:
                    problems.append(
                        (
                            "没有填写固定公网域名",
                            "打开“项目设置”，填写 Cloudflare Tunnel 对应的域名，例如 mcp.example.com。",
                        )
                    )
                else:
                    ok_items.append("固定公网域名已经填写")
                if not get_project_tunnel_token(project.id):
                    problems.append(
                        (
                            "没有填写 Cloudflare 访问码",
                            "打开“项目设置”，把 Cloudflare 提供的访问码粘贴到“Cloudflare 访问码”。",
                        )
                    )
                else:
                    ok_items.append("Cloudflare 隧道凭据已经保存")
            elif method == ConnectionMethod.NGROK:
                if not project.public_hostname:
                    problems.append(
                        (
                            "没有填写 ngrok 固定域名",
                            "打开“项目设置”，填写你的 ngrok Reserved Domain。",
                        )
                    )
                if not (shutil.which("ngrok") or shutil.which("ngrok.exe")):
                    problems.append(
                        (
                            "电脑里没有找到 ngrok",
                            "如果你没有使用过 ngrok，建议改用 Quick Tunnel；否则请安装 ngrok 并加入 PATH。",
                        )
                    )
            elif method == ConnectionMethod.QUICK:
                if not (
                    shutil.which("cloudflared") or Path(self.coord.tunnel.cloudflared).is_file()
                ):
                    problems.append(
                        (
                            "没有找到 Quick Tunnel 所需的 cloudflared",
                            "重新安装 MCP DevBridge 正式版；安装包会自带 cloudflared。",
                        )
                    )
                else:
                    ok_items.append("Quick Tunnel 所需组件已经就绪")
                details.append("Quick Tunnel 的地址是临时的；重建后会换地址。")
            else:
                details.append("“仅本机”不会把这台电脑暴露到互联网。")
                if project.client_target in {"chatgpt", "gemini"}:
                    problems.append(
                        (
                            "当前选择了“仅本机”",
                            "网页端 ChatGPT / Gemini 无法直接访问仅本机地址。到“项目设置”改用 Quick Tunnel、Cloudflare 固定地址或 ngrok。",
                        )
                    )

            if project.client_target == "gemini" and not project.gemini_redirect_uri:
                problems.append(
                    (
                        "Gemini 还缺少 Redirect URI",
                        "到“项目设置 → Gemini 授权”，粘贴 Gemini 提供的回调地址。",
                    )
                )

            ports = (
                self._app_config.gateway_port,
                project.codexpro_port,
                project.windows_bridge_port,
            )
            if not all(ports) or len(set(ports)) != 3:
                problems.append(
                    (
                        "当前项目的内部端口配置有冲突",
                        "停止这个项目后，打开“高级设置”，恢复默认端口或改成互不重复的端口。",
                    )
                )
            else:
                details.append(
                    f"内部连接：主连接 {ports[0]} / 项目服务 {ports[1]} / Windows 控制 {ports[2]}"
                )

            unit = self.pm.unit(project.id)
            state = self._project_state(project)
            if state == EngineState.READY:
                ok_items.append("项目服务正在运行")
                set_layer(1, "正常", "当前项目服务处于可用状态")
                local_result: SelftestResult | None = None
                public_result: SelftestResult | None = None
                try:
                    local_result = run_selftest(
                        self._local_url(),
                        access_value or None,
                        timeout=15.0,
                        route_workspace_id=project.id,
                        expect_hub_contract=True,
                    )
                    if method != ConnectionMethod.LOCAL and self.coord.public_url:
                        public_result = run_selftest(
                            self.coord.public_url,
                            access_value or None,
                            timeout=15.0,
                            route_workspace_id=project.id,
                            expect_hub_contract=True,
                        )
                except Exception as exc:  # noqa: BLE001
                    problems.append(
                        (
                            "连接测试没有完成",
                            f"服务已经启动，但分层自测时出现异常：{exc}。先尝试停止再启动；仍失败时查看“日志 → 运行情况”。",
                        )
                    )

                active_result = public_result if public_result is not None else local_result
                set_layer(
                    2,
                    "正常" if local_result and local_result.ok else "异常",
                    "本机真实 MCP 调用" if local_result else "本机检查未完成",
                )
                if method == ConnectionMethod.LOCAL:
                    set_layer(3, "不适用", "当前为仅本机模式")
                else:
                    set_layer(
                        3,
                        "正常" if public_result and public_result.ok else "异常",
                        "公网真实 MCP 调用" if public_result else "公网检查未完成",
                    )
                set_layer(
                    4,
                    "正常" if step_ok(active_result, "initialize") else "异常",
                    "initialize",
                )
                set_layer(
                    5,
                    "正常" if active_result and active_result.tool_count == 50 else "异常",
                    f"实际 {active_result.tool_count if active_result else 0} 个",
                )
                set_layer(
                    6,
                    "正常" if active_result and active_result.hub_contract_match else "异常",
                    active_result.schema_fingerprint if active_result else "未取得",
                )
                set_layer(
                    7,
                    "正常"
                    if step_ok(active_result, "server_config", "open_current_workspace")
                    else "异常",
                    "只读生产调用",
                )
                set_layer(
                    8,
                    "正常" if step_ok(active_result, "streamable_http") else "异常",
                    "Streamable HTTP / SSE 通道",
                )
                set_layer(
                    9,
                    "正常" if access_value and active_result and active_result.ok else "异常",
                    "同一访问授权完成连续调用" if access_value else "没有可用访问授权",
                )
                set_layer(
                    10,
                    "正常" if active_result and active_result.ok else "异常",
                    "诊断调用显式绑定当前项目，没有使用默认项目",
                )

                recovery = self.coord.recovery_snapshot()
                gateway_age = recovery.get("gateway_restart_seconds_ago")
                public_age = recovery.get("public_restart_seconds_ago")
                if gateway_age is None and public_age is None:
                    set_layer(11, "正常", "本次运行尚无自动恢复记录")
                elif public_age is None or (
                    gateway_age is not None and gateway_age <= public_age
                ):
                    set_layer(
                        11,
                        "已恢复",
                        f"最近恢复了本机连接服务，约 {int(float(gateway_age or 0))} 秒前",
                    )
                else:
                    set_layer(
                        11,
                        "已恢复",
                        f"最近恢复了公网连接，约 {int(float(public_age))} 秒前",
                    )

                all_transport_ok = bool(local_result and local_result.ok) and (
                    method == ConnectionMethod.LOCAL
                    or bool(public_result and public_result.ok)
                )
                if all_transport_ok:
                    set_layer(
                        12,
                        "边界清晰",
                        "本机和公网连接均正常；若当前 ChatGPT 仍整体失效，应改用新会话继续，已保存的授权和工作区不会因此丢失",
                    )
                    ok_items.append("分层实际连接测试通过")
                else:
                    set_layer(
                        12,
                        "未判定",
                        "本机或公网链路仍有异常，暂不能把问题归到 ChatGPT 会话对象",
                    )
                    problems.append(
                        (
                            "实际连接测试没有完全通过",
                            "按下方分层结果定位失败层；修复后重新运行诊断，不要通过反复重连 ChatGPT 掩盖本机故障。",
                        )
                    )
            elif state == EngineState.STARTING:
                problems.append(
                    (
                        "项目还在连接中",
                        "等待工作台状态变成“可以使用”后，再运行一次诊断。",
                    )
                )
            elif state == EngineState.STOPPING:
                problems.append(
                    (
                        "项目正在停止",
                        "等待停止完成，再重新启动项目。",
                    )
                )
            elif state == EngineState.ERROR:
                message = unit.message if unit is not None else ""
                problems.append(
                    (
                        "项目上一次启动失败",
                        f"先回到工作台重新启动。{('系统记录：' + str(message)) if message else '如果继续失败，请查看“日志 → 运行情况”。'}",
                    )
                )
            else:
                problems.append(
                    (
                        "项目还没有启动",
                        "回到工作台点击“启动服务”。状态变成“可以使用”后再运行诊断。",
                    )
                )

            if state != EngineState.READY:
                if state == EngineState.STARTING:
                    set_layer(1, "等待", "项目仍在启动")
                elif state == EngineState.STOPPING:
                    set_layer(1, "等待", "项目正在停止")
                elif state == EngineState.ERROR:
                    set_layer(1, "异常", "项目上一次启动失败")
                else:
                    set_layer(1, "未启动", "项目尚未启动")

            peer = SecretsStore().get(HUB_PEER_SECRET_KEY)
            if self._app_config.hub_url and peer:
                details.append(f"这台电脑已加入主电脑：{self._app_config.hub_url}")
            remote_views = [
                view
                for view in self.device_registry.views(local_online=state == EngineState.READY)
                if not view.local
            ]
            if remote_views:
                online = sum(1 for view in remote_views if view.online)
                details.append(
                    f"已配对 {len(remote_views)} 台远程电脑，其中 {online} 台在线"
                )

            lines: list[str] = []
            if problems:
                lines.extend(
                    [
                        "需要处理后再使用",
                        f"发现 {len(problems)} 个需要处理的问题。按下面顺序操作即可：",
                        "",
                    ]
                )
                for index, (title, action) in enumerate(problems, 1):
                    lines.append(f"{index}. {title}")
                    lines.append(f"   怎么做：{action}")
                    lines.append("")
            else:
                lines.extend(
                    [
                        "可以正常使用",
                        "没有发现会阻止 ChatGPT / Gemini 使用当前项目的问题。",
                        "",
                    ]
                )

            lines.append("分层连接状态")
            healthy_states = {"正常", "已恢复", "边界清晰", "不适用"}
            neutral_states = {"未检查", "等待", "未启动", "未判定"}
            for index, (title, value) in enumerate(zip(layer_titles, layer_values, strict=True), 1):
                status, detail = value
                marker = "✓" if status in healthy_states else "•" if status in neutral_states else "!"
                line = f"{index}. {marker} {title}：{status}"
                if detail:
                    line += f" — {detail}"
                lines.append(line)
            lines.append("")

            if ok_items:
                lines.append("已经正常的项目")
                lines.extend(f"✓ {item}" for item in ok_items)
                lines.append("")
            if details:
                lines.append("补充信息")
                lines.extend(f"• {item}" for item in details)
            return "\n".join(lines).rstrip()

        def done(result: Any) -> None:
            self.diag_btn.setEnabled(True)
            if isinstance(result, Exception):
                output = (
                    "诊断没有完成\n\n"
                    f"检查过程中发生异常：{result}\n"
                    "建议先停止并重新启动当前项目，然后再次诊断。"
                )
            else:
                output = str(result)
            self._diag_outputs[project_id] = output
            if project_id == self._selected_project_id():
                self.diag_output.setPlainText(output)

        _run_async(work, done)

    def _take_top_tab(self, title: str) -> QWidget:
        for index in range(self.tabs.count()):
            if self.tabs.tabText(index) == title:
                widget = self.tabs.widget(index)
                self.tabs.removeTab(index)
                assert widget is not None
                return widget
        raise RuntimeError(f"找不到标签页：{title}")

    def _organize_top_level_tabs(self) -> None:
        process_page = self._take_top_tab("进程日志")
        audit_page = self._take_top_tab("审计日志")
        gateway_page = self._take_top_tab("Gateway 日志")
        diagnostics_page = self._take_top_tab("连接诊断")
        project_settings_page = self._take_top_tab("项目设置")
        devices_page = self._take_top_tab("设备")
        manual_page = self._take_top_tab("使用手册")
        app_settings_page = self._take_top_tab("设置")

        self.logs_tab = QWidget()
        logs_layout = QVBoxLayout(self.logs_tab)
        logs_layout.setContentsMargins(0, 8, 0, 4)
        self.log_tabs = QTabWidget()
        self.log_tabs.setDocumentMode(True)
        self.process_log_page = process_page
        self.audit_log_page = audit_page
        self.gateway_log_page = gateway_page
        self.log_tabs.addTab(process_page, "运行情况")
        self.log_tabs.addTab(audit_page, "操作记录")
        self.log_tabs.addTab(gateway_page, "网络连接")
        self.log_tabs.currentChanged.connect(self._on_log_tab_changed)
        logs_layout.addWidget(self.log_tabs)

        self.tabs.insertTab(1, devices_page, "设备")
        self.tabs.insertTab(2, project_settings_page, "项目设置")
        self.tabs.insertTab(3, diagnostics_page, "诊断")
        self.tabs.insertTab(4, self.logs_tab, "日志")
        self.tabs.insertTab(5, manual_page, "使用手册")
        self.tabs.insertTab(6, app_settings_page, "设置")

    def _on_log_tab_changed(self, _index: int) -> None:
        current = self.log_tabs.currentWidget()
        if current is self.process_log_page:
            self._refresh_process_log()
        elif current is self.audit_log_page:
            self._refresh_audit_tool_combo()
            self._refresh_audit_log()
        elif current is self.gateway_log_page:
            self._refresh_gateway_log()

    def _build_devices_tab(self) -> None:
        self.devices_tab = QWidget()
        layout = QVBoxLayout(self.devices_tab)
        layout.setContentsMargins(0, 8, 0, 4)
        layout.setSpacing(12)

        identity_box = QGroupBox("这台电脑")
        identity_form = QFormLayout(identity_box)
        identity_form.setContentsMargins(14, 18, 14, 14)
        self.device_name_edit = QLineEdit(self._app_config.device_name)
        self.device_name_edit.setPlaceholderText("给这台电脑起一个容易辨认的名字")
        self.device_name_edit.editingFinished.connect(self._save_device_name)
        identity_form.addRow("设备名称", self.device_name_edit)
        id_view = QLineEdit(self._app_config.device_id)
        id_view.setReadOnly(True)
        identity_form.addRow("设备 ID", id_view)
        layout.addWidget(identity_box)

        hub_box = QGroupBox("让别的电脑连接这台主电脑")
        hub_v = QVBoxLayout(hub_box)
        hub_v.setContentsMargins(14, 18, 14, 14)
        hub_v.setSpacing(8)
        hub_text = QLabel(
            "把这台主电脑的连接地址和下面的 6 位配对码发给另一台电脑。配对码 10 分钟内有效，只能使用一次。"
        )
        hub_text.setWordWrap(True)
        hub_text.setObjectName("MutedText")
        hub_v.addWidget(hub_text)
        code_row = QHBoxLayout()
        self.pair_code_edit = QLineEdit("点击右侧生成")
        self.pair_code_edit.setReadOnly(True)
        self.pair_code_edit.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.pair_code_btn = QPushButton("生成配对码")
        self.pair_code_btn.clicked.connect(self._generate_device_pair_code)
        code_row.addWidget(self.pair_code_edit, 1)
        code_row.addWidget(self.pair_code_btn)
        hub_v.addLayout(code_row)
        layout.addWidget(hub_box)

        join_box = QGroupBox("把这台电脑连接到另一台主电脑")
        join_form = QFormLayout(join_box)
        join_form.setContentsMargins(14, 18, 14, 14)
        join_form.setSpacing(10)
        self.hub_url_edit = QLineEdit(self._app_config.hub_url)
        self.hub_url_edit.setPlaceholderText("主电脑的连接地址，例如 https://mcp.example.com/mcp")
        self.hub_pair_edit = QLineEdit()
        self.hub_pair_edit.setPlaceholderText("6 位配对码")
        self.hub_pair_edit.setMaxLength(6)
        self.join_hub_btn = QPushButton("连接主电脑")
        self.join_hub_btn.clicked.connect(self._join_remote_hub)
        join_form.addRow("主电脑连接地址", self.hub_url_edit)
        join_form.addRow("配对码", self.hub_pair_edit)
        join_form.addRow("", self.join_hub_btn)
        self.hub_status_label = QLabel("未连接其它主电脑")
        self.hub_status_label.setObjectName("MutedText")
        join_form.addRow("状态", self.hub_status_label)
        layout.addWidget(join_box)

        connected_box = QGroupBox("在线设备")
        connected_v = QVBoxLayout(connected_box)
        connected_v.setContentsMargins(14, 18, 14, 14)
        self.device_table = QTableWidget(0, 4)
        self.device_table.setHorizontalHeaderLabels(["电脑", "状态", "公网连接地址", "操作"])
        self.device_table.horizontalHeader().setSectionResizeMode(
            0, QHeaderView.ResizeMode.ResizeToContents
        )
        self.device_table.horizontalHeader().setSectionResizeMode(
            1, QHeaderView.ResizeMode.ResizeToContents
        )
        self.device_table.horizontalHeader().setSectionResizeMode(2, QHeaderView.ResizeMode.Stretch)
        self.device_table.horizontalHeader().setSectionResizeMode(
            3, QHeaderView.ResizeMode.ResizeToContents
        )
        self.device_table.verticalHeader().setVisible(False)
        self.device_table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        connected_v.addWidget(self.device_table)
        layout.addWidget(connected_box)
        layout.addStretch(1)
        self.tabs.addTab(self.devices_tab, "设备")
        self._refresh_device_table()
        self._update_hub_status()

    @staticmethod
    def _host_of(value: str) -> str:
        raw = (value or "").strip()
        if not raw:
            return ""
        try:
            return (urlsplit(raw if "://" in raw else f"https://{raw}").hostname or "").casefold()
        except ValueError:
            return ""

    def _named_tunnel_conflicts_with_hub(self, hub_mcp: str) -> bool:
        project = self._project_config()
        return bool(
            project
            and project.connection == ConnectionMethod.CLOUDFLARE.value
            and self._host_of(project.public_hostname)
            and self._host_of(project.public_hostname) == self._host_of(hub_mcp)
        )

    def _sanitize_remote_replica_configuration(self) -> None:
        peer = SecretsStore().get(HUB_PEER_SECRET_KEY) or ""
        hub = self._app_config.hub_url.strip()
        hub_host = self._host_of(hub)
        if not peer or not hub_host:
            return
        changed = False
        for project in self.pm.list():
            if (
                project.connection == ConnectionMethod.CLOUDFLARE.value
                and self._host_of(project.public_hostname) == hub_host
            ):
                project.connection = ConnectionMethod.QUICK.value
                project.public_hostname = ""
                self.pm.update(project)
                clear_project_tunnel_token(project.id)
                changed = True
        if changed:
            self._append_log(
                "检测到远端电脑和主电脑使用了同一个固定连接；已自动改为临时连接，避免请求被发到错误的电脑。"
            )
            self._apply_selected_project()

    def _save_device_name(self) -> None:
        name = self.device_name_edit.text().strip() or socket.gethostname() or "本机"
        self.device_name_edit.setText(name)
        self._app_config.device_name = name
        save_app_config(self._app_config)
        self.device_registry.set_local_identity(self._app_config.device_id, name)
        self._refresh_device_table()

    def _generate_device_pair_code(self) -> None:
        code, expires = self.device_registry.generate_pair_code()
        self.pair_code_edit.setText(code)
        until = datetime.datetime.fromtimestamp(expires).strftime("%H:%M")
        self.pair_code_edit.setToolTip(f"此配对码将在 {until} 过期，并且成功使用一次后立即失效。")
        QApplication.clipboard().setText(code)
        self._flash_button_success(self.pair_code_btn, "已复制")
        self._append_log("已生成一次性设备配对码，并复制到剪贴板。")

    def _hub_transport_project(self) -> ProjectConfig | None:
        """Return settings used by the shared transport, never a routing owner."""
        selected = self._project_config()
        if selected is not None:
            return selected
        projects = self.pm.list()
        ready = next(
            (project for project in projects if self._project_state(project) == EngineState.READY),
            None,
        )
        return ready or (projects[0] if projects else None)

    def _public_hub_for_pairing(self) -> tuple[str, str] | None:
        project = self._hub_transport_project()
        if project is None or self.coord.state != EngineState.READY or not self.coord.public_url:
            return None
        try:
            method = ConnectionMethod(project.connection)
        except ValueError:
            method = ConnectionMethod.LOCAL
        if method == ConnectionMethod.LOCAL or not self.coord.public_url.startswith("https://"):
            return None
        bearer = _hub_access_token(ensure=True)
        return (self.coord.public_url, bearer) if bearer else None

    def _join_remote_hub(self) -> None:
        hub_raw = self.hub_url_edit.text().strip()
        pair_code = self.hub_pair_edit.text().strip()
        public = self._public_hub_for_pairing()
        if not hub_raw or len(pair_code) != 6:
            QMessageBox.warning(self, "还差一点", "请填写主电脑的连接地址和 6 位配对码。")
            return
        if public is None:
            QMessageBox.warning(
                self,
                "这台电脑还没有公网地址",
                "请先在工作台为一个项目选择 Cloudflare、ngrok 或 Quick Tunnel 并启动服务。\n"
                "只有“仅本机”连接时，另一台电脑无法访问这里。",
            )
            return
        try:
            hub_mcp = normalize_mcp_url(hub_raw)
            hub_base = mcp_base_url(hub_mcp)
        except ValueError as exc:
            QMessageBox.warning(self, "主电脑地址不正确", str(exc))
            return
        public_url, token = public
        if self._named_tunnel_conflicts_with_hub(hub_mcp):
            QMessageBox.warning(
                self,
                "不能和主电脑使用同一个 Cloudflare 固定连接",
                "这台电脑正在使用与主电脑相同的固定连接。这样会让访问请求随机发到不同电脑，从而导致连接失败。\n\n"
                "请给这台远端电脑使用临时连接、ngrok 或它自己的独立域名。ChatGPT 仍然只连接主电脑的固定地址。",
            )
            return
        self.join_hub_btn.setEnabled(False)
        self.hub_status_label.setText("正在配对…")

        def work() -> dict[str, Any]:
            last_error: Exception | None = None
            for attempt in range(3):
                try:
                    response = httpx.post(
                        f"{hub_base}/device/register",
                        json={
                            "pair_code": pair_code,
                            "device_id": self._app_config.device_id,
                            "name": self._app_config.device_name,
                            "mcp_url": public_url,
                            "bearer": token,
                        },
                        timeout=12.0,
                    )
                    payload = response.json()
                    if response.status_code >= 400 or not payload.get("ok"):
                        raise RuntimeError(
                            str(payload.get("message") or f"HTTP {response.status_code}")
                        )
                    return {"hub_mcp": hub_mcp, "peer": str(payload.get("peer_secret") or "")}
                except (httpx.HTTPError, RuntimeError, ValueError) as exc:
                    last_error = exc
                    if attempt < 2:
                        time.sleep(1.0)
            raise RuntimeError(str(last_error or "配对请求失败"))

        def done(result: Any) -> None:
            self.join_hub_btn.setEnabled(True)
            if isinstance(result, Exception):
                self.hub_status_label.setText(f"配对失败：{result}")
                return
            peer = str(result.get("peer") or "")
            if not peer:
                self.hub_status_label.setText("配对失败：主电脑没有返回连接信息")
                return
            SecretsStore().set(HUB_PEER_SECRET_KEY, peer)
            self._app_config.hub_url = str(result["hub_mcp"])
            save_app_config(self._app_config)
            self.hub_url_edit.setText(self._app_config.hub_url)
            self.hub_pair_edit.clear()
            self._update_hub_status()
            self.hub_status_label.setText(
                "已连接主电脑。下一步：继续在 ChatGPT 使用原来的连接，需要时切换到这台电脑。"
            )
            self._append_log("这台电脑已连接主电脑；ChatGPT 无需新增第二个连接。")
            self._send_device_heartbeat()

        _run_async(work, done)

    def _update_hub_status(self) -> None:
        peer = SecretsStore().get(HUB_PEER_SECRET_KEY)
        if self._app_config.hub_url and peer:
            self.hub_status_label.setText(f"已连接主电脑：{self._app_config.hub_url}")
        else:
            self.hub_status_label.setText("未连接其它主电脑")

    def _send_device_heartbeat(self) -> None:
        if self._device_heartbeat_busy:
            return
        hub_url = self._app_config.hub_url.strip()
        peer = SecretsStore().get(HUB_PEER_SECRET_KEY) or ""
        public = self._public_hub_for_pairing()
        if not hub_url or not peer or public is None:
            return
        try:
            hub_base = mcp_base_url(hub_url)
        except ValueError:
            return
        public_url, token = public
        self._device_heartbeat_busy = True

        def work() -> str:
            response = httpx.post(
                f"{hub_base}/device/heartbeat",
                json={
                    "device_id": self._app_config.device_id,
                    "peer_secret": peer,
                    "name": self._app_config.device_name,
                    "mcp_url": public_url,
                    "bearer": token,
                },
                timeout=15.0,
            )
            payload = response.json()
            if response.status_code >= 400 or not payload.get("ok"):
                raise RuntimeError(str(payload.get("message") or f"HTTP {response.status_code}"))
            return "ok"

        def done(result: Any) -> None:
            self._device_heartbeat_busy = False
            if isinstance(result, Exception):
                self.hub_status_label.setText(f"主电脑暂时无法连接：{result}")
            else:
                self._update_hub_status()
            self._refresh_device_table()

        _run_async(work, done)

    def _remove_remote_device(self, device_id: str, name: str) -> None:
        answer = QMessageBox.question(
            self,
            "移除电脑",
            f"确定断开“{name}”吗？对方之后需要重新配对才能连接。",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
        )
        if answer != QMessageBox.StandardButton.Yes:
            return
        self.device_registry.remove(device_id)
        self._refresh_device_table()

    def _refresh_device_table(self) -> None:
        if not hasattr(self, "device_table"):
            return
        local_online = any(
            (unit := self.pm.unit(project.id)) is not None and unit.state == EngineState.READY
            for project in self.pm.list()
        )
        rows = self.device_registry.views(local_online=local_online)
        self.device_table.setRowCount(len(rows))
        for row, view in enumerate(rows):
            name = f"{view.name}（本机）" if view.local else view.name
            self.device_table.setItem(row, 0, QTableWidgetItem(name))
            self.device_table.setItem(row, 1, QTableWidgetItem("在线" if view.online else "离线"))
            address = (
                self.coord.public_url if view.local and self.coord.public_url else view.endpoint_url
            )
            self.device_table.setItem(row, 2, QTableWidgetItem(address or "—"))
            if view.local:
                action = QLabel("—")
                action.setAlignment(Qt.AlignmentFlag.AlignCenter)
                self.device_table.setCellWidget(row, 3, action)
            else:
                btn = QPushButton("移除")
                btn.clicked.connect(
                    lambda _checked=False, did=view.id, n=view.name: self._remove_remote_device(
                        did, n
                    )
                )
                self.device_table.setCellWidget(row, 3, btn)

    def _build_manual_tab(self) -> None:
        self.manual_tab = QWidget()
        root = QVBoxLayout(self.manual_tab)
        root.setContentsMargins(0, 8, 0, 4)
        root.setSpacing(10)

        search_row = QHBoxLayout()
        self.manual_search = QLineEdit()
        self.manual_search.setPlaceholderText("搜索：Quick Tunnel、ChatGPT、多设备、连不上……")
        self.manual_search.textChanged.connect(self._filter_manual_topics)
        search_row.addWidget(self.manual_search, 1)
        root.addLayout(search_row)

        advisor = QGroupBox("不知道选哪种连接方式？")
        advisor_row = QHBoxLayout(advisor)
        advisor_row.setContentsMargins(12, 18, 12, 12)
        self.advisor_internet = QCheckBox("需要网页端 ChatGPT / Gemini 访问")
        self.advisor_internet.setChecked(True)
        self.advisor_long = QCheckBox("准备长期使用")
        self.advisor_domain = QCheckBox("已经有固定域名")
        advisor_btn = QPushButton("给我建议")
        advisor_btn.clicked.connect(self._update_connection_advice)
        advisor_row.addWidget(self.advisor_internet)
        advisor_row.addWidget(self.advisor_long)
        advisor_row.addWidget(self.advisor_domain)
        advisor_row.addWidget(advisor_btn)
        root.addWidget(advisor)
        self.advisor_result = QLabel("第一次体验通常从 Quick Tunnel 开始最省事。")
        self.advisor_result.setWordWrap(True)
        self.advisor_result.setObjectName("MutedText")
        root.addWidget(self.advisor_result)

        content_row = QHBoxLayout()
        self.manual_list = QListWidget()
        self.manual_list.setMinimumWidth(230)
        self.manual_list.setMaximumWidth(320)
        self.manual_list.currentRowChanged.connect(self._show_manual_topic)
        self.manual_browser = QTextBrowser()
        self.manual_browser.setOpenExternalLinks(False)
        content_row.addWidget(self.manual_list)
        content_row.addWidget(self.manual_browser, 1)
        root.addLayout(content_row, 1)

        nav = QHBoxLayout()
        self.manual_prev = QPushButton("上一篇")
        self.manual_next = QPushButton("下一篇")
        self.manual_prev.clicked.connect(lambda: self._move_manual_topic(-1))
        self.manual_next.clicked.connect(lambda: self._move_manual_topic(1))
        nav.addStretch(1)
        nav.addWidget(self.manual_prev)
        nav.addWidget(self.manual_next)
        root.addLayout(nav)
        self.tabs.addTab(self.manual_tab, "使用手册")
        self._filter_manual_topics("")

    def _filter_manual_topics(self, query: str) -> None:
        self._manual_topics = search_topics(query)
        self.manual_list.blockSignals(True)
        self.manual_list.clear()
        for topic in self._manual_topics:
            self.manual_list.addItem(topic.title)
        self.manual_list.blockSignals(False)
        if self._manual_topics:
            self.manual_list.setCurrentRow(0)
            self._show_manual_topic(0)
        else:
            self.manual_browser.setHtml(
                "<h3>没有找到相关内容</h3><p>换一个更短的关键词试试，例如“Quick”“多设备”或“诊断”。</p>"
            )
            self.manual_prev.setEnabled(False)
            self.manual_next.setEnabled(False)

    def _show_manual_topic(self, row: int) -> None:
        if row < 0 or row >= len(getattr(self, "_manual_topics", [])):
            return
        topic = self._manual_topics[row]
        self.manual_browser.setHtml(topic.html)
        self.manual_prev.setEnabled(row > 0)
        self.manual_next.setEnabled(row + 1 < len(self._manual_topics))

    def _move_manual_topic(self, delta: int) -> None:
        target = self.manual_list.currentRow() + delta
        if 0 <= target < self.manual_list.count():
            self.manual_list.setCurrentRow(target)

    def _update_connection_advice(self) -> None:
        self.advisor_result.setText(
            recommend_connection(
                internet_client=self.advisor_internet.isChecked(),
                long_term=self.advisor_long.isChecked(),
                has_fixed_domain=self.advisor_domain.isChecked(),
            )
        )

    def _build_app_settings_tab(self) -> None:
        self.app_settings_tab = QWidget()
        layout = QVBoxLayout(self.app_settings_tab)
        layout.setContentsMargins(0, 8, 0, 4)
        layout.setSpacing(12)
        box = QGroupBox("窗口行为")
        form = QFormLayout(box)
        form.setContentsMargins(14, 16, 14, 14)
        form.setSpacing(10)
        self.close_behavior_combo = NoWheelComboBox()
        self.close_behavior_combo.addItem("最小化到系统托盘", "tray")
        self.close_behavior_combo.addItem("直接退出程序", "exit")
        index = self.close_behavior_combo.findData(self._app_config.close_behavior)
        self.close_behavior_combo.setCurrentIndex(index if index >= 0 else 0)
        self.close_behavior_combo.currentIndexChanged.connect(self._on_close_behavior_changed)
        form.addRow("点击 ×", self.close_behavior_combo)
        note = QLabel("标题栏“—”始终只最小化到任务栏。")
        note.setObjectName("MutedText")
        form.addRow("", note)
        layout.addWidget(box)
        layout.addStretch(1)
        self.tabs.addTab(self.app_settings_tab, "设置")

    def _on_close_behavior_changed(self) -> None:
        value = str(self.close_behavior_combo.currentData() or "tray")
        self._app_config.close_behavior = "exit" if value == "exit" else "tray"
        save_app_config(self._app_config)

    def _flash_button_success(
        self, button: QPushButton | QToolButton, text: str = "复制成功"
    ) -> None:
        original = button.text()
        old_style = button.styleSheet()
        button.setText(text)
        button.setStyleSheet(
            "background:#dcfce7;color:#15803d;border:1px solid #86efac;font-weight:600;"
        )
        QTimer.singleShot(1300, lambda: (button.setText(original), button.setStyleSheet(old_style)))

    def _copy_with_feedback(self, button: QPushButton | QToolButton, text: str) -> None:
        value = (text or "").strip()
        if not value or value in {"—", "选择项目后显示"}:
            return
        QApplication.clipboard().setText(value)
        self._flash_button_success(button)

    def _check_for_updates(self) -> None:
        if self._update_check_busy:
            return
        self._update_check_busy = True

        def work() -> ReleaseInfo:
            return fetch_latest_release()

        def done(result: Any) -> None:
            self._update_check_busy = False
            if isinstance(result, Exception):
                return
            if is_newer(result.version, __version__):
                self._latest_release = result
                self.update_btn.setVisible(True)
                self.update_btn.setToolTip(f"发现 v{result.version}，点击更新")
                if not self._update_notice_shown and self.tray_icon.isVisible():
                    self.tray_icon.showMessage(
                        "MCP DevBridge 有新版本",
                        f"v{result.version} 已发布，点击主窗口右上角 ↑ 可安装。",
                        QSystemTrayIcon.MessageIcon.Information,
                        5000,
                    )
                    self._update_notice_shown = True
            else:
                self._latest_release = None
                self.update_btn.setVisible(False)

        _run_async(work, done)

    def _show_update_dialog(self) -> None:
        info = self._latest_release
        if info is None:
            self._check_for_updates()
            return
        dialog = QDialog(self)
        dialog.setWindowTitle(f"更新 MCP DevBridge · v{info.version}")
        dialog.resize(620, 430)
        layout = QVBoxLayout(dialog)
        title = QLabel(f"发现新版本 v{info.version}")
        title.setObjectName("PageTitle")
        layout.addWidget(title)
        desc = QLabel("安装会自动关闭当前 MCP DevBridge、保留配置并重新启动。")
        desc.setObjectName("MutedText")
        desc.setWordWrap(True)
        layout.addWidget(desc)
        notes = QTextBrowser()
        notes.setPlainText(info.notes or "此版本没有附加说明。")
        layout.addWidget(notes, 1)
        buttons = QDialogButtonBox()
        install_btn = buttons.addButton("下载并安装", QDialogButtonBox.ButtonRole.AcceptRole)
        cancel_btn = buttons.addButton("稍后", QDialogButtonBox.ButtonRole.RejectRole)
        cancel_btn.clicked.connect(dialog.reject)
        install_btn.clicked.connect(
            lambda: (dialog.accept(), self._download_and_install_update(info))
        )
        layout.addWidget(buttons)
        dialog.exec()

    def _download_and_install_update(self, info: ReleaseInfo) -> None:
        self.update_btn.setEnabled(False)
        self.update_btn.setText("…")
        self._append_log(f"正在下载 v{info.version} 更新…")

        def work() -> Path:
            return download_installer(info)

        def done(result: Any) -> None:
            if isinstance(result, Exception):
                self.update_btn.setEnabled(True)
                self.update_btn.setText("↑")
                QMessageBox.warning(self, "更新失败", str(result))
                return
            try:
                root = self._selected_root() or (self._app_config.active_workspace or "")
                launch_update(result, project_root=root)
            except Exception as exc:
                self.update_btn.setEnabled(True)
                self.update_btn.setText("↑")
                QMessageBox.warning(self, "无法启动安装", str(exc))
                return
            self._append_log("安装程序已就绪，MCP DevBridge 将自动重启。")
            self.update_btn.setToolTip("正在更新…")

        _run_async(work, done)

    def _setup_tray(self) -> None:
        self.tray_icon = QSystemTrayIcon(self)
        icon = self.style().standardIcon(QStyle.StandardPixmap.SP_ComputerIcon)
        self.setWindowIcon(icon)
        self.tray_icon.setIcon(icon)
        self.tray_icon.setToolTip("MCP DevBridge")
        menu = QMenu(self)
        self.tray_show_action = QAction("显示主窗口", self)
        self.tray_show_action.triggered.connect(self._show_from_tray)
        self.tray_exit_action = QAction("退出 MCP DevBridge", self)
        self.tray_exit_action.triggered.connect(self._quit_from_tray)
        menu.addAction(self.tray_show_action)
        menu.addSeparator()
        menu.addAction(self.tray_exit_action)
        self.tray_icon.setContextMenu(menu)
        self.tray_icon.activated.connect(self._tray_activated)
        self.tray_icon.show()

    def _show_from_tray(self) -> None:
        self.showNormal()
        self.raise_()
        self.activateWindow()

    def _tray_activated(self, reason: QSystemTrayIcon.ActivationReason) -> None:
        if reason in (
            QSystemTrayIcon.ActivationReason.DoubleClick,
            QSystemTrayIcon.ActivationReason.Trigger,
        ):
            self._show_from_tray()

    def _quit_from_tray(self) -> None:
        if self._closing:
            return
        self._force_exit = True
        self.tray_exit_action.setEnabled(False)
        self.tray_exit_action.setText("正在退出…")
        QTimer.singleShot(0, self.close)
        QTimer.singleShot(12000, self._shutdown_watchdog)

    def _shutdown_watchdog(self) -> None:
        if not self._closing:
            return
        self.tray_icon.hide()
        QApplication.quit()

    def _build_process_log_tab(self) -> None:
        proc_tab = QWidget()
        proc_v = QVBoxLayout(proc_tab)
        proc_v.setContentsMargins(0, 4, 0, 4)
        proc_v.setSpacing(8)
        row = QHBoxLayout()
        row.setSpacing(8)
        self.proc_combo = QComboBox()
        self.proc_combo.addItem("项目服务", "service")
        self.proc_combo.addItem("Windows 控制", "windows")
        self.proc_combo.addItem("公网连接", "tunnel")
        self.proc_refresh_btn = QPushButton("刷新")
        self.proc_refresh_btn.clicked.connect(self._refresh_process_log)
        row.addWidget(QLabel("查看:"))
        row.addWidget(self.proc_combo, 1)
        row.addWidget(self.proc_refresh_btn)
        proc_v.addLayout(row)
        self.proc_empty = QLabel("还没有运行记录。启动当前项目后，这里会显示服务启动和连接过程。")
        self.proc_empty.setObjectName("MutedText")
        self.proc_empty.setWordWrap(True)
        proc_v.addWidget(self.proc_empty)
        self.proc_view = QTableWidget(0, 3)
        self.proc_view.setHorizontalHeaderLabels(["序号", "来源", "说明"])
        self.proc_view.horizontalHeader().setSectionResizeMode(2, QHeaderView.ResizeMode.Stretch)
        self.proc_view.verticalHeader().setVisible(False)
        self.proc_view.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        proc_v.addWidget(self.proc_view)
        self.tabs.addTab(proc_tab, "进程日志")

    def _build_audit_tab(self) -> None:
        audit_tab = QWidget()
        audit_v = QVBoxLayout(audit_tab)
        audit_v.setContentsMargins(0, 4, 0, 4)
        audit_v.setSpacing(8)
        row = QHBoxLayout()
        row.setSpacing(8)
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
        row.addWidget(QLabel("操作:"))
        row.addWidget(self.audit_tool_combo, 1)
        row.addWidget(QLabel("结果:"))
        row.addWidget(self.audit_success_combo)
        row.addWidget(self.audit_refresh_btn)
        audit_v.addLayout(row)
        self.audit_empty = QLabel(
            "还没有 AI 操作记录。ChatGPT / Gemini 读取文件、修改代码或执行命令后会出现在这里。"
        )
        self.audit_empty.setObjectName("MutedText")
        self.audit_empty.setWordWrap(True)
        audit_v.addWidget(self.audit_empty)
        self.audit_view = QTableWidget(0, 6)
        self.audit_view.setHorizontalHeaderLabels(["时间", "操作", "结果", "用时", "来源", "说明"])
        self.audit_view.horizontalHeader().setSectionResizeMode(5, QHeaderView.ResizeMode.Stretch)
        self.audit_view.verticalHeader().setVisible(False)
        self.audit_view.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        audit_v.addWidget(self.audit_view)
        self.tabs.addTab(audit_tab, "审计日志")
        self._refresh_audit_tool_combo()

    def _on_tab_changed(self, index: int) -> None:
        current = self.tabs.widget(index)
        if hasattr(self, "logs_tab") and current is self.logs_tab:
            self._on_log_tab_changed(self.log_tabs.currentIndex())

    def _refresh_gateway_log(self) -> None:
        """Show network activity in beginner-friendly language."""
        from datetime import date as _date

        path = constants.LOG_DIR / f"gateway-{_date.today().isoformat()}.jsonl"
        self.gw_log_path_label.setToolTip(str(path))
        if not path.exists():
            self.gw_log_view.setPlainText(
                "还没有网页端连接记录。启动公网服务并从 ChatGPT / Gemini 连接后，这里会出现记录。"
            )
            return
        try:
            lines = path.read_text(encoding="utf-8", errors="replace").splitlines()[-500:]
        except OSError:
            self.gw_log_view.setPlainText("暂时无法读取网络连接记录。")
            return
        display: list[str] = []
        for line in lines:
            try:
                record = json.loads(line)
            except json.JSONDecodeError:
                continue
            if isinstance(record, dict):
                friendly = self._friendly_gateway_record(record)
                if friendly:
                    display.append(friendly)
        self.gw_log_view.setPlainText(
            "\n".join(display[-300:]) if display else "目前没有需要关注的网络连接记录。"
        )
        sb = self.gw_log_view.verticalScrollBar()
        if sb:
            sb.setValue(sb.maximum())

    def _copy_gateway_log(self) -> None:
        text = self.gw_log_view.toPlainText()
        if text and "尚未收到" not in text:
            QApplication.clipboard().setText(text)

    def _open_gateway_log_dir(self) -> None:
        d = constants.LOG_DIR
        d.mkdir(parents=True, exist_ok=True)
        if not open_in_file_manager(d):
            QMessageBox.information(self, "日志目录", str(d))

    def _build_gateway_log_tab(self) -> None:
        gw_tab = QWidget()
        gw_v = QVBoxLayout(gw_tab)
        gw_v.setContentsMargins(0, 4, 0, 4)
        gw_v.setSpacing(8)
        row = QHBoxLayout()
        row.setSpacing(8)
        self.gw_log_path_label = QLabel("这里显示网页端是否真正连接到了这台电脑。")
        self.gw_log_path_label.setObjectName("MutedText")
        row.addWidget(self.gw_log_path_label, 1)
        gw_refresh = QPushButton("刷新")
        gw_refresh.clicked.connect(self._refresh_gateway_log)
        gw_copy = QPushButton("复制显示内容")
        gw_copy.clicked.connect(self._copy_gateway_log)
        gw_open_dir = QPushButton("打开原始日志目录")
        gw_open_dir.clicked.connect(self._open_gateway_log_dir)
        row.addWidget(gw_refresh)
        row.addWidget(gw_copy)
        row.addWidget(gw_open_dir)
        gw_v.addLayout(row)
        self.gw_log_view = QPlainTextEdit()
        self.gw_log_view.setReadOnly(True)
        self.gw_log_view.setMinimumHeight(300)
        self.gw_log_view.setMaximumBlockCount(2000)
        self.gw_log_view.setWordWrapMode(QTextOption.WrapMode.NoWrap)
        self.gw_log_view.setFont(QFont("Consolas", 9))
        gw_v.addWidget(self.gw_log_view)
        self.tabs.addTab(gw_tab, "Gateway 日志")

    def _refresh_project_list(self) -> None:
        table = self.project_table
        selected_root = self._selected_root() or (self._app_config.active_workspace or "")
        views = self.pm.views()
        table.blockSignals(True)
        try:
            table.setRowCount(len(views))
            for row, view in enumerate(views):
                project = self.pm.get(view.id)
                state_obj = self._project_state(project)
                state = state_obj.value
                values = (
                    view.name,
                    view.root_path,
                    state,
                    str(view.codexpro_port),
                )
                for column, value in enumerate(values):
                    item = table.item(row, column)
                    if item is None:
                        item = QTableWidgetItem(value)
                        table.setItem(row, column, item)
                    elif item.text() != value:
                        item.setText(value)
                svc_btn = table.cellWidget(row, 4)
                if (
                    not isinstance(svc_btn, QPushButton)
                    or str(svc_btn.property("project_id") or "") != view.id
                ):
                    svc_btn = QPushButton("启动服务")
                    svc_btn.setProperty("project_id", view.id)
                    svc_btn.setProperty("project_root", view.root_path)
                    svc_btn.clicked.connect(
                        lambda _checked=False, button=svc_btn: self._toggle_service_for(
                            str(button.property("project_root") or "")
                        )
                    )
                    table.setCellWidget(row, 4, svc_btn)
                busy = self._is_project_busy(view.id)
                if state_obj == EngineState.READY:
                    svc_btn.setText("停止服务")
                elif state_obj == EngineState.STARTING:
                    svc_btn.setText("启动中…")
                elif state_obj == EngineState.STOPPING:
                    svc_btn.setText("停止中…")
                elif state_obj == EngineState.ERROR:
                    svc_btn.setText("重新启动")
                else:
                    svc_btn.setText("启动服务")
                svc_btn.setEnabled(
                    not busy and state_obj not in (EngineState.STARTING, EngineState.STOPPING)
                )
                svc_btn.setToolTip(f"启动或停止 {view.name}")
            table.horizontalHeader().setSectionResizeMode(
                0, QHeaderView.ResizeMode.ResizeToContents
            )
            table.horizontalHeader().setSectionResizeMode(
                2, QHeaderView.ResizeMode.ResizeToContents
            )
            table.horizontalHeader().setSectionResizeMode(
                3, QHeaderView.ResizeMode.ResizeToContents
            )
            table.horizontalHeader().setSectionResizeMode(
                4, QHeaderView.ResizeMode.ResizeToContents
            )
            if views:
                table.selectRow(max(self._row_of_root(selected_root), 0))
        finally:
            table.blockSignals(False)

    def _row_of_root(self, root: str) -> int:
        for row in range(self.project_table.rowCount()):
            item = self.project_table.item(row, 1)
            if item is not None and _same_root(item.text(), root):
                return row
        return 0

    def _select_root(self, root: str) -> None:
        self.project_table.selectRow(self._row_of_root(root))

    def _load_active_project(self) -> None:
        self._apply_selected_project()

    def _apply_selected_project(self) -> None:
        root = self._selected_root()
        if not root:
            return
        project = self.pm.by_root(root)
        if project is None:
            return
        self._loading_project = True
        try:
            self.pm.ensure_ports(project)
            self._loaded_project_root = project.root_path
            self._app_config.active_workspace = project.root_path
            save_app_config(self._app_config)
            modes = [mode[0] for mode in PERMISSION_MODES]
            idx = modes.index(project.permission_mode) if project.permission_mode in modes else 2
            self.permission_combo.setCurrentIndex(idx)
            client_idx = self.client_combo.findData(project.client_target)
            self.client_combo.setCurrentIndex(client_idx if client_idx >= 0 else 0)
            try:
                method = ConnectionMethod(project.connection)
            except ValueError:
                method = ConnectionMethod.CLOUDFLARE
            method_idx = self.connection_combo.findData(method.value)
            self.connection_combo.setCurrentIndex(method_idx if method_idx >= 0 else 0)
            self.hostname_edit.setText(project.public_hostname or "")
            self.git_name_edit.setText(project.git_user_name or "")
            self.git_email_edit.setText(project.git_user_email or "")
            self.git_remote_edit.setText(project.default_push_remote or "")
            self.git_branch_edit.setText(project.default_push_branch or "")
            self.bridge_check.setChecked(project.windows_enabled)
            self.gemini_uri_edit.setText(project.gemini_redirect_uri or "")
            self._current_token = _hub_access_token(ensure=True)
            self._tunnel_token_default = get_project_tunnel_token(project.id) or ""
            self.cf_token_edit.setText(self._tunnel_token_default)
            self._load_gemini_credential_view(project.gemini_redirect_uri)
            self.test_output.setText(self._test_outputs.get(project.id, "（尚未运行）"))
            if hasattr(self, "diag_output"):
                self.diag_output.setPlainText(self._diag_outputs.get(project.id, "（尚未诊断）"))
        finally:
            self._loading_project = False
        self._sync_connection_fields()
        self._sync_client_fields()
        self._sync_token_ui()
        self._update_gateway_port_ui()

    def _project_config(self) -> ProjectConfig | None:
        """Config of the currently selected table row, or None."""
        root = self._selected_root()
        if not root:
            return None
        project = self.pm.by_root(root)
        if project is None:
            project = next((p for p in load_projects() if p.root_path == root), None)
        return project

    def _selected_project_id(self) -> str:
        """Project id of the currently selected table row."""
        project = self._project_config()
        return project.id if project is not None else ""

    def _selected_root(self) -> str:
        row = self.project_table.currentRow()
        if row < 0:
            return ""
        item = self.project_table.item(row, 1)
        return str(item.text()) if item is not None else ""

    def _selected_permission_mode(self) -> PermissionMode:
        mode = PERMISSION_MODES[self.permission_combo.currentIndex()][0]
        if mode in ("read_only", "workspace", "system"):
            return cast(PermissionMode, mode)
        return "system"

    def _selected_execution_profile(self) -> str:
        """Command profile follows the selected permission mode."""
        return PERMISSION_PROFILE.get(self._selected_permission_mode(), "full_system")

    def _runtime_preflight(self) -> None:
        missing: list[str] = []
        if not find_node():
            missing.append("Node.js")
        if (
            IS_WINDOWS
            and any(project.windows_enabled for project in self.pm.list())
            and not find_uvx()
        ):
            missing.append("uv/uvx")
        if missing:
            self._append_log(
                "运行组件自检发现缺失："
                + "、".join(missing)
                + "。正式安装包应内置这些组件，请重新安装最新版。"
            )
        else:
            platform_name = "Windows" if IS_WINDOWS else "Linux/SteamOS"
            self._append_log(f"运行组件自检通过（{platform_name}）；无需额外安装运行时。")

    def _run_env_check(self) -> None:
        """Detect the default shell and probe the toolchain; no server needed."""
        self.test_output.setText("正在检测开发环境…")
        self.test_btn.setEnabled(False)
        self.env_btn.setEnabled(False)

        def work() -> list[str]:
            shell_info = get_shell_info()
            default = cast(dict[str, Any], shell_info.get("default") or {})
            detected = [
                str(s.get("name", ""))
                for s in cast(list[dict[str, Any]], shell_info.get("detected") or [])
            ]
            lines = [
                f"已检测 Shell: {'、'.join(detected) or '（无）'}",
                f"默认 Shell: {default.get('name')} ({default.get('path')})"
                + ("" if default.get("executable") else " —— 不可执行！"),
            ]
            for name, arg in (
                ("python", "--version"),
                ("git", "--version"),
                ("pytest", "--version"),
                ("pyright", "--version"),
            ):
                exe = shutil.which(name)
                if not exe:
                    lines.append(f"{name}: 未安装（PATH 中找不到）")
                    continue
                try:
                    res = _run_program(
                        exe,
                        [arg],
                        cwd=Path(self._selected_root() or os.getcwd()),
                        timeout_seconds=30,
                    )
                    text = (res.stdout or res.stderr or "").strip().splitlines()
                    lines.append(f"{name}: {text[0] if text else '(无输出)'}")
                except Exception as exc:  # noqa: BLE001
                    lines.append(f"{name}: 失败（{exc}）")
            return lines

        project_id = self._selected_project_id()

        def done(lines: list[str]) -> None:
            self.test_btn.setEnabled(True)
            self.env_btn.setEnabled(True)
            ok = all(
                not line.endswith(("未安装（PATH 中找不到）", "不可执行！"))
                and "失败（" not in line
                for line in lines[2:]
            )
            head = (
                "开发环境检测：工具链就绪，可运行测试/检查命令。"
                if ok
                else "开发环境检测：存在缺失或异常项。"
            )
            output = "\n".join([head, *lines])
            if project_id:
                self._test_outputs[project_id] = output
            if project_id == self._selected_project_id():
                self.test_output.setText(output)

        _run_async(work, done)

    def _selected_connection(self) -> ConnectionMethod:
        data = self.connection_combo.currentData()
        try:
            return ConnectionMethod(str(data))
        except ValueError:
            return ConnectionMethod.LOCAL

    def _on_project_selected(self) -> None:
        if self._loading_project:
            return
        selected = self._selected_root()
        if (
            self._loaded_project_root
            and selected
            and not _same_root(self._loaded_project_root, selected)
        ):
            self._save_project_settings(root_override=self._loaded_project_root, show_errors=False)
        self._apply_selected_project()
        self._poll_status()

    def _on_connection_changed(self) -> None:
        self._sync_connection_fields()
        self._autosave_project_settings()

    def _on_client_changed(self) -> None:
        self._sync_client_fields()
        self._autosave_project_settings()

    def _on_gemini_uri_edited(self) -> None:
        if self._loading_project:
            return
        if self._save_project_settings(show_errors=False):
            self._load_gemini_credential_view(self.gemini_uri_edit.text().strip())

    def _autosave_project_settings(self, *_args: object) -> None:
        if not self._loading_project:
            self._save_project_settings(show_errors=False)

    def _generate_gemini_credentials(self) -> None:
        project = self._project_config()
        if project is None:
            QMessageBox.warning(self, "未选择项目", "请先选择项目。")
            return
        uri = self.gemini_uri_edit.text().strip()
        if not uri:
            QMessageBox.warning(self, "缺少 URI", "请先粘贴 Gemini 的 redirect URI。")
            return
        try:
            client_id, secret = get_or_create_gemini_client(uri, rotate_secret=True)
        except ValueError as exc:
            QMessageBox.warning(self, "URI无效", str(exc))
            return
        project.client_target = "gemini"
        project.gemini_redirect_uri = uri
        self.pm.update(project)
        self._gemini_store.set(GEMINI_LAST_URI_CRED_NAME, uri)
        self.gemini_id_edit.setText(client_id)
        self._gemini_secret = secret
        self.gemini_secret_edit.setText("•" * 16)
        self._append_log(
            f"已更新 {project.display_name or project.root_path} 的 Gemini 授权信息。"
        )

    @staticmethod
    def _copy_text(text: str) -> None:
        QApplication.clipboard().setText(text)

    def _load_gemini_credential_view(self, uri: str) -> None:
        self.gemini_id_edit.setText("—")
        self.gemini_secret_edit.setText("—")
        self._gemini_secret = ""
        if not uri:
            return
        try:
            client_id, secret = get_or_create_gemini_client(uri, rotate_secret=False)
        except ValueError:
            return
        self.gemini_id_edit.setText(client_id)
        self._gemini_secret = secret
        self.gemini_secret_edit.setText("•" * 16)

    def _sync_connection_fields(self) -> None:
        method = self._selected_connection()
        need_domain = method in (ConnectionMethod.CLOUDFLARE, ConnectionMethod.NGROK)
        project = self._project_config()
        state = self._project_state(project)
        editable = bool(
            project is not None
            and not self._is_project_busy(project.id)
            and state in (EngineState.IDLE, EngineState.ERROR)
        )
        self.hostname_edit.setEnabled(need_domain and editable)
        self.cf_token_edit.setEnabled(method == ConnectionMethod.CLOUDFLARE and editable)
        if method == ConnectionMethod.CLOUDFLARE:
            self.hostname_edit.setPlaceholderText("例如 mcp.example.com")
        elif method == ConnectionMethod.NGROK:
            self.hostname_edit.setPlaceholderText("已保留的 ngrok 域名")
        elif method == ConnectionMethod.QUICK:
            self.hostname_edit.setPlaceholderText("启动后自动生成临时地址")
        else:
            self.hostname_edit.setPlaceholderText("仅本机，无需填写")

    def _sync_client_fields(self) -> None:
        is_gemini = str(self.client_combo.currentData() or "chatgpt") == "gemini"
        self.gemini_box.setVisible(is_gemini)

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
        root_str = str(root)
        project = self.pm.by_root(root_str)
        if project is None:
            try:
                project = self.pm.add(root_str)
            except ValueError as exc:
                QMessageBox.warning(self, "无法添加项目", str(exc))
                return
        self._app_config.active_workspace = root_str
        save_app_config(self._app_config)
        self._refresh_project_list()
        self._select_root(root_str)
        self._apply_selected_project()
        self._append_log(f"已添加项目：{project.display_name}（{root_str}）")

    # ----------------------------------------------- project engine control
    def _start_project_engine_for(self, project: ProjectConfig) -> None:
        access = self._ensure_workspace_credential(project.id)
        bridge = _bridge_token(ensure=project.windows_enabled)
        self._set_project_busy(project.id, True)
        self._append_log(f"正在启动项目（{project.display_name}）…")

        def run() -> str:
            self.pm.start(
                project.id,
                codex_token=access,
                permission_mode=project.permission_mode,
                execution_profile=PERMISSION_PROFILE.get(project.permission_mode, "full_system"),
                windows_token=bridge,
                elevated=bool(IS_WINDOWS and project.permission_mode == "system"),
            )
            if not self.coord.running:
                options = self._current_options()
                conflict = self._ports_conflict(options)
                if conflict:
                    self.pm.stop(project.id)
                    raise RuntimeError(conflict)
                self.coord.start(options)
                if self.coord.state != EngineState.READY:
                    self.pm.stop(project.id)
                    raise RuntimeError(self.coord.message or "连接服务未进入可用状态。")
            return f"项目已连接：{project.display_name}；连接服务保持可用"

        def done(result: Any) -> None:
            self._set_project_busy(project.id, False)
            if isinstance(result, Exception):
                self._append_log(f"启动项目失败：{result}")
            else:
                self._append_log(str(result))
            self._sync_token_ui()
            self._poll_status()

        _run_async(run, done)

    def _stop_project_engine_for(self, project: ProjectConfig) -> None:
        self._set_project_busy(project.id, True)
        self._append_log(f"正在停止项目（{project.display_name}）…")

        def run() -> str:
            self.pm.stop(project.id)
            remaining = [
                candidate
                for candidate in self.pm.list()
                if candidate.id != project.id
                and self._project_state(candidate)
                in (EngineState.STARTING, EngineState.READY, EngineState.STOPPING)
            ]
            if not remaining and (self.coord.running or self.coord.state == EngineState.ERROR):
                self.coord.stop()
                return f"项目已停止：{project.display_name}；已无运行项目，连接服务一并停止。"
            return f"项目已停止：{project.display_name}；其它运行项目和连接服务不受影响。"

        def done(result: Any) -> None:
            self._set_project_busy(project.id, False)
            self._append_log(
                str(result) if not isinstance(result, Exception) else f"停止项目出错：{result}"
            )
            self._poll_status()

        _run_async(run, done)

    def _remove_project(self) -> None:
        project = self._project_config()
        if project is None:
            QMessageBox.warning(self, "未选择项目", "请先选择或添加项目。")
            return
        answer = QMessageBox.question(
            self,
            "删除项目",
            f"确定从项目列表移除“{project.display_name}”吗？\n"
            "其引擎进程（若运行中）会一并停止，但磁盘上的项目文件不受影响。",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
        )
        if answer != QMessageBox.StandardButton.Yes:
            return

        def run() -> str:
            self.pm.remove(project.id)
            self._workspace_credentials.pop(project.id, None)
            if _same_root(project.root_path, self._app_config.active_workspace or ""):
                self._app_config.active_workspace = ""
                save_app_config(self._app_config)
            remaining = [
                candidate
                for candidate in self.pm.list()
                if self._project_state(candidate)
                in (EngineState.STARTING, EngineState.READY, EngineState.STOPPING)
            ]
            if not remaining and (self.coord.running or self.coord.state == EngineState.ERROR):
                self.coord.stop()
            return f"已移除项目：{project.display_name}"

        def done(result: Any) -> None:
            if isinstance(result, Exception):
                self._append_log(f"删除项目出错:{result}")
            else:
                self._append_log(str(result))
            self._refresh_project_list()
            self._apply_selected_project()
            self._poll_status()

        _run_async(run, done)

    def _has_active_projects(self) -> bool:
        for project in self.pm.list():
            if self._project_state(project) in (
                EngineState.STARTING,
                EngineState.READY,
                EngineState.STOPPING,
            ):
                return True
        return False

    def _sync_all_projects_button(self) -> None:
        projects = self.pm.list()
        if self._bulk_project_action == "start":
            self.all_projects_btn.setText("停止所有项目")
            self.all_projects_btn.setEnabled(False)
            return
        if self._bulk_project_action == "stop":
            self.all_projects_btn.setText("启动所有项目")
            self.all_projects_btn.setEnabled(False)
            return
        active = self._has_active_projects()
        any_busy = any(self._is_project_busy(project.id) for project in projects)
        self.all_projects_btn.setText("停止所有项目" if active else "启动所有项目")
        self.all_projects_btn.setEnabled(bool(projects) and not any_busy)

    def _toggle_all_projects(self) -> None:
        if self._bulk_project_action is not None:
            return
        if self._has_active_projects():
            self._stop_all_projects()
        else:
            self._start_all_projects()

    def _start_all_projects(self) -> None:
        projects = self.pm.list()
        if not projects:
            QMessageBox.warning(self, "没有项目", "请先添加项目。")
            return
        if any(self._is_project_busy(project.id) for project in projects):
            return
        if self._project_config() is None:
            self._select_root(projects[0].root_path)
            self._apply_selected_project()
        if not self._save_project_settings(show_errors=True):
            return
        projects = self.pm.list()
        if self._require_start_confirmations(projects):
            return
        options = self._current_options()
        conflict = self._ports_conflict(options)
        if conflict:
            QMessageBox.warning(self, "端口被占用", conflict)
            return
        access = {project.id: self._ensure_workspace_credential(project.id) for project in projects}
        bridge = _bridge_token(ensure=any(project.windows_enabled for project in projects))
        project_ids = {project.id for project in projects}
        self._bulk_project_action = "start"
        self._busy_project_ids.update(project_ids)
        self._append_log(f"正在并列启动全部 {len(projects)} 个项目；没有入口项目…")
        self._poll_status()

        def run() -> str:
            from concurrent.futures import ThreadPoolExecutor, as_completed

            failures: list[str] = []
            started_ids: list[str] = []
            max_workers = min(len(projects), 8)
            with ThreadPoolExecutor(max_workers=max_workers) as executor:
                futures = {
                    executor.submit(
                        self.pm.start,
                        project.id,
                        codex_token=access[project.id],
                        permission_mode=project.permission_mode,
                        execution_profile=PERMISSION_PROFILE.get(
                            project.permission_mode, "full_system"
                        ),
                        windows_token=bridge,
                        elevated=bool(IS_WINDOWS and project.permission_mode == "system"),
                    ): project
                    for project in projects
                }
                for future in as_completed(futures):
                    project = futures[future]
                    try:
                        future.result()
                        started_ids.append(project.id)
                    except Exception as exc:  # noqa: BLE001
                        failures.append(f"{project.display_name}: {exc}")
            if not started_ids:
                raise RuntimeError(
                    "没有任何项目成功启动。" + (f" {failures[0]}" if failures else "")
                )
            connection_issue = ""
            if not self.coord.running:
                try:
                    self.coord.start(options)
                except Exception as exc:  # noqa: BLE001
                    connection_issue = str(exc) or type(exc).__name__
            if self.coord.state != EngineState.READY and not connection_issue:
                connection_issue = self.coord.message or "连接服务未进入可用状态。"
            if (
                self.coord.state == EngineState.READY
                and options.connection != ConnectionMethod.LOCAL
                and not self.coord.tunnel.is_running
                and not connection_issue
            ):
                connection_issue = self.coord.message or (
                    "公网连接尚未就绪；本机项目引擎和连接服务保持可用。"
                )
            if connection_issue:
                failure_note = ""
                if failures:
                    preview = "；".join(failures[:3])
                    failure_note = f"；另有项目启动失败：{preview}"
                return (
                    f"项目引擎已启动：{len(started_ids)}/{len(projects)} 个平等运行根可用；"
                    f"共享连接未就绪：{connection_issue}。项目引擎保持运行，"
                    f"网络恢复或修正配置后可再次启动连接{failure_note}"
                )
            if failures:
                preview = "；".join(failures[:3])
                if len(failures) > 3:
                    preview += f"；另有 {len(failures) - 3} 个失败"
                return f"批量启动完成：{len(started_ids)}/{len(projects)} 个平等运行根可用。失败：{preview}"
            return f"批量启动完成：{len(projects)}/{len(projects)} 个平等运行根全部可用。"

        def done(result: Any) -> None:
            self._bulk_project_action = None
            self._busy_project_ids.difference_update(project_ids)
            self._append_log(
                str(result) if not isinstance(result, Exception) else f"启动所有项目失败：{result}"
            )
            self._sync_token_ui()
            self._poll_status()

        _run_async(run, done)

    def _stop_all_projects(self) -> None:
        projects = self.pm.list()
        if not projects:
            return
        project_ids = {project.id for project in projects}
        self._bulk_project_action = "stop"
        self._busy_project_ids.update(project_ids)
        self._append_log(f"正在停止全部 {len(projects)} 个项目…")
        self._poll_status()

        def run() -> str:
            from concurrent.futures import ThreadPoolExecutor, as_completed

            failures: list[str] = []
            if self.coord.running or self.coord.state == EngineState.ERROR:
                try:
                    self.coord.stop()
                except Exception as exc:  # noqa: BLE001
                    failures.append(f"连接服务: {exc}")
            max_workers = min(len(projects), 8)
            with ThreadPoolExecutor(max_workers=max_workers) as executor:
                futures = {
                    executor.submit(self.pm.stop, project.id): project for project in projects
                }
                for future in as_completed(futures):
                    project = futures[future]
                    try:
                        future.result()
                    except Exception as exc:  # noqa: BLE001
                        failures.append(f"{project.display_name}: {exc}")
            if failures:
                preview = "；".join(failures[:3])
                if len(failures) > 3:
                    preview += f"；另有 {len(failures) - 3} 个失败"
                return f"停止完成，但有 {len(failures)} 项异常：{preview}"
            return f"已停止全部 {len(projects)} 个项目和连接服务。"

        def done(result: Any) -> None:
            self._bulk_project_action = None
            self._busy_project_ids.difference_update(project_ids)
            self._append_log(
                str(result) if not isinstance(result, Exception) else f"停止所有项目出错：{result}"
            )
            self._poll_status()

        _run_async(run, done)

    def _lookup_workspace(self, project_id: str) -> tuple[int, str] | None:
        """Return one running project's engine port/root for Hub routing."""
        project = self.pm.get(project_id)
        if project is None:
            return None
        unit = self.pm.unit(project_id)
        if unit is not None and unit.state == EngineState.READY:
            port = project.codexpro_port or constants.DEFAULT_CODEXPRO_PORT
            return (port, project.root_path)
        return None

    def _lookup_configured_workspace(self, project_id: str) -> str | None:
        """Return a configured root even while its engine is restarting."""
        project = self.pm.get(project_id)
        return project.root_path if project is not None else None

    def _project_runtime_snapshot(self) -> list[dict[str, object]]:
        """Cheap, bounded component snapshot for the on-disk flight recorder."""
        rows: list[dict[str, object]] = []
        for project in self.pm.list()[:32]:
            unit = self.pm.unit(project.id)
            rows.append(
                {
                    "project_id": project.id,
                    "state": str(unit.state if unit is not None else EngineState.IDLE),
                    "engine_pid": unit.engine_pid if unit is not None else None,
                    "engine_port": project.codexpro_port or constants.DEFAULT_CODEXPRO_PORT,
                    "windows_enabled": bool(project.windows_enabled),
                }
            )
        return rows

    def _ensure_workspace_credential(self, project_id: str) -> str:
        value = self._workspace_credentials.get(project_id)
        if value:
            return value
        value = ensure_project_access_token(project_id)
        self._workspace_credentials[project_id] = value
        return value

    def _lookup_workspace_credential(self, project_id: str) -> str | None:
        return self._workspace_credentials.get(project_id)

    # -------------------------------------------------- service control
    def _toggle_service_for(self, project_root: str) -> None:
        project = self.pm.by_root(project_root)
        if project is None or self._is_project_busy(project.id):
            return
        self._select_root(project.root_path)
        self._apply_selected_project()
        unit = self.pm.unit(project.id)
        if unit is not None and unit.state == EngineState.READY:
            self._stop_project_engine_for(project)
            return
        if not self._save_project_settings(show_errors=True):
            return
        project = self.pm.get(project.id) or project
        if self._require_start_confirmations([project]):
            return
        self._start_project_engine_for(project)

    def _current_options(self) -> StartOptions:
        return StartOptions(
            connection=self._selected_connection(),
            public_hostname=self.hostname_edit.text().strip(),
            tunnel_token=self._tunnel_token_value(),
            gateway_port=self._app_config.gateway_port,
        )

    def _tunnel_token_value(self) -> str | None:
        field = self.cf_token_edit.text().strip()
        return field or self._tunnel_token_default or None

    def _on_tunnel_token_edited(self, value: str) -> None:
        if self._loading_project or not value.strip():
            return
        project = self._project_config()
        if project is None:
            return
        remember_project_tunnel_token(project.id, value)
        self._tunnel_token_default = value.strip()

    def _admin_setup_choice(self) -> str:
        dialog = QMessageBox(self)
        dialog.setIcon(QMessageBox.Icon.Warning)
        dialog.setWindowTitle(ADMIN_SETUP_TITLE)
        dialog.setText(ADMIN_SETUP_TEXT)
        fallback = dialog.addButton(
            "以普通权限启动", QMessageBox.ButtonRole.AcceptRole
        )
        retry = dialog.addButton("重新设置", QMessageBox.ButtonRole.ActionRole)
        cancel = dialog.addButton("取消", QMessageBox.ButtonRole.RejectRole)
        dialog.setDefaultButton(retry)
        dialog.exec()
        clicked = dialog.clickedButton()
        if clicked is retry:
            return "retry"
        if clicked is fallback:
            return "workspace"
        if clicked is cancel:
            return "cancel"
        return "cancel"

    def _use_workspace_recovery(self, projects: list[ProjectConfig]) -> bool:
        changed = [project for project in projects if project.permission_mode == "system"]
        if not changed:
            return True
        affected_ids = {project.id for project in changed}
        try:
            catalog = self.pm.list()
            for stored in catalog:
                if stored.id in affected_ids:
                    stored.permission_mode = "workspace"
            save_projects(catalog)
            for project in changed:
                project.permission_mode = "workspace"
        except Exception as exc:
            self._append_log(f"普通权限恢复失败：{type(exc).__name__}: {exc}")
            QMessageBox.warning(
                self,
                "无法使用普通权限启动",
                "程序没能保存新的权限设置。请打开“项目设置”，选择“项目工作区”后再试。",
            )
            return False
        self._append_log(
            "管理员权限设置未完成；已按你的选择改为“项目工作区”，继续启动项目。"
        )
        self._apply_selected_project()
        return True

    def _require_start_confirmations(self, projects: list[ProjectConfig] | None = None) -> bool:
        """Return True when startup must stop because a required confirmation failed."""
        needs_system = (
            any(project.permission_mode == "system" for project in projects)
            if projects is not None
            else self._selected_permission_mode() == "system"
        )
        if needs_system and (
            not self._app_config.first_system_risk_accepted
            or not self._app_config.full_system_risk_accepted
        ):
            answer = QMessageBox.question(
                self,
                "完全访问风险确认",
                '“完全访问”允许 AI 操作项目目录之外的文件，并执行系统级操作。\n'
                "请确认你了解这个权限范围后继续（只需确认一次）。",
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            )
            if answer == QMessageBox.StandardButton.No:
                return True
            self._app_config.first_system_risk_accepted = True
            self._app_config.full_system_risk_accepted = True
            save_app_config(self._app_config)
        if needs_system and IS_WINDOWS:
            target_projects = list(projects or [])
            while True:
                detail = "Windows 管理员授权没有完成。"
                try:
                    from .elevation import get_elevation_controller

                    controller = get_elevation_controller()
                    if controller.ensure_registered(interactive=True):
                        controller.ensure_running(interactive_registration=False)
                        break
                except Exception as exc:
                    detail = f"{type(exc).__name__}: {exc}"
                self._append_log(f"管理员权限设置未完成：{detail}")
                choice = self._admin_setup_choice()
                if choice == "retry":
                    continue
                if choice == "workspace":
                    if target_projects and self._use_workspace_recovery(target_projects):
                        break
                    return True
                return True
        return False

    def _save_git_settings(self) -> None:
        if self._save_project_settings(show_errors=True):
            self._append_log("Git 参数已保存。")

    def _save_settings_for_all_projects(self) -> None:
        projects = self.pm.list()
        source = self._project_config()
        if source is None or not projects:
            QMessageBox.warning(self, "没有项目", "请先添加并选择一个项目。")
            return
        active = [
            project
            for project in projects
            if self._is_project_busy(project.id)
            or self._project_state(project)
            in (EngineState.STARTING, EngineState.READY, EngineState.STOPPING)
        ]
        if active or self.coord.running:
            QMessageBox.warning(
                self, "项目正在运行", "请先停止所有项目，再批量保存连接与权限设置。"
            )
            return
        if not self._save_project_settings(show_errors=True):
            return
        source = self.pm.get(source.id) or source
        tunnel_token = self._tunnel_token_value() or ""
        for project in self.pm.list():
            target = self.pm.get(project.id) or project
            target.permission_mode = source.permission_mode
            target.client_target = source.client_target
            target.connection = source.connection
            target.public_hostname = source.public_hostname
            target.windows_enabled = source.windows_enabled
            target.gemini_redirect_uri = source.gemini_redirect_uri
            self.pm.update(target)
            if tunnel_token:
                remember_project_tunnel_token(target.id, tunnel_token)
            else:
                clear_project_tunnel_token(target.id)
        self._append_log(f"已将连接与权限设置保存到 {len(projects)} 个项目。")
        QMessageBox.information(
            self, "批量保存完成", f"连接与权限已同步到 {len(projects)} 个项目。"
        )
        self._refresh_project_list()

    def _save_project_settings(
        self,
        root_override: str | None = None,
        *,
        show_errors: bool = True,
    ) -> bool:
        root = root_override or self._selected_root()
        if not root:
            return False
        project = self.pm.by_root(root)
        if project is None:
            return False
        git_vals = {
            "git_user_name": self.git_name_edit.text().strip(),
            "git_user_email": self.git_email_edit.text().strip(),
            "default_push_remote": self.git_remote_edit.text().strip(),
            "default_push_branch": self.git_branch_edit.text().strip(),
        }
        for kind, value in git_vals.items():
            error = git_field_error(kind, value)
            if error is not None:
                if show_errors:
                    QMessageBox.warning(self, "Git 参数不合法", error)
                return False
        project.permission_mode = self._selected_permission_mode()
        project.client_target = cast(Any, str(self.client_combo.currentData() or "chatgpt"))
        project.connection = self._selected_connection().value
        project.public_hostname = self.hostname_edit.text().strip()
        project.windows_enabled = bool(IS_WINDOWS and self.bridge_check.isChecked())
        project.gemini_redirect_uri = self.gemini_uri_edit.text().strip()
        self._app_config.gateway_port = self.gateway_port_spin.value()
        project.git_user_name = git_vals["git_user_name"]
        project.git_user_email = git_vals["git_user_email"]
        project.default_push_remote = git_vals["default_push_remote"]
        project.default_push_branch = git_vals["default_push_branch"]
        self.pm.update(project)
        self._app_config.active_workspace = root
        save_app_config(self._app_config)
        return True

    def _set_project_busy(self, project_id: str, busy: bool) -> None:
        if not project_id:
            return
        if busy:
            self._busy_project_ids.add(project_id)
        else:
            self._busy_project_ids.discard(project_id)
        self._poll_status()

    def _is_project_busy(self, project_id: str) -> bool:
        return bool(project_id and project_id in self._busy_project_ids)

    def _project_state(self, project: ProjectConfig | None) -> EngineState:
        if project is None:
            return EngineState.IDLE
        unit = self.pm.unit(project.id)
        return unit.state if unit is not None else EngineState.IDLE

    def _service_url_text(self) -> str:
        return f"本机连接地址：{gateway_service_url(self.gateway_port_spin.value())}"

    def _on_gateway_port_changed(self, _value: int) -> None:
        if not self._loading_project:
            self._app_config.gateway_port = self.gateway_port_spin.value()
            save_app_config(self._app_config)
        self._update_gateway_port_ui()

    def _update_gateway_port_ui(self) -> None:
        self.service_url_edit.setText(self._service_url_text())
        port = self.gateway_port_spin.value()
        if port != constants.DEFAULT_GATEWAY_PORT:
            self.port_warn_label.setText(
                f"本机连接编号修改后，请同步更新 Cloudflare 的连接地址为 "
                f"{gateway_service_url(port)}，否则公网连接会失败。"
            )
            self.port_warn_label.setVisible(True)
        else:
            self.port_warn_label.setVisible(False)

    def _check_gateway_port(self) -> None:
        port = self.gateway_port_spin.value()
        if self.coord.running:
            QMessageBox.information(self, "连接服务正在运行", f"当前正在使用本机连接编号 {port}。")
        elif port_in_use(port):
            QMessageBox.warning(
                self,
                "本机连接设置被占用",
                f"本机连接编号 {port} 已被其他程序占用。\n请关闭占用程序，或改用其他编号。",
            )
        else:
            QMessageBox.information(
                self, "连接检查", f"本机连接编号 {port} 当前可用。"
            )

    def _restore_default_gateway_port(self) -> None:
        if self.coord.running:
            QMessageBox.warning(self, "连接服务正在运行", "请先停止所有项目，再修改本机连接设置。")
            return
        self.gateway_port_spin.setValue(constants.DEFAULT_GATEWAY_PORT)
        QMessageBox.information(
            self,
            "已恢复默认",
            f"本机连接编号已恢复为默认值 {constants.DEFAULT_GATEWAY_PORT}。",
        )

    def _open_advanced_settings(self) -> None:
        project = self._project_config()
        if project is None:
            QMessageBox.warning(self, "未选择项目", "请先选择项目。")
            return
        unit = self.pm.unit(project.id)
        if unit is not None and unit.is_running:
            QMessageBox.warning(self, "项目正在运行", "请先停止这个项目，再修改内部端口。")
            return
        self.pm.ensure_ports(project)
        dialog = QDialog(self)
        dialog.setWindowTitle(f"高级设置 · {project.display_name or Path(project.root_path).name}")
        form = QFormLayout(dialog)
        hub_label = QLineEdit(str(self._app_config.gateway_port))
        hub_label.setReadOnly(True)
        form.addRow("连接服务:", hub_label)
        codex_spin = QSpinBox()
        codex_spin.setRange(1, 65535)
        codex_spin.setValue(project.codexpro_port or constants.DEFAULT_CODEXPRO_PORT)
        form.addRow("开发服务:", codex_spin)
        windows_spin = QSpinBox()
        windows_spin.setRange(1, 65535)
        windows_spin.setValue(project.windows_bridge_port or constants.DEFAULT_WINDOWS_MCP_PORT)
        form.addRow("Windows 控制:", windows_spin)
        legacy_spin = QSpinBox()
        legacy_spin.setRange(1, 65535)
        legacy_spin.setValue(self._app_config.legacy_backend_port)
        form.addRow("兼容服务（全局）:", legacy_spin)
        buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel
        )
        buttons.accepted.connect(dialog.accept)
        buttons.rejected.connect(dialog.reject)
        form.addRow(buttons)
        if dialog.exec() != QDialog.DialogCode.Accepted:
            return
        ports = (self._app_config.gateway_port, codex_spin.value(), windows_spin.value())
        if len(set(ports)) != 3:
            QMessageBox.warning(
                self, "内部连接设置冲突", "这些内部连接编号必须互不相同。"
            )
            return
        project.codexpro_port = codex_spin.value()
        project.windows_bridge_port = windows_spin.value()
        self._app_config.legacy_backend_port = legacy_spin.value()
        save_app_config(self._app_config)
        try:
            self.pm.reconfigure(project)
        except Exception as exc:
            QMessageBox.warning(self, "保存失败", str(exc))
            return
        self._append_log(
            f"{project.display_name} 的内部连接设置已保存。"
        )
        self._refresh_project_list()
        self._update_gateway_port_ui()

    def _ports_conflict(self, options: StartOptions) -> str | None:
        if not self.coord.running and port_in_use(options.gateway_port):
            return f"本机连接编号 {options.gateway_port} 已被占用。请先停止占用它的程序。"
        return None

    # -------------------------------------------------- coordinator events
    def _emit_coord_event(self, state: EngineState, message: str | None) -> None:
        self._signals.coord_event.emit(state, message)

    def _on_coord_event(self, state: EngineState, message: str | None) -> None:
        if message:
            self._append_log(f"[{state.value}] {message}")

    # ------------------------------------------------------- status / URL
    def _local_url(self) -> str:
        return f"http://127.0.0.1:{self._app_config.gateway_port}{constants.DEFAULT_MCP_PATH}"

    def _display_url(self) -> str:
        project = self._project_config()
        if self.coord.public_url:
            return self.coord.public_url
        if project is not None:
            try:
                method = ConnectionMethod(project.connection)
            except ValueError:
                method = ConnectionMethod.LOCAL
            host = project.public_hostname.strip().rstrip("/")
            if method in (ConnectionMethod.CLOUDFLARE, ConnectionMethod.NGROK) and host:
                return f"https://{host}/mcp"
        return self._local_url()

    def _refresh_url_ui(self) -> None:
        project = self._project_config()
        if project is None:
            self.url_edit.setText("选择项目后显示")
            self.service_url_edit.setText("选择项目后显示")
            self.port_warn_label.setVisible(False)
            return
        url = self._display_url()
        method = self._selected_connection()
        if self.coord.public_url:
            suffix = "临时地址" if self.coord.url_mutable else "固定地址"
        elif method == ConnectionMethod.QUICK:
            suffix = "启动后生成临时地址"
        elif method in (ConnectionMethod.CLOUDFLARE, ConnectionMethod.NGROK):
            suffix = "固定配置"
        else:
            suffix = "仅本机"
        self.url_edit.setText(f"{url}  ·  {suffix}")
        self._update_gateway_port_ui()

    def _poll_status(self) -> None:
        selected = self._project_config()
        selected_unit = self.pm.unit(selected.id) if selected is not None else None
        state = self._project_state(selected)
        busy = self._is_project_busy(selected.id) if selected is not None else False
        if state == EngineState.ERROR:
            self.status_label.setText("状态：连接失败")
        else:
            friendly_state = {
                EngineState.IDLE: "未启动",
                EngineState.STARTING: "正在连接",
                EngineState.READY: "可以使用",
                EngineState.STOPPING: "正在停止",
            }.get(state, state.value)
            self.status_label.setText(f"状态：{friendly_state}")
        self.add_project_btn.setEnabled(True)
        self.remove_project_btn.setEnabled(
            selected is not None and not busy and state in (EngineState.IDLE, EngineState.ERROR)
        )
        self.test_btn.setEnabled(selected is not None and state == EngineState.READY)
        editable = bool(
            selected is not None and not busy and state in (EngineState.IDLE, EngineState.ERROR)
        )
        self.gateway_port_spin.setEnabled(
            not self.coord.running and not self._has_active_projects()
        )
        for widget in (
            self.advanced_btn,
            self.permission_combo,
            self.client_combo,
            self.connection_combo,
        ):
            widget.setEnabled(editable)
        self.bridge_check.setEnabled(bool(IS_WINDOWS and editable))
        all_settings_editable = bool(
            editable
            and not self._has_active_projects()
            and not any(self._is_project_busy(project.id) for project in self.pm.list())
        )
        self.save_all_project_settings_btn.setEnabled(all_settings_editable)
        self._sync_all_projects_button()
        self._sync_connection_fields()
        components = self.coord.component_states()
        codex_state = state.value
        gateway_state = components.get("gateway", EngineState.IDLE).value
        tunnel_state = components.get("tunnel", EngineState.IDLE).value
        windows_state = (
            selected_unit.windows.state.value
            if IS_WINDOWS and selected_unit is not None
            else (EngineState.IDLE.value if IS_WINDOWS else "不适用")
        )
        bridge_label = "Windows 控制" if IS_WINDOWS else "Linux 原生工具"
        self.component_status.setText(
            f"项目服务：{codex_state} · 连接服务：{gateway_state} · "
            f"公网连接：{tunnel_state} · {bridge_label}：{windows_state}"
        )
        self._refresh_project_list()
        self._refresh_device_table()
        self._refresh_url_ui()
        self._poll_gateway_log()

    def _poll_gateway_log(self) -> None:
        """Auto-refresh gateway log every ~5 seconds (timer every 3s, refresh every 2nd call)."""
        if not hasattr(self, "_gw_poll_count"):
            self._gw_poll_count = 0
        self._gw_poll_count += 1
        if (
            self._gw_poll_count % 2 == 0
            and hasattr(self, "logs_tab")
            and self.tabs.currentWidget() is self.logs_tab
            and self.log_tabs.currentWidget() is self.gateway_log_page
        ):
            self._refresh_gateway_log()

    # ------------------------------------------------------ token helpers
    def _sync_token_ui(self) -> None:
        self._current_token = _hub_access_token(ensure=True)
        self.token_edit.setText(self._current_token or "尚未生成")

    def _copy_to_clipboard(self, text: str) -> None:
        if not text:
            return
        QApplication.clipboard().setText(text)

    def _regenerate_token(self) -> None:
        if self._has_active_projects() or self.coord.running:
            QMessageBox.warning(
                self, "连接服务正在运行", "请先停止所有项目，再重新生成连接访问码。"
            )
            return
        self._append_log("正在重新生成连接访问码…")

        def run() -> str:
            return _hub_access_token(regenerate=True)

        def done(result: Any) -> None:
            if isinstance(result, Exception):
                self._append_log(f"访问码生成失败：{result}")
                return
            self._current_token = str(result)
            self._sync_token_ui()
            self._append_log("已重新生成连接访问码。")

        _run_async(run, done)

    def _run_selftest(self) -> None:
        project = self._project_config()
        if project is None:
            self.test_output.setText("（请先选择一个运行项目作为自测目标）")
            return
        unit = self.pm.unit(project.id)
        if unit is None or unit.state != EngineState.READY or self.coord.state != EngineState.READY:
            self.test_output.setText("（请先启动当前项目和连接服务）")
            return
        project_id = project.id
        url = self.coord.public_url or self._local_url()
        access_value = _hub_access_token(ensure=True)
        self.test_btn.setEnabled(False)
        self.test_output.setText(f"正在检查连接 {url} …")

        def run() -> SelftestResult:
            return run_selftest(url, access_value or None)

        def done(result: Any) -> None:
            self.test_btn.setEnabled(True)
            if isinstance(result, Exception):
                output = f"自测异常：{result}"
            else:
                lines = [
                    f"{'✔' if s['ok'] else '✘'}  {s['step']}：{s['detail']}" for s in result.steps
                ]
                output = "\n".join(lines) if lines else "（无步骤）"
                if result.ok:
                    self._append_log("连接自测通过")
                else:
                    self._append_log(f"连接自测未通过：{result.error or '有步骤失败'}")
            self._test_outputs[project_id] = output
            if project_id == self._selected_project_id():
                self.test_output.setText(output)

        _run_async(run, done)

    def _append_log(self, text: str) -> None:
        now = datetime.datetime.now().strftime("%H:%M:%S")
        self.log_text.appendPlainText(f"[{now}] {text}")
        scrollbar = self.log_text.verticalScrollBar()
        scrollbar.setValue(scrollbar.maximum())

    def _friendly_tool_name(self, name: str) -> str:
        labels = {
            "read_file": "读取文件",
            "read_files": "读取多个文件",
            "write_file": "写入文件",
            "replace_text": "修改文件内容",
            "apply_patch": "应用代码补丁",
            "delete_path": "删除文件/目录",
            "list_directory": "查看目录",
            "search_files": "搜索文件",
            "search_text": "搜索代码内容",
            "run_command": "执行命令",
            "run_program": "运行程序",
            "start_process": "启动后台进程",
            "stop_process": "停止后台进程",
            "git_status": "查看 Git 状态",
            "git_diff": "查看代码差异",
            "git_commit": "提交 Git",
            "git_push": "推送 Git",
            "git_restore": "恢复 Git 文件",
            "devbridge_list_workspaces": "查看项目列表",
            "devbridge_switch_workspace": "显式项目覆盖",
            "devbridge_list_devices": "查看电脑列表",
            "devbridge_switch_device": "切换电脑",
            "get_task": "查看任务",
            "wait_task": "等待任务",
            "list_tasks": "查看任务列表",
            "cancel_task": "取消任务",
            "shell_self_test": "检查开发环境",
        }
        return labels.get(name, name.replace("_", " ") or "未知操作")

    def _friendly_parameters(self, summary: Any) -> str:
        if not isinstance(summary, dict) or not summary:
            return "—"
        parts: list[str] = []
        if summary.get("path"):
            parts.append(f"文件：{summary['path']}")
        if summary.get("project_id"):
            parts.append(f"项目：{summary['project_id']}")
        if summary.get("device_id"):
            parts.append(f"电脑：{summary['device_id']}")
        if "command" in summary:
            parts.append("命令内容已隐藏（保护隐私）")
        if not parts:
            visible = [f"{key}={value}" for key, value in summary.items() if value != "<redacted>"]
            parts.extend(visible[:3])
        return "；".join(parts) if parts else "敏感参数已隐藏"

    def _friendly_client_name(self, value: str) -> str:
        lowered = value.lower()
        if "chatgpt" in lowered or "openai" in lowered:
            return "ChatGPT"
        if "gemini" in lowered or "google" in lowered:
            return "Gemini"
        return "网页客户端" if value else "—"

    def _friendly_process_line(self, line: str) -> str:
        lowered = line.lower()
        if "error" in lowered or "failed" in lowered or "exception" in lowered:
            return f"发生错误：{line.strip()}"
        if "listening" in lowered or "ready" in lowered:
            return "服务已准备好，可以接收请求。"
        if "trycloudflare.com" in lowered:
            return "Quick Tunnel 已获得新的临时公网地址。"
        if "connected" in lowered and ("cloudflare" in lowered or "tunnel" in lowered):
            return "公网连接已经建立。"
        if "starting" in lowered or "spawn" in lowered:
            return "正在启动服务进程。"
        return line.strip()

    def _friendly_gateway_record(self, record: dict[str, Any]) -> str | None:
        path = str(record.get("path") or "")
        status = int(record.get("status") or record.get("upstream_status") or 0)
        event = str(record.get("event") or "")
        method = str(record.get("jsonrpc_method") or "")
        stamp = str(record.get("timestamp") or "")
        when = stamp[11:19] if len(stamp) >= 19 else "--:--:--"
        if path == "/device/heartbeat" and status < 400:
            return None
        if event == "device_paired":
            message = "新电脑已成功连接。"
        elif "consent" in path:
            message = "Gemini 授权流程已到达这台电脑。"
        elif method == "initialize":
            message = "网页端正在建立连接。"
        elif method == "tools/list":
            message = "网页端已成功获取可用功能列表。"
        elif path == "/mcp":
            message = "收到了一次来自网页端的请求。"
        elif path.startswith("/device/"):
            message = "收到了一次多设备连接请求。"
        else:
            message = f"收到网络请求：{path or '/'}"
        if status >= 400 or record.get("error"):
            message += f" 结果：失败（{record.get('error') or 'HTTP ' + str(status)}）"
        elif status:
            message += " 结果：正常。"
        return f"[{when}] {message}"

    # ------------------------------------------------- logs: process tail
    def _engine_log_source(self) -> list[str]:
        project = self._project_config()
        if project is None:
            return []
        key = str(self.proc_combo.currentData() or "service")
        unit = self.pm.unit(project.id)
        manager: Any = None
        if key == "service" and unit is not None:
            manager = unit.codex
        elif key == "windows" and unit is not None:
            manager = unit.windows
        elif key == "tunnel" and self.coord.running:
            manager = self.coord.tunnel
        if manager is None:
            return []
        try:
            text = manager.log_tail(400)
        except Exception:
            return []
        return [line for line in text.splitlines() if line.strip()]

    def _refresh_process_log(self) -> None:
        lines = self._engine_log_source()[-400:]
        self.proc_empty.setVisible(not lines)
        self.proc_view.setVisible(bool(lines))
        self.proc_view.setRowCount(len(lines))
        source = self.proc_combo.currentText()
        for row, line in enumerate(lines):
            self.proc_view.setItem(row, 0, QTableWidgetItem(str(row + 1)))
            self.proc_view.setItem(row, 1, QTableWidgetItem(source))
            self.proc_view.setItem(row, 2, QTableWidgetItem(self._friendly_process_line(line)))
        self.proc_view.setColumnWidth(0, 55)
        self.proc_view.setColumnWidth(1, 110)

    # ------------------------------------------------- logs: audit page
    def _refresh_audit_tool_combo(self) -> None:
        current = str(self.audit_tool_combo.currentData() or "")
        names = available_tool_names()
        self.audit_tool_combo.blockSignals(True)
        self.audit_tool_combo.clear()
        self.audit_tool_combo.addItem("全部操作", "")
        for name in names:
            self.audit_tool_combo.addItem(self._friendly_tool_name(name), name)
        index = self.audit_tool_combo.findData(current)
        self.audit_tool_combo.setCurrentIndex(index if index >= 0 else 0)
        self.audit_tool_combo.blockSignals(False)

    def _refresh_audit_log(self) -> None:
        day_mode = self.audit_day_combo.currentIndex()
        tool = str(self.audit_tool_combo.currentData() or "")
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
        self.audit_empty.setVisible(not records)
        self.audit_view.setVisible(bool(records))
        self.audit_view.setRowCount(len(records))
        for row, record in enumerate(records):
            duration = record.get("duration_ms", "")
            self.audit_view.setItem(
                row, 0, QTableWidgetItem(str(record.get("timestamp", ""))[11:19])
            )
            self.audit_view.setItem(
                row, 1, QTableWidgetItem(self._friendly_tool_name(str(record.get("tool_name", ""))))
            )
            self.audit_view.setItem(
                row, 2, QTableWidgetItem("成功" if record.get("success") else "失败")
            )
            self.audit_view.setItem(
                row, 3, QTableWidgetItem(f"{duration} ms" if duration != "" else "—")
            )
            self.audit_view.setItem(
                row,
                4,
                QTableWidgetItem(self._friendly_client_name(str(record.get("client_name", "")))),
            )
            self.audit_view.setItem(
                row, 5, QTableWidgetItem(self._friendly_parameters(record.get("parameter_summary")))
            )
        self.audit_view.setColumnWidth(0, 70)
        self.audit_view.setColumnWidth(1, 120)
        self.audit_view.setColumnWidth(2, 60)
        self.audit_view.setColumnWidth(3, 75)
        self.audit_view.setColumnWidth(4, 90)

    def _resume_upgrade_if_requested(self) -> None:
        """Consume installer handoff and restore all previously running roots equally.

        ``project_root`` is accepted only as a legacy handoff field. It is folded
        into ``project_roots`` and never treated as an entry/owner project.
        """
        request_file = constants.config_dir() / "upgrade-resume.json"
        if not request_file.is_file():
            return
        try:
            import json

            payload = json.loads(request_file.read_text(encoding="utf-8"))
        except Exception as exc:
            self._append_log(f"升级接力文件无法读取：{exc}")
            request_file.unlink(missing_ok=True)
            return
        request_file.unlink(missing_ok=True)
        raw_project_roots = payload.get("project_roots") or []
        project_roots = (
            [str(item).strip() for item in raw_project_roots if str(item).strip()]
            if isinstance(raw_project_roots, list)
            else []
        )
        legacy_root = str(payload.get("project_root") or "").strip()
        if legacy_root and legacy_root not in project_roots:
            project_roots.append(legacy_root)
        targets: list[ProjectConfig] = []
        seen: set[str] = set()
        for root in project_roots:
            project = self.pm.by_root(root)
            if project is None:
                self._append_log(f"升级接力跳过不存在的项目：{root}")
                continue
            if project.id in seen:
                continue
            seen.add(project.id)
            if project.permission_mode == "system" and (
                not self._app_config.first_system_risk_accepted
                or not self._app_config.full_system_risk_accepted
            ):
                self._append_log(
                    f"升级接力未自动恢复 {project.display_name or project.root_path}：完全访问模式尚未确认。"
                )
                continue
            targets.append(project)
        if not targets:
            self._append_log("升级接力没有需要恢复的运行项目。")
            return
        self._select_root(targets[0].root_path)
        self._apply_selected_project()
        self._append_log(
            f"检测到升级接力请求，正在平等恢复 {len(targets)} 个运行根；没有入口项目。"
        )
        for project in targets:
            self._start_project_engine_for(project)

    # --------------------------------------------------------------- end
    def closeEvent(self, event: Any) -> None:  # noqa: N802 - Qt naming
        if (
            not self._force_exit
            and self._app_config.close_behavior == "tray"
            and self.tray_icon.isVisible()
        ):
            event.ignore()
            self.hide()
            if not self._tray_hint_shown and self.tray_icon.isVisible():
                self.tray_icon.showMessage(
                    "MCP DevBridge",
                    "程序仍在后台运行。右键托盘图标可退出。",
                    QSystemTrayIcon.MessageIcon.Information,
                    2500,
                )
                self._tray_hint_shown = True
            return
        if self._closing:
            event.ignore()
            return
        units = [self.pm.unit(project.id) for project in self.pm.list()]
        any_running = any(unit is not None and unit.is_running for unit in units)
        if not self.coord.running and not any_running:
            self.tray_icon.hide()
            event.accept()
            QTimer.singleShot(0, QApplication.quit)
            return
        event.ignore()
        self._closing = True
        self.hide()

        def cleanup() -> None:
            self.coord.stop()
            self.pm.stop_all()
            if IS_WINDOWS:
                with contextlib.suppress(Exception):
                    from .elevation import get_elevation_controller

                    get_elevation_controller().shutdown_if_idle()

        def done(_result: Any) -> None:
            self.tray_icon.hide()
            QApplication.quit()

        _run_async(cleanup, done)


_APP_LOCK: QLockFile | None = None


def main() -> int:
    global _APP_LOCK
    os.environ.setdefault("PYTHONIOENCODING", "utf-8")
    app = QApplication(sys.argv)
    app.setQuitOnLastWindowClosed(False)
    constants.CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    lock = QLockFile(str(constants.CONFIG_DIR / "desktop-instance.lock"))
    lock.setStaleLockTime(30_000)
    if not lock.tryLock(100):
        lock.removeStaleLockFile()
        if not lock.tryLock(100):
            QMessageBox.information(
                None,
                "MCP DevBridge 已在运行",
                "只能运行一个 MCP DevBridge。请从任务栏或系统托盘打开已运行的窗口。",
            )
            return 0
    _APP_LOCK = lock
    font = QFont(app.font())
    font.setPointSize(10)
    app.setFont(font)
    app.setStyleSheet(
        """
        QMainWindow, QScrollArea { background: #f5f7fa; }
        QScrollArea { border: none; }
        QLabel#PageTitle { font-size: 24px; font-weight: 700; color: #111827; }
        QLabel#PageSubtitle { color: #6b7280; font-size: 12px; }
        QLabel#VersionBadge { background: #e8eefc; color: #3558a8; border-radius: 10px; padding: 4px 10px; font-weight: 600; }
        QLabel#MutedText { color: #6b7280; }
        QTabWidget::pane { border: none; top: -1px; }
        QTabBar::tab { background: transparent; color: #64748b; padding: 9px 14px; margin-right: 4px; border-bottom: 2px solid transparent; }
        QTabBar::tab:selected { color: #1d4ed8; border-bottom: 2px solid #2563eb; font-weight: 600; }
        QGroupBox { background: #ffffff; border: 1px solid #e5e7eb; border-radius: 10px; margin-top: 20px; padding-top: 14px; font-size: 13px; font-weight: 600; color: #111827; }
        QGroupBox::title { subcontrol-origin: margin; subcontrol-position: top left; left: 12px; top: 2px; padding: 0 5px; background: #f5f7fa; }
        QToolButton { border: 1px solid rgba(100,116,139,0.35); color: rgba(71,85,105,0.72); background: rgba(241,245,249,0.75); border-radius: 9px; font-weight: 700; }
        QToolButton:hover { color: #2563eb; border-color: rgba(37,99,235,0.5); background: #eef4ff; }
        QToolButton#UpdateButton { color:#ffffff; background:#2563eb; border:none; border-radius:15px; font-size:18px; font-weight:700; }
        QToolButton#UpdateButton:hover { background:#1d4ed8; color:#ffffff; }
        QLineEdit, QComboBox, QSpinBox, QPlainTextEdit { background: #ffffff; border: 1px solid #d7dde5; border-radius: 7px; padding: 6px 8px; color: #111827; }
        QLineEdit:focus, QComboBox:focus, QSpinBox:focus, QPlainTextEdit:focus { border: 1px solid #7aa2f7; }
        QLineEdit:disabled, QComboBox:disabled, QSpinBox:disabled { background: #f3f4f6; color: #9ca3af; }
        QPushButton { min-height: 31px; padding: 0 13px; border-radius: 7px; border: 1px solid #d7dde5; background: #ffffff; color: #334155; }
        QPushButton:hover { background: #f8fafc; border-color: #b8c2cf; }
        QPushButton[role="primary"] { background: #2563eb; color: white; border: 1px solid #2563eb; font-weight: 600; }
        QPushButton[role="danger"] { background: #fff7f7; color: #b42318; border: 1px solid #f3c7c3; font-weight: 600; }
        QPushButton:disabled { background: #f3f4f6; color: #a3aab4; border-color: #e5e7eb; }
        QTableWidget { background: #ffffff; border: 1px solid #e5e7eb; border-radius: 7px; gridline-color: transparent; alternate-background-color: #f8fafc; selection-background-color: #e8f0fe; selection-color: #111827; }
        QHeaderView::section { background: #f8fafc; color: #475569; border: none; border-bottom: 1px solid #e5e7eb; padding: 7px 6px; font-weight: 600; }
        """
    )
    window = MainWindow()
    window.show()
    return app.exec()


if __name__ == "__main__":
    sys.exit(main())
