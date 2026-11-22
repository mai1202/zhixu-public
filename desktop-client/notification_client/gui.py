"""普通用户的配对、常驻、自启与解除配对界面；所有群选择留在网页。"""

import argparse
import ctypes
import json
import os
from pathlib import Path
import queue
import shutil
import socket
import threading
import time
import zipfile
import tkinter as tk
from tkinter import font as tkfont, messagebox, ttk

from .api_client import ClientApiError, validate_server_url
from .autostart import autostart_enabled, set_autostart
from .client_app import ClientApplication, SingleInstanceLock, default_data_dir
from .wechat_cli import WechatCliError
from . import __version__, updater

# 固定选项按产品需要的顺序展示；自定义只保存用户输入的 HTTPS 地址。
OFFICIAL_SERVER_URL = "https://261117.xyz"
LOCAL_SERVER_URL = "http://127.0.0.1:8080"
SERVICE_CHOICES = (
    ("官方服务 · 261117.xyz", OFFICIAL_SERVER_URL),
    ("本地部署 · 127.0.0.1:8080", LOCAL_SERVER_URL),
    ("自定义地址", ""),
)
CUSTOM_SERVICE_LABEL = SERVICE_CHOICES[-1][0]

# 只使用 Tk 标准组件和 Segoe UI/微软雅黑，避免 Win10 专属界面 API。
BACKGROUND = "#eef4fb"
SURFACE = "#ffffff"
TEXT = "#14263d"
MUTED = "#66788d"
BORDER = "#d8e4f0"
ACCENT = "#2563eb"
ACCENT_ACTIVE = "#1d4ed8"
SUCCESS = "#16845b"
SUCCESS_TINT = "#e9f8f1"
DANGER = "#c2414b"
DANGER_TINT = "#fff1f2"


def enable_high_dpi() -> None:
    """在创建 Tk 前声明 DPI 感知；Win7 回退到 Vista+ 的系统 DPI API。"""
    if os.name != "nt":
        return
    try:
        # 1 为 system DPI aware，Tk 8.6 在跨屏移动时比逐屏缩放更稳定。
        ctypes.windll.shcore.SetProcessDpiAwareness(1)
        return
    except (AttributeError, OSError):
        pass
    try:
        ctypes.windll.user32.SetProcessDPIAware()
    except (AttributeError, OSError):
        pass


def server_choice_for_url(url: str) -> str:
    """把配置中的地址映射到固定的服务选项，其他地址自动进入自定义。"""
    normalized = (url or "").strip().rstrip("/")
    for label, value in SERVICE_CHOICES[:-1]:
        if value and normalized == value:
            return label
    return CUSTOM_SERVICE_LABEL


def server_url_for_choice(label: str) -> str:
    """返回固定选项地址；自定义项保留独立输入框，不在此处猜测。"""
    for choice_label, value in SERVICE_CHOICES:
        if choice_label == label:
            return value
    return ""


