"""Récupération au moment du pick : adversaire de lane, cache d'une journée, matchups, bons picks."""

from __future__ import annotations

import asyncio
import json
from datetime import date

import pytest

from lolbuilds.app import server
from lolbuilds.app.server import Controller, select_counter_picks
from lolbuilds.cache import BuildCache
from lolbuilds.config import Config
from lolbuilds.ddragon import ChampionCatalog, ItemCatalog, StaticData
from lolbuilds.live import LiveBuilds, guess_lanes
from lolbuilds.sources import BuildSourceError
from lolbuilds.sources.opgg import build_from_analysis, counters_from_guide, matchup_insight, matchup_variants, parse_toon
from conftest import FIXTURES, make_build
from test_applier import FakeLcu, session

ROLES = {
    "Syndra": {"middle": 0.9},
    "Thresh": {"utility": 0.95},
    "Pantheon": {"top": 0.4, "utility": 0.3, "middle": 0.2},
    "Ahri": {"middle": 0.95},
}


# Syndra contre chaque adversaire (extrait de `data.counters` du guide de matchup).
COUNTER_GUIDE = {"data": {"counters": [
    {"champion_id": 55, "champion_name": "Katarina", "play": 4092, "win": 1824},  # Katarina gagne 55,4 %
    {"champion_id": 105, "champion_name": "Fizz", "play": 4392, "win": 1985},
    {"champion_id": 103, "champion_name": "Ahri", "play": 7994, "win": 4028},  # Ahri perd (49,6 %)
    {"champion_id": 161, "champion_name": "Vel'Koz", "play": 120, "win": 40},  # trop peu de parties
]}}


def guide():
    return json.loads((FIXTURES / "opgg_matchup_ahri_syndra.json").read_text(encoding="utf-8"))


# --------------------------------------------------------------------------- adversaire

def test_guess_lanes_maximizes_role_rates():
    lanes = guess_lanes(["Syndra", "Thresh", "Pantheon"], ROLES)
    assert lanes == {"Syndra": "middle", "Thresh": "utility", "Pantheon": "top"}


def test_guess_lanes_flex_pick_takes_free_role():
    # Syndra absente : Pantheon peut être mid, mais top reste son meilleur rôle.
    assert guess_lanes(["Thresh", "Pantheon"], ROLES)["Pantheon"] == "top"
    assert guess_lanes(["Inconnu"], ROLES) == {}


# --------------------------------------------------------------------------- matchup

def test_matchup_variants_from_real_guide():
    variants = matchup_variants(guide(), opponent="Syndra")
    assert len(variants) == 2
    assert variants[0]["winrate"] >= variants[1]["winrate"]
    assert all(len(v["perks"]) == 9 and v["tags"][0] == "Contre Syndra" for v in variants)
    keystones = {v["perks"][0] for v in variants}
    assert keystones == {8112, 8992}  # pages ne différant que par les shards regroupées
    assert "Le plus joué" in variants[0]["tags"]


def test_matchup_without_runes_gives_nothing():
    assert matchup_variants({"data": {}}, opponent="Syndra") == []


# --------------------------------------------------------------------------- cache d'une journée

class FakeSource:
    name = "opgg"

    def __init__(self, fail: bool = False) -> None:
        champs = ChampionCatalog(by_key={103: "Ahri", 134: "Syndra", 412: "Thresh", 80: "Pantheon"},
                                 names={"Ahri": "Ahri", "Syndra": "Syndra", "Thresh": "Thresh", "Pantheon": "Pantheon"})
        self.static = StaticData(version="16.21.1", champions=champs, items=ItemCatalog())
        self._roles = None
        self.fail = fail
        self.aram_perk: int | None = None  # None : le build ARAM est identique au build classé
        self.calls: list[str] = []

    def aram_targets(self, champions):
        from lolbuilds.models import Target

        return [Target(c, "aram") for c in sorted(set(champions))]

    def roles(self):
        self.calls.append("roles")
        return ROLES

    def fetch_build(self, champion, role):
        self.calls.append(f"build {champion} {role}")
        if self.fail:
            raise BuildSourceError("hors ligne")
        analysis = parse_toon((FIXTURES / "opgg_fizz_mid.txt").read_text(encoding="utf-8"))
        build = build_from_analysis(analysis, champion=champion, champion_id=103, role=role, patch="16.21")
        if role == "aram" and self.aram_perk:
            build.variants[0]["perks"][0] = self.aram_perk
            build.variants[0]["subStyle"] = 8300
            build.perks[0] = self.aram_perk
        return build

    def fetch_matchup(self, champion, role, opponent, insight=False):
        self.calls.append(f"matchup {champion} {opponent}")
        if self.fail:
            raise BuildSourceError("hors ligne")
        variants = matchup_variants(guide(), opponent=opponent)
        return (variants, matchup_insight(guide())) if insight else variants

    def fetch_counters(self, enemy, role):
        self.calls.append(f"counters {enemy} {role}")
        return counters_from_guide(COUNTER_GUIDE)

    def fetch_tier_list(self, role):
        self.calls.append(f"tiers {role}")
        return [{"championId": cid, "tier": 1, "rank": i, "winrate": 0.52, "pickRate": 0.1, "banRate": 0.2, "games": 9000}
                for i, cid in enumerate([103, 134, 55, 105, 80, 412, 161, 245])]

    def close(self):
        pass


@pytest.fixture
def live(tmp_path):
    cfg = Config(cache_dir=tmp_path)
    return LiveBuilds(cfg, BuildCache(tmp_path), source=FakeSource())


def test_global_build_fetched_once_per_day(live):
    b = live.global_build("Ahri", "middle")
    assert b and b.fetched_at == date.today().isoformat()
    live.global_build("Ahri", "middle")
    assert live.source.calls.count("build Ahri middle") == 1


def test_global_build_keeps_old_cache_when_offline(tmp_path):
    cache = BuildCache(tmp_path)
    cache.save(make_build(fetched_at="2026-01-01"))
    live = LiveBuilds(Config(cache_dir=tmp_path), cache, source=FakeSource(fail=True))
    assert live.global_build("Ahri", "middle").fetched_at == "2026-01-01"


