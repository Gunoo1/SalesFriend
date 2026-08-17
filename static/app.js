/* SalesAgent frontend — vanilla JS, PriceEngine pattern (fetch + innerHTML).
   SSE arrives on the POST /message response body (fetch ReadableStream —
   EventSource can't POST).

   Chat architecture: ONE PANE PER CONVERSATION (.msg-pane[data-cid]) so a
   streaming response always lands in the conversation that asked for it,
   even if the rep switches conversations mid-stream. Streams are tracked
   per-conversation in State.streams with an AbortController — that's the
   Stop button. Workspace events (artifacts/jobs) only render live for the
   ACTIVE conversation; switching back re-renders from the DB. */
"use strict";

const $ = (sel) => document.querySelector(sel);

function escapeHtml(s) {
  return String(s ?? "").replace(/[&<>"']/g, (c) => ({
    "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;",
  }[c]));
}

async function api(path, opts = {}) {
  const res = await fetch(path, {
    headers: { "Content-Type": "application/json" },
    credentials: "same-origin",
    ...opts,
  });
  if (res.status === 401) { showLogin(); throw new Error("not logged in"); }
  if (!res.ok) {
    let detail = res.statusText;
    try { detail = (await res.json()).detail || detail; } catch (e) { /* keep */ }
    throw new Error(detail);
  }
  return res.json();
}

/* Parse an SSE stream from a fetch response body. opts.signal aborts it. */
async function streamSSE(url, body, onEvent, opts = {}) {
  const res = await fetch(url, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    credentials: "same-origin",
    body: JSON.stringify(body),
    signal: opts.signal,
  });
  if (!res.ok) {
    let detail = res.statusText;
    try { detail = (await res.json()).detail || detail; } catch (e) { /* keep */ }
    throw new Error(detail);
  }
  const reader = res.body.getReader();
  const dec = new TextDecoder();
  let buf = "";
  for (;;) {
    const { done, value } = await reader.read();
    if (done) break;
    buf += dec.decode(value, { stream: true });
    let idx;
    while ((idx = buf.indexOf("\n\n")) >= 0) {
      const chunk = buf.slice(0, idx);
      buf = buf.slice(idx + 2);
      if (!chunk || chunk.startsWith(":")) continue;   // keepalive comment
      let ev = "message", data = "";
      for (const line of chunk.split("\n")) {
        if (line.startsWith("event:")) ev = line.slice(6).trim();
        else if (line.startsWith("data:")) data += line.slice(5).trim();
      }
      let parsed = {};
      try { parsed = data ? JSON.parse(data) : {}; } catch (e) { parsed = { raw: data }; }
      onEvent(ev, parsed);
    }
  }
}

/* ---------------- state ---------------- */
const State = {
  me: null,
  cid: null,             // active conversation
  streams: {},           // cid -> {controller}   (live turn per conversation)
  toolGroups: null,
};

/* ---------------- example prompts (shown in welcome + help modal) -------- */
const EXAMPLE_PROMPTS = [
  { label: "Districts & schools", prompts: [
    "Show me the 20 biggest school districts in New Jersey with science sections and Title I dollars",
    "Find TX districts with 200+ science sections and over $5M in Title I funding, sorted by capital equipment spend",
    "Give me a profile of Red Clay Consolidated in Delaware",
    "Write a sales brief for pitching Eisco lab equipment to Newark Public Schools NJ",
  ]},
  { label: "Change the view (after a table appears)", prompts: [
    "Only show ones over 10,000 students",
    "Chart it as a bar chart by state",
    "Put them on a map",
    "Undo that",
    "Export all of this to Excel",
  ]},
  { label: "Who buys — gov spend & checkbooks", prompts: [
    "Which federal agencies buy from Uline and what do they buy?",
    "Which Delaware school districts pay Fisher Scientific through their checkbooks?",
    "What do Delaware schools buy alongside lab supplies?",
  ]},
  { label: "Contacts (Seamless)", prompts: [
    "Do we already have contacts for the top 10 districts in that table?",
    "Preview purchasing contacts at Trenton Public Schools NJ with Seamless, then reveal the best 2",
  ]},
  { label: "Company locations & open/closed", prompts: [
    "Find all Grainger locations in New Jersey on a map",
    "Find all Fastenal branches in Delaware",
    "Check if those locations are still open",
    "Find universities and labs within 25 miles of Newark NJ",
  ]},
  { label: "Timing & market research", prompts: [
    "Which states have upcoming science adoption windows and when should we propose?",
    "Who assembles OpenSciEd kits?",
    "Search the web for Carolina Biological Supply and summarize what they sell",
  ]},
  { label: "Pricing", prompts: [
    "Scrape CH0162C on VWR and show me the price grid",
    "Run a price comparison on the SKUs I uploaded against VWR and Amazon",
    "What's our Acumatica price for CH0162C?",
  ]},
  { label: "Playbooks", prompts: [
    "Find potential lab-supply customers in Delaware: big districts, nearby universities, and anyone paying Fisher — merge into one ranked list",
    "Rank Grainger's NJ branches by the K12 enrollment within 15 miles, check they're open, then get contacts for the districts around the top 3",
  ]},
];
const WELCOME_STARTERS = [
  EXAMPLE_PROMPTS[0].prompts[0],
  EXAMPLE_PROMPTS[2].prompts[0],
  EXAMPLE_PROMPTS[4].prompts[0],
  EXAMPLE_PROMPTS[5].prompts[0],
  EXAMPLE_PROMPTS[3].prompts[1],
  EXAMPLE_PROMPTS[7].prompts[0],
];

