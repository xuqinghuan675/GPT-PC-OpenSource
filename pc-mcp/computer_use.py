"""Low-latency semantic Windows computer-use backend for GPT-PC.

A persistent PowerShell worker loads UI Automation once. Desktop snapshots are
scoped by Runtime lease + app and guarded by snapshot_id so concurrent
conversations cannot silently reinterpret stale element ids.

This module owns no Runtime/lease/control state. server.py remains the only
MCP surface and applies GPT-PC Runtime gates.
"""

from __future__ import annotations

import atexit
import json
import os
from pathlib import Path
import queue
import subprocess
import threading
import time
from typing import Any


class ComputerUseUnavailable(RuntimeError):
    """The local semantic desktop backend is unavailable."""


class ComputerUseError(RuntimeError):
    """The semantic desktop backend rejected or partially failed an operation."""

    def __init__(
        self,
        message: str,
        *,
        error_type: str = "COMPUTER_USE_FAILED",
        retryable: bool = False,
        details: dict[str, Any] | None = None,
    ) -> None:
        super().__init__(message)
        self.error_type = error_type
        self.retryable = retryable
        self.details = dict(details or {})


class ComputerUseOutcomeUnknown(ComputerUseError):
    """A side-effect request was sent but its completion reply was lost."""

    def __init__(self, message: str) -> None:
        super().__init__(
            message,
            error_type="COMPUTER_USE_OUTCOME_UNKNOWN",
            retryable=False,
            details={"unknown_outcome": True, "instruction": "Refresh state and reconcile before any retry."},
        )


_ALLOWED_ACTIONS = {
    "apps", "state", "click", "type_text", "press_key", "set_value", "scroll", "batch",
}
_BATCH_ACTIONS = {"click", "type_text", "press_key", "set_value", "scroll"}
_READ_ONLY_ACTIONS = {"apps", "state"}
_MAX_BATCH_CALLS = 32
_MAX_NODES = 600
_MAX_DEPTH = 64
_WORKER_PATH = Path(__file__).with_name("computer_use_worker.ps1")
_KEY_MODIFIERS = frozenset({"ctrl", "control", "shift", "alt", "win", "super"})
_KEY_NAMES = frozenset({
    "enter", "return", "tab", "escape", "esc", "backspace", "delete", "space",
    "left", "up", "right", "down", "home", "end", "page_up", "page_down",
})
_FUNCTION_KEYS = frozenset(f"f{i}" for i in range(1, 13))
_FOCUS_MODES = frozenset({"background", "foreground"})


def _bounded_int(value: Any, name: str, *, minimum: int, maximum: int, default: int) -> int:
    if value is None:
        return default
    if isinstance(value, bool) or not isinstance(value, int):
        raise ValueError(f"{name} must be an integer")
    if not minimum <= value <= maximum:
        raise ValueError(f"{name} must be between {minimum} and {maximum}")
    return value


def _clean_snapshot_id(value: str | None) -> str | None:
    clean = str(value or "").strip()
    if not clean:
        return None
    if len(clean) > 128:
        raise ValueError("snapshot_id is too long")
    return clean


def _normalise_focus_mode(value: str | None) -> str:
    clean = str(value or "background").strip().lower()
    if clean not in _FOCUS_MODES:
        raise ValueError("focus_mode must be background or foreground")
    return clean


def _normalise_key(value: str | None) -> str:
    clean = str(value or "").strip()
    if not clean:
        raise ValueError("press_key requires key")
    parts = [part.strip().lower() for part in clean.split("+")]
    if any(not part for part in parts):
        raise ValueError("key contains an empty modifier or main key")
    invalid_modifiers = [part for part in parts[:-1] if part not in _KEY_MODIFIERS]
    if invalid_modifiers:
        raise ValueError(f"unsupported key modifier: {invalid_modifiers[0]}")
    main = parts[-1]
    valid_main = (
        main in _KEY_NAMES
        or (len(main) == 1 and ("a" <= main <= "z" or "0" <= main <= "9"))
        or main in _FUNCTION_KEYS
    )
    if not valid_main:
        raise ValueError(f"unsupported key: {main}")
    return "+".join(parts)


