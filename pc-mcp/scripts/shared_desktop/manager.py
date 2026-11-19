"""GPT-PC lightweight virtual desktops + one shared Chromium profile.

No resident service. No destructive operations. All commands are explicit.
Native desktop operations use the pinned VirtualDesktopAccessor DLL.
Agent browser windows use one Chrome profile and Chromium CDP.
"""
from __future__ import annotations

import argparse
import ctypes
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import time
from urllib.request import ProxyHandler, build_opener

BASE = Path(__file__).resolve().parent
DLL = BASE / "VirtualDesktopAccessor.dll"
DLL_SHA256 = "8740c572a1c000e3b87ffeb1e4c397eae9af3bd4a2abdc3bcffacab4493f8ff5"
PROFILE = Path(os.environ["LOCALAPPDATA"]) / "GPTPCSharedBrowser" / "ChromeProfile"
STATE = Path(os.environ["LOCALAPPDATA"]) / "GPTPCSharedBrowser" / "agent-windows.json"
HOST = "127.0.0.1"
PORT = 9336
ENDPOINT = f"http://{HOST}:{PORT}"


class DesktopError(RuntimeError):
    pass


def vda():
    if not DLL.is_file() or hashlib.sha256(DLL.read_bytes()).hexdigest() != DLL_SHA256:
        raise DesktopError("VirtualDesktopAccessor.dll missing or checksum mismatch")
    return ctypes.WinDLL(str(DLL))


def func(dll, name, *argtypes):
    f = getattr(dll, name)
    f.restype = ctypes.c_int
    f.argtypes = list(argtypes)
    return f


def checked(n, op):
    if n == -1:
        raise DesktopError(f"{op} failed on this Windows build")
    return n


def count(dll):
    return checked(func(dll, "GetDesktopCount")(), "GetDesktopCount")


def current(dll):
    return checked(func(dll, "GetCurrentDesktopNumber")(), "GetCurrentDesktopNumber")


def name(dll, index):
    buf = ctypes.create_string_buffer(1024)
    f = func(dll, "GetDesktopName", ctypes.c_int, ctypes.c_void_p, ctypes.c_size_t)
    result = f(index, ctypes.cast(buf, ctypes.c_void_p), len(buf))
    return buf.value.decode("utf-8", errors="replace") if result != -1 else ""


def desktop_status(dll):
    n = count(dll)
    return {
        "count": n,
        "current_number": current(dll) + 1,
        "desktops": [{"number": i + 1, "name": name(dll, i) or f"Desktop {i+1}"} for i in range(n)],
    }


def desktop_setup(dll, labels):
    if len(labels) < 1 or len(labels) > 8:
        raise DesktopError("Choose 1..8 desktop labels")
    n = count(dll)
    old_active = current(dll)
    if n > len(labels):
        raise DesktopError("Refusing to delete existing desktops; provide at least as many labels")
    if n > 1:
        existing = [name(dll, i) for i in range(n)]
        if any(x and not x.startswith("Desktop ") and x != labels[i] for i, x in enumerate(existing)):
            raise DesktopError("Existing named desktops differ; refusing to rename user's layout")
    create = func(dll, "CreateDesktop")
    rename = func(dll, "SetDesktopName", ctypes.c_int, ctypes.c_char_p)
    try:
        for i in range(n, len(labels)):
            checked(create(), "CreateDesktop")
            if count(dll) < i+1:
                raise DesktopError("Desktop count did not increase after creation")
        for i, label in enumerate(labels):
            checked(rename(i, label.encode("utf-8")), "SetDesktopName")
    finally:
        if current(dll) != old_active and old_active < count(dll):
            checked(func(dll, "GoToDesktopNumber", ctypes.c_int)(old_active), "RestoreDesktop")
    return desktop_status(dll)


