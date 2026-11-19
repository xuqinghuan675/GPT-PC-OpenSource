"""Persistent runtime primitives for GPT-PC.

This module deliberately has no MCP dependency.  It stores short-lived
workspace leases and background-job metadata outside user repositories so a
server restart can reconnect to already-running child processes.
"""

from __future__ import annotations

from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
import hashlib
import json
import os
from pathlib import Path
import subprocess
import threading
import time
from typing import Any, Iterator, Literal
import uuid

import psutil


PROJECT_ROOT = Path(os.environ.get("GPT_PC_HOME") or Path(__file__).resolve().parent.parent).expanduser().resolve()
MEDIA_ROOT = PROJECT_ROOT / "media"
MEDIA_UPLOAD_DIR = MEDIA_ROOT / "uploads"
MEDIA_GMAIL_DIR = MEDIA_ROOT / "gmail"
MEDIA_SCREENSHOT_DIR = MEDIA_ROOT / "screenshots"
MEDIA_REFERENCE_DIR = MEDIA_ROOT / "references"
MEDIA_ATTACHMENT_DIR = MEDIA_ROOT / "attachments"
MEDIA_OUTBOX_DIR = MEDIA_ROOT / "outbox"
RUNTIME_DIR = PROJECT_ROOT / "runtime"
LEASE_DIR = RUNTIME_DIR / "leases"
CONTROLLER_STATE_DIR = RUNTIME_DIR / "controllers"
JOB_DIR = RUNTIME_DIR / "jobs"
JOB_LOG_DIR = PROJECT_ROOT / "logs" / "jobs"
METADATA_LOCK_PATH = RUNTIME_DIR / ".metadata.lock"
DEFAULT_LEASE_SECONDS = 2 * 60 * 60
MAX_LEASE_SECONDS = 24 * 60 * 60
VISUAL_OBSERVATION_METADATA_WINDOW = 128
LOCK_WAIT_SECONDS = 5.0
STALE_LOCK_SECONDS = 120.0
PROCESS_CREATE_TIME_TOLERANCE_SECONDS = 0.01
LEGACY_JOB_START_TOLERANCE_SECONDS = 5.0
DESKTOP_OPS_WORKSPACE = PROJECT_ROOT / "workspaces" / "desktop-ops"
CONSOLE_RELAY_WORKSPACE = PROJECT_ROOT / "workspaces" / "console-relay"
RUNTIME_MAINTENANCE_WORKSPACE = RUNTIME_DIR / "maintenance-workspace"
LEASE_TYPE_WORKSPACE = "workspace"
LEASE_TYPE_DESKTOP_OPS = "desktop_ops"
LEASE_TYPE_CONSOLE_RELAY = "console_relay"
LEASE_TYPE_RUNTIME_ADMIN = "runtime_admin"
LEASE_ROLE_OWNER = "owner"
LEASE_ROLE_PARTICIPANT = "participant"

# Fast-path index: historical Job JSON remains authoritative on disk, while
# normal lifecycle checks only revisit the small set of jobs that were active
# at startup or created during this Runtime. Values keep the record mtime so
# external/legacy metadata edits are detected without rescanning history.
_ACTIVE_JOB_INDEX: dict[str, tuple[dict[str, Any], int]] = {}
_ACTIVE_JOB_INDEX_SOURCE: str | None = None

# Same-process MCP worker threads should block on a cheap in-memory lock instead
# of seeing the cross-process lock file and entering 50 ms polling sleeps. The
# file lock remains authoritative for coordination with other processes.
_METADATA_THREAD_LOCK = threading.RLock()
_CHECKPOINT_OVERLAY_LOCK = threading.RLock()
_CHECKPOINT_OVERLAYS: dict[str, dict[str, Any]] = {}
_COALESCED_CHECKPOINT_TOOLS = frozenset(
    {
        "list_directory", "list_directory_tree", "file_info", "read_text_file",
        "read_text_file_with_hash", "read_text_tail", "read_text_range",
        "read_multiple_files", "read_binary_file", "search_files", "search_text",
        "code_navigate", "calculate_hash", "git_status", "git_diff", "git_log",
        "git_show", "git_rev_parse",
    }
)

_PROTECTED_RUNTIME_ROOTS = tuple(
    PROJECT_ROOT / name
    for name in (
        "config",
        "logs",
        "media",
        "pc-mcp",
        "pc-mcp.venv",
        "powershell7",
        "runtime",
        "scripts",
        "tunnel",
    )
)


class RuntimePolicyError(RuntimeError):
    """A model-readable policy result, not a server fault."""

    def __init__(
        self,
        category: str,
        message: str,
        *,
        retryable: bool = False,
        error_type: str | None = None,
        details: dict[str, Any] | None = None,
    ):
        super().__init__(message)
        self.category = category
        self.retryable = retryable
        self.error_type = error_type or category
        self.details = dict(details or {})


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def utc_iso(value: datetime | None = None) -> str:
    return (value or utc_now()).isoformat().replace("+00:00", "Z")


def _parse_utc(value: str) -> datetime:
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


def _path_is_within(path: Path, root: Path) -> bool:
    try:
        path.resolve(strict=False).relative_to(root.resolve(strict=False))
        return True
    except ValueError:
        return False


def _paths_overlap(first: Path, second: Path) -> bool:
    return _path_is_within(first, second) or _path_is_within(second, first)


def _ensure_runtime_dirs() -> None:
    for directory in (
        RUNTIME_DIR,
        LEASE_DIR,
        JOB_DIR,
        JOB_LOG_DIR,
        MEDIA_ROOT,
        MEDIA_UPLOAD_DIR,
        MEDIA_GMAIL_DIR,
        MEDIA_SCREENSHOT_DIR,
        MEDIA_REFERENCE_DIR,
        MEDIA_ATTACHMENT_DIR,
        MEDIA_OUTBOX_DIR,
        DESKTOP_OPS_WORKSPACE,
        CONSOLE_RELAY_WORKSPACE,
        RUNTIME_MAINTENANCE_WORKSPACE,
    ):
        directory.mkdir(parents=True, exist_ok=True)


# The two lease anchors must exist before MCP validates acquire_workspace(path, must_exist=True).
_ensure_runtime_dirs()


@contextmanager
def _cross_process_metadata_lock() -> Iterator[None]:
    """A tiny cross-process lock for JSON metadata updates on Windows."""

    _ensure_runtime_dirs()
    deadline = time.monotonic() + LOCK_WAIT_SECONDS
    descriptor: int | None = None
    while descriptor is None:
        try:
            descriptor = os.open(
                METADATA_LOCK_PATH,
                os.O_CREAT | os.O_EXCL | os.O_WRONLY,
            )
            os.write(descriptor, f"pid={os.getpid()} at={utc_iso()}\n".encode("utf-8"))
        except FileExistsError:
            try:
                age = time.time() - METADATA_LOCK_PATH.stat().st_mtime
                if age > STALE_LOCK_SECONDS:
                    METADATA_LOCK_PATH.unlink(missing_ok=True)
                    continue
            except OSError:
                pass
            if time.monotonic() >= deadline:
                raise RuntimePolicyError(
                    "运行时繁忙",
                    "运行时元数据正在被另一项操作更新，请稍后重试",
                    retryable=True,
                )
            time.sleep(0.05)
    try:
        yield
    finally:
        try:
            os.close(descriptor)
        finally:
            try:
                METADATA_LOCK_PATH.unlink(missing_ok=True)
            except OSError:
                pass


@contextmanager
def _metadata_lock() -> Iterator[None]:
    """Serialize local threads cheaply, then take the cross-process file lock."""
    with _METADATA_THREAD_LOCK:
        with _cross_process_metadata_lock():
            yield


def _read_json(path: Path) -> dict[str, Any] | None:
    try:
        raw = path.read_text(encoding="utf-8")
        parsed = json.loads(raw)
    except (OSError, json.JSONDecodeError, UnicodeDecodeError):
        return None
    return parsed if isinstance(parsed, dict) else None


def _write_json(path: Path, value: dict[str, Any]) -> None:
    if path.parent == LEASE_DIR and "expires_at" in value:
        _enforce_mode_deadlines(value)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
    try:
        temporary.write_text(
            json.dumps(value, ensure_ascii=False, sort_keys=True, indent=2),
            encoding="utf-8",
        )
        os.replace(temporary, path)
    finally:
        try:
            temporary.unlink(missing_ok=True)
        except OSError:
            pass
    if (
        path.parent == LEASE_DIR
        and "expires_at" in value
        and str(value.get("lease_role") or "") != LEASE_ROLE_PARTICIPANT
        and not value.get("shared_root_lease_id")
    ):
        _write_controller_envelope(value)


def _lease_path(lease_id: str) -> Path:
    return LEASE_DIR / f"lease-{lease_id}.json"


def _is_lease_participant(record: dict[str, Any] | None) -> bool:
    return bool(record and record.get("lease_role") == LEASE_ROLE_PARTICIPANT and record.get("shared_root_lease_id"))


def _normalise_controller_id(value: str | None) -> str:
    text = str(value or "").strip()
    if not text:
        return ""
    if len(text) > 160 or any(
        not (character.isascii() and (character.isalnum() or character in "._:-"))
        for character in text
    ):
        raise RuntimePolicyError(
            "参数无效",
            "controller_id 最多 160 个 ASCII 字母数字或 . _ : - 字符",
        )
    return text


def lease_controller_id(record: dict[str, Any] | None) -> str:
    """Runtime execution-controller key, not a guaranteed ChatGPT conversation identity.

    Equality is useful positive evidence that two owner handles belong to the
    same local controller episode. Inequality is not proof that they came from
    different ChatGPT conversations: a later visible turn may rebuild its
    Runtime handle without re-supplying the prior controller_id. GPTAgent
    participants instead fall back to logical_agent_id so rotating participant
    handles do not create false disconnect identities.
    """
    if not record:
        return ""
    explicit = str(record.get("controller_id") or "").strip()
    if explicit:
        return explicit
    if _is_lease_participant(record):
        logical_agent_id = str(record.get("logical_agent_id") or "").strip()
        if logical_agent_id:
            return "gptagent:" + logical_agent_id
    thread_id = str(record.get("thread_id") or "").strip()
    return ("thread:" + thread_id) if thread_id else str(record.get("lease_id") or "")


def _normalise_web_conversation_id(value: str | None) -> str:
    text = str(value or "").strip()
    if not text:
        return ""
    if len(text) > 128 or any(
        not (ch.isascii() and (ch.isalnum() or ch in "-_"))
        for ch in text
    ):
        raise RuntimePolicyError(
            "参数无效",
            "conversation_id 最多 128 个 ASCII 字母数字或 - _ 字符",
            error_type="CONVERSATION_ID_INVALID",
        )
    return text


def _bind_web_conversation_locked(
    record: dict[str, Any],
    *,
    conversation_id: str | None,
    conversation_url: str | None = None,
    source: str | None = None,
) -> bool:
    clean_id = _normalise_web_conversation_id(conversation_id)
    if not clean_id:
        return False
    existing = str(record.get("web_conversation_id") or "").strip()
    if existing and existing != clean_id:
        raise RuntimePolicyError(
            "网页会话身份冲突",
            "该 Root lease 已绑定另一个 ChatGPT conversation_id；拒绝静默改绑。",
            error_type="WEB_CONVERSATION_IDENTITY_CONFLICT",
            details={"existing_conversation_id": existing, "requested_conversation_id": clean_id},
        )
    clean_url = str(conversation_url or "").strip()
    if clean_url and (
        len(clean_url) > 512
        or clean_id not in clean_url
        or not clean_url.startswith("https://chatgpt.com/")
    ):
        raise RuntimePolicyError(
            "参数无效",
            "conversation_url 必须是包含 conversation_id 的 chatgpt.com HTTPS URL",
            error_type="CONVERSATION_URL_INVALID",
        )
    clean_source = str(source or "").strip()[:160]
    changed = (
        existing != clean_id
        or (clean_url and str(record.get("web_conversation_url") or "") != clean_url)
        or (clean_source and str(record.get("web_conversation_source") or "") != clean_source)
    )
    record["web_conversation_id"] = clean_id
    if clean_url:
        record["web_conversation_url"] = clean_url
    if clean_source:
        record["web_conversation_source"] = clean_source
    if changed:
        record["web_conversation_bound_at"] = utc_iso()
    return changed


def _controller_state_path(controller_id: str) -> Path:
    clean = str(controller_id or "").strip()
    digest = hashlib.sha256(clean.encode("utf-8")).hexdigest()
    return CONTROLLER_STATE_DIR / f"controller-{digest}.json"


def _write_controller_envelope(record: dict[str, Any], *, closed_at: str | None = None) -> None:
    """Persist public recovery state independently from one ephemeral owner lease."""
    if _is_lease_participant(record):
        return
    controller_id = lease_controller_id(record)
    if not controller_id:
        return
    path = _controller_state_path(controller_id)
    old = _read_json(path) or {}
    execution = {
        key: json.loads(json.dumps(value))
        for key, value in record.items()
        if key.startswith("execution_")
    }
    payload = {
        "schema_version": 1,
        "controller_id": controller_id,
        "thread_id": record.get("thread_id"),
        "workspace": record.get("workspace"),
        "scope_root": record.get("scope_root"),
        "lease_type": record.get("lease_type", LEASE_TYPE_WORKSPACE),
        "last_lease_id": record.get("lease_id"),
        "owner_label": record.get("owner_label"),
        "web_conversation_id": record.get("web_conversation_id"),
        "web_conversation_url": record.get("web_conversation_url"),
        "web_conversation_source": record.get("web_conversation_source"),
        "web_conversation_bound_at": record.get("web_conversation_bound_at"),
        "runtime_instance": record.get("runtime_instance"),
        "task_started_at": (
            old.get("task_started_at")
            or record.get("controller_task_started_at")
            or record.get("created_at")
        ),
        "workspace_validation_revision": int(record.get("workspace_validation_revision") or 0),
        "goal_mode": (
            json.loads(json.dumps(record.get("goal_mode")))
            if isinstance(record.get("goal_mode"), dict) else None
        ),
        "ultra_mode": (
            json.loads(json.dumps(record.get("ultra_mode")))
            if isinstance(record.get("ultra_mode"), dict) else None
        ),
        "visual_mode": (
            json.loads(json.dumps(record.get("visual_mode")))
            if isinstance(record.get("visual_mode"), dict) else None
        ),
        "execution": execution,
        "updated_at": utc_iso(),
        "closed_at": closed_at,
    }
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
    try:
        temporary.write_text(
            json.dumps(payload, ensure_ascii=False, sort_keys=True, indent=2),
            encoding="utf-8",
        )
        os.replace(temporary, path)
    finally:
        try:
            temporary.unlink(missing_ok=True)
        except OSError:
            pass


def _hydrate_controller_envelope(record: dict[str, Any]) -> dict[str, Any]:
    """Recover unfinished Goal/progress state after the owner handle disappeared."""
    controller_id = lease_controller_id(record)
    if not controller_id:
        return record
    envelope = _read_json(_controller_state_path(controller_id))
    if not envelope or envelope.get("closed_at"):
        return record
    if (
        envelope.get("controller_id") != controller_id
        or envelope.get("thread_id") != record.get("thread_id")
        or envelope.get("workspace") != record.get("workspace")
        or envelope.get("lease_type", LEASE_TYPE_WORKSPACE)
            != record.get("lease_type", LEASE_TYPE_WORKSPACE)
    ):
        return record
    for key in ("goal_mode", "ultra_mode", "visual_mode"):
        value = envelope.get(key)
        if isinstance(value, dict):
            record[key] = json.loads(json.dumps(value))
    for key in ("web_conversation_id", "web_conversation_url", "web_conversation_source", "web_conversation_bound_at"):
        value = envelope.get(key)
        if value:
            record[key] = value
    record["workspace_validation_revision"] = int(envelope.get("workspace_validation_revision") or 0)
    for key, value in (envelope.get("execution") or {}).items():
        if isinstance(key, str) and key.startswith("execution_"):
            record[key] = json.loads(json.dumps(value))
    record["controller_task_started_at"] = envelope.get("task_started_at") or record.get("created_at")
    record["controller_recovered_from_envelope"] = True
    record["controller_envelope_updated_at"] = envelope.get("updated_at")
    record["recovered_from_lease_id"] = envelope.get("last_lease_id")
    return record


def _resource_lease_id(record: dict[str, Any]) -> str:
    """Return the one authoritative resource lease for an owner/participant handle."""
    if _is_lease_participant(record):
        return str(record.get("shared_root_lease_id") or "")
    return str(record.get("lease_id") or "")


def _shared_root_record_locked(record: dict[str, Any]) -> dict[str, Any]:
    """Resolve a lightweight conversation participant to its resource owner lease."""
    if not _is_lease_participant(record):
        return record
    root_id = str(record.get("shared_root_lease_id") or "")
    root = _read_mode_lease_record_locked(root_id)
    if root is None or _is_lease_participant(root):
        raise RuntimePolicyError(
            "共享租约失效",
            "GPTAgent participant 对应的 Root workspace lease 已不存在；不得继续使用旧 participant handle",
            retryable=True,
            error_type="SHARED_ROOT_LEASE_MISSING",
        )
    try:
        if _parse_utc(str(root["expires_at"])) <= utc_now():
            raise RuntimePolicyError(
                "共享租约已过期",
                "GPTAgent participant 对应的 Root workspace lease 已过期",
                retryable=True,
                error_type="SHARED_ROOT_LEASE_EXPIRED",
            )
    except (KeyError, TypeError, ValueError) as exc:
        raise RuntimePolicyError("共享租约无效", "Root workspace lease 记录不完整") from exc
    return root


def _participant_view(record: dict[str, Any], root: dict[str, Any]) -> dict[str, Any]:
    """Overlay family-scoped execution policy while preserving conversation-local identity/checkpoint."""
    merged = dict(record)
    for key in (
        "expires_at", "renewed_at", "stay_started_at", "stay_until_at", "stay_duration_seconds",
        "visual_mode", "goal_mode", "ultra_mode", "workspace_validation_revision",
    ):
        if key in root:
            merged[key] = json.loads(json.dumps(root[key])) if isinstance(root[key], (dict, list)) else root[key]
        else:
            merged.pop(key, None)
    merged["shared_root_lease_id"] = str(root.get("lease_id") or "")
    merged["lease_role"] = LEASE_ROLE_PARTICIPANT
    merged["resource_lease_owner"] = False
    return merged


def _normalise_participant_write_paths(
    root: dict[str, Any],
    write_paths: list[str] | None,
) -> list[str] | None:
    """Return an explicit hard mutation scope for a shared-lease participant."""
    if write_paths is None:
        return None
    if not isinstance(write_paths, list) or len(write_paths) > 64:
        raise RuntimePolicyError("参数无效", "participant write_paths 必须是不超过 64 项的列表")
    workspace = Path(str(root.get("workspace") or "")).resolve(strict=False)
    scope_root = Path(str(root.get("scope_root") or root.get("workspace") or "")).resolve(strict=False)
    result: list[str] = []
    for raw in write_paths:
        if not isinstance(raw, str) or not raw.strip() or "\x00" in raw:
            raise RuntimePolicyError("参数无效", "participant write_paths 含无效路径")
        path = Path(raw)
        resolved = (path if path.is_absolute() else workspace / path).resolve(strict=False)
        if not _path_is_within(resolved, scope_root):
            raise RuntimePolicyError(
                "participant 写入范围越界",
                f"participant 只能获得 Root scope 内的写入范围：{resolved}",
                error_type="PARTICIPANT_WRITE_SCOPE_OUTSIDE_ROOT",
            )
        result.append(str(resolved))
    return sorted(set(result))


def _participant_ancestor_ids_locked(record: dict[str, Any]) -> set[str]:
    """Resolve participant ancestors without ever treating the Root as a participant."""
    ancestors: set[str] = set()
    parent_id = str(record.get("shared_parent_lease_id") or "")
    root_id = str(record.get("shared_root_lease_id") or "")
    while parent_id and parent_id != root_id and parent_id not in ancestors:
        parent = _read_mode_lease_record_locked(parent_id)
        if parent is None or not _is_lease_participant(parent):
            break
        ancestors.add(parent_id)
        parent_id = str(parent.get("shared_parent_lease_id") or "")
    return ancestors


def _enforce_shared_participant_mutation_scope_locked(
    record: dict[str, Any],
    targets: list[Path],
) -> None:
    """Hard-stop cross-agent mutations inside one shared resource lease."""
    if not targets:
        return
    if _is_lease_participant(record):
        scopes = record.get("participant_write_paths")
        if scopes is not None:
            allowed = [Path(str(path)).resolve(strict=False) for path in scopes]
            for target in targets:
                normalized = target.resolve(strict=False)
                if not any(_path_is_within(normalized, scope) for scope in allowed):
                    raise RuntimePolicyError(
                        "participant 写入越界",
                        f"当前 Child 只允许修改已委派 write_paths，目标未获授权：{normalized}",
                        error_type="PARTICIPANT_WRITE_SCOPE_VIOLATION",
                        details={"participant_lease_id": record.get("lease_id"), "allowed_write_paths": list(scopes)},
                    )
        current_id = str(record.get("lease_id") or "")
        root_id = str(record.get("shared_root_lease_id") or "")
        for _path, other in _lease_records_locked():
            if not _is_lease_participant(other) or str(other.get("shared_root_lease_id") or "") != root_id:
                continue
            if current_id not in _participant_ancestor_ids_locked(other):
                continue
            delegated = other.get("participant_write_paths")
            if delegated is None:
                continue
            for target in targets:
                normalized = target.resolve(strict=False)
                if any(_paths_overlap(normalized, Path(str(path)).resolve(strict=False)) for path in delegated):
                    raise RuntimePolicyError(
                        "下级 Child 正在持有写入范围",
                        "父 participant 不能在仍由活跃后代 Child 持有的 write_paths 内并发修改",
                        retryable=True,
                        error_type="PARTICIPANT_RESOURCE_OWNERSHIP_CONFLICT",
                        details={"conflicting_participant_lease_id": other.get("lease_id"), "target": str(normalized)},
                    )
        return

    root_id = str(record.get("lease_id") or "")
    for _path, participant in _lease_records_locked():
        if not _is_lease_participant(participant):
            continue
        if str(participant.get("shared_root_lease_id") or "") != root_id:
            continue
        delegated = participant.get("participant_write_paths")
        if delegated is None:
            continue
        for target in targets:
            normalized = target.resolve(strict=False)
            if any(_paths_overlap(normalized, Path(str(path)).resolve(strict=False)) for path in delegated):
                raise RuntimePolicyError(
                    "Child 正在持有写入范围",
                    "Root 不能在活跃 participant 已委派的 write_paths 内并发修改；先等待该 Child 退出或调整任务边界",
                    retryable=True,
                    error_type="PARTICIPANT_RESOURCE_OWNERSHIP_CONFLICT",
                    details={"conflicting_participant_lease_id": participant.get("lease_id"), "target": str(normalized)},
                )


def _job_path(job_id: str) -> Path:
    return JOB_DIR / f"job-{job_id}.json"


def _read_mode_lease_record_locked(lease_id: str) -> dict[str, Any] | None:
    """Recover legacy short-expiry records while holding the existing metadata lock."""
    path = _lease_path(lease_id)
    record = _read_json(path)
    if record is not None and _enforce_mode_deadlines(record):
        _write_json(path, record)
        _sync_visual_manifest_locked(record)
    return record


def _lease_records_locked() -> list[tuple[Path, dict[str, Any]]]:
    now = utc_now()
    active: list[tuple[Path, dict[str, Any]]] = []
    for path in LEASE_DIR.glob("lease-*.json"):
        record = _read_json(path)
        if record is None:
            continue
        try:
            if _is_lease_participant(record):
                # Participant lifetime follows the shared Root.  Its cached
                # expires_at is informational only and may lag after Root /stay.
                record = _participant_view(record, _shared_root_record_locked(record))
                expired = False
            else:
                if _enforce_mode_deadlines(record):
                    _write_json(path, record)
                expired = _parse_utc(str(record["expires_at"])) <= now
        except (KeyError, TypeError, ValueError, RuntimePolicyError):
            expired = True
        if expired:
            try:
                path.unlink(missing_ok=True)
            except OSError:
                pass
            continue
        active.append((path, record))
    return active


