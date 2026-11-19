import asyncio
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from types import SimpleNamespace
import json
from pathlib import Path
import pytest
import chatgpt_browser_session as browser_module
from machine_config import MachineConfig
from chatgpt_agent_store import AgentError, AgentStore
from chatgpt_browser_session import BrowserLoop, BrowserSessionManager, argument_value, validate_owner_records
import runtime
from test_server import call_tool, isolate_runtime


@pytest.fixture
def config(tmp_path):
    return MachineConfig.from_mapping({},runtime_root=str(tmp_path))


def process(config,**updates):
    return {'pid':101,'name':'chrome.exe','create_time':1000.0,'exe':'C:/Browser/chrome.exe',
            'cmdline':['C:/Browser/chrome.exe','--remote-debugging-port=9232','--user-data-dir='+config.profile_dir],**updates}


def test_argument_flags():
    assert argument_value(['chrome','--a','value'],'--a') == 'value'
    assert argument_value(['chrome','--a="value"'],'--a') == 'value'
    assert argument_value(['chrome','--ab=value'],'--a') is None


def test_verified_owner_and_unstarted(config):
    assert validate_owner_records(config,[],[]) is None
    p = process(config)
    result = validate_owner_records(config,[p],[{'pid':101,'host':'127.0.0.1','port':9232}])
    assert result['pid'] == 101 and result['listening']
    assert 'cmdline' not in result
    assert not validate_owner_records(config,[p],[])['listening']


@pytest.mark.parametrize('host',['0.0.0.0','::','192.168.1.2'])
def test_non_loopback_listener_rejected(config,host):
    with pytest.raises(AgentError) as e:
        validate_owner_records(config,[process(config)],[{'pid':101,'host':host,'port':9232}])
    assert e.value.code == 'CDP_NON_LOOPBACK'


def test_foreign_port_profile_and_duplicate_roots_rejected(config):
    with pytest.raises(AgentError) as e:
        validate_owner_records(config,[],[{'pid':999,'host':'127.0.0.1','port':9232}])
    assert e.value.code == 'CDP_PORT_OWNERSHIP_MISMATCH'
    p = process(config)
    with pytest.raises(AgentError):
        validate_owner_records(config,[p],[{'pid':999,'host':'127.0.0.1','port':9232}])
    with pytest.raises(AgentError) as e:
        validate_owner_records(config,[process(config,cmdline=['chrome','--user-data-dir='+config.profile_dir,'--remote-debugging-port=9231'])],[])
    assert e.value.code == 'PROFILE_BUSY_OTHER_ENDPOINT'
    with pytest.raises(AgentError) as e:
        validate_owner_records(config,[p,{**p,'pid':102}],[])
    assert e.value.code == 'BROWSER_OWNER_AMBIGUOUS'
    renderer = process(config,cmdline=p['cmdline']+['--type=renderer'])
    assert validate_owner_records(config,[renderer],[]) is None


def test_async_browser_io_can_progress_two_calls_concurrently():
    loop = BrowserLoop()
    entered = []
    async def work(number):
        entered.append(number)
        for _ in range(40):
            if len(entered)==2:
                return number
            await asyncio.sleep(0.01)
        raise AssertionError('browser transport calls were globally serialized')
    try:
        with ThreadPoolExecutor(max_workers=2) as pool:
            results = list(pool.map(lambda n:loop.call(work(n)),[1,2]))
        assert results == [1,2]
    finally:
        loop.stop()
    with pytest.raises(AgentError) as e:
        loop.call(work(3))
    assert e.value.code == 'BROWSER_LOOP_CLOSED'


def test_no_implicit_browser_launch_during_observation(config):
    store = AgentStore(config)
    session = BrowserSessionManager(config,store,launch_managed=lambda *a:pytest.fail('unrequested launch'),
        control_check=lambda _:None,owner_inspector=lambda _:None)
    async def run():
        with pytest.raises(AgentError) as e:
            await session.ensure(SimpleNamespace(lease_id='lease'),launch=False)
        assert e.value.code == 'BROWSER_NOT_STARTED'
    asyncio.run(run())


