import { useCallback, useMemo, useState } from "react";
import { Pill } from "./Pill.tsx";
import { keyedLines } from "./keyed-lines.ts";
import { StateDot, type StateDotState } from "./StateDot.tsx";
import css from "./TerminalBlock.module.css";
import { contentLines, cx, headTailCap, writeClipboard } from "./utils.ts";

export const DEFAULT_TERMINAL_MAX_LINES = 16;

export interface TerminalBlockLabels {
  signal: (signal: string) => string;
  exitCode: (exitCode: number) => string;
  running: string;
  stopped: string;
  failed: string;
  done: string;
  copy: string;
  copied: string;
  noOutput: string;
  collapseAria: string;
  collapse: string;
  expandAria: (hidden: number) => string;
  expand: (hidden: number) => string;
}

export interface TerminalBlockProps {
  command: string;
  cwd?: string;
  home?: string;
  output?: string;
  exitCode?: number;
  signal?: string;
  running?: boolean;
  stopped?: boolean;
  maxLines?: number;
  className?: string;
  labels: TerminalBlockLabels;
  testId?: string;
}

function promptLabel(cwd: string, home?: string): string {
  const trimmed = cwd.replace(/[/\\]+$/, "");
  if (home !== undefined && trimmed === home.replace(/[/\\]+$/, "")) return "~";
  const segment = trimmed.split(/[/\\]/).pop();
  return segment || cwd;
}

function statusText(exitCode: number | undefined, signal: string | undefined, labels: TerminalBlockLabels) {
  if (signal !== undefined) return labels.signal(signal);
  if (exitCode !== undefined && exitCode !== 0) return labels.exitCode(exitCode);
  return undefined;
}

function runState(running: boolean, stopped: boolean, exitCode: number | undefined, signal: string | undefined, labels: TerminalBlockLabels): { state: StateDotState; label: string } {
  if (running) return { state: "ongoing", label: labels.running };
  if (stopped) return { state: "warning", label: labels.stopped };
  if (statusText(exitCode, signal, labels) !== undefined) return { state: "error", label: labels.failed };
  return { state: "done", label: labels.done };
}

// Adapted from DeepSeek Harness ui-primitives/TerminalBlock.tsx. ANSI parsing
// is deliberately omitted for Phase 1.5; command/output geometry and folding
// are the upstream presentation with plain text payloads.
export function TerminalBlock({
  command, cwd, home, output = "", exitCode, signal, running = false, stopped = false,
  maxLines = DEFAULT_TERMINAL_MAX_LINES, className, labels, testId,
}: TerminalBlockProps) {
  const lines = useMemo(() => keyedLines(contentLines(output)), [output]);
  const commandLines = useMemo(() => contentLines(command), [command]);
  const commands = keyedLines(commandLines.length === 0 ? [""] : commandLines);
  const empty = lines.every((line) => line.text.trim() === "");
  const [expanded, setExpanded] = useState(false);
  const [copied, setCopied] = useState(false);
  const status = statusText(exitCode, signal, labels);
  const state = runState(running, stopped, exitCode, signal, labels);
  const { hidden, capped, headLines, tailLines } = headTailCap(lines.length, maxLines, expanded);
  const onCopy = useCallback(() => {
    if (copied) return;
    void writeClipboard(output).then((ok) => {
      if (!ok) return;
      setCopied(true);
      window.setTimeout(() => setCopied(false), 1000);
    });
  }, [copied, output]);

  return (
    <div
      className={cx(css.block, className)}
      data-terminal=""
      data-running={running ? "" : undefined}
      data-stopped={stopped ? "" : undefined}
      data-testid={testId ?? "terminal-block"}
    >
      <div className={css.header}>
        <div className={css.prompt}>
          <span className={css.runStateLabel}>{state.label}</span>
          {commands.map((line, index) => (
            <div key={line.id} className={css.promptLine}>
              {index === 0 && <StateDot state={state.state} className={css.runState} />}
              <span className={css.cwd}>{index > 0 || cwd === undefined ? "$" : promptLabel(cwd, home)}</span>
              <span className={css.command}>{line.text}</span>
            </div>
          ))}
        </div>
        {status !== undefined && <Pill className={css.status}>{status}</Pill>}
        {!running && !empty && <button type="button" className={css.copyButton} onClick={onCopy}>{copied ? labels.copied : labels.copy}</button>}
      </div>
      {!running && (empty
        ? <div className={css.empty}>{labels.noOutput}</div>
        : (
          <div className={css.output}>
            {(capped ? lines.slice(0, headLines) : lines).map((line) => <div key={line.id} className={css.line}>{line.text}</div>)}
            {hidden > 0 && (
              <button
                type="button"
                className={css.expand}
                aria-expanded={expanded}
                aria-label={expanded ? labels.collapseAria : labels.expandAria(hidden)}
                onClick={() => setExpanded((value) => !value)}
              >
                {expanded ? labels.collapse : labels.expand(hidden)}
              </button>
            )}
            {capped && lines.slice(lines.length - tailLines).map((line) => <div key={line.id} className={css.line}>{line.text}</div>)}
          </div>
        ))}
    </div>
  );
}