def _merge_checkpoint_overlay(record: dict[str, Any]) -> dict[str, Any]:
    lease_id = str(record.get("lease_id") or "")
    if not lease_id:
        return record
    with _CHECKPOINT_OVERLAY_LOCK:
        overlay = _CHECKPOINT_OVERLAYS.get(lease_id)
        return {**record, **overlay} if overlay else record


def _repair_snapshot_mode_deadlines(path: Path, record: dict[str, Any]) -> dict[str, Any]:
    """Persist only the rare legacy mode/expiry repair discovered by a snapshot."""
    if not _enforce_mode_deadlines(record):
        return record
    with _metadata_lock():
        current = _read_json(path)
        if current is None:
            return record
        if _enforce_mode_deadlines(current):
            _write_json(path, current)
        return current


def _lease_records_snapshot() -> list[tuple[Path, dict[str, Any]]]:
    """Read active lease metadata without taking the cross-process write lock.

    Lease files are replaced atomically, so readers may safely observe either the
    previous or next complete JSON document. Normal status/activity reads remain
    lock-free; only a detected legacy mode/expiry invariant repair takes the
    writer lock and persists the repair. Participant handles are included only
    while their shared Root resource lease is active; their own cached expiry is
    never authoritative.
    """
    now = utc_now()
    active: list[tuple[Path, dict[str, Any]]] = []
    for path in LEASE_DIR.glob("lease-*.json"):
        record = _read_json(path)
        if record is None:
            continue
        try:
            if _is_lease_participant(record):
                root_id = str(record.get("shared_root_lease_id") or "")
                root_path = _lease_path(root_id)
                root = _read_json(root_path)
                if root is None or _is_lease_participant(root):
                    continue
                root = _repair_snapshot_mode_deadlines(root_path, root)
                if _parse_utc(str(root["expires_at"])) <= now:
                    continue
                record = _participant_view(record, root)
            else:
                record = _repair_snapshot_mode_deadlines(path, record)
                if _parse_utc(str(record["expires_at"])) <= now:
                    continue
        except (KeyError, TypeError, ValueError):
            continue
        active.append((path, _merge_checkpoint_overlay(record)))
    return active


def active_lease_record(lease_id: str) -> dict[str, Any] | None:
    """Return one active lease through an atomic read-only snapshot path."""
    path = _lease_path(lease_id)
    record = _read_json(path)
    if record is None:
        return None
    try:
        if _is_lease_participant(record):
            root_id = str(record.get("shared_root_lease_id") or "")
            root_path = _lease_path(root_id)
            root = _read_json(root_path)
            if root is None or _is_lease_participant(root):
                return None
            root = _repair_snapshot_mode_deadlines(root_path, root)
            if _parse_utc(str(root["expires_at"])) <= utc_now():
                return None
            record = _participant_view(record, root)
        else:
            record = _repair_snapshot_mode_deadlines(path, record)
            if _parse_utc(str(record["expires_at"])) <= utc_now():
                return None
    except (KeyError, TypeError, ValueError):
        return None
    return _merge_checkpoint_overlay(record)


def attach_workspace_participant(
    source_lease_id: str,
    *,
    logical_agent_id: str,
    owner_label: str,
    write_paths: list[str] | None = None,
    session_generation: int = 1,
) -> dict[str, Any]:
    """Attach one GPTAgent Conversation identity to an existing resource lease.

    This creates a lightweight Runtime participant handle only; it does not
    acquire another workspace lock. The source may itself be a participant,
    allowing nested Child agents to stay in the same resource-lease family.
    """
    clean_agent = str(logical_agent_id or "").strip()
    clean_owner = str(owner_label or "GPTAgent Child").strip()[:240] or "GPTAgent Child"
    if not clean_agent or len(clean_agent) > 160:
        raise RuntimePolicyError("参数无效", "logical_agent_id 无效")
    if type(session_generation) is not int or session_generation < 1:
        raise RuntimePolicyError("参数无效", "session_generation 必须是正整数")
    with _metadata_lock():
        source = _read_mode_lease_record_locked(source_lease_id)
        if source is None:
            raise RuntimePolicyError("租约不存在", "父 Agent 的共享 workspace lease 已失效", retryable=True)
        root = _shared_root_record_locked(source)
        # Owner handles do not take the participant-to-root expiry path above.
        # Recheck under the metadata lock after potentially slow browser proof;
        # otherwise a lease that expired during join can mint a dangling handle.
        try:
            expired = _parse_utc(str(root["expires_at"])) <= utc_now()
        except (KeyError, TypeError, ValueError) as exc:
            raise RuntimePolicyError("共享租约无效", "Root workspace lease 记录不完整") from exc
        if expired:
            raise RuntimePolicyError("共享租约已过期", "Root workspace lease 已过期，不能创建 participant",
                                     retryable=True, error_type="SHARED_ROOT_LEASE_EXPIRED")
        if root.get("mode") != "execute_exclusive" or root.get("lease_type") == LEASE_TYPE_CONSOLE_RELAY:
            raise RuntimePolicyError("租约只读", "GPTAgent Child 只能加入 execute_exclusive 资源租约")
        root_id = str(root.get("lease_id") or "")
        scoped_paths = _normalise_participant_write_paths(root, write_paths)
        for _path, existing in _lease_records_locked():
            if (
                _is_lease_participant(existing)
                and str(existing.get("shared_root_lease_id") or "") == root_id
                and str(existing.get("logical_agent_id") or "") == clean_agent
            ):
                existing_generation = int(existing.get("session_generation") or 1)
                if existing_generation != session_generation:
                    raise RuntimePolicyError(
                        "participant generation 冲突",
                        "同一 logical Agent 的旧 participant 属于另一 session generation；必须走显式恢复/换代，不能静默复用",
                        error_type="PARTICIPANT_SESSION_GENERATION_CONFLICT",
                        details={
                            "existing_generation": existing_generation,
                            "requested_generation": session_generation,
                        },
                    )
                if scoped_paths is not None:
                    old_scope = existing.get("participant_write_paths")
                    if old_scope is None:
                        existing["participant_write_paths"] = scoped_paths
                        _write_json(_lease_path(str(existing.get("lease_id") or "")), existing)
                    elif sorted(str(p) for p in old_scope) != scoped_paths:
                        raise RuntimePolicyError(
                            "participant 写入范围冲突",
                            "同一 logical Agent 已绑定不同 participant write_paths；拒绝静默扩大或改写权限",
                            error_type="PARTICIPANT_WRITE_SCOPE_CONFLICT",
                        )
                return {**_participant_view(existing, root), "reused_existing": True}
        if scoped_paths is not None:
            ancestor_ids = _participant_ancestor_ids_locked(source) if _is_lease_participant(source) else set()
            if _is_lease_participant(source):
                ancestor_ids.add(str(source.get("lease_id") or source_lease_id))
            for _path, existing in _lease_records_locked():
                if not _is_lease_participant(existing):
                    continue
                if str(existing.get("shared_root_lease_id") or "") != root_id:
                    continue
                if str(existing.get("lease_id") or "") in ancestor_ids:
                    continue
                existing_paths = existing.get("participant_write_paths")
                if existing_paths is None:
                    continue
                if any(
                    _paths_overlap(Path(a), Path(b))
                    for a in scoped_paths
                    for b in existing_paths
                ):
                    raise RuntimePolicyError(
                        "participant 写入范围冲突",
                        "并行兄弟 participant 的硬写入范围不得重叠；请重新拆分任务或等待原 participant 退出",
                        retryable=True,
                        error_type="PARTICIPANT_WRITE_SCOPE_CONFLICT",
                        details={"conflicting_participant_lease_id": existing.get("lease_id")},
                    )
        now = utc_now()
        participant_id = uuid.uuid4().hex
        record = {
            "lease_id": participant_id,
            "thread_id": f"thread-{uuid.uuid4().hex}",
            "workspace": root.get("workspace"),
            "scope_root": root.get("scope_root") or root.get("workspace"),
            "lease_type": root.get("lease_type", LEASE_TYPE_WORKSPACE),
            "mode": root.get("mode"),
            "owner_label": clean_owner,
            "created_at": utc_iso(now),
            "renewed_at": root.get("renewed_at") or utc_iso(now),
            "expires_at": root.get("expires_at"),
            "runtime_instance": root.get("runtime_instance"),
            "lease_role": LEASE_ROLE_PARTICIPANT,
            "shared_root_lease_id": root_id,
            "shared_parent_lease_id": str(source.get("lease_id") or source_lease_id),
            "logical_agent_id": clean_agent,
            "controller_id": "gptagent:" + clean_agent,
            "session_generation": session_generation,
        }
        if scoped_paths is not None:
            record["participant_write_paths"] = scoped_paths
        _write_json(_lease_path(participant_id), record)
        return {**_participant_view(record, root), "reused_existing": False}


def rotate_workspace_participant(
    source_lease_id: str,
    *,
    old_participant_id: str | None,
    logical_agent_id: str,
    owner_label: str,
    write_paths: list[str] | None,
    expected_generation: int,
    next_generation: int,
) -> dict[str, Any]:
    """Atomically replace one stale GPTAgent participant without touching the Root resource lease."""
    clean_agent = str(logical_agent_id or "").strip()
    clean_owner = str(owner_label or "GPTAgent Child").strip()[:240] or "GPTAgent Child"
    if not clean_agent or len(clean_agent) > 160:
        raise RuntimePolicyError("参数无效", "logical_agent_id 无效")
    if type(expected_generation) is not int or expected_generation < 1:
        raise RuntimePolicyError("参数无效", "expected_generation 必须是正整数")
    if type(next_generation) is not int or next_generation < expected_generation:
        raise RuntimePolicyError("参数无效", "next_generation 不能小于 expected_generation")
    old_id = str(old_participant_id or "").strip()
    with _metadata_lock():
        source = _read_mode_lease_record_locked(source_lease_id)
        if source is None:
            raise RuntimePolicyError("租约不存在", "恢复发起者的共享 workspace lease 已失效", retryable=True)
        root = _shared_root_record_locked(source)
        root_id = str(root.get("lease_id") or "")
        if root.get("mode") != "execute_exclusive" or root.get("lease_type") == LEASE_TYPE_CONSOLE_RELAY:
            raise RuntimePolicyError("租约只读", "只能在 execute_exclusive Root resource lease 中恢复 participant")
        try:
            if _parse_utc(str(root["expires_at"])) <= utc_now():
                raise RuntimePolicyError("共享租约已过期", "Root workspace lease 已过期，不能恢复 Child",
                                         retryable=True, error_type="SHARED_ROOT_LEASE_EXPIRED")
        except KeyError as exc:
            raise RuntimePolicyError("共享租约无效", "Root workspace lease 记录不完整") from exc

        old = _read_mode_lease_record_locked(old_id) if old_id else None
        if old is not None:
            if (not _is_lease_participant(old)
                    or str(old.get("shared_root_lease_id") or "") != root_id
                    or str(old.get("logical_agent_id") or "") != clean_agent):
                raise RuntimePolicyError(
                    "恢复身份不匹配",
                    "old_participant_id 不属于当前 Root/logical Agent；拒绝抢占其他 Child",
                    error_type="PARTICIPANT_RECOVERY_IDENTITY_MISMATCH",
                )
            old_generation = int(old.get("session_generation") or 1)
            if old_generation != expected_generation:
                raise RuntimePolicyError(
                    "恢复 generation 已过期",
                    "旧 participant 的 session generation 已变化；重新读取 Team 状态后再恢复",
                    retryable=True,
                    error_type="PARTICIPANT_SESSION_GENERATION_CONFLICT",
                    details={"existing_generation": old_generation, "expected_generation": expected_generation},
                )
            running_jobs = [
                job for job in _running_job_records_locked()
                if str(job.get("lease_id") or "") == old_id
            ]
            if running_jobs:
                raise RuntimePolicyError(
                    "Child 后台任务仍在运行",
                    "旧 participant 仍有后台任务；不能把一个仍在本机执行的 Child 判定为断线",
                    retryable=True,
                    error_type="PARTICIPANT_JOBS_ACTIVE",
                )

        scoped_paths = _normalise_participant_write_paths(root, write_paths)
        for _path, existing in _lease_records_locked():
            if not _is_lease_participant(existing):
                continue
            if str(existing.get("shared_root_lease_id") or "") != root_id:
                continue
            existing_id = str(existing.get("lease_id") or "")
            if old_id and existing_id == old_id:
                continue
            if str(existing.get("logical_agent_id") or "") == clean_agent:
                generation = int(existing.get("session_generation") or 1)
                if generation == next_generation:
                    return {**_participant_view(existing, root), "reused_existing": True}
                raise RuntimePolicyError(
                    "participant 恢复冲突",
                    "同一 logical Agent 已有另一活动 participant；拒绝创建第二个活动身份",
                    retryable=True,
                    error_type="PARTICIPANT_RECOVERY_CONFLICT",
                    details={"existing_participant_lease_id": existing_id, "session_generation": generation},
                )
            existing_paths = existing.get("participant_write_paths")
            if scoped_paths is None or existing_paths is None:
                continue
            if any(_paths_overlap(Path(a), Path(b)) for a in scoped_paths for b in existing_paths):
                raise RuntimePolicyError(
                    "participant 写入范围冲突",
                    "恢复后的 participant 会与仍活跃兄弟的硬写入范围重叠",
                    retryable=True,
                    error_type="PARTICIPANT_WRITE_SCOPE_CONFLICT",
                    details={"conflicting_participant_lease_id": existing_id},
                )

        if old_id and old is not None:
            _lease_path(old_id).unlink(missing_ok=True)
            with _CHECKPOINT_OVERLAY_LOCK:
                _CHECKPOINT_OVERLAYS.pop(old_id, None)

        now = utc_now()
        participant_id = uuid.uuid4().hex
        record = {
            "lease_id": participant_id,
            "thread_id": f"thread-{uuid.uuid4().hex}",
            "workspace": root.get("workspace"),
            "scope_root": root.get("scope_root") or root.get("workspace"),
            "lease_type": root.get("lease_type", LEASE_TYPE_WORKSPACE),
            "mode": root.get("mode"),
            "owner_label": clean_owner,
            "created_at": utc_iso(now),
            "renewed_at": root.get("renewed_at") or utc_iso(now),
            "expires_at": root.get("expires_at"),
            "runtime_instance": root.get("runtime_instance"),
            "lease_role": LEASE_ROLE_PARTICIPANT,
            "shared_root_lease_id": root_id,
            "shared_parent_lease_id": str(source.get("lease_id") or source_lease_id),
            "logical_agent_id": clean_agent,
            "controller_id": "gptagent:" + clean_agent,
            "session_generation": next_generation,
            "recovered_from_participant_id": old_id or None,
        }
        if scoped_paths is not None:
            record["participant_write_paths"] = scoped_paths
        _write_json(_lease_path(participant_id), record)
        return {**_participant_view(record, root), "reused_existing": False, "rotated_from": old_id or None}


def promote_workspace_participant_to_owner(
    root_lease_id: str,
    participant_lease_id: str,
    *,
    reason: str,
    owner_label: str | None = None,
) -> dict[str, Any]:
    """Promote one verified Ultra participant into the next Root owner after confirmed Root disconnect."""
    clean_reason = str(reason or "").strip()[:500]
    if not clean_reason:
        raise RuntimePolicyError("接班原因缺失", "Root rescue 必须记录公开原因")
    with _metadata_lock():
        root = _read_mode_lease_record_locked(root_lease_id)
        participant = _read_mode_lease_record_locked(participant_lease_id)
        if root is None or _is_lease_participant(root):
            raise RuntimePolicyError("Root 已变化", "待接班 Root lease 已不存在或不是 owner", error_type="ROOT_RESCUE_STALE")
        if participant is None or not _is_lease_participant(participant):
            raise RuntimePolicyError("接班身份无效", "只有当前 Ultra participant 可以接班 Root", error_type="ROOT_RESCUE_PARTICIPANT_REQUIRED")
        if str(participant.get("shared_root_lease_id") or "") != root_lease_id:
            raise RuntimePolicyError("接班身份越界", "participant 不属于待恢复 Root", error_type="ROOT_RESCUE_SCOPE_MISMATCH")
        confirmation = disconnect_confirmation_state(root)
        if not confirmation:
            raise RuntimePolicyError("Root 尚未确认掉线", "必须先完成 Root liveness 确认，不能抢健康 Root", error_type="ROOT_DISCONNECT_NOT_CONFIRMED")
        running_root_jobs = [
            job for job in _running_job_records_locked()
            if str(job.get("lease_id") or "") == root_lease_id
        ]
        if running_root_jobs:
            raise RuntimePolicyError(
                "Root 后台任务仍在运行",
                "确认掉线后仍检测到 Root-owned Runtime Job；先收口再接班",
                retryable=True,
                error_type="ROOT_RESCUE_JOBS_ACTIVE",
            )

        now = utc_now()
        new_root = dict(root)
        new_root.update({
            "lease_id": participant_lease_id,
            "thread_id": participant.get("thread_id"),
            "controller_id": participant.get("controller_id") or ("gptagent:" + str(participant.get("logical_agent_id") or "")),
            "owner_label": str(owner_label or ("Ultra rescued Root " + str(participant.get("logical_agent_id") or ""))).strip()[:240],
            "created_at": utc_iso(now),
            "renewed_at": utc_iso(now),
            "supersedes_lease_id": root_lease_id,
            "root_rescued_at": utc_iso(now),
            "root_rescue_reason": clean_reason,
            "root_rescued_from_participant_id": participant_lease_id,
        })
        for key in (
            "lease_role", "shared_root_lease_id", "shared_parent_lease_id",
            "logical_agent_id", "session_generation", "participant_write_paths",
            "disconnect_probe_id", "disconnect_probe_activity_at", "disconnect_probe_sent_at",
            "disconnect_probe_delivered_at", "disconnect_probe_delivered_via_lease_id",
            "disconnect_probe_response_deadline_at", "disconnect_probe_acked_at",
            "disconnect_probe_acked_via_lease_id", "disconnect_alert_pending_probe_id",
            "disconnect_alert_claimed_at", "disconnect_alert_retry_after_at",
            "disconnect_confirmed_at", "disconnect_confirmed_probe_id",
            "disconnect_rescue_deadline_at",
        ):
            new_root.pop(key, None)

        for path, other in _lease_records_locked():
            if not _is_lease_participant(other):
                continue
            if str(other.get("shared_root_lease_id") or "") != root_lease_id:
                continue
            other_id = str(other.get("lease_id") or "")
            if other_id == participant_lease_id:
                continue
            other["shared_root_lease_id"] = participant_lease_id
            if str(other.get("shared_parent_lease_id") or "") == root_lease_id:
                other["shared_parent_lease_id"] = participant_lease_id
            _write_json(path, other)

        _write_controller_envelope(root, closed_at=utc_iso(now))
        with _CHECKPOINT_OVERLAY_LOCK:
            overlay = _CHECKPOINT_OVERLAYS.pop(root_lease_id, None)
            if overlay:
                new_root.update(overlay)
            _CHECKPOINT_OVERLAYS.pop(participant_lease_id, None)
        _write_json(_lease_path(participant_lease_id), new_root)
        _write_controller_envelope(new_root)
        _lease_path(root_lease_id).unlink(missing_ok=True)
        _sync_visual_manifest_locked(new_root)
        return {
            **new_root,
            "status": "promoted_to_root",
            "old_root_lease_id": root_lease_id,
            "new_root_lease_id": participant_lease_id,
            "resource_lease_owner": True,
            "disconnect_confirmation": confirmation,
        }


def release_workspace_participant(lease_id: str) -> dict[str, Any]:
    """Internal cleanup for a failed/aborted join; never releases the resource root."""
    with _metadata_lock():
        record = _read_mode_lease_record_locked(lease_id)
        if record is None:
            return {"status": "already_released", "lease_id": lease_id}
        if not _is_lease_participant(record):
            raise RuntimePolicyError("不是 participant", "该句柄是资源 Root lease，不能走 participant 清理")
        running_jobs = [
            job for job in _running_job_records_locked()
            if str(job.get("lease_id") or "") == lease_id
        ]
        if running_jobs:
            raise RuntimePolicyError(
                "Child 后台任务仍在运行",
                f"participant 仍有 {len(running_jobs)} 个后台任务；不能自动清理该 participant",
                retryable=True,
                error_type="PARTICIPANT_JOBS_ACTIVE",
            )
        _lease_path(lease_id).unlink(missing_ok=True)
        with _CHECKPOINT_OVERLAY_LOCK:
            _CHECKPOINT_OVERLAYS.pop(lease_id, None)
        return {
            "status": "released",
            "lease_id": lease_id,
            "shared_root_lease_id": record.get("shared_root_lease_id"),
            "resource_lease_released": False,
        }


def release_workspace_participants_for_agent(logical_agent_id: str) -> dict[str, Any]:
    """Release forced-terminal GPTAgent participants without releasing their Root lease.

    This is for logical Agents that have already been durably forced into a
    non-resumable terminal state (CLOSED/BLOCKED). Normal DONE Agents keep the
    explicit participant report_progress(done) handshake.
    """
    clean_agent = str(logical_agent_id or "").strip()
    if not clean_agent or len(clean_agent) > 160:
        raise RuntimePolicyError("参数无效", "logical_agent_id 无效")
    released: list[str] = []
    roots: set[str] = set()
    with _metadata_lock():
        participants = [
            (path, record)
            for path, record in _lease_records_locked()
            if _is_lease_participant(record)
            and str(record.get("logical_agent_id") or "") == clean_agent
        ]
        for _path, record in participants:
            lease_id = str(record.get("lease_id") or "")
            running_jobs = [
                job for job in _running_job_records_locked()
                if str(job.get("lease_id") or "") == lease_id
            ]
            if running_jobs:
                raise RuntimePolicyError(
                    "Child 后台任务仍在运行",
                    f"participant {lease_id} 仍有 {len(running_jobs)} 个后台任务；不能自动清理",
                    retryable=True,
                    error_type="PARTICIPANT_JOBS_ACTIVE",
                    details={"logical_agent_id": clean_agent, "lease_id": lease_id},
                )
        for path, record in participants:
            lease_id = str(record.get("lease_id") or "")
            roots.add(str(record.get("shared_root_lease_id") or ""))
            path.unlink(missing_ok=True)
            with _CHECKPOINT_OVERLAY_LOCK:
                _CHECKPOINT_OVERLAYS.pop(lease_id, None)
            released.append(lease_id)
    return {
        "status": "released" if released else "already_released",
        "logical_agent_id": clean_agent,
        "released_participant_lease_ids": released,
        "shared_root_lease_ids": sorted(root for root in roots if root),
        "resource_lease_released": False,
    }


