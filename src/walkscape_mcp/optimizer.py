"""Loadout search: greedy construction + hill climbing + set-bonus seeding."""

from __future__ import annotations

import math
from dataclasses import dataclass, field

from .engine import (
    GEAR_DEPENDENT_REQS, SLOT_ORDER, Context, Evaluation, Loadout, check_all, evaluate, gear_source,
    slot_type, static_sources, steps_per_item,
)
from .gamedata import QUALITIES, QUALITY_NAMES, GameData
from .player import OwnedItem, Player
from .quality import at_least, quality_odds

OBJECTIVES = {
    "item": "minimize expected steps per drop of `target` item (activity drop tables + 'chance to find' gear)",
    "fine_item": "minimize expected steps per fine version of `target` item",
    "xp": "maximize XP per step for `target` skill (default: activity's main skill)",
    "total_xp": "maximize total XP per step across all skills the activity rewards",
    "reward_rolls": "minimize steps per loot roll (general 'more drops' objective)",
    "actions": "minimize steps per action (ignores double rewards)",
    "fine": "minimize steps per fine-material roll",
    "chests": "minimize steps per chest (any chest table)",
    "gems": "minimize steps per gem",
    "collectibles": "minimize steps per collectible drop",
    "items": "minimize steps until you have every quantity in `targets` (several items farmed at once)",
}


@dataclass
class Objective:
    kind: str
    target: str | None = None
    targets: dict[str, int] | None = None  # "items": item id -> how many
    # "quality": target is the minimum quality; the outcome is level_bonus + gear/service quality outcome
    recipe_level: int = 0
    level_bonus: float = 0.0
    fine: bool = False

    def value(self, ev: Evaluation) -> float:
        """Lower is better."""
        m = ev.metrics
        match self.kind:
            case "item":
                return steps_per_item(ev, self.target)
            case "fine_item":
                return steps_per_item(ev, self.target, fine=True)
            case "quality":
                p = at_least(quality_odds(self.recipe_level, self.level_bonus + m["quality_outcome"], self.fine),
                             self.target)
                return m["steps_per_reward_roll"] / p if p > 0 else math.inf
            case "items":  # drops roll together, so the slowest item decides when you're done
                return max(n * steps_per_item(ev, iid) for iid, n in self.targets.items())
            case "xp":
                v = m["xp_per_step"].get(self.target or m["main_skill"], 0)
                return 1 / v if v > 0 else math.inf
            case "total_xp":
                v = sum(m["xp_per_step"].values())
                return 1 / v if v > 0 else math.inf
            case "reward_rolls":
                return m["steps_per_reward_roll"]
            case "actions":
                return m["steps_per_action"]
            case "fine":
                return m["steps_per_fine_roll"]
            case "chests" | "gems" | "collectibles":
                kind = {"chests": "chestTable", "gems": "gem", "collectibles": "collectible"}[self.kind]
                per_roll = sum(d["per_roll"] for d in ev.drops if d["table"] == kind)
                return m["steps_per_reward_roll"] / per_roll if per_roll > 0 else math.inf
        raise ValueError(f"Unknown objective {self.kind!r}; choose from {list(OBJECTIVES)}")

    def describe(self, v: float) -> str:
        if math.isinf(v):
            return "not obtainable with this loadout"
        match self.kind:
            case "xp" | "total_xp":
                return f"{1 / v:.4f} XP/step"
            case "items":
                return f"{v:,.0f} steps to get all of them"
            case "quality":
                return f"{v:,.0f} steps per item of at least {QUALITY_NAMES[self.target]} quality"
            case _:
                return f"{v:,.1f} steps per unit"


@dataclass
class Candidate:
    oi: OwnedItem
    slot_type: str
    keywords: frozenset[str]
    banned: frozenset[str]


