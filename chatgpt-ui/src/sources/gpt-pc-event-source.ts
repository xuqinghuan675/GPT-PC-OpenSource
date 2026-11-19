import { parseGoalMode, parseUltraMode, parseVisualContext, parseVisualObservation, type GoalMode, type UltraMode, type VisualContext } from "../visual-model.ts";
import { invoke } from "@tauri-apps/api/core";
import { SessionStore, type ViewItem } from "../session-store.ts";
import type {
  AgentEvent,
  AgentEventSource,
  CommandExecutionItem,
  FileChangeItem,
  ItemStatus,
  McpToolCallItem,
  ProgressPhase,
  UserAttachment,
} from "../event-model.ts";

interface RawActivityEvent {
  id?: string;
  timestamp?: string;
  timestampMs?: number;
  type?: string;
  taskId?: string;
  threadId?: string;
  controllerId?: string;
  workspace?: string;
  ownerLabel?: string;
  callId?: string;
  tool?: string;
  arguments?: Record<string, unknown>;
  result?: Record<string, unknown>;
  resultStatus?: unknown;
  durationMs?: number;
  filesChanged?: number;
  toolCalls?: number;
  additions?: number;
  deletions?: number;
  phase?: string;
  text?: string;
  title?: string;
  status?: string;
  path?: string;
  sourcePath?: string;
  operation?: string;
  diff?: string;
  beforeHash?: string | null;
  afterHash?: string | null;
  beforeSize?: number | null;
  afterSize?: number | null;
  bytesWritten?: number | null;
  detectedBy?: string;
  error?: string;
  attachments?: unknown;
  observation?: unknown;
  visual?: unknown;
}

interface RuntimeLease {
  id: string;
  threadId: string;
  controllerId?: string;
  workspace: string;
  ownerLabel: string;
  createdAt?: string;
  stayUntilAt?: string;
  visual?: VisualContext;
  goal?: GoalMode;
  ultra?: UltraMode;
  heartbeatAt?: string;
  controlAckRequired: boolean;
  controlResumeRequired: boolean;
}

interface RuntimeSnapshot {
  sampledAt: number;
  leases: RuntimeLease[];
}

interface TaskStats {
  startedAt: number;
  lastAt: number;
  files: Set<string>;
  indexedFileCount?: number;
  additions: number;
  deletions: number;
  toolCalls: number;
}

interface TaskRuntimeState {
  id: string;
  threadId: string;
  workspace: string;
  ownerLabel: string;
  stats: TaskStats;
  activeTools: Map<string, { tool: string; arguments: Record<string, unknown> }>;
}

interface AsyncJobRuntimeState {
  taskId: string;
  callId: string;
  tool: string;
  arguments: Record<string, unknown>;
  command: string;
  cwd: string;
  startedAtMs: number;
}

const READ_BATCH_BYTES = 192 * 1024;
const IDLE_POLL_MS = 300;
const BUSY_POLL_MS = 80;
const RUNTIME_SNAPSHOT_MS = 250;
const RETIRED_TASK_LIMIT = 512;
const SERVER_NAME = "GPT-PC";

function sleep(ms: number, signal?: AbortSignal): Promise<void> {
  if (signal?.aborted) return Promise.resolve();
  return new Promise((resolve) => {
    const timer = window.setTimeout(done, ms);
    const onAbort = () => done();
    function done() {
      window.clearTimeout(timer);
      signal?.removeEventListener("abort", onAbort);
      resolve();
    }
    signal?.addEventListener("abort", onAbort, { once: true });
  });
}

function record(value: unknown): Record<string, unknown> {
  return value !== null && typeof value === "object" && !Array.isArray(value) ? value as Record<string, unknown> : {};
}

function stringValue(value: unknown): string | undefined {
  return typeof value === "string" ? value : undefined;
}

function internalSystemIdentity(controllerId?: string, threadId?: string): boolean {
  return Boolean(
    (controllerId && controllerId.startsWith("system:"))
    || (threadId && threadId.startsWith("system-")),
  );
}

function numberValue(value: unknown): number | undefined {
  return typeof value === "number" && Number.isFinite(value) ? value : undefined;
}

function userAttachments(value: unknown): UserAttachment[] {
  if (!Array.isArray(value)) return [];
  return value.slice(0, 8).flatMap((entry) => {
    const item = record(entry);
    const id = stringValue(item.id);
    if (!id) return [];
    const size = numberValue(item.size);
    return [{
      id,
      name: stringValue(item.name) ?? "附件",
      mime: stringValue(item.mime) ?? "application/octet-stream",
      ...(size !== undefined ? { size } : {}),
    }];
  });
}

