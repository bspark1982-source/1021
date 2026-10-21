"""데이터 모델: OCR 줄(Line)과 번역 단위 블록(Block)."""
from __future__ import annotations

from dataclasses import dataclass, field, asdict
from typing import List, Optional
import itertools

Box = List[float]  # [x0, y0, x1, y1] (이미지 픽셀 좌표)

_ids = itertools.count(1)


def new_id() -> int:
    return next(_ids)


def bump_ids(min_value: int) -> None:
    """작업 파일을 불러온 뒤 id 충돌을 막기 위해 카운터를 올림."""
    global _ids
    cur = next(_ids)
    _ids = itertools.count(max(cur, min_value + 1))


@dataclass
class Line:
    box: Box
    text: str
    score: float = 1.0
    source: str = "ocr"            # ocr | pdf
    font_size: Optional[float] = None   # PDF 텍스트 레이어에서 얻은 실제 크기(px)
    color: Optional[str] = None         # "#rrggbb"
    bold: bool = False
    group: Optional[int] = None         # PDF 블록 번호 (같은 문단 묶기용)
    lead_symbol: bool = False           # 앞에 도시 점(●) 등 기호가 있었음
    angle: float = 0.0                  # 기울어진 글자 각도(도, 시계방향 +)
    quad: Optional[list] = None         # 기울어진 글자의 네 꼭짓점 [[x,y]*4]
    rw: float = 0.0                     # 기울어진 글자 길이
    rh: float = 0.0                     # 기울어진 글자 높이

    @property
    def x0(self): return self.box[0]
    @property
    def y0(self): return self.box[1]
    @property
    def x1(self): return self.box[2]
    @property
    def y1(self): return self.box[3]
    @property
    def h(self): return self.box[3] - self.box[1]
    @property
    def w(self): return self.box[2] - self.box[0]


@dataclass
class Block:
    """번역·렌더링의 단위. 여러 줄(문단)을 하나로 묶음."""
    id: int = field(default_factory=new_id)
    src: str = ""                 # 영어 원문(수정 가능)
    dst: str = ""                 # 한글 번역문(수정 가능)
    box: Box = field(default_factory=lambda: [0, 0, 10, 10])   # 한글을 그릴 영역
    erase_boxes: List[Box] = field(default_factory=list)       # 지울 원문 줄 영역
    line_texts: List[str] = field(default_factory=list)        # 분할용 원래 줄 텍스트
    font_size: float = 16.0       # px
    color: str = "#000000"        # 글자색
    bg: str = "#ffffff"           # 덮을 배경색
    bold: bool = False
    align: str = "left"           # left | center | right
    auto_fit: bool = True         # 영역에 맞게 글자 크기 자동 축소
    erase: bool = True            # 원문 지우기
    enabled: bool = True          # 이 블록 적용 여부
    kind: str = "text"            # text | keep(숫자 등 보호) | manual
    source: str = "ocr"
    translated_by: str = ""       # glossary | google | mymemory | manual ...
    angle: float = 0.0            # 기울어진 글자 각도(도). 0 이면 가로쓰기
    rot_w: float = 0.0            # 기울어진 글자 영역 길이
    rot_h: float = 0.0            # 기울어진 글자 영역 높이
    erase_polys: List[list] = field(default_factory=list)      # 기울어진 원문 영역(사각형 꼭짓점)

    def to_dict(self) -> dict:
        return asdict(self)

    @staticmethod
    def from_dict(d: dict) -> "Block":
        b = Block()
        for k, v in d.items():
            if hasattr(b, k):
                setattr(b, k, v)
        return b


def union_box(boxes: List[Box]) -> Box:
    return [min(b[0] for b in boxes), min(b[1] for b in boxes),
            max(b[2] for b in boxes), max(b[3] for b in boxes)]


def overlap_ratio(a: Box, b: Box) -> float:
    """a 면적 대비 a∩b 면적 비율."""
    ix0, iy0 = max(a[0], b[0]), max(a[1], b[1])
    ix1, iy1 = min(a[2], b[2]), min(a[3], b[3])
    if ix1 <= ix0 or iy1 <= iy0:
        return 0.0
    area_a = max(1e-6, (a[2] - a[0]) * (a[3] - a[1]))
    return (ix1 - ix0) * (iy1 - iy0) / area_a
