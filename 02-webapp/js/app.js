/* ============================================================================ */
/* app.js                                                                       */
/* Main application controller.                                                */
/* Initializes auth, loads conversations, handles the chat input loop.        */
/* ============================================================================ */

import { isLoggedIn, getLoginUrl, clearTokens, getUserInfo } from "./auth.js";
import {
  registerUser, getUsage,
  createConversation, listQueries, submitQuery,
} from "./api.js";
import {
  initSidebar, refreshSidebar,
  setActiveConversation, prependConversation, updateConvTitle,
} from "./sidebar.js";
import {
  renderHistory, appendUserBubble,
  appendPendingBubble, stopAllPolls,
} from "./chat.js";
import { showAlert } from "./modal.js";

/* ---------------------------------------------------------------------------- */
/* Application state                                                             */
/* ---------------------------------------------------------------------------- */

let _activeConvId = null;
let _sending      = false;

/* ---------------------------------------------------------------------------- */
/* Boot                                                                          */
/* ---------------------------------------------------------------------------- */

async function boot() {
  if (!isLoggedIn()) {
    _showSignIn();
    return;
  }

  // Register user (idempotent — creates usage record on first visit)
  try {
    const reg = await registerUser();
    if (reg?.error === "user_limit_reached") {
      _showSignIn();
      await showAlert(
        "Access unavailable",
        "The demo is currently at capacity. Email mamonaco1973@gmail.com to request access."
      );
      return;
    }
  } catch (err) {
    if (err.status === 403) {
      _showSignIn();
      await showAlert(
        "Access unavailable",
        "The demo is currently at capacity. Email mamonaco1973@gmail.com to request access."
      );
      return;
    }
  }

  // Show app shell
  document.getElementById("app-shell").classList.remove("hidden");
  document.getElementById("btn-sign-out").classList.remove("hidden");

  const { name, email } = getUserInfo();
  const userEl = document.getElementById("sidebar-user");
  userEl.textContent = name || email;
  userEl.classList.remove("hidden");

  // Init sidebar
  initSidebar({
    onSelect: _selectConversation,
    onDelete: _onConversationDeleted,
  });

  // Wire controls
  _wireControls();

  // Load sidebar + token ring
  await Promise.all([refreshSidebar(), _refreshUsage()]);
}

/* ---------------------------------------------------------------------------- */
/* Sign-in modal                                                                 */
/* ---------------------------------------------------------------------------- */

function _showSignIn() {
  document.getElementById("sign-in-modal").classList.remove("hidden");
  document.getElementById("btn-cognito-sign-in").addEventListener("click", () => {
    window.location.href = getLoginUrl();
  });
}

/* ---------------------------------------------------------------------------- */
/* Control wiring                                                                */
/* ---------------------------------------------------------------------------- */

function _wireControls() {
  // New chat button
  document.getElementById("btn-new-chat").addEventListener("click", _startNewChat);

  // Mobile sidebar toggle
  const toggle  = document.getElementById("sidebar-toggle");
  const sidebar = document.getElementById("sidebar");
  const overlay = document.getElementById("sidebar-overlay");
  if (toggle) {
    toggle.addEventListener("click", () => {
      sidebar.classList.toggle("open");
      overlay.classList.toggle("visible");
    });
    overlay.addEventListener("click", () => {
      sidebar.classList.remove("open");
      overlay.classList.remove("visible");
    });
  }

  // Sign out
  document.getElementById("btn-sign-out").addEventListener("click", () => {
    clearTokens();
    window.location.reload();
  });

  // Send button
  document.getElementById("btn-send").addEventListener("click", _handleSend);

  // Enter to send (Shift+Enter for newline)
  document.getElementById("chat-input").addEventListener("keydown", (e) => {
    if (e.key === "Enter" && !e.shiftKey) {
      e.preventDefault();
      _handleSend();
    }
  });

  // Auto-resize textarea
  document.getElementById("chat-input").addEventListener("input", _autoResize);

  // Starter question buttons
  document.querySelectorAll(".starter-btn").forEach(btn => {
    btn.addEventListener("click", () => {
      document.getElementById("chat-input").value = btn.dataset.q;
      _autoResize();
      _handleSend();
    });
  });
}

/* ---------------------------------------------------------------------------- */
/* New chat                                                                      */
/* ---------------------------------------------------------------------------- */

function _closeMobileSidebar() {
  document.getElementById("sidebar")?.classList.remove("open");
  document.getElementById("sidebar-overlay")?.classList.remove("visible");
}

async function _startNewChat() {
  stopAllPolls();
  _setSending(false);
  _closeMobileSidebar();

  try {
    const conv = await createConversation();
    _activeConvId = conv.conv_id;

    prependConversation(conv);
    setActiveConversation(_activeConvId);

    // Show empty state, hide log
    document.getElementById("empty-state").classList.remove("hidden");
    document.getElementById("chat-log").classList.add("hidden");
    document.getElementById("chat-log").innerHTML = "";

    document.getElementById("chat-input").focus();
  } catch (err) {
    console.error("Failed to create conversation", err);
    await showAlert("Error", "Failed to start a new chat. Please try again.");
  }
}