def _lease_type_for_workspace(workspace: Path, mode: str) -> str:
    if not workspace.exists() or not workspace.is_dir():
        raise RuntimePolicyError("工作区不存在", f"工作区必须是现有目录：{workspace}")
    normalized = workspace.resolve(strict=False)
    if normalized == RUNTIME_MAINTENANCE_WORKSPACE.resolve(strict=False):
        if mode != "execute_exclusive":
            raise RuntimePolicyError("参数无效", "运行底座管理员租约必须使用 execute_exclusive")
        return LEASE_TYPE_RUNTIME_ADMIN
    if normalized == DESKTOP_OPS_WORKSPACE.resolve(strict=False):
        if mode != "execute_exclusive":
            raise RuntimePolicyError("参数无效", "桌面操作空租约必须使用 execute_exclusive")
        return LEASE_TYPE_DESKTOP_OPS
    if normalized == CONSOLE_RELAY_WORKSPACE.resolve(strict=False):
        if mode != "read_shared":
            raise RuntimePolicyError("参数无效", "远程监视/传话身份必须使用 read_shared；它只提供身份关联，不授予文件、命令、Git 或进程权限")
        return LEASE_TYPE_CONSOLE_RELAY
    if normalized == PROJECT_ROOT.resolve(strict=False):
        raise RuntimePolicyError(
            "受保护运行目录",
            f"不能把 {PROJECT_ROOT} 根目录作为普通开发工作区；桌面操作请租用 "
            f"{DESKTOP_OPS_WORKSPACE}，运行底座维护请租用 {RUNTIME_MAINTENANCE_WORKSPACE}",
        )
    if any(_paths_overlap(normalized, root) for root in _PROTECTED_RUNTIME_ROOTS):
        raise RuntimePolicyError(
            "受保护运行目录",
            f"不能把运行底座目录作为普通工作区：{workspace}；明确维护底座时使用 {RUNTIME_MAINTENANCE_WORKSPACE}",
        )
    return LEASE_TYPE_WORKSPACE


def _job_index_source() -> str:
    return str(JOB_DIR.resolve(strict=False))


def _job_record_mtime_ns(job_id: str) -> int:
    try:
        return _job_path(job_id).stat().st_mtime_ns
    except OSError:
        return -1


def _rebuild_active_job_index_locked() -> None:
    global _ACTIVE_JOB_INDEX_SOURCE
    _ACTIVE_JOB_INDEX.clear()
    for path in JOB_DIR.glob("job-*.json"):
        record = _read_json(path)
        if record is None:
            continue
        _process, status, _pid_present = _job_process_state(record)
        if status != "running":
            continue
        job_id = str(record.get("job_id") or "")
        if job_id:
            try:
                mtime_ns = path.stat().st_mtime_ns
            except OSError:
                mtime_ns = -1
            _ACTIVE_JOB_INDEX[job_id] = (record, mtime_ns)
    _ACTIVE_JOB_INDEX_SOURCE = _job_index_source()


def _ensure_active_job_index_locked() -> None:
    if _ACTIVE_JOB_INDEX_SOURCE != _job_index_source():
        _rebuild_active_job_index_locked()


def _set_active_job_index_record_locked(record: dict[str, Any]) -> None:
    _ensure_active_job_index_locked()
    job_id = str(record.get("job_id") or "")
    if job_id:
        _ACTIVE_JOB_INDEX[job_id] = (dict(record), _job_record_mtime_ns(job_id))


def _drop_active_job_index_record_locked(job_id: str) -> None:
    _ensure_active_job_index_locked()
    _ACTIVE_JOB_INDEX.pop(job_id, None)


def _running_job_records_locked() -> list[dict[str, Any]]:
    _ensure_active_job_index_locked()
    running: list[dict[str, Any]] = []
    for job_id, (cached_record, cached_mtime_ns) in list(_ACTIVE_JOB_INDEX.items()):
        record = cached_record
        current_mtime_ns = _job_record_mtime_ns(job_id)
        if current_mtime_ns != cached_mtime_ns:
            refreshed = _read_json(_job_path(job_id))
            if refreshed is None:
                _ACTIVE_JOB_INDEX.pop(job_id, None)
                continue
            record = refreshed
            _ACTIVE_JOB_INDEX[job_id] = (record, current_mtime_ns)
        _process, status, _pid_present = _job_process_state(record)
        if status == "running":
            running.append(record)
        else:
            _ACTIVE_JOB_INDEX.pop(job_id, None)
    return running


def acquire_workspace(
    workspace: Path,
    *,
    mode: Literal["read_shared", "execute_exclusive"],
    ttl_seconds: int = DEFAULT_LEASE_SECONDS,
    owner_label: str = "未命名任务",
    thread_id: str | None = None,
    controller_id: str | None = None,
    runtime_instance: str | None = None,
    conversation_id: str | None = None,
    conversation_url: str | None = None,
    conversation_source: str | None = None,
) -> dict[str, Any]:
    _ensure_runtime_dirs()
    if mode not in {"read_shared", "execute_exclusive"}:
        raise RuntimePolicyError("参数无效", "mode 必须是 read_shared 或 execute_exclusive")
    lease_type = _lease_type_for_workspace(workspace, mode)
    ttl_seconds = int(ttl_seconds)
    if ttl_seconds < 60 or ttl_seconds > MAX_LEASE_SECONDS:
        raise RuntimePolicyError("参数无效", f"租约时长必须在 60 到 {MAX_LEASE_SECONDS} 秒之间")
    label = owner_label.strip() or "未命名任务"
    if len(label) > 200:
        raise RuntimePolicyError("参数无效", "任务标识不能超过 200 个字符")
    if lease_type == LEASE_TYPE_RUNTIME_ADMIN and label == "未命名任务":
        raise RuntimePolicyError("参数无效", "运行底座管理员租约必须提供明确 owner_label")
    requested_controller_id = _normalise_controller_id(controller_id)
    thread_identity_supplied = bool(isinstance(thread_id, str) and thread_id.strip())
    if not thread_identity_supplied:
        resolved_thread_id = f"thread-{uuid.uuid4().hex}"
    else:
        resolved_thread_id = thread_id.strip()
        if len(resolved_thread_id) > 128 or any(
            not (character.isascii() and (character.isalnum() or character in "._:-"))
            for character in resolved_thread_id
        ):
            raise RuntimePolicyError("参数无效", "thread_id 最多 128 个 ASCII 字母数字或 . _ : - 字符")
    resolved_runtime_instance = runtime_instance.strip() if isinstance(runtime_instance, str) else ""
    requested_conversation_id = _normalise_web_conversation_id(conversation_id)
    if resolved_runtime_instance and (
        len(resolved_runtime_instance) > 128
        or any(
            not (character.isascii() and (character.isalnum() or character in "._:-"))
            for character in resolved_runtime_instance
        )
    ):
        raise RuntimePolicyError("参数无效", "runtime_instance 最多 128 个 ASCII 字母数字或 . _ : - 字符")

    normalized = workspace.resolve(strict=False)
    with _metadata_lock():
        active = _lease_records_locked()
        same_thread_non_relay = [
            record for _path, record in active
            if not _is_lease_participant(record)
            if str(record.get("thread_id") or "") == resolved_thread_id
            and str(record.get("lease_type", LEASE_TYPE_WORKSPACE)) != LEASE_TYPE_CONSOLE_RELAY
        ]
        same_thread_relay = [
            record for _path, record in active
            if not _is_lease_participant(record)
            if str(record.get("thread_id") or "") == resolved_thread_id
            and str(record.get("lease_type", LEASE_TYPE_WORKSPACE)) == LEASE_TYPE_CONSOLE_RELAY
        ]
        inherited_controller_ids = {
            lease_controller_id(record)
            for record in same_thread_non_relay
            if lease_controller_id(record)
        }
        if requested_controller_id and inherited_controller_ids and requested_controller_id not in inherited_controller_ids:
            raise RuntimePolicyError(
                "主控身份冲突",
                "显式 controller_id 与当前 thread 已有租约的主控身份不一致；拒绝把两个 GPT 主控静默合并",
                error_type="CONTROLLER_IDENTITY_CONFLICT",
                details={"controller_id": requested_controller_id, "existing": sorted(inherited_controller_ids)},
            )
        if requested_controller_id:
            resolved_controller_id = requested_controller_id
        elif len(inherited_controller_ids) == 1:
            resolved_controller_id = next(iter(inherited_controller_ids))
        elif len(inherited_controller_ids) > 1:
            raise RuntimePolicyError(
                "主控身份歧义",
                "同一 thread 的活动租约记录出现多个 controller_id；拒绝自动选择",
                retryable=True,
                error_type="CONTROLLER_IDENTITY_AMBIGUOUS",
                details={"controller_ids": sorted(inherited_controller_ids)},
            )
        elif thread_identity_supplied:
            # Match lease_controller_id()'s legacy fallback exactly. This keeps
            # the stable controller identity unchanged when an old owner handle
            # disappears and the same ChatGPT conversation recreates it from
            # its durable thread_id.
            resolved_controller_id = "thread:" + resolved_thread_id
        else:
            resolved_controller_id = "controller:" + uuid.uuid4().hex
        if lease_type == LEASE_TYPE_CONSOLE_RELAY and same_thread_non_relay:
            raise RuntimePolicyError(
                "传话身份冲突",
                "该 thread 已经持有真实执行/桌面/管理员任务；不能再为同一 thread 建立远程传话身份给自己的任务发用户消息",
                error_type="CONSOLE_RELAY_SELF_IDENTITY",
            )
        if lease_type != LEASE_TYPE_CONSOLE_RELAY and same_thread_relay:
            raise RuntimePolicyError(
                "传话身份冲突",
                "该 thread 已经作为远程监视/传话身份存在；同一 thread 不能同时转为真实执行/桌面/管理员任务。先释放传话身份，再开始新的执行任务",
                error_type="CONSOLE_RELAY_IDENTITY_CONFLICT",
            )
        # A caller that explicitly preserved its high-random thread identity may
        # recover the exact same workspace lease even after model-context or
        # Runtime rehydration.  runtime_instance is diagnostic here: a server
        # restart must not turn the same logical ChatGPT task into a stranger.
        #
        # Conversely, when thread_id was omitted we deliberately generated a
        # fresh local placeholder above.  That placeholder is NOT evidence that
        # an existing lease belongs to another conversation.
        if (thread_identity_supplied or requested_controller_id) and lease_type != LEASE_TYPE_DESKTOP_OPS:
            identity_matches = [
                record
                for _path, record in active
                if not _is_lease_participant(record)
                if Path(str(record.get("workspace", ""))).resolve(strict=False) == normalized
                and (
                    (requested_controller_id and lease_controller_id(record) == requested_controller_id)
                    or (thread_identity_supplied and str(record.get("thread_id") or "") == resolved_thread_id)
                )
            ]
            reusable = [
                record
                for record in identity_matches
                if str(record.get("lease_type", LEASE_TYPE_WORKSPACE)) == lease_type
                and str(record.get("mode") or "") == mode
            ]
            if len(reusable) == 1:
                reused = dict(reusable[0])
                if not reused.get("controller_id"):
                    reused["controller_id"] = lease_controller_id(reused)
                if requested_conversation_id:
                    _bind_web_conversation_locked(
                        reused,
                        conversation_id=requested_conversation_id,
                        conversation_url=conversation_url,
                        source=conversation_source,
                    )
                _write_json(_lease_path(str(reused.get("lease_id") or "")), reused)
                _write_controller_envelope(reused)
                reused["status"] = "reused"
                reused["reused_existing"] = True
                return reused
            if len(reusable) > 1:
                raise RuntimePolicyError(
                    "租约恢复歧义",
                    "当前调用身份下存在多个可兼容的活动租约，拒绝自动恢复；请检查 workspace_status 后显式选择",
                    retryable=True,
                    error_type="LEASE_RECOVERY_AMBIGUOUS",
                    details={
                        "workspace": str(normalized),
                        "candidate_count": len(reusable),
                        "recommended_action": "workspace_status_then_explicit_lease",
                    },
                )
            if identity_matches:
                raise RuntimePolicyError(
                    "租约恢复不兼容",
                    "当前 thread 在该工作区已有活动租约，但 lease_type 或 mode 与本次请求不兼容；拒绝自动恢复或创建重叠租约",
                    retryable=True,
                    error_type="LEASE_RECOVERY_INCOMPATIBLE",
                    details={
                        "workspace": str(normalized),
                        "recommended_action": "workspace_status_then_explicit_lease",
                    },
                )
        # Lease identity and resource ownership are deliberately separate.
        # runtime_admin, project workspace and desktop_ops leases may coexist;
        # actual write conflicts are decided by the resource/workspace overlap
        # checks below and by each tool's lease scope validation. A running Job
        # owned by another lease therefore does not block an administrator from
        # acquiring the maintenance lease, nor does administrator presence block
        # an unrelated project or desktop lease.
        if mode == "execute_exclusive":
            conflicts = [
                record
                for _path, record in active
                if not _is_lease_participant(record)
                if str(record.get("mode")) == "execute_exclusive"
                and _paths_overlap(normalized, Path(str(record.get("workspace", ""))))
                and not (
                    lease_type == LEASE_TYPE_DESKTOP_OPS
                    and str(record.get("lease_type", LEASE_TYPE_WORKSPACE)) == LEASE_TYPE_DESKTOP_OPS
                )
            ]
            if conflicts:
                conflict = conflicts[0]
                recovery_task = _recovery_runtime_task(conflict)
                exact_workspace = (
                    Path(str(conflict.get("workspace") or "")).resolve(strict=False) == normalized
                )
                compatible_shape = (
                    str(conflict.get("lease_type", LEASE_TYPE_WORKSPACE)) == lease_type
                    and str(conflict.get("mode") or "") == mode
                )
                label_matches = str(conflict.get("owner_label") or "") == label
                conflict_controller_id = lease_controller_id(conflict)
                conflict_conversation_id = str(conflict.get("web_conversation_id") or "").strip()
                same_web_conversation = bool(
                    requested_conversation_id
                    and conflict_conversation_id
                    and requested_conversation_id == conflict_conversation_id
                )
                conflicting_web_conversation = bool(
                    requested_conversation_id
                    and conflict_conversation_id
                    and requested_conversation_id != conflict_conversation_id
                )
                if same_web_conversation:
                    conflict_kind = "same_web_conversation"
                elif conflicting_web_conversation:
                    conflict_kind = "different_web_conversation"
                elif requested_controller_id and conflict_controller_id == requested_controller_id:
                    conflict_kind = "same_controller"
                elif not thread_identity_supplied:
                    conflict_kind = "caller_identity_unknown"
                elif str(conflict.get("thread_id") or "") != resolved_thread_id:
                    conflict_kind = "other_thread"
                else:
                    conflict_kind = "incompatible_existing_lease"
                self_recovery_candidate = bool(
                    exact_workspace
                    and compatible_shape
                    and not conflicting_web_conversation
                    and (
                        same_web_conversation
                        or (requested_controller_id and conflict_controller_id == requested_controller_id)
                        or not thread_identity_supplied
                    )
                )
                if self_recovery_candidate:
                    raise RuntimePolicyError(
                        "需要恢复原任务",
                        "同一工作区已有可恢复的兼容活动执行租约。owner_label 变化、当前轮忘记旧 thread_id，"
                        "或模型上下文重建都不能证明这是另一个对话。"
                        "必须复用 recoverable_runtime_task.lease_id 继续原任务；不得重新 acquire，也不得把它描述成‘别的对话正在工作’。",
                        retryable=True,
                        error_type="LEASE_RECOVERY_REQUIRED",
                        details={
                            "conflict_kind": conflict_kind,
                            "workspace": str(conflict.get("workspace") or normalized),
                            "owner_label": str(conflict.get("owner_label") or ""),
                            "owner_label_matches_request": label_matches,
                            "self_recovery_candidate": True,
                            "controller_id": conflict_controller_id,
                            "recommended_action": "reuse_recoverable_lease_id",
                            "recoverable_runtime_task": recovery_task,
                        },
                    )
                raise RuntimePolicyError(
                    "工作区忙",
                    "工作区忙：已有活动执行租约。但仅凭‘存在活跃租约’或调用方本轮新生成的 thread 占位符，不能判定它属于另一个对话。"
                    "若返回的 recoverable_runtime_task 与当前中断任务的 workspace/owner/目标一致，直接复用其中 lease_id；"
                    "只有存在明确不同任务或不同稳定 thread 的证据时，才按其他任务处理。",
                    retryable=True,
                    error_type="WORKSPACE_BUSY",
                    details={
                        "conflict_kind": conflict_kind,
                        "workspace": str(conflict.get("workspace") or normalized),
                        "owner_label": str(conflict.get("owner_label") or ""),
                        "owner_label_matches_request": label_matches,
                        "self_recovery_candidate": False,
                        "recommended_action": "recover_if_same_task_else_read_only_or_wait",
                        "same_task_recovery_action": "reuse_recoverable_lease_id_do_not_reacquire",
                        "recoverable_runtime_task": recovery_task,
                    },
                )
        lease_id = uuid.uuid4().hex
        now = utc_now()
        expires_at = now + timedelta(seconds=ttl_seconds)
        record = {
            "lease_id": lease_id,
            "workspace": str(normalized),
            "scope_root": str(PROJECT_ROOT.resolve(strict=False) if lease_type == LEASE_TYPE_RUNTIME_ADMIN else normalized),
            "lease_type": lease_type,
            "mode": mode,
            "owner_label": label,
            "thread_id": resolved_thread_id,
            "controller_id": resolved_controller_id,
            "runtime_instance": resolved_runtime_instance or None,
            "created_at": utc_iso(now),
            "renewed_at": utc_iso(now),
            "expires_at": utc_iso(expires_at),
        }
        record = _hydrate_controller_envelope(record)
        if requested_conversation_id:
            _bind_web_conversation_locked(
                record,
                conversation_id=requested_conversation_id,
                conversation_url=conversation_url,
                source=conversation_source,
            )
        _write_json(_lease_path(lease_id), record)
        _write_controller_envelope(record)
    return record


def renew_workspace(
    lease_id: str,
    *,
    ttl_seconds: int = DEFAULT_LEASE_SECONDS,
    conversation_id: str | None = None,
    conversation_url: str | None = None,
    conversation_source: str | None = None,
) -> dict[str, Any]:
    ttl_seconds = int(ttl_seconds)
    if ttl_seconds < 60 or ttl_seconds > MAX_LEASE_SECONDS:
        raise RuntimePolicyError("参数无效", f"租约时长必须在 60 到 {MAX_LEASE_SECONDS} 秒之间")
    with _metadata_lock():
        source = _read_mode_lease_record_locked(lease_id)
        if source is None:
            raise RuntimePolicyError("租约不存在", "该工作区租约不存在或已经过期")
        record = _shared_root_record_locked(source)
        resource_lease_id = str(record.get("lease_id") or lease_id)
        try:
            if _parse_utc(str(record["expires_at"])) <= utc_now():
                _lease_path(resource_lease_id).unlink(missing_ok=True)
                raise RuntimePolicyError("租约已过期", "该工作区租约已经过期，请重新申请")
        except KeyError as exc:
            raise RuntimePolicyError("租约无效", "工作区租约记录不完整") from exc
        if conversation_id:
            _bind_web_conversation_locked(
                record,
                conversation_id=conversation_id,
                conversation_url=conversation_url,
                source=conversation_source,
            )
        now = utc_now()
        record["renewed_at"] = utc_iso(now)
        deadlines = [now + timedelta(seconds=ttl_seconds)]
        if record.get("stay_until_at"):
            deadlines.append(_parse_utc(str(record["stay_until_at"])))
        visual = record.get("visual_mode")
        if isinstance(visual, dict) and visual.get("active") and visual.get("until_at"):
            deadlines.append(_parse_utc(str(visual["until_at"])))
        record["expires_at"] = utc_iso(max(deadlines))
        _write_json(_lease_path(resource_lease_id), record)
        _write_controller_envelope(record)
        for path, participant in _lease_records_locked():
            if _is_lease_participant(participant) and str(participant.get("shared_root_lease_id") or "") == resource_lease_id:
                participant["renewed_at"] = record["renewed_at"]
                participant["expires_at"] = record["expires_at"]
                _write_json(path, participant)
        return _participant_view(source, record) if _is_lease_participant(source) else record


def set_workspace_stay_mode(
    lease_id: str,
    *,
    duration_seconds: int | None = None,
    command_id: str | None = None,
) -> dict[str, Any]:
    """Agent-facing enable/extend ONLY; closing requires verified Console ingestion."""
    if duration_seconds is None:
        raise RuntimePolicyError("禁止自行解除驻场", "当前 GPT 不能清空自己的驻场；使用可信 Console 或独立 thread 救援", error_type="STAY_SELF_DISABLE_FORBIDDEN")
    if isinstance(duration_seconds, bool) or not isinstance(duration_seconds, int) or not 10 <= duration_seconds <= MAX_LEASE_SECONDS:
        raise RuntimePolicyError("参数无效", "驻场时长须为 10 至 86400 的整数秒")
    with _metadata_lock():
        source = _active_mode_lease_locked(lease_id)
        record = _shared_root_record_locked(source)
        resource_lease_id = str(record.get("lease_id") or lease_id)
        now = utc_now()
        until = now + timedelta(seconds=duration_seconds)
        previous = _mode_deadline(record.get("stay_until_at"))
        if previous and previous > now and until < previous:
            raise RuntimePolicyError("禁止缩短驻场", "当前 GPT 只能开启或延长驻场，不能缩短", error_type="STAY_SHORTEN_FORBIDDEN")
        record.update(stay_started_at=utc_iso(now), stay_until_at=utc_iso(until), stay_duration_seconds=duration_seconds)
        if command_id:
            record["stay_command_id"] = command_id[:256]
        _write_json(_lease_path(resource_lease_id), record)
        _sync_visual_manifest_locked(record)
    return _participant_view(source, record) if _is_lease_participant(source) else record


def _ensure_workspace_releasable_locked(record: dict[str, Any]) -> None:
    if not _is_lease_participant(record):
        root_id = str(record.get("lease_id") or "")
        active_participants = [
            other for _path, other in _lease_records_locked()
            if _is_lease_participant(other) and str(other.get("shared_root_lease_id") or "") == root_id
        ]
        if active_participants:
            raise RuntimePolicyError(
                "Child 仍在共享租约中",
                f"当前 Root workspace lease 仍有 {len(active_participants)} 个 GPTAgent participant；先让 Child complete + report_progress(done) 退出 participant，再由 Root 最终释放资源租约",
                retryable=True,
                error_type="SHARED_LEASE_PARTICIPANTS_ACTIVE",
                details={"participant_count": len(active_participants)},
            )
    stay = _mode_deadline(record.get("stay_until_at"))
    if stay and stay > utc_now():
        raise RuntimePolicyError("驻场模式生效中", "驻场期间禁止 done/release；当前 GPT 不得自行解锁", error_type="STAY_ACTIVE")
    visual = visual_mode_state(record, include_details=False)
    if visual.get("active") or visual.get("status") in {"needs_review", "expired"}:
        raise RuntimePolicyError("看图模式生效中", "先完成当前 visual.targets 声明的视觉验收，再完成任务", error_type="VISUAL_ACTIVE")
    goal = goal_mode_state(record, include_details=False)
    if goal.get("active") or goal.get("status") == "needs_review":
        raise RuntimePolicyError(
            "目标模式尚未验收",
            "先逐条完成当前 goal_mode.success_criteria，并通过 set_visual_mode(action='complete', review={'goal': ...}) 写入目标验收，再 done/release",
            error_type="GOAL_ACTIVE",
        )
    if goal.get("status") == "completed":
        validated = goal.get("validated_workspace_revision")
        current_revision = int(record.get("workspace_validation_revision") or 0)
        if type(validated) is not int or validated != current_revision:
            raise RuntimePolicyError(
                "目标验收证据已过期",
                "目标验收后工作区又发生了实际修改；必须重新逐条验收 success_criteria，不能沿用旧的 done 结论",
                error_type="GOAL_EVIDENCE_STALE",
            )
    if str(record.get("lease_type", LEASE_TYPE_WORKSPACE)) != LEASE_TYPE_RUNTIME_ADMIN:
        return
    lease_id = str(record.get("lease_id") or "")
    running_jobs = [
        job
        for job in _running_job_records_locked()
        if str(job.get("lease_id") or "") == lease_id
    ]
    if not running_jobs:
        return
    job_names = ", ".join(
        str(job.get("name") or job.get("job_id") or "后台任务")[:80]
        for job in running_jobs[:3]
    )
    suffix = f"（{job_names}）" if job_names else ""
    raise RuntimePolicyError(
        "运行底座维护任务仍在运行",
        f"runtime_admin 仍有 {len(running_jobs)} 个属于该租约的后台任务在运行{suffix}；请先等待或取消这些任务，再完成/释放管理员租约",
        retryable=True,
    )


