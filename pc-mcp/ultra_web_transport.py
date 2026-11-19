"""ChatGPT Web transport for GPTAgent Ultra.

The local Node bridge attaches to the already signed-in dedicated ChatGPT page
through raw CDP and executes fixed same-origin protocol operations. Credentials
and one-shot admission tokens stay inside the page. No Platform API, Work/Codex
task, DOM composer typing, or automatic replay is used.
"""
from __future__ import annotations

import copy
import json
from pathlib import Path
import re
import shutil
import subprocess
import time
from urllib.parse import quote

from chatgpt_agent_store import AgentError
from machine_config import load_machine_config
from ultra_team_store import PROJECT_NAME

PROJECT_ID = re.compile(r"^g-p-[a-fA-F0-9]{32}$")
CONVERSATION_ID = re.compile(r"^[a-fA-F0-9]{8}-[a-fA-F0-9-]{27,36}$")
READ_BRIDGE_TIMEOUT_SECONDS = 35.0
READ_BRIDGE_TIMEOUT_RETRIES = 1


def validate_project(data: dict) -> dict:
    if (
        not isinstance(data, dict)
        or data.get("name") != PROJECT_NAME
        or not PROJECT_ID.fullmatch(str(data.get("id") or data.get("project_id") or ""))
    ):
        raise AgentError(
            "PROJECT_NOT_VERIFIED",
            "Expected the exact GPTAgent Project, not a similarly named chat or GPT",
        )
    if data.get("type") not in (None, "snorlax"):
        raise AgentError("PROJECT_TYPE_MISMATCH", "The selected object is not a ChatGPT Project")
    return data


def select_model(catalog: dict, requested: str = "non-pro", effort: str = "extra_high") -> dict:
    requested_effort = {"extra_high": "max", "high": "extended", "medium": "standard"}.get(effort)
    if not requested_effort:
        raise AgentError("MODEL_EFFORT_INVALID", "Unsupported effort")
    versions = catalog.get("versions") or []
    ordered = sorted(versions, key=lambda item: item.get("id") != "latest")
    models = {item.get("slug") for item in catalog.get("models", [])}
    for version in ordered:
        if version.get("enabled") is False:
            continue
        for preset in version.get("presets") or []:
            slug = str(preset.get("model_slug") or "")
            if (
                preset.get("preset_type") != "available"
                or preset.get("enabled") is False
                or preset.get("lane") != "thinking"
                or preset.get("thinking_effort") != requested_effort
                or slug not in models
                or "pro" in slug.casefold()
                or slug.endswith("-wm")
                or "codex" in slug.casefold()
            ):
                continue
            if requested != "non-pro" and requested != slug:
                continue
            return {"model": slug, "effort": requested_effort, "requested_effort": effort}
    raise AgentError(
        "MODEL_POLICY_UNAVAILABLE",
        "No account-visible normal Chat model satisfies the exact model/effort policy; no downgrade was attempted",
    )


def verify_snapshot(snapshot: dict, conversation_id: str, project_id: str, user_message_id: str) -> dict:
    if snapshot.get("conversation_id") != conversation_id or snapshot.get("project_id") != project_id:
        raise AgentError(
            "CONVERSATION_PROJECT_MISMATCH",
            "Conversation/project read-back does not match the requested GPTAgent worker",
        )
    mapping = snapshot.get("mapping") or {}
    node = mapping.get(user_message_id)
    if (
        not node
        or (node.get("message") or {}).get("id") != user_message_id
        or (node.get("message") or {}).get("role") != "user"
    ):
        raise AgentError(
            "USER_MESSAGE_NOT_OBSERVED",
            "The exact new user message was not present; do not resend",
        )

    queue, seen, finals = list(node.get("children") or []), set(), []
    while queue:
        message_id = queue.pop()
        if message_id in seen:
            continue
        seen.add(message_id)
        branch = mapping.get(message_id) or {}
        message = branch.get("message") or {}
        # A later user node belongs to another turn and is a hard branch boundary.
        if message.get("role") == "user":
            continue
        if (
            message.get("role") == "assistant"
            and message.get("end_turn") is True
            and message.get("status") == "finished_successfully"
            and message.get("channel") not in {"analysis", "justify", "confidence"}
            and message.get("text")
        ):
            finals.append(
                {
                    "message_id": message.get("id") or message_id,
                    "text": message["text"],
                    "model": message.get("model"),
                    "effort": message.get("effort"),
                }
            )
            continue
        queue.extend(branch.get("children") or [])

    if len(finals) > 1:
        raise AgentError(
            "TURN_BRANCH_AMBIGUOUS",
            "Multiple terminal responses belong to this turn; do not guess a branch",
        )
    final = finals[0] if finals else None
    return {"user_message_id": user_message_id, "terminal": final is not None, "final": final}


