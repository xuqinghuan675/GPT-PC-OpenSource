"""Offline behavioral contracts. All ChatGPT responses and leases are fixtures."""
import copy
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
import json
import uuid

import pytest

from chatgpt_agent import AgentManager, Caller, is_read_action
from chatgpt_agent_store import AgentError
from machine_config import MachineConfig, ConfigError
from ultra_collaboration import UltraCoordinator
from ultra_web_transport import verify_snapshot, select_model, validate_project

PID = 'g-p-' + 'a' * 32


class FakeTransport:
    def __init__(self):
        self.calls = []
        self.snapshots = {}
        self.receipts = {}
        self.fail_after_write = False
        self.reject = False
        self.wrong_project = False
        self.account_tag = 'fixture-account'
        self.terminal = True

    def project(self):
        return {'name':'GPTAgent','project_id':PID,'account_tag':self.account_tag}

    def model(self, requested, effort, account_tag):
        assert requested == 'non-pro'
        return {'model':'test-thinking','effort':'max'}

    def send(self, operation_id, payload, account_tag):
        self.calls.append(copy.deepcopy(payload))
        if self.reject:
            return {'status':'rejected','error':'WEB_ADMISSION_REQUIRED','http_status':403}
        cid = payload.get('conversation_id') or str(uuid.uuid4())
        mid = payload['messages'][0]['id']; aid = str(uuid.uuid4())
        mapping = self.snapshots.get(cid, {}).get('mapping', {}).copy()
        if payload.get('conversation_id'):
            mapping[payload['parent_message_id']]['children'] = [mid]
        mapping[mid] = {'parent':payload['parent_message_id'],'children':[aid], 'message':{'id':mid,'role':'user','text':payload['messages'][0]['content']['parts'][0]}}
        mapping[aid] = {'parent':mid,'children':[],'message':{'id':aid,'role':'assistant','end_turn':self.terminal,
            'status':'finished_successfully' if self.terminal else 'in_progress','channel':'final','text':'fixture-answer' if self.terminal else '', 'model':payload['model'],'effort':payload['thinking_effort']}}
        self.snapshots[cid] = {'conversation_id':cid,'project_id':('g-p-'+'b'*32) if self.wrong_project else PID,
                               'current_node':aid,'mapping':mapping}
        receipt = {'status':'accepted','conversation_id':cid,'http_status':200}
        self.receipts[operation_id] = receipt
        if self.fail_after_write:
            raise TimeoutError('fixture lost ACK')
        return receipt

    def receipt(self, oid):
        return self.receipts.get(oid, {'status':'unknown'})

    def conversation(self, cid, account_tag):
        assert account_tag == self.account_tag
        return copy.deepcopy(self.snapshots[cid])

    def recent(self, project_id, account_tag):
        return [{'id':cid} for cid in self.snapshots]


@pytest.fixture
def env(tmp_path):
    config = MachineConfig.from_mapping({'chatgpt':{'ultra_collaboration':True}}, runtime_root=str(tmp_path))
    workspace = tmp_path/'workspaces'; workspace.mkdir()
    leases = {'root-lease':{'lease_id':'root-lease','thread_id':'root-thread','workspace':str(workspace),
                          'scope_root':str(workspace),'mode':'execute_exclusive','lease_type':'project'}}
    released=[]
    def attach(source_lease_id, *, logical_agent_id, owner_label, write_paths=None, session_generation=1):
        source=leases[source_lease_id]; root=source.get('shared_root_lease_id') or source_lease_id
        for old in leases.values():
            if old.get('logical_agent_id')==logical_agent_id:
                return old
        lid=uuid.uuid4().hex
        leases[lid]={'lease_id':lid,'thread_id':'thread-'+lid,'workspace':str(workspace),'mode':'execute_exclusive',
                     'lease_type':'project','lease_role':'participant','shared_root_lease_id':root,
                     'logical_agent_id':logical_agent_id,'owner_label':owner_label,
                     'controller_id':'gptagent:'+logical_agent_id,'session_generation':session_generation,
                     'participant_write_paths':list(write_paths or [])}
        return leases[lid]
    def rotate(source_lease_id, *, old_participant_id, logical_agent_id, owner_label,
               write_paths=None, expected_generation, next_generation):
        source=leases[source_lease_id]; root=source.get('shared_root_lease_id') or source_lease_id
        old=leases.get(old_participant_id) if old_participant_id else None
        if old is not None:
            assert old.get('logical_agent_id')==logical_agent_id
            assert int(old.get('session_generation') or 1)==expected_generation
            leases.pop(old_participant_id,None)
            released.append(old_participant_id)
        for existing in leases.values():
            if existing.get('logical_agent_id')==logical_agent_id:
                assert int(existing.get('session_generation') or 1)==next_generation
                return existing
        lid=uuid.uuid4().hex
        leases[lid]={'lease_id':lid,'thread_id':'thread-'+lid,'workspace':str(workspace),'mode':'execute_exclusive',
                     'lease_type':'project','lease_role':'participant','shared_root_lease_id':root,
                     'logical_agent_id':logical_agent_id,'owner_label':owner_label,
                     'controller_id':'gptagent:'+logical_agent_id,'session_generation':next_generation,
                     'participant_write_paths':list(write_paths or [])}
        return leases[lid]
    def release(lid):
        assert lid != 'root-lease'
        released.append(lid); leases.pop(lid,None)
        return {'resource_lease_released':False}
    m=AgentManager(config,caller_resolver=leases.get,control_check=lambda _:None,
        runtime_facts=lambda _:{'ultra_mode':{'active':True}}, participant_attacher=attach,
        participant_rotator=rotate,participant_releaser=release)
    transport=FakeTransport(); core=UltraCoordinator(m, transport); m._ultra_coordinator=core
    m.dispatch('ensure_project',workspace_lease_id='root-lease')
    return m, core, transport, leases, released


def call(env, action, lease='root-lease', **p):
    return env[0].dispatch(action, workspace_lease_id=lease, **p)


def spawn(env, name='parser-worker', lease='root-lease', **p):
    p.setdefault('estimated_minutes', 60)
    p.setdefault('work_mode', 'bounded')
    return call(env,'spawn',lease=lease,name=name,role='检查指定组件并输出可核验结果',
                delegation_reason='该组件可以独立分析和测试，再向父级回传验收结果',
                idempotency_key='spawn:'+name, **p)


def join(env, result):
    aid=result['agent']['logical_agent_id']; member=env[1].teams.member(aid)
    joined=call(env,'join',lease=member['participant_id'])
    assert joined['status']=='joined'
    return aid, member['participant_id']


