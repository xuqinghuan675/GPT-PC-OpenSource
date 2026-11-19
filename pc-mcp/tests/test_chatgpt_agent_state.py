import asyncio
import json
import uuid
import pytest
from mcp import Client
from machine_config import MachineConfig
from chatgpt_agent import AgentManager, Caller
from chatgpt_agent_store import AgentError
import server
from test_server import call_tool, isolate_runtime


@pytest.fixture
def manager(tmp_path):
    leases = {}
    checks = []
    config = MachineConfig.from_mapping({},runtime_root=str(tmp_path))
    manager = AgentManager(config,caller_resolver=lambda x:leases.get(x),control_check=lambda x:checks.append(x),
        runtime_facts=lambda x:{'execution_checkpoint':{'successful_action_count':9,'last_tool':'pytest'},'goal_mode':{'outcome':'verify'}})
    manager.test_leases, manager.test_checks = leases, checks
    return manager


def make_caller(manager, thread='controller', mode='execute_exclusive', kind='project'):
    lid = uuid.uuid4().hex
    manager.test_leases[lid] = {'lease_id':lid,'thread_id':thread,'workspace':manager.config.default_work_root,'mode':mode,'lease_type':kind}
    return lid


def make_agent(manager,parent=None,owner='controller',name='decoder'):
    a = manager.store.create(name=name,role='接口验收',workspace=manager.config.default_work_root,
        project_id='project-test',owner_thread_id=owner,parent_id=parent,
        delegation_reason='独立核对该子模块并回传可验收结果' if parent else None)
    aid, cid, thread = a['logical_agent_id'],str(uuid.uuid4()),'thread-'+uuid.uuid4().hex
    manager.store.bind_conversation(aid,cid,'https://chatgpt.com/c/'+cid,'project-test')
    ticket = manager.store.issue_ticket(aid)
    a = manager.store.join(ticket,runtime_thread_id=thread,project_id='project-test',conversation_id=cid,browser_verified=True)
    return a, make_caller(manager,thread)


def test_no_caller_and_readonly_mutation_rejected(manager):
    with pytest.raises(AgentError) as e:
        manager.dispatch('list')
    assert e.value.code == 'CALLER_IDENTITY_REQUIRED'
    lease = make_caller(manager,mode='read_shared')
    assert manager.dispatch('list',workspace_lease_id=lease)['count'] == 0
    with pytest.raises(AgentError) as e:
        manager.dispatch('spawn',workspace_lease_id=lease)
    assert e.value.code == 'EXECUTE_IDENTITY_REQUIRED'


def test_parent_cannot_forge_child_complete(manager):
    root, root_lease = make_agent(manager)
    child, child_lease = make_agent(manager,parent=root['logical_agent_id'],name='audit')
    with pytest.raises(AgentError) as e:
        manager.dispatch('complete',agent_ref=child['logical_agent_id'],summary='parent says done',workspace_lease_id=root_lease)
    assert e.value.code == 'SELF_ACTION_REQUIRED'
    manager.dispatch('checkpoint',summary='verified',workspace_lease_id=child_lease)
    assert manager.dispatch('complete',summary='explicit completion',workspace_lease_id=child_lease)['agent']['state'] == 'DONE'
    assert manager.test_checks


def test_scope_and_control_gate(manager):
    root, lease = make_agent(manager)
    outsider = make_caller(manager,'unrelated')
    assert manager.dispatch('list',workspace_lease_id=outsider)['count'] == 0
    with pytest.raises(AgentError):
        manager.dispatch('inspect',agent_ref=root['logical_agent_id'],workspace_lease_id=outsider)
    manager.control_check = lambda _:(_ for _ in ()).throw(AgentError('USER_STOP','stop'))
    with pytest.raises(AgentError) as e:
        manager.dispatch('message',message='do not send',workspace_lease_id=lease)
    assert e.value.code == 'USER_STOP' and manager.store.inbox(root['logical_agent_id']) == []


def test_safe_boundary_delivers_only_bound_thread_and_resume_is_live(manager):
    root, lease = make_agent(manager)
    child, child_lease = make_agent(manager,parent=root['logical_agent_id'],name='review')
    manager.dispatch('message',agent_ref=child['logical_agent_id'],message='对齐接口',workspace_lease_id=lease)
    assert manager.safe_boundary('unrelated') == {}
    assert manager.safe_boundary(root['runtime_thread_id']) == {}
    envelope = manager.safe_boundary(child['runtime_thread_id'])
    assert envelope['agent_messages'][0]['content'] == '对齐接口'
    assert manager.safe_boundary(child['runtime_thread_id']) == {}
    result = manager.dispatch('resume_context',workspace_lease_id=child_lease)
    assert result['runtime_facts']['execution_checkpoint']['successful_action_count'] == 9
    assert len(result['mailbox']) == 1
    manager.dispatch('inbox',ack_ids=[envelope['agent_messages'][0]['message_id']],workspace_lease_id=child_lease)
    assert manager.dispatch('inbox',workspace_lease_id=child_lease)['messages'] == []


