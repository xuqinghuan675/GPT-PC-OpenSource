import asyncio
import time
from types import SimpleNamespace
from unittest.mock import Mock

from chatgpt_web_controller import ChatGPTWebController, browser_login_required, composer_text_equivalent, wait_unique_effort_control


def test_browser_login_required_recognizes_native_auth_redirects_only():
    assert browser_login_required('https://chatgpt.com/auth/login/?next=%2Fg%2Fg-p-test') is True
    assert browser_login_required('https://chatgpt.com/auth') is True
    assert browser_login_required('https://chatgpt.com/g/g-p-0123456789abcdef0123456789abcdef-gptagent/project') is False
    assert browser_login_required('https://example.com/auth/login') is False


def test_composer_text_equivalent_accepts_contenteditable_layout_whitespace():
    expected = 'alpha\nbeta\n\ngamma'
    actual = 'alpha\n\nbeta\n\n\ngamma\u200b'
    assert composer_text_equivalent(actual, expected)


def test_composer_text_equivalent_rejects_changed_non_whitespace_content():
    assert not composer_text_equivalent('alpha beta!', 'alpha beta')
    assert not composer_text_equivalent('alpha be ta', 'alpha beta')


def test_wait_unique_effort_control_tolerates_delayed_project_hydration():
    class Candidate:
        def __init__(self):
            self.counts=[0,0,1]
            self.calls=0
        async def count(self):
            value=self.counts[min(self.calls,len(self.counts)-1)]
            self.calls += 1
            return value

    class Empty:
        async def count(self): return 0

    class Page:
        def __init__(self): self.candidate=Candidate()
        def get_by_role(self,role,**_kwargs):
            assert role=='button'
            return self.candidate
        def locator(self,selector):
            assert selector=='button[data-testid="stop-button"]:visible'
            return Empty()

    page=Page()
    result=asyncio.run(wait_unique_effort_control(page,timeout=1,poll_seconds=0))
    assert result is page.candidate
    assert page.candidate.calls==3


def test_watchdog_spawn_page_never_consumes_shared_management_composer():
    controller=ChatGPTWebController(SimpleNamespace(store=SimpleNamespace(),config=SimpleNamespace(),_notify=lambda:None),
                                    session=None,loop=None)
    calls=[]
    async def owned(_caller,key,url):
        calls.append((key,url))
        return 'dedicated-watchdog-page'
    async def forbidden(*_args,**_kwargs):
        raise AssertionError('Watchdog must not claim the shared management composer')
    controller._owned_page=owned
    controller._claim_management_project_page=forbidden
    page=asyncio.run(controller._watchdog_spawn_page(None,{'url':'https://chatgpt.com/g/project'},'wd-live'))
    assert page=='dedicated-watchdog-page'
    assert calls==[('agent_tab:wd-live','https://chatgpt.com/g/project')]

def test_conversation_watchdog_supervisor_spawns_missing_watchdog_without_touching_business_agents():
    class Store:
        def __init__(self):
            self.scope={'scope_thread_id':'scope-a','enabled':True,'mode':'conversation'}
        def watchdog_scopes(self, *, enabled_only=False):
            assert enabled_only is True
            return [dict(self.scope)]
        def watchdog_reconcile(self, *, now=None, owner_thread_id=None):
            assert owner_thread_id=='scope-a'
            return {'active':[]}
        def watchdog_rows(self, *, owner_thread_id=None, states=None):
            assert owner_thread_id=='scope-a'
            return []
        def set_watchdog_scope(self, scope_id, **updates):
            assert scope_id=='scope-a'
            self.scope.update(updates)
            return dict(self.scope)

    store=Store()
    manager=SimpleNamespace(
        store=store,
        config=SimpleNamespace(watchdog_replicas=1,spawn_interval_minutes=30),
        _notify=lambda:None,
    )
    controller=ChatGPTWebController(manager,session=None,loop=None)
    spawned=[]
    async def spawn(_caller,scope_id):
        spawned.append(scope_id)
        return {'status':'watchdog_spawned','generation':7,'agent':{'logical_agent_id':'wd7'}}
    async def forbidden(*_args,**_kwargs):
        raise AssertionError('lifecycle supervisor must never recover business agents directly')
    controller._spawn_watchdog=spawn
    controller._recover_same_conversation=forbidden

    result=asyncio.run(controller._supervisor_tick(SimpleNamespace(thread_id='service-thread')))
    assert result['mode']=='conversation'
    assert result['scope_count']==1
    assert [x['generation'] for x in result['started']]==[7]
    assert spawned==['scope-a']

