import type { ViewItem } from "../session-store.ts";
import type { DiffHunk } from "../dsh/primitives/DiffBlock.tsx";
import type { DshPresentationItem, DshToolPresentation, PresentationStatus, ToolKind } from "./types.ts";

const EXECUTE_WORDS = ["exec", "command", "shell", "powershell", "cmd", "npm", "npx", "python", "node", "run", "test", "build"];

function includesAny(value: string, words: readonly string[]): boolean {
  return words.some((word) => value.includes(word));
}

export function classifyTool(tool: string): ToolKind {
  const name = tool.toLowerCase();
  if (includesAny(name, ["delete", "remove", "unlink"])) return "delete";
  if (includesAny(name, ["move", "rename", "relocate"])) return "move";
  if (includesAny(name, ["search", "find", "grep", "glob", "query"])) return "search";
  if (includesAny(name, ["read", "list", "file_info", "stat", "show", "log", "status", "rev_parse"])) return "read";
  if (includesAny(name, ["write", "patch", "replace", "append", "create", "copy", "edit", "commit", "add"])) return "edit";
  if (includesAny(name, ["fetch", "http", "web", "download", "request"])) return "fetch";
  if (includesAny(name, EXECUTE_WORDS)) return "execute";
  return "other";
}

function statusFrom(value?: string): PresentationStatus {
  if (value === "inProgress") return "running";
  if (value === "failed" || value === "declined") return "error";
  if (value === "stopped") return "stopped";
  return "ok";
}

function safeJson(text: string): unknown {
  try { return JSON.parse(text); } catch { return undefined; }
}

function record(value: unknown): Record<string, unknown> | undefined {
  return value !== null && typeof value === "object" && !Array.isArray(value) ? value as Record<string, unknown> : undefined;
}

function stringValue(value: unknown): string | undefined {
  return typeof value === "string" ? value : undefined;
}

function numberValue(value: unknown): number | undefined {
  return typeof value === "number" && Number.isFinite(value) ? value : undefined;
}

function guessPath(argumentsSummary: string): string | undefined {
  const args = record(safeJson(argumentsSummary));
  if (!args) return undefined;
  for (const key of ["path", "file", "file_path", "repo_path", "root", "source"]) {
    const value = stringValue(args[key]);
    if (value) return value;
  }
  return undefined;
}

function compactSummary(value: string, limit = 120): string {
  const compact = value.replace(/\s+/g, " ").trim();
  return compact.length <= limit ? compact : `${compact.slice(0, limit - 1)}…`;
}

function argumentSummaryLabel(argumentsSummary: string): string | undefined {
  const args = record(safeJson(argumentsSummary));
  if (!args) return undefined;

  const paths = Array.isArray(args.paths)
    ? args.paths.filter((value): value is string => typeof value === "string" && value.length > 0)
    : [];
  if (paths.length === 1) return compactSummary(paths[0]);
  if (paths.length > 1) return `${compactSummary(paths[0], 88)} 等 ${paths.length} 个文件`;

  const path = guessPath(argumentsSummary);
  const query = stringValue(args.query) ?? stringValue(args.pattern) ?? stringValue(args.regex);
  if (path && query) return `${compactSummary(path, 88)} · ${compactSummary(query, 72)}`;
  if (path) return compactSummary(path);
  if (query) return compactSummary(query);

  const cwd = stringValue(args.cwd);
  const command = stringValue(args.command);
  if (cwd && command) return `${compactSummary(cwd, 78)} · ${compactSummary(command, 82)}`;
  if (cwd) return compactSummary(cwd);
  if (command) return compactSummary(command);
  return undefined;
}

function languageFor(path: string): string | undefined {
  const ext = path.split(".").pop()?.toLowerCase();
  const map: Record<string, string> = {
    ts: "typescript", tsx: "tsx", js: "javascript", jsx: "jsx", css: "css", html: "html",
    json: "json", md: "markdown", rs: "rust", py: "python", ps1: "powershell", toml: "toml",
  };
  return ext ? map[ext] : undefined;
}