def test_exact_project_no_root_fallback():
    validate_project({'name':'GPTAgent','id':PID,'type':'snorlax'})
    for bad in [{'name':'GPTagent','id':PID},{'name':'GPTAgent','id':'g-not-project'}, {'name':'GPTAgent','id':PID,'type':'gpt'}]:
        with pytest.raises(AgentError): validate_project(bad)
    with pytest.raises(ConfigError):
        MachineConfig.from_mapping({'chatgpt':{'project_name':'Elsewhere','ultra_collaboration':True}})
    with pytest.raises(ConfigError):
        MachineConfig.from_mapping({'chatgpt':{'ultra_collaboration':'true'}})


def test_spawn_requires_estimate_caps_bounded_and_allows_explicit_continuous(env):
    base = dict(
        name='budget-check', role='独立验收工时预算规则',
        delegation_reason='独立验证工时门禁不会被普通子任务绕过',
        idempotency_key='budget-check-missing',
    )
    with pytest.raises(AgentError) as err:
        call(env, 'spawn', **base)
    assert err.value.code == 'WORK_ESTIMATE_REQUIRED'
    assert env[2].calls == []

    with pytest.raises(AgentError) as err:
        call(env, 'spawn', **{**base, 'idempotency_key':'budget-check-101', 'estimated_minutes':101})
    assert err.value.code == 'WORK_BUDGET_EXCEEDED'
    assert env[2].calls == []

    result = call(
        env, 'spawn', **{**base, 'name':'continuous-budget', 'idempotency_key':'continuous-budget-ok',
                        'work_mode':'continuous', 'estimated_minutes':180}
    )
    aid = result['agent']['logical_agent_id']
    policy = env[1].teams.work_policy(aid)
    assert policy['work_mode'] == 'continuous'
    assert policy['estimated_minutes'] == 180
    assert policy['refresh_interval_minutes'] == 45


def test_spawn_defaults_to_extra_high_and_bootstrap_explains_transient_429(env):
    result = spawn(env, 'first-turn-throttle-contract')
    agent = result['agent']
    assert agent['requested_reasoning_effort'] == 'extra_high'

    downgraded = spawn(
        env, 'no-business-downgrade',
        reasoning_effort='medium', performance_priority='latency',
    )
    assert downgraded['agent']['requested_reasoning_effort'] == 'extra_high'

    payload = env[2].calls[-2]
    prompt = payload['messages'][0]['content']['parts'][0]
    assert 'GPT-PC 已随本次 Child 请求注入' in prompt
    assert '不得仅凭模型记忆或工具列表判断插件未加载' in prompt
    assert '先实际尝试当前 ultra_agent/chatgpt_agent join' in prompt
    assert 'WEB_READ_THROTTLED/HTTP 429' in prompt
    assert '不代表 GPT-PC 插件未加载或不可用' in prompt
    assert '重试同一个 join' in prompt
    assert '不新建身份、不重发任务' in prompt


def test_local_tag_is_exact_identity_and_title_is_not_used_for_binding(env):
    aid, alid = join(env, spawn(env, 'identity-a', display_name='相同展示标题'))
    bid, blid = join(env, spawn(env, 'identity-b', display_name='相同展示标题'))
    team = call(env, 'team_status')
    am = next(m for m in team['members'] if m['agent_id'] == aid)
    bm = next(m for m in team['members'] if m['agent_id'] == bid)

    assert am['local_tag'] != bm['local_tag']
    assert am['conversation_id'] != bm['conversation_id']
    assert am['bound_project_id'] == bm['bound_project_id'] == PID
    assert am['local_tag'].startswith('gptagent:')

    sent = call(
        env, 'team_message', lease=alid, agent_ref=bm['local_tag'],
        message='按本地稳定标签寻址', idempotency_key='tag-route-1',
    )
    assert call(env, 'team_inbox', lease=blid)['messages'][0]['message_id'] == sent['message_id']

    fake = str(uuid.uuid4())
    root_caller = Caller.from_lease(env[3]['root-lease'])
    exact_team = env[1].teams.context(root_caller)[0]
    with pytest.raises(AgentError) as err:
        env[1].teams.bind_conversation_identity(
            exact_team, aid, PID, fake, 'https://chatgpt.com/c/' + fake,
        )
    assert err.value.code == 'CONVERSATION_BINDING_CHANGED'


def test_continuous_worker_can_only_self_refresh_and_45_minute_gate_is_hard(env):
    result = spawn(env, 'continuous-self', work_mode='continuous', estimated_minutes=240)
    aid, lid = join(env, result)
    assert env[0].store.get(aid)['state'] == 'IDLE'

    with pytest.raises(AgentError) as err:
        call(
            env, 'followup', agent_ref=aid, message='root must not refresh this worker',
            idempotency_key='continuous-root-followup',
        )
    assert err.value.code == 'CONTINUOUS_SELF_REFRESH_ONLY'

    with pytest.raises(AgentError) as err:
        call(env, 'refresh_turn', lease=lid)
    assert err.value.code == 'SELF_REFRESH_NOT_DUE'
    assert len(env[2].calls) == 1

    latest = env[0].store.latest_turn(aid)
    with env[0].store.transaction() as c:
        c.execute(
            'UPDATE turns SET generation_started_at=? WHERE agent_id=? AND turn_epoch=?',
            (env[0].store.clock() - 2701, aid, latest['turn_epoch']),
        )
    refreshed = call(env, 'refresh_turn', lease=lid)
    assert refreshed['agent']['conversation_id'] == result['agent']['conversation_id']
    assert len(env[2].calls) == 2
    assert env[2].calls[-1]['conversation_id'] == result['agent']['conversation_id']
    assert env[2].calls[-1]['messages'][0]['content']['parts'] == ['继续']


def test_bounded_worker_never_self_refreshes(env):
    _, lid = join(env, spawn(env, 'bounded-self', estimated_minutes=100))
    with pytest.raises(AgentError) as err:
        call(env, 'refresh_turn', lease=lid)
    assert err.value.code == 'SELF_REFRESH_NOT_CONTINUOUS'


def test_explicit_migrate_cannot_steal_healthy_member(env):
    result = spawn(env, 'healthy-migrate-guard')
    aid, _ = join(env, result)
    before = len(env[2].calls)
    with pytest.raises(AgentError) as err:
        call(
            env, 'recover_member', agent_ref=aid, recovery_mode='migrate',
            summary='manual migration must not steal healthy member',
            idempotency_key='healthy-migrate-guard-v1',
        )
    assert err.value.code == 'MEMBER_STILL_ACTIVE'
    assert len(env[2].calls) == before
    assert env[1].teams.current_session(aid)['generation'] == 1
    assert env[1].teams.current_session(aid)['conversation_id'] == result['agent']['conversation_id']


