"""GPT-PC - local Windows computer MCP server.

The server intentionally exposes the capabilities of the Windows user that
starts it.  It uses stdio, so stdout is reserved for MCP protocol messages;
operational logs are written to stderr and to the configured runtime logs directory.
"""

from __future__ import annotations

import argparse
import base64
import difflib
from contextlib import contextmanager
import fnmatch
from functools import wraps
import hashlib
import inspect
import json
import logging
from logging.handlers import RotatingFileHandler
import ntpath
import os
import platform
import re
import shlex
import shutil
import signal
import socket
import stat as stat_module
import subprocess
import sys
import tempfile
from context_patch import apply_context_patch
import time
import threading
import uuid
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Literal, NotRequired, TypedDict

import psutil
from mcp.server import MCPServer
from mcp.server.mcpserver.exceptions import ToolError
from mcp.types import BlobResourceContents, EmbeddedResource, ImageContent, TextContent, ToolAnnotations

from code_navigation import navigate_code as _navigate_code
import native_acceleration as native_backend
from mode_commands import parse_stay_command as _shared_parse_stay_command
import runtime as mode_runtime
import visual as visual_io
import browser_validation
import computer_use as computer_backend
from machine_config import load_machine_config
from chatgpt_agent import AgentManager, Caller, is_read_action as _agent_read_action
from chatgpt_agent_store import AgentError, AgentStore
from mode_commands import parse_visual_duration
from runtime import (
    DEFAULT_LEASE_SECONDS,
    MAX_LEASE_SECONDS,
    DESKTOP_OPS_WORKSPACE,
    CONSOLE_RELAY_WORKSPACE,
    LEASE_TYPE_CONSOLE_RELAY,
    RUNTIME_DIR,
    RUNTIME_MAINTENANCE_WORKSPACE,
    RuntimePolicyError,
    active_lease_record as _active_lease_record,
    acquire_workspace as _acquire_workspace,
    cancel_job as _cancel_job,
    create_job as _create_job,
    ensure_workspace_releasable as _ensure_workspace_releasable,
    get_job as _get_job,
    job_for_pid as _job_for_pid,
    job_history_count as _job_history_count,
    list_active_jobs as _list_active_jobs,
    list_jobs as _list_jobs,
    read_job_tail as _read_job_tail,
    release_workspace as _release_workspace,
    renew_workspace as _renew_workspace,
    require_execute_lease as _require_execute_lease,
    set_workspace_stay_mode as _set_workspace_stay_mode,
    update_workspace_control_ack_pending as _update_workspace_control_ack_pending,
    update_workspace_control_cursor as _update_workspace_control_cursor,
    update_workspace_control_resume_pending as _update_workspace_control_resume_pending,
    update_workspace_execution_checkpoint as _update_workspace_execution_checkpoint,
    workspace_status as _workspace_status,
)


PROJECT_ROOT = mode_runtime.PROJECT_ROOT
LOG_DIR = PROJECT_ROOT / "logs"
DISCONNECT_ALERT_HELPER = PROJECT_ROOT / "scripts" / "gpt-disconnect-notifier.py"
DISCONNECT_ALERT_LOG = LOG_DIR / "gpt-disconnect-notifier.log"
DISCONNECT_ALERT_PROBE_AFTER_SECONDS = 3 * 60
DISCONNECT_ALERT_RESPONSE_SECONDS = 2 * 60
DISCONNECT_ALERT_STALE_SECONDS = DISCONNECT_ALERT_PROBE_AFTER_SECONDS + DISCONNECT_ALERT_RESPONSE_SECONDS
ROOT_RESCUE_GRACE_SECONDS = 5 * 60
DISCONNECT_ALERT_SCAN_SECONDS = 15.0
DISCONNECT_ALERT_LAUNCH_COOLDOWN_SECONDS = 60.0
DEFAULT_CWD = PROJECT_ROOT
MAX_TOOL_OUTPUT_CHARS = 100_000
MAX_RESULT_ITEMS = 1_000
EDIT_FEEDBACK_MAX_CHARS = 12_000
EDIT_FEEDBACK_MAX_SOURCE_CHARS = 2_000_000
MCP_LOG_MAX_BYTES = 2 * 1024 * 1024
MCP_LOG_BACKUP_COUNT = 4
# Tool admission is lane-based. Control/status traffic never waits behind bulk
# reads/searches/tests, while same-GPT independent work can fan out within the
# bounded capacity of its resource class.
TOOL_QUEUE_TIMEOUT_SECONDS = 600
PATH_LOCK_STRIPE_COUNT = 256
LEASE_MUTATION_LOCK_STRIPE_COUNT = 256
TOOL_LANE_LIMITS = {
    "control": 8,
    "file_read": 32,
    "search": 8,
    "git_read": 12,
    "code_navigation": 6,
    "execution": 8,
    "network": 12,
    "mutation": 8,
    "default": 16,
}
DEFAULT_IGNORED_DIRECTORY_NAMES = frozenset(
    {
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
    }
)
MCP_INSTANCE_ID = uuid.uuid4().hex
ACTIVITY_DATA_DIR = PROJECT_ROOT / "chatgpt-ui-data"
ACTIVITY_EVENT_FILE = ACTIVITY_DATA_DIR / "events.jsonl"
ACTIVITY_EVENT_BACKUP_FILE = ACTIVITY_DATA_DIR / "events.previous.jsonl"
ACTIVITY_EVENT_MAX_BYTES = 4 * 1024 * 1024
ACTIVITY_EVENT_HISTORY_COUNT = 128
UI_INPUT_FILE = ACTIVITY_DATA_DIR / "ui-inbox.jsonl"
UI_INPUT_LOCK_FILE = ACTIVITY_DATA_DIR / ".ui-inbox.lock"
UI_INPUT_MAX_BYTES = 8 * 1024 * 1024
UI_INPUT_RETAIN_BYTES = 4 * 1024 * 1024
UI_INPUT_LOCK_WAIT_SECONDS = 2.0
UI_INPUT_LOCK_STALE_SECONDS = 5.0
UI_ATTACHMENT_DIR = mode_runtime.MEDIA_ATTACHMENT_DIR
UI_ATTACHMENT_MAX_BYTES = 25 * 1024 * 1024
THREAD_NOTE_FILE = ACTIVITY_DATA_DIR / "thread-notes.jsonl"
THREAD_CONTROL_READ_MAX_BYTES = 8 * 1024 * 1024
THREAD_CONTROL_TEXT_MAX_CHARS = 4_000
ACTIVITY_EVENT_MAX_TEXT_CHARS = 4_000
ACTIVITY_EVENT_MAX_DIFF_CHARS = 8 * 1024
ACTIVITY_DIFF_SOURCE_MAX_BYTES = 256 * 1024
_ACTIVITY_EVENT_LOCK = threading.Lock()


def _configure_logging() -> logging.Logger:
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    logger = logging.getLogger("gpt_pc")
    if logger.handlers:
        return logger
    logger.setLevel(logging.INFO)
    formatter = logging.Formatter(
        "%(asctime)s %(levelname)s %(name)s %(message)s",
        datefmt="%Y-%m-%dT%H:%M:%S%z",
    )
    stream_handler = logging.StreamHandler(sys.stderr)
    stream_handler.setFormatter(formatter)
    logger.addHandler(stream_handler)
    try:
        file_handler = RotatingFileHandler(
            LOG_DIR / "mcp-server.log",
            maxBytes=MCP_LOG_MAX_BYTES,
            backupCount=MCP_LOG_BACKUP_COUNT,
            encoding="utf-8",
        )
        file_handler.setFormatter(formatter)
        logger.addHandler(file_handler)
        root_logger = logging.getLogger()
        root_logger.setLevel(logging.INFO)
        root_logger.addHandler(file_handler)
    except OSError:
        # The protocol must remain usable even when the log file cannot be opened.
        pass
    logger.propagate = False
    return logger


LOGGER = _configure_logging()


READ_ANNOTATIONS = ToolAnnotations(
    readOnlyHint=True,
    destructiveHint=False,
    idempotentHint=True,
    openWorldHint=False,
)
OPEN_READ_ANNOTATIONS = ToolAnnotations(
    readOnlyHint=True,
    destructiveHint=False,
    idempotentHint=True,
    openWorldHint=True,
)
WRITE_ANNOTATIONS = ToolAnnotations(
    readOnlyHint=False,
    destructiveHint=False,
    idempotentHint=False,
    openWorldHint=False,
)
OPEN_WRITE_ANNOTATIONS = ToolAnnotations(
    readOnlyHint=False,
    destructiveHint=False,
    idempotentHint=False,
    openWorldHint=True,
)
DESTRUCTIVE_ANNOTATIONS = ToolAnnotations(
    readOnlyHint=False,
    destructiveHint=True,
    idempotentHint=False,
    openWorldHint=False,
)
COMMAND_ANNOTATIONS = ToolAnnotations(
    readOnlyHint=False,
    destructiveHint=True,
    idempotentHint=False,
    openWorldHint=True,
)


mcp = MCPServer(
    name="GPT-PC",
    title="GPT-PC",
    description=(
        "Windows 本机 AI Agent MCP 服务：Runtime、文件、代码、Git、进程、浏览器、Computer Use、Camera、可选 Gmail 与多 Agent 协作。项目说明见 README.md，部署说明见 INSTALL.md。"
    ),
    instructions=(
        "为用户授权的本机 AI 工作流提供 MCP 工具。写操作应保护已有文件并遵循工作区控制规则；stdio 模式下 stdout 仅用于 MCP 协议，诊断写入 stderr。浏览器、虚拟桌面和 Gmail 属于可选集成，缺少依赖或本机配置时应给出清晰错误。活动控制台仅显示显式公开进度和脱敏工具活动，不采集私有模型推理。"
    ),
    version="0.1.0",
)


_TOOL_LANE_SEMAPHORES = {
    name: threading.BoundedSemaphore(limit)
    for name, limit in TOOL_LANE_LIMITS.items()
}
_PATH_LOCKS = [threading.RLock() for _ in range(PATH_LOCK_STRIPE_COUNT)]
_LEASE_MUTATION_LOCKS = [threading.RLock() for _ in range(LEASE_MUTATION_LOCK_STRIPE_COUNT)]
_ACTIVE_LEASE_TOOL_CALLS_LOCK = threading.RLock()
_ACTIVE_LEASE_TOOL_CALLS: dict[str, int] = {}
_ACTIVE_LEASE_TOOL_LAST_AT: dict[str, datetime] = {}
_DISCONNECT_ALERT_LAST_LAUNCH_AT: dict[str, datetime] = {}
_MUTATING_TOOL_NAMES = frozenset(
    {
        "create_file",
        "write_text_file",
        "append_text_file",
        "replace_text",
        "apply_patch",
        "copy_path",
        "move_path",
        "make_directory",
        "delete_path",
        "save_chatgpt_upload",
    }
)
_LEASE_REQUIRED_TOOL_NAMES = frozenset(
    {
        *_MUTATING_TOOL_NAMES,
        "chatgpt_agent",
        "ultra_root",
        "ultra_agent",
        "ultra_recovery",
        "ultra_finalize",
        "camera",
        "run_powershell",
        "run_python",
        "run_node",
        "run_npm",
        "run_npx",
        "git_init",
        "git_clone",
        "git_fetch",
        "git_remote",
        "git_add",
        "git_commit",
        "git_pull",
        "git_push",
        "git_branch",
        "git_switch",
        "git_restore",
        "start_process",
        "stop_process",
        "start_job",
        "cancel_job",
        "gmail",
        "apply_stay_command",
        "set_visual_mode",
        "capture_screenshot",
        "computer_use",
        "report_progress",
    }
)
_QUIET_ACTIVITY_TOOL_NAMES = frozenset({"poll_ui_messages", "read_thread_notes", "relay_console_user_message"})
_CONTROL_GATED_TOOL_NAMES = frozenset((_LEASE_REQUIRED_TOOL_NAMES - {"report_progress"}) | {"renew_workspace", "release_workspace", "relay_console_user_message", "force_exit_stay_mode"})
_CONTROL_PRE_ACK_ALLOWED_TOOL_NAMES = frozenset({"poll_ui_messages", "read_ui_attachment", "get_visual_context", "inspect_image"})
_CONTROL_RESUME_NON_ACTION_TOOL_NAMES = frozenset(
    {
        "acquire_workspace",
        "renew_workspace",
        "release_workspace",
        "report_progress",
        "poll_ui_messages",
        "read_ui_attachment",
        "inspect_image",
        "get_visual_context",
        "workspace_status",
        "runtime_status",
        "computer_info",
        "publish_thread_note",
        "read_thread_notes",
        "force_exit_stay_mode",
        "relay_console_user_message",
    }
)
_AUDITED_TOOL_NAMES = frozenset(
    {
        *_MUTATING_TOOL_NAMES,
        "chatgpt_agent",
        "ultra_root",
        "ultra_agent",
        "ultra_recovery",
        "ultra_finalize",
        "camera",
        "acquire_workspace",
        "renew_workspace",
        "release_workspace",
        "start_job",
        "cancel_job",
        "gmail",
        "force_exit_stay_mode",
        "relay_console_user_message",
        "start_process",
        "stop_process",
        "run_powershell",
        "run_python",
        "run_node",
        "run_npm",
        "run_npx",
        "computer_use",
        "git_init",
        "git_clone",
        "git_fetch",
        "git_remote",
        "git_add",
        "git_commit",
        "git_pull",
        "git_push",
        "git_branch",
        "git_switch",
        "git_restore",
    }
)
_CONTROL_LANE_TOOL_NAMES = frozenset(
    {
        "runtime_status",
        "workspace_status",
        "acquire_workspace",
        "renew_workspace",
        "release_workspace",
        "poll_ui_messages",
        "report_progress",
        "cancel_job",
        "stop_process",
        "force_exit_stay_mode",
        "relay_console_user_message",
        "apply_stay_command",
        "publish_thread_note",
        "read_thread_notes",
    }
)
_FILE_READ_LANE_TOOL_NAMES = frozenset(
    {
        "list_directory",
        "file_info",
        "read_text_file",
        "read_text_tail",
        "read_text_range",
        "read_multiple_files",
        "read_binary_file",
    }
)
_SEARCH_LANE_TOOL_NAMES = frozenset({"search_files", "search_text"})
_GIT_READ_LANE_TOOL_NAMES = frozenset({"git_status", "git_diff", "git_log", "git_show", "git_rev_parse"})
_CODE_NAVIGATION_LANE_TOOL_NAMES = frozenset({"code_navigate"})
_EXECUTION_LANE_TOOL_NAMES = frozenset(
    {
        "run_powershell",
        "run_python",
        "run_node",
        "camera",
        "run_npm",
        "run_npx",
        "computer_use",
        "start_process",
        "start_job",
        "wait_process",
        "get_job",
        "tail_job",
    }
)
_NETWORK_LANE_TOOL_NAMES = frozenset({"http_probe", "gmail", "chatgpt_agent", "ultra_root", "ultra_agent", "ultra_recovery", "ultra_finalize"})
_GIT_MUTATING_TOOL_NAMES = frozenset(
    {
        "git_init",
        "git_clone",
        "git_fetch",
        "git_remote",
        "git_add",
        "git_commit",
        "git_pull",
        "git_push",
        "git_branch",
        "git_switch",
        "git_restore",
    }
)
_WORKSPACE_MUTATION_TOOL_NAMES = frozenset(set(_MUTATING_TOOL_NAMES) | set(_GIT_MUTATING_TOOL_NAMES))
# A deliberately small, conservative source-revision signal. File mutations and
# commands can change the product under test; read-only checks and screenshots do not.
_VALIDATION_REVISION_TOOL_NAMES = frozenset({
    "create_file", "write_text_file", "append_text_file", "replace_text", "apply_patch",
    "copy_path", "move_path", "delete_path",
    "run_powershell", "run_python", "run_node", "run_npm", "run_npx",
    "git_pull", "git_switch", "git_restore", "git_clone",
})


def _mark_active_lease_tool(lease_id: str, delta: int) -> None:
    if not lease_id:
        return
    with _ACTIVE_LEASE_TOOL_CALLS_LOCK:
        _ACTIVE_LEASE_TOOL_LAST_AT[lease_id] = datetime.now(timezone.utc)
        next_count = int(_ACTIVE_LEASE_TOOL_CALLS.get(lease_id, 0)) + delta
        if next_count > 0:
            _ACTIVE_LEASE_TOOL_CALLS[lease_id] = next_count
        else:
            _ACTIVE_LEASE_TOOL_CALLS.pop(lease_id, None)


def _active_lease_tool_count(lease_id: str) -> int:
    with _ACTIVE_LEASE_TOOL_CALLS_LOCK:
        return int(_ACTIVE_LEASE_TOOL_CALLS.get(lease_id, 0))


def _lease_recent_tool_activity_at(lease_id: str) -> datetime | None:
    with _ACTIVE_LEASE_TOOL_CALLS_LOCK:
        return _ACTIVE_LEASE_TOOL_LAST_AT.get(lease_id)


def _liveness_thread_id(record: dict[str, Any]) -> str:
    # Historical name retained for compatibility. The identity is now the
    # stable GPT controller, not one ephemeral Runtime thread/lease handle.
    return mode_runtime.lease_controller_id(record)


def _execute_thread_records(record: dict[str, Any]) -> list[dict[str, Any]]:
    controller_id = _liveness_thread_id(record)
    if not controller_id:
        return []
    return [
        item
        for _path, item in mode_runtime._lease_records_snapshot()
        if str(item.get("mode") or "") == "execute_exclusive"
        and _liveness_thread_id(item) == controller_id
        and not str(item.get("thread_id") or "").startswith("system-disconnect-alert-")
    ]


def _record_observable_activity(record: dict[str, Any]) -> datetime | None:
    lease_id = str(record.get("lease_id") or "")
    heartbeat = mode_runtime._mode_deadline(mode_runtime._lease_heartbeat_at(record))
    recent_tool_activity = _lease_recent_tool_activity_at(lease_id)
    return max(
        (value for value in (heartbeat, recent_tool_activity) if value is not None),
        default=None,
    )


def _controller_observable_activity(members: list[dict[str, Any]]) -> datetime | None:
    return max(
        (value for value in (_record_observable_activity(item) for item in members) if value is not None),
        default=None,
    )


def _pending_controller_liveness(
    members: list[dict[str, Any]],
) -> list[tuple[dict[str, Any], dict[str, Any]]]:
    pending: list[tuple[dict[str, Any], dict[str, Any]]] = []
    for item in members:
        probe = mode_runtime.disconnect_probe_state(item)
        if not probe or probe.get("acked_at"):
            continue
        probe_activity = mode_runtime._mode_deadline(probe.get("activity_at"))
        item_lease_id = str(item.get("lease_id") or "")
        sibling_activity = max(
            (
                value
                for other in members
                if str(other.get("lease_id") or "") != item_lease_id
                for value in [_record_observable_activity(other)]
                if value is not None
            ),
            default=None,
        )
        if sibling_activity is not None and probe_activity is not None and sibling_activity > probe_activity:
            # Only a DIFFERENT handle of the same stable controller may prove a
            # newer visible turn resumed. Activity on the probed lease itself
            # can be detached/ghost execution and must not cancel its own probe.
            try:
                mode_runtime.clear_disconnect_probe(
                    item_lease_id,
                    probe_id=str(probe.get("id") or ""),
                    reason="same_controller_new_handle_activity_resumed",
                )
            except Exception:
                LOGGER.warning("disconnect_probe_clear_pending controller=%s", _liveness_thread_id(item))
            continue
        pending.append((item, probe))
    pending.sort(key=lambda pair: str(pair[1].get("sent_at") or ""))
    return pending


def _thread_pending_liveness_probe(record: dict[str, Any]) -> tuple[dict[str, Any], dict[str, Any]] | None:
    pending = _pending_controller_liveness(_execute_thread_records(record))
    return pending[0] if pending else None


def _deliver_pending_thread_liveness(record: dict[str, Any]) -> dict[str, Any] | None:
    pending = _thread_pending_liveness_probe(record)
    if pending is None:
        return None
    owner, probe = pending
    owner_lease_id = str(owner.get("lease_id") or "")
    via_lease_id = str(record.get("lease_id") or "")
    if not probe.get("delivered_at") or not probe.get("response_deadline_at"):
        delivered = mode_runtime.deliver_disconnect_probe(
            owner_lease_id,
            str(probe.get("id") or ""),
            response_seconds=DISCONNECT_ALERT_RESPONSE_SECONDS,
            delivered_via_lease_id=via_lease_id,
        )
        probe = (delivered.get("probe") or probe) if isinstance(delivered, dict) else probe
    return {
        **probe,
        "owner_lease_id": owner_lease_id,
        "reply_via_lease_id": via_lease_id,
        "acknowledged": False,
        "reply_required": True,
        "priority": "system_liveness",
        "non_preemptive": True,
        "text": (
            "高优先级存活确认：Runtime 已连续约 3 分钟没有观察到该 GPT controller 任一权威 lease 的可信活动。"
            "请在下一步先调用一次 report_progress，用一句自然语言明确回复仍在工作并继续原任务；"
            "单个 lease 静默只代表 handle 可能 stale，不代表整个 GPT 对话断线。"
            "普通 tool/poll 不算显式回复；明确回复后才取消约 2 分钟后的掉线告警。"
        ),
    }


def _ack_pending_thread_liveness(record: dict[str, Any]) -> dict[str, Any] | None:
    pending = _thread_pending_liveness_probe(record)
    if pending is None:
        return None
    owner, probe = pending
    owner_lease_id = str(owner.get("lease_id") or "")
    via_lease_id = str(record.get("lease_id") or "")
    ack = mode_runtime.ack_disconnect_probe(
        owner_lease_id,
        str(probe.get("id") or ""),
        acknowledged_via_lease_id=via_lease_id,
    )
    if ack.get("status") not in {"acked", "already_acked"}:
        return None
    stored = ack.get("probe") or probe
    return {
        **stored,
        "owner_lease_id": owner_lease_id,
        "reply_via_lease_id": via_lease_id,
        "acknowledged": True,
        "reply_required": False,
        "priority": "system_liveness",
        "non_preemptive": True,
        "text": "已收到 GPT 的显式 report_progress 存活回复；本轮掉线告警已取消，继续原任务。",
    }


def _disconnect_alert_launch_is_cooling_down(lease_id: str, *, now: datetime) -> bool:
    with _ACTIVE_LEASE_TOOL_CALLS_LOCK:
        launched_at = _DISCONNECT_ALERT_LAST_LAUNCH_AT.get(lease_id)
    return bool(
        launched_at
        and (now - launched_at).total_seconds() < DISCONNECT_ALERT_LAUNCH_COOLDOWN_SECONDS
    )


def _mark_disconnect_alert_launch(lease_id: str) -> None:
    with _ACTIVE_LEASE_TOOL_CALLS_LOCK:
        _DISCONNECT_ALERT_LAST_LAUNCH_AT[lease_id] = datetime.now(timezone.utc)


def _prune_lease_tool_activity(active_lease_ids: set[str]) -> None:
    with _ACTIVE_LEASE_TOOL_CALLS_LOCK:
        for lease_id in list(_ACTIVE_LEASE_TOOL_LAST_AT):
            if lease_id not in active_lease_ids:
                _ACTIVE_LEASE_TOOL_LAST_AT.pop(lease_id, None)
                _ACTIVE_LEASE_TOOL_CALLS.pop(lease_id, None)
                _DISCONNECT_ALERT_LAST_LAUNCH_AT.pop(lease_id, None)


def _tool_lane_name(tool_name: str) -> str:
    if tool_name in _CONTROL_LANE_TOOL_NAMES:
        return "control"
    if tool_name in _SEARCH_LANE_TOOL_NAMES:
        return "search"
    if tool_name in _GIT_READ_LANE_TOOL_NAMES:
        return "git_read"
    if tool_name in _CODE_NAVIGATION_LANE_TOOL_NAMES:
        return "code_navigation"
    if tool_name in _EXECUTION_LANE_TOOL_NAMES:
        return "execution"
    if tool_name in _NETWORK_LANE_TOOL_NAMES:
        return "network"
    if tool_name in _WORKSPACE_MUTATION_TOOL_NAMES:
        return "mutation"
    if tool_name in _FILE_READ_LANE_TOOL_NAMES:
        return "file_read"
    return "default"


_TOOL_PATH_ARGUMENTS = {
    "git_clone": ("destination",),
    "git_fetch": ("repo_path",),
    "git_remote": ("repo_path",),
    "git_add": ("repo_path",),
    "git_commit": ("repo_path",),
    "git_pull": ("repo_path",),
    "git_push": ("repo_path",),
    "git_branch": ("repo_path",),
    "git_switch": ("repo_path",),
    "git_restore": ("repo_path",),
    "run_powershell": ("cwd",),
    "run_cmd": ("cwd",),
    "run_python": ("cwd",),
    "run_node": ("cwd",),
    "run_npm": ("cwd",),
    "run_npx": ("cwd",),
    "start_process": ("cwd",),
    "start_job": ("cwd",),
}


def _structured_tool_error(
    category: str,
    detail: str,
    *,
    retryable: bool = False,
    details: dict[str, Any] | None = None,
) -> ToolError:
    payload: dict[str, Any] = {
        "ok": False,
        "error_type": category,
        "message": detail,
        "retryable": retryable,
    }
    if details:
        payload.update(details)
    return ToolError(json.dumps(payload, ensure_ascii=False))


def _decorate_runtime_recovery_task(task: dict[str, Any]) -> dict[str, Any]:
    decorated = dict(task)
    lease_runtime_instance = str(task.get("runtime_instance") or "")
    runtime_restarted = bool(lease_runtime_instance and lease_runtime_instance != MCP_INSTANCE_ID)
    decorated["lease_runtime_instance"] = lease_runtime_instance or None
    decorated["current_runtime_instance"] = MCP_INSTANCE_ID
    decorated["runtime_restarted"] = runtime_restarted
    if runtime_restarted:
        decorated["recovery_instruction"] = (
            str(task.get("recovery_instruction") or "")
            + " 当前 MCP instance 与 lease 创建时不同，表示该活动 lease 跨 Runtime 热切换继续存在；"
            "只要 hostname/workspace/owner 仍一致，这是受支持的恢复状态，不要因此重建任务或调查 Tunnel。"
        ).strip()
    return decorated


def _expected_tool_error_details(exc: Exception) -> tuple[str, str, bool, dict[str, Any]]:
    if isinstance(exc, RuntimePolicyError):
        details = dict(exc.details)
        recovery_task = details.get("recoverable_runtime_task")
        if isinstance(recovery_task, dict):
            details["recoverable_runtime_task"] = _decorate_runtime_recovery_task(recovery_task)
        return exc.error_type, str(exc), exc.retryable, details
    if isinstance(exc, psutil.NoSuchProcess):
        return "进程不存在", str(exc), False, {}
    if isinstance(exc, psutil.AccessDenied):
        return "访问被拒绝", str(exc), False, {}
    if isinstance(exc, FileNotFoundError):
        return "路径不存在", exc.filename or str(exc), False, {}
    if isinstance(exc, FileExistsError):
        return "目标已存在", exc.filename or str(exc), False, {}
    if isinstance(exc, NotADirectoryError):
        return "路径不是目录", exc.filename or str(exc), False, {}
    if isinstance(exc, IsADirectoryError):
        return "路径是目录，不是文件", exc.filename or str(exc), False, {}
    if isinstance(exc, PermissionError):
        return "访问被拒绝", exc.filename or str(exc), False, {}
    if isinstance(exc, ValueError):
        return "参数无效", str(exc) or exc.__class__.__name__, False, {}
    return "系统错误", str(exc) or exc.__class__.__name__, False, {}


def _lease_target_paths(function: Any, args: tuple[Any, ...], kwargs: dict[str, Any]) -> list[Path]:
    bound_kwargs = dict(kwargs)
    bound_kwargs.pop("workspace_lease_id", None)
    bound = inspect.signature(function).bind_partial(*args, **bound_kwargs)
    names = _TOOL_PATH_ARGUMENTS.get(
        function.__name__,
        ("path", "source", "destination", "repo_path"),
    )
    targets: list[Path] = []
    for name in names:
        value = bound.arguments.get(name)
        if name == "cwd" and value is None:
            value = str(DEFAULT_CWD)
        if isinstance(value, str):
            targets.append(_normalise_path(value))
    # Validate the path that will actually be changed, including rename targets
    # and the child selected when copy/move receives an existing directory.
    source = bound.arguments.get("source")
    destination = bound.arguments.get("destination")
    if function.__name__ in {"copy_path", "move_path"} and isinstance(source, str) and isinstance(destination, str):
        source_path = _normalise_path(source)
        destination_path = _normalise_path(destination)
        actual = destination_path / source_path.name if destination_path.is_dir() else destination_path
        targets.append(_normalise_path(str(actual)))
    elif function.__name__ == "rename_path":
        path = bound.arguments.get("path")
        new_name = bound.arguments.get("new_name")
        if isinstance(path, str) and isinstance(new_name, str) and Path(new_name).name == new_name:
            targets.append(_normalise_path(str(_normalise_path(path).parent / new_name)))
    return targets


_GMAIL_LEASE_FREE_ACTIONS = frozenset({
    "profile", "search", "get_message", "get_thread", "download_attachment",
    "list_drafts", "get_draft", "list_labels",
})


def _path_in_media(value: Any) -> bool:
    if not isinstance(value, str) or not value.strip():
        return False
    try:
        root = mode_runtime.MEDIA_ROOT.resolve(strict=False)
        target = _normalise_path(value)
        return target != root and target.is_relative_to(root)
    except (OSError, ValueError):
        return False


def _media_lease_exempt(function: Any, args: tuple[Any, ...], kwargs: dict[str, Any]) -> bool:
    name = function.__name__
    bound_kwargs = dict(kwargs)
    bound_kwargs.pop("workspace_lease_id", None)
    try:
        bound = inspect.signature(function).bind_partial(*args, **bound_kwargs).arguments
    except (TypeError, ValueError):
        bound = bound_kwargs
    if name in {"save_chatgpt_upload", "capture_screenshot"}:
        return True
    if name == "gmail":
        return str(bound.get("action") or "") in _GMAIL_LEASE_FREE_ACTIONS
    if name in {"create_file", "write_text_file", "append_text_file", "replace_text", "apply_patch", "make_directory", "delete_path"}:
        return _path_in_media(bound.get("path"))
    if name == "copy_path":
        return _path_in_media(bound.get("destination"))
    if name == "move_path":
        return _path_in_media(bound.get("source")) and _path_in_media(bound.get("destination"))
    if name == "rename_path":
        return _path_in_media(bound.get("path"))
    return False


def _require_tool_workspace_lease(function: Any, args: tuple[Any, ...], kwargs: dict[str, Any]) -> dict[str, Any] | None:
    if function.__name__ not in _LEASE_REQUIRED_TOOL_NAMES or _media_lease_exempt(function, args, kwargs):
        return None
    if function.__name__ == "chatgpt_agent":
        bound = _activity_bound_arguments(function, args, kwargs)
        if _agent_read_action(str(bound.get("action") or ""), bound):
            # The facade still requires a valid active caller identity. Read
            # actions do not acquire authority to mutate the browser/workspace.
            return None
    lease_id = kwargs.get("workspace_lease_id")
    targets = _lease_target_paths(function, args, kwargs)
    if function.__name__ == "stop_process":
        bound_kwargs = dict(kwargs)
        bound_kwargs.pop("workspace_lease_id", None)
        pid = int(
            inspect.signature(function)
            .bind_partial(*args, **bound_kwargs)
            .arguments.get("pid", 0)
            or 0
        )
        job = _job_for_pid(pid)
        if job is None:
            raise RuntimePolicyError(
                "非托管进程",
                "只能通过 cancel_job 停止已登记的后台任务；运行底座及普通进程受保护",
            )
        targets = [Path(str(job["workspace"]))]
    descendant_targets: list[Path] = []
    if function.__name__ in {"delete_path", "rename_path"}:
        descendant_targets = targets
    elif function.__name__ == "move_path" and targets:
        descendant_targets = [targets[0], targets[-1]]
    elif function.__name__ == "copy_path" and targets:
        descendant_targets = [targets[-1]]
    return _require_execute_lease(
        lease_id if isinstance(lease_id, str) else None,
        targets,
        descendant_targets=descendant_targets,
    )


_ACTIVITY_SENSITIVE_KEY = re.compile(
    r"(authorization|api[_-]?key|access[_-]?token|refresh[_-]?token|token|password|passwd|pwd|secret|cookie|credential|private[_-]?key)",
    re.I,
)
_ACTIVITY_SAFE_ARGUMENT_KEYS = frozenset({
    "path", "paths", "root", "repo_path", "source", "destination", "cwd", "new_name",
    "query", "pattern", "regex", "file_pattern", "case_sensitive", "context_lines",
    "start_line", "end_line", "lines", "recursive", "cached", "revision", "remote",
    "branch", "action", "name", "pid", "job_id", "mode", "owner_label", "shell",
    "command", "args", "code", "python_executable", "timeout_seconds", "max_output_chars",
    "algorithm", "phase", "app", "snapshot_id", "element", "button", "direction", "amount",
    "max_nodes", "max_depth", "refresh_after", "count",
})
_ACTIVITY_SAFE_RESULT_KEYS = frozenset({
    "status", "path", "source", "destination", "type", "exists", "count", "scanned_files",
    "truncated", "timed_out", "exit_code", "cwd", "execution_time", "stdout", "stderr",
    "command",
    "repository", "hash", "sha256", "bytes_written", "replacements", "additions", "encoding",
    "pid", "job_id", "lease_type", "mode", "workspace", "owner_label", "requested_lines",
    "returned_lines", "start_offset", "start_line", "end_line", "total_lines",
    "action", "backend", "elapsed_ms", "element_count", "scanned_nodes", "failed_at", "method", "best_effort",
})
_ACTIVITY_FILE_CHANGE_TOOLS = frozenset({
    "create_file", "write_text_file", "append_text_file", "replace_text", "apply_patch",
    "copy_path", "move_path", "rename_path", "delete_path",
})
_ACTIVITY_COMMAND_FILE_CHANGE_TOOLS = frozenset({
    "run_powershell", "run_cmd", "run_python", "run_node", "run_npm", "run_npx",
})
_ACTIVITY_COMMAND_SNAPSHOT_MAX_PATHS = 2_000
_ACTIVITY_TRUNCATION_WARNING_KEYS: set[tuple[str, str]] = set()
_ACTIVITY_TRUNCATION_WARNING_LOCK = threading.Lock()


def _activity_redact_text(value: str, *, limit: int = ACTIVITY_EVENT_MAX_TEXT_CHARS) -> str:
    text = value
    text = re.sub(r"ga_ticket_[A-Za-z0-9_-]+", "[REDACTED SPAWN TICKET]", text)
    text = re.sub(r"(?i)\bBearer\s+[^\s,;]+", "Bearer [REDACTED]", text)
    text = re.sub(r"(?i)(https?://)[^/\s:@]+:[^@\s/]+@", r"\1[REDACTED]@", text)
    text = re.sub(r"\b(?:sk-[A-Za-z0-9_-]{10,}|gh[pousr]_[A-Za-z0-9]{10,}|AKIA[A-Z0-9]{16})\b", "[REDACTED]", text)
    text = re.sub(
        r"(?i)((?:authorization|api[_-]?key|access[_-]?token|refresh[_-]?token|token|password|passwd|pwd|secret|cookie|credential|private[_-]?key)\s*[:=]\s*)(?:\"[^\"]*\"|'[^']*'|[^\s,;]+)",
        r"\1[REDACTED]",
        text,
    )
    text = re.sub(
        r"(?i)(--(?:api[-_]?key|token|password|passwd|secret|cookie|authorization)\s+)(?:\"[^\"]*\"|'[^']*'|\S+)",
        r"\1[REDACTED]",
        text,
    )
    if len(text) <= limit:
        return text
    return text[:limit] + f"\n… {len(text) - limit} chars omitted"


def _activity_redact_output(value: str, *, limit: int = ACTIVITY_EVENT_MAX_TEXT_CHARS) -> str:
    text = _activity_redact_text(value, limit=max(len(value) + 1, limit))
    text = re.sub(
        r"-----BEGIN [^-\r\n]*PRIVATE KEY-----.*?-----END [^-\r\n]*PRIVATE KEY-----",
        "[REDACTED PRIVATE KEY]",
        text,
        flags=re.I | re.S,
    )
    text = re.sub(
        r"\b[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}\b",
        "[REDACTED JWT]",
        text,
    )
    text = re.sub(
        r"(?<![A-Za-z0-9+/=_-])[A-Za-z0-9+/=_-]{80,}(?![A-Za-z0-9+/=_-])",
        "[REDACTED LONG TOKEN]",
        text,
    )
    if len(text) <= limit:
        return text
    return text[:limit] + f"\n… {len(text) - limit} chars omitted"


