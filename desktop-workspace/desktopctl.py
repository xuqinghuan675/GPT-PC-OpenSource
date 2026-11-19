"""GPT-PC Desktop Workspace: session-bridged Windows desktop control.

Independent of ChatGPT plugins, RPC runtimes and browser processes.
The interactive-user broker owns all shell calls; SSH/terminal clients
speak to a local Windows named pipe authenticated with a per-user key.
"""
from __future__ import annotations

import argparse
import ctypes
from ctypes import wintypes
import getpass
import hashlib
import json
import logging
from logging.handlers import RotatingFileHandler
from multiprocessing.connection import Listener, Client, AuthenticationError
import os
from pathlib import Path
import re
import secrets
import subprocess
import sys
import time
import winreg

ROOT = Path(__file__).resolve().parent
DLL = Path(os.environ.get("GPT_PC_VDA_DLL") or (ROOT / "VirtualDesktopAccessor.dll"))
DLL_HASH = "8740c572a1c000e3b87ffeb1e4c397eae9af3bd4a2abdc3bcffacab4493f8ff5"
BROKER_OWNER = os.environ.get("GPT_PC_DESKTOP_BROKER_OWNER") or getpass.getuser()
OWNER_HOME = Path(os.environ.get("USERPROFILE") or Path.home())
LOCAL_APP_DATA = Path(os.environ.get("LOCALAPPDATA") or (OWNER_HOME / "AppData" / "Local"))
DATA = LOCAL_APP_DATA / "GPTPCDesktopWorkspace"
KEY_FILE = DATA / "auth.key"
LOG_FILE = DATA / "broker.log"
AUTO_KEY = r"Software\Microsoft\Windows\CurrentVersion\Run"
AUTO_NAME = "GPTPCDesktopWorkspace"
PIPE_SUFFIX = hashlib.sha256(
    (os.environ.get("COMPUTERNAME","") + "\\" + BROKER_OWNER).encode("utf-8")
).hexdigest()[:20]
PIPE_NAME = rf"\\.\pipe\GPTPCDesktopWorkspace-{PIPE_SUFFIX}"
MAX_DESKTOPS = 6
NATIVE = None
LOG = logging.getLogger("desktop-workspace")


class WorkspaceError(Exception):
    pass


def session_id(pid=None):
    current = os.getpid() if pid is None else int(pid)
    out = wintypes.DWORD()
    if not ctypes.windll.kernel32.ProcessIdToSessionId(current, ctypes.byref(out)):
        raise WorkspaceError(f"Cannot determine Windows session for PID {current}")
    return out.value


def check_native():
    global NATIVE
    if NATIVE is not None:
        return NATIVE
    if not DLL.is_file() or hashlib.sha256(DLL.read_bytes()).hexdigest() != DLL_HASH:
        raise WorkspaceError("Desktop DLL missing or checksum mismatch")
    NATIVE = ctypes.WinDLL(str(DLL))
    return NATIVE


def native(name, *argtypes):
    f = getattr(check_native(), name)
    f.restype = ctypes.c_int
    f.argtypes = list(argtypes)
    return f


def checked(n, label):
    if int(n) == -1:
        raise WorkspaceError(f"{label} failed; verify Windows build and interactive session")
    return int(n)


def count():
    return checked(native("GetDesktopCount")(), "GetDesktopCount")


def active():
    return checked(native("GetCurrentDesktopNumber")(), "GetCurrentDesktopNumber")


def desk_name(number):
    buff = ctypes.create_string_buffer(1024)
    result = native("GetDesktopName", ctypes.c_int, ctypes.c_void_p, ctypes.c_size_t)(
        number, ctypes.cast(buff, ctypes.c_void_p), len(buff)
    )
    return buff.value.decode("utf-8", "replace") if result != -1 else ""


def desktop_status():
    number = count()
    if number < 1 or number > 50:
        raise WorkspaceError(f"Unexpected desktop count: {number}")
    return {
        "count": number,
        "active": active() + 1,
        "desktops": [
            {"number": i+1, "name": desk_name(i) or f"Desktop {i+1}"}
            for i in range(number)
        ],
    }


def validate_agent(agent):
    if not isinstance(agent, str) or not re.fullmatch(r"[A-Za-z0-9_-]{1,48}", agent):
        raise WorkspaceError("Agent ID must be 1–48 ASCII letters/digits/_/-")
    return agent


