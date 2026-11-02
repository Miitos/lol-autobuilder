"""Robustesse de la couche données : réponses mal formées, minuit, rôles périmés, écritures concurrentes, TTL."""

from __future__ import annotations

import json
import threading
from datetime import date, timedelta

import httpx
import pytest

from lolbuilds.cache import BuildCache
from lolbuilds.config import Config, OpggSettings
from lolbuilds.ddragon import ChampionCatalog, ItemCatalog, StaticData
from lolbuilds.live import LiveBuilds, _write
from lolbuilds.sources import BuildSourceError
from lolbuilds.sources.opgg import (
    OpggClient,
    OpggSource,
    build_from_analysis,
    counters_from_guide,
    matchup_variants,
    tier_list,
)
from conftest import make_build


# --------------------------------------------------------------------------- 1. client / parsing

def client_with(handler) -> OpggClient:
    return OpggClient(interval=0, http=httpx.Client(transport=httpx.MockTransport(handler)))


def rpc_handler(body, content_type="application/json"):
    def handler(request: httpx.Request) -> httpx.Response:
        msg = json.loads(request.content)
        if msg["method"] == "initialize":
            return httpx.Response(200, headers={"mcp-session-id": "s1"}, json={"jsonrpc": "2.0", "id": 1, "result": {}})
        if msg["method"] == "notifications/initialized":
            return httpx.Response(202)
        return httpx.Response(200, headers={"content-type": content_type}, content=body)
    return handler


@pytest.mark.parametrize("body", [
    b"<html>maintenance</html>",
    b'{"error": "boom"}',
    b'{"error": {"message": "boom"}}',
    b"[1, 2]",
    b'{"result": [1]}',
    b'{"result": {"content": [{"type": "text", "text": "{pas du json"}]}}',
    b'{"result": {"content": null, "isError": true}}',
])
def test_client_bad_bodies_raise_build_source_error(body):
    with pytest.raises(BuildSourceError):
        client_with(rpc_handler(body)).call("x", {}, retries=0)


def test_client_400_is_not_retried(monkeypatch):
    monkeypatch.setattr("lolbuilds.sources.opgg.time.sleep", lambda _s: None)
    calls = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        msg = json.loads(request.content)
        if msg["method"] == "initialize":
            return httpx.Response(200, headers={"mcp-session-id": "s1"}, json={"result": {}})
        if msg["method"] == "notifications/initialized":
            return httpx.Response(202)
        calls["n"] += 1
        return httpx.Response(400, text="mauvais arguments")

    with pytest.raises(BuildSourceError):
        client_with(handler).call("x", {})
    assert calls["n"] == 1


def test_failed_initialize_keeps_no_session():
    client = client_with(lambda r: httpx.Response(200, headers={"mcp-session-id": "bogus"}, content=b"<html>"))
    with pytest.raises(BuildSourceError):
        client.call("x", {}, retries=0)
    assert client.session_id is None


def test_parsers_survive_explicit_nulls():
    analysis = {"data": {
        "runes": {"primary_page_id": 8100, "secondary_page_id": 8200, "primary_rune_ids": None,
                  "secondary_rune_ids": None, "stat_mod_ids": None, "play": None, "pick_rate": None},
        "summoner_spells": {"ids": None, "play": None},
        "core_items": None, "boots": {"ids": None}, "fourth_items": None, "skills": {"order": None},
    }}
    try:
        build_from_analysis(analysis, champion="Fizz", champion_id=105, role="middle", patch="16.21")
    except BuildSourceError:
        pass  # validate() peut refuser (9 perks) : l'important est de ne pas planter en TypeError/AttributeError
    except ValueError:
        pass
    assert matchup_variants({"data": {"runes": [None, {"play": None}], "summoner_spells": None}}, opponent="X") == []
    assert counters_from_guide({"data": {"counters": [None, {"play": None, "win": None}]}}) == []
    champs = ChampionCatalog(by_key={103: "Ahri"}, names={"Ahri": "Ahri"})
    meta = {"data": {"positions": {"mid": [None, {"champion": None}, {"champion": "Ahri", "tier": None, "play": None}]}}}
    assert tier_list(meta, champs, "middle")[0]["tier"] == 5


@pytest.fixture
def static() -> StaticData:
    champs = ChampionCatalog(by_key={103: "Ahri", 105: "Fizz"}, names={"Ahri": "Ahri", "Fizz": "Fizz"})
    return StaticData(version="16.21.1", champions=champs, items=ItemCatalog())


class FakeClient:
    def __init__(self, response) -> None:
        self.response = response
        self.calls = 0

    def call(self, tool, arguments):
        self.calls += 1
        return self.response

    def close(self) -> None:
        pass


@pytest.mark.parametrize("response", [[], {"data": []}, {"data": {"runes": [], "summoner_spells": 3}}, "texte"])
def test_source_wraps_conversion_errors(static, response):
    src = OpggSource(static, OpggSettings(), FakeClient(response))
    for call in (lambda: src.fetch_build("Fizz", "middle"), lambda: src.fetch_matchup("Fizz", "middle", "Ahri"),
                 lambda: src.fetch_tier_list("middle"), lambda: src.fetch_counters("Ahri", "middle"),
                 lambda: src.roles()):
        try:
            call()
        except BuildSourceError:
            pass


# --------------------------------------------------------------------------- 3. minuit

META = {"data": {"positions": {"mid": [{"champion": "Ahri", "role_rate": 0.9}]}}}


