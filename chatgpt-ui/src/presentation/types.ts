import type { VisualObservation } from "../visual-model.ts";
import type { ProgressPhase, UserAttachment } from "../event-model.ts";
import type { DiffHunk } from "../dsh/primitives/DiffBlock.tsx";
import type { ReadBlockLine } from "../dsh/primitives/ReadBlock.tsx";
import type { SearchFileGroup } from "../dsh/primitives/SearchBlock.tsx";

export type ToolKind = "read" | "edit" | "delete" | "move" | "search" | "execute" | "fetch" | "other";
export type CardKind = "generic" | "terminal" | "diff" | "read" | "search";
export type PresentationStatus = "running" | "ok" | "error" | "stopped";

export type DshPresentationItem =
  | { kind: "observation"; id: string; observation: VisualObservation }
  | { kind: "user"; id: string; text: string; attachments: UserAttachment[] }
  | { kind: "reasoning"; id: string; text: string; running: boolean; phase?: ProgressPhase }
  | { kind: "assistant"; id: string; text: string }
  | DshToolPresentation;

export interface DshToolPresentation {
  kind: "tool";
  id: string;
  toolKind: ToolKind;
  card: CardKind;
  status: PresentationStatus;
  title: string;
  summary: string;
  server?: string;
  tool?: string;
  durationMs?: number | null;
  input?: string;
  output?: string;
  terminal?: {
    command: string;
    cwd?: string;
    output: string;
    exitCode?: number;
    running: boolean;
    stopped?: boolean;
  };
  diff?: { diffs: DiffHunk[] };
  read?: {
    label: string;
    lines: ReadBlockLine[];
    totalLines: number;
    lang?: string;
  };
  search?:
    | { kind: "paths"; paths: string[]; total: number; truncated: boolean }
    | { kind: "matches"; files: SearchFileGroup[]; total: number; truncated: boolean };
}
