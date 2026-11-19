"""Regression cases for lifecycle health and acceptance gates; no live browser."""
import asyncio
import subprocess
from datetime import timedelta
from types import SimpleNamespace

import pytest

import runtime
import server
from chatgpt_agent import AgentManager, Caller
from chatgpt_agent_store import AgentStore
from chatgpt_web_controller import ChatGPTWebController
from machine_config import MachineConfig
from test_server import isolate_runtime
from test_visual_modes import acquire, observation, _single_target_review


@pytest.fixture
def lifecycle(tmp_path, monkeypatch):
    now = [1000.0]
    monkeypatch.setattr('chatgpt_web_controller.time.time', lambda: now[0])
    store = AgentStore(MachineConfig.from_mapping({}, runtime_root=str(tmp_path)), clock=lambda: now[0])
    store.set_watchdog_scope('scope-test', enabled=True, mode='conversation', started_at=100.0)
    manager = SimpleNamespace(store=store, config=store.config, _notify=lambda: None)
    controller = ChatGPTWebController(manager, session=None, loop=None)
    spawns = []

    async def spawn(_caller, scope):
        spawns.append(scope)
        return {'status': 'watchdog_spawned', 'generation': 99}

    controller._spawn_watchdog = spawn
    return now, store, controller, spawns


def test_watchdog_backoff_starts_when_slow_startup_fails(lifecycle):
    now, store, controller, spawns = lifecycle
    row = store.allocate_watchdog('scope-test', born_at=870.0)
    store.set_watchdog_state(row['generation'], 'FAILED')
    result = asyncio.run(controller._supervisor_tick(None))
    assert spawns == []
    assert result['errors'][0]['reason'] == 'spawn_backoff'
    assert result['errors'][0]['retry_after_seconds'] == 15.0


def test_watchdog_circuit_breaker_counts_slow_failures_across_episode(lifecycle):
    now, store, controller, spawns = lifecycle
    for born in (400.0, 550.0, 700.0):
        now[0] = born + 125.0
        row = store.allocate_watchdog('scope-test', born_at=born)
        store.set_watchdog_state(row['generation'], 'FAILED')
    now[0] = 900.0
    result = asyncio.run(controller._supervisor_tick(None))
    assert spawns == []
    assert store.watchdog_scope('scope-test')['enabled'] is False
    assert result['errors'][0]['reason'] == 'spawn_circuit_breaker'


@pytest.mark.parametrize('state', ['IDLE', 'DEAD_RECOVERABLE'])
def test_joined_watchdog_that_stopped_is_not_healthy(lifecycle, state):
    now, store, controller, _ = lifecycle
    row = store.allocate_watchdog('scope-test')
    agent = store.create(name='watchdog-health', role='supervise', workspace=store.config.default_work_root,
                         project_id='project-test', owner_thread_id='scope-test', kind='watchdog',
                         watchdog_generation=row['generation'])
    aid = agent['logical_agent_id']
    store.bind_watchdog(row['generation'], aid)
    store.bind_conversation(aid, 'conversation-health', 'https://chatgpt.com/c/conversation-health', 'project-test')
    ticket = store.issue_ticket(aid)
    store.join(ticket, runtime_thread_id='watchdog-thread', project_id='project-test',
               conversation_id='conversation-health', browser_verified=True)
    store.activate_watchdog(aid)
    store.set_state(aid, state)
    result = asyncio.run(controller._supervisor_tick(None))
    assert result['failed'][0]['reason'] == 'watchdog_unhealthy'
    assert store.watchdog_rows()[0]['state'] == 'FAILED'


def test_ultra_idle_joined_worker_does_not_hide_serial_execution(tmp_path):
    config = MachineConfig.from_mapping({}, runtime_root=str(tmp_path))
    manager = AgentManager(config, caller_resolver=lambda _: None, control_check=lambda _: None,
                           runtime_facts=lambda _: {})
    agent = manager.store.create(name='idle-worker', role='inspect', workspace=str(tmp_path),
                                 project_id='project-test', owner_thread_id='native-controller')
    aid = agent['logical_agent_id']
    manager.store.assign_supervisor_scope(aid, 'native-controller')
    manager.store.bind_conversation(aid, 'conversation-idle', 'https://chatgpt.com/c/conversation-idle', 'project-test')
    manager.store.join(manager.store.issue_ticket(aid), runtime_thread_id='worker-thread',
                       project_id='project-test', conversation_id='conversation-idle', browser_verified=True)
    manager.store.set_state(aid, 'IDLE')
    caller = Caller('a' * 32, 'native-controller', str(tmp_path), 'execute_exclusive', 'workspace')
    status = manager.ultra_utilization(caller, successful_actions=10, goal_active=True)
    assert status['joined_parallel_workers'] == 0
    assert status['inactive_joined_workers'] == 1
    assert status['delegation_review_needed'] is True


