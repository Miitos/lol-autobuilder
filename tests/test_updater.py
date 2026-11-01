"""Mise à jour : comparaison de versions, lecture de la release, téléchargement vérifié (aucun réseau)."""

from __future__ import annotations

import hashlib

import httpx
import pytest

from lolbuilds import updater

SETUP = b"MZ faux installateur"
PORTABLE = b"MZ faux exe portable"


def github(tag: str = "v0.2.0", setup: bytes = SETUP, sums_hash: str | None = None, with_installer: bool = True,
           portable: bytes = PORTABLE, portable_hash: str | None = None):
    digest = sums_hash or hashlib.sha256(setup).hexdigest()
    portable_digest = portable_hash or hashlib.sha256(portable).hexdigest()
    assets = [{"name": "SHA256SUMS.txt", "browser_download_url": "https://dl/SHA256SUMS.txt"},
              {"name": "lolbuilds-portable-0.2.0.exe", "browser_download_url": "https://dl/portable.exe"}]
    if with_installer:
        assets.append({"name": "lolbuilds-setup-0.2.0.exe", "browser_download_url": "https://dl/setup.exe"})

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/releases/latest"):
            return httpx.Response(200, json={"tag_name": tag, "body": "Nouveautés", "assets": assets,
                                             "html_url": "https://github.com/Miitos/lol-autobuilder/releases/tag/" + tag})
        if request.url.path == "/SHA256SUMS.txt":
            return httpx.Response(200, text=f"{digest}  lolbuilds-setup-0.2.0.exe\n{portable_digest}  lolbuilds-portable-0.2.0.exe\n")
        if request.url.path == "/portable.exe":
            return httpx.Response(200, content=portable)
        if request.url.path == "/setup.exe":
            return httpx.Response(200, content=setup)
        return httpx.Response(404)

    return httpx.Client(transport=httpx.MockTransport(handler))


def test_parse_version():
    assert updater.parse_version("v1.10.2") > updater.parse_version("1.9.9")
    assert updater.parse_version("0.2.0-beta") == (0, 2, 0)


def test_check_newer_release():
    info = updater.check("0.1.0", http=github())
    assert info["newer"] and info["latest"] == "0.2.0"
    assert info["installer"]["name"] == "lolbuilds-setup-0.2.0.exe" and info["notes"] == "Nouveautés"


def test_check_up_to_date():
    assert not updater.check("0.2.0", http=github())["newer"]


def test_check_network_error():
    client = httpx.Client(transport=httpx.MockTransport(lambda r: httpx.Response(503)))
    with pytest.raises(updater.UpdateError):
        updater.check("0.1.0", http=client)


def test_download_verified(tmp_path):
    client = github()
    path = updater.download_installer(updater.check("0.1.0", http=client), tmp_path, http=client)
    assert path.read_bytes() == SETUP


def test_download_rejects_wrong_checksum(tmp_path):
    client = github(sums_hash="0" * 64)
    with pytest.raises(updater.UpdateError, match="empreinte"):
        updater.download_installer(updater.check("0.1.0", http=client), tmp_path, http=client)
    assert not (tmp_path / "lolbuilds-setup-0.2.0.exe").exists()


def test_release_without_installer(tmp_path):
    client = github(with_installer=False)
    info = updater.check("0.1.0", http=client)
    assert info["installer"] is None
    with pytest.raises(updater.UpdateError):
        updater.download_installer(info, tmp_path, http=client)


def test_not_installed_from_sources():
    assert updater.installed() is False  # tests lancés depuis les sources, pas depuis l'exe installé


def test_check_finds_portable_exe():
    info = updater.check("0.1.0", http=github())
    assert info["portable"]["name"] == "lolbuilds-portable-0.2.0.exe"


def test_download_portable_verified(tmp_path):
    client = github()
    path = updater.download_portable(updater.check("0.1.0", http=client), tmp_path, http=client)
    assert path.read_bytes() == PORTABLE and path.parent == tmp_path


def test_download_portable_rejects_wrong_checksum(tmp_path):
    client = github(portable_hash="0" * 64)
    with pytest.raises(updater.UpdateError, match="empreinte"):
        updater.download_portable(updater.check("0.1.0", http=client), tmp_path, http=client)
    assert list(tmp_path.iterdir()) == []


def test_download_portable_rejects_non_exe(tmp_path):
    client = github(portable=b"<html>pas un exe</html>")
    with pytest.raises(updater.UpdateError, match="exécutable"):
        updater.download_portable(updater.check("0.1.0", http=client), tmp_path, http=client)
    assert list(tmp_path.iterdir()) == []


def test_release_without_portable(tmp_path):
    client = github()
    info = updater.check("0.1.0", http=client)
    info["portable"] = None
    with pytest.raises(updater.UpdateError):
        updater.download_portable(info, tmp_path, http=client)


def test_swap_exe_keeps_old_as_dot_old(tmp_path):
    current, new = tmp_path / "lolbuilds.exe", tmp_path / "update.download"
    current.write_bytes(b"MZ ancien")
    new.write_bytes(b"MZ nouveau")
    updater.swap_exe(current, new)
    assert current.read_bytes() == b"MZ nouveau"
    assert (tmp_path / "lolbuilds.exe.old").read_bytes() == b"MZ ancien" and not new.exists()
    # une mise à jour suivante écrase l'ancien .old
    new.write_bytes(b"MZ encore plus recent")
    updater.swap_exe(current, new)
    assert (tmp_path / "lolbuilds.exe.old").read_bytes() == b"MZ nouveau"


def test_swap_exe_restores_on_failure(tmp_path):
    current = tmp_path / "lolbuilds.exe"
    current.write_bytes(b"MZ ancien")
    with pytest.raises(OSError):
        updater.swap_exe(current, tmp_path / "absent")
    assert current.read_bytes() == b"MZ ancien"


def test_no_automatic_update_from_sources():
    assert updater.mode() is None and updater.cleanup_previous() is True
