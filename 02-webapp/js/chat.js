/* ============================================================================ */
/* chat.js                                                                      */
/* Renders chat messages and polls pending queries for completion.             */
/* ============================================================================ */

import { getQuery } from "./api.js";

const POLL_INTERVAL_MS = 2000;
const POLL_MAX_ATTEMPTS = 60;   // 2 minutes before giving up

/* ---------------------------------------------------------------------------- */
/* Module state                                                                  */
/* ---------------------------------------------------------------------------- */

let _activePolls    = {};   // queryId → intervalId
let _cancelHandlers = {};   // queryId → onComplete callback

/* ---------------------------------------------------------------------------- */
/* Public: render a full conversation history into #chat-log                    */
/* ---------------------------------------------------------------------------- */

export function renderHistory(queries) {
  const log = document.getElementById("chat-log");
  log.innerHTML = "";

  for (const q of queries) {
    appendUserMessage(q.question || "");

    if (q.status === "complete") {
      appendAssistantMessage(q.answer || "", q.sources || [], q.query_id);
    } else if (q.status === "failed") {
      appendErrorMessage("This query failed. Please try again.", q.query_id);
    } else {
      appendThinkingMessage(q.query_id);
    }
  }

  scrollToBottom();
}

/* ---------------------------------------------------------------------------- */
/* Public: append a user bubble immediately on submit                           */
/* ---------------------------------------------------------------------------- */

export function appendUserBubble(text) {
  appendUserMessage(text);
  scrollToBottom();
}

/* ---------------------------------------------------------------------------- */
/* Public: append a thinking bubble and start polling for query_id             */
/* ---------------------------------------------------------------------------- */

export function appendPendingBubble(convId, queryId, onComplete) {
  appendThinkingMessage(queryId);
  scrollToBottom();
  _cancelHandlers[queryId] = onComplete;
  _startPolling(convId, queryId, onComplete);
}

/* ---------------------------------------------------------------------------- */
/* Public: stop all active polls (e.g. when switching conversations)           */
/* ---------------------------------------------------------------------------- */

export function stopAllPolls() {
  for (const id of Object.values(_activePolls)) {
    clearInterval(id);
  }
  _activePolls    = {};
  _cancelHandlers = {};
}

/* ---------------------------------------------------------------------------- */
/* Internal message builders                                                     */
/* ---------------------------------------------------------------------------- */

function appendUserMessage(text) {
  const log = document.getElementById("chat-log");
  const row = document.createElement("div");
  row.className = "msg-row msg-row--user";

  const bubble = document.createElement("div");
  bubble.className = "msg-bubble";
  bubble.textContent = text;

  row.appendChild(bubble);
  log.appendChild(row);
}

function appendThinkingMessage(queryId) {
  const log = document.getElementById("chat-log");
  const row = document.createElement("div");
  row.className = "msg-row msg-row--assistant";
  row.dataset.queryId = queryId;

  const bubble = document.createElement("div");
  bubble.className = "msg-bubble";

  const dots = document.createElement("div");
  dots.className = "thinking-dots";
  dots.innerHTML = "<span></span><span></span><span></span>";

  const cancelBtn = document.createElement("button");
  cancelBtn.className = "cancel-query-btn";
  cancelBtn.textContent = "Cancel";
  cancelBtn.addEventListener("click", () => _cancelQuery(queryId));

  bubble.appendChild(dots);
  bubble.appendChild(cancelBtn);
  row.appendChild(bubble);
  log.appendChild(row);
}

function appendAssistantMessage(text, sources, queryId) {
  const log   = document.getElementById("chat-log");
  const existing = queryId
    ? log.querySelector(`[data-query-id="${queryId}"]`)
    : null;

  const row = existing || document.createElement("div");
  row.className = "msg-row msg-row--assistant";
  if (queryId) row.dataset.queryId = queryId;

  const bubble = document.createElement("div");
  bubble.className = "msg-bubble";

  const body = document.createElement("div");
  body.className = "msg-markdown";
  if (window.marked) {
    const renderer = new window.marked.Renderer();
    renderer.link = ({ href, title, text }) =>
      `<a href="${href}" target="_blank" rel="noopener noreferrer" title="${title || href}">${text}</a>`;
    body.innerHTML = window.marked.parse(text, { renderer });
  } else {
    body.innerHTML = text.replace(/\n/g, "<br>");
  }
  bubble.appendChild(body);

  // Sources section
  if (sources && sources.length > 0) {
    bubble.appendChild(_buildSources(sources));
  }

  row.innerHTML = "";
  row.appendChild(bubble);

  if (!existing) {
    log.appendChild(row);
  }
}