def lazy_allocate(dll, agent: str, mode: str):
    """Allocate a visible Windows desktop only when visual GUI use is required.

    Most agent tasks use mode=background; web tasks use the existing
    Playwright Chrome extension, and neither allocates a Windows desktop.
    """
    if not agent or len(agent) > 50 or not agent.replace("-", "").replace("_", "").isalnum():
        raise DesktopError("Agent name must contain 1-50 letters/numbers/_/-")
    if mode not in {"background", "web", "visual"}:
        raise DesktopError("Mode must be background, web, or visual")
    if mode != "visual":
        return {
            "agent": agent, "mode": mode, "desktop_number": None,
            "desktop_created": False,
            "browser": "existing_chrome_extension" if mode == "web" else None,
            "note": "No GUI workspace or new browser process allocated",
        }

    # Different GPT calls may race; serialize only on-demand native creation.
    import msvcrt
    lock_path = BASE / ".lazy-desktop.lock"
    lock_path.touch(exist_ok=True)
    with lock_path.open("r+b") as lock_file:
        locked = False
        for _ in range(60):
            try:
                lock_file.seek(0)
                msvcrt.locking(lock_file.fileno(), msvcrt.LK_NBLCK, 1)
                locked = True
                break
            except OSError:
                time.sleep(0.1)
        if not locked:
            raise DesktopError("Visual desktop allocator busy; retry later")
        try:
            tag = f"AI-{agent}"
            for i in range(count(dll)):
                if name(dll, i) == tag:
                    return {
                        "agent": agent, "mode": mode, "desktop_number": i + 1,
                        "desktop_created": False, "desktop_reused": True,
                    }
            if count(dll) >= 6:
                raise DesktopError("Visual desktop limit reached (6); inspect existing desktops")
            before = count(dll)
            active = current(dll)
            try:
                checked(func(dll, "CreateDesktop")(), "CreateDesktop")
                if count(dll) != before + 1:
                    raise DesktopError("Windows virtual desktop count did not increase")
                checked(func(dll, "SetDesktopName", ctypes.c_int, ctypes.c_char_p)(
                    before, tag.encode("utf-8")
                ), "SetDesktopName")
            finally:
                if current(dll) != active and active < count(dll):
                    checked(func(dll, "GoToDesktopNumber", ctypes.c_int)(
                        active
                    ), "RestoreDesktop")
            return {
                "agent": agent, "mode": mode, "desktop_number": before + 1,
                "desktop_created": True, "desktop_reused": False,
                "note": "Only desktop container created; no browser/app started",
            }
        finally:
            lock_file.seek(0)
            msvcrt.locking(lock_file.fileno(), msvcrt.LK_UNLCK, 1)


def desktop_switch(dll, number):
    if not 1 <= number <= count(dll):
        raise DesktopError("Desktop number out of range")
    checked(func(dll, "GoToDesktopNumber", ctypes.c_int)(number-1), "GoToDesktopNumber")
    return desktop_status(dll)


def desktop_move(dll, hwnd, number):
    if not 1 <= number <= count(dll):
        raise DesktopError("Desktop number out of range")
    if not ctypes.windll.user32.IsWindow(hwnd):
        raise DesktopError("Window handle is no longer valid")
    checked(func(dll, "MoveWindowToDesktopNumber", ctypes.c_void_p, ctypes.c_int)(hwnd, number-1), "MoveWindowToDesktopNumber")
    return {"window": hwnd, "desktop_number": number, "status": "moved"}


def chrome_exe():
    for root in (os.environ.get("PROGRAMFILES"), os.environ.get("PROGRAMFILES(X86)")):
        if root:
            p = Path(root) / "Google" / "Chrome" / "Application" / "chrome.exe"
            if p.is_file():
                return p
    raise DesktopError("Chrome executable not found")


