"""Crafted item quality odds, ported from the wiki's Quality Outcome (Mechanics) page.

Quality outcome = (skill level - recipe level) + quality outcome from gear, consumables and services. Each quality
has a starting weight that falls linearly across its band once the outcome passes the band start, down to a minimum
weight, and never below the weight of the quality above it. Fine materials move every roll up one quality.
"""

from __future__ import annotations

from .gamedata import QUALITIES

# Normal, Good, Great, Excellent, Perfect, Eternal (common .. ethereal). The wiki gives these as the weights recipes
# use; the game data has no per-recipe weights.
START_WEIGHTS = [1000, 200, 50, 10, 2.5, 0.05]
MIN_WEIGHTS = [4, 4, 4, 4, 2, 0.05]


def quality_odds(recipe_level: int, quality_outcome: float, fine_materials: bool = False) -> dict[str, float]:
    """Probability of each quality (keyed common .. ethereal) for one crafted item."""
    weights = [0.0] * len(START_WEIGHTS)
    for i in reversed(range(len(START_WEIGHTS))):
        start, end = 100 * i, (100 + recipe_level) * (i + 1)
        w = START_WEIGHTS[i]
        if quality_outcome > start:
            slope = (START_WEIGHTS[i] - MIN_WEIGHTS[i]) / (start - end)
            w = max(MIN_WEIGHTS[i], START_WEIGHTS[i] + slope * (quality_outcome - start))
        if i + 1 < len(weights):
            w = max(w, weights[i + 1])  # a lower quality is never rarer than a higher one
        weights[i] = w
    total = sum(weights)
    probs = [w / total for w in weights]
    if fine_materials:
        probs = [0.0, *probs[:-2], probs[-2] + probs[-1]]
    return dict(zip(QUALITIES, probs, strict=True))


def at_least(odds: dict[str, float], quality: str) -> float:
    return sum(p for q, p in odds.items() if QUALITIES.index(q) >= QUALITIES.index(quality))
