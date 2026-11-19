from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
import json
import os
from pathlib import Path
import sqlite3
import uuid
import pytest
from machine_config import MachineConfig
from chatgpt_agent_store import AgentStore, AgentError, public_text


@pytest.fixture
def store(tmp_path):
    return AgentStore(MachineConfig.from_mapping({},runtime_root=str(tmp_path)))


def create(store, name='root-control', parent=None, **kwargs):
    if parent is not None and 'delegation_reason' not in kwargs:
        kwargs['delegation_reason'] = '独立核对该子模块并回传可验收结果'
    return store.create(name=name,role='核对解码测试',workspace=store.config.default_work_root,
                        project_id='project-fixture',owner_thread_id='owner-fixture',parent_id=parent,**kwargs)


def joined(store, name='root-control', parent=None):
    a = create(store,name,parent)
    cid = str(uuid.uuid4())
    token = store.issue_ticket(a['logical_agent_id'])
    store.bind_conversation(a['logical_agent_id'],cid,'https://chatgpt.com/c/'+cid,a['project_id'])
    a = store.join(token,runtime_thread_id='thread-'+uuid.uuid4().hex,project_id=a['project_id'],conversation_id=cid,browser_verified=True)
    store.record_turn(a['logical_agent_id'],turn_exchange_id='tx-'+uuid.uuid4().hex,user_message_id='initial-user')
    return store.get(a['logical_agent_id'])


def test_schema_migration_idempotent_wal_foreign_keys_and_rollback(store):
    a = create(store)
    reloaded = AgentStore(store.config)
    assert reloaded.get('/root') == a
    with store.connection() as c:
        assert c.execute('PRAGMA user_version').fetchone()[0] == 2
        assert c.execute('PRAGMA journal_mode').fetchone()[0] == 'wal'
        assert c.execute('PRAGMA foreign_keys').fetchone()[0] == 1
    with pytest.raises(RuntimeError):
        with store.transaction() as c:
            c.execute("INSERT INTO settings VALUES('rolled-back','1')")
            raise RuntimeError('crash')
    assert store.setting('rolled-back') is None


def test_future_schema_rejected(store):
    with store.connection() as c:
        c.execute('PRAGMA user_version=99')
    with pytest.raises(AgentError,match='newer'):
        AgentStore(store.config)


def test_scoped_watchdogs_isolate_branches_even_with_shared_logical_parent(store):
    root = create(store)
    store.assign_supervisor_scope(root['logical_agent_id'], 'thread-top-a')
    branch_a = store.create(
        name='branch-alpha', role='alpha', workspace=store.config.default_work_root,
        project_id=root['project_id'], owner_thread_id='thread-top-a', parent_id=root['logical_agent_id'],
        delegation_reason='scope alpha branch validation',
    )
    branch_b = store.create(
        name='branch-beta', role='beta', workspace=store.config.default_work_root,
        project_id=root['project_id'], owner_thread_id='thread-top-b', parent_id=root['logical_agent_id'],
        delegation_reason='scope beta branch validation',
    )
    store.assign_supervisor_scope(branch_a['logical_agent_id'], 'thread-top-a')
    store.assign_supervisor_scope(branch_b['logical_agent_id'], 'thread-top-b')

    assert {row['logical_agent_id'] for row in store.agents_in_supervisor_scope('thread-top-a')} == {
        root['logical_agent_id'], branch_a['logical_agent_id'],
    }
    assert [row['logical_agent_id'] for row in store.agents_in_supervisor_scope('thread-top-b')] == [branch_b['logical_agent_id']]

    active = store.set_watchdog_scope('thread-top-a', enabled=True, manual_disabled=False, source='test')
    stopped = store.set_watchdog_scope('thread-top-b', enabled=False, manual_disabled=True, source='test')
    assert active['enabled'] is True and stopped['manual_disabled'] is True
    migrated = store.set_watchdog_scope('thread-top-b', mode='conversation', migrated_from_program=True)
    assert migrated['migrated_from_program'] is True
    assert [row['scope_thread_id'] for row in store.watchdog_scopes(enabled_only=True)] == ['thread-top-a']

    snapshot = json.loads(store.public_relations_path.read_text(encoding='utf-8'))
    assert snapshot['version'] == 1
    assert {row['scope_thread_id'] for row in snapshot['agents']} == {'thread-top-a', 'thread-top-b'}
    assert {row['scope_thread_id'] for row in snapshot['watchdogs']} == {'thread-top-a', 'thread-top-b'}
    assert store.public_relations_path.stat().st_size < 64 * 1024


