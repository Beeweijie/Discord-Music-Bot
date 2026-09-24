"""Windows tray dashboard for the Discord music bot."""

import json
import os
import socket
import subprocess
import sys
import threading
import queue
import tempfile
import time
import tkinter as tk
import atexit
import ctypes
from datetime import datetime
from pathlib import Path
from tkinter import messagebox, scrolledtext, ttk

import pystray
from PIL import Image, ImageDraw


PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))
from bot.process_runtime import (
    is_process_record_running,
    process_identity,
    read_process_record,
    terminate_process_record,
)
from bot.status import enqueue_control, write_status
from scripts.ui_protocol import SHOW_ACK, SHOW_REQUEST, bind_ui_server, notify_existing_ui
RUNTIME_DIR = PROJECT_ROOT / "runtime"
STATUS_FILE = RUNTIME_DIR / "status.json"
CONTROL_FILE = RUNTIME_DIR / "control.jsonl"
PID_FILE = RUNTIME_DIR / "bot.pid"
VOICE_MODERATION_CONFIG = PROJECT_ROOT / "config" / "voice_moderation.json"
LOG_DIR = PROJECT_ROOT / "logs"
BOT_LOG = LOG_DIR / "bot.log"
STARTUP_LOG = LOG_DIR / "startup.log"
LOG_TAIL_BYTES = 64 * 1024
VOICE_LEVEL_LABELS = {
    "low": "轻轻提醒",
    "normal": "普通提醒",
    "strict": "严格盯防",
}
VOICE_LEVEL_VALUES = {label: value for value, label in VOICE_LEVEL_LABELS.items()}
VOICE_MODEL_LABELS = {
    "small": "小耳朵 small",
    "medium": "认真听 medium",
}
VOICE_MODEL_VALUES = {label: value for value, label in VOICE_MODEL_LABELS.items()}
STARTUP_LINK = (
    Path(os.environ.get("APPDATA", ""))
    / "Microsoft"
    / "Windows"
    / "Start Menu"
    / "Programs"
    / "Startup"
    / "Discord Music Bot.lnk"
)
INSTALL_STARTUP = PROJECT_ROOT / "scripts" / "install_startup.ps1"
UNINSTALL_STARTUP = PROJECT_ROOT / "scripts" / "uninstall_startup.ps1"

DISPLAY_MODES = {
    "1080p": {"geometry": "980x720", "minsize": (900, 660), "scale": 1.0},
    "2K": {"geometry": "1220x840", "minsize": (1080, 760), "scale": 1.25},
    "4K": {"geometry": "1580x1040", "minsize": (1320, 900), "scale": 1.65},
}

JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE = 0x00002000
JobObjectExtendedLimitInformation = 9


class JOBOBJECT_BASIC_LIMIT_INFORMATION(ctypes.Structure):
    _fields_ = [
        ("PerProcessUserTimeLimit", ctypes.c_int64),
        ("PerJobUserTimeLimit", ctypes.c_int64),
        ("LimitFlags", ctypes.c_uint32),
        ("MinimumWorkingSetSize", ctypes.c_size_t),
        ("MaximumWorkingSetSize", ctypes.c_size_t),
        ("ActiveProcessLimit", ctypes.c_uint32),
        ("Affinity", ctypes.c_size_t),
        ("PriorityClass", ctypes.c_uint32),
        ("SchedulingClass", ctypes.c_uint32),
    ]


class IO_COUNTERS(ctypes.Structure):
    _fields_ = [
        ("ReadOperationCount", ctypes.c_uint64),
        ("WriteOperationCount", ctypes.c_uint64),
        ("OtherOperationCount", ctypes.c_uint64),
        ("ReadTransferCount", ctypes.c_uint64),
        ("WriteTransferCount", ctypes.c_uint64),
        ("OtherTransferCount", ctypes.c_uint64),
    ]


class JOBOBJECT_EXTENDED_LIMIT_INFORMATION(ctypes.Structure):
    _fields_ = [
        ("BasicLimitInformation", JOBOBJECT_BASIC_LIMIT_INFORMATION),
        ("IoInfo", IO_COUNTERS),
        ("ProcessMemoryLimit", ctypes.c_size_t),
        ("JobMemoryLimit", ctypes.c_size_t),
        ("PeakProcessMemoryUsed", ctypes.c_size_t),
        ("PeakJobMemoryUsed", ctypes.c_size_t),
    ]


def write_offline_status():
    write_status(state="offline", current_song=None, queue_size=0, music_sessions={})


def python_works(candidate: Path | str) -> bool:
    try:
        result = subprocess.run(
            [str(candidate), "--version"],
            capture_output=True,
            text=True,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
            timeout=5,
        )
        return result.returncode == 0
    except Exception:
        return False


def find_python() -> str:
    for candidate in [
        PROJECT_ROOT / ".venv" / "Scripts" / "python.exe",
        PROJECT_ROOT / "venv" / "Scripts" / "python.exe",
    ]:
        if candidate.exists() and python_works(candidate):
            return str(candidate)
    return sys.executable


