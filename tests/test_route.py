import pytest

from walkscape_mcp.service import Service


@pytest.fixture
def svc(gd, player, tmp_path, monkeypatch):
    monkeypatch.setattr("walkscape_mcp.service.player_info_file", lambda: tmp_path / "player_info.json")
    svc = Service.__new__(Service)
    svc._gd, svc._player, svc._not_met = gd, player, {}
    svc._reload_snapshot = lambda: None
    return svc


def test_route_legs_connect_and_gear_beats_base_distance(svc):
    out = svc.plan_route("Azurazera", "Everhaven")
    legs = [l["leg"].split(" → ") for l in out["legs"]]
    assert legs[0][0] == "Azurazera" and legs[-1][1] == "Everhaven"
    assert all(a[1] == b[0] for a, b in zip(legs, legs[1:]))
    assert out["steps_swapping_gear_each_leg"] < out["base_steps"]
    assert out["steps_swapping_gear_each_leg"] <= out["single_loadout"]["steps"]
    border = next(l for l in out["legs"] if l["leg"] == "Fort of Permafrost → Noiseless Pass")
    assert border["requires"] == ["Jarvonian border check: have Jarvonian letter of passage with you"]
    assert "gear" in out["legs"][0] and out["single_loadout"]["planner_link"].startswith("https://gear.walkscape.app/")


def test_route_via_and_gear_requirements(svc):
    out = svc.plan_route("Granfiddich", "Granfiddich", via=["Kelp Forest"])
    underwater = [l for l in out["legs"] if "requires" in l]
    assert underwater and all("diving_gear" in l["requires"][0] for l in underwater)
    with pytest.raises(ValueError, match="No usable route"):
        svc.plan_route("Granfiddich", "Kelp Forest", avoid=["Granfiddich Shores", "Vastalume"])
