"""Application d'un build dans le client League (runes, sorts d'invocateur, item set) via le LCU."""

from __future__ import annotations

import json
import logging
import uuid
from collections.abc import Sequence
from typing import Any, Protocol

from .. import AUTO_PREFIX
from ..cache import BuildCache
from ..config import ChampSelectSettings
from ..models import Build

log = logging.getLogger(__name__)

FLASH = 4


# --------------------------------------------------------------------------- accès LCU

class Lcu(Protocol):
    async def request(self, method: str, path: str, body: Any = None) -> tuple[int, Any]:
        """Retourne (code HTTP, JSON décodé ou None)."""


class DryRunLcu:
    """Logue les écritures au lieu de les envoyer ; les lectures passent par `inner` s'il existe."""

    def __init__(self, inner: Lcu | None = None) -> None:
        self.inner = inner

    async def request(self, method: str, path: str, body: Any = None) -> tuple[int, Any]:
        if method.upper() == "GET":
            if self.inner:
                return await self.inner.request(method, path)
            if path.startswith("/lol-item-sets/"):
                return 200, {"itemSets": []}
            return 200, _FAKE_GETS.get(path)
        log.info("[dry-run] %s %s %s", method.upper(), path, "" if body is None else _short(body))
        return 200, None


_FAKE_GETS: dict[str, Any] = {
    "/lol-perks/v1/pages": [],
    "/lol-perks/v1/inventory": {"ownedPageCount": 20},
    "/lol-summoner/v1/current-summoner": {"summonerId": 0},
}


def _short(body: Any) -> str:
    text = str(body)
    return text if len(text) < 300 else text[:300] + "…"


# --------------------------------------------------------------------------- logique pure

def find_build(cache: BuildCache, champion: str, position: str) -> Build | None:
    """Build du rôle assigné ; si pas de rôle (blind, ARAM...) : le build en cache du champion."""
    if position:
        return cache.load(champion, position)
    builds = cache.builds_for_champion(champion)
    return builds[0] if builds else None


def orient_spells(spells: Sequence[int], flash_key: str) -> tuple[int, int]:
    """Place Flash sur la touche préférée (D = spell1, F = spell2)."""
    a, b = spells
    if FLASH in (a, b) and flash_key in ("D", "F"):
        other = b if a == FLASH else a
        return (FLASH, other) if flash_key == "D" else (other, FLASH)
    return a, b


