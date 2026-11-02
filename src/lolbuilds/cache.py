"""Cache local des builds : un fichier JSON par couple champion/rôle."""

from __future__ import annotations

import json
import logging
import os
import threading
from datetime import date
from pathlib import Path

from . import ROLES
from .models import Build

log = logging.getLogger(__name__)


class BuildCache:
    """Lecture/écriture de `<root>/{Champion}_{role}.json`.

    Les écritures sont atomiques (fichier temporaire + `os.replace`) : un crash en
    plein milieu ne peut pas corrompre le build de la veille.
    """

    def __init__(self, root: Path) -> None:
        self.root = root
        self.root.mkdir(parents=True, exist_ok=True)

    def path_for(self, champion: str, role: str) -> Path:
        return self.root / f"{champion}_{role}.json"

    def load(self, champion: str, role: str) -> Build | None:
        return self._load_path(self.path_for(champion, role))

    def _load_path(self, path: Path) -> Build | None:
        if not path.exists():
            return None
        try:
            return Build.from_dict(json.loads(path.read_text(encoding="utf-8")))
        except (OSError, ValueError, TypeError, AttributeError, KeyError) as exc:  # JSON valide mais pas un build
            log.warning("Cache illisible %s : %s", path.name, exc)
            return None

    def is_fresh(self, champion: str, role: str, today: date) -> bool:
        """Vrai si le build a été récupéré aujourd'hui."""
        build = self.load(champion, role)
        return build is not None and build.fetched_at == today.isoformat()

    def save(self, build: Build) -> Path:
        build.validate()
        path = self.path_for(build.champion, build.role)
        tmp = path.with_name(f"{path.name}.{os.getpid()}.{threading.get_ident()}.tmp")  # unique par thread
        try:
            tmp.write_text(json.dumps(build.to_dict(), indent=2, ensure_ascii=False), encoding="utf-8")
            os.replace(tmp, path)
        except OSError:
            tmp.unlink(missing_ok=True)
            raise
        return path

    def build_paths(self) -> list[Path]:
        """Fichiers de build uniquement (`{Champion}_{role}.json`), pas ui_static.json & co."""
        return sorted(p for p in self.root.glob("*_*.json") if p.stem.rsplit("_", 1)[1] in ROLES)

    def all_builds(self) -> list[Build]:
        return [b for p in self.build_paths() if (b := self._load_path(p))]

    def builds_for_champion(self, champion: str) -> list[Build]:
        """Tous les builds en cache d'un champion (toutes les lanes), plus gros échantillon d'abord.

        Le build ARAM (`{Champion}_aram.json`) n'en fait pas partie : ce n'est pas une lane."""
        builds = [b for p in self.root.glob(f"{champion}_*.json")
                  if p.stem.rsplit("_", 1)[1] in ROLES and (b := self._load_path(p)) and b.champion == champion]
        return sorted(builds, key=lambda b: b.sample_size, reverse=True)
