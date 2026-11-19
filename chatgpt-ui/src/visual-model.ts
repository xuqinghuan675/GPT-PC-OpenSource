// Task-level visual and goal context is deliberately separate from user messages.
export interface VisualAsset {
  id: string;
  kind: "image" | "text";
  name: string;
  label?: string;
  text?: string;
  mime?: string;
  size?: number;
  width?: number;
  height?: number;
}

export interface VisualObservation extends VisualAsset {
  kind: "image";
  iteration: number;
  capture_type?: string;
  inspected_at?: string;
}

export type VisualDepth = "visual" | "interactive" | "frontend";

export interface FrontendJourney {
  id: string;
  name: string;
  required: boolean;
}

export interface GoalCorrection {
  revision: number;
  at?: string;
  source: string;
  fields: string[];
  note: string;
}

export interface GoalMode {
  active: boolean;
  status: string;
  outcome: string;
  success_criteria: string[];
  requirements: string[];
  corrections: GoalCorrection[];
  revision: number;
  reuse_visual_context: boolean;
  updated_at?: string;
}

export interface UltraMode {
  active: boolean;
  status: string;
  revision: number;
  source: string;
  updated_at?: string;
  enabled_at?: string;
  disabled_at?: string;
  user_mode?: "enabled" | "disabled";
}

export interface VisualContext {
  active: boolean;
  status: string;
  until_at?: string;
  updated_at?: string;
  user_mode?: "enabled" | "disabled";
  user_mode_updated_at?: string;
  references: VisualAsset[];
  criteria: string[];
  depth: VisualDepth;
  journeys: FrontendJourney[];
  workspace_revision: number;
  iteration: number;
  latest_observation_id?: string;
  latest_review?: Record<string, unknown>;
  goal?: GoalMode;
}

function record(value: unknown): Record<string, unknown> | undefined {
  return value !== null && typeof value === "object" && !Array.isArray(value) ? value as Record<string, unknown> : undefined;
}
function text(value: unknown, limit = 240): string | undefined {
  return typeof value === "string" ? value.slice(0, limit) : undefined;
}
function number(value: unknown): number | undefined {
  return typeof value === "number" && Number.isFinite(value) && value >= 0 ? value : undefined;
}

export function parseGoalMode(value: unknown): GoalMode | undefined {
  const raw = record(value);
  if (!raw) return undefined;
  const criteria = Array.isArray(raw.success_criteria)
    ? raw.success_criteria.filter((item): item is string => typeof item === "string").slice(0, 64).map((item) => item.slice(0, 2000))
    : [];
  const requirements = Array.isArray(raw.requirements)
    ? raw.requirements.filter((item): item is string => typeof item === "string").slice(0, 64).map((item) => item.slice(0, 2000))
    : [];
  const corrections = Array.isArray(raw.corrections) ? raw.corrections.slice(-32).flatMap((entry) => {
    const item = record(entry);
    if (!item) return [];
    const fields = Array.isArray(item.fields) ? item.fields.filter((field): field is string => typeof field === "string").slice(0, 16) : [];
    return [{ revision: number(item.revision) ?? 0, at: text(item.at, 80), source: text(item.source, 32) ?? "agent",
      fields, note: text(item.note, 2000) ?? "更新 Goal" } satisfies GoalCorrection];
  }) : [];
  return {
    active: raw.active === true,
    status: text(raw.status, 40) ?? "off",
    outcome: text(raw.outcome, 8000) ?? "",
    success_criteria: criteria,
    requirements,
    corrections,
    revision: number(raw.revision) ?? 0,
    reuse_visual_context: raw.reuse_visual_context !== false,
    updated_at: text(raw.updated_at, 80),
  };
}

export function parseUltraMode(value: unknown): UltraMode | undefined {
  const raw = record(value);
  if (!raw) return undefined;
  const userMode = raw.user_mode === "enabled" || raw.user_mode === "disabled" ? raw.user_mode : undefined;
  return {
    active: raw.active === true,
    status: text(raw.status, 40) ?? (raw.active === true ? "on" : "off"),
    revision: number(raw.revision) ?? 0,
    source: text(raw.source, 32) ?? "default",
    updated_at: text(raw.updated_at, 80),
    enabled_at: text(raw.enabled_at, 80),
    disabled_at: text(raw.disabled_at, 80),
    user_mode: userMode,
  };
}

export function parseVisualAsset(value: unknown): VisualAsset | undefined {
  const raw = record(value);
  const id = text(raw?.id, 160);
  if (!raw || !id || !/^(ref|obs)-[a-zA-Z0-9_-]+$/.test(id)) return undefined;
  const kind = raw.kind === "text" ? "text" : "image";
  return { id, kind, name: text(raw.name) ?? text(raw.label) ?? id,
    label: text(raw.label), text: kind === "text" ? text(raw.text, 8000) : undefined,
    mime: text(raw.mime, 160), size: number(raw.size), width: number(raw.width), height: number(raw.height) };
}

export function parseVisualObservation(value: unknown): VisualObservation | undefined {
  const asset = parseVisualAsset(value);
  const raw = record(value);
  if (asset?.kind !== "image" || !asset.id.startsWith("obs-")) return undefined;
  return { ...asset, kind: "image", iteration: number(raw?.iteration) ?? 0,
    capture_type: text(raw?.capture_type, 80), inspected_at: text(raw?.inspected_at, 80) };
}

export function parseVisualContext(value: unknown): VisualContext | undefined {
  const raw = record(value);
  if (!raw) return undefined;
  const references = Array.isArray(raw.references)
    ? raw.references.slice(0, 16).flatMap((item) => { const asset = parseVisualAsset(item); return asset ? [asset] : []; }) : [];
  const criteria = Array.isArray(raw.criteria) ? raw.criteria.filter((item): item is string => typeof item === "string").slice(0, 64).map((item) => item.slice(0, 2000)) : [];
  const userMode = raw.user_mode === "enabled" || raw.user_mode === "disabled" ? raw.user_mode : undefined;
  const depth: VisualDepth = raw.depth === "interactive" || raw.depth === "frontend" ? raw.depth : "visual";
  const journeys = Array.isArray(raw.journeys) ? raw.journeys.slice(0, 12).flatMap((item) => {
    const journey = record(item); const id = text(journey?.id, 80); const name = text(journey?.name, 160);
    return id ? [{ id, name: name ?? id, required: journey?.required !== false }] : [];
  }) : [];
  return { active: raw.active === true, status: text(raw.status, 40) ?? "off",
    until_at: text(raw.until_at, 80), updated_at: text(raw.updated_at, 80),
    user_mode: userMode, user_mode_updated_at: text(raw.user_mode_updated_at, 80), references, criteria,
    depth, journeys, workspace_revision: number(raw.workspace_revision) ?? 0,
    iteration: number(raw.iteration) ?? 0, latest_observation_id: text(raw.latest_observation_id, 160),
    latest_review: record(raw.latest_review), goal: parseGoalMode(raw.goal) };
}
