"""Detect when hand-ported logic may be out of date.

The data refresh (sync.py) keeps numbers current, but some logic was ported by hand from
sources the refresh never looks at:

  - the official planner's JavaScript (step math, level WE, quality stacking, consumables,
    special tables, pet attributes, gear set import, API endpoints)
  - wiki mechanics pages and the XP tables (via the offline ZIM dump)
  - the set of requirement / stat / loot-table types the engine knows how to evaluate
  - the shape of API objects and of the player's save export

References for all of these live in reference/. `walkscape-drift` reports differences;
`walkscape-drift --accept` records the current state once the code has been updated;
`walkscape-drift --init` only creates missing references (the untracked planner JS/wiki copies on a fresh clone).
"""

from __future__ import annotations

import argparse
import difflib
import json
import re
import sys
import time
from pathlib import Path

import httpx

from . import engine
from .paths import player_file
from .player import SAVE_SLOTS
from .sync import USER_AGENT, load_snapshot

REPO = Path(__file__).resolve().parents[2]
REF = REPO / "reference"
PLANNER = "https://gear.walkscape.app"

# (name, regex anchor in the main planner bundle, chars before, chars after). Code we ported mirrors these.
BUNDLE_ANCHORS = [
    ("level_work_efficiency", r"const \w+=\.005,\w+=\.0125,\w+=20;function \w+\(\{playerLevel", 50, 700),
    ("quality_tier_stacking", r"\.includes\(\"consumable\"\)\)return", 120, 900),
    ("consumable_fine_attrs", r"fineAttributes\.map", 250, 150),
    ("special_table_attrs", r"!==\"rollSpecialTable\"\?", 250, 500),
    ("pet_level_attrs", r"if\(\"egg\"in \w+\)", 100, 400),
    ("drop_modifier_stats", r"chestFind:\w+\.value,findCollectibles", 150, 300),
    ("drop_steps_per_item", r"stepsPerRewardRoll/", 200, 200),
    ("gear_set_import", r"async _processGearSetData", 50, 900),
]

WIKI_PAGES = [
    "Work_Efficiency_(Mechanics)", "Steps_Required_(Mechanics)", "Double_Action_(Mechanics)",
    "Double_Rewards_(Mechanics)", "No_Materials_Consumed_(Mechanics)", "Chest_Finding_(Mechanics)",
    "Fine_Material_Finding_(Mechanics)", "Find_Gems_(Mechanics)", "Find_Collectibles_(Mechanics)",
    "Find_Bird_Nests_(Mechanics)", "Roll_Special_Table_(Mechanics)", "Bonus_Experience_(Mechanics)",
    "Skill_Experience", "Character_Level",
]

KEEP_SHORT = {"if", "in", "do", "of", "e", "t"}  # keep a few tokens so normalized code stays readable


def normalize_js(code: str) -> list[str]:
    """Blank out minified identifiers so a rebuild without logic changes produces no diff."""
    code = re.sub(r"(?<![\w$.\"'])[A-Za-z_$][\w$]?(?![\w$])", lambda m: m.group(0) if m.group(0) in KEEP_SHORT else "_", code)
    lines = re.split(r"(?<=[;{}])", code)
    return [l.strip() for l in lines if l.strip()]


class Report:
    def __init__(self):
        self.sections: list[tuple[str, str, str]] = []  # (status, title, detail)

    def ok(self, title, detail=""):
        self.sections.append(("OK", title, detail))

    def drift(self, title, detail):
        self.sections.append(("DRIFT", title, detail))

    def warn(self, title, detail):
        self.sections.append(("WARN", title, detail))

    @property
    def drifted(self) -> bool:
        return any(s == "DRIFT" for s, _, _ in self.sections)

    def render(self) -> str:
        out = []
        for status, title, detail in sorted(self.sections, key=lambda s: ["DRIFT", "WARN", "OK"].index(s[0])):
            out.append(f"[{status}] {title}")
            if detail and status != "OK":
                out.extend("    " + l for l in detail.rstrip().splitlines())
            elif detail:
                out.append("    " + detail)
        return "\n".join(out)


def _diff(old: list[str], new: list[str], name: str, limit: int = 120) -> str:
    d = list(difflib.unified_diff(old, new, f"reference/{name}", f"current/{name}", lineterm="", n=2))
    return "\n".join(d[:limit]) + (f"\n… {len(d) - limit} more diff lines" if len(d) > limit else "")


def _record(path: Path, text: str, accept: str | None):
    """accept="all" overwrites references; "missing" only creates absent ones (fresh clone)."""
    if accept == "all" or (accept == "missing" and not path.exists()):
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text)


