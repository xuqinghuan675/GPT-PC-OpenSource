"""Lightweight deterministic browser journey acceptance for Visual Mode.

This module deliberately stays inside the existing Runtime/tool call. It is not
another browser Agent, MCP server, scheduler, or resident daemon. One call owns
one short-lived Playwright context, performs a bounded user journey, captures
structured health evidence, and returns the final real screenshot.
"""
from __future__ import annotations

import re
from typing import Any
from urllib.parse import urlsplit, urlunsplit

from playwright.sync_api import Locator, Page, sync_playwright

MAX_STEPS = 32
MAX_EVENT_ITEMS = 8
MAX_EVENT_TEXT = 500
MAX_SNAPSHOT_CHARS = 12_000
_SUPPORTED_ACTIONS = frozenset({
    "goto", "click", "double_click", "fill", "press", "hover", "scroll", "drag", "select",
    "check", "uncheck", "wait", "back", "forward", "reload",
    "assert_visible", "assert_hidden", "assert_text", "assert_value", "assert_url",
    "assert_checked",
})


def _bounded_text(value: Any, limit: int = MAX_EVENT_TEXT) -> str:
    clean = str(value or "").replace("\x00", "").strip()
    return clean[:limit]


def _safe_url(value: str) -> str:
    """Return navigation context without credentials, query tokens, or fragments."""
    try:
        parsed = urlsplit(value)
    except ValueError:
        return ""
    hostname = parsed.hostname or ""
    port = f":{parsed.port}" if parsed.port else ""
    netloc = f"{hostname}{port}" if hostname else ""
    if parsed.scheme == "file":
        return urlunsplit((parsed.scheme, parsed.netloc, parsed.path, "", ""))[:1000]
    return urlunsplit((parsed.scheme, netloc, parsed.path, "", ""))[:1000]


def _origin(value: str) -> tuple[str, str, int | None]:
    try:
        parsed = urlsplit(value)
        return parsed.scheme.lower(), (parsed.hostname or "").lower(), parsed.port
    except ValueError:
        return "", "", None


def _same_origin(first: str, second: str) -> bool:
    first_origin = _origin(first)
    second_origin = _origin(second)
    if first_origin[0] == "file" and second_origin[0] == "file":
        return True
    return bool(first_origin[0] and first_origin == second_origin)


def _validate_url(value: Any, name: str = "url") -> str:
    clean = str(value or "").strip()
    if not clean or len(clean) > 4096 or not clean.lower().startswith(("http://", "https://", "file://")):
        raise ValueError(f"{name} 必须是有效的 http/https/file URL")
    return clean


def _int(value: Any, name: str, minimum: int, maximum: int) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or not minimum <= value <= maximum:
        raise ValueError(f"{name} 必须是 {minimum}..{maximum} 的整数")
    return value


def _position(step: dict[str, Any]) -> dict[str, int] | None:
    raw = step.get("position")
    if raw is None:
        return None
    if not isinstance(raw, dict) or set(raw) != {"x", "y"}:
        raise ValueError("step.position 必须是 {x, y} 目标内相对坐标")
    return {
        "x": _int(raw.get("x"), "step.position.x", 0, 100_000),
        "y": _int(raw.get("y"), "step.position.y", 0, 100_000),
    }


def _has_locator(step: dict[str, Any]) -> bool:
    return any(isinstance(step.get(key), str) and bool(str(step.get(key)).strip())
               for key in ("role", "label", "testid", "placeholder", "text", "css"))


def _locator(page: Page, step: dict[str, Any]) -> Locator:
    """Prefer user-visible semantics; CSS is an explicit last-resort fallback."""
    role = step.get("role")
    name = step.get("name")
    if isinstance(role, str) and role.strip():
        clean_name = name.strip() if isinstance(name, str) and name.strip() else None
        return page.get_by_role(role.strip(), name=clean_name, exact=bool(step.get("exact", False)))
    label = step.get("label")
    if isinstance(label, str) and label.strip():
        return page.get_by_label(label.strip(), exact=bool(step.get("exact", False)))
    testid = step.get("testid")
    if isinstance(testid, str) and testid.strip():
        return page.get_by_test_id(testid.strip())
    placeholder = step.get("placeholder")
    if isinstance(placeholder, str) and placeholder.strip():
        return page.get_by_placeholder(placeholder.strip(), exact=bool(step.get("exact", False)))
    text = step.get("text")
    if isinstance(text, str) and text.strip():
        return page.get_by_text(text.strip(), exact=bool(step.get("exact", False)))
    css = step.get("css")
    if isinstance(css, str) and css.strip() and len(css) <= 1000:
        return page.locator(css.strip())
    raise ValueError("该步骤需要 locator：优先 role+name / label / testid / placeholder / text，必要时才用 css")


def _expect_text(locator: Locator, expected: str) -> None:
    actual = locator.inner_text(timeout=5_000)
    if expected not in actual:
        raise AssertionError(f"文本断言失败：期望包含 {expected!r}，实际 {_bounded_text(actual, 240)!r}")


