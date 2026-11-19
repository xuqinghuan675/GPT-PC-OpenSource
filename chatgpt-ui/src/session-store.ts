import type { UltraMode, VisualContext, VisualObservation } from "./visual-model.ts";
import type {
  ActivityStatus,
  AgentEvent,
  AgentStatus,
  CommandExecutionItem,
  FileChangeItem,
  LeaseLifecycle,
  McpToolCallItem,
  ProgressPhase,
  ThreadItem,
  UserAttachment,
} from "./event-model.ts";

export interface TaskSummary {
  filesChanged: number;
  additions: number;
  deletions: number;
  toolCalls: number;
  durationMs: number;
}

interface BaseViewItem { id: string; status?: string; occurredAtMs?: number }
export interface ObservationViewItem extends BaseViewItem { kind: "observation"; observation: VisualObservation }
export interface UserViewItem extends BaseViewItem { kind: "user"; text: string; attachments: UserAttachment[] }
export interface ReasoningViewItem extends BaseViewItem { kind: "reasoning"; text: string; phase?: ProgressPhase }
export interface AgentMessageViewItem extends BaseViewItem { kind: "agent"; text: string }
export interface McpViewItem extends BaseViewItem {
  kind: "mcp";
  activity: "tool" | "read" | "search";
  server: string;
  tool: string;
  argumentsSummary: string;
  resultSummary: string;
  durationMs?: number;
}
export interface CommandViewItem extends BaseViewItem {
  kind: "command";
  tool?: string;
  argumentsSummary: string;
  command: string;
  cwd: string;
  outputSummary: string;
  exitCode?: number | null;
  durationMs?: number | null;
}
export interface FileChangeViewItem extends BaseViewItem {
  kind: "fileChange";
  tool?: string;
  changes: Array<{
    path: string;
    kind: "add" | "delete" | "update";
    diff: string;
    additions: number;
    deletions: number;
    beforeHash?: string | null;
    afterHash?: string | null;
    beforeSize?: number | null;
    afterSize?: number | null;
    bytesWritten?: number | null;
    detectedBy?: string;
  }>;
}
export type ViewItem = ObservationViewItem | UserViewItem | ReasoningViewItem | AgentMessageViewItem | McpViewItem | CommandViewItem | FileChangeViewItem;
export interface ApplyResult { dirtyIds: string[]; orderChanged: boolean; chromeChanged: boolean }

const RESULT_LIMIT = 6000;
const OUTPUT_LIMIT = 8000;
const DIFF_LIMIT = 64_000;
// Resident rows per lease; 10,000 browseable records stay on disk.
export const MAX_RETAINED_ITEMS = 320;

function compactText(text: string, limit: number): string {
  const normalized = text.replace(/\r\n/g, "\n").trim();
  return normalized.length <= limit ? normalized : `${normalized.slice(0, limit)}\n… ${normalized.length - limit} chars omitted`;
}

function appendStatusText(existing: string, message: string, limit: number): string {
  return compactText(existing ? `${existing}\n${message}` : message, limit);
}

function summarizeUnknown(value: unknown): string {
  if (value == null) return "";
  if (typeof value === "string") return compactText(value, RESULT_LIMIT);
  try { return compactText(JSON.stringify(value, null, 2), RESULT_LIMIT); } catch { return String(value); }
}

function diffStats(diff: string): { additions: number; deletions: number } {
  let additions = 0;
  let deletions = 0;
  for (const line of diff.split("\n")) {
    if (line.startsWith("+++") || line.startsWith("---")) continue;
    if (line.startsWith("+")) additions += 1;
    if (line.startsWith("-")) deletions += 1;
  }
  return { additions, deletions };
}

function activityFromTool(tool: string): "tool" | "read" | "search" {
  const lower = tool.toLowerCase();
  if (lower.includes("search") || lower.includes("find")) return "search";
  if (lower.includes("read") || lower.includes("file_info") || lower.includes("list_directory")) return "read";
  return "tool";
}

function occurredAtMs(timestamp?: string): number | undefined {
  if (!timestamp) return undefined;
  const parsed = Date.parse(timestamp);
  return Number.isFinite(parsed) ? parsed : undefined;
}

