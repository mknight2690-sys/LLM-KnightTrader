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

let lastStream = {
  equity: null,
  available: null,
  uplTotal: null,
  positionsSig: null,
  tradesSig: null,
  researchSig: null,
  lastDecisionSig: null,
  baselineSig: null,
};

function fmtTsMs(ts) {
  const d = new Date(ts);
  const hh = String(d.getHours()).padStart(2, "0");
  const mm = String(d.getMinutes()).padStart(2, "0");
  const ss = String(d.getSeconds()).padStart(2, "0");
  const ms = String(d.getMilliseconds()).padStart(3, "0");
  return `${hh}:${mm}:${ss}.${ms}`;
}

function streamNumbers(kind, data) {
  const el = $("numbers-stream");
  if (!el) return;
  const line = document.createElement("div");
  line.className = "stream-line";
  line.textContent = `${fmtTsMs(Date.now())} ${kind} ${JSON.stringify(data)}`;
  el.appendChild(line);
  while (el.children.length > 300) el.removeChild(el.firstElementChild);
}

function positionsSignature(positions) {
  const rows = (positions || []).slice(0, 12).map((p) => ({
    instId: p.instId,
    side: p.side,
    size: p.size,
    mark: p.mark,
    upl: p.upl,
    leverage: p.leverage,
  }));
  rows.sort((a, b) => String(a.instId || "").localeCompare(String(b.instId || "")));
  return JSON.stringify(rows);
}

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
  return "$" + Number(n).toLocaleString(undefined, { minimumFractionDigits: 2, maximumFractionDigits: 2 });
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

  // Millisecond stream of BloFin-linked figures: emit only when values change materially.
  try {
    const uplTotalNow = metricTargets.uplTotal;
    const posSig = positionsSignature(acct.positions);
    // Round to 2 decimals for comparison to suppress noise
    const rEquity = Math.round(equity * 100) / 100;
    const rAvailable = Math.round(available * 100) / 100;
    const rUplTotal = Math.round(uplTotalNow * 100) / 100;
    const diff = {};
    if (lastStream.equity !== rEquity) diff.equity = rEquity;
    if (lastStream.available !== rAvailable) diff.available = rAvailable;
    if (lastStream.uplTotal !== rUplTotal) diff.uplTotal = rUplTotal;
    if (lastStream.positionsSig !== posSig) {
      const rows = (acct.positions || []).slice(0, 6).map((p) => p.instId);
      diff.positionsChanged = true;
      diff.positions_sample = rows;
      diff.positions_count = (acct.positions || []).length;
    }
    if (Object.keys(diff).length) {
      lastStream.equity = rEquity;
      lastStream.available = rAvailable;
      lastStream.uplTotal = rUplTotal;
      lastStream.positionsSig = posSig;
      streamNumbers("account", diff);
    }
  } catch (_) {
    // ignore stream failures
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
    if (Math.abs(metricTargets.equity - metricDisplay.equity) < 0.005) {
      metricDisplay.equity = metricTargets.equity;
    }
    if (Math.abs(metricTargets.available - metricDisplay.available) < 0.005) {
      metricDisplay.available = metricTargets.available;
    }
    if (Math.abs(metricTargets.uplTotal - metricDisplay.uplTotal) < 0.005) {
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
      fromEl.textContent = Number.isFinite(base) ? `from $${base.toFixed(2)}` : "from —";
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
    "• OK = use current equity" + (currentEq ? ` ($${Number(currentEq).toFixed(2)})` : "") + "\n" +
    "• Cancel = enter a custom $ amount";
  if (confirm(msg)) {
    try {
      await setUserBaseline({ useCurrent: true });
    } catch (err) {
      alert("Set baseline failed: " + err.message);
    }
    return;
  }
  const raw = prompt("Baseline equity ($):", currentEq ? Number(currentEq).toFixed(2) : "");
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
    try {
      const ld = data.state.last_decision || {};
      const reasoning = String(ld.reasoning || "");
      const tsInt = Math.floor(Number(ld.ts || 0));
      const sig = `${ld.action || ""}:${ld.confidence ?? ""}:${tsInt}:${reasoning.slice(0, 60)}`;
      if (lastStream.lastDecisionSig !== sig) {
        lastStream.lastDecisionSig = sig;
        streamNumbers("last_llm_decision", {
          action: ld.action || null,
          confidence: ld.confidence ?? null,
          ts: tsInt,
        });
      }
    } catch (_) {
      // ignore
    }
  }

  try {
    const pb = data.performance_baseline || null;
    if (pb) {
      const sig = JSON.stringify({
        active: pb.active,
        armed: pb.armed,
        baseline_equity: pb.baseline_equity,
        equity_change_usd: pb.equity_change_usd,
        equity_change_pct: pb.equity_change_pct,
        expected_injection_usd: pb.expected_injection_usd,
      });
      if (lastStream.baselineSig !== sig) {
        lastStream.baselineSig = sig;
        streamNumbers("baseline", {
          active: pb.active,
          armed: pb.armed,
          baseline_equity: pb.baseline_equity ?? null,
          equity_change_usd: pb.equity_change_usd ?? null,
          equity_change_pct: pb.equity_change_pct ?? null,
        });
      }
    }
  } catch (_) {
    // ignore
  }
}

