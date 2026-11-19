from __future__ import annotations

import io
import sys

from server import _BoundedCommandOutput, _run_argv


def test_output_capture_drains_without_retaining_the_whole_stream():
    capture = _BoundedCommandOutput(4096)
    capture.consume(io.BytesIO(b"x" * (5 * 1024 * 1024)))
    data, truncated = capture.snapshot()
    assert data == b"x" * 4096
    assert len(capture.buffer) == 4096
    assert capture.total_bytes == 5 * 1024 * 1024
    assert truncated is True


def test_large_stdout_and_stderr_keep_exit_status_and_utf8(tmp_path):
    result = _run_argv(
        [sys.executable, "-c", "import os; os.write(1, '\\u4e2d\\u6587'.encode('utf-8')*500000); os.write(2, b'e'*2000000); raise SystemExit(7)"],
        cwd=tmp_path, timeout_seconds=15, max_output_chars=101,
    )
    assert result["exit_code"] == 7
    assert result["stdout"].startswith("中文" * 50 + "中")
    assert result["stderr"].startswith("e" * 101)
    assert result["stdout_truncated"] and result["stderr_truncated"]
    assert len(result["stdout"]) < 150 and len(result["stderr"]) < 150
    assert result["stdout_encoding"] == "utf-8"
    assert result["timed_out"] is False


def test_timeout_does_not_duplicate_partial_output(tmp_path):
    result = _run_argv(
        [sys.executable, "-u", "-c", "import time; print('ONLY_ONCE'); time.sleep(20)"],
        cwd=tmp_path, timeout_seconds=2, max_output_chars=1000,
    )
    assert result["timed_out"] is True
    assert result["stdout"].count("ONLY_ONCE") == 1


def test_small_output_is_preserved(tmp_path):
    result = _run_argv([sys.executable, "-c", "print('ok')"], cwd=tmp_path, timeout_seconds=10, max_output_chars=100)
    assert result["exit_code"] == 0
    assert result["stdout"].strip() == "ok"
    assert result["stdout_truncated"] is False
    assert result["stderr_truncated"] is False
