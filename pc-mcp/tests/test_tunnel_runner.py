"""Offline process/log lifecycle checks; never touch the running Tunnel."""
from pathlib import Path
import importlib.util
import io

import pytest


@pytest.fixture
def tunnel_runner():
    path = Path(__file__).resolve().parents[2] / 'scripts' / 'run-tunnel-client.py'
    spec = importlib.util.spec_from_file_location('tunnel_runner_fixture', path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.mark.parametrize('text', ['x' * 1000, '中🙂\n' * 200], ids=['ascii', 'unicode'])
def test_long_record_never_exceeds_rotation_cap(tunnel_runner, tmp_path, monkeypatch, text):
    monkeypatch.setattr(tunnel_runner, 'MAX_LOG_BYTES', 128)
    path = tmp_path / 'tunnel.log'
    with tunnel_runner.RotatingTextLog(path) as log:
        log.write(text)
    files = list(tmp_path.iterdir())
    assert len(files) <= tunnel_runner.BACKUP_COUNT + 1
    assert all(p.stat().st_size <= 128 for p in files)
    assert path.read_text(encoding='utf-8').endswith('\n')


def test_log_failure_reaps_only_owned_tunnel_process(tunnel_runner, tmp_path, monkeypatch):
    class Process:
        def __init__(self):
            self.stdout = io.StringIO('hello\n')
            self.returncode = None
            self.terminated = False
        def poll(self):
            return self.returncode
        def terminate(self):
            self.terminated = True
            self.returncode = -1
        def wait(self, timeout=None):
            return self.returncode
    process = Process()
    monkeypatch.setattr(tunnel_runner.subprocess, 'Popen', lambda *_a, **_kw: process)
    def full_disk(_self):
        raise OSError('disk full')
    monkeypatch.setattr(tunnel_runner.RotatingTextLog, '__enter__', full_disk)
    with pytest.raises(OSError, match='disk full'):
        tunnel_runner.main(['--log-file', str(tmp_path/'log.txt'), '--', 'fixture-not-a-real-executable'])
    assert process.terminated
    assert process.stdout.closed


def test_tunnel_exit_code_and_log_are_preserved(tunnel_runner, tmp_path, monkeypatch):
    class Process:
        def __init__(self):
            self.stdout = io.StringIO('one\ntwo\n')
        def wait(self, timeout=None):
            return 7
        def poll(self):
            return 7
        def terminate(self):
            raise AssertionError('Do not terminate an exited process')
    process = Process()
    monkeypatch.setattr(tunnel_runner.subprocess, 'Popen', lambda *_a, **_kw: process)
    path = tmp_path / 'log.txt'
    assert tunnel_runner.main(['--log-file', str(path), '--', 'fixture-not-real']) == 7
    assert path.read_text(encoding='utf-8') == 'one\ntwo\n'
    assert process.stdout.closed