@pytest.fixture
def mode_lease(monkeypatch, tmp_path):
    isolate_runtime(monkeypatch, tmp_path)
    return acquire(tmp_path)


def test_visual_only_evidence_is_invalid_after_code_change(mode_lease):
    identity = mode_lease['lease_id']
    runtime.set_visual_mode(identity, targets=[{'name': 'desktop', 'viewport_mode': 'free'}])
    observation(mode_lease, 'before-change', (120, 80))
    review = _single_target_review('before-change')
    runtime.set_visual_mode(identity, action='complete', review=review)
    assert runtime.bump_workspace_validation_revision(identity)['visual_mode']['status'] == 'needs_review'
    with pytest.raises(runtime.RuntimePolicyError) as error:
        runtime.set_visual_mode(identity, action='complete', review=review)
    assert error.value.error_type == 'VISUAL_EVIDENCE_STALE'
    observation(mode_lease, 'after-change', (120, 80))
    runtime.set_visual_mode(identity, action='complete', review=_single_target_review('after-change'))
    runtime.release_workspace(identity)


def test_latest_failed_journey_cannot_reuse_earlier_pass(mode_lease):
    identity = mode_lease['lease_id']
    runtime.set_visual_mode(identity, depth='interactive', journeys=[{'id': 'nav', 'name': '导航'}],
                            targets=[{'name': 'desktop', 'viewport_mode': 'free'}])
    observation(mode_lease, 'old-pass', (120, 80), journey_id='nav', interaction_status='PASS')
    observation(mode_lease, 'new-failure', (120, 80), journey_id='nav', interaction_status='FAIL')
    with pytest.raises(runtime.RuntimePolicyError) as error:
        runtime.set_visual_mode(identity, action='complete', review=_single_target_review('new-failure'))
    assert error.value.error_type == 'VISUAL_INTERACTION_REQUIRED'


def test_editing_completed_goal_requires_new_acceptance(mode_lease):
    identity = mode_lease['lease_id']
    runtime.set_visual_mode(identity, action='update', goal_enabled=True, goal_outcome='完成修复',
                            goal_success_criteria=['原标准'])
    runtime.set_visual_mode(identity, action='complete', review={'goal': {
        'status': 'PASS', 'summary': '验证完成',
        'criteria': [{'criterion': '原标准', 'status': 'PASS', 'evidence': '相关测试通过'}]}})
    result = runtime.set_visual_mode(identity, action='update', goal_success_criteria=['新增标准'])
    assert result['goal_mode']['active'] is True
    with pytest.raises(runtime.RuntimePolicyError) as error:
        runtime.release_workspace(identity)
    assert error.value.error_type == 'GOAL_ACTIVE'


def test_visual_deadline_does_not_waive_acceptance(mode_lease, monkeypatch):
    identity = mode_lease['lease_id']
    runtime.set_visual_mode(identity, duration_seconds=10)
    later = runtime.utc_now() + timedelta(seconds=20)
    monkeypatch.setattr(runtime, 'utc_now', lambda: later)
    with pytest.raises(runtime.RuntimePolicyError) as error:
        runtime.release_workspace(identity)
    assert error.value.error_type == 'VISUAL_ACTIVE'


def test_disconnected_browser_does_not_delete_durable_tab_mapping(lifecycle):
    _, store, controller, _ = lifecycle
    store.set_setting('agent_tab:recoverable-agent', 'target-surviving-restart')
    controller.session = SimpleNamespace(context=None, disconnected=True, chatgpt_pages=lambda: [])
    assert asyncio.run(controller._prune_agent_tab_settings()) == []
    assert store.setting('agent_tab:recoverable-agent') == 'target-surviving-restart'


def test_monitor_survives_transient_metadata_error(lifecycle, monkeypatch):
    _, store, controller, _ = lifecycle
    calls = []

    async def restore():
        return None

    async def prune():
        calls.append('tick')
        if len(calls) == 1:
            raise PermissionError('transient file lock')
        controller.closed = True

    async def no_wait(_seconds):
        return None

    controller._restore_managed_observers = restore
    controller._prune_observers = prune
    monkeypatch.setattr('chatgpt_web_controller.asyncio.sleep', no_wait)
    asyncio.run(controller._monitor())
    assert calls == ['tick', 'tick']


def test_monitor_reattaches_persisted_observers_without_launch_or_send(lifecycle):
    _, store, controller, _ = lifecycle
    agent = store.create(name='resume-observer', role='observe', workspace=store.config.default_work_root,
                         project_id='project-test', owner_thread_id='scope-test')
    aid = agent['logical_agent_id']
    store.set_setting('agent_tab:' + aid, 'existing-target')
    calls = []

    async def ensure(caller, *, launch):
        assert launch is False
        calls.append('attach-browser')

    async def observer(row, caller=None, *, reopen=False):
        assert row['logical_agent_id'] == aid and reopen is False
        calls.append('observe-existing-tab')
        controller.observers[aid] = object()

    controller.session = SimpleNamespace(ensure=ensure)
    controller._observer = observer
    asyncio.run(controller._restore_managed_observers())
    assert calls == ['attach-browser', 'observe-existing-tab']


