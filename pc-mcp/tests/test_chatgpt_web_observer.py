import asyncio
import json
from types import SimpleNamespace
import pytest
from machine_config import MachineConfig
from chatgpt_agent_store import AgentStore, digest
from chatgpt_web_observer import PageObserver,SSEDecoder,TurnEvidence,request_class,safe_request,safe_node,trusted_final,exact_topic_frame


@pytest.fixture
def observer(tmp_path):
    config=MachineConfig.from_mapping({},runtime_root=str(tmp_path))
    return PageObserver(SimpleNamespace(url='https://chatgpt.com/c/conversation-test'),AgentStore(config),clock=lambda:1000.0)


def native(prompt='hello',cid=None,parent=None,action='next'):
    return {'requestId':'request-one','request':{'method':'POST','url':'https://chatgpt.com/backend-api/f/conversation',
        'postData':json.dumps({'action':action,'model':'gpt-5-6','thinking_effort':'extra_high','conversation_id':cid,'parent_message_id':parent,
            'messages':[{'id':'user-one','content':{'content_type':'text','parts':[prompt]}}]})}}


def final_node(**updates):
    raw={'id':'final-one','author':{'role':'assistant'},'recipient':'all','channel':'final','status':'finished_successfully',
         'end_turn':True,'content':{'content_type':'text','parts':['public answer']},
         'metadata':{'turn_exchange_id':'turn-one','finish_details':{'type':'stop'}}}
    raw.update(updates)
    return safe_node(raw)


@pytest.mark.parametrize('method,path,kind',[
    ('POST','/backend-api/f/conversation','generation'),('POST','/backend-api/f/conversation/prepare','prepare'),
    ('POST','/backend-api/f/conversation/resume','resume'),('GET','/backend-api/conversation/id','conversation_get'),
    ('GET','/backend-api/f/conversation','other'),('POST','/backend-api/conversation','other'),
])
def test_exact_native_request_routes(method,path,kind):
    assert request_class(method,'https://chatgpt.com'+path)==kind
    assert request_class(method,'https://other.example'+path)=='other'


def test_request_metadata_does_not_persist_prompt_credentials():
    e=native('ga_ticket_supersecret private prompt')
    r=safe_request(e['request']['method'],e['request']['url'],e['request']['postData'])
    assert r['message_ids']==['user-one'] and r['prompt_sha256']==digest('ga_ticket_supersecret private prompt')
    assert 'supersecret' not in json.dumps(r) and 'private prompt' not in json.dumps(r)


def test_sse_incremental_utf8_crlf_and_done_is_only_transport():
    decoder=SSEDecoder()
    raw=('data: '+json.dumps({'type':'message','text':'中文'},ensure_ascii=False)+'\r\n\r\ndata: [DONE]\n\n').encode('utf-8')
    events=[]
    for byte in raw:
        events.extend(decoder.push(bytes([byte])))
    assert events==[{'type':'message','text':'中文'},{'type':'sse_done'}]
    assert decoder.push(b'data: {"type":"tail"}',final=True)==[{'type':'tail'}]
    assert decoder.push(b'data: not-json\n\n')[0]['type']=='sse_unparsed'


def test_sse_buffer_bound():
    d=SSEDecoder()
    assert d.push(b'x'*(2*1024*1024+1))==[] and d.overflow


def test_exact_topic_not_substring_and_native_utf8_encoded_item(observer):
    frame={'topic_id':'topic-one','payload':{'payload':{'type':'heartbeat'}}}
    assert not exact_topic_frame(json.dumps(frame),'topic-on')['matches']
    assert exact_topic_frame(json.dumps(frame),'topic-one')['heartbeats']==1
    observer.evidence.topic_id='topic-one'
    observer.evidence.turn_exchange_id='turn-one'
    encoded='data: '+json.dumps({'type':'conversation_async_status','async_status':3})+'\n\n'
    frame['payload']['payload']={'encoded_item':encoded}
    observer._ws({'response':{'payloadData':json.dumps(frame)}})
    assert observer.evidence.exact_ws_seen and observer.evidence.async_active
    assert observer.ws_frame_count==1 and observer.evidence.last_server_progress_at==1000.0
    assert not observer.error_types