function mcpToView(item: McpToolCallItem, timestamp?: string): McpViewItem {
  return {
    kind: "mcp", id: item.id, activity: activityFromTool(item.tool), server: item.server, tool: item.tool,
    status: item.status, argumentsSummary: summarizeUnknown(item.arguments),
    resultSummary: item.error ? summarizeUnknown(item.error) : summarizeUnknown(item.result), occurredAtMs: occurredAtMs(timestamp),
  };
}

function commandToView(item: CommandExecutionItem, timestamp?: string): CommandViewItem {
  return {
    kind: "command", id: item.id, tool: item.tool, argumentsSummary: summarizeUnknown(item.arguments),
    command: item.command, cwd: item.cwd, status: item.status,
    outputSummary: compactText(item.aggregatedOutput ?? "", OUTPUT_LIMIT), exitCode: item.exitCode, durationMs: item.durationMs,
    occurredAtMs: occurredAtMs(timestamp),
  };
}

function fileChangeToView(item: FileChangeItem, timestamp?: string): FileChangeViewItem {
  return {
    kind: "fileChange", id: item.id, tool: item.tool, status: item.status, occurredAtMs: occurredAtMs(timestamp),
    changes: item.changes.map((change) => {
      const diff = compactText(change.diff, DIFF_LIMIT);
      return { ...change, diff, ...diffStats(diff) };
    }),
  };
}

function threadItemToView(item: ThreadItem, timestamp?: string): ViewItem {
  const at = occurredAtMs(timestamp);
  switch (item.type) {
    case "agentObservation": return { kind: "observation", id: item.id, observation: item.observation, occurredAtMs: at };
    case "userMessage": return { kind: "user", id: item.id, text: item.content.map((part) => part.text).join("\n"), attachments: item.attachments ?? [], occurredAtMs: at };
    case "reasoning": return { kind: "reasoning", id: item.id, text: item.summary.join("\n\n"), phase: item.phase, occurredAtMs: at };
    case "agentMessage": return { kind: "agent", id: item.id, text: item.text, occurredAtMs: at };
    case "mcpToolCall": return mcpToView(item, timestamp);
    case "commandExecution": return commandToView(item, timestamp);
    case "fileChange": return fileChangeToView(item, timestamp);
  }
}

const EMPTY_SUMMARY: TaskSummary = { filesChanged: 0, additions: 0, deletions: 0, toolCalls: 0, durationMs: 0 };

export class SessionStore {
  readonly items = new Map<string, ViewItem>();
  readonly order: string[] = [];
  activityStatus: ActivityStatus = "Idle";
  lifecycle: LeaseLifecycle | null = null;
  taskId = "";
  threadId = "";
  taskTitle = "";
  workspace = "";
  startedAt = 0;
  stayUntilAt = 0;
  visualContext: VisualContext | undefined;
  ultraMode: UltraMode | undefined;
  residentHeartbeatAt = 0;
  controlAckRequired = false;
  controlResumeRequired = false;
  taskSummary: TaskSummary = { ...EMPTY_SUMMARY };
  processedItemCount = 0;

  // Flush old task rows without affecting identity, lifecycle or counters.
  releaseDetails(): void {
    this.items.clear();
    this.order.length = 0;
  }

  get agentStatus(): AgentStatus {
    switch (this.lifecycle) {
      case "active": return this.activityStatus === "Idle" ? "Thinking" : this.activityStatus;
      case "completed": return "Completed";
      case "failed": return "Error";
      case "ended_missing_completion": return "Ended";
      default: return "Idle";
    }
  }

  get active(): boolean {
    return this.lifecycle === "active";
  }

  reset(): void {
    this.items.clear();
    this.order.length = 0;
    this.activityStatus = "Idle";
    this.lifecycle = null;
    this.taskId = "";
    this.threadId = "";
    this.taskTitle = "";
    this.workspace = "";
    this.startedAt = 0;
    this.stayUntilAt = 0;
    this.visualContext = undefined;
    this.ultraMode = undefined;
    this.residentHeartbeatAt = 0;
    this.controlAckRequired = false;
    this.controlResumeRequired = false;
    this.taskSummary = { ...EMPTY_SUMMARY };
    this.processedItemCount = 0;
  }

