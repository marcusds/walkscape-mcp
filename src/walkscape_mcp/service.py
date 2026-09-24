"""Tool implementations, independent of the MCP transport (so they're easy to test)."""

from __future__ import annotations

import json
import math
import re
import threading
import time
from pathlib import Path

from . import gearset, sync
from .engine import (
    GEAR_DEPENDENT_REQS, Context, Loadout, check_requirement, drop_report, evaluate, gear_source, history_key, slot_type,
)
from .gamedata import QUALITIES, QUALITY_NAMES, GameData, describe_requirement, strip_markup
from .optimizer import OBJECTIVES, Objective, all_gear_pool, optimize, player_loadout, prepare, quick_score
from .paths import player_file, player_info_file, snapshot_dir
from .player import OwnedItem, Player, parse_save
from .wiki import Wiki

STALE_AFTER = 7 * 24 * 3600  # fallback only; a save reporting a new game version triggers a refresh sooner
STAMP_WINDOW = 24 * 3600  # a snapshot this fresh is assumed to match the loaded save's game version
RANK_FULL_SEARCH = 40  # rank_activities runs the full search on at most this many (or 3x top) candidates
SLOT_LABELS = {"ring0": "ring 1", "ring1": "ring 2", **{f"tool{i}": f"tool {i + 1}" for i in range(6)}}