def test_names_identity_and_discovery_are_stable(store):
    root = create(store)
    a = create(store,'decoder',root['logical_agent_id'])
    b = create(store,'decoder',root['logical_agent_id'])
    assert a['agent_path'] == '/root/decoder'
    assert b['agent_path'].startswith('/root/decoder-') and a['short_id'] != b['short_id']
    assert a['web_title'].startswith('[GA·'+a['short_id']+'] root/decoder')
    assert store.get(a['short_id']) == store.get(a['agent_path']) == a
    with pytest.raises(AgentError) as err:
        store.get('decoder')
    assert err.value.code == 'AGENT_REF_CANDIDATES' and len(err.value.details['candidates']) == 2
    assert len(store.agents(path_prefix='/root/decoder')) == 1
    assert len(store.agents(query='decoder')) == 2


@pytest.mark.parametrize('name',['a','task1','child-1','new-chat','with space','解码器','/evil','X'*40])
def test_meaningless_names_rejected(store,name):
    with pytest.raises(AgentError):
        create(store,name)


def test_depth_children_and_live_limits(store):
    store.config = replace(store.config,max_depth=1,max_children_per_agent=1)
    a = create(store)
    b = create(store,'decoder',a['logical_agent_id'])
    with pytest.raises(AgentError) as e:
        create(store,'hardware',a['logical_agent_id'])
    assert e.value.code == 'CHILD_LIMIT'
    with pytest.raises(AgentError) as e:
        create(store,'analysis',b['logical_agent_id'])
    assert e.value.code == 'DEPTH_LIMIT'
    store.config = replace(store.config,max_live_agents=2,max_depth=2,max_children_per_agent=3,max_parallel_children_per_agent=3)
    with pytest.raises(AgentError) as e:
        create(store,'capacity-check',a['logical_agent_id'])
    assert e.value.code == 'AGENT_LIMIT'
    watchdog = create(store,'watchdog-monitor',kind='watchdog',watchdog_generation=1)
    assert watchdog['kind'] == 'watchdog'


def test_concurrent_names_do_not_collide(store):
    store.config = replace(store.config,max_parallel_children_per_agent=4)
    root = create(store)
    with ThreadPoolExecutor(max_workers=4) as pool:
        rows = list(pool.map(lambda _: create(store,'parallel-check',root['logical_agent_id']),range(4)))
    assert len({x['agent_path'] for x in rows}) == 4
    assert len({x['short_id'] for x in rows}) == 4


def test_ticket_pending_expiry_and_binding(store):
    now = [1000.0]
    store.clock = lambda:now[0]
    a = create(store)
    aid = a['logical_agent_id']
    token = store.issue_ticket(aid,ttl_seconds=60)
    kwargs = dict(runtime_thread_id='thread-test',project_id=a['project_id'],conversation_id='conversation-test',browser_verified=True)
    with pytest.raises(AgentError) as e:
        store.join(token,**kwargs)
    assert e.value.code == 'SPAWN_BINDING_PENDING'
    store.bind_conversation(aid,'conversation-test','https://chatgpt.com/c/conversation-test',a['project_id'])
    with pytest.raises(AgentError) as e:
        store.join(token,**{**kwargs,'project_id':'wrong'})
    assert e.value.code == 'JOIN_IDENTITY_MISMATCH'
    with pytest.raises(AgentError):
        store.join(token,**{**kwargs,'browser_verified':False})
    now[0] += 61
    with pytest.raises(AgentError) as e:
        store.join(token,**kwargs)
    assert e.value.code == 'TICKET_INVALID'
    token = store.issue_ticket(aid)
    row = store.join(token,**kwargs)
    assert row['join_state'] == 'joined' and row['runtime_thread_id'] == 'thread-test'
    with pytest.raises(AgentError):
        store.join(token,**kwargs)
    with store.connection() as c:
        dump = '\n'.join(c.iterdump())
    assert token not in dump and 'ga_ticket_' not in dump


def test_conversation_and_runtime_thread_are_unique(store):
    a = joined(store)
    b = create(store,'child-review',a['logical_agent_id'])
    with pytest.raises(AgentError) as e:
        store.bind_conversation(b['logical_agent_id'],a['conversation_id'],a['conversation_url'],a['project_id'])
    assert e.value.code == 'CONVERSATION_ALREADY_BOUND'
    cid = str(uuid.uuid4())
    store.bind_conversation(b['logical_agent_id'],cid,'https://chatgpt.com/c/'+cid,b['project_id'])
    token = store.issue_ticket(b['logical_agent_id'])
    with pytest.raises(AgentError) as e:
        store.join(token,runtime_thread_id=a['runtime_thread_id'],project_id=b['project_id'],conversation_id=cid,browser_verified=True)
    assert e.value.code == 'THREAD_ALREADY_BOUND'
    assert store.ticket_target(token)['logical_agent_id'] == b['logical_agent_id']


