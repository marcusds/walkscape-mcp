"""Stat aggregation, step math, and drop rates.

Formulas are ported from the official gear planner's optimiser worker
(gear.walkscape.app) plus the wiki's mechanics pages:
  - level WE bonus: min(level - required, 20) * 1.25%
  - total WE = 1 + sum(WE), capped at the activity's maxWorkEfficiency
  - steps/completion = max(10, ceil(work / WE * (1 + steps%)) + flat steps)
  - DA/DR/NMC capped at 100%; steps/action = steps / (1 + DA); steps/reward roll = that / (1 + DR)
  - chest/gem/collectible/bird-nest tables scale their drop chance by (1 + stat)
  - fine materials: 1% * (1 + fine finding) per main-table roll
  - "chance to find X" (roll special table) is rolled per reward roll
"""

from __future__ import annotations

import json
import math
from dataclasses import dataclass, field
from functools import lru_cache

from .gamedata import GameData, strip_markup
from .player import OwnedItem, Player, tool_slots

LEVEL_WE_PER_LEVEL = 0.0125
LEVEL_WE_MAX_LEVELS = 20
BASE_FINE_CHANCE = 0.01

TABLE_MODIFIER = {"chestTable": "chestFind", "gem": "findGems", "collectible": "findCollectibles", "birdNest": "findBirdNests"}

# Requirement types whose truth depends on what is equipped (vs. only on the activity/player)
GEAR_DEPENDENT_REQS = {"distinctKeywordItemsEquipped", "keywordEquipped", "itemEquipped", "abilityAvailable", "keywordWithLevelEquipped"}

# Kept in sync with check_requirement / aggregate / compute_metrics; the drift checker compares these
# against the types that actually occur in the game data.
HANDLED_REQUIREMENT_TYPES = {
    "mainSkill", "mainSkillType", "locationHasKeywords", "realm", "skillLevel", "characterLevel", "achievementPoint",
    "totalSkillLevel", "totalSkillLevelUps", "activityType", "traveling", "gameData", "itemAnywhere",
    "itemAnywhereWithYou", "collectiblesOwned", "totalWealth", "historyData", "exploreRealm",
    "distinctKeywordItemsEquipped", "keywordEquipped", "itemEquipped", "abilityAvailable", "keywordWithLevelEquipped",
    "service", "skillTypeLevel", "inputKeywordWithLevel",
}
# Known but deliberately approximated (assumed satisfied and reported in notes)
APPROXIMATED_REQUIREMENT_TYPES = {"distinctKeywordItemInInventory"}
HANDLED_STAT_TYPES = {
    "workEfficiency", "doubleRewards", "chestFind", "fineMaterialFind", "doubleAction", "noMaterialsConsumed",
    "qualityOutcome", "bonusExperience", "rollSpecialTable", "stepsRequired", "findCollectibles", "findBirdNests",
    "findGems", "inventorySpace",
}
# countsAsKeyword is handled via abilities in equipped_state; travel distance only matters for Travelling
IGNORED_STAT_TYPES = {"countsAsKeyword", "travelingDistance"}

SLOT_ORDER = ["head", "cape", "back", "chest", "primary", "secondary", "hands", "legs", "neck", "feet",
              "ring0", "ring1", "tool0", "tool1", "tool2", "tool3", "tool4", "tool5"]


def slot_type(slot: str) -> str:
    return slot.rstrip("0123456789")


