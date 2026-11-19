"""Lightweight BrowserSkill Agent Window -> Windows desktop watcher.

Only future sessions are eligible. Each session is matched to its exact BSK
window, and is moved only when a matching AI-<agent> desktop already exists.
Never changes focus, opens another profile, or touches existing user windows.
"""
from __future__ import annotations
import ctypes
from ctypes import wintypes
import json
import logging
from logging.handlers import RotatingFileHandler
import os
import getpass
import shutil
from pathlib import Path
import subprocess
import sys
import time

ROOT = Path(__file__).resolve().parents[2]
TOOLS = ROOT / "desktop-workspace"
OWNER = os.getenv("GPT_PC_DESKTOP_BROKER_OWNER") or getpass.getuser()
HOME = Path(os.getenv("USERPROFILE") or Path.home())
LOCAL_APP_DATA = Path(os.getenv("LOCALAPPDATA") or (HOME/"AppData"/"Local"))
DATA = LOCAL_APP_DATA/"GPTPCDesktopWorkspace"
REGISTRY = DATA/"aiwork-sessions.json"
LOG = DATA/"agent-window-router.log"
BSK = Path(shutil.which("bsk") or (TOOLS/"browser-skill"/"bsk.exe"))
DESKTOP = TOOLS/"desktopctl.cmd"
DLL = Path(os.getenv("GPT_PC_VDA_DLL") or (TOOLS/"VirtualDesktopAccessor.dll"))
ENV = {**os.environ,"BSK_AUTO_START":"0","BSK_AUTO_UPDATE":"off","BSK_HOME":os.environ.get("BSK_HOME") or str(HOME/".bsk")}

sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from aiwork_window_router import place_agent_window, named_desktop, WindowRouteError


def exec_json(exe: Path,*args: str,timeout: float=10):
    proc=subprocess.run(
        [str(exe),*args],capture_output=True,text=True,encoding="utf-8",
        errors="replace",cwd=str(TOOLS),env=ENV,timeout=timeout)
    try:
        reply=json.loads(proc.stdout)
    except ValueError as e:
        raise RuntimeError("Invalid CLI JSON reply: "+str((proc.stderr or proc.stdout)[:130])) from e
    # The currently running old desktop bridge may reject windows listing.
    # In that case the router performs read-only Session 1 window enumeration.
    if proc.returncode and args and args[0]!="windows":
        raise RuntimeError("CLI command failed: "+str((proc.stderr or proc.stdout)[:180]))
    return reply


def bsk(*args,timeout=16):
    return exec_json(BSK,*args,timeout=timeout)


def desktop(*args):
    return exec_json(DESKTOP,*args,timeout=12)


def agent_registry():
    if not REGISTRY.exists():return {}
    try:
        data=json.loads(REGISTRY.read_text(encoding="utf-8"))
        return {k:v for k,v in data.items() if isinstance(k,str) and isinstance(v,str)}
    except (ValueError,OSError):
        return {}


def active_sessions():
    data=bsk("status","--json")
    if not isinstance(data,dict):return {}
    return {s["session_id"]:s for s in data.get("sessions",[]) if s.get("session_id")}


def eligible(initial,finished,attempts,registry,sessions):
    """Pure function: only newly created sessions mapped to a real agent."""
    live=set(sessions)
    finished.intersection_update(live)
    for sid in list(attempts):
        if sid not in live:attempts.pop(sid,None)
    owner={sid:agent for agent,sid in registry.items() if sid in live}
    return [(owner[sid],sid,sessions[sid]) for sid in live-initial-finished
            if sid in owner and attempts.get(sid,0)<2]


def run():
    sid=wintypes.DWORD()
    if not ctypes.windll.kernel32.ProcessIdToSessionId(os.getpid(),ctypes.byref(sid)) or sid.value==0:
        raise RuntimeError("Router must run in the interactive Windows user session")
    DATA.mkdir(parents=True,exist_ok=True)
    log=logging.getLogger("aiwork-window-router")
    log.setLevel(logging.INFO)
    log.addHandler(RotatingFileHandler(LOG,maxBytes=300000,backupCount=2,encoding="utf-8"))

    kernel=ctypes.WinDLL("kernel32",use_last_error=True)
    kernel.CreateMutexW.argtypes=[ctypes.c_void_p,wintypes.BOOL,wintypes.LPCWSTR]
    kernel.CreateMutexW.restype=wintypes.HANDLE
    kernel.ReleaseMutex.argtypes=[wintypes.HANDLE]
    kernel.CloseHandle.argtypes=[wintypes.HANDLE]
    mutex=kernel.CreateMutexW(None,True,r"Local\GPT-PCAIWindowRouter")
    if not mutex:raise RuntimeError("Singleton mutex failed")
    if ctypes.get_last_error()==183:
        kernel.CloseHandle(mutex)
        return 0
    try:
        initial=set(active_sessions())
        finished=set()
        attempts={}
        log.info("Started, %d existing sessions excluded from routing",len(initial))
        while True:
            try:
                sessions=active_sessions()
                for agent,session_id,entry in eligible(initial,finished,attempts,agent_registry(),sessions):
                    if named_desktop(desktop,agent) is None:continue
                    try:
                        result=place_agent_window(
                            agent,session_id,entry.get("agent_window_id"),
                            bsk,desktop,browser="chrome",dll_path=DLL,
                            require_named_desktop=True)
                        finished.add(session_id)
                        log.info("Placed agent=%s session=%s window=%s desktop=%s status=%s",
                                 agent,session_id,result.get("hwnd"),
                                 result.get("desktop_number"),result.get("status"))
                    except (RuntimeError,OSError,ValueError,WindowRouteError) as e:
                        attempts[session_id]=attempts.get(session_id,0)+1
                        log.warning("Routing agent=%s session=%s attempt=%d: %s",
                                    agent,session_id,attempts[session_id],str(e)[:260])
                time.sleep(1.25)
            except (RuntimeError,OSError,ValueError) as e:
                log.warning("Watcher status error: %s",str(e)[:200])
                time.sleep(2.5)
    finally:
        kernel.ReleaseMutex(mutex)
        kernel.CloseHandle(mutex)


if __name__=="__main__":
    raise SystemExit(run())
