const $ = (id) => document.getElementById(id);

let chatHydrated = false;
let pendingThinkingEl = null;
let chatHovering = false;
let renderedChatCount = 0;
let chatAgentName = "LLM KnightTrader";
let seenActivityTs = new Set();

const metricTargets = { equity: 0, available: 0, uplTotal: 0 };
const metricDisplay = { equity: 0, available: 0, uplTotal: 0 };
let metricsAnimFrame = null;

function chatLogEl() {
  return $("chat-log");
}

function scrollChatToBottom(smooth = false) {
  const log = chatLogEl();
  if (!log) return;
  if (smooth && log.scrollTo) {
    log.scrollTo({ top: log.scrollHeight, behavior: "smooth" });
  } else {
    log.scrollTop = log.scrollHeight;
  }
}

function setupChatScrollBehavior() {
  const log = chatLogEl();
  if (!log) return;
  log.addEventListener("mouseenter", () => {
    chatHovering = true;
  });
  log.addEventListener("mouseleave", () => {
    chatHovering = false;
    scrollChatToBottom(true);
  });
}

function fmtMoney(n) {
  if (n == null || Number.isNaN(n)) return "—";
  return "$" + Number(n).toLocaleString(undefined, { minimumFractionDigits: 2, maximumFractionDigits: 4 });
}

function fmtTime(ts) {
  return new Date(ts * 1000).toLocaleTimeString();
}

function trackActivityEvent(ev, atTop = true) {
  const ts = ev?.ts;
  if (ts != null && seenActivityTs.has(ts)) return false;
  if (ts != null) seenActivityTs.add(ts);
  prependEvent($("activity-log"), ev, atTop);
  return true;
}

async function refreshActivity() {
  try {
    const res = await fetch("/api/activity?limit=120");
    const data = await res.json();
    const events = data.events || [];
    if (!events.length) return;
    const log = $("activity-log");
    if (!log) return;
    if (seenActivityTs.size === 0) {
      log.innerHTML = "";
      [...events].reverse().forEach((ev) => trackActivityEvent(ev, false));
      return;
    }
    for (const ev of events) {
      if (!seenActivityTs.has(ev.ts)) {
        trackActivityEvent(ev, true);
      }
    }
  } catch (_) {
    // ignore
  }
}

function prependEvent(container, ev, atTop = true) {
  const div = document.createElement("div");
  div.className = `event ${ev.type || "system"}`;
  div.innerHTML = `
    <div class="title">${escapeHtml(ev.title || "")}</div>
    <div class="meta">${fmtTime(ev.ts)} · ${escapeHtml(ev.type || "")}</div>
    ${ev.detail ? `<div class="detail">${escapeHtml(ev.detail)}</div>` : ""}
  `;
  if (atTop) container.prepend(div);
  else container.appendChild(div);
  while (container.children.length > 200) container.removeChild(container.lastChild);
}

function escapeHtml(s) {
  return String(s)
    .replaceAll("&", "&amp;")
    .replaceAll("<", "&lt;")
    .replaceAll(">", "&gt;");
}

function renderMarkdownLite(text) {
  let s = escapeHtml(text || "");
  s = s.replace(/\*\*(.+?)\*\*/g, "<strong>$1</strong>");
  s = s.replace(/\n/g, "<br/>");
  return s;
}

function fmtPrice(n) {
  if (n == null || Number.isNaN(n) || Number(n) === 0) return "—";
  const v = Number(n);
  if (v >= 100) return v.toFixed(2);
  if (v >= 1) return v.toFixed(4);
  return v.toFixed(6);
}

function fmtPnl(n) {
  if (n == null || Number.isNaN(n)) return "—";
  const v = Number(n);
  const sign = v >= 0 ? "+" : "";
  return sign + v.toFixed(4);
}