  private retain(view: ViewItem, result: ApplyResult): void {
    const existing = this.items.get(view.id);
    const isNew = !existing;
    // Completion/update events may arrive later than item start.  Preserve the
    // first observed timestamp so an existing row never jumps around.
    if (existing?.occurredAtMs != null) view.occurredAtMs = existing.occurredAtMs;
    this.items.set(view.id, view);
    if (!isNew) return;
    this.processedItemCount += 1;
    const at = view.occurredAtMs;
    if (at == null) {
      this.order.push(view.id);
    } else {
      const insertAt = this.order.findIndex((id) => {
        const current = this.items.get(id)?.occurredAtMs;
        return current != null && current > at;
      });
      if (insertAt === -1) this.order.push(view.id);
      else this.order.splice(insertAt, 0, view.id);
    }
    if (this.order.length > MAX_RETAINED_ITEMS) {
      const removed = this.order.shift();
      if (removed) this.items.delete(removed);
    }
    result.orderChanged = true;
  }

  private closeRunningActivities(result: ApplyResult, reason: string): void {
    for (const item of this.items.values()) {
      if (item.status !== "inProgress") continue;
      item.status = "stopped";
      if (item.kind === "mcp") item.resultSummary = appendStatusText(item.resultSummary, reason, RESULT_LIMIT);
      if (item.kind === "command") item.outputSummary = appendStatusText(item.outputSummary, reason, OUTPUT_LIMIT);
      result.dirtyIds.push(item.id);
    }
  }

  private finishLifecycle(result: ApplyResult, lifecycle: Exclude<LeaseLifecycle, "active">, reason: string): void {
    this.lifecycle = lifecycle;
    this.activityStatus = "Idle";
    this.closeRunningActivities(result, reason);
    result.chromeChanged = true;
  }