def test_prune_observers_closes_disabled_watchdog_without_refreshing_graph():
    class Page:
        def __init__(self): self.closed=False
        def is_closed(self): return self.closed
        async def close(self): self.closed=True

    class Observer:
        def __init__(self):
            self.page=Page(); self.stopped=False; self.refresh_count=0
        async def stop(self): self.stopped=True
        async def refresh(self):
            self.refresh_count += 1
            return {'state':'RUNNING'}

    class Store:
        def __init__(self):
            self.agent={'logical_agent_id':'wd-stale','kind':'watchdog','state':'RUNNING',
                        'owner_thread_id':'scope-stale','watchdog_generation':2}
        def get(self, aid):
            assert aid=='wd-stale'
            return dict(self.agent)
        def watchdog_rows(self, *, owner_thread_id=None, states=None):
            assert owner_thread_id=='scope-stale'
            return [{'generation':2,'state':'CLOSED','logical_agent_id':'wd-stale'}]
        def watchdog_scope(self, scope_id):
            assert scope_id=='scope-stale'
            return {'enabled':False,'scope_thread_id':scope_id}
        def set_state(self, aid, state):
            assert aid=='wd-stale' and state=='CLOSED'
            self.agent['state']=state
            return dict(self.agent)
        def delete_setting(self, key):
            assert key=='agent_tab:wd-stale'
            return True

    store=Store()
    controller=ChatGPTWebController(SimpleNamespace(store=store,config=SimpleNamespace(),_notify=lambda:None),
                                    session=None,loop=None)
    observer=Observer()
    controller.observers={'wd-stale':observer}
    removed=asyncio.run(controller._prune_observers())

    assert removed==['wd-stale']
    assert store.agent['state']=='CLOSED'
    assert observer.stopped is True
    assert observer.page.closed is True
    assert observer.refresh_count==0
    assert controller.observers=={}


def test_prune_observers_keeps_allocated_watchdog_without_agent_generation_field():
    class Page:
        def is_closed(self): return False
        async def close(self): raise AssertionError('live Watchdog tab must not be closed')

    class Observer:
        def __init__(self): self.page=Page(); self.stopped=False
        async def stop(self): self.stopped=True

    class Store:
        def __init__(self):
            # agents has no watchdog_generation column in the real schema.
            self.agent={'logical_agent_id':'wd-live','kind':'watchdog','state':'CREATED',
                        'owner_thread_id':'scope-live'}
        def get(self, aid):
            assert aid=='wd-live'
            return dict(self.agent)
        def watchdog_rows(self, *, owner_thread_id=None, states=None):
            assert owner_thread_id=='scope-live'
            return [{'generation':9,'state':'ALLOCATED','logical_agent_id':'wd-live'}]
        def watchdog_scope(self, scope_id):
            assert scope_id=='scope-live'
            return {'enabled':True,'scope_thread_id':scope_id}
        def set_state(self, *_args, **_kwargs):
            raise AssertionError('allocated Watchdog must not be closed before join')

    store=Store()
    controller=ChatGPTWebController(SimpleNamespace(store=store,config=SimpleNamespace(),_notify=lambda:None),
                                    session=None,loop=None)
    observer=Observer()
    controller.observers={'wd-live':observer}
    removed=asyncio.run(controller._prune_observers())
    assert removed==[]
    assert controller.observers['wd-live'] is observer
    assert observer.stopped is False


