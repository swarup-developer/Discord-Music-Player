from __future__ import annotations

import atexit
import ctypes
import logging
import os
from pathlib import Path

from bot.app import build_bot
from bot.config import DISCORD_TOKEN
from bot.opus_bootstrap import ensure_opus_loaded

logger = logging.getLogger(__name__)
LOCK_FILE = Path(__file__).resolve().with_name("dc.lock")


def _process_is_running(pid: int) -> bool:
    if pid <= 0:
        return False
    import platform
    if platform.system() == "Windows":
        try:
            import ctypes
            kernel32 = ctypes.windll.kernel32
            PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
            handle = kernel32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
            if handle:
                kernel32.CloseHandle(handle)
                return True
        except Exception:
            pass
        return False
    else:
        try:
            os.kill(pid, 0)
        except OSError:
            return False
        return True


def _acquire_single_instance_lock() -> None:
    if LOCK_FILE.exists():
        try:
            existing_pid = int(LOCK_FILE.read_text(encoding="utf-8").strip())
        except Exception:
            existing_pid = -1
        if _process_is_running(existing_pid):
            raise SystemExit(
                f"Another bot instance is already running (PID {existing_pid}). "
                "Stop that process before starting this copy."
            )
        try:
            LOCK_FILE.unlink()
        except Exception:
            pass

    LOCK_FILE.write_text(str(os.getpid()), encoding="utf-8")

    def _release_lock() -> None:
        try:
            if LOCK_FILE.exists():
                LOCK_FILE.unlink()
        except Exception:
            pass

    atexit.register(_release_lock)


def main() -> None:
    if not DISCORD_TOKEN:
        raise ValueError("Missing DISCORD_TOKEN in environment variables")
    if not ensure_opus_loaded():
        logger.warning("Opus library could not be loaded automatically. Voice mode will stay disabled.")
    _acquire_single_instance_lock()
    build_bot().run(DISCORD_TOKEN, log_handler=None)


if __name__ == "__main__":
    main()
