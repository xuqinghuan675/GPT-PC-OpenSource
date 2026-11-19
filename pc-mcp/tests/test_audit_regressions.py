"""Regressions found by exercising a new native top-level conversation."""
from concurrent.futures import ThreadPoolExecutor
from types import SimpleNamespace

import pytest

from chatgpt_agent_store import AgentError, AgentStore
from chatgpt_web_controller import ChatGPTWebController
from machine_config import MachineConfig


@pytest.fixture
def store(tmp_path):
    return AgentStore(MachineConfig.from_mapping({}, runtime_root=str(tmp_path)))


def root(store, owner, name='module-audit'):
    return store.create(name=name, role='Independent module audit',
                        workspace=store.config.default_work_root,
                        project_id='project-fixture', owner_thread_id=owner,
                        delegation_reason='Independent module with disjoint write ownership')


def test_new_native_controllers_never_reuse_another_roots_identity(store):
    first = root(store, 'controller-original')
    later = root(store, 'controller-new')
    assert first['agent_path'] == '/root'
    assert later['agent_path'] != first['agent_path']
    assert later['root_agent_id'] == later['logical_agent_id']
    assert later['parent_agent_id'] is None
    assert store.get('/root') == first
    assert AgentStore(store.config).get(later['agent_path']) == later


def test_native_controller_parallel_worker_cap_is_atomic(store):
    def attempt(index):
        try:
            return root(store, 'controller-parallel', f'module-audit-{index}')
        except AgentError as exc:
            return exc.code
    with ThreadPoolExecutor(max_workers=6) as executor:
        results = list(executor.map(attempt, range(6)))
    assert sum(isinstance(result, dict) for result in results) == 3
    assert results.count('PARALLEL_CHILD_LIMIT') == 3
    completed = next(result for result in results if isinstance(result, dict))
    store.set_state(completed['logical_agent_id'], 'CLOSED')
    assert root(store, 'controller-parallel', 'replacement-audit')['state'] == 'CREATED'


def test_watchdog_bootstrap_identifies_its_exact_independent_workspace(tmp_path):
    workspace = tmp_path / 'watchdog-generation-7'
    workspace.mkdir()
    manager = SimpleNamespace(store=None, config=SimpleNamespace(
        connector_name='GPT-PC', project_name='GPTAgent',
        runtime_root=str(tmp_path), turn_rotation_minutes=60))
    controller = ChatGPTWebController(manager, session=None, loop=None)
    agent = {'agent_path': '/watchdog/wd-007', 'logical_agent_id': 'ag_wd7',
             'workspace': str(workspace)}
    prompt = controller._watchdog_bootstrap(agent, 'ga_ticket_fixture', 'controller-scope', 7)
    assert str(workspace) in prompt
    assert '自己的 execute_exclusive 租约' in prompt


def test_shared_admin_participant_cannot_control_its_parent(store):
    import uuid
    from chatgpt_agent import AgentManager, Caller
    parent = root(store, 'native-admin')
    child = store.create(name='child-auth-audit', role='Review a disjoint module',
                         workspace=store.config.default_work_root,
                         project_id=parent['project_id'], owner_thread_id='native-admin',
                         parent_id=parent['logical_agent_id'],
                         delegation_reason='Independent audit with restricted tree control')
    cid = str(uuid.uuid4())
    store.bind_conversation(child['logical_agent_id'], cid, 'https://chatgpt.com/c/' + cid, child['project_id'])
    store.join(store.issue_ticket(child['logical_agent_id']), runtime_thread_id='child-thread',
               project_id=child['project_id'], conversation_id=cid, browser_verified=True)
    manager = AgentManager(store.config, caller_resolver=lambda _: None,
                           control_check=lambda _: None, runtime_facts=lambda _: {}, store=store)
    caller = Caller('a' * 32, 'child-thread', child['workspace'], 'execute_exclusive',
                    'runtime_admin', lease_role='participant', shared_root_lease_id='b' * 32)
    with pytest.raises(AgentError) as error:
        manager._authorize(caller, parent, 'close')
    assert error.value.code == 'AGENT_TREE_FORBIDDEN'
    assert manager._authorize(caller, store.get(child['logical_agent_id']), 'checkpoint') is not None