function timestampMs(raw: RawActivityEvent): number {
  if (typeof raw.timestampMs === "number" && Number.isFinite(raw.timestampMs)) return raw.timestampMs;
  return Date.parse(raw.timestamp ?? "") || Date.now();
}

function sortableTimestampMs(raw: RawActivityEvent): number {
  if (typeof raw.timestampMs === "number" && Number.isFinite(raw.timestampMs)) return raw.timestampMs;
  const parsed = Date.parse(raw.timestamp ?? "");
  return Number.isFinite(parsed) ? parsed : Number.MAX_SAFE_INTEGER;
}

export function mergeRowsChronologically(
  activityRows: RawActivityEvent[],
  inputRows: RawActivityEvent[],
): RawActivityEvent[] {
  return [
    ...activityRows.map((row, index) => ({ row, timestamp: sortableTimestampMs(row), source: 0, index })),
    ...inputRows.map((row, index) => ({ row, timestamp: sortableTimestampMs(row), source: 1, index })),
  ]
    .sort((left, right) => left.timestamp - right.timestamp || left.source - right.source || left.index - right.index)
    .map(({ row }) => row);
}

function diffCounts(diff: string): { additions: number; deletions: number } {
  let additions = 0;
  let deletions = 0;
  for (const line of diff.split("\n")) {
    if (line.startsWith("+++") || line.startsWith("---")) continue;
    if (line.startsWith("+")) additions += 1;
    if (line.startsWith("-")) deletions += 1;
  }
  return { additions, deletions };
}

function commandTool(tool: string): boolean {
  return ["run_powershell", "run_cmd", "run_python", "run_node", "run_npm", "run_npx", "start_process", "start_job"].includes(tool);
}

function inlineSnippet(value: string, limit = 180): string {
  const compact = value.replace(/\s+/g, " ").trim();
  if (!compact) return "";
  return compact.length <= limit ? compact : `${compact.slice(0, limit - 1)}…`;
}

function stringArray(value: unknown): string[] {
  return Array.isArray(value) ? value.filter((item): item is string => typeof item === "string") : [];
}

function commandLabel(tool: string, args: Record<string, unknown>, result?: Record<string, unknown>): string {
  const command = stringValue(args.command);
  if (command) return inlineSnippet(command);
  const code = stringValue(args.code);
  if (code) {
    const label = tool === "run_python" ? "Python" : tool === "run_node" ? "Node" : tool;
    return `${label} · ${inlineSnippet(code)}`;
  }
  const argv = stringArray(args.args);
  if (argv.length > 0) {
    const executable = tool === "run_npm" ? "npm" : tool === "run_npx" ? "npx" : tool;
    return inlineSnippet(`${executable} ${argv.join(" ")}`);
  }
  const completedCommand = result?.command;
  if (typeof completedCommand === "string" && completedCommand.trim()) return inlineSnippet(completedCommand);
  if (Array.isArray(completedCommand)) {
    const parts = completedCommand.filter((item): item is string => typeof item === "string" && item.length > 0);
    if (parts.length > 0) {
      const tail = tool === "run_npm" && parts.length > 1 ? ["npm", ...parts.slice(1)]
        : tool === "run_npx" && parts.length > 1 ? ["npx", ...parts.slice(1)]
        : parts;
      return inlineSnippet(tail.join(" "));
    }
  }
  return tool;
}

function commandOutput(result: Record<string, unknown>): string {
  return [stringValue(result.stdout), stringValue(result.stderr)].filter(Boolean).join("\n");
}

function asyncJobOutput(result: Record<string, unknown>): string {
  const summary: Record<string, unknown> = {};
  for (const key of ["status", "job_id", "pid", "cwd", "workspace"]) {
    if (result[key] !== undefined && result[key] !== null) summary[key] = result[key];
  }
  return Object.keys(summary).length > 0 ? JSON.stringify(summary, null, 2) : "";
}

function asyncTerminalStatus(status?: string): ItemStatus | undefined {
  if (!status || status === "running" || status === "stop_requested") return undefined;
  if (status === "stopped") return "stopped";
  if (status === "exited" || status === "already_exited") return "completed";
  return "failed";
}

function readOnlyTool(tool: string): boolean {
  const lower = tool.toLowerCase();
  return lower.startsWith("read_") || lower.startsWith("search_") || lower.startsWith("list_") || lower === "file_info" || lower === "git_status" || lower === "git_diff" || lower === "git_log" || lower === "git_show" || lower === "git_rev_parse";
}

function fileKind(operation?: string): "add" | "delete" | "update" {
  if (operation === "create" || operation === "copy") return "add";
  if (operation === "delete") return "delete";
  return "update";
}

