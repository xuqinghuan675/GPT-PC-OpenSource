from dataclasses import replace
import pytest

import ultra_web_transport as uwt
from chatgpt_agent_store import AgentError
from machine_config import MachineConfig


def test_payload_uses_current_machine_plugin_app_id(monkeypatch, tmp_path):
    first = "asdk_app_" + "a" * 32
    current = "asdk_app_" + "b" * 32
    config = MachineConfig.from_mapping({"chatgpt": {"plugin_app_id": first}}, runtime_root=str(tmp_path))
    monkeypatch.setattr(uwt, "load_machine_config", lambda runtime_root=None: replace(config, plugin_app_id=current))
    transport = uwt.BrowserWebTransport(config)
    payload = transport._payload_with_plugin({"system_hints": ["plugin:asdk_app_" + "c" * 32, "keep-me"]})
    assert payload["system_hints"] == ["keep-me", "plugin:" + current]


def test_payload_fails_closed_without_current_plugin_app_id(monkeypatch, tmp_path):
    config = MachineConfig.from_mapping({}, runtime_root=str(tmp_path))
    monkeypatch.setattr(uwt, "load_machine_config", lambda runtime_root=None: config)
    transport = uwt.BrowserWebTransport(config)
    with pytest.raises(AgentError) as error:
        transport._payload_with_plugin({})
    assert error.value.code == "PLUGIN_APP_ID_REQUIRED"


def test_raw_cdp_bridge_wakes_chatgpt_target_before_page_commands():
    source = uwt.Path(uwt.__file__).with_name("ultra_cdp_bridge.mjs").read_text(encoding="utf-8")
    activation = source.index('"/json/activate/"')
    page_socket = source.index("new WebSocket(target.webSocketDebuggerUrl)")
    runtime_eval = source.index('call("Runtime.evaluate"')
    assert activation < page_socket < runtime_eval


def test_read_retries_transient_429_without_replaying_mutation(monkeypatch, tmp_path):
    config = MachineConfig.from_mapping({}, runtime_root=str(tmp_path))
    transport = uwt.BrowserWebTransport(config)
    responses = [
        {"error": "WEB_READ_REJECTED", "http_status": 429, "retry_after": "0", "account_tag": "acct"},
        {"error": "WEB_READ_REJECTED", "http_status": 429, "retry_after": None, "account_tag": "acct"},
        {"account_tag": "acct", "data": {"ok": True}},
    ]
    calls = []
    sleeps = []
    monkeypatch.setattr(transport, "_bridge", lambda action, **kwargs: calls.append((action, kwargs)) or responses.pop(0))
    monkeypatch.setattr(uwt.time, "sleep", sleeps.append)

    result = transport._read("/backend-api/conversation/example", "conversation", "acct")

    assert result["data"] == {"ok": True}
    assert [action for action, _ in calls] == ["read", "read", "read"]
    assert sleeps == [0.8, 1.5]


def test_read_exhausted_429_is_explicitly_transient(monkeypatch, tmp_path):
    config = MachineConfig.from_mapping({}, runtime_root=str(tmp_path))
    transport = uwt.BrowserWebTransport(config)
    monkeypatch.setattr(
        transport,
        "_bridge",
        lambda action, **kwargs: {
            "error": "WEB_READ_REJECTED",
            "http_status": 429,
            "retry_after": "0",
            "account_tag": "acct",
        },
    )
    monkeypatch.setattr(uwt.time, "sleep", lambda _seconds: None)

    with pytest.raises(AgentError) as error:
        transport._read("/backend-api/conversation/example", "conversation", "acct")

    assert error.value.code == "WEB_READ_THROTTLED"
    assert "app is not missing" in str(error.value).lower()


def test_read_retries_runtime_evaluate_timeout_with_larger_outer_budget(monkeypatch, tmp_path):
    config = MachineConfig.from_mapping({}, runtime_root=str(tmp_path))
    transport = uwt.BrowserWebTransport(config)
    calls = []
    sleeps = []

    def fake_bridge(action, **kwargs):
        calls.append((action, kwargs))
        if len(calls) == 1:
            raise AgentError("CDP_BRIDGE_FAILED", "CDP command timeout: Runtime.evaluate")
        return {"account_tag": "acct", "data": {"ok": True}}

    monkeypatch.setattr(transport, "_bridge", fake_bridge)
    monkeypatch.setattr(uwt.time, "sleep", sleeps.append)

    result = transport._read("/backend-api/example", "project", "acct")

    assert result["data"] == {"ok": True}
    assert len(calls) == 2
    assert all(kwargs["timeout_seconds"] == uwt.READ_BRIDGE_TIMEOUT_SECONDS for _, kwargs in calls)
    assert uwt.READ_BRIDGE_TIMEOUT_SECONDS > 24
    assert sleeps == [0.8]


def test_read_exhausted_runtime_evaluate_timeout_is_explicit(monkeypatch, tmp_path):
    config = MachineConfig.from_mapping({}, runtime_root=str(tmp_path))
    transport = uwt.BrowserWebTransport(config)
    monkeypatch.setattr(
        transport,
        "_bridge",
        lambda action, **kwargs: (_ for _ in ()).throw(
            AgentError("CDP_BRIDGE_FAILED", "CDP command timeout: Runtime.evaluate")
        ),
    )
    monkeypatch.setattr(uwt.time, "sleep", lambda _seconds: None)

    with pytest.raises(AgentError) as error:
        transport._read("/backend-api/example", "project", "acct")

    assert error.value.code == "WEB_READ_TIMEOUT"
    assert error.value.details["bridge_error"] == "CDP_BRIDGE_FAILED"