def test_turn_identity_idempotence_and_transport_is_not_done(store):
    a = joined(store)
    aid = a['logical_agent_id']
    turn = store.latest_turn(aid)
    assert store.record_turn(aid,turn_exchange_id=turn['turn_exchange_id']) == turn
    store.observe_turn(aid,1,termination_class='transport_only',evidence={'sse':True,'cookie':'never'})
    assert store.get(aid)['state'] == 'RUNNING'
    store.observe_turn(aid,1,termination_class='trusted_final',evidence={'graph_terminal':True})
    assert store.get(aid)['state'] == 'IDLE' and store.get(aid)['completed_at'] is None
    failed = joined(store,'failed-recovery',a['logical_agent_id'])
    store.observe_turn(failed['logical_agent_id'],1,termination_class='failed',evidence={'graph_terminal':False})
    assert store.get(failed['logical_agent_id'])['state'] == 'DEAD_RECOVERABLE'
    store.set_state(failed['logical_agent_id'],'BLOCKED',expected='DEAD_RECOVERABLE')
    with pytest.raises(AgentError):
        store.complete(aid,'done without checkpoint')
    cp = store.checkpoint(aid,'verified result',runtime_snapshot={'last_tool':'pytest','secret':'never'},goal_snapshot={'outcome':'pass','unknown':'never'})
    assert 'secret' not in cp['runtime_snapshot_json']
    assert store.complete(aid,'explicitly complete')['state'] == 'DONE'
    assert store.complete(aid,'same completion')['state'] == 'DONE'
    store.record_turn(aid,turn_exchange_id='post-completion-turn')
    assert store.get(aid)['state'] == 'DONE'
    with pytest.raises(AgentError):
        store.set_state(aid,'RUNNING')


def test_checkpoint_public_and_material_progress(store):
    a = joined(store)
    aid = a['logical_agent_id']
    store.checkpoint(aid,'secret=abc public result',runtime_snapshot={'job_ids':['job-a'],'successful_action_count':2})
    first = store.get(aid)['progress_version']
    store.checkpoint(aid,'secret=abc public result',runtime_snapshot={'job_ids':['job-a'],'successful_action_count':2})
    assert store.get(aid)['progress_version'] == first
    cp = store.checkpoint(aid,'new result',pending_work='integration test')
    assert store.get(aid)['progress_version'] == first+1 and cp['seq'] == 3


def test_history_scope_incremental_archive_provenance_and_visible_only(store):
    root = joined(store)
    a = joined(store,'decoder',root['logical_agent_id'])
    b = joined(store,'hardware',root['logical_agent_id'])
    message = {'message_id':'copied-message','role':'assistant','channel':'final','visible_text':'核对解码结果 alpha-beta public text','content_type':'text'}
    assert store.archive_message(a['logical_agent_id'],message)
    assert not store.archive_message(a['logical_agent_id'],message)
    assert store.archive_message(b['logical_agent_id'],message)  # id may appear in copied conversations
    assert not store.archive_message(a['logical_agent_id'],{**message,'channel':'analysis','visible_text':'hidden-thought'})
    assert not store.archive_message(a['logical_agent_id'],{**message,'hidden':True})
    assert not store.archive_message(a['logical_agent_id'],{**message,'role':'tool'})
    rows = store.history_search(a['logical_agent_id'],'解码结果',scope='project')
    assert len(rows) == 2 and {r['conversation_id'] for r in rows} == {a['conversation_id'],b['conversation_id']}
    assert all(r['message_id']=='copied-message' and r['turn_epoch']==1 for r in rows)
    own = store.history_search(a['logical_agent_id'],'alpha-beta',scope='lineage')
    assert len(own) == 1 and own[0]['agent_id'] == a['logical_agent_id']
    got = store.history_get(a['logical_agent_id'],own[0]['source_handle'],scope='self',length=5)
    assert len(got['text']) == 5 and got['truncated']
    forbidden = [r for r in rows if r['agent_id']==b['logical_agent_id']][0]
    with pytest.raises(AgentError):
        store.history_get(a['logical_agent_id'],forbidden['source_handle'],scope='lineage')
    with pytest.raises(AgentError):
        store.history_search(a['logical_agent_id'],'text',scope='all_local')
    assert store.history_search(a['logical_agent_id'],'hidden-thought') == []


def test_mailbox_ack_and_delivery_are_agent_scoped(store):
    a = joined(store)
    b = joined(store,'reviewer',a['logical_agent_id'])
    message = store.enqueue(a['logical_agent_id'],b['logical_agent_id'],'请核对接口')
    assert store.inbox(a['logical_agent_id']) == []
    assert len(store.inbox(b['logical_agent_id'],deliver=True)) == 1
    assert store.inbox(b['logical_agent_id'],deliver=True) == []
    assert len(store.inbox(b['logical_agent_id'])) == 1  # lost response remains explicitly retrievable
    store.inbox(a['logical_agent_id'],ack_ids=[message['message_id']])
    assert len(store.inbox(b['logical_agent_id'])) == 1
    assert store.inbox(b['logical_agent_id'],ack_ids=[message['message_id']]) == []