@dataclass
class Context:
    """Everything that's fixed while choosing gear: the activity, location and player state."""

    gd: GameData
    activity_id: str
    location_id: str | None
    skill_levels: dict[str, int]
    char_level: int = 99
    achievement_points: int = 10_000
    reputation: dict[str, float] = field(default_factory=dict)
    owned_ids: set[str] = field(default_factory=set)
    collectibles: list[str] = field(default_factory=list)
    coins: int = 0
    # action history (not in the save): key -> threshold the user confirmed reaching / said they haven't reached
    history_met: dict[str, float] = field(default_factory=dict)
    history_not_met: dict[str, float] = field(default_factory=dict)
    explored: set[str] = field(default_factory=set)  # regions the user said they've fully explored
    service: dict | None = None  # recipes: the crafting service used ({"id", "name", "attrs", ...})
    achievement_points_total: int | None = None  # all achievement points in the game, for "50% of points" checks
    assume_unknown_true: bool = True

    def __post_init__(self):
        gd = self.gd
        self.activity = gd.activity_like(self.activity_id)
        self.is_recipe = self.activity_id not in gd.activities
        skills = self.activity.get("relatedSkillsList") or []
        self.main_skill = skills[0] if skills else None
        self.skill_type = gd.skill_type(self.main_skill) if self.main_skill else None
        loc = gd.locations.get(self.location_id) if self.location_id else None
        self.location_keywords = set((loc or {}).get("keywords") or [])
        self.faction = (loc or {}).get("faction")
        self.sub_factions = set((loc or {}).get("subFactions") or [])
        self.activity_keywords = set(self.activity.get("keywords") or [])
        self.required_level = max(
            [r["requirement"]["level"] for r in self.activity.get("requirements") or []
             if r["type"] == "skillLevel" and r["requirement"].get("skill") == self.main_skill] or [1]
        )
        self.unverified: set[str] = set()
        self.assumed_history: set[tuple[str, float]] = set()  # history requirements the user hasn't told us about
        self._static: dict[int, tuple] = {}  # id(requirement list) -> see static_check

    @classmethod
    def for_player(cls, gd: GameData, player: Player | None, activity_id: str, location_id: str | None,
                   history_met: dict[str, float] | None = None,
                   history_not_met: dict[str, float] | None = None,
                   explored: set[str] | None = None) -> "Context":
        hist = {"history_met": history_met or {}, "history_not_met": history_not_met or {}, "explored": explored or set()}
        if player is None:
            return cls(gd, activity_id, location_id, skill_levels={s: 99 for s in gd.skills}, **hist)
        return cls(
            gd, activity_id, location_id,
            skill_levels=player.skill_levels,
            char_level=player.char_level,
            achievement_points=player.achievement_points,
            reputation=player.reputation,
            owned_ids=player.all_item_ids,
            collectibles=player.collectibles,
            coins=player.coins,
            **hist,
        )

    @property
    def tool_slots(self) -> int:
        return tool_slots(self.char_level)


@dataclass
class Equipped:
    """Equipment-dependent state used when evaluating requirements."""

    keyword_counts: dict[str, int]
    item_ids: set[str]
    abilities: set[str]
    gear: list[tuple[str, dict]]  # (item_id, item) for keywordWithLevelEquipped


def history_key(category: str, data: str | None = None) -> str:
    return f"{category}:{data}" if data else category


