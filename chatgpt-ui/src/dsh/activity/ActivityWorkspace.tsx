import { useEffect, useMemo, useRef, useState } from "react";
import { invoke } from "@tauri-apps/api/core";
import type { ConversationRecord, ConversationRegistry } from "../../conversation-registry.ts";
import { ConversationShell, type AgentRelationItem, type WatchdogViewState } from "../conversation/ConversationShell.tsx";
import { StateDot, type StateDotState } from "../primitives/StateDot.tsx";
import css from "./ActivityWorkspace.module.css";

export interface ViewerPreferences {
  selectedTaskId?: string;
  aliases: Record<string, string>;
  archived: string[];
  deleted: string[];
}

interface Props {
  registry: ConversationRegistry;
  revision: number;
  selectedTaskId: string | null;
  preferences: ViewerPreferences;
  showArchived: boolean;
  onSelect: (taskId: string) => void;
  onShowArchived: (show: boolean) => void;
  onRename: (taskId: string, title: string) => void;
  onArchive: (taskId: string, archived: boolean) => void;
  onDelete: (taskId: string) => void;
  agentSnapshotOverride?: AgentRelationSnapshot;
}

interface AgentRelationAgent {
  logical_agent_id: string;
  short_id: string;
  agent_path: string;
  parent_agent_id?: string | null;
  runtime_thread_id?: string | null;
  scope_thread_id: string;
  display_name: string;
  state: string;
  conversation_id_suffix?: string;
}

interface AgentWatchdogScope {
  scope_thread_id: string;
  enabled?: boolean;
  manual_enabled?: boolean;
  manual_disabled?: boolean;
  source?: string;
  estimated_minutes?: number | null;
}

export interface AgentRelationSnapshot {
  version: number;
  updated_at: number;
  agents: AgentRelationAgent[];
  watchdogs: AgentWatchdogScope[];
}

const EMPTY_RELATIONS: AgentRelationSnapshot = { version: 1, updated_at: 0, agents: [], watchdogs: [] };

function parseAgentSnapshot(raw: string): AgentRelationSnapshot {
  try {
    const value = JSON.parse(raw) as Partial<AgentRelationSnapshot>;
    if (!Array.isArray(value.agents) || !Array.isArray(value.watchdogs)) return EMPTY_RELATIONS;
    return {
      version: typeof value.version === "number" ? value.version : 1,
      updated_at: typeof value.updated_at === "number" ? value.updated_at : 0,
      agents: value.agents.slice(0, 200),
      watchdogs: value.watchdogs.slice(0, 100),
    };
  } catch {
    return EMPTY_RELATIONS;
  }
}

function preferredThreadRecord(registry: ConversationRegistry, threadId?: string | null): ConversationRecord | undefined {
  if (!threadId) return undefined;
  const records = registry.thread(threadId);
  return records.find((record) => record.store.lifecycle === "active") ?? records[0];
}

function stateFor(record: ConversationRecord): StateDotState {
  switch (record.store.lifecycle) {
    case "active": return "ongoing";
    case "failed": return "error";
    case "ended_missing_completion": return "warning";
    default: return "done";
  }
}