def test_same_session_recovery_rotates_lost_participant_and_keeps_conversation(env):
    result = spawn(env, 'same-session-recovery')
    aid, old_lid = join(env, result)
    old_cid = result['agent']['conversation_id']
    env[3].pop(old_lid)

    recovered = call(
        env, 'recover_member', agent_ref=aid, recovery_mode='same_session',
        summary='runtime participant disappeared while exact web conversation remains healthy',
        idempotency_key='same-session-recovery-v1',
    )
    member = env[1].teams.member(aid)
    new_lid = member['participant_id']
    assert new_lid != old_lid
    assert new_lid in env[3]
    assert env[3][new_lid]['session_generation'] == 1
    assert recovered['agent']['logical_agent_id'] == aid
    assert recovered['agent']['conversation_id'] == old_cid
    assert recovered['session']['generation'] == 1
    assert env[2].calls[-1]['conversation_id'] == old_cid
    assert env[1].teams.binding(aid)['conversation_id'] == old_cid



def test_context_truncation_continuation_is_diagnostic_not_migration_trigger(env):
    result = spawn(env, 'rollover-diagnostic')
    aid, old_lid = join(env, result)
    cid = result['agent']['conversation_id']
    env[3].pop(old_lid)
    env[2].snapshots[cid]['context_truncation_continuation'] = {
        'source_conversation_id': 'source-chat',
        'boundary_message_id': 'boundary-node',
        'visible_from_message_id': 'visible-node',
    }

    recovered = call(
        env, 'recover_member', agent_ref=aid, recovery_mode='same_session',
        summary='participant disappeared; native Chat rollover marker is diagnostic only',
        idempotency_key='rollover-diagnostic-v1',
    )
    assert env[1].teams.current_session(aid)['conversation_id'] == cid
    assert recovered['context_rollover']['present'] is True
    assert recovered['context_rollover']['source_conversation_id'] == 'source-chat'
    assert recovered['context_rollover']['interpretation'] == 'native_chat_context_rollover_not_summary_proof'
    assert env[1].teams.current_session(aid)['generation'] == 1


def test_session_migration_preserves_logical_agent_and_fences_old_session(env):
    result = spawn(env, 'session-migrate')
    aid, old_lid = join(env, result)
    old_cid = result['agent']['conversation_id']
    old_lease = copy.deepcopy(env[3][old_lid])
    env[3].pop(old_lid)

    migrated = call(
        env, 'recover_member', agent_ref=aid, recovery_mode='migrate',
        summary='platform requires a fresh ChatGPT conversation',
        idempotency_key='session-migrate-v1',
    )
    new_cid = migrated['agent']['conversation_id']
    member = env[1].teams.member(aid)
    new_lid = member['participant_id']
    history = env[1].teams.session_history(aid)

    assert migrated['agent']['logical_agent_id'] == aid
    assert new_cid != old_cid
    assert migrated['session']['generation'] == 2
    assert [row['generation'] for row in history] == [1, 2]
    assert history[0]['state'] == 'retired'
    assert history[0]['conversation_id'] == old_cid
    assert history[1]['state'] == 'active'
    assert history[1]['conversation_id'] == new_cid
    assert env[3][new_lid]['session_generation'] == 2
    assert env[1].teams.binding(aid)['conversation_id'] == new_cid
    root_caller = Caller.from_lease(env[3]['root-lease'])
    team = env[1].teams.context(root_caller)[0]
    with pytest.raises(AgentError) as err:
        env[1].teams.verify_conversation_identity(team, aid, PID, old_cid)
    assert err.value.code == 'CONVERSATION_BINDING_MISMATCH'

    # Even if an old handle reappears after migration, it is not current.
    env[3][old_lid] = old_lease
    with pytest.raises(AgentError) as err:
        env[1].teams.context(Caller.from_lease(old_lease))
    assert err.value.code in {'TEAM_SCOPE_FORBIDDEN', 'STALE_SESSION_GENERATION'}


def test_unknown_migration_send_must_reconcile_before_any_retry(env):
    result = spawn(env, 'migration-reconcile')
    aid, old_lid = join(env, result)
    env[3].pop(old_lid)
    before = len(env[2].calls)
    env[2].fail_after_write = True

    with pytest.raises(AgentError) as err:
        call(
            env, 'recover_member', agent_ref=aid, recovery_mode='migrate',
            summary='verified old conversation cannot continue',
            idempotency_key='migration-unknown-v1',
        )
    assert err.value.code == 'SEND_OUTCOME_UNKNOWN'
    assert len(env[2].calls) == before + 1
    env[2].fail_after_write = False

    with pytest.raises(AgentError) as err:
        call(
            env, 'recover_member', agent_ref=aid, recovery_mode='migrate',
            summary='verified old conversation cannot continue',
            idempotency_key='migration-unknown-v2',
        )
    assert err.value.code == 'RECOVERY_RECONCILE_REQUIRED'
    assert len(env[2].calls) == before + 1

    reconciled = call(env, 'web_reconcile', agent_ref=aid)
    assert reconciled['session']['generation'] == 2
    assert len(env[2].calls) == before + 1
    assert env[1].teams.current_session(aid)['generation'] == 2


def test_create_verify_join_are_distinct(env):
    result=spawn(env)
    assert result['status']=='submitted' and result['project_verified']
    assert result['child_joined'] is False
    assert result['caller_identity']=={
        'caller_lease_id':'root-lease',
        'caller_thread_id':'root-thread',
        'caller_role':'root',
        'shared_root_lease_id':'root-lease',
        'shared_root_thread_id':'root-thread',
        'root_resource_still_owned_by_root':True,
    }
    aid,lid=join(env,result)
    joined=call(env,'team_status',lease=lid)
    assert joined['caller_identity']['caller_role']=='participant'
    assert joined['caller_identity']['caller_lease_id']==lid
    assert joined['caller_identity']['shared_root_lease_id']=='root-lease'
    assert joined['caller_identity']['shared_root_thread_id']=='root-thread'
    assert env[3][lid]['shared_root_lease_id']=='root-lease'
    assert env[0].store.get(aid)['runtime_thread_id']==env[3][lid]['thread_id']
    assert 'root-lease' in env[3]
    root_status=call(env,'team_status')
    assert root_status['caller_identity']['caller_role']=='root'
    assert root_status['caller_identity']['caller_lease_id']=='root-lease'
    assert root_status['caller_identity']['shared_root_lease_id']=='root-lease'
    assert env[2].calls[0]['conversation_mode']=={'kind':'gizmo_interaction','gizmo_id':PID}