def test_watchdog_supervisor_circuit_breaks_after_three_recent_failures(monkeypatch):
    now=1000.0
    monkeypatch.setattr(time,'time',lambda:now)

    class Store:
        def __init__(self):
            self.scope={'scope_thread_id':'scope-loop','enabled':True,'mode':'conversation','started_at':900.0}
            self.rows=[
                {'generation':1,'state':'FAILED','born_at':940.0,'retire_at':2000.0,'logical_agent_id':None},
                {'generation':2,'state':'FAILED','born_at':960.0,'retire_at':2000.0,'logical_agent_id':None},
                {'generation':3,'state':'FAILED','born_at':980.0,'retire_at':2000.0,'logical_agent_id':None},
            ]
        def watchdog_scopes(self, *, enabled_only=False): return [dict(self.scope)]
        def watchdog_reconcile(self, *, now=None, owner_thread_id=None): return {'active':[]}
        def watchdog_rows(self, *, owner_thread_id=None, states=None): return [dict(r) for r in self.rows]
        def set_watchdog_scope(self, scope_id, **updates):
            assert scope_id=='scope-loop'
            self.scope.update(updates)
            return dict(self.scope)

    store=Store()
    manager=SimpleNamespace(store=store,config=SimpleNamespace(watchdog_replicas=1,spawn_interval_minutes=30),
                            _notify=lambda:None,caller_resolver=lambda _lease:None)
    controller=ChatGPTWebController(manager,session=None,loop=None)
    async def forbidden(*_args,**_kwargs):
        raise AssertionError('circuit breaker must stop repeated Watchdog spawn')
    controller._spawn_watchdog=forbidden
    result=asyncio.run(controller._supervisor_tick(SimpleNamespace(thread_id='service-thread')))
    assert store.scope['enabled'] is False
    assert store.scope['manual_disabled'] is False
    assert store.scope['source']=='watchdog-spawn-circuit-breaker'
    assert result['errors'][0]['reason']=='spawn_circuit_breaker'
    assert result['errors'][0]['failed_generations']==[1,2,3]


def test_watchdog_supervisor_backs_off_single_recent_failure(monkeypatch):
    now=1000.0
    monkeypatch.setattr(time,'time',lambda:now)

    class Store:
        def __init__(self):
            self.scope={'scope_thread_id':'scope-backoff','enabled':True,'mode':'conversation','started_at':900.0}
            self.rows=[{'generation':4,'state':'FAILED','born_at':995.0,'retire_at':2000.0,'logical_agent_id':None}]
        def watchdog_scopes(self, *, enabled_only=False): return [dict(self.scope)]
        def watchdog_reconcile(self, *, now=None, owner_thread_id=None): return {'active':[]}
        def watchdog_rows(self, *, owner_thread_id=None, states=None): return [dict(r) for r in self.rows]
        def set_watchdog_scope(self, scope_id, **updates):
            self.scope.update(updates); return dict(self.scope)

    store=Store()
    manager=SimpleNamespace(store=store,config=SimpleNamespace(watchdog_replicas=1,spawn_interval_minutes=30),
                            _notify=lambda:None,caller_resolver=lambda _lease:None)
    controller=ChatGPTWebController(manager,session=None,loop=None)
    async def forbidden(*_args,**_kwargs):
        raise AssertionError('recent failure must back off before another spawn')
    controller._spawn_watchdog=forbidden
    result=asyncio.run(controller._supervisor_tick(SimpleNamespace(thread_id='service-thread')))
    assert result['errors'][0]['reason']=='spawn_backoff'
    assert result['errors'][0]['retry_after_seconds']==10.0

def test_followup_dead_recoverable_routes_mailbox_then_recovery():
    class Store:
        def __init__(self):
            self.target = {
                'logical_agent_id': 'child-agent', 'state': 'DEAD_RECOVERABLE',
                'kind': 'business', 'conversation_id': 'conversation-1',
            }
            self.queued = []

        def get(self, _agent_id):
            return self.target

        def by_thread(self, _thread_id):
            return {'logical_agent_id': 'parent-agent', 'kind': 'business'}

        def enqueue(self, sender, target, text, *, kind):
            self.queued.append((sender, target, text, kind))
            return {'message_id': 'mail-1', 'to_agent_id': target}

    class Observer:
        async def refresh(self):
            return {'state': 'DEAD_RECOVERABLE', 'recoverable': True}

    store = Store()
    manager = SimpleNamespace(
        store=store,
        config=SimpleNamespace(),
        public_agent=lambda agent: agent,
        _authorize=Mock(),
        _notify=lambda: None,
    )
    controller = ChatGPTWebController(manager, session=None, loop=None)

    async def observer(*_args, **_kwargs):
        return Observer()

    async def recover(_caller, target, *, claimed_by):
        assert target['logical_agent_id'] == 'child-agent'
        assert claimed_by == 'parent-agent'
        return {'status': 'resumed'}

    controller._observer = observer
    controller._recover_same_conversation = recover
    result = asyncio.run(controller.agent_action(
        'followup', SimpleNamespace(thread_id='parent-thread'), store.target,
        {'message': '继续检查 held-session'},
    ))

    assert result['status'] == 'resumed' and result['route'] == 'recovery'
    assert result['mailbox']['message_id'] == 'mail-1'
    manager._authorize.assert_called_once()
    assert store.queued == [('parent-agent', 'child-agent', '继续检查 held-session', 'message')]


