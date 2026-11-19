"""Passive, exact-identity ChatGPT turn observation.

Transport completion is not model completion. Only lifecycle metadata and public
message text may be persisted; no headers, credentials or hidden reasoning.
"""
from __future__ import annotations
import asyncio
import base64
import codecs
from dataclasses import dataclass
import json
import re
import time
from typing import Any, Callable
from urllib.parse import urlsplit
from chatgpt_agent_store import AgentError, AgentStore, TERMINAL_AGENT_STATES, digest, public_text


def canonical_prompt(value: str) -> str:
    """Mirror the native contenteditable equivalence used before submit."""
    value=re.sub(r'[\u200b\u2060\ufeff]', '', value or '')
    return re.sub(r'\s+', ' ', value).strip()


def request_class(method: str, url: str) -> str:
    p = urlsplit(url)
    if p.scheme != 'https' or p.hostname != 'chatgpt.com':
        return 'other'
    if method.upper() == 'POST':
        return {'/backend-api/f/conversation':'generation','/backend-api/f/conversation/prepare':'prepare',
                '/backend-api/f/conversation/resume':'resume'}.get(p.path,'other')
    if method.upper() == 'GET' and re.fullmatch(r'/backend-api/conversation/[^/]+',p.path):
        return 'conversation_get'
    return 'other'


def safe_request(method: str, url: str, post_data: str | None) -> dict:
    kind = request_class(method,url)
    out = {'method':method.upper(),'kind':kind}
    if kind != 'generation' or not post_data or len(post_data)>1024*1024:
        return out
    try:
        body = json.loads(post_data)
    except (ValueError,TypeError):
        return out
    if not isinstance(body,dict):
        return out
    for key in ('action','model','thinking_effort','conversation_id','parent_message_id','gizmo_id'):
        if isinstance(body.get(key),str):
            out[key] = body[key][:160]
    ids,parts = [],[]
    for message in body.get('messages',[])[:16]:
        if not isinstance(message,dict):
            continue
        if isinstance(message.get('id'),str):
            ids.append(message['id'])
        content = message.get('content') or {}
        if isinstance(content,dict):
            parts.extend(x for x in content.get('parts',[]) if isinstance(x,str))
    out['message_ids'] = ids
    if parts:
        text = '\n'.join(parts)
        out.update(prompt_sha256=digest(canonical_prompt(text)),prompt_utf8_bytes=len(text.encode('utf-8')))
    return out


class SSEDecoder:
    def __init__(self):
        self.decoder = codecs.getincrementaldecoder('utf-8')('replace')
        self.buffer = ''
        self.overflow = False

    def push(self, data: bytes, *, final=False) -> list[dict]:
        self.buffer += self.decoder.decode(data,final=final)
        if len(self.buffer)>2*1024*1024:
            self.buffer = ''
            self.overflow = True
            return []
        out = []
        while True:
            match = re.search(r'\r?\n\r?\n',self.buffer)
            if not match:
                break
            record,self.buffer = self.buffer[:match.start()],self.buffer[match.end():]
            value = self.parse(record)
            if value is not None:
                out.append(value)
        if final and self.buffer.strip():
            value = self.parse(self.buffer)
            self.buffer = ''
            if value is not None:
                out.append(value)
        return out

    @staticmethod
    def parse(record: str) -> dict | None:
        text = '\n'.join(line[5:].lstrip() for line in record.replace('\r','').split('\n') if line.startswith('data:'))
        if text == '[DONE]':
            return {'type':'sse_done'}
        if not text:
            return None
        try:
            value = json.loads(text)
            return value if isinstance(value,dict) else None
        except ValueError:
            return {'type':'sse_unparsed','length':len(text)}


