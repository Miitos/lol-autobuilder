"""Mise à jour depuis les releases GitHub : vérification, téléchargement vérifié (SHA-256), installation.

Deux modes de mise à jour automatique, selon la façon dont l'appli a été installée :

- **installée** (un `unins000.exe` à côté de l'exe) : l'installateur de la release est lancé en silencieux ;
- **portable** (exe seul, sans `unins000.exe`) : le nouvel exe portable remplace l'exe en cours. Windows interdit
  d'écraser un exe qui tourne mais accepte de le renommer : l'ancien devient `<exe>.old` (supprimé au démarrage
  suivant) et le nouveau prend sa place.

L'installation depuis les sources ouvre simplement la page de la release.
"""

from __future__ import annotations

import hashlib
import logging
import os
import re
import subprocess
import sys
from pathlib import Path
from typing import Any

import httpx

from . import __version__

log = logging.getLogger(__name__)

REPO = "Miitos/lol-autobuilder"
LATEST = f"https://api.github.com/repos/{REPO}/releases/latest"
INSTALLER = re.compile(r"^lolbuilds-setup-.+\.exe$")
PORTABLE = re.compile(r"^lolbuilds-portable-.+\.exe$")
SUMS = "SHA256SUMS.txt"


class UpdateError(Exception):
    """Vérification ou téléchargement impossible (réseau, release incomplète, empreinte fausse)."""


def parse_version(text: str) -> tuple[int, ...]:
    """« v1.2.3 » -> (1, 2, 3) ; les suffixes (« -beta ») sont ignorés."""
    return tuple(int(n) for n in re.findall(r"\d+", text.split("-")[0])[:3]) or (0,)


def installed() -> bool:
    """Exe installé par l'installateur (et donc remplaçable par une nouvelle version de celui-ci)."""
    return bool(getattr(sys, "frozen", False)) and (Path(sys.executable).parent / "unins000.exe").exists()


def portable() -> bool:
    """Exe portable : un exe seul (PyInstaller), qui ne vient pas de l'installateur."""
    return bool(getattr(sys, "frozen", False)) and not installed()


def mode() -> str | None:
    """`installer`, `portable` ou None (depuis les sources : pas de mise à jour automatique)."""
    return "installer" if installed() else "portable" if portable() else None


def _client(http: httpx.Client | None) -> httpx.Client:
    return http or httpx.Client(timeout=30, follow_redirects=True,
                                headers={"User-Agent": f"lolbuilds/{__version__}", "Accept": "application/vnd.github+json"})


def check(current: str = __version__, http: httpx.Client | None = None) -> dict[str, Any]:
    """Dernière release : version, notes, liens. `newer` vaut True si elle est plus récente que `current`."""
    client = _client(http)
    try:
        resp = client.get(LATEST)
        resp.raise_for_status()
        release = resp.json()
    except (httpx.HTTPError, ValueError) as exc:
        raise UpdateError(f"GitHub injoignable ({exc})") from exc
    finally:
        if http is None:
            client.close()
    latest = str(release.get("tag_name", "")).lstrip("v")
    assets = {a.get("name", ""): a.get("browser_download_url", "") for a in release.get("assets") or []}
    installer = next((name for name in assets if INSTALLER.match(name)), None)
    portable_exe = next((name for name in assets if PORTABLE.match(name)), None)
    return {
        "current": current,
        "latest": latest,
        "newer": parse_version(latest) > parse_version(current),
        "notes": str(release.get("body") or "")[:4000],
        "url": release.get("html_url") or f"https://github.com/{REPO}/releases/latest",
        "installer": {"name": installer, "url": assets[installer]} if installer else None,
        "portable": {"name": portable_exe, "url": assets[portable_exe]} if portable_exe else None,
        "sums": assets.get(SUMS),
    }


