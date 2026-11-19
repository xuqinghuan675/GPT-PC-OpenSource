"""Optional native acceleration backends for GPT-PC.

This module is intentionally state-free: it accelerates file discovery and text
search while Runtime lifecycle, lease validation, locking and recovery remain in
Python. Missing, stale or checksum-mismatched helpers raise
``NativeAccelerationUnavailable`` so callers can fall back to the existing
Python implementation without changing MCP contracts.
"""

from __future__ import annotations

from dataclasses import dataclass
import fnmatch
import hashlib
import json
import os
from pathlib import Path
import subprocess
import threading
import time
from typing import Any


class NativeAccelerationUnavailable(RuntimeError):
    """The optional native fast path cannot be used safely for this request."""


@dataclass(frozen=True)
class NativeToolSpec:
    name: str
    version: str
    executable: str
    exe_sha256: str


EXPECTED_TOOLS: dict[str, NativeToolSpec] = {
    "ripgrep": NativeToolSpec(
        name="ripgrep",
        version="15.2.0",
        executable="rg.exe",
        exe_sha256="14231169855ec5205cf5a1b6f1db358ff4aed4247c86b69ce8aae647c77f6680",
    ),
    "fd": NativeToolSpec(
        name="fd",
        version="10.4.2",
        executable="fd.exe",
        exe_sha256="4c9d082ee20f0d9e44881ac4e92adf765efc314d82103c53d7f576bd78dc5761",
    ),
}

DEFAULT_IGNORED_DIRECTORY_NAMES = (
    ".git",
    ".mypy_cache",
    ".next",
    ".pytest_cache",
    ".ruff_cache",
    ".venv",
    "__pycache__",
    "build",
    "coverage",
    "dist",
    "node_modules",
)

_NATIVE_ROOT = Path(__file__).resolve().parent.parent / "runtime" / "native-tools"
_VERIFY_LOCK = threading.Lock()
_VERIFY_CACHE: dict[str, tuple[str, int, int]] = {}
_TREE_CLASSIFY_LOCK = threading.Lock()
_TREE_CLASSIFY_CACHE: dict[tuple[str, bool, int], tuple[float, bool]] = {}
TREE_CLASSIFY_CACHE_SECONDS = 5.0
DEFAULT_NATIVE_TREE_THRESHOLD = 256


def native_acceleration_enabled() -> bool:
    value = os.environ.get("GPT_PC_NATIVE_ACCELERATION", "1").strip().lower()
    return value not in {"0", "false", "off", "no"}


def _hash_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        while chunk := handle.read(1024 * 1024):
            digest.update(chunk)
    return digest.hexdigest()


def resolve_tool(name: str) -> Path:
    if not native_acceleration_enabled():
        raise NativeAccelerationUnavailable("native acceleration disabled")
    spec = EXPECTED_TOOLS.get(name)
    if spec is None:
        raise NativeAccelerationUnavailable(f"unknown native tool: {name}")
    path = _NATIVE_ROOT / spec.name / spec.version / spec.executable
    try:
        stat = path.stat()
    except OSError as exc:
        raise NativeAccelerationUnavailable(f"{name} helper missing") from exc
    cache_key = (str(path), stat.st_size, stat.st_mtime_ns)
    with _VERIFY_LOCK:
        if _VERIFY_CACHE.get(name) != cache_key:
            actual = _hash_file(path)
            if actual.lower() != spec.exe_sha256.lower():
                raise NativeAccelerationUnavailable(
                    f"{name} checksum mismatch: expected {spec.exe_sha256}, got {actual}"
                )
            _VERIFY_CACHE[name] = cache_key
    return path


def prefer_native_for_tree(
    base: Path,
    *,
    include_ignored: bool = False,
    threshold_entries: int = DEFAULT_NATIVE_TREE_THRESHOLD,
) -> bool:
    """Return True when process startup is likely cheaper than Python walking.

    The classifier stops as soon as a tree is clearly large. Small trees are
    fully counted once, then cached briefly. This affects backend selection
    only; it never supplies search results or freshness data.
    """
    if not native_acceleration_enabled() or not base.is_dir():
        return False
    threshold_entries = max(32, int(threshold_entries))
    key = (str(base.resolve(strict=False)).casefold(), bool(include_ignored), threshold_entries)
    now = time.monotonic()
    with _TREE_CLASSIFY_LOCK:
        cached = _TREE_CLASSIFY_CACHE.get(key)
        if cached and cached[0] >= now:
            return cached[1]

    count = 0
    try:
        for _root, dirnames, filenames in os.walk(base):
            if not include_ignored:
                dirnames[:] = [
                    name for name in dirnames
                    if name.casefold() not in DEFAULT_IGNORED_DIRECTORY_NAMES
                ]
            count += len(dirnames) + len(filenames)
            if count >= threshold_entries:
                result = True
                break
        else:
            result = False
    except OSError:
        result = False

    with _TREE_CLASSIFY_LOCK:
        _TREE_CLASSIFY_CACHE[key] = (now + TREE_CLASSIFY_CACHE_SECONDS, result)
    return result


