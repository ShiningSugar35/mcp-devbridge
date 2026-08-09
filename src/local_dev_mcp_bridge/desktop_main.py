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
import shutil
import sys
from collections.abc import Callable
from pathlib import Path
from typing import Any, cast

from PySide6.QtCore import QObject, Qt, QThreadPool, QTimer, Signal
from PySide6.QtGui import QFont, QTextOption
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
    QMainWindow,
    QMessageBox,
    QPlainTextEdit,
    QPushButton,
    QScrollArea,
    QSizePolicy,
    QSpinBox,
    QTableWidget,
    QTableWidgetItem,
    QTabWidget,
    QVBoxLayout,
    QWidget,
)

from . import APP_NAME, __version__, constants
from .app_state import ServiceCoordinator, StartOptions
from .audit import AuditQuery, available_tool_names, query_logs
from .backend_manager import current_access_token, port_in_use, regenerate_access_token
from .config_store import (
    load_app_config,
    load_projects,
    save_app_config,
    upsert_project,
)
from .engines import EngineState
from .models import PermissionMode, ProjectConfig, gateway_service_url, git_field_error
from .oauth_provider import get_or_create_gemini_client
from .project_manager import ProjectManager
from .secrets import SecretsStore, generate_token
from .selftest import SelftestResult, run_selftest
from .shell import get_shell_info
from .shell import run_program as _run_program
from .tunnel_manager import ConnectionMethod

