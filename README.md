# walkscape-mcp

An MCP server for optimizing WalkScape loadouts by talking to Claude, e.g.

> Create me the ideal loadout for getting random drops of Adventurers' Guild tokens while leaving up my camel pet by mining at Crown of Cinders

## Data sources

- **Game data** – the official gear planner API (`gear.walkscape.app`): exact item stats per quality, attribute conditions, activities, loot table weights, pets, recipes. Snapshotted to `~/.local/share/walkscape-mcp/snapshot/` and refreshed in the background when a loaded save reports a different game version, or after 7 days as a fallback. A full refresh takes about 6 minutes: the API is slow, and requests are capped at 4 in flight to keep load off it. The running server keeps using the old snapshot and switches to the new one when the refresh finishes.
- **Wiki** – the daily ZIM dump from [Walkscape-Wiki-Scrapper](https://github.com/samuellmdev/Walkscape-Wiki-Scrapper), downloaded at most every 6h, so wiki.walkscape.app itself gets no traffic. Used for mechanics, lore, shops and anything the structured data doesn't cover.

## Setup

```sh
uv sync
uv run python -m walkscape_mcp.sync        # first snapshot (~6 min)
claude mcp add --scope user walkscape -- uv run --directory "$PWD" walkscape-mcp
```

Then in Claude, paste your character export (in-game: Settings → Export character data) and ask away. The save is stored so you only paste it again when your gear or levels change. The wiki dump downloads by itself the first time it's needed.

The export leaves out some things, such as how many times you've completed an activity. Some gear bonuses and activities unlock after a number of completions (skis, skydiscs, diving gear). Results assume these unlocks are reached and list them, and Claude asks whether you have. A "yes" is saved in `player_info.json`, along with any achievements or other facts you mention, and survives pasting a new save. A "not yet" lasts only for the current session, because your counts keep growing.

To maintain the hand-ported logic (see [Keeping it up to date](#keeping-it-up-to-date)), also run:

```sh
uv run walkscape-drift --init              # create the local, untracked reference copies
ln -s "$PWD/.claude/skills/walkscape-update" ~/.claude/skills/walkscape-update
```

## Tools

| Tool | Purpose |
| --- | --- |
| `load_player_save` / `player_summary` | Load and inspect your character export |
| `remember_player_info` | Store what the export lacks: action-history unlocks you've reached, achievements and other notes |
| `optimize_loadout` | Best loadout for an activity/recipe and objective, using owned gear; shows the diff from your current gear, unowned upgrades and an export string |
| `evaluate_loadout` | Stats, steps and drop rates for your current gear or a gear-set string |
| `rank_activities` | Where to farm an item in the fewest steps |
| `get_item` / `get_activity` / `get_location` / `search_game_data` | Lookups |
| `decode_gear_set` | Read a gear.walkscape.app export string |
| `wiki_search` / `wiki_page` | Offline wiki |
| `data_status` | Data freshness; `refresh=true` forces an update |

Objectives: `item`, `fine_item`, `xp`, `total_xp`, `reward_rolls`, `actions`, `fine`, `chests`, `gems`, `collectibles`.

## Model

The step and drop formulas are ported from the official planner's optimiser worker and the wiki mechanics pages:

- Level work efficiency: +1.25% per level above the requirement, up to 20 levels. Total WE is capped at the activity's max.
- Steps per completion: `max(10, ceil(work / WE × (1 + steps%)) + flat steps)`. Double action divides steps per action, and double rewards divides steps per reward roll.
- Chest, gem, collectible and bird-nest tables scale by their finding stat. Fine materials use 1% × (1 + fine finding).
- "Chance to find X" gear rolls once per reward roll, the same way the planner handles it.
- Attributes are active only when their conditions hold: skill, skill type, location keywords, realm, set-piece counts, equipped keywords, pet abilities, reputation and so on. Tool slots follow character level, and tools with banned keyword combinations (two pickaxes, for example) can't be equipped together.

The optimizer builds a loadout greedily, then hill-climbs one slot at a time, including the pet and consumable. It also seeds each set bonus so multi-piece sets get a fair trial.

### Not modelled

- Crafting service bonuses. For recipes, the service requirement is assumed met.
- Quality-outcome odds.
- Level-scaled loot weights for fishing.
- A few rare requirement types (`skillTypeLevel`, `inputKeywordWithLevel`). These are assumed satisfied and reported in `notes`.

## Keeping it up to date

Game data refreshes on its own. Some logic was ported by hand from the planner's JavaScript and the wiki's mechanics pages, and a data refresh never checks those sources. `walkscape-drift` compares them against reference copies in `reference/`, together with the requirement, stat and loot-table types the engine handles and the save format:

```sh
uv run walkscape-drift            # report; exit 1 on drift
uv run walkscape-drift --accept   # after updating the code, record new references
```

The planner JavaScript and wiki text are copyrighted by their authors, so those references are not in git. They are kept locally only. On a fresh clone, `walkscape-drift --init` creates them without touching the tracked references. Don't use `--accept` for this: it would also overwrite the tracked references and hide any real drift since the last commit.

The `walkscape-update` Claude Code skill (`.claude/skills/walkscape-update`, symlinked into `~/.claude/skills`) covers the whole routine: refresh, check for drift, fix, test, accept and commit. Ask Claude to "update walkscape" after a game update.

## Tests

```sh
uv run pytest
```