function progressPhase(value?: string): ProgressPhase {
  switch (value) {
    case "plan":
    case "decision":
    case "progress":
    case "verification":
    case "warning":
    case "done":
      return value;
    default:
      return "progress";
  }
}

function statusForPhase(phase?: string) {
  return phase === "verification" ? "Verifying" as const : "Thinking" as const;
}

function parseLines(text: string): RawActivityEvent[] {
  const rows: RawActivityEvent[] = [];
  for (const line of text.split("\n")) {
    if (!line.trim()) continue;
    try {
      const value = JSON.parse(line) as unknown;
      if (value && typeof value === "object") rows.push(value as RawActivityEvent);
    } catch {
      // A corrupt historical line must not block the remaining activity stream.
    }
  }
  return rows;
}

export function parseRuntimeSnapshot(response: [number, string[]]): RuntimeSnapshot {
  const [sampledAt, rows] = response;
  const leases: RuntimeLease[] = [];
  for (const row of rows) {
    try {
      const value = record(JSON.parse(row) as unknown);
      const id = stringValue(value.lease_id);
      const workspace = stringValue(value.workspace);
      const controllerId = stringValue(value.controller_id);
      const rawThreadId = stringValue(value.thread_id);
      const expiresAt = Date.parse(stringValue(value.expires_at) ?? "");
      if (!id || !workspace || !Number.isFinite(expiresAt) || expiresAt <= sampledAt) continue;
      const threadId = rawThreadId ?? id;
      if (internalSystemIdentity(controllerId, threadId)) continue;
      leases.push({
        id,
        threadId,
        controllerId,
        workspace,
        ownerLabel: stringValue(value.owner_label) ?? "GPT-PC 会话",
        createdAt: stringValue(value.created_at),
        stayUntilAt: stringValue(value.stay_until_at),
        visual: parseVisualContext(value.visual_mode),
        goal: parseGoalMode(value.goal_mode),
        ultra: parseUltraMode(value.ultra_mode),
        heartbeatAt:
          stringValue(value.execution_checkpoint_updated_at)
          ?? stringValue(value.execution_last_success_at)
          ?? stringValue(value.execution_last_progress_at)
          ?? stringValue(value.created_at),
        controlAckRequired: value.control_ack_required === true || Boolean(stringValue(value.control_ack_pending_id)),
        controlResumeRequired: value.control_resume_required === true || Boolean(stringValue(value.control_resume_pending_id)),
      });
    } catch {
      // Mirror Runtime's tolerance of malformed stale metadata.
    }
  }
  leases.sort((a, b) => a.id.localeCompare(b.id));
  return { sampledAt, leases };
}

export class GPTPcEventSource implements AgentEventSource {
  readonly name = "GPT-PC Activity";
  private offset: number | undefined;
  private inputOffset: number | undefined;
  private historyLoaded = false;
  private bootstrapEmitted = false;
  private bootstrapPendingRows: RawActivityEvent[] = [];
  private runtimeSnapshotErrorLogged = false;
  private streamReadErrorLogged = false;
  private lastRuntimeSnapshotAt = 0;
  private lastLeaseSignature: string | undefined;
  private readonly tasks = new Map<string, TaskRuntimeState>();
  private readonly activeTaskByWorkspace = new Map<string, string | null>();
  private readonly retiredTasks = new Set<string>();
  private readonly callTask = new Map<string, string>();
  private readonly asyncJobs = new Map<string, AsyncJobRuntimeState>();

  private hasAsyncJob(taskId: string): boolean {
    for (const job of this.asyncJobs.values()) {
      if (job.taskId === taskId) return true;
    }
    return false;
  }

  private bootstrapRowsHaveTaskIdentity(rows: RawActivityEvent[]): boolean {
    if (rows.length === 0) return false;
    const knownTaskIds = new Set(this.tasks.keys());
    for (const row of rows) {
      if (row.type === "task.started" && row.taskId) knownTaskIds.add(row.taskId);
    }
    if (knownTaskIds.size === 0) return false;
    return rows.every((row) => !row.taskId || knownTaskIds.has(row.taskId));
  }

  private resolveTaskId(raw: RawActivityEvent): string | undefined {
    if (raw.taskId) return raw.taskId;
    if (raw.callId) {
      const taskId = this.callTask.get(raw.callId);
      if (taskId) return taskId;
    }
    if (raw.workspace) return this.activeTaskByWorkspace.get(raw.workspace) ?? undefined;
    return undefined;
  }