def ensure_workspace_releasable(lease_id: str) -> dict[str, Any] | None:
    """Check whether a lease can complete without orphaning admin-owned jobs."""
    with _metadata_lock():
        record = _read_mode_lease_record_locked(lease_id)
        if record is None:
            return None
        if _is_lease_participant(record):
            running_jobs = [
                job for job in _running_job_records_locked()
                if str(job.get("lease_id") or "") == lease_id
            ]
            if running_jobs:
                raise RuntimePolicyError(
                    "Child 后台任务仍在运行",
                    f"participant 仍有 {len(running_jobs)} 个后台任务；先等待或取消后再退出 Child",
                    retryable=True,
                    error_type="PARTICIPANT_JOBS_ACTIVE",
                )
            return record
        _ensure_workspace_releasable_locked(record)
        return record


def release_workspace(lease_id: str) -> dict[str, Any]:
    with _metadata_lock():
        path = _lease_path(lease_id)
        record = _read_mode_lease_record_locked(lease_id)
        if record is None:
            with _CHECKPOINT_OVERLAY_LOCK:
                _CHECKPOINT_OVERLAYS.pop(lease_id, None)
            return {"status": "already_released", "lease_id": lease_id}
        if _is_lease_participant(record):
            running_jobs = [
                job for job in _running_job_records_locked()
                if str(job.get("lease_id") or "") == lease_id
            ]
            if running_jobs:
                raise RuntimePolicyError(
                    "Child 后台任务仍在运行",
                    f"participant 仍有 {len(running_jobs)} 个后台任务；先等待或取消后再退出 Child",
                    retryable=True,
                    error_type="PARTICIPANT_JOBS_ACTIVE",
                )
            with _CHECKPOINT_OVERLAY_LOCK:
                _CHECKPOINT_OVERLAYS.pop(lease_id, None)
            path.unlink(missing_ok=True)
            return {
                "status": "released",
                "lease_id": lease_id,
                "workspace": record.get("workspace"),
                "lease_type": record.get("lease_type", LEASE_TYPE_WORKSPACE),
                "lease_role": LEASE_ROLE_PARTICIPANT,
                "shared_root_lease_id": record.get("shared_root_lease_id"),
                "resource_lease_released": False,
            }
        _ensure_workspace_releasable_locked(record)
        with _CHECKPOINT_OVERLAY_LOCK:
            overlay = _CHECKPOINT_OVERLAYS.pop(lease_id, None)
            if overlay:
                record.update(overlay)
        _write_controller_envelope(record, closed_at=utc_iso())
        path.unlink(missing_ok=True)
    return {
        "status": "released",
        "lease_id": lease_id,
        "workspace": record.get("workspace"),
        "lease_type": record.get("lease_type", LEASE_TYPE_WORKSPACE),
    }


def _execution_checkpoint(record: dict[str, Any]) -> dict[str, Any]:
    successful_actions = int(record.get("execution_successful_action_count") or 0)
    side_effect_actions = int(record.get("execution_side_effect_action_count") or 0)
    last_progress_text = str(record.get("execution_last_progress_text") or "").strip()
    return {
        "started_at": record.get("controller_task_started_at") or record.get("created_at"),
        "successful_action_count": successful_actions,
        "side_effect_action_count": side_effect_actions,
        "last_tool": record.get("execution_last_tool"),
        "last_success_at": record.get("execution_last_success_at"),
        "last_side_effect_tool": record.get("execution_last_side_effect_tool"),
        "last_side_effect_at": record.get("execution_last_side_effect_at"),
        "last_progress_phase": record.get("execution_last_progress_phase"),
        "last_progress_text": last_progress_text or None,
        "last_progress_at": record.get("execution_last_progress_at"),
        "checkpoint_updated_at": record.get("execution_checkpoint_updated_at"),
        "has_execution_history": bool(successful_actions or side_effect_actions or last_progress_text),
    }


def _lease_heartbeat_at(record: dict[str, Any]) -> str | None:
    checkpoint = _execution_checkpoint(record)
    for key in ("checkpoint_updated_at", "last_success_at", "last_progress_at", "started_at"):
        value = checkpoint.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    return None


def disconnect_probe_state(record: dict[str, Any]) -> dict[str, Any] | None:
    probe_id = str(record.get("disconnect_probe_id") or "").strip()
    activity_at = str(record.get("disconnect_probe_activity_at") or "").strip()
    sent_at = str(record.get("disconnect_probe_sent_at") or "").strip()
    deadline_at = str(record.get("disconnect_probe_response_deadline_at") or "").strip()
    delivered_at = str(record.get("disconnect_probe_delivered_at") or "").strip()
    if not probe_id or not activity_at or not sent_at:
        return None
    # Legacy probes had no distinct delivery timestamp; an existing deadline
    # proves they were already delivered. New probes may be queued while a tool
    # is in flight and start their response clock only at the safe boundary.
    if not delivered_at and deadline_at:
        delivered_at = sent_at
    return {
        "id": probe_id,
        "activity_at": activity_at,
        "sent_at": sent_at,
        "delivered_at": delivered_at or None,
        "response_deadline_at": deadline_at or None,
        "acked_at": record.get("disconnect_probe_acked_at"),
        "acked_via_lease_id": record.get("disconnect_probe_acked_via_lease_id"),
        "delivered_via_lease_id": record.get("disconnect_probe_delivered_via_lease_id"),
    }


def ensure_disconnect_probe(
    lease_id: str,
    *,
    activity_at: str,
    response_seconds: int = 120,
    defer_delivery: bool = False,
) -> dict[str, Any]:
    """Create at most one persistent system liveness probe per activity episode."""
    if isinstance(response_seconds, bool) or not isinstance(response_seconds, int) or not 30 <= response_seconds <= 600:
        raise RuntimePolicyError("参数无效", "存活探测响应窗口需为 30 至 600 秒")
    if type(defer_delivery) is not bool:
        raise RuntimePolicyError("参数无效", "defer_delivery 必须是布尔值")
    activity = _mode_deadline(activity_at)
    if not activity:
        raise RuntimePolicyError("参数无效", "存活探测 activity_at 必须是有效 UTC 时间")
    activity_text = utc_iso(activity)
    with _metadata_lock():
        record = _active_mode_lease_locked(lease_id)
        current = disconnect_probe_state(record)
        if current and current.get("activity_at") == activity_text:
            return {"status": "existing", "lease_id": lease_id, "probe": current}
        now = utc_now()
        now_text = utc_iso(now)
        probe_id = f"probe-{uuid.uuid4().hex}"
        record["disconnect_probe_id"] = probe_id
        record["disconnect_probe_activity_at"] = activity_text
        record["disconnect_probe_sent_at"] = now_text
        if defer_delivery:
            record.pop("disconnect_probe_delivered_at", None)
            record.pop("disconnect_probe_delivered_via_lease_id", None)
            record.pop("disconnect_probe_response_deadline_at", None)
        else:
            record["disconnect_probe_delivered_at"] = now_text
            record["disconnect_probe_delivered_via_lease_id"] = lease_id
            record["disconnect_probe_response_deadline_at"] = utc_iso(now + timedelta(seconds=response_seconds))
        for key in (
            "disconnect_probe_acked_at",
            "disconnect_probe_acked_via_lease_id",
            "disconnect_alert_pending_probe_id",
            "disconnect_alert_claimed_at",
            "disconnect_alert_retry_after_at",
            "disconnect_confirmed_at",
            "disconnect_confirmed_probe_id",
            "disconnect_rescue_deadline_at",
        ):
            record.pop(key, None)
        _write_json(_lease_path(lease_id), record)
        return {"status": "created", "lease_id": lease_id, "probe": disconnect_probe_state(record)}


def deliver_disconnect_probe(
    lease_id: str,
    probe_id: str,
    *,
    response_seconds: int = 120,
    delivered_via_lease_id: str | None = None,
) -> dict[str, Any]:
    """Deliver a queued liveness probe at a safe tool boundary."""
    if isinstance(response_seconds, bool) or not isinstance(response_seconds, int) or not 30 <= response_seconds <= 600:
        raise RuntimePolicyError("参数无效", "存活探测响应窗口需为 30 至 600 秒")
    clean_id = str(probe_id or "").strip()
    with _metadata_lock():
        record = _active_mode_lease_locked(lease_id)
        current = disconnect_probe_state(record)
        if not current or current.get("id") != clean_id:
            return {"status": "stale", "lease_id": lease_id}
        if current.get("acked_at"):
            return {"status": "already_acked", "lease_id": lease_id, "probe": current}
        if current.get("delivered_at") and current.get("response_deadline_at"):
            return {"status": "already_delivered", "lease_id": lease_id, "probe": current}
        now = utc_now()
        record["disconnect_probe_delivered_at"] = utc_iso(now)
        record["disconnect_probe_response_deadline_at"] = utc_iso(now + timedelta(seconds=response_seconds))
        via = str(delivered_via_lease_id or lease_id).strip()
        if via:
            record["disconnect_probe_delivered_via_lease_id"] = via[:128]
        _write_json(_lease_path(lease_id), record)
        return {"status": "delivered", "lease_id": lease_id, "probe": disconnect_probe_state(record)}


def ack_disconnect_probe(
    lease_id: str,
    probe_id: str,
    *,
    acknowledged_via_lease_id: str | None = None,
) -> dict[str, Any]:
    """Record an explicit GPT progress reply to a system liveness probe."""
    clean_id = str(probe_id or "").strip()
    with _metadata_lock():
        record = _active_mode_lease_locked(lease_id)
        current = disconnect_probe_state(record)
        if not current or current.get("id") != clean_id:
            return {"status": "stale", "lease_id": lease_id}
        if current.get("acked_at"):
            return {"status": "already_acked", "lease_id": lease_id, "probe": current}
        record["disconnect_probe_acked_at"] = utc_iso()
        via = str(acknowledged_via_lease_id or lease_id).strip()
        if via:
            record["disconnect_probe_acked_via_lease_id"] = via[:128]
        if record.get("disconnect_alert_pending_probe_id") == clean_id:
            record.pop("disconnect_alert_pending_probe_id", None)
            record.pop("disconnect_alert_claimed_at", None)
        for key in ("disconnect_confirmed_at", "disconnect_confirmed_probe_id", "disconnect_rescue_deadline_at"):
            record.pop(key, None)
        _write_json(_lease_path(lease_id), record)
        return {"status": "acked", "lease_id": lease_id, "probe": disconnect_probe_state(record)}


def clear_disconnect_probe(
    lease_id: str,
    *,
    probe_id: str | None = None,
    reason: str = "controller_activity_resumed",
) -> dict[str, Any]:
    """Cancel one stale liveness episode when the same GPT controller is active elsewhere."""
    clean_probe_id = str(probe_id or "").strip()
    with _metadata_lock():
        record = _read_mode_lease_record_locked(lease_id)
        if record is None:
            return {"status": "missing", "lease_id": lease_id}
        current = disconnect_probe_state(record)
        if current is None:
            return {"status": "absent", "lease_id": lease_id}
        if clean_probe_id and current.get("id") != clean_probe_id:
            return {"status": "stale", "lease_id": lease_id, "probe": current}
        for key in (
            "disconnect_probe_id",
            "disconnect_probe_activity_at",
            "disconnect_probe_sent_at",
            "disconnect_probe_delivered_at",
            "disconnect_probe_delivered_via_lease_id",
            "disconnect_probe_response_deadline_at",
            "disconnect_probe_acked_at",
            "disconnect_probe_acked_via_lease_id",
            "disconnect_alert_pending_probe_id",
            "disconnect_alert_claimed_at",
            "disconnect_alert_retry_after_at",
            "disconnect_confirmed_at",
            "disconnect_confirmed_probe_id",
            "disconnect_rescue_deadline_at",
        ):
            record.pop(key, None)
        record["disconnect_probe_cleared_at"] = utc_iso()
        record["disconnect_probe_clear_reason"] = str(reason or "controller_activity_resumed")[:200]
        _write_json(_lease_path(lease_id), record)
        return {"status": "cleared", "lease_id": lease_id, "probe_id": current.get("id")}


def disconnect_confirmation_state(record: dict[str, Any] | None) -> dict[str, Any] | None:
    if not record:
        return None
    confirmed_at = str(record.get("disconnect_confirmed_at") or "").strip()
    rescue_deadline_at = str(record.get("disconnect_rescue_deadline_at") or "").strip()
    if not confirmed_at or not rescue_deadline_at:
        return None
    return {
        "confirmed_at": confirmed_at,
        "probe_id": record.get("disconnect_confirmed_probe_id"),
        "rescue_deadline_at": rescue_deadline_at,
    }


def confirm_disconnect_for_rescue(
    lease_id: str,
    *,
    probe_id: str,
    rescue_grace_seconds: int = 300,
) -> dict[str, Any]:
    if isinstance(rescue_grace_seconds, bool) or not isinstance(rescue_grace_seconds, int) or not 30 <= rescue_grace_seconds <= 3600:
        raise RuntimePolicyError("参数无效", "rescue grace 需为 30 至 3600 秒")
    clean_probe_id = str(probe_id or "").strip()
    with _metadata_lock():
        record = _active_mode_lease_locked(lease_id)
        if _is_lease_participant(record):
            raise RuntimePolicyError("掉线确认目标无效", "只允许确认 Root owner 掉线", error_type="DISCONNECT_ROOT_REQUIRED")
        probe = disconnect_probe_state(record)
        if not probe or str(probe.get("id") or "") != clean_probe_id:
            raise RuntimePolicyError("掉线探测已变化", "liveness probe 已变化，重新读取后再确认", error_type="DISCONNECT_PROBE_STALE")
        if probe.get("acked_at"):
            return {"status": "recovered", "lease_id": lease_id}
        existing = disconnect_confirmation_state(record)
        if existing:
            return {"status": "confirmed", "lease_id": lease_id, **existing}
        now = utc_now()
        record["disconnect_confirmed_at"] = utc_iso(now)
        record["disconnect_confirmed_probe_id"] = clean_probe_id
        record["disconnect_rescue_deadline_at"] = utc_iso(now + timedelta(seconds=rescue_grace_seconds))
        _write_json(_lease_path(lease_id), record)
        return {"status": "confirmed", "lease_id": lease_id, **(disconnect_confirmation_state(record) or {})}


def claim_disconnect_alert(lease_id: str, *, stale_seconds: int = 300) -> dict[str, Any]:
    """Atomically claim one active GPT-task disconnect episode for the local notifier."""
    if isinstance(stale_seconds, bool) or not isinstance(stale_seconds, int) or not 30 <= stale_seconds <= 3600:
        raise RuntimePolicyError("参数无效", "掉线判定阈值需为 30 至 3600 秒")
    with _metadata_lock():
        record = _active_mode_lease_locked(lease_id)
        now = utc_now()
        retry_after = _mode_deadline(record.get("disconnect_alert_retry_after_at"))
        if retry_after and now < retry_after:
            return {
                "status": "retry_backoff",
                "lease_id": lease_id,
                "retry_after_at": utc_iso(retry_after),
            }
        probe = disconnect_probe_state(record)
        if probe is not None:
            if probe.get("acked_at"):
                return {"status": "explicit_response", "lease_id": lease_id, "probe_id": probe.get("id")}
            response_deadline = _mode_deadline(probe.get("response_deadline_at"))
            if (
                not probe.get("delivered_at") or not response_deadline or now <= response_deadline
            ):
                return {"status": "probe_waiting", "lease_id": lease_id, "probe_id": probe.get("id")}
            probe_id = str(probe.get("id") or "")
            confirmation = disconnect_confirmation_state(record)
            if (
                not confirmation
                or str(confirmation.get("probe_id") or "") != probe_id
            ):
                return {
                    "status": "disconnect_not_confirmed",
                    "lease_id": lease_id,
                    "probe_id": probe_id,
                }
            rescue_deadline = _mode_deadline(confirmation.get("rescue_deadline_at"))
            if rescue_deadline is None or now <= rescue_deadline:
                return {
                    "status": "rescue_grace",
                    "lease_id": lease_id,
                    "probe_id": probe_id,
                    "rescue_deadline_at": confirmation.get("rescue_deadline_at"),
                }
            heartbeat_text = str(probe.get("activity_at") or _lease_heartbeat_at(record) or "")
            heartbeat = _mode_deadline(heartbeat_text)
            age_seconds = max(0, int((now - heartbeat).total_seconds())) if heartbeat else stale_seconds
            if record.get("disconnect_alert_sent_probe_id") == probe_id:
                return {"status": "already_notified", "lease_id": lease_id, "probe_id": probe_id, "heartbeat_at": heartbeat_text, "age_seconds": age_seconds}
            if record.get("disconnect_alert_pending_probe_id") == probe_id:
                claimed_at = _mode_deadline(record.get("disconnect_alert_claimed_at"))
                claim_timeout_seconds = max(120, stale_seconds)
                if claimed_at and (now - claimed_at).total_seconds() <= claim_timeout_seconds:
                    return {"status": "in_progress", "lease_id": lease_id, "probe_id": probe_id, "heartbeat_at": heartbeat_text, "age_seconds": age_seconds}
                record.pop("disconnect_alert_pending_probe_id", None)
                record.pop("disconnect_alert_claimed_at", None)
            record["disconnect_alert_pending_probe_id"] = probe_id
            record["disconnect_alert_claimed_at"] = utc_iso(now)
            _write_json(_lease_path(lease_id), record)
            stay_until = _mode_deadline(record.get("stay_until_at"))
            return {
                "status": "claimed",
                "lease_id": lease_id,
                "probe_id": probe_id,
                "owner_label": record.get("owner_label") or "GPT-PC 会话",
                "workspace": record.get("workspace"),
                "heartbeat_at": heartbeat_text,
                "probe_sent_at": probe.get("sent_at"),
                "probe_delivered_at": probe.get("delivered_at"),
                "probe_response_deadline_at": probe.get("response_deadline_at"),
                "stay_until_at": record.get("stay_until_at"),
                "resident_active": bool(stay_until and stay_until > now),
                "age_seconds": age_seconds,
            }

        # Strict policy: only an explicit probe + confirmed disconnect may
        # ever produce an external disconnect email.
        return {"status": "disconnect_not_confirmed", "lease_id": lease_id}


def complete_disconnect_alert(
    lease_id: str,
    heartbeat_at: str,
    *,
    success: bool,
    probe_id: str | None = None,
    retry_after_seconds: int = 0,
) -> dict[str, Any]:
    """Complete or release a disconnect notification claim; failed sends remain retryable."""
    if isinstance(retry_after_seconds, bool) or not isinstance(retry_after_seconds, int) or not 0 <= retry_after_seconds <= 86400:
        raise RuntimePolicyError("参数无效", "掉线通知失败重试退避需为 0 至 86400 秒")
    with _metadata_lock():
        path = _lease_path(lease_id)
        if not path.is_file():
            return {"status": "lease_gone", "lease_id": lease_id}
        record = _read_json(path)
        if not isinstance(record, dict):
            return {"status": "lease_gone", "lease_id": lease_id}
        clean_probe_id = str(probe_id or "").strip()
        if clean_probe_id:
            if record.get("disconnect_alert_pending_probe_id") != clean_probe_id:
                return {"status": "stale_claim", "lease_id": lease_id, "probe_id": clean_probe_id}
            record.pop("disconnect_alert_pending_probe_id", None)
            record.pop("disconnect_alert_claimed_at", None)
            if record.get("disconnect_probe_program_watchdog_direct_id") == clean_probe_id:
                record.pop("disconnect_probe_program_watchdog_direct_id", None)
                record.pop("disconnect_probe_program_watchdog_direct_at", None)
            if success:
                record.pop("disconnect_alert_retry_after_at", None)
                record["disconnect_alert_sent_probe_id"] = clean_probe_id
                record["disconnect_alert_sent_heartbeat_at"] = heartbeat_at
                record["disconnect_alert_sent_at"] = utc_iso()
            elif retry_after_seconds:
                record["disconnect_alert_retry_after_at"] = utc_iso(
                    utc_now() + timedelta(seconds=retry_after_seconds)
                )
            else:
                record.pop("disconnect_alert_retry_after_at", None)
            _write_json(path, record)
            return {
                "status": "sent" if success else "retryable",
                "lease_id": lease_id,
                "probe_id": clean_probe_id,
                "heartbeat_at": heartbeat_at,
                "retry_after_at": record.get("disconnect_alert_retry_after_at"),
            }
        if record.get("disconnect_alert_pending_heartbeat_at") != heartbeat_at:
            return {"status": "stale_claim", "lease_id": lease_id}
        record.pop("disconnect_alert_pending_heartbeat_at", None)
        record.pop("disconnect_alert_claimed_at", None)
        if success:
            record.pop("disconnect_alert_retry_after_at", None)
            record["disconnect_alert_sent_heartbeat_at"] = heartbeat_at
            record["disconnect_alert_sent_at"] = utc_iso()
        elif retry_after_seconds:
            record["disconnect_alert_retry_after_at"] = utc_iso(
                utc_now() + timedelta(seconds=retry_after_seconds)
            )
        else:
            record.pop("disconnect_alert_retry_after_at", None)
        _write_json(path, record)
        return {
            "status": "sent" if success else "retryable",
            "lease_id": lease_id,
            "heartbeat_at": heartbeat_at,
            "retry_after_at": record.get("disconnect_alert_retry_after_at"),
        }