def test_attach_never_creates_participant_from_expired_root(monkeypatch):
    from contextlib import nullcontext
    import runtime
    expired = {'lease_id': 'a' * 32, 'thread_id': 'old-root', 'workspace': 'unused',
               'mode': 'execute_exclusive', 'lease_type': 'workspace',
               'expires_at': '2000-01-01T00:00:00Z'}
    monkeypatch.setattr(runtime, '_metadata_lock', nullcontext)
    monkeypatch.setattr(runtime, '_read_mode_lease_record_locked', lambda _: dict(expired))
    monkeypatch.setattr(runtime, '_lease_records_locked', lambda: [])
    def forbidden_write(*_args, **_kwargs):
        raise AssertionError('An expired Root must not produce a persisted participant')
    monkeypatch.setattr(runtime, '_write_json', forbidden_write)
    with pytest.raises(runtime.RuntimePolicyError):
        runtime.attach_workspace_participant('a' * 32, logical_agent_id='ag_fixture', owner_label='Child')


def test_completed_native_scope_does_not_spawn_watchdogs_after_restart(store):
    import asyncio
    from chatgpt_agent import AgentManager
    store.set_watchdog_scope('finished-native', enabled=True, manual_disabled=False,
                             mode='program', root_lease_id='expired-native-lease')
    manager = AgentManager(store.config, caller_resolver=lambda _: None,
                           control_check=lambda _: None, runtime_facts=lambda _: {}, store=store)
    controller = ChatGPTWebController(manager, None, None)
    attempted = []
    async def fake_spawn(caller, scope):
        attempted.append(scope)
        return {'generation': 1, 'status': 'watchdog_spawned'}
    controller._spawn_watchdog = fake_spawn
    result = asyncio.run(controller._supervisor_tick(None))
    assert attempted == []
    assert store.watchdog_scope('finished-native')['enabled'] is False
    assert store.watchdog_scope('finished-native')['manual_disabled'] is False


def make_manager(store):
    from chatgpt_agent import AgentManager
    return AgentManager(store.config, caller_resolver=lambda _: None,
                        control_check=lambda _: None, runtime_facts=lambda _: {}, store=store)


def join_as(store, agent, thread):
    import uuid
    cid = str(uuid.uuid4())
    store.bind_conversation(agent['logical_agent_id'], cid, 'https://chatgpt.com/c/' + cid, agent['project_id'])
    return store.join(store.issue_ticket(agent['logical_agent_id']), runtime_thread_id=thread,
                      project_id=agent['project_id'], conversation_id=cid, browser_verified=True)


def test_ultra_filters_terminal_history_before_limiting(store):
    from chatgpt_agent import Caller
    manager = make_manager(store)
    for i in range(store.config.max_live_agents + 2):
        agent = root(store, f'past-owner-{i}', f'past-work-{i}')
        store.assign_supervisor_scope(agent['logical_agent_id'], 'native-scope')
        store.set_state(agent['logical_agent_id'], 'CLOSED')
    live = root(store, 'current-owner', 'current-worker')
    store.assign_supervisor_scope(live['logical_agent_id'], 'native-scope')
    result = manager.ultra_utilization(Caller('lease', 'native-scope', live['workspace'], 'execute_exclusive', 'workspace'),
                                       successful_actions=8, goal_active=True)
    assert result['launching_workers'] == 0
    assert result['stalled_unjoined_workers'] == 1
    assert result['parallel_capacity_remaining'] == 3
    assert result['delegation_review_needed'] is True


