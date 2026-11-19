import { useCallback, useMemo, useState } from "react";
import { FoldToggle } from "./FoldToggle.tsx";
import { keyedLines } from "./keyed-lines.ts";
import css from "./DiffBlock.module.css";
import { contentLines, cx, writeClipboard } from "./utils.ts";

export const DEFAULT_DIFF_MAX_LINES = 16;

export interface DiffHunk {
  path: string;
  oldText: string | null;
  newText: string;
}

export interface DiffBlockLabels {
  copy: string;
  copied: string;
  collapseAria: string;
  expandAria: (hidden: number) => string;
  collapse: string;
  expand: (hidden: number) => string;
  files: (count: number) => string;
}

export interface DiffBlockProps {
  diffs: DiffHunk[];
  labels: DiffBlockLabels;
  maxLines?: number;
  className?: string;
  testId?: string;
}

type DiffRow = { id: string; kind: "path" | "del" | "add" | "gap"; text: string };

const ROW_CLASS: Record<DiffRow["kind"], string | undefined> = {
  path: css.path,
  del: css.del,
  add: css.add,
  gap: css.gap,
};

export function diffTotals(diffs: DiffHunk[]): { added: number; removed: number } {
  let added = 0;
  let removed = 0;
  for (const diff of diffs) {
    if (diff.oldText !== null) removed += contentLines(diff.oldText).length;
    added += contentLines(diff.newText).length;
  }
  return { added, removed };
}

function buildRows(diffs: DiffHunk[]): { rows: DiffRow[]; added: number; removed: number; files: number } {
  const rows: Omit<DiffRow, "id">[] = [];
  const paths = new Set<string>();
  let previousPath: string | undefined;
  for (const diff of diffs) {
    paths.add(diff.path);
    rows.push(diff.path === previousPath ? { kind: "gap", text: "⋯" } : { kind: "path", text: diff.path });
    previousPath = diff.path;
    if (diff.oldText !== null) {
      for (const line of contentLines(diff.oldText)) rows.push({ kind: "del", text: line });
    }
    for (const line of contentLines(diff.newText)) rows.push({ kind: "add", text: line });
  }
  const identities = keyedLines(rows.map((row) => `${row.kind}:${row.text}`));
  return { rows: rows.map((row, index) => ({ ...row, id: identities[index].id })), ...diffTotals(diffs), files: paths.size };
}

function clipboardText(rows: DiffRow[]): string {
  return rows.map((row) => row.kind === "del" ? `- ${row.text}` : row.kind === "add" ? `+ ${row.text}` : row.text).join("\n");
}

// Adapted from DeepSeek Harness ui-primitives/DiffBlock.tsx. The rendering and
// head/tail collapse model are retained; only clsx/clipboard helpers are local.
export function DiffBlock({ diffs, labels, maxLines = DEFAULT_DIFF_MAX_LINES, className, testId }: DiffBlockProps) {
  const { rows, added, removed, files } = useMemo(() => buildRows(diffs), [diffs]);
  const [expanded, setExpanded] = useState(false);
  const [copied, setCopied] = useState(false);
  const onCopy = useCallback(() => {
    if (copied) return;
    void writeClipboard(clipboardText(rows)).then((ok) => {
      if (!ok) return;
      setCopied(true);
      window.setTimeout(() => setCopied(false), 1000);
    });
  }, [copied, rows]);

  if (rows.length === 0) return null;
  const hidden = rows.length - maxLines;
  const capped = hidden > 0 && !expanded;
  const headLines = Math.ceil(maxLines / 2);
  const tailLines = maxLines - headLines;
  const head = capped ? rows.slice(0, headLines) : rows;
  const tail = capped ? rows.slice(rows.length - tailLines) : [];

  return (
    <div className={cx(css.block, className)} data-diff="" data-testid={testId ?? "diff-block"}>
      <button type="button" className={css.copyButton} onClick={onCopy}>{copied ? labels.copied : labels.copy}</button>
      <div className={css.body}>
        {head.map((row) => <div key={row.id} className={cx(css.line, ROW_CLASS[row.kind])}>{row.text}</div>)}
        {hidden > 0 && (
          <FoldToggle
            className={css.expand}
            testId="diff-fold-toggle"
            expanded={expanded}
            hidden={hidden}
            labels={labels}
            onToggle={() => setExpanded((value) => !value)}
          />
        )}
        {tail.map((row) => <div key={row.id} className={cx(css.line, ROW_CLASS[row.kind])}>{row.text}</div>)}
      </div>
      <div className={css.footer}>
        <span>└</span>
        <span className={css.footerAdd}>+{added}</span>
        <span className={css.footerDel}>-{removed}</span>
        <span className={css.footerFiles}>· {labels.files(files)}</span>
      </div>
    </div>
  );
}
