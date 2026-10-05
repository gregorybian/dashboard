const main = document.getElementById("main");
const statusEl = document.getElementById("status");
const tasksEl = document.getElementById("tasks");
let tab = "todo";
let fastPoll = null;
try { tab = localStorage.getItem("tab") || "todo"; } catch (e) {}
if (tab === "today") tab = "todo";

// Replace a region's HTML. Unless forced, skip it while you're typing in one of its fields.
function render(el, html, force) {
  const a = document.activeElement;
  if (!force && el.contains(a) && a.matches("input, textarea")) return;
  const openDetails = el.querySelector("details.closed[open]");
  // Keep a half-typed new task across re-renders.
  const draft = el.querySelector("#todo-add");
  const saved = draft && { text: draft.elements.text.value, description: draft.elements.description.value };
  el.innerHTML = html;
  if (openDetails) el.querySelector("details.closed")?.setAttribute("open", "");
  const form = el.querySelector("#todo-add");
  if (form && saved) for (const k in saved) form.elements[k].value = saved[k];
}

async function load(force = false) {
  try {
    const [page, head, tasks] = await Promise.all([
      fetch(`/partials/${tab}`).then(r => r.text()),
      fetch("/partials/header").then(r => r.text()),
      fetch("/partials/tasks").then(r => r.text()),
    ]);
    render(main, page, force);
    render(tasksEl, tasks, force);
    document.getElementById("sb-count").textContent = Number(tasksEl.querySelector(".tasks")?.dataset.n) || "";
    statusEl.innerHTML = head;
    const v = statusEl.querySelector(".statusbar")?.dataset.version;
    if (v && v !== document.body.dataset.version) return location.reload();
    updateCounts();
    // Poll faster while a review is in progress.
    clearTimeout(fastPoll);
    if (main.querySelector(".rv-running")) fastPoll = setTimeout(load, 5000);
  } catch (e) {
    statusEl.innerHTML = `<div class="statusbar"><span class="sync err">dashboard server unreachable</span></div>`;
  }
}

function updateCounts() {
  const bar = statusEl.querySelector(".statusbar");
  if (!bar) return;
  const total = Number(bar.dataset.total || 0);
  const n = { todo: total };
  bar.querySelectorAll("[data-src]").forEach(s => { n[s.dataset.src] = Number(s.dataset.n); });
  document.querySelectorAll("[data-count]").forEach(el => {
    const v = n[el.dataset.count] || 0;
    el.textContent = v ? v : "";
  });
  document.title = total ? `(${total}) Solar Dashboard` : "Solar Dashboard";
}

function selectTab(t) {
  tab = t;
  try { localStorage.setItem("tab", t); } catch (e) {}
  document.querySelectorAll(".tab").forEach(b => b.classList.toggle("active", b.dataset.tab === t));
  load();
}

document.addEventListener("click", async e => {
  const tabBtn = e.target.closest(".tab");
  if (tabBtn) return selectTab(tabBtn.dataset.tab);
  const editBtn = e.target.closest("[data-edit-desc]");
  if (editBtn) {
    const card = editBtn.closest(".todo");
    card.classList.add("editing");
    const ta = card.querySelector(".todo-desc-edit");
    ta.focus();
    ta.setSelectionRange(ta.value.length, ta.value.length);
    return;
  }
  const todo = e.target.closest("[data-todo]");
  if (todo) {
    todo.closest(".todo, .closed-row")?.classList.add("leaving");
    await fetch(`/api/todos/${todo.dataset.id}/${todo.dataset.todo}`, { method: "POST" });
    return load(true);
  }
  const act = e.target.closest("[data-action]");
  if (act) {
    act.closest(".item, .closed-row")?.classList.add("leaving");
    await fetch(`/api/items/${act.dataset.action}/${encodeURIComponent(act.dataset.id)}`, { method: "POST" });
    return load(true);
  }
  // Buttons that post something publicly: first click arms, second click (within 5s) sends.
  // No native confirm() dialog, which browsers can silently block.
  const btn = e.target.closest("[data-post]");
  if (btn) {
    if (!btn.classList.contains("armed")) {
      const label = btn.innerHTML;
      btn.classList.add("armed");
      btn.textContent = btn.dataset.confirm;
      setTimeout(() => {
        if (btn.isConnected && btn.classList.contains("armed")) {
          btn.classList.remove("armed");
          btn.innerHTML = label;
        }
      }, 5000);
      return;
    }
    btn.classList.remove("armed");
    btn.disabled = true;
    btn.innerHTML = '<span class="spinner"></span>' + (btn.dataset.busy || "Working…");
    const r = await fetch(btn.dataset.post, { method: "POST" });
    const data = await r.json().catch(() => ({}));
    if (!r.ok) {
      btn.outerHTML = `<span class="rv-fail">⚠ ${data.detail || `Failed (${r.status})`}</span>`;
      return;
    }
    if (data.message) btn.outerHTML = `<span class="rv-ok">✓ ${data.message}</span>`;
    return setTimeout(() => load(true), data.message ? 3000 : 0);
  }
  if (e.target.id === "refresh") {
    e.target.disabled = true;
    await fetch("/api/refresh", { method: "POST" });
    setTimeout(() => { e.target.disabled = false; load(); }, 4000);
  }
});

document.addEventListener("submit", async e => {
  if (e.target.id !== "todo-add") return;
  e.preventDefault();
  const { text, description } = e.target.elements;
  if (!text.value.trim()) return;
  await fetch("/api/todos", {
    method: "POST", headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ text: text.value, description: description.value }),
  });
  e.target.reset();
  await load(true);
  tasksEl.querySelector("#todo-add input")?.focus();
});

// Description edits save when the box loses focus; Ctrl/Cmd+Enter saves, Esc cancels.
async function saveDesc(ta) {
  if (ta.value !== ta.defaultValue) {
    await fetch(`/api/todos/${ta.dataset.id}/description`, {
      method: "PUT", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ description: ta.value }),
    });
  }
  load(true);
}
document.addEventListener("focusout", e => {
  if (e.target.matches(".todo-desc-edit")) saveDesc(e.target);
});
document.addEventListener("keydown", e => {
  const t = e.target;
  if (t.matches(".todo-desc-edit")) {
    if (e.key === "Escape") { t.value = t.defaultValue; t.blur(); }
    else if (e.key === "Enter" && (e.ctrlKey || e.metaKey)) t.blur();
  } else if (t.matches("#todo-add textarea") && e.key === "Enter" && (e.ctrlKey || e.metaKey)) {
    t.form.requestSubmit();
  }
});

selectTab(tab);
setInterval(load, 30000);
