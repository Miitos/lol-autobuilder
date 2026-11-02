"""Job nocturne : met à jour le cache des builds (une fois par jour et par champion/rôle).

Tous les champions sont suivis, dans chacun des rôles où ils sont joués (source OP.GG).
Optionnel : par défaut l'interface récupère chaque build au moment du pick ([prefetch] nightly).

Usage :
    python -m lolbuilds.prefetch [--config config.toml] [--only Ahri[:middle]] [--force]
"""

from __future__ import annotations

import argparse
import logging
import sys
from collections.abc import Sequence
from datetime import date
from enum import Enum
from pathlib import Path

from .assets import AssetStore
from .cache import BuildCache
from .config import Config, default_config_path, load_config, setup_logging
from .ddragon import DataDragon, StaticData
from .models import Target
from .sources import BuildSource, BuildSourceError
from .sources.opgg import OpggSource
from .targets import filter_only

log = logging.getLogger("lolbuilds.prefetch")


class Outcome(Enum):
    FRESH = "déjà à jour"
    UPDATED = "mis à jour"
    KEPT_OLD = "échec, ancien cache conservé"
    MISSING = "échec, aucun cache"


def refresh_one(cache: BuildCache, source: BuildSource, target: Target, today: date, force: bool = False) -> Outcome:
    """Met à jour un build. En cas d'erreur, l'ancien fichier n'est jamais touché."""
    if not force and cache.is_fresh(target.champion, target.role, today):
        return Outcome.FRESH
    try:
        build = source.fetch_build(target.champion, target.role)
        build.fetched_at = today.isoformat()
        cache.save(build)
        log.info("%s %s : %d parties, %.1f %% WR", target.champion, target.role, build.sample_size, build.winrate * 100)
        return Outcome.UPDATED
    except Exception as exc:  # un champion défectueux ne doit jamais interrompre tout le prefetch
        old = cache.load(target.champion, target.role)
        if old:
            log.warning("%s %s : %s -> build du %s conservé", target.champion, target.role, exc, old.fetched_at)
            return Outcome.KEPT_OLD
        log.warning("%s %s : %s (aucun cache)", target.champion, target.role, exc)
        return Outcome.MISSING


def refresh_all(cache: BuildCache, source: BuildSource, targets: Sequence[Target], today: date,
                force: bool = False, collect: bool = True) -> dict[Target, Outcome]:
    """Prépare la source seulement s'il reste des builds périmés, puis les met à jour."""
    stale = [t for t in targets if force or not cache.is_fresh(t.champion, t.role, today)]
    results = {t: Outcome.FRESH for t in targets if t not in stale}
    if not stale:
        log.info("Tous les builds (%d) sont à jour.", len(targets))
        return results
    if collect:
        source.prepare(stale)
    for t in stale:
        results[t] = refresh_one(cache, source, t, today, force=True)
    return results


def make_source(cfg: Config, static: StaticData) -> OpggSource:
    """Point d'extension : instancier ici une autre source."""
    return OpggSource(static, cfg.opgg)


def load_static(cfg: Config) -> StaticData | None:
    dd = DataDragon(cfg.cache_dir)
    try:
        return dd.ensure()
    except Exception as exc:  # réseau, JSON...
        log.error("Data Dragon injoignable (%s), utilisation de la version en cache", exc)
        return dd.load_cached()
    finally:
        dd.close()


def update_ui_assets(cfg: Config, static: StaticData, cache: BuildCache) -> None:
    """Images et textes fr de l'interface ; un échec ici ne bloque jamais les builds."""
    store = AssetStore(cfg.cache_dir)
    try:
        store.ensure(static.version)
    except Exception as exc:
        log.warning("Assets de l'interface non mis à jour : %s", exc)
    finally:
        store.close()


def run(args: argparse.Namespace) -> int:
    cfg = load_config(args.config)
    setup_logging(cfg.cache_dir, "prefetch", args.verbose)
    if args.scheduled and not cfg.prefetch.nightly:
        log.info("Prefetch nocturne désactivé ([prefetch] nightly = false) : rien à faire.")
        return 0
    today = date.today()
    log.info("=== Prefetch du %s ===", today.isoformat())

    static = load_static(cfg)
    if static is None:
        log.error("Aucune donnée Data Dragon disponible, abandon.")
        return 2
    log.info("Patch courant : %s", static.patch)
    cache = BuildCache(cfg.cache_dir)
    update_ui_assets(cfg, static, cache)

    source = make_source(cfg, static)
    try:
        targets = source.targets()
        if args.only:
            targets = filter_only(targets, args.only, static.champions, source.main_role)
        targets = [*targets, *source.aram_targets({t.champion for t in targets})]  # + un build ARAM par champion
        log.info("%d cibles (%d champions)", len(targets), len({t.champion for t in targets}))
        results = refresh_all(cache, source, targets, today, force=args.force)
    except BuildSourceError as exc:
        log.error("Source %s indisponible : %s. Les builds existants sont conservés.", source.name, exc)
        return 3
    finally:
        source.close()

    summary: dict[Outcome, int] = {}
    for outcome in results.values():
        summary[outcome] = summary.get(outcome, 0) + 1
    log.info("Bilan : %s", ", ".join(f"{n} {o.value}" for o, n in summary.items()) or "rien à faire")
    return 0 if Outcome.MISSING not in summary else 1


def parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    p = argparse.ArgumentParser(prog="lolbuilds-prefetch", description=__doc__.splitlines()[0])
    p.add_argument("--config", type=Path, default=default_config_path())
    p.add_argument("--only", action="append", default=[], metavar="CHAMP[:ROLE]",
                   help="limite aux champions donnés (répétable), ex : --only Ahri --only Thresh:utility")
    p.add_argument("--force", action="store_true", help="ignore la fraîcheur du cache")
    p.add_argument("--scheduled", action="store_true", help="lancement par la tâche planifiée : respecte [prefetch] nightly")
    p.add_argument("-v", "--verbose", action="store_true")
    return p.parse_args(argv)


def main(argv: Sequence[str] | None = None) -> None:
    sys.exit(run(parse_args(argv)))


if __name__ == "__main__":
    main()