def owner_pid():
    import psutil
    expected = os.path.normcase(os.path.abspath(PROFILE))
    roots = []
    for process in psutil.process_iter(["name"]):
        try:
            if str(process.info.get("name") or "").lower() != "chrome.exe":
                continue
            argv = process.cmdline()
            if any(a.startswith("--type=") for a in argv):
                continue
            data_dir = None
            for i, val in enumerate(argv):
                if val.startswith("--user-data-dir="):
                    data_dir = val.partition("=")[2].strip('"')
                elif val == "--user-data-dir" and i + 1 < len(argv):
                    data_dir = argv[i + 1].strip('"')
            if data_dir and os.path.normcase(os.path.abspath(data_dir)) == expected:
                roots.append(process.pid)
        except (psutil.NoSuchProcess, psutil.AccessDenied):
            continue
    if len(roots) > 1:
        raise DesktopError("Multiple Chrome roots claim the managed profile")
    return roots[0] if roots else None


def cdp_ready():
    try:
        with build_opener(ProxyHandler({})).open(ENDPOINT + "/json/version", timeout=0.75) as response:
            data = json.loads(response.read(8192))
        return data.get("Browser", "unknown")
    except Exception:
        return None


def browser_status():
    root = owner_pid()
    return {"chrome_root_pid": root, "cdp_ready": bool(cdp_ready()), "profile_exists": PROFILE.is_dir(), "port": PORT}


def browser_start():
    status = browser_status()
    if status["cdp_ready"] and status["chrome_root_pid"]:
        return status
    if status["cdp_ready"]:
        raise DesktopError("CDP port is occupied by another browser")
    if status["chrome_root_pid"]:
        raise DesktopError("Managed Chrome profile is running without CDP; close that Chrome instance normally first")
    PROFILE.mkdir(parents=True, exist_ok=True)
    cmd = [str(chrome_exe()), f"--user-data-dir={PROFILE}",
           f"--remote-debugging-port={PORT}", "--remote-debugging-address=127.0.0.1",
           "--no-first-run", "--no-default-browser-check", "about:blank"]
    subprocess.Popen(cmd, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, close_fds=True)
    for _ in range(36):
        time.sleep(0.25)
        if cdp_ready() and owner_pid():
            return browser_status()
    raise DesktopError("Managed browser startup timed out; inspect Chrome window before retrying")


def chrome_windows(pid):
    import win32gui
    import win32process
    result = []
    def each(hwnd, _):
        try:
            _, window_pid = win32process.GetWindowThreadProcessId(hwnd)
            if window_pid == pid and win32gui.GetClassName(hwnd).startswith("Chrome_WidgetWin"):
                result.append(hwnd)
        except Exception:
            pass
    win32gui.EnumWindows(each, None)
    return set(result)


def existing_chrome_root():
    """Find exactly one root for Chrome's ordinary, already-running profile."""
    import psutil
    roots = []
    for process in psutil.process_iter(["name"]):
        try:
            if str(process.info.get("name") or "").lower() != "chrome.exe":
                continue
            argv = process.cmdline()
            if not any(x.startswith("--type=") for x in argv) and not any(
                x.startswith("--user-data-dir") for x in argv
            ):
                roots.append(process.pid)
        except (psutil.NoSuchProcess, psutil.AccessDenied):
            continue
    if len(roots) != 1:
        raise DesktopError(
            f"Expected one already running ordinary Chrome root; found {len(roots)}"
        )
    return roots[0]


def existing_chrome_profile():
    """Read only the last-used profile name; never read or copy login data."""
    state_file = Path(os.environ["LOCALAPPDATA"]) / "Google" / "Chrome" / "User Data" / "Local State"
    profile = "Default"
    if state_file.is_file():
        data = json.loads(state_file.read_text(encoding="utf-8"))
        profile = str(data.get("profile", {}).get("last_used") or "Default")
    if not profile or Path(profile).name != profile or "/" in profile or "\\" in profile:
        raise DesktopError("Invalid Chrome profile directory name")
    return profile


