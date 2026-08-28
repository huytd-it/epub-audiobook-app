"""Deterministic handheld-console games: the asset-free ``retro`` family.

These games replace the sprite-hungry pixel/neon catalog. Nothing is loaded from disk: the
board is a grid of coloured cells and every pixel comes from :mod:`app.gameplay_retro_render`.
A replay only freezes the seed, the board spec and the final scoreboard — the renderer re-runs
the very same engine tick by tick, so a retried clip reproduces the identical match.

Every engine advances on one shared 20 Hz tick; per-game cadence is a tick divider that
shrinks as the level rises, which is exactly how the old brick-game handhelds sped up.
"""
from __future__ import annotations

import random
from collections import deque
from dataclasses import dataclass, field

from app.gameplay_models import GameplayReplay

SCHEMA_VERSION = 3
TICK_RATE = 20
MIN_SECONDS, MAX_SECONDS = 180, 300
START_LIVES = 3

# One shared palette keeps the six games looking like one console.
PALETTE: dict[str, tuple[int, int, int]] = {
    "grid": (24, 32, 44),
    "wall": (62, 78, 102),
    "steel": (166, 182, 206),
    "player": (94, 234, 212),
    "player_dim": (38, 118, 112),
    "enemy": (255, 92, 122),
    "enemy_dim": (128, 46, 62),
    "food": (255, 206, 74),
    "shot": (255, 255, 255),
    "base": (126, 231, 96),
    "shield": (110, 160, 220),
    "trail": (46, 62, 84),
    "road": (34, 44, 60),
    "p0": (86, 214, 255), "p1": (255, 214, 82), "p2": (186, 128, 255),
    "p3": (126, 231, 96), "p4": (255, 118, 118), "p5": (96, 148, 255),
    "p6": (255, 148, 86),
}

# Milestones worth keeping in the replay; routine points are counted, not logged.
_EVENT_KINDS = frozenset({"level", "game_over", "stage_clear", "wave_clear", "crash", "top_out",
                           "ball_lost", "ship_lost", "tank_lost", "base_lost", "overrun", "boss_down",
                           "boss_skill"})


# Vietnamese catalog copy; the UI shows these verbatim.
@dataclass(frozen=True)
class RetroSpec:
    id: str
    name: str
    waveform_policy: str
    description: str
    stat_labels: tuple[tuple[str, str], ...]


@dataclass
class RetroFrame:
    """Everything the painter needs for one tick."""
    cells: list[tuple[int, int, str]]
    hud: dict
    overlay: str | None = None
    mini: list[tuple[int, int, str]] = field(default_factory=list)
    mini_size: tuple[int, int] = (4, 4)
    mini_label: str = ""


class Engine:
    """Base class: lives, level, score, the game-over cycle and event bookkeeping."""

    game_id = ""
    cols = 10
    rows = 10

    def __init__(self, seed: int, config: dict | None = None) -> None:
        self.seed = seed
        self.config = config or {}
        self.rng = random.Random(seed ^ 0xA17E)
        self.tick = 0
        self.score = 0
        self.level = 1
        self.lives = START_LIVES
        self.best = 0
        self.total = 0
        self.games = 1
        self.deaths = 0
        self.top_level = 1
        self.stats: dict[str, int] = {}
        self.best_stats: dict[str, int] = {}
        self.tally: dict[str, int] = {}
        self._peak = -1
        self.events: list[dict] = []
        self._overlay: str | None = None
        self._overlay_until = -1
        self.reset_game()

    # -- lifecycle -------------------------------------------------------
    def reset_round(self) -> None:
        """Rebuild the board after a death, keeping score and level."""

    def reset_game(self) -> None:
        """Rebuild everything after a game over; subclasses reset run stats then the board."""
        self.reset_round()

    def update(self) -> None:
        raise NotImplementedError

    def step(self) -> None:
        self.tick += 1
        if self._overlay is not None and self.tick > self._overlay_until:
            self._overlay = None
        self.update()
        self.best = max(self.best, self.score)
        self.top_level = max(self.top_level, self.level)
        if self.score > self._peak:
            # Keep the board stats of the best game, not of whichever game happens to end last.
            self._peak = self.score
            self.best_stats = dict(self.stats)

    # -- helpers ---------------------------------------------------------
    def now(self) -> float:
        return round(self.tick / TICK_RATE, 3)

    def emit(self, kind: str, **fields) -> None:
        self.events.append({"t": self.now(), "type": kind, **fields})

    def flash(self, text: str, seconds: float = 1.4) -> None:
        self._overlay = text
        self._overlay_until = self.tick + int(seconds * TICK_RATE)

    @property
    def overlay(self) -> str | None:
        return self._overlay

    def award(self, points: int, kind: str = "score", **fields) -> None:
        self.score += points
        self.total += points
        self.tally[kind] = self.tally.get(kind, 0) + 1
        if kind in _EVENT_KINDS:
            self.emit(kind, points=points, score=self.score, **fields)

    def level_up(self, level: int) -> None:
        if level > self.level:
            self.level = level
            self.emit("level", level=level)
            self.flash(f"LEVEL {level}", 1.0)

    def lose_life(self, reason: str = "crash") -> None:
        self.deaths += 1
        self.lives -= 1
        self.emit(reason, lives=max(0, self.lives), score=self.score)
        if self.lives <= 0:
            self.emit("game_over", score=self.score, level=self.level)
            self.flash("GAME OVER", 2.2)
            self.games += 1
            self.score = 0
            self.level = 1
            self.lives = START_LIVES
            self.reset_game()
            return
        self.flash(f"{self.lives} UP", 0.9)
        self.reset_round()

    def hud(self) -> dict:
        return {"score": self.score, "level": self.level, "lives": max(0, self.lives),
                "best": self.best, "stats": dict(self.stats)}

    def frame(self) -> RetroFrame:
        raise NotImplementedError


# ---------------------------------------------------------------------------
# Rắn săn mồi
# ---------------------------------------------------------------------------
_DIRS = ((1, 0), (0, 1), (-1, 0), (0, -1))