def test_recovery_claim_race_and_crash_reconciliation(store):
    a = joined(store)
    aid = a['logical_agent_id']
    with ThreadPoolExecutor(max_workers=6) as pool:
        results = list(pool.map(lambda n: store.claim_recovery(aid,1,claimed_by=f'wd-{n}',expected_tail_message_id='tail-a'),range(6)))
    assert sum(r['status']=='claimed' for r in results) == 1
    op = next(r['operation'] for r in results if r['status']=='claimed')
    oid = op['operation_id']
    store.advance_operation(oid,'CLAIMED','PRECHECKED')
    store.advance_operation(oid,'PRECHECKED','SUBMITTING')
    reloaded = AgentStore(store.config)
    retry = reloaded.claim_recovery(aid,1,claimed_by='restart',expected_tail_message_id='tail-a')
    assert retry['status'] == 'reconcile_required' and retry['operation']['operation_id'] == oid
    with pytest.raises(AgentError):
        store.advance_operation(oid,'SUBMITTING','PRECHECKED')
    store.advance_operation(oid,'SUBMITTING','RECONCILING')
    with pytest.raises(AgentError) as e:
        store.advance_operation(oid,'RECONCILING','RESUMED')
    assert e.value.code == 'RESUME_PROOF_REQUIRED'
    store.record_turn(aid,turn_exchange_id='new-real-tx',user_message_id='new-user')
    store.advance_operation(oid,'RECONCILING','RESUMED',observed_new_turn_exchange_id='new-real-tx',observed_user_message_id='new-user')
    assert store.get(aid)['turn_epoch'] == 2
    assert store.open_operations() == []


def test_expired_pre_submit_can_reclaim_but_submitting_cannot(store):
    now = [1000.0]
    store.clock = lambda:now[0]
    a = joined(store)
    aid = a['logical_agent_id']
    old = store.claim_recovery(aid,1,claimed_by='a',expected_tail_message_id='tail',ttl_seconds=5)['operation']
    now[0] += 6
    with pytest.raises(AgentError):
        store.advance_operation(old['operation_id'],'CLAIMED','PRECHECKED')
    new = store.claim_recovery(aid,1,claimed_by='b',expected_tail_message_id='tail')['operation']
    assert old['operation_id'] != new['operation_id']
    assert store.operation(old['operation_id'])['state'] == 'ABORTED'
    store.advance_operation(new['operation_id'],'CLAIMED','PRECHECKED')
    store.advance_operation(new['operation_id'],'PRECHECKED','SUBMITTING')
    now[0] += 1000
    assert store.claim_recovery(aid,1,claimed_by='c',expected_tail_message_id='tail')['status'] == 'reconcile_required'


def test_external_activity_invalidates_pre_submit_claim(store):
    a = joined(store)
    aid = a['logical_agent_id']
    oid = store.claim_recovery(aid,1,claimed_by='wd',expected_tail_message_id='tail')['operation']['operation_id']
    store.advance_operation(oid,'CLAIMED','PRECHECKED')
    store.set_state(aid,'EXTERNAL_ACTIVITY')
    with pytest.raises(AgentError):
        store.advance_operation(oid,'PRECHECKED','SUBMITTING')
    assert store.operation(oid)['state'] == 'ABORTED'


def test_no_progress_recovery_limit_persists_blocked(store):
    a = joined(store)
    aid = a['logical_agent_id']
    for epoch in range(1,4):
        oid = store.claim_recovery(aid,epoch,claimed_by='wd',expected_tail_message_id='tail')['operation']['operation_id']
        store.advance_operation(oid,'CLAIMED','PRECHECKED')
        store.advance_operation(oid,'PRECHECKED','SUBMITTING')
        store.advance_operation(oid,'SUBMITTING','SUBMITTED',observed_user_message_id=f'user-{epoch}')
        store.record_turn(aid,turn_exchange_id=f'tx-{epoch}',user_message_id=f'user-{epoch}')
        store.advance_operation(oid,'SUBMITTED','RESUMED',observed_new_turn_exchange_id=f'tx-{epoch}')
    assert store.claim_recovery(aid,4,claimed_by='wd',expected_tail_message_id='tail')['status'] == 'blocked'
    assert AgentStore(store.config).get(aid)['state'] == 'BLOCKED'


def test_backoff_and_single_service_owner(store):
    now = [1000.0]
    store.clock = lambda:now[0]
    a = joined(store)
    store.backoff(a['logical_agent_id'],60)
    with pytest.raises(AgentError) as e:
        store.claim_recovery(a['logical_agent_id'],1,claimed_by='wd',expected_tail_message_id='tail')
    assert e.value.code == 'RECOVERY_BACKOFF'
    assert store.service_claim('supervisor','one',20)
    assert not store.service_claim('supervisor','two',20)
    store.service_release('supervisor','two')
    assert not store.service_claim('supervisor','two',20)
    now[0] += 21
    assert store.service_claim('supervisor','two',20)
    assert store.events_after(0) and store.latest_event_seq()>0


