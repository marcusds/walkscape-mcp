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
