"""Wiki facts the game data doesn't have, parsed once per wiki dump into an index file.

Crafting service bonuses, buildings (type, what you can do there, entry requirements, shop stock) and achievements (with their
requirements split into typed goals) live only on the wiki. Parsing them takes many page reads, so it's done when
a new dump arrives and stored in ~/.local/share/walkscape-mcp/wiki/index.json; the server just loads that.

    uv run walkscape-wiki-index    # rebuild now (the server also rebuilds it when the dump changes)
"""

from __future__ import annotations

import json
import os
import re
import time

from .paths import wiki_dir
from .services import parse_building_requirements, parse_buildings_page, parse_services_page, parse_shop_stock

INDEX_VERSION = 4  # bump when the index layout or a parser changes, so old indexes are rebuilt
ACHIEVEMENT_ROW = re.compile(r"(?P<name>[^|]+?) \| (?P<requirements>.+?) \| (?P<rewards>.*?\b(?P<points>\d+) x Achievement point.*)")
N = r"\[(?P<n>[\d,]+)\]"


def index_file():
    return wiki_dir() / "index.json"


# ---------- achievements ----------

def parse_achievements_page(text: str) -> dict[str, dict]:
    out, difficulty = {}, None
    for line in text.splitlines():
        line = line.strip()
        if m := re.fullmatch(r"(Easy|Normal|Hard|Extreme) Achievements", line):
            difficulty = m[1].lower()
        elif m := ACHIEVEMENT_ROW.fullmatch(line):
            out[m["name"]] = {"difficulty": difficulty, "points": int(m["points"]), "requirements": m["requirements"],
                              "rewards": m["rewards"], "goals": parse_achievement_goals(m["requirements"])}
    return out


# Clauses whose count sits mid-sentence; tried before the generic "<text> [N]" clause.
WHOLE_CLAUSES = [
    (rf"Have at least {N} (?P<quality>\w+) Loot items equipped\.",
     lambda m: {"type": "equip_quality", "quality": m["quality"].lower()}),
    (rf"Have every skill at least level {N} \.", lambda m: {"type": "all_skills"}),
    (rf"At least (?P<skill>\w+) {N} \.", lambda m: {"type": "skill_level", "skill": m["skill"].lower()}),
    (rf"Have any (?P<kw>.+?) stack at a quantity of {N}", lambda m: {"type": "stack", "keyword": m["kw"]}),
    (rf"Have a character level of {N}", lambda m: {"type": "character_level"}),
    (rf"While having work efficiency at least {N} %\.", lambda m: {"type": "work_efficiency", "percent": True}),
    (rf"Sell things to {N} different shops\.", lambda m: {"type": "other"}),
]

