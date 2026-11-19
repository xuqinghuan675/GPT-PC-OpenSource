import type { ProgressPhase } from "../../event-model.ts";
import { IconThinkOutline14 } from "../primitives/icons.tsx";
import css from "./ReasoningRow.module.css";

function phaseLabel(phase: ProgressPhase): string {
  switch (phase) {
    case "plan": return "计划";
    case "decision": return "判断";
    case "progress": return "进展";
    case "verification": return "验证";
    case "warning": return "注意";
    case "done": return "完成";
  }
}

export function ReasoningRow({
  id,
  text,
  running,
  phase = "progress",
}: {
  id: string;
  text: string;
  running: boolean;
  phase?: ProgressPhase;
}) {
  return (
    <div
      className={css.root}
      data-agent-item={id}
      data-testid="reasoning-summary"
      data-variant="think"
      data-phase={phase}
      data-state={running ? "running" : "ok"}
    >
      <div className={css.meta} title="Agent 主动上报的任务进展、判断与验证状态">
        <IconThinkOutline14 size={14} />
        <span>执行说明</span>
        <span aria-hidden>·</span>
        <span className={css.phase}>{phaseLabel(phase)}</span>
        {running && <span className={css.live}>进行中</span>}
      </div>
      <div className={css.body}>{text}</div>
    </div>
  );
}
