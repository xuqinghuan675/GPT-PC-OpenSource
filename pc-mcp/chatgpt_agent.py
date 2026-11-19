"""Single GPTAgent facade; browser/SQLite details stay outside MCP server.py."""
from __future__ import annotations
from dataclasses import dataclass
import threading
import time
from typing import Any, Callable
from chatgpt_agent_store import AgentError, AgentStore, TERMINAL_AGENT_STATES, public_snapshot
from machine_config import MachineConfig
from ultra_team_store import TEAM_ACTIONS, TEAM_READ_ACTIONS

COLLAB_WEB_ACTIONS = frozenset({
    'ensure_project','spawn','join','followup','wake','refresh_turn',
    'recover_member','web_read','web_reconcile',
})


READ_ACTIONS = frozenset({'list','inspect','inbox','wait','resume_context','history_search','history_get','watchdog_status','ultra_status'})
ACTIONS = READ_ACTIONS | {'ensure_project','spawn','join','attach','message','followup','wake','refresh_turn','recover_member','checkpoint','complete','interrupt','close','watchdog_start','watchdog_stop','ultra_on'}
READ_ACTIONS = READ_ACTIONS | TEAM_READ_ACTIONS
ACTIONS = ACTIONS | TEAM_ACTIONS | {'web_read','web_reconcile'}
SELF_ACTIONS = frozenset({'checkpoint','complete','resume_context'})
BROWSER_ACTIONS = frozenset({'ensure_project','spawn','join','attach','followup','wake','refresh_turn','recover_member','interrupt'})


def is_read_action(action: str, arguments: dict | None = None) -> bool:
    return action in READ_ACTIONS and not (action in {'inbox','team_inbox'} and (arguments or {}).get('ack_ids'))


@dataclass(frozen=True)
class Caller:
    lease_id: str
    thread_id: str
    workspace: str
    mode: str
    lease_type: str
    lease_role: str = 'owner'
    shared_root_lease_id: str = ''
    controller_id: str = ''
    logical_agent_id: str = ''
    session_generation: int = 0

    @classmethod
    def from_lease(cls, lease: dict | None) -> 'Caller':
        if not lease or not lease.get('lease_id') or not lease.get('thread_id'):
            raise AgentError('CALLER_IDENTITY_REQUIRED', 'An active Runtime lease with a thread identity is required')
        return cls(
            str(lease['lease_id']), str(lease['thread_id']), str(lease.get('workspace') or ''),
            str(lease.get('mode') or ''), str(lease.get('lease_type') or ''),
            str(lease.get('lease_role') or 'owner'), str(lease.get('shared_root_lease_id') or ''),
            str(lease.get('controller_id') or ''), str(lease.get('logical_agent_id') or ''),
            int(lease.get('session_generation') or 0),
        )


