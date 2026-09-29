"""Local OS process controls, independent of Tk widgets."""
import ctypes
import os
import subprocess
import sys
from pathlib import Path
from bot_app.infrastructure.paths import BASE_DIR as PROJECT_ROOT
from bot_app.infrastructure.processes.runtime import is_process_record_running, read_process_record, terminate_process_record
from bot_app.infrastructure.persistence.runtime import write_status

PID_FILE = PROJECT_ROOT / "runtime/bot.pid"
LOG_DIR = PROJECT_ROOT / "logs"
BOT_LOG = LOG_DIR / "bot.log"
STARTUP_LOG = LOG_DIR / "startup.log"
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


class DesktopProcessController:
    def __init__(self):
        self.process = None
        self.shutting_down = False
        self.job_handle = self.create_shutdown_job()

    def open_web_console(self):
        result = subprocess.run([find_python(), "-B", str(PROJECT_ROOT / "scripts/open_web_ui.py")],
                                cwd=PROJECT_ROOT, capture_output=True, text=True,
                                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0), timeout=10)
        if result.returncode:
            raise RuntimeError("无法打开浏览器控制台，请确认 Bot 已启动且管理 API 已启用。")

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
