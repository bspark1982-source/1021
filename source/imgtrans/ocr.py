"""OCR: RapidOCR(PP-OCRv6, 오프라인, 무료)로 영어 줄 단위 인식."""
from __future__ import annotations

from typing import List
import logging
import math

import numpy as np
from PIL import Image

from .model import Line

_engine = None


def _get_engine():
    global _engine
    if _engine is None:
        from rapidocr import RapidOCR
        try:
            _engine = RapidOCR(params={"Global.log_level": "error"})
        except Exception:
            _engine = RapidOCR()
        logging.getLogger("RapidOCR").setLevel(logging.ERROR)
    return _engine


def run_ocr(img: Image.Image, min_score: float = 0.5) -> List[Line]:
    """이미지에서 텍스트 줄을 찾아 원본 좌표계 Line 목록으로 반환."""
    rgb = img.convert("RGB")
    w, h = rgb.size
    # 작은 이미지는 확대해서 인식률을 높임
    scale = 1.0
    if max(w, h) < 1400:
        scale = 2.0
        rgb = rgb.resize((int(w * scale), int(h * scale)), Image.LANCZOS)
    arr = np.asarray(rgb)[:, :, ::-1].copy()  # RGB→BGR

    res = _get_engine()(arr)
    boxes = getattr(res, "boxes", None)
    if boxes is None:
        return []
    out: List[Line] = []
    for b, t, s in zip(res.boxes, res.txts, res.scores):
        t = (t or "").strip()
        if not t or float(s) < min_score:
            continue
        b = np.asarray(b, dtype=float) / scale
        x0, y0 = float(b[:, 0].min()), float(b[:, 1].min())
        x1, y1 = float(b[:, 0].max()), float(b[:, 1].max())
        x0, y0 = max(0.0, x0), max(0.0, y0)
        x1, y1 = min(float(w), x1), min(float(h), y1)
        ln = Line(box=[x0, y0, x1, y1], text=t, score=float(s), source="ocr")
        if len(b) == 4:
            v = b[1] - b[0]
            ang = math.degrees(math.atan2(v[1], v[0]))
            if abs(ang) > 7:   # 강·바다·해협 이름처럼 기울어진 글자
                ln.angle = float(ang)
                ln.quad = b.round(1).tolist()
                ln.rw = float(np.linalg.norm(b[1] - b[0]))
                ln.rh = float(np.linalg.norm(b[3] - b[0]))
        out.append(ln)
    return out
