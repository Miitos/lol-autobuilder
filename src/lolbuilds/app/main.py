"""Lance l'interface : serveur local + fenêtre de bureau (pywebview).

Usage :
    pythonw -m lolbuilds.app             # fenêtre, suit le client League
    python  -m lolbuilds.app --demo      # faux champ select, rien n'est écrit dans le client
    python  -m lolbuilds.app --demo-builds   # vrai client, builds d'exemple (écrit dans le client !)
    python  -m lolbuilds.app --browser   # ouvre l'UI dans le navigateur au lieu d'une fenêtre
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import os
import sys
import threading
import time
import tomllib
import urllib.request
import webbrowser
from collections.abc import Sequence
from pathlib import Path
from typing import Any

from .. import updater
from ..config import Config, default_config_path, load_config, setup_logging
from .server import UI_DIR, Controller, start_server
from .winstate import fullscreen_app_active

log = logging.getLogger("lolbuilds.app")


def _set_top_most(window: Any, on_top: bool) -> None:
    """« Toujours au premier plan », exécuté sur le thread de la fenêtre.

    `window.on_top = …` de pywebview modifie `TopMost` directement depuis le thread qui appelle (ici celui de
    l'API JS), alors que WinForms n'accepte de toucher à une fenêtre que depuis son propre thread : l'appli
    plantait au clic sur l'épingle. Les autres actions de pywebview (réduire, restaurer…) passent, elles,
    par `Invoke`, comme ici."""
    from System import Func, Type  # type: ignore[import-not-found]  # pythonnet : Windows seulement
    from webview.platforms import winforms

    form = winforms.BrowserView.instances.get(window.uid)
    if form is None:
        return

    def apply() -> None:
        form.TopMost = on_top

    form.Invoke(Func[Type](apply))


class PinController:
    """« Toujours au premier plan » (l'épingle) et ramener la fenêtre devant pour l'alerte, sans gêner un jeu.

    Un jeu en plein écran garde la priorité : tant qu'il est au premier plan, l'épingle est suspendue (la fenêtre
    ne passe pas par-dessus, ne lui vole pas le focus) et elle revient toute seule quand il est fermé. Le son, lui,
    est joué par la page et ne dépend pas de la fenêtre : il se fait entendre dans tous les cas."""

    def __init__(self, set_top_most: Any, busy: Any) -> None:
        self._set_top_most = set_top_most  # (bool) -> None, sur le thread de la fenêtre
        self._busy = busy  # () -> bool : une appli plein écran a la main
        self.pinned = False  # voulu par l'utilisateur
        self.suspended = False  # épinglée, mais mise en retrait pour un jeu plein écran
        self._lock = threading.Lock()

    def toggle(self) -> bool:
        with self._lock:
            want = not self.pinned
            if want and not self._busy():
                self._set_top_most(True)
            elif not want and not self.suspended:
                self._set_top_most(False)
            self.pinned = want
            self.suspended = want and self._busy()
            return self.pinned

    def poll(self) -> None:
        """Appelé régulièrement : suspend l'épingle devant un jeu plein écran, la rétablit ensuite."""
        with self._lock:
            if not self.pinned:
                return
            busy = self._busy()
            if busy and not self.suspended:
                self._set_top_most(False)
                self.suspended = True
            elif not busy and self.suspended:
                self._set_top_most(True)
                self.suspended = False

    def bring_to_front(self) -> bool:
        """Passage bref au premier plan (Windows refuse de voler le focus autrement). Faux si un jeu a la main."""
        with self._lock:
            if self._busy():
                return False
            if self.pinned:
                return True  # déjà au premier plan
            self._set_top_most(True)
        time.sleep(0.4)
        with self._lock:
            if not self.pinned:
                self._set_top_most(False)
        return True


class WindowApi:
    """Méthodes appelables depuis la page (window.pywebview.api.*) pour la barre de titre maison."""

    def __init__(self) -> None:
        self._window: Any = None
        self.pin = PinController(self._top_most, fullscreen_app_active)

    def _top_most(self, on_top: bool) -> None:
        _set_top_most(self._window, on_top)

    def start_watching(self) -> None:
        """Surveille les jeux plein écran (épingle suspendue puis rétablie)."""
        def loop() -> None:
            while True:
                time.sleep(1.5)
                try:
                    self.pin.poll()
                except Exception:  # noqa: BLE001
                    log.exception("Surveillance du plein écran")

        threading.Thread(target=loop, name="lolbuilds-pin", daemon=True).start()

    def minimize(self) -> None:
        self._window.minimize()

    def close(self) -> None:
        self._window.destroy()

    def toggle_on_top(self) -> bool:
        try:
            return self.pin.toggle()
        except Exception:  # noqa: BLE001  l'épingle ne doit jamais faire tomber l'appli
            log.exception("Impossible de changer « toujours au premier plan »")
            return self.pin.pinned

    def focus(self) -> None:
        """Ramène la fenêtre devant le client League (c'est au joueur de bannir ou de choisir)."""
        if fullscreen_app_active():
            log.info("Alerte : un jeu plein écran a la main, la fenêtre reste derrière (le son est joué)")
            return
        try:
            self._window.restore()
            self.pin.bring_to_front()
        except Exception:  # noqa: BLE001
            log.exception("Impossible de ramener la fenêtre au premier plan")


def _alert(text: str) -> None:
    """Boîte de message Windows : sans console (exe, pythonw), un log seul passerait inaperçu."""
    if sys.platform == "win32":
        import ctypes

        ctypes.windll.user32.MessageBoxW(None, text, "lolbuilds", 0x40)


def _cleanup_previous() -> None:
    for _ in range(15):
        if updater.cleanup_previous():
            return
        time.sleep(2)


def serve_in_thread(ctrl: Controller, host: str, port: int) -> threading.Event:
    ready = threading.Event()
    errors: list[BaseException] = []

    def target() -> None:
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        try:
            loop.run_until_complete(start_server(ctrl, host, port))
        except BaseException as exc:  # port déjà pris, etc.
            errors.append(exc)
            ready.set()
            return
        ready.set()
        loop.run_forever()

    threading.Thread(target=target, name="lolbuilds-server", daemon=True).start()
    ready.wait(15)
    if errors:
        raise errors[0]
    return ready


REQUIRED_UI_FILES = ("index.html", "audio/yume-no-mukou-e.mp3", "fonts/fonts.css")


def check_gui() -> int:
    """Auto-test de la CI : la couche fenêtre (pywebview + pythonnet) se charge-t-elle dans l'exe construit ?

    C'est exactement l'import qui plantait au démarrage (« Failed to resolve Python.Runtime.Loader.Initialize »).
    Sans console, le résultat passe par le code de sortie et un fichier texte."""
    import tempfile
    import traceback

    report = Path(tempfile.gettempdir()) / "lolbuilds-check-gui.txt"
    try:
        import webview  # noqa: F401
        import webview.platforms.winforms  # noqa: F401  (importe pythonnet : le point qui plantait)
    except BaseException:  # noqa: BLE001
        report.write_text(traceback.format_exc(), encoding="utf-8")
        return 1
    # Données embarquées : un exe sans interface ou sans polices démarrerait quand même, mais vide.
    missing = [rel for rel in REQUIRED_UI_FILES if not (UI_DIR / rel).is_file()]
    if missing:
        report.write_text(f"fichiers d'interface manquants dans {UI_DIR} : {', '.join(missing)}", encoding="utf-8")
        return 1
    report.write_text("ok", encoding="utf-8")
    return 0


def _start_server(ctrl: Controller, port: int, retry: bool) -> None:
    """Démarre le serveur local ; après une mise à jour, l'ancien processus libère le port dans les secondes qui suivent."""
    deadline = time.monotonic() + (20 if retry else 0)
    while True:
        try:
            serve_in_thread(ctrl, "127.0.0.1", port)
            return
        except OSError:
            if time.monotonic() >= deadline:
                raise
            time.sleep(0.5)


PORT_FALLBACKS = 20  # ports essayés après celui demandé (8766, 8767…) s'il est occupé par autre chose


def _existing_instance(port: int) -> bool | None:
    """Le port est-il tenu par lolbuilds ? True : sa fenêtre a été ramenée devant ; None : c'est lolbuilds mais une
    version qui ne sait pas le faire ; False : autre chose (autre logiciel, plage de ports réservée par Windows)."""
    base = f"http://127.0.0.1:{port}"
    try:
        with urllib.request.urlopen(base + "/", timeout=2) as resp:
            if b"<title>lolbuilds</title>" not in resp.read(4096):
                return False
    except Exception:  # noqa: BLE001  (rien n'écoute vraiment : port réservé, pare-feu…)
        return False
    try:
        urllib.request.urlopen(urllib.request.Request(base + "/api/focus", data=b"{}", method="POST"), timeout=2).close()
        return True
    except Exception:  # noqa: BLE001
        return None


def _start_on_free_port(ctrl: Controller, port: int) -> int | None:
    for candidate in range(port + 1, port + 1 + PORT_FALLBACKS):
        try:
            serve_in_thread(ctrl, "127.0.0.1", candidate)
            return candidate
        except OSError:
            continue
    return None


def run(args: argparse.Namespace) -> int:
    if args.check_gui:
        return check_gui()
    try:
        cfg = load_config(args.config)
    except (tomllib.TOMLDecodeError, ValueError, OSError) as exc:
        # Sans console, l'exe fenêtré mourrait sans rien dire : on prévient et on démarre avec les réglages par défaut.
        cfg = Config()
        try:
            setup_logging(cfg.cache_dir, "app", args.verbose)
        except Exception:  # noqa: BLE001
            pass
        log.error("config.toml invalide (%s) : réglages par défaut", exc)
        _alert(f"config.toml invalide : {exc}\n\nlolbuilds démarre avec les réglages par défaut.")
    else:
        setup_logging(cfg.cache_dir, "app", args.verbose)
    # Supprime l'ancien exe laissé par la dernière mise à jour portable (il se libère après quelques secondes).
    threading.Thread(target=_cleanup_previous, name="lolbuilds-cleanup", daemon=True).start()
    ctrl = Controller(cfg, demo=args.demo, demo_builds=args.demo_builds)
    port = args.port
    try:
        _start_server(ctrl, port, args.after_update)
    except OSError as exc:
        running = _existing_instance(port)
        if running is not False:
            # lolbuilds tourne déjà : sa fenêtre revient devant (les anciennes versions ne savent pas, on prévient).
            log.info("lolbuilds tourne déjà sur le port %d : on ramène sa fenêtre", port)
            if running is None and not args.browser:
                _alert("lolbuilds est déjà ouvert.\n\nCherche sa fenêtre dans la barre des tâches.")
            if args.browser:
                webbrowser.open(f"http://127.0.0.1:{port}/")
            return 0
        # Une instance déjà lancée sur un port de repli ? On la ramène devant plutôt que d'en ouvrir une seconde.
        for candidate in range(port + 1, port + 1 + PORT_FALLBACKS):
            if _existing_instance(candidate) is not False:
                log.info("lolbuilds tourne déjà sur le port %d : on ramène sa fenêtre", candidate)
                if args.browser:
                    webbrowser.open(f"http://127.0.0.1:{candidate}/")
                return 0
        # Port pris par autre chose (souvent une plage réservée par Windows avec Hyper-V, WSL ou Docker) : un autre.
        free = _start_on_free_port(ctrl, port)
        if free is None:
            log.error("Ports %d à %d indisponibles (%s)", port, port + PORT_FALLBACKS, exc)
            _alert(f"lolbuilds n'a trouvé aucun port libre entre {port} et {port + PORT_FALLBACKS}.\n\n"
                   "Un autre logiciel les occupe : redémarre le PC puis relance lolbuilds.")
            return 1
        log.warning("Port %d occupé par un autre logiciel (%s) : interface sur le port %d", port, exc, free)
        port = free
    url = f"http://127.0.0.1:{port}/"
    log.info("Interface sur %s%s", url, " (démo)" if args.demo else "")

    if args.browser or args.server_only:
        if args.browser:
            webbrowser.open(url)
        threading.Event().wait()  # jusqu'à Ctrl+C
        return 0

    # Sons d'alerte sans clic préalable : WebView2 applique sinon la politique d'autoplay de Chromium.
    # Et sans bridage quand la fenêtre est cachée, réduite ou recouverte par un jeu plein écran : l'alerte sonore
    # doit retentir même si la page n'est pas visible.
    os.environ.setdefault(
        "WEBVIEW2_ADDITIONAL_BROWSER_ARGUMENTS",
        "--autoplay-policy=no-user-gesture-required --disable-background-timer-throttling "
        "--disable-renderer-backgrounding --disable-backgrounding-occluded-windows",
    )
    import webview  # import local : seulement pour le mode fenêtre

    api = WindowApi()
    api.start_watching()
    ctrl.on_focus = api.focus  # une seconde instance lancée par le joueur ramène cette fenêtre devant
    api._window = webview.create_window(
        "lolbuilds", url + "?desktop=1", js_api=api, width=1240, height=800, min_size=(960, 640),
        frameless=True, easy_drag=False, background_color="#0A0D13",
    )
    webview.start()  # profil temporaire : les préférences sont côté serveur
    return 0


def parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    p = argparse.ArgumentParser(prog="lolbuilds-app", description=__doc__.splitlines()[0])
    p.add_argument("--config", type=Path, default=default_config_path())
    p.add_argument("--port", type=int, default=8765)
    p.add_argument("--demo", action="store_true", help="faux champ select avec builds d'exemple")
    p.add_argument("--demo-builds", action="store_true",
                   help="vrai client League, mais avec les builds d'exemple (avant le premier prefetch)")
    p.add_argument("--browser", action="store_true", help="ouvre l'UI dans le navigateur")
    p.add_argument("--server-only", action="store_true", help="serveur seul, sans fenêtre")
    p.add_argument("--check-gui", action="store_true", help="vérifie que la couche fenêtre se charge, puis quitte (CI)")
    p.add_argument("--after-update", action="store_true", help=argparse.SUPPRESS)  # relance par la mise à jour portable
    p.add_argument("-v", "--verbose", action="store_true")
    return p.parse_args(argv)


def main(argv: Sequence[str] | None = None) -> None:
    sys.exit(run(parse_args(argv)))


if __name__ == "__main__":
    main()