def existing_chrome_status():
    try:
        pid = existing_chrome_root()
    except DesktopError:
        pid = None
    try:
        profile = existing_chrome_profile()
    except (OSError, ValueError, KeyError, TypeError):
        profile = None
    return {"root_pid": pid, "last_used_profile": profile, "shares_existing_logins": True}


def existing_chrome_window(dll, agent, number, url):
    """Explicitly open a window in the user's existing profile, then move it.

    One-time window creation can briefly focus Chrome. Ongoing agent actions
    should use Playwright MCP extension background mode.
    """
    from urllib.parse import urlsplit
    if not 1 <= number <= count(dll):
        raise DesktopError("Desktop number out of range")
    if not agent or len(agent) > 50 or not agent.replace("-", "").replace("_", "").isalnum():
        raise DesktopError("Agent name must be 1-50 letters/numbers/_/-")
    if url != "about:blank" and urlsplit(url).scheme not in {"http", "https"}:
        raise DesktopError("Only http(s) URLs or about:blank are supported")
    state = load_state()
    if agent in state:
        raise DesktopError("Agent name is already in use")
    pid = existing_chrome_root()
    profile = existing_chrome_profile()
    before = chrome_windows(pid)
    subprocess.Popen(
        [str(chrome_exe()), f"--profile-directory={profile}", "--new-window", url],
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        close_fds=True,
    )
    found = set()
    for _ in range(50):
        found = chrome_windows(pid) - before
        if len(found) != 1:
            if len(found) > 1:
                break
            time.sleep(0.1)
        else:
            break
    if len(found) != 1:
        return {
            "agent": agent,
            "status": "window_unmapped",
            "note": "A Chrome window may have opened; inspect manually before retrying",
        }
    hwnd = next(iter(found))
    desktop_move(dll, hwnd, number)
    state[agent] = {
        "origin": "existing_chrome",
        "hwnd": hwnd,
        "desktop_number": number,
        "profile": profile,
    }
    save_state(state)
    return {"agent": agent, "window": hwnd, "desktop_number": number,
            "profile": profile, "status": "opened_existing_profile"}


def load_state():
    if not STATE.exists():
        return {}
    obj = json.loads(STATE.read_text("utf-8"))
    return obj if isinstance(obj, dict) else {}


def save_state(state):
    STATE.parent.mkdir(parents=True, exist_ok=True)
    temp = STATE.with_suffix(".new")
    temp.write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")
    os.replace(temp, STATE)


def browser_connect(p):
    root = owner_pid()
    if not root or not cdp_ready():
        raise DesktopError("Run browser-start first, then log in to the managed Chrome once")
    browser = p.chromium.connect_over_cdp(ENDPOINT, timeout=10000, no_defaults=True)
    if len(browser.contexts) != 1:
        raise DesktopError("Expected one persistent shared Chrome context")
    return browser, root


def browser_open(dll, agent, number, url):
    if not 1 <= number <= count(dll):
        raise DesktopError("Desktop number out of range")
    if not agent or len(agent) > 50 or not agent.replace("-", "").replace("_", "").isalnum():
        raise DesktopError("Agent name must be 1-50 letters/numbers/_/-")
    state = load_state()
    if agent in state:
        raise DesktopError("Agent already has a window; close it explicitly or use a new name")
    from playwright.sync_api import sync_playwright
    with sync_playwright() as p:
        browser, root = browser_connect(p)
        before = chrome_windows(root)
        session = browser.new_browser_cdp_session()
        response = session.send("Target.createTarget", {
            "url": url, "newWindow": True, "background": True, "focus": False,
        })
        target = response["targetId"]
        new_windows = set()
        for _ in range(35):
            new_windows = chrome_windows(root) - before
            if len(new_windows) == 1:
                break
            time.sleep(0.10)
        if len(new_windows) != 1:
            return {"agent": agent, "target_id": target, "status": "target_created_but_window_unmapped",
                    "note": "No window moved; inspect manually, then retry binding"}
        hwnd = next(iter(new_windows))
        desktop_move(dll, hwnd, number)
        state[agent] = {"target_id": target, "hwnd": hwnd, "desktop_number": number}
        save_state(state)
        return {"agent": agent, "target_id": target, "window": hwnd, "desktop_number": number, "status": "opened"}


