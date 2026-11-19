from __future__ import annotations

import asyncio
from pathlib import Path

from mcp import Client

import server as server_module
from server import mcp


def call_tool(name: str, arguments: dict):
    async def run():
        async with Client(mcp) as client:
            return await client.call_tool(name, arguments)

    return asyncio.run(run()).structured_content


def _match_projection(result: dict) -> list[tuple[str, int, str]]:
    return [
        (Path(item["path"]).name, int(item["line"]), str(item["text"]))
        for item in result["matches"]
    ]


def _path_projection(result: dict) -> list[str]:
    return sorted(Path(item["path"]).relative_to(result["root"]).as_posix() for item in result["matches"])


def test_native_search_text_matches_python_fallback(monkeypatch, tmp_path: Path):
    monkeypatch.setattr(server_module.native_backend, "prefer_native_for_tree", lambda *args, **kwargs: True)
    root = tmp_path / "repo"
    root.mkdir()
    (root / "first.py").write_text("before\nNeedle one\nafter\n", encoding="utf-8")
    nested = root / "nested"
    nested.mkdir()
    (nested / "second.PY").write_text("zero\nneedle two\ntail\n", encoding="utf-8")
    ignored = root / "node_modules"
    ignored.mkdir()
    (ignored / "ignored.py").write_text("needle ignored\n", encoding="utf-8")

    args = {
        "root": str(root),
        "query": "needle",
        "regex": False,
        "file_pattern": "*.py",
        "case_sensitive": False,
        "context_lines": 1,
        "max_results": 20,
        "max_file_bytes": 1024 * 1024,
        "timeout_seconds": 10,
        "include_ignored": False,
    }
    native = call_tool("search_text", args)
    assert native.get("backend") in {None, "ripgrep"}

    monkeypatch.setenv("GPT_PC_NATIVE_ACCELERATION", "0")
    fallback = call_tool("search_text", args)

    assert _match_projection(native) == _match_projection(fallback)
    assert all("ignored.py" not in item[0] for item in _match_projection(native))


def test_native_search_files_matches_python_fallback(monkeypatch, tmp_path: Path):
    monkeypatch.setattr(server_module.native_backend, "prefer_native_for_tree", lambda *args, **kwargs: True)
    root = tmp_path / "repo"
    root.mkdir()
    (root / "top.py").write_text("", encoding="utf-8")
    (root / "top.txt").write_text("", encoding="utf-8")
    nested = root / "nested"
    nested.mkdir()
    (nested / "nested.PY").write_text("", encoding="utf-8")
    ignored = root / "node_modules"
    ignored.mkdir()
    (ignored / "ignored.py").write_text("", encoding="utf-8")

    args = {
        "root": str(root),
        "pattern": "*.py",
        "recursive": True,
        "include_directories": False,
        "case_sensitive": False,
        "max_results": 20,
        "timeout_seconds": 10,
        "include_ignored": False,
    }
    native = call_tool("search_files", args)
    assert native.get("backend") in {None, "fd"}

    monkeypatch.setenv("GPT_PC_NATIVE_ACCELERATION", "0")
    fallback = call_tool("search_files", args)

    assert _path_projection(native) == _path_projection(fallback)
    assert "node_modules/ignored.py" not in _path_projection(native)


def test_native_search_respects_max_results(monkeypatch, tmp_path: Path):
    monkeypatch.setattr(server_module.native_backend, "prefer_native_for_tree", lambda *args, **kwargs: True)
    root = tmp_path / "repo"
    root.mkdir()
    for index in range(5):
        (root / f"file{index}.py").write_text("needle\n", encoding="utf-8")

    files = call_tool(
        "search_files",
        {
            "root": str(root),
            "pattern": "*.py",
            "max_results": 2,
            "timeout_seconds": 10,
        },
    )
    text = call_tool(
        "search_text",
        {
            "root": str(root),
            "query": "needle",
            "file_pattern": "*.py",
            "max_results": 2,
            "timeout_seconds": 10,
        },
    )
    assert files.get("backend") in {None, "fd"}
    assert files["count"] == 2
    assert files["truncated"] is True
    assert text.get("backend") in {None, "ripgrep"}
    assert text["count"] == 2
    assert text["truncated"] is True
