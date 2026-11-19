from __future__ import annotations

import json
import subprocess
from pathlib import Path

import server as server_module


def _init_git_repo(tmp_path: Path) -> tuple[Path, Path]:
    repo = tmp_path / "activity-git-repo"
    repo.mkdir()
    subprocess.run(["git", "init"], cwd=repo, check=True, capture_output=True)
    subprocess.run(["git", "config", "user.email", "activity@test.invalid"], cwd=repo, check=True)
    subprocess.run(["git", "config", "user.name", "Activity Test"], cwd=repo, check=True)
    target = repo / "tracked.txt"
    target.write_text("one\n", encoding="utf-8")
    subprocess.run(["git", "add", "tracked.txt"], cwd=repo, check=True)
    subprocess.run(["git", "commit", "-m", "initial"], cwd=repo, check=True, capture_output=True)
    return repo, target


def test_command_arguments_keep_diagnostic_context_and_redact_secrets():
    safe = server_module._activity_safe_arguments(
        server_module.run_python,
        (),
        {
            "code": "print('visible context')\napi_key='super-secret-value'",
            "args": ["--probe", "value"],
            "cwd": r"D:\work",
            "workspace_lease_id": "must-not-leak",
        },
    )
    payload = json.dumps(safe, ensure_ascii=False)
    assert "visible context" in payload
    assert "--probe" in payload
    assert "work" in payload
    assert "super-secret-value" not in payload
    assert "must-not-leak" not in payload


def test_command_file_delta_detects_clean_and_already_dirty_writes(monkeypatch, tmp_path):
    repo, target = _init_git_repo(tmp_path)
    lease = {"workspace": str(repo), "lease_id": "lease-activity", "thread_id": "thread-activity"}
    emitted: list[dict] = []
    monkeypatch.setattr(
        server_module,
        "_activity_emit",
        lambda event_type, **payload: emitted.append({"type": event_type, **payload}),
    )

    clean_context = server_module._activity_command_file_context(
        server_module.run_python, (), {"code": "pass", "cwd": str(repo)}, lease
    )
    assert clean_context is not None
    target.write_text("two\n", encoding="utf-8")
    clean_after_size = target.stat().st_size
    server_module._activity_emit_command_file_changes(
        server_module.run_python, clean_context, call_id="clean-write", lease=lease
    )
    clean_rows = [row for row in emitted if row["type"] == "file.changed"]
    assert len(clean_rows) == 1
    assert clean_rows[0]["operation"] == "write"
    assert clean_rows[0]["path"].endswith("tracked.txt")
    assert clean_rows[0]["beforeSize"] == 4
    assert clean_rows[0]["afterSize"] == clean_after_size
    assert "+two" in clean_rows[0]["diff"]

    emitted.clear()
    dirty_context = server_module._activity_command_file_context(
        server_module.run_python, (), {"code": "pass", "cwd": str(repo)}, lease
    )
    assert dirty_context is not None
    target.write_text("three\n", encoding="utf-8")
    dirty_after_size = target.stat().st_size
    server_module._activity_emit_command_file_changes(
        server_module.run_python, dirty_context, call_id="dirty-write", lease=lease
    )
    dirty_rows = [row for row in emitted if row["type"] == "file.changed"]
    assert len(dirty_rows) == 1
    assert dirty_rows[0]["beforeSize"] == clean_after_size
    assert dirty_rows[0]["afterSize"] == dirty_after_size
    assert "+three" in dirty_rows[0]["diff"]
    assert "-two" in dirty_rows[0]["diff"]


def test_command_file_delta_emits_each_file_in_a_batch(monkeypatch, tmp_path):
    repo, target = _init_git_repo(tmp_path)
    lease = {"workspace": str(repo), "lease_id": "lease-batch", "thread_id": "thread-batch"}
    context = server_module._activity_command_file_context(
        server_module.run_python, (), {"code": "pass", "cwd": str(repo)}, lease
    )
    assert context is not None

    added = repo / "added.txt"
    target.write_text("updated\n", encoding="utf-8")
    added.write_text("new file\n", encoding="utf-8")
    target_after_size = target.stat().st_size
    added_after_size = added.stat().st_size
    emitted: list[dict] = []
    monkeypatch.setattr(
        server_module,
        "_activity_emit",
        lambda event_type, **payload: emitted.append({"type": event_type, **payload}),
    )
    server_module._activity_emit_command_file_changes(
        server_module.run_python, context, call_id="batch-write", lease=lease
    )

    rows = [row for row in emitted if row["type"] == "file.changed"]
    assert {Path(row["path"]).name for row in rows} == {"tracked.txt", "added.txt"}
    tracked = next(row for row in rows if row["path"].endswith("tracked.txt"))
    created = next(row for row in rows if row["path"].endswith("added.txt"))
    assert tracked["operation"] == "write"
    assert tracked["beforeSize"] == 4
    assert tracked["afterSize"] == target_after_size
    assert created["operation"] == "create"
    assert created["beforeSize"] is None
    assert created["afterSize"] == added_after_size


