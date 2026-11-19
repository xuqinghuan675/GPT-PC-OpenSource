interface FoldToggleProps {
  className?: string;
  testId?: string;
  expanded: boolean;
  hidden: number;
  labels: {
    collapseAria: string;
    expandAria: (hidden: number) => string;
    collapse: string;
    expand: (hidden: number) => string;
  };
  onToggle: () => void;
}

// Adapted from DeepSeek Harness ui-primitives/FoldToggle.tsx.
export function FoldToggle({ className, testId, expanded, hidden, labels, onToggle }: FoldToggleProps) {
  return (
    <button
      type="button"
      className={className}
      data-testid={testId}
      aria-expanded={expanded}
      aria-label={expanded ? labels.collapseAria : labels.expandAria(hidden)}
      onClick={onToggle}
    >
      {expanded ? labels.collapse : labels.expand(hidden)}
    </button>
  );
}
