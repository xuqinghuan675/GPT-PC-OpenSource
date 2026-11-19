"""Owned, machine-local Chromium/CDP session. No credentials are read or copied.

Playwright lives on one asyncio loop, not a global synchronous browser lock.
Chrome is launched through the existing Runtime managed-process callback.
"""
from __future__ import annotations
import asyncio
from concurrent.futures import TimeoutError as FutureTimeout
import json
import os
from pathlib import Path
import shutil
import threading
from typing import Any, Callable, Coroutine
from urllib.parse import urlsplit
from urllib.request import ProxyHandler, build_opener
import psutil
from chatgpt_agent_store import AgentError, AgentStore
from machine_config import MachineConfig


_BROWSER_NAMES = {'chrome.exe','msedge.exe','chromium.exe','chrome','chromium','chromium-browser','google-chrome','msedge'}


def argument_value(argv: list[str], name: str) -> str | None:
    for index, value in enumerate(argv):
        if value == name and index+1 < len(argv):
            return argv[index+1].strip('"')
        if value.startswith(name+'='):
            return value[len(name)+1:].strip('"')
    return None


def normalized_path(path: str) -> str:
    return os.path.normcase(os.path.abspath(path)).rstrip('\\/')


def validate_owner_records(config: MachineConfig, processes: list[dict], listeners: list[dict]) -> dict | None:
    """Pure identity check, testable without browser/profile access."""
    expected_profile = normalized_path(config.profile_dir)
    expected_port = urlsplit(config.cdp_endpoint).port
    owners = []
    for process in processes:
        if str(process.get('name','')).lower() not in _BROWSER_NAMES:
            continue
        argv = process.get('cmdline') or []
        if argument_value(argv,'--type'):
            continue
        profile = argument_value(argv,'--user-data-dir')
        if not profile or normalized_path(profile) != expected_profile:
            continue
        port = argument_value(argv,'--remote-debugging-port')
        if str(port) != str(expected_port):
            raise AgentError('PROFILE_BUSY_OTHER_ENDPOINT','The dedicated profile is already open on a different/no CDP port')
        owners.append(process)
    if len(owners) > 1:
        raise AgentError('BROWSER_OWNER_AMBIGUOUS','Multiple browser roots claim the dedicated profile')
    relevant = [x for x in listeners if x.get('port') == expected_port]
    if any(x.get('host') not in {'127.0.0.1','::1'} for x in relevant):
        raise AgentError('CDP_NON_LOOPBACK','CDP must not listen on public/wildcard interfaces')
    if relevant and (not owners or any(x.get('pid') != owners[0]['pid'] for x in relevant)):
        raise AgentError('CDP_PORT_OWNERSHIP_MISMATCH','CDP port is occupied by a different or unverifiable process')
    if not owners:
        return None
    owner = owners[0]
    return {'pid':owner['pid'],'process_create_time':owner.get('create_time'),'executable':owner.get('exe'),
            'profile_dir':config.profile_dir,'cdp_port':expected_port,'listening':bool(relevant)}


def inspect_browser_owner(config: MachineConfig) -> dict | None:
    processes = []
    # On Windows, asking process_iter() to materialize cmdline/create_time/exe
    # for every process is extremely expensive (measured ~5s on the production
    # host).  First select the small browser candidate set using cheap attrs,
    # then query heavy identity fields only for those candidates.  The exact
    # validate_owner_records() policy below is unchanged.
    for process in psutil.process_iter(['pid','name']):
        try:
            name = str(process.info.get('name',''))
            if name.lower() not in _BROWSER_NAMES:
                continue
            try:
                executable = process.exe()
            except psutil.AccessDenied:
                executable = None
            processes.append({
                'pid': process.pid,
                'name': name,
                'cmdline': process.cmdline(),
                'create_time': process.create_time(),
                'exe': executable,
            })
        except (psutil.NoSuchProcess, psutil.AccessDenied):
            continue
    try:
        listeners = [{'pid':c.pid,'host':c.laddr.ip,'port':c.laddr.port} for c in psutil.net_connections(kind='tcp')
                     if c.status == psutil.CONN_LISTEN and c.laddr]
    except psutil.AccessDenied as exc:
        raise AgentError('CDP_OWNERSHIP_UNVERIFIABLE','Cannot verify local listening-port ownership') from exc
    return validate_owner_records(config,processes,listeners)