def check_requirement(r: dict, ctx: Context, eq: Equipped | None) -> bool:
    """Evaluate one requirement. eq=None means 'gear unknown': gear-dependent checks pass (optimistic)."""
    t, q = r["type"], r.get("requirement") or {}
    if eq is None and t in GEAR_DEPENDENT_REQS:
        return True
    match t:
        case "mainSkill":
            ok = ctx.main_skill == q.get("skill")
        case "mainSkillType":
            ok = ctx.skill_type == q.get("type")
        case "locationHasKeywords":
            ok = all(k in ctx.location_keywords for k in q.get("keywords") or [])
        case "realm":
            ok = q.get("realm") in ({ctx.faction} | ctx.sub_factions)
        case "skillLevel":
            ok = ctx.skill_levels.get(q.get("skill"), 0) >= q.get("level", 0)
        case "characterLevel":
            ok = ctx.char_level >= q.get("level", 0)
        case "achievementPoint":
            if q.get("isPercentage"):
                if ctx.achievement_points_total:
                    ok = ctx.achievement_points >= q.get("value", 0) * ctx.achievement_points_total - 1e-9
                else:
                    ctx.unverified.add("achievementPoint (percentage; achievement list unavailable)")
                    ok = ctx.assume_unknown_true
            else:
                ok = ctx.achievement_points >= q.get("value", 0)
        case "totalSkillLevel":
            ok = sum(ctx.skill_levels.values()) >= q.get("levels", 0)
        case "totalSkillLevelUps":
            ok = sum(v - 1 for v in ctx.skill_levels.values()) >= q.get("levels", 0)
        case "activityType":
            ok = (not q.get("activity") or ctx.activity_id == q["activity"]) and all(
                k in ctx.activity_keywords for k in q.get("keywords") or [])
        case "traveling":
            ok = ctx.activity_id == "travelling"
        case "gameData":
            faction = ctx.gd.reputation_key_to_faction.get(q.get("gameDataId"), q.get("gameDataId"))
            need = float(json.loads(q.get("data") or "{}").get("double", 0))
            ok = ctx.reputation.get(faction, 0) >= need
        case "itemAnywhere" | "itemAnywhereWithYou":
            ok = q.get("item") in ctx.owned_ids
        case "collectiblesOwned":
            ok = len(ctx.collectibles) >= q.get("amount", 0)
        case "totalWealth":
            ok = ctx.coins >= q.get("amount", 0)
        case "historyData":
            # not in the save export; use what the user told us, else assume done and report it
            key = history_key(q.get("category", ""), q.get("data"))
            need = q.get("value", 0)
            if ctx.history_met.get(key, -math.inf) >= need:
                ok = True
            elif ctx.history_not_met.get(key, math.inf) <= need:
                ok = False
            else:
                ctx.assumed_history.add((key, need))
                ok = ctx.assume_unknown_true
        case "exploreRealm":
            # the save has no exploration data: use what the user told us, else having reputation there
            ok = q.get("realm") in ctx.explored or q.get("realm") in ctx.reputation
        case "distinctKeywordItemsEquipped":
            ok = all(eq.keyword_counts.get(k, 0) >= q.get("quantity", 1) for k in q.get("keywords") or [])
        case "keywordEquipped":
            ok = eq.keyword_counts.get(q.get("keyword"), 0) > 0
        case "itemEquipped":
            ok = q.get("item") in eq.item_ids
        case "abilityAvailable":
            ok = q.get("ability") in eq.abilities
        case "keywordWithLevelEquipped":
            ok = any(
                q.get("keyword") in (it.get("keywords") or []) and any(
                    rr["type"] == "skillLevel" and rr["requirement"].get("skill") == q.get("skill")
                    and rr["requirement"].get("level", 0) >= q.get("level", 0) for rr in it.get("requirements") or [])
                for _, it in eq.gear)
        case "service":
            ok = False
        case "skillTypeLevel":
            ok = skill_type_progress(ctx.gd, ctx.skill_levels, q.get("type")) >= q.get("relativeLevel", 0) - 1e-9
        case "inputKeywordWithLevel":
            # only meaningful for a specific input item; see input_fits
            ok = True
        case _:
            # skillTypeLevel, inputKeywordWithLevel, distinctKeywordItemInInventory, ... not modelled
            ctx.unverified.add(t)
            ok = ctx.assume_unknown_true
    return not ok if r.get("opposite") else ok


def skill_type_progress(gd: GameData, skill_levels: dict[str, int], skill_type: str | None) -> float:
    """Share of the way to max level across a skill type (gathering, artisan, utility): levels gained above 1
    over 98 per skill. The wiki shows e.g. "55% towards maximum Gathering level [270]" (0.55 × 98 × 5 skills)."""
    skills = [s for s in gd.skills if gd.skill_type(s) == skill_type]
    if not skills:
        return 0.0
    return sum(max(0, skill_levels.get(s, 1) - 1) for s in skills) / (98 * len(skills))


def input_fits(gd: GameData, item_id: str, req: dict) -> bool:
    """inputKeywordWithLevel: the input item (e.g. arrows) must be for at least this level of the skill."""
    q = req.get("requirement") or {}
    lvl = max([r["requirement"].get("level", 0) for r in gd.items.get(item_id, {}).get("requirements") or []
               if r["type"] == "skillLevel" and r["requirement"].get("skill") == q.get("skill")] or [0])
    return lvl >= q.get("level", 0)


def check_all(reqs, ctx: Context, eq: Equipped | None) -> bool:
    return all(check_requirement(r, ctx, eq) for r in reqs or [])


def static_check(reqs, ctx: Context) -> tuple[bool, list, set, set, object]:
    """Split a requirement list into the part that only depends on the context (checked once and cached)
    and the gear-dependent part. Returns (static ok, gear requirements, assumed history, unverified types);
    the assumptions are returned rather than recorded so callers only report them when they mattered.
    The cache entry holds a reference to `reqs`, so its id can't be reused while the context lives."""
    c = ctx._static.get(id(reqs))
    if c is None or c[4] is not reqs:
        hist, unver = ctx.assumed_history, ctx.unverified
        ctx.assumed_history, ctx.unverified = set(), set()
        ok = all(check_requirement(r, ctx, None) for r in reqs or [] if r["type"] not in GEAR_DEPENDENT_REQS)
        gear = [r for r in reqs or [] if r["type"] in GEAR_DEPENDENT_REQS]
        c = ctx._static[id(reqs)] = (ok, gear, ctx.assumed_history, ctx.unverified, reqs)
        ctx.assumed_history, ctx.unverified = hist, unver
    return c