function renderPositions(positions) {
  const el = $("positions");
  const count = (positions || []).length;
  const countEl = $("positions-count");
  if (countEl) countEl.textContent = String(count);
  if (!positions || !positions.length) {
    el.innerHTML = "<div class='note-card'>No open positions</div>";
    return;
  }
  el.innerHTML = positions.map((p) => {
    const sz = Number(p.size) || 0;
    const side = sz > 0 ? "LONG" : sz < 0 ? "SHORT" : String(p.side || "—").toUpperCase();
    const sideClass = sz > 0 ? "long" : sz < 0 ? "short" : "net";
    const upl = Number(p.upl) || 0;
    const uplClass = upl >= 0 ? "pnl-pos" : "pnl-neg";
    const lev = p.leverage && p.leverage !== "?" ? `${p.leverage}x` : "—";
    return `
    <div class="pos-card">
      <div class="pos-head">
        <strong>${escapeHtml(p.instId || "")}</strong>
        <span class="pos-side ${sideClass}">${side}</span>
      </div>
      <div class="pos-grid">
        <span class="label">Size</span><span class="value">${Math.abs(sz)}</span>
        <span class="label">Entry</span><span class="value">${fmtPrice(p.entry)}</span>
        <span class="label">Mark</span><span class="value">${fmtPrice(p.mark)}</span>
        <span class="label">UPL</span><span class="value ${uplClass}">${fmtPnl(upl)}</span>
        <span class="label">Leverage</span><span class="value">${escapeHtml(lev)}</span>
      </div>
    </div>`;
  }).join("");
}

function renderTrades(trades) {
  const el = $("trades");
  if (!trades || !trades.length) {
    el.innerHTML = "<div class='note-card'>No trades yet</div>";
    return;
  }
  el.innerHTML = [...trades].reverse().slice(0, 20).map((t) => {
    const failed = t.ok === false || t.action === "close_failed";
    const label = failed ? `${t.action || "?"} (failed)` : (t.action || "?");
    return `
    <div class="trade-card${failed ? " trade-failed" : ""}">
      <strong>${label}</strong> ${t.instId || ""} ${t.side || ""}<br/>
      <span class="meta">${t.ts ? fmtTime(t.ts) : ""}</span>
    </div>`;
  }).join("");
}

function renderResearch(notes) {
  const el = $("research");
  if (!notes || !notes.length) {
    el.innerHTML = "<div class='note-card'>Research will appear as the agent learns</div>";
    return;
  }
  el.innerHTML = [...notes].reverse().slice(0, 15).map((n) => `
    <div class="note-card">
      <div class="meta">${n.ts ? fmtTime(n.ts) : ""}</div>
      ${escapeHtml(n.note || "")}
    </div>
  `).join("");
}

function applyAccount(acct) {
  if (!acct) return;
  let equity = Number(acct.equity);
  let available = Number(acct.available);
  if (!Number.isFinite(equity) || equity < 0) equity = 0;
  if (!Number.isFinite(available)) available = 0;
  metricTargets.equity = equity;
  metricTargets.available = available;
  const upl = Number(acct.upl_total);
  if (Number.isFinite(upl)) {
    metricTargets.uplTotal = upl;
  } else {
    metricTargets.uplTotal = (acct.positions || []).reduce((s, p) => s + (Number(p.upl) || 0), 0);
  }
  renderPositions(acct.positions);
  const status = $("conn-status");
  const mtm = acct.mtm ? "Live" : "Cached";
  const markAge = Number(acct.mark_age_sec ?? (acct.mark_ts ? Date.now() / 1000 - acct.mark_ts : NaN));
  const markNote = Number.isFinite(markAge)
    ? (markAge <= 45 ? ` · marks ${Math.round(markAge)}s` : ` · marks stale ${Math.round(markAge)}s`)
    : "";
  if (acct.stale || acct.rate_limited || acct.display_repaired) {
    const age = acct.hydrated ? "restored" : "cached";
    const repairNote = acct.display_repaired ? " · auto-repaired" : "";
    status.textContent = `${mtm} (${age} — API cooldown${repairNote}${markNote})`;
    status.className = "status offline";
  } else {
    status.textContent = `${mtm}${markNote}`;
    status.className = "status online";
  }
  startMetricAnimation();
}

