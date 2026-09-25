import json
from pathlib import Path

import pytest

from walkscape_mcp.gamedata import GameData
from walkscape_mcp.player import parse_save
from walkscape_mcp.sync import load_snapshot

FIXTURES = Path(__file__).parent / "fixtures"


@pytest.fixture(scope="session")
def gd():
    snap = load_snapshot()
    if not snap:
        pytest.skip("no game data snapshot; run `uv run python -m walkscape_mcp.sync` first")
    return GameData(snap)


@pytest.fixture(scope="session")
def player(gd):
    return parse_save(gd, json.loads((FIXTURES / "save.json").read_text()))


@pytest.fixture
def svc(gd, player, tmp_path, monkeypatch):
    """A Service on the fixture save with remembered info in tmp_path (no snapshot reload or background refresh)."""
    from walkscape_mcp.service import Service
    from walkscape_mcp.wiki import Wiki

    monkeypatch.setattr("walkscape_mcp.service.player_info_file", lambda: tmp_path / "player_info.json")
    (tmp_path / "history").mkdir()
    monkeypatch.setattr("walkscape_mcp.service.save_history_dir", lambda: tmp_path / "history")
    svc = Service.__new__(Service)
    svc._gd, svc._player, svc._not_met = gd, player, {}
    svc._reload_snapshot = lambda: None
    svc.wiki = Wiki()
    return svc
