from __future__ import annotations

from pathlib import Path

import browser_validation


def test_browser_journey_uses_semantic_actions_snapshot_and_health(tmp_path: Path):
    page = tmp_path / "journey.html"
    page.write_text(
        """<!doctype html><html><body>
        <label>Name <input aria-label="Name"></label>
        <button>Open detail</button><div id="detail"></div>
        <script>
        document.querySelector('button').onclick=()=>{
          document.querySelector('#detail').textContent='Industry: '+document.querySelector('input').value;
        };
        </script></body></html>""",
        encoding="utf-8",
    )
    data, meta, snapshot = browser_validation.capture_browser_journey(
        browser_url=page.as_uri(), width=800, height=600,
        steps=[
            {"action": "fill", "label": "Name", "value": "AI Infrastructure"},
            {"action": "click", "role": "button", "name": "Open detail"},
            {"action": "assert_text", "css": "#detail", "value": "Industry: AI Infrastructure"},
        ],
        return_snapshot=True,
        check_frontend_health=True,
    )
    assert data.startswith(b"\x89PNG")
    assert meta["interaction_status"] == "PASS"
    assert meta["frontend_health"]["status"] == "PASS"
    assert meta["browser_step_count"] == 3
    assert snapshot and "Open detail" in snapshot


def test_browser_journey_frontend_health_detects_console_error(tmp_path: Path):
    page = tmp_path / "console-error.html"
    page.write_text(
        "<!doctype html><button>Ready</button><script>console.error('SIJI_TEST_ERROR')</script>",
        encoding="utf-8",
    )
    _data, meta, _snapshot = browser_validation.capture_browser_journey(
        browser_url=page.as_uri(), width=800, height=600,
        steps=[{"action": "assert_visible", "role": "button", "name": "Ready"}],
        check_frontend_health=True,
    )
    assert meta["interaction_status"] == "PASS"
    assert meta["frontend_health"]["status"] == "FAIL"
    assert meta["frontend_health"]["console_error_count"] == 1
    assert "SIJI_TEST_ERROR" in meta["frontend_health"]["console_errors"][0]



def test_browser_journey_supports_siji_graph_drag_wheel_and_positioned_click(tmp_path: Path):
    page = tmp_path / "siji-graph.html"
    page.write_text(
        """<!doctype html><html><body>
        <svg id="map" role="img" aria-label="可拖拽缩放的二维产品关系地图"
             width="320" height="220" style="touch-action:none"><rect width="320" height="220" fill="#eef" /></svg>
        <output id="pan">pan:0,0</output><output id="zoom">wheel:0</output><output id="click">click:0,0</output>
        <script>
        const map=document.querySelector('#map'); let start=null;
        map.addEventListener('pointerdown',e=>{start=[e.clientX,e.clientY];});
        map.addEventListener('pointermove',e=>{if(start && e.buttons){
          document.querySelector('#pan').textContent=`pan:${Math.round(e.clientX-start[0])},${Math.round(e.clientY-start[1])}`;
        }});
        map.addEventListener('pointerup',()=>{start=null;});
        map.addEventListener('wheel',e=>{e.preventDefault();document.querySelector('#zoom').textContent=`wheel:${Math.round(e.deltaY)}`;},{passive:false});
        map.addEventListener('click',e=>{const r=map.getBoundingClientRect();document.querySelector('#click').textContent=`click:${Math.round(e.clientX-r.left)},${Math.round(e.clientY-r.top)}`;});
        </script></body></html>""",
        encoding="utf-8",
    )
    _data, meta, _snapshot = browser_validation.capture_browser_journey(
        browser_url=page.as_uri(), width=800, height=600,
        steps=[
            {"action": "drag", "css": "#map", "position": {"x": 120, "y": 80}, "delta_x": 90, "delta_y": 40},
            {"action": "assert_text", "css": "#pan", "value": "pan:90,40"},
            {"action": "scroll", "css": "#map", "position": {"x": 180, "y": 100}, "delta_y": -240},
            {"action": "assert_text", "css": "#zoom", "value": "wheel:-240"},
            {"action": "click", "css": "#map", "position": {"x": 30, "y": 20}},
            {"action": "assert_text", "css": "#click", "value": "click:30,20"},
        ],
        check_frontend_health=True,
    )
    assert meta["interaction_status"] == "PASS"
    assert meta["frontend_health"]["status"] == "PASS"
    assert [item["status"] for item in meta["browser_step_results"]] == ["PASS"] * 6
