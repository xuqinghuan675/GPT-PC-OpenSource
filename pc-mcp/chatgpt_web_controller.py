"""Native SPA controller behind the single chatgpt_agent facade.

All writes are native browser input, never private backend mutation requests.
UI details and lifecycle observers stay here; agents call semantic operations.
"""
from __future__ import annotations
import asyncio
from dataclasses import asdict
import json
from pathlib import Path
import re
import time
from urllib.parse import urlsplit
import uuid
from chatgpt_agent_store import AgentError, TERMINAL_AGENT_STATES, digest, public_text
from chatgpt_web_observer import PageObserver, GRAPH_EXPRESSION
from machine_config import requested_model_policy, join_path


EFFORT_LABELS = {'extra_high':('极高','Extra high','Extra High','Extended'),'high':('高','High'),'medium':('中','Medium','Standard')}
# Native composer may prefix the selected model generation/name before the
# effort label (for example ``6\nPro``).  The controller only needs the final
# effort token here; the opened menu still provides the five-level slider and
# explicit model radio state used for the safety readback below.
EFFORT_NAME = re.compile(r'^(?:[^\n]+\s+)?(低|中|高|极高|Low|Medium|High|Extra high|Extra High|Standard|Extended|Pro)$',re.I)


def project_identity(url:str) -> str:
    p=urlsplit(url)
    if p.scheme!='https' or p.hostname!='chatgpt.com' or p.query or p.fragment or p.username:
        raise AgentError('PROJECT_URL_INVALID','Project URL must be a plain chatgpt.com URL')
    match=re.match(r'^/g/(g-p-[a-f0-9]{32})(?:-|/|$)',p.path)
    if not match:
        raise AgentError('PROJECT_URL_INVALID','URL is not a ChatGPT Project')
    return match.group(1)


def browser_login_required(url:str) -> bool:
    """Recognize native ChatGPT auth redirects before UI waits can time out."""
    p=urlsplit(url)
    return p.scheme=='https' and p.hostname=='chatgpt.com' and (p.path=='/auth' or p.path.startswith('/auth/'))


async def native_activate(locator,*,timeout=4000):
    """Keyboard activation works on background tabs without screenshot guessing.

    It deliberately performs one native action only. Never retry an uncertain
    submit because an animation or network event did not arrive in time.
    """
    if await locator.count()!=1 or not await locator.is_visible() or not await locator.is_enabled():
        raise AgentError('CONTROL_NOT_READY','Native control is absent, ambiguous, hidden or disabled')
    await locator.press('Enter',timeout=timeout)


def composer_text_equivalent(actual: str, expected: str) -> bool:
    """Compare authorized text while ignoring contenteditable-only whitespace layout."""
    def canonical(value: str) -> str:
        value=re.sub(r'[\u200b\u2060\ufeff]', '', value or '')
        return re.sub(r'\s+', ' ', value).strip()
    return canonical(actual)==canonical(expected)


async def wait_unique_effort_control(page, *, timeout: float = 12.0, poll_seconds: float = 0.15):
    """Wait for the native model/effort button to finish hydrating.

    A fresh Project composer can become editable several seconds before the
    model/intelligence control is mounted. A PREPARED spawn has not submitted
    anything yet, so bounded read-only waiting is safer than failing instantly
    and making callers invent a model-policy downgrade or replacement Child.
    """
    candidates=page.get_by_role('button',name=EFFORT_NAME)
    deadline=asyncio.get_running_loop().time()+max(0.0,timeout)
    last_count=await candidates.count()
    while last_count!=1 and asyncio.get_running_loop().time()<deadline:
        if await page.locator('button[data-testid="stop-button"]:visible').count():
            raise AgentError('MODEL_CHANGE_WHILE_RUNNING','Do not change the model of an active generation')
        await asyncio.sleep(max(0.0,poll_seconds))
        last_count=await candidates.count()
    if last_count!=1:
        raise AgentError(
            'MODEL_CONTROLS_UNRECOGNIZED',
            'Native model/intelligence control did not hydrate into one verified non-Pro selector before timeout',
            observed_control_count=last_count,
            safe_exact_retry=True,
        )
    return candidates


async def ensure_non_pro_effort(page,policy:dict) -> dict:
    requested=policy['requested_reasoning_effort']
    old_menu=page.locator('[role="menu"][data-state="open"]:visible').filter(has=page.locator('[data-testid="composer-intelligence-picker-content"]'))
    if await old_menu.count():
        await page.keyboard.press('Escape')
        await old_menu.wait_for(state='hidden',timeout=2000)
    if policy.get('allow_pro'):
        # User-authorized Pro will require an explicit, auditable UI adapter;
        # it is never selected implicitly by this non-Pro path.
        raise AgentError('PRO_ADAPTER_NOT_ENABLED','This controller currently supports the non-Pro policy only')
    stop=page.locator('button[data-testid="stop-button"]:visible')
    if await stop.count():
        raise AgentError('MODEL_CHANGE_WHILE_RUNNING','Do not change the model of an active generation')
    chat=page.get_by_role('radio',name=re.compile(r'^(聊天|Chat)$'))
    if await chat.count()==1 and await chat.get_attribute('aria-checked')!='true':
        await chat.press('Space',timeout=3000)
        if await chat.get_attribute('aria-checked')!='true':
            raise AgentError('CHAT_MODE_NOT_VERIFIED','Chat mode selection did not persist')
    candidates=await wait_unique_effort_control(page)
    await native_activate(candidates)
    menu=page.locator('[role="menu"][data-state="open"]:visible')
    await menu.wait_for(state='visible',timeout=3000)
    slider=menu.get_by_role('slider',name=re.compile(r'推理|Reasoning',re.I))
    effective=None
    fallback=None
    model_label='non-pro (native UI)'
    modern=menu.locator('[data-testid="composer-intelligence-picker-content"] [data-model-reasoning-effort-slider] [role="slider"]')
    if await modern.count()==1:
        # Current native composer uses an aria-hidden slider for numeric state
        # and a keyboard-accessible menuitem for input. Never click its label:
        # that opens model selection rather than changing reasoning effort.
        keyboard=menu.get_by_role('menuitem',name=re.compile(r'^(能力|Intelligence|Reasoning effort)$',re.I))
        minimum=int(await modern.get_attribute('aria-valuemin') or '-1')
        maximum=int(await modern.get_attribute('aria-valuemax') or '-1')
        value=int(await modern.get_attribute('aria-valuenow') or '-1')
        if minimum!=0 or maximum!=4 or await keyboard.count()!=1 or not await keyboard.is_enabled():
            await page.keyboard.press('Escape')
            raise AgentError('EFFORT_LAYOUT_UNRECOGNIZED','Native five-level keyboard control is not in a verified state')
        target={'medium':1,'high':2,'extra_high':3}[requested]
        while value!=target:
            old=value
            await keyboard.press('ArrowRight' if value<target else 'ArrowLeft',timeout=2000)
            deadline=asyncio.get_running_loop().time()+1
            while asyncio.get_running_loop().time()<deadline:
                value=int(await modern.get_attribute('aria-valuenow') or '-1')
                if value!=old:break
                await asyncio.sleep(0.05)
            if value==old or value>3 or value<0:
                await page.keyboard.press('Escape')
                raise AgentError('EFFORT_READBACK_FAILED','Native keyboard control did not move one non-Pro step')
        description=await keyboard.get_attribute('aria-describedby') or ''
        announcement=' '.join([await page.locator('[id="'+identifier+'"]').inner_text() for identifier in description.split()])
        if not any(label.lower() in announcement.lower() for label in EFFORT_LABELS[requested]):
            await page.keyboard.press('Escape')
            raise AgentError('EFFORT_READBACK_FAILED','The accessibility announcement did not confirm the requested effort')
        selected=menu.locator('[role="menuitemradio"][aria-checked="true"]')
        if await selected.count()==1:
            model_label=public_text(await selected.inner_text(),100)
        if re.search(r'\bpro\b',model_label,re.I):
            await page.keyboard.press('Escape')
            raise AgentError('PRO_MODEL_FORBIDDEN','Current selected model is Pro')
        effective=requested
        await page.keyboard.press('Escape')
        await menu.wait_for(state='hidden',timeout=2000)
    elif await slider.count()==1:
        text=await menu.inner_text()
        current=await slider.get_attribute('aria-valuetext') or ''
        minimum=int(await slider.get_attribute('aria-valuemin') or '0')
        maximum=int(await slider.get_attribute('aria-valuemax') or '-1')
        # The current product exposes Low/Medium/High/Extra-high/Pro in order.
        # Do not use End/max: that would silently select Pro.
        levels=['低','中','高','极高','Pro'] if all(x in text for x in ['低','中','高','极高','Pro']) else ['Low','Medium','High','Extra high','Pro']
        if minimum!=0 or maximum!=4 or not all(x.lower() in text.lower() for x in levels):
            await menu.press('Escape',timeout=2000)
            raise AgentError('EFFORT_LAYOUT_UNRECOGNIZED','Reasoning slider layout changed; Pro cannot be safely excluded')
        target={'medium':1,'high':2,'extra_high':3}[requested]
        value=int(await slider.get_attribute('aria-valuenow') or '-1')
        if value!=target:
            await slider.press('Home',timeout=2000)
            for _ in range(target):
                await slider.press('ArrowRight',timeout=2000)
        current=await slider.get_attribute('aria-valuetext') or ''
        value=int(await slider.get_attribute('aria-valuenow') or '-1')
        if value!=target or current.lower() not in {s.lower() for s in EFFORT_LABELS[requested]}:
            await menu.press('Escape',timeout=2000)
            raise AgentError('EFFORT_READBACK_FAILED','Requested non-Pro reasoning effort did not persist')
        selected=menu.locator('[role="menuitemradio"][aria-checked="true"]')
        if await selected.count():
            model_label=public_text(await selected.first.inner_text(),100)
        if re.search(r'\bpro\b',model_label,re.I):
            await menu.press('Escape',timeout=2000)
            raise AgentError('PRO_MODEL_FORBIDDEN','The current selected model is Pro; nothing was submitted')
        effective=requested
        await menu.press('Escape',timeout=2000)
    else:
        # Older native effort menus may expose discrete choices. Prefer the
        # requested effort, otherwise report the highest available non-Pro one.
        for effort in [requested]+[x for x in ('extra_high','high','medium') if x!=requested]:
            for label in EFFORT_LABELS[effort]:
                choice=menu.get_by_text(label,exact=True)
                if await choice.count()==1 and await choice.is_visible():
                    await native_activate(choice)
                    effective=effort
                    fallback=None if effort==requested else 'requested_effort_unavailable_on_non_pro_model'
                    break
            if effective:
                break
        if not effective:
            await menu.press('Escape',timeout=2000)
            raise AgentError('NO_NON_PRO_EFFORT','No supported non-Pro reasoning option was available')
    buttons=page.get_by_role('button',name=EFFORT_NAME)
    if await buttons.count()!=1 or (await buttons.inner_text()).strip().lower() not in {x.lower() for x in EFFORT_LABELS[effective]}:
        raise AgentError('EFFORT_BUTTON_READBACK_FAILED','Closed native control does not reflect the verified reasoning effort')
    return {'requested_model':policy['requested_model'],'effective_model':model_label,'requested_reasoning_effort':requested,
            'effective_reasoning_effort':effective,'fallback_reason':fallback,'pro_excluded':True,'evidence':'native_control_readback'}