def _recovery_runtime_task(record: dict[str, Any]) -> dict[str, Any]:
    lease_id = str(record.get("lease_id") or "")
    active_jobs = [
        {
            "job_id": item.get("job_id"),
            "name": item.get("name"),
            "pid": item.get("pid"),
            "status": "running",
        }
        for item in _running_job_records_locked()
        if str(item.get("lease_id") or "") == lease_id
    ]
    checkpoint = _execution_checkpoint(record)
    instruction = (
        "这是唯一可恢复的活动执行任务。只有确认 workspace/owner 与当前中断任务一致时，才直接复用 lease_id 重试原工具；"
        "不要重新 acquire、不要重新规划整个项目。"
    )
    if checkpoint["has_execution_history"]:
        instruction += (
            " Runtime 已持久记录该任务此前发生过执行活动；不得声称‘尚未开始/没有调用电脑/没有执行工具’。"
            "若不确定具体代码或测试状态，先读取 Git、Runtime 和相关日志重新建立事实。"
        )
    goal = goal_mode_state(record, include_details=False)
    if goal.get("active"):
        instruction += (
            " 当前目标模式已开启：把 goal_mode.outcome 作为持续目标，把 success_criteria 作为完成判据；"
            "goal_mode.requirements 是持续生效的新增要求，goal_mode.corrections 是后续修正历史，始终以 revision 最大的当前版本为准；"
            "在这些条件未满足前继续工作，不要把中间阶段当成完成。"
            "每轮决策先识别当前可并发前沿；同一阶段已有多个互不依赖的读取、搜索、状态、Git只读、导航或验证动作时，默认同轮批量发出，"
            "只有数据依赖、同资源写冲突、资源锁或控制门禁才串行。"
            "若 reuse_visual_context=true 且看图模式开启，同一任务的视觉参考、要求与观察证据同时属于目标验收上下文。"
        )
    ultra = ultra_mode_state(record)
    if ultra.get("active"):
        instruction += (
            " 当前 Ultra 子Agent模式已开启：明显多阶段、可独立拆分的长任务应先识别独立模块，并按实际需要尽早建立 1–3 个长期 Child 与主线并行，不能把 Ultra 仅当作允许开关；"
            "执行/验证类 Child 优先 high + responsiveness，复杂独立审计再用 extra_high；已有合适 Child 时优先在同一 Conversation followup 热复用，避免重复冷启动。"
            "普通/单步或强数据依赖、确实不可拆的任务不要为了凑并行而拆分；每次 spawn 必须说明独立 delegation_reason，禁止无界派生；"
            "共享 Root resource lease 下的 Child 继承同一 Ultra 状态并用自己的 participant handle 继续操作，只有显式不同 workspace 的独立 Child 才维护自己的 Ultra 状态。"
        )
    elif ultra.get("user_mode") == "disabled":
        instruction += " 用户已明确关闭当前 lease 的 Ultra；不得自行反开或 spawn 子 Agent。"
    stay_deadline = _mode_deadline(record.get("stay_until_at"))
    lease_role = str(record.get("lease_role") or LEASE_ROLE_OWNER)
    shared_root_lease_id = str(record.get("shared_root_lease_id") or "") or None
    controller_id = lease_controller_id(record)
    session_generation = int(record.get("session_generation") or 1) if lease_role == LEASE_ROLE_PARTICIPANT else None
    recovery_action = {
        "action": "RECOVER_EXISTING_TASK",
        "lease_id": lease_id,
        "controller_id": controller_id,
        "web_conversation_id": record.get("web_conversation_id"),
        "web_conversation_url": record.get("web_conversation_url"),
        "lease_role": lease_role,
        "shared_root_lease_id": shared_root_lease_id,
        "resource_lease_owner": lease_role != LEASE_ROLE_PARTICIPANT,
        "workspace": record.get("workspace"),
        "thread_id": record.get("thread_id") or lease_id,
        "session_generation": session_generation,
        "owner": record.get("owner_label"),
        "stay": {"until_at": record.get("stay_until_at"), "active": bool(stay_deadline and stay_deadline > utc_now())},
        "visual": visual_mode_state(record, include_details=False),
        "goal": goal,
        "ultra": ultra,
        "successful_action_count": checkpoint["successful_action_count"],
        "side_effect_action_count": checkpoint["side_effect_action_count"],
        "active_jobs": active_jobs,
        "last_progress": {
            "phase": checkpoint.get("last_progress_phase"),
            "text": checkpoint.get("last_progress_text"),
            "at": checkpoint.get("last_progress_at"),
        },
        "directives": [
            "DO_NOT_RESTART",
            "DO_NOT_REACQUIRE",
            "DO_NOT_LABEL_AS_FOREIGN_WITHOUT_IDENTITY_PROOF",
            "PARTICIPANTS_DO_NOT_OWN_ROOT_RESOURCE",
            *(["DO_NOT_CLAIM_NO_WORK"] if checkpoint["has_execution_history"] else []),
        ],
    }
    ownership_semantics = {
        "controller_id": controller_id,
        "lease_role": lease_role,
        "resource_lease_owner": lease_role != LEASE_ROLE_PARTICIPANT,
        "lease_family_root_id": shared_root_lease_id or lease_id,
        "participant_does_not_transfer_root_ownership": True,
        "active_lease_alone_is_not_foreign_task_proof": True,
    }
    return {
        "lease_active": True,
        "lease_id": lease_id,
        "controller_id": controller_id,
        "web_conversation_id": record.get("web_conversation_id"),
        "web_conversation_url": record.get("web_conversation_url"),
        "lease_role": lease_role,
        "shared_root_lease_id": shared_root_lease_id,
        "resource_lease_owner": lease_role != LEASE_ROLE_PARTICIPANT,
        "thread_id": record.get("thread_id") or lease_id,
        "session_generation": session_generation,
        "workspace": record.get("workspace"),
        "scope_root": record.get("scope_root") or record.get("workspace"),
        "lease_type": record.get("lease_type", LEASE_TYPE_WORKSPACE),
        "mode": record.get("mode"),
        "owner_label": record.get("owner_label"),
        "runtime_instance": record.get("runtime_instance"),
        "created_at": record.get("created_at"),
        "renewed_at": record.get("renewed_at"),
        "expires_at": record.get("expires_at"),
        "completion_handshake_required": True,
        "control_ack_required": bool(record.get("control_ack_pending_id")),
        "control_resume_required": bool(record.get("control_resume_pending_id")),
        "execution_checkpoint": checkpoint,
        "stay_until_at": record.get("stay_until_at"),
        "visual_mode": visual_mode_state(record, include_details=False),
        "goal_mode": goal_mode_state(record, include_details=False),
        "ultra_mode": ultra_mode_state(record),
        "active_jobs": active_jobs,
        "ownership_semantics": ownership_semantics,
        "recovery_action": recovery_action,
        "recovery_instruction": instruction,
    }


def recover_execution_context(targets: list[Path]) -> dict[str, Any]:
    """Return a unique active execute lease that can cover the requested targets.

    When a blocked tool has no path argument (for example a lost-handle progress
    call), a single global execute lease is still recoverable. Multiple active
    leases remain ambiguous and fail closed.
    """
    normalized_targets = [target.resolve(strict=False) for target in targets]
    with _metadata_lock():
        records = [record for _path, record in _lease_records_locked()]
        candidates: list[dict[str, Any]] = []
        for record in records:
            if _is_lease_participant(record):
                continue
            if str(record.get("mode") or "") != "execute_exclusive":
                continue
            scope_text = str(record.get("scope_root") or record.get("workspace") or "").strip()
            if not scope_text:
                continue
            scope_root = Path(scope_text).resolve(strict=False)
            if not normalized_targets or all(
                _path_is_within(target, scope_root) for target in normalized_targets
            ):
                candidates.append(record)
        if len(candidates) == 1:
            return {
                "candidate_count": 1,
                "runtime_task": _recovery_runtime_task(candidates[0]),
            }
        return {
            "candidate_count": len(candidates),
            "candidates": [
                {
                    "workspace": record.get("workspace"),
                    "lease_type": record.get("lease_type", LEASE_TYPE_WORKSPACE),
                    "mode": record.get("mode"),
                    "owner_label": record.get("owner_label"),
                }
                for record in candidates[:8]
            ],
        }


def workspace_status(
    workspace: Path | None = None,
    *,
    include_private: bool = False,
) -> dict[str, Any]:
    normalized = workspace.resolve(strict=False) if workspace is not None else None
    all_records = [record for _path, record in _lease_records_snapshot()]
    participants = [record for record in all_records if _is_lease_participant(record)]
    records = [record for record in all_records if not _is_lease_participant(record)]
    if normalized is not None:
        participants = [
            record for record in participants
            if _paths_overlap(normalized, Path(str(record.get("scope_root") or record.get("workspace", ""))))
        ]
        records = [
            record
            for record in records
            if _paths_overlap(
                normalized,
                Path(str(record.get("scope_root") or record.get("workspace", ""))),
            )
        ]
    safe_records: list[dict[str, Any]] = []
    for record in records:
        root_id = str(record.get("lease_id") or "")
        root_participants = [
            participant for participant in participants
            if str(participant.get("shared_root_lease_id") or "") == root_id
        ]
        item = {
            "lease_id": record.get("lease_id"),
            "lease_role": LEASE_ROLE_OWNER,
            "resource_lease_owner": True,
            "shared_root_lease_id": None,
            "workspace": record.get("workspace"),
            "scope_root": record.get("scope_root") or record.get("workspace"),
            "lease_type": record.get("lease_type", LEASE_TYPE_WORKSPACE),
            "mode": record.get("mode"),
            "owner_label": record.get("owner_label"),
            "web_conversation_id": record.get("web_conversation_id"),
            "web_conversation_url": record.get("web_conversation_url"),
            "created_at": record.get("created_at"),
            "renewed_at": record.get("renewed_at"),
            "expires_at": record.get("expires_at"),
            "stay_started_at": record.get("stay_started_at"),
            "stay_until_at": record.get("stay_until_at"),
            "stay_duration_seconds": record.get("stay_duration_seconds"),
            "visual_mode": visual_mode_state(record, include_details=False),
            "goal_mode": goal_mode_state(record, include_details=False),
            "ultra_mode": ultra_mode_state(record),
            "control_ack_required": bool(record.get("control_ack_pending_id")),
            "control_resume_required": bool(record.get("control_resume_pending_id")),
            "execution_checkpoint": _execution_checkpoint(record),
            "participant_count": len(root_participants),
            "participant_semantics": (
                "participant 只共享此 Root resource lease；不会取得 Root 所有权，也不会把 Root 变成‘另一个对话的租约’。"
            ),
        }
        if include_private:
            item["thread_id"] = record.get("thread_id") or record.get("lease_id")
            item["runtime_instance"] = record.get("runtime_instance")
            item["control_cursor_id"] = record.get("control_cursor_id")
            item["control_ack_pending_id"] = record.get("control_ack_pending_id")
            item["control_resume_pending_id"] = record.get("control_resume_pending_id")
            item["participants"] = [
                {
                    "lease_id": participant.get("lease_id"),
                    "thread_id": participant.get("thread_id"),
                    "logical_agent_id": participant.get("logical_agent_id"),
                    "owner_label": participant.get("owner_label"),
                    "shared_parent_lease_id": participant.get("shared_parent_lease_id"),
                    "execution_checkpoint": _execution_checkpoint(participant),
                }
                for participant in root_participants
            ]
        safe_records.append(item)
    result: dict[str, Any] = {"leases": safe_records, "count": len(safe_records)}
    if normalized is not None:
        execute_records = [
            record for record in records if str(record.get("mode") or "") == "execute_exclusive"
        ]
        if len(execute_records) == 1:
            result["recovery"] = {
                "status": "unique_active_execute_lease",
                "action": "RECOVER_EXISTING_TASK",
                "runtime_task": _recovery_runtime_task(execute_records[0]),
                "decision_gate": {
                    "active_lease_is_not_foreign_task_proof": True,
                    "runtime_handle_mismatch_is_not_conversation_proof": True,
                    "match_workspace_and_task_before_calling_foreign": True,
                    "participant_count_does_not_transfer_ownership": True,
                },
                "instruction": (
                    "先判断这是否就是当前 GPT 中断前的原任务：workspace、owner_label、Goal/最近进展与当前用户要求一致时，"
                    "必须直接复用 runtime_task.lease_id；不得仅因 lease/controller/thread 变化就改判为外部任务，也不得再次 acquire。"
                    "只有存在明确不同的网页 conversation/Agent 证据或任务事实明显不一致时，才按其他任务只读/等待。"
                ),
            }
    return result


def _apply_execution_checkpoint_update(
    record: dict[str, Any],
    *,
    clean_tool: str,
    side_effect: bool,
    clean_phase: str,
    clean_text: str,
) -> None:
    now = utc_iso()
    if clean_tool:
        record["execution_successful_action_count"] = int(
            record.get("execution_successful_action_count") or 0
        ) + 1
        record["execution_last_tool"] = clean_tool
        record["execution_last_success_at"] = now
        if side_effect:
            record["execution_side_effect_action_count"] = int(
                record.get("execution_side_effect_action_count") or 0
            ) + 1
            record["execution_last_side_effect_tool"] = clean_tool
            record["execution_last_side_effect_at"] = now
    if clean_phase or clean_text:
        record["execution_last_progress_phase"] = clean_phase or "progress"
        record["execution_last_progress_text"] = clean_text
        record["execution_last_progress_at"] = now
    record["execution_checkpoint_updated_at"] = now


def _execution_overlay_fields(record: dict[str, Any]) -> dict[str, Any]:
    return {
        key: value
        for key, value in record.items()
        if key.startswith("execution_")
    }


def update_workspace_execution_checkpoint(
    lease_id: str,
    *,
    tool_name: str | None = None,
    side_effect: bool = False,
    progress_phase: str | None = None,
    progress_text: str | None = None,
) -> dict[str, Any]:
    """Update a bounded model-visible checkpoint with burst coalescing for reads.

    The first read action is persisted synchronously so recovery history exists.
    Subsequent read/search/Git-read/navigation actions are merged in memory and
    become durable at the next progress/control/side-effect checkpoint. This
    removes same-lease JSON rewrite contention without weakening completion or
    mutation durability.
    """
    clean_tool = str(tool_name or "").strip()[:128]
    clean_phase = str(progress_phase or "").strip().lower()[:32]
    clean_text = str(progress_text or "").strip()[:1200]
    coalesced_read = bool(
        clean_tool in _COALESCED_CHECKPOINT_TOOLS
        and not side_effect
        and not clean_phase
        and not clean_text
    )

    if coalesced_read:
        path = _lease_path(lease_id)
        disk_record = _read_json(path)
        if disk_record is None:
            raise RuntimePolicyError("租约不存在", "工作区租约不存在或已经过期", retryable=True)
        try:
            if _parse_utc(str(disk_record["expires_at"])) <= utc_now():
                raise RuntimePolicyError("租约已过期", "工作区租约已经过期，请重新申请", retryable=True)
        except (KeyError, TypeError, ValueError) as exc:
            raise RuntimePolicyError("租约无效", "工作区租约记录不完整") from exc
        with _CHECKPOINT_OVERLAY_LOCK:
            record = {**disk_record, **_CHECKPOINT_OVERLAYS.get(lease_id, {})}
            first_persisted_action = not bool(disk_record.get("execution_checkpoint_updated_at"))
            _apply_execution_checkpoint_update(
                record,
                clean_tool=clean_tool,
                side_effect=False,
                clean_phase="",
                clean_text="",
            )
            _CHECKPOINT_OVERLAYS[lease_id] = _execution_overlay_fields(record)
        if not first_persisted_action:
            return _execution_checkpoint(record)
        # Establish the first durable execution history record for recovery.

    with _metadata_lock():
        path = _lease_path(lease_id)
        record = _read_mode_lease_record_locked(lease_id)
        if record is None:
            raise RuntimePolicyError("租约不存在", "工作区租约不存在或已经过期", retryable=True)
        try:
            expires_at = _parse_utc(str(record["expires_at"]))
        except (KeyError, TypeError, ValueError) as exc:
            raise RuntimePolicyError("租约无效", "工作区租约记录不完整") from exc
        if expires_at <= utc_now():
            path.unlink(missing_ok=True)
            raise RuntimePolicyError("租约已过期", "工作区租约已经过期，请重新申请", retryable=True)
        with _CHECKPOINT_OVERLAY_LOCK:
            overlay = _CHECKPOINT_OVERLAYS.pop(lease_id, None)
            if overlay:
                record.update(overlay)
            if not coalesced_read:
                _apply_execution_checkpoint_update(
                    record,
                    clean_tool=clean_tool,
                    side_effect=side_effect,
                    clean_phase=clean_phase,
                    clean_text=clean_text,
                )
            _write_json(path, record)
    return _execution_checkpoint(record)


def update_workspace_control_cursor(lease_id: str, cursor_id: str | None) -> dict[str, Any]:
    clean_cursor = cursor_id.strip() if isinstance(cursor_id, str) else ""
    with _metadata_lock():
        path = _lease_path(lease_id)
        record = _read_mode_lease_record_locked(lease_id)
        if record is None:
            raise RuntimePolicyError("租约不存在", "工作区租约不存在或已经过期", retryable=True)
        try:
            expires_at = _parse_utc(str(record["expires_at"]))
        except (KeyError, TypeError, ValueError) as exc:
            raise RuntimePolicyError("租约无效", "工作区租约记录不完整") from exc
        if expires_at <= utc_now():
            path.unlink(missing_ok=True)
            raise RuntimePolicyError("租约已过期", "工作区租约已经过期，请重新申请", retryable=True)
        if clean_cursor:
            record["control_cursor_id"] = clean_cursor[:256]
            record["control_cursor_at"] = utc_iso()
        else:
            record.pop("control_cursor_id", None)
            record.pop("control_cursor_at", None)
        _write_json(path, record)
    return record


def update_workspace_control_ack_pending(lease_id: str, message_id: str | None) -> dict[str, Any]:
    """Persist whether consumed Console input still needs a visible Agent reply."""
    clean_message_id = message_id.strip() if isinstance(message_id, str) else ""
    with _metadata_lock():
        path = _lease_path(lease_id)
        record = _read_mode_lease_record_locked(lease_id)
        if record is None:
            raise RuntimePolicyError("租约不存在", "工作区租约不存在或已经过期", retryable=True)
        try:
            expires_at = _parse_utc(str(record["expires_at"]))
        except (KeyError, TypeError, ValueError) as exc:
            raise RuntimePolicyError("租约无效", "工作区租约记录不完整") from exc
        if expires_at <= utc_now():
            path.unlink(missing_ok=True)
            raise RuntimePolicyError("租约已过期", "工作区租约已经过期，请重新申请", retryable=True)
        if clean_message_id:
            record["control_ack_pending_id"] = clean_message_id[:256]
            record["control_ack_pending_at"] = utc_iso()
        else:
            record.pop("control_ack_pending_id", None)
            record.pop("control_ack_pending_at", None)
        _write_json(path, record)
    return record


def update_workspace_control_resume_pending(lease_id: str, message_id: str | None) -> dict[str, Any]:
    """Persist whether a handled Console interruption still needs actual task resumption."""
    clean_message_id = message_id.strip() if isinstance(message_id, str) else ""
    with _metadata_lock():
        path = _lease_path(lease_id)
        record = _read_mode_lease_record_locked(lease_id)
        if record is None:
            raise RuntimePolicyError("租约不存在", "工作区租约不存在或已经过期", retryable=True)
        try:
            expires_at = _parse_utc(str(record["expires_at"]))
        except (KeyError, TypeError, ValueError) as exc:
            raise RuntimePolicyError("租约无效", "工作区租约记录不完整") from exc
        if expires_at <= utc_now():
            path.unlink(missing_ok=True)
            raise RuntimePolicyError("租约已过期", "工作区租约已经过期，请重新申请", retryable=True)
        if clean_message_id:
            record["control_resume_pending_id"] = clean_message_id[:256]
            record["control_resume_pending_at"] = utc_iso()
        else:
            record.pop("control_resume_pending_id", None)
            record.pop("control_resume_pending_at", None)
        _write_json(path, record)
    return record


def require_execute_lease(
    lease_id: str | None,
    targets: list[Path],
    *,
    descendant_targets: list[Path] | None = None,
) -> dict[str, Any]:
    if not lease_id:
        target_workspace = str(targets[0].resolve(strict=False)) if targets else None
        recovery = recover_execution_context(targets)
        details: dict[str, Any] = {
            "target_workspace": target_workspace,
            "recommended_action": "reuse_existing_lease_or_acquire",
        }
        message = "需要工作区租约：写入、Git、命令和进程操作前必须提供执行工作区租约。"
        if recovery.get("candidate_count") == 1:
            details["recoverable_runtime_task"] = recovery["runtime_task"]
            details["recommended_action"] = "retry_same_tool_with_recoverable_lease_id"
            message += (
                " Runtime 检测到唯一可覆盖目标的活动执行任务；若它就是本对话中断前的原任务，"
                "直接使用 recoverable_runtime_task.lease_id 重试同一个工具，不要重新 acquire。"
            )
        elif int(recovery.get("candidate_count") or 0) > 1:
            details["recovery_candidate_count"] = recovery["candidate_count"]
            details["recovery_candidates"] = recovery.get("candidates", [])
            details["recommended_action"] = "workspace_status_then_choose_or_wait"
            message += " 存在多个可能的活动任务，拒绝自动猜测；先按目标路径查看 workspace_status。"
        else:
            message += " 当前没有可恢复活动任务；正常 acquire_workspace。"
        raise RuntimePolicyError(
            "需要工作区租约",
            message,
            retryable=True,
            error_type="LEASE_REQUIRED",
            details=details,
        )
    recursive_targets = {path.resolve(strict=False) for path in descendant_targets or []}
    with _metadata_lock():
        # One execution authorization path for both Root leases and shared
        # Conversation participants.  Participant expiry/policy resolves through
        # its authoritative Root before any workspace scope check.
        record = _active_mode_lease_locked(lease_id)
        _enforce_shared_participant_mutation_scope_locked(record, targets)
        workspace = Path(str(record.get("workspace", ""))).resolve(strict=False)
        lease_type = str(record.get("lease_type", LEASE_TYPE_WORKSPACE))
        if lease_type == LEASE_TYPE_RUNTIME_ADMIN:
            scope_root = PROJECT_ROOT.resolve(strict=False)
            protected_scopes = [
                {
                    "lease_id": str(other.get("lease_id") or ""),
                    "lease_type": str(other.get("lease_type", LEASE_TYPE_WORKSPACE)),
                    "workspace": Path(str(other.get("workspace", ""))).resolve(strict=False),
                    "owner_label": str(other.get("owner_label") or ""),
                }
                for _path, other in _lease_records_locked()
                if str(other.get("lease_id") or "") != lease_id
                and str(other.get("mode") or "") == "execute_exclusive"
                and str(other.get("lease_type", LEASE_TYPE_WORKSPACE))
                in {LEASE_TYPE_WORKSPACE, LEASE_TYPE_DESKTOP_OPS}
            ]
            for target in targets:
                normalized = target.resolve(strict=False)
                if not _path_is_within(normalized, scope_root):
                    raise RuntimePolicyError(
                        "管理员租约越界",
                        f"运行底座管理员租约只允许操作 {scope_root}：{normalized}",
                    )
                conflict = next(
                    (
                        item for item in protected_scopes
                        if _path_is_within(normalized, item["workspace"])
                        or (normalized in recursive_targets and _path_is_within(item["workspace"], normalized))
                    ),
                    None,
                )
                if conflict is not None:
                    raise RuntimePolicyError(
                        "资源归属冲突",
                        "管理员租约可与项目/桌面租约并存，但默认不能修改其他活跃执行租约持有的资源",
                        retryable=True,
                        error_type="RESOURCE_OWNERSHIP_CONFLICT",
                        details={
                            "target": str(normalized),
                            "conflicting_lease_id": conflict["lease_id"],
                            "conflicting_lease_type": conflict["lease_type"],
                            "conflicting_workspace": str(conflict["workspace"]),
                            "owner_label": conflict["owner_label"],
                            "recommended_action": "use_owning_lease_or_wait_for_resource",
                        },
                    )
            return record
        for target in targets:
            normalized = target.resolve(strict=False)
            if any(
                _path_is_within(normalized, root)
                or (normalized in recursive_targets and _path_is_within(root, normalized))
                for root in _PROTECTED_RUNTIME_ROOTS
            ):
                raise RuntimePolicyError("受保护运行目录", f"普通执行任务不能修改运行底座：{normalized}")
            if not _path_is_within(normalized, workspace):
                raise RuntimePolicyError(
                    "工作区越界",
                    f"目标不在当前工作区租约内：{normalized}",
                )
    return record


def _job_process_state(record: dict[str, Any]) -> tuple[psutil.Process | None, str, bool]:
    """Resolve a job PID only when it still identifies the original process."""

    pid = int(record.get("pid", 0) or 0)
    if pid <= 0:
        return None, "exited", False
    try:
        process = psutil.Process(pid)
    except (psutil.NoSuchProcess, ValueError):
        return None, "exited", False
    except psutil.AccessDenied:
        return None, "identity_unknown", True

    try:
        if not process.is_running() or process.status() == psutil.STATUS_ZOMBIE:
            return None, "exited", False
        actual_create_time = float(process.create_time())
    except psutil.NoSuchProcess:
        return None, "exited", False
    except (psutil.AccessDenied, ValueError):
        return None, "identity_unknown", True

    expected_create_time = record.get("process_create_time")
    if expected_create_time is None:
        # Legacy records predate process identity capture.  They are never safe
        # to terminate.  A clearly different start time can still be reported
        # as PID reuse instead of pretending the old job is alive.
        started_at = record.get("started_at")
        if isinstance(started_at, str):
            try:
                started_timestamp = _parse_utc(started_at).timestamp()
                if abs(actual_create_time - started_timestamp) > LEGACY_JOB_START_TOLERANCE_SECONDS:
                    return None, "pid_reused", True
            except (OverflowError, ValueError):
                pass
        return None, "identity_unknown", True

    try:
        expected_timestamp = float(expected_create_time)
    except (TypeError, ValueError):
        return None, "identity_unknown", True
    if abs(actual_create_time - expected_timestamp) > PROCESS_CREATE_TIME_TOLERANCE_SECONDS:
        return None, "pid_reused", True
    return process, "running", True