def test_established_profile_is_never_silently_recreated(config):
    store = AgentStore(config)
    store.set_setting('browser_profile_identity', {
        'machine_slug': config.machine_slug,
        'profile_dir': config.profile_dir,
        'established_at': 'earlier',
    })
    session = BrowserSessionManager(config,store,launch_managed=lambda *a:pytest.fail('must not launch'),
        control_check=lambda _:None,owner_inspector=lambda _:None)
    async def run():
        with pytest.raises(AgentError) as e:
            await session.ensure(SimpleNamespace(lease_id='lease'),launch=True)
        assert e.value.code == 'BROWSER_PROFILE_MISSING'
        assert not Path(config.profile_dir).exists()
    asyncio.run(run())


def test_established_profile_rejects_missing_marker(config):
    profile = Path(config.profile_dir)
    profile.mkdir(parents=True)
    store = AgentStore(config)
    store.set_setting('browser_profile_identity', {
        'machine_slug': config.machine_slug,
        'profile_dir': config.profile_dir,
        'established_at': 'earlier',
    })
    session = BrowserSessionManager(config,store,launch_managed=lambda *a:pytest.fail('must not launch'),
        control_check=lambda _:None,owner_inspector=lambda _:None)
    async def run():
        with pytest.raises(AgentError) as e:
            await session.ensure(SimpleNamespace(lease_id='lease'),launch=True)
        assert e.value.code == 'BROWSER_PROFILE_MARKER_MISSING'
    asyncio.run(run())


def test_per_conversation_locks_are_scoped_by_identity(config):
    session = BrowserSessionManager(config,AgentStore(config),launch_managed=lambda *a:None,control_check=lambda _:None)
    assert session.conversation_lock('one') is session.conversation_lock('one')
    assert session.conversation_lock('one') is not session.conversation_lock('two')


def handoff_fixture(monkeypatch,tmp_path):
    isolate_runtime(monkeypatch,tmp_path)
    work = tmp_path/'work'
    work.mkdir()
    lease = call_tool('acquire_workspace',{'path':str(work),'mode':'execute_exclusive'})['lease_id']
    record = {'job_id':'fixture-job','pid':101,'process_create_time':1000.0,'lease_id':lease,'workspace':str(work),'lease_type':'workspace'}
    runtime._write_json(runtime._job_path('fixture-job'),record)
    state = {'parent':101,'created':1000.5,'exe':str(tmp_path/'chrome.exe'),'args':['--remote-debugging-port=9232','--user-data-dir='+str(tmp_path/'profile')]}
    class Process:
        def __init__(self,pid): self.pid=pid
        def ppid(self): return state['parent']
        def create_time(self): return state['created'] if self.pid==202 else state.get('parent_created',1000.0)
        def exe(self): return state['exe']
        def cmdline(self): return state['args']
    monkeypatch.setattr(runtime.psutil,'Process',Process)
    monkeypatch.setattr(runtime.psutil,'pid_exists',lambda _:state.get('parent_alive',False))
    monkeypatch.setattr(runtime,'_job_record_state',lambda x:{**x,'status':'running'})
    kwargs = dict(workspace_lease_id=lease,child_pid=202,expected_executable=state['exe'],expected_arguments=list(state['args']))
    return state,kwargs


def test_managed_browser_launcher_child_handoff(monkeypatch,tmp_path):
    state,kwargs = handoff_fixture(monkeypatch,tmp_path)
    got = runtime.adopt_job_child('fixture-job',**kwargs)
    assert got['pid']==202 and got['launcher_pid']==101 and got['launcher_handoff_verified']
    assert runtime.adopt_job_child('fixture-job',**kwargs)['pid']==202
    disk = runtime._read_json(runtime._job_path('fixture-job'))
    assert disk['pid']==202 and disk['process_create_time']==1000.5


@pytest.mark.parametrize('mutations',[
    {'parent':999},{'created':1031.0},{'created':999.9},{'exe':'C:/Other.exe'},
    {'args':['--wrong-profile']},{'parent_alive':True,'parent_created':1000.4},
])
def test_launcher_handoff_cannot_adopt_arbitrary_or_reused_process(monkeypatch,tmp_path,mutations):
    state,kwargs = handoff_fixture(monkeypatch,tmp_path)
    state.update(mutations)
    with pytest.raises(runtime.RuntimePolicyError):
        runtime.adopt_job_child('fixture-job',**kwargs)
    assert runtime._read_json(runtime._job_path('fixture-job'))['pid']==101


