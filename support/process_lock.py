from __future__ import annotations

from contextlib import contextmanager
import os
from pathlib import Path
import time
from typing import BinaryIO, Iterator


def _try_lock(handle: BinaryIO) -> bool:
    handle.seek(0)
    if os.name == "nt":
        import msvcrt

        try:
            msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
            return True
        except OSError:
            return False

    import fcntl

    try:
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        return True
    except BlockingIOError:
        return False


def _unlock(handle: BinaryIO) -> None:
    handle.seek(0)
    if os.name == "nt":
        import msvcrt

        msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
        return

    import fcntl

    fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


@contextmanager
def interprocess_file_lock(
    path: str | Path,
    *,
    label: str,
    poll_seconds: float = 0.5,
    notice_seconds: float = 10.0,
) -> Iterator[Path]:
    """Serialize access to a shared external project across Python processes."""
    lock_path = Path(path).resolve()
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    with lock_path.open("a+b") as handle:
        handle.seek(0, os.SEEK_END)
        if handle.tell() == 0:
            handle.write(b"\0")
            handle.flush()

        started = time.monotonic()
        next_notice = started
        while not _try_lock(handle):
            now = time.monotonic()
            if now >= next_notice:
                waited = int(now - started)
                print(f"[{label}] 共享工程正被另一项目使用，已等待 {waited} 秒...", flush=True)
                next_notice = now + max(1.0, notice_seconds)
            time.sleep(max(0.05, poll_seconds))

        waited = time.monotonic() - started
        if waited >= poll_seconds:
            print(f"[{label}] 已取得共享工程执行权，等待 {waited:.1f} 秒。", flush=True)
        try:
            yield lock_path
        finally:
            try:
                _unlock(handle)
            except OSError:
                pass
