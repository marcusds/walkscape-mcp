from walkscape_mcp.wikidata import parse_achievement_goals


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
    assert goals("Hatch a Mummy egg [1]") == [{"type": "other", "n": 1}]


def test_achievement_goal_views(svc):
    rows = {a["name"]: a for a in svc.achievements("all")["achievements"]}
    light = rows["Lighthouse"]["goals"][0]
    assert light["type"] == "equip_keyword" and "/5 kinds owned" in light["progress"]
    trees = rows["Only You Can Prevent Forest Fires"]["goals"][0]["ways"]
    assert "Cut birch trees" in trees
    deer = rows["Big Game Hunter"]["goals"][0]["ways"][0]
    assert deer.startswith("Deer hunting") and (svc._player.skill_levels.get("hunting", 1) >= 35) == ("(needs" not in deer)
    assert rows["Tutorial Complete"]["goals"][0]["progress"] == f"{svc._player.char_level}/80"