def test_secret_redaction():
    text = public_text('ga_ticket_abcdef Bearer abc.secret token access_token=hello password=world sk-abcdefghijklmnop')
    assert 'ga_ticket_' not in text and 'hello' not in text and 'world' not in text
    assert 'abc.secret' not in text and 'sk-' not in text


def test_watchdog_generation_schedule_persists_and_retires(store):
    first = store.allocate_watchdog('supervisor-thread', born_at=100.0, retire_at=160.0)
    a = store.create(name='watchdog-001', role='监督业务 Agent', workspace=store.config.default_work_root,
                     project_id='project-fixture', owner_thread_id='supervisor-thread', kind='watchdog',
                     watchdog_generation=1)
    bound = store.bind_watchdog(1, a['logical_agent_id'])
    assert bound['state'] == 'ALLOCATED' and bound['logical_agent_id'] == a['logical_agent_id']
    cid = str(uuid.uuid4())
    token = store.issue_ticket(a['logical_agent_id'])
    store.bind_conversation(a['logical_agent_id'],cid,'https://chatgpt.com/c/'+cid,a['project_id'])
    store.join(token,runtime_thread_id='thread-watchdog-'+uuid.uuid4().hex,project_id=a['project_id'],conversation_id=cid,browser_verified=True)
    active = store.activate_watchdog(a['logical_agent_id'])
    assert active['state'] == 'ACTIVE'
    second = store.allocate_watchdog('supervisor-thread', born_at=130.0, retire_at=190.0)
    assert (first['generation'], second['generation']) == (1, 2)
    before = store.watchdog_reconcile(now=159.0)
    assert before['active_count'] == 1 and before['active'][0]['generation'] == 1
    after = store.watchdog_reconcile(now=161.0)
    assert after['retired'] == []
    assert after['active_count'] == 1
    assert store.watchdog_rows()[0]['state'] == 'ACTIVE'
    assert store.get(a['logical_agent_id'])['state'] != 'CLOSED'
    b = store.create(name='watchdog-002', role='监督业务 Agent', workspace=store.config.default_work_root,
                     project_id='project-fixture', owner_thread_id='supervisor-thread', kind='watchdog',
                     watchdog_generation=2)
    store.bind_watchdog(2, b['logical_agent_id'])
    cid2 = str(uuid.uuid4())
    store.bind_conversation(b['logical_agent_id'],cid2,'https://chatgpt.com/c/'+cid2,b['project_id'])
    store.join(store.issue_ticket(b['logical_agent_id']),runtime_thread_id='thread-watchdog-2',
               project_id=b['project_id'],conversation_id=cid2,browser_verified=True)
    store.activate_watchdog(b['logical_agent_id'])
    store.set_watchdog_state(1,'RETIRING',replacement=2)
    retired = store.watchdog_reconcile(now=161.0)
    assert retired['retired'] == [1]
    assert store.watchdog_rows()[0]['state'] == 'RETIRED'
    assert store.get(a['logical_agent_id'])['state'] == 'CLOSED'
    assert AgentStore(store.config).watchdog_rows()[1]['generation'] == 2


def test_terminal_watchdog_closes_logical_agent_and_repairs_legacy_state(store):
    store.allocate_watchdog('scope-terminal', born_at=100.0, retire_at=160.0)
    first = store.create(name='watchdog-terminal-1', role='监督业务 Agent', workspace=store.config.default_work_root,
                         project_id='project-fixture', owner_thread_id='scope-terminal', kind='watchdog',
                         watchdog_generation=1)
    store.bind_watchdog(1, first['logical_agent_id'])
    cid1 = str(uuid.uuid4())
    token1 = store.issue_ticket(first['logical_agent_id'])
    store.bind_conversation(first['logical_agent_id'], cid1, 'https://chatgpt.com/c/' + cid1, first['project_id'])
    store.join(token1, runtime_thread_id='thread-watchdog-terminal-1', project_id=first['project_id'],
               conversation_id=cid1, browser_verified=True)
    store.activate_watchdog(first['logical_agent_id'])
    store.set_watchdog_state(1, 'CLOSED')
    assert store.get(first['logical_agent_id'])['state'] == 'CLOSED'

    store.allocate_watchdog('scope-terminal', born_at=130.0, retire_at=190.0)
    second = store.create(name='watchdog-terminal-2', role='监督业务 Agent', workspace=store.config.default_work_root,
                          project_id='project-fixture', owner_thread_id='scope-terminal', kind='watchdog',
                          watchdog_generation=2)
    store.bind_watchdog(2, second['logical_agent_id'])
    cid2 = str(uuid.uuid4())
    token2 = store.issue_ticket(second['logical_agent_id'])
    store.bind_conversation(second['logical_agent_id'], cid2, 'https://chatgpt.com/c/' + cid2, second['project_id'])
    store.join(token2, runtime_thread_id='thread-watchdog-terminal-2', project_id=second['project_id'],
               conversation_id=cid2, browser_verified=True)
    store.activate_watchdog(second['logical_agent_id'])
    # Simulate the historical bug: lifecycle row was closed while the logical
    # Watchdog Agent was accidentally left RUNNING.
    with store.transaction() as connection:
        connection.execute("UPDATE watchdogs SET state='CLOSED' WHERE generation=2")
        connection.execute("UPDATE agents SET state='RUNNING',closed_at=NULL WHERE logical_agent_id=?",
                           (second['logical_agent_id'],))
    assert store.get(second['logical_agent_id'])['state'] == 'RUNNING'

    reopened = AgentStore(store.config)
    assert reopened.get(second['logical_agent_id'])['state'] == 'CLOSED'