const COST_BADGES = {
  free: ["free", "cost-free"],
  cheap: ["cheap", "cost-cheap"],
  paid_credits: ["paid · approval card", "cost-paid"],
  slow_job: ["background job", "cost-job"],
};

/* ---------------- auth ---------------- */
function showLogin() {
  $("#login-view").classList.remove("hidden");
  $("#app-view").classList.add("hidden");
}
function showApp() {
  $("#login-view").classList.add("hidden");
  $("#app-view").classList.remove("hidden");
  $("#me-name").textContent = State.me.username;
  $("#admin-btn").classList.toggle("hidden", !State.me.is_admin);
}

$("#login-form").addEventListener("submit", async (e) => {
  e.preventDefault();
  $("#login-error").textContent = "";
  try {
    const out = await api("/api/login", {
      method: "POST",
      body: JSON.stringify({
        username: $("#login-user").value,
        password: $("#login-pass").value,
      }),
    });
    State.me = out.user;
    showApp();
    await boot();
  } catch (err) {
    $("#login-error").textContent = err.message;
  }
});

$("#logout").addEventListener("click", async () => {
  await api("/api/logout", { method: "POST" });
  location.reload();
});

/* ---------------- conversation panes ---------------- */
function getPane(cid) {
  return document.querySelector(`.msg-pane[data-cid="${cid}"]`);
}

function ensurePane(cid) {
  let pane = getPane(cid);
  if (!pane) {
    pane = document.createElement("div");
    pane.className = "msg-pane";
    pane.dataset.cid = cid;
    $("#messages").appendChild(pane);
  }
  return pane;
}

function showPane(cid) {
  document.querySelectorAll(".msg-pane").forEach((p) =>
    p.classList.toggle("active", p.dataset.cid === cid));
  const pane = getPane(cid);
  if (pane) pane.scrollTop = pane.scrollHeight;
}

function scrollPane(pane) {
  if (pane.classList.contains("active")) pane.scrollTop = pane.scrollHeight;
}

/* ---------------- conversations ---------------- */
async function loadConversations() {
  const out = await api("/api/conversations");
  const nav = $("#convo-list");
  nav.innerHTML = "";
  for (const c of out.conversations) {
    const div = document.createElement("div");
    div.className = "convo-item" + (c.id === State.cid ? " active" : "");
    const live = State.streams[c.id] ? '<span class="convo-live"></span>' : "";
    div.innerHTML = `${live}${escapeHtml(c.title || "(new conversation)")}`;
    div.title = c.title || c.id;
    div.onclick = () => selectConversation(c.id);
    nav.appendChild(div);
  }
  return out.conversations;
}

async function newConversation() {
  const out = await api("/api/conversations", { method: "POST" });
  await selectConversation(out.id);
  await loadConversations();
}

$("#new-convo").addEventListener("click", newConversation);

async function selectConversation(cid) {
  State.cid = cid;
  const pane = ensurePane(cid);

  // A live-streaming pane already holds the freshest content — never rebuild
  // it (that's what made responses "vanish"); everything else re-renders
  // from the DB.
  if (!State.streams[cid]) {
    const out = await api(`/api/conversations/${cid}`);
    pane.innerHTML = "";
    for (const m of out.messages) {
      if (m.role === "user") {
        addBubble(pane, "user", m.content.text, { ts: m.created_at });
      } else if (m.content.events && m.content.events.length) {
        // full render log persisted with the turn: thinking, tool chips and
        // text runs replay in their original order
        replayEvents(pane, m.content.events);
        if (m.content.error) addBubble(pane, "assistant", "", { error: m.content.error });
        addTimeStamp(pane, m.created_at, "assistant");
      } else {
        addBubble(pane, "assistant", m.content.text || "",
                  { error: m.content.error, ts: m.created_at });
      }
    }
    // turn still waiting on an approval? put the card back
    if (out.conversation.status === "awaiting_confirmation") {
      const lastA = [...out.messages].reverse().find((m) => m.role === "assistant");
      const cf = (lastA?.content?.events || [])
        .filter((e) => e.kind === "confirm").pop();
      if (cf) renderConfirmCard(cid, cf);
    }
    if (!out.messages.length) renderWelcome(pane);
    // workspace: re-render this conversation's artifacts
    Artifacts.reset();
    for (const a of out.artifacts) {
      if (a.latest_version && !a.archived) Artifacts.onArtifactEvent({
        artifact_id: a.artifact_id, version: a.latest_version,
        kind: a.kind, title: a.title,
      });
    }
  } else {
    // returning to a conversation that is mid-stream: workspace re-renders
    // from DB (streamed artifacts are persisted the moment they're created)
    const out = await api(`/api/conversations/${cid}`);
    Artifacts.reset();
    for (const a of out.artifacts) {
      if (a.latest_version && !a.archived) Artifacts.onArtifactEvent({
        artifact_id: a.artifact_id, version: a.latest_version,
        kind: a.kind, title: a.title,
      });
    }
  }
  showPane(cid);
  updateComposer();
  await loadConversations();
}

