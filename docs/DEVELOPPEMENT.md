# lolbuilds : guide développeur

Le [README](../README.md) s'adresse aux joueurs. Ce document décrit le code, l'installation depuis les sources et la publication d'une version.

## Architecture

```
src/lolbuilds/
  sources/base.py            interface BuildSource (prepare / fetch_build / main_role)
  sources/opgg.py            source par défaut : serveur MCP d'OP.GG
  ddragon.py                 Data Dragon (champions, items, runes, sorts) en cache
  live.py                    récupération au pick + détection de l'adversaire, cache d'une journée
  realms.py                  visuels des thèmes de région (site Univers de Riot)
  targets.py                 filtre --only
  cache.py                   un fichier {Champion}_{role}.json, écriture atomique
  prefetch.py                job nocturne (optionnel)
  assets.py                  images et textes fr de l'interface (Data Dragon)
  aggregation.py             agrégation de parties (mode démo uniquement)
  app/                       interface : serveur local aiohttp + fenêtre pywebview
    lcu.py                   connexion LCU asynchrone (REST + WebSocket)
    applier.py               application d'un build : runes, sorts, item set
    server.py                état du champ select, actions, WebSocket vers la page
    demo.py                  mode démo
    ui/                      HTML/CSS/JS, polices embarquées
packaging/                   exe en dossier + exe portable seul (PyInstaller), installateur (Inno Setup), icône, notes de version
.github/workflows/           tests à chaque push, build + release sur un tag vX.Y.Z
install_task.ps1             tâches planifiées (installation depuis les sources)
```

### D'où viennent les builds

1. `lol_list_lane_meta_champions` (un appel par jour) donne, pour chaque champion, la part de ses parties dans chaque rôle. Ça sert à choisir le rôle quand le client n'en donne pas (blind, ARAM) et à deviner la lane des ennemis. Un rôle est proposé s'il dépasse `min_role_rate` (10 %) ; le rôle principal l'est toujours.
2. `lol_get_champion_analysis` (un appel par champion/rôle) donne la page de runes, les sorts, les items de départ, core, bottes et les options suivantes, chacun avec ses parties et victoires.
3. `lol_get_lane_matchup_guide` (un appel par duel) donne, contre un adversaire précis, 5 pages de runes, 5 paires de sorts et 15 chemins d'items. Les pages qui ne diffèrent que par les shards sont regroupées ; les 2 meilleures par winrate (pick ≥ 5 %, 50 parties) sont proposées, la plus jouée toujours incluse. Son champ `counters` (résultats du champion contre tous ses adversaires) sert aux « bons picks » et aux bans suggérés.
4. Les réponses sont relues sans exécuter de code (`ast`), converties en `Build` et écrites dans le cache `~/.lolbuilds/`. En cas d'erreur, **le dernier build connu est conservé**. Tout ce qui est récupéré est gardé jusqu'au lendemain.

### Ce que fait l'interface en champ select

- Elle utilise `assignedPosition`. Si ce champ est vide (blind, ARAM…), elle prend le rôle principal du champion sur OP.GG.
- Avec Auto, le build est appliqué une fois le champion verrouillé, puis remplacé par le meilleur build contre l'adversaire quand il arrive, sauf si l'utilisateur a choisi un build à la main.
- **Runes** : suppression des pages `[Auto]…`, puis création d'une nouvelle page active. Si toutes les pages sont prises par des pages perso, rien n'est supprimé.
- **Sorts** : `PATCH my-selection`, Flash sur la touche choisie.
- **Item set** : relecture de tous les sets du compte, remplacement uniquement de ceux dont le titre commence par `[Auto]`. Le `PUT` du LCU remplace *tous* les sets : si la lecture échoue, rien n'est écrit.

### Ajouter une autre source

```python
class MaSource(BuildSource):
    name = "ma-source"
    def fetch_build(self, champion: str, role: str) -> Build: ...
```

Il suffit ensuite de l'instancier dans `prefetch.make_source` et `live.LiveBuilds`.

## Installation depuis les sources

Python 3.11 à 3.13 (`aiohttp` et `pywebview` ne sont pas garantis sur 3.14+).

```bash
py -3.13 -m venv .venv
```
```bash
.venv\Scripts\pip install -e .[dev]
```
```bash
.venv\Scripts\python -m lolbuilds.app
```