function renderMetricValues() {
  $("equity").textContent = fmtMoney(metricDisplay.equity);
  $("available").textContent = fmtMoney(metricDisplay.available);
  const uplEl = $("upl-total");
  if (uplEl) {
    const v = metricDisplay.uplTotal;
    uplEl.textContent = fmtPnl(v);
    uplEl.className = "value " + (v >= 0 ? "pnl-pos" : "pnl-neg");
  }
}

function startMetricAnimation() {
  if (metricsAnimFrame) return;
  const step = () => {
    const ease = 0.22;
    metricDisplay.equity += (metricTargets.equity - metricDisplay.equity) * ease;
    metricDisplay.available += (metricTargets.available - metricDisplay.available) * ease;
    metricDisplay.uplTotal += (metricTargets.uplTotal - metricDisplay.uplTotal) * ease;
    if (Math.abs(metricTargets.equity - metricDisplay.equity) < 0.00005) {
      metricDisplay.equity = metricTargets.equity;
    }
    if (Math.abs(metricTargets.available - metricDisplay.available) < 0.00005) {
      metricDisplay.available = metricTargets.available;
    }
    if (Math.abs(metricTargets.uplTotal - metricDisplay.uplTotal) < 0.00005) {
      metricDisplay.uplTotal = metricTargets.uplTotal;
    }
    renderMetricValues();
    const settled =
      metricDisplay.equity === metricTargets.equity &&
      metricDisplay.available === metricTargets.available &&
      metricDisplay.uplTotal === metricTargets.uplTotal;
    if (settled) {
      metricsAnimFrame = null;
      return;
    }
    metricsAnimFrame = requestAnimationFrame(step);
  };
  metricsAnimFrame = requestAnimationFrame(step);
}

async function refreshAccount() {
  try {
    const res = await fetch("/api/account");
    const data = await res.json();
    applyAccount(data.account);
  } catch (_) {
    // ignore
  }
}

let stackRestarting = false;

function renderStackStatus(payload) {
  const dot = $("trader-dot");
  const label = $("trader-status-label");
  const btn = $("restart-stack-btn");
  if (!dot || !label) return;

  const stack = payload?.stack || payload || {};
  const issues = payload?.issues || [];
  const dashboard = stack?.dashboard || {};
  const trader = stack?.trader || {};
  const status = trader.status || "offline";
  dot.dataset.state = status;

  if (status === "online" && dashboard.status !== "duplicate") {
    label.textContent = `Trader online (pid ${trader.pid})`;
  } else if (status === "duplicate" || dashboard.status === "duplicate") {
    const parts = [];
    if (dashboard.status === "duplicate") parts.push(`dashboard×${dashboard.count}`);
    if (status === "duplicate") parts.push(`trader×${trader.count}`);
    label.textContent = `Duplicate: ${parts.join(", ")}`;
    dot.dataset.state = "duplicate";
  } else {
    label.textContent = "Trader offline";
  }

  const extras = [];
  const monitorCount = stack?.monitor?.count || 0;
  const watcherCount = stack?.watchers?.count || 0;
  if (monitorCount > 0) extras.push(`monitor×${monitorCount}`);
  if (watcherCount > 0) extras.push(`watchers×${watcherCount}`);
  if (extras.length) {
    label.textContent += ` · ${extras.join(", ")}`;
  }
  if (issues.length) {
    const codes = issues.slice(0, 2).map((i) => i.code).join(", ");
    label.textContent += ` · repairing: ${codes}`;
    dot.dataset.state = trader.status === "online" ? "duplicate" : dot.dataset.state;
  }

  if (btn && !stackRestarting) {
    btn.disabled = false;
  }
}

