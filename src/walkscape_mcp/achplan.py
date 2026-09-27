"""Order the remaining achievements by achievement points per step.

Each achievement's parsed goals (wikidata.py) get a step estimate from the existing tools: steps per action or per
drop with the best owned loadout, plan_recipe / farming for items, the best XP rate for levels. Skill levels an
activity needs count as prerequisites. A greedy pass then repeatedly takes the achievement with the most points
per step, where the steps include levelling still needed at that point in the plan; levels reached earlier are
shared, and walking goals (total steps, character level) complete by themselves as the plan's steps add up.
This is a heuristic order, not a proven optimum.
"""

from __future__ import annotations

import math
import re
from contextlib import contextmanager
from dataclasses import replace

from .engine import GEAR_DEPENDENT_REQS, check_all, drop_report
from .gamedata import norm
from .optimizer import Objective
from .player import CHAR_STEPS, SKILL_XP

CANDIDATES = 8  # activities/recipes tried per goal (the most likely ones first)
PASSIVE = ("total_steps", "character_level")


class NotEstimated(Exception):
    pass


class AchievementPlanner:
    def __init__(self, svc, pet: str | None = "auto"):
        self.s, self.gd, self.p, self.pet = svc, svc.gd, svc._player, pet
        self._metrics: dict = {}
        self._xp_rates: dict = {}
        self._stack: dict = {}
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

    def supply(self, iid: str, count: int) -> dict | None:
        """Cheapest way to get `count` of an item, levelling into a recipe or activity if needed:
        {"steps", "how", "levels"}."""
        s, gd = self.s, self.gd
        if c := s._supply_steps(iid, count, self.near, self.pet):
            return {"steps": c["steps"], "how": c["how"], "levels": {}}
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
                rows.append((steps + sum(self.level_steps(k, self.p.skill_levels.get(k, 1), v)
                                         for k, v in levels.items()), steps, levels, src["id"]))
        if not rows:
            return None
        _, steps, levels, aid = min(rows, key=lambda r: r[0])
        return {"steps": steps, "how": gd.activity_like(aid)["name"], "levels": levels}

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

    def xp_rate(self, skill: str) -> tuple[float, str]:
        """Best XP per step in a skill with activities or recipes the character can do now (recipes include the
        steps to farm their materials)."""
        if skill in self._xp_rates:
            return self._xp_rates[skill]
        gd, best = self.gd, (0.0, "")
        doable = [(a, self.s._context(a, None).required_level) for a in list(gd.activities) + list(gd.recipes)
                  if (gd.activity_like(a).get("relatedSkillsList") or [None])[0] == skill
                  and not any(self.prereqs(a))]
        acts = sorted((x for x in doable if x[0] in gd.activities), key=lambda x: -x[1])[:3]
        recs = sorted((x for x in doable if x[0] in gd.recipes), key=lambda x: -x[1])[:3]
        for aid, _ in acts:
            ctx, lo, ev = self._eval(aid, Objective("xp", skill))
            rate = ev.metrics["xp_per_step"].get(skill, 0)
            if rate > best[0]:
                best = (rate, gd.activity_like(aid)["name"])
        for rid, _ in recs:
            ctx, lo, ev = self._eval(rid, Objective("xp", skill))
            xps = ev.metrics["xp_per_step"].get(skill, 0)
            if xps <= 0:
                continue
            plan = self.s.plan_recipe(rid, 100, self.near, self.pet)
            if "total_steps_leaves_out" in plan:
                continue
            per_craft = ev.metrics["steps_per_action"]
            xp_per_craft = xps * per_craft
            rate = xp_per_craft * plan["completions"] / plan["total_steps"] if plan["total_steps"] else 0
            if rate > best[0]:
                best = (rate, f"{gd.activity_like(rid)['name']} (materials farmed)")
        self._xp_rates[skill] = best
        return best

    def level_steps(self, skill: str, frm: int, to: int) -> float:
        if to <= frm:
            return 0.0
        have = max(SKILL_XP[frm - 1], self.p.skill_xp.get(skill, 0) if frm == self.p.skill_levels.get(skill, 1) else 0)
        rate, _ = self.xp_rate(skill)
        if rate <= 0:
            return math.inf
        return (SKILL_XP[to - 1] - have) / rate

    # ---------- goals ----------

    def goal(self, g: dict, n: int) -> dict:
        """{"steps", "how", "levels"} for n more of a goal; NotEstimated if it can't be estimated."""
        gd, s, t = self.gd, self.s, g["type"]
        items_with = lambda kid: [i for i, it in gd.items.items() if kid in (it.get("keywords") or [])]
        if n <= 0:
            return {"steps": 0, "how": "done", "levels": {}}
        if t in ("actions", "actions_keyword"):
            if t == "actions":
                aids = [gd.resolve(g["activity"], "activity")]
            else:
                kid = s._keyword_id(g["keyword"])
                aids = [a for a, x in gd.activities.items() if kid in (x.get("keywords") or [])]
            return self._best([(aid, lv, n * self._eval(aid, Objective("actions"), lv)[2].metrics["steps_per_action"])
                               for aid, lv in self._candidates(aids)])
        if t in ("gain_item", "gain_keyword"):
            iid, kid = s._item_or_keyword(g.get("item") or g.get("keyword") or "")
            ids = [iid] if iid else items_with(kid) if kid else []
            if not ids and norm(g.get("keyword") or "") == "material":
                ids = [i for i, it in gd.items.items() if it.get("type") == "material"]
            if not ids:
                raise NotEstimated(f"unknown item {g.get('item') or g.get('keyword')}")
            aids = [x["id"] for i in ids for x in gd.item_sources.get(i, []) if x["kind"] == "activity"]
            if sk := g.get("skill"):
                aids = [a for a in aids if (gd.activities[a].get("relatedSkillsList") or [None])[0] == sk]
            if akw := s._keyword_id(g.get("activity_keyword")):
                aids = [a for a in aids if akw in (gd.activities[a].get("keywords") or [])]
            rows, fine, want = [], bool(g.get("fine")), set(ids)
            for aid, lv in self._candidates(aids):
                obj = Objective("fine_item" if fine else "item", iid) if iid else Objective("reward_rolls")
                ev = self._eval(aid, obj, lv)[2]
                per_1000 = sum((1000 / d["steps_per_fine"] if "steps_per_fine" in d else 0) if fine
                               else d["per_1000_steps"] for d in drop_report(ev, 10_000) if d["id"] in want)
                if per_1000 > 0:
                    rows.append((aid, lv, n * 1000 / per_1000))
            return self._best(rows)
        if t in ("craft_item", "craft_keyword", "craft_skill"):
            if t == "craft_skill":
                rids = [r for r in gd.recipes if (gd.activity_like(r).get("relatedSkillsList") or [None])[0] == g["skill"]]
            else:
                iid, kid = s._item_or_keyword(g.get("item") or g.get("keyword") or "")
                ids = {iid} if iid else set(items_with(kid)) if kid else set()
                rids = [x["id"] for i in ids for x in gd.item_sources.get(i, []) if x["kind"] == "recipe"]
            rows = []
            for rid, lv in self._candidates(rids):
                with self._at_levels(lv):
                    plan = s.plan_recipe(rid, n, self.near, self.pet)
                if "total_steps_leaves_out" not in plan:
                    rows.append((rid, lv, plan["total_steps"]))
            return self._best(rows)
        if t == "craft_quality":
            kid = s._keyword_id(g.get("keyword"))
            ids = [i for i in (items_with(kid) if kid else gd.items) if gd.items[i].get("type") == "crafted"]
            rids = [x["id"] for i in ids for x in gd.item_sources.get(i, []) if x["kind"] == "recipe"]
            rows = []
            for rid, lv in self._candidates(rids):
                with self._at_levels(lv):
                    q = s.craft_quality(rid, g["quality"], pet=self.pet)
                if q.get("expected_steps"):
                    rows.append((rid, lv, q["expected_steps"]))
            if not rows:
                raise NotEstimated(f"no {g.get('keyword') or ''} recipe reaches {g['quality']} at your levels".strip())
            return self._best(rows, note="crafting steps only; materials extra")
        if t in ("skill_level", "all_skills"):
            skills = [g["skill"]] if t == "skill_level" else list(gd.skills)
            return {"steps": 0, "how": "levelling", "levels": {k: n for k in skills}}
        if t in ("equip_keyword", "hold_distinct"):
            kid = s._keyword_id(g["keyword"])
            ids = items_with(kid) if kid else []
            owned = [i for i in ids if i in self.p.all_item_ids]
            short = n - len(owned) if t == "hold_distinct" or norm(g["keyword"]) != "ring" else 0
            if short <= 0:
                return {"steps": 0, "how": "already own enough", "levels": {}}
            costs = sorted((c["steps"] + sum(self.level_steps(k, self.p.skill_levels.get(k, 1), v)
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
            return {"steps": sum(c["steps"] for _, _, c in pick),
                    "how": "; ".join(f"{name} via {c['how']}" for _, name, c in pick), "levels": levels}
        if t == "stack":
            key = (g["keyword"], g["n"])
            if key not in self._stack:
                self._stack[key] = s.cheapest_with_keyword(g["keyword"], g["n"], self.near, self.pet, top=1)
            best = (self._stack[key]["ranking"] or [None])[0]
            if not best:
                raise NotEstimated(f"no {g['keyword']} obtainable now")
            return {"steps": best["steps"], "how": f"{best['item']} via {best['how']}", "levels": {}}
        if t == "have_item":
            iid = gd.resolve(g["item"], "item")
            c = self.p.item_counts.get(iid, (0, 0))
            short = n - (c[1] if g.get("fine") else max(sum(c), int(iid in self.p.all_item_ids)))
            if short <= 0:
                return {"steps": 0, "how": "already have it", "levels": {}}
            if g.get("fine"):
                raise NotEstimated(f"fine {gd.name(iid)} (fine crafting chances aren't modelled here)")
            c = self.supply(iid, short)
            if not c:
                raise NotEstimated(f"{gd.name(iid)}: no activity or recipe drops or makes it (shops aren't modelled)")
            return {"steps": c["steps"], "how": f"{gd.name(iid)} via {c['how']}", "levels": c["levels"]}
        if t == "equip_item":
            iid = gd.resolve(g["item"], "item")
            if iid in self.p.all_item_ids:
                return {"steps": 0, "how": "already own it", "levels": {}}
            c = self.supply(iid, 1)
            if not c:
                raise NotEstimated(f"{gd.name(iid)}: no activity or recipe drops or makes it (shops aren't modelled)")
            return {"steps": c["steps"], "how": f"{gd.name(iid)} via {c['how']}", "levels": c["levels"]}
        if t == "visit":
            lid = gd.resolve(g["location"], "location")
            src = s._near(None)
            if src is None:
                raise NotEstimated("current location unknown")
            d = s._base_distances(src)[0].get(lid)
            if d is None:
                raise NotEstimated(f"can't reach {gd.locations[lid]['name']} yet")
            return {"steps": d, "how": f"walk to {gd.locations[lid]['name']} (base distance)", "levels": {}}
        raise NotEstimated({"other": "not modelled (luck, one-off actions or in-game counters)",
                            "wealth": "coin income isn't modelled", "explore_region": "exploration isn't tracked",
                            "while_skill": "needs a specific loadout; check with optimize_loadout",
                            "work_efficiency": "needs a specific loadout; check with optimize_loadout",
                            "while_doing": "needs a specific loadout while doing an activity",
                            "equip_quality": "gear rarity mix isn't modelled",
                            "have_quality": "crafted-quality odds for this item aren't modelled",
                            "craft_distinct": "distinct recipes aren't modelled"}.get(t, f"{t} isn't modelled"))

    def _best(self, rows, note: str | None = None) -> dict:
        """Cheapest (aid, levels, steps) including the levelling it needs from the character's current levels."""
        rows = [r for r in rows if math.isfinite(r[2])]
        if not rows:
            raise NotEstimated("no activity or recipe the character can use (or level into) drops or makes it")
        cur = self.p.skill_levels
        aid, levels, steps = min(rows, key=lambda r: r[2] + sum(self.level_steps(k, cur.get(k, 1), v)
                                                                 for k, v in r[1].items()))
        how = self.gd.activity_like(aid)["name"] + (f" ({note})" if note else "")
        return {"steps": steps, "how": how, "levels": levels}

    # ---------- the plan ----------

    def plan(self, achievements: dict[str, dict], recorded: dict[str, dict], unlocked_points: int,
             targets: list[int]) -> dict:
        tasks, skipped, passive = {}, [], {}
        for name, a in achievements.items():
            if recorded.get(name, {}).get("unlocked"):
                continue
            goals = a.get("goals") or []
            done_counts = [int(x.replace(",", "")) for x, _ in
                           re.findall(r"([\d,.]+)\s*/\s*([\d,.]+)", recorded.get(name, {}).get("progress") or "")]
            if goals and all(g["type"] in PASSIVE for g in goals):
                target = max(CHAR_STEPS[g["n"] - 1] if g["type"] == "character_level" else g["n"] for g in goals)
                passive[name] = {"points": a["points"], "at_steps": target}
                continue
            est, reasons = [], []
            for i, g in enumerate(goals):
                n = g["n"] - (done_counts[i] if i < len(done_counts) and g["type"] not in ("skill_level",) else 0)
                try:
                    est.append(self.goal(g, n))
                except NotEstimated as e:
                    reasons.append(f"{g['text']}: {e}")
                except KeyError as e:
                    reasons.append(f"{g['text']}: {e}")
            if reasons or not goals:
                skipped.append({"name": name, "points": a["points"], "why": reasons or ["no goals parsed"]})
                continue
            levels: dict[str, int] = {}
            for e in est:
                for k, v in e["levels"].items():
                    levels[k] = max(levels.get(k, 0), v)
            tasks[name] = {"points": a["points"], "steps": sum(e["steps"] for e in est), "levels": levels,
                           "how": [e["how"] for e in est if e["how"] not in ("done",)]}

        state = dict(self.p.skill_levels)
        walked, points, order = self.p.steps, unlocked_points, []

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
            def cost(t):
                lvl = sum(self.level_steps(k, state.get(k, 1), v) for k, v in t["levels"].items())
                return t["steps"] + lvl, lvl
            scored = {name: cost(t) for name, t in tasks.items()}
            name = max(tasks, key=lambda nm: tasks[nm]["points"] / max(scored[nm][0], 1))
            t, (total, lvl) = tasks.pop(name), scored[name]
            if not math.isfinite(total):
                skipped.append({"name": name, "points": t["points"], "why": ["no XP source for a level it needs"]})
                continue
            ups = {k: f"{state.get(k, 1)} -> {v}" for k, v in t["levels"].items() if v > state.get(k, 1)}
            for k, v in t["levels"].items():
                state[k] = max(state.get(k, 1), v)
            walked += total
            points += t["points"]
            order.append({"name": name, "points": t["points"], "steps": round(total),
                          **({"levelling": {k: f"{v} ({self.xp_rate(k)[1]})" for k, v in ups.items()},
                              "levelling_steps": round(lvl)} if ups else {}),
                          "how": t["how"], "total_steps_walked": round(walked), "achievement_points": points})
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
                "note": "Greedy by achievement points per step; levels needed are shared once reached. Steps are "
                        "expected values without travel between activities, and loot luck can move them a lot. "
                        "Progress recorded with remember_player_info is counted."}