/* ---------------- chat rendering (all pane-scoped) ---------------- */
/* Compact Eastern-time stamp: "2:41 PM ET" today, "8/11 2:41 PM ET" older. */
function etStamp(ts, opts = {}) {
  if (!ts) return "—";
  const d = new Date(ts);
  if (isNaN(d)) return String(ts);
  const tz = { timeZone: "America/New_York" };
  const day = new Intl.DateTimeFormat("en-US", { ...tz, month: "numeric", day: "numeric" });
  const time = new Intl.DateTimeFormat("en-US", { ...tz, hour: "numeric", minute: "2-digit" });
  const sameDay = day.format(d) === day.format(new Date());
  return (opts.alwaysDate || !sameDay ? day.format(d) + " " : "")
    + time.format(d) + " ET";
}

function addTimeStamp(pane, ts, role) {
  const t = document.createElement("div");
  t.className = "msg-time" + (role === "user" ? " right" : "");
  t.textContent = etStamp(ts);
  t.title = String(ts) + " (UTC)";
  pane.appendChild(t);
  return t;
}

function addBubble(pane, role, text, opts = {}) {
  const wrap = document.createElement("div");
  wrap.className = `msg ${role}`;
  const b = document.createElement("div");
  b.className = "bubble" + (opts.error ? " error" : "");
  if (opts.error) {
    b.textContent = `Error: ${opts.error}`;
  } else if (role === "assistant") {
    // agent replies are markdown — render via the escape-first subset renderer
    b._raw = text || "";
    b.innerHTML = Artifacts.mdToHtml(b._raw);
  } else {
    b.textContent = text;
  }
  wrap.appendChild(b);
  pane.appendChild(wrap);
  if (opts.ts) addTimeStamp(pane, opts.ts, role);
  scrollPane(pane);
  return b;
}

function addToolChip(pane, name, argsPreview) {
  const chip = document.createElement("div");
  chip.className = "tool-chip";
  const args = argsPreview
    ? ` <code class="targs" title="${escapeHtml(argsPreview)}">${escapeHtml(String(argsPreview).slice(0, 110))}</code>`
    : "";
  chip.innerHTML = `<span class="spin"></span> <span class="tname">${escapeHtml(name)}</span>${args}`;
  pane.appendChild(chip);
  scrollPane(pane);
  return chip;
}

function addThinkingBlock(pane) {
  const d = document.createElement("div");
  d.className = "think-block";
  d.innerHTML = `
    <div class="think-head">
      <span class="think-caret">&#9662;</span>
      <span class="think-label">Thinking&hellip;</span>
    </div>
    <div class="think-body"></div>`;
  d.querySelector(".think-head").onclick = () => d.classList.toggle("collapsed");
  pane.appendChild(d);
  scrollPane(pane);
  return d;
}

function addNoteChip(pane, text, variant) {
  const chip = document.createElement("div");
  chip.className = "tool-chip " + (variant || "done");
  chip.innerHTML = `<span class="tname">${escapeHtml(text)}</span>`;
  pane.appendChild(chip);
  scrollPane(pane);
  return chip;
}

function settleChip(chip, data) {
  chip.classList.add(data.ok === false ? "failed" : "done");
  chip.querySelector(".spin")?.remove();
  const extra = data.row_count != null ? ` · ${data.row_count} rows` : "";
  const cache = data.cache_hit ? " · cached" : "";
  chip.querySelector(".tname").textContent = `${data.tool}${extra}${cache}`;
}

/* Rebuild a persisted turn exactly as it streamed: thinking blocks
   (collapsed), tool chips with their results, text bubbles, notes. */
function replayEvents(pane, events) {
  for (const ev of events) {
    if (ev.kind === "thinking") {
      const tb = addThinkingBlock(pane);
      tb.querySelector(".think-body").textContent = ev.text || "";
      tb.classList.add("collapsed", "done");
      tb.querySelector(".think-label").textContent = "Thought about it";
    } else if (ev.kind === "tool") {
      const chip = addToolChip(pane, ev.tool || "?", ev.args_preview);
      if (ev.ok == null) chip.querySelector(".spin")?.remove();  // never finished
      else settleChip(chip, ev);
    } else if (ev.kind === "text") {
      if (ev.text) addBubble(pane, "assistant", ev.text);
    } else if (ev.kind === "confirm") {
      addNoteChip(pane, `approval requested — ${ev.description || "paid action"}`);
    } else if (ev.kind === "note") {
      addNoteChip(pane, ev.text || "", ev.variant);
    }
  }
}