def native_backend_status() -> dict[str, Any]:
    result: dict[str, Any] = {"enabled": native_acceleration_enabled(), "tools": {}}
    for name, spec in EXPECTED_TOOLS.items():
        try:
            path = resolve_tool(name)
            result["tools"][name] = {
                "available": True,
                "version": spec.version,
                "path": str(path),
            }
        except NativeAccelerationUnavailable as exc:
            result["tools"][name] = {
                "available": False,
                "version": spec.version,
                "reason": str(exc),
            }
    return result


def _run_utf8(args: list[str], *, timeout_seconds: float) -> subprocess.CompletedProcess[str]:
    try:
        return subprocess.run(
            args,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            encoding="utf-8",
            errors="replace",
            timeout=max(0.01, float(timeout_seconds)),
            check=False,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise NativeAccelerationUnavailable(str(exc)) from exc


def search_files_native(
    base: Path,
    *,
    pattern: str,
    recursive: bool,
    include_directories: bool,
    case_sensitive: bool,
    max_depth: int | None,
    max_results: int,
    timeout_seconds: float,
    include_ignored: bool,
) -> dict[str, Any]:
    """Discover matching paths through fd while preserving basename-glob semantics."""
    fd = resolve_tool("fd")
    if not base.is_dir():
        raise NativeAccelerationUnavailable("fd backend requires a directory root")

    effective_depth = max_depth if recursive else 0
    args = [
        str(fd),
        "--color",
        "never",
        "--absolute-path",
        "--hidden",
        "--glob",
        "--case-sensitive" if case_sensitive else "--ignore-case",
        "--type",
        "file",
    ]
    if include_directories:
        args.extend(["--type", "directory"])
    if effective_depth is not None:
        # Python _iter_walk(max_depth=N) evaluates entries in directories at
        # depth N, so returned children may be N+1 levels below the root.
        args.extend(["--max-depth", str(effective_depth + 1)])
    if include_ignored:
        args.extend(["--no-ignore"])
    else:
        for name in DEFAULT_IGNORED_DIRECTORY_NAMES:
            args.extend(["--exclude", name])
            args.extend(["--exclude", f"{name}/**"])
    # Ask for one extra result so the MCP layer can report truncation exactly.
    args.extend(["--max-results", str(max_results + 1), pattern, str(base)])
    proc = _run_utf8(args, timeout_seconds=timeout_seconds)
    if proc.returncode not in {0, 1}:
        raise NativeAccelerationUnavailable(proc.stderr.strip() or f"fd exit {proc.returncode}")

    paths: list[str] = []
    for raw in proc.stdout.splitlines():
        value = raw.strip()
        if not value:
            continue
        candidate = Path(value).resolve(strict=False)
        # fd glob semantics are close to fnmatch but we keep the old tool's
        # basename test as the final compatibility gate.
        name = candidate.name
        lhs = name if case_sensitive else name.lower()
        rhs = pattern if case_sensitive else pattern.lower()
        if not fnmatch.fnmatchcase(lhs, rhs):
            continue
        paths.append(str(candidate))
        if len(paths) > max_results:
            break
    truncated = len(paths) > max_results
    return {
        "backend": "fd",
        "paths": paths[:max_results],
        "truncated": truncated,
        "timed_out": False,
    }


def _json_text(value: Any) -> str:
    if not isinstance(value, dict):
        raise NativeAccelerationUnavailable("ripgrep JSON payload missing text wrapper")
    text = value.get("text")
    if isinstance(text, str):
        return text
    # A bytes-only JSON field means ripgrep could not represent the file as
    # Unicode. Let Python's existing encoding fallback handle that request.
    raise NativeAccelerationUnavailable("ripgrep returned non-text bytes")


def _strip_record_newline(text: str) -> str:
    if text.endswith("\r\n"):
        return text[:-2]
    if text.endswith("\n") or text.endswith("\r"):
        return text[:-1]
    return text


def _encoding_hint(path: Path) -> str:
    try:
        with path.open('rb') as handle:
            prefix = handle.read(4)
    except OSError:
        return "utf-8"
    if prefix.startswith(b"\xef\xbb\xbf"):
        return "utf-8-sig"
    if prefix.startswith(b"\xff\xfe") or prefix.startswith(b"\xfe\xff"):
        return "utf-16"
    return "utf-8"


def search_text_native(
    base: Path,
    *,
    query: str,
    regex: bool,
    file_pattern: str,
    case_sensitive: bool,
    context_lines: int,
    max_results: int,
    max_file_bytes: int,
    timeout_seconds: float,
    include_ignored: bool,
) -> dict[str, Any]:
    """Search with ripgrep JSON output and return the existing structured shape."""
    rg = resolve_tool("ripgrep")
    args = [
        str(rg),
        "--json",
        "--stats",
        "--line-number",
        "--color",
        "never",
        "--hidden",
        "--encoding",
        "auto",
        "--max-filesize",
        str(max_file_bytes),
        "--max-count",
        str(max_results + 1),
    ]
    if not regex:
        args.append("--fixed-strings")
    args.append("--case-sensitive" if case_sensitive else "--ignore-case")
    if context_lines:
        args.extend(["--context", str(context_lines)])
    if base.is_dir() and file_pattern != "*":
        args.extend(["--glob" if case_sensitive else "--iglob", file_pattern])
    if include_ignored:
        args.append("--no-ignore")
    else:
        for name in DEFAULT_IGNORED_DIRECTORY_NAMES:
            # ripgrep prunes an ignored directory only when the directory entry
            # itself is excluded; excluding only its descendants is not enough.
            args.extend(["--glob", f"!{name}"])
            args.extend(["--glob", f"!{name}/**"])
            args.extend(["--glob", f"!**/{name}"])
            args.extend(["--glob", f"!**/{name}/**"])
    args.extend(["--", query, str(base)])

    proc = _run_utf8(args, timeout_seconds=timeout_seconds)
    # ripgrep: 0 = matches, 1 = no matches, >1 = execution/regex error.
    if proc.returncode not in {0, 1}:
        raise NativeAccelerationUnavailable(proc.stderr.strip() or f"ripgrep exit {proc.returncode}")

    line_cache: dict[str, dict[int, str]] = {}
    match_order: list[tuple[str, int]] = []
    summary_stats: dict[str, Any] = {}
    for raw in proc.stdout.splitlines():
        if not raw:
            continue
        try:
            event = json.loads(raw)
        except json.JSONDecodeError as exc:
            raise NativeAccelerationUnavailable("invalid ripgrep JSON output") from exc
        kind = event.get("type")
        data = event.get("data") or {}
        if kind in {"match", "context"}:
            path_text = _json_text(data.get("path"))
            line_number = int(data.get("line_number") or 0)
            if line_number <= 0:
                raise NativeAccelerationUnavailable("ripgrep JSON line number missing")
            line_text = _strip_record_newline(_json_text(data.get("lines")))
            line_cache.setdefault(path_text, {})[line_number] = line_text
            if kind == "match":
                match_order.append((path_text, line_number))
        elif kind == "summary":
            stats = data.get("stats")
            if isinstance(stats, dict):
                summary_stats = stats

    total_matched_lines = int(summary_stats.get("matched_lines") or len(match_order))
    matches: list[dict[str, Any]] = []
    # ripgrep may traverse files on multiple threads. Stabilize ordering so the
    # same query returns deterministic path/line order across runs.
    ordered_match_order = sorted(match_order, key=lambda item: (item[0].casefold(), item[1]))
    for path_text, line_number in ordered_match_order[:max_results]:
        lines = line_cache.get(path_text, {})
        if context_lines:
            selected = [
                lines[number]
                for number in range(line_number - context_lines, line_number + context_lines + 1)
                if number in lines
            ]
            rendered = "\n".join(selected)
        else:
            rendered = lines.get(line_number, "")
        path = Path(path_text).resolve(strict=False)
        matches.append(
            {
                "path": str(path),
                "line": line_number,
                "encoding": _encoding_hint(path),
                "text": rendered,
            }
        )
    return {
        "backend": "ripgrep",
        "matches": matches,
        "count": len(matches),
        "scanned_files": int(summary_stats.get("searches") or 0),
        "truncated": total_matched_lines > max_results,
        "timed_out": False,
    }
