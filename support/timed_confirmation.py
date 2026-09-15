"""Bounded console confirmation without a background stdin reader."""
from __future__ import annotations
import os
import sys
import time


def _poll_windows_pipe_line() -> str | None:
    """Consume exactly one complete pipe line, including CRLF, without blocking.

    Peek rather than readline: a readable pipe may contain only part of a line.
    No thread survives the confirmation and bytes for later menus stay unread.
    """
    import ctypes
    from ctypes import wintypes
    import msvcrt

    try:
        handle = wintypes.HANDLE(msvcrt.get_osfhandle(sys.stdin.fileno()))
    except (OSError, ValueError, AttributeError):
        return None
    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    peek = kernel.PeekNamedPipe
    peek.argtypes = [wintypes.HANDLE, wintypes.LPVOID, wintypes.DWORD,
                     ctypes.POINTER(wintypes.DWORD), ctypes.POINTER(wintypes.DWORD),
                     ctypes.POINTER(wintypes.DWORD)]
    peek.restype = wintypes.BOOL
    buffer = ctypes.create_string_buffer(65536)
    copied = wintypes.DWORD()
    if not peek(handle, buffer, len(buffer), ctypes.byref(copied), None, None):
        return None  # EOF, closed pipe, or a non-pipe redirected stream.
    data = buffer.raw[:copied.value]
    newline = data.find(b"\n")
    if newline < 0:
        return None
    read = kernel.ReadFile
    read.argtypes = [wintypes.HANDLE, wintypes.LPVOID, wintypes.DWORD,
                     ctypes.POINTER(wintypes.DWORD), wintypes.LPVOID]
    read.restype = wintypes.BOOL
    received = wintypes.DWORD()
    if not read(handle, buffer, newline + 1, ctypes.byref(received), None):
        return None
    return buffer.raw[:received.value].decode("utf-8", errors="replace").strip()


def confirm_dictionary_translation(timeout: float = 30) -> bool:
    print(f"[动态词库] 是否继续汉化？[Y/n]，{int(timeout)} 秒无应答自动允许（Y/N 可直接按键）。", flush=True)
    deadline = time.monotonic() + timeout
    shown = None
    while time.monotonic() < deadline:
        remaining = max(1, int(deadline - time.monotonic()) + 1)
        if remaining != shown and (remaining % 5 == 0 or remaining <= 3):
            print(f"[动态词库] 等待确认：剩余 {remaining} 秒……", flush=True)
            shown = remaining
        key = None
        if os.name == "nt" and sys.stdin.isatty():
            import msvcrt
            if msvcrt.kbhit():
                key = msvcrt.getwch()
                if key in ("\x00", "\xe0"):
                    msvcrt.getwch()
                    key = None
        elif os.name == "nt":
            key = _poll_windows_pipe_line()
        else:
            import select
            if select.select([sys.stdin], [], [], 0)[0]:
                key = sys.stdin.readline().strip()
        if key == "\x03":
            raise KeyboardInterrupt
        if key and key.lower() in ("y", "yes", "n", "no"):
            allowed = key.lower() in ("y", "yes")
            print("[动态词库] 已允许汉化。" if allowed else "[动态词库] 已跳过汉化，保留空译文字典。", flush=True)
            return allowed
        time.sleep(0.1)
    print(f"[动态词库] {timeout:g} 秒无有效应答，自动允许汉化。", flush=True)
    return True