# "<body> [N]" clauses, classified by their body
BODY_CLAUSES = [
    (r"Complete actions of the (?P<a>.+?) activity\.", lambda m: {"type": "actions", "activity": m["a"]}),
    (r"Complete actions of (?P<kw>.+)", lambda m: {"type": "actions_keyword", "keyword": m["kw"]}),
    (r"Walk a total amount of steps\.", lambda m: {"type": "total_steps"}),
    (r"Go to the (?:village of|location called) (?P<loc>.+)", lambda m: {"type": "visit", "location": m["loc"]}),
    (r"Explore every location in (?P<r>.+)", lambda m: {"type": "explore_region", "region": m["r"]}),
    (r"Explore to find (?P<i>.+)", lambda m: {"type": "gain_item", "item": m["i"]}),
    (r"Gain any (?P<kw>.+?) while (?P<skill>\w+)", lambda m: {"type": "gain_keyword", "keyword": m["kw"],
                                                              "skill": m["skill"].lower()}),
    (r"Gain any (?P<kw>.+?) as a drop from an activity\.", lambda m: {"type": "gain_keyword", "keyword": m["kw"]}),
    (r'Gain a "fine" material from an activity\.', lambda m: {"type": "gain_keyword", "keyword": "material",
                                                              "fine": True}),
    (r'Gain a "fine" (?P<i>.+?) from an activity\.', lambda m: {"type": "gain_item", "item": m["i"], "fine": True}),
    (r"Gain a (?P<i>.+?) from a (?P<skill>\w+) recipe", lambda m: {"type": "craft_item", "item": m["i"]}),
    (r"Gain (?:a |an )?(?P<i>.+?) (?:as a drop )?from (?:any activity|an activity|activities)\.",
     lambda m: {"type": "gain_item", "item": m["i"]}),
    (r"Gain (?P<i>[^.]+)", lambda m: {"type": "gain_item", "item": m["i"]}),
    (r'Find some "fine" (?P<i>.+?) from an activity\.', lambda m: {"type": "gain_item", "item": m["i"], "fine": True}),
    (r"Find (?P<i>.+?) from activities\.", lambda m: {"type": "gain_item", "item": m["i"]}),
    (r"Find a (?P<i>.+)", lambda m: {"type": "gain_item", "item": m["i"]}),
    (r"Forage (?P<i>.+?) from (?P<kw>.+?) activities\.", lambda m: {"type": "gain_item", "item": m["i"],
                                                                     "activity_keyword": m["kw"]}),
    (r"Mine (?P<kw>.+?) from (?P<akw>.+?) activities\.", lambda m: {"type": "gain_keyword", "keyword": m["kw"],
                                                                     "activity_keyword": m["akw"]}),
    (r"Collect (?P<kw>.+?) from activities\.", lambda m: {"type": "gain_keyword", "keyword": m["kw"]}),
    (r"Catch (?P<kw>.+?) while (?P<skill>\w+)", lambda m: {"type": "gain_keyword", "keyword": m["kw"],
                                                           "skill": m["skill"].lower()}),
    (r"Claim a rare Pet egg", lambda m: {"type": "rare_egg"}),
    (r"Hatch any Pet egg", lambda m: {"type": "hatch", "item": None}),
    (r"Hatch an? (?P<i>.+? egg)", lambda m: {"type": "hatch", "item": m["i"]}),
    (r"Complete a (?P<skill>\w+) recipe", lambda m: {"type": "craft_skill", "skill": m["skill"].lower()}),
    (r"Complete different (?P<skill>\w+) recipe options\.", lambda m: {"type": "craft_distinct",
                                                                       "skill": m["skill"].lower()}),
    (r"Complete (?P<i>.+?) recipes", lambda m: {"type": "craft_item", "item": m["i"]}),
    (r"Craft a (?P<kw>.+?) of highest quality\.", lambda m: {"type": "craft_quality", "keyword": m["kw"],
                                                             "quality": "ethereal"}),
    (r"Craft a perfect item\.", lambda m: {"type": "craft_quality", "keyword": None, "quality": "legendary"}),
    (r"Craft any (?P<kw>.+)", lambda m: {"type": "craft_keyword", "keyword": m["kw"]}),
    (r"Craft (?P<kw>.+?) items\.", lambda m: {"type": "craft_keyword", "keyword": m["kw"]}),
    (r"Craft a (?P<i>.+?) \.", lambda m: {"type": "craft_item", "item": m["i"]}),
    (r"Prepare drinks of (?P<kw>.+)", lambda m: {"type": "craft_keyword", "keyword": m["kw"]}),
    (r"Make a (?P<kw>.+)", lambda m: {"type": "craft_keyword", "keyword": m["kw"]}),
    (r"Have different types of (?P<kw>.+?) in your inventory at once\.", lambda m: {"type": "hold_distinct",
                                                                                    "keyword": m["kw"]}),
    (r"Have both Ring slots equipped\.", lambda m: {"type": "equip_keyword", "keyword": "ring"}),
    (r"Have an item equipped in each gear slot\.", lambda m: {"type": "other"}),
    (r"Have different item stacks in the bank\.", lambda m: {"type": "other"}),
    (r"Have (?:several |a |an )?(?P<kw>.+?) equipped(?: at once)?\.", lambda m: {"type": "equip_keyword",
                                                                                 "keyword": m["kw"]}),
    (r"Have a total wealth of coins\.", lambda m: {"type": "wealth"}),
    (r'Have obtained "fine" (?P<i>.+)', lambda m: {"type": "have_item", "item": m["i"], "fine": True}),
    (r"Have obtained an? (?P<q>normal|good|great|excellent|perfect|eternal) (?P<kw>.+)",
     lambda m: {"type": "have_quality", "keyword": m["kw"], "quality": m["q"]}),
    (r'Have obtained (?:an? )?(?P<i>.+)', lambda m: {"type": "have_item", "item": m["i"]}),
    (r"Have (?:an? )?(?P<i>.+)", lambda m: {"type": "have_item", "item": m["i"]}),
    (r"Equip a (?P<i>.+)", lambda m: {"type": "equip_item", "item": m["i"]}),
    (r"While doing the (?P<a>.+?) activity\.", lambda m: {"type": "while_doing", "activity": m["a"]}),
    (r"While crafting with skill (?P<skill>\w+)", lambda m: {"type": "while_skill", "skill": m["skill"].lower()}),
]


