import asyncio

from walkscape_mcp import sync


def test_lists_come_from_bootstrap(monkeypatch):
    """Lists the planner serves in /bootstrap are taken from it; the rest still use their own endpoints."""
    calls = []
    boot = {b: [{"id": f"{b}-1"}] for b in sync.BOOTSTRAP_KEYS.values()}

    async def fake_get(client, sem, path):
        calls.append(path)
        return boot if path == "bootstrap" else [{"id": "x"}] if "/" not in path else {"id": path}

    async def fake_post(client, sem, path, body):
        return [{"id": i} for i in body["ids"]]

    monkeypatch.setattr(sync, "_get", fake_get)
    monkeypatch.setattr(sync, "_post", fake_post)
    data = asyncio.run(sync.fetch_snapshot())

    for key, bkey in sync.BOOTSTRAP_KEYS.items():
        assert data[key] == boot[bkey]
    bulk = [p for p in calls if "/" not in p]
    assert sorted(bulk) == sorted(["bootstrap", *sync.BULK_GETS.values()])