def browser_screenshot(agent, output):
    state = load_state()
    if agent not in state:
        raise DesktopError("Agent window not registered")
    target_id = state[agent]["target_id"]
    from playwright.sync_api import sync_playwright
    with sync_playwright() as p:
        browser, _ = browser_connect(p)
        context = browser.contexts[0]
        for page in context.pages:
            session = context.new_cdp_session(page)
            info = session.send("Target.getTargetInfo")
            if info.get("targetInfo", {}).get("targetId") == target_id:
                output.parent.mkdir(parents=True, exist_ok=True)
                page.screenshot(path=str(output), timeout=10000)
                return {"agent": agent, "screenshot": str(output), "status": "saved"}
    raise DesktopError("Target page not found; it may have been closed")


def main():
    ap = argparse.ArgumentParser(description="Windows virtual desktop + shared Chrome agent controller")
    subs = ap.add_subparsers(dest="cmd", required=True)
    subs.add_parser("status")
    lazy = subs.add_parser("allocate")
    lazy.add_argument("--agent", required=True)
    lazy.add_argument("--mode", choices=["background", "web", "visual"], default="background")
    init = subs.add_parser("setup")
    init.add_argument("--names", nargs="+", default=["我的工作", "AI作业", "AI接单"])
    sw = subs.add_parser("switch")
    sw.add_argument("number", type=int)
    mv = subs.add_parser("move")
    mv.add_argument("hwnd", type=lambda x: int(x, 0))
    mv.add_argument("number", type=int)
    subs.add_parser("browser-status")
    subs.add_parser("browser-start")
    op = subs.add_parser("browser-open")
    op.add_argument("--agent", required=True)
    op.add_argument("--desktop", type=int, required=True)
    op.add_argument("--url", default="about:blank")
    ep = subs.add_parser("existing-open")
    ep.add_argument("--agent", required=True)
    ep.add_argument("--desktop", type=int, required=True)
    ep.add_argument("--url", default="about:blank")
    shot = subs.add_parser("browser-screenshot")
    shot.add_argument("--agent", required=True)
    shot.add_argument("--output", type=Path, required=True)
    args = ap.parse_args()
    try:
        if args.cmd == "status":
            result = {"windows": desktop_status(vda()), "existing_chrome": existing_chrome_status(),
                      "fallback_dedicated_browser": browser_status(), "registered_agents": load_state()}
        elif args.cmd == "allocate":
            result = lazy_allocate(vda() if args.mode == "visual" else None,
                                   args.agent, args.mode)
        elif args.cmd == "setup":
            result = desktop_setup(vda(), args.names)
        elif args.cmd == "switch":
            result = desktop_switch(vda(), args.number)
        elif args.cmd == "move":
            result = desktop_move(vda(), args.hwnd, args.number)
        elif args.cmd == "browser-status":
            result = browser_status()
        elif args.cmd == "browser-start":
            result = browser_start()
        elif args.cmd == "browser-open":
            result = browser_open(vda(), args.agent, args.desktop, args.url)
        elif args.cmd == "existing-open":
            result = existing_chrome_window(vda(), args.agent, args.desktop, args.url)
        elif args.cmd == "browser-screenshot":
            result = browser_screenshot(args.agent, args.output)
        else:
            raise DesktopError("Unknown command")
        print(json.dumps({"ok": True, **result}, ensure_ascii=False, indent=2))
    except (DesktopError, ValueError, OSError) as exc:
        print(json.dumps({"ok": False, "error": str(exc)}, ensure_ascii=False))
        return 2
    return 0


if __name__ == "__main__":
    sys.exit(main())
