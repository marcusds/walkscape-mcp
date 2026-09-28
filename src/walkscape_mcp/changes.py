"""What changed in the game data between snapshots, kept as a small history for the whats_new tool.

Each time a snapshot is saved over an older one, the items, activities, recipes, locations and pets added, removed
or changed are appended to snapshot/changes.json (newest last).
"""

from __future__ import annotations

import json
import time

from .gamedata import GameData
from .paths import snapshot_dir

KEEP = 30  # snapshots worth of history
LIST_LIMIT = 60  # names per list, so a big update stays readable


def changes_file():
    return snapshot_dir() / "changes.json"


def _collections(gd: GameData) -> dict[str, dict]:
    return {"activities": gd.activities, "recipes": gd.recipes, "items": gd.items, "locations": gd.locations,
            "pets": gd.pets}


def diff_snapshots(old: dict, new: dict) -> dict:
    """{"from", "to", "added": {kind: [names]}, "removed": {...}, "changed": {...}} between two snapshots."""
    a, b = GameData(old), GameData(new)
    out = {"from": a.meta.get("game_version"), "to": b.meta.get("game_version"), "at": time.time(),
           "added": {}, "removed": {}, "changed": {}}
    for kind, coll_b in _collections(b).items():
        coll_a = _collections(a)[kind]
        name = lambda coll, i: (coll[i].get("name") or i) if isinstance(coll[i], dict) else i
        added = sorted(name(coll_b, i) for i in coll_b.keys() - coll_a.keys())
        removed = sorted(name(coll_a, i) for i in coll_a.keys() - coll_b.keys())
        changed = sorted(name(coll_b, i) for i in coll_b.keys() & coll_a.keys()
                         if json.dumps(coll_b[i], sort_keys=True) != json.dumps(coll_a[i], sort_keys=True))
        for key, names in (("added", added), ("removed", removed), ("changed", changed)):
            if names:
                out[key][kind] = names[:LIST_LIMIT] + ([f"... {len(names) - LIST_LIMIT} more"]
                                                      if len(names) > LIST_LIMIT else [])
    return out


def has_changes(d: dict) -> bool:
    return any(d[k] for k in ("added", "removed", "changed"))


def history() -> list[dict]:
    try:
        return json.loads(changes_file().read_text())
    except (FileNotFoundError, ValueError):
        return []


def record(old: dict | None, new: dict) -> dict | None:
    """Append the difference between two snapshots to the history, if there is one."""
    if not old:
        return None
    d = diff_snapshots(old, new)
    if not has_changes(d):
        return None
    h = (history() + [d])[-KEEP:]
    tmp = changes_file().with_suffix(".tmp")
    tmp.write_text(json.dumps(h))
    tmp.replace(changes_file())
    return d