  private task(raw: RawActivityEvent): TaskRuntimeState | undefined {
    const taskId = this.resolveTaskId(raw);
    return taskId ? this.tasks.get(taskId) : undefined;
  }

  private touch(task: TaskRuntimeState, raw: RawActivityEvent): void {
    task.stats.lastAt = Math.max(task.stats.lastAt, timestampMs(raw));
  }

  private rememberTool(task: TaskRuntimeState, callId: string, tool: string, args: Record<string, unknown>): void {
    task.activeTools.set(callId, { tool, arguments: args });
    this.callTask.set(callId, task.id);
    if (task.activeTools.size > 256) {
      const oldest = task.activeTools.keys().next().value;
      if (typeof oldest === "string") {
        task.activeTools.delete(oldest);
        this.callTask.delete(oldest);
      }
    }
  }

  private rememberWorkspace(workspace: string, taskId: string): void {
    const previous = this.activeTaskByWorkspace.get(workspace);
    // Shared resource ownership is not transcript identity. Ambiguous legacy
    // events must carry taskId/callId rather than picking the last participant.
    this.activeTaskByWorkspace.set(workspace, previous === undefined || previous === taskId ? taskId : null);
  }

  private retireTask(task: TaskRuntimeState): void {
    if (this.retiredTasks.has(task.id)) return;
    for (const callId of task.activeTools.keys()) this.callTask.delete(callId);
    task.activeTools.clear();
    for (const [jobId, job] of this.asyncJobs) {
      if (job.taskId === task.id) this.asyncJobs.delete(jobId);
    }
    this.retiredTasks.add(task.id);
    while (this.retiredTasks.size > RETIRED_TASK_LIMIT) {
      const oldest = this.retiredTasks.values().next().value;
      if (oldest === undefined) break;
      this.retiredTasks.delete(oldest);
      this.tasks.delete(oldest);
    }
  }

  private summaryEvent(task: TaskRuntimeState): AgentEvent {
    return {
      method: "ui/taskSummary",
      params: {
        taskId: task.id,
        filesChanged: Math.max(task.stats.files.size, task.stats.indexedFileCount ?? 0),
        additions: task.stats.additions,
        deletions: task.stats.deletions,
        toolCalls: task.stats.toolCalls,
        durationMs: Math.max(0, task.stats.lastAt - task.stats.startedAt),
      },
    };
  }

  private *reconcileRuntime(snapshot: RuntimeSnapshot): Iterable<AgentEvent> {
    const activeIds = snapshot.leases.map((lease) => lease.id);
    const activeSet = new Set(activeIds);
    this.activeTaskByWorkspace.clear();
    for (const task of this.tasks.values()) {
      if (!activeSet.has(task.id)) this.retireTask(task);
      else this.retiredTasks.delete(task.id);
    }

    for (const lease of snapshot.leases) {
      let task = this.tasks.get(lease.id);
      if (!task) {
        const startedAt = Date.parse(lease.createdAt ?? "") || snapshot.sampledAt;
        task = {
          id: lease.id,
          threadId: lease.threadId,
          workspace: lease.workspace,
          ownerLabel: lease.ownerLabel,
          stats: { startedAt, lastAt: startedAt, files: new Set(), additions: 0, deletions: 0, toolCalls: 0 },
          activeTools: new Map(),
        };
        this.tasks.set(task.id, task);
        yield {
          method: "task/started",
          params: { task: { id: task.id, threadId: task.threadId, title: task.ownerLabel, workspace: task.workspace, startedAt: lease.createdAt, status: "inProgress" } },
        };
      }
      this.rememberWorkspace(lease.workspace, lease.id);
    }

    const stayUntilByTask = Object.fromEntries(
      snapshot.leases.flatMap((lease) => lease.stayUntilAt ? [[lease.id, lease.stayUntilAt]] : []),
    );
    const visualByTask = Object.fromEntries(snapshot.leases.flatMap((lease) => {
      if (!lease.visual && !lease.goal) return [];
      const visual: VisualContext = lease.visual ?? { active: false, status: "off", references: [], criteria: [], depth: "visual", journeys: [], workspace_revision: 0, iteration: 0 };
      return [[lease.id, lease.goal ? { ...visual, goal: lease.goal } : visual]];
    }));
    const ultraByTask = Object.fromEntries(
      snapshot.leases.flatMap((lease) => lease.ultra ? [[lease.id, lease.ultra]] : []),
    );
    const heartbeatAtByTask = Object.fromEntries(
      snapshot.leases.flatMap((lease) => lease.heartbeatAt ? [[lease.id, lease.heartbeatAt]] : []),
    );
    const controlAckByTask = Object.fromEntries(snapshot.leases.map((lease) => [lease.id, lease.controlAckRequired]));
    const controlResumeByTask = Object.fromEntries(snapshot.leases.map((lease) => [lease.id, lease.controlResumeRequired]));
    const signature = snapshot.leases
      .map((lease) => `${lease.id}:${lease.stayUntilAt ?? ""}:${lease.visual?.updated_at ?? ""}:${lease.visual?.until_at ?? ""}:${lease.visual?.status ?? ""}:${lease.goal?.updated_at ?? ""}:${lease.goal?.status ?? ""}:${lease.goal?.revision ?? 0}:${lease.ultra?.updated_at ?? ""}:${lease.ultra?.status ?? ""}:${lease.heartbeatAt ?? ""}:${lease.controlAckRequired ? 1 : 0}:${lease.controlResumeRequired ? 1 : 0}`)
      .join("|");
    if (!this.bootstrapEmitted || signature !== this.lastLeaseSignature) {
      this.lastLeaseSignature = signature;
      yield {
        method: "ui/leaseSnapshot",
        params: { activeTaskIds: activeIds, sampledAt: snapshot.sampledAt, stayUntilByTask, visualByTask, ultraByTask, heartbeatAtByTask, controlAckByTask, controlResumeByTask },
      };
    }
  }

