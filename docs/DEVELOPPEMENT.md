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

## Tout télécharger, plein écran et épingle

- **Réglages › Builds en cache › Tout télécharger** appelle `POST /api/prefetch` : `LiveBuilds.prefetch_all` (même logique que `python -m lolbuilds.prefetch`) tourne en tâche de fond, la progression passe par `state["prefetch"]` (`idle | running | done | error`). Un build est « à jour » s'il date d'aujourd'hui (`BuildCache.is_fresh`) : seuls les autres sont retéléchargés, et en cas d'erreur l'ancien fichier est conservé.
- **Épingle et alerte** (`app/main.py`, `PinController`) : `TopMost` n'est modifié que sur le thread de la fenêtre (`Invoke`) ; `window.on_top = …` de pywebview le fait depuis le thread appelant et faisait planter l'appli. `app/winstate.py` interroge `SHQueryUserNotificationState` : quand une appli plein écran (ou un jeu Direct3D exclusif) a la main, l'épingle est suspendue, la fenêtre ne prend pas le focus, et tout revient quand le jeu se ferme. Le son vient de la page (Web Audio) : WebView2 est lancé sans bridage des fenêtres cachées ou recouvertes pour qu'il retentisse dans tous les cas.

## ARAM et build « atypique »

- OP.GG a des stats d'ARAM à part : `lol_get_champion_analysis` avec `game_mode = "aram"` (la lane est ignorée ; l'outil exige quand même un `position`, on envoie `mid`). `OpggSource._fetch_aram` en fait un `Build` de pseudo-rôle `ARAM` (`aram` dans `__init__.py`, hors de `ROLES` : ce n'est pas une lane), enregistré dans `{Champion}_aram.json`, avec un item set pour la carte 12 (Abîme hurlant, sans quoi il n'apparaît pas dans la boutique d'ARAM). Les builds ARAM ne sont pas dans la bibliothèque ni dans `builds_for_champion` (ils ne doivent jamais servir de « build du rôle »).
- **Détection** : carte de la file (`/lol-gameflow/v1/session`, `map.id` 12 = Abîme hurlant), lue au début du champ select → `champSelect.mode = "aram"`. Si la carte est illisible, `benchEnabled` (banc de remplaçants) sert de secours ; l'ARURF active aussi le banc, mais sur la Faille. En ARAM, `Controller._live_refresh_aram` charge le build du jour (cache d'une journée comme les autres) ; pas de lane, d'adversaire, de bans, de bons picks ni de Disco pick.
- **En partie** : les joueurs viennent de la Live Client Data API dès `GameStart`. Pendant l'écran de chargement, elle répond 404 avec un JSON d'erreur : `players.live_players` vérifie le statut et les types, puis réessaie. En ARAM (pas de postes), les joueurs sont affichés face à face dans l'ordre de la partie, sans l'indication « peu joué » (champion tiré au sort).
- **Atypique** (préférence `atypical` de l'UI, désactivée par défaut ; sans elle, aucune requête ARAM hors ARAM) : hors ARAM, `Controller._atypical` garde la première proposition du build ARAM du champion, étiquetée « Atypique : build ARAM », ajoutée **en dernier** dans les propositions (jamais appliquée par défaut, hors des préférences mémorisées) et ignorée si elle a la même keystone, le même arbre secondaire et les mêmes items core qu'une proposition existante.
- **Coût en requêtes OP.GG** : une requête par champion et par jour (puis le cache). « Tout télécharger » et `python -m lolbuilds.prefetch` ajoutent un build ARAM par champion (`OpggSource.aram_targets`), soit environ une requête de plus par champion (de l'ordre de +60 % de requêtes). `[opgg] aram = false` désactive tout.

## Item sets et taille des requêtes

Le `PUT .../sets` du client remplace **tous** les item sets du compte : on renvoie donc ceux de l'utilisateur, plus le nôtre. Le client répond `413 BAD_REQUEST_HEADERS « Content length is too large »` quand le corps est trop gros (un compte avec beaucoup d'item sets d'autres applis). `lcu.encode_body` envoie du JSON compact en UTF-8 (aiohttp `json=` ajoute des espaces et écrit chaque accent en `\uXXXX`), et `BuildApplier.problems` garde la raison lisible de chaque étape en échec, affichée sous la barre « Appliqué ». Si le corps reste trop gros, c'est à l'utilisateur de supprimer des item sets : on ne supprime jamais ceux qui ne sont pas à nous (préfixe `[Auto]`).