def exact_topic_frame(payload: str, topic_id: str) -> dict:
    out = {'matches':False,'heartbeats':0,'encoded_items':[]}
    if not topic_id or not isinstance(payload,str) or len(payload)>4*1024*1024:
        return out
    try:
        data = json.loads(payload)
    except ValueError:
        return out
    visited = 0
    def walk(value,depth=0):
        nonlocal visited
        visited += 1
        if depth>10 or visited>4096:
            return
        if isinstance(value,list):
            for item in value[:200]:
                walk(item,depth+1)
        elif isinstance(value,dict):
            if value.get('topic_id') == topic_id:
                out['matches'] = True
                envelope = value.get('payload')
                inner = envelope.get('payload') if isinstance(envelope,dict) else None
                if isinstance(inner,dict):
                    if inner.get('type') == 'heartbeat':
                        out['heartbeats'] += 1
                    if isinstance(inner.get('encoded_item'),str):
                        out['encoded_items'].append(inner['encoded_item'])
            for item in value.values():
                if isinstance(item,(dict,list)):
                    walk(item,depth+1)
    walk(data)
    return out


def safe_node(raw: dict, *, parent: str | None = None) -> dict | None:
    if not isinstance(raw,dict) or not isinstance(raw.get('id'),str):
        return None
    meta,author,content = raw.get('metadata') or {},raw.get('author') or {},raw.get('content') or {}
    if not all(isinstance(x,dict) for x in (meta,author,content)):
        return None
    role,channel,ctype = author.get('role'),raw.get('channel'),content.get('content_type')
    visible = (role in {'user','assistant'} and channel in {None,'final','commentary'} and ctype=='text'
               and not meta.get('is_visually_hidden_from_conversation') and not meta.get('is_hidden')
               and (role=='user' or raw.get('recipient') in {None,'all'}))
    text = '\n'.join(x for x in content.get('parts',[]) if isinstance(x,str)) if visible else ''
    finish = meta.get('finish_details') or {}
    return {'message_id':raw['id'],'parent_message_id':parent or meta.get('parent_id'),'role':role,'channel':channel,
        'recipient':raw.get('recipient'),'status':raw.get('status'),'end_turn':raw.get('end_turn'),'content_type':ctype,
        'visible_text':public_text(text),'public_content_sha256':digest(text) if visible else None,
        'hidden':not visible,'turn_exchange_id':meta.get('turn_exchange_id'),
        'finish_type':finish.get('type') if isinstance(finish,dict) else None,'reasoning_status':meta.get('reasoning_status'),
        'create_time':raw.get('create_time'),'is_complete':meta.get('is_complete')}


def trusted_final(node: dict, turn_exchange_id: str) -> bool:
    return bool(turn_exchange_id and node.get('turn_exchange_id')==turn_exchange_id and
        node.get('role')=='assistant' and node.get('recipient')=='all' and node.get('channel') in {None,'final'} and
        node.get('content_type')=='text' and not node.get('hidden') and node.get('visible_text') and
        node.get('status')=='finished_successfully' and node.get('end_turn') is True and node.get('finish_type') in {None,'stop'})


@dataclass
class TurnEvidence:
    conversation_id: str | None = None
    turn_exchange_id: str | None = None
    topic_id: str | None = None
    request_id: str | None = None
    user_message_id: str | None = None
    parent_message_id: str | None = None
    started_at: float | None = None
    last_server_progress_at: float | None = None
    last_transport_progress_at: float | None = None
    trusted_final_at: float | None = None
    explicit_terminal: str | None = None
    client_interrupted: bool = False
    external_activity: bool = False
    dom_stop: bool = False
    graph_active: bool = False
    async_active: bool = False
    disconnected: bool = False
    graph_seen: bool = False
    exact_ws_seen: bool = False
    sse_seen: bool = False
    request_seen: bool = False
    final_message_id: str | None = None

    def classification(self, now: float, *, risk_minutes=95) -> dict:
        if self.external_activity:
            return {'state':'EXTERNAL_ACTIVITY','reason':'unrecognized_user_activity','recoverable':False}
        if self.client_interrupted:
            return {'state':'IDLE','reason':'cancelled','recoverable':False}
        if self.dom_stop or self.graph_active or self.async_active:
            return {'state':'RUNNING','reason':'active_ui_graph_or_async','recoverable':False}
        if self.trusted_final_at is not None and self.graph_seen:
            return {'state':'IDLE','reason':'trusted_final','recoverable':True}
        if self.explicit_terminal and self.turn_exchange_id:
            if self.explicit_terminal == 'failed':
                return {'state':'DEAD_RECOVERABLE','reason':'failed','recoverable':True}
            return {'state':'IDLE','reason':self.explicit_terminal,'recoverable':False}
        if self.disconnected:
            return {'state':'DETACHED','reason':'transport_not_execution','recoverable':False}
        if self.started_at is not None and now-self.started_at>=risk_minutes*60:
            return {'state':'SUSPECT','reason':'risk_age_requires_observation_not_resend','recoverable':False}
        return {'state':'RUNNING' if self.request_seen else 'DETACHED','reason':'no_trusted_terminal_evidence','recoverable':False}

    def public(self,now:float,risk_minutes:float=95) -> dict:
        return {**self.classification(now,risk_minutes=risk_minutes),
            'conversation_id':self.conversation_id,'turn_exchange_id':self.turn_exchange_id,'topic_id':self.topic_id,
            'request_id':self.request_id,'user_message_id':self.user_message_id,'started_at':self.started_at,
            'last_server_progress_at':self.last_server_progress_at,'last_transport_progress_at':self.last_transport_progress_at,
            'trusted_final_at':self.trusted_final_at,'final_message_id':self.final_message_id,
            'evidence':{'sse':self.sse_seen,'ws_exact_topic':self.exact_ws_seen,'graph_terminal':self.trusted_final_at is not None,
                'graph_seen':self.graph_seen,'dom_active':self.dom_stop,'async_active':self.async_active,'request_seen':self.request_seen}}