def _activity_redact_value(value: Any, *, limit: int = ACTIVITY_EVENT_MAX_TEXT_CHARS) -> Any:
    if isinstance(value, str):
        return _activity_redact_text(value, limit=limit)
    if isinstance(value, bool) or value is None or isinstance(value, (int, float)):
        return value
    if isinstance(value, list):
        return [_activity_redact_value(item, limit=limit) for item in value[:50]]
    if isinstance(value, dict):
        output: dict[str, Any] = {}
        for key, item in list(value.items())[:50]:
            output[str(key)] = "[REDACTED]" if _ACTIVITY_SENSITIVE_KEY.search(str(key)) else _activity_redact_value(item, limit=limit)
        return output
    return _activity_redact_text(str(value), limit=limit)


def _activity_bound_arguments(function: Any, args: tuple[Any, ...], kwargs: dict[str, Any]) -> dict[str, Any]:
    try:
        return dict(inspect.signature(function).bind_partial(*args, **kwargs).arguments)
    except (TypeError, ValueError):
        return dict(kwargs)


def _activity_safe_arguments(function: Any, args: tuple[Any, ...], kwargs: dict[str, Any]) -> dict[str, Any]:
    bound = _activity_bound_arguments(function, args, kwargs)
    return {
        key: ("[REDACTED]" if _ACTIVITY_SENSITIVE_KEY.search(key) else _activity_redact_value(value))
        for key, value in bound.items()
        if key != "workspace_lease_id" and key in _ACTIVITY_SAFE_ARGUMENT_KEYS
    }


def _activity_safe_result(result: Any) -> dict[str, Any]:
    if not isinstance(result, dict):
        return {"status": "completed"}
    safe: dict[str, Any] = {}
    for key, value in result.items():
        if key not in _ACTIVITY_SAFE_RESULT_KEYS:
            continue
        if _ACTIVITY_SENSITIVE_KEY.search(key):
            safe[key] = "[REDACTED]"
        elif key in {"stdout", "stderr"}:
            # Tool output is user-facing execution evidence in Agent Console.
            # Keep enough context for diagnostics while preserving the same
            # redaction pass and a hard event-size bound.
            safe[key] = _activity_redact_output(str(value), limit=8_000)
        else:
            safe[key] = _activity_redact_value(value)
    return safe


def _activity_task_from_lease(lease: dict[str, Any] | None) -> dict[str, str]:
    if not lease:
        return {}
    output: dict[str, str] = {}
    for source, target in (
        ("lease_id", "taskId"),
        ("thread_id", "threadId"),
        ("controller_id", "controllerId"),
        ("workspace", "workspace"),
        ("owner_label", "ownerLabel"),
    ):
        value = lease.get(source)
        if isinstance(value, str) and value:
            output[target] = _activity_redact_text(value, limit=500)
    return output


def _activity_find_lease(function: Any, args: tuple[Any, ...], kwargs: dict[str, Any]) -> dict[str, Any] | None:
    bound = _activity_bound_arguments(function, args, kwargs)
    lease_id = bound.get("workspace_lease_id")
    if isinstance(lease_id, str) and lease_id:
        matched = _active_lease_record(lease_id)
        # The release call itself may race with lease visibility in the runtime state.
        # The explicit lease id is still a safe, stable task correlation key; only a
        # successful release will emit task.completed later in the wrapper.
        return matched or {"lease_id": lease_id}
    try:
        leases = _workspace_status(include_private=True).get("leases", [])
    except Exception:
        return None
    candidates: list[Path] = []
    for key in ("path", "root", "repo_path", "source", "destination", "cwd", "workspace"):
        value = bound.get(key)
        if not isinstance(value, str) or not value:
            continue
        try:
            candidates.append(_normalise_path(value))
        except (OSError, ValueError):
            pass
    for lease in leases:
        scope_value = lease.get("scope_root") or lease.get("workspace")
        if not isinstance(scope_value, str):
            continue
        try:
            scope = Path(scope_value).resolve(strict=False)
            if any(path == scope or path.is_relative_to(scope) for path in candidates):
                return lease
        except (OSError, ValueError):
            continue
    return None


def _lifecycle_find_lease(function: Any, args: tuple[Any, ...], kwargs: dict[str, Any]) -> dict[str, Any] | None:
    """Resolve lifecycle ownership only from an explicit workspace lease id.

    Path correlation remains available to Activity Viewer, but it must not
    advance another task's checkpoint, consume control state, or inject that
    task's continuation.
    """
    bound = _activity_bound_arguments(function, args, kwargs)
    lease_id = bound.get("workspace_lease_id")
    if not isinstance(lease_id, str) or not lease_id:
        return None
    return _active_lease_record(lease_id)


def _activity_rotate_locked() -> None:
    if not ACTIVITY_EVENT_FILE.exists():
        return
    if ACTIVITY_EVENT_BACKUP_FILE.exists():
        stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
        archive = ACTIVITY_DATA_DIR / f"events.{stamp}.{uuid.uuid4().hex[:8]}.jsonl"
        os.replace(ACTIVITY_EVENT_BACKUP_FILE, archive)
    os.replace(ACTIVITY_EVENT_FILE, ACTIVITY_EVENT_BACKUP_FILE)
    histories = [path for path in ACTIVITY_DATA_DIR.glob("events*.jsonl") if path != ACTIVITY_EVENT_FILE]
    histories.sort(key=lambda path: path.stat().st_mtime, reverse=True)
    for stale in histories[ACTIVITY_EVENT_HISTORY_COUNT:]:
        stale.unlink(missing_ok=True)


def _activity_append(event: dict[str, Any]) -> None:
    try:
        line = json.dumps(event, ensure_ascii=False, separators=(",", ":")) + "\n"
        with _ACTIVITY_EVENT_LOCK:
            ACTIVITY_DATA_DIR.mkdir(parents=True, exist_ok=True)
            if ACTIVITY_EVENT_FILE.exists() and ACTIVITY_EVENT_FILE.stat().st_size >= ACTIVITY_EVENT_MAX_BYTES:
                _activity_rotate_locked()
            with ACTIVITY_EVENT_FILE.open("a", encoding="utf-8", newline="\n") as handle:
                handle.write(line)
    except OSError as exc:
        LOGGER.warning("activity_event_write_failed error=%s", exc)


def _jsonl_tail(path: Path, *, max_bytes: int = THREAD_CONTROL_READ_MAX_BYTES) -> list[dict[str, Any]]:
    if not path.exists() or not path.is_file():
        return []
    size = path.stat().st_size
    start = max(0, size - max(1, int(max_bytes)))
    with path.open("rb") as handle:
        handle.seek(start)
        data = handle.read()
    if start > 0:
        newline = data.find(b"\n")
        data = data[newline + 1 :] if newline >= 0 else b""
    rows: list[dict[str, Any]] = []
    # JSONL records are delimited by the literal LF byte only. Do not use
    # str.splitlines(): it also treats Unicode U+0085/U+2028/U+2029 inside a
    # perfectly valid JSON string as record boundaries and can silently drop
    # Console messages that contain pasted rich-text line separators.
    for raw_line in data.split(b"\n"):
        if not raw_line.strip():
            continue
        line = raw_line.decode("utf-8", errors="replace")
        try:
            value = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(value, dict):
            rows.append(value)
    return rows


