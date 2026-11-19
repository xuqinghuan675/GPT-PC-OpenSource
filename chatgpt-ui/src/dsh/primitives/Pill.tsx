import type { ButtonHTMLAttributes, ReactNode } from "react";
import css from "./Pill.module.css";
import { cx } from "./utils.ts";

// Adapted from DeepSeek Harness ui-primitives/Pill.tsx.
export function Pill({ active = false, className, children, onClick, ...rest }: {
  active?: boolean;
  className?: string;
  children?: ReactNode;
} & ButtonHTMLAttributes<HTMLButtonElement>) {
  if (!onClick) {
    return <span className={cx(css.pill, active && css.active, className)}>{children}</span>;
  }
  return (
    <button
      type="button"
      className={cx(css.pill, css.interactive, active && css.active, className)}
      onClick={onClick}
      {...rest}
    >
      {children}
    </button>
  );
}