def auto_pages(pages: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Pages créées par l'outil et supprimables."""
    return [p for p in pages if str(p.get("name", "")).startswith(AUTO_PREFIX) and p.get("isDeletable", True)]


def merge_item_sets(existing: dict[str, Any], new_set: dict[str, Any]) -> dict[str, Any]:
    """Payload pour le PUT : sets de l'utilisateur intacts, sets de l'outil remplacés par `new_set`.

    Le PUT remplace *tous* les sets du compte : on repart donc toujours de la
    réponse du GET, et on n'enlève que ce qui porte notre préfixe.
    """
    payload = dict(existing)
    kept = [s for s in existing.get("itemSets", []) if not str(s.get("title", "")).startswith(AUTO_PREFIX)]
    # Le jeu trie par sortrank décroissant et présélectionne le premier : le nôtre passe devant ceux des autres applis.
    ranks = [r for r in (s.get("sortrank") for s in kept) if isinstance(r, int | float)]
    first = {**new_set, "sortrank": int(max(ranks, default=0)) + 1}
    payload["itemSets"] = [first, *kept]
    return payload


def lcu_item_set(build: Build) -> dict[str, Any]:
    src = build.itemSet
    return {
        "uid": str(uuid.uuid4()),
        "title": src.get("title", f"{AUTO_PREFIX} {build.name}"),
        "type": "custom",
        "map": "any",
        "mode": "any",
        "sortrank": 0,
        "startedFrom": "blank",
        "associatedChampions": src.get("associatedChampions", [build.champion_id]),
        "associatedMaps": src.get("associatedMaps", [11]),
        "preferredItemSlots": [],
        "blocks": [
            {"type": b["type"], "items": b["items"], "hideIfSummonerSpell": "", "showIfSummonerSpell": ""}
            for b in src.get("blocks", [])
        ],
    }


# --------------------------------------------------------------------------- application

class BuildApplier:
    def __init__(self, settings: ChampSelectSettings) -> None:
        self.settings = settings
        self.problems: dict[str, str] = {}  # étape -> raison lisible, pour l'interface (en plus du journal)

    async def apply(self, lcu: Lcu, build: Build) -> dict[str, bool]:
        """Applique les étapes activées ; chacune est indépendante (l'échec de l'une n'arrête pas les autres)."""
        log.info("Application de %s (patch %s, %d parties, du %s)", build.name, build.patch, build.sample_size, build.fetched_at)
        steps: dict[str, bool] = {}
        self.problems = {}
        for name, enabled, step in (
            ("runes", self.settings.apply_runes, self.apply_runes),
            ("spells", self.settings.apply_spells, self.apply_spells),
            ("items", self.settings.apply_item_set, self.apply_item_set),
        ):
            if not enabled:
                continue
            try:
                steps[name] = bool(await step(lcu, build))
            except Exception:
                log.exception("Échec de %s", name)
                steps[name] = False
        return steps

    async def apply_runes(self, lcu: Lcu, build: Build) -> bool:
        status, pages = await lcu.request("GET", "/lol-perks/v1/pages")
        if status != 200 or not isinstance(pages, list):
            log.warning("Runes : lecture des pages impossible (%s)", status)
            return False
        for page in auto_pages(pages):
            await lcu.request("DELETE", f"/lol-perks/v1/pages/{page['id']}")
        # Le client limite le nombre de pages : on ne supprime jamais celles de l'utilisateur.
        _, inv = await lcu.request("GET", "/lol-perks/v1/inventory")
        owned = (inv or {}).get("ownedPageCount")
        custom = [p for p in pages if p.get("isEditable") and p not in auto_pages(pages)]
        if owned is not None and len(custom) >= owned:
            log.warning("Runes : aucune page libre (%d/%d), supprimez une page pour activer l'import", len(custom), owned)
            return False
        body = {
            "name": f"{AUTO_PREFIX} {build.name}",
            "primaryStyleId": build.primaryStyle,
            "subStyleId": build.subStyle,
            "selectedPerkIds": build.perks,
            "current": True,
        }
        status, data = await lcu.request("POST", "/lol-perks/v1/pages", body)
        if status not in (200, 201, 204):
            log.warning("Runes : création refusée (%s) %s", status, data)
            return False
        log.info("Runes : page %s créée", body["name"])
        return True

    async def apply_spells(self, lcu: Lcu, build: Build) -> bool:
        d, f = orient_spells(build.spells, self.settings.flash_key)
        status, data = await lcu.request("PATCH", "/lol-champ-select/v1/session/my-selection", {"spell1Id": d, "spell2Id": f})
        if status not in (200, 204):
            log.warning("Sorts : refusé (%s) %s", status, data)
            return False
        log.info("Sorts : %d / %d", d, f)
        return True

    async def apply_item_set(self, lcu: Lcu, build: Build) -> bool:
        status, me = await lcu.request("GET", "/lol-summoner/v1/current-summoner")
        if status != 200 or not me:
            log.warning("Item set : invocateur courant illisible (%s)", status)
            return False
        path = f"/lol-item-sets/v1/item-sets/{me['summonerId']}/sets"
        status, existing = await lcu.request("GET", path)
        if status != 200 or not isinstance(existing, dict):
            # Sans lecture fiable, un PUT effacerait les sets de l'utilisateur : on s'abstient.
            log.warning("Item set : lecture des sets impossible (%s), abandon", status)
            return False
        payload = merge_item_sets(existing, lcu_item_set(build))
        status, data = await lcu.request("PUT", path, payload)
        if status not in (200, 201, 204):
            size = len(json.dumps(payload, separators=(",", ":"), ensure_ascii=False).encode("utf-8"))
            others = len(payload["itemSets"]) - 1
            log.warning("Item set : refusé (%s) %s ; envoyé : %d Ko, %d de tes item sets + le nôtre", status, data, size // 1024, others)
            if status == 413:
                self.problems["items"] = (f"Le client refuse d'enregistrer l'item set : tes {others} item sets existants pèsent "
                                          f"{size // 1024} Ko, trop pour lui. Supprime ceux que tu n'utilises plus (souvent ceux d'autres applis).")
            else:
                self.problems["items"] = f"Le client a refusé l'item set (code {status})."
            return False
        log.info("Item set : %s", build.itemSet.get("title"))
        return True
