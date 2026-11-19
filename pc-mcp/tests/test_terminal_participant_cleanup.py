from __future__ import annotations

from types import SimpleNamespace

import pytest

import runtime
from chatgpt_web_controller import ChatGPTWebController
from test_server import isolate_runtime


def test_agent_scoped_cleanup_releases_only_matching_participant(monkeypatch, tmp_path):
    isolate_runtime(monkeypatch, tmp_path)
    workspace = tmp_path / "shared"
    workspace.mkdir()
    root = runtime.acquire_workspace(
        workspace,
        mode="execute_exclusive",
        owner_label="root",
        thread_id="thread-root-terminal-cleanup",
    )
    closed = runtime.attach_workspace_participant(
        root["lease_id"], logical_agent_id="ag_closed_cleanup", owner_label="closed child"
    )
    running = runtime.attach_workspace_participant(
        root["lease_id"], logical_agent_id="ag_running_cleanup", owner_label="running child"
    )

    with pytest.raises(runtime.RuntimePolicyError) as blocked:
        runtime.release_workspace(root["lease_id"])
    assert blocked.value.error_type == "SHARED_LEASE_PARTICIPANTS_ACTIVE"

    result = runtime.release_workspace_participants_for_agent("ag_closed_cleanup")
    assert result["status"] == "released"
    assert result["released_participant_lease_ids"] == [closed["lease_id"]]
    assert runtime.active_lease_record(closed["lease_id"]) is None
    assert runtime.active_lease_record(running["lease_id"])["lease_role"] == "participant"

    runtime.release_workspace(running["lease_id"])
    assert runtime.release_workspace(root["lease_id"])["status"] == "released"


def test_forced_cleanup_refuses_participant_with_running_job(monkeypatch, tmp_path):
    isolate_runtime(monkeypatch, tmp_path)
    workspace = tmp_path / "busy"
    workspace.mkdir()
    root = runtime.acquire_workspace(
        workspace,
        mode="execute_exclusive",
        owner_label="root",
        thread_id="thread-root-busy-cleanup",
    )
    participant = runtime.attach_workspace_participant(
        root["lease_id"], logical_agent_id="ag_busy_cleanup", owner_label="busy child"
    )
    monkeypatch.setattr(
        runtime,
        "_running_job_records_locked",
        lambda: [{"lease_id": participant["lease_id"], "job_id": "job-busy"}],
    )

    with pytest.raises(runtime.RuntimePolicyError) as blocked:
        runtime.release_workspace_participants_for_agent("ag_busy_cleanup")
    assert blocked.value.error_type == "PARTICIPANT_JOBS_ACTIVE"
    assert runtime.active_lease_record(participant["lease_id"])["lease_role"] == "participant"


def test_controller_cleans_only_forced_nonresumable_terminal_states():
    calls: list[str] = []
    manager = SimpleNamespace(
        store=SimpleNamespace(),
        config=SimpleNamespace(),
        terminal_participant_releaser=lambda agent_id: calls.append(agent_id) or {
            "status": "released",
            "logical_agent_id": agent_id,
        },
    )
    controller = ChatGPTWebController(manager, session=None, loop=None)

    closed = {"logical_agent_id": "ag_closed", "state": "CLOSED"}
    blocked = {"logical_agent_id": "ag_blocked", "state": "BLOCKED"}
    done = {"logical_agent_id": "ag_done", "state": "DONE"}
    recoverable = {"logical_agent_id": "ag_recoverable", "state": "DEAD_RECOVERABLE"}
    interrupted = {"logical_agent_id": "ag_interrupted", "state": "IDLE"}

    assert controller._cleanup_forced_terminal_participant(closed)["status"] == "released"
    assert controller._cleanup_forced_terminal_participant(blocked)["status"] == "released"
    assert controller._cleanup_forced_terminal_participant(done) is None
    assert controller._cleanup_forced_terminal_participant(recoverable) is None
    assert controller._cleanup_forced_terminal_participant(interrupted) is None
    assert calls == ["ag_closed", "ag_blocked"]


def test_terminal_reconcile_retries_job_blocked_cleanup_without_touching_done():
    class BusyParticipant(Exception):
        error_type = "PARTICIPANT_JOBS_ACTIVE"

    closed = {"logical_agent_id": "ag_retry_closed", "state": "CLOSED"}
    done = {"logical_agent_id": "ag_retry_done", "state": "DONE"}

    class Store:
        def agents(self, *, state=None, limit=100, offset=0, **_kwargs):
            if offset:
                return []
            if state == "CLOSED":
                return [dict(closed)]
            return []

        def get(self, agent_id):
            if agent_id == closed["logical_agent_id"]:
                return dict(closed)
            if agent_id == done["logical_agent_id"]:
                return dict(done)
            raise AssertionError(agent_id)

    calls: list[str] = []

    def releaser(agent_id):
        calls.append(agent_id)
        if len(calls) <= 2:
            raise BusyParticipant("job still active")
        return {"status": "released", "logical_agent_id": agent_id}

    manager = SimpleNamespace(
        store=Store(),
        config=SimpleNamespace(),
        terminal_participant_releaser=releaser,
    )
    controller = ChatGPTWebController(manager, session=None, loop=None)

    first = controller._reconcile_terminal_participants()
    assert controller.terminal_participant_bootstrap_reconciled is True
    assert controller.pending_terminal_participant_cleanup == {closed["logical_agent_id"]}
    assert [item["status"] for item in first] == ["deferred", "deferred"]

    second = controller._reconcile_terminal_participants()
    assert second == [{"status": "released", "logical_agent_id": closed["logical_agent_id"]}]
    assert controller.pending_terminal_participant_cleanup == set()
    assert done["logical_agent_id"] not in calls