async function refreshStackStatus() {
  try {
    const res = await fetch("/api/stack/status");
    const data = await res.json();
    renderStackStatus(data);
  } catch (_) {
    // ignore
  }
}

async function waitForStackHealth(timeoutMs = 90000) {
  const deadline = Date.now() + timeoutMs;
  while (Date.now() < deadline) {
    try {
      const res = await fetch("/api/health");
      if (res.ok) return true;
    } catch (_) {
      // dashboard restarting
    }
    await new Promise((r) => setTimeout(r, 1500));
  }
  return false;
}

async function restartStack() {
  const btn = $("restart-stack-btn");
  if (!btn || stackRestarting) return;
  if (
    !confirm(
      "Stop ALL LLM KnightTrader processes and restart one dashboard + one trader?\n\n" +
        "Same as the desktop Start shortcut. The stack operator will reconnect when ready."
    )
  ) {
    return;
  }

  stackRestarting = true;
  btn.disabled = true;
  btn.textContent = "Restarting…";
  try {
    const res = await fetch("/api/stack/restart", { method: "POST" });
    const data = await res.json();
    if (!data.ok) {
      alert(data.error || "Full stack restart failed to start");
      return;
    }

    btn.textContent = "Reconnecting…";
    const up = await waitForStackHealth();
    if (up) {
      window.location.reload();
      return;
    }
    alert("Restart started but dashboard did not come back in time. Try the desktop Start shortcut.");
  } catch (err) {
    const up = await waitForStackHealth(30000);
    if (up) {
      window.location.reload();
      return;
    }
    alert("Restart failed: " + err.message);
  } finally {
    stackRestarting = false;
    btn.textContent = "Restart traders";
    btn.disabled = false;
    refreshStackStatus();
  }
}

function applyPerformanceBaseline(bl) {
  const blEl = $("baseline-delta");
  const fromEl = $("baseline-from");
  if (!blEl) return;
  if (bl.active) {
    const ch = Number(bl.equity_change_usd) || 0;
    const pct = Number(bl.equity_change_pct) || 0;
    blEl.textContent = `${ch >= 0 ? "+" : ""}$${ch.toFixed(2)} (${pct >= 0 ? "+" : ""}${pct.toFixed(1)}%)`;
    blEl.className = "value " + (ch >= 0 ? "pos" : "neg");
    if (fromEl) {
      const base = Number(bl.baseline_equity);
      fromEl.textContent = Number.isFinite(base) ? `from $${base.toFixed(4)}` : "from —";
    }
  } else if (bl.armed) {
    blEl.textContent = `await +$${Number(bl.expected_injection_usd || 0).toFixed(2)}`;
    blEl.className = "value";
    if (fromEl) fromEl.textContent = "click to set manually";
  } else {
    blEl.textContent = "—";
    blEl.className = "value";
    if (fromEl) fromEl.textContent = "click to set baseline";
  }
}

async function setUserBaseline({ equity = null, useCurrent = true } = {}) {
  const body = { use_current: useCurrent, reason: "user set via dashboard click" };
  if (equity != null) body.equity = equity;
  const res = await fetch("/api/baseline/set", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body),
  });
  const data = await res.json();
  if (!data.ok) throw new Error(data.error || "Failed to set baseline");
  if (data.performance_baseline) applyPerformanceBaseline(data.performance_baseline);
  return data;
}

async function promptSetBaseline() {
  const currentEq = metricDisplay.equity || metricTargets.equity;
  const msg =
    "Set performance baseline (Baseline Δ zero point):\n\n" +
    "• OK = use current equity" + (currentEq ? ` ($${Number(currentEq).toFixed(4)})` : "") + "\n" +
    "• Cancel = enter a custom $ amount";
  if (confirm(msg)) {
    try {
      await setUserBaseline({ useCurrent: true });
    } catch (err) {
      alert("Set baseline failed: " + err.message);
    }
    return;
  }
  const raw = prompt("Baseline equity ($):", currentEq ? Number(currentEq).toFixed(4) : "");
  if (raw == null || !String(raw).trim()) return;
  const val = parseFloat(String(raw).replace(/^\$/, ""));
  if (!Number.isFinite(val) || val <= 0) {
    alert("Enter a positive dollar amount.");
    return;
  }
  try {
    await setUserBaseline({ equity: val, useCurrent: false });
  } catch (err) {
    alert("Set baseline failed: " + err.message);
  }
}