def test_matchup_cached_for_the_day(live, tmp_path):
    assert len(live.matchup("Ahri", "middle", "Syndra")) == 2
    assert len(live.matchup("Ahri", "middle", "Syndra")) == 2
    assert live.source.calls.count("matchup Ahri Syndra") == 1
    path = tmp_path / "matchups" / "Ahri_middle_vs_Syndra.json"
    data = json.loads(path.read_text(encoding="utf-8"))
    data["date"] = "2026-01-01"  # le lendemain : nouvel appel
    path.write_text(json.dumps(data), encoding="utf-8")
    live.matchup("Ahri", "middle", "Syndra")
    assert live.source.calls.count("matchup Ahri Syndra") == 2


def test_roles_cached_for_the_day(tmp_path):
    first = LiveBuilds(Config(cache_dir=tmp_path), BuildCache(tmp_path), source=FakeSource())
    assert first.role_for("Pantheon", "") == "top"
    second = LiveBuilds(Config(cache_dir=tmp_path), BuildCache(tmp_path), source=FakeSource())
    assert second.guess_opponent(["Syndra", "Thresh"], "middle") == "Syndra"
    assert "roles" not in second.source.calls  # relu depuis opgg/lanes.json


# --------------------------------------------------------------------------- contrôleur

@pytest.fixture
def ctrl(tmp_path, monkeypatch):
    monkeypatch.setattr(server, "APPLY_DEBOUNCE", 0)
    c = Controller(Config(cache_dir=tmp_path))
    c.live = LiveBuilds(c.cfg, c.cache, source=FakeSource())
    c.champions = c.live.source.static.champions
    c.lcu = FakeLcu()
    return c


def ahri_vs(enemies):
    s = session()
    s["theirTeam"] = [{"cellId": 6 + i, "championId": cid} for i, cid in enumerate(enemies)]
    return s


def run(ctrl, sess):
    async def scenario():
        await ctrl.on_session(sess)
        await ctrl._live_refresh()
        if ctrl._apply_task:
            await ctrl._apply_task

    asyncio.run(scenario())


def test_pick_fetches_build_and_matchup_then_auto_applies(ctrl):
    run(ctrl, ahri_vs([134, 412]))
    cur = ctrl.state["current"]
    assert cur["opponent"] == {"championId": 134, "guessed": True, "status": "ok"}
    variants = cur["build"]["variants"]
    assert [v["tags"][0] for v in variants] == ["Contre Syndra", "Contre Syndra", "Recommandé OP.GG"]
    assert ctrl.state["applied"]["variant"] == 0 and ctrl.state["applied"]["auto"]
    post = next(b for m, p, b in ctrl.lcu.calls if m == "POST")
    assert post["selectedPerkIds"][0] == variants[0]["perks"][0]


def test_same_matchup_not_refetched(ctrl):
    run(ctrl, ahri_vs([134]))
    asyncio.run(ctrl._live_refresh())
    asyncio.run(ctrl._live_refresh())
    assert ctrl.live.source.calls.count("matchup Ahri Syndra") == 1
    assert ctrl.live.source.calls.count("build Ahri middle") == 1


def test_manual_opponent_overrides_guess(ctrl):
    run(ctrl, ahri_vs([134, 80]))
    ctrl._opponent_override = 80
    asyncio.run(ctrl._live_refresh())
    assert ctrl.state["current"]["opponent"]["championId"] == 80
    assert not ctrl.state["current"]["opponent"]["guessed"]
    assert "matchup Ahri Pantheon" in ctrl.live.source.calls


def test_manual_choice_stops_auto_apply(ctrl):
    ctrl.prefs.data["autoApply"] = True
    run(ctrl, ahri_vs([]))  # pas encore d'adversaire : build global appliqué
    asyncio.run(ctrl.apply(0))  # choix manuel
    calls = len(ctrl.lcu.calls)
    run(ctrl, ahri_vs([134]))  # l'adversaire arrive ensuite
    assert ctrl.state["current"]["build"]["variants"][0]["tags"][0] == "Contre Syndra"
    assert len(ctrl.lcu.calls) == calls  # rien réappliqué derrière l'utilisateur


# --------------------------------------------------------------------------- bons picks

def test_counters_from_guide_are_from_the_picker_side():
    counters = counters_from_guide(COUNTER_GUIDE)
    assert counters[0]["championId"] == 161 and counters[0]["winrate"] == round(1 - 40 / 120, 4)
    kata = next(c for c in counters if c["championId"] == 55)
    assert kata["winrate"] == round(1 - 1824 / 4092, 4) and kata["games"] == 4092


def test_select_counter_picks_filters_and_marks_mains():
    counters = counters_from_guide(COUNTER_GUIDE)
    picks = select_counter_picks(counters, unavailable=set(), owned=None, mastery={105: 90000})
    assert [p["championId"] for p in picks] == [55, 105]  # Vel'Koz : échantillon ; Ahri : < 50 %
    assert picks[1]["main"] and not picks[0]["main"]
    assert [p["championId"] for p in select_counter_picks(counters, {55}, None, {})] == [105]  # bannie / prise
    assert select_counter_picks(counters, set(), owned={103}, mastery={}) == []  # non possédés


def test_counters_cached_for_the_day(live):
    live.counters("Syndra", "middle")
    live.counters("Syndra", "middle")
    assert live.source.calls.count("counters Syndra middle") == 1


class PoolLcu(FakeLcu):
    async def request(self, method, path, body=None):
        if path == "/lol-champions/v1/owned-champions-minimal":
            return 200, [{"id": 105, "active": True, "ownership": {"owned": True}},
                         {"id": 55, "active": True, "ownership": {"owned": False}, "freeToPlay": False}]
        if path == "/lol-champion-mastery/v1/local-player/champion-mastery":
            return 200, [{"championId": 105, "championPoints": 120000}]
        return await super().request(method, path, body)


def before_pick(enemies, bans=()):
    s = ahri_vs(enemies)
    s["myTeam"][1]["championId"] = 0
    s["actions"] = [[{"type": "ban", "completed": True, "championId": b, "isAllyAction": True} for b in bans]]
    return s


def test_counter_picks_before_pick_use_owned_champions(ctrl):
    ctrl.lcu = PoolLcu()
    run(ctrl, before_pick([134, 412]))
    cp = ctrl.state["counterPicks"]
    assert cp["enemyId"] == 134 and cp["guessed"] and cp["status"] == "ok"
    assert [p["championId"] for p in cp["picks"]] == [105]  # Katarina non possédée
    assert cp["picks"][0]["main"]
    assert ctrl.state["current"] is None  # rien de choisi : pas de build affiché