# ---------- attribute sources ----------

@dataclass
class Source:
    """A thing that contributes attributes: an item, pet, consumable, collectible or level bonus."""

    kind: str  # gear | pet | consumable | collectible | level | service
    id: str
    label: str
    attrs: list[dict]
    keywords: tuple[str, ...] = ()
    abilities: tuple[str, ...] = ()
    item: dict | None = None


@lru_cache(maxsize=8192)
def _gear_source_cached(gd_id: int, item_id: str, quality: str) -> Source:
    gd = _GD_REGISTRY[gd_id]
    item = gd.items[item_id]
    return Source("gear", item_id, f"{item['name']} ({quality})", gd.item_attrs(item_id, quality),
                  tuple(item.get("keywords") or ()), item=item)


_GD_REGISTRY: dict[int, GameData] = {}


def gear_source(gd: GameData, oi: OwnedItem) -> Source:
    _GD_REGISTRY[id(gd)] = gd
    return _gear_source_cached(id(gd), oi.id, oi.quality)


def pet_source(gd: GameData, species: str, level: int) -> Source:
    _GD_REGISTRY[id(gd)] = gd
    return _pet_source_cached(id(gd), species, level)


@lru_cache(maxsize=1024)
def _pet_source_cached(gd_id: int, species: str, level: int) -> Source:
    gd = _GD_REGISTRY[gd_id]
    pet = gd.pets[species]
    return Source("pet", species, f"{pet['name']} pet (lvl {level})", gd.pet_attrs(species, level),
                  abilities=tuple(gd.pet_abilities(species, level)))


def consumable_source(gd: GameData, item_id: str, fine: bool) -> Source:
    return Source("consumable", item_id, gd.name(item_id) + (" (fine)" if fine else ""), gd.consumable_attrs(item_id, fine))


def static_sources(ctx: Context) -> list[Source]:
    gd = ctx.gd
    out = []
    lvl = ctx.skill_levels.get(ctx.main_skill, 1) if ctx.main_skill else 1
    over = max(lvl - ctx.required_level, 0)
    we = min(over, LEVEL_WE_MAX_LEVELS) * LEVEL_WE_PER_LEVEL
    if ctx.activity_id == "travelling":
        we = over * 0.005
    if we:
        out.append(Source("level", "level_bonus", f"{ctx.main_skill} lvl {lvl} vs req {ctx.required_level} ({over} over)",
                          [{"statText": "Work efficiency", "requirements": [], "stats": [
                              {"type": "workEfficiency", "isPercent": True, "value": we, "name": "Work efficiency"}]}]))
    for c in ctx.collectibles:
        if c in gd.items and gd.items[c].get("itemAttrs"):
            out.append(Source("collectible", c, gd.name(c), gd.item_attrs(c)))
    if ctx.service and ctx.service.get("attrs"):
        out.append(Source("service", ctx.service["id"], ctx.service["name"], ctx.service["attrs"]))
    return out


# ---------- loadout ----------

@dataclass
class Loadout:
    slots: dict[str, OwnedItem | None] = field(default_factory=dict)
    pet: tuple[str, int] | None = None  # (species, level)
    consumable: tuple[str, bool] | None = None  # (item_id, fine)

    def copy(self) -> "Loadout":
        return Loadout(dict(self.slots), self.pet, self.consumable)

    def items(self) -> list[tuple[str, OwnedItem]]:
        return [(s, i) for s, i in self.slots.items() if i]


@dataclass
class Evaluation:
    stats: dict[str, float]
    metrics: dict
    drops: list[dict]
    special: list[dict]
    active: list[tuple[str, str]]  # (source label, attr description)
    inactive: list[tuple[str, str]]
    unmet_activity_requirements: list[str]
    invalid_items: list[str]
    abilities: set[str]

    @property
    def valid(self) -> bool:
        return not self.unmet_activity_requirements and not self.invalid_items


