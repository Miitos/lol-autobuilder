/* lolbuilds — Disco pick : pendant ton tour de pick, une roulette eurobeat choisit au hasard ton champion, son build
   et tes sorts, puis le verrouille dès l'arrêt de la roulette (on assume le hasard). L'écran reste jusqu'au lancement
   de la partie.
   Activable dans les Réglages ; s'appuie sur les globales de app.js (state, post…). */
"use strict";

const DISCO_SPELLS = [1, 3, 4, 6, 7, 11, 12, 14, 21];  // sorts de la faille tirés en « Chaos total »
const DISCO_BPM = 155;
const DISCO_SPIN_MS = 15000;  // la roulette tourne 15 s…
const DISCO_SPEED = 1500;  // px/s pendant la pleine vitesse (≈ 14 vignettes par seconde, au rythme de l'eurobeat)
const DISCO_MARGIN_MS = 1800;  // …sauf s'il ne reste plus le temps de verrouiller après (réponse du client)
const DISCO_RUSH_MS = 5000;  // moins de temps que ça avant la fin du tour : roulette courte et deux fois plus rapide
const DISCO_VU_COLS = 64;

let disco = null;  // partie en cours : { id, reel, roll, timers, music, raf, ... }

/* ------------------------------------------------------------------ réglages */
function renderDiscoSettings() {
  const d = state?.settings?.disco;
  if (!d) return;
  $("#set-disco-on").checked = d.enabled;
  document.querySelectorAll("[data-disco]").forEach((b) => {
    const [key, value] = b.dataset.disco.split(":");
    b.setAttribute("aria-pressed", String(d[key] === value));
  });
  document.querySelectorAll(".disco-opt").forEach((el) => el.classList.toggle("off", !d.enabled));
}

$("#set-disco-on").addEventListener("change", (e) => post("/api/settings", { disco: { enabled: e.target.checked } }));
document.addEventListener("click", (e) => {
  const opt = e.target.closest("[data-disco]");
  if (opt) {
    const [key, value] = opt.dataset.disco.split(":");
    post("/api/settings", { disco: { [key]: value } });
  }
  if (e.target.closest("[data-discostart]")) discoStart();
  const act = e.target.closest("[data-discoact]");
  if (act) ({ nitro: discoNitro, close: discoHide })[act.dataset.discoact]?.();
});

/** Bouton de la colonne de gauche, pendant mon tour de pick (si le Disco pick est activé). */
function discoButtonHtml() {
  const d = state?.settings?.disco;
  // Jamais en ARAM : le champion y est attribué au hasard par le jeu.
  if (!d?.enabled || state.champSelect?.mode === "aram" || state.champSelect?.turn?.type !== "pick") return "";
  const role = ROLE_LABEL[(state.champSelect.allies.find((a) => a.isMe) || {}).position];
  return `<button type="button" class="disco-btn" data-discostart>
    <span class="disco-btn-streaks" aria-hidden="true"><i></i><i></i><i></i><i></i></span>
    ${discoBallSvg("disco-btn-ball")}
    <span class="disco-btn-txt"><b>DISCO PICK ››</b><span>LE HASARD PREND LE VOLANT</span>
      <small>${d.pool === "lane" && role ? `champion ${esc(role)} au hasard` : "n'importe quel champion"} · build et sorts</small></span></button>`;
}

function discoBallSvg(cls) {
  return `<svg class="${cls}" viewBox="0 0 100 100" aria-hidden="true">
    <defs><radialGradient id="db-g" cx="38%" cy="32%" r="70%"><stop offset="0" stop-color="#fff"/><stop offset=".35" stop-color="#c9d3e6"/><stop offset="1" stop-color="#3a4256"/></radialGradient>
    <clipPath id="db-c"><circle cx="50" cy="50" r="46"/></clipPath></defs>
    <circle cx="50" cy="50" r="46" fill="url(#db-g)"/>
    <g clip-path="url(#db-c)" stroke="#1d2433" stroke-width="1.4" fill="none" opacity=".75"><path d="M4 20H96M4 32H96M4 44H96M4 56H96M4 68H96M4 80H96"/>
      <path d="M50 4V96M36 4C30 30 30 70 36 96M64 4C70 30 70 70 64 96M22 8C12 34 12 66 22 92M78 8C88 34 88 66 78 92"/></g>
    <rect x="30" y="26" width="9" height="9" fill="#fff" opacity=".9"/><rect x="58" y="50" width="8" height="8" fill="#3ff0ff" opacity=".8"/>
    <rect x="40" y="62" width="8" height="8" fill="#ff4fd8" opacity=".8"/></svg>`;
}

