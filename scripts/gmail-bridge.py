from __future__ import annotations

import argparse
import base64
from email.message import EmailMessage
from email.utils import getaddresses
import hashlib
import json
import mimetypes
import os
from pathlib import Path
import time
from typing import Any

from google.auth.exceptions import RefreshError
from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow
from googleapiclient.discovery import build


PROJECT_ROOT = Path(os.environ.get("GPT_PC_HOME") or Path(__file__).resolve().parent.parent).expanduser().resolve()
MEDIA_GMAIL_DIR = PROJECT_ROOT / "media" / "gmail"
CLIENT_CONFIG = Path(os.environ.get("GPT_PC_GMAIL_OAUTH_CLIENT", PROJECT_ROOT / "config" / "gmail-oauth-client.json"))
TOKEN_FILE = Path(os.environ.get("GPT_PC_GMAIL_OAUTH_TOKEN", PROJECT_ROOT / "config" / "gmail-oauth-token.json"))
SCOPES = ("https://mail.google.com/",)
MAX_ATTACHMENT_BYTES = 50 * 1024 * 1024
MAX_BODY_CHARS = 200_000
MAX_RESULTS = 50


class GmailOAuthSetupRequired(RuntimeError):
    """Raised when a headless caller cannot repair missing/revoked OAuth."""


def _result(value: dict[str, Any], code: int = 0) -> None:
    print(json.dumps(value, ensure_ascii=False, separators=(",", ":")))
    raise SystemExit(code)


def _load_credentials(*, interactive_auth: bool = True) -> Credentials | None:
    credentials: Credentials | None = None
    refresh_revoked = False
    if TOKEN_FILE.is_file():
        try:
            credentials = Credentials.from_authorized_user_file(str(TOKEN_FILE), SCOPES)
        except Exception:
            credentials = None
    if credentials and credentials.expired and credentials.refresh_token:
        try:
            credentials.refresh(Request())
        except RefreshError:
            # A revoked/expired refresh token is recoverable for an explicit
            # foreground Gmail call, but background notifier helpers must not
            # open consent tabs on their own.
            credentials = None
            refresh_revoked = True
    if credentials and credentials.valid:
        return credentials
    if not CLIENT_CONFIG.is_file():
        return None
    if not interactive_auth:
        if refresh_revoked:
            raise GmailOAuthSetupRequired("Gmail OAuth 授权已失效或被撤销，需要在前台重新授权")
        raise GmailOAuthSetupRequired("Gmail OAuth 尚未完成有效授权，需要在前台授权")
    flow = InstalledAppFlow.from_client_secrets_file(str(CLIENT_CONFIG), SCOPES)
    credentials = flow.run_local_server(
        host="127.0.0.1",
        port=0,
        open_browser=True,
        authorization_prompt_message="正在浏览器中打开 GPT-PC Gmail API 授权页。",
        success_message="GPT-PC Gmail API 授权完成，可以关闭此页面。",
        timeout_seconds=180,
        access_type="offline",
        prompt="consent",
    )
    TOKEN_FILE.parent.mkdir(parents=True, exist_ok=True)
    temporary = TOKEN_FILE.with_name(f".{TOKEN_FILE.name}.{os.getpid()}.tmp")
    temporary.write_text(credentials.to_json(), encoding="utf-8")
    os.replace(temporary, TOKEN_FILE)
    return credentials


def _service(expected_account: str, *, interactive_auth: bool = True):
    try:
        credentials = _load_credentials(interactive_auth=interactive_auth)
    except GmailOAuthSetupRequired as exc:
        return None, None, {
            "status": "oauth_setup_required",
            "error": str(exc),
            "client_config_path": str(CLIENT_CONFIG),
        }
    if credentials is None:
        return None, None, {
            "status": "oauth_setup_required",
            "error": f"缺少 Gmail OAuth Desktop client 配置：{CLIENT_CONFIG}",
            "client_config_path": str(CLIENT_CONFIG),
        }
    service = build("gmail", "v1", credentials=credentials, cache_discovery=False)
    profile = service.users().getProfile(userId="me").execute()
    account = str(profile.get("emailAddress") or "").strip()
    if expected_account and account.casefold() != expected_account.casefold():
        raise RuntimeError(f"当前 Gmail API 账号不是预期账号：{account or 'unknown'}")
    return service, account, None


