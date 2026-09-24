import pytest

from walkscape_mcp.service import Service


@pytest.fixture
def svc(gd, player, tmp_path, monkeypatch):
    monkeypatch.setattr("walkscape_mcp.service.player_info_file", lambda: tmp_path / "player_info.json")
    svc = Service.__new__(Service)  # skip __init__: no snapshot reload or background refresh
    svc._gd, svc._player, svc._not_met = gd, player, {}
    svc._reload_snapshot = lambda: None
    return svc


def test_reached_is_saved_not_yet_is_session_only(svc, tmp_path):
    out = svc.remember_player_info(completed=["Classic skiing"], not_yet=["travel steps"],
                                   notes=["Unlocked achievement: Ski Champ"])
    assert out["reached"] == ["Classic skiing completed 50+ times"]
    assert out["not_yet_this_session"] == ["125,000+ steps walked while travelling"]
    assert '"stepsWalkedTraveling"' not in (tmp_path / "player_info.json").read_text()
    ctx = svc._context("mine_gold_ore", None)
    assert ctx.history_met == {"actionCompleted:classic_skiing": 50}
    assert ctx.history_not_met == {"stepsWalkedTraveling": 125000}


def test_explicit_threshold_and_corrections(svc):
    svc.remember_player_info(completed=["travel steps 125,000"])
    assert svc._info()["history"] == {"stepsWalkedTraveling": 125000}
    svc.remember_player_info(not_yet=["travel steps 125000"])  # correction drops the saved entry
    assert svc._info()["history"] == {}
    svc.remember_player_info(completed=["travel steps 175000"])  # reaching it clears "not yet"
    assert svc._not_met == {}
    out = svc.remember_player_info(forget=["travelling"])
    assert out["reached"] == []


def test_unknown_history_rejected(svc):
    with pytest.raises(KeyError):
        svc.remember_player_info(completed=["Mine gold ore"])
