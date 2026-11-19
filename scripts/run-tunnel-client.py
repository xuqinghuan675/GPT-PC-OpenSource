"""Run tunnel-client and keep its combined output in a bounded rotating log."""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path


MAX_LOG_BYTES = 5 * 1024 * 1024
BACKUP_COUNT = 4


class RotatingTextLog:
    def __init__(self, path: Path) -> None:
        self.path = path
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.handle = None

    def __enter__(self) -> "RotatingTextLog":
        self.handle = self.path.open("a", encoding="utf-8", errors="replace", newline="")
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        if self.handle is not None:
            try:
                self.handle.flush()
            finally:
                self.handle.close()
                self.handle = None

    def _rotate(self, incoming_bytes: int) -> None:
        if self.handle is None:
            return
        self.handle.flush()
        if self.path.stat().st_size + incoming_bytes <= MAX_LOG_BYTES:
            return
        self.handle.close()
        self.handle = None
        oldest = self.path.with_name(self.path.name + f".{BACKUP_COUNT}")
        if oldest.exists():
            oldest.unlink()
        for index in range(BACKUP_COUNT - 1, 0, -1):
            source = self.path.with_name(self.path.name + f".{index}")
            destination = self.path.with_name(self.path.name + f".{index + 1}")
            if source.exists():
                source.replace(destination)
        if self.path.exists():
            self.path.replace(self.path.with_name(self.path.name + ".1"))
        self.handle = self.path.open("a", encoding="utf-8", errors="replace", newline="")

    def write(self, text: str) -> None:
        if self.handle is None:
            raise RuntimeError("log is not open")
        if not text.endswith("\n"):
            text += "\n"
        # A single pathological line must obey the same cap as ordinary logs.
        # Four bytes per Unicode code point is the UTF-8 upper bound.
        chunk_chars = max(1, min(16 * 1024, MAX_LOG_BYTES // 4))
        for start in range(0, len(text), chunk_chars):
            chunk = text[start : start + chunk_chars]
            self._rotate(len(chunk.encode("utf-8", errors="replace")))
            self.handle.write(chunk)
        self.handle.flush()


def main(argv: list[str]) -> int:
    if "--" not in argv:
        print("用法：run-tunnel-client.py --log-file 日志路径 -- 可执行文件 参数...", file=sys.stderr)
        return 2
    separator = argv.index("--")
    before = argv[:separator]
    command = argv[separator + 1 :]
    if not command:
        print("缺少隧道客户端命令", file=sys.stderr)
        return 2
    try:
        log_index = before.index("--log-file")
        log_path = Path(before[log_index + 1])
    except (ValueError, IndexError):
        print("缺少日志路径", file=sys.stderr)
        return 2

    creationflags = 0
    if os.name == "nt":
        creationflags = subprocess.CREATE_NEW_PROCESS_GROUP
    process = subprocess.Popen(
        command,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        encoding="utf-8",
        errors="replace",
        bufsize=1,
        creationflags=creationflags,
    )
    assert process.stdout is not None
    try:
        with RotatingTextLog(log_path) as log:
            for line in iter(lambda: process.stdout.readline(16 * 1024), ""):
                log.write(line)
        return process.wait()
    finally:
        # On a log/read exception, don't leave this wrapper's tunnel process
        # orphaned. Never find/kill unrelated processes or the whole process tree.
        try:
            if process.poll() is None:
                process.terminate()
                try:
                    process.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.wait(timeout=5)
        finally:
            process.stdout.close()


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