@dataclass
class SearchSpace:
    ctx: Context
    slots: list[str]
    candidates: dict[str, list[Candidate]]  # slot type -> candidates
    pets: list[tuple[str, int] | None]
    consumables: list[tuple[str, bool] | None]
    locked: dict[str, object] = field(default_factory=dict)  # slot -> OwnedItem | None | pet/consumable tuple
    pruned_count: int = 0
    extras: dict[str, list[Candidate]] = field(default_factory=dict)  # equipable but irrelevant: for complete()
    copies: dict[str, int] = field(default_factory=dict)  # item id -> copies of its best quality in the pool


def _banned_for(gd: GameData, keywords) -> frozenset[str]:
    out = set()
    for k in keywords:
        out.update((gd.keywords.get(k) or {}).get("bannedKeywords") or [])
    return frozenset(out)


def _useful_keywords(ctx: Context, items: list[OwnedItem]) -> set[str]:
    """Keywords mentioned by the activity's requirements or by gear-dependent attribute requirements."""
    kws = set()
    reqs = list(ctx.activity.get("requirements") or [])
    for oi in items:
        for a in gear_source(ctx.gd, oi).attrs:
            reqs.extend(r for r in a.get("requirements") or [] if r["type"] in GEAR_DEPENDENT_REQS)
    for r in reqs:
        q = r.get("requirement") or {}
        if r["type"] == "keywordEquipped" or r["type"] == "keywordWithLevelEquipped":
            kws.add(q.get("keyword"))
        elif r["type"] == "distinctKeywordItemsEquipped":
            kws.update(q.get("keywords") or [])
    return kws


def build_space(
    ctx: Context,
    pool: list[OwnedItem],
    pets: list[tuple[str, int] | None],
    consumables: list[tuple[str, bool] | None],
    locked: dict[str, object] | None = None,
    exclude: set[str] | None = None,
) -> SearchSpace:
    gd = ctx.gd
    exclude = exclude or set()
    slots = [s for s in SLOT_ORDER if not s.startswith("tool") or int(s[4:]) < ctx.tool_slots]

    # keep only the best quality of each item id
    best: dict[str, OwnedItem] = {}
    for oi in pool:
        if oi.id in exclude or not gd.is_gear(oi.id):
            continue
        if oi.id not in best or QUALITIES.index(oi.quality) > QUALITIES.index(best[oi.id].quality):
            best[oi.id] = oi
    copies = {i: sum(1 for oi in pool if oi == b) for i, b in best.items()}
    equipable = [oi for oi in best.values() if check_all(gd.items[oi.id].get("requirements"), ctx, None)]

    useful_kw = _useful_keywords(ctx, equipable)
    cands: dict[str, list[Candidate]] = {}
    extras: dict[str, list[Candidate]] = {}
    pruned = 0
    for oi in equipable:
        item = gd.items[oi.id]
        kws = frozenset(item.get("keywords") or [])
        could_help = any(a.get("stats") and check_all(a.get("requirements"), ctx, None) for a in gear_source(gd, oi).attrs)
        st = item["gearType"]
        cand = Candidate(oi, st, kws, _banned_for(gd, kws))
        if not could_help and not (kws & useful_kw):
            pruned += 1
            extras.setdefault(st, []).append(cand)
            continue
        cands.setdefault(st, []).append(cand)
    return SearchSpace(ctx, slots, cands, pets, consumables, dict(locked or {}), pruned, extras, copies)


def _conflicts(space: SearchSpace, lo: Loadout, slot: str, cand: Candidate) -> bool:
    st = slot_type(slot)
    for other_slot, oi in lo.slots.items():
        if other_slot == slot or not oi or slot_type(other_slot) != st:
            continue
        if oi.id == cand.oi.id and not (st == "ring" and space.copies.get(oi.id, 1) >= 2):
            return True  # the same item twice only works for two copies of a ring
        other_kws = frozenset(space.ctx.gd.items[oi.id].get("keywords") or [])
        if cand.keywords & _banned_for(space.ctx.gd, other_kws) or other_kws & cand.banned:
            return True
    return False


