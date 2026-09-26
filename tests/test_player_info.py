import json
from pathlib import Path

import pytest

SAVE = Path(__file__).parent / "fixtures" / "save.json"


@pytest.fixture
def loading_svc(svc, tmp_path, monkeypatch):
    """svc whose load_save works offline."""
    monkeypatch.setattr("walkscape_mcp.service.player_file", lambda: tmp_path / "player.json")
    svc._needs_refresh = lambda *a: False
    svc._stamp_version = lambda v: None
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


def test_unusable_history_skipped_without_losing_the_rest(svc):
    out = svc.remember_player_info(completed=["Mine gold ore", "Classic skiing 50", "Not a real activity"],
                                   notes=["Unlocked achievement: Human Fish"])
    assert out["reached"] == ["Classic skiing completed 50+ times"]
    assert out["notes"] == ["Unlocked achievement: Human Fish"]
    assert len(out["skipped"]) == 2 and "Mine gold ore" in out["skipped"][0]


def test_forget_then_add_replaces_a_note(svc):
    svc.remember_player_info(notes=["Progress: honeycomb 20/100"])
    out = svc.remember_player_info(forget=["honeycomb"], notes=["Progress: honeycomb 33/100"])
    assert out["notes"] == ["Progress: honeycomb 33/100"]


ACH = {name: {"difficulty": "normal", "points": 3, "requirements": "", "rewards": ""}
       for name in ("Masterchef", "Winnie The Pooh", "Human Fish", "Wetlands Explorer")}


@pytest.fixture
def ach_svc(svc):
    svc.achievement_list = lambda: ACH
    return svc


def test_achievements_survive_broad_forget(ach_svc):
    ach_svc.remember_player_info(achievements_unlocked=["masterchef", "Wetlands explorer"],
                                 achievement_progress={"Winnie the Pooh": "41/100"})
    out = ach_svc.remember_player_info(forget=["achievement", "Winnie"])
    assert out["achievements_unlocked"] == ["Masterchef", "Wetlands Explorer"]
    assert list(out["achievements_in_progress"]) == ["Winnie The Pooh"]


def test_unlocking_clears_progress_and_unknown_names_are_skipped(ach_svc):
    ach_svc.remember_player_info(achievement_progress={"Winnie The Pooh": "41/100"})
    out = ach_svc.remember_player_info(achievements_unlocked=["Winnie The Pooh", "Not An Achievement"])
    assert out["achievements_unlocked"] == ["Winnie The Pooh"]
    assert out["achievements_in_progress"] == {}
    assert "Not An Achievement" in out["skipped"][0]
    out = ach_svc.remember_player_info(achievements_not_unlocked=["Winnie The Pooh"])
    assert out["achievements_unlocked"] == []


def test_old_achievement_notes_are_migrated(ach_svc, tmp_path):
    (tmp_path / "player_info.json").write_text(
        '{"notes": ["Unlocked achievement: Human Fish", "Unlocked achievement: Masterchef (120 points)", "Other"]}')
    out = ach_svc.remember_player_info()
    assert out["achievements_unlocked"] == ["Human Fish", "Masterchef"]
    assert out["notes"] == ["Other"]
    rows = {r["name"]: r["status"] for r in ach_svc.achievements("all")["achievements"]}
    assert rows == {"Masterchef": "unlocked", "Winnie The Pooh": "not recorded", "Human Fish": "unlocked",
                    "Wetlands Explorer": "not recorded"}


def test_wiki_achievement_page_parses(svc):
    from walkscape_mcp.wiki import Wiki
    svc.wiki = Wiki()
    if not svc.wiki.state().get("path"):
        pytest.skip("no wiki dump downloaded")
    known = svc.achievement_list()
    assert known["Winnie The Pooh"]["points"] == 3 and known["Winnie The Pooh"]["difficulty"] == "normal"
    assert known["Tutorial Complete"]["points"] == 10
    assert len(known) > 60


def test_updates_since_save_feed_every_tool(svc):
    assert "flippy_spatula@rare" not in svc._player.owned_gear
    cooking = svc._player.skill_levels["cooking"]
    out = svc.remember_player_info(gear_found=["Flippy spatula (rare)", "Berries"], skill_levels={"cooking": cooking + 3},
                                   item_counts={"Berries": 586})
    assert "Berries isn't gear" in out["skipped"][0]
    assert out["since_last_save"] == ["Flippy spatula (rare)", f"cooking {cooking + 3}", "586 Berries"]
    assert "flippy_spatula@rare" in svc._player.owned_gear
    assert svc._player.skill_levels["cooking"] == cooking + 3
    assert svc._player.item_counts["berries"][0] == 586
    assert svc._context("brew_beer", None).skill_levels["cooking"] == cooking + 3


