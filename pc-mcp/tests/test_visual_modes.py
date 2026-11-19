"""Isolated policy tests: never acquire/change the real desktop task's residency."""
from __future__ import annotations

import hashlib
import asyncio
import json
from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta
from pathlib import Path

from PIL import Image
import pytest
from mcp import Client
import runtime
import server
from test_server import isolate_runtime, call_tool


@pytest.fixture(autouse=True)
def sandbox(monkeypatch, tmp_path):
    isolate_runtime(monkeypatch, tmp_path)


def acquire(tmp_path: Path, name="workspace", thread_id=None):
    workspace = tmp_path / name
    workspace.mkdir()
    return runtime.acquire_workspace(workspace, mode="execute_exclusive", ttl_seconds=60,
                                     owner_label="Visual policy test", thread_id=thread_id)


def record(lease):
    return json.loads(runtime._lease_path(lease["lease_id"]).read_text(encoding="utf-8"))


def arm_confirmed_disconnect(lease, *, minutes_stale=5):
    """Prepare one fully confirmed disconnect episode with the 5m rescue window elapsed."""
    identity = lease["lease_id"]
    activity_at = runtime.utc_iso(runtime.utc_now() - timedelta(minutes=minutes_stale))
    value = record(lease)
    value["execution_checkpoint_updated_at"] = activity_at
    runtime._write_json(runtime._lease_path(identity), value)
    probe = runtime.ensure_disconnect_probe(identity, activity_at=activity_at, response_seconds=30)
    value = record(lease)
    value["disconnect_probe_delivered_at"] = runtime.utc_iso(runtime.utc_now() - timedelta(minutes=7))
    value["disconnect_probe_response_deadline_at"] = runtime.utc_iso(runtime.utc_now() - timedelta(minutes=6))
    value["disconnect_confirmed_at"] = runtime.utc_iso(runtime.utc_now() - timedelta(minutes=6))
    value["disconnect_confirmed_probe_id"] = probe["probe"]["id"]
    value["disconnect_rescue_deadline_at"] = runtime.utc_iso(runtime.utc_now() - timedelta(minutes=1))
    runtime._write_json(runtime._lease_path(identity), value)
    return activity_at, probe


def invariant(value):
    until = runtime._parse_utc(value["expires_at"])
    stay = runtime._mode_deadline(value.get("stay_until_at"))
    visual = value.get("visual_mode", {})
    if stay:
        assert until >= stay
    if visual.get("active"):
        visual_until = runtime._parse_utc(visual["until_at"])
        assert until >= visual_until
        if stay:
            assert visual_until >= stay


@pytest.mark.parametrize("command,seconds", [("/stay 10m",600),("/stay 3h",10800),("/stay 12h",43200),("/stay 24h",86400)])
def test_web_stay_really_persists(tmp_path, command, seconds):
    lease = acquire(tmp_path)
    response = call_tool("apply_stay_command", {"workspace_lease_id": lease["lease_id"], "command": command})
    assert response["stay_mode"]["active"]
    assert record(lease)["stay_duration_seconds"] == seconds
    invariant(record(lease))


def test_self_off_and_legacy_clear_are_forbidden(tmp_path):
    lease = acquire(tmp_path)
    runtime.apply_stay_command(lease["lease_id"], "/stay 3h")
    before = record(lease)
    for action in (lambda: runtime.apply_stay_command(lease["lease_id"], "/stay off"),
                   lambda: runtime.set_workspace_stay_mode(lease["lease_id"], duration_seconds=None),
                   lambda: runtime.set_workspace_stay_mode(lease["lease_id"], duration_seconds=3600)):
        with pytest.raises(runtime.RuntimePolicyError):
            action()
        assert record(lease) == before
    with pytest.raises(runtime.RuntimePolicyError, match="驻场"):
        runtime.release_workspace(lease["lease_id"])


def test_every_mode_and_legacy_renew_obeys_time_invariant(tmp_path):
    lease = acquire(tmp_path)
    identity = lease["lease_id"]
    runtime.set_visual_mode(identity, duration_seconds=10800)
    runtime.apply_stay_command(identity, "/stay 2h")
    invariant(record(lease))
    runtime.apply_stay_command(identity, "/stay 5h")
    invariant(record(lease))
    assert record(lease)["visual_mode"]["until_at"] == record(lease)["stay_until_at"]
    stay = record(lease)["stay_until_at"]
    runtime.set_visual_mode(identity, action="extend", duration_seconds=8*3600)
    assert record(lease)["stay_until_at"] == stay
    runtime.renew_workspace(identity, ttl_seconds=60)
    invariant(record(lease))
    runtime.update_workspace_execution_checkpoint(identity, tool_name="test", side_effect=False)
    invariant(record(lease))
    runtime.set_visual_mode(identity, action="extend", duration_seconds=10)
    invariant(record(lease))


def test_visual_requested_below_stay_is_promoted(tmp_path):
    lease = acquire(tmp_path)
    runtime.apply_stay_command(lease["lease_id"], "/stay 8h")
    runtime.set_visual_mode(lease["lease_id"], duration_seconds=3600)
    value = record(lease)
    assert value["visual_mode"]["until_at"] == value["stay_until_at"]
    invariant(value)


def test_parallel_mode_writes_never_break_invariant(tmp_path):
    lease = acquire(tmp_path)
    identity = lease["lease_id"]
    runtime.set_visual_mode(identity, duration_seconds=3600)
    runtime.apply_stay_command(identity, "/stay 3h")
    def mutate(index):
        if index % 2:
            runtime.renew_workspace(identity, ttl_seconds=60)
        else:
            runtime.set_visual_mode(identity, action="extend", duration_seconds=6*3600)
        invariant(record(lease))
    with ThreadPoolExecutor(max_workers=4) as pool:
        list(pool.map(mutate, range(12)))
    invariant(record(lease))


def observation(lease, name, size, **metadata):
    directory = runtime.VISUAL_DIR / lease["lease_id"] / "observations"
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / f"{name}.png"
    Image.new("RGB", size, "white").save(path)
    return runtime.record_visual_observation(lease["lease_id"], {
        "id": name, "path": str(path), "width": size[0], "height": size[1],
        "mime": "image/png", "sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
        "inspected_at": runtime.utc_iso(), "label": name, **metadata,
    })


