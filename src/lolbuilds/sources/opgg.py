"""Source OP.GG : statistiques via le serveur MCP officiel d'OP.GG (https://mcp-api.op.gg/mcp).

Un appel `lol_list_lane_meta_champions` donne les rôles joués par chaque champion,
puis un appel `lol_get_champion_analysis` par champion/rôle donne runes, sorts et items
(échantillon de plusieurs centaines de milliers de parties classées, Émeraude+ par défaut).

Les réponses ne sont pas du JSON mais un format compact :

    class Runes: id,primary_page_id,...
    LolGetChampionAnalysis("Fizz",Data(Summary(...),Runes(8112,8100,[...]),...))

`parse_toon` le relit avec `ast` (aucune évaluation de code).
"""

from __future__ import annotations

import ast
import functools
import json
import logging
import re
import time
from collections import Counter
from datetime import date
from collections.abc import Iterable
from typing import Any

import httpx

from .. import ARAM, AUTO_PREFIX, ROLE_LABELS
from ..aggregation import CONSUMABLES
from ..config import OpggSettings
from ..ddragon import ChampionCatalog, ItemCatalog, StaticData
from ..models import Build, Target
from .base import BuildSource, BuildSourceError, InsufficientDataError

log = logging.getLogger(__name__)

MCP_URL = "https://mcp-api.op.gg/mcp"
PROTOCOL_VERSION = "2025-06-18"

# Rôle LCU <-> position OP.GG
TO_OPGG = {"top": "top", "jungle": "jungle", "middle": "mid", "bottom": "adc", "utility": "support"}
FROM_OPGG = {v: k for k, v in TO_OPGG.items()}

_STATS = "play,win,pick_rate"
ANALYSIS_FIELDS = [
    "data.summary.average_stats.{play,win_rate,pick_rate,tier}",
    "data.summary.positions[].name",
    "data.summary.positions[].stats.{play,win_rate,pick_rate,role_rate}",
    "data.runes.{primary_page_id,primary_rune_ids[],secondary_page_id,secondary_rune_ids[],stat_mod_ids[]," + _STATS + "}",
    "data.summoner_spells.{ids[]," + _STATS + "}",
    "data.starter_items.{ids[]," + _STATS + "}",
    "data.core_items.{ids[]," + _STATS + "}",
    "data.boots.{ids[]," + _STATS + "}",
    "data.fourth_items[].{ids[]," + _STATS + "}",
    "data.fifth_items[].{ids[]," + _STATS + "}",
    "data.sixth_items[].{ids[]," + _STATS + "}",
    "data.skills.{order[]," + _STATS + "}",
]
META_FIELDS = [f"data.positions.{p}[].{{champion,play,role_rate,win_rate}}" for p in TO_OPGG.values()]


# --------------------------------------------------------------------------- format de réponse

LITERALS = {"true": True, "false": False, "null": None}


def parse_toon(text: str) -> Any:
    """Relit la réponse compacte d'OP.GG en dicts/listes Python."""
    fields: dict[str, list[str]] = {}
    body: list[str] = []
    for line in text.splitlines():
        m = re.match(r"class (\w+): (.*)$", line)
        if m and not body:
            fields[m.group(1)] = [f.strip() for f in m.group(2).split(",")]
        elif line.strip():
            body.append(line)
    try:
        tree = ast.parse("\n".join(body), mode="eval").body
    except SyntaxError as exc:
        raise BuildSourceError(f"réponse OP.GG illisible : {text[:200]!r}") from exc

    def conv(node: ast.AST) -> Any:
        if isinstance(node, ast.Constant):
            return node.value
        if isinstance(node, ast.List | ast.Tuple):
            return [conv(e) for e in node.elts]
        if isinstance(node, ast.Name) and node.id in LITERALS:  # booléens / null écrits à la JSON
            return LITERALS[node.id]
        if isinstance(node, ast.UnaryOp) and isinstance(node.op, ast.USub):
            return -conv(node.operand)
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Name):
            names = fields.get(node.func.id, [])
            obj = dict(zip(names, (conv(a) for a in node.args)))
            obj.update({k.arg: conv(k.value) for k in node.keywords if k.arg})
            return obj
        raise BuildSourceError(f"réponse OP.GG inattendue : {ast.dump(node)[:120]}")

    return conv(tree)