async function refreshTradesResearch() {
  const [t, r] = await Promise.all([
    fetch("/api/trades").then((x) => x.json()),
    fetch("/api/research").then((x) => x.json()),
  ]);
  renderTrades(t.trades);
  renderResearch(r.notes);

  try {
    const trades = t.trades || [];
    const last = trades[trades.length - 1] || {};
    const failedCount = trades.filter((x) => x?.ok === false || x?.action === "close_failed").length;
    const tsInt = Math.floor(Number(last.ts || 0));
    const sig = `${trades.length}:${failedCount}:${last.action || ""}:${last.instId || ""}:${tsInt}`;
    if (lastStream.tradesSig !== sig) {
      lastStream.tradesSig = sig;
      streamNumbers("trades", {
        total: trades.length,
        failedCount,
        last_action: last.action || null,
        last_instId: last.instId || null,
        last_ok: last.ok,
        ts: tsInt,
      });
    }

    const notes = r.notes || [];
    const lastN = notes[notes.length - 1] || {};
    const lastNoteSig = `${notes.length}:${Math.floor(Number(lastN.ts || 0))}:${String(lastN.note || "").slice(0, 40)}`;
    if (lastStream.researchSig !== lastNoteSig) {
      lastStream.researchSig = lastNoteSig;
      streamNumbers("research", {
        total: notes.length,
        last_note_snip: String(lastN.note || "").slice(0, 80),
      });
    }
  } catch (_) {
    // ignore stream
  }
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

    const start = chatHydrated ? renderedChatCount : 0;
    if (messages.length <= start) return;

    const newMessages = messages.slice(start);
    for (const msg of newMessages) {
      addChat(msg.role === "user" ? "user" : "assistant", msg.content || "", "", { forceScroll: false });
    }
    renderedChatCount = messages.length;
    chatHydrated = true;
    if (newMessages.length) scrollChatToBottom();
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

/* ============================================================
   EQUITY CHART — ported from blohunter-connect, adapted to
   the LLM KnightTrader theme and bundled into a single file.
   ============================================================ */

const EQUITY_TIMEFRAME_MS = {
  '1D': 24 * 60 * 60 * 1000,
  '1W': 7 * 24 * 60 * 60 * 1000,
  '1M': 30 * 24 * 60 * 60 * 1000,
  '3M': 90 * 24 * 60 * 60 * 1000,
};
const CHART_MARGIN = Object.freeze({ top: 26, right: 68, bottom: 32, left: 18 });
const DEFAULT_EQUITY_RANGE = '1D';
const EQUITY_FLASH_DURATION_MS = 1000;
const EQUITY_SHIMMER_INTERVAL_MS = 5 * 60 * 1000;
const EQUITY_SHIMMER_DURATION_MS = 2600;
const EQUITY_INITIAL_SHIMMER_DELAY_MS = 1200;

let selectedEquityRange = DEFAULT_EQUITY_RANGE;
let latestEquityHistory = [];
let latestAccountEquity = 0;
let latestUnrealizedPnl = 0;
let latestRenderedSeries = [];
let hoveredEquityIndex = null;
let latestEquityDataSignature = '';
let equityFlashUntil = 0;
let equityFlashFrame = null;
let equityShimmerUntil = 0;
let nextEquityShimmerAt = Date.now() + EQUITY_SHIMMER_INTERVAL_MS;
let equityShimmerTimer = null;
let hasPlayedInitialEquityShimmer = false;
let pendingInitialEquityShimmerTimer = null;
let equityShimmerVisibilityBound = false;

function triggerEquityShimmer(now = Date.now(), { rescheduleAuto = false } = {}) {
  equityShimmerUntil = now + EQUITY_SHIMMER_DURATION_MS;
  if (rescheduleAuto) {
    nextEquityShimmerAt = now + EQUITY_SHIMMER_INTERVAL_MS;
    scheduleEquityShimmer();
  }
}

function scheduleInitialEquityShimmer() {
  if (hasPlayedInitialEquityShimmer) return;
  if (pendingInitialEquityShimmerTimer !== null) return;
  pendingInitialEquityShimmerTimer = window.setTimeout(() => {
    pendingInitialEquityShimmerTimer = null;
    hasPlayedInitialEquityShimmer = true;
    triggerEquityShimmer(Date.now());
    redrawEquityChart();
  }, EQUITY_INITIAL_SHIMMER_DELAY_MS);
}

function scheduleEquityShimmer() {
  if (equityShimmerTimer !== null) {
    window.clearTimeout(equityShimmerTimer);
    equityShimmerTimer = null;
  }
  const delayMs = Math.max(0, nextEquityShimmerAt - Date.now());
  equityShimmerTimer = window.setTimeout(() => {
    equityShimmerTimer = null;
    triggerEquityShimmer(Date.now(), { rescheduleAuto: true });
    redrawEquityChart();
  }, delayMs);
}

function bindEquityShimmerVisibilityReplay() {
  if (equityShimmerVisibilityBound) return;
  equityShimmerVisibilityBound = true;
  document.addEventListener('visibilitychange', () => {
    if (document.visibilityState !== 'visible') return;
    const now = Date.now();
    if (hasPlayedInitialEquityShimmer && now >= nextEquityShimmerAt && equityShimmerUntil <= now) {
      triggerEquityShimmer(now, { rescheduleAuto: true });
      redrawEquityChart();
    }
  });
}

function isSupportedEquityRange(range) {
  return range === 'ALL' || Object.hasOwn(EQUITY_TIMEFRAME_MS, range);
}

function buildEquityDataSignature(history, currentEquity) {
  return JSON.stringify({
    history: (history || []).map((point) => [
      Number.parseInt(point?.at, 10) || 0,
      Number.parseFloat(point?.equity || 0).toFixed(2),
    ]),
    currentEquity: Number.parseFloat(currentEquity || 0).toFixed(2),
  });
}

function directionClass(value) {
  return value >= 0 ? 'positive' : 'negative';
}

function clamp(value, min, max) {
  return Math.min(Math.max(value, min), max);
}

function normalizeEquityHistory(history, currentEquity) {
  const normalized = (history || [])
    .map((point) => ({
      at: Number.parseInt(point?.at, 10),
      equity: Number.parseFloat(point?.equity),
    }))
    .filter(
      (point) => Number.isFinite(point.at) && Number.isFinite(point.equity) && point.equity > 0
    )
    .sort((a, b) => a.at - b.at);

  const now = Date.now();
  if (Number.isFinite(currentEquity) && currentEquity > 0) {
    const last = normalized[normalized.length - 1];
    if (
      !last ||
      Math.abs(now - last.at) > 60 * 1000 ||
      Math.abs(last.equity - currentEquity) > 0.0001
    ) {
      normalized.push({ at: now, equity: currentEquity });
    }
  }

  return normalized;
}

function interpolateEquityPointAt(history, timestamp) {
  if (!history.length) return null;

  const target = Number(timestamp);
  const nextIndex = history.findIndex((point) => point.at >= target);
  if (nextIndex < 0) {
    return { at: target, equity: history[history.length - 1].equity };
  }
  if (nextIndex === 0) {
    return { at: target, equity: history[0].equity };
  }

  const previous = history[nextIndex - 1];
  const next = history[nextIndex];
  if (next.at === target) return { ...next };

  const progress = (target - previous.at) / Math.max(next.at - previous.at, 1);
  return {
    at: target,
    equity: previous.equity + (next.equity - previous.equity) * progress,
  };
}

function uniqueEquityPoints(points = []) {
  const byTimestamp = new Map();
  for (const point of points) {
    if (!point || !Number.isFinite(point.at) || !Number.isFinite(point.equity)) continue;
    byTimestamp.set(point.at, point);
  }
  return [...byTimestamp.values()].sort((a, b) => a.at - b.at);
}

function getEquitySeriesForRange(history, range, now = Date.now()) {
  if (!history.length) return [];
  if (range === 'ALL') return history;

  const durationMs = EQUITY_TIMEFRAME_MS[range] || 0;
  const domainStart = now - durationMs;
  const domainEnd = now;
  const filtered = history.filter((point) => point.at > domainStart && point.at < domainEnd);

  return uniqueEquityPoints([
    interpolateEquityPointAt(history, domainStart),
    ...filtered,
    interpolateEquityPointAt(history, domainEnd),
  ]);
}

function formatEquityRangeLabel(points) {
  if (!points.length) return 'No account history yet';
  if (points.length === 1) return `Started tracking ${formatCompactDateTime(points[0].at)}`;
  return `${formatCompactDateTime(points[0].at)} to ${formatCompactDateTime(points[points.length - 1].at)}`;
}

function formatEquityAxisLabel(timestamp, range) {
  const date = new Date(timestamp);
  if (range === '1D') {
    return date.toLocaleTimeString([], { hour: 'numeric', minute: '2-digit' });
  }
  if (range === '1W') {
    return date.toLocaleDateString([], { weekday: 'short' });
  }
  return date.toLocaleDateString([], { month: 'short', day: 'numeric' });
}

function formatCompactDate(value) {
  return new Date(value).toLocaleDateString([], {
    month: 'numeric',
    day: 'numeric',
    year: '2-digit',
  });
}

function syncEquitySummary(series, fallbackCurrentEquity, hoveredPoint = null) {
  const current =
    hoveredPoint?.equity ?? series[series.length - 1]?.equity ?? fallbackCurrentEquity ?? 0;
  const baseline = series[0]?.equity ?? current;
  const change = current - baseline;
  const changePct = baseline > 0 ? (change / baseline) * 100 : 0;

  const valEl = $('equityChartValue');
  const chEl = $('equityChartChange');
  const rngEl = $('equityChartRange');
  if (valEl) valEl.textContent = fmtMoney(current);
  if (chEl) {
    chEl.textContent = `${fmtMoney(change)} (${changePct >= 0 ? '+' : ''}${changePct.toFixed(2)}%)`;
    chEl.className = `growth-change ${directionClass(change)}`;
  }
  if (rngEl) {
    rngEl.textContent = hoveredPoint
      ? `${formatCompactDateTime(hoveredPoint.at)} · ${fmtMoney(hoveredPoint.equity)}`
      : formatEquityRangeLabel(series);
  }
}

function drawSafeAxisLabel(ctx, label, x, y, cssWidth) {
  const width = ctx.measureText(label).width;
  const safeX = clamp(x, width / 2 + 6, cssWidth - width / 2 - 6);
  ctx.fillText(label, safeX, y);
}

function buildEquityAxisTicks(points = [], desiredTickCount = 5) {
  const validPoints = (points || [])
    .filter((point) => Number.isFinite(point?.at))
    .sort((a, b) => a.at - b.at);
  if (!validPoints.length) return [];

  const minTime = validPoints[0].at;
  const maxTime = validPoints[validPoints.length - 1].at;
  const timeSpan = maxTime - minTime;
  if (timeSpan <= 0 || desiredTickCount <= 1) return [maxTime];

  const tickCount = Math.max(2, Math.floor(desiredTickCount));
  return Array.from({ length: tickCount }, (_, index) =>
    Math.round(minTime + timeSpan * (index / (tickCount - 1)))
  );
}

function buildSmoothLinePath(points, xFor, yFor) {
  const path = new Path2D();
  if (!points.length) return path;

  path.moveTo(xFor(points[0].at), yFor(points[0].equity));
  if (points.length === 1) return path;

  for (let index = 0; index < points.length - 1; index++) {
    const current = points[index];
    const next = points[index + 1];
    const currentX = xFor(current.at);
    const currentY = yFor(current.equity);
    const nextX = xFor(next.at);
    const nextY = yFor(next.equity);
    const controlX = currentX + (nextX - currentX) / 2;

    path.bezierCurveTo(controlX, currentY, controlX, nextY, nextX, nextY);
  }

  return path;
}

function drawHoverState(ctx, hoveredPoint, x, y, cssWidth, cssHeight, margin) {
  ctx.save();

  ctx.strokeStyle = 'rgba(226, 232, 240, 0.14)';
  ctx.lineWidth = 1;
  ctx.beginPath();
  ctx.moveTo(x, margin.top);
  ctx.lineTo(x, cssHeight - margin.bottom);
  ctx.stroke();

  ctx.strokeStyle = 'rgba(61, 220, 151, 0.22)';
  ctx.lineWidth = 10;
  ctx.beginPath();
  ctx.arc(x, y, 5, 0, Math.PI * 2);
  ctx.stroke();

  ctx.fillStyle = '#0b1018';
  ctx.beginPath();
  ctx.arc(x, y, 5, 0, Math.PI * 2);
  ctx.fill();

  ctx.fillStyle = '#3ddc97';
  ctx.beginPath();
  ctx.arc(x, y, 3.25, 0, Math.PI * 2);
  ctx.fill();

  const labelValue = fmtMoney(hoveredPoint.equity);
  const labelTime = formatCompactDateTime(hoveredPoint.at);
  ctx.font = '600 11px system-ui, -apple-system, Segoe UI, Roboto, Arial, sans-serif';
  const labelWidth =
    Math.max(ctx.measureText(labelValue).width, ctx.measureText(labelTime).width) + 18;
  const labelHeight = 38;
  const labelX = clamp(x - labelWidth / 2, margin.left, cssWidth - margin.right - labelWidth);
  const labelY = y > 60 ? y - labelHeight - 12 : y + 14;

  ctx.fillStyle = 'rgba(7, 11, 18, 0.92)';
  ctx.strokeStyle = 'rgba(61, 220, 151, 0.14)';
  ctx.lineWidth = 1;
  ctx.beginPath();
  ctx.roundRect(labelX, labelY, labelWidth, labelHeight, 10);
  ctx.fill();
  ctx.stroke();

  ctx.textAlign = 'left';
  ctx.fillStyle = '#ecfdf5';
  ctx.fillText(labelValue, labelX + 9, labelY + 14);
  ctx.fillStyle = '#9ba7bf';
  ctx.fillText(labelTime, labelX + 9, labelY + 28);

  ctx.restore();
}

function eventClientX(event) {
  if (typeof event.clientX === 'number') return event.clientX;
  const touch = event.touches?.[0] || event.changedTouches?.[0];
  return typeof touch?.clientX === 'number' ? touch.clientX : null;
}

function drawEquityChart(history, range, currentUnrealized = 0, hoverIndex = null) {
  const canvas = $('equityChartCanvas');
  const ctx = canvas.getContext('2d');
  if (!ctx) return;
  const now = Date.now();
  const flashRemaining = Math.max(0, equityFlashUntil - Date.now());
  const flashProgress = flashRemaining > 0 ? flashRemaining / EQUITY_FLASH_DURATION_MS : 0;
  const shimmerRemaining = Math.max(0, equityShimmerUntil - now);
  const shimmerProgress =
    shimmerRemaining > 0 ? 1 - shimmerRemaining / EQUITY_SHIMMER_DURATION_MS : 0;

  const dpr = window.devicePixelRatio || 1;
  const cssWidth = canvas.clientWidth || 1200;
  const cssHeight = canvas.clientHeight || 228;
  canvas.width = Math.round(cssWidth * dpr);
  canvas.height = Math.round(cssHeight * dpr);
  ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
  ctx.clearRect(0, 0, cssWidth, cssHeight);

  const margin = CHART_MARGIN;
  const plotWidth = cssWidth - margin.left - margin.right;
  const plotHeight = cssHeight - margin.top - margin.bottom;

  const panelGlow = ctx.createLinearGradient(0, 0, 0, cssHeight);
  panelGlow.addColorStop(0, 'rgba(91, 157, 255, 0.08)');
  panelGlow.addColorStop(0.52, 'rgba(61, 220, 151, 0.04)');
  panelGlow.addColorStop(1, 'rgba(4, 8, 14, 0)');
  ctx.fillStyle = panelGlow;
  ctx.fillRect(0, 0, cssWidth, cssHeight);

  if (!history.length) {
    ctx.fillStyle = 'rgba(173, 182, 201, 0.78)';
    ctx.font = '600 14px system-ui, -apple-system, Segoe UI, Roboto, Arial, sans-serif';
    ctx.textAlign = 'center';
    ctx.fillText(
      'Account growth will appear once equity snapshots are collected.',
      cssWidth / 2,
      cssHeight / 2
    );
    return;
  }

  if (history.length === 1) {
    ctx.fillStyle = 'rgba(173, 182, 201, 0.78)';
    ctx.font = '600 14px system-ui, -apple-system, Segoe UI, Roboto, Arial, sans-serif';
    ctx.textAlign = 'center';
    ctx.fillText(
      'Tracking started. The equity curve will build as new snapshots come in.',
      cssWidth / 2,
      cssHeight / 2
    );
  }

  const minEquity = Math.min(...history.map((point) => point.equity));
  const maxEquity = Math.max(...history.map((point) => point.equity));
  const equitySpan = Math.max(maxEquity - minEquity, maxEquity * 0.02, 1);
  const paddedMin = Math.max(0, minEquity - equitySpan * 0.18);
  const paddedMax = maxEquity + equitySpan * 0.14;
  const minTime = history[0].at;
  const maxTime = history[history.length - 1].at;
  const timeSpan = Math.max(maxTime - minTime, 1);
  const xFor = (time) => margin.left + ((time - minTime) / timeSpan) * plotWidth;
  const yFor = (equity) =>
    margin.top + ((paddedMax - equity) / (paddedMax - paddedMin)) * plotHeight;

  ctx.strokeStyle = 'rgba(255,255,255,0.06)';
  ctx.lineWidth = 1;
  for (let i = 0; i <= 4; i++) {
    const y = margin.top + (plotHeight / 4) * i;
    ctx.beginPath();
    ctx.moveTo(margin.left, y);
    ctx.lineTo(cssWidth - margin.right, y);
    ctx.stroke();
  }

  const path = buildSmoothLinePath(history, xFor, yFor);

  const areaPath = new Path2D(path);
  areaPath.lineTo(xFor(history[history.length - 1].at), margin.top + plotHeight);
  areaPath.lineTo(xFor(history[0].at), margin.top + plotHeight);
  areaPath.closePath();

  const fill = ctx.createLinearGradient(0, margin.top, 0, margin.top + plotHeight);
  fill.addColorStop(0, 'rgba(61, 220, 151, 0.35)');
  fill.addColorStop(0.55, 'rgba(91, 157, 255, 0.14)');
  fill.addColorStop(1, 'rgba(91, 157, 255, 0.02)');
  ctx.fillStyle = fill;
  ctx.fill(areaPath);

  if (Math.abs(currentUnrealized) >= 0.01) {
    const unrealizedColor =
      currentUnrealized >= 0 ? 'rgba(0, 214, 122, 0.18)' : 'rgba(255, 96, 96, 0.16)';
    ctx.strokeStyle = unrealizedColor;
    ctx.lineWidth = 8;
    ctx.lineJoin = 'round';
    ctx.lineCap = 'round';
    ctx.shadowColor = unrealizedColor;
    ctx.shadowBlur = 20;
    ctx.stroke(path);
    ctx.shadowBlur = 0;
  }

  ctx.strokeStyle = '#3ddc97';
  ctx.lineWidth = 3;
  ctx.lineJoin = 'round';
  ctx.lineCap = 'round';
  ctx.shadowColor = 'rgba(61, 220, 151, 0.22)';
  ctx.shadowBlur = 16;
  ctx.stroke(path);
  ctx.shadowBlur = 0;

  if (shimmerProgress > 0) {
    const shimmerTravelStart = margin.left - plotWidth * 0.22;
    const shimmerTravelEnd = margin.left + plotWidth * 1.18;
    const shimmerCenter =
      shimmerTravelStart + (shimmerTravelEnd - shimmerTravelStart) * shimmerProgress;
    const shimmerBand = Math.max(160, plotWidth * 0.34);
    const shimmerGradient = ctx.createLinearGradient(
      shimmerCenter - shimmerBand,
      0,
      shimmerCenter + shimmerBand,
      0
    );
    shimmerGradient.addColorStop(0, 'rgba(255, 59, 48, 0)');
    shimmerGradient.addColorStop(0.18, 'rgba(255, 59, 48, 0)');
    shimmerGradient.addColorStop(0.28, 'rgba(255, 59, 48, 0.98)');
    shimmerGradient.addColorStop(0.34, 'rgba(255, 204, 0, 0.98)');
    shimmerGradient.addColorStop(0.5, 'rgba(52, 199, 89, 0.98)');
    shimmerGradient.addColorStop(0.66, 'rgba(24, 212, 255, 0.98)');
    shimmerGradient.addColorStop(0.82, 'rgba(91, 140, 255, 0.96)');
    shimmerGradient.addColorStop(0.92, 'rgba(214, 107, 255, 0.98)');
    shimmerGradient.addColorStop(0.995, 'rgba(214, 107, 255, 0)');
    shimmerGradient.addColorStop(1, 'rgba(214, 107, 255, 0)');
    ctx.save();
    ctx.strokeStyle = shimmerGradient;
    ctx.lineWidth = 4.8;
    ctx.lineJoin = 'round';
    ctx.lineCap = 'round';
    ctx.shadowColor = 'rgba(255, 255, 255, 0.38)';
    ctx.shadowBlur = 18;
    ctx.stroke(path);
    ctx.restore();
  }

  if (flashProgress > 0) {
    ctx.save();
    ctx.strokeStyle = `rgba(255, 255, 255, ${0.22 + flashProgress * 0.46})`;
    ctx.lineWidth = 4.6 + flashProgress * 3.1;
    ctx.lineJoin = 'round';
    ctx.lineCap = 'round';
    ctx.shadowColor = `rgba(61, 220, 151, ${0.18 + flashProgress * 0.34})`;
    ctx.shadowBlur = 16 + flashProgress * 24;
    ctx.stroke(path);
    ctx.restore();
  }

  const last = history[history.length - 1];
  const lastX = xFor(last.at);
  const lastY = yFor(last.equity);
  ctx.fillStyle = '#3ddc97';
  ctx.beginPath();
  ctx.arc(lastX, lastY, 4.5, 0, Math.PI * 2);
  ctx.fill();
  ctx.strokeStyle = 'rgba(61, 220, 151, 0.22)';
  ctx.lineWidth = 10;
  ctx.beginPath();
  ctx.arc(lastX, lastY, 4.5, 0, Math.PI * 2);
  ctx.stroke();

  if (flashProgress > 0) {
    ctx.save();
    ctx.strokeStyle = `rgba(255, 255, 255, ${0.2 + flashProgress * 0.5})`;
    ctx.lineWidth = 8 + flashProgress * 6;
    ctx.shadowColor = `rgba(61, 220, 151, ${0.18 + flashProgress * 0.32})`;
    ctx.shadowBlur = 12 + flashProgress * 18;
    ctx.beginPath();
    ctx.arc(lastX, lastY, 5.5 + flashProgress * 2.5, 0, Math.PI * 2);
    ctx.stroke();
    ctx.restore();
  }

  ctx.fillStyle = '#9ba7bf';
  ctx.font = '12px system-ui, -apple-system, Segoe UI, Roboto, Arial, sans-serif';
  ctx.textAlign = 'right';
  for (let i = 0; i <= 4; i++) {
    const equity = paddedMax - ((paddedMax - paddedMin) / 4) * i;
    const y = margin.top + (plotHeight / 4) * i + 4;
    ctx.fillText(fmtMoney(equity), cssWidth - 8, y);
  }

  ctx.textAlign = 'center';
  for (const tickTime of buildEquityAxisTicks(history, 5)) {
    const label = formatEquityAxisLabel(tickTime, range);
    drawSafeAxisLabel(ctx, label, xFor(tickTime), cssHeight - 8, cssWidth);
  }

  if (hoverIndex !== null && hoverIndex >= 0 && hoverIndex < history.length) {
    const hoveredPoint = history[hoverIndex];
    drawHoverState(
      ctx,
      hoveredPoint,
      xFor(hoveredPoint.at),
      yFor(hoveredPoint.equity),
      cssWidth,
      cssHeight,
      margin
    );
  }
}

function getHoveredEquityIndex(event) {
  const canvas = $('equityChartCanvas');
  if (!canvas || !latestRenderedSeries.length) return null;
  const rect = canvas.getBoundingClientRect();
  if (!rect.width) return null;
  const clientX = eventClientX(event);
  if (clientX === null) return null;
  const relativeX = clamp(clientX - rect.left, 0, rect.width);
  const minTime = latestRenderedSeries[0]?.at ?? 0;
  const maxTime = latestRenderedSeries[latestRenderedSeries.length - 1]?.at ?? minTime;
  const timeSpan = Math.max(maxTime - minTime, 1);
  const plotWidth = rect.width - CHART_MARGIN.left - CHART_MARGIN.right;

  let bestIndex = 0;
  let bestDistance = Number.POSITIVE_INFINITY;
  for (let index = 0; index < latestRenderedSeries.length; index++) {
    const pointX =
      latestRenderedSeries.length === 1
        ? rect.width / 2
        : CHART_MARGIN.left + ((latestRenderedSeries[index].at - minTime) / timeSpan) * plotWidth;
    const distance = Math.abs(pointX - relativeX);
    if (distance < bestDistance) {
      bestDistance = distance;
      bestIndex = index;
    }
  }

  return bestIndex;
}

function handleEquityHover(event) {
  const nextIndex = getHoveredEquityIndex(event);
  if (nextIndex === hoveredEquityIndex) return;
  hoveredEquityIndex = nextIndex;
  redrawEquityChart();
}

function clearEquityHover() {
  if (hoveredEquityIndex === null) return;
  hoveredEquityIndex = null;
  redrawEquityChart();
}

function handleEquityClick() {
  triggerEquityShimmer();
  redrawEquityChart();
}

function renderEquityChart(
  equityHistory,
  currentEquity,
  currentUnrealized = 0,
  options = {}
) {
  const { skipFlash = false, alreadyNormalized = false } = options;
  const now = Date.now();
  const normalizedHistory = alreadyNormalized
    ? equityHistory || []
    : normalizeEquityHistory(equityHistory, currentEquity);
  if (!hasPlayedInitialEquityShimmer && normalizedHistory.length >= 2) {
    scheduleInitialEquityShimmer();
  } else if (now >= nextEquityShimmerAt && equityShimmerUntil <= now) {
    triggerEquityShimmer(now, { rescheduleAuto: true });
  }

  if (!skipFlash) {
    const nextDataSignature = buildEquityDataSignature(equityHistory, currentEquity);
    if (latestEquityDataSignature && latestEquityDataSignature !== nextDataSignature) {
      equityFlashUntil = Date.now() + EQUITY_FLASH_DURATION_MS;
    }
    latestEquityDataSignature = nextDataSignature;
  }
  latestEquityHistory = normalizedHistory;
  latestAccountEquity = currentEquity;
  latestUnrealizedPnl = currentUnrealized;
  const range = isSupportedEquityRange(selectedEquityRange)
    ? selectedEquityRange
    : DEFAULT_EQUITY_RANGE;
  selectedEquityRange = range;
  const series = getEquitySeriesForRange(latestEquityHistory, range, now);
  latestRenderedSeries = series;
  const hoveredPoint =
    hoveredEquityIndex !== null && series[hoveredEquityIndex] ? series[hoveredEquityIndex] : null;

  const capEl = $('equityChartCaption');
  if (capEl) {
    capEl.textContent = latestEquityHistory.length
      ? `Tracking since ${formatCompactDate(latestEquityHistory[0].at)}`
      : 'Tracking starts after the first balance snapshot';
  }
  syncEquitySummary(series, currentEquity, hoveredPoint);

  document.querySelectorAll('[data-equity-range]').forEach((button) => {
    button.classList.toggle('active', button.getAttribute('data-equity-range') === range);
  });

  drawEquityChart(series, range, latestUnrealizedPnl, hoveredEquityIndex);

  if (equityFlashUntil > now || equityShimmerUntil > now) {
    if (equityFlashFrame !== null) {
      window.cancelAnimationFrame(equityFlashFrame);
      equityFlashFrame = null;
    }
    equityFlashFrame = window.requestAnimationFrame(() => {
      equityFlashFrame = null;
      redrawEquityChart();
    });
  } else if (equityFlashFrame !== null) {
    window.cancelAnimationFrame(equityFlashFrame);
    equityFlashFrame = null;
  }
}

function redrawEquityChart() {
  renderEquityChart(latestEquityHistory, latestAccountEquity, latestUnrealizedPnl, {
    skipFlash: true,
    alreadyNormalized: true,
  });
}

function bindEquityTimeframes() {
  scheduleEquityShimmer();
  bindEquityShimmerVisibilityReplay();
  document.querySelectorAll('[data-equity-range]').forEach((button) => {
    if (button.dataset.bound === 'true') return;
    button.dataset.bound = 'true';
    button.addEventListener('click', () => {
      selectedEquityRange = button.getAttribute('data-equity-range') || DEFAULT_EQUITY_RANGE;
      redrawEquityChart();
    });
  });

  const canvas = $('equityChartCanvas');
  if (!canvas || canvas.dataset.hoverBound === 'true') return;
  canvas.dataset.hoverBound = 'true';
  canvas.addEventListener('mousemove', handleEquityHover);
  canvas.addEventListener('mouseleave', clearEquityHover);
  canvas.addEventListener('click', handleEquityClick);
  canvas.addEventListener('touchstart', handleEquityHover, { passive: true });
  canvas.addEventListener('touchmove', handleEquityHover, { passive: true });
  canvas.addEventListener('touchend', clearEquityHover);
  canvas.addEventListener('touchcancel', clearEquityHover);
}

function formatCompactDateTime(ts) {
  const d = new Date(ts);
  const hh = String(d.getHours()).padStart(2, '0');
  const mm = String(d.getMinutes()).padStart(2, '0');
  const mo = String(d.getMonth() + 1).padStart(2, '0');
  const da = String(d.getDate()).padStart(2, '0');
  return `${mo}/${da} ${hh}:${mm}`;
}

async function refreshEquity() {
  try {
    const res = await fetch('/api/equity');
    const data = await res.json();
    const history = data.history || [];
    const currentEquity = data.current_equity || 0;
    const unrealized = metricTargets.uplTotal || 0;
    renderEquityChart(history, currentEquity, unrealized);
  } catch (_) {
    // ignore — chart will show placeholder until data arrives
  }
}

/* Resize observer for equity chart */
const equityChartResizeObserver = new ResizeObserver(() => {
  if (latestRenderedSeries.length) redrawEquityChart();
});

document.addEventListener('DOMContentLoaded', () => {
  const canvas = $('equityChartCanvas');
  if (canvas) equityChartResizeObserver.observe(canvas);
  bindEquityTimeframes();
});

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
      const ns = $("numbers-stream");
      if (ns) ns.innerHTML = "";
      lastStream = {
        equity: null,
        available: null,
        uplTotal: null,
        positionsSig: null,
        tradesSig: null,
        researchSig: null,
        lastDecisionSig: null,
        baselineSig: null,
      };
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

// ─── Agent CLI ─────────────────────────────────────────────────────────────
const agentCliLogEl = () => $("agent-cli-log");

function addAgentCliEntry(role, text) {
  const div = document.createElement("div");
  div.className = `msg ${role}`;
  const who = role === "user" ? "You" : "Agent CLI";
  div.innerHTML = `<div class="who">${who}</div><div class="body"><pre style="white-space:pre-wrap;word-break:break-word;">${text.replace(/</g, "&lt;")}</pre></div>`;
  agentCliLogEl().appendChild(div);
  agentCliLogEl().scrollTop = agentCliLogEl().scrollHeight;
  return div;
}

$("agent-cli-form")?.addEventListener("submit", async (e) => {
  e.preventDefault();
  const input = $("agent-cli-input");
  const text = input.value.trim();
  if (!text) return;
  input.value = "";
  addAgentCliEntry("user", text);
  const thinking = addAgentCliEntry("assistant", "Running…");
  try {
    const res = await fetch("/api/agent_cli", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ prompt: text, auto: false }),
    });
    const data = await res.json();
    thinking.remove();
    if (data.ok) {
      addAgentCliEntry("assistant", data.result);
    } else {
      addAgentCliEntry("assistant", "Error: " + (data.error || "Unknown error"));
    }
  } catch (err) {
    thinking.remove();
    addAgentCliEntry("assistant", "Error: " + err.message);
  }
});
$("agent-cli-form")?.addEventListener("submit", async (e) => {
  e.preventDefault();
  const input = $("agent-cli-input");
  const prompt = input.value.trim();
  if (!prompt) return;
  input.value = "";
  addAgentCliEntry("user", prompt);
  const thinking = addAgentCliEntry("assistant", "Running…", "thinking");
  try {
    const res = await fetch("/api/agent_cli", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ prompt, auto: false }),
    });
    const data = await res.json();
    thinking.remove();
    if (data.ok && data.result) {
      const r = data.result;
      if (r.tool_results && r.tool_results.length) {
        for (const tr of r.tool_results) {
          addAgentCliEntry("tool", `${tr.tool}: ${tr.result?.substring?.(0, 400) || JSON.stringify(tr.result)?.substring?.(0, 400) || ""}`);
        }
      }
      addAgentCliEntry("assistant", r.response || "Done.");
    } else {
      addAgentCliEntry("assistant", "Error: " + (data.error || "Unknown"));
    }
  } catch (err) {
    thinking.remove();
    addAgentCliEntry("assistant", "Error: " + err.message);
  }
});

