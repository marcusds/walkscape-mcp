"""Indexed, read-only view over a gear planner API snapshot."""

from __future__ import annotations

import copy
import difflib
import re
from functools import cached_property

QUALITIES = ["common", "uncommon", "rare", "epic", "legendary", "ethereal"]
# In-game names for crafted item qualities (the save/export use the internal names above)
QUALITY_NAMES = {
    "common": "Normal",
    "uncommon": "Good",
    "rare": "Great",
    "epic": "Excellent",
    "legendary": "Perfect",
    "ethereal": "Eternal",
}
GEAR_SLOTS = ["head", "cape", "back", "chest", "primary", "secondary", "hands", "legs", "neck", "feet", "ring", "tool"]


def norm(s: str) -> str:
    return re.sub(r"[^a-z0-9]+", "_", s.lower().replace("'", "")).strip("_")


def strip_markup(s: str | None) -> str:
    if not s:
        return ""
    s = re.sub(r'<object id="([^"]+)"\s*/>', lambda m: m.group(1).replace("_", " "), s)
    return re.sub(r"<[^>]+>", "", s)


class GameData:
    def __init__(self, snap: dict):
        self.snap = snap
        self.items: dict[str, dict] = {}
        for g in snap["items_categorized"]:
            for c in g["categories"]:
                for it in c["items"]:
                    if "type" in it:  # the "Pets" category holds pet summaries, not items
                        self.items[it["id"]] = it
        for it in snap["materials"] + snap["containers"]:
            self.items.setdefault(it["id"], it)
        for it in snap["items_list"]:
            self.items.setdefault(it["id"], {"id": it["id"], "name": it["name"], "type": "other", "keywords": []})
        self.activities: dict[str, dict] = snap["activities"]
        self.locations: dict[str, dict] = snap["locations"]
        self.pets: dict[str, dict] = snap["pets"]
        self.recipes: dict[str, dict] = snap["recipes"]
        self.loot_tables: dict[str, dict] = snap["loot_tables"]
        self.abilities: dict[str, dict] = snap["abilities"]
        self.keywords = {k["id"]: k for k in snap["keywords"]}
        self.skills = {s["id"]: s for s in snap["skills"]}
        self.factions = {f["id"]: f for f in snap["factions"]}
        # gameData requirements reference e.g. "erdwiseReputation"
        self.reputation_key_to_faction = {f["reputation"]: f["id"] for f in snap["factions"] if f.get("reputation")}
        for f in snap["factions"]:
            self.reputation_key_to_faction.setdefault(f"{f['id']}Reputation", f["id"])

    @property
    def meta(self) -> dict:
        return self.snap.get("_meta", {})

    # ---------- lookup ----------

    @cached_property
    def _name_index(self) -> list[tuple[str, str, str, str]]:
        """(normalized name, kind, id, display name)."""
        idx = []
        for kind, coll in (
            ("item", self.items),
            ("activity", self.activities),
            ("location", self.locations),
            ("pet", self.pets),
            ("recipe", self.recipes),
            ("keyword", self.keywords),
        ):
            for i, obj in coll.items():
                name = obj.get("name") or i
                idx.append((norm(name), kind, i, name))
                if norm(i) != norm(name):
                    idx.append((norm(i), kind, i, name))
        return idx

    def search(self, query: str, kinds: list[str] | None = None, limit: int = 10) -> list[dict]:
        q = norm(query)
        scored = []
        for n, kind, i, name in self._name_index:
            if kinds and kind not in kinds:
                continue
            if n == q:
                score = 1.0
            elif q in n:
                score = 0.9 - 0.3 * (len(n) - len(q)) / max(len(n), 1)
            else:
                score = difflib.SequenceMatcher(None, q, n).ratio() * 0.8
            if score > 0.45:
                scored.append((score, kind, i, name))
        scored.sort(key=lambda x: -x[0])
        out, seen = [], set()
        for score, kind, i, name in scored:
            if (kind, i) in seen:
                continue
            seen.add((kind, i))
            out.append({"kind": kind, "id": i, "name": name, "score": round(score, 2)})
            if len(out) >= limit:
                break
        return out

    def resolve(self, query: str, kind: str) -> str:
        coll = {"item": self.items, "activity": self.activities, "location": self.locations, "pet": self.pets, "recipe": self.recipes}[kind]
        if query in coll:
            return query
        n = norm(query)
        if n in coll:
            return n
        hits = self.search(query, [kind], 1)
        if not hits or hits[0]["score"] < 0.6:
            raise KeyError(f"No {kind} matching {query!r}")
        return hits[0]["id"]

    def name(self, item_id: str) -> str:
        return (self.items.get(item_id) or {}).get("name", item_id)

    # ---------- attributes ----------

    @staticmethod
    def _attr_key(a: dict) -> str:
        req_types = "|".join(sorted(r["type"] for r in a.get("requirements") or []))
        return f"{a['stats'][0]['type']}-{a.get('skillText', '')}-{req_types}"

    def item_attrs(self, item_id: str, quality: str | None = None) -> list[dict]:
        """Attributes for an item at a quality. Mirrors the planner: higher quality tiers
        stack cumulatively on top of the base attributes."""
        item = self.items[item_id]
        quality = quality or item.get("quality") or "common"
        attrs = copy.deepcopy(item.get("itemAttrs") or [])
        tiers = sorted(item.get("itemQualityAttrs") or [], key=lambda t: QUALITIES.index(t["quality"]))
        if tiers:
            keys = [self._attr_key(a) for a in attrs]
            for tier in tiers:
                if QUALITIES.index(tier["quality"]) > QUALITIES.index(quality):
                    break
                for a in copy.deepcopy(tier["attributes"]):
                    if not a.get("stats"):
                        continue
                    k = self._attr_key(a)
                    if k in keys:
                        s = attrs[keys.index(k)]["stats"][0]
                        s["value"] = round(s["value"] + a["stats"][0]["value"], 4)
                    else:
                        attrs.append(a)
                        keys.append(k)
        return attrs

    def consumable_attrs(self, item_id: str, fine: bool = False) -> list[dict]:
        item = self.items[item_id]
        buffs = [b for blk in item.get("buffs") or [] for d in blk["data"] for b in d["buffs"]]
        if not buffs:
            return []
        return copy.deepcopy(buffs[0].get("fineAttributes" if fine else "attributes") or [])

    def pet_attrs(self, species: str, level: int) -> list[dict]:
        pet = self.pets[species]
        lv = [l for l in pet.get("levels", []) if l["level"] == level]
        return copy.deepcopy(lv[0]["attributes"]) if lv else []

    def pet_abilities(self, species: str, level: int) -> list[str]:
        return [a["ability"] for a in self.pets[species].get("abilities", []) if a["unlockLevel"] <= level]

    def item_qualities(self, item_id: str) -> list[str]:
        item = self.items[item_id]
        if item.get("itemQualityAttrs") or item.get("type") == "crafted":
            return QUALITIES
        return [item.get("quality") or "common"]

    def is_gear(self, item_id: str) -> bool:
        return bool((self.items.get(item_id) or {}).get("gearType"))

    def activity_like(self, activity_id: str) -> dict:
        """Activities and recipes share the fields the engine needs; normalize recipes to activity shape."""
        if activity_id in self.activities:
            return self.activities[activity_id]
        r = self.recipes[activity_id]
        return {**r, "relatedSkillsList": r.get("relatedSkills") or [], "xpRewardsMap": r.get("xpRewards") or {}}

    @cached_property
    def item_sources(self) -> dict[str, list[dict]]:
        """item id -> where it comes from (activity drops, chests, recipes, 'chance to find' gear)."""
        by_table: dict[str, list[tuple[str, str]]] = {}
        for aid, a in self.activities.items():
            for g in a.get("tables") or []:
                for t in g.get("tables") or []:
                    by_table.setdefault(t, []).append(("activity", aid))
        for iid, it in self.items.items():
            for g in it.get("tables") or []:
                for t in g.get("tables") or []:
                    by_table.setdefault(t, []).append(("container", iid))
            for a in it.get("itemAttrs") or []:
                if any(s.get("type") == "rollSpecialTable" for s in a.get("stats") or []):
                    for g in a.get("tables") or []:
                        for t in g.get("tables") or []:
                            by_table.setdefault(t, []).append(("gear_special", iid))
        out: dict[str, list[dict]] = {}
        for tid, t in self.loot_tables.items():
            rows = list(t.get("tableRows") or []) + [r for st in t.get("subTables") or [] for r in st.get("tableRows") or []]
            for r in rows:
                iid = r.get("rowItemID")
                if not iid:
                    continue
                for kind, src in by_table.get(tid, []):
                    entry = {"kind": kind, "id": src, "table": tid}
                    if entry not in out.setdefault(iid, []):
                        out[iid].append(entry)
        for rid, r in self.recipes.items():
            for iid in r.get("itemRewards") or {}:
                out.setdefault(iid, []).append({"kind": "recipe", "id": rid})
        return out

    def activity_locations(self, activity_id: str) -> list[str]:
        return [l for l, loc in self.locations.items() if activity_id in (loc.get("activityList") or [])]

    def skill_type(self, skill: str) -> str | None:
        return (self.skills.get(skill) or {}).get("type")

    # ---------- text ----------

    def describe_attr(self, a: dict) -> str:
        s = a["stats"][0]
        v = s["value"]
        val = f"{v * 100:+.4g}%" if s.get("isPercent") else f"{v:+g}"
        label = strip_markup(a.get("customText")) or a.get("statText") or s["name"]
        if s["type"] == "rollSpecialTable":
            text = f"{val} {label}"
        else:
            text = f"{val} {label}"
        conds = [describe_requirement(r) for r in a.get("requirements") or []]
        return text + (f" ({'; '.join(conds)})" if conds else "")