def _read_ref(path: Path):
    return json.loads(path.read_text()) if path.exists() else None


# ---------- planner JavaScript ----------

def fetch_planner() -> dict:
    with httpx.Client(timeout=60, headers={"User-Agent": USER_AGENT}, follow_redirects=True) as c:
        index = c.get(PLANNER + "/").text
        bundle_path = re.search(r"/assets/index-[\w-]+\.js", index).group(0)
        bundle = c.get(PLANNER + bundle_path).text
        worker_path = re.search(r"/assets/optimiser\.worker-[\w-]+\.js", bundle).group(0)
        worker = c.get(PLANNER + worker_path).text
    snippets = {}
    for name, pat, before, after in BUNDLE_ANCHORS:
        ms = list(re.finditer(pat, bundle))
        snippets[name] = [bundle[max(0, m.start() - before): m.end() + after] for m in ms]
    endpoints = sorted(set(re.findall(r"url:[\"`]([^\"`]+)[\"`]", bundle)))
    return {"bundle": bundle_path, "worker_path": worker_path, "worker": worker, "snippets": snippets, "endpoints": endpoints}


def check_planner(rep: Report, cur: dict, accept: str | None):
    d = REF / "planner"
    fp = _read_ref(d / "fingerprint.json") or {}
    if fp.get("bundle") == cur["bundle"] and fp.get("worker") == cur["worker_path"]:
        rep.ok("Planner JS unchanged", f"{cur['bundle']}, {cur['worker_path']}")
    else:
        rep.warn("Planner JS rebuilt", f"{fp.get('bundle')} -> {cur['bundle']}\n{fp.get('worker')} -> {cur['worker_path']}\n"
                 "Normalized diffs below show whether logic actually changed.")

    ref_worker = (d / "optimiser.worker.js").read_text() if (d / "optimiser.worker.js").exists() else None
    old, new = normalize_js(ref_worker or ""), normalize_js(cur["worker"])
    if ref_worker is None:
        rep.warn("Optimiser worker has no reference", "Run with --init (fresh clone) or --accept after reviewing.")
    elif old == new:
        rep.ok("Optimiser worker logic (step math, caps, requirement checks, tool slots)")
    else:
        rep.drift("Optimiser worker logic changed -> review engine.compute_metrics / check_requirement, player.tool_slots",
                  _diff(old, new, "optimiser.worker.js"))

    ref_snips = _read_ref(d / "snippets.json")
    if ref_snips is None:
        rep.warn("Planner snippets have no reference", "Run with --init (fresh clone) or --accept after reviewing.")
    for name, *_ in BUNDLE_ANCHORS:
        now = cur["snippets"].get(name, [])
        if not now:
            rep.drift(f"Planner snippet '{name}' not found", "Anchor regex no longer matches; the code may have been "
                      "rewritten. Search the bundle manually and update BUNDLE_ANCHORS in drift.py.")
            continue
        if ref_snips is None:
            continue
        o = [l for s in ref_snips.get(name, []) for l in normalize_js(s)]
        n = [l for s in now for l in normalize_js(s)]
        if o == n:
            rep.ok(f"Planner snippet '{name}'")
        else:
            rep.drift(f"Planner snippet '{name}' changed", _diff(o, n, name))

    ref_ep = _read_ref(d / "endpoints.json") or []
    added, removed = sorted(set(cur["endpoints"]) - set(ref_ep)), sorted(set(ref_ep) - set(cur["endpoints"]))
    if added or removed:
        rep.drift("Planner API endpoints changed -> consider sync.BULK_GETS",
                  f"added: {added}\nremoved: {removed}")
    else:
        rep.ok("Planner API endpoints", f"{len(ref_ep)} endpoints")

    _record(d / "fingerprint.json", json.dumps({"bundle": cur["bundle"], "worker": cur["worker_path"]}, indent=1) + "\n", accept)
    _record(d / "optimiser.worker.js", cur["worker"], accept)
    _record(d / "snippets.json", json.dumps(cur["snippets"], indent=1) + "\n", accept)
    _record(d / "endpoints.json", json.dumps(cur["endpoints"], indent=1) + "\n", accept)


# ---------- wiki mechanics ----------

def build_number(version: str | None) -> int | None:
    m = re.search(r"\+(\d+)", version or "")
    return int(m.group(1)) if m else None


def latest_wiki_build() -> int | None:
    from .wiki import Wiki

    a = Wiki().archive()
    builds = [int(m.group(1)) for i in range(a.all_entry_count)
              if (m := re.fullmatch(r"Versions/(\d+)", a._get_entry_by_id(i).path))]
    return max(builds) if builds else None


