"""Gear set export strings (gzip + base64 JSON), as used by gear.walkscape.app."""

from __future__ import annotations

import asyncio
import base64
import gzip
import json
import re

from .engine import Loadout
from .gamedata import GameData, norm
from .player import OwnedItem
from .sync import API, USER_AGENT

EXPORT_ORDER = [("head", 0), ("cape", 0), ("back", 0), ("chest", 0), ("primary", 0), ("secondary", 0),
                ("hands", 0), ("legs", 0), ("neck", 0), ("feet", 0), ("ring", 0), ("ring", 1),
                *[("tool", i) for i in range(6)], ("pet", 0), ("activityInput", 0)]


def _slot_name(t: str, idx: int) -> str:
    return f"{t}{idx}" if t in ("ring", "tool") else t


def _api_map(ids: list[str], target: str | None = None) -> dict[str, str]:
    import httpx

    async def go():
        async with httpx.AsyncClient(timeout=20, headers={"User-Agent": USER_AGENT}) as c:
            body = {"ids": ids, **({"target": target} if target else {})}
            r = await c.post(f"{API}/items/ids", json=body)
            r.raise_for_status()
            return r.json()

    try:
        return asyncio.run(go())
    except Exception:
        return {}


def _local_guess(gd: GameData, export_id: str) -> str | None:
    m = re.match(r"item-(.+)-[0-9a-f]{8}-[0-9a-f-]+$", export_id)
    if not m:
        return export_id if export_id in gd.items else None
    slug = norm(m.group(1))
    if slug in gd.items:
        return slug
    hits = gd.search(slug, ["item"], 1)
    return hits[0]["id"] if hits and hits[0]["score"] > 0.75 else None


def decode(gd: GameData, s: str) -> tuple[Loadout, list[str]]:
    data = json.loads(gzip.decompress(base64.b64decode(s.strip())))
    raw = []
    for e in data.get("items", []):
        item = json.loads(e["item"]) if isinstance(e.get("item"), str) else e.get("item")
        if item:
            raw.append((e["type"], e["index"], item))
    export_ids = [it["id"] for t, _, it in raw if t not in ("pet", "activityInput")]
    mapping = _api_map(export_ids) if export_ids else {}

    lo, notes = Loadout(), []
    for t, idx, it in raw:
        if t == "pet":
            lvl = (data.get("generic_slots", {}).get("_pet_meta") or {}).get("level") or int(it.get("quality") or 1)
            lo.pet = (it["id"], int(lvl))
            continue
        if t == "activityInput":
            notes.append(f"activity input {it['id']} ignored")
            continue
        item_id = mapping.get(it["id"]) or _local_guess(gd, it["id"])
        if not item_id or item_id not in gd.items:
            notes.append(f"unrecognized item id {it['id']}")
            continue
        q = (it.get("quality") or "").lower()
        if q not in ("common", "uncommon", "rare", "epic", "legendary", "ethereal"):
            q = gd.items[item_id].get("quality") or "common"
        lo.slots[_slot_name(t, idx)] = OwnedItem(item_id, q)
    cons = (data.get("generic_slots") or {}).get("consumable")
    if cons and cons.get("itemId"):
        cid = cons["itemId"]
        fine = cid.endswith("_fine") or bool(cons.get("is_fine"))
        lo.consumable = (cid.removesuffix("_fine"), fine)
    return lo, notes


def encode(gd: GameData, lo: Loadout) -> str:
    ids = sorted({i.id for i in lo.slots.values() if i})
    legacy = _api_map(ids, "old") if ids else {}
    entries = []
    for t, idx in EXPORT_ORDER:
        val = None
        if t == "pet" and lo.pet:
            val = {"id": lo.pet[0], "quality": str(lo.pet[1]), "tag": None}
        elif t not in ("pet", "activityInput"):
            oi = lo.slots.get(_slot_name(t, idx))
            if oi:
                val = {"id": legacy.get(oi.id, oi.id), "quality": oi.quality, "tag": None}
        entries.append({"type": t, "index": idx, "item": json.dumps(val, separators=(",", ":")) if val else "null", "errors": []})
    out: dict = {"items": entries}
    generic: dict = {}
    if lo.pet:
        generic["_pet_meta"] = {"level": lo.pet[1], "variant": "normal", "useAbility": False}
    if lo.consumable:
        cid, fine = lo.consumable
        generic["consumable"] = {
            "itemId": cid + ("_fine" if fine else ""),
            "name": gd.name(cid) + (" (Fine)" if fine else ""),
            "rarity": "fine" if fine else "common",
            "slot": "consumable", "type": "consumable", "is_fine": fine,
        }
    if generic:
        out["generic_slots"] = generic
    return base64.b64encode(gzip.compress(json.dumps(out, separators=(",", ":")).encode())).decode()