def equipped_state(gd: GameData, sources: list[Source]) -> Equipped:
    counts: dict[str, int] = {}
    ids, abilities, gear = set(), set(), []
    for s in sources:
        for k in set(s.keywords):
            counts[k] = counts.get(k, 0) + 1
        if s.kind == "gear":
            ids.add(s.id)
            gear.append((s.id, s.item))
        abilities.update(s.abilities)
    # countsAsKeyword abilities (e.g. "Clever Climber") make the ability count as an equipped keyword
    for a in abilities:
        for attr in (gd.abilities.get(a) or {}).get("attributes") or []:
            for st in attr.get("stats") or []:
                if st.get("type") == "countsAsKeyword" and st.get("keyword"):
                    counts[st["keyword"]] = counts.get(st["keyword"], 0) + 1
    return Equipped(counts, ids, abilities, gear)


def aggregate(attrs: list[dict]) -> dict[str, float]:
    stats: dict[str, float] = {}
    for a in attrs:
        for s in a.get("stats") or []:
            t = s["type"]
            if t == "rollSpecialTable":
                continue
            key = t + (":" + s["skill"] if s.get("skill") else "") + ("" if s.get("isPercent") else ":flat")
            stats[key] = stats.get(key, 0.0) + (s.get("value") or 0.0)
    return stats


def compute_metrics(stats: dict[str, float], activity: dict, main_skill: str | None) -> dict:
    g = lambda k: stats.get(k, 0.0)
    max_we = activity.get("maxWorkEfficiency") or 1
    work = activity.get("workRequired") or 1
    raw_we = 1 + g("workEfficiency")
    we = max(min(raw_we, max_we), 0.01)
    steps_flat, steps_pct = g("stepsRequired:flat"), 1 + g("stepsRequired")
    completion = max(10, math.ceil(work / we * steps_pct) + steps_flat)
    da, dr, nmc = min(1, g("doubleAction")), min(1, g("doubleRewards")), min(1, g("noMaterialsConsumed"))
    per_action = completion / (1 + da)
    per_roll = per_action / (1 + dr)
    fine_chance = BASE_FINE_CHANCE * (1 + g("fineMaterialFind"))
    xp_pct, xp_flat = g("bonusExperience"), g("bonusExperience:flat")
    xp = {}
    for skill, base in (activity.get("xpRewardsMap") or {}).items():
        xp[skill] = (1 + xp_pct) * (base + xp_flat + g(f"bonusExperience:{skill}:flat"))
    min_steps = max(10, math.ceil(work / max_we * steps_pct) + steps_flat)
    return {
        "base_steps": work,
        "max_work_efficiency": max_we,
        "work_efficiency": round(we, 4),
        "uncapped_work_efficiency": round(raw_we, 4),
        "work_efficiency_wasted": round(max(0, raw_we - max_we), 4),
        "steps_per_completion": completion,
        "min_possible_steps_per_completion": min_steps,
        "double_action": round(da, 4),
        "double_rewards": round(dr, 4),
        "no_materials_consumed": round(nmc, 4),
        "steps_per_action": round(per_action, 3),
        "steps_per_reward_roll": round(per_roll, 3),
        "fine_chance_per_roll": round(fine_chance, 5),
        "steps_per_fine_roll": round(per_roll / fine_chance, 1),
        "chest_find": round(g("chestFind"), 4),
        "find_gems": round(g("findGems"), 4),
        "find_collectibles": round(g("findCollectibles"), 4),
        "find_bird_nests": round(g("findBirdNests"), 4),
        "quality_outcome": g("qualityOutcome:flat"),
        "inventory_space": g("inventorySpace:flat"),
        "xp_per_action": {k: round(v, 2) for k, v in xp.items()},
        "xp_per_step": {k: round(v / per_action, 4) for k, v in xp.items()},
        "main_skill": main_skill,
    }


def row_weight(row: dict, skill_levels: dict[str, int] | None) -> float:
    """A loot row's weight at the character's levels. Rows with level bonuses (fish, mostly) are absent below their
    level requirement and, with linear scaling, grow from levelMinScaling to full weight at levelMaxScaling:
    weight × max(minWeightScale, min(1, (level - min + 1) / (max - min + 1))), rounded to 0.1 while scaling.
    Fitted to the wiki's per-level fishing tables (exact for Sea fishing (Rod) and Lake fishing except at level 10,
    where the wiki's own table disagrees with its other levels). Without a character (skill_levels None) rows get full weight."""
    w = row.get("rowWeight", 0)
    if skill_levels is None:
        return w
    for b in row.get("requirementsBonuses") or []:
        lvl = skill_levels.get(b.get("relatedSkill"), 1)
        if lvl < b.get("levelRequirement", 0):
            return 0.0
        lo, hi = b.get("levelMinScaling", 0), b.get("levelMaxScaling", 0)
        if row.get("linearWeightScaling") and hi >= lo:
            scale = max(row.get("minWeightScale", 0), min(1.0, (lvl - lo + 1) / (hi - lo + 1)))
            if scale < 1:  # partly scaled weights are rounded to 0.1; tiny full weights (trinkets) are not
                w = round(w * scale, 1)
    return w


