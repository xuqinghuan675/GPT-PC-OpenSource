from __future__ import annotations

import asyncio
from concurrent.futures import ThreadPoolExecutor
import hashlib
import http.server
import io
import json
import shutil
import subprocess
import sys
import threading
import time
import uuid
from pathlib import Path

from mcp import Client
import pytest

import code_navigation as code_navigation_module
import runtime as runtime_module
import server as server_module
from server import PROJECT_ROOT, _decode_command_output, calculate_hash, mcp, write_text_file
from runtime import CONSOLE_RELAY_WORKSPACE, DESKTOP_OPS_WORKSPACE, RUNTIME_MAINTENANCE_WORKSPACE


EXPECTED_TOOLS = {
    "chatgpt_agent",
    "ultra_root",
    "ultra_agent",
    "ultra_recovery",
    "ultra_finalize",
    "camera",
    "acquire_workspace",
    "supersede_workspace_owner",
    "apply_stay_command",
    "set_visual_mode",
    "get_visual_context",
    "inspect_image",
    "capture_screenshot",
    "computer_use",
    "renew_workspace",
    "release_workspace",
    "report_progress",
    "gmail",
    "force_exit_stay_mode",
    "poll_ui_messages",
    "read_ui_attachment",
    "publish_thread_note",
    "read_thread_notes",
    "relay_console_user_message",
    "workspace_status",
    "runtime_status",
    "computer_info",
    "list_drives",
    "disk_usage",
    "get_environment",
    "list_processes",
    "process_info",
    "list_directory",
    "file_info",
    "read_text_file",
    "read_text_tail",
    "read_text_range",
    "read_multiple_files",
    "read_binary_file",
    "search_files",
    "search_text",
    "code_navigate",
    "save_chatgpt_upload",
    "create_file",
    "write_text_file",
    "append_text_file",
    "replace_text",
    "apply_patch",
    "copy_path",
    "move_path",
    "make_directory",
    "delete_path",
    "run_powershell",
    "run_python",
    "run_node",
    "run_npm",
    "run_npx",
    "git_init",
    "git_clone",
    "git_fetch",
    "git_show",
    "git_rev_parse",
    "git_remote",
    "git_status",
    "git_diff",
    "git_log",
    "git_add",
    "git_commit",
    "git_pull",
    "git_push",
    "git_branch",
    "git_switch",
    "git_restore",
    "wait_process",
    "http_probe",
    "start_process",
    "stop_process",
    "start_job",
    "get_job",
    "list_jobs",
    "tail_job",
    "cancel_job",
}

def call_tool(name: str, arguments: dict):
    async def run():
        async with Client(mcp) as client:
            return await client.call_tool(name, arguments)

    return asyncio.run(run()).structured_content


def raw_tool(name: str, arguments: dict):
    async def run():
        async with Client(mcp) as client:
            return await client.call_tool(name, arguments)

    return asyncio.run(run())


def acquire_execution_lease(workspace: Path) -> str:
    result = call_tool(
        "acquire_workspace",
        {
            "path": str(workspace),
            "mode": "execute_exclusive",
            "owner_label": "自动测试",
        },
    )
    return result["lease_id"]


def isolate_runtime(monkeypatch, tmp_path: Path) -> None:
    runtime_dir = tmp_path / "runtime"
    data_dir = tmp_path / "activity"
    monkeypatch.setattr(runtime_module, "RUNTIME_DIR", runtime_dir)
    monkeypatch.setattr(runtime_module, "LEASE_DIR", runtime_dir / "leases")
    monkeypatch.setattr(runtime_module, "JOB_DIR", runtime_dir / "jobs")
    monkeypatch.setattr(runtime_module, "JOB_LOG_DIR", tmp_path / "logs" / "jobs")
    monkeypatch.setattr(runtime_module, "METADATA_LOCK_PATH", runtime_dir / ".metadata.lock")
    monkeypatch.setattr(server_module, "ACTIVITY_DATA_DIR", data_dir)
    monkeypatch.setattr(server_module, "ACTIVITY_EVENT_FILE", data_dir / "events.jsonl")
    monkeypatch.setattr(server_module, "ACTIVITY_EVENT_BACKUP_FILE", data_dir / "events.previous.jsonl")
    monkeypatch.setattr(server_module, "UI_INPUT_FILE", data_dir / "ui-inbox.jsonl")
    monkeypatch.setattr(runtime_module, "CONSOLE_INPUT_FILE", data_dir / "ui-inbox.jsonl")
    media_dir = tmp_path / "media"
    monkeypatch.setattr(runtime_module, "MEDIA_ROOT", media_dir)
    monkeypatch.setattr(runtime_module, "MEDIA_UPLOAD_DIR", media_dir / "uploads")
    monkeypatch.setattr(runtime_module, "MEDIA_GMAIL_DIR", media_dir / "gmail")
    monkeypatch.setattr(runtime_module, "MEDIA_SCREENSHOT_DIR", media_dir / "screenshots")
    monkeypatch.setattr(runtime_module, "MEDIA_REFERENCE_DIR", media_dir / "references")
    monkeypatch.setattr(runtime_module, "MEDIA_ATTACHMENT_DIR", media_dir / "attachments")
    monkeypatch.setattr(runtime_module, "MEDIA_OUTBOX_DIR", media_dir / "outbox")
    monkeypatch.setattr(runtime_module, "VISUAL_DIR", media_dir / "visual")
    monkeypatch.setattr(server_module, "UI_INPUT_LOCK_FILE", data_dir / ".ui-inbox.lock")
    monkeypatch.setattr(server_module, "UI_ATTACHMENT_DIR", media_dir / "attachments")
    monkeypatch.setattr(server_module, "THREAD_NOTE_FILE", data_dir / "thread-notes.jsonl")
    runtime_module._ensure_runtime_dirs()


@pytest.fixture(autouse=True)
def isolated_runtime_metadata(monkeypatch, tmp_path):
    isolate_runtime(monkeypatch, tmp_path)
    yield


def test_tool_discovery_and_annotations():
    async def run():
        async with Client(mcp) as client:
            response = await client.list_tools()
            return response.tools

    tools = asyncio.run(run())
    assert {tool.name for tool in tools} == EXPECTED_TOOLS
    assert len(tools) == 82
    assert all(tool.description for tool in tools)
    assert all(tool.input_schema for tool in tools)
    delete_tool = next(tool for tool in tools if tool.name == "delete_path")
    assert delete_tool.annotations.destructive_hint is True
    read_tool = next(tool for tool in tools if tool.name == "read_text_file")
    assert read_tool.annotations.read_only_hint is True
    write_tool = next(tool for tool in tools if tool.name == "write_text_file")
    assert "workspace_lease_id" in write_tool.input_schema["properties"]
    progress_tool = next(tool for tool in tools if tool.name == "report_progress")
    assert "workspace_lease_id" in progress_tool.input_schema["properties"]
    acquire_tool = next(tool for tool in tools if tool.name == "acquire_workspace")
    assert "thread_id" in acquire_tool.input_schema["properties"]
    chatgpt_tool = next(tool for tool in tools if tool.name == "chatgpt_agent")
    chatgpt_props = chatgpt_tool.input_schema["properties"]
    assert "message_kind" in chatgpt_props
    assert {
        "message", "question", "answer", "finding", "blocker", "review_request", "handoff"
    }.issubset(set(chatgpt_props["message_kind"].get("enum", [])))
    assert "team_broadcast" in set(chatgpt_props["action"].get("enum", []))
    assert "refresh_turn" in set(chatgpt_props["action"].get("enum", []))
    assert "recover_member" in set(chatgpt_props["action"].get("enum", []))
    assert set(chatgpt_props["work_mode"].get("enum", [])) == {"bounded", "continuous"}
    assert set(chatgpt_props["recovery_mode"].get("enum", [])) == {"auto", "same_session", "migrate"}
    assert "estimated_minutes" in chatgpt_props
    for ultra_name in {"ultra_root", "ultra_agent", "ultra_recovery", "ultra_finalize"}:
        assert ultra_name in {tool.name for tool in tools}
    assert set(next(tool for tool in tools if tool.name == "ultra_root").input_schema["properties"]["action"]["enum"]) >= {"spawn", "task_create", "message"}
    assert set(next(tool for tool in tools if tool.name == "ultra_agent").input_schema["properties"]["action"]["enum"]) >= {"join", "resume", "task_update", "checkpoint"}
    assert set(next(tool for tool in tools if tool.name == "ultra_recovery").input_schema["properties"]["action"]["enum"]) >= {"recover_member", "root_rescue", "reconcile"}
    assert "notification_email" in next(tool for tool in tools if tool.name == "ultra_finalize").input_schema["properties"]
    camera_tool = next(tool for tool in tools if tool.name == "camera")
    assert set(camera_tool.input_schema["properties"]["action"]["enum"]) == {"list", "photo", "video"}
    assert "workspace_lease_id" in set(camera_tool.input_schema.get("required", []))
    assert "max_depth" in next(tool for tool in tools if tool.name == "list_directory").input_schema["properties"]
    assert "include_hash" in next(tool for tool in tools if tool.name == "read_text_file").input_schema["properties"]
    assert "hash_algorithm" in next(tool for tool in tools if tool.name == "file_info").input_schema["properties"]
    assert "shell" in next(tool for tool in tools if tool.name == "run_powershell").input_schema["properties"]
    retired = {"list_directory_tree", "read_text_file_with_hash", "calculate_hash", "rename_path", "run_cmd"}
    assert retired.isdisjoint({tool.name for tool in tools})
    capture_tool = next(tool for tool in tools if tool.name == "capture_screenshot")
    assert capture_tool.annotations.read_only_hint is False
    assert {"workspace_lease_id", "target", "browser_steps", "journey_id", "return_snapshot", "check_frontend_health"}.issubset(
        capture_tool.input_schema["properties"]
    )
    assert "target" in set(capture_tool.input_schema.get("required", []))
    assert "workspace_lease_id" not in set(capture_tool.input_schema.get("required", []))
    visual_tool = next(tool for tool in tools if tool.name == "set_visual_mode")
    assert {"goal_enabled", "goal_outcome", "goal_success_criteria", "goal_reuse_visual", "depth", "journeys"}.issubset(
        visual_tool.input_schema["properties"]
    )
    assert "goal_enabled" not in set(visual_tool.input_schema.get("required", []))
    upload_tool = next(tool for tool in tools if tool.name == "save_chatgpt_upload")
    assert upload_tool.annotations.read_only_hint is False
    assert upload_tool.annotations.open_world_hint is False
    upload_dump = upload_tool.model_dump(by_alias=True, exclude_none=True)
    assert upload_dump["_meta"]["openai/fileParams"] == ["file"]
    assert {"file", "filename", "overwrite"}.issubset(upload_tool.input_schema["properties"])
    assert "workspace_lease_id" not in upload_tool.input_schema["properties"]
    assert set(upload_tool.input_schema.get("required", [])) == {"file"}
    file_schema = upload_tool.input_schema["$defs"]["OpenAIProvidedFile"]
    assert set(file_schema["properties"]) == {"download_url", "file_id", "mime_type", "file_name"}
    assert set(file_schema["required"]) == {"download_url", "file_id"}
    gmail_tool = next(tool for tool in tools if tool.name == "gmail")
    assert gmail_tool.annotations.read_only_hint is False
    assert gmail_tool.annotations.destructive_hint is True
    assert gmail_tool.annotations.open_world_hint is True
    gmail_properties = gmail_tool.input_schema["properties"]
    assert {"action", "query", "message_id", "message_ids", "thread_id", "draft_id", "attachment_id", "filename", "output_filename", "to", "cc", "bcc", "subject", "body", "attachment_files", "label_id", "label_name", "label_ids_add", "label_ids_remove", "max_results", "include_body", "overwrite", "expected_account", "workspace_lease_id"}.issubset(gmail_properties)
    action_schema = gmail_properties["action"]
    action_values = set(action_schema.get("enum", []))
    for required_action in {"profile", "search", "get_message", "get_thread", "download_attachment", "send", "create_draft", "send_draft", "list_labels", "create_label", "modify_message_labels", "archive", "mark_read", "mark_unread", "trash", "untrash", "delete_forever", "batch_delete_forever"}:
        assert required_action in action_values
    poll_tool = next(tool for tool in tools if tool.name == "poll_ui_messages")
    assert "workspace_lease_id" in poll_tool.input_schema["properties"]
    assert "wait_seconds" in poll_tool.input_schema["properties"]
    relay_tool = next(tool for tool in tools if tool.name == "relay_console_user_message")
    relay_schema = relay_tool.input_schema["properties"]
    relay_required = set(relay_tool.input_schema.get("required", []))
    assert {"target_lease_id", "text", "user_authorized", "allow_task_control", "workspace_lease_id"}.issubset(relay_schema)
    assert {"target_lease_id", "text", "user_authorized", "workspace_lease_id"}.issubset(relay_required)


def test_camera_photo_and_video_return_visual_content_without_real_hardware(monkeypatch):
    import cv2
    import numpy as np

    frame = np.zeros((48, 64, 3), dtype=np.uint8)
    frame[:, :, 1] = 180

    class FakeCap:
        def read(self):
            return True, frame.copy()

        def get(self, prop):
            return 10.0

        def release(self):
            return None

    class FakeWriter:
        def __init__(self, path):
            self.path = Path(path)
            self.opened = True

        def isOpened(self):
            return self.opened

        def write(self, _frame):
            return None

        def release(self):
            self.path.parent.mkdir(parents=True, exist_ok=True)
            self.path.write_bytes(b"FAKE_AVI")

    monkeypatch.setattr(
        server_module,
        "_camera_open",
        lambda device_index, width, height, fps: (cv2, FakeCap(), "fake", frame.copy()),
    )
    monkeypatch.setattr(
        cv2,
        "VideoWriter",
        lambda path, fourcc, fps, size: FakeWriter(path),
    )

    lease = call_tool(
        "acquire_workspace",
        {
            "path": str(DESKTOP_OPS_WORKSPACE),
            "mode": "execute_exclusive",
            "owner_label": "Camera fake test",
        },
    )
    lease_id = lease["lease_id"]

    async def raw_camera(arguments):
        async with Client(mcp) as client:
            return await client.call_tool("camera", arguments)

    try:
        photo = asyncio.run(raw_camera({
            "action": "photo",
            "workspace_lease_id": lease_id,
            "device_index": 0,
        }))
        assert photo.is_error is False
        assert any(getattr(item, "type", "") == "image" for item in photo.content)
        assert any('"action": "photo"' in getattr(item, "text", "") for item in photo.content)

        video = asyncio.run(raw_camera({
            "action": "video",
            "workspace_lease_id": lease_id,
            "device_index": 0,
            "duration_seconds": 1,
            "fps": 2,
            "sample_frames": 2,
        }))
        assert video.is_error is False
        images = [item for item in video.content if getattr(item, "type", "") == "image"]
        assert len(images) >= 2
        metadata = next(getattr(item, "text", "") for item in video.content if '"action": "video"' in getattr(item, "text", ""))
        assert '"audio": false' in metadata
        assert '"sample_frames": 2' in metadata
    finally:
        call_tool("release_workspace", {"workspace_lease_id": lease_id})


def test_camera_requires_desktop_ops_lease(tmp_path):
    lease_id = acquire_execution_lease(tmp_path)

    async def raw():
        async with Client(mcp) as client:
            return await client.call_tool(
                "camera",
                {"action": "list", "workspace_lease_id": lease_id},
            )

    try:
        blocked = asyncio.run(raw())
        assert blocked.is_error is True
        assert "DESKTOP_OPS_LEASE_REQUIRED" in blocked.content[0].text
    finally:
        call_tool("release_workspace", {"workspace_lease_id": lease_id})


def test_coding_hot_path_tool_descriptions_are_disambiguated():
    async def run():
        async with Client(mcp) as client:
            response = await client.list_tools()
            return {tool.name: tool.description for tool in response.tools}

    descriptions = asyncio.run(run())
    expected_cross_refs = {
        "search_files": {"search_text", "code_navigate"},
        "search_text": {"search_files", "code_navigate"},
        "code_navigate": {"search_text", "read_text_range"},
        "read_text_file": {"read_text_range", "read_multiple_files"},
        "read_text_range": {"search_text", "code_navigate"},
        "read_multiple_files": {"read_text_range"},
        "replace_text": {"apply_patch"},
        "apply_patch": {"replace_text"},
        "run_powershell": {"npm", "Python", "Git"},
        "run_python": {"start_job"},
        "run_node": {"run_npm", "run_npx", "start_job"},
        "run_npm": {"start_job"},
        "run_npx": {"run_npm", "run_node", "start_job"},
        "start_job": {"start_process"},
        "start_process": {"start_job"},
    }
    for tool_name, needles in expected_cross_refs.items():
        description = descriptions[tool_name]
        for needle in needles:
            assert needle in description, (tool_name, needle, description)


@pytest.mark.parametrize(
    ("command", "dedicated_tool"),
    [
        ("npm test", "run_npm"),
        ("npx vitest", "run_npx"),
        (r"D:\\venv\\Scripts\\python.exe -m pytest", "run_python"),
        ("node script.js", "run_node"),
        ("apply_patch patch.txt", "apply_patch"),
    ],
)
def test_simple_shell_command_routing_hint_points_to_dedicated_tool(command, dedicated_tool):
    hint = server_module._dedicated_command_routing_hint(command)
    assert hint is not None
    assert dedicated_tool in hint


def test_shell_routing_hint_does_not_interfere_with_real_shell_composition():
    assert server_module._dedicated_command_routing_hint("npm test | Tee-Object test.log") is None
    assert server_module._dedicated_command_routing_hint("Get-ChildItem -Recurse") is None


def test_run_powershell_keeps_semantics_and_adds_non_blocking_routing_hint(tmp_path):
    lease_id = acquire_execution_lease(tmp_path)
    result = call_tool(
        "run_powershell",
        {
            "command": "python -c \"print('routing-ok')\"",
            "cwd": str(tmp_path),
            "workspace_lease_id": lease_id,
        },
    )
    assert result["exit_code"] == 0
    assert "routing-ok" in result["stdout"]
    assert "run_python" in result["routing_hint"]


def test_chatgpt_file_param_bridge_saves_exact_bytes_to_media_without_lease(monkeypatch):
    payload = b"\x89PNG\r\n\x1a\nexact-chatgpt-upload-bytes"

    class FakeResponse:
        def __init__(self):
            self.stream = io.BytesIO(payload)
        def read(self, size=-1):
            return self.stream.read(size)
        def __enter__(self):
            return self
        def __exit__(self, exc_type, exc, tb):
            self.stream.close(); return False

    monkeypatch.setattr(server_module.urllib.request, "urlopen", lambda request, timeout=60: FakeResponse())
    result = call_tool(
        "save_chatgpt_upload",
        {
            "file": {
                "file_id": "file-test",
                "download_url": "https://oaisdmntprkoreacentral.blob.core.windows.net/test/upload.png",
                "file_name": "source.png",
                "mime_type": "image/png",
            },
            "filename": "saved.png",
        },
    )
    target = runtime_module.MEDIA_UPLOAD_DIR / "saved.png"
    assert target.read_bytes() == payload
    assert result["status"] == "saved"
    assert result["path"] == str(target.resolve(strict=False))
    assert result["bytes_written"] == len(payload)
    assert result["sha256"] == hashlib.sha256(payload).hexdigest()
    assert result["source_file_id"] == "file-test"
    assert result["mime_type"] == "image/png"


def test_chatgpt_file_param_bridge_maps_stale_openai_azure_url_to_retryable_error(monkeypatch):
    url = "https://oaisdmntprkoreacentral.blob.core.windows.net/test/stale.png"

    def stale_urlopen(request, timeout=60):
        raise server_module.urllib.error.HTTPError(
            request.full_url,
            403,
            "Forbidden",
            hdrs=None,
            fp=None,
        )

    monkeypatch.setattr(server_module.urllib.request, "urlopen", stale_urlopen)

    async def run():
        async with Client(mcp) as client:
            return await client.call_tool(
                "save_chatgpt_upload",
                {
                    "file": {
                        "file_id": "file-stale-azure",
                        "download_url": url,
                        "file_name": "stale.png",
                        "mime_type": "image/png",
                    }
                },
            )

    result = asyncio.run(run())
    assert result.is_error is True
    assert "CHATGPT_FILE_URL_STALE" in result.content[0].text
    assert "重新调用一次 save_chatgpt_upload" in result.content[0].text


def test_chatgpt_file_param_bridge_rejects_unrelated_azure_blob_host(monkeypatch):
    def unexpected_urlopen(*args, **kwargs):
        raise AssertionError("untrusted Azure host must be rejected before network access")

    monkeypatch.setattr(server_module.urllib.request, "urlopen", unexpected_urlopen)

    async def run():
        async with Client(mcp) as client:
            return await client.call_tool(
                "save_chatgpt_upload",
                {
                    "file": {
                        "file_id": "file-untrusted-azure",
                        "download_url": "https://unrelatedaccount.blob.core.windows.net/test.png",
                        "file_name": "blocked.png",
                        "mime_type": "image/png",
                    }
                },
            )

    result = asyncio.run(run())
    assert result.is_error is True
    assert "download_url 必须是 ChatGPT/OpenAI 提供的 HTTPS 文件地址" in result.content[0].text


def test_unified_gmail_bridge_uses_full_oauth_scope_and_api_surface():
    script = PROJECT_ROOT / "scripts" / "gmail-bridge.py"
    text = script.read_text(encoding="utf-8")
    assert "InstalledAppFlow.from_client_secrets_file" in text
    assert "https://mail.google.com/" in text
    assert '.messages().list(userId="me", q=query' in text
    assert '.messages().get(userId="me", id=message_id, format="full")' in text
    assert ".messages().attachments().get(" in text
    assert '.messages().send(userId="me"' in text
    assert '.drafts().create(userId="me"' in text
    assert '.drafts().send(userId="me"' in text
    assert '.labels().create(userId="me"' in text
    assert '.messages().modify(userId="me"' in text
    assert '.messages().batchModify(userId="me"' in text
    assert '.messages().trash(userId="me"' in text
    assert '.messages().delete(userId="me"' in text
    assert "MAX_ATTACHMENT_BYTES = 50 * 1024 * 1024" in text
    assert "hashlib.sha256" in text

    server_text = (PROJECT_ROOT / "pc-mcp" / "server.py").read_text(encoding="utf-8")
    assert 'PROJECT_ROOT / "scripts" / "gmail-bridge.py"' in server_text
    assert "def gmail(" in server_text
    assert "def search_gmail_messages(" not in server_text
    assert "def save_gmail_attachment_to_desktop(" not in server_text
    assert "def send_gmail_attachment(" not in server_text


def test_unified_gmail_search_attachment_and_action_routing(monkeypatch):
    attachment_payload = b"original-gmail-attachment-bytes\x00\xff"
    seen: list[dict] = []

    def fake_bridge(request_payload, *, timeout_seconds):
        seen.append(dict(request_payload))
        assert timeout_seconds >= 15
        action = request_payload["action"]
        if action == "search":
            return {"status": "ok", "account": "user@example.invalid", "query": request_payload["query"],
                    "messages": [{"message_id": "msg-1", "subject": "Attachment test", "attachment_count": 1,
                                  "attachments": [{"filename": "original.bin", "mime_type": "application/octet-stream",
                                                   "attachment_id": "att-1", "size": len(attachment_payload)}]}], "count": 1}
        if action == "download_attachment":
            target = Path(request_payload["output_path"]); target.write_bytes(attachment_payload)
            return {"status": "saved", "account": "user@example.invalid", "message_id": request_payload["message_id"],
                    "source_filename": "original.bin", "mime_type": "application/octet-stream", "attachment_id": "att-1",
                    "path": str(target), "bytes_written": len(attachment_payload),
                    "sha256": hashlib.sha256(attachment_payload).hexdigest()}
        if action == "mark_read":
            return {"status": "marked_read", "account": "user@example.invalid", "message_id": request_payload["message_id"]}
        raise AssertionError(f"unexpected action: {action}")

    monkeypatch.setattr(server_module, "_run_gmail_bridge", fake_bridge)
    search = call_tool("gmail", {"action": "search", "query": "newer_than:1d has:attachment", "max_results": 3})
    assert search["status"] == "ok"
    assert search["messages"][0]["message_id"] == "msg-1"

    saved = call_tool("gmail", {"action": "download_attachment", "message_id": "msg-1",
                                 "attachment_id": "att-1", "filename": "original.bin",
                                 "output_filename": "gmail-attachment.bin"})
    target = runtime_module.MEDIA_GMAIL_DIR / "gmail-attachment.bin"
    assert target.read_bytes() == attachment_payload
    assert saved["status"] == "saved"
    assert saved["sha256"] == hashlib.sha256(attachment_payload).hexdigest()

    async def mark_without_lease():
        async with Client(mcp) as client:
            return await client.call_tool("gmail", {"action": "mark_read", "message_id": "msg-1"})
    blocked = asyncio.run(mark_without_lease())
    assert blocked.is_error is True
    assert "LEASE_REQUIRED" in blocked.content[0].text

    lease = call_tool("acquire_workspace", {"path": str(DESKTOP_OPS_WORKSPACE), "mode": "execute_exclusive",
                                             "owner_label": "Gmail mutation boundary test"})
    try:
        marked = call_tool("gmail", {"action": "mark_read", "message_id": "msg-1", "workspace_lease_id": lease["lease_id"]})
        assert marked["status"] == "marked_read"
    finally:
        call_tool("release_workspace", {"workspace_lease_id": lease["lease_id"]})
    assert [item["action"] for item in seen] == ["search", "download_attachment", "mark_read"]


def test_gmail_send_normalizes_non_media_attachment_into_outbox(monkeypatch, tmp_path):
    source = tmp_path / "outside.png"
    payload = b"outbox-normalization"
    source.write_bytes(payload)
    captured: dict[str, str] = {}

    def fake_bridge(request_payload, *, timeout_seconds):
        assert request_payload["action"] == "send"
        assert timeout_seconds >= 15
        attachment = Path(request_payload["attachment_files"][0])
        assert attachment.is_relative_to(runtime_module.MEDIA_OUTBOX_DIR)
        assert attachment.read_bytes() == payload
        captured["path"] = str(attachment)
        return {
            "status": "sent",
            "verified": True,
            "account": "user@example.invalid",
            "message_id": "sent-1",
            "thread_id": "thread-1",
            "attachments": [attachment.name],
        }

    monkeypatch.setattr(server_module, "_run_gmail_bridge", fake_bridge)
    lease = call_tool("acquire_workspace", {
        "path": str(DESKTOP_OPS_WORKSPACE),
        "mode": "execute_exclusive",
        "owner_label": "Gmail outbox normalization test",
    })
    try:
        result = call_tool("gmail", {
            "action": "send",
            "to": "receiver@example.com",
            "subject": "Media outbox test",
            "body": "test",
            "attachment_files": [str(source)],
            "workspace_lease_id": lease["lease_id"],
        })
        assert result["status"] == "sent"
        assert result["verified"] is True
        assert Path(captured["path"]).is_file()
    finally:
        call_tool("release_workspace", {"workspace_lease_id": lease["lease_id"]})


def test_unified_gmail_attachment_id_only_requires_output_filename():
    async def run():
        async with Client(mcp) as client:
            return await client.call_tool("gmail", {"action": "download_attachment", "message_id": "msg-1", "attachment_id": "att-1"})
    result = asyncio.run(run())
    assert result.is_error is True
    assert "output_filename" in result.content[0].text