async function refreshStatus() {
  const res = await fetch("/api/status");
  const data = await res.json();
  const acct = data.account || {};
  applyAccount(acct);
  if (data.chat_agent_name) chatAgentName = data.chat_agent_name;
  $("target").textContent = fmtMoney(data.target_equity);
  $("cycles").textContent = data.state?.cycles ?? "0";
  applyPerformanceBaseline(data.performance_baseline || {});
  $("llm-keys").textContent = data.llm?.openrouter_keys ?? "0";
  $("mission").textContent = (data.mission || "").slice(0, 120) + "...";
  if (data.app_name) {
    document.title = data.app_name;
    const h1 = document.querySelector(".brand h1");
    if (h1) h1.textContent = data.app_name;
  }
  if (data.state?.last_decision) {
    $("decision").textContent = JSON.stringify(data.state.last_decision, null, 2);
  }
}

async function refreshTradesResearch() {
  const [t, r] = await Promise.all([
    fetch("/api/trades").then((x) => x.json()),
    fetch("/api/research").then((x) => x.json()),
  ]);
  renderTrades(t.trades);
  renderResearch(r.notes);
}

function addChat(role, text, extraClass = "", options = {}) {
  const forceScroll = options.forceScroll === true;
  const div = document.createElement("div");
  div.className = `msg ${role} ${extraClass}`.trim();
  div.innerHTML = `<div class="who">${role === "user" ? "You" : chatAgentName}</div><div class="body">${renderMarkdownLite(text)}</div>`;
  chatLogEl().appendChild(div);
  if (!chatHovering || forceScroll) {
    scrollChatToBottom();
  }
  return div;
}

function setThinking(on) {
  const btn = $("chat-form").querySelector("button");
  const input = $("chat-input");
  btn.disabled = on;
  input.disabled = on;
  btn.textContent = on ? "Thinking…" : "Send";
}

function clearThinkingBubble() {
  if (pendingThinkingEl) {
    pendingThinkingEl.remove();
    pendingThinkingEl = null;
  }
}

async function loadChatHistory() {
  if (chatHovering) return;
  try {
    const res = await fetch("/api/chat");
    const data = await res.json();
    const messages = data.messages || [];
    if (!messages.length && chatHydrated) return;
    if (chatHydrated && messages.length === renderedChatCount) return;

    chatLogEl().innerHTML = "";
    for (const msg of messages) {
      addChat(msg.role === "user" ? "user" : "assistant", msg.content || "", "", { forceScroll: false });
    }
    renderedChatCount = messages.length;
    chatHydrated = true;
    scrollChatToBottom();
  } catch (_) {
    // ignore
  }
}

function handleChatActivity(ev) {
  const status = ev.data?.status;
  const title = ev.title || "";
  const detail = ev.detail || "";

  if (title === "You" && status === "received") {
    return;
  }
  const isAgent = title === chatAgentName || title === "Hermes" || title === "LLM KnightTrader";
  if (!isAgent) return;

  if (status === "thinking") {
    clearThinkingBubble();
    pendingThinkingEl = addChat("assistant", "Thinking…", "thinking");
    return;
  }
  if (status === "complete" || (!status && detail && detail !== "Thinking…")) {
    clearThinkingBubble();
    setThinking(false);
    const body = $("chat-log");
    const last = body.lastElementChild;
    const already = last && last.classList.contains("assistant") && last.querySelector(".body")?.textContent?.includes(detail.slice(0, 40));
    if (!already) {
      addChat("assistant", detail);
    }
  }
}

