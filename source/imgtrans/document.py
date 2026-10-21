"""문서(이미지 1장 또는 PDF 여러 페이지) 상태 관리, 인식·번역·저장 파이프라인."""
from __future__ import annotations

import io
import json
import os
from dataclasses import dataclass, field
from typing import Callable, Dict, List, Optional

from PIL import Image

from .model import Block, Line, overlap_ratio, bump_ids
from . import grouping, ocr, render as R, translate as T
from .glossary import Glossary
from .style import to_hex

Image.MAX_IMAGE_PIXELS = 300_000_000


@dataclass
class Page:
    index: int
    blocks: List[Block] = field(default_factory=list)
    recognized: bool = False
    image: Optional[Image.Image] = None


class Document:
    def __init__(self):
        self.kind = "image"          # image | pdf
        self.path = ""
        self.dpi = 200
        self.pages: List[Page] = []
        self._pdf = None

    # ---------------- 열기 ----------------
    @staticmethod
    def from_image(path: str) -> "Document":
        d = Document()
        d.kind, d.path = "image", path
        img = Image.open(path)
        img.load()
        d.pages = [Page(0, image=_flatten(img))]
        return d

    @staticmethod
    def from_pil(img: Image.Image) -> "Document":
        d = Document()
        d.kind, d.path = "image", ""
        d.pages = [Page(0, image=_flatten(img))]
        return d

    @staticmethod
    def from_pdf(path: str, dpi: int = 200) -> "Document":
        try:
            import pymupdf as fitz
        except ImportError:
            import fitz
        d = Document()
        d.kind, d.path, d.dpi = "pdf", path, dpi
        d._pdf = fitz.open(path)
        d.pages = [Page(i) for i in range(d._pdf.page_count)]
        return d

    @property
    def page_count(self) -> int:
        return len(self.pages)

    def image(self, i: int) -> Image.Image:
        p = self.pages[i]
        if p.image is None and self.kind == "pdf":
            try:
                import pymupdf as fitz
            except ImportError:
                import fitz
            page = self._pdf[i]
            zoom = self.dpi / 72.0
            pix = page.get_pixmap(matrix=fitz.Matrix(zoom, zoom), alpha=False)
            p.image = Image.frombytes("RGB", (pix.width, pix.height), pix.samples)
        return p.image

    # ---------------- 인식 ----------------
    def pdf_text_lines(self, i: int) -> List[Line]:
        page = self._pdf[i]
        zoom = self.dpi / 72.0
        data = page.get_text("dict")
        lines: List[Line] = []
        for bi, blk in enumerate(data.get("blocks", [])):
            if blk.get("type", 0) != 0:
                continue
            for ln in blk.get("lines", []):
                dx, dy = ln.get("dir", (1, 0))
                if abs(dy) > 0.1:      # 세로·회전 글자는 OCR 에 맡김
                    continue
                spans = [s for s in ln.get("spans", []) if s.get("text", "").strip()]
                if not spans:
                    continue
                text = "".join(s["text"] for s in ln["spans"]).strip()
                x0, y0, x1, y1 = ln["bbox"]
                main = max(spans, key=lambda s: len(s["text"]))
                c = main.get("color", 0)
                rgb = ((c >> 16) & 255, (c >> 8) & 255, c & 255)
                bold = bool(main.get("flags", 0) & 16) or "bold" in main.get("font", "").lower()
                lines.append(Line(
                    box=[x0 * zoom, y0 * zoom, x1 * zoom, y1 * zoom], text=text, source="pdf",
                    font_size=main.get("size", 10) * zoom, color=to_hex(rgb), bold=bold, group=bi,
                ))
        return lines

    def recognize(self, i: int, mode: str = "auto") -> List[Block]:
        """mode: auto(텍스트레이어+이미지 속 글자 OCR) | text | ocr"""
        img = self.image(i)
        lines: List[Line] = []
        if self.kind == "pdf" and mode in ("auto", "text"):
            lines = self.pdf_text_lines(i)
        if self.kind != "pdf" or mode in ("auto", "ocr"):
            o = ocr.run_ocr(img)
            if lines:  # 텍스트 레이어와 겹치는 OCR 결과는 버림
                o = [l for l in o if max((overlap_ratio(l.box, t.box) for t in lines), default=0) < 0.4]
            lines += o
        blocks = grouping.group_lines(img, lines)
        self.pages[i].blocks = blocks
        self.pages[i].recognized = True
        return blocks

    @staticmethod
    def apply_protect(blocks: List[Block], glossary: Optional[Glossary]) -> int:
        """용어집에서 '='(보호)로 지정한 글자(로고 등)는 번역·지우기 대상에서 제외."""
        if not glossary:
            return 0
        n = 0
        for b in blocks:
            if b.kind != "keep" and glossary.is_protected(b.src):
                b.kind, b.enabled, b.erase, b.dst = "keep", False, False, b.src
                n += 1
        return n

    # ---------------- 번역 ----------------
    @staticmethod
    def translate_blocks(blocks: List[Block], glossary: Optional[Glossary], engine: str,
                         only_empty: bool = False,
                         progress: Optional[Callable[[int, int], None]] = None) -> Dict[str, int]:
        targets = [b for b in blocks if b.kind != "keep" and b.src.strip()
                   and (not only_empty or not b.dst.strip())]
        if glossary:
            Document.apply_protect(targets, glossary)
            targets = [b for b in targets if b.kind != "keep"]
        stats = {"total": len(targets), "glossary": 0, "mt": 0, "failed": 0}
        mt_list: List[Block] = []
        for b in targets:
            ko = glossary.exact(b.src) if glossary else None
            if ko is not None:
                b.dst, b.translated_by = ko, "용어집"
                stats["glossary"] += 1
            else:
                mt_list.append(b)
        if engine == "none":
            stats["failed"] = len(mt_list)
            return stats
        srcs = [glossary.presubstitute(b.src) if glossary else b.src for b in mt_list]
        res = T.translate_many(srcs, engine, progress=progress)
        for b, r in zip(mt_list, res):
            if r:
                b.dst, b.translated_by = r, engine
                stats["mt"] += 1
            else:
                stats["failed"] += 1
        return stats

    # ---------------- 출력 ----------------
    def rendered(self, i: int, font_name: str = "") -> Image.Image:
        return R.render(self.image(i), self.pages[i].blocks, font_name)

    def export_pdf(self, out_path: str, font_name: str = "", placement: str = "replace",
                   pages: Optional[List[int]] = None, progress=None):
        """placement: replace(번역 페이지로 교체) | after(원본 뒤에 번역 페이지 삽입) | only(번역 페이지만)"""
        try:
            import pymupdf as fitz
        except ImportError:
            import fitz
        src = self._pdf
        out = fitz.open()
        todo = set(pages if pages is not None else [p.index for p in self.pages if p.recognized])
        for i in range(src.page_count):
            pg = src[i]
            translated = i in todo
            if placement != "only" and not (translated and placement == "replace"):
                out.insert_pdf(src, from_page=i, to_page=i)
            if translated:
                img = self.rendered(i, font_name)
                buf = io.BytesIO()
                img.save(buf, format="JPEG", quality=92)
                rect = pg.rect
                np_ = out.new_page(width=rect.width, height=rect.height)
                np_.insert_image(np_.rect, stream=buf.getvalue())
            if progress:
                progress(i + 1, src.page_count)
        out.save(out_path, garbage=3, deflate=True)
        out.close()

    # ---------------- 작업 저장/불러오기 ----------------
    def save_project(self, path: str, font_name: str = ""):
        base = os.path.splitext(path)[0]
        src = self.path
        if self.kind == "image" and not src:      # 클립보드 이미지는 함께 저장
            src = base + "_원본.png"
            self.pages[0].image.save(src)
        data = {
            "version": 1, "kind": self.kind, "path": os.path.abspath(src), "dpi": self.dpi,
            "font": font_name,
            "pages": {str(p.index): {"recognized": p.recognized,
                                     "blocks": [b.to_dict() for b in p.blocks]}
                      for p in self.pages if p.recognized or p.blocks},
        }
        with open(path, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=1)

    @staticmethod
    def load_project(path: str):
        with open(path, encoding="utf-8") as f:
            data = json.load(f)
        src = data["path"]
        if not os.path.exists(src):   # 프로젝트 파일 옆에서 다시 찾기
            alt = os.path.join(os.path.dirname(path), os.path.basename(src))
            if os.path.exists(alt):
                src = alt
            else:
                raise FileNotFoundError(f"원본 파일을 찾을 수 없음: {data['path']}")
        d = Document.from_pdf(src, data.get("dpi", 200)) if data["kind"] == "pdf" else Document.from_image(src)
        max_id = 0
        for k, v in data.get("pages", {}).items():
            p = d.pages[int(k)]
            p.blocks = [Block.from_dict(b) for b in v.get("blocks", [])]
            p.recognized = v.get("recognized", True)
            max_id = max([max_id] + [b.id for b in p.blocks])
        bump_ids(max_id)
        return d, data.get("font", "")


def _flatten(img: Image.Image) -> Image.Image:
    """투명 PNG 등은 흰 배경에 합성해 RGB 로."""
    if img.mode in ("RGBA", "LA") or (img.mode == "P" and "transparency" in img.info):
        rgba = img.convert("RGBA")
        bg = Image.new("RGB", rgba.size, (255, 255, 255))
        bg.paste(rgba, mask=rgba.split()[-1])
        return bg
    return img.convert("RGB")