def _run_step(page: Page, step: dict[str, Any], timeout_ms: int) -> dict[str, Any]:
    if not isinstance(step, dict):
        raise ValueError("browser_steps 每项必须是对象")
    action = str(step.get("action") or "").strip().lower()
    if action not in _SUPPORTED_ACTIONS:
        raise ValueError(f"不支持的 browser action：{action or '(empty)'}")
    step_timeout = step.get("timeout_ms", min(timeout_ms, 10_000))
    step_timeout = _int(step_timeout, "step.timeout_ms", 100, 60_000)
    result: dict[str, Any] = {"action": action, "status": "PASS"}

    if action == "goto":
        response = page.goto(_validate_url(step.get("url"), "step.url"), wait_until="domcontentloaded", timeout=step_timeout)
        result["http_status"] = response.status if response is not None else None
        result["url"] = _safe_url(page.url)
        return result
    if action == "back":
        page.go_back(wait_until="domcontentloaded", timeout=step_timeout); return result
    if action == "forward":
        page.go_forward(wait_until="domcontentloaded", timeout=step_timeout); return result
    if action == "reload":
        page.reload(wait_until="domcontentloaded", timeout=step_timeout); return result
    if action == "wait":
        milliseconds = _int(step.get("ms", 250), "step.ms", 0, 10_000)
        page.wait_for_timeout(milliseconds); result["ms"] = milliseconds; return result
    if action == "scroll":
        delta_x = _int(step.get("delta_x", 0), "step.delta_x", -100_000, 100_000)
        delta_y = _int(step.get("delta_y", 600), "step.delta_y", -100_000, 100_000)
        position = _position(step)
        if _has_locator(step):
            _locator(page, step).hover(position=position, timeout=step_timeout)
        elif position is not None:
            raise ValueError("带 position 的 scroll 必须同时提供目标 locator")
        page.mouse.wheel(delta_x, delta_y); result.update(delta_x=delta_x, delta_y=delta_y); return result
    if action == "assert_url":
        expected = _bounded_text(step.get("value"), 1000)
        if not expected:
            raise ValueError("assert_url 需要 value")
        if expected not in page.url:
            raise AssertionError(f"URL 断言失败：期望包含 {expected!r}，实际 {_safe_url(page.url)!r}")
        result["url"] = _safe_url(page.url); return result

    locator = _locator(page, step)
    position = _position(step)
    if action == "click": locator.click(position=position, timeout=step_timeout)
    elif action == "double_click": locator.dblclick(position=position, timeout=step_timeout)
    elif action == "fill": locator.fill(str(step.get("value") or ""), timeout=step_timeout)
    elif action == "press":
        key = _bounded_text(step.get("key"), 80)
        if not key: raise ValueError("press 需要 key")
        locator.press(key, timeout=step_timeout)
    elif action == "hover": locator.hover(position=position, timeout=step_timeout)
    elif action == "drag":
        delta_x = _int(step.get("delta_x", 0), "step.delta_x", -100_000, 100_000)
        delta_y = _int(step.get("delta_y", 0), "step.delta_y", -100_000, 100_000)
        move_steps = _int(step.get("steps", 8), "step.steps", 1, 100)
        box = locator.bounding_box(timeout=step_timeout)
        if box is None:
            raise AssertionError("拖拽目标当前没有可交互边界")
        start_x = box["x"] + (position["x"] if position is not None else box["width"] / 2)
        start_y = box["y"] + (position["y"] if position is not None else box["height"] / 2)
        if not (box["x"] <= start_x <= box["x"] + box["width"] and box["y"] <= start_y <= box["y"] + box["height"]):
            raise ValueError("drag.position 必须位于目标元素内部")
        page.mouse.move(start_x, start_y)
        page.mouse.down(button="left")
        try:
            page.mouse.move(start_x + delta_x, start_y + delta_y, steps=move_steps)
        finally:
            page.mouse.up(button="left")
        result.update(delta_x=delta_x, delta_y=delta_y, steps=move_steps)
    elif action == "select":
        value = step.get("value")
        if not isinstance(value, (str, list)): raise ValueError("select 需要字符串或字符串列表 value")
        if isinstance(value, list) and any(not isinstance(item, str) for item in value): raise ValueError("select value 列表只能包含字符串")
        locator.select_option(value=value, timeout=step_timeout)
    elif action == "check": locator.check(timeout=step_timeout)
    elif action == "uncheck": locator.uncheck(timeout=step_timeout)
    elif action == "assert_visible":
        if not locator.is_visible(timeout=step_timeout): raise AssertionError("元素应可见，但当前不可见")
    elif action == "assert_hidden":
        if locator.is_visible(timeout=step_timeout): raise AssertionError("元素应隐藏，但当前仍可见")
    elif action == "assert_text":
        expected = str(step.get("value") or "")
        if not expected: raise ValueError("assert_text 需要 value")
        _expect_text(locator, expected)
    elif action == "assert_value":
        expected = str(step.get("value") or "")
        actual = locator.input_value(timeout=step_timeout)
        if actual != expected: raise AssertionError(f"值断言失败：期望 {expected!r}，实际 {_bounded_text(actual, 240)!r}")
    elif action == "assert_checked":
        expected = step.get("value", True)
        if type(expected) is not bool: raise ValueError("assert_checked.value 必须是布尔值")
        actual = locator.is_checked(timeout=step_timeout)
        if actual != expected: raise AssertionError(f"checked 断言失败：期望 {expected}，实际 {actual}")
    return result


