import { useEffect, useRef, useState } from "react";
import { invoke } from "@tauri-apps/api/core";
import type { VisualObservation } from "../../visual-model.ts";
import css from "./Visual.module.css";

interface ImageProps {
  taskId: string;
  imageId: string;
  source: "user" | "visual";
  label: string;
  width?: number;
  height?: number;
  compact?: boolean;
}

let imagesInFlight = 0;
const imageQueue: Array<() => void> = [];
function drainImageQueue(): void {
  while (imagesInFlight < 2 && imageQueue.length) imageQueue.shift()?.();
}
function readTaskImage(args: Record<string, unknown>, cancelled: () => boolean): Promise<string> {
  return new Promise((resolve, reject) => {
    imageQueue.push(() => {
      if (cancelled()) { resolve(""); return; }
      imagesInFlight += 1;
      void invoke<string>("activity_image", args).then(resolve, reject).finally(() => {
        imagesInFlight -= 1;
        drainImageQueue();
      });
    });
    drainImageQueue();
  });
}

// Load only visible images. Never accept an arbitrary local path or event-provided URL.
export function TaskImage({ taskId, imageId, source, label, width, height, compact }: ImageProps) {
  const container = useRef<HTMLDivElement>(null);
  const [visible, setVisible] = useState(false);
  const [url, setUrl] = useState("");
  const [error, setError] = useState("");
  const [naturalSize, setNaturalSize] = useState<[number, number] | undefined>();
  useEffect(() => {
    const node = container.current;
    if (!node) return;
    const observer = new IntersectionObserver((entries) => {
      setVisible(entries.some((entry) => entry.isIntersecting));
    }, { rootMargin: "160px" });
    observer.observe(node);
    return () => observer.disconnect();
  }, []);
  useEffect(() => {
    if (!visible) { setUrl(""); return; }
    let cancelled = false;
    setUrl("");
    setError("");
    void readTaskImage({ taskId, imageId, source, thumbnail: Boolean(compact) }, () => cancelled).then((value) => {
      if (!cancelled) setUrl(value);
    }).catch((reason: unknown) => {
      if (!cancelled) setError(reason instanceof Error ? reason.message : String(reason));
    });
    return () => { cancelled = true; };
  }, [visible, taskId, imageId, source, compact]);
  const ratio = width && height ? `${width} / ${height}` : naturalSize ? `${naturalSize[0]} / ${naturalSize[1]}` : undefined;
  return <div ref={container} className={compact ? css.thumbnail : css.imageFrame} data-image-source={source} data-image-id={imageId}
    style={ratio ? { aspectRatio: ratio } : undefined}>
    {url ? <img src={url} alt={label} width={width} height={height} loading="lazy" decoding="async"
      onLoad={(event) => setNaturalSize([event.currentTarget.naturalWidth, event.currentTarget.naturalHeight])}
      onError={() => setError("图片解码失败，原始文件可能损坏")} /> : null}
    {!url && !error ? <span className={css.imagePlaceholder}>{visible ? "读取图片…" : "图片"}</span> : null}
    {error ? <span className={css.imageError} role="status">{error}</span> : null}
  </div>;
}

export function AgentObservation({ taskId, observation }: { taskId: string; observation: VisualObservation }) {
  return <article className={css.observation} data-agent-item={observation.id} data-testid="agent-observation">
    <div className={css.observationHeader}>
      <span className={css.identity}>GPT 视觉观察 #{observation.iteration}</span>
      <span className={css.sourceLabel}>{["browser", "window", "region", "screen"].includes(observation.capture_type ?? "") ? "GPT Screenshot" : "Agent Observation"}</span>
    </div>
    <div className={css.observationLabel}>{observation.label || observation.name}</div>
    <TaskImage taskId={taskId} imageId={observation.id} source="visual" label={observation.label || observation.name}
      width={observation.width} height={observation.height} />
    <div className={css.evidenceMeta}>{observation.width} × {observation.height} · Agent 自行取得的观察，不是用户输入</div>
  </article>;
}
