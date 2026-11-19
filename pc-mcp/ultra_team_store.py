"""Durable Ultra collaboration. Shares AgentStore's DB, never owns a resource lease.

No browser/network I/O belongs in a transaction. A mailbox delivery is data,
not a model turn, and a task claim is coordination, not an OS sandbox.
"""
from __future__ import annotations

import json
from pathlib import Path
import secrets
import uuid
from typing import Any

from chatgpt_agent_store import AgentError, canonical_json, digest, public_text

PROJECT_NAME = 'GPTAgent'
TEAM_ACTIONS = frozenset({'team_status', 'team_message', 'team_broadcast', 'team_inbox', 'task_create',
                          'task_list', 'task_claim', 'task_update', 'team_shutdown', 'team_finalize',
                          'team_root_rescue'})
TEAM_READ_ACTIONS = frozenset({'team_status', 'team_inbox', 'task_list'})
_SCHEMA = '''
CREATE TABLE IF NOT EXISTS ultra_teams(
 team_id TEXT PRIMARY KEY, root_lease_id TEXT UNIQUE NOT NULL,
 root_thread_id TEXT NOT NULL, workspace TEXT NOT NULL,
 project_id TEXT NOT NULL, account_tag TEXT NOT NULL,
 state TEXT NOT NULL DEFAULT 'active', created_at REAL NOT NULL);
CREATE TABLE IF NOT EXISTS ultra_members(
 agent_id TEXT PRIMARY KEY REFERENCES agents(logical_agent_id),
 team_id TEXT NOT NULL REFERENCES ultra_teams(team_id),
 parent_actor TEXT NOT NULL, participant_id TEXT UNIQUE, thread_id TEXT UNIQUE,
 write_paths_json TEXT NOT NULL DEFAULT '[]', created_at REAL NOT NULL);
CREATE INDEX IF NOT EXISTS ultra_member_team_idx ON ultra_members(team_id);
CREATE TABLE IF NOT EXISTS ultra_work_policy(
 agent_id TEXT PRIMARY KEY REFERENCES agents(logical_agent_id),
 team_id TEXT NOT NULL REFERENCES ultra_teams(team_id),
 work_mode TEXT NOT NULL CHECK(work_mode IN ('bounded','continuous')),
 estimated_minutes REAL NOT NULL,
 refresh_interval_minutes REAL NOT NULL DEFAULT 45,
 created_at REAL NOT NULL, updated_at REAL NOT NULL);
CREATE INDEX IF NOT EXISTS ultra_work_policy_team_idx ON ultra_work_policy(team_id,work_mode);
CREATE TABLE IF NOT EXISTS ultra_conversation_bindings(
 agent_id TEXT PRIMARY KEY REFERENCES agents(logical_agent_id),
 team_id TEXT NOT NULL REFERENCES ultra_teams(team_id),
 local_tag TEXT UNIQUE NOT NULL, project_id TEXT NOT NULL,
 conversation_id TEXT UNIQUE, conversation_url TEXT UNIQUE,
 created_at REAL NOT NULL, bound_at REAL, verified_at REAL, updated_at REAL NOT NULL);
CREATE INDEX IF NOT EXISTS ultra_binding_team_idx ON ultra_conversation_bindings(team_id,local_tag);
CREATE TABLE IF NOT EXISTS ultra_session_heads(
 agent_id TEXT PRIMARY KEY REFERENCES agents(logical_agent_id),
 team_id TEXT NOT NULL REFERENCES ultra_teams(team_id),
 current_generation INTEGER NOT NULL DEFAULT 1,
 updated_at REAL NOT NULL);
CREATE TABLE IF NOT EXISTS ultra_sessions(
 agent_id TEXT NOT NULL REFERENCES agents(logical_agent_id),
 team_id TEXT NOT NULL REFERENCES ultra_teams(team_id),
 generation INTEGER NOT NULL,
 project_id TEXT NOT NULL,
 conversation_id TEXT UNIQUE, conversation_url TEXT UNIQUE,
 state TEXT NOT NULL CHECK(state IN ('allocating','active','retired','unusable')),
 reason TEXT NOT NULL DEFAULT '',
 created_at REAL NOT NULL, verified_at REAL, retired_at REAL,
 PRIMARY KEY(agent_id,generation));
CREATE INDEX IF NOT EXISTS ultra_session_team_idx ON ultra_sessions(team_id,agent_id,generation);
CREATE TABLE IF NOT EXISTS ultra_mail(
 message_id TEXT PRIMARY KEY, team_id TEXT NOT NULL REFERENCES ultra_teams(team_id),
 from_actor TEXT NOT NULL, to_actor TEXT NOT NULL, kind TEXT NOT NULL,
 content TEXT NOT NULL, reply_to TEXT, request_key TEXT NOT NULL, body_hash TEXT NOT NULL,
 created_at REAL NOT NULL, delivered_at REAL, acked_at REAL,
 UNIQUE(team_id,from_actor,request_key));
CREATE INDEX IF NOT EXISTS ultra_mail_inbox_idx ON ultra_mail(team_id,to_actor,acked_at);
CREATE TABLE IF NOT EXISTS ultra_tasks(
 task_id TEXT PRIMARY KEY, team_id TEXT NOT NULL REFERENCES ultra_teams(team_id),
 title TEXT NOT NULL, description TEXT NOT NULL, creator TEXT NOT NULL,
 owner TEXT, status TEXT NOT NULL DEFAULT 'pending', version INTEGER NOT NULL DEFAULT 1,
 depends_json TEXT NOT NULL, write_paths_json TEXT NOT NULL,
 evidence TEXT NOT NULL DEFAULT '', artifacts_json TEXT NOT NULL DEFAULT '[]',
 request_key TEXT NOT NULL, body_hash TEXT NOT NULL, created_at REAL NOT NULL, updated_at REAL NOT NULL,
 UNIQUE(team_id,creator,request_key));
CREATE TABLE IF NOT EXISTS ultra_sends(
 operation_id TEXT PRIMARY KEY REFERENCES web_operations(operation_id),
 team_id TEXT NOT NULL REFERENCES ultra_teams(team_id), actor TEXT NOT NULL,
 message_id TEXT UNIQUE NOT NULL, request_hash TEXT NOT NULL, prompt TEXT NOT NULL, model TEXT NOT NULL, effort TEXT NOT NULL,
 conversation_id TEXT, parent_message_id TEXT, created_at REAL NOT NULL);
CREATE TABLE IF NOT EXISTS ultra_root_identity(
 team_id TEXT PRIMARY KEY REFERENCES ultra_teams(team_id),
 controller_id TEXT NOT NULL, root_thread_id TEXT NOT NULL,
 lease_workspace TEXT NOT NULL, owner_label TEXT NOT NULL,
 generation INTEGER NOT NULL DEFAULT 1, updated_at REAL NOT NULL);
CREATE TABLE IF NOT EXISTS ultra_rescue_caps(
 agent_id TEXT PRIMARY KEY REFERENCES agents(logical_agent_id),
 team_id TEXT NOT NULL REFERENCES ultra_teams(team_id),
 token_hash TEXT NOT NULL, token_value TEXT NOT NULL,
 created_at REAL NOT NULL);
CREATE TABLE IF NOT EXISTS ultra_root_rescues(
 rescue_id TEXT PRIMARY KEY, team_id TEXT NOT NULL REFERENCES ultra_teams(team_id),
 requester_agent_id TEXT NOT NULL REFERENCES agents(logical_agent_id),
 request_key TEXT NOT NULL, body_hash TEXT NOT NULL,
 state TEXT NOT NULL, new_root_lease_id TEXT, conversation_id TEXT, conversation_url TEXT,
 error_class TEXT, created_at REAL NOT NULL, updated_at REAL NOT NULL,
 UNIQUE(team_id,requester_agent_id,request_key));
CREATE TABLE IF NOT EXISTS ultra_completions(
 completion_id TEXT PRIMARY KEY, team_id TEXT UNIQUE NOT NULL REFERENCES ultra_teams(team_id),
 root_lease_id TEXT NOT NULL, task_name TEXT NOT NULL, summary TEXT NOT NULL,
 report_json TEXT NOT NULL, recipient TEXT NOT NULL DEFAULT '',
 state TEXT NOT NULL DEFAULT 'armed', attempts INTEGER NOT NULL DEFAULT 0,
 attempt_token TEXT, gmail_message_id TEXT, last_error TEXT NOT NULL DEFAULT '',
 created_at REAL NOT NULL, finalized_at REAL NOT NULL, updated_at REAL NOT NULL, sent_at REAL);
'''


def _paths(values: list[str] | None, workspace: str) -> list[str]:
    if values is not None and (not isinstance(values, list) or len(values) > 32):
        raise AgentError('WRITE_PATHS_INVALID', 'write_paths must be a list of at most 32 paths')
    root = Path(workspace).resolve()
    result = []
    for raw in values or []:
        if not isinstance(raw, str) or not raw.strip() or '\x00' in raw:
            raise AgentError('WRITE_PATHS_INVALID', 'Empty or invalid write path')
        path = Path(raw)
        resolved = (path if path.is_absolute() else root / path).resolve()
        if not resolved.is_relative_to(root):
            raise AgentError('WRITE_PATH_OUTSIDE_WORKSPACE', 'Delegated paths must remain inside the shared workspace')
        result.append(str(resolved))
    return sorted(set(result))


def _overlap(a: str, b: str) -> bool:
    aa, bb = Path(a).resolve(), Path(b).resolve()
    return aa == bb or aa.is_relative_to(bb) or bb.is_relative_to(aa)