def check_wiki(rep: Report, accept: str | None):
    from .wiki import Wiki

    w = Wiki()
    w.update()
    d = REF / "wiki"
    for page in WIKI_PAGES:
        try:
            text = w.page(page, max_chars=100_000)
        except KeyError:
            rep.drift(f"Wiki page {page} missing", "Page was renamed or removed; find its replacement and update WIKI_PAGES.")
            continue
        body = text.split("\n", 2)[-1]  # drop the header with the dump tag
        f = d / f"{page}.txt"
        if f.exists() and f.read_text() == body:
            rep.ok(f"Wiki {page}")
        elif not f.exists():
            rep.warn(f"Wiki {page} has no reference", "Run with --init (fresh clone) or --accept after reviewing.")
        else:
            rep.drift(f"Wiki {page} changed", _diff(f.read_text().splitlines(), body.splitlines(), f.name))
        _record(f, body, accept)

    # latest game build in the wiki vs the data snapshot
    latest = latest_wiki_build()
    snap_ver = (load_snapshot() or {}).get("_meta", {}).get("game_version") or ""
    snap_build = build_number(snap_ver)
    if latest and snap_build and latest > snap_build:
        rep.warn("Game data older than latest game build", f"wiki lists build +{latest}; data snapshot is from {snap_ver}. "
                 "Refresh game data and ask for a new save export.")
    else:
        rep.ok("Game build", f"wiki latest +{latest}, snapshot {snap_ver or 'unknown'}")


# ---------- data vs code ----------

def _walk(o, fn):
    if isinstance(o, dict):
        fn(o)
        for v in o.values():
            _walk(v, fn)
    elif isinstance(o, list):
        for v in o:
            _walk(v, fn)


def data_schema(snap: dict) -> dict:
    req, stats, groups, cats = set(), set(), set(), set()

    def visit(o):
        if "requirement" in o and "opposite" in o and "type" in o:
            req.add(o["type"])
        if isinstance(o.get("stats"), list) and "requirements" in o:  # an attribute
            for s in o["stats"]:
                if isinstance(s, dict) and s.get("type"):
                    stats.add(s["type"])
        if "isPrimary" in o and "rollAmount" in o:
            groups.update(o.get("type") or ["<primary>" if o["isPrimary"] else "<none>"])

    _walk({k: v for k, v in snap.items() if k not in ("stats",)}, visit)
    for t in snap["loot_tables"].values():
        cats.add(t.get("category"))

    def keys(objs):
        return sorted({k for o in objs for k in o})

    items = [i for g in snap["items_categorized"] for c in g["categories"] for i in c["items"] if "type" in i]
    rows = [r for t in snap["loot_tables"].values() for r in t.get("tableRows") or []]
    return {
        "requirement_types": sorted(req),
        "stat_types": sorted(stats),
        "table_group_types": sorted(groups),
        "loot_table_categories": sorted(c for c in cats if c),
        "item_types": sorted({i["type"] for i in items}),
        "gear_types": sorted({i["gearType"] for i in items if i.get("gearType")}),
        "keys": {
            "item": keys(items), "activity": keys(snap["activities"].values()), "recipe": keys(snap["recipes"].values()),
            "pet": keys(snap["pets"].values()), "location": keys(snap["locations"].values()),
            "loot_table": keys(snap["loot_tables"].values()), "loot_row": keys(rows),
            "pet_level": keys(l for p in snap["pets"].values() for l in p.get("levels") or []),
        },
    }


