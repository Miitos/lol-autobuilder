"""Démarrage : config invalide et contrôle des fichiers d'interface."""
from __future__ import annotations

from lolbuilds.app import main as app_main
from lolbuilds.app.server import UI_DIR


def test_required_ui_files_exist():
    assert all((UI_DIR / rel).is_file() for rel in app_main.REQUIRED_UI_FILES)


def test_invalid_config_alerts_and_falls_back(tmp_path, monkeypatch):
    bad = tmp_path / "config.toml"
    bad.write_text("ceci = [pas du toml", encoding="utf-8")
    alerts: list[str] = []
    monkeypatch.setattr(app_main, "_alert", alerts.append)
    monkeypatch.setattr(app_main, "setup_logging", lambda *a, **k: None)

    class Stop(Exception):
        pass

    def boom(*a, **k):
        raise Stop

    monkeypatch.setattr(app_main, "Controller", boom)
    args = app_main.parse_args(["--config", str(bad)])
    try:
        app_main.run(args)
    except Stop:
        pass
    assert alerts and "config.toml invalide" in alerts[0]