/* ============================================================
   AGENT STACK PANEL — orchestrator status, start/stop, rotation
   ============================================================ */

let _agentDataCache = null;
let _agentCatFilter = "all";

function renderAgentCategories(categories) {
  const el = $("agent-categories");
  if (!el) return;
  const cats = ["all", ...categories];
  el.innerHTML = cats.map((c) => {
    const active = c === _agentCatFilter ? "active" : "";
    const label = c === "all" ? "All" : c.charAt(0).toUpperCase() + c.slice(1);
    return `<button class="agent-cat-btn ${active}" data-cat="${c}">${label}</button>`;
  }).join("");
  el.querySelectorAll(".agent-cat-btn").forEach((btn) => {
    btn.addEventListener("click", () => {
      _agentCatFilter = btn.dataset.cat;
      if (_agentDataCache) renderAgentList(_agentDataCache.agents, _agentDataCache.native);
    });
  });
}

function renderAgentList(agents, native) {
  const el = $("agent-list");
  if (!el) return;
  if (!agents || !agents.length) {
    el.innerHTML = "<div class='note-card'>No agents registered</div>";
    return;
  }

  const filtered = _agentCatFilter === "all" ? agents : agents.filter((a) => a.category === _agentCatFilter);

  // Sort: online first, then by category, then by name
  filtered.sort((a, b) => {
    if (a.status !== b.status) return a.status === "online" ? -1 : 1;
    if (a.category !== b.category) return (a.category || "").localeCompare(b.category || "");
    return (a.label || a.name).localeCompare(b.label || b.name);
  });

  el.innerHTML = filtered.map((a) => {
    const statusClass = a.status === "online" ? "online" : "offline";
    const modelShort = (a.openrouter_model || "").split("/").pop()?.replace(":free", "") || "—";
    const pidStr = a.pid ? `pid ${a.pid}` : "—";
    return `
      <div class="agent-card" data-agent="${a.name}">
        <div class="agent-dot ${statusClass}"></div>
        <div class="agent-info">
          <div class="agent-name">${escapeHtml(a.label || a.name)}</div>
          <div class="agent-meta">${escapeHtml(a.category || "")} · ${pidStr}</div>
        </div>
        <div class="agent-model" title="${escapeHtml(a.openrouter_model || "")}">${escapeHtml(modelShort)}</div>
        <div class="agent-actions">
          <button data-action="start" data-agent="${a.name}">Start</button>
          <button class="danger" data-action="stop" data-agent="${a.name}">Stop</button>
        </div>
      </div>
    `;
  }).join("");

  // Native components
  if (native && _agentCatFilter === "all") {
    const nativeCards = Object.entries(native).map(([key, n]) => {
      const statusClass = n.status === "online" ? "online" : "offline";
      const pidStr = n.pid ? `pid ${n.pid}` : "—";
      const count = n.count > 1 ? `×${n.count}` : "";
      return `
        <div class="agent-card" data-native="${key}">
          <div class="agent-dot ${statusClass}"></div>
          <div class="agent-info">
            <div class="agent-name">${escapeHtml(n.label || key)}</div>
            <div class="agent-meta">native · ${pidStr} ${count}</div>
          </div>
          <div class="agent-model">${escapeHtml(n.module || "")}</div>
          <div class="agent-actions"></div>
        </div>
      `;
    }).join("");
    el.insertAdjacentHTML("beforeend", nativeCards);
  }

  // Bind actions
  el.querySelectorAll("[data-action]").forEach((btn) => {
    btn.addEventListener("click", async () => {
      const agent = btn.dataset.agent;
      const action = btn.dataset.action;
      if (!agent || !action) return;
      btn.disabled = true;
      try {
        const res = await fetch(`/api/agents/${agent}/${action}`, { method: "POST" });
        const data = await res.json();
        if (!data.ok) {
          alert(`Failed to ${action} ${agent}: ${data.error || "unknown"}`);
        }
        await refreshAgents();
      } catch (err) {
        alert(`${action} ${agent} failed: ${err.message}`);
      } finally {
        btn.disabled = false;
      }
    });
  });
}