def test_ultra_capacity_is_direct_children_not_all_descendants(store):
    from chatgpt_agent import Caller
    manager = make_manager(store)
    parent = root(store, 'native-scope')
    store.assign_supervisor_scope(parent['logical_agent_id'], 'native-scope')
    parent = join_as(store, parent, 'coordinator-thread')
    child = store.create(name='direct-worker', role='module', workspace=parent['workspace'], project_id=parent['project_id'],
                         owner_thread_id='coordinator-thread', parent_id=parent['logical_agent_id'], delegation_reason='Independent module work')
    store.assign_supervisor_scope(child['logical_agent_id'], 'native-scope')
    for i in range(3):
        grandchild = store.create(name=f'grandchild-{i}', role='module', workspace=parent['workspace'], project_id=parent['project_id'],
                                 owner_thread_id='child-thread', parent_id=child['logical_agent_id'], delegation_reason='Independent grandchild work')
        store.assign_supervisor_scope(grandchild['logical_agent_id'], 'native-scope')
    result = manager.ultra_utilization(Caller('lease', 'coordinator-thread', parent['workspace'], 'execute_exclusive', 'workspace'),
                                       successful_actions=8, goal_active=True)
    assert result['live_business_agents'] == 4
    assert result['launching_workers'] == 0
    assert result['stalled_unjoined_workers'] == 4
    assert result['direct_live_workers'] == 0
    assert result['parallel_capacity_remaining'] == 3


def test_native_root_participant_inherits_watchdog_and_cannot_toggle(store):
    from chatgpt_agent import Caller
    agent = join_as(store, root(store, 'native-scope'), 'native-worker-thread')
    caller = Caller('lease', 'native-worker-thread', agent['workspace'], 'execute_exclusive', 'runtime_admin',
                    lease_role='participant', shared_root_lease_id='parent-lease')
    assert make_manager(store).caller_is_child_agent(caller) is True


@pytest.mark.parametrize('inactive', ['stopped', 'RETIRING', 'FAILED'])
def test_inactive_watchdog_cannot_continue_mutating_business_tree(store, inactive):
    from chatgpt_agent import Caller
    manager = make_manager(store)
    scope = 'native-owner'
    target = root(store, scope)
    store.assign_supervisor_scope(target['logical_agent_id'], scope)
    generation = store.allocate_watchdog(scope)['generation']
    watcher = store.create(name=f'watchdog-{generation:03d}', role='watchdog', workspace=store.config.default_work_root,
                           project_id=target['project_id'], owner_thread_id=scope, kind='watchdog', watchdog_generation=generation)
    store.assign_supervisor_scope(watcher['logical_agent_id'], scope)
    store.bind_watchdog(generation, watcher['logical_agent_id'])
    watcher = join_as(store, watcher, 'watchdog-runtime')
    store.activate_watchdog(watcher['logical_agent_id'])
    store.set_watchdog_scope(scope, enabled=True, manual_disabled=False)
    caller = Caller('watchdog-lease', 'watchdog-runtime', watcher['workspace'], 'execute_exclusive', 'workspace')
    assert manager._authorize(caller, target, 'followup') is not None
    if inactive == 'stopped':
        store.set_watchdog_scope(scope, enabled=False, manual_disabled=True)
    else:
        store.set_watchdog_state(generation, inactive)
    with pytest.raises(AgentError) as error:
        manager._authorize(caller, target, 'followup')
    assert error.value.code == 'WATCHDOG_INACTIVE'
    assert manager._authorize(caller, target, 'inspect') is not None


def test_only_one_pending_watchdog_can_be_allocated_per_scope(store):
    def attempt(_):
        try:
            return store.allocate_watchdog('same-scope')
        except AgentError as exc:
            return exc.code
    with ThreadPoolExecutor(max_workers=4) as executor:
        results = list(executor.map(attempt, range(4)))
    assert sum(isinstance(result, dict) for result in results) == 1
    assert results.count('WATCHDOG_ALLOCATION_IN_PROGRESS') == 3
    assert len(store.watchdog_rows(owner_thread_id='same-scope', states=('ALLOCATED',))) == 1


