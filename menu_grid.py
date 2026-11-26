"""献立表画像の罫線からメニューセルを検出するモジュール。

固定比率のクロップは画像サイズや版の違いでズレやすいため、
「黒い罫線で囲まれた白い領域（＝セル）」を連結成分として検出し、
曜日列 × 区分行 にマッピングする。
"""
from __future__ import annotations

import io
from dataclasses import dataclass, field

import numpy as np
from PIL import Image, ImageDraw, ImageOps
from scipy import ndimage as ndi

DAY_KEYS = ["月", "火", "水", "木", "金", "土"]


@dataclass
class Cell:
    x0: int
    y0: int
    x1: int
    y1: int
    ink_ratio: float = 0.0  # セル内部の黒画素率（ほぼ0なら空欄）

    @property
    def box(self) -> tuple[int, int, int, int]:
        return (self.x0, self.y0, self.x1, self.y1)

    @property
    def is_blank(self) -> bool:
        return self.ink_ratio < 0.002


@dataclass
class MenuGrid:
    width: int
    height: int
    # rows[i][day_key] -> Cell   （上から順の区分行）
    rows: list[dict[str, Cell]] = field(default_factory=list)
    # 曜日列に属さない枠（トレビ下段の週替わりパスタなど）。左から順。
    extras: list[Cell] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return len(self.rows) > 0


def _cluster(values: list[int], tol: int) -> list[list[int]]:
    """近い値同士をまとめる（ソート済み前提でなくてもよい）"""
    groups: list[list[int]] = []
    for v in sorted(values):
        if groups and v - groups[-1][-1] <= tol:
            groups[-1].append(v)
        else:
            groups.append([v])
    return groups


def detect_grid(img: Image.Image) -> MenuGrid:
    gray = np.array(img.convert("L"))
    h, w = gray.shape
    dark = gray < 128
    labels, _ = ndi.label(~dark)
    objs = ndi.find_objects(labels)

    cells: list[Cell] = []
    for sl in objs:
        if sl is None:
            continue
        y0, y1 = sl[0].start, sl[0].stop
        x0, x1 = sl[1].start, sl[1].stop
        # 画像外周につながる背景は除外
        if y0 == 0 or x0 == 0 or y1 == h or x1 == w:
            continue
        cw, ch = x1 - x0, y1 - y0
        # メニュー名セルは「幅が画像の8%以上・高さ4%以上」。
        # 縦書きの区分ラベル（細い）や栄養成分の行（低い）はここで落ちる。
        if cw < w * 0.08 or ch < h * 0.04:
            continue
        pad = 3
        inner = dark[y0 + pad:y1 - pad, x0 + pad:x1 - pad]
        ink = float(inner.mean()) if inner.size else 0.0
        cells.append(Cell(x0, y0, x1, y1, ink))

    grid = MenuGrid(width=w, height=h)
    if not cells:
        return grid

    tol_y = max(4, int(h * 0.01))
    row_groups = _cluster([c.y0 for c in cells], tol_y)
    for g in row_groups:
        lo, hi = g[0], g[-1]
        row_cells = sorted([c for c in cells if lo <= c.y0 <= hi], key=lambda c: c.x0)
        if len(row_cells) == len(DAY_KEYS):
            grid.rows.append(dict(zip(DAY_KEYS, row_cells)))
        else:
            grid.extras.extend(row_cells)
    return grid


def _preprocess(crop: Image.Image, scale: int = 3) -> Image.Image:
    g = ImageOps.grayscale(crop)
    g = g.resize((g.width * scale, g.height * scale), Image.Resampling.LANCZOS)
    g = ImageOps.autocontrast(g)
    # 余白を足すと検出器が端の文字を拾いやすくなる
    return ImageOps.expand(g, border=24, fill=255)


def cell_image_for_ocr(img: Image.Image, cell: Cell) -> Image.Image:
    inset = 3
    crop = img.crop((cell.x0 + inset, cell.y0 + inset, cell.x1 - inset, cell.y1 - inset))
    return _preprocess(crop)


def stitch_cells(img: Image.Image, cells: list[Cell], scale: float = 1.5) -> bytes:
    """指定セル（今日のメニュー欄）だけを縦に並べた原本画像を作る。

    OCRが怪しいときでも、Discord上で「実物の欄」をそのまま読めるようにするため。
    """
    if not cells:
        return b""
    crops = []
    for c in cells:
        crop = img.crop(c.box).convert("RGB")
        # 文字のある範囲まで下側の余白を詰める（空欄セルは低い帯にする）
        arr = np.array(crop.convert("L")) < 128
        arr[:3, :] = arr[-3:, :] = False
        arr[:, :3] = arr[:, -3:] = False
        ys = np.where(arr.any(axis=1))[0]
        bottom = min(crop.height, int(ys.max()) + 10) if ys.size else 24
        crop = crop.crop((0, 0, crop.width, max(bottom, 24)))
        crops.append(crop.resize((int(crop.width * scale), int(crop.height * scale)), Image.Resampling.LANCZOS))
    gap = 8
    width = max(c.width for c in crops)
    height = sum(c.height for c in crops) + gap * (len(crops) - 1)
    canvas = Image.new("RGB", (width, height), (255, 255, 255))
    draw = ImageDraw.Draw(canvas)
    y = 0
    for i, crop in enumerate(crops):
        canvas.paste(crop, (0, y))
        y += crop.height
        if i < len(crops) - 1:
            draw.rectangle((0, y, width, y + gap - 1), fill=(203, 213, 225))
            y += gap
    out = io.BytesIO()
    canvas.save(out, format="PNG", optimize=True)
    return out.getvalue()