def final_review():
    return {"desktop":"PASS", "mobile":"PASS", "critical_differences":0, "major_differences":0,
            "minor_differences":0, "reference_requirements":"satisfied",
            "final_observation_ids":{"desktop":"desktop-final", "mobile":"mobile-final"}}


def test_visual_completion_keeps_residency_and_evidence(tmp_path):
    lease = acquire(tmp_path)
    identity = lease["lease_id"]
    runtime.set_visual_mode(identity, duration_seconds=7200, criteria=["No horizontal overflow"])
    runtime.apply_stay_command(identity, "/stay 1h")
    observation(lease,"desktop-final",(1440,900))
    observation(lease,"mobile-final",(390,844))
    response = runtime.set_visual_mode(identity, action="complete", review=final_review())
    assert response["visual_mode"]["status"] == "completed"
    assert not response["visual_mode"]["active"]
    assert record(lease)["stay_until_at"]
    with pytest.raises(runtime.RuntimePolicyError, match="驻场"):
        runtime.release_workspace(identity)
    assert runtime.workspace_status()["count"] == 1
    manifest = json.loads((runtime.VISUAL_DIR / identity / "manifest.json").read_text(encoding="utf-8"))
    assert manifest["visual_mode"]["iteration"] == 2


def test_visual_without_stay_can_complete_and_release(tmp_path):
    lease = acquire(tmp_path)
    identity = lease["lease_id"]
    runtime.set_visual_mode(identity, duration_seconds=3600)
    with pytest.raises(runtime.RuntimePolicyError):
        runtime.release_workspace(identity)
    with pytest.raises(runtime.RuntimePolicyError):
        runtime.set_visual_mode(identity, action="complete", review=final_review())
    observation(lease,"desktop-final",(1440,900))
    observation(lease,"mobile-final",(390,844))
    runtime.set_visual_mode(identity, action="complete", review=final_review())
    assert runtime.release_workspace(identity)["status"] == "released"


def test_tampered_visual_final_fails_closed(tmp_path):
    lease = acquire(tmp_path)
    runtime.set_visual_mode(lease["lease_id"])
    first = observation(lease,"desktop-final",(1440,900))
    observation(lease,"mobile-final",(390,844))
    Path(first["path"]).write_bytes(b"not the verified image")
    with pytest.raises(runtime.RuntimePolicyError):
        runtime.set_visual_mode(lease["lease_id"], action="complete", review=final_review())
    assert record(lease)["visual_mode"]["active"]


def test_recovery_contains_stay_and_visual(tmp_path):
    lease = acquire(tmp_path)
    identity = lease["lease_id"]
    runtime.set_visual_mode(identity, criteria=["Recover this criterion"])
    runtime.apply_stay_command(identity, "/stay 10m")
    state = runtime.workspace_status(Path(lease["workspace"]), include_private=True)
    summary = state["leases"][0]["visual_mode"]
    assert summary["active"] is True
    assert "criteria" not in summary and "observations" not in summary and "references" not in summary
    context = runtime.get_visual_context(identity)["visual_mode"]
    assert context["criteria"] == ["Recover this criterion"]
    task = state["recovery"]["runtime_task"]
    assert task["visual_mode"]["active"]
    assert "observations" not in task["visual_mode"]
    assert task["stay_until_at"]


def append_console_row(lease, identity, kind, text):
    runtime.CONSOLE_INPUT_FILE.parent.mkdir(parents=True, exist_ok=True)
    row = {"id":identity, "type":kind, "scope":"task", "taskId":lease["lease_id"],
           "threadId":lease["thread_id"], "timestampMs":1, "text":text}
    with runtime.CONSOLE_INPUT_FILE.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(row) + "\n")


def test_observation_is_not_console_and_cannot_clear_stay(tmp_path):
    lease = acquire(tmp_path)
    identity = lease["lease_id"]
    runtime.apply_stay_command(identity,"/stay 3h")
    before = record(lease)
    append_console_row(lease,"not-user","agent.observation","/stay off")
    with pytest.raises(runtime.RuntimePolicyError):
        runtime.apply_console_stay_command(identity,"not-user")
    assert record(lease) == before
    assert server._targeted_ui_rows(lease) == []
    append_console_row(lease,"real-console","ui.message","/stay off")
    runtime.apply_console_stay_command(identity,"real-console")
    assert "stay_until_at" not in record(lease)


def test_same_thread_cannot_use_sibling_lease_to_rescue(tmp_path):
    first = acquire(tmp_path,"first","same-thread")
    second = acquire(tmp_path,"second","same-thread")
    runtime.apply_stay_command(first["lease_id"],"/stay 3h")
    with pytest.raises(runtime.RuntimePolicyError):
        runtime.force_release_staying_workspace(second["lease_id"],first["lease_id"])
    assert runtime.workspace_status()["count"] == 2


def test_goal_mode_persists_and_recovery_carries_success_criteria(tmp_path):
    lease = acquire(tmp_path)
    identity = lease["lease_id"]
    runtime.set_visual_mode(identity, action="update", criteria=["视觉要求复用"])
    result = runtime.set_goal_mode(
        identity,
        enabled=True,
        outcome="完成一个可真实验收的长期目标",
        success_criteria=["核心测试通过", "真实流程通过"],
        reuse_visual_context=True,
        expected_updated_at="",
    )
    assert result["goal_mode"]["active"]
    assert result["goal_mode"]["outcome"] == "完成一个可真实验收的长期目标"
    assert result["goal_mode"]["success_criteria"] == ["核心测试通过", "真实流程通过"]
    state = runtime.workspace_status(Path(lease["workspace"]), include_private=True)
    assert state["leases"][0]["goal_mode"]["reuse_visual_context"] is True
    task = state["recovery"]["runtime_task"]
    assert task["goal_mode"]["active"] is True
    assert task["recovery_action"]["goal"]["success_criteria"] == ["核心测试通过", "真实流程通过"]
    assert "目标模式已开启" in task["recovery_instruction"]
    assert "criteria" not in task["visual_mode"] and "observations" not in task["visual_mode"]
    assert runtime.get_visual_context(identity)["visual_mode"]["criteria"] == ["视觉要求复用"]


