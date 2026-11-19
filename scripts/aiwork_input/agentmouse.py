"""Per-agent logical mouse for native Windows apps, without global pointer input.

Runs a small authenticated named-pipe broker in the interactive Windows session.
Only UIA control-pattern operations are exposed. Target HWND MUST belong to
a named AI-<agent> Windows virtual desktop and cannot be pinned on all desktops.

No SetCursorPos, SendInput, mouse_event, foreground switching, or clipboard.
Browser operations stay inside BrowserSkill/CDP using aiwork, not this broker.
"""
from __future__ import annotations
import argparse
import ctypes
from ctypes import wintypes
from multiprocessing.connection import Listener, Client, AuthenticationError
import getpass
import hashlib
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import threading
import time
import winreg

HERE=Path(__file__).resolve().parent
WORKSPACE=HERE.parents[1]/"desktop-workspace"
OWNER=os.environ.get("GPT_PC_DESKTOP_BROKER_OWNER") or getpass.getuser()
HOME=Path(os.environ.get("USERPROFILE") or Path.home())
LOCAL_APP_DATA=Path(os.environ.get("LOCALAPPDATA") or (HOME/"AppData"/"Local"))
AUTH=LOCAL_APP_DATA/"GPTPCDesktopWorkspace"/"auth.key"
DLL=Path(os.environ.get("GPT_PC_VDA_DLL") or (WORKSPACE/"VirtualDesktopAccessor.dll"))
DLL_HASH="8740c572a1c000e3b87ffeb1e4c397eae9af3bd4a2abdc3bcffacab4493f8ff5"
SUFFIX=hashlib.sha256((os.environ.get("COMPUTERNAME","")+"\\AIWorkNativeInput\\"+OWNER).encode("utf-8")).hexdigest()[:20]
PIPE=rf"\\.\pipe\AIWorkNativeInput-{SUFFIX}"
RUN_KEY=r"Software\Microsoft\Windows\CurrentVersion\Run"
RUN_NAME="GPTPCBackgroundInput"
UIA=HERE/"uia.ps1"
MAX_INPUT=24000
CONCURRENT=threading.BoundedSemaphore(2)
AGENT_LOCKS={}
AGENT_LOCKS_GUARD=threading.Lock()

class InputError(RuntimeError):pass

def session_id(pid=None):
    out=wintypes.DWORD()
    if not ctypes.windll.kernel32.ProcessIdToSessionId(int(pid or os.getpid()),ctypes.byref(out)):
        raise InputError("SESSION_LOOKUP_FAILED")
    return out.value

def agent_id(value):
    if not isinstance(value,str) or re.fullmatch(r"[A-Za-z0-9_-]{1,48}",value) is None:
        raise InputError("INVALID_AGENT")
    return value

def key():
    if not AUTH.is_file():raise InputError("MISSING_DESKTOP_BROKER_AUTH")
    raw=bytes.fromhex(AUTH.read_text("ascii").strip())
    if len(raw)!=32:raise InputError("INVALID_AUTH_KEY")
    return raw