class SnakeEngine(Engine):
    game_id = "snake_arena"
    cols, rows = 30, 22

    def reset_game(self) -> None:
        self.eaten = 0
        self.stats = {"apples": 0, "length": 4}
        self.reset_round()

    def reset_round(self) -> None:
        cx, cy = self.cols // 2, self.rows // 2
        self.body = deque((cx - index, cy) for index in range(4))
        self.taken = set(self.body)
        self.heading = (1, 0)
        self.move_at = 0
        self.food = self._spawn_food()

    def _spawn_food(self) -> tuple[int, int]:
        free = [(c, r) for r in range(self.rows) for c in range(self.cols) if (c, r) not in self.taken]
        return self.rng.choice(free) if free else (0, 0)

    def _open(self, cell: tuple[int, int], blocked: set) -> bool:
        c, r = cell
        return 0 <= c < self.cols and 0 <= r < self.rows and cell not in blocked

    def _space(self, start: tuple[int, int], blocked: set, cap: int) -> int:
        """Flood fill capped at cap cells: the snake only needs to know it can still escape."""
        seen = {start}
        queue = deque([start])
        while queue and len(seen) < cap:
            c, r = queue.popleft()
            for dc, dr in _DIRS:
                cell = (c + dc, r + dr)
                if cell not in seen and self._open(cell, blocked):
                    seen.add(cell)
                    queue.append(cell)
        return len(seen)

    def update(self) -> None:
        if self.tick < self.move_at:
            return
        self.move_at = self.tick + max(2, 7 - self.level)
        head = self.body[0]
        blocked = set(self.taken)
        blocked.discard(self.body[-1])  # the tail vacates its cell on this very tick
        back = (-self.heading[0], -self.heading[1])
        cap = len(self.body) + 6
        choice = None
        for direction in _DIRS:
            if direction == back:
                continue
            cell = (head[0] + direction[0], head[1] + direction[1])
            if not self._open(cell, blocked):
                continue
            room = self._space(cell, blocked, cap)
            distance = abs(cell[0] - self.food[0]) + abs(cell[1] - self.food[1])
            # Escape room first, then hunger: a shorter path that traps the snake is worthless.
            key = (room >= min(cap, len(self.body) + 2), -distance, room)
            if choice is None or key > choice[0]:
                choice = (key, direction, cell)
        if choice is None:
            self.lose_life("crash")
            return
        _, self.heading, cell = choice
        self.body.appendleft(cell)
        self.taken.add(cell)
        if cell == self.food:
            self.eaten += 1
            self.stats["apples"] = self.stats.get("apples", 0) + 1
            self.award(10 * self.level, "apple")
            self.level_up(min(9, 1 + self.eaten // 6))
            self.food = self._spawn_food()
        else:
            self.taken.discard(self.body.pop())
        self.stats["length"] = len(self.body)

    def frame(self) -> RetroFrame:
        cells = [(c, r, "player_dim") for c, r in self.body]
        head = self.body[0]
        cells.append((head[0], head[1], "player"))
        cells.append((self.food[0], self.food[1], "food"))
        return RetroFrame(cells, self.hud(), self.overlay)


# ---------------------------------------------------------------------------
# Xếp gạch
# ---------------------------------------------------------------------------
_TETROMINOES = (
    ("I", (((0, 1), (1, 1), (2, 1), (3, 1)), ((2, 0), (2, 1), (2, 2), (2, 3)))),
    ("O", (((1, 0), (2, 0), (1, 1), (2, 1)),)),
    ("T", (((1, 0), (0, 1), (1, 1), (2, 1)), ((1, 0), (1, 1), (2, 1), (1, 2)),
           ((0, 1), (1, 1), (2, 1), (1, 2)), ((1, 0), (0, 1), (1, 1), (1, 2)))),
    ("S", (((1, 0), (2, 0), (0, 1), (1, 1)), ((1, 0), (1, 1), (2, 1), (2, 2)))),
    ("Z", (((0, 0), (1, 0), (1, 1), (2, 1)), ((2, 0), (1, 1), (2, 1), (1, 2)))),
    ("J", (((0, 0), (0, 1), (1, 1), (2, 1)), ((1, 0), (2, 0), (1, 1), (1, 2)),
           ((0, 1), (1, 1), (2, 1), (2, 2)), ((1, 0), (1, 1), (0, 2), (1, 2)))),
    ("L", (((2, 0), (0, 1), (1, 1), (2, 1)), ((1, 0), (1, 1), (1, 2), (2, 2)),
           ((0, 1), (1, 1), (2, 1), (0, 2)), ((0, 0), (1, 0), (1, 1), (1, 2)))),
)
_LINE_SCORE = (0, 100, 300, 500, 800)


class BrickStackEngine(Engine):
    game_id = "brick_stack"
    cols, rows = 10, 20

    def reset_game(self) -> None:
        self.lines = 0
        self.stats = {"lines": 0, "pieces": 0}
        self.reset_round()

    def reset_round(self) -> None:
        self.grid: list[list[str | None]] = [[None] * self.cols for _ in range(self.rows)]
        self.bag: list[int] = []
        self.next_index = self._draw()
        self._spawn()

    def _draw(self) -> int:
        if not self.bag:
            self.bag = list(range(len(_TETROMINOES)))
            self.rng.shuffle(self.bag)
        return self.bag.pop()

    def _states(self, piece: int) -> tuple:
        return _TETROMINOES[piece][1]

    def _spawn(self) -> None:
        self.piece = self.next_index
        self.next_index = self._draw()
        self.rotation = 0
        self.x = (self.cols - 4) // 2
        self.y = -1
        self.drop_at = self.tick + self._gravity()
        self.stats["pieces"] = self.stats.get("pieces", 0) + 1
        if self._collides(self.rotation, self.x, self.y):
            self.lose_life("top_out")
            return
        self.target = self._plan()

    def _gravity(self) -> int:
        return max(1, 8 - self.level)

    def _shape(self, rotation: int) -> tuple[tuple[int, int], ...]:
        states = self._states(self.piece)
        return states[rotation % len(states)]

    def _collides(self, rotation: int, x: int, y: int, grid=None) -> bool:
        grid = self.grid if grid is None else grid
        for dx, dy in self._shape(rotation):
            c, r = x + dx, y + dy
            if c < 0 or c >= self.cols or r >= self.rows:
                return True
            if r >= 0 and grid[r][c] is not None:
                return True
        return False

    def _evaluate(self, rotation: int, x: int) -> float | None:
        """Dellacherie-style heuristic: a flat stack with no buried holes wins."""
        y = -3
        while not self._collides(rotation, x, y + 1):
            y += 1
        grid = [row[:] for row in self.grid]
        for dx, dy in self._shape(rotation):
            c, r = x + dx, y + dy
            if r < 0:
                return None  # the piece would lock above the ceiling
            grid[r][c] = "x"
        kept = [row for row in grid if not all(cell is not None for cell in row)]
        cleared = self.rows - len(kept)
        grid = [[None] * self.cols for _ in range(cleared)] + kept
        heights, holes = [], 0
        for c in range(self.cols):
            height, filled = 0, False
            for r in range(self.rows):
                if grid[r][c] is not None:
                    if not filled:
                        height = self.rows - r
                    filled = True
                elif filled:
                    holes += 1
            heights.append(height)
        bumpiness = sum(abs(heights[i] - heights[i + 1]) for i in range(self.cols - 1))
        return (-0.510066 * sum(heights) + 0.760666 * cleared
                - 0.35663 * holes - 0.184483 * bumpiness)

    def _plan(self) -> tuple[int, int]:
        best = (float("-inf"), self.rotation, self.x)
        for rotation in range(len(self._states(self.piece))):
            for x in range(-2, self.cols):
                if self._collides(rotation, x, -3):
                    continue
                value = self._evaluate(rotation, x)
                if value is not None and value > best[0]:
                    best = (value, rotation, x)
        return best[1], best[2]

    def _lock(self) -> None:
        color = f"p{self.piece}"
        for dx, dy in self._shape(self.rotation):
            c, r = self.x + dx, self.y + dy
            if r >= 0:
                self.grid[r][c] = color
        full = [r for r in range(self.rows) if all(cell is not None for cell in self.grid[r])]
        for r in full:
            self.grid.pop(r)
            self.grid.insert(0, [None] * self.cols)
        if full:
            self.lines += len(full)
            self.stats["lines"] = self.lines
            self.award(_LINE_SCORE[len(full)] * self.level, "lines", count=len(full))
            if len(full) == 4:
                self.flash("TETRIS", 1.0)
            self.level_up(min(12, 1 + self.lines // 10))
        self._spawn()

    def update(self) -> None:
        rotation, x = self.target
        aligned = self.rotation == rotation and self.x == x
        if not aligned:
            states = len(self._states(self.piece))
            if self.rotation != rotation and not self._collides((self.rotation + 1) % states, self.x, self.y):
                self.rotation = (self.rotation + 1) % states
            elif self.x != x:
                step = 1 if x > self.x else -1
                if not self._collides(self.rotation, self.x + step, self.y):
                    self.x += step
                else:
                    self.target = (self.rotation, self.x)  # blocked in flight: drop where it stands
        if self.tick < self.drop_at:
            return
        # Once the piece sits over its target column it slams down, like a held-down soft drop.
        self.drop_at = self.tick + (1 if aligned else self._gravity())
        if self._collides(self.rotation, self.x, self.y + 1):
            self._lock()
        else:
            self.y += 1

    def frame(self) -> RetroFrame:
        cells = [(c, r, color) for r, row in enumerate(self.grid)
                 for c, color in enumerate(row) if color]
        for dx, dy in self._shape(self.rotation):
            r = self.y + dy
            if r >= 0:
                cells.append((self.x + dx, r, f"p{self.piece}"))
        mini = [(dx, dy, f"p{self.next_index}") for dx, dy in self._states(self.next_index)[0]]
        return RetroFrame(cells, self.hud(), self.overlay, mini, (4, 4), "NEXT")


# ---------------------------------------------------------------------------
# Xe tăng
# ---------------------------------------------------------------------------
_FACES = {"up": (0, -1), "down": (0, 1), "left": (-1, 0), "right": (1, 0)}
_FACE_ORDER = ("up", "right", "down", "left")


class TankDuelEngine(Engine):
    game_id = "tank_duel"
    cols, rows = 26, 18
    _WAVE = 8

    def reset_game(self) -> None:
        self.stats = {"kills": 0, "stages": 0}
        self.next_stage()
        self.reset_round()

    def next_stage(self) -> None:
        self._build_map()
        self.remaining = self._WAVE + self.level

    def reset_round(self) -> None:
        """A lost tank respawns into the same stage; only the walls it broke stay broken."""
        self.enemies: list[dict] = []
        self.bullets: list[dict] = []
        self.spawn_at = self.tick + 10
        self.player = {"x": self.cols // 2 - 4, "y": self.rows - 2, "face": "up",
                       "move_at": 0, "fire_at": 0, "side": "p"}

    def _build_map(self) -> None:
        self.terrain: list[list[str | None]] = [[None] * self.cols for _ in range(self.rows)]
        for _ in range(14 + self.level):
            c = self.rng.randrange(2, self.cols - 4)
            r = self.rng.randrange(3, self.rows - 4)
            for dc in range(self.rng.randint(2, 4)):
                for dr in range(self.rng.randint(1, 3)):
                    self.terrain[min(self.rows - 4, r + dr)][min(self.cols - 1, c + dc)] = "wall"
        for _ in range(3):
            c, r = self.rng.randrange(3, self.cols - 3), self.rng.randrange(4, self.rows - 5)
            self.terrain[r][c] = "steel"
            self.terrain[r][min(self.cols - 1, c + 1)] = "steel"
        self.base = (self.cols // 2, self.rows - 1)
        bx, by = self.base
        self.terrain[by][bx] = "base"
        # The eagle keeps its brick collar; enemies must chew through it to win.
        for c, r in ((bx - 1, by), (bx + 1, by), (bx - 1, by - 1), (bx, by - 1), (bx + 1, by - 1)):
            if 0 <= c < self.cols and 0 <= r < self.rows:
                self.terrain[r][c] = "wall"

    def _terrain_at(self, c: int, r: int) -> str | None:
        if 0 <= c < self.cols and 0 <= r < self.rows:
            return self.terrain[r][c]
        return "steel"

    def _blocked(self, c: int, r: int, mover: dict | None = None) -> bool:
        if not (0 <= c < self.cols and 0 <= r < self.rows):
            return True
        if self.terrain[r][c] is not None:
            return True
        for tank in [self.player, *self.enemies]:
            if tank is not mover and tank["x"] == c and tank["y"] == r:
                return True
        return False

    def _fire(self, tank: dict, cooldown: int) -> bool:
        if self.tick < tank["fire_at"]:
            return False
        tank["fire_at"] = self.tick + cooldown
        dc, dr = _FACES[tank["face"]]
        # The shell starts inside the barrel: stepping out first would skip the adjacent wall.
        self.bullets.append({"x": tank["x"], "y": tank["y"], "dx": dc, "dy": dr,
                             "side": tank["side"]})
        return True

    def _clear_line(self, tank: dict, target: tuple[int, int]) -> str | None:
        """Facing needed to hit target with nothing solid in between, else None."""
        tx, ty = target
        if tank["x"] == tx:
            step = 1 if ty > tank["y"] else -1
            for r in range(tank["y"] + step, ty, step):
                if self.terrain[r][tank["x"]] is not None:
                    return None
            return "down" if step > 0 else "up"
        if tank["y"] == ty:
            step = 1 if tx > tank["x"] else -1
            for c in range(tank["x"] + step, tx, step):
                if self.terrain[tank["y"]][c] is not None:
                    return None
            return "right" if step > 0 else "left"
        return None

    def _drive(self, tank: dict, target: tuple[int, int], interval: int) -> None:
        if self.tick < tank["move_at"]:
            return
        tank["move_at"] = self.tick + interval
        dx, dy = target[0] - tank["x"], target[1] - tank["y"]
        options = []
        if abs(dx) >= abs(dy):
            options = [("right" if dx > 0 else "left", abs(dx)), ("down" if dy > 0 else "up", abs(dy))]
        else:
            options = [("down" if dy > 0 else "up", abs(dy)), ("right" if dx > 0 else "left", abs(dx))]
        for face, distance in options:
            if not distance:
                continue
            dc, dr = _FACES[face]
            ahead = (tank["x"] + dc, tank["y"] + dr)
            if self._terrain_at(*ahead) == "wall":
                tank["face"] = face  # a brick in the way is a target, not a detour
                if self._fire(tank, 14):
                    return
                continue  # gun still hot: try the other axis instead of idling
            if not self._blocked(*ahead, mover=tank):
                tank["face"] = face
                tank["x"], tank["y"] = ahead
                return
        for face in _FACE_ORDER:
            dc, dr = _FACES[face]
            if not self._blocked(tank["x"] + dc, tank["y"] + dr, mover=tank):
                tank["face"] = face
                tank["x"] += dc
                tank["y"] += dr
                return

    def _spawn_enemy(self) -> None:
        if self.remaining <= 0 or len(self.enemies) >= 4 or self.tick < self.spawn_at:
            return
        self.spawn_at = self.tick + max(24, 70 - self.level * 4)
        for c in (1, self.cols // 2, self.cols - 2):
            if not self._blocked(c, 0):
                self.remaining -= 1
                self.enemies.append({"x": c, "y": 0, "face": "down", "move_at": self.tick,
                                     "fire_at": self.tick + 10, "side": "e",
                                     "hunt": self.rng.random() < 0.45})
                return

    def _advance_bullets(self) -> None:
        for bullet in list(self.bullets):
            for _ in range(2):  # shells cross two cells per tick
                bullet["x"] += bullet["dx"]
                bullet["y"] += bullet["dy"]
                c, r = bullet["x"], bullet["y"]
                if not (0 <= c < self.cols and 0 <= r < self.rows):
                    self.bullets.remove(bullet)
                    break
                cell = self.terrain[r][c]
                if cell == "wall":
                    self.terrain[r][c] = None
                    self.bullets.remove(bullet)
                    break
                if cell == "steel":
                    self.bullets.remove(bullet)
                    break
                if cell == "base":
                    self.bullets.remove(bullet)
                    self.emit("base_lost")
                    self.flash("BASE DOWN", 1.6)
                    self.lose_life("base_lost")
                    return
                if bullet["side"] == "p":
                    hit = next((e for e in self.enemies if e["x"] == c and e["y"] == r), None)
                    if hit is not None:
                        self.enemies.remove(hit)
                        self.bullets.remove(bullet)
                        self.stats["kills"] = self.stats.get("kills", 0) + 1
                        self.award(100 * self.level, "kill")
                        break
                elif self.player["x"] == c and self.player["y"] == r:
                    self.bullets.remove(bullet)
                    self.lose_life("tank_lost")
                    return

    def update(self) -> None:
        self._advance_bullets()
        self._spawn_enemy()
        target = min(self.enemies, key=lambda e: abs(e["x"] - self.player["x"]) + abs(e["y"] - self.player["y"]),
                     default=None)
        if target is not None:
            face = self._clear_line(self.player, (target["x"], target["y"]))
            if face:
                self.player["face"] = face
                self._fire(self.player, 8)
            else:
                self._drive(self.player, (target["x"], target["y"]), max(2, 5 - self.level // 3))
        for enemy in list(self.enemies):
            goal = (self.player["x"], self.player["y"]) if enemy["hunt"] else self.base
            face = self._clear_line(enemy, goal)
            if face:
                enemy["face"] = face
                self._fire(enemy, max(14, 34 - self.level * 2))
            else:
                self._drive(enemy, goal, max(4, 8 - self.level // 2))
        if self.remaining <= 0 and not self.enemies:
            self.stats["stages"] = self.stats.get("stages", 0) + 1
            self.award(500 * self.level, "stage_clear")
            self.level_up(min(12, self.level + 1))
            self.flash("STAGE CLEAR", 1.6)
            self.next_stage()
            self.reset_round()

    def frame(self) -> RetroFrame:
        cells = [(c, r, {"wall": "wall", "steel": "steel", "base": "base"}[cell])
                 for r, row in enumerate(self.terrain) for c, cell in enumerate(row) if cell]
        for tank, body, turret in ((self.player, "player", "player_dim"),
                                   *((enemy, "enemy", "enemy_dim") for enemy in self.enemies)):
            cells.append((tank["x"], tank["y"], body))
            dc, dr = _FACES[tank["face"]]
            cells.append((tank["x"] + dc, tank["y"] + dr, turret))
        cells.extend((bullet["x"], bullet["y"], "shot") for bullet in self.bullets)
        hud = self.hud()
        hud["stats"] = {**hud["stats"], "left": max(0, self.remaining) + len(self.enemies)}
        return RetroFrame([cell for cell in cells if 0 <= cell[0] < self.cols and 0 <= cell[1] < self.rows],
                          hud, self.overlay)


# ---------------------------------------------------------------------------
# Đập gạch
# ---------------------------------------------------------------------------
class BrickBreakerEngine(Engine):
    game_id = "brick_breaker"
    cols, rows = 24, 18
    _BRICK_W = 2
    _BRICK_ROWS = 5

    def reset_game(self) -> None:
        self.stats = {"bricks": 0, "stages": 0}
        self._build_wall()
        self.reset_round()

    def reset_round(self) -> None:
        """Only the ball and paddle reset on a miss; the wall keeps the damage."""
        self.paddle_w = max(3, 6 - self.level // 3)
        self.paddle = (self.cols - self.paddle_w) / 2
        self.ball = [self.cols / 2, self.rows - 4.0]
        speed = 0.62 + 0.05 * self.level
        self.velocity = [speed * self.rng.choice((-1, 1)), -speed]
        self.aim = self.paddle

    def _build_wall(self) -> None:
        columns = self.cols // self._BRICK_W
        self.bricks = {(c, r): f"p{(r + self.level) % 7}"
                       for r in range(self._BRICK_ROWS) for c in range(columns)
                       if (r + c + self.level) % 7 or r == 0}

    def _predict(self) -> float:
        """Where the ball meets the paddle row, reflecting off the side walls."""
        x, y = self.ball
        vx, vy = self.velocity
        if vy <= 0:
            return self.cols / 2
        steps = (self.rows - 2 - y) / vy
        x += vx * steps
        span = 2 * (self.cols - 1)
        x = abs(x) % span
        return x if x < self.cols else span - x

    def update(self) -> None:
        if self.velocity[1] > 0 and self.tick % 6 == 0:
            # A human hand is late and a little off; that is what eventually costs a life.
            drift = self.rng.uniform(-1.0, 1.0) * (0.7 + 0.25 * self.level)
            self.aim = self._predict() + drift - self.paddle_w / 2
        speed = 0.9 + 0.07 * self.level
        self.paddle = min(max(0.0, self.paddle + max(-speed, min(speed, self.aim - self.paddle))),
                          self.cols - self.paddle_w)
        for axis in (0, 1):
            self.ball[axis] += self.velocity[axis]
        if self.ball[0] < 0.5:
            self.ball[0], self.velocity[0] = 0.5, abs(self.velocity[0])
        elif self.ball[0] > self.cols - 1.5:
            self.ball[0], self.velocity[0] = self.cols - 1.5, -abs(self.velocity[0])
        if self.ball[1] < 0.5:
            self.ball[1], self.velocity[1] = 0.5, abs(self.velocity[1])
        cell = (int(self.ball[0]) // self._BRICK_W, int(self.ball[1]))
        if cell in self.bricks:
            del self.bricks[cell]
            self.velocity[1] = -self.velocity[1]
            self.stats["bricks"] = self.stats.get("bricks", 0) + 1
            self.award((self._BRICK_ROWS - cell[1]) * 10 * self.level, "brick")
            if not self.bricks:
                self.stats["stages"] = self.stats.get("stages", 0) + 1
                self.award(500 * self.level, "stage_clear")
                self.level_up(min(12, self.level + 1))
                self.flash("STAGE CLEAR", 1.4)
                self._build_wall()
                self.reset_round()
            return
        paddle_row = self.rows - 2
        if self.velocity[1] > 0 and paddle_row <= self.ball[1] <= paddle_row + 1:
            if self.paddle - 0.5 <= self.ball[0] <= self.paddle + self.paddle_w + 0.5:
                offset = (self.ball[0] - (self.paddle + self.paddle_w / 2)) / max(1.0, self.paddle_w / 2)
                magnitude = abs(self.velocity[1])
                self.velocity = [magnitude * max(-1.1, min(1.1, offset * 1.2)), -magnitude]
                self.ball[1] = paddle_row - 0.1
        elif self.ball[1] > self.rows - 0.5:
            self.lose_life("ball_lost")

    def frame(self) -> RetroFrame:
        cells = [(c * self._BRICK_W + dc, r, color)
                 for (c, r), color in self.bricks.items() for dc in range(self._BRICK_W)]
        cells.extend((int(self.paddle) + offset, self.rows - 1, "player") for offset in range(self.paddle_w))
        cells.append((int(self.ball[0]), int(self.ball[1]), "shot"))
        return RetroFrame([cell for cell in cells if 0 <= cell[0] < self.cols], self.hud(), self.overlay)


# ---------------------------------------------------------------------------
# Bắn ruồi
# ---------------------------------------------------------------------------
class StarDefenderEngine(Engine):
    game_id = "star_defender"
    cols, rows = 22, 18
    _WIDE, _DEEP = 8, 4

    def reset_game(self) -> None:
        self.stats = {"kills": 0, "waves": 0}
        self.next_wave()
        self.reset_round()

    def next_wave(self) -> None:
        self.invaders = {(c, r) for r in range(self._DEEP) for c in range(self._WIDE)}
        self.offset = [2, 1]
        self.march = 1
        self.march_at = self.tick + 20
        self.shields = {(c, self.rows - 5): 2 for base in (3, 10, 17) for c in (base - 1, base, base + 1)}

    def reset_round(self) -> None:
        """A lost ship costs a life, not the progress already made against the wave."""
        self.ship = self.cols // 2
        self.shot: list[int] | None = None
        self.bombs: list[list[int]] = []
        self.move_at = 0
        self.fire_at = 0

    def _cell(self, invader: tuple[int, int]) -> tuple[int, int]:
        return self.offset[0] + invader[0] * 2, self.offset[1] + invader[1] * 2

    def _march(self) -> None:
        if self.tick < self.march_at:
            return
        alive = max(1, len(self.invaders))
        self.march_at = self.tick + max(3, int(24 * alive / (self._WIDE * self._DEEP)) + 3 - self.level)
        columns = [self._cell(invader)[0] for invader in self.invaders]
        if columns and (max(columns) + self.march > self.cols - 2 or min(columns) + self.march < 1):
            self.march = -self.march
            self.offset[1] += 1
            if any(self._cell(invader)[1] >= self.rows - 2 for invader in self.invaders):
                self.lose_life("overrun")
                return
        else:
            self.offset[0] += self.march
        if self.invaders and len(self.bombs) < 1 + self.level // 3 and self.rng.random() < 0.3:
            # Only the front rank drops bombs, and it aims at the column nearest the ship.
            aimed = self.rng.random() < 0.7
            bomber = max(sorted(self.invaders),
                         key=lambda inv: (inv[1], -abs(self._cell(inv)[0] - self.ship) if aimed else 0))
            c, r = self._cell(bomber)
            self.bombs.append([c, r + 1])

    def update(self) -> None:
        self._march()
        if self.shot is not None:
            for _ in range(2):
                self.shot[1] -= 1
                hit = next((inv for inv in sorted(self.invaders) if self._cell(inv) == tuple(self.shot)), None)
                if hit is not None:
                    self.invaders.discard(hit)
                    self.stats["kills"] = self.stats.get("kills", 0) + 1
                    self.award((self._DEEP - hit[1]) * 10 * self.level, "kill")
                    self.shot = None
                    break
                key = (self.shot[0], self.shot[1])
                if key in self.shields:
                    self.shields[key] -= 1
                    if self.shields[key] <= 0:
                        del self.shields[key]
                    self.shot = None
                    break
                if self.shot[1] < 0:
                    self.shot = None
                    break
        if self.tick % 2 == 0:
            for bomb in list(self.bombs):
                bomb[1] += 1
                key = (bomb[0], bomb[1])
                if key in self.shields:
                    self.shields[key] -= 1
                    if self.shields[key] <= 0:
                        del self.shields[key]
                    self.bombs.remove(bomb)
                elif bomb[1] >= self.rows - 1:
                    if abs(bomb[0] - self.ship) <= 1:
                        self.bombs.remove(bomb)
                        self.lose_life("ship_lost")
                        return
                    self.bombs.remove(bomb)
        if not self.invaders:
            self.stats["waves"] = self.stats.get("waves", 0) + 1
            self.award(300 * self.level, "wave_clear")
            self.level_up(min(12, self.level + 1))
            self.flash("WAVE CLEAR", 1.4)
            self.next_wave()
            self.reset_round()
            return
        danger = next((bomb for bomb in self.bombs
                       if abs(bomb[0] - self.ship) <= 2 and bomb[1] > self.rows - 11), None)
        target = danger[0] + (4 if danger[0] <= self.ship else -4) if danger else self._cell(max(self.invaders, key=lambda inv: (inv[1], -inv[0])))[0]
        if self.tick >= self.move_at:
            self.move_at = self.tick + (1 if danger else max(1, 3 - self.level // 4))
            if target > self.ship:
                self.ship = min(self.cols - 2, self.ship + 1)
            elif target < self.ship:
                self.ship = max(1, self.ship - 1)
        if self.shot is None and not danger and self.tick >= self.fire_at:
            self.fire_at = self.tick + max(3, 8 - self.level)
            self.shot = [self.ship, self.rows - 2]

    def frame(self) -> RetroFrame:
        cells = [(*self._cell(inv), "enemy" if inv[1] else "enemy_dim") for inv in sorted(self.invaders)]
        cells.extend((c, r, "shield") for (c, r), hp in self.shields.items())
        cells.append((self.ship, self.rows - 1, "player"))
        cells.extend(((self.ship - 1, self.rows - 1, "player_dim"), (self.ship + 1, self.rows - 1, "player_dim")))
        if self.shot is not None:
            cells.append((self.shot[0], self.shot[1], "shot"))
        cells.extend((bomb[0], bomb[1], "food") for bomb in self.bombs)
        hud = self.hud()
        hud["stats"] = {**hud["stats"], "left": len(self.invaders)}
        return RetroFrame([cell for cell in cells if 0 <= cell[0] < self.cols and 0 <= cell[1] < self.rows],
                          hud, self.overlay)


# ---------------------------------------------------------------------------
# Đua xe
# ---------------------------------------------------------------------------
class PixelDashEngine(Engine):
    game_id = "pixel_dash"
    cols, rows = 12, 20
    _LANES = 5

    def reset_game(self) -> None:
        self.stats = {"passed": 0, "distance": 0}
        self.reset_round()

    def reset_round(self) -> None:
        self.lane = self._LANES // 2
        self.traffic: list[list[int]] = []
        self.scroll_at = 0
        self.shift_at = 0
        self.spawn_row = -3

    def _lane_x(self, lane: int) -> int:
        return 1 + lane * 2

    def _free(self, lane: int) -> bool:
        return all(car[0] != lane or car[1] > self.rows - 3 or car[1] < -1 for car in self.traffic)

    def _gap(self, lane: int) -> int:
        """Rows of clear road ahead of the bumper in this lane."""
        ahead = [self.rows - 4 - car[1] for car in self.traffic
                 if car[0] == lane and car[1] <= self.rows - 4]
        return min(ahead) if ahead else self.rows

    def update(self) -> None:
        if self.tick >= self.scroll_at:
            self.scroll_at = self.tick + max(1, 6 - self.level // 2)
            self.stats["distance"] = self.stats.get("distance", 0) + 1
            for car in list(self.traffic):
                car[1] += 1
                if car[1] > self.rows:
                    self.traffic.remove(car)
                    self.stats["passed"] = self.stats.get("passed", 0) + 1
                    self.award(10 * self.level, "overtake")
                    self.level_up(min(12, 1 + self.stats["passed"] // 12))
            self.spawn_row += 1
            if self.spawn_row >= 0 and len(self.traffic) < 4:
                self.spawn_row = -self.rng.randint(3, 6)
                lanes = [lane for lane in range(self._LANES) if self._free(lane)]
                if len(lanes) > 1:  # never block every lane at once
                    self.traffic.append([self.rng.choice(lanes), -2])
        if self.tick >= self.shift_at:
            self.shift_at = self.tick + 2
            reachable = [lane for lane in (self.lane - 1, self.lane, self.lane + 1)
                         if 0 <= lane < self._LANES]
            # Only swerve when the lane actually closes up, and only into a roomier one.
            goal = max(reachable, key=lambda lane: (self._gap(lane), lane == self.lane))
            if self._gap(self.lane) < 7 and self._gap(goal) > self._gap(self.lane):
                self.lane += 1 if goal > self.lane else -1
        for car in self.traffic:
            if car[0] == self.lane and self.rows - 4 <= car[1] <= self.rows - 2:
                self.traffic.remove(car)
                self.lose_life("crash")
                return

    def _car(self, lane: int, row: int, body: str, glass: str) -> list[tuple[int, int, str]]:
        x = self._lane_x(lane)
        return [(x, row, body), (x + 1, row, body), (x, row + 1, glass), (x + 1, row + 1, glass),
                (x, row + 2, body), (x + 1, row + 2, body)]

    def frame(self) -> RetroFrame:
        cells = [(0, r, "road") for r in range(self.rows)]
        cells.extend((self.cols - 1, r, "road") for r in range(self.rows))
        cells.extend((self._lane_x(lane) + 1, r, "trail")
                     for lane in range(self._LANES - 1)
                     for r in range(self.rows) if (r + self.stats.get("distance", 0)) % 4 < 2)
        for car in self.traffic:
            cells.extend(self._car(car[0], car[1], "enemy", "enemy_dim"))
        cells.extend(self._car(self.lane, self.rows - 4, "player", "player_dim"))
        return RetroFrame([cell for cell in cells if 0 <= cell[1] < self.rows], self.hud(), self.overlay)


# ---------------------------------------------------------------------------
# Pac-Man
# ---------------------------------------------------------------------------
class PacmanEngine(Engine):
    game_id = "pacman_maze"
    cols, rows = 25, 19

    def reset_game(self) -> None:
        self.stats = {"pellets": 0, "ghosts": 0}
        self._build_maze()
        self.reset_round()

    def _build_maze(self) -> None:
        self.walls = {(c, r) for c in range(self.cols) for r in range(self.rows)
                      if c in (0, self.cols - 1) or r in (0, self.rows - 1)}
        for c in range(3, self.cols - 3, 4):
            for r in range(2, self.rows - 2):
                if r not in (4, 9, 14):
                    self.walls.add((c, r))
        for r in (4, 9, 14):
            for c in range(2, self.cols - 2):
                if c not in (5, 10, 15, 20):
                    self.walls.add((c, r))
        self.start = (1, 1)
        self.ghost_home = (self.cols // 2, self.rows // 2)
        self.walls.discard(self.start)
        self.walls.discard(self.ghost_home)
        self.pellets = {(c, r) for c in range(1, self.cols - 1) for r in range(1, self.rows - 1)
                        if (c, r) not in self.walls and (c, r) not in (self.start, self.ghost_home)}
        self.power = {(1, self.rows - 2), (self.cols - 2, 1),
                      (self.cols - 2, self.rows - 2), (1, 1)}
        self.pellets.update(self.power)

    def reset_round(self) -> None:
        self.player = self.start
        self.ghosts = [[self.ghost_home[0] - 1, self.ghost_home[1]],
                       [self.ghost_home[0] + 1, self.ghost_home[1]]]
        self.frightened_until = 0
        self.move_at = self.tick

    def _open(self, cell: tuple[int, int]) -> bool:
        return 0 <= cell[0] < self.cols and 0 <= cell[1] < self.rows and cell not in self.walls

    def _next_step(self, origin: tuple[int, int], target: tuple[int, int], flee: bool = False) -> tuple[int, int]:
        choices = [(origin[0] + dc, origin[1] + dr) for dc, dr in _DIRS]
        choices = [cell for cell in choices if self._open(cell)]
        return min(choices, key=lambda cell: (-(abs(cell[0] - target[0]) + abs(cell[1] - target[1]))
                                               if flee else abs(cell[0] - target[0]) + abs(cell[1] - target[1]), cell))

    def update(self) -> None:
        if self.tick < self.move_at:
            return
        self.move_at = self.tick + max(2, 5 - self.level // 2)
        if self.pellets:
            self.player = self._next_step(self.player, min(self.pellets,
                key=lambda cell: abs(cell[0] - self.player[0]) + abs(cell[1] - self.player[1])))
        if self.player in self.pellets:
            self.pellets.remove(self.player)
            self.stats["pellets"] += 1
            self.award(50 if self.player in self.power else 10, "pellet")
            if self.player in self.power:
                self.frightened_until = self.tick + 90
        for ghost in list(self.ghosts):
            current = (ghost[0], ghost[1])
            flee = self.tick < self.frightened_until
            ghost[:] = self._next_step(current, self.player, flee)
            if tuple(ghost) == self.player:
                if flee:
                    self.ghosts.remove(ghost)
                    self.stats["ghosts"] += 1
                    self.award(200 * self.level, "ghost")
                else:
                    self.lose_life("crash")
                    return
        if not self.pellets:
            self.award(500 * self.level, "stage_clear")
            self.level_up(min(12, self.level + 1))
            self.flash("MAZE CLEAR", 1.4)
            self._build_maze()
            self.reset_round()

    def frame(self) -> RetroFrame:
        cells = [(c, r, "wall") for c, r in self.walls]
        cells.extend((c, r, "food") for c, r in self.pellets)
        cells.extend((ghost[0], ghost[1], "shield" if self.tick < self.frightened_until else "enemy")
                     for ghost in self.ghosts)
        cells.append((*self.player, "player"))
        return RetroFrame(cells, self.hud(), self.overlay)


# ---------------------------------------------------------------------------
# Phi thuyền bắn gà
# ---------------------------------------------------------------------------
class ChickenShooterEngine(Engine):
    game_id = "chicken_shooter"
    cols, rows = 24, 18

    def reset_game(self) -> None:
        self.stats = {"chickens": 0, "waves": 0}
        self.next_wave()
        self.reset_round()

    def next_wave(self) -> None:
        self.chickens = [[c * 3 + 2, r * 2 + 1] for r in range(3) for c in range(7)]
        self.direction = 1
        self.move_at = self.tick + 10

    def reset_round(self) -> None:
        self.ship = self.cols // 2
        self.shots: list[list[int]] = []
        self.eggs: list[list[int]] = []
        self.fire_at = self.tick

    def update(self) -> None:
        if self.tick >= self.move_at:
            self.move_at = self.tick + max(3, 12 - self.level)
            edge = any(chicken[0] + self.direction in (0, self.cols - 1) for chicken in self.chickens)
            if edge:
                self.direction *= -1
                for chicken in self.chickens:
                    chicken[1] += 1
            else:
                for chicken in self.chickens:
                    chicken[0] += self.direction
            if self.chickens and self.rng.random() < 0.75:
                source = min(self.chickens, key=lambda chicken: abs(chicken[0] - self.ship))
                self.eggs.append([source[0], source[1] + 1])
        target = min(self.chickens, key=lambda chicken: abs(chicken[0] - self.ship), default=[self.ship, 0])
        self.ship += 1 if target[0] > self.ship else -1 if target[0] < self.ship else 0
        self.ship = max(1, min(self.cols - 2, self.ship))
        if self.tick >= self.fire_at:
            self.fire_at = self.tick + max(3, 7 - self.level // 2)
            self.shots.append([self.ship, self.rows - 2])
        for shot in list(self.shots):
            shot[1] -= 2
            hit = next((chicken for chicken in self.chickens if chicken == shot), None)
            if hit:
                self.chickens.remove(hit)
                self.shots.remove(shot)
                self.stats["chickens"] += 1
                self.award(25 * self.level, "chicken")
            elif shot[1] < 0:
                self.shots.remove(shot)
        for egg in list(self.eggs):
            egg[1] += 1
            if egg[1] >= self.rows - 1:
                self.eggs.remove(egg)
                if abs(egg[0] - self.ship) <= 1:
                    self.lose_life("ship_lost")
                    return
        if not self.chickens:
            self.stats["waves"] += 1
            self.award(400 * self.level, "wave_clear")
            self.level_up(min(12, self.level + 1))
            self.flash("WAVE CLEAR", 1.2)
            self.next_wave()

    def frame(self) -> RetroFrame:
        cells = [(*chicken, "enemy") for chicken in self.chickens]
        cells.extend((shot[0], shot[1], "shot") for shot in self.shots)
        cells.extend((egg[0], egg[1], "food") for egg in self.eggs)
        cells.extend(((self.ship - 1, self.rows - 1, "player_dim"), (self.ship, self.rows - 2, "player"),
                      (self.ship + 1, self.rows - 1, "player_dim")))
        return RetroFrame(cells, self.hud(), self.overlay)


# ---------------------------------------------------------------------------
# Phi thuyền vượt không gian
# ---------------------------------------------------------------------------
class SpaceshipVoyagerEngine(Engine):
    game_id = "spaceship_voyager"
    cols, rows = 26, 18

    def reset_game(self) -> None:
        self.stats = {"distance": 0, "asteroids": 0, "bosses": 0, "skills": 0}
        self.stars = [[self.rng.randrange(self.cols), self.rng.randrange(self.rows)] for _ in range(28)]
        self.asteroids: list[list[int]] = []
        self.shots: list[list[int]] = []
        self.boss: list[int] | None = None
        self.boss_kind = 0
        self.boss_index = 0
        self.boss_hp = 0
        self.boss_at = 180
        self.boss_skill_at = 0
        self.spawn_at = 0
        self.fire_at = 0
        self.reset_round()

    def reset_round(self) -> None:
        self.ship = [self.cols // 2, self.rows - 4]
        self.shots = []
        self.asteroids = []
        self.boss = None
        self.boss_hp = 0

    def _spawn_asteroid(self) -> None:
        lane = self.rng.randrange(1, self.cols - 1)
        self.asteroids.append([lane, 0, 1 + self.rng.randrange(2)])

    def _spawn_boss(self) -> None:
        self.boss_kind = self.boss_index % 3
        self.boss_index += 1
        self.boss = [self.cols // 2, 2, 1]
        self.boss_hp = 10 + self.level * 3 + self.boss_kind * 4
        self.boss_skill_at = self.tick + 12
        names = ("IRON COMET", "VOID WARDEN", "NOVA HIVE")
        self.flash(f"BOSS: {names[self.boss_kind]}", 1.2)

    def _boss_skill(self) -> None:
        """Boss attacks are deterministic patterns, each requiring a distinct evasive route."""
        if not self.boss:
            return
        x, y, _ = self.boss
        self.stats["skills"] += 1
        if self.boss_kind == 0:
            for lane in (x - 2, x, x + 2):
                self.asteroids.append([max(0, min(self.cols - 1, lane)), y + 2, 1])
            skill = "COMET BARRAGE"
        elif self.boss_kind == 1:
            gap = (self.tick // 7 + self.boss_index * 5) % (self.cols - 4) + 2
            for lane in range(1, self.cols - 1, 2):
                if abs(lane - gap) > 1:
                    self.asteroids.append([lane, y + 1, 1])
            skill = "VOID CURTAIN"
        else:
            for offset in range(-3, 4, 2):
                self.asteroids.append([max(0, min(self.cols - 1, x + offset)), y + abs(offset) // 2, 2])
            skill = "NOVA SPIRAL"
        self.emit("boss_skill", boss=self.boss_kind, skill=skill)
        self.flash(skill, 0.55)

    def update(self) -> None:
        # Star and asteroid motion continually pull toward the player, creating forward flight.
        for star in self.stars:
            star[1] += 1
            if star[1] >= self.rows:
                star[:] = [self.rng.randrange(self.cols), 0]
        self.stats["distance"] += 1
        if self.boss is None and self.tick >= self.spawn_at:
            self.spawn_at = self.tick + max(5, 14 - self.level)
            self._spawn_asteroid()
        if self.boss is None and self.tick >= self.boss_at:
            self._spawn_boss()
        if self.boss:
            self.boss[0] += self.boss[2]
            if self.boss[0] in (2, self.cols - 3):
                self.boss[2] *= -1
            if self.tick >= self.boss_skill_at:
                self.boss_skill_at = self.tick + (12, 18, 15)[self.boss_kind]
                self._boss_skill()
        for asteroid in list(self.asteroids):
            asteroid[1] += asteroid[2]
            if asteroid[1] >= self.rows:
                self.asteroids.remove(asteroid)
                continue
            if abs(asteroid[0] - self.ship[0]) <= 1 and abs(asteroid[1] - self.ship[1]) <= 1:
                self.lose_life("crash")
                return
        danger = next((rock for rock in self.asteroids if rock[1] >= self.ship[1] - 4
                       and abs(rock[0] - self.ship[0]) <= 2), None)
        target_x = (danger[0] + (3 if danger[0] <= self.ship[0] else -3)) if danger else (self.boss[0] if self.boss else self.cols // 2)
        target_y = self.rows - 6 if danger else self.rows - 4 + (self.tick // 22) % 3
        if self.boss_kind == 2 and self.boss and self.tick % 30 < 10:
            target_y = self.rows - 8
        self.ship[0] += 1 if target_x > self.ship[0] else -1 if target_x < self.ship[0] else 0
        self.ship[1] += 1 if target_y > self.ship[1] else -1 if target_y < self.ship[1] else 0
        self.ship[0] = max(1, min(self.cols - 2, self.ship[0]))
        self.ship[1] = max(3, min(self.rows - 2, self.ship[1]))
        if self.tick >= self.fire_at:
            self.fire_at = self.tick + max(2, 6 - self.level // 2)
            self.shots.append([self.ship[0], self.ship[1] - 1])
        for shot in list(self.shots):
            shot[1] -= 2
            hit = next((rock for rock in self.asteroids if abs(rock[0] - shot[0]) <= 1 and rock[1] == shot[1]), None)
            if hit:
                self.asteroids.remove(hit); self.shots.remove(shot)
                self.stats["asteroids"] += 1; self.award(20 * self.level, "asteroid")
            elif self.boss and abs(self.boss[0] - shot[0]) <= 2 and abs(self.boss[1] - shot[1]) <= 1:
                self.shots.remove(shot); self.boss_hp -= 1; self.award(15 * self.level, "boss_hit")
                if self.boss_hp <= 0:
                    self.stats["bosses"] += 1; self.award(800 * self.level, "boss_down")
                    self.level_up(min(12, self.level + 1)); self.flash("BOSS DOWN", 1.4)
                    self.boss = None; self.boss_at = self.tick + 180
            elif shot[1] < 0:
                self.shots.remove(shot)

    def frame(self) -> RetroFrame:
        cells = [(star[0], star[1], "trail") for star in self.stars]
        cells.extend((rock[0], rock[1], "food") for rock in self.asteroids)
        cells.extend((shot[0], shot[1], "shot") for shot in self.shots)
        if self.boss:
            boss_color = ("enemy", "shield", "food")[self.boss_kind]
            cells.extend((self.boss[0] + dx, self.boss[1] + dy, boss_color)
                         for dx in range(-2, 3) for dy in range(2))
        x, y = self.ship
        cells.extend(((x, y - 1, "player"), (x - 1, y, "player_dim"), (x, y, "player"),
                      (x + 1, y, "player_dim"), (x, y + 1, "trail")))
        hud = self.hud()
        if self.boss:
            hud["stats"] = {**hud["stats"], "boss_hp": self.boss_hp}
        return RetroFrame(cells, hud, self.overlay)


# ---------------------------------------------------------------------------
# Flappy Bird
# ---------------------------------------------------------------------------
class FlappyBirdEngine(Engine):
    game_id = "flappy_bird"
    cols, rows = 22, 18

    def reset_game(self) -> None:
        self.stats = {"pipes": 0, "distance": 0}
        self.reset_round()

    def reset_round(self) -> None:
        self.bird_y = self.rows / 2
        self.velocity = 0.0
        self.pipes: list[list[int]] = []
        self.scroll_at = self.tick
        self.flap_at = self.tick
        self.spawn_in = 8

    def update(self) -> None:
        if self.tick >= self.flap_at:
            self.flap_at = self.tick + max(4, 8 - self.level // 2)
            next_pipe = next((pipe for pipe in self.pipes if pipe[0] >= 5), None)
            if next_pipe and self.bird_y > next_pipe[1] + 2:
                self.velocity = -1.25
            elif not next_pipe and self.bird_y > self.rows / 2 + 1:
                self.velocity = -1.1
        self.velocity = min(1.2, self.velocity + 0.18)
        self.bird_y += self.velocity
        if self.tick >= self.scroll_at:
            self.scroll_at = self.tick + max(2, 5 - self.level // 3)
            self.stats["distance"] += 1
            self.spawn_in -= 1
            if self.spawn_in <= 0:
                gap = max(4, 6 - self.level // 3)
                self.pipes.append([self.cols - 1, self.rng.randint(2, self.rows - gap - 2), gap, 0])
                self.spawn_in = 9
            for pipe in list(self.pipes):
                pipe[0] -= 1
                if pipe[0] == 4 and not pipe[3]:
                    pipe[3] = 1
                    self.stats["pipes"] += 1
                    self.award(100 * self.level, "pipe")
                    self.level_up(min(12, 1 + self.stats["pipes"] // 8))
                if pipe[0] < 0:
                    self.pipes.remove(pipe)
        bird_row = int(self.bird_y)
        if bird_row < 0 or bird_row >= self.rows or any(pipe[0] in (4, 5) and not (pipe[1] <= bird_row < pipe[1] + pipe[2]) for pipe in self.pipes):
            self.lose_life("crash")

    def frame(self) -> RetroFrame:
        cells = [(c, r, "wall") for c, gap_top, gap, _ in self.pipes for r in range(self.rows)
                 if c >= 0 and (r < gap_top or r >= gap_top + gap)]
        cells.append((4, int(self.bird_y), "food"))
        return RetroFrame(cells, self.hud(), self.overlay)


# ---------------------------------------------------------------------------
# Catalog, tag generation and the deterministic replay envelope
# ---------------------------------------------------------------------------
RETRO_GAMES: tuple[RetroSpec, ...] = (
    RetroSpec("snake_arena", "Rắn Săn Mồi", "allowed_with_safe_area",
              "Con rắn tự săn mồi, dài ra và tăng tốc theo từng cấp.",
              (("apples", "Mồi"), ("length", "Độ dài"))),
    RetroSpec("brick_stack", "Xếp Gạch", "allowed_with_safe_area",
              "Tetris cổ điển: khối rơi, xếp kín hàng và phá hàng liên tiếp.",
              (("lines", "Hàng"), ("pieces", "Khối"))),
    RetroSpec("tank_duel", "Xe Tăng 90", "default_off",
              "Xe tăng bắn phá tường gạch, diệt địch và giữ căn cứ.",
              (("kills", "Diệt"), ("stages", "Màn"))),
    RetroSpec("brick_breaker", "Đập Gạch", "allowed_with_safe_area",
              "Thanh trượt đỡ bóng, phá sạch tường gạch nhiều màu.",
              (("bricks", "Gạch"), ("stages", "Màn"))),
    RetroSpec("star_defender", "Bắn Ruồi", "default_off",
              "Đội hình địch tiến xuống, phi thuyền né bom và bắn trả.",
              (("kills", "Hạ"), ("waves", "Đợt"))),
    RetroSpec("pixel_dash", "Đua Xe", "forbidden",
              "Xe lách qua dòng xe ngược chiều, càng lâu càng nhanh.",
              (("passed", "Vượt"), ("distance", "Quãng"))),
    RetroSpec("pacman_maze", "Pac-Man", "allowed_with_safe_area",
              "Ăn chấm trong mê cung, né ma và săn ma khi nhặt viên năng lượng.",
              (("pellets", "Chấm"), ("ghosts", "Ma"))),
    RetroSpec("chicken_shooter", "Phi Thuyền Bắn Gà", "default_off",
               "Phi thuyền tự né trứng, bắn đội hình gà không gian theo từng đợt.",
               (("chickens", "Gà hạ"), ("waves", "Đợt"))),
    RetroSpec("spaceship_voyager", "Phi Thuyền", "default_off",
              "Bay xuyên trường sao, né thiên thạch và đối đầu ba Boss với kỹ năng riêng.",
              (("distance", "Quãng bay"), ("asteroids", "Thiên thạch"), ("bosses", "Boss hạ"), ("skills", "Skill né"))),
    RetroSpec("flappy_bird", "Flappy Bird", "allowed_with_safe_area",
              "Chú chim tự vỗ cánh luồn qua ống nước, tốc độ tăng dần theo quãng bay.",
              (("pipes", "Ống qua"), ("distance", "Quãng"))),
)

_ENGINES: dict[str, type[Engine]] = {engine.game_id: engine for engine in (
    SnakeEngine, BrickStackEngine, TankDuelEngine, BrickBreakerEngine,
    StarDefenderEngine, PixelDashEngine, PacmanEngine, ChickenShooterEngine, SpaceshipVoyagerEngine,
    FlappyBirdEngine)}

RETRO_IDS = frozenset(_ENGINES)
SPECS: dict[str, RetroSpec] = {spec.id: spec for spec in RETRO_GAMES}
assert RETRO_IDS == frozenset(SPECS), "every retro engine needs a catalog entry"

_PILOTS = ("NOVA", "ORION", "KIRA", "AXEL", "VEGA", "LUMA", "RHEA", "JETT",
           "MIRA", "ODIN", "SAGE", "ZENO")
_MAX_EVENTS = 200


def is_retro(game_id: str) -> bool:
    return game_id in RETRO_IDS


def rank_tier(score: int, reference: int) -> str:
    """Where a run stands against the cabinet record it was measured against.

    S means it matched or beat that record, and a first run on an empty board is an S.
    """
    if reference <= 0:
        return "S" if score > 0 else "-"
    ratio = score / reference
    for tier, floor in (("S", 1.0), ("A", 0.8), ("B", 0.6), ("C", 0.4), ("D", 0.2)):
        if ratio >= floor:
            return tier
    return "E"


def player_tag(seed: int) -> str:
    """A stable arcade handle for the run, so the leaderboard reads like a cabinet."""
    return f"{_PILOTS[seed % len(_PILOTS)]}-{seed // 7 % 1000:03d}"


def build_engine(game_id: str, seed: int, config: dict | None = None) -> Engine:
    try:
        return _ENGINES[game_id](seed, config or {})
    except KeyError as exc:
        raise ValueError(f"unknown retro game: {game_id}") from exc


def _thin(events: list[dict]) -> list[dict]:
    kept = [event for event in events if event["type"] in _EVENT_KINDS]
    if len(kept) <= _MAX_EVENTS:
        return kept
    stride = len(kept) / _MAX_EVENTS
    return [kept[int(index * stride)] for index in range(_MAX_EVENTS)]


def simulate_retro(game_id: str, seed: int, config: dict | None = None) -> GameplayReplay:
    """Play the whole clip out once; the renderer replays the identical match from the seed."""
    config = config or {}
    spec = SPECS[game_id]
    duration = float(random.Random(seed).randint(MIN_SECONDS, MAX_SECONDS))
    engine = build_engine(game_id, seed, config)
    for _ in range(int(duration * TICK_RATE)):
        engine.step()
    hi_score = max(0, int(config.get("hi_score") or 0))
    payload = {
        "preset": config.get("preset", "calm"),
        "seed": seed, "cols": engine.cols, "rows": engine.rows, "tick_rate": TICK_RATE,
        "title": spec.name, "player": player_tag(seed),
        # Frozen at simulation time so the HUD shows the same target on every re-render.
        "hi_score": hi_score,
        "stat_labels": [list(pair) for pair in spec.stat_labels],
        "events": [{"t": 0.0, "type": "start"}, *_thin(engine.events),
                   {"t": round(duration, 3), "type": "result", "score": engine.best}],
    }
    metrics = {"score": engine.best, "total_score": engine.total, "level": engine.top_level,
               "games": engine.games, "deaths": engine.deaths,
               **{key: int(value) for key, value in engine.best_stats.items()},
               **{f"n_{key}": int(value) for key, value in sorted(engine.tally.items())}}
    result = {"status": "complete", "score": engine.best, "player": payload["player"],
              "hi_score": max(hi_score, engine.best), "metrics": metrics}
    return GameplayReplay(SCHEMA_VERSION, game_id, seed, duration, TICK_RATE, "1", "1", "1",
                          payload, result)
