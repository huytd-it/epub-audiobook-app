"""Painter for the retro handheld family: a dot-matrix board plus a scoreboard panel.

Nothing here loads an asset. The clip re-runs the very same engine the simulation ran
(:mod:`app.gameplay_retro`), so frame N always shows the tick the replay recorded, and a
re-render after a failed encode reproduces the match exactly.
"""
from __future__ import annotations

from functools import lru_cache
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont

from app.gameplay_retro import PALETTE, RetroFrame, build_engine, is_retro, rank_tier

SHELL = (8, 11, 18)
PANEL = (14, 19, 28)
BEZEL = (30, 40, 54)
OFF_CELL = (22, 29, 40)
TEXT = (226, 236, 248)
DIM_TEXT = (118, 134, 156)
ACCENT = (94, 234, 212)
WARN = (255, 206, 74)

_FONT_FILES = ("C:/Windows/Fonts/consolab.ttf", "C:/Windows/Fonts/arialbd.ttf",
               "/usr/share/fonts/truetype/dejavu/DejaVuSansMono-Bold.ttf")


@lru_cache(maxsize=32)
def font(size: int):
    for path in _FONT_FILES:
        if Path(path).is_file():
            try:
                return ImageFont.truetype(path, size)
            except OSError:
                continue
    return ImageFont.load_default()