def test_watchdog_handoff_retires_old_only_after_new_generation_is_active():
    now=time.time()
    class Store:
        def __init__(self):
            self.scope={'scope_thread_id':'scope-handoff','enabled':True,'mode':'conversation'}
            self.rows=[
                {'generation':1,'state':'ACTIVE','born_at':now-1000,'retire_at':now+1800,'replacement':None,'owner_thread_id':'scope-handoff','logical_agent_id':'wd1'},
                {'generation':2,'state':'ACTIVE','born_at':now-10,'retire_at':now+3600,'replacement':None,'owner_thread_id':'scope-handoff','logical_agent_id':'wd2'},
            ]
        def watchdog_scopes(self, *, enabled_only=False): return [dict(self.scope)]
        def watchdog_reconcile(self, *, now=None, owner_thread_id=None): return {'active':self.rows}
        def watchdog_rows(self, *, owner_thread_id=None, states=None): return [dict(r) for r in self.rows]
        def get(self, aid):
            return {'logical_agent_id':aid,'kind':'watchdog','state':'RUNNING','join_state':'joined'}
        def set_watchdog_state(self,generation,state,*,replacement=None):
            row=next(r for r in self.rows if r['generation']==generation)
            row['state']=state
            if replacement is not None: row['replacement']=replacement
            return dict(row)
        def set_watchdog_scope(self,scope_id,**updates): self.scope.update(updates); return dict(self.scope)

    store=Store()
    controller=ChatGPTWebController(SimpleNamespace(
        store=store,config=SimpleNamespace(watchdog_replicas=1,spawn_interval_minutes=30),_notify=lambda:None),
        session=None,loop=None)
    async def forbidden(*_args,**_kwargs): raise AssertionError('replacement already active; do not spawn again')
    controller._spawn_watchdog=forbidden
    result=asyncio.run(controller._supervisor_tick(SimpleNamespace(thread_id='service')))
    assert result['retiring']==[{'scope_thread_id':'scope-handoff','generation':1,'replacement':2}]
    assert store.rows[0]['state']=='RETIRING'
    assert store.rows[1]['state']=='ACTIVE'


def test_watchdog_near_retirement_spawns_new_before_retiring_old():
    now=time.time()
    class Store:
        def __init__(self):
            self.scope={'scope_thread_id':'scope-overlap','enabled':True,'mode':'conversation'}
            self.rows=[{'generation':3,'state':'ACTIVE','born_at':now-3500,'retire_at':now+20,
                        'replacement':None,'owner_thread_id':'scope-overlap','logical_agent_id':'wd3'}]
        def watchdog_scopes(self, *, enabled_only=False): return [dict(self.scope)]
        def watchdog_reconcile(self, *, now=None, owner_thread_id=None): return {'active':self.rows}
        def watchdog_rows(self, *, owner_thread_id=None, states=None): return [dict(r) for r in self.rows]
        def get(self,aid): return {'logical_agent_id':aid,'kind':'watchdog','state':'RUNNING','join_state':'joined'}
        def set_watchdog_state(self,*_args,**_kwargs): raise AssertionError('old must remain ACTIVE until new joins')
        def set_watchdog_scope(self,scope_id,**updates): self.scope.update(updates); return dict(self.scope)

    store=Store()
    controller=ChatGPTWebController(SimpleNamespace(
        store=store,config=SimpleNamespace(watchdog_replicas=1,spawn_interval_minutes=30),_notify=lambda:None),
        session=None,loop=None)
    spawned=[]
    async def spawn(_caller,scope_id):
        spawned.append(scope_id)
        return {'status':'watchdog_spawned','generation':4,'agent':{'logical_agent_id':'wd4'}}
    controller._spawn_watchdog=spawn
    result=asyncio.run(controller._supervisor_tick(SimpleNamespace(thread_id='service')))
    assert [x['generation'] for x in result['started']]==[4]
    assert result['retiring']==[]
    assert store.rows[0]['state']=='ACTIVE'
    assert spawned==['scope-overlap']


