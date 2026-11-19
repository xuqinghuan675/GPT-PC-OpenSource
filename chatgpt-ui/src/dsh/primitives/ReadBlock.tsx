import { useCallback, useMemo, useState } from "react";
import { FoldToggle } from "./FoldToggle.tsx";
import css from "./ReadBlock.module.css";
import { cx, writeClipboard } from "./utils.ts";

export const DEFAULT_READ_MAX_LINES = 16;

export interface ReadBlockLine {
  number: number;
  text: string;
}

export interface ReadBlockLabels {
  window: (shown: number, total: number) => string;
  copy: string;
  copied: string;
  collapseAria: string;
  expandAria: (hidden: number) => string;
  collapse: string;
  expand: (hidden: number) => string;
}

export interface ReadBlockProps {
  label?: string;
  lines: readonly ReadBlockLine[];
  labels: ReadBlockLabels;
  totalLines: number;
  lang?: string;
  maxLines?: number;
  className?: string;
  testId?: string;
}

// Adapted from DeepSeek Harness ui-primitives/ReadBlock.tsx. The Shiki grammar
// loader is intentionally removed: Phase 1.5 keeps the upstream banner,
// gutter, tokenized surfaces and folding with a plain monospace payload.
export function ReadBlock({ label, labels, lines, totalLines, lang, maxLines = DEFAULT_READ_MAX_LINES, className, testId }: ReadBlockProps) {
  const raw = useMemo(() => lines.map((line) => line.text).join("\n"), [lines]);
  const [expanded, setExpanded] = useState(false);
  const [copied, setCopied] = useState(false);
  const hidden = lines.length - maxLines;
  const capped = hidden > 0 && !expanded;
  const headLines = Math.ceil(maxLines / 2);
  const tailLines = maxLines - headLines;
  const windowed = lines.length < totalLines;
  const onCopy = useCallback(() => {
    if (copied) return;
    void writeClipboard(raw).then((ok) => {
      if (!ok) return;
      setCopied(true);
      window.setTimeout(() => setCopied(false), 1000);
    });
  }, [copied, raw]);

  const renderRows = (slice: readonly ReadBlockLine[]) => slice.map((line) => (
    <div key={line.number} className={css.line}>
      <span className={css.gutter} aria-hidden>{line.number}</span>
      <span className={css.content}>{line.text}</span>
    </div>
  ));

  return (
    <div className={cx(css.block, className)} data-read="" data-testid={testId ?? "read-block"}>
      <div className={css.banner}>
        <div className={css.label}>{label ?? ""}</div>
        <div className={css.action}>
          {windowed && <span className={css.count}>{labels.window(lines.length, totalLines)}</span>}
          <span className={css.lang}>{lang ?? ""}</span>
          {lines.length > 0 && <button type="button" className={css.copyButton} onClick={onCopy}>{copied ? labels.copied : labels.copy}</button>}
        </div>
      </div>
      <div className={css.body}>
        {renderRows(capped ? lines.slice(0, headLines) : lines)}
        {hidden > 0 && (
          <FoldToggle
            className={css.expand}
            testId="read-fold-toggle"
            expanded={expanded}
            hidden={hidden}
            labels={labels}
            onToggle={() => setExpanded((value) => !value)}
          />
        )}
        {capped && renderRows(lines.slice(lines.length - tailLines))}
      </div>
    </div>
  );
}