def allocate(agent, mode):
    validate_agent(agent)
    if mode in ("background", "web"):
        return {
            "agent": agent, "mode": mode, "desktop_number": None,
            "created": False,
            "browser": "existing_chrome_playwright_extension" if mode == "web" else None,
            "profile_dir_name": "Default" if mode == "web" else None,
            "new_chrome_profile_allowed": False,
            "note": "No Windows desktop or Chrome process created",
        }
    if mode != "visual":
        raise WorkspaceError("Mode must be background, web or visual")
    before = desktop_status()
    tag = "AI-" + agent
    for desk in before["desktops"]:
        if desk["name"] == tag:
            return {
                "agent": agent, "mode": mode, "desktop_number": desk["number"],
                "created": False, "reused": True,
                "active_desktop_unchanged": True,
            }
    if before["count"] >= MAX_DESKTOPS:
        raise WorkspaceError(f"Desktop cap {MAX_DESKTOPS} reached; reuse or release an existing desktop")
    original_active = before["active"] - 1
    try:
        checked(native("CreateDesktop")(), "CreateDesktop")
        if count() != before["count"] + 1:
            raise WorkspaceError("Desktop count did not increment")
        checked(
            native("SetDesktopName", ctypes.c_int, ctypes.c_char_p)(
                before["count"], tag.encode("utf-8")
            ),
            "SetDesktopName",
        )
    finally:
        if active() != original_active and original_active < count():
            checked(native("GoToDesktopNumber", ctypes.c_int)(
                original_active
            ), "Restore previous desktop")
    after = desktop_status()
    if after["active"] != before["active"]:
        raise WorkspaceError("Previous active desktop was not preserved")
    return {
        "agent": agent, "mode": mode, "desktop_number": before["count"] + 1,
        "created": True, "reused": False,
        "active_desktop_unchanged": True,
        "note": "Desktop allocated only; app/browser processes are not launched",
    }


def visible_window_count_on_desktop(number):
    """Conservative window check before removing a workspace."""
    user32 = ctypes.windll.user32
    target = number - 1
    windows = set()

    def callback(hwnd, _):
        if user32.IsWindowVisible(hwnd) and user32.GetWindowTextLengthW(hwnd) > 0:
            index = native("GetWindowDesktopNumber", ctypes.c_void_p)(hwnd)
            if index == target:
                windows.add(int(hwnd))
        return 1

    cb = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)(callback)
    if not user32.EnumWindows(cb, 0):
        raise WorkspaceError("Unable to verify desktop windows before release")
    return len(windows)


def release(agent):
    validate_agent(agent)
    listing = desktop_status()
    tag = "AI-" + agent
    match = next((item for item in listing["desktops"] if item["name"] == tag), None)
    if match is None:
        return {"agent": agent, "removed": False, "already_absent": True}
    number = match["number"]
    if number == 1:
        raise WorkspaceError("Refusing to remove primary desktop")
    if listing["active"] == number:
        raise WorkspaceError("Desktop is active; switch to primary desktop before releasing")
    count_windows = visible_window_count_on_desktop(number)
    if count_windows:
        return {"agent": agent, "removed": False, "retained_in_use": True,
                "visible_windows": count_windows}
    checked(
        native("RemoveDesktop", ctypes.c_int, ctypes.c_int)(number-1, 0),
        "RemoveDesktop",
    )
    after = desktop_status()
    if after["count"] != listing["count"] - 1:
        raise WorkspaceError("Desktop removal was not confirmed")
    return {"agent": agent, "removed": True, "desktop_count": after["count"]}


def switch(number):
    n = count()
    if not 1 <= number <= n:
        raise WorkspaceError("Desktop number out of range")
    checked(native("GoToDesktopNumber", ctypes.c_int)(number - 1), "GoToDesktopNumber")
    return {"active": active() + 1, "requested": number}


def move_window(hwnd, number):
    if not 1 <= number <= count():
        raise WorkspaceError("Desktop number out of range")
    if not ctypes.windll.user32.IsWindow(ctypes.c_void_p(hwnd)):
        raise WorkspaceError("Window does not exist")
    pid = wintypes.DWORD()
    ctypes.windll.user32.GetWindowThreadProcessId(
        ctypes.c_void_p(hwnd), ctypes.byref(pid)
    )
    if not pid.value or session_id(pid.value) != session_id():
        raise WorkspaceError("Target window belongs to a different Windows session")
    checked(
        native("MoveWindowToDesktopNumber", ctypes.c_void_p, ctypes.c_int)(
            hwnd, number - 1
        ),
        "MoveWindowToDesktopNumber",
    )
    return {"window": hwnd, "desktop": number, "moved": True}


def ensure_key(create=False):
    if KEY_FILE.exists():
        key = bytes.fromhex(KEY_FILE.read_text(encoding="ascii").strip())
        if len(key) != 32:
            raise WorkspaceError("Auth key is invalid")
        return key
    if not create:
        raise WorkspaceError("Broker not installed; run 'desktopctl install' locally")
    DATA.mkdir(parents=True, exist_ok=True)
    secret = secrets.token_bytes(32)
    try:
        with KEY_FILE.open("x", encoding="ascii") as f:
            f.write(secret.hex())
    except FileExistsError:
        return ensure_key()
    return secret


