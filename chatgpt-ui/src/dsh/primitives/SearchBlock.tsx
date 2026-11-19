import { useCallback, useMemo, useState, type ReactNode } from "react";
import css from "./SearchBlock.module.css";
import { cx, headTailCap, writeClipboard } from "./utils.ts";

export const DEFAULT_SEARCH_MAX_LINES = 16;

export interface SearchBlockLineMatch {
  lineNumber: number;
  line: string;
}

export interface SearchFileGroup {
  path: string;
  matches: SearchBlockLineMatch[];
}

interface SearchBlockCommon {
  labels: SearchBlockLabels;
  truncated: boolean;
  total: number;
  maxLines?: number;
  className?: string;
  testId?: string;
}

export interface SearchBlockLabels {
  pathsSummary: (shown: number, total: number, truncated: boolean) => string;
  matchesSummary: (shown: number, total: number, files: number, truncated: boolean) => string;
  copy: string;
  copied: string;
  noResults: string;
  collapseAria: string;
  expandAria: (hidden: number) => string;
  collapse: string;
  expand: (hidden: number) => string;
}

export interface SearchMatchesBlockProps extends SearchBlockCommon {
  kind: "matches";
  files: SearchFileGroup[];
}

export interface SearchPathsBlockProps extends SearchBlockCommon {
  kind: "paths";
  paths: string[];
}

export type SearchBlockProps = SearchMatchesBlockProps | SearchPathsBlockProps;

type SearchRow =
  | { type: "file"; path: string; count: number; index: number; collapsed: boolean }
  | { type: "match"; lineNumber: number; line: string; key: string; fileIndex: number }
  | { type: "path"; path: string };

function copyText(props: SearchBlockProps): string {
  if (props.kind === "paths") return props.paths.join("\n");
  return props.files.map((file) => [file.path, ...file.matches.map((match) => `${match.lineNumber}: ${match.line}`)].join("\n")).join("\n\n");
}

function shownCount(props: SearchBlockProps): number {
  return props.kind === "paths" ? props.paths.length : props.files.reduce((sum, file) => sum + file.matches.length, 0);
}

function toRows(props: SearchBlockProps, collapsed: ReadonlySet<number>): SearchRow[] {
  if (props.kind === "paths") return props.paths.map((path) => ({ type: "path", path }));
  const rows: SearchRow[] = [];
  props.files.forEach((file, index) => {
    const isCollapsed = collapsed.has(index);
    rows.push({ type: "file", path: file.path, count: file.matches.length, index, collapsed: isCollapsed });
    if (!isCollapsed) {
      for (const match of file.matches) {
        rows.push({ type: "match", lineNumber: match.lineNumber, line: match.line, key: `${index}:${match.lineNumber}`, fileIndex: index });
      }
    }
  });
  return rows;
}

function rowKey(row: SearchRow): string {
  if (row.type === "file") return `file:${row.index}`;
  if (row.type === "match") return `match:${row.key}`;
  return `path:${row.path}`;
}

// Adapted from DeepSeek Harness ui-primitives/SearchBlock.tsx. Group collapse,
// truncation summaries and the head/tail cap are retained without Harness runtime.
export function SearchBlock(props: SearchBlockProps) {
  const { truncated, total, maxLines = DEFAULT_SEARCH_MAX_LINES, className, testId } = props;
  const [expanded, setExpanded] = useState(false);
  const [collapsed, setCollapsed] = useState<ReadonlySet<number>>(() => new Set());
  const [copied, setCopied] = useState(false);
  const rows = useMemo(() => toRows(props, collapsed), [props, collapsed]);
  const shown = shownCount(props);
  const empty = rows.length === 0;
  const summary = props.kind === "paths"
    ? props.labels.pathsSummary(shown, total, truncated)
    : props.labels.matchesSummary(shown, total, props.files.length, truncated);
  const { hidden, capped, headLines, tailLines } = headTailCap(rows.length, maxLines, expanded);
  const head = capped ? rows.slice(0, headLines) : rows;
  const naturalTail = capped ? rows.slice(rows.length - tailLines) : [];
  const tailLead = naturalTail[0];
  const tailHeader = tailLead?.type === "match" && !head.some((row) => row.type === "file" && row.index === tailLead.fileIndex)
    ? rows.find((row): row is Extract<SearchRow, { type: "file" }> => row.type === "file" && row.index === tailLead.fileIndex)
    : undefined;
  const tail = tailHeader === undefined ? naturalTail : naturalTail.slice(1);

  const onCopy = useCallback(() => {
    if (copied) return;
    void writeClipboard(copyText(props)).then((ok) => {
      if (!ok) return;
      setCopied(true);
      window.setTimeout(() => setCopied(false), 1000);
    });
  }, [copied, props]);

  const toggleFile = useCallback((index: number) => {
    setCollapsed((previous) => {
      const next = new Set(previous);
      if (next.has(index)) next.delete(index); else next.add(index);
      return next;
    });
  }, []);

  const renderRow = (row: SearchRow): ReactNode => {
    if (row.type === "path") return <div className={css.line}>{row.path}</div>;
    if (row.type === "match") return <div className={css.line}><span className={css.lineNumber}>{row.lineNumber}: </span>{row.line}</div>;
    return (
      <button type="button" className={css.fileHeader} aria-expanded={!row.collapsed} onClick={() => toggleFile(row.index)}>
        <span className={css.filePath}>{row.path}</span>
        <span className={css.fileCount}>{row.count}</span>
      </button>
    );
  };

  return (
    <div className={cx(css.block, className)} data-search={props.kind} data-testid={testId ?? "search-block"}>
      <div className={css.header}>
        <span className={css.summary}>{summary}</span>
        {!empty && <button type="button" className={css.copyButton} onClick={onCopy}>{copied ? props.labels.copied : props.labels.copy}</button>}
      </div>
      {empty ? <div className={css.empty}>{props.labels.noResults}</div> : (
        <div className={css.body}>
          {head.map((row) => <div key={rowKey(row)}>{renderRow(row)}</div>)}
          {hidden > 0 && (
            <button type="button" className={css.expand} aria-expanded={expanded} aria-label={expanded ? props.labels.collapseAria : props.labels.expandAria(hidden)} onClick={() => setExpanded((value) => !value)}>
              {expanded ? props.labels.collapse : props.labels.expand(hidden)}
            </button>
          )}
          {tailHeader !== undefined && <div key={`tail:${rowKey(tailHeader)}`}>{renderRow(tailHeader)}</div>}
          {tail.map((row) => <div key={rowKey(row)}>{renderRow(row)}</div>)}
        </div>
      )}
    </div>
  );
}