async def submit_native_once(page,observer:PageObserver,text:str,*,control_check,mark_submitting,
                             expected_conversation:str|None=None,expected_tail:str|None=None) -> dict:
    before=await observer.refresh()
    if expected_conversation and before.get('conversation_id')!=expected_conversation:
        raise AgentError('CONVERSATION_CHANGED','Native submission target changed')
    if expected_tail and before.get('tail_message_id')!=expected_tail:
        raise AgentError('EXTERNAL_ACTIVITY','The conversation tail changed before submission')
    if before.get('composer_nonempty') or observer.evidence.dom_stop:
        raise AgentError('USER_DRAFT_OR_GENERATION_ACTIVE','Do not overwrite a draft or submit into an active generation')
    editor=page.locator('#prompt-textarea')
    if await editor.count()!=1:
        raise AgentError('COMPOSER_NOT_FOUND','No unique native composer')
    control_check()
    await editor.fill(text,timeout=4000)
    # Native editor insertion is not a generation. Commit the irreversible intent
    # immediately before the single submit activation, after a second control check.
    control_check()
    after=await page.evaluate(GRAPH_EXPRESSION)
    if expected_conversation and after.get('conversation_id')!=expected_conversation:
        raise AgentError('CONVERSATION_CHANGED','Target changed after filling the composer; no submit was performed')
    if expected_tail and after.get('tail_message_id')!=expected_tail:
        raise AgentError('EXTERNAL_ACTIVITY','Conversation changed during preparation; no submit was performed')
    if not composer_text_equivalent(await editor.inner_text(),text):
        raise AgentError('COMPOSER_CHANGED','Composer no longer contains the exact authorized text')
    send=page.locator('button[data-testid="send-button"]')
    if await send.count()!=1:
        send=page.get_by_role('button',name=re.compile(r'^(发送提示|发送|Send prompt|Send)$',re.I))
    if await send.count()!=1 or not await send.is_enabled():
        raise AgentError('SEND_CONTROL_NOT_READY','Native send control is absent, ambiguous or disabled')
    # Request parent ids may include invisible tool nodes beyond the last visible
    # bubble. The pre-submit graph tail is checked above; request identity is
    # verified by exact text/conversation and recorded rather than guessed.
    observer.arm(text,conversation_id=expected_conversation)
    mark_submitting()
    await native_activate(send)
    # Healthy native submissions can expose the exact turn identity slightly
    # after the POST. Six seconds routinely forced an avoidable second
    # reconciliation roundtrip, so keep the durable fallback but cover the
    # normal handoff window in the first call.
    deadline=asyncio.get_running_loop().time()+9
    while asyncio.get_running_loop().time()<deadline:
        await observer.refresh()
        identity_ready=bool(observer.evidence.request_seen and observer.evidence.turn_exchange_id
                            and observer.evidence.conversation_id)
        if identity_ready:
            # For a fresh spawn, exact transport evidence is necessary but not
            # sufficient: wait until PageObserver has also durably bound that
            # same conversation into the Agent store. Existing-conversation
            # followups are already bound before submission.
            durable_ready=bool(expected_conversation)
            if not durable_ready and observer.agent_id:
                bound=observer.store.get(observer.agent_id)
                durable_ready=(bound.get('conversation_id')==observer.evidence.conversation_id)
            if durable_ready:
                break
        await asyncio.sleep(0.2)
    return observer.public()