  apply(event: AgentEvent): ApplyResult {
    const result: ApplyResult = { dirtyIds: [], orderChanged: false, chromeChanged: false };
    if (event.method === "ui/status") {
      if (this.lifecycle === "active") {
        this.activityStatus = event.params.status;
        result.chromeChanged = true;
      }
      return result;
    }
    if (event.method === "ui/visualContext") {
      this.visualContext = event.params.visual;
      result.chromeChanged = true;
      return result;
    }
    if (event.method === "ui/leaseSnapshot") {
      const nextVisual = event.params.visualByTask?.[this.taskId];
      if (nextVisual && JSON.stringify(this.visualContext) !== JSON.stringify(nextVisual)) {
        this.visualContext = nextVisual;
        result.chromeChanged = true;
      }
      const nextUltra = event.params.ultraByTask?.[this.taskId];
      if (JSON.stringify(this.ultraMode) !== JSON.stringify(nextUltra)) {
        this.ultraMode = nextUltra;
        result.chromeChanged = true;
      }
      const stayUntilRaw = this.taskId ? event.params.stayUntilByTask?.[this.taskId] : undefined;
      const parsedStayUntil = stayUntilRaw ? Date.parse(stayUntilRaw) : 0;
      const nextStayUntil = Number.isFinite(parsedStayUntil) && parsedStayUntil > event.params.sampledAt ? parsedStayUntil : 0;
      if (this.stayUntilAt !== nextStayUntil) {
        this.stayUntilAt = nextStayUntil;
        result.chromeChanged = true;
      }
      const heartbeatRaw = this.taskId ? event.params.heartbeatAtByTask?.[this.taskId] : undefined;
      const parsedHeartbeat = heartbeatRaw ? Date.parse(heartbeatRaw) : 0;
      const nextHeartbeat = Number.isFinite(parsedHeartbeat) ? parsedHeartbeat : 0;
      if (this.residentHeartbeatAt !== nextHeartbeat) {
        this.residentHeartbeatAt = nextHeartbeat;
        result.chromeChanged = true;
      }
      const nextAckRequired = this.taskId ? event.params.controlAckByTask?.[this.taskId] === true : false;
      const nextResumeRequired = this.taskId ? event.params.controlResumeByTask?.[this.taskId] === true : false;
      if (this.controlAckRequired !== nextAckRequired || this.controlResumeRequired !== nextResumeRequired) {
        this.controlAckRequired = nextAckRequired;
        this.controlResumeRequired = nextResumeRequired;
        result.chromeChanged = true;
      }
      if (this.taskId && this.lifecycle === "active" && !event.params.activeTaskIds.includes(this.taskId)) {
        this.finishLifecycle(result, "ended_missing_completion", "任务已结束，完成事件缺失");
      }
      return result;
    }
    if (event.method === "ui/taskSummary") {
      const { filesChanged, additions, deletions, toolCalls, durationMs } = event.params;
      this.taskSummary = { filesChanged, additions, deletions, toolCalls, durationMs };
      result.chromeChanged = true;
      return result;
    }
    if (event.method === "ui/itemMeta") {
      const item = this.items.get(event.params.itemId);
      if (item?.kind === "mcp" && event.params.durationMs != null) item.durationMs = event.params.durationMs;
      if (item?.kind === "command" && event.params.durationMs != null) item.durationMs = event.params.durationMs;
      if (item) result.dirtyIds.push(item.id);
      return result;
    }
    if (event.method === "ui/threadMessage") {
      const view = threadItemToView(event.params.item, event.params.timestamp);
      this.retain(view, result);
      result.dirtyIds.push(view.id);
      return result;
    }
    if (event.method === "task/started") {
      this.items.clear();
      this.order.length = 0;
      this.processedItemCount = 0;
      this.taskSummary = { ...EMPTY_SUMMARY };
      this.taskId = event.params.task.id;
      this.threadId = event.params.task.threadId ?? event.params.task.id;
      this.taskTitle = event.params.task.title ?? "GPT-PC task";
      this.workspace = event.params.task.workspace ?? "";
      this.startedAt = event.params.task.startedAt ? Date.parse(event.params.task.startedAt) : Date.now();
      if (!Number.isFinite(this.startedAt)) this.startedAt = Date.now();
      this.stayUntilAt = 0;
      this.visualContext = undefined;
      this.ultraMode = undefined;
      this.residentHeartbeatAt = 0;
      this.controlAckRequired = false;
      this.controlResumeRequired = false;
      this.lifecycle = "active";
      this.activityStatus = "Thinking";
      result.orderChanged = true;
      result.chromeChanged = true;
      return result;
    }
    if (event.method === "task/completed") {
      const lifecycle = event.params.task.status === "completed" ? "completed" : "failed";
      this.finishLifecycle(result, lifecycle, "任务已结束，工具完成事件缺失");
      return result;
    }
    if (event.method === "item/reasoning/summaryTextDelta") {
      const existing = this.items.get(event.params.itemId);
      if (existing?.kind === "reasoning") {
        existing.text = compactText(`${existing.text}${event.params.delta}`, RESULT_LIMIT);
        result.dirtyIds.push(existing.id);
      }
      return result;
    }
    if (event.method === "item/agentMessage/delta") {
      const existing = this.items.get(event.params.itemId);
      if (existing?.kind === "agent") {
        existing.text = compactText(`${existing.text}${event.params.delta}`, RESULT_LIMIT);
        result.dirtyIds.push(existing.id);
      }
      return result;
    }
    if (event.method === "item/commandExecution/outputDelta") {
      const existing = this.items.get(event.params.itemId);
      if (existing?.kind === "command") {
        existing.outputSummary = compactText(`${existing.outputSummary}${event.params.delta}`, OUTPUT_LIMIT);
        result.dirtyIds.push(existing.id);
      }
      return result;
    }
    if (event.method === "item/started" || event.method === "item/completed") {
      const view = threadItemToView(event.params.item, event.params.timestamp);
      this.retain(view, result);
      result.dirtyIds.push(view.id);
      return result;
    }
    return result;
  }
}