class Service:
    def __init__(self):
        self._gd: GameData | None = None
        self._player: Player | None = None
        self._refresh_thread: threading.Thread | None = None
        self._refresh_error: str | None = None
        self._not_met: dict[str, float] = {}  # session only; see remember_player_info
        self.wiki = Wiki()
        self._snap_mtime = 0.0
        self._reload_snapshot()
        self._load_player()
        if self._needs_refresh():
            self.refresh_in_background()

    # ---------- state ----------

    def _needs_refresh(self, game_version: str | None = None) -> bool:
        if self._gd is None:
            return True
        meta = self._gd.meta
        if time.time() - meta.get("fetched_at", 0) > STALE_AFTER:
            return True
        return bool(game_version and meta.get("game_version") and meta["game_version"] != game_version)

    def refresh_in_background(self, game_version: str | None = None) -> str:
        if self._refresh_thread and self._refresh_thread.is_alive():
            return "A game data refresh is already running."

        def run():
            try:
                version = game_version or (self._player.game_version if self._player else None)
                snap = sync.refresh(version)
                self._gd = GameData(snap)
                self._refresh_error = None
                self._load_player()
            except Exception as e:  # keep serving the old snapshot
                self._refresh_error = f"{type(e).__name__}: {e}"

        self._refresh_thread = threading.Thread(target=run, daemon=True)
        self._refresh_thread.start()
        return "Refreshing game data from gear.walkscape.app in the background (takes a few minutes)."

    def _reload_snapshot(self):
        """Pick up snapshots written by another process (e.g. `python -m walkscape_mcp.sync`)."""
        f = snapshot_dir() / "gamedata.json"
        try:
            mtime = f.stat().st_mtime
        except FileNotFoundError:
            return
        if mtime != self._snap_mtime:
            snap = sync.load_snapshot(f)
            if snap:
                self._gd = GameData(snap)
                self._snap_mtime = mtime
                if self._player:
                    self._load_player()

    @property
    def gd(self) -> GameData:
        self._reload_snapshot()
        if self._gd is None:
            if self._refresh_thread and self._refresh_thread.is_alive():
                raise RuntimeError("Game data is still downloading for the first time (a few minutes). Try again shortly.")
            raise RuntimeError(f"No game data available. Last refresh error: {self._refresh_error}")
        return self._gd

    def _load_player(self):
        f = player_file()
        if f.exists() and self._gd is not None:
            try:
                self._player = parse_save(self._gd, f.read_text())
            except Exception:
                self._player = None
                return
            self._stamp_version(self._player.game_version)

    def _stamp_version(self, game_version: str):
        """A fresh snapshot without a recorded version matches the game the save came from."""
        meta = self._gd.meta
        if game_version and not meta.get("game_version") and time.time() - meta.get("fetched_at", 0) < STAMP_WINDOW:
            self._gd.snap.setdefault("_meta", {})["game_version"] = game_version
            sync.save_snapshot(self._gd.snap)
            self._snap_mtime = (snapshot_dir() / "gamedata.json").stat().st_mtime

    def data_status(self) -> dict:
        gd = self._gd
        return {
            "snapshot_fetched": time.strftime("%Y-%m-%d %H:%M", time.localtime(gd.meta.get("fetched_at", 0))) if gd else None,
            "snapshot_game_version": gd.meta.get("game_version") if gd else None,
            "refresh_running": bool(self._refresh_thread and self._refresh_thread.is_alive()),
            "last_refresh_error": self._refresh_error,
            "wiki_dump": self.wiki.state().get("tag"),
            "player_loaded": self._player.name if self._player else None,
        }

    # ---------- player ----------

    def load_save(self, save: str) -> dict:
        text = save.strip()
        if not text.startswith("{") and Path(text).expanduser().exists():
            text = Path(text).expanduser().read_text()
        data = json.loads(text)
        player = parse_save(self.gd, data)
        player_file().write_text(json.dumps(data))
        self._player = player
        out = {**player.summary(self.gd), "remembered_info": self._describe_info(self._info())}
        if not self.gd.meta.get("game_version"):
            self._stamp_version(player.game_version)
        elif self._needs_refresh(player.game_version):
            out["note"] = self.refresh_in_background(player.game_version)
        return out

    # ---------- facts the save doesn't contain ----------
    # Action-history requirements are thresholds on counts that only grow, so only "reached" is persisted.
    # "Not yet" goes stale as the user plays, so it's kept for this session only and asked again later.

    def _info(self) -> dict:
        f = player_info_file()
        try:
            data = json.loads(f.read_text())
        except (FileNotFoundError, json.JSONDecodeError):
            data = {}
        return {"history": data.get("history") or {}, "notes": data.get("notes") or []}

    def _context(self, activity_id: str, location_id: str | None) -> Context:
        return Context.for_player(self.gd, self._player, activity_id, location_id,
                                  history_met=self._info()["history"], history_not_met=self._not_met)

    def _history_thresholds(self) -> dict[str, list[float]]:
        """Every action-history threshold in the game data, by key."""
        gd = self.gd
        if getattr(self, "_thresholds_for", None) is not gd:
            out: dict[str, set] = {}

            def walk(o):
                if isinstance(o, dict):
                    if o.get("type") == "historyData" and isinstance(o.get("requirement"), dict):
                        q = o["requirement"]
                        out.setdefault(history_key(q.get("category", ""), q.get("data")), set()).add(q.get("value", 0))
                    for v in o.values():
                        walk(v)
                elif isinstance(o, list):
                    for v in o:
                        walk(v)

            walk(gd.snap)
            self._thresholds, self._thresholds_for = {k: sorted(v) for k, v in out.items()}, gd
        return self._thresholds

    def _history_label(self, key: str, value: float) -> str:
        category, _, data = key.partition(":")
        if category == "actionCompleted" and data:
            act = self.gd.activities.get(data) or self.gd.recipes.get(data)
            return f"{act['name'] if act else data} completed {value:,g}+ times"
        if category == "stepsWalkedTraveling":
            return f"{value:,g}+ steps walked while travelling"
        return f"{key} >= {value:,g}"

    def _parse_history(self, entry: str, pick) -> tuple[str, float]:
        """'Classic skiing', 'Classic skiing 50' or 'travel steps 125000' -> (key, threshold)."""
        m = re.fullmatch(r"(.*?)[\s:>=]*([\d,]+)", entry.strip())
        name, value = (m.group(1), float(m.group(2).replace(",", ""))) if m else (entry.strip(), None)
        if "travel" in name.lower() and "step" in name.lower():
            key = history_key("stepsWalkedTraveling")
        else:
            _, aid = self._resolve_any(name, ["activity", "recipe"])
            key = history_key("actionCompleted", aid)
        levels = self._history_thresholds().get(key)
        if not levels:
            raise KeyError(f"Nothing in the game depends on {entry!r} (no action-history requirement for it).")
        return key, value if value is not None else pick(levels)

    def remember_player_info(self, completed: list[str] | None = None, not_yet: list[str] | None = None,
                             notes: list[str] | None = None, forget: list[str] | None = None) -> dict:
        info = self._info()
        met = info["history"]
        # forget first, so one call can replace a note or entry without deleting its replacement
        for x in forget or []:
            info["notes"] = [n for n in info["notes"] if x.lower() not in n.lower()]
            for k in list(met):
                if x.lower() in self._history_label(k, met[k]).lower():
                    del met[k]
        skipped = []  # one unusable entry shouldn't discard the rest of the call

        def parse(entry, pick):
            try:
                return self._parse_history(entry, pick)
            except KeyError as e:
                skipped.append(e.args[0] if e.args else str(e))
                return None, None

        for entry in completed or []:
            key, v = parse(entry, max)
            if key is None:
                continue
            met[key] = max(met.get(key, 0), v)
            if self._not_met.get(key, math.inf) <= v:
                del self._not_met[key]
        for entry in not_yet or []:
            key, v = parse(entry, min)
            if key is None:
                continue
            self._not_met[key] = min(self._not_met.get(key, math.inf), v)
            if met.get(key, -1) >= v:
                del met[key]
        for n in notes or []:
            if n not in info["notes"]:
                info["notes"].append(n)
        player_info_file().write_text(json.dumps(info, indent=1) + "\n")
        out = self._describe_info(info)
        if skipped:
            out["skipped"] = skipped
        return out

    def _describe_info(self, info: dict) -> dict:
        out = {"reached": [self._history_label(k, v) for k, v in info["history"].items()], "notes": info["notes"]}
        if self._not_met:
            out["not_yet_this_session"] = [self._history_label(k, v) for k, v in self._not_met.items()]
        return out

    def player(self) -> Player:
        if not self._player:
            raise RuntimeError("No character loaded. Ask the user to paste their exported character JSON "
                               "(in-game: Settings → Export character data) and call load_player_save.")
        return self._player

    def player_summary(self) -> dict:
        return {**self.player().summary(self.gd), "remembered_info": self._describe_info(self._info())}

    # ---------- lookups ----------

    def search(self, query: str, kind: str | None = None) -> list[dict]:
        return self.gd.search(query, [kind] if kind else None, 15)

    def _resolve_any(self, name: str, kinds: list[str]) -> tuple[str, str]:
        for k in kinds:
            try:
                return k, self.gd.resolve(name, k)
            except KeyError:
                pass
        hits = self.gd.search(name, kinds, 5)
        raise KeyError(f"No {'/'.join(kinds)} matching {name!r}. Close matches: {[h['name'] for h in hits]}")

    def _owned_qualities(self, item_id: str) -> list[str]:
        if not self._player:
            return []
        return [oi.quality for oi in self._player.owned_gear.values() if oi.id == item_id]

    def item_info(self, name: str) -> dict:
        gd = self.gd
        item_id = gd.resolve(name, "item")
        item = gd.items[item_id]
        out = {
            "id": item_id, "name": item["name"], "type": item.get("type"), "slot": item.get("gearType"),
            "keywords": item.get("keywords"), "description": item.get("desc"),
            "requirements": [describe_requirement(r) for r in item.get("requirements") or []],
        }
        if item.get("gearType"):
            quals = gd.item_qualities(item_id)
            out["attributes_by_quality"] = {
                f"{q} ({QUALITY_NAMES[q]})" if len(quals) > 1 else q: [gd.describe_attr(a) for a in gd.item_attrs(item_id, q) if a.get("stats")]
                for q in quals
            }
            if self._player:
                out["owned_qualities"] = self._owned_qualities(item_id)
        if item.get("buffs"):
            out["consumable_effect"] = {
                "normal": [gd.describe_attr(a) for a in gd.consumable_attrs(item_id, False)],
                "fine": [gd.describe_attr(a) for a in gd.consumable_attrs(item_id, True)],
                "duration": item["buffs"][0].get("duration"),
            }
        if self._player:
            out["you_have"] = self._have(item_id)
        out["sources"] = self._sources(item_id, 40)
        return out

    def _sources(self, item_id: str, limit: int) -> list[str]:
        gd = self.gd
        srcs = []
        for s in gd.item_sources.get(item_id, [])[:limit]:
            if s["kind"] == "activity":
                a = gd.activities[s["id"]]
                srcs.append(f"activity: {a['name']} @ {', '.join(gd.locations[l]['name'] for l in gd.activity_locations(s['id']))}")
            elif s["kind"] == "gear_special":
                srcs.append(f"'chance to find' attribute on gear: {gd.name(s['id'])}")
            elif s["kind"] == "container":
                srcs.append(f"container: {gd.name(s['id'])}")
            elif s["kind"] == "recipe":
                srcs.append(f"recipe: {gd.recipes[s['id']]['name']}")
        return srcs

    def _have(self, item_id: str) -> str:
        n, fine = self._player.item_counts.get(item_id, (0, 0))
        return f"{n}" + (f" (+{fine} fine)" if fine else "")

    def _requirement_status(self, ctx: Context, reqs: list[dict]) -> list[str]:
        """Requirements marked against the loaded character, so callers don't need a separate lookup."""
        out = []
        for r in reqs or []:
            desc = describe_requirement(r)
            if not self._player:
                out.append(desc)
            elif r["type"] in GEAR_DEPENDENT_REQS:
                out.append(f"{desc} [gear: optimize_loadout handles it]")
            elif r["type"] == "service":
                out.append(f"{desc} [service: done at a location that has it]")
            else:
                ok = check_requirement(r, ctx, None)
                mine = ""
                if r["type"] == "skillLevel":
                    mine = f", you: {ctx.skill_levels.get(r['requirement'].get('skill'), 1)}"
                out.append(f"{desc} [{'met' if ok else 'NOT MET'}{mine}]")
        return out

    def activity_info(self, name: str) -> dict:
        gd = self.gd
        kind, aid = self._resolve_any(name, ["activity", "recipe"])
        a = gd.activity_like(aid)
        ctx = Context(gd, aid, (gd.activity_locations(aid) or [None])[0], {s: 99 for s in gd.skills})
        base = evaluate(ctx, Loadout(), statics=[])
        pctx = self._context(aid, (gd.activity_locations(aid) or [None])[0]) if self._player else ctx
        out = {
            "id": aid, "name": a["name"], "kind": kind, "skills": a.get("relatedSkillsList"),
            "requirements": self._requirement_status(pctx, a.get("requirements") or []),
            "locations": [gd.locations[l]["name"] for l in gd.activity_locations(aid)],
            "base_steps": a.get("workRequired"), "max_work_efficiency": a.get("maxWorkEfficiency"),
            "min_steps": base.metrics["min_possible_steps_per_completion"],
            "base_xp": a.get("xpRewardsMap"),
            "base_drops_no_gear": [{k: v for k, v in r.items() if k != "id"} for r in drop_report(base)],
        }
        if kind == "recipe":
            out["materials"] = [[self._material(o) for o in m["options"]] for m in a.get("materials") or []]
            out["outputs"] = {gd.name(k): v for k, v in (a.get("itemRewards") or {}).items()}
        return out

    def _material(self, o: dict) -> str:
        """'1x Sea cabbage (have 0; from activity: Merfolk farm foraging @ Elara's Lagoon)'."""
        gd, iid = self.gd, o["item"]
        extra = [f"have {self._have(iid)}"] if self._player else []
        srcs = self._sources(iid, 3)
        if srcs:
            extra.append("from " + "; ".join(srcs))
        return f"{o['amount']}x {gd.name(iid)}" + (f" ({'; '.join(extra)})" if extra else "")

    def location_info(self, name: str) -> dict:
        gd = self.gd
        lid = gd.resolve(name, "location")
        loc = gd.locations[lid]
        return {
            "id": lid, "name": loc["name"], "faction": loc.get("faction"), "keywords": loc.get("keywords"),
            "activities": [gd.activities[a]["name"] for a in loc.get("activityList") or [] if a in gd.activities],
            "services": loc.get("serviceList"),
        }

    # ---------- loadouts ----------

    def _pet_options(self, pet: str | None) -> list[tuple[str, int] | None]:
        gd, p = self.gd, self._player
        owned = [(x["species"], int(x["level"])) for x in (p.pets if p else []) if x["species"] in gd.pets]
        pet = (pet or "current").strip().lower()
        if pet == "current":
            cur = next(((x["species"], int(x["level"])) for x in (p.pets if p else []) if x.get("equipped")), None)
            return [cur]
        if pet == "none":
            return [None]
        if pet == "auto":
            return [None] + owned
        species = gd.resolve(pet.split(":")[0], "pet")
        if ":" in pet:
            return [(species, int(pet.split(":")[1]))]
        lvl = next((l for s, l in owned if s == species), 1)
        return [(species, lvl)]

    def _consumable_options(self, consumable: str | None) -> list[tuple[str, bool] | None]:
        gd, p = self.gd, self._player
        c = (consumable or "none").strip().lower()
        if c == "none":
            return [None]
        if c == "auto":
            opts: list[tuple[str, bool] | None] = [None]
            for raw, n in (p.consumables if p else {}).items():
                if n > 0:
                    iid = raw.removesuffix("_fine")
                    if gd.items.get(iid, {}).get("buffs"):
                        opts.append((iid, raw.endswith("_fine")))
            return opts
        fine = c.endswith("fine")
        iid = gd.resolve(c.removesuffix("(fine)").removesuffix("fine").strip(" _-"), "item")
        return [(iid, fine)]

    def _pick_quality(self, item_id: str, quality: str | None, owned_only: bool) -> OwnedItem:
        if quality:
            q = quality.lower()
            inv = {v.lower(): k for k, v in QUALITY_NAMES.items()}
            return OwnedItem(item_id, inv.get(q, q))
        owned = self._owned_qualities(item_id)
        if owned:
            return OwnedItem(item_id, max(owned, key=QUALITIES.index))
        if owned_only and self._player:
            raise ValueError(f"You don't own {self.gd.name(item_id)}")
        return OwnedItem(item_id, self.gd.item_qualities(item_id)[-1 if not owned_only else 0])

    def _parse_item_spec(self, spec: str) -> tuple[str, str | None]:
        """'Farganite pickaxe (epic)' / 'farganite pickaxe@legendary' -> (id, quality)."""
        spec = spec.strip()
        q = None
        for sep in ("@", "("):
            if sep in spec:
                spec, q = spec.split(sep, 1)
                q = q.strip(" )")
        return self.gd.resolve(spec.strip(), "item"), q

    def _start_and_locks(self, ctx: Context, require: list[str], owned_only: bool, pets, consumables):
        start = player_loadout(self._player) if self._player else Loadout()
        start.pet = pets[0] if len(pets) == 1 else start.pet
        start.consumable = consumables[0] if len(consumables) == 1 else None
        locked: dict[str, object] = {}
        if len(pets) == 1:
            locked["pet"] = pets[0]
        if len(consumables) == 1:
            locked["consumable"] = consumables[0]
        for spec in require or []:
            iid, q = self._parse_item_spec(spec)
            oi = self._pick_quality(iid, q, owned_only)
            st = self.gd.items[iid].get("gearType")
            if not st:
                raise ValueError(f"{self.gd.name(iid)} is not equippable")
            n = {"ring": 2, "tool": ctx.tool_slots}.get(st, 1)
            slots = [f"{st}{i}" for i in range(n)] if st in ("ring", "tool") else [st]
            free = [s for s in slots if s not in locked]
            if not free:
                raise ValueError(f"No free {st} slot for {self.gd.name(iid)}")
            # prefer the slot it's already in
            slot = next((s for s in free if start.slots.get(s) and start.slots[s].id == iid), free[0])
            for s in list(start.slots):
                if s != slot and start.slots[s] and start.slots[s].id == iid:
                    start.slots[s] = None
            locked[slot] = oi
        return start, locked

    def _objective(self, objective: str, target: str | None) -> Objective:
        objective = objective.strip().lower()
        if objective not in OBJECTIVES:
            raise ValueError(f"Unknown objective {objective!r}. Options: {OBJECTIVES}")
        if objective in ("item", "fine_item"):
            if not target:
                raise ValueError("objective 'item' needs a target item name")
            target = self.gd.resolve(target, "item")
        elif objective == "xp" and target:
            target = target.lower()
        return Objective(objective, target)

    def _locations_for(self, aid: str, location: str | None) -> list[str | None]:
        gd = self.gd
        if location:
            lid = gd.resolve(location, "location")
            if aid in gd.activities and lid not in gd.activity_locations(aid):
                raise ValueError(f"{gd.activities[aid]['name']} is not available at {gd.locations[lid]['name']}. "
                                 f"Available at: {[gd.locations[l]['name'] for l in gd.activity_locations(aid)]}")
            return [lid]
        locs = gd.activity_locations(aid)
        return locs or [None]

    def _describe_loadout(self, ctx: Context, lo: Loadout, ev) -> dict:
        gd = self.gd
        by_source: dict[str, list[str]] = {}
        for label, text in ev.active:
            by_source.setdefault(label, []).append(text)
        slots = {}
        for s in [x for x in ["head", "cape", "back", "chest", "primary", "secondary", "hands", "legs", "neck", "feet",
                              "ring0", "ring1", "tool0", "tool1", "tool2", "tool3", "tool4", "tool5"] if x in lo.slots]:
            oi = lo.slots[s]
            if not oi:
                continue
            label = gear_source(gd, oi).label
            slots[SLOT_LABELS.get(s, s)] = {"item": label, "active_effects": by_source.get(label, [])}
        other = {k: v for k, v in by_source.items() if k not in {x["item"] for x in slots.values()}}
        return {"slots": slots, "other_active_effects": other}

    def _metrics_summary(self, ev) -> dict:
        m = ev.metrics
        keys = ["work_efficiency", "max_work_efficiency", "work_efficiency_wasted", "steps_per_completion",
                "min_possible_steps_per_completion", "double_action", "double_rewards", "steps_per_action",
                "steps_per_reward_roll", "steps_per_fine_roll", "chest_find", "find_gems", "find_collectibles",
                "xp_per_step"]
        return {k: m[k] for k in keys}

    def evaluate_loadout(self, activity: str, location: str | None = None, gear_set: str | None = None,
                         pet: str | None = "current", consumable: str | None = "none") -> dict:
        gd = self.gd
        _, aid = self._resolve_any(activity, ["activity", "recipe"])
        loc = self._locations_for(aid, location)[0]
        ctx = self._context(aid, loc)
        notes = []
        if gear_set:
            lo, notes = gearset.decode(gd, gear_set)
            if pet and pet != "current":
                lo.pet = self._pet_options(pet)[0]
        else:
            lo = player_loadout(self.player())
            lo.pet = self._pet_options(pet)[0]
            lo.consumable = self._consumable_options(consumable)[0]
        ev = evaluate(ctx, lo)
        return {
            "activity": ctx.activity["name"], "location": gd.locations[loc]["name"] if loc else None,
            "valid": ev.valid, "unmet_activity_requirements": ev.unmet_activity_requirements,
            "items_you_cannot_equip": ev.invalid_items,
            "loadout": self._describe_loadout(ctx, lo, ev),
            "inactive_effects": [f"{l}: {t}" for l, t in ev.inactive],
            "metrics": self._metrics_summary(ev),
            "drops": drop_report(ev, 25),
            "notes": notes + self._context_notes(ctx),
        }

    def _context_notes(self, ctx: Context) -> list[str]:
        notes = []
        if ctx.assumed_history:
            need = "; ".join(sorted(self._history_label(k, v) for k, v in ctx.assumed_history))
            notes.append(f"Assumed reached (the save has no action history): {need}. Ask the user whether they have, "
                         "and record it with remember_player_info (completed or not_yet).")
        if ctx.unverified:
            notes.append(f"Requirement types not modelled (assumed satisfied): {sorted(ctx.unverified)}")
        if ctx.is_recipe:
            notes.append("Recipe: crafting service bonuses/penalties are not modelled; service requirement assumed met.")
        if not self._player:
            notes.append("No character loaded: assuming level 99 in all skills.")
        return notes

    def optimize_loadout(self, activity: str, objective: str, target: str | None = None, location: str | None = None,
                         pet: str | None = "current", consumable: str | None = "none", require_items: list[str] | None = None,
                         exclude_items: list[str] | None = None, owned_only: bool = True,
                         show_missing_upgrades: bool = True) -> dict:
        gd = self.gd
        _, aid = self._resolve_any(activity, ["activity", "recipe"])
        obj = self._objective(objective, target)
        pets = self._pet_options(pet)
        consumables = self._consumable_options(consumable)
        exclude = {gd.resolve(x, "item") for x in exclude_items or []}
        if owned_only and not self._player:
            owned_only = False
        pool = list(self._player.owned_gear.values()) if owned_only else all_gear_pool(gd)

        results = []
        for loc in self._locations_for(aid, location):
            ctx = self._context(aid, loc)
            start, locked = self._start_and_locks(ctx, require_items or [], owned_only, pets, consumables)
            lo, searcher = optimize(ctx, obj, pool, pets, consumables, start=start, locked=locked, exclude=exclude)
            results.append((searcher.score(lo), loc, ctx, lo, searcher, start))
        results.sort(key=lambda r: r[0])
        score, loc, ctx, lo, searcher, start = results[0]
        bare = lo
        lo = searcher.fill_empty(bare)
        filled = [s for s, oi in lo.items() if not bare.slots.get(s)]
        ctx.assumed_history.clear()  # report only what the final and current loadouts depend on
        ev = evaluate(ctx, lo)

        out: dict = {
            "activity": ctx.activity["name"],
            "location": gd.locations[loc]["name"] if loc else None,
            "objective": f"{obj.kind}" + (f" ({gd.name(obj.target) if obj.kind in ('item', 'fine_item') else obj.target})" if obj.target else ""),
            "result": obj.describe(score[1]),
            "valid": ev.valid,
            "unmet_activity_requirements": ev.unmet_activity_requirements,
            "pool": "owned gear" if owned_only else "all gear in the game",
            "loadout": self._describe_loadout(ctx, lo, ev),
            "pet": f"{gd.pets[lo.pet[0]]['name']} lvl {lo.pet[1]}" if lo.pet else None,
            "consumable": (gd.name(lo.consumable[0]) + (" (fine)" if lo.consumable[1] else "")) if lo.consumable else None,
            "metrics": self._metrics_summary(ev),
            "drops": drop_report(ev, 15),
        }
        if len(results) > 1:
            out["other_locations"] = {gd.locations[r[1]]["name"]: obj.describe(r[0][1]) for r in results[1:] if r[1]}

        if self._player:
            cur = player_loadout(self._player)
            cur.pet, cur.consumable = start.pet, start.consumable
            cur_ev = evaluate(ctx, cur)
            cur_score = searcher.score(cur)
            changes = {}
            for s in sorted(set(cur.slots) | set(lo.slots)):
                a, b = cur.slots.get(s), lo.slots.get(s)
                if (a and a.key()) != (b and b.key()):
                    changes[SLOT_LABELS.get(s, s)] = f"{gear_source(gd, a).label if a else '(empty)'} → {gear_source(gd, b).label if b else '(empty)'}"
            out["vs_current_gear"] = {
                "current_result": obj.describe(cur_score[1]) + ("" if cur_ev.valid else " (current gear is not valid for this activity)"),
                "changes": changes or "none — your current gear is already optimal",
            }
            if not math.isinf(cur_score[1]) and not math.isinf(score[1]) and cur_score[1] > 0:
                out["vs_current_gear"]["improvement"] = f"{(1 - score[1] / cur_score[1]) * 100:.1f}% fewer steps per unit" if obj.kind not in ("xp", "total_xp") else f"{(cur_score[1] / score[1] - 1) * 100:.1f}% more XP/step"

        notes = self._context_notes(ctx)
        if owned_only and show_missing_upgrades and self._player:
            out["unowned_upgrades"] = self._missing_upgrades(ctx, obj, pets, consumables, require_items, exclude, score)

        try:
            out["gear_set_export"] = gearset.encode(gd, lo)
        except Exception as e:
            out["gear_set_export"] = f"(export failed: {e})"
        out["planner_link"] = gearset.encode_link(gd, lo, aid)
        out["notes"] = notes + [
            f"Searched {sum(len(v) for v in searcher.space.candidates.values())} candidate items "
            f"({searcher.space.pruned_count} irrelevant ones skipped), {searcher.evals} loadouts evaluated.",
            "'Chance to find' items (e.g. Adventurers' Guild tokens) roll once per reward roll, as in the official planner.",
        ]
        if filled:
            out["notes"].append(
                "Slots the objective left empty were filled with side benefits (tokens, chests, collectibles, "
                f"fine materials, XP...) that cost nothing: {', '.join(SLOT_LABELS.get(s, s) for s in filled)}.")
        return out

    def _missing_upgrades(self, ctx, obj, pets, consumables, require_items, exclude, owned_score) -> dict:
        """Best-in-slot from all gear at Perfect quality, restricted to items the player can equip, vs owned."""
        gd = self.gd
        pool = all_gear_pool(gd, "legendary")
        start, locked = self._start_and_locks(ctx, require_items or [], False, pets, consumables)
        lo, s = optimize(ctx, obj, pool, pets, consumables, start=start, locked=locked, exclude=exclude)
        best = s.score(lo)
        owned_ids = {oi.id for oi in self._player.owned_gear.values()}
        missing = [f"{gear_source(gd, oi).label} [{SLOT_LABELS.get(sl, sl)}]" for sl, oi in lo.slots.items()
                   if oi and oi.id not in owned_ids]
        return {
            "theoretical_best_result": obj.describe(best[1]),
            "items_you_dont_own_in_that_loadout": missing,
            "note": "Uses Perfect quality for crafted items and only items your levels allow you to equip.",
        }

    def rank_activities(self, target: str, top: int = 10, pet: str | None = "current", consumable: str | None = "none",
                        owned_only: bool = True) -> dict:
        """Which activity/location gives the target item in the fewest steps with your best owned loadout."""
        gd = self.gd
        tid = gd.resolve(target, "item")
        obj = Objective("item", tid)
        pets = self._pet_options(pet)
        consumables = self._consumable_options(consumable)
        special = any(s["kind"] == "gear_special" for s in gd.item_sources.get(tid, []))
        acts = [s["id"] for s in gd.item_sources.get(tid, []) if s["kind"] == "activity"]
        if special:
            acts = list(gd.activities)
        pool = list(self._player.owned_gear.values()) if (owned_only and self._player) else all_gear_pool(gd)
        cands, blocked = [], []
        for aid in dict.fromkeys(a for a in acts if a != "travelling"):  # travel steps depend on the route
            for loc in gd.activity_locations(aid) or [None]:
                ctx = self._context(aid, loc)
                if self._player and ctx.skill_levels.get(ctx.main_skill, 0) < ctx.required_level:
                    if not special:  # every activity is a source of "chance to find" items; don't list them all
                        blocked.append((ctx, loc, [f"{ctx.main_skill} lvl {ctx.required_level} "
                                                   f"(you: {ctx.skill_levels.get(ctx.main_skill, 1)})"]))
                    continue
                if any(r["type"] in ("abilityAvailable",) for r in ctx.activity.get("visibilityRequirements") or []):
                    continue
                start, locked = self._start_and_locks(ctx, [], owned_only, pets, consumables)
                searcher, start = prepare(ctx, obj, pool, pets, consumables, start=start, locked=locked)
                cands.append((ctx, loc, searcher, start))
        # A greedy fill ranks activities almost like the full search, so only the most promising get the full
        # search. Its requirement penalty is ignored: greedy sometimes misses a required tool that the full
        # search finds. Checked on all 45 "chance to find" items: identical top 10/20, worst case needed 29/36.
        if len(cands) > RANK_FULL_SEARCH:
            quick = [quick_score(sr, st)[1:] for _, _, sr, st in cands]
            order = sorted(range(len(cands)), key=lambda i: quick[i])
            cands = [cands[i] for i in order[:max(RANK_FULL_SEARCH, 3 * top)]]
        rows = []
        for ctx, loc, searcher, start in cands:
            lo = searcher.run(start)
            sc = searcher.score(lo)
            if sc[0] == 0 and not math.isinf(sc[1]):
                rows.append((sc[1], ctx.activity["name"], gd.locations[loc]["name"] if loc else None))
            elif sc[0] and not special:
                blocked.append((ctx, loc, evaluate(ctx, lo).unmet_activity_requirements))
        rows.sort()
        out = {
            "target": gd.name(tid),
            "ranking": [{"activity": a, "location": l, "steps_per_item": round(v, 1)} for v, a, l in rows[:top]],
            "note": "Each row uses its own optimized loadout; use optimize_loadout on a row for the gear.",
        }
        if self._player:
            out["you_have"] = self._have(tid)
        if blocked:
            out["blocked_sources"] = [{"activity": c.activity["name"], "location": gd.locations[l]["name"] if l else None,
                                       "unmet": u} for c, l, u in blocked[:10]]
        if not rows:
            out["other_sources"] = [x for x in self._sources(tid, 10) if not x.startswith("activity:")]
        return out

    def decode_gear_set(self, gear_set: str) -> dict:
        gd = self.gd
        lo, notes = gearset.decode(gd, gear_set)
        return {
            "slots": {SLOT_LABELS.get(s, s): gear_source(gd, oi).label for s, oi in lo.slots.items() if oi},
            "pet": lo.pet, "consumable": lo.consumable, "notes": notes,
        }