def test_runtime_status_returns_only_active_jobs_with_history_count(monkeypatch):
    monkeypatch.setattr(
        server_module,
        "_list_active_jobs",
        lambda: {"jobs": [{"job_id": "running-job", "status": "running"}], "count": 1},
    )
    monkeypatch.setattr(server_module, "_job_history_count", lambda: 2)
    monkeypatch.setattr(server_module, "_workspace_status", lambda *args, **kwargs: {"leases": [], "count": 0})
    status = server_module.runtime_status()
    assert status["jobs"]["count"] == 1
    assert status["jobs"]["history_count"] == 2
    assert [job["job_id"] for job in status["jobs"]["jobs"]] == ["running-job"]


def test_activity_event_redaction_and_rotation_path(monkeypatch, tmp_path):
    data_dir = tmp_path / "activity"
    event_file = data_dir / "events.jsonl"
    backup_file = data_dir / "events.previous.jsonl"
    monkeypatch.setattr(server_module, "ACTIVITY_DATA_DIR", data_dir)
    monkeypatch.setattr(server_module, "ACTIVITY_EVENT_FILE", event_file)
    monkeypatch.setattr(server_module, "ACTIVITY_EVENT_BACKUP_FILE", backup_file)

    redacted = server_module._activity_redact_text(
        "curl -H 'Authorization: " + "Bearer " + "test-only-token-fixture' --api-key test-only-secret-fixture"
    )
    assert "test-only-token-fixture" not in redacted
    assert "test-only-secret-fixture" not in redacted
    assert "[REDACTED]" in redacted

    server_module._activity_emit(
        "tool.started",
        call_id="test-call",
        tool="run_powershell",
        arguments={"command": redacted},
    )
    rows = [json.loads(line) for line in event_file.read_text(encoding="utf-8").splitlines()]
    assert rows[0]["type"] == "tool.started"
    assert rows[0]["tool"] == "run_powershell"
    assert "test-only-token-fixture" not in json.dumps(rows, ensure_ascii=False)

    hardened = server_module._activity_redact_output(
        "jwt=aaaaaaaaaaa.bbbbbbbbbbb.ccccccccccc\n"
        "-----BEGIN " + "PRIVATE" + " KEY-----\nTEST_ONLY_KEY_FIXTURE_DATA\n-----END " + "PRIVATE" + " KEY-----\n"
        + ("Ab9_" * 30)
    )
    assert "aaaaaaaaaaa.bbbbbbbbbbb.ccccccccccc" not in hardened
    assert "TEST_ONLY_KEY_FIXTURE_DATA" not in hardened
    assert "Ab9_" * 20 not in hardened


def test_activity_release_keeps_explicit_task_id_when_lease_snapshot_is_unavailable(monkeypatch):
    def fake_release_workspace(workspace_lease_id: str):
        return workspace_lease_id

    monkeypatch.setattr(server_module, "_workspace_status", lambda *args, **kwargs: {"leases": []})
    lease = server_module._activity_find_lease(
        fake_release_workspace,
        (),
        {"workspace_lease_id": "lease-task-123"},
    )
    assert lease == {"lease_id": "lease-task-123"}


def test_activity_rotation_retains_multiple_history_segments(monkeypatch, tmp_path):
    data_dir = tmp_path / "activity-history"
    monkeypatch.setattr(server_module, "ACTIVITY_DATA_DIR", data_dir)
    monkeypatch.setattr(server_module, "ACTIVITY_EVENT_FILE", data_dir / "events.jsonl")
    monkeypatch.setattr(server_module, "ACTIVITY_EVENT_BACKUP_FILE", data_dir / "events.previous.jsonl")
    monkeypatch.setattr(server_module, "ACTIVITY_EVENT_MAX_BYTES", 1)
    monkeypatch.setattr(server_module, "ACTIVITY_EVENT_HISTORY_COUNT", 3)

    for index in range(6):
        server_module._activity_emit("tool.started", call_id=f"rotation-{index}", tool="probe")

    histories = [
        path
        for path in data_dir.glob("events*.jsonl")
        if path != server_module.ACTIVITY_EVENT_FILE
    ]
    assert server_module.ACTIVITY_EVENT_FILE.exists()
    assert server_module.ACTIVITY_EVENT_BACKUP_FILE.exists()
    assert any(path.name not in {"events.jsonl", "events.previous.jsonl"} for path in histories)
    assert len(histories) == 3


def test_jsonl_console_parser_preserves_unicode_line_separators(tmp_path):
    inbox = tmp_path / "ui-inbox.jsonl"
    text = "进程壳\u2028日志\u2029下一段\u0085尾部"
    row = {
        "id": "unicode-line-separators",
        "type": "ui.message",
        "scope": "task",
        "threadId": "thread-unicode-lines",
        "taskId": "task-unicode-lines",
        "timestampMs": 1,
        "text": text,
    }
    inbox.write_text(json.dumps(row, ensure_ascii=False) + "\n", encoding="utf-8")

    rows = server_module._jsonl_tail(inbox)
    assert len(rows) == 1
    assert rows[0]["id"] == "unicode-line-separators"
    assert rows[0]["text"] == text


def test_unicode_line_separator_console_message_blocks_next_side_effect(tmp_path):
    work_dir = PROJECT_ROOT / f"unicode-control-gate-{uuid.uuid4().hex}"
    target = work_dir / "blocked.txt"
    work_dir.mkdir(parents=True)
    lease_id = None
    try:
        acquired = call_tool(
            "acquire_workspace",
            {"path": str(work_dir), "mode": "execute_exclusive", "owner_label": "Unicode Console 门禁测试"},
        )
        lease_id = acquired["lease_id"]
        thread_id = acquired["thread_id"]
        text = "进程壳\u2028日志、实例 ID\u2029继续检查"
        server_module.UI_INPUT_FILE.parent.mkdir(parents=True, exist_ok=True)
        server_module.UI_INPUT_FILE.write_text(
            json.dumps(
                {
                    "id": "unicode-gate-message",
                    "type": "ui.message",
                    "scope": "task",
                    "threadId": thread_id,
                    "taskId": lease_id,
                    "timestampMs": 50,
                    "text": text,
                },
                ensure_ascii=False,
            ) + "\n",
            encoding="utf-8",
        )

        async def blocked_write():
            async with Client(mcp) as client:
                return await client.call_tool(
                    "write_text_file",
                    {"path": str(target), "content": "must-not-run", "workspace_lease_id": lease_id},
                )

        blocked = asyncio.run(blocked_write())
        assert blocked.is_error is True
        assert "用户控制输入待处理" in blocked.content[0].text
        assert not target.exists()

        polled = call_tool("poll_ui_messages", {"workspace_lease_id": lease_id})
        assert [item["id"] for item in polled["messages"]] == ["unicode-gate-message"]
        assert polled["messages"][0]["text"] == text
        assert polled["control_ack_required"] is True
        assert polled["control_resume_required"] is True
    finally:
        if lease_id and runtime_module.workspace_status(work_dir)["count"]:
            runtime_module.update_workspace_control_ack_pending(lease_id, None)
            runtime_module.update_workspace_control_resume_pending(lease_id, None)
            runtime_module.release_workspace(lease_id)
        shutil.rmtree(work_dir, ignore_errors=True)


def test_thread_id_ui_inbox_and_sibling_coordination(monkeypatch, tmp_path):
    first_workspace = tmp_path / "workspace-a"
    second_workspace = tmp_path / "workspace-b"
    first_workspace.mkdir()
    second_workspace.mkdir()

    first = runtime_module.acquire_workspace(
        first_workspace,
        mode="execute_exclusive",
        owner_label="线程测试 A",
    )
    thread_id = first["thread_id"]
    assert thread_id.startswith("thread-")
    second = runtime_module.acquire_workspace(
        second_workspace,
        mode="execute_exclusive",
        owner_label="线程测试 B",
        thread_id=thread_id,
    )
    assert second["thread_id"] == thread_id

    public_status = runtime_module.workspace_status()
    assert {item["lease_id"] for item in public_status["leases"]} == {first["lease_id"], second["lease_id"]}
    assert all("thread_id" not in item for item in public_status["leases"])
    private_status = runtime_module.workspace_status(include_private=True)
    assert {item["thread_id"] for item in private_status["leases"]} == {thread_id}

    server_module.UI_INPUT_FILE.parent.mkdir(parents=True, exist_ok=True)
    messages = [
        {"id": "m1", "type": "ui.message", "scope": "task", "threadId": thread_id, "taskId": first["lease_id"], "timestampMs": 1, "text": "A 先检查日志"},
        {"id": "other", "type": "ui.message", "scope": "task", "threadId": "thread-other", "taskId": "other", "timestampMs": 2, "text": "不应串线"},
        {"id": "m2", "type": "ui.message", "scope": "task", "threadId": thread_id, "taskId": second["lease_id"], "timestampMs": 3, "text": "B 改成先做验证"},
    ]
    server_module.UI_INPUT_FILE.write_text(
        "".join(json.dumps(item, ensure_ascii=False) + "\n" for item in messages),
        encoding="utf-8",
    )

    first_poll = server_module.poll_ui_messages(workspace_lease_id=first["lease_id"])
    assert [item["id"] for item in first_poll["messages"]] == ["m1"]
    assert first_poll["scope"] == "task"
    call_tool(
        "report_progress",
        {"phase": "progress", "text": "A 已看到控制消息。", "workspace_lease_id": first["lease_id"]},
    )
    assert server_module.poll_ui_messages(workspace_lease_id=first["lease_id"])["messages"] == []

    second_poll = server_module.poll_ui_messages(workspace_lease_id=second["lease_id"])
    assert [item["id"] for item in second_poll["messages"]] == ["m2"]
    assert all(item["source_task_id"] == second["lease_id"] for item in second_poll["messages"])
    call_tool(
        "report_progress",
        {"phase": "progress", "text": "B 已看到控制消息。", "workspace_lease_id": second["lease_id"]},
    )
    reset_poll = server_module.poll_ui_messages(
        workspace_lease_id=second["lease_id"],
        after_id="compacted-away-cursor",
    )
    assert reset_poll["cursor_reset"] is True
    assert reset_poll["messages"] == []
    assert reset_poll["next_after_id"] == "m2"

    published = server_module.publish_thread_note(
        workspace_lease_id=first["lease_id"],
        kind="handoff",
        text="A 已完成数据检查，B 继续验证。",
    )
    assert published["thread_id"] == thread_id
    notes = server_module.read_thread_notes(workspace_lease_id=second["lease_id"])
    assert notes["notes"][0]["text"] == "A 已完成数据检查，B 继续验证。"
    assert notes["sibling_count"] == 2
    assert {item["lease_id"] for item in notes["sibling_leases"]} == {first["lease_id"], second["lease_id"]}

    runtime_module.release_workspace(first["lease_id"])
    after_release = server_module.read_thread_notes(workspace_lease_id=second["lease_id"])
    assert after_release["notes"] == []
    assert after_release["sibling_count"] == 1
    runtime_module.release_workspace(second["lease_id"])


def test_console_relay_requires_zero_permission_sender_identity_and_user_authorization(tmp_path):
    target_workspace = tmp_path / "relay-target"
    target_workspace.mkdir()
    target = runtime_module.acquire_workspace(
        target_workspace,
        mode="execute_exclusive",
        owner_label="中继目标",
    )
    try:
        async def raw(arguments):
            async with Client(mcp) as client:
                return await client.call_tool("relay_console_user_message", arguments)

        missing_sender = asyncio.run(raw({
            "target_lease_id": target["lease_id"],
            "text": "无发送身份不应送达",
            "user_authorized": True,
        }))
        assert missing_sender.is_error is True
        assert not server_module.UI_INPUT_FILE.exists()

        sender = runtime_module.acquire_workspace(
            CONSOLE_RELAY_WORKSPACE,
            mode="read_shared",
            owner_label="独立远程监视传话筒",
        )
        try:
            unauthorized = asyncio.run(raw({
                "target_lease_id": target["lease_id"],
                "text": "未获用户授权不应送达",
                "user_authorized": False,
                "workspace_lease_id": sender["lease_id"],
            }))
            assert unauthorized.is_error is True
            assert "缺少用户授权" in unauthorized.content[0].text
            assert not server_module.UI_INPUT_FILE.exists()
        finally:
            runtime_module.release_workspace(sender["lease_id"])
    finally:
        runtime_module.release_workspace(target["lease_id"])


def test_console_relay_identity_is_cross_thread_and_has_no_execution_permission(tmp_path):
    target_workspace = tmp_path / "relay-cross-thread-target"
    source_workspace = tmp_path / "relay-project-source"
    target_workspace.mkdir()
    source_workspace.mkdir()
    target = runtime_module.acquire_workspace(
        target_workspace,
        mode="execute_exclusive",
        owner_label="目标任务",
    )
    with pytest.raises(runtime_module.RuntimePolicyError) as self_identity_error:
        runtime_module.acquire_workspace(
            CONSOLE_RELAY_WORKSPACE,
            mode="read_shared",
            owner_label="同对话伪装传话身份",
            thread_id=target["thread_id"],
            runtime_instance="test-runtime",
        )
    assert self_identity_error.value.error_type == "CONSOLE_RELAY_SELF_IDENTITY"

    sender = runtime_module.acquire_workspace(
        CONSOLE_RELAY_WORKSPACE,
        mode="read_shared",
        owner_label="另一对话远程监视",
    )
    with pytest.raises(runtime_module.RuntimePolicyError) as reverse_identity_error:
        runtime_module.acquire_workspace(
            source_workspace,
            mode="execute_exclusive",
            owner_label="同对话不应转为执行任务",
            thread_id=sender["thread_id"],
            runtime_instance="test-runtime",
        )
    assert reverse_identity_error.value.error_type == "CONSOLE_RELAY_IDENTITY_CONFLICT"

    project_sender = runtime_module.acquire_workspace(
        source_workspace,
        mode="execute_exclusive",
        owner_label="项目执行来源",
    )
    try:
        async def raw(arguments):
            async with Client(mcp) as client:
                return await client.call_tool("relay_console_user_message", arguments)

        project_blocked = asyncio.run(raw({
            "target_lease_id": target["lease_id"],
            "text": "项目执行租约不能当发送身份",
            "user_authorized": True,
            "workspace_lease_id": project_sender["lease_id"],
        }))
        assert project_blocked.is_error is True
        assert "发送身份不允许" in project_blocked.content[0].text

        execute_blocked = asyncio.run(raw({
            "target_lease_id": target["lease_id"],
            "text": "先做这个",
            "user_authorized": True,
            "workspace_lease_id": sender["lease_id"],
        }))
        assert execute_blocked.is_error is False
        assert execute_blocked.structured_content["status"] == "sent"
        assert execute_blocked.structured_content["identity_assurance"] == "console_relay_cross_thread"

        rows = server_module._jsonl_tail(server_module.UI_INPUT_FILE)
        assert len(rows) == 1
        assert rows[0]["relay"]["senderLeaseId"] == sender["lease_id"]
        assert rows[0]["relay"]["senderThreadId"] == sender["thread_id"]

        async def try_command_with_relay_identity():
            async with Client(mcp) as client:
                return await client.call_tool(
                    "run_powershell",
                    {
                        "command": "echo SHOULD_NOT_RUN",
                        "cwd": str(CONSOLE_RELAY_WORKSPACE),
                        "workspace_lease_id": sender["lease_id"],
                    },
                )

        command_blocked = asyncio.run(try_command_with_relay_identity())
        assert command_blocked.is_error is True
        assert "只读租约" in command_blocked.content[0].text
    finally:
        runtime_module.release_workspace(project_sender["lease_id"])
        runtime_module.release_workspace(sender["lease_id"])
        runtime_module.release_workspace(target["lease_id"])


def test_console_relay_blocks_lifecycle_control_without_explicit_elevation(tmp_path):
    target_workspace = tmp_path / "relay-control-target"
    target_workspace.mkdir()
    target = runtime_module.acquire_workspace(
        target_workspace,
        mode="execute_exclusive",
        owner_label="控制目标",
    )
    sender = runtime_module.acquire_workspace(
        CONSOLE_RELAY_WORKSPACE,
        mode="read_shared",
        owner_label="控制消息远程发送方",
    )
    try:
        async def raw(arguments):
            async with Client(mcp) as client:
                return await client.call_tool("relay_console_user_message", arguments)

        blocked = asyncio.run(raw({
            "target_lease_id": target["lease_id"],
            "text": "停止当前任务并释放租约",
            "user_authorized": True,
            "workspace_lease_id": sender["lease_id"],
        }))
        assert blocked.is_error is True
        assert "需要更高控制授权" in blocked.content[0].text
        assert not server_module.UI_INPUT_FILE.exists()

        allowed = call_tool(
            "relay_console_user_message",
            {
                "target_lease_id": target["lease_id"],
                "text": "停止当前任务并释放租约",
                "user_authorized": True,
                "allow_task_control": True,
                "workspace_lease_id": sender["lease_id"],
            },
        )
        assert allowed["status"] == "sent"
        assert allowed["task_control_allowed"] is True
        received = call_tool("poll_ui_messages", {"workspace_lease_id": target["lease_id"]})
        assert received["messages"][0]["text"] == "停止当前任务并释放租约"
        assert received["control_resume_required"] is False
    finally:
        runtime_module.release_workspace(sender["lease_id"])
        if runtime_module.workspace_status(target_workspace)["count"]:
            runtime_module.update_workspace_control_ack_pending(target["lease_id"], None)
            runtime_module.update_workspace_control_resume_pending(target["lease_id"], None)
            runtime_module.release_workspace(target["lease_id"])


def test_console_relay_coexists_with_runtime_admin_and_stay_commands_are_verbatim():
    first_sender = runtime_module.acquire_workspace(
        CONSOLE_RELAY_WORKSPACE,
        mode="read_shared",
        owner_label="管理员任务远程监视 A",
    )
    admin = None
    second_sender = None
    try:
        admin = runtime_module.acquire_workspace(
            RUNTIME_MAINTENANCE_WORKSPACE,
            mode="execute_exclusive",
            owner_label="管理员目标任务",
        )
        second_sender = runtime_module.acquire_workspace(
            CONSOLE_RELAY_WORKSPACE,
            mode="read_shared",
            owner_label="管理员任务远程监视 B",
        )
        assert admin["lease_type"] == "runtime_admin"
        assert first_sender["lease_type"] == "console_relay"
        assert second_sender["lease_type"] == "console_relay"

        entered = call_tool("relay_console_user_message", {
            "target_lease_id": admin["lease_id"],
            "text": "/stay 30s",
            "user_authorized": True,
            "allow_task_control": True,
            "workspace_lease_id": second_sender["lease_id"],
        })
        assert entered["status"] == "sent"
        first = call_tool("poll_ui_messages", {"workspace_lease_id": admin["lease_id"]})
        assert first["messages"][0]["text"] == "/stay 30s"
        assert first["stay_mode"]["status"] == "entered"
        assert first["stay_mode"]["active"] is True

        exited = call_tool("relay_console_user_message", {
            "target_lease_id": admin["lease_id"],
            "text": "/stay off",
            "user_authorized": True,
            "allow_task_control": True,
            "workspace_lease_id": second_sender["lease_id"],
        })
        assert exited["status"] == "sent"
        second = call_tool("poll_ui_messages", {"workspace_lease_id": admin["lease_id"]})
        assert second["messages"][0]["text"] == "/stay off"
        assert second["stay_mode"]["status"] == "exited"
        assert second["stay_mode"]["active"] is False
    finally:
        if admin is not None:
            runtime_module.update_workspace_control_ack_pending(admin["lease_id"], None)
            runtime_module.update_workspace_control_resume_pending(admin["lease_id"], None)
        if second_sender is not None:
            runtime_module.release_workspace(second_sender["lease_id"])
        if admin is not None:
            runtime_module.release_workspace(admin["lease_id"])
        runtime_module.release_workspace(first_sender["lease_id"])


def test_empty_control_queue_with_stale_cursor_is_safe(tmp_path):
    workspace = tmp_path / "empty-control-queue"
    workspace.mkdir()
    lease = runtime_module.acquire_workspace(
        workspace,
        mode="execute_exclusive",
        owner_label="空控制队列测试",
    )
    try:
        runtime_module.update_workspace_control_cursor(lease["lease_id"], "gone-message-id")
        refreshed = runtime_module.require_execute_lease(lease["lease_id"], [])
        server_module.UI_INPUT_FILE.parent.mkdir(parents=True, exist_ok=True)
        server_module.UI_INPUT_FILE.write_text("", encoding="utf-8")
        pending, reset_to = server_module._pending_ui_messages_for_lease(refreshed)
        assert pending == []
        assert reset_to is None
    finally:
        runtime_module.release_workspace(lease["lease_id"])


def test_unread_ui_control_blocks_side_effect_until_polled(tmp_path):
    work_dir = PROJECT_ROOT / f"control-gate-{uuid.uuid4().hex}"
    target = work_dir / "data.txt"
    work_dir.mkdir(parents=True)
    lease_id = None
    try:
        acquired = call_tool(
            "acquire_workspace",
            {"path": str(work_dir), "mode": "execute_exclusive", "owner_label": "控制门测试"},
        )
        lease_id = acquired["lease_id"]
        thread_id = acquired["thread_id"]
        server_module.UI_INPUT_FILE.parent.mkdir(parents=True, exist_ok=True)
        server_module.UI_INPUT_FILE.write_text(
            json.dumps(
                {
                    "id": "gate-message",
                    "type": "ui.message",
                    "scope": "task",
                    "threadId": thread_id,
                    "taskId": lease_id,
                    "timestampMs": 10,
                    "text": "先不要写，先确认控制消息",
                },
                ensure_ascii=False,
            ) + "\n",
            encoding="utf-8",
        )

        async def blocked_write():
            async with Client(mcp) as client:
                return await client.call_tool(
                    "write_text_file",
                    {"path": str(target), "content": "unsafe", "workspace_lease_id": lease_id},
                )

        blocked = asyncio.run(blocked_write())
        assert blocked.is_error is True
        assert "用户控制输入待处理" in blocked.content[0].text
        assert not target.exists()

        polled = call_tool("poll_ui_messages", {"workspace_lease_id": lease_id})
        assert [item["id"] for item in polled["messages"]] == ["gate-message"]

        async def blocked_until_ack():
            async with Client(mcp) as client:
                return await client.call_tool(
                    "write_text_file",
                    {"path": str(target), "content": "still-blocked", "workspace_lease_id": lease_id},
                )

        ack_blocked = asyncio.run(blocked_until_ack())
        assert ack_blocked.is_error is True
        assert "用户消息待回应" in ack_blocked.content[0].text
        ack = call_tool(
            "report_progress",
            {
                "phase": "progress",
                "text": "收到这条 Console 消息，我会先按你的修正处理，再继续写入。",
                "workspace_lease_id": lease_id,
            },
        )
        assert ack["control_resume_required"] is True

        async def blocked_done_after_ack():
            async with Client(mcp) as client:
                return await client.call_tool(
                    "report_progress",
                    {
                        "phase": "done",
                        "text": "回应后不能直接结束",
                        "workspace_lease_id": lease_id,
                    },
                )

        premature_done = asyncio.run(blocked_done_after_ack())
        assert premature_done.is_error is True
        assert "中断后续作待验证" in premature_done.content[0].text

        written = call_tool(
            "write_text_file",
            {"path": str(target), "content": "safe-after-poll", "workspace_lease_id": lease_id},
        )
        assert written["status"] == "written"
        assert target.read_text(encoding="utf-8") == "safe-after-poll"

        with server_module.UI_INPUT_FILE.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps({
                "id": "release-message",
                "type": "ui.message",
                "scope": "task",
                "threadId": thread_id,
                "taskId": lease_id,
                "timestampMs": 11,
                "text": "先别结束",
            }, ensure_ascii=False) + "\n")

        async def blocked_done():
            async with Client(mcp) as client:
                return await client.call_tool(
                    "report_progress",
                    {
                        "phase": "done",
                        "text": "不应越过未读用户修正完成",
                        "workspace_lease_id": lease_id,
                    },
                )

        done_result = asyncio.run(blocked_done())
        assert done_result.is_error is True
        assert "用户控制输入待处理" in done_result.content[0].text

        async def blocked_release():
            async with Client(mcp) as client:
                return await client.call_tool("release_workspace", {"workspace_lease_id": lease_id})

        release_result = asyncio.run(blocked_release())
        assert release_result.is_error is True
        assert "用户控制输入待处理" in release_result.content[0].text

        async def blocked_renew():
            async with Client(mcp) as client:
                return await client.call_tool(
                    "renew_workspace",
                    {"workspace_lease_id": lease_id, "ttl_seconds": 600},
                )

        renew_result = asyncio.run(blocked_renew())
        assert renew_result.is_error is True
        assert "用户控制输入待处理" in renew_result.content[0].text
        assert [item["id"] for item in call_tool("poll_ui_messages", {"workspace_lease_id": lease_id})["messages"]] == ["release-message"]
        second_ack = call_tool(
            "report_progress",
            {
                "phase": "progress",
                "text": "收到结束前修正；已回应后继续原任务，不直接收口。",
                "workspace_lease_id": lease_id,
            },
        )
        assert second_ack["control_resume_required"] is True
        after_ack_release = asyncio.run(blocked_release())
        assert after_ack_release.is_error is True
        assert "中断后续作待验证" in after_ack_release.content[0].text
        resumed = call_tool("read_text_file", {"path": str(target)})
        assert resumed["content"] == "safe-after-poll"
    finally:
        if lease_id:
            call_tool("release_workspace", {"workspace_lease_id": lease_id})
        shutil.rmtree(work_dir, ignore_errors=True)


def test_console_control_classification_preserves_continue_and_allows_explicit_stop():
    assert server_module._ui_message_requires_task_resume({"id": "continue", "text": "不要停止，继续做完"}) is True
    assert server_module._ui_message_requires_task_resume({"id": "ping", "text": "你死了吗"}) is True
    assert server_module._ui_message_requires_task_resume({"id": "stop", "text": "停止当前任务并释放租约"}) is False
    assert server_module._ui_message_requires_task_resume({"id": "stay-off", "text": "/stay off"}) is False
    attachment_only = {
        "id": "image-only",
        "text": "",
        "attachments": [{"id": "att-1", "name": "image.png"}],
    }
    assert server_module._ui_message_requires_visible_reply(attachment_only) is True
    assert server_module._ui_message_requires_task_resume(attachment_only) is True


