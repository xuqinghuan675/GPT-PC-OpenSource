import type { UltraMode, VisualContext, VisualObservation } from "./visual-model.ts";
// Provider-neutral activity vocabulary for the GPT-PC viewer.
// `reasoning` means only user-visible public work summaries emitted by the agent.
// Hidden chain-of-thought is never part of this protocol.

export type ItemStatus = "inProgress" | "completed" | "failed" | "declined" | "stopped";
export type McpStatus = "inProgress" | "completed" | "failed" | "stopped";
export type TaskStatus = "inProgress" | "completed" | "failed";
export type ActivityStatus = "Idle" | "Thinking" | "Running tool" | "Verifying" | "Sleeping";
export type LeaseLifecycle = "active" | "completed" | "failed" | "ended_missing_completion";
export type AgentStatus = ActivityStatus | "Completed" | "Error" | "Ended";
export type ProgressPhase = "plan" | "decision" | "progress" | "verification" | "warning" | "done";

export interface UserAttachment {
  id: string;
  name: string;
  mime: string;
  size?: number;
}

export interface UserMessageItem {
  type: "userMessage";
  id: string;
  content: Array<{ type: "text"; text: string }>;
  attachments?: UserAttachment[];
}

export interface AgentObservationItem {
  type: "agentObservation";
  id: string;
  observation: VisualObservation;
}

export interface ReasoningItem {
  type: "reasoning";
  id: string;
  summary: string[];
  phase?: ProgressPhase;
}

export interface AgentMessageItem {
  type: "agentMessage";
  id: string;
  text: string;
}

export interface CommandExecutionItem {
  type: "commandExecution";
  id: string;
  tool?: string;
  arguments?: unknown;
  command: string;
  cwd: string;
  status: ItemStatus;
  aggregatedOutput?: string;
  exitCode?: number | null;
  durationMs?: number | null;
}

export interface FileChange {
  path: string;
  kind: "add" | "delete" | "update";
  diff: string;
  beforeHash?: string | null;
  afterHash?: string | null;
  beforeSize?: number | null;
  afterSize?: number | null;
  bytesWritten?: number | null;
  detectedBy?: string;
}

export interface FileChangeItem {
  type: "fileChange";
  id: string;
  tool?: string;
  changes: FileChange[];
  status: ItemStatus;
}

export interface McpToolCallItem {
  type: "mcpToolCall";
  id: string;
  server: string;
  tool: string;
  status: McpStatus;
  arguments: unknown;
  result?: unknown;
  error?: unknown;
  readOnlyHint?: boolean | null;
}

export type ThreadItem =
  | UserMessageItem
  | AgentObservationItem
  | ReasoningItem
  | AgentMessageItem
  | CommandExecutionItem
  | FileChangeItem
  | McpToolCallItem;

interface ScopedParams {
  taskId: string;
  threadId?: string;
  timestamp?: string;
}

export type AgentActivityEvent =
  | {
      method: "task/started";
      params: { task: { id: string; threadId?: string; title?: string; workspace?: string; startedAt?: string; status: "inProgress" } };
    }
  | {
      method: "item/started";
      params: ScopedParams & { item: ThreadItem };
    }
  | {
      method: "item/completed";
      params: ScopedParams & { item: ThreadItem };
    }
  | {
      method: "item/reasoning/summaryTextDelta";
      params: ScopedParams & { itemId: string; delta: string };
    }
  | {
      method: "item/agentMessage/delta";
      params: ScopedParams & { itemId: string; delta: string };
    }
  | {
      method: "item/commandExecution/outputDelta";
      params: ScopedParams & { itemId: string; delta: string };
    }
  | {
      method: "task/completed";
      params: { task: { id: string; threadId?: string; completedAt?: string; status: Exclude<TaskStatus, "inProgress">; error?: string } };
    };

export type UiEvent =
  | { method: "ui/taskIndexed"; params: { taskId: string; timestamp: string } }
  | { method: "ui/status"; params: { taskId?: string; status: ActivityStatus } }
  | { method: "ui/itemMeta"; params: { taskId?: string; itemId: string; durationMs?: number } }
  | {
      method: "ui/taskSummary";
      params: {
        taskId?: string;
        filesChanged: number;
        additions: number;
        deletions: number;
        toolCalls: number;
        durationMs: number;
      };
    }
  | {
      method: "ui/leaseSnapshot";
      params: {
        activeTaskIds: string[];
        sampledAt: number;
        stayUntilByTask?: Record<string, string>;
        visualByTask?: Record<string, VisualContext>;
        ultraByTask?: Record<string, UltraMode>;
        heartbeatAtByTask?: Record<string, string>;
        controlAckByTask?: Record<string, boolean>;
        controlResumeByTask?: Record<string, boolean>;
      };
    }
  | { method: "ui/visualContext"; params: ScopedParams & { visual: VisualContext } }
  | { method: "ui/threadMessage"; params: ScopedParams & { item: UserMessageItem } }
  | { method: "ui/bootstrapComplete"; params: Record<string, never> };

export type AgentEvent = AgentActivityEvent | UiEvent;

export interface AgentEventSource {
  readonly name: string;
  stream(signal?: AbortSignal): AsyncIterable<AgentEvent>;
}