def test_idempotent_spawn_does_not_post_twice(env):
    a=spawn(env); b=spawn(env)
    assert a['agent']['logical_agent_id']==b['agent']['logical_agent_id']
    assert len(env[2].calls)==1
    with pytest.raises(AgentError) as err: spawn(env,message='different task')
    assert err.value.code=='IDEMPOTENCY_CONFLICT'


def test_ambiguous_send_reconciles_without_retry(env):
    env[2].fail_after_write=True
    with pytest.raises(AgentError) as err: spawn(env)
    assert err.value.code=='SEND_OUTCOME_UNKNOWN'
    env[2].fail_after_write=False
    result=spawn(env)
    assert result['project_verified'] and len(env[2].calls)==1


def test_uncertain_no_evidence_never_reposts(env):
    env[2].fail_after_write=True
    with pytest.raises(AgentError): spawn(env)
    env[2].snapshots.clear(); env[2].receipts.clear()
    result=spawn(env)
    assert result['status']=='reconciling' and result['automatic_retry'] is False
    assert len(env[2].calls)==1


def test_upstream_rejection_releases_only_child(env):
    env[2].reject=True
    result=spawn(env)
    assert result['status']=='blocked' and result['http_status']==403
    assert env[4] and 'root-lease' in env[3]
    assert len(env[3])==1
    again=spawn(env)
    assert again['status']=='blocked' and len(env[2].calls)==1


def test_wrong_project_is_not_bound(env):
    env[2].wrong_project=True
    with pytest.raises(AgentError) as err: spawn(env)
    assert err.value.code=='CONVERSATION_PROJECT_MISMATCH'
    assert env[0].store.agents()[0]['conversation_id'] is None


def test_parent_cannot_acknowledge_child_join(env):
    spawn(env)
    with pytest.raises(AgentError) as err: call(env,'join')
    assert err.value.code=='PARTICIPANT_REQUIRED'


def test_unjoined_participant_cannot_message(env):
    r=spawn(env); member=env[1].teams.member(r['agent']['logical_agent_id'])
    with pytest.raises(AgentError) as err:
        call(env,'team_message',lease=member['participant_id'],agent_ref='root',message='not joined',idempotency_key='a')
    assert err.value.code=='TEAM_JOIN_PENDING'


def test_siblings_communicate_without_another_workspace_lock(env):
    aid,alid=join(env,spawn(env)); bid,blid=join(env,spawn(env,'test-worker'))
    assert env[0].store.get(aid)['root_agent_id'] != env[0].store.get(bid)['root_agent_id']
    sent=call(env,'team_message',lease=alid,agent_ref=bid,message='接口采用整数',idempotency_key='agreement-1')
    repeat=call(env,'team_message',lease=alid,agent_ref=bid,message='接口采用整数',idempotency_key='agreement-1')
    assert sent['message_id']==repeat['message_id'] and sent['new_turn'] is False
    inbox=call(env,'team_inbox',lease=blid)['messages']; assert len(inbox)==1
    call(env,'team_message',lease=blid,agent_ref=aid,message='收到',reply_to=sent['message_id'],idempotency_key='reply-1')
    call(env,'team_inbox',lease=blid,ack_ids=[sent['message_id']])
    assert not call(env,'team_inbox',lease=blid)['messages']
    assert len(env[2].calls)==2


def test_root_rescue_capability_is_exact_and_root_cannot_self_rescue(env):
    result = spawn(env, 'rescue-auth')
    aid, lid = join(env, result)
    capability = env[1].teams.rescue_capability(aid)
    binding = env[1].teams.binding(aid)
    team = env[1].teams.context(Caller.from_lease(env[3]['root-lease']))[0]

    with pytest.raises(AgentError) as err:
        call(env, 'team_root_rescue', source_handle=capability['token_value'],
             idempotency_key='root-self-rescue')
    assert err.value.code == 'ROOT_RESCUE_CHILD_REQUIRED'

    with pytest.raises(AgentError) as err:
        call(env, 'team_root_rescue', lease=lid, source_handle='wrong-capability',
             idempotency_key='bad-rescue-cap')
    assert err.value.code == 'ROOT_RESCUE_TOKEN_INVALID'

    authorized = env[1].teams.authorize_root_rescue(
        team['team_id'], aid, capability['token_value'],
        binding['project_id'], binding['conversation_id'])
    assert authorized[0]['team_id'] == team['team_id']
    assert authorized[1]['agent_id'] == aid


def test_team_finalize_blocks_live_child_then_arms_unique_completion(env):
    aid, lid = join(env, spawn(env, 'finalize-child'))
    with pytest.raises(AgentError) as err:
        call(env, 'team_finalize', name='half-day-task', summary='should still be blocked')
    assert err.value.code == 'TEAM_MEMBERS_UNFINISHED'

    call(
        env, 'checkpoint', lease=lid,
        summary='child finished assigned work',
        pending_work='', artifacts=[],
    )
    done = call(env, 'complete', lease=lid, summary='child finished assigned work')
    assert done['status'] == 'completed'
    env[3].pop(lid, None)
    env[1].teams._detach_dead_participants(env[1].teams.member(aid)['team_id'])

    finalized = call(env, 'team_finalize', name='half-day-task', summary='Root review PASS')
    assert finalized['status'] == 'finalized'
    completion = finalized['completion']
    assert completion['task_name'] == 'half-day-task'
    assert completion['summary'] == 'Root review PASS'
    assert completion['report']['child_agent_count'] == 1
    assert completion['report']['children'][0]['agent_id'] == aid

    again = call(env, 'team_finalize', name='half-day-task', summary='Root review PASS')
    assert again['completion']['completion_id'] == completion['completion_id']


def test_completion_notification_claim_retry_and_sent_are_idempotent(env):
    finalized = call(env, 'team_finalize', name='notify-task', summary='done')
    completion_id = finalized['completion']['completion_id']
    teams = env[1].teams

    first = teams.claim_completion_notification(completion_id)
    assert first['claim_status'] == 'claimed'
    teams.finish_completion_notification(
        completion_id, first['attempt_token'], success=False, error='temporary mail failure')

    with teams.store.transaction() as c:
        c.execute('UPDATE ultra_completions SET updated_at=? WHERE completion_id=?',
                  (teams.store.clock() - 10000, completion_id))
    retry = teams.claim_completion_notification(completion_id)
    assert retry['claim_status'] == 'claimed'
    sent = teams.finish_completion_notification(
        completion_id, retry['attempt_token'], success=True, gmail_message_id='gmail-test-id')
    assert sent['state'] == 'sent'
    assert sent['gmail_message_id'] == 'gmail-test-id'
    assert teams.claim_completion_notification(completion_id)['claim_status'] == 'already_sent'