def test_console_message_does_not_interrupt_in_flight_program_and_blocks_next_action():
    work_dir = PROJECT_ROOT / f"control-inflight-{uuid.uuid4().hex}"
    target = work_dir / "after-message.txt"
    work_dir.mkdir(parents=True)
    lease_id = None
    try:
        acquired = call_tool(
            "acquire_workspace",
            {"path": str(work_dir), "mode": "execute_exclusive", "owner_label": "中途消息不中断测试"},
        )
        lease_id = acquired["lease_id"]
        thread_id = acquired["thread_id"]
        server_module.UI_INPUT_FILE.parent.mkdir(parents=True, exist_ok=True)
        server_module.UI_INPUT_FILE.write_text("", encoding="utf-8")

        def inject_during_program():
            time.sleep(0.15)
            with server_module.UI_INPUT_FILE.open("a", encoding="utf-8") as handle:
                handle.write(json.dumps({
                    "id": "during-program",
                    "type": "ui.message",
                    "scope": "task",
                    "threadId": thread_id,
                    "taskId": lease_id,
                    "timestampMs": 30,
                    "text": "中途补充：继续做，但先回应我",
                }, ensure_ascii=False) + "\n")

        writer = threading.Thread(target=inject_during_program, daemon=True)
        writer.start()
        running = call_tool(
            "run_python",
            {
                "code": "import time; time.sleep(0.45); print('PROGRAM_FINISHED')",
                "cwd": str(work_dir),
                "workspace_lease_id": lease_id,
            },
        )
        writer.join(timeout=2)
        assert running["exit_code"] == 0
        assert "PROGRAM_FINISHED" in running["stdout"]

        async def blocked_next_action():
            async with Client(mcp) as client:
                return await client.call_tool(
                    "write_text_file",
                    {"path": str(target), "content": "too-early", "workspace_lease_id": lease_id},
                )

        next_blocked = asyncio.run(blocked_next_action())
        assert next_blocked.is_error is True
        assert "用户控制输入待处理" in next_blocked.content[0].text
        assert not target.exists()

        polled = call_tool("poll_ui_messages", {"workspace_lease_id": lease_id})
        assert [item["id"] for item in polled["messages"]] == ["during-program"]
        assert polled["control_ack_required"] is True
        assert polled["control_resume_required"] is True
        ack = call_tool(
            "report_progress",
            {
                "phase": "progress",
                "text": "收到中途补充；当前程序已经正常跑完，现在继续原任务。",
                "workspace_lease_id": lease_id,
            },
        )
        assert ack["control_ack_required"] is False
        assert ack["control_resume_required"] is True

        async def blocked_done_before_resume():
            async with Client(mcp) as client:
                return await client.call_tool(
                    "report_progress",
                    {"phase": "done", "text": "不能回应后直接结束", "workspace_lease_id": lease_id},
                )

        premature_done = asyncio.run(blocked_done_before_resume())
        assert premature_done.is_error is True
        assert "中断后续作待验证" in premature_done.content[0].text

        resumed = call_tool(
            "write_text_file",
            {"path": str(target), "content": "resumed", "workspace_lease_id": lease_id},
        )
        assert resumed["status"] == "written"
        private = runtime_module.workspace_status(work_dir, include_private=True)["leases"][0]
        assert private["control_resume_pending_id"] is None
        done = call_tool(
            "report_progress",
            {"phase": "done", "text": "中途消息续作验证完成", "workspace_lease_id": lease_id},
        )
        assert done["lease_released"] is True
        lease_id = None
    finally:
        if lease_id and runtime_module.workspace_status(work_dir)["count"]:
            runtime_module.update_workspace_control_resume_pending(lease_id, None)
            runtime_module.update_workspace_control_ack_pending(lease_id, None)
            call_tool("release_workspace", {"workspace_lease_id": lease_id})
        shutil.rmtree(work_dir, ignore_errors=True)


def test_cached_schema_can_ack_console_with_poll_after_id_compat():
    work_dir = PROJECT_ROOT / f"control-compat-ack-{uuid.uuid4().hex}"
    work_dir.mkdir(parents=True)
    lease_id = None
    try:
        acquired = call_tool(
            "acquire_workspace",
            {"path": str(work_dir), "mode": "execute_exclusive", "owner_label": "旧工具快照 ACK 兼容测试"},
        )
        lease_id = acquired["lease_id"]
        thread_id = acquired["thread_id"]
        server_module.UI_INPUT_FILE.parent.mkdir(parents=True, exist_ok=True)
        server_module.UI_INPUT_FILE.write_text(json.dumps({
            "id": "compat-ack-message",
            "type": "ui.message",
            "scope": "task",
            "threadId": thread_id,
            "taskId": lease_id,
            "timestampMs": 31,
            "text": "继续当前任务，但先回应这条修正",
        }, ensure_ascii=False) + "\n", encoding="utf-8")

        polled = call_tool("poll_ui_messages", {"workspace_lease_id": lease_id})
        assert polled["control_ack_required"] is True
        assert polled["control_resume_required"] is True
        assert 'ack:compat-ack-message' in polled["compat_ack_instruction"]

        wrong = raw_tool(
            "poll_ui_messages",
            {"workspace_lease_id": lease_id, "after_id": "ack:not-current"},
        )
        assert wrong.is_error is True
        assert "CONTROL_COMPAT_ACK_MISMATCH" in wrong.content[0].text

        ack = call_tool(
            "poll_ui_messages",
            {"workspace_lease_id": lease_id, "after_id": "ack:compat-ack-message"},
        )
        assert ack["compat_ack"] == {"status": "acked", "message_id": "compat-ack-message"}
        assert ack["control_ack_required"] is False
        assert ack["control_resume_required"] is True

        blocked_release = raw_tool("release_workspace", {"workspace_lease_id": lease_id})
        assert blocked_release.is_error is True
        assert "中断后续作待验证" in blocked_release.content[0].text

        listed = call_tool("list_directory", {"path": str(work_dir), "workspace_lease_id": lease_id})
        assert listed["path"] == str(work_dir)
        private = runtime_module.workspace_status(work_dir, include_private=True)["leases"][0]
        assert private["control_resume_pending_id"] is None

        released = call_tool("release_workspace", {"workspace_lease_id": lease_id})
        assert released["status"] == "released"
        lease_id = None
    finally:
        if lease_id and runtime_module.workspace_status(work_dir)["count"]:
            runtime_module.update_workspace_control_ack_pending(lease_id, None)
            runtime_module.update_workspace_control_resume_pending(lease_id, None)
            runtime_module.release_workspace(lease_id)
        shutil.rmtree(work_dir, ignore_errors=True)


def test_cached_schema_can_ack_liveness_with_poll_after_id_compat():
    work_dir = PROJECT_ROOT / f"liveness-compat-ack-{uuid.uuid4().hex}"
    work_dir.mkdir(parents=True)
    lease_id = None
    try:
        acquired = call_tool(
            "acquire_workspace",
            {"path": str(work_dir), "mode": "execute_exclusive", "owner_label": "旧工具快照存活 ACK 兼容测试"},
        )
        lease_id = acquired["lease_id"]
        created = runtime_module.ensure_disconnect_probe(
            lease_id,
            activity_at=runtime_module.utc_iso(runtime_module.utc_now() - runtime_module.timedelta(minutes=4)),
            response_seconds=120,
        )
        probe_id = created["probe"]["id"]

        polled = call_tool("poll_ui_messages", {"workspace_lease_id": lease_id})
        assert polled["liveness_probe"]["id"] == probe_id
        assert polled["liveness_probe"]["acknowledged"] is False
        assert f"alive:{probe_id}" in polled["compat_liveness_instruction"]

        wrong = raw_tool(
            "poll_ui_messages",
            {"workspace_lease_id": lease_id, "after_id": "alive:not-current"},
        )
        assert wrong.is_error is True
        assert "LIVENESS_COMPAT_ACK_MISMATCH" in wrong.content[0].text

        ack = call_tool(
            "poll_ui_messages",
            {"workspace_lease_id": lease_id, "after_id": f"alive:{probe_id}"},
        )
        assert ack["compat_liveness_ack"]["acknowledged"] is True
        stored = runtime_module.disconnect_probe_state(runtime_module.active_lease_record(lease_id) or {})
        assert stored["acked_at"]

        released = call_tool("release_workspace", {"workspace_lease_id": lease_id})
        assert released["status"] == "released"
        lease_id = None
    finally:
        if lease_id and runtime_module.workspace_status(work_dir)["count"]:
            runtime_module.release_workspace(lease_id)
        shutil.rmtree(work_dir, ignore_errors=True)


def test_explicit_stop_console_message_allows_completion_after_ack_without_resume():
    work_dir = PROJECT_ROOT / f"control-explicit-stop-{uuid.uuid4().hex}"
    work_dir.mkdir(parents=True)
    lease_id = None
    try:
        acquired = call_tool(
            "acquire_workspace",
            {"path": str(work_dir), "mode": "execute_exclusive", "owner_label": "明确停止消息测试"},
        )
        lease_id = acquired["lease_id"]
        thread_id = acquired["thread_id"]
        server_module.UI_INPUT_FILE.parent.mkdir(parents=True, exist_ok=True)
        server_module.UI_INPUT_FILE.write_text(json.dumps({
            "id": "explicit-stop",
            "type": "ui.message",
            "scope": "task",
            "threadId": thread_id,
            "taskId": lease_id,
            "timestampMs": 40,
            "text": "停止当前任务并释放租约",
        }, ensure_ascii=False) + "\n", encoding="utf-8")
        polled = call_tool("poll_ui_messages", {"workspace_lease_id": lease_id})
        assert polled["control_ack_required"] is True
        assert polled["control_resume_required"] is False
        call_tool(
            "report_progress",
            {"phase": "progress", "text": "收到停止要求，现在按要求结束。", "workspace_lease_id": lease_id},
        )
        done = call_tool(
            "report_progress",
            {"phase": "done", "text": "已按用户明确停止要求结束", "workspace_lease_id": lease_id},
        )
        assert done["lease_released"] is True
        lease_id = None
    finally:
        if lease_id and runtime_module.workspace_status(work_dir)["count"]:
            runtime_module.update_workspace_control_resume_pending(lease_id, None)
            runtime_module.update_workspace_control_ack_pending(lease_id, None)
            call_tool("release_workspace", {"workspace_lease_id": lease_id})
        shutil.rmtree(work_dir, ignore_errors=True)


def test_progress_compatibility_bridge_consumes_ui_control_without_poll_tool():
    work_dir = PROJECT_ROOT / f"progress-control-bridge-{uuid.uuid4().hex}"
    target = work_dir / "after-control.txt"
    work_dir.mkdir(parents=True)
    lease_id = None
    try:
        acquired = call_tool(
            "acquire_workspace",
            {"path": str(work_dir), "mode": "execute_exclusive", "owner_label": "旧 schema 控制桥测试"},
        )
        lease_id = acquired["lease_id"]
        thread_id = acquired["thread_id"]
        server_module.UI_INPUT_FILE.parent.mkdir(parents=True, exist_ok=True)
        server_module.UI_INPUT_FILE.write_text(
            json.dumps(
                {
                    "id": "legacy-bridge-message",
                    "type": "ui.message",
                    "scope": "task",
                    "threadId": thread_id,
                    "taskId": lease_id,
                    "timestampMs": 20,
                    "text": "旧 schema 也要先看到这条修正",
                },
                ensure_ascii=False,
            ) + "\n",
            encoding="utf-8",
        )

        bridged = call_tool(
            "report_progress",
            {
                "phase": "progress",
                "text": "检查 Agent Console 用户修正",
                "workspace_lease_id": lease_id,
            },
        )
        assert bridged["status"] == "reported"
        assert bridged["phase"] == "progress"
        assert bridged["control_count"] == 1
        assert bridged["control_messages"][0]["id"] == "legacy-bridge-message"
        assert bridged["control_messages"][0]["text"] == "旧 schema 也要先看到这条修正"
        assert bridged["control_next_after_id"] == "legacy-bridge-message"
        assert bridged["control_cursor_reset"] is False
        assert bridged["control_has_more"] is False

        async def blocked_bridge_until_ack():
            async with Client(mcp) as client:
                return await client.call_tool(
                    "renew_workspace",
                    {"workspace_lease_id": lease_id, "ttl_seconds": 600},
                )

        bridge_blocked = asyncio.run(blocked_bridge_until_ack())
        assert bridge_blocked.is_error is True
        assert "用户消息待回应" in bridge_blocked.content[0].text
        ack = call_tool(
            "report_progress",
            {
                "phase": "progress",
                "text": "收到旧 schema 返回的 Console 修正，我会按它继续。",
                "workspace_lease_id": lease_id,
            },
        )
        assert ack["status"] == "reported"
        assert ack["control_resume_required"] is True
        renewed = call_tool(
            "renew_workspace",
            {"workspace_lease_id": lease_id, "ttl_seconds": 600},
        )
        assert renewed["lease_id"] == lease_id

        async def premature_done_after_meta_action():
            async with Client(mcp) as client:
                return await client.call_tool(
                    "report_progress",
                    {
                        "phase": "done",
                        "text": "renew 不能冒充中断后的续作",
                        "workspace_lease_id": lease_id,
                    },
                )

        meta_done = asyncio.run(premature_done_after_meta_action())
        assert meta_done.is_error is True
        assert "中断后续作待验证" in meta_done.content[0].text
        written = call_tool(
            "write_text_file",
            {"path": str(target), "content": "control-consumed", "workspace_lease_id": lease_id},
        )
        assert written["status"] == "written"
        assert target.read_text(encoding="utf-8") == "control-consumed"

        done = call_tool(
            "report_progress",
            {
                "phase": "done",
                "text": "兼容桥验证完成",
                "workspace_lease_id": lease_id,
            },
        )
        assert done["lease_released"] is True
        lease_id = None
    finally:
        if lease_id and runtime_module.workspace_status(work_dir)["count"]:
            call_tool("release_workspace", {"workspace_lease_id": lease_id})
        shutil.rmtree(work_dir, ignore_errors=True)


def test_ultra_root_release_paths_require_finalize_gate(monkeypatch, tmp_path):
    workspace = tmp_path / "ultra-finalize-release-gate"
    workspace.mkdir()
    lease = runtime_module.acquire_workspace(
        workspace, mode="execute_exclusive", owner_label="Ultra finalize gate",
    )
    calls = []

    def blocked(lease_id):
        calls.append(lease_id)
        raise runtime_module.RuntimePolicyError(
            "Ultra Team 尚未 finalize",
            "must finalize first",
            error_type="TEAM_FINALIZE_REQUIRED",
        )

    monkeypatch.setattr(server_module, "_require_ultra_team_finalized_before_root_release", blocked)

    async def raw(name, arguments):
        async with Client(mcp) as client:
            return await client.call_tool(name, arguments)

    try:
        direct = asyncio.run(raw("release_workspace", {"workspace_lease_id": lease["lease_id"]}))
        assert direct.is_error is True
        assert "TEAM_FINALIZE_REQUIRED" in direct.content[0].text

        done = asyncio.run(raw("report_progress", {
            "phase": "done",
            "text": "must not bypass Team finalize",
            "workspace_lease_id": lease["lease_id"],
        }))
        assert done.is_error is True
        assert "TEAM_FINALIZE_REQUIRED" in done.content[0].text
        assert calls == [lease["lease_id"], lease["lease_id"]]
        assert runtime_module.active_lease_record(lease["lease_id"]) is not None
    finally:
        if runtime_module.active_lease_record(lease["lease_id"]) is not None:
            runtime_module.release_workspace(lease["lease_id"])


def test_ultra_completion_worker_waits_for_root_release_and_sends_once(monkeypatch):
    monkeypatch.setenv("GPT_PC_ULTRA_COMPLETION_EMAIL", "user@example.invalid")
    completion = {
        "completion_id": "ucompletion_1234567890abcdef",
        "team_id": "uteam_mail",
        "root_lease_id": "a" * 32,
        "task_name": "half-day research",
        "summary": "Root review PASS",
        "recipient": "",
        "finalized_at": 1_791_000_000.0,
        "report": {
            "completed_at_epoch": 1_791_000_000.0,
            "duration_seconds": 3_723,
            "child_agent_count": 1,
            "children": [{
                "agent_id": "ag_worker",
                "local_tag": "gptagent:worker",
                "display_name": "Worker",
                "role": "audit backend",
                "completion_summary": "backend audit PASS",
                "tasks": [{
                    "task_id": "utask_1",
                    "title": "backend audit",
                    "status": "done",
                    "evidence": "tests pass",
                }],
            }],
        },
    }

    class FakeTeams:
        def __init__(self):
            self.sent = False
            self.finished = []

        def pending_completions(self):
            return [] if self.sent else [dict(completion)]

        def claim_completion_notification(self, completion_id):
            assert completion_id == completion["completion_id"]
            return {**completion, "claim_status": "claimed", "attempt_token": "attempt-1"}

        def finish_completion_notification(self, completion_id, token, *, success, gmail_message_id=None, error=None):
            self.finished.append((completion_id, token, success, gmail_message_id, error))
            if success:
                self.sent = True
            return {"status": "sent" if success else "failed"}

    teams = FakeTeams()

    class FakeCoordinator:
        pass

    coordinator = FakeCoordinator()
    coordinator.teams = teams

    class FakeConfig:
        ultra_collaboration = True

    class FakeManager:
        config = FakeConfig()

        def _get_ultra_coordinator(self):
            return coordinator

    manager = FakeManager()
    monkeypatch.setattr(server_module, "_get_chatgpt_agent_manager", lambda: manager)

    root_active = [True]
    monkeypatch.setattr(
        server_module,
        "_active_lease_record",
        lambda lease_id: {"lease_id": lease_id} if root_active[0] else None,
    )
    acquired = []
    released = []
    monkeypatch.setattr(
        server_module,
        "_acquire_workspace",
        lambda *args, **kwargs: acquired.append(kwargs) or {"lease_id": "b" * 32},
    )
    monkeypatch.setattr(
        server_module,
        "_release_workspace",
        lambda lease_id: released.append(lease_id) or {"status": "released"},
    )

    gmail_calls = []

    def gmail(payload, *, timeout_seconds):
        gmail_calls.append(dict(payload))
        if payload["action"] == "search":
            return {"status": "ok", "messages": []}
        assert payload["action"] == "send"
        assert "half-day research" in payload["subject"]
        assert "总耗时：1h 2m 3s" in payload["body"]
        assert "子 Agent 数量：1" in payload["body"]
        assert "audit backend" in payload["body"]
        assert "backend audit PASS" in payload["body"]
        return {"status": "sent", "message_id": "gmail-message-1"}

    monkeypatch.setattr(server_module, "_run_gmail_bridge", gmail)

    server_module._process_ultra_completion_notifications()
    assert gmail_calls == []
    assert teams.finished == []

    root_active[0] = False
    server_module._process_ultra_completion_notifications()
    assert [row["action"] for row in gmail_calls] == ["search", "send"]
    assert teams.finished == [
        (completion["completion_id"], "attempt-1", True, "gmail-message-1", None)
    ]
    assert acquired and released == ["b" * 32]

    server_module._process_ultra_completion_notifications()
    assert [row["action"] for row in gmail_calls] == ["search", "send"]


def test_done_progress_atomically_releases_workspace_and_emits_completion():
    work_dir = PROJECT_ROOT / f"done-finalization-{uuid.uuid4().hex}"
    work_dir.mkdir(parents=True)
    lease_id = None
    try:
        acquired = call_tool(
            "acquire_workspace",
            {"path": str(work_dir), "mode": "execute_exclusive", "owner_label": "完成握手测试"},
        )
        lease_id = acquired["lease_id"]

        progress = call_tool(
            "report_progress",
            {
                "phase": "progress",
                "text": "仍在执行，不应释放租约",
                "workspace_lease_id": lease_id,
            },
        )
        assert progress["status"] == "reported"
        assert progress["phase"] == "progress"
        assert progress["runtime_task"]["lease_active"] is True
        assert progress["runtime_task"]["completion_handshake_required"] is True
        assert "不要发送 ChatGPT 最终答复" in progress["runtime_task"]["instruction"]
        assert runtime_module.workspace_status(work_dir)["count"] == 1

        done = call_tool(
            "report_progress",
            {
                "phase": "done",
                "text": "验证完成，执行原子完成握手",
                "workspace_lease_id": lease_id,
            },
        )
        assert done["status"] == "reported"
        assert done["phase"] == "done"
        assert done["lease_released"] is True
        assert done["release_status"] == "released"
        assert done["lease_id"] == lease_id
        assert runtime_module.workspace_status(work_dir)["count"] == 0

        rows = [
            json.loads(line)
            for line in server_module.ACTIVITY_EVENT_FILE.read_text(encoding="utf-8").splitlines()
        ]
        task_rows = [row for row in rows if row.get("taskId") == lease_id]
        done_index = next(
            index
            for index, row in enumerate(task_rows)
            if row.get("type") == "agent.progress" and row.get("phase") == "done"
        )
        completed_indexes = [
            index for index, row in enumerate(task_rows) if row.get("type") == "task.completed"
        ]
        assert len(completed_indexes) == 1
        assert completed_indexes[0] > done_index

        already = call_tool("release_workspace", {"workspace_lease_id": lease_id})
        assert already["status"] == "already_released"
        rows_after = [
            json.loads(line)
            for line in server_module.ACTIVITY_EVENT_FILE.read_text(encoding="utf-8").splitlines()
        ]
        assert sum(
            1
            for row in rows_after
            if row.get("taskId") == lease_id and row.get("type") == "task.completed"
        ) == 1
    finally:
        if lease_id and runtime_module.workspace_status(work_dir)["count"]:
            call_tool("release_workspace", {"workspace_lease_id": lease_id})
        shutil.rmtree(work_dir, ignore_errors=True)


def test_start_process_is_managed_and_stop_process_closes_it():
    work_dir = PROJECT_ROOT / f"managed-process-{uuid.uuid4().hex}"
    work_dir.mkdir(parents=True)
    lease_id = None
    started = None
    try:
        lease_id = acquire_execution_lease(work_dir)
        started = call_tool(
            "start_process",
            {
                "command": "Start-Sleep -Seconds 30",
                "cwd": str(work_dir),
                "shell": "powershell",
                "wait": False,
                "workspace_lease_id": lease_id,
            },
        )
        assert started["started"] is True
        assert started["status"] == "running"
        assert started["job_id"]
        assert call_tool("get_job", {"job_id": started["job_id"]})["status"] == "running"
        stopped = call_tool(
            "stop_process",
            {"pid": started["pid"], "force": True, "workspace_lease_id": lease_id},
        )
        assert stopped["status"] in {"stopped", "stop_requested"}
        state = call_tool("get_job", {"job_id": started["job_id"]})
        assert state["status"] in {"exited", "pid_reused"}
    finally:
        if started and lease_id:
            try:
                call_tool("cancel_job", {"job_id": started["job_id"], "workspace_lease_id": lease_id, "force": True})
            except Exception:
                pass
        if lease_id:
            call_tool("release_workspace", {"workspace_lease_id": lease_id})
        shutil.rmtree(work_dir, ignore_errors=True)


def test_file_info_missing_path_is_a_normal_result():
    missing = PROJECT_ROOT / f"file-info-missing-{uuid.uuid4().hex}.tmp"
    result = call_tool("file_info", {"path": str(missing)})
    assert result["path"] == str(missing)
    assert result["exists"] is False
    assert result["error"] == "路径不存在"


def test_missing_read_path_returns_a_tool_error_not_a_server_crash():
    missing = PROJECT_ROOT / f"missing-read-{uuid.uuid4().hex}.txt"

    async def run():
        async with Client(mcp) as client:
            return await client.call_tool("read_text_file", {"path": str(missing)})

    result = asyncio.run(run())
    assert result.is_error is True
    assert "路径不存在" in result.content[0].text


def test_code_navigate_tool_is_thin_and_read_only(monkeypatch, tmp_path):
    captured = {}

    def fake_navigate(repo_path, query, *, intent, max_results):
        captured.update(
            repo_path=repo_path,
            query=query,
            intent=intent,
            max_results=max_results,
        )
        return {"freshness": "fresh", "fallback_used": False, "results": []}

    monkeypatch.setattr(server_module, "_navigate_code", fake_navigate)
    result = call_tool(
        "code_navigate",
        {
            "repo_path": str(tmp_path),
            "query": "WidgetService",
            "intent": "impact",
            "max_results": 4,
        },
    )
    assert result == {"freshness": "fresh", "fallback_used": False, "results": []}
    assert captured["query"] == "WidgetService"
    assert captured["intent"] == "impact"
    assert captured["max_results"] == 4


def test_code_navigation_dirty_graph_miss_falls_back_to_changed_source(monkeypatch, tmp_path):
    repo = tmp_path / "dirty-repo"
    source = repo / "src" / "service.py"
    source.parent.mkdir(parents=True)
    source.write_text("DIRTY_NEW_SYMBOL = 1\n", encoding="utf-8")
    root_text = str(repo.resolve(strict=False))
    repo_state = {
        "is_git": True,
        "root": repo.resolve(strict=False),
        "head": "abc123",
        "branch": "main",
        "dirty": True,
        "changed_files": ["src/service.py"],
    }
    project = {
        "name": "dirty-project",
        "root_path": root_text,
        "nodes": 100,
        "git": {"root_exists": True, "head_sha": "abc123", "branch": "main"},
    }

    git_state_calls = []

    def unexpected_git_state(_repo):
        git_state_calls.append(str(_repo))
        raise AssertionError("source fast path must not call Git state")

    monkeypatch.setattr(code_navigation_module, "_git_state", unexpected_git_state)
    monkeypatch.setattr(code_navigation_module, "_select_project", lambda _state: dict(project))

    def fake_cli(tool, args, *, cwd, timeout_seconds=15.0):
        if tool == "index_status":
            return {
                "status": "ready",
                "root_path": root_text,
                "git": {"head_sha": "abc123", "branch": "main"},
            }
        if tool == "search_graph":
            return {"total": 0, "results": [], "has_more": False}
        raise AssertionError(f"unexpected CLI tool: {tool}")

    monkeypatch.setattr(code_navigation_module, "_run_cli", fake_cli)
    result = code_navigation_module.navigate_code(
        str(repo),
        "dirty_new_symbol",
        intent="symbol",
        max_results=5,
    )
    assert result["freshness"] == "fresh"
    assert result["freshness_basis"] == "working_tree_source"
    assert result["fallback_used"] is False
    assert result["fast_path_used"] is True
    assert result["graph_checked"] is False
    assert result["results"][0]["file"] == "src/service.py"
    assert result["results"][0]["line"] == 1
    assert result["results"][0]["source"] in {"rg_text", "python_text"}


def test_code_navigation_fresh_graph_filters_generated_noise(monkeypatch, tmp_path):
    repo = tmp_path / "fresh-repo"
    source = repo / "src" / "service.ts"
    noise = repo / ".next" / "server" / "service.js"
    source.parent.mkdir(parents=True)
    noise.parent.mkdir(parents=True)
    source.write_text("const stableSymbolReference = 1;\nexport function stableSymbol() {}\n", encoding="utf-8")
    noise.write_text("function stableSymbol() {}\n", encoding="utf-8")
    root_text = str(repo.resolve(strict=False))
    repo_state = {
        "is_git": True,
        "root": repo.resolve(strict=False),
        "head": "def456",
        "branch": "main",
        "dirty": False,
        "changed_files": [],
    }
    project = {
        "name": "fresh-project",
        "root_path": root_text,
        "nodes": 100,
        "git": {"root_exists": True, "head_sha": "def456", "branch": "main"},
    }
    monkeypatch.setattr(code_navigation_module, "_git_state", lambda _repo: dict(repo_state))
    monkeypatch.setattr(code_navigation_module, "_select_project", lambda _state: dict(project))

    def fake_cli(tool, args, *, cwd, timeout_seconds=15.0):
        if tool == "index_status":
            return {
                "status": "ready",
                "root_path": root_text,
                "git": {"head_sha": "def456", "branch": "main"},
            }
        if tool == "search_graph":
            return {
                "total": 2,
                "results": [
                    {
                        "name": "stableSymbol",
                        "label": "Function",
                        "file_path": "src/service.ts",
                        "connected_names": ["callerA", "callerA", "calleeB"],
                    },
                    {
                        "name": "stableSymbol",
                        "label": "Function",
                        "file_path": ".next/server/service.js",
                        "start_line": 1,
                        "end_line": 1,
                    },
                ],
            }
        raise AssertionError(f"unexpected CLI tool: {tool}")

    monkeypatch.setattr(code_navigation_module, "_run_cli", fake_cli)
    result = code_navigation_module.navigate_code(
        str(repo),
        "stableSymbol",
        intent="callers",
        max_results=5,
    )
    assert result["freshness"] == "fresh"
    assert result["fallback_used"] is False
    assert len(result["results"]) == 1
    assert result["results"][0]["file"] == "src/service.ts"
    assert result["results"][0]["line"] == 2
    assert result["results"][0]["relationships"] == ["callerA", "calleeB"]