def set_browser_windows_visible(pid: int, visible: bool, handles: tuple[int,...] | None = None) -> tuple[int,...]:
    """Hide/show only the dedicated browser's top-level Windows windows."""
    if os.name != 'nt' or not int(pid or 0):
        return tuple()
    try:
        import ctypes
        user32=ctypes.windll.user32
        if visible:
            shown=[]
            for hwnd in handles or ():
                if not user32.IsWindow(hwnd):
                    continue
                owner=ctypes.c_ulong()
                user32.GetWindowThreadProcessId(hwnd,ctypes.byref(owner))
                if int(owner.value)!=int(pid):
                    continue
                user32.ShowWindow(hwnd,4)  # SW_SHOWNOACTIVATE
                shown.append(int(hwnd))
            return tuple(shown)
        found=[]
        callback_type=ctypes.WINFUNCTYPE(ctypes.c_bool,ctypes.c_void_p,ctypes.c_void_p)
        @callback_type
        def collect(hwnd,_lparam):
            owner=ctypes.c_ulong()
            user32.GetWindowThreadProcessId(hwnd,ctypes.byref(owner))
            if int(owner.value)==int(pid) and user32.IsWindowVisible(hwnd):
                found.append(int(hwnd))
            return True
        user32.EnumWindows(collect,0)
        for hwnd in found:
            user32.ShowWindow(hwnd,0)  # SW_HIDE
        return tuple(found)
    except (AttributeError,OSError,TypeError,ValueError):
        return tuple()


def find_browser(config: MachineConfig) -> str:
    if config.browser_executable != 'auto':
        candidate = Path(config.browser_executable)
        if not candidate.is_file():
            raise AgentError('BROWSER_EXECUTABLE_MISSING','Configured browser executable does not exist')
        return str(candidate)
    candidates = []
    for root in [os.environ.get('PROGRAMFILES'),os.environ.get('PROGRAMFILES(X86)'),os.environ.get('LOCALAPPDATA')]:
        if root:
            candidates.extend([Path(root)/'Google/Chrome/Application/chrome.exe',Path(root)/'Microsoft/Edge/Application/msedge.exe'])
    for candidate in candidates:
        if candidate.is_file():
            return str(candidate)
    for name in ['google-chrome','chromium','chromium-browser','msedge']:
        found = shutil.which(name)
        if found:
            return found
    raise AgentError('BROWSER_EXECUTABLE_MISSING','No installed Chromium browser found; set browser_executable in machine.local.toml')


def read_cdp_version(endpoint: str) -> dict:
    opener = build_opener(ProxyHandler({}))
    with opener.open(endpoint+'/json/version',timeout=2) as response:
        payload = json.loads(response.read(128*1024))
    ws = urlsplit(str(payload.get('webSocketDebuggerUrl','')))
    target = urlsplit(endpoint)
    if ws.scheme != 'ws' or ws.hostname not in {'127.0.0.1','::1','localhost'} or ws.port != target.port or ws.query or ws.fragment or ws.username:
        raise AgentError('CDP_ENDPOINT_INVALID','CDP discovery returned an unexpected debugging endpoint')
    return {'browser_version':str(payload.get('Browser',''))[:100],'protocol_version':str(payload.get('Protocol-Version',''))[:40]}


class BrowserLoop:
    """Internal transport loop, not an Agent, execution scheduler or Job runtime."""
    def __init__(self):
        self.ready = threading.Event()
        self.loop = None
        self.thread = threading.Thread(target=self._run,name='gptagent-browser-io',daemon=True)
        self.thread.start()
        if not self.ready.wait(5):
            raise AgentError('BROWSER_LOOP_START_FAILED','Browser event loop did not start')

    def _run(self):
        self.loop = asyncio.new_event_loop()
        asyncio.set_event_loop(self.loop)
        self.ready.set()
        try:
            self.loop.run_forever()
        finally:
            pending = asyncio.all_tasks(self.loop)
            for task in pending:
                task.cancel()
            if pending:
                self.loop.run_until_complete(asyncio.gather(*pending,return_exceptions=True))
            self.loop.run_until_complete(self.loop.shutdown_asyncgens())
            self.loop.close()

    def call(self, coroutine: Coroutine, *, timeout=45):
        if self.loop is None or not self.thread.is_alive():
            coroutine.close()
            raise AgentError('BROWSER_LOOP_CLOSED','Browser event loop is not running')
        future = asyncio.run_coroutine_threadsafe(coroutine,self.loop)
        try:
            return future.result(timeout)
        except FutureTimeout as exc:
            future.cancel()
            raise AgentError('BROWSER_OPERATION_TIMEOUT','Operation timed out; reconcile any durable submit intent before retrying') from exc

    def stop(self):
        if self.loop and self.thread.is_alive():
            self.loop.call_soon_threadsafe(self.loop.stop)
            if threading.current_thread() is not self.thread:
                self.thread.join(timeout=5)