def capture_browser_journey(
    *,
    browser_url: str,
    width: int = 1440,
    height: int = 900,
    full_page: bool = False,
    timeout_ms: int = 20_000,
    steps: list[dict[str, Any]] | None = None,
    return_snapshot: bool = False,
    check_frontend_health: bool = False,
) -> tuple[bytes, dict[str, Any], str | None]:
    """Execute one bounded journey and capture its final real browser state."""
    url = _validate_url(browser_url, "browser_url")
    width = _int(width, "width", 200, 4096)
    height = _int(height, "height", 200, 4096)
    timeout_ms = _int(timeout_ms, "timeout_ms", 1_000, 60_000)
    raw_steps = steps or []
    if not isinstance(raw_steps, list) or len(raw_steps) > MAX_STEPS:
        raise ValueError(f"browser_steps 最多 {MAX_STEPS} 项")

    page_errors: list[str] = []
    console_errors: list[str] = []
    response_errors: list[dict[str, Any]] = []
    request_failures: list[dict[str, Any]] = []
    step_results: list[dict[str, Any]] = []
    initial_origin_url = url
    initial_http_status: int | None = None
    snapshot: str | None = None

    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(channel="msedge", headless=True)
        try:
            context = browser.new_context(
                viewport={"width": width, "height": height},
                device_scale_factor=1,
                reduced_motion="reduce",
            )
            try:
                page = context.new_page()
                page.on("pageerror", lambda error: page_errors.append(_bounded_text(error)))
                page.on("console", lambda message: console_errors.append(_bounded_text(message.text)) if message.type == "error" else None)

                def on_response(response):
                    try:
                        request = response.request
                        if response.status >= 500 and request.resource_type in {"document", "xhr", "fetch"} and _same_origin(initial_origin_url, response.url):
                            response_errors.append({"status": response.status, "resource_type": request.resource_type, "url": _safe_url(response.url)})
                    except Exception:
                        return

                def on_request_failed(request):
                    try:
                        if request.resource_type in {"document", "xhr", "fetch"} and _same_origin(initial_origin_url, request.url):
                            request_failures.append({"resource_type": request.resource_type, "url": _safe_url(request.url), "error": _bounded_text(request.failure)})
                    except Exception:
                        return

                page.on("response", on_response)
                page.on("requestfailed", on_request_failed)
                response = page.goto(url, wait_until="domcontentloaded", timeout=timeout_ms)
                initial_http_status = response.status if response is not None else None
                try:
                    page.wait_for_load_state("networkidle", timeout=min(3_000, timeout_ms))
                except Exception:
                    pass

                for index, raw_step in enumerate(raw_steps):
                    try:
                        result = _run_step(page, raw_step, timeout_ms)
                    except Exception as exc:
                        step_results.append({"index": index, "action": _bounded_text(raw_step.get("action") if isinstance(raw_step, dict) else "", 80), "status": "FAIL", "error": _bounded_text(exc)})
                        break
                    step_results.append({"index": index, **result})

                if return_snapshot:
                    snapshot = page.aria_snapshot(mode="ai", boxes=True, timeout=min(5_000, timeout_ms))[:MAX_SNAPSHOT_CHARS]
                data = page.screenshot(type="png", full_page=bool(full_page), animations="disabled")
                current_url = page.url
                page_title = page.title()[:240]
            finally:
                context.close()
        finally:
            browser.close()

    failed_steps = sum(1 for item in step_results if item.get("status") != "PASS")
    interaction_status = "PASS" if raw_steps and failed_steps == 0 and len(step_results) == len(raw_steps) else ("NOT_RUN" if not raw_steps else "FAIL")
    health = {
        "status": "PASS",
        "page_error_count": len(page_errors),
        "console_error_count": len(console_errors),
        "server_error_count": len(response_errors),
        "request_failure_count": len(request_failures),
        "page_errors": page_errors[:MAX_EVENT_ITEMS],
        "console_errors": console_errors[:MAX_EVENT_ITEMS],
        "server_errors": response_errors[:MAX_EVENT_ITEMS],
        "request_failures": request_failures[:MAX_EVENT_ITEMS],
    }
    if check_frontend_health and any((page_errors, console_errors, response_errors, request_failures)):
        health["status"] = "FAIL"
    elif not check_frontend_health:
        health["status"] = "NOT_ENFORCED"

    capture = {
        "capture_type": "browser",
        "browser_url": _safe_url(current_url),
        "page_title": page_title,
        "http_status": initial_http_status,
        "viewport_width": width,
        "viewport_height": height,
        "full_page": bool(full_page),
        "interaction_status": interaction_status,
        "browser_step_count": len(raw_steps),
        "browser_step_results": step_results,
        "frontend_health": health,
    }
    return data, capture, snapshot
