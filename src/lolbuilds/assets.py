"""Données et images de l'interface (Data Dragon, en français), téléchargées par le prefetch.

L'UI n'affiche que ce qui est dans `<cache>/assets/` et `<cache>/ui_static.json` :
elle ne fait aucun appel réseau externe pendant le champ select.
"""

from __future__ import annotations

import json
import logging
import re
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any

import httpx

from .ddragon import DDRAGON, atomic_write

log = logging.getLogger(__name__)

LANG = "fr_FR"
UI_FILES = ("champion.json", "item.json", "runesReforged.json", "summoner.json")

# Les fragments de stats (shards) n'existent pas dans Data Dragon.
SHARDS = {
    5008: ("Force adaptative", "adaptive"),
    5005: ("Vitesse d'attaque", "attackspeed"),
    5007: ("Accélération de compétence", "haste"),
    5010: ("Vitesse de déplacement", "movespeed"),
    5001: ("Santé croissante", "healthscaling"),
    5011: ("Santé", "health"),
    5013: ("Ténacité et résistance aux ralentissements", "tenacity"),
    5002: ("Armure", "armor"),
    5003: ("Résistance magique", "magicres"),
}


def _clean(html: str) -> str:
    """Retire les balises Riot (<br>, <magicDamage>...) d'une description."""
    return re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", html or "")).strip()


def build_ui_static(version: str, raw: dict[str, Any]) -> dict[str, Any]:
    """Résumé compact pour l'UI : noms, descriptions courtes, chemins d'images."""
    champions = {
        c["key"]: {"id": cid, "name": c["name"], "title": c.get("title", ""), "tags": c.get("tags", [])}
        for cid, c in raw["champion.json"]["data"].items()
    }
    items = {
        iid: {"name": it["name"], "gold": it.get("gold", {}).get("total", 0), "text": _clean(it.get("plaintext") or it.get("description", ""))[:160]}
        for iid, it in raw["item.json"]["data"].items()
    }
    styles, perks = {}, {}
    for style in raw["runesReforged.json"]:
        styles[str(style["id"])] = {"name": style["name"], "key": style["key"]}
        for slot_index, slot in enumerate(style["slots"]):
            for r in slot["runes"]:
                perks[str(r["id"])] = {
                    "name": r["name"],
                    "style": style["id"],
                    "slot": slot_index,  # 0 = rune clé
                    "text": _clean(r.get("shortDesc", ""))[:220],
                }
    for sid, (name, key) in SHARDS.items():
        perks[str(sid)] = {"name": name, "style": 0, "slot": -1, "shard": key, "text": name}
    spells = {
        s["key"]: {"id": sid, "name": s["name"], "text": _clean(s.get("description", ""))[:200]}
        for sid, s in raw["summoner.json"]["data"].items()
    }
    return {"version": version, "champions": champions, "items": items, "styles": styles, "perks": perks, "spells": spells}


class AssetStore:
    def __init__(self, cache_dir: Path, http: httpx.Client | None = None) -> None:
        self.cache_dir = cache_dir
        self.root = cache_dir / "assets"
        self.ui_file = cache_dir / "ui_static.json"
        self._own_http = http is None
        self.http = http or httpx.Client(timeout=30)

    def close(self) -> None:
        if self._own_http:
            self.http.close()

    def ensure(self, version: str, splash_for: list[str] | None = None) -> dict[str, Any]:
        """Télécharge les JSON fr_FR et les images manquantes ; écrit `ui_static.json`.

        Les splash (≈ 150 Ko chacun) sont pris pour tous les champions si `splash_for` est None :
        en champ select on peut tomber sur n'importe lequel, et l'UI ne télécharge jamais rien.
        """
        vdir = self.cache_dir / "ddragon" / version / LANG
        vdir.mkdir(parents=True, exist_ok=True)
        raw: dict[str, Any] = {}
        for name in UI_FILES:
            path = vdir / name
            if not path.exists():
                atomic_write(path, self._get(f"{DDRAGON}/cdn/{version}/data/{LANG}/{name}"))
            try:
                raw[name] = json.loads(path.read_text(encoding="utf-8"))
            except ValueError:  # fichier corrompu : retéléchargé au prochain passage
                path.unlink(missing_ok=True)
                raise
        ui = build_ui_static(version, raw)
        atomic_write(self.ui_file, json.dumps(ui, ensure_ascii=False))

        jobs: list[tuple[str, Path]] = []
        cdn = f"{DDRAGON}/cdn/{version}/img"
        for cid, c in raw["champion.json"]["data"].items():
            jobs.append((f"{cdn}/champion/{c['image']['full']}", self.root / "champion" / f"{c['key']}.png"))
        for iid, it in raw["item.json"]["data"].items():
            jobs.append((f"{cdn}/item/{it['image']['full']}", self.root / "item" / f"{iid}.png"))
        for s in raw["summoner.json"]["data"].values():
            jobs.append((f"{cdn}/spell/{s['image']['full']}", self.root / "spell" / f"{s['key']}.png"))
        for style in raw["runesReforged.json"]:
            jobs.append((f"{DDRAGON}/cdn/img/{style['icon']}", self.root / "perk" / f"{style['id']}.png"))
            for slot in style["slots"]:
                for r in slot["runes"]:
                    jobs.append((f"{DDRAGON}/cdn/img/{r['icon']}", self.root / "perk" / f"{r['id']}.png"))
        champs = raw["champion.json"]["data"]
        for cid in champs if splash_for is None else splash_for:
            if cid in champs:
                jobs.append((f"{DDRAGON}/cdn/img/champion/centered/{cid}_0.jpg", self.root / "splash" / f"{champs[cid]['key']}.jpg"))

        missing = [(u, p) for u, p in jobs if not p.exists()]
        if missing:
            log.info("Assets : %d images à télécharger", len(missing))
            with ThreadPoolExecutor(max_workers=8) as pool:
                list(pool.map(lambda job: self._download(*job), missing))
        return ui

    def _get(self, url: str) -> bytes:
        resp = self.http.get(url)
        resp.raise_for_status()
        return resp.content

    def _download(self, url: str, path: Path) -> None:
        try:
            data = self._get(url)
        except httpx.HTTPError as exc:
            log.debug("Asset introuvable %s : %s", url, exc)
            return
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            atomic_write(path, data)  # atomique : pas d'image tronquée prise pour valide
        except OSError as exc:
            log.warning("Asset non écrit %s : %s", path.name, exc)