def table_drops(gd: GameData, activity: dict, stats: dict[str, float],
                skill_levels: dict[str, int] | None = None) -> list[dict]:
    """Expected drops per reward roll for each item in the activity's loot tables."""
    out = []
    fine_chance = BASE_FINE_CHANCE * (1 + stats.get("fineMaterialFind", 0.0))
    for grp in activity.get("tables") or []:
        types = grp.get("type") or []
        mod_stat = next((TABLE_MODIFIER[t] for t in types if t in TABLE_MODIFIER), None)
        mult = 1 + stats.get(mod_stat, 0.0) if mod_stat else 1.0
        rolls = grp.get("rollAmount", 1)
        kind = "main" if grp.get("isPrimary") else (types[0] if types else "secondary")
        for tid in grp.get("tables") or []:
            t = gd.loot_tables.get(tid)
            if not t:
                continue
            rows = [r for r in t["tableRows"]]
            weights = [row_weight(r, skill_levels) for r in rows]
            total_w = sum(weights) or 1
            hit = min(1.0, (1 - t.get("noDropChance", 0)) * mult)
            for r, w in zip(rows, weights, strict=True):
                item_id = r.get("rowItemID") or ("coins" if r.get("isMoney") else None)
                if not item_id or not w:
                    continue
                p = hit * w / total_w
                qty = (r.get("rowMinimumAmount", 1) + r.get("rowMaximumAmount", 1)) / 2
                can_fine = grp.get("isPrimary") and (gd.items.get(item_id) or {}).get("canBeFine")
                out.append({
                    "item": item_id, "name": gd.name(item_id) if item_id != "coins" else "Coins", "table": kind,
                    "chance_per_roll": p * rolls, "qty": qty,
                    "per_roll": p * rolls * qty,
                    "fine_per_roll": p * rolls * qty * fine_chance if can_fine else 0.0,
                    "modifier": mod_stat,
                    # extra XP for catching this row (e.g. fish above the activity's base)
                    "xp_bonus": {b["relatedSkill"]: b["xpBonus"] for b in r.get("requirementsBonuses") or []
                                 if b.get("xpBonus")},
                })
    return out


def special_drops(gd: GameData, attrs: list[dict]) -> list[dict]:
    """'Chance to find X' attributes: each is a chance per reward roll to roll a special table."""
    by_table: dict[str, dict] = {}
    for a in attrs:
        for s in a.get("stats") or []:
            if s["type"] != "rollSpecialTable":
                continue
            for grp in a.get("tables") or []:
                for tid in grp.get("tables") or []:
                    e = by_table.setdefault(tid, {"chance": 0.0, "label": strip_markup(a.get("customText") or a.get("text"))})
                    e["chance"] += s.get("value") or 0.0
    out = []
    for tid, e in by_table.items():
        t = gd.loot_tables.get(tid)
        if not t:
            continue
        rows = t["tableRows"]
        total_w = sum(r.get("rowWeight", 0) for r in rows) or 1
        chance = min(1.0, e["chance"])
        for r in rows:
            item_id = r.get("rowItemID") or "coins"
            qty = (r.get("rowMinimumAmount", 1) + r.get("rowMaximumAmount", 1)) / 2
            p = chance * (1 - t.get("noDropChance", 0)) * r.get("rowWeight", 0) / total_w
            out.append({"item": item_id, "name": gd.name(item_id), "table": f"special:{tid}",
                        "chance_per_roll": p, "qty": qty, "per_roll": p * qty, "fine_per_roll": 0.0, "modifier": "rollSpecialTable"})
    return out