/* ------------------------------------------------------------------ l'écran disco */
function discoTachSvg() {
  const ticks = Array.from({ length: 19 }, (_, i) => `<line x1="100" y1="22" x2="100" y2="${i % 2 ? 29 : 34}" stroke="${i >= 14 ? "#ff2d55" : "#e8e8f0"}"
    stroke-width="${i % 2 ? 1.5 : 3}" transform="rotate(${-135 + i * 15} 100 100)"/>`).join("");
  const nums = Array.from({ length: 10 }, (_, n) => `<text x="100" y="50" fill="${n >= 8 ? "#ff2d55" : "#e8e8f0"}" font-size="13" font-weight="700"
    text-anchor="middle" transform="rotate(${-135 + n * 30} 100 100)">${n}</text>`).join("");
  return `<svg viewBox="0 0 200 200" aria-hidden="true"><circle cx="100" cy="100" r="94" fill="rgba(10,6,20,.85)" stroke="#3ff0ff" stroke-width="2"/>
    <path class="dz-redline" d="M 166.5 166.5 A 94 94 0 0 0 190 100" transform="rotate(-45 100 100)" stroke="#ff2d55" stroke-width="8" fill="none"/>
    ${ticks}${nums}<g class="dz-needle"><line x1="100" y1="100" x2="100" y2="24" stroke="#ff7a1a" stroke-width="4" stroke-linecap="round"/></g>
    <circle cx="100" cy="100" r="9" fill="#ff7a1a"/><text class="dz-rpm" x="100" y="142" fill="#fff" font-size="19" font-weight="700" text-anchor="middle">9 800</text>
    <text x="100" y="158" fill="#3ff0ff" font-size="9" letter-spacing="2" text-anchor="middle">TR/MIN · ${DISCO_BPM} BPM</text></svg>`;
}

function discoOverlay() {
  let el = $("#disco");
  if (el) return el;
  el = document.createElement("div");
  el.id = "disco";
  el.className = "disco";
  el.hidden = true;
  el.setAttribute("role", "dialog");
  el.setAttribute("aria-label", "Disco pick");
  const rays = Array.from({ length: 18 }, (_, i) => `<i style="rotate:${i * 20}deg; animation-delay:${-(i * 0.07).toFixed(2)}s; --c:${["#3ff0ff", "#ff4fd8", "#fff", "#c6ff4f"][i % 4]}"></i>`).join("");
  const streaks = [[12, 380, "#3ff0ff", 0, .45], [22, 260, "#ff4fd8", -.2, .38], [31, 420, "#fff", -.1, .52], [44, 200, "#ff7a1a", -.33, .4],
    [64, 300, "#c6ff4f", -.3, .41], [72, 460, "#3ff0ff", -.15, .48], [81, 220, "#ff4fd8", -.05, .36]]
    .map(([t, w, c, d, du]) => `<i style="top:${t}%; width:${w}px; --c:${c}; animation-delay:${d}s; animation-duration:${du}s"></i>`).join("");
  const cols = Array.from({ length: DISCO_VU_COLS }, () => '<span class="vu-col"><i></i><b></b></span>').join("");
  const scale = ["-20", "-10", "-7", "-5", "-3", "-1", "0"].map((t) => `<span>${t}</span>`).join("") + "<span><b>+3</b></span>";
  el.innerHTML = `<div class="dz-sky"></div><div class="dz-sun"></div><div class="dz-floor-wrap"><div class="dz-floor"></div></div>
    <div class="dz-rays">${rays}</div><div class="dz-streaks">${streaks}</div><div class="dz-strobe"></div>
    <div class="dz-top"><span class="dz-tag"></span><span class="dz-music"></span></div>
    <div class="dz-ball">${discoBallSvg("dz-ball-svg")}</div><div class="dz-tach">${discoTachSvg()}</div>
    <div class="dz-stage"></div>
    <button type="button" class="dz-close" data-discoact="close" aria-label="Fermer l'écran disco" title="Fermer (le tirage va quand même au bout)">×</button>
    <div class="vu"><div class="vu-head"><span class="led"></span><span>VU · EUROBEAT ${DISCO_BPM} BPM</span><span class="scale">${scale}</span><span>dB</span></div>
      <div class="vu-bars">${cols}</div></div>`;
  document.body.appendChild(el);
  return el;
}

