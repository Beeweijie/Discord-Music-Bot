"""Process ownership and single-instance locks shared by the bot and tray.

A PID alone is never permission to terminate a process: Windows reuses PIDs.
Records also contain the OS process creation identity, checked on the same
handle used to terminate the process.
"""

import ctypes
import json
import os
import signal
import tempfile
import time
from pathlib import Path


def _kernel32():
    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    handle = ctypes.c_void_p
    dword = ctypes.c_uint32
    kernel.OpenProcess.argtypes = [dword, ctypes.c_int, dword]
    kernel.OpenProcess.restype = handle
    kernel.CloseHandle.argtypes = [handle]
    kernel.CloseHandle.restype = ctypes.c_int
    kernel.GetProcessTimes.argtypes = [handle] + [ctypes.POINTER(ctypes.c_uint64)] * 4
    kernel.GetProcessTimes.restype = ctypes.c_int
    kernel.WaitForSingleObject.argtypes = [handle, dword]
    kernel.WaitForSingleObject.restype = dword
    kernel.TerminateProcess.argtypes = [handle, dword]
    kernel.TerminateProcess.restype = ctypes.c_int
    return kernel


def _handle_identity(kernel, handle):
    if kernel.WaitForSingleObject(handle, 0) != 258:  # WAIT_TIMEOUT: still alive
        return None
    times = [ctypes.c_uint64() for _ in range(4)]
    if not kernel.GetProcessTimes(handle, *(ctypes.byref(value) for value in times)):
        return None
    return str(times[0].value)


def process_identity(pid: int) -> str | None:
    if not isinstance(pid, int) or isinstance(pid, bool) or pid <= 0:
        return None
    if os.name == "nt":
        kernel = _kernel32()
        handle = kernel.OpenProcess(0x1000 | 0x100000, False, pid)
        if not handle:
            return None
        try:
            return _handle_identity(kernel, handle)
        finally:
            kernel.CloseHandle(handle)
    try:
        # The process name may contain spaces or parentheses.
        fields = Path(f"/proc/{pid}/stat").read_text().rsplit(")", 1)[1].split()
        return None if fields[0] == "Z" else fields[19]
    except (OSError, IndexError):
        return None


def read_process_record(path: Path) -> dict | None:
    try:
        value = json.loads(Path(path).read_text(encoding="utf-8"))
        if isinstance(value, int) and not isinstance(value, bool) and value > 0:
            return {"pid": value, "legacy": True}
        if (isinstance(value, dict) and isinstance(value.get("pid"), int)
                and not isinstance(value["pid"], bool) and value["pid"] > 0
                and isinstance(value.get("created"), str) and value["created"]):
            return value
    except (OSError, ValueError, TypeError):
        pass
    return None


def write_process_record(path: Path) -> dict:
    path = Path(path)
    identity = process_identity(os.getpid())
    if identity is None:
        raise RuntimeError("Cannot establish bot process ownership.")
    record = {"pid": os.getpid(), "created": identity}
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(prefix=path.name + ".", suffix=".tmp", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            json.dump(record, stream)
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)
    return record


def is_process_record_running(record: dict | None) -> bool:
    if not isinstance(record, dict):
        return False
    identity = process_identity(record.get("pid"))
    return identity is not None and (record.get("legacy") is True or identity == record.get("created"))


def remove_process_record(path: Path, record: dict | None) -> bool:
    if not record or read_process_record(path) != record:
        return False
    try:
        Path(path).unlink()
        return True
    except FileNotFoundError:
        return False


def terminate_process_record(record: dict | None, timeout: float = 5) -> bool:
    """Return success only after a verified process has exited; never kill legacy PIDs."""
    if not record or record.get("legacy") or not record.get("created"):
        return False
    pid = record.get("pid")
    if not isinstance(pid, int) or isinstance(pid, bool) or pid <= 0 or pid == os.getpid():
        return False
    if os.name == "nt":
        kernel = _kernel32()
        handle = kernel.OpenProcess(0x1000 | 0x100000 | 0x0001, False, pid)
        if not handle:
            return not is_process_record_running(record)
        try:
            identity = _handle_identity(kernel, handle)
            if identity is None:
                return kernel.WaitForSingleObject(handle, 0) == 0
            if identity != record["created"]:
                return False
            if not kernel.TerminateProcess(handle, 1):
                return False
            return kernel.WaitForSingleObject(handle, max(0, int(timeout * 1000))) == 0
        finally:
            kernel.CloseHandle(handle)
    # Linux pidfds also keep process identity stable between checking and signaling.
    if not hasattr(os, "pidfd_open") or not hasattr(signal, "pidfd_send_signal"):
        return False
    try:
        descriptor = os.pidfd_open(pid)
    except ProcessLookupError:
        return True
    try:
        if not is_process_record_running(record):
            return False
        signal.pidfd_send_signal(descriptor, signal.SIGTERM)
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if not is_process_record_running(record):
                return True
            time.sleep(0.05)
        return not is_process_record_running(record)
    finally:
        os.close(descriptor)


class ProcessLock:
    """OS-backed, crash-safe advisory lock held for the owner's lifetime."""

    def __init__(self, path: Path):
        self.path = Path(path)
        self._stream = None

    def acquire(self) -> bool:
        if self._stream is not None:
            return True
        self.path.parent.mkdir(parents=True, exist_ok=True)
        stream = self.path.open("a+b")
        try:
            if os.name == "nt":
                import msvcrt
                if stream.seek(0, os.SEEK_END) == 0:
                    stream.write(b"\0")
                    stream.flush()
                stream.seek(0)
                msvcrt.locking(stream.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(stream.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            stream.close()
            return False
        self._stream = stream
        return True

    def release(self):
        stream, self._stream = self._stream, None
        if stream is None:
            return
        try:
            if os.name == "nt":
                import msvcrt
                stream.seek(0)
                msvcrt.locking(stream.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                import fcntl
                fcntl.flock(stream.fileno(), fcntl.LOCK_UN)
        finally:
            stream.close()