# Hidden analysis content is excluded inside the page before CDP returns data.
GRAPH_EXPRESSION = r'''() => {
 const safe=m=>{
  if(!m||typeof m!=='object'||typeof m.id!=='string'||!m.author||!m.content)return null;
  const meta=m.metadata||{},role=m.author.role,channel=m.channel,type=m.content.content_type;
  const visible=(role==='user'||role==='assistant')&&(!channel||channel==='final'||channel==='commentary')&&type==='text'&&
   !meta.is_visually_hidden_from_conversation&&!meta.is_hidden&&(role==='user'||!m.recipient||m.recipient==='all');
  return {message_id:m.id,parent_message_id:meta.parent_id||null,role,channel:channel||null,recipient:m.recipient||null,
   status:m.status,end_turn:m.end_turn,content_type:type,hidden:!visible,
   visible_text:visible&&Array.isArray(m.content.parts)?m.content.parts.filter(x=>typeof x==='string').join('\n').slice(0,12000):'',
   turn_exchange_id:meta.turn_exchange_id||null,finish_type:meta.finish_details?.type||null,
   reasoning_status:meta.reasoning_status||null,create_time:typeof m.create_time==='number'?m.create_time:null,is_complete:meta.is_complete};
 };
 const anchor=document.querySelector('[data-message-author-role="assistant"]')||document.querySelector('[data-message-author-role="user"]')||document.querySelector('main');
 const nodes=new Map(),seen=new WeakSet();let visited=0,currentNode=null,projectId=null;
 const walk=(v,depth)=>{
  if(!v||typeof v!=='object'||depth<0||visited>=70000||seen.has(v))return;
  seen.add(v);visited++;
  const n=safe(v);if(n)nodes.set(n.message_id,n);
  if(typeof v.current_node==='string')currentNode=v.current_node;
  if(typeof v.gizmo_id==='string'&&v.gizmo_id.startsWith('g-p-'))projectId=v.gizmo_id;
  if(v.message&&typeof v.parent==='string'){
   const child=safe(v.message);if(child){child.parent_message_id=v.parent;nodes.set(child.message_id,child);}
  }
  if(Array.isArray(v)){for(const item of v.slice(0,1000))walk(item,depth-1);return;}
  for(const [key,item]of Object.entries(v)){
   if(['stateNode','child','sibling','return','alternate'].includes(key)||/authorization|cookie|token|secret|password/i.test(key))continue;
   walk(item,depth-1);
  }
 };
 if(anchor)for(const key of Object.keys(anchor))if(key.startsWith('__reactFiber$')||key.startsWith('__reactProps$')){
  let f=anchor[key];for(let i=0;i<45&&f;i++,f=f.return){walk(f.memoizedProps,9);walk(f.memoizedState,9);walk(f.pendingProps,7);}
 }
 const visible=e=>!!e&&e.getClientRects().length>0;
 const ids=[...document.querySelectorAll('[data-message-id]')].filter(visible).map(e=>e.getAttribute('data-message-id'));
 const stop=document.querySelector('button[data-testid="stop-button"]');
 const buttons=[...document.querySelectorAll('button')].filter(visible);
 const retry=buttons.some(e=>/^(retry|重试)$/i.test((e.innerText||e.getAttribute('aria-label')||'').trim()));
 const alert=[...document.querySelectorAll('[role="alert"],[data-testid*="error"]')].some(e=>visible(e)&&(e.innerText||'').trim());
 const editor=document.querySelector('#prompt-textarea,[contenteditable="true"][role="textbox"]');
 const model=document.querySelector('[data-testid="model-switcher-dropdown-button"]');
 const match=location.pathname.match(/\/c\/([^/?#]+)/);
 return {conversation_id:match?match[1]:null,nodes:[...nodes.values()],current_node:currentNode,project_id:projectId,
  visible_message_ids:ids,tail_message_id:ids.length?ids[ids.length-1]:currentNode,stop_visible:visible(stop),
  retry_visible:retry,has_alert:!!alert,composer_nonempty:!!(editor&&(editor.innerText||editor.textContent||'').trim()),
  model_label:model?(model.innerText||'').trim().slice(0,100):null,title:document.title,graph_visited:visited};
}'''