class BrowserSessionManager:
    def __init__(self, config: MachineConfig, store: AgentStore, *, launch_managed: Callable,
                 control_check: Callable, owner_inspector=inspect_browser_owner, adopt_managed: Callable | None = None):
        self.config,self.store = config,store
        self.launch_managed,self.control_check,self.owner_inspector = launch_managed,control_check,owner_inspector
        self.adopt_managed = adopt_managed
        self.playwright = None
        self.browser = None
        self.context = None
        self.owner = None
        self.connect_lock = asyncio.Lock()
        self.project_lock = asyncio.Lock()
        self.conversation_locks: dict[str,asyncio.Lock] = {}
        self.hidden_window_handles: set[int] = set()
        self.disconnected = True
        self.generation = 0

    def conversation_lock(self, conversation_id: str) -> asyncio.Lock:
        return self.conversation_locks.setdefault(conversation_id,asyncio.Lock())

    async def _hide_owner_windows(self) -> tuple[int,...]:
        if not self.owner or not self.owner.get('pid'):
            return tuple()
        hidden=await asyncio.to_thread(set_browser_windows_visible,int(self.owner['pid']),False,None)
        self.hidden_window_handles.update(hidden)
        return hidden

    async def show_for_user(self, caller) -> dict:
        """Expose the dedicated login window without activating it."""
        await self.assert_owner(caller)
        shown=await asyncio.to_thread(
            set_browser_windows_visible,int(self.owner['pid']),True,tuple(self.hidden_window_handles))
        return {'status':'shown_for_user','window_count':len(shown)}

    def _profile_marker_path(self) -> Path:
        return Path(self.config.profile_dir) / '.gptagent-profile'

    def _prepare_profile_dir(self) -> None:
        """Never silently recreate an established profile after state loss."""
        profile = Path(self.config.profile_dir)
        marker = self._profile_marker_path()
        identity = self.store.setting('browser_profile_identity') or {}
        known = str(identity.get('profile_dir') or '')
        known_matches = bool(known) and normalized_path(known) == normalized_path(self.config.profile_dir)
        if profile.exists():
            if not profile.is_dir():
                raise AgentError('BROWSER_PROFILE_INVALID','Dedicated browser profile path is not a directory')
            if marker.exists():
                try:
                    marker_slug = marker.read_text(encoding='utf-8').strip()
                except OSError as exc:
                    raise AgentError('BROWSER_PROFILE_MARKER_UNREADABLE','Cannot verify the dedicated browser profile marker') from exc
                if marker_slug != self.config.machine_slug:
                    raise AgentError('BROWSER_PROFILE_MACHINE_MISMATCH','Dedicated browser profile belongs to a different machine identity')
            elif known_matches:
                raise AgentError('BROWSER_PROFILE_MARKER_MISSING','Established GPTAgent profile marker is missing; refusing to treat a cleared/replaced directory as a fresh login profile')
            return
        if known_matches:
            raise AgentError('BROWSER_PROFILE_MISSING','Established GPTAgent profile is missing; refusing to recreate it and silently lose login state')
        profile.mkdir(parents=True,exist_ok=False)

    def _establish_profile_identity(self) -> None:
        marker = self._profile_marker_path()
        if marker.exists():
            marker_slug = marker.read_text(encoding='utf-8').strip()
            if marker_slug != self.config.machine_slug:
                raise AgentError('BROWSER_PROFILE_MACHINE_MISMATCH','Dedicated browser profile belongs to a different machine identity')
        else:
            marker.write_text(self.config.machine_slug+'\n',encoding='utf-8')
        self.store.set_setting('browser_profile_identity',{
            'machine_slug':self.config.machine_slug,
            'profile_dir':self.config.profile_dir,
            'established_at':self.store.clock(),
        })

    async def ensure(self, caller, *, launch=True) -> dict:
        async with self.connect_lock:
            if self.browser and self.browser.is_connected():
                # OS identity is rechecked before mutations by assert_owner().
                await self._hide_owner_windows()
                return {'status':'connected','owner':self.owner,'generation':self.generation}
            owner = await asyncio.to_thread(self.owner_inspector,self.config)
            launched_job = None
            if owner is None:
                if not launch:
                    raise AgentError('BROWSER_NOT_STARTED','Dedicated browser is not running; no implicit startup during observation')
                self.control_check(caller.lease_id)
                executable = find_browser(self.config)
                self._prepare_profile_dir()
                port = urlsplit(self.config.cdp_endpoint).port
                argv = [executable,f'--remote-debugging-port={port}','--remote-debugging-address=127.0.0.1',
                        f'--user-data-dir={self.config.profile_dir}','--no-first-run','--no-default-browser-check',
                        '--disable-background-timer-throttling','--disable-backgrounding-occluded-windows',
                        '--disable-renderer-backgrounding','--start-minimized','https://chatgpt.com/']
                job = await asyncio.to_thread(self.launch_managed,caller,argv,self.config.agent_data_dir)
                launched_job = job
                self.store.set_setting('browser_managed_job',{k:job.get(k) for k in ('job_id','pid','process_create_time','lease_id')})
            deadline = asyncio.get_running_loop().time()+15
            startup_owner_error = None
            while True:
                try:
                    owner = await asyncio.to_thread(self.owner_inspector,self.config)
                except AgentError as exc:
                    # Windows/Chrome can expose the loopback CDP listener a few
                    # scheduler ticks before psutil exposes the matching root
                    # process.  Tolerate only that narrow race, and only after
                    # this call has just launched the dedicated browser.  All
                    # other ownership failures remain fail-closed immediately.
                    if launched_job is not None and exc.code == 'CDP_PORT_OWNERSHIP_MISMATCH':
                        startup_owner_error = exc
                        if asyncio.get_running_loop().time() < deadline:
                            await asyncio.sleep(0.25)
                            continue
                    raise
                if owner and owner.get('listening'):
                    break
                if asyncio.get_running_loop().time() >= deadline:
                    if startup_owner_error is not None:
                        raise startup_owner_error
                    raise AgentError('BROWSER_START_PENDING','Managed browser did not open its dedicated CDP port within the bounded startup window')
                await asyncio.sleep(0.25)
            if launched_job and owner['pid'] != launched_job.get('pid'):
                if self.adopt_managed is None:
                    raise AgentError('BROWSER_JOB_HANDOFF_REQUIRED','The launcher changed PID; its existing Runtime Job must adopt the verified direct child')
                adopted=await asyncio.to_thread(self.adopt_managed,caller,launched_job,owner)
                self.store.set_setting('browser_managed_job',{k:adopted.get(k) for k in ('job_id','pid','process_create_time','lease_id')})
            version = await asyncio.to_thread(read_cdp_version,self.config.cdp_endpoint)
            from playwright.async_api import async_playwright
            if self.playwright is not None:
                await self.playwright.stop()
            self.playwright = await async_playwright().start()
            try:
                self.browser = await self.playwright.chromium.connect_over_cdp(self.config.cdp_endpoint,timeout=12000,no_defaults=True)
                contexts = self.browser.contexts
                if len(contexts) != 1:
                    raise AgentError('BROWSER_CONTEXT_AMBIGUOUS','Dedicated browser must expose exactly one persistent context')
                self.context = contexts[0]
                self.context.set_default_timeout(6000)
                self.browser.on('disconnected',self._on_disconnected)
                self.owner = {**owner,**version}
                self._establish_profile_identity()
                self.disconnected = False
                self.generation += 1
                self.store.set_setting('browser_session',{'machine_slug':self.config.machine_slug,**self.owner,'connected_at':self.store.clock()})
                await self._hide_owner_windows()
            except BaseException:
                await self.playwright.stop()
                self.playwright,self.browser,self.context = None,None,None
                raise
            return {'status':'connected','owner':self.owner,'generation':self.generation,'page_count':len(self.context.pages)}

    def _on_disconnected(self, *_):
        self.disconnected = True
        self.context = None

    async def assert_owner(self, caller) -> None:
        self.control_check(caller.lease_id)
        current = await asyncio.to_thread(self.owner_inspector,self.config)
        if not current or not self.owner or current['pid'] != self.owner['pid'] or current['process_create_time'] != self.owner['process_create_time']:
            raise AgentError('BROWSER_OWNER_CHANGED','Browser process identity changed; reconnect and reconcile before a side effect')
        if self.disconnected or not self.browser or not self.browser.is_connected():
            raise AgentError('BROWSER_DISCONNECTED','The owned CDP connection is no longer live')

    def chatgpt_pages(self) -> list:
        if not self.context:
            return []
        return [p for p in self.context.pages if not p.is_closed() and urlsplit(p.url).scheme=='https' and urlsplit(p.url).hostname=='chatgpt.com']

    async def new_background_page(self, caller, url: str, *, timeout_ms: int = 15000):
        """Create an owned page while the dedicated Chrome window stays hidden."""
        await self.assert_owner(caller)
        if not self.browser or not self.context:
            raise AgentError('BROWSER_DISCONNECTED','The owned CDP connection is no longer live')
        await self._hide_owner_windows()
        page=await self.context.new_page()
        try:
            # context.new_page() steals focus only while the native Chrome
            # window is visible. The verified owner window is hidden before
            # and after page creation/navigation, preserving the user's desktop.
            await self._hide_owner_windows()
            await page.goto(url,wait_until='domcontentloaded',timeout=timeout_ms)
            await self._hide_owner_windows()
            return page
        except BaseException:
            if not page.is_closed():
                try:
                    await page.close()
                except Exception:
                    pass
            raise

    async def disconnect(self) -> None:
        # Disconnect the transport, do not close the user's owned Chrome process.
        if self.playwright:
            await self.playwright.stop()
        self.playwright,self.browser,self.context = None,None,None
        self.disconnected = True
