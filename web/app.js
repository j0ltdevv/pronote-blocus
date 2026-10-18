// Pronote Blocus PWA — frontend logic

const API = "";  // same origin
// Le serveur ne rend qu'un jeton opaque : l'identité de l'élève n'existe que
// côté serveur, attachée à ce jeton. sessionStorage : le jeton ne persiste que
// pour la durée de l'onglet, jamais sur le disque du navigateur.
let token = sessionStorage.getItem("pronote_token") || null;
sessionStorage.removeItem("pronote_user_id");  // clé de la version sans jeton
let sessionId = null;
let items = [];

const REDUCED = window.matchMedia("(prefers-reduced-motion: reduce)").matches;

// ── DOM refs ──────────────────────────────────────────────────────────
const $ = (id) => document.getElementById(id);
const chatMessages = $("chatMessages");
const refreshBtn = $("refreshBtn");

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

/** Oublie la session locale (jeton + données affichées) et rend la main à l'écran de connexion. */
function signOut() {
  token = null;
  sessionId = null;
  items = [];
  sessionStorage.removeItem("pronote_token");
  $("qrStatus").textContent = "";
  $("pinStatus").textContent = "";
  chatMessages.innerHTML = "";
  hide("homework", "empty", "loading", "chatFooter", "chatMessages", "authPin");
  show("authQr");
}

/** Révoque la session côté serveur (compte supprimé) puis nettoie le client. */
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

// ── Render items ──────────────────────────────────────────────────────
function itemRow(item) {
  const day = parseDay(item.due);
  const diff = day ? dayDiff(day) : null;
  const rel = day ? relativeLabel(diff) : "sans date";
  const abs = day
    ? day.toLocaleDateString("fr-FR", { weekday: "short", day: "numeric", month: "short" })
    : "";
  const hot = !item.done && ((diff !== null && diff < 0) || item.urgent);
  const id = escapeHtml(item.id);
  const subject = escapeHtml(item.subject);
  const title = escapeHtml(item.title);

  return `
    <li class="row${item.done ? " is-done" : ""}" data-id="${id}">
      <div class="when${hot ? " is-hot" : ""}">
        <span class="when-rel">${rel}</span>
        ${abs ? `<span class="when-abs">${abs}</span>` : ""}
      </div>
      <div class="what">
        <p class="subject">${subject}</p>
        <p class="task"><span class="task-text">${title}</span></p>
      </div>
      <button class="check" type="button" data-done="${id}" aria-pressed="${item.done ? "true" : "false"}"
              aria-label="Fait : ${subject}, ${title}">
        <svg viewBox="0 0 24 24" aria-hidden="true"><path pathLength="1" d="M5.5 12.5l4.2 4.2L18.5 7.5" /></svg>
      </button>
    </li>
  `;
}

function byDue(a, b) {
  const da = parseDay(a.due), db = parseDay(b.due);
  if (da && db) return da - db;
  if (da) return -1;
  if (db) return 1;
  return 0;
}

function render() {
  if (items.length === 0) {
    hide("homework");
    show("empty");
    return;
  }
  hide("empty");

  const today = startOfToday();
  const overdue = [], upcoming = [], done = [];
  for (const it of items) {
    const day = parseDay(it.due);
    if (it.done) done.push(it);
    else if (day && day < today) overdue.push(it);
    else upcoming.push(it);
  }
  overdue.sort(byDue);
  upcoming.sort(byDue);

  // Summary
  const late = overdue.length;
  $("summaryTitle").textContent = late > 0
    ? `${late} ${plural(late, "devoir", "devoirs")} en retard`
    : upcoming.length > 0 ? "Plus rien en retard" : "Tout est fait";
  const meta = [];
  if (upcoming.length) meta.push(`${upcoming.length} à venir`);
  if (done.length) meta.push(`${done.length} ${plural(done.length, "fait", "faits")}`);
  $("summaryMeta").textContent = meta.join(", ");

  // Groups
  const fill = (name, list) => {
    const section = $(name + "Section");
    section.classList.toggle("hidden", list.length === 0);
    $(name + "Count").textContent = list.length ? String(list.length) : "";
    $(name + "List").innerHTML = list.map(itemRow).join("");
  };
  fill("overdue", overdue);
  fill("upcoming", upcoming);
  fill("done", done);

  show("homework");
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
    show("chatFooter", "chatMessages");
  } catch (e) {
    hide("loading");
    if (items.length) render();
    show("chatFooter", "chatMessages");
    // Un 401 a déjà basculé l'écran de connexion : inutile d'expliquer l'échec.
    if (token) {
      showNotice(`Impossible de charger les devoirs (${e.message}). Touche Actualiser pour réessayer.`);
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

  // Animate in place, then let the row move to its new group
  btn.closest(".row").classList.toggle("is-done", item.done);
  btn.setAttribute("aria-pressed", String(item.done));
  const pause = new Promise((r) => setTimeout(r, REDUCED ? 0 : 450));

  try {
    await Promise.all([
      api("/api/items/done", {
        method: "POST",
        body: JSON.stringify({ item_id: id }),
      }),
      pause,
    ]);
  } catch (err) {
    item.done = previous;
    render();
    showNotice(`Impossible d’enregistrer ce devoir (${err.message}).`);
    return;
  }

  try { items = await fetchItems(); } catch { /* garde l'état local */ }
  render();
});

// ── Auth: QR upload ───────────────────────────────────────────────────
$("qrFile").addEventListener("change", (e) => {
  const file = e.target.files[0];
  e.target.value = "";
  if (!file) return;
  const status = $("qrStatus");
  status.classList.remove("is-error");
  status.textContent = "Lecture du QR code…";
  const reader = new FileReader();
  reader.onload = async () => {
    try {
      const base64 = reader.result.split(",")[1];
      const data = await api("/api/auth/qr", {
        method: "POST",
        body: JSON.stringify({ image_base64: base64 }),
      });
      sessionId = data.session_id;
      hide("authQr");
      show("authPin");
      status.textContent = "";
      $("pinInput").focus();
    } catch (err) {
      status.classList.add("is-error");
      status.textContent = `QR code illisible (${err.message}). Essaie avec une capture plus nette.`;
    }
  };
  reader.readAsDataURL(file);
});

// ── Auth: PIN ─────────────────────────────────────────────────────────
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
    hide("authPin");
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
  const pending = addMessage("answer");
  pending.innerHTML = '<span class="dots" role="status" aria-label="Réponse en cours"><i></i><i></i><i></i></span>';

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

// ── Refresh button ────────────────────────────────────────────────────
refreshBtn.addEventListener("click", async () => {
  if (!token || refreshBtn.disabled) return;
  refreshBtn.disabled = true;
  refreshBtn.classList.add("is-busy");
  try {
    await api("/api/items/refresh", { method: "POST" });
    await loadItems({ silent: true });
  } catch (err) {
    if (token) showNotice(`Actualisation impossible (${err.message}).`);
  } finally {
    refreshBtn.disabled = false;
    refreshBtn.classList.remove("is-busy");
  }
});

// ── Init ──────────────────────────────────────────────────────────────
async function init() {
  if (token) {
    await loadItems();
  } else {
    show("authQr");
  }
}

init();

// Register service worker
if ("serviceWorker" in navigator) {
  navigator.serviceWorker.register("/sw.js").catch(() => {});
}