def test_newer_save_drops_updates_it_covers(loading_svc):
    svc = loading_svc
    save = json.loads(SAVE.read_text())
    svc.load_save(json.dumps(save))
    svc.remember_player_info(gear_found=["Flippy spatula (rare)"], item_counts={"Berries": 586})
    out = svc.load_save(json.dumps(save))  # same save again: nothing is covered yet
    assert out["remembered_info"]["since_last_save"] == ["Flippy spatula (rare)", "586 Berries"]
    save["steps"] += 1000
    out = svc.load_save(json.dumps(save))
    assert out["updates_now_in_save"] == ["Flippy spatula (rare)", "586 Berries"]
    assert "since_last_save" not in out["remembered_info"]
    assert "flippy_spatula@rare" not in svc._player.owned_gear


def test_goals_and_explored_regions(svc):
    out = svc.remember_player_info(goals=["190 points for Treasure hunter bandolier", "Cooking 46"],
                                   regions_explored=["Wrentmark", "Atlantis"])
    assert out["goals"] == ["190 points for Treasure hunter bandolier", "Cooking 46"]
    assert out["regions_explored"] == ["Wrentmark"] and "Atlantis" in out["skipped"][0]
    out = svc.remember_player_info(goals_done=["cooking"], forget=["points"])
    assert out["goals"] == ["190 points for Treasure hunter bandolier"]
    assert svc._context("brew_beer", None).explored == {"wrentmark"}


def test_reputation_points_and_carried_gear_since_save(svc):
    base_rep = svc._player.reputation.get("jarvonia", 0)
    carried_before = set(svc._player.carried_gear)
    assert carried_before and carried_before < set(svc._player.owned_gear)  # equipped + inventory, not the bank
    out = svc.remember_player_info(reputation={"Jarvonia": base_rep + 50}, achievement_points=133,
                                   carrying=["Flippy spatula", "Oak skis"])
    assert svc._player.reputation["jarvonia"] == base_rep + 50
    assert svc._player.achievement_points == 133
    assert {oi.id for oi in svc._player.carried_gear.values()} <= {"flippy_spatula", "oak_skis"}
    assert any("Jarvonia reputation" in x for x in out["since_last_save"])
    assert svc._pool(carried_only=True) == list(svc._player.carried_gear.values())


def test_percentage_achievement_requirement(svc):
    from walkscape_mcp.engine import check_requirement

    ctx = svc._context("brew_beer", None)
    half = {"type": "achievementPoint", "requirement": {"isPercentage": True, "value": 0.5}}
    ctx.achievement_points_total, ctx.achievement_points = 265, 132
    assert not check_requirement(half, ctx, None)
    ctx.achievement_points = 133  # the Cape of Half-Achiever unlocked at 133 of 265
    assert check_requirement(half, ctx, None)


def test_carrying_gear_found_in_the_same_call(svc):
    out = svc.remember_player_info(gear_found=["Adventuring sewing needle"], carrying=["Adventuring sewing needle"])
    assert "skipped" not in out, out.get("skipped")
    assert {oi.id for oi in svc._player.carried_gear.values()} == {"adventuring_sewing_needle"}


def test_save_history_and_compare(loading_svc, tmp_path, monkeypatch):
    svc = loading_svc
    save = json.loads(SAVE.read_text())
    svc.load_save(json.dumps(save))
    assert "Need at least two" in svc.compare_saves()["note"]
    later = json.loads(json.dumps(save))
    later["steps"] += 5000
    later["skills"]["foraging"] = later["skills"].get("foraging", 0) + 1234
    later["collectibles"] = [*later.get("collectibles", []), "petrified_branch"]
    slot, worn = next((s, i) for s, i in later["gear"].items() if i)
    later["gear"][slot] = None  # unequipping into the inventory isn't a loss
    later.setdefault("inventory", {})[worn] = later["inventory"].get(worn, 0) + 1
    svc.load_save(json.dumps(later))
    svc.load_save(json.dumps(later))  # the same export again isn't stored twice
    out = svc.compare_saves()
    assert out["steps"] == 5000 and len(out["saves"]) == 2
    assert out["skills"]["foraging"]["xp_gained"] == 1234
    assert out["collectibles_found"] == ["Petrified branch"]
    assert not out["items_used_or_lost"] and "items_gained" in out