def test_command_file_delta_falls_back_to_non_git_filesystem(monkeypatch, tmp_path):
    root = tmp_path / "plain-workspace"
    root.mkdir()
    changed = root / "changed.txt"
    deleted = root / "deleted.txt"
    changed.write_text("before\n", encoding="utf-8")
    deleted.write_text("remove me\n", encoding="utf-8")
    ignored = root / "node_modules"
    ignored.mkdir()
    ignored_file = ignored / "noise.txt"
    ignored_file.write_text("old\n", encoding="utf-8")
    lease = {"workspace": str(root), "lease_id": "lease-plain", "thread_id": "thread-plain"}

    context = server_module._activity_command_file_context(
        server_module.run_python, (), {"code": "pass", "cwd": str(root)}, lease
    )
    assert context is not None
    assert context["detector"] == "filesystem-delta"
    changed.write_text("after\n", encoding="utf-8")
    deleted.unlink()
    added = root / "added.txt"
    added.write_text("created\n", encoding="utf-8")
    ignored_file.write_text("new but ignored\n", encoding="utf-8")

    emitted: list[dict] = []
    monkeypatch.setattr(
        server_module,
        "_activity_emit",
        lambda event_type, **payload: emitted.append({"type": event_type, **payload}),
    )
    server_module._activity_emit_command_file_changes(
        server_module.run_python, context, call_id="plain-batch", lease=lease
    )

    rows = [row for row in emitted if row["type"] == "file.changed"]
    assert {Path(row["path"]).name for row in rows} == {"changed.txt", "deleted.txt", "added.txt"}
    assert all(row["detectedBy"] == "filesystem-delta" for row in rows)
    by_name = {Path(row["path"]).name: row for row in rows}
    assert by_name["changed.txt"]["operation"] == "write"
    assert "+after" in by_name["changed.txt"]["diff"]
    assert "-before" in by_name["changed.txt"]["diff"]
    assert by_name["deleted.txt"]["operation"] == "delete"
    assert by_name["deleted.txt"]["afterHash"] is None
    assert by_name["added.txt"]["operation"] == "create"
    assert by_name["added.txt"]["beforeHash"] is None
    assert not any("node_modules" in row["path"] for row in rows)


def test_direct_file_event_includes_size_hash_and_bytes_written(monkeypatch, tmp_path):
    target = tmp_path / "direct.txt"
    target.write_text("a\n", encoding="utf-8")
    before_size = target.stat().st_size
    context = server_module._activity_file_context(
        server_module.write_text_file,
        (),
        {"path": str(target), "content": "longer\n"},
    )
    assert context is not None
    target.write_text("longer\n", encoding="utf-8")
    after_size = target.stat().st_size
    emitted: list[dict] = []
    monkeypatch.setattr(
        server_module,
        "_activity_emit",
        lambda event_type, **payload: emitted.append({"type": event_type, **payload}),
    )
    server_module._activity_emit_file_change(
        server_module.write_text_file,
        context,
        {"status": "written", "path": str(target), "bytes_written": after_size},
        call_id="direct-write",
        lease={"lease_id": "lease-direct", "thread_id": "thread-direct"},
    )
    rows = [row for row in emitted if row["type"] == "file.changed"]
    assert len(rows) == 1
    assert rows[0]["beforeSize"] == before_size
    assert rows[0]["afterSize"] == after_size
    assert rows[0]["bytesWritten"] == after_size
    assert rows[0]["beforeHash"] != rows[0]["afterHash"]
    assert "+longer" in rows[0]["diff"]