def _download(asset: dict[str, str] | None, sums_url: str | None, dest: Path, what: str,
              http: httpx.Client | None, name: str | None = None) -> Path:
    """Télécharge `asset` dans `dest` et vérifie son empreinte avec SHA256SUMS.txt (le fichier faux est supprimé)."""
    if not asset or not sums_url:
        raise UpdateError(f"la release ne contient pas de {what} vérifiable")
    client = _client(http)
    try:
        sums = client.get(sums_url)
        sums.raise_for_status()
        expected = next((line.split()[0].lower() for line in sums.text.splitlines()
                         if line.strip().endswith(asset["name"])), None)
        if not expected:
            raise UpdateError(f"empreinte de {asset['name']} absente de {SUMS}")
        dest.mkdir(parents=True, exist_ok=True)
        path = dest / (name or asset["name"])
        digest = hashlib.sha256()
        with client.stream("GET", asset["url"]) as resp:
            resp.raise_for_status()
            with path.open("wb") as f:
                for chunk in resp.iter_bytes():
                    digest.update(chunk)
                    f.write(chunk)
    except httpx.HTTPError as exc:
        raise UpdateError(f"téléchargement impossible ({exc})") from exc
    finally:
        if http is None:
            client.close()
    if digest.hexdigest() != expected:
        path.unlink(missing_ok=True)
        raise UpdateError("l'empreinte du fichier téléchargé ne correspond pas : installation annulée")
    return path


def download_installer(info: dict[str, Any], dest: Path, http: httpx.Client | None = None) -> Path:
    """Télécharge l'installateur de la release et vérifie son empreinte avec SHA256SUMS.txt."""
    return _download(info.get("installer"), info.get("sums"), dest, "installateur", http)


def download_portable(info: dict[str, Any], dest: Path, http: httpx.Client | None = None) -> Path:
    """Télécharge l'exe portable de la release, vérifié. `dest` doit être sur le même disque que l'exe en cours
    (échanger des fichiers entre deux disques échoue sous Windows)."""
    path = _download(info.get("portable"), info.get("sums"), dest, "exe portable", http, name="lolbuilds-update.exe.download")
    with path.open("rb") as f:
        if f.read(2) != b"MZ":
            path.unlink(missing_ok=True)
            raise UpdateError("le fichier téléchargé n'est pas un exécutable Windows")
    return path


def swap_exe(current: Path, new: Path) -> None:
    """Met `new` à la place de `current` (en cours d'exécution) : l'ancien devient `<exe>.old`."""
    old = current.with_name(current.name + ".old")
    old.unlink(missing_ok=True)
    os.replace(current, old)
    try:
        os.replace(new, current)
    except OSError:
        os.replace(old, current)  # on remet la version qui marche
        raise


def relaunch(exe: Path) -> None:
    """Relance l'exe portable, détaché, avec `--after-update` (il attend que l'ancien libère le port)."""
    # Un exe « onefile » lancé par un autre hérite des variables de son lanceur et chercherait ses fichiers dans le
    # dossier temporaire du parent, supprimé quand celui-ci se ferme : on les retire pour qu'il extrait les siens.
    env = {k: v for k, v in os.environ.items() if not k.startswith(("_PYI", "_MEIPASS"))}
    env["PYINSTALLER_RESET_ENVIRONMENT"] = "1"
    flags = 0x00000008 | 0x00000200  # DETACHED_PROCESS | CREATE_NEW_PROCESS_GROUP
    subprocess.Popen([str(exe), "--after-update"], creationflags=flags, close_fds=True, cwd=str(exe.parent), env=env)
    log.info("Mise à jour : %s relancé", exe.name)


def cleanup_previous() -> bool:
    """Supprime l'ancien exe laissé par la dernière mise à jour portable ; True quand il n'en reste plus.

    Juste après la mise à jour, l'ancien processus finit de quitter et garde le fichier verrouillé quelques
    secondes : l'appelant réessaie tant que ça renvoie False."""
    if not portable():
        return True
    old = Path(sys.executable).with_name(Path(sys.executable).name + ".old")
    if not old.exists():
        return True
    try:
        old.unlink()
        return True
    except OSError:
        return False


def launch_installer(path: Path) -> None:
    """Lance l'installateur en silencieux, détaché : il remplace l'appli (qui doit quitter) puis la relance."""
    flags = 0x00000008 | 0x00000200  # DETACHED_PROCESS | CREATE_NEW_PROCESS_GROUP
    subprocess.Popen([str(path), "/VERYSILENT", "/SUPPRESSMSGBOXES", "/NORESTART", "/CURRENTUSER"],
                     creationflags=flags, close_fds=True)
    log.info("Mise à jour : installateur %s lancé", path.name)
