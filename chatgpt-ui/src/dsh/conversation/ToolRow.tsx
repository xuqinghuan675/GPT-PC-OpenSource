import { useEffect, useState, type ReactNode } from "react";
import type { DshToolPresentation } from "../../presentation/types.ts";
import { DiffBlock, diffTotals } from "../primitives/DiffBlock.tsx";
import { DisclosureRow } from "../primitives/DisclosureRow.tsx";
import { IconBrowseOutline16, IconEditOutline16, IconSearchOutline16, IconThinkOutline14 } from "../primitives/icons.tsx";
import { ReadBlock } from "../primitives/ReadBlock.tsx";
import { SearchBlock } from "../primitives/SearchBlock.tsx";
import { StateDot } from "../primitives/StateDot.tsx";
import { TerminalBlock } from "../primitives/TerminalBlock.tsx";
import css from "./ToolRow.module.css";
import { cx } from "../primitives/utils.ts";

const foldLabels = {
  collapseAria: "收起",
  expandAria: (hidden: number) => `展开 ${hidden} 行隐藏内容`,
  collapse: "收起",
  expand: (hidden: number) => `再显示 ${hidden} 行`,
};
const diffLabels = { ...foldLabels, copy: "复制", copied: "已复制", files: (count: number) => `${count} 个文件` };
const terminalLabels = {
  ...foldLabels,
  signal: (signal: string) => signal,
  exitCode: (exitCode: number) => `退出码 ${exitCode}`,
  running: "运行中",
  stopped: "已中止",
  failed: "失败",
  done: "完成",
  copy: "复制",
  copied: "已复制",
  noOutput: "无输出",
};
const readLabels = { ...foldLabels, window: (shown: number, total: number) => `显示 ${shown} / ${total} 行`, copy: "复制", copied: "已复制" };
const searchLabels = {
  ...foldLabels,
  pathsSummary: (shown: number, total: number, truncated: boolean) => `${shown}${truncated || total > shown ? ` / ${total}` : ""} 个路径`,
  matchesSummary: (shown: number, total: number, files: number, truncated: boolean) => `${shown}${truncated || total > shown ? ` / ${total}` : ""} 个匹配 · ${files} 个文件`,
  copy: "复制",
  copied: "已复制",
  noResults: "无结果",
};

function iconFor(item: DshToolPresentation): ReactNode {
  switch (item.toolKind) {
    case "read": return <IconBrowseOutline16 size={14} />;
    case "search": return <IconSearchOutline16 size={14} />;
    case "edit": case "delete": case "move": return <IconEditOutline16 size={14} />;
    default: return <IconThinkOutline14 size={14} />;
  }
}

function leadingFor(item: DshToolPresentation): ReactNode {
  if (item.status === "running") return <StateDot state="ongoing" />;
  if (item.status === "error") return <StateDot state="error" />;
  if (item.status === "stopped") return <StateDot state="warning" />;
  return iconFor(item);
}

function CopyTextButton({ text }: { text: string }) {
  const [copied, setCopied] = useState(false);
  const copy = async () => {
    try {
      await navigator.clipboard.writeText(text);
      setCopied(true);
      window.setTimeout(() => setCopied(false), 1200);
    } catch {
      setCopied(false);
    }
  };
  return <button type="button" className={css.copyButton} onClick={copy}>{copied ? "已复制" : "复制"}</button>;
}

function IoSection({ label, text, error = false }: { label: string; text: string; error?: boolean }) {
  return (
    <div className={css.ioSection}>
      <span className={css.ioLabel}>{label}</span>
      <span className={css.ioText} data-error={error || undefined}>{text}</span>
      <CopyTextButton text={text} />
    </div>
  );
}