def request(action, **kwargs):
    return {"action": action, "args": kwargs}


def dispatch(payload):
    if not isinstance(payload, dict) or not isinstance(payload.get("args"), dict):
        raise WorkspaceError("Bad request")
    action, args = payload.get("action"), payload["args"]
    if action == "ping":
        return {"pid": os.getpid(), "session": session_id(), "user": getpass.getuser()}
    if action == "status":
        return {"broker": {"pid": os.getpid(), "session": session_id()},
                **desktop_status()}
    if action == "allocate":
        return allocate(args.get("agent"), args.get("mode", "background"))
    if action == "release":
        return release(args.get("agent"))
    if action == "switch":
        return switch(int(args["number"]))
    if action == "move":
        return move_window(int(args["hwnd"]), int(args["number"]))
    raise WorkspaceError("Unsupported action")


def log_setup():
    DATA.mkdir(parents=True, exist_ok=True)
    LOG.setLevel(logging.INFO)
    LOG.addHandler(RotatingFileHandler(LOG_FILE, maxBytes=256000,
                                       backupCount=2, encoding="utf-8"))
    LOG.info("Broker starting PID=%s session=%s", os.getpid(), session_id())


def single_instance():
    name = "Local\\GPTPCDesktopWorkspace-" + PIPE_SUFFIX
    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel.CreateMutexW.argtypes = [ctypes.c_void_p, wintypes.BOOL, wintypes.LPCWSTR]
    kernel.CreateMutexW.restype = wintypes.HANDLE
    kernel.CloseHandle.argtypes = [wintypes.HANDLE]
    kernel.ReleaseMutex.argtypes = [wintypes.HANDLE]
    mutex = kernel.CreateMutexW(None, True, name)
    if not mutex:
        raise WorkspaceError("Cannot create singleton mutex")
    if ctypes.get_last_error() == 183:
        kernel.CloseHandle(mutex)
        raise WorkspaceError("Another desktop broker is already running")
    return kernel, mutex


def serve():
    if session_id() == 0:
        raise WorkspaceError("Broker must run in the interactive Windows session")
    ensure_key(create=True)
    log_setup()
    kernel, mutex = single_instance()
    try:
        desktop_status()  # fail early if Explorer/COM is unavailable
        with Listener(address=PIPE_NAME, family="AF_PIPE", authkey=ensure_key()) as listener:
            LOG.info("Desktop broker online")
            while True:
                try:
                    conn = listener.accept()
                except AuthenticationError:
                    LOG.warning("Named-pipe client authentication failed")
                    continue
                except Exception:
                    LOG.exception("Named pipe accept failed")
                    time.sleep(0.25)
                    continue
                with conn:
                    try:
                        if not conn.poll(5):
                            continue
                        payload = json.loads(conn.recv_bytes(65536).decode("utf-8"))
                        result = {"ok": True, **dispatch(payload)}
                    except Exception as exc:
                        LOG.warning("Desktop request failed: %s", exc)
                        result = {"ok": False, "error": str(exc)[:350]}
                    try:
                        conn.send_bytes(json.dumps(result, ensure_ascii=False).encode("utf-8"))
                    except (OSError, EOFError):
                        LOG.warning("Client disconnected before response")
    finally:
        kernel.ReleaseMutex(mutex)
        kernel.CloseHandle(mutex)


def rpc(payload):
    authkey = ensure_key()
    with Client(address=PIPE_NAME, family="AF_PIPE", authkey=authkey) as client:
        client.send_bytes(json.dumps(payload, ensure_ascii=False).encode("utf-8"))
        if not client.poll(12):
            raise WorkspaceError("Desktop broker did not respond in 12 seconds")
        return json.loads(client.recv_bytes(65536).decode("utf-8"))


def broker_command():
    pyw = Path(sys.executable).with_name("pythonw.exe")
    if not pyw.is_file():
        raise WorkspaceError(f"Pythonw not found: {pyw}")
    return f'"{pyw}" "{Path(__file__).resolve()}" broker'


