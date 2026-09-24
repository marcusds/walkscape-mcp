from walkscape_mcp import gearset
from walkscape_mcp.engine import Loadout
from walkscape_mcp.player import OwnedItem

USER_EXPORT = (
    "H4sIAAAAAAAAA62WbW/bNhDHv0qhN9sA3yKKpCj63YChQz/DUghH8uhokSWPkrIYQb77jtqWtnDceHXfyDLFO/54D3/yqehm2k/F9venYj4eqNgWd4Sh2BTdEOix2JabdQaPP90WXbgttrfrAOy7oRt27R31e6DGBCHLGmxlLCipNKBzDkSsvbelMuTK22JzW/y5YN/Nx9VLwkTr4Iw7HhiWvn/mdSmlMWWgj8+bFyaP/PM2kx8T9m2eDNGIyqOooJGNAFV6Cda4EpQQUkbjK6XiCdIy+HG/H4dLsRz6+69jeUl1jNGDNoojEyMCBuPBNxwUI0rXeHM1hr+jab4gPHMinJZE7d0yzJTaPxifZqicI0OlBVfJAEoFDc44ASiUqIRARCevyt4hdXtMx9cIs905s4n8OIRvMLzDIUxfj4dWFo1DDUYZ3rPXChyJCJZUNBpV7Rtx1Z572r2BELTF6EiCCS5yhdYR0FQSohcmRmOlEs1VCAO9VZ3/NTKlH6b2gMM8QRkbrKJEEGXDVMbVgLUQwKyGG8roSl1XDJHoklp14zhP7Rjb6UAUQOpoqaprCIgZixvbiSpCXUmsnLHBWLoKK7GYXYA19qHd5UeeDxVKa1RtoEZCUDoHq1QlyNoFR9rpqPE7UolzVBgeaJi5s3MeVzJynuNlK36pKOtOw2TWAsuxCxWZWOJpfdOh85eSzePYXxCviGmHA7+27PseHwkak7WGO896xzGrOZ0WMYDW1ltfSe3cKdn/08Mv2c5Gbc0jVz1IdGjrMkBd155PiFJAEzmXSntnvQ/CRX21SH8JVZ2FSlzuR47WbndsHQ73jCKE9TEfaVZx6Weh9k6CJB1d9PwI3zOV8nyRjbm02ohDO804LwRlGVAFEaCsGscnh5HglNRQq1KzkMgyvNKVrIy0yvq38alzfFn12x7zyTbk1Hn0UoAIilOKngtPK9ZaEzxfUpSU9amMXZdSfdHZdHhL/Q7dY3caNHkpE/q5e2CrD8NheXWlU7iPm4IzQqnz7dSz6hbbp6JlznZPM+Y/PT1Qv1bGA6aOI5zdjGmP2dEy0S+uy6DFNmI/0fOG23WYlj26nrJ5XvhDYJuQOgptTEs3t5FPHbYecJ+pf81f3q1f3v34nj/9lNHZDffnfMcTbnCaaJ5u8hg/85315tMq081nrn+eHrJmssyuTMW/K+WSXbe2Y4+0Cmv+04+Ot8FvYVzYU5vjNw7F1j4/80ZyPPIF69N+Ni+Xrs/HuumfDW3ntNCmuKfjX2MKObzF+3HM1+nfXlb9yJ7/BrgcIwp3CwAA"
)


def test_decode_user_export(gd):
    lo, notes = gearset.decode(gd, USER_EXPORT)
    assert lo.slots["head"] == OwnedItem("mining_helmet", "rare")
    assert lo.slots["ring1"] == OwnedItem("adventuring_ring", "epic")
    assert lo.pet == ("pixie", 3)
    assert lo.consumable == ("dried_fruit", True)
    assert not notes


def test_roundtrip(gd):
    lo = Loadout({"tool0": OwnedItem("farganite_pickaxe", "epic"), "neck": OwnedItem("miners_beard", "rare")}, ("camel", 1))
    back, notes = gearset.decode(gd, gearset.encode(gd, lo))
    assert back.slots == lo.slots and back.pet == lo.pet and not notes


def test_planner_link_round_trip(gd):
    from urllib.parse import unquote

    lo = Loadout({"head": OwnedItem("warm_beanie", "rare"), "ring1": OwnedItem("old_gold_ring", "rare"),
                  "tool0": OwnedItem("sharp_machete", "rare")}, ("pixie", 3), ("beer", True))
    link = gearset.encode_link(gd, lo, "cliff_foraging")
    assert link.startswith("https://gear.walkscape.app/?q=")
    d = gearset.decode_link(gd, unquote(link.split("q=")[1]))
    assert (d["activity"], d["recipe"], d["head"], d["ring1"], d["tool0"], d["pet"]) == \
        ("cliff_foraging", None, "warm_beanie", "old_gold_ring", "sharp_machete", "pixie")
    assert d["consumable"] == "beer" and d["consumable_fine"] == "yes"
    assert d["cape"] is None and d["ring0"] is None
    assert gearset.decode_link(gd, unquote(gearset.encode_link(gd, lo, "brew_beer").split("q=")[1]))["recipe"] == "brew_beer"