function appendErrorMessage(text, queryId) {
  const log = document.getElementById("chat-log");
  const existing = queryId
    ? log.querySelector(`[data-query-id="${queryId}"]`)
    : null;

  const row = existing || document.createElement("div");
  row.className = "msg-row msg-row--assistant";
  if (queryId) row.dataset.queryId = queryId;

  const bubble = document.createElement("div");
  bubble.className = "msg-bubble";
  bubble.style.color = "var(--ring-danger)";
  bubble.textContent = text;

  row.innerHTML = "";
  row.appendChild(bubble);

  if (!existing) log.appendChild(row);
}

/* ---------------------------------------------------------------------------- */
/* Sources widget                                                                */
/* ---------------------------------------------------------------------------- */

function _buildSources(sources) {
  const section = document.createElement("div");
  section.className = "sources-section";

  const toggle = document.createElement("button");
  toggle.className = "sources-toggle";
  toggle.innerHTML = `
    <svg xmlns="http://www.w3.org/2000/svg" width="12" height="12"
         viewBox="0 0 24 24" fill="none" stroke="currentColor"
         stroke-width="2.5" stroke-linecap="round" stroke-linejoin="round">
      <polyline points="9 18 15 12 9 6"/>
    </svg>
    ${sources.length} source${sources.length !== 1 ? "s" : ""}`;

  const list = document.createElement("div");
  list.className = "sources-list";

  for (const src of sources) {
    const a = document.createElement("a");
    a.className = "source-link";
    a.href      = src.source_url || "#";
    a.target    = "_blank";
    a.rel       = "noopener noreferrer";

    const label = src.title || src.file || src.repo || src.source_url || "source";
    a.textContent = `↗ ${label}`;
    list.appendChild(a);
  }

  toggle.addEventListener("click", () => {
    const open = list.classList.toggle("visible");
    toggle.classList.toggle("open", open);
  });

  section.appendChild(toggle);
  section.appendChild(list);
  return section;
}

/* ---------------------------------------------------------------------------- */
/* Polling                                                                       */
/* ---------------------------------------------------------------------------- */

function _startPolling(convId, queryId, onComplete) {
  let attempts = 0;

  const intervalId = setInterval(async () => {
    attempts++;

    // Give up after POLL_MAX_ATTEMPTS — worker likely crashed before updating status
    if (attempts > POLL_MAX_ATTEMPTS) {
      _stopPolling(queryId);
      delete _cancelHandlers[queryId];
      appendErrorMessage("Query timed out. Please try again.", queryId);
      scrollToBottom();
      if (onComplete) onComplete({ status: "failed" });
      return;
    }

    try {
      const q = await getQuery(convId, queryId);

      if (q.status === "complete") {
        _stopPolling(queryId);
        delete _cancelHandlers[queryId];
        appendAssistantMessage(q.answer || "", q.sources || [], queryId);
        scrollToBottom();
        if (onComplete) onComplete(q);
      } else if (q.status === "failed") {
        _stopPolling(queryId);
        delete _cancelHandlers[queryId];
        appendErrorMessage("Query failed. Please try again.", queryId);
        scrollToBottom();
        if (onComplete) onComplete(q);
      }
    } catch (err) {
      // Transient network error — keep polling until timeout
      console.warn("Poll error for query", queryId, err);
    }
  }, POLL_INTERVAL_MS);

  _activePolls[queryId] = intervalId;
}

function _stopPolling(queryId) {
  if (_activePolls[queryId]) {
    clearInterval(_activePolls[queryId]);
    delete _activePolls[queryId];
  }
}

function _cancelQuery(queryId) {
  _stopPolling(queryId);
  const onComplete = _cancelHandlers[queryId];
  delete _cancelHandlers[queryId];
  appendErrorMessage("Query cancelled.", queryId);
  scrollToBottom();
  if (onComplete) onComplete({ status: "cancelled" });
}

/* ---------------------------------------------------------------------------- */
/* Scroll helper                                                                 */
/* ---------------------------------------------------------------------------- */

function scrollToBottom() {
  const log = document.getElementById("chat-log");
  log.scrollTop = log.scrollHeight;
}
