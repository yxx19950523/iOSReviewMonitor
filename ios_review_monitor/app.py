from __future__ import annotations

import json
import os
import platform
import queue
import re
import threading
import time
from dataclasses import asdict, dataclass
from datetime import datetime
from pathlib import Path
import tkinter as tk
from tkinter import filedialog, messagebox, ttk

from .asc_client import ASCConfig, AppStoreConnectClient
from .sound import play_sound


APP_NAME = "iOS 审核状态监控"

WAITING_STATES = {
    "PREPARE_FOR_SUBMISSION",
    "DEVELOPER_REJECTED",
    "WAITING_FOR_REVIEW",
    "INVALID_BINARY",
}
IN_REVIEW_STATES = {"IN_REVIEW"}
DONE_STATES = {
    "PENDING_DEVELOPER_RELEASE",
    "PENDING_APPLE_RELEASE",
    "PROCESSING_FOR_APP_STORE",
    "READY_FOR_SALE",
    "REJECTED",
    "METADATA_REJECTED",
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
    if state in IN_REVIEW_STATES:
        return "in_review"
    if state in DONE_STATES:
        return "done"
    if state in WAITING_STATES:
        return "waiting"
    return "unknown"


def status_text(state: str) -> str:
    return STATE_LABELS.get(state, state or "未知")


def parse_app_ids(raw: str) -> list[str]:
    seen: set[str] = set()
    app_ids: list[str] = []
    for item in re.split(r"[\s,;，；]+", raw.strip()):
        app_id = item.strip()
        if app_id and app_id not in seen:
            seen.add(app_id)
            app_ids.append(app_id)
    return app_ids


@dataclass
class Settings:
    key_id: str = ""
    issuer_id: str = ""
    p8_path: str = ""
    app_id: str = ""
    bundle_id: str = ""
    refresh_seconds: int = 300
    sound_enabled: bool = True
    demo_mode: bool = False


class ReviewMonitorApp:
    def __init__(self) -> None:
        self.root = tk.Tk()
        self.root.title(APP_NAME)
        self.root.geometry("1040x680")
        self.root.minsize(920, 620)

        self.config_path = data_dir() / "settings.json"
        self.log_path = data_dir() / "monitor.log"
        self.settings = self.load_settings()
        self.events: queue.Queue[tuple[str, object]] = queue.Queue()
        self.stop_event = threading.Event()
        self.worker: threading.Thread | None = None
        self.running = False
        self.last_categories: dict[str, str] = {}
        self.app_rows: dict[str, str] = {}
        self.latest_state = "未开始"
        self.next_check_at = 0.0
        self.started_at = 0.0

        self.build_ui()
        self.apply_settings()
        self.root.after(200, self.drain_events)
        self.root.after(500, self.tick)

    def load_settings(self) -> Settings:
        if not self.config_path.exists():
            return Settings()
        try:
            data = json.loads(self.config_path.read_text("utf-8"))
            return Settings(**{**asdict(Settings()), **data})
        except Exception:
            return Settings()

    def save_settings(self) -> None:
        try:
            refresh = max(30, int(self.refresh_var.get().strip() or "300"))
        except ValueError:
            refresh = 300
        self.settings = Settings(
            key_id=self.key_id_var.get().strip(),
            issuer_id=self.issuer_id_var.get().strip(),
            p8_path=self.p8_path_var.get().strip(),
            app_id=self.app_ids_text.get("1.0", "end").strip(),
            bundle_id=self.bundle_id_var.get().strip(),
            refresh_seconds=refresh,
            sound_enabled=bool(self.sound_var.get()),
            demo_mode=bool(self.demo_var.get()),
        )
        self.config_path.write_text(json.dumps(asdict(self.settings), ensure_ascii=False, indent=2), "utf-8")

    def build_ui(self) -> None:
        self.root.configure(bg="#f4f6f8")
        style = ttk.Style()
        style.configure("Title.TLabel", font=("", 22, "bold"))
        style.configure("Status.TLabel", font=("", 28, "bold"))
        style.configure("Metric.TLabel", font=("", 15, "bold"))
        style.configure("Primary.TButton", font=("", 12, "bold"))

        self.root.columnconfigure(0, weight=1)
        self.root.rowconfigure(1, weight=1)

        header = ttk.Frame(self.root, padding=(18, 16, 18, 8))
        header.grid(row=0, column=0, sticky="ew")
        header.columnconfigure(0, weight=1)
        ttk.Label(header, text=APP_NAME, style="Title.TLabel").grid(row=0, column=0, sticky="w")
        ttk.Label(header, text="监控 App Store Connect 后台状态：等待审核不提示，正在审核和审核完成播放不同提示音。").grid(row=1, column=0, sticky="w", pady=(4, 0))

        body = ttk.Frame(self.root, padding=(18, 8, 18, 18))
        body.grid(row=1, column=0, sticky="nsew")
        body.columnconfigure(0, weight=0)
        body.columnconfigure(1, weight=1)
        body.rowconfigure(3, weight=1)

        config = ttk.LabelFrame(body, text="连接配置", padding=12)
        config.grid(row=0, column=0, rowspan=4, sticky="nsw", padx=(0, 14))
        config.columnconfigure(0, weight=1)

        self.key_id_var = tk.StringVar()
        self.issuer_id_var = tk.StringVar()
        self.p8_path_var = tk.StringVar()
        self.bundle_id_var = tk.StringVar()
        self.refresh_var = tk.StringVar()
        self.sound_var = tk.BooleanVar()
        self.demo_var = tk.BooleanVar()

        self.add_entry(config, "Key ID", self.key_id_var, 0)
        self.add_entry(config, "Issuer ID", self.issuer_id_var, 2)

        ttk.Label(config, text=".p8 私钥文件").grid(row=4, column=0, sticky="w", pady=(8, 3))
        p8_row = ttk.Frame(config)
        p8_row.grid(row=5, column=0, sticky="ew")
        p8_row.columnconfigure(0, weight=1)
        self.p8_label = ttk.Label(p8_row, textvariable=self.p8_path_var, width=34)
        self.p8_label.grid(row=0, column=0, sticky="ew", padx=(0, 8))
        ttk.Button(p8_row, text="选择文件...", command=self.choose_p8).grid(row=0, column=1)

        ttk.Label(config, text="App ID（可填多个，用逗号或换行分隔）").grid(row=6, column=0, sticky="w", pady=(8, 3))
        app_id_frame = ttk.Frame(config)
        app_id_frame.grid(row=7, column=0, sticky="ew")
        app_id_frame.columnconfigure(0, weight=1)
        self.app_ids_text = tk.Text(app_id_frame, height=4, width=42, wrap="word")
        self.app_ids_text.grid(row=0, column=0, sticky="ew")
        app_scroll = ttk.Scrollbar(app_id_frame, command=self.app_ids_text.yview)
        app_scroll.grid(row=0, column=1, sticky="ns")
        self.app_ids_text.configure(yscrollcommand=app_scroll.set)

        self.add_entry(config, "Bundle ID（备用：只监控一个 App）", self.bundle_id_var, 8)
        self.add_entry(config, "检查间隔（秒，至少 30）", self.refresh_var, 10)

        ttk.Checkbutton(config, text="启用提示音", variable=self.sound_var).grid(row=12, column=0, sticky="w", pady=(10, 0))
        ttk.Checkbutton(config, text="演示模式（不连接 Apple）", variable=self.demo_var).grid(row=13, column=0, sticky="w", pady=(6, 0))

        tests = ttk.Frame(config)
        tests.grid(row=14, column=0, sticky="ew", pady=(12, 0))
        tests.columnconfigure((0, 1), weight=1)
        ttk.Button(tests, text="测试正在审核音", command=lambda: play_sound("in_review")).grid(row=0, column=0, sticky="ew", padx=(0, 6))
        ttk.Button(tests, text="测试完成音", command=lambda: play_sound("done")).grid(row=0, column=1, sticky="ew", padx=(6, 0))

        actions = ttk.Frame(config)
        actions.grid(row=15, column=0, sticky="ew", pady=(14, 0))
        actions.columnconfigure((0, 1), weight=1)
        self.start_btn = ttk.Button(actions, text="开始监控", style="Primary.TButton", command=self.start)
        self.start_btn.grid(row=0, column=0, sticky="ew", padx=(0, 6))
        self.stop_btn = ttk.Button(actions, text="停止", command=self.stop, state="disabled")
        self.stop_btn.grid(row=0, column=1, sticky="ew", padx=(6, 0))

        ttk.Button(config, text="保存配置", command=self.save_settings).grid(row=16, column=0, sticky="ew", pady=(10, 0))

        summary = ttk.LabelFrame(body, text="审核状态", padding=16)
        summary.grid(row=0, column=1, sticky="ew")
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
        for col, (label, var) in enumerate([("版本", self.version_var), ("上次检查", self.checked_var), ("下次检查", self.next_var)]):
            ttk.Label(summary, text=label).grid(row=1, column=col, sticky="w", pady=(18, 3))
            ttk.Label(summary, textvariable=var, style="Metric.TLabel").grid(row=2, column=col, sticky="w")
        ttk.Label(summary, text="运行时长").grid(row=3, column=0, sticky="w", pady=(12, 3))
        ttk.Label(summary, textvariable=self.running_var, style="Metric.TLabel").grid(row=4, column=0, sticky="w")

        apps_frame = ttk.LabelFrame(body, text="监控 App", padding=8)
        apps_frame.grid(row=2, column=1, sticky="nsew", pady=(0, 12))
        apps_frame.rowconfigure(0, weight=1)
        apps_frame.columnconfigure(0, weight=1)
        self.apps_tree = ttk.Treeview(
            apps_frame,
            columns=("app_id", "version", "status", "raw", "checked"),
            show="headings",
            height=7,
        )
        for column, title, width in [
            ("app_id", "App ID", 130),
            ("version", "版本", 90),
            ("status", "状态", 150),
            ("raw", "原始状态", 190),
            ("checked", "检查时间", 90),
        ]:
            self.apps_tree.heading(column, text=title)
            self.apps_tree.column(column, width=width, anchor="w")
        self.apps_tree.grid(row=0, column=0, sticky="nsew")
        apps_scroll = ttk.Scrollbar(apps_frame, command=self.apps_tree.yview)
        apps_scroll.grid(row=0, column=1, sticky="ns")
        self.apps_tree.configure(yscrollcommand=apps_scroll.set)

        rules = ttk.LabelFrame(body, text="提示规则", padding=12)
        rules.grid(row=1, column=1, sticky="ew", pady=(12, 12))
        ttk.Label(rules, text="等待审核：只更新界面和日志，不播放声音。").grid(row=0, column=0, sticky="w")
        ttk.Label(rules, text="正在审核：第一次进入 IN_REVIEW 时播放短促上升提示音。").grid(row=1, column=0, sticky="w", pady=(5, 0))
        ttk.Label(rules, text="已完成：进入等待发布、Apple 发布处理或 Ready for Sale 时播放完成提示音。").grid(row=2, column=0, sticky="w", pady=(5, 0))

        log_frame = ttk.LabelFrame(body, text="运行日志", padding=8)
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
        self.key_id_var.set(self.settings.key_id)
        self.issuer_id_var.set(self.settings.issuer_id)
        self.p8_path_var.set(self.settings.p8_path or "尚未选择 .p8 文件")
        self.app_ids_text.delete("1.0", "end")
        self.app_ids_text.insert("1.0", self.settings.app_id)
        self.bundle_id_var.set(self.settings.bundle_id)
        self.refresh_var.set(str(self.settings.refresh_seconds))
        self.sound_var.set(self.settings.sound_enabled)
        self.demo_var.set(self.settings.demo_mode)

    def choose_p8(self) -> None:
        path = filedialog.askopenfilename(
            title="选择 App Store Connect API .p8 私钥",
            filetypes=[("Apple API 私钥", "*.p8"), ("所有文件", "*.*")],
        )
        if path:
            self.p8_path_var.set(path)

    def start(self) -> None:
        self.save_settings()
        if not self.settings.demo_mode:
            missing = []
            if not self.settings.key_id:
                missing.append("Key ID")
            if not self.settings.issuer_id:
                missing.append("Issuer ID")
            if not self.settings.p8_path or self.settings.p8_path == "尚未选择 .p8 文件":
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
        app_count = len(parse_app_ids(self.settings.app_id)) or (1 if self.settings.bundle_id else 0)
        self.log(f"开始监控，App 数量：{app_count}")
        self.worker = threading.Thread(target=self.worker_loop, daemon=True)
        self.worker.start()

    def stop(self) -> None:
        self.stop_event.set()
        self.running = False
        self.start_btn.configure(state="normal")
        self.stop_btn.configure(state="disabled")
        self.next_var.set("--")
        self.log("已停止")

    def worker_loop(self) -> None:
        client = AppStoreConnectClient(
            ASCConfig(
                key_id=self.settings.key_id,
                issuer_id=self.settings.issuer_id,
                p8_path=self.settings.p8_path,
                app_id=self.settings.app_id,
                bundle_id=self.settings.bundle_id,
            )
        )
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
                        result = {"app_id": app_id, "version": "Demo 1.0", "state": state}
                        self.events.put(("state", result))
                else:
                    if app_ids:
                        for app_id in app_ids:
                            try:
                                self.events.put(("state", client.latest_ios_version(app_id)))
                            except Exception as exc:
                                self.events.put(("error", f"App {app_id}：{exc}"))
                    else:
                        self.events.put(("state", client.latest_ios_version()))
            except Exception as exc:
                self.events.put(("error", exc))

            wait_seconds = max(30, self.settings.refresh_seconds)
            self.next_check_at = time.time() + wait_seconds
            self.events.put(("next", self.next_check_at))
            if self.stop_event.wait(wait_seconds):
                break

    def drain_events(self) -> None:
        while True:
            try:
                kind, payload = self.events.get_nowait()
            except queue.Empty:
                break
            if kind == "state":
                self.handle_state(payload)  # type: ignore[arg-type]
            elif kind == "error":
                self.log(f"错误：{payload}")
                self.checked_var.set(datetime.now().strftime("%H:%M:%S"))
            elif kind == "next":
                self.next_var.set(datetime.fromtimestamp(float(payload)).strftime("%H:%M:%S"))
        self.root.after(200, self.drain_events)

    def handle_state(self, result: dict[str, str]) -> None:
        app_id = str(result.get("app_id") or "--")
        state = result.get("state", "UNKNOWN")
        category = classify_state(state)
        label = status_text(state)
        version = result.get("version", "--")
        checked_at = datetime.now().strftime("%H:%M:%S")
        self.state_var.set(f"{app_id}：{label}")
        self.version_var.set(version)
        self.checked_var.set(checked_at)
        self.update_app_row(app_id, version, label, state, checked_at)
        self.log(f"App {app_id} 状态：{label}（{state}），版本：{version}")
        self.paint_status(category)

        if category != self.last_categories.get(app_id):
            if category == "in_review":
                self.log(f"提示：App {app_id} 进入正在审核")
                if self.settings.sound_enabled:
                    play_sound("in_review")
            elif category == "done":
                self.log(f"提示：App {app_id} 审核流程已完成")
                if self.settings.sound_enabled:
                    play_sound("done")
        self.last_categories[app_id] = category

    def update_app_row(self, app_id: str, version: str, label: str, state: str, checked_at: str) -> None:
        values = (app_id, version, label, state, checked_at)
        row_id = self.app_rows.get(app_id)
        if row_id and self.apps_tree.exists(row_id):
            self.apps_tree.item(row_id, values=values)
            return
        row_id = self.apps_tree.insert("", "end", values=values)
        self.app_rows[app_id] = row_id

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
        if self.running:
            elapsed = int(time.time() - self.started_at)
            self.running_var.set(f"{elapsed // 3600:02d}:{(elapsed // 60) % 60:02d}:{elapsed % 60:02d}")
            if self.next_check_at:
                remain = max(0, int(self.next_check_at - time.time()))
                self.next_var.set(f"{remain // 60:02d}:{remain % 60:02d}")
        self.root.after(500, self.tick)

    def log(self, message: str) -> None:
        line = f"[{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}] {message}\n"
        self.log_path.parent.mkdir(parents=True, exist_ok=True)
        with self.log_path.open("a", encoding="utf-8") as fh:
            fh.write(line)
        self.log_text.configure(state="normal")
        self.log_text.insert("end", line)
        self.log_text.see("end")
        self.log_text.configure(state="disabled")

    def run(self) -> None:
        self.root.mainloop()


def main() -> None:
    ReviewMonitorApp().run()