def _job_record_state(record: dict[str, Any]) -> dict[str, Any]:
    result = dict(record)
    _process, status, pid_present = _job_process_state(record)
    result["status"] = status
    result["pid_alive"] = status == "running"
    result["pid_present"] = pid_present
    return result


def create_job(
    *,
    workspace_lease: dict[str, Any],
    argv: list[str] | str,
    cwd: Path,
    shell: str,
    name: str | None = None,
    log_path: Path | None = None,
) -> dict[str, Any]:
    _ensure_runtime_dirs()
    job_id = uuid.uuid4().hex
    resolved_log_path = log_path or (JOB_LOG_DIR / f"job-{job_id}.log")
    resolved_log_path.parent.mkdir(parents=True, exist_ok=True)
    creationflags = 0
    if os.name == "nt":
        creationflags = subprocess.CREATE_NEW_PROCESS_GROUP | subprocess.CREATE_NO_WINDOW
    handle = resolved_log_path.open("ab")
    try:
        process = subprocess.Popen(
            argv,
            cwd=str(cwd),
            stdin=subprocess.DEVNULL,
            stdout=handle,
            stderr=subprocess.STDOUT,
            creationflags=creationflags,
        )
    finally:
        handle.close()
    now = utc_iso()
    try:
        process_create_time: float | None = float(psutil.Process(process.pid).create_time())
    except (psutil.NoSuchProcess, psutil.AccessDenied, ValueError):
        process_create_time = None
    record = {
        "job_id": job_id,
        "pid": process.pid,
        "process_create_time": process_create_time,
        "cwd": str(cwd),
        "workspace": workspace_lease["workspace"],
        "lease_type": workspace_lease.get("lease_type", LEASE_TYPE_WORKSPACE),
        "lease_id": workspace_lease["lease_id"],
        "owner_label": workspace_lease.get("owner_label", "未命名任务"),
        "name": (name or "后台任务").strip()[:200] or "后台任务",
        "shell": shell,
        "log_file": str(resolved_log_path),
        "started_at": now,
        "updated_at": now,
    }
    with _metadata_lock():
        _write_json(_job_path(job_id), record)
        _set_active_job_index_record_locked(record)
    return _job_record_state(record)


def adopt_job_child(
    job_id: str,
    *,
    workspace_lease_id: str,
    child_pid: int,
    expected_executable: str,
    expected_arguments: list[str],
) -> dict[str, Any]:
    """Internal launcher handoff, not a tool or a second Job registry.

    Chrome may exit its launcher and retain a directly spawned browser root.
    Only the same lease's real, identity-checked direct child may replace that
    Job's PID. An arbitrary existing process is never adoptable.
    """
    with _metadata_lock():
        lease = _read_mode_lease_record_locked(workspace_lease_id)
        record = _read_json(_job_path(job_id))
        if not lease or not record or record.get("lease_id") != workspace_lease_id:
            raise RuntimePolicyError("托管启动器身份无效", "只能接管当前活跃租约自己创建的进程")
        if not expected_arguments:
            raise RuntimePolicyError("缺少子进程参数证明", "接管需要固定的专用进程参数")
        try:
            child = psutil.Process(child_pid)
            created = float(child.create_time())
            old_pid = int(record["pid"])
            old_created = float(record["process_create_time"])
            if child_pid == old_pid and abs(created - old_created) <= PROCESS_CREATE_TIME_TOLERANCE_SECONDS:
                return _job_record_state(record)
            if child.ppid() != old_pid or not 0 <= created - old_created <= 30:
                raise RuntimePolicyError("子进程归属不匹配", "目标不是该启动器在启动窗口内创建的直接子进程")
            if psutil.pid_exists(old_pid):
                actual_parent_created = float(psutil.Process(old_pid).create_time())
                if abs(actual_parent_created - old_created) > PROCESS_CREATE_TIME_TOLERANCE_SECONDS:
                    raise RuntimePolicyError("启动器PID已复用", "拒绝跨进程生命周期接管")
            if os.path.normcase(os.path.abspath(child.exe())) != os.path.normcase(os.path.abspath(expected_executable)):
                raise RuntimePolicyError("子进程可执行文件不匹配", "拒绝接管其他程序")
            command = child.cmdline()
            if any(argument not in command for argument in expected_arguments):
                raise RuntimePolicyError("子进程参数不匹配", "拒绝接管其他profile或端口的进程")
        except (psutil.NoSuchProcess, psutil.AccessDenied, KeyError, TypeError, ValueError) as exc:
            raise RuntimePolicyError("子进程身份不可核验", "没有修改托管记录，请重新读取真实进程状态") from exc
        record.update(
            launcher_pid=old_pid,
            launcher_process_create_time=old_created,
            pid=child_pid,
            process_create_time=created,
            launcher_handoff_verified=True,
            updated_at=utc_iso(),
        )
        _write_json(_job_path(job_id), record)
        _set_active_job_index_record_locked(record)
        return _job_record_state(record)


def get_job(job_id: str) -> dict[str, Any]:
    with _metadata_lock():
        record = _read_json(_job_path(job_id))
        if record is None:
            known_active_jobs = [
                str(item.get("job_id") or "")
                for item in _running_job_records_locked()
                if item.get("job_id")
            ]
            raise RuntimePolicyError(
                "任务不存在",
                "job_id 未找到。job_id 必须来自 start_job 或后台 start_process 的返回值；不要把任务名、占位符或文件路径当作 job_id",
                error_type="JOB_NOT_FOUND",
                details={
                    "job_id": job_id,
                    "job_id_sources": ["start_job", "background start_process"],
                    "known_active_jobs": known_active_jobs,
                },
            )
        state = _job_record_state(record)
        if state.get("status") == "running":
            _set_active_job_index_record_locked(record)
        else:
            _drop_active_job_index_record_locked(job_id)
        return state


def list_active_jobs(workspace: Path | None = None) -> dict[str, Any]:
    normalized = workspace.resolve(strict=False) if workspace is not None else None
    # Read-only hot path: the in-process lock is sufficient because Job JSON
    # files are atomically replaced and the active index rechecks mtimes.
    with _METADATA_THREAD_LOCK:
        records = _running_job_records_locked()
        if normalized is not None:
            records = [
                record
                for record in records
                if _paths_overlap(normalized, Path(str(record.get("workspace", ""))))
            ]
        states = [
            {**record, "status": "running", "pid_alive": True, "pid_present": True}
            for record in records
        ]
    states.sort(key=lambda item: str(item.get("started_at", "")), reverse=True)
    return {"jobs": states, "count": len(states)}


def job_history_count() -> int:
    return sum(1 for _path in JOB_DIR.glob("job-*.json"))


def list_jobs(workspace: Path | None = None) -> dict[str, Any]:
    records: list[dict[str, Any]] = []
    normalized = workspace.resolve(strict=False) if workspace is not None else None
    for path in JOB_DIR.glob("job-*.json"):
        record = _read_json(path)
        if record is None:
            continue
        if normalized is not None and not _paths_overlap(normalized, Path(str(record.get("workspace", "")))):
            continue
        records.append(_job_record_state(record))
    records.sort(key=lambda item: str(item.get("started_at", "")), reverse=True)
    return {"jobs": records, "count": len(records)}


def read_job_tail(job_id: str, *, max_bytes: int = 64 * 1024) -> dict[str, Any]:
    record = get_job(job_id)
    log_path = Path(str(record["log_file"]))
    if not log_path.exists():
        return {"job": record, "content": "", "truncated": False}
    max_bytes = max(1, min(int(max_bytes), 512 * 1024))
    with log_path.open("rb") as handle:
        size = log_path.stat().st_size
        if size > max_bytes:
            handle.seek(-max_bytes, os.SEEK_END)
            truncated = True
        else:
            truncated = False
        data = handle.read()
    return {
        "job": record,
        "content": data.decode("utf-8", errors="replace"),
        "truncated": truncated,
    }


def cancel_job(job_id: str, *, workspace_lease_id: str, force: bool = False) -> dict[str, Any]:
    record = get_job(job_id)
    lease = require_execute_lease(workspace_lease_id, [Path(str(record["workspace"]))])
    if lease["workspace"] != record.get("workspace"):
        raise RuntimePolicyError("工作区越界", "该后台任务不属于当前工作区租约")
    pid = int(record.get("pid", 0) or 0)
    process, process_status, pid_present = _job_process_state(record)
    if process_status == "exited":
        return {"status": "already_exited", "job_id": job_id, "pid": pid}
    if process is None:
        return {
            "status": process_status,
            "job_id": job_id,
            "pid": pid,
            "pid_present": pid_present,
            "requested": [],
            "alive": [],
        }
    processes = process.children(recursive=True) + [process]
    requested: list[int] = []
    for item in reversed(processes):
        try:
            (item.kill() if force else item.terminate())
            requested.append(item.pid)
        except (psutil.NoSuchProcess, psutil.AccessDenied):
            pass
    _gone, alive = psutil.wait_procs(processes, timeout=5)
    if force:
        for item in alive:
            try:
                item.kill()
            except (psutil.NoSuchProcess, psutil.AccessDenied):
                pass
    return {
        "status": "stopped" if not alive else "stop_requested",
        "job_id": job_id,
        "pid": pid,
        "requested": requested,
        "alive": [item.pid for item in alive],
    }


def job_for_pid(pid: int) -> dict[str, Any] | None:
    with _metadata_lock():
        for record in _running_job_records_locked():
            if int(record.get("pid", 0) or 0) != pid:
                continue
            return {**record, "status": "running", "pid_alive": True, "pid_present": True}
    return None


# Resident/Visual mode policy is part of this Runtime, not a second scheduler.
VISUAL_DIR = MEDIA_ROOT / "visual"
CONSOLE_INPUT_FILE = PROJECT_ROOT / "chatgpt-ui-data" / "ui-inbox.jsonl"


def _mode_deadline(raw: Any) -> datetime | None:
    if not raw:
        return None
    try:
        value = _parse_utc(str(raw))
        return value if value.tzinfo is not None else None
    except (TypeError, ValueError, OverflowError):
        return None


def _enforce_mode_deadlines(record: dict[str, Any]) -> bool:
    """Enforce Lease >= active Visual >= active Stay on every lease write.

    Completed Visual is no longer a live interval: normal residency may remain.
    Historical metadata must never reactivate a completed mode.
    """
    original_visual = record.get("visual_mode")
    before = (record.get("expires_at"), original_visual.get("until_at") if isinstance(original_visual, dict) else None)
    now = utc_now()
    stay = _mode_deadline(record.get("stay_until_at"))
    visual = record.get("visual_mode")
    lease_until = _mode_deadline(record.get("expires_at"))
    deadlines = [lease_until]
    if stay and stay > now:
        deadlines.append(stay)
    if isinstance(visual, dict) and visual.get("active"):
        visual_until = _mode_deadline(visual.get("until_at"))
        if visual.get("user_mode") == "enabled" and lease_until and (not visual_until or visual_until < lease_until):
            visual_until = lease_until
            visual["until_at"] = utc_iso(lease_until)
        if stay and stay > now and (not visual_until or visual_until < stay):
            visual_until = stay
            visual["until_at"] = utc_iso(stay)
        if visual_until:
            deadlines.append(visual_until)
    valid = [value for value in deadlines if value is not None]
    if valid:
        record["expires_at"] = utc_iso(max(valid))
    after = (record.get("expires_at"), visual.get("until_at") if isinstance(visual, dict) else None)
    return after != before


def _active_mode_lease_locked(lease_id: str) -> dict[str, Any]:
    if len(lease_id) != 32 or any(c not in "0123456789abcdef" for c in lease_id):
        raise RuntimePolicyError("租约无效", "Invalid execution lease ID")
    record = _read_mode_lease_record_locked(lease_id)
    if record is None:
        raise RuntimePolicyError("租约不存在", "该工作区租约不存在或已经过期")
    if record.get("takeover_pending_at"):
        raise RuntimePolicyError(
            "旧 turn 已被同主控接管",
            "该 owner lease 已进入 same-controller takeover fencing；旧 turn 后续工具调用不再拥有写权限。",
            retryable=False,
            error_type="SAME_CONTROLLER_TAKEOVER_FENCED",
            details={
                "controller_id": lease_controller_id(record),
                "takeover_pending_at": record.get("takeover_pending_at"),
                "takeover_reason": record.get("takeover_reason"),
            },
        )
    if _is_lease_participant(record):
        # A participant's lifetime is governed by the shared Root resource lease.
        # Its local cached expires_at may lag after Root /stay or renewal, so never
        # let that cache kill a still-active Child Conversation.
        record = _participant_view(record, _shared_root_record_locked(record))
    else:
        _enforce_mode_deadlines(record)
    expires = _mode_deadline(record.get("expires_at"))
    if not expires or expires <= utc_now():
        raise RuntimePolicyError("租约已过期", "该工作区租约已过期")
    if record.get("mode") != "execute_exclusive":
        raise RuntimePolicyError("租约只读", "只读租约不能执行写入、Git 或命令操作")
    return record


def begin_same_controller_takeover(
    target_lease_id: str,
    *,
    controller_id: str,
    reason: str,
) -> dict[str, Any]:
    """Fence one stale owner handle before a newer turn of the same GPT controller takes over."""
    clean_controller = _normalise_controller_id(controller_id)
    clean_reason = str(reason or "same-controller newer turn superseded detached execution").strip()[:500]
    if not clean_controller:
        raise RuntimePolicyError("主控身份缺失", "same-controller takeover 必须提供稳定 controller_id")
    with _metadata_lock():
        record = _read_mode_lease_record_locked(target_lease_id)
        if record is None:
            raise RuntimePolicyError("租约不存在", "待接管 owner lease 已不存在或过期", error_type="TAKEOVER_TARGET_MISSING")
        if _is_lease_participant(record):
            raise RuntimePolicyError("接管目标无效", "只能接管 Root owner lease，不能接管 participant",
                                     error_type="TAKEOVER_TARGET_NOT_OWNER")
        if str(record.get("mode") or "") != "execute_exclusive":
            raise RuntimePolicyError("接管目标只读", "same-controller takeover 只适用于 execute_exclusive owner",
                                     error_type="TAKEOVER_TARGET_NOT_EXECUTE")
        actual_controller = lease_controller_id(record)
        if actual_controller != clean_controller:
            raise RuntimePolicyError(
                "主控身份冲突",
                "待接管 lease 的稳定 controller_id 与当前 controller 不一致；拒绝按标题、owner_label 或工作区猜身份。",
                error_type="CONTROLLER_IDENTITY_CONFLICT",
                details={"expected_controller_id": clean_controller, "actual_controller_id": actual_controller},
            )
        root_id = str(record.get("lease_id") or target_lease_id)
        participants = [
            other for _path, other in _lease_records_locked()
            if _is_lease_participant(other)
            and str(other.get("shared_root_lease_id") or "") == root_id
        ]
        if participants:
            raise RuntimePolicyError(
                "接管被 Child 阻止",
                "当前 Root 仍有 GPTAgent participant；为避免破坏共享资源家族，先收口/迁移 Child，再接管 Root。",
                retryable=True,
                error_type="SAME_CONTROLLER_TAKEOVER_PARTICIPANTS_ACTIVE",
                details={"participant_count": len(participants)},
            )
        jobs = [job for job in _running_job_records_locked() if str(job.get("lease_id") or "") == target_lease_id]
        if jobs:
            raise RuntimePolicyError(
                "接管被后台 Job 阻止",
                "旧 owner 仍有 Runtime Job；不能通过删除 lease 制造孤儿进程。先等待/取消这些 Job。",
                retryable=True,
                error_type="SAME_CONTROLLER_TAKEOVER_JOBS_ACTIVE",
                details={"job_ids": [str(job.get("job_id") or "") for job in jobs[:32]]},
            )
        if record.get("takeover_pending_at"):
            pending_controller = str(record.get("takeover_pending_controller_id") or "")
            if pending_controller != clean_controller:
                raise RuntimePolicyError(
                    "接管状态冲突",
                    "该 lease 已被另一个 controller 标记 takeover pending；拒绝覆盖。",
                    error_type="CONTROLLER_IDENTITY_CONFLICT",
                )
            return {
                "status": "already_fenced",
                "lease_id": target_lease_id,
                "controller_id": clean_controller,
                "takeover_pending_at": record.get("takeover_pending_at"),
                "takeover_reason": record.get("takeover_reason"),
            }
        record["takeover_pending_at"] = utc_iso()
        record["takeover_pending_controller_id"] = clean_controller
        record["takeover_reason"] = clean_reason
        _write_json(_lease_path(target_lease_id), record)
        _write_controller_envelope(record)
        return {
            "status": "fenced",
            "lease_id": target_lease_id,
            "controller_id": clean_controller,
            "takeover_pending_at": record["takeover_pending_at"],
            "takeover_reason": clean_reason,
        }


def finish_same_controller_takeover(
    target_lease_id: str,
    *,
    controller_id: str,
    runtime_instance: str | None = None,
    owner_label: str | None = None,
) -> dict[str, Any]:
    """Rotate a fenced owner handle to a fresh lease while preserving controller task state."""
    clean_controller = _normalise_controller_id(controller_id)
    with _metadata_lock():
        old = _read_mode_lease_record_locked(target_lease_id)
        if old is None:
            raise RuntimePolicyError("接管目标丢失", "fenced owner lease 已不存在；请从 controller recovery envelope 恢复",
                                     error_type="TAKEOVER_TARGET_MISSING")
        if _is_lease_participant(old):
            raise RuntimePolicyError("接管目标无效", "只能轮换 Root owner lease", error_type="TAKEOVER_TARGET_NOT_OWNER")
        actual_controller = lease_controller_id(old)
        if actual_controller != clean_controller:
            raise RuntimePolicyError(
                "主控身份冲突",
                "finish takeover 的 controller_id 与 fenced owner 不一致。",
                error_type="CONTROLLER_IDENTITY_CONFLICT",
            )
        if not old.get("takeover_pending_at") or str(old.get("takeover_pending_controller_id") or "") != clean_controller:
            raise RuntimePolicyError(
                "接管尚未 fencing",
                "必须先 begin_same_controller_takeover，再等待旧 turn 在途工具排空。",
                retryable=True,
                error_type="TAKEOVER_NOT_FENCED",
            )
        root_id = str(old.get("lease_id") or target_lease_id)
        if any(
            _is_lease_participant(other)
            and str(other.get("shared_root_lease_id") or "") == root_id
            for _path, other in _lease_records_locked()
        ):
            raise RuntimePolicyError(
                "接管被 Child 阻止",
                "fencing 后出现/残留 participant，拒绝轮换 Root。",
                retryable=True,
                error_type="SAME_CONTROLLER_TAKEOVER_PARTICIPANTS_ACTIVE",
            )
        if any(str(job.get("lease_id") or "") == target_lease_id for job in _running_job_records_locked()):
            raise RuntimePolicyError(
                "接管被后台 Job 阻止",
                "fencing 后仍有 Runtime Job，拒绝轮换 Root。",
                retryable=True,
                error_type="SAME_CONTROLLER_TAKEOVER_JOBS_ACTIVE",
            )
        now = utc_now()
        new_id = uuid.uuid4().hex
        new_record = json.loads(json.dumps(old))
        new_record["lease_id"] = new_id
        new_record["controller_id"] = clean_controller
        if owner_label is not None and str(owner_label).strip():
            new_record["owner_label"] = str(owner_label).strip()[:200]
        new_record["runtime_instance"] = str(runtime_instance or "").strip() or old.get("runtime_instance")
        new_record["created_at"] = utc_iso(now)
        new_record["renewed_at"] = utc_iso(now)
        try:
            old_expiry = _parse_utc(str(old.get("expires_at") or ""))
        except Exception:
            old_expiry = now
        new_record["expires_at"] = utc_iso(max(old_expiry, now + timedelta(seconds=DEFAULT_LEASE_SECONDS)))
        new_record["supersedes_lease_id"] = target_lease_id
        new_record["same_controller_takeover_at"] = utc_iso(now)
        new_record["same_controller_takeover_reason"] = str(old.get("takeover_reason") or "")[:500]
        for key in list(new_record):
            if key.startswith("disconnect_probe_") or key.startswith("disconnect_alert_") or key.startswith("takeover_pending"):
                new_record.pop(key, None)
        new_record.pop("takeover_reason", None)
        _write_json(_lease_path(new_id), new_record)
        _write_controller_envelope(new_record)
        _lease_path(target_lease_id).unlink(missing_ok=True)
        with _CHECKPOINT_OVERLAY_LOCK:
            _CHECKPOINT_OVERLAYS.pop(target_lease_id, None)
        _sync_visual_manifest_locked(new_record)
        return {
            **new_record,
            "status": "taken_over",
            "old_lease_id": target_lease_id,
            "new_lease_id": new_id,
            "resource_lease_owner": True,
        }


def _sync_visual_manifest_locked(record: dict[str, Any]) -> None:
    visual = record.get("visual_mode")
    if not isinstance(visual, dict):
        return
    _write_json(VISUAL_DIR / record["lease_id"] / "manifest.json", {
        "schema_version": 1, "lease_id": record["lease_id"], "workspace": record["workspace"],
        "visual_mode": visual, "stay_until_at": record.get("stay_until_at"),
        "lease_until_at": record.get("expires_at"),
    })


def visual_mode_state(record: dict[str, Any], *, include_details: bool = True) -> dict[str, Any]:
    original = record.get("visual_mode") or {"active": False, "status": "off"}
    if include_details:
        state = json.loads(json.dumps(original))
    else:
        # Routine continuations must not retransmit reference prose or the entire
        # image history. Explicit context recovery remains lossless.
        state = {key: original[key] for key in (
            "active", "status", "started_at", "until_at", "iteration", "latest_observation_id",
            "latest_review", "targets", "depth", "journeys", "workspace_revision", "requirements_revision",
            "updated_at", "user_mode", "user_mode_updated_at",
        ) if key in original}
        state["reference_count"] = len(original.get("references", []))
        state["context_tool"] = "get_visual_context"
    until = _mode_deadline(state.get("until_at"))
    state["remaining_seconds"] = max(0, int((until - utc_now()).total_seconds())) if until else 0
    if state.get("active") and until and until <= utc_now():
        state.update(active=False, status="expired")
    return state


def _default_goal_state() -> dict[str, Any]:
    return {
        "active": False,
        "status": "off",
        "outcome": "",
        "success_criteria": [],
        "requirements": [],
        "corrections": [],
        "revision": 0,
        "reuse_visual_context": True,
        "latest_review": None,
    }


def goal_mode_state(record: dict[str, Any], *, include_details: bool = True) -> dict[str, Any]:
    original = record.get("goal_mode") or _default_goal_state()
    if include_details:
        return json.loads(json.dumps(original))
    criteria = original.get("success_criteria", [])
    review = original.get("latest_review")
    compact_review = ({"status": str(review.get("status") or "")[:32],
                       "summary": str(review.get("summary") or "")[:500]}
                      if isinstance(review, dict) else None)
    return {
        "active": bool(original.get("active")),
        "status": str(original.get("status") or "off"),
        "outcome": str(original.get("outcome") or "")[:2000],
        "success_criteria": [str(item)[:1000] for item in criteria[:32]] if isinstance(criteria, list) else [],
        "requirements": [str(item)[:1000] for item in (original.get("requirements") or [])[:32]]
                        if isinstance(original.get("requirements"), list) else [],
        "corrections": json.loads(json.dumps((original.get("corrections") or [])[-12:]))
                       if isinstance(original.get("corrections"), list) else [],
        "revision": int(original.get("revision") or 0),
        "reuse_visual_context": original.get("reuse_visual_context") is not False,
        "latest_review": compact_review,
        "validated_workspace_revision": original.get("validated_workspace_revision"),
        "completed_at": original.get("completed_at"),
        "updated_at": original.get("updated_at"),
        "user_mode": original.get("user_mode"),
        "user_mode_updated_at": original.get("user_mode_updated_at"),
    }


