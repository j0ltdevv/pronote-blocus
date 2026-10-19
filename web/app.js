// Pronote Blocus PWA — logique frontend
//
// Design : coquille « espace élève » (sidebar + cartes). La logique est
// inchangée : jeton opaque en sessionStorage, API same-origin, chat libre.

const API = "";  // same origin
let token = sessionStorage.getItem("pronote_token") || null;
sessionStorage.removeItem("pronote_user_id");  // clé de la version sans jeton
/** Jeton CSRF de la session courante, joint à chaque écriture. */
let csrfToken = null;
let csrfSerial = 0;  // ignore une réponse de /csrf devenue obsolète
let sessionId = null;
let items = [];
let filter = "all";
let search = "";
let subject = "";

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
function fold(s) {
  return String(s || "")
    .normalize("NFD")
    .replace(/[\u0300-\u036f]/g, "")
    .toLowerCase()
    .trim();
}

function plural(n, one, many) {
  return n > 1 ? many : one;
}

// ── API calls ─────────────────────────────────────────────────────────
/** (Re)demande le jeton CSRF : lié à la session si le cookie existe déjà. */
async function refreshCsrf() {
  const serial = ++csrfSerial;
  const headers = token ? { Authorization: `Bearer ${token}` } : {};
  const res = await fetch(API + "/api/auth/csrf", { headers });
  if (!res.ok) return null;
  const data = await res.json();
  const value = data.csrf_token || null;
  if (serial === csrfSerial) csrfToken = value;
  return value;
}

/** Adopte le jeton rendu par le login et invalide les rafraîchissements en vol. */
function setCsrf(value) {
  csrfSerial++;
  csrfToken = value || null;
}

async function api(path, opts = {}, retried = false) {
  const authed = Boolean(token);
  const method = (opts.method || "GET").toUpperCase();
  const unsafe = !["GET", "HEAD", "OPTIONS"].includes(method);
  const headers = { ...(opts.headers || {}) };
  if (opts.body) headers["Content-Type"] = "application/json";
  if (token) headers.Authorization = `Bearer ${token}`;
  if (unsafe && csrfToken) headers["X-CSRF-Token"] = csrfToken;

  const res = await fetch(API + path, { ...opts, headers });

  if (res.status === 403 && unsafe && !retried) {
    // Jeton CSRF périmé ou absent (session rechargée) : on le renouvelle
    // une fois, puis on rejoue la requête avec le même corps.
    const err = await res.json().catch(() => ({}));
    if (String(err.detail || "").includes("CSRF") && (await refreshCsrf())) {
      return api(path, opts, true);
    }
    throw new Error(err.detail || res.statusText);
  }
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
  csrfToken = null;
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
  // Le cookie CSRF vient d'être effacé côté serveur : on reprend un jeton
  // de pré-connexion pour le prochain formulaire.
  refreshCsrf().catch(() => {});
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
  const set = (id, v) => { const el = $(id); if (el) el.textContent = v; };
  set("mAll", all); set("mLate", late); set("mSoon", soon); set("mDone", done);
}

