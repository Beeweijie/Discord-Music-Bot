"""Dependency-free supervisor: restart updates and restore failed releases."""
import importlib.util
import json
import logging
import os
import re
import subprocess
import sys
import time
from pathlib import Path

logger = logging.getLogger("updater")
UPDATE_EXIT = 75


class LauncherLock:
    def __init__(self, path):
        self.path = path
        self.stream = None

    def acquire(self):
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
        self.stream = stream
        return True

    def release(self):
        if self.stream:
            self.stream.close()
            self.stream = None


def load_updater(root):
    """Use the pre-update recovery copy if code replacement was interrupted."""
    pending = root / "runtime/updates/pending.json"
    if pending.is_file():
        request = json.loads(pending.read_text(encoding="utf-8"))
        job_id = request.get("id", "")
        if not re.fullmatch(r"[a-f0-9]{32}", job_id):
            raise ValueError("Invalid pending update identity")
        recovery = root / "runtime/updates" / job_id / "recovery.py"
        if not recovery.resolve().is_relative_to(root.resolve()) or not recovery.is_file():
            raise ValueError("Missing update recovery helper")
        spec = importlib.util.spec_from_file_location("bot_update_recovery", recovery)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        return module.GitHubUpdater(root)
    from bot_app.infrastructure.updates import GitHubUpdater
    return GitHubUpdater(root)


def launch(root, python, args, job_id=None):
    environment = os.environ.copy()
    environment.pop("PYTHONPATH", None)
    environment.pop("PYTHONHOME", None)
    environment["BOT_SUPERVISED"] = "1"
    environment.pop("BOT_UPDATE_ID", None)
    if job_id:
        environment["BOT_UPDATE_ID"] = job_id
    return subprocess.Popen([str(python), "-u", "-m", "bot_app.main", *args], cwd=root, env=environment,
                            stdout=sys.stdout, stderr=sys.stderr,
                            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))


def wait_ready(process, marker, timeout=90):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if process.poll() is not None:
            return False
        if marker.is_file():
            return True
        time.sleep(0.1)
    return False


def stop_child(process):
    if process.poll() is None:
        process.terminate()
        try:
            process.wait(timeout=15)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait(timeout=10)


def supervise(root, args=None, fallback=None, readiness_timeout=90):
    root = Path(root).resolve()
    args = list(sys.argv[1:] if args is None else args)
    fallback = fallback or sys.executable
    updater = load_updater(root)
    if "--check" in args:
        return launch(root, updater.selected_python(fallback), args).wait()
    lock = LauncherLock(root / "runtime/launcher.lock")
    if not lock.acquire():
        return 0
    process = None
    try:
        if updater.pending.exists():
            request = json.loads(updater.pending.read_text(encoding="utf-8"))
            journal = json.loads((updater.job(request["id"]) / "journal.json").read_text(encoding="utf-8")) if (
                updater.job(request["id"]) / "journal.json").exists() else {}
            if journal.get("phase") == "committed":
                updater.finish(request, True)
            else:
                updater.rollback(request)
                updater.finish(request, False)
                logger.warning("Recovered an interrupted update; restored the previous release")
        python = updater.selected_python(fallback)
        process = launch(root, python, args)
        while True:
            code = process.wait()
            if code != UPDATE_EXIT:
                return code
            if not updater.pending.exists():
                logger.error("Bot requested update restart without a prepared update")
                return 1
            request = json.loads(updater.pending.read_text(encoding="utf-8"))
            old_python = python
            try:
                python = updater.apply(request)
                process = launch(root, python, args, request["id"])
                if not wait_ready(process, updater.job(request["id"]) / "ready.json", readiness_timeout):
                    stop_child(process)
                    raise RuntimeError("The updated bot did not become ready before the startup deadline")
                updater.finish(request, True)
                logger.info("Update committed: %s", request["version"])
            except Exception:
                logger.exception("Update failed; restoring the previous release")
                if process.poll() is None:
                    stop_child(process)
                updater.rollback(request)
                updater.finish(request, False)
                python = old_python
                process = launch(root, python, args)
    finally:
        if process is not None:
            stop_child(process)
        lock.release()


def main():
    root = Path(__file__).resolve().parent.parent
    log = root / "logs/update.log"
    log.parent.mkdir(parents=True, exist_ok=True)
    handler = logging.FileHandler(log, encoding="utf-8")
    handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(message)s"))
    logger.addHandler(handler)
    logger.setLevel(logging.INFO)
    try:
        return supervise(root)
    except KeyboardInterrupt:
        return 0
    except Exception:
        logger.exception("Supervisor failed; pending recovery data was retained")
        return 1
    finally:
        logger.removeHandler(handler)
        handler.close()