def test_same_thread_reacquire_reuses_existing_lease_without_renewing(tmp_path):
    workspace = tmp_path / "self-reacquire"
    workspace.mkdir()
    first = call_tool(
        "acquire_workspace",
        {
            "path": str(workspace),
            "mode": "execute_exclusive",
            "ttl_seconds": 7200,
            "owner_label": "原始 Coding 任务",
        },
    )
    second = call_tool(
        "acquire_workspace",
        {
            "path": str(workspace),
            "mode": "execute_exclusive",
            "ttl_seconds": 60,
            "owner_label": "不应覆盖原 owner",
            "thread_id": first["thread_id"],
        },
    )

    assert second["status"] == "reused"
    assert second["reused_existing"] is True
    assert second["lease_id"] == first["lease_id"]
    assert second["owner_label"] == first["owner_label"] == "原始 Coding 任务"
    assert second["renewed_at"] == first["renewed_at"]
    assert second["expires_at"] == first["expires_at"]
    assert runtime_module.workspace_status(workspace)["count"] == 1
    task = second["runtime_task"]
    assert task["lease_id"] == first["lease_id"]
    assert task["thread_id"] == first["thread_id"]
    assert task["workspace"] == str(workspace.resolve(strict=False))
    assert task["lease_type"] == "workspace"
    assert task["mode"] == "execute_exclusive"
    assert task["owner_label"] == "原始 Coding 任务"

    rows = [
        json.loads(line)
        for line in server_module.ACTIVITY_EVENT_FILE.read_text(encoding="utf-8").splitlines()
    ]
    assert sum(
        1
        for row in rows
        if row.get("type") == "task.started" and row.get("taskId") == first["lease_id"]
    ) == 1


def test_same_thread_rebuild_after_owner_handle_loss_preserves_controller_identity(tmp_path):
    workspace = tmp_path / "owner-handle-loss"
    workspace.mkdir()
    stable_thread = "thread-stable-controller-rebuild"
    first = runtime_module.acquire_workspace(
        workspace,
        mode="execute_exclusive",
        owner_label="before handle loss",
        thread_id=stable_thread,
    )
    legacy = dict(first)
    legacy.pop("controller_id", None)
    legacy_controller = runtime_module.lease_controller_id(legacy)
    assert legacy_controller == "thread:" + stable_thread

    # Simulate an unexpected owner-handle disappearance rather than a normal
    # completion. The stable ChatGPT thread remains the durable controller key.
    runtime_module._lease_path(first["lease_id"]).unlink(missing_ok=True)
    rebuilt = runtime_module.acquire_workspace(
        workspace,
        mode="execute_exclusive",
        owner_label="after handle loss",
        thread_id=stable_thread,
    )
    try:
        assert rebuilt["lease_id"] != first["lease_id"]
        assert rebuilt["thread_id"] == stable_thread
        assert rebuilt["controller_id"] == legacy_controller
        assert runtime_module.lease_controller_id(rebuilt) == legacy_controller
        assert runtime_module.workspace_status(workspace)["count"] == 1
    finally:
        runtime_module.release_workspace(rebuilt["lease_id"])


def test_same_controller_takeover_fences_old_owner_and_rotates_handle(tmp_path):
    workspace = tmp_path / "same-controller-takeover"
    workspace.mkdir()
    first = runtime_module.acquire_workspace(
        workspace,
        mode="execute_exclusive",
        owner_label="ghost turn owner",
        thread_id="thread-same-controller",
        controller_id="controller:same-chat",
    )
    with pytest.raises(runtime_module.RuntimePolicyError) as wrong:
        runtime_module.begin_same_controller_takeover(
            first["lease_id"],
            controller_id="controller:someone-else",
            reason="must not steal by title",
        )
    assert wrong.value.error_type == "CONTROLLER_IDENTITY_CONFLICT"

    fenced = runtime_module.begin_same_controller_takeover(
        first["lease_id"],
        controller_id="controller:same-chat",
        reason="new visible turn supersedes detached ghost turn",
    )
    assert fenced["status"] == "fenced"
    with pytest.raises(runtime_module.RuntimePolicyError) as blocked:
        runtime_module._active_mode_lease_locked(first["lease_id"])
    assert blocked.value.error_type == "SAME_CONTROLLER_TAKEOVER_FENCED"

    second = runtime_module.finish_same_controller_takeover(
        first["lease_id"],
        controller_id="controller:same-chat",
        runtime_instance="runtime-new-turn",
        owner_label="current visible turn",
    )
    try:
        assert second["status"] == "taken_over"
        assert second["old_lease_id"] == first["lease_id"]
        assert second["new_lease_id"] != first["lease_id"]
        assert second["controller_id"] == "controller:same-chat"
        assert second["thread_id"] == first["thread_id"]
        assert second["workspace"] == first["workspace"]
        assert second["owner_label"] == "current visible turn"
        assert second["supersedes_lease_id"] == first["lease_id"]
        assert runtime_module._read_mode_lease_record_locked(first["lease_id"]) is None
        active = runtime_module._active_mode_lease_locked(second["new_lease_id"])
        assert active["lease_id"] == second["new_lease_id"]
        status = runtime_module.workspace_status(workspace)
        assert status["count"] == 1
        assert status["leases"][0]["lease_id"] == second["new_lease_id"]
    finally:
        runtime_module.release_workspace(second["new_lease_id"])


def test_same_controller_takeover_refuses_live_participants(tmp_path):
    workspace = tmp_path / "takeover-with-child"
    workspace.mkdir()
    root = runtime_module.acquire_workspace(
        workspace,
        mode="execute_exclusive",
        owner_label="root with child",
        thread_id="thread-root-child",
        controller_id="controller:root-child",
    )
    child = runtime_module.attach_workspace_participant(
        root["lease_id"],
        logical_agent_id="ag_takeover_child",
        owner_label="child",
        write_paths=[],
        session_generation=1,
    )
    try:
        with pytest.raises(runtime_module.RuntimePolicyError) as blocked:
            runtime_module.begin_same_controller_takeover(
                root["lease_id"],
                controller_id="controller:root-child",
                reason="must not break child family",
            )
        assert blocked.value.error_type == "SAME_CONTROLLER_TAKEOVER_PARTICIPANTS_ACTIVE"
        assert runtime_module._active_mode_lease_locked(root["lease_id"])["lease_id"] == root["lease_id"]
    finally:
        runtime_module.release_workspace_participant(child["lease_id"])
        runtime_module.release_workspace(root["lease_id"])


def test_same_controller_takeover_refuses_runtime_jobs(monkeypatch, tmp_path):
    workspace = tmp_path / "takeover-with-job"
    workspace.mkdir()
    root = runtime_module.acquire_workspace(
        workspace,
        mode="execute_exclusive",
        owner_label="root with job",
        thread_id="thread-root-job",
        controller_id="controller:root-job",
    )
    monkeypatch.setattr(
        runtime_module,
        "_running_job_records_locked",
        lambda: [{"job_id": "job-live", "lease_id": root["lease_id"]}],
    )
    try:
        with pytest.raises(runtime_module.RuntimePolicyError) as blocked:
            runtime_module.begin_same_controller_takeover(
                root["lease_id"],
                controller_id="controller:root-job",
                reason="must not orphan runtime job",
            )
        assert blocked.value.error_type == "SAME_CONTROLLER_TAKEOVER_JOBS_ACTIVE"
        assert runtime_module._read_mode_lease_record_locked(root["lease_id"]).get("takeover_pending_at") is None
    finally:
        monkeypatch.setattr(runtime_module, "_running_job_records_locked", lambda: [])
        runtime_module.release_workspace(root["lease_id"])


def test_same_controller_takeover_tool_waits_for_inflight_drain(monkeypatch, tmp_path):
    workspace = tmp_path / "takeover-drain"
    workspace.mkdir()
    first = call_tool(
        "acquire_workspace",
        {
            "path": str(workspace),
            "mode": "execute_exclusive",
            "owner_label": "ghost turn",
            "thread_id": "thread-takeover-drain",
            "controller_id": "controller:takeover-drain",
        },
    )

    monkeypatch.setattr(server_module, "_active_lease_tool_count", lambda _lease_id: 1)
    draining = call_tool(
        "supersede_workspace_owner",
        {
            "target_lease_id": first["lease_id"],
            "controller_id": "controller:takeover-drain",
            "reason": "new turn observed ghost execution",
            "owner_label": "visible new turn",
            "drain_timeout_seconds": 0,
        },
    )
    assert draining["status"] == "draining"
    assert draining["new_lease_id"] is None
    assert draining["active_tool_calls"] == 1
    with pytest.raises(runtime_module.RuntimePolicyError) as fenced:
        runtime_module._active_mode_lease_locked(first["lease_id"])
    assert fenced.value.error_type == "SAME_CONTROLLER_TAKEOVER_FENCED"

    monkeypatch.setattr(server_module, "_active_lease_tool_count", lambda _lease_id: 0)
    taken = call_tool(
        "supersede_workspace_owner",
        {
            "target_lease_id": first["lease_id"],
            "controller_id": "controller:takeover-drain",
            "reason": "same takeover retry after drain",
            "owner_label": "visible new turn",
            "drain_timeout_seconds": 0,
        },
    )
    try:
        assert taken["status"] == "taken_over"
        assert taken["same_controller_takeover"] is True
        assert taken["old_turn_fenced"] is True
        assert taken["drained_old_tool_calls"] is True
        assert taken["old_lease_id"] == first["lease_id"]
        assert taken["new_lease_id"] != first["lease_id"]
        assert runtime_module.workspace_status(workspace)["leases"][0]["lease_id"] == taken["new_lease_id"]
    finally:
        runtime_module.release_workspace(taken["new_lease_id"])


def test_same_workspace_other_thread_still_fails_closed(tmp_path):
    workspace = tmp_path / "other-thread-collision"
    workspace.mkdir()
    first = call_tool(
        "acquire_workspace",
        {"path": str(workspace), "mode": "execute_exclusive", "owner_label": "线程 A"},
    )

    async def collide():
        async with Client(mcp) as client:
            return await client.call_tool(
                "acquire_workspace",
                {
                    "path": str(workspace),
                    "mode": "execute_exclusive",
                    "owner_label": "线程 B",
                    "thread_id": f"thread-other-{uuid.uuid4().hex}",
                },
            )

    result = asyncio.run(collide())
    assert result.is_error is True
    text = result.content[0].text
    assert "WORKSPACE_BUSY" in text
    assert "other_thread" in text
    assert "read_only_or_wait" in text
    assert first["lease_id"] in {
        item["lease_id"] for item in runtime_module.workspace_status(workspace)["leases"]
    }


def test_lost_thread_same_task_label_requires_recovery_not_foreign_classification(tmp_path):
    workspace = tmp_path / "lost-thread-same-task"
    workspace.mkdir()
    first = runtime_module.acquire_workspace(
        workspace,
        mode="execute_exclusive",
        owner_label="同一个长任务",
        thread_id="thread-before-compaction",
        runtime_instance="runtime-a",
    )
    try:
        runtime_module.update_workspace_execution_checkpoint(
            first["lease_id"], tool_name="apply_patch", side_effect=True
        )
        with pytest.raises(runtime_module.RuntimePolicyError) as error:
            runtime_module.acquire_workspace(
                workspace,
                mode="execute_exclusive",
                owner_label="同一个长任务",
            )
        assert error.value.error_type == "LEASE_RECOVERY_REQUIRED"
        assert error.value.details["conflict_kind"] == "caller_identity_unknown"
        assert error.value.details["self_recovery_candidate"] is True
        assert error.value.details["owner_label_matches_request"] is True
        recovered = error.value.details["recoverable_runtime_task"]
        assert recovered["lease_id"] == first["lease_id"]
        assert recovered["thread_id"] == "thread-before-compaction"
        assert recovered["ownership_semantics"]["active_lease_alone_is_not_foreign_task_proof"] is True
        assert "DO_NOT_LABEL_AS_FOREIGN_WITHOUT_IDENTITY_PROOF" in recovered["recovery_action"]["directives"]
    finally:
        runtime_module.release_workspace(first["lease_id"])


def test_reacquire_is_fail_closed_for_mode_mismatch_and_runtime_restart(tmp_path):
    mode_workspace = tmp_path / "mode-mismatch"
    mode_workspace.mkdir()
    first = runtime_module.acquire_workspace(
        mode_workspace,
        mode="read_shared",
        owner_label="只读任务",
        thread_id="thread-mode-test",
        runtime_instance="runtime-a",
    )
    with pytest.raises(runtime_module.RuntimePolicyError) as mode_error:
        runtime_module.acquire_workspace(
            mode_workspace,
            mode="execute_exclusive",
            owner_label="不兼容恢复",
            thread_id="thread-mode-test",
            runtime_instance="runtime-a",
        )
    assert mode_error.value.error_type == "LEASE_RECOVERY_INCOMPATIBLE"
    assert runtime_module.workspace_status(mode_workspace)["leases"][0]["lease_id"] == first["lease_id"]

    restart_workspace = tmp_path / "runtime-restart"
    restart_workspace.mkdir()
    old = runtime_module.acquire_workspace(
        restart_workspace,
        mode="execute_exclusive",
        owner_label="旧 Runtime",
        thread_id="thread-restart-test",
        runtime_instance="runtime-old",
    )
    recovered = runtime_module.acquire_workspace(
        restart_workspace,
        mode="execute_exclusive",
        owner_label="新 Runtime",
        thread_id="thread-restart-test",
        runtime_instance="runtime-new",
    )
    assert recovered["status"] == "reused"
    assert recovered["reused_existing"] is True
    assert recovered["lease_id"] == old["lease_id"]
    assert recovered["owner_label"] == "旧 Runtime"
    assert runtime_module.workspace_status(restart_workspace)["leases"][0]["lease_id"] == old["lease_id"]


def test_expired_lease_is_not_reused(tmp_path):
    workspace = tmp_path / "expired-reacquire"
    workspace.mkdir()
    first = runtime_module.acquire_workspace(
        workspace,
        mode="execute_exclusive",
        owner_label="即将过期",
        thread_id="thread-expired-test",
        runtime_instance="runtime-expired-test",
    )
    stored = dict(first)
    stored["expires_at"] = "2000-01-01T00:00:00Z"
    runtime_module._write_json(runtime_module._lease_path(first["lease_id"]), stored)

    replacement = runtime_module.acquire_workspace(
        workspace,
        mode="execute_exclusive",
        owner_label="过期后新任务",
        thread_id="thread-expired-test",
        runtime_instance="runtime-expired-test",
    )
    assert replacement["lease_id"] != first["lease_id"]
    assert replacement.get("reused_existing") is not True
    assert runtime_module.workspace_status(workspace)["count"] == 1


def test_same_thread_multiple_workspaces_recover_without_current_alias(tmp_path):
    first_workspace = tmp_path / "multi-a"
    second_workspace = tmp_path / "multi-b"
    first_workspace.mkdir()
    second_workspace.mkdir()
    first = call_tool(
        "acquire_workspace",
        {"path": str(first_workspace), "mode": "execute_exclusive", "owner_label": "多租约 A"},
    )
    second = call_tool(
        "acquire_workspace",
        {
            "path": str(second_workspace),
            "mode": "execute_exclusive",
            "owner_label": "多租约 B",
            "thread_id": first["thread_id"],
        },
    )
    recovered_first = call_tool(
        "acquire_workspace",
        {
            "path": str(first_workspace),
            "mode": "execute_exclusive",
            "owner_label": "ignored",
            "thread_id": first["thread_id"],
        },
    )
    recovered_second = call_tool(
        "acquire_workspace",
        {
            "path": str(second_workspace),
            "mode": "execute_exclusive",
            "owner_label": "ignored",
            "thread_id": first["thread_id"],
        },
    )
    assert first["lease_id"] != second["lease_id"]
    assert recovered_first["lease_id"] == first["lease_id"]
    assert recovered_second["lease_id"] == second["lease_id"]
    assert recovered_first["reused_existing"] is True
    assert recovered_second["reused_existing"] is True


def test_structured_lease_required_and_job_not_found_errors(tmp_path):
    workspace = tmp_path / "structured-errors"
    workspace.mkdir()

    async def run_without_lease():
        async with Client(mcp) as client:
            return await client.call_tool(
                "run_powershell",
                {"command": "echo SHOULD_NOT_RUN", "cwd": str(workspace)},
            )

    lease_error = asyncio.run(run_without_lease())
    assert lease_error.is_error is True
    lease_text = lease_error.content[0].text
    assert "LEASE_REQUIRED" in lease_text
    assert "target_workspace" in lease_text
    lease_payload = json.loads(lease_text.split(": ", 1)[1])
    assert lease_payload["target_workspace"] == str(workspace.resolve(strict=False))
    assert lease_payload["recommended_action"] == "reuse_existing_lease_or_acquire"

    async def missing_job():
        async with Client(mcp) as client:
            return await client.call_tool("get_job", {"job_id": "noop"})

    job_error = asyncio.run(missing_job())
    assert job_error.is_error is True
    job_text = job_error.content[0].text
    assert "JOB_NOT_FOUND" in job_text
    assert "start_job" in job_text
    assert "background start_process" in job_text
    assert "known_active_jobs" in job_text


def test_lost_execution_handles_rehydrate_from_runtime_checkpoint(tmp_path):
    workspace = tmp_path / "execution-amnesia"
    workspace.mkdir()
    first = call_tool(
        "acquire_workspace",
        {
            "path": str(workspace),
            "mode": "execute_exclusive",
            "owner_label": "长任务执行历史恢复",
        },
    )
    try:
        action = call_tool(
            "run_powershell",
            {
                "command": "echo CHECKPOINT_OK",
                "cwd": str(workspace),
                "workspace_lease_id": first["lease_id"],
            },
        )
        task = action["runtime_task"]
        checkpoint = task["execution_checkpoint"]
        assert task["lease_runtime_instance"] == server_module.MCP_INSTANCE_ID
        assert task["current_runtime_instance"] == server_module.MCP_INSTANCE_ID
        assert task["runtime_restarted"] is False
        assert checkpoint["has_execution_history"] is True
        assert checkpoint["successful_action_count"] >= 1
        assert checkpoint["side_effect_action_count"] >= 1
        assert checkpoint["last_tool"] == "run_powershell"
        assert checkpoint["last_side_effect_tool"] == "run_powershell"
        assert "不得声称" in task["instruction"]
        assert "Codex" in task["instruction"]
        assert "Claude" in task["instruction"]

        progress = call_tool(
            "report_progress",
            {
                "phase": "progress",
                "text": "数据库恢复已经完成，正在继续真实联调。",
                "workspace_lease_id": first["lease_id"],
            },
        )
        progress_checkpoint = progress["runtime_task"]["execution_checkpoint"]
        assert progress_checkpoint["last_progress_phase"] == "progress"
        assert "数据库恢复已经完成" in progress_checkpoint["last_progress_text"]

        async def lose_both_handles():
            async with Client(mcp) as client:
                return await client.call_tool(
                    "run_powershell",
                    {"command": "echo SHOULD_RETRY", "cwd": str(workspace)},
                )

        blocked = asyncio.run(lose_both_handles())
        assert blocked.is_error is True
        text = blocked.content[0].text
        assert "LEASE_REQUIRED" in text
        payload = json.loads(text.split(": ", 1)[1])
        assert payload["recommended_action"] == "retry_same_tool_with_recoverable_lease_id"
        recovered = payload["recoverable_runtime_task"]
        assert recovered["lease_id"] == first["lease_id"]
        assert recovered["thread_id"] == first["thread_id"]
        assert recovered["workspace"] == str(workspace.resolve(strict=False))
        assert recovered["current_runtime_instance"] == server_module.MCP_INSTANCE_ID
        assert recovered["runtime_restarted"] is False
        assert recovered["execution_checkpoint"]["has_execution_history"] is True
        assert "数据库恢复已经完成" in recovered["execution_checkpoint"]["last_progress_text"]
        assert "不得声称" in recovered["recovery_instruction"]

        status = call_tool("workspace_status", {"path": str(workspace)})
        assert status["count"] == 1
        assert status["recovery"]["status"] == "unique_active_execute_lease"
        status_task = status["recovery"]["runtime_task"]
        assert status_task["lease_id"] == first["lease_id"]
        assert status_task["thread_id"] == first["thread_id"]
        assert status_task["current_runtime_instance"] == server_module.MCP_INSTANCE_ID
        assert status_task["runtime_restarted"] is False
        assert status_task["execution_checkpoint"]["has_execution_history"] is True

        retried = call_tool(
            "run_powershell",
            {
                "command": "echo RETRY_OK",
                "cwd": str(workspace),
                "workspace_lease_id": recovered["lease_id"],
            },
        )
        assert retried["exit_code"] == 0
        assert "RETRY_OK" in retried["stdout"]
        assert runtime_module.workspace_status(workspace)["count"] == 1
    finally:
        runtime_module.release_workspace(first["lease_id"])


def test_workspace_busy_returns_recoverable_original_task(tmp_path):
    workspace = tmp_path / "busy-amnesia"
    workspace.mkdir()
    first = call_tool(
        "acquire_workspace",
        {"path": str(workspace), "mode": "execute_exclusive", "owner_label": "原始长任务"},
    )
    try:
        call_tool(
            "run_powershell",
            {
                "command": "echo ORIGINAL_TASK_ACTIVITY",
                "cwd": str(workspace),
                "workspace_lease_id": first["lease_id"],
            },
        )

        async def reacquire_without_original_thread():
            async with Client(mcp) as client:
                return await client.call_tool(
                    "acquire_workspace",
                    {
                        "path": str(workspace),
                        "mode": "execute_exclusive",
                        "owner_label": "未命名任务",
                    },
                )

        blocked = asyncio.run(reacquire_without_original_thread())
        assert blocked.is_error is True
        text = blocked.content[0].text
        assert "LEASE_RECOVERY_REQUIRED" in text
        payload = json.loads(text.split(": ", 1)[1])
        assert payload["recommended_action"] == "reuse_recoverable_lease_id"
        assert payload["self_recovery_candidate"] is True
        recovered = payload["recoverable_runtime_task"]
        assert recovered["lease_id"] == first["lease_id"]
        assert recovered["thread_id"] == first["thread_id"]
        assert recovered["owner_label"] == "原始长任务"
        assert recovered["execution_checkpoint"]["has_execution_history"] is True
    finally:
        runtime_module.release_workspace(first["lease_id"])



def test_web_conversation_binding_is_stable_and_conflict_fails(tmp_path):
    workspace = tmp_path / "web-conversation-binding"
    workspace.mkdir()
    cid = "6ac1b264-722c-83ec-af0f-6bf38b2005d8"
    url = f"https://chatgpt.com/c/{cid}"
    first = call_tool(
        "acquire_workspace",
        {
            "path": str(workspace),
            "mode": "execute_exclusive",
            "owner_label": "conversation-bound",
            "conversation_id": cid,
            "conversation_url": url,
            "conversation_source": "local-profile-audit",
        },
    )
    try:
        assert first["web_conversation_id"] == cid
        assert first["web_conversation_url"] == url
        renewed = call_tool(
            "renew_workspace",
            {
                "workspace_lease_id": first["lease_id"],
                "conversation_id": cid,
                "conversation_url": url,
                "conversation_source": "local-profile-audit",
            },
        )
        assert renewed["web_conversation_id"] == cid
        status = call_tool("workspace_status", {"path": str(workspace)})
        assert status["leases"][0]["web_conversation_id"] == cid
        assert status["recovery"]["runtime_task"]["web_conversation_id"] == cid
        runtime_status = call_tool("runtime_status", {})
        candidate = next(x for x in runtime_status["recovery_candidates"] if x["lease_id"] == first["lease_id"])
        assert candidate["web_conversation_id"] == cid
        assert candidate["web_conversation_url"] == url

        async def conflicting_renew():
            async with Client(mcp) as client:
                return await client.call_tool(
                    "renew_workspace",
                    {
                        "workspace_lease_id": first["lease_id"],
                        "conversation_id": "7bc2c375-833d-94fd-bf10-7cg49c3116e9",
                    },
                )
        blocked = asyncio.run(conflicting_renew())
        assert blocked.is_error is True
        assert "WEB_CONVERSATION_IDENTITY_CONFLICT" in blocked.content[0].text
    finally:
        runtime_module.release_workspace(first["lease_id"])


def test_same_web_conversation_recovers_even_when_runtime_handles_differ(tmp_path):
    workspace = tmp_path / "same-web-conversation-recovery"
    workspace.mkdir()
    cid = "6ac1b264-722c-83ec-af0f-6bf38b2005d8"
    first = call_tool(
        "acquire_workspace",
        {
            "path": str(workspace),
            "mode": "execute_exclusive",
            "owner_label": "original",
            "thread_id": "thread-old-handle",
            "controller_id": "controller:old-handle",
            "conversation_id": cid,
        },
    )
    try:
        async def reacquire_same_chat_new_handles():
            async with Client(mcp) as client:
                return await client.call_tool(
                    "acquire_workspace",
                    {
                        "path": str(workspace),
                        "mode": "execute_exclusive",
                        "owner_label": "continued",
                        "thread_id": "thread-new-handle",
                        "controller_id": "controller:new-handle",
                        "conversation_id": cid,
                    },
                )
        blocked = asyncio.run(reacquire_same_chat_new_handles())
        assert blocked.is_error is True
        payload = json.loads(blocked.content[0].text.split(": ", 1)[1])
        assert payload["error_type"] == "LEASE_RECOVERY_REQUIRED"
        assert payload["conflict_kind"] == "same_web_conversation"
        assert payload["recoverable_runtime_task"]["lease_id"] == first["lease_id"]
        assert payload["recoverable_runtime_task"]["web_conversation_id"] == cid
    finally:
        runtime_module.release_workspace(first["lease_id"])


def test_different_bound_web_conversation_is_foreign_evidence(tmp_path):
    workspace = tmp_path / "different-web-conversation"
    workspace.mkdir()
    first = call_tool(
        "acquire_workspace",
        {
            "path": str(workspace),
            "mode": "execute_exclusive",
            "owner_label": "original",
            "conversation_id": "6ac1b264-722c-83ec-af0f-6bf38b2005d8",
        },
    )
    try:
        async def reacquire_other_chat():
            async with Client(mcp) as client:
                return await client.call_tool(
                    "acquire_workspace",
                    {
                        "path": str(workspace),
                        "mode": "execute_exclusive",
                        "owner_label": "other-chat",
                        "conversation_id": "7bc2c375-833d-94fd-bf10-7cg49c3116e9",
                    },
                )
        blocked = asyncio.run(reacquire_other_chat())
        assert blocked.is_error is True
        payload = json.loads(blocked.content[0].text.split(": ", 1)[1])
        assert payload["error_type"] == "WORKSPACE_BUSY"
        assert payload["conflict_kind"] == "different_web_conversation"
        assert payload["self_recovery_candidate"] is False
    finally:
        runtime_module.release_workspace(first["lease_id"])


