import type { AgentEvent, AgentEventSource, McpToolCallItem, ProgressPhase } from "../event-model.ts";

const taskId = "mock-task";
const root = "D:\\Projects\\demo-app";

function mcpItem(id: string, tool: string, status: McpToolCallItem["status"], args: unknown, result?: unknown): McpToolCallItem {
  return {
    type: "mcpToolCall",
    id,
    server: "GPT-PC",
    tool,
    status,
    arguments: args,
    result,
    readOnlyHint: tool.startsWith("read_") || tool.startsWith("search_") || tool.startsWith("git_"),
  };
}

function sleep(ms: number, signal?: AbortSignal): Promise<void> {
  if (ms <= 0 || signal?.aborted) return Promise.resolve();
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

function demoEvents(): AgentEvent[] {
  const diff = `--- a/src/demo.ts\n+++ b/src/demo.ts\n@@ -1,2 +1,3 @@\n export const ready = true;\n+export const viewer = "activity";\n`;
  return [
    { method: "task/started", params: { task: { id: taskId, title: "Activity viewer visual test", workspace: root, status: "inProgress" } } },
    { method: "ui/status", params: { taskId, status: "Thinking" } },
    { method: "item/completed", params: { taskId, item: { type: "reasoning", id: "progress-1", phase: "decision", summary: ["已确认修改范围，接下来只调整事件适配层并验证展示。"] } } },
    { method: "ui/status", params: { taskId, status: "Running tool" } },
    { method: "item/completed", params: { taskId, item: mcpItem("tool-generic", "acquire_workspace", "completed", { path: root }, { status: "acquired", mode: "execute_exclusive" }) } },
    { method: "ui/itemMeta", params: { taskId, itemId: "tool-generic", durationMs: 34 } },
    { method: "item/completed", params: { taskId, item: mcpItem("tool-read", "read_text_range", "completed", { path: `${root}\\src\\demo.ts`, start_line: 1, end_line: 40 }, { path: `${root}\\src\\demo.ts`, start_line: 1, end_line: 18, total_lines: 18, status: "ok" }) } },
    { method: "ui/itemMeta", params: { taskId, itemId: "tool-read", durationMs: 41 } },
    { method: "item/completed", params: { taskId, item: mcpItem("tool-search", "search_text", "completed", { root, query: "viewer" }, { count: 3, paths: [`${root}\\src\\demo.ts`], truncated: false }) } },
    { method: "ui/itemMeta", params: { taskId, itemId: "tool-search", durationMs: 57 } },
    { method: "item/completed", params: { taskId, item: { type: "commandExecution", id: "command-1", command: "npm test", cwd: root, status: "completed", aggregatedOutput: "3 tests passed", exitCode: 0, durationMs: 812 } } },
    { method: "item/completed", params: { taskId, item: { type: "fileChange", id: "file-1", tool: "run_python", status: "completed", changes: [{ path: `${root}\\src\\demo.ts`, kind: "update", diff, beforeHash: "1111111111aaaaaaaa", afterHash: "2222222222bbbbbbbb", beforeSize: 128, afterSize: 162, bytesWritten: 162, detectedBy: "git-worktree-delta" }] } } },
    { method: "item/completed", params: { taskId, item: mcpItem("tool-error", "read_text_file", "failed", { path: `${root}\\missing.txt` }, { error_type: "路径不存在", message: `${root}\\missing.txt`, retryable: false }) } },
    { method: "ui/itemMeta", params: { taskId, itemId: "tool-error", durationMs: 18 } },
    { method: "ui/status", params: { taskId, status: "Verifying" } },
    { method: "item/completed", params: { taskId, item: mcpItem("tool-git", "git_diff", "completed", { repo_path: root }, { exit_code: 0, stdout: "1 file changed, 1 insertion(+)" }) } },
    { method: "ui/itemMeta", params: { taskId, itemId: "tool-git", durationMs: 29 } },
    { method: "item/completed", params: { taskId, item: { type: "agentMessage", id: "done-1", text: "Viewer mock validation completed." } } },
    { method: "ui/taskSummary", params: { taskId, filesChanged: 1, additions: 1, deletions: 0, toolCalls: 6, durationMs: 1840 } },
    { method: "task/completed", params: { task: { id: taskId, status: "completed" } } },
  ];
}

export class MockEventSource implements AgentEventSource {
  readonly name = "mock-activity";
  async *stream(signal?: AbortSignal): AsyncIterable<AgentEvent> {
    for (const event of demoEvents()) {
      if (signal?.aborted) return;
      await sleep(25, signal);
      yield event;
    }
  }
}

export class StressEventSource implements AgentEventSource {
  readonly name: string;
  constructor(readonly count: number) { this.name = `stress-${count}`; }
  async *stream(signal?: AbortSignal): AsyncIterable<AgentEvent> {
    const id = `stress-${this.count}`;
    yield { method: "task/started", params: { task: { id, title: `${this.count} event stress test`, workspace: root, status: "inProgress" } } };
    for (let index = 0; index < this.count; index += 1) {
      if (signal?.aborted) return;
      const tool = index % 3 === 0 ? "read_text_range" : index % 3 === 1 ? "search_text" : "git_status";
      yield {
        method: "item/completed",
        params: {
          taskId: id,
          item: mcpItem(
            `stress-${index}`,
            tool,
            "completed",
            tool === "search_text" ? { root, query: `query-${index}` } : { path: `${root}\\file-${index}.ts` },
            tool === "search_text" ? { count: index % 7, truncated: false } : { status: "ok" },
          ),
        },
      };
    }
    yield { method: "ui/taskSummary", params: { taskId: id, filesChanged: 0, additions: 0, deletions: 0, toolCalls: this.count, durationMs: 1000 } };
    yield { method: "task/completed", params: { task: { id, status: "completed" } } };
  }
}

export type LifecycleScenario = "normal" | "done-active" | "missing-completion" | "stale-tool" | "active" | "no-progress" | "phases" | "scroll" | "sidebar";

function scenarioEvents(name: LifecycleScenario): AgentEvent[] {
  const id = `scenario-${name}`;
  const started: AgentEvent = { method: "task/started", params: { task: { id, title: `Lifecycle ${name}`, workspace: `${root}\\${name}`, status: "inProgress" } } };
  const activeSnapshot: AgentEvent = { method: "ui/leaseSnapshot", params: { activeTaskIds: [id], sampledAt: 1000 } };
  const endedSnapshot: AgentEvent = { method: "ui/leaseSnapshot", params: { activeTaskIds: [], sampledAt: 2000 } };
  const bootstrap: AgentEvent = { method: "ui/bootstrapComplete", params: {} };

  switch (name) {
    case "normal":
      return [
        started,
        activeSnapshot,
        { method: "item/started", params: { taskId: id, item: mcpItem("normal-tool", "read_text_file", "inProgress", { path: `${root}\\README.md` }) } },
        { method: "item/completed", params: { taskId: id, item: mcpItem("normal-tool", "read_text_file", "completed", { path: `${root}\\README.md` }, { status: "ok" }) } },
        { method: "task/completed", params: { task: { id, status: "completed" } } },
        endedSnapshot,
        bootstrap,
      ];
    case "done-active":
      return [
        started,
        activeSnapshot,
        { method: "ui/status", params: { taskId: id, status: "Thinking" } },
        { method: "item/completed", params: { taskId: id, item: { type: "reasoning", id: "done-progress", phase: "done", summary: ["公开完成说明已经发出，但 lease 仍然活跃。"] } } },
        bootstrap,
      ];
    case "missing-completion":
      return [started, activeSnapshot, { method: "ui/status", params: { taskId: id, status: "Verifying" } }, endedSnapshot, bootstrap];
    case "stale-tool":
      return [
        started,
        activeSnapshot,
        { method: "item/started", params: { taskId: id, item: mcpItem("stale-mcp", "search_text", "inProgress", { root, query: "missing" }) } },
        { method: "item/started", params: { taskId: id, item: { type: "commandExecution", id: "stale-command", command: "npm test", cwd: root, status: "inProgress", aggregatedOutput: "" } } },
        endedSnapshot,
        bootstrap,
      ];
    case "active":
      return [started, activeSnapshot, { method: "ui/status", params: { taskId: id, status: "Running tool" } }, bootstrap];
    case "no-progress":
      return [
        started,
        activeSnapshot,
        { method: "item/completed", params: { taskId: id, item: mcpItem("plain-tool", "git_status", "completed", { repo_path: root }, { status: "ok" }) } },
        { method: "task/completed", params: { task: { id, status: "completed" } } },
        endedSnapshot,
        bootstrap,
      ];
    case "scroll":
      return [
        started,
        activeSnapshot,
        ...Array.from({ length: 120 }, (_, index): AgentEvent => ({
          method: "item/completed",
          params: { taskId: id, item: mcpItem(`scroll-${index}`, "read_text_range", "completed", { path: `${root}\\file-${index}.ts` }, { status: "ok", line: index }) },
        })),
        { method: "task/completed", params: { task: { id, status: "completed" } } },
        endedSnapshot,
        bootstrap,
      ];
    case "sidebar":
      return [
        { method: "task/started", params: { task: { id: "sidebar-noise", title: "自动测试", workspace: "C:\\GPT-PC\\job-test-fixture", startedAt: "2026-09-01T00:10:00Z", status: "inProgress" } } },
        { method: "task/completed", params: { task: { id: "sidebar-noise", completedAt: "2026-09-01T00:11:00Z", status: "completed" } } },
        { method: "task/started", params: { task: { id: "sidebar-alpha-main", title: "Alpha latest activity", workspace: "D:\\Projects\\Alpha", startedAt: "2026-09-01T01:00:00Z", status: "inProgress" } } },
        { method: "item/completed", params: { taskId: "sidebar-alpha-main", timestamp: "2026-09-01T03:00:00Z", item: mcpItem("sidebar-alpha-tool", "read_text_file", "completed", { path: "D:\\Projects\\Alpha\\README.md" }, { status: "ok" }) } },
        { method: "task/completed", params: { task: { id: "sidebar-alpha-main", completedAt: "2026-09-01T03:00:00Z", status: "completed" } } },
        { method: "task/started", params: { task: { id: "sidebar-alpha-alt", title: "Alpha path normalization", workspace: "d:/projects/alpha/", startedAt: "2026-09-01T02:20:00Z", status: "inProgress" } } },
        { method: "task/completed", params: { task: { id: "sidebar-alpha-alt", completedAt: "2026-09-01T02:30:00Z", status: "completed" } } },
        { method: "task/started", params: { task: { id: "sidebar-maintenance", title: "Runtime maintenance", workspace: "C:\\GPT-PC\\runtime\\maintenance-workspace", startedAt: "2026-09-01T00:30:00Z", status: "inProgress" } } },
        { method: "task/completed", params: { task: { id: "sidebar-maintenance", completedAt: "2026-09-01T02:50:00Z", status: "completed" } } },
        { method: "task/started", params: { task: { id: "sidebar-desktop", title: "Desktop operation", workspace: "C:\\GPT-PC\\workspaces\\desktop-ops", startedAt: "2026-09-01T00:40:00Z", status: "inProgress" } } },
        { method: "task/completed", params: { task: { id: "sidebar-desktop", completedAt: "2026-09-01T02:40:00Z", status: "completed" } } },
        { method: "task/started", params: { task: { id: "sidebar-temp", title: "Temporary validation", workspace: "C:\\Users\\ExampleUser\\AppData\\Local\\Temp", startedAt: "2026-09-01T00:50:00Z", status: "inProgress" } } },
        { method: "task/completed", params: { task: { id: "sidebar-temp", completedAt: "2026-09-01T02:35:00Z", status: "completed" } } },
        { method: "task/started", params: { task: { id: "sidebar-legit-auto-test", title: "自动测试", workspace: "D:\\Projects\\Gamma", startedAt: "2026-09-01T01:30:00Z", status: "inProgress" } } },
        { method: "task/completed", params: { task: { id: "sidebar-legit-auto-test", completedAt: "2026-09-01T02:15:00Z", status: "completed" } } },
        { method: "task/started", params: { task: { id: "sidebar-beta", title: "Beta newer start", workspace: "D:\\Projects\\Beta", startedAt: "2026-09-01T02:00:00Z", status: "inProgress" } } },
        { method: "task/completed", params: { task: { id: "sidebar-beta", completedAt: "2026-09-01T02:10:00Z", status: "completed" } } },
        { method: "task/started", params: { task: { id: "sidebar-noise-after", title: "自动测试", workspace: "C:\\GPT-PC\\job-test-after-fixture", startedAt: "2026-09-01T03:10:00Z", status: "inProgress" } } },
        bootstrap,
      ];
    case "phases": {
      const phases: ProgressPhase[] = ["plan", "decision", "progress", "verification", "warning", "done"];
      return [
        started,
        activeSnapshot,
        ...phases.map<AgentEvent>((phase) => ({
          method: "item/completed",
          params: { taskId: id, item: { type: "reasoning", id: `phase-${phase}`, phase, summary: [`phase:${phase}`] } },
        })),
        { method: "task/completed", params: { task: { id, status: "completed" } } },
        endedSnapshot,
        bootstrap,
      ];
    }
  }
}

export class LifecycleScenarioEventSource implements AgentEventSource {
  readonly name: string;
  constructor(readonly scenario: LifecycleScenario) { this.name = `lifecycle-${scenario}`; }
  async *stream(signal?: AbortSignal): AsyncIterable<AgentEvent> {
    for (const event of scenarioEvents(this.scenario)) {
      if (signal?.aborted) return;
      yield event;
      await sleep(5, signal);
    }
  }
}