def _jsonl_append(path: Path, row: dict[str, Any]) -> None:
    line = json.dumps(row, ensure_ascii=False, separators=(",", ":")) + "\n"
    with _ACTIVITY_EVENT_LOCK:
        path.parent.mkdir(parents=True, exist_ok=True)
        if path.exists() and path.stat().st_size >= THREAD_CONTROL_READ_MAX_BYTES:
            retained = _jsonl_tail(path, max_bytes=THREAD_CONTROL_READ_MAX_BYTES // 2)
            temporary = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
            try:
                with temporary.open("w", encoding="utf-8", newline="\n") as handle:
                    for item in retained:
                        handle.write(json.dumps(item, ensure_ascii=False, separators=(",", ":")) + "\n")
                    handle.write(line)
                os.replace(temporary, path)
            finally:
                temporary.unlink(missing_ok=True)
            return
        with path.open("a", encoding="utf-8", newline="\n") as handle:
            handle.write(line)


@contextmanager
def _ui_input_file_lock():
    """Cross-process lock compatible with the Agent Console desktop writer."""
    UI_INPUT_LOCK_FILE.parent.mkdir(parents=True, exist_ok=True)
    deadline = time.monotonic() + UI_INPUT_LOCK_WAIT_SECONDS
    descriptor: int | None = None
    while descriptor is None:
        try:
            descriptor = os.open(UI_INPUT_LOCK_FILE, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
            os.write(descriptor, f"pid={os.getpid()} at={round(time.time() * 1000)}\n".encode("utf-8"))
        except FileExistsError:
            try:
                if time.time() - UI_INPUT_LOCK_FILE.stat().st_mtime >= UI_INPUT_LOCK_STALE_SECONDS:
                    UI_INPUT_LOCK_FILE.unlink(missing_ok=True)
                    continue
            except OSError:
                pass
            if time.monotonic() >= deadline:
                raise RuntimePolicyError("Console 输入队列繁忙", "Agent Console 输入队列正在写入，请稍后重试", retryable=True)
            time.sleep(0.025)
    try:
        yield
    finally:
        try:
            os.close(descriptor)
        finally:
            UI_INPUT_LOCK_FILE.unlink(missing_ok=True)


def _compact_ui_input_locked() -> None:
    if not UI_INPUT_FILE.exists():
        return
    size = UI_INPUT_FILE.stat().st_size
    if size < UI_INPUT_MAX_BYTES:
        return
    start = max(0, size - UI_INPUT_RETAIN_BYTES)
    with UI_INPUT_FILE.open("rb") as handle:
        handle.seek(start)
        data = handle.read()
    if start > 0:
        newline = data.find(b"\n")
        data = data[newline + 1 :] if newline >= 0 else b""
    temporary = UI_INPUT_FILE.with_name(f".ui-inbox.{os.getpid()}.{uuid.uuid4().hex}.tmp")
    try:
        temporary.write_bytes(data)
        os.replace(temporary, UI_INPUT_FILE)
    finally:
        temporary.unlink(missing_ok=True)


def _append_ui_input_row(row: dict[str, Any]) -> None:
    UI_INPUT_FILE.parent.mkdir(parents=True, exist_ok=True)
    line = json.dumps(row, ensure_ascii=False, separators=(",", ":")) + "\n"
    with _ui_input_file_lock():
        _compact_ui_input_locked()
        with UI_INPUT_FILE.open("a", encoding="utf-8", newline="\n") as handle:
            handle.write(line)
            handle.flush()
            os.fsync(handle.fileno())


def _active_thread_lease(workspace_lease_id: str) -> dict[str, Any]:
    lease_id = workspace_lease_id.strip()
    if not lease_id:
        raise ValueError("workspace_lease_id 不能为空")
    return _require_execute_lease(lease_id, [])


def _active_console_relay_identity(workspace_lease_id: str) -> dict[str, Any]:
    lease_id = workspace_lease_id.strip()
    if not lease_id:
        raise ValueError("workspace_lease_id 不能为空")
    identity = next(
        (item for item in _workspace_status(include_private=True).get("leases", [])
         if str(item.get("lease_id") or "") == lease_id),
        None,
    )
    if identity is None:
        raise RuntimePolicyError("传话身份不存在", "远程监视/传话身份不存在、已过期或已释放；重新申请零执行权限的传话身份")
    if str(identity.get("lease_type") or "") != LEASE_TYPE_CONSOLE_RELAY or str(identity.get("mode") or "") != "read_shared":
        raise RuntimePolicyError("发送身份不允许", "跨对话 Console 传话只能使用零执行权限的远程监视/传话身份；普通项目、桌面操作和管理员租约都不能直接充当发送身份")
    return identity


def _thread_id_from_lease(lease: dict[str, Any]) -> str:
    return str(lease.get("thread_id") or lease.get("lease_id") or "")


def _rows_after_id(
    rows: list[dict[str, Any]],
    *,
    after_id: str | None,
    max_items: int,
) -> tuple[list[dict[str, Any]], bool, bool]:
    candidates = rows
    if after_id:
        index = next((index for index, row in enumerate(rows) if row.get("id") == after_id), None)
        if index is None:
            # A compacted-away cursor is ambiguous. Skipping retained rows is safer
            # than replaying an old corrective command as a fresh instruction.
            return [], True, False
        candidates = rows[index + 1 :]
    selected = candidates[:max_items]
    return selected, False, len(candidates) > len(selected)


def _targeted_ui_rows(lease: dict[str, Any]) -> list[dict[str, Any]]:
    lease_id = str(lease.get("lease_id") or "")
    thread_id = _thread_id_from_lease(lease)
    if not lease_id or not thread_id:
        return []
    return [
        row
        for row in _jsonl_tail(UI_INPUT_FILE)
        if row.get("type") == "ui.message"
        and str(row.get("scope") or "task") == "task"
        and str(row.get("taskId") or "") == lease_id
        and str(row.get("threadId") or "") == thread_id
    ]


def _ui_attachment_summaries(row: dict[str, Any]) -> list[dict[str, Any]]:
    raw = row.get("attachments")
    if not isinstance(raw, list):
        return []
    output: list[dict[str, Any]] = []
    for item in raw[:8]:
        if not isinstance(item, dict):
            continue
        attachment_id = str(item.get("id") or "").strip()
        name = str(item.get("name") or "附件").strip()[:240]
        mime = str(item.get("mime") or "application/octet-stream").strip()[:160]
        size = item.get("size")
        if not attachment_id:
            continue
        output.append({
            "id": attachment_id,
            "name": name,
            "mime": mime,
            "size": int(size) if isinstance(size, (int, float)) else None,
        })
    return output


def _find_ui_attachment_for_lease(lease: dict[str, Any], attachment_id: str) -> dict[str, Any] | None:
    target = attachment_id.strip()
    if not target:
        return None
    for row in reversed(_targeted_ui_rows(lease)):
        raw = row.get("attachments")
        if not isinstance(raw, list):
            continue
        for item in raw:
            if isinstance(item, dict) and str(item.get("id") or "") == target:
                return item
    return None


def _control_idle_state(lease: dict[str, Any], rows: list[dict[str, Any]]) -> dict[str, Any]:
    """Return how long the current Console control stream has been idle."""
    timestamps = [
        float(value)
        for row in rows
        if _ui_message_is_interrupting(row)
        if isinstance((value := row.get("timestampMs")), (int, float))
    ]
    last_control_at_ms: int | None = round(max(timestamps)) if timestamps else None
    if last_control_at_ms is None:
        for key in ("stay_started_at", "created_at"):
            raw = str(lease.get(key) or "").strip()
            if not raw:
                continue
            try:
                last_control_at_ms = round(datetime.fromisoformat(raw.replace("Z", "+00:00")).timestamp() * 1000)
                break
            except ValueError:
                continue
    now_ms = round(time.time() * 1000)
    idle_seconds = None if last_control_at_ms is None else max(0.0, (now_ms - last_control_at_ms) / 1000.0)
    return {
        "last_control_at_ms": last_control_at_ms,
        "control_idle_seconds": round(idle_seconds, 3) if idle_seconds is not None else None,
    }


def _pending_ui_messages_for_lease(
    lease: dict[str, Any],
    *,
    max_messages: int = 5,
) -> tuple[list[dict[str, Any]], str | None]:
    rows = _targeted_ui_rows(lease)
    cursor = lease.get("control_cursor_id")
    after_id = str(cursor) if isinstance(cursor, str) and cursor else None
    candidates = rows
    if after_id:
        index = next((index for index, row in enumerate(rows) if row.get("id") == after_id), None)
        if index is None:
            reset_to = str(rows[-1].get("id")) if rows and rows[-1].get("id") else None
            return [], reset_to
        candidates = rows[index + 1 :]
    blocking = [row for row in candidates if _ui_message_is_interrupting(row)]
    if not blocking:
        advance_to = str(candidates[-1].get("id")) if candidates and candidates[-1].get("id") else None
        return [], advance_to
    first_index = candidates.index(blocking[0])
    advance_to = None
    if first_index > 0 and candidates[first_index - 1].get("id"):
        advance_to = str(candidates[first_index - 1]["id"])
    return blocking[:max_messages], advance_to


_UI_EXPLICIT_SILENCE_RE = re.compile(
    r"(?:不用|无需|不要|别)(?:回复|回应|应答)(?:我|这条|此消息|本消息)?|"
    r"(?:保持|请保持)?静默|"
    r"\b(?:do not reply|don't reply|no need to reply|no reply needed|stay silent)\b",
    re.I,
)


_UI_EXPLICIT_KEEP_WORKING_RE = re.compile(
    r"(?:不要|别|不许|禁止|不能|不可).{0,6}(?:停止|停下|结束|终止|退出|释放)|"
    r"(?:继续|接着|恢复)(?:这个|当前|本次|本轮)?(?:任务|工作|执行|测试|验收|检查|修复|处理|做)|"
    r"^\s*(?:继续|接着|继续做|接着做)[！!。.]*\s*$",
    re.I,
)
_UI_EXPLICIT_TASK_END_RE = re.compile(
    r"(?:停止|结束|终止)(?:这个|当前|本次|本轮)?(?:任务|工作|执行|测试|验收|监控)|"
    r"(?:这个|当前|本次|本轮)?(?:任务|工作|执行|测试|验收|监控)(?:停止|结束|终止)|"
    r"(?:不用|不需要|无需|不要|别)继续(?:这个|当前|本次|本轮)?(?:任务|工作|执行|测试|验收|监控)|"
    r"(?:释放|放掉)(?:这个|当前|本次|本轮)?(?:租约|lease)|"
    r"(?:可以|就|直接)(?:停|结束|收工)(?:了|吧|即可)?[！!。.]*\s*$",
    re.I,
)


def _ui_message_has_payload(message: dict[str, Any]) -> bool:
    return bool(str(message.get("text") or "").strip() or message.get("attachments"))


def _ui_message_is_interrupting(message: dict[str, Any]) -> bool:
    """Only an explicit informational tag may bypass ordinary user-control gates."""
    behavior = message.get("controlBehavior", message.get("control_behavior", "interrupt"))
    return str(behavior or "interrupt").strip().lower() != "informational"


def _ui_message_requires_visible_reply(message: dict[str, Any]) -> bool:
    """Default to acknowledging Console input, including attachment-only messages."""
    if not _ui_message_is_interrupting(message):
        return False
    clean = str(message.get("text") or "").strip()
    if not _ui_message_has_payload(message):
        return False
    return not clean or _UI_EXPLICIT_SILENCE_RE.search(clean) is None


def _ui_message_requires_task_resume(message: dict[str, Any]) -> bool:
    """Treat ordinary Console input as an interruption, not permission to end the task."""
    if not _ui_message_is_interrupting(message):
        return False
    if not _ui_message_has_payload(message):
        return False
    clean = str(message.get("text") or "").strip()
    if not clean:
        return True
    if _UI_EXPLICIT_KEEP_WORKING_RE.search(clean):
        return True
    stay_command = _parse_stay_command(clean)
    if stay_command is not None and stay_command.get("action") in {"enter", "exit"}:
        return False
    return _UI_EXPLICIT_TASK_END_RE.search(clean) is None


_STAY_ENTER_RE = re.compile(
    r"^/stay\s+(?P<value>\d+(?:\.\d+)?)\s*(?P<unit>h|hr|hrs|hour|hours|小时|时|m|min|mins|minute|minutes|分钟|分|s|sec|secs|second|seconds|秒)$",
    re.I,
)
_STAY_EXIT_RE = re.compile(
    r"^(?:/stay\s+off|(?:(?:请|现在|立即|马上|我(?:现在)?(?:明确|强烈)?要求)\s*)?退出驻场模式[！!。.]*$)",
    re.I,
)
_STAY_LEASE_FIELDS = (
    "stay_started_at",
    "stay_until_at",
    "stay_duration_seconds",
    "stay_command_id",
    "visual_mode",
    "renewed_at",
    "expires_at",
)


def _parse_stay_command(text: str) -> dict[str, Any] | None:
    return _shared_parse_stay_command(text)


def _stay_mode_state(lease: dict[str, Any]) -> dict[str, Any]:
    raw_until = str(lease.get("stay_until_at") or "").strip()
    active = False
    remaining_seconds = 0
    if raw_until:
        try:
            until = datetime.fromisoformat(raw_until.replace("Z", "+00:00"))
            remaining_seconds = max(0, int((until - datetime.now(timezone.utc)).total_seconds()))
            active = until > datetime.now(timezone.utc)
        except ValueError:
            raw_until = ""
    return {
        "active": active,
        "until_at": raw_until or None,
        "remaining_seconds": remaining_seconds,
        "duration_seconds": lease.get("stay_duration_seconds") if active else None,
    }


def _sync_stay_lease_fields(target: dict[str, Any], source: dict[str, Any]) -> None:
    for key in _STAY_LEASE_FIELDS:
        target.pop(key, None)
        if key in source:
            target[key] = source[key]


def _apply_stay_commands(lease: dict[str, Any], messages: list[dict[str, Any]]) -> dict[str, Any]:
    lease_id = str(lease.get("lease_id") or "")
    last_command: dict[str, Any] | None = None
    for message in messages:
        command = _parse_stay_command(str(message.get("text") or ""))
        if command is None:
            continue
        if command["action"] == "invalid":
            result = {"status": "invalid", **_stay_mode_state(lease), "error": command["error"]}
        else:
            updated = mode_runtime.apply_console_stay_command(lease_id, str(message.get("id") or ""))
            _sync_stay_lease_fields(lease, updated)
            result = {"status": "exited" if command["action"] == "exit" else "entered", **_stay_mode_state(lease)}
        message["stay_command"] = result
        last_command = result
    return last_command or _stay_mode_state(lease)


def _control_gate_state(lease: dict[str, Any]) -> dict[str, Any]:
    ack_required = bool(lease.get("control_ack_pending_id"))
    resume_required = bool(lease.get("control_resume_pending_id"))
    result: dict[str, Any] = {
        "control_ack_required": ack_required,
        "control_resume_required": resume_required,
    }
    if resume_required:
        result["continuation_instruction"] = (
            "Agent Console 中断已读取，但当前任务尚未完成一次中断后的续作。"
            "先回应用户（如仍待回应），然后继续原任务并成功执行至少一个同租约的后续实质工具/验证动作；"
            "在此之前不要 phase=done、release_workspace 或发送 ChatGPT 最终答复。"
        )
    return result


def _mode_activation_guidance(record: dict[str, Any]) -> str:
    """Give the model a short decision aid, without auto-enabling any mode."""
    goal = mode_runtime.goal_mode_state(record, include_details=False)
    visual = mode_runtime.visual_mode_state(record)
    ultra = mode_runtime.ultra_mode_state(record)
    hints: list[str] = []
    if not goal.get("active") and goal.get("user_mode") != "disabled":
        hints.append(
            "多阶段修改、反复验证或部分完成不代表总目标完成时，先调用 "
            "set_visual_mode(action='update', goal_enabled=true, goal_outcome=最终结果, "
            "goal_success_criteria=少量可验证条件)；只开目标不需要看图。"
        )
    if not visual.get("active") and (record.get("visual_mode") or {}).get("user_mode") != "disabled":
        hints.append(
            "参考图、界面、文档排版或真实外观影响判断时，主动开启同一任务的 Visual，"
            "复用参考资料并真实读取图像；网页按响应式检查，交互任务增加 journeys。"
        )
    if not ultra.get("active") and ultra.get("user_mode") != "disabled":
        hints.append(
            "存在可独立拆分子任务时，可开启 Ultra，再按 docs/GPTAGENT_ULTRA_COLLABORATION_2026-10-04.md 使用 GPTAgent Project 子对话、participant、任务表和显式 followup；"
            "简单任务保持单对话，后台不自动派生。"
        )
    if not hints:
        return ""
    return (
        "开始实质工作前按任务语义判断，不等用户提示模式名称，也不按标题关键词强制开启。"
        + "".join(hints)
        + "已有模式与参考资料直接复用；简单读取、复制、单次截图走最短路径。用户关闭的模式不得重开。"
    )


def _runtime_task_continuation(lease_id: str) -> dict[str, Any]:
    """Repeat active-task lifecycle instructions on successful tool results.

    MCP cannot force the ChatGPT platform to emit another model turn after it has
    stopped generating.  Repeating this state on every same-lease result makes the
    active lease and completion handshake visible at each tool boundary instead of
    relying only on the initial prompt/AGENTS instructions.
    """
    current = _active_lease_record(lease_id)
    if current is None or str(current.get("mode") or "") != "execute_exclusive":
        return {}
    participant = str(current.get("lease_role") or "") == "participant"
    shared_root_lease_id = str(current.get("shared_root_lease_id") or "")
    shared_parent_lease_id = str(current.get("shared_parent_lease_id") or "")
    gates = _control_gate_state(current)
    checkpoint = mode_runtime._execution_checkpoint(current)
    lease_runtime_instance = str(current.get("runtime_instance") or "")
    runtime_restarted = bool(lease_runtime_instance and lease_runtime_instance != MCP_INSTANCE_ID)
    active_jobs: list[dict[str, Any]] = []
    workspace_text = str(current.get("workspace") or "").strip()
    if workspace_text:
        try:
            job_rows = _list_active_jobs(Path(workspace_text)).get("jobs", [])
            active_jobs = [
                {
                    "job_id": item.get("job_id"),
                    "name": item.get("name"),
                    "pid": item.get("pid"),
                    "status": item.get("status"),
                }
                for item in job_rows
                if str(item.get("lease_id") or "") == lease_id and item.get("status") == "running"
            ][:8]
        except (OSError, ValueError, RuntimePolicyError, psutil.Error):
            active_jobs = []
    instruction = (
        "任务仍活跃：继续工作或验证；仅 report_progress(phase='done') 返回 lease_released=true 后结束，未释放前不要发送 ChatGPT 最终答复。"
        "确定失败无新证据不重复探测；异步产物跟踪真实Job并有界等待。"
    )
    if runtime_restarted:
        instruction = (
            "租约已跨服务热切换保留；主机/目录/负责人一致就续作，不重新申请或误判丢失。"
            + instruction
        )
    if checkpoint.get("has_execution_history") and not participant:
        instruction = (
            "已有执行历史，不得声称未工作；遗忘时按checkpoint、Git、Job与真实文件恢复，禁止重置重来。"
            "若 runtime_task.recovery_action.action=RECOVER_EXISTING_TASK，返回的 lease_id/thread_id 就是当前对话的权威恢复身份；"
            "看到 Child participant、兄弟租约或其他活跃任务都不得据此声称自己的 Root lease 被抢走/不存在，也不得重新 acquire。"
            + instruction
        )
    elif participant:
        instruction = (
            "当前身份是 GPTAgent participant，不是 Root owner。只继续当前 participant lease/thread；"
            "shared_root_lease_id 仅表示共享资源锁，Root 所有权始终属于父级。"
            "不得 acquire 新租约、不得把 Root 的恢复候选当成自己的任务、不得尝试恢复/释放/接管 Root lease。"
            "需要协作时使用当前 participant 的 team_inbox/team_message/task/followup 协议。"
            + instruction
        )
    goal = mode_runtime.goal_mode_state(current, include_details=False)
    ultra = mode_runtime.ultra_mode_state(current)
    liveness_probe_ack = _deliver_pending_thread_liveness(current)
    if liveness_probe_ack:
        instruction = str(liveness_probe_ack.get("text") or "") + instruction
    if goal.get("active"):
        instruction = (
            "目标开启：按goal_mode的目标、持续requirements与最新revision修正继续，部分成功不算完成；按reuse_visual_context复用视觉资料。"
            + instruction
        )
        if int(checkpoint.get("successful_action_count") or 0) >= 2:
            instruction = (
                "并发优先：每轮先找当前所有互不依赖的工作前沿；若已有多个独立读取/搜索/状态/Git只读/导航/验证请求，"
                "同一轮批量发出而不是逐个等待。只有数据依赖、同资源写冲突、资源锁或控制门禁才串行。"
                + instruction
            )
    if ultra.get("active"):
        instruction = (
            "Ultra 已允许显式 GPTAgent 子对话协作。先读 docs/GPTAGENT_ULTRA_COLLABORATION_2026-10-04.md，"
            "按需要建立 1–3 个子对话，优先复用已有对话，保存阶段交接和产物；后台不自动派生或唤醒。"
            "开关不代表已经并行，实际进展以页面和产物为准。" + instruction
        )
    elif ultra.get("user_mode") == "disabled":
        instruction = "用户已关闭新增并行子对话的许可，不得自行反开。" + instruction
    if active_jobs:
        instruction = "当前任务仍有 Runtime Job 在运行；不得声称没有启动进程或后台任务。" + instruction
    if str(current.get("lease_type") or "") == "workspace":
        instruction = (
            "普通项目错误留在项目内：大仓先导航，搜索限定目录；无底座异常不转去维护，也不自行启动Codex、Claude或其他模型接管。"
            + instruction
        )
    if gates.get("control_ack_required"):
        instruction = "当前有已读取但尚未公开回应的 Console 消息；先回应用户，再继续原任务。" + instruction
    elif gates.get("control_resume_required"):
        instruction = str(gates.get("continuation_instruction") or "") + instruction
    return {
        "runtime_task": {
            "lease_active": True,
            "lease_id": lease_id,
            "thread_id": current.get("thread_id"),
            "workspace": current.get("workspace"),
            "lease_type": current.get("lease_type"),
            "mode": current.get("mode"),
            "owner_label": current.get("owner_label"),
            "lease_role": current.get("lease_role", "owner"),
            "shared_root_lease_id": shared_root_lease_id or None,
            "shared_parent_lease_id": shared_parent_lease_id or None,
            "resource_lease_owner": not participant,
            "lease_runtime_instance": lease_runtime_instance or None,
            "current_runtime_instance": MCP_INSTANCE_ID,
            "runtime_restarted": runtime_restarted,
            "completion_handshake_required": True,
            "control_ack_required": bool(gates.get("control_ack_required")),
            "control_resume_required": bool(gates.get("control_resume_required")),
            "execution_checkpoint": checkpoint,
            "stay_mode": _stay_mode_state(current),
            "visual_mode": mode_runtime.visual_mode_state(current, include_details=False),
            "goal_mode": goal,
            "ultra_mode": ultra,
            "liveness_probe": liveness_probe_ack,
            "active_jobs": active_jobs,
            "recovery_action": {
                "action": "CONTINUE_PARTICIPANT" if participant else "RECOVER_EXISTING_TASK",
                "lease_id": lease_id,
                "workspace": current.get("workspace"),
                "thread_id": current.get("thread_id"),
                "owner": current.get("owner_label"),
                "lease_role": current.get("lease_role", "owner"),
                "shared_root_lease_id": shared_root_lease_id or None,
                "root_resource_still_owned_by_root": True if participant else None,
                "stay": _stay_mode_state(current),
                "visual": mode_runtime.visual_mode_state(current, include_details=False),
                "goal": goal,
                "ultra": ultra,
                "successful_action_count": checkpoint.get("successful_action_count", 0),
                "side_effect_action_count": checkpoint.get("side_effect_action_count", 0),
                "active_jobs": active_jobs,
                "last_progress": {
                    "phase": checkpoint.get("last_progress_phase"),
                    "text": checkpoint.get("last_progress_text"),
                    "at": checkpoint.get("last_progress_at"),
                },
                "directives": [
                    "DO_NOT_RECOVER_ROOT" if participant else "DO_NOT_RESTART",
                    "DO_NOT_REACQUIRE",
                    *(["ROOT_OWNERSHIP_UNCHANGED"] if participant else []),
                    *(["DO_NOT_CLAIM_NO_WORK"] if checkpoint.get("has_execution_history") else []),
                ],
            },
            "instruction": instruction,
        }
    }


def _mark_consumed_ui_messages_for_control_state(
    lease: dict[str, Any], messages: list[dict[str, Any]]
) -> None:
    """Persist reply and post-interruption resumption requirements for consumed Console input."""
    lease_id = str(lease.get("lease_id") or "")
    if not lease_id or not messages:
        return

    payload_messages = [
        message for message in messages
        if message.get("id") and _ui_message_has_payload(message) and _ui_message_is_interrupting(message)
    ]
    if not payload_messages:
        return

    reply_required = [
        message for message in payload_messages if _ui_message_requires_visible_reply(message)
    ]
    if reply_required:
        pending_id = str(reply_required[-1]["id"])
        _update_workspace_control_ack_pending(lease_id, pending_id)
        lease["control_ack_pending_id"] = pending_id
    else:
        _update_workspace_control_ack_pending(lease_id, None)
        lease.pop("control_ack_pending_id", None)

    latest = payload_messages[-1]
    if _ui_message_requires_task_resume(latest):
        resume_id = str(latest["id"])
        _update_workspace_control_resume_pending(lease_id, resume_id)
        lease["control_resume_pending_id"] = resume_id
    else:
        _update_workspace_control_resume_pending(lease_id, None)
        lease.pop("control_resume_pending_id", None)


def _consume_ui_messages_via_progress(
    lease: dict[str, Any],
    *,
    max_messages: int = 20,
) -> dict[str, Any]:
    """Compatibility bridge for chats whose cached tool schema lacks poll_ui_messages."""
    lease_id = str(lease.get("lease_id") or "")
    rows = _targeted_ui_rows(lease)
    cursor = lease.get("control_cursor_id")
    after_id = str(cursor) if isinstance(cursor, str) and cursor else None
    selected, cursor_reset, has_more = _rows_after_id(
        rows,
        after_id=after_id,
        max_items=max_messages,
    )
    messages = [
        {
            "id": str(row.get("id") or ""),
            "timestamp_ms": row.get("timestampMs"),
            "source_task_id": row.get("taskId"),
            "text": str(row.get("text") or ""),
            "attachments": _ui_attachment_summaries(row),
            "control_behavior": "interrupt" if _ui_message_is_interrupting(row) else "informational",
        }
        for row in selected
        if row.get("id") and isinstance(row.get("text"), str)
    ]
    stay_mode = _apply_stay_commands(lease, messages)
    idle_state = _control_idle_state(lease, rows)
    if cursor_reset:
        next_after_id = str(rows[-1].get("id")) if rows and rows[-1].get("id") else after_id
    else:
        next_after_id = messages[-1]["id"] if messages else after_id
    if lease_id and next_after_id and next_after_id != cursor:
        _update_workspace_control_cursor(lease_id, str(next_after_id))
        lease["control_cursor_id"] = str(next_after_id)
    _mark_consumed_ui_messages_for_control_state(lease, messages)
    return {
        "control_messages": messages,
        "control_count": len(messages),
        "control_interrupting_count": sum(1 for message in messages if _ui_message_is_interrupting(message)),
        "control_next_after_id": next_after_id,
        "control_cursor_reset": cursor_reset,
        "control_has_more": has_more,
        "stay_mode": stay_mode,
        **_control_gate_state(lease),
        **idle_state,
    }


def _activity_emit(event_type: str, *, call_id: str | None = None, tool: str | None = None, lease: dict[str, Any] | None = None, **payload: Any) -> None:
    event: dict[str, Any] = {
        "id": uuid.uuid4().hex,
        "timestamp": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
        "type": event_type,
    }
    if call_id:
        event["callId"] = call_id
    if tool:
        event["tool"] = tool
    event.update(_activity_task_from_lease(lease))
    event.update(payload)
    _activity_append(event)


def _activity_file_snapshot(path: Path | None) -> dict[str, Any]:
    if path is None or not path.exists() or not path.is_file():
        return {"path": str(path) if path else None, "hash": None, "size": None, "text": None}
    try:
        size = path.stat().st_size
        digest = _sha256_path(path)
        text: str | None = None
        if size <= ACTIVITY_DIFF_SOURCE_MAX_BYTES:
            raw = path.read_bytes()
            if b"\x00" not in raw:
                try:
                    text = raw.decode("utf-8")
                except UnicodeDecodeError:
                    pass
        return {"path": str(path), "hash": digest, "size": size, "text": text}
    except OSError:
        return {"path": str(path), "hash": None, "size": None, "text": None}


def _activity_file_context(function: Any, args: tuple[Any, ...], kwargs: dict[str, Any]) -> dict[str, Any] | None:
    if function.__name__ not in _ACTIVITY_FILE_CHANGE_TOOLS:
        return None
    bound = _activity_bound_arguments(function, args, kwargs)
    raw_source = bound.get("path") or bound.get("source")
    source: Path | None = None
    if isinstance(raw_source, str):
        try:
            source = _normalise_path(raw_source)
        except (OSError, ValueError):
            pass
    return {"before": _activity_file_snapshot(source), "bound": bound}


def _activity_unified_diff(path: str, before: str | None, after: str | None) -> str:
    if before is None and after is None:
        return ""
    diff = "".join(difflib.unified_diff(
        (before or "").splitlines(keepends=True),
        (after or "").splitlines(keepends=True),
        fromfile=path,
        tofile=path,
    ))
    return _activity_redact_output(diff, limit=ACTIVITY_EVENT_MAX_DIFF_CHARS)


def _activity_emit_file_change(function: Any, context: dict[str, Any] | None, result: Any, *, call_id: str, lease: dict[str, Any] | None) -> None:
    if context is None or not isinstance(result, dict) or result.get("status") in {"no_change", "exists", "conflict"}:
        return
    operation = {
        "create_file": "create", "write_text_file": "write", "append_text_file": "append",
        "replace_text": "replace", "apply_patch": "patch", "copy_path": "copy",
        "move_path": "move", "rename_path": "rename", "delete_path": "delete",
    }.get(function.__name__, function.__name__)
    target_value = result.get("destination") or result.get("path") or context["before"].get("path")
    target = Path(target_value) if isinstance(target_value, str) else None
    after = _activity_file_snapshot(target)
    before = context["before"]
    diff = ""
    if function.__name__ == "apply_patch":
        patch = context["bound"].get("patch")
        if isinstance(patch, str):
            diff = _activity_redact_output(patch, limit=ACTIVITY_EVENT_MAX_DIFF_CHARS)
    elif function.__name__ not in {"move_path", "rename_path", "copy_path"}:
        diff = _activity_unified_diff(str(target_value or ""), before.get("text"), after.get("text"))
    payload: dict[str, Any] = {
        "path": _activity_redact_text(str(target_value or ""), limit=1_000),
        "operation": operation,
        "beforeHash": before.get("hash"),
        "afterHash": after.get("hash"),
        "beforeSize": before.get("size"),
        "afterSize": after.get("size"),
    }
    bytes_written = result.get("bytes_written")
    if isinstance(bytes_written, int) and not isinstance(bytes_written, bool):
        payload["bytesWritten"] = bytes_written
    source_path = before.get("path")
    if source_path and source_path != target_value:
        payload["sourcePath"] = _activity_redact_text(str(source_path), limit=1_000)
    if diff:
        payload["diff"] = diff
    _activity_emit("file.changed", call_id=call_id, tool=function.__name__, lease=lease, **payload)


def _activity_git_run(root: Path, args: list[str], *, timeout: float = 3.0) -> subprocess.CompletedProcess[bytes] | None:
    """Small read-only Git probe used only for activity attribution."""
    try:
        return subprocess.run(
            ["git", "-C", str(root), *args],
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            timeout=timeout,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return None


def _activity_git_root(cwd: Path) -> Path | None:
    probe = _activity_git_run(cwd, ["rev-parse", "--show-toplevel"])
    if probe is None or probe.returncode != 0:
        return None
    raw = probe.stdout.decode("utf-8", errors="replace").strip()
    if not raw:
        return None
    try:
        return Path(raw).resolve()
    except OSError:
        return None


def _activity_git_head(root: Path) -> str | None:
    probe = _activity_git_run(root, ["rev-parse", "--verify", "HEAD"])
    if probe is None or probe.returncode != 0:
        return None
    value = probe.stdout.decode("ascii", errors="ignore").strip()
    return value or None


def _activity_git_status_paths(root: Path) -> tuple[set[str], bool]:
    probe = _activity_git_run(root, ["status", "--porcelain=v1", "-z", "--untracked-files=all"])
    if probe is None or probe.returncode != 0:
        return set(), False
    entries = probe.stdout.decode("utf-8", errors="replace").split("\0")
    paths: set[str] = set()
    truncated = False
    index = 0
    while index < len(entries):
        entry = entries[index]
        index += 1
        if not entry or len(entry) < 4:
            continue
        status = entry[:2]
        path = entry[3:]
        if path:
            paths.add(path)
        if any(code in {"R", "C"} for code in status) and index < len(entries):
            source = entries[index]
            index += 1
            if source:
                paths.add(source)
        if len(paths) >= _ACTIVITY_COMMAND_SNAPSHOT_MAX_PATHS:
            truncated = True
            break
    return paths, truncated


def _activity_git_changed_between(root: Path, before_head: str | None, after_head: str | None) -> set[str]:
    if not before_head or not after_head or before_head == after_head:
        return set()
    probe = _activity_git_run(root, ["diff", "--name-only", "-z", before_head, after_head])
    if probe is None or probe.returncode != 0:
        return set()
    return {
        path
        for path in probe.stdout.decode("utf-8", errors="replace").split("\0")
        if path
    }


def _activity_git_blob_snapshot(root: Path, head: str | None, relative_path: str) -> dict[str, Any]:
    absolute = root / Path(relative_path)
    if not head:
        return {"path": str(absolute), "hash": None, "size": None, "text": None}
    spec = f"{head}:{relative_path.replace(os.sep, '/')}"
    probe = _activity_git_run(root, ["show", spec])
    if probe is None or probe.returncode != 0:
        return {"path": str(absolute), "hash": None, "size": None, "text": None}
    raw = probe.stdout
    text: str | None = None
    if len(raw) <= ACTIVITY_DIFF_SOURCE_MAX_BYTES and b"\x00" not in raw:
        try:
            text = raw.decode("utf-8")
        except UnicodeDecodeError:
            pass
    return {"path": str(absolute), "hash": hashlib.sha256(raw).hexdigest(), "size": len(raw), "text": text}


def _activity_filesystem_snapshot(root: Path) -> tuple[dict[str, dict[str, Any]], bool]:
    """Bounded non-Git fallback for command file-change attribution."""
    snapshots: dict[str, dict[str, Any]] = {}
    truncated = False
    try:
        for current, directories, files in os.walk(root, topdown=True, followlinks=False, onerror=lambda exc: None):
            directories[:] = sorted(
                directory
                for directory in directories
                if directory.casefold() not in DEFAULT_IGNORED_DIRECTORY_NAMES
            )
            for filename in sorted(files):
                path = Path(current) / filename
                try:
                    if path.is_symlink():
                        continue
                    relative = path.relative_to(root).as_posix()
                except (OSError, ValueError):
                    continue
                if len(snapshots) >= _ACTIVITY_COMMAND_SNAPSHOT_MAX_PATHS:
                    truncated = True
                    return snapshots, truncated
                snapshots[relative] = _activity_file_snapshot(path)
    except OSError:
        pass
    return snapshots, truncated


def _activity_command_file_context(
    function: Any,
    args: tuple[Any, ...],
    kwargs: dict[str, Any],
    lease: dict[str, Any] | None,
) -> dict[str, Any] | None:
    if function.__name__ not in _ACTIVITY_COMMAND_FILE_CHANGE_TOOLS or not lease:
        return None
    bound = _activity_bound_arguments(function, args, kwargs)
    raw_cwd = bound.get("cwd") or lease.get("workspace")
    if not isinstance(raw_cwd, str) or not raw_cwd.strip():
        return None
    try:
        cwd = _normalise_path(raw_cwd, must_exist=True)
    except (OSError, ValueError):
        return None
    if not cwd.is_dir():
        cwd = cwd.parent
    root = _activity_git_root(cwd)
    if root is not None:
        dirty_paths, truncated = _activity_git_status_paths(root)
        snapshots = {
            path: _activity_file_snapshot(root / Path(path))
            for path in sorted(dirty_paths)[:_ACTIVITY_COMMAND_SNAPSHOT_MAX_PATHS]
        }
        return {
            "root": root,
            "detector": "git-worktree-delta",
            "head": _activity_git_head(root),
            "dirty": snapshots,
            "truncated": truncated,
        }
    root = cwd.resolve(strict=False)
    snapshots, truncated = _activity_filesystem_snapshot(root)
    return {
        "root": root,
        "detector": "filesystem-delta",
        "files": snapshots,
        "truncated": truncated,
    }


def _activity_emit_command_file_changes(
    function: Any,
    context: dict[str, Any] | None,
    *,
    call_id: str,
    lease: dict[str, Any] | None,
) -> None:
    if context is None:
        return
    root = context.get("root")
    if not isinstance(root, Path):
        return
    detector = str(context.get("detector") or "git-worktree-delta")
    before_truncated = bool(context.get("truncated"))
    if detector == "filesystem-delta":
        before_dirty = context.get("files") if isinstance(context.get("files"), dict) else {}
        after_snapshots, truncated = _activity_filesystem_snapshot(root)
        candidates = set(str(path) for path in before_dirty)
        candidates.update(after_snapshots)
    else:
        after_paths, truncated = _activity_git_status_paths(root)
        after_head = _activity_git_head(root)
        before_dirty = context.get("dirty") if isinstance(context.get("dirty"), dict) else {}
        after_snapshots = {}
        candidates = set(str(path) for path in before_dirty)
        candidates.update(after_paths)
        candidates.update(_activity_git_changed_between(root, context.get("head"), after_head))
    ordered = sorted(candidates)
    if len(ordered) > _ACTIVITY_COMMAND_SNAPSHOT_MAX_PATHS:
        ordered = ordered[:_ACTIVITY_COMMAND_SNAPSHOT_MAX_PATHS]
        truncated = True
    for relative_path in ordered:
        before = before_dirty.get(relative_path)
        if not isinstance(before, dict):
            if detector == "filesystem-delta":
                if before_truncated:
                    continue
                before = {"path": str(root / Path(relative_path)), "hash": None, "size": None, "text": None}
            else:
                before = _activity_git_blob_snapshot(root, context.get("head"), relative_path)
        after = after_snapshots.get(relative_path) if detector == "filesystem-delta" else None
        if not isinstance(after, dict):
            after = _activity_file_snapshot(root / Path(relative_path))
        if before.get("hash") == after.get("hash"):
            continue
        before_hash = before.get("hash")
        after_hash = after.get("hash")
        operation = (
            "create" if before_hash is None and after_hash is not None
            else "delete" if before_hash is not None and after_hash is None
            else "write"
        )
        diff = _activity_unified_diff(
            str(root / Path(relative_path)), before.get("text"), after.get("text")
        )
        payload: dict[str, Any] = {
            "path": _activity_redact_text(str(root / Path(relative_path)), limit=1_000),
            "operation": operation,
            "beforeHash": before_hash,
            "afterHash": after_hash,
            "beforeSize": before.get("size"),
            "afterSize": after.get("size"),
            "detectedBy": detector,
        }
        if diff:
            payload["diff"] = diff
        _activity_emit("file.changed", call_id=call_id, tool=function.__name__, lease=lease, **payload)
    truncated_now = bool(context.get("truncated") or truncated)
    warning_key = (str((lease or {}).get("lease_id") or ""), str(root))
    emit_truncation_warning = False
    if lease:
        with _ACTIVITY_TRUNCATION_WARNING_LOCK:
            if truncated_now:
                if warning_key not in _ACTIVITY_TRUNCATION_WARNING_KEYS:
                    _ACTIVITY_TRUNCATION_WARNING_KEYS.add(warning_key)
                    emit_truncation_warning = True
            else:
                _ACTIVITY_TRUNCATION_WARNING_KEYS.discard(warning_key)
    if emit_truncation_warning:
        _activity_emit(
            "agent.progress",
            call_id=call_id,
            lease=lease,
            phase="warning",
            text=f"文件变更观测超过 {_ACTIVITY_COMMAND_SNAPSHOT_MAX_PATHS} 项，活动记录已截断；请结合 Git 状态复核。",
        )


def _check_tool_control(
    tool_name: str,
    lifecycle_lease: dict[str, Any] | None,
    *,
    is_progress: bool = False,
    is_completion: bool = False,
) -> None:
    """Check current user control at a safe boundary; never interrupt a running body."""
    if lifecycle_lease is not None and not is_progress and tool_name != "poll_ui_messages":
        pending_liveness = _thread_pending_liveness_probe(lifecycle_lease)
        if pending_liveness is not None:
            liveness = _deliver_pending_thread_liveness(lifecycle_lease)
            raise RuntimePolicyError(
                "存活确认待明确回复",
                str((liveness or {}).get("text") or "Runtime 正在等待显式 GPT 存活回复；优先调用 report_progress。")
                + (
                    f" 若当前 Chat 冻结工具快照里没有 report_progress，先公开说明仍在工作，再调用 "
                    f'poll_ui_messages(after_id="alive:{str((liveness or {}).get("id") or "")}")。'
                ),
                retryable=True,
                error_type="LIVENESS_REPLY_REQUIRED",
                details={"liveness_probe": liveness or {}},
            )
    if lifecycle_lease is not None and (
        tool_name in _CONTROL_GATED_TOOL_NAMES
        or is_completion
    ):
        pending_messages, reset_to = _pending_ui_messages_for_lease(lifecycle_lease)
        if reset_to:
            _update_workspace_control_cursor(str(lifecycle_lease.get("lease_id") or ""), reset_to)
            lifecycle_lease["control_cursor_id"] = reset_to
        if pending_messages:
            raise RuntimePolicyError(
                "用户控制输入待处理",
                f"Agent Console 有 {len(pending_messages)} 条当前租约的新用户修正；请先调用 poll_ui_messages 读取并据此调整后续行为；"
                "若当前对话缓存旧工具 schema、没有 poll_ui_messages，则调用一次非 done 的 report_progress 读取返回的 control_messages",
                retryable=True,
            )
    pending_ack_id = str((lifecycle_lease or {}).get("control_ack_pending_id") or "")
    if (
        pending_ack_id
        and (not is_progress or is_completion)
        and tool_name not in _CONTROL_PRE_ACK_ALLOWED_TOOL_NAMES
    ):
        raise RuntimePolicyError(
            "用户消息待回应",
            "已读取 Agent Console 用户消息，但还没有公开回应。继续任何实质操作或完成任务前，优先调用一次非 done 的 report_progress，"
            "用自然语言回应用户并说明接下来会做什么；若当前 Chat 冻结工具快照里没有 report_progress，先公开回应用户，"
            f'再调用 poll_ui_messages(after_id="ack:{pending_ack_id}") 做兼容 ACK。只有用户明确要求静默/不用回复时才会跳过此约束。',
            retryable=True,
        )
    pending_resume_id = str((lifecycle_lease or {}).get("control_resume_pending_id") or "")
    if pending_resume_id and (is_completion or tool_name == "release_workspace"):
        raise RuntimePolicyError(
            "中断后续作待验证",
            "Agent Console 中断已经读取并回应，但原任务还没有恢复实际工作。"
            "请先成功执行至少一个同租约的后续实质工具/验证动作；额外 report_progress、poll、renew_workspace 或状态查询不算续作。"
            "在此之前不要 phase=done、release_workspace，也不要发送 ChatGPT 最终答复。",
            retryable=True,
        )
    stay_mode = _stay_mode_state(lifecycle_lease or {})
    participant_exit = bool((lifecycle_lease or {}).get("lease_role") == "participant")
    if stay_mode["active"] and (is_completion or tool_name == "release_workspace") and not participant_exit:
        raise RuntimePolicyError(
            "驻场模式生效中",
            f"当前租约处于驻场模式，截止 {stay_mode['until_at']}；驻场期间禁止 done/release。"
            "请等待驻场到期，或让用户明确说出“退出驻场模式”/发送 /stay off；当前 GPT 不得自行解锁。",
            retryable=True,
        )
    if lifecycle_lease is not None and (is_completion or tool_name == "release_workspace"):
        lease_id = str(lifecycle_lease.get("lease_id") or "")
        if lease_id:
            _ensure_workspace_releasable(lease_id)


def _guard_tool_call(function: Any) -> Any:
    @wraps(function)
    def guarded(*args: Any, **kwargs: Any) -> Any:
        call_id = uuid.uuid4().hex
        queue_started = time.monotonic()
        lane_name = _tool_lane_name(function.__name__)
        if function.__name__ == "chatgpt_agent":
            agent_args = _activity_bound_arguments(function, args, kwargs)
            if _agent_read_action(str(agent_args.get("action") or ""), agent_args):
                lane_name = "control"
        lane_semaphore = _TOOL_LANE_SEMAPHORES[lane_name]
        if not lane_semaphore.acquire(timeout=TOOL_QUEUE_TIMEOUT_SECONDS):
            raise _structured_tool_error(
                "请求排队超时",
                f"{lane_name} lane 已等待 {TOOL_QUEUE_TIMEOUT_SECONDS} 秒；请稍后重试",
                retryable=True,
            )
        waited_seconds = time.monotonic() - queue_started
        call_started = time.monotonic()
        telemetry: dict[str, float | str | None] = {
            "queue_wait_seconds": waited_seconds,
            "lane": lane_name,
        }
        tool_outcome = "unknown"
        stage_started = time.monotonic()
        inferred_lease = _activity_find_lease(function, args, kwargs)
        telemetry["activity_correlation_seconds"] = time.monotonic() - stage_started
        is_progress = function.__name__ == "report_progress"
        progress_bound = _activity_bound_arguments(function, args, kwargs) if is_progress else {}
        progress_phase = str(progress_bound.get("phase", "progress")).strip().lower() if is_progress else ""
        is_completion = is_progress and progress_phase == "done"
        is_quiet_activity = function.__name__ in _QUIET_ACTIVITY_TOOL_NAMES
        if not is_progress and not is_quiet_activity:
            _activity_emit(
                "tool.started",
                call_id=call_id,
                tool=function.__name__,
                lease=inferred_lease,
                arguments=_activity_safe_arguments(function, args, kwargs),
            )
        file_context = _activity_file_context(function, args, kwargs)
        command_file_context: dict[str, Any] | None = None
        activity_lease = inferred_lease
        active_lease_tool_id = ""
        try:
            if waited_seconds >= 5:
                LOGGER.warning(
                    "tool_queue_wait instance=%s call=%s tool=%s wait_seconds=%.3f",
                    MCP_INSTANCE_ID,
                    call_id,
                    function.__name__,
                    waited_seconds,
                )
            stage_started = time.monotonic()
            lease = _require_tool_workspace_lease(function, args, kwargs)
            lifecycle_lease = lease or _lifecycle_find_lease(function, args, kwargs)
            active_lease_tool_id = str((lifecycle_lease or {}).get("lease_id") or "")
            if active_lease_tool_id:
                _mark_active_lease_tool(active_lease_tool_id, 1)
            telemetry["lease_resolution_seconds"] = time.monotonic() - stage_started
            activity_lease = lease or inferred_lease
            stage_started = time.monotonic()
            _check_tool_control(
                function.__name__, lifecycle_lease, is_progress=is_progress, is_completion=is_completion,
            )
            telemetry["control_gate_seconds"] = time.monotonic() - stage_started
            call_kwargs = dict(kwargs)
            if "workspace_lease_id" not in inspect.signature(function).parameters:
                call_kwargs.pop("workspace_lease_id", None)
            workspace = lease.get("workspace", "") if lease else ""
            owner_label = lease.get("owner_label", "") if lease else ""
            audited = function.__name__ in _AUDITED_TOOL_NAMES
            if audited:
                LOGGER.info(
                    "tool_start instance=%s call=%s tool=%s parent_pid=%s workspace=%s owner=%s",
                    MCP_INSTANCE_ID,
                    call_id,
                    function.__name__,
                    os.getppid(),
                    workspace,
                    owner_label,
                )
            with _lock_mutating_tool_paths(function, args, call_kwargs, lease=lease) as resource_lock_wait_seconds:
                telemetry["resource_lock_wait_seconds"] = float(resource_lock_wait_seconds)
                if function.__name__ in _WORKSPACE_MUTATION_TOOL_NAMES:
                    # A queued call has not begun its mutation yet. Re-read the lease
                    # and control state after acquiring the resource, before writing.
                    fresh_lease = _require_tool_workspace_lease(function, args, kwargs)
                    fresh_lifecycle = fresh_lease or _lifecycle_find_lease(function, args, kwargs)
                    _check_tool_control(function.__name__, fresh_lifecycle)
                command_file_context = _activity_command_file_context(
                    function, args, call_kwargs, lifecycle_lease
                )
                stage_started = time.monotonic()
                result = function(*args, **call_kwargs)
                telemetry["tool_body_seconds"] = time.monotonic() - stage_started
            if lifecycle_lease is not None and function.__name__ in _VALIDATION_REVISION_TOOL_NAMES:
                should_bump = True
                if function.__name__ in _ACTIVITY_FILE_CHANGE_TOOLS and isinstance(result, dict):
                    should_bump = result.get("status") not in {"no_change", "exists", "conflict"}
                if should_bump:
                    mode_runtime.bump_workspace_validation_revision(str(lifecycle_lease["lease_id"]))
            duration = time.monotonic() - call_started
            if audited or duration >= 2:
                LOGGER.info(
                    "tool_success instance=%s call=%s tool=%s duration_seconds=%.3f",
                    MCP_INSTANCE_ID,
                    call_id,
                    function.__name__,
                    duration,
                )
            stage_started = time.monotonic()
            checkpoint_lease_id = str((lifecycle_lease or {}).get("lease_id") or "")
            if checkpoint_lease_id and not is_completion and function.__name__ != "release_workspace":
                if is_progress:
                    _update_workspace_execution_checkpoint(
                        checkpoint_lease_id,
                        progress_phase=progress_phase or "progress",
                        progress_text=_activity_redact_text(
                            str(progress_bound.get("text", "")), limit=1_200
                        ),
                    )
                elif function.__name__ == "poll_ui_messages":
                    _update_workspace_execution_checkpoint(
                        checkpoint_lease_id,
                        tool_name="poll_ui_messages",
                        side_effect=False,
                    )
                elif not is_quiet_activity and function.__name__ != "acquire_workspace":
                    _update_workspace_execution_checkpoint(
                        checkpoint_lease_id,
                        tool_name=function.__name__,
                        side_effect=function.__name__ in _LEASE_REQUIRED_TOOL_NAMES,
                    )
            telemetry["checkpoint_seconds"] = time.monotonic() - stage_started
            stage_started = time.monotonic()
            if is_progress:
                _activity_emit(
                    "agent.progress",
                    call_id=call_id,
                    lease=activity_lease,
                    phase=progress_phase or "progress",
                    text=_activity_redact_text(str(progress_bound.get("text", "")), limit=2_000),
                )
                if lifecycle_lease is not None:
                    lease_id = str(lifecycle_lease.get("lease_id") or "")
                    if not is_completion and str(lifecycle_lease.get("lease_role") or "owner") != "participant":
                        renewed = _renew_workspace(lease_id, ttl_seconds=DEFAULT_LEASE_SECONDS)
                        lifecycle_lease = _active_lease_record(lease_id) or renewed
                        if isinstance(result, dict):
                            result = {
                                **result,
                                "owner_lease_renewed": True,
                                "lease_until_at": renewed.get("expires_at"),
                                "controller_id": mode_runtime.lease_controller_id(renewed),
                            }
                    liveness_reply = _ack_pending_thread_liveness(lifecycle_lease)
                    if liveness_reply:
                        _activity_emit(
                            "runtime.liveness_reply",
                            call_id=call_id,
                            lease=activity_lease,
                            phase="progress",
                            text=str(liveness_reply.get("text") or ""),
                            probe=liveness_reply,
                        )
                        if isinstance(result, dict):
                            result = {**result, "liveness_probe": liveness_reply}
                if not is_completion and lifecycle_lease is not None and isinstance(result, dict):
                    lease_id = str(lifecycle_lease.get("lease_id") or "")
                    if lease_id and lifecycle_lease.get("control_ack_pending_id"):
                        _update_workspace_control_ack_pending(lease_id, None)
                        lifecycle_lease.pop("control_ack_pending_id", None)
                    control = _consume_ui_messages_via_progress(lifecycle_lease)
                    if (
                        control["control_count"]
                        or control["control_cursor_reset"]
                        or control.get("control_ack_required")
                        or control.get("control_resume_required")
                    ):
                        result = {
                            **result,
                            **control,
                        }
                if is_completion:
                    lease_id = str((lifecycle_lease or {}).get("lease_id") or "")
                    if not lease_id:
                        raise RuntimePolicyError("任务完成握手失败", "phase=done 缺少有效工作区租约，不能标记任务完成")
                    if str((lifecycle_lease or {}).get("lease_role") or "owner") != "participant":
                        _require_ultra_team_finalized_before_root_release(lease_id)
                    release_result = _release_workspace(lease_id)
                    release_status = str(release_result.get("status") or "")
                    if release_status not in {"released", "already_released"}:
                        raise RuntimePolicyError("任务完成握手失败", f"租约释放未成功：{release_status or 'unknown'}")
                    if isinstance(result, dict):
                        result = {
                            **result,
                            "lease_released": True,
                            "release_status": release_status,
                            "lease_id": lease_id,
                            "lease_role": release_result.get("lease_role", "owner"),
                            "shared_root_lease_id": release_result.get("shared_root_lease_id"),
                            "resource_lease_released": release_result.get("resource_lease_released", True),
                        }
                    _activity_emit(
                        "task.completed",
                        call_id=call_id,
                        lease=activity_lease,
                        status="completed",
                    )
            elif not is_quiet_activity:
                if (
                    lifecycle_lease is not None
                    and lifecycle_lease.get("control_resume_pending_id")
                    and function.__name__ not in _CONTROL_RESUME_NON_ACTION_TOOL_NAMES
                ):
                    lease_id = str(lifecycle_lease.get("lease_id") or "")
                    if lease_id:
                        _update_workspace_control_resume_pending(lease_id, None)
                        lifecycle_lease.pop("control_resume_pending_id", None)
                _activity_emit(
                    "tool.completed",
                    call_id=call_id,
                    tool=function.__name__,
                    lease=activity_lease,
                    arguments=_activity_safe_arguments(function, args, kwargs),
                    durationMs=round(duration * 1000, 1),
                    resultStatus=(result.get("status") if isinstance(result, dict) else "completed"),
                    result=_activity_safe_result(result),
                )
                _activity_emit_file_change(function, file_context, result, call_id=call_id, lease=activity_lease)
                _activity_emit_command_file_changes(
                    function,
                    command_file_context,
                    call_id=call_id,
                    lease=activity_lease,
                )
                if (
                    function.__name__ == "acquire_workspace"
                    and isinstance(result, dict)
                    and str(result.get("lease_type") or "") != LEASE_TYPE_CONSOLE_RELAY
                    and not result.get("reused_existing")
                ):
                    _activity_emit(
                        "task.started",
                        call_id=call_id,
                        lease=result,
                        title=_activity_redact_text(str(result.get("owner_label", "未命名任务")), limit=500),
                    )
                elif (
                    function.__name__ == "release_workspace"
                    and activity_lease
                    and str(activity_lease.get("lease_type") or "") != LEASE_TYPE_CONSOLE_RELAY
                    and isinstance(result, dict)
                    and result.get("status") == "released"
                ):
                    _activity_emit("task.completed", call_id=call_id, lease=activity_lease, status="completed")
            telemetry["activity_seconds"] = time.monotonic() - stage_started
            stage_started = time.monotonic()
            if (
                isinstance(result, dict)
                and lifecycle_lease is not None
                and not is_completion
                and function.__name__ != "release_workspace"
            ):
                lease_id = str(lifecycle_lease.get("lease_id") or "")
                if lease_id:
                    continuation = _runtime_task_continuation(lease_id)
                    if continuation:
                        result = {**result, **continuation}
                    manager = _CHATGPT_AGENT_MANAGER
                    if manager is not None:
                        try:
                            result = {**result, **manager.safe_boundary(str(lifecycle_lease.get("thread_id") or ""))}
                        except Exception as mailbox_error:
                            # Optional delivery must not make an executed command
                            # appear failed and invite duplicate execution.
                            LOGGER.warning("agent_mailbox_delivery_pending error_type=%s", type(mailbox_error).__name__)
                            result = {**result, "agent_mailbox_delivery_pending": True}
            telemetry["continuation_seconds"] = time.monotonic() - stage_started
            tool_outcome = "success"
            return result
        except ToolError as exc:
            tool_outcome = "tool_error"
            if not is_progress and not is_quiet_activity:
                _activity_emit_command_file_changes(
                    function,
                    command_file_context,
                    call_id=call_id,
                    lease=activity_lease,
                )
                _activity_emit(
                    "tool.failed",
                    call_id=call_id,
                    tool=function.__name__,
                    lease=inferred_lease,
                    arguments=_activity_safe_arguments(function, args, kwargs),
                    durationMs=round((time.monotonic() - call_started) * 1000, 1),
                    error=_activity_redact_text(str(exc), limit=2_000),
                )
            LOGGER.info(
                "tool_business_error instance=%s call=%s tool=%s duration_seconds=%.3f",
                MCP_INSTANCE_ID,
                call_id,
                function.__name__,
                time.monotonic() - call_started,
            )
            raise
        except (OSError, ValueError, RuntimePolicyError, psutil.Error) as exc:
            tool_outcome = "expected_error"
            category, detail, retryable, details = _expected_tool_error_details(exc)
            if not is_progress and not is_quiet_activity:
                _activity_emit_command_file_changes(
                    function,
                    command_file_context,
                    call_id=call_id,
                    lease=activity_lease,
                )
                _activity_emit(
                    "tool.failed",
                    call_id=call_id,
                    tool=function.__name__,
                    lease=inferred_lease,
                    arguments=_activity_safe_arguments(function, args, kwargs),
                    durationMs=round((time.monotonic() - call_started) * 1000, 1),
                    errorType=category,
                    error=_activity_redact_text(detail, limit=2_000),
                )
            LOGGER.info(
                "tool_expected_error instance=%s call=%s tool=%s category=%s duration_seconds=%.3f",
                MCP_INSTANCE_ID,
                call_id,
                function.__name__,
                category,
                time.monotonic() - call_started,
            )
            raise _structured_tool_error(category, detail, retryable=retryable, details=details) from exc
        finally:
            if active_lease_tool_id:
                _mark_active_lease_tool(active_lease_tool_id, -1)
            telemetry["wrapper_total_seconds"] = time.monotonic() - queue_started
            telemetry["serialization_seconds"] = None  # MCP transport serialization occurs outside this guard.
            try:
                LOGGER.info(
                    "tool_e2e %s",
                    json.dumps(
                        {
                            "instance": MCP_INSTANCE_ID,
                            "call": call_id,
                            "tool": function.__name__,
                            "outcome": tool_outcome,
                            "stages": {
                                key: (round(value, 6) if isinstance(value, float) else value)
                                for key, value in telemetry.items()
                            },
                        },
                        ensure_ascii=False,
                        separators=(",", ":"),
                    ),
                )
            finally:
                lane_semaphore.release()

    return guarded


_register_tool = mcp.tool


def _register_guarded_tool(*args: Any, **kwargs: Any) -> Any:
    register = _register_tool(*args, **kwargs)

    def decorator(function: Any) -> Any:
        return register(_guard_tool_call(function))

    return decorator


mcp.tool = _register_guarded_tool


_CHATGPT_AGENT_MANAGER: AgentManager | None = None
_CHATGPT_AGENT_MANAGER_LOCK = threading.Lock()


def _chatgpt_agent_control_check(lease_id: str) -> None:
    lease = _active_lease_record(lease_id)
    if lease is None:
        raise RuntimePolicyError("GPTAgent caller identity expired", "当前调用方租约已失效；不得继续网页副作用")
    _check_tool_control("chatgpt_agent", lease)


def _chatgpt_agent_runtime_facts(lease_id: str) -> dict[str, Any]:
    return dict(_runtime_task_continuation(lease_id).get("runtime_task") or {})


def _chatgpt_agent_root_promoter(root_lease_id: str, participant_lease_id: str, *,
                                 reason: str, owner_label: str | None = None) -> dict[str, Any]:
    active_calls = _active_lease_tool_count(root_lease_id)
    if active_calls:
        raise RuntimePolicyError(
            "Root 仍有在途工具",
            "Root rescue 只能在旧 Root 在途工具排空后提交；保持 Child 身份稍后重试。",
            retryable=True,
            error_type="ROOT_RESCUE_TOOLS_ACTIVE",
            details={"active_tool_calls": active_calls},
        )
    return mode_runtime.promote_workspace_participant_to_owner(
        root_lease_id,
        participant_lease_id,
        reason=reason,
        owner_label=owner_label,
    )


def _get_chatgpt_agent_manager() -> AgentManager:
    global _CHATGPT_AGENT_MANAGER
    with _CHATGPT_AGENT_MANAGER_LOCK:
        if _CHATGPT_AGENT_MANAGER is None:
            _CHATGPT_AGENT_MANAGER = AgentManager(
                load_machine_config(runtime_root=str(PROJECT_ROOT)),
                caller_resolver=_active_lease_record,
                control_check=_chatgpt_agent_control_check,
                runtime_facts=_chatgpt_agent_runtime_facts,
                ultra_setter=mode_runtime.set_ultra_mode,
                participant_attacher=mode_runtime.attach_workspace_participant,
                participant_rotator=mode_runtime.rotate_workspace_participant,
                participant_releaser=mode_runtime.release_workspace_participant,
                terminal_participant_releaser=mode_runtime.release_workspace_participants_for_agent,
                root_promoter=_chatgpt_agent_root_promoter,
                root_active_tool_count=_active_lease_tool_count,
            )
        return _CHATGPT_AGENT_MANAGER


def _dispatch_chatgpt_agent_action(action: str, arguments: dict[str, Any]) -> dict[str, Any]:
    payload = dict(arguments)
    payload.pop("action", None)
    try:
        result = _get_chatgpt_agent_manager().dispatch(action, **payload)
        if (
            action == "join"
            and isinstance(result, dict)
            and result.get("participant_workspace_lease_id")
            and not result.get("participant_reused")
        ):
            participant = _active_lease_record(str(result["participant_workspace_lease_id"]))
            if participant is not None:
                agent = result.get("agent") if isinstance(result.get("agent"), dict) else {}
                _activity_emit(
                    "task.started",
                    lease=participant,
                    title=_activity_redact_text(
                        str(agent.get("display_name") or agent.get("agent_path") or participant.get("owner_label") or "GPTAgent Child"),
                        limit=500,
                    ),
                )
        return result
    except AgentError as exc:
        raise _structured_tool_error(
            exc.code,
            str(exc),
            retryable=exc.code in {
                "SPAWN_BINDING_PENDING",
                "RECOVERY_BACKOFF",
                "BROWSER_COMPONENT_NOT_READY",
                "MODEL_CONTROLS_UNRECOGNIZED",
            },
            details=exc.details,
        ) from exc


@mcp.tool(annotations=OPEN_WRITE_ANNOTATIONS)
def chatgpt_agent(
    action: Literal["ensure_project", "spawn", "join", "attach", "list", "inspect", "inbox", "message", "followup", "wake", "refresh_turn", "recover_member", "wait", "checkpoint", "resume_context", "history_search", "history_get", "complete", "interrupt", "close", "watchdog_start", "watchdog_stop", "watchdog_status", "ultra_on", "ultra_status", "team_status", "team_message", "team_broadcast", "team_inbox", "task_create", "task_list", "task_claim", "task_update", "team_shutdown", "team_root_rescue", "team_finalize", "web_read", "web_reconcile"],
    agent_ref: str | None = None,
    parent_agent_id: str | None = None,
    name: str | None = None,
    role: str | None = None,
    delegation_reason: str | None = None,
    display_name: str | None = None,
    workspace: str | None = None,
    conversation_url: str | None = None,
    spawn_ticket: str | None = None,
    message: str | None = None,
    summary: str | None = None,
    pending_work: str | None = None,
    artifacts: list[str] | None = None,
    model_policy: str = "non-pro",
    reasoning_effort: Literal["extra_high", "high", "medium"] = "extra_high",
    performance_priority: Literal["latency", "responsiveness", "load"] | None = None,
    allow_pro: bool = False,
    estimated_minutes: float | None = None,
    work_mode: Literal["bounded", "continuous"] = "bounded",
    recovery_mode: Literal["auto", "same_session", "migrate"] = "auto",
    query: str | None = None,
    source_handle: str | None = None,
    root_rescue_token: str | None = None,
    notification_email: str | None = None,
    scope: Literal["self", "lineage", "root", "project"] = "project",
    path_prefix: str | None = None,
    state: str | None = None,
    limit: int = 20,
    offset: int = 0,
    length: int = 2000,
    wait_seconds: float = 0,
    after_event_seq: int | None = None,
    ack_ids: list[str] | None = None,
    idempotency_key: str | None = None,
    reply_to: str | None = None,
    message_kind: Literal["message", "question", "answer", "finding", "blocker", "review_request", "handoff"] = "message",
    task_id: str | None = None,
    expected_version: int | None = None,
    task_status: Literal["done", "blocked", "cancelled", "pending"] | None = None,
    write_paths: list[str] | None = None,
    depends_on: list[str] | None = None,
    workspace_lease_id: str | None = None,
) -> dict[str, Any]:
    """兼容 GPTAgent 总入口。新的 Ultra 协作优先使用 ultra_root / ultra_agent / ultra_recovery / ultra_finalize；历史和低频动作仍可从这里访问。所有入口共用同一 Team、身份、lease、session-generation 与发送对账状态。"""
    return _dispatch_chatgpt_agent_action(action, dict(locals()))


@mcp.tool(annotations=OPEN_WRITE_ANNOTATIONS)
def ultra_root(
    action: Literal["prepare", "status", "spawn", "task_create", "task_list", "message", "broadcast", "followup", "shutdown"],
    workspace_lease_id: str,
    agent_ref: str | None = None,
    name: str | None = None,
    display_name: str | None = None,
    role: str | None = None,
    delegation_reason: str | None = None,
    message: str | None = None,
    summary: str | None = None,
    estimated_minutes: float | None = None,
    work_mode: Literal["bounded", "continuous"] = "bounded",
    write_paths: list[str] | None = None,
    depends_on: list[str] | None = None,
    idempotency_key: str | None = None,
    reply_to: str | None = None,
    message_kind: Literal["message", "question", "answer", "finding", "blocker", "review_request", "handoff"] = "message",
    model_policy: str = "non-pro",
    reasoning_effort: Literal["extra_high", "high", "medium"] = "extra_high",
    performance_priority: Literal["latency", "responsiveness", "load"] | None = None,
    allow_pro: bool = False,
) -> dict[str, Any]:
    """Ultra Root 主控入口：准备 Project、分派 Child、维护任务表、Team 传话/广播、续聊 bounded Child、查看状态或停止新分派。"""
    mapped = {
        "prepare": "ensure_project",
        "status": "team_status",
        "spawn": "spawn",
        "task_create": "task_create",
        "task_list": "task_list",
        "message": "team_message",
        "broadcast": "team_broadcast",
        "followup": "followup",
        "shutdown": "team_shutdown",
    }[action]
    return _dispatch_chatgpt_agent_action(mapped, dict(locals()))


@mcp.tool(annotations=OPEN_WRITE_ANNOTATIONS)
def ultra_agent(
    action: Literal["join", "resume", "inbox", "message", "task_claim", "task_update", "refresh", "checkpoint", "complete"],
    workspace_lease_id: str,
    spawn_ticket: str | None = None,
    conversation_url: str | None = None,
    agent_ref: str | None = None,
    message: str | None = None,
    ack_ids: list[str] | None = None,
    task_id: str | None = None,
    expected_version: int | None = None,
    task_status: Literal["done", "blocked", "cancelled", "pending"] | None = None,
    summary: str | None = None,
    pending_work: str | None = None,
    artifacts: list[str] | None = None,
    idempotency_key: str | None = None,
    reply_to: str | None = None,
    message_kind: Literal["message", "question", "answer", "finding", "blocker", "review_request", "handoff"] = "message",
) -> dict[str, Any]:
    """Ultra Agent 入口：Child join、恢复上下文、收件箱/消息、领取或更新任务、continuous 自续、checkpoint 与完成。只用自己的 participant。"""
    mapped = {
        "join": "join",
        "resume": "resume_context",
        "inbox": "team_inbox",
        "message": "team_message",
        "task_claim": "task_claim",
        "task_update": "task_update",
        "refresh": "refresh_turn",
        "checkpoint": "checkpoint",
        "complete": "complete",
    }[action]
    return _dispatch_chatgpt_agent_action(mapped, dict(locals()))


@mcp.tool(annotations=OPEN_WRITE_ANNOTATIONS)
def ultra_recovery(
    action: Literal["status", "recover_member", "root_rescue", "web_read", "reconcile"],
    workspace_lease_id: str,
    agent_ref: str | None = None,
    recovery_mode: Literal["auto", "same_session", "migrate"] = "auto",
    summary: str | None = None,
    root_rescue_token: str | None = None,
    idempotency_key: str | None = None,
    model_policy: str = "non-pro",
    reasoning_effort: Literal["extra_high", "high", "medium"] = "extra_high",
    performance_priority: Literal["latency", "responsiveness", "load"] | None = None,
) -> dict[str, Any]:
    """Ultra 救援入口：恢复失联成员、Root 已确认掉线后的唯一接班、只读远端状态与未知发送 reconcile；禁止靠迁移重放副作用。"""
    mapped = {
        "status": "team_status",
        "recover_member": "recover_member",
        "root_rescue": "team_root_rescue",
        "web_read": "web_read",
        "reconcile": "web_reconcile",
    }[action]
    return _dispatch_chatgpt_agent_action(mapped, dict(locals()))


@mcp.tool(annotations=OPEN_WRITE_ANNOTATIONS)
def ultra_finalize(
    action: Literal["status", "finalize", "shutdown"],
    workspace_lease_id: str,
    name: str | None = None,
    summary: str | None = None,
    notification_email: str | None = None,
) -> dict[str, Any]:
    """Ultra 完成入口：查看收口状态、停止新分派，或由当前 Root 在 Child/任务/发送全部收口后生成唯一 completion record。"""
    mapped = {
        "status": "team_status",
        "finalize": "team_finalize",
        "shutdown": "team_shutdown",
    }[action]
    return _dispatch_chatgpt_agent_action(mapped, dict(locals()))


def _limit_text(value: str, max_chars: int) -> tuple[str, bool]:
    limit = max(1, min(int(max_chars), MAX_TOOL_OUTPUT_CHARS))
    if len(value) <= limit:
        return value, False
    return value[:limit] + "\n...[输出已截断]", True


def _validate_limit(value: int, name: str, maximum: int = MAX_RESULT_ITEMS) -> int:
    if value < 1:
        raise ValueError(f"{name} 必须大于 0")
    return min(value, maximum)


def _normalise_path(path: str, *, must_exist: bool = False) -> Path:
    if not isinstance(path, str) or not path.strip():
        raise ValueError("路径不能为空")
    if "\x00" in path:
        raise ValueError("路径不能包含空字符")
    drive, tail = ntpath.splitdrive(path)
    if drive and not tail.startswith(("\\", "/")):
        raise ValueError("不支持 Windows 驱动器相对路径，请使用完整路径")
    candidate = Path(path).expanduser()
    if not candidate.is_absolute():
        candidate = DEFAULT_CWD / candidate
    try:
        candidate = candidate.resolve(strict=False)
    except OSError:
        candidate = candidate.absolute()
    if must_exist and not candidate.exists():
        raise FileNotFoundError(str(candidate))
    return candidate


def _normalise_cwd(cwd: str | None) -> Path:
    path = _normalise_path(cwd or str(DEFAULT_CWD), must_exist=True)
    if not path.is_dir():
        raise NotADirectoryError(str(path))
    return path


@contextmanager
def _lock_paths(*paths: Path):
    indices = sorted(
        {
            hash(str(path).casefold()) % PATH_LOCK_STRIPE_COUNT
            for path in paths
        }
    )
    locks = [_PATH_LOCKS[index] for index in indices]
    wait_started = time.monotonic()
    for lock in locks:
        lock.acquire()
    lock_wait_seconds = time.monotonic() - wait_started
    try:
        yield lock_wait_seconds
    finally:
        for lock in reversed(locks):
            lock.release()


@contextmanager
def _lock_mutating_tool_paths(
    function: Any,
    args: tuple[Any, ...],
    kwargs: dict[str, Any],
    *,
    lease: dict[str, Any] | None = None,
):
    tool_name = function.__name__
    if tool_name not in _WORKSPACE_MUTATION_TOOL_NAMES:
        yield 0.0
        return
    paths = _lease_target_paths(function, args, kwargs)
    lease_id = str((lease or {}).get("lease_id") or kwargs.get("workspace_lease_id") or "")
    lease_lock = None
    if lease_id:
        lease_index = hash(lease_id) % LEASE_MUTATION_LOCK_STRIPE_COUNT
        lease_lock = _LEASE_MUTATION_LOCKS[lease_index]
    wait_started = time.monotonic()
    if lease_lock is not None:
        lease_lock.acquire()
    try:
        with _lock_paths(*paths):
            yield time.monotonic() - wait_started
    finally:
        if lease_lock is not None:
            lease_lock.release()


def _utc_iso(timestamp: float) -> str:
    return datetime.fromtimestamp(timestamp, tz=timezone.utc).isoformat().replace(
        "+00:00", "Z"
    )


def _sha256_path(path: Path, *, chunk_size: int = 1024 * 1024) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        while True:
            chunk = handle.read(chunk_size)
            if not chunk:
                break
            digest.update(chunk)
    return digest.hexdigest()


class OpenAIProvidedFile(TypedDict):
    """ChatGPT/Apps SDK file-param payload injected by the host."""

    download_url: str
    file_id: str
    mime_type: NotRequired[str]
    file_name: NotRequired[str]


def _safe_media_filename(value: str) -> str:
    clean = value.strip()
    if not clean or clean in {".", ".."} or "\x00" in clean:
        raise ValueError("filename 不是有效文件名")
    if ntpath.basename(clean) != clean or ntpath.splitdrive(clean)[0] or any(separator in clean for separator in ("/", "\\")):
        raise ValueError("filename 只能是媒体目录下的单个文件名，不能包含路径")
    return clean


def _unique_media_copy(source: Path, directory: Path) -> Path:
    directory.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
    safe_name = _safe_media_filename(source.name)
    target = directory / f"{stamp}-{uuid.uuid4().hex[:8]}-{safe_name}"
    shutil.copy2(source, target)
    return target


def _validate_sha256(value: str | None) -> str | None:
    if value is None:
        return None
    value = value.strip().lower()
    if not re.fullmatch(r"[0-9a-f]{64}", value):
        raise ValueError("expected_sha256 必须是 64 位十六进制 SHA-256")
    return value


def _entry_info(path: Path, *, include_hash: bool = False) -> dict[str, Any]:
    info: dict[str, Any] = {"path": str(path)}
    if path.is_symlink():
        info["type"] = "symlink"
        try:
            info["target"] = os.readlink(path)
        except OSError:
            info["target"] = None
    elif path.is_dir():
        info["type"] = "directory"
    elif path.is_file():
        info["type"] = "file"
    else:
        info["type"] = "other"
    try:
        metadata = path.stat()
        info.update(
            {
                "size": metadata.st_size,
                "created_at": _utc_iso(metadata.st_ctime),
                "modified_at": _utc_iso(metadata.st_mtime),
                "accessed_at": _utc_iso(metadata.st_atime),
                "mode": stat_module.filemode(metadata.st_mode),
            }
        )
        if include_hash and path.is_file():
            info["sha256"] = _sha256_path(path)
    except OSError as exc:
        info["metadata_error"] = str(exc)
    return info


def _decode_bytes(data: bytes, encoding: str = "auto") -> tuple[str, str]:
    requested = encoding.strip().lower()
    if requested != "auto":
        return data.decode(encoding), encoding
    candidates: list[str]
    if data.startswith(b"\xef\xbb\xbf"):
        candidates = ["utf-8-sig"]
    elif data.startswith(b"\xff\xfe") or data.startswith(b"\xfe\xff"):
        candidates = ["utf-16"]
    else:
        candidates = ["utf-8", "gb18030", "utf-16", "cp1252"]
    for candidate in candidates:
        try:
            return data.decode(candidate), candidate
        except UnicodeDecodeError:
            continue
    raise UnicodeDecodeError("auto", data, 0, len(data), "无法识别文本编码")


def _decode_command_output(data: bytes) -> tuple[str, str, bool]:
    """Command output must be returned even when its bytes are mixed or malformed."""
    try:
        text, encoding = _decode_bytes(data, "auto")
        return text, encoding, False
    except UnicodeDecodeError:
        return data.decode("utf-8", errors="replace"), "utf-8", True


def _read_text(path: Path, *, encoding: str = "auto", max_bytes: int = 16 * 1024 * 1024) -> tuple[str, str, bool]:
    if not path.is_file():
        raise FileNotFoundError(str(path))
    max_bytes = _validate_limit(max_bytes, "max_bytes", 512 * 1024 * 1024)
    with path.open("rb") as handle:
        data = handle.read(max_bytes + 1)
    truncated = len(data) > max_bytes
    if truncated:
        data = data[:max_bytes]
    try:
        text, used_encoding = _decode_bytes(data, encoding)
    except UnicodeDecodeError:
        if encoding.strip().lower() == "auto":
            raise ValueError("文件不是可识别的文本，请改用 read_binary_file")
        raise
    return text, used_encoding, truncated


def _read_text_snapshot_with_hash(
    path: Path,
    *,
    encoding: str = "auto",
    max_bytes: int = 16 * 1024 * 1024,
) -> tuple[str, str, bool, str, int, bool]:
    """Read content and SHA-256 from the same open file handle."""
    if not path.is_file():
        raise FileNotFoundError(str(path))
    max_bytes = _validate_limit(max_bytes, "max_bytes", 512 * 1024 * 1024)
    digest = hashlib.sha256()
    collected = bytearray()
    total_bytes = 0
    with path.open("rb") as handle:
        before = os.fstat(handle.fileno())
        while True:
            chunk = handle.read(1024 * 1024)
            if not chunk:
                break
            digest.update(chunk)
            total_bytes += len(chunk)
            if len(collected) < max_bytes + 1:
                remaining = max_bytes + 1 - len(collected)
                collected.extend(chunk[:remaining])
        after = os.fstat(handle.fileno())
    byte_truncated = total_bytes > max_bytes
    data = bytes(collected[:max_bytes])
    try:
        text, used_encoding = _decode_bytes(data, encoding)
    except UnicodeDecodeError:
        if encoding.strip().lower() == "auto":
            raise ValueError("文件不是可识别的文本，请改用 read_binary_file")
        raise
    stable_snapshot = (
        before.st_size == after.st_size
        and before.st_mtime_ns == after.st_mtime_ns
    )
    return (
        text,
        used_encoding,
        byte_truncated,
        digest.hexdigest(),
        total_bytes,
        stable_snapshot,
    )


def _read_code_snapshot(path: Path, *, encoding: str, max_bytes: int) -> tuple[str, str, bool, dict[str, Any]]:
    """Bounded reading with an edit version for complete, stable file snapshots."""
    max_bytes = _validate_limit(max_bytes, "max_bytes", 512 * 1024 * 1024)
    with path.open("rb") as handle:
        before = os.fstat(handle.fileno())
        data = handle.read(max_bytes + 1)
        after = os.fstat(handle.fileno())
    truncated = len(data) > max_bytes
    stable = before.st_size == after.st_size and before.st_mtime_ns == after.st_mtime_ns
    complete = not truncated and len(data) == after.st_size
    try:
        text, used_encoding = _decode_bytes(data[:max_bytes], encoding)
    except UnicodeDecodeError:
        if encoding.strip().lower() == "auto":
            raise ValueError("文件不是可识别的文本，请改用 read_binary_file")
        raise
    return text, used_encoding, truncated, {
        "sha256": hashlib.sha256(data).hexdigest() if complete and stable else None,
        "snapshot_stable": stable,
        "size": after.st_size,
    }


def _edit_feedback(path: Path, before: str, after: str) -> dict[str, Any]:
    """Return a bounded real diff so a successful edit need not be reread only for confirmation."""
    if len(before) + len(after) > EDIT_FEEDBACK_MAX_SOURCE_CHARS:
        return {
            "diff": None,
            "diff_truncated": True,
            "diff_omitted_reason": "source_too_large",
            "diff_additions": None,
            "diff_deletions": None,
            "changed_lines": None,
        }

    chunks: list[str] = []
    retained = 0
    truncated = False
    additions = 0
    deletions = 0
    for line in difflib.unified_diff(
        before.splitlines(keepends=True),
        after.splitlines(keepends=True),
        fromfile=str(path),
        tofile=str(path),
        n=3,
    ):
        if line.startswith("+") and not line.startswith("+++"):
            additions += 1
        elif line.startswith("-") and not line.startswith("---"):
            deletions += 1

        if retained >= EDIT_FEEDBACK_MAX_CHARS:
            truncated = True
            continue
        if retained + len(line) <= EDIT_FEEDBACK_MAX_CHARS:
            chunks.append(line)
            retained += len(line)
        else:
            truncated = True

    diff = "".join(chunks)
    if truncated:
        marker = "\n… diff truncated; read a targeted range only if more context is needed\n"
        room = max(0, EDIT_FEEDBACK_MAX_CHARS - len(diff))
        if room:
            diff += marker[:room]
    return {
        "diff": diff,
        "diff_truncated": truncated,
        "diff_omitted_reason": None,
        "diff_additions": additions,
        "diff_deletions": deletions,
        "changed_lines": additions + deletions,
    }


def _atomic_write(
    path: Path,
    data: bytes,
    *,
    overwrite: bool,
    expected_sha256: str | None = None,
    create_parents: bool = False,
) -> dict[str, Any]:
    with _lock_paths(path):
        return _atomic_write_locked(
            path,
            data,
            overwrite=overwrite,
            expected_sha256=expected_sha256,
            create_parents=create_parents,
        )


def _atomic_write_locked(
    path: Path,
    data: bytes,
    *,
    overwrite: bool,
    expected_sha256: str | None = None,
    create_parents: bool = False,
) -> dict[str, Any]:
    expected_sha256 = _validate_sha256(expected_sha256)
    if path.exists() and path.is_dir():
        raise IsADirectoryError(str(path))
    if not path.parent.exists():
        if not create_parents:
            raise FileNotFoundError(f"父目录不存在：{path.parent}")
        path.parent.mkdir(parents=True, exist_ok=True)
    current_hash = _sha256_path(path) if path.is_file() else None
    if expected_sha256 is not None:
        if current_hash is None:
            return {
                "status": "conflict",
                "path": str(path),
                "reason": "目标文件不存在或不是普通文件",
                "expected_sha256": expected_sha256,
                "actual_sha256": current_hash,
            }
        if current_hash != expected_sha256:
            return {
                "status": "conflict",
                "path": str(path),
                "reason": "目标文件自读取后已经变化，拒绝覆盖",
                "expected_sha256": expected_sha256,
                "actual_sha256": current_hash,
            }
    if path.exists() and not overwrite:
        return {"status": "exists", "path": str(path), "sha256": current_hash}

    temporary_path = path.parent / f".{path.name}.gpt_pc-{uuid.uuid4().hex}.tmp"
    try:
        with temporary_path.open("wb") as handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        if expected_sha256 is not None:
            second_hash = _sha256_path(path) if path.is_file() else None
            if second_hash != expected_sha256:
                return {
                    "status": "conflict",
                    "path": str(path),
                    "reason": "写入前二次校验发现目标文件已经变化，拒绝覆盖",
                    "expected_sha256": expected_sha256,
                    "actual_sha256": second_hash,
                }
        if path.exists() and not overwrite:
            return {"status": "exists", "path": str(path), "sha256": current_hash}
        os.replace(temporary_path, path)
    finally:
        try:
            if temporary_path.exists():
                temporary_path.unlink()
        except OSError:
            pass
    return {
        "status": "written",
        "path": str(path),
        "bytes_written": len(data),
        "sha256": _sha256_path(path),
    }


class _BoundedCommandOutput:
    """Drain a pipe completely while retaining only a bounded output prefix."""

    def __init__(self, max_bytes: int):
        self.max_bytes = max_bytes
        self.buffer = bytearray()
        self.total_bytes = 0
        self.error: OSError | None = None
        self.lock = threading.Lock()

    def consume(self, pipe: Any) -> None:
        try:
            read = getattr(pipe, "read1", pipe.read)
            while chunk := read(64 * 1024):
                with self.lock:
                    self.total_bytes += len(chunk)
                    remaining = self.max_bytes - len(self.buffer)
                    if remaining > 0:
                        self.buffer.extend(chunk[:remaining])
        except OSError as exc:
            self.error = exc
        finally:
            pipe.close()

    def snapshot(self) -> tuple[bytes, bool]:
        with self.lock:
            data = bytes(self.buffer)
            truncated = self.total_bytes > len(data)
        if truncated:
            # Do not misidentify valid UTF-8 as a legacy encoding just because
            # the retained prefix ends in the middle of a character.
            try:
                data.decode("utf-8")
            except UnicodeDecodeError as exc:
                if exc.reason == "unexpected end of data" and exc.end == len(data):
                    data = data[:exc.start]
        return data, truncated


def _run_argv(
    argv: list[str] | str,
    *,
    cwd: Path,
    timeout_seconds: float,
    max_output_chars: int,
    env: dict[str, str] | None = None,
) -> dict[str, Any]:
    if timeout_seconds <= 0:
        raise ValueError("timeout_seconds 必须大于 0")
    timeout_seconds = min(float(timeout_seconds), 24 * 60 * 60)
    max_output_chars = max(1, min(int(max_output_chars), MAX_TOOL_OUTPUT_CHARS))
    started = time.perf_counter()
    process: subprocess.Popen[bytes] | None = None
    timed_out = False
    stdout_bytes = b""
    stderr_bytes = b""
    captures: list[_BoundedCommandOutput] = []
    readers: list[threading.Thread] = []
    deadline = time.monotonic() + timeout_seconds
    stdout_byte_truncated = stderr_byte_truncated = False
    try:
        creation_flags = 0
        if os.name == "nt":
            creation_flags = subprocess.CREATE_NEW_PROCESS_GROUP
        process = subprocess.Popen(
            argv,
            cwd=str(cwd),
            env=env,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            creationflags=creation_flags,
        )
        for pipe in (process.stdout, process.stderr):
            capture = _BoundedCommandOutput(max_output_chars * 4 + 4)
            reader = threading.Thread(target=capture.consume, args=(pipe,), daemon=True)
            captures.append(capture)
            readers.append(reader)
            reader.start()
        try:
            process.wait(timeout=max(0, deadline - time.monotonic()))
            for reader in readers:
                reader.join(timeout=max(0, deadline - time.monotonic()))
            if any(reader.is_alive() for reader in readers):
                raise subprocess.TimeoutExpired(argv, timeout_seconds)
        except subprocess.TimeoutExpired:
            timed_out = True
            if process.poll() is None:
                _terminate_process_tree(process.pid)
            try:
                process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=5)
            drain_deadline = time.monotonic() + 5
            for reader in readers:
                reader.join(timeout=max(0, drain_deadline - time.monotonic()))
        stdout_bytes, stdout_byte_truncated = captures[0].snapshot()
        stderr_bytes, stderr_byte_truncated = captures[1].snapshot()
        stdout_byte_truncated |= readers[0].is_alive()
        stderr_byte_truncated |= readers[1].is_alive()
        for capture in captures:
            if capture.error is not None:
                raise capture.error
    except FileNotFoundError as exc:
        return {
            "exit_code": None,
            "stdout": "",
            "stderr": str(exc),
            "cwd": str(cwd),
            "execution_time": round(time.perf_counter() - started, 3),
            "timed_out": False,
            "command": argv,
        }
    finally:
        if not readers and process is not None and process.stdout:
            process.stdout.close()
        if not readers and process is not None and process.stderr:
            process.stderr.close()
    stdout, stdout_encoding, stdout_decode_replacements = _decode_command_output(stdout_bytes)
    stderr, stderr_encoding, stderr_decode_replacements = _decode_command_output(stderr_bytes)
    stdout, stdout_truncated = _limit_text(stdout, max_output_chars)
    stderr, stderr_truncated = _limit_text(stderr, max_output_chars)
    return {
        "exit_code": process.returncode if process is not None else None,
        "stdout": stdout,
        "stderr": stderr,
        "cwd": str(cwd),
        "execution_time": round(time.perf_counter() - started, 3),
        "timed_out": timed_out,
        "stdout_truncated": stdout_truncated or stdout_byte_truncated,
        "stderr_truncated": stderr_truncated or stderr_byte_truncated,
        "stdout_encoding": stdout_encoding,
        "stderr_encoding": stderr_encoding,
        "stdout_decode_replacements": stdout_decode_replacements,
        "stderr_decode_replacements": stderr_decode_replacements,
        "command": argv,
    }


def _terminate_process_tree(pid: int) -> None:
    if pid <= 0:
        return
    if os.name == "nt":
        subprocess.run(
            ["taskkill.exe", "/PID", str(pid), "/T", "/F"],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            check=False,
        )
        return
    try:
        process = psutil.Process(pid)
        children = process.children(recursive=True)
        for child in children:
            child.terminate()
        process.terminate()
    except (psutil.NoSuchProcess, psutil.AccessDenied):
        pass


def _shell_result(
    argv: list[str] | str, *, cwd: Path, timeout_seconds: float, max_output_chars: int
) -> dict[str, Any]:
    return _run_argv(
        argv,
        cwd=cwd,
        timeout_seconds=timeout_seconds,
        max_output_chars=max_output_chars,
    )


def _powershell_argv(command: str) -> list[str]:
    bundled = PROJECT_ROOT / "powershell7" / "pwsh.exe"
    executable = shutil.which("pwsh") or (str(bundled) if bundled.is_file() else None)
    executable = executable or shutil.which("powershell") or "powershell.exe"
    preamble = (
        "$ProgressPreference = 'SilentlyContinue'; "
        "$utf8 = [System.Text.UTF8Encoding]::new($false); "
        "[Console]::OutputEncoding = $utf8; "
        "$OutputEncoding = $utf8; "
    )
    encoded = base64.b64encode((preamble + command).encode("utf-16le")).decode("ascii")
    return [
        executable,
        "-NoLogo",
        "-NoProfile",
        "-NonInteractive",
        "-ExecutionPolicy",
        "Bypass",
        "-EncodedCommand",
        encoded,
    ]


def _dedicated_command_routing_hint(command: str) -> str | None:
    """Return a small non-blocking hint when a simple shell command has a dedicated MCP tool."""
    stripped = command.strip()
    if not stripped or any(marker in stripped for marker in ("&&", "||", "|", ">", "<", ";")):
        return None
    match = re.match(r'^(?:"([^"]+)"|(\S+))(?:\s+(.*))?$', stripped, flags=re.DOTALL)
    if match is None:
        return None
    executable = ntpath.basename(match.group(1) or match.group(2) or "").casefold()
    remainder = (match.group(3) or "").lstrip()
    dedicated = {
        "npm": "run_npm", "npm.cmd": "run_npm",
        "npx": "run_npx", "npx.cmd": "run_npx",
        "python": "run_python", "python.exe": "run_python",
        "py": "run_python", "py.exe": "run_python",
        "node": "run_node", "node.exe": "run_node",
    }.get(executable)
    if dedicated:
        return f"同类单一命令后续首选 {dedicated}；只有需要 shell 管道、重定向、cmdlet 或多命令组合时才使用 shell 工具。"
    if executable in {"apply_patch", "apply_patch.exe"}:
        return "修改源码补丁应直接调用 apply_patch MCP 工具，不要把 apply_patch 当作 shell 命令执行。"
    return None


def _command_args(args: list[str] | None) -> list[str]:
    if args is None:
        return []
    if not isinstance(args, list) or any(not isinstance(item, str) for item in args):
        raise ValueError("args 必须是字符串列表")
    return args


def _iter_walk(
    root: Path,
    max_depth: int | None = None,
    *,
    include_ignored: bool = False,
):
    if root.is_file():
        yield root, [], [root.name]
        return
    if not root.is_dir():
        raise NotADirectoryError(str(root))
    for current, directories, files in os.walk(
        root, topdown=True, followlinks=False, onerror=lambda exc: None
    ):
        if not include_ignored:
            directories[:] = [
                directory
                for directory in directories
                if directory.casefold() not in DEFAULT_IGNORED_DIRECTORY_NAMES
            ]
        current_path = Path(current)
        try:
            depth = len(current_path.relative_to(root).parts)
        except ValueError:
            depth = 0
        if max_depth is not None and depth >= max_depth:
            directories[:] = []
        yield current_path, directories, files


def _matches_name(name: str, pattern: str, case_sensitive: bool) -> bool:
    if case_sensitive:
        return fnmatch.fnmatchcase(name, pattern)
    return fnmatch.fnmatchcase(name.lower(), pattern.lower())


def _git_run(
    repo_path: str,
    args: list[str],
    *,
    timeout_seconds: float = 120,
    max_output_chars: int = 100_000,
) -> dict[str, Any]:
    cwd = _normalise_path(repo_path, must_exist=True)
    if not cwd.is_dir():
        raise NotADirectoryError(str(cwd))
    result = _run_argv(
        ["git", *args],
        cwd=cwd,
        timeout_seconds=timeout_seconds,
        max_output_chars=max_output_chars,
    )
    result["repository"] = str(cwd)
    return result


def _sensitive_environment_name(name: str) -> bool:
    return bool(re.search(r"(secret|token|password|passwd|api[_-]?key|private|credential|auth)", name, re.I))


def _process_to_dict(process: psutil.Process, *, include_cmdline: bool = False) -> dict[str, Any]:
    try:
        with process.oneshot():
            info: dict[str, Any] = {
                "pid": process.pid,
                "name": process.name(),
                "exe": None,
                "status": process.status(),
                "username": None,
                "started_at": None,
                "memory_bytes": process.memory_info().rss,
            }
            try:
                info["exe"] = process.exe()
            except (psutil.AccessDenied, psutil.NoSuchProcess):
                pass
            try:
                info["username"] = process.username()
            except (psutil.AccessDenied, psutil.NoSuchProcess):
                pass
            try:
                info["started_at"] = _utc_iso(process.create_time())
            except (psutil.AccessDenied, psutil.NoSuchProcess):
                pass
            if include_cmdline:
                try:
                    info["cmdline"] = process.cmdline()
                except (psutil.AccessDenied, psutil.NoSuchProcess):
                    info["cmdline"] = None
            return info
    except (psutil.NoSuchProcess, psutil.AccessDenied) as exc:
        return {"pid": process.pid, "error": str(exc)}


def _apply_unified_patch(original: str, patch: str) -> tuple[str, int]:
    hunk_header = re.compile(
        r"^@@ -(\d+)(?:,(\d+))? \+(\d+)(?:,(\d+))? @@"
    )
    patch_lines = patch.splitlines(keepends=True)
    hunks: list[tuple[int, list[str]]] = []
    index = 0
    while index < len(patch_lines):
        match = hunk_header.match(patch_lines[index].rstrip("\r\n"))
        if not match:
            index += 1
            continue
        old_start = int(match.group(1))
        body: list[str] = []
        index += 1
        while index < len(patch_lines):
            line = patch_lines[index]
            if hunk_header.match(line.rstrip("\r\n")):
                break
            if line.startswith((" ", "+", "-")) or line.startswith("\\"):
                body.append(line)
            else:
                raise ValueError("补丁包含无法识别的行")
            index += 1
        hunks.append((old_start, body))
    if not hunks:
        raise ValueError("补丁中没有找到统一差异区块")

    original_lines = original.splitlines(keepends=True)
    output: list[str] = []
    cursor = 0
    additions = 0

    def same_line(left: str, right: str) -> bool:
        return left.rstrip("\r\n") == right.rstrip("\r\n")

    for old_start, body in hunks:
        target = max(old_start - 1, 0)
        if target < cursor or target > len(original_lines):
            raise ValueError(f"补丁位置不匹配：第 {old_start} 行")
        output.extend(original_lines[cursor:target])
        cursor = target
        for line in body:
            if line.startswith("\\"):
                continue
            prefix, value = line[0], line[1:]
            if prefix == " ":
                if cursor >= len(original_lines) or not same_line(original_lines[cursor], value):
                    raise ValueError("补丁上下文不匹配，文件可能已经变化")
                output.append(original_lines[cursor])
                cursor += 1
            elif prefix == "-":
                if cursor >= len(original_lines) or not same_line(original_lines[cursor], value):
                    raise ValueError("补丁删除行不匹配，文件可能已经变化")
                cursor += 1
            elif prefix == "+":
                output.append(value)
                additions += 1
    output.extend(original_lines[cursor:])
    return "".join(output), additions


@mcp.tool(annotations=READ_ANNOTATIONS)
def computer_info() -> dict[str, Any]:
    """获取这台 Windows 电脑的基本信息、当前用户、Python 和内存概况。"""
    memory = psutil.virtual_memory()
    return {
        "hostname": socket.gethostname(),
        "platform": platform.platform(),
        "system": platform.system(),
        "release": platform.release(),
        "machine": platform.machine(),
        "processor": platform.processor(),
        "user": os.environ.get("USERNAME") or os.environ.get("USER") or "",
        "user_profile": os.environ.get("USERPROFILE", ""),
        "python": sys.executable,
        "python_version": platform.python_version(),
        "process_id": os.getpid(),
        "boot_time": _utc_iso(psutil.boot_time()),
        "memory": {
            "total_bytes": memory.total,
            "available_bytes": memory.available,
            "used_bytes": memory.used,
            "percent": memory.percent,
        },
        "project_root": str(PROJECT_ROOT),
        "lease_workspaces": {
            "desktop_ops": str(DESKTOP_OPS_WORKSPACE),
            "console_relay": str(CONSOLE_RELAY_WORKSPACE),
            "runtime_maintenance": str(RUNTIME_MAINTENANCE_WORKSPACE),
        },
        "media_root": str(mode_runtime.MEDIA_ROOT),
    }


@mcp.tool(annotations=WRITE_ANNOTATIONS)
def acquire_workspace(
    path: str,
    mode: Literal["read_shared", "execute_exclusive"] = "execute_exclusive",
    ttl_seconds: int = DEFAULT_LEASE_SECONDS,
    owner_label: str = "未命名任务",
    thread_id: str | None = None,
    controller_id: str | None = None,
    conversation_id: str | None = None,
    conversation_url: str | None = None,
    conversation_source: str | None = None,
) -> dict[str, Any]:
    """申请工作区租约。controller_id/thread_id/lease_id 都是 Runtime 执行句柄，不是网页 conversation_id；相同值可作为同一执行链的正向证据，不同值不能单独证明来自不同 ChatGPT 对话。开始实质工作前主动按任务判断 Goal/Visual；参考返回的 mode_guidance，简单任务不必开启，用户关闭的模式不得重开。"""
    workspace = _normalise_path(path, must_exist=True)
    result = _acquire_workspace(
        workspace,
        mode=mode,
        ttl_seconds=ttl_seconds,
        owner_label=owner_label,
        thread_id=thread_id,
        controller_id=controller_id,
        runtime_instance=MCP_INSTANCE_ID,
        conversation_id=conversation_id,
        conversation_url=conversation_url,
        conversation_source=conversation_source,
    )
    lease_id = str(result.get("lease_id") or "")
    continuation = _runtime_task_continuation(lease_id) if lease_id else {}
    if mode == "execute_exclusive" and lease_id:
        guidance = _mode_activation_guidance(_active_lease_record(lease_id) or result)
        if guidance:
            result["mode_guidance"] = guidance
    return {**result, **continuation}


@mcp.tool(annotations=WRITE_ANNOTATIONS)
def supersede_workspace_owner(
    target_lease_id: str,
    controller_id: str,
    reason: str,
    owner_label: str | None = None,
    drain_timeout_seconds: float = 30,
) -> dict[str, Any]:
    """同一网页 GPT controller 的新 turn 接管幽灵旧 owner。

    必须精确匹配稳定 controller_id；标题/owner_label 相同不算身份。先 fencing
    旧 lease，阻止旧 turn 发起新的写工具；已经在途的工具允许排空。只有无活跃
    participant、无 Runtime Job 且在途工具归零后才创建新的 owner lease。
    """
    if len(target_lease_id) != 32 or any(c not in "0123456789abcdef" for c in target_lease_id):
        raise ValueError("target_lease_id 必须是 32 位十六进制 lease id")
    if isinstance(drain_timeout_seconds, bool) or not isinstance(drain_timeout_seconds, (int, float)):
        raise ValueError("drain_timeout_seconds 必须是数字")
    timeout = max(0.0, min(float(drain_timeout_seconds), 60.0))
    fenced = mode_runtime.begin_same_controller_takeover(
        target_lease_id,
        controller_id=controller_id,
        reason=reason,
    )
    deadline = time.monotonic() + timeout
    while _active_lease_tool_count(target_lease_id) > 0 and time.monotonic() < deadline:
        time.sleep(0.05)
    remaining = _active_lease_tool_count(target_lease_id)
    if remaining > 0:
        return {
            **fenced,
            "status": "draining",
            "active_tool_calls": remaining,
            "new_lease_id": None,
            "instruction": (
                "旧 owner 已 fencing，不能再发起新的写工具；仍有已在途工具未结束。"
                "保持当前新 turn 只读，稍后用相同 target_lease_id/controller_id 重试本工具完成接管。"
            ),
        }
    taken = mode_runtime.finish_same_controller_takeover(
        target_lease_id,
        controller_id=controller_id,
        runtime_instance=MCP_INSTANCE_ID,
        owner_label=owner_label,
    )
    new_lease_id = str(taken.get("new_lease_id") or taken.get("lease_id") or "")
    continuation = _runtime_task_continuation(new_lease_id) if new_lease_id else {}
    guidance = _mode_activation_guidance(_active_lease_record(new_lease_id) or taken) if new_lease_id else ""
    if guidance:
        taken["mode_guidance"] = guidance
    return {
        **taken,
        **continuation,
        "same_controller_takeover": True,
        "old_turn_fenced": True,
        "drained_old_tool_calls": True,
        "instruction": (
            "same-controller takeover 完成：旧 owner lease 已失权。"
            "本 turn 后续所有副作用必须只使用 new_lease_id；旧 lease 不得再使用。"
        ),
    }


@mcp.tool(annotations=WRITE_ANNOTATIONS)
def apply_stay_command(workspace_lease_id: str, command: str) -> dict[str, Any]:
    """真正应用网页 /stay 时长；仅允许开启/延长，禁止当前 GPT 关闭或缩短自己的驻场。"""
    updated = mode_runtime.apply_stay_command(workspace_lease_id, command)
    return {"status": "applied", "stay_mode": _stay_mode_state(updated),
            "visual_mode": mode_runtime.visual_mode_state(updated), "lease_until_at": updated["expires_at"]}


@mcp.tool(annotations=WRITE_ANNOTATIONS)
def renew_workspace(
    workspace_lease_id: str,
    ttl_seconds: int = DEFAULT_LEASE_SECONDS,
    conversation_id: str | None = None,
    conversation_url: str | None = None,
    conversation_source: str | None = None,
) -> dict[str, Any]:
    """续期已有工作区租约；有直接网页证据时可顺便绑定稳定 Chat conversation_id。"""
    if not workspace_lease_id.strip():
        raise ValueError("workspace_lease_id 不能为空")
    return _renew_workspace(
        workspace_lease_id,
        ttl_seconds=ttl_seconds,
        conversation_id=conversation_id,
        conversation_url=conversation_url,
        conversation_source=conversation_source,
    )


@mcp.tool(annotations=WRITE_ANNOTATIONS)
def release_workspace(workspace_lease_id: str) -> dict[str, Any]:
    """释放完成任务的工作区租约。Ultra Team Root 必须先 finalize。"""
    if not workspace_lease_id.strip():
        raise ValueError("workspace_lease_id 不能为空")
    lease = _active_lease_record(workspace_lease_id)
    if lease and str(lease.get("lease_role") or "owner") != "participant":
        _require_ultra_team_finalized_before_root_release(workspace_lease_id)
    return _release_workspace(workspace_lease_id)


@mcp.tool(annotations=WRITE_ANNOTATIONS)
def force_exit_stay_mode(
    target_lease_id: str,
    reason: str,
    workspace_lease_id: str,
) -> dict[str, Any]:
    """由另一个 GPT thread 外部终止目标任务；可解除 Stay/Visual/Goal 后释放租约。

    这是用户明确放弃/清理旧执行时的逃生口，不把未完成 Goal 伪造成 PASS。
    普通成功完成仍使用 Goal 验收 + report_progress(done)。
    """
    requester = next((item for item in _workspace_status(include_private=True).get("leases", [])
                      if item.get("lease_id") == workspace_lease_id), None)
    if requester is None or (requester.get("mode") != "execute_exclusive" and requester.get("lease_type") != LEASE_TYPE_CONSOLE_RELAY):
        raise RuntimePolicyError("救援身份无效", "需要独立执行租约或零执行权限 Console relay 身份")
    target_id = target_lease_id.strip()
    clean_reason = reason.strip()
    if not target_id:
        raise ValueError("target_lease_id 不能为空")
    if not clean_reason:
        raise ValueError("reason 不能为空")
    if len(clean_reason) > 1_000:
        raise ValueError("reason 最多 1000 字符")

    target = next(
        (
            item
            for item in _workspace_status(include_private=True).get("leases", [])
            if str(item.get("lease_id") or "") == target_id
        ),
        None,
    )
    if target is None:
        raise RuntimePolicyError("目标租约不存在", "目标租约不存在、已过期或已经释放")
    requester_thread = _thread_id_from_lease(requester)
    target_thread = _thread_id_from_lease(target)
    if str(requester.get("lease_id") or "") == target_id or requester_thread == target_thread:
        raise RuntimePolicyError(
            "外部终止拒绝",
            "force_exit_stay_mode 只能由另一个不同 GPT thread 调用；目标 GPT 不能给自己绕过完成门禁",
        )

    before = _stay_mode_state(target)
    release_result = mode_runtime.force_release_staying_workspace(
        workspace_lease_id,
        target_id,
        reason=clean_reason,
    )
    if release_result.get("status") != "released":
        raise RuntimePolicyError("外部终止失败", f"目标租约释放失败：{release_result.get('status')}")
    _activity_emit(
        "task.completed",
        lease=target,
        status="aborted",
        forced=True,
        forcedByLeaseId=requester.get("lease_id"),
        reason=_activity_redact_text(clean_reason, limit=1_000),
    )
    return {
        "status": "forced_terminated",
        "target_lease_id": target_id,
        "target_thread_id": target_thread,
        "previous_until_at": before["until_at"],
        "lease_released": True,
        "reason": clean_reason,
        "goal_status": release_result.get("goal_status"),
    }


@mcp.tool(annotations=WRITE_ANNOTATIONS)
def report_progress(
    phase: Literal["plan", "decision", "progress", "verification", "warning", "done"],
    text: str,
    workspace_lease_id: str,
) -> dict[str, Any]:
    """报告公开进展；非 done 会兼容读取 Console 修正；phase=done 成功时 Runtime 原子释放当前租约。"""
    if not text.strip():
        raise ValueError("text 不能为空")
    if len(text) > 4_000:
        raise ValueError("text 最多 4000 字符")
    return {"status": "reported", "phase": phase}


def _poll_ui_messages_once(
    workspace_lease_id: str,
    after_id: str | None = None,
    max_messages: int = 20,
) -> dict[str, Any]:
    """读取当前 workspace lease 的新 Agent Console 修正，并持久推进消费游标。

    兼容旧 Chat 冻结工具快照：
    - after_id="ack:<message_id>"：确认已公开回应这条 Console 用户修正；
    - after_id="alive:<probe_id>"：确认仍在工作并继续当前任务。
    这两个兼容命令只解除对应 ACK，不会清除 control_resume_required。
    """
    max_messages = _validate_limit(max_messages, "max_messages", 100)
    lease = _active_thread_lease(workspace_lease_id)
    lease_id = str(lease.get("lease_id") or "")
    thread_id = _thread_id_from_lease(lease)
    rows = _targeted_ui_rows(lease)
    raw_after = after_id.strip() if isinstance(after_id, str) and after_id.strip() else None
    compat_ack: dict[str, Any] | None = None
    compat_liveness: dict[str, Any] | None = None
    explicit_after = raw_after
    if raw_after and raw_after.startswith("ack:"):
        message_id = raw_after[4:].strip()
        pending_id = str(lease.get("control_ack_pending_id") or "")
        if not message_id or not pending_id or message_id != pending_id:
            raise RuntimePolicyError(
                "兼容 ACK 目标不匹配",
                "旧工具快照兼容 ACK 必须精确使用当前 control_ack_pending 的消息 ID；先调用 poll_ui_messages 读取当前消息。",
                retryable=True,
                error_type="CONTROL_COMPAT_ACK_MISMATCH",
            )
        _update_workspace_control_ack_pending(lease_id, None)
        lease.pop("control_ack_pending_id", None)
        compat_ack = {"status": "acked", "message_id": message_id}
        explicit_after = None
    elif raw_after and raw_after.startswith("alive:"):
        probe_id = raw_after[6:].strip()
        pending = _thread_pending_liveness_probe(lease)
        current_probe_id = str((pending[1] if pending else {}).get("id") or "")
        if not probe_id or not current_probe_id or probe_id != current_probe_id:
            raise RuntimePolicyError(
                "兼容存活 ACK 目标不匹配",
                "旧工具快照兼容存活确认必须精确使用当前 liveness probe ID；先调用 poll_ui_messages 获取当前 probe。",
                retryable=True,
                error_type="LIVENESS_COMPAT_ACK_MISMATCH",
            )
        compat_liveness = _ack_pending_thread_liveness(lease)
        explicit_after = None
    stored_after = lease.get("control_cursor_id")
    effective_after = explicit_after or (str(stored_after) if isinstance(stored_after, str) and stored_after else None)
    selected, cursor_reset, has_more = _rows_after_id(
        rows,
        after_id=effective_after,
        max_items=max_messages,
    )
    messages = [
        {
            "id": str(row.get("id") or ""),
            "timestamp_ms": row.get("timestampMs"),
            "source_task_id": row.get("taskId"),
            "text": str(row.get("text") or ""),
            "attachments": _ui_attachment_summaries(row),
            "control_behavior": "interrupt" if _ui_message_is_interrupting(row) else "informational",
        }
        for row in selected
        if row.get("id") and isinstance(row.get("text"), str)
    ]
    stay_mode = _apply_stay_commands(lease, messages)
    idle_state = _control_idle_state(lease, rows)
    if cursor_reset:
        next_after_id = str(rows[-1].get("id")) if rows and rows[-1].get("id") else effective_after
    else:
        next_after_id = messages[-1]["id"] if messages else effective_after
    if lease_id and next_after_id and next_after_id != stored_after:
        _update_workspace_control_cursor(lease_id, str(next_after_id))
        lease["control_cursor_id"] = str(next_after_id)
    liveness_probe = _deliver_pending_thread_liveness(lease)
    _mark_consumed_ui_messages_for_control_state(lease, messages)
    result = {
        "thread_id": thread_id,
        "lease_id": lease_id,
        "scope": "task",
        "messages": messages,
        "count": len(messages),
        "interrupting_count": sum(1 for message in messages if _ui_message_is_interrupting(message)),
        "liveness_probe": liveness_probe,
        "next_after_id": next_after_id,
        "cursor_reset": cursor_reset,
        "has_more": has_more,
        "stay_mode": stay_mode,
        **_control_gate_state(lease),
        **idle_state,
    }
    if compat_ack is not None:
        result["compat_ack"] = compat_ack
    if compat_liveness is not None:
        result["compat_liveness_ack"] = compat_liveness
    if result.get("control_ack_required") and messages:
        pending_id = str(lease.get("control_ack_pending_id") or messages[-1]["id"])
        result["compat_ack_instruction"] = (
            "若当前 Chat 冻结工具快照里没有 report_progress，先在当前 Chat 公开回应用户，"
            f'再调用 poll_ui_messages(after_id="ack:{pending_id}")；这只确认回应，不会清除续作门禁。'
        )
    if liveness_probe and not liveness_probe.get("acknowledged"):
        probe_id = str(liveness_probe.get("id") or "")
        if probe_id:
            result["compat_liveness_instruction"] = (
                "若当前 Chat 冻结工具快照里没有 report_progress，先公开说明仍在工作，"
                f'再调用 poll_ui_messages(after_id="alive:{probe_id}")。'
            )
    return result


def _ui_inbox_signature() -> tuple[int, int]:
    try:
        stat = Path(UI_INPUT_FILE).stat()
    except OSError:
        return (0, 0)
    return (int(stat.st_size), int(stat.st_mtime_ns))


def _poll_wait_signature(lease_id: str) -> tuple[int, int, int]:
    ui_size, ui_mtime = _ui_inbox_signature()
    try:
        lease_mtime = mode_runtime._lease_path(lease_id).stat().st_mtime_ns
    except OSError:
        lease_mtime = 0
    return (ui_size, ui_mtime, int(lease_mtime))


@mcp.tool(annotations=WRITE_ANNOTATIONS)
def relay_console_user_message(
    target_lease_id: str,
    text: str,
    user_authorized: bool,
    workspace_lease_id: str,
    allow_task_control: bool = False,
) -> dict[str, Any]:
    """按用户本轮明确授权，用零执行权限的远程监视/传话身份把一条消息作为 Console 用户输入投递给另一个正在执行的任务。"""
    target_id = target_lease_id.strip()
    clean = text.strip()
    if not target_id:
        raise ValueError("target_lease_id 不能为空")
    if not clean:
        raise ValueError("text 不能为空")
    if len(clean) > 8_000:
        raise ValueError("text 最多 8000 字符")
    if user_authorized is not True:
        raise RuntimePolicyError("缺少用户授权", "跨对话 Console 传话必须由用户在本轮明确要求；不能由 GPT 自主发起")

    target = next(
        (item for item in _workspace_status(include_private=True).get("leases", [])
         if str(item.get("lease_id") or "") == target_id and str(item.get("mode") or "") == "execute_exclusive"),
        None,
    )
    if target is None:
        raise RuntimePolicyError("目标任务不可用", "目标必须是一个当前仍在执行的活动任务；请先查看当前工作区状态并选择准确目标")

    sender = _active_console_relay_identity(workspace_lease_id)
    sender_thread = _thread_id_from_lease(sender)
    target_thread = _thread_id_from_lease(target)
    if str(sender.get("lease_id") or "") == target_id or sender_thread == target_thread:
        raise RuntimePolicyError("禁止自发消息", "正在执行的对话不能通过中继给自己发送 Console 用户消息；请选择来自另一个独立 GPT 对话的零执行权限传话身份")
    identity_assurance = "console_relay_cross_thread"

    message_probe = {"id": "relay-probe", "text": clean}
    stay_command = _parse_stay_command(clean)
    is_task_control = (stay_command is not None) or (not _ui_message_requires_task_resume(message_probe))
    if is_task_control and not allow_task_control:
        raise RuntimePolicyError(
            "需要更高控制授权",
            "这条消息可能停止/结束当前任务或改变驻场状态。默认中继禁止这类控制；只有用户本轮明确要求该控制时才能开启 allow_task_control",
        )

    now_ms = round(time.time() * 1000)
    message_id = f"relay-{now_ms}-{os.getpid()}-{uuid.uuid4().hex[:10]}"
    row: dict[str, Any] = {
        "id": message_id,
        "timestampMs": now_ms,
        "type": "ui.message",
        "scope": "task",
        "taskId": target_id,
        "threadId": _thread_id_from_lease(target),
        "text": clean,
        "attachments": [],
        "relay": {
            "origin": "gpt_console_relay",
            "authorization": "current_user_explicit",
            "identityAssurance": identity_assurance,
            "senderLeaseId": sender.get("lease_id") if sender else None,
            "senderThreadId": _thread_id_from_lease(sender) if sender else None,
            "senderOwnerLabel": sender.get("owner_label") if sender else None,
        },
    }
    _append_ui_input_row(row)
    return {
        "status": "sent",
        "message_id": message_id,
        "target_lease_id": target_id,
        "target_workspace": target.get("workspace"),
        "target_owner_label": target.get("owner_label"),
        "delivered_as": "user_console_input",
        "identity_assurance": identity_assurance,
        "task_control_allowed": bool(allow_task_control),
    }


@mcp.tool(annotations=READ_ANNOTATIONS)
def poll_ui_messages(
    workspace_lease_id: str,
    after_id: str | None = None,
    max_messages: int = 20,
    wait_seconds: float = 0.0,
) -> dict[str, Any]:
    """读取当前租约的 Console 修正；也接收高优先级 Runtime liveness probe。

    新工具快照优先用 report_progress 明确 ACK；旧 Chat 冻结快照缺 report_progress 时，
    可复用既有 after_id 字段：ack:<message_id> 或 alive:<probe_id>。兼容 ACK 不会替代真实续作。
    """
    try:
        wait_value = float(wait_seconds)
    except (TypeError, ValueError) as exc:
        raise ValueError("wait_seconds 必须是数字") from exc
    if wait_value < 0 or wait_value > 60:
        raise ValueError("wait_seconds 必须在 0 到 60 秒之间")

    started = time.monotonic()
    result = _poll_ui_messages_once(
        workspace_lease_id=workspace_lease_id,
        after_id=after_id,
        max_messages=max_messages,
    )
    if result.get("interrupting_count") or result.get("liveness_probe") or wait_value <= 0:
        result["waited_seconds"] = round(time.monotonic() - started, 3)
        result["woke_on_message"] = bool(result.get("interrupting_count"))
        result["woke_on_liveness_probe"] = bool(result.get("liveness_probe"))
        return result

    deadline = started + wait_value
    signature = _poll_wait_signature(workspace_lease_id)
    while True:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            break
        time.sleep(min(0.25, remaining))
        current_signature = _poll_wait_signature(workspace_lease_id)
        if current_signature == signature:
            continue
        signature = current_signature
        result = _poll_ui_messages_once(
            workspace_lease_id=workspace_lease_id,
            after_id=None,
            max_messages=max_messages,
        )
        if result.get("interrupting_count") or result.get("liveness_probe"):
            result["waited_seconds"] = round(time.monotonic() - started, 3)
            result["woke_on_message"] = bool(result.get("interrupting_count"))
            result["woke_on_liveness_probe"] = bool(result.get("liveness_probe"))
            return result

    result = _poll_ui_messages_once(
        workspace_lease_id=workspace_lease_id,
        after_id=None,
        max_messages=max_messages,
    )
    result["waited_seconds"] = round(time.monotonic() - started, 3)
    result["woke_on_message"] = bool(result.get("interrupting_count"))
    result["woke_on_liveness_probe"] = bool(result.get("liveness_probe"))
    return result


def _visual_content_for_record(lease: dict[str, Any], record: dict[str, Any]):
    path = visual_io.owned_image_path(str(record.get("path") or ""), lease, UI_ATTACHMENT_DIR)
    metadata, data, mime = visual_io.read_image(path)
    if record.get("sha256") and metadata["sha256"] != record["sha256"]:
        raise RuntimePolicyError("视觉证据变化", "图片与记录的 SHA256 不符，必须重新取得观察")
    return [TextContent(text=json.dumps({"source": "agent.observation" if str(record.get("id", "")).startswith("obs-") else "visual.reference",
                                        **{k: record.get(k) for k in (
                                            "id", "label", "name", "iteration", "width", "height",
                                            "capture_type", "browser_url", "viewport_width", "viewport_height",
                                            "journey_id", "interaction_status", "browser_step_count", "browser_step_results",
                                            "frontend_health", "requirements_revision", "workspace_revision",
                                        )},
                                        "delivery": metadata}, ensure_ascii=False)),
            ImageContent(data=base64.b64encode(data).decode("ascii"), mimeType=mime)]


def _publish_visual_bytes(lease: dict[str, Any], data: bytes, label: str, capture: dict[str, Any] | None = None):
    observation = visual_io.persist_image(str(lease["lease_id"]), "observations", data, label=label)
    observation.update(capture or {})
    observation["inspected_at"] = mode_runtime.utc_iso()
    observation = mode_runtime.record_visual_observation(str(lease["lease_id"]), observation)
    content = _visual_content_for_record(lease, observation)
    _activity_emit("agent.observation", lease=lease, observation=observation)
    return content


@mcp.tool(annotations=WRITE_ANNOTATIONS)
def set_visual_mode(
    workspace_lease_id: str,
    action: Literal["enable", "extend", "update", "review", "complete"] = "enable",
    duration_seconds: int | None = None,
    command: str | None = None,
    references: list[dict[str, Any]] | None = None,
    criteria: list[str] | None = None,
    targets: list[dict[str, Any]] | None = None,
    review: dict[str, Any] | None = None,
    depth: Literal["visual", "interactive", "frontend"] | None = None,
    journeys: list[dict[str, Any]] | None = None,
    goal_enabled: bool | None = None,
    goal_outcome: str | None = None,
    goal_success_criteria: list[str] | None = None,
    goal_reuse_visual: bool | None = None,
) -> dict[str, Any]:
    """主动按任务语义设置 Goal/Visual。多阶段任务可只开 Goal：action=update + goal_enabled=true + goal_outcome + goal_success_criteria。Goal 开启后是 done/release 硬门禁：完成时用 action=complete，并在 review.goal 中逐条提交与原 success_criteria 精确对应的 PASS 和公开 evidence；验收后若工作区再修改会自动回到 needs_review。当前 GPT 不能靠 goal_enabled=false 自行绕过，只有用户可明确关闭。参考图/界面/排版影响判断时再开启 Visual；Visual 同样必须基于真实 observation/review 完成。depth=interactive/frontend 时还要求交互/前端健康证据。模式不扩大权限。"""
    lease = _active_thread_lease(workspace_lease_id)
    if command is not None:
        if action not in {"enable", "extend"} or duration_seconds is not None:
            raise ValueError("command=/vision 3h 仅用于 enable/extend，不能同时传 duration_seconds")
        duration_seconds = parse_visual_duration(command)
    mounted = None if references is None else visual_io.mount_references(lease, references, UI_ATTACHMENT_DIR)
    result = mode_runtime.set_visual_mode(
        workspace_lease_id,
        action=action,
        duration_seconds=duration_seconds,
        references=mounted,
        criteria=criteria,
        targets=targets,
        review=review,
        depth=depth,
        journeys=journeys,
        goal_enabled=goal_enabled,
        goal_outcome=goal_outcome,
        goal_success_criteria=goal_success_criteria,
        goal_reuse_visual=goal_reuse_visual,
    )
    _activity_emit("visual.context", lease=lease, visual=result["visual_mode"], goal=result["goal_mode"])
    return result


@mcp.tool(annotations=READ_ANNOTATIONS)
def get_visual_context(workspace_lease_id: str, include_images: bool = False):
    """恢复本任务参考资料、标准、轮次和最新观察，同时返回当前 completion_review_template。按真实证据填写模板后传给 set_visual_mode(action='complete', review=...)，无需全仓搜索验收字段；PENDING/空证据不能通过。include_images 返回最多四张真实 ImageContent；其他图片可用 read_ui_attachment('visual:<id>') 逐一读取。"""
    lease = _active_thread_lease(workspace_lease_id)
    context = mode_runtime.get_visual_context(workspace_lease_id)
    state = context["visual_mode"]
    images = [ref for ref in state.get("references", []) if ref.get("kind") == "image"]
    observations = state.get("observations", [])
    if observations:
        images.append(observations[-1])
    context["image_read_ids"] = ["visual:" + str(item["id"]) for item in images]
    if not include_images:
        return context
    content = [TextContent(text=json.dumps(context, ensure_ascii=False))]
    # Reserve one slot for the latest observation; avoid loading a whole image history.
    selected = images[:3] + ([observations[-1]] if observations and observations[-1] not in images[:3] else [])
    for item in selected[:4]:
        content.extend(_visual_content_for_record(lease, item))
    return content


@mcp.tool(annotations=WRITE_ANNOTATIONS)
def inspect_image(path: str, workspace_lease_id: str | None = None, label: str | None = None, publish_to_console: bool = True):
    """读取真实本地图像并返回 ImageContent；Runtime media 目录内图片免租约，任务内发布 observation 时可提供 lease。"""
    lease = _active_thread_lease(workspace_lease_id) if isinstance(workspace_lease_id, str) and workspace_lease_id.strip() else None
    if lease is None:
        source = _normalise_path(path, must_exist=True)
        if not source.is_relative_to(mode_runtime.MEDIA_ROOT.resolve(strict=False)):
            raise RuntimePolicyError("媒体图片越界", f"无租约 inspect_image 只允许读取 {mode_runtime.MEDIA_ROOT}", error_type="MEDIA_SCOPE_DENIED")
    else:
        source = visual_io.owned_image_path(path, lease, UI_ATTACHMENT_DIR)
    with source.open("rb") as handle:
        data = handle.read(visual_io.MAX_IMAGE_BYTES + 1)
    if lease is not None and publish_to_console:
        return _publish_visual_bytes(lease, data, label or source.name, {"capture_type": "inspect", "media_source_path": str(source)})
    metadata, delivery, mime = visual_io.decode_image(data)
    return [TextContent(text=json.dumps({"source": "media.image" if lease is None else "local.image", "path": str(source), "label": label or source.name, **metadata}, ensure_ascii=False)),
            ImageContent(data=base64.b64encode(delivery).decode("ascii"), mimeType=mime)]


@mcp.tool(annotations=WRITE_ANNOTATIONS)
def capture_screenshot(
    target: Literal["browser", "active_window", "desktop"],
    workspace_lease_id: str | None = None,
    label: str | None = None,
    browser_url: str | None = None,
    width: int | None = None,
    height: int | None = None,
    full_page: bool = False,
    timeout_ms: int = 20_000,
    browser_steps: list[dict[str, Any]] | None = None,
    journey_id: str | None = None,
    return_snapshot: bool = False,
    check_frontend_health: bool = False,
):
    """抓取真实画面；browser 可在同一次调用中执行最多 32 个有限语义步骤并截图。browser_steps 优先用 role/name、label、testid 等定位完成 click/fill/hover/drag/scroll/assert；图形界面可在定位目标内使用 position={x,y}，drag 使用 delta_x/delta_y，适配 SIJI 这类 SVG 地图平移、滚轮缩放和侧栏拖拽。CSS 仅作 fallback，不开放任意 JS；return_snapshot 返回 Playwright AI ARIA snapshot。frontend depth 自动强制 pageerror/console.error/同源 5xx 与核心请求失败健康验收。普通截图行为保持兼容。"""
    lease = _active_thread_lease(workspace_lease_id) if isinstance(workspace_lease_id, str) and workspace_lease_id.strip() else None
    browser_specific = bool(browser_steps or journey_id or return_snapshot or check_frontend_health)
    if target != "browser" and browser_specific:
        raise ValueError("browser_steps/journey_id/return_snapshot/check_frontend_health 仅支持 target=browser")
    snapshot = None
    visual_depth = "visual"
    if lease is not None:
        visual_state = mode_runtime.get_visual_context(str(lease["lease_id"]))["visual_mode"]
        visual_depth = str(visual_state.get("depth") or "visual")
        if journey_id:
            clean_journey = str(journey_id).strip()
            configured = {str(item.get("id") or "") for item in visual_state.get("journeys", []) if isinstance(item, dict)}
            if visual_depth in {"interactive", "frontend"} and clean_journey not in configured:
                raise ValueError("journey_id 必须属于当前 Visual Mode 已声明的 journeys")
            journey_id = clean_journey
    if journey_id and not browser_steps:
        raise ValueError("journey_id 需要 browser_steps 执行真实交互")
    if target == "browser" and browser_specific:
        data, capture, snapshot = browser_validation.capture_browser_journey(
            browser_url=str(browser_url or ""),
            width=1440 if width is None else width,
            height=900 if height is None else height,
            full_page=full_page,
            timeout_ms=timeout_ms,
            steps=browser_steps,
            return_snapshot=return_snapshot,
            check_frontend_health=bool(check_frontend_health or visual_depth == "frontend"),
        )
        if journey_id:
            capture["journey_id"] = journey_id
    else:
        data, capture = visual_io.capture_screenshot(
            target, browser_url=browser_url, width=width, height=height, full_page=full_page, timeout_ms=timeout_ms,
        )
    clean_label = (label or f"{target} screenshot").strip()[:240] or f"{target} screenshot"
    if lease is not None:
        content = _publish_visual_bytes(lease, data, clean_label, capture)
        if snapshot:
            content.insert(1, TextContent(text=json.dumps({"source": "browser.accessibility", "snapshot": snapshot}, ensure_ascii=False)))
        return content
    record = visual_io.persist_media_image(mode_runtime.MEDIA_SCREENSHOT_DIR, data, prefix=target, label=clean_label)
    metadata, delivery, mime = visual_io.decode_image(data)
    content = [TextContent(text=json.dumps({"source": "media.screenshot", **record, **capture}, ensure_ascii=False))]
    if snapshot:
        content.append(TextContent(text=json.dumps({"source": "browser.accessibility", "snapshot": snapshot}, ensure_ascii=False)))
    content.append(ImageContent(data=base64.b64encode(delivery).decode("ascii"), mimeType=mime))
    return content


def _computer_use_desktop_lease(workspace_lease_id: str) -> dict[str, Any]:
    lease = _active_thread_lease(workspace_lease_id)
    if str(lease.get("lease_type") or "") != "desktop_ops":
        raise RuntimePolicyError(
            "需要桌面操作租约",
            f"Computer Use 必须使用 {DESKTOP_OPS_WORKSPACE} 的 execute_exclusive 租约",
            retryable=True,
            error_type="DESKTOP_OPS_LEASE_REQUIRED",
        )
    return lease


@mcp.tool(annotations=WRITE_ANNOTATIONS)
def computer_use(
    action: Literal["apps", "state", "click", "type_text", "press_key", "set_value", "scroll", "batch"],
    workspace_lease_id: str,
    app: str | None = None,
    snapshot_id: str | None = None,
    element: int | None = None,
    text: str | None = None,
    key: str | None = None,
    x: int | None = None,
    y: int | None = None,
    button: Literal["left", "right", "middle"] = "left",
    count: int = 1,
    direction: Literal["up", "down", "left", "right"] = "down",
    amount: float = 1.0,
    calls: list[dict[str, Any]] | None = None,
    max_nodes: int = 300,
    max_depth: int = 24,
    refresh_after: bool = False,
    focus_mode: Literal["background", "foreground"] = "background",
    timeout_seconds: float = 15.0,
) -> dict[str, Any]:
    """Windows 桌面语义快路。使用 desktop_ops 租约；先 state 取得 snapshot_id 与 UIA 元素 id，后续动作必须携带该 snapshot_id。多个已经确定、不会依赖中间新 UI 的动作可用 batch 一次完成。focus_mode 默认 background，绝不主动抢前台；只有用户明确要求前台操作时才使用 foreground 主动激活目标窗口。type_text 不使用隐式抢焦点的 UIA ValuePattern fallback，窗口消息 fallback 会明确标记 best_effort。视觉/排版敏感的重要动作仍需 capture_screenshot/Visual 做前后对比，state/batch 不能替代视觉验收。"""
    lease = _computer_use_desktop_lease(workspace_lease_id)
    try:
        return computer_backend.execute(
            action=action,
            session_id=str(lease["lease_id"]),
            app=app,
            snapshot_id=snapshot_id,
            element=element,
            text=text,
            key=key,
            x=x,
            y=y,
            button=button,
            count=count,
            direction=direction,
            amount=amount,
            calls=calls,
            max_nodes=max_nodes,
            max_depth=max_depth,
            refresh_after=refresh_after,
            focus_mode=focus_mode,
            timeout_seconds=timeout_seconds,
        )
    except computer_backend.ComputerUseOutcomeUnknown as exc:
        raise RuntimePolicyError(
            "Computer Use 结果不确定",
            str(exc),
            retryable=False,
            error_type="COMPUTER_USE_OUTCOME_UNKNOWN",
            details=exc.details,
        ) from exc
    except computer_backend.ComputerUseUnavailable as exc:
        raise RuntimePolicyError(
            "Computer Use 不可用",
            str(exc),
            retryable=True,
            error_type="COMPUTER_USE_UNAVAILABLE",
        ) from exc
    except computer_backend.ComputerUseError as exc:
        raise RuntimePolicyError(
            "Computer Use 操作失败",
            str(exc),
            retryable=exc.retryable,
            error_type=exc.error_type,
            details=exc.details,
        ) from exc


def _camera_cv2():
    try:
        import cv2
    except ImportError as exc:
        raise RuntimePolicyError(
            "摄像头组件未安装",
            "需要安装 pc-mcp 的 opencv-python-headless 依赖后才能读取摄像头。",
            retryable=False,
            error_type="CAMERA_COMPONENT_MISSING",
        ) from exc
    return cv2


def _camera_open(device_index: int, width: int | None, height: int | None, fps: float | None):
    if device_index < 0 or device_index > 15:
        raise ValueError("device_index 必须在 0 到 15 之间")
    cv2 = _camera_cv2()
    backend_candidates = []
    if os.name == "nt":
        backend_candidates.extend([
            ("dshow", getattr(cv2, "CAP_DSHOW", None)),
            ("msmf", getattr(cv2, "CAP_MSMF", None)),
        ])
    backend_candidates.append(("auto", getattr(cv2, "CAP_ANY", 0)))
    seen = set()
    errors = []
    for backend_name, backend in backend_candidates:
        if backend is None or backend in seen:
            continue
        seen.add(backend)
        cap = cv2.VideoCapture(device_index, backend)
        if not cap.isOpened():
            cap.release()
            errors.append(backend_name)
            continue
        if width is not None:
            cap.set(cv2.CAP_PROP_FRAME_WIDTH, int(width))
        if height is not None:
            cap.set(cv2.CAP_PROP_FRAME_HEIGHT, int(height))
        if fps is not None:
            cap.set(cv2.CAP_PROP_FPS, float(fps))
        ok = False
        frame = None
        for _ in range(4):
            ok, frame = cap.read()
            if ok and frame is not None:
                break
            time.sleep(0.05)
        if ok and frame is not None:
            return cv2, cap, backend_name, frame
        cap.release()
        errors.append(backend_name + ":no-frame")
    raise RuntimePolicyError(
        "摄像头不可用",
        f"无法打开摄像头 index={device_index}；尝试后端：{', '.join(errors) or 'none'}。",
        retryable=True,
        error_type="CAMERA_UNAVAILABLE",
        details={"device_index": device_index, "backends": errors},
    )


def _camera_jpeg(cv2: Any, frame: Any, quality: int = 90) -> bytes:
    ok, encoded = cv2.imencode(".jpg", frame, [int(cv2.IMWRITE_JPEG_QUALITY), int(quality)])
    if not ok:
        raise RuntimePolicyError("摄像头编码失败", "无法把当前帧编码为 JPEG", error_type="CAMERA_ENCODE_FAILED")
    return bytes(encoded.tobytes())


def _camera_image_content(data: bytes, record: dict[str, Any]) -> list[Any]:
    metadata, delivery, mime = visual_io.decode_image(data)
    return [
        TextContent(text=json.dumps({"source": "camera", **record, **metadata}, ensure_ascii=False)),
        ImageContent(data=base64.b64encode(delivery).decode("ascii"), mimeType=mime),
    ]


@mcp.tool(annotations=WRITE_ANNOTATIONS)
def camera(
    action: Literal["list", "photo", "video"],
    workspace_lease_id: str,
    device_index: int = 0,
    max_devices: int = 4,
    duration_seconds: float = 4.0,
    fps: float = 10.0,
    sample_frames: int = 4,
    width: int | None = None,
    height: int | None = None,
    label: str | None = None,
):
    """显式读取本机摄像头。list 枚举可打开设备；photo 返回当前照片；video 录制最多15秒无声视频并返回关键帧供 GPT 直接分析。不会后台常驻。"""
    _computer_use_desktop_lease(workspace_lease_id)
    if width is not None and not 160 <= int(width) <= 3840:
        raise ValueError("width 必须在 160 到 3840 之间")
    if height is not None and not 120 <= int(height) <= 2160:
        raise ValueError("height 必须在 120 到 2160 之间")
    if action == "list":
        max_devices = max(1, min(int(max_devices), 8))
        devices = []
        for index in range(max_devices):
            try:
                cv2, cap, backend, frame = _camera_open(index, width, height, fps)
                try:
                    h, w = frame.shape[:2]
                    devices.append({
                        "device_index": index,
                        "backend": backend,
                        "width": int(w),
                        "height": int(h),
                        "fps": float(cap.get(cv2.CAP_PROP_FPS) or 0.0),
                    })
                finally:
                    cap.release()
            except RuntimePolicyError:
                continue
        return {"status": "ok", "devices": devices, "count": len(devices), "probed": max_devices}

    cv2, cap, backend, first_frame = _camera_open(device_index, width, height, fps)
    camera_dir = (mode_runtime.MEDIA_ROOT / "camera").resolve(strict=False)
    camera_dir.mkdir(parents=True, exist_ok=True)
    clean_label = (label or f"camera {device_index} {action}").strip()[:160] or f"camera {device_index} {action}"
    try:
        if action == "photo":
            frame = first_frame
            for _ in range(2):
                ok, candidate = cap.read()
                if ok and candidate is not None:
                    frame = candidate
            data = _camera_jpeg(cv2, frame)
            record = visual_io.persist_media_image(camera_dir, data, prefix=f"camera-{device_index}", label=clean_label)
            record.update({"action": "photo", "device_index": device_index, "backend": backend})
            return _camera_image_content(data, record)

        duration = max(1.0, min(float(duration_seconds), 15.0))
        target_fps = max(1.0, min(float(fps), 30.0))
        samples_requested = max(2, min(int(sample_frames), 8))
        h, w = first_frame.shape[:2]
        stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
        video_path = camera_dir / f"camera-{device_index}-{stamp}-{uuid.uuid4().hex[:8]}.avi"
        writer = cv2.VideoWriter(
            str(video_path),
            cv2.VideoWriter_fourcc(*"MJPG"),
            target_fps,
            (int(w), int(h)),
        )
        if not writer.isOpened():
            writer.release()
            raise RuntimePolicyError(
                "摄像头视频写入不可用",
                "无法建立 MJPG/AVI writer；照片能力仍可使用。",
                retryable=False,
                error_type="CAMERA_VIDEO_WRITER_UNAVAILABLE",
            )
        sample_times = [duration * index / (samples_requested - 1) for index in range(samples_requested)]
        next_sample = 0
        samples: list[Any] = []
        frame = first_frame
        frames_written = 0
        started = time.monotonic()
        next_frame_at = started
        try:
            while True:
                elapsed = time.monotonic() - started
                if elapsed >= duration:
                    break
                if frames_written:
                    ok, candidate = cap.read()
                    if not ok or candidate is None:
                        continue
                    frame = candidate
                writer.write(frame)
                frames_written += 1
                while next_sample < len(sample_times) and elapsed >= sample_times[next_sample]:
                    samples.append(frame.copy())
                    next_sample += 1
                next_frame_at += 1.0 / target_fps
                delay = next_frame_at - time.monotonic()
                if delay > 0:
                    time.sleep(min(delay, 0.1))
            while next_sample < len(sample_times):
                samples.append(frame.copy())
                next_sample += 1
        finally:
            writer.release()
        if not video_path.is_file() or video_path.stat().st_size <= 0:
            raise RuntimePolicyError("摄像头视频保存失败", "视频文件为空", error_type="CAMERA_VIDEO_EMPTY")
        content: list[Any] = [
            TextContent(text=json.dumps({
                "source": "camera",
                "action": "video",
                "device_index": device_index,
                "backend": backend,
                "video_path": str(video_path),
                "duration_seconds": duration,
                "fps": target_fps,
                "frames_written": frames_written,
                "sample_frames": len(samples),
                "width": int(w),
                "height": int(h),
                "audio": False,
                "label": clean_label,
            }, ensure_ascii=False))
        ]
        for index, sample in enumerate(samples[:8], 1):
            data = _camera_jpeg(cv2, sample)
            record = visual_io.persist_media_image(
                camera_dir,
                data,
                prefix=f"camera-{device_index}-frame-{index}",
                label=f"{clean_label} frame {index}",
            )
            content.extend(_camera_image_content(data, {
                **record,
                "action": "video_frame",
                "frame_index": index,
                "device_index": device_index,
                "backend": backend,
                "video_path": str(video_path),
            }))
        return content
    finally:
        cap.release()


@mcp.tool(annotations=READ_ANNOTATIONS)
def read_ui_attachment(attachment_id: str, workspace_lease_id: str):
    """读取当前 Console 租约消息中的附件；图片直接返回视觉内容，其他文件返回文本或二进制 resource。"""
    lease = _active_thread_lease(workspace_lease_id)
    if attachment_id.startswith("visual:"):
        identity = attachment_id.removeprefix("visual:")
        context = mode_runtime.get_visual_context(workspace_lease_id)["visual_mode"]
        records = context.get("references", []) + context.get("observations", [])
        record = next((item for item in records if item.get("id") == identity and item.get("kind", "image") == "image"), None)
        if record is None and identity.startswith("obs-"):
            # Long-running Visual tasks retain only a recent metadata window.
            # Older image files may still be available in rolling storage, so
            # resolve the exact task-owned observation by id before failing.
            observation_root = (mode_runtime.VISUAL_DIR / str(lease["lease_id"]) / "observations").resolve(strict=False)
            matches = [
                path for path in observation_root.glob(identity + ".*")
                if path.is_file() and path.resolve(strict=False).parent == observation_root
            ] if observation_root.exists() else []
            if len(matches) == 1:
                metadata, _delivery, _mime = visual_io.read_image(matches[0])
                record = {
                    "id": identity,
                    "kind": "image",
                    "path": str(matches[0]),
                    "name": matches[0].name,
                    "label": identity,
                    **metadata,
                }
        if record is None:
            raise FileNotFoundError("该视觉图片不属于当前任务或已超出滚动保留窗口")
        return _visual_content_for_record(lease, record)
    record = _find_ui_attachment_for_lease(lease, attachment_id)
    if record is None:
        raise FileNotFoundError(f"找不到当前租约附件：{attachment_id}")
    lease_id = str(lease.get("lease_id") or "")
    raw_path = str(record.get("path") or "").strip()
    if not raw_path:
        raise FileNotFoundError(f"附件路径缺失：{attachment_id}")
    path = _normalise_path(raw_path, must_exist=True)
    allowed_root = (UI_ATTACHMENT_DIR / lease_id).resolve(strict=False)
    try:
        if not path.is_relative_to(allowed_root):
            raise RuntimePolicyError("附件越界", "附件不在当前租约的受控目录中")
    except ValueError as exc:
        raise RuntimePolicyError("附件越界", "附件路径无效") from exc
    if not path.is_file():
        raise FileNotFoundError(str(path))
    size = path.stat().st_size
    if size > UI_ATTACHMENT_MAX_BYTES:
        raise ValueError("附件超过 25 MB 读取上限")
    data = path.read_bytes()
    name = str(record.get("name") or path.name)[:240]
    mime = str(record.get("mime") or "application/octet-stream")[:160]
    meta = TextContent(text=f"附件：{name}\nMIME：{mime}\n大小：{size} bytes\n本地路径：{path}")
    encoded = base64.b64encode(data).decode("ascii")
    if mime.startswith("image/") or path.suffix.lower() in {".png", ".jpg", ".jpeg", ".webp", ".gif", ".bmp"}:
        image_metadata, delivery, actual_mime = visual_io.decode_image(data)
        verified_meta = TextContent(text=f"附件：{name}\n实际 MIME：{image_metadata['mime']}\n大小：{size} bytes\n尺寸：{image_metadata['width']} × {image_metadata['height']}\n本地路径：{path}")
        return [verified_meta, ImageContent(data=base64.b64encode(delivery).decode("ascii"), mimeType=actual_mime)]
    text_extensions = {".txt", ".md", ".csv", ".json", ".jsonl", ".py", ".js", ".ts", ".tsx", ".jsx", ".html", ".css", ".xml", ".yaml", ".yml", ".toml", ".ini", ".log"}
    if mime.startswith("text/") or path.suffix.lower() in text_extensions:
        text = data.decode("utf-8", errors="replace")
        if len(text) > 2_000_000:
            text = text[:2_000_000] + "\n… [附件文本已截断]"
        return [meta, TextContent(text=text)]
    return [
        meta,
        EmbeddedResource(
            resource=BlobResourceContents(
                uri=path.as_uri(),
                mimeType=mime,
                blob=encoded,
            )
        ),
    ]


@mcp.tool(annotations=WRITE_ANNOTATIONS)
def publish_thread_note(
    workspace_lease_id: str,
    text: str,
    kind: Literal["coordination", "handoff", "decision", "warning"] = "coordination",
) -> dict[str, Any]:
    """向同一 GPT thread 的其他活跃租约发布简短公开协调事实；不得写入隐藏推理或秘密。"""
    lease = _active_thread_lease(workspace_lease_id)
    clean = text.strip()
    if not clean:
        raise ValueError("text 不能为空")
    if len(clean) > THREAD_CONTROL_TEXT_MAX_CHARS:
        raise ValueError(f"text 最多 {THREAD_CONTROL_TEXT_MAX_CHARS} 字符")
    note_id = f"note-{uuid.uuid4().hex}"
    now = datetime.now(timezone.utc)
    thread_id = _thread_id_from_lease(lease)
    row = {
        "id": note_id,
        "timestamp": now.isoformat().replace("+00:00", "Z"),
        "timestampMs": round(now.timestamp() * 1000),
        "type": "thread.note",
        "threadId": thread_id,
        "taskId": lease.get("lease_id"),
        "workspace": lease.get("workspace"),
        "ownerLabel": lease.get("owner_label"),
        "kind": kind,
        "text": _activity_redact_text(clean, limit=THREAD_CONTROL_TEXT_MAX_CHARS),
    }
    _jsonl_append(THREAD_NOTE_FILE, row)
    return {"status": "published", "note_id": note_id, "thread_id": thread_id, "kind": kind}


@mcp.tool(annotations=READ_ANNOTATIONS)
def read_thread_notes(
    workspace_lease_id: str,
    after_id: str | None = None,
    max_notes: int = 20,
) -> dict[str, Any]:
    """读取同一 GPT thread 的公开协调 note，并列出当前同线程活跃租约。"""
    max_notes = _validate_limit(max_notes, "max_notes", 100)
    lease = _active_thread_lease(workspace_lease_id)
    thread_id = _thread_id_from_lease(lease)
    private_status = _workspace_status(include_private=True)
    siblings = [
        {
            "lease_id": item.get("lease_id"),
            "controller_id": mode_runtime.lease_controller_id(
                _active_lease_record(str(item.get("lease_id") or "")) or item
            ),
            "workspace": item.get("workspace"),
            "owner_label": item.get("owner_label"),
            "lease_type": item.get("lease_type"),
            "mode": item.get("mode"),
            "is_current": item.get("lease_id") == lease.get("lease_id"),
        }
        for item in private_status.get("leases", [])
        if str(item.get("thread_id") or item.get("lease_id") or "") == thread_id
    ]
    active_ids = {str(item.get("lease_id") or "") for item in siblings if item.get("lease_id")}
    rows = [
        row
        for row in _jsonl_tail(THREAD_NOTE_FILE)
        if row.get("type") == "thread.note"
        and str(row.get("threadId") or "") == thread_id
        and str(row.get("taskId") or "") in active_ids
    ]
    effective_after = after_id.strip() if isinstance(after_id, str) and after_id.strip() else None
    selected, cursor_reset, has_more = _rows_after_id(
        rows,
        after_id=effective_after,
        max_items=max_notes,
    )
    notes = [
        {
            "id": str(row.get("id") or ""),
            "timestamp": row.get("timestamp"),
            "source_task_id": row.get("taskId"),
            "source_workspace": row.get("workspace"),
            "source_owner_label": row.get("ownerLabel"),
            "kind": row.get("kind"),
            "text": str(row.get("text") or ""),
        }
        for row in selected
        if row.get("id") and isinstance(row.get("text"), str)
    ]
    if cursor_reset:
        next_after_id = str(rows[-1].get("id")) if rows and rows[-1].get("id") else effective_after
    else:
        next_after_id = notes[-1]["id"] if notes else effective_after
    return {
        "thread_id": thread_id,
        "lease_id": lease.get("lease_id"),
        "notes": notes,
        "count": len(notes),
        "next_after_id": next_after_id,
        "cursor_reset": cursor_reset,
        "has_more": has_more,
        "sibling_leases": siblings,
        "sibling_count": len(siblings),
    }


@mcp.tool(annotations=READ_ANNOTATIONS)
def workspace_status(path: str | None = None) -> dict[str, Any]:
    """查看当前工作区的读写租约占用情况；路径级唯一租约会返回可恢复执行上下文。"""
    workspace = _normalise_path(path, must_exist=True) if path else None
    result = _workspace_status(workspace)
    recovery = result.get("recovery") if isinstance(result, dict) else None
    if isinstance(recovery, dict) and isinstance(recovery.get("runtime_task"), dict):
        result = dict(result)
        result["recovery"] = {
            **recovery,
            "runtime_task": _decorate_runtime_recovery_task(recovery["runtime_task"]),
        }
    return result


@mcp.tool(annotations=READ_ANNOTATIONS)
def runtime_status() -> dict[str, Any]:
    """断线恢复时查看本机身份、当前 MCP 实例、活跃租约与后台任务。"""
    active = _list_active_jobs()
    active_jobs = active.get("jobs", []) if isinstance(active, dict) else []
    jobs = {
        "jobs": active_jobs,
        "count": len(active_jobs),
        "history_count": _job_history_count(),
    }
    leases = _workspace_status()
    recovery_candidates = [
        {
            "classification": "RECOVERABLE_OWNER_EXECUTE_LEASE",
            "lease_id": item.get("lease_id"),
            "controller_id": mode_runtime.lease_controller_id(
                _active_lease_record(str(item.get("lease_id") or "")) or item
            ),
            "workspace": item.get("workspace"),
            "scope_root": item.get("scope_root"),
            "lease_type": item.get("lease_type"),
            "owner_label": item.get("owner_label"),
            "web_conversation_id": item.get("web_conversation_id"),
            "web_conversation_url": item.get("web_conversation_url"),
            "participant_count": item.get("participant_count", 0),
            "execution_checkpoint": item.get("execution_checkpoint"),
            "active_lease_is_not_foreign_task_proof": True,
            "participant_count_does_not_transfer_ownership": True,
        }
        for item in leases.get("leases", [])
        if item.get("mode") == "execute_exclusive" and item.get("resource_lease_owner", True)
    ]
    return {
        "hostname": socket.gethostname(),
        "instance_id": MCP_INSTANCE_ID,
        "pid": os.getpid(),
        "parent_pid": os.getppid(),
        "lease_workspaces": {
            "desktop_ops": str(DESKTOP_OPS_WORKSPACE),
            "console_relay": str(CONSOLE_RELAY_WORKSPACE),
            "runtime_maintenance": str(RUNTIME_MAINTENANCE_WORKSPACE),
        },
        "leases": leases,
        "recovery_candidates": recovery_candidates,
        "recovery_instruction": (
            "断线/上下文重建后，先结合 workspace、Goal/最近进展、用户续接语义和可用的网页 conversation 绑定判断。"
            "controller_id 只是 Runtime 执行链标识：相同是同一执行链的强正证据；不同本身不能证明来自不同 ChatGPT 对话。"
            "活动 owner 与当前任务事实一致时，不得仅因 controller/thread/lease 变化就声称‘另一个对话正在工作’；通常复用既有 lease。"
            "若用户/运行事实确认旧 turn 仍在幽灵写入，则按目标 lease 当前 controller_id 做 fencing/drain/rotate；"
            "存在活跃 Child/Runtime Job 或任务身份仍不确定时不得强抢。"
        ),
        "jobs": jobs,
    }


@mcp.tool(annotations=READ_ANNOTATIONS)
def list_drives() -> dict[str, Any]:
    """列出当前 Windows 用户可见的本地磁盘和挂载点。"""
    partitions: list[dict[str, Any]] = []
    seen: set[str] = set()
    for partition in psutil.disk_partitions(all=True):
        mountpoint = partition.mountpoint
        if not mountpoint or mountpoint in seen:
            continue
        seen.add(mountpoint)
        item: dict[str, Any] = {
            "device": partition.device,
            "mountpoint": mountpoint,
            "filesystem": partition.fstype,
            "options": partition.opts,
        }
        try:
            usage = shutil.disk_usage(mountpoint)
            item.update(
                {
                    "total_bytes": usage.total,
                    "used_bytes": usage.used,
                    "free_bytes": usage.free,
                    "free_percent": round(usage.free / usage.total * 100, 2)
                    if usage.total
                    else None,
                }
            )
        except OSError as exc:
            item["usage_error"] = str(exc)
        partitions.append(item)
    return {"drives": partitions, "count": len(partitions)}


@mcp.tool(annotations=READ_ANNOTATIONS)
def disk_usage(path: str) -> dict[str, Any]:
    """获取指定磁盘或目录所在卷的总容量、已用容量和可用容量。"""
    target = _normalise_path(path, must_exist=True)
    usage = shutil.disk_usage(str(target))
    return {
        "path": str(target),
        "total_bytes": usage.total,
        "used_bytes": usage.used,
        "free_bytes": usage.free,
        "used_percent": round(usage.used / usage.total * 100, 2)
        if usage.total
        else None,
    }


@mcp.tool(annotations=READ_ANNOTATIONS)
def get_environment(
    name: str | None = None,
    include_sensitive: bool = False,
    include_values: bool = True,
    max_items: int = 500,
) -> dict[str, Any]:
    """读取当前服务进程的环境变量；敏感值默认隐藏，单独指定名称也遵守此规则。"""
    max_items = _validate_limit(max_items, "max_items", 5_000)
    if name:
        exists = name in os.environ
        sensitive = _sensitive_environment_name(name)
        value: str | None
        if not exists:
            value = None
        elif include_values and (include_sensitive or not sensitive):
            value = os.environ[name]
        elif include_values and sensitive:
            value = "[敏感值已隐藏]"
        else:
            value = None
        return {"name": name, "exists": exists, "value": value, "sensitive": sensitive}
    names = sorted(os.environ)[:max_items]
    values: dict[str, Any] = {}
    for key in names:
        if not include_values:
            values[key] = None
        elif _sensitive_environment_name(key) and not include_sensitive:
            values[key] = "[敏感值已隐藏]"
        else:
            values[key] = os.environ[key]
    return {"variables": values, "count": len(names), "truncated": len(os.environ) > len(names)}


@mcp.tool(annotations=READ_ANNOTATIONS)
def list_processes(
    max_results: int = 200,
    include_cmdline: bool = False,
    sort_by: Literal["pid", "name", "memory"] = "pid",
) -> dict[str, Any]:
    """列出当前可见进程；命令行参数默认不返回，以减少凭据泄露风险。"""
    max_results = _validate_limit(max_results, "max_results")
    processes = [
        _process_to_dict(process, include_cmdline=include_cmdline)
        for process in psutil.process_iter()
    ]
    if sort_by == "name":
        processes.sort(key=lambda item: str(item.get("name", "")).lower())
    elif sort_by == "memory":
        processes.sort(key=lambda item: int(item.get("memory_bytes", 0)), reverse=True)
    else:
        processes.sort(key=lambda item: int(item.get("pid", 0)))
    truncated = len(processes) > max_results
    return {"processes": processes[:max_results], "count": len(processes), "truncated": truncated}


@mcp.tool(annotations=READ_ANNOTATIONS)
def process_info(pid: int, include_cmdline: bool = True) -> dict[str, Any]:
    """获取指定进程的详细信息，包括可见的路径、用户和子进程。"""
    if pid <= 0:
        raise ValueError("pid 必须大于 0")
    process = psutil.Process(pid)
    result = _process_to_dict(process, include_cmdline=include_cmdline)
    try:
        result["parent_pid"] = process.ppid()
        result["children"] = [child.pid for child in process.children(recursive=False)]
        result["threads"] = process.num_threads()
        result["cpu_percent"] = process.cpu_percent(interval=0.05)
    except (psutil.AccessDenied, psutil.NoSuchProcess) as exc:
        result["detail_error"] = str(exc)
    return result


@mcp.tool(annotations=READ_ANNOTATIONS)
def list_directory(
    path: str,
    include_hidden: bool = True,
    include_files: bool = True,
    include_directories: bool = True,
    max_depth: int = 1,
    max_results: int = 500,
    workspace_lease_id: str | None = None,
) -> dict[str, Any]:
    """列出目录；max_depth=1 为直接子项，增大可替代旧 list_directory_tree。"""
    directory = _normalise_path(path, must_exist=True)
    if not directory.is_dir():
        raise NotADirectoryError(str(directory))
    if max_depth < 1 or max_depth > 32:
        raise ValueError("max_depth 必须在 1 到 32 之间")
    max_results = _validate_limit(max_results, "max_results")
    entries: list[dict[str, Any]] = []
    errors: list[str] = []
    for current, directories, files in _iter_walk(directory, max_depth=max_depth):
        if not include_hidden:
            directories[:] = [name for name in directories if not name.startswith(".")]
            files = [name for name in files if not name.startswith(".")]
        directories.sort(key=str.lower)
        files.sort(key=str.lower)
        names = (directories if include_directories else []) + (files if include_files else [])
        for name in names:
            candidate = current / name
            try:
                depth = len(candidate.relative_to(directory).parts)
            except ValueError:
                continue
            if depth > max_depth:
                continue
            try:
                item = _entry_info(candidate)
                item["relative_path"] = str(candidate.relative_to(directory))
                item["depth"] = depth
                entries.append(item)
            except OSError as exc:
                errors.append(f"{candidate}: {exc}")
            if len(entries) >= max_results:
                return {
                    "path": str(directory),
                    "entries": entries,
                    "count": len(entries),
                    "truncated": True,
                    "max_depth": max_depth,
                    "errors": errors[:20],
                }
    return {
        "path": str(directory),
        "entries": entries,
        "count": len(entries),
        "truncated": False,
        "max_depth": max_depth,
        "errors": errors[:20],
    }


def list_directory_tree(
    path: str,
    max_depth: int = 2,
    max_results: int = 1_000,
    include_files: bool = True,
    workspace_lease_id: str | None = None,
) -> dict[str, Any]:
    """按深度列出目录树；默认只展开两层，避免一次返回过大。"""
    root = _normalise_path(path, must_exist=True)
    if not root.is_dir():
        raise NotADirectoryError(str(root))
    if max_depth < 0:
        raise ValueError("max_depth 不能小于 0")
    max_results = _validate_limit(max_results, "max_results")
    entries: list[dict[str, Any]] = []
    for current, directories, files in _iter_walk(root, max_depth=max_depth):
        directories.sort(key=str.lower)
        files.sort(key=str.lower)
        for name in directories + (files if include_files else []):
            candidate = current / name
            entries.append(_entry_info(candidate))
            if len(entries) >= max_results:
                return {
                    "path": str(root),
                    "entries": entries,
                    "count": len(entries),
                    "truncated": True,
                }
    return {"path": str(root), "entries": entries, "count": len(entries), "truncated": False}


@mcp.tool(annotations=READ_ANNOTATIONS)
def file_info(
    path: str,
    include_hash: bool = False,
    hash_algorithm: Literal["sha256", "sha1", "md5"] | None = None,
    workspace_lease_id: str | None = None,
) -> dict[str, Any]:
    """读取文件/目录元数据；需要校验时可直接附带 SHA-256/SHA-1/MD5，替代独立 calculate_hash。"""
    target = _normalise_path(path)
    if not target.exists():
        return {"path": str(target), "exists": False, "error": "路径不存在"}
    algorithm = hash_algorithm or ("sha256" if include_hash else None)
    result = _entry_info(target, include_hash=algorithm == "sha256")
    result["exists"] = True
    if algorithm and target.is_file():
        if algorithm == "sha256" and result.get("sha256"):
            value = str(result["sha256"])
        else:
            digest = hashlib.new(algorithm)
            with target.open("rb") as handle:
                while chunk := handle.read(1024 * 1024):
                    digest.update(chunk)
            value = digest.hexdigest()
        result["hash_algorithm"] = algorithm
        result["hash"] = value
        if algorithm == "sha256":
            result["sha256"] = value
    return result


@mcp.tool(annotations=READ_ANNOTATIONS)
def read_text_file(
    path: str,
    encoding: str = "auto",
    max_chars: int = 100_000,
    max_bytes: int = 16 * 1024 * 1024,
    include_hash: bool = False,
    workspace_lease_id: str | None = None,
) -> dict[str, Any]:
    """读取已知文本文件；局部内容用 read_text_range，多文件比较用 read_multiple_files。include_hash=true 同一次稳定快照返回 SHA-256。"""
    target = _normalise_path(path, must_exist=True)
    if include_hash:
        text, used_encoding, byte_truncated, sha256, size, stable_snapshot = _read_text_snapshot_with_hash(
            target, encoding=encoding, max_bytes=max_bytes
        )
        text, char_truncated = _limit_text(text, max_chars)
        return {
            "path": str(target),
            "encoding": used_encoding,
            "content": text,
            "size": size,
            "sha256": sha256,
            "stable_snapshot": stable_snapshot,
            "truncated": byte_truncated or char_truncated,
        }
    text, used_encoding, byte_truncated = _read_text(target, encoding=encoding, max_bytes=max_bytes)
    text, char_truncated = _limit_text(text, max_chars)
    return {
        "path": str(target),
        "encoding": used_encoding,
        "content": text,
        "truncated": byte_truncated or char_truncated,
    }


def read_text_file_with_hash(
    path: str,
    encoding: str = "auto",
    max_chars: int = 100_000,
    max_bytes: int = 16 * 1024 * 1024,
    workspace_lease_id: str | None = None,
) -> dict[str, Any]:
    """需要全文并明确需要稳定 SHA-256 时使用；read_text_range/read_multiple_files 已返回可用稳定哈希时不要重复调用本工具。"""
    target = _normalise_path(path, must_exist=True)
    (
        text,
        used_encoding,
        byte_truncated,
        sha256,
        size,
        stable_snapshot,
    ) = _read_text_snapshot_with_hash(target, encoding=encoding, max_bytes=max_bytes)
    text, char_truncated = _limit_text(text, max_chars)
    return {
        "path": str(target),
        "encoding": used_encoding,
        "content": text,
        "size": size,
        "sha256": sha256,
        "stable_snapshot": stable_snapshot,
        "truncated": byte_truncated or char_truncated,
    }


@mcp.tool(annotations=READ_ANNOTATIONS)
def read_text_tail(
    path: str,
    lines: int = 100,
    encoding: str = "auto",
    max_chars: int = 100_000,
    max_bytes: int = 8 * 1024 * 1024,
    workspace_lease_id: str | None = None,
) -> dict[str, Any]:
    """只需要日志/长输出文件末尾时首选；源码局部按行读取用 read_text_range，不会为了取尾部返回整个文件。"""
    lines = _validate_limit(lines, "lines", 10_000)
    max_bytes = _validate_limit(max_bytes, "max_bytes", 128 * 1024 * 1024)
    target = _normalise_path(path, must_exist=True)
    if not target.is_file():
        raise IsADirectoryError(str(target))
    with target.open("rb") as handle:
        handle.seek(0, os.SEEK_END)
        size = handle.tell()
        read_size = min(size, max_bytes)
        start_offset = size - read_size
        handle.seek(start_offset)
        data = handle.read(read_size)
    if start_offset > 0:
        newline = data.find(b"\n")
        if newline >= 0:
            data = data[newline + 1 :]
    try:
        text, used_encoding = _decode_bytes(data, encoding)
    except UnicodeDecodeError:
        if encoding.strip().lower() != "auto":
            raise
        decoded = None
        for shift in range(1, min(5, len(data) + 1)):
            try:
                decoded = data[shift:].decode("utf-8")
                break
            except UnicodeDecodeError:
                continue
        if decoded is None:
            decoded = data.decode("utf-8", errors="replace")
        text, used_encoding = decoded, "utf-8"
    selected = text.splitlines(keepends=True)[-lines:]
    content, char_truncated = _limit_text("".join(selected), max_chars)
    return {
        "path": str(target),
        "encoding": used_encoding,
        "size": size,
        "requested_lines": lines,
        "returned_lines": len(selected),
        "start_offset": start_offset,
        "window_truncated": start_offset > 0,
        "content": content,
        "truncated": char_truncated,
    }
@mcp.tool(annotations=READ_ANNOTATIONS)
def read_text_range(
    path: str,
    start_line: int = 1,
    end_line: int | None = None,
    encoding: str = "auto",
    max_chars: int = 100_000,
    max_bytes: int = 32 * 1024 * 1024,
    workspace_lease_id: str | None = None,
) -> dict[str, Any]:
    """已知具体文件且只需局部代码/文本时首选；不知道文件在哪先 search_text/code_navigate。返回同次稳定 SHA-256，可直接安全修改，无需再取哈希。"""
    if start_line < 1:
        raise ValueError("start_line 必须从 1 开始")
    if end_line is not None and end_line < start_line:
        raise ValueError("end_line 不能小于 start_line")
    target = _normalise_path(path, must_exist=True)
    text, used_encoding, byte_truncated, snapshot = _read_code_snapshot(
        target, encoding=encoding, max_bytes=max_bytes
    )
    lines = text.splitlines(keepends=True)
    selected = lines[start_line - 1 : end_line]
    content, char_truncated = _limit_text("".join(selected), max_chars)
    return {
        "path": str(target),
        "encoding": used_encoding,
        "start_line": start_line,
        "end_line": min(end_line or len(lines), len(lines)),
        "total_lines": len(lines),
        "content": content,
        "truncated": byte_truncated or char_truncated,
        **snapshot,
    }


@mcp.tool(annotations=READ_ANNOTATIONS)
def read_multiple_files(
    paths: list[str],
    encoding: str = "auto",
    max_chars_each: int = 50_000,
    max_bytes_each: int = 8 * 1024 * 1024,
    workspace_lease_id: str | None = None,
) -> dict[str, Any]:
    """已知2个以上相关文件且需要一起比较/修改时首选；单文件局部首选 read_text_range，不知道路径先搜索。各文件同时返回稳定 SHA-256，单文件失败不影响其他结果。"""
    if not paths:
        raise ValueError("paths 不能为空")
    if len(paths) > 100:
        raise ValueError("一次最多读取 100 个文件")
    results: list[dict[str, Any]] = []
    for raw_path in paths:
        try:
            target = _normalise_path(raw_path, must_exist=True)
            text, used_encoding, byte_truncated, snapshot = _read_code_snapshot(
                target, encoding=encoding, max_bytes=max_bytes_each
            )
            text, char_truncated = _limit_text(text, max_chars_each)
            results.append(
                {
                    "path": str(target),
                    "content": text,
                    "encoding": used_encoding,
                    "truncated": byte_truncated or char_truncated,
                    **snapshot,
                }
            )
        except Exception as exc:  # one bad path should not hide successful reads
            results.append({"path": raw_path, "error": str(exc)})
    return {"files": results, "count": len(results)}


@mcp.tool(annotations=READ_ANNOTATIONS)
def read_binary_file(path: str, max_bytes: int = 1_000_000, workspace_lease_id: str | None = None) -> dict[str, Any]:
    """读取二进制文件并以 Base64 返回，默认最多返回约一兆字节。"""
    target = _normalise_path(path, must_exist=True)
    if not target.is_file():
        raise IsADirectoryError(str(target))
    max_bytes = _validate_limit(max_bytes, "max_bytes", 64 * 1024 * 1024)
    with target.open("rb") as handle:
        data = handle.read(max_bytes + 1)
    truncated = len(data) > max_bytes
    data = data[:max_bytes]
    return {
        "path": str(target),
        "size": target.stat().st_size,
        "base64": base64.b64encode(data).decode("ascii"),
        "truncated": truncated,
    }


@mcp.tool(annotations=READ_ANNOTATIONS)
def search_files(
    root: str,
    pattern: str = "*",
    recursive: bool = True,
    include_directories: bool = False,
    case_sensitive: bool = False,
    max_depth: int | None = None,
    max_results: int = 100,
    timeout_seconds: float = 30,
    include_ignored: bool = False,
    workspace_lease_id: str | None = None,
) -> dict[str, Any]:
    """只知道文件/目录名称或通配符、不知道路径时首选；搜源码内容/错误文字用 search_text，大仓符号关系用 code_navigate。"""
    base = _normalise_path(root, must_exist=True)
    max_results = _validate_limit(max_results, "max_results")
    if max_depth is not None and max_depth < 0:
        raise ValueError("max_depth 不能小于 0")
    started = time.perf_counter()
    matches: list[dict[str, Any]] = []
    timed_out = False
    if base.is_file():
        if _matches_name(base.name, pattern, case_sensitive):
            matches.append(_entry_info(base))
        return {
            "root": str(base),
            "matches": matches,
            "count": len(matches),
            "truncated": False,
            "timed_out": False,
            "backend": "python",
        }
    if native_backend.prefer_native_for_tree(base, include_ignored=include_ignored):
        try:
            native = native_backend.search_files_native(
                base,
                pattern=pattern,
                recursive=recursive,
                include_directories=include_directories,
                case_sensitive=case_sensitive,
                max_depth=max_depth,
                max_results=max_results,
                timeout_seconds=timeout_seconds,
                include_ignored=include_ignored,
            )
            native_matches: list[dict[str, Any]] = []
            for path_text in native["paths"]:
                candidate = Path(path_text)
                try:
                    native_matches.append(_entry_info(candidate))
                except OSError as exc:
                    native_matches.append({"path": str(candidate), "error": str(exc)})
            return {
                "root": str(base),
                "matches": native_matches,
                "count": len(native_matches),
                "truncated": bool(native.get("truncated")),
                "timed_out": bool(native.get("timed_out")),
                "backend": str(native.get("backend") or "fd"),
            }
        except native_backend.NativeAccelerationUnavailable as exc:
            LOGGER.debug("search_files native fallback: %s", exc)
    walk_depth = max_depth if recursive else 0
    for current, directories, files in _iter_walk(
        base,
        max_depth=walk_depth,
        include_ignored=include_ignored,
    ):
        candidates = directories if include_directories else []
        candidates = list(candidates) + list(files)
        for name in candidates:
            if time.perf_counter() - started > timeout_seconds:
                timed_out = True
                break
            if not _matches_name(name, pattern, case_sensitive):
                continue
            candidate = current / name
            try:
                matches.append(_entry_info(candidate))
            except OSError as exc:
                matches.append({"path": str(candidate), "error": str(exc)})
            if len(matches) >= max_results:
                return {
                    "root": str(base),
                    "matches": matches,
                    "count": len(matches),
                    "truncated": True,
                    "timed_out": timed_out,
                }
        if timed_out:
            break
    return {
        "root": str(base),
        "matches": matches,
        "count": len(matches),
        "truncated": timed_out,
        "timed_out": timed_out,
    }


def _git_search_candidates(base: Path, timeout_seconds: float) -> list[Path] | None:
    """Keep Git ignores on the Python fallback without a stale file index.

    Explicit ignored directory roots still use the ordinary walk, so logs and
    generated data remain searchable when the caller intentionally targets them.
    """
    repository = next((path for path in (base, *base.parents) if (path / '.git').exists()), None)
    if repository is None or timeout_seconds <= 0:
        return None
    git = shutil.which('git')
    if git is None:
        return None
    deadline = time.perf_counter() + timeout_seconds

    def run(arguments):
        return subprocess.run(
            [git, '-C', str(base), *arguments], stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, encoding='utf-8', errors='replace',
            timeout=max(0.01, deadline-time.perf_counter()), check=False,
            creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0),
        )

    try:
        if base != repository:
            ignored = run(['check-ignore', '--quiet', '--', '.'])
            if ignored.returncode != 1:
                return None
        result = run(['ls-files', '--cached', '--others', '--exclude-standard', '-z', '--', '.'])
    except (OSError, subprocess.TimeoutExpired):
        return None
    if result.returncode != 0:
        return None
    return [base / path for path in dict.fromkeys(result.stdout.split('\0')) if path]


@mcp.tool(annotations=READ_ANNOTATIONS)
def search_text(
    root: str,
    query: str,
    regex: bool = False,
    file_pattern: str = "*",
    case_sensitive: bool = False,
    context_lines: int = 0,
    max_results: int = 100,
    max_file_bytes: int = 4 * 1024 * 1024,
    timeout_seconds: float = 30,
    include_ignored: bool = False,
    workspace_lease_id: str | None = None,
) -> dict[str, Any]:
    """知道源码文字、错误、配置键或正则但不知道在哪个文件时首选；root 优先限定到相关模块，超时/无结果先缩小范围或纠正文件类型，不要反复扫描整个运行根。默认保留 Git 忽略规则；查日志/生成文件时直接指定该目录，确需全量才 include_ignored=true。只找文件名用 search_files，大仓符号关系用 code_navigate；查询模式验收格式直接用 get_visual_context。"""
    if not query:
        raise ValueError("query 不能为空")
    if context_lines < 0:
        raise ValueError("context_lines 不能小于 0")
    base = _normalise_path(root, must_exist=True)
    max_results = _validate_limit(max_results, "max_results")
    max_file_bytes = _validate_limit(max_file_bytes, "max_file_bytes", 512 * 1024 * 1024)
    if regex:
        flags = 0 if case_sensitive else re.IGNORECASE
        matcher = re.compile(query, flags)
    else:
        needle = query if case_sensitive else query.lower()
        matcher = None
    # One request owns one deadline, including native fallback and traversal.
    started = time.perf_counter()
    if native_backend.prefer_native_for_tree(base, include_ignored=include_ignored):
        try:
            native = native_backend.search_text_native(
                base,
                query=query,
                regex=regex,
                file_pattern=file_pattern,
                case_sensitive=case_sensitive,
                context_lines=context_lines,
                max_results=max_results,
                max_file_bytes=max_file_bytes,
                timeout_seconds=max(0.01, timeout_seconds - (time.perf_counter() - started)),
                include_ignored=include_ignored,
            )
            return {"root": str(base), **native}
        except native_backend.NativeAccelerationUnavailable as exc:
            LOGGER.debug("search_text native fallback: %s", exc)
    matches: list[dict[str, Any]] = []
    scanned_files = 0
    timed_out = False

    def candidates():
        nonlocal timed_out
        if time.perf_counter() - started >= timeout_seconds:
            timed_out = True
            return
        if base.is_file():
            yield base
            return
        git_candidates = None if include_ignored else _git_search_candidates(
            base, timeout_seconds - (time.perf_counter() - started)
        )
        if git_candidates is not None:
            for candidate in git_candidates:
                if time.perf_counter() - started >= timeout_seconds:
                    timed_out = True
                    return
                relative = candidate.relative_to(base)
                if (not any(part.casefold() in DEFAULT_IGNORED_DIRECTORY_NAMES for part in relative.parts[:-1])
                        and _matches_name(candidate.name, file_pattern, case_sensitive)):
                    yield candidate
            return
        if time.perf_counter() - started >= timeout_seconds:
            timed_out = True
            return
        for current, _directories, files in _iter_walk(
            base,
            include_ignored=include_ignored,
        ):
            if time.perf_counter() - started >= timeout_seconds:
                timed_out = True
                return
            for name in files:
                if time.perf_counter() - started >= timeout_seconds:
                    timed_out = True
                    return
                if _matches_name(name, file_pattern, case_sensitive):
                    yield current / name

    for candidate in candidates():
        if time.perf_counter() - started >= timeout_seconds:
            timed_out = True
            break
        try:
            if candidate.stat().st_size > max_file_bytes:
                continue
            text, used_encoding, _ = _read_text(
                candidate, encoding="auto", max_bytes=max_file_bytes
            )
        except (OSError, ValueError, UnicodeError):
            continue
        scanned_files += 1
        lines = text.splitlines()
        for index, line in enumerate(lines):
            if time.perf_counter() - started >= timeout_seconds:
                timed_out = True
                break
            found = bool(matcher.search(line)) if matcher else (needle in (line if case_sensitive else line.lower()))
            if not found:
                continue
            start = max(index - context_lines, 0)
            end = min(index + context_lines + 1, len(lines))
            matches.append(
                {
                    "path": str(candidate),
                    "line": index + 1,
                    "encoding": used_encoding,
                    "text": "\n".join(lines[start:end]),
                }
            )
            if len(matches) >= max_results:
                return {
                    "root": str(base),
                    "matches": matches,
                    "count": len(matches),
                    "scanned_files": scanned_files,
                    "truncated": True,
                    "timed_out": timed_out,
                }
        if timed_out:
            break
    return {
        "root": str(base),
        "matches": matches,
        "count": len(matches),
        "scanned_files": scanned_files,
        "truncated": timed_out,
        "timed_out": timed_out,
    }


@mcp.tool(annotations=READ_ANNOTATIONS)
def code_navigate(
    repo_path: str,
    query: str,
    intent: Literal["symbol", "callers", "callees", "impact", "architecture", "path"] = "symbol",
    max_results: int = 8,
    workspace_lease_id: str | None = None,
) -> dict[str, Any]:
    """大型仓库跨模块定位 symbol/callers/callees/impact/architecture/path 时首选；字面文本搜索用 search_text，已知具体文件局部直接 read_text_range。图谱仅作导航，dirty/stale 自动回退真实源码。"""
    return _navigate_code(repo_path, query, intent=intent, max_results=max_results)


def calculate_hash(
    path: str,
    algorithm: Literal["sha256", "sha1", "md5"] = "sha256",
    workspace_lease_id: str | None = None,
) -> dict[str, Any]:
    """计算文件哈希；默认使用 SHA-256。"""
    target = _normalise_path(path, must_exist=True)
    if not target.is_file():
        raise IsADirectoryError(str(target))
    digest = hashlib.new(algorithm)
    with target.open("rb") as handle:
        while chunk := handle.read(1024 * 1024):
            digest.update(chunk)
    return {"path": str(target), "algorithm": algorithm, "hash": digest.hexdigest()}


@mcp.tool(
    annotations=WRITE_ANNOTATIONS,
    meta={"openai/fileParams": ["file"]},
)
def save_chatgpt_upload(
    file: OpenAIProvidedFile,
    filename: str | None = None,
    overwrite: bool = False,
) -> dict[str, Any]:
    """把当前 ChatGPT 消息的原始上传文件逐字节保存到 Runtime media/uploads；媒体入站不需要 workspace lease。"""
    if not isinstance(file, dict):
        raise ValueError("file 必须由 ChatGPT file-param 注入")
    download_url = str(file.get("download_url") or "").strip()
    if not download_url:
        raise ValueError("ChatGPT file-param 没有提供 download_url；产品层尚未把上传附件桥接到此工具")
    parsed = urllib.parse.urlsplit(download_url)
    host = (parsed.hostname or "").lower()
    trusted_openai_azure_blob = bool(
        re.fullmatch(r"oaisdmntpr[a-z0-9]+\.blob\.core\.windows\.net", host)
    )
    trusted_host = (
        host == "openai.com" or host.endswith(".openai.com")
        or host == "chatgpt.com" or host.endswith(".chatgpt.com")
        or host == "oaiusercontent.com" or host.endswith(".oaiusercontent.com")
        or trusted_openai_azure_blob
    )
    if parsed.scheme.lower() != "https" or not trusted_host or parsed.username or parsed.password:
        raise ValueError(f"download_url 必须是 ChatGPT/OpenAI 提供的 HTTPS 文件地址（host={host or 'missing'}）")
    source_name = str(file.get("file_name") or "").strip()
    target_name = _safe_media_filename(filename or source_name)
    directory = mode_runtime.MEDIA_UPLOAD_DIR.resolve(strict=False)
    directory.mkdir(parents=True, exist_ok=True)
    target = directory / target_name
    if target.exists() and target.is_dir():
        raise IsADirectoryError(str(target))
    if target.exists() and not overwrite:
        raise FileExistsError(str(target))
    temporary = directory / f".{target_name}.chatgpt-upload-{uuid.uuid4().hex}.tmp"
    digest = hashlib.sha256()
    total = 0
    request = urllib.request.Request(download_url, method="GET", headers={"User-Agent": "GPT-PC/0.1.0"})
    try:
        try:
            response_context = urllib.request.urlopen(request, timeout=60)
        except urllib.error.HTTPError as exc:
            if exc.code == 403 and trusted_openai_azure_blob:
                raise RuntimePolicyError(
                    "CHATGPT_FILE_URL_STALE",
                    "ChatGPT 提供的 OpenAI Azure Blob 临时地址返回 403；请重新调用一次 save_chatgpt_upload，让 ChatGPT host 注入新的临时下载地址。",
                ) from exc
            raise
        with response_context as response, temporary.open("xb") as handle:
            while True:
                chunk = response.read(1024 * 1024)
                if not chunk:
                    break
                total += len(chunk)
                if total > 50 * 1024 * 1024:
                    raise ValueError("下载内容超过本工具 50 MiB 上限")
                handle.write(chunk)
                digest.update(chunk)
            handle.flush(); os.fsync(handle.fileno())
        with _lock_paths(target):
            if target.exists() and not overwrite:
                raise FileExistsError(str(target))
            if overwrite:
                os.replace(temporary, target)
            else:
                os.rename(temporary, target)
    finally:
        temporary.unlink(missing_ok=True)
    saved_hash = _sha256_path(target)
    if saved_hash != digest.hexdigest():
        raise RuntimePolicyError("保存后校验失败", "媒体文件 SHA-256 与下载流不一致")
    return {
        "status": "saved", "path": str(target), "bytes_written": total, "sha256": saved_hash,
        "source_file_id": str(file.get("file_id") or "") or None, "source_file_name": source_name or None,
        "mime_type": str(file.get("mime_type") or "") or None, "source_transport": "download_url",
    }


@mcp.tool(annotations=WRITE_ANNOTATIONS)
def create_file(
    path: str,
    content: str = "",
    encoding: str = "utf-8",
    create_parents: bool = False,
    overwrite: bool = False,
    workspace_lease_id: str | None = None,
) -> dict[str, Any]:
    """只用于新建文本文件；修改已有文件不要用，局部修改优先 replace_text/apply_patch。默认不覆盖已有文件。"""
    target = _normalise_path(path)
    data = content.encode(encoding)
    return _atomic_write(
        target,
        data,
        overwrite=overwrite,
        create_parents=create_parents,
    )


@mcp.tool(annotations=WRITE_ANNOTATIONS)
def write_text_file(
    path: str,
    content: str,
    encoding: str = "utf-8",
    create_parents: bool = False,
    overwrite: bool = True,
    expected_sha256: str | None = None,
    workspace_lease_id: str | None = None,
) -> dict[str, Any]:
    """创建或确实需要全量重写整个文本文件时使用；已有文件的局部修改优先 replace_text/apply_patch。expected_sha256 可阻止覆盖新版本。"""
    target = _normalise_path(path)
    data = content.encode(encoding)
    return _atomic_write(
        target,
        data,
        overwrite=overwrite,
        expected_sha256=expected_sha256,
        create_parents=create_parents,
    )


@mcp.tool(annotations=WRITE_ANNOTATIONS)
def append_text_file(
    path: str,
    content: str,
    encoding: str = "auto",
    create_parents: bool = False,
    expected_sha256: str | None = None,
    max_file_bytes: int = 64 * 1024 * 1024,
    workspace_lease_id: str | None = None,
) -> dict[str, Any]:
    """只有明确需要在文件末尾追加内容时使用；修改中间已有内容用 replace_text/apply_patch。支持 expected_sha256 冲突保护。"""
    target = _normalise_path(path)
    if target.exists():
        if not target.is_file():
            raise IsADirectoryError(str(target))
        if target.stat().st_size > max_file_bytes:
            raise ValueError("目标文件超过 max_file_bytes，拒绝一次性重写")
        current_bytes = target.read_bytes()
        used_encoding = encoding if encoding != "auto" else _decode_bytes(current_bytes, "auto")[1]
        data = current_bytes + content.encode(used_encoding)
    else:
        used_encoding = "utf-8" if encoding == "auto" else encoding
        data = content.encode(used_encoding)
    result = _atomic_write(
        target,
        data,
        overwrite=True,
        expected_sha256=expected_sha256,
        create_parents=create_parents,
    )
    result["encoding"] = used_encoding
    return result


@mcp.tool(annotations=WRITE_ANNOTATIONS)
def replace_text(
    path: str,
    old: str,
    new: str,
    count: int = -1,
    encoding: str = "auto",
    expected_sha256: str | None = None,
    max_file_bytes: int = 64 * 1024 * 1024,
    workspace_lease_id: str | None = None,
) -> dict[str, Any]:
    """已知精确 old/new 的简单字符串替换时首选；多行结构性修改或多个相邻改动首选 apply_patch。支持 expected_sha256 冲突保护。"""
    if not old:
        raise ValueError("old 不能为空")
    if count == 0 or count < -1:
        raise ValueError("count 必须为 -1 或正整数")
    target = _normalise_path(path, must_exist=True)
    if target.stat().st_size > max_file_bytes:
        raise ValueError("目标文件超过 max_file_bytes，拒绝一次性重写")
    text, used_encoding, _ = _read_text(target, encoding=encoding, max_bytes=max_file_bytes)
    occurrences = text.count(old)
    if occurrences == 0:
        return {"status": "no_change", "path": str(target), "replacements": 0, "encoding": used_encoding}
    before_text = text
    text = text.replace(old, new, count)
    result = _atomic_write(
        target,
        text.encode(used_encoding),
        overwrite=True,
        expected_sha256=expected_sha256,
    )
    result.update({"replacements": min(occurrences, count) if count > 0 else occurrences, "encoding": used_encoding})
    if result.get("status") == "written":
        result.update(_edit_feedback(target, before_text, text))
    return result


@mcp.tool(annotations=WRITE_ANNOTATIONS)
def apply_patch(
    path: str,
    patch: str,
    expected_sha256: str | None = None,
    encoding: str = "auto",
    max_file_bytes: int = 64 * 1024 * 1024,
    workspace_lease_id: str | None = None,
) -> dict[str, Any]:
    """已有相关源码片段、需要局部结构性修改/多处相邻改动时首选；单个精确字符串替换用 replace_text，大量规律性跨文件机械修改优先项目脚本。支持上下文补丁和 expected_sha256，冲突不写入。"""
    target = _normalise_path(path, must_exist=True)
    if target.stat().st_size > max_file_bytes:
        raise ValueError("目标文件超过 max_file_bytes，拒绝一次性重写")
    text, used_encoding, truncated, snapshot = _read_code_snapshot(target, encoding=encoding, max_bytes=max_file_bytes)
    actual_sha256 = snapshot["sha256"]
    expected_sha256 = _validate_sha256(expected_sha256)
    if truncated or not actual_sha256 or (expected_sha256 is not None and expected_sha256 != actual_sha256):
        return {"status": "conflict", "path": str(target), "actual_sha256": actual_sha256,
                "reason": "文件版本已变化或未取得完整稳定快照，请重新读取目标片段"}
    if patch.lstrip().startswith("*** Begin Patch"):
        patched, additions = apply_context_patch(text, patch, target)
    else:
        patched, additions = _apply_unified_patch(text, patch)
    if patched == text:
        return {
            "status": "no_change",
            "path": str(target),
            "additions": additions,
            "sha256": actual_sha256,
            "diff": "",
            "diff_truncated": False,
            "diff_omitted_reason": None,
            "diff_additions": 0,
            "diff_deletions": 0,
            "changed_lines": 0,
        }
    result = _atomic_write(
        target,
        patched.encode(used_encoding),
        overwrite=True,
        expected_sha256=actual_sha256,
    )
    result.update({"additions": additions, "encoding": used_encoding})
    if result.get("status") == "written":
        result.update(_edit_feedback(target, text, patched))
    return result


@mcp.tool(annotations=WRITE_ANNOTATIONS)
def copy_path(
    source: str,
    destination: str,
    overwrite: bool = False,
    workspace_lease_id: str | None = None,
) -> dict[str, Any]:
    """复制文件或目录；目标已存在时默认拒绝覆盖。"""
    source_path = _normalise_path(source, must_exist=True)
    destination_path = _normalise_path(destination)
    if source_path == destination_path:
        raise ValueError("源和目标不能相同")
    if source_path.is_dir() and destination_path.is_relative_to(source_path):
        raise ValueError("不能把目录复制到自身内部")
    actual_destination = destination_path / source_path.name if destination_path.is_dir() else destination_path
    if actual_destination.exists() and not overwrite:
        return {"status": "exists", "source": str(source_path), "destination": str(actual_destination)}
    if actual_destination.exists() and overwrite:
        if actual_destination.is_dir() and source_path.is_dir():
            shutil.copytree(source_path, actual_destination, dirs_exist_ok=True)
        elif source_path.is_file():
            shutil.copy2(source_path, actual_destination)
        else:
            raise ValueError("不支持把目录复制到已有普通文件")
    elif source_path.is_dir():
        shutil.copytree(source_path, actual_destination)
    else:
        actual_destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source_path, actual_destination)
    return {"status": "copied", "source": str(source_path), "destination": str(actual_destination)}