def test_recovery_distinguishes_runtime_restart_from_task_loss(tmp_path):
    workspace = tmp_path / "runtime-restart-recovery"
    workspace.mkdir()
    first = call_tool(
        "acquire_workspace",
        {"path": str(workspace), "mode": "execute_exclusive", "owner_label": "跨 Runtime 长任务"},
    )
    try:
        stored = runtime_module._read_json(runtime_module._lease_path(first["lease_id"]))
        assert stored is not None
        stored["runtime_instance"] = "runtime-previous-instance"
        runtime_module._write_json(runtime_module._lease_path(first["lease_id"]), stored)

        status = call_tool("workspace_status", {"path": str(workspace)})
        recovered = status["recovery"]["runtime_task"]
        assert recovered["lease_id"] == first["lease_id"]
        assert recovered["lease_runtime_instance"] == "runtime-previous-instance"
        assert recovered["current_runtime_instance"] == server_module.MCP_INSTANCE_ID
        assert recovered["runtime_restarted"] is True
        assert "热切换" in recovered["recovery_instruction"]
    finally:
        runtime_module.release_workspace(first["lease_id"])


def test_write_requires_an_exclusive_workspace_lease():
    work_dir = PROJECT_ROOT / f"lease-required-{uuid.uuid4().hex}"
    target = work_dir / "data.txt"
    work_dir.mkdir(parents=True)
    try:
        async def run_without_lease():
            async with Client(mcp) as client:
                return await client.call_tool(
                    "write_text_file",
                    {"path": str(target), "content": "blocked"},
                )

        blocked = asyncio.run(run_without_lease())
        assert blocked.is_error is True
        assert "需要工作区租约" in blocked.content[0].text

        lease_id = acquire_execution_lease(work_dir)
        assert call_tool(
            "write_text_file",
            {"path": str(target), "content": "allowed", "workspace_lease_id": lease_id},
        )["status"] == "written"

        async def acquire_conflict():
            async with Client(mcp) as client:
                return await client.call_tool(
                    "acquire_workspace",
                    {"path": str(work_dir), "mode": "execute_exclusive", "thread_id": "thread-explicit-other-controller"},
                )

        conflict = asyncio.run(acquire_conflict())
        assert conflict.is_error is True
        assert "工作区忙" in conflict.content[0].text
    finally:
        if "lease_id" in locals():
            call_tool("release_workspace", {"workspace_lease_id": lease_id})
        shutil.rmtree(work_dir, ignore_errors=True)


def test_media_file_operations_are_lease_free_but_outside_media_still_requires_lease(tmp_path):
    target = runtime_module.MEDIA_REFERENCE_DIR / "note.txt"
    written = call_tool("write_text_file", {"path": str(target), "content": "media-ok", "create_parents": True})
    assert written["status"] == "written"
    assert target.read_text(encoding="utf-8") == "media-ok"

    source = tmp_path / "source.bin"; source.write_bytes(b"copy-me")
    copied = runtime_module.MEDIA_OUTBOX_DIR / "copied.bin"
    result = call_tool("copy_path", {"source": str(source), "destination": str(copied)})
    assert result["status"] == "copied" and copied.read_bytes() == b"copy-me"

    outside = tmp_path / "outside.txt"
    async def outside_write():
        async with Client(mcp) as client:
            return await client.call_tool("write_text_file", {"path": str(outside), "content": "blocked"})
    blocked = asyncio.run(outside_write())
    assert blocked.is_error is True
    assert "LEASE_REQUIRED" in blocked.content[0].text


def test_desktop_ops_uses_neutral_empty_workspace_without_runtime_access():
    target = PROJECT_ROOT / f"desktop-ops-outside-{uuid.uuid4().hex}.txt"
    lease_id = None
    try:
        async def acquire_project_root_as_normal_workspace():
            async with Client(mcp) as client:
                return await client.call_tool(
                    "acquire_workspace",
                    {
                        "path": str(PROJECT_ROOT),
                        "mode": "execute_exclusive",
                        "owner_label": "错误根目录租约测试",
                    },
                )

        root_blocked = asyncio.run(acquire_project_root_as_normal_workspace())
        assert root_blocked.is_error is True
        assert "desktop-ops" in root_blocked.content[0].text
        assert "maintenance-workspace" in root_blocked.content[0].text

        result = call_tool(
            "acquire_workspace",
            {
                "path": str(DESKTOP_OPS_WORKSPACE),
                "mode": "execute_exclusive",
                "owner_label": "桌面应用自动测试",
            },
        )
        lease_id = result["lease_id"]
        assert result["lease_type"] == "desktop_ops"
        command = call_tool(
            "run_powershell",
            {
                "command": "Write-Output DESKTOP_OPS_OK",
                "cwd": str(DESKTOP_OPS_WORKSPACE),
                "workspace_lease_id": lease_id,
            },
        )
        assert command["exit_code"] == 0
        assert "DESKTOP_OPS_OK" in command["stdout"]

        async def attempt_outside_write():
            async with Client(mcp) as client:
                return await client.call_tool(
                    "write_text_file",
                    {
                        "path": str(target),
                        "content": "blocked",
                        "workspace_lease_id": lease_id,
                    },
                )

        blocked = asyncio.run(attempt_outside_write())
        assert blocked.is_error is True
        assert "工作区越界" in blocked.content[0].text
        assert not target.exists()
    finally:
        if lease_id:
            call_tool("release_workspace", {"workspace_lease_id": lease_id})
        target.unlink(missing_ok=True)


def test_desktop_ops_allows_multiple_parallel_empty_leases():
    first = None
    second = None
    try:
        first = call_tool(
            "acquire_workspace",
            {
                "path": str(DESKTOP_OPS_WORKSPACE),
                "mode": "execute_exclusive",
                "owner_label": "桌面空租约并发 A",
            },
        )
        second = call_tool(
            "acquire_workspace",
            {
                "path": str(DESKTOP_OPS_WORKSPACE),
                "mode": "execute_exclusive",
                "owner_label": "桌面空租约并发 B",
            },
        )
        assert first["lease_type"] == "desktop_ops"
        assert second["lease_type"] == "desktop_ops"
        assert first["lease_id"] != second["lease_id"]
        status = call_tool("workspace_status", {"path": str(DESKTOP_OPS_WORKSPACE)})
        ids = {item["lease_id"] for item in status["leases"]}
        assert {first["lease_id"], second["lease_id"]}.issubset(ids)
    finally:
        if second:
            call_tool("release_workspace", {"workspace_lease_id": second["lease_id"]})
        if first:
            call_tool("release_workspace", {"workspace_lease_id": first["lease_id"]})


def test_runtime_admin_done_waits_for_its_own_running_job():
    admin = None
    job_id = None
    try:
        admin = call_tool(
            "acquire_workspace",
            {
                "path": str(RUNTIME_MAINTENANCE_WORKSPACE),
                "mode": "execute_exclusive",
                "owner_label": "管理员后台任务完成门禁测试",
            },
        )
        started = call_tool(
            "start_job",
            {
                "command": "Start-Sleep -Seconds 30",
                "cwd": str(RUNTIME_MAINTENANCE_WORKSPACE),
                "shell": "powershell",
                "name": "管理员完成门禁测试 Job",
                "workspace_lease_id": admin["lease_id"],
            },
        )
        job_id = started["job_id"]

        async def blocked_done():
            async with Client(mcp) as client:
                return await client.call_tool(
                    "report_progress",
                    {
                        "phase": "done",
                        "text": "后台任务尚未结束，不应完成",
                        "workspace_lease_id": admin["lease_id"],
                    },
                )

        result = asyncio.run(blocked_done())
        assert result.is_error is True
        assert "运行底座维护任务仍在运行" in result.content[0].text
        assert runtime_module.workspace_status(RUNTIME_MAINTENANCE_WORKSPACE)["count"] == 1
        rows = []
        if server_module.ACTIVITY_EVENT_FILE.exists():
            rows = [json.loads(line) for line in server_module.ACTIVITY_EVENT_FILE.read_text(encoding="utf-8").splitlines()]
        assert not any(
            row.get("type") == "agent.progress"
            and row.get("phase") == "done"
            and row.get("taskId") == admin["lease_id"]
            for row in rows
        )

        cancelled = call_tool(
            "cancel_job",
            {
                "job_id": job_id,
                "workspace_lease_id": admin["lease_id"],
                "force": True,
            },
        )
        assert cancelled["status"] in {"cancelled", "exited", "stopped"}
        job_id = None
        done = call_tool(
            "report_progress",
            {
                "phase": "done",
                "text": "管理员后台任务已结束，现在完成",
                "workspace_lease_id": admin["lease_id"],
            },
        )
        assert done["lease_released"] is True
        admin = None
    finally:
        if admin is not None:
            if job_id is not None:
                try:
                    call_tool(
                        "cancel_job",
                        {"job_id": job_id, "workspace_lease_id": admin["lease_id"], "force": True},
                    )
                except Exception:
                    pass
            try:
                runtime_module.release_workspace(admin["lease_id"])
            except Exception:
                pass


def test_runtime_admin_coexists_with_project_desktop_and_jobs_while_scope_stays_protected():
    normal_dir = PROJECT_ROOT / f"admin-coexist-{uuid.uuid4().hex}"
    normal_target = normal_dir / "project.txt"
    blocked_admin_target = normal_dir / "admin-must-not-touch-project.txt"
    probe = RUNTIME_MAINTENANCE_WORKSPACE / f"admin-probe-{uuid.uuid4().hex}.txt"
    normal_dir.mkdir(parents=True)
    normal_lease = None
    desktop_lease = None
    admin_lease = None
    job_id = None
    try:
        normal = call_tool(
            "acquire_workspace",
            {
                "path": str(normal_dir),
                "mode": "execute_exclusive",
                "owner_label": "并行项目任务",
            },
        )
        normal_lease = normal["lease_id"]
        desktop = call_tool(
            "acquire_workspace",
            {
                "path": str(DESKTOP_OPS_WORKSPACE),
                "mode": "execute_exclusive",
                "owner_label": "并行桌面任务",
            },
        )
        desktop_lease = desktop["lease_id"]
        running_job = call_tool(
            "start_job",
            {
                "command": "Start-Sleep -Seconds 30",
                "cwd": str(normal_dir),
                "shell": "powershell",
                "name": "并行项目 Job",
                "workspace_lease_id": normal_lease,
            },
        )
        job_id = running_job["job_id"]

        admin = call_tool(
            "acquire_workspace",
            {
                "path": str(RUNTIME_MAINTENANCE_WORKSPACE),
                "mode": "execute_exclusive",
                "owner_label": "运行底座并行维护自动测试",
            },
        )
        admin_lease = admin["lease_id"]
        assert admin["lease_type"] == "runtime_admin"
        assert Path(admin["scope_root"]).resolve() == PROJECT_ROOT.resolve()

        active = runtime_module.workspace_status()["leases"]
        active_types = {item["lease_type"] for item in active}
        assert {"runtime_admin", "workspace", "desktop_ops"}.issubset(active_types)
        assert {normal_lease, desktop_lease, admin_lease}.issubset(
            {item["lease_id"] for item in active}
        )
        assert call_tool("get_job", {"job_id": job_id})["status"] == "running"

        project_write = call_tool(
            "write_text_file",
            {
                "path": str(normal_target),
                "content": "PROJECT_CONTINUES",
                "workspace_lease_id": normal_lease,
            },
        )
        assert project_write["status"] == "written"

        desktop_command = call_tool(
            "run_powershell",
            {
                "command": "Write-Output DESKTOP_CONTINUES",
                "cwd": str(DESKTOP_OPS_WORKSPACE),
                "workspace_lease_id": desktop_lease,
            },
        )
        assert desktop_command["exit_code"] == 0
        assert "DESKTOP_CONTINUES" in desktop_command["stdout"]

        admin_write = call_tool(
            "create_file",
            {
                "path": str(probe),
                "content": "RUNTIME_ADMIN_OK",
                "workspace_lease_id": admin_lease,
            },
        )
        assert admin_write["status"] == "written"

        async def admin_attempts_project_write():
            async with Client(mcp) as client:
                return await client.call_tool(
                    "write_text_file",
                    {
                        "path": str(blocked_admin_target),
                        "content": "MUST_NOT_WRITE",
                        "workspace_lease_id": admin_lease,
                    },
                )

        blocked_write = asyncio.run(admin_attempts_project_write())
        assert blocked_write.is_error is True
        assert "RESOURCE_OWNERSHIP_CONFLICT" in blocked_write.content[0].text
        assert not blocked_admin_target.exists()

        async def admin_attempts_project_job_cancel():
            async with Client(mcp) as client:
                return await client.call_tool(
                    "cancel_job",
                    {
                        "job_id": job_id,
                        "workspace_lease_id": admin_lease,
                        "force": True,
                    },
                )

        blocked_cancel = asyncio.run(admin_attempts_project_job_cancel())
        assert blocked_cancel.is_error is True
        assert "RESOURCE_OWNERSHIP_CONFLICT" in blocked_cancel.content[0].text
        assert call_tool("get_job", {"job_id": job_id})["status"] == "running"

        cancelled = call_tool(
            "cancel_job",
            {
                "job_id": job_id,
                "workspace_lease_id": normal_lease,
                "force": True,
            },
        )
        assert cancelled["status"] in {"cancelled", "exited", "stopped"}
        job_id = None

        call_tool("release_workspace", {"workspace_lease_id": admin_lease})
        admin_lease = None
        remaining = runtime_module.workspace_status()["leases"]
        remaining_ids = {item["lease_id"] for item in remaining}
        assert normal_lease in remaining_ids
        assert desktop_lease in remaining_ids
    finally:
        probe.unlink(missing_ok=True)
        blocked_admin_target.unlink(missing_ok=True)
        if job_id and normal_lease:
            try:
                call_tool(
                    "cancel_job",
                    {
                        "job_id": job_id,
                        "workspace_lease_id": normal_lease,
                        "force": True,
                    },
                )
            except Exception:
                pass
        if admin_lease:
            call_tool("release_workspace", {"workspace_lease_id": admin_lease})
        if desktop_lease:
            call_tool("release_workspace", {"workspace_lease_id": desktop_lease})
        if normal_lease:
            call_tool("release_workspace", {"workspace_lease_id": normal_lease})
        shutil.rmtree(normal_dir, ignore_errors=True)


def test_background_job_is_observable_and_does_not_block_the_call():
    work_dir = PROJECT_ROOT / f"job-test-{uuid.uuid4().hex}"
    work_dir.mkdir(parents=True)
    try:
        lease_id = acquire_execution_lease(work_dir)
        started = call_tool(
            "start_job",
            {
                "command": "Start-Sleep -Seconds 1; Write-Output JOB_DONE",
                "cwd": str(work_dir),
                "shell": "powershell",
                "workspace_lease_id": lease_id,
            },
        )
        assert started["status"] == "running"
        assert isinstance(started["process_create_time"], float)
        waited = call_tool("wait_process", {"pid": started["pid"], "timeout_seconds": 10})
        assert waited["status"] in {"exited", "gone"}
        tail = call_tool("tail_job", {"job_id": started["job_id"]})
        assert "JOB_DONE" in tail["content"]
        assert tail["job"]["status"] == "exited"
        listed = call_tool("list_jobs", {"workspace": str(work_dir)})
        assert listed["count"] == 1
    finally:
        if "lease_id" in locals():
            call_tool("release_workspace", {"workspace_lease_id": lease_id})
        shutil.rmtree(work_dir, ignore_errors=True)


def test_reused_job_pid_is_never_treated_as_safe_to_terminate():
    work_dir = PROJECT_ROOT / f"job-identity-test-{uuid.uuid4().hex}"
    work_dir.mkdir(parents=True)
    record_path: Path | None = None
    original_record: dict | None = None
    try:
        lease_id = acquire_execution_lease(work_dir)
        started = call_tool(
            "start_job",
            {
                "command": "Start-Sleep -Seconds 30",
                "cwd": str(work_dir),
                "shell": "powershell",
                "workspace_lease_id": lease_id,
            },
        )
        record_path = runtime_module.JOB_DIR / f"job-{started['job_id']}.json"
        original_record = json.loads(record_path.read_text(encoding="utf-8"))
        forged = dict(original_record)
        forged["process_create_time"] = float(original_record["process_create_time"]) + 3600.0
        record_path.write_text(json.dumps(forged, ensure_ascii=False, sort_keys=True, indent=2), encoding="utf-8")

        state = call_tool("get_job", {"job_id": started["job_id"]})
        assert state["status"] == "pid_reused"
        assert state["pid_alive"] is False
        assert state["pid_present"] is True

        blocked_cancel = call_tool(
            "cancel_job",
            {
                "job_id": started["job_id"],
                "workspace_lease_id": lease_id,
                "force": True,
            },
        )
        assert blocked_cancel["status"] == "pid_reused"
        assert blocked_cancel["requested"] == []

        async def attempt_stop_process():
            async with Client(mcp) as client:
                return await client.call_tool(
                    "stop_process",
                    {
                        "pid": started["pid"],
                        "force": True,
                        "workspace_lease_id": lease_id,
                    },
                )

        blocked_stop = asyncio.run(attempt_stop_process())
        assert blocked_stop.is_error is True
        assert "非托管进程" in blocked_stop.content[0].text

        still_running = call_tool("wait_process", {"pid": started["pid"], "timeout_seconds": 0.1})
        assert still_running["status"] == "running"
        assert still_running["timed_out"] is True
    finally:
        if record_path is not None and original_record is not None:
            try:
                record_path.write_text(json.dumps(original_record, ensure_ascii=False, sort_keys=True, indent=2), encoding="utf-8")
            except OSError:
                pass
        if "started" in locals() and "lease_id" in locals():
            try:
                call_tool(
                    "cancel_job",
                    {
                        "job_id": started["job_id"],
                        "workspace_lease_id": lease_id,
                        "force": True,
                    },
                )
            except Exception:
                pass
        if "lease_id" in locals():
            call_tool("release_workspace", {"workspace_lease_id": lease_id})
        shutil.rmtree(work_dir, ignore_errors=True)


def test_command_output_always_has_a_fallback_decoder():
    text, encoding, replaced = _decode_command_output(b"\xff\x81\x00not-text")
    assert encoding == "utf-8"
    assert replaced is True
    assert "\ufffd" in text


def test_concurrent_same_file_writes_preserve_hash_conflict():
    work_dir = PROJECT_ROOT / f"concurrency-test-{uuid.uuid4().hex}"
    target = work_dir / "shared.txt"
    try:
        work_dir.mkdir(parents=True)
        lease_id = acquire_execution_lease(work_dir)
        write_text_file(str(target), "initial", workspace_lease_id=lease_id)
        original_hash = calculate_hash(str(target))["hash"]
        barrier = threading.Barrier(2)

        def attempt(content: str):
            barrier.wait(timeout=5)
            return write_text_file(
                str(target),
                content,
                expected_sha256=original_hash,
                workspace_lease_id=lease_id,
            )

        with ThreadPoolExecutor(max_workers=2) as executor:
            results = list(executor.map(attempt, ("first", "second")))

        assert sorted(result["status"] for result in results) == ["conflict", "written"]
        assert target.read_text(encoding="utf-8") in {"first", "second"}
    finally:
        if "lease_id" in locals():
            call_tool("release_workspace", {"workspace_lease_id": lease_id})
        shutil.rmtree(work_dir, ignore_errors=True)


def test_file_read_write_modify_copy_move_delete_workflow():
    work_dir = PROJECT_ROOT / f"automated-test-{uuid.uuid4().hex}"
    source = work_dir / "中文测试.txt"
    copied = work_dir / "copied.txt"
    moved_dir = work_dir / "moved"
    moved = moved_dir / source.name
    try:
        work_dir.mkdir(parents=True)
        lease_id = acquire_execution_lease(work_dir)
        created = call_tool(
            "create_file",
            {"path": str(source), "content": "第一行\n第二行\n", "workspace_lease_id": lease_id},
        )
        assert created["status"] == "written"
        assert call_tool("read_text_file", {"path": str(source)})["content"] == "第一行\n第二行\n"
        replaced = call_tool(
            "replace_text",
            {"path": str(source), "old": "第二行", "new": "已修改", "workspace_lease_id": lease_id},
        )
        assert replaced["replacements"] == 1
        assert "已修改" in call_tool("read_text_file", {"path": str(source)})["content"]
        patch = "@@ -1,2 +1,3 @@\n 第一行\n-已修改\n+已修改\n+第三行\n"
        patched = call_tool("apply_patch", {"path": str(source), "patch": patch, "workspace_lease_id": lease_id})
        assert patched["status"] == "written"
        assert "第三行" in call_tool("read_text_file", {"path": str(source)})["content"]
        assert call_tool("make_directory", {"path": str(moved_dir), "workspace_lease_id": lease_id})["status"] == "created"
        assert call_tool("copy_path", {"source": str(source), "destination": str(copied), "workspace_lease_id": lease_id})["status"] == "copied"
        assert call_tool("move_path", {"source": str(source), "destination": str(moved), "workspace_lease_id": lease_id})["status"] == "moved"
        assert call_tool("delete_path", {"path": str(copied), "workspace_lease_id": lease_id})["status"] == "deleted"
        assert call_tool("delete_path", {"path": str(work_dir), "recursive": True, "workspace_lease_id": lease_id})["status"] == "deleted"
    finally:
        if "lease_id" in locals():
            call_tool("release_workspace", {"workspace_lease_id": lease_id})
        if work_dir.exists():
            shutil.rmtree(work_dir, ignore_errors=True)


def test_stale_hash_is_a_conflict():
    work_dir = PROJECT_ROOT / f"hash-test-{uuid.uuid4().hex}"
    target = work_dir / "data.txt"
    try:
        work_dir.mkdir(parents=True)
        lease_id = acquire_execution_lease(work_dir)
        call_tool("create_file", {"path": str(target), "content": "old", "workspace_lease_id": lease_id})
        old_hash = call_tool("file_info", {"path": str(target), "include_hash": True})["hash"]
        call_tool("write_text_file", {"path": str(target), "content": "changed", "workspace_lease_id": lease_id})
        result = call_tool(
            "write_text_file",
            {"path": str(target), "content": "unsafe", "expected_sha256": old_hash, "workspace_lease_id": lease_id},
        )
        assert result["status"] == "conflict"
        assert call_tool("read_text_file", {"path": str(target)})["content"] == "changed"
    finally:
        if "lease_id" in locals():
            call_tool("release_workspace", {"workspace_lease_id": lease_id})
        if work_dir.exists():
            shutil.rmtree(work_dir, ignore_errors=True)


def test_agent_read_hash_tail_and_custom_python():
    work_dir = PROJECT_ROOT / f"agent-read-test-{uuid.uuid4().hex}"
    target = work_dir / "agent.log"
    content = "".join(f"line-{index}\n" for index in range(1, 11))
    try:
        work_dir.mkdir(parents=True)
        lease_id = acquire_execution_lease(work_dir)
        call_tool("create_file", {"path": str(target), "content": content, "workspace_lease_id": lease_id})
        snapshot = call_tool("read_text_file", {"path": str(target), "include_hash": True})
        assert snapshot["content"] == content
        assert snapshot["sha256"] == hashlib.sha256(content.encode("utf-8")).hexdigest()
        assert snapshot["stable_snapshot"] is True
        tail = call_tool("read_text_tail", {"path": str(target), "lines": 3})
        assert tail["content"] == "line-8\nline-9\nline-10\n"
        custom = call_tool("run_python", {"python_executable": sys.executable, "code": "import sys; print(sys.executable); print('CUSTOM_PYTHON_OK')", "cwd": str(work_dir), "workspace_lease_id": lease_id})
        assert custom["exit_code"] == 0
        assert "CUSTOM_PYTHON_OK" in custom["stdout"]
    finally:
        if "lease_id" in locals():
            call_tool("release_workspace", {"workspace_lease_id": lease_id})
        if work_dir.exists():
            shutil.rmtree(work_dir, ignore_errors=True)


def test_run_python_filesystem_side_effect_emits_file_changed(tmp_path):
    work_dir = tmp_path / f"activity-python-test-{uuid.uuid4().hex}"
    target = work_dir / "generated.txt"
    try:
        work_dir.mkdir(parents=True)
        lease_id = acquire_execution_lease(work_dir)
        call_tool("create_file", {"path": str(target), "content": "before\n", "workspace_lease_id": lease_id})
        code = f"from pathlib import Path; Path({str(target)!r}).write_text('after\\n', encoding='utf-8')"
        result = call_tool(
            "run_python",
            {
                "python_executable": sys.executable,
                "code": code,
                "cwd": str(work_dir),
                "workspace_lease_id": lease_id,
            },
        )
        assert result["exit_code"] == 0
        rows = [json.loads(line) for line in server_module.ACTIVITY_EVENT_FILE.read_text(encoding="utf-8").splitlines()]
        command_changes = [
            row
            for row in rows
            if row.get("type") == "file.changed"
            and row.get("tool") == "run_python"
            and row.get("path") == str(target)
        ]
        assert len(command_changes) == 1
        assert command_changes[0]["operation"] == "write"
        assert command_changes[0]["detectedBy"] == "filesystem-delta"
        assert "+after" in command_changes[0]["diff"]
        assert "-before" in command_changes[0]["diff"]
        completions = [
            row
            for row in rows
            if row.get("type") == "tool.completed" and row.get("tool") == "run_python"
        ]
        assert len(completions) == 1
        assert completions[0]["arguments"]["cwd"] == str(work_dir)
        assert completions[0]["arguments"]["code"] == code
    finally:
        if "lease_id" in locals():
            call_tool("release_workspace", {"workspace_lease_id": lease_id})
        if work_dir.exists():
            shutil.rmtree(work_dir, ignore_errors=True)


def test_wait_process_and_http_probe():
    process = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(0.15)"])
    waited = call_tool("wait_process", {"pid": process.pid, "timeout_seconds": 5})
    assert waited["status"] in {"exited", "gone"}
    assert waited["timed_out"] is False

    class Handler(http.server.BaseHTTPRequestHandler):
        def do_GET(self):
            body = b"GPT_PC_HTTP_PROBE_OK"
            self.send_response(200)
            self.send_header("Content-Type", "text/plain; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, format, *args):
            return

    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        host, port = server.server_address
        result = call_tool("http_probe", {"url": f"http://{host}:{port}/health"})
        assert result["ok"] is True
        assert result["status_code"] == 200
        assert result["body"] == "GPT_PC_HTTP_PROBE_OK"
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)


