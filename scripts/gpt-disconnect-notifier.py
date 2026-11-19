from __future__ import annotations

import argparse
import base64
import json
import os
from pathlib import Path
import sqlite3
import subprocess
import sys
import uuid

PROJECT_ROOT = Path(os.environ.get("GPT_PC_HOME") or Path(__file__).resolve().parent.parent).expanduser().resolve()
PC_MCP_ROOT = PROJECT_ROOT / "pc-mcp"
GMAIL_BRIDGE = PROJECT_ROOT / "scripts" / "gmail-bridge.py"
FAILURE_RETRY_SECONDS = 10 * 60

sys.path.insert(0, str(PC_MCP_ROOT))
import runtime  # noqa: E402
from machine_config import load_machine_config  # noqa: E402


def _ultra_root_gate(lease_id: str) -> dict[str, object]:
    """Fail closed unless this is an active Ultra Root past its rescue window."""
    record = runtime.active_lease_record(lease_id)
    if not record:
        return {"eligible": False, "status": "target_missing"}
    probe = runtime.disconnect_probe_state(record)
    if not probe or probe.get("acked_at"):
        return {"eligible": False, "status": "recovered_or_unconfirmed"}
    confirmation = runtime.disconnect_confirmation_state(record)
    if (
        not confirmation
        or str(confirmation.get("probe_id") or "") != str(probe.get("id") or "")
    ):
        return {"eligible": False, "status": "disconnect_not_confirmed"}
    deadline = runtime._mode_deadline(confirmation.get("rescue_deadline_at"))
    if deadline is None or runtime.utc_now() <= deadline:
        return {
            "eligible": False,
            "status": "rescue_grace",
            "rescue_deadline_at": confirmation.get("rescue_deadline_at"),
        }

    config = load_machine_config(runtime_root=str(PROJECT_ROOT))
    if not config.ultra_collaboration:
        return {"eligible": False, "status": "not_ultra_root"}
    with sqlite3.connect(config.database_path) as connection:
        connection.row_factory = sqlite3.Row
        team = connection.execute(
            "SELECT team_id,state FROM ultra_teams WHERE root_lease_id=? LIMIT 1",
            (lease_id,),
        ).fetchone()
        if not team or str(team["state"] or "") != "active":
            return {"eligible": False, "status": "not_active_ultra_root"}
        rescue = connection.execute(
            """SELECT rescue_id FROM ultra_root_rescues
               WHERE team_id=? AND state IN ('prepared','submitting','reconciling')
               ORDER BY created_at DESC LIMIT 1""",
            (team["team_id"],),
        ).fetchone()
        if rescue:
            return {"eligible": False, "status": "root_rescue_in_progress"}
        return {
            "eligible": True,
            "status": "eligible",
            "team_id": str(team["team_id"]),
            "probe_id": str(probe.get("id") or ""),
        }


def _bridge(payload: dict[str, object], timeout_seconds: int = 60) -> dict[str, object]:
    encoded = base64.b64encode(json.dumps(payload, ensure_ascii=False).encode("utf-8")).decode("ascii")
    child_env = os.environ.copy()
    child_env["PYTHONIOENCODING"] = "utf-8"
    child_env["PYTHONUTF8"] = "1"
    process = subprocess.run(
        [sys.executable, str(GMAIL_BRIDGE), "--payload-base64", encoded],
        cwd=str(PROJECT_ROOT),
        env=child_env,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=timeout_seconds,
        check=False,
    )
    stdout = (process.stdout or "").strip()
    try:
        result = json.loads(stdout.splitlines()[-1]) if stdout else {}
    except (ValueError, IndexError):
        result = {}
    if process.returncode != 0 or not isinstance(result, dict) or result.get("status") in {None, "error", "oauth_setup_required"}:
        error = result.get("error") if isinstance(result, dict) else None
        raise RuntimeError(str(error or process.stderr or stdout or f"gmail bridge exit={process.returncode}"))
    return result