def test_goal_mode_can_be_disabled_without_losing_configuration(tmp_path):
    lease = acquire(tmp_path)
    identity = lease["lease_id"]
    first = runtime.set_goal_mode(identity, enabled=True, outcome="保留这个目标",
                                  success_criteria=["标准 A"], reuse_visual_context=False)
    revision = first["goal_mode"]["updated_at"]
    second = runtime.set_goal_mode(identity, enabled=False, outcome="保留这个目标",
                                   success_criteria=["标准 A"], reuse_visual_context=False,
                                   expected_updated_at=revision)
    assert second["goal_mode"]["active"] is False
    assert second["goal_mode"]["status"] == "off"
    assert second["goal_mode"]["outcome"] == "保留这个目标"
    assert second["goal_mode"]["success_criteria"] == ["标准 A"]
    with pytest.raises(runtime.RuntimePolicyError, match="目标结果"):
        runtime.set_goal_mode(identity, enabled=True, outcome="", success_criteria=[])


def test_set_visual_mode_can_enable_goal_only_without_touching_visual(tmp_path):
    lease = acquire(tmp_path)
    identity = lease["lease_id"]
    before_visual = record(lease).get("visual_mode")
    result = runtime.set_visual_mode(
        identity,
        action="update",
        goal_enabled=True,
        goal_outcome="完成整个任务并验收",
        goal_success_criteria=["核心修改完成", "测试通过", "最终结果已验证"],
        goal_reuse_visual=True,
    )
    assert result["goal_mode"]["active"] is True
    assert result["goal_mode"]["outcome"] == "完成整个任务并验收"
    assert result["visual_mode"]["active"] is False
    assert record(lease).get("visual_mode") == before_visual


def test_set_visual_mode_can_enable_goal_and_visual_atomically(tmp_path):
    lease = acquire(tmp_path)
    identity = lease["lease_id"]
    result = runtime.set_visual_mode(
        identity,
        action="enable",
        duration_seconds=600,
        criteria=["桌面排版通过"],
        targets=[{"name": "desktop", "width": 1440, "height": 900}],
        goal_enabled=True,
        goal_outcome="完成视觉任务",
        goal_success_criteria=["桌面验收通过", "功能测试通过"],
        goal_reuse_visual=True,
    )
    stored = record(lease)
    assert result["visual_mode"]["active"] is True
    assert result["goal_mode"]["active"] is True
    assert stored["visual_mode"]["criteria"] == ["桌面排版通过"]
    assert stored["goal_mode"]["success_criteria"] == ["桌面验收通过", "功能测试通过"]


def test_console_explicit_goal_off_blocks_agent_auto_reenable(tmp_path):
    lease = acquire(tmp_path)
    identity = lease["lease_id"]
    enabled = runtime.set_goal_mode(identity, enabled=True, outcome="用户目标", success_criteria=["完成"])
    disabled = runtime.set_goal_mode(
        identity,
        enabled=False,
        outcome="用户目标",
        success_criteria=["完成"],
        expected_updated_at=enabled["goal_mode"]["updated_at"],
    )
    assert disabled["goal_mode"]["user_mode"] == "disabled"
    with pytest.raises(runtime.RuntimePolicyError) as error:
        runtime.set_visual_mode(
            identity,
            action="update",
            goal_enabled=True,
            goal_outcome="GPT 不应覆盖用户关闭",
            goal_success_criteria=["不应执行"],
        )
    assert error.value.error_type == "GOAL_USER_DISABLED"


def test_visual_completion_only_requires_declared_targets(tmp_path):
    lease = acquire(tmp_path)
    identity = lease["lease_id"]
    runtime.set_visual_mode(
        identity,
        duration_seconds=600,
        targets=[{"name": "desktop", "width": 1440, "height": 900}],
    )
    observation(lease, "desktop-only-final", (1440, 900))
    review = {
        "desktop": "PASS",
        "critical_differences": 0,
        "major_differences": 0,
        "minor_differences": 0,
        "reference_requirements": "satisfied",
        "final_observation_ids": {"desktop": "desktop-only-final"},
    }
    result = runtime.set_visual_mode(identity, action="complete", review=review)
    assert result["visual_mode"]["status"] == "completed"


def test_visual_target_defaults_to_free_screenshot_size(tmp_path):
    lease = acquire(tmp_path)
    identity = lease["lease_id"]
    result = runtime.set_visual_mode(
        identity,
        duration_seconds=600,
        targets=[{"name": "desktop", "width": 1144, "height": 894}],
    )
    assert result["visual_mode"]["targets"] == [
        {"name": "desktop", "viewport_mode": "free", "width": 1144, "height": 894}
    ]
    observation(lease, "desktop-free-final", (2048, 1120))
    review = {
        "desktop": "PASS",
        "critical_differences": 0,
        "major_differences": 0,
        "minor_differences": 0,
        "reference_requirements": "satisfied",
        "final_observation_ids": {"desktop": "desktop-free-final"},
    }
    completed = runtime.set_visual_mode(identity, action="complete", review=review)
    assert completed["visual_mode"]["status"] == "completed"


def test_visual_fixed_target_still_requires_exact_size(tmp_path):
    lease = acquire(tmp_path)
    identity = lease["lease_id"]
    runtime.set_visual_mode(
        identity,
        duration_seconds=600,
        targets=[{"name": "desktop", "viewport_mode": "fixed", "width": 1144, "height": 894}],
    )
    observation(lease, "desktop-fixed-final", (1600, 1000))
    review = {
        "desktop": "PASS",
        "critical_differences": 0,
        "major_differences": 0,
        "minor_differences": 0,
        "reference_requirements": "satisfied",
        "final_observation_ids": {"desktop": "desktop-fixed-final"},
    }
    with pytest.raises(runtime.RuntimePolicyError, match="尺寸"):
        runtime.set_visual_mode(identity, action="complete", review=review)
    observation(lease, "desktop-fixed-exact", (1144, 894))
    review["final_observation_ids"]["desktop"] = "desktop-fixed-exact"
    assert runtime.set_visual_mode(identity, action="complete", review=review)["visual_mode"]["status"] == "completed"


