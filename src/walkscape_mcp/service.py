"""Tool implementations, independent of the MCP transport (so they're easy to test)."""

from __future__ import annotations

import dataclasses
import difflib
import fcntl
import functools
import hashlib
import heapq
import itertools
import json
import math
import os
import re
import sqlite3
import threading
import time
from contextlib import contextmanager
from pathlib import Path

from . import gearset, sync, wikidata
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
from .paths import loadout_cache_file, player_file, player_info_file, save_history_dir, snapshot_dir
from .player import SKILL_XP, OwnedItem, Player, parse_save, with_updates
from .quality import at_least, quality_odds
from .wiki import Wiki

STALE_AFTER = 7 * 24 * 3600  # fallback only; a save reporting a new game version triggers a refresh sooner
STAMP_WINDOW = 24 * 3600  # a snapshot this fresh is assumed to match the loaded save's game version
RANK_FULL_SEARCH = 40  # rank_activities runs the full search on at most this many (or 3x top) candidates
ACHIEVEMENT_NOTE = re.compile(r"Unlocked achievement: (?P<name>[^(]+?)\s*(\(.*)?")  # pre-structured notes, migrated
# a service's kind comes from its id/icon (e.g. "sawmill_halfling.png"); recipes require a kind and a tier
SERVICE_KINDS = ("kitchen", "loom", "workshop", "trinketry_bench", "sawmill", "forge", "mailbox", "wardrobe",
                 "mysterious_merchant")
# services whose id and icon don't name their kind (the wiki: smithing and trinketry bonuses)
LOADOUT_CACHE_VERSION = 5  # bump when the optimizer, engine or a memoized planner changes its results


def disk_memo(fn):
    """Keep a method's JSON result on disk per character fingerprint (see Service._fingerprint), for the slow
    planners the achievement plan calls again and again. Results that aren't plain JSON aren't kept."""
    @functools.wraps(fn)
    def wrapper(self, *args, **kwargs):
        db = self._loadout_cache()
        if db is None:
            return fn(self, *args, **kwargs)
        # items being supplied up the call stack cut recursion short, so they're part of the result's key
        busy = sorted(getattr(self, "_supplying", None) or [])
        key = json.dumps([fn.__name__, self._fingerprint(), args, sorted(kwargs.items()), busy], default=str)
        row = db.execute("SELECT value FROM memo WHERE key = ?", (key,)).fetchone()
        if row:
            return json.loads(row[0])
        out = fn(self, *args, **kwargs)
        try:
            value = json.dumps(out)
        except (TypeError, ValueError):
            return out
        with db:
            db.execute("INSERT OR REPLACE INTO memo VALUES (?, ?)", (key, value))
        return out
    return wrapper
SERVICE_KIND_OVERRIDES = {"heatstroke_metalworks": "forge", "granular_faceting_facility": "trinketry_bench"}
STACK_SIZE = {"material": 25, "consumable": 20}  # per the wiki; crafted items, gear and chests stack to 10
NO_INVENTORY_SLOT = {"other", "collectible"}  # currencies (tokens, chips) and collectibles don't take slots
SINCE_SAVE_SECTIONS = ("gear", "skills", "items", "reputation", "points", "carried")
INPUT_CANDIDATES = 2  # input items (e.g. arrow types) costed when getting more; each runs plan_recipe
SLOT_LABELS = {"ring0": "ring 1", "ring1": "ring 2", **{f"tool{i}": f"tool {i + 1}" for i in range(6)}}


