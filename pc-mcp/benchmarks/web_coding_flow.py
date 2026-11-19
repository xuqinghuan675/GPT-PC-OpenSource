"""Compare the same three-file edit via an isolated, real HTTP MCP endpoint.

No model/API inference is invoked. Results measure plugin workflow requests,
structured response bytes, and loopback protocol time, not ChatGPT latency.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import logging
from pathlib import Path
import socket
import statistics
import sys
import tempfile
import threading
import time
import types

ROOT = Path(__file__).resolve().parents[2]
sys.path[:0] = [str(ROOT / "pc-mcp"), str(ROOT / "pc-mcp/tests")]


async def measure(client, workspace, iterations, paired=False):
    async def call(name, args):
        response = await client.call_tool(name, args)
        if response.is_error:
            raise RuntimeError(f"{name}: {response.content}")
        return response.structured_content

    lease = await call("acquire_workspace", {"path": str(workspace), "owner_label": "isolated web coding benchmark"})
    lease_id = lease["lease_id"]
    await call("set_visual_mode", {"workspace_lease_id": lease_id, "action": "update", "goal_enabled": True,
                                  "goal_outcome": "Update three related modules and verify the result", "goal_success_criteria": ["All three functions updated correctly"]})
    files = [workspace / f"module_{index}.py" for index in range(3)]
    samples = {"before": [], "after": []} if paired else {"single": []}
    try:
        for iteration in range(iterations + 1):
            variants = (["before", "after"] if iteration % 2 else ["after", "before"]) if paired else ["single"]
            for variant in variants:
                sample = await measure_one(call, workspace, files, lease_id, "before_" if variant == "before" else "")
                if iteration:
                    sample["iteration"] = iteration
                    samples[variant].append(sample)
        done = await call("report_progress", {"workspace_lease_id": lease_id, "phase": "done", "text": "Isolated benchmark complete"})
        assert done["lease_released"]
        lease_id = None
    finally:
        if lease_id:
            await call("release_workspace", {"workspace_lease_id": lease_id})
    summaries = {key: {"iterations": iterations, "requests_per_task": rows[0]["requests"],
                      "median_response_bytes": statistics.median(s["response_bytes"] for s in rows),
                      "median_ms": round(statistics.median(s["elapsed_ms"] for s in rows), 3),
                      "samples": rows} for key, rows in samples.items()}
    return summaries if paired else summaries["single"]


async def measure_one(call, workspace, files, lease_id, prefix):
    for path in files:
        path.write_text("# representative module\n" * 32 + "def compute(x):\n    return x + 1\n", encoding="utf-8")
    requests = 0
    response_bytes = 0
    steps = []

    async def step(name, args):
        nonlocal requests, response_bytes
        start = time.perf_counter()
        value = await call(prefix + name, {**args, "workspace_lease_id": lease_id})
        steps.append({"tool": name, "ms": round((time.perf_counter() - start) * 1000, 3)})
        requests += 1
        response_bytes += len(json.dumps(value, ensure_ascii=False, separators=(",", ":")).encode("utf-8"))
        return value

    started = time.perf_counter()
    read = await step("read_multiple_files", {"paths": [str(path) for path in files]})
    for item in read["files"]:
        version = item.get("sha256")
        if not version:
            version = (await step("read_text_file_with_hash", {"path": item["path"], "max_chars": 1}))["sha256"]
        changed = await step("replace_text", {"path": item["path"], "old": "return x + 1", "new": "return x + 2", "expected_sha256": version})
        assert changed["status"] == "written"
    check = await step("read_multiple_files", {"paths": [str(path) for path in files]})
    assert all("return x + 2" in item["content"] for item in check["files"])
    elapsed_ms = (time.perf_counter() - started) * 1000
    # Independent correctness oracle; not counted as a model/tool step.
    for path in files:
        namespace = {}
        exec(compile(path.read_text(encoding="utf-8"), str(path), "exec"), namespace)
        assert namespace["compute"](3) == 5
    return {"requests": requests, "response_bytes": response_bytes, "elapsed_ms": elapsed_ms, "steps": steps}

def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--source", type=Path, default=ROOT / "pc-mcp/server.py")
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--iterations", type=int, default=5)
    parser.add_argument("--compare-baseline", type=Path)
    options = parser.parse_args()
    source = options.source.read_text(encoding="utf-8")
    # Keep runtime-relative paths identical while selecting before/after code.
    server = types.ModuleType("server")
    server.__file__ = str(ROOT / "pc-mcp/server.py")
    sys.modules["server"] = server
    exec(compile(source, server.__file__, "exec"), server.__dict__)
    baseline = None
    if options.compare_baseline:
        baseline = types.ModuleType("server_before")
        baseline.__file__ = server.__file__
        sys.modules["server_before"] = baseline
        exec(compile(options.compare_baseline.read_text(encoding="utf-8"), baseline.__file__, "exec"), baseline.__dict__)
        baseline.MCP_INSTANCE_ID = server.MCP_INSTANCE_ID
        for name in ("read_multiple_files", "read_text_file_with_hash", "replace_text"):
            server.mcp.add_tool(getattr(baseline, name), name="before_" + name)
    logging.disable(logging.CRITICAL)
    from mcp import Client
    import pytest
    import uvicorn
    from test_server import isolate_runtime

    with tempfile.TemporaryDirectory(prefix="web-flow-", dir=ROOT / "workspaces") as temporary:
        base = Path(temporary)
        monkeypatch = pytest.MonkeyPatch()
        isolate_runtime(monkeypatch, base)
        if baseline is not None:
            for name, value in vars(server).items():
                if name.startswith(("ACTIVITY_", "UI_")) or name == "THREAD_NOTE_FILE":
                    setattr(baseline, name, value)
        project = base / "project"
        project.mkdir()
        with socket.socket() as available:
            available.bind(("127.0.0.1", 0))
            port = available.getsockname()[1]
        service = uvicorn.Server(uvicorn.Config(server.mcp.streamable_http_app(), host="127.0.0.1", port=port, log_level="critical", access_log=False))
        worker = threading.Thread(target=service.run, daemon=True)
        worker.start()
        try:
            deadline = time.monotonic() + 30
            while not service.started:
                if not worker.is_alive() or time.monotonic() > deadline:
                    raise RuntimeError("Isolated benchmark endpoint did not start")
                time.sleep(0.05)

            async def run():
                async with Client(f"http://127.0.0.1:{port}/mcp") as client:
                    return await measure(client, project, options.iterations, baseline is not None)

            result = asyncio.run(run())
        finally:
            service.should_exit = True
            worker.join(timeout=10)
            monkeypatch.undo()
            if worker.is_alive():
                raise RuntimeError("Benchmark endpoint did not stop")
    result.update(source=str(options.source), scope="isolated HTTP plugin workflow; excludes model inference and external network")
    options.output.parent.mkdir(parents=True, exist_ok=True)
    options.output.write_text(json.dumps(result, indent=2), encoding="utf-8")
    print(json.dumps(result))


if __name__ == "__main__":
    main()