def test_child_requires_delegation_reason_and_parallel_cap(store):
    root = create(store)
    with pytest.raises(AgentError) as err:
        store.create(name='audit-one', role='接口验收', workspace=store.config.default_work_root,
                     project_id='project-fixture', owner_thread_id='owner-fixture', parent_id=root['logical_agent_id'])
    assert err.value.code == 'DELEGATION_REASON_REQUIRED'

    # Merely CREATED / join-pending children are stalled allocation rows, not
    # real Ultra workers, so they must not consume the parallel execution cap.
    first = create(store,'audit-one',root['logical_agent_id'])
    second = create(store,'audit-two',root['logical_agent_id'])
    third = create(store,'audit-three',root['logical_agent_id'])
    fourth = create(store,'audit-four',root['logical_agent_id'])
    assert fourth['parent_agent_id'] == root['logical_agent_id']

    # Once three children are actually joined/running, further spawns are
    # blocked even though the fourth stalled row still exists.
    joined_rows = []
    for child in (first, second, third):
        cid = str(uuid.uuid4())
        token = store.issue_ticket(child['logical_agent_id'])
        store.bind_conversation(child['logical_agent_id'], cid, 'https://chatgpt.com/c/' + cid, child['project_id'])
        joined_rows.append(store.join(
            token, runtime_thread_id='thread-' + uuid.uuid4().hex,
            project_id=child['project_id'], conversation_id=cid, browser_verified=True,
        ))
    with pytest.raises(AgentError) as err:
        create(store,'audit-five',root['logical_agent_id'])
    assert err.value.code == 'PARALLEL_CHILD_LIMIT'

    store.set_state(joined_rows[0]['logical_agent_id'],'CLOSED')
    fifth = create(store,'audit-five',root['logical_agent_id'])
    assert fifth['parent_agent_id'] == root['logical_agent_id']
    assert fourth['state'] == 'CREATED'


def test_parent_complete_waits_for_all_children(store):
    root = joined(store)
    child = joined(store,'child-audit',root['logical_agent_id'])
    store.checkpoint(root['logical_agent_id'],'root checkpoint')
    with pytest.raises(AgentError) as err:
        store.complete(root['logical_agent_id'],'root complete')
    assert err.value.code == 'CHILDREN_STILL_RUNNING'
    store.checkpoint(child['logical_agent_id'],'child checkpoint')
    store.complete(child['logical_agent_id'],'child complete')
    assert store.complete(root['logical_agent_id'],'root complete')['state'] == 'DONE'


def test_public_watchdog_snapshot_redacts_root_lease_id(store):
    root_lease_id = "a" * 32
    internal = store.set_watchdog_scope(
        "thread-public-root",
        enabled=True,
        manual_disabled=False,
        source="test",
        root_lease_id=root_lease_id,
    )
    assert internal["root_lease_id"] == root_lease_id
    snapshot = store.sync_public_relations()
    public = next(row for row in snapshot["watchdogs"] if row["scope_thread_id"] == "thread-public-root")
    assert "root_lease_id" not in public
    assert public["root_monitored"] is False
    assert public["mode"] == "manual"


def test_public_relations_retries_a_transient_windows_replace_lock(store, monkeypatch):
    original_replace = os.replace
    calls = 0

    def replace_once_locked(source, target):
        nonlocal calls
        calls += 1
        if calls == 1:
            raise PermissionError("temporary lock")
        return original_replace(source, target)

    monkeypatch.setattr("chatgpt_agent_store.os.replace", replace_once_locked)
    snapshot = store.sync_public_relations()
    assert snapshot["version"] == 1
    assert calls == 2