def test_explicit_agent_ref_and_history_provenance(manager):
    a, lease = make_agent(manager)
    manager.dispatch('checkpoint',summary='中文接口 tests PASS',workspace_lease_id=lease)
    result = manager.dispatch('history_search',query='接口',workspace_lease_id=lease)
    assert result['count'] >= 1 and result['results'][0]['conversation_id'] == a['conversation_id']
    source = manager.dispatch('history_get',source_handle=result['results'][0]['source_handle'],workspace_lease_id=lease)
    assert source['source']['agent_id'] == a['logical_agent_id']
    with pytest.raises(AgentError):
        manager.dispatch('inspect',agent_ref='接口',workspace_lease_id=lease)


def test_wait_zero_is_bounded(manager):
    a, lease = make_agent(manager)
    result = manager.dispatch('wait',wait_seconds=0,workspace_lease_id=lease)
    assert result['status'] == 'timeout'


def test_mcp_discovery_adds_exactly_one_tool_and_safe_args():
    async def discover():
        async with Client(server.mcp) as client:
            return (await client.list_tools()).tools
    tools = asyncio.run(discover())
    assert len(tools) == 82
    assert len([t for t in tools if t.name=='chatgpt_agent']) == 1
    tool = next(t for t in tools if t.name=='chatgpt_agent')
    assert tool.input_schema['properties']['reasoning_effort']['default'] == 'extra_high'
    assert tool.input_schema['properties']['allow_pro']['default'] is False
    assert 'root_rescue_token' in tool.input_schema['properties']
    assert 'notification_email' in tool.input_schema['properties']
    args = server._activity_safe_arguments(server.chatgpt_agent,(),{
        'action':'join','spawn_ticket':'ga_ticket_TOPSECRET','query':'ga_ticket_TOPSECRET',
        'root_rescue_token':'rescue_TOPSECRET','notification_email':'private@example.invalid'})
    assert 'TOPSECRET' not in json.dumps(args) and 'spawn_ticket' not in args
    assert 'root_rescue_token' not in args and 'notification_email' not in args
    assert 'chatgpt_agent' in server._CONTROL_GATED_TOOL_NAMES
    assert 'chatgpt_agent' in server._AUDITED_TOOL_NAMES
    assert 'chatgpt_agent' not in server._WORKSPACE_MUTATION_TOOL_NAMES


def test_real_wrapper_active_read_identity_and_optional_mailbox_failure(monkeypatch,tmp_path):
    isolate_runtime(monkeypatch,tmp_path)
    config = MachineConfig.from_mapping({},runtime_root=str(tmp_path))
    manager = AgentManager(config,caller_resolver=server._active_lease_record,
        control_check=server._chatgpt_agent_control_check,runtime_facts=server._chatgpt_agent_runtime_facts)
    monkeypatch.setattr(server,'_CHATGPT_AGENT_MANAGER',manager)
    workspace = tmp_path/'work'
    workspace.mkdir()
    lease = call_tool('acquire_workspace',{'path':str(workspace),'mode':'read_shared'})['lease_id']
    assert call_tool('chatgpt_agent',{'action':'list','workspace_lease_id':lease})['count'] == 0
    path = workspace/'fixture.txt'
    path.write_text('confirmed',encoding='utf-8')
    monkeypatch.setattr(manager,'safe_boundary',lambda _:(_ for _ in ()).throw(OSError('fixture database unavailable')))
    result = call_tool('read_text_file',{'path':str(path),'workspace_lease_id':lease})
    assert result['content'] == 'confirmed' and result['agent_mailbox_delivery_pending']
    call_tool('release_workspace',{'workspace_lease_id':lease})


def test_ultra_status_and_agent_self_enable(manager):
    lease = make_caller(manager)
    status = manager.dispatch('ultra_status', workspace_lease_id=lease)
    assert status['ultra_mode']['active'] is False
    assert status['limits']['max_parallel_children_per_agent'] == 3
    calls = []
    manager.ultra_setter = lambda lid, **kw: calls.append((lid, kw)) or {'ultra_mode': {'active': True, 'status': 'on', 'revision': 1}}
    enabled = manager.dispatch('ultra_on', workspace_lease_id=lease)
    assert enabled['ultra_mode']['active'] is True
    assert calls[0][1]['enabled'] is True and calls[0][1]['user_explicit'] is False
    assert '1–3 个独立子任务' in enabled['instruction']
    assert '后台不会自动创建' in enabled['instruction']