def opgg_name(display_name: str) -> str:
    """"Cho'Gath" -> "CHO_GATH", "Nunu & Willump" -> "NUNU_WILLUMP"."""
    return re.sub(r"[^A-Za-z0-9]+", "_", display_name).strip("_").upper()


# --------------------------------------------------------------------------- client MCP

class OpggClient:
    """Client MCP minimal (HTTP « streamable ») avec espacement des requêtes et reprises."""

    def __init__(self, interval: float = 0.5, http: httpx.Client | None = None, url: str = MCP_URL) -> None:
        self.url = url
        self.interval = interval
        self.http = http or httpx.Client(timeout=60)
        self.session_id: str | None = None
        self._last = 0.0
        self._id = 0

    def close(self) -> None:
        self.http.close()

    def call(self, tool: str, arguments: dict[str, Any], retries: int = 3) -> Any:
        """Appelle un outil et renvoie sa réponse décodée ; lève BuildSourceError sinon."""
        for attempt in range(retries + 1):
            try:
                if self.session_id is None:
                    self._initialize()
                result = self._rpc("tools/call", {"name": tool, "arguments": arguments})
                content = result.get("content") or []
                text = "".join(str(c.get("text") or "") for c in content
                               if isinstance(c, dict) and c.get("type") == "text")
                if result.get("isError"):
                    raise BuildSourceError(f"{tool} : {text[:200]}")
                try:
                    return json.loads(text) if text.lstrip().startswith("{") else parse_toon(text)
                except (ValueError, TypeError, RecursionError, MemoryError) as exc:  # corps illisible
                    raise BuildSourceError(f"{tool} : réponse OP.GG illisible ({exc})") from exc
            except _Retry as exc:
                if attempt == retries:
                    raise BuildSourceError(f"OP.GG indisponible : {exc}") from exc
                self.session_id = None if exc.reset_session else self.session_id
                wait = 2 ** attempt * 2
                log.warning("OP.GG : %s, nouvel essai dans %d s", exc, wait)
                time.sleep(wait)
        raise AssertionError("unreachable")

    def _initialize(self) -> None:
        self.session_id = None
        resp = self._post({"jsonrpc": "2.0", "id": self._next_id(), "method": "initialize", "params": {
            "protocolVersion": PROTOCOL_VERSION, "capabilities": {},
            "clientInfo": {"name": "lolbuilds", "version": "0.1"},
        }})
        self._decode(resp)  # lève si l'initialisation a échoué : pas de session_id bidon gardé
        self.session_id = resp.headers.get("mcp-session-id")
        self._post({"jsonrpc": "2.0", "method": "notifications/initialized"})

    def _rpc(self, method: str, params: dict[str, Any]) -> dict[str, Any]:
        return self._decode(self._post({"jsonrpc": "2.0", "id": self._next_id(), "method": method, "params": params}))

    def _post(self, payload: dict[str, Any]) -> httpx.Response:
        delay = self._last + self.interval - time.monotonic()
        if delay > 0:
            time.sleep(delay)
        headers = {"Accept": "application/json, text/event-stream", "Content-Type": "application/json"}
        if self.session_id:
            headers["mcp-session-id"] = self.session_id
        try:
            resp = self.http.post(self.url, json=payload, headers=headers)
        except httpx.HTTPError as exc:
            raise _Retry(str(exc)) from exc
        finally:
            self._last = time.monotonic()
        if resp.status_code == 404 and self.session_id:  # seul 404 = session expirée ; un 400 est une vraie erreur
            raise _Retry(f"session expirée ({resp.status_code})", reset_session=True)
        if resp.status_code == 429 or resp.status_code >= 500:
            raise _Retry(f"HTTP {resp.status_code}")
        if resp.status_code >= 400:
            raise BuildSourceError(f"OP.GG HTTP {resp.status_code} : {resp.text[:200]}")
        return resp

    @staticmethod
    def _decode(resp: httpx.Response) -> dict[str, Any]:
        if resp.status_code == 202 or not resp.content:
            return {}
        text = resp.text
        if "text/event-stream" in resp.headers.get("content-type", ""):
            text = "\n".join(line[5:].strip() for line in text.splitlines() if line.startswith("data:"))
        try:
            msg = json.loads(text)
        except ValueError as exc:  # corps HTML, tronqué...
            raise BuildSourceError(f"OP.GG : réponse non JSON ({text[:100]!r})") from exc
        if not isinstance(msg, dict):
            raise BuildSourceError(f"OP.GG : réponse inattendue ({text[:100]!r})")
        if msg.get("error"):
            err = msg["error"]
            raise BuildSourceError(f"OP.GG : {err.get('message', err) if isinstance(err, dict) else err}")
        result = msg.get("result")
        if result is None:
            return {}
        if not isinstance(result, dict):
            raise BuildSourceError(f"OP.GG : résultat inattendu ({str(result)[:100]!r})")
        return result

    def _next_id(self) -> int:
        self._id += 1
        return self._id


