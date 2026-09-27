"""Crafting service bonuses and requirements, parsed from the wiki's Services page.

The planner data only lists which services a location has. What each one does (e.g. the Sawmill of Barbantok's
+17% carpentry work efficiency) and what it needs (diving gear, a skill level, reputation) is only on the wiki.
"""

from __future__ import annotations

import re

from .gamedata import norm

ROW = re.compile(r"\| (?P<name>[^|]+?) \| (?P<tier>Basic|Advanced) \| (?P<locations>[^|]*?) \| (?P<attrs>[^|]*?) \| "
                 r"(?P<reqs>[^|]*)")
ATTR = re.compile(r"(?P<global>Global )?(?P<value>[+-][\d.]+)(?P<pct>%?) (?P<stat>[A-Za-z ]+?)"
                  r"(?: While doing (?P<skill>\w+) \.)?(?: Have \[(?P<rep>[\d,.]+)\] (?P<faction>[\w ]+?) faction reputation\.)?"
                  r"(?= [+-]|\s*Global|\s*$)")
STATS = {
    "Work efficiency": "workEfficiency", "No materials consumed": "noMaterialsConsumed",
    "Steps required": "stepsRequired", "Bonus experience": "bonusExperience", "Double rewards": "doubleRewards",
    "Double action": "doubleAction", "Quality outcome": "qualityOutcome", "Chest finding": "chestFind",
    "Fine material finding": "fineMaterialFind",
}


def _reputation_req(amount: str, faction: str) -> dict:
    key = norm(faction).replace("_", " ").title().replace(" ", "")
    key = key[0].lower() + key[1:] + "Reputation"  # "Halfling Rebels" -> halflingRebelsReputation
    return {"type": "gameData", "opposite": False,
            "requirement": {"gameDataId": key, "data": f'{{"double":"{float(amount.replace(",", ""))}"}}'}}


def parse_attrs(text: str) -> list[dict]:
    """'+17% Work efficiency While doing Carpentry .' -> attribute dicts in the game data's format."""
    out = []
    for m in ATTR.finditer(text.strip()):
        stat = STATS.get(m["stat"].strip())
        if not stat:
            continue
        pct = bool(m["pct"])
        value = float(m["value"]) / (100 if pct else 1)
        reqs = []
        if m["skill"]:
            reqs.append({"type": "mainSkill", "opposite": False, "requirement": {"skill": m["skill"].lower()}})
        if m["rep"]:
            reqs.append(_reputation_req(m["rep"], m["faction"]))
        out.append({"statText": m["stat"].strip(), "requirements": reqs,
                    "stats": [{"type": stat, "isPercent": pct, "value": value, "name": m["stat"].strip()}]})
    # reputation tiers ("+1% at 10 rep, +2% at 30 rep") replace each other; stored as increments so they stack right
    last: dict[str, float] = {}
    for a in sorted(out, key=_rep_threshold):
        st = a["stats"][0]
        if any(r["type"] == "gameData" for r in a["requirements"]):
            st["value"], last[st["type"]] = st["value"] - last.get(st["type"], 0.0), st["value"]
    return out


def _rep_threshold(a: dict) -> float:
    for r in a["requirements"]:
        if r["type"] == "gameData":
            return float(r["requirement"]["data"].split('"')[3])
    return 0.0


