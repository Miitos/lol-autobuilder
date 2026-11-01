"""Serveur local de l'interface : état du champ select en direct (WebSocket) + actions.

Tout tourne dans une seule boucle asyncio :
  - `Controller.lcu_loop` suit le client League (connexion, phase, session) ;
  - `Controller.live_worker` récupère sur OP.GG, au moment du pick, le build du jour
    et les propositions contre l'adversaire de lane (cache d'une journée, voir `live.py`) ;
  - les routes HTTP servent l'UI, les images en cache et les actions (appliquer un build...) ;
  - chaque changement d'état est poussé aux pages ouvertes via `/ws`.
"""

from __future__ import annotations

import asyncio
import dataclasses
import json
import logging
import os
import sys
import tempfile
import time
from datetime import date
from pathlib import Path
from typing import Any

from aiohttp import WSMsgType, web

from .. import ROLE_LABELS, ROLES, __version__, players, updater
from ..cache import BuildCache
from .applier import BuildApplier, DryRunLcu, Lcu, find_build
from ..config import Config
from ..ddragon import ChampionCatalog, load_champion_catalog
from ..live import LiveBuilds
from ..realms import ensure_realm_assets
from ..models import Build
from .lcu import LcuConnection, wait_for_client

log = logging.getLogger(__name__)

UI_DIR = Path(__file__).parent / "ui"
APPLY_DEBOUNCE = 0.8  # s : évite de recréer une page de runes à chaque survol
LIVE_DEBOUNCE = 0.6  # s : le client envoie les sessions en rafale
COUNTER_MIN_GAMES = 300  # parties minimum dans le duel pour proposer un pick
COUNTER_MAX = 8
MAIN_MASTERY_RANK = 15  # « tes champions » : les 15 meilleures maîtrises
BAN_MAX = 6


THEMES = ("hextech", "noxus", "piltover", "ionia")
SOUNDS = ("none", "cloche", "hextech", "gong", "bip")
# Événements du champ select qui peuvent jouer un son (réglages > Alertes).
SOUND_EVENTS = {"start": "cloche", "banPhase": "none", "pickPhase": "none", "myBan": "gong", "myPick": "hextech"}


class Prefs:
    """Préférences de l'UI (application auto, alertes, thème, touche de Flash, variante préférée par champion)."""

    def __init__(self, path: Path, flash_key: str) -> None:
        self.path = path
        self.data: dict[str, Any] = {
            "autoApply": True, "turnAlert": True, "alertFocus": True, "volume": 0.6,
            "sounds": dict(SOUND_EVENTS), "theme": "hextech", "flashKey": flash_key, "preferred": {},
        }
        try:
            saved = json.loads(path.read_text(encoding="utf-8"))
            self.data.update({k: v for k, v in saved.items() if k != "sounds"})
            self.data["sounds"].update({k: v for k, v in (saved.get("sounds") or {}).items()
                                        if k in SOUND_EVENTS and v in SOUNDS})
        except (OSError, ValueError, AttributeError):
            pass

    def update(self, body: dict[str, Any]) -> None:
        """Applique les réglages envoyés par la page (valeurs invalides ignorées)."""
        for key in ("autoApply", "turnAlert", "alertFocus"):
            if key in body:
                self.data[key] = bool(body[key])
        if isinstance(body.get("volume"), int | float):
            self.data["volume"] = min(1.0, max(0.0, float(body["volume"])))
        for event, sound in (body.get("sounds") or {}).items():
            if event in SOUND_EVENTS and sound in SOUNDS:
                self.data["sounds"][event] = sound
        if body.get("theme") in THEMES:
            self.data["theme"] = body["theme"]
        if body.get("flashKey") in ("D", "F", ""):
            self.data["flashKey"] = body["flashKey"]

    def save(self) -> None:
        self.path.write_text(json.dumps(self.data, indent=2), encoding="utf-8")


def _flat_variant(b: Build) -> dict[str, Any]:
    """Build sans liste de variantes (ancien cache) vu comme une proposition."""
    return {"primaryStyle": b.primaryStyle, "subStyle": b.subStyle, "perks": b.perks, "spells": b.spells,
            "blocks": b.itemSet.get("blocks", []), "games": b.sample_size, "winrate": b.winrate, "pick_rate": 1,
            "tags": [], "details": b.details}


def select_counter_picks(counters: list[dict[str, Any]], unavailable: set[int], owned: set[int] | None,
                         mastery: dict[int, int]) -> list[dict[str, Any]]:
    """Meilleurs picks disponibles contre l'ennemi : assez de parties, winrate > 50 %, champion jouable."""
    mains = set(sorted(mastery, key=mastery.get, reverse=True)[:MAIN_MASTERY_RANK])
    picks = [
        {**c, "mastery": mastery.get(c["championId"], 0), "main": c["championId"] in mains}
        for c in counters
        if c["games"] >= COUNTER_MIN_GAMES and c["winrate"] > 0.5 and c["championId"] not in unavailable
        and (owned is None or c["championId"] in owned)
    ]
    return sorted(picks, key=lambda c: c["winrate"], reverse=True)[:COUNTER_MAX]