def _header(payload: dict[str, Any], name: str) -> str:
    wanted = name.casefold()
    for item in payload.get("headers") or []:
        if str(item.get("name") or "").casefold() == wanted:
            return str(item.get("value") or "")
    return ""


def _iter_parts(part: dict[str, Any], *, path: tuple[int, ...] = (0,)):
    yield part, path
    for index, child in enumerate(part.get("parts") or []):
        if isinstance(child, dict):
            yield from _iter_parts(child, path=(*path, index))


def _decode_base64url(data: str) -> bytes:
    clean = data.strip()
    if not clean:
        return b""
    padding = "=" * (-len(clean) % 4)
    return base64.urlsafe_b64decode(clean + padding)


def _attachment_rows(payload: dict[str, Any]) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for part, path in _iter_parts(payload):
        filename = str(part.get("filename") or "").strip()
        body = part.get("body") if isinstance(part.get("body"), dict) else {}
        attachment_id = str(body.get("attachmentId") or "").strip()
        data = str(body.get("data") or "").strip()
        if not filename or (not attachment_id and not data):
            continue
        size = body.get("size")
        rows.append(
            {
                "filename": filename,
                "mime_type": str(part.get("mimeType") or "application/octet-stream"),
                "attachment_id": attachment_id or None,
                "size": int(size) if isinstance(size, (int, float)) else None,
                "part_path": ".".join(str(value) for value in path),
                "inline_data_available": bool(data and not attachment_id),
            }
        )
    return rows


def _extract_bodies(payload: dict[str, Any]) -> dict[str, str]:
    plain: list[str] = []
    html: list[str] = []
    for part, _path in _iter_parts(payload):
        mime = str(part.get("mimeType") or "").lower()
        body = part.get("body") if isinstance(part.get("body"), dict) else {}
        data = str(body.get("data") or "")
        if not data or mime not in {"text/plain", "text/html"}:
            continue
        raw = _decode_base64url(data)
        if not raw:
            continue
        text = raw.decode("utf-8", errors="replace")
        if mime == "text/plain":
            plain.append(text)
        else:
            html.append(text)
    return {
        "text_plain": "\n".join(plain)[:MAX_BODY_CHARS],
        "text_html": "\n".join(html)[:MAX_BODY_CHARS],
    }


def _message_view(message: dict[str, Any], *, include_body: bool = False) -> dict[str, Any]:
    payload = message.get("payload") if isinstance(message.get("payload"), dict) else {}
    attachments = _attachment_rows(payload)
    row: dict[str, Any] = {
        "message_id": str(message.get("id") or "") or None,
        "thread_id": str(message.get("threadId") or "") or None,
        "history_id": str(message.get("historyId") or "") or None,
        "internal_date_ms": int(message.get("internalDate")) if str(message.get("internalDate") or "").isdigit() else None,
        "label_ids": [str(value) for value in message.get("labelIds") or []],
        "from": _header(payload, "From"),
        "to": _header(payload, "To"),
        "cc": _header(payload, "Cc"),
        "bcc": _header(payload, "Bcc"),
        "reply_to": _header(payload, "Reply-To"),
        "date": _header(payload, "Date"),
        "subject": _header(payload, "Subject"),
        "message_id_header": _header(payload, "Message-ID"),
        "in_reply_to": _header(payload, "In-Reply-To"),
        "references": _header(payload, "References"),
        "snippet": str(message.get("snippet") or "")[:1000],
        "attachments": attachments,
        "attachment_count": len(attachments),
    }
    if include_body:
        row.update(_extract_bodies(payload))
    return row