class PageObserver:
    """One managed tab's passive listeners and bounded readonly snapshots."""
    def __init__(self,page,store:AgentStore,agent_id:str|None=None,*,clock=time.time,on_change:Callable|None=None):
        self.page,self.store,self.agent_id,self.clock = page,store,agent_id,clock
        self.on_change = on_change or (lambda:None)
        self.evidence = TurnEvidence()
        self.cdp = None
        self.decoders = {}
        self.stream_enabled = set()
        self.get_requests = set()
        self.requests: list[dict] = []
        self.pending_tasks:set[asyncio.Task] = set()
        self.intent_sha = None
        self.intent_request_id = None
        self.expected_parent = None
        self.expected_conversation = None
        self.generation_count = 0
        self.ws_frame_count = 0
        self.raw_ws_count = 0
        self.event_types:set[str] = set()
        self.nodes:dict[str,dict] = {}
        self.snapshot = {}
        self.error_types:set[str] = set()
        self.last_graph_signature = None
        self.last_store_signature = None
        self.last_archive_signature = None
        self.stopped = False
        self.project_id = None
        if agent_id:
            a = store.get(agent_id)
            self.project_id,self.expected_conversation = a['project_id'],a['conversation_id']
            previous = store.latest_turn(agent_id)
            if previous:
                self.evidence = TurnEvidence(conversation_id=a['conversation_id'],turn_exchange_id=previous['turn_exchange_id'],
                    topic_id=previous['topic_id'],user_message_id=previous['user_message_id'],started_at=previous['generation_started_at'],
                    last_server_progress_at=previous['last_server_progress_at'],trusted_final_at=previous['trusted_final_at'])

    def _task(self,coroutine):
        task = asyncio.create_task(coroutine)
        self.pending_tasks.add(task)
        def finished(done):
            self.pending_tasks.discard(done)
            if not done.cancelled() and done.exception():
                self.error_types.add(type(done.exception()).__name__)
        task.add_done_callback(finished)
        return task

    async def start(self):
        self.cdp = await self.page.context.new_cdp_session(self.page)
        self.cdp.on('Network.requestWillBeSent',self._request)
        self.cdp.on('Network.responseReceived',lambda e:self._task(self._response(e)))
        self.cdp.on('Network.dataReceived',self._data)
        self.cdp.on('Network.loadingFinished',lambda e:self._task(self._finished(e)))
        self.cdp.on('Network.loadingFailed',self._failed)
        self.cdp.on('Network.webSocketFrameReceived',self._ws)
        self.page.on('close',self._closed)
        await self.cdp.send('Network.enable',{'maxPostDataSize':1024*1024})
        await self.refresh()
        return self

    def arm(self,prompt:str,*,expected_parent:str|None=None,conversation_id:str|None=None):
        if self.intent_sha is not None and self.intent_request_id is None:
            raise AgentError('SUBMISSION_INTENT_ACTIVE','A pending single-submit intent already exists for this tab')
        self.intent_sha,self.intent_request_id = digest(canonical_prompt(prompt)),None
        self.expected_parent,self.expected_conversation = expected_parent,conversation_id
        self.generation_count = 0

    def _request(self,event):
        request = event.get('request') or {}
        summary = safe_request(request.get('method',''),request.get('url',''),request.get('postData'))
        rid = event.get('requestId')
        if summary['kind'] == 'conversation_get':
            self.get_requests.add(rid)
            return
        if summary['kind'] != 'generation':
            return
        self.generation_count += 1
        action=summary.get('action')
        prompt_ok=(summary.get('prompt_sha256')==self.intent_sha)
        conversation_ok=(not self.expected_conversation or summary.get('conversation_id')==self.expected_conversation)
        parent_ok=(not self.expected_parent or summary.get('parent_message_id')==self.expected_parent)
        action_ok=action in {None,'next','continue'}
        recognized=(self.intent_sha is not None and self.intent_request_id is None and
            prompt_ok and action_ok and conversation_ok and parent_ok)
        if not recognized:
            mismatch=[]
            if self.intent_sha is None: mismatch.append('intent_missing')
            elif not prompt_ok: mismatch.append('prompt_mismatch')
            if not action_ok:
                safe_action=action if isinstance(action,str) and re.fullmatch(r'[A-Za-z0-9_-]{1,32}',action) else 'other'
                mismatch.append('action_'+safe_action)
            if not conversation_ok:
                mismatch.append('conversation_missing' if not summary.get('conversation_id') else 'conversation_mismatch')
            if not parent_ok:
                mismatch.append('parent_missing' if not summary.get('parent_message_id') else 'parent_mismatch')
            if self.intent_request_id is not None: mismatch.append('intent_already_consumed')
            self.error_types.add('unrecognized_generation:'+','.join(mismatch or ['unknown']))
            self.evidence.external_activity = True
            self._persist()
            self.on_change()
            return
        self.intent_request_id = rid
        ids = summary.get('message_ids') or []
        self.evidence = TurnEvidence(conversation_id=summary.get('conversation_id'),request_id=rid,
            user_message_id=ids[-1] if ids else None,parent_message_id=summary.get('parent_message_id'),
            started_at=self.clock(),last_transport_progress_at=self.clock(),request_seen=True)
        self.requests.append({**summary,'request_id':rid,'observed_at':self.clock()})
        self.requests = self.requests[-20:]
        self.nodes = {}
        self.last_graph_signature = self.last_store_signature = None
        self.on_change()

    async def _response(self,event):
        rid = event.get('requestId')
        if rid != self.evidence.request_id:
            return
        response = event.get('response') or {}
        status = int(response.get('status') or 0)
        self.evidence.last_transport_progress_at = self.clock()
        if status in {401,403,429}:
            self.error_types.add('HTTP_'+str(status))
        decoder = self.decoders.setdefault(rid,SSEDecoder())
        try:
            result = await self.cdp.send('Network.streamResourceContent',{'requestId':rid})
            self.stream_enabled.add(rid)
            if result.get('bufferedData'):
                self._consume(decoder.push(base64.b64decode(result['bufferedData'])))
        except Exception as exc:
            self.error_types.add('streamResourceContent:'+type(exc).__name__)

    def _data(self,event):
        rid = event.get('requestId')
        if rid != self.evidence.request_id:
            return
        self.evidence.last_transport_progress_at = self.clock()
        if event.get('data') and rid in self.decoders:
            try:
                self._consume(self.decoders[rid].push(base64.b64decode(event['data'])))
            except (ValueError,TypeError):
                self.error_types.add('InvalidStreamEncoding')

    async def _finished(self,event):
        rid = event.get('requestId')
        if rid == self.evidence.request_id:
            self.evidence.last_transport_progress_at = self.clock()
            if rid in self.decoders:
                self._consume(self.decoders[rid].push(b'',final=True))
            if not self.evidence.turn_exchange_id:
                try:
                    result = await self.cdp.send('Network.getResponseBody',{'requestId':rid})
                    body = result.get('body','')
                    raw = base64.b64decode(body) if result.get('base64Encoded') else body.encode('utf-8')
                    self._consume(SSEDecoder().push(raw,final=True))
                except Exception as exc:
                    self.error_types.add('getResponseBody:'+type(exc).__name__)
        if rid in self.get_requests:
            self.get_requests.discard(rid)
            try:
                result = await self.cdp.send('Network.getResponseBody',{'requestId':rid})
                body = result.get('body','')
                if result.get('base64Encoded'):
                    body = base64.b64decode(body).decode('utf-8')
                if len(body)>8*1024*1024:
                    return
                payload = json.loads(body)
                cid = payload.get('conversation_id') or payload.get('id')
                if cid != self.evidence.conversation_id:
                    return
                for raw in (payload.get('mapping') or {}).values():
                    if isinstance(raw,dict):
                        node = safe_node(raw.get('message'),parent=raw.get('parent'))
                        if node:
                            self.nodes[node['message_id']] = node
                if isinstance(payload.get('gizmo_id'),str):
                    self.snapshot['project_id'] = payload['gizmo_id']
                self._update_graph()
            except Exception as exc:
                self.error_types.add('nativeGraph:'+type(exc).__name__)

    def _failed(self,event):
        if event.get('requestId') == self.evidence.request_id:
            # Network cancellation is not necessarily the user pressing Stop.
            self.evidence.last_transport_progress_at = self.clock()
            self.error_types.add('transport_failed')
        self.get_requests.discard(event.get('requestId'))

    def _closed(self,*_):
        self.evidence.disconnected = True
        self.on_change()

    def _ws(self,event):
        self.raw_ws_count += 1
        payload = (event.get('response') or {}).get('payloadData') or ''
        match = exact_topic_frame(payload,self.evidence.topic_id or '')
        if not match['matches']:
            return
        self.ws_frame_count += 1
        self.evidence.exact_ws_seen = True
        self.evidence.last_transport_progress_at = self.clock()
        if match['encoded_items']:
            self.evidence.last_server_progress_at = self.clock()
        for encoded in match['encoded_items']:
            try:
                if not encoded.strip() or encoded.strip()=='[DONE]':
                    continue
                # Native encoded_item currently contains UTF-8 SSE text, not
                # base64. Accept an explicitly decodable fallback without ever
                # retaining the raw item in a log or database.
                raw = encoded.encode('utf-8')
                if not encoded.lstrip().startswith(('data:', '{', '[')):
                    raw = base64.b64decode(encoded,validate=True)
                events = SSEDecoder().push(raw,final=True)
                if not events and raw.lstrip().startswith((b'{',b'[')):
                    value = json.loads(raw)
                    events = value if isinstance(value,list) else [value]
                self._consume([x for x in events if isinstance(x,dict)])
            except (ValueError,TypeError):
                self.error_types.add('ws_encoded_item_unparsed')
        self.on_change()

    def _consume(self,events:list[dict]):
        for event in events:
            kind = event.get('type')
            if isinstance(kind,str):
                self.event_types.add(kind[:80])
            self.evidence.sse_seen = True
            if kind in {'sse_done','sse_unparsed','resume_conversation_token','server_ste_metadata','server_perf_stats'}:
                continue
            if kind == 'stream_handoff':
                cid,tx = event.get('conversation_id'),event.get('turn_exchange_id')
                options = event.get('options') or []
                topic = next((x.get('topic_id') for x in options if isinstance(x,dict) and x.get('type')=='subscribe_ws_topic'),None)
                if not all(isinstance(x,str) and x for x in (cid,tx,topic)):
                    continue
                if ((self.expected_conversation and cid!=self.expected_conversation) or
                    (self.evidence.turn_exchange_id and tx!=self.evidence.turn_exchange_id)):
                    self.evidence.external_activity = True
                    continue
                self.evidence.conversation_id,self.evidence.turn_exchange_id,self.evidence.topic_id = cid,tx,topic
                self.evidence.last_server_progress_at = self.clock()
            elif kind == 'conversation_async_status':
                value = event.get('async_status')
                self.evidence.async_active = value in {1,3,5,6,7,'STREAMING','REALTIME','REALTIME_BUSY','REALTIME_BACKGROUND','DEPRECATED_PENDING'}
            elif kind in {'error','conversation_error'}:
                # Record only a classification. Error text can contain private data.
                self.error_types.add('product_error')
            message = event.get('message')
            if isinstance(message,dict):
                node = safe_node(message)
                if node:
                    self.nodes[node['message_id']] = node
            # Delta envelopes may contain lifecycle metadata without full messages.
            if isinstance(event.get('v'),dict):
                val = event['v']
                message = val.get('message')
                if isinstance(message,dict):
                    node = safe_node(message)
                    if node:
                        self.nodes[node['message_id']] = node
        self._update_graph()
        self._persist()
        self.on_change()

    def _update_graph(self):
        ev = self.evidence
        # A matching native request plus a message in its exact conversation can
        # establish identity even when Chromium missed the tiny SSE bootstrap.
        if not ev.turn_exchange_id and ev.request_seen:
            user = self.nodes.get(ev.user_message_id or '')
            candidate = user.get('turn_exchange_id') if user else None
            if candidate:
                ev.turn_exchange_id = candidate
        relevant = [n for n in self.nodes.values() if ev.turn_exchange_id and n.get('turn_exchange_id')==ev.turn_exchange_id]
        if not relevant:
            return
        ev.graph_seen = True
        signature = digest(json.dumps([(n['message_id'],n.get('status'),n.get('end_turn'),n.get('visible_text'),n.get('reasoning_status')) for n in sorted(relevant,key=lambda n:n['message_id'])],ensure_ascii=False))
        if signature != self.last_graph_signature:
            self.last_graph_signature = signature
            ev.last_server_progress_at = self.clock()
        finals = [n for n in relevant if trusted_final(n,ev.turn_exchange_id)]
        ev.graph_active = any(n.get('status') in {'in_progress','streaming','waiting','processing'} for n in relevant)
        if finals:
            final = max(finals,key=lambda n:n.get('create_time') or 0)
            ev.trusted_final_at = ev.trusted_final_at or self.clock()
            ev.final_message_id = final['message_id']
            # Exact-turn final metadata supersedes older async-status and graph
            # snapshots from that same turn. A visible Stop still blocks recovery.
            ev.graph_active = False
            ev.async_active = False
        elif not ev.dom_stop and any(n.get('status') in {'failed','error'} for n in relevant):
            ev.explicit_terminal = 'failed'
        elif not ev.dom_stop and any(n.get('status') in {'cancelled','canceled'} for n in relevant):
            ev.explicit_terminal = 'cancelled'

    def _persist(self):
        if not self.agent_id:
            return
        try:
            a = self.store.get(self.agent_id)
            ev = self.evidence
            if ev.external_activity and a['state'] not in TERMINAL_AGENT_STATES|{'EXTERNAL_ACTIVITY'}:
                self.store.set_state(self.agent_id,'EXTERNAL_ACTIVITY')
                return
            if not ev.conversation_id or not ev.turn_exchange_id:
                return
            if not a['conversation_id']:
                native = self.requests[-1] if self.requests else {}
                page_cid = self.snapshot.get('conversation_id')
                # The actual native POST may omit gizmo_id. Require BOTH the
                # exact project URL and SPA project membership in that case.
                project_match = (native.get('gizmo_id')==a['project_id'] or
                    (self.snapshot.get('project_id')==a['project_id'] and
                     re.match(r'^/g/'+re.escape(a['project_id'])+r'(?:-|/|$)',urlsplit(self.page.url).path) is not None))
                if not (ev.request_seen and self.intent_request_id and project_match and page_cid==ev.conversation_id):
                    return
                self.store.bind_conversation(self.agent_id,ev.conversation_id,self.page.url,a['project_id'])
            if self.store.get(self.agent_id)['conversation_id'] != ev.conversation_id:
                raise AgentError('OBSERVER_CONVERSATION_MISMATCH','Observation cannot change the bound conversation')
            turn = self.store.record_turn(self.agent_id,turn_exchange_id=ev.turn_exchange_id,
                user_message_id=ev.user_message_id,topic_id=ev.topic_id,model_label=self.requests[-1].get('model') if self.requests else None)
            state = ev.public(self.clock(),self.store.config.risk_window_minutes)
            terminal = state['reason'] if state['state'] in {'IDLE','DEAD_RECOVERABLE'} and state['reason'] in {'trusted_final','failed','cancelled'} else None
            signature = (turn['turn_epoch'],terminal,ev.last_server_progress_at,ev.exact_ws_seen,ev.sse_seen,ev.dom_stop)
            # Archive visible public messages before persisting a terminal turn.
            # A terminal handoff can then salvage the Child's last visible final
            # even when it never got another tool boundary to checkpoint itself.
            if self.last_archive_signature != self.last_graph_signature:
                for node in self.nodes.values():
                    if node.get('turn_exchange_id') not in {None,ev.turn_exchange_id}:
                        # Old visible nodes can be indexed with their known epoch.
                        with self.store.connection() as connection:
                            old = connection.execute('SELECT turn_epoch FROM turns WHERE agent_id=? AND turn_exchange_id=?',
                                (self.agent_id,node['turn_exchange_id'])).fetchone()
                        if old is None:
                            continue
                        epoch = old[0]
                    else:
                        epoch = turn['turn_epoch']
                    self.store.archive_message(self.agent_id,{**node,'turn_epoch':epoch,'source':'web_graph'})
                self.last_archive_signature = self.last_graph_signature
            if signature != self.last_store_signature:
                self.store.observe_turn(self.agent_id,turn['turn_epoch'],termination_class=terminal,
                    progress=bool(ev.last_server_progress_at),evidence=state['evidence'])
                self.last_store_signature = signature
        except Exception as exc:
            self.error_types.add('persistence:'+getattr(exc,'code',type(exc).__name__))

    async def refresh(self) -> dict:
        if self.stopped or self.page.is_closed():
            self.evidence.disconnected = True
            return self.public()
        try:
            snapshot = await asyncio.wait_for(self.page.evaluate(GRAPH_EXPRESSION),timeout=4)
            if not isinstance(snapshot,dict):
                raise ValueError('Unexpected graph snapshot')
            cid = snapshot.get('conversation_id')
            if self.expected_conversation and cid != self.expected_conversation:
                self.evidence.external_activity = True
                self._persist()
                return self.public()
            if cid and not self.evidence.conversation_id:
                self.evidence.conversation_id = cid
            nodes = snapshot.pop('nodes',[])
            for node in nodes:
                if isinstance(node,dict) and node.get('message_id'):
                    if not node.get('hidden'):
                        node['public_content_sha256']=digest(node.get('visible_text') or '')
                    node['visible_text'] = public_text(node.get('visible_text') or '')
                    self.nodes[node['message_id']] = node
            self.snapshot = snapshot
            self.evidence.dom_stop = bool(snapshot.get('stop_visible'))
            self.evidence.disconnected = False
            self._update_graph()
            self._persist()
            self.on_change()
        except Exception as exc:
            self.error_types.add('refresh:'+type(exc).__name__)
            self.evidence.disconnected = True
        return self.public()

    def public(self) -> dict:
        return {**self.evidence.public(self.clock(),self.store.config.risk_window_minutes),
            'tail_message_id':self.snapshot.get('tail_message_id'),'composer_nonempty':bool(self.snapshot.get('composer_nonempty')),
            'generation_post_count':self.generation_count,'exact_ws_frame_count':self.ws_frame_count,
            'raw_ws_frame_count':self.raw_ws_count,'event_types':sorted(self.event_types),
            'observer_errors':sorted(self.error_types),'stream_resource_enabled':bool(self.stream_enabled),
            'project_id':self.snapshot.get('project_id'),'retry_visible':bool(self.snapshot.get('retry_visible')),
            'public_message_count':sum(not n.get('hidden') for n in self.nodes.values())}

    async def stop(self):
        self.stopped = True
        self.page.remove_listener('close',self._closed)
        if self.cdp:
            try:
                await self.cdp.detach()
            except Exception:
                pass
        for task in list(self.pending_tasks):
            task.cancel()
        if self.pending_tasks:
            await asyncio.gather(*self.pending_tasks,return_exceptions=True)
        self.pending_tasks.clear()