def verify_target(agent,hwnd):
    agent=agent_id(agent)
    try:hwnd=int(hwnd)
    except Exception:raise InputError("INVALID_HWND")
    if not 0 < hwnd <= 0x7fffffffffffffff:
        raise InputError("INVALID_HWND")
    u=ctypes.windll.user32
    u.IsWindow.argtypes=[wintypes.HWND];u.IsWindow.restype=wintypes.BOOL
    u.GetWindowThreadProcessId.argtypes=[wintypes.HWND,ctypes.POINTER(wintypes.DWORD)]
    if not u.IsWindow(hwnd):raise InputError("WINDOW_NOT_FOUND")
    pid=wintypes.DWORD()
    u.GetWindowThreadProcessId(hwnd,ctypes.byref(pid))
    if not pid.value or session_id(pid.value)!=session_id():
        raise InputError("WINDOW_OUTSIDE_INTERACTIVE_SESSION")
    if not DLL.is_file() or hashlib.sha256(DLL.read_bytes()).hexdigest()!=DLL_HASH:
        raise InputError("DESKTOP_DLL_UNVERIFIED")
    native=ctypes.WinDLL(str(DLL))
    number=native.GetWindowDesktopNumber
    number.argtypes=[wintypes.HWND];number.restype=ctypes.c_int
    idx=int(number(hwnd))
    if idx<1:raise InputError("PRIMARY_OR_UNKNOWN_WINDOW_REJECTED")
    for name in ("IsPinnedWindow","IsPinnedApp"):
        pin=getattr(native,name,None)
        if pin:
            pin.argtypes=[wintypes.HWND];pin.restype=ctypes.c_int
            # -1 means this desktop DLL could not resolve the app pin state
            # (common for Win32 apps without an AppUserModelID). Only +1 is
            # a positive pinned verdict; the named desktop check still guards.
            if pin(hwnd)==1:raise InputError("WINDOW_PINNED_TO_ALL_DESKTOPS")
    total=native.GetDesktopCount()
    if not 2<=total<=50 or idx>=total:raise InputError("DESKTOP_INDEX_INVALID")
    getname=native.GetDesktopName
    getname.argtypes=[ctypes.c_int,ctypes.c_void_p,ctypes.c_size_t]
    getname.restype=ctypes.c_int
    namebuf=ctypes.create_string_buffer(512)
    if getname(idx,ctypes.cast(namebuf,ctypes.c_void_p),len(namebuf))==-1:
        raise InputError("DESKTOP_NAME_UNAVAILABLE")
    actual=namebuf.value.decode("utf-8","replace")
    if actual!="AI-"+agent:
        raise InputError("WINDOW_NOT_OWNED_BY_THIS_AGENT")
    return {"window":hwnd,"pid":pid.value,"desktop":idx+1,"desktop_name":actual}