@mcp.tool(annotations=DESTRUCTIVE_ANNOTATIONS)
def move_path(
    source: str,
    destination: str,
    overwrite: bool = False,
    workspace_lease_id: str | None = None,
) -> dict[str, Any]:
    """移动文件或目录；目标已存在时默认拒绝覆盖。"""
    source_path = _normalise_path(source, must_exist=True)
    destination_path = _normalise_path(destination)
    if source_path == destination_path:
        raise ValueError("源和目标不能相同")
    if source_path.is_dir() and destination_path.is_relative_to(source_path):
        raise ValueError("不能把目录移动到自身内部")
    actual_destination = destination_path / source_path.name if destination_path.is_dir() else destination_path
    if actual_destination.exists() and not overwrite:
        return {"status": "exists", "source": str(source_path), "destination": str(actual_destination)}
    if actual_destination.exists() and overwrite:
        _delete_path(actual_destination, recursive=True)
    actual_destination.parent.mkdir(parents=True, exist_ok=True)
    shutil.move(str(source_path), str(actual_destination))
    return {"status": "moved", "source": str(source_path), "destination": str(actual_destination)}


def rename_path(
    path: str,
    new_name: str,
    overwrite: bool = False,
    workspace_lease_id: str | None = None,
) -> dict[str, Any]:
    """在原目录内重命名文件或目录；new_name 不能包含路径分隔符。"""
    source_path = _normalise_path(path, must_exist=True)
    if not new_name or Path(new_name).name != new_name or new_name in {".", ".."}:
        raise ValueError("new_name 必须是单个名称，不能包含路径分隔符")
    destination = source_path.parent / new_name
    _require_execute_lease(
        workspace_lease_id,
        [source_path, destination],
        descendant_targets=[source_path, destination],
    )
    if destination.exists() and not overwrite:
        return {"status": "exists", "source": str(source_path), "destination": str(destination)}
    if destination.exists():
        _delete_path(destination, recursive=True)
    os.replace(source_path, destination)
    return {"status": "renamed", "source": str(source_path), "destination": str(destination)}