def _search(service, payload: dict[str, Any], account: str) -> dict[str, Any]:
    query = str(payload.get("query") or "").strip()
    max_results = max(1, min(int(payload.get("max_results") or 10), MAX_RESULTS))
    if not query:
        raise ValueError("query 不能为空；请使用 Gmail 搜索语法")
    listed = service.users().messages().list(userId="me", q=query, maxResults=max_results).execute()
    messages: list[dict[str, Any]] = []
    for row in listed.get("messages") or []:
        message_id = str(row.get("id") or "").strip()
        if not message_id:
            continue
        message = service.users().messages().get(userId="me", id=message_id, format="full").execute()
        messages.append(_message_view(message, include_body=False))
    return {
        "status": "ok",
        "account": account,
        "query": query,
        "messages": messages,
        "count": len(messages),
        "result_size_estimate": listed.get("resultSizeEstimate"),
    }


def _get_message(service, payload: dict[str, Any], account: str) -> dict[str, Any]:
    message_id = str(payload.get("message_id") or "").strip()
    if not message_id:
        raise ValueError("message_id 不能为空")
    message = service.users().messages().get(userId="me", id=message_id, format="full").execute()
    return {"status": "ok", "account": account, "message": _message_view(message, include_body=bool(payload.get("include_body", True)))}


def _get_thread(service, payload: dict[str, Any], account: str) -> dict[str, Any]:
    thread_id = str(payload.get("thread_id") or "").strip()
    if not thread_id:
        raise ValueError("thread_id 不能为空")
    thread = service.users().threads().get(userId="me", id=thread_id, format="full").execute()
    messages = [
        _message_view(message, include_body=bool(payload.get("include_body", True)))
        for message in thread.get("messages") or []
        if isinstance(message, dict)
    ]
    return {"status": "ok", "account": account, "thread_id": thread_id, "messages": messages, "count": len(messages)}


def _safe_output_path(raw: str) -> Path:
    target = Path(raw).resolve(strict=False)
    MEDIA_GMAIL_DIR.mkdir(parents=True, exist_ok=True)
    root = MEDIA_GMAIL_DIR.resolve(strict=False)
    if target.parent != root or target.name in {"", ".", ".."}:
        raise ValueError("output_path 只能指向 Runtime media/gmail 下的单个文件")
    return target


