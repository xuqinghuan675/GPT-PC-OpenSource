#![cfg_attr(not(debug_assertions), windows_subsystem = "windows")]

mod activity_history;
mod visual_images;
mod visual_references;

use base64::{engine::general_purpose::STANDARD as BASE64_STANDARD, Engine as _};

use std::{
    collections::HashSet,
    fs::{self, File, OpenOptions},
    io::{BufRead, BufReader, Read, Seek, SeekFrom, Write},
    path::{Path, PathBuf},
    sync::atomic::{AtomicU64, Ordering},
    thread,
    time::{Duration, SystemTime, UNIX_EPOCH},
};

const ACTIVITY_DATA_DIR: &str = r"C:\GPT-PC\chatgpt-ui-data";
const ACTIVITY_EVENT_FILE: &str = r"C:\GPT-PC\chatgpt-ui-data\events.jsonl";
const CONTROLLER_ARCHIVE_DIR: &str = r"C:\GPT-PC\runtime\controllers";
const AGENT_RELATIONS_FILE: &str = r"C:\GPT-PC\chatgpt-agent-data\agent-relations.json";
const CONTROL_INPUT_FILE: &str = r"C:\GPT-PC\chatgpt-ui-data\ui-inbox.jsonl";
const CONTROL_LOCK_FILE: &str = r"C:\GPT-PC\chatgpt-ui-data\.ui-inbox.lock";
const MEDIA_DATA_DIR: &str = r"C:\GPT-PC\media";
const ATTACHMENT_DATA_DIR: &str = r"C:\GPT-PC\media\attachments";
const RUNTIME_LEASE_DIR: &str = r"C:\GPT-PC\runtime\leases";
const HISTORY_BYTES: u64 = 64 * 1024 * 1024;
const CONTROLLER_INDEX_MAX_AGE: Duration = Duration::from_secs(7 * 24 * 60 * 60);
const DEFAULT_READ_BYTES: usize = 192 * 1024;
const MAX_READ_BYTES: usize = 256 * 1024;
const MAX_INPUT_TEXT_CHARS: usize = 8_000;
const MAX_ATTACHMENTS: usize = 8;
const MAX_ATTACHMENT_BYTES: usize = 25 * 1024 * 1024;
const MAX_ATTACHMENT_TOTAL_BYTES: usize = 50 * 1024 * 1024;
const CONTROL_MAX_BYTES: u64 = 8 * 1024 * 1024;
const CONTROL_RETAIN_BYTES: u64 = 4 * 1024 * 1024;
const CONTROL_LOCK_WAIT_MS: u64 = 2_000;
const CONTROL_LOCK_STALE_MS: u64 = 5_000;
static MESSAGE_COUNTER: AtomicU64 = AtomicU64::new(1);

struct FileLockGuard {
    path: PathBuf,
}

impl Drop for FileLockGuard {
    fn drop(&mut self) {
        let _ = fs::remove_file(&self.path);
    }
}

fn unix_millis() -> u64 {
    let millis = SystemTime::now()
        .duration_since(UNIX_EPOCH)
        .unwrap_or_default()
        .as_millis();
    millis.min(u64::MAX as u128) as u64
}

fn acquire_file_lock(path: &Path) -> Result<FileLockGuard, String> {
    let started = unix_millis();
    loop {
        match OpenOptions::new().write(true).create_new(true).open(path) {
            Ok(mut file) => {
                let _ = writeln!(file, "pid={} at={}", std::process::id(), unix_millis());
                return Ok(FileLockGuard { path: path.to_path_buf() });
            }
            Err(error) if error.kind() == std::io::ErrorKind::AlreadyExists => {
                let stale = path
                    .metadata()
                    .and_then(|meta| meta.modified())
                    .ok()
                    .and_then(|modified| SystemTime::now().duration_since(modified).ok())
                    .is_some_and(|age| age.as_millis() >= CONTROL_LOCK_STALE_MS as u128);
                if stale {
                    let _ = fs::remove_file(path);
                    continue;
                }
                if unix_millis().saturating_sub(started) >= CONTROL_LOCK_WAIT_MS {
                    return Err("UI inbox is busy; retry shortly".to_string());
                }
                thread::sleep(Duration::from_millis(25));
            }
            Err(error) => return Err(format!("Cannot lock UI inbox: {error}")),
        }
    }
}

