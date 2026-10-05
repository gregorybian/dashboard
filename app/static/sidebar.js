// Task sidebar: collapse/expand and drag-to-resize, both remembered per browser.
// Shared by the live page (index.html) and the portfolio snapshot (snapshot.html, inlined).
(() => {
const sidebar = document.getElementById("sidebar");
function store(k, v) { try { localStorage.setItem(k, v); } catch (e) {} }
const SB_MIN = 220, SB_DEFAULT = 340;
const sbMax = () => Math.max(SB_MIN, window.innerWidth * 0.7);
function stored(k) { try { return localStorage.getItem(k); } catch (e) { return null; } }
function setSidebarWidth(w) {
  w = Math.round(Math.min(sbMax(), Math.max(SB_MIN, w)));
  sidebar.style.setProperty("--sb-w", w + "px");
  return w;
}
function setCollapsed(c) {
  sidebar.classList.toggle("collapsed", c);
  store("sb-collapsed", c ? "1" : "");
}
setSidebarWidth(Number(stored("sb-width")) || SB_DEFAULT);
setCollapsed(!!stored("sb-collapsed"));
document.getElementById("sb-toggle").addEventListener("click", () => setCollapsed(true));
document.getElementById("sb-open").addEventListener("click", () => setCollapsed(false));

const handle = document.getElementById("sb-resize");
handle.addEventListener("pointerdown", e => {
  e.preventDefault();
  handle.setPointerCapture(e.pointerId);
  const startX = e.clientX, startW = sidebar.getBoundingClientRect().width;
  document.body.classList.add("resizing");
  const move = ev => setSidebarWidth(startW + ev.clientX - startX);
  handle.addEventListener("pointermove", move);
  handle.addEventListener("pointerup", () => {
    handle.removeEventListener("pointermove", move);
    document.body.classList.remove("resizing");
    store("sb-width", sidebar.getBoundingClientRect().width);
  }, { once: true });
});
handle.addEventListener("dblclick", () => store("sb-width", setSidebarWidth(SB_DEFAULT)));

// The sidebar sticks just under the (wrapping) header.
const topBar = document.querySelector(".top");
new ResizeObserver(() => document.body.style.setProperty("--top-h", topBar.offsetHeight + "px")).observe(topBar);
})();