/* One handler per agent-turn stream, bound to ITS conversation's pane.
   Workspace events only render live when that conversation is active. */
function makeTurnHandler(cid) {
  const pane = ensurePane(cid);
  let streamBubble = null;
  let thinkBlock = null;
  const chips = {};   // tool name -> chip element

  const ensureBubble = () => {
    if (!streamBubble) streamBubble = addBubble(pane, "assistant", "");
    return streamBubble;
  };
  const settleThinking = () => {
    if (thinkBlock) {
      thinkBlock.classList.add("collapsed", "done");
      thinkBlock.querySelector(".think-label").textContent = "Thought about it";
      thinkBlock = null;
    }
  };
  const isActive = () => State.cid === cid;

  return (ev, data) => {
    if (ev === "thinking") {
      if (!thinkBlock) thinkBlock = addThinkingBlock(pane);
      thinkBlock.querySelector(".think-body").textContent += data.text || "";
      scrollPane(pane);
    } else if (ev === "token") {
      settleThinking();
      const b = ensureBubble();
      b._raw = (b._raw || "") + (data.text || "");
      b.innerHTML = Artifacts.mdToHtml(b._raw);   // live re-render as it streams
      scrollPane(pane);
    } else if (ev === "tool_start") {
      settleThinking();
      chips[data.tool] = addToolChip(pane, data.tool, data.args_preview);
      streamBubble = null;           // next tokens start a fresh bubble
    } else if (ev === "tool_result") {
      const chip = chips[data.tool];
      if (chip) settleChip(chip, data);
    } else if (ev === "artifact") {
      if (isActive()) Artifacts.onArtifactEvent(data);
    } else if (ev === "artifact_archived") {
      if (isActive() && data.archived !== false)
        Artifacts.removeArtifacts(data.artifact_ids);
    } else if (ev === "warning") {
      addNoteChip(pane, data.message || "", "warn");
    } else if (ev === "confirm_request") {
      settleThinking();
      renderConfirmCard(cid, data);
    } else if (ev === "job_started" || ev === "job_update" || ev === "job_done") {
      if (isActive()) Artifacts.onJobEvent(ev, data);
    } else if (ev === "done") {
      settleThinking();
      addTimeStamp(pane, new Date().toISOString(), "assistant");
    } else if (ev === "error") {
      settleThinking();
      addBubble(pane, "assistant", "", { error: data.message });
    }
  };
}

/* ---------------- sending / stopping ---------------- */
function updateComposer() {
  const live = !!State.streams[State.cid];
  const btn = $("#send-btn");
  btn.textContent = live ? "Stop" : "Send";
  btn.classList.toggle("stop", live);
  btn.disabled = false;
}

/* The first model call (and each one after tool results) can be silent for
   many seconds — show a pulsing "working…" row so sending never feels like a
   void. Removed on the first stream event; re-armed after tool results. */
function addWaitRow(pane) {
  const d = document.createElement("div");
  d.className = "wait-row";
  d.innerHTML = `<span class="wdot"></span><span class="wdot"></span>
    <span class="wdot"></span><span class="wlabel">working…</span>`;
  const t0 = Date.now();
  d._timer = setInterval(() => {
    const secs = Math.round((Date.now() - t0) / 1000);
    if (secs >= 3) d.querySelector(".wlabel").textContent = `working… ${secs}s`;
  }, 1000);
  pane.appendChild(d);
  scrollPane(pane);
  return d;
}
function removeWaitRow(d) {
  if (d) { clearInterval(d._timer); d.remove(); }
}

async function runTurnStream(cid, url, body) {
  const controller = new AbortController();
  State.streams[cid] = { controller };
  updateComposer();
  loadConversations();   // show the live dot
  const pane = ensurePane(cid);
  const handler = makeTurnHandler(cid);
  let waitRow = addWaitRow(pane);
  let rearm = null;
  const clearWait = () => {
    clearTimeout(rearm); rearm = null;
    removeWaitRow(waitRow); waitRow = null;
  };
  const wrapped = (ev, data) => {
    clearWait();
    handler(ev, data);
    if (ev === "tool_result" && State.streams[cid]) {
      // next agent step may think silently for a while — re-show shortly
      rearm = setTimeout(() => { waitRow = addWaitRow(pane); }, 600);
    }
  };
  try {
    await streamSSE(url, body, wrapped, { signal: controller.signal });
  } catch (err) {
    if (err.name === "AbortError") {
      addNoteChip(pane, "⏹ stopped — partial answer kept; just keep chatting");
    } else {
      addBubble(pane, "assistant", "", { error: err.message });
    }
  } finally {
    clearWait();
    delete State.streams[cid];
    if (State.cid === cid) updateComposer();
    loadConversations();   // refresh titles/ordering/live dots
  }
}