def check_data(rep: Report, accept: str | None):
    snap = load_snapshot()
    if not snap:
        rep.drift("No game data snapshot", "Run `uv run python -m walkscape_mcp.sync` first.")
        return
    cur = data_schema(snap)

    unhandled = set(cur["requirement_types"]) - engine.HANDLED_REQUIREMENT_TYPES - engine.APPROXIMATED_REQUIREMENT_TYPES
    if unhandled:
        rep.drift("Requirement types the engine doesn't evaluate -> engine.check_requirement",
                  "\n".join(sorted(unhandled)) + "\nThey currently fall through to 'assumed satisfied'. Look up examples "
                  "in the snapshot and the planner worker's requirement switch.")
    else:
        rep.ok("All requirement types handled", f"{len(cur['requirement_types'])} types")

    unhandled = set(cur["stat_types"]) - engine.HANDLED_STAT_TYPES - engine.IGNORED_STAT_TYPES
    if unhandled:
        rep.drift("Stat types the engine doesn't use -> engine.compute_metrics / aggregate", "\n".join(sorted(unhandled)))
    else:
        rep.ok("All stat types handled", f"{len(cur['stat_types'])} types")

    known_groups = set(engine.TABLE_MODIFIER) | {"<primary>", "<none>", "petEgg"}
    unhandled = set(cur["table_group_types"]) - known_groups
    if unhandled:
        rep.drift("Loot table group types without a modifier rule -> engine.TABLE_MODIFIER", "\n".join(sorted(unhandled)))
    else:
        rep.ok("All loot table group types handled", ", ".join(cur["table_group_types"]))

    ref = _read_ref(REF / "schema.json")
    if ref is None:
        rep.warn("No schema reference", "Run with --accept after reviewing.")
    else:
        diffs = []
        for k in ("requirement_types", "stat_types", "table_group_types", "loot_table_categories", "item_types", "gear_types"):
            a, r = set(cur[k]) - set(ref.get(k, [])), set(ref.get(k, [])) - set(cur[k])
            if a or r:
                diffs.append(f"{k}: +{sorted(a)} -{sorted(r)}")
        for k, v in cur["keys"].items():
            a, r = set(v) - set(ref["keys"].get(k, [])), set(ref["keys"].get(k, [])) - set(v)
            if a or r:
                diffs.append(f"{k} object keys: +{sorted(a)} -{sorted(r)}")
        if diffs:
            rep.drift("Game data schema changed -> check gamedata.py / engine.py parsing", "\n".join(diffs))
        else:
            rep.ok("Game data schema unchanged")
    _record(REF / "schema.json", json.dumps(cur, indent=1) + "\n", accept)


def check_save(rep: Report, accept: str | None):
    f = player_file()
    if not f.exists():
        rep.warn("No saved character export", "Ask the user to paste a fresh export and call load_player_save.")
        return
    save = json.loads(f.read_text())
    age_days = (time.time() - f.stat().st_mtime) / 86400
    latest = latest_wiki_build()
    if latest and (build_number(save.get("game_version")) or 0) < latest:
        rep.warn("Character export is from an older game build",
                 f"save {save.get('game_version')}, latest +{latest}. Ask the user for a fresh export.")
    elif age_days > 7:
        rep.warn("Character export is over a week old",
                 f"{age_days:.0f} days; gear, levels and pets may have changed. Ask the user for a fresh export.")
    cur = {"top_level": sorted(save), "gear_slots": sorted(save.get("gear") or {}),
           "pet_fields": sorted((save.get("pets") or {}).get("pet") or {})}
    unknown_slots = set(cur["gear_slots"]) - set(SAVE_SLOTS)
    if unknown_slots:
        rep.drift("Save has gear slots the parser doesn't map -> player.SAVE_SLOTS", str(sorted(unknown_slots)))
    ref = _read_ref(REF / "save_format.json")
    if ref and ref != cur:
        rep.drift("Character export format changed -> player.parse_save",
                  "\n".join(f"{k}: +{sorted(set(cur[k]) - set(ref[k]))} -{sorted(set(ref[k]) - set(cur[k]))}"
                            for k in cur if set(cur[k]) != set(ref.get(k, []))))
    elif ref:
        rep.ok("Character export format unchanged", f"save from game {save.get('game_version')}")
    else:
        rep.warn("No save format reference", "Run with --accept after reviewing.")
    _record(REF / "save_format.json", json.dumps(cur, indent=1) + "\n", accept)


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    mode = ap.add_mutually_exclusive_group()
    mode.add_argument("--accept", action="store_true", help="record current state as the new reference")
    mode.add_argument("--init", action="store_true",
                      help="create only missing references (the untracked planner JS and wiki copies after a fresh clone)")
    ap.add_argument("--skip", nargs="*", default=[], choices=["planner", "wiki", "data", "save"])
    args = ap.parse_args(argv)
    accept = "all" if args.accept else "missing" if args.init else None
    rep = Report()
    if "planner" not in args.skip:
        try:
            check_planner(rep, fetch_planner(), accept)
        except Exception as e:
            rep.drift("Could not analyse planner JS", f"{type(e).__name__}: {e}")
    if "wiki" not in args.skip:
        check_wiki(rep, accept)
    if "data" not in args.skip:
        check_data(rep, accept)
    if "save" not in args.skip:
        check_save(rep, accept)
    print(rep.render())
    if args.accept:
        print("\nReferences updated in reference/.")
    elif args.init:
        print("\nMissing references created in reference/; existing ones were left alone, so drift above is real.")
    return 1 if rep.drifted and not args.accept else 0


if __name__ == "__main__":
    sys.exit(main())