@mcp.tool(annotations=WRITE_ANNOTATIONS)
def make_directory(
    path: str,
    parents: bool = True,
    exist_ok: bool = True,
    workspace_lease_id: str | None = None,
) -> dict[str, Any]:
    """创建目录；默认同时创建缺失的父目录。"""
    target = _normalise_path(path)
    target.mkdir(parents=parents, exist_ok=exist_ok)
    return {"status": "created", "path": str(target), "exists": target.exists()}


def _delete_path(target: Path, *, recursive: bool) -> dict[str, Any]:
    if target.is_symlink() or target.is_file():
        target.unlink()
        return {"status": "deleted", "path": str(target), "type": "file"}
    if target.is_dir():
        if not recursive:
            raise IsADirectoryError(f"目录删除必须明确 recursive=true：{target}")
        try:
            shutil.rmtree(target)
        except PermissionError:
            # Git object files and some Windows-generated files can carry the
            # read-only bit.  Clear only the attributes inside this exact target
            # before retrying; no broader directory is touched.
            for child in target.rglob("*"):
                try:
                    child.chmod(child.stat().st_mode | stat_module.S_IWRITE)
                except OSError:
                    pass
            target.chmod(target.stat().st_mode | stat_module.S_IWRITE)
            shutil.rmtree(target)
        return {"status": "deleted", "path": str(target), "type": "directory"}
    raise FileNotFoundError(str(target))