class Service:
    _supply_cache: dict | None = None  # see _input_supply
    _supplying: frozenset[str] | set[str] = frozenset()

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

    def whats_new(self, since: int | None = None) -> dict:
        """The game's latest version and change log (the wiki's Versions pages), what changed in the game data at
        the last refreshes, and wiki pages created since the dump that the game data may not have yet."""
        from . import changes

        out: dict = {}
        data_version = self.gd.meta.get("game_version")
        data_build = int(m[1]) if data_version and (m := re.search(r"\+(\d+)", data_version)) else None
        try:
            self.wiki.update()
            versions = self.wiki.page("Versions", 200_000)
        except Exception as e:
            versions, out["wiki_error"] = "", str(e)
        current = re.search(r"current version of the game is (\S+)", versions)
        builds = sorted({int(b) for b in re.findall(r"\+(\d+) Change Log", versions)})
        out["game_version"] = {"latest": current[1] if current else None,
                               "server_data_labelled": data_version,
                               "label_note": "the data's label is the game version of the save loaded when it was "
                                             "fetched, so it can lag; server_has_new_content checks the content"}
        wanted = [b for b in builds if b > since] if since else builds[-1:]
        logs = {}
        for b in wanted[-5:]:
            try:
                text = self.wiki.page(f"Versions/{b}", 30_000)
                logs[f"+{b}"] = text.split("\n", 3)[-1].strip()  # drop the title and source lines
            except KeyError:
                logs[f"+{b}"] = "change log not on the wiki yet"
        out["change_logs"] = logs
        # does the game data have what the latest change log lists as new?
        known = {norm(x.get("name") or k) for coll in (self.gd.items, self.gd.activities, self.gd.recipes,
                                                       self.gd.locations, self.gd.pets)
                 for k, x in coll.items() if isinstance(x, dict)}
        known |= {norm(b["name"]) for b in self.building_table().values()}
        if logs:
            lines = [ln.strip() for ln in list(logs.values())[-1].splitlines()]
            listed, missing = 0, []
            for i, ln in enumerate(lines):
                if m := re.match(r"\((Activities|Items|Locations|Pets|Buildings)\) (\d+) new", ln):
                    for name in lines[i + 1:i + 1 + int(m[2])]:
                        listed += 1
                        if norm(name) not in known:
                            missing.append(name)
            if listed:
                out["server_has_new_content"] = (f"yes: all {listed} new activities, items, locations, pets and "
                                                 "buildings in the latest change log are in the game data"
                                                 if not missing else
                                                 f"not yet: missing {', '.join(missing)} (the planner API may not "
                                                 "have the update; tools don't know about these)")
        hist = changes.history()
        if hist:
            last = hist[-1]
            out["last_data_change"] = {**{k: last[k] for k in ("from", "to", "added", "removed")},
                                       "changed_counts": {k: len(v) for k, v in last["changed"].items()},
                                       "when": time.strftime("%Y-%m-%d %H:%M", time.localtime(last["at"]))}
        try:
            fresh = [t for t in self.wiki.new_pages() if not t.startswith("Versions")]
        except Exception:
            fresh = []
        # a new page about something the game data already has (e.g. "Bake bread (Recipe)") isn't new content
        known = {norm(x.get("name") or k) for coll in (self.gd.items, self.gd.activities, self.gd.recipes,
                                                       self.gd.locations, self.gd.pets)
                 for k, x in coll.items() if isinstance(x, dict)}
        known |= {norm(b["name"]) for b in self.building_table().values()}
        fresh = [t for t in fresh if norm(re.sub(r"\s*\([^)]*\)$", "", t)) not in known]
        if fresh:
            out["new_on_wiki_not_in_data"] = fresh
        return out

    def data_status(self) -> dict:
        gd = self._gd
        return {
            "snapshot_fetched": time.strftime("%Y-%m-%d %H:%M", time.localtime(gd.meta.get("fetched_at", 0))) if gd else None,
            "snapshot_game_version": gd.meta.get("game_version") if gd else None,
            "refresh_running": bool(self._refresh_thread and self._refresh_thread.is_alive()),
            "last_refresh_error": self._refresh_error,
            "wiki_dump": self.wiki.state().get("tag"),
            "wiki_live_pages": len(self.wiki.live_pages()),  # edited since the dump; read from the live wiki
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
        self._record_history(data)
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

    # ---------- save history ----------

    def _record_history(self, data: dict):
        """Keep a copy of each distinct export (the same lifetime step count is the same export)."""
        d = save_history_dir()
        steps = data.get("steps", 0)
        if any(f.stem.endswith(f"-{steps}") for f in d.glob("*.json")):
            return
        stamp = time.strftime("%Y%m%dT%H%M%S")
        (d / f"{stamp}-{steps}.json").write_text(json.dumps(data))

    def _history(self) -> list[tuple[str, dict]]:
        """(load time, export) oldest first, ordered by lifetime steps."""
        out = []
        for f in save_history_dir().glob("*.json"):
            try:
                out.append((f.stem.split("-")[0], json.loads(f.read_text())))
            except (ValueError, OSError):
                continue
        return sorted(out, key=lambda t: t[1].get("steps", 0))

    def compare_saves(self, older: int = -2, newer: int = -1) -> dict:
        """Progress between two stored exports (indexes into the history, oldest first; negatives from the end)."""
        gd, hist = self.gd, self._history()
        listing = [{"index": i, "loaded": f"{t[:4]}-{t[4:6]}-{t[6:8]} {t[9:11]}:{t[11:13]}", "steps": d.get("steps", 0)}
                   for i, (t, d) in enumerate(hist)]
        if len(hist) < 2:
            return {"saves": listing, "note": "Need at least two exports to compare; paste another later."}
        try:
            (t0, a), (t1, b) = hist[older], hist[newer]
        except IndexError:
            raise ValueError(f"No save at that index. Saves: {listing}")
        pa, pb = parse_save(gd, a), parse_save(gd, b)
        la, lb = pa.skill_levels, pb.skill_levels
        skills = {}
        for sk in sorted(set(pa.skill_xp) | set(pb.skill_xp)):
            dx = pb.skill_xp.get(sk, 0) - pa.skill_xp.get(sk, 0)
            if dx:
                skills[sk] = {"xp_gained": dx, "level": f"{la.get(sk, 1)} → {lb.get(sk, 1)}"
                              if la.get(sk, 1) != lb.get(sk, 1) else lb.get(sk, 1)}

        def counts(p):
            """Bank + inventory + equipped, so swapping gear on or off isn't a gain or loss."""
            c = {k: n + f for k, (n, f) in p.item_counts.items()}
            for oi in p.equipped.values():
                c[oi.id] = c.get(oi.id, 0) + 1
            return c

        ca, cb = counts(pa), counts(pb)
        changes = {gd.name(k): cb.get(k, 0) - ca.get(k, 0) for k in set(ca) | set(cb) if cb.get(k, 0) != ca.get(k, 0)}
        gained = sorted(((n, k) for k, n in changes.items() if n > 0), reverse=True)
        spent = sorted((n, k) for k, n in changes.items() if n < 0)
        out = {
            "from": listing[older % len(hist)], "to": listing[newer % len(hist)],
            "steps": pb.steps - pa.steps,
            "character_level": f"{pa.char_level} → {pb.char_level}" if pa.char_level != pb.char_level else pb.char_level,
            "achievement_points": pb.achievement_points - pa.achievement_points,
            "coins": pb.coins - pa.coins,
            "skills": skills,
            "collectibles_found": [gd.name(c) for c in pb.collectibles if c not in pa.collectibles],
            "gear_gained": sorted(gear_source(gd, oi).label for k, oi in pb.owned_gear.items() if k not in pa.owned_gear),
            "items_gained": {k: n for n, k in gained[:25]},
            "items_used_or_lost": {k: n for n, k in spent[:25]},
            "reputation": {f: round(pb.reputation.get(f, 0) - pa.reputation.get(f, 0), 2)
                           for f in set(pa.reputation) | set(pb.reputation)
                           if pb.reputation.get(f, 0) != pa.reputation.get(f, 0)},
            "saves": listing,
            "note": "Item counts are bank + inventory; currencies (tokens, chips) are included.",
        }
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
                "since_save": {k: (data.get("since_save") or {}).get(k) or {} for k in SINCE_SAVE_SECTIONS}}
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
            ("reputation", lambda k, e: save.reputation.get(k, 0) >= e["value"]),
            ("points", lambda k, e: save.achievement_points >= e["value"]),
            ("carried", lambda k, e: False),
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
        if section == "reputation":
            return f"{key.replace('_', ' ').title()} reputation {e['value']:g}"
        if section == "points":
            return f"{e['value']} achievement points"
        if section == "carried":
            return f"carrying {len(e['items'])} gear pieces (equipped + inventory)"
        return f"{e['count']:,} {self.gd.name(key)}"

    def _realms(self) -> dict[str, str]:
        """Region id -> display name, for exploreRealm requirements."""
        out = {}
        for loc in self.gd.locations.values():
            if f := loc.get("faction"):
                out[f] = f.replace("_", " ").title()
        return out

    def _achievement_total(self) -> int | None:
        """Every achievement point in the game (for "50% of all points" requirements), from the wiki's list."""
        if getattr(self, "wiki", None) is None:
            return None
        return sum(a["points"] for a in self.achievement_list().values()) or None

    def _wiki_index(self) -> dict:
        """Wiki facts parsed once per dump and live edit (wikidata.py): services, buildings, achievements. Empty
        sections if the wiki is unavailable."""
        tag = self.wiki.version()
        idx = getattr(self, "_windex", None)
        if idx is None or idx.get("tag") != tag:
            try:
                idx = wikidata.load(self.wiki)
            except Exception:
                idx = {"tag": tag, "services": {}, "buildings": {}, "achievements": {}}
            self._windex = idx
        return idx

    def achievement_list(self) -> dict[str, dict]:
        """Every achievement on the wiki's Achievements page, by name, with its requirements parsed into goals."""
        return self._wiki_index()["achievements"]

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

    def _context(self, activity_id: str, location_id: str | None, carried_only: bool = False) -> Context:
        info = self._info()
        ctx = Context.for_player(self.gd, self._player, activity_id, location_id,
                                 history_met=info["history"], history_not_met=self._not_met,
                                 explored=set(info["explored"]))
        if carried_only and self._player:  # away from a bank: only what's in the inventory now
            ctx.inventory_ids = self._player.inventory_ids
        ctx.achievement_points_total = self._achievement_total()
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
        idx = self._wiki_index()
        if getattr(self, "_services_for", None) is not idx or getattr(self, "_services", None) is None:
            wiki = {norm(k): v for k, v in idx["services"].items()}
            out = {}
            for x in self.gd.snap.get("services_list") or []:
                base = self._service(x["id"])
                w = wiki.get(norm(base["name"]), {})
                out[x["id"]] = {**base, "tier": w.get("tier", base["tier"]), "attrs": w.get("attrs", []),
                                "requirements": w.get("requirements", []), "attr_text": w.get("attr_text", ""),
                                "on_wiki": bool(w)}
            self._services, self._services_for = out, idx
        return self._services

    def building_table(self) -> dict[str, dict]:
        """Building id -> {id, name, types, actions}. The planner data only lists building ids per location; what
        each is (bank, tavern, general store...) and what you can do there (buy, sell, deposit, withdraw) comes from
        the wiki's Buildings page."""
        idx = self._wiki_index()
        if getattr(self, "_buildings_for", None) is not idx or getattr(self, "_buildings", None) is None:
            wiki = {norm(k): {"name": k, **v} for k, v in idx["buildings"].items()}
            out = {}
            for lid, loc in self.gd.locations.items():
                for bid in loc.get("buildingList") or []:
                    w = wiki.get(norm(bid))
                    out[bid] = {"id": bid, "name": w["name"] if w else bid.replace("_", " ").title(),
                                "location": lid, "types": w["types"] if w else [],
                                "actions": w["actions"] if w else {},
                                "requirements": w.get("requirements", []) if w else [],
                                "sells": w.get("sells", []) if w else [], "on_wiki": bool(w)}
            self._buildings, self._buildings_for = out, idx
        return self._buildings

    def shop_sources(self, item_id: str) -> list[dict]:
        """Shops that sell an item: [{"building", "location", "price", "currency", "stock", "entry_met"}]."""
        out = []
        for b in self.building_table().values():
            for x in b["sells"]:
                if norm(x["item"]) == item_id:
                    met = True
                    if b["requirements"] and self._player:
                        met = check_all(b["requirements"], self._context("travelling", None), None)
                    out.append({"building": b["name"], "location": b["location"], "price": x["price"],
                                "currency": x["currency"], "stock": x["stock"], "entry_met": met})
        return out

    def _building_label(self, b: dict) -> str:
        """e.g. "Cold Storage of Commitment (Bank): deposit and withdraw [needs: jarvonia rep 150, NOT MET]"."""
        label = b["name"] + (f" ({', '.join(b['types'])})" if b["types"] else "")
        acts = b["actions"]
        if "Bank" in b["types"] or "Deposit" in acts or "Withdraw" in acts:
            if "Deposit" in acts and "Withdraw" in acts:
                label += ": deposit and withdraw"
            elif "Deposit" in acts:
                label += ": deposit only"
            else:
                label += ": no deposit or withdraw"
        if b["requirements"]:
            needs = "; ".join(self._req_text(r) for r in b["requirements"])
            if self._player:
                ctx = self._context("travelling", None)
                met = check_all(b["requirements"], ctx, None)
                needs += ", NOT MET" if not met else ", assumed met (not in the save)" if ctx.assumed_history else ", met"
            label += f" [entry needs: {needs}]"
        return label

    def _matching_buildings(self, q: str) -> dict[str, dict]:
        """Buildings whose type, action or name fits the query; "bank" also finds outposts you can bank at."""
        out = {}
        for bid, b in self.building_table().items():
            types, acts = [norm(t) for t in b["types"]], [norm(a) for a in b["actions"]]
            if (any(q == t or q in t.split("_") for t in types) or any(q == a or q in a.split("_") for a in acts)
                    or q in norm(b["name"]) or (q == "bank" and ("deposit" in acts or "withdraw" in acts))
                    or (q == "shop" and "buy_items" in acts)):
                out[bid] = b
        return out

    def _recipe_service_req(self, rid: str) -> dict | None:
        return next((r["requirement"] for r in self.gd.recipes[rid].get("requirements") or []
                     if r["type"] == "service"), None)

    @staticmethod
    def _need_keywords(need: dict) -> list[str]:
        """A recipe names its service either as serviceKeyword ("loom") or as keywords (["loom", "cursed"])."""
        return [need["serviceKeyword"]] if need.get("serviceKeyword") else list(need.get("keywords") or [])

    def _need_label(self, need: dict) -> str:
        return f"{' '.join(reversed(self._need_keywords(need)))} ({need.get('tier')})"

    def _serves(self, sv: dict, need: dict) -> bool:
        """A service fits a recipe if it's the right kind and tier, and carries any extra keyword (e.g. cursed) in
        its id. Advanced services are assumed to cover basic recipes too."""
        kws = self._need_keywords(need)
        return (bool(kws) and sv["kind"] == kws[0] and all(k in sv["id"] for k in kws[1:])
                and (need.get("tier") != "advanced" or sv["tier"] == "advanced"))

    def _recipe_service_at(self, rid: str, lid: str) -> dict | None:
        need = self._recipe_service_req(rid)
        if not need:
            return None
        table = self.service_table()
        fits = [table[x] for x in self.gd.locations[lid].get("serviceList") or [] if x in table and self._serves(table[x], need)]
        return fits[0] if fits else None

    def _recipe_locations(self, rid: str) -> list[str]:
        """Locations with a service for the recipe that the character can use, one per distinct setting
        (service, region, location keywords), since otherwise identical kitchens give identical results. The
        nearest of each setting is kept, so the choice between settings can weigh the walk."""
        gd, seen, out = self.gd, set(), []
        src = self._near(None) if self._player else None
        dist = self._base_distances(src)[0] if src else {}
        for lid, loc in sorted(gd.locations.items(), key=lambda kv: dist.get(kv[0], math.inf)):
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
                             location: str | None = None, reputation: dict[str, float] | None = None,
                             achievement_points: int | None = None, carrying: list[str] | None = None) -> dict:
        with self._info_lock():
            info = self._info()
            skipped: list[str] = []  # one unusable entry shouldn't discard the rest of the call
            if location:
                try:
                    info["location"] = self.gd.resolve(location, "location")
                except KeyError as e:
                    skipped.append(e.args[0])
            self._remember_since_save(info, gear_found, skill_levels, item_counts, skipped, reputation,
                                      achievement_points, carrying)
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

    def _remember_since_save(self, info: dict, gear, skills, items, skipped: list[str], reputation=None,
                             points=None, carrying=None):
        since, at = info["since_save"], {"at_steps": self._save.steps if getattr(self, "_save", None) else 0}
        factions = {norm(k): k for k in (self._save.reputation if getattr(self, "_save", None) else {})}
        factions |= {norm(k): k for k in self.gd.reputation_key_to_faction.values()}
        for name, value in (reputation or {}).items():
            if (f := factions.get(norm(name))) is None:
                skipped.append(f"No faction {name!r}. Factions: {sorted(set(factions.values()))}")
                continue
            since["reputation"][f] = {"value": float(value), **at}
        if points is not None:
            since["points"]["total"] = {"value": int(points), **at}
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
            key = f"{iid}@{q}"
            since["gear"][key] = {**at, "count": since["gear"].get(key, {}).get("count", 0) + 1}
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
        # after gear, so gear found in this same call can be carried
        if carrying is not None:
            keys = []
            for spec in carrying:
                try:
                    iid, q = self._parse_item_spec(spec)
                except KeyError as e:
                    skipped.append(e.args[0])
                    continue
                # gear found in this same call counts as owned
                keys_owned = {**{k: oi.id for k, oi in self._player.owned_gear.items()},
                              **{k: k.partition("@")[0] for k in since["gear"]}}
                owned = [k for k, i in keys_owned.items() if i == iid and (not q or k.endswith("@" + q.lower()))]
                if not owned:
                    skipped.append(f"You don't own {spec!r}")
                keys += owned[:1]  # list an item twice to carry two copies (e.g. two of a ring)
            since["carried"]["now"] = {"items": keys, **at}

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
            out["since_last_save"] = [self._since_label(sec, k, e) for sec in SINCE_SAVE_SECTIONS
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

    # ---------- achievement goals ----------

    def _keyword_id(self, name: str | None) -> str | None:
        """"Light source" / "Fish" / "Woodcutting trees" -> a keyword id used by items or activities."""
        if not name:
            return None
        n = norm(name)
        by_name = {norm(k["name"]): kid for kid, k in self.gd.keywords.items()}
        activity_kws = {k for a in self.gd.activities.values() for k in a.get("keywords") or []}
        for cand in (n, n.rstrip("s"), n + "s"):
            if cand in self.gd.keywords or cand in activity_kws:
                return cand
            if cand in by_name:
                return by_name[cand]
        return None

    def _item_or_keyword(self, name: str) -> tuple[str | None, str | None]:
        """A goal names an item ("Gold nugget") or a keyword ("Chest"); (item id, keyword id)."""
        kid = self._keyword_id(name)
        if kid and norm(name) not in self.gd.items:
            return None, kid
        try:
            return self.gd.resolve(name, "item"), None
        except KeyError:
            return None, kid

    def _way(self, aid: str) -> str:
        name = self.gd.activity_like(aid)["name"]
        if not self._player:
            return name
        ok, unmet = self._can_do(aid)
        return name if ok else f"{name} (needs {'; '.join(unmet)})"

    def _ways(self, aids, limit: int = 8) -> list[str]:
        aids = list(dict.fromkeys(aids))
        rows = sorted((self._way(a) for a in aids), key=lambda w: ("(needs" in w, w))
        return rows[:limit] + ([f"... {len(rows) - limit} more"] if len(rows) > limit else [])

    def _goal_view(self, g: dict) -> dict:
        """What advances a parsed achievement goal, and the character's progress when the save shows it."""
        gd, p, t, n = self.gd, self._player, g["type"], g["n"]
        out = {"goal": g["text"], "type": t}
        items_with = lambda kid: [i for i, it in gd.items.items() if kid in (it.get("keywords") or [])]
        droppers = lambda ids: [x["id"] for i in ids for x in gd.item_sources.get(i, []) if x["kind"] == "activity"]
        makers = lambda ids: [x["id"] for i in ids for x in gd.item_sources.get(i, []) if x["kind"] == "recipe"]
        try:
            if t == "actions":
                out["ways"] = self._ways([gd.resolve(g["activity"], "activity")])
            elif t == "actions_keyword":
                kid = self._keyword_id(g["keyword"])
                out["ways"] = self._ways(a for a, x in gd.activities.items() if kid in (x.get("keywords") or []))
            elif t in ("total_steps", "character_level", "skill_level", "all_skills", "wealth") and p:
                have = {"total_steps": p.steps, "character_level": p.char_level, "wealth": p.coins,
                        "skill_level": p.skill_levels.get(g.get("skill"), 1)}.get(t)
                if t == "all_skills":
                    low = {k: v for k, v in p.skill_levels.items() if v < n}
                    out["progress"] = "done" if not low else f"below {n}: " + ", ".join(f"{k} {v}" for k, v in low.items())
                else:
                    out["progress"] = f"{have:,}/{n:,}" + (" (coins only; wealth may count more)" if t == "wealth" else "")
            elif t in ("gain_item", "gain_keyword", "craft_item", "craft_keyword"):
                iid, kid = self._item_or_keyword(g.get("item") or g.get("keyword") or "")
                ids = [iid] if iid else items_with(kid) if kid else []
                if not ids and norm(g.get("keyword") or "") == "material":  # "a fine material": any material
                    ids = [i for i, it in gd.items.items() if it.get("type") == "material"]
                if not ids:
                    return {**out, "unresolved": g.get("item") or g.get("keyword")}
                if t.startswith("gain"):
                    aids = droppers(ids)
                    if skill := g.get("skill"):
                        aids = [a for a in aids if (gd.activities[a].get("relatedSkillsList") or [None])[0] == skill]
                    if akw := self._keyword_id(g.get("activity_keyword")):
                        aids = [a for a in aids if akw in (gd.activities[a].get("keywords") or [])]
                    out["ways"] = self._ways(aids)
                else:
                    out["ways"] = self._ways(makers(ids))
                if p and iid and not g.get("fine"):
                    out["you_have"] = self._have(iid)
            elif t in ("craft_skill", "craft_distinct"):
                rs = [r for r in gd.recipes if (gd.activity_like(r).get("relatedSkillsList") or [None])[0] == g["skill"]]
                doable = [r for r in rs if not p or self._can_do(r)[0]]
                out["ways"] = [f"{len(doable)} of {len(rs)} {g['skill']} recipes are doable now"]
            elif t == "craft_quality":
                kid = self._keyword_id(g.get("keyword"))
                ids = [i for i in (items_with(kid) if kid else gd.items) if gd.items[i].get("type") == "crafted"]
                out["ways"] = self._ways(makers(ids))
                out["tool"] = "craft_quality gives the odds and steps for a recipe at a quality"
            elif t in ("equip_keyword", "hold_distinct"):
                kid = self._keyword_id(g["keyword"])
                ids = items_with(kid) if kid else []
                if p and ids:
                    owned = sorted({gd.name(i) for i in ids if i in p.all_item_ids})
                    out["progress"] = f"{len(owned)}/{n} kinds owned" + (f": {', '.join(owned)}" if owned else "")
                out["ways"] = [f"get_item(\"{gd.keywords.get(kid, {}).get('name', g['keyword'])}\") lists all {len(ids)}"]
            elif t == "stack":
                kid = self._keyword_id(g["keyword"])
                if p and kid:
                    best = max(((p.item_counts.get(i, (0, 0))[0], gd.name(i)) for i in items_with(kid)), default=(0, None))
                    out["progress"] = f"biggest stack {best[0]:,}/{n:,}" + (f" ({best[1]})" if best[1] else "")
                out["ways"] = [f"cheapest_with_keyword(\"{g['keyword']}\", {n}) ranks them"]
            elif t == "have_item":
                iid = gd.resolve(g["item"], "item")
                if p:
                    c = p.item_counts.get(iid, (0, 0))
                    out["progress"] = f"{c[1] if g.get('fine') else sum(c) or int(iid in p.all_item_ids)}/{n}"
                out["ways"] = self._ways(droppers([iid]) + makers([iid]))
            elif t in ("visit", "explore_region") and p:
                region = g.get("region") or ""
                if t == "explore_region":
                    realms = self._realms()
                    done = norm(region) in {norm(x) for r in self._info()["explored"] for x in (r, realms.get(r, r))}
                    out["progress"] = "explored" if done else "not recorded as explored"
        except KeyError as e:
            out["unresolved"] = str(e)
        return out

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
                rows.append({"name": name, **{k: v for k, v in a.items() if k != "goals"}, "status": status,
                             **({"progress": mine["progress"]} if mine.get("progress") else {}),
                             "goals": [self._goal_view(g) for g in a.get("goals") or []]})
        recorded = sum(known[a]["points"] for a, v in ach.items() if v.get("unlocked") and a in known)
        out = {"achievements": rows, "recorded_unlocked_points": recorded}
        if self._player:
            # every collectible found is also worth 1 achievement point (wiki)
            collectibles = len(self._player.collectibles)
            out["collectible_points"] = collectibles
            out["character_achievement_points"] = self._player.achievement_points
            missing = self._player.achievement_points - recorded - collectibles
            if missing > 0:
                out["note"] = (f"{missing} points aren't explained by recorded achievements + collectibles, so some "
                               "unlocked achievements aren't recorded yet. 'not recorded' may still be unlocked; ask.")
            out["collectibles_not_found"] = sum(1 for i in self.gd.items.values() if i.get("type") == "collectible") \
                - collectibles
        unmatched = sorted(a for a in ach if a not in known)
        if unmatched:
            out["recorded_but_not_on_wiki"] = unmatched
        return out

    def plan_achievements(self, targets: list[int] | None = None, only: list[str] | None = None,
                          pet: str | None = "auto", rare_egg_chance: float | None = None) -> dict:
        from .achplan import AchievementPlanner

        known = self.achievement_list()
        if not known:
            raise RuntimeError("Couldn't read the wiki's Achievements page.")
        if only:
            known = {k: known[k] for k in (self._resolve_achievement(n, known) for n in only)}
        p = self.player()
        recorded = self._info()["achievements"]
        unlocked = p.achievement_points
        outer = self._supply_cache is None
        if outer:
            self._supply_cache, self._supplying = {}, set()
        try:
            return AchievementPlanner(self, pet, rare_egg_chance).plan(known, recorded, unlocked, targets or [])
        finally:
            if outer:
                self._supply_cache = None

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
        if norm(name) in {norm(k["name"]) for k in gd.keywords.values()} | set(gd.keywords):
            return self._keyword_info(name)
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

    def _keyword_info(self, name: str) -> dict:
        gd = self.gd
        kid = next(k for k, kw in gd.keywords.items() if norm(name) in (k, norm(kw["name"])))
        items = sorted((i for i, it in gd.items.items() if kid in (it.get("keywords") or [])), key=gd.name)
        out = {"keyword": gd.keywords[kid]["name"], "items": [gd.name(i) for i in items]}
        if self._player:
            owned = {}
            for i in items:
                if gd.is_gear(i) and (quals := self._owned_qualities(i)):
                    owned[gd.name(i)] = quals
                elif not gd.is_gear(i) and i in self._player.item_counts:
                    owned[gd.name(i)] = self._have(i)
            out["you_own"] = owned
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
        for x in self.shop_sources(item_id):
            currency = "Adventurers' Guild tokens" if x["currency"] == "adventurers_guild_token" else "coins"
            srcs.append(f"shop: {x['building']} @ {gd.locations[x['location']]['name']} ({x['price']:,} {currency}"
                        f"{'' if x['entry_met'] else '; entry requirement NOT MET'})")
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
            "buildings": [self._building_label(self.building_table()[b]) for b in loc.get("buildingList") or []],
            **({"job_board": True} if loc.get("jobBoards") else {}),
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

    def _worn(self) -> Loadout | None:
        return player_loadout(self._player) if self._player else None

    def _pool(self, owned_only: bool = True, carried_only: bool = False) -> list[OwnedItem]:
        """Gear to optimize with: everything owned, only what's equipped or in the inventory (away from a bank),
        or every item in the game."""
        if not (owned_only and self._player):
            return all_gear_pool(self.gd)
        p = self._player
        gear, copies = (p.carried_gear, p.carried_copies) if carried_only else (p.owned_gear, p.gear_copies)
        # a second copy of a ring can go in the other ring slot
        return [oi for k, oi in gear.items()
                for _ in range(min(copies.get(k, 1), 2) if self.gd.items[oi.id].get("gearType") == "ring" else 1)]

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
                    raise ValueError(f"{gd.locations[lid]['name']} has no {self._need_label(need)}; "
                                     "find_services lists where to go.")
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
            # two copies of the same ring share a label: each shows its own share of the effects
            copies = sum(1 for x in lo.slots.values() if x and gear_source(gd, x).label == label)
            effects = by_source.get(label, [])
            slots[SLOT_LABELS.get(s, s)] = {"item": label, "active_effects": effects[:len(effects) // copies]}
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

    def _input_options(self, aid: str) -> list[tuple[str, list[str], int]]:
        """What an activity uses up each action: (description, items that fit, how many per action)."""
        gd, out = self.gd, []
        for opt in gd.activity_like(aid).get("options") or []:
            for inp in opt.get("inputs") or []:
                n = inp.get("quantity", 1)
                if inp.get("type") == "specific":
                    ids, need = [inp["item"]], f"{n}x {gd.name(inp['item'])}"
                else:
                    kw = inp.get("keyword")
                    ids = [k for k, i in gd.items.items() if kw in (i.get("keywords") or [])]
                    need = f"one {kw.replace('_', ' ')} item"
                reqs = inp.get("requirements") or []
                ids = [i for i in ids if all(input_fits(gd, i, r) for r in reqs if r["type"] == "inputKeywordWithLevel")]
                if reqs:
                    need += f" ({'; '.join(describe_requirement(r) for r in reqs)})"
                out.append((need, ids, n))
        return out

    def _activity_inputs(self, aid: str) -> list[str]:
        """What an activity uses up each action (arrows, traps, plants...) and what the character has of it."""
        gd, out = self.gd, []
        for need, ids, _ in self._input_options(aid):
            if self._player:
                have = [f"{gd.name(i)} ({self._have(i)})" for i in ids if sum(self._player.item_counts.get(i, (0, 0)))]
                need += f"; you have: {', '.join(have) if have else 'none'}"
            out.append(need)
        return out

    def _farm_rate(self, item_id: str, near: str | None, pet: str | None) -> dict | None:
        """The best activity row for farming an item (steps_per_item doesn't depend on how many), cached for the
        current top-level call so materials shared by many recipes (wheat, milk) are ranked once."""
        key, cache = ("farm", item_id, near, pet), self._supply_cache
        if cache is not None and key in cache:
            return cache[key]
        if not any(x["kind"] == "activity" for x in self.gd.item_sources.get(item_id, [])):
            best = None
        else:
            ranked = self.rank_activities(item_id, top=1, pet=pet, near=near, drops_only=True)["ranking"]
            best = ranked[0] if ranked else None
        if cache is not None:
            cache[key] = best
        return best

    def _supply_steps(self, item_id: str, count: int, near: str | None, pet: str | None) -> dict | None:
        """Cheapest way to get `count` more of an item: farming it (rank_activities) or crafting it (plan_recipe,
        which also farms the missing materials). None if the character can't get it either way."""
        key, cache, busy = (item_id, count, near, pet), self._supply_cache, self._supplying
        if key in cache:
            return cache[key]
        if item_id in busy:  # e.g. an input whose recipe needs a material only an activity using that input drops
            return None
        busy.add(item_id)
        try:
            gd, opts = self.gd, []
            best_farm = self._farm_rate(item_id, near, pet)
            if best_farm:
                opts.append({"how": f"{best_farm['activity']} @ {best_farm['location']}",
                             "steps": round(best_farm["steps_per_item"] * count), "farm": best_farm})
            for src in gd.item_sources.get(item_id, []):
                if src["kind"] != "recipe":
                    continue
                probe = self._context(src["id"], None)
                if self._player and probe.skill_levels.get(probe.main_skill, 1) < probe.required_level:
                    continue
                plan = self.plan_recipe(src["id"], count, near, pet)
                if "total_steps_leaves_out" not in plan:  # a material the character can't get: not an option
                    opts.append({"how": f"craft ({plan['recipe']})", "steps": plan["total_steps"]})
            best = min(opts, key=lambda o: o["steps"]) if opts else None
        finally:
            busy.discard(item_id)
        cache[key] = best
        return best

    def _input_supply(self, aid: str, actions: int, near: str | None = None, pet: str | None = "auto") -> dict:
        """Inputs `actions` actions use up, what the character has, and the steps to get the rest."""
        outer = self._supply_cache is None  # cache only for this call: the character may change between calls
        if outer:
            self._supply_cache, self._supplying = {}, set()
        try:
            return self._input_supply_rows(aid, actions, near, pet)
        finally:
            if outer:
                self._supply_cache = None

    def _input_supply_rows(self, aid: str, actions: int, near: str | None, pet: str | None) -> dict:
        gd, rows, total = self.gd, [], 0
        for need, ids, n in self._input_options(aid):
            want = actions * n
            have = {i: sum(self._player.item_counts.get(i, (0, 0))) for i in ids} if self._player else {}
            short = max(0, want - sum(have.values()))
            row = {"input": need, "need": want, "have": sum(have.values()), "short": short}
            if short:
                # the lowest-level inputs (copper arrows over iron) are the cheap ones; only plan those
                cheap = sorted(ids, key=lambda i: max([self._context(x["id"], None).required_level
                                                       for x in gd.item_sources.get(i, []) if x["kind"] == "recipe"]
                                                      or [0]))[:INPUT_CANDIDATES]
                got = [(g, i) for i in cheap if (g := self._supply_steps(i, short, near, pet))]
                if got:
                    g, i = min(got, key=lambda x: x[0]["steps"])
                    row["get"] = {"item": gd.name(i), "how": g["how"], "steps": g["steps"]}
                    total += g["steps"]
                else:
                    row["get"] = None
            rows.append(row)
        return {"inputs": rows, "steps": total}

    def _context_notes(self, ctx: Context) -> list[str]:
        notes = []
        if inputs := self._activity_inputs(ctx.activity_id):
            notes.append(f"Uses up each action (steps_to_level and rank_activities with a quantity count getting "
                         f"more): {' | '.join(inputs)}.")
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
                         show_missing_upgrades: bool = True, targets: dict[str, int] | None = None,
                         carried_only: bool = False) -> dict:
        gd = self.gd
        _, aid = self._resolve_any(activity, ["activity", "recipe"])
        obj = self._objective(objective, target, targets)
        pets = self._pet_options(pet)
        consumables = self._consumable_options(consumable)
        exclude = {gd.resolve(x, "item") for x in exclude_items or []}
        if owned_only and not self._player:
            owned_only = False
        pool = self._pool(owned_only, carried_only)

        results = []
        for loc in self._locations_for(aid, location):
            ctx = self._context(aid, loc, carried_only)
            start, locked = self._start_and_locks(ctx, require_items or [], owned_only, pets, consumables)
            lo, searcher = optimize(ctx, obj, pool, pets, consumables, start=start, locked=locked, exclude=exclude)
            results.append((searcher.score(lo), loc, ctx, lo, searcher, start))
        results = self._location_order(results, None, None)
        score, loc, ctx, lo, searcher, start = results[0]
        bare = lo
        lo = searcher.complete(searcher.fill_empty(bare), self._worn())
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
            "pool": ("gear you're carrying (equipped + inventory)" if carried_only else "owned gear") if owned_only
                    else "all gear in the game",
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
        kind = next((k for k in SERVICE_KINDS if k in text), None) or next(
            (v for k, v in SERVICE_KIND_OVERRIDES.items() if k in sid), None)
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
        buildings = self._matching_buildings(q)
        job_boards = q in ("job_board", "job_boards", "jobs")
        if not wanted and not buildings and not job_boards:
            kinds = sorted({sv["kind"] for sv in known if sv["kind"]})
            types = sorted({t.lower() for b in self.building_table().values() for t in b["types"]})
            actions = sorted({a.lower() for b in self.building_table().values() for a in b["actions"]})
            raise KeyError(f"No service or building matching {service!r}. Services: {kinds}. Buildings: {types}. "
                           f"Building actions: {actions}. Also: job board.")
        dist, prev = self._base_distances(src)
        rows = []
        for lid, loc in gd.locations.items():
            here = [wanted[x] for x in loc.get("serviceList") or [] if x in wanted]
            here_b = [buildings[x] for x in loc.get("buildingList") or [] if x in buildings]
            boards = loc.get("jobBoards") or [] if job_boards else []
            if not here and not here_b and not boards:
                continue
            row = {"location": loc["name"], "services": [
                f"{sv['name']}" + (f" ({sv['tier']})" if sv["tier"] and sv["tier"] not in sv["name"].lower() else "")
                + (f": {sv['attr_text']}" if sv.get("attrs") else "")
                + (f" [needs: {'; '.join(self._req_text(r) for r in sv['requirements'])}]" if sv.get("requirements") else "")
                for sv in here] + [self._building_label(b) for b in here_b] + ["Job board" for _ in boards]}
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

    @disk_memo
    def plan_route(self, destination: str, start: str | None = None, via: list[str] | None = None,
                   avoid: list[str] | None = None, pet: str | None = "auto", owned_only: bool = True,
                   carried_only: bool = False) -> dict:
        gd = self.gd
        src = self._near(start)
        if src is None:
            raise ValueError("Where is the player? Pass start, or remember their location with remember_player_info.")
        stops = [src, *(gd.resolve(x, "location") for x in [*(via or []), destination])]
        avoided = {gd.resolve(x, "location") for x in avoid or []}
        graph = self._route_graph()
        pool = self._pool(owned_only, carried_only)
        pets = self._pet_options(pet)
        obj = Objective("actions")  # a double action while travelling covers two of the route's 10 actions
        # The best gear per (start, terrain) and each leg's steps depend only on the character and the gear pool,
        # so they're kept across calls until the character changes.
        rc = getattr(self, "_route_cache", None)
        if rc is None or rc["player"] is not self._player:
            rc = self._route_cache = {"player": self._player, "gear": {}, "legs": {}}
        pool_key = (owned_only, carried_only, pet)
        gear_cache: dict[tuple, tuple] = rc["gear"].setdefault(pool_key, {})  # (origin, terrain) -> (loadout, searcher)
        legs_cache: dict[tuple[str, str], tuple | None] = rc["legs"].setdefault(pool_key, {})
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
                    lo, se = optimize(ctx, obj, pool, pets, [None])
                    gear_cache[gkey] = (se.complete(se.fill_empty(lo), self._worn()), se)
                ev = evaluate(ctx, gear_cache[gkey][0], detail=False) if gkey in gear_cache else None
                if ev is None or not ev.valid:
                    legs_cache[key] = None
                else:
                    legs_cache[key] = (leg_steps(ev), ctx, gear_cache[gkey][0])
            if legs_cache[key] is None:
                blocked[route["name"]] = [self._modifier_label(m) for m in self._leg_modifiers(route, origin)]
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
            # candidates are already complete, with free slots holding side benefits (judged on their own leg)
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

    @disk_memo
    def rank_activities(self, target: str | None = None, top: int = 10, pet: str | None = "current",
                        consumable: str | None = "none", owned_only: bool = True,
                        targets: dict[str, int] | None = None, near: str | None = None,
                        quantity: int | None = None, fine: bool = False, carried_only: bool = False,
                        drops_only: bool = False) -> dict:
        """Which activity/location gives the target item(s) in the fewest steps with your best owned loadout,
        optionally counting the trip there from `near` (default: the remembered current location).
        drops_only: skip 'chance to find' gear, which would rank every activity in the game."""
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
        special = not drops_only and any(x["kind"] == "gear_special" for x in srcs)
        acts = [x["id"] for x in srcs if x["kind"] == "activity"]
        direct = set(acts)  # activities that drop it; the rest only through "chance to find" gear
        if special:
            acts = list(gd.activities)
        pool = self._pool(owned_only, carried_only)
        cands, blocked, hidden_until = [], [], {}
        for aid in dict.fromkeys(a for a in acts if a != "travelling"):  # travel steps depend on the route
            for loc in gd.activity_locations(aid) or [None]:
                ctx = self._context(aid, loc, carried_only)
                if self._player and ctx.skill_levels.get(ctx.main_skill, 0) < ctx.required_level:
                    if aid in direct:  # every activity is a source of "chance to find" items; don't list them all
                        blocked.append((ctx, loc, [f"{ctx.main_skill} lvl {ctx.required_level} "
                                                   f"(you: {ctx.skill_levels.get(ctx.main_skill, 1)})"]))
                    continue
                status, unlock = self._visibility(ctx)
                if status == "emergency" or any(r["type"] == "abilityAvailable"
                                                for r in ctx.activity.get("visibilityRequirements") or []):
                    continue
                if status == "hidden":
                    if aid in direct:
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
                rows.append((sc[1], ctx.activity["name"], gd.locations[loc]["name"] if loc else None, ctx, lo))
            elif sc[0] and ctx.activity["id"] in direct:
                blocked.append((ctx, loc, evaluate(ctx, lo).unmet_activity_requirements))
        rows.sort(key=lambda r: r[:3])
        src = self._near(near)
        dist = self._base_distances(src)[0] if src else {}
        loc_id = {v["name"]: k for k, v in gd.locations.items()}
        per_unit = "steps_to_get_all" if targets else "steps_per_fine_item" if fine else "steps_per_item"

        def row(v, a, l, ctx, lo):
            r = {"activity": a, "location": l, per_unit: round(v, 1)}
            if (a, l) in hidden_until:
                r["hidden_activity"] = f"only visible after {'; '.join(hidden_until[(a, l)])} (assumed done)"
            if src:
                r["travel_steps"] = dist.get(loc_id.get(l), math.inf) if l else 0
            if self._input_options(ctx.activity_id):
                actions = v / evaluate(ctx, lo).metrics["steps_per_action"]
                r["actions_per_item" if not targets else "actions"] = round(actions, 2)
                if quantity or targets:
                    sup = self._input_supply(ctx.activity_id, math.ceil(actions * (quantity or 1)),
                                             src, pet)
                    r["inputs"], r["input_steps"] = sup["inputs"], sup["steps"]
                else:
                    r["uses_up_each_action"] = self._activity_inputs(ctx.activity_id)
            if quantity or targets:
                r["total_steps"] = round(r.get("travel_steps", 0) + v * (quantity or 1) + r.get("input_steps", 0))
            return r

        ranked = [row(*r) for r in rows]
        if quantity or targets:
            ranked.sort(key=lambda r: r["total_steps"])
        elif src and ranked:  # when a closer source pays off against the fastest one
            best = ranked[0]
            for r in ranked[1:]:
                saved = best["travel_steps"] - r["travel_steps"]
                slower = r[per_unit] - best[per_unit]
                if saved > 0 and slower > 0 and saved / slower >= 1:
                    r["better_than_fastest_below"] = f"{saved / slower:,.0f} items"
        out = {
            "target": ", ".join(f"{n} {gd.name(i)}" for i, n in obj.targets.items()) if targets
                      else f"{gd.name(tids[0])}{' (fine)' if fine else ''}",
            "ranking": ranked[:top],
            "note": "Each row uses its own optimized loadout; use optimize_loadout on a row for the gear.",
        }
        if src:
            out["from"] = gd.locations[src]["name"]
            out["note"] += " travel_steps is base route distance from 'from' (plan_route has it with travel gear)."
        if quantity or targets:
            out["note"] += (" total_steps adds up the farming" + (", travel" if src else "")
                            + " and getting any inputs (arrows, traps...) beyond what you have.")
        if self._player:
            out["you_have"] = {gd.name(t): self._have(t) for t in tids} if targets else self._have(tids[0])
        if blocked:
            out["blocked_sources"] = [{"activity": c.activity["name"], "location": gd.locations[l]["name"] if l else None,
                                       "unmet": u} for c, l, u in blocked[:10]]
        if not rows:
            out["other_sources"] = [x for t in tids for x in self._sources(t, 10) if not x.startswith("activity:")]
        return out

    # ---------- planning ----------

    def _travel_factor(self) -> float:
        """Steps per base step with the character's best travel gear, from one representative route (cached per
        character)."""
        p, src = self._player, self._near(None)
        # it depends on where the character is, their agility and their gear, not on other skills, so characters
        # with raised levels (the achievement planner) share it
        key = (src, p.skill_xp.get("agility") if p else None, frozenset(p.owned_gear) if p else None)
        cache = self.__dict__.setdefault("_tf_cache", {})
        if key in cache:
            return cache[key]
        factor = 0.5
        if src:
            d = self._base_distances(src)[0]
            dest = min((x for x in d if d[x] > 0), key=lambda x: abs(d[x] - 1500), default=None)
            if dest:
                try:
                    r = self.plan_route(self.gd.locations[dest]["name"], start=self.gd.locations[src]["name"])
                    single = r["single_loadout"]
                    factor = (single["steps"] if isinstance(single, dict) else r["steps_swapping_gear_each_leg"]) \
                        / r["base_steps"]
                except Exception:
                    pass
        cache[key] = factor
        return factor

    def _location_order(self, results: list, near: str | None, actions: float | None) -> list:
        """Sort (score, loc, ...) results: by the objective, with equal scores going to the nearest location; with
        `actions`, by the steps for that many actions plus the walk there, so a small bonus far away loses."""
        src = self._near(near)
        dist = self._base_distances(src)[0] if src and len(results) > 1 else {}
        factor = self._travel_factor() if actions and dist else 0.0

        def key(r):
            sc, loc = r[0], r[1]
            walk = dist.get(loc, math.inf) if loc else 0.0
            if actions and math.isfinite(sc[1]):
                return (sc[0], sc[1] * actions + walk * factor)
            return (sc[0], round(sc[1], 6), walk)
        return sorted(results, key=key)

    def _fingerprint(self) -> str:
        """Hash of everything a best loadout depends on besides the activity and objective: the character
        (levels, gear, pets, reputation, items...), remembered unlocks, the game data and wiki dump, and the
        optimizer's version. Cached per character object."""
        fp = getattr(self, "_fp", None)
        if fp and fp[0] is self._player:
            return fp[1]
        info = self._info() if self._player else {}

        def canon(x):  # sets serialize in hash order, which changes between processes
            return sorted(map(str, x)) if isinstance(x, (set, frozenset)) else str(x)
        blob = json.dumps([dataclasses.asdict(self._player) if self._player else None,
                           info.get("history"), info.get("explored"), info.get("location"),
                           sorted(self._not_met.items()),
                           self.gd.meta, self.wiki.version() if getattr(self, "wiki", None) else None,
                           LOADOUT_CACHE_VERSION],
                          sort_keys=True, default=canon)
        h = hashlib.sha1(blob.encode()).hexdigest()
        self._fp = (self._player, h)
        return h

    def _loadout_cache(self):
        if os.environ.get("WALKSCAPE_LOADOUT_CACHE") == "0":
            return None
        db = getattr(self, "_lc_db", None)
        if db is None:
            db = sqlite3.connect(loadout_cache_file(), timeout=10, check_same_thread=False)
            db.execute("CREATE TABLE IF NOT EXISTS best (key TEXT PRIMARY KEY, value TEXT)")
            db.execute("CREATE TABLE IF NOT EXISTS memo (key TEXT PRIMARY KEY, value TEXT)")
            self._lc_db = db
        return db

    def _best_loadout(self, aid: str, obj: Objective, location: str | None = None, pet: str | None = "auto",
                      carried_only: bool = False, near: str | None = None, actions: float | None = None):
        """(ctx, loadout, evaluation) for the best owned loadout at the activity's best location: equal ones go
        to the nearest, and with `actions` the walk there is weighed against the steps for that many actions.
        Results are kept on disk per character fingerprint, so repeating a plan for an unchanged character only
        re-evaluates."""
        db = self._loadout_cache()
        key = json.dumps([self._fingerprint(), aid, obj.kind, obj.target, sorted((obj.targets or {}).items()),
                          obj.recipe_level, obj.level_bonus, obj.fine, location, pet, carried_only, near,
                          round(actions) if actions else None])
        if db is not None:
            row = db.execute("SELECT value FROM best WHERE key = ?", (key,)).fetchone()
            if row:
                v = json.loads(row[0])
                lo = Loadout({k: OwnedItem(*x) if x else None for k, x in v["slots"].items()},
                             tuple(v["pet"]) if v["pet"] else None,
                             tuple(v["consumable"]) if v["consumable"] else None)
                ctx = self._context(aid, v["loc"], carried_only)
                return ctx, lo, evaluate(ctx, lo)
        ctx, lo, ev = self._search_best_loadout(aid, obj, location, pet, carried_only, near, actions)
        if db is not None:
            v = {"loc": ctx.location_id, "slots": {k: [o.id, o.quality] if o else None for k, o in lo.slots.items()},
                 "pet": list(lo.pet) if lo.pet else None, "consumable": list(lo.consumable) if lo.consumable else None}
            with db:
                db.execute("INSERT OR REPLACE INTO best VALUES (?, ?)", (key, json.dumps(v)))
        return ctx, lo, ev

    def _search_best_loadout(self, aid: str, obj: Objective, location: str | None, pet: str | None,
                             carried_only: bool, near: str | None = None, actions: float | None = None):
        pool = self._pool(True, carried_only)
        pets = self._pet_options(pet)
        results = []
        for loc in self._locations_for(aid, location):
            ctx = self._context(aid, loc, carried_only)
            start, locked = self._start_and_locks(ctx, [], bool(self._player), pets, [None])
            lo, searcher = optimize(ctx, obj, pool, pets, [None], start=start, locked=locked)
            results.append((searcher.score(lo), loc, ctx, lo, searcher))
        _, _, ctx, lo, searcher = self._location_order(results, near, actions)[0]
        lo = searcher.complete(searcher.fill_empty(lo), self._worn())
        ctx.assumed_history.clear()  # report only what the chosen loadout depends on
        return ctx, lo, evaluate(ctx, lo)

    # ---------- listing and comparing ----------

    def _matching_activities(self, skill: str | None, keyword: str | None, makes: str | None, kind: str,
                             names: list[str] | None = None) -> list[str]:
        """Activity/recipe ids by main skill, activity keyword (e.g. woodcutting_trees, cooking_recipe) or a
        keyword of an item a recipe makes (e.g. food)."""
        gd = self.gd
        if names:
            return [self._resolve_any(n, ["activity", "recipe"])[1] for n in names]
        if not (skill or keyword or makes):
            raise ValueError("Pass skill, keyword or makes (or names).")
        pools = {"activity": gd.activities, "recipe": gd.recipes}
        kinds = ["activity", "recipe"] if kind == "both" else [kind]
        if any(k not in pools for k in kinds):
            raise ValueError("kind is activity, recipe or both")
        sk, kw, mk = (norm(x) if x else None for x in (skill, keyword, makes))
        out = []
        for k in kinds:
            for aid, a in pools[k].items():
                a = gd.activity_like(aid)
                if sk and (a.get("relatedSkillsList") or [None])[0] != sk:
                    continue
                if kw and not any(kw == norm(x) or kw in norm(x).split("_") for x in a.get("keywords") or []):
                    continue
                if mk and not any(mk in (gd.items.get(i, {}).get("keywords") or []) for i in a.get("itemRewards") or {}):
                    continue
                out.append(aid)
        return out

    def _can_do(self, aid: str) -> tuple[bool, list[str]]:
        """Whether the character meets the level and other non-gear requirements, and which they miss."""
        ctx = self._context(aid, None)
        reqs = [r for r in ctx.activity.get("requirements") or []
                if r["type"] not in GEAR_DEPENDENT_REQS and r["type"] != "service"]
        unmet = [self._req_text(r) for r in reqs if not check_all([r], ctx, None)]
        return not unmet, unmet

    def list_activities(self, skill: str | None = None, keyword: str | None = None, makes: str | None = None,
                        kind: str = "both", doable_only: bool = False) -> dict:
        gd, rows = self.gd, []
        for aid in self._matching_activities(skill, keyword, makes, kind):
            a = gd.activity_like(aid)
            ctx = self._context(aid, None)
            ok, unmet = self._can_do(aid) if self._player else (True, [])
            if doable_only and not ok:
                continue
            row = {"name": a["name"], "kind": "recipe" if aid in gd.recipes else "activity",
                   "skill": ctx.main_skill, "level": ctx.required_level}
            if aid in gd.recipes:
                need = self._recipe_service_req(aid)
                row["makes"] = [gd.name(i) for i in a.get("itemRewards") or {}]
                if need:
                    row["service"] = self._need_label(need)
            else:
                row["locations"] = [gd.locations[l]["name"] for l in gd.activity_locations(aid)]
            if self._player:
                row["can_do"] = ok
                if unmet:
                    row["missing"] = unmet
            rows.append(row)
        rows.sort(key=lambda r: (r["skill"] or "", r["level"], r["name"]))
        return {"count": len(rows), "activities": rows}

    def compare_activities(self, names: list[str] | None = None, skill: str | None = None, keyword: str | None = None,
                           makes: str | None = None, objective: str = "actions", target: str | None = None,
                           count: int | None = None, kind: str = "both", pet: str | None = "auto",
                           top: int = 10) -> dict:
        gd = self.gd
        obj = self._objective(objective, target)
        ids = self._matching_activities(skill, keyword, makes, kind, names)
        rows, skipped = [], []
        for aid in ids:
            name = gd.activity_like(aid)["name"]
            ok, unmet = self._can_do(aid) if self._player else (True, [])
            if not ok:
                skipped.append({"name": name, "missing": unmet})
                continue
            ctx, lo, ev = self._best_loadout(aid, obj, pet=pet)
            v = obj.value(ev)
            if math.isinf(v):
                skipped.append({"name": name, "missing": ["the objective can't be met here with your gear"]})
                continue
            row = {"name": name, "result": obj.describe(v),
                   "location": gd.locations[ctx.location_id]["name"] if ctx.location_id else None}
            if count and obj.kind not in ("xp", "total_xp"):
                row["steps_for_count"] = round(v * count)
            row["planner_link"] = gearset.encode_link(gd, lo, aid)
            rows.append((v, row))
        rows.sort(key=lambda t: t[0])
        return {"objective": obj.kind + (f" ({target})" if target else ""),
                "ranking": [r for _, r in rows[:top]], "not_doable": skipped,
                "note": "Each row uses its own best owned loadout; optimize_loadout on a row gives the full gear."}

    @disk_memo
    def cheapest_with_keyword(self, keyword: str, quantity: int, near: str | None = None, pet: str | None = "auto",
                              top: int = 5) -> dict:
        """For goals like "a stack of 1,000 of any food": each item with the keyword, how many the character has,
        and the steps to get the rest by the cheaper of farming or crafting."""
        gd, p = self.gd, self._player
        kw = norm(keyword)
        items = [i for i, it in gd.items.items() if kw in (it.get("keywords") or [])]
        if not items:
            raise KeyError(f"No items with keyword {keyword!r}")
        src = self._near(near)
        near_name = gd.locations[src]["name"] if src else None
        rows, cannot = [], []
        outer = self._supply_cache is None
        if outer:
            self._supply_cache, self._supplying = {}, set()
        try:
            for i in items:
                have = p.item_counts.get(i, (0, 0))[0] if p else 0
                short = max(0, quantity - have)
                how = self._supply_steps(i, short, near_name, pet) if short else {"how": "already have them", "steps": 0}
                if how is None:
                    cannot.append(gd.name(i))
                    continue
                rows.append((how["steps"], {"item": gd.name(i), "have": have, "short": short,
                                            "how": how["how"], "steps": how["steps"]}))
        finally:
            if outer:
                self._supply_cache = None
        rows.sort(key=lambda t: t[0])
        return {"keyword": keyword, "quantity": quantity, "ranking": [r for _, r in rows[:top]],
                "cannot_get_yet": sorted(cannot),
                "note": "Stock counts only normal-quality items (fine ones stack separately). Steps leave out travel; "
                        "plan_recipe or rank_activities on the winner give the trip and loadout."}

    @disk_memo
    def plan_recipe(self, recipe: str, count: int, near: str | None = None, pet: str | None = "auto") -> dict:
        outer = self._supply_cache is None  # _supply_steps may plan sub-recipes; share one cache per top-level call
        if outer:
            self._supply_cache, self._supplying = {}, set()
        try:
            return self._plan_recipe(recipe, count, near, pet)
        finally:
            if outer:
                self._supply_cache = None

    def _plan_recipe(self, recipe: str, count: int, near: str | None, pet: str | None) -> dict:
        gd = self.gd
        _, rid = self._resolve_any(recipe, ["recipe"])
        r = gd.recipes[rid]
        out_item, out_n = next(iter((r.get("itemRewards") or {"?": 1}).items()))
        ctx, lo, ev = self._best_loadout(rid, Objective("actions"), near=near, actions=count)
        m = ev.metrics
        per_completion = out_n * (1 + m["double_rewards"])
        completions = math.ceil(count / per_completion)
        steps = round(completions * m["steps_per_action"])  # a double action is a free extra completion
        materials, gather_total, missing = [], 0, []
        src = self._near(near)
        near_name = gd.locations[src]["name"] if src else None
        for group in r.get("materials") or []:
            opts = group["options"]
            rows = []
            for o in opts:
                need = math.ceil(completions * o["amount"] * (1 - m["no_materials_consumed"]))
                have = sum(self._player.item_counts.get(o["item"], (0, 0))) if self._player else 0
                rows.append({"item": gd.name(o["item"]), "id": o["item"], "need": need, "have": have,
                             "short": max(0, need - have)})
            pick = next((x for x in rows if not x["short"]), None)
            combined = []
            if pick is None and len(rows) > 1:
                # no single option covers it: use up what's owned of each (e.g. silver ore, then nuggets), and
                # only the crafts still uncovered need one option farmed or crafted
                crafts = math.ceil(completions * (1 - m["no_materials_consumed"]))
                amount = {x["id"]: o["amount"] for x, o in zip(rows, opts)}
                left = crafts
                for x in sorted(rows, key=lambda x: -(x["have"] // amount[x["id"]])):
                    k = min(left, x["have"] // amount[x["id"]])
                    if k:
                        combined.append({"item": x["item"], "id": x["id"], "uses": k * amount[x["id"]],
                                         "crafts": k})
                        left -= k
                if combined and left == 0:
                    pick = {**next(x for x in rows if x["id"] == combined[0]["id"]),
                            "need": combined[0]["uses"], "short": 0}
                    combined = combined[1:]
                elif combined:
                    used = {c["id"]: c["uses"] for c in combined}
                    rows = [{**x, "need": left * amount[x["id"]] + used.get(x["id"], 0),
                             "short": max(0, left * amount[x["id"]] - (x["have"] - used.get(x["id"], 0)))}
                            for x in rows]
            if pick is None:  # every option is short: take the cheapest one to farm or craft
                supply = {x["item"]: self._supply_steps(x["id"], x["short"], near_name, pet) for x in rows}
                pick = min(rows, key=lambda x: supply[x["item"]]["steps"] if supply[x["item"]] else math.inf)
                how = supply[pick["item"]]
                if how is None:
                    blocked = self.rank_activities(pick["id"], top=1).get("blocked_sources", []) if any(
                        x["kind"] == "activity" for x in gd.item_sources.get(pick["id"], [])) else []
                    pick["gather"] = {"cannot_get": "the character can't do any activity that drops it or make it yet",
                                      **({"blocked_activities": [f"{b['activity']} @ {b['location']}: "
                                                                 f"{'; '.join(b['unmet'])}" for b in blocked]}
                                         if blocked else {}),
                                      "other_sources": self._sources(pick["id"], 10)}
                    missing.append(pick["item"])
                elif how["how"].startswith("craft"):
                    pick["gather"] = {"craft": how["how"][7:-1], "steps_for_shortfall": how["steps"]}
                    gather_total += how["steps"]
                else:
                    best = how["farm"]
                    if not self._supplying:  # top-level plan: rank again for this quantity, counting travel
                        best = self.rank_activities(pick["id"], top=1, pet=pet, near=near_name,
                                                    quantity=pick["short"])["ranking"][0]
                    farm = round(best["steps_per_item"] * pick["short"])
                    pick["gather"] = {**best, "steps_for_shortfall": farm}
                    gather_total += farm
            combined = [c for c in combined if c["id"] != pick.get("id")]
            if combined:
                pick["plus_owned"] = [{"item": c["item"], "uses": c["uses"], "for_crafts": c["crafts"]}
                                      for c in combined]
            if len(rows) > 1:
                pick["alternatives"] = [x["item"] for x in rows if x["id"] != pick["id"]]
            del pick["id"]
            materials.append(pick)
        out = {
            "recipe": r["name"], "makes": f"{count} {gd.name(out_item)}",
            "completions": completions, "crafting_steps": steps, "materials": materials,
            **({"craft_at": f"{ctx.service['name']}, {gd.locations[ctx.location_id]['name']}"} if ctx.service else {}),
            "steps_gathering_shortfall": gather_total, "total_steps": steps + gather_total,
            **({"total_steps_leaves_out": f"{', '.join(missing)}: the character can't farm or craft it yet "
                                          "(see its blocked_activities), so the plan can't be completed as is"}
               if missing else {}),
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
                out["nearest_service"] = {"service": self._need_label(svc),
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
            unmet = [self._req_text(r) for r in ctx.activity.get("requirements") or []
                     if r["type"] not in GEAR_DEPENDENT_REQS and r["type"] != "service"
                     and not check_requirement(r, ctx, None)]
            if unmet:
                out["cannot_do_yet"] = (f"{ctx.activity['name']} needs {'; '.join(unmet)}. The steps above assume you "
                                        "could do it now; train on something you meet first.")
            if self._input_options(aid):
                sup = self._input_supply(aid, out["completions"], pet=pet)
                out["inputs"] = sup["inputs"]
                out["steps_getting_inputs"] = sup["steps"]
                out["total_steps"] = out["steps"] + sup["steps"]
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
