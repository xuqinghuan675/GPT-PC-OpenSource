from __future__ import annotations

from pathlib import Path

import pytest

import computer_use
import server


def normalised(action: str, **overrides):
    values = dict(
        action=action,
        app="notepad",
        snapshot_id="snap123",
        element=1,
        text=None,
        key=None,
        x=None,
        y=None,
        button="left",
        count=1,
        direction="down",
        amount=1.0,
        calls=None,
        max_nodes=300,
        max_depth=24,
        refresh_after=False,
        focus_mode="background",
    )
    values.update(overrides)
    return computer_use._normalise_request(**values)


def test_state_does_not_require_snapshot_but_mutation_does():
    state = normalised("state", snapshot_id=None, element=None)
    assert state["action"] == "state" and "snapshot_id" not in state
    with pytest.raises(ValueError, match="snapshot_id"):
        normalised("click", snapshot_id=None)


def test_batch_accepts_only_already_determined_side_effect_actions():
    request = normalised(
        "batch",
        element=None,
        calls=[
            {"action": "click", "element": 2},
            {"action": "type_text", "element": 3, "text": "hello"},
        ],
        refresh_after=True,
    )
    assert request["snapshot_id"] == "snap123"
    assert len(request["calls"]) == 2
    with pytest.raises(ValueError, match="calls\\[0\\].action"):
        normalised("batch", element=None, calls=[{"action": "state"}])


def test_focus_mode_defaults_to_background_and_validates_explicit_foreground():
    assert normalised("click")["focus_mode"] == "background"
    assert normalised("click", focus_mode="foreground")["focus_mode"] == "foreground"
    with pytest.raises(ValueError, match="focus_mode"):
        normalised("click", focus_mode="auto")


def test_batch_subcalls_inherit_focus_mode_and_cannot_override_it():
    request = normalised(
        "batch",
        element=None,
        focus_mode="foreground",
        calls=[{"action": "press_key", "key": "enter"}],
    )
    assert request["focus_mode"] == "foreground"
    assert "focus_mode" not in request["calls"][0]
    with pytest.raises(ValueError, match="inherit"):
        normalised(
            "batch",
            element=None,
            calls=[{"action": "press_key", "key": "enter", "focus_mode": "foreground"}],
        )


@pytest.mark.parametrize(
    ("call", "message"),
    [
        ({"action": "click", "x": 1, "y": 2, "button": "banana"}, "button"),
        ({"action": "scroll", "direction": "diagonal"}, "direction"),
        ({"action": "type_text", "element": 1}, "requires text"),
        ({"action": "click", "element": -1}, "positive integer"),
    ],
)
def test_batch_subcalls_use_the_same_validation_as_single_actions(call, message):
    with pytest.raises(ValueError, match=message):
        normalised("batch", element=None, calls=[call])


@pytest.mark.parametrize("key", ["ctrl+notakey", "bogus+enter", "ctrl++a", "f01", "f13"])
def test_press_key_rejects_unsupported_or_ambiguous_keys(key):
    with pytest.raises(ValueError):
        normalised("press_key", element=None, key=key)


def test_press_key_normalises_supported_key_chords():
    request = normalised("press_key", element=None, key=" CTRL + Shift + F12 ")
    assert request["key"] == "ctrl+shift+f12"


def test_click_requires_semantic_element_or_coordinates():
    with pytest.raises(ValueError, match="element or integer x/y"):
        normalised("click", element=None)


def test_execute_passes_lease_session_and_uses_one_backend_call(monkeypatch):
    seen = []
    def fake_call(request, *, timeout_seconds, retry_safe):
        seen.append((request, timeout_seconds, retry_safe))
        return {"ok": True, "action": request["action"]}
    monkeypatch.setattr(computer_use._BRIDGE, "call", fake_call)
    result = computer_use.execute(
        action="batch",
        session_id="lease-a",
        app="fixture",
        snapshot_id="snap-a",
        calls=[{"action": "click", "element": 1}, {"action": "press_key", "key": "enter"}],
    )
    assert result["ok"] is True
    assert len(seen) == 1
    assert seen[0][0]["session_id"] == "lease-a"
    assert seen[0][0]["snapshot_id"] == "snap-a"
    assert seen[0][0]["focus_mode"] == "background"
    assert seen[0][2] is False


