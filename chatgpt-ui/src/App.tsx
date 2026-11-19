import { useCallback, useEffect, useRef, useState } from "react";
import type { AgentEventSource } from "./event-model.ts";
import { ConversationRegistry } from "./conversation-registry.ts";
import { MAX_RETAINED_ITEMS, SessionStore } from "./session-store.ts";
import { GPTPcEventSource } from "./sources/gpt-pc-event-source.ts";
import {
  LifecycleScenarioEventSource,
  MockEventSource,
  StressEventSource,
  type LifecycleScenario,
} from "./sources/mock-event-source.ts";
import { ActivityWorkspace, type AgentRelationSnapshot, type ViewerPreferences } from "./dsh/activity/ActivityWorkspace.tsx";
import { ConversationShell } from "./dsh/conversation/ConversationShell.tsx";

const PREFS_KEY = "gpt_pc-pc-activity-viewer:prefs:v1";
const nextPaint = () => new Promise<void>((resolve) => requestAnimationFrame(() => requestAnimationFrame(() => resolve())));

const RELATION_TEST_SNAPSHOT: AgentRelationSnapshot = {
  version: 1,
  updated_at: 1,
  agents: [
    {
      logical_agent_id: "scenario-root",
      short_id: "ROOT01",
      agent_path: "/root",
      parent_agent_id: null,
      runtime_thread_id: "sidebar-beta",
      scope_thread_id: "sidebar-beta",
      display_name: "Scenario root",
      state: "DONE",
    },
    {
      logical_agent_id: "scenario-child",
      short_id: "CHILD1",
      agent_path: "/root/alpha-main",
      parent_agent_id: "scenario-root",
      runtime_thread_id: "sidebar-alpha-main",
      scope_thread_id: "sidebar-beta",
      display_name: "Scenario child",
      state: "DONE",
    },
  ],
  watchdogs: [{
    scope_thread_id: "sidebar-beta",
    enabled: true,
    manual_disabled: false,
    source: "relation-e2e",
    estimated_minutes: 90,
  }],
};

function defaultPreferences(): ViewerPreferences {
  return { aliases: {}, archived: [], deleted: [] };
}

function loadPreferences(): ViewerPreferences {
  try {
    const raw = window.localStorage.getItem(PREFS_KEY);
    if (!raw) return defaultPreferences();
    const value = JSON.parse(raw) as Partial<ViewerPreferences>;
    return {
      selectedTaskId: typeof value.selectedTaskId === "string" ? value.selectedTaskId : undefined,
      aliases: value.aliases && typeof value.aliases === "object" ? value.aliases : {},
      archived: Array.isArray(value.archived) ? value.archived.filter((id): id is string => typeof id === "string") : [],
      deleted: Array.isArray(value.deleted) ? value.deleted.filter((id): id is string => typeof id === "string") : [],
    };
  } catch {
    return defaultPreferences();
  }
}

function savePreferences(preferences: ViewerPreferences): void {
  try { window.localStorage.setItem(PREFS_KEY, JSON.stringify(preferences)); } catch { /* viewer metadata is best-effort */ }
}

function isVisible(taskId: string, preferences: ViewerPreferences, showArchived: boolean): boolean {
  if (preferences.deleted.includes(taskId)) return false;
  const archived = preferences.archived.includes(taskId);
  return showArchived ? archived : !archived;
}

function pickConversation(registry: ConversationRegistry, preferences: ViewerPreferences, showArchived = false): string | null {
  if (!showArchived) {
    const active = registry.active().find((record) => isVisible(record.id, preferences, false));
    if (active) return active.id;
  }
  if (preferences.selectedTaskId && registry.get(preferences.selectedTaskId) && isVisible(preferences.selectedTaskId, preferences, showArchived)) {
    return preferences.selectedTaskId;
  }
  return registry.list().find((record) => isVisible(record.id, preferences, showArchived))?.id ?? null;
}

function syntheticSource(): { source: AgentEventSource; stressCount?: number } | null {
  const params = new URLSearchParams(window.location.search);
  const stress = Number.parseInt(params.get("stress") ?? "0", 10);
  if (stress === 100 || stress === 1000) return { source: new StressEventSource(stress), stressCount: stress };
  if (params.get("mock") === "1") return { source: new MockEventSource() };
  return null;
}

function lifecycleScenario(): LifecycleScenario | null {
  const value = new URLSearchParams(window.location.search).get("scenario");
  const scenarios: LifecycleScenario[] = ["normal", "done-active", "missing-completion", "stale-tool", "active", "no-progress", "phases", "scroll", "sidebar"];
  return scenarios.includes(value as LifecycleScenario) ? value as LifecycleScenario : null;
}

