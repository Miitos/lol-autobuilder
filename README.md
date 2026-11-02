# lolbuilds

**Tes runes, tes sorts et ton item set prêts tout seuls en champ select League of Legends, à partir des stats d'OP.GG.**

Une alternative légère à Blitz ou Porofessor, centrée sur une seule chose : quand tu choisis ton champion, lolbuilds crée la bonne page de runes, règle tes sorts d'invocateur et ajoute un item set dans la boutique. Sans publicité, sans compte, sans clé API.

## Ce que ça fait

- **Build automatique** : dès que tu verrouilles ton champion, la page de runes, les sorts (Flash sur D ou F, au choix) et l'item set sont envoyés au client.
- **Builds contre ton adversaire** : quand ton adversaire de lane est connu, lolbuilds propose les runes et les items qui gagnent le plus dans ce duel précis.
- **Bons picks** : si ton adversaire a choisi avant toi, tu vois les champions qui le battent, parmi ceux que tu possèdes.
- **Bans suggérés** : pendant ton tour de ban, les champions les plus dangereux pour toi ou les plus forts de ta lane. Un clic sur l'un d'eux le bannit directement (ça ne touche jamais à ton pick).
- **Alerte quand c'est ton tour** : la fenêtre repasse devant et un son est joué au moment de bannir ou de choisir.
- **Les joueurs de la partie** : une fois la partie chargée, le rang, le winrate de la saison et les résultats de chaque joueur (ton équipe et l'équipe adverse) sur son champion (OP.GG), avec des indications : série de victoires, main du champion, peu d'expérience dessus…
- **Ton vis-à-vis** : ses dernières parties (victoires, KDA, combien sur son champion) et la lecture du duel par OP.GG (avantage en lane, en solo kills, conseil). Le build de ton champion reste affiché pendant la partie.
- **ARAM** : en ARAM, lolbuilds applique un build issu des **statistiques d'ARAM** d'OP.GG (runes, sorts, items, item set pour l'Abîme hurlant), et plus celui d'une lane de la faille.
- **Build atypique** (à activer dans les Réglages) : dans les autres modes, le build ARAM du champion est proposé en dernier, avec l'étiquette « Atypique : build ARAM » (jamais appliqué par défaut). Il est ignoré s'il ressemble déjà à une proposition.
- **Disco pick** (à activer dans les Réglages) : pendant ton tour de pick, un bouton lance une roulette eurobeat (musique, lumières, compte-tours) qui tire au sort un champion que tu possèdes dans ta lane, son build et tes sorts, puis le verrouille dès l'arrêt de la roulette (on assume !). Rien n'apparaît dans League avant la fin de la roulette, qui accélère s'il reste moins de 5 secondes pour choisir. Pas de Disco pick en ARAM. Musique livrée : « 夢の向こうへ » (créée avec Suno par progressivesubtleties471). Pour mettre ton propre morceau, pose un fichier `disco-music.mp3` dans le dossier `%USERPROFILE%\.lolbuilds`.
- **Thèmes** aux couleurs de Runeterra : Hextech, Noxus, Piltover et Ionia.

Les statistiques viennent d'OP.GG (parties classées Émeraude et plus).

## Installation

1. Va sur la page des **[téléchargements](https://github.com/Miitos/lol-autobuilder/releases/latest)**.
2. Télécharge **`lolbuilds-setup-….exe`** (dans la partie *Assets*).
3. Ouvre le fichier et suis l'installation. Aucun droit administrateur n'est demandé.

Il faut **Windows 10 ou 11** (64 bits) et le client League of Legends.

### « Windows a protégé votre ordinateur »

Au premier lancement, Windows peut afficher cet avertissement : l'application est gratuite et n'est pas signée par un éditeur payant, donc Windows ne la connaît pas encore.

Clique sur **Informations complémentaires**, puis sur **Exécuter quand même**. Tu peux aussi vérifier le fichier avec les empreintes `SHA256SUMS.txt` publiées à côté.

### Version portable

Tu préfères ne rien installer ? Télécharge **`lolbuilds-portable-….exe`** : un seul fichier, à mettre où tu veux (clé USB, bureau…) et à lancer. Pas de zip à décompresser. Il se met à jour tout seul, comme la version installée.

## Utilisation

1. Lance **lolbuilds** (menu Démarrer ou raccourci du bureau), avant ou après le client League.
2. Lance une partie. Pendant la sélection des champions, lolbuilds affiche les builds du champion que tu survoles.
3. Verrouille ton champion : avec l'option **Auto** activée (barre du haut), le meilleur build est appliqué tout seul. Sinon, clique sur la proposition de ton choix (ou appuie sur `1`, `2` ou `3`).

Le premier champion de la journée met une à deux secondes à arriver : lolbuilds interroge OP.GG, puis garde le résultat jusqu'au lendemain.

**Astuce** : l'épingle en haut à droite garde la fenêtre au-dessus du client pendant le champ select. Si tu joues en plein écran, le jeu garde toujours la priorité : l'épingle se met en retrait tant qu'il est au premier plan (et revient ensuite), la fenêtre ne lui vole pas le focus, et le son d'alerte retentit quand même.

## Réglages

Tout se règle dans **Réglages** (icône d'engrenage à gauche) :

| Réglage | Effet |
|---|---|
| Application automatique | applique le meilleur build dès que ton champion est verrouillé |
| Alertes | fenêtre au premier plan et son quand c'est ton tour de bannir ou de choisir ; un son au choix pour chaque moment |
| Builds en cache | « Tout télécharger » prépare les builds de tous les champions d'un coup (quelques minutes) et indique la date de la dernière récupération |
| Build atypique (ARAM) | désactivé par défaut ; activé, le build ARAM du champion est proposé en dernier en partie normale, jamais appliqué tout seul |
| Sorts d'invocateur | désactivé : lolbuilds applique le build et les runes mais ne touche jamais à tes sorts (tu peux toujours choisir une paire à la main) |
| Touche de Flash | Flash sur D, sur F, ou là où le build le met |
| Thème | Hextech, Noxus, Piltover ou Ionia |
| Mise à jour | indique ta version et installe la nouvelle en un clic quand elle sort |

Au moment de l'installation, tu peux aussi cocher **Lancer lolbuilds au démarrage de Windows**.

## Mises à jour

Au démarrage, lolbuilds regarde s'il existe une nouvelle version. Si c'est le cas, une pastille apparaît sur **Réglages** : ouvre la section **Mise à jour** et clique sur **Mettre à jour**. L'installateur est téléchargé, vérifié, puis lolbuilds se ferme et se rouvre tout seul, réglages conservés.

La version portable se met à jour de la même façon : le nouvel exe est téléchargé à côté de l'ancien, vérifié, puis remplace l'ancien (le dossier où il se trouve doit donc être modifiable). Seule une installation depuis les sources ouvre la page de téléchargement.

> Tu as la 0.1.0 ou le zip portable de la 0.2.1 ? Ils n'ont pas ce bouton (le zip ne démarrait pas toujours) : installe la dernière version une fois depuis la [page des téléchargements](https://github.com/Miitos/lol-autobuilder/releases/latest), les suivantes arriveront toutes seules.

## Questions fréquentes

**Quand les builds sont-ils mis à jour ?**
Un build est « à jour » pour la journée : le premier pick d'un champion dans la journée le redemande à OP.GG, puis il est gardé jusqu'au lendemain. Il n'y a pas de rafraîchissement en arrière-plan : « Tout télécharger » (Réglages) met à jour d'un coup tous les builds qui ne datent pas d'aujourd'hui, et ceux que tu joues se mettent à jour d'eux-mêmes. Si OP.GG ne répond pas, le dernier build connu est gardé. La date de chaque build est indiquée dans la bibliothèque (en gris si elle n'est pas d'aujourd'hui) et sous le build en champ select.

**Comment l'ARAM est-il détecté ?**
Par la carte de la partie (l'Abîme hurlant). En partie, les joueurs des deux équipes s'affichent aussi en ARAM. Pour désactiver les builds ARAM et l'« Atypique », mets `aram = false` dans la section `[opgg]` de `config.toml`.

**Est-ce que ça touche à mes pages de runes ?**
lolbuilds crée ses propres pages, dont le nom commence par `[Auto]`, et ne supprime que celles-là. Si toutes tes pages sont occupées par tes pages personnelles, il n'en supprime aucune et te le signale. Même chose pour les item sets : les tiens ne sont jamais modifiés.

**Est-ce autorisé par Riot ?**
lolbuilds utilise l'interface locale du client League, comme Blitz, Porofessor ou Mobalytics : il agit seulement pendant la sélection des champions, ne lit pas la mémoire du jeu et ne fait rien en partie. Comme pour tout outil tiers, l'utilisation reste à tes risques.

**Rien ne s'affiche en champ select.**
Vérifie que le client League est bien lancé et que la pastille en haut indique « Client connecté ». Si le problème continue, ferme et relance lolbuilds.

**« lolbuilds est déjà ouvert ».**
L'application tourne déjà : cherche sa fenêtre dans la barre des tâches.

**Où sont mes données ?**
Builds en cache, réglages et journaux sont dans `%USERPROFILE%\.lolbuilds`. Les journaux (`logs\app.log`) aident à signaler un problème.

**Un problème ou une idée ?**
Ouvre un ticket dans l'onglet **[Issues](https://github.com/Miitos/lol-autobuilder/issues)** en joignant `logs\app.log` si possible.

## Désinstaller

Paramètres Windows › Applications › **lolbuilds** › Désinstaller. Pour tout effacer, supprime aussi le dossier `%USERPROFILE%\.lolbuilds`.

## Pour les développeurs

Code, architecture, installation depuis les sources et publication d'une version : voir **[docs/DEVELOPPEMENT.md](docs/DEVELOPPEMENT.md)**.

---

lolbuilds n'est ni approuvé ni soutenu par Riot Games et ne reflète pas les opinions de Riot Games ni de quiconque officiellement impliqué dans la production ou la gestion des propriétés de Riot Games. Riot Games et toutes les propriétés associées sont des marques commerciales ou déposées de Riot Games, Inc. lolbuilds n'est pas affilié à OP.GG.
