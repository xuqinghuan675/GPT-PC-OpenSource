"""Local machine-wide AI workspace CLI: shared Chrome sessions + lazy native desktops.

No ChatGPT plugin dependency. BrowserSkill 0.4.1 is a single connected
Chrome profile; agent IDs map to independent no-focus Agent Windows.
"""
import argparse, contextlib, getpass, json, msvcrt, os, re, shutil, subprocess, sys, time
from pathlib import Path
from urllib.parse import urlsplit

BASE=Path(__file__).resolve().parent
BSK=Path(shutil.which("bsk") or (BASE/"browser-skill"/"bsk.exe"))
DESKTOP=BASE/"desktopctl.cmd"
OWNER=os.getenv("GPT_PC_DESKTOP_BROKER_OWNER") or getpass.getuser()
OWNER_HOME=Path(os.getenv("USERPROFILE") or Path.home())
LOCAL_APP_DATA=Path(os.getenv("LOCALAPPDATA") or (OWNER_HOME/"AppData"/"Local"))
DATA=LOCAL_APP_DATA/"GPTPCDesktopWorkspace"
SESSIONS=DATA/"aiwork-sessions.json"
LOCK=DATA/"aiwork.lock"
ENV={**os.environ,"BSK_AUTO_START":"0","BSK_AUTO_UPDATE":"off","BSK_HOME":os.environ.get("BSK_HOME") or str(OWNER_HOME/".bsk")}

class AIWorkError(Exception):pass

def validate_agent(agent):
    if not isinstance(agent,str) or not re.fullmatch(r"[A-Za-z0-9_-]{1,48}",agent):
        raise AIWorkError("Use an agent ID with 1-48 letters, digits, underscore or hyphen")
    return agent

def valid_url(url):
    p=urlsplit(url)
    if p.scheme not in ("https","http") or not p.netloc or p.username or p.password:
        raise AIWorkError("Web URL must be http(s) without embedded credentials")
    return url

def call(exe,args,timeout=20):
    if not Path(exe).is_file() and not shutil.which(str(exe)):
        raise AIWorkError("BrowserSkill CLI not found. Install it from https://github.com/Tencent/BrowserSkill and verify bsk --version.")
    p=subprocess.run([str(exe),*args],capture_output=True,encoding="utf-8",
                     errors="replace",env=ENV,cwd=str(BASE),timeout=timeout)
    if p.returncode:
        raise AIWorkError((p.stderr or p.stdout or "Command failed")[:700])
    try:return json.loads(p.stdout)
    except json.JSONDecodeError as exc:raise AIWorkError("Expected JSON CLI output") from exc

def bsk(*args,timeout=20):return call(BSK,list(args),timeout)
def desk(*args):return call(DESKTOP,list(args),15)

@contextlib.contextmanager
def mutex():
    DATA.mkdir(parents=True,exist_ok=True)
    with LOCK.open("a+b") as f:
        f.seek(0,2)
        if not f.tell():
            f.write(b"0");f.flush()
        for i in range(100):
            try:
                f.seek(0);msvcrt.locking(f.fileno(),msvcrt.LK_NBLCK,1)
                break
            except OSError:time.sleep(.05)
        else:raise AIWorkError("AI workspace busy")
        try:yield
        finally:
            f.seek(0);msvcrt.locking(f.fileno(),msvcrt.LK_UNLCK,1)

def load():
    if not SESSIONS.exists():return {}
    value=json.loads(SESSIONS.read_text("utf-8"))
    if not isinstance(value,dict):raise AIWorkError("Invalid agent session registry")
    return value

def save(obj):
    import uuid
    temp=DATA/(".aiwork-"+uuid.uuid4().hex+".tmp")
    try:
        temp.write_text(json.dumps(obj,ensure_ascii=False),encoding="utf-8")
        os.replace(temp,SESSIONS)
    finally:temp.unlink(missing_ok=True)

def status():
    s=bsk("status","--json")
    with mutex():entries=load()
    live={x["session_id"] for x in s.get("sessions",[]) if "session_id" in x}
    native_state=desk("status")
    native_ready=bool(native_state.get("broker",{}).get("ok"))
    browser_ready=any(not b.get("unresponsive") for b in s.get("browsers",[]) if b.get("browser_name")=="chrome")
    return {"healthy":bool(native_ready and browser_ready),
            "browser":{"daemon_pid":s.get("pid"),"connected":len(s.get("browsers",[])),
                       "ready":bool(browser_ready),"active_sessions":len(live)},
            "agent_sessions":{k:{"id":v,"active":v in live} for k,v in entries.items()},
            "desktop_ready":native_ready,"windows":native_state}

def session_for(agent):
    agent=validate_agent(agent)
    with mutex():sid=load().get(agent)
    if not sid:raise AIWorkError(f"Agent {agent} has no session; start it first")
    s=bsk("status","--json")
    if sid not in {x.get("session_id") for x in s.get("sessions",[])}:
        raise AIWorkError("Agent session expired; start it again")
    return sid