@mcp.tool(annotations=DESTRUCTIVE_ANNOTATIONS)
def delete_path(
    path: str,
    recursive: bool = False,
    workspace_lease_id: str | None = None,
) -> dict[str, Any]:
    """删除文件或目录；删除目录必须明确 recursive=true。"""
    target = _normalise_path(path, must_exist=True)
    return _delete_path(target, recursive=recursive)


def _run_gmail_bridge(payload: dict[str, Any], *, timeout_seconds: int) -> dict[str, Any]:
    script = PROJECT_ROOT / "scripts" / "gmail-bridge.py"
    if not script.is_file():
        raise FileNotFoundError(str(script))
    encoded_payload = base64.b64encode(
        json.dumps(payload, ensure_ascii=False).encode("utf-8")
    ).decode("ascii")
    process_result = _run_argv(
        [sys.executable, str(script), "--payload-base64", encoded_payload],
        cwd=DESKTOP_OPS_WORKSPACE,
        timeout_seconds=max(timeout_seconds + 35, 210),
        max_output_chars=80_000,
    )
    stdout = str(process_result.get("stdout") or "")
    parsed: dict[str, Any] | None = None
    for line in reversed([item.strip() for item in stdout.splitlines() if item.strip()]):
        try:
            candidate = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(candidate, dict):
            parsed = candidate
            break
    if parsed is None:
        return {
            "status": "error",
            "error": _activity_redact_text(
                str(process_result.get("stderr") or stdout or "Gmail bridge 未返回结构化结果"),
                limit=2_000,
            ),
            "exit_code": process_result.get("exit_code"),
            "timed_out": process_result.get("timed_out", False),
        }
    return {
        **parsed,
        "exit_code": process_result.get("exit_code"),
        "timed_out": process_result.get("timed_out", False),
    }