def parse_requirements(text: str) -> list[dict]:
    """'At least Carpentry lvl. 20.', 'Requires [3] Diving gear equipped.', 'Have item Soup kitchen badge .',
    'Have [77,777] coins.', 'Have [5] Halfling Rebels faction reputation.' -> requirement dicts."""
    reqs, unparsed = [], []
    for part in [p.strip() for p in re.split(r"(?<=\.)\s+(?=[A-Z])", text.strip()) if p.strip()]:
        if part == "None":
            continue
        if m := re.fullmatch(r"At least (\w+) lvl\. (\d+)\.", part):
            reqs.append({"type": "skillLevel", "opposite": False,
                         "requirement": {"skill": m[1].lower(), "level": int(m[2])}})
        elif m := re.fullmatch(r"Requires \[(\d+)\] (.+?) equipped\.", part):
            reqs.append({"type": "distinctKeywordItemsEquipped", "opposite": False,
                         "requirement": {"keywords": [norm(m[2])], "quantity": int(m[1])}})
        elif m := re.fullmatch(r"Have item (.+?) \.", part):
            reqs.append({"type": "itemAnywhere", "opposite": False, "requirement": {"item": norm(m[1])}})
        elif m := re.fullmatch(r"Have \[([\d,]+)\] coins\.", part):
            reqs.append({"type": "totalWealth", "opposite": False,
                         "requirement": {"amount": int(m[1].replace(",", ""))}})
        elif m := re.fullmatch(r"Have \[([\d,.]+)\] ([\w ]+?) faction reputation\.", part):
            reqs.append(_reputation_req(m[1], m[2]))
        elif m := re.fullmatch(r"Have \[([\d,]+)\] achievement points\.", part):
            reqs.append({"type": "achievementPoint", "opposite": False,
                         "requirement": {"value": int(m[1].replace(",", "")), "isPercentage": False}})
        elif m := re.fullmatch(r"Complete actions of the (.+?) activity\. \[([\d,]+)\]", part):
            reqs.append({"type": "historyData", "opposite": False, "requirement": {
                "category": "actionCompleted", "data": norm(m[1]), "value": int(m[2].replace(",", ""))}})
        else:
            unparsed.append(part)
    return reqs + ([{"type": "unparsed", "opposite": False, "requirement": {"text": " ".join(unparsed)}}]
                   if unparsed else [])


def parse_services_page(text: str) -> dict[str, dict]:
    """Service name -> {tier, attrs, requirements} from the wiki's Services page text."""
    out = {}
    for line in text.splitlines():
        m = ROW.search(line)
        if not m or m["name"] == "Service Name":
            continue
        out[m["name"].strip()] = {
            "tier": m["tier"].lower(),
            "attrs": [] if m["attrs"].strip() == "None" else parse_attrs(m["attrs"]),
            "attr_text": re.sub(r"\s+\.", ".", m["attrs"].strip()),
            "requirements": parse_requirements(m["reqs"]),
        }
    return out


def parse_buildings_page(text: str) -> dict[str, dict]:
    """Building name -> {types, actions: {action: note}} from the wiki's Buildings page, which lists buildings
    under "Buildings by Type" (Bank, Tavern, ...) and "Buildings by Action" (Buy items, Withdraw, ...) headings."""
    out: dict[str, dict] = {}
    section = heading = None
    for line in text.splitlines():
        s = line.strip()
        if s in ("Buildings by Type", "Buildings by Action"):
            section, heading = s, None
        elif section and "|" not in s and s:
            heading = None if s.startswith("This page") else s
        elif section and heading and "|" in s:
            cells = [c.strip() for c in s.split("|")]
            if cells[0] == "Building Name":
                continue
            b = out.setdefault(cells[0], {"types": [], "actions": {}})
            if section == "Buildings by Type":
                b["types"].append(heading)
            else:
                b["actions"][heading] = cells[3] if len(cells) > 3 else ""
    return out


def parse_building_requirements(page: str) -> list[dict]:
    """Entry requirements from a building's wiki page ("This building can only be entered if the following
    requirement has been met: Have [150] Jarvonia faction reputation.")."""
    i = page.find("can only be entered if")
    if i < 0:
        return []
    text = " ".join(line.strip() for line in page[page.find(":", i) + 1:].splitlines()[:15] if line.strip())
    m = re.match(r"((?:(?:(?:Have|At least|Requires)[^.]*\.|Complete actions of the [^.]*\. \[[\d,]+\])\s*)+)", text)
    return parse_requirements(m[1]) if m else [{"type": "unparsed", "opposite": False, "requirement": {"text": text[:120]}}]


def parse_shop_stock(page: str) -> list[dict]:
    """Items a building sells, from its wiki page's Inventory section: [{"item", "stock", "price"}]. The page
    doesn't name the currency (the Adventurers' Guild outposts take guild tokens, the rest coins)."""
    i, j = page.find("\nInventory"), page.find("\nPurchases")
    if i < 0:
        return []
    out = []
    for line in page[i:j if j > i else None].splitlines():
        cells = [c.strip() for c in line.split("|")]
        if len(cells) < 3 or cells[0] == "Item Name":
            continue
        try:
            stock, price = int(cells[-2].replace(",", "")), int(cells[-1].replace(",", ""))
        except ValueError:
            continue
        out.append({"item": cells[0], "stock": stock, "price": price})
    return out