def test_bootstrap_uses_fast_first_turn_without_weakening_identity_rules(tmp_path):
    workspace=tmp_path/'plain-workspace'
    workspace.mkdir()
    manager=SimpleNamespace(
        store=SimpleNamespace(),
        config=SimpleNamespace(
            connector_name='GPT-PC',project_name='GPTAgent',runtime_root=r'C:\GPT-PC',
            turn_rotation_minutes=60,
        ),
    )
    controller=ChatGPTWebController(manager,session=None,loop=None)
    agent={
        'agent_path':'/root/perf-smoke','logical_agent_id':'ag_perf','web_title':'ignored',
        'workspace':str(workspace),
    }
    prompt=controller._bootstrap(agent,'ga_ticket_test','修复 duration.py 并测试','隔离真实性能测试')

    assert '并行读取' in prompt
    assert '自己的 execute_exclusive 租约' in prompt
    assert 'spawn_ticket="ga_ticket_test"' in prompt
    assert '首次 join 后直接执行任务，不要 resume_context' in prompt
    assert '普通非 Git 目录不要调用 git_status' in prompt
    assert '只有真实 liveness probe 才先 report_progress' in prompt
    assert '收到 Runtime 存活确认立即 report_progress' not in prompt
    assert '切 Pro' in prompt and 'no-duplicate' in prompt

    shared=controller._bootstrap(
        agent,'ga_ticket_shared','修复 duration.py 并测试','同树并行修复',
        shared_lease_id='a'*32,
    )
    assert '不要 acquire_workspace' in shared
    assert 'workspace_lease_id="' + ('a'*32) + '"' in shared
    assert 'participant_workspace_lease_id' in shared
    assert '不是第二把 workspace 锁' in shared
    assert 'done 只退出当前 participant，不释放共享 Root workspace 资源租约' in shared
    assert '自己的 execute_exclusive 租约' not in shared

    auto=controller._bootstrap(
        agent,None,'修复 duration.py 并测试','同树自动绑定验收',
        shared_lease_id='a'*32,participant_workspace_lease_id='b'*32,
    )
    assert 'participant_workspace_lease_id="' + ('b'*32) + '"' in auto
    assert 'spawn_ticket=' not in auto
    assert '不要自行调用 join' in auto
    assert 'Controller 会在原生 Conversation/project/user-message 证明成立后' in auto
    assert '自己的 execute_exclusive 租约' not in auto


def test_watchdog_bootstrap_uses_independent_lease_and_semantic_guards(tmp_path):
    workspace=tmp_path/'watchdog'
    workspace.mkdir()
    manager=SimpleNamespace(store=SimpleNamespace(),config=SimpleNamespace(
        connector_name='GPT-PC',project_name='GPTAgent',runtime_root=r'C:\GPT-PC',
        turn_rotation_minutes=60))
    controller=ChatGPTWebController(manager,session=None,loop=None)
    agent={'agent_path':'/watchdog/wd-007','logical_agent_id':'ag_wd7','workspace':str(workspace)}
    prompt=controller._watchdog_bootstrap(agent,'ga_ticket_watchdog','scope-root',7)
    assert '自己的 execute_exclusive 租约' in prompt
    assert 'scope_thread_id=scope-root' in prompt
    assert 'generation=7' in prompt
    assert 'followup' in prompt and 'message' in prompt
    assert 'DONE/CLOSED/BLOCKED/EXTERNAL_ACTIVITY' in prompt
    assert '不直接修改业务文件' in prompt
    assert '禁止跨 scope' in prompt


