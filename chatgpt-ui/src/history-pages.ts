import { invoke } from "@tauri-apps/api/core";
import { GPTPcEventSource } from "./sources/gpt-pc-event-source.ts";
import type { ViewItem } from "./session-store.ts";

export const HISTORY_PAGE_SIZE = 200;
export const MAX_HISTORY_ITEMS = 10_000;

export interface ActivityHistoryPage {
  taskId: string;
  total: number;
  start: number;
  end: number;
  items: ViewItem[];
}

export async function loadHistoryPage(
  task: { taskId: string; threadId: string; workspace: string; title: string },
  before?: number,
): Promise<ActivityHistoryPage> {
  const [total, start, rows] = await invoke<[number, number, string]>("activity_task_page", {
    taskId: task.taskId, before: before ?? null, limit: HISTORY_PAGE_SIZE,
  });
  if (!Number.isInteger(total) || total < 0 || total > MAX_HISTORY_ITEMS ||
      !Number.isInteger(start) || start < 0 || start > total || typeof rows !== "string") {
    throw new Error("活动历史分页结果无效");
  }
  const items = new GPTPcEventSource().projectTaskHistory(
    task.taskId, task.threadId, task.workspace, task.title, rows,
  );
  if (items.length > HISTORY_PAGE_SIZE) throw new Error("活动历史单页超过上限");
  return { taskId: task.taskId, total, start, end: before === undefined ? total : Math.min(total, before), items };
}
