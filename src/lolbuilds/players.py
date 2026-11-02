"""Infos sur les joueurs de mon équipe pendant la partie.

- Qui est dans la partie : API locale « Live Client Data » du jeu (https://127.0.0.1:2999), disponible
  une fois la partie chargée ; elle donne le Riot ID, le champion et l'équipe de chaque joueur.
- Leur profil : `lol_get_summoner_profile` du serveur MCP d'OP.GG (rang, winrate de la saison,
  champions les plus joués), résumé en quelques indications simples.
"""

from __future__ import annotations

from typing import Any

import httpx

LIVE = "https://127.0.0.1:2999/liveclientdata"
PROFILE_FIELDS = [
    "data.summoner.league_stats[].{game_type,is_hot_streak,is_fresh_blood,is_veteran,is_inactive,win,lose}",
    "data.summoner.league_stats[].tier_info.{tier,division,lp}",
    "data.summoner.most_champions.champion_stats[].{id,champion_name,play,win,kill,death,assist}",
    "data.summoner.most_champions.{play,win}",
]
MATCH_FIELDS = [
    "data.game_history[].{game_type,created_at}",
    "data.game_history[].participants[].{champion_id,position}",
    "data.game_history[].participants[].stats.{kill,death,assist,result}",
]
TIERS_FR = {"IRON": "Fer", "BRONZE": "Bronze", "SILVER": "Argent", "GOLD": "Or", "PLATINUM": "Platine",
            "EMERALD": "Émeraude", "DIAMOND": "Diamant", "MASTER": "Maître", "GRANDMASTER": "Grand Maître",
            "CHALLENGER": "Challenger"}
ROMAN = {1: "I", 2: "II", 3: "III", 4: "IV"}


def live_players(http: httpx.Client | None = None) -> tuple[str, list[dict[str, Any]]]:
    """(mon Riot ID, joueurs de la partie). Lève httpx.HTTPError tant que la partie n'est pas chargée."""
    client = http or httpx.Client(timeout=3, verify=False)  # certificat auto-signé du jeu
    try:
        # Pendant l'écran de chargement, l'API répond 404 avec un corps JSON d'erreur : ce n'est pas une liste.
        me = client.get(f"{LIVE}/activeplayername").raise_for_status().json()
        players = client.get(f"{LIVE}/playerlist").raise_for_status().json()
    finally:
        if http is None:
            client.close()
    if not isinstance(me, str) or not isinstance(players, list):
        raise httpx.HTTPError("partie pas encore chargée")
    return me, [p for p in players if isinstance(p, dict)]


def riot_id(player: dict[str, Any]) -> tuple[str, str]:
    name = player.get("riotIdGameName") or str(player.get("riotId") or player.get("summonerName") or "").split("#")[0]
    tag = player.get("riotIdTagLine") or (str(player.get("riotId") or "").split("#") + [""])[1]
    return name, tag


def champion_id(player: dict[str, Any]) -> str:
    """Id Data Dragon depuis « game_character_displayname_MonkeyKing »."""
    raw = str(player.get("rawChampionName") or "")
    return raw.rsplit("_", 1)[-1] if raw else str(player.get("championName") or "")


def _pct(win: int, games: int) -> float:
    return round(win / games, 3) if games else 0.0


def player_card(profile: Any, champion_key: int | None, champion_name: str, random_champion: bool = False) -> dict[str, Any]:
    """Résumé d'un profil OP.GG : rang, saison, champion joué, indications (good / bad / info).

    `random_champion` (ARAM) : le champion est tiré au sort, « peu joué » n'apprend rien sur le joueur."""
    summoner = ((profile or {}).get("data") or {}).get("summoner") or {}
    solo = next((s for s in summoner.get("league_stats") or [] if s.get("game_type") == "SOLORANKED"), {}) or {}
    tier = (solo.get("tier_info") or {}).get("tier")
    wins, losses = solo.get("win") or 0, solo.get("lose") or 0
    games = wins + losses
    most = summoner.get("most_champions") or {}
    champs = most.get("champion_stats") or []
    total = most.get("play") or sum(c.get("play") or 0 for c in champs)
    played = next((c for c in champs if champion_key and c.get("id") == champion_key), None)

    tags: list[dict[str, str]] = []
    if solo.get("is_hot_streak"):
        tags.append({"text": "En série de victoires", "kind": "good"})
    if played and (played.get("play") or 0) >= 20 and total and played["play"] / total >= 0.25:
        tags.append({"text": f"Main {champion_name}", "kind": "good"})
    if not played or (played.get("play") or 0) < 3:
        if not random_champion:
            tags.append({"text": f"Peu joué {champion_name} cette saison", "kind": "bad"})
    elif (played.get("play") or 0) >= 10:
        wr = _pct(played.get("win") or 0, played["play"])
        if wr >= 0.6:
            tags.append({"text": f"{round(wr * 100)} % sur {champion_name}", "kind": "good"})
        elif wr <= 0.42:
            tags.append({"text": f"{round(wr * 100)} % sur {champion_name}", "kind": "bad"})
    if games >= 30:
        season = _pct(wins, games)
        if season >= 0.56:
            tags.append({"text": "Bonne saison", "kind": "good"})
        elif season <= 0.45:
            tags.append({"text": "Saison difficile", "kind": "bad"})
    if solo.get("is_fresh_blood"):
        tags.append({"text": "Vient de monter", "kind": "info"})
    if solo.get("is_veteran"):
        tags.append({"text": "Vétéran du rang", "kind": "info"})

    champ = None
    if played and played.get("play"):
        deaths = played.get("death") or 0
        kda = ((played.get("kill") or 0) + (played.get("assist") or 0)) / max(1, deaths)
        champ = {"games": played["play"], "winrate": _pct(played.get("win") or 0, played["play"]), "kda": round(kda, 1)}
    return {
        "rank": (f"{TIERS_FR.get(tier, tier.title())} {ROMAN.get((solo.get('tier_info') or {}).get('division'), '')}".strip()
                 if tier else "Non classé"),
        "lp": (solo.get("tier_info") or {}).get("lp") if tier else None,
        "tier": tier,
        "games": games,
        "winrate": _pct(wins, games),
        "champion": champ,
        "mains": [c.get("id") for c in sorted(champs, key=lambda c: c.get("play") or 0, reverse=True)[:3] if c.get("id")],
        "tags": tags,
    }


def recent_form(matches: Any, champion_key: int | None) -> dict[str, Any] | None:
    """Ses dernières parties (la plus récente d'abord) : bilan, KDA, parties sur ce champion, série en cours."""
    games = []
    for g in ((matches or {}).get("data") or {}).get("game_history") or []:
        p = (g.get("participants") or [{}])[0] or {}
        st = p.get("stats") or {}
        if st.get("result") not in ("WIN", "LOSE"):
            continue  # remakes
        games.append((st["result"] == "WIN", p.get("champion_id"), st.get("kill") or 0, st.get("death") or 0, st.get("assist") or 0))
    if not games:
        return None
    wins = sum(g[0] for g in games)
    kda = (sum(g[2] + g[4] for g in games)) / max(1, sum(g[3] for g in games))
    streak = next((i for i, g in enumerate(games) if g[0] != games[0][0]), len(games))
    return {
        "games": len(games), "wins": wins, "results": [g[0] for g in games], "kda": round(kda, 1),
        "onChampion": sum(1 for g in games if champion_key and g[1] == champion_key),
        "streak": {"win": games[0][0], "count": streak},
    }
