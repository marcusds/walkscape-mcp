from walkscape_mcp.wikidata import parse_achievement_goals, parse_fine_value


def goals(text):
    return [{k: v for k, v in g.items() if k != "text"} for g in parse_achievement_goals(text)]


def test_achievement_goals_are_typed():
    assert goals("Complete actions of the Deer hunting activity. [200] Complete actions of the Bear hunting "
                 "activity. [100]") == [{"type": "actions", "activity": "Deer hunting", "n": 200},
                                        {"type": "actions", "activity": "Bear hunting", "n": 100}]
    assert goals("Complete actions of Woodcutting trees [5,000]") == [
        {"type": "actions_keyword", "keyword": "Woodcutting trees", "n": 5000}]
    assert goals("Have at least [5] Common Loot items equipped. Have at least [1] Legendary Loot items equipped.") == [
        {"type": "equip_quality", "quality": "common", "n": 5}, {"type": "equip_quality", "quality": "legendary", "n": 1}]
    assert goals("At least Agility [80] .") == [{"type": "skill_level", "skill": "agility", "n": 80}]
    assert goals("Have any Food stack at a quantity of [1,000]") == [{"type": "stack", "keyword": "Food", "n": 1000}]
    assert goals('Have obtained "fine" Saltrum [100] Have obtained an eternal Spectral Tool [1]') == [
        {"type": "have_item", "item": "Saltrum", "fine": True, "n": 100},
        {"type": "have_quality", "keyword": "Spectral Tool", "quality": "eternal", "n": 1}]
    assert goals("Craft a Spectral pickaxe. [1]") == [{"type": "craft_item", "item": "Spectral pickaxe", "n": 1}]
    assert goals("Hatch a Mummy egg [1]") == [{"type": "hatch", "item": "Mummy egg", "n": 1}]
    assert goals("Claim a rare Pet egg [1]") == [{"type": "rare_egg", "n": 1}]
    assert goals("Drop an item. [1]") == [{"type": "other", "n": 1}]
    # +712 wording
    assert goals("Gain a Raw shark from an activity . [1]") == [{"type": "gain_item", "item": "Raw shark", "n": 1}]
    assert goals("Gain a fine material from an activity . [1]") == [
        {"type": "gain_keyword", "keyword": "material", "fine": True, "n": 1}]
    assert goals("Gain a fine Trash from an activity . [1]") == [{"type": "gain_item", "item": "Trash", "fine": True,
                                                                "n": 1}]
    assert goals("Gain Ectoplasm (not from trading) [5,000]") == [
        {"type": "gain_item", "item": "Ectoplasm", "not_traded": True, "n": 5000}]
    assert goals("Gain fine Saltrum (not from trading) [100] Gain an eternal Spectral Tool (not from trading) [1]") == [
        {"type": "gain_item", "item": "Saltrum", "fine": True, "not_traded": True, "n": 100},
        {"type": "have_quality", "keyword": "Spectral Tool", "quality": "eternal", "not_traded": True, "n": 1}]
    assert goals("While having work efficiency at least [130%] .") == [
        {"type": "work_efficiency", "percent": True, "n": 130}]
    assert goals("Have a total wealth of coins. [100,000] Ironfoot only (temporarily disabled - will be reworked).") == [
        {"type": "wealth", "n": 100000}, {"type": "ironfoot", "n": 1}]


def test_disabled_achievements_are_flagged_and_not_planned(svc, monkeypatch):
    from walkscape_mcp.wikidata import parse_achievements_page

    page = ("Hard Achievements\nRich | Have a total wealth of coins. [100,000] Ironfoot only (temporarily disabled - "
            "will be reworked). | 5 x Achievement point\nPoor | Have a total wealth of coins. [10] Ironfoot only. | "
            "1 x Achievement point")
    parsed = parse_achievements_page(page)
    assert parsed["Rich"].get("disabled") and not parsed["Poor"].get("disabled")
    monkeypatch.setattr(svc, "achievement_list", lambda: parsed)
    out = svc.plan_achievements(only=["Rich", "Poor"])
    assert {"name": "Rich", "points": 5, "why": ["temporarily disabled in the game"]} in out["not_estimated"]
    assert any(r["name"] == "Poor" for r in out["order"])  # Ironfoot only costs nothing


def test_achievement_goal_views(svc):
    rows = {a["name"]: a for a in svc.achievements("all")["achievements"]}
    light = rows["Lighthouse"]["goals"][0]
    assert light["type"] == "equip_keyword" and "/5 kinds owned" in light["progress"]
    trees = rows["Only You Can Prevent Forest Fires"]["goals"][0]["ways"]
    assert "Cut birch trees" in trees
    deer = rows["Big Game Hunter"]["goals"][0]["ways"][0]
    assert deer.startswith("Deer hunting") and (svc._player.skill_levels.get("hunting", 1) >= 35) == ("(needs" not in deer)
    assert rows["Tutorial Complete"]["goals"][0]["progress"] == f"{svc._player.char_level}/80"