class CollectorWindow:
    """网络与采集在单个工作线程串行执行，Tk 控件仅在主线程更新。"""

    def __init__(self, root: tk.Tk, app: ClientApplication, *, background: bool = False):
        self.root, self.app = root, app
        self.results = queue.Queue()
        self.busy = False
        self.update_info = None
        self.update_events = queue.Queue()
        self.unpair_requested = False
        self.next_catalog = self.next_collection = 0.0
        self.selected = None
        self.autostart_ready = autostart_enabled(app.data_dir)
        self.stopping = False
        config = app.config() if app.config_path.is_file() else {}
        deployment = Path(__file__).resolve().parents[2] / "collector-server.json"
        deployed_server = ""
        if deployment.is_file():
            try:
                deployed_server = str(json.loads(deployment.read_text(encoding="utf-8")).get("server_url") or "")
            except (OSError, ValueError, TypeError):
                deployed_server = ""
        initial_server = str(config.get("server_url") or deployed_server or OFFICIAL_SERVER_URL).rstrip("/")
        self.server = tk.StringVar(value=initial_server)
        self.server_choice = tk.StringVar(value=server_choice_for_url(initial_server))
        self.code = tk.StringVar()
        self.status = tk.StringVar(value="等待输入配对码。")
        self.autostart_var = tk.BooleanVar(master=root, value=self.autostart_ready)
        self.autostart_state = tk.StringVar(
            value="已开启" if self.autostart_ready else "已关闭"
        )
        self._view_mode = ""
        self.ui_scale = self._detect_ui_scale()
        self.window_width = min(self._px(760), self.root.winfo_screenwidth() - self._px(48))
        self.pairing_height = min(self._px(710), self.root.winfo_screenheight() - self._px(60))
        self.connected_height = min(self._px(670), self.root.winfo_screenheight() - self._px(60))
        self.font_family = self._ui_font_family()
        root.title("知序 · 微信采集器")
        root.configure(bg=BACKGROUND)
        root.minsize(min(self._px(680), self.window_width), min(self._px(480), self.connected_height))
        self._center_window(self.window_width, self.pairing_height)
        self._configure_styles()
        self._build_layout()
        self._refresh_connection_mode(reset_status=True)

        def close():
            # 已解绑或尚未配对时正常退出；已配对时保留后台工作。
            current = app.config() if app.config_path.is_file() else {}
            if current.get("unpaired") or not app.credentials_path.exists():
                root.destroy()
            else:
                root.iconify()
        root.protocol("WM_DELETE_WINDOW", close)
        if background:
            root.iconify()
        root.after(200, self.poll)

    def _ui_font_family(self) -> str:
        """优先使用 Windows 7 已有的中文字体，避免依赖新系统字体。"""
        available = set(tkfont.families(self.root))
        for candidate in ("Microsoft YaHei UI", "Microsoft YaHei", "Segoe UI", "Arial"):
            if candidate in available:
                return candidate
        return "TkDefaultFont"

    def _detect_ui_scale(self) -> float:
        """按系统 DPI 调整 Tk 字体和像素尺寸，100% 时保持原设计比例。"""
        try:
            dpi = float(self.root.winfo_fpixels("1i"))
        except (tk.TclError, ValueError):
            dpi = 96.0
        try:
            self.root.tk.call("tk", "scaling", max(1.0, dpi / 72.0))
        except tk.TclError:
            pass
        return max(1.0, dpi / 96.0)

    def _px(self, value: int) -> int:
        """把 96 DPI 设计稿像素换算成当前屏幕像素，最小保留 1px。"""
        return max(1, round(value * self.ui_scale))

    def _center_window(self, width: int, height: int) -> None:
        """在主屏幕中央打开窗口，离屏显示器或小屏也能保留边距。"""
        screen_width = self.root.winfo_screenwidth()
        screen_height = self.root.winfo_screenheight()
        x = max(0, (screen_width - width) // 2)
        y = max(0, (screen_height - height) // 2)
        self.root.geometry(f"{width}x{height}+{x}+{y}")

    def _configure_styles(self) -> None:
        """使用 Tk 自带的 clam 主题绘制扁平按钮，不引入第三方界面库。"""
        style = ttk.Style(self.root)
        try:
            style.theme_use("clam")
        except tk.TclError:
            pass
        style.configure(
            "Accent.TButton",
            background=ACCENT,
            foreground="#ffffff",
            borderwidth=0,
            focusthickness=0,
            padding=(self._px(18), self._px(13)),
            font=(self.font_family, 11, "bold"),
        )
        style.map(
            "Accent.TButton",
            background=[("active", ACCENT_ACTIVE), ("disabled", "#a9bdd9")],
            foreground=[("disabled", "#f3f7fc")],
        )
        style.configure(
            "Danger.TButton",
            background=DANGER_TINT,
            foreground=DANGER,
            borderwidth=1,
            focusthickness=0,
            padding=(self._px(16), self._px(11)),
            font=(self.font_family, 10, "bold"),
        )
        style.map("Danger.TButton", background=[("active", "#ffe3e6")])
        style.configure(
            "Service.TCombobox",
            padding=(self._px(10), self._px(7)),
            arrowsize=16,
            font=(self.font_family, 10),
        )

    def _make_card(self) -> tk.Frame:
        """统一白色卡片、细边框和留白，保持 Win7 可用的普通 Frame。"""
        return tk.Frame(
            self.content,
            bg=SURFACE,
            highlightbackground=BORDER,
            highlightthickness=1,
            bd=0,
        )

    def _build_layout(self) -> None:
        """搭建头部、配对/已连接卡片以及固定在底部的服务选择。"""
        tk.Frame(self.root, bg=ACCENT, height=self._px(4)).pack(fill="x")
        shell = tk.Frame(self.root, bg=BACKGROUND)
        shell.pack(fill="both", expand=True, padx=self._px(36), pady=(self._px(26), self._px(24)))

        hero = tk.Frame(shell, bg=BACKGROUND)
        hero.pack(fill="x")
        mark = tk.Canvas(
            hero,
            width=self._px(58),
            height=self._px(58),
            bg=BACKGROUND,
            highlightthickness=0,
        )
        mark.create_oval(
            self._px(3), self._px(3), self._px(55), self._px(55),
            fill=ACCENT,
            outline="",
        )
        mark.create_text(
            self._px(29), self._px(29),
            text="知",
            fill="#ffffff",
            font=(self.font_family, 19, "bold"),
        )
        mark.pack(side="left")
        hero_copy = tk.Frame(hero, bg=BACKGROUND)
        hero_copy.pack(side="left", padx=(self._px(14), 0), fill="x", expand=True)
        tk.Label(
            hero_copy,
            text="知序采集器",
            bg=BACKGROUND,
            fg=TEXT,
            font=(self.font_family, 20, "bold"),
            anchor="w",
        ).pack(fill="x")
        tk.Label(
            hero_copy,
            text="安全连接你的校园通知助手",
            bg=BACKGROUND,
            fg=MUTED,
            font=(self.font_family, 10),
            anchor="w",
        ).pack(fill="x", pady=(self._px(3), 0))

        # 服务选择先占住底部，中间内容随后填满剩余空间。
        self.service_panel = tk.Frame(shell, bg=BACKGROUND)
        self.service_panel.pack(side="bottom", fill="x", pady=(self._px(18), 0))
        # 更新入口在配对前后都可见，更新状态与采集状态分开呈现。
        update_panel = tk.Frame(shell, bg=BACKGROUND)
        update_panel.pack(side='bottom', fill='x', pady=(self._px(12), 0))
        update_row = tk.Frame(update_panel, bg=BACKGROUND)
        update_row.pack(fill='x')
        tk.Label(update_row, text=f'采集器 v{__version__}', bg=BACKGROUND, fg=MUTED,
            font=(self.font_family, 9)).pack(side='left')
        self.install_update_button = ttk.Button(update_row, text='一键更新', command=self.install_update)
        self.install_update_button.pack(side='right')
        self.install_update_button.state(['disabled'])
        self.check_update_button = ttk.Button(update_row, text='检查更新', command=self.check_update)
        self.check_update_button.pack(side='right', padx=self._px(8))
        self.update_status = tk.StringVar(value='检查新版本，更新会保留配对及待上传数据。')
        tk.Label(update_panel, textvariable=self.update_status, bg=BACKGROUND, fg=MUTED,
            font=(self.font_family, 9), anchor='w', justify='left', wraplength=self._px(620)).pack(fill='x', pady=(self._px(6), 0))
        self.content = tk.Frame(shell, bg=BACKGROUND)
        self.content.pack(fill="both", expand=True, pady=(self._px(22), 0))

        self._build_pairing_card()
        self._build_connected_card()
        self._build_service_panel()

    def _build_pairing_card(self) -> None:
        """未连接时只保留配对码输入框和连接按钮。"""
        self.pairing_card = self._make_card()
        inner = tk.Frame(self.pairing_card, bg=SURFACE)
        inner.pack(fill="both", expand=True, padx=self._px(32), pady=self._px(28))
        tk.Label(
            inner,
            text="连接你的通知助手",
            bg=SURFACE,
            fg=TEXT,
            font=(self.font_family, 18, "bold"),
            anchor="w",
        ).pack(fill="x")
        tk.Label(
            inner,
            text="在网页「设置 → 采集设备」生成 10 位一次性配对码。",
            bg=SURFACE,
            fg=MUTED,
            font=(self.font_family, 10),
            anchor="w",
        ).pack(fill="x", pady=(self._px(8), self._px(20)))
        tk.Label(
            inner,
            text="一次性配对码",
            bg=SURFACE,
            fg=MUTED,
            font=(self.font_family, 10, "bold"),
            anchor="w",
        ).pack(fill="x", pady=(0, self._px(7)))
        self.code_entry = ttk.Entry(
            inner,
            textvariable=self.code,
            justify="center",
            font=(self.font_family, 19, "bold"),
        )
        self.code_entry.pack(fill="x", ipady=self._px(11))
        self.code_entry.bind("<Return>", lambda _event: self.pair())
        self.code.trace_add("write", self._on_code_changed)

        self.pair_button = ttk.Button(
            inner,
            text="连接并启用自动同步",
            style="Accent.TButton",
            command=self.pair,
        )
        self.pair_button.pack(fill="x", pady=(self._px(14), self._px(18)))
        self.status_label = tk.Label(
            inner,
            textvariable=self.status,
            bg=SURFACE,
            fg=MUTED,
            justify="left",
            anchor="w",
            wraplength=self._px(610),
            font=(self.font_family, 10),
        )
        self.status_label.pack(fill="x")
        tk.Label(
            inner,
            text="连接后会在后台自动同步；关闭窗口会最小化，登录 Windows 后自动运行。",
            bg=SURFACE,
            fg=MUTED,
            justify="left",
            anchor="w",
            wraplength=self._px(610),
            font=(self.font_family, 9),
        ).pack(fill="x", pady=(self._px(12), 0))

    def _build_connected_card(self) -> None:
        """已连接时隐藏配对与服务选择，展示设备状态、立即同步和解除配对。"""
        self.connected_card = self._make_card()
        inner = tk.Frame(self.connected_card, bg=SURFACE)
        inner.pack(fill="both", expand=True, padx=self._px(32), pady=self._px(30))
        tk.Label(
            inner,
            text="✓  已连接",
            bg=SUCCESS_TINT,
            fg=SUCCESS,
            font=(self.font_family, 10, "bold"),
            padx=self._px(12),
            pady=self._px(6),
        ).pack(anchor="w")
        tk.Label(
            inner,
            text="微信采集器正在运行",
            bg=SURFACE,
            fg=TEXT,
            font=(self.font_family, 18, "bold"),
            anchor="w",
        ).pack(fill="x", pady=(self._px(18), self._px(8)))
        tk.Label(
            inner,
            text="绑定后会自动同步，也可点击立即同步；只采集网页已勾选的群。",
            bg=SURFACE,
            fg=MUTED,
            justify="left",
            anchor="w",
            wraplength=self._px(610),
            font=(self.font_family, 10),
        ).pack(fill="x")
        self.connected_server_label = tk.Label(
            inner,
            text="",
            bg=SURFACE,
            fg=MUTED,
            justify="left",
            anchor="w",
            wraplength=self._px(610),
            font=(self.font_family, 9),
        )
        self.connected_server_label.pack(fill="x", pady=(self._px(10), self._px(18)))
        self.connected_status_label = tk.Label(
            inner,
            textvariable=self.status,
            bg=SURFACE,
            fg=SUCCESS,
            justify="left",
            anchor="w",
            wraplength=self._px(610),
            font=(self.font_family, 10),
        )
        self.connected_status_label.pack(fill="x", pady=(0, self._px(20)))
        autostart_row = tk.Frame(inner, bg=SURFACE)
        autostart_row.pack(fill="x", pady=(0, self._px(20)))
        autostart_copy = tk.Frame(autostart_row, bg=SURFACE)
        autostart_copy.pack(side="left", fill="x", expand=True)
        tk.Label(
            autostart_copy,
            text="登录 Windows 后自动启动",
            bg=SURFACE,
            fg=TEXT,
            font=(self.font_family, 10, "bold"),
            anchor="w",
        ).pack(fill="x")
        tk.Label(
            autostart_copy,
            textvariable=self.autostart_state,
            bg=SURFACE,
            fg=MUTED,
            font=(self.font_family, 9),
            anchor="w",
        ).pack(fill="x", pady=(self._px(3), 0))
        self.autostart_switch = tk.Canvas(
            autostart_row,
            width=self._px(50),
            height=self._px(28),
            bg=SURFACE,
            highlightthickness=0,
            cursor="hand2",
        )
        self.autostart_switch.pack(side="right", padx=(self._px(16), 0))
        self.autostart_switch.bind("<Button-1>", lambda _event: self.toggle_autostart())
        self._draw_autostart_switch()
        actions = tk.Frame(inner, bg=SURFACE)
        actions.pack(fill="x")
        # 手动同步复用后台工作线程，避免重复采集和凭证并发轮换。
        self.sync_button = ttk.Button(
            actions, text="立即同步", style="Accent.TButton", command=self.sync_now,
        )
        self.sync_button.pack(side="left")
        self.unpair_button = ttk.Button(
            actions,
            text="解除配对",
            style="Danger.TButton",
            command=self.unpair,
        )
        self.unpair_button.pack(side="right")

    def _draw_autostart_switch(self) -> None:
        """用普通 Canvas 绘制高 DPI 可缩放开关，兼容 Win7 的 Tk 8.6。"""
        if not hasattr(self, "autostart_switch"):
            return
        canvas = self.autostart_switch
        canvas.delete("all")
        width, height = self._px(50), self._px(28)
        radius = height // 2
        enabled = bool(self.autostart_var.get())
        track = ACCENT if enabled else "#c8d4e3"
        canvas.create_oval(0, 0, height, height, fill=track, outline="")
        canvas.create_oval(width - height, 0, width, height, fill=track, outline="")
        canvas.create_rectangle(radius, 0, width - radius, height, fill=track, outline="")
        knob = self._px(22)
        padding = self._px(3)
        left = width - knob - padding if enabled else padding
        canvas.create_oval(
            left,
            padding,
            left + knob,
            padding + knob,
            fill="#ffffff",
            outline="",
        )

    def toggle_autostart(self) -> None:
        """修改当前用户登录自启并保存选择，后台同步不会覆盖该开关。"""
        enabled = not bool(self.autostart_var.get())
        try:
            set_autostart(self.app.data_dir, enabled)
            config = self.app.config()
            config["autostart_enabled"] = enabled
            self.app.save_config(config)
        except (OSError, RuntimeError, ValueError):
            self._set_status("无法修改开机自启动，请检查当前用户注册表权限。", "error")
            return
        self.autostart_var.set(enabled)
        self.autostart_ready = enabled
        self.autostart_state.set("已开启" if enabled else "已关闭")
        self._draw_autostart_switch()
        self._set_status(
            "已开启登录 Windows 后自动启动。" if enabled else "已关闭开机自启动，仍可手动打开采集器。",
            "success" if enabled else "muted",
        )

    def _sync_autostart_indicator(self) -> None:
        """在主线程把注册表中的真实状态同步到开关，不覆盖用户选择。"""
        if not hasattr(self, "autostart_var"):
            return
        enabled = autostart_enabled(self.app.data_dir)
        self.autostart_ready = enabled
        self.autostart_var.set(enabled)
        self.autostart_state.set("已开启" if enabled else "已关闭")
        self._draw_autostart_switch()

    def _build_service_panel(self) -> None:
        """服务地址固定放在窗口底部，先展示官方、本地和自定义三档。"""
        tk.Frame(self.service_panel, bg=BORDER, height=self._px(1)).pack(
            fill="x", pady=(0, self._px(12))
        )
        heading = tk.Frame(self.service_panel, bg=BACKGROUND)
        heading.pack(fill="x", pady=(0, self._px(6)))
        tk.Label(
            heading,
            text="服务地址",
            bg=BACKGROUND,
            fg=TEXT,
            font=(self.font_family, 10, "bold"),
        ).pack(side="left")
        tk.Label(
            heading,
            text="默认使用官方服务",
            bg=BACKGROUND,
            fg=MUTED,
            font=(self.font_family, 9),
        ).pack(side="right")
        self.service_combo = ttk.Combobox(
            self.service_panel,
            textvariable=self.server_choice,
            values=[label for label, _url in SERVICE_CHOICES],
            state="readonly",
            style="Service.TCombobox",
        )
        self.service_combo.pack(fill="x")
        self.service_combo.bind("<<ComboboxSelected>>", self._on_service_changed)
        self.custom_server_row = tk.Frame(self.service_panel, bg=BACKGROUND)
        tk.Label(
            self.custom_server_row,
            text="自定义 HTTPS 地址",
            bg=BACKGROUND,
            fg=MUTED,
            font=(self.font_family, 9),
        ).pack(anchor="w", pady=(self._px(8), self._px(5)))
        self.custom_server_entry = ttk.Entry(
            self.custom_server_row,
            textvariable=self.server,
            font=(self.font_family, 10),
        )
        self.custom_server_entry.pack(fill="x", ipady=self._px(6))
        self._on_service_changed()

    def _on_code_changed(self, *_args) -> None:
        """配对码自动转大写并限制为 10 位，空值时不发起无意义请求。"""
        current = self.code.get()
        normalized = "".join(current.upper().split())[:10]
        if normalized != current:
            self.code.set(normalized)
        self._update_pair_button()

    def _on_service_changed(self, _event=None) -> None:
        """选择固定服务时同步请求地址；自定义项才显示地址输入框。"""
        label = self.server_choice.get()
        fixed_url = server_url_for_choice(label)
        if label == CUSTOM_SERVICE_LABEL:
            self.custom_server_row.pack(fill="x")
            if server_choice_for_url(self.server.get()) != CUSTOM_SERVICE_LABEL:
                self.server.set("")
            self.custom_server_entry.focus_set()
        else:
            self.custom_server_row.pack_forget()
            if fixed_url:
                self.server.set(fixed_url)

    def _current_server_url(self) -> str:
        return self.server.get().strip().rstrip("/")

    def _update_pair_button(self) -> None:
        """只允许输入完整 10 位配对码后连接，避免按钮重复误触。"""
        if not hasattr(self, "pair_button"):
            return
        if self.busy or len(self.code.get().strip()) != 10:
            self.pair_button.state(["disabled"])
        else:
            self.pair_button.state(["!disabled"])

    def _has_local_connection(self) -> bool:
        """本地凭证存在且未标记解绑时才展示已连接页面。"""
        try:
            config = self.app.config() if self.app.config_path.is_file() else {}
            return self.app.credentials_path.exists() and not config.get("unpaired")
        except (OSError, RuntimeError, ValueError):
            return False

    def _set_status(self, text: str, kind: str = "muted") -> None:
        """状态文字同时更新两个卡片，切换页面后仍能看到最近结果。"""
        self.status.set(text)
        color = {"success": SUCCESS, "error": DANGER}.get(kind, MUTED)
        for name in ("status_label", "connected_status_label"):
            label = getattr(self, name, None)
            if label is not None:
                label.configure(fg=color)

    def _refresh_connection_mode(self, *, reset_status: bool = False) -> None:
        """依据本地凭证切换配对页或已连接页。"""
        if not hasattr(self, "pairing_card"):
            return
        connected = self._has_local_connection()
        mode = "connected" if connected else "pairing"
        if mode == self._view_mode and not reset_status:
            return
        self._view_mode = mode
        if connected:
            self.pairing_card.pack_forget()
            self.service_panel.pack_forget()
            self._center_window(self.window_width, self.connected_height)
            self._sync_autostart_indicator()
            config = self.app.config() if self.app.config_path.is_file() else {}
            device_name = str(config.get("device_name") or socket.gethostname())
            server_url = str(config.get("server_url") or self._current_server_url())
            self.connected_server_label.configure(text=f"{device_name}  ·  {server_url}")
            self.connected_card.pack(fill="both", expand=True)
            if reset_status:
                self._set_status("正在与服务器同步设备状态…")
        else:
            self.connected_card.pack_forget()
            self.pairing_card.pack(fill="both", expand=True)
            self._center_window(self.window_width, self.pairing_height)
            if not self.service_panel.winfo_manager():
                # 解除配对后重新把服务栏放到内容下方，避免 pack 顺序把它挤出窗口。
                self.service_panel.pack(side="bottom", fill="x", pady=(self._px(18), 0))
                self.content.pack_forget()
                self.content.pack(fill="both", expand=True, pady=(self._px(22), 0))
            self._update_pair_button()
            if reset_status:
                self._set_status("等待输入配对码。")

    def work(self, operation):
        """阻止重复点击产生并发刷新、采集或凭证轮换。"""
        if self.busy or self.stopping:
            return
        self.busy = True
        self.pair_button.state(["disabled"])
        if hasattr(self, "sync_button"):
            self.sync_button.state(["disabled"])

        def execute():
            try:
                self.results.put((True, operation()))
            except (ClientApiError, WechatCliError, RuntimeError, ValueError, OSError, zipfile.BadZipFile) as error:
                code = getattr(error, "code", None)
                explanations = {"network_unavailable": "暂时无法连接服务，恢复网络后会自动重试。",
                                "browser_signature_banned": "服务代理拒绝了当前客户端签名，请下载最新版采集器。",
                                "access_denied": "服务代理拒绝了当前请求，请下载最新版采集器后重试。",
                                "http_error": "服务代理返回了无法识别的响应，请检查网络代理或更新采集器。",
                                "pairing_code_invalid": "配对码无效、过期或已经使用，请在网页重新生成。",
                                "device_already_paired": "这台电脑已有设备绑定，请先在网页撤销旧设备。",
                                "rate_limited": "请求过于频繁，请稍后再试。",
                                "component_unavailable": "内置微信读取组件不完整，请重新下载完整采集器。",
                                "wechat_not_ready": "微信读取尚未就绪，请先登录电脑微信，稍候会自动重试。",
                                "cli_failed": "微信读取组件未就绪，请检查微信登录及组件初始化。",
                                "group_catalog_limit": "群目录超过 200 个，请升级目录分页支持；本轮没有改变上传范围。",
                                "refresh_token_invalid": "连接凭证已失效，请在网页解除旧设备后重新配对。"}
                self.results.put((False, explanations.get(code, f"操作未完成：{code}。" if code else str(error))))
        threading.Thread(target=execute, daemon=True).start()

    def _update_buttons(self):
        """同步采集和更新的互斥状态，避免更新中同时刷新设备凭证。"""
        if not hasattr(self, 'check_update_button'):
            return
        self.check_update_button.state(['disabled'] if self.busy or self.stopping else ['!disabled'])
        info = self.update_info or {}
        enabled = not self.busy and not self.stopping and info.get('update_available') and info.get('can_install')
        self.install_update_button.state(['!disabled'] if enabled else ['disabled'])

    def check_update(self):
        """手动检查更新，不读取微信，也不会自动退出或安装程序。"""
        if self.busy or self.stopping:
            return
        server = self.app.config().get('server_url') if self.app.config_path.is_file() else self._current_server_url()
        self.update_status.set('正在检查更新…')
        self.work(lambda: {'update_check': updater.check_update(server)})
        self._update_buttons()

    def install_update(self):
        """一键下载并校验，准备完成才交给独立助手切换版本。"""
        if self.busy or self.stopping or not self.update_info:
            return
        info = dict(self.update_info)
        self.update_status.set('正在下载更新…')
        def progress(received, total):
            self.update_events.put(f'正在下载更新：{received * 100 // total}%')
        self.work(lambda: {'update_ready': str(updater.stage_update(info, progress=progress))})
        self._update_buttons()

    def _accept_update_result(self, result):
        """工作线程只产生数据，所有Tk变更与退出均在主线程执行。"""
        if 'update_check' in result:
            self.update_info = result['update_check']
            info = self.update_info
            if not info.get('update_supported'):
                text = '服务器暂无可用更新包，请稍后重试或从网页下载。'
            elif info.get('update_available'):
                text = f"发现新版本 v{info['version']}。点击一键更新后将自动重启。"
                if not info.get('can_install'):
                    text = '当前为源码运行，请从网页下载完整采集器后使用一键更新。'
            else:
                text = f"当前已是最新版本 v{info['current_version']}。"
            self.update_status.set(text)
        elif 'update_ready' in result:
            # 当前串行工作已结束，退出后释放GUI锁和采集锁；不强杀正在上传的进程。
            updater.launch_helper(Path(result['update_ready']), self.app.data_dir)
            self.update_status.set('校验完成，正在重启到新版本…')
            self.stopping = True
            self.root.destroy()

    def pair(self):
        """领取配对码后登记当前用户自启，网页核对设备后才开始上传。"""
        code = self.code.get().strip().upper()
        try:
            server = validate_server_url(self._current_server_url())
        except ValueError as error:
            self._set_status(str(error), "error")
            return

        def operation():
            if self.app.credentials_path.exists():
                raise RuntimeError("这台电脑已有连接，请先解除配对。")
            component = self._embedded_component_path()
            if not component:
                raise RuntimeError("采集器缺少内置微信读取组件，请重新下载完整发布包。")
            self.app.pair(server_url=server, code=code, device_name=socket.gethostname(), cli_path=component)
            # 重配时清除上次设备的调度窗口，网页确认后首轮立即执行。
            self.next_catalog = self.next_collection = 0.0
            if hasattr(self, "last_collection"):
                del self.last_collection
            self.selected = None
            self.next_tick = 0.0
            set_autostart(self.app.data_dir, True)
            self.autostart_ready = True
            return "已连接，请在网页核对并确认设备；之后在网页勾选需要绑定的群。"
        self.work(operation)

    def _embedded_component_path(self):
        """优先使用发布包内的受限入口，用户无需再选择 wechat-cli。"""
        package_root = Path(__file__).resolve().parents[2]
        wrapper = package_root / "scripts" / "wechat_component.py"
        if wrapper.is_file():
            return str(wrapper)
        bundled = package_root / "bin" / "wechat-cli.exe"
        if bundled.is_file():
            return str(bundled)
        return shutil.which("wechat-cli")

    def unpair(self):
        """取消自启优先于网络请求；服务不可达时也允许直接解除本机配对。"""
        if self.busy:
            # 同步尚未结束时排到下一项，避免按钮点击被忽略或并发轮换凭证。
            self.unpair_requested = True
            self._set_status("正在结束当前操作，随后解除配对……")
            return
        self.work(self.complete_unpair)

    def complete_unpair(self):
        """手动解绑和旧版待解绑状态使用相同清理流程与结果说明。"""
        set_autostart(self.app.data_dir, False)
        self.autostart_ready = False
        if self.app.unpair():
            return "已解除配对并取消登录自启；此窗口可以关闭。"
        return "无法连接服务，已单方面解除本机配对并取消登录自启；网页端旧设备可稍后手动移除。"

    def sync_now(self):
        """用户点击后跳过五分钟等待，暂停、待确认和撤销仍由原流程核验。"""
        if self.busy or self.stopping:
            return
        self.next_catalog = self.next_collection = 0.0
        if hasattr(self, "last_collection"):
            del self.last_collection
        self._set_status("正在立即同步，请稍候……")
        self.work(self.tick)

    def tick(self):
        """十五秒感知授权、同步请求与模型任务，按网页间隔或显式请求采集。"""
        config = self.app.config() if self.app.config_path.is_file() else {}
        if config.get("unpair_pending"):
            return self.complete_unpair()
        if config.get("unpaired") or not self.app.credentials_path.exists():
            return "尚未连接；在网页生成配对码后粘贴到此处。"
        state = self.app.pairing_status()
        if state == "revoked":
            set_autostart(self.app.data_dir, False)
            self.autostart_ready = False
            self.app.finish_unpair()
            return "网页已解除配对，本机已停止采集并取消登录自启。"
        # 用户明确关闭自启后，后台轮询不会自行重新打开。
        if config.get("autostart_enabled", True) and not self.autostart_ready:
            set_autostart(self.app.data_dir, True)
            self.autostart_ready = True
        if state != "active":
            return "等待网页核对并确认设备；登录后会自动继续等待。"
        now = time.monotonic()
        if now >= self.next_catalog:
            self.app.discover_groups()
            self.next_catalog = now + 300
        settings = self.app._request_device("GET", "/api/v1/device/config").get("settings", {})
        interval = ClientApplication.sync_interval(settings)
        # 网页间隔变更按上次采集时间重新计算，下次轮询即可生效。
        if hasattr(self, "last_collection"):
            self.next_collection = self.last_collection + interval
        selection = tuple(sorted(chat["chat_id"] for chat in settings.get("selected_chats", [])))
        if now >= self.next_collection or selection != self.selected or self.app.pending_upload_request(settings):
            result = self.app.sync_once()
            self.selected = selection
            self.last_collection = now
            self.next_collection = now + interval
            # 明确呈现未选群、暂停、部分失败，避免把操作结束误报为上传成功。
            messages = {
                "waiting_for_chat_selection": "群目录已同步，请在网页勾选允许上传的群并保存，随后会自动同步。",
                "partial": "本轮同步部分完成，仍有内容待重试；可点击立即同步。",
                "paused": "采集已暂停，请恢复采集后再同步。",
                "already_running": "已有同步任务正在进行，请稍候。",
            }
            message = messages.get(result.get("status"))
            if message:
                return message
        self.app.process_local_jobs()
        return f"连接正常，已绑定 {len(selection)} 个群；每{interval // 60}分钟自动采集上传，也可点击立即同步或让主助手请求。关闭窗口后继续运行。"

    def poll(self):
        """队列返回结果后更新界面，保留配对码外的诊断摘要。"""
        try:
            ok, text = self.results.get_nowait()
            if ok and isinstance(text, dict) and ('update_check' in text or 'update_ready' in text):
                try:
                    self._accept_update_result(text)
                except (OSError, RuntimeError, ValueError) as failure:
                    self.update_status.set(f'更新未完成：{failure}')
            else:
                self._set_status(text, 'success' if ok else 'error')
                if not ok and hasattr(self, 'update_status'):
                    self.update_status.set('操作未完成，可检查网络后重试更新。')
            if getattr(self, 'stopping', False):
                return
            self.busy = False
            if hasattr(self, "sync_button"):
                self.sync_button.state(["!disabled"])
            if ok and not isinstance(text, dict):
                self.code.set("")
            self._update_pair_button()
            self._refresh_connection_mode()
            if getattr(self, "_view_mode", "") == "connected":
                self._sync_autostart_indicator()
        except queue.Empty:
            pass
        if getattr(self, 'stopping', False):
            return
        if hasattr(self, 'update_events'):
            while not self.update_events.empty():
                self.update_status.set(self.update_events.get_nowait())
        self._update_buttons()
        if not self.busy and self.unpair_requested:
            self.unpair_requested = False
            self.work(self.complete_unpair)
        elif not self.busy and time.monotonic() >= getattr(self, "next_tick", 0):
            self.next_tick = time.monotonic() + 15
            self.work(self.tick)
        self.root.after(200, self.poll)


def main():
    """整次 GUI 会话持有互斥锁，重复双击不会产生两个采集循环。"""
    parser = argparse.ArgumentParser()
    parser.add_argument("--data-dir")
    parser.add_argument("--background", action="store_true")
    parser.add_argument("--update-token")
    args = parser.parse_args()
    app = ClientApplication(args.data_dir or default_data_dir())
    with SingleInstanceLock(app.data_dir / "collector-gui.lock") as lock:
        enable_high_dpi()
        root = tk.Tk()
        if not lock.acquired:
            root.withdraw()
            messagebox.showinfo("知序采集器", "采集客户端已经运行，请从任务栏打开现有窗口。")
            root.destroy()
            return
        CollectorWindow(root, app, background=args.background)
        if args.update_token:
            # 只有新版GUI成功创建且取得单实例锁后，更新助手才认定启动成功。
            import re
            if not re.fullmatch(r'[0-9a-f]{32}', args.update_token):
                raise ValueError('更新握手参数无效。')
            ready = app.data_dir / 'updates' / (args.update_token + '.ready')
            ready.parent.mkdir(parents=True, exist_ok=True)
            root.after_idle(lambda: ready.write_text('ready', encoding='utf-8'))
        root.mainloop()