def _normalise_request(
    *, action: str, app: str | None, snapshot_id: str | None, element: int | None,
    text: str | None, key: str | None, x: int | None, y: int | None,
    button: str, count: int, direction: str, amount: float,
    calls: list[dict[str, Any]] | None, max_nodes: int, max_depth: int,
    refresh_after: bool, focus_mode: str,
) -> dict[str, Any]:
    clean_action = str(action or "").strip().lower()
    if clean_action not in _ALLOWED_ACTIONS:
        raise ValueError(f"unsupported computer_use action: {clean_action}")
    clean_app = str(app or "").strip() or None
    clean_snapshot = _clean_snapshot_id(snapshot_id)
    clean_focus_mode = _normalise_focus_mode(focus_mode)
    request: dict[str, Any] = {
        "action": clean_action,
        "app": clean_app,
        "focus_mode": clean_focus_mode,
        "max_nodes": _bounded_int(max_nodes, "max_nodes", minimum=32, maximum=_MAX_NODES, default=300),
        "max_depth": _bounded_int(max_depth, "max_depth", minimum=1, maximum=_MAX_DEPTH, default=24),
        "refresh_after": bool(refresh_after),
    }
    if clean_action == "apps":
        return request
    if clean_app is None:
        raise ValueError(f"{clean_action} requires app")
    if clean_action == "state":
        return request
    if clean_snapshot is None:
        raise ValueError(f"{clean_action} requires snapshot_id from the latest state call")
    request["snapshot_id"] = clean_snapshot

    if clean_action == "batch":
        if not isinstance(calls, list) or not calls:
            raise ValueError("batch requires a non-empty calls list")
        if len(calls) > _MAX_BATCH_CALLS:
            raise ValueError(f"batch supports at most {_MAX_BATCH_CALLS} calls")
        normalised_calls: list[dict[str, Any]] = []
        for index, item in enumerate(calls):
            if not isinstance(item, dict):
                raise ValueError(f"calls[{index}] must be an object")
            if "focus_mode" in item:
                raise ValueError(
                    f"calls[{index}].focus_mode is not allowed; "
                    "batch subcalls inherit the top-level focus_mode"
                )
            call_action = str(item.get("action") or "").strip().lower()
            if call_action not in _BATCH_ACTIONS:
                raise ValueError(f"calls[{index}].action must be one of {sorted(_BATCH_ACTIONS)}")
            call = _normalise_request(
                action=call_action,
                app=clean_app,
                snapshot_id=clean_snapshot,
                element=item.get("element"),
                text=item.get("text"),
                key=item.get("key"),
                x=item.get("x"),
                y=item.get("y"),
                button=item.get("button", "left"),
                count=item.get("count", 1),
                direction=item.get("direction", "down"),
                amount=item.get("amount", 1.0),
                calls=None,
                max_nodes=request["max_nodes"],
                max_depth=request["max_depth"],
                refresh_after=False,
                focus_mode=clean_focus_mode,
            )
            for inherited_key in (
                "app", "snapshot_id", "focus_mode", "max_nodes", "max_depth", "refresh_after"
            ):
                call.pop(inherited_key, None)
            normalised_calls.append(call)
        request["calls"] = normalised_calls
        return request

    if element is not None:
        if isinstance(element, bool) or not isinstance(element, int) or element <= 0:
            raise ValueError("element must be a positive integer")
        request["element"] = element
    if clean_action == "click":
        clean_button = str(button or "left").strip().lower()
        if clean_button not in {"left", "right", "middle"}:
            raise ValueError("button must be left, right, or middle")
        request.update({
            "button": clean_button,
            "count": _bounded_int(count, "count", minimum=1, maximum=3, default=1),
        })
        if element is None:
            if isinstance(x, bool) or not isinstance(x, int) or isinstance(y, bool) or not isinstance(y, int):
                raise ValueError("click requires element or integer x/y screen coordinates")
            request.update({"x": x, "y": y})
    elif clean_action in {"type_text", "set_value"}:
        if element is None:
            raise ValueError(f"{clean_action} requires element")
        if text is None:
            raise ValueError(f"{clean_action} requires text")
        request["text"] = str(text)
    elif clean_action == "press_key":
        request["key"] = _normalise_key(key)
        if element is not None:
            request["element"] = element
    elif clean_action == "scroll":
        clean_direction = str(direction or "down").strip().lower()
        if clean_direction not in {"up", "down", "left", "right"}:
            raise ValueError("direction must be up, down, left, or right")
        if isinstance(amount, bool) or not isinstance(amount, (int, float)) or amount <= 0:
            raise ValueError("amount must be positive")
        request.update({"direction": clean_direction, "amount": min(float(amount), 10.0)})
        if element is not None:
            request["element"] = element
    return request