def fallback_win32_control(request, root_info):
    """Targeted Win32 messages for classic Edit/Button controls only.

    AutomationId from UIA must be the child HWND. The child must be an actual
    descendant of the verified parent HWND and in the same process.
    No SendInput/mouse_event/SetCursorPos or global keyboard state is used.
    """
    action=request.get("action")
    if action not in ("set-value","get-value","invoke"):
        raise InputError("WIN32_FALLBACK_ACTION_UNSUPPORTED")
    raw=request.get("automation_id","")
    if not isinstance(raw,str) or not raw.isdecimal():
        raise InputError("WIN32_FALLBACK_REQUIRES_NUMERIC_CHILD_HWND")
    child=int(raw)
    parent=int(root_info["window"])
    u=ctypes.windll.user32
    u.IsChild.argtypes=[wintypes.HWND,wintypes.HWND]
    u.IsChild.restype=wintypes.BOOL
    if not u.IsWindow(child) or not u.IsChild(parent,child):
        raise InputError("CONTROL_NOT_CHILD_OF_AGENT_WINDOW")
    pid=wintypes.DWORD()
    u.GetWindowThreadProcessId(child,ctypes.byref(pid))
    if int(pid.value)!=int(root_info["pid"]):
        raise InputError("CONTROL_NOT_IN_AGENT_PROCESS")
    cls=ctypes.create_unicode_buffer(200)
    u.GetClassNameW(child,cls,199)
    cl=cls.value.casefold()
    is_edit=(cl=="edit" or cl.startswith("windowsforms10.edit."))
    is_button=(cl=="button" or cl.startswith("windowsforms10.button."))
    if action in ("set-value","get-value") and not is_edit:
        raise InputError("EDIT_CONTROL_CLASS_REQUIRED")
    if action=="invoke" and not is_button:
        raise InputError("BUTTON_CONTROL_CLASS_REQUIRED")

    u.SendMessageTimeoutW.argtypes=[
        wintypes.HWND,wintypes.UINT,wintypes.WPARAM,wintypes.LPARAM,
        wintypes.UINT,wintypes.UINT,ctypes.POINTER(ctypes.c_size_t)
    ]
    u.SendMessageTimeoutW.restype=wintypes.LPARAM

    def read_edit_text():
        # GetWindowTextW returns blank for a child Edit in a different
        # process. WM_GETTEXT/LENGTH are system-marshalled Win32 messages.
        length=ctypes.c_size_t()
        delivered=u.SendMessageTimeoutW(
            child,0x000E,0,0,0x0002,1000,ctypes.byref(length))
        if not delivered:
            raise InputError("TARGETED_WM_GETTEXTLENGTH_TIMEOUT")
        if length.value>12000:
            raise InputError("TARGETED_EDIT_TEXT_EXCEEDS_READ_LIMIT")
        buffer=ctypes.create_unicode_buffer(int(length.value)+2)
        copied=ctypes.c_size_t()
        delivered=u.SendMessageTimeoutW(
            child,0x000D,len(buffer),ctypes.cast(buffer,ctypes.c_void_p).value,
            0x0002,1000,ctypes.byref(copied))
        if not delivered:
            raise InputError("TARGETED_WM_GETTEXT_TIMEOUT")
        return buffer.value

    if action=="get-value":
        return {"ok":True,"value":read_edit_text(),
                "method":"WIN32_TARGETED_EDIT_MESSAGE","window":child}
    result=ctypes.c_size_t()
    if action=="set-value":
        value=request.get("value","")
        if not isinstance(value,str) or len(value)>12000:
            raise InputError("UNSAFE_EDIT_VALUE")
        buf=ctypes.create_unicode_buffer(value)
        lp=ctypes.cast(buf,ctypes.c_void_p).value
        delivered=u.SendMessageTimeoutW(child,0x000C,0,lp,0x0002,1200,ctypes.byref(result))
        if not delivered:raise InputError("TARGETED_WM_SETTEXT_TIMEOUT")
        # WM_SETTEXT can finish before a WinForms UI message-pump update.
        # Read the cross-process Edit using WM_GETTEXT, never GetWindowTextW.
        # Poll only; input is never re-sent automatically.
        verified=False
        for _ in range(18):
            if read_edit_text()==value:
                verified=True
                break
            time.sleep(.055)
        if not verified:
            return {"ok":True,"method":"WIN32_TARGETED_EDIT_MESSAGE",
                    "window":child,"message_delivered":True,
                    "value_verified":False,"requires_readback":True,
                    "warning":"WRITE_DELIVERED_BUT_UNCONFIRMED; INSPECT_BEFORE_RETRY"}
        return {"ok":True,"method":"WIN32_TARGETED_EDIT_MESSAGE",
                "window":child,"value_verified":True}
    delivered=u.SendMessageTimeoutW(child,0x00F5,0,0,0x0002,1200,ctypes.byref(result))
    if not delivered:raise InputError("TARGETED_BM_CLICK_TIMEOUT")
    return {"ok":True,"method":"WIN32_TARGETED_BUTTON_MESSAGE",
            "window":child,"message_delivered":True,
            "effect_verified":False}


