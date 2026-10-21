"""OCR/PDF 줄을 문장·문단 단위 블록으로 묶고, 숫자 등은 보호 블록으로 분리."""
from __future__ import annotations

import re
from typing import List

import numpy as np
from PIL import Image

from .model import Block, Line, union_box
from . import style

# 글자가 없거나 숫자/단위만 있는 줄 → 지우지도 번역하지도 않음(그래프·표 숫자 보호)
_NUMERIC = re.compile(
    r"^[\s\d.,:;%$€£₩¥+\-–—/()\[\]~<>=±×*#°'\"]*"
    r"(k|K|M|B|bn|km|mi|m|kg|t|nm|h|hr|hrs|x|pp|bps|USD|EUR|KRW)?"
    r"[\s\d.,:;%)\]]*$"
)


_LEAD_SYM = re.compile(r"^([●•■▪○◆◇►▶★☆◉◎□▲△▼▽◼◾⬤·*]+)\s*")


def _symbol_gap_x(arr, ln: Line):
    """줄 왼쪽의 기호(점) 다음에 오는 빈 세로 띠의 x 좌표를 찾음."""
    x0, y0, x1, y1 = [int(round(v)) for v in ln.box]
    crop = arr[max(0, y0):y1, max(0, x0):x1].astype(float)
    if crop.size == 0:
        return None
    bg = style.estimate_bg(arr, ln.box)
    ink = (np.linalg.norm(crop - bg, axis=2) > 60).sum(axis=0) > 0
    i, n = 0, len(ink)
    while i < n and not ink[i]:
        i += 1
    while i < n and ink[i]:          # 기호
        i += 1
    j = i
    while j < n and not ink[j]:      # 빈 띠
        j += 1
    if j - i < 2 or i > ln.h * 1.6:
        return None
    return x0 + (i + j) / 2


def strip_leading_symbol(ln: Line, arr=None) -> Line:
    """지도의 도시 점(●)·글머리표가 글자와 함께 인식되면 기호는 남기고 글자만 처리."""
    m = _LEAD_SYM.match(ln.text)
    if not m or ln.source != "ocr":
        return ln
    rest = ln.text[m.end():].strip()
    if not rest:
        return ln
    gx = _symbol_gap_x(arr, ln) if arr is not None else None
    shift = (gx - ln.x0) if gx is not None else min(ln.w * 0.5, ln.h * 0.7 * len(m.group(1)))
    return Line(box=[ln.x0 + shift, ln.y0, ln.x1, ln.y1], text=rest, score=ln.score,
                source=ln.source, font_size=ln.font_size, color=ln.color, bold=ln.bold, group=ln.group,
                lead_symbol=True)


def _title_like(text: str) -> bool:
    """제목형(대부분 단어가 대문자로 시작) 줄인지. 범례·라벨·이름 목록에 많음."""
    words = [w for w in re.findall(r"[A-Za-z][A-Za-z'\-]*", text)]
    if not words:
        return False
    if len(words) == 1:
        return words[0][0].isupper()
    small = {"a", "an", "the", "of", "in", "on", "and", "or", "for", "to", "by", "at", "with", "from", "near"}
    caps = sum(1 for w in words if w[0].isupper() or w.lower() in small)
    return caps / len(words) >= 0.8


def is_protected(text: str) -> bool:
    t = text.strip()
    if not t:
        return True
    letters = re.findall(r"[A-Za-z]", t)
    if len(letters) == 0:
        return True
    if len(letters) < 2 and len(t) <= 3:      # N, S, A 같은 단일 문자 라벨
        return True
    if _NUMERIC.match(t):
        return True
    return False


def _join_lines(texts: List[str]) -> str:
    out = ""
    for t in texts:
        t = t.strip()
        if not out:
            out = t
        elif out.endswith(("-", "–")) and t[:1].isalpha():
            out += t                      # 줄끝 하이픈 연결 (Government-/Aligned)
        else:
            out += " " + t
    return out


def _line_styles(arr: np.ndarray, lines: List[Line]):
    st = []
    for ln in lines:
        bg = style.estimate_bg(arr, ln.box)
        if ln.color:
            fg = np.array(style.from_hex(ln.color), dtype=float)
        else:
            fg = style.estimate_fg(arr, ln.box, bg)
        bold = ln.bold if ln.source == "pdf" else style.estimate_bold(arr, ln.box, bg, fg)
        size = ln.font_size if ln.font_size else style.font_px_from_line_height(ln.h)
        st.append(dict(bg=bg, fg=fg, bold=bold, size=size))
    return st


_CONNECTOR_END = re.compile(r"(\b(of|the|in|and|or|to|for|by|at|on|with|from|a|an|near|between)|[-,&/–])\s*$",
                           re.IGNORECASE)
