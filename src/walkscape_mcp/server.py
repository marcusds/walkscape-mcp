from __future__ import annotations

from mcp.server.mcpserver import MCPServer

from .optimizer import OBJECTIVES
from .service import Service

INSTRUCTIONS = f"""\
WalkScape gear/loadout optimizer backed by the official gear planner data (gear.walkscape.app)
and an offline copy of the WalkScape wiki.

Workflow:
1. If no character is loaded, ask the user to paste their exported character JSON and call load_player_save.
2. Resolve names loosely: tools accept in-game names ("Crown of Cinders", "Adventurers' Guild token"), so call
   get_activity/get_item/optimize_loadout directly instead of search_game_data first. Make independent calls in parallel.
   get_activity already marks each requirement against the character and, for recipes, shows how many of each
   material they have and where it comes from; rank_activities lists blocked sources and why. Use these instead of
   follow-up lookups.
3. For "best loadout for X" requests call optimize_loadout. Map the user's goal to an objective:
{chr(10).join(f"   - {k}: {v}" for k, v in OBJECTIVES.items())}
   "keep my camel"/"level my pet" -> pet="camel" (or pet="current"). Items the user insists on -> require_items.
4. For "where should I farm X" call rank_activities. For "how do I get to X" / travel gear, call plan_route;
   for "nearest sawmill/kitchen/...", find_services. Recipes needing a service are done at any location with it.
   Crafting N of something -> plan_recipe; crafting gear of a quality (Perfect, Eternal...) -> craft_quality; "how long to level X" -> steps_to_level; "when is my inventory full"
   -> inventory_fill. Several items at once -> targets on rank_activities / optimize_loadout (objective "items").
   Remember where the player is (remember_player_info location) so travel-aware tools start there.
5. For mechanics/lore/anything not covered, use wiki_search + wiki_page.
6. When the user mentions something about their character that the save export doesn't contain (how many times
   they've done an activity, travel steps, achievements, quests, unlocks), call remember_player_info so it persists.
   Use its structured fields, not notes, for achievements, gear found / skill levels / item counts since the
   export, goals and explored regions. For "what's my next goal", call achievements and ask about any
   "not recorded" ones before assuming they're missing.
   If results note assumed action history, ask the user whether they've reached each one and record the answer.

Present results as a slot-by-slot table, the key numbers vs current gear, the planner_link (opens the loadout
in gear.walkscape.app, handy on a phone; it drops item quality and pet level) and the gear_set_export string
(full detail, importable at gear.walkscape.app). Mention notes/assumptions briefly.
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
    achievements_unlocked: list[str] | None = None,
    achievement_progress: dict[str, str] | None = None,
    achievements_not_unlocked: list[str] | None = None,
    gear_found: list[str] | None = None,
    skill_levels: dict[str, int] | None = None,
    item_counts: dict[str, int] | None = None,
    goals: list[str] | None = None,
    goals_done: list[str] | None = None,
    regions_explored: list[str] | None = None,
    location: str | None = None,
) -> dict:
    """Store facts about the character that the save export doesn't include. Kept across sessions and save reloads.

    Some gear bonuses and activities unlock after completing an activity N times (skis, skydiscs, diving gear,
    log splitters) or walking N travel steps. The save has no such history, so results assume these are reached
    and list them in notes. Ask the user, then record:
    completed: requirements the user has reached, e.g. ["Classic skiing"], ["travel steps 125000"]. Saved permanently.
    not_yet: ones they haven't reached. Remembered for this session only, since the counts keep growing; ask again later.
    An entry is an activity name, optionally followed by the count; without a count, completed means the highest
    threshold in the game and not_yet the lowest.
    notes: free-form facts nothing below covers, e.g. "Finished the bank repair quest".
    forget: remove notes or reached entries containing this text. Never touches achievements.
    achievements_unlocked: achievements the user has unlocked, e.g. ["Masterchef"]. Always record these here,
      never in notes. Names are matched against the wiki's achievement list.
    achievement_progress: progress toward ones not yet unlocked, e.g. {"Winnie The Pooh": "41/100"}; replaces
      the previous value.
    achievements_not_unlocked: undo a mistaken unlock.

    Changes since the save was exported (every tool uses these; dropped once a newer save covers them):
    gear_found: gear the user got since, e.g. ["Flippy spatula (rare)"]; the optimizer will use it.
    skill_levels: levels gained since, e.g. {"cooking": 46}; requirements are checked against these.
    item_counts: materials/consumables the user now has in total (bank + inventory), e.g. {"Berries": 586}.
      If they only give an inventory count, add the bank count from the save.

    goals: what the user is working toward, e.g. ["190 achievement points for Treasure hunter bandolier"].
    goals_done: remove goals containing this text.
    regions_explored: regions the user has fully explored, e.g. ["Jarvonia"] (unlocks exploreRealm requirements).
    location: where the character is now; travel-aware tools start from here when not told otherwise.
      Update it whenever the user mentions where they are or arrive somewhere.
    Returns everything currently remembered."""
    return s().remember_player_info(completed, not_yet, notes, forget,
                                    achievements_unlocked, achievement_progress, achievements_not_unlocked,
                                    gear_found, skill_levels, item_counts, goals, goals_done, regions_explored,
                                    location)


@mcp.tool()
def achievements(show: str = "not_unlocked") -> dict:
    """Every achievement (wiki list: difficulty, points, requirements, rewards) with the user's recorded status.
    show: "not_unlocked" (default; for "what should I go for next"), "unlocked" or "all".
    The save only has a point total, so an achievement the user never mentioned shows as "not recorded"."""
    return s().achievements(show)


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
    targets: dict[str, int] | None = None,
) -> dict:
    """Find the best gear loadout for an activity (or crafting recipe).

    activity: activity or recipe name, e.g. "Mine gold ore".
    objective: one of item, fine_item, xp, total_xp, reward_rolls, actions, fine, chests, gems, collectibles, items.
    target: item name for item/fine_item (e.g. "Adventurers' Guild token"), or skill name for xp.
    targets: for objective "items": item -> quantity, e.g. {"Flax": 50, "Honeycomb": 59}; minimizes the steps
      until you have all of them.
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
                                exclude_items, owned_only, show_missing_upgrades, targets)


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
def rank_activities(target: str | None = None, top: int = 10, pet: str | None = "current",
                    consumable: str | None = "none", owned_only: bool = True,
                    targets: dict[str, int] | None = None, near: str | None = None,
                    quantity: int | None = None, fine: bool = False) -> dict:
    """Rank activities/locations by steps needed to obtain an item, each with its own optimized owned loadout.
    For 'chance to find' items (like Adventurers' Guild tokens) every activity is considered.
    targets: several items at once with quantities, e.g. {"Flax": 50, "Honeycomb": 59}; ranks by steps until
      you have all of them.
    near: where the player starts (default: remembered current location). Each row then gets travel_steps, and
      rows that are closer but slower say below how many items they beat the fastest.
    quantity: how many of `target` are wanted; with a start location, ranks by travel + farming steps.
    fine: rank by steps per fine version of `target` (fine material finding gear) instead.
    Also returns how many the character has, sources they can't use yet with the unmet requirements, and
    non-activity sources (recipes, chests) when no activity works."""
    return s().rank_activities(target, top, pet, consumable, owned_only, targets, near, quantity, fine)