def test_visual_responsive_target_requires_two_real_browser_widths(tmp_path):
    lease = acquire(tmp_path)
    identity = lease["lease_id"]
    runtime.set_visual_mode(
        identity,
        duration_seconds=600,
        targets=[{"name": "desktop", "viewport_mode": "responsive", "width": 1144, "height": 894}],
    )
    observation(
        lease, "responsive-a", (1144, 894), capture_type="browser",
        viewport_width=1144, viewport_height=894, browser_url="http://example.test/",
    )
    review = {
        "desktop": "PASS",
        "responsive_layout": {"desktop": "PASS"},
        "responsive_observation_ids": {"desktop": ["responsive-a"]},
        "critical_differences": 0,
        "major_differences": 0,
        "minor_differences": 0,
        "reference_requirements": "satisfied",
        "final_observation_ids": {"desktop": "responsive-a"},
    }
    with pytest.raises(runtime.RuntimePolicyError, match="两张"):
        runtime.set_visual_mode(identity, action="complete", review=review)
    observation(
        lease, "responsive-b", (1600, 1000), capture_type="browser",
        viewport_width=1600, viewport_height=1000, browser_url="http://example.test/",
    )
    review["responsive_observation_ids"]["desktop"] = ["responsive-a", "responsive-b"]
    review["final_observation_ids"]["desktop"] = "responsive-b"
    assert runtime.set_visual_mode(identity, action="complete", review=review)["visual_mode"]["status"] == "completed"


def test_visual_target_rejects_partial_dimensions_and_fixed_without_size(tmp_path):
    lease = acquire(tmp_path)
    identity = lease["lease_id"]
    with pytest.raises(runtime.RuntimePolicyError, match="同时"):
        runtime.set_visual_mode(identity, targets=[{"name": "desktop", "width": 1144}])
    with pytest.raises(runtime.RuntimePolicyError, match="fixed"):
        runtime.set_visual_mode(identity, targets=[{"name": "desktop", "viewport_mode": "fixed"}])


def test_workspace_status_visual_is_slim_but_context_is_full(tmp_path):
    lease = acquire(tmp_path)
    identity = lease["lease_id"]
    runtime.set_visual_mode(identity, criteria=["保留完整视觉要求"])
    observation(lease, "status-observation", (1440, 900))
    runtime.set_visual_mode(identity, action="review", review={"status": "in_progress"})
    state = runtime.workspace_status(Path(lease["workspace"]), include_private=True)
    summary = state["leases"][0]["visual_mode"]
    assert summary["latest_observation_id"] == "status-observation"
    assert summary["latest_review"] == {"status": "in_progress"}
    assert summary["targets"] == [{"name": "desktop", "viewport_mode": "free"}]
    assert summary["requirements_revision"] >= 1
    assert "observations" not in summary
    assert "criteria" not in summary
    assert "references" not in summary
    full = runtime.get_visual_context(identity)["visual_mode"]
    assert full["criteria"] == ["保留完整视觉要求"]
    assert full["observations"][-1]["id"] == "status-observation"


def test_disconnect_alert_claim_is_stale_and_deduplicated_without_stay(tmp_path):
    lease = acquire(tmp_path)
    identity = lease["lease_id"]
    stale, probe = arm_confirmed_disconnect(lease, minutes_stale=6)
    probe_id = probe["probe"]["id"]

    first = runtime.claim_disconnect_alert(identity, stale_seconds=120)
    assert first["status"] == "claimed"
    assert first["heartbeat_at"] == stale
    assert first["probe_id"] == probe_id
    assert runtime.claim_disconnect_alert(identity, stale_seconds=120)["status"] == "in_progress"
    assert runtime.complete_disconnect_alert(
        identity, stale, success=True, probe_id=probe_id
    )["status"] == "sent"
    assert runtime.claim_disconnect_alert(identity, stale_seconds=120)["status"] == "already_notified"

    runtime.clear_disconnect_probe(identity, probe_id=probe_id, reason="new activity")
    value = record(lease)
    value["execution_checkpoint_updated_at"] = runtime.utc_iso()
    runtime._write_json(runtime._lease_path(identity), value)
    assert runtime.claim_disconnect_alert(identity, stale_seconds=120)["status"] == "disconnect_not_confirmed"

def test_visual_observation_loop_has_no_hard_count_limit(tmp_path):
    lease = acquire(tmp_path)
    identity = lease["lease_id"]
    directory = runtime.VISUAL_DIR / identity / "observations"
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / "shared.png"
    Image.new("RGB", (8, 8), "white").save(path)
    digest = hashlib.sha256(path.read_bytes()).hexdigest()

    for index in range(140):
        runtime.record_visual_observation(identity, {
            "id": f"obs-loop-{index:03d}",
            "kind": "image",
            "path": str(path),
            "width": 8,
            "height": 8,
            "mime": "image/png",
            "sha256": digest,
            "inspected_at": runtime.utc_iso(),
            "label": f"loop {index}",
        })

    visual = record(lease)["visual_mode"]
    assert visual["iteration"] == 140
    assert visual["observation_count"] == 140
    assert visual["retained_observation_count"] == runtime.VISUAL_OBSERVATION_METADATA_WINDOW
    assert len(visual["observations"]) == runtime.VISUAL_OBSERVATION_METADATA_WINDOW
    assert visual["observations"][0]["id"] == "obs-loop-012"
    assert visual["observations"][-1]["id"] == "obs-loop-139"
    assert visual["latest_observation_id"] == "obs-loop-139"