def create_icon(color: tuple[int, int, int]) -> Image.Image:
    image = Image.new("RGBA", (64, 64), (0, 0, 0, 0))
    draw = ImageDraw.Draw(image)
    draw.rounded_rectangle((8, 8, 56, 56), radius=14, fill=color)
    draw.ellipse((22, 20, 30, 28), fill="white")
    draw.ellipse((34, 20, 42, 28), fill="white")
    draw.arc((20, 24, 44, 46), start=15, end=165, fill="white", width=4)
    return image


class TrayApp:
    def __init__(self, initial_show: bool = False, server_socket=None):
        RUNTIME_DIR.mkdir(parents=True, exist_ok=True)
        LOG_DIR.mkdir(parents=True, exist_ok=True)

        self.process: subprocess.Popen | None = None
        self.server_socket = server_socket
        self.ui_actions = queue.Queue()
        self.operation_lock = threading.RLock()
        self.operation_busy = False
        self.pending_control_id = None
        self.refresh_job = None
        self.job_handle = self.create_shutdown_job()
        self.shutting_down = False
        atexit.register(self.shutdown_cleanup)

        self.root = tk.Tk()
        self.root.title("喵酱音乐 Bot 控制台")
        self.root.configure(bg="#f4f6fb")
        self.root.protocol("WM_DELETE_WINDOW", self.hide_window)

        self.resolution_var = tk.StringVar(value="4K")
        self.status_var = tk.StringVar(value="正在读取")
        self.status_detail_var = tk.StringVar(value="正在检查 Bot 状态...")
        self.user_var = tk.StringVar(value="-")
        self.guild_var = tk.StringVar(value="-")
        self.latency_var = tk.StringVar(value="-")
        self.pid_var = tk.StringVar(value="-")
        self.updated_var = tk.StringVar(value="-")
        self.startup_var = tk.StringVar(value="未知")
        self.selected_session_var = tk.StringVar(value="未选择服务器")
        self.control_result_var = tk.StringVar(value="")
        self.volume_var = tk.StringVar(value="40")
        self.loop_mode_var = tk.StringVar(value="off")
        self.voice_moderation_enabled_var = tk.BooleanVar(value=False)
        self.voice_moderation_level_var = tk.StringVar(value=VOICE_LEVEL_LABELS["normal"])
        self.voice_moderation_model_var = tk.StringVar(value=VOICE_MODEL_LABELS["small"])
        self.active_session_var = tk.StringVar(value="-")
        self.total_queue_var = tk.StringVar(value="-")
        self.listener_total_var = tk.StringVar(value="-")
        self.selected_session_id: str | None = None
        self.load_voice_moderation_config()

        self.icon_online = create_icon((36, 155, 88))
        self.icon_offline = create_icon((130, 130, 130))

        self.apply_resolution()
        self.build_window()
        if not initial_show:
            self.root.withdraw()

        self.tray_icon = pystray.Icon(
            "discord_music_bot",
            self.icon_offline,
            "Discord Music Bot",
            menu=pystray.Menu(
                pystray.MenuItem("打开控制台", self.tray_callback(self.show_window), default=True),
                pystray.MenuItem("启动 Bot", self.tray_callback(self.start_bot)),
                pystray.MenuItem("停止 Bot", self.tray_callback(self.stop_bot)),
                pystray.MenuItem("重启 Bot", self.tray_callback(self.restart_bot)),
                pystray.MenuItem("开关自启动", self.tray_callback(self.toggle_startup)),
                pystray.MenuItem("打开日志", self.tray_callback(self.open_log)),
                pystray.MenuItem("退出托盘", self.tray_callback(self.exit_app)),
            ),
        )

    def tray_callback(self, callback):
        # pystray and the socket server have their own threads. Only the Tk
        # event loop is allowed to touch widgets, variables or root.after().
        return lambda *_: self.ui_actions.put(callback)

    def pump_ui_actions(self):
        if self.shutting_down:
            return
        for _ in range(100):
            try:
                callback = self.ui_actions.get_nowait()
            except queue.Empty:
                break
            try:
                callback()
            except Exception as exc:
                messagebox.showerror("Discord Music Bot", str(exc))
        if not self.shutting_down:
            self.root.after(100, self.pump_ui_actions)

    def run_background(self, operation):
        if self.operation_busy or self.shutting_down:
            return
        self.operation_busy = True

        def finish(error=None):
            self.operation_busy = False
            if error:
                self.show_window()
                messagebox.showerror("Discord Music Bot", str(error))
            self.refresh_status()

        def work():
            error = None
            try:
                with self.operation_lock:
                    if not self.shutting_down:
                        operation()
            except Exception as exc:
                error = exc
            self.ui_actions.put(lambda: finish(error))

        threading.Thread(target=work, daemon=True).start()

    def build_window(self):
        style = ttk.Style()
        style.theme_use("clam")
        style.configure("TFrame", background="#f4f6fb")
        style.configure("Title.TLabel", background="#f4f6fb", foreground="#172033", font=("Segoe UI", 20, "bold"))
        style.configure("Subtle.TLabel", background="#f4f6fb", foreground="#667085", font=("Segoe UI", 10))
        style.configure("Treeview", rowheight=34, font=("Segoe UI", 10), background="#ffffff", fieldbackground="#ffffff")
        style.configure("Treeview.Heading", font=("Segoe UI", 10, "bold"), background="#eef2f7", foreground="#172033")

        self.scroll_canvas = tk.Canvas(self.root, bg="#f4f6fb", highlightthickness=0)
        self.scrollbar = ttk.Scrollbar(
            self.root,
            orient="vertical",
            command=self.scroll_canvas.yview,
        )
        self.scroll_canvas.configure(yscrollcommand=self.scrollbar.set)
        self.scrollbar.pack(side="right", fill="y")
        self.scroll_canvas.pack(side="left", fill="both", expand=True)

        frame = ttk.Frame(self.scroll_canvas, padding=18)
        self.scroll_window = self.scroll_canvas.create_window(
            (0, 0),
            window=frame,
            anchor="nw",
        )
        frame.bind("<Configure>", self.update_scroll_region)
        self.scroll_canvas.bind("<Configure>", self.resize_scroll_window)
        self.root.bind_all("<MouseWheel>", self.on_mouse_wheel)

        header = ttk.Frame(frame)
        header.pack(fill="x", pady=(0, 14))
        header.columnconfigure(0, weight=1)

        title_area = ttk.Frame(header)
        title_area.grid(row=0, column=0, sticky="w")
        ttk.Label(title_area, text="喵酱音乐 Bot 控制台", style="Title.TLabel").pack(anchor="w")
        ttk.Label(title_area, text="查看每个服务器的播放状态，不需要打开黑窗口。", style="Subtle.TLabel").pack(anchor="w", pady=(3, 0))

        display_area = ttk.Frame(header)
        display_area.grid(row=0, column=1, sticky="e")
        ttk.Label(display_area, text="显示模式", style="Subtle.TLabel").pack(anchor="e")
        ttk.OptionMenu(
            display_area,
            self.resolution_var,
            self.resolution_var.get(),
            *DISPLAY_MODES.keys(),
            command=lambda *_: self.apply_resolution(),
        ).pack(anchor="e", pady=(4, 0))

        status_card = self.card(frame)
        status_card.pack(fill="x", pady=(0, 12))
        self.status_dot = tk.Canvas(status_card, width=14, height=14, bg="#ffffff", highlightthickness=0)
        self.status_dot.grid(row=0, column=0, rowspan=2, sticky="n", pady=6, padx=(0, 10))
        self.status_dot_id = self.status_dot.create_oval(2, 2, 12, 12, fill="#9ca3af", outline="")
        tk.Label(status_card, textvariable=self.status_var, bg="#ffffff", fg="#172033", font=("Segoe UI", 16, "bold")).grid(row=0, column=1, sticky="w")
        tk.Label(status_card, textvariable=self.status_detail_var, bg="#ffffff", fg="#667085", font=("Segoe UI", 10)).grid(row=1, column=1, sticky="w", pady=(2, 0))
        status_card.columnconfigure(1, weight=1)

        sessions_card = self.card(frame, "服务器播放状态")
        sessions_card.pack(fill="both", expand=True, pady=(0, 12))
        self.build_session_table(sessions_card)

        lower = ttk.Frame(frame)
        lower.pack(fill="both")
        lower.columnconfigure(0, weight=3)
        lower.columnconfigure(1, weight=2)

        metrics_card = self.card(lower, "运行数据")
        metrics_card.grid(row=0, column=0, sticky="nsew", padx=(0, 12))
        self.build_metrics(metrics_card)

        control_card = self.card(lower, "控制")
        control_card.grid(row=0, column=1, sticky="nsew")
        self.build_controls(control_card)

        log_card = self.card(frame, "最近日志")
        log_card.pack(fill="both", expand=True, pady=(12, 0))
        self.log_text = scrolledtext.ScrolledText(
            log_card,
            height=8,
            wrap="word",
            font=("Consolas", 9),
            bg="#0f172a",
            fg="#dbeafe",
            insertbackground="#dbeafe",
            relief="flat",
        )
        self.log_text.pack(fill="both", expand=True, pady=(8, 0))

    def card(self, parent, title: str | None = None):
        card = tk.Frame(parent, bg="#ffffff", padx=14, pady=12, highlightthickness=1, highlightbackground="#e5e7eb")
        if title:
            tk.Label(card, text=title, bg="#ffffff", fg="#172033", font=("Segoe UI", 11, "bold")).pack(anchor="w")
        return card

    def build_session_table(self, parent):
        columns = ("server", "channel", "state", "song", "listeners", "queue", "volume", "loop")
        self.session_tree = ttk.Treeview(parent, columns=columns, show="headings", height=9)
        headings = {
            "server": "服务器",
            "channel": "语音频道",
            "state": "状态",
            "song": "当前歌曲",
            "listeners": "人数",
            "queue": "队列",
            "volume": "音量",
            "loop": "循环",
        }
        widths = {
            "server": 200,
            "channel": 170,
            "state": 90,
            "song": 560,
            "listeners": 70,
            "queue": 80,
            "volume": 80,
            "loop": 80,
        }
        for column in columns:
            self.session_tree.heading(column, text=headings[column])
            self.session_tree.column(column, width=widths[column], anchor="w", stretch=True)
        self.session_tree.pack(fill="both", expand=True, pady=(8, 0))
        self.session_tree.bind("<<TreeviewSelect>>", self.on_session_select)

    def build_metrics(self, parent):
        grid = tk.Frame(parent, bg="#ffffff")
        grid.pack(fill="both", expand=True, pady=(8, 0))
        metrics = [
            ("Bot 名称", self.user_var),
            ("服务器数", self.guild_var),
            ("延迟", self.latency_var),
            ("活跃语音", self.active_session_var),
            ("听众人数", self.listener_total_var),
            ("总队列", self.total_queue_var),
            ("进程 PID", self.pid_var),
            ("最后更新", self.updated_var),
            ("开机自启动", self.startup_var),
        ]
        for index, (label, value) in enumerate(metrics):
            row = index // 2
            col = index % 2
            item = tk.Frame(grid, bg="#f8fafc", padx=10, pady=8, highlightthickness=1, highlightbackground="#eef2f7")
            item.grid(row=row, column=col, sticky="nsew", padx=(0, 8 if col == 0 else 0), pady=(0, 8))
            tk.Label(item, text=label, bg="#f8fafc", fg="#667085", font=("Segoe UI", 9)).pack(anchor="w")
            tk.Label(item, textvariable=value, bg="#f8fafc", fg="#172033", font=("Segoe UI", 11, "bold"), wraplength=220, justify="left").pack(anchor="w", pady=(2, 0))
        grid.columnconfigure(0, weight=1)
        grid.columnconfigure(1, weight=1)

    def build_controls(self, parent):
        buttons = tk.Frame(parent, bg="#ffffff")
        buttons.pack(fill="x", pady=(8, 0))
        self.button(buttons, "启动 Bot", self.start_bot, "#2563eb", "#ffffff").pack(fill="x", pady=(0, 8))
        self.button(buttons, "停止 Bot", self.stop_bot, "#ef4444", "#ffffff").pack(fill="x", pady=(0, 8))
        self.button(buttons, "重启 Bot", self.restart_bot, "#f59e0b", "#111827").pack(fill="x", pady=(0, 8))
        self.button(buttons, "刷新状态", self.refresh_status, "#e5e7eb", "#111827").pack(fill="x", pady=(0, 8))
        self.button(buttons, "打开日志", self.open_log, "#e5e7eb", "#111827").pack(fill="x", pady=(0, 8))
        self.button(buttons, "开关自启动", self.toggle_startup, "#e5e7eb", "#111827").pack(fill="x")

        tk.Label(buttons, textvariable=self.selected_session_var, bg="#ffffff", fg="#667085", font=("Segoe UI", 9)).pack(fill="x", pady=(14, 6))
        tk.Label(buttons, textvariable=self.control_result_var, bg="#ffffff", fg="#667085", wraplength=280, justify="left").pack(fill="x", pady=(0, 6))
        self.button(buttons, "选中服务器：暂停 / 继续", lambda: self.send_control("pause_resume"), "#dbeafe", "#111827").pack(fill="x", pady=(0, 8))
        self.button(buttons, "选中服务器：跳过", lambda: self.send_control("skip"), "#dbeafe", "#111827").pack(fill="x", pady=(0, 8))
        self.button(buttons, "选中服务器：清空队列", lambda: self.send_control("clear"), "#dbeafe", "#111827").pack(fill="x", pady=(0, 8))
        self.button(buttons, "选中服务器：停止", lambda: self.send_control("stop"), "#fee2e2", "#111827").pack(fill="x", pady=(0, 8))
        volume_row = tk.Frame(buttons, bg="#ffffff")
        volume_row.pack(fill="x")
        tk.Entry(volume_row, textvariable=self.volume_var, width=8).pack(side="left", fill="x", expand=True)
        self.button(volume_row, "设置音量", lambda: self.send_control("volume"), "#e5e7eb", "#111827").pack(side="left", padx=(8, 0))
        loop_row = tk.Frame(buttons, bg="#ffffff")
        loop_row.pack(fill="x", pady=(8, 0))
        ttk.OptionMenu(
            loop_row,
            self.loop_mode_var,
            self.loop_mode_var.get(),
            "off",
            "one",
            "queue",
        ).pack(side="left", fill="x", expand=True)
        self.button(loop_row, "设置循环", lambda: self.send_control("loop"), "#e5e7eb", "#111827").pack(side="left", padx=(8, 0))

        voice_box = tk.Frame(buttons, bg="#f8fafc", padx=10, pady=8, highlightthickness=1, highlightbackground="#eef2f7")
        voice_box.pack(fill="x", pady=(14, 0))
        tk.Label(voice_box, text="坏话雷达喵", bg="#f8fafc", fg="#172033", font=("Segoe UI", 10, "bold")).pack(anchor="w")
        tk.Checkbutton(
            voice_box,
            text="开启文明提醒喵",
            variable=self.voice_moderation_enabled_var,
            bg="#f8fafc",
            fg="#111827",
            activebackground="#f8fafc",
            selectcolor="#ffffff",
        ).pack(anchor="w", pady=(6, 4))

        level_row = tk.Frame(voice_box, bg="#f8fafc")
        level_row.pack(fill="x", pady=(0, 6))
        tk.Label(level_row, text="力度", bg="#f8fafc", fg="#667085", width=6, anchor="w").pack(side="left")
        ttk.OptionMenu(
            level_row,
            self.voice_moderation_level_var,
            self.voice_moderation_level_var.get(),
            *VOICE_LEVEL_LABELS.values(),
        ).pack(side="left", fill="x", expand=True)

        model_row = tk.Frame(voice_box, bg="#f8fafc")
        model_row.pack(fill="x", pady=(0, 8))
        tk.Label(model_row, text="耳朵", bg="#f8fafc", fg="#667085", width=6, anchor="w").pack(side="left")
        ttk.OptionMenu(
            model_row,
            self.voice_moderation_model_var,
            self.voice_moderation_model_var.get(),
            *VOICE_MODEL_LABELS.values(),
        ).pack(side="left", fill="x", expand=True)

        self.button(voice_box, "保存提醒设置喵~", self.save_voice_moderation_config, "#e5e7eb", "#111827").pack(fill="x")

    def button(self, parent, text: str, command, bg: str, fg: str):
        return tk.Button(
            parent,
            text=text,
            command=command,
            bg=bg,
            fg=fg,
            activebackground=bg,
            activeforeground=fg,
            relief="flat",
            font=("Segoe UI", 10, "bold"),
            padx=12,
            pady=9,
            cursor="hand2",
        )

    def apply_resolution(self):
        mode = DISPLAY_MODES.get(self.resolution_var.get(), DISPLAY_MODES["2K"])
        self.root.tk.call("tk", "scaling", mode["scale"])
        width, height = (int(part) for part in mode["geometry"].split("x"))
        width = min(width, max(640, self.root.winfo_screenwidth() - 60))
        height = min(height, max(480, self.root.winfo_screenheight() - 100))
        self.root.geometry(f"{width}x{height}")
        self.root.minsize(min(width, mode["minsize"][0]), min(height, mode["minsize"][1]))

    def update_scroll_region(self, *_):
        self.scroll_canvas.configure(scrollregion=self.scroll_canvas.bbox("all"))

    def resize_scroll_window(self, event):
        self.scroll_canvas.itemconfigure(self.scroll_window, width=event.width)

    def on_mouse_wheel(self, event):
        self.scroll_canvas.yview_scroll(int(-1 * (event.delta / 120)), "units")

    def read_status(self) -> dict:
        if not STATUS_FILE.exists():
            return {}
        try:
            status = json.loads(STATUS_FILE.read_text(encoding="utf-8"))
            return status if isinstance(status, dict) else {}
        except Exception:
            return {}

    def read_pid(self) -> int | None:
        record = read_process_record(PID_FILE)
        return record["pid"] if record else None

    def read_voice_moderation_config(self) -> dict:
        default_config = {
            "enabled": False,
            "level": "normal",
            "model": "small",
        }
        if not VOICE_MODERATION_CONFIG.exists():
            return default_config
        try:
            config = json.loads(VOICE_MODERATION_CONFIG.read_text(encoding="utf-8"))
            default_config.update(
                {
                    "enabled": bool(config.get("enabled", default_config["enabled"])),
                    "level": str(config.get("level", default_config["level"])),
                    "model": str(config.get("model", default_config["model"])),
                }
            )
        except Exception:
            pass
        return default_config

    def load_voice_moderation_config(self):
        config = self.read_voice_moderation_config()
        self.voice_moderation_enabled_var.set(bool(config.get("enabled")))
        level = config.get("level") if config.get("level") in {"low", "normal", "strict"} else "normal"
        model = config.get("model") if config.get("model") in {"small", "medium"} else "small"
        self.voice_moderation_level_var.set(VOICE_LEVEL_LABELS[level])
        self.voice_moderation_model_var.set(VOICE_MODEL_LABELS[model])

    def save_voice_moderation_config(self):
        level = VOICE_LEVEL_VALUES.get(self.voice_moderation_level_var.get(), "normal")
        model = VOICE_MODEL_VALUES.get(self.voice_moderation_model_var.get(), "small")
        config = {
            "enabled": bool(self.voice_moderation_enabled_var.get()),
            "level": level,
            "model": model,
        }
        try:
            VOICE_MODERATION_CONFIG.parent.mkdir(parents=True, exist_ok=True)
            fd, filename = tempfile.mkstemp(dir=VOICE_MODERATION_CONFIG.parent, suffix=".tmp")
            try:
                with os.fdopen(fd, "w", encoding="utf-8") as stream:
                    json.dump(config, stream, ensure_ascii=False, indent=2)
                os.replace(filename, VOICE_MODERATION_CONFIG)
            finally:
                if os.path.exists(filename):
                    os.unlink(filename)
        except OSError as exc:
            messagebox.showerror("Discord Music Bot", f"保存提醒设置失败：{exc}")
            return
        self.refresh_status()

    def is_pid_running(self, pid: int | None) -> bool:
        record = read_process_record(PID_FILE)
        return bool(record and record["pid"] == pid and is_process_record_running(record))

    def startup_enabled(self) -> bool:
        return STARTUP_LINK.exists()

    def run_powershell_script(self, script_path: Path):
        result = subprocess.run(
            ["powershell.exe", "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass", "-File", str(script_path)],
            cwd=PROJECT_ROOT,
            capture_output=True,
            text=True,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
            timeout=30,
        )
        if result.returncode:
            raise RuntimeError(result.stderr.strip() or result.stdout.strip() or "自启动设置失败。")

    def toggle_startup(self, *_):
        self.run_background(lambda: self.run_powershell_script(
            UNINSTALL_STARTUP if self.startup_enabled() else INSTALL_STARTUP
        ))

    def state_label(self, state: str, running: bool) -> tuple[str, str, str]:
        if running and state == "online":
            return "Bot 正在运行", "可以正常接收 Discord 指令和播放音乐。", "#22c55e"
        if running and state == "starting":
            return "Bot 正在启动", "正在连接 Discord，请稍等几秒。", "#f59e0b"
        if running:
            return "Bot 进程存在", "程序正在运行，状态文件还没有更新。", "#f59e0b"
        return "Bot 未运行", "点击“启动 Bot”即可开始使用。", "#9ca3af"

    def session_state_label(self, session: dict) -> str:
        if session.get("is_paused"):
            return "暂停中"
        if session.get("is_playing") or session.get("current_song"):
            return "播放中"
        if session.get("is_connected"):
            return "待机"
        return "未连接"

    def compatible_sessions(self, status: dict) -> dict:
        sessions = status.get("music_sessions") or {}
        if isinstance(sessions, dict) and sessions:
            return {str(key): value for key, value in sessions.items() if isinstance(value, dict)}

        current_song = status.get("current_song")
        if current_song:
            return {
                "legacy": {
                    "guild_name": "当前服务器",
                    "channel_name": "-",
                    "current_song": current_song,
                    "requester": status.get("requester"),
                    "queue_size": status.get("queue_size", 0),
                    "volume": status.get("volume", "-"),
                    "is_connected": True,
                    "is_playing": True,
                }
            }
        return {}

    def create_shutdown_job(self):
        if os.name != "nt":
            return None

        try:
            kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
            kernel32.CreateJobObjectW.argtypes = [ctypes.c_void_p, ctypes.c_wchar_p]
            kernel32.CreateJobObjectW.restype = ctypes.c_void_p
            kernel32.SetInformationJobObject.argtypes = [
                ctypes.c_void_p,
                ctypes.c_int,
                ctypes.c_void_p,
                ctypes.c_uint32,
            ]
            kernel32.CloseHandle.argtypes = [ctypes.c_void_p]
            kernel32.CloseHandle.restype = ctypes.c_int
            kernel32.SetInformationJobObject.restype = ctypes.c_int

            job_handle = kernel32.CreateJobObjectW(None, None)
            if not job_handle:
                return None

            limits = JOBOBJECT_EXTENDED_LIMIT_INFORMATION()
            limits.BasicLimitInformation.LimitFlags = JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
            ok = kernel32.SetInformationJobObject(
                job_handle,
                JobObjectExtendedLimitInformation,
                ctypes.byref(limits),
                ctypes.sizeof(limits),
            )
            if not ok:
                kernel32.CloseHandle(job_handle)
                return None
            return job_handle
        except Exception:
            return None

    def attach_process_to_shutdown_job(self, process: subprocess.Popen):
        if os.name != "nt" or not self.job_handle:
            return

        try:
            kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
            kernel32.AssignProcessToJobObject.argtypes = [ctypes.c_void_p, ctypes.c_void_p]
            kernel32.AssignProcessToJobObject.restype = ctypes.c_int
            if not kernel32.AssignProcessToJobObject(self.job_handle, ctypes.c_void_p(int(process._handle))):
                raise ctypes.WinError(ctypes.get_last_error())
        except Exception as exc:
            with STARTUP_LOG.open("a", encoding="utf-8") as log:
                log.write(f"Could not attach bot to shutdown job: {exc}\n")

    def close_shutdown_job(self):
        if os.name != "nt" or not self.job_handle:
            return

        try:
            kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
            kernel32.CloseHandle.argtypes = [ctypes.c_void_p]
            kernel32.CloseHandle.restype = ctypes.c_int
            kernel32.CloseHandle(self.job_handle)
        except Exception:
            pass
        self.job_handle = None

    def refresh_session_table(self, sessions: dict):
        selected_id = self.selected_session_id
        for item in self.session_tree.get_children():
            self.session_tree.delete(item)

        if not sessions:
            self.session_tree.insert("", "end", values=("暂无服务器在播放", "-", "-", "-", "-", "-", "-", "-"))
            self.selected_session_id = None
            self.selected_session_var.set("未选择服务器")
            return

        for session_id, session in sorted(
            sessions.items(),
            key=lambda item: (str(item[1].get("guild_name") or ""), str(item[1].get("channel_name") or "")),
        ):
            song = session.get("current_song") or "-"
            requester = session.get("requester")
            if requester and song != "-":
                song = f"{song}（by {requester}）"
            self.session_tree.insert(
                "",
                "end",
                iid=session_id,
                values=(
                    session.get("guild_name") or "-",
                    session.get("channel_name") or "-",
                    self.session_state_label(session),
                    song,
                    f"{session.get('human_count', 0)} 人",
                    f"{session.get('queue_size', 0)} 首",
                    f"{session.get('volume', '-')}%",
                    session.get("loop_mode") or "off",
                ),
            )
        if selected_id and self.session_tree.exists(selected_id):
            self.session_tree.selection_set(selected_id)
            self.session_tree.focus(selected_id)
        elif self.selected_session_id and not self.session_tree.exists(self.selected_session_id):
            self.selected_session_id = None
            self.selected_session_var.set("未选择服务器")

    def on_session_select(self, *_):
        selection = self.session_tree.selection()
        if not selection:
            return

        item_id = selection[0]
        values = self.session_tree.item(item_id, "values")
        if not values or values[0].startswith("暂无"):
            self.selected_session_id = None
            self.selected_session_var.set("未选择服务器")
            return

        self.selected_session_id = item_id
        server = values[0] if len(values) > 0 else "-"
        channel = values[1] if len(values) > 1 else "-"
        volume = values[6] if len(values) > 6 else ""
        digits = "".join(ch for ch in str(volume) if ch.isdigit())
        if digits:
            self.volume_var.set(digits)
        if len(values) > 7 and values[7] in {"off", "one", "queue"}:
            self.loop_mode_var.set(values[7])
        self.selected_session_var.set(f"已选择：{server} / {channel}")

    def send_control(self, action: str):
        if not self.selected_session_id:
            messagebox.showwarning("Discord Music Bot", "请先在服务器表格中选择一行。")
            return

        if not is_process_record_running(read_process_record(PID_FILE)):
            messagebox.showwarning("Discord Music Bot", "Bot 未运行，请先启动。")
            return

        command = {
            "session_id": self.selected_session_id,
            "action": action,
            "created_at": datetime.now().astimezone().isoformat(),
        }

        if action == "volume":
            try:
                volume = int(self.volume_var.get().strip())
            except ValueError:
                messagebox.showwarning("Discord Music Bot", "音量请输入 0 到 100 的数字。")
                return
            if volume < 0 or volume > 100:
                messagebox.showwarning("Discord Music Bot", "音量请输入 0 到 100 的数字。")
                return
            command["volume"] = volume
        elif action == "loop":
            mode = self.loop_mode_var.get().strip().lower()
            if mode not in {"off", "one", "queue"}:
                messagebox.showwarning("Discord Music Bot", "循环模式只能是 off、one 或 queue。")
                return
            command["mode"] = mode

        try:
            self.pending_control_id = enqueue_control(command)
            self.control_result_var.set("控制命令已发送，等待 Bot 确认…")
            self.refresh_status()
        except Exception as exc:
            messagebox.showerror("Discord Music Bot", f"写入控制命令失败：{exc}")

    def start_bot(self, *_):
        self.run_background(self._start_bot)

    def _start_bot(self):
        if is_process_record_running(read_process_record(PID_FILE)):
            return
        if self.process is not None and self.process.poll() is None:
            return

        env = os.environ.copy()
        env["PYTHONUTF8"] = "1"
        env["PYTHONIOENCODING"] = "utf-8"

        LOG_DIR.mkdir(parents=True, exist_ok=True)
        creationflags = getattr(subprocess, "CREATE_NO_WINDOW", 0) | getattr(
            subprocess,
            "BELOW_NORMAL_PRIORITY_CLASS",
            0,
        )
        with BOT_LOG.open("ab") as log_file:
            self.process = subprocess.Popen(
                [find_python(), "-u", "main.py"],
                cwd=PROJECT_ROOT,
                stdout=log_file,
                stderr=subprocess.STDOUT,
                env=env,
                creationflags=creationflags,
            )

        self.attach_process_to_shutdown_job(self.process)
        with STARTUP_LOG.open("a", encoding="utf-8") as log:
            log.write(f"Started bot from tray with PID {self.process.pid}\n")
        # Surface bad credentials/import errors even when launched hidden.
        try:
            result = self.process.wait(timeout=1)
        except subprocess.TimeoutExpired:
            return
        if result:
            raise RuntimeError(f"Bot 启动失败（退出码 {result}），请查看最近日志。")

    def stop_bot(self, *_, refresh: bool = True):
        self.run_background(self._stop_bot)

    def _stop_bot(self):
        record = read_process_record(PID_FILE)
        if is_process_record_running(record):
            if record.get("legacy"):
                raise RuntimeError("检测到旧版 Bot 进程。请在原启动窗口停止一次，再用此控制台启动。")
            if not terminate_process_record(record):
                raise RuntimeError("无法确认 Bot 已停止，状态保留。请查看进程权限或日志。")
        # A just-spawned child may not have written its identity record yet.
        if self.process is not None and self.process.poll() is None:
            self.process.terminate()
            self.process.wait(timeout=5)
        self.process = None
        # Closing our job also cleans up ffmpeg/yt-dlp descendants.
        self.close_shutdown_job()
        if not self.shutting_down:
            self.job_handle = self.create_shutdown_job()
        if not is_process_record_running(read_process_record(PID_FILE)):
            write_offline_status()

    def restart_bot(self, *_):
        def restart():
            self._stop_bot()
            self._start_bot()
        self.run_background(restart)

    def show_window(self, *_):
        self.root.deiconify()
        self.root.lift()
        self.refresh_status()

    def hide_window(self):
        self.root.withdraw()

    def open_log(self, *_):
        BOT_LOG.touch(exist_ok=True)
        os.startfile(BOT_LOG)

    def read_recent_log_lines(self) -> list[str]:
        if not BOT_LOG.exists():
            return []

        with BOT_LOG.open("rb") as log_file:
            log_file.seek(0, os.SEEK_END)
            size = log_file.tell()
            log_file.seek(max(0, size - LOG_TAIL_BYTES))
            data = log_file.read()

        return data.decode("utf-8", errors="replace").splitlines()[-80:]

    def exit_app(self, *_):
        if self.operation_busy:
            self.root.after(200, self.exit_app)
            return
        try:
            with self.operation_lock:
                self._stop_bot()
        except Exception as exc:
            messagebox.showerror("Discord Music Bot", str(exc))
            return
        self.shutdown_cleanup()
        self.tray_icon.stop()
        self.root.destroy()

    def shutdown_cleanup(self):
        if self.shutting_down:
            return
        self.shutting_down = True
        try:
            with self.operation_lock:
                self._stop_bot()
        except Exception:
            pass
        try:
            self.close_shutdown_job()
        except Exception:
            pass
        try:
            if self.server_socket:
                self.server_socket.close()
        except Exception:
            pass

    def refresh_status(self):
        status = self.read_status()
        pid = self.read_pid()
        running = self.is_pid_running(pid)
        sessions = self.compatible_sessions(status)

        state = status.get("state") or "unknown"
        if not running:
            state = "offline"

        title, detail, color = self.state_label(state, running)
        self.status_var.set(title)
        self.status_detail_var.set(detail)
        self.status_dot.itemconfig(self.status_dot_id, fill=color)
        self.user_var.set(status.get("bot_user") or "-")
        self.guild_var.set(str(status.get("guild_count", "-")))
        latency = status.get("latency_ms")
        self.latency_var.set(f"{latency} ms" if latency is not None else "-")
        self.active_session_var.set(str(len(sessions)))
        self.listener_total_var.set(str(sum(int(session.get("human_count") or 0) for session in sessions.values())))
        self.total_queue_var.set(str(sum(int(session.get("queue_size") or 0) for session in sessions.values())))
        self.pid_var.set(str(pid) if running and pid else "-")
        self.updated_var.set(self.format_time(status.get("updated_at")))
        self.startup_var.set("已开启" if self.startup_enabled() else "未开启")
        self.refresh_session_table(sessions)

        self.tray_icon.icon = self.icon_online if running else self.icon_offline
        self.tray_icon.title = f"Discord Music Bot - {title}"

        if BOT_LOG.exists():
            lines = self.read_recent_log_lines()
            self.log_text.delete("1.0", tk.END)
            self.log_text.insert(tk.END, "\n".join(lines))
            self.log_text.see(tk.END)

        if self.refresh_job is not None:
            try:
                self.root.after_cancel(self.refresh_job)
            except tk.TclError:
                pass
        self.refresh_job = self.root.after(5000, self.refresh_status)

    def format_time(self, value: str | None) -> str:
        if not value:
            return "-"
        try:
            return datetime.fromisoformat(value.replace("Z", "+00:00")).astimezone().strftime("%Y-%m-%d %H:%M:%S")
        except Exception:
            return value

    def run(self):
        self.start_command_server()
        self.start_bot()
        self.refresh_status()
        threading.Thread(target=self.tray_icon.run, daemon=True).start()
        self.root.mainloop()

    def start_command_server(self):
        def server():
            try:
                self.server_socket = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
                self.server_socket.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
                self.server_socket.bind((UI_HOST, UI_PORT))
                self.server_socket.listen(5)
            except OSError:
                return

            while True:
                try:
                    conn, _ = self.server_socket.accept()
                    with conn:
                        command = conn.recv(32).decode("utf-8", errors="ignore")
                    if command.strip().lower() == "show":
                        self.show_window()
                except OSError:
                    return
                except Exception:
                    continue

        threading.Thread(target=server, daemon=True).start()


if __name__ == "__main__":
    try:
        if notify_existing_ui():
            raise SystemExit
        TrayApp(initial_show="--show" in sys.argv).run()
    except Exception as exc:
        messagebox.showerror("Discord Music Bot Tray", str(exc))
