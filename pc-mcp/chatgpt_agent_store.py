"""GPTAgent persistent business state. Execution authority remains in Runtime.

Transactions never span browser/network actions. Durable intents make uncertain
submissions reconcilable rather than retryable. Spawn tickets are stored hashed.
"""
from __future__ import annotations
from contextlib import contextmanager
import hashlib
import json
import os
from pathlib import Path
import re
import secrets
import sqlite3
import threading
import time
from typing import Any, Iterator
from urllib.parse import urlsplit
import uuid
from machine_config import MachineConfig


class AgentError(ValueError):
    def __init__(self, code: str, message: str, **details: Any):
        super().__init__(message)
        self.code, self.details = code, details


TERMINAL_AGENT_STATES = frozenset({'DONE', 'CLOSED', 'BLOCKED'})
AGENT_STATES = frozenset({'CREATED', 'RUNNING', 'IDLE', 'DEAD_RECOVERABLE', 'RECOVERING', 'BLOCKED', 'DONE', 'CLOSED', 'SUSPECT', 'DETACHED', 'EXTERNAL_ACTIVITY'})
OPEN_OPERATIONS = ('CLAIMED', 'PRECHECKED', 'SUBMITTING', 'SUBMITTED', 'RECONCILING')
AMBIGUOUS_OPERATIONS = ('SUBMITTING', 'SUBMITTED', 'RECONCILING')


def public_text(value: str, limit: int = 12000) -> str:
    """Defense in depth; callers must never collect hidden thoughts or headers."""
    value = str(value)
    value = re.sub(r'ga_ticket_[A-Za-z0-9_-]+', '[spawn-ticket-redacted]', value)
    value = re.sub(r'(?i)\bBearer\s+\S+', 'Bearer [redacted]', value)
    value = re.sub(r'\beyJ[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+', '[jwt-redacted]', value)
    value = re.sub(r'(?i)((?:access_token|refresh_token|authorization|cookie|api_key|password|secret)\s*[=:]\s*)[^\s,;]+', r'\1[redacted]', value)
    value = re.sub(r'\bsk-[A-Za-z0-9_-]{16,}', '[key-redacted]', value)
    return value[:limit]


def public_snapshot(value: Any, allowed: set[str]) -> dict:
    """Only known scalar/public-list fields enter snapshots, never raw results."""
    out = {}
    for key, item in (value or {}).items():
        if key not in allowed:
            continue
        if isinstance(item, str):
            out[key] = public_text(item, 1600)
        elif item is None or type(item) in {bool, int}:
            out[key] = item
        elif isinstance(item, list):
            out[key] = [public_text(x, 300) for x in item[:12] if isinstance(x, str)]
    return out


def canonical_json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(',', ':'), allow_nan=False)


def digest(text: str) -> str:
    return hashlib.sha256(text.encode('utf-8')).hexdigest()


def validate_conversation_url(conversation_id: str, url: str) -> None:
    p = urlsplit(url)
    if p.scheme != 'https' or p.hostname != 'chatgpt.com' or p.username or p.password or p.port not in {None, 443} or p.query or p.fragment:
        raise AgentError('INVALID_CONVERSATION_URL', 'Use an exact https://chatgpt.com conversation URL')
    if not re.fullmatch(r'[A-Za-z0-9_-]{8,100}', conversation_id) or not p.path.rstrip('/').endswith('/c/' + conversation_id):
        raise AgentError('CONVERSATION_MISMATCH', 'Conversation URL and id disagree')


_SCHEMA = '''
CREATE TABLE IF NOT EXISTS settings(key TEXT PRIMARY KEY, value_json TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS agents(
 logical_agent_id TEXT PRIMARY KEY, short_id TEXT UNIQUE NOT NULL,
 agent_path TEXT UNIQUE NOT NULL, parent_agent_id TEXT REFERENCES agents(logical_agent_id),
 root_agent_id TEXT NOT NULL, owner_thread_id TEXT NOT NULL,
 role TEXT NOT NULL, name_slug TEXT NOT NULL, display_name TEXT NOT NULL, web_title TEXT NOT NULL,
 kind TEXT NOT NULL DEFAULT 'business' CHECK(kind IN ('business','watchdog')),
 project_name TEXT NOT NULL, project_id TEXT NOT NULL,
 conversation_id TEXT UNIQUE, conversation_url TEXT UNIQUE, runtime_thread_id TEXT UNIQUE,
 workspace TEXT NOT NULL, join_state TEXT NOT NULL DEFAULT 'pending' CHECK(join_state IN ('pending','joined','detached')),
 state TEXT NOT NULL DEFAULT 'CREATED', turn_epoch INTEGER NOT NULL DEFAULT 0,
 requested_model TEXT NOT NULL DEFAULT 'non-pro', effective_model TEXT,
 requested_reasoning_effort TEXT NOT NULL DEFAULT 'extra_high', effective_reasoning_effort TEXT,
 model_policy_json TEXT NOT NULL DEFAULT '{}', pending_work TEXT NOT NULL DEFAULT '',
 progress_version INTEGER NOT NULL DEFAULT 0, progress_fingerprint TEXT,
 last_recovery_progress INTEGER NOT NULL DEFAULT -1, consecutive_recoveries INTEGER NOT NULL DEFAULT 0,
 no_progress_turns INTEGER NOT NULL DEFAULT 0, recovery_not_before REAL NOT NULL DEFAULT 0,
 created_at REAL NOT NULL, updated_at REAL NOT NULL, completed_at REAL, completion_summary TEXT, closed_at REAL
);
CREATE INDEX IF NOT EXISTS agent_parent_idx ON agents(parent_agent_id);
CREATE INDEX IF NOT EXISTS agent_project_idx ON agents(project_id,state);
CREATE TABLE IF NOT EXISTS spawn_tickets(
 ticket_hash TEXT PRIMARY KEY, logical_agent_id TEXT NOT NULL REFERENCES agents(logical_agent_id),
 expected_project_id TEXT NOT NULL, expected_conversation_id TEXT, parent_agent_id TEXT,
 expires_at REAL NOT NULL, consumed_at REAL, consumed_runtime_thread_id TEXT, status TEXT NOT NULL DEFAULT 'pending'
);
CREATE TABLE IF NOT EXISTS turns(
 agent_id TEXT NOT NULL REFERENCES agents(logical_agent_id), turn_epoch INTEGER NOT NULL,
 turn_exchange_id TEXT NOT NULL, user_message_id TEXT, topic_id TEXT,
 generation_started_at REAL NOT NULL, last_server_progress_at REAL, trusted_final_at REAL,
 termination_class TEXT, termination_at REAL, model_label TEXT, evidence_json TEXT NOT NULL DEFAULT '{}',
 PRIMARY KEY(agent_id,turn_epoch), UNIQUE(agent_id,turn_exchange_id)
);
CREATE TABLE IF NOT EXISTS checkpoints(
 checkpoint_id TEXT PRIMARY KEY, agent_id TEXT NOT NULL REFERENCES agents(logical_agent_id),
 turn_epoch INTEGER NOT NULL, seq INTEGER NOT NULL, summary TEXT NOT NULL, workspace TEXT NOT NULL,
 lease_id TEXT, runtime_snapshot_json TEXT NOT NULL, goal_snapshot_json TEXT NOT NULL, created_at REAL NOT NULL,
 UNIQUE(agent_id,seq)
);
CREATE TABLE IF NOT EXISTS conversation_messages(
 conversation_id TEXT NOT NULL, message_id TEXT NOT NULL, parent_message_id TEXT,
 logical_agent_id TEXT NOT NULL REFERENCES agents(logical_agent_id), turn_epoch INTEGER NOT NULL,
 role TEXT NOT NULL, content_type TEXT NOT NULL, visible_text TEXT NOT NULL,
 create_time REAL, content_sha256 TEXT NOT NULL, source TEXT NOT NULL, synced_at REAL NOT NULL,
 PRIMARY KEY(conversation_id,message_id)
);
CREATE TABLE IF NOT EXISTS history_documents(
 source_id TEXT PRIMARY KEY, agent_id TEXT NOT NULL REFERENCES agents(logical_agent_id), project_id TEXT NOT NULL,
 conversation_id TEXT, turn_epoch INTEGER NOT NULL, message_id TEXT, kind TEXT NOT NULL,
 timestamp REAL NOT NULL, text TEXT NOT NULL
);
CREATE VIRTUAL TABLE IF NOT EXISTS history_fts USING fts5(source_id UNINDEXED, text, tokenize='unicode61');
CREATE TABLE IF NOT EXISTS mailbox(
 message_id TEXT PRIMARY KEY, from_agent_id TEXT NOT NULL, to_agent_id TEXT NOT NULL REFERENCES agents(logical_agent_id),
 kind TEXT NOT NULL, content TEXT NOT NULL, created_at REAL NOT NULL, delivered_at REAL, acked_at REAL
);
CREATE TABLE IF NOT EXISTS handoffs(
 handoff_id TEXT PRIMARY KEY, child_agent_id TEXT NOT NULL REFERENCES agents(logical_agent_id),
 parent_agent_id TEXT REFERENCES agents(logical_agent_id), recipient_thread_id TEXT NOT NULL,
 status TEXT NOT NULL, checkpoint_id TEXT, turn_epoch INTEGER NOT NULL,
 summary TEXT NOT NULL, pending_work TEXT NOT NULL, artifacts_json TEXT NOT NULL DEFAULT '[]',
 completion_summary TEXT NOT NULL DEFAULT '', evidence TEXT NOT NULL,
 created_at REAL NOT NULL, delivered_at REAL, acked_at REAL
);
CREATE INDEX IF NOT EXISTS handoff_recipient_idx ON handoffs(recipient_thread_id,acked_at,created_at);
CREATE INDEX IF NOT EXISTS handoff_child_idx ON handoffs(child_agent_id,created_at);
CREATE TABLE IF NOT EXISTS recovery_claims(
 agent_id TEXT NOT NULL REFERENCES agents(logical_agent_id), turn_epoch INTEGER NOT NULL,
 claim_token TEXT NOT NULL, claimed_by TEXT NOT NULL, claimed_at REAL NOT NULL,
 expires_at REAL NOT NULL, status TEXT NOT NULL, PRIMARY KEY(agent_id,turn_epoch)
);
CREATE TABLE IF NOT EXISTS recovery_operations(
 operation_id TEXT PRIMARY KEY, agent_id TEXT NOT NULL REFERENCES agents(logical_agent_id),
 source_turn_epoch INTEGER NOT NULL, claim_token TEXT NOT NULL, state TEXT NOT NULL,
 expected_tail_message_id TEXT, submit_intent_sha256 TEXT NOT NULL,
 observed_user_message_id TEXT, observed_generation_request_id TEXT, observed_new_turn_exchange_id TEXT,
 created_at REAL NOT NULL, updated_at REAL NOT NULL, last_error_class TEXT, requested_text TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS recovery_agent_idx ON recovery_operations(agent_id,state);
CREATE TABLE IF NOT EXISTS watchdogs(
 generation INTEGER PRIMARY KEY AUTOINCREMENT, logical_agent_id TEXT UNIQUE,
 born_at REAL NOT NULL, retire_at REAL NOT NULL, replacement INTEGER, state TEXT NOT NULL, owner_thread_id TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS service_locks(name TEXT PRIMARY KEY, owner TEXT NOT NULL, expires_at REAL NOT NULL);
CREATE TABLE IF NOT EXISTS web_operations(
 operation_id TEXT PRIMARY KEY, idempotency_key TEXT UNIQUE NOT NULL,
 agent_id TEXT NOT NULL REFERENCES agents(logical_agent_id), kind TEXT NOT NULL,
 state TEXT NOT NULL, target_id TEXT, expected_conversation_id TEXT, expected_tail_message_id TEXT,
 prompt_sha256 TEXT, observed_user_message_id TEXT, observed_request_id TEXT, observed_turn_exchange_id TEXT,
 created_at REAL NOT NULL, updated_at REAL NOT NULL, error_class TEXT
);
CREATE INDEX IF NOT EXISTS web_operation_agent_idx ON web_operations(agent_id,state);
CREATE TABLE IF NOT EXISTS agent_events(
 seq INTEGER PRIMARY KEY AUTOINCREMENT, agent_id TEXT, kind TEXT NOT NULL,
 created_at REAL NOT NULL, payload_json TEXT NOT NULL DEFAULT '{}'
);
'''