def _covers(allowed: list[str], requested: list[str]) -> bool:
    return all(any(Path(p).resolve().is_relative_to(Path(a).resolve()) for a in allowed) for p in requested)


class UltraTeamStore:
    def __init__(self, store, lease_resolver):
        self.store, self.lease_resolver = store, lease_resolver
        with store.connection() as c:
            c.executescript('BEGIN IMMEDIATE;\n' + _SCHEMA + '\nCOMMIT;')
        self._backfill_member_policy_and_binding()

    def _backfill_member_policy_and_binding(self) -> None:
        """Upgrade old Ultra rows from durable IDs only; never infer identity from titles."""
        with self.store.transaction() as c:
            rows = c.execute(
                '''SELECT m.agent_id,m.team_id,a.short_id,a.project_id,a.conversation_id,a.conversation_url
                   FROM ultra_members m JOIN agents a ON a.logical_agent_id=m.agent_id'''
            ).fetchall()
            now = self.store.clock()
            for row in rows:
                c.execute(
                    '''INSERT OR IGNORE INTO ultra_work_policy
                       (agent_id,team_id,work_mode,estimated_minutes,refresh_interval_minutes,created_at,updated_at)
                       VALUES(?,?,?,?,?,?,?)''',
                    (row['agent_id'], row['team_id'], 'bounded', 100.0, 45.0, now, now),
                )
                c.execute(
                    '''INSERT OR IGNORE INTO ultra_conversation_bindings
                       (agent_id,team_id,local_tag,project_id,conversation_id,conversation_url,
                        created_at,bound_at,verified_at,updated_at)
                       VALUES(?,?,?,?,?,?,?,?,?,?)''',
                    (row['agent_id'], row['team_id'], 'gptagent:' + str(row['short_id']).lower(),
                     row['project_id'], row['conversation_id'], row['conversation_url'], now,
                     now if row['conversation_id'] else None,
                     now if row['conversation_id'] else None, now),
                )
                c.execute(
                    '''INSERT OR IGNORE INTO ultra_session_heads(agent_id,team_id,current_generation,updated_at)
                       VALUES(?,?,1,?)''',
                    (row['agent_id'], row['team_id'], now),
                )
                c.execute(
                    '''INSERT OR IGNORE INTO ultra_sessions
                       (agent_id,team_id,generation,project_id,conversation_id,conversation_url,state,reason,created_at,verified_at)
                       VALUES(?,?,?,?,?,?,?,?,?,?)''',
                    (row['agent_id'], row['team_id'], 1, row['project_id'],
                     row['conversation_id'], row['conversation_url'],
                     'active' if row['conversation_id'] else 'allocating',
                     'legacy-backfill', now, now if row['conversation_id'] else None),
                )

    def _team(self, c, team_id: str) -> dict:
        row = c.execute('SELECT * FROM ultra_teams WHERE team_id=?', (team_id,)).fetchone()
        if not row:
            raise AgentError('TEAM_NOT_FOUND', 'No exact Ultra team matches this identity')
        return dict(row)

    def root_identity(self, team_id: str) -> dict:
        with self.store.connection() as c:
            row = c.execute('SELECT * FROM ultra_root_identity WHERE team_id=?', (team_id,)).fetchone()
        if not row:
            raise AgentError('ROOT_IDENTITY_MISSING', 'Ultra team is missing its durable Root identity')
        return dict(row)

    def record_root_identity(self, team: dict, caller, root: dict) -> dict:
        controller_id = str(caller.controller_id or root.get('controller_id') or ('thread:' + caller.thread_id))
        now = self.store.clock()
        with self.store.transaction() as c:
            old = c.execute('SELECT generation FROM ultra_root_identity WHERE team_id=?', (team['team_id'],)).fetchone()
            generation = int(old['generation']) if old else 1
            c.execute(
                '''INSERT INTO ultra_root_identity(team_id,controller_id,root_thread_id,lease_workspace,owner_label,generation,updated_at)
                   VALUES(?,?,?,?,?,?,?)
                   ON CONFLICT(team_id) DO UPDATE SET
                     controller_id=excluded.controller_id,
                     root_thread_id=excluded.root_thread_id,
                     lease_workspace=excluded.lease_workspace,
                     owner_label=excluded.owner_label,
                     updated_at=excluded.updated_at''',
                (team['team_id'], controller_id, caller.thread_id, str(root.get('workspace') or caller.workspace),
                 str(root.get('owner_label') or 'GPTAgent Ultra Root'), generation, now),
            )
        return self.root_identity(team['team_id'])

    def rescue_capability(self, agent_id: str) -> dict:
        with self.store.connection() as c:
            row = c.execute('SELECT * FROM ultra_rescue_caps WHERE agent_id=?', (agent_id,)).fetchone()
        if not row:
            raise AgentError('ROOT_RESCUE_CAP_MISSING', 'This Ultra member has no Root rescue capability')
        return dict(row)

    def authorize_root_rescue(self, team_id: str, agent_id: str, token: str,
                              project_id: str, conversation_id: str) -> tuple[dict, dict, dict]:
        with self.store.connection() as c:
            team = self._team(c, team_id)
            member = c.execute('SELECT * FROM ultra_members WHERE team_id=? AND agent_id=?',
                               (team_id, agent_id)).fetchone()
            cap = c.execute('SELECT * FROM ultra_rescue_caps WHERE team_id=? AND agent_id=?',
                            (team_id, agent_id)).fetchone()
            binding = c.execute('SELECT * FROM ultra_conversation_bindings WHERE team_id=? AND agent_id=?',
                                (team_id, agent_id)).fetchone()
            head = c.execute('SELECT current_generation FROM ultra_session_heads WHERE agent_id=?', (agent_id,)).fetchone()
            agent = c.execute('SELECT state FROM agents WHERE logical_agent_id=?', (agent_id,)).fetchone()
        if team['state'] not in {'active','stopping'}:
            raise AgentError('TEAM_NOT_RESCUABLE', 'Completed/superseded Ultra teams cannot be rescued')
        if not member or not cap or not binding or not head or not agent:
            raise AgentError('ROOT_RESCUE_IDENTITY_INVALID', 'Requester is not a durable member of this exact team')
        if agent['state'] in {'DONE','CLOSED','BLOCKED'}:
            raise AgentError('ROOT_RESCUE_REQUESTER_TERMINAL', 'Terminal members cannot rescue a Root')
        if digest(str(token or '')) != cap['token_hash']:
            raise AgentError('ROOT_RESCUE_TOKEN_INVALID', 'Root rescue capability did not match')
        if binding['project_id'] != project_id or binding['conversation_id'] != conversation_id:
            raise AgentError('ROOT_RESCUE_IDENTITY_INVALID', 'Exact project/conversation binding did not match requester')
        session = self.current_session(agent_id)
        if int(session['generation']) != int(head['current_generation']) or session['state'] != 'active':
            raise AgentError('STALE_SESSION_GENERATION', 'Only the current active member session may rescue Root')
        return dict(team), dict(member), dict(binding)

    @staticmethod
    def _team_has_work_locked(c, team_id: str) -> bool:
        for table in ('ultra_members', 'ultra_tasks', 'ultra_mail', 'ultra_sends'):
            if c.execute(f'SELECT 1 FROM {table} WHERE team_id=? LIMIT 1', (team_id,)).fetchone():
                return True
        return False

    def _detach_dead_participants(self, team_id: str) -> int:
        detached = 0
        with self.store.transaction() as c:
            rows = c.execute(
                'SELECT agent_id,participant_id,thread_id FROM ultra_members WHERE team_id=?',
                (team_id,),
            ).fetchall()
            for row in rows:
                participant_id = row['participant_id']
                if not participant_id or self.lease_resolver(participant_id):
                    continue
                c.execute(
                    'UPDATE ultra_members SET participant_id=NULL,thread_id=NULL WHERE agent_id=?',
                    (row['agent_id'],),
                )
                agent = c.execute(
                    'SELECT state FROM agents WHERE logical_agent_id=?',
                    (row['agent_id'],),
                ).fetchone()
                if agent and agent['state'] not in {'DONE', 'CLOSED', 'BLOCKED'}:
                    c.execute(
                        "UPDATE agents SET runtime_thread_id=NULL,join_state='detached',state='DETACHED',updated_at=? "
                        'WHERE logical_agent_id=?',
                        (self.store.clock(), row['agent_id']),
                    )
                detached += 1
        return detached

    def context(self, caller, *, require_active=False) -> tuple[dict, str, dict | None]:
        root_id = caller.shared_root_lease_id or caller.lease_id
        root = self.lease_resolver(root_id)
        if not root or root.get('lease_role') == 'participant':
            raise AgentError('TEAM_ROOT_EXPIRED', 'The shared root resource lease is not active')
        with self.store.connection() as c:
            row = c.execute('SELECT * FROM ultra_teams WHERE root_lease_id=?', (root_id,)).fetchone()
            if not row:
                raise AgentError('TEAM_NOT_FOUND', 'Call ensure_project before starting this Ultra team')
            team = dict(row)
            if caller.lease_role != 'participant':
                if caller.lease_id != root_id or caller.thread_id != team['root_thread_id']:
                    raise AgentError('TEAM_SCOPE_FORBIDDEN', 'Root controller identity does not match')
                actor, member = 'root', None
            else:
                row = c.execute('SELECT * FROM ultra_members WHERE team_id=? AND participant_id=? AND thread_id=?',
                                (team['team_id'], caller.lease_id, caller.thread_id)).fetchone()
                if not row:
                    raise AgentError('TEAM_SCOPE_FORBIDDEN', 'Participant is not a member of this exact team')
                member = dict(row)
                agent = self.store.get(member['agent_id'])
                head = c.execute('SELECT current_generation FROM ultra_session_heads WHERE agent_id=?',
                                 (member['agent_id'],)).fetchone()
                if not head or int(caller.session_generation or 0) != int(head['current_generation']):
                    raise AgentError('STALE_SESSION_GENERATION',
                                     'This participant belongs to a retired Ultra session generation')
                if caller.logical_agent_id and caller.logical_agent_id != member['agent_id']:
                    raise AgentError('TEAM_SCOPE_FORBIDDEN', 'Participant logical_agent_id does not match Team membership')
                if agent['join_state'] != 'joined' or agent['runtime_thread_id'] != caller.thread_id:
                    raise AgentError('TEAM_JOIN_PENDING', 'Participant has not completed verified conversation binding')
                if agent['state'] in {'DONE', 'CLOSED', 'BLOCKED'}:
                    raise AgentError('TEAM_MEMBER_TERMINAL', 'Terminal members cannot start more work')
                actor = member['agent_id']
        if team['project_id'] == '' or self.store.config.project_name != PROJECT_NAME:
            raise AgentError('PROJECT_POLICY_MISMATCH', 'Ultra requires the exact GPTAgent project')
        if require_active and team['state'] != 'active':
            raise AgentError('TEAM_STOPPING', 'No new work is allowed while this team is stopping')
        return team, actor, member

    def ensure(self, caller, project: dict) -> dict:
        root_id = caller.shared_root_lease_id or caller.lease_id
        root = self.lease_resolver(root_id)
        if not root or root.get('lease_role') == 'participant':
            raise AgentError('TEAM_ROOT_EXPIRED', 'Root lease is not active')
        recovered_team_id = None
        with self.store.transaction() as c:
            row = c.execute('SELECT * FROM ultra_teams WHERE root_lease_id=?', (root_id,)).fetchone()
            predecessors = c.execute(
                "SELECT * FROM ultra_teams WHERE root_thread_id=? AND root_lease_id<>? "
                "AND project_id=? AND account_tag=? AND state IN ('active','stopping') ORDER BY created_at DESC",
                (caller.thread_id, root_id, project['project_id'], project['account_tag']),
            ).fetchall()
            live_predecessors = [old for old in predecessors if self.lease_resolver(old['root_lease_id'])]
            if live_predecessors:
                raise AgentError(
                    'TEAM_ROOT_CONFLICT',
                    'Another active Root lease still owns this stable Ultra thread; do not guess or steal it',
                )
            dead_predecessors = list(predecessors)
            if row:
                team = dict(row)
                if (team['project_id'] != project['project_id'] or team['account_tag'] != project['account_tag']):
                    raise AgentError('TEAM_ACCOUNT_CHANGED', 'Account or GPTAgent project changed; no silent rebinding')
                # A reconnect under the same stable Runtime thread may have
                # created an empty replacement team before the old Root lease
                # was recognized as this conversation's prior identity. Prefer
                # the newest stale team that actually contains durable work.
                recover = next(
                    (old for old in dead_predecessors if self._team_has_work_locked(c, old['team_id'])),
                    None,
                )
                if recover and not self._team_has_work_locked(c, team['team_id']):
                    c.execute('DELETE FROM ultra_teams WHERE team_id=?', (team['team_id'],))
                    c.execute(
                        "UPDATE ultra_teams SET root_lease_id=?,workspace=?,state='active' WHERE team_id=?",
                        (root_id, str(root.get('scope_root') or caller.workspace), recover['team_id']),
                    )
                    recovered_team_id = recover['team_id']
                    team = self._team(c, recovered_team_id)
                    dead_predecessors = [old for old in dead_predecessors if old['team_id'] != recovered_team_id]
            else:
                if caller.lease_role == 'participant':
                    raise AgentError('TEAM_SCOPE_FORBIDDEN', 'Only the root may initialize a team')
                recover = next(
                    (old for old in dead_predecessors if self._team_has_work_locked(c, old['team_id'])),
                    None,
                )
                if recover:
                    c.execute(
                        "UPDATE ultra_teams SET root_lease_id=?,workspace=?,state='active' WHERE team_id=?",
                        (root_id, str(root.get('scope_root') or caller.workspace), recover['team_id']),
                    )
                    recovered_team_id = recover['team_id']
                    team = self._team(c, recovered_team_id)
                    dead_predecessors = [old for old in dead_predecessors if old['team_id'] != recovered_team_id]
                else:
                    team = {'team_id': 'team_' + uuid.uuid4().hex, 'root_lease_id': root_id,
                            'root_thread_id': caller.thread_id, 'workspace': str(root.get('scope_root') or caller.workspace),
                            'project_id': project['project_id'], 'account_tag': project['account_tag'],
                            'state': 'active', 'created_at': self.store.clock()}
                    c.execute('INSERT INTO ultra_teams VALUES(:team_id,:root_lease_id,:root_thread_id,:workspace,:project_id,:account_tag,:state,:created_at)', team)
            for old in dead_predecessors:
                c.execute("UPDATE ultra_teams SET state='superseded' WHERE team_id=?", (old['team_id'],))
        # Participants are descendants of the old Root resource lease. Once
        # that Root is gone, their handles are invalid even though the ChatGPT
        # conversations remain durable. Detach only those invalid handles;
        # never release or mutate the current Root resource owner.
        if recovered_team_id:
            self._detach_dead_participants(recovered_team_id)
        for old in dead_predecessors:
            self._detach_dead_participants(old['team_id'])
        self.record_root_identity(team, caller, root)
        self.context(caller)
        return team

    def add_member(self, team: dict, actor: str, agent: dict, write_paths: list[str], *,
                   work_mode: str = 'bounded', estimated_minutes: float = 100.0) -> dict:
        paths = _paths(write_paths, team['workspace'])
        if work_mode not in {'bounded', 'continuous'}:
            raise AgentError('WORK_MODE_INVALID', 'work_mode must be bounded or continuous')
        if isinstance(estimated_minutes, bool) or not isinstance(estimated_minutes, (int, float)) or estimated_minutes <= 0:
            raise AgentError('WORK_ESTIMATE_REQUIRED', 'A positive estimated_minutes value is required')
        if work_mode == 'bounded' and estimated_minutes > 100:
            raise AgentError('WORK_BUDGET_EXCEEDED', 'Bounded Ultra work must be estimated at 100 minutes or less')
        now = self.store.clock()
        local_tag = 'gptagent:' + str(agent['short_id']).lower()
        rescue_token = secrets.token_urlsafe(32)
        with self.store.transaction() as c:
            current = self._team(c, team['team_id'])
            if current['state'] != 'active':
                raise AgentError('TEAM_STOPPING', 'Team stopped during member allocation')
            c.execute('INSERT INTO ultra_members(agent_id,team_id,parent_actor,write_paths_json,created_at) VALUES(?,?,?,?,?)',
                      (agent['logical_agent_id'], team['team_id'], actor, canonical_json(paths), now))
            c.execute('INSERT INTO ultra_work_policy(agent_id,team_id,work_mode,estimated_minutes,refresh_interval_minutes,created_at,updated_at) VALUES(?,?,?,?,?,?,?)',
                      (agent['logical_agent_id'], team['team_id'], work_mode, float(estimated_minutes), 45.0, now, now))
            c.execute('INSERT INTO ultra_conversation_bindings(agent_id,team_id,local_tag,project_id,created_at,updated_at) VALUES(?,?,?,?,?,?)',
                      (agent['logical_agent_id'], team['team_id'], local_tag, team['project_id'], now, now))
            c.execute('INSERT INTO ultra_session_heads(agent_id,team_id,current_generation,updated_at) VALUES(?,?,1,?)',
                      (agent['logical_agent_id'], team['team_id'], now))
            c.execute('''INSERT INTO ultra_sessions
                       (agent_id,team_id,generation,project_id,state,reason,created_at)
                       VALUES(?,?,?,?,?,?,?)''',
                      (agent['logical_agent_id'], team['team_id'], 1, team['project_id'],
                       'allocating', 'initial-allocation', now))
            c.execute(
                'INSERT INTO ultra_rescue_caps(agent_id,team_id,token_hash,token_value,created_at) VALUES(?,?,?,?,?)',
                (agent['logical_agent_id'], team['team_id'], digest(rescue_token), rescue_token, now),
            )
        return {**self.member(agent['logical_agent_id']), 'root_rescue_token': rescue_token}

    def member(self, agent_id: str) -> dict | None:
        with self.store.connection() as c:
            row = c.execute('SELECT * FROM ultra_members WHERE agent_id=?', (agent_id,)).fetchone()
            return dict(row) if row else None

    def work_policy(self, agent_id: str) -> dict:
        with self.store.connection() as c:
            row = c.execute('SELECT * FROM ultra_work_policy WHERE agent_id=?', (agent_id,)).fetchone()
        if not row:
            raise AgentError('WORK_POLICY_MISSING', 'Ultra member is missing its work policy')
        return dict(row)

    def session_head(self, agent_id: str) -> dict:
        with self.store.connection() as c:
            row = c.execute('SELECT * FROM ultra_session_heads WHERE agent_id=?', (agent_id,)).fetchone()
        if not row:
            raise AgentError('SESSION_HEAD_MISSING', 'Ultra member is missing its durable session head')
        return dict(row)

    def current_session(self, agent_id: str) -> dict:
        head = self.session_head(agent_id)
        with self.store.connection() as c:
            row = c.execute(
                'SELECT * FROM ultra_sessions WHERE agent_id=? AND generation=?',
                (agent_id, head['current_generation']),
            ).fetchone()
        if not row:
            raise AgentError('SESSION_STATE_MISSING', 'Current Ultra session generation is missing')
        return dict(row)

    def session_history(self, agent_id: str) -> list[dict]:
        with self.store.connection() as c:
            rows = c.execute(
                'SELECT * FROM ultra_sessions WHERE agent_id=? ORDER BY generation',
                (agent_id,),
            ).fetchall()
        return [dict(row) for row in rows]

    def binding(self, agent_id: str) -> dict:
        with self.store.connection() as c:
            row = c.execute('SELECT * FROM ultra_conversation_bindings WHERE agent_id=?', (agent_id,)).fetchone()
        if not row:
            raise AgentError('CONVERSATION_BINDING_MISSING', 'Ultra member is missing its local conversation binding')
        return dict(row)

    def bind_conversation_identity(self, team: dict, agent_id: str, project_id: str,
                                   conversation_id: str, conversation_url: str) -> dict:
        with self.store.transaction() as c:
            row = c.execute('SELECT * FROM ultra_conversation_bindings WHERE agent_id=? AND team_id=?',
                            (agent_id, team['team_id'])).fetchone()
            if not row:
                raise AgentError('CONVERSATION_BINDING_MISSING', 'Local label was not allocated before web conversation binding')
            if row['project_id'] != project_id or project_id != team['project_id']:
                raise AgentError('CONVERSATION_PROJECT_MISMATCH', 'Local label project binding cannot change')
            if row['conversation_id'] not in (None, conversation_id) or row['conversation_url'] not in (None, conversation_url):
                raise AgentError('CONVERSATION_BINDING_CHANGED', 'A local GPTAgent label cannot be rebound except through explicit session migration')
            head = c.execute('SELECT * FROM ultra_session_heads WHERE agent_id=?', (agent_id,)).fetchone()
            if not head:
                raise AgentError('SESSION_HEAD_MISSING', 'Ultra member is missing its durable session head')
            session = c.execute(
                'SELECT * FROM ultra_sessions WHERE agent_id=? AND generation=?',
                (agent_id, head['current_generation']),
            ).fetchone()
            if not session or session['state'] not in {'allocating','active'}:
                raise AgentError('SESSION_STATE_INVALID', 'Initial conversation can bind only to current session')
            if session['conversation_id'] not in (None, conversation_id):
                raise AgentError('CONVERSATION_BINDING_CHANGED', 'Current session already has another conversation')
            now = self.store.clock()
            try:
                c.execute('UPDATE ultra_conversation_bindings SET conversation_id=?,conversation_url=?,bound_at=coalesce(bound_at,?),verified_at=?,updated_at=? WHERE agent_id=?',
                          (conversation_id, conversation_url, now, now, now, agent_id))
                c.execute(
                    "UPDATE ultra_sessions SET conversation_id=?,conversation_url=?,state='active',verified_at=?,reason=CASE WHEN reason='' THEN 'verified-initial-bind' ELSE reason END WHERE agent_id=? AND generation=?",
                    (conversation_id, conversation_url, now, agent_id, head['current_generation']),
                )
            except Exception as exc:
                raise AgentError('CONVERSATION_BINDING_CONFLICT', 'Exact web conversation is already bound to another local label/session') from exc
            return dict(c.execute('SELECT * FROM ultra_conversation_bindings WHERE agent_id=?', (agent_id,)).fetchone())

    def mark_session_unusable(self, agent_id: str, generation: int, reason: str) -> dict:
        clean_reason = public_text(reason or 'verified-unusable', 1000)
        with self.store.transaction() as c:
            head = c.execute('SELECT * FROM ultra_session_heads WHERE agent_id=?', (agent_id,)).fetchone()
            if not head or int(head['current_generation']) != int(generation):
                raise AgentError('STALE_SESSION_GENERATION', 'Session head already moved')
            c.execute(
                "UPDATE ultra_sessions SET state='unusable',reason=? WHERE agent_id=? AND generation=? AND state<>'retired'",
                (clean_reason, agent_id, generation),
            )
            row = c.execute('SELECT * FROM ultra_sessions WHERE agent_id=? AND generation=?',
                            (agent_id, generation)).fetchone()
            if not row:
                raise AgentError('SESSION_STATE_MISSING', 'Session generation is missing')
            return dict(row)

    def migrate_conversation_identity(self, team: dict, agent_id: str, *,
                                      expected_generation: int, expected_old_conversation_id: str,
                                      new_conversation_id: str, new_conversation_url: str,
                                      new_runtime_thread_id: str, reason: str) -> dict:
        now = self.store.clock()
        clean_reason = public_text(reason or 'verified-session-migration', 1000)
        with self.store.transaction() as c:
            head = c.execute('SELECT * FROM ultra_session_heads WHERE agent_id=? AND team_id=?',
                             (agent_id, team['team_id'])).fetchone()
            if not head or int(head['current_generation']) != int(expected_generation):
                raise AgentError('STALE_SESSION_GENERATION', 'Session head moved before migration commit')
            old_session = c.execute('SELECT * FROM ultra_sessions WHERE agent_id=? AND generation=?',
                                    (agent_id, expected_generation)).fetchone()
            binding = c.execute('SELECT * FROM ultra_conversation_bindings WHERE agent_id=? AND team_id=?',
                                (agent_id, team['team_id'])).fetchone()
            agent = c.execute('SELECT * FROM agents WHERE logical_agent_id=?', (agent_id,)).fetchone()
            if (not old_session or old_session['conversation_id'] != expected_old_conversation_id
                    or not binding or binding['conversation_id'] != expected_old_conversation_id
                    or not agent or agent['conversation_id'] != expected_old_conversation_id):
                raise AgentError('CONVERSATION_BINDING_MISMATCH', 'Migration source is no longer the exact current conversation')
            next_generation = int(expected_generation) + 1
            if c.execute('SELECT 1 FROM ultra_sessions WHERE agent_id=? AND generation=?',
                         (agent_id, next_generation)).fetchone():
                raise AgentError('SESSION_GENERATION_CONFLICT', 'Next session generation already exists')
            try:
                c.execute(
                    "INSERT INTO ultra_sessions(agent_id,team_id,generation,project_id,conversation_id,conversation_url,state,reason,created_at,verified_at) VALUES(?,?,?,?,?,?,'active',?,?,?)",
                    (agent_id, team['team_id'], next_generation, team['project_id'],
                     new_conversation_id, new_conversation_url, clean_reason, now, now),
                )
                c.execute("UPDATE ultra_sessions SET state='retired',reason=?,retired_at=? WHERE agent_id=? AND generation=?",
                          (clean_reason, now, agent_id, expected_generation))
                c.execute('UPDATE ultra_session_heads SET current_generation=?,updated_at=? WHERE agent_id=?',
                          (next_generation, now, agent_id))
                c.execute('UPDATE ultra_conversation_bindings SET conversation_id=?,conversation_url=?,verified_at=?,updated_at=? WHERE agent_id=?',
                          (new_conversation_id, new_conversation_url, now, now, agent_id))
                c.execute(
                    "UPDATE agents SET conversation_id=?,conversation_url=?,runtime_thread_id=?,join_state='joined',state='RUNNING',updated_at=? WHERE logical_agent_id=?",
                    (new_conversation_id, new_conversation_url, new_runtime_thread_id, now, agent_id),
                )
            except Exception as exc:
                if isinstance(exc, AgentError):
                    raise
                raise AgentError('CONVERSATION_BINDING_CONFLICT', 'New conversation already belongs to another Ultra session') from exc
            return dict(c.execute('SELECT * FROM ultra_sessions WHERE agent_id=? AND generation=?',
                                  (agent_id, next_generation)).fetchone())

    def verify_conversation_identity(self, team: dict, agent_id: str, project_id: str,
                                     conversation_id: str) -> dict:
        binding = self.binding(agent_id)
        if (binding['team_id'] != team['team_id'] or binding['project_id'] != project_id
                or binding['conversation_id'] != conversation_id):
            raise AgentError('CONVERSATION_BINDING_MISMATCH',
                             'Use the exact locally bound GPTAgent project/conversation; titles are not identity')
        return binding

    def resolve_local_tag(self, team: dict, ref: str) -> str | None:
        with self.store.connection() as c:
            row = c.execute('SELECT agent_id FROM ultra_conversation_bindings WHERE team_id=? AND local_tag=?',
                            (team['team_id'], ref)).fetchone()
        return row['agent_id'] if row else None

    def bind_participant(self, agent_id: str, participant: dict) -> None:
        with self.store.transaction() as c:
            member = c.execute('SELECT * FROM ultra_members WHERE agent_id=?', (agent_id,)).fetchone()
            if not member:
                raise AgentError('TEAM_MEMBER_MISSING', 'Member must be allocated before participant binding')
            team = self._team(c, member['team_id'])
            if participant.get('shared_root_lease_id') != team['root_lease_id']:
                raise AgentError('TEAM_SCOPE_FORBIDDEN', 'Participant belongs to a different resource lease')
            head = c.execute('SELECT current_generation FROM ultra_session_heads WHERE agent_id=?', (agent_id,)).fetchone()
            if not head or int(participant.get('session_generation') or 1) != int(head['current_generation']):
                raise AgentError('STALE_SESSION_GENERATION', 'Participant does not belong to the current session generation')
            if member['participant_id'] not in (None, participant['lease_id']):
                raise AgentError('PARTICIPANT_BINDING_CONFLICT', 'Member already has another participant')
            c.execute('UPDATE ultra_members SET participant_id=?,thread_id=? WHERE agent_id=?',
                      (participant['lease_id'], participant['thread_id'], agent_id))

    def rebind_recovery_participant(self, agent_id: str, participant: dict, *, generation: int) -> dict:
        with self.store.transaction() as c:
            member = c.execute('SELECT * FROM ultra_members WHERE agent_id=?', (agent_id,)).fetchone()
            if not member:
                raise AgentError('TEAM_MEMBER_MISSING', 'Recovery target is not an Ultra member')
            team = self._team(c, member['team_id'])
            head = c.execute('SELECT * FROM ultra_session_heads WHERE agent_id=?', (agent_id,)).fetchone()
            if not head or int(head['current_generation']) != int(generation):
                raise AgentError('STALE_SESSION_GENERATION', 'Recovery participant belongs to a stale generation')
            if participant.get('shared_root_lease_id') != team['root_lease_id']:
                raise AgentError('TEAM_SCOPE_FORBIDDEN', 'Recovery participant belongs to another Root resource lease')
            if int(participant.get('session_generation') or 0) != int(generation):
                raise AgentError('STALE_SESSION_GENERATION', 'Recovery participant generation does not match session head')
            now = self.store.clock()
            c.execute('UPDATE ultra_members SET participant_id=?,thread_id=? WHERE agent_id=?',
                      (participant['lease_id'], participant['thread_id'], agent_id))
            c.execute(
                "UPDATE agents SET runtime_thread_id=?,join_state='joined',state=CASE WHEN state IN ('DETACHED','DEAD_RECOVERABLE','SUSPECT','RECOVERING') THEN 'IDLE' ELSE state END,updated_at=? WHERE logical_agent_id=?",
                (participant['thread_id'], now, agent_id),
            )
            return dict(c.execute('SELECT * FROM ultra_members WHERE agent_id=?', (agent_id,)).fetchone())

    def stage_migration_participant(self, agent_id: str, participant: dict, *, expected_generation: int) -> dict:
        with self.store.transaction() as c:
            member = c.execute('SELECT * FROM ultra_members WHERE agent_id=?', (agent_id,)).fetchone()
            head = c.execute('SELECT * FROM ultra_session_heads WHERE agent_id=?', (agent_id,)).fetchone()
            if not member or not head:
                raise AgentError('TEAM_MEMBER_MISSING', 'Migration target is not a durable Ultra member')
            if int(head['current_generation']) != int(expected_generation):
                raise AgentError('STALE_SESSION_GENERATION', 'Session head moved before migration staging')
            if int(participant.get('session_generation') or 0) != int(expected_generation) + 1:
                raise AgentError('STALE_SESSION_GENERATION', 'Staged participant must belong to the next session generation')
            c.execute('UPDATE ultra_members SET participant_id=?,thread_id=? WHERE agent_id=?',
                      (participant['lease_id'], participant['thread_id'], agent_id))
            return dict(c.execute('SELECT * FROM ultra_members WHERE agent_id=?', (agent_id,)).fetchone())

    def clear_recovery_participant(self, agent_id: str, participant_id: str) -> None:
        with self.store.transaction() as c:
            c.execute(
                'UPDATE ultra_members SET participant_id=NULL,thread_id=NULL WHERE agent_id=? AND participant_id=?',
                (agent_id, participant_id),
            )
            c.execute(
                "UPDATE agents SET runtime_thread_id=NULL,join_state='detached',state=CASE WHEN state IN ('DONE','CLOSED','BLOCKED') THEN state ELSE 'DETACHED' END,updated_at=? WHERE logical_agent_id=?",
                (self.store.clock(), agent_id),
            )

    def resolve_recipient(self, team: dict, actor: str, ref: str | None, member=None) -> str:
        if ref == 'parent':
            ref = member['parent_actor'] if member else 'root'
        if ref == 'root':
            return 'root'
        if not ref:
            raise AgentError('RECIPIENT_REQUIRED', 'Use an exact Agent id, root, or parent')
        local_agent_id = self.resolve_local_tag(team, ref)
        agent = self.store.get(local_agent_id or ref)
        target = self.member(agent['logical_agent_id'])
        if not target or target['team_id'] != team['team_id']:
            raise AgentError('TEAM_SCOPE_FORBIDDEN', 'Cross-team communication is forbidden')
        return agent['logical_agent_id']

    def post(self, team: dict, actor: str, to: str, content: str, *, key: str, reply_to=None, kind='message') -> dict:
        if not content or not content.strip() or len(content) > 12000 or not key or len(key) > 200:
            raise AgentError('MESSAGE_INVALID', 'Message and bounded idempotency_key are required')
        if kind not in {
            'message', 'question', 'answer', 'finding', 'blocker',
            'review_request', 'handoff', 'shutdown_request', 'shutdown_ack',
        }:
            raise AgentError('MESSAGE_KIND_INVALID', 'Unsupported team message kind')
        text = public_text(content, 12000)
        fingerprint = digest(canonical_json([to, text, reply_to, kind]))
        with self.store.transaction() as c:
            old = c.execute('SELECT * FROM ultra_mail WHERE team_id=? AND from_actor=? AND request_key=?',
                            (team['team_id'], actor, key)).fetchone()
            if old:
                if old['body_hash'] != fingerprint:
                    raise AgentError('IDEMPOTENCY_CONFLICT', 'Same message key was used for different content or recipient')
                return {'status': 'already_queued', 'message_id': old['message_id'], 'new_turn': False}
            if reply_to:
                original = c.execute('SELECT * FROM ultra_mail WHERE message_id=? AND team_id=?',
                                     (reply_to, team['team_id'])).fetchone()
                if not original or original['to_actor'] != actor or original['from_actor'] != to:
                    raise AgentError('REPLY_SCOPE_FORBIDDEN', 'Reply must go back to the sender of a message received by you')
            mid = 'umail_' + uuid.uuid4().hex
            c.execute('INSERT INTO ultra_mail(message_id,team_id,from_actor,to_actor,kind,content,reply_to,request_key,body_hash,created_at) VALUES(?,?,?,?,?,?,?,?,?,?)',
                      (mid, team['team_id'], actor, to, kind, text, reply_to, key, fingerprint, self.store.clock()))
            self.store._event(c, None, 'ultra.mail', team_id=team['team_id'], recipient=to)
        return {'status': 'queued', 'message_id': mid, 'new_turn': False}

    def broadcast(self, team: dict, actor: str, content: str, *, key: str, kind='message') -> dict:
        if not key or not isinstance(key, str) or len(key) > 200:
            raise AgentError('IDEMPOTENCY_KEY_REQUIRED', 'Broadcast requires one bounded stable idempotency_key')
        with self.store.connection() as c:
            rows = c.execute(
                "SELECT m.agent_id,a.join_state,a.state "
                "FROM ultra_members m JOIN agents a ON a.logical_agent_id=m.agent_id "
                "WHERE m.team_id=? ORDER BY m.created_at,m.agent_id",
                (team['team_id'],),
            ).fetchall()
        recipients = []
        if actor != 'root':
            recipients.append('root')
        for row in rows:
            aid = row['agent_id']
            if aid == actor:
                continue
            if row['join_state'] != 'joined' or row['state'] in {'DONE', 'CLOSED', 'BLOCKED'}:
                continue
            recipients.append(aid)
        messages = []
        for recipient in recipients:
            recipient_key = 'broadcast:' + digest(canonical_json([key, recipient]))[:48]
            messages.append(
                self.post(team, actor, recipient, content, key=recipient_key, kind=kind)
            )
        return {
            'status': 'queued',
            'recipient_count': len(recipients),
            'recipients': recipients,
            'message_ids': [item['message_id'] for item in messages],
            'new_turn': False,
        }

    def inbox(self, team: dict, actor: str, *, ack_ids=None, deliver=False) -> list[dict]:
        with self.store.transaction() as c:
            for mid in (ack_ids or [])[:100]:
                c.execute('UPDATE ultra_mail SET acked_at=? WHERE team_id=? AND to_actor=? AND message_id=?',
                          (self.store.clock(), team['team_id'], actor, mid))
            extra = ' AND delivered_at IS NULL' if deliver else ''
            rows = c.execute('SELECT * FROM ultra_mail WHERE team_id=? AND to_actor=? AND acked_at IS NULL' + extra + ' ORDER BY created_at,message_id LIMIT 30',
                             (team['team_id'], actor)).fetchall()
            if deliver:
                for row in rows:
                    c.execute('UPDATE ultra_mail SET delivered_at=? WHERE message_id=?', (self.store.clock(), row['message_id']))
            return [{k: row[k] for k in ('message_id', 'from_actor', 'to_actor', 'kind', 'content', 'reply_to', 'created_at')} for row in rows]

    def tasks(self, team: dict) -> list[dict]:
        with self.store.connection() as c:
            rows = c.execute('SELECT * FROM ultra_tasks WHERE team_id=? ORDER BY created_at,task_id LIMIT 500', (team['team_id'],)).fetchall()
        return [self._task_public(dict(r)) for r in rows]

    @staticmethod
    def _task_public(row: dict) -> dict:
        for field in ('depends', 'write_paths', 'artifacts'):
            row[field] = json.loads(row.pop(field + '_json'))
        row.pop('body_hash', None)
        row.pop('request_key', None)
        return row

    def create_task(self, team: dict, actor: str, member: dict | None, p: dict) -> dict:
        title, description, key = p.get('name') or '', p.get('message') or '', p.get('idempotency_key') or ''
        if not title.strip() or len(title) > 200 or not key or len(key) > 200 or len(description) > 12000:
            raise AgentError('TASK_INVALID', 'Provide a bounded name, message and idempotency_key')
        dependencies = p.get('depends_on') or []
        if not isinstance(dependencies, list) or len(dependencies) > 32 or not all(isinstance(x, str) for x in dependencies):
            raise AgentError('DEPENDENCIES_INVALID', 'depends_on must contain task ids')
        paths = _paths(p.get('write_paths'), team['workspace'])
        if member and not _covers(json.loads(member['write_paths_json']), paths):
            raise AgentError('WRITE_SCOPE_FORBIDDEN', 'Child cannot delegate paths beyond its assigned write scope')
        fingerprint = digest(canonical_json([title, description, sorted(set(dependencies)), paths]))
        with self.store.transaction() as c:
            if self._team(c, team['team_id'])['state'] != 'active':
                raise AgentError('TEAM_STOPPING', 'No new task during shutdown')
            old = c.execute('SELECT * FROM ultra_tasks WHERE team_id=? AND creator=? AND request_key=?', (team['team_id'], actor, key)).fetchone()
            if old:
                if old['body_hash'] != fingerprint:
                    raise AgentError('IDEMPOTENCY_CONFLICT', 'Task key was reused for different task content')
                return self._task_public(dict(old))
            for tid in dependencies:
                if not c.execute('SELECT 1 FROM ultra_tasks WHERE task_id=? AND team_id=?', (tid, team['team_id'])).fetchone():
                    raise AgentError('TASK_DEPENDENCY_MISSING', 'Dependency must already exist in the same team')
            # Dependencies are immutable and point only to older tasks: cycles cannot be introduced.
            tid, now = 'utask_' + uuid.uuid4().hex, self.store.clock()
            c.execute('INSERT INTO ultra_tasks(task_id,team_id,title,description,creator,depends_json,write_paths_json,request_key,body_hash,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?,?,?)',
                      (tid, team['team_id'], public_text(title, 200), public_text(description), actor,
                       canonical_json(sorted(set(dependencies))), canonical_json(paths), key, fingerprint, now, now))
            row = dict(c.execute('SELECT * FROM ultra_tasks WHERE task_id=?', (tid,)).fetchone())
        return self._task_public(row)

    def claim(self, team: dict, actor: str, member: dict | None, task_id: str, version: int) -> dict:
        with self.store.transaction() as c:
            if self._team(c, team['team_id'])['state'] != 'active':
                raise AgentError('TEAM_STOPPING', 'No task claims during shutdown')
            task = self._task_for_update(c, team, task_id, version)
            if task['status'] != 'pending':
                raise AgentError('TASK_NOT_PENDING', 'Task is already owned, blocked or terminal')
            for tid in json.loads(task['depends_json']):
                dep = c.execute('SELECT status FROM ultra_tasks WHERE task_id=? AND team_id=?', (tid, team['team_id'])).fetchone()
                if not dep or dep['status'] != 'done':
                    raise AgentError('TASK_DEPENDENCY_PENDING', 'A dependency is not completed')
            paths = json.loads(task['write_paths_json'])
            if member and not _covers(json.loads(member['write_paths_json']), paths):
                raise AgentError('WRITE_SCOPE_FORBIDDEN', 'Task writes exceed this member assignment')
            # Blocked work keeps its write claim until the owner/root explicitly
            # returns it to pending with evidence; never hand the same path away.
            for other in c.execute("SELECT task_id,write_paths_json FROM ultra_tasks WHERE team_id=? AND status IN ('in_progress','blocked')", (team['team_id'],)):
                if any(_overlap(a, b) for a in paths for b in json.loads(other['write_paths_json'])):
                    raise AgentError('TASK_WRITE_CONFLICT', 'Another active task owns an overlapping write path', conflicting_task_id=other['task_id'])
            c.execute("UPDATE ultra_tasks SET owner=?,status='in_progress',version=version+1,updated_at=? WHERE task_id=?", (actor, self.store.clock(), task_id))
            return self._task_public(dict(c.execute('SELECT * FROM ultra_tasks WHERE task_id=?', (task_id,)).fetchone()))

    def _task_for_update(self, c, team, task_id, version):
        row = c.execute('SELECT * FROM ultra_tasks WHERE task_id=? AND team_id=?', (task_id, team['team_id'])).fetchone()
        if not row:
            raise AgentError('TASK_NOT_FOUND', 'Task is not in this team')
        if type(version) is not int or row['version'] != version:
            raise AgentError('TASK_VERSION_CONFLICT', 'Read the current task version before changing it')
        return dict(row)

    def update_task(self, team, actor, task_id, version, status, evidence='', artifacts=None):
        if status not in {'done', 'blocked', 'cancelled', 'pending'}:
            raise AgentError('TASK_STATUS_INVALID', 'Use done, blocked, cancelled or pending')
        if not evidence or not evidence.strip():
            raise AgentError('TASK_EVIDENCE_REQUIRED', 'State changes require a concrete public result or reason')
        with self.store.transaction() as c:
            task = self._task_for_update(c, team, task_id, version)
            if task['status'] in {'done', 'cancelled'}:
                raise AgentError('TASK_TERMINAL', 'Terminal tasks are immutable; create a new follow-up task')
            if task['owner'] != actor and not (actor == 'root' and status in {'cancelled', 'pending'}):
                raise AgentError('TASK_OWNER_REQUIRED', 'Only the claimant may report its result')
            if task['status'] == 'in_progress' and task['owner'] != actor:
                raise AgentError('TASK_STILL_RUNNING', 'Ask the active owner to stop and acknowledge before releasing its write claim')
            if status == 'done' and task['status'] != 'in_progress':
                raise AgentError('TASK_NOT_RUNNING', 'Claim and verify a task before completing it')
            if status == 'pending' and task['status'] == 'in_progress':
                raise AgentError('TASK_STILL_RUNNING', 'Block or finish active work before releasing its claim')
            c.execute('UPDATE ultra_tasks SET status=?,owner=?,version=version+1,evidence=?,artifacts_json=?,updated_at=? WHERE task_id=?',
                      (status, None if status == 'pending' else task['owner'], public_text(evidence),
                       canonical_json([public_text(x, 1000) for x in (artifacts or [])[:32]]), self.store.clock(), task_id))
            return self._task_public(dict(c.execute('SELECT * FROM ultra_tasks WHERE task_id=?', (task_id,)).fetchone()))


    def prepare_root_rescue(self, team_id: str, requester_agent_id: str,
                            request_key: str, body_hash: str) -> dict:
        if not request_key or len(request_key) > 200:
            raise AgentError('IDEMPOTENCY_KEY_REQUIRED', 'Root rescue requires a stable idempotency key')
        now = self.store.clock()
        with self.store.transaction() as c:
            old = c.execute(
                'SELECT * FROM ultra_root_rescues WHERE team_id=? AND requester_agent_id=? AND request_key=?',
                (team_id, requester_agent_id, request_key),
            ).fetchone()
            if old:
                if old['body_hash'] != body_hash:
                    raise AgentError('IDEMPOTENCY_CONFLICT', 'Root rescue key was reused for another request')
                return dict(old)
            active = c.execute(
                "SELECT * FROM ultra_root_rescues WHERE team_id=? AND state IN ('prepared','submitting','reconciling') LIMIT 1",
                (team_id,),
            ).fetchone()
            if active:
                raise AgentError('ROOT_RESCUE_IN_PROGRESS', 'Another Root rescue request is already unresolved',
                                 rescue_id=active['rescue_id'])
            rescue_id = 'urescue_' + uuid.uuid4().hex
            c.execute(
                '''INSERT INTO ultra_root_rescues
                   (rescue_id,team_id,requester_agent_id,request_key,body_hash,state,created_at,updated_at)
                   VALUES(?,?,?,?,?,'prepared',?,?)''',
                (rescue_id, team_id, requester_agent_id, request_key, body_hash, now, now),
            )
            return dict(c.execute('SELECT * FROM ultra_root_rescues WHERE rescue_id=?', (rescue_id,)).fetchone())

    def root_rescue(self, rescue_id: str) -> dict:
        with self.store.connection() as c:
            row = c.execute('SELECT * FROM ultra_root_rescues WHERE rescue_id=?', (rescue_id,)).fetchone()
        if not row:
            raise AgentError('ROOT_RESCUE_NOT_FOUND', 'Root rescue request was not found')
        return dict(row)

    def active_root_rescue(self, team_id: str) -> dict | None:
        with self.store.connection() as c:
            row = c.execute(
                "SELECT * FROM ultra_root_rescues "
                "WHERE team_id=? AND state IN ('prepared','submitting','reconciling') "
                "ORDER BY created_at DESC LIMIT 1",
                (team_id,),
            ).fetchone()
        return dict(row) if row else None

    def update_root_rescue(self, rescue_id: str, expected_state: str, state: str,
                           *, new_root_lease_id: str | None = None,
                           conversation_id: str | None = None,
                           conversation_url: str | None = None,
                           error_class: str | None = None) -> dict:
        with self.store.transaction() as c:
            row = c.execute('SELECT * FROM ultra_root_rescues WHERE rescue_id=?', (rescue_id,)).fetchone()
            if not row:
                raise AgentError('ROOT_RESCUE_NOT_FOUND', 'Root rescue request was not found')
            if row['state'] != expected_state:
                if row['state'] == state:
                    return dict(row)
                raise AgentError('ROOT_RESCUE_STATE_CONFLICT', 'Root rescue state changed concurrently')
            c.execute(
                '''UPDATE ultra_root_rescues
                   SET state=?,new_root_lease_id=coalesce(?,new_root_lease_id),
                       conversation_id=coalesce(?,conversation_id),
                       conversation_url=coalesce(?,conversation_url),
                       error_class=?,updated_at=? WHERE rescue_id=?''',
                (state, new_root_lease_id, conversation_id, conversation_url,
                 error_class, self.store.clock(), rescue_id),
            )
            return dict(c.execute('SELECT * FROM ultra_root_rescues WHERE rescue_id=?', (rescue_id,)).fetchone())

    def commit_root_rescue(self, team_id: str, rescue_id: str, new_root_lease: dict,
                           conversation_id: str, conversation_url: str) -> dict:
        new_root_id = str(new_root_lease.get('lease_id') or '')
        new_thread = str(new_root_lease.get('thread_id') or '')
        new_controller = str(new_root_lease.get('controller_id') or '')
        if not new_root_id or not new_thread or not new_controller:
            raise AgentError('ROOT_RESCUE_OWNER_INVALID', 'Successor Root lease identity is incomplete')
        now = self.store.clock()
        with self.store.transaction() as c:
            rescue = c.execute('SELECT * FROM ultra_root_rescues WHERE rescue_id=? AND team_id=?',
                               (rescue_id, team_id)).fetchone()
            identity = c.execute('SELECT * FROM ultra_root_identity WHERE team_id=?', (team_id,)).fetchone()
            team = self._team(c, team_id)
            if not rescue or rescue['state'] not in {'submitting','reconciling'}:
                raise AgentError('ROOT_RESCUE_STATE_CONFLICT', 'Root rescue is not ready to commit')
            if not identity:
                raise AgentError('ROOT_IDENTITY_MISSING', 'Durable Root identity is missing')
            requester = rescue['requester_agent_id']
            member = c.execute(
                'SELECT * FROM ultra_members WHERE team_id=? AND agent_id=?',
                (team_id, requester),
            ).fetchone()
            binding = c.execute(
                'SELECT * FROM ultra_conversation_bindings WHERE team_id=? AND agent_id=?',
                (team_id, requester),
            ).fetchone()
            if not member or not binding:
                raise AgentError('ROOT_RESCUE_IDENTITY_INVALID', 'Rescue requester is no longer a durable Team member')
            if binding['conversation_id'] != conversation_id or binding['conversation_url'] != conversation_url:
                raise AgentError('ROOT_RESCUE_IDENTITY_INVALID', 'Successor Root must be the requester exact web conversation')
            c.execute('UPDATE ultra_teams SET root_lease_id=?,root_thread_id=?,state=? WHERE team_id=?',
                      (new_root_id, new_thread, 'active' if team['state'] == 'active' else team['state'], team_id))
            c.execute(
                '''UPDATE ultra_root_identity
                   SET generation=generation+1,controller_id=?,root_thread_id=?,
                       lease_workspace=?,owner_label=?,updated_at=? WHERE team_id=?''',
                (new_controller, new_thread,
                 str(new_root_lease.get('workspace') or identity['lease_workspace']),
                 str(new_root_lease.get('owner_label') or identity['owner_label']), now, team_id),
            )
            c.execute('UPDATE ultra_members SET participant_id=NULL,thread_id=NULL WHERE team_id=? AND agent_id=?',
                      (team_id, requester))
            c.execute("UPDATE ultra_members SET parent_actor='root' WHERE team_id=? AND parent_actor=?",
                      (team_id, requester))
            c.execute(
                "UPDATE ultra_tasks SET owner='root',updated_at=? WHERE team_id=? AND owner=? AND status IN ('in_progress','blocked')",
                (now, team_id, requester),
            )
            c.execute(
                '''UPDATE ultra_root_rescues SET state='verified',new_root_lease_id=?,
                   conversation_id=?,conversation_url=?,error_class=NULL,updated_at=? WHERE rescue_id=?''',
                (new_root_id, conversation_id, conversation_url, now, rescue_id),
            )
        requester_agent = self.store.get(rescue['requester_agent_id'])
        if requester_agent['state'] not in {'DONE','CLOSED','BLOCKED'}:
            with self.store.transaction() as c:
                c.execute(
                    "UPDATE agents SET completion_summary=?,updated_at=? WHERE logical_agent_id=?",
                    ('Promoted to Root after confirmed Root disconnect', self.store.clock(), rescue['requester_agent_id']),
                )
            self.store.set_state(rescue['requester_agent_id'], 'CLOSED')
        self._detach_dead_participants(team_id)
        return self.root_rescue(rescue_id)

    def team_for_root(self, root_lease_id: str) -> dict | None:
        with self.store.connection() as c:
            row = c.execute(
                "SELECT * FROM ultra_teams WHERE root_lease_id=? AND state IN ('active','stopping','finalized')",
                (root_lease_id,),
            ).fetchone()
        return dict(row) if row else None

    def notify_root_disconnect(self, team_id: str, root_lease_id: str, confirmation: dict) -> dict:
        with self.store.connection() as c:
            team = self._team(c, team_id)
            if team['root_lease_id'] != root_lease_id or team['state'] not in {'active','stopping'}:
                return {'status':'stale_root','notified':0}
            rows = c.execute(
                """SELECT m.agent_id,a.state,a.join_state
                   FROM ultra_members m JOIN agents a ON a.logical_agent_id=m.agent_id
                   WHERE m.team_id=? ORDER BY m.created_at,m.agent_id""",
                (team_id,),
            ).fetchall()
        probe_id = str(confirmation.get('probe_id') or '')
        deadline = str(confirmation.get('rescue_deadline_at') or '')
        notified = []
        for row in rows:
            if row['state'] in {'DONE','CLOSED','BLOCKED'} or row['join_state'] != 'joined':
                continue
            aid = row['agent_id']
            key = 'root-rescue:' + digest(canonical_json([team_id, root_lease_id, probe_id, aid]))[:48]
            message = self.post(
                team, 'system', aid,
                'Root 已被 Runtime 确认掉线；进入 5 分钟 Team rescue grace。'
                '如果你的当前 turn 仍在运行，请先 resume_context 核对 exact team/session，'
                '然后用自己的 root_rescue_token 调用 team_root_rescue。'
                f' rescue_deadline_at={deadline}。若其他成员已接班，不要重复抢占。',
                key=key, kind='blocker',
            )
            notified.append(message['message_id'])
        return {'status':'queued','notified':len(notified),'message_ids':notified}

    def build_completion_report(self, team: dict, *, completed_at: float | None = None) -> dict:
        finished = float(completed_at if completed_at is not None else self.store.clock())
        with self.store.connection() as c:
            members = c.execute(
                """SELECT m.agent_id,a.display_name,a.role,a.kind,a.state,a.completion_summary,
                          a.created_at,a.completed_at,b.local_tag
                   FROM ultra_members m
                   JOIN agents a ON a.logical_agent_id=m.agent_id
                   JOIN ultra_conversation_bindings b ON b.agent_id=m.agent_id
                   WHERE m.team_id=? ORDER BY m.created_at,m.agent_id""",
                (team['team_id'],),
            ).fetchall()
            task_rows = c.execute(
                "SELECT * FROM ultra_tasks WHERE team_id=? ORDER BY created_at,task_id",
                (team['team_id'],),
            ).fetchall()
        tasks = [self._task_public(dict(row)) for row in task_rows]
        children = []
        for row in members:
            if row['kind'] != 'business':
                continue
            owned = [
                {
                    'task_id': task['task_id'],
                    'title': task['title'],
                    'status': task['status'],
                    'evidence': task.get('evidence') or '',
                    'artifacts': task.get('artifacts') or [],
                }
                for task in tasks
                if task.get('owner') == row['agent_id'] or task.get('creator') == row['agent_id']
            ]
            children.append({
                'agent_id': row['agent_id'],
                'local_tag': row['local_tag'],
                'display_name': row['display_name'],
                'role': row['role'],
                'state': row['state'],
                'completion_summary': row['completion_summary'] or '',
                'tasks': owned,
            })
        return {
            'team_id': team['team_id'],
            'started_at_epoch': float(team['created_at']),
            'completed_at_epoch': finished,
            'duration_seconds': max(0.0, finished - float(team['created_at'])),
            'child_agent_count': len(children),
            'children': children,
            'task_count': len(tasks),
            'tasks': [
                {
                    'task_id': task['task_id'],
                    'title': task['title'],
                    'status': task['status'],
                    'evidence': task.get('evidence') or '',
                }
                for task in tasks
            ],
        }

    def validate_finalize(self, team: dict, root_lease_id: str) -> dict:
        if team['root_lease_id'] != root_lease_id:
            raise AgentError('TEAM_ROOT_REQUIRED', 'Only the current Team Root may finalize')
        with self.store.connection() as c:
            member_rows = c.execute(
                """SELECT m.agent_id,m.participant_id,a.state,a.kind
                   FROM ultra_members m JOIN agents a ON a.logical_agent_id=m.agent_id
                   WHERE m.team_id=?""",
                (team['team_id'],),
            ).fetchall()
            unfinished_members = [
                {'agent_id': row['agent_id'], 'state': row['state']}
                for row in member_rows
                if row['state'] not in {'DONE','CLOSED','BLOCKED'}
            ]
            unfinished_tasks = [
                {'task_id': row['task_id'], 'status': row['status']}
                for row in c.execute(
                    "SELECT task_id,status FROM ultra_tasks WHERE team_id=? AND status NOT IN ('done','cancelled')",
                    (team['team_id'],),
                ).fetchall()
            ]
            member_ids = [row['agent_id'] for row in member_rows]
            unresolved = []
            if member_ids:
                placeholders = ','.join('?' for _ in member_ids)
                unresolved = [
                    dict(row) for row in c.execute(
                        f"""SELECT operation_id,agent_id,kind,state FROM web_operations
                            WHERE agent_id IN ({placeholders})
                              AND state IN ('PREPARED','SUBMITTING','RECONCILING')""",
                        member_ids,
                    ).fetchall()
                ]
        active_participants = [
            row['participant_id'] for row in member_rows
            if row['participant_id'] and self.lease_resolver(row['participant_id'])
        ]
        if unfinished_members:
            raise AgentError('TEAM_MEMBERS_UNFINISHED', 'All Child Agents must be terminal before Team finalize',
                             members=unfinished_members)
        if unfinished_tasks:
            raise AgentError('TEAM_TASKS_UNFINISHED', 'All Team tasks must be done or cancelled before finalize',
                             tasks=unfinished_tasks)
        if unresolved:
            raise AgentError('TEAM_WEB_RECONCILIATION_REQUIRED', 'All uncertain web sends must be reconciled before finalize',
                             operations=unresolved)
        if active_participants:
            raise AgentError('TEAM_PARTICIPANTS_ACTIVE', 'All Child participant handles must exit before finalize',
                             participant_ids=active_participants)
        return {'status':'ready'}

    def completion(self, team_id: str) -> dict | None:
        with self.store.connection() as c:
            row = c.execute('SELECT * FROM ultra_completions WHERE team_id=?', (team_id,)).fetchone()
        if not row:
            return None
        result = dict(row)
        result['report'] = json.loads(result.pop('report_json'))
        return result

    def completion_for_root(self, root_lease_id: str) -> dict | None:
        with self.store.connection() as c:
            row = c.execute(
                '''SELECT x.* FROM ultra_completions x
                   JOIN ultra_teams t ON t.team_id=x.team_id WHERE t.root_lease_id=?''',
                (root_lease_id,),
            ).fetchone()
        if not row:
            return None
        result = dict(row)
        result['report'] = json.loads(result.pop('report_json'))
        return result

    def arm_completion(self, team: dict, root_lease_id: str, task_name: str,
                       summary: str, report: dict, recipient: str = '') -> dict:
        clean_name = public_text(task_name or 'GPTAgent Ultra task', 500)
        clean_summary = public_text(summary or '', 12000)
        if not clean_summary:
            raise AgentError('TEAM_FINAL_SUMMARY_REQUIRED', 'Root must provide a final public summary')
        now = self.store.clock()
        with self.store.transaction() as c:
            current = self._team(c, team['team_id'])
            old = c.execute('SELECT * FROM ultra_completions WHERE team_id=?', (team['team_id'],)).fetchone()
            if old:
                if old['root_lease_id'] != root_lease_id:
                    raise AgentError('TEAM_COMPLETION_OWNER_CHANGED', 'Completion was already armed by another Root generation')
                result = dict(old)
                result['report'] = json.loads(result.pop('report_json'))
                return result
            completion_id = 'ucomplete_' + uuid.uuid4().hex
            c.execute(
                '''INSERT INTO ultra_completions
                   (completion_id,team_id,root_lease_id,task_name,summary,report_json,recipient,state,attempts,
                    created_at,finalized_at,updated_at)
                   VALUES(?,?,?,?,?,?,?,'armed',0,?,?,?)''',
                (completion_id, team['team_id'], root_lease_id, clean_name, clean_summary,
                 canonical_json(report), public_text(recipient or '', 1000), now, now, now),
            )
            c.execute("UPDATE ultra_teams SET state='finalized' WHERE team_id=?", (team['team_id'],))
        return self.completion(team['team_id'])

    def pending_completions(self) -> list[dict]:
        with self.store.connection() as c:
            rows = c.execute(
                "SELECT * FROM ultra_completions WHERE state IN ('armed','failed','sending') ORDER BY finalized_at,completion_id"
            ).fetchall()
        return [dict(row) for row in rows]

    def claim_completion_notification(self, completion_id: str) -> dict:
        now = self.store.clock()
        with self.store.transaction() as c:
            row = c.execute('SELECT * FROM ultra_completions WHERE completion_id=?', (completion_id,)).fetchone()
            if not row:
                raise AgentError('TEAM_COMPLETION_NOT_FOUND', 'Completion notification record was not found')
            if row['state'] == 'sent':
                return {**dict(row), 'claim_status':'already_sent'}
            if row['state'] == 'sending' and now - float(row['updated_at']) < 600:
                return {**dict(row), 'claim_status':'in_progress'}
            if row['state'] == 'failed':
                backoff = min(1800.0, 60.0 * (2 ** min(int(row['attempts']), 5)))
                if now - float(row['updated_at']) < backoff:
                    return {**dict(row), 'claim_status':'retry_backoff', 'retry_after_seconds':backoff - (now - float(row['updated_at']))}
            token = 'uattempt_' + secrets.token_urlsafe(24)
            c.execute(
                """UPDATE ultra_completions
                   SET state='sending',attempts=attempts+1,attempt_token=?,last_error='',updated_at=?
                   WHERE completion_id=?""",
                (token, now, completion_id),
            )
            claimed = dict(c.execute('SELECT * FROM ultra_completions WHERE completion_id=?', (completion_id,)).fetchone())
            claimed['claim_status'] = 'claimed'
            return claimed

    def finish_completion_notification(self, completion_id: str, attempt_token: str, *,
                                       success: bool, gmail_message_id: str = '',
                                       error: str = '') -> dict:
        with self.store.transaction() as c:
            row = c.execute('SELECT * FROM ultra_completions WHERE completion_id=?', (completion_id,)).fetchone()
            if not row:
                raise AgentError('TEAM_COMPLETION_NOT_FOUND', 'Completion notification record was not found')
            if row['state'] == 'sent':
                return dict(row)
            if row['state'] != 'sending' or str(row['attempt_token'] or '') != str(attempt_token or ''):
                raise AgentError('TEAM_COMPLETION_CLAIM_STALE', 'Completion notification claim is stale')
            now = self.store.clock()
            if success:
                c.execute(
                    """UPDATE ultra_completions SET state='sent',gmail_message_id=?,last_error='',
                       sent_at=?,updated_at=? WHERE completion_id=?""",
                    (public_text(gmail_message_id or '', 500), now, now, completion_id),
                )
            else:
                c.execute(
                    """UPDATE ultra_completions SET state='failed',last_error=?,updated_at=?
                       WHERE completion_id=?""",
                    (public_text(error or 'completion email send failed', 2000), now, completion_id),
                )
            return dict(c.execute('SELECT * FROM ultra_completions WHERE completion_id=?', (completion_id,)).fetchone())

    def status(self, team: dict, actor: str) -> dict:
        with self.store.connection() as c:
            current = self._team(c, team['team_id'])
            rows = c.execute('''SELECT m.agent_id,m.parent_actor,m.participant_id,m.thread_id,m.write_paths_json,
                a.conversation_id,a.conversation_url,a.join_state,a.state,a.role,
                b.local_tag,b.project_id AS bound_project_id,b.verified_at,
                w.work_mode,w.estimated_minutes,w.refresh_interval_minutes,
                h.current_generation AS session_generation,
                s.state AS session_state,s.reason AS session_reason,s.created_at AS session_created_at
                FROM ultra_members m
                JOIN agents a ON a.logical_agent_id=m.agent_id
                JOIN ultra_conversation_bindings b ON b.agent_id=m.agent_id
                JOIN ultra_work_policy w ON w.agent_id=m.agent_id
                JOIN ultra_session_heads h ON h.agent_id=m.agent_id
                JOIN ultra_sessions s ON s.agent_id=m.agent_id AND s.generation=h.current_generation
                WHERE m.team_id=? ORDER BY m.created_at''', (team['team_id'],)).fetchall()
        return {'status': current['state'], 'team_id': team['team_id'], 'actor': actor,
                'project_name': PROJECT_NAME, 'project_id': team['project_id'],
                'members': [{**dict(r), 'write_paths': json.loads(r['write_paths_json'])} for r in rows],
                'tasks': self.tasks(team), 'model_background_scheduler': False}

    def shutdown(self, team, actor):
        if actor != 'root':
            raise AgentError('TEAM_ROOT_REQUIRED', 'Only the coordinator may request team shutdown')
        with self.store.transaction() as c:
            c.execute("UPDATE ultra_teams SET state='stopping' WHERE team_id=?", (team['team_id'],))
            members = c.execute('SELECT agent_id FROM ultra_members WHERE team_id=?', (team['team_id'],)).fetchall()
        for member in members:
            aid = member['agent_id']
            if self.store.get(aid)['state'] not in {'DONE', 'CLOSED', 'BLOCKED'}:
                self.post(team, actor, aid, '停止领取新任务；保存已验证结果、剩余事项和产物。处理自己的在途任务后 checkpoint → complete → report_progress(done)，不要释放根租约。', key='shutdown:' + aid, kind='shutdown_request')
        return {'status': 'stopping', 'new_turn': False, 'root_lease_released': False}