class AgentManager:
    def __init__(self, config: MachineConfig, *, caller_resolver: Callable[[str],dict | None],
                 control_check: Callable[[str],None], runtime_facts: Callable[[str],dict], store: AgentStore | None = None,
                 browser_factory: Callable | None = None, ultra_setter: Callable[...,dict] | None = None,
                 participant_attacher: Callable[...,dict] | None = None,
                 participant_rotator: Callable[...,dict] | None = None,
                 participant_releaser: Callable[[str],dict] | None = None,
                 terminal_participant_releaser: Callable[[str],dict] | None = None,
                 root_promoter: Callable[...,dict] | None = None,
                 root_active_tool_count: Callable[[str],int] | None = None):
        self.config = config
        self.store = store or AgentStore(config)
        self.caller_resolver, self.control_check, self.runtime_facts = caller_resolver, control_check, runtime_facts
        self._condition = threading.Condition()
        self._web = None
        self._web_init_lock = threading.Lock()
        self.browser_factory = browser_factory
        self.ultra_setter = ultra_setter
        self.participant_attacher = participant_attacher
        self.participant_rotator = participant_rotator
        self.participant_releaser = participant_releaser
        self.terminal_participant_releaser = terminal_participant_releaser
        self.root_promoter = root_promoter
        self.root_active_tool_count = root_active_tool_count
        self._ultra_coordinator = None
        self._ultra_init_lock = threading.Lock()

    def _get_ultra_coordinator(self):
        with self._ultra_init_lock:
            if self._ultra_coordinator is None:
                from ultra_collaboration import UltraCoordinator
                self._ultra_coordinator = UltraCoordinator(self)
            return self._ultra_coordinator

    @staticmethod
    def public_agent(agent: dict) -> dict:
        fields = {'logical_agent_id','short_id','agent_path','parent_agent_id','root_agent_id','display_name','web_title','role','kind',
                  'project_name','project_id','conversation_id','conversation_url','workspace','join_state','state','turn_epoch',
                  'requested_model','effective_model','requested_reasoning_effort','effective_reasoning_effort','pending_work',
                  'progress_version','no_progress_turns','consecutive_recoveries','created_at','updated_at','completed_at','completion_summary'}
        result = {k:v for k,v in agent.items() if k in fields}
        result['conversation_id_suffix'] = str(agent.get('conversation_id') or '')[-8:]
        return result

    def _is_controller(self, caller: Caller, agent: dict) -> bool:
        root = self.store.get(agent['root_agent_id'])
        # Sharing an administrative resource lease does not grant a Child
        # global Agent-control authority or permission to close its parent.
        admin_owner = caller.lease_type == 'runtime_admin' and caller.lease_role != 'participant'
        return admin_owner or root['owner_thread_id'] == caller.thread_id

    def _authorize(self, caller: Caller, target: dict, action: str) -> dict | None:
        own = self.store.by_thread(caller.thread_id)
        if action in SELF_ACTIONS:
            if not own or own['logical_agent_id'] != target['logical_agent_id']:
                raise AgentError('SELF_ACTION_REQUIRED', 'Only the joined conversation itself can checkpoint, complete or consume its inbox')
            return own
        if (not own or own.get('kind') != 'watchdog') and self._is_controller(caller,target):
            return own
        if not own or own['project_id'] != target['project_id']:
            raise AgentError('AGENT_SCOPE_FORBIDDEN', 'Caller is not bound to this GPTAgent project/tree')
        if own['kind'] == 'watchdog':
            own_scope=self.store.agent_supervisor_scope(own['logical_agent_id'])
            target_scope=self.store.agent_supervisor_scope(target['logical_agent_id'])
            if own_scope != target_scope:
                raise AgentError('WATCHDOG_SCOPE_FORBIDDEN','Watchdog may only observe or recover business agents in its own top-level scope')
            if target['kind'] == 'watchdog' and target['logical_agent_id'] != own['logical_agent_id']:
                raise AgentError('WATCHDOG_SCOPE_FORBIDDEN','Watchdogs do not supervise peer Watchdogs')
        elif own['root_agent_id'] != target['root_agent_id']:
            raise AgentError('AGENT_TREE_FORBIDDEN','Business agents may only read or control their own top-level Agent tree')
        if is_read_action(action):
            return own
        if own['kind'] == 'watchdog':
            if action not in {'followup','message'} or target['kind'] == 'watchdog':
                raise AgentError('WATCHDOG_SCOPE_FORBIDDEN', 'Watchdogs may inspect business agents and request guarded recovery, not mutate their tasks')
            state = self.store.watchdog_scope(own_scope)
            active = self.store.watchdog_rows(owner_thread_id=own_scope, states=('ACTIVE',))
            if (not state.get('enabled') or state.get('manual_disabled')
                    or not any(row.get('logical_agent_id') == own['logical_agent_id'] for row in active)):
                raise AgentError('WATCHDOG_INACTIVE','A stopped or retiring Watchdog may finish its own task but cannot recover business conversations')
            return own
        if own['root_agent_id'] != target['root_agent_id']:
            raise AgentError('AGENT_TREE_FORBIDDEN', 'Cross-tree mutations are forbidden')
        if action == 'message':
            return own
        if target['agent_path'] == own['agent_path'] or target['agent_path'].startswith(own['agent_path']+'/'):
            return own
        raise AgentError('AGENT_TREE_FORBIDDEN', 'An agent may not control its parent or sibling')

    def _target(self, caller: Caller, ref: str | None) -> dict:
        if ref:
            return self.store.get(ref)
        own = self.store.by_thread(caller.thread_id)
        if own:
            return own
        raise AgentError('AGENT_REF_REQUIRED', 'Specify an exact agent_ref; this Runtime thread has not joined an agent')

    def _notify(self) -> None:
        with self._condition:
            self._condition.notify_all()

    def supervisor_scope_for_caller(self, caller: Caller) -> str:
        """Return the top-level Runtime conversation that owns this Agent tree."""
        own = self.store.by_thread(caller.thread_id)
        if own:
            return self.store.agent_supervisor_scope(own['logical_agent_id'])
        return caller.thread_id

    def caller_is_child_agent(self, caller: Caller) -> bool:
        own = self.store.by_thread(caller.thread_id)
        return bool(caller.lease_role == 'participant' or
                    (own and (own.get('parent_agent_id') or own.get('kind') == 'watchdog'
                              or own.get('owner_thread_id') != caller.thread_id)))

    def ultra_utilization(self, caller: Caller, *, successful_actions: int = 0, goal_active: bool = False) -> dict:
        """Report whether Ultra is creating real parallel workers, not just an ON flag."""
        scope = self.supervisor_scope_for_caller(caller)
        rows = self.store.agents_in_supervisor_scope(scope, limit=self.config.max_live_agents, live_only=True)
        own = self.store.by_thread(caller.thread_id)
        if own and own.get('kind') == 'business':
            # A GPTAgent calling from its own participant is the coordinator,
            # not one of its parallel workers.  Count only descendants of that
            # logical Agent so a parent/sibling elsewhere in the same supervisor
            # scope cannot make this caller look parallel when it is actually
            # executing serially.
            prefix = own['agent_path'].rstrip('/') + '/'
            rows = [a for a in rows if a.get('logical_agent_id') != own['logical_agent_id']
                    and str(a.get('agent_path') or '').startswith(prefix)]
        live = [a for a in rows if a.get('kind') == 'business' and a.get('state') not in TERMINAL_AGENT_STATES]
        joined = [a for a in live if a.get('join_state') == 'joined' and a.get('state') == 'RUNNING']
        inactive = [a for a in live if a.get('join_state') == 'joined' and a.get('state') != 'RUNNING']

        # CREATED / join-pending rows are not real parallel work. Only a native
        # submit that has crossed PREPARED into SUBMITTING/RECONCILING consumes
        # a temporary launch slot. A failed/stale PREPARED row must not make
        # Ultra look healthy or permanently consume capacity.
        launching = []
        stalled_unjoined = []
        for agent in live:
            if agent.get('join_state') == 'joined':
                continue
            operations = self.store.pending_web_operations(agent.get('logical_agent_id'))
            active_submit = any(op.get('state') in {'SUBMITTING', 'RECONCILING'} for op in operations)
            (launching if active_submit else stalled_unjoined).append(agent)

        threshold = 4
        parallel_limit = self.config.max_parallel_children_per_agent
        direct_parent = own['logical_agent_id'] if own and own.get('kind') == 'business' else None
        joined_direct = [a for a in joined if a.get('parent_agent_id') == direct_parent]
        launching_direct = [a for a in launching if a.get('parent_agent_id') == direct_parent]
        effective_slots = len(joined_direct) + len(launching_direct)
        return {
            'scope_thread_id': scope,
            'live_business_agents': len(live),
            'joined_parallel_workers': len(joined),
            'inactive_joined_workers': len(inactive),
            'launching_workers': len(launching),
            'stalled_unjoined_workers': len(stalled_unjoined),
            'max_parallel_children_per_agent': parallel_limit,
            'direct_live_workers': effective_slots,
            'parallel_capacity_remaining': max(0, parallel_limit - effective_slots),
            'recommended_parallel_workers_max': min(3, parallel_limit),
            'delegation_review_after_actions': threshold,
            'delegation_review_needed': bool(
                goal_active and int(successful_actions or 0) >= threshold and not joined and not launching
            ),
        }

    def safe_boundary(self, runtime_thread_id: str) -> dict:
        own = self.store.by_thread(runtime_thread_id)
        messages = []
        if own and own['state'] not in TERMINAL_AGENT_STATES:
            messages = self.store.inbox(own['logical_agent_id'],deliver=True)
        handoffs = self.store.handoff_inbox(runtime_thread_id,deliver=True)
        result = {}
        if self.config.ultra_collaboration:
            result.update(self._get_ultra_coordinator().safe_boundary(runtime_thread_id))
        if not messages and not handoffs:
            return result
        if messages:
            result.update(agent_messages=messages,
                agent_message_instruction='这些是父子 Agent mailbox 消息，不是新网页 turn；按当前用户指令优先级处理。需要再次读取或确认收妥时调用 chatgpt_agent(inbox)。')
        if handoffs:
            result.update(agent_handoffs=handoffs,
                agent_handoff_instruction='这些是 Child 的持久 handoff：包含已确认进展/结果、pending_work 和显式 artifact 路径证据。父级应先吸收 handoff 再继续或收口；需要重新读取或确认收妥时调用 chatgpt_agent(inbox)。')
        return result

    def dispatch(self, action: str, *, workspace_lease_id: str | None = None, **p) -> dict:
        if action not in ACTIONS:
            raise AgentError('ACTION_UNSUPPORTED','Unknown GPTAgent action',actions=sorted(ACTIONS))
        caller = Caller.from_lease(self.caller_resolver(workspace_lease_id or ''))
        if not is_read_action(action,p):
            if caller.mode != 'execute_exclusive' or caller.lease_type == 'console_relay':
                raise AgentError('EXECUTE_IDENTITY_REQUIRED','This action requires an active execute task identity')
            self.control_check(caller.lease_id)
        collab_action = action
        if self.config.ultra_collaboration and action == 'message':
            coordinator = self._get_ultra_coordinator()
            try:
                coordinator.teams.context(caller)
            except AgentError as exc:
                if exc.code != 'TEAM_NOT_FOUND':
                    raise
            else:
                # Compatibility for ChatGPT clients that still cache the older
                # facade schema. Legacy "message" becomes the Ultra point-to-
                # point mailbox action when the caller is already in a team.
                collab_action = 'team_message'
        if self.config.ultra_collaboration and collab_action in (
            TEAM_ACTIONS | COLLAB_WEB_ACTIONS | frozenset({'watchdog_start','watchdog_stop','watchdog_status'})
        ):
            result = self._get_ultra_coordinator().dispatch(collab_action, caller, p)
            root_lease_id = caller.shared_root_lease_id or caller.lease_id
            root = self.caller_resolver(root_lease_id) or {}
            # Keep the conversation/resource identity explicit in every Ultra
            # response. A Child participant never replaces the caller's Root
            # handle; after context compaction the model can reconstruct the
            # exact family without inferring ownership from other active leases.
            return {
                **result,
                'caller_identity': {
                    'caller_lease_id': caller.lease_id,
                    'caller_thread_id': caller.thread_id,
                    'caller_role': 'participant' if caller.lease_role == 'participant' else 'root',
                    'shared_root_lease_id': root_lease_id,
                    'shared_root_thread_id': str(root.get('thread_id') or ''),
                    'root_resource_still_owned_by_root': True,
                },
            }
        if action in TEAM_ACTIONS | {'web_read','web_reconcile'}:
            raise AgentError('ULTRA_COLLABORATION_DISABLED', 'This machine has not enabled the explicit collaboration component')
        if action in BROWSER_ACTIONS:
            if action == 'spawn' and not (self.runtime_facts(caller.lease_id).get('ultra_mode') or {}).get('active'):
                raise AgentError('ULTRA_REQUIRED', '用户尚未允许当前任务使用并行子对话。')
            return self._manual_browser_guide(caller, action, p)
        if action in {'watchdog_start','watchdog_stop','watchdog_status'}:
            return self._manual_watchdog(caller, action, p)
        if action == 'ultra_status':
            facts=self.runtime_facts(caller.lease_id)
            return {
                'status':'ok',
                'ultra_mode':facts.get('ultra_mode') or {'active':False,'status':'off'},
                'collaboration_component':bool(self.config.ultra_collaboration),
                'team_actions':sorted(TEAM_ACTIONS),
                'web_actions':sorted(COLLAB_WEB_ACTIONS),
                'work_policy':{
                    'bounded_max_minutes':100,
                    'continuous_refresh_minutes':45,
                    'continuous_self_refresh_only':True,
                    'identity_fields':['agent_id','local_tag','project_id','conversation_id'],
                    'title_is_identity':False,
                },
                'watchdog':{
                    'status':'available' if self.config.watchdog_enabled else 'frozen',
                    'enabled_by_machine_policy':bool(self.config.watchdog_enabled),
                    'automatic_supervision':False,
                },
                'limits':{'max_depth':self.config.max_depth,'max_children_per_agent':self.config.max_children_per_agent,
                          'max_parallel_children_per_agent':self.config.max_parallel_children_per_agent,
                          'max_live_agents':self.config.max_live_agents},
            }
        if action == 'ultra_on':
            if self.ultra_setter is None:
                raise AgentError('ULTRA_COMPONENT_NOT_READY','Ultra Runtime policy setter is unavailable')
            own=self.store.by_thread(caller.thread_id)
            actor='child' if own and own.get('parent_agent_id') else 'agent'
            result=self.ultra_setter(caller.lease_id,enabled=True,user_explicit=False,actor=actor)
            if self.config.ultra_collaboration:
                return {'status':'enabled','ultra_mode':result.get('ultra_mode'),
                        'instruction':'Ultra 已允许显式 GPTAgent Team：spawn 必须先估算 estimated_minutes；bounded 最多100分钟，超出只能显式 continuous。continuous 只能由 Child 自己每至少45分钟 refresh_turn 到原 conversation，Root/兄弟不能代续。身份使用 agent_id/local_tag/project_id/conversation_id，标题仅展示。team_message/team_broadcast 只入 durable inbox；resume_context 会带回 Team 成员、任务与未 ACK 消息。Watchdog 当前冻结。',
                        'collaboration_component':True,'automatic_supervision':False,
                        'watchdog_frozen':not self.config.watchdog_enabled}
            return {'status':'enabled','ultra_mode':result.get('ultra_mode'),
                    'instruction':(
                        'Ultra 已允许使用网页子对话。先阅读 docs/WEB_GPT_SOFT_ORCHESTRATION.md；'
                        '由父 GPT 自己开页面、查看当前控件、填写并点击发送 1–3 个独立子任务，优先复用已有对话。'
                        '先建立绑定主网页对话的唯一任务关系记录，再约定互不冲突的文件范围、阶段交接和产物；后台不会自动创建、催促或换代。'
                        '开关只代表允许使用，是否真正工作必须检查页面与产物。'
                    )}
        if action == 'list':
            own = self.store.by_thread(caller.thread_id)
            parent = self.store.get(p['parent_agent_id']) if p.get('parent_agent_id') else None
            if own and own.get('kind') == 'watchdog':
                scope=self.store.agent_supervisor_scope(own['logical_agent_id'])
                rows=self.store.agents_in_supervisor_scope(
                    scope,parent=parent['logical_agent_id'] if parent else None,
                    path_prefix=p.get('path_prefix'),state=p.get('state'),query=p.get('query'),
                    limit=p.get('limit',50),offset=p.get('offset',0))
                visible=rows
            elif own and own.get('kind') == 'business':
                rows=self.store.agents(
                    root_agent_id=own['root_agent_id'],parent=parent['logical_agent_id'] if parent else None,
                    path_prefix=p.get('path_prefix'),state=p.get('state'),query=p.get('query'),
                    limit=p.get('limit',50),offset=p.get('offset',0))
                visible=rows
            elif caller.lease_type != 'runtime_admin' or caller.lease_role == 'participant':
                rows=self.store.agents_in_supervisor_scope(
                    caller.thread_id,parent=parent['logical_agent_id'] if parent else None,
                    path_prefix=p.get('path_prefix'),state=p.get('state'),query=p.get('query'),
                    limit=p.get('limit',50),offset=p.get('offset',0))
                visible=rows
            else:
                rows = self.store.agents(parent=parent['logical_agent_id'] if parent else None,
                    path_prefix=p.get('path_prefix'),state=p.get('state'),query=p.get('query'),limit=p.get('limit',50),offset=p.get('offset',0))
                visible = rows
            return {'status':'ok','agents':[self.public_agent(a) for a in visible],'count':len(visible),'next_offset':p.get('offset',0)+len(rows)}
        if action == 'inbox':
            own = self.store.by_thread(caller.thread_id)
            messages = self.store.inbox(own['logical_agent_id'],ack_ids=p.get('ack_ids')) if own else []
            handoffs = self.store.handoff_inbox(caller.thread_id,ack_ids=p.get('ack_ids'))
            return {'status':'ok','messages':messages,'handoffs':handoffs}
        target = self._target(caller,p.get('agent_ref'))
        own = self._authorize(caller,target,action)
        aid = target['logical_agent_id']
        if action == 'inspect':
            return {'status':'ok','agent':self.public_agent(target),'turn':self.store.latest_turn(aid),
                    'checkpoint':self.store.latest_checkpoint(aid),'handoff':self.store.latest_handoff(aid),
                    'children':[self.public_agent(a) for a in self.store.agents(parent=aid)],
                    'child_handoffs':self.store.child_handoffs(aid)}
        if action == 'history_search':
            requested_scope=p.get('scope') or 'project'
            if own and own.get('kind')=='watchdog':
                history_scope='supervisor'
            elif caller.lease_type == 'runtime_admin' and caller.lease_role != 'participant':
                history_scope=requested_scope
            else:
                if requested_scope == 'project':
                    requested_scope='root'
                if requested_scope not in {'self','lineage','root'}:
                    raise AgentError('HISTORY_SCOPE_FORBIDDEN','History is limited to self, lineage, or the caller top-level Agent tree')
                history_scope=requested_scope
            rows = self.store.history_search(aid,p.get('query') or '',scope=history_scope,limit=p.get('limit',8))
            return {'status':'ok','results':rows,'count':len(rows)}
        if action == 'history_get':
            requested_scope=p.get('scope') or 'project'
            if own and own.get('kind')=='watchdog':
                history_scope='supervisor'
            elif caller.lease_type == 'runtime_admin' and caller.lease_role != 'participant':
                history_scope=requested_scope
            else:
                if requested_scope == 'project':
                    requested_scope='root'
                if requested_scope not in {'self','lineage','root'}:
                    raise AgentError('HISTORY_SCOPE_FORBIDDEN','History is limited to self, lineage, or the caller top-level Agent tree')
                history_scope=requested_scope
            return {'status':'ok','source':self.store.history_get(aid,p.get('source_handle') or '',scope=history_scope,start=p.get('offset',0),length=p.get('length',2000))}
        if action == 'checkpoint':
            facts = self.runtime_facts(caller.lease_id)
            checkpoint = self.store.checkpoint(aid,p.get('summary') or '',lease_id=caller.lease_id,
                runtime_snapshot=facts.get('execution_checkpoint',{}),goal_snapshot=facts.get('goal_mode',{}),
                pending_work=p.get('pending_work'),artifacts=p.get('artifacts'))
            self._notify()
            return {'status':'checkpointed','checkpoint':checkpoint,'handoff':self.store.latest_handoff(aid)}
        if action == 'complete':
            if self.config.ultra_collaboration and self._get_ultra_coordinator().teams.member(aid):
                self._get_ultra_coordinator().check_completion(caller)
            result = self.store.complete(aid,p.get('summary') or '')
            if result.get('kind') == 'watchdog':
                scope = self.store.agent_supervisor_scope(aid)
                rows = self.store.watchdog_rows(owner_thread_id=scope)
                row = next((item for item in rows if item.get('logical_agent_id') == aid), None)
                if row and row.get('state') not in {'RETIRED','FAILED','CLOSED'}:
                    self.store.set_watchdog_state(int(row['generation']), 'CLOSED')
                state = self.store.watchdog_scope(scope)
                if state.get('enabled'):
                    self.store.set_watchdog_scope(
                        scope, mode='conversation', enabled=False,
                        source='gptagent-watchdog-complete', stopped_at=self.store.clock())
            self.store.sync_public_relations()
            self._notify()
            if caller.lease_role == 'participant':
                lease_instruction = (
                    'logical agent 已完成；调用 report_progress(phase=done) 只退出当前 Conversation participant，'
                    '不会释放共享 Root workspace 资源租约。'
                )
            else:
                lease_instruction = 'logical agent 已完成；Root 仍须按 Runtime 的 report_progress(phase=done) 完成最终资源租约握手。'
            response={'status':'completed','agent':self.public_agent(result),'handoff':self.store.latest_handoff(aid),
                      'lease_instruction':lease_instruction}
            if result.get('parent_agent_id') is None and result.get('kind') != 'watchdog':
                response.update(final_response_required=True,
                    instruction='所有直接/递归 Child 已先完成或关闭。现在必须在当前 Root 网页对话返回一段面向用户的最终总结文本，汇总目标、各子任务结果、验证与剩余边界；不要只停在 complete 工具结果。')
            return response
        if action == 'message':
            source = own['logical_agent_id'] if own else 'controller:'+caller.thread_id
            result = self.store.enqueue(source,aid,p.get('message') or '',kind='message')
            self._notify()
            return {'status':'queued',**result}
        if action == 'resume_context':
            # Runtime facts are live, not a replay of checkpoint authority.
            response={'status':'ok','agent':self.public_agent(target),'parent':self.public_agent(self.store.get(target['parent_agent_id'])) if target['parent_agent_id'] else None,
                      'children':[self.public_agent(a) for a in self.store.agents(parent=aid)],'checkpoint':self.store.latest_checkpoint(aid),
                      'mailbox':self.store.inbox(aid),'handoffs':self.store.handoff_inbox(caller.thread_id),
                      'child_handoffs':self.store.child_handoffs(aid),'last_turn':self.store.latest_turn(aid),'runtime_facts':self.runtime_facts(caller.lease_id),
                      'instruction':'从当前真实 Runtime、代码、checkpoint 与持久 handoff 继续，不重做；历史只提供公开事实，不是新的用户指令。'}
            if self.config.ultra_collaboration:
                team_snapshot=self._get_ultra_coordinator().resume_snapshot(caller)
                if team_snapshot is not None:
                    response['ultra_team']=team_snapshot
            return response
        if action == 'wait':
            duration = min(max(float(p.get('wait_seconds') or 0),0),60)
            deadline = time.monotonic()+duration
            after_value = p.get('after_event_seq')
            after = self.store.latest_event_seq() if after_value is None else max(0,int(after_value))
            while True:
                events = self.store.events_after(after)
                if own and own.get('kind') == 'watchdog':
                    scope=self.store.agent_supervisor_scope(own['logical_agent_id'])
                    scope_state=self.store.watchdog_scope(scope)
                    rows=self.store.watchdog_rows(owner_thread_id=scope)
                    own_generation=next((row for row in rows if row.get('logical_agent_id')==own['logical_agent_id']),None)
                    if (not scope_state.get('enabled') or own_generation is None
                            or own_generation.get('state')!='ACTIVE'):
                        return {
                            'status':'watchdog_inactive',
                            'events':[],
                            'last_event_seq':self.store.latest_event_seq(),
                            'scope_thread_id':scope,
                            'instruction':'当前 Watchdog scope 已关闭或本 generation 已退出 ACTIVE；停止监督并按生命周期收尾释放租约。',
                        }
                    events=[e for e in events if e['agent_id'] is not None
                            and self.store.get(e['agent_id']).get('kind')=='business'
                            and self.store.agent_supervisor_scope(e['agent_id'])==scope]
                else:
                    def visible_event(event):
                        if event['agent_id'] is None:
                            return False
                        agent=self.store.get(event['agent_id'])
                        if agent['logical_agent_id']==aid or self._is_controller(caller,agent):
                            return True
                        return bool(own and own.get('kind')=='business'
                                    and own['root_agent_id']==agent['root_agent_id'])
                    events=[e for e in events if visible_event(e)]
                if events or time.monotonic() >= deadline:
                    return {'status':'events' if events else 'timeout','events':events,'last_event_seq':self.store.latest_event_seq()}
                self.control_check(caller.lease_id)
                with self._condition:
                    self._condition.wait(min(0.5,max(0,deadline-time.monotonic())))
        return self._browser_dispatch(action,caller,{**p,'agent_ref':aid})

    def _browser_dispatch(self, action: str, caller: Caller, parameters: dict) -> dict:
        # The web GPT chooses each page action. No semantic operation submits
        # messages, launches a supervisor, or guesses the next browser step.
        return self._manual_browser_guide(caller, action, parameters)

    def _manual_browser_guide(self, caller: Caller, action: str, parameters: dict) -> dict:
        return {
            'status': 'legacy_flow_disabled',
            'mode': 'legacy-readonly',
            'action': action,
            'workspace': caller.workspace,
            'task': public_snapshot(parameters, {'name','role','message','delegation_reason','conversation_url'}),
            'instruction': (
                '旧手工浏览器编排已经退出生产主路径；此兼容分支不会打开页面、点击、填写或发送任何内容。'
                '在启用 chatgpt.ultra_collaboration 的机器上使用 GPTAgent Ultra 显式协作；'
                '若协作组件未启用，则保持单对话工作，不得自行回退到旧手工浏览器编排。'
            ),
        }

    def _manual_watchdog(self, caller: Caller, action: str, parameters: dict) -> dict:
        if not self.config.watchdog_enabled:
            if action == 'watchdog_status':
                return {
                    'status':'frozen', 'mode':'frozen', 'automatic_supervision':False,
                    'instruction':'Watchdog 已按机器策略冻结；不会创建、唤醒、轮换或监督 Ultra Team。',
                }
            raise AgentError('WATCHDOG_FROZEN', 'Watchdog 已按用户要求冻结，等待后续重新设计。')
        scope = self.supervisor_scope_for_caller(caller)
        current = self.store.watchdog_scope(scope)
        if action != 'watchdog_status':
            if self.caller_is_child_agent(caller):
                raise AgentError('WATCHDOG_TOP_LEVEL_REQUIRED', '由顶层网页对话管理监督流程，子对话继承现有分工。')
            enabled = action == 'watchdog_start'
            if enabled and current.get('manual_disabled'):
                raise AgentError('WATCHDOG_MANUALLY_DISABLED', '用户已关闭监督流程，只有用户明确重新开启后才能继续。')
            current = self.store.set_watchdog_scope(
                scope, mode='manual', enabled=False, manual_enabled=enabled, manual_disabled=not enabled,
                source='web-gpt-manual', started_at=(current.get('started_at') if current.get('manual_enabled') else time.time()) if enabled else current.get('started_at'),
                stopped_at=None if enabled else time.time(),
            )
        return {
            'status': 'manual', 'mode': 'manual', 'scope': {**current, 'mode': 'manual'},
            'scope_thread_id': scope, 'automatic_supervision': False,
            'instruction': (
                '此开关只表示允许使用监督流程，不表示看门狗已经启动或持续在线。'
                '按软编排规则，预计任务超过 104 分钟时由主 GPT 自己打开监督对话。优先检查已工作超过 50 分钟的业务对话。'
                '看门狗实际接任满 40 分钟后自己准备接替者；接替者确认最新交接并实际检查全部对象后，旧代才退出。'
                '正常工作时只读，不发送探测或催促；异常恢复必须核对实际页面和持久交接，同一故障不重复发送。'
                '提醒、继续和换代也由 GPT 在核对当前页面与持久交接后明确操作；后台程序不会自动发送。'
                '关闭时请将停止监督的要求发到已经打开的监督对话，并保存其交接。'
            ),
        }

    def start_background_services(self) -> dict:
        """Soft orchestration has no background browser/model producer."""
        for scope in self.store.watchdog_scopes():
            if scope.get('enabled'):
                self.store.set_watchdog_scope(scope['scope_thread_id'], mode='manual', enabled=False,
                    manual_enabled=not scope.get('manual_disabled'), source='manual-flow-migration')
        return {'status': 'manual', 'automatic_supervision': False}


    def stop_background_services(self) -> dict:
        with self._web_init_lock:
            web=self._web
            self._web=None
        if web is None:
            return {'status':'not_started'}
        return web.stop_background()