def test_plan_achievements_shares_levelling(svc):
    from dataclasses import replace
    svc._player = replace(svc._player, skill_xp={**svc._player.skill_xp, "hunting": 0})
    out = svc.plan_achievements(targets=[1], only=["It's A Trap!", "Big Game Hunter",
                                                   "One Does Not Simply Walk Into Mordor", "Rare Find"])
    rows = {r["name"]: r for r in out["order"]}
    trap, hunter = rows["It's A Trap!"], rows["Big Game Hunter"]
    # hunting is levelled once: whichever comes second only pays from where the first left off
    first, second = sorted([trap, hunter], key=lambda r: r["total_steps_walked"])
    assert first["levelling"]["hunting"].startswith("1 -> ")
    # its hunting XP is credited to the other, which then needs less levelling
    other = "Big Game Hunter" if first["name"] == "It's A Trap!" else "It's A Trap!"
    assert other in first.get("also_advances", {})
    assert not second.get("levelling") or not second["levelling"]["hunting"].startswith("1 -> ")
    assert "Mordor" in " ".join(rows) and out["not_estimated"][0]["name"] == "Rare Find"
    assert [r["total_steps_walked"] for r in out["order"]] == sorted(r["total_steps_walked"] for r in out["order"])


def test_plan_levels_reputation_and_counts_coins(svc, monkeypatch):
    known = svc.achievement_list()
    wealth = {k: v for k, v in known["All The Things I Could Do"].items() if k != "disabled"}
    monkeypatch.setattr(svc, "achievement_list", lambda: {**known, "All The Things I Could Do": wealth})
    out = svc.plan_achievements(only=["Overprepared", "All The Things I Could Do", "Enter Sandman"])
    rows = {}
    for r in out["order"]:  # an egg's second row is its hatching
        rows.setdefault(r["name"], r)
    if svc._player.reputation.get("jarvonia", 0) < 100:
        assert "jarvonia reputation" in rows["Overprepared"]["levelling"]
    assert "coin drops" in rows["All The Things I Could Do"]["how"][0]
    assert rows["Enter Sandman"]["how"][0].startswith("1 Mummy egg via")
    assert all(r.get("travel_steps", 0) >= 0 for r in out["order"]) and out["travel_factor"] > 0


def test_plan_hatches_eggs_later_and_takes_a_rare_chance(svc):
    out = svc.plan_achievements(only=["Enter Sandman", "Rare Find"], rare_egg_chance=0.05)
    rows = [r for r in out["order"] if r["name"] == "Enter Sandman"]
    found, hatched = rows[0], rows[-1]
    assert "points_when_hatched" in found and hatched["how"] == "egg hatched"
    assert hatched["total_steps_walked"] >= found["total_steps_walked"] + 35_000 - 1
    rare = next(r for r in out["order"] if r["name"] == "Rare Find")
    assert rare["how"][0].startswith("about 20 eggs")
    assert svc.plan_achievements(only=["Rare Find"])["not_estimated"][0]["name"] == "Rare Find"


def test_owned_gear_counts_toward_gear_requirements(svc):
    # Predator fishing needs 3 expert diving gear and a fishing spear; with no spear owned the search leaves the
    # diving gear off too, which mustn't read as "diving gear can't be got"
    from walkscape_mcp.achplan import AchievementPlanner
    from walkscape_mcp.optimizer import Objective
    gd = svc.gd
    expert = [i for i, it in gd.items.items() if "expert_diving_gear" in (it.get("keywords") or [])
              and not it.get("requirements")]
    spears = [i for i, it in gd.items.items() if "fishing_spear" in (it.get("keywords") or [])]
    from dataclasses import replace
    svc._player = replace(svc._player, all_item_ids=(svc._player.all_item_ids | set(expert[:3])) - set(spears))
    svc._supply_cache, svc._supplying = {}, set()
    pl = AchievementPlanner(svc)
    seg = pl._aseg("predator_fishing_spear", Objective("item", "raw_shark"), pl.prereqs("predator_fishing_spear")[0])
    assert seg["gear"] is not None and "spear" in seg["gear"]["how"].lower()


def test_fine_value_from_item_page():
    assert parse_fine_value("Silver bar\nType: | Material\nValue: | 5\nFine Value: | 36\nKeyword: | Bar") == 36
    assert parse_fine_value("Copper sword\nType: | Weapon\nValue: | 3") is None
