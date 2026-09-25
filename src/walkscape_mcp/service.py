"""Tool implementations, independent of the MCP transport (so they're easy to test)."""

from __future__ import annotations

import difflib
import fcntl
import heapq
import itertools
import json
import math
import os
import re
import threading
import time
from contextlib import contextmanager
from pathlib import Path

from . import gearset, sync
from .engine import (
    GEAR_DEPENDENT_REQS,
    Context,
    Loadout,
    check_all,
    check_requirement,
    drop_report,
    evaluate,
    gear_source,
    history_key,
    input_fits,
    slot_type,
)
from .gamedata import (
    QUALITIES,
    QUALITY_NAMES,
    GameData,
    describe_requirement,
    norm,
    strip_markup,
)
from .optimizer import (
    OBJECTIVES,
    Objective,
    all_gear_pool,
    optimize,
    player_loadout,
    prepare,
    quick_score,
)
from .paths import player_file, player_info_file, snapshot_dir
from .player import SKILL_XP, OwnedItem, Player, parse_save, with_updates
from .quality import at_least, quality_odds
from .services import parse_services_page
from .wiki import Wiki

STALE_AFTER = 7 * 24 * 3600  # fallback only; a save reporting a new game version triggers a refresh sooner
STAMP_WINDOW = 24 * 3600  # a snapshot this fresh is assumed to match the loaded save's game version
RANK_FULL_SEARCH = 40  # rank_activities runs the full search on at most this many (or 3x top) candidates
ACHIEVEMENT_ROW = re.compile(r"(?P<name>[^|]+?) \| (?P<requirements>.+?) \| (?P<rewards>.*?\b(?P<points>\d+) x Achievement point.*)")
ACHIEVEMENT_NOTE = re.compile(r"Unlocked achievement: (?P<name>[^(]+?)\s*(\(.*)?")  # pre-structured notes, migrated
# a service's kind comes from its id/icon (e.g. "sawmill_halfling.png"); recipes require a kind and a tier
SERVICE_KINDS = ("kitchen", "loom", "workshop", "trinketry_bench", "sawmill", "forge", "mailbox", "wardrobe",
                 "mysterious_merchant")
