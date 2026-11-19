// Content-based identities keep repeated read-only log/diff lines distinct
// without making their React identity depend on a changing head/tail slice.
export interface KeyedLine { id: string; text: string }

export function keyedLines(lines: string[]): KeyedLine[] {
  const occurrences = new Map<string, number>();
  return lines.map((text) => {
    const occurrence = occurrences.get(text) ?? 0;
    occurrences.set(text, occurrence + 1);
    return { id: `${text.length}:${text}:${occurrence}`, text };
  });
}