def test_disconnect_notifier_uses_existing_gmail_bridge_and_failed_send_is_retryable(tmp_path, monkeypatch):
    import importlib.util
    monkeypatch.setenv("GPT_PC_DISCONNECT_ALERT_EMAIL", "user@example.invalid")

    lease = acquire(tmp_path)
    identity = lease["lease_id"]
    stale, first_probe = arm_confirmed_disconnect(lease, minutes_stale=6)

    notifier_path = Path(__file__).resolve().parents[2] / "scripts" / "gpt-disconnect-notifier.py"
    spec = importlib.util.spec_from_file_location("gpt_pc_disconnect_notifier_test", notifier_path)
    assert spec and spec.loader
    notifier = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(notifier)

    desktop = tmp_path / "desktop-ops"
    desktop.mkdir()
    monkeypatch.setattr(runtime, "DESKTOP_OPS_WORKSPACE", desktop)
    monkeypatch.setattr(notifier.runtime, "DESKTOP_OPS_WORKSPACE", desktop)
    monkeypatch.setattr(
        notifier, "_ultra_root_gate",
        lambda _lease_id: {"eligible": True, "status": "eligible", "team_id": "uteam-test"},
    )
    sent_payloads = []

    def fake_bridge(payload, timeout_seconds=60):
        sent_payloads.append(dict(payload))
        return {"status": "sent", "message_id": "fake-message"}

    monkeypatch.setattr(notifier, "_bridge", fake_bridge)
    result = notifier.notify(identity, stale_seconds=120)
    assert result["status"] == "sent"
    assert result["message_id"] == "fake-message"
    assert sent_payloads[0]["action"] == "send"
    assert sent_payloads[0]["interactive_auth"] is False
    assert sent_payloads[0]["to"] == "user@example.invalid"
    assert identity in sent_payloads[0]["body"]
    assert "Ultra Root" in sent_payloads[0]["body"]
    assert "5 分钟 Team 恢复窗口" in sent_payloads[0]["body"]
    assert runtime.claim_disconnect_alert(identity, stale_seconds=120)["status"] == "already_notified"
    assert record(lease)["disconnect_alert_sent_probe_id"] == first_probe["probe"]["id"]
    assert runtime.workspace_status(desktop)["count"] == 0

    stale_again, second_probe = arm_confirmed_disconnect(lease, minutes_stale=7)

    def failed_bridge(payload, timeout_seconds=60):
        raise RuntimeError("simulated Gmail failure")

    monkeypatch.setattr(notifier, "_bridge", failed_bridge)
    with pytest.raises(RuntimeError, match="simulated Gmail failure"):
        notifier.notify(identity, stale_seconds=120)
    retry = runtime.claim_disconnect_alert(identity, stale_seconds=120)
    assert retry["status"] == "retry_backoff"
    assert retry["retry_after_at"]
    stored_retry = record(lease)
    assert stored_retry["disconnect_alert_retry_after_at"] == retry["retry_after_at"]
    assert stored_retry.get("disconnect_alert_pending_probe_id") is None
    assert stored_retry["disconnect_probe_id"] == second_probe["probe"]["id"]

def test_disconnect_notifier_cancels_if_probe_was_explicitly_acked_before_send(tmp_path, monkeypatch):
    import importlib.util
    monkeypatch.setenv("GPT_PC_DISCONNECT_ALERT_EMAIL", "user@example.invalid")

    lease = acquire(tmp_path)
    identity = lease["lease_id"]
    _stale, probe = arm_confirmed_disconnect(lease, minutes_stale=6)

    notifier_path = Path(__file__).resolve().parents[2] / "scripts" / "gpt-disconnect-notifier.py"
    spec = importlib.util.spec_from_file_location("gpt_pc_disconnect_notifier_ack_test", notifier_path)
    assert spec and spec.loader
    notifier = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(notifier)
    desktop = tmp_path / "desktop-ops-ack"
    desktop.mkdir()
    monkeypatch.setattr(runtime, "DESKTOP_OPS_WORKSPACE", desktop)
    monkeypatch.setattr(notifier.runtime, "DESKTOP_OPS_WORKSPACE", desktop)
    monkeypatch.setattr(
        notifier, "_ultra_root_gate",
        lambda _lease_id: {"eligible": True, "status": "eligible", "team_id": "uteam-test"},
    )
    original_acquire = notifier.runtime.acquire_workspace

    def acquire_then_ack(*args, **kwargs):
        result = original_acquire(*args, **kwargs)
        runtime.ack_disconnect_probe(identity, probe["probe"]["id"])
        return result

    sent_payloads = []
    monkeypatch.setattr(notifier.runtime, "acquire_workspace", acquire_then_ack)
    monkeypatch.setattr(notifier, "_bridge", lambda payload, timeout_seconds=60: sent_payloads.append(payload))
    result = notifier.notify(identity, stale_seconds=120)
    assert result["status"] == "cancelled_explicit_response"
    assert sent_payloads == []
    assert runtime.workspace_status(desktop)["count"] == 0


def test_disconnect_notifier_rejects_non_ultra_target_before_gmail(tmp_path, monkeypatch):
    import importlib.util
    monkeypatch.setenv("GPT_PC_DISCONNECT_ALERT_EMAIL", "user@example.invalid")
    import sqlite3
    from types import SimpleNamespace

    lease = acquire(tmp_path)
    identity = lease["lease_id"]
    arm_confirmed_disconnect(lease, minutes_stale=6)

    notifier_path = Path(__file__).resolve().parents[2] / "scripts" / "gpt-disconnect-notifier.py"
    spec = importlib.util.spec_from_file_location("gpt_pc_disconnect_notifier_gate_test", notifier_path)
    assert spec and spec.loader
    notifier = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(notifier)

    database = tmp_path / "agents.sqlite3"
    with sqlite3.connect(database) as connection:
        connection.executescript(
            """
            CREATE TABLE ultra_teams(
                team_id TEXT PRIMARY KEY,
                root_lease_id TEXT NOT NULL,
                state TEXT NOT NULL
            );
            CREATE TABLE ultra_root_rescues(
                rescue_id TEXT PRIMARY KEY,
                team_id TEXT NOT NULL,
                state TEXT NOT NULL,
                created_at REAL NOT NULL
            );
            """
        )
    monkeypatch.setattr(
        notifier,
        "load_machine_config",
        lambda runtime_root=None: SimpleNamespace(
            ultra_collaboration=True,
            database_path=database,
        ),
    )
    sent_payloads = []
    monkeypatch.setattr(
        notifier,
        "_bridge",
        lambda payload, timeout_seconds=60: sent_payloads.append(dict(payload)),
    )

    result = notifier.notify(identity, stale_seconds=120)
    assert result["eligible"] is False
    assert result["status"] == "not_active_ultra_root"
    assert sent_payloads == []


