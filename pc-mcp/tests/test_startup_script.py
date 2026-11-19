from pathlib import Path
import subprocess
import shutil


def test_cold_start_continues_after_connection_refused(tmp_path):
    root = Path(__file__).resolve().parents[2]
    script = (root / "scripts/start-mcp-service.ps1").read_text(encoding="utf-8")
    # Exercise the real startup preflight in isolation, without starting or
    # stopping any service. Wait long enough to observe the refused connection.
    block = script[script.index("$client ="):script.index("Set-Location")].replace("Wait(500)", "Wait(3000)")
    log = str(tmp_path / "preflight.log").replace("'", "''")
    command = f"""
$ErrorActionPreference = 'Stop'
$reserved = [System.Net.Sockets.TcpListener]::new([System.Net.IPAddress]::Loopback, 0)
$reserved.Start()
$Port = $reserved.LocalEndpoint.Port
$reserved.Stop()
$HostAddress = '127.0.0.1'
$ServiceLog = '{log}'
{block}
Write-Output 'CONTINUED_TO_LAUNCH'
"""
    pwsh = shutil.which("pwsh")
    if not pwsh:
        import pytest
        pytest.skip("PowerShell 7 (pwsh) is an optional integration prerequisite")
    result = subprocess.run(
        [pwsh, "-NoProfile", "-NonInteractive", "-Command", command],
        capture_output=True, timeout=30,
    )
    assert result.returncode == 0, result.stderr.decode("utf-8", errors="replace")
    assert b"CONTINUED_TO_LAUNCH" in result.stdout
    assert "MCP listener unavailable" in (tmp_path / "preflight.log").read_text(encoding="utf-8-sig")


def test_stop_all_does_not_treat_gptagent_supervisor_as_user_work():
    root = Path(__file__).resolve().parents[2]
    script = (root / "scripts/stop-all.ps1").read_text(encoding="utf-8")
    assert "StartsWith('gptagent-supervisor-')" in script
    assert "$record.mode -eq 'execute_exclusive' -and -not $isInternalService" in script