def test_hidden_thoughts_and_tool_content_never_become_public_text():
    hidden=final_node(channel='analysis')
    assert hidden['hidden'] and hidden['visible_text']==''
    assert not trusted_final(hidden,'turn-one')
    tool=final_node(author={'role':'tool'})
    assert tool['visible_text']=='' and tool['hidden']
    invisible=final_node(metadata={'turn_exchange_id':'turn-one','is_visually_hidden_from_conversation':True})
    assert invisible['visible_text']==''


def test_trusted_final_requires_exact_turn_complete_public_assistant():
    n=final_node()
    assert trusted_final(n,'turn-one')
    assert not trusted_final(n,'other-turn')
    for change in [{'end_turn':False},{'status':'in_progress'},{'recipient':'python'},{'channel':'analysis'},{'hidden':True},{'visible_text':''}]:
        assert not trusted_final({**n,**change},'turn-one')


def test_classification_prevents_clock_transport_stop_and_external_false_positives():
    e=TurnEvidence(request_seen=True,started_at=0,turn_exchange_id='tx')
    assert e.classification(100)['state']=='RUNNING'
    assert e.classification(10000)['state']=='SUSPECT' and not e.classification(10000)['recoverable']
    e.disconnected=True
    assert e.classification(10000)['state']=='DETACHED'
    e.trusted_final_at=100;e.graph_seen=True;e.disconnected=False
    assert e.classification(120)['recoverable']
    e.dom_stop=True
    assert not e.classification(120)['recoverable']
    e.external_activity=True
    assert e.classification(120)['state']=='EXTERNAL_ACTIVITY'
    e.external_activity=False;e.dom_stop=False;e.client_interrupted=True
    assert e.classification(120)['reason']=='cancelled' and not e.classification(120)['recoverable']
    failed=TurnEvidence(turn_exchange_id='failed-turn',explicit_terminal='failed')
    assert failed.classification(120)=={'state':'DEAD_RECOVERABLE','reason':'failed','recoverable':True}


def test_native_submission_requires_exact_intent_and_stops_second_post(observer):
    observer.arm('hello')
    observer._request(native())
    assert observer.evidence.request_seen and observer.evidence.user_message_id=='user-one'
    assert not observer.evidence.external_activity
    observer._request(native())
    assert observer.evidence.external_activity and observer.generation_count==2


def test_wrong_parent_conversation_or_unrecognized_text_is_external(observer):
    observer.arm('hello',conversation_id='expected-conversation',expected_parent='expected-parent')
    observer._request(native(cid='wrong',parent='expected-parent'))
    assert observer.evidence.external_activity and observer.intent_request_id is None


def test_stream_handoff_and_done_cannot_fake_completion(observer):
    observer.arm('hello');observer._request(native())
    observer._consume([{'type':'stream_handoff','conversation_id':'conversation-test','turn_exchange_id':'turn-one',
                        'options':[{'type':'subscribe_ws_topic','topic_id':'topic-one'}]},{'type':'sse_done'}])
    assert observer.evidence.turn_exchange_id=='turn-one' and observer.evidence.topic_id=='topic-one'
    assert observer.public()['state']=='RUNNING' and not observer.public()['recoverable']
    observer.nodes={'final-one':final_node()}
    observer._update_graph()
    assert observer.public()['reason']=='trusted_final' and observer.public()['recoverable']


def test_old_turn_final_cannot_end_new_turn(observer):
    observer.evidence.turn_exchange_id='new-turn'
    observer.nodes={'final-one':final_node()}
    observer._update_graph()
    assert observer.evidence.trusted_final_at is None


def test_heartbeat_is_transport_only(observer):
    observer.evidence.topic_id='topic-one'
    observer._ws({'response':{'payloadData':json.dumps({'topic_id':'topic-one','payload':{'payload':{'type':'heartbeat'}}})}})
    assert observer.evidence.last_transport_progress_at==1000.0
    assert observer.evidence.last_server_progress_at is None