def test_launcher_handoff_requires_same_active_lease(monkeypatch,tmp_path):
    state,kwargs = handoff_fixture(monkeypatch,tmp_path)
    with pytest.raises(runtime.RuntimePolicyError):
        runtime.adopt_job_child('fixture-job',**{**kwargs,'workspace_lease_id':'foreign'})
    with pytest.raises(runtime.RuntimePolicyError):
        runtime.adopt_job_child('fixture-job',**{**kwargs,'expected_arguments':[]})


def test_managed_browser_start_tolerates_listener_process_visibility_race(monkeypatch, config):
    calls = 0
    verified_owner = {
        'pid': 101,
        'process_create_time': 1000.0,
        'executable': 'C:/Browser/chrome.exe',
        'profile_dir': config.profile_dir,
        'cdp_port': 9232,
        'listening': True,
    }

    def inspector(_config):
        nonlocal calls
        calls += 1
        if calls == 1:
            return None
        if calls == 2:
            raise AgentError('CDP_PORT_OWNERSHIP_MISMATCH', 'listener became visible before its root process')
        return dict(verified_owner)

    launched = []

    def launch(caller, argv, cwd):
        launched.append((caller.lease_id, list(argv), cwd))
        return {
            'job_id': 'browser-race-fixture',
            'pid': 101,
            'process_create_time': 1000.0,
            'lease_id': caller.lease_id,
        }

    class FakeContext:
        pages = []
        def set_default_timeout(self, _timeout):
            return None

    class FakeBrowser:
        def __init__(self):
            self.contexts = [FakeContext()]
        def is_connected(self):
            return True
        def on(self, *_args):
            return None

    class FakeChromium:
        async def connect_over_cdp(self, *_args, **_kwargs):
            return FakeBrowser()

    class FakePlaywright:
        def __init__(self):
            self.chromium = FakeChromium()
        async def stop(self):
            return None

    class FakeStarter:
        async def start(self):
            return FakePlaywright()

    import playwright.async_api as playwright_api
    monkeypatch.setattr(browser_module, 'find_browser', lambda _config: 'C:/Browser/chrome.exe')
    monkeypatch.setattr(browser_module, 'read_cdp_version', lambda _endpoint: {
        'browser_version': 'Chrome/test', 'protocol_version': '1.3'
    })
    monkeypatch.setattr(playwright_api, 'async_playwright', lambda: FakeStarter())

    store = AgentStore(config)
    session = BrowserSessionManager(
        config,
        store,
        launch_managed=launch,
        control_check=lambda _lease: None,
        owner_inspector=inspector,
    )
    caller = SimpleNamespace(lease_id='lease-race')
    result = asyncio.run(session.ensure(caller, launch=True))
    assert result['status'] == 'connected'
    assert result['owner']['pid'] == 101
    assert calls >= 3
    assert len(launched) == 1
    assert '--start-minimized' in launched[0][1]
    assert '--disable-background-timer-throttling' in launched[0][1]
    assert '--disable-backgrounding-occluded-windows' in launched[0][1]
    assert '--disable-renderer-backgrounding' in launched[0][1]
    assert (Path(config.profile_dir) / '.gptagent-profile').read_text(encoding='utf-8').strip() == config.machine_slug
    assert store.setting('browser_profile_identity')['profile_dir'] == config.profile_dir
    asyncio.run(session.disconnect())


def test_new_background_page_hides_owned_windows_around_new_page_and_navigation(config):
    store=AgentStore(config)
    session=BrowserSessionManager(config,store,launch_managed=lambda *a:None,control_check=lambda _:None)
    calls=[]
    class Page:
        def __init__(self):
            self.closed=False
        def is_closed(self): return self.closed
        async def close(self):
            calls.append('close'); self.closed=True
        async def goto(self,url,wait_until=None,timeout=None):
            calls.append(('goto',url,wait_until,timeout))

    page=Page()
    class Context:
        async def new_page(self):
            calls.append('new_page')
            return page

    session.browser=SimpleNamespace(is_connected=lambda:True)
    session.context=Context()
    async def owner(_caller): return None
    session.assert_owner=owner
    async def hide():
        calls.append('hide')
        return (101,)
    session._hide_owner_windows=hide
    result=asyncio.run(session.new_background_page(SimpleNamespace(lease_id='lease'),'https://chatgpt.com/g/project'))
    assert result is page
    assert calls == [
        'hide',
        'new_page',
        'hide',
        ('goto','https://chatgpt.com/g/project','domcontentloaded',15000),
        'hide',
    ]
    assert page.closed is False
