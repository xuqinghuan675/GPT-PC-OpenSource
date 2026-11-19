import { useCallback, useEffect, useRef, useState, type ClipboardEvent, type DragEvent, type KeyboardEvent } from "react";
import { invoke } from "@tauri-apps/api/core";
import type { SessionStore } from "../../session-store.ts";
import { loadHistoryPage, HISTORY_PAGE_SIZE, MAX_HISTORY_ITEMS, type ActivityHistoryPage } from "../../history-pages.ts";
import { presentViewItem } from "../../presentation/dsh-presentation-adapter.ts";
import type { DshPresentationItem } from "../../presentation/types.ts";
import { StateDot, type StateDotState } from "../primitives/StateDot.tsx";
import { UserMessage, AssistantMessage } from "./MessageItems.tsx";
import { AgentObservation } from "./VisualItems.tsx";
import { VisualReferencePanel } from "./VisualReferencePanel.tsx";
import { ReasoningRow } from "./ReasoningRow.tsx";
import { ToolRow } from "./ToolRow.tsx";
import css from "./Shell.module.css";

const MAX_ATTACHMENTS = 8;
const MAX_ATTACHMENT_BYTES = 25 * 1024 * 1024;
const MAX_ATTACHMENT_TOTAL_BYTES = 50 * 1024 * 1024;
const ATTACHMENT_READ_TIMEOUT_MS = 15_000;
const RESIDENT_HEARTBEAT_STALE_MS = 5 * 60 * 1000;

interface PendingAttachment {
  key: string;
  file: File;
}

function attachmentKey(file: File): string {
  return `${file.name}:${file.size}:${file.lastModified}:${file.type}`;
}

function formatAttachmentBytes(bytes: number): string {
  if (bytes < 1024) return `${bytes} B`;
  if (bytes < 1024 * 1024) return `${(bytes / 1024).toFixed(1)} KB`;
  return `${(bytes / 1024 / 1024).toFixed(1)} MB`;
}

function fileToBase64(file: File): Promise<string> {
  return new Promise((resolve, reject) => {
    const reader = new FileReader();
    const timeout = window.setTimeout(() => {
      if (reader.readyState === FileReader.LOADING) reader.abort();
      reject(new Error(`读取 ${file.name || "剪贴板附件"} 超时，请重新粘贴或选择文件`));
    }, ATTACHMENT_READ_TIMEOUT_MS);
    reader.onerror = () => {
      window.clearTimeout(timeout);
      reject(reader.error ?? new Error(`读取 ${file.name} 失败`));
    };
    reader.onabort = () => {
      window.clearTimeout(timeout);
      reject(new Error(`读取 ${file.name || "剪贴板附件"} 已中止`));
    };
    reader.onload = () => {
      window.clearTimeout(timeout);
      const result = String(reader.result ?? "");
      const comma = result.indexOf(",");
      if (comma < 0) reject(new Error(`读取 ${file.name} 失败`));
      else resolve(result.slice(comma + 1));
    };
    reader.readAsDataURL(file);
  });
}

function statusState(store: SessionStore, residentDisconnected: boolean, heartbeatStale: boolean): StateDotState {
  if (store.lifecycle === "active" && (residentDisconnected || heartbeatStale)) return "warning";
  switch (store.lifecycle) {
    case "active": return "ongoing";
    case "failed": return "error";
    case "ended_missing_completion": return "warning";
    default: return "done";
  }
}

function statusText(store: SessionStore, residentDisconnected: boolean, heartbeatStale: boolean): string {
  switch (store.lifecycle) {
    case "completed": return "已完成";
    case "failed": return "失败";
    case "ended_missing_completion": return "已结束 · 完成事件缺失";
    case "active":
      if (heartbeatStale) {
        if (store.controlAckRequired) return "GPT 无响应 · 待回应用户";
        if (store.controlResumeRequired) return "GPT 无响应 · 待续作";
        if (residentDisconnected) return "GPT 已断开 · 驻场保留";
        return "GPT 无可信活动 · 租约仍持有";
      }
      if (residentDisconnected) {
        if (store.controlAckRequired) return "GPT 已断开 · 待唤醒回应";
        return "GPT 已断开 · 驻场保留";
      }
      if (store.controlAckRequired) return "等待回应用户";
      if (store.controlResumeRequired) return "等待模型续作";
      switch (store.activityStatus) {
        case "Running tool": return "正在调用工具";
        case "Verifying": return "验证中";
        case "Sleeping": return "Sleeping...";
        default: return "进行中";
      }
    default: return "待机";
  }
}