def _download(service, payload: dict[str, Any], account: str) -> dict[str, Any]:
    message_id = str(payload.get("message_id") or "").strip()
    attachment_id = str(payload.get("attachment_id") or "").strip()
    filename = str(payload.get("filename") or "").strip()
    output_path = _safe_output_path(str(payload.get("output_path") or ""))
    overwrite = bool(payload.get("overwrite"))
    if not message_id:
        raise ValueError("message_id 不能为空")
    if not attachment_id and not filename:
        raise ValueError("attachment_id 与 filename 至少提供一个")
    if output_path.exists() and output_path.is_dir():
        raise IsADirectoryError(str(output_path))
    if output_path.exists() and not overwrite:
        raise FileExistsError(str(output_path))

    message = service.users().messages().get(userId="me", id=message_id, format="full").execute()
    message_payload = message.get("payload") if isinstance(message.get("payload"), dict) else {}
    parts: list[tuple[dict[str, Any], dict[str, Any]]] = []
    for part, _path in _iter_parts(message_payload):
        part_filename = str(part.get("filename") or "").strip()
        body = part.get("body") if isinstance(part.get("body"), dict) else {}
        part_attachment_id = str(body.get("attachmentId") or "").strip()
        if part_filename and (part_attachment_id or body.get("data")):
            parts.append((part, body))

    candidates: list[tuple[dict[str, Any], dict[str, Any]]] = []
    selection = "attachment_id" if attachment_id else "filename"
    if attachment_id:
        candidates = [(part, body) for part, body in parts if str(body.get("attachmentId") or "").strip() == attachment_id]
        if not candidates and filename:
            selection = "filename_fallback"
            candidates = [(part, body) for part, body in parts if str(part.get("filename") or "").strip() == filename]
    elif filename:
        candidates = [(part, body) for part, body in parts if str(part.get("filename") or "").strip() == filename]

    if not candidates:
        raise FileNotFoundError("指定 Gmail 附件不存在")
    if len(candidates) > 1:
        raise ValueError("同一邮件内存在多个同名附件；请提供可唯一定位的 attachment_id/filename")

    part, body = candidates[0]
    selected_attachment_id = str(body.get("attachmentId") or "").strip()
    if selected_attachment_id:
        attachment = service.users().messages().attachments().get(userId="me", messageId=message_id, id=selected_attachment_id).execute()
        data = _decode_base64url(str(attachment.get("data") or ""))
    else:
        data = _decode_base64url(str(body.get("data") or ""))
    if not data:
        raise RuntimeError("Gmail API 未返回附件字节")
    if len(data) > MAX_ATTACHMENT_BYTES:
        raise ValueError("Gmail 附件超过 50 MiB 上限")

    temporary = output_path.with_name(f".{output_path.name}.gmail-{os.getpid()}.tmp")
    digest = hashlib.sha256(data).hexdigest()
    try:
        with temporary.open("xb") as handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        if output_path.exists() and not overwrite:
            raise FileExistsError(str(output_path))
        if overwrite:
            os.replace(temporary, output_path)
        else:
            os.rename(temporary, output_path)
    finally:
        temporary.unlink(missing_ok=True)

    saved_digest = hashlib.sha256(output_path.read_bytes()).hexdigest()
    if saved_digest != digest:
        raise RuntimeError("Gmail 附件保存后 SHA-256 校验失败")
    return {
        "status": "saved",
        "account": account,
        "message_id": message_id,
        "thread_id": str(message.get("threadId") or "") or None,
        "subject": _header(message_payload, "Subject"),
        "source_filename": str(part.get("filename") or "") or None,
        "mime_type": str(part.get("mimeType") or "application/octet-stream"),
        "attachment_id": selected_attachment_id or None,
        "selection": selection,
        "path": str(output_path),
        "bytes_written": len(data),
        "sha256": saved_digest,
    }


def _attachment_paths(raw_paths: Any) -> list[Path]:
    paths = [Path(str(item)).resolve(strict=False) for item in (raw_paths or [])]
    if len(paths) > 10:
        raise ValueError("attachment_files 最多 10 个")
    for path in paths:
        if not path.is_file():
            raise FileNotFoundError(str(path))
    return paths


def _build_message(payload: dict[str, Any], account: str) -> tuple[EmailMessage, list[Path]]:
    recipients = [item.strip() for item in str(payload.get("to") or "").replace(";", ",").split(",") if item.strip()]
    subject = str(payload.get("subject") or "").strip()
    body = str(payload.get("body") or "")
    if not recipients:
        raise ValueError("to 不能为空")
    if not subject:
        raise ValueError("subject 不能为空")
    if len(subject) > 500:
        raise ValueError("subject 最多 500 字符")
    if len(body) > 200_000:
        raise ValueError("body 最多 200000 字符")
    attachment_paths = _attachment_paths(payload.get("attachment_files"))
    message = EmailMessage()
    message["From"] = account
    message["To"] = ", ".join(recipients)
    cc = str(payload.get("cc") or "").strip()
    bcc = str(payload.get("bcc") or "").strip()
    if cc:
        message["Cc"] = cc
    if bcc:
        message["Bcc"] = bcc
    message["Subject"] = subject
    message.set_content(body)
    for path in attachment_paths:
        guessed, _encoding = mimetypes.guess_type(path.name)
        maintype, subtype = (guessed or "application/octet-stream").split("/", 1)
        message.add_attachment(path.read_bytes(), maintype=maintype, subtype=subtype, filename=path.name)
    return message, attachment_paths


def _raw_message_body(message: EmailMessage, thread_id: str | None = None) -> dict[str, Any]:
    body: dict[str, Any] = {"raw": base64.urlsafe_b64encode(message.as_bytes()).decode("ascii")}
    if thread_id:
        body["threadId"] = thread_id
    return body