def test_ultra_utilization_detects_serial_goal_and_real_worker(manager):
    lease = make_caller(manager, thread='top-ultra')
    caller = Caller.from_lease(manager.test_leases[lease])
    empty = manager.ultra_utilization(caller, successful_actions=4, goal_active=True)
    assert empty['joined_parallel_workers'] == 0
    assert empty['launching_workers'] == 0
    assert empty['parallel_capacity_remaining'] == 3
    assert empty['recommended_parallel_workers_max'] == 3
    assert empty['delegation_review_needed'] is True

    worker, _worker_lease = make_agent(manager, owner='worker-runtime', name='parallel-review')
    manager.store.assign_supervisor_scope(worker['logical_agent_id'], 'top-ultra')
    active = manager.ultra_utilization(caller, successful_actions=9, goal_active=True)
    assert active['joined_parallel_workers'] == 1
    assert active['parallel_capacity_remaining'] == 2
    assert active['delegation_review_needed'] is False


def test_ultra_utilization_excludes_caller_and_counts_only_descendants(manager):
    root, root_lease = make_agent(manager, owner='root-runtime', name='root-worker')
    manager.store.assign_supervisor_scope(root['logical_agent_id'], 'root-runtime')
    caller = Caller.from_lease(manager.test_leases[root_lease])
    serial = manager.ultra_utilization(caller, successful_actions=8, goal_active=True)
    assert serial['joined_parallel_workers'] == 0
    assert serial['delegation_review_needed'] is True

    child, _child_lease = make_agent(manager, parent=root['logical_agent_id'], owner='child-owner', name='child-review')
    manager.store.assign_supervisor_scope(child['logical_agent_id'], 'root-runtime')
    parallel = manager.ultra_utilization(caller, successful_actions=8, goal_active=True)
    assert parallel['joined_parallel_workers'] == 1
    assert parallel['delegation_review_needed'] is False


def test_root_complete_requires_final_user_summary(manager):
    root, lease = make_agent(manager)
    manager.dispatch('checkpoint', summary='root verified', workspace_lease_id=lease)
    result = manager.dispatch('complete', summary='root done', workspace_lease_id=lease)
    assert result['final_response_required'] is True
    assert '最终总结' in result['instruction']


def test_watchdog_wait_exits_immediately_when_generation_is_not_active(manager):
    scope='watchdog-wait-scope'
    allocation=manager.store.allocate_watchdog(scope)
    watcher=manager.store.create(
        name='watchdog-wait',role='监督业务 Agent',workspace=manager.config.default_work_root,
        project_id='project-test',owner_thread_id=scope,kind='watchdog',watchdog_generation=allocation['generation'])
    manager.store.assign_supervisor_scope(watcher['logical_agent_id'],scope)
    manager.store.bind_watchdog(allocation['generation'],watcher['logical_agent_id'])
    cid=str(uuid.uuid4())
    manager.store.bind_conversation(watcher['logical_agent_id'],cid,'https://chatgpt.com/c/'+cid,'project-test')
    runtime_thread='thread-watchdog-wait'
    joined=manager.store.join(
        manager.store.issue_ticket(watcher['logical_agent_id']),runtime_thread_id=runtime_thread,
        project_id='project-test',conversation_id=cid,browser_verified=True)
    manager.store.activate_watchdog(joined['logical_agent_id'])
    manager.store.set_watchdog_scope(scope,enabled=True,manual_disabled=False,source='test')
    lease=make_caller(manager,runtime_thread)
    manager.store.set_watchdog_state(allocation['generation'],'RETIRING')
    result=manager.dispatch('wait',wait_seconds=60,workspace_lease_id=lease)
    assert result['status']=='watchdog_inactive'
    assert result['scope_thread_id']==scope
    assert '释放租约' in result['instruction']