def test_direct_replace_patch_and_delete_emit_auditable_file_events(monkeypatch, tmp_path):
    emitted: list[dict] = []
    monkeypatch.setattr(
        server_module,
        "_activity_emit",
        lambda event_type, **payload: emitted.append({"type": event_type, **payload}),
    )
    lease = {"lease_id": "lease-direct-ops", "thread_id": "thread-direct-ops"}

    replace_target = tmp_path / "replace.txt"
    replace_target.write_text("old\n", encoding="utf-8")
    replace_context = server_module._activity_file_context(
        server_module.replace_text, (), {"path": str(replace_target), "old": "old", "new": "new"}
    )
    replace_target.write_text("new\n", encoding="utf-8")
    server_module._activity_emit_file_change(
        server_module.replace_text,
        replace_context,
        {"status": "written", "path": str(replace_target), "replacements": 1},
        call_id="replace",
        lease=lease,
    )

    patch_target = tmp_path / "patch.txt"
    patch_target.write_text("one\n", encoding="utf-8")
    patch_text = "*** Begin Patch\n*** Update File: patch.txt\n@@\n-one\n+two\n*** End Patch"
    patch_context = server_module._activity_file_context(
        server_module.apply_patch, (), {"path": str(patch_target), "patch": patch_text}
    )
    patch_target.write_text("two\n", encoding="utf-8")
    server_module._activity_emit_file_change(
        server_module.apply_patch,
        patch_context,
        {"status": "written", "path": str(patch_target)},
        call_id="patch",
        lease=lease,
    )

    delete_target = tmp_path / "delete.txt"
    delete_target.write_text("gone\n", encoding="utf-8")
    delete_context = server_module._activity_file_context(
        server_module.delete_path, (), {"path": str(delete_target)}
    )
    delete_target.unlink()
    server_module._activity_emit_file_change(
        server_module.delete_path,
        delete_context,
        {"status": "deleted", "path": str(delete_target)},
        call_id="delete",
        lease=lease,
    )

    rows = {row["operation"]: row for row in emitted if row["type"] == "file.changed"}
    assert {"replace", "patch", "delete"} <= rows.keys()
    assert "+new" in rows["replace"]["diff"]
    assert "-old" in rows["replace"]["diff"]
    assert "+two" in rows["patch"]["diff"]
    assert rows["patch"]["beforeHash"] != rows["patch"]["afterHash"]
    assert rows["delete"]["beforeHash"] is not None
    assert rows["delete"]["afterHash"] is None
    assert rows["delete"]["afterSize"] is None


def test_command_file_delta_survives_internal_commit(monkeypatch, tmp_path):
    repo, target = _init_git_repo(tmp_path)
    lease = {"workspace": str(repo), "lease_id": "lease-commit", "thread_id": "thread-commit"}
    context = server_module._activity_command_file_context(
        server_module.run_python, (), {"code": "pass", "cwd": str(repo)}, lease
    )
    assert context is not None
    target.write_text("committed change\n", encoding="utf-8")
    subprocess.run(["git", "add", "tracked.txt"], cwd=repo, check=True)
    subprocess.run(["git", "commit", "-m", "inside tool"], cwd=repo, check=True, capture_output=True)

    emitted: list[dict] = []
    monkeypatch.setattr(
        server_module,
        "_activity_emit",
        lambda event_type, **payload: emitted.append({"type": event_type, **payload}),
    )
    server_module._activity_emit_command_file_changes(
        server_module.run_python, context, call_id="committed-write", lease=lease
    )
    rows = [row for row in emitted if row["type"] == "file.changed"]
    assert len(rows) == 1
    assert "+committed change" in rows[0]["diff"]


def test_truncation_warning_is_deduped_per_lease_root_and_resets(monkeypatch, tmp_path):
    repo, _ = _init_git_repo(tmp_path)
    lease = {"workspace": str(repo), "lease_id": "lease-truncated", "thread_id": "thread-truncated"}
    emitted: list[dict] = []
    monkeypatch.setattr(server_module, "_activity_emit", lambda event_type, **payload: emitted.append({"type": event_type, **payload}))
    monkeypatch.setattr(server_module, "_activity_git_status_paths", lambda _root: (set(), False))
    with server_module._ACTIVITY_TRUNCATION_WARNING_LOCK:
        server_module._ACTIVITY_TRUNCATION_WARNING_KEYS.clear()

    truncated_context = {"root": repo.resolve(), "head": None, "dirty": {}, "truncated": True}
    clear_context = {"root": repo.resolve(), "head": None, "dirty": {}, "truncated": False}
    server_module._activity_emit_command_file_changes(server_module.run_python, truncated_context, call_id="first", lease=lease)
    server_module._activity_emit_command_file_changes(server_module.run_python, truncated_context, call_id="second", lease=lease)
    warnings = [row for row in emitted if row["type"] == "agent.progress" and "已截断" in row.get("text", "")]
    assert len(warnings) == 1

    server_module._activity_emit_command_file_changes(server_module.run_python, clear_context, call_id="reset", lease=lease)
    server_module._activity_emit_command_file_changes(server_module.run_python, truncated_context, call_id="third", lease=lease)
    warnings = [row for row in emitted if row["type"] == "agent.progress" and "已截断" in row.get("text", "")]
    assert len(warnings) == 2
