"""Parse the in-game "export character data" JSON."""

from __future__ import annotations

import json
import math
from dataclasses import dataclass, field, replace

from .gamedata import QUALITIES, GameData


def _xp_equate(level: int) -> int:
    return math.floor(level + 300 * 2 ** (level / 7))


def _cumulative(n: int) -> int:
    return sum(_xp_equate(i) for i in range(1, n + 1))


SKILL_XP = [0] + [math.floor(_cumulative(l - 1) / 4) for l in range(2, 100)]  # SKILL_XP[i] = xp for level i+1
CHAR_STEPS = [0] + [math.floor(_cumulative(l - 1) / 4) * 4.6 for l in range(2, 100)]


def skill_level(xp: float) -> int:
    return sum(1 for t in SKILL_XP if xp >= t)


def character_level(steps: float) -> int:
    return sum(1 for t in CHAR_STEPS if steps >= t)


def tool_slots(char_level: int) -> int:
    return 6 if char_level >= 80 else 5 if char_level >= 50 else 4 if char_level >= 20 else 3


SAVE_SLOTS = {
    "head": ("head", 0), "cape": ("cape", 0), "back": ("back", 0), "chest": ("chest", 0),
    "primary": ("primary", 0), "secondary": ("secondary", 0), "hands": ("hands", 0),
    "legs": ("legs", 0), "neck": ("neck", 0), "feet": ("feet", 0),
    "ring_1": ("ring", 0), "ring_2": ("ring", 1),
    **{f"tool_{i}": ("tool", i - 1) for i in range(1, 7)},
}


@dataclass(frozen=True)
class OwnedItem:
    id: str
    quality: str

    def key(self) -> str:
        return f"{self.id}@{self.quality}"


def split_quality(gd: GameData, raw: str) -> tuple[str, str | None, bool]:
    """'farganite_pickaxe_common' -> ('farganite_pickaxe', 'common', False); 'coal_fine' -> ('coal', None, True)."""
    if raw.endswith("_fine") and raw[:-5] in gd.items:
        return raw[:-5], None, True
    for q in QUALITIES:
        if raw.endswith("_" + q) and raw[: -len(q) - 1] in gd.items:
            return raw[: -len(q) - 1], q, False
    return raw, None, False


@dataclass
class Player:
    name: str
    game_version: str
    steps: int
    achievement_points: int
    coins: int
    skill_xp: dict[str, int]
    equipped: dict[str, OwnedItem]  # slot key like "tool_1" -> item
    owned_gear: dict[str, OwnedItem]  # key -> item (deduped by id+quality)
    all_item_ids: set[str]
    collectibles: list[str]
    consumables: dict[str, int]  # "dried_fruit" / "dried_fruit_fine" -> count
    pets: list[dict]  # {"name","species","level","equipped"}
    reputation: dict[str, float]
    unknown_ids: list[str] = field(default_factory=list)
    item_counts: dict[str, tuple[int, int]] = field(default_factory=dict)  # item id -> (normal, fine) in bank + inventory
    carried_gear: dict[str, OwnedItem] = field(default_factory=dict)  # equipped + inventory (not the bank)

    @property
    def skill_levels(self) -> dict[str, int]:
        return {s: skill_level(x) for s, x in self.skill_xp.items()}

    @property
    def char_level(self) -> int:
        return character_level(self.steps)

    def summary(self, gd: GameData) -> dict:
        return {
            "name": self.name,
            "game_version": self.game_version,
            "character_level": self.char_level,
            "tool_slots": tool_slots(self.char_level),
            "skills": self.skill_levels,
            "achievement_points": self.achievement_points,
            "equipped": {s: f"{gd.name(i.id)} ({i.quality})" for s, i in self.equipped.items()},
            "pets": self.pets,
            "collectibles": [gd.name(c) for c in self.collectibles],
            "consumables_owned": {gd.name(k.removesuffix("_fine")) + (" (fine)" if k.endswith("_fine") else ""): v for k, v in self.consumables.items() if v},
            "owned_gear_count": len(self.owned_gear),
            "reputation": self.reputation,
            "unrecognized_ids": self.unknown_ids,
        }