def test_watchdog_list_applies_scope_filter_before_pagination(manager):
    # Global historical rows appear first; they must not consume a Watchdog's
    # requested page before its own supervisor scope is filtered.
    for index in range(4):
        old=manager.store.create(
            name=f'old-agent-{index}',role='历史任务',workspace=manager.config.default_work_root,
            project_id='project-test',owner_thread_id=f'other-scope-{index}')
        manager.store.assign_supervisor_scope(old['logical_agent_id'],f'other-scope-{index}')
        manager.store.set_state(old['logical_agent_id'],'CLOSED')

    scope='scope-pagination'
    target=manager.store.create(
        name='scope-business',role='当前应监督业务',workspace=manager.config.default_work_root,
        project_id='project-test',owner_thread_id=scope)
    manager.store.assign_supervisor_scope(target['logical_agent_id'],scope)

    allocation=manager.store.allocate_watchdog(scope)
    watcher=manager.store.create(
        name='watchdog-page',role='监督分页',workspace=manager.config.default_work_root,
        project_id='project-test',owner_thread_id=scope,kind='watchdog',watchdog_generation=allocation['generation'])
    manager.store.assign_supervisor_scope(watcher['logical_agent_id'],scope)
    manager.store.bind_watchdog(allocation['generation'],watcher['logical_agent_id'])
    cid=str(uuid.uuid4()); thread='thread-watchdog-pagination'
    manager.store.bind_conversation(watcher['logical_agent_id'],cid,'https://chatgpt.com/c/'+cid,'project-test')
    manager.store.join(manager.store.issue_ticket(watcher['logical_agent_id']),runtime_thread_id=thread,
        project_id='project-test',conversation_id=cid,browser_verified=True)
    manager.store.activate_watchdog(watcher['logical_agent_id'])
    manager.store.set_watchdog_scope(scope,enabled=True,manual_disabled=False,source='test')
    lease=make_caller(manager,thread)

    result=manager.dispatch('list',limit=1,offset=0,workspace_lease_id=lease)
    assert result['count']==1
    assert result['agents'][0]['logical_agent_id']==target['logical_agent_id']


def test_business_agents_cannot_read_or_wait_across_top_level_trees(manager):
    first,first_lease=make_agent(manager,owner='controller-first',name='first-root')
    second,second_lease=make_agent(manager,owner='controller-second',name='second-root')

    with pytest.raises(AgentError) as error:
        manager.dispatch('inspect',agent_ref=second['logical_agent_id'],workspace_lease_id=first_lease)
    assert error.value.code=='AGENT_TREE_FORBIDDEN'

    listed=manager.dispatch('list',limit=20,workspace_lease_id=first_lease)
    ids={row['logical_agent_id'] for row in listed['agents']}
    assert first['logical_agent_id'] in ids
    assert second['logical_agent_id'] not in ids

    manager.store.archive_message(first['logical_agent_id'],{
        'message_id':'first-scope-history','role':'assistant','channel':'final',
        'visible_text':'scope-marker first-tree','content_type':'text'})
    manager.store.archive_message(second['logical_agent_id'],{
        'message_id':'second-scope-history','role':'assistant','channel':'final',
        'visible_text':'scope-marker second-tree','content_type':'text'})
    history=manager.dispatch('history_search',query='scope-marker',scope='project',workspace_lease_id=first_lease)
    assert history['count']==1
    assert history['results'][0]['agent_id']==first['logical_agent_id']

    before=manager.store.latest_event_seq()
    manager.store.checkpoint(second['logical_agent_id'],'foreign event marker')
    waited=manager.dispatch('wait',wait_seconds=0,after_event_seq=before,workspace_lease_id=first_lease)
    assert waited['status']=='timeout'


def test_controller_safe_boundary_receives_root_worker_handoff(manager):
    worker, worker_lease = make_agent(manager,owner='controller-handoff',name='handoff-worker')
    result = manager.dispatch('checkpoint',summary='worker verified',pending_work='parent follow-up',workspace_lease_id=worker_lease)
    assert result['handoff']['recipient_thread_id']=='controller-handoff'
    envelope = manager.safe_boundary('controller-handoff')
    assert envelope['agent_handoffs'][0]['summary']=='worker verified'
    assert manager.safe_boundary('controller-handoff')=={}

    controller_lease=make_caller(manager,'controller-handoff')
    inbox=manager.dispatch('inbox',workspace_lease_id=controller_lease)
    assert inbox['handoffs'][0]['pending_work']=='parent follow-up'
    hid=inbox['handoffs'][0]['handoff_id']
    acked=manager.dispatch('inbox',ack_ids=[hid],workspace_lease_id=controller_lease)
    assert acked['handoffs']==[]


