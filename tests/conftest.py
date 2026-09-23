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