def test_watchdog_idle_followup_uses_semantic_native_followup_not_plain_recovery():
    class Store:
        def __init__(self):
            self.target={'logical_agent_id':'business-1','state':'IDLE','kind':'business','conversation_id':'c1'}
        def get(self,_aid): return self.target
        def by_thread(self,_thread_id): return {'logical_agent_id':'wd-1','kind':'watchdog'}
    class Observer:
        async def refresh(self): return {'state':'IDLE','recoverable':False}
    store=Store()
    authorize=Mock()
    controller=ChatGPTWebController(SimpleNamespace(store=store,config=SimpleNamespace(),_notify=lambda:None,
        _authorize=authorize),session=None,loop=None)
    async def observer(*_args,**_kwargs): return Observer()
    submitted=[]
    async def submit(kind,_caller,_target,text,_observer):
        submitted.append((kind,text))
        return {'status':'submitted'}
    async def forbidden(*_args,**_kwargs): raise AssertionError('IDLE Watchdog followup must keep semantic text')
    controller._observer=observer
    controller._submit_existing=submit
    controller._recover_same_conversation=forbidden
    result=asyncio.run(controller.agent_action('followup',SimpleNamespace(thread_id='watchdog-thread'),store.target,
        {'message':'继续检查 Goal 是否完成，并按 checkpoint 接着做'}))
    assert result['status']=='submitted'
    assert submitted==[('followup','继续检查 Goal 是否完成，并按 checkpoint 接着做')]
    authorize.assert_called_once()


def test_watchdog_lifecycle_scope_filter_never_spawns_for_other_top_level_scope():
    class Store:
        def __init__(self):
            self.scopes=[
                {'scope_thread_id':'scope-a','enabled':True,'mode':'conversation'},
                {'scope_thread_id':'scope-b','enabled':True,'mode':'conversation'},
            ]
        def watchdog_scopes(self, *, enabled_only=False): return [dict(x) for x in self.scopes]
        def watchdog_reconcile(self, *, now=None, owner_thread_id=None): return {'active':[]}
        def watchdog_rows(self, *, owner_thread_id=None, states=None): return []
        def set_watchdog_scope(self,scope_id,**updates):
            row=next(x for x in self.scopes if x['scope_thread_id']==scope_id); row.update(updates); return dict(row)
    store=Store()
    controller=ChatGPTWebController(SimpleNamespace(store=store,
        config=SimpleNamespace(watchdog_replicas=1,spawn_interval_minutes=30),_notify=lambda:None),session=None,loop=None)
    spawned=[]
    async def spawn(_caller,scope_id):
        spawned.append(scope_id)
        return {'status':'watchdog_spawned','generation':9,'agent':{'logical_agent_id':'wd9'}}
    controller._spawn_watchdog=spawn
    result=asyncio.run(controller._supervisor_tick(SimpleNamespace(thread_id='service'),scope_filter={'scope-a'}))
    assert result['scope_count']==1
    assert spawned==['scope-a']


def test_observer_never_adopts_unowned_same_conversation_tab():
    class Store:
        def setting(self,key,default=None): return None
    class Session:
        context=object()
        def exact_conversation_page(self,*_args,**_kwargs):
            raise AssertionError('automatic observer must not adopt arbitrary same-conversation tabs')
    controller=ChatGPTWebController(SimpleNamespace(store=Store(),config=SimpleNamespace(),_notify=lambda:None),
                                    session=Session(),loop=None)
    async def no_target(_target): return None
    controller._page_by_target=no_target
    agent={'logical_agent_id':'agent-owned-only','conversation_id':'conversation-user-opened',
           'conversation_url':'https://chatgpt.com/c/conversation-user-opened'}
    from chatgpt_agent_store import AgentError
    try:
        asyncio.run(controller._observer(agent,reopen=False))
        raise AssertionError('detached managed page must fail closed')
    except AgentError as exc:
        assert exc.code=='AGENT_TAB_DETACHED'


