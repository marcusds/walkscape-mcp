import itertools

import pytest


def test_route_legs_connect_and_gear_beats_base_distance(svc):
    out = svc.plan_route("Everhaven", start="Azurazera")
    legs = [l["leg"].split(" → ") for l in out["legs"]]
    assert legs[0][0] == "Azurazera" and legs[-1][1] == "Everhaven"
    assert all(a[1] == b[0] for a, b in itertools.pairwise(legs))
    assert out["steps_swapping_gear_each_leg"] < out["base_steps"]
    assert out["steps_swapping_gear_each_leg"] <= out["single_loadout"]["steps"]
    border = next(l for l in out["legs"] if l["leg"] == "Fort of Permafrost → Noiseless Pass")
    assert border["requires"] == ["Jarvonian border check: have Jarvonian letter of passage with you"]
    assert "gear" in out["legs"][0] and out["single_loadout"]["planner_link"].startswith("https://gear.walkscape.app/")


def test_route_via_and_gear_requirements(svc):
    out = svc.plan_route("Granfiddich", start="Granfiddich", via=["Kelp Forest"])
    underwater = [l for l in out["legs"] if "requires" in l]
    assert underwater and all("diving_gear" in l["requires"][0] for l in underwater)
    with pytest.raises(ValueError, match="No usable route"):
        svc.plan_route("Kelp Forest", start="Granfiddich", avoid=["Granfiddich Shores", "Vastalume"])


def test_find_services_ranks_by_distance(svc):
    out = svc.find_services("sawmill", "Salsfirth")
    rows = out["locations"]
    assert rows[0]["location"] == "Everhaven" and rows[0]["base_steps"] == 1160
    assert rows[0]["route"] == "Salsfirth → Everhaven"
    assert [r["base_steps"] for r in rows] == sorted(r["base_steps"] for r in rows)
    vastalume = next(r for r in rows if r["location"] == "Vastalume")
    assert any("diving" in x for x in vastalume["route_requires"])
    here = svc.find_services("kitchen", "Everhaven")["locations"][0]
    assert here["location"] == "Everhaven" and here["base_steps"] == 0 and "route" not in here
    with pytest.raises(KeyError, match="Kinds"):
        svc.find_services("spaceport", "Everhaven")
