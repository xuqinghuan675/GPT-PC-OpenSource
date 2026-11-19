from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
import threading
import time

import runtime as runtime_module
import server as server_module


def test_tool_lanes_prioritize_control_and_expand_same_gpt_reads():
    assert server_module._tool_lane_name("poll_ui_messages") == "control"
    assert server_module._tool_lane_name("runtime_status") == "control"
    assert server_module._tool_lane_name("report_progress") == "control"
    assert server_module._tool_lane_name("read_text_range") == "file_read"
    assert server_module._tool_lane_name("read_multiple_files") == "file_read"
    assert server_module._tool_lane_name("search_text") == "search"
    assert server_module._tool_lane_name("git_status") == "git_read"
    assert server_module._tool_lane_name("code_navigate") == "code_navigation"
    assert server_module._tool_lane_name("run_python") == "execution"
    assert server_module.TOOL_LANE_LIMITS["file_read"] == 32
    assert server_module.TOOL_LANE_LIMITS["search"] == 8
    assert server_module.TOOL_LANE_LIMITS["control"] == 8


def test_control_lane_remains_available_when_search_lane_is_full():
    search = server_module._TOOL_LANE_SEMAPHORES["search"]
    control = server_module._TOOL_LANE_SEMAPHORES["control"]
    acquired_search = 0
    try:
        for _ in range(server_module.TOOL_LANE_LIMITS["search"]):
            assert search.acquire(blocking=False)
            acquired_search += 1
        assert search.acquire(blocking=False) is False
        assert control.acquire(timeout=0.05) is True
        control.release()
    finally:
        for _ in range(acquired_search):
            search.release()


def test_same_lease_mutations_are_serialized_even_on_different_paths(tmp_path: Path):
    active = 0
    max_active = 0
    counter_lock = threading.Lock()
    lease = {"lease_id": "same-lease-for-mutation-test"}

    def fake_write(path: str, workspace_lease_id: str | None = None):
        return path

    fake_write.__name__ = "write_text_file"

    def worker(path: Path):
        nonlocal active, max_active
        with server_module._lock_mutating_tool_paths(
            fake_write,
            (str(path),),
            {"workspace_lease_id": lease["lease_id"]},
            lease=lease,
        ):
            with counter_lock:
                active += 1
                max_active = max(max_active, active)
            time.sleep(0.05)
            with counter_lock:
                active -= 1

    with ThreadPoolExecutor(max_workers=2) as executor:
        futures = [
            executor.submit(worker, tmp_path / "a.txt"),
            executor.submit(worker, tmp_path / "b.txt"),
        ]
        for future in futures:
            future.result(timeout=2)
    assert max_active == 1


def test_read_multiple_files_preserves_order_while_lane_parallelism_stays_external(tmp_path: Path):
    paths = []
    for index in range(8):
        path = tmp_path / f"file-{index}.txt"
        path.write_text(f"value-{index}", encoding="utf-8")
        paths.append(str(path))

    result = server_module.read_multiple_files(paths, max_chars_each=1000, max_bytes_each=1000)
    assert result["count"] == len(paths)
    assert "parallel_workers" not in result
    assert server_module.TOOL_LANE_LIMITS["file_read"] == 32
    assert [Path(item["path"]).name for item in result["files"]] == [Path(path).name for path in paths]
    assert [item["content"] for item in result["files"]] == [f"value-{index}" for index in range(8)]


def test_read_checkpoint_burst_coalesces_and_flushes(monkeypatch, tmp_path: Path):
    runtime_dir = tmp_path / "runtime"
    monkeypatch.setattr(runtime_module, "RUNTIME_DIR", runtime_dir)
    monkeypatch.setattr(runtime_module, "LEASE_DIR", runtime_dir / "leases")
    monkeypatch.setattr(runtime_module, "JOB_DIR", runtime_dir / "jobs")
    monkeypatch.setattr(runtime_module, "JOB_LOG_DIR", tmp_path / "logs" / "jobs")
    monkeypatch.setattr(runtime_module, "METADATA_LOCK_PATH", runtime_dir / ".metadata.lock")
    runtime_module._ACTIVE_JOB_INDEX_SOURCE = None
    runtime_module._CHECKPOINT_OVERLAYS.clear()
    runtime_module._ensure_runtime_dirs()

    workspace = tmp_path / "workspace"
    workspace.mkdir()
    lease = runtime_module.acquire_workspace(
        workspace,
        mode="execute_exclusive",
        owner_label="checkpoint burst test",
    )
    lease_id = lease["lease_id"]
    try:
        first = runtime_module.update_workspace_execution_checkpoint(
            lease_id,
            tool_name="read_text_range",
            side_effect=False,
        )
        disk_after_first = runtime_module._read_json(runtime_module._lease_path(lease_id))
        assert disk_after_first is not None
        assert first["successful_action_count"] == 1
        assert disk_after_first["execution_successful_action_count"] == 1

        second = runtime_module.update_workspace_execution_checkpoint(
            lease_id,
            tool_name="search_text",
            side_effect=False,
        )
        disk_after_second = runtime_module._read_json(runtime_module._lease_path(lease_id))
        visible_after_second = runtime_module.active_lease_record(lease_id)
        assert disk_after_second is not None
        assert visible_after_second is not None
        assert second["successful_action_count"] == 2
        assert disk_after_second["execution_successful_action_count"] == 1
        assert runtime_module._execution_checkpoint(visible_after_second)["successful_action_count"] == 2

        flushed = runtime_module.update_workspace_execution_checkpoint(
            lease_id,
            tool_name="run_python",
            side_effect=True,
        )
        disk_after_flush = runtime_module._read_json(runtime_module._lease_path(lease_id))
        assert disk_after_flush is not None
        assert flushed["successful_action_count"] == 3
        assert flushed["side_effect_action_count"] == 1
        assert disk_after_flush["execution_successful_action_count"] == 3
        assert lease_id not in runtime_module._CHECKPOINT_OVERLAYS
    finally:
        runtime_module.release_workspace(lease_id)
        assert lease_id not in runtime_module._CHECKPOINT_OVERLAYS