def _updated_goal_state(
    current: dict[str, Any],
    *,
    enabled: bool,
    outcome: str,
    success_criteria: list[str],
    requirements: list[str] | None = None,
    reuse_visual_context: bool,
    user_explicit: bool,
    now: str,
    change_source: str = "agent",
    change_note: str = "",
) -> dict[str, Any]:
    """Build one Goal update without writing; callers persist it with their existing lock/write."""
    if type(enabled) is not bool or type(reuse_visual_context) is not bool:
        raise RuntimePolicyError("参数无效", "目标模式 enabled/reuse_visual_context 必须是布尔值")
    if not isinstance(outcome, str) or len(outcome) > 8000:
        raise RuntimePolicyError("参数无效", "目标结果最多 8000 字符")
    criteria = _visual_text_list(success_criteria, "success_criteria", limit=64)
    resolved_requirements = (
        _visual_text_list(requirements, "requirements", limit=64)
        if requirements is not None
        else [str(item).strip() for item in (current.get("requirements") or []) if str(item).strip()][:64]
    )
    clean_outcome = outcome.strip()
    if enabled and not clean_outcome:
        raise RuntimePolicyError("目标缺失", "开启目标模式前必须填写目标结果", error_type="GOAL_OUTCOME_REQUIRED")
    if enabled and not user_explicit and current.get("user_mode") == "disabled":
        raise RuntimePolicyError(
            "用户已关闭目标模式",
            "Agent Console 用户已明确关闭当前任务的 Goal Mode；当前 GPT 不能自行重新开启。新任务/新 lease 可重新按语义判断。",
            error_type="GOAL_USER_DISABLED",
        )
    if not user_explicit and not enabled and (current.get("active") or current.get("status") in {"needs_review", "completed"}):
        raise RuntimePolicyError(
            "禁止自行解除目标模式",
            "当前 GPT 不能通过关闭 Goal Mode 绕过完成验收；请完成目标验收，或由 Agent Console 用户明确关闭。",
            error_type="GOAL_SELF_DISABLE_FORBIDDEN",
        )
    goal = json.loads(json.dumps(current or _default_goal_state()))
    target_user_mode = ("enabled" if enabled else "disabled") if user_explicit else goal.get("user_mode")
    changed_fields: list[str] = []
    if bool(goal.get("active")) != enabled:
        changed_fields.append("状态")
    if str(goal.get("outcome") or "") != clean_outcome:
        changed_fields.append("目标")
    if list(goal.get("success_criteria") or []) != criteria:
        changed_fields.append("完成标准")
    if list(goal.get("requirements") or []) != resolved_requirements:
        changed_fields.append("持续要求")
    if (goal.get("reuse_visual_context") is not False) != reuse_visual_context:
        changed_fields.append("视觉复用")
    if user_explicit and goal.get("user_mode") != target_user_mode:
        changed_fields.append("用户模式")
    if not changed_fields:
        return goal
    revision = int(goal.get("revision") or 0) + 1
    goal.update(
        active=enabled,
        status="active" if enabled else "off",
        outcome=clean_outcome,
        success_criteria=criteria,
        requirements=resolved_requirements,
        revision=revision,
        reuse_visual_context=reuse_visual_context,
        updated_at=now,
        started_at=goal.get("started_at") or (now if enabled else None),
    )
    if enabled:
        goal["enabled_at"] = now
        goal["latest_review"] = None
        goal.pop("validated_workspace_revision", None)
        goal.pop("completed_at", None)
        goal.pop("disabled_at", None)
    else:
        goal["disabled_at"] = now
    if user_explicit:
        goal["user_mode"] = "enabled" if enabled else "disabled"
        goal["user_mode_updated_at"] = now
    note = str(change_note or "").strip()
    if len(note) > 2000:
        raise RuntimePolicyError("参数无效", "目标修正说明最多 2000 字符")
    source = str(change_source or ("user" if user_explicit else "agent")).strip().lower()[:32]
    if source not in {"user", "agent", "child", "system"}:
        source = "user" if user_explicit else "agent"
    corrections = goal.get("corrections") if isinstance(goal.get("corrections"), list) else []
    corrections = list(corrections)[-63:]
    corrections.append({
        "revision": revision,
        "at": now,
        "source": source,
        "fields": changed_fields,
        "note": note or ("更新" + "、".join(changed_fields)),
    })
    goal["corrections"] = corrections
    return goal


def _default_ultra_state() -> dict[str, Any]:
    return {
        "active": False,
        "status": "off",
        "revision": 0,
        "source": "default",
        "user_mode": None,
        "updated_at": None,
    }


def ultra_mode_state(record: dict[str, Any]) -> dict[str, Any]:
    original = record.get("ultra_mode") or _default_ultra_state()
    return {
        "active": bool(original.get("active")),
        "status": str(original.get("status") or ("on" if original.get("active") else "off")),
        "revision": int(original.get("revision") or 0),
        "source": str(original.get("source") or "default")[:32],
        "updated_at": original.get("updated_at"),
        "enabled_at": original.get("enabled_at"),
        "disabled_at": original.get("disabled_at"),
        "user_mode": original.get("user_mode"),
        "user_mode_updated_at": original.get("user_mode_updated_at"),
    }


def set_ultra_mode(
    lease_id: str,
    *,
    enabled: bool,
    user_explicit: bool = False,
    actor: str = "agent",
    expected_updated_at: str | None = None,
) -> dict[str, Any]:
    """Persist lease-scoped Ultra/sub-agent policy without adding another MCP tool.

    Console users may enable/disable. Agents and child agents may only opt in,
    and a user-explicit opt-out is authoritative for the lifetime of the lease.
    """
    if type(enabled) is not bool:
        raise RuntimePolicyError("参数无效", "Ultra enabled 必须是布尔值")
    clean_actor = str(actor or "agent").strip().lower()[:32]
    if clean_actor not in {"user", "agent", "child", "system"}:
        clean_actor = "user" if user_explicit else "agent"
    with _metadata_lock():
        source = _active_mode_lease_locked(lease_id)
        record = _shared_root_record_locked(source)
        resource_lease_id = str(record.get("lease_id") or lease_id)
        current = record.get("ultra_mode") or _default_ultra_state()
        if expected_updated_at is not None and str(current.get("updated_at") or "") != expected_updated_at:
            raise RuntimePolicyError("Ultra 状态已更新", "Ultra 模式内容已变化；请刷新后重试，未覆盖当前状态",
                                     error_type="ULTRA_CONTEXT_CONFLICT", retryable=True)
        if not user_explicit:
            if not enabled:
                raise RuntimePolicyError("禁止自行关闭 Ultra", "AI 只能按需开启 Ultra；关闭由用户控制",
                                         error_type="ULTRA_SELF_DISABLE_FORBIDDEN")
            if current.get("user_mode") == "disabled":
                raise RuntimePolicyError("用户已关闭 Ultra", "用户已明确关闭当前 lease 的 Ultra 子Agent模式；AI/Child 不得反开",
                                         error_type="ULTRA_USER_DISABLED")
        target_user_mode = ("enabled" if enabled else "disabled") if user_explicit else current.get("user_mode")
        changed = bool(current.get("active")) != enabled or (user_explicit and current.get("user_mode") != target_user_mode)
        if not changed:
            return {"lease_id": lease_id, "changed": False, "ultra_mode": ultra_mode_state(record)}
        now = utc_iso()
        state = json.loads(json.dumps(current))
        state.update(
            active=enabled,
            status="on" if enabled else "off",
            revision=int(state.get("revision") or 0) + 1,
            source="user" if user_explicit else clean_actor,
            updated_at=now,
        )
        if enabled:
            state["enabled_at"] = now
            state.pop("disabled_at", None)
        else:
            state["disabled_at"] = now
        if user_explicit:
            state["user_mode"] = "enabled" if enabled else "disabled"
            state["user_mode_updated_at"] = now
        record["ultra_mode"] = state
        _write_json(_lease_path(resource_lease_id), record)
        return {"lease_id": lease_id, "shared_root_lease_id": resource_lease_id,
                "changed": True, "ultra_mode": ultra_mode_state(record)}


def _validate_goal_completion(goal: dict[str, Any], review: dict[str, Any] | None,
                              workspace_revision: int) -> dict[str, Any]:
    """Validate an explicit, public goal acceptance record before task completion.

    This is intentionally deterministic: the model still performs the substantive
    checks, but Runtime requires every declared criterion to be acknowledged with
    concrete public evidence and binds that acceptance to the current workspace
    validation revision.  A later mutation invalidates the acceptance.
    """
    if not isinstance(review, dict):
        raise RuntimePolicyError("目标验收不完整", "review.goal 缺失", error_type="GOAL_VERIFICATION_REQUIRED")
    goal_review = review.get("goal")
    if not isinstance(goal_review, dict):
        raise RuntimePolicyError(
            "目标验收不完整",
            "完成 Goal Mode 时 review.goal 必须包含 status、summary 与逐条 criteria 证据",
            error_type="GOAL_VERIFICATION_REQUIRED",
        )
    if str(goal_review.get("status") or "").upper() != "PASS":
        raise RuntimePolicyError("目标验收未通过", "review.goal.status 必须为 PASS", error_type="GOAL_VERIFICATION_REQUIRED")
    summary = str(goal_review.get("summary") or "").strip()
    if not summary or len(summary) > 4000:
        raise RuntimePolicyError("目标验收不完整", "review.goal.summary 必须为 1..4000 字符", error_type="GOAL_VERIFICATION_REQUIRED")
    declared = [str(item) for item in (goal.get("success_criteria") or [])]
    evidence = goal_review.get("criteria")
    if not isinstance(evidence, list) or len(evidence) != len(declared):
        raise RuntimePolicyError(
            "目标验收不完整",
            "review.goal.criteria 必须逐条覆盖当前 success_criteria，数量与顺序均一致",
            error_type="GOAL_VERIFICATION_REQUIRED",
        )
    clean_items: list[dict[str, str]] = []
    for index, criterion in enumerate(declared):
        item = evidence[index]
        if not isinstance(item, dict) or str(item.get("criterion") or "") != criterion:
            raise RuntimePolicyError(
                "目标验收标准不匹配",
                f"第 {index + 1} 条验收记录必须精确对应当前 success_criteria",
                error_type="GOAL_VERIFICATION_REQUIRED",
            )
        if str(item.get("status") or "").upper() != "PASS":
            raise RuntimePolicyError(
                "目标验收未通过",
                f"第 {index + 1} 条 success_criteria 尚未 PASS",
                error_type="GOAL_VERIFICATION_REQUIRED",
            )
        proof = str(item.get("evidence") or "").strip()
        if not proof or len(proof) > 4000:
            raise RuntimePolicyError(
                "目标验收证据缺失",
                f"第 {index + 1} 条 success_criteria 必须提供 1..4000 字符的公开证据摘要",
                error_type="GOAL_VERIFICATION_REQUIRED",
            )
        clean_items.append({"criterion": criterion, "status": "PASS", "evidence": proof})
    return {
        "status": "PASS",
        "summary": summary,
        "criteria": clean_items,
        "workspace_revision": int(workspace_revision),
    }


def set_goal_mode(
    lease_id: str,
    *,
    enabled: bool,
    outcome: str,
    success_criteria: list[str],
    requirements: list[str] | None = None,
    reuse_visual_context: bool = True,
    expected_updated_at: str | None = None,
    change_note: str = "",
) -> dict[str, Any]:
    """Persist the trusted Agent Console user's explicit Goal choice on the existing task lease."""
    with _metadata_lock():
        source = _active_mode_lease_locked(lease_id)
        record = _shared_root_record_locked(source)
        resource_lease_id = str(record.get("lease_id") or lease_id)
        current = record.get("goal_mode") or _default_goal_state()
        if expected_updated_at is not None and str(current.get("updated_at") or "") != expected_updated_at:
            raise RuntimePolicyError("目标资料已更新", "目标模式内容已变化；请刷新后重试，未覆盖当前状态", error_type="GOAL_CONTEXT_CONFLICT", retryable=True)
        goal = _updated_goal_state(
            current,
            enabled=enabled,
            outcome=outcome,
            success_criteria=success_criteria,
            requirements=requirements,
            reuse_visual_context=reuse_visual_context,
            user_explicit=True,
            now=utc_iso(),
            change_source="user",
            change_note=change_note,
        )
        changed = goal != current
        record["goal_mode"] = goal
        if changed:
            _write_json(_lease_path(resource_lease_id), record)
        return {"lease_id": lease_id, "shared_root_lease_id": resource_lease_id,
                "changed": changed, "goal_mode": goal_mode_state(record),
                "visual_mode": visual_mode_state(record, include_details=False), "ultra_mode": ultra_mode_state(record)}


def _completion_review_template(visual: dict[str, Any], goal: dict[str, Any]) -> dict[str, Any]:
    """Expose the current acceptance shape without inventing successful evidence."""
    review: dict[str, Any] = {}
    if visual.get("active") or visual.get("status") in {"needs_review", "expired"}:
        targets = [item["name"] for item in visual.get("targets", [])]
        review.update({name: "PENDING" for name in targets})
        review.update(critical_differences=None, major_differences=None, minor_differences=None,
                      reference_requirements="PENDING", final_observation_ids={name: "" for name in targets})
        responsive = [item["name"] for item in visual.get("targets", [])
                      if _visual_target_viewport_mode(item) == "responsive"]
        if responsive:
            review["responsive_layout"] = {name: "PENDING" for name in responsive}
            review["responsive_observation_ids"] = {name: [] for name in responsive}
    if goal.get("active") or goal.get("status") == "needs_review":
        review["goal"] = {"status": "PENDING", "summary": "", "criteria": [
            {"criterion": criterion, "status": "PENDING", "evidence": ""}
            for criterion in goal.get("success_criteria", [])
        ]}
    return review


def get_visual_context(lease_id: str) -> dict[str, Any]:
    with _metadata_lock():
        source = _active_mode_lease_locked(lease_id)
        record = _shared_root_record_locked(source)
        resource_lease_id = str(record.get("lease_id") or lease_id)
        _sync_visual_manifest_locked(record)
        return {"lease_id": lease_id, "shared_root_lease_id": resource_lease_id,
                "lease_until_at": record["expires_at"],
                "stay_until_at": record.get("stay_until_at"), "visual_mode": visual_mode_state(record),
                "goal_mode": goal_mode_state(record), "ultra_mode": ultra_mode_state(record),
                "completion_review_template": _completion_review_template(
                    visual_mode_state(record), goal_mode_state(record))}


def apply_console_stay_command(lease_id: str, message_id: str) -> dict[str, Any]:
    """Console ingestion verifies a real inbox row, not a caller-supplied source flag.

    This internal function is not an MCP tool. It consumes only an actual,
    task-scoped Console command. The Agent-facing Stay function is enable-only.
    """
    # Importing the pure parser here avoids maintaining competing grammars.
    from mode_commands import parse_stay_command
    with _metadata_lock():
        source = _active_mode_lease_locked(lease_id)
        record = _shared_root_record_locked(source)
        resource_lease_id = str(record.get("lease_id") or lease_id)
        found = None
        if CONSOLE_INPUT_FILE.exists():
            with CONSOLE_INPUT_FILE.open("rb") as handle:
                size = handle.seek(0, os.SEEK_END)
                handle.seek(max(0, size - 8 * 1024 * 1024))
                for line in handle.read().decode("utf-8", errors="replace").split("\n"):
                    try:
                        row = json.loads(line)
                    except (ValueError, TypeError):
                        continue
                    if isinstance(row, dict) and row.get("id") == message_id and row.get("type") == "ui.message" and row.get("scope", "task") == "task" and row.get("taskId") == lease_id and row.get("threadId") == source.get("thread_id"):
                        found = row
        command = parse_stay_command(str((found or {}).get("text") or ""))
        if not found or not command or command["action"] == "invalid":
            raise RuntimePolicyError("Console 来源无效", "未找到真实的同租约 Console 驻场命令", error_type="CONSOLE_COMMAND_UNVERIFIED")
        processed = record.get("processed_stay_command_ids", [])
        if message_id in processed:
            return record
        if command["action"] == "exit":
            for key in ("stay_started_at", "stay_until_at", "stay_duration_seconds", "stay_command_id"):
                record.pop(key, None)
        else:
            now = utc_now()
            seconds = command["duration_seconds"]
            record.update(stay_started_at=utc_iso(now), stay_until_at=utc_iso(now + timedelta(seconds=seconds)),
                          stay_duration_seconds=seconds, stay_command_id=message_id)
        record["processed_stay_command_ids"] = (processed + [message_id])[-128:]
        _write_json(_lease_path(resource_lease_id), record)
        _sync_visual_manifest_locked(record)
        return _participant_view(source, record) if _is_lease_participant(source) else record


def apply_stay_command(lease_id: str, command: str) -> dict[str, Any]:
    from mode_commands import parse_stay_command
    parsed = parse_stay_command(command)
    if not parsed or parsed["action"] == "invalid":
        raise RuntimePolicyError("驻场命令无效", (parsed or {}).get("error", "请输入 /stay 时长"))
    if parsed["action"] != "enter":
        raise RuntimePolicyError("禁止自行解除驻场", "网页 /stay off 不授予当前 GPT 自解锁权限", error_type="STAY_SELF_DISABLE_FORBIDDEN")
    return set_workspace_stay_mode(lease_id, duration_seconds=parsed["duration_seconds"])


def _visual_text_list(value: Any, name: str, limit: int = 64) -> list[str]:
    if not isinstance(value, list) or len(value) > limit or any(not isinstance(item, str) or len(item) > 2000 for item in value):
        raise RuntimePolicyError("参数无效", f"{name} 需为最多 {limit} 条、单条不超过 2000 字符的文本列表")
    return [item.strip() for item in value if item.strip()]


def _default_visual_state() -> dict[str, Any]:
    return {
        "active": False, "status": "off", "reference_ids": [], "references": [], "criteria": [],
        # Screenshot size is free by default. A target describes what must be
        # reviewed; it does not silently force every screenshot to one viewport.
        "targets": [{"name": "desktop", "viewport_mode": "free"}],
        "depth": "visual", "journeys": [], "workspace_revision": 0,
        "iteration": 0, "observations": [], "latest_observation_id": None, "latest_review": None,
    }


_VISUAL_VIEWPORT_MODES = frozenset({"free", "fixed", "responsive"})
_VISUAL_DEPTHS = frozenset({"visual", "interactive", "frontend"})


def _normalise_visual_journeys(value: Any) -> list[dict[str, Any]]:
    if not isinstance(value, list) or len(value) > 12:
        raise RuntimePolicyError("参数无效", "journeys 必须是最多 12 条的列表")
    output: list[dict[str, Any]] = []
    seen: set[str] = set()
    for raw in value:
        if not isinstance(raw, dict):
            raise RuntimePolicyError("参数无效", "每条 journey 必须是对象")
        identity = str(raw.get("id") or "").strip()
        name = str(raw.get("name") or identity).strip()
        if not identity or len(identity) > 80 or any(
            not (character.isascii() and (character.isalnum() or character in "._-"))
            for character in identity
        ):
            raise RuntimePolicyError("参数无效", "journey.id 仅支持 1..80 个 ASCII 字母数字或 . _ -")
        if identity in seen:
            raise RuntimePolicyError("参数无效", "journey.id 不得重复")
        if not name or len(name) > 160:
            raise RuntimePolicyError("参数无效", "journey.name 必须是 1..160 字符")
        required = raw.get("required", True)
        if type(required) is not bool:
            raise RuntimePolicyError("参数无效", "journey.required 必须是布尔值")
        seen.add(identity)
        output.append({"id": identity, "name": name, "required": required})
    return output


def _normalise_visual_target(target: Any) -> dict[str, Any]:
    if not isinstance(target, dict):
        raise RuntimePolicyError("参数无效", "每个 Visual target 必须是对象")
    name = str(target.get("name") or "").strip()
    if not name or len(name) > 80:
        raise RuntimePolicyError("参数无效", "target.name 必须是 1..80 字符")
    raw_mode = target.get("viewport_mode", "free")
    if not isinstance(raw_mode, str) or raw_mode not in _VISUAL_VIEWPORT_MODES:
        raise RuntimePolicyError("参数无效", "target.viewport_mode 仅支持 free/fixed/responsive")
    width = target.get("width")
    height = target.get("height")
    if (width is None) != (height is None):
        raise RuntimePolicyError("参数无效", "target.width/height 必须同时提供或同时省略")
    normalised: dict[str, Any] = {"name": name, "viewport_mode": raw_mode}
    if width is not None:
        if any(isinstance(value, bool) or not isinstance(value, int) or not 200 <= value <= 4096
               for value in (width, height)):
            raise RuntimePolicyError("参数无效", "target.width/height 必须是 200..4096 的整数")
        normalised.update(width=width, height=height)
    if raw_mode == "fixed" and width is None:
        raise RuntimePolicyError("参数无效", "viewport_mode=fixed 必须提供 width/height")
    return normalised


def _visual_target_viewport_mode(target: dict[str, Any]) -> str:
    """Preserve fixed semantics only for old stored targets without a mode."""
    mode = target.get("viewport_mode")
    if isinstance(mode, str) and mode in _VISUAL_VIEWPORT_MODES:
        return mode
    if isinstance(target.get("width"), int) and isinstance(target.get("height"), int):
        return "fixed"
    return "free"


def _visual_observation_is_current(visual: dict[str, Any], observation: dict[str, Any]) -> bool:
    if observation.get("requirements_revision", 0) != visual.get("requirements_revision", 0):
        return False
    return int(observation.get("workspace_revision") or 0) == int(visual.get("workspace_revision") or 0)


def _validate_visual_observation_file(observation: dict[str, Any]) -> Path:
    import hashlib
    path = Path(str(observation.get("path") or ""))
    if not observation.get("inspected_at") or not path.is_file():
        raise RuntimePolicyError(
            "视觉验收不完整", "最终视觉证据必须是真实已读取且仍存在的图片", error_type="VISUAL_VERIFICATION_REQUIRED"
        )
    if hashlib.sha256(path.read_bytes()).hexdigest() != observation.get("sha256"):
        raise RuntimePolicyError("视觉验收证据变化", "最终截图内容已变化，必须重新读取和验收", error_type="VISUAL_EVIDENCE_CHANGED")
    return path