async function sendMessage() {
  const cid = State.cid;
  if (!cid) return;
  if (State.streams[cid]) {   // Stop button: server winds down + fetch aborts
    api(`/api/conversations/${cid}/stop`, { method: "POST" }).catch(() => {});
    State.streams[cid].controller.abort();
    return;
  }
  const input = $("#chat-input");
  const text = input.value.trim();
  if (!text) return;
  input.value = "";
  autoGrow(true);

  const pane = ensurePane(cid);
  pane.querySelector(".welcome")?.remove();
  addBubble(pane, "user", text, { ts: new Date().toISOString() });
  await runTurnStream(cid, `/api/conversations/${cid}/message`, { text });
}

$("#composer").addEventListener("submit", (e) => { e.preventDefault(); sendMessage(); });
$("#chat-input").addEventListener("keydown", (e) => {
  if (e.key === "Enter" && !e.shiftKey) { e.preventDefault(); sendMessage(); }
});

/* ---------------- composer sizing ----------------
   Default 3 lines (CSS min-height). The box grows with content but only
   UPWARD — it never shrinks mid-typing and never fights the native resize
   grip (drag the bottom-right corner). A dragged height is remembered and
   becomes the box's resting height after send. */
let manualInputH = parseInt(localStorage.getItem("sa_input_px"), 10) || 0;
let preDragInputH = 0;
if (manualInputH) $("#chat-input").style.height = manualInputH + "px";
$("#chat-input").addEventListener("mousedown", () => {
  preDragInputH = $("#chat-input").offsetHeight;
});
document.addEventListener("mouseup", () => {
  const input = $("#chat-input");
  if (preDragInputH && input.offsetHeight !== preDragInputH) {
    manualInputH = input.offsetHeight;   // user dragged the grip
    localStorage.setItem("sa_input_px", String(manualInputH));
  }
  preDragInputH = 0;
});
function autoGrow(reset) {
  const input = $("#chat-input");
  if (reset) {   // after send: back to dragged height, else 3-line default
    input.style.height = manualInputH ? manualInputH + "px" : "";
    return;
  }
  const cap = Math.round(window.innerHeight * 0.4);
  const needed = Math.min(input.scrollHeight + 2, cap);
  if (needed > input.offsetHeight) input.style.height = needed + "px";
}
$("#chat-input").addEventListener("input", () => autoGrow());

/* ---------------- draggable chat/workspace split ---------------- */
(() => {
  const split = $("#split");
  const chat = $("#chat-panel");
  const MIN_CHAT = 380, MIN_WS = 360;
  const apply = (px) => { chat.style.flex = px ? `0 1 ${px}px` : ""; };
  apply(parseInt(localStorage.getItem("sa_chat_px"), 10) || 0);

  split.addEventListener("pointerdown", (e) => {
    e.preventDefault();
    split.setPointerCapture(e.pointerId);
    document.body.classList.add("col-dragging");
    const left = chat.getBoundingClientRect().left;
    const max = document.documentElement.clientWidth - MIN_WS - left;
    const move = (ev) => apply(Math.max(MIN_CHAT, Math.min(ev.clientX - left, max)));
    const up = () => {
      document.body.classList.remove("col-dragging");
      split.removeEventListener("pointermove", move);
      split.removeEventListener("pointerup", up);
      const px = parseInt(chat.style.flexBasis, 10);
      if (px) localStorage.setItem("sa_chat_px", String(px));
    };
    split.addEventListener("pointermove", move);
    split.addEventListener("pointerup", up);
  });
  split.addEventListener("dblclick", () => {   // back to the 50/50 default
    apply(0);
    localStorage.removeItem("sa_chat_px");
  });
})();

/* ---------------- uploads ---------------- */
$("#attach-btn").classList.remove("hidden");
$("#attach-btn").addEventListener("click", () => $("#attach-input").click());
$("#attach-input").addEventListener("change", async () => {
  const file = $("#attach-input").files[0];
  const cid = State.cid;
  if (!file || !cid) return;
  $("#attach-input").value = "";
  const fd = new FormData();
  fd.append("file", file);
  const pane = ensurePane(cid);
  const chip = addToolChip(pane, `uploading ${file.name}…`);
  try {
    const res = await fetch(`/api/conversations/${cid}/upload`, {
      method: "POST", credentials: "same-origin", body: fd,
    });
    if (!res.ok) {
      let detail = res.statusText;
      try { detail = (await res.json()).detail || detail; } catch (e) { /* keep */ }
      throw new Error(detail);
    }
    const out = await res.json();
    chip.classList.add("done");
    chip.querySelector(".spin")?.remove();
    chip.querySelector(".tname").textContent =
      `uploaded ${out.filename} · ${out.rows} rows`;
    if (State.cid === cid) {
      Artifacts.onArtifactEvent({ artifact_id: out.artifact_id,
                                  version: out.version, kind: out.kind,
                                  title: out.title });
    }
  } catch (err) {
    chip.classList.add("failed");
    chip.querySelector(".spin")?.remove();
    chip.querySelector(".tname").textContent = `upload failed: ${err.message}`;
  }
});

