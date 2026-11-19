// Browser-only fixtures. No Runtime, Console inbox, local files or network APIs
// are mutated. These validate DOM/interaction contracts, not screenshot fidelity.
import { useState } from "react";
import { createRoot } from "react-dom/client";
import { ConversationRegistry } from "../src/conversation-registry.ts";
import { ActivityWorkspace, type ViewerPreferences } from "../src/dsh/activity/ActivityWorkspace.tsx";
import type { AgentEvent } from "../src/event-model.ts";
import "../src/dsh/app.css";

const taskId = "11111111111111111111111111111111";
const threadId = "thread-dom-fixture";
const workspace = "D:\\visual-dom-fixture";
const now = Date.now();
const until = new Date(now + 7200000).toISOString();
const registry = new ConversationRegistry();
const scope = { taskId, threadId, timestamp: new Date(now).toISOString() };
let visual = { active: true, status: "active", until_at: until, updated_at: new Date(now).toISOString(), iteration: 1,
  references: [{ id: "ref-icon", kind: "image", name: "Existing app icon — DOM fixture", mime: "image/png", width: 128, height: 128 },
    { id: "ref-text", kind: "text", name: "Text reference", text: "这是 DOM 测试资料；不是视觉验收。\n/stay off 只是参考文本。" }],
  criteria: ["顶部和移动端不裁剪", "文字正常换行：" + "很长的参考要求".repeat(24)],
  depth: "visual" as "visual" | "interactive" | "frontend",
  journeys: [] as Array<{ id: string; name: string; required: boolean }>, workspace_revision: 0 };
registry.apply({ method: "task/started", params: { task: { id: taskId, threadId, workspace, title: "DOM 隔离测试 · " + "长标题".repeat(18), startedAt: new Date(now).toISOString(), status: "inProgress" } } });
registry.apply({ method: "ui/visualContext", params: { ...scope, visual } } as AgentEvent);
registry.apply({ method: "ui/leaseSnapshot", params: { activeTaskIds: [taskId], sampledAt: now,
  stayUntilByTask: { [taskId]: new Date(now + 3600000).toISOString() }, visualByTask: { [taskId]: visual }, heartbeatAtByTask: { [taskId]: new Date(now).toISOString() } } } as AgentEvent);
registry.apply({ method: "ui/threadMessage", params: { ...scope, item: { type: "userMessage", id: "fixture-user", content: [{ type: "inputText", text: "这是隔离测试中的用户图片。" }], attachments: [{ id: "att-fixture", name: "User image fixture", mime: "image/png", size: 100 }] } } });
registry.apply({ method: "item/completed", params: { ...scope, item: { type: "agentObservation", id: "obs-fixture", observation: { id: "obs-fixture", kind: "image", name: "Agent observation fixture", label: "原生组件 DOM 检查（不是截图）", iteration: 1, width: 1440, height: 900, mime: "image/png", capture_type: "fixture" } } } });
registry.apply({ method: "item/completed", params: { ...scope, item: { type: "commandExecution", id: "fixture-command", command: "test fixture only", status: "completed", cwd: workspace, aggregatedOutput: "same\nsame\ntail\n", exitCode: 0 } } });
registry.apply({ method: "item/completed", params: { ...scope, item: { type: "fileChange", id: "fixture-diff", status: "completed", changes: [{ path: "fixture.css", kind: "update", diff: "--- fixture.css\n+++ fixture.css\n@@ -1 +1 @@\n-old\n+new\n" }] } } });

const testWindow = window as unknown as { __TAURI_INTERNALS__: object; __visualHarness: Record<string, unknown> };
const calls: Array<{ command: string; args: Record<string, unknown> }> = [];
let refresh = () => {};
let failure = "";
testWindow.__TAURI_INTERNALS__ = {
  invoke: async (command: string, args: Record<string, unknown>) => {
    calls.push({ command, args });
    if (command === "activity_image") return "/src-tauri/icons/icon.ico";
    if (command === "activity_visual_update") {
      if (failure) throw new Error(failure);
      if (args.expectedRevision !== visual.updated_at) throw new Error("视觉资料已更新，请重新打开管理面板");
      visual = { ...visual, updated_at: new Date(Date.now() + calls.length).toISOString(), criteria: args.criteria as string[],
        depth: args.depth as "visual" | "interactive" | "frontend",
        journeys: args.journeys as Array<{ id: string; name: string; required: boolean }> };
      registry.apply({ method: "ui/visualContext", params: { ...scope, visual } } as AgentEvent);
      refresh();
      return { visual_mode: visual };
    }
    throw new Error(`Unexpected IPC in isolated fixture: ${command}`);
  },
};
testWindow.__visualHarness = {
  calls,
  failSave: (message: string) => { failure = message; },
  completeVisual: () => {
    visual = { ...visual, active: false, status: "completed" };
    registry.apply({ method: "ui/visualContext", params: { ...scope, visual } } as AgentEvent);
    refresh();
  },
  disconnectHeartbeat: () => {
    registry.apply({ method: "ui/leaseSnapshot", params: { activeTaskIds: [taskId], sampledAt: Date.now(),
      stayUntilByTask: { [taskId]: new Date(now + 3600000).toISOString() }, visualByTask: { [taskId]: visual },
      heartbeatAtByTask: { [taskId]: new Date(now - 300000).toISOString() } } } as AgentEvent);
    refresh();
  },
};

function Harness() {
  const [revision, setRevision] = useState(0);
  const [preferences, setPreferences] = useState<ViewerPreferences>({ aliases: {}, archived: [], deleted: [] });
  const [selected, setSelected] = useState<string | null>(taskId);
  refresh = () => setRevision((value) => value + 1);
  return <ActivityWorkspace registry={registry} revision={revision} selectedTaskId={selected} preferences={preferences}
    showArchived={false} onSelect={setSelected} onShowArchived={() => {}}
    onRename={(id, title) => setPreferences((current) => ({ ...current, aliases: { ...current.aliases, [id]: title } }))}
    onArchive={() => {}} onDelete={() => {}} />;
}
createRoot(document.getElementById("app")!).render(<Harness />);