def _send(service, payload: dict[str, Any], account: str) -> dict[str, Any]:
    message, attachment_paths = _build_message(payload, account)
    thread_id = str(payload.get("thread_id") or "").strip() or None
    sent = service.users().messages().send(userId="me", body=_raw_message_body(message, thread_id)).execute()
    message_id = str(sent.get("id") or "")
    if not message_id:
        raise RuntimeError("Gmail API 未返回 sent message id")
    expected_recipients = {address.casefold() for _name, address in getaddresses([(message["To"] or ""), (message.get("Cc") or ""), (message.get("Bcc") or "")]) if address}
    expected_files = {path.name for path in attachment_paths}
    verified = False
    for _ in range(10):
        fetched = service.users().messages().get(userId="me", id=message_id, format="full").execute()
        fetched_payload = fetched.get("payload") if isinstance(fetched.get("payload"), dict) else {}
        actual_recipients = {address.casefold() for _name, address in getaddresses([_header(fetched_payload, "To"), _header(fetched_payload, "Cc"), _header(fetched_payload, "Bcc")]) if address}
        actual_files = {row["filename"] for row in _attachment_rows(fetched_payload)}
        verified = _header(fetched_payload, "Subject") == str(message["Subject"]) and expected_recipients.issubset(actual_recipients) and expected_files.issubset(actual_files)
        if verified:
            break
        time.sleep(0.25)
    return {
        "status": "sent" if verified else "unverified",
        "verified": verified,
        "account": account,
        "message_id": message_id,
        "thread_id": str(sent.get("threadId") or "") or None,
        "recipients": sorted(expected_recipients),
        "attachments": sorted(expected_files),
        "subject": str(message["Subject"]),
    }


def _create_draft(service, payload: dict[str, Any], account: str) -> dict[str, Any]:
    message, attachment_paths = _build_message(payload, account)
    thread_id = str(payload.get("thread_id") or "").strip() or None
    created = service.users().drafts().create(userId="me", body={"message": _raw_message_body(message, thread_id)}).execute()
    return {
        "status": "draft_created",
        "account": account,
        "draft_id": str(created.get("id") or "") or None,
        "message_id": str((created.get("message") or {}).get("id") or "") or None,
        "thread_id": str((created.get("message") or {}).get("threadId") or "") or None,
        "subject": str(message["Subject"]),
        "attachments": [path.name for path in attachment_paths],
    }


def _list_drafts(service, payload: dict[str, Any], account: str) -> dict[str, Any]:
    max_results = max(1, min(int(payload.get("max_results") or 10), MAX_RESULTS))
    listed = service.users().drafts().list(userId="me", maxResults=max_results).execute()
    drafts: list[dict[str, Any]] = []
    for row in listed.get("drafts") or []:
        draft_id = str(row.get("id") or "").strip()
        if not draft_id:
            continue
        draft = service.users().drafts().get(userId="me", id=draft_id, format="full").execute()
        message = draft.get("message") if isinstance(draft.get("message"), dict) else {}
        drafts.append({"draft_id": draft_id, "message": _message_view(message, include_body=False)})
    return {"status": "ok", "account": account, "drafts": drafts, "count": len(drafts), "result_size_estimate": listed.get("resultSizeEstimate")}


def _get_draft(service, payload: dict[str, Any], account: str) -> dict[str, Any]:
    draft_id = str(payload.get("draft_id") or "").strip()
    if not draft_id:
        raise ValueError("draft_id 不能为空")
    draft = service.users().drafts().get(userId="me", id=draft_id, format="full").execute()
    message = draft.get("message") if isinstance(draft.get("message"), dict) else {}
    return {"status": "ok", "account": account, "draft_id": draft_id, "message": _message_view(message, include_body=bool(payload.get("include_body", True)))}


