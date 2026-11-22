import math

import pytest

from walkscape_mcp.skillplan import Candidate, owned_crafts, plan


def level_of(xp):  # 100 XP per level keeps the numbers readable
    return 1 + int(xp // 100)


def xp_for(level):
    return (level - 1) * 100


def costs(table):
    return lambda iid: table.get(iid)


def test_owned_materials_first_then_the_cheapest_per_xp():
    ore = Candidate("smelt", "Smelt", 1, xp_per_craft=10, steps_per_craft=10, groups=[[("ore", 1)]])
    free = Candidate("whittle", "Whittle", 1, xp_per_craft=5, steps_per_craft=10)
    p = plan([ore, free], 0, 100, level_of, xp_for, {"ore": 5}, costs({"ore": 100}))
    assert [(s.candidate.id, s.crafts) for s in p.segments] == [("smelt", 5), ("whittle", 10)]
    assert p.segments[0].used == {"ore": 5} and not p.segments[0].gathered
    assert p.xp_reached >= 100 and p.stuck is None
    assert p.steps == 5 * 10 + 10 * 10


def test_gathering_is_counted_when_it_still_wins():
    ore = Candidate("smelt", "Smelt", 1, xp_per_craft=10, steps_per_craft=10, groups=[[("ore", 1)]])
    slow = Candidate("whittle", "Whittle", 1, xp_per_craft=1, steps_per_craft=10)
    p = plan([ore, slow], 0, 50, level_of, xp_for, {"ore": 2}, costs({"ore": 5}))
    (seg,) = p.segments  # 2 owned, then 3 gathered at 5 steps each: still beats 10 steps/XP
    assert seg.crafts == 5 and seg.used == {"ore": 2} and seg.gathered == {"ore": 3}
    assert seg.gather_steps == 15 and p.steps == 50 + 15


def test_switches_when_a_better_recipe_unlocks():
    low = Candidate("a", "A", 1, xp_per_craft=10, steps_per_craft=20)
    high = Candidate("b", "B", 2, xp_per_craft=10, steps_per_craft=5)
    p = plan([low, high], 0, 300, level_of, xp_for, {}, costs({}))
    assert [s.candidate.id for s in p.segments] == ["a", "b"]
    assert p.segments[0].xp_to == pytest.approx(100)  # stops at the unlock, not the target


def test_what_a_craft_makes_feeds_a_later_recipe():
    smelt = Candidate("smelt", "Smelt", 1, xp_per_craft=5, steps_per_craft=10, groups=[[("ore", 2)]],
                      output=("bar", 1))
    smith = Candidate("smith", "Smith", 2, xp_per_craft=40, steps_per_craft=10, groups=[[("bar", 1)]])
    p = plan([smelt, smith], 0, 300, level_of, xp_for, {"ore": 100}, costs({}))  # bars can't be gathered
    ids = [s.candidate.id for s in p.segments]
    assert ids[:2] == ["smelt", "smith"]
    assert p.segments[1].used.get("bar")


def test_mixed_options_fill_one_material_slot():
    assert owned_crafts([("silver_ore", 1), ("silver_nugget", 4)], {"silver_ore": 3, "silver_nugget": 9}) == 5
    c = Candidate("ring", "Ring", 1, xp_per_craft=10, steps_per_craft=10,
                  groups=[[("silver_ore", 1), ("silver_nugget", 4)]])
    p = plan([c], 0, 50, level_of, xp_for, {"silver_ore": 3, "silver_nugget": 9}, costs({}))
    assert p.segments[0].crafts == 5 and p.segments[0].used == {"silver_ore": 3, "silver_nugget": 8}


def test_stops_short_with_a_reason_when_nothing_is_obtainable():
    c = Candidate("smelt", "Smelt", 1, xp_per_craft=10, steps_per_craft=10, groups=[[("ore", 1)]])
    p = plan([c], 0, 100, level_of, xp_for, {"ore": 3}, costs({}))
    assert p.xp_reached == 30 and p.stuck and "level 1" in p.stuck
    assert sum(s.crafts for s in p.segments) == 3


def test_no_materials_consumed_stretches_owned_stock():
    c = Candidate("smelt", "Smelt", 1, xp_per_craft=10, steps_per_craft=10, groups=[[("ore", 0.5)]])
    p = plan([c], 0, 100, level_of, xp_for, {"ore": 5}, costs({}))
    assert p.segments[0].crafts == 10 and math.isclose(p.segments[0].used["ore"], 5)


def test_plan_skill_leveling_on_the_fixture_save(svc):
    cur = svc._player.skill_levels["cooking"]
    out = svc.plan_skill_leveling("cooking", min(cur + 3, 99))
    assert out["plan"], out
    assert out["total_steps"] == pytest.approx(out["crafting_steps"] + out["gathering_steps"], abs=2)
    assert out["reaches_level"] >= out["target_level"] or "stops_short" in out
    for row in out["plan"]:
        assert row["crafts"] > 0 and row["xp_per_step"] > 0 and row["planner_link"]
    owned_only = svc.plan_skill_leveling("cooking", min(cur + 3, 99), owned_only=True)
    assert owned_only["gathering_steps"] == 0
