from __future__ import annotations

import json
import os
import platform
import queue
import re
import threading
import time
import uuid
from dataclasses import asdict, dataclass, field
from datetime import datetime
from pathlib import Path
import tkinter as tk
from tkinter import filedialog, messagebox, simpledialog, ttk

from .asc_client import ASCConfig, AppStoreConnectClient
from .sound import play_sound


APP_NAME = "iOS 审核状态监控"
PLACEHOLDER_P8 = "尚未选择 .p8 文件"

WAITING_STATES = {
    "PREPARE_FOR_SUBMISSION",
    "DEVELOPER_REJECTED",
    "READY_FOR_REVIEW",
    "READY_TO_SUBMIT",
    "WAITING_FOR_REVIEW",
    "INVALID_BINARY",
    "NOT_IN_REVIEW",
}
IN_REVIEW_STATES = {"IN_REVIEW"}
DONE_STATES = {
    "PENDING_DEVELOPER_RELEASE",
    "PENDING_APPLE_RELEASE",
    "PROCESSING_FOR_APP_STORE",
    "READY_FOR_SALE",
    "REJECTED",
    "METADATA_REJECTED",
    "APPROVED",
    "ACCEPTED",
    "RUNNING",
    "COMPLETE",
    "COMPLETED",
    "ENDED",
    "STOPPED",
}

STATE_LABELS = {
    "PREPARE_FOR_SUBMISSION": "准备提交",
    "WAITING_FOR_REVIEW": "等待审核",
    "IN_REVIEW": "正在审核",
    "PENDING_DEVELOPER_RELEASE": "等待开发者发布",
    "PENDING_APPLE_RELEASE": "等待 Apple 发布",
    "PROCESSING_FOR_APP_STORE": "正在上架处理",
    "READY_FOR_SALE": "已完成 / 可销售",
    "REJECTED": "审核完成 / 已拒绝",
    "METADATA_REJECTED": "审核完成 / 元数据被拒",
    "DEVELOPER_REJECTED": "开发者已拒绝",
    "INVALID_BINARY": "二进制无效",
    "APPROVED": "已审核通过",
    "ACCEPTED": "已审核通过",
    "RUNNING": "已完成 / 正在运行",
    "COMPLETE": "已完成",
    "COMPLETED": "已完成",
    "ENDED": "已结束",
    "STOPPED": "已停止",
    "NOT_FOUND": "未找到",
    "NOT_IN_REVIEW": "未在审核中",
}


def data_dir() -> Path:
    system = platform.system()
    if system == "Windows":
        base = Path(os.environ.get("APPDATA", Path.home()))
    elif system == "Darwin":
        base = Path.home() / "Library" / "Application Support"
    else:
        base = Path.home() / ".config"
    path = base / "iOSReviewMonitor"
    path.mkdir(parents=True, exist_ok=True)
    return path


def classify_state(state: str) -> str:
    normalized = (state or "").upper()
    if normalized in IN_REVIEW_STATES or "IN_REVIEW" in normalized:
        return "in_review"
    if normalized in DONE_STATES or any(token in normalized for token in ("APPROVED", "ACCEPTED", "COMPLETE", "COMPLETED")):
        return "done"
    if normalized in WAITING_STATES or "WAITING" in normalized or "READY_FOR_REVIEW" in normalized:
        return "waiting"
    return "unknown"


def status_text(state: str) -> str:
    normalized = (state or "").upper()
    return STATE_LABELS.get(normalized, state or "未知")


def parse_app_ids(raw: str) -> list[str]:
    seen: set[str] = set()
    app_ids: list[str] = []
    for item in re.split(r"[\s,;，；]+", raw.strip()):
        app_id = item.strip()
        if app_id and app_id not in seen:
            seen.add(app_id)
            app_ids.append(app_id)
    return app_ids


def safe_file_stem(value: str) -> str:
    cleaned = re.sub(r"[^a-zA-Z0-9_.-]+", "_", value.strip())
    return cleaned.strip("._") or "account"