class Controller:
    def __init__(self, cfg: Config, demo: bool = False, demo_builds: bool = False) -> None:
        self.cfg = cfg
        self.demo = demo
        self.demo_builds = demo_builds  # vrai client, builds d'exemple
        self.cache = BuildCache(cfg.cache_dir)
        self.champions: ChampionCatalog = load_champion_catalog(cfg.cache_dir) or ChampionCatalog()
        self.prefs = Prefs(cfg.cache_dir / "ui_prefs.json", cfg.champselect.flash_key)
        self.lcu: Lcu | None = None
        self.sockets: set[web.WebSocketResponse] = set()
        self.build: Build | None = None  # build affiché : global + propositions contre l'adversaire
        self._base: Build | None = None  # build global du champion/rôle
        self._matchup: tuple[tuple[str, str, str], list[dict[str, Any]]] | None = None
        self._opponent_override: int | None = None  # adversaire choisi à la main dans l'UI
        self._manual = False  # l'utilisateur a choisi un build : plus d'application automatique
        self._apply_task: asyncio.Task[None] | None = None
        self._applied_for: tuple[Any, ...] | None = None
        self.live = LiveBuilds(cfg, self.cache) if cfg.opgg.live and not (demo or demo_builds) else None
        self._live_dirty = asyncio.Event()
        self._live_roles: dict[str, list[str]] = {}
        self._preview: int | None = None  # champion proposé cliqué avant le pick (aperçu)
        self._pool: tuple[set[int] | None, dict[int, int]] | None = None  # (possédés, maîtrises) du joueur
        self._counters: tuple[tuple[str, str], list[dict[str, Any]]] | None = None
        self._team_task: asyncio.Task[None] | None = None
        self.state: dict[str, Any] = {
            "connection": "demo" if demo else "waiting",
            "phase": "None",
            "champSelect": None,
            "current": None,
            "selection": {"variant": 0, "spells": None},
            "applied": None,
            "busy": False,
            "settings": self._settings(),
            "lastPrefetch": self._last_prefetch(),
            "toast": None,
            "notice": None,  # message d'état global (ex : téléchargement en cours)
            "counterPicks": None,  # bons picks contre l'adversaire de lane, avant le pick
            "banSuggestions": None,  # pendant la phase de bans : menaces pour mon champion ou tier list
            # Mise à jour (Réglages) : idle | checking | uptodate | available | downloading | installing | error
            "update": {"status": "idle", "current": __version__, "installable": updater.mode() is not None, "mode": updater.mode()},
            "banPick": None,  # champion choisi pour mon ban depuis l'appli : {championId, locked}
            "team": None,  # pendant la partie : {status, players: [...]} (mon équipe, profils OP.GG)
        }

    # ------------------------------------------------------------------ état

    def _settings(self) -> dict[str, Any]:
        keys = ("autoApply", "turnAlert", "alertFocus", "volume", "sounds", "theme", "flashKey")
        return {k: self.prefs.data[k] for k in keys}

    def _last_prefetch(self) -> str | None:
        dates = [b.fetched_at for b in self.cache.all_builds()]
        return max(dates) if dates else None

    def applier(self) -> BuildApplier:
        settings = dataclasses.replace(self.cfg.champselect, flash_key=self.prefs.data["flashKey"])
        return BuildApplier(settings)

    async def broadcast(self) -> None:
        msg = json.dumps({"type": "state", "state": self.state}, ensure_ascii=False)
        for ws in list(self.sockets):
            try:
                await ws.send_str(msg)
            except ConnectionError:
                self.sockets.discard(ws)

    async def toast(self, kind: str, text: str) -> None:
        self.state["toast"] = {"kind": kind, "text": text, "id": time.time()}
        await self.broadcast()

    # ------------------------------------------------------------------ session

    def _summarize_session(self, s: dict[str, Any]) -> dict[str, Any]:
        cell = s.get("localPlayerCellId")
        allies = [
            {
                "cell": p.get("cellId"),
                "championId": p.get("championId") or 0,
                "intent": p.get("championPickIntent") or 0,
                "position": (p.get("assignedPosition") or "").lower(),
                "isMe": p.get("cellId") == cell,
                "spells": [p.get("spell1Id"), p.get("spell2Id")],
            }
            for p in s.get("myTeam", [])
        ]
        enemies = [{"championId": p.get("championId") or 0} for p in s.get("theirTeam", [])]
        bans = []
        mine = []
        for group in s.get("actions", []):
            for a in group:
                if a.get("type") == "ban" and a.get("completed") and a.get("championId"):
                    bans.append({"championId": a["championId"], "ally": a.get("isAllyAction", False)})
                if a.get("actorCellId") == cell and a.get("type") in ("ban", "pick"):
                    mine.append(a)
        timer = s.get("timer") or {}
        phase = str(timer.get("phase", ""))
        # Pendant le prépick (PLANNING), le client marque déjà les bans « en cours » : seule la phase
        # BAN_PICK du minuteur dit qu'une action est réellement à faire.
        drafting = phase == "BAN_PICK"
        current = next((a for a in mine if a.get("isInProgress") and not a.get("completed")), None) if drafting else None
        running = {a.get("type") for group in s.get("actions", []) for a in group
                   if a.get("isInProgress") and not a.get("completed")} if drafting else set()
        # Étape globale (sons de phase, bandeau) : bans, picks, sinon la phase (planning, finalization...).
        stage = next((t for t in ("ban", "pick") if t in running), phase.lower())
        left = timer.get("adjustedTimeLeftInPhase") or 0
        return {
            "allies": allies,
            "enemies": enemies,
            "bans": bans,
            # Action en cours du joueur local (l'UI alerte quand l'id change) ; ban encore à faire ?
            "turn": {"id": current.get("id"), "type": current["type"]} if current else None,
            "banPending": any(a["type"] == "ban" and not a.get("completed") for a in mine),
            "stage": stage,
            "timer": {"phase": timer.get("phase", ""), "endsAt": time.time() * 1000 + left if left else None,
                      "total": timer.get("totalTimeInPhase") or None},  # ms, pour la barre de progression
        }

    async def on_session(self, s: dict[str, Any] | None) -> None:
        if not s:
            self.state["champSelect"] = None
            self.state["current"] = None
            self.state["applied"] = None
            self.build, self._base, self._applied_for = None, None, None
            self._matchup, self._opponent_override, self._manual = None, None, False
            self._preview, self._pool = None, None
            self.state["counterPicks"] = None
            self.state["banSuggestions"] = None
            self.state["banPick"] = None
            await self.broadcast()
            return
        summary = self._summarize_session(s)
        self.state["champSelect"] = summary
        me = next((a for a in summary["allies"] if a["isMe"]), None)
        champion_id = (me or {}).get("championId") or 0
        if champion_id:
            self._preview = None
        shown_id = champion_id or (me or {}).get("intent") or self._preview or 0
        position = (me or {}).get("position", "")
        current = self.state["current"]
        if shown_id and (not current or current["championId"] != shown_id or current.get("assigned") != position):
            self.load_build(shown_id, position)
        elif not shown_id:
            self.state["current"] = None
            self.build = None
        if self.state["current"]:
            self.state["current"]["locked"] = bool(champion_id)
            self.state["current"]["preview"] = bool(self._preview) and shown_id == self._preview
        await self.broadcast()
        if self.live:
            self._live_dirty.set()  # l'application automatique attend OP.GG (live_worker)
        else:
            self._maybe_auto_apply()

    def load_build(self, champion_id: int, position: str, role: str | None = None) -> None:
        champ = self.champions.by_key.get(champion_id)
        build = None
        if champ:
            build = self.cache.load(champ, role) if role else find_build(self.cache, champ, position)
        cached_roles = {b.role for b in self.cache.builds_for_champion(champ)} if champ else set()
        roles = sorted(cached_roles | set(self._live_roles.get(champ or "", [])), key=ROLES.index)
        self._base = build
        self.state["current"] = {
            "championId": champion_id,
            "champion": champ,
            "assigned": position,
            "role": build.role if build else (role or position),
            "roles": roles,
            "locked": False,
            "fetching": False,
            "opponent": None,
            "build": None,
        }
        self._compose()

    def _matchup_variants(self) -> list[dict[str, Any]]:
        cur = self.state["current"]
        if not cur or not self._matchup or self._matchup[0][:2] != (cur["champion"], cur["role"]):
            return []
        return self._matchup[1]

    def _compose(self) -> None:
        """Build affiché = propositions contre l'adversaire (s'il y en a) puis build global."""
        cur = self.state["current"]
        base, extra = self._base, self._matchup_variants()
        build = base
        if base and extra:
            pairs = {tuple(sorted(o["spells"])) for o in base.spell_options}
            options = [{"spells": v["spells"], **{k: v["details"]["spells"][k] for k in ("games", "winrate", "pick_rate")},
                        "most_played": False} for v in extra[:1] if tuple(sorted(v["spells"])) not in pairs]
            build = dataclasses.replace(base, variants=[*extra, *(base.variants or [_flat_variant(base)])],
                                        spell_options=[*options, *base.spell_options])
        self.build = build
        if cur is not None:
            cur["build"] = build.to_dict() if build else None
        if extra and not self._manual:
            variant = 0
        else:
            preferred = self.prefs.data["preferred"].get(f"{base.champion}_{base.role}", 0) if base else 0
            variant = (preferred + len(extra)) if base and preferred < max(1, len(base.variants)) else 0
        self.state["selection"] = {"variant": variant, "spells": None}

    def _apply_key(self) -> tuple[Any, ...] | None:
        cur = self.state["current"]
        if not cur or not self.build:
            return None
        return (cur["championId"], self.build.role, self._matchup[0][2] if self._matchup_variants() else None)

    def _maybe_auto_apply(self) -> None:
        cur = self.state["current"]
        if (cur and cur.get("locked") and self.build and self.prefs.data["autoApply"] and not self._manual
                and self._applied_for != self._apply_key()):
            self._schedule_apply(cur["championId"])

    def _schedule_apply(self, champion_id: int) -> None:
        if self._apply_task and not self._apply_task.done():
            self._apply_task.cancel()

        async def later() -> None:
            await asyncio.sleep(APPLY_DEBOUNCE)
            current = self.state["current"]
            if current and current["championId"] == champion_id and current["locked"]:
                await self.apply(self.state["selection"]["variant"], auto=True)

        self._apply_task = asyncio.create_task(later())

    # ------------------------------------------------------------------ actions

    async def apply(self, variant: int, auto: bool = False, spells_option: int | None = None) -> dict[str, Any]:
        if not self.build:
            return {"ok": False, "error": "Aucun build chargé"}
        if not self.lcu or not self.state["champSelect"]:
            return {"ok": False, "error": "Disponible uniquement pendant la sélection des champions"}
        variant = max(0, min(variant, max(0, len(self.build.variants) - 1)))
        build = self.build.variant(variant)
        if spells_option is not None and self.build.spell_options:
            build = dataclasses.replace(build, spells=list(self.build.spell_options[spells_option]["spells"]))
        self.state["busy"] = True
        self.state["selection"] = {"variant": variant, "spells": spells_option}
        await self.broadcast()
        steps = await self.applier().apply(self.lcu, build)
        self._applied_for = self._apply_key()
        self.state["busy"] = False
        self.state["applied"] = {"variant": variant, "spells": spells_option, "steps": steps, "auto": auto, "at": time.time()}
        if not auto:
            self._manual = True
            extra = len(self._matchup_variants())
            if variant >= extra:  # préférence mémorisée seulement parmi les builds globaux
                self.prefs.data["preferred"][f"{self.build.champion}_{self.build.role}"] = variant - extra
                self.prefs.save()
        ok = all(steps.values())
        await self.toast("ok" if ok else "warn", ("Build appliqué" if ok else "Build appliqué en partie") + (" automatiquement" if auto else ""))
        return {"ok": ok, "steps": steps}

    async def apply_spells(self, option: int) -> dict[str, Any]:
        if not self.build or not self.lcu or not self.state["champSelect"]:
            return {"ok": False, "error": "Disponible uniquement pendant la sélection des champions"}
        pair = self.build.spell_options[option]["spells"]
        build = dataclasses.replace(self.build, spells=list(pair))
        if not await self.applier().apply_spells(self.lcu, build):
            await self.toast("warn", "Le client a refusé le changement de sorts")
            return {"ok": True}
        self.state["selection"]["spells"] = option
        if self.state["applied"]:
            self.state["applied"]["spells"] = option
        await self.toast("ok", "Sorts d'invocateur mis à jour")
        return {"ok": True}

    # ------------------------------------------------------------------ ban depuis l'appli

    async def ban(self, champion_id: int, lock: bool) -> dict[str, Any]:
        """Survole (puis verrouille si `lock`) un champion dans MON ban en cours ; jamais une action de pick."""
        cs = self.state["champSelect"] or {}
        turn = cs.get("turn")
        if not self.lcu or not turn or turn.get("type") != "ban" or turn.get("id") is None:
            return {"ok": False, "error": "Ce n'est pas ton tour de bannir"}
        if champion_id in {b["championId"] for b in cs.get("bans", [])} | {e["championId"] for e in cs.get("enemies", [])}:
            return {"ok": False, "error": "Ce champion est déjà banni ou choisi"}
        if champion_id in {a["championId"] or a["intent"] for a in cs.get("allies", []) if not a["isMe"]}:
            return {"ok": False, "error": "Un allié veut jouer ce champion"}
        path = f"/lol-champ-select/v1/session/actions/{turn['id']}"
        status, data = await self.lcu.request("PATCH", path, {"championId": champion_id})
        if status not in (200, 204):
            log.warning("Ban : survol refusé (%s) %s", status, data)
            return {"ok": False, "error": "Le client a refusé ce ban"}
        if lock:
            status, data = await self.lcu.request("POST", path + "/complete")
            if status not in (200, 204):
                log.warning("Ban : verrouillage refusé (%s) %s", status, data)
                return {"ok": False, "error": "Le client a refusé de verrouiller le ban"}
        self.state["banPick"] = {"championId": champion_id, "locked": lock, "action": turn["id"]}
        name = self.champions.by_key.get(champion_id, str(champion_id))
        await self.toast("ok", f"{name} banni" if lock else f"{name} sélectionné pour ton ban")
        return {"ok": True}

    # ------------------------------------------------------------------ mon équipe pendant la partie

    def on_phase(self, phase: str) -> None:
        """Partie en cours : charge les profils de mon équipe ; sinon on les oublie."""
        if phase == "InProgress":
            if self.live and (not self._team_task or self._team_task.done()) and not self.state["team"]:
                self._team_task = asyncio.create_task(self._load_team())
        elif phase in ("None", "Lobby", "Matchmaking", "ReadyCheck", "ChampSelect"):
            if self._team_task and not self._team_task.done():
                self._team_task.cancel()
            self.state["team"] = None

    async def _load_team(self) -> None:
        assert self.live
        team: dict[str, Any] = {"status": "loading", "players": []}
        self.state["team"] = team
        await self.broadcast()
        region = "EUW"
        if self.lcu:
            try:
                _, loc = await self.lcu.request("GET", "/riotclient/region-locale")
                region = str((loc or {}).get("region") or region).upper().rstrip("0123456789") or region
            except Exception:
                pass
        for _ in range(120):  # la Live Client Data API répond une fois la partie chargée
            try:
                me, everyone = await asyncio.to_thread(players.live_players)
                if everyone:
                    break
            except Exception:
                pass
            await asyncio.sleep(5)
        else:
            team["status"] = "unavailable"
            await self.broadcast()
            return
        mine = next((p for p in everyone if "#".join(players.riot_id(p)).lower() == me.lower()
                     or str(p.get("riotId") or p.get("summonerName") or "").lower() == me.lower()), None)
        side = (mine or {}).get("team")
        allies = [p for p in everyone if p.get("team") == side] if side else everyone[:5]
        for p in allies:
            name, tag = players.riot_id(p)
            champ = players.champion_id(p)
            key = next((k for k, v in self.champions.by_key.items() if v == champ), None)
            team["players"].append({"riotId": f"{name}#{tag}", "isMe": p is mine, "championId": key,
                                    "position": str(p.get("position") or "").lower(), "status": "loading"})
        await self.broadcast()
        for entry, p in zip(team["players"], allies):
            name, tag = players.riot_id(p)
            try:
                profile = await asyncio.to_thread(self.live.player_profile, name, tag, region)
                champ_name = self.champions.names.get(players.champion_id(p), players.champion_id(p))
                entry.update(players.player_card(profile, entry["championId"], champ_name), status="ok")
            except Exception as exc:
                log.warning("Profil OP.GG de %s#%s indisponible : %s", name, tag, exc)
                entry["status"] = "error"
            await self.broadcast()
        team["status"] = "ok"
        await self.broadcast()

    # ------------------------------------------------------------------ mise à jour (releases GitHub)

    async def check_update(self) -> None:
        upd = self.state["update"]
        upd.update(status="checking", error=None)
        await self.broadcast()
        try:
            info = await asyncio.to_thread(updater.check)
        except updater.UpdateError as exc:
            log.warning("Mise à jour : %s", exc)
            upd.update(status="error", error=str(exc))
        else:
            upd.update(info, status="available" if info["newer"] else "uptodate")
        await self.broadcast()

    async def install_update(self) -> dict[str, Any]:
        upd = self.state["update"]
        if upd.get("status") != "available":
            return {"ok": False, "error": "Aucune mise à jour à installer"}
        mode = updater.mode()
        if mode is None:
            return {"ok": False, "error": "Installation depuis les sources : télécharge la release sur GitHub"}
        upd["status"] = "downloading"
        await self.broadcast()
        try:
            if mode == "installer":
                dest = Path(tempfile.gettempdir()) / "lolbuilds-update"
                setup = await asyncio.to_thread(updater.download_installer, dict(upd), dest)
                await asyncio.to_thread(updater.launch_installer, setup)
            else:
                exe = Path(sys.executable)
                new = await asyncio.to_thread(updater.download_portable, dict(upd), exe.parent)
                await asyncio.to_thread(updater.swap_exe, exe, new)
                await asyncio.to_thread(updater.relaunch, exe)
        except (updater.UpdateError, OSError) as exc:
            log.error("Mise à jour impossible : %s", exc)
            upd.update(status="error", error=str(exc))
            await self.broadcast()
            return {"ok": False, "error": str(exc)}
        upd["status"] = "installing"
        await self.broadcast()
        # L'installateur (ou le nouvel exe) remplace puis relance l'appli : on lui laisse la place.
        asyncio.get_running_loop().call_later(1.5, os._exit, 0)
        return {"ok": True}

    # ------------------------------------------------------------------ OP.GG au moment du pick

    async def refresh_static(self) -> None:
        """Données du jeu à jour au démarrage (le prefetch nocturne est optionnel)."""
        assert self.live
        if await asyncio.to_thread(self.live.refresh_static):
            self.champions = load_champion_catalog(self.cfg.cache_dir) or self.champions
            self.state["notice"] = "reload"
            await self.broadcast()
            self.state["notice"] = None
            await self.broadcast()

    async def live_worker(self) -> None:
        """Traite les mises à jour du champ select une par une ; jamais annulé en plein téléchargement."""
        while True:
            await self._live_dirty.wait()
            await asyncio.sleep(LIVE_DEBOUNCE)
            self._live_dirty.clear()
            try:
                await self._live_refresh()
            except Exception:
                log.exception("Récupération OP.GG en direct")

    async def _lane_opponent(self, role: str) -> tuple[str | None, bool]:
        """(ennemi de lane, deviné ?) : choix manuel, sinon déduit des rôles OP.GG des ennemis."""
        assert self.live
        override = self.champions.by_key.get(self._opponent_override or 0)
        if override:
            return override, False
        cs = self.state["champSelect"] or {}
        enemies = [self.champions.by_key.get(e["championId"]) for e in cs.get("enemies", []) if e["championId"]]
        return await asyncio.to_thread(self.live.guess_opponent, [e for e in enemies if e], role), True

    async def _load_pool(self) -> tuple[set[int] | None, dict[int, int]]:
        """Champions jouables et maîtrises du joueur (lus une fois par champ select, via le client)."""
        if self._pool is None and self.lcu:
            owned: set[int] | None = None
            mastery: dict[int, int] = {}
            try:
                status, data = await self.lcu.request("GET", "/lol-champions/v1/owned-champions-minimal")
                if status == 200 and isinstance(data, list):
                    owned = {c["id"] for c in data if c.get("active", True)
                             and ((c.get("ownership") or {}).get("owned") or c.get("freeToPlay"))}
                status, data = await self.lcu.request("GET", "/lol-champion-mastery/v1/local-player/champion-mastery")
                if status == 200 and isinstance(data, list):
                    mastery = {m["championId"]: m.get("championPoints", 0) for m in data}
            except Exception as exc:
                log.warning("Champions possédés / maîtrises illisibles : %s", exc)
            self._pool = (owned or None, mastery)
        return self._pool or (None, {})

    async def _refresh_counters(self) -> None:
        """Avant le pick : meilleurs champions jouables contre l'adversaire de lane."""
        assert self.live
        cs = self.state["champSelect"]
        me = next((a for a in (cs or {}).get("allies", []) if a["isMe"]), None)
        role = (me or {}).get("position", "")
        if not cs or not self.cfg.opgg.matchup or not me or me["championId"] or role not in ROLES:
            self.state["counterPicks"] = None
            return
        enemy, guessed = await self._lane_opponent(role)
        if not enemy:
            self.state["counterPicks"] = None
            return
        info = {"enemyId": self.champions.key_of(enemy), "guessed": guessed, "role": role, "picks": []}
        if not self._counters or self._counters[0] != (enemy, role):
            self.state["counterPicks"] = {**info, "status": "loading"}
            await self.broadcast()
            self._counters = ((enemy, role), await asyncio.to_thread(self.live.counters, enemy, role))
        owned, mastery = await self._load_pool()
        taken = {a["championId"] or a["intent"] for a in cs["allies"] if not a["isMe"]}
        taken |= {e["championId"] for e in cs["enemies"]} | {b["championId"] for b in cs["bans"]}
        picks = select_counter_picks(self._counters[1], taken - {0}, owned, mastery)
        self.state["counterPicks"] = {**info, "status": "ok" if picks else "empty", "picks": picks}

    async def _refresh_bans(self) -> None:
        """Ban à faire : champions qui battent mon champion déclaré, sinon les plus forts de ma lane."""
        assert self.live
        cs = self.state["champSelect"]
        me = next((a for a in (cs or {}).get("allies", []) if a["isMe"]), None)
        role = (me or {}).get("position", "")
        if not cs or not cs.get("banPending") or not me or role not in ROLES:
            self.state["banSuggestions"] = None
            return
        mine = self.champions.by_key.get(me["championId"] or me["intent"] or self._preview or 0)
        taken = {a["championId"] or a["intent"] for a in cs["allies"]}  # jamais bannir le pick d'un allié
        taken |= {e["championId"] for e in cs["enemies"]} | {b["championId"] for b in cs["bans"]}
        taken.discard(0)
        if mine:
            info = {"mode": "threats", "championId": self.champions.key_of(mine), "role": role}
            ranked = [c for c in await asyncio.to_thread(self.live.counters, mine, role)
                      if c["games"] >= COUNTER_MIN_GAMES and c["winrate"] > 0.5]
        else:
            info = {"mode": "meta", "championId": None, "role": role}
            ranked = await asyncio.to_thread(self.live.tier_list, role)
        picks = [c for c in ranked if c["championId"] not in taken][:BAN_MAX]
        self.state["banSuggestions"] = {**info, "status": "ok" if picks else "empty", "picks": picks}

    async def _live_refresh(self) -> None:
        assert self.live
        try:
            await self._refresh_counters()
        except Exception:
            log.exception("Bons picks OP.GG")
        try:
            await self._refresh_bans()
        except Exception:
            log.exception("Bans suggérés OP.GG")
        cur = self.state["current"]
        if not cur or not cur["champion"]:
            await self.broadcast()
            return
        champ = cur["champion"]

        def still(role: str) -> bool:
            return self.state["current"] is cur and cur["role"] == role

        # 1) rôle (rôle assigné, sinon rôle principal du champion) et onglets de rôles
        role = cur["role"] if cur["role"] in ROLES else None
        try:
            role = role or await asyncio.to_thread(self.live.role_for, champ, cur["assigned"])
            if champ not in self._live_roles:
                self._live_roles[champ] = await asyncio.to_thread(self.live.champion_roles, champ)
        except Exception as exc:
            log.warning("OP.GG : rôles de %s indisponibles (%s)", champ, exc)
        if not role or self.state["current"] is not cur:
            return
        cur["role"] = role
        cur["roles"] = sorted(set(cur["roles"]) | set(self._live_roles.get(champ, [])) | {role}, key=ROLES.index)

        # 2) build global du jour
        if not self.cache.is_fresh(champ, role, date.today()):
            cur["fetching"] = True
            await self.broadcast()
            build = await asyncio.to_thread(self.live.global_build, champ, role)
            if not still(role):
                return
            cur["fetching"] = False
            self._base = build or self._base
            if build and build.fetched_at:
                self.state["lastPrefetch"] = max(build.fetched_at, self.state["lastPrefetch"] or "")
            self._compose()
            await self.broadcast()

        # 3) propositions contre l'adversaire de lane
        cs = self.state["champSelect"]
        if self.cfg.opgg.matchup and cs:
            opponent, guessed = await self._lane_opponent(role)
            if not still(role):
                return
            if not opponent:
                cur["opponent"] = None
                if self._matchup:
                    self._matchup = None
                    self._compose()
            else:
                key = (champ, role, opponent)
                cur["opponent"] = {"championId": self.champions.key_of(opponent), "guessed": guessed,
                                   "status": "ok" if self._matchup and self._matchup[0] == key else "loading"}
                if cur["opponent"]["status"] == "loading":
                    await self.broadcast()
                    variants = await asyncio.to_thread(self.live.matchup, champ, role, opponent)
                    if not still(role):
                        return
                    self._matchup = (key, variants)
                    cur["opponent"]["status"] = "ok" if variants else "empty"
                    self._compose()
        await self.broadcast()
        self._maybe_auto_apply()

    # ------------------------------------------------------------------ boucle LCU

    async def lcu_loop(self) -> None:
        while True:
            self.state["connection"] = "waiting"
            await self.broadcast()
            creds = await wait_for_client()
            conn = LcuConnection(creds)
            self.lcu = conn
            try:
                await self._wait_ready(conn)
                self.state["connection"] = "connected"
                _, phase = await conn.request("GET", "/lol-gameflow/v1/gameflow-phase")
                self.state["phase"] = phase or "None"
                self.on_phase(self.state["phase"])
                status, session = await conn.request("GET", "/lol-champ-select/v1/session")
                await self.on_session(session if status == 200 else None)
                log.info("Connecté au client League (port %s)", creds.port)
                async for event in conn.events():
                    await self.on_event(event)
            except Exception as exc:
                log.info("Connexion au client perdue : %s", exc)
            finally:
                self.lcu = None
                await conn.close()
                await self.on_session(None)
            await asyncio.sleep(2)

    async def _wait_ready(self, conn: LcuConnection) -> None:
        for _ in range(60):
            try:
                status, _ = await conn.request("GET", "/lol-summoner/v1/current-summoner")
                if status == 200:
                    return
            except Exception:
                pass
            await asyncio.sleep(1)

    async def on_event(self, event: dict[str, Any]) -> None:
        uri = event.get("uri", "")
        if uri == "/lol-gameflow/v1/gameflow-phase":
            self.state["phase"] = event.get("data") or "None"
            self.on_phase(self.state["phase"])
            await self.broadcast()
        elif uri == "/lol-champ-select/v1/session":
            deleted = (event.get("eventType") or "").lower() == "delete"
            await self.on_session(None if deleted else event.get("data"))