def install():
    if session_id() == 0:
        raise WorkspaceError("Run install once from the logged-in Windows desktop")
    ensure_key(create=True)
    command = broker_command()
    with winreg.CreateKey(winreg.HKEY_CURRENT_USER, AUTO_KEY) as key:
        winreg.SetValueEx(key, AUTO_NAME, 0, winreg.REG_SZ, command)
    # Existing terminal sessions keep their PATH; fresh SSH logins see updated PATH.
    with winreg.CreateKey(winreg.HKEY_CURRENT_USER, "Environment") as key:
        try:
            path, kind = winreg.QueryValueEx(key, "Path")
        except FileNotFoundError:
            path, kind = "", winreg.REG_EXPAND_SZ
        components = [x.strip('"').rstrip("\\/").lower() for x in path.split(";") if x]
        if str(ROOT).rstrip("\\/").lower() not in components:
            new_path = path + (";" if path else "") + str(ROOT)
            winreg.SetValueEx(key, "Path", 0, kind, new_path)
    # Refresh explorer/user environment for future shells, not required for current.
    try:
        result = wintypes.DWORD_PTR()
        ctypes.windll.user32.SendMessageTimeoutW(
            0xFFFF, 0x001A, 0, "Environment", 0x0002, 1000, ctypes.byref(result)
        )
    except Exception:
        pass
    current = None
    try:
        current = rpc(request("ping"))
    except Exception:
        pass
    if not current or not current.get("ok"):
        pyw = Path(sys.executable).with_name("pythonw.exe")
        kwargs = {"cwd": str(ROOT),
                  "creationflags": subprocess.DETACHED_PROCESS | subprocess.CREATE_NEW_PROCESS_GROUP,
                  "stdin": subprocess.DEVNULL, "stdout": subprocess.DEVNULL, "stderr": subprocess.DEVNULL,
                  "close_fds": True}
        subprocess.Popen([str(pyw), str(Path(__file__).resolve()), "broker"], **kwargs)
        for _ in range(40):
            time.sleep(0.15)
            try:
                current = rpc(request("ping"))
                if current.get("ok"):
                    break
            except Exception:
                continue
    if not current or not current.get("ok"):
        raise WorkspaceError(f"Autostart installed but broker not online; inspect {LOG_FILE}")
    return {"installed": True, "autostart": True, "entrypoint": str(ROOT / "desktopctl.cmd"),
            "broker": current, "path_added_for_new_shells": True}


def local_status():
    state = {"local_session": session_id(), "user": getpass.getuser(),
             "autostart_registry": False, "cli": str(ROOT / "desktopctl.cmd")}
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, AUTO_KEY) as key:
            _, _ = winreg.QueryValueEx(key, AUTO_NAME)
            state["autostart_registry"] = True
    except OSError:
        pass
    try:
        state["broker"] = rpc(request("status"))
    except Exception as exc:
        state["broker"] = {"ok": False, "error": str(exc)[:250]}
    return state


def cli():
    parser = argparse.ArgumentParser(description="On-demand AI Windows desktop (SSH-safe local broker)")
    cmds = parser.add_subparsers(dest="cmd", required=True)
    cmds.add_parser("install")
    cmds.add_parser("broker")
    cmds.add_parser("status")
    cmds.add_parser("ping")
    alloc = cmds.add_parser("allocate")
    alloc.add_argument("--agent", required=True)
    alloc.add_argument("--mode", choices=["background","web","visual"], default="background")
    release_p = cmds.add_parser("release")
    release_p.add_argument("--agent", required=True)
    switch_p = cmds.add_parser("switch")
    switch_p.add_argument("number", type=int)
    move_p = cmds.add_parser("move")
    move_p.add_argument("hwnd", type=lambda x: int(x, 0))
    move_p.add_argument("number", type=int)
    args = parser.parse_args()
    try:
        if args.cmd == "broker":
            serve()
            return 0
        if args.cmd == "install":
            result = {"ok": True, **install()}
        elif args.cmd == "status":
            result = {"ok": True, **local_status()}
        elif args.cmd == "allocate" and args.mode in ("background", "web"):
            result = {"ok": True, **allocate(args.agent, args.mode)}
        else:
            if args.cmd == "ping":
                payload = request("ping")
            elif args.cmd == "allocate":
                payload = request("allocate", agent=args.agent, mode=args.mode)
            elif args.cmd == "release":
                payload = request("release", agent=args.agent)
            elif args.cmd == "switch":
                payload = request("switch", number=args.number)
            elif args.cmd == "move":
                payload = request("move", hwnd=args.hwnd, number=args.number)
            else:
                raise WorkspaceError("Unknown command")
            result = rpc(payload)
            if (args.cmd == "release" and result.get("error") == "Unsupported action"
                    and session_id() != 0 and getpass.getuser().casefold() == BROKER_OWNER.casefold()):
                # Compatibility with the old login-time broker still in memory.
                # Execute ONLY this named empty-desktop release in our own
                # interactive Windows session; never do GUI work in SSH Session 0.
                result = {"ok": True, **release(args.agent), "client_release_fallback": True}
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 0 if result.get("ok") else 2
    except Exception as exc:
        print(json.dumps({"ok": False, "error": str(exc)}, ensure_ascii=False))
        return 2


if __name__ == "__main__":
    sys.exit(cli())