def test_git_agent_tools_local_roundtrip():
    root = PROJECT_ROOT / f"git-agent-test-{uuid.uuid4().hex}"
    source = root / "source"
    clone = root / "clone"
    root.mkdir(parents=True, exist_ok=False)
    try:
        lease_id = acquire_execution_lease(root)
        init = call_tool("git_init", {"path": str(source), "initial_branch": "main", "workspace_lease_id": lease_id})
        assert init["exit_code"] == 0
        subprocess.run(["git", "-C", str(source), "config", "user.name", "GPT-PC Agent Test"], check=True)
        subprocess.run(["git", "-C", str(source), "config", "user.email", "agent-test@local.invalid"], check=True)
        (source / "demo.txt").write_text("first\n", encoding="utf-8")
        subprocess.run(["git", "-C", str(source), "add", "demo.txt"], check=True)
        subprocess.run(["git", "-C", str(source), "commit", "-m", "initial"], check=True, capture_output=True)
        rev = call_tool("git_rev_parse", {"repo_path": str(source), "revision": "HEAD"})
        assert rev["exit_code"] == 0
        assert len(rev["stdout"].strip()) == 40
        shown = call_tool("git_show", {"repo_path": str(source), "revision": "HEAD", "paths": ["demo.txt"]})
        assert shown["exit_code"] == 0
        assert "first" in shown["stdout"]
        remote = root / "remote.git"
        bare = call_tool("git_init", {"path": str(remote), "initial_branch": "main", "bare": True, "workspace_lease_id": lease_id})
        assert bare["exit_code"] == 0
        added_remote = call_tool("git_remote", {"repo_path": str(source), "action": "add", "name": "backup", "url": str(remote), "workspace_lease_id": lease_id})
        assert added_remote["exit_code"] == 0
        listed_remote = call_tool("git_remote", {"repo_path": str(source), "action": "list", "workspace_lease_id": lease_id})
        assert listed_remote["exit_code"] == 0
        assert "backup" in listed_remote["stdout"]
        changed_remote = call_tool("git_remote", {"repo_path": str(source), "action": "set-url", "name": "backup", "url": str(remote), "workspace_lease_id": lease_id})
        assert changed_remote["exit_code"] == 0
        removed_remote = call_tool("git_remote", {"repo_path": str(source), "action": "remove", "name": "backup", "workspace_lease_id": lease_id})
        assert removed_remote["exit_code"] == 0
        cloned = call_tool("git_clone", {"source": str(source), "destination": str(clone), "workspace_lease_id": lease_id})
        assert cloned["exit_code"] == 0
        (source / "demo.txt").write_text("first\nsecond\n", encoding="utf-8")
        subprocess.run(["git", "-C", str(source), "add", "demo.txt"], check=True)
        subprocess.run(["git", "-C", str(source), "commit", "-m", "second"], check=True, capture_output=True)
        fetched = call_tool("git_fetch", {"repo_path": str(clone), "remote": "origin", "workspace_lease_id": lease_id})
        assert fetched["exit_code"] == 0
    finally:
        if root.exists():
            try:
                call_tool(
                    "delete_path",
                    {"path": str(root), "recursive": True, "workspace_lease_id": lease_id},
                )
            except Exception:
                shutil.rmtree(root, ignore_errors=True)
        if "lease_id" in locals():
            call_tool("release_workspace", {"workspace_lease_id": lease_id})


def test_stay_mode_blocks_done_and_release_until_exit(tmp_path):
    workspace = tmp_path / "stay-mode"
    workspace.mkdir()
    lease = runtime_module.acquire_workspace(
        workspace,
        mode="execute_exclusive",
        owner_label="驻场模式回归测试",
    )
    lease_id = lease["lease_id"]
    thread_id = lease["thread_id"]

    def append_ui(message_id: str, text: str, timestamp_ms: int) -> None:
        server_module.UI_INPUT_FILE.parent.mkdir(parents=True, exist_ok=True)
        row = {
            "id": message_id,
            "type": "ui.message",
            "scope": "task",
            "threadId": thread_id,
            "taskId": lease_id,
            "timestampMs": timestamp_ms,
            "text": text,
        }
        with server_module.UI_INPUT_FILE.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(row, ensure_ascii=False) + "\n")

    async def raw_call(name: str, arguments: dict):
        async with Client(mcp) as client:
            return await client.call_tool(name, arguments)

    append_ui("stay-enter", "/stay 30s", 1)
    entered = server_module.poll_ui_messages(workspace_lease_id=lease_id)
    assert entered["stay_mode"]["active"] is True
    assert entered["stay_mode"]["status"] == "entered"
    assert runtime_module.workspace_status(include_private=True)["leases"][0]["stay_until_at"]

    call_tool(
        "report_progress",
        {"phase": "progress", "text": "已进入驻场模式，继续驻场。", "workspace_lease_id": lease_id},
    )
    blocked_done = asyncio.run(
        raw_call(
            "report_progress",
            {"phase": "done", "text": "不应完成", "workspace_lease_id": lease_id},
        )
    )
    assert blocked_done.is_error is True
    assert "驻场模式生效中" in blocked_done.content[0].text

    blocked_release = asyncio.run(raw_call("release_workspace", {"workspace_lease_id": lease_id}))
    assert blocked_release.is_error is True
    assert "驻场模式生效中" in blocked_release.content[0].text

    append_ui("stay-exit", "/stay off", 2)
    exited = server_module.poll_ui_messages(workspace_lease_id=lease_id)
    assert exited["stay_mode"]["active"] is False
    assert exited["stay_mode"]["status"] == "exited"
    call_tool(
        "report_progress",
        {"phase": "progress", "text": "已退出驻场模式。", "workspace_lease_id": lease_id},
    )
    released = call_tool("release_workspace", {"workspace_lease_id": lease_id})
    assert released["status"] == "released"


def test_stay_mode_external_force_termination_requires_other_thread(tmp_path):
    target_workspace = tmp_path / "stay-target"
    rescue_workspace = tmp_path / "stay-rescue"
    target_workspace.mkdir()
    rescue_workspace.mkdir()
    target = runtime_module.acquire_workspace(
        target_workspace,
        mode="execute_exclusive",
        owner_label="驻场目标",
    )
    rescue = runtime_module.acquire_workspace(
        rescue_workspace,
        mode="execute_exclusive",
        owner_label="外部救援",
    )
    runtime_module.set_workspace_stay_mode(target["lease_id"], duration_seconds=60)

    async def raw_call(arguments: dict):
        async with Client(mcp) as client:
            return await client.call_tool("force_exit_stay_mode", arguments)

    self_attempt = asyncio.run(
        raw_call(
            {
                "target_lease_id": target["lease_id"],
                "reason": "目标不能终止自己",
                "workspace_lease_id": target["lease_id"],
            }
        )
    )
    assert self_attempt.is_error is True
    assert "另一个不同 GPT thread" in self_attempt.content[0].text
    assert runtime_module.workspace_status(target_workspace)["count"] == 1

    forced = call_tool(
        "force_exit_stay_mode",
        {
            "target_lease_id": target["lease_id"],
            "reason": "外部救援测试",
            "workspace_lease_id": rescue["lease_id"],
        },
    )
    assert forced["status"] == "forced_terminated"
    assert forced["lease_released"] is True
    assert runtime_module.workspace_status(target_workspace)["count"] == 0
    assert runtime_module.workspace_status(rescue_workspace)["count"] == 1
    runtime_module.release_workspace(rescue["lease_id"])


def test_external_force_exit_can_abort_goal_without_fake_success(tmp_path):
    target_workspace = tmp_path / "goal-abort-target"
    rescue_workspace = tmp_path / "goal-abort-rescue"
    target_workspace.mkdir()
    rescue_workspace.mkdir()
    target = runtime_module.acquire_workspace(
        target_workspace,
        mode="execute_exclusive",
        owner_label="Goal 卡住的旧任务",
        thread_id="thread-goal-abort-target",
    )
    rescue = runtime_module.acquire_workspace(
        rescue_workspace,
        mode="execute_exclusive",
        owner_label="外部清理者",
        thread_id="thread-goal-abort-rescue",
    )
    runtime_module.set_visual_mode(
        target["lease_id"],
        action="update",
        goal_enabled=True,
        goal_outcome="原任务未完成但用户明确要求终止",
        goal_success_criteria=["原任务完成"],
        goal_reuse_visual=False,
    )
    controller_id = runtime_module.lease_controller_id(target)
    forced = call_tool(
        "force_exit_stay_mode",
        {
            "target_lease_id": target["lease_id"],
            "reason": "用户明确放弃旧任务并清理资源",
            "workspace_lease_id": rescue["lease_id"],
        },
    )
    try:
        assert forced["status"] == "forced_terminated"
        assert forced["lease_released"] is True
        assert forced["goal_status"] == "aborted"
        assert runtime_module.workspace_status(target_workspace)["count"] == 0
        envelope = runtime_module._read_json(runtime_module._controller_state_path(controller_id))
        assert envelope["closed_at"]
        assert envelope["goal_mode"]["status"] == "aborted"
        assert envelope["goal_mode"]["latest_review"]["status"] == "ABORTED"
        assert envelope["goal_mode"].get("completed_at") is None
    finally:
        runtime_module.release_workspace(rescue["lease_id"])


def test_external_force_exit_refuses_to_orphan_active_participant(tmp_path):
    target_workspace = tmp_path / "goal-abort-participant-target"
    rescue_workspace = tmp_path / "goal-abort-participant-rescue"
    target_workspace.mkdir()
    rescue_workspace.mkdir()
    target = runtime_module.acquire_workspace(
        target_workspace,
        mode="execute_exclusive",
        owner_label="共享 Root",
        thread_id="thread-abort-root",
    )
    participant = runtime_module.attach_workspace_participant(
        target["lease_id"],
        logical_agent_id="ag_abort_guard",
        owner_label="仍在工作的 Child",
    )
    rescue = runtime_module.acquire_workspace(
        rescue_workspace,
        mode="execute_exclusive",
        owner_label="外部清理者",
        thread_id="thread-abort-rescue",
    )
    try:
        with pytest.raises(runtime_module.RuntimePolicyError) as blocked:
            runtime_module.force_release_staying_workspace(
                rescue["lease_id"],
                target["lease_id"],
                reason="不能遗留 Child",
            )
        assert blocked.value.error_type == "SHARED_LEASE_PARTICIPANTS_ACTIVE"
        assert runtime_module.workspace_status(target_workspace)["count"] == 1
    finally:
        runtime_module.release_workspace_participant(participant["lease_id"])
        runtime_module.release_workspace(target["lease_id"])
        runtime_module.release_workspace(rescue["lease_id"])


def test_stay_command_space_syntax_and_24h_limit():
    accepted = server_module._parse_stay_command("/stay 24h")
    assert accepted is not None
    assert accepted["action"] == "enter"
    assert accepted["duration_seconds"] == 24 * 60 * 60

    short = server_module._parse_stay_command("/stay 2m")
    assert short is not None
    assert short["action"] == "enter"
    assert short["duration_seconds"] == 120

    too_long = server_module._parse_stay_command("/stay 25h")
    assert too_long is not None
    assert too_long["action"] == "invalid"

    legacy_plus = server_module._parse_stay_command("/stay+3h")
    assert legacy_plus is not None
    assert legacy_plus["action"] == "invalid"


def test_poll_ui_messages_refreshes_execution_checkpoint_heartbeat(tmp_path):
    workspace = tmp_path / "resident-heartbeat"
    workspace.mkdir()
    lease = call_tool(
        "acquire_workspace",
        {"path": str(workspace), "mode": "execute_exclusive", "owner_label": "驻场 heartbeat 回归"},
    )
    lease_id = lease["lease_id"]
    try:
        before = runtime_module.workspace_status(workspace, include_private=True)["leases"][0]["execution_checkpoint"]
        result = call_tool("poll_ui_messages", {"workspace_lease_id": lease_id})
        after = runtime_module.workspace_status(workspace, include_private=True)["leases"][0]["execution_checkpoint"]
        assert before["last_tool"] is None
        assert after["last_tool"] == "poll_ui_messages"
        assert after["successful_action_count"] >= 1
        assert after["checkpoint_updated_at"]
        assert result["runtime_task"]["execution_checkpoint"]["last_tool"] == "poll_ui_messages"
    finally:
        runtime_module.release_workspace(lease_id)


def test_poll_ui_messages_wait_wakes_early_on_console_input(tmp_path):
    workspace = tmp_path / "console-event-wait"
    workspace.mkdir()
    lease = runtime_module.acquire_workspace(
        workspace,
        mode="execute_exclusive",
        owner_label="Console 事件等待回归测试",
    )
    lease_id = lease["lease_id"]
    thread_id = lease["thread_id"]
    server_module.UI_INPUT_FILE.parent.mkdir(parents=True, exist_ok=True)
    server_module.UI_INPUT_FILE.write_text("", encoding="utf-8")

    def write_message() -> None:
        time.sleep(0.15)
        row = {
            "id": "wake-message",
            "type": "ui.message",
            "scope": "task",
            "threadId": thread_id,
            "taskId": lease_id,
            "timestampMs": round(time.time() * 1000),
            "text": "立即唤醒",
        }
        with server_module.UI_INPUT_FILE.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(row, ensure_ascii=False) + "\n")

    writer = threading.Thread(target=write_message, daemon=True)
    writer.start()
    started = time.monotonic()
    try:
        result = server_module.poll_ui_messages(
            workspace_lease_id=lease_id,
            wait_seconds=2,
        )
        elapsed = time.monotonic() - started
        assert result["count"] == 1
        assert result["messages"][0]["id"] == "wake-message"
        assert result["messages"][0]["text"] == "立即唤醒"
        assert result["woke_on_message"] is True
        assert result["waited_seconds"] < 1.5
        assert elapsed < 1.5
        assert result["control_idle_seconds"] is not None
    finally:
        writer.join(timeout=1)
        runtime_module.release_workspace(lease_id)