async function discoStart() {
  if (state.champSelect?.mode === "aram") return;  // pas de Disco en ARAM
  if (disco && disco.locked === undefined) return;  // tirage déjà en cours (ou écran fermé en attendant le verrouillage)
  discoStop(false);
  const set = state.settings.disco;
  const el = discoOverlay();
  disco = { busy: true, timers: [] };
  const r = await post("/api/disco/roll", {});
  // Échec : le serveur a déjà poussé un toast d'erreur (ou post() en a affiché un) : pas de second message ici.
  if (!r.ok) { if (disco?.busy) disco = null; return; }
  if (!disco) return;  // annulé (fin du champ select) pendant la requête
  // Temps restant mesuré par le serveur au tirage ; le champion n'est envoyé au client qu'à l'arrêt de la roulette.
  const left = r.timeLeft ?? (state.champSelect?.timer?.endsAt ? state.champSelect.timer.endsAt - Date.now() : 30000);
  const rush = left - DISCO_MARGIN_MS < DISCO_RUSH_MS;
  Object.assign(disco, { id: r.championId, reel: r.reel, count: r.count, role: r.role, busy: false, rush,
                         duration: Math.max(700, Math.min(DISCO_SPIN_MS, left - DISCO_MARGIN_MS)) });
  const reduced = matchMedia("(prefers-reduced-motion: reduce)").matches;
  el.className = `disco lights-${reduced ? "soft" : set.lights}`;
  el.querySelector(".dz-tag").textContent = `DISCO PICK · ${r.role ? (ROLE_LABEL[r.role] || r.role).toUpperCase() : "TOUS RÔLES"}`;
  el.querySelector(".dz-music").textContent = `♫ EUROBEAT · ${DISCO_BPM} BPM`;
  el.hidden = false;
  discoRollView();
  try { discoMusicStart(); } catch { /* sans son (AudioContext refusé) : la roulette tourne quand même */ }
  discoSpin();
}

function discoRollView() {
  $("#disco .dz-stage").innerHTML = `<div class="dz-roll">
    <div class="dz-title-row"><span class="dz-chev">${"<i>›</i>".repeat(6)}</span><h1 class="dz-title">${disco.rush ? "TURBO BOOST !" : "PLEIN GAZ !"}</h1><span class="dz-chev pink">${"<i>›</i>".repeat(6)}</span></div>
    <div class="dz-reel"><div class="dz-track"></div><div class="dz-frame"></div><span class="dz-pin"></span></div>
    <p class="dz-sub">${int(disco.count)} CHAMPIONS ${disco.role ? (ROLE_LABEL[disco.role] || "").toUpperCase() : ""} · AUCUN FREIN</p>
    <div class="dz-actions"><button type="button" class="dz-btn lime" data-discoact="nitro">NITRO ›› ARRÊT IMMÉDIAT</button></div></div>`;
}

/** Fait défiler la bande à pleine vitesse, puis freine sur les dernières secondes pour s'arrêter pile sur le
    champion tiré. Vitesse constante puis décélération linéaire : le défilement dure vraiment toute la roulette. */
function discoSpin() {
  const track = $("#disco .dz-track");
  const reel = $("#disco .dz-reel");
  if (!track || !reel) return;
  const step = 110;  // largeur d'une vignette + écart (voir .dz-track img)
  const total = disco.duration / 1000;
  const brake = Math.min(4.5, total * (disco.rush ? 0.3 : 0.45));  // s de freinage à la fin
  const cruise = total - brake;
  const offset = 14 + 48 - reel.clientWidth / 2;  // centre de la vignette sous le cadre
  const speed = DISCO_SPEED * (disco.rush ? 2 : 1);  // fin de tour proche : ça défile deux fois plus vite
  const wanted = speed * (cruise + brake / 2);  // distance parcourue à la vitesse visée
  const n = disco.reel.length;
  let target = Math.max(0, Math.ceil((wanted - offset) / step));
  while (disco.reel[target % n] !== disco.id) target++;
  track.innerHTML = Array.from({ length: target + 12 }, (_, i) => disco.reel[i % n])
    .map((id) => `<img src="${asset("champion", id)}" alt="" onerror="this.style.visibility='hidden'">`).join("");
  const end = offset + target * step;
  const v = end / (cruise + brake / 2);  // vitesse ajustée pour tomber pile sur la cible
  const t0 = performance.now();
  const run = (now) => {
    if (!disco) return;
    const t = Math.min(total, (now - t0) / 1000);
    const tau = Math.max(0, t - cruise);
    const x = v * Math.min(t, cruise) + v * tau - (v * tau * tau) / (2 * brake);
    track.style.transform = `translateX(${-x}px)`;
    track.style.filter = `blur(${(2.6 * (1 - tau / brake)).toFixed(2)}px)`;
    if (t < total) disco.spin = requestAnimationFrame(run);
    else discoResult();
  };
  disco.spin = requestAnimationFrame(run);
}