def test_parallel_spawns_claim_distinct_warm_composer(store):
    import asyncio
    async def scenario():
        session = SimpleNamespace(project_lock=asyncio.Lock())
        async def assert_owner(_):
            await asyncio.sleep(.001)
        session.assert_owner = assert_owner
        controller = ChatGPTWebController(make_manager(store), session, None)
        page = SimpleNamespace(url='https://chatgpt.com/g/g-p-fixture/project')
        async def evaluate(_expression):
            await asyncio.sleep(.001)
            return {'composer_nonempty': False}
        page.evaluate = evaluate
        store.set_setting('management_target_id', 'warm-tab')
        async def by_target(target):
            await asyncio.sleep(.001)
            return page if target == 'warm-tab' else None
        async def target_id(_):
            await asyncio.sleep(.001)
            return 'warm-tab'
        controller._page_by_target = by_target
        controller._target_id = target_id
        result = await asyncio.gather(*(controller._claim_management_project_page(None, {'url': page.url}, f'agent-{i}') for i in range(3)))
        assert sum(p is page for p in result) == 1
        assert store.setting('management_target_id') is None
        claimed_tabs = [store.setting(f'agent_tab:agent-{i}') for i in range(3)]
        assert claimed_tabs.count('warm-tab') == 1
    asyncio.run(scenario())


def test_management_warm_page_with_foreign_draft_is_preserved(store):
    import asyncio
    async def scenario():
        session = SimpleNamespace(project_lock=asyncio.Lock())
        async def forbidden_owner(_):
            raise AssertionError('dirty warm page must not be claimed')
        session.assert_owner = forbidden_owner
        controller = ChatGPTWebController(make_manager(store), session, None)
        page = SimpleNamespace(url='https://chatgpt.com/g/g-p-fixture/project', draft='user draft must survive')
        async def evaluate(_expression):
            return {'composer_nonempty': True}
        page.evaluate = evaluate
        store.set_setting('management_target_id', 'warm-tab')
        async def by_target(target):
            return page if target == 'warm-tab' else None
        async def forbidden_target_id(_):
            raise AssertionError('dirty warm page must not be rebound')
        controller._page_by_target = by_target
        controller._target_id = forbidden_target_id
        result = await controller._claim_management_project_page(None, {'url': page.url}, 'agent-draft')
        assert result is None
        assert store.setting('management_target_id') == 'warm-tab'
        assert store.setting('agent_tab:agent-draft') is None
        assert page.draft == 'user draft must survive'
    asyncio.run(scenario())


@pytest.mark.parametrize('snapshot_mode', ['raises', 'none'])
def test_management_warm_page_unreadable_snapshot_fails_closed(store, snapshot_mode):
    import asyncio
    async def scenario():
        session = SimpleNamespace(project_lock=asyncio.Lock())
        async def forbidden_owner(_):
            raise AssertionError('unverified warm page must not be claimed')
        session.assert_owner = forbidden_owner
        controller = ChatGPTWebController(make_manager(store), session, None)
        page = SimpleNamespace(url='https://chatgpt.com/g/g-p-fixture/project')
        async def evaluate(_expression):
            if snapshot_mode == 'raises':
                raise RuntimeError('snapshot unavailable')
            return None
        page.evaluate = evaluate
        store.set_setting('management_target_id', 'warm-tab')
        async def by_target(target):
            return page if target == 'warm-tab' else None
        async def forbidden_target_id(_):
            raise AssertionError('unverified warm page must not be rebound')
        controller._page_by_target = by_target
        controller._target_id = forbidden_target_id
        result = await controller._claim_management_project_page(None, {'url': page.url}, 'agent-unverified')
        assert result is None
        assert store.setting('management_target_id') == 'warm-tab'
        assert store.setting('agent_tab:agent-unverified') is None
    asyncio.run(scenario())


