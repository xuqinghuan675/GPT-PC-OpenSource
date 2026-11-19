"""Bind a BrowserSkill-owned Agent Window to a named Windows virtual desktop.

BrowserSkill's Chrome window_id is not the native HWND. This module resizes
only the session's own Agent Window to a unique size, then matches the changed
native window. It never guesses by window title or moves another user's tab.
No permanent service or extra browser profile is created.
"""
from __future__ import annotations

import ctypes
from ctypes import wintypes
import hashlib
import os
from pathlib import Path
import secrets
import time
from typing import Callable


class WindowRouteError(RuntimeError):
    pass


EXE_NAMES = {"chrome": "chrome.exe", "edge": "msedge.exe"}
DLL_HASH = "8740c572a1c000e3b87ffeb1e4c397eae9af3bd4a2abdc3bcffacab4493f8ff5"


class RECT(ctypes.Structure):
    _fields_ = [("left", ctypes.c_long), ("top", ctypes.c_long),
                ("right", ctypes.c_long), ("bottom", ctypes.c_long)]


def _session() -> int:
    out = wintypes.DWORD()
    if not ctypes.windll.kernel32.ProcessIdToSessionId(os.getpid(), ctypes.byref(out)):
        raise WindowRouteError("Cannot resolve Windows session")
    return int(out.value)


def _process_name(pid: int) -> str:
    k = ctypes.windll.kernel32
    k.OpenProcess.restype = wintypes.HANDLE
    k.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
    k.QueryFullProcessImageNameW.argtypes = [
        wintypes.HANDLE, wintypes.DWORD, wintypes.LPWSTR, ctypes.POINTER(wintypes.DWORD)
    ]
    k.CloseHandle.argtypes = [wintypes.HANDLE]
    handle = k.OpenProcess(0x1000, False, int(pid))
    if not handle:
        return ""
    try:
        buf = ctypes.create_unicode_buffer(2048)
        size = wintypes.DWORD(len(buf))
        if not k.QueryFullProcessImageNameW(handle, 0, buf, ctypes.byref(size)):
            return ""
        return Path(buf.value).name.casefold()
    finally:
        k.CloseHandle(handle)


def _local_windows(browser: str, dll_path: Path) -> list[dict]:
    if _session() == 0:
        raise WindowRouteError("SSH session cannot enumerate the interactive desktop")
    if browser not in EXE_NAMES:
        raise WindowRouteError("Unsupported browser")
    if not dll_path.is_file() or hashlib.sha256(dll_path.read_bytes()).hexdigest() != DLL_HASH:
        raise WindowRouteError("Windows desktop DLL missing or checksum mismatch")
    get_desk = ctypes.WinDLL(str(dll_path)).GetWindowDesktopNumber
    get_desk.argtypes = [ctypes.c_void_p]
    get_desk.restype = ctypes.c_int
    u = ctypes.windll.user32
    found = []

    def cb(hwnd, _):
        if not u.IsWindowVisible(hwnd):
            return True
        pid = wintypes.DWORD()
        u.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
        if _process_name(pid.value) != EXE_NAMES[browser]:
            return True
        cls = ctypes.create_unicode_buffer(160)
        u.GetClassNameW(hwnd, cls, 159)
        if cls.value != "Chrome_WidgetWin_1":
            return True
        rect = RECT()
        if not u.GetWindowRect(hwnd, ctypes.byref(rect)):
            return True
        width = int(rect.right - rect.left)
        height = int(rect.bottom - rect.top)
        if width < 420 or height < 300:
            return True
        index = get_desk(ctypes.c_void_p(hwnd))
        found.append({
            "hwnd": int(hwnd), "pid": int(pid.value),
            "width": width, "height": height,
            "desktop_number": index + 1 if index >= 0 else None
        })
        return True

    callback_type = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)
    callback = callback_type(cb)
    if not u.EnumWindows(callback, 0):
        raise WindowRouteError("EnumWindows failed")
    return found


def windows(browser: str, desktop: Callable, dll_path: Path) -> list[dict]:
    if browser not in EXE_NAMES:
        raise WindowRouteError("Unsupported browser type")
    try:
        answer = desktop("windows", "--browser", browser)
        if answer.get("ok") and isinstance(answer.get("windows"), list):
            return answer["windows"]
    except Exception:
        pass
    return _local_windows(browser, dll_path)