class RetroClip:
    """Stateful painter: it owns one engine and advances it to the tick each frame needs."""

    def __init__(self, game_id: str, payload: dict, duration: float, width: int, height: int,
                 fps: int) -> None:
        if not is_retro(game_id):
            raise ValueError(f"{game_id} is not a retro game")
        self.game_id = game_id
        self.payload = payload
        self.duration = max(0.001, float(duration))
        self.width, self.height = int(width), int(height)
        self.fps = max(1, int(fps))
        self.tick_rate = int(payload.get("tick_rate") or 20)
        self.hi_score = max(0, int(payload.get("hi_score") or 0))
        self.title = str(payload.get("title") or game_id).upper()
        self.player = str(payload.get("player") or "")
        self.stat_labels = [tuple(pair) for pair in payload.get("stat_labels") or []]
        self.engine = build_engine(game_id, int(payload.get("seed") or 0), payload)
        self._layout()
        self._base = self._paint_base()

    # -- geometry --------------------------------------------------------
    def _layout(self) -> None:
        """Scoreboard keeps a fixed column; the board takes whatever square cells fit the rest.

        Boards range from 10x20 (xếp gạch) to 30x22 (rắn), so the cell size — not the panel
        split — is what adapts, and a narrow board simply sits centred in its region.
        """
        cols, rows = self.engine.cols, self.engine.rows
        margin = max(6, min(self.width, self.height) // 36)
        gap = margin
        if self.width >= self.height:  # 16:9 and friends: board left, scoreboard right
            panel_w = max(160, int(self.width * 0.24))
            self.panel = (self.width - margin - panel_w, margin, self.width - margin,
                          self.height - margin)
            region = (margin, margin, self.panel[0] - gap, self.height - margin)
        else:  # portrait: board on top, scoreboard underneath
            panel_h = max(150, int(self.height * 0.22))
            self.panel = (margin, self.height - margin - panel_h, self.width - margin,
                          self.height - margin)
            region = (margin, margin, self.width - margin, self.panel[1] - gap)
        region_w, region_h = region[2] - region[0], region[3] - region[1]
        rough = max(2, min(region_w // cols, region_h // rows))
        inset = max(3, rough // 2)
        self.cell = max(2, min((region_w - inset * 2) // cols, (region_h - inset * 2) // rows))
        self.pad = max(1, self.cell // 9)
        board_w, board_h = cols * self.cell, rows * self.cell
        self.board = (region[0] + (region_w - board_w) // 2, region[1] + (region_h - board_h) // 2)
        self.frame_box = (self.board[0] - inset, self.board[1] - inset,
                          self.board[0] + board_w + inset, self.board[1] + board_h + inset)
        panel_w = self.panel[2] - self.panel[0]
        panel_h = self.panel[3] - self.panel[1]
        # A portrait frame gets a wide, short scoreboard: flow it into columns instead of
        # shrinking the type until it disappears.
        self.columns = 3 if panel_w > panel_h * 1.6 else 1
        stacked = (5 + len(self.stat_labels) + self.columns - 1) // self.columns
        # One text unit that fits both the panel width and every row stacked into a column.
        self.unit = max(9, int(min(panel_w * 0.085 * self.columns, panel_h / (2.9 * stacked + 4))))

    def _rect(self, col: int, row: int) -> tuple[int, int, int, int]:
        x0 = self.board[0] + col * self.cell
        y0 = self.board[1] + row * self.cell
        return (x0 + self.pad, y0 + self.pad,
                x0 + self.cell - self.pad - 1, y0 + self.cell - self.pad - 1)

    # -- static chrome ---------------------------------------------------
    def _paint_base(self) -> Image.Image:
        image = Image.new("RGB", (self.width, self.height), SHELL)
        draw = ImageDraw.Draw(image)
        draw.rectangle(self.frame_box, fill=PANEL, outline=BEZEL, width=max(2, self.cell // 6))
        for row in range(self.engine.rows):
            for col in range(self.engine.cols):
                draw.rectangle(self._rect(col, row), fill=OFF_CELL)
        draw.rectangle(self.panel, fill=PANEL, outline=BEZEL, width=max(1, self.cell // 8))
        x0, y0, x1, _ = self.panel
        pad = max(6, self.cell // 2)
        draw.text((x0 + pad, y0 + pad), self.title, font=font(self._size(0.9)), fill=ACCENT)
        draw.line((x0 + pad, y0 + pad + self._size(1.5), x1 - pad, y0 + pad + self._size(1.5)),
                  fill=BEZEL, width=max(1, self.cell // 10))
        return image

    def _size(self, scale: float) -> int:
        return max(8, int(self.unit * scale))

    # -- dynamic layer ---------------------------------------------------
    def _lit(self, draw: ImageDraw.ImageDraw, cells) -> None:
        for col, row, key in cells:
            if not (0 <= col < self.engine.cols and 0 <= row < self.engine.rows):
                continue
            color = PALETTE.get(key, TEXT)
            box = self._rect(col, row)
            draw.rectangle(box, fill=color)
            if self.cell >= 8:  # the moulded highlight every brick-game pixel had
                bright = tuple(min(255, channel + 60) for channel in color)
                third = max(1, (box[2] - box[0]) // 3)
                draw.rectangle((box[0], box[1], box[0] + third, box[1] + third), fill=bright)

    def _entries(self, frame: RetroFrame) -> list[tuple[str, str, object, tuple[int, int, int], float]]:
        hud = frame.hud
        best = max(self.hi_score, int(hud["best"]))
        rows = [("SCORE", "text", f"{hud['score']:,}".replace(",", " "), TEXT, 1.5),
                ("HI-SCORE", "text", f"{best:,}".replace(",", " "),
                 WARN if hud["best"] >= self.hi_score and self.hi_score else DIM_TEXT, 0.85),
                ("LEVEL", "text", str(hud["level"]), TEXT, 0.85)]
        rows.extend((label.upper(), "text", str(hud["stats"].get(key, 0)), TEXT, 0.85)
                    for key, label in self.stat_labels)
        rows.append(("LIVES", "lives", max(0, int(hud["lives"])), PALETTE["player"], 0.85))
        if frame.mini:
            rows.append((frame.mini_label, "mini", frame, TEXT, 0.85))
        return rows

    def _scoreboard(self, draw: ImageDraw.ImageDraw, frame: RetroFrame) -> None:
        x0, y0, x1, y1 = self.panel
        pad = max(6, self.cell // 2)
        label_h = int(self._size(0.55) * 1.3)
        chip = max(4, self.unit // 2)
        top = y0 + pad + self._size(2.2)
        bottom = y1 - pad - self._size(1.7)
        column_w = (x1 - x0 - pad * 2) // self.columns
        cursor, column = top, 0
        for label, kind, value, color, scale in self._entries(frame):
            if kind == "mini":
                body = max(4, self.unit // 2) * value.mini_size[1]
            elif kind == "lives":
                body = chip * 2
            else:
                body = int(self._size(scale) * 1.35)
            if cursor + label_h + body > bottom and column + 1 < self.columns:
                column, cursor = column + 1, top
            if cursor + label_h + body > bottom:
                break  # the panel is full; the remaining stats are in the replay anyway
            left = x0 + pad + column * column_w
            draw.text((left, cursor), label, font=font(self._size(0.55)), fill=DIM_TEXT)
            cursor += label_h
            if kind == "text":
                draw.text((left, cursor), value, font=font(self._size(scale)), fill=color)
            elif kind == "lives":
                for index in range(value):
                    chip_x = left + index * (chip * 2)
                    draw.rectangle((chip_x, cursor, chip_x + chip, cursor + chip), fill=color)
            else:
                box = max(4, self.unit // 2)
                for col, row, key in value.mini:
                    bx, by = left + col * box, cursor + row * box
                    draw.rectangle((bx, by, bx + box - 2, by + box - 2), fill=PALETTE.get(key, TEXT))
            cursor += body
        tier = rank_tier(int(frame.hud["best"]), self.hi_score)
        footer = y1 - pad - self._size(1.5)
        draw.text((x0 + pad, footer), f"RANK {tier}", font=font(self._size(0.9)),
                  fill=ACCENT if tier in {"S", "A"} else DIM_TEXT)
        draw.text((x1 - pad - self._size(0.5) * len(self.player) * 0.62, footer + int(self._size(0.35))),
                  self.player, font=font(self._size(0.5)), fill=DIM_TEXT)

    def _overlay(self, draw: ImageDraw.ImageDraw, text: str) -> None:
        glyph = font(self._size(1.2))
        box = draw.textbbox((0, 0), text, font=glyph)
        width, height = box[2] - box[0], box[3] - box[1]
        cx = self.board[0] + self.engine.cols * self.cell // 2
        cy = self.board[1] + self.engine.rows * self.cell // 2
        margin = max(6, self.cell)
        draw.rectangle((cx - width // 2 - margin, cy - height // 2 - margin,
                        cx + width // 2 + margin, cy + height // 2 + margin),
                       fill=SHELL, outline=ACCENT, width=max(2, self.cell // 6))
        draw.text((cx - width // 2 - box[0], cy - height // 2 - box[1]), text, font=glyph, fill=TEXT)

    def frame(self, index: int, t: float) -> Image.Image:
        target = min(int(t * self.tick_rate), int(self.duration * self.tick_rate))
        while self.engine.tick < target:
            self.engine.step()
        state = self.engine.frame()
        image = self._base.copy()
        draw = ImageDraw.Draw(image)
        self._lit(draw, state.cells)
        self._scoreboard(draw, state)
        if state.overlay:
            self._overlay(draw, state.overlay)
        return image