def evaluate(ctx: Context, lo: Loadout, statics: list[Source] | None = None, detail: bool = True) -> Evaluation:
    gd = ctx.gd
    ctx_sources = statics if statics is not None else static_sources(ctx)
    sources: list[Source] = [gear_source(gd, i) for _, i in lo.items()]
    if lo.pet:
        sources.append(pet_source(gd, *lo.pet))
    if lo.consumable:
        sources.append(consumable_source(gd, *lo.consumable))
    eq = equipped_state(gd, sources)

    active_attrs, active, inactive = [], [], []
    for src in ctx_sources + sources:
        for a in src.attrs:
            if not a.get("stats"):
                continue
            ok, gear_reqs, hist, unver, _ = static_check(a.get("requirements"), ctx)
            if ok and all(check_requirement(r, ctx, eq) for r in gear_reqs):
                ctx.assumed_history |= hist
                ctx.unverified |= unver
                active_attrs.append(a)
                if detail:
                    active.append((src.label, gd.describe_attr(a)))
            elif detail:
                inactive.append((src.label, gd.describe_attr(a)))

    stats = aggregate(active_attrs)
    metrics = compute_metrics(stats, ctx.activity, ctx.main_skill)
    drops = table_drops(gd, ctx.activity, stats, ctx.skill_levels)
    # Pet eggs roll once per completion and aren't modified by any attribute (wiki: "Not modifiable by any special
    # attributes"), so double action/rewards don't add egg rolls. Rescale to the per-reward-roll basis used below.
    per_completion = metrics["steps_per_reward_roll"] / metrics["steps_per_completion"]
    for d in drops:
        if d["table"] == "petEgg":
            for k in ("chance_per_roll", "per_roll", "fine_per_roll"):
                d[k] *= per_completion
    # per-row XP bonuses (fish) add to the activity's XP per completion, scaled like other XP
    for d in drops:
        for skill, bonus in d["xp_bonus"].items():
            extra = d["chance_per_roll"] * (1 + metrics["double_rewards"]) * bonus * (1 + stats.get("bonusExperience", 0))
            metrics["xp_per_step"][skill] = metrics["xp_per_step"].get(skill, 0) + extra / metrics["steps_per_action"]
    special = special_drops(gd, active_attrs)

    # recipes' service requirements are assumed met (you choose where to craft)
    unmet = [r for r in ctx.activity.get("requirements") or []
             if r["type"] != "service" and not check_requirement(r, ctx, eq)]
    invalid = []
    for s in sources:
        if s.kind == "gear":
            ok, _, hist, unver, _ = static_check((s.item or {}).get("requirements"), ctx)
            ctx.assumed_history |= hist
            ctx.unverified |= unver
            if not ok:
                invalid.append(s.label)
    from .gamedata import describe_requirement
    return Evaluation(stats, metrics, drops, special, active, inactive,
                      [describe_requirement(r) for r in unmet], invalid, eq.abilities)


def steps_per_item(ev: Evaluation, item_id: str, fine: bool = False) -> float:
    """Expected steps to obtain one of item_id (inf if it can't drop)."""
    per_roll = sum((d["fine_per_roll"] if fine else d["per_roll"]) for d in ev.drops + ev.special if d["item"] == item_id)
    if per_roll <= 0:
        return math.inf
    return ev.metrics["steps_per_reward_roll"] / per_roll


def drop_report(ev: Evaluation, limit: int = 40) -> list[dict]:
    per_roll_steps = ev.metrics["steps_per_reward_roll"]
    agg: dict[str, dict] = {}
    for d in ev.drops + ev.special:
        e = agg.setdefault(d["item"], {"item": d["item"], "name": d["name"], "per_roll": 0.0, "fine_per_roll": 0.0, "sources": set()})
        e["per_roll"] += d["per_roll"]
        e["fine_per_roll"] += d["fine_per_roll"]
        e["sources"].add(d["table"])
    out = []
    for e in agg.values():
        if e["per_roll"] <= 0:
            continue
        row = {
            "item": e["name"], "id": e["item"],
            "steps_per_drop": round(per_roll_steps / e["per_roll"], 1),
            "per_1000_steps": round(1000 * e["per_roll"] / per_roll_steps, 4),
            "sources": sorted(e["sources"]),
        }
        if e["fine_per_roll"] > 0:
            row["steps_per_fine"] = round(per_roll_steps / e["fine_per_roll"], 1)
        out.append(row)
    out.sort(key=lambda r: r["steps_per_drop"])
    return out[:limit]
