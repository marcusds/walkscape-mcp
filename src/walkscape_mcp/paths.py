import os
from pathlib import Path


def home() -> Path:
    base = os.environ.get("WALKSCAPE_MCP_HOME") or os.path.join(
        os.environ.get("XDG_DATA_HOME", os.path.expanduser("~/.local/share")), "walkscape-mcp"
    )
    p = Path(base)
    p.mkdir(parents=True, exist_ok=True)
    return p


def snapshot_dir() -> Path:
    p = home() / "snapshot"
    p.mkdir(exist_ok=True)
    return p


def wiki_dir() -> Path:
    p = home() / "wiki"
    p.mkdir(exist_ok=True)
    return p


def player_file() -> Path:
    return home() / "player.json"


def player_info_file() -> Path:
    """Facts the user told us that the save export doesn't contain (kept across save reloads)."""
    return home() / "player_info.json"


def save_history_dir() -> Path:
    """Every character export loaded, one file each, for comparing progress between them."""
    p = home() / "save_history"
    p.mkdir(exist_ok=True)
    return p
