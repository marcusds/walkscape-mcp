"""Snapshot the official gear planner API (gear.walkscape.app) to local JSON.

The planner is backed by the game's own data export, so values are exact
(stat values, requirement conditions, loot table weights). We fetch it once,
store it on disk, and only refresh when stale or when the game version changes.
"""

from __future__ import annotations

import asyncio
import json
import time
from pathlib import Path

import httpx
import logging

logging.getLogger("httpx").setLevel(logging.WARNING)

from .paths import snapshot_dir

API = "https://gear.walkscape.app/api"
CONCURRENCY = 4  # max requests in flight to the planner API, shared by every phase
USER_AGENT = "walkscape-mcp (personal gear optimizer; github.com/marcusds)"

BULK_GETS = {
    "items_list": "items",
    "items_categorized": "items/categorized_items",
    "materials": "items/search?type=material&detailed=true",
    "containers": "items/search?type=container&detailed=true",
    "url_mapping": "items/url_mapping",
    "activities_list": "activities",
    "locations_list": "locations",
    "routes": "routes",
    "keywords": "keywords",
    "skills": "skills",
    "stats": "stats",
    "pets_list": "pets",
    "abilities_list": "abilities",
    "factions": "factions",
    "services_list": "services",
    "terrain_modifiers": "terrain_modifiers",
    "global_variables": "global_variables",
    "recipes_list": "recipes",
    "loot_tables_list": "lootTables",
}


async def _get(client: httpx.AsyncClient, sem: asyncio.Semaphore, path: str):
    async with sem:
        for attempt in range(4):
            try:
                r = await client.get(f"{API}/{path}")
                r.raise_for_status()
                return r.json()
            except (httpx.HTTPError, ValueError):
                if attempt == 3:
                    raise
                await asyncio.sleep(1.5 * (attempt + 1))


async def _post(client: httpx.AsyncClient, sem: asyncio.Semaphore, path: str, body: dict):
    async with sem:
        r = await client.post(f"{API}/{path}", json=body)
        r.raise_for_status()
        return r.json()


async def _details(client, sem, prefix: str, ids: list[str]) -> dict:
    results = await asyncio.gather(*(_get(client, sem, f"{prefix}/{i}") for i in ids), return_exceptions=True)
    out = {}
    for i, res in zip(ids, results):
        if not isinstance(res, Exception) and res:
            out[i] = res
    return out


log = logging.getLogger(__name__)


async def fetch_snapshot(game_version: str | None = None) -> dict:
    sem = asyncio.Semaphore(CONCURRENCY)
    t0 = time.time()

    def phase(name):
        log.info("sync %s done at %.1fs", name, time.time() - t0)

    async with httpx.AsyncClient(timeout=60, headers={"User-Agent": USER_AGENT}) as client:
        keys = list(BULK_GETS)
        bulk = await asyncio.gather(*(_get(client, sem, BULK_GETS[k]) for k in keys))
        data = dict(zip(keys, bulk))
        phase("bulk")

        data["activities"] = await _details(client, sem, "activities", [a["id"] for a in data["activities_list"]])
        phase("activities")
        data["locations"] = await _details(client, sem, "locations", [l["id"] for l in data["locations_list"]])
        data["pets"] = await _details(client, sem, "pets", [p["id"] for p in data["pets_list"]])
        data["recipes"] = await _details(client, sem, "recipes", [r["id"] for r in data["recipes_list"]])
        phase("locations/pets/recipes")

        ability_ids = [a["id"] for a in data["abilities_list"]]
        data["abilities"] = {a["id"]: a for a in await _post(client, sem, "abilities/multiple", {"ids": ability_ids})}

        table_ids = [t["id"] for t in data["loot_tables_list"]]
        # the endpoint gets superlinearly slower with batch size (~50s for 100 ids), so use small batches
        batches = await asyncio.gather(*(
            _post(client, sem, "lootTables/multiple", {"ids": table_ids[i : i + 10]}) for i in range(0, len(table_ids), 10)
        ))
        data["loot_tables"] = {t["id"]: t for batch in batches for t in batch}
        phase("loot tables")

    data["_meta"] = {"fetched_at": time.time(), "game_version": game_version, "source": API}
    return data


def save_snapshot(data: dict, path: Path | None = None) -> Path:
    path = path or snapshot_dir() / "gamedata.json"
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(data, separators=(",", ":")))
    tmp.replace(path)
    return path


def load_snapshot(path: Path | None = None) -> dict | None:
    path = path or snapshot_dir() / "gamedata.json"
    if not path.exists():
        return None
    return json.loads(path.read_text())


def refresh(game_version: str | None = None) -> dict:
    data = asyncio.run(fetch_snapshot(game_version))
    save_snapshot(data)
    return data


async def resolve_export_ids(ids: list[str]) -> dict[str, str]:
    """Map gear-set export ids (legacy `item-foo-uuid` or bare UUIDs) to current item ids."""
    async with httpx.AsyncClient(timeout=30, headers={"User-Agent": USER_AGENT}) as client:
        r = await client.post(f"{API}/items/ids", json={"ids": ids})
        r.raise_for_status()
        return r.json()


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    t = time.time()
    d = refresh()
    print({k: len(v) for k, v in d.items() if hasattr(v, "__len__")}, f"{time.time() - t:.1f}s")