fn append_only_read(path: &Path, offset: Option<u64>, max_bytes: Option<usize>) -> Result<(u64, String, bool), String> {
    if !path.exists() {
        return Ok((0, String::new(), false));
    }

    let length = path
        .metadata()
        .map_err(|error| format!("Cannot stat {}: {error}", path.display()))?
        .len();
    let rotated = offset.is_some_and(|value| value > length);
    let mut start = match offset {
        Some(value) if value <= length => value,
        _ => length.saturating_sub(HISTORY_BYTES),
    };
    let limit = max_bytes
        .unwrap_or(DEFAULT_READ_BYTES)
        .clamp(1024, MAX_READ_BYTES);

    let mut file = File::open(path).map_err(|error| format!("Cannot open {}: {error}", path.display()))?;
    if start > 0 {
        file.seek(SeekFrom::Start(start - 1))
            .map_err(|error| format!("Cannot seek {}: {error}", path.display()))?;
        let mut previous = [0_u8; 1];
        file.read_exact(&mut previous)
            .map_err(|error| format!("Cannot align {}: {error}", path.display()))?;
        file.seek(SeekFrom::Start(start))
            .map_err(|error| format!("Cannot seek {}: {error}", path.display()))?;
        if previous[0] != b'\n' {
            let mut reader = BufReader::new(file);
            let mut discarded = String::new();
            reader
                .read_line(&mut discarded)
                .map_err(|error| format!("Cannot align line in {}: {error}", path.display()))?;
            start = reader
                .stream_position()
                .map_err(|error| format!("Cannot read position in {}: {error}", path.display()))?;
            file = reader.into_inner();
        }
    }

    file.seek(SeekFrom::Start(start))
        .map_err(|error| format!("Cannot seek {}: {error}", path.display()))?;
    let mut reader = BufReader::new(file);
    let mut output = String::new();

    loop {
        let line_start = reader
            .stream_position()
            .map_err(|error| format!("Cannot read position in {}: {error}", path.display()))?;
        let mut line = String::new();
        let read = reader
            .read_line(&mut line)
            .map_err(|error| format!("Cannot read line in {}: {error}", path.display()))?;
        if read == 0 {
            break;
        }
        if !output.is_empty() && output.len() + line.len() > limit {
            reader
                .seek(SeekFrom::Start(line_start))
                .map_err(|error| format!("Cannot rewind {}: {error}", path.display()))?;
            break;
        }
        output.push_str(&line);
        if output.len() >= limit {
            break;
        }
    }

    let next_offset = reader
        .stream_position()
        .map_err(|error| format!("Cannot read position in {}: {error}", path.display()))?;
    Ok((next_offset, output, rotated))
}

fn activity_history_paths() -> Result<Vec<PathBuf>, String> {
    let directory = Path::new(ACTIVITY_DATA_DIR);
    if !directory.exists() {
        return Ok(Vec::new());
    }
    let current = Path::new(ACTIVITY_EVENT_FILE);
    let mut paths: Vec<PathBuf> = fs::read_dir(directory)
        .map_err(|error| format!("Cannot read activity data directory: {error}"))?
        .filter_map(|entry| entry.ok().map(|value| value.path()))
        .filter(|path| {
            path != current
                && path.file_name()
                    .and_then(|value| value.to_str())
                    .is_some_and(|name| name.starts_with("events") && name.ends_with(".jsonl"))
        })
        .collect();
    paths.sort_by_key(|path| {
        path.metadata()
            .and_then(|meta| meta.modified())
            .unwrap_or(UNIX_EPOCH)
    });
    Ok(paths)
}

fn valid_task_id(value: &str) -> bool {
    value.len() == 32 && value.bytes().all(|byte| byte.is_ascii_hexdigit())
}

fn valid_thread_id(value: &str) -> bool {
    !value.is_empty()
        && value.len() <= 128
        && value.bytes().all(|byte| byte.is_ascii_alphanumeric() || matches!(byte, b'.' | b'_' | b':' | b'-'))
}

#[derive(Clone, Debug, PartialEq, Eq)]
struct ActiveLeaseTarget {
    task_id: String,
    thread_id: String,
    workspace: String,
}

fn normalized_workspace(value: &str) -> String {
    value
        .trim()
        .trim_end_matches(['\\', '/'])
        .replace('/', "\\")
        .to_ascii_lowercase()
}

fn workspace_matches(left: &str, right: &str) -> bool {
    !left.trim().is_empty()
        && !right.trim().is_empty()
        && normalized_workspace(left) == normalized_workspace(right)
}

fn parse_active_lease(raw: &str) -> Option<ActiveLeaseTarget> {
    let value: serde_json::Value = serde_json::from_str(raw).ok()?;
    let task_id = value.get("lease_id").and_then(|item| item.as_str())?.trim();
    let thread_id = value
        .get("thread_id")
        .and_then(|item| item.as_str())
        .unwrap_or(task_id)
        .trim();
    let workspace = value.get("workspace").and_then(|item| item.as_str())?.trim();
    let mode = value.get("mode").and_then(|item| item.as_str()).unwrap_or_default();
    if !valid_task_id(task_id) || !valid_thread_id(thread_id) || workspace.is_empty() || mode != "execute_exclusive" {
        return None;
    }
    Some(ActiveLeaseTarget {
        task_id: task_id.to_string(),
        thread_id: thread_id.to_string(),
        workspace: workspace.to_string(),
    })
}

fn active_lease_targets(directory: &Path) -> Result<Vec<ActiveLeaseTarget>, String> {
    if !directory.exists() {
        return Ok(Vec::new());
    }
    let mut targets = Vec::new();
    for entry in fs::read_dir(directory).map_err(|error| format!("Cannot read Runtime lease directory: {error}"))? {
        let Ok(entry) = entry else { continue };
        let path = entry.path();
        let is_lease_file = path
            .file_name()
            .and_then(|value| value.to_str())
            .is_some_and(|name| name.starts_with("lease-") && name.ends_with(".json"));
        if !is_lease_file {
            continue;
        }
        match fs::read_to_string(&path) {
            Ok(raw) => {
                if let Some(target) = parse_active_lease(&raw) {
                    targets.push(target);
                }
            }
            Err(error) if error.kind() == std::io::ErrorKind::NotFound => {}
            Err(error) => return Err(format!("Cannot read Runtime lease metadata {}: {error}", path.display())),
        }
    }
    Ok(targets)
}