_NEW_ITEM = re.compile(r"^(As of\b|Updated\b|Source\b|Note\b|\(?\d+[.)]\s)")


def _can_merge(group: List[Line], ps: dict, cur: Line, cs: dict) -> bool:
    prev = group[-1]
    if prev.angle or cur.angle:
        return False
    h = (prev.h + cur.h) / 2
    hmin, hmax = min(prev.h, cur.h), max(prev.h, cur.h)
    if hmax / max(hmin, 1) > 1.3:
        return False
    gap = cur.y0 - prev.y1
    if gap < -0.4 * h or gap > 0.85 * h:
        return False
    # 문단 안 줄 간격보다 눈에 띄게 넓으면 새 문단(Key Takeaways 1·2번 등)
    if len(group) >= 2:
        gaps = [group[i + 1].y0 - group[i].y1 for i in range(len(group) - 1)]
        if gap > float(np.median(gaps)) + 0.3 * h:
            return False
    # 가로 정렬/겹침
    left = abs(cur.x0 - prev.x0) < 1.0 * h
    center = abs((cur.x0 + cur.x1) / 2 - (prev.x0 + prev.x1) / 2) < 1.0 * h
    right = abs(cur.x1 - prev.x1) < 1.0 * h
    ov = min(cur.x1, prev.x1) - max(cur.x0, prev.x0)
    overlap = ov > 0.3 * min(cur.w, prev.w)
    if not ((left or center or right) and overlap):
        return False
    # 글자색이 많이 다르면 다른 요소
    if np.linalg.norm(np.asarray(ps["fg"], float) - np.asarray(cs["fg"], float)) > 80:
        return False
    if prev.source == "ocr":
        if _NEW_ITEM.match(cur.text.strip()):
            return False
        nxt = cur.text.lstrip()[:1]
        short = len(prev.text.split()) <= 2 and len(cur.text.split()) <= 2 and center
        # 범례·목록처럼 제목형 줄이 이어질 때: 줄 간격이 좁고, 앞줄이 연결어(of, the, -, ,)로
        # 끝나거나 뒷줄이 더 짧을 때(한 항목이 두 줄로 넘어간 경우)만 묶음
        if not short and _title_like(prev.text) and (nxt.isupper() or not nxt.isalpha()):
            if gap > 0.3 * h:
                return False
            if not (_CONNECTOR_END.search(prev.text) or cur.w < prev.w * 0.92):
                return False
    if ps["bold"] != cs["bold"] and prev.source == "ocr":
        # 굵기 추정은 불안정하므로 크기 차이가 있을 때만 분리
        if hmax / max(hmin, 1) > 1.12:
            return False
    return True


def _make_block(lines: List[Line], styles: List[dict], source: str) -> Block:
    texts = [l.text for l in lines]
    bgs = np.array([s["bg"] for s in styles], float)
    fgs = np.array([s["fg"] for s in styles], float)
    sizes = [s["size"] for s in styles]
    bold_votes = sum(1 for s in styles if s["bold"])
    pad = 1.0
    erase = [[l.x0 - pad, l.y0 - pad, l.x1 + pad, l.y1 + pad] for l in lines]
    return Block(
        src=_join_lines(texts),
        box=union_box([l.box for l in lines]),
        erase_boxes=erase,
        line_texts=texts,
        font_size=float(np.median(sizes)),
        color=style.to_hex(np.median(fgs, axis=0)),
        bg=style.to_hex(np.median(bgs, axis=0)),
        bold=bold_votes * 2 > len(lines),
        source=source,
    )


def group_lines(img: Image.Image, lines: List[Line]) -> List[Block]:
    arr = style.analyze(img)
    blocks: List[Block] = []
    lines = [strip_leading_symbol(l, arr) for l in lines]

    keep =[l for l in lines if is_protected(l.text)]
    text = [l for l in lines if not is_protected(l.text)]

    for l in keep:
        b = _make_block([l], _line_styles(arr, [l]), l.source)
        b.kind = "keep"
        b.enabled = False
        b.erase = False
        b.dst = l.text
        blocks.append(b)

    text.sort(key=lambda l: (round(l.y0), l.x0))
    styles = _line_styles(arr, text)
    sidx = {id(l): s for l, s in zip(text, styles)}

    groups: List[List[Line]] = []
    for ln in text:
        best, best_gap = None, None
        for g in groups:
            last = g[-1]
            if last.source != ln.source:
                continue
            if ln.source == "pdf" and ln.group is not None and last.group != ln.group:
                continue
            if not _can_merge(g, sidx[id(last)], ln, sidx[id(ln)]):
                continue
            gap = ln.y0 - last.y1
            if best is None or gap < best_gap:
                best, best_gap = g, gap
        if best is not None:
            best.append(ln)
        else:
            groups.append([ln])

    for g in groups:
        b = _make_block(g, [sidx[id(l)] for l in g], g[0].source)
        if g[0].angle and len(g) == 1:
            _make_rotated(b, g[0], arr)
            blocks.append(b)
            continue
        # 짧은 한 줄 라벨(지명·축 라벨 등)은 가운데 정렬이 원래 위치를 잘 유지함
        if g[0].lead_symbol:
            b.align = "left"            # 점(●) 바로 옆에 붙여 씀
        elif len(g) == 1 and len(b.src.split()) <= 2:
            b.align = "center"
        elif len(g) >= 2 and len(b.src.split()) <= 4 and all(
                abs((l.x0 + l.x1) / 2 - (g[0].x0 + g[0].x1) / 2) < 0.6 * l.h for l in g):
            b.align = "center"           # 여러 줄짜리 짧은 지명(SAUDI / ARABIA)
        blocks.append(b)

    flat = [b for b in blocks if b.kind != "keep" and b.source == "ocr" and not b.angle]
    _harmonize_sizes(flat)
    _left_align_columns(flat)
    blocks.sort(key=lambda b: (b.kind == "keep", round(b.box[1] / 10), b.box[0]))
    return blocks


