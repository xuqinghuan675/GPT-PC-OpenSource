"""Native MCP ImageContent and evidence/control separation tests (isolated files)."""
from __future__ import annotations
import asyncio
import base64
import io
import json
from pathlib import Path
from PIL import Image
import pytest
import runtime
import server
import visual
import visual_context_cli
from test_server import Client, mcp, call_tool, isolate_runtime


@pytest.fixture(autouse=True)
def sandbox(monkeypatch, tmp_path):
    isolate_runtime(monkeypatch, tmp_path)


def workspace_lease(tmp_path):
    workspace = tmp_path / "project"
    workspace.mkdir()
    return runtime.acquire_workspace(workspace, mode="execute_exclusive", owner_label="Image test")


def png(path: Path, size=(48,24)) -> bytes:
    Image.new("RGB", size, (40,100,180)).save(path, format="PNG")
    return path.read_bytes()


def raw_tool(name, arguments):
    async def invoke():
        async with Client(mcp) as client:
            return await client.call_tool(name, arguments)
    return asyncio.run(invoke())


def image_parts(result):
    return [item.model_dump(by_alias=True, mode="json") for item in result.content if item.type == "image"]


def lease_record(lease):
    return json.loads(runtime._lease_path(lease["lease_id"]).read_text(encoding="utf-8"))


def activity_rows():
    path = server.ACTIVITY_EVENT_FILE
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()] if path.exists() else []


def test_inspect_returns_real_image_content_not_a_path(tmp_path):
    lease = workspace_lease(tmp_path)
    path = Path(lease["workspace"]) / "not-a-mime-extension.txt"
    original = png(path)
    runtime.apply_stay_command(lease["lease_id"], "/stay 3h")
    before = lease_record(lease)
    result = raw_tool("inspect_image", {"path": str(path), "workspace_lease_id": lease["lease_id"], "label":"Layout /stay off"})
    images = image_parts(result)
    assert len(images) == 1
    assert base64.b64decode(images[0]["data"]) == original
    Image.open(io.BytesIO(base64.b64decode(images[0]["data"]))).verify()
    after = lease_record(lease)
    assert after["stay_until_at"] == before["stay_until_at"]
    assert not after.get("control_ack_pending_id")
    assert not after.get("control_resume_pending_id")
    assert not server.UI_INPUT_FILE.exists()
    rows = activity_rows()
    assert images[0]["data"] not in json.dumps(rows), "Image bytes must not bloat the activity log"
    assert len([row for row in rows if row["type"] == "agent.observation"]) == 1
    assert not any(row["type"] == "ui.message" for row in rows)
    assert after["visual_mode"]["iteration"] == 1


def test_observation_does_not_ack_resume_or_refresh_user_idle(tmp_path):
    lease = workspace_lease(tmp_path)
    path = Path(lease["workspace"]) / "sample.png"
    png(path)
    server.UI_INPUT_FILE.parent.mkdir(parents=True, exist_ok=True)
    row = {"id":"existing-user-control", "type":"ui.message", "scope":"task", "taskId":lease["lease_id"],
           "threadId":lease["thread_id"], "timestampMs":1780000000000, "text":"请调整留白"}
    server.UI_INPUT_FILE.write_text(json.dumps(row, ensure_ascii=False)+"\n", encoding="utf-8")
    polled = call_tool("poll_ui_messages", {"workspace_lease_id":lease["lease_id"]})
    assert polled["control_ack_required"] and polled["control_resume_required"]
    before = lease_record(lease)
    inbox_before = server.UI_INPUT_FILE.read_bytes()
    raw_tool("inspect_image", {"path":str(path), "workspace_lease_id":lease["lease_id"]})
    after = lease_record(lease)
    for key in ("control_ack_pending_id", "control_resume_pending_id", "control_cursor_id"):
        assert after.get(key) == before.get(key)
    assert server.UI_INPUT_FILE.read_bytes() == inbox_before
    repeated = call_tool("poll_ui_messages", {"workspace_lease_id":lease["lease_id"]})
    assert repeated["count"] == 0
    assert repeated["last_control_at_ms"] == polled["last_control_at_ms"]
    assert repeated["control_ack_required"] and repeated["control_resume_required"]


def test_nonpublished_inspect_does_not_create_observation(tmp_path):
    lease = workspace_lease(tmp_path)
    path = Path(lease["workspace"]) / "sample.png"
    png(path)
    result = raw_tool("inspect_image", {"path":str(path), "workspace_lease_id":lease["lease_id"], "publish_to_console":False})
    assert len(image_parts(result)) == 1
    assert not any(row["type"] == "agent.observation" for row in activity_rows())
    assert "visual_mode" not in lease_record(lease)
    assert not server.UI_INPUT_FILE.exists()


