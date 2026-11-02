/* lolbuilds — interface : rendu de l'état poussé par le serveur local (/ws) et actions. */
"use strict";

const $ = (sel, root = document) => root.querySelector(sel);
// API de la fenêtre de bureau (pywebview) ; absente dans le navigateur. Au niveau du module : l'alerte de tour s'en sert aussi.
const api = () => window.pywebview?.api;
let S = null;            // données statiques (noms fr, descriptions) : /api/static
let state = null;        // état courant poussé par le serveur
let view = "live";
let library = [];
let libRole = "";
let lastKey = "";
let lastToast = null;
let lastHtml = "";
let timerEnds = null;  // hors du HTML : sinon chaque event changerait le rendu
let lastTurn = null;  // id de l'action LCU pour laquelle on a déjà alerté
let turnTotal = null;  // durée de l'action en cours (ms), si le client ne la donne pas

const ROLE_LABEL = { top: "Top", jungle: "Jungle", middle: "Mid", bottom: "ADC", utility: "Support", aram: "ARAM" };
const ROLE_ORDER = ["top", "jungle", "middle", "bottom", "utility"];
const ROLE_SVG = {
  top: '<path d="M2 2h9.5L8.5 5H5v3.5l-3 3z" fill="currentColor"/><path d="M9 9h5v5H9z" fill="currentColor" opacity=".35"/>',
  jungle: '<path d="M8 1.5c.6 3.4 4.5 5 4.5 8.5A4.5 4.5 0 0 1 8 14.5 4.5 4.5 0 0 1 3.5 10C3.5 6.5 7.4 4.9 8 1.5z" fill="currentColor"/>',
  middle: '<path d="M2 11.2 11.2 2H14v2.8L4.8 14H2z" fill="currentColor"/><path d="M2 2h5L2 7zM14 14H9l5-5z" fill="currentColor" opacity=".35"/>',
  bottom: '<path d="M14 14H4.5l3-3H11V7.5l3-3z" fill="currentColor"/><path d="M2 2h5v5H2z" fill="currentColor" opacity=".35"/>',
  utility: '<path d="M8 2.2 10.6 5H5.4zM2.5 6h11l-2.2 3H9.2v5H6.8V9H4.7z" fill="currentColor"/>',
};
const SHARD_ABBR = { adaptive: "FA", attackspeed: "VA", haste: "AC", movespeed: "VD", healthscaling: "PV+", health: "PV", tenacity: "TÉN", armor: "AR", magicres: "RM" };
const FLASH = 4;

/* ------------------------------------------------------------------ formats */
const pct = (x, d = 1) => (x * 100).toFixed(d).replace(".", ",") + " %";
const int = (n) => new Intl.NumberFormat("fr-FR").format(n);
const day = (iso) => (iso ? new Date(iso + "T12:00:00").toLocaleDateString("fr-FR", { day: "numeric", month: "short" }) : "—");
const esc = (s) => String(s ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
const wrClass = (wr) => (wr >= 0.52 ? "good" : wr < 0.48 ? "bad" : "");
const roleIcon = (r) => `<svg viewBox="0 0 16 16" aria-hidden="true">${ROLE_SVG[r] || ""}</svg>`;

/* ------------------------------------------------------------------ données statiques */
const champ = (key) => S?.champions?.[String(key)];
const perk = (id) => S?.perks?.[String(id)];
const style = (id) => S?.styles?.[String(id)];
const item = (id) => S?.items?.[String(id)];
const spell = (id) => S?.spells?.[String(id)];
const asset = (kind, id) => `/assets/${kind}/${id}.${kind === "splash" ? "jpg" : "png"}`;

const img = (kind, id, cls = "icon", tip = "") =>
  `<img class="${cls}" src="${asset(kind, id)}" alt="" ${tip} onerror="this.style.visibility='hidden'">`;
const tipAttr = (kind, id) => `data-tip="${kind}" data-id="${id}"`;

function orient(spells, key) {
  const [a, b] = spells;
  if (![a, b].includes(FLASH) || !key) return [a, b];
  const other = a === FLASH ? b : a;
  return key === "D" ? [FLASH, other] : [other, FLASH];
}

/* ------------------------------------------------------------------ API */
async function post(path, body) {
  try {
    const r = await fetch(path, { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body) });
    return await r.json();
  } catch {
    toast("warn", "Le serveur local ne répond pas.");
    return { ok: false };
  }
}

function connect() {
  const ws = new WebSocket(`ws://${location.host}/ws`);
  ws.onmessage = (e) => {
    const msg = JSON.parse(e.data);
    if (msg.type === "state") {
      const prevPrefetch = state?.lastPrefetch;
      state = msg.state;
      // Premier état reçu : le toast déjà présent date d'avant le chargement, on ne le rejoue pas.
      if (lastToast === null) lastToast = state.toast?.id ?? false;
      if (state.notice === "reload") { loadStatic().then(() => { lastHtml = ""; render(); loadLibrary(); }); return; }
      render();
      checkTurn();
      if (view === "library" && prevPrefetch !== state.lastPrefetch) loadLibrary();
    }
  };
  ws.onclose = () => {
    state = state ? { ...state, connection: "offline" } : null;
    render();  // reflète l'état hors ligne (écran « serveur arrêté »)
    setTimeout(connect, 1500);
  };
}

/* ------------------------------------------------------------------ mon tour : alerte */
let lastStage = null;
let wasInChampSelect = null;  // null : premier état reçu, on n'alerte pas pour une session déjà en cours

/** Sons du champ select : début, phases de bans / picks, mon tour (+ fenêtre au premier plan). */
function checkTurn() {
  const cs = state.champSelect;
  const turn = cs?.turn;
  const id = turn ? `${turn.type}:${turn.id}` : cs?.stage === "planning" ? "planning" : null;
  const events = [];
  if (id !== lastTurn) {
    lastTurn = id;
    const ends = cs?.timer?.endsAt;
    turnTotal = ends ? Math.max(1000, ends - Date.now()) : null;
    if (turn) events.push(turn.type === "ban" ? "myBan" : "myPick");
  }
  const stage = cs?.stage || null;
  if (stage !== lastStage) {
    if (stage === "ban") events.push("banPhase");
    if (stage === "pick" && lastStage) events.push("pickPhase");
    lastStage = stage;
  }
  const first = wasInChampSelect === null;
  if (cs && wasInChampSelect === false) events.push("start");
  wasInChampSelect = Boolean(cs);
  const set = state.settings;
  if (first || !events.length || !set?.turnAlert) return;
  if (turn && events.includes(turn.type === "ban" ? "myBan" : "myPick") && set.alertFocus) {
    try { api()?.focus(); } catch { /* sans fenêtre à ramener : le son joue quand même */ }
  }
  // Un seul son à la fois : mon tour d'abord, puis la phase, puis le début du champ select.
  const sound = ["myBan", "myPick", "banPhase", "pickPhase", "start"].filter((e) => events.includes(e))
    .map((e) => set.sounds?.[e]).find((s) => s && s !== "none");
  if (sound) playSound(sound, set.volume);
}

/* ------------------------------------------------------------------ sons (générés, aucun fichier) */
const SOUND_LABEL = { none: "Aucun", cloche: "Cloche", hextech: "Hextech", gong: "Gong", bip: "Bip" };
const SOUND_EVENTS = [
  ["myBan", "Ton tour de bannir"], ["myPick", "Ton tour de choisir"],
  ["banPhase", "Début des bans"], ["pickPhase", "Début des picks"], ["start", "Début du champ select"],
];
// [fréquence Hz, départ s, durée s, forme, harmoniques]
const SOUND_PRESETS = {
  cloche: [[880, 0, 1.2, "sine", [1, 2.76, 5.4]], [1318, 0.18, 1.0, "sine", [1, 2.76]]],
  hextech: [[523, 0, 0.35, "triangle", [1, 2]], [784, 0.11, 0.35, "triangle", [1, 2]], [1047, 0.22, 0.6, "triangle", [1, 2, 3]]],
  gong: [[110, 0, 2.2, "sine", [1, 1.48, 2.1, 2.9]], [165, 0.02, 1.6, "sine", [1, 2.4]]],
  bip: [[660, 0, 0.12, "sine", [1]], [520, 0.13, 0.12, "sine", [1]]],
};