function SyntheticActivityApp({ source, stressCount }: { source: AgentEventSource; stressCount?: number }) {
  const storeRef = useRef(new SessionStore());
  const frameRef = useRef<number | null>(null);
  const mountedRef = useRef(true);
  const [revision, setRevision] = useState(0);

  const renderSoon = useCallback(() => {
    if (frameRef.current !== null) return;
    frameRef.current = requestAnimationFrame(() => {
      frameRef.current = null;
      if (!mountedRef.current) return;
      setRevision((value) => value + 1);
    });
  }, []);

  const markSelfTest = useCallback((status: "idle" | "pass" | "fail", durationMs = 0) => {
    const node = document.querySelector<HTMLElement>("#self-test");
    if (!node) return;
    const rendered = document.querySelectorAll("[data-agent-item]").length;
    node.dataset.status = status;
    node.dataset.rendered = String(rendered);
    node.dataset.processed = String(storeRef.current.processedItemCount);
    node.dataset.durationMs = durationMs.toFixed(1);
    document.body.dataset.selfTest = status;
    document.body.dataset.rendered = String(rendered);
    document.body.dataset.processed = String(storeRef.current.processedItemCount);
  }, []);

  useEffect(() => {
    mountedRef.current = true;
    const controller = new AbortController();
    const store = storeRef.current;
    store.reset();
    markSelfTest("idle");
    const started = performance.now();

    const consume = async () => {
      try {
        for await (const event of source.stream(controller.signal)) {
          if (controller.signal.aborted) return;
          store.apply(event);
          renderSoon();
        }
        renderSoon();
        await nextPaint();
        if (stressCount != null) {
          const rendered = document.querySelectorAll("[data-agent-item]").length;
          const expectedRows = Math.min(stressCount, MAX_RETAINED_ITEMS);
          const passed = store.agentStatus === "Completed" && store.processedItemCount === stressCount && rendered === expectedRows;
          markSelfTest(passed ? "pass" : "fail", performance.now() - started);
        }
      } catch (error) {
        if (!(error instanceof DOMException && error.name === "AbortError")) {
          if (store.taskId) store.apply({ method: "task/completed", params: { task: { id: store.taskId, status: "failed" } } });
          renderSoon();
          markSelfTest("fail", performance.now() - started);
          console.error("[chatgpt-ui] synthetic event source failed", error);
        }
      }
    };
    void consume();
    return () => {
      mountedRef.current = false;
      controller.abort();
      if (frameRef.current !== null) cancelAnimationFrame(frameRef.current);
    };
  }, [markSelfTest, renderSoon, source, stressCount]);

  return <ConversationShell store={storeRef.current} revision={revision} />;
}

function RegistryScenarioApp({ source }: { source: AgentEventSource }) {
  const registryRef = useRef(new ConversationRegistry());
  const [revision, setRevision] = useState(0);
  const [selectedTaskId, setSelectedTaskId] = useState<string | null>(null);
  const preferencesRef = useRef<ViewerPreferences>(defaultPreferences());
  const relationTest = new URLSearchParams(window.location.search).get("relation") === "1";

  useEffect(() => {
    const controller = new AbortController();
    const registry = registryRef.current;
    registry.reset();
    const consume = async () => {
      for await (const event of source.stream(controller.signal)) {
        if (controller.signal.aborted) return;
        const result = registry.apply(event);
        if (event.method === "task/started" && registry.get(event.params.task.id)) setSelectedTaskId(event.params.task.id);
        if (result.bootstrapComplete && !registry.get(event.method === "task/started" ? event.params.task.id : undefined)) {
          setSelectedTaskId(relationTest && registry.get("sidebar-beta")
            ? "sidebar-beta"
            : (registry.list()[0]?.id ?? null));
        }
        setRevision((value) => value + 1);
      }
    };
    void consume();
    return () => controller.abort();
  }, [source, relationTest]);

  return (
    <ActivityWorkspace
      registry={registryRef.current}
      revision={revision}
      selectedTaskId={selectedTaskId}
      preferences={preferencesRef.current}
      showArchived={false}
      onSelect={setSelectedTaskId}
      onShowArchived={() => {}}
      onRename={() => {}}
      onArchive={() => {}}
      onDelete={() => {}}
      agentSnapshotOverride={relationTest ? RELATION_TEST_SNAPSHOT : undefined}
    />
  );
}

