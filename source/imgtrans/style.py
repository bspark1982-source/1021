"""원본 글자의 배경색·글자색·크기 추정."""
from __future__ import annotations

from typing import Tuple

import numpy as np
from PIL import Image

from .model import Box


def to_hex(rgb) -> str:
    r, g, b = [int(max(0, min(255, round(v)))) for v in rgb[:3]]
    return f"#{r:02x}{g:02x}{b:02x}"


def from_hex(h: str) -> Tuple[int, int, int]:
    h = (h or "#000000").lstrip("#")
    if len(h) == 3:
        h = "".join(c * 2 for c in h)
    return int(h[0:2], 16), int(h[2:4], 16), int(h[4:6], 16)


def estimate_bg(arr: np.ndarray, box: Box, pad: int = 3) -> np.ndarray:
    """글자 상자 바깥 테두리(링) 픽셀의 중앙값 = 배경색."""
    H, W = arr.shape[:2]
    x0, y0, x1, y1 = [int(round(v)) for v in box]
    ox0, oy0 = max(0, x0 - pad), max(0, y0 - pad)
    ox1, oy1 = min(W, x1 + pad), min(H, y1 + pad)
    region = arr[oy0:oy1, ox0:ox1].reshape(-1, 3) if oy1 > oy0 and ox1 > ox0 else None
    if region is None or len(region) == 0:
        return np.array([255, 255, 255])
    mask = np.ones((oy1 - oy0, ox1 - ox0), dtype=bool)
    ix0, iy0 = max(0, x0 + 1 - ox0), max(0, y0 + 1 - oy0)
    ix1, iy1 = min(ox1 - ox0, x1 - 1 - ox0), min(oy1 - oy0, y1 - 1 - oy0)
    if ix1 > ix0 and iy1 > iy0:
        mask[iy0:iy1, ix0:ix1] = False
    ring = arr[oy0:oy1, ox0:ox1][mask]
    if len(ring) < 10:
        ring = region
    return np.median(ring, axis=0)


def estimate_fg(arr: np.ndarray, box: Box, bg: np.ndarray) -> np.ndarray:
    """상자 안에서 배경과 가장 다른 픽셀들의 중앙값 = 글자색."""
    x0, y0, x1, y1 = [int(round(v)) for v in box]
    crop = arr[max(0, y0):max(0, y1), max(0, x0):max(0, x1)].reshape(-1, 3).astype(float)
    if len(crop) == 0:
        return np.array([0, 0, 0])
    d = np.linalg.norm(crop - bg.astype(float), axis=1)
    if d.max() < 25:  # 대비가 거의 없으면 배경 밝기에 맞춰 검정/흰색
        return np.array([0, 0, 0]) if bg.mean() > 128 else np.array([255, 255, 255])
    thr = np.percentile(d, 85)
    sel = crop[d >= max(thr, d.max() * 0.55)]
    return np.median(sel, axis=0)


def estimate_bold(arr: np.ndarray, box: Box, bg: np.ndarray, fg: np.ndarray) -> bool:
    """글자 픽셀 밀도로 굵은 글씨 여부를 대략 판단."""
    x0, y0, x1, y1 = [int(round(v)) for v in box]
    sub = arr[max(0, y0):max(0, y1), max(0, x0):max(0, x1)].astype(float)
    if sub.size == 0:
        return False
    hh, ww = sub.shape[:2]
    flat = sub.reshape(-1, 3)
    ink = (np.linalg.norm(flat - fg, axis=1) < np.linalg.norm(flat - bg, axis=1)).reshape(hh, ww)
    # 가로 방향 획 두께(연속 잉크 길이)의 중앙값 / 글자 크기
    pad = np.zeros((hh, 1), dtype=bool)
    m = np.hstack([pad, ink, pad]).astype(np.int8)
    d = np.diff(m, axis=1)
    starts = np.argwhere(d == 1)
    ends = np.argwhere(d == -1)
    if len(starts) < 5 or len(starts) != len(ends):
        return False
    runs = ends[:, 1] - starts[:, 1]
    stroke = float(np.median(runs))
    size = font_px_from_line_height(hh)
    return stroke / max(size, 1) > 0.155


def estimate_quad(arr: np.ndarray, quad, ring: float = 5.0):
    """기울어진 글자: 사각형 안쪽=글자, 바깥 테두리 띠=배경으로 색을 추정."""
    from PIL import Image as _I, ImageDraw as _D
    q = np.asarray(quad, float)
    c = q.mean(axis=0)
    unit = (q - c) / np.maximum(np.linalg.norm(q - c, axis=1, keepdims=True), 1e-6)
    outer = q + unit * ring
    inner = q + unit * 1.0
    H, W = arr.shape[:2]
    x0, y0 = int(max(0, outer[:, 0].min() - 1)), int(max(0, outer[:, 1].min() - 1))
    x1, y1 = int(min(W, outer[:, 0].max() + 2)), int(min(H, outer[:, 1].max() + 2))
    if x1 <= x0 or y1 <= y0:
        return np.array([255, 255, 255]), np.array([0, 0, 0])
    sub = arr[y0:y1, x0:x1].astype(float)

    def mask(poly):
        m = _I.new("L", (x1 - x0, y1 - y0), 0)
        _D.Draw(m).polygon([(px - x0, py - y0) for px, py in poly], fill=255)
        return np.asarray(m) > 0

    m_in, m_out, m_inner = mask(q), mask(outer), mask(inner)
    ring_px = sub[m_out & ~m_inner]
    bg = np.median(ring_px, axis=0) if len(ring_px) > 10 else np.array([255, 255, 255])
    inside = sub[m_in]
    if len(inside) == 0:
        return bg, np.array([0, 0, 0])
    d = np.linalg.norm(inside - bg, axis=1)
    if d.max() < 20:
        return bg, (np.array([0, 0, 0]) if bg.mean() > 128 else np.array([255, 255, 255]))
    thr = np.percentile(d, 88)
    fg = np.median(inside[d >= max(thr, d.max() * 0.5)], axis=0)
    return bg, fg


def font_px_from_line_height(h: float) -> float:
    """OCR 상자 높이(여백 포함) → 한글 글자 크기(px) 근사."""
    return max(8.0, h * 0.72)


def analyze(img: Image.Image):
    return np.asarray(img.convert("RGB"))