def _validate_visual_completion(visual: dict[str, Any]) -> None:
    result = visual.get("latest_review") or {}
    finals = result.get("final_observation_ids") or {}
    target_items = visual.get("targets") or []
    target_names = [str(item.get("name") or "") for item in target_items if isinstance(item, dict)]
    if not target_names or any(not name for name in target_names):
        raise RuntimePolicyError("视觉验收不完整", "Visual targets 为空或无效", error_type="VISUAL_VERIFICATION_REQUIRED")
    if not isinstance(finals, dict) or any(not isinstance(finals.get(name), str) for name in target_names):
        names = ", ".join(target_names)
        raise RuntimePolicyError("视觉验收不完整", f"final_observation_ids 必须包含当前 targets：{names}", error_type="VISUAL_VERIFICATION_REQUIRED")
    observations = {item["id"]: item for item in visual.get("observations", [])}
    targets = {item["name"]: item for item in target_items}
    for name in target_names:
        observation = observations.get(finals.get(name), {})
        target = targets.get(name, {})
        if not _visual_observation_is_current(visual, observation):
            raise RuntimePolicyError("视觉要求已更新", "参考资料或要求更新后必须重新取得最终观察，不能沿用旧验收证据", error_type="VISUAL_EVIDENCE_STALE")
        if result.get(name) != "PASS" or not target:
            raise RuntimePolicyError("视觉验收不完整", f"需要真实已读取的 {name} 最终截图和 PASS 结果", error_type="VISUAL_VERIFICATION_REQUIRED")
        _validate_visual_observation_file(observation)
        viewport_mode = _visual_target_viewport_mode(target)
        if viewport_mode == "fixed" and (
            observation.get("width") != target.get("width")
            or observation.get("height") != target.get("height")
        ):
            raise RuntimePolicyError(
                "视觉验收尺寸不符",
                f"{name} 明确声明 viewport_mode=fixed，最终截图尺寸必须为 {target.get('width')}x{target.get('height')}",
                error_type="VISUAL_VERIFICATION_REQUIRED",
            )
        if viewport_mode == "responsive":
            responsive_status = result.get("responsive_layout") or {}
            responsive_ids = result.get("responsive_observation_ids") or {}
            ids = responsive_ids.get(name) if isinstance(responsive_ids, dict) else None
            if not isinstance(responsive_status, dict) or responsive_status.get(name) != "PASS":
                raise RuntimePolicyError(
                    "响应式视觉验收不完整",
                    f"{name} 使用 viewport_mode=responsive，review.responsive_layout.{name} 必须为 PASS",
                    error_type="VISUAL_VERIFICATION_REQUIRED",
                )
            if not isinstance(ids, list) or len(ids) < 2 or any(not isinstance(item, str) for item in ids):
                raise RuntimePolicyError(
                    "响应式视觉验收不完整",
                    f"{name} 至少需要两张不同宽度的真实 browser observation",
                    error_type="VISUAL_VERIFICATION_REQUIRED",
                )
            if finals.get(name) not in ids:
                raise RuntimePolicyError(
                    "响应式视觉验收不完整",
                    f"{name} 的最终 observation 必须包含在 responsive_observation_ids 中",
                    error_type="VISUAL_VERIFICATION_REQUIRED",
                )
            responsive_observations: list[dict[str, Any]] = []
            for observation_id in dict.fromkeys(ids):
                item = observations.get(observation_id, {})
                if not item or not _visual_observation_is_current(visual, item):
                    raise RuntimePolicyError(
                        "响应式视觉证据过期",
                        f"{name} 的响应式 observation 必须属于当前视觉要求版本",
                        error_type="VISUAL_EVIDENCE_STALE",
                    )
                if item.get("capture_type") != "browser":
                    raise RuntimePolicyError(
                        "响应式视觉验收不完整",
                        f"{name} 的响应式验收必须使用真实 browser screenshot",
                        error_type="VISUAL_VERIFICATION_REQUIRED",
                    )
                _validate_visual_observation_file(item)
                responsive_observations.append(item)
            widths = sorted({
                int(item.get("viewport_width") or item.get("width") or 0)
                for item in responsive_observations
                if int(item.get("viewport_width") or item.get("width") or 0) > 0
            })
            if len(widths) < 2 or widths[-1] - widths[0] < max(80, int(widths[0] * 0.10)):
                raise RuntimePolicyError(
                    "响应式视觉验收范围不足",
                    f"{name} 需要至少两个明显不同的浏览器宽度（至少相差 80px 或 10%）",
                    error_type="VISUAL_VERIFICATION_REQUIRED",
                )
    depth = str(visual.get("depth") or "visual")
    if depth in {"interactive", "frontend"}:
        journeys = visual.get("journeys") or []
        required_journeys = [item for item in journeys if isinstance(item, dict) and item.get("required") is not False]
        if not required_journeys:
            raise RuntimePolicyError(
                "前端交互验收不完整", f"depth={depth} 至少需要一条 required journey",
                error_type="VISUAL_INTERACTION_REQUIRED",
            )
        for journey in required_journeys:
            journey_id = str(journey.get("id") or "")
            evidence = [
                item for item in visual.get("observations", [])
                if isinstance(item, dict) and item.get("journey_id") == journey_id
                and _visual_observation_is_current(visual, item)
            ]
            passed = evidence[-1] if evidence else None
            if passed is None or passed.get("interaction_status") != "PASS":
                raise RuntimePolicyError(
                    "前端交互验收不完整", f"required journey 尚未通过：{journey_id}",
                    error_type="VISUAL_INTERACTION_REQUIRED",
                )
            if depth == "frontend" and (passed.get("frontend_health") or {}).get("status") != "PASS":
                raise RuntimePolicyError(
                    "前端健康验收未通过", f"required journey 存在 page/console/network 错误：{journey_id}",
                    error_type="VISUAL_FRONTEND_HEALTH_REQUIRED",
                )
    if (result.get("critical_differences") != 0 or result.get("major_differences") != 0
            or result.get("reference_requirements") != "satisfied"
            or any(type(result.get(key)) is not int for key in ("critical_differences", "major_differences", "minor_differences"))
            or result["minor_differences"] < 0):
        raise RuntimePolicyError("视觉验收未通过", "Critical/Major 须为 0，Minor 非负，参考要求须 satisfied", error_type="VISUAL_VERIFICATION_REQUIRED")


def set_visual_mode(
    lease_id: str, *, action: str = "enable", duration_seconds: int | None = None,
    criteria: list[str] | None = None, references: list[dict[str, Any]] | None = None,
    targets: list[dict[str, Any]] | None = None, review: dict[str, Any] | None = None,
    depth: str | None = None, journeys: list[dict[str, Any]] | None = None,
    goal_enabled: bool | None = None, goal_outcome: str | None = None,
    goal_success_criteria: list[str] | None = None, goal_reuse_visual: bool | None = None,
    expected_updated_at: str | None = None,
) -> dict[str, Any]:
    """Update Visual and/or Goal context atomically on the existing task lease."""
    if action not in {"enable", "extend", "update", "review", "complete"}:
        raise RuntimePolicyError("参数无效", "Visual action 仅支持 enable/extend/update/review/complete")
    goal_update_requested = any(
        value is not None
        for value in (goal_enabled, goal_outcome, goal_success_criteria, goal_reuse_visual)
    )
    visual_update_requested = action != "update" or any(
        value is not None for value in (duration_seconds, criteria, references, targets, review, depth, journeys)
    )
    with _metadata_lock():
        source = _active_mode_lease_locked(lease_id)
        record = _shared_root_record_locked(source)
        resource_lease_id = str(record.get("lease_id") or lease_id)
        visual = json.loads(json.dumps(record.get("visual_mode") or _default_visual_state()))
        visual.setdefault("depth", "visual")
        visual.setdefault("journeys", [])
        visual["workspace_revision"] = int(record.get("workspace_validation_revision") or 0)
        if action in {"enable", "extend"} and visual.get("user_mode") == "disabled":
            raise RuntimePolicyError("用户已关闭看图模式", "Agent Console 用户已明确关闭看图模式；当前 GPT 不能自行重新开启，等待用户从 UI 重新开启", error_type="VISUAL_USER_DISABLED")
        if visual_update_requested and expected_updated_at is not None and str(visual.get("updated_at") or "") != expected_updated_at:
            raise RuntimePolicyError("视觉资料已更新", "视觉资料已被另一个操作更新；请重新打开管理面板后保存，未覆盖任何现有资料", error_type="VISUAL_CONTEXT_CONFLICT", retryable=True)
        previous_requirements = (
            visual.get("criteria"), visual.get("references"), visual.get("targets"),
            visual.get("depth"), visual.get("journeys"),
        )
        now = utc_now()
        now_text = utc_iso(now)
        if action in {"enable", "extend"}:
            seconds = 3600 if duration_seconds is None else duration_seconds
            if isinstance(seconds, bool) or not isinstance(seconds, int) or not 10 <= seconds <= MAX_LEASE_SECONDS:
                raise RuntimePolicyError("参数无效", "Visual 时长需为 10 至 86400 的整数秒")
            previous = _mode_deadline(visual.get("until_at")) if visual.get("active") else None
            until = max(now + timedelta(seconds=seconds), previous or now)
            visual.update(active=True, status="active", until_at=utc_iso(until), duration_seconds=seconds)
            visual.setdefault("started_at", now_text)
        if criteria is not None:
            visual["criteria"] = _visual_text_list(criteria, "criteria")
        if references is not None:
            if not isinstance(references, list) or len(references) > 16:
                raise RuntimePolicyError("参数无效", "最多挂载 16 项参考资料")
            for ref in references:
                if not isinstance(ref, dict) or ref.get("kind") not in {"image", "text"} or not isinstance(ref.get("id"), str):
                    raise RuntimePolicyError("参数无效", "参考资料格式无效")
                if ref["kind"] == "image":
                    ref_path = Path(str(ref.get("path", "")))
                    reference_roots = (
                        MEDIA_REFERENCE_DIR / lease_id,
                        VISUAL_DIR / lease_id / "references",
                        MEDIA_REFERENCE_DIR / resource_lease_id,
                        VISUAL_DIR / resource_lease_id / "references",
                    )
                    if not any(_path_is_within(ref_path, root) for root in reference_roots):
                        raise RuntimePolicyError("参考资料越界", "参考图片必须位于统一媒体区中当前租约的 references 目录")
                    if not ref_path.is_file():
                        raise RuntimePolicyError("参考图片不存在", "参考图片必须真实存在")
                elif not isinstance(ref.get("text"), str) or len(ref["text"]) > 8000:
                    raise RuntimePolicyError("参数无效", "文本参考最多 8000 字符")
            if len({ref["id"] for ref in references}) != len(references):
                raise RuntimePolicyError("参数无效", "参考资料 ID 不得重复")
            visual["references"] = references
            visual["reference_ids"] = [ref["id"] for ref in references]
        if targets is not None:
            if not isinstance(targets, list) or not 1 <= len(targets) <= 8:
                raise RuntimePolicyError("参数无效", "targets 需为 1 至 8 个视觉目标")
            normalised_targets = [_normalise_visual_target(target) for target in targets]
            if len({item["name"] for item in normalised_targets}) != len(normalised_targets):
                raise RuntimePolicyError("参数无效", "target 名称不得重复")
            visual["targets"] = normalised_targets
        if depth is not None:
            clean_depth = str(depth).strip().lower()
            if clean_depth not in _VISUAL_DEPTHS:
                raise RuntimePolicyError("参数无效", "depth 仅支持 visual/interactive/frontend")
            visual["depth"] = clean_depth
        if journeys is not None:
            visual["journeys"] = _normalise_visual_journeys(journeys)
        if previous_requirements != (
            visual.get("criteria"), visual.get("references"), visual.get("targets"),
            visual.get("depth"), visual.get("journeys"),
        ):
            visual["requirements_revision"] = int(visual.get("requirements_revision") or 0) + 1
            visual["requirements_updated_at"] = now_text
            visual["latest_review"] = None
            if visual.get("status") == "completed":
                visual.update(active=False, status="needs_review")
        if review is not None:
            if not isinstance(review, dict) or len(json.dumps(review, ensure_ascii=False)) > 12000:
                raise RuntimePolicyError("参数无效", "review 必须是最多 12000 字符的公开验收对象")
            visual["latest_review"] = review
        if action == "complete":
            visual_requires_completion = bool(visual.get("active")) or visual.get("status") == "needs_review"
            goal = record.get("goal_mode") or _default_goal_state()
            goal_review_requested = isinstance(review, dict) and isinstance(review.get("goal"), dict)
            if visual_requires_completion:
                _validate_visual_completion(visual)
                visual.update(active=False, status="completed", completed_at=now_text)
            if goal_review_requested:
                if not goal.get("active") and goal.get("status") != "needs_review":
                    raise RuntimePolicyError("目标无需验收", "当前 Goal Mode 不是 active/needs_review", error_type="GOAL_NOT_ACTIVE")
                accepted = _validate_goal_completion(
                    goal,
                    review,
                    int(record.get("workspace_validation_revision") or 0),
                )
                goal = json.loads(json.dumps(goal))
                goal.update(
                    active=False,
                    status="completed",
                    latest_review=accepted,
                    validated_workspace_revision=int(record.get("workspace_validation_revision") or 0),
                    completed_at=now_text,
                    updated_at=now_text,
                )
                record["goal_mode"] = goal
            elif not visual_requires_completion:
                raise RuntimePolicyError(
                    "没有可完成的验收",
                    "Visual 未处于待完成状态；若要完成 Goal Mode，请在 review.goal 中提交逐条 PASS 与公开证据",
                    error_type="COMPLETION_REVIEW_REQUIRED",
                )

        if goal_update_requested:
            if action == "complete" and isinstance(review, dict) and isinstance(review.get("goal"), dict):
                raise RuntimePolicyError(
                    "目标验收与目标修改不能同次提交",
                    "先用 action=update 固定 outcome/success_criteria，再单独 action=complete 验收，避免验收对象在同一调用中变化",
                    error_type="GOAL_REVIEW_UPDATE_CONFLICT",
                )
            current_goal = record.get("goal_mode") or _default_goal_state()
            resolved_enabled = (
                bool(current_goal.get("active")) or current_goal.get("status") in {"completed", "needs_review"}
            ) if goal_enabled is None else goal_enabled
            resolved_outcome = str(current_goal.get("outcome") or "") if goal_outcome is None else goal_outcome
            current_criteria = current_goal.get("success_criteria")
            resolved_criteria = (
                list(current_criteria) if goal_success_criteria is None and isinstance(current_criteria, list)
                else ([] if goal_success_criteria is None else goal_success_criteria)
            )
            resolved_reuse = (
                current_goal.get("reuse_visual_context") is not False
                if goal_reuse_visual is None else goal_reuse_visual
            )
            record["goal_mode"] = _updated_goal_state(
                current_goal,
                enabled=resolved_enabled,
                outcome=resolved_outcome,
                success_criteria=resolved_criteria,
                reuse_visual_context=resolved_reuse,
                user_explicit=False,
                now=now_text,
            )

        if visual_update_requested:
            visual["updated_at"] = now_text
            record["visual_mode"] = visual
        _write_json(_lease_path(resource_lease_id), record)
        if visual_update_requested:
            _sync_visual_manifest_locked(record)
        return {
            "lease_id": lease_id,
            "shared_root_lease_id": resource_lease_id,
            "lease_until_at": record["expires_at"],
            "stay_until_at": record.get("stay_until_at"),
            "visual_mode": visual_mode_state(record),
            "goal_mode": goal_mode_state(record),
            "completion_review_template": _completion_review_template(
                visual_mode_state(record), goal_mode_state(record)),
        }


def set_visual_user_mode(lease_id: str, *, enabled: bool, expected_updated_at: str | None = None) -> dict[str, Any]:
    """Apply the Agent Console user's explicit Visual on/off choice without deleting references."""
    if type(enabled) is not bool:
        raise RuntimePolicyError("参数无效", "enabled 必须是布尔值")
    with _metadata_lock():
        source = _active_mode_lease_locked(lease_id)
        record = _shared_root_record_locked(source)
        resource_lease_id = str(record.get("lease_id") or lease_id)
        visual = json.loads(json.dumps(record.get("visual_mode") or _default_visual_state()))
        if expected_updated_at is not None and str(visual.get("updated_at") or "") != expected_updated_at:
            raise RuntimePolicyError("视觉资料已更新", "看图模式状态已变化；请刷新后重试，未覆盖当前状态", error_type="VISUAL_CONTEXT_CONFLICT", retryable=True)
        now = utc_now()
        visual["user_mode"] = "enabled" if enabled else "disabled"
        visual["user_mode_updated_at"] = utc_iso(now)
        if enabled:
            lease_until = _mode_deadline(record.get("expires_at")) or now
            visual.update(active=True, status="active", until_at=utc_iso(lease_until))
            visual.pop("duration_seconds", None)
            visual.pop("disabled_at", None)
            visual.setdefault("started_at", utc_iso(now))
        else:
            visual.update(active=False, status="off", disabled_at=utc_iso(now))
            visual.pop("until_at", None)
            visual.pop("duration_seconds", None)
        visual["updated_at"] = utc_iso(now)
        record["visual_mode"] = visual
        _write_json(_lease_path(resource_lease_id), record)
        _sync_visual_manifest_locked(record)
        return {"lease_id": lease_id, "shared_root_lease_id": resource_lease_id,
                "lease_until_at": record["expires_at"], "stay_until_at": record.get("stay_until_at"),
                "visual_mode": visual_mode_state(record)}


def record_visual_observation(lease_id: str, observation: dict[str, Any]) -> dict[str, Any]:
    """Publish Agent evidence without imposing a hard visual-loop count limit.

    Runtime metadata keeps only a bounded recent window so long-running Visual
    tasks can continue past hundreds or thousands of screenshots without making
    every lease/status read grow forever. Image files remain independently
    retained by the visual I/O layer's rolling storage policy.
    """
    with _metadata_lock():
        source = _active_mode_lease_locked(lease_id)
        record = _shared_root_record_locked(source)
        resource_lease_id = str(record.get("lease_id") or lease_id)
        visual = json.loads(json.dumps(record.get("visual_mode") or _default_visual_state()))
        visual.setdefault("depth", "visual")
        visual.setdefault("journeys", [])
        visual["workspace_revision"] = int(record.get("workspace_validation_revision") or 0)
        observation_path = Path(str(observation.get("path", "")))
        if not (
            _path_is_within(observation_path, VISUAL_DIR / lease_id / "observations")
            or _path_is_within(observation_path, VISUAL_DIR / resource_lease_id / "observations")
        ):
            raise RuntimePolicyError("观察图片越界", "观察必须存入当前 participant 或共享 Root 的 observations 目录")
        observations = visual.get("observations", [])
        if not isinstance(observations, list):
            observations = []
        # Rolling screenshot storage may already have removed older files.
        # Do not keep dead metadata entries in the bounded lease window.
        observations = [
            item for item in observations
            if isinstance(item, dict) and Path(str(item.get("path") or "")).is_file()
        ]
        visual["iteration"] = int(visual.get("iteration") or 0) + 1
        observation = dict(
            observation, iteration=visual["iteration"],
            requirements_revision=int(visual.get("requirements_revision") or 0),
            workspace_revision=int(record.get("workspace_validation_revision") or 0),
        )
        retained = (observations + [observation])[-VISUAL_OBSERVATION_METADATA_WINDOW:]
        visual["observations"] = retained
        visual["observation_count"] = visual["iteration"]
        visual["retained_observation_count"] = len(retained)
        visual["latest_observation_id"] = observation["id"]
        visual["updated_at"] = utc_iso()
        record["visual_mode"] = visual
        _write_json(_lease_path(resource_lease_id), record)
        _sync_visual_manifest_locked(record)
        return observation


def bump_workspace_validation_revision(lease_id: str) -> dict[str, Any]:
    """Invalidate all visual/goal evidence after a real workspace mutation."""
    with _metadata_lock():
        source = _active_mode_lease_locked(lease_id)
        record = _shared_root_record_locked(source)
        resource_lease_id = str(record.get("lease_id") or lease_id)
        revision = int(record.get("workspace_validation_revision") or 0) + 1
        record["workspace_validation_revision"] = revision
        visual = record.get("visual_mode")
        if isinstance(visual, dict):
            visual = json.loads(json.dumps(visual))
            visual["workspace_revision"] = revision
            visual["latest_review"] = None
            if visual.get("status") == "completed":
                visual.update(active=False, status="needs_review")
                visual.pop("completed_at", None)
            visual["updated_at"] = utc_iso()
            record["visual_mode"] = visual
        goal = record.get("goal_mode")
        if isinstance(goal, dict) and goal.get("status") == "completed":
            goal = json.loads(json.dumps(goal))
            goal.update(active=True, status="needs_review", latest_review=None, updated_at=utc_iso())
            goal.pop("validated_workspace_revision", None)
            goal.pop("completed_at", None)
            record["goal_mode"] = goal
        _write_json(_lease_path(resource_lease_id), record)
        _sync_visual_manifest_locked(record)
        return {"lease_id": lease_id, "shared_root_lease_id": resource_lease_id,
                "workspace_validation_revision": revision,
                "visual_mode": visual_mode_state(record, include_details=False),
                "goal_mode": goal_mode_state(record, include_details=False)}


def force_release_staying_workspace(
    requester_id: str,
    target_id: str,
    *,
    reason: str = "external user-requested termination",
) -> dict[str, Any]:
    """Independent-thread forced termination; ordinary release has no bypass flag.

    This is the explicit escape hatch for a target that must be abandoned rather
    than falsely marked successful.  It applies to Stay/Visual/Goal gates, but
    never to the caller's own lease.  Only the target's already registered
    Runtime Jobs are cancelled using the existing Job implementation; arbitrary
    process IDs are never accepted.
    """
    clean_reason = str(reason or "external user-requested termination").strip()[:1000]

    def validate_locked() -> tuple[dict[str, Any], dict[str, Any]]:
        target = _active_mode_lease_locked(target_id)
        requester = next((record for _path, record in _lease_records_locked() if record.get("lease_id") == requester_id), None)
        if not requester or requester_id == target_id or requester.get("thread_id") == target.get("thread_id"):
            raise RuntimePolicyError("外部驻场解锁拒绝", "只能由另一个不同 GPT thread 救援，目标不能解锁自己")
        if requester.get("mode") != "execute_exclusive" and requester.get("lease_type") != LEASE_TYPE_CONSOLE_RELAY:
            raise RuntimePolicyError("救援身份无效", "需要执行任务或独立 Console relay 身份")
        participants = [
            other for _path, other in _lease_records_locked()
            if _is_lease_participant(other)
            and str(other.get("shared_root_lease_id") or "") == target_id
        ]
        if participants:
            raise RuntimePolicyError(
                "Child 仍在共享租约中",
                "外部终止不能遗留 GPTAgent participant；先让 Child 退出或完成 Team 恢复流程",
                retryable=True,
                error_type="SHARED_LEASE_PARTICIPANTS_ACTIVE",
                details={"participant_count": len(participants)},
            )
        return requester, target

    with _metadata_lock():
        _requester, target = validate_locked()
        jobs = [job for job in _running_job_records_locked() if job.get("lease_id") == target_id]
    for job in jobs:
        result = cancel_job(str(job["job_id"]), workspace_lease_id=target_id, force=True)
        if result.get("pid_alive") or result.get("alive"):
            raise RuntimePolicyError("救援待完成", "目标拥有的 Runtime Job 尚未退出；保留目标租约以避免孤儿进程", retryable=True)
    with _metadata_lock():
        _requester, target = validate_locked()
        if any(job.get("lease_id") == target_id for job in _running_job_records_locked()):
            raise RuntimePolicyError("救援待完成", "目标刚创建了新的 Runtime Job；请重新检查后重试救援", retryable=True)
        now = utc_iso()
        visual = target.get("visual_mode")
        if isinstance(visual, dict):
            visual = json.loads(json.dumps(visual))
            visual.update(active=False, status="forced", forced_at=now, forced_reason=clean_reason)
            visual.pop("until_at", None)
            target["visual_mode"] = visual
        goal = target.get("goal_mode")
        if isinstance(goal, dict) and (
            goal.get("active")
            or goal.get("status") in {"active", "needs_review", "completed"}
        ):
            goal = json.loads(json.dumps(goal))
            goal.update(
                active=False,
                status="aborted",
                aborted_at=now,
                abort_reason=clean_reason,
                updated_at=now,
                latest_review={
                    "status": "ABORTED",
                    "summary": "Externally terminated by an independent GPT thread; no success claim was made.",
                },
            )
            goal.pop("validated_workspace_revision", None)
            goal.pop("completed_at", None)
            target["goal_mode"] = goal
        for key in ("stay_started_at", "stay_until_at", "stay_duration_seconds", "stay_command_id"):
            target.pop(key, None)
        with _CHECKPOINT_OVERLAY_LOCK:
            overlay = _CHECKPOINT_OVERLAYS.pop(target_id, None)
            if overlay:
                target.update(overlay)
        _write_controller_envelope(target, closed_at=now)
        _sync_visual_manifest_locked(target)
        _lease_path(target_id).unlink(missing_ok=True)
        return {
            "status": "released",
            "lease_released": True,
            "lease_id": target_id,
            "forced": True,
            "reason": clean_reason,
            "goal_status": (
                str((target.get("goal_mode") or {}).get("status") or "")
                if isinstance(target.get("goal_mode"), dict) else ""
            ),
        }
