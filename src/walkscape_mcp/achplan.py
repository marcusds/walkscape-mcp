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
          "have_item", "stack")  # goals whose cost scales with how many are left


class NotEstimated(Exception):
    pass


class AchievementPlanner:
    def __init__(self, svc, pet: str | None = "auto"):
        self.s, self.gd, self.p, self.pet = svc, svc.gd, svc._player, pet
        self._metrics: dict = {}
        self._xp_rates: dict = {}
        self._stack: dict = {}
        self._drops: dict = {}
        near = svc._near(None)
        self.near = self.gd.locations[near]["name"] if near else None

    # ---------- shared estimates ----------

    @contextmanager
    def _at_levels(self, levels: dict[str, int]):
        """Evaluate as if the character had these skill levels (for activities it must level into): drops that
        need a level (e.g. fish) and the level work-efficiency bonus follow the raised levels. Supply estimates get
        their own cache while raised."""
        raise_to = {k: v for k, v in levels.items() if v > self.p.skill_levels.get(k, 1)}
        if not raise_to:
            yield
            return
        s, saved = self.s, (self.s._player, self.s._supply_cache, self.s._supplying)
        xp = {**self.p.skill_xp, **{k: SKILL_XP[v - 1] for k, v in raise_to.items()}}
        s._player, s._supply_cache, s._supplying = replace(self.p, skill_xp=xp), {}, set()
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

    def _drop_rates(self, ev) -> dict[str, tuple[float, float]]:
        """item id -> (per step, fine per step) for an evaluated loadout."""
        key = id(ev)
        if key not in self._drops:
            self._drops[key] = (ev, {d["id"]: (d["per_1000_steps"] / 1000,
                                               1 / d["steps_per_fine"] if "steps_per_fine" in d else 0.0)
                                     for d in drop_report(ev, 10_000)})
        return self._drops[key][1]

    def supply(self, iid: str, count: int) -> dict | None:
        """Cheapest way to get `count` of an item, levelling into a recipe or activity if needed:
        {"steps", "how", "levels"}."""
        s, gd = self.s, self.gd
        if c := s._supply_steps(iid, count, self.near, self.pet):
            segs = []
            if farm := c.get("farm"):  # farmed: the activity also drops other things worth crediting
                aid = gd.resolve(farm["activity"], "activity")
                segs = [({"aid": aid, "ev": self._eval(aid, Objective("item", iid))[2]}, c["steps"])]
            return {"steps": c["steps"], "how": c["how"], "levels": {}, "segs": segs}
        rows = []
        for src in gd.item_sources.get(iid, []):
            if src["kind"] not in ("recipe", "activity"):
                continue
            levels, blockers = self.prereqs(src["id"])
            if blockers or not levels:
                continue
            with self._at_levels(levels):
                if src["kind"] == "recipe":
                    plan = s.plan_recipe(src["id"], count, self.near, self.pet)
                    steps = math.inf if "total_steps_leaves_out" in plan else plan["total_steps"]
                else:
                    ev = self._eval(src["id"], Objective("item", iid), levels)[2]
                    steps = count * Objective("item", iid).value(ev)
            if math.isfinite(steps):
                rows.append((steps + sum(self.level_steps(k, self.p.skill_xp.get(k, 0), v)
                                         for k, v in levels.items()), steps, levels, src["id"]))
        if not rows:
            return None
        _, steps, levels, aid = min(rows, key=lambda r: r[0])
        segs = [({"aid": aid, "ev": self._eval(aid, Objective("item", iid), levels)[2]}, steps)] \
            if aid in gd.activities else []
        return {"steps": steps, "how": gd.activity_like(aid)["name"], "levels": levels, "segs": segs}

    def prereqs(self, aid: str) -> tuple[dict[str, int], list[str]]:
        """Skill levels the activity needs above the character's, and other requirements it can't meet."""
        ctx = self.s._context(aid, None)
        levels, blockers = {}, []
        for r in ctx.activity.get("requirements") or []:
            if r["type"] in GEAR_DEPENDENT_REQS or r["type"] == "service":
                continue
            q = r.get("requirement") or {}
            if r["type"] == "skillLevel" and not r.get("opposite"):
                if q.get("level", 1) > self.p.skill_levels.get(q.get("skill"), 1):
                    levels[q["skill"]] = max(levels.get(q["skill"], 0), q["level"])
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
        doable = [(a, self.s._context(a, None).required_level) for a in list(gd.activities) + list(gd.recipes)
                  if (gd.activity_like(a).get("relatedSkillsList") or [None])[0] == skill
                  and not any(self.prereqs(a))]
        acts = sorted((x for x in doable if x[0] in gd.activities), key=lambda x: -x[1])[:3]
        recs = sorted((x for x in doable if x[0] in gd.recipes), key=lambda x: -x[1])[:3]
        for aid, _ in acts:
            ev = self._eval(aid, Objective("xp", skill))[2]
            rate = ev.metrics["xp_per_step"].get(skill, 0)
            if rate > best["rate"]:
                best = {"rate": rate, "how": gd.activity_like(aid)["name"], "seg": {"aid": aid, "ev": ev}}
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
                        "seg": {"rid": rid, "crafts_per_step": crafts_per_step}}
        self._xp_rates[skill] = best
        return best

    def level_steps(self, skill: str, xp_now: float, to: int) -> float:
        need = SKILL_XP[to - 1] - xp_now
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
        aid, ev = seg["aid"], seg["ev"]
        a = gd.activities.get(aid) or {}
        if t in ("actions", "actions_keyword"):
            return steps / ev.metrics["steps_per_action"] if aid in tg["aids"] else 0.0
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
                ev = self._eval(aid, Objective("actions"), lv)[2]
                rows.append((aid, lv, n * ev.metrics["steps_per_action"], {"aid": aid, "ev": ev}))
            return self._best(rows, n)
        if t in ("gain_item", "gain_keyword"):
            tg = self._targets(g)
            if not tg["items"]:
                raise NotEstimated(f"unknown item {g.get('item') or g.get('keyword')}")
            aids = [x["id"] for i in tg["items"] for x in gd.item_sources.get(i, []) if x["kind"] == "activity"]
            iid = next(iter(tg["items"])) if len(tg["items"]) == 1 else None
            rows = []
            for aid, lv in self._candidates(aids):
                obj = Objective("fine_item" if tg["fine"] else "item", iid) if iid else Objective("reward_rolls")
                seg = {"aid": aid, "ev": self._eval(aid, obj, lv)[2]}
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
            rows = []
            for rid, lv in self._candidates(rids):
                with self._at_levels(lv):
                    plan = s.plan_recipe(rid, n, self.near, self.pet)
                if "total_steps_leaves_out" not in plan and plan["total_steps"]:
                    rows.append((rid, lv, plan["total_steps"],
                                 {"rid": rid, "crafts_per_step": plan["completions"] / plan["total_steps"]}))
            return self._best(rows, n)
        if t == "craft_quality":
            kid = s._keyword_id(g.get("keyword"))
            ids = [i for i in (items_with(kid) if kid else gd.items) if gd.items[i].get("type") == "crafted"]
            rids = [x["id"] for i in ids for x in gd.item_sources.get(i, []) if x["kind"] == "recipe"]
            rows = []
            for rid, lv in self._candidates(rids):
                with self._at_levels(lv):
                    q = s.craft_quality(rid, g["quality"], pet=self.pet)
                if q.get("expected_steps"):
                    rows.append((rid, lv, q["expected_steps"],
                                 {"rid": rid, "crafts_per_step": q["expected_items_crafted"] / q["expected_steps"]}))
            if not rows:
                raise NotEstimated(f"no {g.get('keyword') or ''} recipe reaches {g['quality']} at your levels".strip())
            return {**self._best(rows, n, note="crafting steps only; materials extra"), "per_unit": None}
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
            costs = sorted((c["steps"] + sum(self.level_steps(k, self.p.skill_xp.get(k, 0), v)
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
                raise NotEstimated(f"fine {gd.name(iid)} (fine crafting chances aren't modelled here)")
            c = self.supply(iid, short)
            if not c:
                raise NotEstimated(f"{gd.name(iid)}: no activity or recipe drops or makes it (shops aren't modelled)")
            return {"steps": c["steps"], "how": f"{gd.name(iid)} via {c['how']}", "levels": c["levels"],
                    "segs": c["segs"], "per_unit": c["steps"] / short, "left": short}
        if t == "equip_item":
            iid = gd.resolve(g["item"], "item")
            if iid in self.p.all_item_ids:
                return {"steps": 0, "how": "already own it", "levels": {}, "segs": [], "per_unit": 0}
            c = self.supply(iid, 1)
            if not c:
                raise NotEstimated(f"{gd.name(iid)}: no activity or recipe drops or makes it (shops aren't modelled)")
            return {"steps": c["steps"], "how": f"{gd.name(iid)} via {c['how']}", "levels": c["levels"],
                    "segs": c["segs"], "per_unit": None}
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

    def _best(self, rows, n: int, note: str | None = None) -> dict:
        """Cheapest (aid, levels, steps, seg) including the levelling it needs from the character's current XP."""
        rows = [r for r in rows if math.isfinite(r[2])]
        if not rows:
            raise NotEstimated("no activity or recipe the character can use (or level into) drops or makes it")
        xp = self.p.skill_xp
        aid, levels, steps, seg = min(rows, key=lambda r: r[2] + sum(self.level_steps(k, xp.get(k, 0), v)
                                                                      for k, v in r[1].items()))
        how = self.gd.activity_like(aid)["name"] + (f" ({note})" if note else "")
        return {"steps": steps, "how": how, "levels": levels, "segs": [(seg, steps)], "per_unit": steps / n}

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
            for i, g0 in enumerate(goals):
                g = dict(g0)
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

        xp = dict(self.p.skill_xp)
        walked, points, order = float(self.p.steps), unlocked_points, []

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
                if c > 0 and full:
                    out += [(seg, steps * c / full) for seg, steps in r["est"]["segs"]]
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
                    for k, v in seg["ev"].metrics["xp_per_step"].items():
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

        def pass_walking():
            nonlocal points
            for name, t in sorted(passive.items(), key=lambda kv: kv[1]["at_steps"]):
                if walked >= t["at_steps"]:
                    points += t["points"]
                    order.append({"name": name, "points": t["points"], "steps": 0, "how": "comes with walking",
                                  "total_steps_walked": round(walked), "achievement_points": points})
                    del passive[name]

        pass_walking()
        while tasks:
            scored = {}
            for name, t in tasks.items():
                cost, lvl = task_cost(t, xp)
                if not math.isfinite(cost):
                    continue
                shares = credit(name, segments(t, xp), apply=False)
                value = t["points"] + sum(tasks[u]["points"] * f for u, f in shares.items())
                scored[name] = (value / max(cost, 1), cost, lvl)
            if not scored:
                for name, t in tasks.items():
                    skipped.append({"name": name, "points": t["points"], "why": ["no XP source for a level it needs"]})
                break
            name = max(scored, key=lambda nm: scored[nm][0])
            _, cost, lvl = scored[name]
            t = tasks.pop(name)
            ups = {k: f"{skill_level(xp.get(k, 0))} -> {v}" for k, v in t["levels"].items()
                   if SKILL_XP[v - 1] > xp.get(k, 0)}
            segs = segments(t, xp)
            shares = credit(name, segs, apply=True)
            for k, v in t["levels"].items():
                xp[k] = max(xp.get(k, 0), SKILL_XP[v - 1])
            walked += cost
            points += t["points"]
            row = {"name": name, "points": t["points"], "steps": round(cost),
                   "how": [r["est"]["how"] for r in t["goals"] if r["est"]["how"] != "done"]}
            if ups:
                row["levelling"] = {k: f"{v} ({self.xp_rate(k)['how']})" for k, v in ups.items()}
                row["levelling_steps"] = round(lvl)
            advanced = {u: f"{f:.0%}" for u, f in sorted(shares.items(), key=lambda kv: -kv[1]) if f >= 0.05}
            if advanced:
                row["also_advances"] = advanced
            order.append({**row, "total_steps_walked": round(walked), "achievement_points": points})
            pass_walking()
        for name, t in passive.items():
            order.append({"name": name, "points": t["points"], "steps": round(t["at_steps"] - walked),
                          "how": "keep walking", "total_steps_walked": round(t["at_steps"]),
                          "achievement_points": (points := points + t["points"])})
        milestones = {}
        for target in targets:
            hit = next((o for o in order if o["achievement_points"] >= target), None)
            milestones[str(target)] = (f"after {hit['name']}, at {hit['total_steps_walked']:,} total steps "
                                       f"({hit['total_steps_walked'] - self.p.steps:,} from now)") if hit else "not reached"
        return {"start": {"achievement_points": unlocked_points, "total_steps": self.p.steps},
                "order": order, "milestones": milestones, "not_estimated": skipped,
                "note": "Greedy by achievement points per step, where a grind also scores the share of other "
                        "achievements it advances (same activity's actions and drops, crafts, XP toward their "
                        "levels); that progress is then credited. Levels reached are shared. Steps are expected "
                        "values without travel between activities, and loot luck can move them a lot. Progress "
                        "recorded with remember_player_info is counted."}