def test_team_broadcast_is_durable_idempotent_and_does_not_start_turns(env):
    aid, alid = join(env, spawn(env, 'broadcast-a'))
    bid, blid = join(env, spawn(env, 'broadcast-b'))
    cid, clid = join(env, spawn(env, 'broadcast-c'))
    before = len(env[2].calls)

    first = call(
        env, 'team_broadcast', lease=alid,
        message='共享发现：接口字段统一使用整数。',
        message_kind='finding',
        idempotency_key='broadcast-finding-1',
    )
    again = call(
        env, 'team_broadcast', lease=alid,
        message='共享发现：接口字段统一使用整数。',
        message_kind='finding',
        idempotency_key='broadcast-finding-1',
    )

    assert first['recipient_count'] == 3
    assert first['recipients'] == ['root', bid, cid]
    assert first['message_ids'] == again['message_ids']
    assert first['new_turn'] is False
    assert len(env[2].calls) == before
    assert call(env, 'team_inbox', lease=alid)['messages'] == []
    for lease in ('root-lease', blid, clid):
        inbox = call(env, 'team_inbox', lease=lease)['messages']
        assert len(inbox) == 1
        assert inbox[0]['from_actor'] == aid
        assert inbox[0]['kind'] == 'finding'
        assert inbox[0]['content'] == '共享发现：接口字段统一使用整数。'


def test_team_message_kinds_and_reply_semantics(env):
    aid, alid = join(env, spawn(env, 'question-a'))
    bid, blid = join(env, spawn(env, 'answer-b'))
    question = call(
        env, 'team_message', lease=alid, agent_ref=bid,
        message='接口返回值是否允许为空？',
        message_kind='question',
        idempotency_key='question-1',
    )
    received = call(env, 'team_inbox', lease=blid)['messages']
    assert received[0]['kind'] == 'question'
    answer = call(
        env, 'team_message', lease=blid, agent_ref=aid,
        message='不允许为空，失败时返回结构化错误。',
        message_kind='answer',
        reply_to=question['message_id'],
        idempotency_key='answer-1',
    )
    reply = call(env, 'team_inbox', lease=alid)['messages'][0]
    assert reply['message_id'] == answer['message_id']
    assert reply['kind'] == 'answer'
    assert reply['reply_to'] == question['message_id']


def test_resume_context_includes_durable_ultra_team_state(env):
    aid, lid = join(env, spawn(env, 'resume-team'))
    created = task(env, 'resume-readonly')
    call(
        env, 'team_message', agent_ref=aid, message='恢复后先看 Team 状态',
        message_kind='handoff', idempotency_key='resume-mail',
    )
    resumed = call(env, 'resume_context', lease=lid)
    assert resumed['agent']['logical_agent_id'] == aid
    team = resumed['ultra_team']
    assert team['actor'] == aid
    assert team['project_name'] == 'GPTAgent'
    assert any(item['task_id'] == created['task_id'] for item in team['tasks'])
    assert team['messages'][0]['content'] == '恢复后先看 Team 状态'
    assert team['messages'][0]['kind'] == 'handoff'


def test_watchdog_is_fail_closed_when_machine_policy_is_frozen(env):
    frozen = replace(env[0].config, watchdog_enabled=False)
    env[0].config = frozen
    env[1].config = frozen
    before_calls = len(env[2].calls)
    status = call(env, 'watchdog_status')
    assert status['status'] == 'frozen'
    assert status['automatic_supervision'] is False
    with pytest.raises(AgentError) as err:
        call(env, 'watchdog_start')
    assert err.value.code == 'WATCHDOG_FROZEN'
    assert len(env[2].calls) == before_calls
    assert env[0].store.watchdog_rows() == []


def test_parallel_team_mailboxes_never_cross_participant_identity(env):
    workers = []
    for name in ("mail-alpha", "mail-beta", "mail-gamma"):
        aid, lid = join(env, spawn(env, name))
        workers.append((aid, lid))

    def send_one(item):
        sender_index, sequence = item
        sender_aid, sender_lid = workers[sender_index]
        target_aid, _ = workers[(sender_index + 1) % len(workers)]
        result = call(
            env, "team_message", lease=sender_lid, agent_ref=target_aid,
            message=f"{sender_aid}:{sequence}",
            idempotency_key=f"parallel-mail:{sender_index}:{sequence}",
        )
        return result["message_id"]

    workload = [(sender_index, sequence) for sender_index in range(3) for sequence in range(30)]
    with ThreadPoolExecutor(max_workers=18) as pool:
        message_ids = list(pool.map(send_one, workload))
    assert len(message_ids) == len(set(message_ids)) == 90

    for receiver_index, (receiver_aid, receiver_lid) in enumerate(workers):
        expected_sender = workers[(receiver_index - 1) % len(workers)][0]
        inbox = call(env, "team_inbox", lease=receiver_lid)["messages"]
        assert len(inbox) == 30
        assert {message["to_actor"] for message in inbox} == {receiver_aid}
        assert {message["from_actor"] for message in inbox} == {expected_sender}
        ack_ids = [message["message_id"] for message in inbox]
        assert call(env, "team_inbox", lease=receiver_lid, ack_ids=ack_ids)["messages"] == []


def test_child_to_root_mail_and_safe_boundary(env):
    aid,lid=join(env,spawn(env))
    call(env,'team_message',lease=lid,agent_ref='parent',message='研究结果已准备',idempotency_key='handoff-one')
    assert env[0].safe_boundary('root-thread')['ultra_messages'][0]['from_actor']==aid
    assert env[0].safe_boundary('root-thread')=={}
    assert call(env,'team_inbox')['messages']


def test_legacy_message_action_routes_to_ultra_mailbox(env):
    aid,lid=join(env,spawn(env))
    sent=call(env,'message',lease=lid,agent_ref='root',message='E2E_LEGACY_MESSAGE_OK')
    repeat=call(env,'message',lease=lid,agent_ref='root',message='E2E_LEGACY_MESSAGE_OK')
    assert sent['message_id']==repeat['message_id']
    inbox=call(env,'team_inbox')['messages']
    assert len(inbox)==1
    assert inbox[0]['from_actor']==aid
    assert inbox[0]['to_actor']=='root'
    assert inbox[0]['content']=='E2E_LEGACY_MESSAGE_OK'