def test_terminal_agent_revokes_pending_ticket_and_closes_native_intents(store):
    a=create(store,'terminal-native')
    aid=a['logical_agent_id']
    token=store.issue_ticket(aid)
    prepared=store.prepare_web_operation(aid,'terminal-prepared','spawn')
    store.set_state(aid,'CLOSED')
    with pytest.raises(AgentError) as error:
        store.ticket_target(token)
    assert error.value.code=='TICKET_INVALID'
    with store.connection() as c:
        row=c.execute('SELECT state,error_class FROM web_operations WHERE operation_id=?',(prepared['operation_id'],)).fetchone()
    assert row['state']=='ABORTED' and row['error_class']=='CLOSED'

    b=create(store,'terminal-ambiguous')
    bid=b['logical_agent_id']
    operation=store.prepare_web_operation(bid,'terminal-reconciling','spawn')
    store.update_web_operation(operation['operation_id'],'PREPARED','PREPARED',prompt_sha256='a'*64)
    store.update_web_operation(operation['operation_id'],'PREPARED','SUBMITTING')
    store.update_web_operation(operation['operation_id'],'SUBMITTING','RECONCILING',error_class='TimeoutError')
    store.set_state(bid,'BLOCKED')
    with store.connection() as c:
        row=c.execute('SELECT state,error_class FROM web_operations WHERE operation_id=?',(operation['operation_id'],)).fetchone()
    assert row['state']=='BLOCKED' and row['error_class']=='TimeoutError'


def test_complete_rejects_unreconciled_native_web_submission(store):
    a=joined(store,'complete-native-gate')
    aid=a['logical_agent_id']
    store.checkpoint(aid,'ready except native submit')
    store.prepare_web_operation(aid,'complete-native-gate','followup')
    with pytest.raises(AgentError) as error:
        store.complete(aid,'must not complete yet')
    assert error.value.code=='WEB_RECONCILIATION_REQUIRED'
    assert error.value.details['operations'][0]['state']=='PREPARED'


def test_reopen_repairs_legacy_terminal_ticket_and_native_operation(store):
    a=create(store,'legacy-terminal-native')
    aid=a['logical_agent_id']
    store.issue_ticket(aid)
    operation=store.prepare_web_operation(aid,'legacy-terminal-native','spawn')
    with store.transaction() as c:
        c.execute("UPDATE agents SET state='CLOSED',closed_at=? WHERE logical_agent_id=?",(store.clock(),aid))
    reopened=AgentStore(store.config)
    with reopened.connection() as c:
        ticket=c.execute("SELECT status FROM spawn_tickets WHERE logical_agent_id=? ORDER BY expires_at DESC LIMIT 1",(aid,)).fetchone()
        web=c.execute('SELECT state,error_class FROM web_operations WHERE operation_id=?',(operation['operation_id'],)).fetchone()
    assert ticket['status']=='revoked'
    assert web['state']=='ABORTED' and web['error_class']=='legacy-CLOSED'


def test_settings_prefix_and_delete_are_exact(store):
    store.set_setting('agent_tab:alpha','target-a')
    store.set_setting('agent_tab:beta','target-b')
    store.set_setting('agentXtab:other','target-c')
    assert store.settings_prefix('agent_tab:')=={
        'agent_tab:alpha':'target-a','agent_tab:beta':'target-b',
    }
    assert store.delete_setting('agent_tab:alpha') is True
    assert store.delete_setting('agent_tab:alpha') is False
    assert store.settings_prefix('agent_tab:')=={'agent_tab:beta':'target-b'}


def test_durable_handoff_checkpoint_complete_and_artifacts(store):
    root = joined(store,'handoff-root')
    child = joined(store,'handoff-child',root['logical_agent_id'])
    workspace = Path(child['workspace'])
    workspace.mkdir(parents=True,exist_ok=True)
    artifact = workspace/'handoff-result.txt'
    artifact.write_text('verified handoff result',encoding='utf-8')

    store.checkpoint(child['logical_agent_id'],'child verified result',pending_work='finish parent integration',artifacts=['handoff-result.txt'])
    first = store.latest_handoff(child['logical_agent_id'])
    assert first['status']=='checkpoint'
    assert first['recipient_thread_id']==root['runtime_thread_id']
    assert first['summary']=='child verified result'
    assert first['pending_work']=='finish parent integration'
    assert first['artifacts'][0]['path']=='handoff-result.txt'
    assert first['artifacts'][0]['exists'] is True
    assert len(first['artifacts'][0]['sha256'])==64

    store.complete(child['logical_agent_id'],'child complete')
    final = store.latest_handoff(child['logical_agent_id'])
    assert final['status']=='completed'
    assert final['completion_summary']=='child complete'
    assert final['artifacts'][0]['path']=='handoff-result.txt'

    delivered = store.handoff_inbox(root['runtime_thread_id'],deliver=True)
    assert {item['status'] for item in delivered} == {'checkpoint','completed'}
    assert store.handoff_inbox(root['runtime_thread_id'],deliver=True)==[]
    persistent = store.handoff_inbox(root['runtime_thread_id'])
    assert len(persistent)==2
    store.handoff_inbox(root['runtime_thread_id'],ack_ids=[item['handoff_id'] for item in persistent])
    assert store.handoff_inbox(root['runtime_thread_id'])==[]