fn resolve_active_lease_in_dir(
    directory: &Path,
    task_id: &str,
    thread_id: &str,
    workspace: Option<&str>,
) -> Result<(ActiveLeaseTarget, bool), String> {
    if !valid_task_id(task_id) {
        return Err("invalid task_id".to_string());
    }
    if !valid_thread_id(thread_id) {
        return Err("invalid thread_id".to_string());
    }
    let workspace = workspace.map(str::trim).filter(|value| !value.is_empty());
    let exact_path = directory.join(format!("lease-{task_id}.json"));
    if let Ok(raw) = fs::read_to_string(&exact_path) {
        if let Some(target) = parse_active_lease(&raw) {
            let workspace_ok = workspace.is_none_or(|value| workspace_matches(&target.workspace, value));
            if target.task_id == task_id && target.thread_id == thread_id && workspace_ok {
                return Ok((target, false));
            }
        }
    }

    let candidates: Vec<ActiveLeaseTarget> = active_lease_targets(directory)?
        .into_iter()
        .filter(|target| {
            target.thread_id == thread_id
                && workspace.is_some_and(|value| workspace_matches(&target.workspace, value))
        })
        .collect();
    match candidates.as_slice() {
        [target] => Ok((target.clone(), true)),
        [] => Err("current lease is no longer active; refresh or select the current active task".to_string()),
        _ => Err("multiple active leases match this thread/workspace; select the exact task before sending".to_string()),
    }
}

fn resolve_active_lease(
    task_id: &str,
    thread_id: &str,
    workspace: Option<&str>,
) -> Result<(ActiveLeaseTarget, bool), String> {
    resolve_active_lease_in_dir(Path::new(RUNTIME_LEASE_DIR), task_id, thread_id, workspace)
}

fn compact_control_file(path: &Path) -> Result<(), String> {
    let size = path.metadata().map(|meta| meta.len()).unwrap_or(0);
    if size < CONTROL_MAX_BYTES {
        return Ok(());
    }
    let start = size.saturating_sub(CONTROL_RETAIN_BYTES);
    let mut file = File::open(path).map_err(|error| format!("Cannot compact UI inbox: {error}"))?;
    file.seek(SeekFrom::Start(start)).map_err(|error| format!("Cannot seek UI inbox: {error}"))?;
    let mut data = Vec::new();
    file.read_to_end(&mut data).map_err(|error| format!("Cannot read UI inbox: {error}"))?;
    if start > 0 {
        if let Some(index) = data.iter().position(|byte| *byte == b'\n') {
            data.drain(..=index);
        } else {
            data.clear();
        }
    }
    let temporary = path.with_file_name(format!(
        ".ui-inbox.{}.{}.tmp",
        std::process::id(),
        MESSAGE_COUNTER.fetch_add(1, Ordering::Relaxed)
    ));
    fs::write(&temporary, data).map_err(|error| format!("Cannot write compacted UI inbox: {error}"))?;
    fs::rename(&temporary, path).or_else(|_| {
        fs::remove_file(path)?;
        fs::rename(&temporary, path)
    }).map_err(|error| format!("Cannot replace UI inbox: {error}"))?;
    Ok(())
}

fn safe_attachment_name(raw: &str) -> String {
    let mut output = String::new();
    for ch in raw.chars().take(180) {
        if ch.is_control() || matches!(ch, '<' | '>' | ':' | '"' | '/' | '\\' | '|' | '?' | '*') {
            output.push('_');
        } else {
            output.push(ch);
        }
    }
    let clean = output.trim().trim_matches('.').trim();
    if clean.is_empty() { "attachment.bin".to_string() } else { clean.to_string() }
}

fn safe_attachment_mime(raw: &str) -> String {
    let clean: String = raw
        .chars()
        .filter(|ch| !ch.is_control())
        .take(160)
        .collect();
    if clean.contains('/') { clean } else { "application/octet-stream".to_string() }
}

fn cleanup_attachment_paths(paths: &[PathBuf]) {
    for path in paths {
        let _ = fs::remove_file(path);
    }
}