def test_source_roles_refetched_after_midnight(static, monkeypatch):
    client = FakeClient(META)
    src = OpggSource(static, OpggSettings(), client)
    src.roles(), src.roles()
    assert client.calls == 1
    tomorrow = date.today() + timedelta(days=1)

    class FakeDate(date):
        @classmethod
        def today(cls):
            return tomorrow

    monkeypatch.setattr("lolbuilds.sources.opgg.date", FakeDate)
    src.roles()
    src.targets()
    assert client.calls == 2


# --------------------------------------------------------------------------- 4. rôles périmés

class RolesSource:
    def __init__(self, static, roles=None, fail=False) -> None:
        self.static, self._roles, self.fail, self.calls = static, roles, fail, 0

    def roles(self):
        self.calls += 1
        if self.fail:
            raise BuildSourceError("hors ligne")
        return self._roles

    def close(self) -> None:
        pass


def lanes_file(tmp_path, day: date, roles):
    (tmp_path / "opgg").mkdir(exist_ok=True)
    (tmp_path / "opgg" / "lanes.json").write_text(json.dumps({"date": day.isoformat(), "roles": roles}))


def test_roles_fall_back_to_yesterday(tmp_path, static):
    lanes_file(tmp_path, date.today() - timedelta(days=1), {"Ahri": {"middle": 0.9}})
    live = LiveBuilds(Config(cache_dir=tmp_path), BuildCache(tmp_path), source=RolesSource(static, fail=True))
    assert live.roles() == {"Ahri": {"middle": 0.9}}
    assert live.guess_opponent(["Ahri"], "middle") == "Ahri"
    live.roles()
    assert live.source.calls == 1  # pas de réessai à chaque appel
    saved = json.loads((tmp_path / "opgg" / "lanes.json").read_text())
    assert saved["date"] != date.today().isoformat()  # la veille n'est pas re-datée


def test_roles_unavailable_degrades(tmp_path, static):
    live = LiveBuilds(Config(cache_dir=tmp_path), BuildCache(tmp_path), source=RolesSource(static, fail=True))
    assert live.guess_opponent(["Ahri"], "middle") is None
    assert live.champion_roles("Ahri") == []
    assert live.role_for("Ahri", "") is None
    assert live.role_for("Ahri", "top") == "top"


def test_empty_roles_never_written(tmp_path, static):
    live = LiveBuilds(Config(cache_dir=tmp_path), BuildCache(tmp_path), source=RolesSource(static, roles={}))
    assert live.roles() == {}
    assert not (tmp_path / "opgg" / "lanes.json").exists()


def test_roles_cache_without_roles_key(tmp_path, static):
    (tmp_path / "opgg").mkdir()
    (tmp_path / "opgg" / "lanes.json").write_text(json.dumps({"date": date.today().isoformat()}))
    live = LiveBuilds(Config(cache_dir=tmp_path), BuildCache(tmp_path),
                      source=RolesSource(static, roles={"Ahri": {"middle": 1.0}}))
    assert live.roles() == {"Ahri": {"middle": 1.0}}


# --------------------------------------------------------------------------- 5. écritures

def test_write_uses_unique_tmp_and_survives_oserror(tmp_path, monkeypatch):
    seen = []
    real = __import__("os").replace
    monkeypatch.setattr("lolbuilds.live.os.replace", lambda a, b: (seen.append(a.name), real(a, b)))
    _write(tmp_path / "a" / "x.json", {"k": 1})
    assert json.loads((tmp_path / "a" / "x.json").read_text()) == {"k": 1}
    assert str(threading.get_ident()) in seen[0]

    def boom(a, b):
        raise OSError("disque plein")

    monkeypatch.setattr("lolbuilds.live.os.replace", boom)
    _write(tmp_path / "a" / "y.json", {"k": 2})  # ne lève pas
    assert list((tmp_path / "a").glob("*.tmp")) == []


def test_cache_save_tmp_unique_and_corrupt_json_ignored(tmp_path):
    cache = BuildCache(tmp_path)
    path = cache.save(make_build())
    assert path.exists() and not list(tmp_path.glob("*.tmp"))
    (tmp_path / "Bad_top.json").write_text("[1, 2]")
    (tmp_path / "Nul_top.json").write_text("null")
    assert cache.load("Bad", "top") is None
    assert len(cache.all_builds()) == 1


# --------------------------------------------------------------------------- 7. TTL profils

def test_player_profiles_expire(tmp_path, static, monkeypatch):
    live = LiveBuilds(Config(cache_dir=tmp_path), BuildCache(tmp_path), source=RolesSource(static))
    calls = []
    monkeypatch.setattr(live, "_player_call", lambda tool, args: calls.append(tool) or {"n": len(calls)})
    now = [1000.0]
    monkeypatch.setattr("lolbuilds.live.time.monotonic", lambda: now[0])
    assert live.player_profile("A", "EUW", "euw") == {"n": 1}
    assert live.player_profile("a", "euw", "euw") == {"n": 1}  # en mémoire
    now[0] += 599
    live.player_matches("A", "EUW", "euw")
    live.player_matches("A", "EUW", "euw")
    assert len(calls) == 2
    now[0] += 2  # profil > 10 min, parties encore valides
    assert live.player_profile("A", "EUW", "euw") == {"n": 3}
    live.player_matches("A", "EUW", "euw")
    assert len(calls) == 3