class ChatGPTWebController:
    def __init__(self,manager,session,loop,service_caller=None):
        self.manager,self.store,self.config=manager,manager.store,manager.config
        self.session,self.loop=session,loop
        self.observers={}
        self.monitor_task=None
        self.supervisor_task=None
        self.background_tasks=set()
        self.service_caller=service_caller
        self.last_supervisor_tick=0.0
        self.pending_terminal_participant_cleanup=set()
        self.terminal_participant_bootstrap_reconciled=False
        self.closed=False

    def dispatch(self,action,caller,parameters):
        return self.loop.call(self._dispatch(action,caller,parameters),timeout=70)

    def start_background(self):
        """Start the deterministic monitor on Runtime startup without launching Chrome."""
        return self.loop.call(self._ensure_monitor(),timeout=5)

    def stop_background(self):
        try:
            return self.loop.call(self._shutdown_background(),timeout=5)
        finally:
            self.loop.stop()

    async def _shutdown_background(self):
        self.closed=True
        task=self.monitor_task
        if task and not task.done():
            task.cancel()
            await asyncio.gather(task,return_exceptions=True)
        # Stop producers before disposing observers they may still be using.
        pending=list(self.background_tasks)
        for task in pending:
            task.cancel()
        if pending:
            await asyncio.gather(*pending,return_exceptions=True)
        self.background_tasks.clear()
        for observer in list(self.observers.values()):
            try:
                await observer.stop()
            except Exception:
                pass
        self.observers.clear()
        try:
            await self.session.disconnect()
        except Exception:
            pass
        return {'status':'stopped'}

    async def _ensure_monitor(self):
        if self.monitor_task is None or self.monitor_task.done():
            self.monitor_task=asyncio.create_task(self._monitor())
        return {'status':'running'}

    async def _drop_observer(self,agent_id,*,close_page=False):
        """Stop one passive observer and optionally release its managed tab.

        Observer cleanup is deliberately bounded. A wedged Chrome tab must not
        be able to wedge the lifecycle monitor while it is being retired.
        """
        observer=self.observers.pop(agent_id,None)
        if observer is None:
            return False
        page=getattr(observer,'page',None)
        try:
            await asyncio.wait_for(observer.stop(),timeout=2)
        except Exception:
            pass
        if close_page and page is not None and not page.is_closed():
            try:
                await asyncio.wait_for(page.close(),timeout=2)
            except Exception:
                pass
        self.store.delete_setting('agent_tab:'+agent_id)
        return True

    async def _prune_agent_tab_settings(self):
        """Remove stale controller-owned target mappings without touching live Agents."""
        if self.session is None or self.session.context is None or self.session.disconnected:
            # Startup/disconnection is not evidence that the managed tabs died.
            # These mappings are the only safe way to reattach after restart.
            return []
        mappings=self.store.settings_prefix('agent_tab:')
        if not mappings:
            return []
        removed=[]
        pages_by_target={}
        for page in self.session.chatgpt_pages() if self.session is not None else []:
            try:
                pages_by_target[await self._target_id(page)]=page
            except Exception:
                # An incomplete CDP inventory cannot justify deleting ownership.
                return []
        for key,target in mappings.items():
            if not target:
                self.store.delete_setting(key)
                removed.append(key)
                continue
            aid=key.split(':',1)[1]
            page=pages_by_target.get(str(target))
            try:
                agent=self.store.get(aid)
            except AgentError:
                agent=None
            if page is None:
                self.store.delete_setting(key)
                removed.append(key)
                continue
            if agent is None or agent.get('state') in TERMINAL_AGENT_STATES:
                try:
                    await asyncio.wait_for(page.close(),timeout=2)
                except Exception:
                    pass
                self.store.delete_setting(key)
                removed.append(key)
        return removed

    def _cleanup_forced_terminal_participant(self, agent):
        """Release Runtime identity only for forced non-resumable terminals.

        DONE deliberately stays on complete -> report_progress(done), while
        recoverable failed/cancelled turns retain their participant identity.
        """
        if not agent or agent.get('state') not in {'CLOSED','BLOCKED'}:
            return None
        releaser=getattr(self.manager,'terminal_participant_releaser',None)
        if releaser is None:
            return {'status':'component_unavailable'}
        try:
            result=releaser(str(agent['logical_agent_id']))
            self.pending_terminal_participant_cleanup.discard(str(agent['logical_agent_id']))
            return result
        except Exception as exc:
            error_type=str(getattr(exc,'error_type',type(exc).__name__))
            if error_type=='PARTICIPANT_JOBS_ACTIVE':
                self.pending_terminal_participant_cleanup.add(str(agent['logical_agent_id']))
            return {'status':'deferred','error_type':error_type}

    def _reconcile_terminal_participants(self):
        """Repair legacy orphan participants once, then retry job-blocked cleanup."""
        if getattr(self.manager,'terminal_participant_releaser',None) is None:
            return []
        results=[]
        if not self.terminal_participant_bootstrap_reconciled:
            for state in ('CLOSED','BLOCKED'):
                offset=0
                while True:
                    rows=self.store.agents(state=state,limit=200,offset=offset)
                    if not rows:
                        break
                    for agent in rows:
                        result=self._cleanup_forced_terminal_participant(agent)
                        if result:
                            results.append(result)
                    offset += len(rows)
            self.terminal_participant_bootstrap_reconciled=True
        for aid in list(self.pending_terminal_participant_cleanup):
            try:
                agent=self.store.get(aid)
            except AgentError:
                self.pending_terminal_participant_cleanup.discard(aid)
                continue
            if agent.get('state') not in {'CLOSED','BLOCKED'}:
                self.pending_terminal_participant_cleanup.discard(aid)
                continue
            result=self._cleanup_forced_terminal_participant(agent)
            if result:
                results.append(result)
        return results

    async def _prune_observers(self):
        """Drop observers that can no longer represent live managed work.

        In particular, a Watchdog whose scope/generation is already closed is
        not allowed to keep running the expensive React graph snapshot every
        two seconds. This also repairs legacy rows where the Watchdog lifecycle
        was closed but the logical Agent was accidentally left RUNNING.
        """
        removed=[]
        for aid,observer in list(self.observers.items()):
            page=getattr(observer,'page',None)
            # The monitor also accepts lightweight passive observers used by
            # diagnostics/tests. Only managed page observers participate in
            # browser-tab lifecycle pruning.
            if page is None:
                continue
            close_page=False
            stale=page.is_closed()
            if not stale:
                try:
                    agent=self.store.get(aid)
                except AgentError:
                    agent=None
                    stale=True
                if agent:
                    if agent.get('state') in TERMINAL_AGENT_STATES:
                        self._cleanup_forced_terminal_participant(agent)
                        stale=True
                        close_page=True
                    elif agent.get('kind')=='watchdog':
                        scope_id=str(agent.get('owner_thread_id') or '')
                        rows=self.store.watchdog_rows(owner_thread_id=scope_id) if scope_id else []
                        # Watchdog generation belongs to the watchdogs lifecycle
                        # row, not the agents row. Resolve by the durable
                        # logical-agent binding so a freshly spawned Watchdog is
                        # not falsely treated as orphaned before it can join.
                        row=next((r for r in rows if r.get('logical_agent_id')==aid),None)
                        scope=self.store.watchdog_scope(scope_id) if scope_id else {'enabled':False}
                        if (not scope.get('enabled') or row is None
                                or row.get('state') not in {'ALLOCATED','ACTIVE','RETIRING'}):
                            closed=self.store.set_state(aid,'CLOSED')
                            self._cleanup_forced_terminal_participant(closed)
                            stale=True
                            close_page=True
            if stale:
                if await self._drop_observer(aid,close_page=close_page):
                    removed.append(aid)
        return removed

    async def _dispatch(self,action,caller,p):
        if action=='watchdog_status':
            scope_id=self.manager.supervisor_scope_for_caller(caller)
            state=self.store.watchdog_scope(scope_id)
            watchdogs,schedule=self._watchdog_snapshot(scope_id)
            inherited=self.manager.caller_is_child_agent(caller)
            return {'status':'ok','mode':state.get('mode') or 'conversation','scope':state,
                    'scope_thread_id':scope_id,'inherited':inherited,'can_toggle':not inherited,
                    'agent_count':len(self.store.agents_in_supervisor_scope(scope_id)),
                    'watchdogs':watchdogs,'schedule':schedule}
        if action=='watchdog_stop':
            await self._ensure_monitor()
            return await self.watchdog_stop(caller)
        if action=='watchdog_start':
            await self._ensure_monitor()
            return await self.watchdog_start(caller,p)
        await self.session.ensure(caller,launch=action in {'ensure_project','spawn','attach','watchdog_start'})
        await self._ensure_monitor()
        if action=='ensure_project':
            return await self.ensure_project(caller)
        if action=='spawn':
            return await self.spawn(caller,p)
        if action=='join':
            return await self.join(caller,p)
        if action=='attach':
            return await self.attach(caller,p)
        if action in {'followup','wake','interrupt','close'}:
            target=self.store.get(p['agent_ref'])
            self.manager._authorize(caller,target,action)
            return await self.agent_action(action,caller,target,p)
        raise AgentError('ACTION_COMPONENT_PENDING','This action is not installed in the current controller yet')

    async def _monitor(self):
        try:
            while not self.closed:
                try:
                    await self._restore_managed_observers()
                    self._reconcile_terminal_participants()
                    await self._prune_observers()
                    await self._prune_agent_tab_settings()
                    observers=list(self.observers.values())
                    if observers:
                        await asyncio.gather(*(x.refresh() for x in observers),return_exceptions=True)
                    scopes=self.store.watchdog_scopes(enabled_only=True)
                    if (scopes and self.service_caller and time.monotonic()-self.last_supervisor_tick>=5
                            and (self.supervisor_task is None or self.supervisor_task.done())):
                        self.last_supervisor_tick=time.monotonic()
                        self.supervisor_task=self._background(self._run_supervisor_tick(),'watchdog_lifecycle')
                except Exception as exc:
                    # A transient SQLite/file/CDP failure must not permanently
                    # kill the only lifecycle monitor for every Agent tree.
                    try:
                        self.store.set_setting('lifecycle_monitor_error',
                            {'class':getattr(exc,'code',type(exc).__name__),'at':time.time()})
                    except Exception:
                        pass
                await asyncio.sleep(2)
        except asyncio.CancelledError:
            pass

    async def _restore_managed_observers(self):
        """Reattach only persisted managed targets; never launch or submit here."""
        if self.session is None:
            return
        missing=[]
        for key in self.store.settings_prefix('agent_tab:'):
            aid=key.split(':',1)[1]
            if aid in self.observers:
                continue
            try:
                agent=self.store.get(aid)
            except AgentError:
                continue
            if agent.get('state') not in TERMINAL_AGENT_STATES:
                missing.append(agent)
        if not missing:
            return
        try:
            await self.session.ensure(None,launch=False)
        except AgentError:
            return
        for agent in missing:
            try:
                await self._observer(agent,reopen=False)
            except AgentError:
                continue

    async def _run_supervisor_tick(self):
        try:
            caller=await asyncio.to_thread(self.service_caller)
            await self._supervisor_tick(caller)
            self.store.delete_setting('watchdog_supervisor_error')
        except Exception as exc:
            self.store.set_setting('watchdog_supervisor_error',
                {'class':getattr(exc,'code',type(exc).__name__),'at':time.time()})

    async def _target_id(self,page):
        cdp=await page.context.new_cdp_session(page)
        try:
            return (await cdp.send('Target.getTargetInfo'))['targetInfo']['targetId']
        finally:
            await cdp.detach()

    def _background(self,coroutine,label):
        started=time.time()
        error_key='background_error:'+label
        task=asyncio.create_task(coroutine)
        self.background_tasks.add(task)
        def finished(done):
            self.background_tasks.discard(done)
            if done.cancelled():
                return
            try:
                done.result()
                current=self.store.setting(error_key)
                if isinstance(current,dict) and float(current.get('at') or 0)<=started:
                    self.store.delete_setting(error_key)
            except Exception as exc:
                # Performance-only background work must never weaken the
                # synchronous safety path. Persist just the public error class.
                self.store.set_setting(error_key,
                    {'class':getattr(exc,'code',type(exc).__name__),'at':time.time()})
        task.add_done_callback(finished)
        return task

    async def _page_by_target(self,target):
        if not target or not self.session.context:
            return None
        for page in self.session.chatgpt_pages():
            if await self._target_id(page)==target:
                return page
        return None

    async def _owned_page(self,caller,key,url):
        page=await self._page_by_target(self.store.setting(key))
        if page is not None:
            return page
        page=await self.session.new_background_page(caller,url)
        self.store.set_setting(key,await self._target_id(page))
        return page

    async def _fresh_spawn_page_preserving_existing(self,caller,project,aid):
        """Detach a polluted Child composer without clearing or closing its draft."""
        old_observer=self.observers.pop(aid,None)
        if old_observer is not None:
            await old_observer.stop()
        # Keep the old tab/draft untouched. Only controller ownership moves.
        self.store.delete_setting('agent_tab:'+aid)
        page=await self.session.new_background_page(caller,project['url'])
        try:
            await page.locator('#prompt-textarea').wait_for(state='visible',timeout=25000)
            if project_identity(page.url)!=project['project_id'] or '/c/' in urlsplit(page.url).path:
                raise AgentError('SPAWN_FRESH_TARGET_CHANGED','Fresh spawn tab did not remain on the intended project home')
            snapshot=await page.evaluate(GRAPH_EXPRESSION)
            if not isinstance(snapshot,dict) or snapshot.get('composer_nonempty'):
                raise AgentError('SPAWN_FRESH_COMPOSER_NOT_EMPTY','Fresh project composer is not empty; refusing to clear it')
            self.store.set_setting('agent_tab:'+aid,await self._target_id(page))
            observer=await PageObserver(page,self.store,aid,on_change=self.manager._notify).start()
            self.observers[aid]=observer
            return page,observer
        except BaseException:
            if not page.is_closed():
                await page.close()
            raise

    async def _watchdog_spawn_page(self,caller,project,aid):
        """Give Watchdog its own tab; never consume the shared warm composer."""
        return await self._owned_page(caller,'agent_tab:'+aid,project['url'])

    async def _claim_management_project_page(self,caller,project,aid):
        """Transfer the already verified warm project composer to one Child."""
        async with self.session.project_lock:
            return await self._claim_management_project_page_locked(caller,project,aid)

    async def _claim_management_project_page_locked(self,caller,project,aid):
        page=await self._page_by_target(self.store.setting('management_target_id'))
        if page is None or page.url!=project['url'] or '/c/' in urlsplit(page.url).path:
            return None
        # The management tab is only a performance warm slot. If a user or
        # another actor has typed into its composer, the controller must not
        # transfer that draft into a Child-owned tab (and must never clear it).
        # A read-only graph snapshot is the same native composer evidence used
        # by PageObserver. Fail closed: an unreadable/non-empty warm composer
        # is simply skipped, so spawn can use its own clean background page.
        try:
            snapshot=await page.evaluate(GRAPH_EXPRESSION)
        except Exception:
            return None
        if not isinstance(snapshot,dict) or snapshot.get('composer_nonempty'):
            return None
        await self.session.assert_owner(caller)
        target_id=await self._target_id(page)
        self.store.set_setting('management_target_id',None)
        self.store.set_setting('agent_tab:'+aid,target_id)
        return page

    async def _prewarm_management_project_page(self,caller,project):
        """Prepare one exact project-home composer off the next spawn's path."""
        async with self.session.project_lock:
            existing=await self._page_by_target(self.store.setting('management_target_id'))
            if existing is not None and existing.url==project['url'] and '/c/' not in urlsplit(existing.url).path:
                return {'status':'already_warm'}
            page=await self.session.new_background_page(caller,project['url'])
            try:
                await page.locator('#prompt-textarea').wait_for(state='visible',timeout=15000)
                if (page.url!=project['url'] or project_identity(page.url)!=project['project_id']
                        or '/c/' in urlsplit(page.url).path):
                    raise AgentError('PROJECT_PREWARM_TARGET_CHANGED','Prewarmed tab did not remain on the exact project home')
                self.store.set_setting('management_target_id',await self._target_id(page))
                return {'status':'warmed'}
            except BaseException:
                if not page.is_closed():
                    await page.close()
                raise

    async def ensure_project(self,caller):
        async with self.session.project_lock:
            owner=uuid.uuid4().hex
            if not self.store.service_claim('project-ensure',owner,60):
                raise AgentError('PROJECT_ENSURE_BUSY','Another coordinator is reconciling the project')
            try:
                cached=self.store.setting('project')
                known_url=cached.get('url') if cached else None
                page=await self._owned_page(caller,'management_target_id',known_url or 'https://chatgpt.com/')
                if known_url and page.url!=known_url:
                    await self.session.assert_owner(caller)
                    await page.goto(known_url,wait_until='domcontentloaded',timeout=15000)
                if browser_login_required(page.url):
                    await self.session.show_for_user(caller)
                    raise AgentError('BROWSER_LOGIN_REQUIRED','Sign in once to the persistent dedicated profile; the dedicated Chrome window was shown without stealing focus and credentials are never copied')
                if not known_url:
                    await page.locator('nav').first.wait_for(state='visible',timeout=15000)
                    for _ in range(5):
                        row=page.locator('[role="button"][data-sidebar-item="true"]').filter(has=page.get_by_text(self.config.project_name,exact=True))
                        if await row.count():break
                        more=page.get_by_role('button',name=re.compile(r'^(查看更多|See more|Show more)$',re.I))
                        if await more.count()!=1:break
                        await native_activate(more)
                    count=await row.count()
                    if count>1:
                        raise AgentError('PROJECT_AMBIGUOUS','Several projects have this name; no arbitrary project was selected')
                    if count==1:
                        await self.session.assert_owner(caller)
                        # The project row contains a continuously moving marquee; Playwright hover
                        # waits for geometric stability and can time out even though the native
                        # trailing button is valid. Keyboard focus reveals that button without
                        # screenshot coordinates, DOM mutation, or a synthetic click.
                        await row.press('Tab',timeout=3000)
                        home=row.locator('..').get_by_role('button',name=re.compile(r'^(打开项目首页|Open project)$',re.I))
                        if await home.count()!=1:
                            raise AgentError('PROJECT_HOME_CONTROL_MISSING','Native project home control changed')
                        await native_activate(home)
                        await page.wait_for_url(re.compile(r'/g/g-p-'),timeout=10000,wait_until='domcontentloaded')
                    else:
                        await self._create_project(page,caller)
                pid=project_identity(page.url)
                await page.locator('main').get_by_text(self.config.project_name,exact=True).first.wait_for(state='visible',timeout=15000)
                if cached and (cached['project_id']!=pid or cached['name']!=self.config.project_name):
                    raise AgentError('PROJECT_ID_CHANGED','An established project binding cannot silently change')
                url=page.url.split('/c/')[0]
                if not url.endswith('/project'):
                    raise AgentError('PROJECT_HOME_NOT_VERIFIED','Expected the native project home, not a similarly named chat')
                result={'name':self.config.project_name,'project_id':pid,'url':url,'verified_at':self.store.clock(),
                        'machine_slug':self.config.machine_slug,'evidence':'native_project_home_and_exact_url'}
                self.store.set_setting('project',result)
                return {'status':'project_ready','project':result,'browser_reused':True}
            finally:
                self.store.service_release('project-ensure',owner)

    async def _create_project(self,page,caller):
        previous=self.store.setting('project_create_intent')
        if previous and previous.get('state')=='SUBMITTING':
            raise AgentError('PROJECT_CREATE_RECONCILE_REQUIRED','Previous project creation is uncertain; inspect the native list before another create')
        button=page.get_by_role('button',name=re.compile(r'^(新项目|新建项目|New project)$',re.I))
        if await button.count()!=1:
            if await page.locator('input[type="email"]:visible').count():
                raise AgentError('BROWSER_LOGIN_REQUIRED','Sign in once to the persistent dedicated profile; credentials are never copied')
            raise AgentError('PROJECT_CREATE_CONTROL_MISSING','No native new-project control; verify login and account capability')
        await self.session.assert_owner(caller)
        await native_activate(button)
        dialog=page.get_by_role('dialog')
        await dialog.wait_for(state='visible',timeout=5000)
        name=dialog.get_by_role('textbox')
        if await name.count()!=1:
            raise AgentError('PROJECT_NAME_CONTROL_AMBIGUOUS','Project name field is not unique')
        await name.fill(self.config.project_name)
        submit=dialog.get_by_role('button',name=re.compile(r'^(创建项目|Create project|创建|Create)$',re.I))
        await self.session.assert_owner(caller)
        self.store.set_setting('project_create_intent',{'state':'SUBMITTING','name':self.config.project_name,'at':self.store.clock()})
        await native_activate(submit)
        await page.wait_for_url(re.compile(r'/g/g-p-'),timeout=15000,wait_until='domcontentloaded')
        self.store.set_setting('project_create_intent',{'state':'OBSERVED','name':self.config.project_name,'project_id':project_identity(page.url),'at':self.store.clock()})

    async def _observer(self,agent,caller=None,*,reopen=False):
        aid=agent['logical_agent_id']
        observer=self.observers.get(aid)
        if observer and not observer.page.is_closed():
            return observer
        # Automatic lifecycle operations only use a page that this controller
        # explicitly owns. Never adopt an arbitrary user-opened tab merely
        # because it happens to show the same Conversation.
        page=await self._page_by_target(self.store.setting('agent_tab:'+aid))
        if page is None and reopen and agent.get('conversation_url') and caller:
            page=await self.session.new_background_page(caller,agent['conversation_url'])
            self.store.set_setting('agent_tab:'+aid,await self._target_id(page))
        if page is None:
            raise AgentError('AGENT_TAB_DETACHED','Managed tab is absent; reopen the same conversation instead of spawning a replacement')
        observer=await PageObserver(page,self.store,aid,on_change=self.manager._notify).start()
        self.observers[aid]=observer
        return observer

    def _bootstrap(self,agent,ticket,instructions,delegation_reason='',shared_lease_id=None,participant_workspace_lease_id=None):
        workspace_kind='Git 仓库' if (Path(agent['workspace'])/'.git').exists() else '普通非 Git 目录'
        if participant_workspace_lease_id:
            lease_bootstrap=(
                '本 Child 与父 Agent 共用同一个 workspace 资源租约；Controller 已为本 Conversation 预分配 participant，禁止 acquire_workspace，也不要自行调用 join。'
                f'participant_workspace_lease_id="{participant_workspace_lease_id}"。'
                f'首轮并行读取 {join_path(self.config.runtime_root,"AGENTS.md")} 与 runtime_status 时，以及之后所有本机工具、checkpoint、resume_context、complete 和 report_progress，'
                '都必须直接使用这个 participant_workspace_lease_id。Controller 会在原生 Conversation/project/user-message 证明成立后，用内部 single-use ticket 完成身份绑定；'
                'participant 只提供本 Conversation 独立 thread/control/checkpoint 身份，不是第二把 workspace 锁；Goal/Visual/Ultra/Stay 与父树共享。'
            )
            first_turn='只有真实 liveness probe 才先 report_progress；首轮不要自行调用 join，也不要 resume_context，直接按任务执行；只有 followup/Watchdog 开出的后续新 turn 才先 resume_context。\n'
            finish='最终完成时先做最终 checkpoint（summary 写清已确认结果，pending_work 写剩余工作，artifacts 列关键源码/报告/证据文件；checkpoint 会持久生成父级 handoff），确认 handoff 后再 complete→report_progress(phase="done")；不要依赖最后一条自然语言回复作为唯一交接；done 只退出当前 participant，不释放共享 Root workspace 资源租约。'
        elif shared_lease_id:
            lease_bootstrap=(
                f'本 Child 与父 Agent 共用同一个 workspace 资源租约。不要 acquire_workspace。'
                f'首轮读取 {join_path(self.config.runtime_root,"AGENTS.md")} 与 runtime_status 后，直接 '
                f'chatgpt_agent(action="join", spawn_ticket="{ticket}", workspace_lease_id="{shared_lease_id}")。'
                'join 成功会返回 participant_workspace_lease_id；从那一刻起，所有工具、checkpoint、resume_context、complete 和 report_progress 都必须使用该 participant handle。'
                'participant 只提供本 Conversation 独立 thread/control/checkpoint 身份，不是第二把 workspace 锁；Goal/Visual/Ultra/Stay 与父树共享。'
            )
            first_turn='只有真实 liveness probe 才先 report_progress；仅 SPAWN_BINDING_PENDING 才重试 join。首次 join 后直接执行任务，不要 resume_context；只有 followup/Watchdog 开出的后续新 turn 才先 resume_context。\n'
            finish='最终完成时先做最终 checkpoint（summary 写清已确认结果，pending_work 写剩余工作，artifacts 列关键源码/报告/证据文件；checkpoint 会持久生成父级 handoff），确认 handoff 后再 complete→report_progress(phase="done")；不要依赖最后一条自然语言回复作为唯一交接；done 只退出当前 participant，不释放共享 Root workspace 资源租约。'
        else:
            lease_bootstrap=(
                f'首轮并行读取 {join_path(self.config.runtime_root,"AGENTS.md")} 与 runtime_status；随后立即取得该工作区自己的 execute_exclusive 租约。'
                f'拿到租约后直接 chatgpt_agent(action="join", spawn_ticket="{ticket}", workspace_lease_id=你的租约)。'
            )
            first_turn='只有真实 liveness probe 才先 report_progress；仅 SPAWN_BINDING_PENDING 才重试 join。首次 join 后直接执行任务，不要 resume_context；只有 followup/Watchdog 开出的后续新 turn 才先 resume_context。\n'
            finish='最终完成时先做最终 checkpoint（summary 写清已确认结果，pending_work 写剩余工作，artifacts 列关键源码/报告/证据文件；checkpoint 会持久生成父级 handoff），确认 handoff 后再 complete→report_progress(phase="done")；不要依赖最后一条自然语言回复作为唯一交接；最后释放独立租约。'
        return (f'@{self.config.connector_name}\n你是用户授权的 GPTAgent Child，项目={self.config.project_name}。'
            f'身份={agent["agent_path"]}；logical_agent_id={agent["logical_agent_id"]}；工作区={agent["workspace"]}（{workspace_kind}）。\n'
            + lease_bootstrap + first_turn +
            f'任务：{instructions}\n' + (f'委派理由：{delegation_reason}\n' if delegation_reason else '') +
            '按 AGENTS.md 的身份、安全、对账和 no-duplicate 规则执行。普通非 Git 目录不要调用 git_status；不要递归搜索 AGENTS。验证通过后不要重复回读已验证文件或做无意义状态探测。'
            '需要跨 turn 时 checkpoint 保存公开进展和剩余工作但不要 complete；' + finish +
            f'接近 {self.config.turn_rotation_minutes:g} 分钟时按 Watchdog 轮换；禁止自行给网页发“继续”、绕过 chatgpt_agent、切 Pro 或伪报完成。')

    def _stale_bootstrap_draft_owner(self,draft,project_id):
        """Recognize only a provably dead, never-submitted GPTAgent bootstrap draft."""
        text=str(draft or '')
        path_match=re.search(r'身份=([^；;\n]+)',text)
        agent_match=re.search(r'logical_agent_id=(ag_[a-z0-9]+)',text,re.I)
        workspace_match=re.search(r'工作区=([^；;（\n]+)',text)
        ticket_match=re.search(r'(ga_ticket_[A-Za-z0-9_-]+)',text)
        participant_match=re.search(r'participant_workspace_lease_id="([0-9a-f]{32})"',text,re.I)
        if not all((path_match,agent_match,workspace_match)) or not (ticket_match or participant_match):
            return None
        agent_id=agent_match.group(1)
        try:
            agent=self.store.get(agent_id)
        except AgentError:
            return None
        if (agent.get('agent_path')!=path_match.group(1).strip()
                or agent.get('workspace')!=workspace_match.group(1).strip()
                or agent.get('project_id')!=project_id
                or agent.get('conversation_id')
                or agent.get('join_state')=='joined'
                or agent.get('kind') not in {'business','watchdog'}):
            return None
        normalize=lambda value:'\n'.join(
            line.strip() for line in str(value or '').replace('\r\n','\n').replace('\r','\n').split('\n') if line.strip()
        )
        draft_sha=digest(normalize(text))
        now=self.store.clock()
        with self.store.connection() as c:
            ticket=None
            if ticket_match:
                ticket=c.execute('SELECT * FROM spawn_tickets WHERE ticket_hash=?',(digest(ticket_match.group(1)),)).fetchone()
                if (not ticket or ticket['logical_agent_id']!=agent_id
                        or ticket['expected_project_id']!=project_id
                        or ticket['expected_conversation_id']):
                    return None
                if not (ticket['status'] in {'revoked','expired'}
                        or (ticket['status']=='pending' and float(ticket['expires_at'])<=now)):
                    return None
            operations=c.execute('SELECT * FROM web_operations WHERE agent_id=? ORDER BY created_at',(agent_id,)).fetchall()
            if not operations:
                return None
            prompt_hashes=[]
            for operation in operations:
                if operation['state'] not in {'PREPARED','BLOCKED','ABORTED'}:
                    return None
                if (operation['expected_conversation_id'] or operation['observed_user_message_id']
                        or operation['observed_request_id'] or operation['observed_turn_exchange_id']):
                    return None
                if operation['prompt_sha256']:
                    prompt_hashes.append(operation['prompt_sha256'])
            if agent.get('kind')=='watchdog':
                if not ticket_match:
                    return None
                watchdog=c.execute('SELECT * FROM watchdogs WHERE logical_agent_id=?',(agent_id,)).fetchone()
                if not watchdog or watchdog['state'] not in {'FAILED','RETIRED','CLOSED'}:
                    return None
                rebuilt=self._watchdog_bootstrap(
                    agent,ticket_match.group(1),watchdog['owner_thread_id'],int(watchdog['generation'])
                )
                if draft_sha not in prompt_hashes and normalize(rebuilt)!=normalize(text):
                    return None
            else:
                if agent.get('state') not in {'CLOSED','BLOCKED'} or draft_sha not in prompt_hashes:
                    return None
        return agent_id

    async def _clear_proven_bootstrap_draft(self,page,caller,expected_draft):
        await self.session.assert_owner(caller)
        composer=page.locator('#prompt-textarea')
        if await composer.inner_text()!=expected_draft:
            raise AgentError('PREPARED_DRAFT_CHANGED','Prepared bootstrap draft changed after proof; refusing to clear it')
        await composer.fill('')

    def _watchdog_bootstrap(self,agent,ticket,scope_id,generation):
        return (
            f'@{self.config.connector_name}\n'
            f'你是用户授权的 GPTAgent Watchdog 子对话，项目={self.config.project_name}。'
            f'身份={agent["agent_path"]}；logical_agent_id={agent["logical_agent_id"]}；'
            f'工作区={agent["workspace"]}；'
            f'scope_thread_id={scope_id}；generation={generation}。\n'
            f'首轮并行读取 {join_path(self.config.runtime_root,"AGENTS.md")} 与 runtime_status；随后取得你自己的 '
            'execute_exclusive 租约。拿到租约后立即 '
            f'chatgpt_agent(action="join", spawn_ticket="{ticket}", workspace_lease_id=你的租约)。'
            '只有 SPAWN_BINDING_PENDING 才重试 join。\n'
            '你的职责只有监督该 scope 内的 business Agents，不直接修改业务文件、不开新的业务 Agent、不替业务 Agent 做任务。'
            '循环使用 chatgpt_agent(list/inspect/history_search/wait/watchdog_status) 获取公开状态；wait_seconds 建议 30–60 秒。'
            '判断时同时看 state、turn、checkpoint、progress_version、pending_work 和最近公开历史，而不是只看一个超时数字。'
            f'若业务 Agent 长时间无实质进展、明显偏航，或单个网页 turn 接近 {self.config.turn_rotation_minutes:g} 分钟，'
            '先用 message 发一次去重后的 mailbox 提醒：完成当前不可中断步骤后 checkpoint，保留 pending_work，并自然结束本轮；不要 complete。'
            '若业务 Agent 已 IDLE/DEAD_RECOVERABLE，且仍有明确 pending_work、未满足 Goal 或上下文清楚表明任务应继续，'
            '用 followup 请求它在原 Conversation 继续；followup 必须经过系统的 browser proof、EXTERNAL_ACTIVITY、claim、去重和 no-progress 门禁，禁止绕过。'
            'RUNNING 且近期仍有实质进展时不要打扰；DONE/CLOSED/BLOCKED/EXTERNAL_ACTIVITY 一律不要唤醒。'
            '每次动作后重新 inspect，避免重复催促。若 watchdog_status 显示本 scope 已关闭，或你的 generation 不再 ACTIVE，'
            '停止监督；若 logical agent 仍可完成则 checkpoint→complete，最后 report_progress(phase="done") 释放自己的独立租约。'
            '禁止切 Pro、禁止自行操作网页发送“继续”、禁止跨 scope 查看或控制其他顶层对话。'
        )

    async def _reconcile_web(self,operation,observer):
        await observer.refresh()
        agent=self.store.get(operation['agent_id'])
        if operation.get('state') in {'ABORTED','BLOCKED'} or agent.get('state') in TERMINAL_AGENT_STATES:
            return {
                'status':'terminated',
                'operation_id':operation['operation_id'],
                'agent':self.manager.public_agent(agent),
                'observation':observer.public(),
                'instruction':'Logical Agent or native submission intent is terminal; do not resend or reconcile this operation.',
            }
        if agent['conversation_id'] and observer.evidence.turn_exchange_id and observer.evidence.user_message_id:
            if observer.intent_request_id or any(n.get('public_content_sha256')==operation['prompt_sha256'] and
                n.get('message_id')==observer.evidence.user_message_id for n in observer.nodes.values()):
                result=self.store.update_web_operation(operation['operation_id'],('SUBMITTING','RECONCILING'),'OBSERVED',
                    observed_user_message_id=observer.evidence.user_message_id,observed_request_id=observer.evidence.request_id,
                    observed_turn_exchange_id=observer.evidence.turn_exchange_id)
                return {'status':'submitted','operation_id':result['operation_id'],'agent':self.manager.public_agent(agent),'observation':observer.public()}
        if operation['state']=='SUBMITTING':
            self.store.update_web_operation(operation['operation_id'],'SUBMITTING','RECONCILING')
        return {'status':'reconciliation_pending','operation_id':operation['operation_id'],'agent':self.manager.public_agent(agent),
                'observation':observer.public(),'instruction':'Do not resend. Reconcile this exact conversation and submission intent.'}

    async def _auto_join_shared_spawn(self,caller,agent,observer,participant):
        """Bind a same-workspace Child after exact native submit proof, without exposing the ticket to the web model."""
        if participant is None:
            return None
        observation=await observer.refresh()
        target=self.store.get(agent['logical_agent_id'])
        if target.get('join_state')=='joined':
            return {
                'status':'joined','agent':self.manager.public_agent(target),
                'participant_workspace_lease_id':participant['lease_id'],
                'shared_root_lease_id':participant['shared_root_lease_id'],
                'resource_lease_shared':True,'participant_reused':bool(participant.get('reused_existing')),
                'auto_joined':True,
            }
        project_ok=(observation.get('project_id')==target['project_id']
                    and project_identity(observer.page.url)==target['project_id'])
        if (not target.get('conversation_id') or observation.get('conversation_id')!=target['conversation_id']
                or not project_ok or observation.get('state')=='EXTERNAL_ACTIVITY'):
            raise AgentError('AUTO_JOIN_BROWSER_PROOF_FAILED',
                'Controller auto-join requires the exact bound project/conversation after native submit proof')
        raw=self.store.issue_ticket(target['logical_agent_id'])
        try:
            joined=self.store.join(raw,runtime_thread_id=str(participant['thread_id']),
                project_id=target['project_id'],conversation_id=target['conversation_id'],browser_verified=True)
        except BaseException:
            if not participant.get('reused_existing') and self.manager.participant_releaser is not None:
                self.manager.participant_releaser(str(participant['lease_id']))
            raise
        self.store.sync_public_relations()
        self.manager._notify()
        return {
            'status':'joined','agent':self.manager.public_agent(joined),
            'participant_workspace_lease_id':participant['lease_id'],
            'shared_root_lease_id':participant['shared_root_lease_id'],
            'resource_lease_shared':True,'participant_reused':bool(participant.get('reused_existing')),
            'auto_joined':True,
            'instruction':'Controller 已用浏览器原生提交证明完成 participant 身份绑定；Child 无需再调用 join。',
        }

    async def spawn(self,caller,p):
        name=(p.get('name') or '').strip()
        role=(p.get('role') or '').strip()
        delegation_reason=(p.get('delegation_reason') or '').strip()
        if not name or not role:
            raise AgentError('SPAWN_DESCRIPTION_REQUIRED','Provide a semantic name and concrete role/task')
        if len(delegation_reason)<12:
            raise AgentError('DELEGATION_REASON_REQUIRED','Every spawn requires a concrete delegation_reason (at least 12 characters)')
        ultra=(self.manager.runtime_facts(caller.lease_id).get('ultra_mode') or {})
        if not ultra.get('active'):
            raise AgentError('ULTRA_REQUIRED','Spawn is disabled until this task explicitly enables Ultra sub-agent mode')
        if not re.fullmatch(r'[a-z0-9][a-z0-9-]{1,27}',name) or re.fullmatch(r'(?:task\d*|child-?\d*|new-chat|subtask\d*)',name):
            raise AgentError('MEANINGLESS_NAME','Use a semantic kebab-case name before allocating an agent workspace')
        policy=requested_model_policy(self.config,model=p.get('model_policy') or 'non-pro',
            reasoning_effort=p.get('reasoning_effort'),performance_priority=p.get('performance_priority'),
            allow_pro=bool(p.get('allow_pro')),user_pro_authorized=False)
        project=(await self.ensure_project(caller))['project']
        own=self.store.by_thread(caller.thread_id)
        parent_ref=p.get('parent_agent_id') or (own['logical_agent_id'] if own else None)
        parent=self.store.get(parent_ref) if parent_ref else None
        if parent:
            self.manager._authorize(caller,parent,'spawn')
        instructions=p.get('message') or role
        key=digest(json.dumps([caller.thread_id,parent_ref,name,role,instructions,delegation_reason],ensure_ascii=False))
        async with self.session.conversation_lock('spawn:'+key):
            existing=self.store.web_operation_by_key(key)
            if existing:
                agent=self.store.get(existing['agent_id'])
                if existing['state']=='OBSERVED':
                    result={'status':'already_submitted','operation_id':existing['operation_id'],'agent':self.manager.public_agent(agent)}
                    if agent.get('conversation_id'):
                        page=await self._page_by_target(self.store.setting('agent_tab:'+agent['logical_agent_id']))
                        if page:
                            observer=await self._observer(agent,caller)
                            same_workspace=(str(Path(caller.workspace).resolve()).casefold()==str(Path(agent['workspace']).resolve()).casefold())
                            direct_parent=bool(parent and own and own['logical_agent_id']==parent['logical_agent_id'])
                            root_creator=bool(not parent and agent['owner_thread_id']==caller.thread_id)
                            shared_lease_id=caller.lease_id if same_workspace and (direct_parent or root_creator) else None
                            if same_workspace and not shared_lease_id:
                                raise AgentError('JOIN_SHARED_PARENT_MISMATCH','Same-workspace delegation requires the direct parent or native root creator')
                            if shared_lease_id and agent.get('join_state')!='joined':
                                if self.manager.participant_attacher is None:
                                    raise AgentError('SHARED_LEASE_COMPONENT_NOT_READY','Runtime participant attacher is unavailable')
                                participant=self.manager.participant_attacher(
                                    shared_lease_id,logical_agent_id=agent['logical_agent_id'],
                                    owner_label=f'GPTAgent participant {agent["agent_path"]}')
                                joined=await self._auto_join_shared_spawn(caller,agent,observer,participant)
                                result.update(joined,operation_id=existing['operation_id'])
                            self._background(self._rename_web_title(caller,self.store.get(agent['logical_agent_id']),page),'title_alignment')
                            result['title']={'status':'scheduled'}
                    return result
                if existing['state'] in {'SUBMITTING','RECONCILING'}:
                    observer=await self._observer(agent,caller)
                    result=await self._reconcile_web(existing,observer)
                    bound=self.store.get(agent['logical_agent_id'])
                    if result.get('status')=='submitted' and bound.get('conversation_id'):
                        same_workspace=(str(Path(caller.workspace).resolve()).casefold()==str(Path(bound['workspace']).resolve()).casefold())
                        direct_parent=bool(parent and own and own['logical_agent_id']==parent['logical_agent_id'])
                        root_creator=bool(not parent and bound['owner_thread_id']==caller.thread_id)
                        shared_lease_id=caller.lease_id if same_workspace and (direct_parent or root_creator) else None
                        if same_workspace and not shared_lease_id:
                            raise AgentError('JOIN_SHARED_PARENT_MISMATCH','Same-workspace delegation requires the direct parent or native root creator')
                        if shared_lease_id and bound.get('join_state')!='joined':
                            if self.manager.participant_attacher is None:
                                raise AgentError('SHARED_LEASE_COMPONENT_NOT_READY','Runtime participant attacher is unavailable')
                            participant=self.manager.participant_attacher(
                                shared_lease_id,logical_agent_id=bound['logical_agent_id'],
                                owner_label=f'GPTAgent participant {bound["agent_path"]}')
                            joined=await self._auto_join_shared_spawn(caller,bound,observer,participant)
                            result.update(joined,operation_id=existing['operation_id'],observation=observer.public())
                            bound=self.store.get(bound['logical_agent_id'])
                        self._background(self._rename_web_title(caller,bound,observer.page),'title_alignment')
                        result['title']={'status':'scheduled'}
                    return result
                if existing['state']!='PREPARED':
                    raise AgentError('SPAWN_PREVIOUSLY_BLOCKED','Inspect the previous operation; no replacement was automatically created')
            else:
                workspace=p.get('workspace')
                if workspace and not Path(workspace).is_dir():
                    raise AgentError('WORKSPACE_MISSING','Explicit child workspace must already exist; do not implicitly alter another project')
                # A true parent/child delegation shares the parent's resource lease by default.
                # Explicitly choosing another existing workspace keeps the legacy independent-lease path.
                if not workspace:
                    workspace=caller.workspace
                agent=self.store.create(name=name,role=role,display_name=p.get('display_name'),workspace=workspace,
                    project_id=project['project_id'],owner_thread_id=caller.thread_id,
                    parent_id=parent['logical_agent_id'] if parent else None,policy=policy,delegation_reason=delegation_reason)
                # A Watchdog belongs to the top-level Runtime conversation, not
                # the globally reused logical /root. Child callers inherit the
                # creator scope; controller/top-level callers start their own.
                self.store.assign_supervisor_scope(agent['logical_agent_id'],self.manager.supervisor_scope_for_caller(caller))
                existing=self.store.prepare_web_operation(agent['logical_agent_id'],key,'spawn')
            aid=agent['logical_agent_id']
            page=await self._claim_management_project_page(caller,project,aid)
            if page is None:
                page=await self._owned_page(caller,'agent_tab:'+aid,project['url'])
            else:
                # Refill the warm management slot outside this Child's
                # critical path; the exact URL/project proof stays mandatory.
                self._background(self._prewarm_management_project_page(caller,project),'project_prewarm')
            await page.locator('#prompt-textarea').wait_for(state='visible',timeout=25000)
            if project_identity(page.url)!=project['project_id'] or '/c/' in urlsplit(page.url).path:
                raise AgentError('SPAWN_TARGET_CHANGED','Prepared spawn tab no longer shows the intended empty project composer')
            observer=await self._observer(agent,caller)
            before=await observer.refresh()
            if before.get('composer_nonempty'):
                # A PREPARED operation has not crossed the irreversible-submit
                # boundary.  If the draft visibly proves it is this exact
                # logical Agent's own bootstrap on its dedicated empty project
                # tab, it is safe to discard and regenerate a fresh one-time
                # ticket.  Anything else is treated as external/user activity.
                draft=await page.locator('#prompt-textarea').inner_text()
                target_id=await self._target_id(page)
                normalized_draft='\n'.join(
                    line.strip() for line in draft.replace('\r\n','\n').replace('\r','\n').split('\n') if line.strip())
                own_draft=(existing['state']=='PREPARED' and existing.get('target_id') in {None,target_id}
                    and not observer.intent_request_id and not before.get('conversation_id')
                    and f'logical_agent_id={aid}' in draft and agent['workspace'] in draft
                    and agent['agent_path'] in draft
                    and ('ga_ticket_' in draft or (existing.get('prompt_sha256') and existing['prompt_sha256']==digest(normalized_draft))))
                stale_owner=None if own_draft else self._stale_bootstrap_draft_owner(draft,project['project_id'])
                if not own_draft and not stale_owner:
                    raise AgentError('PREPARED_DRAFT_RECONCILE_REQUIRED',
                        'An unrelated/user draft exists; it was preserved and no duplicate submit was attempted')
                await self._clear_proven_bootstrap_draft(page,caller,draft)
                after_clear=await observer.refresh()
                if after_clear.get('composer_nonempty'):
                    code='STALE_BOOTSTRAP_CLEAR_FAILED' if stale_owner else 'PREPARED_DRAFT_CLEAR_FAILED'
                    raise AgentError(code,'Prepared bootstrap draft did not clear cleanly')
            effective=await ensure_non_pro_effort(page,policy)
            self.store.set_effective_policy(aid,effective['effective_model'],effective['effective_reasoning_effort'])
            same_workspace=(str(Path(caller.workspace).resolve()).casefold()==str(Path(agent['workspace']).resolve()).casefold())
            direct_parent=bool(parent and own and own['logical_agent_id']==parent['logical_agent_id'])
            root_creator=bool(not parent and agent['owner_thread_id']==caller.thread_id)
            shared_lease_id=caller.lease_id if same_workspace and (direct_parent or root_creator) else None
            if same_workspace and not shared_lease_id:
                raise AgentError('JOIN_SHARED_PARENT_MISMATCH','Same-workspace delegation requires the direct parent or native root creator')
            if observer.intent_sha and observer.intent_request_id is None:
                # A previous attempt never committed a submit. Its exact draft
                # is not erased here; operator reconciliation remains explicit.
                raise AgentError('PREPARED_DRAFT_RECONCILE_REQUIRED','A prepared draft is still present; no duplicate submit was attempted')
            participant=None
            ticket=None
            if shared_lease_id:
                if self.manager.participant_attacher is None:
                    raise AgentError('SHARED_LEASE_COMPONENT_NOT_READY','Runtime participant attacher is unavailable')
                participant=self.manager.participant_attacher(
                    shared_lease_id,logical_agent_id=aid,owner_label=f'GPTAgent participant {agent["agent_path"]}')
            else:
                ticket=self.store.issue_ticket(aid)
            prompt=self._bootstrap(
                agent,ticket,instructions,delegation_reason,shared_lease_id=shared_lease_id,
                participant_workspace_lease_id=participant['lease_id'] if participant else None)
            oid=existing['operation_id']
            self.store.update_web_operation(oid,'PREPARED','PREPARED',target_id=await self._target_id(page),prompt_sha256=digest(prompt))
            def mark():
                self.store.update_web_operation(oid,'PREPARED','SUBMITTING')
            try:
                await submit_native_once(page,observer,prompt,control_check=lambda:self.manager.control_check(caller.lease_id),mark_submitting=mark)
            except BaseException as exc:
                state=self.store.web_operation(oid)['state']
                if state=='SUBMITTING':
                    self.store.update_web_operation(oid,'SUBMITTING','RECONCILING',error_class=getattr(exc,'code',type(exc).__name__))
                elif participant and not participant.get('reused_existing') and self.manager.participant_releaser is not None:
                    self.manager.participant_releaser(str(participant['lease_id']))
                raise
            result=await self._reconcile_web(self.store.web_operation(oid),observer)
            if result.get('status') in {'submitted','already_submitted'}:
                bound=self.store.get(aid)
                if bound.get('conversation_id'):
                    if participant and bound.get('join_state')!='joined':
                        observation=await observer.refresh()
                        project_ok=(observation.get('project_id')==bound['project_id'] and project_identity(observer.page.url)==bound['project_id'])
                        if observation.get('conversation_id')!=bound['conversation_id'] or not project_ok or observation['state']=='EXTERNAL_ACTIVITY':
                            raise AgentError('AUTO_JOIN_BROWSER_PROOF_FAILED','Controller auto-join browser proof does not match the submitted Child conversation')
                        internal_ticket=self.store.issue_ticket(aid,ttl_seconds=60)
                        bound=self.store.join(
                            internal_ticket,runtime_thread_id=str(participant['thread_id']),project_id=bound['project_id'],
                            conversation_id=bound['conversation_id'],browser_verified=True)
                        self.store.sync_public_relations()
                        self.manager._notify()
                        result.update(
                            auto_joined=True,
                            participant_workspace_lease_id=participant['lease_id'],
                            shared_root_lease_id=participant['shared_root_lease_id'],
                            resource_lease_shared=True)
                    self._background(self._rename_web_title(caller,bound,observer.page),'title_alignment')
                    result['agent']=self.manager.public_agent(bound)
                    result['title']={'status':'scheduled'}
            return result

    async def join(self,caller,p):
        raw=p.get('spawn_ticket') or ''
        target=self.store.ticket_target(raw)
        same_workspace=(str(Path(caller.workspace).resolve()).casefold()==str(Path(target['workspace']).resolve()).casefold())
        own=self.store.by_thread(caller.thread_id)
        direct_parent=bool(target.get('parent_agent_id') and own and own.get('logical_agent_id')==target.get('parent_agent_id'))
        root_creator=bool(not target.get('parent_agent_id') and target.get('owner_thread_id')==caller.thread_id)
        shared_join=same_workspace and (direct_parent or root_creator)
        independent_join=same_workspace and own is None and caller.lease_role!='participant'
        if not shared_join and not independent_join:
            if not same_workspace:
                raise AgentError('JOIN_WORKSPACE_MISMATCH','Cross-workspace Child must hold its own assigned workspace lease')
            raise AgentError('JOIN_SHARED_PARENT_MISMATCH','A shared workspace lease may only mint a participant for its direct Child/root creation')
        observer=await self._observer(target,caller)
        observation=await observer.refresh()
        target=self.store.get(target['logical_agent_id'])
        if not target['conversation_id']:
            raise AgentError('SPAWN_BINDING_PENDING','Wait for the native submission identity to finish binding')
        project_ok=(observation.get('project_id')==target['project_id'] and project_identity(observer.page.url)==target['project_id'])
        if observation.get('conversation_id')!=target['conversation_id'] or not project_ok or observation['state']=='EXTERNAL_ACTIVITY':
            raise AgentError('JOIN_BROWSER_PROOF_FAILED','Native page/project/conversation proof does not match this ticket')
        proof=await observer.page.locator('[data-message-author-role="user"]').evaluate_all(
            '(els,ticket)=>els.some(e=>(e.innerText||e.textContent||"").includes(ticket))',raw)
        if not proof:
            raise AgentError('JOIN_TICKET_NOT_IN_BOOTSTRAP','Ticket was not observed in the bound conversation bootstrap')
        participant=None
        runtime_thread_id=caller.thread_id
        if shared_join:
            if self.manager.participant_attacher is None:
                raise AgentError('SHARED_LEASE_COMPONENT_NOT_READY','Runtime participant attacher is unavailable')
            participant=self.manager.participant_attacher(
                caller.lease_id, logical_agent_id=target['logical_agent_id'],
                owner_label=f'GPTAgent participant {target["agent_path"]}',
            )
            runtime_thread_id=str(participant['thread_id'])
        try:
            joined=self.store.join(raw,runtime_thread_id=runtime_thread_id,project_id=target['project_id'],
                conversation_id=target['conversation_id'],browser_verified=True)
        except BaseException:
            if participant and not participant.get('reused_existing') and self.manager.participant_releaser is not None:
                self.manager.participant_releaser(str(participant['lease_id']))
            raise
        if joined.get('kind')=='watchdog':
            self.store.activate_watchdog(joined['logical_agent_id'])
        self.store.sync_public_relations()
        self.manager._notify()
        result={'status':'joined','agent':self.manager.public_agent(joined),
                'instruction':'身份绑定完成。之后直接 checkpoint / resume_context / complete，不再使用一次性 ticket。'}
        if participant:
            result.update(
                participant_workspace_lease_id=participant['lease_id'],
                shared_root_lease_id=participant['shared_root_lease_id'],
                resource_lease_shared=True,
                participant_reused=bool(participant.get('reused_existing')),
                instruction='身份绑定完成。之后所有本机工具都使用 participant_workspace_lease_id；它与父树共享一个 workspace 资源租约，但保留本 Conversation 独立身份。',
            )
        return result


    async def _rename_web_title(self,caller,agent,page=None):
        """Native rename/readback; titles are never identity credentials."""
        agent=self.store.get(agent['logical_agent_id'])
        if not agent.get('conversation_id'):
            return {'status':'pending','title_drift':True,'reason':'conversation_not_bound'}
        page=page or await self._page_by_target(self.store.setting('agent_tab:'+agent['logical_agent_id']))
        if page is None:
            return {'status':'detached','title_drift':True,'reason':'conversation_tab_absent'}
        await self.session.assert_owner(caller)
        cid,expected=agent['conversation_id'],agent['web_title']
        links=page.locator(f'a[href$="/c/{cid}"]')
        visible=[]
        for index in range(await links.count()):
            item=links.nth(index)
            if await item.is_visible(): visible.append(item)
        if len(visible)!=1:
            result={'status':'unknown','title_drift':True,'reason':'sidebar_conversation_link_not_unique','expected':expected}
            self.store.set_setting('title_status:'+agent['logical_agent_id'],result)
            return result
        link=visible[0]
        current=(await link.inner_text()).strip()
        if current==expected:
            result={'status':'aligned','title_drift':False,'current':current,'expected':expected}
            self.store.set_setting('title_status:'+agent['logical_agent_id'],result)
            return result
        await link.hover(timeout=3000)
        row=link.locator('xpath=ancestor::*[@data-sidebar-item][1]')
        if await row.count()!=1: row=link.locator('xpath=..')
        options=row.get_by_role('button',name=re.compile(r'(对话选项|chat options|conversation options|更多|more)',re.I))
        if await options.count()!=1:
            result={'status':'drift','title_drift':True,'current':current,'expected':expected,'reason':'rename_menu_unavailable'}
            self.store.set_setting('title_status:'+agent['logical_agent_id'],result)
            return result
        await native_activate(options)
        menu=page.locator('[role="menu"][data-state="open"]:visible')
        await menu.wait_for(state='visible',timeout=3000)
        rename=menu.get_by_role('menuitem',name=re.compile(r'^(重命名|Rename)$',re.I))
        if await rename.count()!=1:
            await page.keyboard.press('Escape')
            result={'status':'drift','title_drift':True,'current':current,'expected':expected,'reason':'rename_action_unavailable'}
            self.store.set_setting('title_status:'+agent['logical_agent_id'],result)
            return result
        await native_activate(rename)
        dialog=page.get_by_role('dialog')
        await dialog.wait_for(state='visible',timeout=3000)
        textbox=dialog.get_by_role('textbox')
        if await textbox.count()!=1:
            raise AgentError('TITLE_RENAME_CONTROL_AMBIGUOUS','Native rename textbox is not unique')
        await textbox.fill(expected)
        save=dialog.get_by_role('button',name=re.compile(r'^(保存|Save|重命名|Rename)$',re.I))
        if await save.count()!=1:
            raise AgentError('TITLE_RENAME_SAVE_MISSING','Native rename confirmation control is not unique')
        await self.session.assert_owner(caller)
        await native_activate(save)
        deadline=asyncio.get_running_loop().time()+5
        while asyncio.get_running_loop().time()<deadline:
            current=(await link.inner_text()).strip()
            if current==expected: break
            await asyncio.sleep(0.15)
        result={'status':'aligned' if current==expected else 'drift','title_drift':current!=expected,
                'current':current,'expected':expected,'reason':None if current==expected else 'rename_readback_mismatch'}
        self.store.set_setting('title_status:'+agent['logical_agent_id'],result)
        return result

    async def _submit_existing(self,action,caller,target,text,observer=None):
        if not isinstance(text,str) or not text.strip():
            raise AgentError('FOLLOWUP_REQUIRED','A nonempty follow-up message is required')
        target=self.store.get(target['logical_agent_id'])
        if target['state'] in TERMINAL_AGENT_STATES|{'EXTERNAL_ACTIVITY'}:
            raise AgentError('WAKE_NOT_ALLOWED','Terminal or externally active agents cannot be awakened')
        if not target.get('conversation_id'):
            raise AgentError('CONVERSATION_NOT_BOUND','Agent has no verified conversation')
        observer=observer or await self._observer(target,caller,reopen=True)
        observation=await observer.refresh()
        if observation['state']=='EXTERNAL_ACTIVITY':
            self.store.set_state(target['logical_agent_id'],'EXTERNAL_ACTIVITY')
            raise AgentError('EXTERNAL_ACTIVITY','Conversation changed outside the managed operation')
        if observation['state']=='RUNNING' or observer.evidence.dom_stop:
            raise AgentError('GENERATION_ACTIVE','A new user turn cannot be submitted while this conversation is running')
        cid=target['conversation_id']
        tail=observation.get('tail_message_id')
        key=digest(json.dumps([action,target['logical_agent_id'],target['turn_epoch'],tail,text],ensure_ascii=False))
        async with self.session.conversation_lock(cid):
            existing=self.store.web_operation_by_key(key)
            if existing:
                if existing['state']=='OBSERVED':
                    return {'status':'already_submitted','operation_id':existing['operation_id'],'agent':self.manager.public_agent(self.store.get(target['logical_agent_id']))}
                if existing['state'] in {'SUBMITTING','RECONCILING'}:
                    return await self._reconcile_web(existing,observer)
                if existing['state']!='PREPARED':
                    raise AgentError('FOLLOWUP_PREVIOUSLY_BLOCKED','Previous native submission was blocked; no replacement was created')
            else:
                existing=self.store.prepare_web_operation(target['logical_agent_id'],key,'wake' if action=='wake' else 'followup',
                    target_id=await self._target_id(observer.page),expected_conversation_id=cid,expected_tail_message_id=tail)
            policy=json.loads(target.get('model_policy_json') or '{}')
            if policy:
                effective=await ensure_non_pro_effort(observer.page,policy)
                self.store.set_effective_policy(target['logical_agent_id'],effective['effective_model'],effective['effective_reasoning_effort'])
            oid=existing['operation_id']
            self.store.update_web_operation(oid,'PREPARED','PREPARED',target_id=await self._target_id(observer.page),prompt_sha256=digest(text))
            def control():
                self.manager.control_check(caller.lease_id)
                self.manager._authorize(caller,self.store.get(target['logical_agent_id']),action)
            def mark():
                control()
                self.store.update_web_operation(oid,'PREPARED','SUBMITTING')
            try:
                await submit_native_once(observer.page,observer,text,control_check=control,
                    mark_submitting=mark,expected_conversation=cid,expected_tail=tail)
            except BaseException as exc:
                state=self.store.web_operation(oid)['state']
                if state=='SUBMITTING':
                    self.store.update_web_operation(oid,'SUBMITTING','RECONCILING',error_class=getattr(exc,'code',type(exc).__name__))
                raise
            return await self._reconcile_web(self.store.web_operation(oid),observer)

    async def _reconcile_recovery(self,operation,observer):
        observation=await observer.refresh()
        target=self.store.get(operation['agent_id'])
        if observation['state']=='EXTERNAL_ACTIVITY' or observation.get('conversation_id')!=target.get('conversation_id'):
            if operation['state'] in {'CLAIMED','PRECHECKED'}:
                self.store.advance_operation(operation['operation_id'],operation['state'],'ABORTED',last_error_class='EXTERNAL_ACTIVITY')
            if target['state'] not in TERMINAL_AGENT_STATES|{'EXTERNAL_ACTIVITY'}:
                self.store.set_state(target['logical_agent_id'],'EXTERNAL_ACTIVITY')
            return {'status':'external_activity','agent':self.manager.public_agent(self.store.get(target['logical_agent_id']))}
        source_epoch=int(operation['source_turn_epoch'])
        current=self.store.get(target['logical_agent_id'])
        turn=self.store.latest_turn(target['logical_agent_id'])
        wanted=digest(operation.get('requested_text') or '继续')
        candidates=[n for n in observer.nodes.values() if n.get('role')=='user' and not n.get('hidden') and n.get('public_content_sha256')==wanted]
        if current['turn_epoch']>source_epoch and turn and turn.get('turn_exchange_id')==observation.get('turn_exchange_id'):
            user=next((n for n in candidates if n.get('message_id')==turn.get('user_message_id')),None)
            if user:
                expected=operation['state']
                if expected in {'SUBMITTING','SUBMITTED'}:
                    self.store.advance_operation(operation['operation_id'],expected,'RECONCILING',
                        observed_user_message_id=user['message_id'],observed_generation_request_id=observation.get('request_id'),
                        observed_new_turn_exchange_id=turn['turn_exchange_id'])
                    expected='RECONCILING'
                if expected=='RECONCILING':
                    result=self.store.advance_operation(operation['operation_id'],'RECONCILING','RESUMED',
                        observed_user_message_id=user['message_id'],observed_generation_request_id=observation.get('request_id'),
                        observed_new_turn_exchange_id=turn['turn_exchange_id'])
                    return {'status':'resumed','operation':result,'agent':self.manager.public_agent(self.store.get(target['logical_agent_id'])),'observation':observation}
        return {'status':'reconciliation_pending','operation':self.store.operation(operation['operation_id']),
                'agent':self.manager.public_agent(current),'observation':observation,
                'instruction':'Do not resend 继续. Reconcile the exact user node/generation before any compensation.'}

    async def _recover_same_conversation(self,caller,target,*,claimed_by):
        target=self.store.get(target['logical_agent_id'])
        observer=await self._observer(target,caller,reopen=True)
        observation=await observer.refresh()
        if observation['state']=='EXTERNAL_ACTIVITY':
            self.store.set_state(target['logical_agent_id'],'EXTERNAL_ACTIVITY')
            return {'status':'external_activity','agent':self.manager.public_agent(self.store.get(target['logical_agent_id']))}
        if not observation.get('recoverable'):
            return {'status':'not_recoverable','observation':observation,'agent':self.manager.public_agent(target)}
        claim=self.store.claim_recovery(target['logical_agent_id'],target['turn_epoch'],claimed_by=claimed_by,
            expected_tail_message_id=observation.get('tail_message_id'))
        if claim['status']=='reconcile_required': return await self._reconcile_recovery(claim['operation'],observer)
        if claim['status']!='claimed': return claim
        operation=claim['operation']; cid=target['conversation_id']
        async with self.session.conversation_lock(cid):
            before=await observer.refresh()
            if before['state']=='EXTERNAL_ACTIVITY' or before.get('tail_message_id')!=operation.get('expected_tail_message_id'):
                self.store.advance_operation(operation['operation_id'],'CLAIMED','ABORTED',last_error_class='EXTERNAL_ACTIVITY')
                self.store.set_state(target['logical_agent_id'],'EXTERNAL_ACTIVITY')
                return {'status':'external_activity','agent':self.manager.public_agent(self.store.get(target['logical_agent_id']))}
            if not before.get('recoverable') or observer.evidence.dom_stop:
                self.store.advance_operation(operation['operation_id'],'CLAIMED','ABORTED',last_error_class='NO_LONGER_RECOVERABLE')
                return {'status':'not_recoverable','observation':before}
            self.store.advance_operation(operation['operation_id'],'CLAIMED','PRECHECKED')
            def control():
                self.manager.control_check(caller.lease_id)
                self.manager._authorize(caller,self.store.get(target['logical_agent_id']),'followup')
            def mark():
                control()
                self.store.advance_operation(operation['operation_id'],'PRECHECKED','SUBMITTING')
            try:
                await submit_native_once(observer.page,observer,'继续',control_check=control,
                    mark_submitting=mark,expected_conversation=cid,expected_tail=operation.get('expected_tail_message_id'))
                after=await observer.refresh()
                if observer.evidence.request_seen and observer.evidence.user_message_id and observer.evidence.turn_exchange_id:
                    self.store.advance_operation(operation['operation_id'],'SUBMITTING','SUBMITTED',
                        observed_user_message_id=observer.evidence.user_message_id,observed_generation_request_id=observer.evidence.request_id,
                        observed_new_turn_exchange_id=observer.evidence.turn_exchange_id)
                else:
                    self.store.advance_operation(operation['operation_id'],'SUBMITTING','RECONCILING',last_error_class='SUBMIT_AMBIGUOUS')
                    return await self._reconcile_recovery(self.store.operation(operation['operation_id']),observer)
            except BaseException as exc:
                state=self.store.operation(operation['operation_id'])['state']
                if state=='SUBMITTING':
                    self.store.advance_operation(operation['operation_id'],'SUBMITTING','RECONCILING',last_error_class=getattr(exc,'code',type(exc).__name__))
                raise
            return await self._reconcile_recovery(self.store.operation(operation['operation_id']),observer)

    async def agent_action(self,action,caller,target,p):
        target=self.store.get(target['logical_agent_id'])
        if action=='close':
            page=await self._page_by_target(self.store.setting('agent_tab:'+target['logical_agent_id']))
            dropped=await self._drop_observer(target['logical_agent_id'],close_page=True)
            if not dropped and page and not page.is_closed():
                await self.session.assert_owner(caller)
                await page.close()
            self.store.delete_setting('agent_tab:'+target['logical_agent_id'])
            result=self.store.set_state(target['logical_agent_id'],'CLOSED'); self.manager._notify()
            cleanup=self._cleanup_forced_terminal_participant(result)
            return {'status':'closed','agent':self.manager.public_agent(result),'participant_cleanup':cleanup}
        observer=await self._observer(target,caller,reopen=True)
        observation=await observer.refresh()
        source=self.store.by_thread(caller.thread_id)
        self.manager._authorize(caller,self.store.get(target['logical_agent_id']),action)
        if action=='interrupt':
            if observation['state']!='RUNNING' or not observer.evidence.dom_stop:
                return {'status':'idle','agent':self.manager.public_agent(target),'observation':observation}
            stop=observer.page.locator('button[data-testid="stop-button"]:visible')
            if await stop.count()!=1: raise AgentError('STOP_CONTROL_AMBIGUOUS','Active generation has no unique native stop control')
            async with self.session.conversation_lock(target['conversation_id']):
                await self.session.assert_owner(caller); await native_activate(stop)
                observer.evidence.client_interrupted=True; await observer.refresh()
            self.store.set_state(target['logical_agent_id'],'IDLE'); self.manager._notify()
            return {'status':'interrupted','agent':self.manager.public_agent(self.store.get(target['logical_agent_id'])),'observation':observer.public()}
        if action=='followup' and observation['state']=='DEAD_RECOVERABLE':
            sender=source['logical_agent_id'] if source else 'controller:'+caller.thread_id
            text=(p.get('message') or '').strip()
            queued=None
            if text:
                queued=self.store.enqueue(sender,target['logical_agent_id'],text,kind='message')
                self.manager._notify()
            recovered=await self._recover_same_conversation(caller,target,claimed_by=sender)
            return {**recovered,'route':'recovery','mailbox':queued}
        if action=='followup' and observation['state'] in {'RUNNING','SUSPECT','DETACHED'}:
            source=self.store.by_thread(caller.thread_id); sender=source['logical_agent_id'] if source else 'controller:'+caller.thread_id
            result=self.store.enqueue(sender,target['logical_agent_id'],p.get('message') or '',kind='message'); self.manager._notify()
            return {'status':'queued','route':'mailbox',**result,'observation':observation}
        if action=='followup': return await self._submit_existing('followup',caller,target,p.get('message') or '',observer)
        if action=='wake': return await self._submit_existing('wake',caller,target,p.get('message') or '',observer)
        raise AgentError('ACTION_UNSUPPORTED','Unsupported agent browser action')

    async def attach(self,caller,p):
        url=(p.get('conversation_url') or '').strip(); name=(p.get('name') or '').strip(); role=(p.get('role') or '').strip(); workspace=(p.get('workspace') or '').strip()
        if not url or not name or not role or not workspace:
            raise AgentError('ATTACH_DESCRIPTION_REQUIRED','Attach requires conversation_url, semantic name, role and existing workspace')
        if not Path(workspace).is_dir(): raise AgentError('WORKSPACE_MISSING','Attach workspace must already exist')
        project=(await self.ensure_project(caller))['project']
        if project_identity(url)!=project['project_id']: raise AgentError('ATTACH_PROJECT_MISMATCH','Conversation URL is not in the bound GPTAgent project')
        match=re.search(r'/c/([A-Za-z0-9_-]{8,100})(?:/|$)',urlsplit(url).path)
        if not match: raise AgentError('INVALID_CONVERSATION_URL','Attach requires an exact project conversation URL')
        cid=match.group(1)
        # Validate the attachment in a controller-owned background page. Never
        # reuse a user-visible tab merely because it shows the same Conversation.
        existing=await self.session.new_background_page(caller,url)
        if project_identity(existing.url)!=project['project_id']: raise AgentError('ATTACH_PROJECT_MISMATCH','Opened conversation is not in the bound GPTAgent project')
        parent_ref=p.get('parent_agent_id'); parent=self.store.get(parent_ref) if parent_ref else None
        if parent: self.manager._authorize(caller,parent,'attach')
        policy=requested_model_policy(self.config,model=p.get('model_policy') or 'non-pro',reasoning_effort=p.get('reasoning_effort'),
            performance_priority=p.get('performance_priority'),allow_pro=bool(p.get('allow_pro')),user_pro_authorized=False)
        agent=self.store.create(name=name,role=role,display_name=p.get('display_name'),workspace=workspace,project_id=project['project_id'],
            owner_thread_id=caller.thread_id,parent_id=parent['logical_agent_id'] if parent else None,policy=policy)
        self.store.assign_supervisor_scope(agent['logical_agent_id'],self.manager.supervisor_scope_for_caller(caller))
        self.store.bind_conversation(agent['logical_agent_id'],cid,existing.url,project['project_id'])
        self.store.set_setting('agent_tab:'+agent['logical_agent_id'],await self._target_id(existing))
        observer=await PageObserver(existing,self.store,agent['logical_agent_id'],on_change=self.manager._notify).start(); self.observers[agent['logical_agent_id']]=observer
        observation=await observer.refresh()
        if observation.get('project_id')!=project['project_id'] or observation.get('conversation_id')!=cid:
            self.store.set_state(agent['logical_agent_id'],'CLOSED'); raise AgentError('ATTACH_BROWSER_PROOF_FAILED','Native project/conversation proof did not match the requested attachment')
        ticket=self.store.issue_ticket(agent['logical_agent_id']); bootstrap=self._bootstrap(agent,ticket,p.get('message') or role)
        submitted=await self._submit_existing('wake',caller,agent,bootstrap,observer)
        title=await self._rename_web_title(caller,self.store.get(agent['logical_agent_id']),existing)
        return {'status':'attached','agent':self.manager.public_agent(self.store.get(agent['logical_agent_id'])),'submission':submitted,'title':title}


    async def _spawn_watchdog(self,caller,scope_id):
        """Create one real Watchdog Conversation for a supervisor scope.

        Each generation owns an independent workspace/Runtime lease. Durable
        supervisor context lives in the shared Agent store, so generations can
        hand off without sharing the same failure domain.
        """
        def control():
            state=self.store.watchdog_scope(scope_id)
            if not state.get('enabled') or state.get('manual_disabled'):
                raise AgentError('WATCHDOG_INACTIVE','Scope was disabled while its Watchdog was being prepared')
            if caller is not None:
                self.manager.control_check(caller.lease_id)
        control()
        await self.session.ensure(caller,launch=True)
        project=(await self.ensure_project(caller))['project']
        control()
        allocation=self.store.allocate_watchdog(scope_id)
        generation=int(allocation['generation'])
        workspace=join_path(self.config.default_work_root,f'watchdog-wd-{generation:03d}')
        aid=None
        operation=None
        try:
            await self.session.assert_owner(caller)
            Path(workspace).mkdir(parents=True,exist_ok=True)
            policy=requested_model_policy(self.config,model='non-pro',reasoning_effort='high',
                performance_priority='responsiveness',allow_pro=False,user_pro_authorized=False)
            agent=self.store.create(name=f'watchdog-{generation:03d}',
                role=f'监督顶层 GPTAgent scope {scope_id} 的业务 Agent 状态、续作与换轮，不执行业务任务',
                workspace=workspace,project_id=project['project_id'],owner_thread_id=scope_id,
                kind='watchdog',watchdog_generation=generation,policy=policy)
            aid=agent['logical_agent_id']
            self.store.assign_supervisor_scope(aid,scope_id)
            self.store.bind_watchdog(generation,aid)
            key=digest(json.dumps(['watchdog-spawn',scope_id,generation],ensure_ascii=False))
            operation=self.store.prepare_web_operation(aid,key,'spawn')
            # Watchdog supervision is infrastructure: it must not take over
            # the shared management/prewarm composer used by normal Agent
            # spawns. Keep its browser ownership isolated to its own tab.
            page=await self._watchdog_spawn_page(caller,project,aid)
            await page.locator('#prompt-textarea').wait_for(state='visible',timeout=25000)
            if project_identity(page.url)!=project['project_id'] or '/c/' in urlsplit(page.url).path:
                raise AgentError('WATCHDOG_SPAWN_TARGET_CHANGED','Watchdog spawn tab is not the empty GPTAgent project composer')
            observer=await self._observer(agent,caller)
            before=await observer.refresh()
            if before.get('composer_nonempty') or before.get('conversation_id'):
                raise AgentError('WATCHDOG_SPAWN_COMPOSER_DIRTY','Watchdog requires a verified empty project composer')
            effective=await ensure_non_pro_effort(page,policy)
            self.store.set_effective_policy(aid,effective['effective_model'],effective['effective_reasoning_effort'])
            ticket=self.store.issue_ticket(aid)
            prompt=self._watchdog_bootstrap(agent,ticket,scope_id,generation)
            oid=operation['operation_id']
            self.store.update_web_operation(oid,'PREPARED','PREPARED',target_id=await self._target_id(page),prompt_sha256=digest(prompt))
            def mark():
                control()
                pending=self.store.watchdog_rows(owner_thread_id=scope_id,states=('ALLOCATED',))
                if not any(int(row['generation'])==generation for row in pending):
                    raise AgentError('WATCHDOG_INACTIVE','This Watchdog allocation was retired before submission')
                self.store.update_web_operation(oid,'PREPARED','SUBMITTING')
            try:
                await submit_native_once(page,observer,prompt,
                    control_check=control,mark_submitting=mark)
            except BaseException as exc:
                state=self.store.web_operation(oid)['state']
                if state=='SUBMITTING':
                    self.store.update_web_operation(oid,'SUBMITTING','RECONCILING',error_class=getattr(exc,'code',type(exc).__name__))
                raise
            submission=await self._reconcile_web(self.store.web_operation(oid),observer)
            bound=self.store.get(aid)
            if bound.get('conversation_id'):
                self._background(self._rename_web_title(caller,bound,observer.page),'watchdog_title_alignment')
            return {'status':'watchdog_spawned','generation':generation,
                    'agent':self.manager.public_agent(bound),'submission':submission}
        except BaseException:
            # Fail immediately only while no browser submission could have
            # escaped. After SUBMITTING/RECONCILING the child may already exist
            # and still join; the 120s lifecycle timeout handles that ambiguity.
            safe_to_fail = operation is None
            if operation is not None:
                try:
                    safe_to_fail = self.store.web_operation(operation['operation_id'])['state']=='PREPARED'
                except Exception:
                    safe_to_fail = False
            if safe_to_fail:
                try:
                    self.store.set_watchdog_state(generation,'FAILED')
                except Exception:
                    pass
                if aid:
                    try:
                        current=self.store.get(aid)
                        if current.get('state') not in TERMINAL_AGENT_STATES:
                            self.store.set_state(aid,'CLOSED')
                    except Exception:
                        pass
            raise

    def _watchdog_snapshot(self,scope_id):
        now=time.time()
        rows=self.store.watchdog_rows(owner_thread_id=scope_id)
        public_rows=[]
        active=[]
        overdue=[]
        for row in rows:
            item={k:row.get(k) for k in ('generation','state','born_at','retire_at','replacement')}
            if row.get('logical_agent_id'):
                try:
                    item['agent']=self.manager.public_agent(self.store.get(row['logical_agent_id']))
                except AgentError:
                    item['agent']=None
            public_rows.append(item)
            if row.get('state')=='ACTIVE' and float(row.get('born_at') or 0)<=now:
                active.append(row)
                if float(row.get('retire_at') or 0)<=now:
                    overdue.append(int(row['generation']))
        return public_rows,{
            'at':now,'active_count':len(active),'desired_replicas':self.config.watchdog_replicas,
            'active_generations':[int(r['generation']) for r in active],
            'next_retire_at':min((float(r['retire_at']) for r in active),default=None),
            'overdue_generations':overdue,
            'handoff':'new-joins-before-old-retires',
        }

    async def watchdog_start(self,caller,p):
        """Enable one real Watchdog Conversation for this top-level scope."""
        if not self.config.watchdog_enabled:
            raise AgentError('WATCHDOG_DISABLED','Machine configuration disables watchdogs')
        if self.manager.caller_is_child_agent(caller):
            raise AgentError('WATCHDOG_TOP_LEVEL_REQUIRED','Child/Watchdog conversations inherit the top-level scope and cannot create another Watchdog')
        scope_id=self.manager.supervisor_scope_for_caller(caller)
        current=self.store.watchdog_scope(scope_id)
        if current.get('manual_disabled'):
            raise AgentError('WATCHDOG_MANUALLY_DISABLED',
                'The user manually disabled this top-level Watchdog. Only an explicit user toggle may re-enable it.')
        estimated=p.get('estimated_minutes')
        if estimated is not None:
            if isinstance(estimated,bool) or not isinstance(estimated,(int,float)) or estimated<=0 or estimated>7*24*60:
                raise AgentError('WATCHDOG_ESTIMATE_INVALID','estimated_minutes must be a positive duration no longer than 7 days')
            estimated=round(float(estimated),1)
        # A fresh explicit enable begins a new lifecycle episode so failures
        # from a previous disabled episode cannot trip this episode's circuit.
        started_at=current.get('started_at') if current.get('enabled') else time.time()
        state=self.store.set_watchdog_scope(scope_id,enabled=True,manual_disabled=False,mode='conversation',
            started_at=started_at or time.time(),stopped_at=None,
            estimated_minutes=estimated if estimated is not None else current.get('estimated_minutes'),
            source='top-level-conversation',root_lease_id=caller.lease_id)
        tick=await self._supervisor_tick(caller,scope_filter={scope_id})
        watchdogs,schedule=self._watchdog_snapshot(scope_id)
        self.manager._notify()
        return {'status':'started','mode':'conversation','scope':state,'scope_thread_id':scope_id,
                'agent_count':len(self.store.agents_in_supervisor_scope(scope_id)),
                'watchdogs':watchdogs,'schedule':schedule,'lifecycle':tick}

    async def watchdog_stop(self,caller):
        if self.manager.caller_is_child_agent(caller):
            raise AgentError('WATCHDOG_TOP_LEVEL_REQUIRED','Child/Watchdog conversations inherit the top-level scope and cannot stop it')
        scope_id=self.manager.supervisor_scope_for_caller(caller)
        # Explicit watchdog_stop is sticky just like the Console user toggle;
        # automatic lifecycle completion/circuit breaking uses separate paths
        # that do not set manual_disabled.
        state=self.store.set_watchdog_scope(scope_id,enabled=False,manual_disabled=True,mode='conversation',
            stopped_at=time.time(),source='top-level-conversation')
        for row in self.store.watchdog_rows(owner_thread_id=scope_id):
            if row.get('state') in {'ALLOCATED','ACTIVE','RETIRING'}:
                self.store.set_watchdog_state(int(row['generation']),'CLOSED')
        watchdogs,schedule=self._watchdog_snapshot(scope_id)
        self.manager._notify()
        return {'status':'stopped','mode':'conversation','scope':state,'scope_thread_id':scope_id,
                'watchdogs':watchdogs,'schedule':schedule}

    async def _supervisor_tick(self,caller,scope_filter=None):
        """Maintain Watchdog Conversations only; never make business-task decisions.

        Semantic supervision belongs to the real Watchdog GPT. This local loop
        only provides deterministic lifecycle safety: create, overlap, replace,
        retire and fail closed. Generations share durable scope context but never
        a Runtime lease or Conversation.
        """
        scopes=self.store.watchdog_scopes(enabled_only=True)
        if scope_filter is not None:
            scopes=[s for s in scopes if str(s.get('scope_thread_id') or '') in scope_filter]
        if not scopes:
            return {'status':'disabled','mode':'conversation','scope_count':0}
        now=time.time()
        overlap_lead=min(300.0,max(30.0,float(self.config.spawn_interval_minutes)*60.0/6.0))
        started=[]; retiring=[]; failed=[]; errors=[]
        for state in scopes:
            scope_id=str(state.get('scope_thread_id') or '')
            # A persisted ON flag is not a reason to resurrect supervision for
            # an already completed native task after an MCP restart. Keep an
            # orphaned but still-live business tree supervised, however.
            root_lease_id=state.get('root_lease_id')
            if root_lease_id and self.manager.caller_resolver(root_lease_id) is None:
                business=self.store.agents_in_supervisor_scope(scope_id,live_only=True)
                if not any(a.get('state') not in TERMINAL_AGENT_STATES for a in business):
                    self.store.set_watchdog_scope(scope_id,enabled=False,stopped_at=now,
                        source='lifecycle-task-completed')
                    for old in self.store.watchdog_rows(owner_thread_id=scope_id):
                        if old.get('state') in {'ALLOCATED','ACTIVE','RETIRING'}:
                            self.store.set_watchdog_state(int(old['generation']),'CLOSED')
                    continue
            if state.get('mode')!='conversation':
                state=self.store.set_watchdog_scope(scope_id,mode='conversation',source='program-to-conversation-migration')
            # Hard expiry is lifecycle authority only. It never touches business Agents.
            self.store.watchdog_reconcile(now=now,owner_thread_id=scope_id)
            rows=self.store.watchdog_rows(owner_thread_id=scope_id)
            # Failed startup/Watchdog completion must not block replacement forever.
            for row in rows:
                aid=row.get('logical_agent_id')
                agent=None
                if aid:
                    try: agent=self.store.get(aid)
                    except AgentError: agent=None
                if row.get('state')=='ALLOCATED' and now-float(row.get('born_at') or now)>120:
                    self.store.set_watchdog_state(int(row['generation']),'FAILED')
                    if agent and agent.get('state') not in TERMINAL_AGENT_STATES:
                        self.store.set_state(aid,'CLOSED')
                    failed.append({'scope_thread_id':scope_id,'generation':int(row['generation']),'reason':'startup_timeout'})
                elif row.get('state')=='ACTIVE' and (not agent or agent.get('state') in TERMINAL_AGENT_STATES|{'EXTERNAL_ACTIVITY','IDLE','DEAD_RECOVERABLE'} or agent.get('join_state')!='joined'):
                    self.store.set_watchdog_state(int(row['generation']),'FAILED')
                    failed.append({'scope_thread_id':scope_id,'generation':int(row['generation']),'reason':'watchdog_unhealthy'})
            rows=self.store.watchdog_rows(owner_thread_id=scope_id)
            active=[r for r in rows if r.get('state')=='ACTIVE']
            allocated=[r for r in rows if r.get('state')=='ALLOCATED']
            # Once a newer generation has really joined/activated, retire older
            # generations. This is the deliberate short overlap handoff.
            if len(active)>self.config.watchdog_replicas:
                active=sorted(active,key=lambda r:int(r['generation']))
                keep=active[-self.config.watchdog_replicas:]
                replacement=int(keep[-1]['generation'])
                for old in active[:-self.config.watchdog_replicas]:
                    self.store.set_watchdog_state(int(old['generation']),'RETIRING',replacement=replacement)
                    retiring.append({'scope_thread_id':scope_id,'generation':int(old['generation']),'replacement':replacement})
                active=keep
            primary=max(active,key=lambda r:int(r['generation'])) if active else None
            nearing=bool(primary and float(primary['retire_at'])-now<=overlap_lead)
            # Product/browser startup is fallible. Never create a new Watchdog
            # on every lifecycle tick after failures: back off, then fail closed
            # after three consecutive failures in the current enable episode. An explicit
            # later watchdog_start begins a fresh episode and may retry.
            episode_started=float(state.get('started_at') or 0)
            last_healthy_generation=max((int(r['generation']) for r in rows
                if r.get('state') in {'ACTIVE','RETIRING','RETIRED'}),default=0)
            recent_failed=[r for r in rows if r.get('state')=='FAILED'
                and float(r.get('born_at') or 0)>=episode_started
                and int(r['generation'])>last_healthy_generation]
            if not active and not allocated and len(recent_failed)>=3:
                self.store.set_watchdog_scope(scope_id,enabled=False,manual_disabled=False,
                    stopped_at=now,source='watchdog-spawn-circuit-breaker')
                errors.append({'scope_thread_id':scope_id,'reason':'spawn_circuit_breaker',
                    'failed_generations':[int(r['generation']) for r in recent_failed[-3:]]})
                continue
            if not allocated and recent_failed:
                latest=max(recent_failed,key=lambda r:float(r.get('state_changed_at') or r.get('born_at') or 0))
                attempt_count=min(len(recent_failed),4)
                backoff=min(120.0,15.0*(2**(attempt_count-1)))
                retry_after=backoff-(now-float(latest.get('state_changed_at') or latest.get('born_at') or 0))
                if retry_after>0:
                    errors.append({'scope_thread_id':scope_id,'reason':'spawn_backoff',
                        'retry_after_seconds':round(retry_after,1)})
                    continue
            if (not active or nearing) and not allocated:
                try:
                    created=await self._spawn_watchdog(caller,scope_id)
                    started.append({'scope_thread_id':scope_id,'generation':created['generation'],
                                    'agent':created.get('agent'),'status':created.get('status')})
                except Exception as exc:
                    code=getattr(exc,'code',type(exc).__name__)
                    if code not in {'WATCHDOG_INACTIVE','WATCHDOG_ALLOCATION_IN_PROGRESS'}:
                        errors.append({'scope_thread_id':scope_id,'error_class':code})
        if started or retiring or failed:
            self.manager._notify()
        return {'status':'ok','mode':'conversation','scope_count':len(scopes),'started':started,
                'retiring':retiring,'failed':failed,'errors':errors,'overlap_lead_seconds':overlap_lead}