def start(agent,url=None,browser=None):
    agent=validate_agent(agent)
    if url:valid_url(url)
    with mutex():
        current=bsk("status","--json")
        active={x.get("session_id") for x in current.get("sessions",[])}
        mapping=load()
        sid=mapping.get(agent)
        created=False
        if sid not in active:
            candidates=[b for b in current.get("browsers",[])
                        if b.get("browser_name")=="chrome" and not b.get("unresponsive")
                        and (not browser or b.get("instance_id")==browser or b.get("label")==browser)]
            if len(candidates)!=1:
                raise AIWorkError(f"Expected exactly one connected Chrome profile, found {len(candidates)}")
            result=bsk("session","start","--no-focus","--browser",candidates[0]["instance_id"],
                       "--name",agent,"--json")
            sid=result.get("session_id")
            if not sid:raise AIWorkError("No browser session ID returned")
            mapping[agent]=sid
            try:
                save(mapping)
            except Exception:
                # Avoid leaving an untracked Agent Window if durable registration fails.
                try:bsk("session","stop",sid,"--json")
                except Exception:pass
                raise
            created=True
    out={"agent":agent,"session_id":sid,"created":created,"uses_existing_chrome":True}
    if url:
        try:out["navigation"]=bsk("navigate",url,"--session",sid,
                                   "--wait-until","domcontentloaded","--timeout","15s","--json")
        except Exception as exc:
            # The window remains owned by this agent for explicit retry/cleanup.
            # Report a failure, instead of a misleading successful navigation.
            raise AIWorkError(f"Agent session {sid} remains active; navigation failed: {exc}") from exc
    return out

def browser_interact(action,agent,target=None,value=None,key=None,observation=None):
    """Route browser input only to the owning agent's BrowserSkill session."""
    if action not in ("click","fill","press"):
        raise AIWorkError("Unsupported browser input operation")
    sid=session_for(agent)
    # BrowserSkill requires a fresh control receipt before each input. This is
    # a read-only synchronization; human-held windows must not be overridden.
    sync=bsk("cooperate","sync","--session",sid,"--settle","--json",timeout=40)
    state=sync.get("state") or {}
    context=sync.get("context_id")
    if not context or state.get("mode")!="ready" or state.get("editing"):
        raise AIWorkError("Browser input paused: human has control or sync returned no ready context")
    command=[action]
    if action=="press":
        if not key or len(key)>100:
            raise AIWorkError("Key combo must contain 1-100 characters")
        command.append(key)
    if target is not None:
        if not target or len(target)>2048:
            raise AIWorkError("Target selector/ref must contain 1-2048 characters")
        if re.fullmatch(r"@?e[0-9]+",target):
            if not observation:
                raise AIWorkError("Snapshot element refs require --observation from a fresh browser observe")
            command.extend(("--ref",target))
        else:
            command.extend(("--selector",target))
    if action in ("click","fill") and not target:
        raise AIWorkError("Click/fill requires --target")
    if action=="fill":
        if value is None or len(value)>100000:
            raise AIWorkError("Fill value cannot exceed 100000 characters")
        command.extend(("--value",value))
    if action=="press" and target is None:
        # Untargeted keyboard actions need the strict baseline from this sync.
        current_obs=sync.get("observation_id")
        if not current_obs or (observation and observation!=current_obs):
            raise AIWorkError("Keyboard action requires the latest synchronized observation")
        observation=current_obs
    if observation:
        if len(observation)>128:
            raise AIWorkError("Observation ID too long")
        command.extend(("--observation",observation))
    command.extend(("--session",sid,"--context",str(context),"--json"))
    return {"agent":agent,"session_id":sid,"action":action,"operation":bsk(*command)}


def stop(agent):
    agent=validate_agent(agent)
    with mutex():
        mapping=load();sid=mapping.get(agent)
        if not sid:return {"agent":agent,"already_stopped":True}
        st=bsk("status","--json")
        if sid in {x.get("session_id") for x in st.get("sessions",[])}:
            result=bsk("session","stop",sid,"--json")
            if sid not in result.get("stopped",[]):raise AIWorkError("Agent session stop failed")
        mapping.pop(agent,None);save(mapping)
    return {"agent":agent,"stopped":True,"session_id":sid}

