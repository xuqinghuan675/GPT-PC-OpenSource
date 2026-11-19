import { useRef, useState, type DragEvent } from "react";
import { invoke } from "@tauri-apps/api/core";
import type { VisualContext, VisualDepth } from "../../visual-model.ts";
import { TaskImage } from "./VisualItems.tsx";
import css from "./Visual.module.css";

interface Props {
  taskId: string;
  threadId: string;
  workspace: string;
  context?: VisualContext;
  canEdit: boolean;
}

const MAX_REFERENCE_IMAGES = 16;
const MAX_JOURNEYS = 12;
const SUPPORTED_IMAGE_TYPES = new Set(["image/png", "image/jpeg", "image/webp", "image/gif", "image/bmp"]);
const SUPPORTED_IMAGE_EXTENSION = /\.(?:png|jpe?g|webp|gif|bmp)$/i;

function readFile(file: File): Promise<string> {
  return new Promise((resolve, reject) => {
    const reader = new FileReader();
    const timeout = window.setTimeout(() => { reader.abort(); reject(new Error(`读取 ${file.name} 超时`)); }, 15000);
    reader.onerror = () => { window.clearTimeout(timeout); reject(reader.error ?? new Error("读取参考图片失败")); };
    reader.onabort = () => { window.clearTimeout(timeout); reject(new Error("参考图片读取已中止")); };
    reader.onload = () => {
      window.clearTimeout(timeout);
      const result = String(reader.result ?? "");
      const comma = result.indexOf(",");
      if (comma < 0) reject(new Error("参考图片格式无效"));
      else resolve(result.slice(comma + 1));
    };
    reader.readAsDataURL(file);
  });
}

function supportedImage(file: File): boolean {
  return SUPPORTED_IMAGE_TYPES.has(file.type.toLowerCase()) || (!file.type && SUPPORTED_IMAGE_EXTENSION.test(file.name));
}

function fileKey(file: File): string {
  return `${file.name}\u0000${file.size}\u0000${file.lastModified}`;
}

function formatBytes(bytes: number): string {
  if (bytes < 1024) return `${bytes} B`;
  if (bytes < 1024 * 1024) return `${(bytes / 1024).toFixed(1)} KB`;
  return `${(bytes / (1024 * 1024)).toFixed(1)} MB`;
}

function journeyId(name: string, index: number): string {
  let hash = 2166136261;
  for (let i = 0; i < name.length; i += 1) { hash ^= name.charCodeAt(i); hash = Math.imul(hash, 16777619); }
  return `user-${(hash >>> 0).toString(36)}-${index + 1}`;
}

function parseJourneyLines(value: string, context?: VisualContext) {
  const lines = Array.from(new Set(value.split("\n").map((line) => line.trim()).filter(Boolean)));
  const existing = new Map((context?.journeys ?? []).map((item) => [item.name, item.id]));
  return lines.map((name, index) => ({ id: existing.get(name) ?? journeyId(name, index), name, required: true }));
}

