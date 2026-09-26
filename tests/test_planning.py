import pytest


def test_remembered_location_is_the_default_start(svc):
    with pytest.raises(ValueError, match="Where is the player"):
        svc.find_services("sawmill")
    out = svc.remember_player_info(location="Salsfirth")
    assert out["current_location"] == "Salsfirth"
    assert svc.find_services("sawmill")["near"] == "Salsfirth"


def test_rank_counts_travel_from_near(svc):
    out = svc.rank_activities("Flax", top=3, near="Kallaheim")
    rows = out["ranking"]
    assert [r["steps_per_item"] for r in rows] == sorted(r["steps_per_item"] for r in rows)
    assert out["from"] == "Kallaheim" and all("travel_steps" in r for r in rows)
    closer = [r for r in rows[1:] if "better_than_fastest_below" in r]
    assert closer and closer[0]["travel_steps"] < rows[0]["travel_steps"]
    few = svc.rank_activities("Flax", top=3, near="Kallaheim", quantity=5)["ranking"]
    assert [r["total_steps"] for r in few] == sorted(r["total_steps"] for r in few)
    assert few[0]["travel_steps"] < rows[0]["travel_steps"]  # for 5 flax the nearby source wins


def test_several_items_at_once(svc):
    out = svc.rank_activities(targets={"Flax": 50, "Honeycomb": 59}, top=2)
    assert out["ranking"][0]["activity"] == "Butterfly catching"
    lo = svc.optimize_loadout("Butterfly catching", "items", targets={"Flax": 50, "Honeycomb": 59},
                              pet="none", show_missing_upgrades=False)
    assert lo["result"].endswith("steps to get all of them") and "50 Flax" in lo["objective"]


def test_plan_recipe_and_steps_to_level(svc):
    out = svc.plan_recipe("Brew beer", 50)
    wheat = out["materials"][0]
    assert wheat["item"] == "Wheat" and wheat["need"] > 0
    assert wheat["short"] == max(0, wheat["need"] - wheat["have"])
    assert out["total_steps"] == out["crafting_steps"] + out["steps_gathering_shortfall"]
    cur = svc._player.skill_levels["cooking"]
    lvl = svc.steps_to_level("cooking", cur + 1, "Brew beer")
    assert lvl["xp_needed"] > 0 and lvl["steps"] == pytest.approx(lvl["xp_needed"] / lvl["xp_per_step"], abs=1)


def test_inventory_fill_counts_partial_stacks(svc):
    empty = svc.inventory_fill("Cliff foraging", 5)
    # berries already held top up their stack before opening a new slot
    partial = svc.inventory_fill("Cliff foraging", 5, inventory={"Berries": 10})
    assert partial["steps_until_full"] > empty["steps_until_full"]
    assert sum(r["new_slots"] for r in empty["at_that_point"]) >= 5


def test_rank_fine_items(svc):
    out = svc.rank_activities("Bell pepper", fine=True)
    rows = out["ranking"]
    assert [r["steps_per_fine_item"] for r in rows] == sorted(r["steps_per_fine_item"] for r in rows)
    assert out["target"] == "Bell pepper (fine)"
    assert rows[0]["activity"] == "Summer cave foraging"
    assert rows[0]["steps_per_fine_item"] < rows[1]["steps_per_fine_item"]
    plain = svc.rank_activities("Bell pepper")["ranking"][0]
    assert plain["steps_per_item"] < rows[0]["steps_per_fine_item"]


def test_hidden_activities_are_flagged_or_skipped(svc):
    rows = svc.rank_activities("Bell pepper", fine=True)["ranking"]
    summer = next(r for r in rows if r["activity"] == "Summer cave foraging")
    assert "Spring bat tracking completed 1+ times" in summer["hidden_activity"]
    notes = svc.optimize_loadout("Summer cave foraging", "fine_item", "Bell pepper", pet="none",
                                 show_missing_upgrades=False)["notes"]
    assert any(n.startswith("Hidden activity") for n in notes)
    assert svc.activity_info("Summer cave foraging")["visibility"]["status"] == "assumed"

    svc.remember_player_info(not_yet=["Spring bat tracking 1"])
    out = svc.rank_activities("Bell pepper", fine=True)
    assert [r["activity"] for r in out["ranking"]] == ["Swamp foraging"]
    assert out["blocked_sources"][0]["unmet"] == ["hidden until Spring bat tracking completed 1+ times"]

    svc.remember_player_info(completed=["Spring bat tracking"])
    assert "hidden_activity" not in svc.rank_activities("Bell pepper", fine=True)["ranking"][0]


def test_loadouts_fill_every_slot(svc):
    from walkscape_mcp.engine import SLOT_ORDER

    tools = svc._player and svc._context("treasure_hunt", "horn_of_respite").tool_slots
    slots = [s for s in SLOT_ORDER if not s.startswith("tool") or int(s[4:]) < tools]
    out = svc.optimize_loadout("Treasure hunt", "item", "Adventurers' Guild token", pet="none",
                               show_missing_upgrades=False)
    owned_types = {svc.gd.items[oi.id]["gearType"] for oi in svc._player.owned_gear.values()}
    expected = [s for s in slots if s.rstrip("0123456789") in owned_types]
    assert len(out["loadout"]["slots"]) == len(expected)


def test_two_copies_of_a_ring_can_be_worn(svc):
    ring = next(oi for oi in svc._player.owned_gear.values() if svc.gd.items[oi.id]["gearType"] == "ring")
    before = svc._pool().count(ring)
    svc.remember_player_info(gear_found=[f"{svc.gd.name(ring.id)} ({ring.quality})"])
    assert svc._pool().count(ring) == min(before + 1, 2)
    if ring.id == "adventuring_ring":
        out = svc.optimize_loadout("Treasure hunt", "item", "Adventurers' Guild token", pet="none",
                                   show_missing_upgrades=False)
        rings = [v["item"] for k, v in out["loadout"]["slots"].items() if k.startswith("ring")]
        assert rings.count("Adventuring ring (epic)") == 2


def test_rounding_noise_does_not_leave_slots_empty():
    from walkscape_mcp.optimizer import _worse

    assert not _worse((0, 97499.99999999993), (0, 97499.99999999991))
    assert _worse((0, 97500.1), (0, 97499.9)) and _worse((1, 1.0), (0, 2.0))


def test_steps_to_level_flags_activities_above_your_level(svc):
    hunting = svc._player.skill_levels.get("hunting", 1)
    out = svc.steps_to_level("hunting", 45, "Box trapping")
    if hunting < 40:
        assert "hunting lvl 40" in out["cannot_do_yet"]
    assert out["uses_up_each_action"][0].startswith("one hunting trap item")
