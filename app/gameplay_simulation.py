"""Deterministic, bounded Battle Royale simulation without runtime ML."""
from __future__ import annotations

import math
import random

from app.gameplay_config import FIGHTERS_PER_MATCH, MAX_MATCH_SECONDS, MIN_MATCH_SECONDS
from app.gameplay_models import Fighter, Replay

CLASS_STATS = {
    "tank": {"hp": 155.0, "speed": 0.72, "damage": 13.0, "range": 0.12},
    "assassin": {"hp": 82.0, "speed": 1.35, "damage": 22.0, "range": 0.08},
    "ranger": {"hp": 108.0, "speed": 1.0, "damage": 15.0, "range": 0.32},
}


def simulate_match(seed: int, roster: list[Fighter], themes: list[dict]) -> Replay:
    if len(roster) < FIGHTERS_PER_MATCH:
        raise ValueError("at least 24 fighters are required")
    if not themes:
        raise ValueError("at least one theme is required")
    rng = random.Random(seed)
    selected = rng.sample(roster, FIGHTERS_PER_MATCH)
    theme = dict(rng.choice(themes))
    obstacles = [
        {"x": round(rng.uniform(-0.72, 0.72), 4), "y": round(rng.uniform(-0.72, 0.72), 4),
         "radius": round(rng.uniform(0.035, 0.09), 4)} for _ in range(12)
    ]
    items = [
        {"kind": rng.choice(("heal", "speed", "damage")), "x": round(rng.uniform(-0.8, 0.8), 4),
         "y": round(rng.uniform(-0.8, 0.8), 4)} for _ in range(18)
    ]
    state = {}
    for index, fighter in enumerate(selected):
        angle = 2 * math.pi * index / len(selected) + rng.uniform(-0.08, 0.08)
        stats = CLASS_STATS[fighter.class_name]
        state[fighter.key] = {
            "fighter": fighter, "hp": stats["hp"], "kills": 0,
            "x": 0.76 * math.cos(angle), "y": 0.76 * math.sin(angle),
        }
    events: list[dict] = [{"t": 0.0, "type": "start", "theme": theme}]
    alive = list(state)
    t = 12.0
    # Event simulation is intentionally coarse; rendering interpolates event snapshots.
    while len(alive) > 1 and t < MAX_MATCH_SECONDS:
        progress = t / MAX_MATCH_SECONDS
        sudden = t >= 260
        interval = rng.uniform(4.5, 9.0) * (0.45 if sudden else max(0.55, 1.0 - progress * 0.35))
        t = min(MAX_MATCH_SECONDS, t + interval)
        attacker_key = rng.choice(alive)
        targets = [key for key in alive if key != attacker_key]
        target_key = min(targets, key=lambda key: (
            (state[key]["x"] - state[attacker_key]["x"]) ** 2 +
            (state[key]["y"] - state[attacker_key]["y"]) ** 2 + rng.random() * 0.4
        ))
        attacker = state[attacker_key]
        target = state[target_key]
        a_stats = CLASS_STATS[attacker["fighter"].class_name]
        damage = a_stats["damage"] * rng.uniform(0.75, 1.3)
        if attacker["fighter"].class_name == "ranger" and rng.random() < 0.45:
            damage *= 1.35
        if sudden:
            damage *= 2.6
        target["hp"] -= damage
        # Zone damage guarantees bounded completion and models retreat pressure.
        if progress > 0.45 and rng.random() < progress:
            target["hp"] -= 18 + progress * 25
        if target["hp"] <= 0 or (t >= MAX_MATCH_SECONDS and len(alive) > 1):
            alive.remove(target_key)
            attacker["kills"] += 1
            events.append({"t": round(t, 3), "type": "elimination", "actor": attacker_key,
                           "target": target_key, "alive": len(alive)})
        elif rng.random() < 0.18:
            kind = rng.choice(("heal", "speed", "damage"))
            if kind == "heal":
                target["hp"] = min(CLASS_STATS[target["fighter"].class_name]["hp"], target["hp"] + 30)
            events.append({"t": round(t, 3), "type": "item", "actor": target_key, "kind": kind})
    # In pathological random sequences, sudden death resolves all remaining fighters.
    while len(alive) > 1:
        target_key = min(alive, key=lambda key: (state[key]["hp"], rng.random()))
        alive.remove(target_key)
        attacker_key = rng.choice(alive)
        state[attacker_key]["kills"] += 1
        events.append({"t": MAX_MATCH_SECONDS, "type": "elimination", "actor": attacker_key,
                       "target": target_key, "alive": len(alive)})
    winner = alive[0]
    elimination_order = [e["target"] for e in events if e["type"] == "elimination"]
    top3 = ([winner] + list(reversed(elimination_order)))[:3]
    duration = max(MIN_MATCH_SECONDS, min(MAX_MATCH_SECONDS, (events[-1]["t"] if events else 0) + 8))
    events.append({"t": round(duration, 3), "type": "result", "winner": winner, "top3": top3})
    roster_snapshot = [
        {"key": f.key, "name": f.name, "class_name": f.class_name,
         "eliminations": state[f.key]["kills"]} for f in selected
    ]
    return Replay(seed, duration, roster_snapshot, [theme], {"obstacles": obstacles, "items": items},
                  events, top3, winner)