@mcp.tool(annotations=COMMAND_ANNOTATIONS)
def gmail(
    action: Literal[
        "profile", "search", "get_message", "get_thread", "download_attachment", "send",
        "create_draft", "list_drafts", "get_draft", "update_draft", "send_draft", "delete_draft",
        "list_labels", "create_label", "update_label", "delete_label",
        "modify_message_labels", "batch_modify_labels", "modify_thread_labels",
        "archive", "mark_read", "mark_unread", "star", "unstar",
        "trash", "untrash", "delete_forever", "batch_delete_forever",
        "trash_thread", "untrash_thread", "delete_thread_forever",
    ],
    query: str | None = None,
    message_id: str | None = None,
    message_ids: list[str] | None = None,
    thread_id: str | None = None,
    draft_id: str | None = None,
    attachment_id: str | None = None,
    filename: str | None = None,
    output_filename: str | None = None,
    to: str | None = None,
    cc: str | None = None,
    bcc: str | None = None,
    subject: str | None = None,
    body: str | None = None,
    attachment_files: list[str] | None = None,
    label_id: str | None = None,
    label_name: str | None = None,
    label_ids_add: list[str] | None = None,
    label_ids_remove: list[str] | None = None,
    label_list_visibility: Literal["labelShow", "labelShowIfUnread", "labelHide"] | None = None,
    message_list_visibility: Literal["show", "hide"] | None = None,
    max_results: int = 10,
    include_body: bool = True,
    overwrite: bool = False,
    expected_account: str | None = None,
    timeout_seconds: int = 45,
    workspace_lease_id: str | None = None,
) -> dict[str, Any]:
    """单一 Gmail 全权限入口：只读/附件下载免租约；发送、草稿修改、标签/状态/删除等邮箱副作用使用 desktop_ops。"""
    if action not in _GMAIL_LEASE_FREE_ACTIONS:
        lease = _active_thread_lease(workspace_lease_id or "")
        if str(lease.get("lease_type") or "") != "desktop_ops":
            raise RuntimePolicyError(
                "需要桌面操作租约",
                f"Gmail 邮箱副作用必须使用 {DESKTOP_OPS_WORKSPACE} 的 execute_exclusive 租约",
                retryable=True,
            )
    timeout_seconds = max(15, min(int(timeout_seconds), 120))
    max_results = _validate_limit(max_results, "max_results", 50)
    if query is not None and len(query) > 2_000:
        raise ValueError("query 最多 2000 字符")
    for value, name, limit in (
        (message_id, "message_id", 512), (thread_id, "thread_id", 512), (draft_id, "draft_id", 512),
        (attachment_id, "attachment_id", 8_000), (filename, "filename", 240),
        (label_id, "label_id", 512), (label_name, "label_name", 500),
        (to, "to", 2_000), (cc, "cc", 2_000), (bcc, "bcc", 2_000), (subject, "subject", 500),
    ):
        if value is not None and len(value) > limit:
            raise ValueError(f"{name} 过长")
    if body is not None and len(body) > 200_000:
        raise ValueError("body 最多 200000 字符")
    if message_ids is not None and len(message_ids) > 1_000:
        raise ValueError("message_ids 最多 1000 个")
    if label_ids_add is not None and len(label_ids_add) > 100:
        raise ValueError("label_ids_add 最多 100 个")
    if label_ids_remove is not None and len(label_ids_remove) > 100:
        raise ValueError("label_ids_remove 最多 100 个")
    resolved_files: list[str] | None = None
    if attachment_files is not None:
        if len(attachment_files) > 10:
            raise ValueError("attachment_files 最多 10 个")
        resolved_files = []
        for raw_path in attachment_files:
            path = _normalise_path(raw_path, must_exist=True)
            if not path.is_file():
                raise IsADirectoryError(str(path))
            if not path.is_relative_to(mode_runtime.MEDIA_ROOT.resolve(strict=False)):
                path = _unique_media_copy(path, mode_runtime.MEDIA_OUTBOX_DIR)
            resolved_files.append(str(path))

    payload: dict[str, Any] = {
        "action": action,
        "query": query,
        "message_id": message_id,
        "message_ids": message_ids,
        "thread_id": thread_id,
        "draft_id": draft_id,
        "attachment_id": attachment_id,
        "filename": filename,
        "to": to,
        "cc": cc,
        "bcc": bcc,
        "subject": subject,
        "body": body,
        "attachment_files": resolved_files,
        "label_id": label_id,
        "label_name": label_name,
        "label_ids_add": label_ids_add,
        "label_ids_remove": label_ids_remove,
        "label_list_visibility": label_list_visibility,
        "message_list_visibility": message_list_visibility,
        "max_results": max_results,
        "include_body": bool(include_body),
        "overwrite": bool(overwrite),
        "expected_account": (expected_account or os.environ.get("GPT_PC_GMAIL_EXPECTED_ACCOUNT", "")).strip(),
    }

    target: Path | None = None
    if action == "download_attachment":
        clean_filename = (filename or "").strip()
        requested_output_name = (output_filename or clean_filename).strip()
        if not requested_output_name:
            raise ValueError("download_attachment 必须提供 output_filename，或提供可作为目标名的 filename")
        target_name = _safe_media_filename(requested_output_name)
        mode_runtime.MEDIA_GMAIL_DIR.mkdir(parents=True, exist_ok=True)
        target = mode_runtime.MEDIA_GMAIL_DIR.resolve(strict=False) / target_name
        if target.exists() and target.is_dir():
            raise IsADirectoryError(str(target))
        if target.exists() and not overwrite:
            raise FileExistsError(str(target))
        payload["output_path"] = str(target)

    if target is None:
        result = _run_gmail_bridge(payload, timeout_seconds=timeout_seconds)
    else:
        with _lock_paths(target):
            result = _run_gmail_bridge(payload, timeout_seconds=timeout_seconds)

    if result.get("status") == "error" and result.get("error"):
        return {**result, "error": _activity_redact_text(str(result["error"]), limit=2_000)}
    if action == "download_attachment" and result.get("status") == "saved":
        assert target is not None
        if not target.is_file():
            raise RuntimePolicyError("Gmail 附件保存验证失败", "bridge 返回 saved，但 media/gmail 目标文件不存在")
        actual_hash = _sha256_path(target)
        expected_hash = str(result.get("sha256") or "").strip().lower()
        if not expected_hash or actual_hash != expected_hash:
            raise RuntimePolicyError("Gmail 附件保存验证失败", "media/gmail 文件 SHA-256 与 Gmail bridge 返回值不一致")
        actual_size = target.stat().st_size
        returned_size = result.get("bytes_written")
        if isinstance(returned_size, int) and returned_size != actual_size:
            raise RuntimePolicyError("Gmail 附件保存验证失败", "media/gmail 文件大小与 Gmail bridge 返回值不一致")
        result = {**result, "path": str(target), "bytes_written": actual_size, "sha256": actual_hash}
    return result


@mcp.tool(annotations=COMMAND_ANNOTATIONS)
def run_powershell(
    command: str,
    cwd: str | None = None,
    shell: Literal["powershell", "cmd"] = "powershell",
    timeout_seconds: float = 60,
    max_output_chars: int = 30_000,
    workspace_lease_id: str | None = None,
) -> dict[str, Any]:
    """统一 Windows shell 入口；默认 PowerShell，批处理/cmd 用 shell='cmd'。npm、Python、Node、Git 的简单命令优先对应专用工具。"""
    if not command.strip():
        raise ValueError("command 不能为空")
    working_directory = _normalise_cwd(cwd)
    if shell == "powershell":
        argv: list[str] | str = _powershell_argv(command)
    else:
        comspec = os.environ.get("COMSPEC", "cmd.exe")
        argv = f'"{comspec}" /d /s /c {command}'
    result = _shell_result(
        argv,
        cwd=working_directory,
        timeout_seconds=timeout_seconds,
        max_output_chars=max_output_chars,
    )
    result["shell"] = shell
    routing_hint = _dedicated_command_routing_hint(command)
    if routing_hint:
        result["routing_hint"] = routing_hint
    return result


def run_cmd(
    command: str,
    cwd: str | None = None,
    timeout_seconds: float = 60,
    max_output_chars: int = 30_000,
    workspace_lease_id: str | None = None,
) -> dict[str, Any]:
    """只在明确需要 cmd.exe/batch 语法时使用；一般 Windows shell/cmdlet 用 run_powershell，npm/npx/Python/Node/Git 常规操作用对应专用工具。"""
    if not command.strip():
        raise ValueError("command 不能为空")
    working_directory = _normalise_cwd(cwd)
    comspec = os.environ.get("COMSPEC", "cmd.exe")
    command_line = f'"{comspec}" /d /s /c {command}'
    result = _shell_result(
        command_line,
        cwd=working_directory,
        timeout_seconds=timeout_seconds,
        max_output_chars=max_output_chars,
    )
    routing_hint = _dedicated_command_routing_hint(command)
    if routing_hint:
        result["routing_hint"] = routing_hint
    return result


def _run_runtime(
    executable: str,
    *,
    args: list[str] | None,
    code: str | None,
    code_flag: str,
    cwd: str | None,
    timeout_seconds: float,
    max_output_chars: int,
) -> dict[str, Any]:
    if code is not None and args:
        raise ValueError("code 和 args 不能同时使用")
    resolved = shutil.which(executable)
    if resolved is None:
        raise FileNotFoundError(f"未找到可执行文件：{executable}")
    command = [resolved]
    if code is not None:
        command.extend([code_flag, code])
    else:
        command.extend(_command_args(args))
    return _run_argv(
        command,
        cwd=_normalise_cwd(cwd),
        timeout_seconds=timeout_seconds,
        max_output_chars=max_output_chars,
    )


@mcp.tool(annotations=COMMAND_ANNOTATIONS)
def run_python(
    args: list[str] | None = None,
    code: str | None = None,
    cwd: str | None = None,
    python_executable: str | None = None,
    timeout_seconds: float = 60,
    max_output_chars: int = 30_000,
    workspace_lease_id: str | None = None,
) -> dict[str, Any]:
    """短时 Python 脚本/module/pytest 或小段 Python 代码首选；项目任务应指定项目解释器。预计超过30秒的有限任务改用 start_job，不要再包一层 PowerShell/CMD。"""
    if code is not None and args:
        raise ValueError("code 和 args 不能同时使用")
    executable = sys.executable
    if python_executable is not None:
        requested = python_executable.strip()
        if not requested:
            raise ValueError("python_executable 不能为空")
        if Path(requested).is_absolute() or "\\" in requested or "/" in requested:
            candidate = _normalise_path(requested, must_exist=True)
            if not candidate.is_file():
                raise FileNotFoundError(f"Python 解释器不是普通文件：{candidate}")
            executable = str(candidate)
        else:
            resolved = shutil.which(requested)
            if resolved is None:
                raise FileNotFoundError(f"未找到 Python 解释器：{requested}")
            executable = resolved
    command = [executable]
    if code is not None:
        command.extend(["-c", code])
    else:
        command.extend(_command_args(args))
    return _run_argv(
        command,
        cwd=_normalise_cwd(cwd),
        timeout_seconds=timeout_seconds,
        max_output_chars=max_output_chars,
    )


@mcp.tool(annotations=COMMAND_ANNOTATIONS)
def run_node(
    args: list[str] | None = None,
    code: str | None = None,
    cwd: str | None = None,
    timeout_seconds: float = 60,
    max_output_chars: int = 30_000,
    workspace_lease_id: str | None = None,
) -> dict[str, Any]:
    """短时直接 Node.js 脚本或 JavaScript 代码首选；package script 用 run_npm，一次性包 CLI 用 run_npx，超过30秒的有限任务用 start_job。"""
    return _run_runtime(
        "node",
        args=args,
        code=code,
        code_flag="--eval",
        cwd=cwd,
        timeout_seconds=timeout_seconds,
        max_output_chars=max_output_chars,
    )


@mcp.tool(annotations=COMMAND_ANNOTATIONS)
def run_npm(
    args: list[str] | None = None,
    cwd: str | None = None,
    timeout_seconds: float = 300,
    max_output_chars: int = 30_000,
    workspace_lease_id: str | None = None,
) -> dict[str, Any]:
    """npm install/test/run/build 等短时 npm 子命令和 package scripts 首选；不要再包 PowerShell/CMD。预计超过30秒的有限任务直接用 start_job。"""
    executable = "npm.cmd" if os.name == "nt" else "npm"
    return _run_runtime(
        executable,
        args=args,
        code=None,
        code_flag="",
        cwd=cwd,
        timeout_seconds=timeout_seconds,
        max_output_chars=max_output_chars,
    )


@mcp.tool(annotations=COMMAND_ANNOTATIONS)
def run_npx(
    args: list[str] | None = None,
    cwd: str | None = None,
    timeout_seconds: float = 300,
    max_output_chars: int = 30_000,
    workspace_lease_id: str | None = None,
) -> dict[str, Any]:
    """通过 npx 调一次项目/包 CLI 时首选；package scripts 用 run_npm，直接 Node 脚本用 run_node。预计超过30秒的有限任务用 start_job。"""
    executable = "npx.cmd" if os.name == "nt" else "npx"
    return _run_runtime(
        executable,
        args=args,
        code=None,
        code_flag="",
        cwd=cwd,
        timeout_seconds=timeout_seconds,
        max_output_chars=max_output_chars,
    )

@mcp.tool(annotations=COMMAND_ANNOTATIONS)
def git_init(path: str, initial_branch: str = "main", bare: bool = False, timeout_seconds: float = 120, max_output_chars: int = 50_000, workspace_lease_id: str | None = None) -> dict[str, Any]:
    """初始化 Git 仓库；目标目录可不存在，但其父目录必须已经存在。"""
    target = _normalise_path(path)
    if target.exists() and not target.is_dir():
        raise NotADirectoryError(str(target))
    if not target.parent.is_dir():
        raise FileNotFoundError(f"父目录不存在：{target.parent}")
    args = ["git", "init"]
    if bare:
        args.append("--bare")
    if initial_branch.strip():
        args.extend(["--initial-branch", initial_branch.strip()])
    args.append(str(target))
    result = _run_argv(args, cwd=target.parent, timeout_seconds=timeout_seconds, max_output_chars=max_output_chars)
    result["repository"] = str(target)
    return result
@mcp.tool(annotations=COMMAND_ANNOTATIONS)
def git_clone(source: str, destination: str, branch: str | None = None, depth: int | None = None, timeout_seconds: float = 300, max_output_chars: int = 50_000, workspace_lease_id: str | None = None) -> dict[str, Any]:
    """克隆 Git 仓库到指定目录；支持本地路径和 Git 可访问的远程地址。"""
    if not source.strip():
        raise ValueError("source 不能为空")
    target = _normalise_path(destination)
    if not target.parent.is_dir():
        raise FileNotFoundError(f"目标父目录不存在：{target.parent}")
    args = ["git", "clone"]
    if branch:
        args.extend(["--branch", branch])
    if depth is not None:
        depth = _validate_limit(depth, "depth", 1_000_000)
        args.extend(["--depth", str(depth)])
    args.extend([source, str(target)])
    result = _run_argv(args, cwd=target.parent, timeout_seconds=timeout_seconds, max_output_chars=max_output_chars)
    result["repository"] = str(target)
    return result
@mcp.tool(annotations=COMMAND_ANNOTATIONS)
def git_fetch(repo_path: str, remote: str | None = None, prune: bool = False, tags: bool = False, timeout_seconds: float = 300, max_output_chars: int = 50_000, workspace_lease_id: str | None = None) -> dict[str, Any]:
    """获取 Git 远程更新但不自动合并到当前分支。"""
    args = ["fetch"]
    if prune:
        args.append("--prune")
    if tags:
        args.append("--tags")
    if remote:
        args.append(remote)
    return _git_run(repo_path, args, timeout_seconds=timeout_seconds, max_output_chars=max_output_chars)
@mcp.tool(annotations=READ_ANNOTATIONS)
def git_show(repo_path: str, revision: str = "HEAD", paths: list[str] | None = None, max_output_chars: int = 100_000, workspace_lease_id: str | None = None) -> dict[str, Any]:
    """显示某个 Git revision 的提交和内容，可按仓库相对路径筛选。"""
    revision = revision.strip()
    if not revision or revision.startswith("-"):
        raise ValueError("revision 无效")
    args = ["show", "--no-ext-diff", "--no-color", revision]
    if paths:
        args.append("--")
        args.extend(_command_args(paths))
    return _git_run(repo_path, args, max_output_chars=max_output_chars)


@mcp.tool(annotations=READ_ANNOTATIONS)
def git_rev_parse(repo_path: str, revision: str = "HEAD", max_output_chars: int = 20_000, workspace_lease_id: str | None = None) -> dict[str, Any]:
    """解析 Git revision，常用于确认当前 HEAD、分支或提交对象的真实哈希。"""
    revision = revision.strip()
    if not revision or revision.startswith("-"):
        raise ValueError("revision 无效")
    return _git_run(repo_path, ["rev-parse", "--verify", revision], max_output_chars=max_output_chars)

