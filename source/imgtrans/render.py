"""원문 지우기(배경색 덮기) + 한글 그리기."""
from __future__ import annotations

import glob
import os
from functools import lru_cache
from typing import Dict, List, Optional, Tuple

from PIL import Image, ImageDraw, ImageFont

from .model import Block
from .style import from_hex

APP_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

# 이름 → (보통, 굵게) 파일 후보
_WIN = os.path.join(os.environ.get("WINDIR", r"C:\Windows"), "Fonts")
_CANDIDATES: Dict[str, Tuple[List[str], List[str]]] = {
    "맑은 고딕": ([os.path.join(_WIN, "malgun.ttf")], [os.path.join(_WIN, "malgunbd.ttf")]),
    "나눔고딕": ([os.path.join(_WIN, "NanumGothic.ttf")], [os.path.join(_WIN, "NanumGothicBold.ttf")]),
    "Noto Sans KR": (
        ["/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc"],
        ["/usr/share/fonts/opentype/noto/NotoSansCJK-Bold.ttc"],
    ),
}


def available_fonts() -> Dict[str, Tuple[str, Optional[str]]]:
    """사용 가능한 한글 글꼴 목록. 앱 폴더 fonts/ 에 넣은 TTF/OTF 도 자동 등록."""
    found: Dict[str, Tuple[str, Optional[str]]] = {}
    for name, (regs, bolds) in _CANDIDATES.items():
        r = next((p for p in regs if os.path.exists(p)), None)
        if r:
            b = next((p for p in bolds if os.path.exists(p)), None)
            found[name] = (r, b)
    fdir = os.path.join(APP_DIR, "fonts")
    files = sorted(glob.glob(os.path.join(fdir, "*.ttf")) + glob.glob(os.path.join(fdir, "*.otf"))
                   + glob.glob(os.path.join(fdir, "*.ttc")))
    regular = [f for f in files if not _is_bold_name(f)]
    for f in regular:
        stem = os.path.splitext(os.path.basename(f))[0]
        base = stem.replace("-Regular", "").replace("Regular", "").strip("-_ ")
        bold = next((g for g in files if _is_bold_name(g) and os.path.basename(g).startswith(base)), None)
        found[base or stem] = (f, bold)
    return found


def _is_bold_name(p: str) -> bool:
    n = os.path.basename(p).lower()
    return "bold" in n or n.endswith("bd.ttf") or "black" in n or "heavy" in n


def default_font_name() -> str:
    fonts = available_fonts()
    for n in ("맑은 고딕", "Noto Sans KR", "나눔고딕"):
        if n in fonts:
            return n
    return next(iter(fonts), "")


@lru_cache(maxsize=256)
def _font(path: str, size: int) -> ImageFont.FreeTypeFont:
    try:
        if path.lower().endswith(".ttc"):
            # Noto Sans CJK ttc 안에서 KR 면을 찾음
            for i in range(10):
                f = ImageFont.truetype(path, size, index=i)
                if "KR" in " ".join(f.getname()):
                    return f
        return ImageFont.truetype(path, size, index=0)
    except Exception:
        return ImageFont.load_default()


def get_font(font_name: str, size: float, bold: bool) -> Tuple[ImageFont.FreeTypeFont, bool]:
    """(글꼴, 가짜굵게필요여부)"""
    fonts = available_fonts()
    reg, bld = fonts.get(font_name) or next(iter(fonts.values()), (None, None))
    if reg is None:
        return ImageFont.load_default(), bold
    s = max(6, int(round(size)))
    if bold and bld:
        return _font(bld, s), False
    return _font(reg, s), bold


# ---------------- 줄바꿈 ----------------
def _text_w(draw, font, t: str) -> float:
    if not t:
        return 0
    return draw.textlength(t, font=font)


def wrap(draw, font, text: str, width: float, overflow: float = 1.35) -> List[str]:
    """단어 단위 줄바꿈. 한 단어가 폭을 조금 넘는 정도(overflow 배)면 끊지 않고 한 줄에 둠."""
    lines: List[str] = []
    for para in text.split("\n"):
        words = para.split(" ")
        cur = ""
        for w in words:
            cand = (cur + " " + w) if cur else w
            if _text_w(draw, font, cand) <= width:
                cur = cand
                continue
            if cur:
                lines.append(cur)
                cur = ""
            # 단어 하나가 아주 길 때만 글자 단위로 자름 (사우디아라/비아 방지)
            if _text_w(draw, font, w) > width * overflow:
                piece = ""
                for ch in w:
                    if _text_w(draw, font, piece + ch) <= width or not piece:
                        piece += ch
                    else:
                        lines.append(piece)
                        piece = ch
                cur = piece
            else:
                cur = w
        lines.append(cur)
    return lines


LINE_SPACING = 1.22