class _Retry(Exception):
    def __init__(self, message: str, reset_session: bool = False) -> None:
        super().__init__(message)
        self.reset_session = reset_session


# --------------------------------------------------------------------------- conversion en Build

def lane_roles(meta: Any, champions: ChampionCatalog) -> dict[str, dict[str, float]]:
    """{champion DDragon: {rôle LCU: role_rate}} depuis `lol_list_lane_meta_champions`."""
    roles: dict[str, dict[str, float]] = {}
    positions = (meta.get("data") or {}).get("positions") or {}
    for pos, entries in positions.items():
        role = FROM_OPGG.get(pos)
        for e in entries or []:
            e = e or {}
            champ = champions.resolve(str(e.get("champion") or ""))
            if role and champ:
                roles.setdefault(champ, {})[role] = float(e.get("role_rate") or 0)
            elif role:
                log.debug("OP.GG : champion inconnu de Data Dragon %r", e.get("champion"))
    return roles


def _wr(stat: dict[str, Any]) -> float:
    play = stat.get("play") or 0
    return round((stat.get("win") or 0) / play, 4) if play else 0.0


def _title(label: str, stat: dict[str, Any]) -> str:
    return f"{label} ({_wr(stat):.0%} WR, {stat.get('play') or 0} parties)"


def _block(key: str, title: str, item_ids: Iterable[int]) -> dict[str, Any]:
    counts = Counter(item_ids)
    return {"key": key, "type": title, "items": [{"id": str(i), "count": c} for i, c in counts.items()]}


def _describe(stat: dict[str, Any] | None) -> dict[str, Any] | None:
    if not stat:
        return None
    return {"games": stat.get("play") or 0, "winrate": _wr(stat), "pick_rate": stat.get("pick_rate") or 0, "fallback": False}