  private *map(raw: RawActivityEvent): Iterable<AgentEvent> {
    const type = raw.type ?? "";
    if (type === "task.indexed" && raw.taskId && raw.timestamp) {
      yield { method: "ui/taskIndexed", params: { taskId: raw.taskId, timestamp: raw.timestamp } };
      return;
    }
    if (type === "task.summary_indexed" && raw.taskId) {
      const state = this.tasks.get(raw.taskId);
      if (state) {
        const stats = state.stats;
        stats.indexedFileCount = Math.max(stats.indexedFileCount ?? 0, raw.filesChanged ?? 0);
        stats.toolCalls = Math.max(stats.toolCalls, raw.toolCalls ?? 0);
        stats.additions = Math.max(stats.additions, raw.additions ?? 0);
        stats.deletions = Math.max(stats.deletions, raw.deletions ?? 0);
        stats.lastAt = Math.max(stats.lastAt, timestampMs(raw));
        yield this.summaryEvent(state);
      }
      return;
    }
    const visualScoped = type === "agent.observation" || type === "visual.context";
    if (visualScoped && !stringValue(raw.taskId)) return;
    const rawId = raw.id ?? `${type}-${Date.now()}`;

    if (type === "task.started") {
      if (internalSystemIdentity(raw.controllerId, raw.threadId)) return;
      const taskId = raw.taskId ?? rawId;
      // Runtime snapshot may have already seeded this live lease and its Visual/Goal state.
      if (this.tasks.has(taskId)) return;
      const startedAt = timestampMs(raw);
      const task: TaskRuntimeState = {
        id: taskId,
        threadId: raw.threadId ?? taskId,
        workspace: raw.workspace ?? "",
        ownerLabel: raw.title ?? raw.ownerLabel ?? "GPT-PC 会话",
        stats: { startedAt, lastAt: startedAt, files: new Set(), additions: 0, deletions: 0, toolCalls: 0 },
        activeTools: new Map(),
      };
      this.tasks.set(taskId, task);
      this.retiredTasks.delete(taskId);
      if (task.workspace) this.rememberWorkspace(task.workspace, taskId);
      yield {
        method: "task/started",
        params: { task: { id: task.id, threadId: task.threadId, title: task.ownerLabel, workspace: task.workspace, startedAt: raw.timestamp, status: "inProgress" } },
      };
      return;
    }

    if (type === "ui.message") {
      const taskId = this.resolveTaskId(raw) ?? raw.taskId;
      const threadId = raw.threadId ?? (taskId ? this.tasks.get(taskId)?.threadId : undefined) ?? taskId;
      const text = raw.text ?? "";
      const attachments = userAttachments(raw.attachments);
      if (!taskId || !threadId || (!text && attachments.length === 0)) return;
      yield {
        method: "ui/threadMessage",
        params: {
          taskId,
          threadId,
          timestamp: raw.timestamp ?? new Date(timestampMs(raw)).toISOString(),
          item: {
            type: "userMessage",
            id: rawId,
            content: text ? [{ type: "text", text }] : [],
            attachments,
          },
        },
      };
      return;
    }

    const task = this.task(raw);
    if (!task) return;
    if (visualScoped && stringValue(raw.threadId) && raw.threadId !== task.threadId) return;
    this.touch(task, raw);
    const scope = { taskId: task.id, threadId: task.threadId, timestamp: raw.timestamp };

    if (type === "agent.observation") {
      const observation = parseVisualObservation(raw.observation);
      if (observation) yield { method: "item/completed", params: { ...scope,
        item: { type: "agentObservation", id: observation.id, observation } } };
      return;
    }
    if (type === "visual.context") {
      const visual = parseVisualContext(raw.visual);
      if (visual) yield { method: "ui/visualContext", params: { ...scope, visual } };
      return;
    }
    // Liveness probes are Runtime control-plane traffic, not transcript
    // content.  Rendering every 3-minute probe as a warning card made a
    // healthy long-running GPT look repeatedly disconnected to the user.
    // The probe is still delivered to the model at MCP safe boundaries and
    // remains fully observable in Runtime logs/state; keep it out of the UI.
    if (type === "runtime.liveness_probe" || type === "runtime.liveness_reply") return;

    if (type === "task.completed") {
      yield this.summaryEvent(task);
      yield {
        method: "task/completed",
        params: { task: { id: task.id, threadId: task.threadId, completedAt: raw.timestamp, status: raw.status === "failed" ? "failed" : "completed" } },
      };
      if (task.workspace && this.activeTaskByWorkspace.get(task.workspace) === task.id) this.activeTaskByWorkspace.delete(task.workspace);
      this.retireTask(task);
      return;
    }

    if (type === "agent.progress") {
      const text = (raw.text ?? "").trim();
      if (text === "Sleeping...") {
        yield { method: "ui/status", params: { taskId: task.id, status: "Sleeping" } };
        return;
      }
      const id = `progress-${rawId}`;
      yield { method: "ui/status", params: { taskId: task.id, status: statusForPhase(raw.phase) } };
      yield {
        method: "item/completed",
        params: { ...scope, item: { type: "reasoning", id, summary: [raw.text ?? ""], phase: progressPhase(raw.phase) } },
      };
      return;
    }

    if (type === "tool.started") {
      const tool = raw.tool ?? "tool";
      const args = record(raw.arguments);
      const callId = raw.callId ?? rawId;
      this.rememberTool(task, callId, tool, args);
      yield { method: "ui/status", params: { taskId: task.id, status: "Running tool" } };
      if (commandTool(tool)) {
        const item: CommandExecutionItem = {
          type: "commandExecution",
          id: callId,
          tool,
          arguments: args,
          command: commandLabel(tool, args),
          cwd: stringValue(args.cwd) ?? "",
          status: "inProgress",
          aggregatedOutput: "",
        };
        yield { method: "item/started", params: { ...scope, item } };
      } else {
        const item: McpToolCallItem = {
          type: "mcpToolCall",
          id: callId,
          server: SERVER_NAME,
          tool,
          status: "inProgress",
          arguments: args,
          readOnlyHint: readOnlyTool(tool),
        };
        yield { method: "item/started", params: { ...scope, item } };
      }
      return;
    }

    if (type === "tool.completed") {
      const callId = raw.callId ?? rawId;
      const active = task.activeTools.get(callId);
      const tool = raw.tool ?? active?.tool ?? "tool";
      const args = active?.arguments ?? record(raw.arguments);
      task.activeTools.delete(callId);
      this.callTask.delete(callId);
      const toolResult = record(raw.result);
      const exitCode = numberValue(toolResult.exit_code);
      const failed = exitCode !== undefined && exitCode !== 0;
      const jobId = stringValue(toolResult.job_id);
      const jobStatus = stringValue(toolResult.status);
      const launchedAsync = Boolean(
        jobId && jobStatus === "running" && (tool === "start_job" || tool === "start_process"),
      );
      task.stats.toolCalls += 1;
      if (commandTool(tool)) {
        const item: CommandExecutionItem = {
          type: "commandExecution",
          id: callId,
          tool,
          arguments: args,
          command: commandLabel(tool, args, toolResult),
          cwd: stringValue(toolResult.cwd) ?? stringValue(args.cwd) ?? "",
          status: launchedAsync ? "inProgress" : failed ? "failed" : "completed",
          aggregatedOutput: launchedAsync ? asyncJobOutput(toolResult) : commandOutput(toolResult),
          exitCode: exitCode ?? null,
          durationMs: raw.durationMs ?? null,
        };
        yield { method: "item/completed", params: { ...scope, item } };
        if (launchedAsync && jobId) {
          this.asyncJobs.set(jobId, {
            taskId: task.id,
            callId,
            tool,
            arguments: args,
            command: item.command,
            cwd: item.cwd,
            startedAtMs: timestampMs(raw) - Math.max(0, raw.durationMs ?? 0),
          });
          while (this.asyncJobs.size > 256) {
            const oldest = this.asyncJobs.keys().next().value;
            if (typeof oldest !== "string") break;
            this.asyncJobs.delete(oldest);
          }
        }
      } else {
        const item: McpToolCallItem = {
          type: "mcpToolCall",
          id: callId,
          server: SERVER_NAME,
          tool,
          status: failed ? "failed" : "completed",
          arguments: args,
          result: toolResult,
          readOnlyHint: readOnlyTool(tool),
        };
        yield { method: "item/completed", params: { ...scope, item } };
        if (raw.durationMs != null) yield { method: "ui/itemMeta", params: { taskId: task.id, itemId: item.id, durationMs: raw.durationMs } };
      }
      if (jobId) {
        const trackedJob = this.asyncJobs.get(jobId);
        const terminalStatus = asyncTerminalStatus(jobStatus);
        if (trackedJob && trackedJob.taskId === task.id && terminalStatus) {
          const item: CommandExecutionItem = {
            type: "commandExecution",
            id: trackedJob.callId,
            tool: trackedJob.tool,
            arguments: trackedJob.arguments,
            command: trackedJob.command,
            cwd: trackedJob.cwd,
            status: terminalStatus,
            aggregatedOutput: asyncJobOutput(toolResult),
            exitCode: null,
            durationMs: Math.max(0, timestampMs(raw) - trackedJob.startedAtMs),
          };
          yield { method: "item/completed", params: { ...scope, item } };
          this.asyncJobs.delete(jobId);
        }
      }
      yield this.summaryEvent(task);
      yield { method: "ui/status", params: { taskId: task.id, status: task.activeTools.size || this.hasAsyncJob(task.id) ? "Running tool" : "Thinking" } };
      return;
    }

    if (type === "tool.failed") {
      const callId = raw.callId ?? rawId;
      const active = task.activeTools.get(callId);
      const tool = raw.tool ?? active?.tool ?? "tool";
      const args = active?.arguments ?? record(raw.arguments);
      task.activeTools.delete(callId);
      this.callTask.delete(callId);
      task.stats.toolCalls += 1;
      if (commandTool(tool)) {
        const item: CommandExecutionItem = {
          type: "commandExecution",
          id: callId,
          tool,
          arguments: args,
          command: commandLabel(tool, args),
          cwd: stringValue(args.cwd) ?? "",
          status: "failed",
          aggregatedOutput: raw.error ?? "Tool failed",
          exitCode: null,
          durationMs: raw.durationMs ?? null,
        };
        yield { method: "item/completed", params: { ...scope, item } };
      } else {
        const item: McpToolCallItem = {
          type: "mcpToolCall",
          id: callId,
          server: SERVER_NAME,
          tool,
          status: "failed",
          arguments: args,
          error: raw.error ?? "Tool failed",
          readOnlyHint: readOnlyTool(tool),
        };
        yield { method: "item/completed", params: { ...scope, item } };
        if (raw.durationMs != null) yield { method: "ui/itemMeta", params: { taskId: task.id, itemId: item.id, durationMs: raw.durationMs } };
      }
      yield this.summaryEvent(task);
      yield { method: "ui/status", params: { taskId: task.id, status: task.activeTools.size || this.hasAsyncJob(task.id) ? "Running tool" : "Thinking" } };
      return;
    }

    if (type === "file.changed") {
      const path = raw.path ?? raw.sourcePath ?? "unknown";
      const diff = raw.diff ?? "";
      if (task.stats.files.size < 1000) task.stats.files.add(path);
      const counts = diffCounts(diff);
      task.stats.additions += counts.additions;
      task.stats.deletions += counts.deletions;
      const item: FileChangeItem = {
        type: "fileChange",
        id: `file-${rawId}`,
        tool: raw.tool,
        status: "completed",
        changes: [{
          path,
          kind: fileKind(raw.operation),
          diff,
          beforeHash: raw.beforeHash,
          afterHash: raw.afterHash,
          beforeSize: raw.beforeSize,
          afterSize: raw.afterSize,
          bytesWritten: raw.bytesWritten,
          detectedBy: raw.detectedBy,
        }],
      };
      yield { method: "item/completed", params: { ...scope, item } };
      yield this.summaryEvent(task);
    }
  }