STACK_SIZE = {"material": 25, "consumable": 20}  # per the wiki; crafted items, gear and chests stack to 10
NO_INVENTORY_SLOT = {"other", "collectible"}  # currencies (tokens, chips) and collectibles don't take slots
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
                self._save = parse_save(self._gd, f.read_text())
            except Exception:
                self._save = self._player = None
                return
            self._apply_updates()
            self._stamp_version(self._player.game_version)

    def _apply_updates(self, info: dict | None = None):
        """self._player = the save plus what the user reported since exporting it."""
        if getattr(self, "_save", None) is None:
            self._save = self._player
        if self._save is not None:
            self._player = with_updates(self.gd, self._save, (info or self._info())["since_save"])

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
        self._save = player
        with self._info_lock():
            info = self._info()
            dropped = self._prune_since_save(info, player)
            if dropped:
                self._write_info(info)
        self._apply_updates(info)
        out = {**self._player.summary(self.gd), "remembered_info": self._describe_info(info)}
        if dropped:
            out["updates_now_in_save"] = dropped
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
        info = {"history": data.get("history") or {}, "notes": data.get("notes") or [],
                "achievements": data.get("achievements") or {}, "goals": data.get("goals") or [],
                "explored": data.get("explored") or [], "location": data.get("location"),
                "since_save": {k: (data.get("since_save") or {}).get(k) or {} for k in ("gear", "skills", "items")}}
        for n in list(info["notes"]):
            if m := ACHIEVEMENT_NOTE.fullmatch(n):
                info["achievements"].setdefault(m["name"], {})["unlocked"] = True
                info["notes"].remove(n)
        return info

    @contextmanager
    def _info_lock(self):
        """Several MCP server processes (one per client session) share player_info.json."""
        with open(player_info_file().with_name("player_info.lock"), "w") as f:
            fcntl.flock(f, fcntl.LOCK_EX)
            yield

    def _write_info(self, info: dict):
        f = player_info_file()
        tmp = f.with_name(f"{f.name}.{os.getpid()}.tmp")
        tmp.write_text(json.dumps(info, indent=1) + "\n")
        tmp.replace(f)

    def _prune_since_save(self, info: dict, save: Player) -> list[str]:
        """Drop reported updates that a newer save now covers. Returns what was dropped."""
        since, dropped = info["since_save"], []
        levels = save.skill_levels
        for section, covered in (
            ("gear", lambda k, e: k in save.owned_gear),
            ("skills", lambda k, e: levels.get(k, 0) >= e["level"]),
            ("items", lambda k, e: False),
        ):
            for k, e in list(since[section].items()):
                if covered(k, e) or e.get("at_steps", 0) < save.steps:
                    del since[section][k]
                    dropped.append(self._since_label(section, k, e))
        return dropped

    def _since_label(self, section: str, key: str, e: dict) -> str:
        if section == "gear":
            item_id, _, q = key.partition("@")
            return f"{self.gd.name(item_id)} ({q})"
        if section == "skills":
            return f"{key} {e['level']}"
        return f"{e['count']:,} {self.gd.name(key)}"

    def _realms(self) -> dict[str, str]:
        """Region id -> display name, for exploreRealm requirements."""
        out = {}
        for loc in self.gd.locations.values():
            if f := loc.get("faction"):
                out[f] = f.replace("_", " ").title()
        return out

    def achievement_list(self) -> dict[str, dict]:
        """Every achievement on the wiki's Achievements page, by name. Empty if the wiki is unavailable."""
        tag = self.wiki.state().get("tag")
        if getattr(self, "_achievements_for", None) != tag or not getattr(self, "_achievements", None):
            out, difficulty = {}, None
            try:
                self.wiki.update()
                text = self.wiki.page("Achievements", 1_000_000)
            except Exception:
                return {}
            for line in text.splitlines():
                line = line.strip()
                if m := re.fullmatch(r"(Easy|Normal|Hard|Extreme) Achievements", line):
                    difficulty = m[1].lower()
                elif m := ACHIEVEMENT_ROW.fullmatch(line):
                    out[m["name"]] = {"difficulty": difficulty, "points": int(m["points"]),
                                      "requirements": m["requirements"], "rewards": m["rewards"]}
            self._achievements, self._achievements_for = out, tag
        return self._achievements

    def _resolve_achievement(self, name: str, known: dict[str, dict]) -> str:
        if not known:  # no wiki: store the name as given
            return name.strip()
        by_lower = {k.lower(): k for k in known}
        if hit := by_lower.get(name.strip().lower()):
            return hit
        close = difflib.get_close_matches(name.strip().lower(), by_lower, n=3, cutoff=0.6)
        if len(close) == 1 or (close and difflib.SequenceMatcher(None, name.lower(), close[0]).ratio() >= 0.85):
            return by_lower[close[0]]
        raise KeyError(f"No achievement matching {name!r}. Close matches: {[by_lower[c] for c in close]}")

    def _context(self, activity_id: str, location_id: str | None) -> Context:
        info = self._info()
        ctx = Context.for_player(self.gd, self._player, activity_id, location_id,
                                 history_met=info["history"], history_not_met=self._not_met,
                                 explored=set(info["explored"]))
        if activity_id in self.gd.recipes and location_id and (sv := self._recipe_service_at(activity_id, location_id)):
            # the service's bonuses count like gear; gear it needs (e.g. diving gear) becomes a requirement
            ctx.service = sv
            gear_reqs = [r for r in sv["requirements"] if r["type"] in GEAR_DEPENDENT_REQS]
            if gear_reqs:
                ctx.activity = {**ctx.activity, "requirements": [*(ctx.activity.get("requirements") or []), *gear_reqs]}
        return ctx

    # ---------- crafting services ----------

    def service_table(self) -> dict[str, dict]:
        """Service id -> {id, name, kind, tier, attrs, requirements, attr_text}, bonuses from the wiki."""
        tag = self.wiki.state().get("tag")
        if getattr(self, "_services_for", None) != tag or getattr(self, "_services", None) is None:
            try:
                self.wiki.update()
                wiki = {norm(k): v for k, v in parse_services_page(self.wiki.page("Services", 1_000_000)).items()}
            except Exception:
                wiki = {}
            out = {}
            for x in self.gd.snap.get("services_list") or []:
                base = self._service(x["id"])
                w = wiki.get(norm(base["name"]), {})
                out[x["id"]] = {**base, "tier": w.get("tier", base["tier"]), "attrs": w.get("attrs", []),
                                "requirements": w.get("requirements", []), "attr_text": w.get("attr_text", ""),
                                "on_wiki": bool(w)}
            self._services, self._services_for = out, tag
        return self._services

    def _recipe_service_req(self, rid: str) -> dict | None:
        return next((r["requirement"] for r in self.gd.recipes[rid].get("requirements") or []
                     if r["type"] == "service"), None)

    def _serves(self, sv: dict, need: dict) -> bool:
        """A service fits a recipe if it's the right kind and tier. Advanced services are assumed to cover basic
        recipes too."""
        return sv["kind"] == need.get("serviceKeyword") and (need.get("tier") != "advanced" or sv["tier"] == "advanced")

    def _recipe_service_at(self, rid: str, lid: str) -> dict | None:
        need = self._recipe_service_req(rid)
        if not need:
            return None
        table = self.service_table()
        fits = [table[x] for x in self.gd.locations[lid].get("serviceList") or [] if x in table and self._serves(table[x], need)]
        return fits[0] if fits else None

    def _recipe_locations(self, rid: str) -> list[str]:
        """Locations with a service for the recipe that the character can use, one per distinct setting
        (service, region, location keywords), since otherwise identical kitchens give identical results."""
        gd, seen, out = self.gd, set(), []
        for lid, loc in gd.locations.items():
            sv = self._recipe_service_at(rid, lid)
            if not sv:
                continue
            key = (sv["id"], loc.get("faction"), frozenset(loc.get("keywords") or []))
            if key in seen:
                continue
            ctx = Context.for_player(gd, self._player, rid, lid)
            if not check_all([r for r in sv["requirements"] if r["type"] not in GEAR_DEPENDENT_REQS], ctx, None):
                continue
            seen.add(key)
            out.append(lid)
        return out

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
                             notes: list[str] | None = None, forget: list[str] | None = None,
                             achievements_unlocked: list[str] | None = None,
                             achievement_progress: dict[str, str] | None = None,
                             achievements_not_unlocked: list[str] | None = None,
                             gear_found: list[str] | None = None, skill_levels: dict[str, int] | None = None,
                             item_counts: dict[str, int] | None = None, goals: list[str] | None = None,
                             goals_done: list[str] | None = None, regions_explored: list[str] | None = None,
                             location: str | None = None) -> dict:
        with self._info_lock():
            info = self._info()
            skipped: list[str] = []  # one unusable entry shouldn't discard the rest of the call
            if location:
                try:
                    info["location"] = self.gd.resolve(location, "location")
                except KeyError as e:
                    skipped.append(e.args[0])
            self._remember_since_save(info, gear_found, skill_levels, item_counts, skipped)
            for g in goals_done or []:
                info["goals"] = [x for x in info["goals"] if g.lower() not in x.lower()]
            info["goals"] += [g for g in goals or [] if g not in info["goals"]]
            for r in regions_explored or []:
                realms = {norm(v): k for k, v in self._realms().items()} | {k: k for k in self._realms()}
                if (rid := realms.get(norm(r))) is None:
                    skipped.append(f"No region matching {r!r}. Regions: {sorted(self._realms().values())}")
                elif rid not in info["explored"]:
                    info["explored"].append(rid)
            out = self._remember(info, skipped, completed, not_yet, notes, forget,
                                 achievements_unlocked, achievement_progress, achievements_not_unlocked)
        self._apply_updates(info)
        return out

    def _remember_since_save(self, info: dict, gear, skills, items, skipped: list[str]):
        since, at = info["since_save"], {"at_steps": self._save.steps if getattr(self, "_save", None) else 0}
        for spec in gear or []:
            try:
                iid, q = self._parse_item_spec(spec)
            except KeyError as e:
                skipped.append(e.args[0])
                continue
            item = self.gd.items[iid]
            if not item.get("gearType"):
                skipped.append(f"{item['name']} isn't gear; use item_counts for materials and consumables")
                continue
            q = (q or item.get("quality") or "common").lower()
            if q not in QUALITIES:
                skipped.append(f"Unknown quality {q!r} for {item['name']}")
                continue
            since["gear"][f"{iid}@{q}"] = at
        for skill, level in (skills or {}).items():
            if (sk := norm(skill)) not in self.gd.skills:
                skipped.append(f"No skill {skill!r}")
                continue
            since["skills"][sk] = {"level": int(level), **at}
        for name, count in (items or {}).items():
            try:
                iid = self.gd.resolve(name.removesuffix(" (fine)"), "item")
            except KeyError as e:
                skipped.append(e.args[0])
                continue
            since["items"][iid] = {"count": int(count), **at}

    def _remember(self, info, skipped, completed, not_yet, notes, forget, unlocked, progress, not_unlocked) -> dict:
        met = info["history"]
        # forget first, so one call can replace a note or entry without deleting its replacement
        for x in forget or []:
            info["notes"] = [n for n in info["notes"] if x.lower() not in n.lower()]
            for k in list(met):
                if x.lower() in self._history_label(k, met[k]).lower():
                    del met[k]
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

        # achievements live apart from notes, so a broad `forget` can't wipe them
        known = self.achievement_list() if unlocked or progress or not_unlocked else {}
        ach, today = info["achievements"], time.strftime("%Y-%m-%d")

        def resolve(name):
            try:
                return self._resolve_achievement(name, known)
            except KeyError as e:
                skipped.append(e.args[0])

        for name in not_unlocked or []:
            if (a := resolve(name)) and a in ach:
                ach[a].pop("unlocked", None)
                ach[a]["updated"] = today
        for name, value in (progress or {}).items():
            if a := resolve(name):
                ach.setdefault(a, {}).update(progress=str(value), updated=today)
        for name in unlocked or []:
            if a := resolve(name):
                ach[a] = {"unlocked": True, "updated": today}
        for a in [a for a, v in ach.items() if not v.get("unlocked") and not v.get("progress")]:
            del ach[a]

        self._write_info(info)
        out = self._describe_info(info)
        if skipped:
            out["skipped"] = skipped
        return out

    def _describe_info(self, info: dict) -> dict:
        out = {"reached": [self._history_label(k, v) for k, v in info["history"].items()], "notes": info["notes"]}
        ach = info.get("achievements") or {}
        out["achievements_unlocked"] = sorted(a for a, v in ach.items() if v.get("unlocked"))
        out["achievements_in_progress"] = {a: f"{v['progress']} (as of {v.get('updated', '?')})"
                                           for a, v in sorted(ach.items()) if v.get("progress") and not v.get("unlocked")}
        out["goals"] = info.get("goals") or []
        if info.get("location") in self.gd.locations:
            out["current_location"] = self.gd.locations[info["location"]]["name"]
        if info.get("explored"):
            out["regions_explored"] = [self._realms().get(r, r) for r in info["explored"]]
        since = info.get("since_save") or {}
        if any(since.values()):
            out["since_last_save"] = [self._since_label(sec, k, e) for sec in ("gear", "skills", "items")
                                      for k, e in (since.get(sec) or {}).items()]
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

    def achievements(self, show: str = "not_unlocked") -> dict:
        """Wiki achievement list merged with what the user has told us."""
        known = self.achievement_list()
        if not known:
            raise RuntimeError("Couldn't read the wiki's Achievements page.")
        ach = self._info()["achievements"]
        rows = []
        for name, a in known.items():
            mine = ach.get(name, {})
            status = "unlocked" if mine.get("unlocked") else "in progress" if mine.get("progress") else "not recorded"
            if show == "all" or (show == "unlocked") == (status == "unlocked"):
                rows.append({"name": name, **a, "status": status, **({"progress": mine["progress"]}
                                                                      if mine.get("progress") else {})})
        recorded = sum(known[a]["points"] for a, v in ach.items() if v.get("unlocked") and a in known)
        out = {"achievements": rows, "recorded_unlocked_points": recorded}
        if self._player:
            out["save_achievement_points"] = self._player.achievement_points
            if recorded < self._player.achievement_points:
                out["note"] = ("The save has more points than the recorded unlocks add up to, so some unlocked "
                               "achievements aren't recorded yet. 'not recorded' may still be unlocked; ask the user.")
        unmatched = sorted(a for a in ach if a not in known)
        if unmatched:
            out["recorded_but_not_on_wiki"] = unmatched
        return out

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
            **({"inputs_used_each_action": inp} if (inp := self._activity_inputs(aid)) else {}),
            **({"visibility": {"status": v[0], "unlocks_after": v[1]}}
               if (v := self._visibility(pctx))[0] != "visible" else {}),
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

    def _objective(self, objective: str, target: str | None, targets: dict[str, int] | None = None) -> Objective:
        objective = objective.strip().lower()
        if objective not in OBJECTIVES:
            raise ValueError(f"Unknown objective {objective!r}. Options: {OBJECTIVES}")
        if objective == "items":
            if not targets:
                raise ValueError("objective 'items' needs targets, e.g. {\"Flax\": 50, \"Honeycomb\": 59}")
            return Objective("items", targets={self.gd.resolve(k, "item"): int(v) for k, v in targets.items()})
        if objective in ("item", "fine_item"):
            if not target:
                raise ValueError("objective 'item' needs a target item name")
            target = self.gd.resolve(target, "item")
        elif objective == "xp" and target:
            target = target.lower()
        return Objective(objective, target)

    def _locations_for(self, aid: str, location: str | None) -> list[str | None]:
        gd = self.gd
        if aid in gd.recipes and self._recipe_service_req(aid):
            if location:
                lid = gd.resolve(location, "location")
                if not self._recipe_service_at(aid, lid):
                    need = self._recipe_service_req(aid)
                    raise ValueError(f"{gd.locations[lid]['name']} has no {need.get('serviceKeyword')} "
                                     f"({need.get('tier')}); find_services lists where to go.")
                return [lid]
            return self._recipe_locations(aid) or [None]
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

    def _req_text(self, r: dict) -> str:
        """describe_requirement with item/activity ids replaced by their names."""
        q, text = r.get("requirement") or {}, describe_requirement(r)
        for v in (q.get("item"), q.get("data")):
            if v in self.gd.items:
                text = text.replace(v, self.gd.name(v))
            elif v in self.gd.activities or v in self.gd.recipes:
                text = text.replace(v, self.gd.activity_like(v)["name"])
        return text

    def _visibility(self, ctx: Context) -> tuple[str, list[str]]:
        """Whether the activity shows up in the game for this character: ("visible" | "assumed" | "hidden" |
        "emergency", unlock conditions). Hidden activities appear only after e.g. completing another activity;
        the save has no action history, so unknown ones are "assumed" (and noted) unless the user said otherwise."""
        vis = ctx.activity.get("visibilityRequirements") or []
        if not vis:
            return "visible", []
        if any(r["type"] in GEAR_DEPENDENT_REQS for r in vis):
            return "emergency", []  # only offered when you lack the gear to leave, e.g. light sources or diving gear
        labels = [self._req_text(r) for r in vis]
        if not all(check_requirement(r, ctx, None) for r in vis):
            return "hidden", labels
        unconfirmed = [r for r in vis if r["type"] == "historyData" and not r.get("opposite")
                       and ctx.history_met.get(history_key(r["requirement"].get("category", ""),
                                                           r["requirement"].get("data")), -math.inf)
                       < r["requirement"].get("value", 0)]
        return ("assumed", labels) if unconfirmed else ("visible", labels)

    def _activity_inputs(self, aid: str) -> list[str]:
        """What an activity uses up each action (arrows, traps, plants...) and what the character has of it."""
        gd, out = self.gd, []
        for opt in gd.activity_like(aid).get("options") or []:
            for inp in opt.get("inputs") or []:
                if inp.get("type") == "specific":
                    ids, need = [inp["item"]], f"{inp.get('quantity', 1)}x {gd.name(inp['item'])}"
                else:
                    kw = inp.get("keyword")
                    ids = [k for k, i in gd.items.items() if kw in (i.get("keywords") or [])]
                    need = f"one {kw.replace('_', ' ')} item"
                reqs = inp.get("requirements") or []
                ids = [i for i in ids if all(input_fits(gd, i, r) for r in reqs if r["type"] == "inputKeywordWithLevel")]
                if reqs:
                    need += f" ({'; '.join(describe_requirement(r) for r in reqs)})"
                if self._player:
                    have = [f"{gd.name(i)} ({self._have(i)})" for i in ids if sum(self._player.item_counts.get(i, (0, 0)))]
                    need += f"; you have: {', '.join(have) if have else 'none'}"
                out.append(need)
        return out

    def _context_notes(self, ctx: Context) -> list[str]:
        notes = []
        if inputs := self._activity_inputs(ctx.activity_id):
            notes.append(f"Uses up each action (not counted in steps): {' | '.join(inputs)}.")
        status, unlock = self._visibility(ctx)
        if status == "hidden":
            notes.append(f"HIDDEN ACTIVITY: not visible in-game until {'; '.join(unlock)}.")
        elif status == "assumed":
            notes.append(f"Hidden activity: only visible in-game after {'; '.join(unlock)}. Assumed done; if the "
                         "user can't find it, that's why.")
        elif status == "emergency":
            notes.append("Emergency activity: only offered when missing the gear to travel away.")
        if ctx.assumed_history:
            need = "; ".join(sorted(self._history_label(k, v) for k, v in ctx.assumed_history))
            notes.append(f"Assumed reached (the save has no action history): {need}. Ask the user whether they have, "
                         "and record it with remember_player_info (completed or not_yet).")
        if ctx.unverified:
            notes.append(f"Requirement types not modelled (assumed satisfied): {sorted(ctx.unverified)}")
        if ctx.is_recipe and ctx.service:
            sv = ctx.service
            notes.append(f"Crafted at {sv['name']} in {self.gd.locations[ctx.location_id]['name']}"
                         + (f": {sv['attr_text'].rstrip('.')}" if sv.get("attrs") else " (no service bonuses)")
                         + ("" if sv.get("on_wiki") else "; bonuses unknown (not on the wiki's Services page)") + ".")
        elif ctx.is_recipe and self._recipe_service_req(ctx.activity_id):
            notes.append("Recipe: no usable service location found, so service bonuses aren't counted.")
        if not self._player:
            notes.append("No character loaded: assuming level 99 in all skills.")
        return notes

    def optimize_loadout(self, activity: str, objective: str, target: str | None = None, location: str | None = None,
                         pet: str | None = "current", consumable: str | None = "none", require_items: list[str] | None = None,
                         exclude_items: list[str] | None = None, owned_only: bool = True,
                         show_missing_upgrades: bool = True, targets: dict[str, int] | None = None) -> dict:
        gd = self.gd
        _, aid = self._resolve_any(activity, ["activity", "recipe"])
        obj = self._objective(objective, target, targets)
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
            **({"service": ctx.service["name"]} if ctx.service else {}),
            "objective": f"{obj.kind}" + (f" ({gd.name(obj.target) if obj.kind in ('item', 'fine_item') else obj.target})" if obj.target else "")
                         + (f" ({', '.join(f'{n} {gd.name(i)}' for i, n in obj.targets.items())})" if obj.targets else ""),
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

    # ---------- travel ----------

    def _route_graph(self) -> dict[str, list[tuple[str, dict]]]:
        g: dict[str, list[tuple[str, dict]]] = {}
        for r in self.gd.snap.get("routes") or []:
            a, b = r["locations"]
            g.setdefault(a, []).append((b, r))
            g.setdefault(b, []).append((a, r))
        return g

    def _leg_modifiers(self, route: dict, origin: str) -> list[dict]:
        """Terrain modifiers (required gear, items, levels) for travelling the route away from `origin`."""
        mods = {t["id"]: t for t in self.gd.snap.get("terrain_modifiers") or []}
        return [mods[t] for o in route.get("options") or [] if (o.get("options") or {}).get(origin)
                for t in o.get("terrainModifiers") or [] if t in mods]

    def _leg_context(self, origin: str, route: dict) -> Context:
        """Travelling one leg is an activity whose work is a tenth of the distance (a route is 10 actions) and whose
        requirements are the leg's terrain modifiers. Location-conditional gear uses the leg's starting location."""
        ctx = self._context("travelling", origin)
        ctx.activity = {**ctx.activity, "workRequired": route["distance"] / 10,
                        "requirements": [r for m in self._leg_modifiers(route, origin) for r in m["requirements"]]}
        return ctx

    def _modifier_label(self, m: dict) -> str:
        """e.g. "Jarvonian border check: have Jarvonian letter of passage with you"."""
        name = m.get("name") or m["id"]
        if "." in name:  # untranslated key like terrainmodifiers.singulars.requiresability.navigatedesert.name
            name = m["id"].split(".")[-2]
        reqs = []
        for r in m["requirements"]:
            q = r.get("requirement") or {}
            text = describe_requirement(r)
            for v in (q.get("item"), q.get("data")):
                if v in self.gd.items or v in self.gd.activities:
                    text = text.replace(v, self.gd.name(v) if v in self.gd.items else self.gd.activities[v]["name"])
            reqs.append(text)
        return f"{name}: {'; '.join(reqs)}" if reqs else name

    def _near(self, near: str | None) -> str | None:
        """Location id to measure travel from: the one given, else the remembered current location."""
        if near:
            return self.gd.resolve(near, "location")
        loc = self._info().get("location")
        return loc if loc in self.gd.locations else None

    def _service(self, sid: str) -> dict:
        sv = next((x for x in self.gd.snap.get("services_list") or [] if x["id"] == sid), {"id": sid, "name": sid})
        text = f"{sid} {sv.get('icon', '')}"
        kind = next((k for k in SERVICE_KINDS if k in text), None)
        tier = "advanced" if "advanced" in sid else "basic" if kind in ("kitchen", "loom", "workshop", "trinketry_bench",
                                                                         "sawmill", "forge") else None
        return {"id": sid, "name": sv.get("name", sid), "kind": kind, "tier": tier}

    def _base_distances(self, src: str) -> tuple[dict[str, float], dict[str, tuple[str, dict]]]:
        """Shortest base-step distances from src over legs the character can travel. Gear requirements
        (skis, diving gear, light sources) count as travelable; permits and levels are checked."""
        graph, dist, prev, pq = self._route_graph(), {src: 0}, {}, [(0, src)]
        while pq:
            d, u = heapq.heappop(pq)
            if d > dist[u]:
                continue
            for v, r in graph.get(u, []):
                ctx = self._leg_context(u, r)
                if not check_all([q for q in ctx.activity["requirements"] if q["type"] not in GEAR_DEPENDENT_REQS],
                                 ctx, None):
                    continue
                if d + r["distance"] < dist.get(v, math.inf):
                    dist[v], prev[v] = d + r["distance"], (u, r)
                    heapq.heappush(pq, (dist[v], v))
        return dist, prev

    def find_services(self, service: str, near: str | None = None, top: int = 5) -> dict:
        gd = self.gd
        src = self._near(near)
        if src is None:
            raise ValueError("Where is the player? Pass near, or remember their location with remember_player_info.")
        q = norm(service)
        known = list(self.service_table().values())
        wanted = {sv["id"]: sv for sv in known if sv["kind"] == q or q in norm(sv["name"])}
        if not wanted:
            kinds = sorted({sv["kind"] for sv in known if sv["kind"]})
            raise KeyError(f"No service matching {service!r}. Kinds: {kinds}")
        dist, prev = self._base_distances(src)
        rows = []
        for lid, loc in gd.locations.items():
            here = [wanted[x] for x in loc.get("serviceList") or [] if x in wanted]
            if not here:
                continue
            row = {"location": loc["name"], "services": [
                f"{sv['name']}" + (f" ({sv['tier']})" if sv["tier"] and sv["tier"] not in sv["name"].lower() else "")
                + (f": {sv['attr_text']}" if sv.get("attrs") else "")
                + (f" [needs: {'; '.join(self._req_text(r) for r in sv['requirements'])}]" if sv.get("requirements") else "")
                for sv in here]}
            if lid not in dist:
                row["reachable"] = False
                rows.append((math.inf, row))
                continue
            path, needs, x = [], [], lid
            while x != src:
                u, r = prev[x]
                path.append(gd.locations[x]["name"])
                needs += [self._modifier_label(m) for m in self._leg_modifiers(r, u)]
                x = u
            row["base_steps"] = dist[lid]
            if path:
                row["route"] = " → ".join([gd.locations[src]["name"], *path[::-1]])
            if needs:
                row["route_requires"] = list(dict.fromkeys(needs))
            rows.append((dist[lid], row))
        rows.sort(key=lambda t: t[0])
        return {
            "service": service, "near": gd.locations[src]["name"],
            "locations": [r for _, r in rows[:top]],
            "note": "base_steps is route distance before travel gear; call plan_route for the trip with optimized gear.",
        }

    def plan_route(self, destination: str, start: str | None = None, via: list[str] | None = None,
                   avoid: list[str] | None = None, pet: str | None = "auto", owned_only: bool = True) -> dict:
        gd = self.gd
        src = self._near(start)
        if src is None:
            raise ValueError("Where is the player? Pass start, or remember their location with remember_player_info.")
        stops = [src, *(gd.resolve(x, "location") for x in [*(via or []), destination])]
        avoided = {gd.resolve(x, "location") for x in avoid or []}
        graph = self._route_graph()
        if owned_only and not self._player:
            owned_only = False
        pool = list(self._player.owned_gear.values()) if owned_only else all_gear_pool(gd)
        pets = self._pet_options(pet)
        obj = Objective("actions")  # a double action while travelling covers two of the route's 10 actions
        gear_cache: dict[tuple, tuple] = {}  # (origin, terrain modifiers) -> (loadout, searcher)
        legs_cache: dict[tuple[str, str], tuple | None] = {}
        blocked: dict[str, list[str]] = {}

        def leg(origin: str, route: dict):
            """(steps, ctx, loadout) with the best gear for this leg, or None if the player can't travel it.
            Gear depends on where the leg starts and its terrain, not its length, so it's optimized once per
            (start, terrain) and the steps are evaluated per leg."""
            key = (origin, route["id"])
            if key not in legs_cache:
                ctx = self._leg_context(origin, route)
                reqs = ctx.activity["requirements"]
                static = [r for r in reqs if r["type"] not in GEAR_DEPENDENT_REQS]
                gkey = (origin, tuple(m["id"] for m in self._leg_modifiers(route, origin)))
                if check_all(static, ctx, None) and gkey not in gear_cache:
                    gear_cache[gkey] = optimize(ctx, obj, pool, pets, [None])
                ev = evaluate(ctx, gear_cache[gkey][0], detail=False) if gkey in gear_cache else None
                if ev is None or not ev.valid:
                    legs_cache[key] = None
                    blocked[route["name"]] = [self._modifier_label(m) for m in self._leg_modifiers(route, origin)]
                else:
                    legs_cache[key] = (leg_steps(ev), ctx, gear_cache[gkey][0])
            return legs_cache[key]

        def leg_steps(ev) -> float:
            return round(ev.metrics["steps_per_action"] * 10)

        def best_for(ctx, candidates) -> tuple[float, Loadout] | None:
            """The search is local, so a loadout found for another leg sometimes beats this leg's own."""
            best = None
            for lo in candidates:
                ev = evaluate(ctx, lo, detail=False)
                if ev.valid and (best is None or leg_steps(ev) < best[0]):
                    best = (leg_steps(ev), lo)
            return best

        def shortest(a: str, b: str) -> list[tuple[str, str, dict]]:
            dist, prev, pq = {a: 0}, {}, [(0, a)]
            while pq:
                d, u = heapq.heappop(pq)
                if u == b:
                    break
                if d > dist[u]:
                    continue
                for v, r in graph.get(u, []):
                    if v in avoided and v != b:
                        continue
                    res = leg(u, r)
                    if res and d + res[0] < dist.get(v, math.inf):
                        dist[v], prev[v] = d + res[0], (u, r)
                        heapq.heappush(pq, (dist[v], v))
            if b not in dist:
                raise ValueError(f"No usable route from {gd.locations[a]['name']} to {gd.locations[b]['name']}"
                                 + (f". Blocked legs: {blocked}" if blocked else ""))
            path, x = [], b
            while x != a:
                u, r = prev[x]
                path.append((u, x, r))
                x = u
            return path[::-1]

        path = [hop for a, b in itertools.pairwise(stops) for hop in shortest(a, b)]
        candidates = list({id(lo): lo for *_, lo in (leg(u, r) for u, _, r in path)}.values())
        legs = []
        for u, v, r in path:
            ctx = leg(u, r)[1]
            legs.append((u, v, r, *best_for(ctx, candidates), ctx))

        # one loadout for the whole trip: the candidate that does best across every leg
        best_single = None
        for cand in candidates:
            total = 0
            for *_, ctx in legs:
                ev = evaluate(ctx, cand, detail=False)
                if not ev.valid:
                    break
                total += leg_steps(ev)
            else:
                if best_single is None or total < best_single[0]:
                    best_single = (total, cand)

        def items(lo: Loadout) -> list[str]:
            return [gear_source(gd, oi).label for s, oi in lo.slots.items() if oi]

        name = lambda lid: gd.locations[lid]["name"]
        out: dict = {
            "from": name(stops[0]), "to": name(stops[-1]),
            "base_steps": sum(r["distance"] for _, _, r, *_ in legs),
            "steps_swapping_gear_each_leg": sum(st for _, _, _, st, _, _ in legs),
            "legs": [],
        }
        prev_items = None
        for u, v, r, st, lo, ctx in legs:
            row = {"leg": f"{name(u)} → {name(v)}", "base_steps": r["distance"], "steps": st}
            if mods := self._leg_modifiers(r, u):
                row["requires"] = [self._modifier_label(m) for m in mods]
            cur = items(lo)
            if cur != prev_items:
                row["gear"] = cur
                row["planner_link"] = gearset.encode_link(gd, lo, "travelling")
            prev_items = cur
            out["legs"].append(row)
        if best_single:
            total, lo = best_single
            ctx = legs[0][5]
            # fill free slots with side benefits (chests, tokens...) judged on the first leg
            lo = next(se for lo2, se in gear_cache.values() if lo2 is lo).fill_empty(lo)
            ev = evaluate(ctx, lo)
            out["single_loadout"] = {
                "steps": total,
                "loadout": self._describe_loadout(ctx, lo, ev),
                "pet": f"{gd.pets[lo.pet[0]]['name']} lvl {lo.pet[1]}" if lo.pet else None,
                "planner_link": gearset.encode_link(gd, lo, "travelling"),
            }
            try:
                out["single_loadout"]["gear_set_export"] = gearset.encode(gd, lo)
            except Exception as e:
                out["single_loadout"]["gear_set_export"] = f"(export failed: {e})"
        else:
            out["single_loadout"] = "No single loadout meets every leg's gear requirements; swap gear per leg."
        if blocked:
            out["legs_you_cannot_travel_yet"] = blocked
        out["notes"] = [
            "Steps follow the wiki's travel formula: distance / work efficiency, split into 10 actions, flat step "
            "reductions per action, rounded up, minimum 10 steps per action.",
            "Gear bonuses that depend on location (snowy, in Jarvonia...) are counted at each leg's starting location.",
            "'gear' is shown on a leg only when it changes from the previous leg.",
            "Steps are expected values: double action while travelling covers two of a route's 10 actions.",
        ] + self._context_notes(legs[0][5])
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

    def rank_activities(self, target: str | None = None, top: int = 10, pet: str | None = "current",
                        consumable: str | None = "none", owned_only: bool = True,
                        targets: dict[str, int] | None = None, near: str | None = None,
                        quantity: int | None = None, fine: bool = False) -> dict:
        """Which activity/location gives the target item(s) in the fewest steps with your best owned loadout,
        optionally counting the trip there from `near` (default: the remembered current location)."""
        gd = self.gd
        if targets:
            obj = self._objective("items", None, targets)
            tids = list(obj.targets)
        elif target:
            tid = gd.resolve(target, "item")
            obj, tids = Objective("fine_item" if fine else "item", tid), [tid]
        else:
            raise ValueError("Give a target item, or targets with quantities for several at once")
        pets = self._pet_options(pet)
        consumables = self._consumable_options(consumable)
        srcs = [x for t in tids for x in gd.item_sources.get(t, [])]
        special = any(x["kind"] == "gear_special" for x in srcs)
        acts = [x["id"] for x in srcs if x["kind"] == "activity"]
        if special:
            acts = list(gd.activities)
        pool = list(self._player.owned_gear.values()) if (owned_only and self._player) else all_gear_pool(gd)
        cands, blocked, hidden_until = [], [], {}
        for aid in dict.fromkeys(a for a in acts if a != "travelling"):  # travel steps depend on the route
            for loc in gd.activity_locations(aid) or [None]:
                ctx = self._context(aid, loc)
                if self._player and ctx.skill_levels.get(ctx.main_skill, 0) < ctx.required_level:
                    if not special:  # every activity is a source of "chance to find" items; don't list them all
                        blocked.append((ctx, loc, [f"{ctx.main_skill} lvl {ctx.required_level} "
                                                   f"(you: {ctx.skill_levels.get(ctx.main_skill, 1)})"]))
                    continue
                status, unlock = self._visibility(ctx)
                if status == "emergency" or any(r["type"] == "abilityAvailable"
                                                for r in ctx.activity.get("visibilityRequirements") or []):
                    continue
                if status == "hidden":
                    if not special:
                        blocked.append((ctx, loc, [f"hidden until {x}" for x in unlock]))
                    continue
                if status == "assumed":
                    hidden_until[(ctx.activity["name"], gd.locations[loc]["name"] if loc else None)] = unlock
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
        src = self._near(near)
        dist = self._base_distances(src)[0] if src else {}
        loc_id = {v["name"]: k for k, v in gd.locations.items()}
        per_unit = "steps_to_get_all" if targets else "steps_per_fine_item" if fine else "steps_per_item"

        def row(v, a, l):
            r = {"activity": a, "location": l, per_unit: round(v, 1)}
            if (a, l) in hidden_until:
                r["hidden_activity"] = f"only visible after {'; '.join(hidden_until[(a, l)])} (assumed done)"
            if src:
                r["travel_steps"] = dist.get(loc_id.get(l), math.inf) if l else 0
                if quantity or targets:
                    r["total_steps"] = round(r["travel_steps"] + v * (quantity or 1))
            return r

        ranked = [row(*r) for r in rows]
        if src and (quantity or targets):
            ranked.sort(key=lambda r: r["total_steps"])
        elif src and ranked:  # when a closer source pays off against the fastest one
            best = ranked[0]
            for r in ranked[1:]:
                saved = best["travel_steps"] - r["travel_steps"]
                slower = r[per_unit] - best[per_unit]
                if saved > 0 and slower > 0:
                    r["better_than_fastest_below"] = f"{saved / slower:,.0f} items"
        out = {
            "target": ", ".join(f"{n} {gd.name(i)}" for i, n in obj.targets.items()) if targets
                      else f"{gd.name(tids[0])}{' (fine)' if fine else ''}",
            "ranking": ranked[:top],
            "note": "Each row uses its own optimized loadout; use optimize_loadout on a row for the gear.",
        }
        if src:
            out["from"] = gd.locations[src]["name"]
            out["note"] += (" travel_steps is base route distance from 'from' (plan_route has it with travel gear)"
                            + ("; total_steps adds the farming." if quantity or targets else "."))
        if self._player:
            out["you_have"] = {gd.name(t): self._have(t) for t in tids} if targets else self._have(tids[0])
        if blocked:
            out["blocked_sources"] = [{"activity": c.activity["name"], "location": gd.locations[l]["name"] if l else None,
                                       "unmet": u} for c, l, u in blocked[:10]]
        if not rows:
            out["other_sources"] = [x for t in tids for x in self._sources(t, 10) if not x.startswith("activity:")]
        return out

    # ---------- planning ----------

    def _best_loadout(self, aid: str, obj: Objective, location: str | None = None, pet: str | None = "auto"):
        """(ctx, loadout, evaluation) for the best owned loadout at the activity's best location."""
        pool = list(self._player.owned_gear.values()) if self._player else all_gear_pool(self.gd)
        pets = self._pet_options(pet)
        best = None
        for loc in self._locations_for(aid, location):
            ctx = self._context(aid, loc)
            start, locked = self._start_and_locks(ctx, [], bool(self._player), pets, [None])
            lo, searcher = optimize(ctx, obj, pool, pets, [None], start=start, locked=locked)
            sc = searcher.score(lo)
            if best is None or sc < best[0]:
                best = (sc, ctx, lo)
        _, ctx, lo = best
        ctx.assumed_history.clear()  # report only what the chosen loadout depends on
        return ctx, lo, evaluate(ctx, lo)

    def plan_recipe(self, recipe: str, count: int, near: str | None = None, pet: str | None = "auto") -> dict:
        gd = self.gd
        _, rid = self._resolve_any(recipe, ["recipe"])
        r = gd.recipes[rid]
        out_item, out_n = next(iter((r.get("itemRewards") or {"?": 1}).items()))
        ctx, lo, ev = self._best_loadout(rid, Objective("actions"))
        m = ev.metrics
        per_completion = out_n * (1 + m["double_rewards"])
        completions = math.ceil(count / per_completion)
        steps = round(completions * m["steps_per_action"])  # a double action is a free extra completion
        materials, gather_total = [], 0
        src = self._near(near)
        for group in r.get("materials") or []:
            opts = group["options"]
            rows = []
            for o in opts:
                need = math.ceil(completions * o["amount"] * (1 - m["no_materials_consumed"]))
                have = sum(self._player.item_counts.get(o["item"], (0, 0))) if self._player else 0
                rows.append({"item": gd.name(o["item"]), "id": o["item"], "need": need, "have": have,
                             "short": max(0, need - have)})
            pick = next((x for x in rows if not x["short"]), rows[0])
            if len(rows) > 1:
                pick["alternatives"] = [x["item"] for x in rows if x is not pick]
            if pick["short"]:
                ranked = self.rank_activities(pick["id"], top=1, pet=pet, near=gd.locations[src]["name"] if src else None,
                                              quantity=pick["short"])
                if ranked["ranking"]:
                    best = ranked["ranking"][0]
                    farm = round(best["steps_per_item"] * pick["short"])
                    pick["gather"] = {**best, "steps_for_shortfall": farm}
                    gather_total += farm
                else:
                    pick["gather"] = {"other_sources": ranked.get("other_sources", [])}
            del pick["id"]
            materials.append(pick)
        out = {
            "recipe": r["name"], "makes": f"{count} {gd.name(out_item)}",
            "completions": completions, "crafting_steps": steps, "materials": materials,
            **({"craft_at": f"{ctx.service['name']}, {gd.locations[ctx.location_id]['name']}"} if ctx.service else {}),
            "steps_gathering_shortfall": gather_total, "total_steps": steps + gather_total,
            "loadout": {SLOT_LABELS.get(k, k): gear_source(gd, oi).label for k, oi in lo.slots.items() if oi},
            "planner_link": gearset.encode_link(gd, lo, rid),
            "metrics": {k: m[k] for k in ("steps_per_completion", "double_action", "double_rewards",
                                         "no_materials_consumed")},
        }
        svc = next((q["requirement"] for q in r.get("requirements") or [] if q["type"] == "service"), None)
        if svc and src:
            ok = {sid for sid, sv in self.service_table().items() if self._serves(sv, svc)}
            dist = self._base_distances(src)[0]
            here = sorted((dist.get(lid, math.inf), l["name"]) for lid, l in gd.locations.items()
                          if ok & set(l.get("serviceList") or []))
            if here:
                out["nearest_service"] = {"service": f"{svc.get('serviceKeyword')} ({svc.get('tier')})",
                                          "location": here[0][1], "base_steps": here[0][0]}
        out["notes"] = ["Expected values: double rewards add output, double action halves steps per completion, "
                        "'no materials consumed' saves ingredients.",
                        "Gathering steps exclude travel; rank_activities/plan_route give the trips."]
        return out

    def _quality_name(self, q: str) -> str:
        """'Perfect' / 'legendary' -> 'legendary'."""
        q = q.strip().lower()
        by_name = {v.lower(): k for k, v in QUALITY_NAMES.items()}
        if q in QUALITIES:
            return q
        if q in by_name:
            return by_name[q]
        raise ValueError(f"Unknown quality {q!r}. Qualities: {list(QUALITY_NAMES.values())}")

    def craft_quality(self, recipe: str, quality: str = "Perfect", fine_materials: bool = False,
                      location: str | None = None, pet: str | None = "auto") -> dict:
        gd = self.gd
        _, rid = self._resolve_any(recipe, ["recipe"])
        r = gd.recipes[rid]
        out_item = next(iter(r.get("itemRewards") or {}), None)
        if not out_item or not gd.items.get(out_item, {}).get("gearType") or gd.items[out_item].get("type") != "crafted":
            raise ValueError(f"{r['name']} doesn't make gear that comes in qualities")
        q = self._quality_name(quality)
        probe = self._context(rid, None)
        level_bonus = max(0, probe.skill_levels.get(probe.main_skill, 1) - probe.required_level)
        obj = Objective("quality", q, recipe_level=probe.required_level, level_bonus=level_bonus, fine=fine_materials)
        ctx, lo, ev = self._best_loadout(rid, obj, location, pet)
        m = ev.metrics
        outcome = level_bonus + m["quality_outcome"]
        odds = quality_odds(ctx.required_level, outcome, fine_materials)
        p = at_least(odds, q)
        crafts = 1 / p if p else math.inf
        return {
            "recipe": r["name"], "item": gd.name(out_item), "target": f"{QUALITY_NAMES[q]} or better",
            "fine_materials": fine_materials,
            "quality_outcome": {"total": outcome, "from_level": level_bonus,
                                "from_gear_and_service": m["quality_outcome"]},
            "odds_per_item": {QUALITY_NAMES[k]: f"{v * 100:.3f}%" for k, v in odds.items()},
            "chance_of_target": f"{p * 100:.3f}%",
            "expected_items_crafted": round(crafts, 1),
            "expected_steps": round(m["steps_per_reward_roll"] * crafts) if p else None,
            "materials_expected": {gd.name(o["options"][0]["item"]):
                                   round(o["options"][0]["amount"] * crafts / (1 + m["double_rewards"])
                                         * (1 - m["no_materials_consumed"]))
                                   for o in r.get("materials") or []} if p else None,
            **({"craft_at": f"{ctx.service['name']}, {gd.locations[ctx.location_id]['name']}"} if ctx.service else {}),
            "loadout": {SLOT_LABELS.get(k, k): gear_source(gd, oi).label for k, oi in lo.slots.items() if oi},
            "planner_link": gearset.encode_link(gd, lo, rid),
            "notes": ["Odds follow the wiki's Quality Outcome mechanics with its standard quality weights; the game "
                      "data has no per-recipe weights.",
                      "Fine materials move every roll up one quality." if fine_materials else
                      "Crafting with fine materials moves every roll up one quality (fine_materials=true).",
                      *self._context_notes(ctx)],
        }

    def steps_to_level(self, skill: str, level: int, activity: str | None = None, location: str | None = None,
                       pet: str | None = "auto") -> dict:
        gd, p = self.gd, self.player()
        sk = norm(skill)
        if sk not in gd.skills:
            raise KeyError(f"No skill {skill!r}")
        if not 2 <= level <= len(SKILL_XP):
            raise ValueError(f"Level must be 2-{len(SKILL_XP)}")
        have, need_total = p.skill_xp.get(sk, 0), SKILL_XP[level - 1]
        out = {"skill": sk, "current_level": p.skill_levels.get(sk, 1), "target_level": level,
               "xp_now": have, "xp_needed": max(0, need_total - have)}
        if activity and out["xp_needed"]:
            _, aid = self._resolve_any(activity, ["activity", "recipe"])
            ctx, lo, ev = self._best_loadout(aid, Objective("xp", sk), location, pet)
            xps = ev.metrics["xp_per_step"].get(sk, 0)
            if xps <= 0:
                raise ValueError(f"{ctx.activity['name']} gives no {sk} XP")
            out.update({
                "activity": ctx.activity["name"], "location": gd.locations[ctx.location_id]["name"] if ctx.location_id else None,
                "xp_per_step": round(xps, 4), "steps": math.ceil(out["xp_needed"] / xps),
                "completions": math.ceil(out["xp_needed"] / xps / ev.metrics["steps_per_action"]),
                "loadout": {SLOT_LABELS.get(k, k): gear_source(gd, oi).label for k, oi in lo.slots.items() if oi},
                "planner_link": gearset.encode_link(gd, lo, aid),
            })
            if ctx.is_recipe:
                out["note"] = "For a recipe, completions is how many crafts; plan_recipe gives the materials."
        return out

    def inventory_fill(self, activity: str, free_slots: int, location: str | None = None,
                       inventory: dict[str, int] | None = None, gear_set: str | None = None) -> dict:
        """Steps until `free_slots` more inventory slots are used by the activity's drops."""
        gd = self.gd
        _, aid = self._resolve_any(activity, ["activity", "recipe"])
        loc = self._locations_for(aid, location)[0]
        ctx = self._context(aid, loc)
        lo = gearset.decode(gd, gear_set)[0] if gear_set else player_loadout(self.player())
        ev = evaluate(ctx, lo)
        held = {gd.resolve(k, "item"): int(v) for k, v in (inventory or {}).items()}
        drops = []
        for d in drop_report(ev, 100):
            item = gd.items.get(d["id"], {})
            if item.get("type") in NO_INVENTORY_SLOT or not d["per_1000_steps"]:
                continue
            drops.append((d["id"], d["per_1000_steps"] / 1000, STACK_SIZE.get(item.get("type"), 10)))

        def new_slots(iid: str, rate: float, stack: int, steps: float) -> int:
            h = held.get(iid, 0)  # count whole expected items, so a 5% chest chance doesn't take a slot at once
            return math.ceil((h + math.floor(rate * steps)) / stack) - math.ceil(h / stack)

        def slots_used(steps: float) -> int:
            return sum(new_slots(iid, rate, stack, steps) for iid, rate, stack in drops)

        if not drops or slots_used(10 ** 8) < free_slots:
            raise ValueError("This activity's drops never fill that many slots")
        lo_s, hi_s = 0, 10 ** 8
        while hi_s - lo_s > 1:
            mid = (lo_s + hi_s) // 2
            lo_s, hi_s = (lo_s, mid) if slots_used(mid) >= free_slots else (mid, hi_s)
        steps = hi_s
        return {
            "activity": ctx.activity["name"], "location": gd.locations[loc]["name"] if loc else None,
            "free_slots": free_slots, "steps_until_full": steps,
            "at_that_point": [{"item": gd.name(iid), "gained": round(rate * steps, 1),
                               "new_slots": new_slots(iid, rate, stack, steps)}
                              for iid, rate, stack in sorted(drops, key=lambda x: -x[1]) if rate * steps >= 0.5],
            "notes": ["Expected values with " + ("the given gear set" if gear_set else "the character's equipped gear") + ".",
                      "Stacks: materials 25, consumables 20, everything else 10 (wiki); currencies and collectibles "
                      "take no slot. Pass inventory with current counts so partly filled stacks are counted."],
        }

    def decode_gear_set(self, gear_set: str) -> dict:
        gd = self.gd
        lo, notes = gearset.decode(gd, gear_set)
        return {
            "slots": {SLOT_LABELS.get(s, s): gear_source(gd, oi).label for s, oi in lo.slots.items() if oi},
            "pet": lo.pet, "consumable": lo.consumable, "notes": notes,
        }