def _variant(runes: dict[str, Any], spells: dict[str, Any], *, starter: dict[str, Any] | None,
             core: dict[str, Any] | None, boots: dict[str, Any] | None, situational: Iterable[int],
             skills: list[str], tags: list[str]) -> dict[str, Any]:
    """Une proposition de build au format du cache (runes, sorts, blocs d'items, stats)."""
    blocks = []
    if starter and starter.get("ids"):
        blocks.append(_block("starter", _title("Départ", starter), starter["ids"]))
    if core and core.get("ids"):
        blocks.append(_block("core", _title("Core", core), core["ids"]))
    if boots and boots.get("ids"):
        blocks.append(_block("boots", _title("Bottes", boots), boots["ids"]))
    excluded = set((core or {}).get("ids") or ()) | set((boots or {}).get("ids") or ())
    situ = list(dict.fromkeys(i for i in situational if i not in excluded))[:6]
    if situ:
        blocks.append(_block("situational", "Situationnel", situ))
    blocks.append(_block("consumables", "Consommables", CONSUMABLES))
    return {
        "primaryStyle": runes.get("primary_page_id"),
        "subStyle": runes.get("secondary_page_id"),
        "perks": [*(runes.get("primary_rune_ids") or []), *(runes.get("secondary_rune_ids") or []),
                  *(runes.get("stat_mod_ids") or [])],
        "spells": list(spells.get("ids") or [])[:2],
        "blocks": blocks,
        "games": runes.get("play") or 0,
        "winrate": _wr(runes),
        "pick_rate": runes.get("pick_rate") or 0,
        "tags": tags,
        "details": {
            "runes": _describe(runes),
            "spells": _describe(spells),
            "core": _describe(core),
            "boots": _describe(boots),
            "starter": _describe(starter),
            "skills": skills,
        },
    }


ARAM_MAP = 12  # Abîme hurlant : sans lui, l'item set n'apparaît pas dans la boutique d'une partie d'ARAM
SUMMONERS_RIFT_MAP = 11


def build_from_analysis(analysis: Any, *, champion: str, champion_id: int, role: str, patch: str,
                        source: str = "opgg") -> Build:
    """Convertit la réponse de `lol_get_champion_analysis` en `Build` (une proposition).

    `role` peut être `ARAM` : mêmes données, issues des stats d'ARAM, item set pour l'Abîme hurlant."""
    data = (analysis or {}).get("data") or {}
    runes = data.get("runes")
    spells = data.get("summoner_spells")
    if not runes or not spells:
        raise InsufficientDataError(f"{champion} {role} : pas de runes/sorts sur OP.GG")
    situational = [i for key in ("fourth_items", "fifth_items", "sixth_items")
                   for option in data.get(key) or [] for i in (option or {}).get("ids") or []]
    variant = _variant(runes, spells, starter=data.get("starter_items"), core=data.get("core_items"),
                       boots=data.get("boots"), situational=situational,
                       skills=(data.get("skills") or {}).get("order") or [],
                       tags=["Build ARAM"] if role == ARAM else ["Recommandé OP.GG"])
    # Stats du champion dans CE rôle ; `average_stats` couvre tous ses rôles confondus.
    summary_raw = data.get("summary") or {}
    summary = next(((p or {}).get("stats") or {} for p in summary_raw.get("positions") or []
                    if str((p or {}).get("name") or "").lower() == TO_OPGG.get(role)), None) or summary_raw.get("average_stats") or {}
    label = f"{champion} {ROLE_LABELS.get(role, role)}"
    build = Build(
        champion=champion,
        champion_id=champion_id,
        role=role,
        fetched_at="",
        patch=patch,
        name=label,
        primaryStyle=variant["primaryStyle"],
        subStyle=variant["subStyle"],
        perks=variant["perks"],
        spells=variant["spells"],
        itemSet={
            "title": f"{AUTO_PREFIX} {label} {patch}",
            "associatedChampions": [champion_id],
            "associatedMaps": [ARAM_MAP if role == ARAM else SUMMONERS_RIFT_MAP],
            "blocks": variant["blocks"],
        },
        sample_size=int(summary.get("play") or runes.get("play") or 0),
        winrate=float(summary.get("win_rate") or _wr(runes)),
        source=source,
        details=variant["details"],
        variants=[variant],
        spell_options=[{
            "spells": variant["spells"], "games": spells.get("play") or 0, "winrate": _wr(spells),
            "pick_rate": spells.get("pick_rate") or 0, "most_played": True,
        }],
    )
    build.validate()
    return build


MATCHUP_MIN_PICK = 0.05
MATCHUP_MIN_GAMES = 50


