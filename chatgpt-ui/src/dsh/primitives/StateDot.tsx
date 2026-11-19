import css from "./StateDot.module.css";
import { cx } from "./utils.ts";

export type StateDotState = "done" | "warning" | "ongoing" | "error";

const MATRIX_CELLS: readonly (readonly [number, number])[] = [
  [0, 0], [4, 0], [8, 0], [8, 4], [8, 8], [4, 8], [0, 8], [0, 4],
];

// Adapted from DeepSeek Harness ui-primitives/StateDot.tsx.
export function StateDot({ state, size = 10, className }: {
  state: StateDotState;
  size?: number;
  className?: string;
}) {
  if (state === "ongoing") {
    return (
      <svg
        className={cx(css.matrix, className)}
        data-state="ongoing"
        width={size}
        height={size}
        viewBox="0 0 10 10"
        shapeRendering="crispEdges"
        aria-hidden="true"
      >
        {MATRIX_CELLS.map(([x, y], index) => (
          <rect
            key={`${x}-${y}`}
            className={css.cell}
            x={x}
            y={y}
            width="2"
            height="2"
            style={{ animationDelay: `${(index - MATRIX_CELLS.length) * 125}ms` }}
          />
        ))}
      </svg>
    );
  }
  return (
    <span
      className={cx(css.dot, className)}
      data-state={state}
      style={{ width: size, height: size }}
      aria-hidden="true"
    />
  );
}