def test_reconcile_web_is_idempotent_after_terminalization():
    class Store:
        def get(self, aid):
            assert aid=='agent-terminal-web'
            return {'logical_agent_id':aid,'state':'CLOSED','conversation_id':'conv-terminal'}
        def update_web_operation(self,*_args,**_kwargs):
            raise AssertionError('terminalized web operation must not be CAS-advanced again')
    class Observer:
        intent_request_id=None
        nodes={}
        evidence=SimpleNamespace(turn_exchange_id='tx',user_message_id='user',request_id='req')
        async def refresh(self): return {'state':'CLOSED'}
        def public(self): return {'state':'CLOSED'}
    manager=SimpleNamespace(store=Store(),config=SimpleNamespace(),_notify=lambda:None,
                            public_agent=lambda agent:{'logical_agent_id':agent['logical_agent_id'],'state':agent['state']})
    controller=ChatGPTWebController(manager,session=None,loop=None)
    result=asyncio.run(controller._reconcile_web({
        'operation_id':'web-terminal','agent_id':'agent-terminal-web','state':'BLOCKED','prompt_sha256':'x'},Observer()))
    assert result['status']=='terminated'
    assert result['operation_id']=='web-terminal'


def test_prune_agent_tab_settings_scans_browser_targets_once_per_tick():
    class Page:
        def __init__(self,target): self.target=target; self.closed=False
        def is_closed(self): return self.closed
        async def close(self): self.closed=True
    pages=[Page('target-1'),Page('target-2'),Page('target-3')]
    class Session:
        context=object()
        disconnected=False
        def chatgpt_pages(self): return list(pages)
    class Store:
        def __init__(self):
            self.deleted=[]
            self.settings={f'agent_tab:agent-{i}':'target-1' for i in range(30)}
        def settings_prefix(self,prefix): return dict(self.settings)
        def delete_setting(self,key): self.deleted.append(key); self.settings.pop(key,None); return True
        def get(self,aid): return {'logical_agent_id':aid,'state':'RUNNING','kind':'business'}
    store=Store(); calls=[]
    controller=ChatGPTWebController(SimpleNamespace(store=store,config=SimpleNamespace(),_notify=lambda:None),Session(),None)
    async def target_id(page):
        calls.append(page.target)
        return page.target
    controller._target_id=target_id
    removed=asyncio.run(controller._prune_agent_tab_settings())
    assert removed==[]
    assert calls==['target-1','target-2','target-3']


def test_fresh_spawn_page_preserves_old_draft_tab(monkeypatch):
    import chatgpt_web_controller as controller_module

    project_id = "g-p-0123456789abcdef0123456789abcdef"
    project_url = f"https://chatgpt.com/g/{project_id}-gptagent/project"

    class OldPage:
        def __init__(self): self.closed = False
        def is_closed(self): return self.closed

    class OldObserver:
        def __init__(self):
            self.page = OldPage()
            self.stopped = False
        async def stop(self):
            self.stopped = True

    class Locator:
        async def wait_for(self, **_kwargs): return None

    class NewPage:
        def __init__(self):
            self.url = project_url
            self.closed = False
        def locator(self, selector):
            assert selector == "#prompt-textarea"
            return Locator()
        async def evaluate(self, _expr):
            return {"composer_nonempty": False}
        def is_closed(self): return self.closed
        async def close(self): self.closed = True

    class Session:
        def __init__(self): self.page = NewPage()
        async def new_background_page(self, _caller, url):
            assert url == project_url
            return self.page

    class Store:
        def __init__(self):
            self.deleted = []
            self.settings = {}
        def delete_setting(self, key):
            self.deleted.append(key)
        def set_setting(self, key, value):
            self.settings[key] = value
        def get(self, aid):
            return {"logical_agent_id": aid, "project_id": project_id, "conversation_id": None}

    class FakeObserver:
        def __init__(self, page, store, aid, on_change=None):
            self.page, self.store, self.aid = page, store, aid
        async def start(self):
            return self

    store = Store()
    manager = SimpleNamespace(store=store, config=SimpleNamespace(), _notify=lambda:None)
    controller = ChatGPTWebController(manager, session=Session(), loop=None)
    old = OldObserver()
    controller.observers["agent-one"] = old
    async def target(_page): return "new-target"
    controller._target_id = target
    monkeypatch.setattr(controller_module, "PageObserver", FakeObserver)

    page, observer = asyncio.run(controller._fresh_spawn_page_preserving_existing(
        SimpleNamespace(), {"url": project_url, "project_id": project_id}, "agent-one"
    ))
    assert old.stopped is True
    assert old.page.closed is False
    assert store.deleted == ["agent_tab:agent-one"]
    assert store.settings["agent_tab:agent-one"] == "new-target"
    assert page.closed is False
    assert observer.page is page
