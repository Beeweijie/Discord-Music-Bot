"""One-time bootstrap repair for installations with the venv symlink bug."""
import argparse
import importlib.util
import shutil
import uuid
from pathlib import Path

OLD_CHECK = "if not path.resolve().is_relative_to(expected) or not path.is_file():"
NEW_CHECK = "if not path.parent.resolve().is_relative_to(expected) or not path.is_file():"


def repair(root):
    root = Path(root).resolve()
    target = root / "bot_app/infrastructure/updates.py"
    if target.is_symlink() or not target.parent.resolve().is_relative_to(root):
        raise ValueError("The updater path must stay inside the installation.")
    source = target.read_text(encoding="utf-8")
    if OLD_CHECK not in source and NEW_CHECK in source:
        return None
    if source.count(OLD_CHECK) != 1:
        raise ValueError("The updater does not match the known bug; no files were changed.")
    patched = source.replace(OLD_CHECK, NEW_CHECK, 1)
    compile(patched, str(target), "exec")

    # Use the installation's dependency-free lock so a running supervisor cannot
    # keep the old validator in memory while the on-disk file is repaired.
    spec = importlib.util.spec_from_file_location("repair_launcher", root / "scripts/run_bot.py")
    launcher = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(launcher)
    lock = launcher.LauncherLock(root / "runtime/launcher.lock")
    if not lock.acquire():
        raise ValueError("Stop the bot's launcher/service before running this repair.")
    backup = target.with_name(target.name + ".before-linux-fix-" + uuid.uuid4().hex)
    temporary = target.with_name(target.name + ".linux-fix.tmp")
    try:
        shutil.copy2(target, backup)
        temporary.write_text(patched, encoding="utf-8")
        shutil.copymode(target, temporary)
        temporary.replace(target)
    finally:
        temporary.unlink(missing_ok=True)
        lock.release()
    return backup


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", required=True, type=Path, help="Existing bot installation directory")
    args = parser.parse_args()
    try:
        backup = repair(args.root)
    except (OSError, ValueError) as error:
        parser.exit(1, f"Repair failed: {error}\n")
    print(f"Updater repaired. Backup: {backup}" if backup else "Updater already has the fix.")
    print("Restart the usual bot launcher/service, then run !update in Discord.")