def named_desktop(desktop: Callable, agent: str) -> int | None:
    state = desktop("status")
    details = state.get("broker") if "local_session" in state else state
    if not isinstance(details, dict) or not details.get("ok"):
        raise WindowRouteError("Interactive Windows desktop broker unavailable")
    for item in details.get("desktops", []):
        if item.get("name") == "AI-" + agent:
            return int(item["number"])
    return None


def unique_size(before: list[dict]) -> tuple[int, int]:
    sizes = [(int(x["width"]), int(x["height"])) for x in before]
    for _ in range(90):
        width = 1060 + secrets.randbelow(180)
        height = 700 + secrets.randbelow(160)
        if all(abs(width - w) > 4 or abs(height - h) > 4 for w, h in sizes):
            return width, height
    raise WindowRouteError("Cannot choose a unique agent window size")


def match_resized(before: list[dict], after: list[dict], width: int, height: int) -> dict | None:
    prior = {int(x["hwnd"]): x for x in before}
    matches = []
    for window in after:
        if abs(int(window["width"]) - width) > 2 or abs(int(window["height"]) - height) > 2:
            continue
        old = prior.get(int(window["hwnd"]))
        if old is not None and int(old["width"]) == int(window["width"]) and int(old["height"]) == int(window["height"]):
            continue
        matches.append(window)
    return matches[0] if len(matches) == 1 else None


def place_agent_window(agent: str, session_id: str, browser_window_id: int | None,
                       bsk: Callable, desktop: Callable, *, browser: str,
                       dll_path: Path, require_named_desktop: bool = False) -> dict:
    """Rehome only one confidently verified BrowserSkill-owned window."""
    target = named_desktop(desktop, agent)
    if target is None:
        if require_named_desktop:
            raise WindowRouteError("No AI desktop with name AI-" + agent)
        return {"status": "no_named_desktop", "moved": False, "agent": agent}
    if not session_id:
        raise WindowRouteError("BrowserSkill session required")
    sessions = bsk("session", "list", "--json")
    if not isinstance(sessions, list):
        sessions = sessions.get("sessions", [])
    record = next((x for x in sessions if x.get("session_id") == session_id), None)
    if record is None:
        raise WindowRouteError("BrowserSkill has no such active Agent Window")
    expected = record.get("agent_window_id")
    if browser_window_id is not None and int(expected or -1) != int(browser_window_id):
        raise WindowRouteError("BrowserSkill Agent Window ID mismatch")
    before = windows(browser, desktop, dll_path)
    width, height = unique_size(before)
    receipt = bsk("window", "resize", "--session", session_id,
                  "--width", str(width), "--height", str(height), "--json")
    if int(receipt.get("window_id") or -1) != int(expected or -2):
        raise WindowRouteError("Resize receipt does not belong to the Agent Window")
    matched = None
    for _ in range(14):
        after = windows(browser, desktop, dll_path)
        matched = match_resized(before, after, width, height)
        if matched is not None:
            break
        time.sleep(.1)
    if matched is None:
        raise WindowRouteError("No uniquely identified Win32 Agent Window; no other window moved")
    hwnd = int(matched["hwnd"])
    if matched.get("desktop_number") == target:
        return {"status": "already_on_desktop", "moved": False,
                "hwnd": hwnd, "desktop_number": target, "agent": agent}
    result = desktop("move", hex(hwnd), str(target))
    if not result.get("ok"):
        raise WindowRouteError("Windows move refused: " + str(result.get("error", ""))[:220])
    found = next(
        (x for x in windows(browser, desktop, dll_path)
         if int(x["hwnd"]) == hwnd and int(x["pid"]) == int(matched["pid"])),
        None
    )
    if found is None or found.get("desktop_number") != target:
        raise WindowRouteError("Windows desktop assignment could not be confirmed")
    return {"status": "moved", "moved": True, "hwnd": hwnd,
            "desktop_number": target, "agent": agent,
            "browser_window_id": int(expected)}
