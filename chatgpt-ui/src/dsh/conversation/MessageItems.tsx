import type { UserAttachment } from "../../event-model.ts";
import { TaskImage } from "./VisualItems.tsx";
import css from "./MessageItem.module.css";
import shellCss from "./Shell.module.css";

function formatAttachmentBytes(bytes?: number): string {
  if (bytes == null || !Number.isFinite(bytes)) return "";
  if (bytes < 1024) return `${bytes} B`;
  if (bytes < 1024 * 1024) return `${(bytes / 1024).toFixed(1)} KB`;
  return `${(bytes / 1024 / 1024).toFixed(1)} MB`;
}

export function UserMessage({ taskId, id, text, attachments = [] }: { taskId: string; id: string; text: string; attachments?: UserAttachment[] }) {
  return (
    <div className={css.userRow} data-agent-item={id} data-testid="user-message">
      <div className={css.userStack}>
        <span className={css.userIdentity}>You</span>
        {attachments.length > 0 ? (
          <div className={css.sentAttachmentList} data-testid="sent-attachment-list">
            {attachments.map((attachment) => (
              <div className={css.sentAttachment} data-attachment-id={attachment.id} key={attachment.id}>
                {attachment.mime.startsWith("image/") ? <TaskImage taskId={taskId} imageId={attachment.id} source="user" label={attachment.name} /> : null}
                <span className={css.sentAttachmentKind}>{attachment.mime.startsWith("image/") ? "图片" : "文件"}</span>
                <span className={css.sentAttachmentName} title={attachment.name}>{attachment.name}</span>
                {attachment.size != null ? <span className={css.sentAttachmentSize}>{formatAttachmentBytes(attachment.size)}</span> : null}
              </div>
            ))}
          </div>
        ) : null}
        {text ? <div className={css.bubble}>{text}</div> : null}
      </div>
    </div>
  );
}

export function AssistantMessage({ id, text }: { id: string; text: string }) {
  return <div className={shellCss.assistant} data-agent-item={id} data-testid="assistant-message">{text}</div>;
}