def test_counter_picks_skip_banned_and_hidden_once_locked(ctrl):
    run(ctrl, before_pick([134], bans=[55]))
    assert [p["championId"] for p in ctrl.state["counterPicks"]["picks"]] == [105]
    run(ctrl, ahri_vs([134]))  # Ahri verrouillée : plus de suggestions
    assert ctrl.state["counterPicks"] is None
    assert ctrl.live.source.calls.count("counters Syndra middle") == 1


# --------------------------------------------------------------------------- mon tour, bans suggérés

def ban_phase(intent=0, my_ban_done=False, in_progress=True, enemies=(), bans=(), phase="BAN_PICK"):
    s = before_pick(list(enemies), bans=bans)
    s["myTeam"][0].update(championId=0, championPickIntent=105)  # un allié veut Fizz : jamais suggéré en ban
    s["myTeam"][1]["championPickIntent"] = intent
    s["actions"].append([{"id": 11, "actorCellId": 2, "type": "ban", "championId": 0,
                          "completed": my_ban_done, "isInProgress": in_progress and not my_ban_done}])
    s["timer"] = {"phase": phase, "adjustedTimeLeftInPhase": 30000, "totalTimeInPhase": 30000}
    return s


def test_my_turn_is_detected(ctrl):
    asyncio.run(ctrl.on_session(ban_phase()))
    assert ctrl.state["champSelect"]["turn"] == {"id": 11, "type": "ban"}
    assert ctrl.state["champSelect"]["banPending"] and ctrl.state["champSelect"]["stage"] == "ban"
    asyncio.run(ctrl.on_session(ban_phase(in_progress=False)))  # pas encore à moi
    assert ctrl.state["champSelect"]["turn"] is None and ctrl.state["champSelect"]["banPending"]


def test_prepick_is_not_a_ban_turn(ctrl):
    # Pendant le prépick, le client marque déjà le ban « en cours » : ce n'est pas encore mon tour.
    asyncio.run(ctrl.on_session(ban_phase(phase="PLANNING")))
    cs = ctrl.state["champSelect"]
    assert cs["turn"] is None and cs["stage"] == "planning"
    assert cs["banPending"]  # les bans suggérés peuvent déjà s'afficher pour préparer son ban
    asyncio.run(ctrl.on_session(ban_phase(phase="BAN_PICK")))  # la vraie phase de bans commence
    assert ctrl.state["champSelect"]["turn"] == {"id": 11, "type": "ban"}


def test_ban_suggestions_from_tier_list(ctrl):
    run(ctrl, ban_phase(bans=[134]))
    bs = ctrl.state["banSuggestions"]
    assert bs["mode"] == "meta" and bs["role"] == "middle"
    ids = [p["championId"] for p in bs["picks"]]
    assert 134 not in ids and 105 not in ids  # déjà bannie / voulue par un allié
    assert ids == [103, 55, 80, 412, 161, 245]


def test_ban_suggestions_threats_for_declared_champion(ctrl):
    run(ctrl, ban_phase(intent=134))  # je compte prendre Syndra
    bs = ctrl.state["banSuggestions"]
    assert bs["mode"] == "threats" and bs["championId"] == 134
    assert [p["championId"] for p in bs["picks"]] == [55]  # Fizz voulu par un allié, Vel'Koz : échantillon


def test_no_ban_suggestions_once_banned(ctrl):
    run(ctrl, ban_phase(my_ban_done=True))
    assert ctrl.state["banSuggestions"] is None


# --------------------------------------------------------------------------- ban depuis l'appli

def test_ban_in_one_click(ctrl):
    asyncio.run(ctrl.on_session(ban_phase()))
    ctrl.lcu.calls.clear()
    assert asyncio.run(ctrl.ban(55))["ok"]
    assert ctrl.lcu.calls == [("PATCH", "/lol-champ-select/v1/session/actions/11", {"championId": 55, "completed": True})]
    assert ctrl.state["banPick"] == {"championId": 55, "locked": True, "action": 11}


def test_ban_falls_back_to_select_then_complete(ctrl):
    class OldClient(FakeLcu):  # client qui refuse « completed » dans le PATCH
        async def request(self, method, path, body=None):
            if method == "PATCH" and body and body.get("completed"):
                self.calls.append((method, path, body))
                return 400, {"message": "completed non accepté"}
            return await super().request(method, path, body)

    ctrl.lcu = OldClient()
    asyncio.run(ctrl.on_session(ban_phase()))
    ctrl.lcu.calls.clear()
    assert asyncio.run(ctrl.ban(55))["ok"]
    assert [(m, p) for m, p, _ in ctrl.lcu.calls] == [
        ("PATCH", "/lol-champ-select/v1/session/actions/11"),
        ("PATCH", "/lol-champ-select/v1/session/actions/11"),
        ("POST", "/lol-champ-select/v1/session/actions/11/complete"),
    ]


def test_ban_refused_outside_my_ban_turn(ctrl):
    asyncio.run(ctrl.on_session(ban_phase(phase="PLANNING")))  # prépick : pas encore mon tour
    ctrl.lcu.calls.clear()
    assert not asyncio.run(ctrl.ban(55))["ok"]
    s = ban_phase()
    s["actions"][-1][0]["type"] = "pick"  # mon action en cours est un pick : jamais touchée
    asyncio.run(ctrl.on_session(s))
    assert not asyncio.run(ctrl.ban(55))["ok"]
    assert not any(m in ("PATCH", "POST") for m, _, _ in ctrl.lcu.calls)


def test_ban_refused_for_taken_or_wanted_champion(ctrl):
    asyncio.run(ctrl.on_session(ban_phase(bans=[134])))
    assert "déjà banni" in asyncio.run(ctrl.ban(134))["error"]
    assert "allié" in asyncio.run(ctrl.ban(105))["error"]  # Fizz voulu par un allié


# --------------------------------------------------------------------------- mon équipe en partie

