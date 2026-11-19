"""Thin code-navigation bridge for GPT-PC.

The graph is a navigation hint only. Git and the working tree remain the source
of truth, especially when the repository is dirty or the graph is stale.
"""

from __future__ import annotations

import json
import os
import re
from pathlib import Path
import subprocess
import threading
import time
from typing import Any, Literal

import native_acceleration as native_backend


_CODEBASE_MEMORY_EXE = os.environ.get("GPT_PC_CODEBASE_MEMORY_EXE", "").strip()
CODEBASE_MEMORY_EXE = Path(_CODEBASE_MEMORY_EXE).expanduser() if _CODEBASE_MEMORY_EXE else None
PROJECT_CACHE_SECONDS = 300.0
MAX_SOURCE_FILE_BYTES = 2 * 1024 * 1024
NOISE_PARTS = frozenset(
    {
        ".git",
        ".local",
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
    }
)
CODE_SOURCE_SUFFIXES = frozenset(
    {
        ".c", ".cc", ".cpp", ".cs", ".go", ".h", ".hpp", ".java", ".js", ".jsx",
        ".kt", ".kts", ".mjs", ".php", ".ps1", ".py", ".rb", ".rs", ".sh", ".sql",
        ".svelte", ".swift", ".ts", ".tsx", ".vue",
    }
)
VALID_INTENTS = frozenset({"symbol", "callers", "callees", "impact", "architecture", "path"})

_PROJECT_CACHE_LOCK = threading.Lock()
_PROJECT_CACHE_AT = 0.0
_PROJECT_CACHE: list[dict[str, Any]] = []