def notify(lease_id: str, *, stale_seconds: int = 300) -> dict[str, object]:
    destination = os.environ.get("GPT_PC_DISCONNECT_ALERT_EMAIL", "").strip()
    if not destination:
        return {"status": "not_configured", "lease_id": lease_id}
    gate = _ultra_root_gate(lease_id)
    if not gate.get("eligible"):
        return {"lease_id": lease_id, **gate}
    claim = runtime.claim_disconnect_alert(lease_id, stale_seconds=stale_seconds)
    if claim.get("status") != "claimed":
        return claim

    heartbeat_at = str(claim["heartbeat_at"])
    probe_id = str(claim.get("probe_id") or "")
    destination = os.environ.get("GPT_PC_DISCONNECT_ALERT_EMAIL", "").strip()
    expected_account = os.environ.get("GPT_PC_GMAIL_EXPECTED_ACCOUNT", "").strip()
    owner = str(claim.get("owner_label") or "GPT-PC 会话")
    workspace = str(claim.get("workspace") or "")
    stay_until = str(claim.get("stay_until_at") or "")
    resident_active = bool(claim.get("resident_active"))
    age_seconds = int(claim.get("age_seconds") or 0)
    stale_minutes = max(1, int(round(stale_seconds / 60)))

    notification_lease: dict[str, object] | None = None
    sent = False
    try:
        notification_lease = runtime.acquire_workspace(
            runtime.DESKTOP_OPS_WORKSPACE,
            mode="execute_exclusive",
            ttl_seconds=120,
            owner_label="GPT 掉线邮件通知",
            thread_id=f"system-disconnect-alert-{uuid.uuid4().hex}",
        )
        if probe_id:
            latest = runtime.active_lease_record(lease_id)
            latest_probe = runtime.disconnect_probe_state(latest or {})
            if (
                not latest_probe
                or str(latest_probe.get("id") or "") != probe_id
                or latest_probe.get("acked_at")
            ):
                runtime.complete_disconnect_alert(
                    lease_id, heartbeat_at, success=False, probe_id=probe_id
                )
                return {
                    "status": "cancelled_explicit_response",
                    "lease_id": lease_id,
                    "probe_id": probe_id,
                }
        second_gate = _ultra_root_gate(lease_id)
        if not second_gate.get("eligible"):
            runtime.complete_disconnect_alert(
                lease_id,
                heartbeat_at,
                success=False,
                probe_id=probe_id or None,
            )
            return {"lease_id": lease_id, **second_gate}
        disconnect_summary = (
            f"GPT-PC 已确认 Ultra Root 掉线：存活询问已在真实工具边界送达；"
            f"回复窗口结束后又保留了 5 分钟 Team 恢复窗口，期间没有成功 Root rescue，才发送本邮件。"
            f"当前没有正在执行的工具。总观察窗口至少约 {stale_minutes} 分钟。\n\n"
        )
        payload: dict[str, object] = {
            "action": "send",
            # This helper is unattended: a revoked OAuth token must never
            # open a consent browser repeatedly in the user's foreground.
            "interactive_auth": False,
            "to": destination,
            "subject": f"[GPT-PC] GPT 疑似掉线 · {owner}",
            "body": (
                disconnect_summary
                + f"任务：{owner}\n"
                f"Lease：{lease_id}\n"
                f"Workspace：{workspace}\n"
                f"最近 heartbeat：{heartbeat_at}\n"
                f"无 heartbeat：约 {age_seconds} 秒\n"
                f"驻场状态：{'是' if resident_active else '否'}\n"
                f"驻场保留至：{stay_until if resident_active else '未启用'}\n\n"
                "这是本机 Runtime liveness 通知；目标 lease 可能仍存在，可由新的 ChatGPT turn 恢复。"
            ),
            "expected_account": expected_account,
        }
        bridge_result = _bridge(payload)
        sent = True
        runtime.complete_disconnect_alert(lease_id, heartbeat_at, success=True, probe_id=probe_id or None)
        return {
            "status": "sent",
            "lease_id": lease_id,
            "heartbeat_at": heartbeat_at,
            "message_id": bridge_result.get("message_id"),
        }
    except Exception:
        runtime.complete_disconnect_alert(
            lease_id,
            heartbeat_at,
            success=False,
            probe_id=probe_id or None,
            retry_after_seconds=FAILURE_RETRY_SECONDS,
        )
        raise
    finally:
        if notification_lease:
            try:
                runtime.release_workspace(str(notification_lease["lease_id"]))
            except Exception:
                if sent:
                    pass


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--lease-id", required=True)
    parser.add_argument("--stale-seconds", type=int, default=300)
    args = parser.parse_args()
    try:
        result = notify(args.lease_id, stale_seconds=args.stale_seconds)
        print(json.dumps(result, ensure_ascii=True, separators=(",", ":")))
    except Exception as exc:
        print(json.dumps({"status": "error", "error": str(exc)}, ensure_ascii=True, separators=(",", ":")))
        raise SystemExit(1)


if __name__ == "__main__":
    main()