function normalizeWorkspace(path: string): string {
  return path.replace(/\//g, "\\").replace(/\\+$/, "");
}

function workspaceName(path: string): string {
  const normalized = normalizeWorkspace(path);
  if (!normalized) return "其他";
  const lower = normalized.toLowerCase();
  if (lower.endsWith("\\workspaces\\desktop-ops")) return "桌面操作";
  if (lower.endsWith("\\runtime\\maintenance-workspace")) return "运行底座维护";
  if (/^c:\\users\\[^\\]+\\appdata\\local\\temp$/.test(lower)) return "临时任务";
  const parts = normalized.split("\\");
  return parts.at(-1) || normalized;
}

function timeText(value: number): string {
  const date = new Date(value);
  const now = new Date();
  const sameDay = date.getFullYear() === now.getFullYear() && date.getMonth() === now.getMonth() && date.getDate() === now.getDate();
  if (sameDay) return date.toLocaleTimeString("zh-CN", { hour: "2-digit", minute: "2-digit", hour12: false });
  return date.toLocaleDateString("zh-CN", { month: "2-digit", day: "2-digit" });
}

function titleFor(record: ConversationRecord, preferences: ViewerPreferences): string {
  return preferences.aliases[record.id]?.trim() || record.store.taskTitle || "未命名会话";
}

function groupRecords(records: ConversationRecord[]): Array<{ workspace: string; records: ConversationRecord[] }> {
  const groups = new Map<string, { workspace: string; records: ConversationRecord[] }>();
  for (const record of records) {
    const workspace = normalizeWorkspace(record.store.workspace || "");
    const key = workspace.toLowerCase();
    const group = groups.get(key) ?? { workspace, records: [] };
    group.records.push(record);
    groups.set(key, group);
  }
  return [...groups.values()]
    .map((group) => ({
      ...group,
      records: group.records.sort((a, b) => b.updatedAt - a.updatedAt || b.startedAt - a.startedAt),
    }))
    .sort((a, b) => (b.records[0]?.updatedAt ?? 0) - (a.records[0]?.updatedAt ?? 0));
}

export function ActivityWorkspace({
  registry,
  revision,
  selectedTaskId,
  preferences,
  showArchived,
  onSelect,
  onShowArchived,
  onRename,
  onArchive,
  onDelete,
  agentSnapshotOverride,
}: Props) {
  const [menuTaskId, setMenuTaskId] = useState<string | null>(null);
  const [renameTaskId, setRenameTaskId] = useState<string | null>(null);
  const [renameValue, setRenameValue] = useState("");
  const [sortByTime, setSortByTime] = useState(false);
  const [agentSnapshot, setAgentSnapshot] = useState<AgentRelationSnapshot>(agentSnapshotOverride ?? EMPTY_RELATIONS);
  const renameInputRef = useRef<HTMLInputElement>(null);
  useEffect(() => {
    if (renameTaskId) {
      renameInputRef.current?.focus();
      renameInputRef.current?.select();
    }
  }, [renameTaskId]);

  useEffect(() => {
    if (agentSnapshotOverride) {
      setAgentSnapshot(agentSnapshotOverride);
      return;
    }
    let cancelled = false;
    const refresh = async () => {
      try {
        const raw = await invoke<string>("activity_agent_snapshot");
        if (!cancelled) setAgentSnapshot(parseAgentSnapshot(raw));
      } catch {
        if (!cancelled) setAgentSnapshot(EMPTY_RELATIONS);
      }
    };
    void refresh();
    const timer = window.setInterval(() => void refresh(), 500);
    return () => { cancelled = true; window.clearInterval(timer); };
  }, [agentSnapshotOverride]);

  const archived = useMemo(() => new Set(preferences.archived), [preferences.archived]);
  const deleted = useMemo(() => new Set(preferences.deleted), [preferences.deleted]);

  const visibleRecords = registry.list().filter((record) => {
    if (deleted.has(record.id)) return false;
    return showArchived ? archived.has(record.id) : !archived.has(record.id);
  });
  const groups = groupRecords(visibleRecords);
  const selected = registry.get(selectedTaskId);
  const selectedVisible = selected && !deleted.has(selected.id) && (showArchived ? archived.has(selected.id) : !archived.has(selected.id)) ? selected : undefined;
  const displayTitle = selectedVisible ? titleFor(selectedVisible, preferences) : undefined;
  const relationshipView = useMemo(() => {
    if (!selectedVisible) return { relations: [] as AgentRelationItem[], watchdog: undefined as WatchdogViewState | undefined };
    const selectedThread = selectedVisible.store.threadId || selectedVisible.store.taskId;
    const currentAgent = agentSnapshot.agents.find((agent) => agent.runtime_thread_id === selectedThread);
    const scopeThread = currentAgent?.scope_thread_id || selectedThread;
    const monitorableAgentCount = agentSnapshot.agents.filter((agent) =>
      agent.scope_thread_id === scopeThread
      && Boolean(agent.runtime_thread_id)
      && !["DONE", "CLOSED", "BLOCKED", "EXTERNAL_ACTIVITY"].includes(agent.state),
    ).length;
    const relationByTask = new Map<string, AgentRelationItem>();
    const addRelation = (record: ConversationRecord | undefined, relation: AgentRelationItem["relation"], label: string, depth = 0) => {
      if (!record || record.id === selectedVisible.id || relationByTask.has(record.id)) return;
      relationByTask.set(record.id, { taskId: record.id, label, relation, depth, lifecycle: record.store.lifecycle });
    };

    const topLevel = preferredThreadRecord(registry, scopeThread);
    if (topLevel && topLevel.id !== selectedVisible.id) addRelation(topLevel, "root", titleFor(topLevel, preferences));

    if (currentAgent?.parent_agent_id) {
      const parent = agentSnapshot.agents.find((agent) => agent.logical_agent_id === currentAgent.parent_agent_id);
      if (parent) addRelation(preferredThreadRecord(registry, parent.runtime_thread_id), "parent", parent.display_name || parent.agent_path);
    }

    const pathPrefix = currentAgent ? `${currentAgent.agent_path}/` : "";
    for (const agent of agentSnapshot.agents) {
      if (agent.scope_thread_id !== scopeThread || !agent.runtime_thread_id) continue;
      if (currentAgent && agent.logical_agent_id === currentAgent.logical_agent_id) continue;
      if (currentAgent && !agent.agent_path.startsWith(pathPrefix)) continue;
      const relativeDepth = currentAgent
        ? Math.max(1, agent.agent_path.slice(pathPrefix.length).split("/").filter(Boolean).length)
        : Math.max(1, agent.agent_path.split("/").filter(Boolean).length - 1);
      addRelation(
        preferredThreadRecord(registry, agent.runtime_thread_id),
        relativeDepth === 1 ? "child" : "descendant",
        agent.display_name || agent.agent_path,
        relativeDepth,
      );
    }

    const scope = agentSnapshot.watchdogs.find((item) => item.scope_thread_id === scopeThread);
    const inherited = Boolean(currentAgent && selectedThread !== scopeThread);
    const watchdog: WatchdogViewState = {
      enabled: Boolean(scope?.manual_enabled),
      manualDisabled: Boolean(scope?.manual_disabled),
      inherited,
      canToggle: selectedVisible.store.lifecycle === "active" && !inherited,
      scopeThreadId: scopeThread,
      agentCount: monitorableAgentCount,
      estimatedMinutes: typeof scope?.estimated_minutes === "number" ? scope.estimated_minutes : undefined,
    };
    return { relations: [...relationByTask.values()], watchdog };
  }, [agentSnapshot, preferences, registry, selectedVisible]);

  const beginRename = (record: ConversationRecord) => {
    setMenuTaskId(null);
    setRenameTaskId(record.id);
    setRenameValue(titleFor(record, preferences));
  };

  const finishRename = () => {
    if (!renameTaskId) return;
    const value = renameValue.trim();
    if (value) onRename(renameTaskId, value);
    setRenameTaskId(null);
  };

  const renderRecord = (record: ConversationRecord) => {
    const selectedRow = selectedTaskId === record.id;
    const renaming = renameTaskId === record.id;
    const rowContent = <>
      <StateDot state={stateFor(record)} className={css.sessionDot} />
      <span className={css.sessionText}>
        {renaming ? (
          <input ref={renameInputRef} className={css.renameInput} value={renameValue} aria-label="会话名称"
            onChange={(event) => setRenameValue(event.target.value)}
            onKeyDown={(event) => {
              if (event.key === "Enter") finishRename();
              if (event.key === "Escape") setRenameTaskId(null);
            }} onBlur={finishRename} />
        ) : <span className={css.sessionTitle}>{titleFor(record, preferences)}</span>}
        <span className={css.sessionMeta}>{timeText(record.updatedAt)}</span>
      </span>
    </>;
    return (
      <div
        className={css.sessionWrap}
        key={record.id}
        data-selected={selectedRow || undefined}
        data-task-id={record.id}
        data-lifecycle={record.store.lifecycle ?? "idle"}
      >
        {renaming ? <div className={css.session} title={record.store.workspace}>{rowContent}</div> : (
          <button type="button" className={css.session} aria-pressed={selectedRow}
            onClick={() => onSelect(record.id)} title={record.store.workspace}>{rowContent}</button>
        )}
        {!renaming && (
          <button
            type="button"
            className={css.more}
            aria-label="会话操作"
            onClick={(event) => {
              event.stopPropagation();
              setMenuTaskId((value) => value === record.id ? null : record.id);
            }}
          >···</button>
        )}
        {menuTaskId === record.id && (
          <div className={css.menu} role="menu">
            <button type="button" onClick={() => beginRename(record)}>重命名</button>
            <button type="button" onClick={() => { setMenuTaskId(null); onArchive(record.id, !archived.has(record.id)); }}>{archived.has(record.id) ? "取消归档" : "归档"}</button>
            <button
              type="button"
              className={css.danger}
              onClick={() => {
                setMenuTaskId(null);
                if (window.confirm(`从 Viewer 中删除“${titleFor(record, preferences)}”？\n\n不会删除 MCP 审计事件。`)) onDelete(record.id);
              }}
            >删除</button>
          </div>
        )}
      </div>
    );
  };

  return (
    <main className={css.root} data-testid="activity-workspace">
      <aside className={css.sidebar} data-testid="conversation-sidebar">
        <div className={css.brand}>
          <div className={css.brandTitle}>GPT-PC</div>
          <div className={css.brandSub}>Agent Activity Console</div>
        </div>
        <div className={css.tabs}>
          <button type="button" className={css.tab} data-active={!showArchived || undefined} onClick={() => onShowArchived(false)}>会话</button>
          <button type="button" className={css.tab} data-active={showArchived || undefined} onClick={() => onShowArchived(true)}>已归档</button>
        </div>
        <div className={css.sortBar}>
          <button
            type="button"
            className={css.sortToggle}
            data-testid="sidebar-sort-toggle"
            data-mode={sortByTime ? "time" : "workspace"}
            aria-pressed={sortByTime}
            onClick={() => setSortByTime((value) => !value)}
          >
            {sortByTime ? "按工作区" : "按时间"}
          </button>
        </div>
        <div className={css.groups} data-testid="sidebar-scroll">
          {visibleRecords.length === 0 ? (
            <div className={css.sidebarEmpty}>{showArchived ? "暂无归档会话" : "等待新的 workspace lease…"}</div>
          ) : sortByTime ? (
            <div className={css.timeList} data-testid="sidebar-time-list">
              {visibleRecords.map(renderRecord)}
            </div>
          ) : groups.map((group) => (
            <section className={css.group} key={group.workspace || "__other"}>
              <div className={css.groupHeader} title={group.workspace || "其他活动"}>
                <span className={css.groupName}>{workspaceName(group.workspace)}</span>
                <span className={css.groupCount}>{group.records.length}</span>
              </div>
              <div className={css.groupList}>{group.records.map(renderRecord)}</div>
            </section>
          ))}
        </div>
      </aside>

      <section className={css.main}>
        {selectedVisible ? (
          <ConversationShell
            store={selectedVisible.store}
            revision={revision}
            displayTitle={displayTitle}
            relatedLeaseCount={registry.activeThread(selectedVisible.store.threadId).length}
            relations={relationshipView.relations}
            watchdog={relationshipView.watchdog}
            onSelectRelation={onSelect}
          />
        ) : (
          <div className={css.welcome} data-testid="conversation-empty">
            <strong>GPT-PC</strong>
            <span>{showArchived ? "选择一个归档会话查看活动" : "选择左侧会话，或等待新的 Agent 会话"}</span>
            <small>输入框只控制当前租约；同一 GPT thread 的活跃兄弟租约仅共享公开协调状态。</small>
          </div>
        )}
      </section>
    </main>
  );
}