def _update_draft(service, payload: dict[str, Any], account: str) -> dict[str, Any]:
    draft_id = str(payload.get("draft_id") or "").strip()
    if not draft_id:
        raise ValueError("draft_id 不能为空")
    message, attachment_paths = _build_message(payload, account)
    thread_id = str(payload.get("thread_id") or "").strip() or None
    updated = service.users().drafts().update(userId="me", id=draft_id, body={"message": _raw_message_body(message, thread_id)}).execute()
    return {
        "status": "draft_updated",
        "account": account,
        "draft_id": draft_id,
        "message_id": str((updated.get("message") or {}).get("id") or "") or None,
        "thread_id": str((updated.get("message") or {}).get("threadId") or "") or None,
        "subject": str(message["Subject"]),
        "attachments": [path.name for path in attachment_paths],
    }


def _send_draft(service, payload: dict[str, Any], account: str) -> dict[str, Any]:
    draft_id = str(payload.get("draft_id") or "").strip()
    if not draft_id:
        raise ValueError("draft_id 不能为空")
    sent = service.users().drafts().send(userId="me", body={"id": draft_id}).execute()
    return {"status": "sent", "account": account, "draft_id": draft_id, "message_id": str(sent.get("id") or "") or None, "thread_id": str(sent.get("threadId") or "") or None}


def _delete_draft(service, payload: dict[str, Any], account: str) -> dict[str, Any]:
    draft_id = str(payload.get("draft_id") or "").strip()
    if not draft_id:
        raise ValueError("draft_id 不能为空")
    service.users().drafts().delete(userId="me", id=draft_id).execute()
    return {"status": "draft_deleted", "account": account, "draft_id": draft_id}


def _list_labels(service, _payload: dict[str, Any], account: str) -> dict[str, Any]:
    labels = service.users().labels().list(userId="me").execute().get("labels") or []
    return {"status": "ok", "account": account, "labels": labels, "count": len(labels)}


def _create_label(service, payload: dict[str, Any], account: str) -> dict[str, Any]:
    name = str(payload.get("label_name") or "").strip()
    if not name:
        raise ValueError("label_name 不能为空")
    body = {
        "name": name,
        "labelListVisibility": str(payload.get("label_list_visibility") or "labelShow"),
        "messageListVisibility": str(payload.get("message_list_visibility") or "show"),
    }
    label = service.users().labels().create(userId="me", body=body).execute()
    return {"status": "label_created", "account": account, "label": label}


def _update_label(service, payload: dict[str, Any], account: str) -> dict[str, Any]:
    label_id = str(payload.get("label_id") or "").strip()
    if not label_id:
        raise ValueError("label_id 不能为空")
    body: dict[str, Any] = {}
    if str(payload.get("label_name") or "").strip():
        body["name"] = str(payload.get("label_name")).strip()
    if str(payload.get("label_list_visibility") or "").strip():
        body["labelListVisibility"] = str(payload.get("label_list_visibility")).strip()
    if str(payload.get("message_list_visibility") or "").strip():
        body["messageListVisibility"] = str(payload.get("message_list_visibility")).strip()
    if not body:
        raise ValueError("update_label 至少提供一个可更新字段")
    label = service.users().labels().update(userId="me", id=label_id, body=body).execute()
    return {"status": "label_updated", "account": account, "label": label}


def _delete_label(service, payload: dict[str, Any], account: str) -> dict[str, Any]:
    label_id = str(payload.get("label_id") or "").strip()
    if not label_id:
        raise ValueError("label_id 不能为空")
    service.users().labels().delete(userId="me", id=label_id).execute()
    return {"status": "label_deleted", "account": account, "label_id": label_id}


def _labels(payload: dict[str, Any]) -> tuple[list[str], list[str]]:
    add = [str(value).strip() for value in payload.get("label_ids_add") or [] if str(value).strip()]
    remove = [str(value).strip() for value in payload.get("label_ids_remove") or [] if str(value).strip()]
    return add, remove