def describe_requirement(r: dict) -> str:
    t, q = r["type"], r.get("requirement") or {}
    neg = "NOT " if r.get("opposite") else ""
    match t:
        case "mainSkill":
            s = f"while doing {q.get('skill')}"
        case "mainSkillType":
            s = f"while doing {q.get('type')} skills"
        case "locationHasKeywords":
            s = f"at {'/'.join(q.get('keywords', []))} location"
        case "distinctKeywordItemsEquipped":
            s = f"with {q.get('quantity')}+ {'/'.join(q.get('keywords', []))} items equipped"
        case "keywordEquipped":
            s = f"with {q.get('keyword')} equipped"
        case "skillLevel":
            s = f"{q.get('skill')} lvl {q.get('level')}"
        case "characterLevel":
            s = f"character lvl {q.get('level')}"
        case "realm":
            s = f"in {q.get('realm')}"
        case "gameData":
            s = f"{q.get('gameDataId')} >= {q.get('data')}"
        case "itemEquipped":
            s = f"with {q.get('item')} equipped"
        case "activityType":
            s = f"doing {q.get('activity') or q.get('keywords')}"
        case "achievementPoint":
            s = (f"{q.get('value', 0):.0%} of all achievement points" if q.get("isPercentage")
                 else f"{q.get('value')}+ achievement points")
        case "traveling":
            s = "while travelling"
        case "itemAnywhere" | "itemAnywhereWithYou":
            s = f"have {q.get('item')}" + (" with you" if t == "itemAnywhereWithYou" else "")
        case "historyData":
            s = f"{q.get('data') or q.get('category')} completed {q.get('value', 1)}+ times"
        case "abilityAvailable":
            s = f"gear with the {q.get('ability')} ability"
        case "skillTypeLevel":
            s = f"{round(q.get('relativeLevel', 0) * 100)}% of the way to max level across {q.get('type')} skills"
        case "inputKeywordWithLevel":
            s = f"input for {q.get('skill')} lvl {q.get('level')}+"
        case _:
            s = f"{t} {q}"
    return neg + s