  private async runtimeSnapshot(): Promise<RuntimeSnapshot | null> {
    try {
      const response = await invoke<[number, string[]]>("activity_runtime_snapshot");
      this.runtimeSnapshotErrorLogged = false;
      return parseRuntimeSnapshot(response);
    } catch (error) {
      if (!this.runtimeSnapshotErrorLogged) {
        console.error("[chatgpt-ui] runtime lease snapshot failed", error);
        this.runtimeSnapshotErrorLogged = true;
      }
      return null;
    }
  }

  private async readActivity(): Promise<RawActivityEvent[]> {
    if (!this.historyLoaded) {
      const [offset, text] = await invoke<[number, string]>("activity_bootstrap_compact");
      this.offset = offset;
      this.historyLoaded = true;
      return parseLines(text);
    }
    const [nextOffset, text] = await invoke<[number, string, boolean]>("activity_read", { offset: this.offset, maxBytes: READ_BATCH_BYTES });
    this.offset = nextOffset;
    return parseLines(text);
  }

  // Transient projection: reading old pages never changes live tool/job state.
  projectTaskHistory(taskId: string, threadId: string, workspace: string, title: string, rows: string): ViewItem[] {
    const projector = new GPTPcEventSource();
    const localStore = new SessionStore();
    for (const event of projector.map({ type: "task.started", taskId, threadId, workspace, title })) {
      localStore.apply(event);
    }
    for (const row of parseLines(rows)) {
      for (const event of projector.map(row)) localStore.apply(event);
    }
    return localStore.order.flatMap((id) => {
      const item = localStore.items.get(id);
      return item ? [item] : [];
    });
  }