class BrowserWebTransport:
    def __init__(self, config):
        self.config = config
        self._project_cache = None
        self._project_cache_at = 0.0
        self._model_cache: dict[tuple[str, str, str], tuple[float, dict]] = {}
        self._owner_cache: dict | None = None
        self._owner_cache_at = 0.0
        self._bridge_path = Path(__file__).with_name("ultra_cdp_bridge.mjs")

    @staticmethod
    def _fast_owner_matches(owner: dict | None) -> bool:
        if not owner or not owner.get("pid") or owner.get("process_create_time") is None:
            return False
        try:
            import psutil
            process = psutil.Process(int(owner["pid"]))
            return (
                process.is_running()
                and abs(float(process.create_time()) - float(owner["process_create_time"])) < 0.01
            )
        except (psutil.NoSuchProcess, psutil.AccessDenied, ValueError, TypeError):
            return False

    def _browser_owner(self, *, fresh: bool = False) -> dict:
        from chatgpt_browser_session import inspect_browser_owner

        if (
            not fresh
            and self._owner_cache
            and time.monotonic() - self._owner_cache_at < 60.0
            and self._fast_owner_matches(self._owner_cache)
        ):
            return dict(self._owner_cache)
        owner = inspect_browser_owner(self.config)
        if not owner or not owner.get("listening"):
            self._owner_cache = None
            self._owner_cache_at = 0.0
            raise AgentError(
                "BROWSER_NOT_STARTED",
                "Verified dedicated GPTAgent browser is not running; no implicit launch",
            )
        self._owner_cache = dict(owner)
        self._owner_cache_at = time.monotonic()
        return dict(owner)

    def _bridge(self, action: str, *, timeout_seconds: float = 25.0, **args):
        mutation = action in {"start", "admission"}
        # Full ownership inspection is expensive on Windows. A fresh strict
        # inspection is done when the cache is absent/stale; within that
        # bounded window the exact PID/create_time is checked in O(1).
        owner = self._browser_owner(fresh=False)
        if not self._bridge_path.is_file():
            raise AgentError("CDP_BRIDGE_MISSING", "The fixed Ultra CDP bridge is missing")
        node = shutil.which("node")
        if not node:
            raise AgentError("NODE_REQUIRED", "Node.js is required for the raw CDP bridge")

        request = {
            "action": action,
            "cdp_endpoint": self.config.cdp_endpoint,
            "timeout_ms": max(1000, min(int(timeout_seconds * 1000), 60000)),
            "args": args,
        }
        try:
            completed = subprocess.run(
                [node, str(self._bridge_path)],
                input=json.dumps(request, ensure_ascii=False, separators=(",", ":")),
                text=True,
                encoding="utf-8",
                capture_output=True,
                timeout=timeout_seconds + 5,
                check=False,
            )
        except subprocess.TimeoutExpired as exc:
            raise AgentError("CDP_BRIDGE_TIMEOUT", "Raw CDP bridge timed out") from exc

        try:
            response = json.loads(completed.stdout or "{}")
        except ValueError as exc:
            raise AgentError(
                "CDP_BRIDGE_INVALID",
                "Raw CDP bridge returned invalid JSON",
                stderr=(completed.stderr or "")[-1000:],
            ) from exc
        if completed.returncode != 0 or not response.get("ok"):
            raise AgentError(
                str(response.get("error") or "CDP_BRIDGE_FAILED"),
                str(response.get("message") or "Raw CDP bridge failed"),
                stderr=(completed.stderr or "")[-1000:],
            )

        if mutation and not self._fast_owner_matches(owner):
            raise AgentError("BROWSER_OWNER_CHANGED", "Browser ownership changed during admission")
        return response.get("value")

    def _read(self, path: str, kind: str, account_tag: str | None = None):
        # GET/read operations are safe to retry. A newly-created ChatGPT
        # conversation can transiently return HTTP 429 while the web backend
        # settles; treating that as "plugin unavailable" makes the first Child
        # turn fail even though the exact same join succeeds moments later.
        #
        # The page-side read performs two serial network phases: sessionInfo()
        # and the requested backend GET. They historically allowed 12s each,
        # which left almost no margin inside the old 25s CDP command budget.
        # The bridge now bounds those read-only phases to 8s + 10s; keep a 35s
        # outer budget for renderer scheduling/serialization and retry one exact
        # bridge timeout. This never replays a model submission or mutation.
        delays = (0.8, 1.5, 2.5, 4.0, 6.0)
        last = None
        bridge_timeout_count = 0
        for attempt in range(len(delays) + 1):
            try:
                result = self._bridge(
                    "read",
                    timeout_seconds=READ_BRIDGE_TIMEOUT_SECONDS,
                    path=path,
                    kind=kind,
                )
            except AgentError as exc:
                bridge_timed_out = (
                    exc.code == "CDP_BRIDGE_TIMEOUT"
                    or (
                        exc.code == "CDP_BRIDGE_FAILED"
                        and "timeout" in str(exc).casefold()
                    )
                )
                if not bridge_timed_out:
                    raise
                if bridge_timeout_count < READ_BRIDGE_TIMEOUT_RETRIES:
                    time.sleep(delays[min(bridge_timeout_count, len(delays) - 1)])
                    bridge_timeout_count += 1
                    continue
                raise AgentError(
                    "WEB_READ_TIMEOUT",
                    "ChatGPT read exceeded the bounded CDP/page network budget after a safe read-only retry; no mutation was replayed",
                    bridge_error=exc.code,
                ) from exc
            if not isinstance(result, dict):
                raise AgentError("WEB_READ_INVALID", "ChatGPT read returned an invalid envelope")
            if not result.get("error"):
                if account_tag and result.get("account_tag") != account_tag:
                    raise AgentError("TEAM_ACCOUNT_CHANGED", "Logged-in account changed")
                return result
            last = result
            status = result.get("http_status")
            if result.get("error") == "WEB_READ_REJECTED" and status == 429 and attempt < len(delays):
                raw_retry = str(result.get("retry_after") or "").strip()
                try:
                    header_delay = float(raw_retry)
                except ValueError:
                    header_delay = 0.0
                time.sleep(max(delays[attempt], min(max(header_delay, 0.0), 10.0)))
                continue
            break
        if last and last.get("error") == "WEB_READ_REJECTED" and last.get("http_status") == 429:
            raise AgentError(
                "WEB_READ_THROTTLED",
                "ChatGPT read is temporarily rate-limited (HTTP 429). The GPT-PC app is not missing; retry this same read/join without recreating the Child or resending the task.",
                http_status=429,
                retry_after=last.get("retry_after"),
            )
        raise AgentError(
            last.get("error") if last else "WEB_READ_INVALID",
            "ChatGPT read did not return the expected authenticated data",
            http_status=(last or {}).get("http_status"),
            retry_after=(last or {}).get("retry_after"),
        )

    def project(self):
        if self._project_cache and time.monotonic() - self._project_cache_at < 60:
            return dict(self._project_cache)
        cursor, seen, matches, account_tag = None, set(), {}, None
        for _ in range(100):
            path = "/backend-api/gizmos/snorlax/sidebar?limit=50&owned_only=true&conversations_per_gizmo=0"
            if cursor:
                path += "&cursor=" + quote(str(cursor), safe="")
            result = self._read(path, "projects", account_tag)
            account_tag = result["account_tag"]
            for project in result["data"]["items"]:
                if project.get("name") == PROJECT_NAME:
                    validate_project(project)
                    matches[project["id"]] = project
            cursor = result["data"].get("cursor")
            if not cursor:
                break
            if cursor in seen:
                raise AgentError(
                    "PROJECT_PAGINATION_STALLED",
                    "Project pagination did not advance",
                )
            seen.add(cursor)
        else:
            raise AgentError("PROJECT_LIST_INCOMPLETE", "Project list exceeded the bounded lookup")

        if len(matches) != 1:
            raise AgentError(
                "PROJECT_MISSING" if not matches else "PROJECT_AMBIGUOUS",
                "Exactly one existing GPTAgent Project is required; no root-chat fallback",
            )
        found = next(iter(matches.values()))
        detail = self._read("/backend-api/gizmos/" + quote(found["id"]), "project", account_tag)["data"]
        validate_project(detail)
        if detail.get("id") != found["id"]:
            raise AgentError("PROJECT_ID_CHANGED", "Project detail did not match the discovered Project")

        result = {
            "name": PROJECT_NAME,
            "project_id": found["id"],
            "account_tag": account_tag,
            "short_url": detail.get("short_url") or found.get("short_url") or found["id"],
            "evidence": "live_project_list_and_detail",
            "transport": "raw-cdp-browser-context",
        }
        self._project_cache = dict(result)
        self._project_cache_at = time.monotonic()
        return result

    def model(self, requested: str, effort: str, account_tag: str):
        key = (requested, effort, account_tag)
        cached = self._model_cache.get(key)
        if cached and time.monotonic() - cached[0] < 60:
            return dict(cached[1])
        catalog = self._read(
            "/backend-api/models?history_and_training_disabled=false&supports_model_picker_upgrade_presets=true",
            "models",
            account_tag,
        )["data"]
        result = select_model(catalog, requested, effort)
        self._model_cache[key] = (time.monotonic(), dict(result))
        return result

    def _plugin_hint(self) -> str:
        latest = load_machine_config(runtime_root=self.config.runtime_root)
        app_id = str(latest.plugin_app_id or self.config.plugin_app_id or "").strip()
        if not re.fullmatch(r"asdk_app_[a-f0-9]{32}", app_id):
            raise AgentError(
                "PLUGIN_APP_ID_REQUIRED",
                "Ultra requires chatgpt.plugin_app_id to match the currently installed GPT-PC app",
            )
        return "plugin:" + app_id

    def _payload_with_plugin(self, payload: dict) -> dict:
        result = copy.deepcopy(payload)
        hints = [item for item in (result.get("system_hints") or []) if isinstance(item, str)]
        plugin_hint = self._plugin_hint()
        hints = [item for item in hints if not item.startswith("plugin:asdk_app_")]
        hints.append(plugin_hint)
        result["system_hints"] = hints
        result.setdefault("client_prepare_state", "success")
        result.setdefault("supports_buffering", True)
        result.setdefault("supported_encodings", ["v1"])
        return result

    def admission(self, payload: dict, account_tag: str) -> dict:
        result = self._bridge(
            "admission",
            timeout_seconds=55,
            payload=self._payload_with_plugin(payload),
            account_tag=account_tag,
            project_id=payload.get("gizmo_id"),
        )
        if not isinstance(result, dict):
            raise AgentError("WEB_ADMISSION_INVALID", "Admission preflight returned an invalid result")
        return result

    def send(self, operation_id: str, payload: dict, account_tag: str):
        if (
            self.config.project_name != PROJECT_NAME
            or not PROJECT_ID.fullmatch(str(payload.get("gizmo_id") or ""))
        ):
            raise AgentError("PROJECT_POLICY_MISMATCH", "Send requires a GPTAgent project binding")
        if payload.get("conversation_mode") != {
            "kind": "gizmo_interaction",
            "gizmo_id": payload["gizmo_id"],
        }:
            raise AgentError(
                "PROJECT_POLICY_MISMATCH",
                "Conversation mode must match the exact Project",
            )
        project = self.project()
        if project["project_id"] != payload["gizmo_id"] or project["account_tag"] != account_tag:
            raise AgentError("TEAM_ACCOUNT_CHANGED", "Project/account changed before submission")

        result = self._bridge(
            "start",
            timeout_seconds=60,
            operation_id=operation_id,
            payload=self._payload_with_plugin(payload),
            account_tag=account_tag,
            project_id=project["project_id"],
        )
        if not isinstance(result, dict):
            raise AgentError("WEB_SEND_INVALID", "ChatGPT send returned an invalid receipt")
        for _ in range(12):
            if result.get("status") != "dispatching":
                break
            time.sleep(0.35)
            result = self.receipt(operation_id)
        return result

    def receipt(self, operation_id: str):
        result = self._bridge("receipt", operation_id=operation_id)
        return (
            dict(result)
            if isinstance(result, dict)
            else {"status": "unknown", "error": "PAGE_RECEIPT_INVALID"}
        )

    def conversation(self, conversation_id: str, account_tag: str):
        if not CONVERSATION_ID.fullmatch(conversation_id):
            raise AgentError("CONVERSATION_ID_INVALID", "Expected the exact web conversation UUID")
        last_error = None
        for attempt in range(7):
            try:
                return self._read(
                    "/backend-api/conversation/" + quote(conversation_id),
                    "conversation",
                    account_tag,
                )["data"]
            except AgentError as exc:
                if exc.code != "WEB_READ_REJECTED" or exc.details.get("http_status") != 404:
                    raise
                last_error = exc
                if attempt < 6:
                    time.sleep(min(0.2 * (2 ** attempt), 1.2))
        raise last_error

    def recent(self, project_id: str, account_tag: str):
        if not PROJECT_ID.fullmatch(project_id):
            raise AgentError("PROJECT_POLICY_MISMATCH", "Expected a project id")
        return self._read(
            "/backend-api/gizmos/"
            + quote(project_id)
            + "/conversations?limit=20&cursor=0",
            "project_chats",
            account_tag,
        )["data"]["items"]
