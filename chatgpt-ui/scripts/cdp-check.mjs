const endpoint = process.env.CDP_ENDPOINT ?? "http://127.0.0.1:9222";
const sleep = (ms) => new Promise((resolve) => setTimeout(resolve, ms));

async function findTarget() {
  const response = await fetch(`${endpoint}/json`);
  if (!response.ok) throw new Error(`CDP target list failed: ${response.status}`);
  const targets = await response.json();
  const target = targets.find((item) => item.type === "page" && (
    item.title === "GPT-PC" || item.title === "chatgpt-ui" || item.url.includes("127.0.0.1:1420") || item.url.includes("tauri")
  ));
  if (!target) throw new Error(`No GPT-PC viewer target. Targets: ${JSON.stringify(targets)}`);
  return target;
}

class CdpClient {
  constructor(url) {
    this.ws = new WebSocket(url);
    this.nextId = 1;
    this.pending = new Map();
    this.exceptions = [];
    this.logErrors = [];
  }
  async open() {
    await new Promise((resolve, reject) => {
      this.ws.addEventListener("open", resolve, { once: true });
      this.ws.addEventListener("error", reject, { once: true });
    });
    this.ws.addEventListener("message", (event) => {
      const message = JSON.parse(String(event.data));
      if (message.id) {
        const pending = this.pending.get(message.id);
        if (!pending) return;
        this.pending.delete(message.id);
        if (message.error) pending.reject(new Error(JSON.stringify(message.error)));
        else pending.resolve(message.result);
      } else if (message.method === "Runtime.exceptionThrown") {
        this.exceptions.push(message.params?.exceptionDetails?.text ?? "Runtime exception");
      } else if (message.method === "Log.entryAdded" && message.params?.entry?.level === "error") {
        const entry = message.params.entry;
        const favicon404 = entry?.url?.endsWith("/favicon.ico") && entry?.text?.includes("404");
        if (!favicon404) this.logErrors.push(entry?.text ?? "Log error");
      }
    });
    await this.call("Runtime.enable");
    await this.call("Log.enable");
    await this.call("Page.enable");
  }
  call(method, params = {}) {
    const id = this.nextId++;
    return new Promise((resolve, reject) => {
      this.pending.set(id, { resolve, reject });
      this.ws.send(JSON.stringify({ id, method, params }));
    });
  }
  async evaluate(expression) {
    const result = await this.call("Runtime.evaluate", { expression, returnByValue: true, awaitPromise: true });
    if (result.exceptionDetails) throw new Error(result.exceptionDetails.text ?? "Evaluation failed");
    return result.result?.value;
  }
  async waitFor(expression, timeoutMs = 20_000) {
    const deadline = Date.now() + timeoutMs;
    while (Date.now() < deadline) {
      if (await this.evaluate(expression)) return;
      await sleep(80);
    }
    throw new Error(`Timed out waiting for: ${expression}`);
  }
  close() { this.ws.close(); }
}