def _best(options: list[dict[str, Any]] | None) -> dict[str, Any] | None:
    """Meilleur winrate parmi les options assez jouées, sinon la plus jouée."""
    options = [o for o in options or [] if o and o.get("play")]
    if not options:
        return None
    eligible = [o for o in options if (o.get("pick_rate") or 0) >= MATCHUP_MIN_PICK and o["play"] >= MATCHUP_MIN_GAMES]
    if eligible:
        return max(eligible, key=lambda o: (_wr(o), o["play"]))
    return max(options, key=lambda o: o["play"])


def matchup_variants(guide: Any, *, opponent: str, items: ItemCatalog | None = None,
                     max_variants: int = 2) -> list[dict[str, Any]]:
    """Propositions contre un adversaire depuis `lol_get_lane_matchup_guide`.

    Les pages de runes sont regroupées par (arbre, keystone, arbre secondaire) — OP.GG en liste
    plusieurs qui ne diffèrent que par les shards — puis triées par winrate (la plus jouée reste
    toujours proposée). Sorts et items : meilleur winrate éligible du matchup, communs aux variantes.
    """
    data = (guide or {}).get("data") or {}
    groups: dict[tuple[Any, ...], dict[str, Any]] = {}
    for page in data.get("runes") or []:
        page = page or {}
        ids = page.get("primary_rune_ids") or []
        if len(ids) != 4 or len(page.get("secondary_rune_ids") or []) != 2 or len(page.get("stat_mod_ids") or []) != 3:
            continue
        key = (page.get("primary_page_id"), ids[0], page.get("secondary_page_id"))
        if key not in groups or (page.get("play") or 0) > (groups[key].get("play") or 0):
            groups[key] = page
    pages = sorted(groups.values(), key=lambda p: p.get("play") or 0, reverse=True)
    spells = _best(data.get("summoner_spells"))
    if not pages or not spells:
        return []
    most_played = pages[0]
    eligible = [p for p in pages if (p.get("pick_rate") or 0) >= MATCHUP_MIN_PICK and (p.get("play") or 0) >= MATCHUP_MIN_GAMES]
    chosen = sorted(eligible, key=lambda p: (_wr(p), p["play"]), reverse=True)[:max_variants]
    if most_played not in chosen:
        chosen = [*chosen[: max_variants - 1], most_played]
        chosen.sort(key=lambda p: (_wr(p), p.get("play") or 0), reverse=True)

    def completed(iid: int) -> bool:
        return not items or not items.items or items.is_completed(iid)

    situational = [i for option in data.get("last_items") or [] for i in (option or {}).get("ids") or [] if completed(i)]
    skills = (((data.get("skills") or [{}])[0]) or {}).get("order") or []
    variants = []
    for page in chosen:
        tags = [f"Contre {opponent}"] + (["Le plus joué"] if page is most_played and len(chosen) > 1 else [])
        v = _variant(page, spells, starter=_best(data.get("starter_items")), core=_best(data.get("core_items")),
                     boots=_best(data.get("boots")), situational=situational, skills=skills, tags=tags)
        if len(v["perks"]) == 9 and len(v["spells"]) == 2:
            variants.append(v)
    return variants


def matchup_insight(guide: Any) -> dict[str, str]:
    """Lecture du duel par OP.GG (point de vue de `my_champion`) : avantage en lane, en solo kill, conseil."""
    data = (guide or {}).get("data") or {}
    return {
        "lane": str(data.get("lane_advantage_champion") or "").upper(),
        "soloKill": str(data.get("lane_solo_kill_advantage_champion") or "").upper(),
        "tip": str(data.get("opponent_champion_tip") or "").strip(),
    }


def tier_list(meta: Any, champions: ChampionCatalog, role: str) -> list[dict[str, Any]]:
    """Champions d'un rôle classés par tier OP.GG (1 = OP) puis rang."""
    entries = (((meta or {}).get("data") or {}).get("positions") or {}).get(TO_OPGG.get(role, ""), []) or []
    result = []
    for e in entries:
        e = e or {}
        champ = champions.resolve(str(e.get("champion") or ""))
        if not champ:
            continue
        result.append({
            "championId": champions.key_of(champ), "tier": e.get("tier") or 5, "rank": e.get("rank") or 999,
            "winrate": e.get("win_rate") or 0, "pickRate": e.get("pick_rate") or 0, "banRate": e.get("ban_rate") or 0,
            "games": e.get("play") or 0,
        })
    return sorted(result, key=lambda e: (e["tier"], e["rank"]))