PROFILE = {"data": {"summoner": {
    "league_stats": [{"game_type": "SOLORANKED", "tier_info": {"tier": "GOLD", "division": 1, "lp": 32},
                      "win": 70, "lose": 30, "is_hot_streak": True, "is_fresh_blood": False, "is_veteran": True}],
    "most_champions": {"play": 100, "win": 70, "champion_stats": [
        {"id": 103, "champion_name": "Ahri", "play": 40, "win": 26, "kill": 300, "death": 100, "assist": 200},
        {"id": 134, "champion_name": "Syndra", "play": 2, "win": 1, "kill": 10, "death": 5, "assist": 5},
    ]},
}}}


def test_player_card_indications():
    from lolbuilds.players import player_card

    card = player_card(PROFILE, 103, "Ahri")
    assert card["rank"] == "Or I" and card["lp"] == 32 and card["winrate"] == 0.7
    assert card["champion"] == {"games": 40, "winrate": 0.65, "kda": 5.0}
    texts = [t["text"] for t in card["tags"]]
    assert {"En série de victoires", "Main Ahri", "65 % sur Ahri", "Bonne saison", "Vétéran du rang"} <= set(texts)
    assert "Peu joué Syndra cette saison" in [t["text"] for t in player_card(PROFILE, 134, "Syndra")["tags"]]
    assert player_card({}, 1, "Annie")["rank"] == "Non classé"


def test_team_loaded_when_game_starts(ctrl, monkeypatch):
    from lolbuilds import players

    everyone = [
        {"riotIdGameName": "Moi", "riotIdTagLine": "EUW", "team": "ORDER", "position": "MIDDLE",
         "rawChampionName": "game_character_displayname_Ahri"},
        {"riotIdGameName": "Pote", "riotIdTagLine": "123", "team": "ORDER", "position": "UTILITY",
         "rawChampionName": "game_character_displayname_Thresh"},
        {"riotIdGameName": "Ennemi", "riotIdTagLine": "1", "team": "CHAOS", "position": "MIDDLE",
         "rawChampionName": "game_character_displayname_Syndra"},
    ]
    monkeypatch.setattr(players, "live_players", lambda: ("Moi#EUW", everyone))
    monkeypatch.setattr(ctrl.live, "player_profile", lambda name, tag, region: PROFILE)
    monkeypatch.setattr(ctrl.live, "player_matches", lambda name, tag, region: MATCHES)
    seen = []

    async def scenario():
        async def spy():
            seen.append({k: v for k, v in (ctrl.state["team"] or {}).items() if k in ("status", "done", "total")})
        ctrl.broadcast = spy
        ctrl.on_phase("InProgress")
        await ctrl._team_task

    asyncio.run(scenario())
    team = ctrl.state["team"]
    assert team["status"] == "ok" and [p["riotId"] for p in team["players"]] == ["Moi#EUW", "Pote#123"]
    # progression pendant le chargement, puis tous les joueurs d'un coup
    assert {"status": "loading", "done": 0, "total": 3} in seen and seen[-1] == {"status": "ok", "done": 3, "total": 3}
    assert team["players"][0]["isMe"] and team["players"][0]["championId"] == 103
    assert team["players"][1]["championId"] == 412 and team["players"][1]["rank"] == "Or I"
    ctrl.on_phase("Lobby")
    assert ctrl.state["team"] is None


# --------------------------------------------------------------------------- tout télécharger

class PrefetchSource:
    """Source OP.GG simulée pour « tout télécharger » : deux cibles, l'une en échec."""

    def __init__(self):
        from lolbuilds.models import Target

        self.static = None
        self.targets_list = [Target("Ahri", "middle"), Target("Syndra", "middle")]
        self.prepared: list = []

    def targets(self):
        return list(self.targets_list)

    def aram_targets(self, champions):
        from lolbuilds.models import Target

        return [Target(c, "aram") for c in sorted(set(champions))]

    def prepare(self, stale):
        self.prepared = list(stale)

    def fetch_build(self, champion, role):
        if champion == "Syndra":
            raise BuildSourceError("OP.GG injoignable")
        return make_build(champion=champion, role=role)


def test_prefetch_all_updates_stale_builds_and_keeps_going_on_error(tmp_path):
    cache = BuildCache(tmp_path)
    live = LiveBuilds(Config(cache_dir=tmp_path), cache, source=PrefetchSource())
    steps: list[tuple[int, int]] = []
    counts = live.prefetch_all(lambda done, total: steps.append((done, total)))
    # 2 builds classés + 2 builds ARAM (un par champion) ; Syndra échoue partout
    assert counts == {"total": 4, "fresh": 0, "updated": 2, "failed": 2}
    assert steps == [(1, 4), (2, 4), (3, 4), (4, 4)]
    assert cache.load("Ahri", "middle") is not None and cache.load("Syndra", "middle") is None
    assert cache.load("Ahri", "aram") is not None
    # relancé le jour même : ce qui est à jour n'est pas retéléchargé
    again = live.prefetch_all()
    assert again["fresh"] == 2 and again["updated"] == 0


# --------------------------------------------------------------------------- ARAM

def aram_session(champion_id=103):
    s = session(champion_id, position="")
    s["benchEnabled"] = True  # le client active le banc de remplaçants en ARAM
    return s


def test_aram_champ_select_uses_the_aram_build(ctrl):
    run(ctrl, aram_session())
    cur = ctrl.state["current"]
    assert ctrl.state["champSelect"]["mode"] == "aram"
    assert cur["mode"] == "aram" and cur["role"] == "aram" and cur["roles"] == [] and cur["opponent"] is None
    assert cur["build"]["variants"][0]["tags"] == ["Build ARAM"]
    assert cur["build"]["itemSet"]["associatedMaps"] == [12]  # sinon absent de la boutique d'ARAM
    calls = ctrl.live.source.calls
    assert "build Ahri aram" in calls and not any(c.startswith(("matchup", "roles", "build Ahri middle")) for c in calls)
    assert ctrl.state["applied"]["auto"]


def test_aram_build_is_fetched_once_per_day(ctrl):
    run(ctrl, aram_session())
    asyncio.run(ctrl._live_refresh())
    assert ctrl.live.source.calls.count("build Ahri aram") == 1