/* ---------------------------------------------------------------------------- */
/* Select existing conversation                                                  */
/* ---------------------------------------------------------------------------- */

async function _selectConversation(convId) {
  if (convId === _activeConvId) return;

  stopAllPolls();
  _setSending(false);
  _closeMobileSidebar();
  _activeConvId = convId;
  setActiveConversation(convId);

  // Show log, hide empty state
  document.getElementById("empty-state").classList.add("hidden");
  document.getElementById("chat-log").classList.remove("hidden");
  document.getElementById("chat-log").innerHTML = "";

  try {
    const queries = await listQueries(convId);
    renderHistory(queries);

    // Re-attach polls for any still-pending queries
    for (const q of queries) {
      if (q.status === "pending" || q.status === "processing") {
        appendPendingBubble(convId, q.query_id, (completed) => {
          _refreshUsage();
        });
      }
    }
  } catch (err) {
    console.error("Failed to load queries", err);
  }
}

/* ---------------------------------------------------------------------------- */
/* Handle send                                                                   */
/* ---------------------------------------------------------------------------- */

async function _handleSend() {
  if (_sending) return;

  const input    = document.getElementById("chat-input");
  const errorEl  = document.getElementById("input-error");
  const question = input.value.trim();

  errorEl.classList.add("hidden");

  if (!question) return;

  // Create a conversation if none is active
  if (!_activeConvId) {
    try {
      const conv = await createConversation();
      _activeConvId = conv.conv_id;
      prependConversation(conv);
      setActiveConversation(_activeConvId);
    } catch (err) {
      errorEl.textContent = "Failed to start conversation. Please try again.";
      errorEl.classList.remove("hidden");
      return;
    }
  }

  // Switch from empty state to chat log
  document.getElementById("empty-state").classList.add("hidden");
  const log = document.getElementById("chat-log");
  log.classList.remove("hidden");

  // Clear and lock input
  input.value = "";
  _autoResize();
  _setSending(true);

  // Append user bubble immediately
  appendUserBubble(question);

  try {
    const result = await submitQuery(_activeConvId, question);

    appendPendingBubble(_activeConvId, result.query_id, (completed) => {
      _setSending(false);
      _refreshUsage();
      if (completed.status === "complete") {
        // Refresh sidebar to pick up auto-generated title on first query
        refreshSidebar().then(() => setActiveConversation(_activeConvId));
      }
    });
  } catch (err) {
    _setSending(false);

    if (err.status === 503) {
      await showAlert(
        "Content not available",
        "The portfolio knowledge base hasn't been loaded yet. Please check back soon."
      );
    } else if (err.status === 429) {
      await showAlert(
        "Token limit reached",
        "You have used your full token budget. Email mamonaco1973@gmail.com to request a reset."
      );
    } else {
      errorEl.textContent = "Failed to send. Please try again.";
      errorEl.classList.remove("hidden");
    }
  }
}

/* ---------------------------------------------------------------------------- */
/* Conversation deleted callback                                                 */
/* ---------------------------------------------------------------------------- */

function _onConversationDeleted(convId) {
  if (convId === _activeConvId) {
    _activeConvId = null;
    document.getElementById("chat-log").innerHTML = "";
    document.getElementById("chat-log").classList.add("hidden");
    document.getElementById("empty-state").classList.remove("hidden");
  }
}

/* ---------------------------------------------------------------------------- */
/* Token usage ring                                                              */
/* ---------------------------------------------------------------------------- */

async function _refreshUsage() {
  try {
    const usage  = await getUsage();
    const used   = usage.tokens_used  || 0;
    const limit  = usage.token_limit  || 500000;
    const pct    = Math.min(100, Math.round((used / limit) * 100));

    const arc    = document.getElementById("token-ring-arc");
    const label  = document.getElementById("token-usage-label");
    const widget = document.getElementById("token-usage-widget");

    arc.setAttribute("stroke-dasharray", `${pct} ${100 - pct}`);

    if (pct >= 90) {
      arc.style.stroke = "var(--ring-danger)";
    } else if (pct >= 70) {
      arc.style.stroke = "var(--ring-warn)";
    } else {
      arc.style.stroke = "var(--ring-color)";
    }

    const usedK  = Math.round(used  / 1000);
    const limitK = Math.round(limit / 1000);
    label.textContent = `${usedK}K / ${limitK}K tokens`;

    widget.classList.remove("hidden");
  } catch (err) {
    console.warn("Failed to fetch usage", err);
  }
}

/* ---------------------------------------------------------------------------- */
/* Helpers                                                                       */
/* ---------------------------------------------------------------------------- */

function _setSending(active) {
  _sending = active;
  document.getElementById("btn-send").disabled       = active;
  document.getElementById("chat-input").disabled     = active;
}

function _autoResize() {
  const ta = document.getElementById("chat-input");
  ta.style.height = "auto";
  ta.style.height = `${Math.min(ta.scrollHeight, 200)}px`;
}

/* ---------------------------------------------------------------------------- */
/* Entry point                                                                   */
/* ---------------------------------------------------------------------------- */

boot();
