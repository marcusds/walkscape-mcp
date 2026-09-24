from __future__ import annotations

from mcp.server.mcpserver import MCPServer

from .optimizer import OBJECTIVES
from .service import Service

INSTRUCTIONS = f"""\
WalkScape gear/loadout optimizer backed by the official gear planner data (gear.walkscape.app)
and an offline copy of the WalkScape wiki.

Workflow:
1. If no character is loaded, ask the user to paste their exported character JSON and call load_player_save.
2. Resolve names loosely: tools accept in-game names ("Crown of Cinders", "Adventurers' Guild token").
3. For "best loadout for X" requests call optimize_loadout. Map the user's goal to an objective:
{chr(10).join(f"   - {k}: {v}" for k, v in OBJECTIVES.items())}
   "keep my camel"/"level my pet" -> pet="camel" (or pet="current"). Items the user insists on -> require_items.
4. For "where should I farm X" call rank_activities.
5. For mechanics/lore/anything not covered, use wiki_search + wiki_page.
6. When the user mentions something about their character that the save export doesn't contain (how many times
   they've done an activity, travel steps, achievements, quests, unlocks), call remember_player_info so it persists.
   If results note assumed action history, ask the user whether they've reached each one and record the answer.

Present results as a slot-by-slot table, the key numbers vs current gear, and the gear_set_export string
(importable at gear.walkscape.app). Mention notes/assumptions briefly.
"""

mcp = MCPServer("walkscape", instructions=INSTRUCTIONS)
svc: Service | None = None


def s() -> Service:
    global svc
    if svc is None:
        svc = Service()
    return svc


@mcp.tool()
def load_player_save(save_json: str) -> dict:
    """Load the player's exported character data (the JSON from the game, or a path to a file containing it).
    Persists it so later sessions remember it. Returns a summary: levels, equipped gear, pets, collectibles."""
    return s().load_save(save_json)


@mcp.tool()
def player_summary() -> dict:
    """Summary of the currently loaded character (skill levels, equipped gear, pets, consumables)."""
    return s().player_summary()


@mcp.tool()
def remember_player_info(
    completed: list[str] | None = None,
    not_yet: list[str] | None = None,
    notes: list[str] | None = None,
    forget: list[str] | None = None,
) -> dict:
    """Store facts about the character that the save export doesn't include. Kept across sessions and save reloads.

    Some gear bonuses and activities unlock after completing an activity N times (skis, skydiscs, diving gear,
    log splitters) or walking N travel steps. The save has no such history, so results assume these are reached
    and list them in notes. Ask the user, then record:
    completed: requirements the user has reached, e.g. ["Classic skiing"], ["travel steps 125000"]. Saved permanently.
    not_yet: ones they haven't reached. Remembered for this session only, since the counts keep growing; ask again later.
    An entry is an activity name, optionally followed by the count; without a count, completed means the highest
    threshold in the game and not_yet the lowest.
    notes: free-form facts, e.g. "Unlocked achievement: Master Angler", "Finished the bank repair quest".
    forget: remove notes or reached entries containing this text.
    Returns everything currently remembered."""
    return s().remember_player_info(completed, not_yet, notes, forget)


@mcp.tool()
def optimize_loadout(
    activity: str,
    objective: str,
    target: str | None = None,
    location: str | None = None,
    pet: str | None = "current",
    consumable: str | None = "none",
    require_items: list[str] | None = None,
    exclude_items: list[str] | None = None,
    owned_only: bool = True,
    show_missing_upgrades: bool = True,
) -> dict:
    """Find the best gear loadout for an activity (or crafting recipe).

    activity: activity or recipe name, e.g. "Mine gold ore".
    objective: one of item, fine_item, xp, total_xp, reward_rolls, actions, fine, chests, gems, collectibles.
    target: item name for item/fine_item (e.g. "Adventurers' Guild token"), or skill name for xp.
    location: where to do it; omit to try every location that has the activity.
    pet: "current" (equipped pet), "none", "auto" (try all owned pets), or a species like "camel" / "camel:2".
    consumable: "none", "auto" (try owned consumables), or a name like "dried fruit fine".
    require_items: items that must stay equipped, e.g. ["Adoring fan statue", "Farganite pickaxe (epic)"].
    exclude_items: items to never use.
    owned_only: only use gear the player owns (default). False = theoretical best-in-slot.
    show_missing_upgrades: also report the best loadout using unowned gear.
    Returns the loadout per slot with active effects, metrics, drop rates, diff vs current gear, and an export string.
    """
    return s().optimize_loadout(activity, objective, target, location, pet, consumable, require_items,
                                exclude_items, owned_only, show_missing_upgrades)


@mcp.tool()
def evaluate_loadout(
    activity: str,
    location: str | None = None,
    gear_set: str | None = None,
    pet: str | None = "current",
    consumable: str | None = "none",
) -> dict:
    """Compute stats, steps per action, XP/step and per-item drop rates for a loadout at an activity.
    Uses the player's currently equipped gear unless a gear_set export string is given.
    Also lists which of the loadout's effects are inactive here and why."""
    return s().evaluate_loadout(activity, location, gear_set, pet, consumable)


@mcp.tool()
def rank_activities(target: str, top: int = 10, pet: str | None = "current", consumable: str | None = "none",
                    owned_only: bool = True) -> dict:
    """Rank activities/locations by steps needed to obtain an item, each with its own optimized owned loadout.
    For 'chance to find' items (like Adventurers' Guild tokens) every activity is considered."""
    return s().rank_activities(target, top, pet, consumable, owned_only)


@mcp.tool()
def get_item(name: str) -> dict:
    """Item details: slot, keywords, requirements, attributes at every quality, consumable effects,
    which qualities the player owns, and where the item comes from."""
    return s().item_info(name)


@mcp.tool()
def get_activity(name: str) -> dict:
    """Activity or recipe details: requirements, locations, base/min steps, XP, and base drop rates."""
    return s().activity_info(name)


@mcp.tool()
def get_location(name: str) -> dict:
    """Location details: faction/region, keywords (e.g. desert, underwater), activities and services."""
    return s().location_info(name)


@mcp.tool()
def search_game_data(query: str, kind: str | None = None) -> list[dict]:
    """Fuzzy search item/activity/location/pet/recipe/keyword names. kind filters to one of those."""
    return s().search(query, kind)


@mcp.tool()
def decode_gear_set(gear_set: str) -> dict:
    """Decode a gear set export string (from gear.walkscape.app) into items per slot."""
    return s().decode_gear_set(gear_set)


@mcp.tool()
def wiki_search(query: str) -> list[str]:
    """Full-text search the WalkScape wiki (offline daily dump). Returns page titles."""
    s().wiki.update()
    return s().wiki.search(query)


@mcp.tool()
def wiki_page(title: str, max_chars: int = 12000) -> str:
    """Read a WalkScape wiki page as text (offline daily dump). Good for mechanics, quests, lore, shops."""
    s().wiki.update()
    return s().wiki.page(title, max_chars)


@mcp.tool()
def data_status(refresh: bool = False) -> dict:
    """Show game data/wiki freshness. refresh=True re-downloads game data in the background."""
    st = s().data_status()
    if refresh:
        st["refresh"] = s().refresh_in_background()
        s().wiki.update(force=True)
    return st


def main():
    s()
    mcp.run()
