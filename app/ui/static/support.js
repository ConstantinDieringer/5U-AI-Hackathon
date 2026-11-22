"use strict";
// Support chat: questions go to /api/support/chat, answers come from a model via the 5U gateway.
// Everything is rendered as text only; nothing the model writes becomes HTML.
const SUPPORT = { open: false, busy: false, messages: [] };
const SUPPORT_HELLO = "Hi! Ask me anything about the console: where to find something, why a request is stuck, or what to do next. If you have a request open, I see its details.";
function supportPanel() {
  let panel = $("#support-panel");
  if (panel) return panel;
  panel = el("section", { id: "support-panel", class: "support-panel", role: "dialog", "aria-label": "Support chat", "aria-modal": "false" },
    el("header", { class: "support-head" },
      el("span", { class: "support-avatar" }, icon("chat")),
      el("div", {}, el("b", {}, "Support"), el("span", { class: "caption" }, "AI assistant · answers can be wrong")),
      el("button", { class: "btn ghost support-clear", title: "Start a new conversation", onclick: () => { SUPPORT.messages = []; renderSupport(); } }, icon("undo")),
      el("button", { class: "btn ghost", "aria-label": "Close support", onclick: () => toggleSupport(false) }, icon("x"))),
    el("div", { class: "support-log", id: "support-log", role: "log", "aria-live": "polite" }),
    el("form", { class: "support-form", onsubmit: e => { e.preventDefault(); sendSupport(); } },
      el("textarea", { id: "support-input", rows: "1", maxlength: "2000", placeholder: "Ask a question …", "aria-label": "Your question",
        onkeydown: e => { if (e.key === "Enter" && !e.shiftKey) { e.preventDefault(); sendSupport(); } },
        oninput: e => { e.target.style.height = "auto"; e.target.style.height = Math.min(e.target.scrollHeight, 140) + "px"; } }),
      el("button", { class: "btn primary support-send", type: "submit", "aria-label": "Send" }, icon("send"))));
  document.body.append(panel);
  return panel;
}
function renderSupport() {
  const log = $("#support-log"); if (!log) return;
  // Models like **bold**: turn only that into <b> nodes, everything else stays plain text.
  const rich = text => text.split(/\*\*(.+?)\*\*/g).map((part, i) => i % 2 ? el("b", {}, part) : part);
  const bubble = (role, text, extra = "") => el("div", { class: "support-msg " + role + extra }, ...(role === "assistant" ? rich(text) : [text]));
  log.replaceChildren(bubble("assistant", SUPPORT_HELLO),
    ...SUPPORT.messages.map(m => bubble(m.role, m.content, m.error ? " error" : "")),
    SUPPORT.busy ? el("div", { class: "support-msg assistant typing", "aria-label": "Assistant is typing" }, el("i"), el("i"), el("i")) : "");
  log.scrollTop = log.scrollHeight;
  $(".support-send").disabled = SUPPORT.busy;
}
function toggleSupport(open = !SUPPORT.open) {
  SUPPORT.open = open;
  const panel = supportPanel();
  panel.classList.toggle("open", open);
  $("#support-toggle").setAttribute("aria-expanded", String(open));
  $("#support-toggle").classList.toggle("active", open);
  if (open) { renderSupport(); $("#support-input").focus(); }
}
async function sendSupport() {
  const input = $("#support-input"), text = input.value.trim();
  if (!text || SUPPORT.busy) return;
  SUPPORT.messages.push({ role: "user", content: text.slice(0, 2000) });
  input.value = ""; input.style.height = "auto";
  SUPPORT.busy = true; renderSupport();
  try {
    const history = SUPPORT.messages.filter(m => !m.error).slice(-12).map(({ role, content }) => ({ role, content }));
    const r = await post("/api/support/chat", { messages: history, session_id: S.page === "session" ? S.selected : null });
    SUPPORT.messages.push({ role: "assistant", content: r.reply });
  } catch (e) {
    SUPPORT.messages.push({ role: "assistant", content: e.message || "The assistant is not reachable right now.", error: true });
  } finally { SUPPORT.busy = false; renderSupport(); input.focus(); }
}
$("#support-toggle").addEventListener("click", () => toggleSupport());
$("#mobile-support")?.addEventListener("click", () => toggleSupport());
document.addEventListener("keydown", e => { if (e.key === "Escape" && SUPPORT.open && e.target.closest?.("#support-panel")) toggleSupport(false); });