def parse_achievement_goals(text: str) -> list[dict]:
    """Split an achievement's requirement text into typed goals, each with its count `n` and the source `text`.
    Anything no pattern knows (hatch an egg, drop an item...) becomes {"type": "other"}."""
    goals, rest = [], text.strip()
    while rest:
        for pat, make in WHOLE_CLAUSES:
            if m := re.match(pat + r"\s*", rest):
                break
        else:
            m = re.match(rf"(?P<body>[^\[]+?)\s*{N}(?: times)?\s*\.?\s*", rest)
            if not m:
                goals.append({"type": "other", "n": 1, "text": rest})
                break
            body = m["body"].strip()
            make = next((mk for pat, mk in BODY_CLAUSES if (bm := re.fullmatch(pat, body))), None)
            goal = make(bm) if make else {"type": "other"}
            goals.append({**goal, "n": int(m["n"].replace(",", "")), "text": m[0].strip()})
            rest = rest[m.end():]
            continue
        goals.append({**make(m), "n": int(m["n"].replace(",", "")), "text": m[0].strip()})
        rest = rest[m.end():]
    return goals


# ---------- the index ----------

def build(wiki) -> dict:
    services = parse_services_page(wiki.page("Services", 1_000_000))
    buildings = parse_buildings_page(wiki.page("Buildings", 1_000_000))
    for name, b in buildings.items():
        try:
            page = wiki.page(name, 50_000)
        except Exception:
            page = ""
        b["requirements"] = parse_building_requirements(page)
        currency = "adventurers_guild_token" if "Outpost" in b["types"] else "coins"
        b["sells"] = [{**x, "currency": currency} for x in parse_shop_stock(page)]
    return {"version": INDEX_VERSION, "tag": wiki.version(), "built_at": time.time(),
            "services": services, "buildings": buildings,
            "achievements": parse_achievements_page(wiki.page("Achievements", 1_000_000))}


def save(index: dict) -> None:
    tmp = index_file().with_suffix(".tmp")
    tmp.write_text(json.dumps(index))
    os.replace(tmp, index_file())


def load(wiki, update: bool = True) -> dict:
    """The index for the current wiki dump and live edits, rebuilding it if either (or the index layout) changed."""
    if update:
        try:
            wiki.update()
        except Exception:
            pass  # offline: keep using the dump we have
    tag = wiki.version()
    try:
        index = json.loads(index_file().read_text())
        if index.get("tag") == tag and index.get("version") == INDEX_VERSION:
            return index
    except (FileNotFoundError, ValueError):
        pass
    index = build(wiki)
    save(index)
    return index


def main():
    from .wiki import Wiki

    wiki = Wiki()
    wiki.update(force=True)
    index = build(wiki)
    save(index)
    goals = [g for a in index["achievements"].values() for g in a["goals"]]
    print(f"wiki {index['tag']}: {len(index['services'])} services, {len(index['buildings'])} buildings, "
          f"{len(index['achievements'])} achievements ({sum(g['type'] != 'other' for g in goals)}/{len(goals)} "
          f"goals parsed) -> {index_file()}")


if __name__ == "__main__":
    main()