def test_dirty_warm_page_falls_back_to_dedicated_owned_page(store):
    import asyncio
    async def scenario():
        session = SimpleNamespace(project_lock=asyncio.Lock())
        async def assert_owner(_):
            return None
        session.assert_owner = assert_owner
        controller = ChatGPTWebController(make_manager(store), session, None)
        dirty = SimpleNamespace(url='https://chatgpt.com/g/g-p-fixture/project', draft='foreign draft')
        clean = SimpleNamespace(url=dirty.url)
        async def dirty_evaluate(_expression):
            return {'composer_nonempty': True}
        dirty.evaluate = dirty_evaluate
        store.set_setting('management_target_id', 'warm-tab')
        async def by_target(target):
            if target == 'warm-tab':
                return dirty
            if target == 'agent-tab':
                return clean
            return None
        created = []
        async def new_background_page(_caller, url):
            created.append(url)
            return clean
        session.new_background_page = new_background_page
        async def target_id(page):
            return 'warm-tab' if page is dirty else 'agent-tab'
        controller._page_by_target = by_target
        controller._target_id = target_id
        claimed = await controller._claim_management_project_page(None, {'url': dirty.url}, 'agent-fallback')
        assert claimed is None
        owned = await controller._owned_page(None, 'agent_tab:agent-fallback', dirty.url)
        assert owned is clean
        assert created == [dirty.url]
        assert store.setting('management_target_id') == 'warm-tab'
        assert store.setting('agent_tab:agent-fallback') == 'agent-tab'
        assert dirty.draft == 'foreign draft'
    asyncio.run(scenario())


@pytest.mark.parametrize('disable_during_project', [False, True])
def test_watchdog_disabled_scope_cannot_allocate_after_slow_setup(store, disable_during_project):
    import asyncio
    scope = 'disable-during-start'
    store.set_watchdog_scope(scope, enabled=disable_during_project, manual_disabled=not disable_during_project)
    async def ensure(*_args, **_kwargs):
        return None
    async def project(_caller):
        store.set_watchdog_scope(scope, enabled=False, manual_disabled=True)
        return {'project': {'project_id': 'project-fixture'}}
    controller = ChatGPTWebController(make_manager(store), SimpleNamespace(ensure=ensure), None)
    controller.ensure_project = project
    with pytest.raises(AgentError) as error:
        asyncio.run(controller._spawn_watchdog(None, scope))
    assert error.value.code == 'WATCHDOG_INACTIVE'
    assert store.watchdog_rows(owner_thread_id=scope) == []


def test_slow_watchdog_spawn_never_blocks_observer_monitor(store):
    import asyncio
    async def scenario():
        store.set_watchdog_scope('slow-lifecycle', enabled=True, manual_disabled=False)
        controller = ChatGPTWebController(make_manager(store), None, None, service_caller=lambda: None)
        entered = asyncio.Event()
        refreshed_again = asyncio.Event()
        hold = asyncio.Event()
        class Observer:
            async def refresh(self):
                if entered.is_set():
                    refreshed_again.set()
            async def stop(self):
                return None
        async def slow_tick(_caller):
            entered.set()
            await hold.wait()
        controller.observers['fake-observer'] = Observer()
        controller._supervisor_tick = slow_tick
        controller.monitor_task = asyncio.create_task(controller._monitor())
        try:
            await asyncio.wait_for(entered.wait(), 1)
            await asyncio.wait_for(refreshed_again.wait(), 2.8)
        finally:
            await controller._shutdown_background()
        assert not controller.background_tasks
    asyncio.run(scenario())