def counters_from_guide(guide: Any) -> list[dict[str, Any]]:
    """Résultats de chaque adversaire contre le champion `my_champion` du guide.

    `data.counters` du guide de matchup liste les duels du champion contre tous ses
    adversaires de lane (indépendamment de `opponent_champion`). On renvoie le point de vue
    de l'adversaire : {"championId", "games", "winrate"} = son winrate contre le champion.
    """
    result = []
    for c in ((guide or {}).get("data") or {}).get("counters") or []:
        c = c or {}
        play, win = c.get("play") or 0, c.get("win") or 0
        if play and c.get("champion_id"):
            result.append({"championId": int(c["champion_id"]), "games": play, "winrate": round(1 - win / play, 4)})
    return sorted(result, key=lambda c: c["winrate"], reverse=True)


# --------------------------------------------------------------------------- source

def _guarded(fn):
    """Une réponse mal formée ne doit jamais sortir en KeyError/TypeError : on la ramène à BuildSourceError."""
    @functools.wraps(fn)
    def wrapper(*args, **kwargs):
        try:
            return fn(*args, **kwargs)
        except BuildSourceError:
            raise
        except (KeyError, TypeError, AttributeError, ValueError, IndexError) as exc:
            raise BuildSourceError(f"{fn.__name__} : réponse OP.GG inexploitable ({type(exc).__name__}: {exc})") from exc
    return wrapper