def native_uia(request):
    action=request.get("action")
    if action not in ("inspect","invoke","set-value","get-value","toggle","select"):
        raise InputError("UNSUPPORTED_ACTION")
    agent=agent_id(request.get("agent"))
    target=verify_target(agent,request.get("hwnd"))
    # Browser automation always uses the authenticated BrowserSkill session.
    # UIA native writes must not bypass a browser human-control receipt.
    rootcls=ctypes.create_unicode_buffer(200)
    ctypes.windll.user32.GetClassNameW(target["window"],rootcls,199)
    if action!="inspect" and rootcls.value in ("Chrome_WidgetWin_1","MozillaWindowClass"):
        raise InputError("BROWSER_INPUT_MUST_USE_AGENT_BROWSERSKILL_SESSION")
    identity=request.get("automation_id") or ""
    name=request.get("name") or ""
    if action!="inspect":
        if not isinstance(identity,str) or not isinstance(name,str):
            raise InputError("INVALID_CONTROL_SELECTOR")
        if (not identity and not name) or len(identity)>180 or len(name)>180:
            raise InputError("UNSAFE_CONTROL_SELECTOR")
    value=request.get("value","")
    if not isinstance(value,str) or len(value)>12000:
        raise InputError("VALUE_TOO_LONG")
    u=ctypes.windll.user32
    u.GetCursorPos.argtypes=[ctypes.POINTER(wintypes.POINT)]
    before=wintypes.POINT()
    u.GetCursorPos(ctypes.byref(before))
    fg_before=int(u.GetForegroundWindow())
    payload={
        "hwnd":target["window"],
        "action":action,
        "automation_id":identity,
        "name":name,
        "value":value,
        "limit":min(100,max(1,int(request.get("limit",40))))
    }
    flags=getattr(subprocess,"CREATE_NO_WINDOW",0)
    completed=subprocess.run(
        ["powershell.exe","-NoProfile","-NonInteractive","-ExecutionPolicy","Bypass","-File",str(UIA)],
        input=json.dumps(payload,ensure_ascii=False),
        capture_output=True,text=True,encoding="utf-8",errors="replace",
        cwd=str(HERE),creationflags=flags,timeout=12
    )
    try:response=json.loads(completed.stdout.strip().splitlines()[-1])
    except Exception:
        raise InputError("UIA_FAILED: "+(completed.stderr or completed.stdout)[:250])
    # Classic Win32/WinForms controls may expose only an HWND-backed Pane.
    # Fall back exclusively to verified descendant Edit/Button messages.
    # Never emulate a global mouse click or keyboard sequence.
    if (not response.get("ok") and response.get("error") in
        ("VALUE_PATTERN_UNSUPPORTED","INVOKE_PATTERN_UNSUPPORTED")):
        # Revalidate desktop ownership and process after the UIA subprocess
        # returns. A window could have moved or been replaced meanwhile.
        current=verify_target(agent,target["window"])
        if current["pid"]!=target["pid"]:
            raise InputError("WINDOW_IDENTITY_CHANGED")
        response=fallback_win32_control(request,current)
    after=wintypes.POINT()
    u.GetCursorPos(ctypes.byref(after))
    response["foreground_unchanged"]=fg_before==int(u.GetForegroundWindow())
    response["cursor_observed_unchanged"]=(before.x,before.y)==(after.x,after.y)
    response["used_global_mouse"]=False
    response["used_global_keyboard"]=False
    response["agent"]=agent
    response["target_desktop"]=target["desktop"]
    # If provider itself stole focus, flag explicitly; never silently retry.
    if not response["foreground_unchanged"]:
        response["warning"]="APPLICATION_ACTIVATED_FOREGROUND; STOP_THIS_UIA_PATH"
    return response

def dispatch(message):
    if not isinstance(message,dict):raise InputError("INVALID_REQUEST")
    if message.get("action")=="ping":
        return {"ok":True,"broker_session":session_id(),"pid":os.getpid(),
                "mode":"background_UiAutomation","global_pointer_injection":False}
    return native_uia(message)

def handle_connection(client):
    with client:
        try:
            if not client.poll(7):return
            message=json.loads(client.recv_bytes(MAX_INPUT).decode("utf-8"))
            if not isinstance(message,dict):
                raise InputError("INVALID_REQUEST")
            # Different agents may operate independent controls concurrently,
            # but commands from one agent must never race on the same window.
            agent=message.get("agent")
            if message.get("action")!="ping":
                agent=agent_id(agent)
            with AGENT_LOCKS_GUARD:
                agent_lock=AGENT_LOCKS.setdefault(agent or "__ping__",threading.Lock())
            if not CONCURRENT.acquire(timeout=5):
                result={"ok":False,"error":"NATIVE_INPUT_BUSY"}
            else:
                try:
                    if not agent_lock.acquire(timeout=5):
                        result={"ok":False,"error":"AGENT_INPUT_BUSY"}
                    else:
                        try:result=dispatch(message)
                        finally:agent_lock.release()
                finally:CONCURRENT.release()
        except Exception as exc:
            result={"ok":False,"error":str(exc)[:370]}
        try:client.send_bytes(json.dumps(result,ensure_ascii=False).encode("utf-8"))
        except (EOFError,OSError):pass