def test_search_stops_walking_as_soon_as_enough_matches_exist(mode_lease, tmp_path, monkeypatch):
    (tmp_path / 'first.py').write_text('needle\n', encoding='utf-8')
    monkeypatch.setattr(server.native_backend, 'prefer_native_for_tree', lambda *a, **k: False)
    monkeypatch.setattr(server, '_git_search_candidates', lambda *a: None)

    def walk(*args, **kwargs):
        yield tmp_path, [], ['first.py']
        raise AssertionError('Search needlessly traversed the rest of the tree')

    monkeypatch.setattr(server, '_iter_walk', walk)
    result = server.search_text(str(tmp_path), 'needle', max_results=1)
    assert result['count'] == 1 and result['truncated'] is True


def test_search_native_timeout_does_not_restart_full_python_budget(mode_lease, tmp_path, monkeypatch):
    now = [0.0]
    monkeypatch.setattr(server.time, 'perf_counter', lambda: now[0])
    monkeypatch.setattr(server.native_backend, 'prefer_native_for_tree', lambda *a, **k: True)

    def native(*args, **kwargs):
        now[0] = 30.1
        raise server.native_backend.NativeAccelerationUnavailable('timed out')

    def walk(*args, **kwargs):
        raise AssertionError('Timed out native search started a second full search')

    monkeypatch.setattr(server.native_backend, 'search_text_native', native)
    monkeypatch.setattr(server, '_iter_walk', walk)
    result = server.search_text(str(tmp_path), 'needle', timeout_seconds=30)
    assert result['timed_out'] is True


def test_search_budget_applies_even_when_file_pattern_never_matches(mode_lease, tmp_path, monkeypatch):
    now = [0.0]
    visited = []
    monkeypatch.setattr(server.time, 'perf_counter', lambda: now[0])
    monkeypatch.setattr(server.native_backend, 'prefer_native_for_tree', lambda *a, **k: False)
    monkeypatch.setattr(server, '_git_search_candidates', lambda *a: None)

    def walk(*args, **kwargs):
        for index in range(20):
            now[0] += 1
            visited.append(index)
            yield tmp_path, [], ['unrelated.bin']

    monkeypatch.setattr(server, '_iter_walk', walk)
    result = server.search_text(str(tmp_path), 'needle', file_pattern='*.py', timeout_seconds=3)
    assert result['timed_out'] is True and len(visited) <= 4


def test_fallback_preserves_git_ignores_but_explicit_log_search_still_works(mode_lease, tmp_path, monkeypatch):
    tmp_path = tmp_path / 'repo'
    tmp_path.mkdir()
    subprocess.run(['git', 'init', '--quiet', str(tmp_path)], check=True, capture_output=True)
    (tmp_path / '.gitignore').write_text('pc-mcp.venv/\nlogs/\n', encoding='utf-8')
    for directory in ('src', 'pc-mcp.venv', 'logs'):
        path = tmp_path / directory
        path.mkdir()
        (path / 'sample.py').write_text('needle\n', encoding='utf-8')
    monkeypatch.setattr(server.native_backend, 'prefer_native_for_tree', lambda *a, **k: False)
    result = server.search_text(str(tmp_path), 'needle', file_pattern='*.py')
    assert result['scanned_files'] == 1
    assert result['matches'][0]['path'] == str(tmp_path / 'src' / 'sample.py')
    direct = server.search_text(str(tmp_path / 'logs'), 'needle', file_pattern='*.py')
    assert direct['count'] == 1
    exhaustive = server.search_text(str(tmp_path), 'needle', file_pattern='*.py', include_ignored=True)
    assert exhaustive['count'] == 3


def test_mode_context_explains_acceptance_without_source_search_or_fake_pass(mode_lease):
    identity = mode_lease['lease_id']
    enabled = runtime.set_visual_mode(identity, goal_enabled=True, goal_outcome='验证界面',
        goal_success_criteria=['测试通过'], targets=[{'name': 'desktop', 'viewport_mode': 'responsive'}])
    template = runtime.get_visual_context(identity)['completion_review_template']
    assert enabled['completion_review_template'] == template
    assert template['final_observation_ids'] == {'desktop': ''}
    assert template['responsive_observation_ids'] == {'desktop': []}
    assert template['goal']['criteria'][0]['criterion'] == '测试通过'
    assert template['goal']['status'] == 'PENDING'
    with pytest.raises(runtime.RuntimePolicyError):
        runtime.set_visual_mode(identity, action='complete', review=template)