function durationText(ms: number): string {
  if (ms < 1000) return `${Math.round(ms)}ms`;
  const seconds = ms / 1000;
  if (seconds < 60) return `${seconds.toFixed(seconds < 10 ? 1 : 0)}s`;
  const minutes = Math.floor(seconds / 60);
  return `${minutes}m ${Math.floor(seconds % 60)}s`;
}

function stayRemainingText(ms: number): string {
  const seconds = Math.max(0, Math.ceil(ms / 1000));
  const hours = Math.floor(seconds / 3600);
  const minutes = Math.floor((seconds % 3600) / 60);
  const remainingSeconds = seconds % 60;
  if (hours > 0) return `${hours}h ${minutes}m`;
  if (minutes > 0) return `${minutes}m ${remainingSeconds}s`;
  return `${remainingSeconds}s`;
}

function leaseTypeText(workspace: string): string {
  const normalized = workspace.replace(/\//g, "\\").replace(/\\+$/, "").toLowerCase();
  if (normalized.endsWith("\\runtime\\maintenance-workspace")) return "管理员维护租约";
  if (normalized.endsWith("\\workspaces\\desktop-ops")) return "桌面操作租约";
  return workspace ? "项目租约" : "租约";
}

function leaseStateText(store: SessionStore): string {
  if (store.lifecycle === "active") return "持有中";
  if (store.lifecycle) return "已释放";
  return "无租约";
}

function renderItem(item: DshPresentationItem, taskId: string) {
  switch (item.kind) {
    case "user": return <UserMessage taskId={taskId} id={item.id} text={item.text} attachments={item.attachments} />;
    case "observation": return <AgentObservation taskId={taskId} observation={item.observation} />;
    case "reasoning": return <ReasoningRow id={item.id} text={item.text} running={item.running} phase={item.phase} />;
    case "assistant": return <AssistantMessage id={item.id} text={item.text} />;
    case "tool": return <ToolRow item={item} />;
  }
}

interface ActivityDigest {
  current: string;
  keyFiles: string[];
  actions: string[];
  failures: string[];
}

function digestText(text: string, limit = 160): string {
  const oneLine = text.replace(/\s+/g, " ").trim();
  return oneLine.length <= limit ? oneLine : `${oneLine.slice(0, limit - 1)}…`;
}

function buildActivityDigest(items: DshPresentationItem[]): ActivityDigest {
  const keyFiles: string[] = [];
  const actions: string[] = [];
  const failures: string[] = [];
  let current = "";
  const remember = (target: string[], value: string, limit: number) => {
    const normalized = digestText(value);
    if (normalized && !target.includes(normalized) && target.length < limit) target.push(normalized);
  };
  for (let index = items.length - 1; index >= 0; index -= 1) {
    const item = items[index];
    if (item.kind === "tool") {
      const action = `${item.title}${item.summary ? ` · ${item.summary}` : ""}`;
      remember(actions, action, 4);
      if (["edit", "delete", "move"].includes(item.toolKind)) remember(keyFiles, item.summary, 4);
      if (item.status === "error") remember(failures, action, 3);
      if (!current && item.status === "running") current = action;
    } else if (!current && item.kind === "reasoning" && item.running) {
      current = digestText(item.text || item.phase || "推理中");
    }
  }
  return { current, keyFiles, actions, failures };
}

export interface AgentRelationItem {
  taskId: string;
  label: string;
  relation: "root" | "parent" | "child" | "descendant";
  depth: number;
  lifecycle?: string | null;
}

export interface WatchdogViewState {
  enabled: boolean;
  manualDisabled: boolean;
  inherited: boolean;
  canToggle: boolean;
  scopeThreadId: string;
  agentCount: number;
  estimatedMinutes?: number;
}

interface Props {
  store: SessionStore;
  revision: number;
  displayTitle?: string;
  relatedLeaseCount?: number;
  relations?: AgentRelationItem[];
  watchdog?: WatchdogViewState;
  onSelectRelation?: (taskId: string) => void;
}

export function ConversationShell({
  store, revision, displayTitle, relatedLeaseCount = 1,
  relations = [], watchdog, onSelectRelation,
}: Props) {
  void revision;
  const [now, setNow] = useState(() => Date.now());
  const [draft, setDraft] = useState("");
  const [sending, setSending] = useState(false);
  const [sendError, setSendError] = useState("");
  const [ultraSending, setUltraSending] = useState(false);
  const [ultraError, setUltraError] = useState("");
  const [watchdogSending, setWatchdogSending] = useState(false);
  const [watchdogError, setWatchdogError] = useState("");
  const [attachments, setAttachments] = useState<PendingAttachment[]>([]);
  const fileInputRef = useRef<HTMLInputElement>(null);
  const composerInputRef = useRef<HTMLTextAreaElement>(null);
  const sendButtonRef = useRef<HTMLButtonElement>(null);

  const [historyPage, setHistoryPage] = useState<ActivityHistoryPage | null>(null);
  const [historyLoading, setHistoryLoading] = useState(false);
  const [historyError, setHistoryError] = useState("");
  const historyRequestRef = useRef(0);
  const nativeHistory = typeof window !== "undefined" && "__TAURI_INTERNALS__" in window;

  const requestHistory = useCallback((before?: number, reset = false): void => {
    if (!nativeHistory || !store.taskId) return;
    const ticket = ++historyRequestRef.current;
    if (reset) { setHistoryPage(null); setHistoryError(""); }
    setHistoryLoading(true);
    void loadHistoryPage({
      taskId: store.taskId, threadId: store.threadId,
      workspace: store.workspace, title: store.taskTitle,
    }, before).then((result) => {
      if (ticket !== historyRequestRef.current) return;
      setHistoryPage(result);
      setHistoryLoading(false);
      window.requestAnimationFrame(() => window.requestAnimationFrame(() => {
        const node = document.querySelector<HTMLElement>("[data-conversation-scroll]");
        if (node) node.scrollTop = before === undefined ? node.scrollHeight : 0;
      }));
    }).catch((error: unknown) => {
      if (ticket !== historyRequestRef.current) return;
      setHistoryLoading(false);
      setHistoryError(error instanceof Error ? error.message : String(error));
    });
  }, [nativeHistory, store.taskId, store.threadId, store.workspace, store.taskTitle]);

  useEffect(() => {
    if (nativeHistory && store.taskId) requestHistory(undefined, true);
    return () => { historyRequestRef.current += 1; };
  }, [nativeHistory, store.taskId, requestHistory]);

  useEffect(() => {
    if (!store.taskId || store.lifecycle !== "active") return;
    setNow(Date.now());
    const timer = window.setInterval(() => setNow(Date.now()), 1000);
    return () => window.clearInterval(timer);
  }, [store.lifecycle, store.taskId]);

  const page = historyPage?.taskId === store.taskId ? historyPage : null;
  const liveItems = store.order.flatMap((id) => {
    const item = store.items.get(id);
    return item ? [item] : [];
  });
  let shownItems = page ? page.items : liveItems;
  if (page && page.end === page.total && liveItems.length) {
    const merged = new Map(page.items.map((item) => [item.id, item] as const));
    const threshold = page.items[0]?.occurredAtMs ?? 0;
    for (const item of liveItems) {
      if (merged.has(item.id) || (item.occurredAtMs ?? 0) >= threshold) merged.set(item.id, item);
    }
    shownItems = [...merged.values()].sort((a, b) =>
      (a.occurredAtMs ?? 0) - (b.occurredAtMs ?? 0)
    ).slice(-HISTORY_PAGE_SIZE);
  }
  const items = shownItems.map(presentViewItem);
  const activityDigest = buildActivityDigest(liveItems.map(presentViewItem));
  const summary = store.taskSummary;
  const liveDurationMs = store.lifecycle === "active" && store.startedAt > 0
    ? Math.max(0, now - store.startedAt)
    : summary.durationMs;
  const title = displayTitle || store.taskTitle || "GPT-PC 会话";
  const threadId = store.threadId || store.taskId;
  const canSend = store.lifecycle === "active" && Boolean(store.taskId && threadId);
  const stayActive = store.lifecycle === "active" && store.stayUntilAt > now;
  const stayRemainingMs = stayActive ? store.stayUntilAt - now : 0;
  const visualUntil = Date.parse(store.visualContext?.until_at ?? "");
  const visualActive = store.lifecycle === "active" && store.visualContext?.active === true && visualUntil > now;
  const heartbeatStale = store.lifecycle === "active"
    && store.residentHeartbeatAt > 0
    && now - store.residentHeartbeatAt > RESIDENT_HEARTBEAT_STALE_MS;
  const residentDisconnected = stayActive && heartbeatStale;
  const residentHeartbeatText = store.residentHeartbeatAt > 0
    ? new Date(store.residentHeartbeatAt).toLocaleTimeString()
    : "未知";
  const ultraOn = store.lifecycle === "active" && store.ultraMode?.active === true;

  async function toggleUltra(): Promise<void> {
    if (!canSend || ultraSending) return;
    setUltraSending(true);
    setUltraError("");
    try {
      await invoke("activity_ultra_update", {
        taskId: store.taskId,
        threadId,
        workspace: store.workspace,
        ultraExpectedRevision: store.ultraMode?.updated_at ?? "",
        enabled: !ultraOn,
      });
    } catch (error) {
      setUltraError(error instanceof Error ? error.message : String(error));
    } finally {
      setUltraSending(false);
    }
  }

  async function toggleWatchdog(): Promise<void> {
    if (!watchdog?.canToggle || watchdogSending || !canSend) return;
    setWatchdogSending(true);
    setWatchdogError("");
    try {
      await invoke("activity_watchdog_update", {
        taskId: store.taskId,
        threadId,
        workspace: store.workspace,
        enabled: !watchdog.enabled,
      });
    } catch (error) {
      setWatchdogError(error instanceof Error ? error.message : String(error));
    } finally {
      setWatchdogSending(false);
    }
  }

  function addFiles(files: File[]): void {
    if (!canSend || files.length === 0) return;
    setSendError("");
    setAttachments((current) => {
      const next = [...current];
      const known = new Set(next.map((item) => item.key));
      let total = next.reduce((sum, item) => sum + item.file.size, 0);
      for (const file of files) {
        if (next.length >= MAX_ATTACHMENTS) {
          setSendError(`最多添加 ${MAX_ATTACHMENTS} 个附件`);
          break;
        }
        if (file.size > MAX_ATTACHMENT_BYTES) {
          setSendError(`${file.name} 超过 25 MB`);
          continue;
        }
        if (total + file.size > MAX_ATTACHMENT_TOTAL_BYTES) {
          setSendError("附件总大小不能超过 50 MB");
          break;
        }
        const key = attachmentKey(file);
        if (known.has(key)) continue;
        known.add(key);
        total += file.size;
        next.push({ key, file });
      }
      return next;
    });
  }

  function removeAttachment(key: string): void {
    setAttachments((current) => current.filter((item) => item.key !== key));
  }

  async function submit(): Promise<void> {
    const text = draft.trim();
    if (!canSend || (!text && attachments.length === 0) || sending) return;
    const previousDraft = draft;
    const sentAttachments = attachments;
    const restoreFocus = document.activeElement === composerInputRef.current
      || document.activeElement === sendButtonRef.current;
    setSending(true);
    setSendError("");
    setDraft("");
    setAttachments([]);
    if (fileInputRef.current) fileInputRef.current.value = "";
    if (restoreFocus) composerInputRef.current?.focus({ preventScroll: true });
    try {
      const encodedAttachments = await Promise.all(sentAttachments.map(async ({ file }) => ({
        name: file.name || "clipboard-image.png",
        mime: file.type || "application/octet-stream",
        dataBase64: await fileToBase64(file),
      })));
      await invoke<string>("activity_submit", {
        taskId: store.taskId,
        threadId,
        workspace: store.workspace,
        text,
        attachments: encodedAttachments,
      });
    } catch (error) {
      setSendError(error instanceof Error ? error.message : String(error));
      setDraft((current) => previousDraft
        ? (current ? `${previousDraft}\n${current}` : previousDraft)
        : current);
      setAttachments((current) => {
        const currentKeys = new Set(current.map((item) => item.key));
        return [...sentAttachments.filter((item) => !currentKeys.has(item.key)), ...current];
      });
    } finally {
      setSending(false);
    }
  }

  function onComposerPaste(event: ClipboardEvent<HTMLTextAreaElement>) {
    const files = Array.from(event.clipboardData.files)
      .filter((file) => file.type.startsWith("image/"));
    if (files.length === 0) return;
    event.preventDefault();
    addFiles(files);
  }

  function onComposerDrop(event: DragEvent<HTMLFieldSetElement>) {
    event.preventDefault();
    addFiles(Array.from(event.dataTransfer.files));
  }

  function onComposerKeyDown(event: KeyboardEvent<HTMLTextAreaElement>) {
    if (event.key !== "Enter" || event.shiftKey || event.nativeEvent.isComposing) return;
    event.preventDefault();
    void submit();
  }

  return (
    <main
      className={css.root}
      data-testid="dsh-conversation-root"
      data-agent-status={store.agentStatus}
      data-activity-status={store.activityStatus}
      data-lifecycle={store.lifecycle ?? "idle"}
      data-agent-liveness={heartbeatStale ? "stale" : "recent"}
      data-resident-liveness={stayActive ? (residentDisconnected ? "disconnected" : "connected") : "inactive"}
      data-task-id={store.taskId}
      data-thread-id={threadId}
    >
      <header className={css.header}>
        <div className={css.title} title={title}>{title}</div>
        <div className={css.meta} data-testid="task-context" title={store.workspace}>{store.workspace || "本机 Agent 活动"}</div>
        <div className={css.headerRelations}>
          {relations.length > 0 ? (
            <details className={css.relationGroup}>
              <summary className={css.relationToggle} data-testid="agent-relations-toggle">
                Agents · {relations.length}<span aria-hidden="true">⌄</span>
              </summary>
              <div className={css.relationMenu} role="menu" data-testid="agent-relations-menu">
                {relations.map((relation) => (
                  <button
                    key={relation.taskId}
                    type="button"
                    role="menuitem"
                    className={css.relationItem}
                    data-relation={relation.relation}
                    data-task-id={relation.taskId}
                    onClick={(event) => {
                      const details = event.currentTarget.closest("details");
                      if (details) details.open = false;
                      onSelectRelation?.(relation.taskId);
                    }}
                  >
                    <span className={css.relationRole}>
                      {relation.relation === "root" ? "总对话" : relation.relation === "parent" ? "父 Agent" : relation.relation === "child" ? "子 Agent" : `后代 L${relation.depth}`}
                    </span>
                    <span className={css.relationLabel}>{relation.label}</span>
                  </button>
                ))}
              </div>
            </details>
          ) : null}
          {watchdog ? (
            <div
              className={css.watchdogChip}
              data-testid="watchdog-scope-status"
              data-active={watchdog.enabled || undefined}
              data-manual-off={watchdog.manualDisabled || undefined}
              title={watchdogError || "由网页 GPT 自己建立监督对话、查看页面并决定是否提醒；开关不代表已经启动或持续在线"}
            >
              <span className={css.watchdogDot} aria-hidden="true" />
              <span>{watchdog.inherited ? "继承监督规则" : "看门狗"} · {
                watchdog.enabled
                  ? "已允许手动监督"
                  : watchdog.manualDisabled ? "已手动关闭" : "未启用"
              }</span>
              {watchdog.canToggle ? (
                <button
                  type="button"
                  data-testid="watchdog-scope-toggle"
                  disabled={watchdogSending || !canSend}
                  onClick={() => void toggleWatchdog()}
                >{watchdogSending ? "…" : watchdog.enabled ? "关闭" : "开启"}</button>
              ) : null}
            </div>
          ) : null}
        </div>
        <div className={css.headerRight}>
          <div className={css.leaseMeta} data-testid="lease-meta">
            {leaseTypeText(store.workspace)} · {leaseStateText(store)} · {durationText(liveDurationMs)}
            {relatedLeaseCount > 1 ? ` · 同线程 ${relatedLeaseCount} 租约` : ""}
          </div>
          {stayActive ? (
            <div
              className={css.stayBadge}
              data-testid="stay-mode"
              data-resident-liveness={residentDisconnected ? "disconnected" : "connected"}
              title={residentDisconnected
                ? `驻场租约有效至 ${new Date(store.stayUntilAt).toLocaleTimeString()}；GPT 最近 heartbeat ${residentHeartbeatText}，已超过 5 分钟无活动`
                : `驻场模式有效至 ${new Date(store.stayUntilAt).toLocaleTimeString()}；GPT 最近 heartbeat ${residentHeartbeatText}`}
            >
              {residentDisconnected ? "驻场保留" : "驻场"} · {stayRemainingText(stayRemainingMs)}
            </div>
          ) : null}
          {visualActive ? <div className={css.visualBadge} data-testid="visual-mode" title={`看图模式有效至 ${new Date(visualUntil).toLocaleTimeString()}`}>看图 · {stayRemainingText(visualUntil - now)}</div> : null}
          <div className={css.status} data-testid="agent-status"
            title={heartbeatStale ? `租约仍在，但 GPT 最近可信活动停在 ${residentHeartbeatText}；这不等价于模型 turn 仍在运行` : undefined}>
            <StateDot state={statusState(store, residentDisconnected, heartbeatStale)} />{statusText(store, residentDisconnected, heartbeatStale)}
          </div>
        </div>
      </header>
      {store.taskId ? <VisualReferencePanel key={store.taskId} taskId={store.taskId} threadId={threadId} workspace={store.workspace}
        context={store.visualContext} canEdit={canSend} /> : null}
      <div className={css.summaryBar} data-testid="task-summary">
        <span className={css.summaryLabel}>任务摘要</span>
        <span>文件 <b>{summary.filesChanged}</b></span>
        <span className={css.add}>+{summary.additions}</span>
        <span className={css.del}>-{summary.deletions}</span>
        <span>工具 <b>{summary.toolCalls}</b></span>
        <span>用时 <b>{durationText(liveDurationMs)}</b></span>
      </div>
      {(activityDigest.current || activityDigest.keyFiles.length > 0 || activityDigest.actions.length > 0 || activityDigest.failures.length > 0) ? (
        <details className={css.activityDigest} data-testid="activity-digest">
          <summary>
            <strong>关键活动</strong>
            {activityDigest.current ? <span>当前 · {activityDigest.current}</span> : null}
            {!activityDigest.current && activityDigest.keyFiles[0] ? <span>最近文件 · {activityDigest.keyFiles[0]}</span> : null}
            {activityDigest.failures.length > 0 ? <span className={css.digestFailure}>失败 {activityDigest.failures.length}</span> : null}
          </summary>
          <div className={css.digestGrid}>
            {activityDigest.keyFiles.length > 0 ? <div><b>关键文件</b>{activityDigest.keyFiles.map((value) => <code key={value}>{value}</code>)}</div> : null}
            {activityDigest.actions.length > 0 ? <div><b>主要动作</b>{activityDigest.actions.map((value) => <span key={value}>{value}</span>)}</div> : null}
            {activityDigest.failures.length > 0 ? <div><b>失败记录</b>{activityDigest.failures.map((value) => <span className={css.digestFailure} key={value}>{value}</span>)}</div> : null}
          </div>
        </details>
      ) : null}
      {nativeHistory ? (
        <div className={css.historyPager} data-testid="history-pager">
          <button type="button" disabled={!page || historyLoading || page.start === 0}
            onClick={() => { if (page) requestHistory(page.start); }}>较早</button>
          <span>{historyLoading ? "读取历史…" : historyError
            ? "读取失败：" + historyError
            : page ? String(page.start + (page.items.length ? 1 : 0)) + "–" + page.end + " / " + page.total + "（上限 " + MAX_HISTORY_ITEMS.toLocaleString() + "）"
              : "历史按需读取"}</span>
          <button type="button" disabled={!page || historyLoading || page.end >= page.total}
            onClick={() => { if (page) requestHistory(Math.min(page.total, page.end + HISTORY_PAGE_SIZE)); }}>较新</button>
          <button type="button" disabled={historyLoading || !page || page.end === page.total}
            onClick={() => requestHistory()}>最新</button>
          {historyError ? <button type="button" disabled={historyLoading} onClick={() => requestHistory()}>重试</button> : null}
        </div>
      ) : null}
      <div className={css.body} data-conversation-body="">
        <div className={css.scroll} data-conversation-scroll="">
          <section className={css.transcript} data-testid="conversation-transcript">
            {items.length === 0 ? (
              <div className={css.empty} data-testid="activity-empty">
                <strong>{title}</strong>
                <span>等待 Agent 活动…</span>
                <small>这里展示执行说明、工具调用、文件变更与 UI 指令。</small>
              </div>
            ) : (
              <>
                {items.map((item) => <div className={css.item} data-conversation-item="" key={item.id}>{renderItem(item, store.taskId)}</div>)}
                <div className={css.scrollTail} data-scroll-tail="" aria-hidden="true" />
              </>
            )}
          </section>
        </div>
      </div>
      <div className={css.composerWrap} data-testid="ui-composer">
        {attachments.length > 0 ? (
          <div className={css.attachmentList} data-testid="attachment-list">
            {attachments.map(({ key, file }) => (
              <div className={css.attachmentChip} key={key}>
                <span className={css.attachmentKind}>{file.type.startsWith("image/") ? "图片" : "文件"}</span>
                <span className={css.attachmentName} title={file.name}>{file.name || "剪贴板图片"}</span>
                <span className={css.attachmentSize}>{formatAttachmentBytes(file.size)}</span>
                <button type="button" onClick={() => removeAttachment(key)} aria-label={`移除 ${file.name}`}>×</button>
              </div>
            ))}
          </div>
        ) : null}
        <fieldset
          className={css.composer}
          aria-label="消息与附件输入"
          data-testid="composer"
          data-disabled={!canSend || undefined}
          onDragOver={(event) => event.preventDefault()}
          onDrop={onComposerDrop}
        >
          <input
            ref={fileInputRef}
            className={css.fileInput}
            type="file"
            multiple
            onChange={(event) => addFiles(Array.from(event.target.files ?? []))}
            tabIndex={-1}
          />
          <button
            className={css.attachButton}
            data-testid="attachment-button"
            type="button"
            title="添加图片或文件"
            onClick={() => fileInputRef.current?.click()}
            disabled={!canSend || sending}
          >＋</button>
          <textarea
            ref={composerInputRef}
            value={draft}
            onChange={(event) => setDraft(event.target.value)}
            onKeyDown={onComposerKeyDown}
            onPaste={onComposerPaste}
            disabled={!canSend}
            rows={1}
            maxLength={8000}
            placeholder={canSend ? "输入消息，或 Ctrl+V 粘贴截图 / 拖入文件…" : "任务已结束，输入已关闭"}
            aria-label="向当前执行租约发送修正消息"
            data-testid="ui-composer-input"
          />
          <button
            type="button"
            role="switch"
            aria-checked={ultraOn}
            data-testid="ultra-mode-toggle"
            data-active={ultraOn || undefined}
            className={css.ultraComposer}
            disabled={!canSend || ultraSending}
            onClick={() => void toggleUltra()}
            title={ultraError || (ultraOn ? "已允许网页 GPT 自行开页、点击并发送独立子任务" : "允许网页 GPT 自行建立并行子对话")}
          >
            <span className={css.ultraComposerLabel}>Ultra</span>
            <span className={css.ultraComposerTrack} aria-hidden="true">
              <span className={css.ultraComposerGlow} />
              <span className={css.ultraComposerThumb} />
            </span>
          </button>
          <button ref={sendButtonRef} data-testid="send-button" type="button" onClick={() => void submit()} disabled={!canSend || sending || (!draft.trim() && attachments.length === 0)}>
            {sending ? "发送中" : "发送"}
          </button>
        </fieldset>
        <div className={css.composerHint}>
          <span>Ctrl+V 截图 · 拖拽/＋ 添加附件 · Enter 发送 · 驻场：/stay 3h · 退出：/stay off</span>
          {sendError || ultraError || watchdogError ? <span className={css.composerError}>{sendError || ultraError || watchdogError}</span> : null}
        </div>
      </div>
    </main>
  );
}