def test_immutable_conversation_on_reconnect(observer):
    class Page:
        url='https://chatgpt.com/c/wrong-conversation'
        def is_closed(self):return False
        async def evaluate(self,_):return {'conversation_id':'wrong-conversation','nodes':[]}
    observer.page=Page();observer.expected_conversation='expected-conversation'
    result=asyncio.run(observer.refresh())
    assert result['state']=='EXTERNAL_ACTIVITY'


def test_native_continue_action_is_recognized_only_with_exact_intent(observer):
    observer.arm('继续',conversation_id='conversation-test')
    observer._request(native('继续',cid='conversation-test',action='continue'))
    assert observer.evidence.request_seen
    assert not observer.evidence.external_activity

    other=PageObserver(SimpleNamespace(url='https://chatgpt.com/c/conversation-test'),observer.store,clock=lambda:1000.0)
    other.arm('继续',conversation_id='conversation-test')
    other._request(native('继续',cid='conversation-test',action='variant'))
    assert other.evidence.external_activity


def test_unrecognized_generation_reports_only_safe_protocol_mismatch(observer):
    observer.arm('hello',conversation_id='conversation-test')
    observer._request(native('changed',cid=None,action='variant'))
    assert observer.evidence.external_activity
    errors=' '.join(observer.error_types)
    assert 'prompt_mismatch' in errors
    assert 'action_variant' in errors
    assert 'conversation_missing' in errors
    assert 'hello' not in errors and 'changed' not in errors


def test_intent_hash_accepts_only_contenteditable_whitespace_equivalence(observer):
    observer.arm('line one\n\nline two',conversation_id='conversation-test')
    observer._request(native('line one   line two',cid='conversation-test'))
    assert observer.evidence.request_seen
    assert not observer.evidence.external_activity

    other=PageObserver(SimpleNamespace(url='https://chatgpt.com/c/conversation-test'),observer.store,clock=lambda:1000.0)
    other.arm('line one line two',conversation_id='conversation-test')
    other._request(native('line one changed line two',cid='conversation-test'))
    assert other.evidence.external_activity


def test_terminal_persist_archives_visible_final_before_handoff(tmp_path):
    config=MachineConfig.from_mapping({},runtime_root=str(tmp_path))
    store=AgentStore(config)
    root=store.create(name='observer-root',role='root',workspace=config.default_work_root,
                      project_id='project-fixture',owner_thread_id='controller-thread')
    root_cid='11111111-1111-4111-8111-111111111111'
    root_ticket=store.issue_ticket(root['logical_agent_id'])
    store.bind_conversation(root['logical_agent_id'],root_cid,'https://chatgpt.com/c/'+root_cid,root['project_id'])
    root=store.join(root_ticket,runtime_thread_id='thread-parent',project_id=root['project_id'],
                    conversation_id=root_cid,browser_verified=True)

    child=store.create(name='observer-child',role='child',workspace=config.default_work_root,
                       project_id='project-fixture',owner_thread_id='thread-parent',parent_id=root['logical_agent_id'],
                       delegation_reason='验证 final 归档先于 handoff 持久化')
    child_cid='22222222-2222-4222-8222-222222222222'
    child_ticket=store.issue_ticket(child['logical_agent_id'])
    store.bind_conversation(child['logical_agent_id'],child_cid,'https://chatgpt.com/c/'+child_cid,child['project_id'])
    child=store.join(child_ticket,runtime_thread_id='thread-child',project_id=child['project_id'],
                     conversation_id=child_cid,browser_verified=True)
    store.record_turn(child['logical_agent_id'],turn_exchange_id='turn-one',user_message_id='user-one')

    page=SimpleNamespace(url='https://chatgpt.com/c/'+child_cid)
    watcher=PageObserver(page,store,child['logical_agent_id'],clock=lambda:1000.0)
    watcher.evidence.conversation_id=child_cid
    watcher.evidence.turn_exchange_id='turn-one'
    watcher.evidence.user_message_id='user-one'
    watcher.evidence.graph_seen=True
    watcher.nodes={'final-one':final_node()}
    watcher._update_graph()
    watcher.snapshot={'conversation_id':child_cid}
    watcher._persist()

    handoff=store.latest_handoff(child['logical_agent_id'])
    assert handoff['status']=='turn_final'
    assert handoff['summary']=='public answer'
    assert store.get(child['logical_agent_id'])['state']=='IDLE'