def test_aram_option_off_keeps_the_ranked_behaviour(tmp_path, monkeypatch):
    from lolbuilds.config import OpggSettings

    monkeypatch.setattr(server, "APPLY_DEBOUNCE", 0)
    c = Controller(Config(cache_dir=tmp_path, opgg=OpggSettings(aram=False)))
    c.live = LiveBuilds(c.cfg, c.cache, source=FakeSource())
    c.champions = c.live.source.static.champions
    c.lcu = FakeLcu()
    run(c, aram_session())
    assert not any("aram" in call for call in c.live.source.calls)
    assert c.state["current"]["mode"] == "classic"


def test_ranked_gets_the_aram_build_as_last_atypical_proposal(ctrl):
    ctrl.prefs.update({"atypical": True})
    ctrl.live.source.aram_perk = 8229
    run(ctrl, ahri_vs([134]))
    variants = ctrl.state["current"]["build"]["variants"]
    assert [v["tags"][0] for v in variants] == ["Contre Syndra", "Contre Syndra", "Recommandé OP.GG", "Atypique : build ARAM"]
    assert variants[-1]["perks"][0] == 8229
    assert ctrl.state["applied"]["variant"] == 0  # jamais appliqué par défaut
    assert ctrl.live.source.calls.count("build Ahri aram") == 1


def test_atypical_proposal_skipped_when_identical_to_the_ranked_build(ctrl):
    ctrl.prefs.update({"atypical": True})
    run(ctrl, ahri_vs([134]))  # build ARAM de la fausse source = build classé
    tags = [v["tags"][0] for v in ctrl.state["current"]["build"]["variants"]]
    assert "Atypique : build ARAM" not in tags


def test_atypical_option_off_makes_no_aram_request(tmp_path, monkeypatch):
    from lolbuilds.config import OpggSettings

    monkeypatch.setattr(server, "APPLY_DEBOUNCE", 0)
    c = Controller(Config(cache_dir=tmp_path, opgg=OpggSettings(aram=False)))
    c.live = LiveBuilds(c.cfg, c.cache, source=FakeSource())
    c.champions = c.live.source.static.champions
    c.lcu = FakeLcu()
    c.live.source.aram_perk = 8229
    run(c, ahri_vs([134]))
    assert not any("aram" in call for call in c.live.source.calls)


def test_aram_build_is_not_a_lane_build(tmp_path):
    cache = BuildCache(tmp_path)
    cache.save(make_build())
    cache.save(make_build(role="aram", name="Ahri ARAM"))
    assert [b.role for b in cache.builds_for_champion("Ahri")] == ["middle"]
    assert [p.name for p in cache.build_paths()] == ["Ahri_middle.json"]


def test_library_peek_never_touches_the_champ_select(ctrl):
    run(ctrl, ahri_vs([134]))
    before = ctrl.state["current"]
    ctrl.cache.save(make_build(champion="Syndra", role="top", name="Syndra Top"))
    peek = ctrl.peek_build(134, "top")
    assert peek["champion"] == "Syndra" and peek["build"]["role"] == "top" and peek["roles"] == ["top"]
    assert ctrl.state["current"] is before and before["champion"] == "Ahri"  # la sélection en cours est intacte
    assert ctrl.build.champion == "Ahri"
    missing = ctrl.peek_build(134, "jungle")
    assert missing["build"] is None


def test_library_peek_lists_the_aram_build_as_a_tab(ctrl):
    ctrl.cache.save(make_build())
    ctrl.cache.save(make_build(role="aram", name="Ahri ARAM"))
    peek = ctrl.peek_build(103, "aram")
    assert peek["mode"] == "aram" and peek["roles"] == ["middle", "aram"]


# --------------------------------------------------------------------------- partie en cours : adversaires

MATCHES = {"data": {"game_history": [
    {"participants": [{"champion_id": 134, "stats": {"kill": 2, "death": 6, "assist": 1, "result": "LOSE"}}]},
    {"participants": [{"champion_id": 134, "stats": {"kill": 1, "death": 4, "assist": 3, "result": "LOSE"}}]},
    {"participants": [{"champion_id": 61, "stats": {"kill": 0, "death": 0, "assist": 0, "result": "UNKNOWN"}}]},  # remake
    {"participants": [{"champion_id": 61, "stats": {"kill": 9, "death": 2, "assist": 8, "result": "WIN"}}]},
]}}


def test_recent_form():
    from lolbuilds.players import recent_form

    form = recent_form(MATCHES, 134)
    assert form["games"] == 3 and form["wins"] == 1 and form["results"] == [False, False, True]
    assert form["onChampion"] == 2 and form["streak"] == {"win": False, "count": 2}
    assert form["kda"] == round((2 + 1 + 1 + 3 + 9 + 8) / 12, 1)
    assert recent_form({}, 134) is None


def test_matchup_insight():
    from lolbuilds.sources.opgg import matchup_insight

    guide = {"data": {"lane_advantage_champion": "even", "lane_solo_kill_advantage_champion": "ADVANTAGE",
                      "opponent_champion_tip": " Dodge her E. "}}
    assert matchup_insight(guide) == {"lane": "EVEN", "soloKill": "ADVANTAGE", "tip": "Dodge her E."}
    assert matchup_insight(None) == {"lane": "", "soloKill": "", "tip": ""}


def test_enemies_and_lane_opponent_loaded(ctrl, monkeypatch):
    from lolbuilds import players

    everyone = [
        {"riotIdGameName": "Moi", "riotIdTagLine": "EUW", "team": "ORDER", "position": "MIDDLE",
         "rawChampionName": "game_character_displayname_Ahri"},
        {"riotIdGameName": "Haut", "riotIdTagLine": "1", "team": "CHAOS", "position": "TOP",
         "rawChampionName": "game_character_displayname_Thresh"},
        {"riotIdGameName": "Ennemi", "riotIdTagLine": "1", "team": "CHAOS", "position": "MIDDLE",
         "rawChampionName": "game_character_displayname_Syndra"},
    ]
    monkeypatch.setattr(players, "live_players", lambda: ("Moi#EUW", everyone))
    monkeypatch.setattr(ctrl.live, "player_profile", lambda name, tag, region: PROFILE)
    monkeypatch.setattr(ctrl.live, "player_matches", lambda name, tag, region: MATCHES)
    ctrl.state["current"] = {"championId": 103, "champion": "Ahri", "role": "middle", "locked": True, "opponent": None}

    async def scenario():
        ctrl.on_phase("InProgress")
        await ctrl._team_task

    asyncio.run(scenario())
    team = ctrl.state["team"]
    assert [p["riotId"] for p in team["enemies"]] == ["Haut#1", "Ennemi#1"]
    opp = team["enemies"][1]
    assert opp["isOpponent"] and opp["status"] == "ok" and opp["form"]["onChampion"] == 2
    assert set(opp["matchup"]) == {"lane", "soloKill", "tip"}
    assert "isOpponent" not in team["enemies"][0] and "form" not in team["enemies"][0]