def _make_rotated(b: Block, ln: Line, arr=None):
    """기울어진 글자: 원문은 기울어진 사각형으로 지우고, 한글도 같은 각도로 씀."""
    q = np.asarray(ln.quad, float)
    c = q.mean(axis=0)
    grown = c + (q - c) * 1.0 + np.sign(q - c) * 2.0     # 가장자리 2px 여유
    b.angle = float(ln.angle)
    b.rot_w, b.rot_h = float(ln.rw), float(ln.rh)
    b.erase_polys = [grown.round(1).tolist()]
    b.erase_boxes = []
    b.font_size = style.font_px_from_line_height(ln.rh)
    b.align = "center"
    if arr is not None:
        bg, fg = style.estimate_quad(arr, ln.quad)
        b.bg, b.color = style.to_hex(bg), style.to_hex(fg)


def _left_align_columns(blocks: List[Block]):
    """왼쪽 끝이 나란한 블록들(범례·목록)은 왼쪽 정렬 → 범례 기호 옆에 붙어 있게."""
    for b in blocks:
        if b.align != "center":
            continue
        h = b.font_size
        for c in blocks:
            if c is b:
                continue
            if abs(c.box[0] - b.box[0]) < 6 and abs(c.box[1] - b.box[1]) < h * 5:
                b.align = "left"
                break


def _cdist(a: str, b: str) -> float:
    return float(np.linalg.norm(np.array(style.from_hex(a), float) - np.array(style.from_hex(b), float)))


def _harmonize_sizes(blocks: List[Block]):
    """왼쪽 끝이 같고 색이 같은 블록(범례·목록)은 글자 크기를 통일."""
    used = set()
    for b in blocks:
        if b.id in used:
            continue
        grp = [c for c in blocks if c.id not in used
               and abs(c.box[0] - b.box[0]) < 6 and _cdist(c.color, b.color) < 40
               and 0.75 < c.font_size / max(b.font_size, 1) < 1.33]
        if len(grp) >= 2:
            med = float(np.median([c.font_size for c in grp]))
            for c in grp:
                c.font_size = med
                used.add(c.id)


def split_block(img: Image.Image, b: Block) -> List[Block]:
    """블록을 원래 줄 단위로 분할."""
    if len(b.erase_boxes) <= 1:
        return [b]
    arr = style.analyze(img)
    out = []
    for box, t in zip(b.erase_boxes, b.line_texts or [""] * len(b.erase_boxes)):
        ln = Line(box=[box[0] + 1, box[1] + 1, box[2] - 1, box[3] - 1], text=t, source=b.source)
        nb = _make_block([ln], _line_styles(arr, [ln]), b.source)
        nb.color, nb.bg, nb.bold, nb.align = b.color, b.bg, b.bold, b.align
        nb.font_size = b.font_size
        out.append(nb)
    return out


def merge_blocks(blocks: List[Block]) -> Block:
    """선택한 블록들을 하나로 병합(위→아래, 왼→오 순서)."""
    blocks = sorted(blocks, key=lambda b: (b.box[1], b.box[0]))
    first = blocks[0]
    m = Block(
        src=_join_lines([b.src for b in blocks]),
        dst=" ".join(b.dst for b in blocks if b.dst).strip(),
        box=union_box([b.box for b in blocks]),
        erase_boxes=[e for b in blocks for e in b.erase_boxes],
        erase_polys=[p for b in blocks for p in b.erase_polys],
        line_texts=[t for b in blocks for t in (b.line_texts or [b.src])],
        font_size=float(np.median([b.font_size for b in blocks])),
        color=first.color, bg=first.bg, bold=first.bold, align=first.align,
        source=first.source,
    )
    return m
