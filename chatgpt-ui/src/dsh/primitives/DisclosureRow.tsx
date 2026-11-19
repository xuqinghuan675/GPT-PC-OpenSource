import type { MouseEvent, ReactNode } from "react";
import { IconChevronDownOutline14 } from "./icons.tsx";
import css from "./DisclosureRow.module.css";
import { cx } from "./utils.ts";

export interface DisclosureRowProps {
  icon: ReactNode;
  title: string;
  open: boolean;
  expandable: boolean;
  onToggle: () => void;
  expandOnRowClick?: boolean;
  previewChevron?: boolean;
  keepContentWhenOpen?: boolean;
  collapsedContent?: ReactNode;
  children?: ReactNode;
  className?: string;
  rowClassName?: string;
  leadingClassName?: string;
  chevronClassName?: string;
  titleClassName?: string;
}

// Adapted from DeepSeek Harness ui-primitives/DisclosureRow.tsx.
export function DisclosureRow({
  icon, title, open, expandable, onToggle, expandOnRowClick = false,
  previewChevron = expandable, keepContentWhenOpen = false, collapsedContent,
  children, className, rowClassName, leadingClassName, chevronClassName, titleClassName,
}: DisclosureRowProps) {
  const rowExpands = expandable && expandOnRowClick;
  const toggleFromLeading = (event: MouseEvent<HTMLButtonElement>) => { event.stopPropagation(); onToggle(); };
  const collapsedLeading = previewChevron ? (
    <><span className={css.iconIdle}>{icon}</span><IconChevronDownOutline14 className={cx(chevronClassName, css.chevronHover)} /></>
  ) : icon;
  const leading = open ? <IconChevronDownOutline14 className={chevronClassName} /> : collapsedLeading;
  const rowContent = <>
    {expandable && !rowExpands ? (
      <button type="button" className={cx(css.leading, leadingClassName)} aria-expanded={open} aria-label={title} onClick={toggleFromLeading}>{leading}</button>
    ) : <span className={cx(css.leading, leadingClassName)}>{leading}</span>}
    <span className={cx(css.title, titleClassName)}>{title}</span>
    {(keepContentWhenOpen || !open) && collapsedContent}
  </>;
  return (
    <div className={cx(css.root, className)} data-open={open || undefined}>
      {rowExpands ? (
        <button type="button" className={cx(css.row, rowClassName)} data-disclosure-row data-expandable
          aria-expanded={open} onClick={onToggle}>{rowContent}</button>
      ) : (
        <div className={cx(css.row, rowClassName)} data-disclosure-row>{rowContent}</div>
      )}
      {open && children}
    </div>
  );
}