def test_batch_partial_failure_is_an_error(monkeypatch):
    monkeypatch.setattr(
        computer_use._BRIDGE,
        "call",
        lambda request, **kwargs: {
            "ok": False,
            "action": "batch",
            "failed_at": 1,
            "error": "elementNotFound(3)",
            "results": [{"ok": True}, {"ok": False}],
        },
    )
    with pytest.raises(computer_use.ComputerUseError) as exc:
        computer_use.execute(
            action="batch",
            session_id="lease-a",
            app="fixture",
            snapshot_id="snap-a",
            calls=[{"action": "click", "element": 1}, {"action": "click", "element": 3}],
        )
    assert exc.value.error_type == "COMPUTER_USE_PARTIAL_FAILURE"
    assert exc.value.retryable is False
    assert exc.value.details["failed_at"] == 1


def test_batch_stale_snapshot_before_first_side_effect_is_retryable(monkeypatch):
    monkeypatch.setattr(
        computer_use._BRIDGE,
        "call",
        lambda request, **kwargs: {
            "ok": False,
            "action": "batch",
            "failed_at": 0,
            "error": "staleSnapshot('old'). Refresh state and retry.",
            "results": [{"ok": False, "error": "staleSnapshot('old')", "index": 0}],
        },
    )
    with pytest.raises(computer_use.ComputerUseError) as exc:
        computer_use.execute(
            action="batch",
            session_id="lease-a",
            app="fixture",
            snapshot_id="old",
            calls=[{"action": "click", "element": 1}],
        )
    assert exc.value.error_type == "COMPUTER_USE_STALE_SNAPSHOT"
    assert exc.value.retryable is True
    assert exc.value.details["failed_at"] == 0


def test_side_effect_transport_failure_is_not_replayed(monkeypatch):
    class FakeStdin:
        def write(self, _value):
            return None
        def flush(self):
            return None
        def close(self):
            return None

    class FakeProcess:
        stdin = FakeStdin()
        stdout = object()
        def poll(self):
            return None
        def terminate(self):
            return None
        def wait(self, timeout=None):
            return 0

    bridge = computer_use._PersistentPowerShellBridge()
    calls = {"live": 0}
    process = FakeProcess()

    def live():
        calls["live"] += 1
        return process

    monkeypatch.setattr(bridge, "_live_process", live)
    monkeypatch.setattr(
        bridge,
        "_readline_with_timeout",
        lambda process, timeout_seconds: (_ for _ in ()).throw(TimeoutError("late reply")),
    )
    with pytest.raises(computer_use.ComputerUseOutcomeUnknown):
        bridge.call({"action": "click"}, retry_safe=False)
    assert calls["live"] == 1


def test_desktop_lease_boundary(monkeypatch):
    monkeypatch.setattr(server, "_active_thread_lease", lambda _lease: {"lease_id": "x", "lease_type": "workspace"})
    with pytest.raises(server.RuntimePolicyError) as exc:
        server._computer_use_desktop_lease("x")
    assert exc.value.error_type == "DESKTOP_OPS_LEASE_REQUIRED"

    monkeypatch.setattr(server, "_active_thread_lease", lambda _lease: {"lease_id": "desktop", "lease_type": "desktop_ops"})
    assert server._computer_use_desktop_lease("desktop")["lease_id"] == "desktop"


def test_worker_guards_snapshot_scope_and_generic_lists():
    worker = Path(computer_use.__file__).with_name("computer_use_worker.ps1").read_text(encoding="utf-8")
    assert "Snapshot-Key" in worker
    assert "snapshot_id=$snapshotId" in worker
    assert "staleSnapshot" in worker
    assert "pid=$p.Id" in worker
    assert "Get-Process -Id ([int]$s.pid)" in worker
    assert "target process/window changed" in worker
    assert "$rows.ToArray()" in worker
    assert "$results.ToArray()" in worker
    assert "$n.ToArray()" in worker
    assert "@($rows)" not in worker
    assert "@($results)" not in worker
    assert "will not use UIA ValuePattern fallback because it may steal focus" in worker
    assert "Target-IsForeground" in worker
    assert "Window-Click -h " in worker
    assert "return[ordered]@" not in worker
    assert "return$c" not in worker
    assert "$error=$null" not in worker.lower()
    assert 'focusMode="background"' in worker
    assert 'if($m-eq"foreground" -and -not$before)' in worker
    assert "[GPTPCWin32]::SetForegroundWindow" in worker
    assert "foregroundActivationFailed" in worker
    assert "activated_foreground" in worker
    assert "$v=Vk $main;foreach($m in $mods)" in worker
