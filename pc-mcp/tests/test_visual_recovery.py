"""Recovery and stale-proof regression cases; all lease files are temporary fixtures."""
from __future__ import annotations
import json
from datetime import timedelta
from pathlib import Path
import pytest
import runtime
import server
from mode_commands import parse_visual_duration
from test_server import isolate_runtime, call_tool
from test_visual_modes import acquire, record, invariant, observation, final_review
from test_visual_images import raw_tool

@pytest.fixture(autouse=True)
def sandbox(monkeypatch, tmp_path):
    isolate_runtime(monkeypatch, tmp_path)

@pytest.mark.parametrize('operation', ['renew','require','context','checkpoint','status','release','ensure'])
def test_legacy_short_expiry_is_repaired_before_discarding_active_modes(tmp_path, operation):
    lease=acquire(tmp_path)
    identity=lease['lease_id']
    value=record(lease)
    value['stay_until_at']=runtime.utc_iso(runtime.utc_now()+timedelta(hours=1))
    value['visual_mode']={**runtime._default_visual_state(),'active':True,'status':'active','until_at':runtime.utc_iso(runtime.utc_now()+timedelta(hours=2))}
    value['expires_at']=runtime.utc_iso(runtime.utc_now()-timedelta(seconds=1))
    # Simulate an old-version or interrupted record, deliberately outside the new
    # writer, inside the pytest sandbox only. Production state is never touched.
    runtime._lease_path(identity).write_text(json.dumps(value),encoding='utf-8')
    if operation=='renew': runtime.renew_workspace(identity,ttl_seconds=60)
    elif operation=='require': runtime.require_execute_lease(identity,[])
    elif operation=='context': runtime.get_visual_context(identity)
    elif operation=='checkpoint': runtime.update_workspace_execution_checkpoint(identity,tool_name='test',side_effect=False)
    elif operation=='status': assert runtime.workspace_status()['count']==1
    elif operation=='release':
        with pytest.raises(runtime.RuntimePolicyError,match='驻场'): runtime.release_workspace(identity)
    elif operation=='ensure':
        with pytest.raises(runtime.RuntimePolicyError,match='驻场'): runtime.ensure_workspace_releasable(identity)
    assert runtime._lease_path(identity).is_file()
    invariant(record(lease))


def test_reference_changes_require_fresh_final_observations(tmp_path):
    lease=acquire(tmp_path)
    identity=lease['lease_id']
    runtime.set_visual_mode(identity,criteria=['Original requirement'])
    observation(lease,'desktop-final',(1440,900))
    observation(lease,'mobile-final',(390,844))
    runtime.set_visual_mode(identity,action='update',criteria=['Changed requirement'])
    with pytest.raises(runtime.RuntimePolicyError,match='重新取得最终观察'):
        runtime.set_visual_mode(identity,action='complete',review=final_review())
    assert record(lease)['visual_mode']['active']


@pytest.mark.parametrize('field,value',[('critical_differences',False),('major_differences',0.0),('minor_differences',True),('minor_differences',-1)])
def test_visual_difference_counts_are_strict_nonnegative_integers(tmp_path, field, value):
    lease=acquire(tmp_path); identity=lease['lease_id']
    runtime.set_visual_mode(identity)
    observation(lease,'desktop-final',(1440,900));observation(lease,'mobile-final',(390,844))
    review=final_review();review[field]=value
    with pytest.raises(runtime.RuntimePolicyError): runtime.set_visual_mode(identity,action='complete',review=review)


def test_stay_blocks_done_after_visual_is_completed_without_writing_completion(tmp_path):
    lease=acquire(tmp_path); identity=lease['lease_id']
    runtime.set_visual_mode(identity,duration_seconds=7200)
    runtime.apply_stay_command(identity,'/stay 1h')
    observation(lease,'desktop-final',(1440,900));observation(lease,'mobile-final',(390,844))
    runtime.set_visual_mode(identity,action='complete',review=final_review())
    response=raw_tool('report_progress',{'phase':'done','text':'must not complete','workspace_lease_id':identity})
    assert response.model_dump(by_alias=True).get('isError') is True
    assert runtime._lease_path(identity).exists()
    rows=[]
    if server.ACTIVITY_EVENT_FILE.exists():
        rows=[json.loads(line) for line in server.ACTIVITY_EVENT_FILE.read_text(encoding='utf-8').splitlines()]
    assert not any(row.get('type')=='task.completed' for row in rows)
    assert not any(row.get('type')=='agent.progress' and row.get('phase')=='done' for row in rows)


def test_visual_command_requires_its_own_explicit_verb():
    assert parse_visual_duration('/vision 4h')==14400
    for command in ['/stay 4h','/vision off','hello','/vision 25h']:
        with pytest.raises(ValueError): parse_visual_duration(command)


def test_completed_visual_reopens_review_when_requirements_change(tmp_path):
    lease=acquire(tmp_path); identity=lease['lease_id']
    runtime.set_visual_mode(identity,criteria=['Original'])
    observation(lease,'desktop-final',(1440,900));observation(lease,'mobile-final',(390,844))
    runtime.set_visual_mode(identity,action='complete',review=final_review())
    runtime.set_visual_mode(identity,action='update',criteria=['New criterion'])
    assert record(lease)['visual_mode']['status']=='needs_review'
    with pytest.raises(runtime.RuntimePolicyError): runtime.release_workspace(identity)
    with pytest.raises(runtime.RuntimePolicyError): runtime.set_visual_mode(identity,action='complete',review=final_review())
    observation(lease,'desktop-fresh',(1440,900));observation(lease,'mobile-fresh',(390,844))
    review=final_review();review['final_observation_ids']={'desktop':'desktop-fresh','mobile':'mobile-fresh'}
    runtime.set_visual_mode(identity,action='complete',review=review)
    assert runtime.release_workspace(identity)['status']=='released'


def test_routine_visual_summary_avoids_replaying_image_history(tmp_path):
    lease=acquire(tmp_path); identity=lease['lease_id']
    runtime.set_visual_mode(identity,criteria=['Very long requirements ' * 80])
    observation(lease,'desktop-final',(1440,900))
    compact=runtime.visual_mode_state(record(lease),include_details=False)
    assert 'observations' not in compact and 'criteria' not in compact and 'references' not in compact
    assert compact['latest_observation_id']=='desktop-final'
    assert compact['context_tool']=='get_visual_context'
    full=runtime.get_visual_context(identity)['visual_mode']
    assert full['criteria'] and len(full['observations'])==1