# ---------------------------------------------------------------------- routes HTTP

def make_app(ctrl: Controller) -> web.Application:
    app = web.Application()
    routes = web.RouteTableDef()

    @routes.get("/")
    async def index(_req: web.Request) -> web.FileResponse:
        return web.FileResponse(UI_DIR / "index.html", headers={"Cache-Control": "no-store"})

    @routes.get("/ws")
    async def ws_handler(req: web.Request) -> web.WebSocketResponse:
        ws = web.WebSocketResponse(heartbeat=30)
        await ws.prepare(req)
        ctrl.sockets.add(ws)
        await ws.send_str(json.dumps({"type": "state", "state": ctrl.state}, ensure_ascii=False))
        async for msg in ws:
            if msg.type == WSMsgType.ERROR:
                break
        ctrl.sockets.discard(ws)
        return ws

    @routes.get("/api/static")
    async def static_data(_req: web.Request) -> web.Response:
        path = ctrl.cfg.cache_dir / "ui_static.json"
        if not path.exists():
            return web.json_response({"error": "Lancez le prefetch pour télécharger les données du jeu."}, status=404)
        return web.FileResponse(path)

    @routes.get("/api/library")
    async def library(_req: web.Request) -> web.Response:
        rows = []
        for b in ctrl.cache.all_builds():
            if b:
                rows.append({
                    "champion": b.champion, "championId": b.champion_id, "role": b.role, "roleLabel": ROLE_LABELS.get(b.role, b.role),
                    "winrate": b.winrate, "games": b.sample_size, "patch": b.patch, "fetchedAt": b.fetched_at,
                    "variants": len(b.variants) or 1, "primaryStyle": b.primaryStyle, "keystone": b.perks[0],
                })
        return web.json_response(rows)

    @routes.post("/api/open")
    async def open_build(req: web.Request) -> web.Response:
        """Affiche un build de la bibliothèque (ou un autre rôle du champion courant)."""
        body = await req.json()
        champion_id = int(body["championId"])
        cs = ctrl.state["champSelect"]
        me = next((a for a in (cs or {}).get("allies", []) if a["isMe"]), None)
        position = me["position"] if me and (me["championId"] or me["intent"]) == champion_id else ""
        ctrl.load_build(champion_id, position, body.get("role"))
        if me and (me["championId"] or 0) == champion_id:
            ctrl.state["current"]["locked"] = True
        await ctrl.broadcast()
        return web.json_response({"ok": True})

    @routes.post("/api/select")
    async def select(req: web.Request) -> web.Response:
        body = await req.json()
        variant = int(body.get("variant", 0))
        if not body.get("apply", True):
            ctrl.state["selection"]["variant"] = variant
            await ctrl.broadcast()
            return web.json_response({"ok": True})
        result = await ctrl.apply(variant, spells_option=ctrl.state["selection"].get("spells"))
        if not result["ok"] and "error" in result:
            ctrl.state["selection"]["variant"] = variant
            await ctrl.toast("info", result["error"])
        return web.json_response(result)

    @routes.post("/api/opponent")
    async def opponent(req: web.Request) -> web.Response:
        """Adversaire de lane choisi à la main (0 = revenir à la détection automatique)."""
        champion_id = int((await req.json()).get("championId") or 0)
        if ctrl._opponent_override == champion_id:
            champion_id = 0  # second clic sur l'adversaire choisi : on revient à la détection
        ctrl._opponent_override = champion_id or None
        ctrl._live_dirty.set()
        return web.json_response({"ok": True})

    @routes.post("/api/preview")
    async def preview(req: web.Request) -> web.Response:
        """Aperçu d'un bon pick avant de choisir (second clic : on referme)."""
        champion_id = int((await req.json()).get("championId") or 0)
        cs = ctrl.state["champSelect"]
        me = next((a for a in (cs or {}).get("allies", []) if a["isMe"]), None)
        if not me or me["championId"] or me["intent"]:
            return web.json_response({"ok": False, "error": "Aperçu disponible avant de survoler ou choisir un champion"})
        ctrl._preview = None if ctrl._preview == champion_id else champion_id
        if ctrl._preview:
            ctrl.load_build(ctrl._preview, me["position"])
            ctrl.state["current"]["preview"] = True
        else:
            ctrl.state["current"], ctrl.build = None, None
        await ctrl.broadcast()
        if ctrl.live:
            ctrl._live_dirty.set()
        return web.json_response({"ok": True})

    @routes.post("/api/ban")
    async def ban(req: web.Request) -> web.Response:
        body = await req.json()
        result = await ctrl.ban(int(body.get("championId") or 0), bool(body.get("lock")))
        if not result["ok"]:
            await ctrl.toast("warn", result["error"])
        return web.json_response(result)

    @routes.post("/api/update/check")
    async def update_check(_req: web.Request) -> web.Response:
        await ctrl.check_update()
        return web.json_response({"ok": True})

    @routes.post("/api/update/install")
    async def update_install(_req: web.Request) -> web.Response:
        result = await ctrl.install_update()
        if not result["ok"]:
            await ctrl.toast("warn", result["error"])
        return web.json_response(result)

    @routes.post("/api/spells")
    async def spells(req: web.Request) -> web.Response:
        result = await ctrl.apply_spells(int((await req.json())["option"]))
        if not result["ok"]:
            await ctrl.toast("info", result["error"])
        return web.json_response(result)

    @routes.post("/api/settings")
    async def settings(req: web.Request) -> web.Response:
        ctrl.prefs.update(await req.json())
        ctrl.prefs.save()
        ctrl.state["settings"] = ctrl._settings()
        await ctrl.broadcast()
        return web.json_response({"ok": True})

    async def no_cache_ui(req: web.Request, resp: web.StreamResponse) -> None:
        # Sinon la fenêtre garde l'ancien CSS/JS après une mise à jour de l'outil.
        if req.path.startswith("/ui/"):
            resp.headers["Cache-Control"] = "no-cache"

    app.on_response_prepare.append(no_cache_ui)
    app.add_routes(routes)
    app.router.add_static("/ui/", UI_DIR)
    assets = ctrl.cfg.cache_dir / "assets"
    assets.mkdir(parents=True, exist_ok=True)
    app.router.add_static("/assets/", assets)
    return app


async def start_server(ctrl: Controller, host: str, port: int) -> web.AppRunner:
    runner = web.AppRunner(make_app(ctrl), access_log=None)
    await runner.setup()
    await web.TCPSite(runner, host, port).start()
    # Visuels des thèmes de région (une seule fois ; sans réseau, les thèmes gardent couleurs et ornements).
    asyncio.create_task(asyncio.to_thread(ensure_realm_assets, ctrl.cfg.cache_dir))
    if not ctrl.demo:  # une nouvelle version est signalée dans les Réglages dès le démarrage
        asyncio.create_task(ctrl.check_update())
    if ctrl.demo:
        from .demo import launch_demo

        launch_demo(ctrl)
    elif ctrl.demo_builds:
        from .demo import launch_demo_builds

        launch_demo_builds(ctrl)
    else:
        asyncio.create_task(ctrl.lcu_loop())
        if ctrl.live:
            asyncio.create_task(ctrl.refresh_static())
            asyncio.create_task(ctrl.live_worker())
    return runner


__all__ = ["Controller", "DryRunLcu", "make_app", "start_server"]