def layout(draw, b: Block, font_name: str):
    """블록 안에 들어갈 글자 크기와 줄 목록 계산."""
    x0, y0, x1, y1 = b.box
    W, H = max(4.0, x1 - x0), max(4.0, y1 - y0)
    text = (b.dst or "").strip()
    base = max(6.0, float(b.font_size))
    single = len(b.erase_boxes) <= 1 and "\n" not in text

    def measure(size, allow_wrap):
        font, fake = get_font(font_name, size, b.bold)
        lines = wrap(draw, font, text, W) if allow_wrap else text.split("\n")
        lh = size * LINE_SPACING
        width = max((_text_w(draw, font, l) for l in lines), default=0)
        height = lh * (len(lines) - 1) + size * 1.1
        return font, fake, lines, width, height

    if not b.auto_fit:  # 크기 고정: 영역 폭에 맞춰 줄바꿈만
        f, fake, lines, w, h = measure(base, True)
        return f, fake, lines, base
    if single:
        s = base
        while s >= base * 0.75:
            f, fake, lines, w, h = measure(s, False)
            if w <= W * 1.05:
                return f, fake, lines, s
            s -= 0.5
        # 짧은 라벨은 조금 넘치더라도 한 줄 유지 (가운데 기준으로 양옆으로 넘침)
        s = base * 0.75
        f, fake, lines, w, h = measure(s, False)
        if w <= W * 1.8 and len(text) <= 14:
            return f, fake, lines, s
    s = base
    while s >= max(6.0, base * 0.55):
        f, fake, lines, w, h = measure(s, True)
        if h <= H * 1.08 and w <= W * (1.36 if len(lines) <= 2 else 1.02):
            return f, fake, lines, s
        s -= 0.5
    s = max(6.0, base * 0.55)
    f, fake, lines, w, h = measure(s, True)
    return f, fake, lines, s


def _draw_rotated(out: Image.Image, d, b: Block, font_name: str):
    """기울어진 글자: 투명 레이어에 가로로 쓴 뒤 같은 각도로 돌려 붙임."""
    import copy
    tmp = copy.copy(b)
    tmp.box = [0, 0, b.rot_w, b.rot_h]
    tmp.erase_boxes = [tmp.box]
    tmp.angle = 0
    font, fake_bold, lines, size = layout(d, tmp, font_name)
    lh = size * LINE_SPACING
    tw = max(_text_w(d, font, l) for l in lines)
    th = lh * (len(lines) - 1) + size * 1.25
    W, H = int(max(b.rot_w, tw) + 8), int(max(b.rot_h, th) + 8)
    layer = Image.new("RGBA", (W, H), (0, 0, 0, 0))
    ld = ImageDraw.Draw(layer)
    color = from_hex(b.color) + (255,)
    stroke = max(1, int(size / 22)) if fake_bold else 0
    top = (H - th) / 2
    for i, ln in enumerate(lines):
        w = _text_w(ld, font, ln)
        ld.text(((W - w) / 2, top + i * lh), ln, font=font, fill=color, anchor="la",
                stroke_width=stroke, stroke_fill=color if stroke else None)
    rot = layer.rotate(-b.angle, resample=Image.BICUBIC, expand=True)
    cx, cy = (b.box[0] + b.box[2]) / 2, (b.box[1] + b.box[3]) / 2
    out.paste(rot, (int(round(cx - rot.width / 2)), int(round(cy - rot.height / 2))), rot)


def render(img: Image.Image, blocks: List[Block], font_name: str = "",
           erase_pad: int = 1) -> Image.Image:
    font_name = font_name or default_font_name()
    out = img.convert("RGB").copy()
    d = ImageDraw.Draw(out)
    active = [b for b in blocks if b.enabled and b.kind != "keep"]
    # 1) 지우기를 먼저 모두 처리(겹치는 블록이 서로 지우지 않도록)
    for b in active:
        if b.erase and (b.dst or "").strip():
            bg = from_hex(b.bg)
            for e in b.erase_boxes:
                d.rectangle([e[0] - erase_pad, e[1] - erase_pad, e[2] + erase_pad, e[3] + erase_pad], fill=bg)
            for poly in b.erase_polys:
                d.polygon([tuple(p) for p in poly], fill=bg)
    # 2) 한글 그리기
    for b in active:
        text = (b.dst or "").strip()
        if not text:
            continue
        if b.angle and b.rot_w > 0:
            _draw_rotated(out, d, b, font_name)
            continue
        font, fake_bold, lines, size = layout(d, b, font_name)
        color = from_hex(b.color)
        x0, y0, x1, y1 = b.box
        lh = size * LINE_SPACING
        total = lh * (len(lines) - 1) + size * 1.1
        top = y0 + max(0.0, ((y1 - y0) - total) / 2)
        stroke = max(1, int(size / 22)) if fake_bold else 0
        for i, ln in enumerate(lines):
            w = _text_w(d, font, ln)
            if b.align == "center":
                x = (x0 + x1) / 2 - w / 2
            elif b.align == "right":
                x = x1 - w
            else:
                x = x0
            y = top + i * lh
            d.text((x, y), ln, font=font, fill=color, anchor="la",
                   stroke_width=stroke, stroke_fill=color if stroke else None)
    return out