/* ---------------- confirm card (M3) ---------------- */
function renderConfirmCard(cid, data) {
  const pane = ensurePane(cid);
  const card = document.createElement("div");
  card.className = "confirm-card";
  card.innerHTML = `
    <div class="cc-title">Approval needed — this will spend credits</div>
    <div>${escapeHtml(data.description || data.tool)}</div>
    <div>Estimated: <b>${escapeHtml(String(data.estimated_credits ?? "?"))} credits</b>
      ${data.budget ? ` · org today: ${escapeHtml(String(data.budget.org_today))}/${escapeHtml(String(data.budget.org_cap))}` : ""}</div>
    <div class="cc-actions">
      <button class="approve">Approve</button>
      <button class="deny">Deny</button>
    </div>`;
  const resume = async (approved) => {
    card.querySelectorAll("button").forEach((b) => (b.disabled = true));
    card.querySelector(".cc-title").textContent = approved ? "Approved — continuing…" : "Denied";
    await runTurnStream(cid, `/api/conversations/${cid}/resume`, { approved });
  };
  card.querySelector(".approve").onclick = () => resume(true);
  card.querySelector(".deny").onclick = () => resume(false);
  pane.appendChild(card);
  scrollPane(pane);
}

/* ---------------- welcome panel (empty conversation) ---------------- */
function fillComposer(text) {
  const input = $("#chat-input");
  input.value = text;
  autoGrow();   // expand so the whole prompt is visible for editing
  input.focus();
  closeHelp();
}

function promptChip(text) {
  const b = document.createElement("button");
  b.type = "button";
  b.className = "prompt-chip";
  b.textContent = text;
  b.onclick = () => fillComposer(text);
  return b;
}

function renderWelcome(pane) {
  if (pane.querySelector(".welcome")) return;
  const w = document.createElement("div");
  w.className = "welcome";
  w.innerHTML = `
    <div class="w-title">What can I find for you?</div>
    <div class="w-sub">Districts, contacts, gov purchases, branch locations,
      prices — results land as tables, charts and maps on the right, and you
      can reshape them by just asking ("only NJ", "chart it", "export").</div>
    <div class="w-chips"></div>
    <button type="button" class="w-more linkish">See all example prompts &amp; capabilities &#10024;</button>`;
  const chips = w.querySelector(".w-chips");
  for (const p of WELCOME_STARTERS) chips.appendChild(promptChip(p));
  w.querySelector(".w-more").onclick = () => openHelp();
  pane.appendChild(w);
}

/* ---------------- help modal (prompts + live tool catalog) ---------------- */
function renderHelpPrompts() {
  const body = $("#help-body-prompts");
  body.innerHTML = "";
  for (const group of EXAMPLE_PROMPTS) {
    const g = document.createElement("div");
    g.className = "cap-group";
    g.innerHTML = `<div class="cap-family">${escapeHtml(group.label)}</div>`;
    const chips = document.createElement("div");
    chips.className = "w-chips";
    for (const p of group.prompts) chips.appendChild(promptChip(p));
    g.appendChild(chips);
    body.appendChild(g);
  }
}

async function renderHelpTools() {
  const body = $("#help-body-tools");
  if (!State.toolGroups) {
    body.innerHTML = `<div class="cap-loading">loading…</div>`;
    try {
      State.toolGroups = (await api("/api/tools")).groups;
    } catch (err) {
      body.innerHTML = `<div class="cap-loading">couldn't load tools: ${escapeHtml(err.message)}</div>`;
      return;
    }
  }
  body.innerHTML = "";
  for (const group of State.toolGroups) {
    const g = document.createElement("div");
    g.className = "cap-group";
    g.innerHTML = `<div class="cap-family">${escapeHtml(group.family)}</div>`;
    for (const t of group.tools) {
      const [label, cls] = COST_BADGES[t.cost_class] || [t.cost_class, "cost-free"];
      const confirm = t.needs_confirmation
        ? ` <span class="cost-badge cost-paid">asks approval</span>` : "";
      const d = document.createElement("div");
      d.className = "cap-tool";
      d.innerHTML = `<div class="cap-name"><code>${escapeHtml(t.name)}</code>
          <span class="cost-badge ${cls}">${escapeHtml(label)}</span>${confirm}</div>
        <div class="cap-desc">${escapeHtml(t.description)}</div>`;
      g.appendChild(d);
    }
    body.appendChild(g);
  }
}

function openHelp(tab = "prompts") {
  renderHelpPrompts();
  setHelpTab(tab);           // renders the tools tab lazily when selected
  $("#help-overlay").classList.remove("hidden");
}
function closeHelp() { $("#help-overlay").classList.add("hidden"); }
function setHelpTab(tab) {
  document.querySelectorAll(".help-tab:not(.adm-tab)").forEach((b) =>
    b.classList.toggle("active", b.dataset.tab === tab));
  $("#help-body-prompts").classList.toggle("hidden", tab !== "prompts");
  $("#help-body-tools").classList.toggle("hidden", tab !== "tools");
  if (tab === "tools") renderHelpTools();
}