function discoNitro() {
  if (!disco || disco.result) return;
  cancelAnimationFrame(disco.spin);
  discoResult();
}

/** Le champion tiré : son build (proposition au hasard ou la plus jouée) et ses sorts, puis le compte à rebours. */
function discoResult() {
  if (!disco) return;
  disco.result = true;
  const set = state.settings.disco;
  const c = champ(disco.id);
  $("#disco .dz-stage").innerHTML = `<div class="dz-card">
    <div class="dz-splash" style="background-image:url('${asset("splash", disco.id)}')"><div class="dz-splash-shade"></div>
      <div class="dz-who"><span class="dz-fate">LE DESTIN A CHOISI <span class="dz-chev small">${"<i>›</i>".repeat(4)}</span></span>
        <h1 class="dz-title big">${esc((c?.name || "").toUpperCase())}</h1></div>
      <div class="dz-flames" aria-hidden="true"><i></i><i></i><i></i><i></i><i></i></div></div>
    <div class="dz-build"><p class="dz-wait loading">Récupération du build sur OP.GG…</p></div>
    <div class="dz-foot"><span class="dz-count"></span><span class="dz-foot-txt"></span></div></div>`;
  discoConfetti();
  // Sorts « Chaos total » tirés tout de suite : ils ne dépendent pas du build.
  const pool = [...DISCO_SPELLS];
  const draw = () => pool.splice(Math.floor(Math.random() * pool.length), 1)[0];
  disco.spells = set.spells === "chaos" ? [draw(), draw()] : null;
  discoLock();  // le hasard a parlé : verrouillé sur-le-champ (le serveur attend le build pour l'envoyer)
  // Le build arrive par l'état poussé par le serveur (champion survolé) : on l'attend un peu.
  const waitBuild = (tries = 0) => {
    if (!disco) return;
    const cur = state.current;
    // Build affiché une fois connu, avec la proposition tirée par le serveur au verrouillage.
    if (cur?.championId === disco.id && cur.build && !cur.fetching && disco.variant !== undefined) return discoShowBuild(cur.build, set);
    // Pas de build en route (ni en cache, ni demandé à OP.GG) : inutile d'attendre davantage.
    if (tries > 60 || (tries > 8 && cur?.championId === disco.id && !cur.build && !cur.fetching)) return discoShowBuild(null, set);
    disco.timers.push(setTimeout(() => waitBuild(tries + 1), 200));
  };
  waitBuild();
}

function discoShowBuild(b, set) {
  if (!disco) return;
  const v = b ? variantsOf(b)[disco.variant || 0] : null;
  const spells = disco.spells || (v ? orient(v.spells, state.settings.flashKey) : []);
  const core = v ? ((v.blocks.find((x) => x.key === "core") || v.blocks.find((x) => /^Core/.test(x.type)))?.items || []) : [];
  $("#disco .dz-build").innerHTML = v ? `
    <div><h3>Runes${set.build === "random" ? " (au hasard)" : ""}</h3><span class="dz-ks">${img("perk", v.perks[0], "dz-ks-img")}<b>${esc(perk(v.perks[0])?.name || "")}</b></span></div>
    <div><h3>Core</h3><span class="dz-icons">${core.map((it) => img("item", it.id, "icon", tipAttr("item", it.id))).join("")}</span></div>
    <div><h3>Sorts${set.spells === "chaos" ? " (chaos)" : ""}</h3><span class="dz-icons">${spells.map((s) => img("spell", s, "icon", tipAttr("spell", s))).join("")}</span></div>`
    : `<p class="dz-wait">Pas de build OP.GG pour ce champion : seul le champion est verrouillé.</p>`;
}

