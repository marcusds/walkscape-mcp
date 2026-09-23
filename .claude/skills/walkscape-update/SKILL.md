---
name: walkscape-update
description: Bring the WalkScape MCP fully up to date - refresh game data and the wiki dump, detect when the hand-ported game logic (planner formulas, wiki mechanics, requirement/stat types, save format) has drifted from its sources, fix the code, and re-verify. Use when the user asks to update/check WalkScape data, after a WalkScape game update, when results look wrong, or when data_status reports stale data.
---

# Update the WalkScape MCP

Repo: `/mnt/storage1/workspace/walkscape-mcp` (run every command with `uv run --directory` pointing there, or cd into it).

Two different things can go stale:

1. **Data**: items, activities, loot tables and pets. `sync.py` refreshes these from gear.walkscape.app, and the wiki dump is updated daily. This part is mechanical.
2. **Hand-ported logic** that no refresh touches. `walkscape-drift` compares each piece against a reference copy in `reference/`:

| Logic in our code | Ported from | Drift item |
| --- | --- | --- |
| `engine.compute_metrics` (step math, WE cap, min 10 steps, DA/DR caps, fine = 1% × (1+x)) | planner `optimiser.worker.js` | "Optimiser worker logic" |
| `engine.check_requirement` (requirement types) | worker's requirement `switch` | "Optimiser worker logic" / "Requirement types" |
| `player.tool_slots` (3/4/5/6 at char lvl 1/20/50/80) | worker `{1:3,20:4,50:5,80:6}` + wiki Character_Level | "Optimiser worker logic", "Wiki Character_Level" |
| `engine.static_sources` level WE (1.25%/lvl, 20 lvls, travel 0.5%) | bundle snippet | "level_work_efficiency", "Wiki Work_Efficiency_(Mechanics)" |
| `gamedata.item_attrs` (quality tiers stack cumulatively, merge key) | bundle snippet | "quality_tier_stacking" |
| `gamedata.consumable_attrs` (fine → `fineAttributes`) | bundle snippet | "consumable_fine_attrs" |
| `engine.special_drops` (chance-to-find rolled per reward roll) | bundle snippets | "special_table_attrs", "drop_steps_per_item", "Wiki Roll_Special_Table_(Mechanics)" |
| `engine.TABLE_MODIFIER` (chest/gem/collectible/nest scaling) | bundle snippet | "drop_modifier_stats", "Loot table group types", finding mechanics wiki pages |
| `gamedata.pet_attrs` (levels[level-1]) | bundle snippet | "pet_level_attrs" |
| `gearset.py` import/export format | bundle snippet | "gear_set_import" |
| `player.SKILL_XP` / `CHAR_STEPS` curves | wiki | "Wiki Skill_Experience", "Wiki Character_Level" |
| `sync.BULK_GETS` / detail endpoints | planner API calls | "Planner API endpoints" |
| `engine.HANDLED_*_TYPES` | snapshot contents | "Requirement types / Stat types the engine doesn't…" |
| `gamedata.py` / `engine.py` field access | snapshot object keys | "Game data schema changed" |
| `player.parse_save`, `SAVE_SLOTS` | the user's export | "Character export format", "Save has gear slots…" |

## Procedure

### 1. Kick off the data refresh (slow, run in background)

```sh
uv run --directory /mnt/storage1/workspace/walkscape-mcp python -m walkscape_mcp.sync
```

Run this with `run_in_background`; it takes about 6 minutes. Don't raise `sync.CONCURRENCY` above 4 because the planner API is a small community server. The running MCP server picks up the new snapshot automatically.

### 2. While it runs, check logic drift against the planner JS, the wiki and the save

```sh
uv run --directory /mnt/storage1/workspace/walkscape-mcp walkscape-drift --skip data
```

This also downloads the newest wiki dump.

### 3. After the refresh finishes, check the data-side drift

```sh
uv run --directory /mnt/storage1/workspace/walkscape-mcp walkscape-drift --skip planner wiki save
```

Exit code 0 means no drift. WARN lines are informational: a planner rebuild with identical logic, a stale save, or missing references.

### 4. Triage each DRIFT item

Use the table above to find the code. Then:

- **Planner JS diffs** are normalized: minified identifiers are replaced by `_`, and the code is split on `; { }`. A plain rebuild produces no diff, so any diff that remains is a real change to literals, strings or structure.
  - To read the real code, fetch the bundle (`curl -s https://gear.walkscape.app/ | grep -o '/assets/index-[^"]*\.js'`) and grep around the anchor regex from `drift.BUNDLE_ANCHORS`.
  - Compare it with `reference/planner/`. `optimiser.worker.js`, `snippets.json` and `reference/wiki/` are git-ignored (not licensed for redistribution), so `git diff` can't show their changes. Copy them aside before running `--accept` if you need the old version.
  - Port the semantic change, not the minified code.
  - If an anchor no longer matches, locate the equivalent code by its behaviour and update `BUNDLE_ANCHORS`.
- **Wiki mechanics diffs**: many are only rewording or a "game version" line bump. Change code only when a formula, constant, cap or rule changed. To read a full page:
  `uv run --directory /mnt/storage1/workspace/walkscape-mcp python -c "from walkscape_mcp.wiki import Wiki; print(Wiki().page('Work_Efficiency_(Mechanics)', 100000))"`
  When the wiki and the planner JS disagree, prefer the planner: it is what the official gear planner computes. Mention the disagreement to the user.
- **Unhandled requirement or stat types**: find real examples in the snapshot (for example, walk `~/.local/share/walkscape-mcp/snapshot/gamedata.json` for `"type": "<new type>"`) and the worker's handling of that type.
  - If it can be evaluated from the save or the context, implement it in `check_requirement` or `compute_metrics` and add it to `HANDLED_*`.
  - Otherwise add it to `APPROXIMATED_REQUIREMENT_TYPES` / `IGNORED_STAT_TYPES` with a comment explaining why.
- **New loot table group types**: decide which stat scales them, if any, and add them to `TABLE_MODIFIER`. Types with no scaling go in the `known_groups` set in `drift.check_data`.
- **Schema key changes**: a new key can mean a new mechanic (for example a new activity field). Inspect examples before deciding it's irrelevant.
- **Save format changes**: update `player.parse_save` and `SAVE_SLOTS`.

### 5. Verify

```sh
uv run --directory /mnt/storage1/workspace/walkscape-mcp pytest -q
```

`tests/test_engine.py` pins numbers taken from the wiki: Mine gold ore at 68/40 steps, the chest every 17,000 steps, Farganite pickaxe WE by quality, and the XP tables. If a mechanic changed on purpose, update the pinned values from the wiki page, not from our own output. Add a test for every logic change.

Also run a smoke test through the service:
```sh
uv run --directory /mnt/storage1/workspace/walkscape-mcp python -c "
from walkscape_mcp.service import Service; s=Service()
r=s.optimize_loadout('Mine gold ore','item',\"Adventurers' Guild token\",'Crown of Cinders',pet='camel')
print(r['result'], r['vs_current_gear'], r['notes'])"
```

### 6. Record the new references and commit

Only do this after the code matches the new sources:

```sh
uv run --directory /mnt/storage1/workspace/walkscape-mcp walkscape-drift --accept
```

Commit the code changes and the tracked parts of `reference/` together (`schema.json`, `save_format.json`, `planner/fingerprint.json`, `planner/endpoints.json`). The message should say which sources changed and what logic was updated. The planner JS and wiki copies stay local and git-ignored.

### 7. Player save

If drift reports a stale or older-build character export, ask the user to paste a fresh one (in-game: Settings → Export character data) and load it with the `load_player_save` MCP tool.

### 8. Code changes need an MCP reconnect

The server hot-reloads data snapshots but not Python code. After changing code, tell the user to reconnect `walkscape` from `/mcp`, or start a new session.

### Optional: dependencies

Every month or so, run `uv lock --upgrade` and then `pytest`. `mcp` 2.x has had breaking API renames, so check `server.py` still imports (`from mcp.server.mcpserver import MCPServer`).

## Report back

In a few lines, tell the user:
- the data and wiki dump versions
- which drift items were found and what changed in the code (or that nothing changed)
- test results
- whether a fresh save export is needed