def test_root_status_cleans_terminal_child_participant_only(env):
    aid,lid=join(env,spawn(env))
    call(env,'checkpoint',lease=lid,summary='terminal cleanup ready',pending_work='')
    call(env,'complete',lease=lid,summary='terminal cleanup done')
    assert lid in env[3] and 'root-lease' in env[3]
    status=call(env,'team_status')
    assert lid in status['terminal_participants_cleaned']
    assert lid not in env[3] and 'root-lease' in env[3]
    member=env[1].teams.member(aid)
    assert member['participant_id'] is None and member['thread_id'] is None


def test_cross_team_and_forged_participant_rejected(env):
    _,lid=join(env,spawn(env))
    env[3]['other']={'lease_id':'other','thread_id':'other-thread','workspace':env[3]['root-lease']['workspace'],
                     'mode':'execute_exclusive','lease_type':'runtime_admin'}
    call(env,'ensure_project',lease='other')
    foreign=spawn(env,'foreign-worker',lease='other')['agent']['logical_agent_id']
    with pytest.raises(AgentError) as err:
        call(env,'team_message',lease=lid,agent_ref=foreign,message='cross',idempotency_key='cross')
    assert err.value.code=='TEAM_SCOPE_FORBIDDEN'
    env[3]['forged']={**env[3][lid],'lease_id':'forged','thread_id':'different-thread'}
    with pytest.raises(AgentError): call(env,'team_status',lease='forged')


def test_root_expiration_revokes_family(env):
    _,lid=join(env,spawn(env)); env[3].pop('root-lease')
    with pytest.raises(AgentError) as err: call(env,'team_status',lease=lid)
    assert err.value.code=='TEAM_ROOT_EXPIRED'


def test_root_lease_rotation_recovers_same_team_and_detaches_dead_participants(env):
    first_team=call(env,'team_status')['team_id']
    aid,lid=join(env,spawn(env))
    old_root=env[3].pop('root-lease')
    env[3].pop(lid)
    env[3]['root-lease-2']={**old_root,'lease_id':'root-lease-2'}
    # Reproduce the bad reconnect state: an empty replacement team was created
    # before the prior stable-thread team was recognized.
    with env[0].store.transaction() as c:
        c.execute(
            'INSERT INTO ultra_teams VALUES(?,?,?,?,?,?,?,?)',
            ('replacement-empty','root-lease-2',old_root['thread_id'],old_root['workspace'],
             PID,env[2].account_tag,'active',env[0].store.clock()+1),
        )
    recovered=call(env,'ensure_project',lease='root-lease-2')
    assert recovered['team_id']==first_team
    status=call(env,'team_status',lease='root-lease-2')
    assert status['team_id']==first_team
    member=env[1].teams.member(aid)
    assert member['participant_id'] is None and member['thread_id'] is None
    agent=env[0].store.get(aid)
    assert agent['join_state']=='detached' and agent['state']=='DETACHED'
    with env[0].store.connection() as c:
        old=c.execute('SELECT state FROM ultra_teams WHERE team_id=?',(first_team,)).fetchone()
        empty=c.execute('SELECT 1 FROM ultra_teams WHERE team_id=?',('replacement-empty',)).fetchone()
    assert old['state']=='active' and empty is None


def test_observed_but_unjoined_workers_do_not_consume_parallel_slots(env):
    results=[spawn(env,f'pending-{index}') for index in range(4)]
    assert all(item['status']=='submitted' for item in results)
    assert len(env[2].calls)==4


def test_nested_agents_share_same_root_and_narrow_paths(env):
    _,lid=join(env,spawn(env,write_paths=['src']))
    child=spawn(env,'nested-review',lease=lid,write_paths=['src/tests'])
    _,nested=join(env,child)
    assert env[3][nested]['shared_root_lease_id']=='root-lease'
    with pytest.raises(AgentError) as err: spawn(env,'escape-scope',lease=lid,write_paths=['other'])
    assert err.value.code=='WRITE_SCOPE_FORBIDDEN'


def task(env, name, *, lease='root-lease', paths=None, deps=None):
    return call(env,'task_create',lease=lease,name=name,message='验收合同',write_paths=paths or [],depends_on=deps or [],idempotency_key='task:'+name)['task']


def test_task_dependency_and_revision_contract(env):
    a=task(env,'api'); b=task(env,'consumer',deps=[a['task_id']])
    with pytest.raises(AgentError) as err:
        call(env,'task_claim',task_id=b['task_id'],expected_version=1)
    assert err.value.code=='TASK_DEPENDENCY_PENDING'
    claimed=call(env,'task_claim',task_id=a['task_id'],expected_version=1)['task']
    assert claimed['version']==2
    with pytest.raises(AgentError) as err:
        call(env,'task_update',task_id=a['task_id'],expected_version=1,task_status='done',summary='verified')
    assert err.value.code=='TASK_VERSION_CONFLICT'
    call(env,'task_update',task_id=a['task_id'],expected_version=2,task_status='done',summary='tests PASS',artifacts=['report.md'])
    assert call(env,'task_claim',task_id=b['task_id'],expected_version=1)['status']=='claimed'


def test_concurrent_claim_only_one_winner(env):
    t=task(env,'one-owner')
    def attempt(_):
        try: return call(env,'task_claim',task_id=t['task_id'],expected_version=1)['status']
        except AgentError as exc: return exc.code
    with ThreadPoolExecutor(max_workers=2) as pool: results=list(pool.map(attempt,range(2)))
    assert results.count('claimed')==1
    assert results.count('TASK_VERSION_CONFLICT')==1


def test_write_conflicts_but_independent_paths_are_parallel(env):
    a=task(env,'source',paths=['src']); b=task(env,'source-child',paths=['src/a.py']); c=task(env,'test-only',paths=['tests'])
    call(env,'task_claim',task_id=a['task_id'],expected_version=1)
    with pytest.raises(AgentError) as err: call(env,'task_claim',task_id=b['task_id'],expected_version=1)
    assert err.value.code=='TASK_WRITE_CONFLICT'
    assert call(env,'task_claim',task_id=c['task_id'],expected_version=1)['status']=='claimed'


def test_readonly_member_cannot_claim_write_task(env):
    _,lid=join(env,spawn(env)); t=task(env,'write',paths=['source.py'])
    with pytest.raises(AgentError) as err: call(env,'task_claim',lease=lid,task_id=t['task_id'],expected_version=1)
    assert err.value.code=='WRITE_SCOPE_FORBIDDEN'
    with pytest.raises(AgentError): task(env,'escape',paths=['../escape'])