async function discoLock() {
  if (!disco || disco.locking || !disco.result) return;
  disco.locking = true;
  $("#disco .dz-foot-txt").innerHTML = "VERROUILLAGE…";
  // variant -1 : le serveur tire une des propositions OP.GG une fois le build arrivé.
  const r = await post("/api/disco/lock", { championId: disco.id, variant: state.settings.disco.build === "random" ? -1 : 0, spells: disco.spells });
  if (!disco) return;
  disco.locked = r.ok;
  disco.variant = r.variant ?? 0;
  $("#disco .dz-count").textContent = r.ok ? "✓" : "!";
  $("#disco .dz-foot-txt").innerHTML = r.ok ? "VERROUILLÉ · DIRECTION LA FAILLE<br><small>la musique continue jusqu'au lancement de la partie</small>"
    : "VERROUILLAGE REFUSÉ PAR LE CLIENT<br><small>choisis ton champion dans League</small>";
  if (disco.hidden) discoClose();  // écran déjà fermé par la croix : le tirage est allé au bout
}

/** La croix : ferme l'écran et coupe la musique. On assume : un tirage commencé va quand même jusqu'au verrouillage. */
function discoHide() {
  if (!disco || disco.locked !== undefined) return discoClose();
  disco.hidden = true;
  $("#disco").hidden = true;
  discoMusicStop();
  disco.music = null;
}

function discoClose() { discoStop(true); }

function discoStop(hide) {
  if (!disco) { if (hide && $("#disco")) $("#disco").hidden = true; return; }
  cancelAnimationFrame(disco.spin);
  disco.timers.forEach(clearTimeout);
  discoMusicStop();
  disco = null;
  if (hide && $("#disco")) $("#disco").hidden = true;
}

function discoConfetti() {
  const box = $("#disco .dz-stage");
  const colors = ["#ff4fd8", "#3ff0ff", "#c6ff4f", "#ff7a1a", "#9b5cff"];
  box.insertAdjacentHTML("beforeend", `<div class="dz-confetti" aria-hidden="true">${Array.from({ length: 16 }, (_, i) =>
    `<i style="left:${(i * 6.3 + 3) % 100}%; background:${colors[i % 5]}; animation-delay:${(-i * 0.37).toFixed(2)}s; animation-duration:${(1.8 + (i % 4) * 0.25).toFixed(2)}s"></i>`).join("")}</div>`);
}

/* La piste de danse reste ouverte jusqu'au lancement de la partie (ou une esquive) : fin du champ select. */
setInterval(() => {
  if (disco && !disco.busy && !state?.champSelect) discoClose();
}, 500);

/* ------------------------------------------------------------------ musique (boucle synthétisée ou MP3 du joueur) */
// Boucle eurobeat à 155 BPM : grosse caisse, basse en octaves sur les contretemps, charleston, clap et arpège en la mineur.
const EURO_CHORDS = [[57, 60, 64], [53, 57, 60], [55, 59, 62], [52, 55, 59]];  // Am F G Em
const EURO_BASS = [33, 29, 31, 28];
const midiHz = (m) => 440 * Math.pow(2, (m - 69) / 12);

function discoAudio() {
  audio = audio || new AudioContext();
  if (audio.state === "suspended") audio.resume();
  return audio;
}

function discoMusicStart(seconds = 0) {
  const set = state.settings;
  const ctx = discoAudio();
  const out = ctx.createGain();
  out.gain.value = 0.55 * (set.volume ?? 0.6);
  const analyser = ctx.createAnalyser();
  analyser.fftSize = 256;
  out.connect(analyser).connect(ctx.destination);
  const music = { out, analyser, ctx, stopped: false };
  if (set.disco.music) {  // fichier posé dans le dossier lolbuilds (disco-music.mp3…)
    const el = new Audio(`/api/disco/music?t=${Date.now()}`);
    el.loop = true;
    ctx.createMediaElementSource(el).connect(out);
    el.play().catch(() => { if (!music.stopped) discoSynth(music); });  // fichier illisible : la boucle intégrée prend le relais
    music.el = el;
  } else discoSynth(music);
  if (disco) disco.music = music;
  discoVu(music);
  if (seconds) setTimeout(() => discoMusicStop(music), seconds * 1000);
  return music;
}

