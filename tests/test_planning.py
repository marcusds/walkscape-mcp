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