def _modify_message_labels(service, payload: dict[str, Any], account: str) -> dict[str, Any]:
    message_id = str(payload.get("message_id") or "").strip()
    if not message_id:
        raise ValueError("message_id 不能为空")
    add, remove = _labels(payload)
    if not add and not remove:
        raise ValueError("至少提供 label_ids_add 或 label_ids_remove")
    message = service.users().messages().modify(userId="me", id=message_id, body={"addLabelIds": add, "removeLabelIds": remove}).execute()
    return {"status": "message_labels_modified", "account": account, "message_id": message_id, "label_ids": message.get("labelIds") or []}


def _batch_modify_labels(service, payload: dict[str, Any], account: str) -> dict[str, Any]:
    message_ids = [str(value).strip() for value in payload.get("message_ids") or [] if str(value).strip()]
    if not message_ids:
        raise ValueError("message_ids 不能为空")
    if len(message_ids) > 1000:
        raise ValueError("message_ids 最多 1000 个")
    add, remove = _labels(payload)
    if not add and not remove:
        raise ValueError("至少提供 label_ids_add 或 label_ids_remove")
    service.users().messages().batchModify(userId="me", body={"ids": message_ids, "addLabelIds": add, "removeLabelIds": remove}).execute()
    return {"status": "messages_labels_modified", "account": account, "count": len(message_ids), "message_ids": message_ids}


def _modify_thread_labels(service, payload: dict[str, Any], account: str) -> dict[str, Any]:
    thread_id = str(payload.get("thread_id") or "").strip()
    if not thread_id:
        raise ValueError("thread_id 不能为空")
    add, remove = _labels(payload)
    if not add and not remove:
        raise ValueError("至少提供 label_ids_add 或 label_ids_remove")
    thread = service.users().threads().modify(userId="me", id=thread_id, body={"addLabelIds": add, "removeLabelIds": remove}).execute()
    return {"status": "thread_labels_modified", "account": account, "thread_id": thread_id, "messages": [str(row.get("id") or "") for row in thread.get("messages") or []]}


def _message_label_alias(service, payload: dict[str, Any], account: str, *, add: list[str] | None = None, remove: list[str] | None = None, status: str) -> dict[str, Any]:
    local = dict(payload)
    local["label_ids_add"] = add or []
    local["label_ids_remove"] = remove or []
    result = _modify_message_labels(service, local, account)
    result["status"] = status
    return result


def _message_state(service, payload: dict[str, Any], account: str, action: str) -> dict[str, Any]:
    message_id = str(payload.get("message_id") or "").strip()
    if not message_id:
        raise ValueError("message_id 不能为空")
    if action == "trash":
        result = service.users().messages().trash(userId="me", id=message_id).execute()
        return {"status": "trashed", "account": account, "message_id": message_id, "label_ids": result.get("labelIds") or []}
    if action == "untrash":
        result = service.users().messages().untrash(userId="me", id=message_id).execute()
        return {"status": "untrashed", "account": account, "message_id": message_id, "label_ids": result.get("labelIds") or []}
    service.users().messages().delete(userId="me", id=message_id).execute()
    return {"status": "deleted_forever", "account": account, "message_id": message_id}


def _batch_delete(service, payload: dict[str, Any], account: str) -> dict[str, Any]:
    message_ids = [str(value).strip() for value in payload.get("message_ids") or [] if str(value).strip()]
    if not message_ids:
        raise ValueError("message_ids 不能为空")
    if len(message_ids) > 1000:
        raise ValueError("message_ids 最多 1000 个")
    service.users().messages().batchDelete(userId="me", body={"ids": message_ids}).execute()
    return {"status": "deleted_forever", "account": account, "count": len(message_ids), "message_ids": message_ids}


