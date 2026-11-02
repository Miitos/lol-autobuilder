"""Données statiques Data Dragon (champions, items, runes, sorts), mises en cache localement.

Le prefetch télécharge la version courante ; le client champ select ne lit que le
cache (`static.json`), sans aucun appel réseau.
"""

from __future__ import annotations

import json
import logging
import os
import re
import threading
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import httpx

log = logging.getLogger(__name__)

DDRAGON = "https://ddragon.leagueoflegends.com"
FILES = ("champion.json", "item.json", "runesReforged.json", "summoner.json")


def atomic_write(path: Path, data: bytes | str) -> None:
    """Écrit via un fichier temporaire unique puis `os.replace` : jamais de fichier tronqué."""
    tmp = path.with_name(f"{path.name}.{os.getpid()}.{threading.get_ident()}.tmp")
    try:
        if isinstance(data, str):
            tmp.write_text(data, encoding="utf-8")
        else:
            tmp.write_bytes(data)
        os.replace(tmp, path)
    except BaseException:
        tmp.unlink(missing_ok=True)
        raise


def patch_of(version: str) -> str:
    """'15.21.1' ou '15.21.628.3456' -> '15.21'."""
    return ".".join(version.split(".")[:2])


def _norm(name: str) -> str:
    return re.sub(r"[^a-z0-9]", "", name.lower())


@dataclass
class ChampionCatalog:
    """Correspondances clé numérique <-> id Data Dragon <-> nom affiché."""

    by_key: dict[int, str] = field(default_factory=dict)  # 103 -> "Ahri"
    names: dict[str, str] = field(default_factory=dict)  # "MonkeyKing" -> "Wukong"

    @classmethod
    def from_ddragon(cls, champion_json: dict[str, Any]) -> ChampionCatalog:
        cat = cls()
        for cid, c in champion_json["data"].items():
            cat.by_key[int(c["key"])] = cid
            cat.names[cid] = c["name"]
        return cat

    def key_of(self, champion: str) -> int:
        for key, cid in self.by_key.items():
            if cid == champion:
                return key
        raise KeyError(champion)

    def resolve(self, text: str) -> str | None:
        """Retrouve l'id Data Dragon à partir d'un id, d'un nom ou d'une clé ("wukong" -> "MonkeyKing")."""
        if text.isdigit():
            return self.by_key.get(int(text))
        wanted = _norm(text)
        for cid, name in self.names.items():
            if _norm(cid) == wanted or _norm(name) == wanted:
                return cid
        return None

    def to_dict(self) -> dict[str, Any]:
        return {"by_key": {str(k): v for k, v in self.by_key.items()}, "names": self.names}

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> ChampionCatalog:
        return cls({int(k): v for k, v in data["by_key"].items()}, dict(data["names"]))


@dataclass
class ItemInfo:
    id: int
    name: str
    completed: bool  # objet final (ne se transforme plus)
    boots: bool
    consumable: bool  # potions, wards, trinkets...


@dataclass
class ItemCatalog:
    items: dict[int, ItemInfo] = field(default_factory=dict)

    @classmethod
    def from_ddragon(cls, item_json: dict[str, Any]) -> ItemCatalog:
        cat = cls()
        for sid, it in item_json["data"].items():
            tags = set(it.get("tags", []))
            gold = it.get("gold", {})
            on_sr = it.get("maps", {}).get("11", True)
            if not on_sr or not gold.get("purchasable", True):
                continue
            iid = int(sid)
            boots = "Boots" in tags and iid != 1001  # 1001 = bottes de base
            consumable = bool(tags & {"Consumable", "Trinket"}) or it.get("consumed", False)
            completed = (not it.get("into") and gold.get("total", 0) >= 1000 and not consumable) or boots
            cat.items[iid] = ItemInfo(iid, it.get("name", sid), completed, boots, consumable)
        return cat

    def is_completed(self, item_id: int) -> bool:
        info = self.items.get(item_id)
        return bool(info and info.completed and not info.boots)

    def is_boots(self, item_id: int) -> bool:
        info = self.items.get(item_id)
        return bool(info and info.boots)

    def is_starter(self, item_id: int) -> bool:
        """Achetable en début de partie (tout sauf trinkets)."""
        info = self.items.get(item_id)
        return bool(info) and item_id not in TRINKETS