function discoSynth(music) {
  if (music.stopped || music.timer) return;  // arrêtée entre-temps (AbortError du play) : ne pas fuir un setInterval
  const { ctx, out } = music;
  const sixteenth = 60 / DISCO_BPM / 4;
  const noise = ctx.createBuffer(1, ctx.sampleRate * 0.3, ctx.sampleRate);
  const data = noise.getChannelData(0);
  for (let i = 0; i < data.length; i++) data[i] = Math.random() * 2 - 1;
  let step = 0;
  let next = ctx.currentTime + 0.05;
  const voice = (type, freq, t, dur, gain, filter) => {
    const o = ctx.createOscillator();
    const g = ctx.createGain();
    o.type = type;
    o.frequency.setValueAtTime(freq, t);
    g.gain.setValueAtTime(gain, t);
    g.gain.exponentialRampToValueAtTime(0.0001, t + dur);
    let node = o;
    if (filter) { const f = ctx.createBiquadFilter(); f.type = "lowpass"; f.frequency.value = filter; node.connect(f); node = f; }
    node.connect(g).connect(out);
    o.start(t);
    o.stop(t + dur + 0.02);
    return o;
  };
  const hit = (t, dur, gain, type, freq) => {
    const s = ctx.createBufferSource();
    s.buffer = noise;
    const f = ctx.createBiquadFilter();
    f.type = type;
    f.frequency.value = freq;
    const g = ctx.createGain();
    g.gain.setValueAtTime(gain, t);
    g.gain.exponentialRampToValueAtTime(0.0001, t + dur);
    s.connect(f).connect(g).connect(out);
    s.start(t);
    s.stop(t + dur);
  };
  const play = (s, t) => {
    const bar = Math.floor(s / 16) % 4;
    const pos = s % 16;
    if (pos % 4 === 0) {  // grosse caisse sur chaque temps
      const k = voice("sine", 150, t, 0.28, 0.9);
      k.frequency.exponentialRampToValueAtTime(42, t + 0.12);
    }
    if (pos % 2 === 0) voice("sawtooth", midiHz(EURO_BASS[bar] + (pos % 4 === 2 ? 12 : 0)), t, sixteenth * 1.8, 0.32, 520);  // basse octaviée
    if (pos % 4 === 2) hit(t, 0.05, 0.25, "highpass", 7000);  // charleston sur le contretemps
    if (pos === 4 || pos === 12) hit(t, 0.16, 0.35, "bandpass", 1600);  // clap
    const chord = EURO_CHORDS[bar];
    const note = chord[[0, 1, 2, 1][pos % 4]] + (pos >= 8 ? 12 : 0);
    voice("square", midiHz(note + 12), t, sixteenth * 0.9, 0.07, 3200);  // arpège
    voice("sawtooth", midiHz(note + 12) * 1.006, t, sixteenth * 0.9, 0.05, 2600);
  };
  music.timer = setInterval(() => {
    while (next < ctx.currentTime + 0.12) { play(step++, next); next += sixteenth; }
  }, 25);
}

function discoMusicStop(music = disco?.music) {
  if (!music) return;
  music.stopped = true;
  clearInterval(music.timer);
  cancelAnimationFrame(music.vu);
  music.el?.pause();
  try { music.out.gain.setTargetAtTime(0, music.ctx.currentTime, 0.05); } catch { /* déjà arrêtée */ }
  setTimeout(() => { try { music.out.disconnect(); } catch { /* déjà débranchée */ } }, 300);
}

/** VU-mètre à LED piloté par le vrai signal (analyseur WebAudio), avec repère de crête. */
function discoVu(music) {
  const cols = [...document.querySelectorAll("#disco .vu-col")];
  if (!cols.length) return;
  const bins = new Uint8Array(music.analyser.frequencyBinCount);
  const peaks = new Array(cols.length).fill(0);
  const used = Math.floor(bins.length * 0.75);
  const draw = () => {
    music.analyser.getByteFrequencyData(bins);
    cols.forEach((col, i) => {
      const a = Math.floor((i / cols.length) * used);
      const level = Math.min(1, (bins[a] / 255) * 1.15);
      peaks[i] = Math.max(level, peaks[i] - 0.012);
      col.firstChild.style.clipPath = `inset(${((1 - level) * 100).toFixed(1)}% 0 0 0)`;
      col.lastChild.style.top = `${((1 - peaks[i]) * 100).toFixed(1)}%`;
    });
    music.vu = requestAnimationFrame(draw);
  };
  draw();
}
