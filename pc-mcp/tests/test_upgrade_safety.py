from __future__ import annotations

from contextlib import contextmanager
import asyncio
import json

import pytest

from chatgpt_agent_store import AgentStore
from machine_config import MachineConfig
import runtime as runtime_module
import server as server_module
from test_server import call_tool, isolate_runtime


@pytest.fixture(autouse=True)
def isolated_metadata(monkeypatch, tmp_path):
    isolate_runtime(monkeypatch, tmp_path)


def test_queued_write_rechecks_new_user_control(monkeypatch, tmp_path):
    workspace = tmp_path / "work"
    workspace.mkdir()
    lease = call_tool("acquire_workspace", {"path": str(workspace)})
    original_lock = server_module._lock_mutating_tool_paths

    @contextmanager
    def message_arrives_during_lock_wait(*args, **kwargs):
        with original_lock(*args, **kwargs) as elapsed:
            server_module.UI_INPUT_FILE.parent.mkdir(parents=True, exist_ok=True)
            server_module.UI_INPUT_FILE.write_text(json.dumps({
                "id": "arrived-while-queued", "type": "ui.message", "scope": "task",
                "threadId": lease["thread_id"], "taskId": lease["lease_id"],
                "timestampMs": 10, "text": "停止写入",
            }) + "\n", encoding="utf-8")
            yield elapsed

    monkeypatch.setattr(server_module, "_lock_mutating_tool_paths", message_arrives_during_lock_wait)
    target = workspace / "data.txt"
    with pytest.raises(Exception, match="用户控制输入待处理"):
        server_module.write_text_file(str(target), "MUST_NOT_WRITE", workspace_lease_id=lease["lease_id"])
    assert not target.exists()


def test_queued_write_rechecks_released_lease(monkeypatch, tmp_path):
    workspace = tmp_path / "work"
    workspace.mkdir()
    lease = call_tool("acquire_workspace", {"path": str(workspace)})
    original_lock = server_module._lock_mutating_tool_paths

    @contextmanager
    def released_during_lock_wait(*args, **kwargs):
        with original_lock(*args, **kwargs) as elapsed:
            runtime_module.release_workspace(lease["lease_id"])
            yield elapsed

    monkeypatch.setattr(server_module, "_lock_mutating_tool_paths", released_during_lock_wait)
    target = workspace / "data.txt"
    with pytest.raises(Exception, match="租约不存在"):
        server_module.write_text_file(str(target), "MUST_NOT_WRITE", workspace_lease_id=lease["lease_id"])
    assert not target.exists()


def test_admin_tree_operations_protect_nested_workspaces_without_blocking_commands(monkeypatch, tmp_path):
    root = tmp_path / "root"
    parent = root / "projects"
    project = parent / "active-project"
    project.mkdir(parents=True)
    maintenance = root / "runtime" / "maintenance-workspace"
    maintenance.mkdir(parents=True)
    monkeypatch.setattr(runtime_module, "PROJECT_ROOT", root)
    monkeypatch.setattr(runtime_module, "RUNTIME_MAINTENANCE_WORKSPACE", maintenance)
    admin = runtime_module.acquire_workspace(maintenance, mode="execute_exclusive", owner_label="maintenance")
    runtime_module.acquire_workspace(project, mode="execute_exclusive", owner_label="active project")
    # A command starting at the parent is still allowed: this is coordination, not an OS sandbox.
    runtime_module.require_execute_lease(admin["lease_id"], [root])
    target = project / "keep.txt"
    target.write_text("KEEP", encoding="utf-8")
    with pytest.raises(Exception, match="RESOURCE_OWNERSHIP_CONFLICT"):
        server_module.delete_path(str(parent), recursive=True, workspace_lease_id=admin["lease_id"])
    assert target.read_text(encoding="utf-8") == "KEEP"
    with pytest.raises(Exception, match="RESOURCE_OWNERSHIP_CONFLICT"):
        server_module.move_path(str(parent), str(root / "moved"), workspace_lease_id=admin["lease_id"])
    assert target.exists()


def test_rename_validates_destination_ownership(monkeypatch, tmp_path):
    root = tmp_path / "root"
    root.mkdir()
    maintenance = root / "runtime" / "maintenance-workspace"
    maintenance.mkdir(parents=True)
    monkeypatch.setattr(runtime_module, "PROJECT_ROOT", root)
    monkeypatch.setattr(runtime_module, "RUNTIME_MAINTENANCE_WORKSPACE", maintenance)
    admin = runtime_module.acquire_workspace(maintenance, mode="execute_exclusive", owner_label="maintenance")
    owned = root / "owned"
    owned.mkdir()
    runtime_module.acquire_workspace(owned, mode="execute_exclusive", owner_label="project")
    source = root / "incoming"
    source.mkdir()
    with pytest.raises(runtime_module.RuntimePolicyError) as exc:
        server_module.rename_path(str(source), "owned", overwrite=True, workspace_lease_id=admin["lease_id"])
    assert exc.value.error_type == "RESOURCE_OWNERSHIP_CONFLICT"
    assert source.exists() and owned.exists()


def test_mode_guidance_is_advisory_and_respects_user_switches(tmp_path):
    workspace = tmp_path / "work"
    workspace.mkdir()
    lease = call_tool("acquire_workspace", {"path": str(workspace)})
    assert "action='update'" in lease["mode_guidance"]
    assert "不按标题关键词强制开启" in lease["mode_guidance"]
    record = runtime_module.active_lease_record(lease["lease_id"])
    assert not runtime_module.goal_mode_state(record)["active"]
    assert not runtime_module.visual_mode_state(record)["active"]
    record["goal_mode"] = {"user_mode": "disabled", "enabled": False, "status": "off"}
    record["visual_mode"] = {"user_mode": "disabled", "status": "off"}
    # Ultra is an independent user-controlled execution mode. If it is not
    # explicitly disabled, guidance may still recommend it for complex work.
    record["ultra_mode"] = {"user_mode": "disabled", "active": False, "status": "off"}
    assert server_module._mode_activation_guidance(record) == ""


def test_health_probe_checks_actual_client_and_does_not_hide_connection_failure(monkeypatch):
    import health_probe

    result = asyncio.run(health_probe.probe(server_module.mcp))
    assert result["status"] == "PASS"
    assert result["runtime_instance"] == server_module.MCP_INSTANCE_ID
    assert result["tool_count"] == 82
    targets = []

    def unavailable(target):
        targets.append(target)
        raise ConnectionError("live endpoint unavailable")

    monkeypatch.setattr(health_probe, "Client", unavailable)
    with pytest.raises(ConnectionError, match="live endpoint unavailable"):
        asyncio.run(health_probe.probe())
    assert targets == [health_probe.DEFAULT_MCP_URL]


def test_runtime_manager_does_not_launch_a_browser(monkeypatch, tmp_path):
    config = MachineConfig.from_mapping({}, runtime_root=str(tmp_path))
    monkeypatch.setattr(server_module, '_CHATGPT_AGENT_MANAGER', None)
    monkeypatch.setattr(server_module, 'load_machine_config', lambda **_: config)
    monkeypatch.setattr(server_module, '_create_job', lambda **_: pytest.fail('no automatic browser job'))
    manager = server_module._get_chatgpt_agent_manager()
    assert manager.browser_factory is None
    assert manager.start_background_services()['automatic_supervision'] is False
    assert manager.store.setting('browser_managed_job') is None