function cardBody(item: DshToolPresentation): ReactNode {
  if (item.card === "terminal" && item.terminal) {
    return (
      <div className={css.detailStack}>
        {item.input != null && item.input !== "" && (
          <div className={css.ioCard} data-testid="terminal-input-card">
            <IoSection label="参数" text={item.input} />
          </div>
        )}
        <TerminalBlock {...item.terminal} labels={terminalLabels} maxLines={24} className={css.terminalBody} testId="terminal-block" />
      </div>
    );
  }
  if (item.card === "diff" && item.diff) {
    return <DiffBlock {...item.diff} labels={diffLabels} maxLines={16} className={css.diffBody} testId="diff-block" />;
  }
  if (item.card === "read" && item.read) {
    return <ReadBlock {...item.read} labels={readLabels} maxLines={16} className={css.readBody} testId="read-block" />;
  }
  if (item.card === "search" && item.search) {
    return <SearchBlock {...item.search} labels={searchLabels} maxLines={16} className={css.searchBody} testId="search-block" />;
  }
  const inputText = item.input ?? "";
  const outputText = item.output && item.output.trim() !== ""
    ? item.output
    : item.status === "running"
      ? ""
      : item.status === "error"
        ? "失败（工具未返回错误文本）"
        : "无输出（工具已完成）";
  if (inputText === "" && outputText === "") return null;
  return (
    <div className={css.ioCard} data-testid="generic-tool-card">
      {inputText !== "" && <IoSection label="输入" text={inputText} />}
      {inputText !== "" && outputText !== "" && <span className={css.ioDivider} aria-hidden />}
      {outputText !== "" && <IoSection label={item.status === "error" ? "错误" : "输出"} text={outputText} error={item.status === "error"} />}
    </div>
  );
}

// Adapted from DeepSeek Harness ui-tool/ToolRow.tsx and GenericToolCard.tsx.
export function ToolRow({ item }: { item: DshToolPresentation }) {
  const [expanded, setExpanded] = useState(item.status === "error");
  useEffect(() => {
    if (item.status === "error") setExpanded(true);
  }, [item.status]);
  const body = cardBody(item);
  const expandable = body !== null;
  const diffStat = item.diff ? (() => {
    const totals = diffTotals(item.diff.diffs);
    return (
      <>
        <span className={css.diffAdd} data-diff-additions="">+{totals.added}</span>
        <span className={css.diffDel} data-diff-deletions="">-{totals.removed}</span>
      </>
    );
  })() : null;
  const statusLabel = item.status === "running" ? "运行中" : item.status === "error" ? "失败" : item.status === "stopped" ? "已中止" : "完成";
  const runtimeMeta = [statusLabel, item.tool, item.durationMs != null ? `${item.durationMs} ms` : ""].filter(Boolean).join(" · ");
  const suffix = runtimeMeta || diffStat ? (
    <span className={css.metaCluster}>
      {runtimeMeta && <span>{runtimeMeta}</span>}
      {diffStat && <span className={css.diffStat}>{diffStat}</span>}
    </span>
  ) : null;
  const variant = item.toolKind === "execute" ? "bash" : item.toolKind === "other" ? "others" : item.toolKind;
  return (
    <div
      className={css.root}
      data-agent-item={item.id}
      data-testid={item.card === "generic" ? "generic-tool-row" : `${item.card}-tool-row`}
      data-variant={variant}
      data-tool={item.tool}
      data-state={item.status}
    >
      <DisclosureRow
        rowClassName={css.row}
        leadingClassName={css.leading}
        titleClassName={css.title}
        chevronClassName={css.chevron}
        icon={leadingFor(item)}
        title={item.title}
        open={expanded && expandable}
        expandable={expandable}
        expandOnRowClick
        keepContentWhenOpen
        onToggle={() => setExpanded((value) => !value)}
        collapsedContent={(
          <>
            <span className={css.sep} aria-hidden />
            <span className={cx(css.summary, item.status === "error" && css.errorSummary)}>{item.summary}</span>
            {suffix && <span className={css.summarySuffix}>{suffix}</span>}
          </>
        )}
      >
        <div className={css.bodyWrap}>{body}</div>
      </DisclosureRow>
    </div>
  );
}