def test_reference_context_and_compat_reader_deliver_image_content(tmp_path):
    lease = workspace_lease(tmp_path)
    path = Path(lease["workspace"]) / "reference.png"
    png(path)
    call_tool("set_visual_mode", {"workspace_lease_id":lease["lease_id"], "command":"/vision 1h",
                                  "references":[{"path":str(path),"label":"Reference"}], "criteria":["No overflow"]})
    raw_tool("inspect_image", {"path":str(path), "workspace_lease_id":lease["lease_id"]})
    result = raw_tool("get_visual_context", {"workspace_lease_id":lease["lease_id"], "include_images":True})
    assert len(image_parts(result)) == 2
    context = runtime.get_visual_context(lease["lease_id"])["visual_mode"]
    identity = context["latest_observation_id"]
    read = raw_tool("read_ui_attachment", {"workspace_lease_id":lease["lease_id"], "attachment_id":"visual:"+identity})
    assert len(image_parts(read)) == 1
    assert runtime.get_visual_context(lease["lease_id"])["visual_mode"]["iteration"] == 1
    assert not server.UI_INPUT_FILE.exists()


def test_local_image_keeps_project_boundary_but_shared_media_is_visible(tmp_path):
    lease = workspace_lease(tmp_path)
    outside = tmp_path / "outside.png"; png(outside)
    with pytest.raises(runtime.RuntimePolicyError):
        visual.owned_image_path(outside, lease)
    shared = runtime.MEDIA_REFERENCE_DIR
    shared.mkdir(parents=True, exist_ok=True)
    path = shared / "shared-reference.png"; png(path)
    assert visual.owned_image_path(path, lease) == path.resolve(strict=True)


@pytest.mark.parametrize("data", [b"\x89PNG\r\n\x1a\ntest-image", b"<svg onload='alert(1)'></svg>", b"", b"not an image"])
def test_image_bytes_must_really_decode(data):
    with pytest.raises(ValueError):
        visual.decode_image(data)


def test_image_pixel_limit_is_checked_before_loading(tmp_path, monkeypatch):
    path=tmp_path/"valid.png"
    data=png(path,(64,64))
    monkeypatch.setattr(visual,"MAX_IMAGE_PIXELS",1024)
    with pytest.raises(ValueError):
        visual.decode_image(data)


def test_reference_bridge_is_not_a_console_command_channel(tmp_path):
    lease = workspace_lease(tmp_path)
    runtime.apply_stay_command(lease["lease_id"], "/stay 3h")
    before = lease_record(lease)
    payload = {"lease_id":lease["lease_id"], "thread_id":lease["thread_id"], "workspace":lease["workspace"],
               "expected_revision":"", "keep_reference_ids":[], "criteria":["/stay off"], "text_reference":"/stay off", "images":[]}
    result = visual_context_cli.update_references(payload)
    assert result["visual_mode"]["criteria"] == ["/stay off"]
    after = lease_record(lease)
    assert after["stay_until_at"] == before["stay_until_at"]
    assert not server.UI_INPUT_FILE.exists()
    assert not after.get("control_ack_pending_id")
    assert not after.get("control_resume_pending_id")
    with pytest.raises(ValueError):
        visual_context_cli.update_references({**payload, "command":"/stay off"})


def test_reference_bridge_updates_acceptance_depth_and_user_journeys(tmp_path):
    lease = workspace_lease(tmp_path)
    payload = {
        "lease_id": lease["lease_id"], "thread_id": lease["thread_id"], "workspace": lease["workspace"],
        "expected_revision": "", "keep_reference_ids": [], "criteria": ["响应式无溢出"],
        "text_reference": "", "images": [], "depth": "frontend",
        "journeys": [
            {"id": "user-map-pan-1", "name": "拖拽产品地图后仍可点击产品", "required": True},
            {"id": "user-search-2", "name": "搜索产品并打开研究面板", "required": True},
        ],
    }
    result = visual_context_cli.update_references(payload)
    visual_mode = result["visual_mode"]
    assert visual_mode["depth"] == "frontend"
    assert [item["name"] for item in visual_mode["journeys"]] == [
        "拖拽产品地图后仍可点击产品", "搜索产品并打开研究面板"
    ]
    with pytest.raises(ValueError):
        visual_context_cli.update_references({**payload, "expected_revision": visual_mode["updated_at"], "depth": "agent"})


def test_reference_bridge_uploads_real_image_without_user_message(tmp_path):
    lease = workspace_lease(tmp_path)
    path=Path(lease["workspace"])/"input.png"
    data=png(path)
    result = visual_context_cli.update_references({
        "lease_id":lease["lease_id"], "thread_id":lease["thread_id"], "workspace":lease["workspace"],
        "expected_revision":"", "keep_reference_ids":[], "criteria":["Aligned grid"], "text_reference":"",
        "images":[{"name":"target.png", "data_base64":base64.b64encode(data).decode()}]})
    references = result["visual_mode"]["references"]
    assert len(references) == 1
    assert Path(references[0]["path"]).read_bytes() == data
    assert not server.UI_INPUT_FILE.exists()


