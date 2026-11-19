//! Fixed one-shot reference update bridge to the existing Python Runtime.
//! No shell, configurable executable, user-message insertion or Stay controls.
use serde_json::Value;
use std::{io::{Read, Write}, path::Path, process::{Command, Stdio}, thread, time::{Duration, Instant}};

const MAX_REQUEST: usize = 72 * 1024 * 1024;
const MAX_RESPONSE: u64 = 2 * 1024 * 1024;

fn validate(payload: &Value) -> Result<Vec<u8>, String> {
    let object = payload.as_object().ok_or("视觉资料请求必须为对象")?;
    let allowed = ["lease_id", "thread_id", "workspace", "expected_revision", "keep_reference_ids", "criteria", "text_reference", "images", "depth", "journeys", "mode_enabled", "goal_enabled", "goal_outcome", "goal_success_criteria", "goal_requirements", "goal_change_note", "goal_reuse_visual", "goal_expected_revision", "ultra_enabled", "ultra_expected_revision", "watchdog_enabled"];
    if object.keys().any(|key| !allowed.contains(&key.as_str())) { return Err("视觉资料请求包含未知字段".into()); }
    let task = object.get("lease_id").and_then(Value::as_str).ok_or("缺少任务 ID")?;
    if !super::valid_task_id(task) { return Err("任务 ID 无效".into()); }
    if object.contains_key("watchdog_enabled") {
        if object.get("watchdog_enabled").and_then(Value::as_bool).is_none() { return Err("Watchdog 开关必须是布尔值".into()); }
        if object.keys().any(|key| !matches!(key.as_str(), "lease_id" | "thread_id" | "workspace" | "watchdog_enabled")) {
            return Err("Watchdog 更新不能和其他模式修改混在同一次请求中".into());
        }
    } else if object.contains_key("ultra_enabled") {
        if object.get("ultra_enabled").and_then(Value::as_bool).is_none() { return Err("Ultra 模式开关必须是布尔值".into()); }
        if object.get("ultra_expected_revision").and_then(Value::as_str).is_none_or(|value| value.len() > 80) { return Err("Ultra 状态版本无效".into()); }
        if object.keys().any(|key| !matches!(key.as_str(), "lease_id" | "thread_id" | "workspace" | "ultra_enabled" | "ultra_expected_revision")) {
            return Err("Ultra 模式更新不能和其他模式修改混在同一次请求中".into());
        }
    } else if object.contains_key("goal_enabled") {
        if object.get("goal_enabled").and_then(Value::as_bool).is_none() { return Err("目标模式开关必须是布尔值".into()); }
        if object.get("goal_reuse_visual").and_then(Value::as_bool).is_none() { return Err("目标模式视觉复用开关必须是布尔值".into()); }
        if object.get("goal_outcome").and_then(Value::as_str).is_none_or(|value| value.len() > 8000) { return Err("目标结果最多 8000 字符".into()); }
        if object.get("goal_expected_revision").and_then(Value::as_str).is_none_or(|value| value.len() > 80) { return Err("目标资料版本无效".into()); }
        let criteria = object.get("goal_success_criteria").and_then(Value::as_array).ok_or("目标成功标准必须为列表")?;
        if criteria.len() > 64 || criteria.iter().any(|item| item.as_str().is_none_or(|value| value.len() > 2000)) {
            return Err("目标成功标准最多 64 条，每条最多 2000 字符".into());
        }
        if ["expected_revision", "keep_reference_ids", "criteria", "text_reference", "images", "depth", "journeys", "mode_enabled", "ultra_enabled", "ultra_expected_revision", "watchdog_enabled"].iter().any(|key| object.contains_key(*key)) {
            return Err("目标模式更新不能和视觉资料修改混在同一次请求中".into());
        }
    } else if object.contains_key("mode_enabled") {
        if object.get("mode_enabled").and_then(Value::as_bool).is_none() { return Err("看图模式开关必须是布尔值".into()); }
        if ["keep_reference_ids", "criteria", "text_reference", "images", "depth", "journeys"].iter().any(|key| object.contains_key(*key)) {
            return Err("看图模式开关不能和参考资料修改混在同一次请求中".into());
        }
    } else {
        if let Some(depth) = object.get("depth") {
            let depth = depth.as_str().ok_or("验收深度必须是文本")?;
            if !matches!(depth, "visual" | "interactive" | "frontend") { return Err("验收深度仅支持 visual/interactive/frontend".into()); }
        }
        if let Some(journeys) = object.get("journeys") {
            let journeys = journeys.as_array().ok_or("交互 Journey 必须为列表")?;
            if journeys.len() > 12 { return Err("交互 Journey 最多 12 条".into()); }
            for journey in journeys {
                let row = journey.as_object().ok_or("每条 Journey 必须是对象")?;
                if row.keys().any(|key| !matches!(key.as_str(), "id" | "name" | "required")) { return Err("Journey 包含未知字段".into()); }
                let id = row.get("id").and_then(Value::as_str).ok_or("Journey 缺少 id")?;
                if id.is_empty() || id.len() > 80 || !id.bytes().all(|byte| byte.is_ascii_alphanumeric() || matches!(byte, b'.' | b'_' | b'-')) { return Err("Journey id 无效".into()); }
                let name = row.get("name").and_then(Value::as_str).ok_or("Journey 缺少 name")?;
                if name.trim().is_empty() || name.chars().count() > 160 { return Err("Journey name 最多 160 字符".into()); }
                if row.get("required").and_then(Value::as_bool).is_none() { return Err("Journey required 必须是布尔值".into()); }
            }
        }
        if object.get("images").and_then(Value::as_array).is_none_or(|values| values.len() > 16) {
            return Err("最多 16 张参考图片".into());
        }
    }
    let bytes = serde_json::to_vec(payload).map_err(|error| error.to_string())?;
    if bytes.len() > MAX_REQUEST { return Err("视觉资料请求超过大小限制".into()); }
    Ok(bytes)
}

