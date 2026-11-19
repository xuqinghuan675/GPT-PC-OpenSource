"""Browser DOM/interaction checks only. No screenshots and no production Console writes."""
from __future__ import annotations
import json
import sys
from pathlib import Path
from playwright.sync_api import sync_playwright

URL = "http://127.0.0.1:1431/tests/fixtures/visual-harness.html"
RESULT_FILE = Path(__file__).resolve().parents[2] / "logs" / "visual-dom-verification.json"


def run_case(browser, name: str, size: dict[str, int]) -> dict:
    context = browser.new_context(viewport=size, device_scale_factor=1, reduced_motion="reduce")
    page = context.new_page()
    errors: list[str] = []
    page.on("pageerror", lambda error: errors.append(str(error)))
    try:
        page.goto(URL, wait_until="networkidle", timeout=20000)
        page.locator('[data-testid="visual-reference-panel"]').wait_for(timeout=10000)
        assert page.locator('[data-testid="stay-mode"]').is_visible(), "Stay badge missing"
        assert page.locator('[data-testid="visual-mode"]').is_visible(), "Visual badge missing"
        assert "GPT 视觉观察" in page.locator('[data-testid="agent-observation"]').inner_text()
        assert "不是用户输入" in page.locator('[data-testid="agent-observation"]').inner_text()
        assert "参考 2" in page.locator('[data-testid="visual-reference-panel"]').inner_text()
        for selector in ['[data-image-id="ref-icon"] img', '[data-image-id="obs-fixture"] img', '[data-image-id="att-fixture"] img']:
            frame = page.locator(selector.removesuffix(' img'))
            frame.scroll_into_view_if_needed()
            page.wait_for_function("selector => { const image=document.querySelector(selector); return image?.complete && image.naturalWidth > 0; }", arg=selector, timeout=10000)
        counts = page.locator('[data-testid="conversation-transcript"] [data-conversation-item]').count()
        panel = page.locator('[data-testid="visual-reference-panel"]')
        panel.get_by_role("button", name="验收设置").click()
        panel.get_by_label("前端验收深度").select_option("frontend")
        panel.locator('[data-testid="visual-journeys"]').fill("拖拽产品地图后仍可点击产品\n搜索产品并打开研究面板")
        editor = panel.get_by_label("验收要求，每行一条")
        editor.fill("要求一\n要求二\n/stay off")
        panel.get_by_role("button", name="保存参考资料").click()
        page.wait_for_function("window.__visualHarness.calls.some(call => call.command === 'activity_visual_update' && call.args.depth === 'frontend')")
        panel.get_by_role("button", name="验收设置").wait_for()
        assert "前端验收" in panel.inner_text() and "旅程 2" in panel.inner_text()
        saved = page.evaluate("window.__visualHarness.calls.filter(call => call.command === 'activity_visual_update').at(-1).args")
        assert saved["depth"] == "frontend"
        assert [item["name"] for item in saved["journeys"]] == ["拖拽产品地图后仍可点击产品", "搜索产品并打开研究面板"]
        assert all(item["required"] is True for item in saved["journeys"])
        assert page.locator('[data-testid="conversation-transcript"] [data-conversation-item]').count() == counts, "Reference update became conversation input"
        assert page.locator('[data-testid="stay-mode"]').is_visible(), "Reference text changed Stay"
        assert page.evaluate("window.__visualHarness.calls.every(call => ['activity_image','activity_visual_update'].includes(call.command))")
        page.evaluate("window.__visualHarness.failSave('测试版本冲突，原资料未覆盖')")
        panel.get_by_role("button", name="验收设置").click()
        panel.get_by_role("button", name="保存参考资料").click()
        page.get_by_role("alert").filter(has_text="测试版本冲突").wait_for()
        panel.get_by_role("button", name="取消").click()
        page.evaluate("window.__visualHarness.failSave('')")
        # Native keyboard semantics: panel toggle works through Enter, not bespoke key handlers.
        toggle = panel.get_by_role("button", name="任务控制", exact=True)
        toggle.focus()
        toggle.press("Enter")
        assert toggle.get_attribute("aria-expanded") == "false"
        toggle.press("Enter")
        assert toggle.get_attribute("aria-expanded") == "true"
        # Real existing components for command/diff rows still expand after native-button refactor.
        for test_id, expanded in [("terminal-tool-row", "terminal-block"), ("diff-tool-row", "diff-block")]:
            disclosure = page.locator(f'[data-testid="{test_id}"] [data-disclosure-row]')
            disclosure.scroll_into_view_if_needed()
            disclosure.press("Enter")
            page.locator(f'[data-testid="{expanded}"]').wait_for(timeout=5000)
        geometry = page.evaluate("""() => {
          const root=document.querySelector('[data-testid="dsh-conversation-root"]');
          const composer=document.querySelector('[data-testid="ui-composer"]');
          const panel=document.querySelector('[data-testid="visual-reference-panel"]');
          const rect=node=>{const r=node.getBoundingClientRect();return {x:r.x,y:r.y,width:r.width,height:r.height,right:r.right,bottom:r.bottom};};
          return {viewport:{width:innerWidth,height:innerHeight},documentWidth:document.documentElement.scrollWidth,
            root:rect(root),composer:rect(composer),panel:rect(panel),
            composerTag:document.querySelector('[data-testid="composer"]').tagName};
        }""")
        assert geometry['documentWidth'] <= size['width'] + 1, f"Horizontal overflow: {geometry}"
        assert geometry['root']['right'] <= size['width'] + 1, f"Root clipping: {geometry}"
        assert geometry['composer']['bottom'] <= size['height'] + 1, f"Composer clipped: {geometry}"
        assert geometry['composer']['height'] >= 60
        assert geometry['composerTag'] == 'FIELDSET'
        page.evaluate("window.__visualHarness.completeVisual()")
        page.wait_for_function("!document.querySelector('[data-testid=visual-mode]')")
        assert page.locator('[data-testid="stay-mode"]').is_visible(), "Visual completion cleared Stay"
        assert page.locator('[data-testid="dsh-conversation-root"]').get_attribute("data-lifecycle") == "active"
        page.evaluate("window.__visualHarness.disconnectHeartbeat()")
        page.wait_for_function("document.querySelector('[data-testid=agent-status]')?.textContent.includes('断开')")
        assert page.locator('[data-testid="stay-mode"]').is_visible()
        assert not errors, errors
        return {"case": name, "status": "PASS", "geometry": geometry, "console_errors": errors,
                "scope": "DOM and mocked native IPC only; no screenshot or visual-fidelity acceptance"}
    except Exception as exc:
        details = page.evaluate("""() => ({title:document.title,width:innerWidth,scrollWidth:document.documentElement.scrollWidth,
          body:document.body.innerText.slice(0,1600),requests:window.__visualHarness?.calls?.slice(-6)})""")
        return {"case": name, "status": "FAIL", "error": str(exc), "console_errors": errors, "details": details}
    finally:
        context.close()


def main() -> int:
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(channel="msedge", headless=True)
        try:
            results = [run_case(browser,"desktop",{"width":1440,"height":900}),
                       run_case(browser,"mobile",{"width":390,"height":844})]
        finally:
            browser.close()
    RESULT_FILE.parent.mkdir(parents=True, exist_ok=True)
    RESULT_FILE.write_text(json.dumps(results, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(results, ensure_ascii=False, indent=2))
    return 0 if all(item["status"] == "PASS" for item in results) else 1


if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8")
    raise SystemExit(main())
