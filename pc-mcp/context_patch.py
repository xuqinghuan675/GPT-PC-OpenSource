"""Exact, single-file context patches for the web ChatGPT plugin workflow."""
from __future__ import annotations

from pathlib import Path
import re


def apply_context_patch(original: str, patch: str, target: Path) -> tuple[str, int]:
    lines = patch.strip().splitlines()
    if len(lines) < 4 or lines[0] != "*** Begin Patch" or lines[-1] != "*** End Patch":
        raise ValueError("上下文补丁需要完整的 Begin Patch / End Patch 标记")
    if not lines[1].startswith("*** Update File: "):
        raise ValueError("这里只支持 Update File；新建、删除请用对应工具")
    header = lines[1].removeprefix("*** Update File: ").replace("\\", "/")
    if ".." in header.split("/") or not header:
        raise ValueError("补丁文件路径无效")
    normalized = target.resolve(strict=False).as_posix().casefold()
    if normalized != header.casefold() and not normalized.endswith("/" + header.casefold().removeprefix("./")):
        raise ValueError("补丁声明的文件与 path 不一致")

    hunks: list[tuple[str, list[tuple[str, str]], bool]] = []
    scope = ""
    body: list[tuple[str, str]] | None = None
    at_eof = False
    for line in lines[2:-1]:
        if line == "@@" or line.startswith("@@ "):
            if body is not None:
                hunks.append((scope, body, at_eof))
            scope, body, at_eof = line[2:].strip(), [], False
        elif line == "*** End of File" and body is not None:
            at_eof = True
        elif body is not None and not at_eof and (not line or line[0] in " +-"):
            body.append((line[0], line[1:]) if line else (" ", ""))
        else:
            raise ValueError("补丁区块无效；每次只更新 path 指定的一个文件")
    if body is not None:
        hunks.append((scope, body, at_eof))
    if not hunks:
        raise ValueError("补丁缺少 @@ 上下文区块")

    source = original.splitlines(keepends=True)
    values = [line.rstrip("\r\n") for line in source]
    ending = re.search(r"\r\n|\n|\r", original)
    newline = ending.group() if ending else "\n"
    cursor = 0
    output: list[str] = []
    additions = 0
    for scope, body, at_eof in hunks:
        if not body or not any(op in "+-" for op, _ in body):
            raise ValueError("补丁区块没有修改")
        lower = cursor
        if scope:
            anchors = [i for i in range(cursor, len(values)) if values[i].strip() == scope]
            if len(anchors) != 1:
                raise ValueError("补丁范围标记不唯一或不存在，请提供完整上下文")
            lower = anchors[0]
        before = [value for op, value in body if op != "+"]
        if not before:
            if source:
                raise ValueError("插入代码需要相邻原文作为定位依据")
            matches = [0]
        else:
            matches = []
            for i in range(lower, len(values) - len(before) + 1):
                if values[i] == before[0] and values[i:i + len(before)] == before:
                    if not at_eof or i + len(before) == len(values):
                        matches.append(i)
                        if len(matches) == 2:
                            break
        if len(matches) != 1:
            raise ValueError("补丁上下文不唯一或已经变化；请补充上下文后重试，不会猜测位置")
        start = matches[0]
        output.extend(source[cursor:start])
        cursor = start
        for op, value in body:
            if op == " ":
                output.append(source[cursor])
                cursor += 1
            elif op == "-":
                cursor += 1
            else:
                # When inserting after an unterminated last line, separate it
                # from the new line without changing any other line endings.
                if output and not output[-1].endswith(("\r", "\n")):
                    output[-1] += newline
                output.append(value + newline)
                additions += 1
    output.extend(source[cursor:])
    result = "".join(output)
    if original and not original.endswith(("\r", "\n")) and result.endswith(newline):
        result = result[:-len(newline)]
    return result, additions