function connectWs() {
  const proto = location.protocol === "https:" ? "wss" : "ws";
  const ws = new WebSocket(`${proto}://${location.host}/ws`);
  ws.onopen = () => {
    $("conn-status").textContent = "MTM";
    $("conn-status").className = "status online";
  };
  ws.onclose = () => {
    $("conn-status").textContent = "Reconnecting…";
    $("conn-status").className = "status offline";
    setTimeout(connectWs, 2000);
  };
  ws.onmessage = (evt) => {
    const msg = JSON.parse(evt.data);
    if (msg.type === "snapshot") {
      const log = $("activity-log");
      log.innerHTML = "";
      seenActivityTs.clear();
      [...msg.events].reverse().forEach((ev) => trackActivityEvent(ev, false));
      if (msg.account) applyAccount(msg.account);
    }
    if (msg.type === "account_update") {
      applyAccount(msg.account);
      const sg = msg.stream_guardian;
      const status = $("conn-status");
      if (sg && status) {
        if (sg.live_verified && sg.ok) {
          const age = sg.live_age_sec != null ? ` · verified ${Math.round(sg.live_age_sec)}s ago` : "";
          status.textContent = `Live${age}`;
          status.className = "status online";
        } else if (sg.refreshed) {
          status.textContent = "Live · corrected";
          status.className = "status online";
        }
      }
    }
    if (msg.type === "stack_repair") {
      if (msg.account) applyAccount(msg.account);
      refreshStackStatus();
    }
    if (msg.type === "activity") {
      trackActivityEvent(msg.event, true);
      if (msg.event.type === "chat") {
        handleChatActivity(msg.event);
      }
      if (msg.event.type === "account" || msg.event.type === "trade") {
        refreshAccount();
        refreshTradesResearch();
      }
    }
  };
  setInterval(() => { if (ws.readyState === 1) ws.send("ping"); }, 25000);
}

$("chat-form").addEventListener("submit", async (e) => {
  e.preventDefault();
  const input = $("chat-input");
  const text = input.value.trim();
  if (!text) return;
  input.value = "";
  addChat("user", text, "", { forceScroll: true });
  clearThinkingBubble();
  pendingThinkingEl = addChat("assistant", "Received. Thinking…", "thinking", { forceScroll: true });
  setThinking(true);
  try {
    const res = await fetch("/api/chat", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ message: text }),
    });
    const raw = await res.text();
    let data;
    try {
      data = JSON.parse(raw);
    } catch {
      throw new Error(raw.slice(0, 120) || `HTTP ${res.status}`);
    }
    if (!res.ok && !data.reply) {
      throw new Error(data.error || `HTTP ${res.status}`);
    }
    clearThinkingBubble();
    const suffix = data.provider && data.provider !== "error"
      ? `\n\n(${data.provider}/${data.model}, ${Math.round(data.latency_ms || 0)}ms)`
      : "";
    addChat("assistant", (data.reply || "No response") + suffix, "", { forceScroll: !chatHovering });
    if (data.performance_baseline) applyPerformanceBaseline(data.performance_baseline);
    renderedChatCount += 2;
    refreshTradesResearch();
  } catch (err) {
    clearThinkingBubble();
    addChat("assistant", "Error: " + err.message);
  } finally {
    setThinking(false);
  }
});

$("restart-stack-btn")?.addEventListener("click", restartStack);
$("baseline-metric")?.addEventListener("click", promptSetBaseline);

loadChatHistory();
setupChatScrollBehavior();
refreshStackStatus();
refreshStatus();
refreshAccount();
refreshActivity();
refreshTradesResearch();
connectWs();
setInterval(refreshAccount, 3000);
setInterval(refreshStackStatus, 5000);
setInterval(refreshStatus, 30000);
setInterval(refreshActivity, 3000);
setInterval(refreshTradesResearch, 30000);
setInterval(loadChatHistory, 8000);