def test_parent_inspect_and_resume_include_child_handoffs(manager):
    root, root_lease = make_agent(manager,owner='root-handoff',name='handoff-parent')
    child, child_lease = make_agent(manager,parent=root['logical_agent_id'],owner='child-handoff',name='handoff-review')
    manager.dispatch('checkpoint',summary='child evidence ready',pending_work='parent merge',workspace_lease_id=child_lease)

    inspected=manager.dispatch('inspect',agent_ref=root['logical_agent_id'],workspace_lease_id=root_lease)
    assert inspected['child_handoffs'][0]['child_agent_id']==child['logical_agent_id']
    child_inspected=manager.dispatch('inspect',agent_ref=child['logical_agent_id'],workspace_lease_id=root_lease)
    assert child_inspected['handoff']['summary']=='child evidence ready'

    resumed=manager.dispatch('resume_context',workspace_lease_id=root_lease)
    assert resumed['child_handoffs'][0]['pending_work']=='parent merge'
    assert resumed['handoffs'][0]['summary']=='child evidence ready'


def test_watchdog_wait_observes_business_handoff_event(manager):
    scope='handoff-watch-scope'
    target,target_lease=make_agent(manager,owner=scope,name='watched-worker')
    manager.store.assign_supervisor_scope(target['logical_agent_id'],scope)

    allocation=manager.store.allocate_watchdog(scope)
    watcher=manager.store.create(
        name='watchdog-handoff',role='监督 handoff',workspace=manager.config.default_work_root,
        project_id='project-test',owner_thread_id=scope,kind='watchdog',watchdog_generation=allocation['generation'])
    manager.store.assign_supervisor_scope(watcher['logical_agent_id'],scope)
    manager.store.bind_watchdog(allocation['generation'],watcher['logical_agent_id'])
    cid=str(uuid.uuid4()); thread='thread-watchdog-handoff'
    manager.store.bind_conversation(watcher['logical_agent_id'],cid,'https://chatgpt.com/c/'+cid,'project-test')
    manager.store.join(manager.store.issue_ticket(watcher['logical_agent_id']),runtime_thread_id=thread,
        project_id='project-test',conversation_id=cid,browser_verified=True)
    manager.store.activate_watchdog(watcher['logical_agent_id'])
    manager.store.set_watchdog_scope(scope,enabled=True,manual_disabled=False,source='test')
    watcher_lease=make_caller(manager,thread)

    before=manager.store.latest_event_seq()
    manager.dispatch('checkpoint',summary='watched child checkpoint',pending_work='continue audit',workspace_lease_id=target_lease)
    waited=manager.dispatch('wait',wait_seconds=0,after_event_seq=before,workspace_lease_id=watcher_lease)
    assert waited['status']=='events'
    assert any(event['kind']=='handoff.created' and event['agent_id']==target['logical_agent_id'] for event in waited['events'])
    inspected=manager.dispatch('inspect',agent_ref=target['logical_agent_id'],workspace_lease_id=watcher_lease)
    assert inspected['handoff']['summary']=='watched child checkpoint'


def test_ultra_created_pending_worker_does_not_consume_capacity(manager):
    lease = make_caller(manager, thread="top-ultra-pending")
    caller = Caller.from_lease(manager.test_leases[lease])
    pending = manager.store.create(
        name="pending-review",
        role="等待网页绑定",
        workspace=manager.config.default_work_root,
        project_id="project-test",
        owner_thread_id="top-ultra-pending",
    )
    manager.store.assign_supervisor_scope(pending["logical_agent_id"], "top-ultra-pending")
    result = manager.ultra_utilization(caller, successful_actions=8, goal_active=True)
    assert result["live_business_agents"] == 1
    assert result["joined_parallel_workers"] == 0
    assert result["launching_workers"] == 0
    assert result["stalled_unjoined_workers"] == 1
    assert result["parallel_capacity_remaining"] == 3
    assert result["delegation_review_needed"] is True


def test_ultra_only_irreversible_native_submit_consumes_launch_slot(manager):
    lease = make_caller(manager, thread="top-ultra-submit")
    caller = Caller.from_lease(manager.test_leases[lease])
    pending = manager.store.create(
        name="submitting-review",
        role="网页提交中",
        workspace=manager.config.default_work_root,
        project_id="project-test",
        owner_thread_id="top-ultra-submit",
    )
    aid = pending["logical_agent_id"]
    manager.store.assign_supervisor_scope(aid, "top-ultra-submit")
    op = manager.store.prepare_web_operation(aid, "spawn:test-submit", "spawn")
    manager.store.update_web_operation(op["operation_id"], "PREPARED", "PREPARED", prompt_sha256="abc")
    manager.store.update_web_operation(op["operation_id"], "PREPARED", "SUBMITTING")
    result = manager.ultra_utilization(caller, successful_actions=8, goal_active=True)
    assert result["launching_workers"] == 1
    assert result["stalled_unjoined_workers"] == 0
    assert result["parallel_capacity_remaining"] == 2
    assert result["delegation_review_needed"] is False