pub fn update(runtime_root: &Path, payload: Value) -> Result<Value, String> {
    let bytes = validate(&payload)?;
    let python = runtime_root.join("pc-mcp.venv").join("Scripts").join("python.exe");
    let script = runtime_root.join("pc-mcp").join("visual_context_cli.py");
    if !python.is_file() || !script.is_file() { return Err("本机视觉资料接口尚未安装，无法保存".into()); }
    let mut command = Command::new(python);
    command.arg(script).current_dir(runtime_root.join("pc-mcp"))
        .env("PYTHONIOENCODING", "utf-8").env("PYTHONUTF8", "1")
        .stdin(Stdio::piped()).stdout(Stdio::piped()).stderr(Stdio::piped());
    #[cfg(windows)] {
        use std::os::windows::process::CommandExt;
        command.creation_flags(0x08000000); // Short IPC request must not flash a console window.
    }
    let mut child = command.spawn().map_err(|error| format!("无法启动本机参考资料接口：{error}"))?;
    let mut stdin = child.stdin.take().ok_or("无法写入参考资料请求")?;
    let stdout = child.stdout.take().ok_or("无法读取参考资料响应")?;
    let stderr = child.stderr.take().ok_or("无法读取参考资料错误")?;
    let writer = thread::spawn(move || stdin.write_all(&bytes));
    let reader = thread::spawn(move || {
        let mut bytes = Vec::new();
        stdout.take(MAX_RESPONSE + 1).read_to_end(&mut bytes).map(|_| bytes)
    });
    let errors = thread::spawn(move || {
        let mut bytes = Vec::new();
        stderr.take(65537).read_to_end(&mut bytes).map(|_| bytes)
    });
    let started = Instant::now();
    let status = loop {
        match child.try_wait() {
            Ok(Some(status)) => break Ok(status),
            Ok(None) if started.elapsed() < Duration::from_secs(20) => thread::sleep(Duration::from_millis(25)),
            Ok(None) => { let _ = child.kill(); let _ = child.wait(); break Err("保存参考资料超时；请检查本机 Runtime 状态".to_string()); }
            Err(error) => { let _ = child.kill(); let _ = child.wait(); break Err(format!("参考资料请求失败：{error}")); }
        }
    };
    let write_result = writer.join().map_err(|_| "参考资料写入线程异常".to_string())?;
    let output = reader.join().map_err(|_| "参考资料响应线程异常".to_string())?.map_err(|error| error.to_string())?;
    let error_output = errors.join().map_err(|_| "参考资料错误读取异常".to_string())?.map_err(|error| error.to_string())?;
    let status = status?;
    if output.len() as u64 > MAX_RESPONSE { return Err("参考资料接口响应超过大小限制".into()); }
    if output.is_empty() {
        write_result.map_err(|error| format!("无法发送参考资料：{error}"))?;
        return Err(format!("参考资料接口未返回结果：{}", String::from_utf8_lossy(&error_output).chars().take(1000).collect::<String>()));
    }
    let result: Value = serde_json::from_slice(&output).map_err(|_| "参考资料接口返回了无效响应".to_string())?;
    if !status.success() || result.get("ok").and_then(Value::as_bool) != Some(true) {
        return Err(result.get("error").and_then(Value::as_str).unwrap_or("保存参考资料失败").to_string());
    }
    Ok(result.get("result").cloned().unwrap_or(Value::Null))
}