function readPresentation(item: Extract<ViewItem, { kind: "mcp" }>) {
  const args = record(safeJson(item.argumentsSummary));
  const result = record(safeJson(item.resultSummary));
  const path = stringValue(result?.path) ?? argumentSummaryLabel(item.argumentsSummary) ?? item.tool;
  const start = numberValue(result?.start_line) ?? numberValue(result?.startLine) ?? numberValue(args?.start_line) ?? 1;
  const total = numberValue(result?.total_lines) ?? numberValue(result?.totalLines);
  const content = result?.content;
  let textLines: string[] = [];
  if (Array.isArray(content)) textLines = content.filter((value): value is string => typeof value === "string");
  else if (typeof content === "string") textLines = content.replace(/\r\n/g, "\n").split("\n");
  if (textLines.length === 0) textLines = item.resultSummary.split("\n");
  return {
    label: path,
    lines: textLines.map((text, index) => ({ number: start + index, text })),
    totalLines: total ?? Math.max(start + textLines.length - 1, textLines.length),
    lang: languageFor(path),
  };
}

function searchPresentation(item: Extract<ViewItem, { kind: "mcp" }>): DshToolPresentation["search"] {
  const result = record(safeJson(item.resultSummary));
  const total = numberValue(result?.total) ?? numberValue(result?.matches) ?? numberValue(result?.count) ?? 0;
  const truncated = result?.truncated === true;
  const files = result?.files;
  if (Array.isArray(files)) {
    const groups = files.flatMap((entry) => {
      if (typeof entry === "string") return [];
      const group = record(entry);
      const path = stringValue(group?.path);
      const matches = group?.matches;
      if (!path || !Array.isArray(matches)) return [];
      return [{
        path,
        matches: matches.flatMap((match) => {
          const row = record(match);
          const lineNumber = numberValue(row?.lineNumber) ?? numberValue(row?.line);
          const line = stringValue(row?.text) ?? stringValue(row?.content);
          return lineNumber !== undefined && line !== undefined ? [{ lineNumber, line }] : [];
        }),
      }];
    });
    if (groups.length > 0) {
      const retained = groups.reduce((sum, group) => sum + group.matches.length, 0);
      return { kind: "matches", files: groups, total: total || retained, truncated };
    }
    const paths = files.filter((value): value is string => typeof value === "string");
    return { kind: "paths", paths, total: total || paths.length, truncated };
  }
  const paths = Array.isArray(result?.paths) ? result.paths.filter((value): value is string => typeof value === "string") : [];
  if (paths.length > 0) return { kind: "paths", paths, total: total || paths.length, truncated };
  const fallbackPath = guessPath(item.argumentsSummary);
  const fallback = fallbackPath ? [fallbackPath] : item.resultSummary ? [item.resultSummary] : [];
  return { kind: "paths", paths: fallback, total: total || fallback.length, truncated };
}

function diffHunk(path: string, kind: "add" | "delete" | "update", diff: string): DiffHunk {
  const oldLines: string[] = [];
  const newLines: string[] = [];
  for (const line of diff.replace(/\r\n/g, "\n").split("\n")) {
    if (line.startsWith("---") || line.startsWith("+++")) continue;
    if (line.startsWith("-")) oldLines.push(line.slice(1));
    else if (line.startsWith("+")) newLines.push(line.slice(1));
  }
  return {
    path,
    oldText: kind === "add" ? null : oldLines.join("\n"),
    newText: kind === "delete" ? "" : newLines.join("\n"),
  };
}

function titleFor(kind: ToolKind): string {
  switch (kind) {
    case "read": return "读取";
    case "search": return "搜索";
    case "edit": return "修改";
    case "delete": return "删除";
    case "move": return "移动";
    case "execute": return "运行";
    case "fetch": return "获取";
    default: return "工具";
  }
}