def test_set_visual_mode_mcp_tool_carries_goal_context_without_new_tool(tmp_path):
    lease = acquire(tmp_path)
    identity = lease["lease_id"]
    result = call_tool("set_visual_mode", {
        "workspace_lease_id": identity,
        "action": "update",
        "goal_enabled": True,
        "goal_outcome": "通过现有 set_visual_mode 完成整个任务",
        "goal_success_criteria": ["改动完成", "测试通过"],
        "goal_reuse_visual": True,
    })
    assert result["goal_mode"]["active"] is True
    assert result["goal_mode"]["outcome"] == "通过现有 set_visual_mode 完成整个任务"
    assert result["goal_mode"]["success_criteria"] == ["改动完成", "测试通过"]
    assert result["visual_mode"]["active"] is False
    assert record(lease)["goal_mode"]["active"] is True


def test_disconnect_alert_stale_pending_claim_can_be_reclaimed(tmp_path):
    lease = acquire(tmp_path)
    identity = lease["lease_id"]
    heartbeat, probe = arm_confirmed_disconnect(lease, minutes_stale=6)
    probe_id = probe["probe"]["id"]
    value = record(lease)
    value["disconnect_alert_pending_probe_id"] = probe_id
    value["disconnect_alert_claimed_at"] = runtime.utc_iso(runtime.utc_now() - timedelta(minutes=5))
    runtime._write_json(runtime._lease_path(identity), value)

    claimed = runtime.claim_disconnect_alert(identity, stale_seconds=120)
    assert claimed["status"] == "claimed"
    assert claimed["heartbeat_at"] == heartbeat
    assert claimed["probe_id"] == probe_id
    refreshed = record(lease)
    assert refreshed["disconnect_alert_pending_probe_id"] == probe_id
    assert refreshed["disconnect_alert_claimed_at"] != value["disconnect_alert_claimed_at"]
    runtime.complete_disconnect_alert(
        identity, heartbeat, success=False, probe_id=probe_id
    )

def test_disconnect_probe_is_one_per_activity_episode_and_poll_does_not_ack_or_enter_user_control(tmp_path):
    lease = acquire(tmp_path)
    identity = lease["lease_id"]
    first_activity = runtime.utc_iso(runtime.utc_now() - timedelta(minutes=4))
    first = runtime.ensure_disconnect_probe(identity, activity_at=first_activity, response_seconds=120)
    duplicate = runtime.ensure_disconnect_probe(identity, activity_at=first_activity, response_seconds=120)
    assert first["status"] == "created"
    assert duplicate["status"] == "existing"
    assert duplicate["probe"]["id"] == first["probe"]["id"]

    polled = call_tool("poll_ui_messages", {"workspace_lease_id": identity})
    assert polled["messages"] == []
    assert polled["count"] == 0
    assert polled["liveness_probe"]["id"] == first["probe"]["id"]
    assert polled["liveness_probe"]["acknowledged"] is False
    assert polled["liveness_probe"]["reply_required"] is True
    assert polled["control_ack_required"] is False
    assert polled["control_resume_required"] is False
    stored = record(lease)
    assert stored.get("disconnect_probe_acked_at") is None

    replied = call_tool(
        "report_progress",
        {"workspace_lease_id": identity, "phase": "progress", "text": "仍在工作，继续。"},
    )
    assert replied["liveness_probe"]["acknowledged"] is True
    stored = record(lease)
    assert stored["disconnect_probe_acked_at"]

    second_activity = runtime.utc_iso(runtime.utc_now())
    second = runtime.ensure_disconnect_probe(identity, activity_at=second_activity, response_seconds=120)
    assert second["status"] == "created"
    assert second["probe"]["id"] != first["probe"]["id"]
    assert second["probe"]["acked_at"] is None


def test_liveness_probe_does_not_enter_user_message_queue_but_requires_explicit_progress_reply(tmp_path):
    lease = acquire(tmp_path)
    identity = lease["lease_id"]
    runtime.ensure_disconnect_probe(
        identity,
        activity_at=runtime.utc_iso(runtime.utc_now() - timedelta(minutes=4)),
        response_seconds=120,
    )
    result = server._poll_ui_messages_once(identity)
    assert result["messages"] == []
    assert result["count"] == 0
    assert result["liveness_probe"]["acknowledged"] is False
    assert result["liveness_probe"]["reply_required"] is True
    assert "report_progress" in result["liveness_probe"]["text"]
    private = runtime.workspace_status(Path(lease["workspace"]), include_private=True)["leases"][0]
    assert private["control_ack_pending_id"] is None
    assert private["control_resume_pending_id"] is None

    replied = call_tool(
        "report_progress",
        {"workspace_lease_id": identity, "phase": "progress", "text": "存活，继续当前任务。"},
    )
    assert replied["liveness_probe"]["acknowledged"] is True
    assert record(lease)["disconnect_probe_acked_at"]


def _single_target_review(observation_id: str) -> dict:
    return {
        "desktop": "PASS",
        "critical_differences": 0,
        "major_differences": 0,
        "minor_differences": 0,
        "reference_requirements": "satisfied",
        "final_observation_ids": {"desktop": observation_id},
    }


def test_interactive_depth_requires_current_required_journey(tmp_path):
    lease = acquire(tmp_path)
    identity = lease["lease_id"]
    runtime.set_visual_mode(
        identity,
        duration_seconds=600,
        depth="interactive",
        journeys=[{"id": "open-detail", "name": "打开详情"}],
        targets=[{"name": "desktop", "viewport_mode": "free"}],
    )
    observation(lease, "interaction-final-missing", (1200, 800))
    with pytest.raises(runtime.RuntimePolicyError) as missing:
        runtime.set_visual_mode(identity, action="complete", review=_single_target_review("interaction-final-missing"))
    assert missing.value.error_type == "VISUAL_INTERACTION_REQUIRED"

    observation(
        lease, "interaction-final-pass", (1200, 800),
        journey_id="open-detail", interaction_status="PASS",
        frontend_health={"status": "NOT_ENFORCED"},
    )
    completed = runtime.set_visual_mode(
        identity, action="complete", review=_single_target_review("interaction-final-pass")
    )
    assert completed["visual_mode"]["status"] == "completed"