def test_locked_build_kept_during_game(ctrl):
    asyncio.run(ctrl.on_session(session(103)))
    ctrl.state["current"]["locked"] = True
    ctrl.state["phase"] = "InProgress"
    asyncio.run(ctrl.on_session(None))  # fin du champ select
    assert ctrl.state["champSelect"] is None and ctrl.state["current"]["championId"] == 103
    ctrl.state["phase"] = "Lobby"
    ctrl.on_phase("Lobby")
    assert ctrl.state["current"] is None


def test_hovered_build_dropped_when_champ_select_ends(ctrl):
    asyncio.run(ctrl.on_session(session(103)))
    ctrl.state["current"]["locked"] = False  # esquive : rien n'était verrouillé
    asyncio.run(ctrl.on_session(None))
    assert ctrl.state["current"] is None


def test_slow_profile_does_not_block_the_others(ctrl, monkeypatch):
    import time as _time
    from lolbuilds import players

    everyone = [
        {"riotIdGameName": "Moi", "riotIdTagLine": "EUW", "team": "ORDER", "position": "MIDDLE",
         "rawChampionName": "game_character_displayname_Ahri"},
        {"riotIdGameName": "Lent", "riotIdTagLine": "1", "team": "CHAOS", "position": "TOP",
         "rawChampionName": "game_character_displayname_Thresh"},
    ]

    def profile(name, tag, region):
        if name == "Lent":
            _time.sleep(0.3)
        return PROFILE

    monkeypatch.setattr(server, "TEAM_WAIT", 0.05)
    monkeypatch.setattr(players, "live_players", lambda: ("Moi#EUW", everyone))
    monkeypatch.setattr(ctrl.live, "player_profile", profile)
    statuses = []

    async def scenario():
        async def spy():
            statuses.append(ctrl.state["team"]["status"])
        ctrl.broadcast = spy
        ctrl.on_phase("InProgress")
        await ctrl._team_task

    asyncio.run(scenario())
    assert "partial" in statuses and statuses[-1] == "ok"  # affiché sans attendre le retardataire


# --------------------------------------------------------------------------- Disco pick

def pick_phase(enemies=(134,)):
    s = before_pick(list(enemies))
    s["actions"].append([{"id": 21, "actorCellId": 2, "type": "pick", "championId": 0, "completed": False, "isInProgress": True}])
    s["timer"] = {"phase": "BAN_PICK", "adjustedTimeLeftInPhase": 30000, "totalTimeInPhase": 30000}
    return s


def test_disco_pick_is_off_by_default(ctrl):
    asyncio.run(ctrl.on_session(pick_phase()))
    assert not asyncio.run(ctrl.disco_roll())["ok"]


def test_disco_roll_draws_a_free_champion_without_hovering(ctrl):
    ctrl.prefs.update({"disco": {"enabled": True}})
    asyncio.run(ctrl.on_session(pick_phase()))
    ctrl.lcu.calls.clear()
    r = asyncio.run(ctrl.disco_roll())
    assert r["ok"] and r["championId"] in (103, 80)  # ni Thresh (allié) ni Syndra (ennemie)
    assert r["championId"] in r["reel"] and not {412, 134} & set(r["reel"])
    assert not any(m == "PATCH" for m, *_ in ctrl.lcu.calls)  # rien dans le client avant la fin de la roulette
    assert ctrl.state["current"]["championId"] == r["championId"]  # son build se prépare pendant la roulette
    assert 25000 < r["timeLeft"] <= 30000  # la roulette s'adapte au temps restant


def test_disco_roll_refused_outside_my_pick(ctrl):
    ctrl.prefs.update({"disco": {"enabled": True}})
    asyncio.run(ctrl.on_session(ban_phase()))
    assert asyncio.run(ctrl.disco_roll())["error"] == "Ce n'est pas ton tour de choisir"


def test_disco_lock_locks_and_applies_chaos_spells(ctrl):
    ctrl.prefs.update({"disco": {"enabled": True}})

    async def scenario():
        await ctrl.on_session(pick_phase())
        r = await ctrl.disco_roll()
        await ctrl._live_refresh()
        ctrl.lcu.calls.clear()
        return r, await ctrl.disco_lock(r["championId"], 0, [6, 21])

    r, locked = asyncio.run(scenario())
    assert locked["ok"]
    assert ctrl.lcu.calls[0] == ("PATCH", "/lol-champ-select/v1/session/actions/21", {"championId": r["championId"], "completed": True})
    spells = next(b for m, p, b in ctrl.lcu.calls if p == "/lol-champ-select/v1/session/my-selection")
    assert sorted(spells.values()) == [6, 21]  # « Chaos total » : Fantôme + Barrière, sans Flash
    assert ctrl.state["applied"]["variant"] == 0


def test_disco_lock_picks_a_random_proposal(ctrl):
    ctrl.prefs.update({"disco": {"enabled": True}})

    async def scenario():
        await ctrl.on_session(pick_phase())
        r = await ctrl.disco_roll()
        await ctrl._live_refresh()
        return await ctrl.disco_lock(r["championId"], -1, None)

    locked = asyncio.run(scenario())
    variants = ctrl.build.variants
    assert locked["ok"] and 0 <= locked["variant"] < len(variants)
    assert not any(t.startswith("Atypique") for t in variants[locked["variant"]].get("tags", []))
    assert ctrl.state["applied"]["variant"] == locked["variant"]


def test_disco_settings_are_validated(ctrl):
    ctrl.prefs.update({"disco": {"enabled": 1, "lights": "laser", "spells": "chaos"}})
    d = ctrl.prefs.data["disco"]
    assert d["enabled"] is True and d["lights"] == "disco" and d["spells"] == "chaos"


