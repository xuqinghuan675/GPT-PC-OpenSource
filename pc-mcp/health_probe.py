from __future__ import annotations

import asyncio
import argparse
import json
from pathlib import Path
from typing import Any

from mcp import Client

PROJECT_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_MCP_URL = "http://127.0.0.1:18765/mcp"


# 健康检查验证关键能力与关键 schema，而不是把工具总数写死。
# 工具增加属于正常升级，不应让已正常运行的 MCP 被误报为失败。
REQUIRED_TOOL_NAMES = frozenset(
    {
        "computer_info",
        "list_drives",
        "list_directory",
        "acquire_workspace",
        "release_workspace",
        "report_progress",
        "gmail",
        "save_chatgpt_upload",
        "capture_screenshot",
        "inspect_image",
        "poll_ui_messages",
        "publish_thread_note",
        "read_thread_notes",
        "relay_console_user_message",
        "code_navigate",
        "start_job",
        "get_job",
        "runtime_status",
        "set_visual_mode",
        "computer_use",
        "camera",
        "ultra_root",
        "ultra_agent",
        "ultra_recovery",
        "ultra_finalize",
        "chatgpt_agent",
    }
)


async def probe(target: Any = DEFAULT_MCP_URL) -> dict[str, Any]:
    # A health check must contact the running endpoint. Importing server here
    # would create a separate instance and could hide a dead production service.
    async with Client(target) as client:
        tool_response = await client.list_tools()
        tools = {tool.name: tool for tool in tool_response.tools}
        missing_tools = sorted(REQUIRED_TOOL_NAMES - tools.keys())
        if missing_tools:
            raise RuntimeError("关键工具缺失：" + "、".join(missing_tools))

        acquire_schema = tools["acquire_workspace"].input_schema.get("properties", {})
        poll_schema = tools["poll_ui_messages"].input_schema.get("properties", {})
        relay_input_schema = tools["relay_console_user_message"].input_schema
        relay_schema = relay_input_schema.get("properties", {})
        relay_required_fields = set(relay_input_schema.get("required", []))
        progress_description = tools["report_progress"].description or ""
        if "thread_id" not in acquire_schema:
            raise RuntimeError("acquire_workspace 缺少 thread_id schema")
        if "workspace_lease_id" not in poll_schema:
            raise RuntimeError("poll_ui_messages 缺少 workspace_lease_id schema")
        relay_required = {"target_lease_id", "text", "user_authorized", "allow_task_control", "workspace_lease_id"}
        if not relay_required.issubset(relay_schema):
            raise RuntimeError("跨对话 Console 中继 schema 不完整")
        if not {"target_lease_id", "text", "user_authorized", "workspace_lease_id"}.issubset(relay_required_fields):
            raise RuntimeError("跨对话 Console 中继缺少必需的发送身份字段")
        if "phase=done" not in progress_description or "释放" not in progress_description:
            raise RuntimeError("report_progress 缺少 done 原子释放语义")

        visual_schema = tools["set_visual_mode"].input_schema.get("properties", {})
        if not {"goal_enabled", "goal_outcome", "goal_success_criteria", "targets", "depth"}.issubset(visual_schema):
            raise RuntimeError("目标/看图模式入口不完整")

        if len(tools) > 100:
            raise RuntimeError(f"工具总数超过治理上限：{len(tools)} > 100")
        retired = {"list_directory_tree", "read_text_file_with_hash", "calculate_hash", "rename_path", "run_cmd"}
        leaked = sorted(retired & tools.keys())
        if leaked:
            raise RuntimeError("已合并旧工具仍暴露：" + "、".join(leaked))
        if set(tools["ultra_root"].input_schema["properties"]["action"].get("enum", [])) < {"spawn", "task_create", "message"}:
            raise RuntimeError("ultra_root schema 不完整")
        if set(tools["ultra_agent"].input_schema["properties"]["action"].get("enum", [])) < {"join", "resume", "task_update", "checkpoint"}:
            raise RuntimeError("ultra_agent schema 不完整")
        if set(tools["ultra_recovery"].input_schema["properties"]["action"].get("enum", [])) < {"recover_member", "root_rescue", "reconcile"}:
            raise RuntimeError("ultra_recovery schema 不完整")
        if "notification_email" not in tools["ultra_finalize"].input_schema["properties"]:
            raise RuntimeError("ultra_finalize 缺少 notification_email")
        camera_schema = tools["camera"].input_schema
        if set(camera_schema["properties"]["action"].get("enum", [])) != {"list", "photo", "video"}:
            raise RuntimeError("camera action schema 不完整")
        if "workspace_lease_id" not in set(camera_schema.get("required", [])):
            raise RuntimeError("camera 必须绑定 desktop_ops lease")

        runtime_response = await client.call_tool("runtime_status", {})
        computer_response = await client.call_tool("computer_info", {})
        drives_response = await client.call_tool("list_drives", {})
        directory_response = await client.call_tool(
            "list_directory", {"path": str(PROJECT_ROOT), "max_results": 50}
        )
        for response in (runtime_response, computer_response, drives_response, directory_response):
            if response.is_error or not isinstance(response.structured_content, dict):
                raise RuntimeError("实际 MCP 关键工具调用失败")
        runtime = runtime_response.structured_content
        if not runtime.get("instance_id") or not runtime.get("pid"):
            raise RuntimeError("实际服务缺少运行实例身份")
        return {
            "status": "PASS",
            "endpoint": target if isinstance(target, str) else "in-process-test",
            "runtime_instance": runtime["instance_id"],
            "runtime_pid": runtime["pid"],
            "tool_count": len(tool_response.tools),
            "control_tools": sorted(
                name
                for name in (
                    "poll_ui_messages",
                    "publish_thread_note",
                    "read_thread_notes",
                    "relay_console_user_message",
                )
                if name in tools
            ),
            "acquire_has_thread_id": "thread_id" in acquire_schema,
            "computer_info": computer_response.structured_content,
            "drive_count": drives_response.structured_content.get("count"),
            "project_root_entries": directory_response.structured_content.get("count"),
        }


async def main() -> None:
    parser = argparse.ArgumentParser(description="验证正在运行的 MCP 服务")
    parser.add_argument("--url", default=DEFAULT_MCP_URL)
    options = parser.parse_args()
    result = await asyncio.wait_for(probe(options.url), timeout=45)
    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    asyncio.run(main())