def test_frontend_depth_requires_clean_health_for_each_required_journey(tmp_path):
    lease = acquire(tmp_path)
    identity = lease["lease_id"]
    runtime.set_visual_mode(
        identity,
        duration_seconds=600,
        depth="frontend",
        journeys=[{"id": "search", "name": "搜索产业"}],
        targets=[{"name": "desktop", "viewport_mode": "free"}],
    )
    observation(
        lease, "frontend-health-fail", (1200, 800), journey_id="search", interaction_status="PASS",
        frontend_health={"status": "FAIL", "console_error_count": 1},
    )
    with pytest.raises(runtime.RuntimePolicyError) as unhealthy:
        runtime.set_visual_mode(identity, action="complete", review=_single_target_review("frontend-health-fail"))
    assert unhealthy.value.error_type == "VISUAL_FRONTEND_HEALTH_REQUIRED"

    observation(
        lease, "frontend-health-pass", (1200, 800), journey_id="search", interaction_status="PASS",
        frontend_health={"status": "PASS", "console_error_count": 0},
    )
    assert runtime.set_visual_mode(
        identity, action="complete", review=_single_target_review("frontend-health-pass")
    )["visual_mode"]["status"] == "completed"


def test_workspace_mutation_invalidates_completed_frontend_evidence(tmp_path):
    lease = acquire(tmp_path)
    identity = lease["lease_id"]
    runtime.set_visual_mode(
        identity,
        duration_seconds=600,
        depth="frontend",
        journeys=[{"id": "nav", "name": "导航"}],
        targets=[{"name": "desktop", "viewport_mode": "free"}],
    )
    observation(
        lease, "before-code-change", (1200, 800), journey_id="nav", interaction_status="PASS",
        frontend_health={"status": "PASS"},
    )
    runtime.set_visual_mode(identity, action="complete", review=_single_target_review("before-code-change"))
    invalidated = runtime.bump_workspace_validation_revision(identity)
    assert invalidated["visual_mode"]["status"] == "needs_review"
    assert invalidated["workspace_validation_revision"] == 1
    with pytest.raises(runtime.RuntimePolicyError):
        runtime.set_visual_mode(identity, action="complete", review=_single_target_review("before-code-change"))
    with pytest.raises(runtime.RuntimePolicyError, match="视觉"):
        runtime.release_workspace(identity)


def test_visual_depth_remains_backward_compatible_with_no_journeys(tmp_path):
    lease = acquire(tmp_path)
    identity = lease["lease_id"]
    runtime.set_visual_mode(identity, duration_seconds=600, depth="visual",
                            targets=[{"name": "desktop", "viewport_mode": "free"}])
    observation(lease, "visual-only-final", (1024, 768))
    result = runtime.set_visual_mode(identity, action="complete", review=_single_target_review("visual-only-final"))
    assert result["visual_mode"]["status"] == "completed"


def test_real_mcp_workspace_write_bumps_frontend_validation_revision(tmp_path):
    lease = acquire(tmp_path)
    identity = lease["lease_id"]
    target = Path(lease["workspace"]) / "frontend.tsx"
    assert int(record(lease).get("workspace_validation_revision") or 0) == 0

    call_tool("write_text_file", {
        "path": str(target),
        "content": "export const ready = true;\n",
        "workspace_lease_id": identity,
    })
    assert int(record(lease).get("workspace_validation_revision") or 0) == 1

    call_tool("read_text_file", {"path": str(target)})
    assert int(record(lease).get("workspace_validation_revision") or 0) == 1


def test_goal_mode_is_a_hard_done_gate_until_explicit_acceptance(tmp_path):
    lease = acquire(tmp_path, name="goal-hard-gate")
    identity = lease["lease_id"]
    runtime.set_visual_mode(
        identity,
        action="update",
        goal_enabled=True,
        goal_outcome="完成目标并逐条验收",
        goal_success_criteria=["核心修改完成", "回归测试通过"],
        goal_reuse_visual=False,
    )
    with pytest.raises(runtime.RuntimePolicyError) as error:
        runtime.release_workspace(identity)
    assert error.value.error_type == "GOAL_ACTIVE"
    with pytest.raises(runtime.RuntimePolicyError) as error:
        runtime.set_visual_mode(identity, action="update", goal_enabled=False)
    assert error.value.error_type == "GOAL_SELF_DISABLE_FORBIDDEN"

    review = {
        "goal": {
            "status": "PASS",
            "summary": "代码与回归均已核验",
            "criteria": [
                {"criterion": "核心修改完成", "status": "PASS", "evidence": "diff 已核对"},
                {"criterion": "回归测试通过", "status": "PASS", "evidence": "pytest 全量通过"},
            ],
        }
    }
    result = runtime.set_visual_mode(identity, action="complete", review=review)
    assert result["goal_mode"]["active"] is False
    assert result["goal_mode"]["status"] == "completed"
    assert result["goal_mode"]["latest_review"]["status"] == "PASS"
    assert runtime.release_workspace(identity)["status"] == "released"


def test_goal_acceptance_requires_exact_criteria_and_is_invalidated_by_mutation(tmp_path):
    lease = acquire(tmp_path, name="goal-stale")
    identity = lease["lease_id"]
    runtime.set_visual_mode(
        identity,
        action="update",
        goal_enabled=True,
        goal_outcome="完成并验证",
        goal_success_criteria=["标准 A", "标准 B"],
    )
    bad = {
        "goal": {
            "status": "PASS",
            "summary": "不完整验收",
            "criteria": [
                {"criterion": "标准 A", "status": "PASS", "evidence": "A ok"},
                {"criterion": "写错的标准", "status": "PASS", "evidence": "B?"},
            ],
        }
    }
    with pytest.raises(runtime.RuntimePolicyError) as error:
        runtime.set_visual_mode(identity, action="complete", review=bad)
    assert error.value.error_type == "GOAL_VERIFICATION_REQUIRED"

    good = {
        "goal": {
            "status": "PASS",
            "summary": "两条标准都已核验",
            "criteria": [
                {"criterion": "标准 A", "status": "PASS", "evidence": "A ok"},
                {"criterion": "标准 B", "status": "PASS", "evidence": "B ok"},
            ],
        }
    }
    runtime.set_visual_mode(identity, action="complete", review=good)
    state = runtime.bump_workspace_validation_revision(identity)
    assert state["goal_mode"]["active"] is True
    assert state["goal_mode"]["status"] == "needs_review"
    with pytest.raises(runtime.RuntimePolicyError) as error:
        runtime.release_workspace(identity)
    assert error.value.error_type == "GOAL_ACTIVE"


