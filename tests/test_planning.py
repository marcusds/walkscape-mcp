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
    trap = out["inputs"][0]
    assert trap["input"].startswith("one hunting trap item") and trap["need"] == out["completions"]
    assert trap["get"]["item"] == "Boxtrap" and trap["get"]["steps"] == out["steps_getting_inputs"] > 0
    assert out["total_steps"] == out["steps"] + out["steps_getting_inputs"]


def test_rank_counts_the_inputs_used_up(svc):
    from dataclasses import replace
    svc._player = replace(svc._player, skill_xp={**svc._player.skill_xp, "hunting": 10**8})
    out = svc.rank_activities("Basic hide", top=2, quantity=100, owned_only=False)
    assert out["ranking"]
    for r in out["ranking"]:
        assert r["inputs"][0]["input"].startswith("one arrows item") and r["input_steps"] > 0
        assert r["total_steps"] == pytest.approx(r["steps_per_item"] * 100 + r["input_steps"], abs=10)


def test_recipes_naming_their_service_by_keywords(svc):
    # Make linen cloth asks for keywords ["loom"] rather than serviceKeyword; spectral gear for ["workshop", "cursed"]
    out = svc.plan_recipe("Make linen cloth", 5, near="Halfling Campgrounds")
    assert "Loom" in out["craft_at"] and out["nearest_service"]["service"] == "loom (basic)"
    spectral = svc.optimize_loadout("Craft a spectral hatchet", "actions", pet="none", show_missing_upgrades=False)
    assert spectral["service"].startswith("Cursed Workshop")


def test_find_buildings(svc):
    # Azurazera's bank is the Cold Storage of Commitment: no "bank" in its id, so the type comes from the wiki
    bank = svc.find_services("bank", near="Azurazera", top=2)["locations"][0]
    assert bank["location"] == "Azurazera"
    assert bank["services"][0].startswith("Cold Storage of Commitment (Bank): deposit and withdraw "
                                          "[entry needs: 150+ Jarvonia reputation")
    assert "Polar Pruning (Barber)" in svc.location_info("Azurazera")["buildings"]
    assert svc.find_services("job board", near="Kallaheim", top=1)["locations"][0]["location"] == "Kallaheim"
    assert svc.service_table()["heatstroke_metalworks_advanced"]["kind"] == "forge"


def _at_hunting(svc, xp):
    from dataclasses import replace
    svc._player = replace(svc._player, skill_xp={**svc._player.skill_xp, "hunting": xp})


def test_plan_recipe_crafts_or_flags_the_shortfall(svc):
    # crab rolls need bread (crafted from wheat) and raw crab, which only hunting activities drop
    _at_hunting(svc, 0)
    out = svc.plan_recipe("Make crab rolls", 200)
    mats = {m["item"]: m for m in out["materials"]}
    assert mats["Bread"]["gather"]["craft"] == "Bake bread" and mats["Bread"]["gather"]["steps_for_shortfall"] > 0
    assert "cannot_get" in mats["Raw crab"]["gather"] and "Raw crab" in out["total_steps_leaves_out"]
    assert out["total_steps"] == out["crafting_steps"] + out["steps_gathering_shortfall"]


def test_list_and_compare_activities(svc):
    trees = svc.list_activities(keyword="woodcutting_trees")["activities"]
    assert len(trees) >= 10 and {"Cut birch trees", "Cut mangrove trees"} <= {t["name"] for t in trees}
    assert [t["name"] for t in svc.list_activities(skill="tailoring", doable_only=True)["activities"]] == ["Make linen cloth"]
    assert "Bread" in {m for r in svc.list_activities(makes="food")["activities"] for m in r.get("makes", [])}
    out = svc.compare_activities(names=["Cut birch trees", "Cut oak trees", "Cut yew trees"], count=100)
    rows = out["ranking"]
    assert rows[0]["name"] == "Cut birch trees" and rows[0]["steps_for_count"] < rows[1]["steps_for_count"]
    assert out["not_doable"][0]["missing"] == ["woodcutting lvl 60"]


def test_cheapest_with_keyword_counts_stock(svc):
    out = svc.cheapest_with_keyword("fibrous_plant", 140, top=10)
    rows = out["ranking"]
    assert {"Hemp", "Flax"} <= {r["item"] for r in rows}
    assert all(r["short"] == max(0, 140 - r["have"]) and (r["steps"] == 0) == (r["short"] == 0) for r in rows)
    assert [r["steps"] for r in rows] == sorted(r["steps"] for r in rows)


def test_plan_recipe_names_what_blocks_a_material(svc):
    _at_hunting(svc, 0)
    gather = svc.plan_recipe("Cook meat", 20)["materials"][0]["gather"]
    assert any(b.startswith("Squirrel hunting") and "hunting lvl 15" in b for b in gather["blocked_activities"])
    _at_hunting(svc, 10**8)  # at hunting 99 the same plan farms the meat instead
    assert "steps_for_shortfall" in svc.plan_recipe("Cook meat", 20)["materials"][0]["gather"]


def test_loadout_cache(svc, tmp_path, monkeypatch):
    from walkscape_mcp.optimizer import Objective
    monkeypatch.setenv("WALKSCAPE_LOADOUT_CACHE", "1")
    monkeypatch.setattr("walkscape_mcp.service.loadout_cache_file", lambda: tmp_path / "cache.sqlite")
    ctx, lo, ev = svc._best_loadout("cut_birch_trees", Objective("actions"))
    searched = []
    monkeypatch.setattr(svc, "_search_best_loadout", lambda *a, **k: searched.append(a))
    ctx2, lo2, ev2 = svc._best_loadout("cut_birch_trees", Objective("actions"))
    assert not searched and lo2.slots == lo.slots and ev2.metrics == ev.metrics
    # a changed character misses the cache
    from dataclasses import replace
    svc._player = replace(svc._player, skill_xp={**svc._player.skill_xp, "woodcutting": 10**7})
    try:
        svc._best_loadout("cut_birch_trees", Objective("actions"))
    except TypeError:
        pass  # the stub returns None; reaching it proves the lookup missed
    assert searched


def test_equal_locations_go_to_the_nearest(svc):
    # Litter looting is the same at Kallaheim and Granfiddich; from Kallaheim, stay there
    svc.remember_player_info(location="Kallaheim")
    assert svc.optimize_loadout("Litter looting", "fine_item", "Trash", pet="none",
                                show_missing_upgrades=False)["location"] == "Kallaheim"
    # identical basic trinketry benches: the nearest one is used
    out = svc.plan_recipe("Create a silver ring", 50, near="Kallaheim")
    assert out["craft_at"].endswith("Salsfirth")


def test_material_options_are_combined(svc):
    # a silver bar takes 2 silver ore or 7 silver nuggets: 10 ore + 70 nuggets make 15 bars without gathering
    from dataclasses import replace
    counts = {**svc._player.item_counts, "silver_ore": (10, 0), "silver_nugget": (70, 0)}
    svc._player = replace(svc._player, item_counts=counts)
    out = svc.plan_recipe("Smelt a silver bar", 12)
    mat = out["materials"][0]
    assert mat["short"] == 0 and out["steps_gathering_shortfall"] == 0
    assert mat.get("plus_owned"), mat