function renderRotation(rotation) {
  if (!rotation) return;
  const keyEl = $("rotation-key");
  const modelEl = $("rotation-model");
  const failEl = $("rotation-failures");
  if (keyEl) keyEl.textContent = `Key ${rotation.key_index + 1}/7`;
  if (modelEl) {
    const short = (rotation.model_name || "—").split("/").pop()?.replace(":free", "") || "—";
    modelEl.textContent = short;
    modelEl.title = rotation.model_name || "";
  }
  if (failEl) {
    const fails = rotation.failures || {};
    const totalFails = Object.values(fails).reduce((a, b) => a + b, 0);
    failEl.textContent = totalFails > 0 ? `${totalFails} failures` : "";
  }
}

async function refreshAgents() {
  try {
    const res = await fetch("/api/agents");
    const data = await res.json();
    if (!data.ok) return;
    _agentDataCache = data;

    // Extract unique categories
    const categories = [...new Set((data.agents || []).map((a) => a.category).filter(Boolean))];
    renderAgentCategories(categories);
    renderAgentList(data.agents, data.native);
    renderRotation(data.rotation);
  } catch (_) {
    // ignore
  }
}

async function startAllAgents() {
  const btn = $("start-all-agents");
  if (!btn) return;
  btn.disabled = true;
  try {
    for (const meta of (_agentDataCache?.agents || [])) {
      if (meta.status === "offline") {
        await fetch(`/api/agents/${meta.name}/start`, { method: "POST" });
        await new Promise((r) => setTimeout(r, 600));
      }
    }
    await refreshAgents();
  } catch (err) {
    alert("Start all failed: " + err.message);
  } finally {
    btn.disabled = false;
  }
}