fn persist_attachments(
    task_id: &str,
    attachments: Option<Vec<serde_json::Value>>,
) -> Result<(Vec<serde_json::Value>, Vec<PathBuf>), String> {
    let values = attachments.unwrap_or_default();
    if values.len() > MAX_ATTACHMENTS {
        return Err(format!("at most {MAX_ATTACHMENTS} attachments are allowed"));
    }
    if values.is_empty() {
        return Ok((Vec::new(), Vec::new()));
    }

    let task_dir = Path::new(ATTACHMENT_DATA_DIR).join(task_id);
    fs::create_dir_all(&task_dir).map_err(|error| format!("Cannot create attachment directory: {error}"))?;
    let mut total_bytes = 0usize;
    let mut records = Vec::with_capacity(values.len());
    let mut paths = Vec::with_capacity(values.len());

    let persist_result = (|| -> Result<(), String> {
        for value in values {
            let name = safe_attachment_name(value.get("name").and_then(|item| item.as_str()).unwrap_or("attachment.bin"));
            let mime = safe_attachment_mime(value.get("mime").and_then(|item| item.as_str()).unwrap_or("application/octet-stream"));
            let encoded = value
                .get("dataBase64")
                .and_then(|item| item.as_str())
                .ok_or_else(|| format!("attachment {name} is missing dataBase64"))?;
            let bytes = BASE64_STANDARD
                .decode(encoded)
                .map_err(|_| format!("attachment {name} contains invalid base64 data"))?;
            if bytes.len() > MAX_ATTACHMENT_BYTES {
                return Err(format!("attachment {name} exceeds the 25 MB limit"));
            }
            total_bytes = total_bytes.saturating_add(bytes.len());
            if total_bytes > MAX_ATTACHMENT_TOTAL_BYTES {
                return Err("attachments exceed the 50 MB total limit".to_string());
            }

            let now = unix_millis();
            let sequence = MESSAGE_COUNTER.fetch_add(1, Ordering::Relaxed);
            let id = format!("att-{now}-{}-{sequence}", std::process::id());
            let path = task_dir.join(format!("{id}__{name}"));
            let mut file = OpenOptions::new()
                .create_new(true)
                .write(true)
                .open(&path)
                .map_err(|error| format!("Cannot create attachment {name}: {error}"))?;
            paths.push(path.clone());
            file.write_all(&bytes).map_err(|error| format!("Cannot write attachment {name}: {error}"))?;
            file.flush().map_err(|error| format!("Cannot flush attachment {name}: {error}"))?;
            records.push(serde_json::json!({
                "id": id,
                "name": name,
                "mime": mime,
                "size": bytes.len(),
                "path": path.to_string_lossy(),
            }));
        }
        Ok(())
    })();

    if let Err(error) = persist_result {
        cleanup_attachment_paths(&paths);
        return Err(error);
    }
    Ok((records, paths))
}

fn archived_task_starts(history: &str, directory: &Path) -> String {
    let mut known_ids = HashSet::new();
    for line in history.lines() {
        let Ok(row) = serde_json::from_str::<serde_json::Value>(line) else { continue; };
        if row.get("type").and_then(|value| value.as_str()) == Some("task.started") {
            if let Some(id) = row.get("taskId").and_then(|value| value.as_str()) {
                known_ids.insert(id.to_owned());
            }
        }
    }

    let cutoff = SystemTime::now()
        .checked_sub(CONTROLLER_INDEX_MAX_AGE)
        .unwrap_or(UNIX_EPOCH);
    let Ok(entries) = fs::read_dir(directory) else { return String::new(); };
    let mut recovered = Vec::new();
    for entry in entries.flatten() {
        let path = entry.path();
        if !path.file_name().and_then(|name| name.to_str())
            .is_some_and(|name| name.starts_with("controller-") && name.ends_with(".json"))
        {
            continue;
        }
        if !path.metadata().and_then(|meta| meta.modified())
            .is_ok_and(|modified| modified >= cutoff)
        {
            continue;
        }
        let Ok(raw) = fs::read_to_string(&path) else { continue; };
        let Ok(value) = serde_json::from_str::<serde_json::Value>(&raw) else { continue; };
        let get = |field: &str| value.get(field).and_then(|item| item.as_str());
        let (Some(id), Some(thread), Some(workspace), Some(title), Some(started_at)) = (
            get("last_lease_id"), get("thread_id"), get("workspace"),
            get("owner_label"), get("task_started_at"),
        ) else { continue; };
        if !valid_task_id(id) || !valid_thread_id(thread) || workspace.trim().is_empty()
            || title.trim().is_empty() || started_at.len() < 20
            || !known_ids.insert(id.to_owned())
        {
            continue;
        }
        let event = serde_json::json!({
            "id": format!("archived-index-{id}"),
            "timestamp": started_at,
            "type": "task.started",
            "taskId": id,
            "threadId": thread,
            "workspace": workspace,
            "ownerLabel": title,
            "title": title
        });
        recovered.push((started_at.to_owned(), event.to_string()));
    }
    recovered.sort_by(|a, b| a.0.cmp(&b.0));
    let mut output = String::new();
    for (_, row) in recovered {
        output.push_str(&row);
        output.push('\n');
    }
    output
}

#[tauri::command]
fn activity_bootstrap() -> Result<(u64, String), String> {
    let current = Path::new(ACTIVITY_EVENT_FILE);
    let current_len = current.metadata().map(|meta| meta.len()).unwrap_or(0);
    let mut output = Vec::<u8>::new();

    for path in activity_history_paths()?.into_iter().chain(std::iter::once(current.to_path_buf())) {
        if !path.exists() {
            continue;
        }
        let bytes = fs::read(&path).map_err(|error| format!("Cannot read activity history {}: {error}", path.display()))?;
        output.extend_from_slice(&bytes);
        if output.len() > HISTORY_BYTES as usize {
            let overflow = output.len() - HISTORY_BYTES as usize;
            let newline = output[overflow..].iter().position(|byte| *byte == b'\n').map(|index| overflow + index + 1).unwrap_or(overflow);
            output.drain(..newline);
        }
    }

    let history = String::from_utf8_lossy(&output).into_owned();
    // Rebuild only missing task identity from closed Runtime leases, not content or outcomes.
    let index = archived_task_starts(&history, Path::new(CONTROLLER_ARCHIVE_DIR));
    Ok((current_len, format!("{index}{history}")))
}