def test_completion_requires_evidence_and_owner(env):
    _,lid=join(env,spawn(env)); t=task(env,'owned')
    call(env,'task_claim',lease=lid,task_id=t['task_id'],expected_version=1)
    with pytest.raises(AgentError) as err:
        call(env,'task_update',task_id=t['task_id'],expected_version=2,task_status='done',summary='parent says done')
    assert err.value.code=='TASK_OWNER_REQUIRED'
    with pytest.raises(AgentError) as err:
        call(env,'task_update',lease=lid,task_id=t['task_id'],expected_version=2,task_status='done')
    assert err.value.code=='TASK_EVIDENCE_REQUIRED'
    call(env,'checkpoint',lease=lid,summary='partial',pending_work='')
    with pytest.raises(AgentError) as err: call(env,'complete',lease=lid,summary='premature')
    assert err.value.code=='TEAM_TASKS_UNFINISHED'


def test_task_and_mail_survive_coordinator_recreation(env):
    t=task(env,'persist'); call(env,'team_message',agent_ref='root',message='persist mail',idempotency_key='persist')
    env[0]._ultra_coordinator=UltraCoordinator(env[0],env[2])
    assert call(env,'task_list')['tasks'][0]['task_id']==t['task_id']
    assert call(env,'team_inbox')['messages'][0]['content']=='persist mail'


def test_followup_reuses_conversation_and_does_not_replay(env):
    r=spawn(env); aid,_=join(env,r)
    p={'agent_ref':aid,'message':'检查第二阶段','idempotency_key':'second-turn'}
    first=call(env,'followup',**p); again=call(env,'followup',**p)
    assert first['agent']['conversation_id']==r['agent']['conversation_id']==again['agent']['conversation_id']
    assert len(env[2].calls)==2


def test_busy_followup_queues_without_sending(env):
    env[2].terminal=False; r=spawn(env); aid,_=join(env,r)
    result=call(env,'followup',agent_ref=aid,message='之后处理',idempotency_key='later')
    assert result['status']=='queued' and result['new_turn'] is False and len(env[2].calls)==1


def test_peer_cannot_control_or_restart_root(env):
    _,alid=join(env,spawn(env)); bid,_=join(env,spawn(env,'peer-review'))
    with pytest.raises(AgentError) as err: call(env,'followup',lease=alid,agent_ref=bid,message='override',idempotency_key='x')
    assert err.value.code=='AGENT_TREE_FORBIDDEN'
    with pytest.raises(AgentError): call(env,'followup',lease=alid,agent_ref='root',message='override',idempotency_key='x')


def test_shutdown_is_graceful_and_never_releases_root(env):
    _,lid=join(env,spawn(env)); result=call(env,'team_shutdown')
    assert result['root_lease_released'] is False and 'root-lease' in env[3]
    assert call(env,'team_inbox',lease=lid)['messages'][0]['kind']=='shutdown_request'
    with pytest.raises(AgentError): task(env,'too-late')
    with pytest.raises(AgentError): spawn(env,'too-late-worker')


def test_ultra_off_prevents_new_browser_work(env):
    env[0].runtime_facts=lambda _:{'ultra_mode':{'active':False}}
    with pytest.raises(AgentError) as err: spawn(env)
    assert err.value.code=='ULTRA_REQUIRED' and not env[2].calls


def test_read_shared_cannot_ack_or_mutate(env):
    env[3]['root-lease']['mode']='read_shared'
    assert call(env,'task_list')['tasks']==[]
    with pytest.raises(AgentError): call(env,'team_inbox',ack_ids=['fake'])
    assert not is_read_action('team_inbox',{'ack_ids':['x']})


def test_model_discovery_rejects_pro_work_and_silent_lower_effort():
    def catalog(slug='chat-thinking',lane='thinking',effort='max'):
        return {'models':[{'slug':slug}], 'versions':[{'id':'latest','enabled':True,'presets':[{'model_slug':slug,'lane':lane,'thinking_effort':effort,'preset_type':'available'}]}]}
    assert select_model(catalog())['model']=='chat-thinking'
    for bad in [catalog('chat-pro'),catalog('chat-wm'),catalog(lane='pro'),catalog(effort='extended')]:
        with pytest.raises(AgentError): select_model(bad)


def test_old_response_and_hidden_reasoning_are_not_current_turn_proof():
    snap={'conversation_id':'cid','project_id':PID,'mapping':{'old':{'children':['old-answer'],'message':{'id':'old','role':'user'}},
        'old-answer':{'children':[],'message':{'id':'old-answer','role':'assistant','status':'finished_successfully','end_turn':True,'text':'OLD'}},
        'new':{'children':['hidden'],'message':{'id':'new','role':'user'}},
        'hidden':{'children':[],'message':{'id':'hidden','role':'assistant','channel':'analysis','status':'finished_successfully','end_turn':True,'text':'PRIVATE'}}}}
    result=verify_snapshot(snap,'cid',PID,'new')
    assert result['terminal'] is False and result['final'] is None


def test_queued_busy_followup_never_turns_into_a_second_send(env):
    env[2].terminal=False
    r=spawn(env); aid,_=join(env,r)
    args={'agent_ref':aid,'message':'later','idempotency_key':'stable-later'}
    first=call(env,'followup',**args)
    snap=env[2].snapshots[r['agent']['conversation_id']]
    snap['mapping'][snap['current_node']]['message'].update(end_turn=True,status='finished_successfully',text='done')
    again=call(env,'followup',**args)
    assert first['message_id']==again['message_id']
    assert len(env[2].calls)==1


def test_wrong_project_releases_only_preallocated_participant(env):
    env[2].wrong_project=True
    with pytest.raises(AgentError): spawn(env)
    assert env[4] and set(env[3])=={'root-lease'}


def test_later_turn_cannot_complete_an_earlier_turn():
    snap={'conversation_id':'cid','project_id':PID,'mapping':{
        'first':{'children':['second'],'message':{'id':'first','role':'user'}},
        'second':{'children':['reply'],'message':{'id':'second','role':'user'}},
        'reply':{'children':[],'message':{'id':'reply','role':'assistant','status':'finished_successfully','end_turn':True,'text':'belongs to second'}}}}
    assert verify_snapshot(snap,'cid',PID,'first')['terminal'] is False
    assert verify_snapshot(snap,'cid',PID,'second')['terminal'] is True