TRINKETS = {3340, 3363, 3364, 3330}


@dataclass
class StaticData:
    version: str
    champions: ChampionCatalog
    items: ItemCatalog
    rune_ids: set[int] = field(default_factory=set)
    spell_ids: set[int] = field(default_factory=set)

    @property
    def patch(self) -> str:
        return patch_of(self.version)


class DataDragon:
    """Téléchargement et mise en cache des fichiers Data Dragon d'une version donnée."""

    def __init__(self, cache_dir: Path, http: httpx.Client | None = None) -> None:
        self.dir = cache_dir / "ddragon"
        self.static_file = cache_dir / "static.json"
        self._own_http = http is None
        self.http = http or httpx.Client(timeout=30)

    def close(self) -> None:
        """Ferme le client httpx s'il a été créé ici."""
        if self._own_http:
            self.http.close()

    def latest_version(self) -> str:
        resp = self.http.get(f"{DDRAGON}/api/versions.json")
        resp.raise_for_status()
        return resp.json()[0]

    def ensure(self, version: str | None = None) -> StaticData:
        """Télécharge (si besoin) la version demandée, met à jour `static.json`, retourne les données."""
        version = version or self.latest_version()
        vdir = self.dir / version
        vdir.mkdir(parents=True, exist_ok=True)
        raw: dict[str, Any] = {}
        for name in FILES:
            path = vdir / name
            if not path.exists():
                log.info("Data Dragon %s : téléchargement de %s", version, name)
                resp = self.http.get(f"{DDRAGON}/cdn/{version}/data/en_US/{name}")
                resp.raise_for_status()
                atomic_write(path, resp.content)
            try:
                raw[name] = json.loads(path.read_text(encoding="utf-8"))
            except ValueError:  # fichier corrompu : on le retélécharge au prochain passage
                path.unlink(missing_ok=True)
                raise
        static = build_static(version, raw)
        # Fichier compact lu hors-ligne par le client champ select.
        atomic_write(self.static_file, json.dumps({"version": version, "champions": static.champions.to_dict()}))
        return static

    def load_cached(self) -> StaticData | None:
        """Dernière version en cache, sans réseau (secours si Data Dragon est injoignable)."""
        try:
            version = json.loads(self.static_file.read_text(encoding="utf-8"))["version"]
            vdir = self.dir / version
            if not all((vdir / n).exists() for n in FILES):
                return None
            return build_static(version, {n: json.loads((vdir / n).read_text(encoding="utf-8")) for n in FILES})
        except (OSError, ValueError, KeyError, TypeError, AttributeError) as exc:
            log.warning("Cache Data Dragon illisible : %s", exc)
            return None


def build_static(version: str, raw: dict[str, Any]) -> StaticData:
    runes: set[int] = set()
    for style in raw["runesReforged.json"]:
        runes.add(style["id"])
        for slot in style["slots"]:
            runes.update(r["id"] for r in slot["runes"])
    spells = {int(s["key"]) for s in raw["summoner.json"]["data"].values()}
    return StaticData(
        version=version,
        champions=ChampionCatalog.from_ddragon(raw["champion.json"]),
        items=ItemCatalog.from_ddragon(raw["item.json"]),
        rune_ids=runes,
        spell_ids=spells,
    )


def load_champion_catalog(cache_dir: Path) -> ChampionCatalog | None:
    """Catalogue des champions pour le champ select (lecture seule, hors-ligne)."""
    try:
        data = json.loads((cache_dir / "static.json").read_text(encoding="utf-8"))
        return ChampionCatalog.from_dict(data["champions"])
    except (OSError, ValueError, KeyError, TypeError, AttributeError):
        return None
