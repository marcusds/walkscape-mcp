import math

from walkscape_mcp.engine import Context, Loadout, drop_report, evaluate, steps_per_item
from walkscape_mcp.gamedata import QUALITIES
from walkscape_mcp.optimizer import Objective, optimize, player_loadout
from walkscape_mcp.player import OwnedItem, character_level, skill_level


def test_levels():
    assert skill_level(0) == 1
    assert skill_level(83) == 2
    assert skill_level(13_034_431) == 99
    assert skill_level(327_932) == 61
    assert character_level(20_562) == 20
    assert character_level(2_943_530) == 68


def test_base_activity_matches_wiki(gd):
    # wiki: Mine gold ore 68 base steps, 40 min steps, mining chest 1/250 actions
    ctx = Context(gd, "mine_gold_ore", "crown_of_cinders", {"mining": 40})
    ev = evaluate(ctx, Loadout())
    assert ev.metrics["steps_per_completion"] == 68
    assert ev.metrics["min_possible_steps_per_completion"] == 40
    assert ev.unmet_activity_requirements == ["with pickaxe equipped"]
    chest = next(d for d in drop_report(ev) if d["id"] == "mining_chest")
    assert chest["steps_per_drop"] == 17000


def test_quality_tiers_stack(gd):
    # wiki: Farganite pickaxe WE 15/18/23/30/37/46% by quality
    we = []
    for q in QUALITIES:
        attrs = gd.item_attrs("farganite_pickaxe", q)
        we.append(round(sum(a["stats"][0]["value"] for a in attrs if a["stats"][0]["type"] == "workEfficiency"), 2))
    assert we == [0.15, 0.18, 0.23, 0.30, 0.37, 0.46]


def test_player_save(player):
    assert player.char_level == 68
    assert player.skill_levels["mining"] == 61
    assert player.equipped["tool_1"] == OwnedItem("farganite_pickaxe", "common")
    assert player.equipped["ring_2"] == OwnedItem("adventuring_ring", "epic")
    assert not player.unknown_ids


def test_camel_penalty_applies_in_desert(gd, player):
    ctx = Context.for_player(gd, player, "mine_gold_ore", "crown_of_cinders")
    lo = player_loadout(player)
    with_camel = evaluate(ctx, lo).metrics["steps_per_completion"]
    lo.pet = None
    without = evaluate(ctx, lo).metrics["steps_per_completion"]
    assert with_camel > without


def test_optimizer_improves_ag_tokens(gd, player):
    ctx = Context.for_player(gd, player, "mine_gold_ore", "crown_of_cinders")
    cur = player_loadout(player)
    obj = Objective("item", "adventurers_guild_token")
    lo, searcher = optimize(ctx, obj, list(player.owned_gear.values()), [("camel", 1)], [None], start=cur,
                            locked={"pet": ("camel", 1)})
    ev = evaluate(ctx, lo)
    assert ev.valid
    assert lo.pet == ("camel", 1)
    best = steps_per_item(ev, "adventurers_guild_token")
    assert best <= steps_per_item(evaluate(ctx, cur), "adventurers_guild_token")
    ids = {oi.id for oi in lo.slots.values() if oi}
    assert {"adventuring_ring", "adoring_fan_statue"} <= ids
    # only one pickaxe (banned keyword), never duplicated items
    assert sum("pickaxe" in (gd.items[i].get("keywords") or []) for i in ids) == 1
    assert math.isfinite(best)



def test_fill_empty_adds_free_side_benefits(gd, player):
    # the actions objective leaves rings empty; filling them must not cost steps and should add the token ring
    ctx = Context.for_player(gd, player, "surface_swimming", "farsand_coast")
    obj = Objective("actions")
    lo, searcher = optimize(ctx, obj, list(player.owned_gear.values()), [("pixie", 3)], [None])
    filled = searcher.fill_empty(lo)
    assert searcher.score(filled)[:2] <= searcher.score(lo)[:2]
    assert all(a >= b for a, b in zip(searcher.side_benefits(filled), searcher.side_benefits(lo)))
    assert "adventuring_ring" in {oi.id for oi in filled.slots.values() if oi}
    assert set(s for s, _ in lo.items()) < set(s for s, _ in filled.items())


def test_meets_multi_piece_gear_requirement(gd, player):
    # "3+ diving gear" can't be reached one piece at a time; the optimizer must place the pieces together
    ctx = Context.for_player(gd, player, "merfolk_farm_foraging", "elaras_lagoon")
    lo, _ = optimize(ctx, Objective("item", "underwater_lotus"), list(player.owned_gear.values()), [None], [None])
    assert evaluate(ctx, lo).valid

def test_history_requirement(gd):
    from walkscape_mcp.engine import check_requirement
    r = {"type": "historyData", "requirement": {"category": "actionCompleted", "data": "classic_skiing", "value": 50}}
    key = "actionCompleted:classic_skiing"
    unknown = Context(gd, "mine_gold_ore", None, {})
    assert check_requirement(r, unknown, None)
    assert unknown.assumed_history == {(key, 50)}
    met = Context(gd, "mine_gold_ore", None, {}, history_met={key: 50})
    assert check_requirement(r, met, None) and not met.assumed_history
    not_met = Context(gd, "mine_gold_ore", None, {}, history_not_met={key: 50})
    assert not check_requirement(r, not_met, None) and not not_met.assumed_history


def test_fishing_rows_scale_with_level(gd):
    from walkscape_mcp.engine import row_weight

    rows = gd.loot_tables["lake_fishing_jarvonia"]["tableRows"]

    def shares(level):
        w = [row_weight(r, {"fishing": level}) for r in rows]
        return [round(x / sum(w) * 100, 3) for x in w]

    # carp, pike, trout per the wiki's Lake fishing table
    assert shares(5) == [100.0, 0.0, 0.0]
    assert shares(11) == [80.851, 19.149, 0.0]
    assert shares(15) == [68.571, 31.429, 0.0]
    assert shares(20) == [51.282, 42.735, 5.983]
    assert shares(29) == [33.708, 28.09, 38.202]
    assert shares(30) == [32.432, 27.027, 40.541]
    net = {r["rowItemID"]: r for r in gd.loot_tables["sea_fishing_jarvonia_net"]["tableRows"]}
    assert row_weight(net["pink_pearl_trinket"], {"fishing": 30}) == 0.03  # full weight, not rounded away
    assert row_weight(net["raw_jellyfish"], {"fishing": 29}) == 0  # below its level requirement


def test_skill_type_level_matches_wiki_thresholds(gd):
    from walkscape_mcp.engine import skill_type_progress

    gathering = [s for s in gd.skills if gd.skill_type(s) == "gathering"]
    # the wiki shows Elderhide tunic needing "55% towards maximum Gathering level [270]"
    levels = {s: 1 for s in gd.skills} | {gathering[0]: 99, gathering[1]: 99, gathering[2]: 75}
    assert sum(v - 1 for s, v in levels.items() if s in gathering) == 270
    assert skill_type_progress(gd, levels, "gathering") >= 0.55
    levels[gathering[2]] = 74
    assert skill_type_progress(gd, levels, "gathering") < 0.55


def test_activity_inputs_are_reported(svc):
    inputs = svc.activity_info("Alligator hunting")["inputs_used_each_action"]
    assert inputs[0].startswith("one arrows item (input for hunting lvl 30+)")
    notes = svc.activity_info("Repair the bank")["inputs_used_each_action"]
    assert notes[0].startswith("50x Ectoplasm")
