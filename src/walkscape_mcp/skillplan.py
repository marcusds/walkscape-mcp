"""Plan the cheapest way to level a crafting skill: pure planning over recipe descriptors, no game data.

Service.plan_skill_leveling builds the Candidates (best XP loadout per recipe, material groups, gathering cost)
and turns the Segments into a readable plan; this module only decides what to craft, how many, and in what order.

The plan is greedy per level band: between one recipe unlock and the next, it repeatedly picks the recipe with the
fewest steps per XP, counting crafting steps plus the steps to gather any material the character doesn't own.
Owned materials cost nothing to use, so a plan burns through them first wherever that's cheaper per XP. What a
craft makes goes into the stock, so a later recipe can use it (bars smelted early feed smithing later).
"""

from __future__ import annotations

import math
from collections.abc import Callable
from dataclasses import dataclass, field

Cost = Callable[[str], float | None]  # item id -> steps to gather one more (None: can't get it)


@dataclass
class Candidate:
    id: str
    name: str
    level: int  # main skill level the recipe needs
    xp_per_craft: float
    steps_per_craft: float
    # one entry per material slot: the options that fill it, as (item id, amount used up per craft)
    groups: list[list[tuple[str, float]]] = field(default_factory=list)
    output: tuple[str, float] | None = None  # (item id, how many per craft), added to the stock

    @property
    def steps_per_xp(self) -> float:
        return self.steps_per_craft / self.xp_per_craft if self.xp_per_craft > 0 else math.inf


@dataclass
class Segment:
    candidate: Candidate
    crafts: int
    xp_from: float
    xp_to: float
    used: dict[str, float] = field(default_factory=dict)  # owned materials used up
    gathered: dict[str, float] = field(default_factory=dict)  # materials to farm/craft first
    gather_steps: float = 0.0

    @property
    def crafting_steps(self) -> float:
        return self.crafts * self.candidate.steps_per_craft

    @property
    def steps(self) -> float:
        return self.crafting_steps + self.gather_steps


@dataclass
class Plan:
    segments: list[Segment]
    xp_reached: float
    stuck: str | None = None  # why the plan stops short of the target

    @property
    def steps(self) -> float:
        return sum(s.steps for s in self.segments)


def owned_crafts(group: list[tuple[str, float]], stock: dict[str, float]) -> int:
    """Crafts the stock covers for one material slot, mixing its options (silver ore, then nuggets)."""
    return sum(int(stock.get(i, 0) // a) if a > 0 else 10 ** 9 for i, a in group)


def _unit_cost(group: list[tuple[str, float]], cost: Cost) -> tuple[float, str | None]:
    """Cheapest per-craft gathering cost for a slot and the option it uses."""
    best, pick = math.inf, None
    for i, a in group:
        if a <= 0:
            return 0.0, None
        c = cost(i)
        if c is not None and a * c < best:
            best, pick = a * c, i
    return best, pick


def chunk_cost(c: Candidate, n: int, stock: dict[str, float], cost: Cost) -> tuple[float, dict[str, int]]:
    """Gathering steps for n crafts beyond what the stock covers, and how many crafts' worth per material."""
    total, short = 0.0, {}
    for g in c.groups:
        missing = max(0, n - owned_crafts(g, stock))
        if not missing:
            continue
        per, pick = _unit_cost(g, cost)
        if pick is None:
            if per == 0:
                continue
            return math.inf, {}
        total += missing * per
        short[pick] = short.get(pick, 0) + missing
    return total, short


def _consume(c: Candidate, n: int, stock: dict[str, float], short: dict[str, int]) -> tuple[dict, dict]:
    """Take n crafts' materials: owned first (most-owned option first), the rest gathered as `short` says."""
    used, gathered = {}, {}
    amount = {i: a for g in c.groups for i, a in g}
    for g in c.groups:
        left = n
        for i, a in sorted(g, key=lambda x: -(stock.get(x[0], 0) // x[1] if x[1] else 0)):
            if a <= 0 or not left:
                continue
            k = min(left, int(stock.get(i, 0) // a))
            if k:
                stock[i] = stock.get(i, 0) - k * a
                used[i] = used.get(i, 0) + k * a
                left -= k
    for i, crafts in short.items():
        gathered[i] = gathered.get(i, 0) + crafts * amount[i]
    if c.output:
        stock[c.output[0]] = stock.get(c.output[0], 0) + n * c.output[1]
    return used, gathered


def plan(candidates: list[Candidate], xp_now: float, xp_target: float, level_of: Callable[[float], int],
         xp_for_level: Callable[[int], float], stock: dict[str, float], cost: Cost) -> Plan:
    """Greedy plan from xp_now to xp_target. stock is copied, not changed."""
    stock, xp, segments = dict(stock), xp_now, []
    cands = [c for c in candidates if c.xp_per_craft > 0 and c.steps_per_craft > 0]
    while xp < xp_target - 1e-9:
        lvl = level_of(xp)
        avail = sorted((c for c in cands if c.level <= lvl), key=lambda c: c.steps_per_xp)
        unlocks = [xp_for_level(c.level) for c in cands if c.level > lvl]
        stop = min([xp_target, *unlocks])
        best = None  # (steps per xp, candidate, crafts, gather steps, short)
        for c in avail:
            if best and c.steps_per_xp >= best[0]:
                break  # sorted by the no-gathering bound: nothing later can beat it
            need = max(1, math.ceil((stop - xp) / c.xp_per_craft - 1e-9))
            own = min([need, *(owned_crafts(g, stock) for g in c.groups)])
            if own:  # what the stock covers, gathering nothing
                r = c.steps_per_xp
                if not best or r < best[0]:
                    best = (r, c, own, 0.0, {})
            g_steps, short = chunk_cost(c, need, stock, cost)
            if math.isfinite(g_steps):
                r = (need * c.steps_per_craft + g_steps) / (need * c.xp_per_craft)
                if not best or r < best[0] - 1e-12:
                    best = (r, c, need, g_steps, short)
        if best is None:
            why = ("no recipe the character can do at level %d gives XP and has materials it owns or can get" % lvl
                   if avail else "no recipe unlocked at level %d" % lvl)
            return Plan(_merge(segments), xp, why)
        _, c, n, g_steps, short = best
        used, gathered = _consume(c, n, stock, short)
        gained = n * c.xp_per_craft
        segments.append(Segment(c, n, xp, xp + gained, used, gathered, g_steps))
        xp += gained
    return Plan(_merge(segments), xp)


def _merge(segments: list[Segment]) -> list[Segment]:
    out: list[Segment] = []
    for s in segments:
        if out and out[-1].candidate.id == s.candidate.id:
            p = out[-1]
            p.crafts += s.crafts
            p.xp_to = s.xp_to
            p.gather_steps += s.gather_steps
            for src, dst in ((s.used, p.used), (s.gathered, p.gathered)):
                for k, v in src.items():
                    dst[k] = dst.get(k, 0) + v
        else:
            out.append(s)
    return out
