import re
from pathlib import Path

from walkscape_mcp.drift import normalize_js

WORKER = (Path(__file__).parents[1] / "reference/planner/optimiser.worker.js").read_text()


def test_identifier_renames_are_not_drift():
    # variables only: a rebuild keeps property names (.y) and strings
    renamed = re.sub(r"(?<![\w$.\"'])(W|N|y)\b", lambda m: {"W": "Q", "N": "Z", "y": "k"}[m.group(1)], WORKER)
    assert renamed != WORKER
    assert normalize_js(renamed) == normalize_js(WORKER)


def test_constant_changes_are_drift():
    changed = WORKER.replace("Math.max(10,", "Math.max(12,")
    assert changed != WORKER
    assert normalize_js(changed) != normalize_js(WORKER)
