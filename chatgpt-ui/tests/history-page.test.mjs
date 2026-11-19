import test from "node:test";
import assert from "node:assert/strict";
import { GPTPcEventSource } from "../src/sources/gpt-pc-event-source.ts";
import { MAX_RETAINED_ITEMS, SessionStore } from "../src/session-store.ts";
import { ConversationRegistry } from "../src/conversation-registry.ts";

const taskId = "11111111111111111111111111111111";
const otherId = "22222222222222222222222222222222";
const timestamp = "2026-10-14T01:00:00Z";
const started = (id) => ({
  method: "task/started",
  params: { task: { id, threadId: "thread-" + id, title: "Task " + id, workspace: "D:\\example", startedAt: timestamp, status: "inProgress" } },
});
const reason = (id, name) => ({
  method: "item/completed",
  params: { taskId: id, timestamp, item: { type: "reasoning", id: name, summary: [name] } },
});

test("the projected history page joins tool start/result, visual and diff without touching live state", () => {
  const source = new GPTPcEventSource();
  const raws = [
    { type: "tool.started", taskId, threadId: "thread-" + taskId, callId: "call-A", tool: "run_python", arguments: { code: "print(2)" }, timestamp },
    { type: "tool.completed", taskId, callId: "call-A", tool: "run_python", result: { stdout: "2", exit_code: 0 }, timestamp },
    { type: "agent.progress", taskId, id: "p1", text: "working", timestamp },
    { type: "file.changed", taskId, id: "f1", path: "D:\\example\\foo.py", diff: "+new", timestamp },
    { type: "runtime.liveness_probe", taskId, id: "heartbeat", timestamp },
    { type: "agent.progress", taskId: otherId, id: "outsider", text: "unrelated", timestamp },
  ];
  const rows = raws.map((row) => JSON.stringify(row)).join("\n") + "\n";
  const page = source.projectTaskHistory(taskId, "thread-" + taskId, "D:\\example", "Task", rows);
  assert.equal(page.length, 3);
  assert.equal(page[0].kind, "command");
  assert.equal(page[0].status, "completed");
  assert.match(page[0].outputSummary, /2/);
  assert.equal(page[1].kind, "reasoning");
  assert.equal(page[2].kind, "fileChange");
  assert.equal(source.tasks.size, 0, "page projection must not pollute the live source");
});

test("in-memory records are capped independently of the ten-thousand-item disk browse window", () => {
  const store = new SessionStore();
  store.apply(started(taskId));
  for (let i = 0; i < 1200; i++) store.apply(reason(taskId, "item-" + i));
  assert.equal(store.order.length, MAX_RETAINED_ITEMS);
  assert.equal(store.items.size, MAX_RETAINED_ITEMS);
  assert.equal(store.processedItemCount, 1200);
  assert.equal(store.order.at(-1), "item-1199");
  store.releaseDetails();
  assert.equal(store.items.size, 0);
  assert.equal(store.order.length, 0);
  assert.equal(store.taskId, taskId);
  assert.equal(store.lifecycle, "active");
  assert.equal(store.processedItemCount, 1200);
});

test("inactive unselected leases are evicted periodically without hiding conversations", () => {
  const registry = new ConversationRegistry();
  registry.apply(started(taskId));
  registry.apply(reason(taskId, "historic"));
  registry.apply({ method: "task/completed", params: { task: { id: taskId, completedAt: timestamp, status: "completed" } } });
  registry.apply(started(otherId));
  registry.apply(reason(otherId, "active-message"));
  assert.equal(registry.evictColdDetails(null), 1);
  assert.equal(registry.get(taskId).store.items.size, 0);
  assert.equal(registry.get(otherId).store.items.size, 1);
  assert.equal(registry.list().length, 2);
});

test("lightweight index updates sidebar recency without creating a visible event card", () => {
  const registry = new ConversationRegistry();
  registry.apply(started(taskId));
  const recent = "2026-10-14T02:00:00Z";
  registry.apply({ method: "ui/taskIndexed", params: { taskId, timestamp: recent } });
  assert.equal(registry.get(taskId).updatedAt, Date.parse(recent));
  assert.equal(registry.get(taskId).store.items.size, 0);
});


test("compact bootstrap retains historical counts without loading tool bodies", () => {
  const converter = new GPTPcEventSource();
  [...converter.map({ type: "task.started", taskId, threadId: "thread-" + taskId, workspace: "D:\\example", timestamp })];
  const events = [...converter.map({ type: "task.summary_indexed", taskId, timestamp, filesChanged: 9, toolCalls: 144, additions: 120, deletions: 31 })];
  assert.equal(events.length, 1);
  assert.equal(events[0].method, "ui/taskSummary");
  assert.deepEqual([events[0].params.filesChanged, events[0].params.toolCalls, events[0].params.additions, events[0].params.deletions], [9, 144, 120, 31]);
});