export function presentViewItem(item: ViewItem): DshPresentationItem {
  if (item.kind === "observation") return { kind: "observation", id: item.id, observation: item.observation };
  if (item.kind === "user") return { kind: "user", id: item.id, text: item.text, attachments: item.attachments };
  if (item.kind === "reasoning") return { kind: "reasoning", id: item.id, text: item.text, running: item.status === "inProgress", phase: item.phase };
  if (item.kind === "agent") return { kind: "assistant", id: item.id, text: item.text };

  if (item.kind === "command") {
    const exitCode = typeof item.exitCode === "number" ? item.exitCode : undefined;
    return {
      kind: "tool",
      id: item.id,
      toolKind: "execute",
      card: "terminal",
      status: statusFrom(item.status),
      title: "运行",
      summary: item.command,
      server: "GPT-PC",
      tool: item.tool,
      durationMs: item.durationMs,
      input: item.argumentsSummary,
      output: item.outputSummary,
      terminal: { command: item.command, cwd: item.cwd, output: item.outputSummary, exitCode, running: item.status === "inProgress", stopped: item.status === "stopped" },
    };
  }

  if (item.kind === "fileChange") {
    const additions = item.changes.reduce((sum, change) => sum + change.additions, 0);
    const deletions = item.changes.reduce((sum, change) => sum + change.deletions, 0);
    const firstChange = item.changes[0];
    const summary = item.changes.length === 1 ? firstChange?.path ?? "文件变更" : `${item.changes.length} 个文件`;
    const hasTextDiff = item.changes.some((change) => change.diff.trim().length > 0);
    const title = item.changes.length === 1
      ? firstChange?.kind === "add" ? "新增文件" : firstChange?.kind === "delete" ? "删除文件" : "修改文件"
      : "文件变更";
    const evidence = item.changes.map((change) => {
      const beforeHash = change.beforeHash?.slice(0, 10) ?? "∅";
      const afterHash = change.afterHash?.slice(0, 10) ?? "∅";
      const beforeSize = change.beforeSize == null ? "∅" : `${change.beforeSize} B`;
      const afterSize = change.afterSize == null ? "∅" : `${change.afterSize} B`;
      const written = change.bytesWritten == null ? "" : ` · 写入 ${change.bytesWritten} B`;
      const source = change.detectedBy ? ` · ${change.detectedBy}` : "";
      return `${change.path}\nsize ${beforeSize} → ${afterSize}${written}\nhash ${beforeHash} → ${afterHash}${source}`;
    }).join("\n\n");
    return {
      kind: "tool",
      id: item.id,
      toolKind: "edit",
      card: hasTextDiff ? "diff" : "generic",
      status: statusFrom(item.status),
      title,
      summary,
      server: "GPT-PC",
      tool: item.tool ?? "file.changed",
      output: hasTextDiff ? `+${additions} -${deletions}\n${evidence}` : (evidence || "内容已变化（无文本 diff）"),
      diff: hasTextDiff ? { diffs: item.changes.map((change) => diffHunk(change.path, change.kind, change.diff)) } : undefined,
    };
  }

  const gitOperation = item.tool.toLowerCase().startsWith("git_");
  const toolKind = gitOperation ? "other" : classifyTool(item.tool);
  const common: DshToolPresentation = {
    kind: "tool",
    id: item.id,
    toolKind,
    card: "generic",
    status: statusFrom(item.status),
    title: gitOperation ? "Git" : titleFor(toolKind),
    summary: argumentSummaryLabel(item.argumentsSummary) ?? `${item.server} · ${item.tool}`,
    server: item.server,
    tool: item.tool,
    durationMs: item.durationMs,
    input: item.argumentsSummary,
    output: item.resultSummary,
  };
  if (toolKind === "read" && includesAny(item.tool.toLowerCase(), ["read", "file_info", "list_directory"])) {
    return { ...common, card: "read", read: readPresentation(item) };
  }
  if (toolKind === "search") return { ...common, card: "search", search: searchPresentation(item) };
  return common;
}