  private async readInput(): Promise<RawActivityEvent[]> {
    const [nextOffset, text] = await invoke<[number, string, boolean]>("control_read", { offset: this.inputOffset, maxBytes: READ_BATCH_BYTES });
    this.inputOffset = nextOffset;
    return parseLines(text);
  }

  async *stream(signal?: AbortSignal): AsyncIterable<AgentEvent> {
    while (!signal?.aborted) {
      // The bootstrap tail is bounded. On a long-running task its original
      // task.started row may already be outside that window even though newer
      // tool/file rows are still present. Prefer seeding task identity from the
      // Runtime snapshot before mapping the tail. If the snapshot IPC is
      // temporarily unavailable, a self-contained history containing its own
      // task.started rows may still bootstrap; otherwise retain the consumed
      // rows in memory until the snapshot recovers instead of dropping them.
      const bootstrapSnapshot = !this.bootstrapEmitted ? await this.runtimeSnapshot() : null;
      if (bootstrapSnapshot) {
        this.lastRuntimeSnapshotAt = Date.now();
        yield* this.reconcileRuntime(bootstrapSnapshot);
      }

      let activityRows: RawActivityEvent[] = [];
      let inputRows: RawActivityEvent[] = [];
      const [activity, input] = await Promise.allSettled([this.readActivity(), this.readInput()]);
      if (activity.status === "fulfilled") activityRows = activity.value;
      if (input.status === "fulfilled") inputRows = input.value;
      const failed = activity.status === "rejected" || input.status === "rejected";
      if (failed && !this.streamReadErrorLogged) {
        const error = activity.status === "rejected" ? activity.reason : input.status === "rejected" ? input.reason : undefined;
        console.error("[chatgpt-ui] activity/control read failed; retrying", error);
      }
      this.streamReadErrorLogged = failed;

      let mergedRows = mergeRowsChronologically(activityRows, inputRows);
      if (!this.bootstrapEmitted) {
        if (this.bootstrapPendingRows.length > 0) {
          mergedRows = mergeRowsChronologically(this.bootstrapPendingRows, mergedRows);
        }
        if (!bootstrapSnapshot && !this.bootstrapRowsHaveTaskIdentity(mergedRows)) {
          this.bootstrapPendingRows = mergedRows;
          await sleep(IDLE_POLL_MS, signal);
          continue;
        }
        this.bootstrapPendingRows = [];
      }

      for (const row of mergedRows) {
        if (signal?.aborted) return;
        yield* this.map(row);
      }

      const now = Date.now();
      if (!this.bootstrapEmitted) {
        // Do not declare the recovered transcript complete while either
        // append-only source is temporarily unavailable.  Successful rows
        // above are retained/mapped, then the failed source is retried; this
        // keeps bootstrapComplete meaningful without discarding good work.
        if (!failed) {
          this.bootstrapEmitted = true;
          yield { method: "ui/bootstrapComplete", params: {} };
        }
      } else if (now - this.lastRuntimeSnapshotAt >= RUNTIME_SNAPSHOT_MS) {
        this.lastRuntimeSnapshotAt = now;
        const snapshot = await this.runtimeSnapshot();
        if (snapshot) yield* this.reconcileRuntime(snapshot);
      }

      const busy = activityRows.length > 0 || inputRows.length > 0;
      await sleep(busy ? BUSY_POLL_MS : IDLE_POLL_MS, signal);
    }
  }
}
