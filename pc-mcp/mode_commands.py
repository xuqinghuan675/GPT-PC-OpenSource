"""Pure mode-command grammar shared by Console and MCP; no identity or I/O."""
from __future__ import annotations

import math
import re
from typing import Any

_ENTER = re.compile(
    r"^/stay\s+(?P<value>\d+(?:\.\d+)?)\s*(?P<unit>h|hr|hrs|hour|hours|小时|时|m|min|mins|minute|minutes|分钟|分|s|sec|secs|second|seconds|秒)$", re.I)
_EXIT = re.compile(
    r"^(?:/stay\s+off|(?:(?:请|现在|立即|马上|我(?:现在)?(?:明确|强烈)?要求)\s*)?退出驻场模式[！!。.]*)$", re.I)


def parse_stay_command(text: str) -> dict[str, Any] | None:
    clean = text.strip()
    if _EXIT.fullmatch(clean):
        return {"action": "exit", "raw": clean}
    if not clean.lower().startswith("/stay"):
        return None
    invalid = {"action": "invalid", "raw": clean,
               "error": "驻场命令格式：/stay 3h、/stay 2m、/stay 24h；时长 10 秒至 24 小时；退出仅可信 Console /stay off"}
    match = _ENTER.fullmatch(clean)
    if not match:
        return invalid
    unit = match.group("unit").lower()
    multiplier = 3600 if unit in {"h", "hr", "hrs", "hour", "hours", "小时", "时"} else 60 if unit in {"m", "min", "mins", "minute", "minutes", "分钟", "分"} else 1
    value = float(match.group("value")) * multiplier
    if not math.isfinite(value) or value < 10 or value > 86400:
        return invalid
    return {"action": "enter", "raw": clean, "duration_seconds": round(value)}


def parse_visual_duration(command: str) -> int:
    """/vision uses the duration grammar only; off never implies completion."""
    if not re.match(r"^/vision\s+", command.strip(), flags=re.I):
        raise ValueError("看图命令必须以 /vision 开头")
    parsed = parse_stay_command(re.sub(r"^/vision\b", "/stay", command.strip(), flags=re.I))
    if not parsed or parsed.get("action") != "enter":
        raise ValueError("看图命令格式：/vision 3h；完成必须经过视觉验收")
    return int(parsed["duration_seconds"])