async function stopAllAgents() {
  const btn = $("stop-all-agents");
  if (!btn) return;
  if (!confirm("Stop ALL orchestrated agents? (Native stack components are not affected)")) return;
  btn.disabled = true;
  try {
    for (const meta of (_agentDataCache?.agents || [])) {
      if (meta.status === "online") {
        await fetch(`/api/agents/${meta.name}/stop`, { method: "POST" });
        await new Promise((r) => setTimeout(r, 400));
      }
    }
    await refreshAgents();
  } catch (err) {
    alert("Stop all failed: " + err.message);
  } finally {
    btn.disabled = false;
  }
}

$("start-all-agents")?.addEventListener("click", startAllAgents);
$("stop-all-agents")?.addEventListener("click", stopAllAgents);
$("refresh-agents")?.addEventListener("click", refreshAgents);

$("baseline-metric")?.addEventListener("click", promptSetBaseline);

loadChatHistory();
setupChatScrollBehavior();
refreshStackStatus();
refreshStatus();
refreshAccount();
refreshActivity();
refreshTradesResearch();
refreshEquity(); // load equity chart
refreshAgents();
connectWs();
setInterval(refreshAccount, 3000);
setInterval(refreshStackStatus, 5000);
setInterval(refreshStatus, 30000);
setInterval(refreshActivity, 3000);
setInterval(refreshTradesResearch, 30000);
setInterval(loadChatHistory, 8000);
setInterval(refreshEquity, 15000); // refresh equity chart every 15s
setInterval(refreshAgents, 5000);
