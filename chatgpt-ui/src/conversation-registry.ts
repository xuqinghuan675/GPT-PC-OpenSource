import type { AgentEvent } from "./event-model.ts";
import { SessionStore } from "./session-store.ts";

export interface ConversationRecord {
  id: string;
  store: SessionStore;
  startedAt: number;
  updatedAt: number;
}

export interface RegistryApplyResult {
  taskId?: string;
  created: boolean;
  bootstrapComplete: boolean;
}

function eventTaskId(event: AgentEvent): string | undefined {
  if (event.method === "ui/taskIndexed") return event.params.taskId;
  if (event.method === "task/started" || event.method === "task/completed") return event.params.task.id;
  if (event.method === "item/started" || event.method === "item/completed") return event.params.taskId;
  if (event.method === "item/reasoning/summaryTextDelta" || event.method === "item/agentMessage/delta" || event.method === "item/commandExecution/outputDelta") return event.params.taskId;
  if (event.method === "ui/status" || event.method === "ui/itemMeta" || event.method === "ui/taskSummary" || event.method === "ui/visualContext" || event.method === "ui/threadMessage") return event.params.taskId;
  return undefined;
}

function eventActivityAt(event: AgentEvent): number | undefined {
  let timestamp: string | undefined;
  if (event.method === "ui/taskIndexed") timestamp = event.params.timestamp;
  else if (event.method === "task/started") timestamp = event.params.task.startedAt;
  else if (event.method === "task/completed") timestamp = event.params.task.completedAt;
  else if (
    event.method === "item/started" ||
    event.method === "item/completed" ||
    event.method === "item/reasoning/summaryTextDelta" ||
    event.method === "item/agentMessage/delta" ||
    event.method === "item/commandExecution/outputDelta" ||
    event.method === "ui/threadMessage" ||
    event.method === "ui/visualContext"
  ) timestamp = event.params.timestamp;
  else return undefined;

  const parsed = Date.parse(timestamp ?? "");
  return Number.isFinite(parsed) ? parsed : undefined;
}

function normalizedWorkspace(path: string | undefined): string {
  return (path ?? "").replace(/\//g, "\\").replace(/\\+$/, "").toLowerCase();
}

function isInternalRuntimeTest(event: Extract<AgentEvent, { method: "task/started" }>): boolean {
  const workspace = normalizedWorkspace(event.params.task.workspace);
  if (/(?:^|\\)(?:lease-required|admin-conflict|job-test|job-identity-test|concurrency-test|automated-test|hash-test|agent-read-test|git-agent-test)-/.test(workspace)) return true;
  return /\\runtime\\maintenance-workspace\\pytest-runtime-/.test(workspace);
}

export class ConversationRegistry {
  private readonly records = new Map<string, ConversationRecord>();

  reset(): void {
    this.records.clear();
  }

  get(taskId: string | null | undefined): ConversationRecord | undefined {
    return taskId ? this.records.get(taskId) : undefined;
  }

  list(): ConversationRecord[] {
    return [...this.records.values()].sort((a, b) => b.updatedAt - a.updatedAt || b.startedAt - a.startedAt);
  }

  active(): ConversationRecord[] {
    return this.list().filter((record) => record.store.lifecycle === "active");
  }

  evictColdDetails(selectedTaskId: string | null): number {
    let evicted = 0;
    for (const record of this.records.values()) {
      if (record.id !== selectedTaskId && record.store.lifecycle !== "active" && record.store.items.size) {
        record.store.releaseDetails();
        evicted += 1;
      }
    }
    return evicted;
  }

  activeThread(threadId: string | null | undefined): ConversationRecord[] {
    if (!threadId) return [];
    return this.active().filter((record) => record.store.threadId === threadId);
  }

  thread(threadId: string | null | undefined): ConversationRecord[] {
    if (!threadId) return [];
    return this.list().filter((record) => record.store.threadId === threadId);
  }

  apply(event: AgentEvent): RegistryApplyResult {
    if (event.method === "ui/bootstrapComplete") return { created: false, bootstrapComplete: true };

    if (event.method === "ui/leaseSnapshot") {
      for (const record of this.records.values()) record.store.apply(event);
      return { created: false, bootstrapComplete: false };
    }

    if (event.method === "ui/threadMessage") {
      const record = this.records.get(event.params.taskId);
      if (!record || record.store.threadId !== event.params.threadId) return { taskId: event.params.taskId, created: false, bootstrapComplete: false };
      record.store.apply(event);
      const activityAt = eventActivityAt(event);
      if (activityAt !== undefined) record.updatedAt = Math.max(record.updatedAt, activityAt);
      return { taskId: event.params.taskId, created: false, bootstrapComplete: false };
    }

    const taskId = eventTaskId(event);
    if (!taskId) return { created: false, bootstrapComplete: false };

    let record = this.records.get(taskId);
    let created = false;
    if (event.method === "task/started" && !record) {
      if (isInternalRuntimeTest(event)) return { taskId, created: false, bootstrapComplete: false };
      const startedAt = eventActivityAt(event) ?? Date.now();
      record = { id: taskId, store: new SessionStore(), startedAt, updatedAt: startedAt };
      this.records.set(taskId, record);
      created = true;
    }

    if (!record) return { taskId, created: false, bootstrapComplete: false };
    const visualScoped = event.method === "ui/visualContext"
      || (event.method === "item/completed" && event.params.item.type === "agentObservation");
    if (visualScoped && event.params.threadId && event.params.threadId !== record.store.threadId)
      return { taskId, created: false, bootstrapComplete: false };
    record.store.apply(event);
    const activityAt = eventActivityAt(event);
    if (activityAt !== undefined) record.updatedAt = Math.max(record.updatedAt, activityAt);
    return { taskId, created, bootstrapComplete: false };
  }
}
