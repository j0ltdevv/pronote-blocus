// Pronote Blocus PWA — logique frontend
//
// Design : coquille « espace élève » (sidebar + cartes). La logique est
// inchangée : jeton opaque en sessionStorage, API same-origin, chat libre.

const API = "";  // same origin
let token = sessionStorage.getItem("pronote_token") || null;
sessionStorage.removeItem("pronote_user_id");  // clé de la version sans jeton
let sessionId = null;
let items = [];
let filter = "all";

const REDUCED = window.matchMedia("(prefers-reduced-motion: reduce)").matches;

// ── DOM refs ──────────────────────────────────────────────────────────
const $ = (id) => document.getElementById(id);
const chatMessages = $("chatMessages");
const refreshBtn = $("refreshBtn");
const topRefresh = $("topRefresh");

// ── Helpers ───────────────────────────────────────────────────────────
function show(...ids) {
  for (const id of ids) $(id).classList.remove("hidden");
}
function hide(...ids) {
  for (const id of ids) $(id).classList.add("hidden");
}

function escapeHtml(s) {
  return String(s ?? "")
    .replace(/&/g, "&amp;")
    .replace(/</g, "&lt;")
    .replace(/>/g, "&gt;")
    .replace(/"/g, "&quot;")
    .replace(/'/g, "&#39;");
}

let noticeTimer = null;
function showNotice(message) {
  const el = $("notice");
  el.textContent = message;
  el.classList.remove("hidden");
  clearTimeout(noticeTimer);
  noticeTimer = setTimeout(clearNotice, 8000);
}
function clearNotice() {
  $("notice").classList.add("hidden");
}

let toastTimer = null;
function toast(message) {
  const el = $("toast");
  el.textContent = message;
  el.classList.add("is-visible");
  clearTimeout(toastTimer);
  toastTimer = setTimeout(() => el.classList.remove("is-visible"), 2600);
}

// ── Dates ─────────────────────────────────────────────────────────────
function parseDay(s) {
  const m = /^(\d{4})-(\d{2})-(\d{2})/.exec(s || "");
  return m ? new Date(+m[1], +m[2] - 1, +m[3]) : null;
}
function startOfToday() {
  const d = new Date();
  return new Date(d.getFullYear(), d.getMonth(), d.getDate());
}
function dayDiff(day) {
  return Math.round((day - startOfToday()) / 86400000);
}
function relativeLabel(diff) {
  if (diff === 0) return "aujourd’hui";
  if (diff === 1) return "demain";
  if (diff === -1) return "hier";
  return diff < 0 ? `il y a ${-diff} j` : `dans ${diff} j`;
}
function plural(n, one, many) {
  return n > 1 ? many : one;
}

// ── API calls ─────────────────────────────────────────────────────────
async function api(path, opts = {}) {
  const authed = Boolean(token);
  const headers = { ...(opts.headers || {}) };
  if (opts.body) headers["Content-Type"] = "application/json";
  if (token) headers.Authorization = `Bearer ${token}`;

  const res = await fetch(API + path, { ...opts, headers });

  if (res.status === 401 && authed) {
    // Jeton expiré ou révoqué : on repart de l'écran de connexion.
    signOut();
    throw new Error("Session expirée, reconnecte-toi.");
  }
  if (!res.ok) {
    const err = await res.json().catch(() => ({ detail: res.statusText }));
    throw new Error(err.detail || res.statusText);
  }
  return res.json();
}

/** Oublie la session locale et rend la main à l'écran de connexion. */
function signOut() {
  token = null;
  document.body.classList.add("is-guest");
  sessionId = null;
  items = [];
  sessionStorage.removeItem("pronote_token");
  $("qrStatus").textContent = "";
  $("pinStatus").textContent = "";
  chatMessages.innerHTML = "";
  hide("homework", "empty", "loading", "chat");
  show("authQr");
  $("summaryTitle").textContent = "Bonjour.";
  $("summaryMeta").textContent = "Connecte ton compte pour voir tes devoirs.";
  $("profileName").textContent = "Mon compte";
  $("profileMeta").textContent = "PRONOTE";
  $("syncLabel").textContent = "Pas encore synchronisé";
  updateCounts({ late: 0, soon: 0, done: 0, all: 0 });
}

/** Révoque la session côté serveur puis nettoie le client. */
async function logout() {
  try {
    await api("/api/auth/logout", { method: "POST" });
  } catch {
    /* jeton déjà mort : le nettoyage local suffit */
  }
  signOut();
}

function fetchItems() {
  return api("/api/items");
}

// ── Render cards ──────────────────────────────────────────────────────
const KIND_LABEL = { devoir: "DM", controle: "DS", cours: "CRS", message: "MSG" };

function itemCard(item) {
  const day = parseDay(item.due);
  const diff = day ? dayDiff(day) : null;
  const rel = day ? relativeLabel(diff) : "sans date";
  const abs = day
    ? day.toLocaleDateString("fr-FR", { weekday: "short", day: "numeric", month: "short" })
    : "";
  const late = !item.done && diff !== null && diff < 0;
  const id = escapeHtml(item.id);
  const subject = escapeHtml(item.subject);
  const title = escapeHtml(item.title);
  const badge = KIND_LABEL[item.kind] || "DOC";

  return `
    <article class="document-card${item.done ? " is-done" : ""}" data-id="${id}">
      <div class="doc-top">
        <span class="file-icon ${late ? "pdf" : "doc"}">${badge}</span>
        <span class="done-mark${item.done ? " active" : ""}" title="${item.done ? "Fait" : "À faire"}"
              aria-label="${item.done ? "Fait" : "À faire"}">${item.done ? "★" : "☆"}</span>
      </div>
      <h3>${title}</h3>
      <p>${subject} <i>·</i> ${late ? `<b class="late">${rel}</b>` : rel}</p>
      <div class="doc-footer">
        <span>${abs || "—"}</span>
        <button class="open-doc" data-done="${id}">${item.done ? "↺ Refaire" : "✓ Marquer fait"}</button>
      </div>
    </article>
  `;
}

function byDue(a, b) {
  const da = parseDay(a.due), db = parseDay(b.due);
  if (da && db) return da - db;
  if (da) return -1;
  if (db) return 1;
  return 0;
}

function updateCounts({ late, soon, done, all }) {
  $("navAll").textContent = all;
  $("navLate").textContent = late;
  $("navSoon").textContent = soon;
  $("navDone").textContent = done;
  $("orbitCount").textContent = `${all} ${plural(all, "devoir", "devoirs")}`;
}

function render() {
  const today = startOfToday();
  const late = [], soon = [], done = [];
  for (const it of items) {
    const day = parseDay(it.due);
    if (it.done) done.push(it);
    else if (day && day < today) late.push(it);
    else soon.push(it);
  }
  late.sort(byDue);
  soon.sort(byDue);

  updateCounts({ late: late.length, soon: soon.length, done: done.length, all: items.length });

  const lateWord = plural(late.length, "devoir", "devoirs");
  $("summaryTitle").textContent = late.length
    ? `${late.length} ${lateWord} en retard`
    : soon.length ? "Plus rien en retard" : "Tout est fait.";
  const meta = [];
  if (soon.length) meta.push(`${soon.length} à venir`);
  if (done.length) meta.push(`${done.length} ${plural(done.length, "fait", "faits")}`);
  $("summaryMeta").textContent = meta.join(" · ") || "Rien à signaler.";
  $("profileMeta").textContent = `${items.length} ${plural(items.length, "devoir", "devoirs")} suivis`;

  const fill = (name, list) => {
    $(name + "Section").classList.toggle("hidden", list.length === 0 || (filter !== "all" && filter !== name));
    $(name + "Count").textContent = list.length ? String(list.length) : "";
    $(name + "List").innerHTML = list.map(itemCard).join("");
  };
  fill("late", late);
  fill("soon", soon);
  fill("done", done);

  if (items.length === 0) {
    hide("homework");
    $("empty").textContent = "Rien à afficher pour l'instant. Touche ↻ pour synchroniser.";
    show("empty");
  } else {
    hide("empty");
    show("homework");
  }
}

// ── Load items ────────────────────────────────────────────────────────
async function loadItems({ silent = false } = {}) {
  if (!token) return;
  if (!silent) {
    show("loading");
    hide("homework", "empty");
  }
  try {
    items = await fetchItems();
    clearNotice();
    hide("loading");
    render();
    show("chat");
    $("syncLabel").textContent = "Synchronisé à l'instant";
  } catch (e) {
    hide("loading");
    if (items.length) render();
    show("chat");
    if (token) {
      showNotice(`Impossible de charger les devoirs (${e.message}). Touche ↻ pour réessayer.`);
    }
  }
}

// ── Mark done ─────────────────────────────────────────────────────────
$("homework").addEventListener("click", async (e) => {
  const btn = e.target.closest("[data-done]");
  if (!btn) return;
  const id = btn.dataset.done;
  const item = items.find((i) => String(i.id) === id);
  if (!item) return;

  const previous = item.done;
  item.done = !previous;
  btn.closest(".document-card").classList.toggle("is-done", item.done);
  const pause = new Promise((r) => setTimeout(r, REDUCED ? 0 : 320));

  try {
    await Promise.all([
      api("/api/items/done", { method: "POST", body: JSON.stringify({ item_id: id }) }),
      pause,
    ]);
  } catch (err) {
    item.done = previous;
    render();
    showNotice(`Impossible d’enregistrer ce devoir (${err.message}).`);
    return;
  }
  toast(item.done ? "Marqué comme fait" : "Remis à faire");
  try { items = await fetchItems(); } catch { /* garde l'état local */ }
  render();
});

// ── Filtres de la sidebar ─────────────────────────────────────────────
document.querySelectorAll("[data-filter]").forEach((link) => {
  link.addEventListener("click", (e) => {
    filter = link.dataset.filter;
    document.querySelectorAll(".nav-item").forEach((n) => n.classList.toggle("active", n === link));
    if (filter === "all") {
      show("homework");
      render();
    } else {
      render();
      show("homework");
    }
  });
});

// ── Auth : QR ─────────────────────────────────────────────────────────
$("qrBtn").addEventListener("click", () => $("qrFile").click());

// Message adapté : sur ordinateur, coller est plus simple que choisir un fichier.
(function adaptQrHint() {
  const hint = $("qrHint");
  if (!hint) return;
  const ua = navigator.userAgent;
  const mobile = /Android|iPhone|iPad|iPod/i.test(ua);
  if (!mobile) {
    hint.innerHTML = '<strong>Sur ordinateur :</strong> fais la capture du QR puis ' +
      '<strong>colle-la ici</strong> (Ctrl+V) — ou choisis-la ci-dessous.';
  }
})();

/** Envoie une capture (fichier, collage ou glisser-déposer) au serveur. */
async function sendQrFile(file) {
  const status = $("qrStatus");
  status.classList.remove("is-error");
  status.textContent = "Lecture du QR code…";

  const preview = $("qrPreview");
  if (preview) {
    preview.src = URL.createObjectURL(file);
    preview.hidden = false;
  }

  const base64 = await new Promise((resolve, reject) => {
    const reader = new FileReader();
    reader.onload = () => resolve(String(reader.result).split(",")[1]);
    reader.onerror = () => reject(new Error("lecture du fichier impossible"));
    reader.readAsDataURL(file);
  });

  try {
    const data = await api("/api/auth/qr", {
      method: "POST",
      body: JSON.stringify({ image_base64: base64 }),
    });
    sessionId = data.session_id;
    hide("authQr");
    show("authPin");
    status.textContent = "";
    $("pinStatus").textContent = "QR lu ✅ Entre le code à 4 chiffres.";
    $("pinInput").focus();
  } catch (err) {
    status.classList.add("is-error");
    status.textContent = `QR code illisible (${err.message}). Réessaie avec une capture plus nette ou plus grande.`;
    if (preview) preview.hidden = true;
  }
}

$("qrFile").addEventListener("change", (e) => {
  const file = e.target.files[0];
  e.target.value = "";
  if (file) sendQrFile(file);
});

// Coller une capture : sur téléphone, c'est un raccourci (copier l'image).
document.addEventListener("paste", (e) => {
  if (!token && $("authQr").classList.contains("hidden")) return;
  const items = [...(e.clipboardData?.items || [])];
  const image = items.find((i) => i.type.startsWith("image/"));
  if (!image) return;
  const file = image.getAsFile();
  if (file) {
    e.preventDefault();
    sendQrFile(file);
  }
});

// Glisser-déposer sur le panneau de connexion.
const authPanel = $("authQr");
["dragover", "dragenter"].forEach((ev) =>
  authPanel.addEventListener(ev, (e) => { e.preventDefault(); authPanel.classList.add("is-dragover"); })
);
["dragleave", "drop"].forEach((ev) =>
  authPanel.addEventListener(ev, () => authPanel.classList.remove("is-dragover"))
);
authPanel.addEventListener("drop", (e) => {
  e.preventDefault();
  const file = [...(e.dataTransfer?.files || [])].find((f) => f.type.startsWith("image/"));
  if (file) sendQrFile(file);
});

// ── Auth : PIN ────────────────────────────────────────────────────────
$("pinInput").addEventListener("input", (e) => {
  e.target.value = e.target.value.replace(/\D/g, "").slice(0, 4);
});

$("pinForm").addEventListener("submit", async (e) => {
  e.preventDefault();
  const pin = $("pinInput").value.trim();
  const status = $("pinStatus");
  if (!/^\d{4}$/.test(pin)) {
    status.textContent = "Entre les 4 chiffres du code.";
    return;
  }
  status.textContent = "";
  $("pinBtn").disabled = true;
  try {
    const data = await api("/api/auth/pin", {
      method: "POST",
      body: JSON.stringify({ session_id: sessionId, pin }),
    });
    token = data.token;
    sessionStorage.setItem("pronote_token", token);
    document.body.classList.remove("is-guest");
    hide("authPin");
    if (Array.isArray(data.subjects) && data.subjects.length) {
      $("profileName").textContent = data.subjects.length + " matières";
    }
    await loadItems();
  } catch (err) {
    status.textContent = `Code refusé (${err.message}).`;
  } finally {
    $("pinBtn").disabled = false;
  }
});

// ── Chat ──────────────────────────────────────────────────────────────
function addMessage(kind, text) {
  const el = document.createElement("div");
  el.className = `msg msg-${kind}`;
  if (text != null) el.textContent = text;
  chatMessages.appendChild(el);
  el.scrollIntoView({ block: "end", behavior: REDUCED ? "auto" : "smooth" });
  return el;
}

$("chatForm").addEventListener("submit", async (e) => {
  e.preventDefault();
  const input = $("chatInput");
  const question = input.value.trim();
  if (!question || !token) return;
  input.value = "";

  addMessage("user", question);
  const pending = addMessage("answer", "…");

  try {
    const data = await api("/api/chat", {
      method: "POST",
      body: JSON.stringify({ question }),
    });
    pending.textContent = data.answer;
  } catch (err) {
    pending.classList.add("msg-error");
    pending.textContent = token
      ? `Pas de réponse (${err.message}). Reformule ta question ou réessaie.`
      : "Session expirée, reconnecte-toi.";
  }
  pending.scrollIntoView({ block: "end", behavior: REDUCED ? "auto" : "smooth" });
});

// ── Actualisation ─────────────────────────────────────────────────────
async function doRefresh(btn) {
  if (!token || btn.disabled) return;
  btn.disabled = true;
  btn.classList.add("is-busy");
  try {
    await api("/api/items/refresh", { method: "POST" });
    await loadItems({ silent: true });
    toast("Devoirs à jour");
  } catch (err) {
    if (token) showNotice(`Actualisation impossible (${err.message}).`);
  } finally {
    btn.disabled = false;
    btn.classList.remove("is-busy");
  }
}

refreshBtn.addEventListener("click", () => doRefresh(refreshBtn));
topRefresh.addEventListener("click", () => doRefresh(topRefresh));
$("logoutBtn").addEventListener("click", () => logout());

// ── Menu mobile (tiroir) ──────────────────────────────────────────────
const sidebar = document.querySelector(".sidebar");
const navOverlay = $("navOverlay");

function setMenu(open) {
  sidebar.classList.toggle("open", open);
  navOverlay.hidden = !open;
  $("menuBtn").setAttribute("aria-expanded", String(open));
}

$("menuBtn").addEventListener("click", () => setMenu(!sidebar.classList.contains("open")));
navOverlay.addEventListener("click", () => setMenu(false));
window.addEventListener("keydown", (e) => { if (e.key === "Escape") setMenu(false); });
sidebar.addEventListener("click", (e) => { if (e.target.closest(".nav-item")) setMenu(false); });

// ── Init ──────────────────────────────────────────────────────────────
async function init() {
  document.body.classList.toggle("is-guest", !token);
  if (token) {
    await loadItems();
    return;
  }
  // Pas de jeton en sessionStorage : le cookie httpOnly peut quand même
  // authentifier (autre onglet, navigateur relancé, après 90 jours de session).
  try {
    items = await fetchItems();
    render();
    show("homework", "chat");
    document.body.classList.remove("is-guest");
  } catch {
    show("authQr");
  }
}

init();

// Service worker
if ("serviceWorker" in navigator) {
  navigator.serviceWorker.register("/sw.js").catch(() => {});
}
