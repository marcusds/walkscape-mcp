"""Order the remaining achievements by achievement points per step, crediting what each grind also advances.

Each achievement's parsed goals (wikidata.py) get a step estimate from the existing tools: steps per action or per
drop with the best owned loadout, plan_recipe / farming for items, the best XP rate for levels. Each estimate keeps
the activity (or recipe) it runs and for how many steps. The plan then repeatedly takes the achievement with the
most points per step, where a grind's value also counts the share of other achievements it advances (the same
activity's action counts and drops, crafts, and XP toward levels they need). Once taken, everything it produced
is credited: other goals shrink, XP raises levels, walking goals (total steps, character level) complete as steps
add up. Levels reached are shared. This is a heuristic order, not a proven optimum.
"""

from __future__ import annotations

import math
import re
from contextlib import contextmanager
from dataclasses import replace

from .engine import GEAR_DEPENDENT_REQS, check_all, drop_report
from .gamedata import norm
from .optimizer import Objective
from .player import CHAR_STEPS, SKILL_XP, skill_level

CANDIDATES = 8  # activities/recipes tried per goal (the most likely ones first)
PASSIVE = ("total_steps", "character_level")
LINEAR = ("actions", "actions_keyword", "gain_item", "gain_keyword", "craft_item", "craft_keyword", "craft_skill",
          "have_item", "stack", "wealth", "hatch")  # goals whose cost scales with how many are left


class NotEstimated(Exception):
    pass