$("#help-btn").addEventListener("click", () => openHelp());
$("#help-close").addEventListener("click", closeHelp);
$("#help-overlay").addEventListener("click", (e) => {
  if (e.target === $("#help-overlay")) closeHelp();
});
document.addEventListener("keydown", (e) => {
  if (e.key === "Escape") { closeHelp(); closeAdmin(); }
});
document.querySelectorAll(".help-tab:not(.adm-tab)").forEach((b) =>
  b.addEventListener("click", () => setHelpTab(b.dataset.tab)));

/* ---------------- admin panel (users + activity) ---------------- */
/* All server timestamps are UTC; everything user-facing renders Eastern. */
function fmtTs(ts) { return etStamp(ts, { alwaysDate: true }); }

async function renderAdminUsers() {
  const box = $("#adm-users-table");
  box.innerHTML = `<div class="cap-loading">loading…</div>`;
  let users;
  try { users = (await api("/api/admin/users")).users; }
  catch (err) { box.innerHTML = `<div class="cap-loading">${escapeHtml(err.message)}</div>`; return; }
  const rows = users.map((u) => `
    <tr data-uid="${u.id}" data-name="${escapeHtml(u.username)}">
      <td><b>${escapeHtml(u.username)}</b>${u.is_admin ? ' <span class="cost-badge cost-paid">admin</span>' : ""}</td>
      <td class="num">${u.conversations}</td>
      <td class="num">${u.messages}</td>
      <td class="num">${u.credits_spent}</td>
      <td class="num">${u.jobs}</td>
      <td>${fmtTs(u.last_active || u.last_login)}</td>
      <td class="adm-actions">
        <button class="adm-pw" title="Reset password">reset pw</button>
        <button class="adm-del" title="Remove user">remove</button>
      </td>
    </tr>`).join("");
  box.innerHTML = `
    <table class="art adm-table">
      <thead><tr><th>User</th><th>Convos</th><th>Msgs</th><th>Credits</th>
        <th>Jobs</th><th>Last active</th><th></th></tr></thead>
      <tbody>${rows}</tbody>
    </table>`;
  box.querySelectorAll(".adm-del").forEach((b) => b.onclick = async () => {
    const tr = b.closest("tr");
    const name = tr.dataset.name;
    if (!confirm(`Remove user '${name}'? They are logged out immediately; their past conversations stay in the database but become inaccessible.`)) return;
    try {
      await api(`/api/admin/users/${tr.dataset.uid}`, { method: "DELETE" });
      renderAdminUsers();
    } catch (err) { alert(err.message); }
  });
  box.querySelectorAll(".adm-pw").forEach((b) => b.onclick = async () => {
    const tr = b.closest("tr");
    const pw = prompt(`New password for '${tr.dataset.name}' (min 6 chars — logs them out everywhere):`);
    if (!pw) return;
    try {
      await api(`/api/admin/users/${tr.dataset.uid}/password`, {
        method: "POST", body: JSON.stringify({ password: pw }),
      });
      alert(`Password updated for ${tr.dataset.name}.`);
    } catch (err) { alert(err.message); }
  });
}

async function renderAdminActivity() {
  const box = $("#adm-body-activity");
  box.innerHTML = `<div class="cap-loading">loading…</div>`;
  let out;
  try { out = await api("/api/admin/activity"); }
  catch (err) { box.innerHTML = `<div class="cap-loading">${escapeHtml(err.message)}</div>`; return; }
  const s = out.stats;
  const kindBadge = { message: "cost-cheap", job: "cost-job",
                      upload: "cost-free", seamless: "cost-paid" };
  const events = out.events.map((e) => `
    <tr>
      <td class="adm-ts">${fmtTs(e.ts)}</td>
      <td>${escapeHtml(e.username || "?")}</td>
      <td><span class="cost-badge ${kindBadge[e.kind] || "cost-free"}">${escapeHtml(e.kind)}</span></td>
      <td class="adm-ctx">${escapeHtml(e.context || "")}</td>
      <td class="adm-detail">${escapeHtml(e.detail || "")}</td>
    </tr>`).join("");
  box.innerHTML = `
    <div class="adm-stats">
      <span><b>${s.users}</b> users</span> · <span><b>${s.conversations}</b> conversations</span>
      · <span><b>${s.messages}</b> messages</span> · <span><b>${s.artifacts}</b> artifacts</span>
      · <span><b>${s.jobs}</b> jobs</span>
      · <span>Seamless today: <b>${s.credits_today}</b>/${s.seamless_day_cap} credits (all-time ${s.credits_total})</span>
    </div>
    <table class="art adm-table">
      <thead><tr><th>When (UTC)</th><th>User</th><th>Kind</th><th>Where</th><th>Detail</th></tr></thead>
      <tbody>${events}</tbody>
    </table>`;
}

/* Integrations tab: credentials stored server-side (app_config) over .env.
   Inputs stay EMPTY unless edited — only touched fields are sent, so saved
   secrets are never round-tripped back into the page. */
async function renderAdminIntegrations() {
  const box = $("#adm-body-integrations");
  box.innerHTML = `<div class="cap-loading">loading…</div>`;
  let out;
  try { out = await api("/api/admin/integrations"); }
  catch (err) { box.innerHTML = `<div class="cap-loading">${escapeHtml(err.message)}</div>`; return; }
  paintIntegrations(out.fields);
}