class AgentStore:
    def __init__(self, config: MachineConfig, *, clock=time.time):
        self.config, self.path, self.clock = config, config.database_path, clock
        self.public_relations_path = self.path.with_name('agent-relations.json')
        self._public_relations_lock = threading.RLock()
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.connection() as c:
            if c.execute('PRAGMA user_version').fetchone()[0] > 2:
                raise AgentError('SCHEMA_TOO_NEW', 'Database belongs to a newer GPTAgent version')
            c.execute('PRAGMA journal_mode=WAL')
            c.executescript('BEGIN IMMEDIATE;\n' + _SCHEMA + '\nPRAGMA user_version=2;\nCOMMIT;')
        self._backfill_supervisor_scopes()
        self._repair_terminal_watchdog_agents()
        self._repair_terminal_submission_state()
        self._backfill_legacy_handoffs()
        self.sync_public_relations()

    @contextmanager
    def connection(self) -> Iterator[sqlite3.Connection]:
        c = sqlite3.connect(self.path, timeout=5, isolation_level=None)
        c.row_factory = sqlite3.Row
        c.execute('PRAGMA foreign_keys=ON')
        c.execute('PRAGMA busy_timeout=5000')
        c.execute('PRAGMA synchronous=FULL')
        try:
            yield c
        finally:
            c.close()

    @contextmanager
    def transaction(self) -> Iterator[sqlite3.Connection]:
        with self.connection() as c:
            c.execute('BEGIN IMMEDIATE')
            try:
                yield c
                c.commit()
            except BaseException:
                c.rollback()
                raise

    def _event(self, c, agent_id: str | None, kind: str, **payload: Any) -> None:
        c.execute('INSERT INTO agent_events(agent_id,kind,created_at,payload_json) VALUES(?,?,?,?)',
                  (agent_id, kind, self.clock(), canonical_json(payload)))

    def setting(self, key: str, default=None):
        with self.connection() as c:
            row = c.execute('SELECT value_json FROM settings WHERE key=?', (key,)).fetchone()
        return json.loads(row[0]) if row else default

    def set_setting(self, key: str, value: Any) -> None:
        with self.transaction() as c:
            c.execute('INSERT INTO settings VALUES(?,?) ON CONFLICT(key) DO UPDATE SET value_json=excluded.value_json', (key, canonical_json(value)))

    def settings_prefix(self, prefix: str) -> dict[str, Any]:
        if not isinstance(prefix,str) or not prefix:
            raise AgentError('SETTING_PREFIX_INVALID','Setting prefix is required')
        escaped=prefix.replace('\\','\\\\').replace('%','\\%').replace('_','\\_')
        with self.connection() as c:
            rows=c.execute("SELECT key,value_json FROM settings WHERE key LIKE ? ESCAPE '\\' ORDER BY key",(escaped+'%',)).fetchall()
        result={}
        for row in rows:
            try:
                result[row['key']]=json.loads(row['value_json'])
            except (TypeError,json.JSONDecodeError):
                continue
        return result

    def delete_setting(self, key: str) -> bool:
        with self.transaction() as c:
            return bool(c.execute('DELETE FROM settings WHERE key=?',(key,)).rowcount)

    @staticmethod
    def _agent_scope_key(agent_id: str) -> str:
        return 'agent_supervisor_scope:' + agent_id

    @staticmethod
    def _watchdog_scope_key(scope_thread_id: str) -> str:
        return 'watchdog_scope:' + scope_thread_id

    @staticmethod
    def _valid_scope_id(value: str) -> str:
        clean = str(value or '').strip()
        if not clean or len(clean) > 128 or any(
            not (character.isascii() and (character.isalnum() or character in '._:-'))
            for character in clean
        ):
            raise AgentError('WATCHDOG_SCOPE_INVALID', 'Watchdog scope must be a Runtime thread identity')
        return clean

    def _backfill_supervisor_scopes(self) -> None:
        """Assign legacy agents to the top-level Runtime conversation that created them."""
        with self.transaction() as c:
            rows = [dict(row) for row in c.execute(
                "SELECT * FROM agents WHERE kind='business' ORDER BY created_at,logical_agent_id"
            )]
            creator_by_thread = {
                str(row.get('runtime_thread_id') or ''): row['logical_agent_id']
                for row in rows if row.get('runtime_thread_id')
            }
            scope_by_agent: dict[str, str] = {}
            for row in rows:
                key = self._agent_scope_key(row['logical_agent_id'])
                existing = c.execute('SELECT value_json FROM settings WHERE key=?', (key,)).fetchone()
                scope = ''
                if existing:
                    try:
                        scope = self._valid_scope_id(json.loads(existing[0]))
                    except (ValueError, TypeError, json.JSONDecodeError, AgentError):
                        scope = ''
                if not scope:
                    owner_thread = self._valid_scope_id(row['owner_thread_id'])
                    creator_id = creator_by_thread.get(owner_thread)
                    scope = scope_by_agent.get(creator_id or '') or owner_thread
                    c.execute(
                        'INSERT INTO settings VALUES(?,?) ON CONFLICT(key) DO UPDATE SET value_json=excluded.value_json',
                        (key, canonical_json(scope)),
                    )
                scope_by_agent[row['logical_agent_id']] = scope

    def assign_supervisor_scope(self, agent_id: str, scope_thread_id: str) -> str:
        scope = self._valid_scope_id(scope_thread_id)
        with self.transaction() as c:
            self._row(c, agent_id)
            c.execute(
                'INSERT INTO settings VALUES(?,?) ON CONFLICT(key) DO UPDATE SET value_json=excluded.value_json',
                (self._agent_scope_key(agent_id), canonical_json(scope)),
            )
        self.sync_public_relations()
        return scope

    def agent_supervisor_scope(self, agent_id: str) -> str:
        with self.connection() as c:
            agent = self._row(c, agent_id)
            row = c.execute('SELECT value_json FROM settings WHERE key=?', (self._agent_scope_key(agent_id),)).fetchone()
        if row:
            try:
                return self._valid_scope_id(json.loads(row[0]))
            except (ValueError, TypeError, json.JSONDecodeError, AgentError):
                pass
        return self._valid_scope_id(agent['owner_thread_id'])

    def agents_in_supervisor_scope(self, scope_thread_id: str, *, limit: int = 200, offset: int = 0,
                                   live_only: bool = False, parent=None, path_prefix=None,
                                   state=None, query=None) -> list[dict]:
        scope = self._valid_scope_id(scope_thread_id)
        encoded = canonical_json(scope)
        clauses=['a.kind=?','s.value_json=?']
        params=['business',encoded]
        if live_only:
            clauses.append("a.state NOT IN ('DONE','CLOSED','BLOCKED')")
        if parent is not None:
            clauses.append('a.parent_agent_id=?')
            params.append(parent)
        if state is not None:
            clauses.append('a.state=?')
            params.append(state)
        if path_prefix:
            prefix=path_prefix.rstrip('/')+'/'
            clauses.append('(a.agent_path=? OR substr(a.agent_path,1,?)=?)')
            params.extend([path_prefix,len(prefix),prefix])
        if query:
            clauses.append('(instr(lower(a.web_title),lower(?))>0 OR instr(lower(a.display_name),lower(?))>0)')
            params.extend([query,query])
        with self.connection() as c:
            rows = c.execute(
                '''SELECT a.* FROM agents a JOIN settings s
                   ON s.key=('agent_supervisor_scope:' || a.logical_agent_id)
                   WHERE '''+' AND '.join(clauses)+
                   ' ORDER BY a.created_at,a.logical_agent_id LIMIT ? OFFSET ?',
                (*params,min(max(int(limit),1),200),max(int(offset),0)),
            ).fetchall()
        return [dict(row) for row in rows]

    def watchdog_scope(self, scope_thread_id: str) -> dict:
        scope = self._valid_scope_id(scope_thread_id)
        value = self.setting(self._watchdog_scope_key(scope), {})
        state = dict(value) if isinstance(value, dict) else {}
        state.setdefault('scope_thread_id', scope)
        state.setdefault('enabled', False)
        # New scopes use a real GPT Watchdog conversation for semantic
        # supervision. Persisted legacy scopes may still say ``program`` and
        # are migrated by AgentManager.start_background_services().
        state.setdefault('mode', 'manual')
        state.setdefault('manual_disabled', False)
        return state

    def watchdog_scopes(self, *, enabled_only: bool = False) -> list[dict]:
        with self.connection() as c:
            rows = c.execute("SELECT key,value_json FROM settings WHERE key LIKE 'watchdog_scope:%' ORDER BY key").fetchall()
        result = []
        for row in rows:
            scope = row['key'].split(':', 1)[1]
            try:
                value = json.loads(row['value_json'])
            except (TypeError, json.JSONDecodeError):
                continue
            if not isinstance(value, dict):
                continue
            state = {**value, 'scope_thread_id': scope}
            if enabled_only and not state.get('enabled'):
                continue
            result.append(state)
        return result

    def set_watchdog_scope(self, scope_thread_id: str, **updates: Any) -> dict:
        scope = self._valid_scope_id(scope_thread_id)
        current = self.watchdog_scope(scope)
        allowed = {
            'enabled', 'mode', 'manual_enabled', 'manual_disabled', 'started_at', 'stopped_at',
            'estimated_minutes', 'source', 'migrated_from_global', 'migrated_from_program', 'root_lease_id',
        }
        if set(updates) - allowed:
            raise AgentError('WATCHDOG_SCOPE_UPDATE_INVALID', 'Unsupported Watchdog scope field')
        mode = updates.get('mode', current.get('mode') or 'conversation')
        if mode not in {'manual', 'conversation', 'program'}:
            raise AgentError('WATCHDOG_MODE_INVALID', 'Watchdog mode must be manual or a readable legacy mode')
        state = {**current, **updates, 'scope_thread_id': scope, 'mode': mode}
        with self.transaction() as c:
            c.execute(
                'INSERT INTO settings VALUES(?,?) ON CONFLICT(key) DO UPDATE SET value_json=excluded.value_json',
                (self._watchdog_scope_key(scope), canonical_json(state)),
            )
        self.sync_public_relations()
        return state

    def sync_public_relations(self) -> dict:
        """Publish compact secret-free relationships for the Activity Console."""
        with self._public_relations_lock:
            with self.connection() as c:
                rows = c.execute(
                    '''SELECT a.logical_agent_id,a.short_id,a.agent_path,a.parent_agent_id,
                              a.runtime_thread_id,a.owner_thread_id,a.display_name,a.state,a.conversation_id,
                              s.value_json AS scope_json
                       FROM agents a LEFT JOIN settings s
                         ON s.key=('agent_supervisor_scope:' || a.logical_agent_id)
                       WHERE a.kind='business'
                       ORDER BY a.created_at,a.logical_agent_id'''
                ).fetchall()
            agents = []
            for row in rows:
                item = dict(row)
                raw_scope = item.pop('scope_json')
                try:
                    scope = json.loads(raw_scope) if raw_scope else item['owner_thread_id']
                    scope = self._valid_scope_id(scope)
                except (ValueError, TypeError, json.JSONDecodeError, AgentError):
                    continue
                conversation = str(item.pop('conversation_id') or '')
                agents.append({
                    'logical_agent_id': item['logical_agent_id'],
                    'short_id': item['short_id'],
                    'agent_path': item['agent_path'],
                    'parent_agent_id': item['parent_agent_id'],
                    'runtime_thread_id': item['runtime_thread_id'],
                    'scope_thread_id': scope,
                    'display_name': public_text(item['display_name'], 120),
                    'state': item['state'],
                    'conversation_id_suffix': conversation[-8:] if conversation else '',
                })
            watchdogs = []
            for scope in self.watchdog_scopes():
                # root_lease_id is an internal Runtime binding used by the
                # supervisor.  The Console only needs the public policy state.
                public_scope = {key: value for key, value in scope.items() if key != 'root_lease_id'}
                public_scope['mode'] = 'manual'
                public_scope['root_monitored'] = False
                watchdogs.append(public_scope)
            snapshot = {
                'version': 1,
                'updated_at': self.clock(),
                'agents': agents,
                'watchdogs': watchdogs,
            }
            target = self.public_relations_path
            target.parent.mkdir(parents=True, exist_ok=True)
            temporary = target.with_name('.' + target.name + '.' + uuid.uuid4().hex + '.tmp')
            try:
                temporary.write_text(canonical_json(snapshot), encoding='utf-8')
                for attempt in range(5):
                    try:
                        os.replace(temporary, target)
                        break
                    except PermissionError:
                        if attempt == 4:
                            raise
                        time.sleep(0.1 * (attempt + 1))
            finally:
                try:
                    temporary.unlink(missing_ok=True)
                except OSError:
                    pass
            return snapshot

    @staticmethod
    def _row(c, agent_id: str) -> dict:
        row = c.execute('SELECT * FROM agents WHERE logical_agent_id=?', (agent_id,)).fetchone()
        if not row:
            raise AgentError('AGENT_NOT_FOUND', 'No such logical agent')
        return dict(row)

    def get(self, ref: str) -> dict:
        if not isinstance(ref, str) or not ref.strip():
            raise AgentError('AGENT_REF_REQUIRED', 'Specify a logical id, full path, or unique short id')
        with self.connection() as c:
            for column in ('logical_agent_id', 'agent_path'):
                row = c.execute(f'SELECT * FROM agents WHERE {column}=?', (ref,)).fetchone()
                if row:
                    return dict(row)
            short = ref.removeprefix('[GA·').removesuffix(']').removeprefix('GA·').upper()
            row = c.execute('SELECT * FROM agents WHERE short_id=?', (short,)).fetchone()
            if row:
                return dict(row)
            rows = c.execute('SELECT * FROM agents WHERE instr(lower(name_slug),lower(?))>0 OR instr(lower(display_name),lower(?))>0 OR instr(lower(web_title),lower(?))>0 LIMIT 20', (ref, ref, ref)).fetchall()
        if not rows:
            raise AgentError('AGENT_NOT_FOUND', 'No agent matches the reference')
        raise AgentError('AGENT_REF_CANDIDATES', 'Keywords discover candidates; use an exact id or path to select',
                         candidates=[{k: r[k] for k in ('logical_agent_id','agent_path','short_id','display_name')} for r in rows])

    def by_thread(self, thread_id: str) -> dict | None:
        with self.connection() as c:
            row = c.execute('SELECT * FROM agents WHERE runtime_thread_id=?', (thread_id,)).fetchone()
        return dict(row) if row else None

    def agents(self, *, project_id=None, root_agent_id=None, parent=None, path_prefix=None, state=None, query=None, limit=100, offset=0) -> list[dict]:
        clauses, params = [], []
        for column, val in [('project_id', project_id), ('root_agent_id', root_agent_id), ('parent_agent_id', parent), ('state', state)]:
            if val is not None:
                clauses.append(f'{column}=?')
                params.append(val)
        if path_prefix:
            prefix = path_prefix.rstrip('/') + '/'
            clauses.append('(agent_path=? OR substr(agent_path,1,?)=?)')
            params.extend([path_prefix, len(prefix), prefix])
        if query:
            clauses.append('(instr(lower(web_title),lower(?))>0 OR instr(lower(display_name),lower(?))>0)')
            params.extend([query, query])
        sql = 'SELECT * FROM agents' + (' WHERE ' + ' AND '.join(clauses) if clauses else '') + ' ORDER BY created_at,logical_agent_id LIMIT ? OFFSET ?'
        with self.connection() as c:
            return [dict(r) for r in c.execute(sql, (*params, min(max(int(limit),1),200), max(int(offset),0)))]

    def create(self, *, name: str, role: str, workspace: str, project_id: str, owner_thread_id: str,
               parent_id: str | None = None, display_name: str | None = None, kind: str = 'business',
               policy: dict | None = None, watchdog_generation: int | None = None,
               delegation_reason: str | None = None) -> dict:
        if kind not in {'business', 'watchdog'} or not project_id or not owner_thread_id or not role.strip():
            raise AgentError('INVALID_AGENT', 'Agent kind, project, caller and role are required')
        if not re.fullmatch(r'[a-z0-9][a-z0-9-]{1,27}', name) or re.fullmatch(r'(?:task\d*|child-?\d*|new-chat|subtask\d*)', name):
            raise AgentError('MEANINGLESS_NAME', 'Use a semantic kebab-case name, 2–28 characters')
        policy = policy or {'requested_model': 'non-pro', 'requested_reasoning_effort': 'extra_high'}
        agent_id, now = 'ag_' + uuid.uuid4().hex, self.clock()
        short = agent_id[3:9].upper()
        with self.transaction() as c:
            # Business capacity and Watchdog capacity are isolated. A full
            # business tree must never be able to evict/deny its supervisor.
            # Conversely, stale Watchdog generations cannot consume business
            # slots. Each pool keeps the same hard max as a final safety cap.
            if self.config.ultra_collaboration:
                # Drafts are not model work. Joined workers and ambiguous/accepted
                # submissions still reserve capacity, including historical ones.
                live = c.execute("""SELECT count(*) FROM agents a WHERE a.kind=?
                    AND a.state NOT IN ('DONE','CLOSED','BLOCKED')
                    AND (a.join_state='joined' OR EXISTS (
                        SELECT 1 FROM web_operations w WHERE w.agent_id=a.logical_agent_id
                        AND w.state IN ('SUBMITTING','RECONCILING','OBSERVED')))""", (kind,)).fetchone()[0]
            else:
                live = c.execute("SELECT count(*) FROM agents WHERE kind=? AND state NOT IN ('DONE','CLOSED','BLOCKED')", (kind,)).fetchone()[0]
            if live >= self.config.max_live_agents:
                code = 'WATCHDOG_LIMIT' if kind == 'watchdog' else 'AGENT_LIMIT'
                raise AgentError(code, 'Maximum live-agent limit reached for this agent class')
            while c.execute('SELECT 1 FROM agents WHERE short_id=?', (short,)).fetchone():
                agent_id = 'ag_' + uuid.uuid4().hex
                short = agent_id[3:9].upper()
            if kind == 'watchdog':
                if watchdog_generation is None or type(watchdog_generation) is not int or watchdog_generation < 1:
                    raise AgentError('WATCHDOG_GENERATION_REQUIRED', 'Watchdogs require a persistent supervisor generation')
                short, path = f'WD-{watchdog_generation:03d}', f'/watchdog/wd-{watchdog_generation:03d}'
                root_id, parent_id = parent_id or agent_id, None
            elif parent_id:
                parent = self._row(c, parent_id)
                if parent['kind'] != 'business' or parent['state'] in TERMINAL_AGENT_STATES or parent['project_id'] != project_id:
                    raise AgentError('INVALID_PARENT', 'Parent is terminal, a watchdog, or in another project')
                count = c.execute('SELECT count(*) FROM agents WHERE parent_agent_id=?', (parent_id,)).fetchone()[0]
                if count >= self.config.max_children_per_agent:
                    raise AgentError('CHILD_LIMIT', 'Maximum child count reached')
                live_children = c.execute("""SELECT count(*) FROM agents a WHERE a.parent_agent_id=? AND ((a.join_state='joined' AND a.state NOT IN ('DONE','CLOSED','BLOCKED')) OR (a.join_state!='joined' AND EXISTS (SELECT 1 FROM web_operations w WHERE w.agent_id=a.logical_agent_id AND w.state IN ('SUBMITTING','RECONCILING'))))""", (parent_id,)).fetchone()[0]
                if live_children >= self.config.max_parallel_children_per_agent:
                    raise AgentError('PARALLEL_CHILD_LIMIT', 'Finish or close an existing child before spawning another one',
                                     max_parallel_children=self.config.max_parallel_children_per_agent)
                reason = public_text(delegation_reason or '', 1000).strip()
                if len(reason) < 12:
                    raise AgentError('DELEGATION_REASON_REQUIRED', 'Child spawn requires a concrete delegation reason (at least 12 characters)')
                path, root_id = parent['agent_path'] + '/' + name, parent['root_agent_id']
                if len(path.strip('/').split('/')) - 1 > self.config.max_depth:
                    raise AgentError('DEPTH_LIMIT', 'Maximum agent depth reached')
            else:
                # A native top-level Conversation is a controller, not a joined
                # logical Agent. Its delegated roots need independent stable
                # identities; never borrow the historical global /root.
                siblings = c.execute(
                    "SELECT logical_agent_id,state,join_state FROM agents WHERE kind='business' AND parent_agent_id IS NULL AND owner_thread_id=? AND project_id=?",
                    (owner_thread_id, project_id),
                ).fetchall()
                if len(siblings) >= self.config.max_children_per_agent:
                    raise AgentError('CHILD_LIMIT', 'Maximum native-controller worker count reached')
                if self.config.ultra_collaboration:
                    parallel_siblings = sum(row['state'] not in TERMINAL_AGENT_STATES and
                        (row['join_state']=='joined' or c.execute(
                            "SELECT 1 FROM web_operations WHERE agent_id=? AND state IN ('SUBMITTING','RECONCILING')",
                            (row['logical_agent_id'],)).fetchone() is not None) for row in siblings)
                else:
                    parallel_siblings = sum(row['state'] not in TERMINAL_AGENT_STATES for row in siblings)
                if parallel_siblings >= self.config.max_parallel_children_per_agent:
                    raise AgentError('PARALLEL_CHILD_LIMIT', 'Finish a native-controller worker before spawning another one',
                                     max_parallel_children=self.config.max_parallel_children_per_agent)
                path, root_id = '/root', agent_id
                if c.execute('SELECT 1 FROM agents WHERE agent_path=?', (path,)).fetchone():
                    path = '/root-' + name[:21] + '-' + short.lower()
            if c.execute('SELECT 1 FROM agents WHERE agent_path=?', (path,)).fetchone():
                if path == '/root' or kind == 'watchdog':
                    raise AgentError('ROOT_OR_GENERATION_EXISTS', 'Use the existing stable identity; do not replace it')
                name = name[:21] + '-' + short.lower()
                path = path.rsplit('/', 1)[0] + '/' + name
            clean_role = public_text(role, 500)
            title_path = '/'.join(path.strip('/').split('/')[-3:])
            keywords = re.sub(r'^(?:(?:负责|处理|子任务|执行|完成)[ ：:]*)+', '', clean_role) or clean_role
            title = f'[GA·{short}] {title_path} · {keywords[:60]}'
            if kind == 'watchdog':
                title = f'[GA·{short}] watchdog · gen{watchdog_generation:03d} · born-{time.strftime("%H%M", time.localtime(now))}'
            c.execute('''INSERT INTO agents(logical_agent_id,short_id,agent_path,parent_agent_id,root_agent_id,owner_thread_id,
              role,name_slug,display_name,web_title,kind,project_name,project_id,workspace,requested_model,requested_reasoning_effort,
              model_policy_json,pending_work,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)''',
              (agent_id, short, path, parent_id, root_id, owner_thread_id, clean_role, name, public_text(display_name or role, 120), title,
               kind, self.config.project_name, project_id, workspace, policy['requested_model'], policy['requested_reasoning_effort'],
               canonical_json(policy), clean_role, now, now))
            row = self._row(c, agent_id)
            self._index(c, row, 'agent:' + agent_id, title + '\n' + clean_role, 'agent', now)
            self._event(c, agent_id, 'agent.created', delegation_reason=public_text(delegation_reason or '',1000) if parent_id else '')
            return row

    def bind_conversation(self, agent_id: str, conversation_id: str, url: str, project_id: str) -> dict:
        validate_conversation_url(conversation_id, url)
        with self.transaction() as c:
            a = self._row(c, agent_id)
            if a['project_id'] != project_id or (a['conversation_id'] and a['conversation_id'] != conversation_id):
                raise AgentError('IDENTITY_MISMATCH', 'Project/conversation binding cannot change')
            try:
                c.execute('UPDATE agents SET conversation_id=?,conversation_url=?,updated_at=? WHERE logical_agent_id=?', (conversation_id, url, self.clock(), agent_id))
            except sqlite3.IntegrityError as exc:
                raise AgentError('CONVERSATION_ALREADY_BOUND', 'A conversation cannot belong to multiple logical agents') from exc
            c.execute("UPDATE spawn_tickets SET expected_conversation_id=? WHERE logical_agent_id=? AND status='pending'", (conversation_id, agent_id))
            c.execute('UPDATE history_documents SET conversation_id=? WHERE agent_id=?', (conversation_id, agent_id))
            return self._row(c, agent_id)

    def issue_ticket(self, agent_id: str, *, ttl_seconds: int = 1800) -> str:
        raw = 'ga_ticket_' + secrets.token_urlsafe(32)
        with self.transaction() as c:
            a = self._row(c, agent_id)
            if a['join_state'] == 'joined' or a['state'] in TERMINAL_AGENT_STATES:
                raise AgentError('JOIN_NOT_ALLOWED', 'Agent is already joined or terminal')
            c.execute("UPDATE spawn_tickets SET status='revoked' WHERE logical_agent_id=? AND status='pending'", (agent_id,))
            c.execute('''INSERT INTO spawn_tickets(ticket_hash,logical_agent_id,expected_project_id,expected_conversation_id,parent_agent_id,expires_at)
                         VALUES(?,?,?,?,?,?)''', (digest(raw), agent_id, a['project_id'], a['conversation_id'], a['parent_agent_id'], self.clock() + min(max(ttl_seconds,30),7200)))
        return raw

    def ticket_target(self, raw_ticket: str) -> dict:
        with self.connection() as c:
            row = c.execute('SELECT * FROM spawn_tickets WHERE ticket_hash=?', (digest(raw_ticket),)).fetchone()
            if not row or row['status'] != 'pending' or row['expires_at'] <= self.clock():
                raise AgentError('TICKET_INVALID', 'Spawn ticket is absent, expired, revoked or already consumed')
            return {**self._row(c,row['logical_agent_id']), 'expected_conversation_id': row['expected_conversation_id']}

    def join(self, raw_ticket: str, *, runtime_thread_id: str, project_id: str, conversation_id: str, browser_verified: bool) -> dict:
        if not runtime_thread_id or not browser_verified:
            raise AgentError('JOIN_PROOF_REQUIRED', 'Join requires an active Runtime identity and independent browser verification')
        with self.transaction() as c:
            ticket = c.execute('SELECT * FROM spawn_tickets WHERE ticket_hash=?', (digest(raw_ticket),)).fetchone()
            if not ticket or ticket['status'] != 'pending' or ticket['expires_at'] <= self.clock():
                raise AgentError('TICKET_INVALID', 'Spawn ticket is absent, expired, revoked or already consumed')
            a = self._row(c, ticket['logical_agent_id'])
            if not ticket['expected_conversation_id']:
                raise AgentError('SPAWN_BINDING_PENDING', 'Initial submission has not yet yielded a verified conversation')
            if (ticket['expected_project_id'] != project_id or ticket['expected_conversation_id'] != conversation_id
                    or a['conversation_id'] != conversation_id or a['state'] in TERMINAL_AGENT_STATES):
                raise AgentError('JOIN_IDENTITY_MISMATCH', 'Ticket does not authorize this project/conversation')
            try:
                c.execute("UPDATE agents SET runtime_thread_id=?,join_state='joined',state='RUNNING',updated_at=? WHERE logical_agent_id=?", (runtime_thread_id,self.clock(),a['logical_agent_id']))
            except sqlite3.IntegrityError as exc:
                raise AgentError('THREAD_ALREADY_BOUND', 'A Runtime thread is already bound to another agent') from exc
            c.execute("UPDATE spawn_tickets SET status='consumed',consumed_at=?,consumed_runtime_thread_id=? WHERE ticket_hash=? AND status='pending'", (self.clock(),runtime_thread_id,digest(raw_ticket)))
            self._event(c,a['logical_agent_id'],'agent.joined')
            return self._row(c,a['logical_agent_id'])

    def set_state(self, agent_id: str, state: str, *, expected: str | None = None) -> dict:
        if state not in AGENT_STATES or state == 'DONE':
            raise AgentError('INVALID_STATE', 'DONE is reachable only through explicit complete')
        with self.transaction() as c:
            a = self._row(c,agent_id)
            if expected and a['state'] != expected:
                raise AgentError('STATE_CONFLICT', 'Agent state changed during operation')
            if a['state'] in TERMINAL_AGENT_STATES and state != 'CLOSED':
                raise AgentError('TERMINAL_AGENT', 'Terminal agents cannot be automatically revived')
            c.execute("UPDATE agents SET state=?,updated_at=?,closed_at=CASE WHEN ?='CLOSED' THEN ? ELSE closed_at END WHERE logical_agent_id=?", (state,self.clock(),state,self.clock(),agent_id))
            if state in {'CLOSED','BLOCKED','EXTERNAL_ACTIVITY'}:
                c.execute("UPDATE recovery_claims SET status='revoked' WHERE agent_id=? AND status='active'", (agent_id,))
                c.execute("UPDATE recovery_operations SET state='ABORTED',last_error_class=?,updated_at=? WHERE agent_id=? AND state IN ('CLAIMED','PRECHECKED')", (state,self.clock(),agent_id))
                self._terminalize_native_state(c,agent_id,state)
                current=self._row(c,agent_id)
                self._write_handoff_locked(c,current,status=state.lower(),evidence='terminal-state:'+state)
            self._event(c,agent_id,'agent.state',state=state)
            return self._row(c,agent_id)

    def set_effective_policy(self, agent_id: str, model: str, effort: str) -> None:
        with self.transaction() as c:
            c.execute('UPDATE agents SET effective_model=?,effective_reasoning_effort=?,updated_at=? WHERE logical_agent_id=?', (public_text(model,100),effort,self.clock(),agent_id))

    def record_turn(self, agent_id: str, *, turn_exchange_id: str, user_message_id: str | None = None,
                    topic_id: str | None = None, model_label: str | None = None) -> dict:
        if not turn_exchange_id:
            raise AgentError('TURN_ID_REQUIRED', 'A real observed turn_exchange_id is required')
        with self.transaction() as c:
            a = self._row(c,agent_id)
            row = c.execute('SELECT * FROM turns WHERE agent_id=? AND turn_exchange_id=?', (agent_id,turn_exchange_id)).fetchone()
            if row:
                return dict(row)
            epoch = a['turn_epoch'] + 1
            c.execute('INSERT INTO turns(agent_id,turn_epoch,turn_exchange_id,user_message_id,topic_id,generation_started_at,model_label) VALUES(?,?,?,?,?,?,?)', (agent_id,epoch,turn_exchange_id,user_message_id,topic_id,self.clock(),model_label))
            state = a['state'] if a['state'] in TERMINAL_AGENT_STATES | {'EXTERNAL_ACTIVITY'} else 'RUNNING'
            c.execute('UPDATE agents SET turn_epoch=?,state=?,updated_at=? WHERE logical_agent_id=?', (epoch,state,self.clock(),agent_id))
            self._event(c,agent_id,'turn.started',turn_epoch=epoch)
            return dict(c.execute('SELECT * FROM turns WHERE agent_id=? AND turn_epoch=?', (agent_id,epoch)).fetchone())

    def latest_turn(self, agent_id: str) -> dict | None:
        with self.connection() as c:
            row = c.execute('SELECT * FROM turns WHERE agent_id=? ORDER BY turn_epoch DESC LIMIT 1', (agent_id,)).fetchone()
        return dict(row) if row else None

    def observe_turn(self, agent_id: str, epoch: int, *, termination_class: str | None = None,
                     progress: bool = False, evidence: dict | None = None) -> None:
        safe = {k: bool(v) for k,v in (evidence or {}).items() if k in {'sse','ws_exact_topic','graph_terminal','dom_active','async_active','request_seen','transport_only'}}
        if termination_class not in {None,'trusted_final','failed','cancelled','transport_only','unknown'}:
            raise AgentError('INVALID_TERMINATION', 'Unknown termination classification')
        with self.transaction() as c:
            if not c.execute('SELECT 1 FROM turns WHERE agent_id=? AND turn_epoch=?', (agent_id,epoch)).fetchone():
                raise AgentError('TURN_NOT_FOUND', 'Cannot update an unbound turn')
            now = self.clock()
            c.execute('UPDATE turns SET last_server_progress_at=CASE WHEN ? THEN ? ELSE last_server_progress_at END,evidence_json=? WHERE agent_id=? AND turn_epoch=?', (bool(progress),now,canonical_json(safe),agent_id,epoch))
            if termination_class in {'trusted_final','failed','cancelled'}:
                c.execute("UPDATE turns SET termination_class=?,termination_at=?,trusted_final_at=CASE WHEN ?='trusted_final' THEN ? ELSE trusted_final_at END WHERE agent_id=? AND turn_epoch=?", (termination_class,now,termination_class,now,agent_id,epoch))
                next_state = 'DEAD_RECOVERABLE' if termination_class == 'failed' else 'IDLE'
                c.execute("UPDATE agents SET state=?,updated_at=? WHERE logical_agent_id=? AND turn_epoch=? AND state IN ('RUNNING','SUSPECT','DETACHED','IDLE','DEAD_RECOVERABLE')", (next_state,now,agent_id,epoch))
                handoff_status='turn_final' if termination_class=='trusted_final' else 'interrupted'
                self._write_handoff_locked(c,self._row(c,agent_id),status=handoff_status,evidence='turn:'+termination_class)
                self._event(c,agent_id,'turn.terminal',turn_epoch=epoch,termination_class=termination_class)

    def _index(self, c, agent: dict, source_id: str, text: str, kind: str, timestamp: float, *, message_id=None, turn_epoch=None) -> None:
        text = public_text(text)
        epoch = agent['turn_epoch'] if turn_epoch is None else turn_epoch
        c.execute('''INSERT INTO history_documents VALUES(?,?,?,?,?,?,?,?,?) ON CONFLICT(source_id) DO UPDATE SET
                     text=excluded.text,timestamp=excluded.timestamp,conversation_id=excluded.conversation_id,turn_epoch=excluded.turn_epoch''',
                  (source_id,agent['logical_agent_id'],agent['project_id'],agent['conversation_id'],epoch,message_id,kind,timestamp,text))
        c.execute('DELETE FROM history_fts WHERE source_id=?', (source_id,))
        c.execute('INSERT INTO history_fts(source_id,text) VALUES(?,?)', (source_id,text))

    def checkpoint(self, agent_id: str, summary: str, *, lease_id=None, runtime_snapshot=None,
                   goal_snapshot=None, pending_work: str | None = None,
                   artifacts: list[str] | None = None) -> dict:
        if not summary.strip():
            raise AgentError('CHECKPOINT_REQUIRED', 'A public progress summary is required')
        initial=self.get(agent_id)
        artifact_manifest=self._artifact_manifest(initial['workspace'],artifacts)
        runtime_safe = public_snapshot(runtime_snapshot, {'workspace','lease_id','successful_action_count','side_effect_action_count','last_tool','job_ids'})
        goal_safe = public_snapshot(goal_snapshot, {'outcome','success_criteria','requirements','revision','status'})
        summary = public_text(summary,4000)
        runtime_json, goal_json = canonical_json(runtime_safe), canonical_json(goal_safe)
        with self.transaction() as c:
            a = self._row(c,agent_id)
            if a['join_state'] != 'joined' or a['state'] in TERMINAL_AGENT_STATES:
                raise AgentError('CHECKPOINT_NOT_ALLOWED', 'Only a joined nonterminal agent can checkpoint')
            seq = c.execute('SELECT coalesce(max(seq),0)+1 FROM checkpoints WHERE agent_id=?', (agent_id,)).fetchone()[0]
            checkpoint_id = 'cp_' + uuid.uuid4().hex
            pending = public_text(pending_work,2000) if pending_work is not None else a['pending_work']
            fingerprint = digest(summary + '\n' + pending + '\n' + runtime_json)
            version = a['progress_version'] + int(fingerprint != a['progress_fingerprint'])
            c.execute('INSERT INTO checkpoints VALUES(?,?,?,?,?,?,?,?,?,?)', (checkpoint_id,agent_id,a['turn_epoch'],seq,summary,a['workspace'],lease_id,runtime_json,goal_json,self.clock()))
            c.execute('UPDATE agents SET progress_version=?,progress_fingerprint=?,pending_work=?,updated_at=? WHERE logical_agent_id=?', (version,fingerprint,pending,self.clock(),agent_id))
            self._index(c,a,'checkpoint:'+checkpoint_id,summary,'checkpoint',self.clock())
            self._event(c,agent_id,'agent.checkpoint',seq=seq)
            checkpoint=dict(c.execute('SELECT * FROM checkpoints WHERE checkpoint_id=?', (checkpoint_id,)).fetchone())
            self._write_handoff_locked(
                c,self._row(c,agent_id),status='checkpoint',checkpoint=checkpoint,
                summary=summary,pending_work=pending,artifacts=artifact_manifest,evidence='checkpoint:'+str(seq),
            )
            return checkpoint

    def latest_checkpoint(self, agent_id: str) -> dict | None:
        with self.connection() as c:
            row = c.execute('SELECT * FROM checkpoints WHERE agent_id=? ORDER BY seq DESC LIMIT 1', (agent_id,)).fetchone()
        return dict(row) if row else None

    def _artifact_manifest(self, workspace: str, artifacts: list[str] | None) -> list[dict]:
        if artifacts is None:
            return []
        if not isinstance(artifacts,list) or len(artifacts) > 24 or any(not isinstance(item,str) for item in artifacts):
            raise AgentError('ARTIFACTS_INVALID','checkpoint artifacts must be a list of at most 24 workspace paths')
        try:
            root=Path(workspace).resolve(strict=False)
        except OSError as exc:
            raise AgentError('ARTIFACTS_INVALID','Agent workspace cannot be resolved') from exc
        manifest=[]
        seen=set()
        for raw in artifacts:
            clean=public_text(raw,1000).strip()
            if not clean:
                continue
            candidate=Path(clean)
            try:
                resolved=(candidate if candidate.is_absolute() else root/candidate).resolve(strict=False)
                relative=resolved.relative_to(root).as_posix()
            except (OSError,ValueError) as exc:
                raise AgentError('ARTIFACT_OUTSIDE_WORKSPACE','handoff artifacts must stay inside the Child workspace',artifact=clean) from exc
            if relative in seen:
                continue
            seen.add(relative)
            exists=resolved.exists()
            kind='missing'
            size=None
            sha256=None
            if exists:
                try:
                    if resolved.is_file():
                        kind='file'; size=resolved.stat().st_size
                        if size <= 16 * 1024 * 1024:
                            sha256=hashlib.sha256(resolved.read_bytes()).hexdigest()
                    elif resolved.is_dir():
                        kind='directory'
                    else:
                        kind='other'
                except OSError:
                    kind='unreadable'
            manifest.append({'path':relative,'kind':kind,'exists':exists,'size':size,'sha256':sha256})
        return manifest

    def _handoff_recipient_locked(self,c,child: dict) -> tuple[str | None,str] | None:
        if child.get('kind')!='business':
            return None
        parent_id=child.get('parent_agent_id')
        if parent_id:
            parent=self._row(c,parent_id)
            recipient=str(parent.get('runtime_thread_id') or parent.get('owner_thread_id') or '').strip()
        else:
            recipient=str(child.get('owner_thread_id') or '').strip()
        return (parent_id,recipient) if recipient else None

    def _latest_checkpoint_locked(self,c,agent_id: str) -> dict | None:
        row=c.execute('SELECT * FROM checkpoints WHERE agent_id=? ORDER BY seq DESC LIMIT 1',(agent_id,)).fetchone()
        return dict(row) if row else None

    def _latest_artifacts_locked(self,c,agent_id: str) -> list[dict]:
        row=c.execute("SELECT artifacts_json FROM handoffs WHERE child_agent_id=? AND artifacts_json!='[]' ORDER BY created_at DESC LIMIT 1",(agent_id,)).fetchone()
        if not row:
            return []
        try:
            value=json.loads(row['artifacts_json'])
        except (TypeError,json.JSONDecodeError):
            return []
        return value if isinstance(value,list) else []

    def _write_handoff_locked(self,c,child: dict,*,status: str,checkpoint: dict | None = None,
                              summary: str | None = None,pending_work: str | None = None,
                              artifacts: list[dict] | None = None,completion_summary: str | None = None,
                              evidence: str | None = None) -> dict | None:
        recipient=self._handoff_recipient_locked(c,child)
        if recipient is None:
            return None
        parent_id,recipient_thread_id=recipient
        checkpoint=checkpoint or self._latest_checkpoint_locked(c,child['logical_agent_id'])
        if summary is None and checkpoint:
            summary=checkpoint.get('summary')
        if not (summary or '').strip():
            history=c.execute("SELECT text FROM history_documents WHERE agent_id=? AND kind IN ('message','checkpoint','complete') ORDER BY timestamp DESC LIMIT 1",(child['logical_agent_id'],)).fetchone()
            summary=(history['text'] if history else '') or f"Child ended without a durable checkpoint; assigned work: {child.get('role') or ''}"
        if artifacts is None:
            artifacts=self._latest_artifacts_locked(c,child['logical_agent_id'])
        checkpoint_id=checkpoint.get('checkpoint_id') if checkpoint else None
        checkpoint_seq=int(checkpoint.get('seq') or 0) if checkpoint else 0
        completion=public_text(completion_summary if completion_summary is not None else child.get('completion_summary') or '',1600)
        pending=public_text(pending_work if pending_work is not None else child.get('pending_work') or '',1600)
        clean_summary=public_text(summary,2200)
        clean_artifacts=[]
        for item in (artifacts or [])[:24]:
            if not isinstance(item,dict):
                continue
            clean_artifacts.append({
                'path':public_text(item.get('path') or '',500),
                'kind':public_text(item.get('kind') or '',40),
                'exists':bool(item.get('exists')),
                'size':item.get('size') if type(item.get('size')) is int else None,
                'sha256':public_text(item.get('sha256') or '',80),
            })
        key='\n'.join([child['logical_agent_id'],status,str(checkpoint_id or ''),str(child.get('turn_epoch') or 0),str(child.get('progress_version') or 0)])
        handoff_id='ho_'+digest(key)[:32]
        now=self.clock()
        c.execute('''INSERT OR IGNORE INTO handoffs(
            handoff_id,child_agent_id,parent_agent_id,recipient_thread_id,status,checkpoint_id,turn_epoch,
            summary,pending_work,artifacts_json,completion_summary,evidence,created_at
        ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)''',(
            handoff_id,child['logical_agent_id'],parent_id,recipient_thread_id,status,checkpoint_id,
            int(child.get('turn_epoch') or 0),clean_summary,pending,canonical_json(clean_artifacts),completion,
            public_text(evidence or ('checkpoint:'+str(checkpoint_seq) if checkpoint else 'public-state-fallback'),200),now,
        ))
        row=c.execute('SELECT * FROM handoffs WHERE handoff_id=?',(handoff_id,)).fetchone()
        if row:
            envelope={k:row[k] for k in ('handoff_id','child_agent_id','parent_agent_id','recipient_thread_id','status','checkpoint_id','turn_epoch','summary','pending_work','completion_summary','evidence','created_at')}
            envelope.update(workspace=child.get('workspace'),agent_path=child.get('agent_path'),short_id=child.get('short_id'))
            envelope['artifacts']=clean_artifacts
            self._index(c,child,'handoff:'+handoff_id,canonical_json(envelope),'handoff',now)
            self._event(c,child['logical_agent_id'],'handoff.created',handoff_id=handoff_id,status=status,recipient_thread_id=recipient_thread_id)
            return envelope
        return None

    def handoff_inbox(self,runtime_thread_id: str,*,deliver: bool = False,ack_ids: list[str] | None = None) -> list[dict]:
        runtime_thread_id=str(runtime_thread_id or '').strip()
        if not runtime_thread_id:
            return []
        with (self.transaction() if deliver or ack_ids else self.connection()) as c:
            if ack_ids:
                for hid in ack_ids[:100]:
                    c.execute('UPDATE handoffs SET acked_at=? WHERE handoff_id=? AND recipient_thread_id=?',(self.clock(),hid,runtime_thread_id))
            extra=' AND delivered_at IS NULL' if deliver else ''
            rows=c.execute('''SELECT h.*,a.workspace,a.agent_path,a.short_id FROM handoffs h
                              JOIN agents a ON a.logical_agent_id=h.child_agent_id
                              WHERE h.recipient_thread_id=? AND h.acked_at IS NULL'''+extra+' ORDER BY h.created_at DESC LIMIT 20',(runtime_thread_id,)).fetchall()
            if deliver:
                for row in rows:
                    c.execute('UPDATE handoffs SET delivered_at=? WHERE handoff_id=? AND delivered_at IS NULL',(self.clock(),row['handoff_id']))
            result=[]
            for row in rows:
                item=dict(row)
                try:
                    item['artifacts']=json.loads(item.pop('artifacts_json'))
                except (TypeError,json.JSONDecodeError):
                    item['artifacts']=[]
                result.append(item)
            return result

    def latest_handoff(self,child_agent_id: str) -> dict | None:
        with self.connection() as c:
            row=c.execute('''SELECT h.*,a.workspace,a.agent_path,a.short_id FROM handoffs h
                             JOIN agents a ON a.logical_agent_id=h.child_agent_id
                             WHERE h.child_agent_id=? ORDER BY h.created_at DESC LIMIT 1''',(child_agent_id,)).fetchone()
        if not row:
            return None
        item=dict(row)
        try:item['artifacts']=json.loads(item.pop('artifacts_json'))
        except (TypeError,json.JSONDecodeError):item['artifacts']=[]
        return item

    def child_handoffs(self,parent_agent_id: str) -> list[dict]:
        with self.connection() as c:
            rows=c.execute('''SELECT h.*,a.workspace,a.agent_path,a.short_id FROM handoffs h JOIN agents a ON a.logical_agent_id=h.child_agent_id
                              WHERE a.parent_agent_id=? ORDER BY h.created_at DESC''',(parent_agent_id,)).fetchall()
        result=[]; seen=set()
        for row in rows:
            if row['child_agent_id'] in seen:
                continue
            seen.add(row['child_agent_id'])
            item=dict(row)
            try:item['artifacts']=json.loads(item.pop('artifacts_json'))
            except (TypeError,json.JSONDecodeError):item['artifacts']=[]
            result.append(item)
        return result

    def complete(self, agent_id: str, summary: str) -> dict:
        if not summary.strip():
            raise AgentError('SUMMARY_REQUIRED', 'Completion needs an explicit public result')
        with self.transaction() as c:
            a = self._row(c,agent_id)
            if a['state'] == 'DONE':
                return a
            if a['join_state'] != 'joined' or a['state'] in {'CLOSED','BLOCKED'}:
                raise AgentError('COMPLETE_NOT_ALLOWED', 'Only a joined active agent can explicitly complete')
            unfinished = [dict(row) for row in c.execute(
                "SELECT logical_agent_id,short_id,agent_path,state FROM agents WHERE parent_agent_id=? AND state NOT IN ('DONE','CLOSED','BLOCKED') ORDER BY created_at",
                (agent_id,)
            ).fetchall()]
            if unfinished:
                raise AgentError('CHILDREN_STILL_RUNNING', 'All direct child agents must reach a terminal state before the parent can complete',
                                 children=unfinished)
            terminal_status={'DONE':'completed','CLOSED':'closed','BLOCKED':'blocked'}
            terminal_children=[dict(row) for row in c.execute(
                "SELECT * FROM agents WHERE parent_agent_id=? AND state IN ('DONE','CLOSED','BLOCKED') ORDER BY created_at",
                (agent_id,)
            ).fetchall()]
            for child in terminal_children:
                expected_status=terminal_status[child['state']]
                exists=c.execute(
                    "SELECT 1 FROM handoffs WHERE child_agent_id=? AND status=? LIMIT 1",
                    (child['logical_agent_id'],expected_status),
                ).fetchone()
                if not exists:
                    self._write_handoff_locked(c,child,status=expected_status,evidence='terminal-backfill')
                if not c.execute(
                    "SELECT 1 FROM handoffs WHERE child_agent_id=? AND status=? LIMIT 1",
                    (child['logical_agent_id'],expected_status),
                ).fetchone():
                    raise AgentError('CHILD_HANDOFF_REQUIRED','Terminal Child has no durable handoff',child_agent_id=child['logical_agent_id'])
            if c.execute("SELECT 1 FROM recovery_operations WHERE agent_id=? AND state IN ('SUBMITTING','SUBMITTED','RECONCILING')", (agent_id,)).fetchone():
                raise AgentError('RECOVERY_RECONCILIATION_REQUIRED', 'Ambiguous recovery must be reconciled before completion')
            pending_native=[dict(row) for row in c.execute(
                "SELECT operation_id,kind,state FROM web_operations WHERE agent_id=? AND state IN ('PREPARED','SUBMITTING','RECONCILING') ORDER BY created_at",
                (agent_id,)
            ).fetchall()]
            if pending_native:
                raise AgentError('WEB_RECONCILIATION_REQUIRED',
                    'Native web submission must be reconciled or aborted before completion',
                    operations=pending_native)
            if not c.execute('SELECT 1 FROM checkpoints WHERE agent_id=?', (agent_id,)).fetchone():
                raise AgentError('CHECKPOINT_REQUIRED', 'Persist a final checkpoint before complete')
            c.execute("UPDATE agents SET state='DONE',completed_at=?,completion_summary=?,pending_work='',updated_at=? WHERE logical_agent_id=?", (self.clock(),public_text(summary,4000),self.clock(),agent_id))
            c.execute("UPDATE recovery_claims SET status='revoked' WHERE agent_id=?", (agent_id,))
            c.execute("UPDATE recovery_operations SET state='ABORTED',last_error_class='DONE',updated_at=? WHERE agent_id=? AND state IN ('CLAIMED','PRECHECKED')", (self.clock(),agent_id))
            self._terminalize_native_state(c,agent_id,'DONE')
            self._index(c,a,'complete:'+agent_id,summary,'complete',self.clock())
            self._event(c,agent_id,'agent.completed')
            completed=self._row(c,agent_id)
            self._write_handoff_locked(
                c,completed,status='completed',checkpoint=self._latest_checkpoint_locked(c,agent_id),
                completion_summary=summary,evidence='complete',
            )
            return completed

    def archive_message(self, agent_id: str, message: dict) -> bool:
        if message.get('role') not in {'user','assistant'} or message.get('channel') not in {None,'final','commentary'}:
            return False
        if message.get('content_type','text') != 'text' or not isinstance(message.get('visible_text'),str) or not message.get('message_id'):
            return False
        if message.get('hidden'):
            return False
        text = public_text(message['visible_text'])
        with self.transaction() as c:
            a = self._row(c,agent_id)
            if not a['conversation_id'] or message.get('conversation_id',a['conversation_id']) != a['conversation_id']:
                raise AgentError('ARCHIVE_CONVERSATION_MISMATCH', 'Message must belong to the bound conversation')
            epoch = int(message.get('turn_epoch',a['turn_epoch']))
            source = message.get('source','web_graph')
            if source not in {'web_graph','runtime_progress','checkpoint','handoff','web_dom'}:
                raise AgentError('ARCHIVE_SOURCE_INVALID', 'Only visible/public sources can be archived')
            old = c.execute('SELECT content_sha256 FROM conversation_messages WHERE conversation_id=? AND message_id=?', (a['conversation_id'],message['message_id'])).fetchone()
            sha = digest(text)
            if old and old[0] == sha:
                return False
            timestamp = message.get('create_time') or self.clock()
            c.execute('''INSERT INTO conversation_messages VALUES(?,?,?,?,?,?,?,?,?,?,?,?)
                ON CONFLICT(conversation_id,message_id) DO UPDATE SET visible_text=excluded.visible_text,
                content_sha256=excluded.content_sha256,synced_at=excluded.synced_at,turn_epoch=excluded.turn_epoch''',
                (a['conversation_id'],message['message_id'],message.get('parent_message_id'),agent_id,epoch,message['role'],'text',text,timestamp,sha,source,self.clock()))
            self._index(c,a,'message:'+a['conversation_id']+':'+message['message_id'],text,'message',timestamp,message_id=message['message_id'],turn_epoch=epoch)
            return True

    def _scope_ids(self, c, agent_id: str, scope: str) -> list[str]:
        a = self._row(c,agent_id)
        if scope == 'self':
            return [agent_id]
        if scope == 'supervisor':
            row = c.execute('SELECT value_json FROM settings WHERE key=?', (self._agent_scope_key(agent_id),)).fetchone()
            raw_scope = json.loads(row[0]) if row else a['owner_thread_id']
            supervisor_scope = self._valid_scope_id(raw_scope)
            encoded = canonical_json(supervisor_scope)
            return [r[0] for r in c.execute(
                '''SELECT a.logical_agent_id FROM agents a JOIN settings s
                   ON s.key=('agent_supervisor_scope:' || a.logical_agent_id)
                   WHERE a.kind='business' AND s.value_json=?
                   ORDER BY a.created_at,a.logical_agent_id''',
                (encoded,),
            )]
        if scope == 'project':
            return [r[0] for r in c.execute('SELECT logical_agent_id FROM agents WHERE project_id=?', (a['project_id'],))]
        if scope == 'root':
            return [r[0] for r in c.execute(
                'SELECT logical_agent_id FROM agents WHERE root_agent_id=? ORDER BY created_at,logical_agent_id',
                (a['root_agent_id'],),
            )]
        if scope == 'lineage':
            rows = c.execute('SELECT logical_agent_id,agent_path FROM agents WHERE root_agent_id=?', (a['root_agent_id'],))
            p = a['agent_path']
            return [r[0] for r in rows if r[1] == p or r[1].startswith(p+'/') or p.startswith(r[1]+'/')]
        raise AgentError('HISTORY_SCOPE_FORBIDDEN', 'Supported scopes: self, lineage, root, supervisor, project; all_local is disabled')

    def history_search(self, agent_id: str, query: str, *, scope='project', limit=8) -> list[dict]:
        query = query.strip()[:300]
        if not query:
            return []
        with self.connection() as c:
            ids = self._scope_ids(c,agent_id,scope)
            placeholders = ','.join('?' for _ in ids)
            terms = re.findall(r'[\w\u4e00-\u9fff]+',query,re.UNICODE)
            match = ' AND '.join('"'+t.replace('"','""')+'"' for t in terms)
            rows = []
            if match:
                rows = c.execute(f'''SELECT d.*,a.agent_path,a.short_id FROM history_documents d
                    JOIN history_fts f ON f.source_id=d.source_id JOIN agents a ON a.logical_agent_id=d.agent_id
                    WHERE history_fts MATCH ? AND d.agent_id IN ({placeholders}) ORDER BY bm25(history_fts),d.timestamp DESC LIMIT ?''',
                    (match,*ids,min(max(int(limit),1),20))).fetchall()
            if not rows:
                rows = c.execute(f'''SELECT d.*,a.agent_path,a.short_id FROM history_documents d JOIN agents a ON a.logical_agent_id=d.agent_id
                    WHERE d.agent_id IN ({placeholders}) AND instr(lower(d.text),lower(?))>0 ORDER BY d.timestamp DESC LIMIT ?''',
                    (*ids,query,min(max(int(limit),1),20))).fetchall()
            result = []
            for row in rows:
                item = dict(row)
                text = item.pop('text')
                start = max(0,text.lower().find(query.lower())-80)
                item['excerpt'] = text[start:start+500]
                item['source_handle'] = item.pop('source_id')
                result.append(item)
            return result

    def history_get(self, agent_id: str, source_handle: str, *, scope='project', start=0, length=2000) -> dict:
        with self.connection() as c:
            ids = self._scope_ids(c,agent_id,scope)
            row = c.execute('SELECT * FROM history_documents WHERE source_id=?', (source_handle,)).fetchone()
            if not row or row['agent_id'] not in ids:
                raise AgentError('HISTORY_SOURCE_NOT_FOUND', 'Source is absent or outside the authorized scope')
            item = dict(row)
            text = item.pop('text')
            start, length = max(int(start),0), min(max(int(length),1),4000)
            item.update(text=text[start:start+length],start=start,total_chars=len(text),truncated=start+length<len(text))
            return item

    def enqueue(self, from_agent_id: str, to_agent_id: str, content: str, *, kind='message') -> dict:
        if kind not in {'message','handoff','command'} or not content.strip():
            raise AgentError('INVALID_MESSAGE', 'A supported kind and nonempty public content are required')
        with self.transaction() as c:
            a = self._row(c,to_agent_id)
            if a['state'] in TERMINAL_AGENT_STATES:
                raise AgentError('TERMINAL_AGENT', 'Messages cannot revive terminal logical agents')
            mid, text = 'mail_'+uuid.uuid4().hex, public_text(content,4000)
            c.execute('INSERT INTO mailbox(message_id,from_agent_id,to_agent_id,kind,content,created_at) VALUES(?,?,?,?,?,?)', (mid,from_agent_id,to_agent_id,kind,text,self.clock()))
            self._index(c,a,'mailbox:'+mid,text,'mailbox',self.clock(),message_id=mid)
            self._event(c,to_agent_id,'mailbox.pending')
            return {'message_id':mid,'to_agent_id':to_agent_id,'route':'mailbox'}

    def inbox(self, agent_id: str, *, deliver=False, ack_ids: list[str] | None = None) -> list[dict]:
        with (self.transaction() if deliver or ack_ids else self.connection()) as c:
            if ack_ids:
                for mid in ack_ids[:100]:
                    c.execute('UPDATE mailbox SET acked_at=? WHERE message_id=? AND to_agent_id=?', (self.clock(),mid,agent_id))
            extra = ' AND delivered_at IS NULL' if deliver else ''
            rows = c.execute('SELECT * FROM mailbox WHERE to_agent_id=? AND acked_at IS NULL'+extra+' ORDER BY created_at LIMIT 20', (agent_id,)).fetchall()
            if deliver:
                for row in rows:
                    c.execute('UPDATE mailbox SET delivered_at=? WHERE message_id=? AND delivered_at IS NULL', (self.clock(),row['message_id']))
            return [dict(r) for r in rows]

    def latest_event_seq(self) -> int:
        with self.connection() as c:
            return c.execute('SELECT coalesce(max(seq),0) FROM agent_events').fetchone()[0]

    def events_after(self, seq: int, *, limit=50) -> list[dict]:
        with self.connection() as c:
            return [dict(r) for r in c.execute('SELECT * FROM agent_events WHERE seq>? ORDER BY seq LIMIT ?', (seq,min(max(int(limit),1),100)))]

    def claim_recovery(self, agent_id: str, epoch: int, *, claimed_by: str,
                       expected_tail_message_id: str | None, text='继续', ttl_seconds=120) -> dict:
        if text != '继续':
            raise AgentError('RECOVERY_TEXT_FIXED', 'Recovery may submit exactly one plain 继续, not a new instruction')
        now, blocked = self.clock(), False
        with self.transaction() as c:
            a = self._row(c,agent_id)
            if a['state'] in TERMINAL_AGENT_STATES | {'EXTERNAL_ACTIVITY'} or a['join_state'] != 'joined':
                raise AgentError('RECOVERY_NOT_ALLOWED', 'Agent is terminal, externally active, or not joined')
            if a['kind'] == 'watchdog':
                raise AgentError('WATCHDOG_REPLACEMENT_ONLY', 'Watchdogs are replaced by the local supervisor')
            if a['turn_epoch'] != epoch or epoch < 1 or not a['pending_work']:
                raise AgentError('STALE_RECOVERY', 'Turn changed or there is no explicit pending work')
            if a['recovery_not_before'] > now:
                raise AgentError('RECOVERY_BACKOFF', 'Persistent backoff is active',retry_at=a['recovery_not_before'])
            op = c.execute("SELECT * FROM recovery_operations WHERE agent_id=? AND state IN ('SUBMITTING','SUBMITTED','RECONCILING') ORDER BY created_at DESC LIMIT 1", (agent_id,)).fetchone()
            if op:
                return {'status':'reconcile_required','operation':dict(op)}
            claim = c.execute('SELECT * FROM recovery_claims WHERE agent_id=? AND turn_epoch=?', (agent_id,epoch)).fetchone()
            if claim and claim['status'] == 'active' and claim['expires_at'] > now:
                return {'status':'already_claimed'}
            same_progress = a['last_recovery_progress'] == a['progress_version']
            no_progress = a['no_progress_turns'] if same_progress else 0
            consecutive = a['consecutive_recoveries'] if same_progress else 0
            if no_progress >= self.config.no_progress_turn_limit or consecutive >= self.config.max_consecutive_recoveries:
                c.execute("UPDATE agents SET state='BLOCKED',updated_at=? WHERE logical_agent_id=?", (now,agent_id))
                self._event(c,agent_id,'agent.blocked',reason='no_progress')
                blocked = True
            else:
                token, opid = uuid.uuid4().hex, 'rec_'+uuid.uuid4().hex
                c.execute("UPDATE recovery_operations SET state='ABORTED',last_error_class='expired_pre_submit_claim',updated_at=? WHERE agent_id=? AND source_turn_epoch=? AND state IN ('CLAIMED','PRECHECKED')", (now,agent_id,epoch))
                c.execute('''INSERT INTO recovery_claims VALUES(?,?,?,?,?,?,?) ON CONFLICT(agent_id,turn_epoch) DO UPDATE SET
                    claim_token=excluded.claim_token,claimed_by=excluded.claimed_by,claimed_at=excluded.claimed_at,
                    expires_at=excluded.expires_at,status=excluded.status''',
                    (agent_id,epoch,token,claimed_by,now,now+min(max(ttl_seconds,5),600),'active'))
                c.execute('''INSERT INTO recovery_operations(operation_id,agent_id,source_turn_epoch,claim_token,state,
                    expected_tail_message_id,submit_intent_sha256,created_at,updated_at,requested_text) VALUES(?,?,?,?,?,?,?,?,?,?)''',
                    (opid,agent_id,epoch,token,'CLAIMED',expected_tail_message_id,digest(text),now,now,text))
                c.execute("UPDATE agents SET state='RECOVERING',last_recovery_progress=?,no_progress_turns=?,consecutive_recoveries=?,updated_at=? WHERE logical_agent_id=?", (a['progress_version'],no_progress,consecutive,now,agent_id))
                self._event(c,agent_id,'recovery.claimed',operation_id=opid)
                result = {'status':'claimed','operation':dict(c.execute('SELECT * FROM recovery_operations WHERE operation_id=?', (opid,)).fetchone())}
        return {'status':'blocked','reason':'no_progress'} if blocked else result

    def operation(self, operation_id: str) -> dict:
        with self.connection() as c:
            row = c.execute('SELECT * FROM recovery_operations WHERE operation_id=?', (operation_id,)).fetchone()
        if not row:
            raise AgentError('OPERATION_NOT_FOUND', 'No recovery operation')
        return dict(row)

    def open_operations(self) -> list[dict]:
        with self.connection() as c:
            return [dict(r) for r in c.execute("SELECT * FROM recovery_operations WHERE state IN ('CLAIMED','PRECHECKED','SUBMITTING','SUBMITTED','RECONCILING') ORDER BY created_at")]

    def advance_operation(self, operation_id: str, expected: str | tuple[str,...], state: str, **fields) -> dict:
        transitions = {
            'CLAIMED': {'PRECHECKED','ABORTED','BLOCKED'},
            'PRECHECKED': {'SUBMITTING','ABORTED','BLOCKED'},
            'SUBMITTING': {'SUBMITTED','RECONCILING','BLOCKED'},
            'SUBMITTED': {'RECONCILING','RESUMED','BLOCKED'},
            'RECONCILING': {'RECONCILING','RESUMED','BLOCKED'},
        }
        allowed_fields = {'observed_user_message_id','observed_generation_request_id','observed_new_turn_exchange_id','last_error_class'}
        if set(fields) - allowed_fields:
            raise AgentError('INVALID_OPERATION_UPDATE', 'Unsupported recovery field')
        expected = (expected,) if isinstance(expected,str) else expected
        with self.transaction() as c:
            op = c.execute('SELECT * FROM recovery_operations WHERE operation_id=?', (operation_id,)).fetchone()
            if not op or op['state'] not in expected:
                raise AgentError('RECOVERY_CAS_CONFLICT', 'Another coordinator advanced this operation')
            if state not in transitions.get(op['state'],set()):
                raise AgentError('INVALID_OPERATION_TRANSITION', 'Recovery transition is not permitted')
            a = self._row(c,op['agent_id'])
            if state in {'PRECHECKED','SUBMITTING'}:
                claim = c.execute('SELECT * FROM recovery_claims WHERE agent_id=? AND turn_epoch=?', (op['agent_id'],op['source_turn_epoch'])).fetchone()
                if not claim or claim['status'] != 'active' or claim['claim_token'] != op['claim_token'] or claim['expires_at'] <= self.clock():
                    raise AgentError('CLAIM_EXPIRED', 'A live matching claim is required before submission')
                if a['state'] in TERMINAL_AGENT_STATES | {'EXTERNAL_ACTIVITY'} or a['turn_epoch'] != op['source_turn_epoch']:
                    raise AgentError('STALE_RECOVERY', 'Agent or turn changed before submit')
            if state == 'RESUMED':
                new_tx = fields.get('observed_new_turn_exchange_id') or op['observed_new_turn_exchange_id']
                user_id = fields.get('observed_user_message_id') or op['observed_user_message_id']
                turn = c.execute('SELECT * FROM turns WHERE agent_id=? AND turn_exchange_id=?', (op['agent_id'],new_tx)).fetchone()
                if not new_tx or not user_id or not turn or turn['turn_epoch'] <= op['source_turn_epoch'] or turn['user_message_id'] != user_id:
                    raise AgentError('RESUME_PROOF_REQUIRED', 'Resumed requires the observed user message and a new bound turn')
            assignments = ['state=?','updated_at=?']+[k+'=?' for k in fields]
            c.execute('UPDATE recovery_operations SET '+','.join(assignments)+' WHERE operation_id=?', (state,self.clock(),*fields.values(),operation_id))
            if state in {'RESUMED','ABORTED','BLOCKED'}:
                c.execute("UPDATE recovery_claims SET status='released' WHERE agent_id=? AND turn_epoch=? AND claim_token=?", (op['agent_id'],op['source_turn_epoch'],op['claim_token']))
            if state == 'RESUMED':
                c.execute("UPDATE agents SET no_progress_turns=no_progress_turns+1,consecutive_recoveries=consecutive_recoveries+1,state=CASE WHEN state IN ('DONE','CLOSED','BLOCKED','EXTERNAL_ACTIVITY') THEN state ELSE 'RUNNING' END,updated_at=? WHERE logical_agent_id=?", (self.clock(),op['agent_id']))
            elif state == 'BLOCKED':
                c.execute("UPDATE agents SET state=CASE WHEN state IN ('DONE','CLOSED') THEN state ELSE 'BLOCKED' END,updated_at=? WHERE logical_agent_id=?", (self.clock(),op['agent_id']))
            elif state == 'ABORTED':
                c.execute("UPDATE agents SET state='IDLE',updated_at=? WHERE logical_agent_id=? AND state='RECOVERING'", (self.clock(),op['agent_id']))
            self._event(c,op['agent_id'],'recovery.'+state.lower(),operation_id=operation_id)
            return dict(c.execute('SELECT * FROM recovery_operations WHERE operation_id=?', (operation_id,)).fetchone())

    def backoff(self, agent_id: str, seconds: float) -> None:
        with self.transaction() as c:
            c.execute('UPDATE agents SET recovery_not_before=max(recovery_not_before,?),updated_at=? WHERE logical_agent_id=?', (self.clock()+min(max(seconds,1),3600),self.clock(),agent_id))

    def service_claim(self, name: str, owner: str, ttl_seconds: float) -> bool:
        with self.transaction() as c:
            now = self.clock()
            row = c.execute('SELECT * FROM service_locks WHERE name=?', (name,)).fetchone()
            if row and row['owner'] != owner and row['expires_at'] > now:
                return False
            c.execute('INSERT INTO service_locks VALUES(?,?,?) ON CONFLICT(name) DO UPDATE SET owner=excluded.owner,expires_at=excluded.expires_at', (name,owner,now+ttl_seconds))
            return True

    def service_release(self, name: str, owner: str) -> None:
        with self.transaction() as c:
            c.execute('DELETE FROM service_locks WHERE name=? AND owner=?', (name,owner))

    def web_operation_by_key(self, key: str) -> dict | None:
        with self.connection() as c:
            row=c.execute('SELECT * FROM web_operations WHERE idempotency_key=?',(key,)).fetchone()
        return dict(row) if row else None

    def prepare_web_operation(self, agent_id: str, key: str, kind: str, *, target_id=None,
                              expected_conversation_id=None, expected_tail_message_id=None) -> dict:
        if kind not in {'spawn','followup','wake'}:
            raise AgentError('WEB_OPERATION_KIND_INVALID','Unsupported native submission intent')
        with self.transaction() as c:
            a=self._row(c,agent_id)
            existing=c.execute('SELECT * FROM web_operations WHERE idempotency_key=?',(key,)).fetchone()
            if existing:
                if existing['agent_id']!=agent_id or existing['kind']!=kind:
                    raise AgentError('IDEMPOTENCY_SCOPE_MISMATCH','Intent belongs to another agent or action')
                return dict(existing)
            if a['state'] in TERMINAL_AGENT_STATES|{'EXTERNAL_ACTIVITY'}:
                raise AgentError('WEB_SUBMIT_NOT_ALLOWED','Terminal or externally active agents cannot be submitted')
            if c.execute("SELECT 1 FROM web_operations WHERE agent_id=? AND state IN ('PREPARED','SUBMITTING','RECONCILING')",(agent_id,)).fetchone():
                raise AgentError('WEB_SUBMIT_PENDING','Reconcile the existing native submission first')
            oid='web_'+uuid.uuid4().hex
            now=self.clock()
            c.execute('''INSERT INTO web_operations(operation_id,idempotency_key,agent_id,kind,state,target_id,
                expected_conversation_id,expected_tail_message_id,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?,?)''',
                (oid,key,agent_id,kind,'PREPARED',target_id,expected_conversation_id,expected_tail_message_id,now,now))
            return dict(c.execute('SELECT * FROM web_operations WHERE operation_id=?',(oid,)).fetchone())

    def web_operation(self, operation_id: str) -> dict:
        with self.connection() as c:
            row=c.execute('SELECT * FROM web_operations WHERE operation_id=?',(operation_id,)).fetchone()
        if not row:
            raise AgentError('WEB_OPERATION_NOT_FOUND','Native submission intent is absent')
        return dict(row)

    def update_web_operation(self, operation_id: str, expected: str|tuple[str,...], state: str, **fields) -> dict:
        expected=(expected,) if isinstance(expected,str) else expected
        transitions={'PREPARED':{'PREPARED','SUBMITTING','ABORTED'},'SUBMITTING':{'RECONCILING','OBSERVED','BLOCKED'},
                     'RECONCILING':{'RECONCILING','OBSERVED','BLOCKED'}}
        allowed={'target_id','prompt_sha256','observed_user_message_id','observed_request_id','observed_turn_exchange_id','error_class'}
        if set(fields)-allowed:
            raise AgentError('WEB_OPERATION_FIELD_INVALID','Unsupported submission metadata')
        with self.transaction() as c:
            row=c.execute('SELECT * FROM web_operations WHERE operation_id=?',(operation_id,)).fetchone()
            if not row or row['state'] not in expected:
                raise AgentError('WEB_OPERATION_CAS_CONFLICT','Submission was already advanced by another caller')
            if state not in transitions.get(row['state'],set()):
                raise AgentError('WEB_OPERATION_TRANSITION_INVALID','Ambiguous native submits may only be reconciled, never retried')
            a=self._row(c,row['agent_id'])
            if state=='SUBMITTING':
                if self.config.ultra_collaboration:
                    active = c.execute("""SELECT count(*) FROM agents x WHERE x.kind=?
                        AND x.logical_agent_id!=? AND x.state NOT IN ('DONE','CLOSED','BLOCKED')
                        AND (x.join_state='joined' OR EXISTS (SELECT 1 FROM web_operations w
                            WHERE w.agent_id=x.logical_agent_id AND w.state IN ('SUBMITTING','RECONCILING','OBSERVED')))""",
                        (a['kind'],a['logical_agent_id'])).fetchone()[0]
                    if active >= self.config.max_live_agents:
                        raise AgentError('AGENT_LIMIT', 'Live submission capacity is full; draft was not sent')
                if a['state'] in TERMINAL_AGENT_STATES|{'EXTERNAL_ACTIVITY'}:
                    raise AgentError('WEB_SUBMIT_NOT_ALLOWED','Agent changed before native submission')
                if not (fields.get('prompt_sha256') or row['prompt_sha256']):
                    raise AgentError('SUBMIT_HASH_REQUIRED','Commit the exact prompt hash before a native submission')
            if state=='OBSERVED':
                tx=fields.get('observed_turn_exchange_id') or row['observed_turn_exchange_id']
                uid=fields.get('observed_user_message_id') or row['observed_user_message_id']
                turn=c.execute('SELECT * FROM turns WHERE agent_id=? AND turn_exchange_id=?',(row['agent_id'],tx)).fetchone()
                if not tx or not uid or not turn or turn['user_message_id']!=uid or not a['conversation_id']:
                    raise AgentError('WEB_SUBMIT_PROOF_REQUIRED','Observed needs the exact bound conversation/user message/turn')
            c.execute('UPDATE web_operations SET state=?,updated_at=?'+''.join(','+key+'=?' for key in fields)+' WHERE operation_id=?',
                      (state,self.clock(),*fields.values(),operation_id))
            self._event(c,row['agent_id'],'web_submit.'+state.lower(),operation_id=operation_id)
            return dict(c.execute('SELECT * FROM web_operations WHERE operation_id=?',(operation_id,)).fetchone())

    def pending_web_operations(self, agent_id: str|None=None) -> list[dict]:
        with self.connection() as c:
            sql="SELECT * FROM web_operations WHERE state IN ('PREPARED','SUBMITTING','RECONCILING')"
            if agent_id:
                return [dict(r) for r in c.execute(sql+' AND agent_id=? ORDER BY created_at',(agent_id,))]
            return [dict(r) for r in c.execute(sql+' ORDER BY created_at')]

    def _terminalize_native_state(self, c: sqlite3.Connection, agent_id: str, reason: str) -> dict:
        """Close native submission credentials/intents when an Agent becomes terminal."""
        at=self.clock()
        revoked=c.execute(
            "UPDATE spawn_tickets SET status='revoked' WHERE logical_agent_id=? AND status='pending'",
            (agent_id,),
        ).rowcount
        operations=c.execute(
            "SELECT operation_id,state FROM web_operations WHERE agent_id=? AND state IN ('PREPARED','SUBMITTING','RECONCILING') ORDER BY created_at",
            (agent_id,),
        ).fetchall()
        closed=0
        for row in operations:
            # PREPARED never crossed the native submit boundary.  A submission
            # that reached SUBMITTING/RECONCILING remains ambiguous, so record
            # it as BLOCKED instead of pretending that it was never sent.
            terminal='ABORTED' if row['state']=='PREPARED' else 'BLOCKED'
            c.execute(
                'UPDATE web_operations SET state=?,updated_at=?,error_class=coalesce(error_class,?) WHERE operation_id=?',
                (terminal,at,public_text(reason,120),row['operation_id']),
            )
            self._event(c,agent_id,'web_submit.'+terminal.lower(),operation_id=row['operation_id'])
            closed += 1
        return {'revoked_tickets':int(revoked or 0),'closed_web_operations':closed}

    def _repair_terminal_submission_state(self) -> dict:
        """Repair legacy terminal Agents that retained live tickets/native intents."""
        repaired_agents=0
        revoked_tickets=0
        closed_operations=0
        with self.transaction() as c:
            rows=c.execute(
                "SELECT logical_agent_id,state FROM agents WHERE state IN ('DONE','CLOSED','BLOCKED') ORDER BY created_at"
            ).fetchall()
            for row in rows:
                pending_ticket=c.execute(
                    "SELECT 1 FROM spawn_tickets WHERE logical_agent_id=? AND status='pending' LIMIT 1",
                    (row['logical_agent_id'],),
                ).fetchone()
                pending_operation=c.execute(
                    "SELECT 1 FROM web_operations WHERE agent_id=? AND state IN ('PREPARED','SUBMITTING','RECONCILING') LIMIT 1",
                    (row['logical_agent_id'],),
                ).fetchone()
                if not pending_ticket and not pending_operation:
                    continue
                result=self._terminalize_native_state(c,row['logical_agent_id'],'legacy-'+str(row['state']))
                repaired_agents += 1
                revoked_tickets += result['revoked_tickets']
                closed_operations += result['closed_web_operations']
        return {'agents':repaired_agents,'revoked_tickets':revoked_tickets,'closed_web_operations':closed_operations}

    def _backfill_legacy_handoffs(self) -> dict:
        """Recover durable handoffs for pre-protocol business Agents.

        Only persisted terminal/turn state plus public checkpoint/history is
        used. This migration never upgrades unfinished work to DONE.
        """
        repaired=0
        with self.transaction() as c:
            agents=[dict(row) for row in c.execute(
                "SELECT * FROM agents WHERE kind='business' ORDER BY created_at,logical_agent_id"
            ).fetchall()]
            for agent in agents:
                if c.execute(
                    'SELECT 1 FROM handoffs WHERE child_agent_id=? LIMIT 1',
                    (agent['logical_agent_id'],),
                ).fetchone():
                    continue
                status=None
                evidence=None
                state=agent.get('state')
                if state=='DONE':
                    status,evidence='completed','legacy-migration:DONE'
                elif state=='CLOSED':
                    status,evidence='closed','legacy-migration:CLOSED'
                elif state=='BLOCKED':
                    status,evidence='blocked','legacy-migration:BLOCKED'
                elif state=='DEAD_RECOVERABLE':
                    status,evidence='interrupted','legacy-migration:DEAD_RECOVERABLE'
                elif state=='IDLE':
                    turn=c.execute(
                        'SELECT termination_class FROM turns WHERE agent_id=? ORDER BY turn_epoch DESC LIMIT 1',
                        (agent['logical_agent_id'],),
                    ).fetchone()
                    termination=turn['termination_class'] if turn else None
                    if termination=='trusted_final':
                        status,evidence='turn_final','legacy-migration:trusted_final'
                    elif termination in {'failed','cancelled'}:
                        status,evidence='interrupted','legacy-migration:'+str(termination)
                if status and self._write_handoff_locked(c,agent,status=status,evidence=evidence):
                    repaired += 1
        return {'handoffs':repaired}


    def allocate_watchdog(self, owner_thread_id: str, *, born_at: float | None = None,
                          retire_at: float | None = None) -> dict:
        now=self.clock() if born_at is None else float(born_at)
        retire=now+self.config.max_agent_age_minutes*60 if retire_at is None else float(retire_at)
        if not owner_thread_id or retire<=now:
            raise AgentError('WATCHDOG_SCHEDULE_INVALID','Watchdog owner and a future retire time are required')
        with self.transaction() as c:
            if c.execute("SELECT 1 FROM watchdogs WHERE owner_thread_id=? AND state='ALLOCATED' LIMIT 1", (owner_thread_id,)).fetchone():
                raise AgentError('WATCHDOG_ALLOCATION_IN_PROGRESS','This scope already has a Watchdog generation joining')
            cursor=c.execute("INSERT INTO watchdogs(logical_agent_id,born_at,retire_at,replacement,state,owner_thread_id) VALUES(NULL,?,?,NULL,'ALLOCATED',?)",
                (now,retire,owner_thread_id))
            generation=int(cursor.lastrowid)
            self._event(c,None,'watchdog.allocated',generation=generation)
            return dict(c.execute('SELECT * FROM watchdogs WHERE generation=?',(generation,)).fetchone())

    def bind_watchdog(self, generation: int, agent_id: str) -> dict:
        with self.transaction() as c:
            row=c.execute('SELECT * FROM watchdogs WHERE generation=?',(int(generation),)).fetchone()
            if not row:
                raise AgentError('WATCHDOG_GENERATION_NOT_FOUND','No allocated watchdog generation')
            agent=self._row(c,agent_id)
            if agent['kind']!='watchdog' or agent['short_id']!=f'WD-{int(generation):03d}':
                raise AgentError('WATCHDOG_IDENTITY_MISMATCH','Watchdog generation and logical agent identity disagree')
            if row['logical_agent_id'] and row['logical_agent_id']!=agent_id:
                raise AgentError('WATCHDOG_ALREADY_BOUND','Generation is already bound to another logical agent')
            # Binding reserves the persistent generation identity. It becomes
            # ACTIVE only after the real Watchdog Conversation joins with its
            # own Runtime lease; this makes generation handoff fail-safe.
            c.execute("UPDATE watchdogs SET logical_agent_id=? WHERE generation=?",(agent_id,int(generation)))
            self._event(c,agent_id,'watchdog.bound',generation=int(generation))
            return dict(c.execute('SELECT * FROM watchdogs WHERE generation=?',(int(generation),)).fetchone())

    def activate_watchdog(self, agent_id: str) -> dict:
        with self.transaction() as c:
            agent=self._row(c,agent_id)
            if agent['kind']!='watchdog' or agent['join_state']!='joined':
                raise AgentError('WATCHDOG_JOIN_REQUIRED','Watchdog must be joined before activation')
            row=c.execute('SELECT * FROM watchdogs WHERE logical_agent_id=?',(agent_id,)).fetchone()
            if not row:
                raise AgentError('WATCHDOG_GENERATION_NOT_FOUND','No generation is bound to this Watchdog')
            if row['state'] not in {'ALLOCATED','ACTIVE'}:
                raise AgentError('WATCHDOG_ACTIVATION_NOT_ALLOWED','Only an allocated Watchdog can become active')
            c.execute("UPDATE watchdogs SET state='ACTIVE' WHERE generation=?",(row['generation'],))
            self._event(c,agent_id,'watchdog.active',generation=int(row['generation']))
            return dict(c.execute('SELECT * FROM watchdogs WHERE generation=?',(row['generation'],)).fetchone())

    def watchdog_rows(self, *, states: tuple[str,...] | None = None,
                      owner_thread_id: str | None = None) -> list[dict]:
        owner = self._valid_scope_id(owner_thread_id) if owner_thread_id is not None else None
        with self.connection() as c:
            where=[]
            params=[]
            if states:
                placeholders=','.join('?' for _ in states)
                where.append(f'state IN ({placeholders})')
                params.extend(states)
            if owner is not None:
                where.append('owner_thread_id=?')
                params.append(owner)
            sql="SELECT w.*,coalesce(CAST(s.value_json AS REAL),w.born_at) AS state_changed_at FROM watchdogs w LEFT JOIN settings s ON s.key=('watchdog_state_at:' || w.generation)"
            if where:
                sql += ' WHERE ' + ' AND '.join(where)
            rows=c.execute(sql+' ORDER BY generation',tuple(params)).fetchall()
            return [dict(r) for r in rows]

    def set_watchdog_state(self, generation: int, state: str, *, replacement: int | None = None) -> dict:
        if state not in {'ALLOCATED','ACTIVE','RETIRING','RETIRED','FAILED','CLOSED'}:
            raise AgentError('WATCHDOG_STATE_INVALID','Unsupported watchdog lifecycle state')
        with self.transaction() as c:
            row=c.execute('SELECT * FROM watchdogs WHERE generation=?',(int(generation),)).fetchone()
            if not row:
                raise AgentError('WATCHDOG_GENERATION_NOT_FOUND','No watchdog generation')
            c.execute('UPDATE watchdogs SET state=?,replacement=coalesce(?,replacement) WHERE generation=?',(state,replacement,int(generation)))
            if row['state'] != state:
                c.execute('INSERT INTO settings VALUES(?,?) ON CONFLICT(key) DO UPDATE SET value_json=excluded.value_json',
                    ('watchdog_state_at:'+str(int(generation)),canonical_json(self.clock())))
            if state in {'RETIRED','FAILED','CLOSED'} and row['logical_agent_id']:
                # A terminal Watchdog generation must never leave its logical
                # Agent RUNNING. Otherwise the browser monitor keeps polling a
                # dead/superseded tab forever even after the supervisor scope
                # has ended.
                agent=c.execute('SELECT state FROM agents WHERE logical_agent_id=?',(row['logical_agent_id'],)).fetchone()
                if agent and agent['state'] not in TERMINAL_AGENT_STATES:
                    at=self.clock()
                    c.execute("UPDATE agents SET state='CLOSED',closed_at=coalesce(closed_at,?),updated_at=? WHERE logical_agent_id=?",
                        (at,at,row['logical_agent_id']))
                    c.execute("UPDATE recovery_claims SET status='revoked' WHERE agent_id=? AND status='active'",(row['logical_agent_id'],))
                    c.execute("UPDATE recovery_operations SET state='ABORTED',last_error_class='CLOSED',updated_at=? WHERE agent_id=? AND state IN ('CLAIMED','PRECHECKED')",
                        (at,row['logical_agent_id']))
                    self._event(c,row['logical_agent_id'],'agent.state',state='CLOSED')
                self._terminalize_native_state(c,row['logical_agent_id'],'watchdog-'+state)
            self._event(c,row['logical_agent_id'],'watchdog.'+state.lower(),generation=int(generation),replacement=replacement)
            return dict(c.execute('SELECT * FROM watchdogs WHERE generation=?',(int(generation),)).fetchone())

    def _repair_terminal_watchdog_agents(self) -> list[str]:
        """Repair legacy lifecycle rows that left a terminal Watchdog RUNNING."""
        repaired=[]
        with self.transaction() as c:
            rows=c.execute('''SELECT w.logical_agent_id FROM watchdogs w
                              JOIN agents a ON a.logical_agent_id=w.logical_agent_id
                              WHERE w.state IN ('RETIRED','FAILED','CLOSED')
                                AND a.state NOT IN ('DONE','CLOSED','BLOCKED')''').fetchall()
            at=self.clock()
            for row in rows:
                aid=row['logical_agent_id']
                c.execute("UPDATE agents SET state='CLOSED',closed_at=coalesce(closed_at,?),updated_at=? WHERE logical_agent_id=?",
                    (at,at,aid))
                c.execute("UPDATE recovery_claims SET status='revoked' WHERE agent_id=? AND status='active'",(aid,))
                c.execute("UPDATE recovery_operations SET state='ABORTED',last_error_class='CLOSED',updated_at=? WHERE agent_id=? AND state IN ('CLAIMED','PRECHECKED')",
                    (at,aid))
                self._terminalize_native_state(c,aid,'legacy-watchdog-terminal')
                self._event(c,aid,'agent.state',state='CLOSED')
                repaired.append(aid)
        return repaired

    def watchdog_reconcile(self, *, now: float | None = None,
                           owner_thread_id: str | None = None) -> dict:
        """Deterministic local schedule facts only; it never submits web messages."""
        at=self.clock() if now is None else float(now)
        owner = self._valid_scope_id(owner_thread_id) if owner_thread_id is not None else None
        with self.transaction() as c:
            if owner is None:
                rows=[dict(r) for r in c.execute('SELECT * FROM watchdogs ORDER BY generation')]
            else:
                rows=[dict(r) for r in c.execute('SELECT * FROM watchdogs WHERE owner_thread_id=? ORDER BY generation',(owner,))]
            retired=[]
            for row in rows:
                # A healthy ACTIVE Watchdog is never hard-killed merely for
                # reaching its nominal rotation age. Rotation is fail-safe:
                # the controller first brings a replacement to ACTIVE, then
                # marks the old generation RETIRING. Only RETIRING generations
                # are allowed to age out here. This prevents a supervision gap
                # when replacement startup is delayed or fails.
                if row['state']=='RETIRING' and row['retire_at']<=at:
                    c.execute("UPDATE watchdogs SET state='RETIRED' WHERE generation=?",(row['generation'],))
                    if row['logical_agent_id']:
                        c.execute("UPDATE agents SET state=CASE WHEN state IN ('DONE','CLOSED','BLOCKED') THEN state ELSE 'CLOSED' END,closed_at=coalesce(closed_at,?),updated_at=? WHERE logical_agent_id=?",
                            (at,at,row['logical_agent_id']))
                        self._terminalize_native_state(c,row['logical_agent_id'],'watchdog-RETIRED')
                    self._event(c,row['logical_agent_id'],'watchdog.retired',generation=row['generation'])
                    retired.append(row['generation'])
            if owner is None:
                active=[dict(r) for r in c.execute("SELECT * FROM watchdogs WHERE state='ACTIVE' AND born_at<=? ORDER BY born_at",(at,))]
            else:
                active=[dict(r) for r in c.execute("SELECT * FROM watchdogs WHERE owner_thread_id=? AND state='ACTIVE' AND born_at<=? ORDER BY born_at",(owner,at))]
            next_birth=(max((r['born_at'] for r in active),default=at-self.config.spawn_interval_minutes*60)+self.config.spawn_interval_minutes*60)
            return {'at':at,'active':active,'retired':retired,'active_count':len(active),'next_birth_at':next_birth,
                    'desired_replicas':self.config.watchdog_replicas,'owner_thread_id':owner}