#[tauri::command]
fn activity_read(offset: Option<u64>, max_bytes: Option<usize>) -> Result<(u64, String, bool), String> {
    append_only_read(Path::new(ACTIVITY_EVENT_FILE), offset, max_bytes)
}

#[tauri::command]
fn control_read(offset: Option<u64>, max_bytes: Option<usize>) -> Result<(u64, String, bool), String> {
    append_only_read(Path::new(CONTROL_INPUT_FILE), offset, max_bytes)
}

fn append_control_text(target: &ActiveLeaseTarget, text: &str, control_behavior: &str) -> Result<String, String> {
    let clean = text.trim();
    if clean.is_empty() || clean.chars().count() > MAX_INPUT_TEXT_CHARS {
        return Err("generated control message is empty or too long".to_string());
    }
    if !matches!(control_behavior, "interrupt" | "informational") {
        return Err("generated control message behavior is invalid".to_string());
    }
    fs::create_dir_all(ACTIVITY_DATA_DIR).map_err(|error| format!("Cannot create activity data directory: {error}"))?;
    let _lock = acquire_file_lock(Path::new(CONTROL_LOCK_FILE))?;
    let input_path = Path::new(CONTROL_INPUT_FILE);
    compact_control_file(input_path)?;
    let now = unix_millis();
    let sequence = MESSAGE_COUNTER.fetch_add(1, Ordering::Relaxed);
    let id = format!("ui-{now}-{}-{sequence}", std::process::id());
    let row = serde_json::json!({
        "id": id,
        "timestampMs": now,
        "type": "ui.message",
        "scope": "task",
        "taskId": target.task_id,
        "threadId": target.thread_id,
        "text": clean,
        "attachments": [],
        "controlBehavior": control_behavior,
    });
    let mut file = OpenOptions::new().create(true).append(true).open(input_path)
        .map_err(|error| format!("Cannot open UI inbox: {error}"))?;
    writeln!(file, "{}", row).map_err(|error| format!("Cannot append UI inbox: {error}"))?;
    file.flush().map_err(|error| format!("Cannot flush UI inbox: {error}"))?;
    Ok(id)
}

#[tauri::command]
fn activity_submit(
    task_id: String,
    thread_id: String,
    workspace: Option<String>,
    text: String,
    attachments: Option<Vec<serde_json::Value>>,
) -> Result<String, String> {
    let requested_task_id = task_id.trim();
    let requested_thread_id = thread_id.trim();
    let clean = text.trim();
    let (target, rebound) = resolve_active_lease(
        requested_task_id,
        requested_thread_id,
        workspace.as_deref(),
    )?;
    if clean.chars().count() > MAX_INPUT_TEXT_CHARS {
        return Err(format!("message is limited to {MAX_INPUT_TEXT_CHARS} characters"));
    }

    let (attachment_records, attachment_paths) = persist_attachments(&target.task_id, attachments)?;
    if clean.is_empty() && attachment_records.is_empty() {
        return Err("message or attachment is required".to_string());
    }

    let append_result = (|| -> Result<String, String> {
        fs::create_dir_all(ACTIVITY_DATA_DIR).map_err(|error| format!("Cannot create activity data directory: {error}"))?;
        let _lock = acquire_file_lock(Path::new(CONTROL_LOCK_FILE))?;
        let input_path = Path::new(CONTROL_INPUT_FILE);
        compact_control_file(input_path)?;

        let now = unix_millis();
        let sequence = MESSAGE_COUNTER.fetch_add(1, Ordering::Relaxed);
        let id = format!("ui-{now}-{}-{sequence}", std::process::id());
        let row = serde_json::json!({
            "id": id,
            "timestampMs": now,
            "type": "ui.message",
            "scope": "task",
            "taskId": target.task_id,
            "threadId": target.thread_id,
            "text": clean,
            "attachments": attachment_records,
            "reboundFromTaskId": if rebound { Some(requested_task_id) } else { None },
        });
        let mut file = OpenOptions::new()
            .create(true)
            .append(true)
            .open(input_path)
            .map_err(|error| format!("Cannot open UI inbox: {error}"))?;
        writeln!(file, "{}", row).map_err(|error| format!("Cannot append UI inbox: {error}"))?;
        file.flush().map_err(|error| format!("Cannot flush UI inbox: {error}"))?;
        Ok(id)
    })();

    if append_result.is_err() {
        cleanup_attachment_paths(&attachment_paths);
    }
    append_result
}