const target = await findTarget();
const baseUrl = target.url.replace(/[?#].*$/, "");
const cdp = new CdpClient(target.webSocketDebuggerUrl);
await cdp.open();
const result = {};

async function navigate(query) {
  await cdp.call("Page.navigate", { url: `${baseUrl}${query}` });
  await cdp.waitFor(`document.querySelector('[data-testid="dsh-conversation-root"]') !== null`);
}

async function scenario(name, lifecycle) {
  await navigate(`?scenario=${name}`);
  const id = `scenario-${name}`;
  await cdp.waitFor(`document.querySelector('[data-testid="dsh-conversation-root"]')?.dataset.taskId === ${JSON.stringify(id)} && document.querySelector('[data-testid="dsh-conversation-root"]')?.dataset.lifecycle === ${JSON.stringify(lifecycle)}`);
  await sleep(50);
  return cdp.evaluate(`(() => {
    const root = document.querySelector('[data-testid="dsh-conversation-root"]');
    const session = document.querySelector('[data-testid="conversation-sidebar"] [data-task-id=${JSON.stringify(id)}]');
    return {
      lifecycle: root?.dataset.lifecycle,
      agentStatus: root?.dataset.agentStatus,
      activityStatus: root?.dataset.activityStatus,
      statusText: document.querySelector('[data-testid="agent-status"]')?.textContent,
      leaseMeta: document.querySelector('[data-testid="lease-meta"]')?.textContent,
      sidebarLifecycle: session?.dataset.lifecycle,
      sidebarDot: session?.querySelector('[data-state]')?.dataset.state,
      reasoning: document.querySelectorAll('[data-testid="reasoning-summary"]').length,
      reasoningText: Array.from(document.querySelectorAll('[data-testid="reasoning-summary"]')).map((node) => node.textContent || ''),
      phases: Array.from(document.querySelectorAll('[data-testid="reasoning-summary"]')).map((node) => node.dataset.phase),
      rows: document.querySelectorAll('[data-agent-item]').length,
      runningRows: document.querySelectorAll('[data-agent-item][data-state="running"]').length,
      stoppedRows: document.querySelectorAll('[data-agent-item][data-state="stopped"]').length,
      rowText: Array.from(document.querySelectorAll('[data-agent-item]')).map((node) => node.textContent || ''),
      composer: !!document.querySelector('[data-testid="composer"]'),
      composerDisabled: document.querySelector('[data-testid="composer"]')?.hasAttribute('data-disabled'),
      composerInViewport: (() => {
        const composer = document.querySelector('[data-testid="ui-composer"]');
        const rootRect = root?.getBoundingClientRect();
        const composerRect = composer?.getBoundingClientRect();
        return !!rootRect && !!composerRect && composerRect.height > 40 && composerRect.top >= rootRect.top - 1 && composerRect.bottom <= rootRect.bottom + 1;
      })(),
      send: !!document.querySelector('[data-testid="send-button"]'),
      sendDisabled: document.querySelector('[data-testid="send-button"]')?.disabled
    };
  })()`);
}

async function sidebarAcceptance() {
  await navigate("?scenario=sidebar");
  await cdp.waitFor(`document.querySelector('[data-testid="conversation-sidebar"] [data-task-id="sidebar-beta"]') !== null`);
  await sleep(80);
  const grouped = await cdp.evaluate(`(() => {
    const sidebar = document.querySelector('[data-testid="conversation-sidebar"]');
    const sections = sidebar ? Array.from(sidebar.querySelectorAll('section')) : [];
    return {
      groups: sections.length,
      sessions: sidebar?.querySelectorAll('[data-task-id]').length ?? 0,
      groupNames: sections.map((section) => section.querySelector(':scope > div span')?.textContent || ''),
      groupCounts: sections.map((section) => Number(section.querySelector(':scope > div span:last-child')?.textContent || 0)),
      taskIds: sidebar ? Array.from(sidebar.querySelectorAll('[data-task-id]')).map((node) => node.dataset.taskId) : [],
      noiseVisible: !!sidebar?.querySelector('[data-task-id^="sidebar-noise"]'),
      selectedTaskId: document.querySelector('[data-testid="dsh-conversation-root"]')?.dataset.taskId ?? null,
      toggleText: document.querySelector('[data-testid="sidebar-sort-toggle"]')?.textContent,
    };
  })()`);

  await cdp.evaluate(`document.querySelector('[data-testid="sidebar-sort-toggle"]')?.click()`);
  await cdp.waitFor(`document.querySelector('[data-testid="sidebar-sort-toggle"]')?.dataset.mode === 'time' && document.querySelector('[data-testid="sidebar-time-list"]') !== null`);
  const time = await cdp.evaluate(`(() => {
    const sidebar = document.querySelector('[data-testid="conversation-sidebar"]');
    const list = document.querySelector('[data-testid="sidebar-time-list"]');
    return {
      groups: sidebar?.querySelectorAll('section').length ?? 0,
      taskIds: list ? Array.from(list.querySelectorAll('[data-task-id]')).map((node) => node.dataset.taskId) : [],
      toggleText: document.querySelector('[data-testid="sidebar-sort-toggle"]')?.textContent,
    };
  })()`);

  await cdp.evaluate(`document.querySelector('[data-testid="sidebar-sort-toggle"]')?.click()`);
  await cdp.waitFor(`document.querySelector('[data-testid="sidebar-sort-toggle"]')?.dataset.mode === 'workspace' && document.querySelectorAll('[data-testid="conversation-sidebar"] section').length === 6`);
  const restored = await cdp.evaluate(`(() => ({
    groups: document.querySelectorAll('[data-testid="conversation-sidebar"] section').length,
    taskIds: Array.from(document.querySelectorAll('[data-testid="conversation-sidebar"] [data-task-id]')).map((node) => node.dataset.taskId),
    toggleText: document.querySelector('[data-testid="sidebar-sort-toggle"]')?.textContent,
  }))()`);

  const scrollBefore = await cdp.evaluate(`(() => {
    const scroll = document.querySelector('[data-testid="sidebar-scroll"]');
    if (!scroll) return null;
    scroll.style.maxHeight = '140px';
    scroll.scrollTop = 0;
    const rect = scroll.getBoundingClientRect();
    return {
      clientHeight: scroll.clientHeight,
      scrollHeight: scroll.scrollHeight,
      overflowY: getComputedStyle(scroll).overflowY,
      x: rect.left + rect.width / 2,
      y: rect.top + rect.height / 2,
    };
  })()`);
  if (scrollBefore) {
    await cdp.call("Input.dispatchMouseEvent", { type: "mouseMoved", x: scrollBefore.x, y: scrollBefore.y });
    await cdp.call("Input.dispatchMouseEvent", { type: "mouseWheel", x: scrollBefore.x, y: scrollBefore.y, deltaX: 0, deltaY: 240 });
    await sleep(150);
  }
  const scrollAfter = await cdp.evaluate(`(() => {
    const scroll = document.querySelector('[data-testid="sidebar-scroll"]');
    return scroll ? { scrollTop: scroll.scrollTop, scrollHeight: scroll.scrollHeight, clientHeight: scroll.clientHeight } : null;
  })()`);

  return { grouped, time, restored, scroll: { before: scrollBefore, after: scrollAfter } };
}

async function verticalScrollAcceptance() {
  await navigate("?scenario=scroll");
  await cdp.waitFor(`document.querySelectorAll('[data-agent-item]').length === 120 && document.querySelector('[data-testid="dsh-conversation-root"]')?.dataset.lifecycle === 'completed'`);
  await sleep(100);
  const before = await cdp.evaluate(`(() => {
    const workspace = document.querySelector('[data-testid="activity-workspace"]');
    const main = workspace?.querySelector(':scope > section');
    const root = document.querySelector('[data-testid="dsh-conversation-root"]');
    const body = document.querySelector('[data-conversation-body]');
    const scroll = document.querySelector('[data-conversation-scroll]');
    const transcript = document.querySelector('[data-testid="conversation-transcript"]');
    const info = (node) => node ? ({ clientHeight: node.clientHeight, scrollHeight: node.scrollHeight, height: node.getBoundingClientRect().height, overflowY: getComputedStyle(node).overflowY }) : null;
    if (scroll) scroll.scrollTop = 0;
    return { workspace: info(workspace), main: info(main), root: info(root), body: info(body), scroll: info(scroll), transcript: info(transcript), scrollTop: scroll?.scrollTop ?? null };
  })()`);
  const programmatic = await cdp.evaluate(`(() => {
    const scroll = document.querySelector('[data-conversation-scroll]');
    if (!scroll) return null;
    const max = scroll.scrollHeight - scroll.clientHeight;
    scroll.scrollTop = max;
    return { max, scrollTop: scroll.scrollTop, clientHeight: scroll.clientHeight, scrollHeight: scroll.scrollHeight };
  })()`);
  await sleep(60);
  await cdp.evaluate(`document.querySelector('[data-conversation-scroll]').scrollTop = 0`);
  const rect = await cdp.evaluate(`(() => { const r = document.querySelector('[data-conversation-scroll]').getBoundingClientRect(); return { x: r.left + r.width / 2, y: r.top + r.height / 2 }; })()`);
  await cdp.call("Input.dispatchMouseEvent", { type: "mouseMoved", x: rect.x, y: rect.y });
  await cdp.call("Input.dispatchMouseEvent", { type: "mouseWheel", x: rect.x, y: rect.y, deltaX: 0, deltaY: 650 });
  await sleep(180);
  const afterWheel = await cdp.evaluate(`(() => {
    const scroll = document.querySelector('[data-conversation-scroll]');
    return scroll ? { scrollTop: scroll.scrollTop, scrollHeight: scroll.scrollHeight } : null;
  })()`);
  const bottom = await cdp.evaluate(`(() => {
    const scroll = document.querySelector('[data-conversation-scroll]');
    const transcript = document.querySelector('[data-testid="conversation-transcript"]');
    const rows = transcript ? Array.from(transcript.querySelectorAll('[data-conversation-item]')) : [];
    const last = rows.at(-1);
    const tail = transcript?.querySelector('[data-scroll-tail]');
    scroll.scrollTop = scroll.scrollHeight;
    const max = scroll.scrollHeight - scroll.clientHeight;
    const scrollRect = scroll.getBoundingClientRect();
    const lastRect = last?.getBoundingClientRect();
    return {
      max,
      scrollTop: scroll.scrollTop,
      scrollHeight: scroll.scrollHeight,
      atBottom: Math.abs(scroll.scrollTop - max) <= 2,
      bottomGap: lastRect ? scrollRect.bottom - lastRect.bottom : null,
      tailHeight: tail?.getBoundingClientRect().height ?? null,
    };
  })()`);
  return { before, programmatic, afterWheel, bottom };
}

try {
  await navigate("?scenario=scroll");
  await cdp.waitFor(`document.querySelector('[data-testid="dsh-conversation-root"]') !== null`);
  await cdp.waitFor(`document.querySelectorAll('[data-agent-item]').length > 0`, 20_000);
  result.live = await cdp.evaluate(`(() => ({
    sidebar: !!document.querySelector('[data-testid="conversation-sidebar"]'),
    sessions: document.querySelectorAll('[data-testid="conversation-sidebar"] [data-task-id]').length,
    groups: document.querySelectorAll('[data-testid="conversation-sidebar"] section').length,
    activeSessions: Array.from(document.querySelectorAll('[data-testid="conversation-sidebar"] [data-task-id][data-lifecycle="active"]')).map((node) => node.dataset.taskId),
    staleOngoingSessions: Array.from(document.querySelectorAll('[data-testid="conversation-sidebar"] [data-task-id]:not([data-lifecycle="active"])')).filter((node) => node.querySelector('[data-state="ongoing"]')).map((node) => node.dataset.taskId),
    status: document.querySelector('[data-testid="dsh-conversation-root"]')?.dataset.agentStatus,
    lifecycle: document.querySelector('[data-testid="dsh-conversation-root"]')?.dataset.lifecycle,
    taskId: document.querySelector('[data-testid="dsh-conversation-root"]')?.dataset.taskId,
    context: document.querySelector('[data-testid="task-context"]')?.textContent,
    rows: document.querySelectorAll('[data-agent-item]').length,
    progress: Array.from(document.querySelectorAll('[data-testid="reasoning-summary"]')).map((node) => node.textContent || '').join(' ').slice(0, 1800),
    terminal: document.querySelectorAll('[data-testid="terminal-tool-row"]').length,
    diff: document.querySelectorAll('[data-testid="diff-tool-row"]').length,
    generic: document.querySelectorAll('[data-testid="generic-tool-row"]').length,
    composer: !!document.querySelector('[data-testid="composer"]'),
    composerDisabled: document.querySelector('[data-testid="composer"]')?.hasAttribute('data-disabled'),
    model: !!document.querySelector('[data-testid="model-selector"]'),
    send: !!document.querySelector('[data-testid="send-button"]'),
    sendDisabled: document.querySelector('[data-testid="send-button"]')?.disabled,
    stop: !!document.querySelector('[data-testid="stop-button"]')
  }))()`);

  await navigate("?mock=1");
  await cdp.waitFor(`document.querySelector('[data-testid="dsh-conversation-root"]')?.dataset.agentStatus === 'Completed'`);
  result.mock = await cdp.evaluate(`(() => ({
    status: document.querySelector('[data-testid="dsh-conversation-root"]')?.dataset.agentStatus,
    lifecycle: document.querySelector('[data-testid="dsh-conversation-root"]')?.dataset.lifecycle,
    taskId: document.querySelector('[data-testid="dsh-conversation-root"]')?.dataset.taskId,
    rows: document.querySelectorAll('[data-agent-item]').length,
    reasoning: document.querySelectorAll('[data-testid="reasoning-summary"]').length,
    reasoningPhase: document.querySelector('[data-testid="reasoning-summary"]')?.dataset.phase,
    reasoningText: document.querySelector('[data-testid="reasoning-summary"]')?.textContent,
    generic: document.querySelectorAll('[data-testid="generic-tool-row"]').length,
    read: document.querySelectorAll('[data-testid="read-tool-row"]').length,
    search: document.querySelectorAll('[data-testid="search-tool-row"]').length,
    terminal: document.querySelectorAll('[data-testid="terminal-tool-row"]').length,
    diff: document.querySelectorAll('[data-testid="diff-tool-row"]').length,
    composer: !!document.querySelector('[data-testid="composer"]'),
    composerDisabled: document.querySelector('[data-testid="composer"]')?.hasAttribute('data-disabled'),
    model: !!document.querySelector('[data-testid="model-selector"]'),
    send: !!document.querySelector('[data-testid="send-button"]'),
    sendDisabled: document.querySelector('[data-testid="send-button"]')?.disabled,
    stop: !!document.querySelector('[data-testid="stop-button"]'),
    rootFits: document.documentElement.scrollWidth <= document.documentElement.clientWidth
  }))()`);

  for (const selector of ["[data-testid=\"generic-tool-row\"]", "[data-testid=\"read-tool-row\"]", "[data-testid=\"search-tool-row\"]", "[data-testid=\"terminal-tool-row\"]", "[data-testid=\"diff-tool-row\"]"]) {
    await cdp.evaluate(`document.querySelector(${JSON.stringify(selector + " [data-disclosure-row]")})?.click()`);
  }
  await cdp.waitFor(`document.querySelector('[data-testid="generic-tool-card"]') !== null`);
  await cdp.waitFor(`document.querySelector('[data-testid="read-block"]') !== null`);
  await cdp.waitFor(`document.querySelector('[data-testid="search-block"]') !== null`);
  await cdp.waitFor(`document.querySelector('[data-testid="terminal-block"]') !== null`);
  await cdp.waitFor(`document.querySelector('[data-testid="diff-block"]') !== null`);
  result.cards = await cdp.evaluate(`({
    generic: document.querySelector('[data-testid="generic-tool-card"]')?.textContent?.slice(0, 160),
    read: document.querySelector('[data-testid="read-block"]')?.textContent?.slice(0, 160),
    search: document.querySelector('[data-testid="search-block"]')?.textContent?.slice(0, 160),
    terminal: document.querySelector('[data-testid="terminal-block"]')?.textContent?.slice(0, 160),
    diff: document.querySelector('[data-testid="diff-block"]')?.textContent?.slice(0, 160)
  })`);
  result.diffColors = await cdp.evaluate(`(() => {
    const add = document.querySelector("[data-diff-additions]");
    const del = document.querySelector("[data-diff-deletions]");
    return {
      add: add ? getComputedStyle(add).color : null,
      del: del ? getComputedStyle(del).color : null,
    };
  })()`);

  result.normal = await scenario("normal", "completed");
  result.doneActive = await scenario("done-active", "active");
  result.missingCompletion = await scenario("missing-completion", "ended_missing_completion");
  result.staleTool = await scenario("stale-tool", "ended_missing_completion");
  await cdp.evaluate(`document.querySelector('[data-testid="terminal-tool-row"] [data-disclosure-row]')?.click()`);
  await cdp.waitFor(`document.querySelector('[data-testid="terminal-block"]') !== null`);
  result.staleTerminal = await cdp.evaluate(`({ stopped: document.querySelector('[data-testid="terminal-block"]')?.hasAttribute('data-stopped'), text: document.querySelector('[data-testid="terminal-block"]')?.textContent })`);
  result.active = await scenario("active", "active");
  result.noProgress = await scenario("no-progress", "completed");
  result.phases = await scenario("phases", "completed");
  result.sidebar = await sidebarAcceptance();
  result.verticalScroll = await verticalScrollAcceptance();

  await cdp.call("Emulation.setDeviceMetricsOverride", { width: 720, height: 560, deviceScaleFactor: 1, mobile: false });
  await sleep(120);
  result.minViewport = await cdp.evaluate(`(() => {
    const root = document.querySelector('[data-testid="dsh-conversation-root"]');
    const scroller = document.querySelector('[data-conversation-scroll]');
    return { viewport: [innerWidth, innerHeight], rootFits: root ? root.scrollWidth <= root.clientWidth : false, horizontalOverflow: scroller ? scroller.scrollWidth - scroller.clientWidth : null };
  })()`);
  await cdp.call("Emulation.clearDeviceMetricsOverride");

  for (const count of [100, 1000]) {
    await navigate(`?stress=${count}`);
    await cdp.waitFor(`document.body.dataset.selfTest === 'pass'`, 20_000);
    result[`stress${count}`] = await cdp.evaluate(`({
      status: document.body.dataset.selfTest,
      rendered: Number(document.body.dataset.rendered),
      processed: Number(document.body.dataset.processed),
      lifecycle: document.querySelector('[data-testid="dsh-conversation-root"]')?.dataset.lifecycle,
      agentStatus: document.querySelector('[data-testid="dsh-conversation-root"]')?.dataset.agentStatus,
      durationMs: Number(document.querySelector('#self-test')?.dataset.durationMs)
    })`);
  }

  await sleep(120);
  result.runtimeExceptions = [...cdp.exceptions];
  result.logErrors = [...cdp.logErrors];
  const expectedPhases = ["plan", "decision", "progress", "verification", "warning", "done"];
  const vertical = result.verticalScroll;
  result.pass =
    result.live?.sidebar === true && result.live?.groups >= 1 && result.live?.sessions >= 1 && !!result.live?.taskId && result.live?.rows > 0 &&
    result.live?.staleOngoingSessions?.length === 0 &&
    result.live?.composer === true && result.live?.composerDisabled === (result.live?.lifecycle !== "active") && result.live?.model === false && result.live?.send === true && result.live?.sendDisabled === true && result.live?.stop === false &&
    result.mock?.status === "Completed" && result.mock?.lifecycle === "completed" && result.mock?.taskId === "mock-task" && result.mock?.rows >= 8 &&
    result.mock?.reasoning >= 1 && result.mock?.reasoningPhase === "decision" && result.mock?.reasoningText?.includes("公开工作说明") && result.mock?.reasoningText?.includes("判断") &&
    result.mock?.generic >= 2 && result.mock?.read >= 1 && result.mock?.search >= 1 && result.mock?.terminal === 1 && result.mock?.diff === 1 &&
    result.mock?.composer === true && result.mock?.composerDisabled === true && result.mock?.model === false && result.mock?.send === true && result.mock?.sendDisabled === true && result.mock?.stop === false && result.mock?.rootFits === true &&
    !!result.cards?.generic && !!result.cards?.read && !!result.cards?.search && !!result.cards?.terminal && !!result.cards?.diff &&
    !!result.diffColors?.add && !!result.diffColors?.del && result.diffColors.add !== result.diffColors.del &&
    result.normal?.lifecycle === "completed" && result.normal?.agentStatus === "Completed" && result.normal?.runningRows === 0 && result.normal?.leaseMeta?.includes("项目租约") && result.normal?.leaseMeta?.includes("已释放") && result.normal?.composerInViewport === true &&
    result.doneActive?.lifecycle === "active" && result.doneActive?.agentStatus !== "Completed" && result.doneActive?.leaseMeta?.includes("持有中") && result.doneActive?.reasoningText?.some((text) => text.includes("公开工作说明") && text.includes("完成")) &&
    result.missingCompletion?.lifecycle === "ended_missing_completion" && result.missingCompletion?.agentStatus === "Ended" && result.missingCompletion?.statusText?.includes("完成事件缺失") && result.missingCompletion?.sidebarLifecycle === "ended_missing_completion" && result.missingCompletion?.sidebarDot !== "ongoing" &&
    result.staleTool?.lifecycle === "ended_missing_completion" && result.staleTool?.stoppedRows === 2 && result.staleTool?.runningRows === 0 && result.staleTool?.rowText?.every((text) => text.includes("已中止")) && result.staleTerminal?.stopped === true && result.staleTerminal?.text?.includes("已中止") &&
    result.active?.lifecycle === "active" && result.active?.sidebarDot === "ongoing" && result.active?.agentStatus === "Running tool" && result.active?.composer === true && result.active?.composerDisabled === false && result.active?.composerInViewport === true && result.active?.send === true && result.active?.sendDisabled === true &&
    result.noProgress?.lifecycle === "completed" && result.noProgress?.reasoning === 0 && result.noProgress?.rows >= 1 &&
    JSON.stringify(result.phases?.phases) === JSON.stringify(expectedPhases) && result.phases?.reasoningText?.every((text) => text.includes("公开工作说明")) &&
    result.sidebar?.grouped?.groups === 6 && result.sidebar?.grouped?.sessions === 7 && result.sidebar?.grouped?.noiseVisible === false && result.sidebar?.grouped?.selectedTaskId === "sidebar-beta" && result.sidebar?.grouped?.toggleText === "按时间" &&
    JSON.stringify(result.sidebar?.grouped?.groupNames) === JSON.stringify(["Alpha", "运行底座维护", "桌面操作", "临时任务", "Gamma", "Beta"]) &&
    JSON.stringify(result.sidebar?.grouped?.groupCounts) === JSON.stringify([2, 1, 1, 1, 1, 1]) &&
    JSON.stringify(result.sidebar?.grouped?.taskIds) === JSON.stringify(["sidebar-alpha-main", "sidebar-alpha-alt", "sidebar-maintenance", "sidebar-desktop", "sidebar-temp", "sidebar-legit-auto-test", "sidebar-beta"]) &&
    result.sidebar?.time?.groups === 0 && result.sidebar?.time?.toggleText === "按工作区" &&
    JSON.stringify(result.sidebar?.time?.taskIds) === JSON.stringify(["sidebar-alpha-main", "sidebar-maintenance", "sidebar-desktop", "sidebar-temp", "sidebar-alpha-alt", "sidebar-legit-auto-test", "sidebar-beta"]) &&
    result.sidebar?.restored?.groups === 6 && result.sidebar?.restored?.toggleText === "按时间" &&
    JSON.stringify(result.sidebar?.restored?.taskIds) === JSON.stringify(["sidebar-alpha-main", "sidebar-alpha-alt", "sidebar-maintenance", "sidebar-desktop", "sidebar-temp", "sidebar-legit-auto-test", "sidebar-beta"]) &&
    result.sidebar?.scroll?.before?.scrollHeight > result.sidebar?.scroll?.before?.clientHeight && result.sidebar?.scroll?.before?.overflowY === "auto" && result.sidebar?.scroll?.after?.scrollTop > 0 &&
    vertical?.before?.scroll?.scrollHeight > vertical?.before?.scroll?.clientHeight && vertical?.before?.body?.clientHeight === vertical?.before?.scroll?.clientHeight &&
    vertical?.programmatic?.max > 0 && Math.abs(vertical.programmatic.scrollTop - vertical.programmatic.max) <= 2 &&
    vertical?.afterWheel?.scrollTop > 0 && Math.abs(vertical.afterWheel.scrollHeight - vertical.before.scroll.scrollHeight) <= 2 &&
    vertical?.bottom?.atBottom === true && vertical?.bottom?.tailHeight >= 320 && vertical?.bottom?.tailHeight <= 520 &&
    vertical?.bottom?.bottomGap >= 368 && vertical?.bottom?.bottomGap <= 568 && Math.abs(vertical.bottom.scrollHeight - vertical.before.scroll.scrollHeight) <= 2 &&
    result.minViewport?.viewport?.[0] === 720 && result.minViewport?.viewport?.[1] === 560 && result.minViewport?.rootFits === true && (result.minViewport?.horizontalOverflow ?? 1) <= 1 &&
    result.stress100?.status === "pass" && result.stress100?.processed === 100 && result.stress100?.rendered === 100 && result.stress100?.lifecycle === "completed" &&
    result.stress1000?.status === "pass" && result.stress1000?.processed === 1000 && result.stress1000?.rendered === 1000 && result.stress1000?.lifecycle === "completed" &&
    result.runtimeExceptions.length === 0 && result.logErrors.length === 0;
} finally {
  cdp.close();
}

console.log(JSON.stringify(result, null, 2));
if (!result.pass) process.exitCode = 1;