class OpggSource(BuildSource):
    name = "opgg"

    def __init__(self, static: StaticData, settings: OpggSettings, client: OpggClient | None = None) -> None:
        self.static = static
        self.settings = settings
        self.client = client or OpggClient(settings.request_interval)
        self._roles: dict[str, dict[str, float]] | None = None
        self._roles_date = ""

    @_guarded
    def roles(self) -> dict[str, dict[str, float]]:
        """Rôles joués par champion (un seul appel par jour, mis en mémoire)."""
        today = date.today().isoformat()
        if self._roles is None or self._roles_date != today:  # le process peut passer minuit
            self._roles = None
            meta = self.client.call("lol_list_lane_meta_champions", {
                "position": "all", "lang": "en_US",
                "desired_output_fields": META_FIELDS,
            })
            self._roles = lane_roles(meta, self.static.champions)
            self._roles_date = today
            log.info("OP.GG : %d champions dans la méta des lanes", len(self._roles))
        return self._roles

    @_guarded
    def targets(self) -> list[Target]:
        """Tous les champions, dans chaque rôle joué au moins `min_role_rate` (et toujours le principal)."""
        result: set[Target] = set()
        for champ, rates in self.roles().items():
            main = max(rates, key=rates.get)
            result.update(Target(champ, r) for r, rate in rates.items()
                          if r == main or rate >= self.settings.min_role_rate)
        missing = set(self.static.champions.by_key.values()) - {t.champion for t in result}
        if missing:
            log.info("OP.GG : sans données de lane (ignorés) : %s", ", ".join(sorted(missing)))
        return sorted(result)

    def main_role(self, champion: str) -> str | None:
        rates = self.roles().get(champion)
        return max(rates, key=rates.get) if rates else None

    def aram_targets(self, champions: Iterable[str]) -> list[Target]:
        """Un build ARAM par champion (pas de lane en ARAM), si l'option `aram` est active."""
        return [Target(champ, ARAM) for champ in sorted(set(champions))] if self.settings.aram else []

    @_guarded
    def fetch_build(self, champion: str, role: str) -> Build:
        if role == ARAM:
            return self._fetch_aram(champion)
        if role not in TO_OPGG:
            raise BuildSourceError(f"rôle inconnu : {role}")
        try:
            champion_id = self.static.champions.key_of(champion)
        except KeyError as exc:
            raise BuildSourceError(f"champion inconnu : {champion}") from exc
        args: dict[str, Any] = {
            "champion": opgg_name(self.static.champions.names.get(champion, champion)),
            "position": TO_OPGG[role],
            "game_mode": self.settings.game_mode,
            "lang": "en_US",
            "desired_output_fields": ANALYSIS_FIELDS,
        }
        if self.settings.tier:
            args["tier"] = self.settings.tier
        analysis = self.client.call("lol_get_champion_analysis", args)
        return build_from_analysis(analysis, champion=champion, champion_id=champion_id, role=role,
                                   patch=self.static.patch)

    def _fetch_aram(self, champion: str) -> Build:
        """Build issu des stats d'ARAM d'OP.GG (une seule requête ; la lane demandée est ignorée par OP.GG)."""
        try:
            champion_id = self.static.champions.key_of(champion)
        except KeyError as exc:
            raise BuildSourceError(f"champion inconnu : {champion}") from exc
        analysis = self.client.call("lol_get_champion_analysis", {
            "champion": opgg_name(self.static.champions.names.get(champion, champion)),
            "position": "mid",  # obligatoire pour l'outil, sans effet en ARAM
            "game_mode": "aram",
            "lang": "en_US",
            "desired_output_fields": ANALYSIS_FIELDS,
        })
        return build_from_analysis(analysis, champion=champion, champion_id=champion_id, role=ARAM,
                                   patch=self.static.patch)

    @_guarded
    def fetch_matchup(self, champion: str, role: str, opponent: str, insight: bool = False) -> Any:
        """Propositions de `champion` contre `opponent` (ids Data Dragon) dans le rôle donné.

        `insight` : renvoie (propositions, lecture du duel), voir `matchup_insight`."""
        if role not in TO_OPGG:
            raise BuildSourceError(f"rôle inconnu : {role}")
        names = self.static.champions.names
        guide = self.client.call("lol_get_lane_matchup_guide", {
            "position": TO_OPGG[role], "lang": "en_US",
            "my_champion": opgg_name(names.get(champion, champion)),
            "opponent_champion": opgg_name(names.get(opponent, opponent)),
        })
        variants = matchup_variants(guide, opponent=names.get(opponent, opponent), items=self.static.items)
        return (variants, matchup_insight(guide)) if insight else variants

    @_guarded
    def fetch_tier_list(self, role: str) -> list[dict[str, Any]]:
        """Tier list OP.GG du rôle (les champions les plus forts en premier)."""
        if role not in TO_OPGG:
            raise BuildSourceError(f"rôle inconnu : {role}")
        pos = TO_OPGG[role]
        meta = self.client.call("lol_list_lane_meta_champions", {
            "position": pos, "lang": "en_US",
            "desired_output_fields": [f"data.positions.{pos}[].{{champion,tier,rank,win_rate,pick_rate,ban_rate,play}}"],
        })
        return tier_list(meta, self.static.champions, role)

    @_guarded
    def fetch_counters(self, enemy: str, role: str) -> list[dict[str, Any]]:
        """Winrate de chaque champion contre `enemy` dans ce rôle (meilleurs picks en premier)."""
        if role not in TO_OPGG:
            raise BuildSourceError(f"rôle inconnu : {role}")
        names = self.static.champions.names
        # Le guide exige un adversaire, mais `counters` n'en dépend pas : n'importe lequel convient.
        dummy = "ZED" if opgg_name(names.get(enemy, enemy)) == "AHRI" else "AHRI"
        guide = self.client.call("lol_get_lane_matchup_guide", {
            "position": TO_OPGG[role], "lang": "en_US",
            "my_champion": opgg_name(names.get(enemy, enemy)), "opponent_champion": dummy,
        })
        return counters_from_guide(guide)

    def close(self) -> None:
        self.client.close()