@dataclass
class AccountSettings:
    id: str = field(default_factory=lambda: f"profile-{uuid.uuid4().hex[:10]}")
    name: str = "账号1"
    key_id: str = ""
    issuer_id: str = ""
    p8_path: str = ""
    app_id: str = ""
    bundle_id: str = ""
    refresh_seconds: int = 300
    sound_enabled: bool = True
    demo_mode: bool = False


@dataclass
class ProfilesDocument:
    active_profile_id: str = ""
    profiles: list[AccountSettings] = field(default_factory=list)


class ReviewMonitorApp:
    def __init__(self) -> None:
        self.root = tk.Tk()
        self.root.title(APP_NAME)
        self.root.geometry("1120x720")
        self.root.minsize(980, 640)
        self.root.configure(bg="#f4f6f8")

        self.config_path = data_dir() / "settings.json"
        self.document = self.load_document()
        self.tabs: dict[str, AccountTab] = {}

        self.build_ui()
        self.build_tabs()
        self.root.protocol("WM_DELETE_WINDOW", self.on_close)

    def load_document(self) -> ProfilesDocument:
        if not self.config_path.exists():
            profile = AccountSettings()
            return ProfilesDocument(active_profile_id=profile.id, profiles=[profile])
        try:
            raw = json.loads(self.config_path.read_text("utf-8"))
        except Exception:
            profile = AccountSettings()
            return ProfilesDocument(active_profile_id=profile.id, profiles=[profile])

        if isinstance(raw, dict) and isinstance(raw.get("profiles"), list):
            profiles = [self.profile_from_dict(item, index) for index, item in enumerate(raw["profiles"], start=1)]
            profiles = [profile for profile in profiles if profile.id]
            if not profiles:
                profiles = [AccountSettings()]
            active_id = str(raw.get("active_profile_id") or profiles[0].id)
            return ProfilesDocument(active_profile_id=active_id, profiles=profiles)

        profile = self.profile_from_dict(raw if isinstance(raw, dict) else {}, 1)
        if not profile.name:
            profile.name = "账号1"
        return ProfilesDocument(active_profile_id=profile.id, profiles=[profile])

    def profile_from_dict(self, data: dict[str, object], index: int) -> AccountSettings:
        defaults = asdict(AccountSettings(name=f"账号{index}"))
        merged = {**defaults, **data}
        if not merged.get("id"):
            merged["id"] = f"profile-{uuid.uuid4().hex[:10]}"
        if not merged.get("name"):
            merged["name"] = f"账号{index}"
        try:
            merged["refresh_seconds"] = max(30, int(merged.get("refresh_seconds") or 300))
        except (TypeError, ValueError):
            merged["refresh_seconds"] = 300
        allowed = set(defaults)
        return AccountSettings(**{key: merged[key] for key in allowed})

    def save_document(self) -> None:
        profiles = [tab.collect_settings() for tab in self.tabs.values()]
        selected = self.notebook.select()
        active_id = ""
        if selected:
            selected_tab = self.tab_by_widget(selected)
            active_id = selected_tab.settings.id if selected_tab else ""
        self.document = ProfilesDocument(active_profile_id=active_id, profiles=profiles)
        payload = {
            "active_profile_id": self.document.active_profile_id,
            "profiles": [asdict(profile) for profile in self.document.profiles],
        }
        self.config_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), "utf-8")

    def build_ui(self) -> None:
        style = ttk.Style()
        style.configure("Title.TLabel", font=("", 22, "bold"))
        style.configure("Status.TLabel", font=("", 26, "bold"))
        style.configure("Metric.TLabel", font=("", 14, "bold"))
        style.configure("Primary.TButton", font=("", 12, "bold"))

        self.root.columnconfigure(0, weight=1)
        self.root.rowconfigure(2, weight=1)

        header = ttk.Frame(self.root, padding=(18, 16, 18, 8))
        header.grid(row=0, column=0, sticky="ew")
        header.columnconfigure(0, weight=1)
        ttk.Label(header, text=APP_NAME, style="Title.TLabel").grid(row=0, column=0, sticky="w")
        ttk.Label(header, text="每个标签页是一个 Apple 账号，可分别配置 API Key、p8 文件和 App ID。").grid(row=1, column=0, sticky="w", pady=(4, 0))

        toolbar = ttk.Frame(self.root, padding=(18, 0, 18, 8))
        toolbar.grid(row=1, column=0, sticky="ew")
        ttk.Button(toolbar, text="+ 新账号", command=self.add_account).grid(row=0, column=0, padx=(0, 8))
        ttk.Button(toolbar, text="重命名账号", command=self.rename_current_account).grid(row=0, column=1, padx=(0, 8))
        ttk.Button(toolbar, text="关闭当前账号", command=self.close_current_account).grid(row=0, column=2, padx=(0, 8))
        ttk.Button(toolbar, text="保存全部配置", command=self.save_all).grid(row=0, column=3)

        self.notebook = ttk.Notebook(self.root)
        self.notebook.grid(row=2, column=0, sticky="nsew", padx=18, pady=(0, 18))
        self.notebook.bind("<<NotebookTabChanged>>", self.on_tab_changed)

    def build_tabs(self) -> None:
        active_widget = None
        for profile in self.document.profiles:
            tab = self.create_tab(profile)
            if profile.id == self.document.active_profile_id:
                active_widget = tab.frame
        if active_widget is not None:
            self.notebook.select(active_widget)

    def create_tab(self, settings: AccountSettings) -> "AccountTab":
        frame = ttk.Frame(self.notebook)
        self.notebook.add(frame, text=settings.name)
        tab = AccountTab(self, frame, settings)
        self.tabs[str(frame)] = tab
        return tab

    def add_account(self) -> None:
        index = len(self.tabs) + 1
        profile = AccountSettings(name=f"账号{index}")
        tab = self.create_tab(profile)
        self.notebook.select(tab.frame)
        self.save_document()

    def rename_current_account(self) -> None:
        tab = self.current_tab()
        if not tab:
            return
        name = simpledialog.askstring("重命名账号", "请输入账号标签名称：", initialvalue=tab.name_var.get(), parent=self.root)
        if not name:
            return
        tab.name_var.set(name.strip())
        self.notebook.tab(tab.frame, text=tab.name_var.get())
        self.save_document()

    def close_current_account(self) -> None:
        tab = self.current_tab()
        if not tab:
            return
        if len(self.tabs) <= 1:
            messagebox.showinfo("无法关闭", "至少保留一个账号标签。")
            return
        if tab.running and not messagebox.askyesno("关闭账号", "当前账号正在监控，确定停止并关闭吗？"):
            return
        tab.stop()
        tab.closed = True
        self.tabs.pop(str(tab.frame), None)
        self.notebook.forget(tab.frame)
        self.save_document()

    def save_all(self) -> None:
        self.save_document()
        current = self.current_tab()
        if current:
            current.log("全部账号配置已保存")

    def on_tab_changed(self, _event: tk.Event) -> None:
        self.save_document()

    def current_tab(self) -> "AccountTab | None":
        selected = self.notebook.select()
        return self.tab_by_widget(selected)

    def tab_by_widget(self, widget_name: str) -> "AccountTab | None":
        return self.tabs.get(str(widget_name))

    def update_tab_title(self, tab: "AccountTab") -> None:
        title = tab.name_var.get().strip() or "未命名账号"
        if tab.running:
            title = f"{title} *"
        self.notebook.tab(tab.frame, text=title)

    def on_close(self) -> None:
        for tab in list(self.tabs.values()):
            tab.stop()
            tab.closed = True
        self.save_document()
        self.root.destroy()

    def run(self) -> None:
        self.root.mainloop()