| Commande | Effet |
|---|---|
| `python -m lolbuilds.app` | fenêtre de bureau |
| `python -m lolbuilds.app --demo` | faux champ select avec des builds d'exemple ; rien n'est envoyé au client |
| `python -m lolbuilds.app --demo-builds` | vrai client, builds d'exemple (sans OP.GG) |
| `python -m lolbuilds.app --browser` | interface dans le navigateur (http://127.0.0.1:8765) |
| `python -m lolbuilds.prefetch` | met à jour tous les builds périmés (cache d'un autre jour) |
| `python -m lolbuilds.prefetch --only Thresh:utility --force` | un seul champion/rôle, en ignorant le cache |

**Configuration** (optionnelle) : `config.toml` à la racine du projet (copie de `config.example.toml`) ; pour l'exe installé, `%USERPROFILE%\.lolbuilds\config.toml`. On y règle l'elo des stats (`tier`), la récupération au pick (`[opgg] live`, `matchup`), le prefetch nocturne (`[prefetch] nightly`) et les étapes appliquées (`[champselect] apply_*`).

**Planification** (depuis les sources) : `powershell -ExecutionPolicy Bypass -File install_task.ps1 -WithApp` lance l'interface à l'ouverture de session, plus le prefetch à 4 h (inactif tant que `nightly = false`).

Logs : `%USERPROFILE%\.lolbuilds\logs\app.log` et `prefetch.log`.

## Tests

```bash
.venv\Scripts\python -m pytest
```

Réponses OP.GG enregistrées, fixtures Data Dragon, faux clients LCU et HTTP : **aucun appel réseau**. La CI les lance à chaque push sur `main` (`.github/workflows/ci.yml`).

## Construire l'exe et l'installateur

En local :

```bash
.venv\Scripts\pip install -e .[build]
```
```bash
.venv\Scripts\pyinstaller --noconfirm --clean packaging\lolbuilds.spec
```
```bash
.venv\Scripts\pyinstaller --noconfirm --clean packaging\lolbuilds-portable.spec
```

L'exe en dossier est dans `dist\lolbuilds\lolbuilds.exe` (dossier complet, ~45 Mo) ; l'exe **portable** (un seul fichier) est `dist\lolbuilds-portable.exe`. `lolbuilds-portable.exe --check-gui` vérifie que la couche fenêtre se charge (code de sortie 0, détail dans `%TEMP%\lolbuilds-check-gui.txt`) : la CI le lance sur les deux exe avant de publier. L'installateur se construit avec [Inno Setup 6](https://jrsoftware.org/isinfo.php) : `iscc /DAppVersion=0.1.0 packaging\installer.iss` (sortie dans `dist\installer\`). L'icône se régénère avec `python packaging\make_icon.py` (Pillow).

## Publier une version

1. Mettre à jour `version` dans `pyproject.toml` et `__version__` dans `src/lolbuilds/__init__.py`, puis `packaging/RELEASE_NOTES.md`.
2. Commit, puis tag :
   ```bash
   git tag v0.2.0
   ```
   ```bash
   git push origin main v0.2.0
   ```
3. La CI (`.github/workflows/release.yml`) vérifie que le tag correspond à la version, lance les tests, construit l'exe, l'installateur, l'exe portable (et vérifie qu'ils démarrent) et les empreintes SHA-256, puis publie la release GitHub. Lancée à la main (onglet Actions), elle construit seulement et dépose les fichiers en artefacts.

## Limites connues

- **Adversaire deviné** : sur un champion polyvalent, la lane devinée peut être fausse ; un clic sur le bon ennemi corrige.
- **Serveur MCP d'OP.GG** : service public sans clé, dont le format et la disponibilité peuvent changer. En cas d'erreur, les builds de la veille restent en cache.
- **Endpoints LCU non officiels** : ils peuvent changer entre deux patchs. Chaque étape (runes, sorts, item set) est isolée et les erreurs sont loguées.
- **Exe non signé** : Windows SmartScreen affiche un avertissement au premier lancement (voir le README).

## Mise à jour automatique

`updater.py` choisit le mode d'après l'exe en cours : **installé** (`unins000.exe` à côté) → l'installateur de la release est lancé en silencieux ; **portable** (exe seul) → le nouvel exe est téléchargé dans le même dossier, vérifié (SHA-256 de `SHA256SUMS.txt`), puis échangé avec l'exe en cours (Windows interdit de l'écraser mais accepte de le renommer : l'ancien devient `lolbuilds-portable-….exe.old`, supprimé au démarrage suivant) et relancé avec `--after-update`, qui attend que l'ancien processus libère le port. Depuis les sources : pas de mise à jour automatique. Les assets de la release sont reconnus par leur nom (`lolbuilds-setup-*.exe`, `lolbuilds-portable-*.exe`, `SHA256SUMS.txt`).