def test_ambiguous_terminal_branches_are_not_guessed():
    reply=lambda mid:{'children':[],'message':{'id':mid,'role':'assistant','status':'finished_successfully','end_turn':True,'text':mid}}
    snap={'conversation_id':'cid','project_id':PID,'mapping':{'u':{'children':['a','b'],'message':{'id':'u','role':'user'}},'a':reply('a'),'b':reply('b')}}
    with pytest.raises(AgentError) as err: verify_snapshot(snap,'cid',PID,'u')
    assert err.value.code=='TURN_BRANCH_AMBIGUOUS'


def test_root_cannot_release_a_working_child_write_claim(env):
    _,lid=join(env,spawn(env,write_paths=['src']))
    t=task(env,'active-write',paths=['src'])
    call(env,'task_claim',lease=lid,task_id=t['task_id'],expected_version=1)
    with pytest.raises(AgentError) as err:
        call(env,'task_update',task_id=t['task_id'],expected_version=2,task_status='cancelled',summary='stop request')
    assert err.value.code=='TASK_STILL_RUNNING'
    assert call(env,'task_list')['tasks'][0]['status']=='in_progress'


def test_tail_changes_during_model_discovery_do_not_post(env):
    r=spawn(env); aid,_=join(env,r); cid=r['agent']['conversation_id']
    model=env[2].model
    def changed(*args):
        result=model(*args)
        env[2].snapshots[cid]['current_node']='another-turn'
        return result
    env[2].model=changed
    with pytest.raises(AgentError) as err:
        call(env,'followup',agent_ref=aid,message='after-tail-change',idempotency_key='tail-change')
    assert err.value.code=='CONTINUATION_TAIL_CHANGED'
    assert len(env[2].calls)==1


def test_model_mismatch_does_not_accept_worker(env):
    original=env[2].send
    def mismatch(*args):
        result=original(*args)
        snap=env[2].snapshots[result['conversation_id']]
        snap['mapping'][snap['current_node']]['message']['model']='unexpected-pro'
        return result
    env[2].send=mismatch
    with pytest.raises(AgentError) as err: spawn(env)
    assert err.value.code=='MODEL_OBSERVATION_MISMATCH'
    assert set(env[3])=={'root-lease'}


def test_unsubmitted_legacy_drafts_do_not_exhaust_live_capacity(env):
    store=env[0].store
    store.config=replace(store.config,max_live_agents=1)
    for name in ['old-draft-a','old-draft-b']:
        store.create(name=name,role='draft never sent',workspace=env[3]['root-lease']['workspace'],project_id=PID,owner_thread_id='legacy-'+name)
    result=spawn(env)
    assert result['project_verified'] and len(env[2].calls)==1
    with pytest.raises(AgentError) as err: spawn(env,'capacity-worker')
    assert err.value.code=='AGENT_LIMIT'


def test_submission_capacity_is_atomic_after_draft_creation(env):
    store=env[0].store;store.config=replace(store.config,max_live_agents=1)
    ops=[]
    for name in ['candidate-a','candidate-b']:
        a=store.create(name=name,role='candidate',workspace=env[3]['root-lease']['workspace'],project_id=PID,owner_thread_id='owner-'+name)
        op=store.prepare_web_operation(a['logical_agent_id'],name,'spawn')
        store.update_web_operation(op['operation_id'],'PREPARED','PREPARED',prompt_sha256='known-prompt-hash')
        ops.append(op)
    def submit(op):
        try: store.update_web_operation(op['operation_id'],'PREPARED','SUBMITTING'); return 'submitted'
        except AgentError as exc: return exc.code
    with ThreadPoolExecutor(max_workers=2) as pool: results=list(pool.map(submit,ops))
    assert results.count('submitted')==1 and results.count('AGENT_LIMIT')==1


def test_failed_or_uncertain_submission_still_counts_until_reconciled(env):
    store=env[0].store;store.config=replace(store.config,max_live_agents=1)
    env[2].fail_after_write=True
    with pytest.raises(AgentError): spawn(env)
    with pytest.raises(AgentError) as err: spawn(env,'must-not-race')
    assert err.value.code=='AGENT_LIMIT'
    assert len(env[2].calls)==1


def test_blocked_task_keeps_write_claim_until_explicit_release(env):
    first=task(env,'blocked-writer',paths=['src'])
    second=task(env,'waiting-writer',paths=['src/module.py'])
    claimed=call(env,'task_claim',task_id=first['task_id'],expected_version=1)['task']
    blocked=call(env,'task_update',task_id=first['task_id'],expected_version=claimed['version'],
                 task_status='blocked',summary='external dependency; writer stopped at checkpoint')['task']
    assert blocked['status']=='blocked'
    with pytest.raises(AgentError) as err:
        call(env,'task_claim',task_id=second['task_id'],expected_version=1)
    assert err.value.code=='TASK_WRITE_CONFLICT'
    released=call(env,'task_update',task_id=first['task_id'],expected_version=blocked['version'],
                  task_status='pending',summary='owner confirmed no process or write remains')['task']
    assert released['status']=='pending' and released['owner'] is None
    assert call(env,'task_claim',task_id=second['task_id'],expected_version=1)['status']=='claimed'


def test_watchdog_uses_same_project_root_lease_and_graceful_lifecycle(env):
    started=call(env,'watchdog_start')
    assert started['watchdog_generation'] >= 1
    aid=started['agent']['logical_agent_id']
    member=env[1].teams.member(aid)
    participant=member['participant_id']
    assert env[3][participant]['shared_root_lease_id']=='root-lease'
    assert started['agent']['project_id']==PID
    joined=call(env,'join',lease=participant)
    assert joined['agent']['kind']=='watchdog'
    status=call(env,'watchdog_status')
    current=next(row for row in status['watchdogs'] if row['logical_agent_id']==aid)
    assert current['state']=='ACTIVE'
    assert status['scope']['enabled'] is True
    stopped=call(env,'watchdog_stop')
    assert stopped['root_lease_released'] is False
    assert 'root-lease' in env[3]
    inbox=call(env,'team_inbox',lease=participant)['messages']
    assert inbox and inbox[0]['kind']=='shutdown_request'
    call(env,'checkpoint',lease=participant,summary='watchdog final observation',pending_work='')
    completed=call(env,'complete',lease=participant,summary='watchdog stopped cleanly')
    assert completed['agent']['state']=='DONE'
    row=next(row for row in env[0].store.watchdog_rows() if row['logical_agent_id']==aid)
    assert row['state']=='CLOSED'
    assert env[0].store.watchdog_scope('root-thread')['enabled'] is False