def test_reference_edit_uses_compare_and_swap_and_does_not_overwrite(tmp_path):
    lease = workspace_lease(tmp_path)
    first = runtime.set_visual_mode(lease["lease_id"], criteria=["Original"])
    revision = first["visual_mode"]["updated_at"]
    runtime.set_visual_mode(lease["lease_id"], action="update", criteria=["Newer"])
    before = lease_record(lease)
    with pytest.raises(runtime.RuntimePolicyError):
        visual_context_cli.update_references({"lease_id":lease["lease_id"], "thread_id":lease["thread_id"],
            "workspace":lease["workspace"], "expected_revision":revision, "keep_reference_ids":[],
            "criteria":["Stale overwrite"], "text_reference":"", "images":[]})
    assert lease_record(lease) == before


def test_observation_storage_rolls_old_images_instead_of_blocking(tmp_path, monkeypatch):
    lease = workspace_lease(tmp_path)
    source = tmp_path / "rolling.png"
    data = png(source, (64, 64))
    monkeypatch.setattr(visual, "MAX_VISUAL_DISK_BYTES", len(data) + 16)

    first = visual.persist_image(lease["lease_id"], "observations", data, label="first")
    second = visual.persist_image(lease["lease_id"], "observations", data, label="second")

    assert not Path(first["path"]).exists()
    assert Path(second["path"]).is_file()
    assert second["pruned_files"] >= 1
    assert second["retained_bytes"] <= visual.MAX_VISUAL_DISK_BYTES


def test_visual_attachment_can_resolve_retained_file_after_metadata_rolloff(tmp_path):
    lease = workspace_lease(tmp_path)
    source = Path(lease["workspace"]) / "rolloff.png"
    data = png(source)
    observation = visual.persist_image(lease["lease_id"], "observations", data, label="rolloff")
    observation["inspected_at"] = runtime.utc_iso()
    runtime.record_visual_observation(lease["lease_id"], observation)

    stored = lease_record(lease)
    stored["visual_mode"]["observations"] = []
    runtime._write_json(runtime._lease_path(lease["lease_id"]), stored)

    result = raw_tool("read_ui_attachment", {
        "workspace_lease_id": lease["lease_id"],
        "attachment_id": "visual:" + observation["id"],
    })
    images = image_parts(result)
    assert len(images) == 1
    assert base64.b64decode(images[0]["data"]) == data


def test_console_watchdog_binds_top_level_root_without_children(tmp_path):
    lease = workspace_lease(tmp_path)
    payload = {
        "lease_id": lease["lease_id"],
        "thread_id": lease["thread_id"],
        "workspace": lease["workspace"],
        "watchdog_enabled": True,
    }
    state = visual_context_cli.update_references(payload)
    assert (tmp_path / "chatgpt-agent-data" / "gptagent.db").is_file()
    assert state["enabled"] is False
    assert state["manual_enabled"] is True
    assert state["manual_disabled"] is False
    assert state["root_lease_id"] == lease["lease_id"]

    disabled = visual_context_cli.update_references({**payload, "watchdog_enabled": False})
    assert disabled["enabled"] is False
    assert disabled["manual_enabled"] is False
    assert disabled["manual_disabled"] is True


def test_console_watchdog_reenable_starts_fresh_failure_episode(tmp_path, monkeypatch):
    from datetime import timedelta
    lease=workspace_lease(tmp_path)
    payload={
        'lease_id':lease['lease_id'],'thread_id':lease['thread_id'],'workspace':lease['workspace'],
        'watchdog_enabled':True,
    }
    first=visual_context_cli.update_references(payload)
    visual_context_cli.update_references({**payload,'watchdog_enabled':False})
    original_now=runtime.utc_now()
    monkeypatch.setattr(runtime,'utc_now',lambda:original_now+timedelta(seconds=123))
    second=visual_context_cli.update_references(payload)
    assert second['enabled'] is False and second['manual_enabled'] is True and second['manual_disabled'] is False
    assert second['started_at'] > first['started_at']


def test_console_watchdog_rejects_participant_toggle(tmp_path):
    from chatgpt_agent_store import AgentStore
    from machine_config import load_machine_config
    lease=workspace_lease(tmp_path)
    participant=runtime.attach_workspace_participant(
        lease['lease_id'],logical_agent_id='ag_console_participant',owner_label='participant-toggle-test')
    payload={
        'lease_id':participant['lease_id'],'thread_id':participant['thread_id'],
        'workspace':participant['workspace'],'watchdog_enabled':True,
    }
    with pytest.raises(runtime.RuntimePolicyError) as error:
        visual_context_cli.update_references(payload)
    assert error.value.error_type=='WATCHDOG_TOP_LEVEL_REQUIRED'
    assert not AgentStore(load_machine_config(runtime_root=str(tmp_path))).watchdog_scopes(enabled_only=True)