def test_goal_and_visual_can_be_accepted_in_separate_steps(tmp_path):
    lease = acquire(tmp_path, name="goal-visual-separate")
    identity = lease["lease_id"]
    runtime.set_visual_mode(
        identity,
        action="enable",
        duration_seconds=600,
        targets=[{"name": "desktop", "width": 1440, "height": 900}],
        goal_enabled=True,
        goal_outcome="功能和视觉都完成",
        goal_success_criteria=["功能测试通过", "视觉验收通过"],
    )
    observation(lease, "desktop-final-goal", (1440, 900))
    visual_review = {
        "desktop": "PASS",
        "critical_differences": 0,
        "major_differences": 0,
        "minor_differences": 0,
        "reference_requirements": "satisfied",
        "final_observation_ids": {"desktop": "desktop-final-goal"},
    }
    first = runtime.set_visual_mode(identity, action="complete", review=visual_review)
    assert first["visual_mode"]["status"] == "completed"
    assert first["goal_mode"]["active"] is True
    with pytest.raises(runtime.RuntimePolicyError) as error:
        runtime.release_workspace(identity)
    assert error.value.error_type == "GOAL_ACTIVE"

    goal_review = {
        "goal": {
            "status": "PASS",
            "summary": "功能与视觉证据均已完成",
            "criteria": [
                {"criterion": "功能测试通过", "status": "PASS", "evidence": "相关自动测试通过"},
                {"criterion": "视觉验收通过", "status": "PASS", "evidence": "desktop-final-goal 已读取并 PASS"},
            ],
        }
    }
    second = runtime.set_visual_mode(identity, action="complete", review=goal_review)
    assert second["goal_mode"]["status"] == "completed"
    assert runtime.release_workspace(identity)["status"] == "released"


def test_report_progress_done_is_blocked_by_goal_until_goal_review_passes(tmp_path):
    lease = acquire(tmp_path, name="goal-done-wrapper")
    identity = lease["lease_id"]
    runtime.set_visual_mode(
        identity,
        action="update",
        goal_enabled=True,
        goal_outcome="完成后才能 done",
        goal_success_criteria=["验证完成握手门禁"],
    )

    async def raw_done():
        async with Client(server.mcp) as client:
            return await client.call_tool("report_progress", {
                "workspace_lease_id": identity,
                "phase": "done",
                "text": "不应提前完成",
            })

    blocked = asyncio.run(raw_done())
    assert blocked.is_error is True
    assert '"error_type": "GOAL_ACTIVE"' in blocked.content[0].text
    assert runtime.active_lease_record(identity) is not None

    review = {
        "goal": {
            "status": "PASS",
            "summary": "完成握手门禁已验证",
            "criteria": [
                {"criterion": "验证完成握手门禁", "status": "PASS", "evidence": "提前 done 被 Runtime 拒绝"},
            ],
        }
    }
    runtime.set_visual_mode(identity, action="complete", review=review)
    success = asyncio.run(raw_done())
    assert success.is_error is False
    assert success.structured_content["lease_released"] is True
    assert runtime.active_lease_record(identity) is None


def test_goal_revision_requirements_corrections_and_noop(tmp_path):
    lease = acquire(tmp_path)
    identity = lease["lease_id"]
    first = runtime.set_goal_mode(
        identity, enabled=True, outcome="完成动态目标", success_criteria=["验收通过"],
        requirements=["持续保护已有工作", "后续修正优先"], change_note="用户新增持续约束",
        reuse_visual_context=True,
    )
    goal = first["goal_mode"]
    assert first["changed"] is True
    assert goal["revision"] == 1
    assert goal["requirements"] == ["持续保护已有工作", "后续修正优先"]
    assert goal["corrections"][-1]["source"] == "user"
    assert goal["corrections"][-1]["note"] == "用户新增持续约束"
    same = runtime.set_goal_mode(
        identity, enabled=True, outcome="完成动态目标", success_criteria=["验收通过"],
        requirements=["持续保护已有工作", "后续修正优先"], change_note="不会重复",
        reuse_visual_context=True, expected_updated_at=goal["updated_at"],
    )
    assert same["changed"] is False and same["goal_mode"]["revision"] == 1
    task = runtime.workspace_status(Path(lease["workspace"]), include_private=True)["recovery"]["runtime_task"]
    assert task["goal_mode"]["requirements"] == ["持续保护已有工作", "后续修正优先"]
    assert task["goal_mode"]["revision"] == 1
    assert "revision" in task["recovery_instruction"]


def test_ultra_user_and_agent_authority(tmp_path):
    lease = acquire(tmp_path)
    identity = lease["lease_id"]
    agent_on = runtime.set_ultra_mode(identity, enabled=True, actor="agent")
    assert agent_on["changed"] is True and agent_on["ultra_mode"]["active"] is True
    with pytest.raises(runtime.RuntimePolicyError) as err:
        runtime.set_ultra_mode(identity, enabled=False, actor="agent")
    assert err.value.error_type == "ULTRA_SELF_DISABLE_FORBIDDEN"
    user_off = runtime.set_ultra_mode(identity, enabled=False, user_explicit=True, actor="user",
                                      expected_updated_at=agent_on["ultra_mode"]["updated_at"])
    assert user_off["ultra_mode"]["user_mode"] == "disabled"
    with pytest.raises(runtime.RuntimePolicyError) as err:
        runtime.set_ultra_mode(identity, enabled=True, actor="child")
    assert err.value.error_type == "ULTRA_USER_DISABLED"
    user_on = runtime.set_ultra_mode(identity, enabled=True, user_explicit=True, actor="user",
                                     expected_updated_at=user_off["ultra_mode"]["updated_at"])
    assert user_on["ultra_mode"]["active"] is True and user_on["ultra_mode"]["user_mode"] == "enabled"
    task = runtime.workspace_status(Path(lease["workspace"]), include_private=True)["recovery"]["runtime_task"]
    assert task["ultra_mode"]["active"] is True
    assert task["recovery_action"]["ultra"]["active"] is True