@mcp.tool(annotations=COMMAND_ANNOTATIONS)
def git_remote(
    repo_path: str,
    action: Literal["list", "add", "remove", "set-url"] = "list",
    name: str | None = None,
    url: str | None = None,
    timeout_seconds: float = 120,
    max_output_chars: int = 50_000,
    workspace_lease_id: str | None = None,
) -> dict[str, Any]:
    """查看或修改 Git remote；list 返回远程名称和 URL，其他操作需要明确名称与目标。"""
    if action == "list":
        return _git_run(
            repo_path,
            ["remote", "--verbose"],
            timeout_seconds=timeout_seconds,
            max_output_chars=max_output_chars,
        )
    if not name or not name.strip():
        raise ValueError("add/remove/set-url 必须提供 name")
    remote_name = name.strip()
    if remote_name.startswith("-"):
        raise ValueError("name 无效")
    if action == "remove":
        args = ["remote", "remove", remote_name]
    else:
        if not url or not url.strip():
            raise ValueError("add/set-url 必须提供 url")
        remote_url = url.strip()
        if action == "add":
            args = ["remote", "add", remote_name, remote_url]
        else:
            args = ["remote", "set-url", remote_name, remote_url]
    return _git_run(
        repo_path,
        args,
        timeout_seconds=timeout_seconds,
        max_output_chars=max_output_chars,
    )


@mcp.tool(annotations=READ_ANNOTATIONS)
def git_status(repo_path: str, max_output_chars: int = 50_000, workspace_lease_id: str | None = None) -> dict[str, Any]:
    """获取 Git 仓库当前分支和工作区状态。"""
    return _git_run(repo_path, ["status", "--short", "--branch"], max_output_chars=max_output_chars)


@mcp.tool(annotations=READ_ANNOTATIONS)
def git_diff(
    repo_path: str,
    cached: bool = False,
    paths: list[str] | None = None,
    max_output_chars: int = 50_000,
    workspace_lease_id: str | None = None,
) -> dict[str, Any]:
    """获取工作区或暂存区差异，可按仓库相对路径筛选。"""
    args = ["diff", "--no-ext-diff", "--no-color"]
    if cached:
        args.append("--cached")
    args.append("--")
    args.extend(_command_args(paths))
    return _git_run(repo_path, args, max_output_chars=max_output_chars)


@mcp.tool(annotations=READ_ANNOTATIONS)
def git_log(
    repo_path: str,
    max_count: int = 20,
    paths: list[str] | None = None,
    oneline: bool = True,
    max_output_chars: int = 50_000,
    workspace_lease_id: str | None = None,
) -> dict[str, Any]:
    """获取 Git 提交历史，可按仓库相对路径筛选。"""
    max_count = _validate_limit(max_count, "max_count", 1_000)
    format_arg = "--oneline" if oneline else "--format=fuller"
    args = ["log", format_arg, f"-n{max_count}", "--no-color", "--"]
    args.extend(_command_args(paths))
    return _git_run(repo_path, args, max_output_chars=max_output_chars)


@mcp.tool(annotations=DESTRUCTIVE_ANNOTATIONS)
def git_add(
    repo_path: str,
    paths: list[str] | None = None,
    all_changes: bool = False,
    max_output_chars: int = 50_000,
    workspace_lease_id: str | None = None,
) -> dict[str, Any]:
    """将指定路径或全部变化加入 Git 暂存区。"""
    if not all_changes and not paths:
        raise ValueError("请提供 paths，或明确设置 all_changes=true")
    args = ["add"]
    if all_changes:
        args.append("--all")
    else:
        args.append("--")
        args.extend(_command_args(paths))
    return _git_run(repo_path, args, max_output_chars=max_output_chars)


@mcp.tool(annotations=DESTRUCTIVE_ANNOTATIONS)
def git_commit(
    repo_path: str,
    message: str,
    allow_empty: bool = False,
    max_output_chars: int = 50_000,
    workspace_lease_id: str | None = None,
) -> dict[str, Any]:
    """创建 Git 提交；不会修改全局 Git 配置。"""
    if not message.strip():
        raise ValueError("提交说明不能为空")
    args = ["commit", "-m", message]
    if allow_empty:
        args.append("--allow-empty")
    return _git_run(repo_path, args, max_output_chars=max_output_chars)


@mcp.tool(annotations=COMMAND_ANNOTATIONS)
def git_pull(
    repo_path: str,
    remote: str | None = None,
    branch: str | None = None,
    rebase: bool = False,
    ff_only: bool = True,
    timeout_seconds: float = 300,
    max_output_chars: int = 50_000,
    workspace_lease_id: str | None = None,
) -> dict[str, Any]:
    """从远程拉取 Git 更新；默认使用 fast-forward only，避免自动制造合并提交。"""
    args = ["pull"]
    if rebase:
        args.append("--rebase")
    elif ff_only:
        args.append("--ff-only")
    if remote:
        args.append(remote)
    if branch:
        args.append(branch)
    return _git_run(repo_path, args, timeout_seconds=timeout_seconds, max_output_chars=max_output_chars)


@mcp.tool(annotations=COMMAND_ANNOTATIONS)
def git_push(
    repo_path: str,
    remote: str | None = None,
    branch: str | None = None,
    force_with_lease: bool = False,
    set_upstream: bool = False,
    timeout_seconds: float = 300,
    max_output_chars: int = 50_000,
    workspace_lease_id: str | None = None,
) -> dict[str, Any]:
    """推送 Git 提交；如需覆盖远程历史，只允许使用更安全的 force-with-lease。"""
    args = ["push"]
    if force_with_lease:
        args.append("--force-with-lease")
    if set_upstream:
        args.append("--set-upstream")
    if remote:
        args.append(remote)
    if branch:
        args.append(branch)
    return _git_run(repo_path, args, timeout_seconds=timeout_seconds, max_output_chars=max_output_chars)


@mcp.tool(annotations=DESTRUCTIVE_ANNOTATIONS)
def git_branch(
    repo_path: str,
    action: Literal["list", "create", "delete"] = "list",
    name: str | None = None,
    force_delete: bool = False,
    max_output_chars: int = 50_000,
    workspace_lease_id: str | None = None,
) -> dict[str, Any]:
    """列出、创建或删除 Git 分支。"""
    if action == "list":
        args = ["branch", "--all", "--verbose", "--no-color"]
    else:
        if not name:
            raise ValueError("create 或 delete 必须提供 name")
        args = ["branch"]
        if action == "delete":
            args.append("-D" if force_delete else "-d")
        args.extend([name])
    return _git_run(repo_path, args, max_output_chars=max_output_chars)


@mcp.tool(annotations=DESTRUCTIVE_ANNOTATIONS)
def git_switch(
    repo_path: str,
    branch: str,
    create: bool = False,
    discard_changes: bool = False,
    max_output_chars: int = 50_000,
    workspace_lease_id: str | None = None,
) -> dict[str, Any]:
    """切换 Git 分支；可以明确创建新分支或丢弃工作区变化。"""
    if not branch.strip():
        raise ValueError("branch 不能为空")
    args = ["switch"]
    if create:
        args.append("--create")
    if discard_changes:
        args.append("--discard-changes")
    args.append(branch)
    return _git_run(repo_path, args, max_output_chars=max_output_chars)


@mcp.tool(annotations=DESTRUCTIVE_ANNOTATIONS)
def git_restore(
    repo_path: str,
    paths: list[str] | None = None,
    source: str | None = None,
    staged: bool = False,
    worktree: bool = True,
    max_output_chars: int = 50_000,
    workspace_lease_id: str | None = None,
) -> dict[str, Any]:
    """恢复 Git 文件；默认恢复工作区，必须明确提供目标范围或确认使用全部路径。"""
    if not staged and not worktree:
        raise ValueError("staged 和 worktree 至少一个为 true")
    args = ["restore"]
    if source:
        args.append(f"--source={source}")
    if staged:
        args.append("--staged")
    if worktree:
        args.append("--worktree")
    args.append("--")
    args.extend(_command_args(paths) if paths else ["."])
    return _git_run(repo_path, args, max_output_chars=max_output_chars)

@mcp.tool(annotations=READ_ANNOTATIONS)
def wait_process(pid: int, timeout_seconds: float = 60) -> dict[str, Any]:
    """等待指定进程退出；超时时只报告仍在运行，不会终止进程。"""
    if pid <= 0:
        raise ValueError("pid 必须大于 0")
    if timeout_seconds <= 0:
        raise ValueError("timeout_seconds 必须大于 0")
    started = time.perf_counter()
    try:
        process = psutil.Process(pid)
    except psutil.NoSuchProcess:
        return {"pid": pid, "status": "gone", "timed_out": False, "exit_code": None, "waited_seconds": 0.0}
    try:
        code = process.wait(timeout=min(float(timeout_seconds), 24 * 60 * 60))
    except psutil.TimeoutExpired:
        return {"pid": pid, "status": "running", "timed_out": True, "exit_code": None, "waited_seconds": round(time.perf_counter() - started, 3)}
    return {"pid": pid, "status": "exited", "timed_out": False, "exit_code": code, "waited_seconds": round(time.perf_counter() - started, 3)}
@mcp.tool(annotations=OPEN_READ_ANNOTATIONS)
def http_probe(url: str, method: Literal["GET", "HEAD"] = "GET", timeout_seconds: float = 10, max_body_chars: int = 20_000, max_body_bytes: int = 1_000_000) -> dict[str, Any]:
    """对 HTTP/HTTPS 地址做一次健康探测，返回状态码、少量响应头、响应正文和耗时。"""
    parsed = urllib.parse.urlsplit(url)
    if parsed.scheme.lower() not in {"http", "https"} or not parsed.netloc:
        raise ValueError("url 必须是完整的 http:// 或 https:// 地址")
    if timeout_seconds <= 0:
        raise ValueError("timeout_seconds 必须大于 0")
    max_body_bytes = _validate_limit(max_body_bytes, "max_body_bytes", 16 * 1024 * 1024)
    request = urllib.request.Request(url, method=method, headers={"User-Agent": "GPT-PC/0.1.0"})
    started = time.perf_counter()
    def result(response: Any, ok: bool, error: str | None = None) -> dict[str, Any]:
        raw = b"" if method == "HEAD" else response.read(max_body_bytes + 1)
        body_cut = len(raw) > max_body_bytes
        raw = raw[:max_body_bytes]
        charset = response.headers.get_content_charset() if hasattr(response.headers, "get_content_charset") else None
        body, char_cut = _limit_text(raw.decode(charset or "utf-8", errors="replace"), max_body_chars)
        headers = {k.lower(): response.headers.get(k) for k in ("Content-Type", "Content-Length", "Location", "Server", "Date") if response.headers.get(k) is not None}
        return {"url": url, "final_url": response.geturl(), "method": method, "ok": ok, "status_code": int(response.status), "headers": headers, "body": body, "body_truncated": body_cut or char_cut, "elapsed_seconds": round(time.perf_counter() - started, 3), "error": error}
    try:
        with urllib.request.urlopen(request, timeout=min(float(timeout_seconds), 60.0)) as response:
            return result(response, 200 <= int(response.status) < 400)
    except urllib.error.HTTPError as exc:
        return result(exc, False, str(exc))
    except urllib.error.URLError as exc:
        return {"url": url, "method": method, "ok": False, "status_code": None, "headers": {}, "body": "", "body_truncated": False, "elapsed_seconds": round(time.perf_counter() - started, 3), "error": str(exc)}

@mcp.tool(annotations=COMMAND_ANNOTATIONS)
def start_process(
    command: str,
    cwd: str | None = None,
    shell: Literal["direct", "cmd", "powershell"] = "direct",
    wait: bool = False,
    timeout_seconds: float = 60,
    max_output_chars: int = 30_000,
    log_file: str | None = None,
    workspace_lease_id: str | None = None,
) -> dict[str, Any]:
    """长期服务/dev server、需要自定义日志或明确进程生命周期控制时首选；普通短命令用对应 runner，超过30秒但最终会结束的 build/test/train/install 优先 start_job。"""
    if not command.strip():
        raise ValueError("command 不能为空")
    working_directory = _normalise_cwd(cwd)
    if shell == "powershell":
        encoded = base64.b64encode(command.encode("utf-16le")).decode("ascii")
        argv = ["powershell.exe", "-NoLogo", "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass", "-EncodedCommand", encoded]
    elif shell == "cmd":
        comspec = os.environ.get("COMSPEC", "cmd.exe")
        argv = f'"{comspec}" /d /s /c {command}'
    else:
        argv = command
    if wait:
        result = _run_argv(
            argv,
            cwd=working_directory,
            timeout_seconds=timeout_seconds,
            max_output_chars=max_output_chars,
        )
        result["started"] = result.get("exit_code") is not None
        return result
    lease_targets = [working_directory]
    output_path: Path | None = None
    if log_file:
        output_path = _normalise_path(log_file)
        lease_targets.append(output_path)
    lease = _require_execute_lease(workspace_lease_id, lease_targets)
    managed = _create_job(
        workspace_lease=lease,
        argv=argv,
        cwd=working_directory,
        shell=shell,
        name="托管进程",
        log_path=output_path,
    )
    return {"started": True, "command": argv, **managed}


def _background_argv(command: str, shell: Literal["direct", "cmd", "powershell"]) -> list[str] | str:
    if shell == "powershell":
        return _powershell_argv(command)
    if shell == "cmd":
        comspec = os.environ.get("COMSPEC", "cmd.exe")
        return f'"{comspec}" /d /s /c {command}'
    return command


@mcp.tool(annotations=COMMAND_ANNOTATIONS)
def start_job(
    command: str,
    cwd: str,
    shell: Literal["direct", "cmd", "powershell"] = "direct",
    name: str | None = None,
    workspace_lease_id: str | None = None,
) -> dict[str, Any]:
    """预计超过30秒且最终会结束的 build/test/train/install/migration 等有限任务首选；立即返回并用 get_job/tail_job/cancel_job 跟踪。长期服务/dev server 优先 start_process。"""
    if not command.strip():
        raise ValueError("command 不能为空")
    working_directory = _normalise_cwd(cwd)
    lease = _require_execute_lease(workspace_lease_id, [working_directory])
    return _create_job(
        workspace_lease=lease,
        argv=_background_argv(command, shell),
        cwd=working_directory,
        shell=shell,
        name=name,
    )


@mcp.tool(annotations=READ_ANNOTATIONS)
def get_job(job_id: str) -> dict[str, Any]:
    """读取一个后台任务的当前存活状态、目录和日志位置。"""
    if not job_id.strip():
        raise ValueError("job_id 不能为空")
    return _get_job(job_id)


@mcp.tool(annotations=READ_ANNOTATIONS)
def list_jobs(workspace: str | None = None) -> dict[str, Any]:
    """列出当前或指定工作区的后台任务，不会停止或修改任务。"""
    target = _normalise_path(workspace, must_exist=True) if workspace else None
    return _list_jobs(target)


@mcp.tool(annotations=READ_ANNOTATIONS)
def tail_job(job_id: str, max_bytes: int = 64 * 1024) -> dict[str, Any]:
    """读取后台任务日志的末尾；大任务无需长时间占用一次 MCP 调用。"""
    if not job_id.strip():
        raise ValueError("job_id 不能为空")
    return _read_job_tail(job_id, max_bytes=max_bytes)


@mcp.tool(annotations=DESTRUCTIVE_ANNOTATIONS)
def cancel_job(
    job_id: str,
    workspace_lease_id: str | None = None,
    force: bool = False,
) -> dict[str, Any]:
    """停止属于当前执行工作区租约的后台任务及其子进程。"""
    if not job_id.strip():
        raise ValueError("job_id 不能为空")
    if not workspace_lease_id:
        raise RuntimePolicyError("需要工作区租约", "停止后台任务前必须提供执行工作区租约")
    return _cancel_job(job_id, workspace_lease_id=workspace_lease_id, force=force)


@mcp.tool(annotations=DESTRUCTIVE_ANNOTATIONS)
def stop_process(
    pid: int,
    force: bool = False,
    include_children: bool = True,
    workspace_lease_id: str | None = None,
) -> dict[str, Any]:
    """停止指定进程，默认先优雅终止；可选择强制终止并处理子进程。"""
    if pid <= 0:
        raise ValueError("pid 必须大于 0")
    if pid == os.getpid():
        raise ValueError("不能停止当前 MCP 服务进程")
    job = _job_for_pid(pid)
    if job is None:
        raise RuntimePolicyError(
            "任务进程身份变化",
            "后台任务已经退出、PID 已被复用或旧任务缺少可验证的进程身份；拒绝终止该 PID",
        )
    process = psutil.Process(pid)
    try:
        expected_create_time = float(job["process_create_time"])
        actual_create_time = float(process.create_time())
    except (KeyError, TypeError, ValueError, psutil.NoSuchProcess, psutil.AccessDenied) as exc:
        raise RuntimePolicyError(
            "任务进程身份变化",
            "无法再次验证后台任务进程身份；拒绝终止该 PID",
        ) from exc
    if abs(actual_create_time - expected_create_time) > 0.01:
        raise RuntimePolicyError(
            "任务进程身份变化",
            "PID 已指向其他进程；拒绝终止该 PID",
        )
    children = process.children(recursive=True) if include_children else []
    targets = children + [process]
    requested: list[int] = []
    for item in reversed(targets):
        try:
            if force:
                item.kill()
            else:
                item.terminate()
            requested.append(item.pid)
        except (psutil.NoSuchProcess, psutil.AccessDenied):
            pass
    gone, alive = psutil.wait_procs(targets, timeout=5)
    if force:
        for item in alive:
            try:
                item.kill()
            except (psutil.NoSuchProcess, psutil.AccessDenied):
                pass
    return {
        "status": "stopped" if not alive else "stop_requested",
        "pid": pid,
        "requested": requested,
        "gone": [item.pid for item in gone],
        "alive": [item.pid for item in alive],
        "force": force,
    }


_DISCONNECT_ALERT_STOP = threading.Event()
_DISCONNECT_ALERT_THREAD: threading.Thread | None = None


def _ultra_team_for_root(lease_id: str):
    try:
        manager = _get_chatgpt_agent_manager()
        if not manager.config.ultra_collaboration:
            return None, None
        teams = manager._get_ultra_coordinator().teams
        return teams, teams.team_for_root(lease_id)
    except Exception:
        return None, None


def _notify_ultra_root_disconnect(lease_id: str, confirmation: dict[str, Any]) -> None:
    teams, team = _ultra_team_for_root(lease_id)
    if not teams or not team:
        return
    result = teams.notify_root_disconnect(team["team_id"], lease_id, confirmation)
    if result.get("notified"):
        _get_chatgpt_agent_manager()._notify()
    LOGGER.info(
        "ultra_root_disconnect_rescue_window team=%s lease=%s notified=%s deadline=%s",
        team["team_id"], lease_id, result.get("notified", 0), confirmation.get("rescue_deadline_at"),
    )


def _ultra_root_rescue_in_progress(lease_id: str) -> bool:
    teams, team = _ultra_team_for_root(lease_id)
    return bool(teams and team and teams.active_root_rescue(team["team_id"]))


def _require_ultra_team_finalized_before_root_release(lease_id: str) -> dict | None:
    teams, team = _ultra_team_for_root(lease_id)
    if not teams or not team:
        return None
    completion = teams.completion(team["team_id"])
    if not completion or completion.get("root_lease_id") != lease_id:
        raise RuntimePolicyError(
            "Ultra Team 尚未 finalize",
            "当前 lease 是 Ultra Team Root；必须先由 Root 调用 team_finalize，确认所有 Child/任务均已收口，再执行完成释放。",
            retryable=True,
            error_type="TEAM_FINALIZE_REQUIRED",
            details={"team_id": team["team_id"]},
        )
    return completion


def _duration_text(seconds: float) -> str:
    total = max(0, int(round(float(seconds or 0))))
    hours, remain = divmod(total, 3600)
    minutes, secs = divmod(remain, 60)
    if hours:
        return f"{hours}h {minutes}m {secs}s"
    if minutes:
        return f"{minutes}m {secs}s"
    return f"{secs}s"


def _completion_mail_content(completion: dict[str, Any]) -> tuple[str, str]:
    report = completion.get("report")
    if not isinstance(report, dict):
        report = json.loads(str(completion.get("report_json") or "{}"))
    task_name = str(completion.get("task_name") or "GPTAgent Ultra task")
    suffix = str(completion.get("completion_id") or "")[-8:]
    subject = f"[GPTAgent Ultra] 任务完成 · {task_name} · {suffix}"
    finished = datetime.fromtimestamp(
        float(report.get("completed_at_epoch") or completion.get("finalized_at") or time.time())
    ).astimezone()
    lines = [
        f"任务：{task_name}",
        f"完成时间：{finished.strftime('%Y-%m-%d %H:%M:%S %z')}",
        f"总耗时：{_duration_text(float(report.get('duration_seconds') or 0))}",
        f"子 Agent 数量：{int(report.get('child_agent_count') or 0)}",
        "",
        "Root 最终总结：",
        str(completion.get("summary") or ""),
    ]
    children = report.get("children") if isinstance(report.get("children"), list) else []
    if children:
        lines += ["", "子 Agent 明细："]
    for index, child in enumerate(children, 1):
        lines += [
            "",
            f"{index}. {child.get('display_name') or child.get('local_tag') or child.get('agent_id')}",
            f"   职责：{child.get('role') or ''}",
            f"   结果：{child.get('completion_summary') or child.get('state') or ''}",
        ]
        tasks = child.get("tasks") if isinstance(child.get("tasks"), list) else []
        for task in tasks:
            lines.append(
                f"   - {task.get('title') or task.get('task_id')}: "
                f"{task.get('status')} · {task.get('evidence') or ''}"
            )
    lines += ["", f"Completion ID：{completion.get('completion_id') or ''}"]
    return subject, "\n".join(lines)


def _process_ultra_completion_notifications() -> None:
    manager = _get_chatgpt_agent_manager()
    if not manager.config.ultra_collaboration:
        return
    teams = manager._get_ultra_coordinator().teams
    for pending in teams.pending_completions():
        root_lease_id = str(pending.get("root_lease_id") or "")
        if root_lease_id and _active_lease_record(root_lease_id) is not None:
            continue
        temp_lease = None
        claim = None
        try:
            recipient = str(pending.get("recipient") or os.environ.get("GPT_PC_ULTRA_COMPLETION_EMAIL", "")).strip()
            if not recipient:
                continue
            temp_lease = _acquire_workspace(
                DESKTOP_OPS_WORKSPACE,
                mode="execute_exclusive",
                ttl_seconds=120,
                owner_label="GPTAgent Ultra 完成邮件通知",
                thread_id=f"system-ultra-completion-{uuid.uuid4().hex}",
                controller_id=f"system:ultra-completion:{pending['completion_id']}",
                runtime_instance=MCP_INSTANCE_ID,
            )
            claim = teams.claim_completion_notification(pending["completion_id"])
            if claim.get("claim_status") != "claimed":
                continue
            subject, body = _completion_mail_content(claim)
            recipient = str(claim.get("recipient") or recipient).strip()
            expected_account = os.environ.get("GPT_PC_GMAIL_EXPECTED_ACCOUNT", "").strip()
            existing = _run_gmail_bridge(
                {
                    "action": "search",
                    "interactive_auth": False,
                    "query": f'subject:"{subject}"',
                    "max_results": 5,
                    "expected_account": expected_account,
                },
                timeout_seconds=30,
            )
            exact = next(
                (row for row in (existing.get("messages") or []) if str(row.get("subject") or "") == subject),
                None,
            )
            if exact:
                teams.finish_completion_notification(
                    pending["completion_id"], claim["attempt_token"],
                    success=True, gmail_message_id=str(exact.get("message_id") or ""),
                )
                continue
            sent = _run_gmail_bridge(
                {
                    "action": "send",
                    "interactive_auth": False,
                    "to": recipient,
                    "subject": subject,
                    "body": body,
                    "expected_account": expected_account,
                },
                timeout_seconds=45,
            )
            if sent.get("status") not in {"sent", "unverified"} or not sent.get("message_id"):
                raise RuntimeError(str(sent.get("error") or "Gmail completion send did not return message id"))
            teams.finish_completion_notification(
                pending["completion_id"], claim["attempt_token"],
                success=True, gmail_message_id=str(sent.get("message_id") or ""),
            )
            LOGGER.info(
                "ultra_completion_email_sent completion=%s message=%s",
                pending["completion_id"], sent.get("message_id"),
            )
        except Exception as exc:
            if claim and claim.get("claim_status") == "claimed":
                try:
                    teams.finish_completion_notification(
                        pending["completion_id"], claim["attempt_token"],
                        success=False, error=str(exc),
                    )
                except Exception:
                    pass
            LOGGER.warning(
                "ultra_completion_email_failed completion=%s error=%s",
                pending.get("completion_id"), exc,
            )
        finally:
            if temp_lease:
                try:
                    _release_workspace(str(temp_lease["lease_id"]))
                except Exception:
                    pass


def _disconnect_alert_candidate_ids(*, now: datetime | None = None) -> list[str]:
    """Controller-level two-stage liveness: 3m silence -> probe; +2m no reply -> alert."""
    current = now or datetime.now(timezone.utc)
    records = mode_runtime._lease_records_snapshot()
    execute_records = [
        record
        for _path, record in records
        if str(record.get("mode") or "") == "execute_exclusive"
        and str(record.get("lease_role") or "owner") != "participant"
        and re.fullmatch(r"[0-9a-f]{32}", str(record.get("lease_id") or ""))
        and not str(record.get("thread_id") or "").startswith(("system-disconnect-alert-", "gptagent-supervisor-"))
    ]
    active_ids = {str(record.get("lease_id") or "") for record in execute_records}
    _prune_lease_tool_activity(active_ids)
    groups: dict[str, list[dict[str, Any]]] = {}
    for record in execute_records:
        groups.setdefault(_liveness_thread_id(record), []).append(record)

    candidates: list[str] = []
    for _controller_id, members in groups.items():
        if not members:
            continue
        in_flight_count = sum(_active_lease_tool_count(str(item.get("lease_id") or "")) for item in members)
        pending = _pending_controller_liveness(members)

        # A probe survives only while the WHOLE controller is silent. Activity
        # on another lease of the same controller cancels the stale handle's
        # probe instead of declaring the GPT conversation disconnected.
        if pending:
            owner, probe = pending[0]
            owner_lease_id = str(owner.get("lease_id") or "")
            if not probe.get("delivered_at") or not probe.get("response_deadline_at"):
                # The background monitor can observe silence, but it cannot inject
                # a prompt into a model turn by itself.  Keep the probe queued
                # until a real MCP safe boundary calls _deliver_pending_thread_liveness;
                # only that boundary may start the explicit-reply clock.
                continue
            response_deadline = mode_runtime._mode_deadline(probe.get("response_deadline_at"))
            if not response_deadline or current < response_deadline:
                continue
            if in_flight_count > 0 or any(
                _active_lease_tool_count(str(item.get("lease_id") or "")) > 0 for item in members
            ):
                continue
            probe_id = str(probe.get("id") or "")
            teams, root_team = _ultra_team_for_root(owner_lease_id)
            if not teams or not root_team or str(root_team.get("state") or "") != "active":
                # Keep controller-level liveness/recovery local for ordinary
                # tasks. External disconnect mail is reserved for an active
                # Ultra Team Root, per the user notification policy.
                continue
            confirmation = mode_runtime.disconnect_confirmation_state(owner)
            if (
                not confirmation
                or str(confirmation.get("probe_id") or "") != probe_id
            ):
                confirmation = mode_runtime.confirm_disconnect_for_rescue(
                    owner_lease_id,
                    probe_id=probe_id,
                    rescue_grace_seconds=ROOT_RESCUE_GRACE_SECONDS,
                )
                if confirmation.get("status") == "confirmed":
                    _notify_ultra_root_disconnect(owner_lease_id, confirmation)
                continue
            rescue_deadline = mode_runtime._mode_deadline(confirmation.get("rescue_deadline_at"))
            if not rescue_deadline or current <= rescue_deadline:
                continue
            if _ultra_root_rescue_in_progress(owner_lease_id):
                continue
            if owner.get("disconnect_alert_sent_probe_id") == probe_id:
                continue
            if _disconnect_alert_launch_is_cooling_down(owner_lease_id, now=current):
                continue
            if owner.get("disconnect_alert_pending_probe_id") == probe_id:
                claimed_at = mode_runtime._mode_deadline(owner.get("disconnect_alert_claimed_at"))
                claim_timeout = max(120, DISCONNECT_ALERT_STALE_SECONDS)
                if claimed_at and (current - claimed_at).total_seconds() <= claim_timeout:
                    continue
            candidates.append(owner_lease_id)
            continue

        observations = [
            (activity, item)
            for item in members
            for activity in [_record_observable_activity(item)]
            if activity is not None
        ]
        if not observations:
            continue
        observable_activity, representative = max(observations, key=lambda pair: pair[0])
        if (current - observable_activity).total_seconds() <= DISCONNECT_ALERT_PROBE_AFTER_SECONDS:
            continue

        representative_id = str(representative.get("lease_id") or "")
        created = mode_runtime.ensure_disconnect_probe(
            representative_id,
            activity_at=observable_activity.isoformat().replace("+00:00", "Z"),
            response_seconds=DISCONNECT_ALERT_RESPONSE_SECONDS,
            # A monitor scan is not a model-delivery boundary.  Always queue the
            # probe first; _check_tool_control / poll / continuation delivers it
            # when the GPT actually reaches its next MCP safe boundary.
            defer_delivery=True,
        )
        probe = created.get("probe") or {}
        if created.get("status") == "created":
            LOGGER.info(
                "disconnect_liveness_probe_sent controller=%s lease=%s probe=%s activity_at=%s response_seconds=%s deferred=%s",
                _controller_id,
                representative_id,
                probe.get("id"),
                probe.get("activity_at"),
                DISCONNECT_ALERT_RESPONSE_SECONDS,
                bool(in_flight_count),
            )
            event_probe = {
                **probe,
                "owner_lease_id": representative_id,
                "reply_required": True,
                "priority": "system_liveness",
                "non_preemptive": True,
                "deferred_until_safe_boundary": True,
            }
            _activity_emit(
                "runtime.liveness_probe",
                lease=representative,
                phase="warning",
                text=(
                    "系统存活确认已排队：该 GPT controller 的全部权威 lease 已约 3 分钟没有可信活动。"
                    "单个 lease 静默只会被视为 stale handle；同 controller 其他 lease 的活动会取消此 probe。"
                    "Runtime 不会打断正在进行的模型推理；只有 GPT 到达下一 MCP 安全边界时才真正投递，"
                    "并从实际投递时刻起给约 2 分钟，用 report_progress 明确回复仍在工作。"
                ),
                probe=event_probe,
            )
    return candidates


def _launch_disconnect_notifier(lease_id: str) -> int:
    """Launch the short-lived notifier without registering a user Runtime Job."""
    if not DISCONNECT_ALERT_HELPER.is_file():
        raise FileNotFoundError(str(DISCONNECT_ALERT_HELPER))
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    creationflags = subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0
    handle = DISCONNECT_ALERT_LOG.open("ab")
    try:
        process = subprocess.Popen(
            [
                sys.executable,
                str(DISCONNECT_ALERT_HELPER),
                "--lease-id",
                lease_id,
                "--stale-seconds",
                str(DISCONNECT_ALERT_STALE_SECONDS),
            ],
            cwd=str(PROJECT_ROOT),
            stdin=subprocess.DEVNULL,
            stdout=handle,
            stderr=subprocess.STDOUT,
            creationflags=creationflags,
        )
    finally:
        handle.close()
    return int(process.pid)


def _disconnect_alert_monitor() -> None:
    LOGGER.info(
        "disconnect_alert_monitor_start probe_after_seconds=%s response_seconds=%s stale_seconds=%s scan_seconds=%s",
        DISCONNECT_ALERT_PROBE_AFTER_SECONDS,
        DISCONNECT_ALERT_RESPONSE_SECONDS,
        DISCONNECT_ALERT_STALE_SECONDS,
        DISCONNECT_ALERT_SCAN_SECONDS,
    )
    while not _DISCONNECT_ALERT_STOP.is_set():
        try:
            for lease_id in _disconnect_alert_candidate_ids():
                try:
                    pid = _launch_disconnect_notifier(lease_id)
                    _mark_disconnect_alert_launch(lease_id)
                    LOGGER.info("disconnect_alert_notifier_started lease=%s pid=%s", lease_id, pid)
                except (OSError, ValueError) as exc:
                    LOGGER.warning("disconnect_alert_notifier_start_failed lease=%s error=%s", lease_id, exc)
        except Exception as exc:  # monitor must never take down MCP serving
            LOGGER.exception("disconnect_alert_monitor_scan_failed error=%s", exc)
        try:
            _process_ultra_completion_notifications()
        except Exception as exc:
            LOGGER.exception("ultra_completion_monitor_failed error=%s", exc)
        _DISCONNECT_ALERT_STOP.wait(DISCONNECT_ALERT_SCAN_SECONDS)
    LOGGER.info("disconnect_alert_monitor_stop")


def _start_disconnect_alert_monitor() -> bool:
    global _DISCONNECT_ALERT_THREAD
    if _DISCONNECT_ALERT_THREAD is not None and _DISCONNECT_ALERT_THREAD.is_alive():
        return True
    if not DISCONNECT_ALERT_HELPER.is_file():
        LOGGER.warning("disconnect_alert_monitor_disabled helper_missing=%s", DISCONNECT_ALERT_HELPER)
        return False
    _DISCONNECT_ALERT_STOP.clear()
    _DISCONNECT_ALERT_THREAD = threading.Thread(
        target=_disconnect_alert_monitor,
        name="gpt-pc-disconnect-alert-monitor",
        daemon=True,
    )
    _DISCONNECT_ALERT_THREAD.start()
    return True


def _stop_disconnect_alert_monitor() -> None:
    global _DISCONNECT_ALERT_THREAD
    _DISCONNECT_ALERT_STOP.set()
    thread = _DISCONNECT_ALERT_THREAD
    if thread is not None and thread.is_alive():
        thread.join(timeout=2.0)
    _DISCONNECT_ALERT_THREAD = None


def _install_termination_audit() -> None:
    def on_signal(signum: int, _frame: Any) -> None:
        LOGGER.warning(
            "runtime_termination_signal instance=%s signal=%s pid=%s parent_pid=%s",
            MCP_INSTANCE_ID,
            signum,
            os.getpid(),
            os.getppid(),
        )
        raise SystemExit(128 + signum)

    for name in ("SIGINT", "SIGBREAK", "SIGTERM"):
        candidate = getattr(signal, name, None)
        if candidate is not None:
            try:
                signal.signal(candidate, on_signal)
            except (OSError, RuntimeError, ValueError):
                pass


def main() -> None:
    parser = argparse.ArgumentParser(description="GPT-PC 本机 MCP 服务")
    parser.add_argument(
        "--transport",
        choices=("stdio", "streamable-http"),
        default=os.environ.get("GPT_PC_MCP_TRANSPORT", "stdio"),
    )
    parser.add_argument("--host", default=os.environ.get("GPT_PC_MCP_HOST", "127.0.0.1"))
    parser.add_argument("--port", type=int, default=int(os.environ.get("GPT_PC_MCP_PORT", "18765")))
    parser.add_argument("--path", default=os.environ.get("GPT_PC_MCP_PATH", "/mcp"))
    options = parser.parse_args()
    _install_termination_audit()
    logging.getLogger("uvicorn.access").disabled = True
    logging.getLogger("uvicorn.access").propagate = False
    LOGGER.info(
        "runtime_start instance=%s transport=%s pid=%s parent_pid=%s host=%s port=%s path=%s",
        MCP_INSTANCE_ID,
        options.transport,
        os.getpid(),
        os.getppid(),
        options.host,
        options.port,
        options.path,
    )
    pid_file = RUNTIME_DIR / "mcp-server.pid"
    agent_manager: AgentManager | None = None
    if options.transport == "streamable-http":
        RUNTIME_DIR.mkdir(parents=True, exist_ok=True)
        pid_file.write_text(str(os.getpid()), encoding="ascii")
        _start_disconnect_alert_monitor()
        # Migrate old supervision permission only. The web GPT now performs
        # individual browser actions; startup creates no browser/model loop.
        agent_manager = _get_chatgpt_agent_manager()
        agent_manager.start_background_services()
    try:
        if options.transport == "streamable-http":
            mcp.run(
                transport="streamable-http",
                host=options.host,
                port=options.port,
                streamable_http_path=options.path,
            )
        else:
            mcp.run(transport="stdio")
    finally:
        if options.transport == "streamable-http":
            if agent_manager is not None:
                try:
                    agent_manager.stop_background_services()
                except Exception:
                    LOGGER.exception("gptagent_background_stop_failed")
            _stop_disconnect_alert_monitor()
            try:
                if pid_file.read_text(encoding="ascii").strip() == str(os.getpid()):
                    pid_file.unlink(missing_ok=True)
            except OSError:
                pass
        LOGGER.warning(
            "runtime_exit instance=%s pid=%s parent_pid=%s",
            MCP_INSTANCE_ID,
            os.getpid(),
            os.getppid(),
        )


if __name__ == "__main__":
    main()