#[tauri::command]
fn activity_runtime_snapshot() -> Result<(u64, Vec<String>), String> {
    let directory = Path::new(RUNTIME_LEASE_DIR);
    if !directory.exists() {
        return Ok((unix_millis(), Vec::new()));
    }

    let mut paths: Vec<PathBuf> = fs::read_dir(directory)
        .map_err(|error| format!("Cannot read runtime lease directory: {error}"))?
        .filter_map(|entry| entry.ok().map(|value| value.path()))
        .filter(|path| {
            path.file_name()
                .and_then(|value| value.to_str())
                .is_some_and(|name| name.starts_with("lease-") && name.ends_with(".json"))
        })
        .collect();
    paths.sort();

    let mut records = Vec::with_capacity(paths.len());
    for path in paths {
        match fs::read_to_string(&path) {
            Ok(raw) => records.push(raw),
            Err(error) if error.kind() == std::io::ErrorKind::NotFound => {}
            Err(error) => return Err(format!("Cannot read runtime lease metadata {}: {error}", path.display())),
        }
    }
    Ok((unix_millis(), records))
}

#[tauri::command]
fn activity_agent_snapshot() -> Result<String, String> {
    let path = Path::new(AGENT_RELATIONS_FILE);
    match fs::read_to_string(path) {
        Ok(raw) if raw.len() <= 2 * 1024 * 1024 => Ok(raw),
        Ok(_) => Err("GPTAgent 关系快照超过安全大小限制".into()),
        Err(error) if error.kind() == std::io::ErrorKind::NotFound => {
            Ok(r#"{"version":1,"updated_at":0,"agents":[],"watchdogs":[]}"#.into())
        }
        Err(error) => Err(format!("Cannot read GPTAgent relationship snapshot: {error}")),
    }
}

#[tauri::command]
async fn activity_image(task_id: String, image_id: String, source: String, thumbnail: Option<bool>) -> Result<String, String> {
    tauri::async_runtime::spawn_blocking(move || visual_images::image_data_uri(
        Path::new(MEDIA_DATA_DIR), &task_id, &image_id, &source, thumbnail.unwrap_or(false),
    )).await.map_err(|error| format!("图片请求失败：{error}"))?
}

#[tauri::command]
async fn activity_visual_update(
    task_id: String, thread_id: String, workspace: String, expected_revision: String,
    keep_reference_ids: Vec<String>, criteria: Vec<String>, text_reference: String, images: Vec<serde_json::Value>,
    depth: String, journeys: Vec<serde_json::Value>,
) -> Result<serde_json::Value, String> {
    let (target, rebound) = resolve_active_lease(&task_id, &thread_id, Some(&workspace))?;
    if rebound { return Err("当前任务租约已经变化，请刷新参考面板；不会自动迁移旧任务资料".into()); }
    let payload = serde_json::json!({
        "lease_id": target.task_id, "thread_id": target.thread_id, "workspace": target.workspace,
        "expected_revision": expected_revision, "keep_reference_ids": keep_reference_ids,
        "criteria": criteria, "text_reference": text_reference, "images": images,
        "depth": depth, "journeys": journeys,
    });
    tauri::async_runtime::spawn_blocking(move || {
        let root = Path::new(ACTIVITY_DATA_DIR).parent().ok_or("运行目录无效")?;
        visual_references::update(root, payload)
    }).await.map_err(|error| format!("参考资料请求失败：{error}"))?
}

#[tauri::command]
async fn activity_visual_toggle(
    task_id: String, thread_id: String, workspace: String, expected_revision: String, enabled: bool,
) -> Result<serde_json::Value, String> {
    let (target, rebound) = resolve_active_lease(&task_id, &thread_id, Some(&workspace))?;
    if rebound { return Err("当前任务租约已经变化，请刷新看图模式状态；不会自动操作旧任务".into()); }
    let payload = serde_json::json!({
        "lease_id": target.task_id, "thread_id": target.thread_id, "workspace": target.workspace,
        "expected_revision": expected_revision, "mode_enabled": enabled,
    });
    tauri::async_runtime::spawn_blocking(move || {
        let root = Path::new(ACTIVITY_DATA_DIR).parent().ok_or("运行目录无效")?;
        visual_references::update(root, payload)
    }).await.map_err(|error| format!("看图模式请求失败：{error}"))?
}

#[tauri::command]
async fn activity_goal_update(
    task_id: String, thread_id: String, workspace: String, goal_expected_revision: String,
    enabled: bool, outcome: String, success_criteria: Vec<String>, goal_requirements: Vec<String>,
    goal_change_note: String, reuse_visual_context: bool,
) -> Result<serde_json::Value, String> {
    let (target, rebound) = resolve_active_lease(&task_id, &thread_id, Some(&workspace))?;
    if rebound { return Err("当前任务租约已经变化，请刷新目标模式状态；不会自动操作旧任务".into()); }
    let notice_target = target.clone();
    let payload = serde_json::json!({
        "lease_id": target.task_id, "thread_id": target.thread_id, "workspace": target.workspace,
        "goal_expected_revision": goal_expected_revision, "goal_enabled": enabled,
        "goal_outcome": outcome, "goal_success_criteria": success_criteria,
        "goal_requirements": goal_requirements, "goal_change_note": goal_change_note,
        "goal_reuse_visual": reuse_visual_context,
    });
    let result = tauri::async_runtime::spawn_blocking(move || {
        let root = Path::new(ACTIVITY_DATA_DIR).parent().ok_or("运行目录无效")?;
        visual_references::update(root, payload)
    }).await.map_err(|error| format!("目标模式请求失败：{error}"))??;
    if result.get("changed").and_then(|value| value.as_bool()) == Some(true) {
        let goal = result.get("goal_mode").cloned().unwrap_or_else(|| serde_json::json!({}));
        let revision = goal.get("revision").and_then(|value| value.as_u64()).unwrap_or(0);
        let active = goal.get("active").and_then(|value| value.as_bool()).unwrap_or(false);
        let current_outcome = goal.get("outcome").and_then(|value| value.as_str()).unwrap_or("");
        let requirements = goal.get("requirements").and_then(|value| value.as_array()).cloned().unwrap_or_default();
        let criteria = goal.get("success_criteria").and_then(|value| value.as_array()).cloned().unwrap_or_default();
        let requirement_text = requirements.iter().filter_map(|item| item.as_str()).take(12).map(|item| format!("- {item}")).collect::<Vec<_>>().join("\n");
        let criteria_text = criteria.iter().filter_map(|item| item.as_str()).take(12).map(|item| format!("- {item}")).collect::<Vec<_>>().join("\n");
        let note = goal.get("corrections").and_then(|value| value.as_array()).and_then(|items| items.last())
            .and_then(|item| item.get("note")).and_then(|value| value.as_str()).unwrap_or("");
        let message = format!(
            "[Goal 修正 · r{revision}]\n用户刚在 Agent Console 修改持续目标。把本条作为新的用户信息，并始终以最新 Goal revision 为准。\n状态：{}\n目标：{}\n持续要求：\n{}\n完成标准：\n{}\n本次修正：{}",
            if active { "开启" } else { "关闭" }, current_outcome,
            if requirement_text.is_empty() { "- 无新增持续要求" } else { &requirement_text },
            if criteria_text.is_empty() { "- 无" } else { &criteria_text },
            if note.is_empty() { "用户更新了 Goal 配置" } else { note },
        );
        append_control_text(&notice_target, &message, "interrupt")?;
    }
    Ok(result)
}

#[tauri::command]
async fn activity_ultra_update(
    task_id: String, thread_id: String, workspace: String, ultra_expected_revision: String, enabled: bool,
) -> Result<serde_json::Value, String> {
    let (target, rebound) = resolve_active_lease(&task_id, &thread_id, Some(&workspace))?;
    if rebound { return Err("当前任务租约已经变化，请刷新 Ultra 状态；不会自动操作旧任务".into()); }
    let notice_target = target.clone();
    let payload = serde_json::json!({
        "lease_id": target.task_id, "thread_id": target.thread_id, "workspace": target.workspace,
        "ultra_expected_revision": ultra_expected_revision, "ultra_enabled": enabled,
    });
    let result = tauri::async_runtime::spawn_blocking(move || {
        let root = Path::new(ACTIVITY_DATA_DIR).parent().ok_or("运行目录无效")?;
        visual_references::update(root, payload)
    }).await.map_err(|error| format!("Ultra 模式请求失败：{error}"))??;
    if result.get("changed").and_then(|value| value.as_bool()) == Some(true) {
        let message = if enabled {
            "[Ultra 已开启]\n用户允许网页 GPT 自行开页、查看控件、填写并发送独立子任务。先读 docs/WEB_GPT_SOFT_ORCHESTRATION.md；按任务需要建立 1–3 个子对话，先约定不冲突的文件范围和持久交接，优先复用已有对话。后台不会自动创建或发送。"
        } else {
            "[Ultra 已关闭]\n用户已关闭新增并行子对话的许可。不要自行反开或新建子对话；已有子任务由父 GPT 查看页面并安全收口，保存交接。"
        };
        append_control_text(&notice_target, message, "interrupt")?;
    }
    Ok(result)
}

#[tauri::command]
async fn activity_watchdog_update(
    task_id: String, thread_id: String, workspace: String, enabled: bool,
) -> Result<serde_json::Value, String> {
    let (target, rebound) = resolve_active_lease(&task_id, &thread_id, Some(&workspace))?;
    if rebound { return Err("当前任务租约已经变化，请刷新 Watchdog 状态；不会自动操作旧任务".into()); }
    let notice_target = target.clone();
    let payload = serde_json::json!({
        "lease_id": target.task_id, "thread_id": target.thread_id, "workspace": target.workspace,
        "watchdog_enabled": enabled,
    });
    let result = tauri::async_runtime::spawn_blocking(move || {
        let root = Path::new(ACTIVITY_DATA_DIR).parent().ok_or("运行目录无效")?;
        visual_references::update(root, payload)
    }).await.map_err(|error| format!("Watchdog 请求失败：{error}"))??;
    let message = if enabled {
        "[看门狗已允许]\n由当前网页 GPT 按 docs/WEB_GPT_SOFT_ORCHESTRATION.md 自己打开监督对话、发送任务和交接资料。监督对话负责本任务及其子任务；后台不自动创建、唤醒或换代。开关不代表已经启动或持续在线。"
    } else {
        "[看门狗已关闭]\n用户已关闭监督流程，不得自行反开。请由网页 GPT 向已建立的监督对话发送停止要求，并保存交接；后台不会自动操作对话。"
    };
    append_control_text(&notice_target, message, "interrupt")?;
    Ok(result)
}

fn main() {
    tauri::Builder::default()
        .invoke_handler(tauri::generate_handler![
            activity_history::activity_bootstrap_compact,
            activity_history::activity_task_page,
            activity_bootstrap,
            activity_read,
            control_read,
            activity_submit,
            activity_runtime_snapshot,
            activity_agent_snapshot,
            activity_image,
            activity_visual_update,
            activity_visual_toggle,
            activity_goal_update,
            activity_ultra_update,
            activity_watchdog_update
        ])
        .run(tauri::generate_context!())
        .expect("error while running GPT-PC Agent Console");
}

#[cfg(test)]
mod tests {
    use super::*;

    fn test_lease_dir(label: &str) -> PathBuf {
        let path = std::env::temp_dir().join(format!(
            "gpt-pc-console-{label}-{}-{}",
            std::process::id(),
            unix_millis()
        ));
        fs::create_dir_all(&path).unwrap();
        path
    }

    fn write_test_lease(directory: &Path, task_id: &str, thread_id: &str, workspace: &str) {
        let row = serde_json::json!({
            "lease_id": task_id,
            "thread_id": thread_id,
            "workspace": workspace,
            "mode": "execute_exclusive",
        });
        fs::write(
            directory.join(format!("lease-{task_id}.json")),
            serde_json::to_vec(&row).unwrap(),
        )
        .unwrap();
    }

    #[test]
    fn closed_controller_restores_only_missing_task_identity() {
        let directory = test_lease_dir("archive-index");
        let missing = "11111111111111111111111111111111";
        let existing = "22222222222222222222222222222222";
        for id in [missing, existing] {
            let value = serde_json::json!({
                "last_lease_id": id,
                "thread_id": "thread-test",
                "workspace": "D:\\work\\example",
                "owner_label": "Historical session",
                "task_started_at": "2026-10-12T08:12:00Z",
                "closed_at": "2026-10-12T08:13:00Z",
                "private_data": "should-not-be-in-UI"
            });
            fs::write(directory.join(format!("controller-{id}.json")), value.to_string()).unwrap();
        }
        let history = format!(r#"{{"type":"task.started","taskId":"{existing}"}}"#);
        let result = archived_task_starts(&history, &directory);
        assert!(result.contains(missing));
        assert!(!result.contains(existing));
        assert!(result.contains("Historical session"));
        assert!(!result.contains("should-not-be-in-UI"));
        let _ = fs::remove_dir_all(directory);
    }

    #[test]
    fn stale_console_target_rebinds_only_to_unique_same_thread_workspace() {
        let directory = test_lease_dir("rebind");
        let stale = "11111111111111111111111111111111";
        let current = "22222222222222222222222222222222";
        let thread = "thread-console-rebind";
        let workspace = r"C:\GPT-PC\runtime\maintenance-workspace";
        write_test_lease(&directory, current, thread, workspace);

        let (resolved, rebound) = resolve_active_lease_in_dir(
            &directory,
            stale,
            thread,
            Some(r"d:/chatgpt/runtime/maintenance-workspace/"),
        )
        .unwrap();
        assert_eq!(resolved.task_id, current);
        assert_eq!(resolved.thread_id, thread);
        assert!(rebound);

        let (exact, exact_rebound) = resolve_active_lease_in_dir(
            &directory,
            current,
            thread,
            Some(workspace),
        )
        .unwrap();
        assert_eq!(exact.task_id, current);
        assert!(!exact_rebound);
        let _ = fs::remove_dir_all(directory);
    }

    #[test]
    fn console_rebind_fails_closed_for_other_thread_or_multiple_candidates() {
        let directory = test_lease_dir("fail-closed");
        let stale = "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa";
        let first = "bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb";
        let second = "cccccccccccccccccccccccccccccccc";
        let workspace = r"D:\Projects\Example";
        write_test_lease(&directory, first, "thread-other", workspace);

        let other_thread = resolve_active_lease_in_dir(
            &directory,
            stale,
            "thread-requester",
            Some(workspace),
        )
        .unwrap_err();
        assert!(other_thread.contains("no longer active"));

        write_test_lease(&directory, first, "thread-requester", workspace);
        write_test_lease(&directory, second, "thread-requester", workspace);
        let ambiguous = resolve_active_lease_in_dir(
            &directory,
            stale,
            "thread-requester",
            Some(workspace),
        )
        .unwrap_err();
        assert!(ambiguous.contains("multiple active leases"));
        let _ = fs::remove_dir_all(directory);
    }

    #[test]
    fn failed_multi_attachment_persistence_rolls_back_files() {
        let task_id = format!("audit-attachment-rollback-{}-{}", std::process::id(), unix_millis());
        let task_dir = Path::new(ATTACHMENT_DATA_DIR).join(&task_id);
        let _ = fs::remove_dir_all(&task_dir);
        let result = persist_attachments(
            &task_id,
            Some(vec![
                serde_json::json!({"name": "first.txt", "mime": "text/plain", "dataBase64": "T0s="}),
                serde_json::json!({"name": "broken.txt", "mime": "text/plain", "dataBase64": "%%%"}),
            ]),
        );
        let remaining_files = fs::read_dir(&task_dir)
            .map(|items| items.filter_map(Result::ok).filter(|item| item.path().is_file()).count())
            .unwrap_or(0);
        let _ = fs::remove_dir_all(&task_dir);
        assert!(result.is_err());
        assert_eq!(remaining_files, 0);
    }
}