def test_ui_attachment_summary_and_image_content(tmp_path):
    workspace = tmp_path / "attachment-workspace"
    workspace.mkdir()
    lease = runtime_module.acquire_workspace(workspace, mode="execute_exclusive", owner_label="附件测试")
    lease_id = lease["lease_id"]
    thread_id = lease["thread_id"]
    attachment_id = "att-test-image"
    attachment_dir = server_module.UI_ATTACHMENT_DIR / lease_id
    attachment_dir.mkdir(parents=True, exist_ok=True)
    attachment_path = attachment_dir / f"{attachment_id}__tiny.png"
    from PIL import Image
    Image.new("RGB", (4, 4), "white").save(attachment_path, format="PNG")
    server_module.UI_INPUT_FILE.parent.mkdir(parents=True, exist_ok=True)
    server_module.UI_INPUT_FILE.write_text(
        json.dumps({
            "id": "attachment-message",
            "type": "ui.message",
            "scope": "task",
            "threadId": thread_id,
            "taskId": lease_id,
            "timestampMs": 100,
            "text": "看图",
            "attachments": [{
                "id": attachment_id,
                "name": "tiny.png",
                "mime": "image/png",
                "size": attachment_path.stat().st_size,
                "path": str(attachment_path),
            }],
        }, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )

    polled = call_tool("poll_ui_messages", {"workspace_lease_id": lease_id})
    assert polled["messages"][0]["attachments"][0]["id"] == attachment_id
    assert polled["control_ack_required"] is True
    assert polled["control_resume_required"] is True

    async def read_attachment():
        async with Client(mcp) as client:
            return await client.call_tool(
                "read_ui_attachment",
                {"attachment_id": attachment_id, "workspace_lease_id": lease_id},
            )

    result = asyncio.run(read_attachment())
    assert result.is_error is False
    assert [item.type for item in result.content] == ["text", "image"]
    private_before_ack = runtime_module.workspace_status(workspace, include_private=True)["leases"][0]
    assert private_before_ack["control_ack_pending_id"] == "attachment-message"
    assert private_before_ack["control_resume_pending_id"] == "attachment-message"

    ack = call_tool(
        "report_progress",
        {"phase": "progress", "text": "收到图片并已读取，继续原任务。", "workspace_lease_id": lease_id},
    )
    assert ack["control_ack_required"] is False
    assert ack["control_resume_required"] is True
    call_tool("list_directory", {"path": str(workspace), "workspace_lease_id": lease_id})
    released = call_tool("release_workspace", {"workspace_lease_id": lease_id})
    assert released["status"] == "released"



def test_capture_and_inspect_media_image_without_lease(monkeypatch):
    from PIL import Image
    stream = io.BytesIO(); Image.new("RGB", (120, 80), "white").save(stream, format="PNG"); png = stream.getvalue()
    monkeypatch.setattr(server_module.visual_io, "capture_screenshot", lambda target, **kwargs: (png, {"capture_type": target}))

    async def capture():
        async with Client(mcp) as client:
            return await client.call_tool("capture_screenshot", {"target": "desktop", "label": "lease-free screenshot"})
    response = asyncio.run(capture())
    assert response.is_error is False
    text_item = next(item for item in response.content if getattr(item, "type", None) == "text")
    envelope = json.loads(text_item.text)
    assert envelope["source"] == "media.screenshot"
    path = Path(envelope["path"]); assert path.is_relative_to(runtime_module.MEDIA_SCREENSHOT_DIR) and path.is_file()

    async def inspect():
        async with Client(mcp) as client:
            return await client.call_tool("inspect_image", {"path": str(path), "publish_to_console": False})
    inspected = asyncio.run(inspect())
    assert inspected.is_error is False
    assert any(getattr(item, "type", None) == "image" for item in inspected.content)


def test_capture_screenshot_returns_imagecontent_and_never_becomes_user_control(monkeypatch, tmp_path):
    import io
    from PIL import Image

    workspace = tmp_path / "capture-observation"
    workspace.mkdir()
    lease = runtime_module.acquire_workspace(
        workspace,
        mode="execute_exclusive",
        owner_label="截图观察测试",
    )
    stream = io.BytesIO()
    Image.new("RGB", (390, 844), "white").save(stream, format="PNG")
    png = stream.getvalue()
    before_inbox = server_module.UI_INPUT_FILE.read_bytes() if server_module.UI_INPUT_FILE.exists() else None

    def fake_capture(target, **kwargs):
        assert target == "browser"
        assert kwargs["width"] == 390
        assert kwargs["height"] == 844
        return png, {
            "capture_type": "browser",
            "browser_url": "http://127.0.0.1:1431/tests/fixtures/visual-harness.html",
            "page_title": "Visual fixture",
            "viewport_width": 390,
            "viewport_height": 844,
            "full_page": False,
        }

    monkeypatch.setattr(server_module.visual_io, "capture_screenshot", fake_capture)

    async def raw_call():
        async with Client(mcp) as client:
            return await client.call_tool(
                "capture_screenshot",
                {
                    "workspace_lease_id": lease["lease_id"],
                    "target": "browser",
                    "label": "mobile capture",
                    "browser_url": "http://127.0.0.1:1431/tests/fixtures/visual-harness.html",
                    "width": 390,
                    "height": 844,
                },
            )

    try:
        response = asyncio.run(raw_call())
        assert response.is_error is False
        assert any(getattr(item, "type", None) == "image" for item in response.content)
        text_item = next(item for item in response.content if getattr(item, "type", None) == "text")
        envelope = json.loads(text_item.text)
        assert envelope["source"] == "agent.observation"
        assert envelope["width"] == 390
        assert envelope["height"] == 844

        context = runtime_module.get_visual_context(lease["lease_id"])["visual_mode"]
        observation = context["observations"][-1]
        assert observation["id"].startswith("obs-")
        assert observation["capture_type"] == "browser"
        assert observation["width"] == 390
        assert observation["height"] == 844
        assert len(observation["sha256"]) == 64
        assert observation["inspected_at"]
        assert observation["path"].endswith(".png")

        after_inbox = server_module.UI_INPUT_FILE.read_bytes() if server_module.UI_INPUT_FILE.exists() else None
        assert after_inbox == before_inbox
        refreshed = runtime_module.workspace_status(workspace, include_private=True)["leases"][0]
        assert refreshed["control_ack_required"] is False
        assert refreshed["control_resume_required"] is False

        events = [json.loads(line) for line in server_module.ACTIVITY_EVENT_FILE.read_text(encoding="utf-8").splitlines()]
        observation_events = [event for event in events if event.get("type") == "agent.observation"]
        assert observation_events
        assert observation_events[-1]["observation"]["id"] == observation["id"]
        assert not any(event.get("type") == "ui.message" for event in events)
    finally:
        runtime_module.release_workspace(lease["lease_id"])


def test_console_relay_force_exit_bypasses_generic_execute_guard_and_preserves_unrelated_lease(tmp_path):
    target_workspace = tmp_path / "relay-rescue-target"
    unrelated_workspace = tmp_path / "relay-rescue-unrelated"
    target_workspace.mkdir()
    unrelated_workspace.mkdir()
    target = runtime_module.acquire_workspace(
        target_workspace,
        mode="execute_exclusive",
        owner_label="旧 GPT 驻场目标",
        thread_id="thread-target-bootstrap",
    )
    unrelated = runtime_module.acquire_workspace(
        unrelated_workspace,
        mode="execute_exclusive",
        owner_label="无关执行任务",
        thread_id="thread-unrelated-bootstrap",
    )
    relay = runtime_module.acquire_workspace(
        CONSOLE_RELAY_WORKSPACE,
        mode="read_shared",
        owner_label="外部救援 relay",
        thread_id="thread-rescue-bootstrap",
    )
    runtime_module.set_workspace_stay_mode(target["lease_id"], duration_seconds=60)
    assert "force_exit_stay_mode" not in server_module._LEASE_REQUIRED_TOOL_NAMES

    async def raw_call():
        async with Client(mcp) as client:
            return await client.call_tool(
                "force_exit_stay_mode",
                {
                    "target_lease_id": target["lease_id"],
                    "reason": "bootstrap 外部救援回归",
                    "workspace_lease_id": relay["lease_id"],
                },
            )

    try:
        response = asyncio.run(raw_call())
        assert response.is_error is False, response.content
        assert response.structured_content["status"] == "forced_terminated"
        assert response.structured_content["lease_released"] is True
        assert runtime_module.workspace_status(target_workspace)["count"] == 0
        assert runtime_module.workspace_status(unrelated_workspace)["count"] == 1
    finally:
        runtime_module.release_workspace(relay["lease_id"])
        runtime_module.release_workspace(unrelated["lease_id"])


def test_runtime_status_surfaces_owner_recovery_and_child_does_not_steal_root(tmp_path):
    workspace = tmp_path / "runtime-status-recovery"
    workspace.mkdir()
    root = runtime_module.acquire_workspace(
        workspace,
        mode="execute_exclusive",
        owner_label="上下文固定任务",
        thread_id="thread-runtime-status-recovery",
        runtime_instance="runtime-before-loss",
    )
    participant = runtime_module.attach_workspace_participant(
        root["lease_id"],
        logical_agent_id="ag_runtime_status_child",
        owner_label="恢复测试 Child",
    )
    try:
        runtime_module.update_workspace_execution_checkpoint(
            root["lease_id"], tool_name="run_python", side_effect=True
        )
        status = call_tool("runtime_status", {})
        candidate = next(
            item for item in status["recovery_candidates"]
            if item["lease_id"] == root["lease_id"]
        )
        assert candidate["classification"] == "RECOVERABLE_OWNER_EXECUTE_LEASE"
        assert candidate["participant_count"] == 1
        assert candidate["active_lease_is_not_foreign_task_proof"] is True
        assert candidate["participant_count_does_not_transfer_ownership"] is True
        assert "不同本身不能证明来自不同 ChatGPT 对话" in status["recovery_instruction"]
        path_status = call_tool("workspace_status", {"path": str(workspace)})
        lease_row = path_status["leases"][0]
        assert lease_row["lease_role"] == "owner"
        assert lease_row["resource_lease_owner"] is True
        assert lease_row["participant_count"] == 1
        assert path_status["recovery"]["decision_gate"]["runtime_handle_mismatch_is_not_conversation_proof"] is True
        assert path_status["recovery"]["decision_gate"]["participant_count_does_not_transfer_ownership"] is True
    finally:
        runtime_module.release_workspace_participant(participant["lease_id"])
        runtime_module.release_workspace(root["lease_id"])


def test_recovery_action_exposes_existing_task_facts_and_do_not_restart_directives(tmp_path):
    workspace = tmp_path / "strong-recovery"
    workspace.mkdir()
    lease = runtime_module.acquire_workspace(
        workspace,
        mode="execute_exclusive",
        owner_label="已有大量修改的任务",
        thread_id="thread-strong-recovery",
        runtime_instance="runtime-before-rehydrate",
    )
    try:
        runtime_module.update_workspace_execution_checkpoint(
            lease["lease_id"], tool_name="run_cmd", side_effect=True
        )
        runtime_module.update_workspace_execution_checkpoint(
            lease["lease_id"],
            progress_phase="verification",
            progress_text="已有真实测试与修改，继续收口。",
        )
        status = runtime_module.workspace_status(workspace, include_private=True)
        assert status["recovery"]["action"] == "RECOVER_EXISTING_TASK"
        task = status["recovery"]["runtime_task"]
        action = task["recovery_action"]
        assert action["action"] == "RECOVER_EXISTING_TASK"
        assert action["lease_id"] == lease["lease_id"]
        assert action["workspace"] == str(workspace.resolve())
        assert action["thread_id"] == "thread-strong-recovery"
        assert action["owner"] == "已有大量修改的任务"
        assert action["successful_action_count"] == 1
        assert action["side_effect_action_count"] == 1
        assert action["active_jobs"] == []
        assert action["last_progress"]["phase"] == "verification"
        assert "已有真实测试" in action["last_progress"]["text"]
        assert {"DO_NOT_RESTART", "DO_NOT_REACQUIRE", "DO_NOT_CLAIM_NO_WORK"}.issubset(action["directives"])
    finally:
        runtime_module.release_workspace(lease["lease_id"])


def test_path_activity_correlation_does_not_own_lifecycle_without_explicit_lease(tmp_path):
    workspace = tmp_path / "activity-vs-lifecycle"
    workspace.mkdir()
    target = workspace / "probe.txt"
    target.write_text("line one\nline two\n", encoding="utf-8")
    lease = call_tool(
        "acquire_workspace",
        {"path": str(workspace), "mode": "execute_exclusive", "owner_label": "lifecycle isolation"},
    )
    lease_id = lease["lease_id"]
    try:
        before = runtime_module.workspace_status(workspace, include_private=True)["leases"][0]["execution_checkpoint"]
        observed = call_tool("read_text_range", {"path": str(target), "start_line": 1, "end_line": 1})
        after_observed = runtime_module.workspace_status(workspace, include_private=True)["leases"][0]["execution_checkpoint"]
        assert observed["content"].strip() == "line one"
        assert "runtime_task" not in observed
        assert after_observed["successful_action_count"] == before["successful_action_count"]
        assert after_observed["last_tool"] == before["last_tool"]

        owned = call_tool(
            "read_text_range",
            {
                "path": str(target),
                "start_line": 1,
                "end_line": 1,
                "workspace_lease_id": lease_id,
            },
        )
        after_owned = runtime_module.workspace_status(workspace, include_private=True)["leases"][0]["execution_checkpoint"]
        assert owned["runtime_task"]["lease_id"] == lease_id
        assert after_owned["successful_action_count"] == after_observed["successful_action_count"] + 1
        assert after_owned["last_tool"] == "read_text_range"
    finally:
        runtime_module.release_workspace(lease_id)


def test_code_navigation_symbol_uses_rg_fast_path_before_graph(monkeypatch, tmp_path):
    repo = tmp_path / "fresh-rg-repo"
    source = repo / "src" / "service.py"
    source.parent.mkdir(parents=True)
    source.write_text("def FastSymbol():\n    return 1\n", encoding="utf-8")
    root_text = str(repo.resolve(strict=False))
    repo_state = {
        "is_git": True,
        "root": repo.resolve(strict=False),
        "head": "fast123",
        "branch": "main",
        "dirty": False,
        "changed_files": [],
    }
    project = {
        "name": "fresh-rg-project",
        "root_path": root_text,
        "nodes": 100,
        "git": {"root_exists": True, "head_sha": "fast123", "branch": "main"},
    }
    git_state_calls = []

    def unexpected_git_state(_repo):
        git_state_calls.append(str(_repo))
        raise AssertionError("source fast path must not call Git state")

    monkeypatch.setattr(code_navigation_module, "_git_state", unexpected_git_state)
    monkeypatch.setattr(code_navigation_module, "_select_project", lambda _state: dict(project))

    graph_calls = []

    def fake_cli(tool, args, *, cwd, timeout_seconds=15.0):
        if tool == "index_status":
            return {
                "status": "ready",
                "root_path": root_text,
                "git": {"head_sha": "fast123", "branch": "main"},
            }
        if tool == "search_graph":
            graph_calls.append(args)
            return {"total": 0, "results": [], "has_more": False}
        raise AssertionError(f"unexpected CLI tool: {tool}")

    monkeypatch.setattr(code_navigation_module, "_run_cli", fake_cli)
    result = code_navigation_module.navigate_code(str(repo), "FastSymbol", intent="symbol", max_results=5)
    assert result["freshness"] == "fresh"
    assert result["fast_path_used"] is True
    assert result["results"][0]["file"] == "src/service.py"
    assert result["results"][0]["line"] == 1
    assert result["results"][0]["source"] in {"rg_text", "python_text"}
    assert git_state_calls == []
    assert graph_calls == []


def test_disconnect_alert_monitor_selects_stale_execute_tasks_with_or_without_stay(monkeypatch):
    now = server_module.datetime.now(server_module.timezone.utc)
    assert server_module.DISCONNECT_ALERT_PROBE_AFTER_SECONDS == 180
    assert server_module.DISCONNECT_ALERT_RESPONSE_SECONDS == 120
    assert server_module.DISCONNECT_ALERT_STALE_SECONDS == 300

    def iso_offset(seconds: float) -> str:
        return server_module.datetime.fromtimestamp(
            now.timestamp() + seconds,
            tz=server_module.timezone.utc,
        ).isoformat().replace("+00:00", "Z")

    def execution(lease_id: str, heartbeat_age: int, **extra):
        heartbeat = iso_offset(-heartbeat_age)
        value = {
            "lease_id": lease_id,
            "mode": "execute_exclusive",
            "thread_id": f"thread-{lease_id}",
            "expires_at": iso_offset(900),
            "created_at": iso_offset(-1200),
            "execution_checkpoint_updated_at": heartbeat,
        }
        value.update(extra)
        return value

    stale_no_stay = execution("a" * 32, 360)
    connected = execution("b" * 32, 120)
    stale_with_stay = execution("f" * 32, 360, stay_until_at=iso_offset(600))
    relay_read_shared = execution("g" * 32, 360, mode="read_shared")
    notifier_helper = execution(
        "h" * 32,
        360,
        thread_id="system-disconnect-alert-test",
        owner_label="GPT 掉线邮件通知",
    )
    rows = [stale_no_stay, connected, stale_with_stay, relay_read_shared, notifier_helper]
    by_id = {row["lease_id"]: row for row in rows}
    probe_now = [now]

    def fake_ensure_probe(lease_id, *, activity_at, response_seconds, defer_delivery=False):
        row = by_id[lease_id]
        current = runtime_module.disconnect_probe_state(row)
        if current and current["activity_at"] == activity_at:
            return {"status": "existing", "lease_id": lease_id, "probe": current}
        probe_id = f"probe-{lease_id[0]}"
        sent = probe_now[0].isoformat().replace("+00:00", "Z")
        row["disconnect_probe_id"] = probe_id
        row["disconnect_probe_activity_at"] = activity_at
        row["disconnect_probe_sent_at"] = sent
        if defer_delivery:
            row.pop("disconnect_probe_delivered_at", None)
            row.pop("disconnect_probe_response_deadline_at", None)
        else:
            row["disconnect_probe_delivered_at"] = sent
            row["disconnect_probe_response_deadline_at"] = server_module.datetime.fromtimestamp(
                probe_now[0].timestamp() + response_seconds,
                tz=server_module.timezone.utc,
            ).isoformat().replace("+00:00", "Z")
        row.pop("disconnect_probe_acked_at", None)
        return {"status": "created", "lease_id": lease_id, "probe": runtime_module.disconnect_probe_state(row)}

    monkeypatch.setattr(
        runtime_module,
        "_lease_records_snapshot",
        lambda: [(Path(f"lease-{index}.json"), row) for index, row in enumerate(rows)],
    )
    monkeypatch.setattr(runtime_module, "ensure_disconnect_probe", fake_ensure_probe)
    def fake_confirm_disconnect(lease_id, *, probe_id, rescue_grace_seconds=300):
        row = by_id[lease_id]
        existing = runtime_module.disconnect_confirmation_state(row)
        if existing:
            return {"status": "confirmed", "lease_id": lease_id, **existing}
        confirmed_at = probe_now[0]
        row["disconnect_confirmed_at"] = confirmed_at.isoformat().replace("+00:00", "Z")
        row["disconnect_confirmed_probe_id"] = probe_id
        row["disconnect_rescue_deadline_at"] = server_module.datetime.fromtimestamp(
            confirmed_at.timestamp() + rescue_grace_seconds,
            tz=server_module.timezone.utc,
        ).isoformat().replace("+00:00", "Z")
        return {"status": "confirmed", "lease_id": lease_id, **runtime_module.disconnect_confirmation_state(row)}
    monkeypatch.setattr(runtime_module, "confirm_disconnect_for_rescue", fake_confirm_disconnect)
    monkeypatch.setattr(server_module, "_ultra_team_for_root", lambda lease_id: ((object(), {"team_id":"uteam-alert","state":"active"}) if lease_id == "a" * 32 else (None, None)))
    monkeypatch.setattr(server_module, "_notify_ultra_root_disconnect", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(server_module, "_ultra_root_rescue_in_progress", lambda _lease_id: False)
    with server_module._ACTIVE_LEASE_TOOL_CALLS_LOCK:
        server_module._ACTIVE_LEASE_TOOL_CALLS.clear()
        server_module._ACTIVE_LEASE_TOOL_LAST_AT.clear()
        server_module._DISCONNECT_ALERT_LAST_LAUNCH_AT.clear()

    # First stage: silence queues a probe, but the monitor itself cannot deliver
    # a prompt into a model turn and therefore must not start the 2m reply clock.
    assert server_module._disconnect_alert_candidate_ids(now=now) == []
    assert by_id["a" * 32]["disconnect_probe_id"] == "probe-a"
    assert by_id["f" * 32]["disconnect_probe_id"] == "probe-f"
    for probe_owner in (by_id["a" * 32], by_id["f" * 32]):
        probe = runtime_module.disconnect_probe_state(probe_owner)
        assert probe and probe["delivered_at"] is None
        assert probe["response_deadline_at"] is None
    assert "disconnect_probe_id" not in connected
    assert "disconnect_probe_id" not in relay_read_shared
    assert "disconnect_probe_id" not in notifier_helper

    # Even much later an undelivered queued probe cannot become an email candidate.
    later = server_module.datetime.fromtimestamp(now.timestamp() + 121, tz=server_module.timezone.utc)
    probe_now[0] = later
    assert server_module._disconnect_alert_candidate_ids(now=later) == []

    # Simulate the next real MCP safe boundary delivering each queued probe.
    for probe_owner in (by_id["a" * 32], by_id["f" * 32]):
        probe_owner["disconnect_probe_delivered_at"] = iso_offset(0)
        probe_owner["disconnect_probe_response_deadline_at"] = iso_offset(120)
    assert server_module._disconnect_alert_candidate_ids(now=later) == []
    after_grace = server_module.datetime.fromtimestamp(
        later.timestamp() + server_module.ROOT_RESCUE_GRACE_SECONDS + 1,
        tz=server_module.timezone.utc,
    )
    probe_now[0] = after_grace
    assert server_module._disconnect_alert_candidate_ids(now=after_grace) == ["a" * 32]

    # Mail dedupe is per probe episode, not per lease heartbeat.
    by_id["a" * 32]["disconnect_alert_sent_probe_id"] = "probe-a"
    assert server_module._disconnect_alert_candidate_ids(now=after_grace) == []


def test_disconnect_alert_monitor_aggregates_leases_by_stable_controller_id(monkeypatch):
    now = server_module.datetime.now(server_module.timezone.utc)

    def iso_offset(seconds: float) -> str:
        return server_module.datetime.fromtimestamp(
            now.timestamp() + seconds,
            tz=server_module.timezone.utc,
        ).isoformat().replace("+00:00", "Z")

    sibling_a = {
        "lease_id": "1" * 32,
        "mode": "execute_exclusive",
        "thread_id": "thread-stale-handle",
        "controller_id": "controller:shared-real-chat",
        "execution_checkpoint_updated_at": iso_offset(-420),
        "created_at": iso_offset(-1000),
        "expires_at": iso_offset(900),
    }
    sibling_b = {
        "lease_id": "2" * 32,
        "mode": "execute_exclusive",
        "thread_id": "thread-current-handle",
        "controller_id": "controller:shared-real-chat",
        "execution_checkpoint_updated_at": iso_offset(-60),
        "created_at": iso_offset(-1000),
        "expires_at": iso_offset(900),
    }
    rows = [sibling_a, sibling_b]
    monkeypatch.setattr(runtime_module, "_lease_records_snapshot", lambda: [(Path("a"), sibling_a), (Path("b"), sibling_b)])
    created_ids = []

    def fake_ensure_probe(lease_id, *, activity_at, response_seconds, defer_delivery=False):
        assert defer_delivery is True
        created_ids.append(lease_id)
        row = sibling_a if lease_id == sibling_a["lease_id"] else sibling_b
        sent = now.isoformat().replace("+00:00", "Z")
        row["disconnect_probe_id"] = "probe-shared"
        row["disconnect_probe_activity_at"] = activity_at
        row["disconnect_probe_sent_at"] = sent
        row.pop("disconnect_probe_delivered_at", None)
        row.pop("disconnect_probe_response_deadline_at", None)
        return {"status": "created", "lease_id": lease_id, "probe": runtime_module.disconnect_probe_state(row)}

    monkeypatch.setattr(runtime_module, "ensure_disconnect_probe", fake_ensure_probe)
    confirm_now = [now]
    def fake_confirm_disconnect(lease_id, *, probe_id, rescue_grace_seconds=300):
        row = sibling_a if lease_id == sibling_a["lease_id"] else sibling_b
        existing = runtime_module.disconnect_confirmation_state(row)
        if existing:
            return {"status": "confirmed", "lease_id": lease_id, **existing}
        confirmed_at = confirm_now[0]
        row["disconnect_confirmed_at"] = confirmed_at.isoformat().replace("+00:00", "Z")
        row["disconnect_confirmed_probe_id"] = probe_id
        row["disconnect_rescue_deadline_at"] = server_module.datetime.fromtimestamp(
            confirmed_at.timestamp() + rescue_grace_seconds,
            tz=server_module.timezone.utc,
        ).isoformat().replace("+00:00", "Z")
        return {"status": "confirmed", "lease_id": lease_id, **runtime_module.disconnect_confirmation_state(row)}
    monkeypatch.setattr(runtime_module, "confirm_disconnect_for_rescue", fake_confirm_disconnect)
    with server_module._ACTIVE_LEASE_TOOL_CALLS_LOCK:
        server_module._ACTIVE_LEASE_TOOL_CALLS.clear()
        server_module._ACTIVE_LEASE_TOOL_LAST_AT.clear()
        server_module._DISCONNECT_ALERT_LAST_LAUNCH_AT.clear()

    # A fresh lease proves the same stable controller is still active even
    # though another old handle/thread has been silent for more than 5 minutes.
    assert server_module._disconnect_alert_candidate_ids(now=now) == []
    assert created_ids == []

    # Only after the whole controller is silent for 3m do we create one probe.
    sibling_b["execution_checkpoint_updated_at"] = iso_offset(-360)
    assert server_module._disconnect_alert_candidate_ids(now=now) == []
    assert len(created_ids) == 1
    owner_id = created_ids[0]
    owner = sibling_a if owner_id == sibling_a["lease_id"] else sibling_b
    queued = runtime_module.disconnect_probe_state(owner)
    assert queued and queued["delivered_at"] is None and queued["response_deadline_at"] is None
    monkeypatch.setattr(server_module, "_ultra_team_for_root", lambda lease_id: ((object(), {"team_id":"uteam-shared-controller","state":"active"}) if lease_id == owner_id else (None, None)))
    monkeypatch.setattr(server_module, "_notify_ultra_root_disconnect", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(server_module, "_ultra_root_rescue_in_progress", lambda _lease_id: False)
    later = server_module.datetime.fromtimestamp(now.timestamp() + 121, tz=server_module.timezone.utc)
    assert server_module._disconnect_alert_candidate_ids(now=later) == []
    owner["disconnect_probe_delivered_at"] = iso_offset(0)
    owner["disconnect_probe_response_deadline_at"] = iso_offset(120)
    confirm_now[0] = later
    assert server_module._disconnect_alert_candidate_ids(now=later) == []
    after_grace = server_module.datetime.fromtimestamp(
        later.timestamp() + server_module.ROOT_RESCUE_GRACE_SECONDS + 1,
        tz=server_module.timezone.utc,
    )
    assert server_module._disconnect_alert_candidate_ids(now=after_grace) == [owner_id]


def test_disconnect_alert_launcher_uses_current_python_and_existing_helper(monkeypatch, tmp_path):
    helper = tmp_path / "gpt-disconnect-notifier.py"
    helper.write_text("# test helper\n", encoding="utf-8")
    log_path = tmp_path / "disconnect.log"
    seen = {}

    class FakeProcess:
        pid = 43210

    def fake_popen(argv, **kwargs):
        seen["argv"] = list(argv)
        seen["kwargs"] = dict(kwargs)
        return FakeProcess()

    monkeypatch.setattr(server_module, "DISCONNECT_ALERT_HELPER", helper)
    monkeypatch.setattr(server_module, "DISCONNECT_ALERT_LOG", log_path)
    monkeypatch.setattr(server_module.subprocess, "Popen", fake_popen)
    lease_id = "a" * 32
    assert server_module._launch_disconnect_notifier(lease_id) == 43210
    assert seen["argv"] == [
        sys.executable,
        str(helper),
        "--lease-id",
        lease_id,
        "--stale-seconds",
        str(server_module.DISCONNECT_ALERT_STALE_SECONDS),
    ]
    assert seen["kwargs"]["cwd"] == str(server_module.PROJECT_ROOT)
    assert seen["kwargs"]["stdin"] is subprocess.DEVNULL
    assert seen["kwargs"]["stderr"] is subprocess.STDOUT
    assert log_path.is_file()


def test_disconnect_alert_monitor_does_not_treat_inflight_or_recent_bound_tool_as_disconnect(monkeypatch):
    now = server_module.datetime.now(server_module.timezone.utc)
    lease_id = "a" * 32
    record = {
        "lease_id": lease_id,
        "mode": "execute_exclusive",
        "thread_id": "thread-inflight-test",
        "expires_at": server_module.datetime.fromtimestamp(now.timestamp() + 900, tz=server_module.timezone.utc).isoformat().replace("+00:00", "Z"),
        "created_at": server_module.datetime.fromtimestamp(now.timestamp() - 1200, tz=server_module.timezone.utc).isoformat().replace("+00:00", "Z"),
        "execution_checkpoint_updated_at": server_module.datetime.fromtimestamp(now.timestamp() - 600, tz=server_module.timezone.utc).isoformat().replace("+00:00", "Z"),
    }
    monkeypatch.setattr(runtime_module, "_lease_records_snapshot", lambda: [(Path("lease.json"), record)])
    probe_now = [now]

    def fake_ensure_probe(target_id, *, activity_at, response_seconds, defer_delivery=False):
        current = runtime_module.disconnect_probe_state(record)
        if current and current["activity_at"] == activity_at:
            return {"status": "existing", "lease_id": target_id, "probe": current}
        record["disconnect_probe_id"] = "probe-a"
        record["disconnect_probe_activity_at"] = activity_at
        sent = probe_now[0].isoformat().replace("+00:00", "Z")
        record["disconnect_probe_sent_at"] = sent
        if defer_delivery:
            record.pop("disconnect_probe_delivered_at", None)
            record.pop("disconnect_probe_response_deadline_at", None)
        else:
            record["disconnect_probe_delivered_at"] = sent
            record["disconnect_probe_response_deadline_at"] = server_module.datetime.fromtimestamp(
                probe_now[0].timestamp() + response_seconds,
                tz=server_module.timezone.utc,
            ).isoformat().replace("+00:00", "Z")
        record.pop("disconnect_probe_acked_at", None)
        return {"status": "created", "lease_id": target_id, "probe": runtime_module.disconnect_probe_state(record)}

    monkeypatch.setattr(runtime_module, "ensure_disconnect_probe", fake_ensure_probe)
    confirm_now = [now]
    def fake_confirm_disconnect(target_id, *, probe_id, rescue_grace_seconds=300):
        existing = runtime_module.disconnect_confirmation_state(record)
        if existing:
            return {"status": "confirmed", "lease_id": target_id, **existing}
        confirmed_at = confirm_now[0]
        record["disconnect_confirmed_at"] = confirmed_at.isoformat().replace("+00:00", "Z")
        record["disconnect_confirmed_probe_id"] = probe_id
        record["disconnect_rescue_deadline_at"] = server_module.datetime.fromtimestamp(
            confirmed_at.timestamp() + rescue_grace_seconds,
            tz=server_module.timezone.utc,
        ).isoformat().replace("+00:00", "Z")
        return {"status": "confirmed", "lease_id": target_id, **runtime_module.disconnect_confirmation_state(record)}
    monkeypatch.setattr(runtime_module, "confirm_disconnect_for_rescue", fake_confirm_disconnect)
    monkeypatch.setattr(server_module, "_ultra_team_for_root", lambda target_id: ((object(), {"team_id":"uteam-inflight","state":"active"}) if target_id == lease_id else (None, None)))
    monkeypatch.setattr(server_module, "_notify_ultra_root_disconnect", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(server_module, "_ultra_root_rescue_in_progress", lambda _lease_id: False)
    with server_module._ACTIVE_LEASE_TOOL_CALLS_LOCK:
        server_module._ACTIVE_LEASE_TOOL_CALLS.clear()
        server_module._ACTIVE_LEASE_TOOL_LAST_AT.clear()
        server_module._DISCONNECT_ALERT_LAST_LAUNCH_AT.clear()

    server_module._mark_active_lease_tool(lease_id, 1)
    assert server_module._disconnect_alert_candidate_ids(now=now) == []
    assert "disconnect_probe_id" not in record
    server_module._mark_active_lease_tool(lease_id, -1)
    # A just-finished lifecycle-bound tool is still proof that the GPT is alive.
    assert server_module._disconnect_alert_candidate_ids(now=now) == []
    assert "disconnect_probe_id" not in record

    with server_module._ACTIVE_LEASE_TOOL_CALLS_LOCK:
        server_module._ACTIVE_LEASE_TOOL_LAST_AT[lease_id] = server_module.datetime.fromtimestamp(
            now.timestamp() - 360,
            tz=server_module.timezone.utc,
        )
    # 3 minutes+ silence queues a probe, but monitor-only scans cannot start
    # its reply clock because no model-safe delivery boundary occurred yet.
    assert server_module._disconnect_alert_candidate_ids(now=now) == []
    assert record["disconnect_probe_id"] == "probe-a"
    queued = runtime_module.disconnect_probe_state(record)
    assert queued and queued["delivered_at"] is None and queued["response_deadline_at"] is None
    later = server_module.datetime.fromtimestamp(now.timestamp() + 121, tz=server_module.timezone.utc)
    probe_now[0] = later
    assert server_module._disconnect_alert_candidate_ids(now=later) == []
    record["disconnect_probe_delivered_at"] = now.isoformat().replace("+00:00", "Z")
    record["disconnect_probe_response_deadline_at"] = server_module.datetime.fromtimestamp(
        now.timestamp() + server_module.DISCONNECT_ALERT_RESPONSE_SECONDS,
        tz=server_module.timezone.utc,
    ).isoformat().replace("+00:00", "Z")
    confirm_now[0] = later
    assert server_module._disconnect_alert_candidate_ids(now=later) == []
    after_grace = server_module.datetime.fromtimestamp(
        later.timestamp() + server_module.ROOT_RESCUE_GRACE_SECONDS + 1,
        tz=server_module.timezone.utc,
    )
    assert server_module._disconnect_alert_candidate_ids(now=after_grace) == [lease_id]


def test_inflight_tool_is_not_interrupted_and_pending_probe_requires_explicit_progress_at_result_boundary(tmp_path):
    workspace = tmp_path / "inflight-probe-boundary"
    workspace.mkdir()
    lease = call_tool(
        "acquire_workspace",
        {"path": str(workspace), "mode": "execute_exclusive", "owner_label": "inflight probe boundary"},
    )
    lease_id = lease["lease_id"]
    try:
        with ThreadPoolExecutor(max_workers=1) as pool:
            future = pool.submit(
                call_tool,
                "run_python",
                {
                    "code": "import time; time.sleep(0.45); print('LONG_TOOL_FINISHED')",
                    "cwd": str(workspace),
                    "workspace_lease_id": lease_id,
                },
            )
            deadline = time.monotonic() + 1.5
            while server_module._active_lease_tool_count(lease_id) == 0 and time.monotonic() < deadline:
                time.sleep(0.01)
            assert server_module._active_lease_tool_count(lease_id) == 1
            activity_at = server_module._lease_recent_tool_activity_at(lease_id)
            assert activity_at is not None
            synthetic_now = server_module.datetime.fromtimestamp(
                activity_at.timestamp() + server_module.DISCONNECT_ALERT_PROBE_AFTER_SECONDS + 1,
                tz=server_module.timezone.utc,
            )
            # Probe is queued while the tool is in-flight; its 2m reply clock has not started.
            assert server_module._disconnect_alert_candidate_ids(now=synthetic_now) == []
            pending = runtime_module.disconnect_probe_state(runtime_module.active_lease_record(lease_id) or {})
            assert pending and pending["acked_at"] is None
            assert pending["delivered_at"] is None
            assert pending["response_deadline_at"] is None

            result = future.result(timeout=3)
            assert result["exit_code"] == 0
            assert "LONG_TOOL_FINISHED" in result["stdout"]
            boundary_probe = result["runtime_task"]["liveness_probe"]
            assert boundary_probe["id"] == pending["id"]
            assert boundary_probe["acknowledged"] is False
            assert boundary_probe["reply_required"] is True
            assert boundary_probe["priority"] == "system_liveness"
            assert boundary_probe["non_preemptive"] is True
            stored_probe = runtime_module.disconnect_probe_state(runtime_module.active_lease_record(lease_id) or {})
            assert stored_probe["delivered_at"]
            assert stored_probe["response_deadline_at"]
            assert stored_probe["acked_at"] is None

            # Polling exposes the high-priority request but does not satisfy it.
            polled = call_tool("poll_ui_messages", {"workspace_lease_id": lease_id})
            assert polled["liveness_probe"]["acknowledged"] is False
            assert polled["liveness_probe"]["reply_required"] is True

            replied = call_tool(
                "report_progress",
                {"workspace_lease_id": lease_id, "phase": "progress", "text": "仍在工作，继续原任务。"},
            )
            assert replied["liveness_probe"]["acknowledged"] is True
            stored_probe = runtime_module.disconnect_probe_state(runtime_module.active_lease_record(lease_id) or {})
            assert stored_probe["acked_at"]
    finally:
        if runtime_module.workspace_status(workspace)["count"]:
            runtime_module.release_workspace(lease_id)


def test_waiting_poll_wakes_on_system_liveness_probe_but_requires_explicit_progress_reply(tmp_path):
    workspace = tmp_path / "poll-probe-wakeup"
    workspace.mkdir()
    lease = call_tool(
        "acquire_workspace",
        {"path": str(workspace), "mode": "execute_exclusive", "owner_label": "poll probe wake"},
    )
    lease_id = lease["lease_id"]
    try:
        with ThreadPoolExecutor(max_workers=1) as pool:
            future = pool.submit(
                call_tool,
                "poll_ui_messages",
                {"workspace_lease_id": lease_id, "wait_seconds": 2},
            )
            time.sleep(0.2)
            created = runtime_module.ensure_disconnect_probe(
                lease_id,
                activity_at=runtime_module.utc_iso(runtime_module.utc_now() - runtime_module.timedelta(minutes=4)),
                response_seconds=120,
            )
            assert created["status"] == "created"
            result = future.result(timeout=3)
            assert result["messages"] == []
            assert result["count"] == 0
            assert result["woke_on_message"] is False
            assert result["woke_on_liveness_probe"] is True
            assert result["liveness_probe"]["id"] == created["probe"]["id"]
            assert result["liveness_probe"]["acknowledged"] is False
            assert result["liveness_probe"]["reply_required"] is True
            assert result["control_ack_required"] is False
            assert result["control_resume_required"] is False
            stored = runtime_module.disconnect_probe_state(runtime_module.active_lease_record(lease_id) or {})
            assert stored["acked_at"] is None

            replied = call_tool(
                "report_progress",
                {"workspace_lease_id": lease_id, "phase": "progress", "text": "仍在工作，继续。"},
            )
            assert replied["liveness_probe"]["acknowledged"] is True
            stored = runtime_module.disconnect_probe_state(runtime_module.active_lease_record(lease_id) or {})
            assert stored["acked_at"]
    finally:
        if runtime_module.workspace_status(workspace)["count"]:
            runtime_module.release_workspace(lease_id)


def test_guard_tracks_inflight_lease_tool_until_completion(tmp_path):
    workspace = tmp_path / "inflight-disconnect-guard"
    workspace.mkdir()
    lease = call_tool(
        "acquire_workspace",
        {"path": str(workspace), "mode": "execute_exclusive", "owner_label": "inflight disconnect guard"},
    )
    lease_id = lease["lease_id"]
    try:
        with ThreadPoolExecutor(max_workers=1) as pool:
            future = pool.submit(
                call_tool,
                "run_python",
                {
                    "code": "import time; time.sleep(0.4); print('DONE')",
                    "cwd": str(workspace),
                    "workspace_lease_id": lease_id,
                },
            )
            deadline = time.monotonic() + 1.5
            while server_module._active_lease_tool_count(lease_id) == 0 and time.monotonic() < deadline:
                time.sleep(0.01)
            assert server_module._active_lease_tool_count(lease_id) == 1
            result = future.result(timeout=3)
            assert result["exit_code"] == 0
            assert "DONE" in result["stdout"]
        assert server_module._active_lease_tool_count(lease_id) == 0
    finally:
        if runtime_module.workspace_status(workspace)["count"]:
            runtime_module.release_workspace(lease_id)


def test_gmail_bridge_revoked_oauth_is_headless_safe_and_foreground_recoverable(tmp_path, monkeypatch):
    import importlib.util

    bridge_path = PROJECT_ROOT / "scripts" / "gmail-bridge.py"
    spec = importlib.util.spec_from_file_location("gpt_pc_gmail_bridge_oauth_test", bridge_path)
    assert spec and spec.loader
    bridge = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(bridge)

    token_file = tmp_path / "gmail-oauth-token.json"
    client_file = tmp_path / "gmail-oauth-client.json"
    token_file.write_text("{}", encoding="utf-8")
    client_file.write_text("{}", encoding="utf-8")
    monkeypatch.setattr(bridge, "TOKEN_FILE", token_file)
    monkeypatch.setattr(bridge, "CLIENT_CONFIG", client_file)

    class RevokedCredentials:
        expired = True
        refresh_token = "present"
        valid = False

        def refresh(self, _request):
            raise bridge.RefreshError("invalid_grant")

    monkeypatch.setattr(
        bridge.Credentials,
        "from_authorized_user_file",
        lambda *args, **kwargs: RevokedCredentials(),
    )

    flow_calls = []

    class FreshCredentials:
        valid = True

        def to_json(self):
            return '{"fresh":true}'

    class FakeFlow:
        def run_local_server(self, **kwargs):
            flow_calls.append(dict(kwargs))
            return FreshCredentials()

    monkeypatch.setattr(
        bridge.InstalledAppFlow,
        "from_client_secrets_file",
        lambda *args, **kwargs: FakeFlow(),
    )

    with pytest.raises(bridge.GmailOAuthSetupRequired, match="前台重新授权"):
        bridge._load_credentials(interactive_auth=False)
    assert flow_calls == []

    recovered = bridge._load_credentials(interactive_auth=True)
    assert isinstance(recovered, FreshCredentials)
    assert len(flow_calls) == 1
    assert flow_calls[0]["open_browser"] is True
    assert token_file.read_text(encoding="utf-8") == '{"fresh":true}'


def test_undelivered_probe_never_escalates_even_after_the_reply_window(monkeypatch):
    now = server_module.datetime.now(server_module.timezone.utc)
    root_id = "a" * 32

    def iso_offset(seconds: float) -> str:
        return server_module.datetime.fromtimestamp(
            now.timestamp() + seconds,
            tz=server_module.timezone.utc,
        ).isoformat().replace("+00:00", "Z")

    root = {
        "lease_id": root_id,
        "mode": "execute_exclusive",
        "thread_id": "thread-program-watchdog-root",
        "expires_at": iso_offset(900),
        "created_at": iso_offset(-1200),
        "execution_checkpoint_updated_at": iso_offset(-360),
    }
    probe_now = [now]

    def fake_ensure_probe(lease_id, *, activity_at, response_seconds, defer_delivery=False):
        current = runtime_module.disconnect_probe_state(root)
        if current and current["activity_at"] == activity_at:
            return {"status": "existing", "lease_id": lease_id, "probe": current}
        root["disconnect_probe_id"] = "probe-root"
        root["disconnect_probe_activity_at"] = activity_at
        root["disconnect_probe_sent_at"] = probe_now[0].isoformat().replace("+00:00", "Z")
        root.pop("disconnect_probe_delivered_at", None)
        root.pop("disconnect_probe_response_deadline_at", None)
        root.pop("disconnect_probe_acked_at", None)
        return {"status": "created", "lease_id": lease_id, "probe": runtime_module.disconnect_probe_state(root)}

    monkeypatch.setattr(runtime_module, "_lease_records_snapshot", lambda: [(Path("lease-root.json"), root)])
    monkeypatch.setattr(runtime_module, "ensure_disconnect_probe", fake_ensure_probe)
    with server_module._ACTIVE_LEASE_TOOL_CALLS_LOCK:
        server_module._ACTIVE_LEASE_TOOL_CALLS.clear()
        server_module._ACTIVE_LEASE_TOOL_LAST_AT.clear()
        server_module._DISCONNECT_ALERT_LAST_LAUNCH_AT.clear()

    assert server_module._disconnect_alert_candidate_ids(now=now) == []
    queued = runtime_module.disconnect_probe_state(root)
    assert queued and queued["delivered_at"] is None and queued["response_deadline_at"] is None

    later = server_module.datetime.fromtimestamp(now.timestamp() + 121, tz=server_module.timezone.utc)
    probe_now[0] = later
    assert server_module._disconnect_alert_candidate_ids(now=later) == []
    assert runtime_module.disconnect_probe_state(root)["delivered_at"] is None


def test_legacy_watchdog_bypass_marker_cannot_claim_an_undelivered_probe(monkeypatch, tmp_path):
    isolate_runtime(monkeypatch, tmp_path)
    workspace = tmp_path / "program-watchdog-root"
    workspace.mkdir()
    lease = runtime_module.acquire_workspace(workspace, mode="execute_exclusive", owner_label="Program Watchdog root")
    lease_id = lease["lease_id"]
    activity_at = server_module.datetime.fromtimestamp(
        server_module.datetime.now(server_module.timezone.utc).timestamp() - 600,
        tz=server_module.timezone.utc,
    ).isoformat().replace("+00:00", "Z")
    created = runtime_module.ensure_disconnect_probe(
        lease_id,
        activity_at=activity_at,
        response_seconds=server_module.DISCONNECT_ALERT_RESPONSE_SECONDS,
        defer_delivery=True,
    )
    probe_id = created["probe"]["id"]
    waiting = runtime_module.claim_disconnect_alert(lease_id, stale_seconds=server_module.DISCONNECT_ALERT_STALE_SECONDS)
    assert waiting["status"] == "probe_waiting"

    record = runtime_module.active_lease_record(lease_id)
    record["disconnect_probe_program_watchdog_direct_id"] = probe_id
    runtime_module._write_json(runtime_module._lease_path(lease_id), record)
    claimed = runtime_module.claim_disconnect_alert(lease_id, stale_seconds=server_module.DISCONNECT_ALERT_STALE_SECONDS)
    assert claimed["status"] == "probe_waiting"
    assert runtime_module.disconnect_probe_state(runtime_module.active_lease_record(lease_id))["delivered_at"] is None


def test_informational_console_notice_is_visible_but_non_blocking():
    work_dir = PROJECT_ROOT / f"control-info-{uuid.uuid4().hex}"
    target = work_dir / "data.txt"
    work_dir.mkdir(parents=True)
    lease_id = None
    try:
        acquired = call_tool(
            "acquire_workspace",
            {"path": str(work_dir), "mode": "execute_exclusive", "owner_label": "信息通知门禁测试"},
        )
        lease_id = acquired["lease_id"]
        thread_id = acquired["thread_id"]
        server_module.UI_INPUT_FILE.parent.mkdir(parents=True, exist_ok=True)
        server_module.UI_INPUT_FILE.write_text(
            json.dumps({
                "id": "info-only",
                "type": "ui.message",
                "scope": "task",
                "threadId": thread_id,
                "taskId": lease_id,
                "timestampMs": 10,
                "text": "Watchdog 状态已更新；无需回应。",
                "controlBehavior": "informational",
            }, ensure_ascii=False) + "\n",
            encoding="utf-8",
        )

        assert server_module._ui_message_requires_visible_reply({
            "id": "info", "text": "状态通知", "controlBehavior": "informational",
        }) is False
        assert server_module._ui_message_requires_task_resume({
            "id": "info", "text": "状态通知", "controlBehavior": "informational",
        }) is False

        written = call_tool(
            "write_text_file",
            {"path": str(target), "content": "not-blocked", "workspace_lease_id": lease_id},
        )
        assert written["status"] == "written"
        refreshed = runtime_module.require_execute_lease(lease_id, [])
        assert refreshed.get("control_cursor_id") == "info-only"
        assert not refreshed.get("control_ack_pending_id")
        assert not refreshed.get("control_resume_pending_id")

        with server_module.UI_INPUT_FILE.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps({
                "id": "info-before-real-control",
                "type": "ui.message",
                "scope": "task",
                "threadId": thread_id,
                "taskId": lease_id,
                "timestampMs": 11,
                "text": "Watchdog 又更新了一次；无需回应。",
                "controlBehavior": "informational",
            }, ensure_ascii=False) + "\n")
            handle.write(json.dumps({
                "id": "real-control-after-info",
                "type": "ui.message",
                "scope": "task",
                "threadId": thread_id,
                "taskId": lease_id,
                "timestampMs": 12,
                "text": "真正的用户修正必须阻塞下一步",
            }, ensure_ascii=False) + "\n")

        async def blocked_write():
            async with Client(mcp) as client:
                return await client.call_tool(
                    "write_text_file",
                    {"path": str(target), "content": "must-not-write", "workspace_lease_id": lease_id},
                )

        blocked = asyncio.run(blocked_write())
        assert blocked.is_error is True
        assert "用户控制输入待处理" in blocked.content[0].text
        assert target.read_text(encoding="utf-8") == "not-blocked"
        refreshed = runtime_module.require_execute_lease(lease_id, [])
        assert refreshed.get("control_cursor_id") == "info-before-real-control"
        runtime_module.update_workspace_control_cursor(lease_id, "real-control-after-info")
    finally:
        if lease_id and runtime_module.workspace_status(work_dir)["count"]:
            runtime_module.update_workspace_control_ack_pending(lease_id, None)
            runtime_module.update_workspace_control_resume_pending(lease_id, None)
            runtime_module.release_workspace(lease_id)
        shutil.rmtree(work_dir, ignore_errors=True)


def test_shared_agent_participant_uses_one_resource_lease(tmp_path):
    workspace = tmp_path / "shared-agent-workspace"
    workspace.mkdir()
    root = runtime_module.acquire_workspace(
        workspace, mode="execute_exclusive", owner_label="Root shared lease",
        thread_id="thread-root-shared",
    )
    participant = runtime_module.attach_workspace_participant(
        root["lease_id"], logical_agent_id="ag_child_shared", owner_label="Child participant",
    )

    assert participant["lease_id"] != root["lease_id"]
    assert participant["thread_id"] != root["thread_id"]
    assert participant["lease_role"] == "participant"
    assert participant["shared_root_lease_id"] == root["lease_id"]
    assert participant["workspace"] == root["workspace"]
    assert participant["resource_lease_owner"] is False

    # Participant expiry is only a cache.  Even if stale, the live Root lease
    # remains authoritative for status scans and participant operations.
    raw_participant = runtime_module._read_json(runtime_module._lease_path(participant["lease_id"]))
    raw_participant["expires_at"] = "2000-01-01T00:00:00Z"
    runtime_module._write_json(runtime_module._lease_path(participant["lease_id"]), raw_participant)
    assert runtime_module.active_lease_record(participant["lease_id"]) is not None

    status = runtime_module.workspace_status(workspace, include_private=True)
    assert status["count"] == 1, "participant handle must not become a second workspace lock"
    assert status["leases"][0]["lease_id"] == root["lease_id"]
    assert status["leases"][0]["participant_count"] == 1
    assert status["leases"][0]["participants"][0]["lease_id"] == participant["lease_id"]

    target = workspace / "child.txt"
    runtime_module.require_execute_lease(participant["lease_id"], [target])
    runtime_module.set_ultra_mode(
        participant["lease_id"], enabled=True, user_explicit=True, actor="user",
    )
    assert runtime_module.active_lease_record(root["lease_id"])["ultra_mode"]["active"] is True
    assert runtime_module.active_lease_record(participant["lease_id"])["ultra_mode"]["active"] is True

    before_revision = int(runtime_module.active_lease_record(root["lease_id"]).get("workspace_validation_revision") or 0)
    runtime_module.bump_workspace_validation_revision(participant["lease_id"])
    assert int(runtime_module.active_lease_record(root["lease_id"])["workspace_validation_revision"]) == before_revision + 1

    with pytest.raises(runtime_module.RuntimePolicyError) as exc:
        runtime_module.release_workspace(root["lease_id"])
    assert exc.value.error_type == "SHARED_LEASE_PARTICIPANTS_ACTIVE"

    child_done = runtime_module.release_workspace(participant["lease_id"])
    assert child_done["lease_role"] == "participant"
    assert child_done["resource_lease_released"] is False
    assert runtime_module.active_lease_record(root["lease_id"]) is not None
    assert runtime_module.active_lease_record(participant["lease_id"]) is None
    assert runtime_module.release_workspace(root["lease_id"])["status"] == "released"


def test_participant_done_keeps_shared_root_and_control_identity_separate(tmp_path):
    workspace = tmp_path / "shared-agent-done"
    workspace.mkdir()
    root = runtime_module.acquire_workspace(
        workspace, mode="execute_exclusive", owner_label="Root shared done",
        thread_id="thread-root-done",
    )
    participant = runtime_module.attach_workspace_participant(
        root["lease_id"], logical_agent_id="ag_child_done", owner_label="Child done participant",
    )
    participant_id = participant["lease_id"]
    participant_thread = participant["thread_id"]

    # Root Goal is family-scoped but must not stop a finished Child Conversation from exiting.
    runtime_module.set_goal_mode(
        root["lease_id"], enabled=True, outcome="Root continues after child",
        success_criteria=["Root eventually completes"],
    )
    refreshed = runtime_module.active_lease_record(participant_id)
    assert refreshed["goal_mode"]["active"] is True

    # Console targeting remains per Conversation participant, not the shared Root identity.
    server_module.UI_INPUT_FILE.parent.mkdir(parents=True, exist_ok=True)
    server_module.UI_INPUT_FILE.write_text(json.dumps({
        "id": "participant-only-control",
        "type": "ui.message",
        "scope": "task",
        "taskId": participant_id,
        "threadId": participant_thread,
        "timestampMs": 100,
        "text": "Child only control",
    }, ensure_ascii=False) + "\n", encoding="utf-8")
    polled = call_tool("poll_ui_messages", {"workspace_lease_id": participant_id})
    assert polled["count"] == 1
    assert polled["messages"][0]["id"] == "participant-only-control"
    assert not runtime_module.active_lease_record(root["lease_id"]).get("control_ack_pending_id")

    # Clear the participant-local control gate, perform one real action, then done.
    runtime_module.update_workspace_control_ack_pending(participant_id, None)
    runtime_module.update_workspace_control_resume_pending(participant_id, None)
    runtime_module.update_workspace_control_cursor(participant_id, "participant-only-control")
    written = call_tool("write_text_file", {
        "path": str(workspace / "child-output.txt"),
        "content": "child complete",
        "workspace_lease_id": participant_id,
    })
    assert written["status"] == "written"
    done = call_tool("report_progress", {
        "phase": "done",
        "text": "Child finished its delegated work.",
        "workspace_lease_id": participant_id,
    })
    assert done["lease_released"] is True
    assert done["lease_role"] == "participant"
    assert done["resource_lease_released"] is False
    assert done["shared_root_lease_id"] == root["lease_id"]
    assert runtime_module.active_lease_record(participant_id) is None
    root_after = runtime_module.active_lease_record(root["lease_id"])
    assert root_after is not None
    assert root_after["goal_mode"]["active"] is True

    # Test cleanup: trusted user-side Goal update clears the family mode, then Root releases normally.
    runtime_module.set_goal_mode(
        root["lease_id"], enabled=False, outcome="", success_criteria=[], change_note="test cleanup",
    )
    assert runtime_module.release_workspace(root["lease_id"])["status"] == "released"


def test_delayed_or_repeated_participant_done_never_falls_back_to_shared_root(tmp_path):
    workspace = tmp_path / "shared-agent-delayed-done"
    workspace.mkdir()
    root = runtime_module.acquire_workspace(
        workspace, mode="execute_exclusive", owner_label="Root survives delayed child done",
        thread_id="thread-root-delayed-done",
    )
    participant = runtime_module.attach_workspace_participant(
        root["lease_id"], logical_agent_id="ag_child_delayed_done",
        owner_label="Child delayed done participant",
    )
    participant_id = participant["lease_id"]

    first = call_tool("report_progress", {
        "phase": "done",
        "text": "Child exits exactly its own participant.",
        "workspace_lease_id": participant_id,
    })
    assert first["lease_role"] == "participant"
    assert first["resource_lease_released"] is False
    assert first["shared_root_lease_id"] == root["lease_id"]
    assert runtime_module.active_lease_record(participant_id) is None
    assert runtime_module.active_lease_record(root["lease_id"]) is not None

    async def delayed_repeat():
        async with Client(mcp) as client:
            return await client.call_tool("report_progress", {
                "phase": "done",
                "text": "Delayed duplicate child completion must not touch Root.",
                "workspace_lease_id": participant_id,
            })

    repeated = asyncio.run(delayed_repeat())
    assert repeated.is_error is True
    assert "租约不存在" in repeated.content[0].text
    assert runtime_module.active_lease_record(root["lease_id"]) is not None
    assert runtime_module.release_workspace(root["lease_id"])["status"] == "released"


def test_precleaned_participant_delayed_done_never_releases_shared_root(tmp_path):
    workspace = tmp_path / "shared-agent-precleaned-done"
    workspace.mkdir()
    root = runtime_module.acquire_workspace(
        workspace, mode="execute_exclusive", owner_label="Root survives precleaned child",
        thread_id="thread-root-precleaned-done",
    )
    participant = runtime_module.attach_workspace_participant(
        root["lease_id"], logical_agent_id="ag_child_precleaned_done",
        owner_label="Child precleaned participant",
    )
    participant_id = participant["lease_id"]
    cleanup = runtime_module.release_workspace_participant(participant_id)
    assert cleanup["resource_lease_released"] is False
    assert runtime_module.active_lease_record(participant_id) is None

    async def delayed_done():
        async with Client(mcp) as client:
            return await client.call_tool("report_progress", {
                "phase": "done",
                "text": "Stale child completion after cleanup must fail closed.",
                "workspace_lease_id": participant_id,
            })

    result = asyncio.run(delayed_done())
    assert result.is_error is True
    assert "租约不存在" in result.content[0].text
    root_after = runtime_module.active_lease_record(root["lease_id"])
    assert root_after is not None
    assert root_after["lease_id"] == root["lease_id"]
    assert runtime_module.release_workspace(root["lease_id"])["status"] == "released"


def test_participant_continuation_never_instructs_root_recovery(tmp_path):
    workspace = tmp_path / "participant-continuation-identity"
    workspace.mkdir()
    root = runtime_module.acquire_workspace(
        workspace,
        mode="execute_exclusive",
        owner_label="Root identity authority",
        thread_id="thread-root-authority",
        runtime_instance="runtime-before-restart",
    )
    participant = runtime_module.attach_workspace_participant(
        root["lease_id"],
        logical_agent_id="ag_child_identity",
        owner_label="Child identity authority",
    )
    try:
        runtime_module.update_workspace_execution_checkpoint(
            participant["lease_id"], tool_name="read_text_file", side_effect=False
        )
        payload = server_module._runtime_task_continuation(participant["lease_id"])["runtime_task"]
        assert payload["lease_id"] == participant["lease_id"]
        assert payload["thread_id"] == participant["thread_id"]
        assert payload["lease_role"] == "participant"
        assert payload["resource_lease_owner"] is False
        assert payload["shared_root_lease_id"] == root["lease_id"]
        assert payload["recovery_action"]["action"] == "CONTINUE_PARTICIPANT"
        assert payload["recovery_action"]["lease_id"] == participant["lease_id"]
        assert payload["recovery_action"]["shared_root_lease_id"] == root["lease_id"]
        assert payload["recovery_action"]["root_resource_still_owned_by_root"] is True
        assert "DO_NOT_RECOVER_ROOT" in payload["recovery_action"]["directives"]
        assert "ROOT_OWNERSHIP_UNCHANGED" in payload["recovery_action"]["directives"]
        assert "RECOVER_EXISTING_TASK" not in payload["instruction"]
        assert "不得尝试恢复/释放/接管 Root lease" in payload["instruction"]
        root_payload = server_module._runtime_task_continuation(root["lease_id"])["runtime_task"]
        assert root_payload["recovery_action"]["action"] == "RECOVER_EXISTING_TASK"
        assert root_payload["resource_lease_owner"] is True
    finally:
        runtime_module.release_workspace_participant(participant["lease_id"])
        runtime_module.release_workspace(root["lease_id"])


def test_scoped_participants_hard_enforce_disjoint_write_ownership(tmp_path):
    workspace = tmp_path / "scoped-shared-agent"
    left = workspace / "left"
    right = workspace / "right"
    nested = left / "nested"
    for path in (workspace, left, right, nested):
        path.mkdir(exist_ok=True)
    root = runtime_module.acquire_workspace(
        workspace, mode="execute_exclusive", owner_label="Root scoped shared",
        thread_id="thread-root-scoped",
    )
    first = runtime_module.attach_workspace_participant(
        root["lease_id"], logical_agent_id="ag_scoped_left", owner_label="Left child",
        write_paths=[str(left)],
    )
    second = runtime_module.attach_workspace_participant(
        root["lease_id"], logical_agent_id="ag_scoped_right", owner_label="Right child",
        write_paths=[str(right)],
    )
    try:
        runtime_module.require_execute_lease(first["lease_id"], [left / "ok.txt"])
        runtime_module.require_execute_lease(second["lease_id"], [right / "ok.txt"])

        with pytest.raises(runtime_module.RuntimePolicyError) as exc:
            runtime_module.require_execute_lease(first["lease_id"], [right / "escape.txt"])
        assert exc.value.error_type == "PARTICIPANT_WRITE_SCOPE_VIOLATION"

        with pytest.raises(runtime_module.RuntimePolicyError) as exc:
            runtime_module.require_execute_lease(root["lease_id"], [left / "root-race.txt"])
        assert exc.value.error_type == "PARTICIPANT_RESOURCE_OWNERSHIP_CONFLICT"
        runtime_module.require_execute_lease(root["lease_id"], [workspace / "outside-delegation.txt"])

        with pytest.raises(runtime_module.RuntimePolicyError) as exc:
            runtime_module.attach_workspace_participant(
                root["lease_id"], logical_agent_id="ag_overlap_sibling", owner_label="Overlap sibling",
                write_paths=[str(nested)],
            )
        assert exc.value.error_type == "PARTICIPANT_WRITE_SCOPE_CONFLICT"

        child = runtime_module.attach_workspace_participant(
            first["lease_id"], logical_agent_id="ag_nested_child", owner_label="Nested child",
            write_paths=[str(nested)],
        )
        try:
            runtime_module.require_execute_lease(child["lease_id"], [nested / "child.txt"])
            runtime_module.require_execute_lease(first["lease_id"], [left / "parent-only.txt"])
            with pytest.raises(runtime_module.RuntimePolicyError) as exc:
                runtime_module.require_execute_lease(first["lease_id"], [nested / "parent-race.txt"])
            assert exc.value.error_type == "PARTICIPANT_RESOURCE_OWNERSHIP_CONFLICT"
        finally:
            runtime_module.release_workspace_participant(child["lease_id"])
    finally:
        runtime_module.release_workspace_participant(first["lease_id"])
        runtime_module.release_workspace_participant(second["lease_id"])
        runtime_module.release_workspace(root["lease_id"])


def test_shared_root_release_race_never_orphans_participants(tmp_path):
    workspace = tmp_path / "shared-release-race"
    workspace.mkdir()
    root = runtime_module.acquire_workspace(
        workspace, mode="execute_exclusive", owner_label="Root release race",
        thread_id="thread-root-release-race",
    )
    participants = []
    for index in range(8):
        scope = workspace / f"worker-{index}"
        scope.mkdir()
        participants.append(runtime_module.attach_workspace_participant(
            root["lease_id"], logical_agent_id=f"ag_release_{index}", owner_label=f"worker {index}",
            write_paths=[str(scope)],
        ))

    barrier = threading.Barrier(len(participants) + 1)
    def release_child(participant):
        barrier.wait()
        return runtime_module.release_workspace_participant(participant["lease_id"])["status"]

    def release_root():
        barrier.wait()
        try:
            return runtime_module.release_workspace(root["lease_id"])["status"]
        except runtime_module.RuntimePolicyError as exc:
            return exc.error_type

    with ThreadPoolExecutor(max_workers=len(participants) + 1) as pool:
        futures = [pool.submit(release_child, participant) for participant in participants]
        root_future = pool.submit(release_root)
        child_results = [future.result() for future in futures]
        root_result = root_future.result()

    assert child_results == ["released"] * len(participants)
    assert root_result in {"released", "SHARED_LEASE_PARTICIPANTS_ACTIVE"}
    assert all(runtime_module.active_lease_record(p["lease_id"]) is None for p in participants)
    if runtime_module.active_lease_record(root["lease_id"]) is not None:
        assert root_result == "SHARED_LEASE_PARTICIPANTS_ACTIVE"
        assert runtime_module.release_workspace(root["lease_id"])["status"] == "released"


def test_parallel_same_agent_attach_reuses_one_participant(tmp_path):
    workspace = tmp_path / "parallel-participant-idempotency"
    scope = workspace / "scope"
    scope.mkdir(parents=True)
    root = runtime_module.acquire_workspace(
        workspace, mode="execute_exclusive", owner_label="Root attach race",
        thread_id="thread-root-attach-race",
    )

    def attach_same(_):
        return runtime_module.attach_workspace_participant(
            root["lease_id"], logical_agent_id="ag_same_parallel", owner_label="Same parallel child",
            write_paths=[str(scope)],
        )

    try:
        with ThreadPoolExecutor(max_workers=16) as pool:
            rows = list(pool.map(attach_same, range(48)))
        assert len({row["lease_id"] for row in rows}) == 1
        assert len({row["thread_id"] for row in rows}) == 1
        assert sum(not row.get("reused_existing") for row in rows) == 1
        status = runtime_module.workspace_status(workspace, include_private=True)
        assert status["leases"][0]["participant_count"] == 1
    finally:
        participant_id = rows[0]["lease_id"]
        runtime_module.release_workspace_participant(participant_id)
        runtime_module.release_workspace(root["lease_id"])


def test_parallel_overlapping_siblings_have_exactly_one_scope_owner(tmp_path):
    workspace = tmp_path / "parallel-overlap-owner"
    shared = workspace / "shared"
    shared.mkdir(parents=True)
    root = runtime_module.acquire_workspace(
        workspace, mode="execute_exclusive", owner_label="Root overlap race",
        thread_id="thread-root-overlap-race",
    )

    def attach_sibling(index):
        try:
            row = runtime_module.attach_workspace_participant(
                root["lease_id"], logical_agent_id=f"ag_overlap_{index}", owner_label=f"Overlap {index}",
                write_paths=[str(shared)],
            )
            return ("ok", row["lease_id"])
        except runtime_module.RuntimePolicyError as exc:
            return (exc.error_type, None)

    try:
        with ThreadPoolExecutor(max_workers=16) as pool:
            results = list(pool.map(attach_sibling, range(32)))
        winners = [lease_id for status, lease_id in results if status == "ok"]
        conflicts = [status for status, _ in results if status != "ok"]
        assert len(winners) == 1
        assert conflicts == ["PARTICIPANT_WRITE_SCOPE_CONFLICT"] * 31
        status = runtime_module.workspace_status(workspace, include_private=True)
        assert status["leases"][0]["participant_count"] == 1
    finally:
        if winners:
            runtime_module.release_workspace_participant(winners[0])
        runtime_module.release_workspace(root["lease_id"])


def test_liveness_scan_does_not_read_soft_watchdog_policy(monkeypatch):
    monkeypatch.setattr(runtime_module, '_lease_records_snapshot', lambda: [])
    monkeypatch.setattr(server_module, 'AgentStore', lambda *_args, **_kwargs: (_ for _ in ()).throw(AssertionError('liveness is independent of soft supervision')))
    assert server_module._disconnect_alert_candidate_ids() == []