def test_disco_music_is_the_players_file(ctrl):
    assert ctrl._settings()["disco"]["music"] == "yume-no-mukou-e.mp3"  # musique livrée avec l'appli
    assert server.DISCO_MUSIC.is_file()
    (ctrl.cfg.cache_dir / "disco-music.mp3").write_bytes(b"ID3")
    (ctrl.cfg.cache_dir / "disco-music.txt").write_text("pas de la musique")
    assert ctrl._settings()["disco"]["music"] == "disco-music.mp3"


# --------------------------------------------------------------------------- ARAM : partie et Disco pick

def test_live_players_waits_while_the_game_loads():
    """Écran de chargement : l'API répond 404 avec un JSON d'erreur, jamais pris pour la liste des joueurs."""
    import httpx

    from lolbuilds import players

    def loading(_req):
        return httpx.Response(404, json={"errorCode": "RESOURCE_NOT_FOUND", "httpStatus": 404})

    with httpx.Client(transport=httpx.MockTransport(loading)) as http, pytest.raises(httpx.HTTPError):
        players.live_players(http)

    def loaded(req):
        if req.url.path.endswith("activeplayername"):
            return httpx.Response(200, json="Moi#EUW")
        return httpx.Response(200, json=[{"riotId": "Moi#EUW", "team": "ORDER"}, "intrus"])

    with httpx.Client(transport=httpx.MockTransport(loaded)) as http:
        assert players.live_players(http) == ("Moi#EUW", [{"riotId": "Moi#EUW", "team": "ORDER"}])


def test_aram_team_is_shown_without_positions(ctrl, monkeypatch):
    from lolbuilds import players

    everyone = [
        {"riotId": "Moi#EUW", "team": "ORDER", "position": "", "rawChampionName": "game_character_displayname_Ahri"},
        {"riotId": "Pote#123", "team": "ORDER", "position": "", "rawChampionName": "game_character_displayname_Thresh"},
        {"riotId": "Ennemi#1", "team": "CHAOS", "position": "", "rawChampionName": "game_character_displayname_Syndra"},
        {"riotId": "", "summonerName": "", "isBot": True, "team": "CHAOS", "position": "",
         "rawChampionName": "game_character_displayname_Pantheon"},
    ]
    monkeypatch.setattr(players, "live_players", lambda: ("Moi#EUW", everyone))
    monkeypatch.setattr(ctrl.live, "player_profile", lambda name, tag, region: PROFILE)

    async def scenario():
        ctrl.state["phase"] = "ChampSelect"
        await ctrl.on_session(aram_session())
        await ctrl.on_session(None)  # fin du champ select : le build reste affiché
        ctrl.state["phase"] = "GameStart"
        ctrl.on_phase("GameStart")  # chargement de la partie : les joueurs arrivent déjà
        await ctrl._team_task

    asyncio.run(scenario())
    team = ctrl.state["team"]
    assert team["status"] == "ok"
    assert [p["riotId"] for p in team["players"]] == ["Moi#EUW", "Pote#123"]
    assert [p["status"] for p in team["enemies"]] == ["ok", "bot"]
    assert not any(t["text"].startswith("Peu joué") for t in team["players"][1]["tags"])  # champion tiré au sort
    assert ctrl.state["current"]["championId"] == 103


def test_team_never_stays_loading_after_an_error(ctrl, monkeypatch):
    from lolbuilds import players

    monkeypatch.setattr(players, "live_players", lambda: ("Moi#EUW", [{"riotId": "Moi#EUW", "team": "ORDER"}]))
    monkeypatch.setattr(players, "riot_id", lambda p: 1 / 0)

    async def scenario():
        ctrl.on_phase("InProgress")
        await ctrl._team_task

    asyncio.run(scenario())
    assert ctrl.state["team"]["status"] == "unavailable"


def test_no_disco_pick_in_aram(ctrl):
    ctrl.prefs.update({"disco": {"enabled": True}})
    s = aram_session()
    s["actions"] = [[{"id": 21, "actorCellId": 2, "type": "pick", "championId": 103, "completed": False, "isInProgress": True}]]
    s["timer"] = {"phase": "BAN_PICK", "adjustedTimeLeftInPhase": 30000}
    asyncio.run(ctrl.on_session(s))
    assert not asyncio.run(ctrl.disco_roll())["ok"]
    assert not asyncio.run(ctrl.disco_lock(103, 0, None))["ok"]
    assert not any(m == "PATCH" for m, *_ in ctrl.lcu.calls)


def test_aram_detected_from_the_map(ctrl):
    """Abîme hurlant (carte 12) = ARAM ; le banc seul (ARURF, sur la Faille) ne suffit plus."""
    async def gameflow(map_id):
        async def request(method, path, body=None):
            if path == "/lol-gameflow/v1/session":
                return 200, {"map": {"id": map_id}}
            return await FakeLcu().request(method, path, body)
        ctrl.lcu.request = request
        await ctrl.on_session(aram_session())  # session reçue avant la carte
        await ctrl.on_event({"uri": "/lol-gameflow/v1/gameflow-phase", "data": "ChampSelect"})

    asyncio.run(gameflow(11))
    assert ctrl.state["champSelect"]["mode"] == "classic" and ctrl.state["current"]["mode"] == "classic"
    asyncio.run(gameflow(12))
    assert ctrl.state["champSelect"]["mode"] == "aram" and ctrl.state["current"]["mode"] == "aram"


# --------------------------------------------------------------------------- application : concurrence

def test_manual_apply_cancels_the_pending_auto_apply(ctrl, monkeypatch):
    monkeypatch.setattr(server, "APPLY_DEBOUNCE", 0.05)

    async def scenario():
        await ctrl.on_session(ahri_vs([134]))
        await ctrl._live_refresh()  # programme l'application automatique
        pending = ctrl._apply_task
        await ctrl.apply(1)  # choix manuel avant la fin de l'attente
        await asyncio.sleep(0.1)
        return pending

    pending = asyncio.run(scenario())
    assert pending.cancelled()
    assert ctrl.state["applied"]["variant"] == 1 and not ctrl.state["applied"]["auto"]


def test_busy_is_reset_when_the_apply_fails(ctrl, monkeypatch):
    from lolbuilds.app.applier import BuildApplier

    async def boom(self, lcu, build):
        raise asyncio.CancelledError

    monkeypatch.setattr(BuildApplier, "apply", boom)

    async def scenario():
        await ctrl.on_session(ahri_vs([134]))
        await ctrl._live_refresh()
        if ctrl._apply_task:
            await asyncio.gather(ctrl._apply_task, return_exceptions=True)

    asyncio.run(scenario())
    assert ctrl.state["busy"] is False and not ctrl._applying