class Searcher:
    def __init__(self, space: SearchSpace, objective: Objective, secondary: Objective | None = None):
        self.space = space
        self.obj = objective
        self.secondary = secondary or Objective("reward_rolls")
        self.statics = static_sources(space.ctx)
        self.evals = 0
        self._cache: dict[tuple, tuple] = {}

    def key(self, lo: Loadout) -> tuple:
        return (tuple(sorted((s, i.key()) for s, i in lo.slots.items() if i)), lo.pet, lo.consumable)

    def score(self, lo: Loadout) -> tuple:
        k = self.key(lo)
        if k in self._cache:
            return self._cache[k]
        self.evals += 1
        ev = evaluate(self.space.ctx, lo, self.statics, detail=False)
        penalty = len(ev.unmet_activity_requirements) + len(ev.invalid_items)
        s = (penalty, self.obj.value(ev), self.secondary.value(ev))
        self._cache[k] = s
        return s

    def moves(self, lo: Loadout, slot: str):
        if slot in self.space.locked:
            return
        if slot == "pet":
            for p in self.space.pets:
                if p != lo.pet:
                    yield "pet", p
            return
        if slot == "consumable":
            for c in self.space.consumables:
                if c != lo.consumable:
                    yield "consumable", c
            return
        cur = lo.slots.get(slot)
        if cur is not None:
            yield slot, None
        for c in self.space.candidates.get(slot_type(slot), []):
            if cur and c.oi == cur:
                continue
            if not _conflicts(self.space, lo, slot, c):
                yield slot, c.oi

    @staticmethod
    def apply(lo: Loadout, slot: str, val) -> Loadout:
        new = lo.copy()
        if slot == "pet":
            new.pet = val
        elif slot == "consumable":
            new.consumable = val
        else:
            new.slots[slot] = val
        return new

    def all_slots(self) -> list[str]:
        extra = []
        if len(self.space.pets) > 1 and "pet" not in self.space.locked:
            extra.append("pet")
        if len(self.space.consumables) > 1 and "consumable" not in self.space.locked:
            extra.append("consumable")
        return extra + self.space.slots

    def _place_keyword(self, lo: Loadout, kw: str, need: int) -> tuple[Loadout, int]:
        """Put up to `need` distinct items carrying `kw` into free slots (empty ones first). Returns (loadout, count)."""
        gd = self.space.ctx.gd
        have = sum(kw in (gd.items[oi.id].get("keywords") or []) for _, oi in lo.items())
        free = [s for s in self.space.slots if s not in self.space.locked]
        free.sort(key=lambda s: lo.slots.get(s) is not None)
        for slot in free:
            if have >= need:
                break
            cur = lo.slots.get(slot)
            if cur and kw in (gd.items[cur.id].get("keywords") or []):
                continue
            options = [c for c in self.space.candidates.get(slot_type(slot), [])
                       if kw in c.keywords and not _conflicts(self.space, lo, slot, c) and c.oi not in lo.slots.values()]
            if options:
                best = min(options, key=lambda c: self.score(self.apply(lo, slot, c.oi)))
                lo = self.apply(lo, slot, best.oi)
                have += 1
        return lo, have

    def meet_gear_requirements(self, lo: Loadout) -> Loadout:
        """Equip enough keyword items for requirements like "3+ diving gear". Adding one piece at a time never
        clears such a requirement, so greedy and hill climbing alone can't get there."""
        for r in self.space.ctx.activity.get("requirements") or []:
            q = r.get("requirement") or {}
            if r["type"] == "distinctKeywordItemsEquipped":
                for kw in q.get("keywords") or []:
                    lo, _ = self._place_keyword(lo, kw, q.get("quantity", 1))
            elif r["type"] == "keywordEquipped":
                lo, _ = self._place_keyword(lo, q.get("keyword"), 1)
        return lo

    def greedy(self, lo: Loadout) -> Loadout:
        lo = self.meet_gear_requirements(lo)
        # fill most-constrained slots first, as the official planner does
        order = sorted(self.all_slots(), key=lambda s: len(self.space.candidates.get(slot_type(s), [])))
        for slot in order:
            best, best_s = lo, self.score(lo)
            for s, v in self.moves(lo, slot):
                cand = self.apply(lo, s, v)
                sc = self.score(cand)
                if sc < best_s:
                    best, best_s = cand, sc
            lo = best
        return lo

    def climb(self, lo: Loadout, max_rounds: int = 30) -> Loadout:
        cur_s = self.score(lo)
        for _ in range(max_rounds):
            best, best_s = None, cur_s
            for slot in self.all_slots():
                for s, v in self.moves(lo, slot):
                    cand = self.apply(lo, s, v)
                    sc = self.score(cand)
                    if sc < best_s:
                        best, best_s = cand, sc
            if best is None:
                break
            lo, cur_s = best, best_s
        return self.trim(lo)

    def trim(self, lo: Loadout) -> Loadout:
        """Drop items that contribute nothing (keeps the result readable)."""
        base = self.score(lo)
        for slot in list(lo.slots):
            if slot in self.space.locked or not lo.slots[slot]:
                continue
            cand = self.apply(lo, slot, None)
            if self.score(cand) <= base:
                lo = cand
        return lo

    def side_benefits(self, lo: Loadout) -> tuple[float, ...]:
        """Per-step rates of everything the objective might ignore; higher is better in every component."""
        ev = evaluate(self.space.ctx, lo, self.statics, detail=False)
        self.evals += 1
        m = ev.metrics
        per_step = 1 / m["steps_per_reward_roll"]
        table = lambda kind: per_step * sum(d["per_roll"] for d in ev.drops if d["table"] == kind)
        return (
            per_step,
            per_step * m["fine_chance_per_roll"],
            table("chestTable"), table("gem"), table("collectible"), table("birdNest"),
            per_step * sum(d["per_roll"] for d in ev.special),
            sum(m["xp_per_step"].values()),
            m["inventory_space"],
            m["quality_outcome"],
        )

    def fill_empty(self, lo: Loadout) -> Loadout:
        """Put side benefits (tokens, chests, collectibles, XP...) into slots the objective left empty.
        An item goes in only if it leaves the objective no worse and no side benefit lower."""
        while True:
            base_s, base_b = self.score(lo)[:2], self.side_benefits(lo)
            best, best_gain = None, 0.0
            for slot in self.space.slots:
                if slot in self.space.locked or lo.slots.get(slot):
                    continue
                for s, v in self.moves(lo, slot):
                    cand = self.apply(lo, s, v)
                    if self.score(cand)[:2] > base_s:
                        continue
                    b = self.side_benefits(cand)
                    if any(x < y - 1e-12 for x, y in zip(b, base_b)):
                        continue
                    # relative gain per component, so rare drops count as much as common ones
                    gain = sum((x - y) / y if y > 0 else (1.0 if x > y else 0.0) for x, y in zip(b, base_b))
                    if gain > best_gain + 1e-9:
                        best, best_gain = cand, gain
            if best is None:
                return lo
            lo = best

    def complete(self, lo: Loadout, prefer: Loadout | None = None) -> Loadout:
        """Fill every slot still empty, so the loadout can be equipped exactly as given: first with what's already
        worn there (prefer), then any owned piece, as long as the objective and side benefits get no worse."""
        lo = lo.copy()
        for slot in self.space.slots:
            if lo.slots.get(slot) or slot in self.space.locked:
                continue
            st = slot_type(slot)
            options = self.space.candidates.get(st, []) + self.space.extras.get(st, [])
            worn = prefer.slots.get(slot) if prefer else None
            options.sort(key=lambda c: c.oi != worn)
            base_s, base_b = self.score(lo)[:2], self.side_benefits(lo)
            for c in options:
                if _conflicts(self.space, lo, slot, c):
                    continue
                cand = self.apply(lo, slot, c.oi)
                if self.score(cand)[:2] > base_s:
                    continue
                if any(x < y - 1e-12 for x, y in zip(self.side_benefits(cand), base_b)):
                    continue
                lo = cand
                break
        return lo

    def set_seeds(self, lo: Loadout) -> list[Loadout]:
        """For each keyword set bonus, force in the pieces we own and let hill climbing sort out the rest."""
        gd = self.space.ctx.gd
        set_kws: dict[str, int] = {}
        for cands in self.space.candidates.values():
            for c in cands:
                for a in gear_source(gd, c.oi).attrs:
                    for r in a.get("requirements") or []:
                        if r["type"] == "distinctKeywordItemsEquipped":
                            for k in r["requirement"].get("keywords") or []:
                                set_kws[k] = max(set_kws.get(k, 0), r["requirement"].get("quantity", 1))
        seeds = []
        for kw in set_kws:
            seed, placed = self._place_keyword(lo.copy(), kw, len(self.space.slots))
            if placed >= 2:
                seeds.append(seed)
        return seeds

    def run(self, start: Loadout) -> Loadout:
        for slot, val in self.space.locked.items():
            start = self.apply(start, slot, val)
        results = [self.climb(self.greedy(start.copy()))]
        results.append(self.climb(start.copy()))
        best = min(results, key=self.score)
        for seed in self.set_seeds(best):
            results.append(self.climb(seed))
        return min(results, key=self.score)