def run():
    if len(sys.argv)>1 and sys.argv[1]=="seat":
        sys.path.insert(0,str(BASE/"seatlab"))
        from seat_host import run as seat_run
        return seat_run(sys.argv[2:])
    # Machine-wide native UI controls share the same public aiwork entry.
    # Keep this subcommand fully separate from active BrowserSkill sessions.
    if len(sys.argv)>1 and sys.argv[1]=="native":
        native_dir=BASE.parent/"scripts"/"aiwork_input"
        sys.path.insert(0,str(native_dir))
        from unified_native import main as native_main
        return native_main(sys.argv[2:])
    ap=argparse.ArgumentParser(description="On-demand multi-AI shared Chrome + Windows desktop controller")
    subs=ap.add_subparsers(dest="kind",required=True)
    subs.add_parser("status")
    subs.add_parser("seat",help="Independent X11/Wine GUI (use seat --help)")
    web=subs.add_parser("browser")
    ws=web.add_subparsers(dest="action",required=True)
    for a in ("start","navigate","observe","screenshot","stop","click","fill","press"):
        x=ws.add_parser(a);x.add_argument("--agent",required=True)
        if a=="start":
            x.add_argument("--url");x.add_argument("--browser")
        elif a=="navigate":x.add_argument("--url",required=True)
        elif a=="screenshot":x.add_argument("--output",required=True)
        elif a=="observe":
            x.add_argument("--max-tokens",type=int,default=1800)
            x.add_argument("--cursor")
        elif a in ("click","fill","press"):
            if a in ("click","fill"):x.add_argument("--target",required=True)
            else:x.add_argument("--target")
            if a=="fill":x.add_argument("--value",required=True)
            if a=="press":x.add_argument("--key",required=True)
            x.add_argument("--observation")
    d=subs.add_parser("desktop")
    ds=d.add_subparsers(dest="action",required=True)
    x=ds.add_parser("allocate");x.add_argument("--agent",required=True)
    x.add_argument("--mode",choices=("background","web","visual"),default="background")
    x=ds.add_parser("release");x.add_argument("--agent",required=True)
    x=ds.add_parser("switch");x.add_argument("number",type=int)
    x=ds.add_parser("move");x.add_argument("hwnd");x.add_argument("number",type=int)
    args=ap.parse_args()
    try:
        if args.kind=="status":out=status()
        elif args.kind=="desktop":
            if args.action=="allocate":
                out=desk("allocate","--agent",validate_agent(args.agent),"--mode",args.mode)
            elif args.action=="release":
                out=desk("release","--agent",validate_agent(args.agent))
            elif args.action=="switch":out=desk("switch",str(args.number))
            else:out=desk("move",args.hwnd,str(args.number))
        elif args.action=="start":out=start(args.agent,args.url,args.browser)
        elif args.action=="stop":out=stop(args.agent)
        elif args.action in ("click","fill","press"):
            out=browser_interact(args.action,args.agent,args.target,
                                  getattr(args,"value",None),getattr(args,"key",None),args.observation)
        else:
            sid=session_for(args.agent)
            if args.action=="navigate":
                # Navigation writes to a shared Chrome tab: require the same
                # fresh human-cooperation receipt as click/fill/press.
                sync=bsk("cooperate","sync","--session",sid,"--settle","--json",timeout=40)
                state=sync.get("state") or {}
                context=sync.get("context_id")
                if not context or state.get("mode")!="ready" or state.get("editing"):
                    raise AIWorkError("Browser navigation paused: human has control or sync returned no ready context")
                observation=sync.get("observation_id")
                if not observation:
                    raise AIWorkError("Browser navigation paused: cooperation sync returned no observation")
                out=bsk("navigate",valid_url(args.url),"--session",sid,
                        "--context",str(context),"--observation",str(observation),
                        "--wait-until","domcontentloaded","--json")
            elif args.action=="observe":
                command=["observe","--session",sid,"--max-tokens",str(args.max_tokens)]
                if args.cursor:command.extend(("--cursor",args.cursor))
                out=bsk(*command,"--json")
            else:
                path=Path(args.output)
                if not path.is_absolute() or path.suffix.lower()!=".png":
                    raise AIWorkError("Screenshot output must be an absolute PNG file path")
                path.parent.mkdir(parents=True,exist_ok=True)
                import uuid
                temp=path.with_name("."+path.stem+"-"+uuid.uuid4().hex+".png")
                try:
                    bsk("screenshot","--session",sid,"--out",str(temp),"--json")
                    if not temp.is_file() or temp.stat().st_size<8:
                        raise AIWorkError("Browser screenshot did not produce a valid PNG file")
                    with temp.open("rb") as f:
                        if f.read(8)!=b"\x89PNG\r\n\x1a\n":
                            raise AIWorkError("Browser screenshot output is not PNG")
                    os.replace(temp,path)
                    out={"output":str(path),"bytes":path.stat().st_size}
                finally:temp.unlink(missing_ok=True)
        print(json.dumps({"ok":True,"result":out},ensure_ascii=True))
        return 0
    except (AIWorkError,subprocess.TimeoutExpired,OSError,ValueError) as exc:
        print(json.dumps({"ok":False,"error":str(exc)[:800]},ensure_ascii=True))
        return 2

if __name__=="__main__":sys.exit(run())