def test_root_worker_handoff_targets_native_controller_thread(store):
    worker = joined(store,'native-root-worker')
    store.checkpoint(worker['logical_agent_id'],'root worker checkpoint',pending_work='return to controller')
    handoffs = store.handoff_inbox(worker['owner_thread_id'])
    assert len(handoffs)==1
    assert handoffs[0]['child_agent_id']==worker['logical_agent_id']
    assert handoffs[0]['parent_agent_id'] is None
    assert handoffs[0]['status']=='checkpoint'


def test_failed_and_closed_child_salvage_latest_public_checkpoint(store):
    root = joined(store,'salvage-root')
    child = joined(store,'salvage-child',root['logical_agent_id'])
    store.checkpoint(child['logical_agent_id'],'verified before failure',pending_work='rerun final test')
    store.observe_turn(child['logical_agent_id'],1,termination_class='failed',evidence={'graph_terminal':False})
    interrupted = store.latest_handoff(child['logical_agent_id'])
    assert interrupted['status']=='interrupted'
    assert interrupted['summary']=='verified before failure'
    assert interrupted['pending_work']=='rerun final test'
    assert interrupted['evidence']=='turn:failed'
    store.set_state(child['logical_agent_id'],'BLOCKED',expected='DEAD_RECOVERABLE')
    blocked = store.latest_handoff(child['logical_agent_id'])
    assert blocked['status']=='blocked'
    assert blocked['summary']=='verified before failure'


def test_parent_complete_backfills_legacy_terminal_child_handoff(store):
    root = joined(store,'legacy-handoff-root')
    child = joined(store,'legacy-handoff-child',root['logical_agent_id'])
    store.checkpoint(child['logical_agent_id'],'legacy child result')
    store.complete(child['logical_agent_id'],'legacy child complete')
    with store.transaction() as c:
        c.execute('DELETE FROM handoffs WHERE child_agent_id=?',(child['logical_agent_id'],))
    assert store.latest_handoff(child['logical_agent_id']) is None
    store.checkpoint(root['logical_agent_id'],'root final checkpoint')
    assert store.complete(root['logical_agent_id'],'root complete')['state']=='DONE'
    backfilled = store.latest_handoff(child['logical_agent_id'])
    assert backfilled is not None and backfilled['status']=='completed'
    assert backfilled['summary']=='legacy child result'


def test_checkpoint_artifacts_must_stay_inside_child_workspace(store,tmp_path):
    child = joined(store,'artifact-boundary')
    outside = tmp_path/'outside.txt'
    outside.write_text('no',encoding='utf-8')
    with pytest.raises(AgentError) as error:
        store.checkpoint(child['logical_agent_id'],'should reject outside',artifacts=[str(outside)])
    assert error.value.code=='ARTIFACT_OUTSIDE_WORKSPACE'


def test_trusted_final_turn_without_checkpoint_still_handoffs_public_result(store):
    root = joined(store,'turn-final-root')
    child = joined(store,'turn-final-child',root['logical_agent_id'])
    store.archive_message(child['logical_agent_id'],{
        'role':'assistant','channel':'final','content_type':'text','visible_text':'Visible partial audit result',
        'message_id':'msg-turn-final','conversation_id':child['conversation_id'],'turn_epoch':1,'source':'web_graph',
    })
    store.observe_turn(child['logical_agent_id'],1,termination_class='trusted_final',evidence={'graph_terminal':True})
    handoff=store.latest_handoff(child['logical_agent_id'])
    assert handoff['status']=='turn_final'
    assert handoff['summary']=='Visible partial audit result'
    assert handoff['evidence']=='turn:trusted_final'
    assert store.get(child['logical_agent_id'])['state']=='IDLE'


def test_startup_backfills_preprotocol_terminal_handoff(store):
    root = joined(store,'migration-root')
    child = joined(store,'migration-child',root['logical_agent_id'])
    store.checkpoint(child['logical_agent_id'],'legacy checkpoint evidence',pending_work='legacy pending work')
    store.complete(child['logical_agent_id'],'legacy completion evidence')
    with store.transaction() as connection:
        connection.execute('DELETE FROM handoffs WHERE child_agent_id=?',(child['logical_agent_id'],))
    assert store.latest_handoff(child['logical_agent_id']) is None

    reloaded=AgentStore(store.config)
    migrated=reloaded.latest_handoff(child['logical_agent_id'])
    assert migrated is not None
    assert migrated['status']=='completed'
    assert migrated['summary']=='legacy checkpoint evidence'
    # Pre-protocol DONE cleared agents.pending_work and checkpoints never stored
    # that field, so migration must not invent data that is no longer persisted.
    assert migrated['pending_work']==''
    assert migrated['completion_summary']=='legacy completion evidence'
    assert migrated['evidence']=='legacy-migration:DONE'
