export function cx(...values: Array<string | false | null | undefined>): string {
  return values.filter(Boolean).join(" ");
}

export function contentLines(text: string): string[] {
  if (text === "") return [];
  const body = text.endsWith("\n") ? text.slice(0, -1) : text;
  return body.split("\n");
}

export function headTailCap(total: number, maxLines: number, expanded: boolean) {
  const hidden = Math.max(0, total - maxLines);
  const capped = hidden > 0 && !expanded;
  const headLines = Math.ceil(maxLines / 2);
  const tailLines = maxLines - headLines;
  return { hidden, capped, headLines, tailLines };
}

export async function writeClipboard(text: string): Promise<boolean> {
  try {
    if (!navigator.clipboard) return false;
    await navigator.clipboard.writeText(text);
    return true;
  } catch {
    return false;
  }
}