#[cfg(test)]
mod tests {
    use super::*;
    #[test]
    fn reference_bridge_rejects_control_or_executable_fields() {
        let good = serde_json::json!({"lease_id":"11111111111111111111111111111111", "images":[]});
        assert!(validate(&good).is_ok());
        let mut bad = good.clone(); bad["command"] = Value::String("/stay off".into());
        assert!(validate(&bad).is_err());
        let mut bad = good; bad["executable"] = Value::String("arbitrary.exe".into());
        assert!(validate(&bad).is_err());
    }

    #[test]
    fn visual_acceptance_update_validates_depth_and_user_journeys() {
        let good = serde_json::json!({
            "lease_id":"11111111111111111111111111111111", "expected_revision":"",
            "keep_reference_ids":[], "criteria":[], "text_reference":"", "images":[],
            "depth":"frontend", "journeys":[{"id":"user-abc-1", "name":"搜索产品并打开研究面板", "required":true}]
        });
        assert!(validate(&good).is_ok());
        let mut bad_depth = good.clone(); bad_depth["depth"] = Value::String("agent".into());
        assert!(validate(&bad_depth).is_err());
        let mut bad_id = good; bad_id["journeys"][0]["id"] = Value::String("中文 id".into());
        assert!(validate(&bad_id).is_err());
    }

    #[test]
    fn goal_update_is_bounded_and_cannot_mix_with_visual_mutation() {
        let goal = serde_json::json!({
            "lease_id":"11111111111111111111111111111111",
            "goal_enabled":true,
            "goal_outcome":"Ship the verified result",
            "goal_success_criteria":["tests pass", "real flow passes"],
            "goal_reuse_visual":true,
            "goal_expected_revision":""
        });
        assert!(validate(&goal).is_ok());
        let mut mixed = goal.clone();
        mixed["images"] = serde_json::json!([]);
        assert!(validate(&mixed).is_err());
        let mut invalid = goal;
        invalid["goal_success_criteria"] = serde_json::json!(["x".repeat(2001)]);
        assert!(validate(&invalid).is_err());
    }

    #[test]
    fn ultra_and_watchdog_updates_are_bounded_fixed_bridge_calls() {
        let ultra = serde_json::json!({
            "lease_id":"11111111111111111111111111111111",
            "thread_id":"thread-top",
            "workspace":"D:\\Temp\\task",
            "ultra_enabled":true,
            "ultra_expected_revision":"2026-09-11T00:00:00Z"
        });
        assert!(validate(&ultra).is_ok());

        let watchdog = serde_json::json!({
            "lease_id":"11111111111111111111111111111111",
            "thread_id":"thread-top",
            "workspace":"D:\\Temp\\task",
            "watchdog_enabled":true
        });
        assert!(validate(&watchdog).is_ok());

        let mut mixed = watchdog.clone();
        mixed["ultra_enabled"] = Value::Bool(true);
        assert!(validate(&mixed).is_err());

        let mut invalid_ultra = ultra;
        invalid_ultra["ultra_expected_revision"] = Value::String("x".repeat(81));
        assert!(validate(&invalid_ultra).is_err());
    }
}