def _run(
    argv: list[str],
    *,
    cwd: Path,
    timeout_seconds: float,
) -> subprocess.CompletedProcess[str] | None:
    creationflags = subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0
    try:
        return subprocess.run(
            argv,
            cwd=str(cwd),
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=timeout_seconds,
            check=False,
            creationflags=creationflags,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None


def _run_cli(tool: str, args: dict[str, Any], *, cwd: Path, timeout_seconds: float = 15.0) -> dict[str, Any] | None:
    if CODEBASE_MEMORY_EXE is None or not CODEBASE_MEMORY_EXE.is_file():
        return None
    result = _run(
        [str(CODEBASE_MEMORY_EXE), "cli", tool, json.dumps(args, ensure_ascii=False, separators=(",", ":"))],
        cwd=cwd,
        timeout_seconds=timeout_seconds,
    )
    if result is None or result.returncode != 0:
        return None
    text = result.stdout.strip()
    if not text:
        return None
    try:
        parsed = json.loads(text)
    except json.JSONDecodeError:
        return None
    return parsed if isinstance(parsed, dict) else None


def _normalise_for_compare(value: str | Path) -> str:
    return os.path.normcase(os.path.abspath(os.fspath(value))).rstrip("\\/")


def _git_text(repo: Path, args: list[str], *, timeout_seconds: float = 8.0) -> str | None:
    result = _run(["git", *args], cwd=repo, timeout_seconds=timeout_seconds)
    if result is None or result.returncode != 0:
        return None
    return result.stdout.strip()


def _parse_changed_files(status_text: str) -> list[str]:
    changed: list[str] = []
    seen: set[str] = set()
    for raw_line in status_text.splitlines():
        if len(raw_line) < 4:
            continue
        path_text = raw_line[3:].strip()
        if " -> " in path_text:
            path_text = path_text.split(" -> ", 1)[1].strip()
        path_text = path_text.strip('"').replace("\\", "/")
        if path_text and path_text not in seen:
            seen.add(path_text)
            changed.append(path_text)
    return changed


def _git_state(repo_path: Path) -> dict[str, Any]:
    # One rev-parse process yields root, HEAD and branch. The previous four-Git
    # setup made simple symbol navigation pay process startup overhead before the
    # source fast path could even run.
    identity = _git_text(
        repo_path,
        ["rev-parse", "--show-toplevel", "HEAD", "--abbrev-ref", "HEAD"],
    )
    identity_lines = identity.splitlines() if identity else []
    if len(identity_lines) < 3:
        return {
            "is_git": False,
            "root": repo_path.resolve(strict=False),
            "head": None,
            "branch": None,
            "dirty": None,
            "changed_files": [],
        }
    root = Path(identity_lines[0]).resolve(strict=False)
    head = identity_lines[1].strip() or None
    branch = identity_lines[2].strip() or None
    status = _git_text(
        root,
        ["-c", "core.quotepath=false", "status", "--porcelain=v1", "--untracked-files=normal"],
    )
    changed_files = _parse_changed_files(status or "")
    return {
        "is_git": True,
        "root": root,
        "head": head,
        "branch": branch or None,
        "dirty": bool(changed_files),
        "changed_files": changed_files,
    }


def _list_projects(cwd: Path) -> list[dict[str, Any]]:
    global _PROJECT_CACHE_AT, _PROJECT_CACHE
    now = time.monotonic()
    with _PROJECT_CACHE_LOCK:
        if _PROJECT_CACHE and now - _PROJECT_CACHE_AT <= PROJECT_CACHE_SECONDS:
            return [dict(item) for item in _PROJECT_CACHE]
        payload = _run_cli("list_projects", {}, cwd=cwd, timeout_seconds=12.0)
        projects = payload.get("projects", []) if payload else []
        clean = [dict(item) for item in projects if isinstance(item, dict)]
        _PROJECT_CACHE = clean
        _PROJECT_CACHE_AT = now
        return [dict(item) for item in clean]


def _select_project(repo_state: dict[str, Any]) -> dict[str, Any] | None:
    root = Path(repo_state["root"])
    candidates = [
        item
        for item in _list_projects(root)
        if item.get("root_path")
        and _normalise_for_compare(str(item["root_path"])) == _normalise_for_compare(root)
    ]
    if not candidates:
        return None
    head = str(repo_state.get("head") or "")
    branch = str(repo_state.get("branch") or "")

    def score(item: dict[str, Any]) -> tuple[int, int, int, int]:
        git = item.get("git") if isinstance(item.get("git"), dict) else {}
        return (
            int(bool(head and str(git.get("head_sha") or "") == head)),
            int(bool(branch and str(git.get("branch") or "") == branch)),
            int(bool(git.get("root_exists"))),
            int(item.get("nodes") or 0),
        )

    return max(candidates, key=score)


def _freshness(repo_state: dict[str, Any], project: dict[str, Any] | None, index_status: dict[str, Any] | None) -> str:
    if not repo_state.get("is_git") or project is None or index_status is None:
        return "stale"
    if str(index_status.get("status") or "") != "ready":
        return "stale"
    graph_root = index_status.get("root_path")
    graph_git = index_status.get("git") if isinstance(index_status.get("git"), dict) else {}
    if not graph_root or _normalise_for_compare(str(graph_root)) != _normalise_for_compare(repo_state["root"]):
        return "stale"
    if str(graph_git.get("head_sha") or "") != str(repo_state.get("head") or ""):
        return "stale"
    graph_branch = str(graph_git.get("branch") or "")
    repo_branch = str(repo_state.get("branch") or "")
    if graph_branch != repo_branch:
        return "stale"
    return "dirty" if repo_state.get("dirty") else "fresh"


def _is_noise_path(path_text: str) -> bool:
    parts = [part.casefold() for part in Path(path_text.replace("/", os.sep)).parts]
    return any(part in NOISE_PARTS or part.startswith(".next") for part in parts)


def _is_code_path(path_text: str) -> bool:
    return Path(path_text).suffix.casefold() in CODE_SOURCE_SUFFIXES


def _decode_source(data: bytes) -> str | None:
    if b"\x00" in data[:4096]:
        return None
    for encoding in ("utf-8-sig", "utf-8", "gb18030"):
        try:
            return data.decode(encoding)
        except UnicodeDecodeError:
            continue
    return None


def _source_match_result(relative_path: str, line_number: int, line_text: str, query: str) -> dict[str, Any]:
    stripped = line_text.strip()
    kind = "source_match"
    symbol = query
    lowered = stripped.casefold()
    if " class " in f" {lowered} " or lowered.startswith("class "):
        kind = "class"
    elif " def " in f" {lowered} " or lowered.startswith(("def ", "async def ")):
        kind = "function"
    elif "function " in lowered or "=>" in stripped:
        kind = "function"
    return {
        "symbol": symbol,
        "kind": kind,
        "file": relative_path.replace("\\", "/"),
        "line": line_number,
        "relationships": [],
        "confidence": 1.0,
    }


def _search_specific_files(root: Path, relative_files: list[str], query: str, max_results: int) -> list[dict[str, Any]]:
    needle = query.casefold()
    results: list[dict[str, Any]] = []
    for relative in relative_files:
        if len(results) >= max_results or _is_noise_path(relative) or not _is_code_path(relative):
            continue
        path = (root / Path(relative)).resolve(strict=False)
        try:
            if not path.is_file() or path.stat().st_size > MAX_SOURCE_FILE_BYTES:
                continue
            text = _decode_source(path.read_bytes())
        except OSError:
            continue
        if text is None:
            continue
        for line_number, line in enumerate(text.splitlines(), start=1):
            if needle in line.casefold():
                results.append(_source_match_result(relative, line_number, line, query))
                break
    return results[:max_results]


def _git_grep(root: Path, query: str, max_results: int) -> list[dict[str, Any]]:
    result = _run(
        ["git", "-c", "core.quotepath=false", "grep", "-n", "-I", "-F", "-e", query, "--", "."],
        cwd=root,
        timeout_seconds=8.0,
    )
    if result is None or result.returncode not in {0, 1}:
        return []
    matches: list[dict[str, Any]] = []
    for row in result.stdout.splitlines():
        if len(matches) >= max_results:
            break
        try:
            file_text, line_text, content = row.split(":", 2)
            line_number = int(line_text)
        except (ValueError, TypeError):
            continue
        file_text = file_text.replace("\\", "/")
        if _is_noise_path(file_text) or not _is_code_path(file_text):
            continue
        item = _source_match_result(file_text, line_number, content, query)
        item["source"] = "git_grep"
        item["confidence"] = 0.95
        matches.append(item)
    return matches


def _rg_source_search(root: Path, query: str, max_results: int) -> list[dict[str, Any]]:
    """Use the verified ripgrep backend for cheap source-first navigation."""
    try:
        payload = native_backend.search_text_native(
            root,
            query=query,
            regex=False,
            file_pattern="*",
            case_sensitive=False,
            context_lines=0,
            max_results=max(max_results * 4, max_results),
            max_file_bytes=MAX_SOURCE_FILE_BYTES,
            timeout_seconds=8.0,
            include_ignored=False,
        )
    except native_backend.NativeAccelerationUnavailable:
        needle = query.casefold()
        results: list[dict[str, Any]] = []
        for current_root, dirnames, filenames in os.walk(root):
            dirnames[:] = [name for name in dirnames if name.casefold() not in NOISE_PARTS]
            for filename in filenames:
                if len(results) >= max_results:
                    return results
                path = Path(current_root) / filename
                try:
                    relative = path.resolve(strict=False).relative_to(root).as_posix()
                    if _is_noise_path(relative) or not _is_code_path(relative) or path.stat().st_size > MAX_SOURCE_FILE_BYTES:
                        continue
                    text = _decode_source(path.read_bytes())
                except OSError:
                    continue
                if text is None:
                    continue
                for line_number, line in enumerate(text.splitlines(), start=1):
                    if needle in line.casefold():
                        item = _source_match_result(relative, line_number, line, query)
                        item["source"] = "python_text"
                        item["confidence"] = 0.95
                        results.append(item)
                        break
        return results[:max_results]
    results: list[dict[str, Any]] = []
    seen: set[tuple[str, int]] = set()
    for item in payload.get("matches", []):
        if len(results) >= max_results or not isinstance(item, dict):
            break
        path_text = str(item.get("path") or "")
        line_number = int(item.get("line") or 0)
        if not path_text or line_number <= 0:
            continue
        try:
            relative = Path(path_text).resolve(strict=False).relative_to(root).as_posix()
        except ValueError:
            continue
        if _is_noise_path(relative) or not _is_code_path(relative):
            continue
        key = (relative.casefold(), line_number)
        if key in seen:
            continue
        seen.add(key)
        result = _source_match_result(relative, line_number, str(item.get("text") or ""), query)
        result["source"] = "rg_text"
        result["confidence"] = 0.98
        results.append(result)
    return results


def _find_symbol_line(root: Path, relative_file: str, symbol: str, kind: str) -> int | None:
    path = (root / relative_file).resolve(strict=False)
    try:
        if not path.is_file() or path.stat().st_size > MAX_SOURCE_FILE_BYTES:
            return None
        text = _decode_source(path.read_bytes())
    except OSError:
        return None
    if text is None:
        return None
    escaped = re.escape(symbol)
    patterns: list[re.Pattern[str]] = []
    if kind in {"function", "method"}:
        patterns.extend(
            [
                re.compile(rf"^\s*(?:async\s+)?def\s+{escaped}\b"),
                re.compile(rf"^\s*(?:export\s+)?(?:async\s+)?function\s+{escaped}\b"),
                re.compile(rf"^\s*(?:export\s+)?(?:const|let|var)\s+{escaped}\b"),
                re.compile(rf"^\s*(?:(?:public|private|protected|static|async)\s+)*{escaped}\s*\("),
            ]
        )
    elif kind in {"class", "interface"}:
        patterns.append(re.compile(rf"^\s*(?:export\s+)?(?:abstract\s+)?(?:class|interface)\s+{escaped}\b"))
    elif kind == "variable":
        patterns.append(re.compile(rf"^\s*(?:export\s+)?(?:const|let|var)\s+{escaped}\b"))

    fallback: int | None = None
    for line_number, line in enumerate(text.splitlines(), start=1):
        if fallback is None and symbol in line:
            fallback = line_number
        if any(pattern.search(line) for pattern in patterns):
            return line_number
    return fallback


def _graph_results(
    root: Path,
    project_name: str,
    query: str,
    intent: str,
    max_results: int,
) -> list[dict[str, Any]]:
    limit = min(max(max_results * 4, 8), 40)
    identifier_like = bool(query) and query.replace("_", "").isalnum()
    search_args: dict[str, Any] = {
        "project": project_name,
        "include_connected": intent != "symbol",
        "limit": limit,
    }
    if identifier_like:
        search_args["name_pattern"] = query
    else:
        search_args["query"] = query
    payload = _run_cli("search_graph", search_args, cwd=root)
    raw_results = payload.get("results", []) if payload else []
    if identifier_like and not raw_results:
        payload = _run_cli(
            "search_graph",
            {
                "project": project_name,
                "query": query,
                "include_connected": intent != "symbol",
                "limit": limit,
            },
            cwd=root,
        )
        raw_results = payload.get("results", []) if payload else []
    results: list[dict[str, Any]] = []
    for item in raw_results:
        if len(results) >= max_results or not isinstance(item, dict):
            continue
        relative = str(item.get("file_path") or "").replace("\\", "/")
        if (
            not relative
            or _is_noise_path(relative)
            or not _is_code_path(relative)
            or not (root / relative).is_file()
        ):
            continue
        start_line = item.get("start_line")
        end_line = item.get("end_line")
        line_value: int | str | None
        if isinstance(start_line, int) and isinstance(end_line, int) and end_line != start_line:
            line_value = f"{start_line}-{end_line}"
        else:
            line_value = start_line if isinstance(start_line, int) else None
        connected = item.get("connected_names") if isinstance(item.get("connected_names"), list) else []
        relationships: list[str] = []
        seen: set[str] = set()
        for name in connected:
            clean = str(name).strip()
            if clean and clean not in seen and clean != item.get("name"):
                seen.add(clean)
                relationships.append(clean)
            if len(relationships) >= 6:
                break
        results.append(
            {
                "symbol": str(item.get("name") or query),
                "kind": str(item.get("label") or "symbol").casefold(),
                "file": relative,
                "line": line_value,
                "relationships": relationships,
                "confidence": 0.9,
                "source": "graph",
            }
        )
    return results


def _merge_results(primary: list[dict[str, Any]], secondary: list[dict[str, Any]], max_results: int) -> list[dict[str, Any]]:
    merged: list[dict[str, Any]] = []
    seen: set[tuple[str, str]] = set()
    for item in [*primary, *secondary]:
        key = (str(item.get("file") or "").casefold(), str(item.get("line") or ""))
        if key in seen:
            continue
        seen.add(key)
        merged.append(item)
        if len(merged) >= max_results:
            break
    return merged


def navigate_code(
    repo_path: str,
    query: str,
    intent: Literal["symbol", "callers", "callees", "impact", "architecture", "path"] = "symbol",
    max_results: int = 8,
) -> dict[str, Any]:
    clean_query = query.strip()
    if not clean_query:
        raise ValueError("query 不能为空")
    if intent not in VALID_INTENTS:
        raise ValueError("intent 必须是 symbol/callers/callees/impact/architecture/path")
    max_results = max(1, min(int(max_results), 20))
    requested = Path(repo_path).expanduser().resolve(strict=False)
    if not requested.is_dir():
        raise FileNotFoundError(str(requested))

    # The working tree is the source of truth. For simple symbol/path lookups,
    # query it before spawning Git or graph/index helpers. Dirty files are already
    # represented by their actual current bytes, so graph freshness is irrelevant
    # when this path succeeds.
    if intent in {"symbol", "path"}:
        direct_source = _rg_source_search(requested, clean_query, max_results)
        if direct_source:
            return {
                "freshness": "fresh",
                "freshness_basis": "working_tree_source",
                "fallback_used": False,
                "fast_path_used": True,
                "graph_checked": False,
                "results": direct_source[:max_results],
            }

    repo_state = _git_state(requested)
    root = Path(repo_state["root"])
    changed_files = [str(item).replace("\\", "/") for item in repo_state.get("changed_files", [])]
    changed_set = {item.casefold() for item in changed_files}

    fallback_used = False
    source: list[dict[str, Any]] = []
    if repo_state.get("dirty"):
        source = _search_specific_files(root, changed_files, clean_query, max_results)
        for item in source:
            item["source"] = "dirty_source"
        fallback_used = bool(source)

    fast_path_used = False
    if intent in {"symbol", "path"} and len(source) < max_results:
        fast_source = _rg_source_search(root, clean_query, max_results)
        if repo_state.get("dirty"):
            fast_source = [
                item
                for item in fast_source
                if str(item.get("file") or "").casefold() not in changed_set
            ]
        if fast_source:
            source = _merge_results(source, fast_source, max_results)
            fast_path_used = True
        if source:
            return {
                "freshness": "dirty" if repo_state.get("dirty") else "fresh",
                "fallback_used": fallback_used,
                "fast_path_used": fast_path_used,
                "graph_checked": False,
                "results": source[:max_results],
            }

    # Only relationship queries or source misses pay for graph discovery and
    # index freshness checks.
    project = _select_project(repo_state)
    project_name = str(project.get("name") or "") if project else ""
    index_status = _run_cli("index_status", {"project": project_name}, cwd=root) if project_name else None
    freshness = _freshness(repo_state, project, index_status)

    changed_definition_found = any(
        str(item.get("kind") or "") in {"function", "class"}
        for item in source
    )

    graph: list[dict[str, Any]] = []
    if (
        freshness != "stale"
        and project_name
        and not (intent == "symbol" and changed_definition_found)
    ):
        graph = _graph_results(root, project_name, clean_query, intent, max_results)
        if freshness == "dirty":
            graph = [item for item in graph if str(item.get("file") or "").casefold() not in changed_set]
            for item in graph:
                item["confidence"] = 0.75
        for item in graph:
            if item.get("line") is not None:
                continue
            file_text = str(item.get("file") or "")
            symbol_text = str(item.get("symbol") or clean_query)
            kind_text = str(item.get("kind") or "symbol")
            verified_line = _find_symbol_line(root, file_text, symbol_text, kind_text)
            if verified_line is not None:
                item["line"] = verified_line
                item["confidence"] = 0.85 if freshness == "dirty" else 0.95

    if not graph and not source:
        source = _git_grep(root, clean_query, max_results)
        fallback_used = True

    results = _merge_results(source, graph, max_results)
    return {
        "freshness": freshness,
        "fallback_used": fallback_used,
        "fast_path_used": fast_path_used,
        "results": results,
    }
