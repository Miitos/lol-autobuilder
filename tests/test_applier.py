"""Application d'un build dans le client : logique pure et appels LCU simulés (aucun client requis)."""

from __future__ import annotations

import asyncio

from lolbuilds.app.applier import BuildApplier, find_build, merge_item_sets, orient_spells
from lolbuilds.cache import BuildCache
from lolbuilds.config import ChampSelectSettings
from conftest import make_build


class FakeLcu:
    def __init__(self, pages=None, sets=None):
        self.calls: list[tuple[str, str, object]] = []
        self.pages = pages or []
        self.sets = sets if sets is not None else {"accountId": 1, "itemSets": [], "timestamp": 0}

    async def request(self, method, path, body=None):
        self.calls.append((method, path, body))
        if method == "GET":
            if path == "/lol-perks/v1/pages":
                return 200, self.pages
            if path == "/lol-perks/v1/inventory":
                return 200, {"ownedPageCount": 2}
            if path == "/lol-summoner/v1/current-summoner":
                return 200, {"summonerId": 42}
            if path.startswith("/lol-item-sets/"):
                return 200, self.sets
        return 204, None


def session(champion_id=103, position="middle"):
    return {"localPlayerCellId": 2, "myTeam": [
        {"cellId": 1, "championId": 412, "assignedPosition": "utility"},
        {"cellId": 2, "championId": champion_id, "assignedPosition": position},
    ]}


def test_orient_spells():
    assert orient_spells([4, 14], "F") == (14, 4)
    assert orient_spells([14, 4], "D") == (4, 14)
    assert orient_spells([14, 4], "") == (14, 4)
    assert orient_spells([11, 14], "D") == (11, 14)  # pas de Flash : inchangé


def test_merge_item_sets_keeps_user_sets():
    existing = {"accountId": 1, "timestamp": 5, "itemSets": [
        {"title": "Mon set perso", "uid": "u1"},
        {"title": "[Auto] Thresh Support 15.20", "uid": "old"},
    ]}
    merged = merge_item_sets(existing, {"title": "[Auto] Ahri Mid 15.21", "uid": "new"})
    assert [s["uid"] for s in merged["itemSets"]] == ["new", "u1"]  # le nôtre en premier : présélectionné en jeu
    assert merged["accountId"] == 1 and existing["itemSets"][1]["uid"] == "old"  # entrée non mutée


def test_find_build_role_fallback(tmp_path):
    cache = BuildCache(tmp_path)
    cache.save(make_build(role="middle", sample_size=300))
    assert find_build(cache, "Ahri", "middle").role == "middle"
    assert find_build(cache, "Ahri", "top") is None  # rôle assigné sans cache : rien
    assert find_build(cache, "Ahri", "").role == "middle"  # pas de rôle (blind) : cache existant


def test_apply_full_flow():
    lcu = FakeLcu(
        pages=[{"id": 1, "name": "Ma page", "isEditable": True, "isDeletable": True},
               {"id": 7, "name": "[Auto] Ahri Mid", "isEditable": True, "isDeletable": True}],
        sets={"accountId": 1, "itemSets": [{"title": "Perso", "uid": "u1"}], "timestamp": 0},
    )
    steps = asyncio.run(BuildApplier(ChampSelectSettings(flash_key="D")).apply(lcu, make_build()))
    assert steps == {"runes": True, "spells": True, "items": True}

    methods = [(m, p) for m, p, _ in lcu.calls]
    assert ("DELETE", "/lol-perks/v1/pages/7") in methods
    assert ("DELETE", "/lol-perks/v1/pages/1") not in methods
    post = next(b for m, p, b in lcu.calls if m == "POST")
    assert post["current"] is True and len(post["selectedPerkIds"]) == 9
    patch = next(b for m, p, b in lcu.calls if m == "PATCH")
    assert patch == {"spell1Id": 4, "spell2Id": 14}
    put = next(b for m, p, b in lcu.calls if m == "PUT")
    assert [s["title"] for s in put["itemSets"]] == ["[Auto] Ahri Mid", "Perso"]


def test_disabled_steps_are_skipped():
    lcu = FakeLcu()
    settings = ChampSelectSettings(apply_spells=False, apply_item_set=False)
    assert asyncio.run(BuildApplier(settings).apply(lcu, make_build())) == {"runes": True}
    assert not any(m in ("PATCH", "PUT") for m, _, _ in lcu.calls)


def test_no_free_rune_page_does_not_delete_user_pages():
    lcu = FakeLcu(pages=[{"id": i, "name": f"Page {i}", "isEditable": True, "isDeletable": True} for i in (1, 2)])
    steps = asyncio.run(BuildApplier(ChampSelectSettings(apply_spells=False, apply_item_set=False)).apply(lcu, make_build()))
    assert steps == {"runes": False}
    assert not any(m in ("DELETE", "POST") for m, _, _ in lcu.calls)


def test_item_set_aborts_if_read_fails():
    class BrokenSets(FakeLcu):
        async def request(self, method, path, body=None):
            if method == "GET" and path.startswith("/lol-item-sets/"):
                self.calls.append((method, path, body))
                return 500, None
            return await super().request(method, path, body)

    lcu = BrokenSets()
    steps = asyncio.run(BuildApplier(ChampSelectSettings(apply_runes=False, apply_spells=False)).apply(lcu, make_build()))
    assert steps == {"items": False}
    assert not any(m == "PUT" for m, _, _ in lcu.calls)


def test_item_set_too_large_is_explained(caplog):
    """Le client répond 413 quand les item sets du compte sont trop volumineux : la raison est donnée à l'utilisateur."""
    class TooLarge(FakeLcu):
        async def request(self, method, path, body=None):
            if method == "PUT":
                self.calls.append((method, path, body))
                return 413, {"errorCode": "BAD_REQUEST_HEADERS", "httpStatus": 413, "message": "Content length is too large"}
            return await super().request(method, path, body)

    lcu = TooLarge(sets={"accountId": 1, "timestamp": 0, "itemSets": [{"title": f"Set {i}", "uid": str(i), "blocks": []} for i in range(50)]})
    applier = BuildApplier(ChampSelectSettings(apply_runes=False, apply_spells=False))
    steps = asyncio.run(applier.apply(lcu, make_build()))
    assert steps == {"items": False}
    assert "413" not in applier.problems["items"] and "50 item sets" in applier.problems["items"]
    assert "Supprime" in applier.problems["items"]
    applier2 = BuildApplier(ChampSelectSettings(apply_runes=False, apply_spells=False))
    asyncio.run(applier2.apply(FakeLcu(), make_build()))
    assert applier2.problems == {}  # aucune erreur : rien à expliquer


def test_lcu_body_is_compact_utf8():
    from lolbuilds.app.lcu import encode_body

    raw = encode_body({"title": "Départ", "n": [1, 2]})
    assert raw == '{"title":"Départ","n":[1,2]}'.encode("utf-8")


def test_our_item_set_outranks_the_others():
    existing = {"itemSets": [{"title": "Blitz Ahri", "uid": "a", "sortrank": 7}, {"title": "Vieux set", "uid": "b"},
                             {"title": "[Auto] ancien", "uid": "c", "sortrank": 99}]}
    merged = merge_item_sets(existing, {"title": "[Auto] Ahri Mid", "uid": "new", "sortrank": 0})
    assert [s["uid"] for s in merged["itemSets"]] == ["new", "a", "b"]
    assert merged["itemSets"][0]["sortrank"] == 8 and merged["itemSets"][1]["sortrank"] == 7  # les autres intacts