class AchievementPlanner:
    def __init__(self, svc, pet: str | None = "auto", rare_egg_chance: float | None = None):
        self.s, self.gd, self.p, self.pet = svc, svc.gd, svc._player, pet
        self.rare_egg_chance = rare_egg_chance
        self._real_hops: dict = {}
        self._raised: dict = {}
        self._seen_base = self._seen_real = 0.0
        self._metrics: dict = {}
        self._xp_rates: dict = {}
        self._stack: dict = {}
        self._drops: dict = {}
        self._dists: dict = {}
        self._travel_factor: float | None = None
        self.start = svc._near(None)
        self.near = self.gd.locations[self.start]["name"] if self.start else None

    # ---------- travel ----------

    def dist(self, a: str | None, b: str | None) -> float:
        """Base route distance between locations (0 when either is unknown)."""
        if not a or not b or a == b:
            return 0.0
        if a not in self._dists:
            self._dists[a] = self.s._base_distances(a)[0]
        return self._dists[a].get(b, math.inf)

    def travel_factor(self) -> float:
        """Steps per base step with the character's best travel loadout: from the routes planned so far, or one
        representative route before any."""
        if self._seen_base:
            return self._seen_real / self._seen_base
        if self._travel_factor is None:
            self._travel_factor = 1.0
            if self.start:
                d = self.s._base_distances(self.start)[0]
                dest = min((x for x in d if d[x] > 0), key=lambda x: abs(d[x] - 1500), default=None)
                if dest:
                    try:
                        r = self.s.plan_route(self.gd.locations[dest]["name"], start=self.near)
                        self._travel_factor = r["single_loadout"]["steps"] / r["base_steps"]
                    except Exception:
                        pass
        return self._travel_factor

    def route(self, groups: list[list[str]], start: str | None) -> tuple[float, str | None, list[tuple[str, str]]]:
        """Estimated travel steps to visit each segment's stops (segments nearest-first, a segment's stops in
        order), where it ends, and the hops."""
        cur, total, hops, groups = start, 0.0, [], [g for g in groups if g]
        while groups:
            i = min(range(len(groups)), key=lambda k: self.dist(cur, groups[k][0]))
            for loc in groups.pop(i):
                total += self.dist(cur, loc)
                if cur and loc != cur:
                    hops.append((cur, loc))
                cur = loc
        return total * self.travel_factor(), cur, hops

    def real_travel(self, hops: list[tuple[str, str]]) -> float:
        """Travel steps for the hops with plan_route's best single travel loadout per trip; also sharpens the
        factor used to estimate other trips."""
        total = 0.0
        for a, b in hops:
            if (a, b) not in self._real_hops:
                base = self.dist(a, b)
                try:
                    r = self.s.plan_route(self.gd.locations[b]["name"], start=self.gd.locations[a]["name"])
                    single = r["single_loadout"]  # a message when no one loadout meets every leg's terrain
                    real = float(single["steps"] if isinstance(single, dict) else r["steps_swapping_gear_each_leg"])
                except Exception:
                    real = base * self.travel_factor()
                self._real_hops[(a, b)] = real
                if math.isfinite(base) and base > 0:
                    self._seen_base += base
                    self._seen_real += real
            total += self._real_hops[(a, b)]
        return total

    # ---------- shared estimates ----------

    # A "level" key is a skill ("hunting": level) or a reputation ("rep:jarvonia": amount); its state is the
    # skill's XP or the reputation.

    def _now(self, key: str) -> float:
        if key.startswith("rep:"):
            return float(self.p.reputation.get(key[4:], 0))
        return float(self.p.skill_xp.get(key, 0))

    @staticmethod
    def _need(key: str, v: float) -> float:
        """XP (or reputation) a level target means."""
        return float(v) if key.startswith("rep:") else float(SKILL_XP[int(v) - 1])

    @contextmanager
    def _at_levels(self, levels: dict[str, int]):
        """Evaluate as if the character had these skill levels (for activities it must level into): drops that
        need a level (e.g. fish) and the level work-efficiency bonus follow the raised levels. Supply estimates get
        their own cache while raised."""
        raise_to = {k: v for k, v in levels.items() if self._need(k, v) > self._now(k)}
        if not raise_to:
            yield
            return
        s, saved = self.s, (self.s._player, self.s._supply_cache, self.s._supplying)
        key = tuple(sorted(raise_to.items()))
        if key not in self._raised:  # one character and supply cache per set of raised levels, reused
            xp = {**self.p.skill_xp, **{k: self._need(k, v) for k, v in raise_to.items() if not k.startswith("rep:")}}
            rep = {**self.p.reputation, **{k[4:]: v for k, v in raise_to.items() if k.startswith("rep:")}}
            self._raised[key] = (replace(self.p, skill_xp=xp, reputation=rep), {})
        player, cache = self._raised[key]
        s._player, s._supply_cache, s._supplying = player, cache, set()
        try:
            yield
        finally:
            s._player, s._supply_cache, s._supplying = saved

    def _eval(self, aid: str, obj: Objective, levels: dict[str, int] | None = None):
        levels = levels or {}
        key = (aid, obj.kind, obj.target, tuple(sorted(levels.items())))
        if key not in self._metrics:
            with self._at_levels(levels):
                self._metrics[key] = self.s._best_loadout(aid, obj, pet=self.pet)
        return self._metrics[key]

    def _aseg(self, aid: str, obj: Objective, levels: dict[str, int] | None = None) -> dict:
        """A segment spent on an activity: its loadout's evaluation, where it's done, and the gear it still
        needs ("gear": {"steps", "segs", "how"}, or None if that gear can't be got)."""
        ctx, lo, ev = self._eval(aid, obj, levels)
        return {"aid": aid, "ev": ev, "locs": [ctx.location_id] if ctx.location_id else [],
                "gear": self._gear_gap(ctx, lo)}

    def _gear_gap(self, ctx, lo) -> dict | None:
        """Getting gear an activity requires that the best owned loadout lacks (a fishing spear, 3 diving gear):
        the cheapest items with the keyword. None if something required can't be got."""
        from .engine import check_requirement, equipped_state, gear_source, pet_source
        gd, out, hows = self.gd, {"steps": 0.0, "segs": [], "how": ""}, []
        busy = getattr(self, "_gear_busy", set())
        eq = equipped_state(gd, [gear_source(gd, i) for _, i in lo.items()]
                            + ([pet_source(gd, *lo.pet)] if lo.pet else []))
        for r in ctx.activity.get("requirements") or []:
            if r["type"] not in GEAR_DEPENDENT_REQS or check_requirement(r, ctx, eq):
                continue
            q = r.get("requirement") or {}
            if r["type"] == "keywordEquipped":
                kws, need = [q.get("keyword")], 1
            elif r["type"] == "distinctKeywordItemsEquipped":
                kws, need = q.get("keywords") or [], q.get("quantity", 1)
            elif r["type"] == "itemEquipped" and q.get("item") in gd.items:
                kws, need = None, 1
            else:
                return None
            ids = [q["item"]] if kws is None else [i for i, it in gd.items.items()
                                                   if set(kws) & set(it.get("keywords") or [])]
            owned = [i for i in ids if i in self.p.all_item_ids]
            short = need - len(owned)
            if short <= 0:  # owned but unusable (e.g. its own level requirement)
                return None
            costs = []
            for i in ids:
                if i in owned or i in busy:
                    continue
                self._gear_busy = busy | {i}
                try:
                    c = self.supply(i, 1)
                finally:
                    self._gear_busy = busy
                if c and not c["levels"]:
                    costs.append((c["steps"], gd.name(i), c))
            if len(costs) < short:
                return None
            for steps, name, c in sorted(costs)[:short]:
                out["steps"] += steps
                out["segs"] += c["segs"]
                hows.append(f"get {name} ({c['how']})")
        out["how"] = "; ".join(hows)
        return out

    def _loc_id(self, name: str | None) -> str | None:
        if not name:
            return None
        try:
            return self.gd.resolve(name.split(", ")[-1], "location")
        except KeyError:
            return None

    def _rseg(self, rid: str, crafts_per_step: float, plan: dict | None = None) -> dict:
        """A segment spent crafting: materials gathered where plan_recipe says, then crafted at its service."""
        locs = []
        for m in (plan or {}).get("materials") or []:
            if lid := self._loc_id((m.get("gather") or {}).get("location")):
                locs.append(lid)
        if lid := self._loc_id((plan or {}).get("craft_at")):
            locs.append(lid)
        return {"rid": rid, "crafts_per_step": crafts_per_step, "locs": locs}

    def _drop_rates(self, ev) -> dict[str, tuple[float, float]]:
        """item id -> (per step, fine per step) for an evaluated loadout."""
        key = id(ev)
        if key not in self._drops:
            self._drops[key] = (ev, {d["id"]: (d["per_1000_steps"] / 1000,
                                               1 / d["steps_per_fine"] if "steps_per_fine" in d else 0.0)
                                     for d in drop_report(ev, 10_000)})
        return self._drops[key][1]

    def _sell_value(self, iid: str, fine: bool = False) -> float:
        if iid == "coins":
            return 1.0
        v = (self.gd.items.get(iid) or {}).get("itemValue") or {}
        if v.get("currency") != "money":
            return 0.0
        return float((v.get("value") or {}).get("fine" if fine else "common") or 0)

    def _coins_per_step(self, ev) -> float:
        """Coins an activity makes per step: coin drops plus selling everything else it drops."""
        return sum((rate - fine) * self._sell_value(i) + fine * self._sell_value(i, True)
                   for i, (rate, fine) in self._drop_rates(ev).items())

    def supply(self, iid: str, count: int) -> dict | None:
        """Cheapest way to get `count` of an item, levelling into a recipe or activity if needed:
        {"steps", "how", "levels"}."""
        s, gd = self.s, self.gd
        if iid == "adventurers_guild_token" and (farm := self.token_farm()):
            steps = count * farm["steps_per_token"]
            return {"steps": steps, "how": farm["how"], "levels": {}, "segs": [(farm["seg"], steps)]}
        if c := s._supply_steps(iid, count, self.near, self.pet):
            segs, steps, how = [], c["steps"], c["how"]
            if farm := c.get("farm"):  # farmed: the activity also drops other things worth crediting
                aid = gd.resolve(farm["activity"], "activity")
                seg = self._aseg(aid, Objective("item", iid))
                if seg["gear"] is None:
                    return None
                segs = [(seg, steps)] + seg["gear"]["segs"]
                if seg["gear"]["steps"]:
                    steps, how = steps + seg["gear"]["steps"], f"{how} [{seg['gear']['how']}]"
            return {"steps": steps, "how": how, "levels": {}, "segs": segs}
        rows = []
        for src in gd.item_sources.get(iid, []):
            if src["kind"] not in ("recipe", "activity"):
                continue
            levels, blockers = self.prereqs(src["id"])
            if blockers:
                continue
            with self._at_levels(levels):
                if src["kind"] == "recipe":
                    plan = s.plan_recipe(src["id"], count, self.near, self.pet)
                    steps = plan["total_steps"]
                    if "total_steps_leaves_out" in plan:  # a material it must level into (e.g. feathers)
                        more = None if src["id"] in self._busy_recipes else self._with_busy(
                            src["id"], lambda: self._missing_materials(plan))
                        steps = math.inf if more is None else steps + more["steps"]
                        if more:
                            levels = {**levels, **{k: max(levels.get(k, 0), v) for k, v in more["levels"].items()}}
                else:
                    seg = self._aseg(src["id"], Objective("item", iid), levels)
                    steps = math.inf if seg["gear"] is None else \
                        count * Objective("item", iid).value(seg["ev"]) + seg["gear"]["steps"]
            if math.isfinite(steps):
                rows.append((steps + sum(self.level_steps(k, self._now(k), v)
                                         for k, v in levels.items()), steps, levels, src["id"]))
        if not rows:
            return self._shop(iid, count)
        _, steps, levels, aid = min(rows, key=lambda r: r[0])
        segs = []
        if aid in gd.activities:
            seg = self._aseg(aid, Objective("item", iid), levels)
            segs = [(seg, steps - seg["gear"]["steps"])] + seg["gear"]["segs"]
        return {"steps": steps, "how": gd.activity_like(aid)["name"], "levels": levels, "segs": segs}

    def token_farm(self) -> dict | None:
        """Best way to farm Adventurers' Guild tokens ("chance to find" gear rolls on any activity)."""
        if not hasattr(self, "_token_farm"):
            ranked = self.s.rank_activities("adventurers_guild_token", top=1, pet=self.pet)["ranking"]
            self._token_farm = None
            if ranked:
                aid = self.gd.resolve(ranked[0]["activity"], "activity")
                self._token_farm = {"steps_per_token": ranked[0]["steps_per_item"],
                                    "seg": self._aseg(aid, Objective("item", "adventurers_guild_token")),
                                    "how": ranked[0]["activity"]}
        return self._token_farm

    def _shop(self, iid: str, count: int) -> dict | None:
        """Buy it from a shop the character can enter: coins aren't modelled (the price is noted), guild tokens
        are farmed."""
        p, gd = self.p, self.gd
        for x in sorted(self.s.shop_sources(iid), key=lambda x: x["price"]):
            if not x["entry_met"] or count > x["stock"]:  # restocking isn't documented: one visit's stock only
                continue
            price, where = x["price"] * count, f"{x['building']} @ {gd.locations[x['location']]['name']}"
            shop_seg = ({"locs": [x["location"]]}, 0.0)
            if x["currency"] == "coins":
                note = "" if p.coins >= price else f"; you have {p.coins:,}, coins aren't modelled"
                return {"steps": 0.0, "how": f"buy at {where} for {price:,} coins{note}", "levels": {},
                        "segs": [shop_seg]}
            tokens = p.item_counts.get("adventurers_guild_token", (0, 0))[0]
            short, farm = max(0, price - tokens), self.token_farm()
            if short and not farm:
                continue
            steps = short * farm["steps_per_token"] if short else 0.0
            return {"steps": steps, "levels": {},
                    "how": f"buy at {where} for {price:,} guild tokens"
                           + (f" (farm {short:,} more at {farm['how']})" if short else ""),
                    "segs": ([(farm["seg"], steps)] if short else []) + [shop_seg]}
        return None

    _busy_recipes: set = set()

    def _with_busy(self, rid: str, fn):
        """Run fn with rid marked in progress (a recipe whose material needs the recipe's own output)."""
        self._busy_recipes = self._busy_recipes | {rid}
        try:
            return fn()
        finally:
            self._busy_recipes = self._busy_recipes - {rid}

    def prereqs(self, aid: str) -> tuple[dict[str, int], list[str]]:
        """Skill levels the activity needs above the character's, and other requirements it can't meet."""
        ctx = self.s._context(aid, None)
        levels, blockers = {}, []
        for r in ctx.activity.get("requirements") or []:
            if r["type"] in GEAR_DEPENDENT_REQS or r["type"] == "service":
                continue
            q = r.get("requirement") or {}
            fac = self.gd.reputation_key_to_faction.get(q.get("gameDataId") or "") if r["type"] == "gameData" else None
            if r["type"] == "skillLevel" and not r.get("opposite"):
                if q.get("level", 1) > self.p.skill_levels.get(q.get("skill"), 1):
                    levels[q["skill"]] = max(levels.get(q["skill"], 0), q["level"])
            elif fac and not r.get("opposite") and not check_all([r], ctx, None):
                amount = float(re.search(r"[\d.]+", str(q.get("data")))[0])
                levels[f"rep:{fac}"] = max(levels.get(f"rep:{fac}", 0), amount)
            elif not check_all([r], ctx, None):
                blockers.append(self.s._req_text(r))
        return levels, blockers

    def _candidates(self, aids) -> list[tuple[str, dict]]:
        """Activities without non-level blockers, lowest level requirement first."""
        out = []
        for aid in dict.fromkeys(aids):
            levels, blockers = self.prereqs(aid)
            if not blockers:
                out.append((aid, levels))
        out.sort(key=lambda t: (sum(t[1].values()), self.s._context(t[0], None).required_level))
        return out[:CANDIDATES]

    def xp_rate(self, skill: str) -> dict:
        """Best way to gain XP in a skill with activities or recipes the character can do now (recipes include the
        steps to farm their materials): {"rate", "how", "seg"} where seg is what one step of it produces."""
        if skill in self._xp_rates:
            return self._xp_rates[skill]
        gd, best = self.gd, {"rate": 0.0, "how": "", "seg": None}
        if skill.startswith("rep:"):  # reputation: the best activity that rewards it per action
            fac = skill[4:]
            for aid, a in gd.activities.items():
                amount = sum(x.get("amount", 0) for x in a.get("rewards") or []
                             if x.get("runtimeType") == "factionReputation" and x.get("faction") == fac)
                if not amount or any(self.prereqs(aid)):
                    continue
                seg = self._aseg(aid, Objective("actions"))
                if seg["gear"] is None or seg["gear"]["steps"]:
                    continue
                rate = amount / seg["ev"].metrics["steps_per_action"]
                if rate > best["rate"]:
                    best = {"rate": rate, "how": a["name"], "seg": seg}
            self._xp_rates[skill] = best
            return best
        doable = [(a, self.s._context(a, None).required_level) for a in list(gd.activities) + list(gd.recipes)
                  if (gd.activity_like(a).get("relatedSkillsList") or [None])[0] == skill
                  and not any(self.prereqs(a))]
        acts = sorted((x for x in doable if x[0] in gd.activities), key=lambda x: -x[1])[:3]
        recs = sorted((x for x in doable if x[0] in gd.recipes), key=lambda x: -x[1])[:3]
        for aid, _ in acts:
            seg = self._aseg(aid, Objective("xp", skill))
            if seg["gear"] is None or seg["gear"]["steps"]:  # levelling uses activities doable with owned gear
                continue
            rate = seg["ev"].metrics["xp_per_step"].get(skill, 0)
            if rate > best["rate"]:
                best = {"rate": rate, "how": gd.activity_like(aid)["name"], "seg": seg}
        for rid, _ in recs:
            ev = self._eval(rid, Objective("xp", skill))[2]
            xps = ev.metrics["xp_per_step"].get(skill, 0)
            if xps <= 0:
                continue
            plan = self.s.plan_recipe(rid, 100, self.near, self.pet)
            if "total_steps_leaves_out" in plan or not plan["total_steps"]:
                continue
            crafts_per_step = plan["completions"] / plan["total_steps"]
            rate = xps * ev.metrics["steps_per_action"] * crafts_per_step
            if rate > best["rate"]:
                best = {"rate": rate, "how": f"{gd.activity_like(rid)['name']} (materials farmed)",
                        "seg": self._rseg(rid, crafts_per_step, plan)}
        self._xp_rates[skill] = best
        return best

    def level_steps(self, skill: str, xp_now: float, to: float) -> float:
        need = self._need(skill, to) - xp_now
        if need <= 0:
            return 0.0
        rate = self.xp_rate(skill)["rate"]
        return need / rate if rate > 0 else math.inf

    # ---------- goals ----------

    def _targets(self, g: dict) -> dict:
        """What a goal counts, resolved once: {"aids"} for action goals, {"items", "fine", "skill", "akw"} for
        drops/items, {"skill"} or {"items"} for crafts."""
        if "_targets" in g:
            return g["_targets"]
        gd, s, t = self.gd, self.s, g["type"]
        items_with = lambda kid: {i for i, it in gd.items.items() if kid in (it.get("keywords") or [])}
        out: dict = {}
        if t == "actions":
            out["aids"] = {gd.resolve(g["activity"], "activity")}
        elif t == "actions_keyword":
            kid = s._keyword_id(g["keyword"])
            out["aids"] = {a for a, x in gd.activities.items() if kid in (x.get("keywords") or [])}
        elif t in ("gain_item", "gain_keyword", "have_item", "stack", "craft_item", "craft_keyword"):
            iid, kid = s._item_or_keyword(g.get("item") or g.get("keyword") or "")
            ids = {iid} if iid else items_with(kid) if kid else set()
            if not ids and norm(g.get("keyword") or "") == "material":
                ids = {i for i, it in gd.items.items() if it.get("type") == "material"}
            out = {"items": ids, "fine": bool(g.get("fine")), "skill": g.get("skill"),
                   "akw": s._keyword_id(g.get("activity_keyword"))}
        elif t == "craft_skill":
            out["skill"] = g["skill"]
        elif t == "wealth":  # coins: dropped (money loot rows) or from selling what drops
            out = {"items": {"coins"}, "fine": False, "skill": None, "akw": None, "value": True}
        g["_targets"] = out
        return out

    def produced(self, g: dict, seg: dict, steps: float) -> float:
        """How many of a goal `steps` of a segment (an activity with its loadout, or a recipe) produce."""
        t, tg, gd = g["type"], self._targets(g), self.gd
        if "rid" in seg:  # crafting: count crafts toward craft goals
            crafts = steps * seg["crafts_per_step"]
            r = gd.activity_like(seg["rid"])
            if t == "craft_skill":
                return crafts if (r.get("relatedSkillsList") or [None])[0] == tg["skill"] else 0.0
            if t in ("craft_item", "craft_keyword") and set(r.get("itemRewards") or {}) & tg["items"]:
                return crafts
            return 0.0
        if "aid" not in seg:  # a stop (e.g. a shop): nothing produced
            return 0.0
        aid, ev = seg["aid"], seg["ev"]
        a = gd.activities.get(aid) or {}
        if t in ("actions", "actions_keyword"):
            return steps / ev.metrics["steps_per_action"] if aid in tg["aids"] else 0.0
        if t == "wealth":
            return steps * self._coins_per_step(ev)
        if t in ("gain_item", "gain_keyword", "have_item", "stack"):
            if tg.get("skill") and (a.get("relatedSkillsList") or [None])[0] != tg["skill"]:
                return 0.0
            if tg.get("akw") and tg["akw"] not in (a.get("keywords") or []):
                return 0.0
            rates = self._drop_rates(ev)
            return steps * sum(r[1] if tg["fine"] else r[0] for i, r in rates.items() if i in tg["items"])
        return 0.0

    def goal(self, g: dict, n: int) -> dict:
        """{"steps", "how", "levels", "segs", "per_unit"} for n more of a goal. segs are (activity or recipe, steps)
        the steps are spent on, where known; per_unit is steps per unit for goals whose cost scales with the count;
        "left" overrides n when fewer are needed (a stack already partly held). NotEstimated if it can't be
        estimated."""
        gd, s, t = self.gd, self.s, g["type"]
        items_with = lambda kid: [i for i, it in gd.items.items() if kid in (it.get("keywords") or [])]
        if n <= 0:
            return {"steps": 0, "how": "done", "levels": {}, "segs": [], "per_unit": 0}
        if t in ("actions", "actions_keyword"):
            rows = []
            for aid, lv in self._candidates(self._targets(g)["aids"]):
                seg = self._aseg(aid, Objective("actions"), lv)
                rows.append((aid, lv, n * seg["ev"].metrics["steps_per_action"], seg))
            return self._best(rows, n)
        if t == "wealth":
            short = n - self.p.coins
            if short <= 0:
                return {"steps": 0, "how": "already have the coins", "levels": {}, "segs": [], "per_unit": 0}
            from .engine import Loadout, evaluate
            quick = []  # rank every doable activity by coins per step with no gear, then optimize the best few
            for aid in gd.activities:
                if aid == "travelling" or any(self.prereqs(aid)):
                    continue
                for loc in gd.activity_locations(aid)[:1]:
                    quick.append((self._coins_per_step(evaluate(s._context(aid, loc), Loadout())), aid))
            rows = []
            for _, aid in sorted(quick, reverse=True)[:5]:
                seg = self._aseg(aid, Objective("reward_rolls"))
                per_step = self._coins_per_step(seg["ev"])
                if per_step > 0:
                    rows.append((aid, {}, short / per_step, seg))
            est = self._best(rows, short, note="selling what it drops, plus coin drops; sell at a general store")
            return {**est, "left": short}
        if t == "rare_egg":
            if not self.rare_egg_chance:
                raise NotEstimated("the chance an egg is rare isn't in the game data or on the wiki; pass "
                                   "rare_egg_chance if you know it")
            est = self.goal({"type": "hatch", "item": None, "n": 1, "text": ""}, 1)
            eggs = 1 / self.rare_egg_chance
            return {**est, "steps": est["per_unit"] * eggs, "per_unit": None, "hatch_steps": 0,
                    "how": f"about {eggs:,.0f} eggs at a {self.rare_egg_chance:.2%} rare chance; " + est["how"]}
        if t == "hatch":
            eggs = {gd.resolve(g["item"], "item")} if g.get("item") else \
                {i for i in gd.items for pet in gd.pets.values() if norm(pet.get("egg", {}).get("name", "")) == i}
            rows = []
            for egg in eggs:
                try:
                    est = self.goal({"type": "gain_item", "item": gd.name(egg), "n": 1, "text": ""}, 1)
                except NotEstimated:
                    continue
                rows.append((est["steps"], egg, est))
            if not rows:
                raise NotEstimated("no pet egg the character can find")
            steps, egg, est = min(rows, key=lambda r: r[0])
            pet = next(p for p in gd.pets.values() if norm(p.get("egg", {}).get("name", "")) == egg)
            hatch = (pet.get("levels") or [{}])[0].get("xp", 0)
            return {**est, "steps": steps * n, "per_unit": steps, "hatch_steps": hatch * n,
                    "how": f"{n} {gd.name(egg)} via {est['how']}; each hatches after {hatch:,} steps as the active "
                           "pet, one at a time, alongside other grinds"}
        if t in ("gain_item", "gain_keyword"):
            tg = g.get("_targets") or self._targets(g)
            if not tg["items"]:
                raise NotEstimated(f"unknown item {g.get('item') or g.get('keyword')}")
            aids = [x["id"] for i in tg["items"] for x in gd.item_sources.get(i, []) if x["kind"] == "activity"]
            iid = next(iter(tg["items"])) if len(tg["items"]) == 1 else None
            rows = []
            for aid, lv in self._candidates(aids):
                obj = Objective("fine_item" if tg["fine"] else "item", iid) if iid else Objective("reward_rolls")
                seg = self._aseg(aid, obj, lv)
                per_step = self.produced(g, seg, 1.0)
                if per_step > 0:
                    rows.append((aid, lv, n / per_step, seg))
            return self._best(rows, n)
        if t in ("craft_item", "craft_keyword", "craft_skill"):
            tg = self._targets(g)
            if t == "craft_skill":
                rids = [r for r in gd.recipes if (gd.activity_like(r).get("relatedSkillsList") or [None])[0] == g["skill"]]
            else:
                rids = [x["id"] for i in tg["items"] for x in gd.item_sources.get(i, []) if x["kind"] == "recipe"]
            rows, extra = [], {}
            for rid, lv in self._candidates(rids):
                with self._at_levels(lv):
                    plan = s.plan_recipe(rid, n, self.near, self.pet)
                if not plan["total_steps"]:
                    continue
                more = self._missing_materials(plan)
                if more is None:
                    continue
                lv = {**lv}
                for k, v in more["levels"].items():
                    lv[k] = max(lv.get(k, 0), v)
                extra[rid] = more
                rows.append((rid, lv, plan["total_steps"] + more["steps"],
                             self._rseg(rid, plan["completions"] / plan["total_steps"], plan)))
            best = self._best(rows, n)
            more = extra.get(best["segs"][0][0]["rid"]) if best["segs"] else None
            if more and more["steps"]:
                best["segs"] = best["segs"] + more["segs"]
                best["how"] += f" (+ {more['how']})"
            return best
        if t == "have_quality":  # "an eternal Spectral Tool": a spectral item for the tool slot, crafted at that quality
            from .gamedata import QUALITY_NAMES
            words = g["keyword"].split()
            kid, slot = s._keyword_id(" ".join(words[:-1])), words[-1].lower()
            ids = [i for i in (items_with(kid) if kid else []) if gd.items[i].get("gearType") == slot]
            quality = {v.lower(): k for k, v in QUALITY_NAMES.items()}.get(g["quality"].lower(), g["quality"])
            if any(q == quality for i in ids for q in self.s._owned_qualities(i)):
                return {"steps": 0, "how": "already own one", "levels": {}, "segs": [], "per_unit": 0}
            return self.goal({**g, "type": "craft_quality", "quality": quality, "_ids": ids}, 1)
        if t == "craft_quality":
            kid = s._keyword_id(g.get("keyword"))
            ids = g.get("_ids") or [i for i in (items_with(kid) if kid else gd.items)
                                    if gd.items[i].get("type") == "crafted"]
            rids = [x["id"] for i in ids for x in gd.item_sources.get(i, []) if x["kind"] == "recipe"]
            rows, mats = [], {}
            for rid, lv in self._candidates(rids):
                with self._at_levels(lv):
                    q = s.craft_quality(rid, g["quality"], pet=self.pet)
                if not q.get("expected_steps"):
                    continue
                try:
                    m = self._materials(q["materials_expected"])
                except NotEstimated:
                    continue
                lv = {**lv, **{k: max(lv.get(k, 0), v) for k, v in m["levels"].items()}}
                mats[rid] = m
                rows.append((rid, lv, q["expected_steps"] + m["steps"],
                             self._rseg(rid, q["expected_items_crafted"] / q["expected_steps"], q)))
            if not rows:
                raise NotEstimated(f"no {g.get('keyword') or ''} recipe reaches {g['quality']} at your levels".strip())
            best = self._best(rows, n, note="materials included")
            best["segs"] = best["segs"] + mats[best["segs"][0][0]["rid"]]["segs"]
            return {**best, "per_unit": None}
        if t in ("skill_level", "all_skills"):
            skills = [g["skill"]] if t == "skill_level" else list(gd.skills)
            return {"steps": 0, "how": "levelling", "levels": {k: n for k in skills}, "segs": [], "per_unit": 0}
        if t in ("equip_keyword", "hold_distinct"):
            kid = s._keyword_id(g["keyword"])
            ids = items_with(kid) if kid else []
            owned = [i for i in ids if i in self.p.all_item_ids]
            short = n - len(owned) if t == "hold_distinct" or norm(g["keyword"]) != "ring" else 0
            if short <= 0:
                return {"steps": 0, "how": "already own enough", "levels": {}, "segs": [], "per_unit": 0}
            costs = sorted((c["steps"] + sum(self.level_steps(k, self._now(k), v)
                                             for k, v in c["levels"].items()), gd.name(i), c)
                           for i in ids if i not in owned if (c := self.supply(i, 1)))
            if len(costs) < short:
                raise NotEstimated(f"only {len(costs)} more {g['keyword']} items are obtainable, even levelling "
                                   f"into their recipes; need {short}")
            pick = costs[:short]
            levels: dict[str, int] = {}
            for _, _, c in pick:
                for k, v in c["levels"].items():
                    levels[k] = max(levels.get(k, 0), v)
            return {"steps": sum(c["steps"] for _, _, c in pick), "per_unit": None,
                    "segs": [x for _, _, c in pick for x in c["segs"]],
                    "how": "; ".join(f"{name} via {c['how']}" for _, name, c in pick), "levels": levels}
        if t == "stack":
            key = (g["keyword"], g["n"])
            if key not in self._stack:
                self._stack[key] = s.cheapest_with_keyword(g["keyword"], g["n"], self.near, self.pet, top=1)
            best = (self._stack[key]["ranking"] or [None])[0]
            if not best:
                raise NotEstimated(f"no {g['keyword']} obtainable now")
            g["_targets"] = {**self._targets(g), "items": {gd.resolve(best["item"], "item")}}  # one stack counts
            return {"steps": best["steps"], "how": f"{best['item']} via {best['how']}", "levels": {}, "segs": [],
                    "per_unit": best["steps"] / max(best["short"], 1), "left": best["short"]}
        if t == "have_item":
            iid = gd.resolve(g["item"], "item")
            c = self.p.item_counts.get(iid, (0, 0))
            short = n - (c[1] if g.get("fine") else max(sum(c), int(iid in self.p.all_item_ids)))
            if short <= 0:
                return {"steps": 0, "how": "already have it", "levels": {}, "segs": [], "per_unit": 0}
            if g.get("fine"):
                c = self._fine_craft(iid, short)
                return {**c, "per_unit": c["steps"] / short, "left": short}
            c = self.supply(iid, short)
            if not c:
                raise NotEstimated(self._why_not(iid))
            return {"steps": c["steps"], "how": f"{gd.name(iid)} via {c['how']}", "levels": c["levels"],
                    "segs": c["segs"], "per_unit": c["steps"] / short, "left": short}
        if t == "equip_item":
            iid = gd.resolve(g["item"], "item")
            if iid in self.p.all_item_ids:
                return {"steps": 0, "how": "already own it", "levels": {}, "segs": [], "per_unit": 0}
            c = self.supply(iid, 1)
            if not c:
                raise NotEstimated(self._why_not(iid))
            return {"steps": c["steps"], "how": f"{gd.name(iid)} via {c['how']}", "levels": c["levels"],
                    "segs": c["segs"], "per_unit": None}
        if t == "while_skill":  # the work_efficiency (or other) goal beside it carries the cost
            return {"steps": 0, "how": f"while doing {g['skill']}", "levels": {}, "segs": [], "per_unit": 0}
        if t == "work_efficiency":
            return self._reach_work_efficiency(g.get("_skill"), g["n"] / 100)
        if t == "while_doing":
            aid = gd.resolve(g["activity"], "activity")
            levels, blockers = self.prereqs(aid)
            if blockers:
                raise NotEstimated(f"{gd.activities[aid]['name']} needs {'; '.join(blockers)}")
            seg = self._aseg(aid, Objective("actions"), levels)
            return {**self._best([(aid, levels, seg["ev"].metrics["steps_per_action"], seg)], 1,
                                 note="one action with the gear it asks for"), "per_unit": None}
        if t == "visit":
            lid = gd.resolve(g["location"], "location")
            src = s._near(None)
            if src is None:
                raise NotEstimated("current location unknown")
            d = s._base_distances(src)[0].get(lid)
            if d is None:
                raise NotEstimated(f"can't reach {gd.locations[lid]['name']} yet")
            return {"steps": d, "how": f"walk to {gd.locations[lid]['name']} (base distance)", "levels": {},
                    "segs": [], "per_unit": None}
        raise NotEstimated({"other": "not modelled (luck, one-off actions or in-game counters)",
                            "wealth": "coin income isn't modelled", "explore_region": "exploration isn't tracked",
                            "while_skill": "needs a specific loadout; check with optimize_loadout",
                            "work_efficiency": "needs a specific loadout; check with optimize_loadout",
                            "while_doing": "needs a specific loadout while doing an activity",
                            "equip_quality": "gear rarity mix isn't modelled",
                            "have_quality": "crafted-quality odds for this item aren't modelled",
                            "craft_distinct": "distinct recipes aren't modelled"}.get(t, f"{t} isn't modelled"))

    def _reach_work_efficiency(self, skill: str | None, target: float) -> dict:
        """One action of a `skill` activity/recipe at `target` work efficiency: the cheapest one whose best loadout
        reaches it, levelling the skill if needed (1.25% per level above the requirement, up to 20 levels)."""
        from .engine import LEVEL_WE_MAX_LEVELS, LEVEL_WE_PER_LEVEL
        if not skill:
            raise NotEstimated("no skill named for the work efficiency")
        gd, rows = self.gd, []
        aids = [a for a in list(gd.activities) + list(gd.recipes)
                if (gd.activity_like(a).get("relatedSkillsList") or [None])[0] == skill]
        for aid, lv in self._candidates(aids):
            seg = self._aseg(aid, Objective("actions"), lv)
            m = seg["ev"].metrics
            if m["max_work_efficiency"] < target:
                continue
            ctx = self.s._context(aid, None)
            have = m["work_efficiency"] + m.get("work_efficiency_wasted", 0)
            over_now = max(0, self.p.skill_levels.get(skill, 1) - ctx.required_level)
            extra = max(0, math.ceil(round((target - have) / LEVEL_WE_PER_LEVEL, 6)))
            if over_now + extra > LEVEL_WE_MAX_LEVELS:
                continue
            levels = {**lv}
            if extra:
                levels[skill] = max(levels.get(skill, 0), self.p.skill_levels.get(skill, 1) + extra)
            steps = m["steps_per_action"]
            if aid in gd.recipes:
                plan = self.s.plan_recipe(aid, 1, self.near, self.pet)
                if "total_steps_leaves_out" in plan:
                    continue
                steps = plan["total_steps"]
            rows.append((aid, levels, steps, seg))
        if not rows:
            raise NotEstimated(f"no {skill} activity reaches {target:.0%} work efficiency with owned gear")
        best = self._best(rows, 1, note=f"{target:.0%} work efficiency with the best loadout")
        return {**best, "per_unit": None}

    def _why_not(self, iid: str) -> str:
        """Why an item can't be got: what blocks the activities that drop it, else no source."""
        name = self.gd.name(iid)
        blocked = self.s.rank_activities(iid, top=1).get("blocked_sources") or [] if any(
            x["kind"] == "activity" for x in self.gd.item_sources.get(iid, [])) else []
        if blocked:
            return f"{name}: " + "; ".join(f"{b['activity']} needs {', '.join(b['unmet'])}" for b in blocked[:3])
        return f"{name}: nothing the character can do drops, makes or sells it"

    def _fine_craft(self, iid: str, count: int) -> dict:
        """Fine crafted items come from crafting with all-fine materials: farm the fine materials, then craft."""
        gd, s = self.gd, self.s
        rid = next((x["id"] for x in gd.item_sources.get(iid, []) if x["kind"] == "recipe"
                    and not any(self.prereqs(x["id"]))), None)
        if not rid:
            raise NotEstimated(f"fine {gd.name(iid)}: no recipe the character can do makes it")
        plan = s.plan_recipe(rid, count, self.near, self.pet)
        steps, segs, hows = float(plan["crafting_steps"]), [], []
        per = plan["completions"] / max(count, 1)
        for group in gd.recipes[rid].get("materials") or []:
            o = group["options"][0]
            need = math.ceil(o["amount"] * count * per) - self.p.item_counts.get(o["item"], (0, 0))[1]
            if need <= 0:
                continue
            ranked = s.rank_activities(o["item"], top=1, pet=self.pet, fine=True)["ranking"]
            if not ranked:
                raise NotEstimated(f"fine {gd.name(o['item'])} can't be farmed at the character's levels")
            farm = need * ranked[0]["steps_per_fine_item"]
            aid = gd.resolve(ranked[0]["activity"], "activity")
            segs.append((self._aseg(aid, Objective("fine_item", o["item"])), farm))
            steps += farm
            hows.append(f"{need} fine {gd.name(o['item'])} at {ranked[0]['activity']}")
        segs.append((self._rseg(rid, plan["completions"] / max(plan["crafting_steps"], 1), plan),
                     plan["crafting_steps"]))
        return {"steps": steps, "how": f"{gd.recipes[rid]['name']} with fine materials ({'; '.join(hows)})",
                "levels": {}, "segs": segs}

    def _missing_materials(self, plan: dict) -> dict | None:
        """Materials a recipe plan couldn't get, got by levelling into them: {"steps", "levels", "segs", "how"};
        None if some can't be got at all."""
        out = {"steps": 0.0, "levels": {}, "segs": [], "how": ""}
        hows = []
        for m in plan["materials"]:
            if "cannot_get" not in (m.get("gather") or {}):
                continue
            c = self.supply(self.gd.resolve(m["item"], "item"), m["short"])
            if not c:
                return None
            out["steps"] += c["steps"]
            out["segs"] += c["segs"]
            for k, v in c["levels"].items():
                out["levels"][k] = max(out["levels"].get(k, 0), v)
            hows.append(f"{m['short']} {m['item']} via {c['how']}")
        out["how"] = "; ".join(hows)
        return out

    def _materials(self, amounts: dict[str, int]) -> dict:
        """Getting a recipe's expected materials beyond what the character has: {"steps", "levels", "segs"}."""
        out = {"steps": 0.0, "levels": {}, "segs": []}
        for name, amount in (amounts or {}).items():
            iid = self.gd.resolve(name, "item")
            short = amount - sum(self.p.item_counts.get(iid, (0, 0)))
            if short <= 0:
                continue
            c = self.supply(iid, short)
            if not c:
                raise NotEstimated(f"{name} for the crafts can't be got")
            out["steps"] += c["steps"]
            out["segs"] += c["segs"]
            for k, v in c["levels"].items():
                out["levels"][k] = max(out["levels"].get(k, 0), v)
        return out

    def _best(self, rows, n: int, note: str | None = None) -> dict:
        """Cheapest (aid, levels, steps, seg) including the levelling it needs from the character's current XP."""
        gear = lambda seg: seg.get("gear", {"steps": 0.0, "segs": [], "how": ""})
        rows = [r for r in rows if math.isfinite(r[2]) and gear(r[3]) is not None]
        if not rows:
            raise NotEstimated("no activity or recipe the character can use (or level into) drops or makes it")
        xp = self.p.skill_xp
        aid, levels, steps, seg = min(rows, key=lambda r: r[2] + gear(r[3])["steps"] + sum(
            self.level_steps(k, xp.get(k, 0), v) for k, v in r[1].items()))
        g = gear(seg)
        how = self.gd.activity_like(aid)["name"] + (f" ({note})" if note else "") + (f" [{g['how']}]" if g["how"] else "")
        inputs = self._inputs(seg, steps)
        if inputs["steps"]:
            how += f" [inputs: {inputs['how']}]"
        total = steps + g["steps"] + inputs["steps"]
        levels = {**levels, **{k: max(levels.get(k, 0), v) for k, v in inputs["levels"].items()}}
        return {"steps": total, "how": how, "levels": levels, "segs": [(seg, steps)] + g["segs"],
                "per_unit": total / n}

    def _inputs(self, seg: dict, steps: float) -> dict:
        """Steps to get the inputs (arrows, traps, bait) `steps` of an activity use up beyond what the character
        has: {"steps", "how"}."""
        if "aid" not in seg or not self.s._input_options(seg["aid"]):
            return {"steps": 0.0, "how": "", "levels": {}}
        actions = math.ceil(steps / seg["ev"].metrics["steps_per_action"])
        sup = self.s._input_supply(seg["aid"], actions, self.near, self.pet)
        total, levels, hows = float(sup["steps"] or 0), {}, []
        options = self.s._input_options(seg["aid"])
        for r, (_, ids, _) in zip(sup["inputs"], options):
            if not r.get("short"):
                continue
            if r.get("get"):
                hows.append(f"{r['short']} {r['get']['item']} via {r['get']['how']}")
                continue
            # none the character can make or farm now: level into the cheapest
            got = sorted(((c["steps"] + sum(self.level_steps(k, self._now(k), v)
                                            for k, v in c["levels"].items()), i, c)
                          for i in ids if (c := self.supply(i, r["short"]))), key=lambda x: x[0])
            if not got:
                raise NotEstimated(f"{self.gd.activity_like(seg['aid'])['name']} uses up {r['input']}, which "
                                   "can't be got")
            _, i, c = got[0]
            total += c["steps"]
            for k, v in c["levels"].items():
                levels[k] = max(levels.get(k, 0), v)
            hows.append(f"{r['short']} {self.gd.name(i)} via {c['how']}")
        return {"steps": total, "how": "; ".join(hows), "levels": levels}

    # ---------- the plan ----------

    def plan(self, achievements: dict[str, dict], recorded: dict[str, dict], unlocked_points: int,
             targets: list[int]) -> dict:
        tasks, skipped, passive = {}, [], {}
        for name, a in achievements.items():
            if recorded.get(name, {}).get("unlocked"):
                continue
            goals = a.get("goals") or []
            done_counts = [int(x.replace(",", "")) for x, _ in
                           re.findall(r"([\d,]+)\s*/\s*([\d,]+)", recorded.get(name, {}).get("progress") or "")]
            if goals and all(g["type"] in PASSIVE for g in goals):
                target = max(CHAR_STEPS[g["n"] - 1] if g["type"] == "character_level" else g["n"] for g in goals)
                passive[name] = {"points": a["points"], "at_steps": target}
                continue
            rows, reasons = [], []
            sibling_skill = next((g["skill"] for g in goals if g["type"] == "while_skill"), None)
            for i, g0 in enumerate(goals):
                g = {**g0, "_skill": sibling_skill}
                n = g["n"] - (done_counts[i] if i < len(done_counts) and g["type"] in LINEAR else 0)
                try:
                    est = self.goal(g, n)
                    rows.append({"g": g, "left": max(est.get("left", n), 0), "est": est})
                except (NotEstimated, KeyError) as e:
                    reasons.append(f"{g['text']}: {e}")
            if reasons or not goals:
                skipped.append({"name": name, "points": a["points"], "why": reasons or ["no goals parsed"]})
                continue
            levels: dict[str, int] = {}
            for r in rows:
                for k, v in r["est"]["levels"].items():
                    levels[k] = max(levels.get(k, 0), v)
            tasks[name] = {"points": a["points"], "goals": rows, "levels": levels}

        xp = {**self.p.skill_xp, **{f"rep:{f}": float(v) for f, v in self.p.reputation.items()}}
        walked, points, order, here = float(self.p.steps), unlocked_points, [], self.start

        def goal_cost(r) -> float:
            if r["left"] <= 0:
                return 0.0
            per = r["est"]["per_unit"]
            return r["left"] * per if per is not None and r["g"]["type"] in LINEAR else r["est"]["steps"]

        def task_cost(t, xp_state) -> tuple[float, float]:
            lvl = sum(self.level_steps(k, xp_state.get(k, 0), v) for k, v in t["levels"].items())
            return sum(goal_cost(r) for r in t["goals"]) + lvl, lvl

        def segments(t, xp_state):
            """(seg, steps) the task spends: each goal's activity for what's left, and the levelling."""
            out = []
            for r in t["goals"]:
                c, full = goal_cost(r), r["est"]["steps"]
                if r["left"] <= 0:
                    continue
                out += [(seg, steps * c / full if full else steps) for seg, steps in r["est"]["segs"]]
            for k, v in t["levels"].items():
                steps = self.level_steps(k, xp_state.get(k, 0), v)
                seg = self.xp_rate(k)["seg"]
                if seg and 0 < steps < math.inf:
                    out.append((seg, steps))
            return out

        def xp_from(segs) -> dict[str, float]:
            gained: dict[str, float] = {}
            for seg, steps in segs:
                if "ev" in seg:
                    per_step = dict(seg["ev"].metrics["xp_per_step"])
                    spa = seg["ev"].metrics["steps_per_action"]
                    for x in (self.gd.activities.get(seg["aid"]) or {}).get("rewards") or []:
                        if x.get("runtimeType") == "factionReputation":
                            key = f"rep:{x['faction']}"
                            per_step[key] = per_step.get(key, 0) + x.get("amount", 0) / spa
                elif "rid" not in seg:
                    continue
                else:  # crafting: the recipe's base XP per craft (gear XP bonuses left out)
                    crafts = seg["crafts_per_step"]
                    per_step = {k: v * crafts for k, v in
                                (self.gd.activity_like(seg["rid"]).get("xpRewardsMap") or {}).items()}
                for k, v in per_step.items():
                    gained[k] = gained.get(k, 0) + v * steps
            return gained

        def credit(t_name, segs, apply: bool) -> dict[str, float]:
            """Share of each other task's remaining cost the segments cover; applied to their goals if apply."""
            shares = {}
            gained = xp_from(segs)
            xp_after = {k: xp.get(k, 0) + gained.get(k, 0) for k in set(xp) | set(gained)}
            for name, u in tasks.items():
                if name == t_name:
                    continue
                before = task_cost(u, xp)[0]
                if not before or not math.isfinite(before):
                    continue
                cut = []
                for r in u["goals"]:
                    got = sum(self.produced(r["g"], seg, steps) for seg, steps in segs) if r["g"]["type"] in LINEAR else 0
                    cut.append(got)
                saved = sum(min(got, r["left"]) * (r["est"]["per_unit"] or 0) for got, r in zip(cut, u["goals"]))
                saved += task_cost(u, xp)[1] - task_cost(u, xp_after)[1]
                if saved > 0:
                    shares[name] = min(1.0, saved / before)
                if apply:
                    for got, r in zip(cut, u["goals"]):
                        r["left"] = max(0.0, r["left"] - got)
            if apply:
                xp.update(xp_after)
            return shares

        hatch_free = walked  # eggs hatch one at a time, as the active pet: when the incubating slot frees up

        def pass_walking():
            nonlocal points
            for name, t in sorted(passive.items(), key=lambda kv: kv[1]["at_steps"]):
                if walked >= t["at_steps"]:
                    points += t["points"]
                    order.append({"name": name, "points": t["points"], "steps": 0,
                                  "how": t.get("how", "comes with walking"),
                                  "total_steps_walked": round(walked), "achievement_points": points})
                    del passive[name]

        pass_walking()
        while tasks:
            scored = {}
            for name, t in tasks.items():
                cost, lvl = task_cost(t, xp)
                segs = segments(t, xp)
                travel, end, stops = self.route([seg["locs"] for seg, _ in segs], here)
                cost += travel
                if not math.isfinite(cost):
                    continue
                shares = credit(name, segs, apply=False)
                value = t["points"] + sum(tasks[u]["points"] * f for u, f in shares.items())
                scored[name] = (value / max(cost, 1), cost, lvl, travel, end, stops, segs)
            if not scored:
                for name, t in tasks.items():
                    skipped.append({"name": name, "points": t["points"],
                                    "why": ["can't reach where it's done, or no XP source for a level it needs"]})
                break
            name = max(scored, key=lambda nm: scored[nm][0])
            _, cost, lvl, travel, here, hops, segs = scored[name]
            real = self.real_travel(hops)
            cost += real - travel
            travel = real
            t = tasks.pop(name)
            ups = {(f"{k[4:]} reputation" if k.startswith("rep:") else k):
                   f"{xp.get(k, 0):g} -> {v:g}" if k.startswith("rep:") else f"{skill_level(xp.get(k, 0))} -> {v}"
                   for k, v in t["levels"].items() if self._need(k, v) > xp.get(k, 0)}
            how_up = {(f"{k[4:]} reputation" if k.startswith("rep:") else k): self.xp_rate(k)["how"]
                      for k in t["levels"]}
            shares = credit(name, segs, apply=True)
            for k, v in t["levels"].items():
                xp[k] = max(xp.get(k, 0), self._need(k, v))
            walked += cost
            hatch = sum(r["est"].get("hatch_steps", 0) for r in t["goals"])
            row = {"name": name, "points": t["points"], "steps": round(cost),
                   "how": [r["est"]["how"] for r in t["goals"] if r["est"]["how"] != "done"]}
            if hatch:  # the points come when the egg hatches, after its steps as the active pet
                hatch_free = max(hatch_free, walked) + hatch
                passive[name] = {"points": t["points"], "at_steps": hatch_free, "how": "egg hatched"}
                row["points_when_hatched"] = f"at {round(hatch_free):,} total steps"
            else:
                points += t["points"]
            if travel:
                row["travel_steps"] = round(travel)
                row["route"] = " → ".join(self.gd.locations[b]["name"] for _, b in hops)
            if ups:
                row["levelling"] = {k: f"{v} ({how_up[k]})" for k, v in ups.items()}
                row["levelling_steps"] = round(lvl)
            advanced = {u: f"{f:.0%}" for u, f in sorted(shares.items(), key=lambda kv: -kv[1]) if f >= 0.05}
            if advanced:
                row["also_advances"] = advanced
            order.append({**row, "total_steps_walked": round(walked), "achievement_points": points})
            pass_walking()
        for name, t in sorted(passive.items(), key=lambda kv: kv[1]["at_steps"]):
            order.append({"name": name, "points": t["points"], "steps": round(t["at_steps"] - walked),
                          "how": "keep walking" + (" (egg hatches)" if t.get("how") == "egg hatched" else ""),
                          "total_steps_walked": round(t["at_steps"]),
                          "achievement_points": (points := points + t["points"])})
            walked = max(walked, t["at_steps"])
        milestones = {}
        for target in targets:
            hit = next((o for o in order if o["achievement_points"] >= target), None)
            milestones[str(target)] = (f"after {hit['name']}, at {hit['total_steps_walked']:,} total steps "
                                       f"({hit['total_steps_walked'] - self.p.steps:,} from now)") if hit else "not reached"
        return {"start": {"achievement_points": unlocked_points, "total_steps": self.p.steps},
                "order": order, "milestones": milestones, "not_estimated": skipped,
                "travel_factor": round(self.travel_factor(), 3),
                "note": "Greedy by achievement points per step, where a grind also scores the share of other "
                        "achievements it advances (same activity's actions and drops, crafts, XP toward their "
                        "levels); that progress is then credited. Levels reached are shared. Steps include "
                        "travel from where the previous achievement ended (base distance × travel_factor, your "
                        "travel gear's speed on a sample route). They're expected values, and loot luck can move "
                        "them a lot. Progress recorded with remember_player_info is counted."}