class _PersistentPowerShellBridge:
    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._process: subprocess.Popen[str] | None = None

    def _start(self) -> subprocess.Popen[str]:
        if os.name != "nt":
            raise ComputerUseUnavailable("computer_use is available only on Windows")
        if not _WORKER_PATH.is_file():
            raise ComputerUseUnavailable(f"computer-use worker is missing: {_WORKER_PATH}")
        process = subprocess.Popen(
            ["powershell.exe", "-NoLogo", "-NoProfile", "-NonInteractive",
             "-ExecutionPolicy", "Bypass", "-File", str(_WORKER_PATH)],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            text=True,
            encoding="utf-8",
            errors="replace",
            bufsize=1,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
        if process.stdin is None or process.stdout is None:
            process.kill()
            raise ComputerUseUnavailable("failed to open computer-use worker pipes")
        self._process = process
        return process

    def _live_process(self) -> subprocess.Popen[str]:
        process = self._process
        if process is None or process.poll() is not None:
            self.close()
            process = self._start()
        return process

    @staticmethod
    def _readline_with_timeout(process: subprocess.Popen[str], timeout_seconds: float) -> str:
        assert process.stdout is not None
        result: queue.Queue[object] = queue.Queue(maxsize=1)

        def reader() -> None:
            try:
                result.put(process.stdout.readline())
            except BaseException as exc:
                result.put(exc)

        threading.Thread(target=reader, name="gpt-pc-computer-use-read", daemon=True).start()
        try:
            value = result.get(timeout=max(0.1, timeout_seconds))
        except queue.Empty as exc:
            raise TimeoutError("computer-use worker timed out") from exc
        if isinstance(value, BaseException):
            raise ComputerUseUnavailable(str(value))
        return str(value)

    def call(
        self,
        request: dict[str, Any],
        *,
        timeout_seconds: float = 15.0,
        retry_safe: bool = False,
    ) -> dict[str, Any]:
        payload = json.dumps(request, ensure_ascii=False, separators=(",", ":"))
        attempts = 2 if retry_safe else 1
        with self._lock:
            for attempt in range(attempts):
                process = self._live_process()
                sent = False
                try:
                    assert process.stdin is not None
                    sent = True
                    process.stdin.write(payload + "\n")
                    process.stdin.flush()
                    line = self._readline_with_timeout(process, timeout_seconds)
                    if not line:
                        raise ComputerUseUnavailable("computer-use worker exited without a response")
                    response = json.loads(line)
                    if not isinstance(response, dict):
                        raise ComputerUseUnavailable("computer-use worker returned a non-object response")
                    return response
                except (BrokenPipeError, OSError, TimeoutError, json.JSONDecodeError, ComputerUseUnavailable) as exc:
                    self.close()
                    if attempt + 1 < attempts:
                        continue
                    if sent and not retry_safe:
                        raise ComputerUseOutcomeUnknown(
                            "Computer Use side-effect request was sent but its reply was lost; "
                            "do not replay it blindly. Refresh state and reconcile first."
                        ) from exc
                    if isinstance(exc, ComputerUseUnavailable):
                        raise
                    raise ComputerUseUnavailable(str(exc)) from exc
        raise ComputerUseUnavailable("computer-use worker failed")

    def close(self) -> None:
        process, self._process = self._process, None
        if process is None:
            return
        try:
            if process.stdin is not None:
                process.stdin.close()
        except OSError:
            pass
        try:
            if process.poll() is None:
                process.terminate()
                process.wait(timeout=2)
        except (OSError, subprocess.TimeoutExpired):
            try:
                process.kill()
            except OSError:
                pass


_BRIDGE = _PersistentPowerShellBridge()
atexit.register(_BRIDGE.close)


def execute(
    *, action: str, session_id: str, app: str | None = None,
    snapshot_id: str | None = None, element: int | None = None,
    text: str | None = None, key: str | None = None, x: int | None = None,
    y: int | None = None, button: str = "left", count: int = 1,
    direction: str = "down", amount: float = 1.0,
    calls: list[dict[str, Any]] | None = None, max_nodes: int = 300,
    max_depth: int = 24, refresh_after: bool = False,
    focus_mode: str = "background",
    timeout_seconds: float = 15.0,
) -> dict[str, Any]:
    clean_session = str(session_id or "").strip()
    if not clean_session:
        raise ValueError("session_id is required")
    if isinstance(timeout_seconds, bool) or not isinstance(timeout_seconds, (int, float)) or not 1 <= float(timeout_seconds) <= 60:
        raise ValueError("timeout_seconds must be between 1 and 60")
    request = _normalise_request(
        action=action, app=app, snapshot_id=snapshot_id, element=element,
        text=text, key=key, x=x, y=y, button=button, count=count,
        direction=direction, amount=amount, calls=calls, max_nodes=max_nodes,
        max_depth=max_depth, refresh_after=refresh_after, focus_mode=focus_mode,
    )
    request["session_id"] = clean_session
    started = time.perf_counter()
    response = _BRIDGE.call(
        request,
        timeout_seconds=float(timeout_seconds),
        retry_safe=request["action"] in _READ_ONLY_ACTIONS,
    )
    response.setdefault("backend", "windows_uia_persistent_powershell")
    response["elapsed_ms"] = round((time.perf_counter() - started) * 1000, 2)
    if not response.get("ok", False):
        message = str(response.get("error") or "computer-use operation failed")
        stale = "snapshot" in message.lower() or "cached ui state" in message.lower()
        if request["action"] == "batch":
            if response.get("failed_at") == 0 and stale:
                raise ComputerUseError(
                    message,
                    error_type="COMPUTER_USE_STALE_SNAPSHOT",
                    retryable=True,
                    details=response,
                )
            raise ComputerUseError(
                f"Computer Use batch stopped at step {response.get('failed_at')}: {message}",
                error_type="COMPUTER_USE_PARTIAL_FAILURE",
                retryable=False,
                details=response,
            )
        raise ComputerUseError(
            message,
            error_type="COMPUTER_USE_STALE_SNAPSHOT" if stale else "COMPUTER_USE_FAILED",
            retryable=stale,
            details=response,
        )
    return response