@mcp.tool()
def get_item(name: str) -> dict:
    """Item details: slot, keywords, requirements, attributes at every quality, consumable effects,
    which qualities the player owns, how many they have, and where the item comes from."""
    return s().item_info(name)


@mcp.tool()
def get_activity(name: str) -> dict:
    """Activity or recipe details: requirements (marked met/NOT MET for the loaded character, with their level),
    locations, base/min steps, XP, base drop rates. Recipes also list each material with how many the character
    has and where it comes from."""
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
def plan_route(destination: str, start: str | None = None, via: list[str] | None = None,
               avoid: list[str] | None = None, pet: str | None = "auto", owned_only: bool = True) -> dict:
    """Fastest travel route between two locations with the best travel gear for each leg.

    Picks the route by steps with optimized gear (not base distance), skipping legs whose terrain requirements
    (skis, diving gear, light sources, permits, agility level) the character can't meet.
    via: locations to pass through in order (e.g. to compare an overland and an underwater route).
    start: where the trip begins (default: remembered current location).
    avoid: locations to route around.
    Returns each leg with base and optimized steps, the gear whenever it changes (with planner_link), and the best
    single loadout for the whole trip (planner_link and gear_set_export) for users who don't want to swap."""
    return s().plan_route(destination, start, via, avoid, pet, owned_only)


@mcp.tool()
def find_services(service: str, near: str | None = None, top: int = 5) -> dict:
    """Nearest locations with a service (sawmill, kitchen, forge, loom, workshop, trinketry bench, mailbox,
    wardrobe...) or a named one ("Cursed forge"), ranked by base travel steps from `near`. Each row lists the
    services there (with tier; recipes need basic or advanced), the route, and terrain the route requires.
    near defaults to the remembered current location.
    Legs blocked by permits or levels are avoided."""
    return s().find_services(service, near, top)


@mcp.tool()
def plan_recipe(recipe: str, count: int, near: str | None = None, pet: str | None = "auto") -> dict:
    """Plan crafting `count` of a recipe's output: crafts needed and steps with the best owned loadout, each
    material needed vs owned, and for any shortfall the best place to gather it and the steps. Also the nearest
    location with the required service (from `near`, default the remembered current location)."""
    return s().plan_recipe(recipe, count, near, pet)


@mcp.tool()
def craft_quality(recipe: str, quality: str = "Perfect", fine_materials: bool = False, location: str | None = None,
                  pet: str | None = "auto") -> dict:
    """Odds of each quality (Normal, Good, Great, Excellent, Perfect, Eternal) when crafting gear, and the best
    owned loadout and crafting service for getting at least `quality`: expected crafts, steps and materials.
    Quality outcome comes from skill level over the recipe's level, gear, consumables and the service.
    fine_materials: crafting with fine materials moves every roll up one quality."""
    return s().craft_quality(recipe, quality, fine_materials, location, pet)


@mcp.tool()
def steps_to_level(skill: str, level: int, activity: str | None = None, location: str | None = None,
                   pet: str | None = "auto") -> dict:
    """XP the character needs to reach `level` in `skill`; with an activity or recipe, also the steps and
    completions to get there using the best owned XP loadout."""
    return s().steps_to_level(skill, level, activity, location, pet)


@mcp.tool()
def inventory_fill(activity: str, free_slots: int, location: str | None = None,
                   inventory: dict[str, int] | None = None, gear_set: str | None = None) -> dict:
    """Steps until an activity's drops fill `free_slots` more inventory slots (time to bank).
    inventory: current counts of items already in the inventory, e.g. {"Berries": 223}, so partly filled
    stacks are counted. Uses the equipped gear, or gear_set (an export string, e.g. from optimize_loadout)."""
    return s().inventory_fill(activity, free_slots, location, inventory, gear_set)


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