function render() {
  const today = startOfToday();
  const late = [], soon = [], done = [];
  const needle = fold(search);
  for (const it of items) {
    if (subject && it.subject !== subject) continue;
    if (needle && !fold(`${it.title} ${it.subject}`).includes(needle)) continue;
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

  const select = $("subjectSelect");
  if (select) {
    const subjects = [...new Set(items.map((it) => it.subject).filter(Boolean))].sort((a, b) => a.localeCompare(b, "fr"));
    const signature = subjects.join("|");
    if (select.dataset.signature !== signature) {
      select.dataset.signature = signature;
      select.innerHTML = `<option value="">Toutes les matières</option>` +
        subjects.map((s) => `<option value="${escapeHtml(s)}">${escapeHtml(s)}</option>`).join("");
      select.value = subjects.includes(subject) ? subject : "";
      if (!subjects.includes(subject)) subject = "";
    }
  }

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
  if (!$("planView").classList.contains("hidden")) renderPlan();
}

// ── Rattrapage : plan sur 5 jours ─────────────────────────────────────
// Un devoir non fait = une séance. On répartit les retards et les
// échéances proches sur les 5 prochains jours, ~45 min par jour.
const PLAN_DAYS = 5;
const PLAN_MINUTES = 45;

function estimateMinutes(it) {
  const base = it.kind === "pdf" ? 30 : it.kind === "ppt" ? 25 : 20;
  return String(it.title || "").length > 60 ? base + 10 : base;
}

function planStamp() {
  const d = new Date();
  return `${d.getFullYear()}-${d.getMonth() + 1}-${d.getDate()}`;
}

function loadPlan() {
  try {
    const raw = JSON.parse(localStorage.getItem("pronote_plan") || "null");
    return raw && raw.stamp === planStamp() ? raw.map : null;
  } catch {
    return null;
  }
}

function savePlan(map) {
  try {
    localStorage.setItem("pronote_plan", JSON.stringify({ stamp: planStamp(), map }));
  } catch { /* mode privé : plan recalculé à chaque visite */ }
}

function buildPlan() {
  const today = startOfToday();
  const horizon = new Date(today);
  horizon.setDate(horizon.getDate() + PLAN_DAYS);
  const todo = items
    .filter((it) => {
      if (it.done) return false;
      const day = parseDay(it.due);
      return !day || day <= horizon;
    })
    .sort(byDue);
  const map = {};
  let day = 0;
  let budget = 0;
  for (const it of todo) {
    const min = estimateMinutes(it);
    if (day < PLAN_DAYS - 1 && budget > 0 && budget + min > PLAN_MINUTES) {
      day += 1;
      budget = 0;
    }
    map[String(it.id)] = day;
    budget += min;
  }
  return map;
}

function dayLabel(offset) {
  if (offset === 0) return "Aujourd'hui";
  if (offset === 1) return "Demain";
  const d = startOfToday();
  d.setDate(d.getDate() + offset);
  return d.toLocaleDateString("fr-FR", { weekday: "long", day: "numeric", month: "short" });
}

function planRow(it) {
  const id = String(it.id);
  return `
    <article class="plan-row${it.done ? " is-done" : ""}" data-id="${escapeHtml(id)}">
      <button class="plan-check${it.done ? " active" : ""}" data-done="${escapeHtml(id)}"
              aria-label="${it.done ? "Remettre à faire" : "Marquer comme fait"}">${it.done ? "✓" : ""}</button>
      <div class="plan-body">
        <h3>${escapeHtml(it.title || "Devoir")}</h3>
        <p>${escapeHtml(it.subject || "—")} · ${estimateMinutes(it)} min</p>
      </div>
    </article>`;
}

function renderPlan() {
  const host = $("planDays");
  if (!host) return;
  let map = loadPlan();
  if (!map) {
    map = buildPlan();
    savePlan(map);
  }
  const planned = items.filter((it) => map[String(it.id)] !== undefined);
  const remaining = planned.filter((it) => !it.done);
  const totalMin = remaining.reduce((sum, it) => sum + estimateMinutes(it), 0);
  const usedDays = new Set(remaining.map((it) => map[String(it.id)])).size;

  $("navPlan").textContent = String(remaining.length);
  $("planSummary").textContent = remaining.length
    ? `${remaining.length} ${plural(remaining.length, "devoir", "devoirs")} · ~${totalMin} min · ${usedDays} ${plural(usedDays, "jour", "jours")}`
    : "Rien à rattraper. Ton plan est vide.";

  const done = planned.length - remaining.length;
  const pct = planned.length ? Math.round((done / planned.length) * 100) : 0;
  const bar = $("planBar");
  if (bar) bar.style.width = `${pct}%`;
  const progress = host.parentElement?.querySelector(".plan-progress");
  if (progress) progress.setAttribute("aria-valuenow", String(pct));

  if (!remaining.length) {
    host.innerHTML = `<p class="empty">Tout est fait. Rien à étaler.</p>`;
    return;
  }
  const blocks = [];
  for (let d = 0; d < PLAN_DAYS; d += 1) {
    const list = remaining.filter((it) => map[String(it.id)] === d);
    if (!list.length) continue;
    const min = list.reduce((sum, it) => sum + estimateMinutes(it), 0);
    blocks.push(`
      <section class="plan-day">
        <header><h3>${escapeHtml(dayLabel(d))}</h3><span>${list.length} · ${min} min</span></header>
        ${list.map(planRow).join("")}
      </section>`);
  }
  host.innerHTML = blocks.join("");
}

function showPlan() {
  hide("homework", "empty", "authQr", "authPin", "chat");
  show("planView");
  document.querySelectorAll(".nav-item").forEach((n) => n.classList.toggle("active", n.id === "planNav"));
  renderPlan();
}

$("planNav").addEventListener("click", (e) => {
  e.preventDefault();
  showPlan();
});

$("replanBtn").addEventListener("click", () => {
  const map = buildPlan();
  savePlan(map);
  renderPlan();
  toast("Plan recalculé");
});

$("planView").addEventListener("click", async (e) => {
  const btn = e.target.closest("[data-done]");
  if (!btn) return;
  await toggleDone(btn.dataset.done, btn.closest(".plan-row"));
  renderPlan();
});

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
    $("mobileFilters").classList.remove("hidden");
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
async function toggleDone(id, card) {
  const item = items.find((i) => String(i.id) === id);
  if (!item) return;

  const previous = item.done;
  item.done = !previous;
  card?.classList.toggle("is-done", item.done);
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
}

$("homework").addEventListener("click", async (e) => {
  const btn = e.target.closest("[data-done]");
  if (!btn) return;
  await toggleDone(btn.dataset.done, btn.closest(".document-card"));
});

// ── Filtres (sidebar + mobile) ─────────────────────────────────────
function setFilter(next) {
  filter = next;
  document.querySelectorAll(".nav-item[data-filter]").forEach((n) => n.classList.toggle("active", n.dataset.filter === next));
  document.querySelectorAll(".mf[data-filter]").forEach((n) => n.classList.toggle("active", n.dataset.filter === next));
  hide("planView"); show("homework"); render();
}
document.querySelectorAll("[data-filter]").forEach((link) => {
  link.addEventListener("click", (e) => { e.preventDefault(); setFilter(link.dataset.filter); });
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
  $("qrZone")?.classList.add("has-preview");

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
/* ── connexion sans ordinateur : identifiant + mot de passe ──────────── */

// ── tabs auth ───────────────────────────────────────────────────────
let defaultUrl = "", defaultEnt = "";
for (const btn of document.querySelectorAll(".auth-tab")) {
  btn.addEventListener("click", () => {
    for (const b of document.querySelectorAll(".auth-tab")) {
      b.classList.toggle("active", b === btn);
      b.setAttribute("aria-selected", b === btn ? "true" : "false");
    }
    const tab = btn.dataset.tab;
    $("pwdTab").classList.toggle("hidden", tab !== "pwd");
    $("qrTab").classList.toggle("hidden", tab !== "qr");
  });
}

/** Remplit le sélecteur d'ENT avec ce que pronotepy sait gérer. */
async function loadEnts() {
  try {
    const data = await api("/api/auth/options");
    const select = $("pwdEnt");
    for (const nom of data.ents || []) {
      const option = document.createElement("option");
      option.value = nom;
      option.textContent = nom.replace(/_/g, " ");
      select.append(option);
    }
    defaultUrl = data.default_url || "";
    defaultEnt = data.default_ent || "";
    if (defaultEnt) select.value = defaultEnt;
    if (defaultUrl) $("pwdUrl").value = defaultUrl;
  } catch {
    /* liste indisponible : on laisse « Aucun », le champ reste utilisable */
  }
}

$("pwdForm").addEventListener("submit", async (e) => {
  e.preventDefault();
  const status = $("pwdStatus");
  const url = ($("pwdUrl").value.trim() || defaultUrl || "").trim() || null;
  const username = $("pwdUser").value.trim();
  const password = $("pwdPass").value;
  const ent = $("pwdEnt").value || defaultEnt || null;
  if (!username || !password) {
    status.textContent = "Identifiant et mot de passe requis.";
    return;
  }
  status.textContent = "";
  $("pwdBtn").disabled = true;
  try {
    const data = await api("/api/auth/password", {
      method: "POST",
      body: JSON.stringify({ pronote_url: url, username, password, ent }),
    });
    token = data.token;
    setCsrf(data.csrf_token);
    sessionStorage.setItem("pronote_token", token);
    $("pwdPass").value = "";
    document.body.classList.remove("is-guest");
    hide("authQr", "authPin");
    if (Array.isArray(data.subjects) && data.subjects.length) {
      $("profileName").textContent = data.subjects.length + " matières";
    }
    await loadItems();
  } catch (err) {
    status.textContent = err.message;
  } finally {
    $("pwdBtn").disabled = false;
  }
});

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
    setCsrf(data.csrf_token);
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
  loadEnts();
  refreshCsrf().catch(() => {});
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

// ── Recherche et filtre par matière ───────────────────────────────────
$("searchInput").addEventListener("input", (e) => {
  search = e.target.value;
  render();
});
$("subjectSelect").addEventListener("change", (e) => {
  subject = e.target.value;
  render();
});