def test_applies_never_overlap(ctrl, monkeypatch):
    from lolbuilds.app.applier import BuildApplier

    running, overlaps = [0], []

    async def slow(self, lcu, build):
        running[0] += 1
        overlaps.append(running[0])
        await asyncio.sleep(0.02)
        running[0] -= 1
        self.problems = {}
        return {"runes": True, "spells": True, "itemSet": True}

    monkeypatch.setattr(BuildApplier, "apply", slow)

    async def scenario():
        await ctrl.on_session(ahri_vs([134]))
        await ctrl._live_refresh()
        await asyncio.gather(ctrl.apply(0), ctrl.apply(1), ctrl.apply(0))

    asyncio.run(scenario())
    assert max(overlaps) == 1


def test_live_refresh_survives_an_opgg_failure(ctrl, monkeypatch):
    def down(*_a, **_k):
        raise RuntimeError("OP.GG en panne")

    monkeypatch.setattr(ctrl.live, "matchup", down)
    run(ctrl, ahri_vs([134]))
    cur = ctrl.state["current"]
    assert cur["opponent"]["status"] == "empty" and not cur["fetching"]
    assert ctrl.state["applied"] is not None  # le build global est quand même appliqué


def test_bad_requests_get_a_400(ctrl):
    from aiohttp.test_utils import TestClient, TestServer

    async def scenario():
        async with TestClient(TestServer(server.make_app(ctrl))) as client:
            r = await client.post("/api/ban", data="pas du json")
            return r.status

    assert asyncio.run(scenario()) == 400


# --------------------------------------------------------------------------- Disco pick : verrouillage

def test_disco_safety_lock_before_the_timer_ends(ctrl, monkeypatch):
    """Page fermée pendant la roulette : le champion tiré est verrouillé juste avant la fin du minuteur."""
    monkeypatch.setattr(server, "DISCO_GUARD", 29.9)  # minuteur de 30 s : le secours part tout de suite
    ctrl.prefs.update({"disco": {"enabled": True}})

    async def scenario():
        await ctrl.on_session(pick_phase())
        r = await ctrl.disco_roll()
        await ctrl._disco_guard
        again = await ctrl.disco_lock(r["championId"], 0, None)  # la page arrive après : déjà fait
        return r, again

    r, again = asyncio.run(scenario())
    locks = [c for c in ctrl.lcu.calls if c[0] == "PATCH" and c[1].endswith("/actions/21")]
    assert locks == [("PATCH", "/lol-champ-select/v1/session/actions/21", {"championId": r["championId"], "completed": True})]
    assert again["ok"]


# --------------------------------------------------------------------------- partie normale : cas limites

def my_pick(champion_id, completed):
    s = ahri_vs([134])
    s["myTeam"][1]["championId"] = champion_id
    s["actions"] = [[{"id": 21, "actorCellId": 2, "type": "pick", "championId": champion_id, "completed": completed,
                      "isInProgress": not completed}]]
    s["timer"] = {"phase": "BAN_PICK", "adjustedTimeLeftInPhase": 30000}
    return s


def test_hover_during_my_turn_is_not_a_lock(ctrl):
    run(ctrl, my_pick(103, completed=False))
    assert ctrl.state["current"]["locked"] is False and ctrl.state["applied"] is None
    run(ctrl, my_pick(103, completed=True))
    assert ctrl.state["current"]["locked"] is True and ctrl.state["applied"] is not None


def test_champion_trade_applies_the_new_champion(ctrl):
    run(ctrl, ahri_vs([134]))
    asyncio.run(ctrl.apply(1))  # choix manuel sur Ahri
    traded = ahri_vs([134])
    traded["myTeam"][1]["championId"] = 80  # échange avec un allié : Pantheon
    ctrl.lcu.calls.clear()
    run(ctrl, traded)
    assert ctrl.state["current"]["championId"] == 80
    assert any(p == "/lol-perks/v1/pages" and m == "POST" for m, p, _ in ctrl.lcu.calls)


def test_browsing_another_role_never_replaces_the_applied_build(ctrl):
    from aiohttp.test_utils import TestClient, TestServer

    run(ctrl, ahri_vs([134]))
    ctrl.lcu.calls.clear()

    async def scenario():
        async with TestClient(TestServer(server.make_app(ctrl))) as client:
            await client.post("/api/open", json={"championId": 103, "role": "top"})
        await ctrl.on_session(ahri_vs([134]))
        await ctrl._live_refresh()
        if ctrl._apply_task:
            await asyncio.gather(ctrl._apply_task, return_exceptions=True)

    asyncio.run(scenario())
    assert not any(m in ("POST", "PUT") for m, *_ in ctrl.lcu.calls)


def test_failed_matchup_is_retried_later(ctrl, monkeypatch):
    calls = []
    monkeypatch.setattr(ctrl.live, "matchup", lambda *a: calls.append(a) or [])
    run(ctrl, ahri_vs([134]))
    asyncio.run(ctrl._live_refresh())
    assert len(calls) == 1 and ctrl.state["current"]["opponent"]["status"] == "empty"
    monkeypatch.setattr(server, "MATCHUP_RETRY", 0)
    asyncio.run(ctrl._live_refresh())
    assert len(calls) == 2


def test_arena_is_shown_but_never_applied(ctrl):
    ctrl._map_id = 30
    s = session(103, position="")
    run(ctrl, s)
    assert ctrl.state["champSelect"]["mode"] == "other"
    assert ctrl.state["applied"] is None and not any(m == "POST" for m, *_ in ctrl.lcu.calls)


def test_atypical_proposal_is_off_by_default(ctrl):
    ctrl.live.source.aram_perk = 8229
    assert ctrl.prefs.data["atypical"] is False
    run(ctrl, ahri_vs([134]))
    tags = [v["tags"][0] for v in ctrl.state["current"]["build"]["variants"]]
    assert not any(t.startswith("Atypique") for t in tags)
    assert "build Ahri aram" not in ctrl.live.source.calls  # aucune requête ARAM en plus
