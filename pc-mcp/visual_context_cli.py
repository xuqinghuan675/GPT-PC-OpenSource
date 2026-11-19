"""Fixed, bounded Agent Console -> existing Runtime reference-update bridge.

This is a one-shot request, not a service, scheduler, Job runtime or model. It
never accepts Stay commands, executable paths, shell commands or user messages.
"""
from __future__ import annotations

import base64
import json
from pathlib import Path
import sys
from typing import Any

import runtime

MAX_REQUEST_BYTES = 72 * 1024 * 1024
MAX_UPLOAD_BYTES = 50 * 1024 * 1024


def update_references(payload: dict[str, Any]) -> dict[str, Any]:
    allowed = {"lease_id", "thread_id", "workspace", "expected_revision", "keep_reference_ids", "criteria", "text_reference", "images", "depth", "journeys", "mode_enabled", "goal_enabled", "goal_outcome", "goal_success_criteria", "goal_requirements", "goal_change_note", "goal_reuse_visual", "goal_expected_revision", "ultra_enabled", "ultra_expected_revision", "watchdog_enabled"}
    if not isinstance(payload, dict) or set(payload) - allowed:
        raise ValueError("参考资料请求包含无效字段")
    identity = payload.get("lease_id")
    if not isinstance(identity, str) or len(identity) != 32 or any(c not in "0123456789abcdef" for c in identity):
        raise ValueError("任务租约标识无效")
    lease = runtime.require_execute_lease(identity, [])
    if payload.get("thread_id") != lease.get("thread_id"):
        raise runtime.RuntimePolicyError("任务身份不匹配", "参考资料只能提交到当前执行任务")
    workspace = payload.get("workspace")
    if not isinstance(workspace, str) or Path(workspace).resolve(strict=False) != Path(lease["workspace"]).resolve(strict=False):
        raise runtime.RuntimePolicyError("任务工作区不匹配", "参考资料工作区已经变化")
    if "watchdog_enabled" in payload:
        from chatgpt_agent_store import AgentStore
        from machine_config import load_machine_config

        watchdog_enabled = payload.get("watchdog_enabled")
        if type(watchdog_enabled) is not bool:
            raise ValueError("Watchdog 开关参数无效")
        forbidden = {"expected_revision", "keep_reference_ids", "criteria", "text_reference", "images", "depth", "journeys", "mode_enabled",
                     "goal_enabled", "goal_outcome", "goal_success_criteria", "goal_requirements", "goal_change_note", "goal_reuse_visual", "goal_expected_revision",
                     "ultra_enabled", "ultra_expected_revision"} & set(payload)
        if forbidden:
            raise ValueError("Watchdog 更新不能和其他模式修改混在同一次请求中")
        store = AgentStore(load_machine_config(runtime_root=str(Path(runtime.RUNTIME_DIR).parent)))
        caller = store.by_thread(str(lease.get("thread_id") or ""))
        if (lease.get("lease_role") == "participant"
                or (caller and (caller.get("parent_agent_id") or caller.get("kind") == "watchdog"
                                or caller.get("owner_thread_id") != lease.get("thread_id")))):
            raise runtime.RuntimePolicyError("Child 继承 Watchdog", "子 Agent 不能创建或关闭自己的 Watchdog；请在对应总对话操作",
                                             error_type="WATCHDOG_TOP_LEVEL_REQUIRED")
        scope = store.agent_supervisor_scope(caller["logical_agent_id"]) if caller else str(lease.get("thread_id") or "")
        current = store.watchdog_scope(scope)
        now = runtime.utc_now().timestamp()
        if watchdog_enabled:
            started_at = current.get("started_at") if current.get("manual_enabled") else now
            return store.set_watchdog_scope(scope, mode='manual', enabled=False, manual_enabled=True, manual_disabled=False,
                                            started_at=started_at or now,
                                            stopped_at=None, source="activity-console-user",
                                            root_lease_id=identity)
        return store.set_watchdog_scope(scope, mode='manual', enabled=False, manual_enabled=False, manual_disabled=True,
                                        stopped_at=now, source="activity-console-user")
    if "ultra_enabled" in payload:
        ultra_enabled = payload.get("ultra_enabled")
        ultra_expected = payload.get("ultra_expected_revision", "")
        if type(ultra_enabled) is not bool:
            raise ValueError("Ultra 模式开关参数无效")
        if not isinstance(ultra_expected, str) or len(ultra_expected) > 80:
            raise ValueError("Ultra 状态版本无效")
        forbidden = {"expected_revision", "keep_reference_ids", "criteria", "text_reference", "images", "depth", "journeys", "mode_enabled",
                     "goal_enabled", "goal_outcome", "goal_success_criteria", "goal_requirements", "goal_change_note", "goal_reuse_visual", "goal_expected_revision",
                     "watchdog_enabled"} & set(payload)
        if forbidden:
            raise ValueError("Ultra 模式更新不能和其他模式修改混在同一次请求中")
        return runtime.set_ultra_mode(identity, enabled=ultra_enabled, user_explicit=True, actor="user",
                                      expected_updated_at=ultra_expected)
    if "goal_enabled" in payload:
        goal_enabled = payload.get("goal_enabled")
        goal_outcome = payload.get("goal_outcome", "")
        goal_success_criteria = payload.get("goal_success_criteria", [])
        goal_requirements = payload.get("goal_requirements", [])
        goal_change_note = payload.get("goal_change_note", "")
        goal_reuse_visual = payload.get("goal_reuse_visual", True)
        goal_expected = payload.get("goal_expected_revision", "")
        if type(goal_enabled) is not bool or type(goal_reuse_visual) is not bool:
            raise ValueError("目标模式开关参数无效")
        if not isinstance(goal_outcome, str) or len(goal_outcome) > 8000:
            raise ValueError("目标结果最多 8000 字符")
        if not isinstance(goal_success_criteria, list):
            raise ValueError("目标成功标准必须为文本列表")
        if not isinstance(goal_requirements, list):
            raise ValueError("目标持续要求必须为文本列表")
        if not isinstance(goal_change_note, str) or len(goal_change_note) > 2000:
            raise ValueError("目标修正说明最多 2000 字符")
        if not isinstance(goal_expected, str) or len(goal_expected) > 80:
            raise ValueError("目标资料版本无效")
        forbidden = {"expected_revision", "keep_reference_ids", "criteria", "text_reference", "images", "depth", "journeys", "mode_enabled",
                     "ultra_enabled", "ultra_expected_revision", "watchdog_enabled"} & set(payload)
        if forbidden:
            raise ValueError("目标模式更新不能和视觉资料修改混在同一次请求中")
        return runtime.set_goal_mode(identity, enabled=goal_enabled, outcome=goal_outcome,
                                     success_criteria=goal_success_criteria, requirements=goal_requirements,
                                     change_note=goal_change_note, reuse_visual_context=goal_reuse_visual,
                                     expected_updated_at=goal_expected)
    expected = payload.get("expected_revision")
    if not isinstance(expected, str) or len(expected) > 80:
        raise ValueError("必须提供当前视觉资料版本")
    context = runtime.get_visual_context(identity)["visual_mode"]
    if expected != str(context.get("updated_at") or ""):
        raise runtime.RuntimePolicyError("视觉资料已更新", "请重新打开管理面板；未覆盖已有参考或要求", error_type="VISUAL_CONTEXT_CONFLICT")
    if "mode_enabled" in payload:
        mode_enabled = payload.get("mode_enabled")
        if type(mode_enabled) is not bool:
            raise ValueError("mode_enabled 必须是布尔值")
        forbidden = {"keep_reference_ids", "criteria", "text_reference", "images", "depth", "journeys"} & set(payload)
        if forbidden:
            raise ValueError("看图模式开关不能和参考资料修改混在同一次请求中")
        return runtime.set_visual_user_mode(identity, enabled=mode_enabled, expected_updated_at=expected)
    criteria = runtime._visual_text_list(payload.get("criteria", []), "criteria")
    depth = payload.get("depth")
    if depth is not None and (not isinstance(depth, str) or depth not in {"visual", "interactive", "frontend"}):
        raise ValueError("depth 仅支持 visual/interactive/frontend")
    journeys = payload.get("journeys")
    if journeys is not None:
        journeys = runtime._normalise_visual_journeys(journeys)
    keep_ids = payload.get("keep_reference_ids", [])
    if not isinstance(keep_ids, list) or len(keep_ids) > 16 or any(not isinstance(item, str) for item in keep_ids) or len(set(keep_ids)) != len(keep_ids):
        raise ValueError("保留的参考资料 ID 列表无效")
    current = {item["id"]: item for item in context.get("references", [])}
    if any(identity not in current for identity in keep_ids):
        raise ValueError("参考资料已变化或不属于当前任务")
    text = payload.get("text_reference", "")
    if not isinstance(text, str) or len(text) > 8000:
        raise ValueError("文字参考最多 8000 字符")
    import visual

    images = payload.get("images", [])
    if not isinstance(images, list) or len(images) > 16:
        raise ValueError("图片列表无效")
    if len(keep_ids) + len(images) + bool(text.strip()) > 16:
        raise ValueError("最多保存 16 项参考资料")
    references = [current[identity] for identity in keep_ids]
    if text.strip():
        import uuid
        references.append({"id": "ref-" + uuid.uuid4().hex, "kind": "text", "name": "Console 文字参考", "text": text.strip()})
    new_paths: list[Path] = []
    total = 0
    try:
        for item in images:
            if not isinstance(item, dict) or set(item) - {"name", "data_base64"}:
                raise ValueError("图片上传对象格式无效")
            encoded = item.get("data_base64")
            name = item.get("name")
            if not isinstance(name, str) or not name.strip() or len(name) > 240:
                raise ValueError("图片名称无效")
            if not isinstance(encoded, str) or len(encoded) > (visual.MAX_IMAGE_BYTES + 2) // 3 * 4:
                raise ValueError("参考图片超过 25 MB 限制")
            data = base64.b64decode(encoded, validate=True)
            total += len(data)
            if total > MAX_UPLOAD_BYTES:
                raise ValueError("本次参考图片超过 50 MB 限制")
            # Decoded format, dimensions and compressed-image limits are authoritative;
            # neither filename extension nor a client MIME assertion is trusted.
            reference = visual.persist_image(identity, "references", data, label=name)
            reference["origin"] = "console_reference"
            new_paths.append(Path(reference["path"]))
            references.append(reference)
        result = runtime.set_visual_mode(identity, action="update", references=references,
                                         criteria=criteria, depth=depth, journeys=journeys,
                                         expected_updated_at=expected)
        # Viewer reads this same lease via its existing snapshot channel. No inbox or
        # activity user-message is written, and no acknowledgement/idle gate changes.
        result["visual_mode"].pop("observations", None)
        return result
    except Exception:
        # Roll back only new unique images created by this request, never prior data.
        for path in new_paths:
            path.unlink(missing_ok=True)
        raise


def main() -> int:
    try:
        data = sys.stdin.buffer.read(MAX_REQUEST_BYTES + 1)
        if len(data) > MAX_REQUEST_BYTES:
            raise ValueError("参考资料请求超过大小限制")
        payload = json.loads(data.decode("utf-8"))
        result = update_references(payload)
        output = {"ok": True, "result": result}
        code = 0
    except (ValueError, TypeError, KeyError, OSError, runtime.RuntimePolicyError) as exc:
        output = {"ok": False, "error": str(exc)[:1500]}
        code = 1
    sys.stdout.buffer.write(json.dumps(output, ensure_ascii=False).encode("utf-8"))
    return code


if __name__ == "__main__":
    raise SystemExit(main())
