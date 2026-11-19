from __future__ import annotations

import argparse
import json
import math
import os
from pathlib import Path
import statistics
import sys
import time
from typing import Callable, TypeVar

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

import runtime
import server

# Benchmark processes must not contend with the live Runtime's rotating log file.
server.LOGGER.disabled = True

T = TypeVar("T")


def _stats(values: list[float]) -> dict[str, float | int]:
    ordered = sorted(values)

    def percentile(fraction: float) -> float:
        index = min(len(ordered) - 1, max(0, math.ceil(len(ordered) * fraction) - 1))
        return ordered[index]

    return {
        "n": len(ordered),
        "mean_ms": round(statistics.mean(ordered) * 1000, 3),
        "p50_ms": round(percentile(0.50) * 1000, 3),
        "p95_ms": round(percentile(0.95) * 1000, 3),
        "min_ms": round(ordered[0] * 1000, 3),
        "max_ms": round(ordered[-1] * 1000, 3),
    }


def _measure(fn: Callable[[], T], iterations: int) -> tuple[dict[str, float | int], T]:
    timings: list[float] = []
    last: T | None = None
    for _ in range(iterations):
        started = time.perf_counter()
        last = fn()
        timings.append(time.perf_counter() - started)
    assert last is not None
    return _stats(timings), last


def _search_probe(repo: Path) -> dict[str, object]:
    return server.search_text(
        root=str(repo),
        query="runtime_admin",
        regex=False,
        file_pattern="*.py",
        case_sensitive=False,
        context_lines=1,
        max_results=50,
        max_file_bytes=4 * 1024 * 1024,
        timeout_seconds=15,
        include_ignored=False,
    )


def _files_probe(repo: Path) -> dict[str, object]:
    return server.search_files(
        root=str(repo),
        pattern="*.py",
        recursive=True,
        include_directories=False,
        case_sensitive=False,
        max_results=100,
        timeout_seconds=15,
        include_ignored=False,
    )


def _batch_probe(repo: Path) -> dict[str, object]:
    candidates = sorted(
        path
        for path in repo.rglob("*.py")
        if ".venv" not in path.parts and "__pycache__" not in path.parts
    )[:24]
    return server.read_multiple_files(
        [str(path) for path in candidates],
        max_chars_each=20_000,
        max_bytes_each=2 * 1024 * 1024,
    )


def _set_search_mode(mode: str) -> None:
    if mode == "forced_native":
        os.environ["GPT_PC_NATIVE_ACCELERATION"] = "1"
    elif mode == "forced_python":
        os.environ["GPT_PC_NATIVE_ACCELERATION"] = "0"
    elif mode == "adaptive":
        os.environ.pop("GPT_PC_NATIVE_ACCELERATION", None)
    else:
        raise ValueError(mode)


def _mode_probe(repo: Path, iterations: int, mode: str) -> dict[str, object]:
    _set_search_mode(mode)
    original_prefer_native = server.native_backend.prefer_native_for_tree
    if mode == "forced_native":
        server.native_backend.prefer_native_for_tree = lambda *args, **kwargs: True
    try:
        search_stats, search_result = _measure(lambda: _search_probe(repo), iterations)
        file_stats, file_result = _measure(lambda: _files_probe(repo), iterations)
    finally:
        server.native_backend.prefer_native_for_tree = original_prefer_native
    return {
        "search_text": {
            "timing": search_stats,
            "backend": search_result.get("backend", "python"),
            "count": search_result.get("count"),
        },
        "search_files": {
            "timing": file_stats,
            "backend": file_result.get("backend", "python"),
            "count": file_result.get("count"),
        },
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--iterations", type=int, default=5)
    parser.add_argument("--repo", type=Path, default=Path(__file__).resolve().parents[1])
    parser.add_argument(
        "--output",
        type=Path,
        default=Path(r"C:\GPT-PC\runtime\benchmarks\fast-path-v1-latest.json"),
    )
    args = parser.parse_args()
    iterations = max(2, min(args.iterations, 20))
    repo = args.repo.resolve()

    runtime._ACTIVE_JOB_INDEX_SOURCE = None
    started = time.perf_counter()
    active_after_rebuild = runtime._running_job_records_locked()
    rebuild_ms = round((time.perf_counter() - started) * 1000, 3)
    warm_active, _ = _measure(runtime._running_job_records_locked, max(iterations * 4, 10))

    old_acceleration = os.environ.get("GPT_PC_NATIVE_ACCELERATION")
    try:
        forced_native = _mode_probe(repo, iterations, "forced_native")
        forced_python = _mode_probe(repo, iterations, "forced_python")
        adaptive = _mode_probe(repo, iterations, "adaptive")
    finally:
        if old_acceleration is None:
            os.environ.pop("GPT_PC_NATIVE_ACCELERATION", None)
        else:
            os.environ["GPT_PC_NATIVE_ACCELERATION"] = old_acceleration

    batch_read, batch_result = _measure(lambda: _batch_probe(repo), iterations)
    native_text_p50 = float(forced_native["search_text"]["timing"]["p50_ms"])
    python_text_p50 = float(forced_python["search_text"]["timing"]["p50_ms"])
    native_files_p50 = float(forced_native["search_files"]["timing"]["p50_ms"])
    python_files_p50 = float(forced_python["search_files"]["timing"]["p50_ms"])

    payload = {
        "schema_version": 2,
        "repo": str(repo),
        "iterations": iterations,
        "job_history_count": runtime.job_history_count(),
        "active_index": {
            "cold_rebuild_ms": rebuild_ms,
            "active_jobs": len(active_after_rebuild),
            "warm_lookup": warm_active,
        },
        "search_modes": {
            "forced_native": forced_native,
            "forced_python": forced_python,
            "adaptive": adaptive,
            "forced_native_speedup_p50": {
                "search_text": round(python_text_p50 / max(native_text_p50, 0.001), 2),
                "search_files": round(python_files_p50 / max(native_files_p50, 0.001), 2),
            },
        },
        "read_multiple_files_baseline": {
            "timing": batch_read,
            "files": batch_result.get("count"),
            "internal_thread_fanout": False,
            "note": "Single-GPT read parallelism is provided by outer tool lanes; local batch reading stays sequential because measured internal threading was slower on this machine.",
        },
    }
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(payload, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