def prepare(
    ctx: Context,
    objective: Objective,
    pool: list[OwnedItem],
    pets: list[tuple[str, int] | None],
    consumables: list[tuple[str, bool] | None],
    start: Loadout | None = None,
    locked: dict[str, object] | None = None,
    exclude: set[str] | None = None,
    secondary: Objective | None = None,
) -> tuple[Searcher, Loadout]:
    space = build_space(ctx, pool, pets, consumables, locked, exclude)
    searcher = Searcher(space, objective, secondary)
    start = start or Loadout(pet=pets[0] if len(pets) == 1 else None,
                             consumable=consumables[0] if len(consumables) == 1 else None)
    # the starting loadout may include items not in the pruned pool or tool slots we don't have
    start = Loadout({s: i for s, i in start.slots.items() if s in space.slots}, start.pet, start.consumable)
    return searcher, start


def quick_score(searcher: Searcher, start: Loadout) -> tuple:
    """Greedy fill only: a cheap estimate that ranks activities almost like the full search does."""
    lo = start.copy()
    for slot, val in searcher.space.locked.items():
        lo = searcher.apply(lo, slot, val)
    return searcher.score(searcher.greedy(lo))


def optimize(ctx: Context, objective: Objective, pool: list[OwnedItem], pets: list[tuple[str, int] | None],
             consumables: list[tuple[str, bool] | None], **kw) -> tuple[Loadout, Searcher]:
    searcher, start = prepare(ctx, objective, pool, pets, consumables, **kw)
    return searcher.run(start), searcher


def all_gear_pool(gd: GameData, max_quality: str = "ethereal") -> list[OwnedItem]:
    cap = QUALITIES.index(max_quality)
    out = []
    for i, item in gd.items.items():
        if not item.get("gearType"):
            continue
        qs = gd.item_qualities(i)
        q = qs[min(len(qs) - 1, cap)] if len(qs) > 1 else qs[0]
        out.append(OwnedItem(i, q))
    return out


def player_loadout(p: Player) -> Loadout:
    slots = {}
    for s, oi in p.equipped.items():
        if s.startswith("ring_"):
            slots[f"ring{int(s[5:]) - 1}"] = oi
        elif s.startswith("tool_"):
            slots[f"tool{int(s[5:]) - 1}"] = oi
        else:
            slots[s] = oi
    pet = next(((x["species"], x["level"]) for x in p.pets if x.get("equipped")), None)
    return Loadout(slots, pet)