export function VisualReferencePanel({ taskId, threadId, workspace, context, canEdit }: Props) {
  const fileInputRef = useRef<HTMLInputElement>(null);
  const [open, setOpen] = useState(true);
  const [editing, setEditing] = useState(false);
  const [criteria, setCriteria] = useState("");
  const [textReference, setTextReference] = useState("");
  const [visualDepth, setVisualDepth] = useState<VisualDepth>("visual");
  const [journeyText, setJourneyText] = useState("");
  const [keptIds, setKeptIds] = useState<string[]>([]);
  const [files, setFiles] = useState<File[]>([]);
  const [expectedRevision, setExpectedRevision] = useState("");
  const [sending, setSending] = useState(false);
  const [modeSending, setModeSending] = useState(false);
  const [dragActive, setDragActive] = useState(false);
  const [goalEditing, setGoalEditing] = useState(false);
  const [goalOutcome, setGoalOutcome] = useState("");
  const [goalCriteria, setGoalCriteria] = useState("");
  const [goalRequirements, setGoalRequirements] = useState("");
  const [goalChangeNote, setGoalChangeNote] = useState("");
  const [goalReuseVisual, setGoalReuseVisual] = useState(true);
  const [goalDraftEnabled, setGoalDraftEnabled] = useState(false);
  const [goalSending, setGoalSending] = useState(false);
  const [error, setError] = useState("");
  const references = context?.references ?? [];
  const count = references.length;
  const visualUntil = Date.parse(context?.until_at ?? "");
  const userDisabled = context?.user_mode === "disabled";
  const modeOn = !userDisabled && context?.active === true && (context?.user_mode === "enabled" || visualUntil > Date.now());
  const status = userDisabled ? "已关闭" : context?.status === "completed" ? "已验收" : context?.status === "needs_review" ? "待复验" : modeOn ? "进行中" : context?.status === "expired" || context?.active ? "已到期" : "未开启";
  const goal = context?.goal;
  const goalOn = goal?.active === true;
  const goalStatus = goalOn ? "进行中" : goal?.outcome ? "已配置" : "未设置";
  const depthLabel = context?.depth === "frontend" ? "前端验收" : context?.depth === "interactive" ? "交互验收" : "看图";
  const journeyCount = (context?.journeys ?? []).filter((item) => item.required).length;
  const panelId = `visual-reference-${taskId}`;

  function beginEdit(): void {
    setOpen(true);
    setGoalEditing(false);
    setEditing(true);
    setCriteria((context?.criteria ?? []).join("\n"));
    setTextReference("");
    setVisualDepth(context?.depth ?? "visual");
    setJourneyText((context?.journeys ?? []).filter((item) => item.required).map((item) => item.name).join("\n"));
    setKeptIds(references.map((item) => item.id));
    setExpectedRevision(context?.updated_at ?? "");
    setFiles([]);
    setDragActive(false);
    setError("");
    if (fileInputRef.current) fileInputRef.current.value = "";
  }

  function beginGoalEdit(): void {
    setOpen(true);
    setEditing(false);
    setGoalEditing(true);
    setGoalOutcome(goal?.outcome ?? "");
    setGoalCriteria((goal?.success_criteria ?? []).join("\n"));
    setGoalRequirements((goal?.requirements ?? []).join("\n"));
    setGoalChangeNote("");
    setGoalReuseVisual(goal?.reuse_visual_context !== false);
    setGoalDraftEnabled(goalOn);
    setError("");
  }

  function addReferenceFiles(nextFiles: File[]): void {
    if (!nextFiles.length) return;
    const rejected = nextFiles.filter((file) => !supportedImage(file));
    if (rejected.length) {
      setError(`仅支持 PNG/JPEG/WebP/GIF/BMP 图片：${rejected.slice(0, 3).map((file) => file.name).join("、")}`);
      return;
    }
    setError("");
    setFiles((current) => {
      const seen = new Set(current.map(fileKey));
      const merged = [...current];
      for (const file of nextFiles) {
        const key = fileKey(file);
        if (!seen.has(key) && merged.length < MAX_REFERENCE_IMAGES) {
          seen.add(key);
          merged.push(file);
        }
      }
      return merged;
    });
  }

  function removeReferenceFile(key: string): void {
    setFiles((current) => current.filter((file) => fileKey(file) !== key));
  }

  function onReferenceDrop(event: DragEvent<HTMLButtonElement>): void {
    event.preventDefault();
    setDragActive(false);
    if (!sending && canEdit) addReferenceFiles(Array.from(event.dataTransfer.files));
  }


  async function toggleMode(): Promise<void> {
    if (modeSending || !canEdit) return;
    setModeSending(true);
    setError("");
    try {
      await invoke("activity_visual_toggle", {
        taskId,
        threadId,
        workspace,
        expectedRevision: context?.updated_at ?? "",
        enabled: !modeOn,
      });
    } catch (reason: unknown) {
      setError(reason instanceof Error ? reason.message : String(reason));
    } finally {
      setModeSending(false);
    }
  }

  async function submitGoal(enabled: boolean, outcome: string, criteriaLines: string[], requirementLines: string[], changeNote: string, reuseVisual: boolean): Promise<void> {
    await invoke("activity_goal_update", {
      taskId,
      threadId,
      workspace,
      goalExpectedRevision: goal?.updated_at ?? "",
      enabled,
      outcome,
      successCriteria: criteriaLines,
      goalRequirements: requirementLines,
      goalChangeNote: changeNote,
      reuseVisualContext: reuseVisual,
    });
  }

  async function toggleGoal(): Promise<void> {
    if (goalSending || !canEdit) return;
    if (!goalOn && !goal?.outcome?.trim()) {
      beginGoalEdit();
      setGoalDraftEnabled(true);
      setError("先填写目标结果和成功标准，再开启目标模式。");
      return;
    }
    setGoalSending(true);
    setError("");
    try {
      await submitGoal(!goalOn, goal?.outcome ?? "", goal?.success_criteria ?? [], goal?.requirements ?? [],
        goalOn ? "用户关闭目标模式" : "用户开启目标模式", goal?.reuse_visual_context !== false);
    } catch (reason: unknown) {
      setError(reason instanceof Error ? reason.message : String(reason));
    } finally {
      setGoalSending(false);
    }
  }

  async function saveGoal(): Promise<void> {
    if (goalSending || !canEdit) return;
    const cleanOutcome = goalOutcome.trim();
    const lines = goalCriteria.split("\n").map((line) => line.trim()).filter(Boolean);
    const requirements = goalRequirements.split("\n").map((line) => line.trim()).filter(Boolean);
    const changeNote = goalChangeNote.trim();
    if (cleanOutcome.length > 8000) { setError("目标结果最多 8000 字符"); return; }
    if (goalDraftEnabled && !cleanOutcome) { setError("开启目标模式前必须填写目标结果"); return; }
    if (lines.length > 64 || lines.some((line) => line.length > 2000)) { setError("成功标准最多 64 条，每条最多 2000 字符"); return; }
    if (requirements.length > 64 || requirements.some((line) => line.length > 2000)) { setError("持续要求最多 64 条，每条最多 2000 字符"); return; }
    if (changeNote.length > 2000) { setError("本次修正说明最多 2000 字符"); return; }
    setGoalSending(true);
    setError("");
    try {
      await submitGoal(goalDraftEnabled, cleanOutcome, lines, requirements, changeNote, goalReuseVisual);
      setGoalEditing(false);
    } catch (reason: unknown) {
      setError(reason instanceof Error ? reason.message : String(reason));
    } finally {
      setGoalSending(false);
    }
  }

  async function save(): Promise<void> {
    if (sending || !canEdit) return;
    setError("");
    const lines = criteria.split("\n").map((line) => line.trim()).filter(Boolean);
    const journeys = parseJourneyLines(journeyText, context);
    if (lines.length > 64 || lines.some((line) => line.length > 2000)) { setError("最多 64 条要求，每条最多 2000 字符"); return; }
    if (journeys.length > MAX_JOURNEYS || journeys.some((item) => item.name.length > 160)) { setError("关键交互最多 12 条，每条最多 160 字符"); return; }
    if (visualDepth !== "visual" && journeys.length === 0) { setError("交互验收和完整前端至少需要 1 条关键交互 Journey"); return; }
    if (keptIds.length + files.length + (textReference.trim() ? 1 : 0) > 16) { setError("最多 16 项参考资料"); return; }
    if (files.some((file) => file.size > 25 * 1024 * 1024) || files.reduce((total, file) => total + file.size, 0) > 50 * 1024 * 1024) {
      setError("参考图片每张最多 25 MB，本次上传总计最多 50 MB"); return;
    }
    setSending(true);
    try {
      const images = await Promise.all(files.map(async (file) => ({ name: file.name, data_base64: await readFile(file) })));
      await invoke("activity_visual_update", { taskId, threadId, workspace, expectedRevision,
        keepReferenceIds: keptIds, criteria: lines, textReference: textReference.trim(), images,
        depth: visualDepth, journeys });
      setEditing(false);
      setFiles([]);
      if (fileInputRef.current) fileInputRef.current.value = "";
    } catch (reason: unknown) {
      setError(reason instanceof Error ? reason.message : String(reason));
    } finally { setSending(false); }
  }

  return <section className={css.panel} data-testid="visual-reference-panel" aria-label="任务目标与视觉参考资料">
    <div className={css.panelSummary}>
      <button type="button" aria-expanded={open} aria-controls={panelId} onClick={() => setOpen(!open)}>
        <span aria-hidden="true">{open ? "▾" : "▸"}</span>任务控制
      </button>
      <span className={css.panelMeta}>目标 {goalStatus} · {depthLabel} {status}{journeyCount ? ` · 旅程 ${journeyCount}` : ""} · 参考 {count} 项{context?.iteration ? ` · 观察 ${context.iteration} 次` : ""}</span>
    </div>
    {open ? <div className={css.panelBody} id={panelId}>
      <div className={css.modeGrid}>
        <section className={`${css.modeCard} ${goalEditing ? css.modeCardExpanded : ""}`} data-active={goalOn || undefined} data-testid="goal-mode-card">
          <div className={css.modeHeader}>
            <div className={css.modeIdentity}>
              <span className={css.modeIcon} aria-hidden="true">◎</span>
              <div><strong>目标模式</strong><span>持续围绕结果与成功标准推进</span></div>
              <span className={css.statusBadge} data-active={goalOn || undefined}>{goalStatus}</span>
            </div>
            <div className={css.modeActions}>
              {canEdit ? <button type="button" data-testid="goal-mode-toggle" data-primary={goalOn || undefined} aria-pressed={goalOn} disabled={goalSending}
                onClick={() => void toggleGoal()}>{goalSending ? "处理中…" : goalOn ? "关闭" : "开启"}</button> : null}
              {canEdit && !goalEditing ? <button type="button" onClick={beginGoalEdit}>编辑</button> : null}
            </div>
          </div>
          {goalEditing ? <div className={css.formGrid}>
            <label className={css.formField}>目标结果
              <textarea value={goalOutcome} onChange={(event) => setGoalOutcome(event.target.value)} disabled={goalSending} maxLength={8000} rows={2}
                placeholder="描述最终要达成的结果，而不是下一步动作。" />
            </label>
            <label className={css.formField}>成功标准，每行一条
              <textarea value={goalCriteria} onChange={(event) => setGoalCriteria(event.target.value)} disabled={goalSending} maxLength={128000} rows={3}
                placeholder="例如：核心测试全部通过；真实流程完成；关键视觉验收无 Major 差异。" />
            </label>
            <label className={css.formField}>持续要求，每行一条
              <textarea value={goalRequirements} onChange={(event) => setGoalRequirements(event.target.value)} disabled={goalSending} maxLength={128000} rows={3}
                placeholder="例如：不要重做已有结果；所有修改都要回归；用户后续修正优先于旧计划。" />
            </label>
            <label className={css.formField}>本次修正说明（可选）
              <textarea value={goalChangeNote} onChange={(event) => setGoalChangeNote(event.target.value)} disabled={goalSending} maxLength={2000} rows={2}
                placeholder="说明这次为什么调整。保存后会作为一条新的用户修正发送给当前 Agent。" />
            </label>
            <div className={css.compactChecks}>
              <label><input type="checkbox" checked={goalReuseVisual} disabled={goalSending}
                onChange={(event) => setGoalReuseVisual(event.target.checked)} />复用看图参考、要求与观察证据</label>
              <label><input type="checkbox" checked={goalDraftEnabled} disabled={goalSending}
                onChange={(event) => setGoalDraftEnabled(event.target.checked)} />保存后启用目标模式</label>
            </div>
            <div className={`${css.actions} ${css.spanAll}`}>
              <button type="button" className={css.primary} disabled={goalSending || !canEdit} onClick={() => void saveGoal()}>{goalSending ? "保存中…" : "保存目标"}</button>
              <button type="button" disabled={goalSending} onClick={() => { setGoalEditing(false); setError(""); }}>取消</button>
            </div>
          </div> : <div className={css.modeSummary}>
            {goal?.outcome ? <p className={css.summaryText}>{goal.outcome}</p> : <p className={css.emptyCompact}>尚未设置目标。填写结果和成功标准后可一键开启。</p>}
            <div className={css.summaryStats}>
              <span>版本 <b>r{goal?.revision ?? 0}</b></span>
              <span>标准 <b>{goal?.success_criteria.length ?? 0}</b></span>
              <span>持续要求 <b>{goal?.requirements.length ?? 0}</b></span>
              <span>视觉证据 <b>{goal?.reuse_visual_context === false ? "独立" : "复用"}</b></span>
            </div>
            {goal?.requirements.length ? <ul className={css.compactList}>{Array.from(new Set(goal.requirements)).slice(0, 3).map((line) => <li key={line}>持续 · {line}</li>)}</ul> : null}
            {goal?.success_criteria.length ? <ul className={css.compactList}>{Array.from(new Set(goal.success_criteria)).slice(0, 2).map((line) => <li key={line}>{line}</li>)}</ul> : null}
            {goal?.corrections.length ? <ul className={css.compactList} data-testid="goal-corrections">{goal.corrections.slice(-3).reverse().map((item) =>
              <li key={`${item.revision}-${item.at ?? ""}`}>r{item.revision} · {item.source} · {item.note}</li>)}</ul> : null}
          </div>}
        </section>

        <section className={`${css.modeCard} ${editing ? css.modeCardExpanded : ""}`} data-active={modeOn || undefined} data-testid="visual-mode-card">
          <div className={css.modeHeader}>
            <div className={css.modeIdentity}>
              <span className={css.modeIcon} aria-hidden="true">◫</span>
              <div><strong>看图模式</strong><span>{depthLabel === "看图" ? "参考图、验收要求与截图闭环" : `${depthLabel}：视觉 + 真实 UI Journey`}</span></div>
              <span className={css.statusBadge} data-active={modeOn || undefined}>{status}</span>
            </div>
            <div className={css.modeActions}>
              {canEdit ? <button type="button" data-testid="visual-mode-toggle" data-primary={modeOn || undefined} aria-pressed={modeOn} disabled={modeSending || sending}
                onClick={() => void toggleMode()}>{modeSending ? "切换中…" : modeOn ? "关闭" : "开启"}</button> : null}
              {canEdit && !editing ? <button type="button" onClick={beginEdit}>验收设置</button> : null}
            </div>
          </div>
          {editing ? <div className={css.formGrid}>
            <div className={`${css.panelMeta} ${css.spanAll}`}>验收配置与参考资料固定到当前任务；Journey 直接写用户流程，不需要填写 Playwright selector 或坐标。</div>
            <label className={css.formField}>验收深度
              <select aria-label="前端验收深度" value={visualDepth} disabled={sending}
                onChange={(event) => setVisualDepth(event.target.value as VisualDepth)}>
                <option value="visual">仅视觉</option>
                <option value="interactive">视觉 + 交互</option>
                <option value="frontend">完整前端</option>
              </select>
              <span className={css.panelMeta}>{visualDepth === "visual" ? "只验视觉与响应式" : visualDepth === "interactive" ? "额外执行关键用户流程" : "用户流程 + page / console / network 健康门"}</span>
            </label>
            {visualDepth !== "visual" ? <label className={`${css.formField} ${css.spanAll}`}>关键交互 Journey，每行一条
              <textarea data-testid="visual-journeys" value={journeyText} onChange={(event) => setJourneyText(event.target.value)} disabled={sending} rows={5}
                placeholder={"例如：\n拖拽产品地图后仍可点击产品\n滚轮缩放产品地图后节点与标签保持可用\n搜索产品并打开研究面板\n从产品进入完整产业链并选择链内位置\n调整研究侧栏宽度后主舞台不溢出"} />
              <span className={css.panelMeta}>最多 12 条。你只写“要完成什么”；GPT 负责把它转换成语义点击、拖拽、滚轮和断言。</span>
            </label> : null}
            {references.length ? <div className={`${css.keepList} ${css.spanAll}`}>{references.map((reference) => <label className={css.keepReference} key={reference.id}>
              <input type="checkbox" checked={keptIds.includes(reference.id)} disabled={sending}
                onChange={(event) => setKeptIds((ids) => event.target.checked ? [...ids, reference.id] : ids.filter((id) => id !== reference.id))} />
              <span>保留 {reference.name}</span>
            </label>)}</div> : null}
            <label className={css.formField}>验收要求，每行一条
              <textarea value={criteria} onChange={(event) => setCriteria(event.target.value)} disabled={sending} maxLength={128000} rows={3}
                placeholder="例如：Desktop 1440×900 无横向溢出；Mobile 390×844 导航不遮挡正文" />
            </label>
            <label className={css.formField}>新增文字参考（可选）
              <textarea value={textReference} onChange={(event) => setTextReference(event.target.value)} disabled={sending} maxLength={8000} rows={3}
                placeholder="补充设计语义、参考说明或必须遵守的视觉要求…" />
            </label>
            <input ref={fileInputRef} className={css.hiddenFile} type="file"
              accept="image/png,image/jpeg,image/webp,image/gif,image/bmp" multiple disabled={sending}
              onChange={(event) => { addReferenceFiles(Array.from(event.target.files ?? [])); event.target.value = ""; }} />
            <button
              className={`${css.dropZone} ${css.spanAll} ${dragActive ? css.dropZoneActive : ""}`}
              data-testid="visual-reference-dropzone"
              type="button"
              disabled={!canEdit || sending}
              onClick={() => fileInputRef.current?.click()}
              onDragEnter={(event) => { event.preventDefault(); if (canEdit && !sending) setDragActive(true); }}
              onDragOver={(event) => { event.preventDefault(); if (canEdit && !sending) setDragActive(true); }}
              onDragLeave={() => setDragActive(false)}
              onDrop={onReferenceDrop}
            >
              <strong>{dragActive ? "松开即可加入参考图" : "拖入参考图片"}</strong>
              <span>或点击选择 PNG / JPEG / WebP / GIF / BMP</span>
            </button>
            {files.length ? <div className={`${css.pendingFiles} ${css.spanAll}`} data-testid="visual-reference-pending-files">
              {files.map((file) => <div className={css.pendingFile} key={fileKey(file)}>
                <span className={css.pendingFileName} title={file.name}>{file.name}</span>
                <span>{formatBytes(file.size)}</span>
                <button type="button" disabled={sending} aria-label={`移除 ${file.name}`} onClick={() => removeReferenceFile(fileKey(file))}>×</button>
              </div>)}
            </div> : null}
            <div className={`${css.actions} ${css.spanAll}`}>
              <button type="button" className={css.primary} disabled={sending || !canEdit} onClick={() => void save()}>{sending ? "保存中…" : "保存参考资料"}</button>
              <button type="button" disabled={sending} onClick={() => { setEditing(false); setFiles([]); setDragActive(false); setError(""); }}>取消</button>
            </div>
          </div> : <div className={css.modeSummary}>
            <div className={css.summaryStats}>
              <span>模式 <b>{depthLabel}</b></span>
              <span>Journey <b>{journeyCount}</b></span>
              <span>参考 <b>{count}</b></span>
              <span>要求 <b>{context?.criteria.length ?? 0}</b></span>
              <span>观察 <b>{context?.iteration ?? 0}</b></span>
            </div>
            {count ? <div className={css.referenceGrid}>{references.slice(0, 4).map((reference) => <figure className={css.reference} key={reference.id}>
              {reference.kind === "image" ? <TaskImage compact source="visual" taskId={taskId} imageId={reference.id}
                label={`Reference · ${reference.name}`} width={reference.width} height={reference.height} />
                : <div className={css.referenceText}>{reference.text}</div>}
              <figcaption>{reference.name}</figcaption>
            </figure>)}</div> : <p className={css.emptyCompact}>暂无参考资料。可拖入图片、选择文件或填写文字要求。</p>}
            {journeyCount ? <ul className={css.compactList}>{(context?.journeys ?? []).filter((item) => item.required).slice(0, 3).map((item) => <li key={item.id}>{item.name}</li>)}</ul> : null}
            {context?.criteria.length ? <ul className={css.compactList}>{Array.from(new Set(context.criteria)).slice(0, 3).map((line) => <li key={line}>{line}</li>)}</ul> : null}
          </div>}
        </section>
      </div>
      {error ? <p className={css.error} role="alert">{error}</p> : null}
    </div> : null}
  </section>;
}
