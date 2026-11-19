"""Native-input subcommand adapter for the machine-wide aiwork command.

Keeps agentmouse behind the existing aiwork entry point. No new browser
profiles, no server restart, no global keyboard/mouse injection.
"""
from __future__ import annotations
import json
from pathlib import Path
import subprocess
import sys

HERE = Path(__file__).resolve().parent
ENTRY = HERE / "agentmouse.py"
ALLOWED = {"status", "inspect", "invoke", "get-value", "set-value", "toggle", "select"}


def main(argv: list[str] | None = None) -> int:
    args = list(sys.argv[1:] if argv is None else argv)
    if not args or args[0] not in ALLOWED:
        print(json.dumps({
            "ok":False, "error":"SUPPORTED_NATIVE_ACTIONS: "+", ".join(sorted(ALLOWED))
        }))
        return 2
    if not ENTRY.is_file():
        print(json.dumps({"ok":False,"error":"NATIVE_INPUT_ENTRY_MISSING"}))
        return 2
    try:
        completed = subprocess.run(
            [sys.executable, str(ENTRY), *args],
            capture_output=True, text=True, encoding="utf-8", errors="replace",
            cwd=str(HERE), timeout=28,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
        # Match the aiwork public response shape. Native failures keep a
        # nonzero return code and are never converted to success.
        try:
            payload=json.loads(completed.stdout)
        except ValueError:
            payload={"ok":False,"error":"INVALID_NATIVE_JSON_RESPONSE"}
        if completed.returncode==0 and isinstance(payload,dict) and payload.get("ok"):
            print(json.dumps({"ok":True,"result":payload},ensure_ascii=True))
            return 0
        error=payload.get("error") if isinstance(payload,dict) else None
        error=error or (completed.stderr or "NATIVE_INPUT_FAILED")
        print(json.dumps({"ok":False,"error":str(error)[:500]},ensure_ascii=True))
        return 2
    except subprocess.TimeoutExpired:
        print(json.dumps({
            "ok":False,
            "error":"NATIVE_INPUT_TIMEOUT; UNKNOWN_OUTCOME; INSPECT_BEFORE_RETRY",
        }))
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