let audio;
// Les navigateurs bloquent le son tant que la page n'a reçu aucun clic : on prépare le contexte audio
// au premier geste (l'exe lance aussi WebView2 avec --autoplay-policy=no-user-gesture-required).
for (const ev of ["pointerdown", "keydown"]) {
  window.addEventListener(ev, () => {
    try { audio = audio || new AudioContext(); if (audio.state === "suspended") audio.resume(); } catch { /* sans audio */ }
  }, { once: true, capture: true });
}
function playSound(name, volume = 0.6) {
  const notes = SOUND_PRESETS[name];
  if (!notes || volume <= 0) return;
  try {
    audio = audio || new AudioContext();
    if (audio.state === "suspended") audio.resume();
    const out = audio.createGain();
    out.gain.value = 0.25 * volume;
    out.connect(audio.destination);
    for (const [freq, start, dur, type, partials] of notes) {
      partials.forEach((ratio, k) => {
        const t = audio.currentTime + start;
        const osc = audio.createOscillator();
        const gain = audio.createGain();
        osc.type = type;
        osc.frequency.value = freq * ratio;
        gain.gain.setValueAtTime(0.0001, t);
        gain.gain.exponentialRampToValueAtTime(1 / (k + 1), t + 0.015);
        gain.gain.exponentialRampToValueAtTime(0.0001, t + dur / (1 + k * 0.6));
        osc.connect(gain).connect(out);
        osc.start(t);
        osc.stop(t + dur);
      });
    }
  } catch { /* pas de son : l'alerte visuelle suffit */ }
}

/* ------------------------------------------------------------------ réglages + thèmes */
const THEMES = [
  { id: "hextech", name: "Hextech", tag: "Le client d'origine : or, cristal et hexagones", sw: ["#0a0d13", "#151b26", "#c9a45f", "#43cfc1"] },
  { id: "noxus", name: "Noxus", tag: "L'empire : acier noirci, cramoisi, capitales romaines", sw: ["#0b0708", "#1c1112", "#d2463a", "#c9ced8"] },
  { id: "piltover", name: "Piltover", tag: "La cité du progrès : ivoire, laiton, art nouveau", sw: ["#f3ece0", "#fbf7f0", "#a8711f", "#1f8fbf"] },
  { id: "ionia", name: "Ionia", tag: "Les Terres premières : crépuscule, fleurs d'esprit", sw: ["#0e0a13", "#1c1522", "#e58fb4", "#6fd3a6"] },
];

function applyTheme(theme) {
  const t = THEMES.some((x) => x.id === theme) ? theme : "hextech";
  document.documentElement.dataset.realm = t;
  try { localStorage.setItem("lolbuilds-theme", t); } catch { /* sans stockage : thème appliqué au prochain état */ }
}
try { applyTheme(localStorage.getItem("lolbuilds-theme")); } catch { applyTheme("hextech"); }

/** Réécrit innerHTML seulement si le HTML change : sinon le focus et le survol sautent à chaque push. */
const htmlCache = new WeakMap();
function setHtml(el, html) {
  if (htmlCache.get(el) === html) return;
  htmlCache.set(el, html);
  el.innerHTML = html;
}

function renderSettings() {
  const set = state?.settings;
  if (!set) return;
  const keep = (el) => el === document.activeElement;  // ne pas écraser un réglage en cours de modification
  $("#set-alert").checked = set.turnAlert;
  $("#set-focus").checked = set.alertFocus;
  $("#set-auto").checked = set.autoApply;
  $("#set-spells").checked = set.applySpells;
  $("#set-atypical").checked = set.atypical;
  if (!keep($("#set-volume"))) $("#set-volume").value = set.volume;
  $("#set-volume-val").textContent = `${Math.round(set.volume * 100)} %`;
  document.querySelectorAll("[data-setflash]").forEach((b) => b.setAttribute("aria-pressed", String(b.dataset.setflash === set.flashKey)));
  const box = $("#set-sounds");
  if (!box.childElementCount) {
    box.innerHTML = SOUND_EVENTS.map(([ev, label]) => `
      <div class="set-row"><span class="set-txt"><b>${label}</b></span>
        <span class="set-sound"><select data-sound="${ev}" aria-label="Son : ${label}">${Object.entries(SOUND_LABEL).map(([k, v]) => `<option value="${k}">${v}</option>`).join("")}</select>
        <button type="button" class="btn ghost" data-testsound="${ev}" aria-label="Écouter">▶</button></span></div>`).join("");
  }
  box.querySelectorAll("select[data-sound]").forEach((sel) => { if (!keep(sel)) sel.value = set.sounds?.[sel.dataset.sound] || "none"; });
  setHtml($("#set-themes"), THEMES.map((t) => `
    <button type="button" class="theme-card${set.theme === t.id ? " selected" : ""}" data-theme="${t.id}" aria-pressed="${set.theme === t.id}">
      <span class="theme-art${t.id === "hextech" ? " hex" : ""}" ${t.id === "hextech" ? "" : `style="background-image:url('/assets/realms/${t.id}/bg.jpg')"`}>
        ${t.id === "hextech" ? "" : `<i style="--m:url('/assets/realms/${t.id}/crest.png'); --c:${t.sw[2]}"></i>`}</span>
      <span class="theme-sw">${t.sw.map((c) => `<i style="background:${c}"></i>`).join("")}</span>
      <span class="theme-name">${t.name}</span><span class="theme-tag">${t.tag}</span></button>`).join(""));
  applyTheme(set.theme);
  renderUpdate();
  renderBuilds();
  renderDiscoSettings();
}

