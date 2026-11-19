from __future__ import annotations

import hashlib
import os
from pathlib import Path
from types import SimpleNamespace

import pytest

from context_patch import apply_context_patch
import server
from test_server import call_tool, isolate_runtime


@pytest.fixture(autouse=True)
def isolated(monkeypatch, tmp_path):
    isolate_runtime(monkeypatch, tmp_path)


def context_patch(body, filename="module.py"):
    return f"*** Begin Patch\n*** Update File: {filename}\n{body}\n*** End Patch"


def test_range_returns_hash_of_whole_file_from_same_read(tmp_path):
    path = tmp_path / "module.py"
    data = "标题\r\nvalue = 1\r\n尾部\r\n".encode("utf-8")
    path.write_bytes(data)
    result = server.read_text_range(str(path), start_line=2, end_line=2)
    assert result["content"] == "value = 1\r\n"
    assert result["sha256"] == hashlib.sha256(data).hexdigest()
    assert result["snapshot_stable"] is True


def test_batch_returns_independent_hashes_and_preserves_failure_isolation(tmp_path):
    paths = [tmp_path / "a.py", tmp_path / "b.py"]
    for index, path in enumerate(paths):
        path.write_text(f"v = {index}\n", encoding="utf-8")
    result = server.read_multiple_files([str(p) for p in paths] + [str(tmp_path / "missing.py")])
    for item, path in zip(result["files"], paths):
        assert item["sha256"] == hashlib.sha256(path.read_bytes()).hexdigest()
    assert "error" in result["files"][2]


def test_truncated_or_changing_read_never_advertises_a_full_file_hash(monkeypatch, tmp_path):
    path = tmp_path / "module.py"
    path.write_text("abcdef" * 1000, encoding="utf-8")
    result = server.read_text_range(str(path), max_bytes=50)
    assert result["truncated"] and result["sha256"] is None
    real_fstat = os.fstat
    calls = 0

    def changed_stat(fd):
        nonlocal calls
        calls += 1
        info = real_fstat(fd)
        return SimpleNamespace(st_size=info.st_size, st_mtime_ns=info.st_mtime_ns + calls)

    monkeypatch.setattr(server.os, "fstat", changed_stat)
    result = server.read_text_range(str(path))
    assert result["snapshot_stable"] is False and result["sha256"] is None


@pytest.mark.parametrize("ending,final_newline", [("\n", True), ("\r\n", True), ("\r\n", False)])
def test_context_patch_preserves_line_endings_and_needs_no_line_numbers(tmp_path, ending, final_newline):
    original = ending.join(["# title", "def compute(x):", "    return x + 1"])
    if final_newline:
        original += ending
    patch = context_patch("@@\n def compute(x):\n-    return x + 1\n+    return x + 2")
    updated, count = apply_context_patch(original, patch, tmp_path / "module.py")
    assert updated == original.replace("x + 1", "x + 2")
    assert count == 1


@pytest.mark.parametrize("body", ["@@\n-x = 1\n+x = 2", "@@\n-y = 0\n+y = 1", "@@\n+unanchored = 1"])
def test_context_patch_rejects_ambiguous_missing_or_unanchored_edits(tmp_path, body):
    with pytest.raises(ValueError):
        apply_context_patch("x = 1\nx = 1\n", context_patch(body), tmp_path / "module.py")


def test_context_patch_handles_multiple_hunks_and_rejects_another_file(tmp_path):
    original = "a = 1\n\nb = 2\n"
    patch = context_patch("@@\n-a = 1\n+a = 3\n@@\n-b = 2\n+b = 4\n*** End of File")
    updated, _ = apply_context_patch(original, patch, tmp_path / "module.py")
    assert updated == "a = 3\n\nb = 4\n"
    with pytest.raises(ValueError, match="path"):
        apply_context_patch(original, patch, tmp_path / "other.py")


def test_read_then_context_patch_and_stale_version_rejection(tmp_path):
    workspace = tmp_path / "project"
    workspace.mkdir()
    lease = call_tool("acquire_workspace", {"path": str(workspace)})
    path = workspace / "module.py"
    path.write_text("value = 1\n", encoding="utf-8")
    read = call_tool("read_text_range", {"path": str(path), "workspace_lease_id": lease["lease_id"]})
    patch = context_patch("@@\n-value = 1\n+value = 2")
    changed = call_tool("apply_patch", {"path": str(path), "patch": patch, "expected_sha256": read["sha256"], "workspace_lease_id": lease["lease_id"]})
    assert changed["status"] == "written" and path.read_text() == "value = 2\n"
    assert changed["sha256"] != read["sha256"]
    assert changed["diff_truncated"] is False
    assert changed["diff_additions"] == 1 and changed["diff_deletions"] == 1
    assert "-value = 1" in changed["diff"] and "+value = 2" in changed["diff"]
    stale = call_tool("apply_patch", {"path": str(path), "patch": patch, "expected_sha256": read["sha256"], "workspace_lease_id": lease["lease_id"]})
    assert stale["status"] == "conflict" and path.read_text() == "value = 2\n"


def test_replace_text_returns_post_write_sha_and_real_bounded_diff(tmp_path):
    workspace = tmp_path / "replace-project"
    workspace.mkdir()
    lease = call_tool("acquire_workspace", {"path": str(workspace)})
    path = workspace / "settings.txt"
    path.write_text("mode=old\nkeep=yes\n", encoding="utf-8")
    read = call_tool(
        "read_text_range",
        {"path": str(path), "workspace_lease_id": lease["lease_id"]},
    )
    changed = call_tool(
        "replace_text",
        {
            "path": str(path),
            "old": "mode=old",
            "new": "mode=new",
            "expected_sha256": read["sha256"],
            "workspace_lease_id": lease["lease_id"],
        },
    )
    assert changed["status"] == "written"
    assert changed["sha256"] == hashlib.sha256(path.read_bytes()).hexdigest()
    assert changed["diff_truncated"] is False
    assert changed["diff_additions"] == 1 and changed["diff_deletions"] == 1
    assert "-mode=old" in changed["diff"] and "+mode=new" in changed["diff"]