function LiveActivityApp() {
  const registryRef = useRef(new ConversationRegistry());
  const frameRef = useRef<number | null>(null);
  const mountedRef = useRef(true);
  const bootstrappedRef = useRef(false);
  const selectedTaskIdRef = useRef<string | null>(null);
  const initialPreferencesRef = useRef<ViewerPreferences | null>(null);
  if (initialPreferencesRef.current === null) initialPreferencesRef.current = loadPreferences();
  const [preferences, setPreferences] = useState<ViewerPreferences>(initialPreferencesRef.current);
  const preferencesRef = useRef(preferences);
  const [revision, setRevision] = useState(0);
  const [selectedTaskId, setSelectedTaskId] = useState<string | null>(null);
  const [showArchived, setShowArchived] = useState(false);

  const commitPreferences = useCallback((next: ViewerPreferences) => {
    preferencesRef.current = next;
    setPreferences(next);
    savePreferences(next);
  }, []);

  const selectTask = useCallback((taskId: string | null, persist = true) => {
    selectedTaskIdRef.current = taskId;
    setSelectedTaskId(taskId);
    if (!persist) return;
    const current = preferencesRef.current;
    const next = { ...current, selectedTaskId: taskId ?? undefined };
    commitPreferences(next);
  }, [commitPreferences]);

  const renderSoon = useCallback(() => {
    if (frameRef.current !== null) return;
    frameRef.current = requestAnimationFrame(() => {
      frameRef.current = null;
      if (!mountedRef.current) return;
      setRevision((value) => value + 1);
      requestAnimationFrame(() => {
        const scroller = document.querySelector<HTMLElement>("[data-conversation-scroll]");
        if (scroller && scroller.scrollHeight - scroller.scrollTop - scroller.clientHeight < 180) scroller.scrollTop = scroller.scrollHeight;
      });
    });
  }, []);

  // Evict cold completed task rows every minute; metadata and disk history survive.
  useEffect(() => {
    const timer = window.setInterval(() => {
      registryRef.current.evictColdDetails(selectedTaskIdRef.current);
    }, 60_000);
    return () => window.clearInterval(timer);
  }, []);

  useEffect(() => {
    if (!selectedTaskId) return;
    let secondFrame: number | null = null;
    const firstFrame = window.requestAnimationFrame(() => {
      secondFrame = window.requestAnimationFrame(() => {
        const scroller = document.querySelector<HTMLElement>("[data-conversation-scroll]");
        if (scroller) scroller.scrollTop = scroller.scrollHeight;
      });
    });
    return () => {
      window.cancelAnimationFrame(firstFrame);
      if (secondFrame !== null) window.cancelAnimationFrame(secondFrame);
    };
  }, [selectedTaskId]);

  useEffect(() => {
    mountedRef.current = true;
    bootstrappedRef.current = false;
    const controller = new AbortController();
    const registry = registryRef.current;
    registry.reset();
    const source = new GPTPcEventSource();

    const consume = async () => {
      try {
        for await (const event of source.stream(controller.signal)) {
          if (controller.signal.aborted) return;
          const result = registry.apply(event);
          if (result.bootstrapComplete) {
            bootstrappedRef.current = true;
            const next = pickConversation(registry, preferencesRef.current, false);
            setShowArchived(false);
            selectTask(next, false);
          } else if (bootstrappedRef.current && event.method === "task/started" && registry.get(event.params.task.id)) {
            setShowArchived(false);
            selectTask(event.params.task.id);
          } else if (bootstrappedRef.current && event.method === "ui/threadMessage" && registry.get(event.params.taskId)?.store.lifecycle === "active") {
            setShowArchived(false);
            selectTask(event.params.taskId);
          }
          renderSoon();
        }
      } catch (error) {
        if (!(error instanceof DOMException && error.name === "AbortError")) console.error("[chatgpt-ui] live event source failed", error);
      }
    };
    void consume();
    return () => {
      mountedRef.current = false;
      controller.abort();
      if (frameRef.current !== null) cancelAnimationFrame(frameRef.current);
    };
  }, [renderSoon, selectTask]);

  const renameTask = useCallback((taskId: string, title: string) => {
    const current = preferencesRef.current;
    commitPreferences({ ...current, aliases: { ...current.aliases, [taskId]: title } });
  }, [commitPreferences]);

  const archiveTask = useCallback((taskId: string, archived: boolean) => {
    const current = preferencesRef.current;
    const set = new Set(current.archived);
    if (archived) set.add(taskId); else set.delete(taskId);
    const next = { ...current, archived: [...set] };
    commitPreferences(next);
    if (selectedTaskId === taskId && archived !== showArchived) selectTask(pickConversation(registryRef.current, next, showArchived), false);
  }, [commitPreferences, selectTask, selectedTaskId, showArchived]);

  const deleteTask = useCallback((taskId: string) => {
    const current = preferencesRef.current;
    const deleted = new Set(current.deleted);
    deleted.add(taskId);
    const next = { ...current, deleted: [...deleted] };
    commitPreferences(next);
    if (selectedTaskId === taskId) selectTask(pickConversation(registryRef.current, next, showArchived), false);
  }, [commitPreferences, selectTask, selectedTaskId, showArchived]);

  const changeArchiveView = useCallback((show: boolean) => {
    setShowArchived(show);
    if (selectedTaskId && isVisible(selectedTaskId, preferencesRef.current, show)) return;
    selectTask(pickConversation(registryRef.current, preferencesRef.current, show), false);
  }, [selectTask, selectedTaskId]);

  return (
    <ActivityWorkspace
      registry={registryRef.current}
      revision={revision}
      selectedTaskId={selectedTaskId}
      preferences={preferences}
      showArchived={showArchived}
      onSelect={(taskId) => selectTask(taskId)}
      onShowArchived={changeArchiveView}
      onRename={renameTask}
      onArchive={archiveTask}
      onDelete={deleteTask}
    />
  );
}

export function App() {
  const scenario = lifecycleScenario();
  if (scenario) return <RegistryScenarioApp source={new LifecycleScenarioEventSource(scenario)} />;
  const synthetic = syntheticSource();
  if (synthetic) return <SyntheticActivityApp source={synthetic.source} stressCount={synthetic.stressCount} />;
  return <LiveActivityApp />;
}