def broker():
    if session_id()==0 or getpass.getuser().casefold()!=OWNER.casefold():
        raise InputError("BROKER_MUST_RUN_IN_CurrentUser_INTERACTIVE_SESSION")
    k=ctypes.WinDLL("kernel32",use_last_error=True)
    k.CreateMutexW.argtypes=[ctypes.c_void_p,wintypes.BOOL,wintypes.LPCWSTR]
    k.CreateMutexW.restype=wintypes.HANDLE
    k.CloseHandle.argtypes=[wintypes.HANDLE]
    k.ReleaseMutex.argtypes=[wintypes.HANDLE]
    name=r"Local\AIWorkNativeInput-"+SUFFIX
    mutex=k.CreateMutexW(None,True,name)
    if not mutex:raise InputError("SINGLETON_LOCK_FAILED")
    if ctypes.get_last_error()==183:
        k.CloseHandle(mutex)
        return 0
    try:
        with Listener(address=PIPE,family="AF_PIPE",authkey=key()) as listener:
            while True:
                try:client=listener.accept()
                except AuthenticationError:continue
                except OSError:
                    time.sleep(.2);continue
                threading.Thread(target=handle_connection,args=(client,),daemon=True).start()
    finally:
        k.ReleaseMutex(mutex);k.CloseHandle(mutex)

def rpc(request):
    with Client(address=PIPE,family="AF_PIPE",authkey=key()) as client:
        client.send_bytes(json.dumps(request,ensure_ascii=False).encode("utf-8"))
        if not client.poll(16):raise InputError("INPUT_BROKER_TIMEOUT")
        return json.loads(client.recv_bytes(65536).decode("utf-8"))

def install():
    if session_id()==0 or getpass.getuser().casefold()!=OWNER.casefold():
        raise InputError("INSTALL_FROM_CurrentUser_INTERACTIVE_SESSION")
    pythonw=Path(sys.executable).with_name("pythonw.exe")
    if not pythonw.is_file():raise InputError("PYTHONW_NOT_FOUND")
    cmd=f'"{pythonw}" "{Path(__file__).resolve()}" broker'
    with winreg.CreateKeyEx(winreg.HKEY_CURRENT_USER,RUN_KEY,0,winreg.KEY_READ|winreg.KEY_WRITE) as k:
        try:old,_=winreg.QueryValueEx(k,RUN_NAME)
        except FileNotFoundError:old=None
        if old and old!=cmd:raise InputError("UNEXPECTED_EXISTING_RUN_ENTRY")
        if not old:winreg.SetValueEx(k,RUN_NAME,0,winreg.REG_SZ,cmd)
    try:reply=rpc({"action":"ping"})
    except Exception:reply=None
    if not reply or not reply.get("ok"):
        subprocess.Popen(
            [str(pythonw),str(Path(__file__).resolve()),"broker"],
            stdin=subprocess.DEVNULL,stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL,
            cwd=str(HERE),creationflags=subprocess.DETACHED_PROCESS|subprocess.CREATE_NEW_PROCESS_GROUP,
            close_fds=True
        )
        for _ in range(32):
            time.sleep(.2)
            try:
                reply=rpc({"action":"ping"})
                if reply.get("ok"):break
            except Exception:pass
    if not reply or not reply.get("ok"):
        raise InputError("INPUT_BROKER_NOT_ONLINE")
    return {"autostart":True,"broker":reply}

def main():
    ap=argparse.ArgumentParser(description="Independent per-AI targeted background UIA input")
    sub=ap.add_subparsers(dest="action",required=True)
    for x in ("status","install","broker"):sub.add_parser(x)
    for x in ("inspect","invoke","set-value","get-value","toggle","select"):
        p=sub.add_parser(x)
        p.add_argument("--agent",required=True)
        p.add_argument("--hwnd",required=True,type=lambda s:int(s,0))
        p.add_argument("--id",dest="automation_id",default="")
        p.add_argument("--name",default="")
        if x=="set-value":p.add_argument("--value",required=True)
        if x=="inspect":p.add_argument("--limit",type=int,default=40)
    args=ap.parse_args()
    try:
        if args.action=="broker":return broker()
        if args.action=="install":out={"ok":True,**install()}
        elif args.action=="status":out=rpc({"action":"ping"})
        else:
            req=vars(args)
            out=rpc(req)
        print(json.dumps(out,ensure_ascii=True))
        return 0 if out.get("ok") else 2
    except Exception as exc:
        print(json.dumps({"ok":False,"error":str(exc)[:400]},ensure_ascii=True))
        return 2

if __name__=="__main__":raise SystemExit(main())