/** Section « Mise à jour » : version installée, dernière release GitHub, bouton d'installation. */
function renderBuilds() {
  const p = state?.prefetch;
  if (!p) return;
  const last = state.lastPrefetch;
  const running = p.status === "running";
  const sub = {
    idle: last ? `Dernière récupération : ${day(last)}` : "Aucun build en cache pour l'instant",
    running: p.total ? `Téléchargement… ${p.done} / ${p.total}` : "Préparation du téléchargement…",
    done: p.updated || p.failed
      ? `Terminé : ${p.updated} mis à jour${p.failed ? `, ${p.failed} en échec (l'ancien build est conservé)` : ""}`
      : "Tous les builds étaient déjà à jour aujourd'hui",
    error: `Impossible : ${esc(p.error || "erreur")}`,
  }[p.status] || "";
  $("#bld-title").textContent = running ? "Téléchargement en cours" : "Builds en cache";
  $("#bld-sub").innerHTML = `${sub}${last && p.status !== "idle" && !running ? ` · dernière récupération : ${day(last)}` : ""}`;
  setHtml($("#bld-actions"), `<button type="button" class="btn" data-prefetch${running ? " disabled" : ""}>Tout télécharger</button>`);
}

function renderUpdate() {
  const u = state?.update;
  if (!u) return;
  const latest = u.latest ? `version ${esc(u.latest)}` : "";
  const sub = {
    idle: `Version ${esc(u.current)}`,
    checking: `Version ${esc(u.current)} · recherche d'une nouvelle version…`,
    uptodate: `Version ${esc(u.current)} · c'est la dernière`,
    available: `Version ${esc(u.current)} installée · ${latest} disponible`,
    downloading: `Téléchargement de la ${latest} et vérification…`,
    installing: "Installation en cours : lolbuilds va se fermer puis se rouvrir tout seul.",
    error: `Version ${esc(u.current)} · ${esc(u.error || "vérification impossible")}`,
  }[u.status] || `Version ${esc(u.current)}`;
  $("#upd-title").textContent = u.status === "available" ? "Nouvelle version disponible" : "Version installée";
  $("#upd-sub").innerHTML = sub;
  const busy = ["checking", "downloading", "installing"].includes(u.status);
  const page = u.url ? `<a class="btn ghost" href="${esc(u.url)}" target="_blank" rel="noopener">Voir la release</a>` : "";
  let actions = `<button type="button" class="btn ghost" data-update="check"${busy ? " disabled" : ""}>Vérifier</button>`;
  if (u.status === "available") {
    actions = u.installable
      ? `${page}<button type="button" class="btn" data-update="install">Mettre à jour</button>`
      : u.url ? `<a class="btn" href="${esc(u.url)}" target="_blank" rel="noopener">Télécharger la ${latest}</a>` : actions;  // sans URL : pas de lien vide
  }
  setHtml($("#upd-actions"), actions);
  const notes = $("#upd-notes");
  notes.hidden = u.status !== "available" || !u.notes;
  if (!notes.hidden) notes.textContent = u.notes;
}

/* ------------------------------------------------------------------ rendu global */
function render() {
  if (!state) return;
  renderTop();
  if (state.settings) applyTheme(state.settings.theme);
  if (view === "settings") renderSettings();
  if (view === "live") renderLive();
  if (state.toast && state.toast.id !== lastToast) {
    lastToast = state.toast.id;
    toast(state.toast.kind, state.toast.text);
  }
}

function renderTop() {
  const conn = $("#conn");
  const label = $("#conn-label");
  let s = state?.connection || "offline";
  let text = { waiting: "En attente du client", connected: "Client connecté", demo: "Démo", offline: "Serveur local arrêté" }[s];
  if (s === "connected" && state.champSelect) { s = "live"; text = "Champ select en direct"; }
  if (s === "connected" && ["GameStart", "InProgress", "Reconnect"].includes(state.phase)) text = "Partie en cours";
  conn.dataset.state = s;
  label.textContent = text;
  if (!state?.settings) return;
  $("#opt-auto").checked = state.settings.autoApply;
  $("#opt-alert").checked = state.settings.turnAlert;
  for (const k of ["D", "F"]) $(`#flash-${k.toLowerCase()}`).setAttribute("aria-pressed", String(state.settings.flashKey === k));
  $("#rail-foot").innerHTML = `<b>${esc(S?.version?.split(".").slice(0, 2).join(".") || "—")}</b>patch<br><br><b>${day(state.lastPrefetch)}</b>builds`;
  const upd = state.update?.status === "available";
  $("#nav-settings").classList.toggle("has-update", upd);
  $("#nav-settings").title = upd ? `Nouvelle version ${state.update.latest} disponible` : "";
}

/* ------------------------------------------------------------------ vue champ select */
/* Une seule structure pour toutes les étapes : barre d'étape en haut ; à gauche le portrait du champion et ses
   builds ; à droite la draft, les suggestions de l'étape (bans, bons picks), le détail du build, puis les joueurs
   de la partie une fois en jeu. */
function renderLive() {
  const root = $("#view-live");
  const cur = state.current;
  const cs = state.champSelect;
  const team = state.team;
  let html;
  timerEnds = cs?.timer?.endsAt || null;
  if (state.connection === "offline" || (!cur && !cs && !team)) {
    html = emptyHtml(state.connection);
  } else {
    const b = cur?.build;
    const key = `${cur?.championId}:${b?.role}:${b?.fetched_at}:${b?.variants?.length}`;
    const enter = key !== lastKey;
    lastKey = key;
    const variants = b ? variantsOf(b) : [];
    const sel = Math.min(state.selection.variant || 0, Math.max(0, variants.length - 1));
    const noBuild = cur && !b ? `<p class="side-msg${cur.fetching ? " loading" : ""}">${cur.fetching ? "Récupération du build sur OP.GG…" : "Pas de build OP.GG pour ce champion dans ce rôle."}</p>` : "";
    let side, main;
    if (!cs && team) {
      // En jeu : le build ne change plus, il passe en résumé à gauche ; à droite, les 10 joueurs face à face.
      const v = variants[state.applied?.variant] ?? variants[sel];
      side = b ? (v ? gameBuildHtml(b, v) : "") : noBuild;
      main = lanesHtml(team);
    } else {
      side = !cur ? sideHintHtml(cs) : b ? (variants.length ? variantListHtml(variants, sel, cs, enter) : "") : noBuild;
      main = (cs ? draftHtml(cs, cur, appliedStepsHtml(cur)) + banHtml(state.banSuggestions) : "")
        + (cs && !cur?.locked ? counterHtml(state.counterPicks, cur) : "")
        + (cur && b && variants[sel] ? detailHtml(b, variants[sel], cs) : "")
        + (cur && !b ? noBuildHtml(cur) : "");
    }
    html = stageHtml(cs, team) + `<div class="live"><aside class="live-side">${portraitHtml(cur, cs)}${cs ? discoButtonHtml() : ""}${side}</aside>
      <div class="live-main">${main}</div></div>`;
  }
  // Le client envoie des events en rafale : on ne touche au DOM que si le rendu change.
  if (html.replace(" enter", "") === lastHtml.replace(" enter", "")) return;
  lastHtml = html;
  root.innerHTML = html;
  tickTimer();
}

function emptyHtml(kind) {
  const hex = `<svg class="hexwait" viewBox="0 0 80 80" aria-hidden="true">
      <g class="spin"><path d="M40 4 71 22v36L40 76 9 58V22z" fill="none" stroke="currentColor" stroke-width="1.5" stroke-dasharray="6 8"/></g>
      <path d="M40 22 56 31v18L40 58 24 49V31z" fill="currentColor" opacity=".18"/><path d="M40 30 48 34.6v9.2L40 48.5 32 43.8v-9.2z" fill="currentColor"/></svg>`;
  const facts = `<div class="facts"><div><b>${int(state.libraryCount ?? libraryCountGuess())}</b>builds en cache</div><div><b>${day(state.lastPrefetch)}</b>dernière mise à jour</div></div>`;
  const content = {
    waiting: [`En attente du client League`, `Lance League of Legends : l'application se connecte toute seule et suit ta sélection de champion.`],
    offline: [`Serveur local arrêté`, `Relance lolbuilds pour retrouver tes builds.`],
    connected: [`Prêt pour la prochaine partie`, `Dès que la sélection des champions commence, tes builds s'affichent ici. ${state.settings.autoApply ? "Le build préféré sera appliqué automatiquement." : "Clique sur un build pour l'appliquer."}`],
    demo: [`Démo`, esc(state.notice || "Préparation du faux champ select…")],
    pick: [`Choisis ton champion`, `Survole ou verrouille un champion : ses builds apparaissent ici avec leur winrate.`],
  }[kind] || [`Prêt`, ``];
  const pad = kind === "pick" ? ' style="min-height: 320px"' : "";
  return `<div class="empty"${pad}><div class="empty-inner">${hex}<h2>${content[0]}</h2><p>${content[1]}</p>
    ${kind === "pick" ? "" : facts}
    ${kind === "pick" ? "" : `<button type="button" class="btn" data-goto="library">Parcourir les builds</button>`}</div></div>`;
}

function libraryCountGuess() { return library.length || 0; }

function heroHtml(cur, cs, library = false) {
  const c = champ(cur.championId);
  const b = cur.build;
  if (!library) timerEnds = cs?.timer?.endsAt || null;  // la bibliothèque ne touche pas au minuteur du champ select
  const timer = !library && timerEnds ? `<span class="timer num js-timer"></span>` : "";
  const phase = library ? "Bibliothèque" : cs ? (cur.locked ? "Champion verrouillé" : cur.preview ? "Aperçu · bon pick" : "Champion survolé") : "Aperçu";
  const roles = (cur.roles.length ? cur.roles : [cur.role]).filter(Boolean);
  const tabs = roles.map((r) => `<button type="button" class="role-tab" data-role="${r}" aria-pressed="${r === cur.role}">
      ${roleIcon(r)}${ROLE_LABEL[r] || r}${r === cur.assigned ? '<span class="assigned" title="Rôle assigné"></span>' : ""}</button>`).join("");
  const meta = b ? `<div class="meta num">
      <span>Winrate <b class="${wrClass(b.winrate)}">${pct(b.winrate)}</b></span>
      <span><b>${int(b.sample_size)}</b> parties${b.source === "opgg" ? " · OP.GG" : ""}</span>
      <span>Patch <b>${esc(b.patch)}</b></span>
      <span>Données du <b>${day(b.fetched_at)}</b></span></div>` : "";
  const fetching = cur.fetching ? `<div class="meta"><span class="loading">Récupération du build sur OP.GG…</span></div>` : "";
  return `<header class="hero">
    <div class="hero-splash" style="background-image:url('${asset("splash", cur.championId)}')"></div>
    <div class="hero-top"><span class="eyebrow">${phase}</span>${timer}</div>
    <div><h1 class="hero-name">${esc(c?.name || cur.champion || "Champion")}</h1>
    <p class="hero-title">${esc(c?.title || "")}</p></div>
    <div class="hero-row"><div class="roles">${tabs}</div>${fetching || meta}</div>
  </header>`;
}

function draftHtml(cs, cur, extra = "") {
  const cp = state.counterPicks;
  const opp = cur?.opponent || (cp ? { championId: cp.enemyId } : null);
  const slot = (p, enemy) => {
    const id = p.championId || p.intent;
    const isOpp = enemy && opp && id === opp.championId;
    const cls = ["slot", p.isMe ? "me" : "", !p.championId && p.intent ? "intent" : "", isOpp ? "opp" : ""].join(" ");
    const pos = !enemy && p.position ? `<span class="pos">${roleIcon(p.position)}</span>` : "";
    const pick = enemy && id ? ` data-opponent="${id}" role="button" aria-label="Adversaire : ${esc(champ(id)?.name || "")}"` : "";
    return `<div class="${cls}" ${id ? tipAttr("champion", id) : ""}${pick}>${id ? img("champion", id, "") : ""}${pos}</div>`;
  };
  const sorted = [...cs.allies].sort((a, b) => ROLE_ORDER.indexOf(a.position) - ROLE_ORDER.indexOf(b.position));
  const bans = cs.bans.length ? `<div class="bans"><span>Bans</span>${cs.bans.map((b) => `<div class="slot" ${tipAttr("champion", b.championId)}>${img("champion", b.championId, "")}</div>`).join("")}</div>` : "<div></div>";
  return `<div class="draft"><div class="team">${sorted.map((p) => slot(p, false)).join("")}</div>
    <span class="vs">VS</span><div class="team enemy">${cs.enemies.map((p) => slot(p, true)).join("")}</div>${bans}${extra}</div>
    ${cur ? matchupHtml(cs, opp) : ""}`;
}

/** Barre d'étape, toujours en haut : ce qui se passe, le fil des étapes et le compte à rebours. */
function stageHtml(cs, team) {
  const STEPS = ["Prépick", "Bans", "Picks", "Finalisation", "En jeu"];
  let step, text, cls = "";
  if (!cs) {
    step = 4; text = team ? "Partie en cours" : "Partie terminée"; cls = "game";
  } else if (cs.turn) {
    step = cs.turn.type === "ban" ? 1 : 2;
    text = cs.turn.type === "ban" ? "À toi de bannir"
      : cs.mode === "aram" ? "ARAM · échange ton champion avec le banc si tu veux" : "À toi de choisir ton champion";
    cls = cs.turn.type;
  } else {
    const picked = [...cs.allies, ...cs.enemies].some((p) => p.championId);
    step = { planning: 0, ban: 1, pick: 2, finalization: 3 }[cs.stage] ?? (picked ? 2 : 1);
    text = { planning: "Prépick · annonce ton champion", ban: "Bans en cours", pick: "Picks en cours",
             finalization: "Finalisation · la partie va commencer" }[cs.stage] || "Sélection des champions";
    cls = cs.stage === "planning" ? "plan" : "";
  }
  const crumbs = STEPS.map((s, i) => `<span class="crumb${i === step ? " on" : i < step ? " done" : ""}">${s}</span>`)
    .join('<span class="crumb-sep" aria-hidden="true">›</span>');
  return `<div class="turn stage ${cls}" role="status"><span class="turn-fill js-turn-fill" aria-hidden="true"></span>
    ${cs?.turn ? '<span class="turn-dot"></span>' : ""}<b>${text}</b><span class="crumbs">${crumbs}</span>
    ${cs && timerEnds ? '<span class="timer num js-timer"></span>' : ""}</div>`;
}

/** Portrait du champion (splash), ou l'emblème du thème tant que rien n'est survolé. */
function portraitHtml(cur, cs) {
  if (!cur) {
    return `<header class="portrait is-empty"><div class="portrait-emblem">${`<svg class="hexwait" viewBox="0 0 80 80" aria-hidden="true">
      <g class="spin"><path d="M40 4 71 22v36L40 76 9 58V22z" fill="none" stroke="currentColor" stroke-width="1.5" stroke-dasharray="6 8"/></g>
      <path d="M40 22 56 31v18L40 58 24 49V31z" fill="currentColor" opacity=".18"/><path d="M40 30 48 34.6v9.2L40 48.5 32 43.8v-9.2z" fill="currentColor"/></svg>`}</div>
      <div class="portrait-info"><span class="eyebrow">${cs ? "Aucun champion" : "En jeu"}</span>
      <h1 class="hero-name">${cs ? "À toi de choisir" : "Partie en cours"}</h1></div></header>`;
  }
  const c = champ(cur.championId);
  const b = cur.build;
  const phase = cs ? (cur.locked ? "Verrouillé" : cur.preview ? "Aperçu · bon pick" : "Survolé") : "En jeu";
  const roles = (cur.roles.length ? cur.roles : [cur.role]).filter(Boolean);
  const tabs = roles.map((r) => `<button type="button" class="role-tab" data-role="${r}" aria-pressed="${r === cur.role}">
      ${roleIcon(r)}${ROLE_LABEL[r] || r}${r === cur.assigned ? '<span class="assigned" title="Rôle assigné"></span>' : ""}</button>`).join("");
  const meta = cur.fetching ? '<span class="loading">Récupération sur OP.GG…</span>'
    : b ? `<span>Winrate <b class="${wrClass(b.winrate)}">${pct(b.winrate)}</b></span><span><b>${int(b.sample_size)}</b> parties</span>
      <span>Patch <b>${esc(b.patch)}</b></span>` : "";
  return `<header class="portrait"><div class="portrait-splash" style="background-image:url('${asset("splash", cur.championId)}')"></div>
    <span class="portrait-chip">${phase}</span>
    <div class="portrait-info"><h1 class="hero-name">${esc(c?.name || cur.champion || "Champion")}</h1>
      <p class="hero-title">${esc(c?.title || "")}</p><div class="roles">${tabs}</div><div class="meta num">${meta}</div></div></header>`;
}

function sideHintHtml(cs) {
  if (!cs) return "";
  return `<p class="side-msg">Survole ou verrouille un champion : ses builds OP.GG s'affichent ici, triés par winrate.</p>`;
}

/** Les builds proposés, en lignes compactes sous le portrait (touches 1 à 3). */
function variantListHtml(variants, sel, cs, enter) {
  const applied = state.applied;
  const flashKey = state.settings.flashKey;
  const rows = variants.map((v, i) => {
    const ks = perk(v.perks[0]);
    const core = (v.blocks.find((x) => x.key === "core") || v.blocks.find((x) => /^Core/.test(x.type)))?.items || [];
    const boots = (v.blocks.find((x) => x.key === "boots") || v.blocks.find((x) => /^Bottes/.test(x.type)))?.items?.[0];
    const tags = [...(v.tags || []).map((t) => `<span class="tag${t === "Le plus joué" ? " alt" : t.startsWith("Contre ") ? " vs" : t.startsWith("Atypique") ? " atypical" : ""}">${esc(t)}</span>`),
      v.details?.runes?.fallback ? '<span class="tag warn">Échantillon faible</span>' : ""].join("");
    const isApplied = applied && applied.variant === i;
    const action = !cs ? "" : isApplied ? "Appliqué" : state.busy && i === sel ? "Application…" : "Appliquer";
    const items = [...core, ...(boots ? [boots] : [])].map((it) => img("item", it.id, "icon", tipAttr("item", it.id))).join("");
    const spells = orient(v.spells, flashKey).map((s) => img("spell", s, "icon", tipAttr("spell", s))).join("");
    return `<button type="button" class="vrow${i === sel ? " selected" : ""}${isApplied ? " applied" : ""}" data-variant="${i}" aria-pressed="${i === sel}">
      <span class="vrow-key">${img("perk", v.perks[0], "keystone", tipAttr("perk", v.perks[0]))}<span class="vrow-n num">${i + 1}</span></span>
      <span class="vrow-txt"><span class="vname">${esc(ks?.name || "Runes")}</span>
        <span class="vtrees">${img("perk", v.primaryStyle, "")}${esc(style(v.primaryStyle)?.name || "")}<span>·</span>${img("perk", v.subStyle, "")}${esc(style(v.subStyle)?.name || "")}</span></span>
      <span class="vwr num"><strong class="${wrClass(v.winrate)}">${pct(v.winrate)}</strong><span>${int(v.games)} parties</span></span>
      <span class="tags">${tags}</span>
      <span class="vrow-foot"><span class="vitems">${items}</span><span class="spells">${spells}</span>${action ? `<span class="vaction">${action}</span>` : ""}</span>
    </button>`;
  }).join("");
  return `<div class="side-head"><h2 class="section-title">Builds</h2><span class="section-note">touches 1 à ${variants.length}</span></div>
    <div class="vrows${enter ? " enter" : ""}">${rows}</div>`;
}

/** En jeu : le build appliqué, en résumé sous le portrait (runes, items, sorts, compétences). */
function gameBuildHtml(b, v) {
  const skills = v.details?.skills || b.details?.skills || [];
  const sp = orient(v.spells, state.settings.flashKey);
  return `<div class="panel game-build">
      <div class="gb-head"><h3>Build appliqué</h3><span class="gb-spells">${sp.map((s) => img("spell", s, "icon", tipAttr("spell", s))).join("")}</span></div>
      ${runesHtml(v).replace("<h3>Runes</h3>", "")}</div>
    <div class="panel game-build">${itemsHtml(v)}</div>
    ${skills.length ? `<div class="panel game-build">${skillsHtml(skills)}</div>` : ""}`;
}

/** En jeu : une ligne par poste, mon joueur à gauche et son adversaire en face ; ma lane est détaillée dessous. */
function lanesHtml(team) {
  const allies = team.players || [];
  const enemies = team.enemies || [];
  const pos = (p) => ROLE_OF[p?.position] || "";
  let pairs;
  if (allies.length && allies.every(pos)) {
    pairs = ROLE_ORDER.map((r) => [allies.find((p) => pos(p) === r), enemies.find((p) => pos(p) === r), r]).filter(([a, e]) => a || e);
  } else {
    pairs = Array.from({ length: Math.max(allies.length, enemies.length) }, (_, i) => [allies[i], enemies[i], pos(allies[i])]);
  }
  const rows = pairs.map(([a, e, r]) => `<div class="lane${a?.isMe ? " mine" : ""}">${laneSideHtml(a, false)}
      <span class="ln-role">${r ? roleIcon(r) + (ROLE_LABEL[r] || "") : ""}</span>${laneSideHtml(e, true)}</div>`).join("");
  const none = team.status === "unavailable" ? '<p class="cp-msg dim">Les infos de la partie ne sont pas disponibles.</p>' : "";
  if (team.status === "loading") {
    // Les profils arrivent dans le désordre : on montre la progression, puis tous les joueurs d'un coup.
    const total = team.total || 10;
    const done = team.done || 0;
    const text = allies.length ? `Profils OP.GG des joueurs… <b class="num">${done} / ${total}</b>` : "En attente du chargement de la partie…";
    return `<section class="section lanes-loading"><p class="cp-msg"><span class="loading">${text}</span></p>
      <div class="ll-track"><div class="ll-fill" style="width:${allies.length ? Math.round(done / total * 100) : 0}%"></div></div></section>`;
  }
  const waiting = "";
  const opp = enemies.find((p) => p.isOpponent);
  return `<section class="section lanes"><div class="lanes-head"><h2 class="section-title ally">Ton équipe</h2>
      <span class="section-note">Profils OP.GG · classé solo, saison en cours</span><h2 class="section-title enemy">Équipe adverse</h2></div>
    ${waiting}${none}${rows}</section>${opp ? opponentHtml(opp) : ""}`;
}

function laneSideHtml(p, enemy) {
  if (!p) return `<div class="ln-p${enemy ? " enemy" : ""}"></div>`;
  const name = esc(p.riotId.split("#")[0]);
  const cname = esc(champ(p.championId)?.name || "");
  let stats;
  if (p.status === "loading") stats = '<span class="ln-line"><span class="loading">Profil OP.GG…</span></span>';
  else if (p.status === "error") stats = '<span class="ln-line dim">Profil introuvable sur OP.GG</span>';
  else if (p.status === "bot") stats = '<span class="ln-line dim">Bot</span>';
  else {
    const c = p.champion;
    stats = `<span class="ln-rank"><b>${esc(p.rank)}</b>${p.lp != null ? ` · ${p.lp} LP` : ""}${p.games ? ` · saison <b class="${wrClass(p.winrate)}">${pct(p.winrate, 0)}</b>` : ""}</span>
      <span class="ln-line num">${cname} ${c ? `<b class="${wrClass(c.winrate)}">${pct(c.winrate, 0)}</b> · ${int(c.games)} parties` : "· aucune partie classée"}</span>`;
  }
  const tags = (p.tags || []).slice(0, 2).map((t) => `<span class="tag ${t.kind}">${esc(t.text)}</span>`).join("");
  const cls = ["ln-p", enemy ? "enemy" : "", p.isMe ? "me" : "", p.isOpponent ? "opp" : ""].filter(Boolean).join(" ");
  return `<div class="${cls}">${p.championId ? img("champion", p.championId, "ln-champ", tipAttr("champion", p.championId)) : '<span class="ln-champ"></span>'}
    <div class="ln-info"><span class="ln-name">${name}<small>${p.isMe ? " · toi" : ""}</small></span>${stats}</div><div class="ln-tags">${tags}</div></div>`;
}

/** Détail du build choisi : runes et items côte à côte, puis sorts et ordre des compétences. */
function detailHtml(b, v, cs) {
  const skills = v.details?.skills || b.details?.skills || [];
  return `<div class="detail"><div class="panel">${runesHtml(v)}</div><div class="panel">${itemsHtml(v)}</div></div>
    <div class="detail detail-2"><div class="panel">${spellsHtml(b, v, cs)}</div>${skills.length ? `<div class="panel">${skillsHtml(skills)}</div>` : ""}</div>`;
}

function skillsHtml(order) {
  const main = ["Q", "W", "E"].map((k) => [k, order.filter((s) => s === k).length])
    .sort((a, b) => b[1] - a[1]).map(([k]) => k).join(" › ");
  const rows = ["Q", "W", "E", "R"].map((k) => `<span class="sk-key">${k}</span>` + order.slice(0, 18)
    .map((s, i) => `<span class="sk${s === k ? (k === "R" ? " on r" : " on") : ""}">${s === k ? i + 1 : ""}</span>`).join("")).join("");
  return `<h3>Compétences <span class="h3-note">${main}</span></h3>
    <div class="skills" style="--n:${Math.min(order.length, 18)}">${rows}</div>`;
}

function banHtml(bs) {
  if (!bs) return "";
  const role = ROLE_LABEL[bs.role] || "";
  const title = bs.mode === "threats" ? `Bans suggérés · menaces pour ${esc(champ(bs.championId)?.name || "")}` : `Bans suggérés · les plus forts en ${role}`;
  const note = bs.mode === "threats" ? "winrate de ces champions contre le tien (OP.GG)" : "tier list OP.GG du moment";
  // Pendant MON tour de ban (et seulement là), un clic bannit le champion (sélection + verrouillage).
  const myTurn = state.champSelect?.turn?.type === "ban";
  const chosen = state.banPick && state.banPick.action === state.champSelect?.turn?.id ? state.banPick : null;
  const card = (p) => {
    const inner = `${img("champion", p.championId, "")}
        <span class="cp-txt"><span class="cp-name">${esc(champ(p.championId)?.name || "")}</span>
          <span class="cp-games num">${bs.mode === "threats" ? `${int(p.games)} parties` : `Tier ${p.tier} · ${Math.round(p.banRate * 100)} % ban`}</span></span>
        <span class="cp-wr num ${wrClass(p.winrate)}">${pct(p.winrate)}</span>`;
    if (!myTurn) return `<div class="cp ban" ${tipAttr("champion", p.championId)}>${inner}</div>`;
    const sel = chosen?.championId === p.championId;
    return `<button type="button" class="cp ban pickable${sel ? " selected" : ""}" data-ban="${p.championId}" aria-pressed="${sel}"${chosen?.locked ? " disabled" : ""}
      aria-label="Bannir ${esc(champ(p.championId)?.name || "")}">${inner}</button>`;
  };
  const body = !bs.picks.length ? '<p class="cp-msg dim">Rien à suggérer : tous les candidats sont déjà bannis ou pris.</p>'
    : `<div class="cp-list">${bs.picks.map(card).join("")}</div>`;
  const hint = myTurn ? (chosen?.locked ? "ban verrouillé" : "clique un champion pour le bannir") : `${role} · ${note}`;
  return `<section class="section counters bans-sugg"><div class="section-head"><h2 class="section-title">${title}</h2>
    <span class="section-note">${hint}</span></div>${body}</section>`;
}

const ROLE_OF = { top: "top", jungle: "jungle", middle: "middle", mid: "middle", bottom: "bottom", utility: "utility", support: "utility" };

const ADVANTAGE = { EVEN: ["équilibrée", ""], ADVANTAGE: ["à ton avantage", "good"], DISADVANTAGE: ["à son avantage", "bad"],
                    STRONG: ["à ton avantage", "good"], WEAK: ["à son avantage", "bad"], EASY: ["à ton avantage", "good"], HARD: ["à son avantage", "bad"] };

/** Mon vis-à-vis : son profil, ses dernières parties et la lecture du duel par OP.GG. */
function opponentHtml(o) {
  const name = esc(o.riotId.split("#")[0]);
  const cname = esc(champ(o.championId)?.name || "son champion");
  const f = o.form;
  const form = !f ? (o.status === "ok" ? '<p class="cp-msg dim">Historique indisponible</p>' : "")
    : `<div class="form-dots" aria-label="${f.wins} victoires sur ${f.games} parties">${f.results.map((w) => `<span class="${w ? "w" : "l"}">${w ? "V" : "D"}</span>`).join("")}</div>
      <div class="tm-line num"><b class="${wrClass(f.wins / f.games)}">${f.wins} V – ${f.games - f.wins} D</b> · KDA ${f.kda}</div>
      <div class="tm-line num">${f.onChampion ? `${f.onChampion} partie${f.onChampion > 1 ? "s" : ""} sur ${cname} récemment` : `Pas joué ${cname} récemment`}</div>
      ${f.streak.count >= 3 ? `<span class="tag ${f.streak.win ? "bad" : "good"}">${f.streak.count} ${f.streak.win ? "victoires" : "défaites"} de suite</span>` : ""}`;
  const m = o.matchup;
  const adv = (v) => { const [t, k] = ADVANTAGE[v] || [String(v || "").toLowerCase() || "—", ""]; return `<b class="${k}">${esc(t)}</b>`; };
  const duel = !m ? '<p class="cp-msg dim">Pas de lecture du duel pour l\'instant.</p>'
    : `<div class="tm-line">Lane : ${adv(m.lane)}</div><div class="tm-line">Solo kills : ${adv(m.soloKill)}</div>
      ${m.tip ? `<blockquote class="duel-tip" lang="en">${esc(m.tip)}</blockquote>` : ""}`;
  return `<section class="section opp-view"><div class="section-head"><h2 class="section-title">Ton vis-à-vis</h2>
    <span class="section-note">${name} · ${cname}</span></div>
    <div class="opp-grid"><div class="opp-col"><h3>Dernières parties</h3>${form}</div>
    <div class="opp-col"><h3>Le duel selon OP.GG</h3>${duel}</div></div></section>`;
}

function counterHtml(cp, cur) {
  if (!cp) return "";
  const enemy = esc(champ(cp.enemyId)?.name || "");
  const how = cp.guessed ? "adversaire deviné" : "adversaire choisi à la main";
  let body;
  if (cp.status === "loading") {
    body = '<p class="cp-msg"><span class="loading">Recherche des meilleurs picks sur OP.GG…</span></p>';
  } else if (!cp.picks.length) {
    body = '<p class="cp-msg dim">Aucun champion jouable n\'a assez de parties gagnantes contre lui.</p>';
  } else {
    body = `<div class="cp-list">${cp.picks.map((p) => `
      <button type="button" class="cp${cur?.preview && cur.championId === p.championId ? " selected" : ""}" data-preview="${p.championId}" aria-pressed="${Boolean(cur?.preview && cur.championId === p.championId)}">
        ${img("champion", p.championId, "")}
        <span class="cp-txt"><span class="cp-name">${esc(champ(p.championId)?.name || "")}</span>
          <span class="cp-games num">${int(p.games)} parties${p.main ? ' · <b>ton champion</b>' : ""}</span></span>
        <span class="cp-wr num ${wrClass(p.winrate)}">${pct(p.winrate)}</span>
      </button>`).join("")}</div>`;
  }
  return `<section class="section counters"><div class="section-head"><h2 class="section-title">Bons picks contre ${enemy}</h2>
    <span class="section-note">${ROLE_LABEL[cp.role] || ""} · ${how} · winrate dans ce duel · clique pour voir le build</span></div>${body}</section>`;
}

function matchupHtml(cs, opp) {
  if (cs.mode === "aram") return "";  // pas d'adversaire de lane en ARAM
  const anyEnemy = cs.enemies.some((e) => e.championId);
  if (!opp) {
    return `<div class="matchup"><span class="dim">${anyEnemy ? "Adversaire de lane non détecté : clique sur un ennemi pour voir les builds contre lui." : "Les builds contre ton adversaire de lane apparaîtront dès qu'il sera choisi."}</span></div>`;
  }
  const name = esc(champ(opp.championId)?.name || "");
  const status = { loading: '<span class="loading">Récupération sur OP.GG…</span>', empty: '<span class="dim">pas assez de parties pour ce duel</span>' }[opp.status] || "";
  const how = opp.guessed ? "deviné d'après les rôles OP.GG · clique sur un autre ennemi pour changer" : "choisi à la main · reclique dessus pour revenir à la détection";
  return `<div class="matchup">${img("champion", opp.championId, "")}<span>Contre <b>${name}</b></span>${status}<span class="dim">${how}</span></div>`;
}

function noBuildHtml(cur) {
  const c = champ(cur.championId);
  const others = cur.roles.length ? ` Builds disponibles pour : ${cur.roles.map((r) => ROLE_LABEL[r]).join(", ")}.` : "";
  const title = `${esc(c?.name || cur.champion)}${cur.role ? " " + (ROLE_LABEL[cur.role] || "") : ""}`;
  if (cur.fetching) {
    return `<div class="empty" style="min-height: 280px"><div class="empty-inner"><h2>Récupération du build ${title}…</h2>
      <p>OP.GG répond en une ou deux secondes. Le build est ensuite gardé pour la journée.</p></div></div>`;
  }
  return `<div class="empty" style="min-height: 280px"><div class="empty-inner">
    <h2>Pas de build pour ${title}</h2>
    <p>OP.GG n'a pas assez de parties pour ce champion dans ce rôle, ou il n'a pas répondu.${others}</p></div></div>`;
}

function variantsOf(b) {
  if (b?.variants?.length) return b.variants;
  if (!b?.itemSet) return [];  // build incomplet : aucune variante plutôt qu'une exception
  return [{ primaryStyle: b.primaryStyle, subStyle: b.subStyle, perks: b.perks, spells: b.spells, blocks: b.itemSet.blocks,
            games: b.sample_size, winrate: b.winrate, pick_rate: 1, tags: [], details: b.details }];
}

// `ctx` : build ouvert depuis la bibliothèque ({ variant }), indépendant du champ select en cours (rien n'est appliqué).
function buildHtml(cur, b, cs, enter, ctx = null) {
  const variants = variantsOf(b);
  const sel = Math.min(ctx ? ctx.variant : state.selection.variant || 0, variants.length - 1);
  const applied = ctx ? null : state.applied;
  const flashKey = state.settings.flashKey;

  const cards = variants.map((v, i) => {
    const ks = perk(v.perks[0]);
    const core = (v.blocks.find((x) => x.key === "core") || v.blocks.find((x) => /^Core/.test(x.type)))?.items || [];
    const boots = (v.blocks.find((x) => x.key === "boots") || v.blocks.find((x) => /^Bottes/.test(x.type)))?.items?.[0];
    const sp = orient(v.spells, flashKey);
    const tags = [...(v.tags || []).map((t) => `<span class="tag${t === "Le plus joué" ? " alt" : t.startsWith("Contre ") ? " vs" : t.startsWith("Atypique") ? " atypical" : ""}">${esc(t)}</span>`),
      v.details?.runes?.fallback ? '<span class="tag warn">Échantillon faible</span>' : ""].join("");
    const isApplied = applied && applied.variant === i;
    const action = !cs ? "Voir" : isApplied ? "Appliqué" : state.busy && i === sel ? "Application…" : "Appliquer";
    const items = core.map((it, k) => (k ? '<svg class="arrow" viewBox="0 0 10 10"><path d="m3 2 3 3-3 3" fill="none" stroke="currentColor" stroke-width="1.5"/></svg>' : "") +
      img("item", it.id, "icon", tipAttr("item", it.id))).join("");
    return `<button type="button" class="vcard${i === sel ? " selected" : ""}${isApplied ? " applied" : ""}" data-variant="${i}" aria-pressed="${i === sel}">
      <div class="vin">
        <div class="vhead">
          ${img("perk", v.perks[0], "keystone", tipAttr("perk", v.perks[0]))}
          <div><div class="vname">${esc(ks?.name || "Runes")}</div>
            <div class="vtrees">${img("perk", v.primaryStyle, "")}${esc(style(v.primaryStyle)?.name || "")}<span>·</span>${img("perk", v.subStyle, "")}${esc(style(v.subStyle)?.name || "")}</div></div>
          <div class="vwr num"><strong class="${wrClass(v.winrate)}">${pct(v.winrate)}</strong><span>winrate</span></div>
        </div>
        <div class="tags">${tags}</div>
        <div class="pickbar"><div class="pickbar-track"><div class="pickbar-fill" style="width:${Math.max(2, v.pick_rate * 100)}%"></div></div>
          <div class="pickbar-legend num"><span>Pick ${pct(v.pick_rate, 0)}</span><span>${int(v.games)} parties</span></div></div>
        <div class="vitems">${items}<span class="gap"></span>${boots ? img("item", boots.id, "icon", tipAttr("item", boots.id)) : ""}</div>
        <div class="vfoot"><div class="spells">${sp.map((s) => img("spell", s, "icon", tipAttr("spell", s))).join("")}</div>
          <span class="vaction">${action}<span class="num"> · ${i + 1}</span></span></div>
      </div></button>`;
  }).join("");

  const v = variants[sel];
  const note = ctx ? `<div class="preview-note">Aperçu depuis les builds en cache : ta sélection de champion en cours n'est pas modifiée.</div>`
    : cs ? appliedBarHtml(applied, variants)
    : `<div class="preview-note">Aperçu : ce build s'appliquera pendant la sélection des champions${state.settings.autoApply ? " (automatiquement)" : ""}.</div>`;
  return `${note}
    <section class="section"><div class="section-head"><h2 class="section-title">Propositions de build</h2>
      <span class="section-note">Triées par winrate · touches 1 à ${variants.length} pour choisir</span></div>
      <div class="variants${enter ? " enter" : ""}">${cards}</div></section>
    <div class="detail">
      <div class="panel">${runesHtml(v)}</div>
      <div class="panel" style="display:grid; gap:22px; align-content:start">${itemsHtml(v)}${spellsHtml(b, v, cs, ctx)}</div>
    </div>`;
}

/** État de l'application du build, dans la ligne de la draft (et les problèmes éventuels dessous). */
function appliedStepsHtml(cur) {
  const applied = state.applied;
  if (!cur) return "";
  if (!applied) {
    return `<span class="draft-note">${state.settings.autoApply ? "build appliqué au verrouillage" : "clique un build pour l'appliquer"}</span>`;
  }
  const step = (ok, label) => ok === undefined ? "" : `<span class="step ${ok ? "ok" : "ko"}">${ok ? "✓" : "✕"} ${label}</span>`;
  const problems = Object.values(applied.problems || {});
  return `<span class="draft-applied" ${problems.length ? `title="${esc(problems.join(" "))}"` : ""}>${step(applied.steps.runes, "Runes")}${step(applied.steps.spells, "Sorts")}${step(applied.steps.items, "Item set")}</span>`;
}

function appliedBarHtml(applied, variants) {
  if (!applied) {
    return `<div class="applied-bar"><span>${state.settings.autoApply ? "Le build préféré sera appliqué dès que ton champion sera verrouillé." : "Clique sur un build pour l'envoyer au client."}</span></div>`;
  }
  // Étapes désactivées dans config.toml ([champselect] apply_*) : absentes de `steps`, pas affichées.
  const step = (ok, label) => ok === undefined ? "" : `<span class="step ${ok ? "ok" : "ko"}">${ok ? "✓" : "✕"} <span>${label}</span></span>`;
  const ks = perk(variants[applied.variant]?.perks?.[0]);
  return `<div class="applied-bar">${step(applied.steps.runes, "Page de runes")}${step(applied.steps.spells, "Sorts d'invocateur")}${step(applied.steps.items, "Item set")}
    <span class="spacer"></span><span>${applied.auto ? "Appliqué automatiquement" : "Appliqué"} · ${esc(ks?.name || "")}</span></div>
    ${Object.values(applied.problems || {}).map((t) => `<div class="applied-problem">${esc(t)}</div>`).join("")}`;
}

function runesHtml(v) {
  const p = v.perks;
  const rune = (id, key) => `<div class="rune${key ? " key" : ""}" ${tipAttr("perk", id)}>${img("perk", id, "")}<span class="rune-name">${esc(perk(id)?.name || id)}</span></div>`;
  const shards = p.slice(6, 9).map((id) => {
    const s = perk(id);
    return `<span class="shard" ${tipAttr("perk", id)}><i>${SHARD_ABBR[s?.shard] || "+"}</i>${esc(s?.name || id)}</span>`;
  }).join("");
  return `<h3>Runes</h3><div class="runes">
    <div><div class="tree-head">${img("perk", v.primaryStyle, "")}${esc(style(v.primaryStyle)?.name || "")}</div>
      <div class="rune-list">${rune(p[0], true)}${p.slice(1, 4).map((id) => rune(id)).join("")}</div></div>
    <div><div class="tree-head">${img("perk", v.subStyle, "")}${esc(style(v.subStyle)?.name || "")}</div>
      <div class="rune-list">${p.slice(4, 6).map((id) => rune(id)).join("")}</div>
      <div class="shards">${shards}</div></div>
  </div>`;
}

function itemsHtml(v) {
  const titles = { starter: "Départ", core: "Core", boots: "Bottes", situational: "Situationnel", consumables: "Consommables" };
  const blocks = v.blocks.map((bl) => {
    const key = bl.key || "";
    const stat = (bl.type.match(/\((.*)\)/) || [])[1] || "";
    const items = bl.items.map((it, k) => `<span class="item" ${tipAttr("item", it.id)}>${img("item", it.id)}
        ${key === "core" ? `<span class="step">${k + 1}</span>` : ""}${it.count > 1 ? `<span class="count">${it.count}</span>` : ""}</span>`).join("");
    return `<div><div class="block-head"><b>${esc(titles[key] || bl.type.replace(/\s*\(.*\)/, ""))}</b><span class="num">${esc(stat)}</span></div>
      <div class="block-items">${items}</div></div>`;
  }).join("");
  return `<div><h3>Items</h3><div class="blocks">${blocks}</div></div>`;
}

function spellsHtml(b, v, cs, ctx = null) {
  const flashKey = state.settings.flashKey;
  const opts = b.spell_options?.length ? b.spell_options : [{ spells: v.spells, games: v.games, winrate: v.winrate, pick_rate: v.pick_rate }];
  const same = (a, c) => a.length === c.length && a.every((x) => c.includes(x));
  const active = (ctx ? null : state.selection.spells) ?? opts.findIndex((o) => same(o.spells, v.spells));
  const rows = opts.map((o, i) => {
    const sp = orient(o.spells, flashKey);
    return `<button type="button" class="spell-opt${i === active ? " active" : ""}" data-spells="${i}" ${cs ? "" : "disabled"}>
      <span class="pair">${sp.map((s) => img("spell", s, "icon", tipAttr("spell", s))).join("")}</span>
      <span class="label">${sp.map((s) => esc(spell(s)?.name || s)).join(" + ")}<small class="num">${int(o.games)} parties · pick ${pct(o.pick_rate, 0)}</small></span>
      <span class="wr num ${wrClass(o.winrate)}">${pct(o.winrate)}<small>winrate</small></span></button>`;
  }).join("");
  const hint = flashKey ? `<div class="keyhint">Flash placé sur <kbd>${flashKey}</kbd></div>` : "";
  return `<div><h3>Sorts d'invocateur</h3><div class="spell-opts">${rows}</div>${hint}</div>`;
}

/* ------------------------------------------------------------------ bibliothèque */
async function loadLibrary() {
  try {
    library = await (await fetch("/api/library")).json();
  } catch { library = []; }
  renderLibrary();
  render();  // le compteur « builds en cache » de l'écran d'attente en dépend
}

/* Un build de la bibliothèque s'ouvre ici, dans la bibliothèque : il ne remplace jamais le champ select. */
let libDetail = null;  // { cur, variant }

async function openLibraryBuild(championId, role) {
  try {
    const r = await (await fetch(`/api/peek?championId=${championId}&role=${encodeURIComponent(role || "")}`)).json();
    libDetail = { cur: r.current, variant: 0 };
  } catch { toast("warn", "Le serveur local ne répond pas."); return; }
  renderLibraryDetail(true);
}

function renderLibraryDetail(enter = false) {
  const open = Boolean(libDetail);
  $("#lib-detail").hidden = !open;
  $("#lib-head").hidden = open;
  $("#lib-table").hidden = open;
  if (!open) return;
  const { cur } = libDetail;
  const b = cur.build;
  $("#lib-detail").innerHTML = `<button type="button" class="btn ghost lib-back" data-libback>← Builds en cache</button>
    ${heroHtml(cur, null, true)}${b ? buildHtml(cur, b, null, enter, libDetail) : noBuildHtml(cur)}`;
}

function renderLibrary() {
  const q = $("#lib-search").value.trim().toLowerCase();
  const rows = library
    .filter((r) => !libRole || r.role === libRole)
    .filter((r) => !q || (champ(r.championId)?.name || r.champion).toLowerCase().includes(q))
    .sort((a, b) => (champ(a.championId)?.name || a.champion).localeCompare(champ(b.championId)?.name || b.champion, "fr"));
  const today = new Date().toISOString().slice(0, 10);
  $("#lib-sub").textContent = library.length
    ? `${library.length} builds · patch ${library[0].patch} · clique sur une ligne pour voir les propositions`
    : "Aucun build pour l'instant : ils s'ajoutent quand tu choisis un champion en champ select, ou télécharge-les tous dans Réglages.";
  $("#lib-roles").innerHTML = ROLE_ORDER.map((r) => `<button type="button" class="chip" data-librole="${r}" aria-pressed="${libRole === r}" title="${ROLE_LABEL[r]}" aria-label="${ROLE_LABEL[r]}">${roleIcon(r)}</button>`).join("");
  const header = `<div class="lib-row header"><span></span><span>Champion</span><span>Rôle</span><span>Winrate</span><span>Parties</span><span>Builds</span><span>Mis à jour</span></div>`;
  $("#lib-table").innerHTML = header + rows.map((r) => `
    <button type="button" class="lib-row" data-open="${r.championId}" data-role="${r.role}">
      ${img("champion", r.championId)}
      <span class="lib-name">${esc(champ(r.championId)?.name || r.champion)}</span>
      <span class="lib-role">${roleIcon(r.role)}${r.roleLabel}</span>
      <span class="lib-wr num ${wrClass(r.winrate)}">${pct(r.winrate)}</span>
      <span class="lib-dim num">${int(r.games)}</span>
      <span class="lib-key">${img("perk", r.keystone, "", tipAttr("perk", r.keystone))}<span class="lib-dim num">×${r.variants}</span></span>
      <span class="lib-dim ${r.fetchedAt !== today ? "stale" : ""}">${day(r.fetchedAt)}</span>
    </button>`).join("");
}

function setView(v) {
  const wasLibrary = view === "library";  // un second clic sur « Builds » revient à la liste
  view = v;
  $("#view-live").hidden = v !== "live";
  $("#view-library").hidden = v !== "library";
  $("#view-settings").hidden = v !== "settings";
  document.querySelectorAll(".rail-btn").forEach((b) => b.classList.toggle("active", b.dataset.view === v));
  if (v === "library") { if (wasLibrary) libDetail = null; renderLibraryDetail(); loadLibrary(); }
  else render();
}

/* ------------------------------------------------------------------ interactions */
document.addEventListener("click", async (e) => {
  const t = e.target.closest("[data-variant],[data-spells],[data-role],[data-goto],[data-open],[data-view],[data-librole],[data-key],[data-opponent],[data-preview],[data-ban],[data-libback]");
  if (!t) return;
  if (t.dataset.ban) {
    post("/api/ban", { championId: Number(t.dataset.ban) });
  } else if (t.dataset.preview) {
    post("/api/preview", { championId: Number(t.dataset.preview) });
  } else if (t.dataset.opponent) {
    post("/api/opponent", { championId: Number(t.dataset.opponent) });
  } else if (t.dataset.open) {
    openLibraryBuild(Number(t.dataset.open), t.dataset.role);
  } else if (t.dataset.libback !== undefined) {
    libDetail = null;
    renderLibraryDetail();
  } else if (t.dataset.variant !== undefined) {
    if (view === "library" && libDetail) { libDetail.variant = Number(t.dataset.variant); renderLibraryDetail(); }
    else choose(Number(t.dataset.variant));
  } else if (t.dataset.spells !== undefined) {
    post("/api/spells", { option: Number(t.dataset.spells) });
  } else if (t.dataset.role && t.classList.contains("role-tab")) {
    if (t.getAttribute("aria-pressed") === "true") return;
    if (view === "library" && libDetail) openLibraryBuild(libDetail.cur.championId, t.dataset.role);
    else post("/api/open", { championId: state.current.championId, role: t.dataset.role });
  } else if (t.dataset.goto) {
    setView(t.dataset.goto);
  } else if (t.dataset.view) {
    setView(t.dataset.view);
  } else if (t.dataset.librole) {
    libRole = libRole === t.dataset.librole ? "" : t.dataset.librole;
    renderLibrary();
  } else if (t.dataset.key) {
    post("/api/settings", { flashKey: state.settings.flashKey === t.dataset.key ? "" : t.dataset.key });
  }
});

function choose(i) {
  if (!state?.current?.build) return;
  const n = variantsOf(state.current.build).length;
  if (i < 0 || i >= n) return;
  post("/api/select", { variant: i, apply: Boolean(state.champSelect) });
}

document.addEventListener("keydown", (e) => {
  // Raccourcis chiffrés : pas sur répétition, avec modificateur, dans un champ de saisie ou sous l'écran disco.
  if (e.repeat || e.ctrlKey || e.metaKey || e.altKey) return;
  if (e.target.closest?.("input, select, textarea, [contenteditable]")) return;
  if (typeof disco !== "undefined" && disco && !$("#disco")?.hidden) return;
  if (view === "library" && libDetail) {
    const build = libDetail.cur?.build;
    const n = build ? variantsOf(build).length : 0;
    if (/^[1-9]$/.test(e.key) && Number(e.key) <= n) { libDetail.variant = Number(e.key) - 1; renderLibraryDetail(); }
    return;
  }
  if (view !== "live") return;
  if (/^[1-9]$/.test(e.key)) choose(Number(e.key) - 1);
});

$("#opt-auto").addEventListener("change", (e) => post("/api/settings", { autoApply: e.target.checked }));
$("#opt-alert").addEventListener("change", (e) => post("/api/settings", { turnAlert: e.target.checked }));
$("#set-alert").addEventListener("change", (e) => post("/api/settings", { turnAlert: e.target.checked }));
$("#set-focus").addEventListener("change", (e) => post("/api/settings", { alertFocus: e.target.checked }));
$("#set-auto").addEventListener("change", (e) => post("/api/settings", { autoApply: e.target.checked }));
$("#set-spells").addEventListener("change", (e) => post("/api/settings", { applySpells: e.target.checked }));
$("#set-atypical").addEventListener("change", (e) => post("/api/settings", { atypical: e.target.checked }));
$("#set-volume").addEventListener("input", (e) => { $("#set-volume-val").textContent = `${Math.round(e.target.value * 100)} %`; });
$("#set-volume").addEventListener("change", (e) => {
  post("/api/settings", { volume: Number(e.target.value) });
  playSound("cloche", Number(e.target.value));
});
$("#set-sounds").addEventListener("change", (e) => {
  const sel = e.target.closest("select[data-sound]");
  if (!sel) return;
  post("/api/settings", { sounds: { [sel.dataset.sound]: sel.value } });
  playSound(sel.value, state?.settings?.volume);
});
$("#view-settings").addEventListener("click", (e) => {
  const jump = e.target.closest("[data-setjump]");
  if (jump) settingsJump(jump.dataset.setjump);
  const test = e.target.closest("[data-testsound]");
  if (test) playSound($(`select[data-sound="${test.dataset.testsound}"]`).value, state?.settings?.volume);
  const theme = e.target.closest("[data-theme]");
  if (theme) { applyTheme(theme.dataset.theme); post("/api/settings", { theme: theme.dataset.theme }); }
  const flash = e.target.closest("[data-setflash]");
  if (flash) post("/api/settings", { flashKey: flash.dataset.setflash });
  if (e.target.closest("[data-prefetch]")) post("/api/prefetch", {});
  const upd = e.target.closest("[data-update]");
  if (upd) post(`/api/update/${upd.dataset.update}`, {});
});
$("#lib-search").addEventListener("input", renderLibrary);

/* Garde-fou : seule .main défile. Un élément qui prend le focus ou un défilement automatique pourrait sinon décaler
   la fenêtre entière (barre de titre et rail hors de l'écran), même avec overflow: hidden. */
for (const el of [document.documentElement, document.body, $(".app")]) {
  el?.addEventListener("scroll", () => { if (el.scrollTop || el.scrollLeft) el.scrollTo(0, 0); });
}
window.addEventListener("scroll", () => { if (window.scrollY || window.scrollX) window.scrollTo(0, 0); });

/** Sommaire des réglages : ne fait défiler que la zone de contenu. `scrollIntoView` ferait aussi défiler la fenêtre
    entière (barre de titre et rail sortaient de l'écran). */
function settingsJump(id) {
  const main = $(".main");
  const target = $(`#${id}`);
  if (!main || !target) return;
  const top = target.getBoundingClientRect().top - main.getBoundingClientRect().top + main.scrollTop - 16;
  main.scrollTo({ top: Math.max(0, top), behavior: "smooth" });
}

/* ------------------------------------------------------------------ minuteur */
function tickTimer() {
  if (!timerEnds) return;
  const left = Math.max(0, Math.round((timerEnds - Date.now()) / 1000));
  for (const el of document.querySelectorAll(".js-timer")) {
    el.textContent = `${Math.floor(left / 60)}:${String(left % 60).padStart(2, "0")}`;
    el.classList.toggle("low", left <= 10);
  }
  // Barre du tour : se remplit de gauche à droite à mesure que le temps s'écoule.
  const lcuTotal = state?.champSelect?.timer?.total;
  const total = lcuTotal && lcuTotal >= timerEnds - Date.now() - 1000 ? lcuTotal : turnTotal;
  for (const el of document.querySelectorAll(".js-turn-fill")) {
    const done = total ? Math.min(1, Math.max(0, 1 - (timerEnds - Date.now()) / total)) : 0;
    el.style.transform = `scaleX(${done})`;
    el.classList.toggle("low", left <= 10);
  }
}
setInterval(tickTimer, 250);

/* ------------------------------------------------------------------ infobulles */
const tip = $("#tooltip");
document.addEventListener("mouseover", (e) => {
  const t = e.target.closest("[data-tip]");
  if (!t) { tip.hidden = true; return; }
  const id = t.dataset.id;
  let html = "";
  if (t.dataset.tip === "item") {
    const it = item(id);
    html = it ? `<b>${esc(it.name)}</b><em class="num">${int(it.gold)} or</em>${esc(it.text)}` : "";
  } else if (t.dataset.tip === "perk") {
    const p = perk(id);
    html = p ? `<b>${esc(p.name)}</b>${p.shard ? "Fragment de stats" : esc(p.text)}` : "";
  } else if (t.dataset.tip === "spell") {
    const s = spell(id);
    html = s ? `<b>${esc(s.name)}</b>${esc(s.text)}` : "";
  } else if (t.dataset.tip === "champion") {
    const c = champ(id);
    html = c ? `<b>${esc(c.name)}</b>${esc(c.title)}` : "";
  }
  tip.innerHTML = html;
  tip.hidden = !html;
});
document.addEventListener("mousemove", (e) => {
  if (tip.hidden) return;
  const r = tip.getBoundingClientRect();
  const x = Math.min(e.clientX + 14, innerWidth - r.width - 8);
  const y = e.clientY + 18 + r.height > innerHeight ? e.clientY - r.height - 12 : e.clientY + 18;
  tip.style.left = x + "px";
  tip.style.top = y + "px";
});

/* ------------------------------------------------------------------ toasts */
function toast(kind, text) {
  const el = document.createElement("div");
  el.className = `toast ${kind}`;
  el.textContent = text;
  $("#toasts").append(el);
  setTimeout(() => { el.classList.add("out"); setTimeout(() => el.remove(), 350); }, 2600);
}

/* ------------------------------------------------------------------ fenêtre de bureau */
if (new URLSearchParams(location.search).has("desktop")) {
  $("#winctl").hidden = false;
  $("#win-min").onclick = () => api()?.minimize();
  $("#win-close").onclick = () => api()?.close();
  $("#win-pin").onclick = async () => {
    const on = await api()?.toggle_on_top();
    $("#win-pin").setAttribute("aria-pressed", String(Boolean(on)));
  };
}

/* ------------------------------------------------------------------ démarrage */
async function loadStatic() {
  try {
    const r = await fetch("/api/static");
    if (r.ok) S = await r.json();
  } catch { /* noms affichés en ids si les données manquent */ }
}

(async () => {
  await loadStatic();
  connect();
  loadLibrary();
})();