def _thread_state(service, payload: dict[str, Any], account: str, action: str) -> dict[str, Any]:
    thread_id = str(payload.get("thread_id") or "").strip()
    if not thread_id:
        raise ValueError("thread_id 不能为空")
    if action == "trash_thread":
        result = service.users().threads().trash(userId="me", id=thread_id).execute()
        return {"status": "thread_trashed", "account": account, "thread_id": thread_id, "message_count": len(result.get("messages") or [])}
    if action == "untrash_thread":
        result = service.users().threads().untrash(userId="me", id=thread_id).execute()
        return {"status": "thread_untrashed", "account": account, "thread_id": thread_id, "message_count": len(result.get("messages") or [])}
    service.users().threads().delete(userId="me", id=thread_id).execute()
    return {"status": "thread_deleted_forever", "account": account, "thread_id": thread_id}


def _profile(service, _payload: dict[str, Any], account: str) -> dict[str, Any]:
    profile = service.users().getProfile(userId="me").execute()
    return {"status": "ok", "account": account, "profile": profile, "oauth_scope": "https://mail.google.com/"}


def _dispatch(payload: dict[str, Any]) -> dict[str, Any]:
    action = str(payload.get("action") or "").strip().lower()
    expected_account = str(payload.get("expected_account") or "").strip()
    interactive_auth = payload.get("interactive_auth", True)
    if type(interactive_auth) is not bool:
        raise ValueError("interactive_auth 必须是布尔值")
    service, account, setup = _service(expected_account, interactive_auth=interactive_auth)
    if setup is not None:
        return setup
    assert service is not None and account is not None

    if action == "profile":
        return _profile(service, payload, account)
    if action == "search":
        return _search(service, payload, account)
    if action == "get_message":
        return _get_message(service, payload, account)
    if action == "get_thread":
        return _get_thread(service, payload, account)
    if action == "download_attachment":
        return _download(service, payload, account)
    if action == "send":
        return _send(service, payload, account)
    if action == "create_draft":
        return _create_draft(service, payload, account)
    if action == "list_drafts":
        return _list_drafts(service, payload, account)
    if action == "get_draft":
        return _get_draft(service, payload, account)
    if action == "update_draft":
        return _update_draft(service, payload, account)
    if action == "send_draft":
        return _send_draft(service, payload, account)
    if action == "delete_draft":
        return _delete_draft(service, payload, account)
    if action == "list_labels":
        return _list_labels(service, payload, account)
    if action == "create_label":
        return _create_label(service, payload, account)
    if action == "update_label":
        return _update_label(service, payload, account)
    if action == "delete_label":
        return _delete_label(service, payload, account)
    if action == "modify_message_labels":
        return _modify_message_labels(service, payload, account)
    if action == "batch_modify_labels":
        return _batch_modify_labels(service, payload, account)
    if action == "modify_thread_labels":
        return _modify_thread_labels(service, payload, account)
    if action == "archive":
        return _message_label_alias(service, payload, account, remove=["INBOX"], status="archived")
    if action == "mark_read":
        return _message_label_alias(service, payload, account, remove=["UNREAD"], status="marked_read")
    if action == "mark_unread":
        return _message_label_alias(service, payload, account, add=["UNREAD"], status="marked_unread")
    if action == "star":
        return _message_label_alias(service, payload, account, add=["STARRED"], status="starred")
    if action == "unstar":
        return _message_label_alias(service, payload, account, remove=["STARRED"], status="unstarred")
    if action in {"trash", "untrash", "delete_forever"}:
        return _message_state(service, payload, account, action)
    if action == "batch_delete_forever":
        return _batch_delete(service, payload, account)
    if action in {"trash_thread", "untrash_thread", "delete_thread_forever"}:
        return _thread_state(service, payload, account, action)
    raise ValueError("不支持的 Gmail action")


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--payload-base64", required=True)
    args = parser.parse_args()
    try:
        raw = base64.b64decode(args.payload_base64).decode("utf-8")
        payload = json.loads(raw)
        if not isinstance(payload, dict):
            raise ValueError("payload 必须是 JSON object")
        result = _dispatch(payload)
        _result(result, 0 if result.get("status") not in {"error"} else 1)
    except SystemExit:
        raise
    except Exception as exc:
        _result({"status": "error", "error": str(exc)}, 1)


if __name__ == "__main__":
    main()