# 权限模式（与命令执行档位合二为一）：
#   只读     = read_only  + safe        （只读安全操作）
#   默认     = workspace  + developer   （项目内开发工具白名单）
#   完全访问 = system     + full_system （任意命令，首次启动需风险确认）
PERMISSION_MODES = [
    ("read_only", "只读"),
    ("workspace", "默认（推荐）"),
    ("system", "完全访问（危险）"),
]
PERMISSION_PROFILE = {"read_only": "safe", "workspace": "developer", "system": "full_system"}
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
        self.pm = ProjectManager()
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
        QTimer.singleShot(2500, self._auto_restore_enabled_projects)

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
        root.setContentsMargins(12, 12, 12, 12)
        root.setSpacing(8)

        self.tabs = QTabWidget()
        root.addWidget(self.tabs)
        self.ctrl_tab = QWidget()
        self.ctrl_layout = QVBoxLayout(self.ctrl_tab)
        self.ctrl_layout.setContentsMargins(0, 4, 0, 4)
        self.ctrl_layout.setSpacing(8)
        self.tabs.addTab(self.ctrl_tab, "控制")

        # --- project list (多项目并行)
        proj_box = QGroupBox("项目列表（多项目并行；选中行 = 启动公网服务的项目）")
        proj_v = QVBoxLayout(proj_box)
        proj_v.setContentsMargins(12, 12, 12, 12)
        proj_v.setSpacing(8)
        self.project_table = QTableWidget(0, 6)
        self.project_table.setHorizontalHeaderLabels(["名称", "路径", "状态", "CodexPro端口", "启用", "入口"])
        self.project_table.horizontalHeader().setSectionResizeMode(1, QHeaderView.ResizeMode.Stretch)
        self.project_table.verticalHeader().setVisible(False)
        self.project_table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.project_table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.project_table.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
        self.project_table.itemSelectionChanged.connect(self._on_project_selected)
        proj_v.addWidget(self.project_table)

        proj_btns = QHBoxLayout()
        proj_btns.setSpacing(8)
        self.add_project_btn = QPushButton("添加项目…")
        self.add_project_btn.setFixedWidth(110)
        self.add_project_btn.clicked.connect(self._browse_project)
        self.remove_project_btn = QPushButton("删除项目")
        self.remove_project_btn.clicked.connect(self._remove_project)
        self.start_project_btn = QPushButton("启动项目（引擎）")
        self.start_project_btn.setToolTip("仅启动该项目 CodexPro 引擎（本机回环端口），不影响其他项目")
        self.start_project_btn.clicked.connect(self._start_project_engine)
        self.stop_project_btn = QPushButton("停止项目")
        self.stop_project_btn.clicked.connect(self._stop_project_engine)
        proj_btns.addWidget(self.add_project_btn)
        proj_btns.addWidget(self.remove_project_btn)
        proj_btns.addWidget(self.start_project_btn)
        proj_btns.addWidget(self.stop_project_btn)
        proj_btns.addStretch(1)
        proj_v.addLayout(proj_btns)
        proj_hint = QLabel(
            "「启动公网服务」使用选中项目；「启动项目（引擎）」可让多个项目引擎同时在 127.0.0.1 各自端口运行，"
            "彼此独立。表中勾选「启用」后，桌面启动会自动恢复该项目引擎。"
        )
        proj_hint.setWordWrap(True)
        proj_hint.setStyleSheet("color: gray;")
        proj_v.addWidget(proj_hint)
        self.ctrl_layout.addWidget(proj_box)

        # --- config: permission + connection + bridge
        cfg_box = QGroupBox("服务配置")
        cfg_form = QFormLayout(cfg_box)
        cfg_form.setContentsMargins(12, 12, 12, 12)
        cfg_form.setSpacing(8)
        self.permission_combo = QComboBox()
        for _value, label in PERMISSION_MODES:
            self.permission_combo.addItem(label)
        cfg_form.addRow("权限模式:", self.permission_combo)
        perm_hint = QLabel(
            "只读：安全只读操作。\n"
            "默认：项目内可写、可执行开发工具与安全命令（pytest/pyright/ruff/git 等）。\n"
            "完全访问：可读写项目外文件、执行任意命令，首次启动需风险确认。"
        )
        perm_hint.setWordWrap(True)
        perm_hint.setStyleSheet("color: gray;")
        cfg_form.addRow("", perm_hint)

        self.connection_combo = QComboBox()
        for method in CONNECTION_METHODS:
            self.connection_combo.addItem(method.label(), method.value)
        self.connection_combo.currentIndexChanged.connect(self._on_connection_changed)
        cfg_form.addRow("连接方式:", self.connection_combo)

        self.hostname_edit = QLineEdit()
        self.hostname_edit.setPlaceholderText("例如 bridge.example.com")
        cfg_form.addRow("公网域名:", self.hostname_edit)

        self.cf_token_edit = QLineEdit()
        self.cf_token_edit.setEchoMode(QLineEdit.EchoMode.Password)
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
        gemini_form.setContentsMargins(12, 12, 12, 12)
        gemini_form.setSpacing(8)
        self._gemini_store = SecretsStore()
        self._gemini_secret = ""
        self.gemini_uri_edit = QLineEdit()
        self.gemini_uri_edit.setPlaceholderText("从 Gemini「Custom Connected App → Advanced Settings → Copy redirect URI」粘贴到此处")
        self.gemini_uri_edit.setText(self._gemini_store.get(GEMINI_LAST_URI_CRED_NAME) or "")
        gemini_form.addRow("Gemini Redirect URI:", self.gemini_uri_edit)

        self.gemini_gen_btn = QPushButton("生成Gemini凭证")
        self.gemini_gen_btn.clicked.connect(self._generate_gemini_credentials)
        gemini_form.addRow("", self.gemini_gen_btn)

        self.gemini_id_edit = QLineEdit("—")
        self.gemini_id_edit.setReadOnly(True)
        self.gemini_id_edit.setToolTip("Client ID：只读，可选中复制")
        self.gemini_id_copy = QPushButton("复制")
        id_row = QHBoxLayout()
        id_row.setSpacing(8)
        id_row.addWidget(self.gemini_id_edit, 1)
        id_row.addWidget(self.gemini_id_copy)
        gemini_form.addRow("Client ID:", id_row)

        self.gemini_secret_edit = QLineEdit("—")
        self.gemini_secret_edit.setReadOnly(True)
        self.gemini_secret_edit.setToolTip("Client Secret：掩码显示，仅可复制")
        self.gemini_secret_copy = QPushButton("复制")
        secret_row = QHBoxLayout()
        secret_row.setSpacing(8)
        secret_row.addWidget(self.gemini_secret_edit, 1)
        secret_row.addWidget(self.gemini_secret_copy)
        gemini_form.addRow("Client Secret:", secret_row)

        gemini_hint = QLabel(
            "每次点击「生成Gemini凭证」都会更新 client_secret（旧值立即失效）；client_id 复用不变。"
            "Secret 加密保存、掩码显示，仅「复制」进剪贴板。"
        )
        gemini_hint.setWordWrap(True)
        gemini_hint.setStyleSheet("color: gray;")
        gemini_form.addRow("", gemini_hint)

        self.gemini_id_copy.clicked.connect(lambda: self._copy_text(self.gemini_id_edit.text()))
        self.gemini_secret_copy.clicked.connect(lambda: self._copy_text(self._gemini_secret))
        cfg_form.addRow("", gemini_box)

        if self.gemini_uri_edit.text():
            try:
                client_id, secret = get_or_create_gemini_client(self.gemini_uri_edit.text(), rotate_secret=False)
                self.gemini_id_edit.setText(client_id)
                self._gemini_secret = secret
                self.gemini_secret_edit.setText("•" * 16)
            except ValueError:
                pass

        self.bridge_check = QCheckBox("启用 Windows 控制桥接（uvx 子进程，令牌自动生成并加密保存）")
        cfg_form.addRow("", self.bridge_check)
        self.ctrl_layout.addWidget(cfg_box)

        # --- git settings (Phase 5)
        git_box = QGroupBox("Git 参数（可空）")
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
        self.git_save_btn = QPushButton("保存 Git 设置")
        self.git_save_btn.clicked.connect(self._save_git_settings)
        git_form.addRow("", self.git_save_btn)
        self.ctrl_layout.addWidget(git_box)

        # --- service control
        ctrl_box = QGroupBox("服务控制")
        ctrl_row = QHBoxLayout(ctrl_box)
        ctrl_row.setContentsMargins(12, 8, 12, 8)
        ctrl_row.setSpacing(8)
        self.start_btn = QPushButton("启动服务")
        self.stop_btn = QPushButton("停止服务")
        self.restart_btn = QPushButton("重启服务")
        self.advanced_btn = QPushButton("高级设置…")
        self.start_btn.clicked.connect(self._start_service)
        self.stop_btn.clicked.connect(self._stop_service)
        self.restart_btn.clicked.connect(self._restart_service)
        self.advanced_btn.clicked.connect(self._open_advanced_settings)
        for btn in (self.start_btn, self.stop_btn, self.restart_btn, self.advanced_btn):
            btn.setSizePolicy(QSizePolicy.Policy.Minimum, QSizePolicy.Policy.Fixed)
            btn.setMinimumHeight(28)
        ctrl_row.addWidget(self.start_btn)
        ctrl_row.addWidget(self.stop_btn)
        ctrl_row.addWidget(self.restart_btn)
        ctrl_row.addStretch(1)
        ctrl_row.addWidget(self.advanced_btn)
        self.ctrl_layout.addWidget(ctrl_box)

        # --- status
        self.status_label = QLabel("状态：未启动")
        self.status_label.setWordWrap(True)
        self.ctrl_layout.addWidget(self.status_label)

        # --- token / URL
        tok_box = QGroupBox("访问令牌与 MCP 地址（Cloudflare 公网入口）")
        tok_layout = QVBoxLayout(tok_box)
        tok_layout.setContentsMargins(12, 12, 12, 12)
        tok_layout.setSpacing(8)
        self.token_edit = QLineEdit("令牌：未生成（点击“重新生成令牌”）")
        self.token_edit.setReadOnly(True)
        self.url_edit = QLineEdit("MCP 地址：http://127.0.0.1:8765/mcp（仅本机）")
        self.url_edit.setReadOnly(True)
        tok_row = QHBoxLayout()
        tok_row.setSpacing(8)
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
        tok_layout.addWidget(self.token_edit)
        tok_layout.addWidget(self.url_edit)
        tok_layout.addLayout(tok_row)

        # --- gateway port (Cloudflare 公网入口端口)
        port_row = QHBoxLayout()
        port_row.setSpacing(8)
        port_row.addWidget(QLabel("公网入口端口（Gateway）:"))
        self.gateway_port_spin = QSpinBox()
        self.gateway_port_spin.setRange(1, 65535)
        self.gateway_port_spin.setValue(self._app_config.gateway_port)
        self.gateway_port_spin.setFixedWidth(90)
        self.gateway_port_spin.valueChanged.connect(self._on_gateway_port_changed)
        port_row.addWidget(self.gateway_port_spin)
        port_check_btn = QPushButton("检测端口")
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
        self.service_url_copy_btn = QPushButton("复制 Service URL")
        self.service_url_copy_btn.clicked.connect(lambda: self._copy_text(self._service_url_text()))
        service_row.addWidget(self.service_url_copy_btn)
        tok_layout.addLayout(service_row)

        self.port_warn_label = QLabel("")
        self.port_warn_label.setWordWrap(True)
        self.port_warn_label.setStyleSheet("color: #c62828; font-weight: bold;")
        self.port_warn_label.setVisible(False)
        tok_layout.addWidget(self.port_warn_label)

        self.ctrl_layout.addWidget(tok_box)

        # --- self test
        test_group = QGroupBox("连接自测")
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
        self.ctrl_layout.addWidget(test_group)

        # --- 最近消息（控制页）
        msg_group = QGroupBox("最近消息")
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
        self.tabs.currentChanged.connect(self._on_tab_changed)

    def _build_process_log_tab(self) -> None:
        proc_tab = QWidget()
        proc_v = QVBoxLayout(proc_tab)
        proc_v.setContentsMargins(0, 4, 0, 4)
        proc_v.setSpacing(8)
        row = QHBoxLayout()
        row.setSpacing(8)
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
        row.addWidget(QLabel("工具:"))
        row.addWidget(self.audit_tool_combo, 1)
        row.addWidget(QLabel("结果:"))
        row.addWidget(self.audit_success_combo)
        row.addWidget(self.audit_refresh_btn)
        audit_v.addLayout(row)
        self.audit_view = QTableWidget(0, 6)
        self.audit_view.setHorizontalHeaderLabels(["时间", "工具", "结果", "耗时 ms", "客户端", "参数摘要"])
        self.audit_view.horizontalHeader().setSectionResizeMode(5, QHeaderView.ResizeMode.Stretch)
        self.audit_view.verticalHeader().setVisible(False)
        self.audit_view.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
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
        table = self.project_table
        table.setRowCount(0)
        views = self.pm.views()
        active = self._app_config.active_workspace
        for row, view in enumerate(views):
            table.insertRow(row)
            table.setItem(row, 0, QTableWidgetItem(view.name))
            table.setItem(row, 1, QTableWidgetItem(view.root_path))
            table.setItem(row, 2, QTableWidgetItem(view.state))
            table.setItem(row, 3, QTableWidgetItem(str(view.codexpro_port)))
            table.setItem(row, 4, QTableWidgetItem(""))
            table.setItem(row, 5, QTableWidgetItem("★" if active and _same_root(view.root_path, active) else ""))
            enable_box = QCheckBox()
            enable_box.setChecked(view.enabled)
            enable_box.setToolTip("启用后，桌面启动时自动恢复该项目引擎")
            enable_box.stateChanged.connect(lambda _state, root=view.root_path: self._toggle_project_enabled(root))
            table.setCellWidget(row, 4, enable_box)
        table.horizontalHeader().setSectionResizeMode(0, QHeaderView.ResizeMode.ResizeToContents)
        table.horizontalHeader().setSectionResizeMode(2, QHeaderView.ResizeMode.ResizeToContents)
        table.horizontalHeader().setSectionResizeMode(3, QHeaderView.ResizeMode.ResizeToContents)
        table.horizontalHeader().setSectionResizeMode(4, QHeaderView.ResizeMode.ResizeToContents)
        table.horizontalHeader().setSectionResizeMode(5, QHeaderView.ResizeMode.ResizeToContents)
        if views:
            table.selectRow(max(self._row_of_root(self._app_config.active_workspace), 0))

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
        if not _same_root(root, self._app_config.active_workspace or ""):
            self._app_config.active_workspace = root
            save_app_config(self._app_config)
        project = next((p for p in load_projects() if p.root_path == root), None)
        if project is None:
            return
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
        self.bridge_check.setChecked(project.windows_enabled)
        self._sync_connection_fields()

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
        return "workspace"

    def _selected_execution_profile(self) -> str:
        """命令执行档位跟随权限模式（合一设计：只读→safe / 默认→developer / 完全访问→full_system）。"""
        return PERMISSION_PROFILE.get(self._selected_permission_mode(), "developer")

    def _run_env_check(self) -> None:
        """Detect the default shell and probe the toolchain; no server needed."""
        self.test_output.setText("正在检测开发环境…")
        self.test_btn.setEnabled(False)
        self.env_btn.setEnabled(False)

        def work() -> list[str]:
            shell_info = get_shell_info()
            default = cast(dict[str, Any], shell_info.get("default") or {})
            detected = [str(s.get("name", "")) for s in cast(list[dict[str, Any]], shell_info.get("detected") or [])]
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
                        exe, [arg], cwd=Path(self._selected_root() or os.getcwd()),
                        timeout_seconds=30,
                    )
                    text = (res.stdout or res.stderr or "").strip().splitlines()
                    lines.append(f"{name}: {text[0] if text else '(无输出)'}")
                except Exception as exc:  # noqa: BLE001
                    lines.append(f"{name}: 失败（{exc}）")
            return lines

        def done(lines: list[str]) -> None:
            self.test_btn.setEnabled(True)
            self.env_btn.setEnabled(True)
            ok = all(
                not line.endswith(("未安装（PATH 中找不到）", "不可执行！")) and "失败（" not in line
                for line in lines[2:]
            )
            head = "开发环境检测：工具链就绪，可运行 测试/检查 命令。" if ok else "开发环境检测：存在缺失或异常项。"
            self.test_output.setText("\n".join([head, *lines]))

        _run_async(work, done)

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
        self.gemini_id_edit.setText(client_id)
        self._gemini_secret = secret
        self.gemini_secret_edit.setText("•" * 16)

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
    def _toggle_project_enabled(self, root: str) -> None:
        """Persist the 「启用」checkbox for auto-restore of a project engine."""
        project = self.pm.by_root(root)
        if project is None:
            return
        checkbox = self.sender()
        checked = bool(checkbox.isChecked()) if isinstance(checkbox, QCheckBox) else False
        project.enabled = checked
        self.pm.update(project)
        self._append_log(f"{'已启用' if checked else '已停用'}自动恢复：{project.display_name or root}")

    def _start_project_engine(self) -> None:
        """Start ONLY this project's CodexPro engine on its own loopback port."""
        project = self._project_config()
        if project is None:
            QMessageBox.warning(self, "未选择项目", "请先选择或添加项目，再启动该项目引擎。")
            return
        if not self._current_token:
            QMessageBox.warning(self, "缺少令牌", "请先点击“重新生成令牌”创建访问令牌。")
            return
        self._append_log(f"正在启动项目引擎（{project.display_name}）…")

        def run() -> str:
            view = self.pm.start(project.id, codex_token=self._current_token, windows_token=self._bridge_token)
            return f"项目引擎已启动：{project.display_name} @127.0.0.1:{view.codexpro_port}（{view.state}）"

        def done(result: Any) -> None:
            if isinstance(result, Exception):
                self._append_log(f"启动项目引擎失败:{result}")
            else:
                self._append_log(str(result))
            self._refresh_project_list()
            self._poll_status()

        _run_async(run, done)

    def _stop_project_engine(self) -> None:
        project = self._project_config()
        if project is None:
            QMessageBox.warning(self, "未选择项目", "请先选择或添加项目。")
            return
        self._append_log(f"正在停止项目引擎（{project.display_name}）…")

        def run() -> str:
            self.pm.stop(project.id)
            return f"项目引擎已停止：{project.display_name}"

        def done(result: Any) -> None:
            if isinstance(result, Exception):
                self._append_log(f"停止项目引擎出错:{result}")
            else:
                self._append_log(str(result))
            self._refresh_project_list()
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
            if _same_root(project.root_path, self._app_config.active_workspace or ""):
                self._app_config.active_workspace = ""
                save_app_config(self._app_config)
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

    def _auto_restore_enabled_projects(self) -> None:
        """桌面启动后自动恢复「启用」勾选的项目引擎（后台，不阻塞 UI）。"""
        if not self._current_token:
            self._append_log("尚未生成访问令牌，跳过项目引擎自动恢复。")
            return
        enabled = [p for p in self.pm.list() if p.enabled]
        if not enabled:
            return
        self._append_log(f"正在自动恢复已启用项目的引擎（{len(enabled)} 个）…")

        def run() -> str:
            views = self.pm.start_enabled(codex_token=self._current_token, windows_token=self._bridge_token)
            if not views:
                return "自动恢复：没有可启动的项目引擎。"
            return "自动恢复完成：" + "；".join(
                f"{v.name} @127.0.0.1:{v.codexpro_port}" for v in views
            )

        def done(result: Any) -> None:
            if isinstance(result, Exception):
                self._append_log(f"自动恢复失败:{result}")
            else:
                self._append_log(str(result))
            self._refresh_project_list()
            self._poll_status()

        _run_async(run, done)

    def _bind_coord_engines(self) -> None:
        """让 ServiceCoordinator 复用选中项目的引擎实例，避免重复 spawn。

        多项目场景下，Coordinator 自身构造的默认管理器仅用于无项目占位；
        一旦以某项目启动公网服务，就把它替换为该项目 unit 的引擎管理器，
        使引擎只存在一份，端口一致，停止/重启都作用于同一个进程。
        """
        project_id = self._selected_project_id()
        if not project_id:
            return
        unit = self.pm.unit_for(project_id)
        if unit is not None:
            self.coord.codex = unit.codex
            self.coord.windows = unit.windows

    # -------------------------------------------------- service control
    def _current_options(self) -> StartOptions:
        project = self._project_config()
        codexpro_port = (
            (project.codexpro_port or constants.DEFAULT_CODEXPRO_PORT)
            if project is not None
            else self._app_config.codexpro_port
        )
        windows_mcp_port = (
            (project.windows_bridge_port or constants.DEFAULT_WINDOWS_MCP_PORT)
            if project is not None
            else self._app_config.windows_mcp_port
        )
        return StartOptions(
            project_root=self._selected_root(),
            permission_mode=self._selected_permission_mode(),
            execution_profile=self._selected_execution_profile(),
            full_system_confirmed=self._app_config.full_system_risk_accepted,
            codex_token=self._current_token,
            windows_enabled=self.bridge_check.isChecked(),
            windows_token=self._bridge_token,
            connection=self._selected_connection(),
            public_hostname=self.hostname_edit.text().strip(),
            tunnel_token=self._tunnel_token_value(),
            gateway_port=self._app_config.gateway_port,
            codexpro_port=codexpro_port,
            windows_mcp_port=windows_mcp_port,
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
        if self._selected_permission_mode() == "system" and (
            not self._app_config.first_system_risk_accepted
            or not self._app_config.full_system_risk_accepted
        ):
            answer = QMessageBox.question(
                self,
                "完全访问模式风险确认",
                "“完全访问”模式下 AI 可读写项目目录之外的文件、执行任意命令（含系统级命令）等高风险操作。\n"
                "请确认您理解风险后继续（仅首次确认，之后不再提示）。",
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            )
            if answer == QMessageBox.StandardButton.No:
                return True
            self._app_config.first_system_risk_accepted = True
            self._app_config.full_system_risk_accepted = True
            save_app_config(self._app_config)
        if self._selected_connection() == ConnectionMethod.QUICK:
            answer = QMessageBox.question(
                self,
                "Quick Tunnel 临时测试",
                "Quick Tunnel 的公开地址每次启动都会变化，仅适合临时调试。\n"
                "正式使用请选择 Cloudflare 固定地址或 ngrok 固定地址。\n\n是否继续？",
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            )
            if answer == QMessageBox.StandardButton.No:
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
        project.windows_enabled = self.bridge_check.isChecked()
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
        self._bind_coord_engines()
        options = self._current_options()
        conflict = self._ports_conflict(options)
        if conflict:
            QMessageBox.warning(self, "端口被占用", conflict)
            return
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
        self._bind_coord_engines()
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
        self._bind_coord_engines()
        options = self._current_options()
        conflict = self._ports_conflict(options)
        if conflict:
            QMessageBox.warning(self, "端口被占用", conflict)
            return
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

    # ---------------------------------------------------- port settings
    def _service_url_text(self) -> str:
        return f"Cloudflare Service URL: {gateway_service_url(self.gateway_port_spin.value())}"

    def _on_gateway_port_changed(self, _value: int) -> None:
        self._app_config.gateway_port = self.gateway_port_spin.value()
        save_app_config(self._app_config)
        self._update_gateway_port_ui()

    def _update_gateway_port_ui(self) -> None:
        self.service_url_edit.setText(self._service_url_text())
        port = self.gateway_port_spin.value()
        if port != constants.DEFAULT_GATEWAY_PORT:
            self.port_warn_label.setText(
                f"端口修改后，请同步将 Cloudflare Tunnel 的 Service URL 修改为 "
                f"{gateway_service_url(port)}，否则公网连接会失败。"
            )
            self.port_warn_label.setVisible(True)
        else:
            self.port_warn_label.setVisible(False)

    def _check_gateway_port(self) -> None:
        port = self.gateway_port_spin.value()
        if port_in_use(port):
            QMessageBox.warning(
                self,
                "端口被占用",
                f"端口 {port} 已被占用，启动开放公网连接会失败。\n"
                "请关闭占用该端口的程序，或改用其他端口。",
            )
        else:
            QMessageBox.information(self, "端口检测", f"端口 {port} 当前空闲，可以正常使用。")

    def _restore_default_gateway_port(self) -> None:
        self.gateway_port_spin.setValue(constants.DEFAULT_GATEWAY_PORT)
        QMessageBox.information(
            self,
            "已恢复默认",
            f"公网入口端口已恢复为默认值 {constants.DEFAULT_GATEWAY_PORT}。",
        )

    def _open_advanced_settings(self) -> None:
        dialog = QDialog(self)
        dialog.setWindowTitle("高级设置 · 内部组件端口")
        form = QFormLayout(dialog)
        hint = QLabel(
            "内部组件端口仅监听 127.0.0.1，默认无需修改。\n"
            "修改后永久保存；正在运行时无法修改，请先停止服务。"
        )
        hint.setWordWrap(True)
        hint.setStyleSheet("color: gray;")
        form.addRow(hint)

        gateway_spin = QSpinBox()
        gateway_spin.setRange(1, 65535)
        gateway_spin.setValue(self._app_config.gateway_port)
        form.addRow("Gateway（公网入口，CF Service URL 端口）:", gateway_spin)
        codex_spin = QSpinBox()
        codex_spin.setRange(1, 65535)
        codex_spin.setValue(self._app_config.codexpro_port)
        form.addRow("CodexPro 引擎:", codex_spin)
        windows_spin = QSpinBox()
        windows_spin.setRange(1, 65535)
        windows_spin.setValue(self._app_config.windows_mcp_port)
        form.addRow("Windows-MCP 桥接:", windows_spin)
        legacy_spin = QSpinBox()
        legacy_spin.setRange(1, 65535)
        legacy_spin.setValue(self._app_config.legacy_backend_port)
        form.addRow("Legacy backend:", legacy_spin)

        buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel
        )
        buttons.accepted.connect(dialog.accept)
        buttons.rejected.connect(dialog.reject)
        form.addRow(buttons)
        if dialog.exec() != QDialog.DialogCode.Accepted:
            return
        self._app_config.gateway_port = gateway_spin.value()
        self._app_config.codexpro_port = codex_spin.value()
        self._app_config.windows_mcp_port = windows_spin.value()
        self._app_config.legacy_backend_port = legacy_spin.value()
        save_app_config(self._app_config)
        self.gateway_port_spin.setValue(self._app_config.gateway_port)
        self._append_log("内部组件端口已保存，重启服务后生效。")

    def _ports_conflict(self, options: StartOptions) -> str | None:
        if not self.coord.codex.is_running and port_in_use(options.codexpro_port):
            return f"CodexPro 引擎端口 {options.codexpro_port} 已被占用。请先停止占用该端口的程序。"
        if (
            options.windows_enabled
            and not self.coord.windows.is_running
            and port_in_use(options.windows_mcp_port)
        ):
            return f"Windows 控制桥端口 {options.windows_mcp_port} 已被占用。请先停止占用该端口的程序。"
        if options.connection != ConnectionMethod.LOCAL and port_in_use(options.gateway_port):
            return f"Gateway 端口 {options.gateway_port} 已被占用，公网连接无法建立。请先停止占用该端口的程序。"
        return None

    # -------------------------------------------------- coordinator events
    def _emit_coord_event(self, state: EngineState, message: str | None) -> None:
        self._signals.coord_event.emit(state, message)

    def _on_coord_event(self, state: EngineState, message: str | None) -> None:
        if message:
            self._append_log(f"[{state.value}] {message}")

    # ------------------------------------------------------- status / URL
    def _local_url(self) -> str:
        return f"http://127.0.0.1:{self._app_config.codexpro_port}/mcp"

    def _display_url(self) -> str:
        return self.coord.public_url or self._local_url()

    def _refresh_url_ui(self) -> None:
        url = self.coord.public_url
        if url:
            suffix = "（固定地址，重启不变）" if not self.coord.url_mutable else "（临时地址，重启会变）"
        else:
            url = self._local_url()
            suffix = "（仅本机）"
        self.url_edit.setText(f"MCP 地址：{url} {suffix}")

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
        # 服务运行期间锁定端口输入（修改需先停止服务）
        editable = not running
        self.gateway_port_spin.setEnabled(editable)
        self.advanced_btn.setEnabled(editable)
        self._refresh_url_ui()

    # ------------------------------------------------------ token helpers
    def _sync_token_ui(self) -> None:
        if self._current_token:
            self.token_edit.setText(f"令牌（Bearer）：{self._current_token}")
        else:
            self.token_edit.setText("令牌：未生成（点击“重新生成令牌”）")

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
        now = datetime.datetime.now().strftime("%H:%M:%S")
        self.log_text.appendPlainText(f"[{now}] {text}")
        scrollbar = self.log_text.verticalScrollBar()
        scrollbar.setValue(scrollbar.maximum())

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
        self.pm.stop_all()
        super().closeEvent(event)


def main() -> int:
    os.environ.setdefault("PYTHONIOENCODING", "utf-8")
    app = QApplication(sys.argv)
    font = QFont(app.font())
    font.setPointSize(10)
    app.setFont(font)
    app.setStyleSheet(
        """
        QGroupBox { font-size: 14px; font-weight: 600; }
        QLabel { font-size: 12px; }
        QLineEdit, QComboBox, QSpinBox, QPushButton, QPlainTextEdit, QCheckBox, QTableWidget { font-size: 12px; }
        """
    )
    window = MainWindow()
    window.show()
    return app.exec()


if __name__ == "__main__":
    sys.exit(main())