def test_stale_watchdog_bootstrap_requires_full_reconstruction_and_terminal_state(store, tmp_path):
    workspace = tmp_path / 'watchdog-generation-proof'
    workspace.mkdir()
    scope_id = 'scope-stale-proof'
    allocation = store.allocate_watchdog(scope_id)
    generation = allocation['generation']
    agent = store.create(
        name='watchdog-proof', role='Supervise one Runtime conversation',
        workspace=str(workspace), project_id='project-fixture', owner_thread_id=scope_id,
        kind='watchdog', watchdog_generation=generation,
    )
    store.bind_watchdog(generation, agent['logical_agent_id'])
    store.prepare_web_operation(agent['logical_agent_id'], 'stale-proof-operation', 'spawn')
    raw_ticket = store.issue_ticket(agent['logical_agent_id'])
    controller = ChatGPTWebController(SimpleNamespace(store=store, config=store.config), session=None, loop=None)
    prompt = controller._watchdog_bootstrap(agent, raw_ticket, scope_id, generation)

    # Live/allocated Watchdogs and merely GPTAgent-looking text are never disposable.
    assert controller._stale_bootstrap_draft_owner(prompt, 'project-fixture') is None
    assert controller._stale_bootstrap_draft_owner('user draft ga_ticket_not_enough', 'project-fixture') is None

    store.set_watchdog_state(generation, 'FAILED')
    assert controller._stale_bootstrap_draft_owner(prompt, 'project-fixture') == agent['logical_agent_id']

    # Even a terminal Watchdog is not disposable once it is joined or bound.
    with store.transaction() as c:
        c.execute('UPDATE agents SET join_state=? WHERE logical_agent_id=?', ('joined', agent['logical_agent_id']))
    assert controller._stale_bootstrap_draft_owner(prompt, 'project-fixture') is None
    with store.transaction() as c:
        c.execute('UPDATE agents SET join_state=?,conversation_id=? WHERE logical_agent_id=?', ('pending', 'conversation-seen', agent['logical_agent_id']))
    assert controller._stale_bootstrap_draft_owner(prompt, 'project-fixture') is None
    with store.transaction() as c:
        c.execute('UPDATE agents SET conversation_id=NULL WHERE logical_agent_id=?', (agent['logical_agent_id'],))
    assert controller._stale_bootstrap_draft_owner(prompt, 'project-fixture') == agent['logical_agent_id']

    # Any identity/body mutation invalidates the normalized reconstruction proof.
    mutations = [
        prompt.replace(agent['agent_path'], '/watchdog/wd-999'),
        prompt.replace(agent['workspace'], str(tmp_path / 'other-workspace')),
        prompt.replace(raw_ticket, raw_ticket + 'x'),
        prompt.replace(f'scope_thread_id={scope_id}', 'scope_thread_id=some-other-scope'),
        prompt.replace('你的职责只有监督', '你的职责包括监督'),
    ]
    assert all(controller._stale_bootstrap_draft_owner(value, 'project-fixture') is None for value in mutations)


def test_stale_watchdog_bootstrap_rejects_observed_native_submission(store, tmp_path):
    workspace = tmp_path / 'watchdog-observed-proof'
    workspace.mkdir()
    scope_id = 'scope-observed-proof'
    allocation = store.allocate_watchdog(scope_id)
    generation = allocation['generation']
    agent = store.create(
        name='watchdog-observed', role='Supervise one Runtime conversation',
        workspace=str(workspace), project_id='project-fixture', owner_thread_id=scope_id,
        kind='watchdog', watchdog_generation=generation,
    )
    store.bind_watchdog(generation, agent['logical_agent_id'])
    operation = store.prepare_web_operation(agent['logical_agent_id'], 'observed-proof-operation', 'spawn')
    raw_ticket = store.issue_ticket(agent['logical_agent_id'])
    controller = ChatGPTWebController(SimpleNamespace(store=store, config=store.config), session=None, loop=None)
    prompt = controller._watchdog_bootstrap(agent, raw_ticket, scope_id, generation)
    store.set_watchdog_state(generation, 'FAILED')
    with store.transaction() as c:
        c.execute('UPDATE web_operations SET observed_request_id=? WHERE operation_id=?', ('request-seen', operation['operation_id']))
    assert controller._stale_bootstrap_draft_owner(prompt, 'project-fixture') is None


def test_proven_bootstrap_clear_rejects_toctou_change(store):
    import asyncio

    class Composer:
        def __init__(self):
            self.fill_called = False
        async def inner_text(self):
            return 'changed after proof'
        async def fill(self, _value):
            self.fill_called = True

    composer = Composer()
    page = SimpleNamespace(locator=lambda _selector: composer)
    session = SimpleNamespace()
    async def assert_owner(_caller):
        return None
    session.assert_owner = assert_owner
    controller = ChatGPTWebController(SimpleNamespace(store=store, config=store.config), session=session, loop=None)

    async def scenario():
        with pytest.raises(AgentError) as error:
            await controller._clear_proven_bootstrap_draft(page, None, 'the proven draft')
        assert error.value.code == 'PREPARED_DRAFT_CHANGED'
        assert composer.fill_called is False
    asyncio.run(scenario())