function paintIntegrations(fields) {
  const box = $("#adm-body-integrations");
  const srcBadge = { app: ["cost-paid", "set here"], env: ["cost-free", "from .env"],
                     unset: ["cost-job", "not set"] };
  const groups = [...new Set(fields.map((f) => f.group))];
  const body = groups.map((g) => `
    <div class="adm-int-group">${escapeHtml(g)}</div>
    ${fields.filter((f) => f.group === g).map((f) => {
      const [cls, txt] = srcBadge[f.source];
      return `
      <div class="adm-int-row" data-name="${f.name}">
        <label class="adm-int-label">${escapeHtml(f.label)}</label>
        <input class="adm-int-input" type="${f.secret ? "password" : "text"}"
               autocomplete="off" spellcheck="false"
               placeholder="${escapeHtml(f.preview || "(not set)")}">
        <span class="cost-badge ${cls}" title="${f.updated_by ? `by ${escapeHtml(f.updated_by)} ${fmtTs(f.updated_at)}` : ""}">${txt}</span>
        <button class="adm-int-clear linkish" ${f.source === "app" ? "" : "disabled"}
                title="Remove the value saved here and fall back to .env">clear</button>
      </div>`;
    }).join("")}`).join("");
  box.innerHTML = `
    <p class="adm-int-note">Values save to the app database and take effect
    immediately — no restart. A blank field is left unchanged; saved secrets
    show only their last 4 characters.</p>
    ${body}
    <div class="adm-int-foot">
      <button id="adm-int-save">Save changes</button>
      <span id="adm-int-msg" class="adm-msg"></span>
    </div>`;
  $("#adm-int-save").onclick = async () => {
    const values = {};
    box.querySelectorAll(".adm-int-row").forEach((row) => {
      const v = row.querySelector(".adm-int-input").value.trim();
      if (v) values[row.dataset.name] = v;
    });
    const msg = $("#adm-int-msg");
    if (!Object.keys(values).length) { msg.textContent = "nothing changed"; return; }
    msg.textContent = "saving…";
    try {
      const out = await api("/api/admin/integrations",
                            { method: "PUT", body: JSON.stringify({ values }) });
      paintIntegrations(out.fields);
      $("#adm-int-msg").textContent = "saved — active immediately";
    } catch (err) { msg.textContent = err.message; }
  };
  box.querySelectorAll(".adm-int-clear").forEach((b) => b.onclick = async () => {
    const row = b.closest(".adm-int-row");
    if (!confirm(`Clear the saved value for '${row.dataset.name}' and fall back to .env?`)) return;
    try {
      const out = await api("/api/admin/integrations",
                            { method: "PUT", body: JSON.stringify({ values: { [row.dataset.name]: "" } }) });
      paintIntegrations(out.fields);
    } catch (err) { alert(err.message); }
  });
}

function setAdminTab(tab) {
  document.querySelectorAll(".adm-tab").forEach((b) =>
    b.classList.toggle("active", b.dataset.tab === tab));
  $("#adm-body-users").classList.toggle("hidden", tab !== "users");
  $("#adm-body-activity").classList.toggle("hidden", tab !== "activity");
  $("#adm-body-integrations").classList.toggle("hidden", tab !== "integrations");
  if (tab === "users") renderAdminUsers();
  else if (tab === "integrations") renderAdminIntegrations();
  else renderAdminActivity();
}
function openAdmin() { setAdminTab("users"); $("#admin-overlay").classList.remove("hidden"); }
function closeAdmin() { $("#admin-overlay").classList.add("hidden"); }

$("#admin-btn").addEventListener("click", openAdmin);
$("#admin-close").addEventListener("click", closeAdmin);
$("#admin-overlay").addEventListener("click", (e) => {
  if (e.target === $("#admin-overlay")) closeAdmin();
});
document.querySelectorAll(".adm-tab").forEach((b) =>
  b.addEventListener("click", () => setAdminTab(b.dataset.tab)));

$("#adm-add-form").addEventListener("submit", async (e) => {
  e.preventDefault();
  const msg = $("#adm-add-msg");
  msg.textContent = "";
  try {
    const out = await api("/api/admin/users", {
      method: "POST",
      body: JSON.stringify({
        username: $("#adm-add-name").value,
        password: $("#adm-add-pass").value,
        is_admin: $("#adm-add-admin").checked,
      }),
    });
    msg.textContent = `added ${out.username}`;
    $("#adm-add-name").value = ""; $("#adm-add-pass").value = "";
    $("#adm-add-admin").checked = false;
    renderAdminUsers();
  } catch (err) {
    msg.textContent = err.message;
  }
});

/* ---------------- boot ---------------- */
async function boot() {
  const convos = await loadConversations();
  if (convos.length) await selectConversation(convos[0].id);
  else await newConversation();
}

(async function init() {
  try {
    const me = await api("/api/me");
    State.me = me;
    showApp();
    await boot();
  } catch (e) {
    showLogin();
  }
})();