def parse_save(gd: GameData, save: dict | str) -> Player:
    if isinstance(save, str):
        save = json.loads(save)

    owned: dict[str, OwnedItem] = {}
    all_ids: set[str] = set()
    unknown: list[str] = []
    consumables: dict[str, int] = {}
    counts: dict[str, list[int]] = {}

    def add(raw: str, count: int = 1):
        item_id, q, fine = split_quality(gd, raw)
        if item_id not in gd.items:
            unknown.append(raw)
            return None
        all_ids.add(item_id)
        item = gd.items[item_id]
        if item.get("type") == "consumable" and count:
            consumables[raw] = consumables.get(raw, 0) + count
        if not item.get("gearType"):
            return None
        oi = OwnedItem(item_id, q or item.get("quality") or "common")
        owned[oi.key()] = oi
        return oi

    equipped, carried = {}, {}
    for slot, raw in (save.get("gear") or {}).items():
        if raw:
            oi = add(raw)
            if oi:
                equipped[slot] = carried[oi.key()] = oi
    for src in ("inventory", "bank"):
        for raw, n in (save.get(src) or {}).items():
            if n:
                oi = add(raw, n)
                if oi and src == "inventory":
                    carried[oi.key()] = oi
                item_id, _, fine = split_quality(gd, raw)
                counts.setdefault(item_id, [0, 0])[fine] += n
    for raw, n in (save.get("consumables") or {}).items():
        add(raw, n)
    for raw, n in (save.get("currencies") or {}).items():  # e.g. Adventurers' Guild tokens, chips
        if n and raw in gd.items:
            all_ids.add(raw)
            counts.setdefault(raw, [0, 0])[0] += n

    pets = []
    if (save.get("pets") or {}).get("pet"):
        p = save["pets"]["pet"]
        pets.append({**p, "equipped": True})
    for p in save.get("available_pets") or []:
        pets.append({**p, "equipped": False})

    return Player(
        name=save.get("name", "?"),
        game_version=save.get("game_version", ""),
        steps=save.get("steps", 0),
        achievement_points=save.get("achievement_points", 0),
        coins=save.get("coins", 0),
        skill_xp=save.get("skills") or {},
        equipped=equipped,
        owned_gear=owned,
        all_item_ids=all_ids | set(save.get("collectibles") or []),
        collectibles=list(save.get("collectibles") or []),
        consumables=consumables,
        pets=pets,
        reputation=save.get("reputation") or {},
        unknown_ids=sorted(set(unknown)),
        item_counts={k: (v[0], v[1]) for k, v in counts.items()},
        carried_gear=carried,
    )


def with_updates(gd: GameData, player: Player, since: dict) -> Player:
    """Apply what the user reported after exporting the save: gear found, skill levels, item counts.
    `since` is player_info's "since_save" section; entries the save already covers were pruned on load."""
    owned, ids, xp, counts = dict(player.owned_gear), set(player.all_item_ids), dict(player.skill_xp), dict(player.item_counts)
    reputation = {**player.reputation, **{f: e["value"] for f, e in (since.get("reputation") or {}).items()}}
    points = ((since.get("points") or {}).get("total") or {}).get("value", player.achievement_points)
    carried = dict(player.carried_gear)
    for key in since.get("gear") or {}:
        item_id, _, quality = key.partition("@")
        if item_id in gd.items:
            owned[key] = carried[key] = OwnedItem(item_id, quality)  # found gear lands in the inventory
            ids.add(item_id)
    if (c := (since.get("carried") or {}).get("now")) and c.get("items") is not None:  # what's with them now
        carried = {k: owned[k] for k in c["items"] if k in owned}
    for skill, e in (since.get("skills") or {}).items():
        xp[skill] = max(xp.get(skill, 0), SKILL_XP[min(e["level"], len(SKILL_XP)) - 1])
    for item_id, e in (since.get("items") or {}).items():
        counts[item_id] = (e["count"], counts.get(item_id, (0, 0))[1])
        if e["count"]:
            ids.add(item_id)
    return replace(player, owned_gear=owned, all_item_ids=ids, skill_xp=xp, item_counts=counts,
                   reputation=reputation, achievement_points=points, carried_gear=carried)
