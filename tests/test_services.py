import pytest

from walkscape_mcp.services import parse_attrs, parse_requirements


def test_parse_service_attributes_and_requirements():
    attrs = parse_attrs("-10% Work efficiency While doing Carpentry . +5 Steps required While doing Carpentry .")
    assert [(a["stats"][0]["type"], a["stats"][0]["value"], a["stats"][0]["isPercent"]) for a in attrs] == [
        ("workEfficiency", -0.1, True), ("stepsRequired", 5.0, False)]
    assert attrs[0]["requirements"][0]["requirement"] == {"skill": "carpentry"}
    tiers = parse_attrs("Global +1% Double action Have [10] Halfling Rebels faction reputation. "
                        "Global +2% Double action Have [30] Halfling Rebels faction reputation.")
    assert [a["stats"][0]["value"] for a in tiers] == [0.01, 0.01]  # the 30-rep tier adds 1% on top of the first
    reqs = parse_requirements("Requires [3] Diving gear equipped.")
    assert reqs[0]["requirement"] == {"keywords": ["diving_gear"], "quantity": 3}
    assert parse_requirements("Have [77,777] coins.")[0]["requirement"] == {"amount": 77777}


@pytest.fixture
def wiki_svc(svc):
    if not svc.wiki.state().get("path"):
        pytest.skip("no wiki dump downloaded")
    return svc


def test_recipes_pick_the_best_service_location(wiki_svc):
    svc = wiki_svc
    out = svc.optimize_loadout("Cut a willow plank", "actions", pet="none", show_missing_upgrades=False)
    assert out["service"] == "Sawmill of Barbantok (Basic)"  # +17% carpentry work efficiency
    assert any("Crafted at Sawmill of Barbantok" in n for n in out["notes"])
    plain = svc.optimize_loadout("Cut a willow plank", "actions", location="Everhaven", pet="none",
                                 show_missing_upgrades=False)
    assert float(plain["result"].split()[0]) > float(out["result"].split()[0])
    with pytest.raises(ValueError, match="has no sawmill"):
        svc.optimize_loadout("Cut a willow plank", "actions", location="Salsfirth")


def test_service_bonuses_listed_by_find_services(wiki_svc):
    svc = wiki_svc
    rows = svc.find_services("kitchen", "Azurazera")["locations"]
    assert rows[0]["location"] == "Azurazera" and "+10% Bonus experience" in rows[0]["services"][0]


def test_quality_odds_match_the_wiki_examples():
    from walkscape_mcp.quality import quality_odds

    base = quality_odds(20, 0)
    assert round(base["common"] * 100, 2) == 79.20 and round(base["legendary"] * 100, 2) == 0.20
    mid = quality_odds(20, 64)  # level 40 vs 20, +19 gear, +25 consumable
    assert round(mid["common"] * 100, 1) == 64.1 and round(mid["uncommon"] * 100, 2) == 27.35
    high = quality_odds(20, 200)
    assert round(high["common"], 4) == round(high["uncommon"], 4)  # normal is floored at good's weight
    fine = quality_odds(20, 200, fine_materials=True)
    assert fine["common"] == 0 and fine["rare"] == high["uncommon"]


def test_craft_quality(wiki_svc):
    out = wiki_svc.craft_quality("Craft a bronze pickaxe", "Perfect")
    assert out["target"] == "Perfect or better"
    p = float(out["chance_of_target"].rstrip("%")) / 100
    assert out["expected_items_crafted"] == pytest.approx(1 / p, rel=0.01)
    assert out["quality_outcome"]["total"] == out["quality_outcome"]["from_level"] + out["quality_outcome"]["from_gear_and_service"]
    fine = wiki_svc.craft_quality("Craft a bronze pickaxe", "Perfect", fine_materials=True)
    assert fine["expected_steps"] < out["expected_steps"]
    with pytest.raises(ValueError, match="qualities"):
        wiki_svc.craft_quality("Brew beer")
