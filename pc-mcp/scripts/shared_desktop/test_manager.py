"""Non-invasive tests: no real desktop, Chrome, or user profile is modified."""
import hashlib
from pathlib import Path
import sys

import pytest

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import manager


def test_pinned_dll_hash():
    assert hashlib.sha256(manager.DLL.read_bytes()).hexdigest() == manager.DLL_SHA256


def test_setup_preserves_active_desktop(monkeypatch):
    state = {"n": 1, "active": 0, "names": [""]}
    calls = []

    def fake_func(_dll, symbol, *_types):
        def invoke(*args):
            calls.append((symbol, args))
            if symbol == "CreateDesktop":
                state["n"] += 1
                state["names"].append("")
                return state["n"] - 1
            if symbol == "SetDesktopName":
                i, data = args
                state["names"][i] = data.decode("utf-8")
                return 0
            if symbol == "GoToDesktopNumber":
                state["active"] = args[0]
                return 0
            raise AssertionError(symbol)
        return invoke

    monkeypatch.setattr(manager, "func", fake_func)
    monkeypatch.setattr(manager, "count", lambda _dll: state["n"])
    monkeypatch.setattr(manager, "current", lambda _dll: state["active"])
    monkeypatch.setattr(manager, "name", lambda _dll, i: state["names"][i])
    result = manager.desktop_setup(object(), ["我的工作", "AI作业", "AI接单"])
    assert result["count"] == 3
    assert result["current_number"] == 1
    assert [d["name"] for d in result["desktops"]] == ["我的工作", "AI作业", "AI接单"]
    assert [c[0] for c in calls].count("CreateDesktop") == 2


def test_setup_does_not_rename_existing_user_desktops(monkeypatch):
    monkeypatch.setattr(manager, "count", lambda _: 2)
    monkeypatch.setattr(manager, "current", lambda _: 0)
    monkeypatch.setattr(manager, "name", lambda _, i: ["Project A", "Project B"][i])
    with pytest.raises(manager.DesktopError, match="Existing named desktops"):
        manager.desktop_setup(object(), ["我的工作", "AI作业", "AI接单"])


def test_switch_rejects_invalid_index_without_native_call(monkeypatch):
    monkeypatch.setattr(manager, "count", lambda _: 3)
    with pytest.raises(manager.DesktopError, match="out of range"):
        manager.desktop_switch(object(), 4)


def test_new_browser_profile_differs_from_default_chrome():
    assert str(manager.PROFILE).lower().endswith(r"gpt_pcsharedbrowser\chromeprofile")
    assert manager.PORT == 9336
    assert manager.ENDPOINT == "http://127.0.0.1:9336"


def test_existing_open_uses_user_chrome_and_moves_only_new_window(monkeypatch):
    launches = []
    moved = []
    saved = []
    windows = [{101}, {101, 202}]
    monkeypatch.setattr(manager, "count", lambda _dll: 3)
    monkeypatch.setattr(manager, "load_state", lambda: {})
    monkeypatch.setattr(manager, "existing_chrome_root", lambda: 22222)
    monkeypatch.setattr(manager, "existing_chrome_profile", lambda: "Default")
    monkeypatch.setattr(manager, "chrome_exe", lambda: Path(r"C:\Program Files\Google\Chrome\Application\chrome.exe"))
    monkeypatch.setattr(manager, "chrome_windows", lambda _pid: windows.pop(0))
    monkeypatch.setattr(manager.subprocess, "Popen", lambda args, **kw: launches.append(args))
    monkeypatch.setattr(manager, "desktop_move", lambda _dll, hwnd, number: moved.append((hwnd, number)))
    monkeypatch.setattr(manager, "save_state", lambda s: saved.append(s.copy()))
    result = manager.existing_chrome_window(object(), "homework", 2, "https://example.com")
    assert result["status"] == "opened_existing_profile"
    assert moved == [(202, 2)]
    assert launches[0][-2:] == ["--new-window", "https://example.com"]
    assert "--profile-directory=Default" in launches[0]
    assert saved[0]["homework"]["hwnd"] == 202


def test_existing_open_rejects_invalid_url_before_start(monkeypatch):
    monkeypatch.setattr(manager, "count", lambda _: 3)
    with pytest.raises(manager.DesktopError, match="Only http"):
        manager.existing_chrome_window(object(), "homework", 2, "file:///etc/passwd")


def test_existing_open_requires_unique_agent_name(monkeypatch):
    monkeypatch.setattr(manager, "count", lambda _: 3)
    monkeypatch.setattr(manager, "load_state", lambda: {"homework": {}})
    with pytest.raises(manager.DesktopError, match="already in use"):
        manager.existing_chrome_window(object(), "homework", 2, "about:blank")


def test_lazy_background_never_touches_desktop(monkeypatch):
    monkeypatch.setattr(manager, "count", lambda _: (_ for _ in ()).throw(AssertionError("desktop count touched")))
    result = manager.lazy_allocate(None, "agent-1", "background")
    assert result["desktop_number"] is None
    assert not result["desktop_created"]
    assert result["browser"] is None


def test_lazy_web_never_allocates_windows_desktop(monkeypatch):
    monkeypatch.setattr(manager, "count", lambda _: (_ for _ in ()).throw(AssertionError("desktop touched")))
    result = manager.lazy_allocate(None, "agent-2", "web")
    assert result["desktop_number"] is None
    assert result["browser"] == "existing_chrome_extension"


def test_lazy_visual_allocates_only_one_and_reuses(monkeypatch):
    state = {"n": 1, "active": 0, "names": ["Desktop 1"]}
    calls = []
    def fake_func(_dll, symbol, *_types):
        def call(*args):
            calls.append(symbol)
            if symbol == "CreateDesktop":
                state["n"] += 1
                state["names"].append("")
                return state["n"] - 1
            if symbol == "SetDesktopName":
                state["names"][args[0]] = args[1].decode("utf-8")
                return 0
            if symbol == "GoToDesktopNumber":
                state["active"] = args[0]
                return 0
            raise AssertionError(symbol)
        return call
    monkeypatch.setattr(manager, "func", fake_func)
    monkeypatch.setattr(manager, "count", lambda _: state["n"])
    monkeypatch.setattr(manager, "current", lambda _: state["active"])
    monkeypatch.setattr(manager, "name", lambda _, i: state["names"][i])
    first = manager.lazy_allocate(object(), "gui-agent", "visual")
    second = manager.lazy_allocate(object(), "gui-agent", "visual")
    assert first["desktop_created"] is True
    assert second["desktop_reused"] is True
    assert first["desktop_number"] == second["desktop_number"] == 2
    assert calls.count("CreateDesktop") == 1
    assert state["active"] == 0