class AccountTab:
    def __init__(self, app: ReviewMonitorApp, frame: ttk.Frame, settings: AccountSettings) -> None:
        self.app = app
        self.root = app.root
        self.frame = frame
        self.settings = settings
        self.events: queue.Queue[tuple[str, object]] = queue.Queue()
        self.stop_event = threading.Event()
        self.worker: threading.Thread | None = None
        self.validation_worker: threading.Thread | None = None
        self.running = False
        self.last_categories: dict[str, str] = {}
        self.app_rows: dict[str, str] = {}
        self.next_check_at = 0.0
        self.started_at = 0.0
        self.closed = False
        self.log_path = data_dir() / f"monitor-{safe_file_stem(settings.id)}.log"

        self.build_ui()
        self.apply_settings()
        self.root.after(200, self.drain_events)
        self.root.after(500, self.tick)

    def build_ui(self) -> None:
        self.frame.columnconfigure(0, weight=0)
        self.frame.columnconfigure(1, weight=1)
        self.frame.rowconfigure(3, weight=1)

        config = ttk.LabelFrame(self.frame, text="连接配置", padding=12)
        config.grid(row=0, column=0, rowspan=4, sticky="nsw", padx=(0, 14), pady=12)
        config.columnconfigure(0, weight=1)

        self.name_var = tk.StringVar()
        self.key_id_var = tk.StringVar()
        self.issuer_id_var = tk.StringVar()
        self.p8_path_var = tk.StringVar()
        self.bundle_id_var = tk.StringVar()
        self.refresh_var = tk.StringVar()
        self.sound_var = tk.BooleanVar()
        self.demo_var = tk.BooleanVar()

        self.add_entry(config, "账号标签名称", self.name_var, 0)
        self.add_entry(config, "Key ID", self.key_id_var, 2)
        self.add_entry(config, "Issuer ID", self.issuer_id_var, 4)

        ttk.Label(config, text=".p8 私钥文件").grid(row=6, column=0, sticky="w", pady=(8, 3))
        p8_row = ttk.Frame(config)
        p8_row.grid(row=7, column=0, sticky="ew")
        p8_row.columnconfigure(0, weight=1)
        self.p8_label = ttk.Label(p8_row, textvariable=self.p8_path_var, width=34)
        self.p8_label.grid(row=0, column=0, sticky="ew", padx=(0, 8))
        ttk.Button(p8_row, text="选择文件...", command=self.choose_p8).grid(row=0, column=1)

        ttk.Label(config, text="App ID（可填多个，用逗号或换行分隔）").grid(row=8, column=0, sticky="w", pady=(8, 3))
        app_id_frame = ttk.Frame(config)
        app_id_frame.grid(row=9, column=0, sticky="ew")
        app_id_frame.columnconfigure(0, weight=1)
        self.app_ids_text = tk.Text(app_id_frame, height=4, width=42, wrap="word")
        self.app_ids_text.grid(row=0, column=0, sticky="ew")
        app_scroll = ttk.Scrollbar(app_id_frame, command=self.app_ids_text.yview)
        app_scroll.grid(row=0, column=1, sticky="ns")
        self.app_ids_text.configure(yscrollcommand=app_scroll.set)

        self.add_entry(config, "Bundle ID（备用：只监控一个 App）", self.bundle_id_var, 10)
        self.add_entry(config, "检查间隔（秒，至少 30）", self.refresh_var, 12)

        ttk.Checkbutton(config, text="启用提示音", variable=self.sound_var).grid(row=14, column=0, sticky="w", pady=(10, 0))
        ttk.Checkbutton(config, text="演示模式（不连接 Apple）", variable=self.demo_var).grid(row=15, column=0, sticky="w", pady=(6, 0))

        tests = ttk.Frame(config)
        tests.grid(row=16, column=0, sticky="ew", pady=(12, 0))
        tests.columnconfigure((0, 1), weight=1)
        ttk.Button(tests, text="测试正在审核音", command=lambda: play_sound("in_review")).grid(row=0, column=0, sticky="ew", padx=(0, 6))
        ttk.Button(tests, text="测试完成音", command=lambda: play_sound("done")).grid(row=0, column=1, sticky="ew", padx=(6, 0))

        actions = ttk.Frame(config)
        actions.grid(row=17, column=0, sticky="ew", pady=(14, 0))
        actions.columnconfigure((0, 1), weight=1)
        self.start_btn = ttk.Button(actions, text="开始监控", style="Primary.TButton", command=self.start)
        self.start_btn.grid(row=0, column=0, sticky="ew", padx=(0, 6))
        self.stop_btn = ttk.Button(actions, text="停止", command=self.stop, state="disabled")
        self.stop_btn.grid(row=0, column=1, sticky="ew", padx=(6, 0))

        ttk.Button(config, text="保存当前账号", command=self.save_current).grid(row=18, column=0, sticky="ew", pady=(10, 0))

        summary = ttk.LabelFrame(self.frame, text="审核状态", padding=16)
        summary.grid(row=0, column=1, sticky="ew", pady=(12, 0))
        summary.columnconfigure((0, 1, 2), weight=1)
        self.state_var = tk.StringVar(value="未开始")
        self.version_var = tk.StringVar(value="--")
        self.checked_var = tk.StringVar(value="--")
        self.next_var = tk.StringVar(value="--")
        self.running_var = tk.StringVar(value="00:00")
        visual = ttk.Frame(summary)
        visual.grid(row=0, column=0, columnspan=3, sticky="ew")
        visual.columnconfigure(1, weight=1)
        self.status_canvas = tk.Canvas(visual, width=72, height=72, highlightthickness=0, bg="#f4f6f8")
        self.status_canvas.grid(row=0, column=0, sticky="w", padx=(0, 14))
        self.status_canvas.create_oval(8, 8, 64, 64, fill="#9aa4b2", outline="")
        ttk.Label(visual, textvariable=self.state_var, style="Status.TLabel").grid(row=0, column=1, sticky="w")
        for col, (label, var) in enumerate([("版本/方案", self.version_var), ("上次检查", self.checked_var), ("下次检查", self.next_var)]):
            ttk.Label(summary, text=label).grid(row=1, column=col, sticky="w", pady=(18, 3))
            ttk.Label(summary, textvariable=var, style="Metric.TLabel").grid(row=2, column=col, sticky="w")
        ttk.Label(summary, text="运行时长").grid(row=3, column=0, sticky="w", pady=(12, 3))
        ttk.Label(summary, textvariable=self.running_var, style="Metric.TLabel").grid(row=4, column=0, sticky="w")

        rules = ttk.LabelFrame(self.frame, text="提示规则", padding=12)
        rules.grid(row=1, column=1, sticky="ew", pady=(12, 12))
        ttk.Label(rules, text="等待审核：只更新界面和日志，不播放声音。").grid(row=0, column=0, sticky="w")
        ttk.Label(rules, text="正在审核：第一次进入 IN_REVIEW 时播放短促上升提示音。").grid(row=1, column=0, sticky="w", pady=(5, 0))
        ttk.Label(rules, text="已完成：进入等待发布、Apple 发布处理或 Ready for Sale 时播放完成提示音。").grid(row=2, column=0, sticky="w", pady=(5, 0))

        apps_frame = ttk.LabelFrame(self.frame, text="监控 App", padding=8)
        apps_frame.grid(row=2, column=1, sticky="nsew", pady=(0, 12))
        apps_frame.rowconfigure(0, weight=1)
        apps_frame.columnconfigure(0, weight=1)
        self.apps_tree = ttk.Treeview(
            apps_frame,
            columns=("app_id", "status", "raw", "checked", "detail"),
            show="headings",
            height=7,
        )
        for column, title, width in [
            ("app_id", "App ID", 130),
            ("status", "状态", 150),
            ("raw", "原始状态", 190),
            ("checked", "检查时间", 90),
            ("detail", "详情", 360),
        ]:
            self.apps_tree.heading(column, text=title)
            self.apps_tree.column(column, width=width, anchor="w")
        self.apps_tree.grid(row=0, column=0, sticky="nsew")
        apps_scroll = ttk.Scrollbar(apps_frame, command=self.apps_tree.yview)
        apps_scroll.grid(row=0, column=1, sticky="ns")
        self.apps_tree.configure(yscrollcommand=apps_scroll.set)

        log_frame = ttk.LabelFrame(self.frame, text="运行日志", padding=8)
        log_frame.grid(row=3, column=1, sticky="nsew")
        log_frame.rowconfigure(0, weight=1)
        log_frame.columnconfigure(0, weight=1)
        self.log_text = tk.Text(log_frame, height=18, wrap="word", state="disabled")
        self.log_text.grid(row=0, column=0, sticky="nsew")
        scroll = ttk.Scrollbar(log_frame, command=self.log_text.yview)
        scroll.grid(row=0, column=1, sticky="ns")
        self.log_text.configure(yscrollcommand=scroll.set)

    def add_entry(self, parent: ttk.Frame, label: str, var: tk.StringVar, row: int) -> None:
        ttk.Label(parent, text=label).grid(row=row, column=0, sticky="w", pady=(8 if row else 0, 3))
        ttk.Entry(parent, textvariable=var, width=44).grid(row=row + 1, column=0, sticky="ew")

    def apply_settings(self) -> None:
        self.name_var.set(self.settings.name)
        self.key_id_var.set(self.settings.key_id)
        self.issuer_id_var.set(self.settings.issuer_id)
        self.p8_path_var.set(self.settings.p8_path or PLACEHOLDER_P8)
        self.app_ids_text.delete("1.0", "end")
        self.app_ids_text.insert("1.0", self.settings.app_id)
        self.bundle_id_var.set(self.settings.bundle_id)
        self.refresh_var.set(str(self.settings.refresh_seconds))
        self.sound_var.set(self.settings.sound_enabled)
        self.demo_var.set(self.settings.demo_mode)

    def collect_settings(self) -> AccountSettings:
        try:
            refresh = max(30, int(self.refresh_var.get().strip() or "300"))
        except ValueError:
            refresh = 300
        self.settings = AccountSettings(
            id=self.settings.id,
            name=self.name_var.get().strip() or self.settings.name or "未命名账号",
            key_id=self.key_id_var.get().strip(),
            issuer_id=self.issuer_id_var.get().strip(),
            p8_path="" if self.p8_path_var.get().strip() == PLACEHOLDER_P8 else self.p8_path_var.get().strip(),
            app_id=self.app_ids_text.get("1.0", "end").strip(),
            bundle_id=self.bundle_id_var.get().strip(),
            refresh_seconds=refresh,
            sound_enabled=bool(self.sound_var.get()),
            demo_mode=bool(self.demo_var.get()),
        )
        return self.settings

    def save_current(self) -> None:
        self.collect_settings()
        self.app.update_tab_title(self)
        self.app.save_document()
        self.log("当前账号配置已保存")

    def choose_p8(self) -> None:
        path = filedialog.askopenfilename(
            title="选择 App Store Connect API .p8 私钥",
            filetypes=[("Apple API 私钥", "*.p8"), ("所有文件", "*.*")],
        )
        if path:
            self.p8_path_var.set(path)

    def start(self) -> None:
        self.collect_settings()
        self.app.update_tab_title(self)
        self.app.save_document()
        if not self.settings.demo_mode:
            missing = []
            if not self.settings.key_id:
                missing.append("Key ID")
            if not self.settings.issuer_id:
                missing.append("Issuer ID")
            if not self.settings.p8_path:
                missing.append(".p8 私钥文件")
            if not parse_app_ids(self.settings.app_id) and not self.settings.bundle_id:
                missing.append("App ID")
            if missing:
                messagebox.showwarning("配置不完整", "请补充：" + "、".join(missing))
                return

        self.running = True
        self.started_at = time.time()
        self.last_categories = {}
        self.app_rows = {}
        for item in self.apps_tree.get_children():
            self.apps_tree.delete(item)
        self.stop_event.clear()
        self.start_btn.configure(state="disabled")
        self.stop_btn.configure(state="normal")
        self.app.update_tab_title(self)
        app_count = len(parse_app_ids(self.settings.app_id)) or (1 if self.settings.bundle_id else 0)
        self.log(f"开始配置自检，App 数量：{app_count}")
        self.state_var.set("正在自检配置")
        self.validation_worker = threading.Thread(target=self.validation_loop, daemon=True)
        self.validation_worker.start()

    def validation_loop(self) -> None:
        try:
            if self.settings.demo_mode:
                if self.stop_event.is_set():
                    return
                self.events.put(("validation_ok", "演示模式已跳过 Apple API 自检"))
                return

            client = self.make_client()
            app_ids = parse_app_ids(self.settings.app_id)
            if self.stop_event.is_set():
                return
            self.events.put(("log", "验证 API Key、Issuer ID 和 .p8 私钥"))
            client.validate_credentials()
            self.events.put(("log", "API 凭证验证通过"))

            if not app_ids:
                resolved = client.resolve_app_id()
                app_ids = [resolved]
                self.events.put(("log", f"Bundle ID 已解析为 App ID：{resolved}"))

            for app_id in app_ids:
                if self.stop_event.is_set():
                    return
                self.events.put(("log", f"检查 App ID 是否存在：{app_id}"))
                app_info = client.app_exists(app_id)
                app_name = app_info.get("name") or "未命名 App"
                bundle_id = app_info.get("bundle_id") or "未知 Bundle ID"
                self.events.put(("log", f"App ID 有效：{app_id}，名称：{app_name}，Bundle ID：{bundle_id}"))
                self.events.put(("log", f"自检 App {app_id} 的最终审核状态"))
                review_status = client.unified_review_status(app_id)
                self.events.put(("log", f"自检通过：App {app_id} 最终状态为 {review_status.get('state', 'UNKNOWN')}；{review_status.get('detail', '')}"))

            self.events.put(("validation_ok", "配置自检通过，开始监控"))
        except Exception as exc:
            self.events.put(("validation_error", exc))

    def begin_monitoring_after_validation(self, message: str) -> None:
        if self.stop_event.is_set():
            return
        self.log(message)
        self.state_var.set("配置自检通过")
        self.worker = threading.Thread(target=self.worker_loop, daemon=True)
        self.worker.start()

    def stop(self) -> None:
        self.stop_event.set()
        self.running = False
        self.start_btn.configure(state="normal")
        self.stop_btn.configure(state="disabled")
        self.next_var.set("--")
        self.app.update_tab_title(self)
        self.log("已停止")

    def make_client(self) -> AppStoreConnectClient:
        return AppStoreConnectClient(
            ASCConfig(
                key_id=self.settings.key_id,
                issuer_id=self.settings.issuer_id,
                p8_path=self.settings.p8_path,
                app_id=self.settings.app_id,
                bundle_id=self.settings.bundle_id,
            )
        )

    def worker_loop(self) -> None:
        client = self.make_client()
        demo_states = ["WAITING_FOR_REVIEW", "IN_REVIEW", "PENDING_DEVELOPER_RELEASE", "READY_FOR_SALE"]
        demo_index = 0
        app_ids = parse_app_ids(self.settings.app_id)
        if self.settings.demo_mode and not app_ids:
            app_ids = ["Demo-App-1", "Demo-App-2"]
        while not self.stop_event.is_set():
            try:
                if self.settings.demo_mode:
                    for app_id in app_ids:
                        state = demo_states[demo_index % len(demo_states)]
                        demo_index += 1
                        self.events.put(("state", {
                            "app_id": app_id,
                            "monitor_type": "unified_review",
                            "name": "最终审核状态",
                            "version": "Demo 1.0",
                            "state": state,
                            "detail": "演示模式",
                        }))
                else:
                    if app_ids:
                        for app_id in app_ids:
                            self.events.put(("log", f"开始检查 App {app_id} 的最终审核状态"))
                            try:
                                self.events.put(("state", client.unified_review_status(app_id)))
                            except Exception as exc:
                                self.events.put(("error", f"App {app_id} 最终审核状态：{exc}"))
                    else:
                        self.events.put(("state", client.unified_review_status()))
            except Exception as exc:
                self.events.put(("error", exc))

            wait_seconds = max(30, self.settings.refresh_seconds)
            self.next_check_at = time.time() + wait_seconds
            self.events.put(("next", self.next_check_at))
            if self.stop_event.wait(wait_seconds):
                break

    def drain_events(self) -> None:
        if self.closed:
            return
        while True:
            try:
                kind, payload = self.events.get_nowait()
            except queue.Empty:
                break
            if kind == "state":
                self.handle_state(payload)  # type: ignore[arg-type]
            elif kind == "log":
                self.log(str(payload))
            elif kind == "error":
                self.log(f"错误：{payload}")
                self.checked_var.set(datetime.now().strftime("%H:%M:%S"))
            elif kind == "validation_ok":
                self.begin_monitoring_after_validation(str(payload))
            elif kind == "validation_error":
                self.handle_validation_error(payload)
            elif kind == "next":
                self.next_var.set(datetime.fromtimestamp(float(payload)).strftime("%H:%M:%S"))
        if not self.closed and self.frame.winfo_exists():
            self.root.after(200, self.drain_events)

    def handle_validation_error(self, payload: object) -> None:
        message = f"配置自检失败：{payload}"
        self.log(message)
        self.running = False
        self.start_btn.configure(state="normal")
        self.stop_btn.configure(state="disabled")
        self.next_var.set("--")
        self.state_var.set("配置自检失败")
        self.app.update_tab_title(self)
        messagebox.showerror("配置自检失败", message)

    def handle_state(self, result: dict[str, str]) -> None:
        app_id = str(result.get("app_id") or "--")
        name = str(result.get("name") or "最终审核状态")
        source = str(result.get("source") or "app_store_version")
        detail = str(result.get("detail") or source)
        state = result.get("state", "UNKNOWN")
        category = classify_state(state)
        label = status_text(state)
        checked_at = datetime.now().strftime("%H:%M:%S")
        self.state_var.set(f"{app_id}：{label}")
        self.version_var.set(name)
        self.checked_var.set(checked_at)
        self.update_app_row(app_id, label, state, checked_at, detail)
        self.log(f"App {app_id} 最终审核状态：{label}（{state}），来源：{source}，详情：{detail}")
        self.paint_status(category)

        key = app_id
        if category != self.last_categories.get(key):
            if category == "in_review":
                self.log(f"提示：App {app_id} 进入正在审核")
                if self.settings.sound_enabled:
                    play_sound("in_review")
            elif category == "done":
                self.log(f"提示：App {app_id} 审核流程已完成")
                if self.settings.sound_enabled:
                    play_sound("done")
        self.last_categories[key] = category

    def update_app_row(
        self,
        app_id: str,
        label: str,
        state: str,
        checked_at: str,
        detail: str,
    ) -> None:
        values = (app_id, label, state, checked_at, detail)
        key = app_id
        row_id = self.app_rows.get(key)
        if row_id and self.apps_tree.exists(row_id):
            self.apps_tree.item(row_id, values=values)
            return
        row_id = self.apps_tree.insert("", "end", values=values)
        self.app_rows[key] = row_id

    def paint_status(self, category: str) -> None:
        colors = {
            "waiting": "#9aa4b2",
            "in_review": "#2f80ed",
            "done": "#1f9d55",
            "unknown": "#d97706",
        }
        fill = colors.get(category, colors["unknown"])
        self.status_canvas.delete("all")
        self.status_canvas.create_oval(8, 8, 64, 64, fill=fill, outline="")
        self.status_canvas.create_oval(22, 22, 50, 50, fill="#ffffff", outline="")

    def tick(self) -> None:
        if self.closed:
            return
        if self.running:
            elapsed = int(time.time() - self.started_at)
            self.running_var.set(f"{elapsed // 3600:02d}:{(elapsed // 60) % 60:02d}:{elapsed % 60:02d}")
            if self.next_check_at:
                remain = max(0, int(self.next_check_at - time.time()))
                self.next_var.set(f"{remain // 60:02d}:{remain % 60:02d}")
        if not self.closed and self.frame.winfo_exists():
            self.root.after(500, self.tick)

    def log(self, message: str) -> None:
        line = f"[{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}] [{self.name_var.get() or self.settings.name}] {message}\n"
        self.log_path.parent.mkdir(parents=True, exist_ok=True)
        with self.log_path.open("a", encoding="utf-8") as fh:
            fh.write(line)
        self.log_text.configure(state="normal")
        self.log_text.insert("end", line)
        self.log_text.see("end")
        self.log_text.configure(state="disabled")


def main() -> None:
    ReviewMonitorApp().run()
