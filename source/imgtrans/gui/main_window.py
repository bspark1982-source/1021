"""메인 창: 열기 → ① 글자 인식 → ② 자동 번역 → ③ 수정 → ④ 저장."""
from __future__ import annotations

import copy
import os
from typing import List, Optional

from PIL import Image
from PySide6.QtCore import QItemSelectionModel, QSettings, Qt, QTimer
from PySide6.QtGui import (QAction, QColor, QGuiApplication, QImage, QKeySequence, QPixmap)
from PySide6.QtWidgets import (QAbstractItemView, QCheckBox, QColorDialog, QComboBox,
                               QDoubleSpinBox, QFileDialog, QFormLayout, QGroupBox,
                               QHBoxLayout, QHeaderView, QInputDialog, QLabel, QMainWindow,
                               QMessageBox, QPlainTextEdit, QProgressBar, QPushButton,
                               QSpinBox, QSplitter, QTableWidget, QTableWidgetItem, QToolBar,
                               QVBoxLayout, QWidget)

from .. import __version__
from .. import ocr as OCR
from .. import render as R
from .. import style as S
from .. import translate as T
from ..document import Document
from ..glossary import Glossary
from ..grouping import merge_blocks, split_block
from ..model import Block, overlap_ratio
from .canvas import ImageCanvas
from .dialogs import (ExportTextDialog, GlossaryDialog, ImportTextDialog, PdfExportDialog,
                      parse_pages)
from .worker import Worker

APP_DIR = R.APP_DIR
IMG_EXT = (".png", ".jpg", ".jpeg", ".bmp", ".gif", ".webp", ".tif", ".tiff")


def pil_to_qpixmap(img: Image.Image) -> QPixmap:
    img = img.convert("RGB")
    data = img.tobytes("raw", "RGB")
    qimg = QImage(data, img.width, img.height, img.width * 3, QImage.Format_RGB888).copy()
    return QPixmap.fromImage(qimg)


def qimage_to_pil(q: QImage) -> Image.Image:
    q = q.convertToFormat(QImage.Format_RGB888)
    w, h, bpl = q.width(), q.height(), q.bytesPerLine()
    buf = bytes(q.constBits())[: bpl * h]
    return Image.frombuffer("RGB", (w, h), buf, "raw", "RGB", bpl, 1).copy()


class MainWindow(QMainWindow):
    def __init__(self):
        super().__init__()
        self.setWindowTitle(f"이미지·PDF 한글화 편집기 v{__version__}")
        self.resize(1500, 920)
        self.settings = QSettings("ImageKoTranslator", "app")
        self.glossary = Glossary(os.path.join(APP_DIR, "glossary.csv"))
        self.doc: Optional[Document] = None
        self.cur = 0
        self.view_mode = "result"
        self.dirty = False
        self.undo_stack, self.redo_stack = [], []
        self._ck_key = None
        self._loading = False
        self._workers = []
        self._busy = False
        self.project_path = None

        self.font_name = self.settings.value("font", R.default_font_name(), type=str)
        if self.font_name not in R.available_fonts():
            self.font_name = R.default_font_name()

        self._render_timer = QTimer(self)
        self._render_timer.setSingleShot(True)
        self._render_timer.timeout.connect(self.do_render)

        self._build_ui()
        self._update_enabled()

    # =====================================================================
    # UI 구성
    # =====================================================================
    def _act(self, text, slot, shortcut=None, tip=None, checkable=False):
        a = QAction(text, self)
        if shortcut:
            a.setShortcut(QKeySequence(shortcut))
        if tip:
            a.setToolTip(tip)
            a.setStatusTip(tip)
        a.setCheckable(checkable)
        if slot:
            a.triggered.connect(slot)
        return a

    def _build_ui(self):
        # ---------------- 동작 ----------------
        self.a_open = self._act("열기", self.open_dialog, "Ctrl+O", "이미지(PNG·JPG 등), PDF, 작업 파일 열기")
        self.a_paste = self._act("붙여넣기", self.paste_image, "Ctrl+Shift+V", "클립보드의 이미지(캡처)를 바로 열기")
        self.a_ocr = self._act("① 글자 인식", self.run_recognize, "F5", "영어 글자 위치·내용을 인식하고 문단 단위로 묶음")
        self.a_tr = self._act("② 자동 번역", self.run_translate, "F6", "용어집 적용 후 무료 번역기로 한글 번역")
        self.a_save_img = self._act("③ 이미지 저장", self.save_image, "Ctrl+S", "현재 페이지 번역 결과를 PNG/JPG 로 저장")
        self.a_export_pdf = self._act("PDF 내보내기", self.export_pdf, None, "작업한 페이지를 반영한 PDF 저장")
        self.a_save_all_img = self._act("작업한 모든 페이지 이미지로 저장", self.save_all_images)
        self.a_save_proj = self._act("작업 저장", self.save_project, "Ctrl+Shift+S", "나중에 이어서 수정할 수 있게 작업 상태 저장")
        self.a_open_proj = self._act("작업 열기", self.open_project_dialog)
        self.a_batch = self._act("여러 페이지 일괄 인식+번역…", self.run_batch)
        self.a_undo = self._act("되돌리기", self.undo, "Ctrl+Z")
        self.a_redo = self._act("다시 실행", self.redo, "Ctrl+Y")
        self.a_view = self._act("원본 보기", self.toggle_view, "Space", "원본 ↔ 번역 결과 전환 (스페이스)", True)
        self.a_boxes = self._act("상자 표시", self.toggle_boxes, "B", "블록 상자 표시/숨김 (B)", True)
        self.a_boxes.setChecked(True)
        self.a_fit = self._act("화면 맞춤", lambda: self.canvas.fit(), "Ctrl+0")
        self.a_z100 = self._act("100%", lambda: self.canvas.zoom_100(), "Ctrl+1")
        self.a_zin = self._act("확대", lambda: self.canvas.zoom(1.25), "Ctrl+=")
        self.a_zout = self._act("축소", lambda: self.canvas.zoom(0.8), "Ctrl+-")
        self.a_add = self._act("상자 추가", self.toggle_add_mode, "A",
                               "놓친 글자 영역을 드래그로 추가 (그 영역만 다시 인식·번역)", True)
        self.a_merge = self._act("병합", self.merge_selected, "Ctrl+M", "선택한 블록들을 한 문단으로 합침")
        self.a_split = self._act("줄 단위 분할", self.split_selected, "Ctrl+D", "블록을 원래 줄 단위로 나눔")
        self.a_delete = self._act("블록 삭제", self.delete_selected, "Delete")
        self.a_glossary = self._act("용어집", self.edit_glossary, "Ctrl+G", "고정 번역어(지명·부대명·범례) 관리")
        self.a_export_txt = self._act("원문 내보내기 (파파고·DeepL용)", self.export_text)
        self.a_import_txt = self._act("번역문 붙여넣기", self.import_text)
        self.a_prev = self._act("◀", lambda: self.goto_page(self.cur - 1), "PgUp", "이전 페이지")
        self.a_next = self._act("▶", lambda: self.goto_page(self.cur + 1), "PgDown", "다음 페이지")
        self.a_auto_tr = self._act("인식 후 바로 번역", None, checkable=True)
        self.a_auto_tr.setChecked(self.settings.value("auto_tr", True, type=bool))
        self.a_auto_tr.toggled.connect(lambda v: self.settings.setValue("auto_tr", v))
        self.a_use_gl = self._act("용어집 적용", None, checkable=True)
        self.a_use_gl.setChecked(self.settings.value("use_gl", True, type=bool))
        self.a_use_gl.toggled.connect(lambda v: self.settings.setValue("use_gl", v))
        self.a_dpi = self._act("PDF 해상도(DPI)…", self.set_dpi)
        self.a_help = self._act("사용법", self.show_help, "F1")

        # ---------------- 메뉴 ----------------
        mb = self.menuBar()
        m = mb.addMenu("파일")
        for a in (self.a_open, self.a_paste, None, self.a_save_img, self.a_save_all_img, self.a_export_pdf,
                  None, self.a_save_proj, self.a_open_proj, None, self.a_dpi):
            m.addSeparator() if a is None else m.addAction(a)
        m.addSeparator()
        m.addAction(self._act("종료", self.close))
        m = mb.addMenu("편집")
        for a in (self.a_undo, self.a_redo, None, self.a_add, self.a_merge, self.a_split, self.a_delete):
            m.addSeparator() if a is None else m.addAction(a)
        m = mb.addMenu("번역")
        for a in (self.a_ocr, self.a_tr, self.a_batch, None, self.a_auto_tr, self.a_use_gl, None,
                  self.a_glossary, None, self.a_export_txt, self.a_import_txt):
            m.addSeparator() if a is None else m.addAction(a)
        m = mb.addMenu("보기")
        for a in (self.a_view, self.a_boxes, None, self.a_fit, self.a_z100, self.a_zin, self.a_zout):
            m.addSeparator() if a is None else m.addAction(a)
        mb.addMenu("도움말").addAction(self.a_help)

        # ---------------- 도구 모음 ----------------
        tb = QToolBar("주요 단계")
        tb.setMovable(False)
        tb.setToolButtonStyle(Qt.ToolButtonTextOnly)
        self.addToolBar(tb)
        tb.addAction(self.a_open)
        tb.addAction(self.a_paste)
        tb.addSeparator()
        tb.addAction(self.a_prev)
        self.page_combo = QComboBox()
        self.page_combo.setMinimumWidth(90)
        self.page_combo.currentIndexChanged.connect(lambda i: self.goto_page(i) if i >= 0 else None)
        tb.addWidget(self.page_combo)
        tb.addAction(self.a_next)
        tb.addSeparator()
        tb.addAction(self.a_ocr)
        self.mode_combo = QComboBox()
        self.mode_combo.addItem("PDF: 글자층+이미지 OCR", "auto")
        self.mode_combo.addItem("PDF: 글자층만", "text")
        self.mode_combo.addItem("PDF: 전부 OCR", "ocr")
        self.mode_combo.setToolTip("PDF 페이지 인식 방식 (이미지 파일은 항상 OCR)")
        tb.addWidget(self.mode_combo)
        tb.addAction(self.a_tr)
        self.engine_combo = QComboBox()
        for k, v in T.ENGINES.items():
            self.engine_combo.addItem(v, k)
        eng = self.settings.value("engine", "google", type=str)
        self.engine_combo.setCurrentIndex(max(0, self.engine_combo.findData(eng)))
        self.engine_combo.currentIndexChanged.connect(
            lambda: self.settings.setValue("engine", self.engine_combo.currentData()))
        tb.addWidget(self.engine_combo)
        tb.addAction(self.a_glossary)
        tb.addSeparator()
        tb.addAction(self.a_view)
        tb.addAction(self.a_boxes)
        tb.addAction(self.a_add)
        tb.addAction(self.a_fit)
        tb.addSeparator()
        tb.addWidget(QLabel(" 글꼴 "))
        self.font_combo = QComboBox()
        self.font_combo.addItems(list(R.available_fonts().keys()))
        self.font_combo.setCurrentText(self.font_name)
        self.font_combo.currentTextChanged.connect(self._font_changed)
        tb.addWidget(self.font_combo)
        tb.addSeparator()
        tb.addAction(self.a_save_img)
        tb.addAction(self.a_export_pdf)

        # ---------------- 본문 ----------------
        self.canvas = ImageCanvas()
        self.canvas.selection_changed.connect(self._canvas_selected)
        self.canvas.geometry_changed.connect(self._canvas_geometry)
        self.canvas.box_drawn.connect(self._box_drawn)
        self.canvas.color_picked.connect(self._color_picked)
        self.canvas.files_dropped.connect(lambda fs: self.open_path(fs[0]))

        right = QWidget()
        rl = QVBoxLayout(right)
        rl.setContentsMargins(4, 4, 4, 4)

        filt = QHBoxLayout()
        self.chk_show_keep = QCheckBox("숫자 등 보호 블록도 목록에 표시")
        self.chk_show_keep.toggled.connect(self.rebuild_table)
        filt.addWidget(self.chk_show_keep)
        filt.addStretch()
        self.lbl_stats = QLabel("")
        filt.addWidget(self.lbl_stats)
        rl.addLayout(filt)

        self.table = QTableWidget(0, 5)
        self.table.setHorizontalHeaderLabels(["#", "적용", "영어 원문", "한글 번역 (더블클릭 수정)", "방식"])
        hh = self.table.horizontalHeader()
        hh.setSectionResizeMode(0, QHeaderView.ResizeToContents)
        hh.setSectionResizeMode(1, QHeaderView.ResizeToContents)
        hh.setSectionResizeMode(2, QHeaderView.Stretch)
        hh.setSectionResizeMode(3, QHeaderView.Stretch)
        hh.setSectionResizeMode(4, QHeaderView.ResizeToContents)
        self.table.verticalHeader().setVisible(False)
        self.table.setSelectionBehavior(QAbstractItemView.SelectRows)
        self.table.setWordWrap(True)
        self.table.setEditTriggers(QAbstractItemView.DoubleClicked | QAbstractItemView.EditKeyPressed)
        self.table.itemSelectionChanged.connect(self._table_selected)
        self.table.itemChanged.connect(self._table_item_changed)

        rsplit = QSplitter(Qt.Vertical)
        rsplit.addWidget(self.table)
        rsplit.addWidget(self._build_editor())
        rsplit.setSizes([430, 420])
        rl.addWidget(rsplit)

        split = QSplitter(Qt.Horizontal)
        split.addWidget(self.canvas)
        split.addWidget(right)
        split.setSizes([900, 600])
        self.setCentralWidget(split)

        self.progress = QProgressBar()
        self.progress.setMaximumWidth(260)
        self.progress.setVisible(False)
        self.statusBar().addPermanentWidget(self.progress)
        self.statusBar().showMessage("이미지나 PDF 를 열거나(Ctrl+O), 화면에 끌어놓거나, 캡처를 붙여넣기(Ctrl+Shift+V) 하세요.")

    def _build_editor(self) -> QWidget:
        box = QGroupBox("선택한 블록 수정")
        lay = QVBoxLayout(box)

        lay.addWidget(QLabel("영어 원문 (OCR 오류 수정 가능)"))
        self.ed_src = QPlainTextEdit()
        self.ed_src.setFixedHeight(64)
        self.ed_src.textChanged.connect(self._src_edited)
        lay.addWidget(self.ed_src)

        lay.addWidget(QLabel("한글 번역"))
        self.ed_dst = QPlainTextEdit()
        self.ed_dst.setFixedHeight(84)
        self.ed_dst.textChanged.connect(self._dst_edited)
        lay.addWidget(self.ed_dst)

        r1 = QHBoxLayout()
        self.btn_retr = QPushButton("선택 블록 다시 번역")
        self.btn_retr.clicked.connect(self.retranslate_selected)
        self.btn_gl = QPushButton("용어집에 추가")
        self.btn_gl.setToolTip("원문 → 현재 번역을 용어집에 저장 (다음부터 같은 표기로 자동 번역). 보호 블록은 '항상 보호'로 저장")
        self.btn_gl.clicked.connect(self.add_to_glossary)
        self.btn_kind = QPushButton("번역 대상 ↔ 보호 전환")
        self.btn_kind.setToolTip("숫자로 오인된 글자를 번역 대상으로, 또는 번역하지 않을 글자를 보호로 전환")
        self.btn_kind.clicked.connect(self.toggle_kind)
        r1.addWidget(self.btn_retr)
        r1.addWidget(self.btn_gl)
        r1.addWidget(self.btn_kind)
        lay.addLayout(r1)

        form = QFormLayout()
        r2 = QHBoxLayout()
        self.sp_size = QDoubleSpinBox()
        self.sp_size.setRange(4, 400)
        self.sp_size.setDecimals(1)
        self.sp_size.setSuffix(" px")
        self.sp_size.valueChanged.connect(lambda v: self._style_edit("font_size", float(v)))
        self.chk_fit = QCheckBox("영역에 맞게 자동 축소")
        self.chk_fit.toggled.connect(lambda v: self._style_edit("auto_fit", bool(v)))
        self.chk_bold = QCheckBox("굵게")
        self.chk_bold.toggled.connect(lambda v: self._style_edit("bold", bool(v)))
        self.cb_align = QComboBox()
        for t, k in (("왼쪽", "left"), ("가운데", "center"), ("오른쪽", "right")):
            self.cb_align.addItem(t, k)
        self.cb_align.currentIndexChanged.connect(lambda: self._style_edit("align", self.cb_align.currentData()))
        for w in (self.sp_size, self.chk_fit, self.chk_bold, self.cb_align):
            r2.addWidget(w)
        form.addRow("글자", r2)

        r3 = QHBoxLayout()
        self.btn_fg = QPushButton("글자색")
        self.btn_fg.clicked.connect(lambda: self._pick_color("color"))
        self.btn_bg = QPushButton("배경색")
        self.btn_bg.clicked.connect(lambda: self._pick_color("bg"))
        self.btn_eye = QPushButton("배경색 스포이드")
        self.btn_eye.setCheckable(True)
        self.btn_eye.setToolTip("누른 뒤 이미지에서 원하는 배경 지점을 클릭")
        self.btn_eye.toggled.connect(lambda v: self.canvas.set_mode("pick" if v else "select"))
        self.chk_erase = QCheckBox("원문 지우기")
        self.chk_erase.toggled.connect(lambda v: self._style_edit("erase", bool(v)))
        self.chk_enabled = QCheckBox("적용")
        self.chk_enabled.toggled.connect(lambda v: self._style_edit("enabled", bool(v)))
        for w in (self.btn_fg, self.btn_bg, self.btn_eye, self.chk_erase, self.chk_enabled):
            r3.addWidget(w)
        form.addRow("색·적용", r3)

        r4 = QHBoxLayout()
        self.sp_xywh = []
        for name in ("X", "Y", "W", "H"):
            sp = QSpinBox()
            sp.setRange(0, 50000)
            sp.setPrefix(name + " ")
            sp.valueChanged.connect(self._xywh_edited)
            self.sp_xywh.append(sp)
            r4.addWidget(sp)
        self.sp_angle = QDoubleSpinBox()
        self.sp_angle.setRange(-180, 180)
        self.sp_angle.setDecimals(1)
        self.sp_angle.setSuffix("°")
        self.sp_angle.setToolTip("기울어진 글자 각도 (0 = 가로쓰기)")
        self.sp_angle.valueChanged.connect(self._angle_edited)
        r4.addWidget(self.sp_angle)
        form.addRow("위치·회전", r4)
        lay.addLayout(form)

        r5 = QHBoxLayout()
        for a in (self.a_merge, self.a_split, self.a_delete):
            b = QPushButton(a.text())
            b.setToolTip(a.toolTip())
            b.clicked.connect(a.trigger)
            r5.addWidget(b)
        lay.addLayout(r5)
        tip = QLabel("팁: 상자를 끌면 한글 위치 이동, 오른쪽 아래 모서리를 끌면 크기 조절. "
                     "여러 블록을 선택하면 글자·색 설정이 한꺼번에 적용됨. Ctrl+휠 확대, 가운데 버튼 드래그 이동.")
        tip.setWordWrap(True)
        tip.setStyleSheet("color:#666")
        lay.addWidget(tip)
        lay.addStretch()
        self.editor_box = box
        return box

    # =====================================================================
    # 공통 도우미
    # =====================================================================
    def blocks(self) -> List[Block]:
        return self.doc.pages[self.cur].blocks if self.doc else []

    def block(self, bid) -> Optional[Block]:
        return next((b for b in self.blocks() if b.id == bid), None)

    def selected_blocks(self) -> List[Block]:
        ids = self.canvas.selected_ids()
        return [b for b in self.blocks() if b.id in ids]

    def mark_dirty(self):
        self.dirty = True
        t = self.windowTitle().rstrip(" *")
        self.setWindowTitle(t + " *")

    def checkpoint(self, key=None):
        """되돌리기 지점 저장. 같은 key 의 연속 수정은 한 번만 저장."""
        if not self.doc:
            return
        if key is not None and key == self._ck_key:
            return
        self._ck_key = key
        self.undo_stack.append((self.cur, copy.deepcopy(self.blocks())))
        self.undo_stack = self.undo_stack[-60:]
        self.redo_stack.clear()
        self.mark_dirty()

    def undo(self):
        if not self.undo_stack:
            return
        page, blocks = self.undo_stack.pop()
        self.redo_stack.append((page, copy.deepcopy(self.doc.pages[page].blocks)))
        self.doc.pages[page].blocks = blocks
        self._ck_key = None
        if page != self.cur:
            self.goto_page(page)
        self.refresh_all()

    def redo(self):
        if not self.redo_stack:
            return
        page, blocks = self.redo_stack.pop()
        self.undo_stack.append((page, copy.deepcopy(self.doc.pages[page].blocks)))
        self.doc.pages[page].blocks = blocks
        self._ck_key = None
        if page != self.cur:
            self.goto_page(page)
        self.refresh_all()

    def _update_enabled(self):
        has = self.doc is not None
        busy = self._busy
        for a in (self.a_ocr, self.a_tr, self.a_save_img, self.a_save_proj, self.a_merge, self.a_split,
                  self.a_delete, self.a_add, self.a_export_txt, self.a_import_txt, self.a_view, self.a_fit):
            a.setEnabled(has and not busy)
        is_pdf = has and self.doc.kind == "pdf"
        for a in (self.a_export_pdf, self.a_batch, self.a_save_all_img, self.a_prev, self.a_next):
            a.setEnabled(is_pdf and not busy)
        self.page_combo.setEnabled(is_pdf and not busy)
        self.mode_combo.setEnabled(is_pdf and not busy)
        self.a_open.setEnabled(not busy)
        self.a_paste.setEnabled(not busy)
        self.editor_box.setEnabled(has and not busy and bool(self.canvas.selected_ids()))

    def run_task(self, fn, on_done, label: str, *args):
        self._busy = True
        self._update_enabled()
        self.progress.setVisible(True)
        self.progress.setRange(0, 0)
        self.statusBar().showMessage(label + " …")
        w = Worker(fn, *args)

        def prog(i, n, msg):
            self.progress.setRange(0, max(1, n))
            self.progress.setValue(i)
            if msg:
                self.statusBar().showMessage(msg)

        def fin(res):
            self._finish(w)
            on_done(res)

        def fail(msg):
            self._finish(w)
            QMessageBox.critical(self, "오류", f"{label} 중 오류가 발생함.\n\n{msg[:2500]}")

        w.progress.connect(prog)
        w.done.connect(fin)
        w.failed.connect(fail)
        self._workers.append(w)
        w.start()

    def _finish(self, w):
        self._busy = False
        self.statusBar().clearMessage()
        self.progress.setVisible(False)
        if w in self._workers:
            self._workers.remove(w)
        self._update_enabled()

    # =====================================================================
    # 열기 / 페이지
    # =====================================================================
    def _confirm_discard(self) -> bool:
        if not self.dirty:
            return True
        r = QMessageBox.question(self, "확인", "저장하지 않은 작업이 있음. 계속하면 사라짐. 계속할까요?")
        return r == QMessageBox.Yes

    def open_dialog(self):
        d = self.settings.value("last_dir", "", type=str)
        p, _ = QFileDialog.getOpenFileName(
            self, "열기", d,
            "지원 파일 (*.png *.jpg *.jpeg *.bmp *.gif *.webp *.tif *.tiff *.pdf *.json);;"
            "이미지 (*.png *.jpg *.jpeg *.bmp *.gif *.webp *.tif *.tiff);;PDF (*.pdf);;작업 파일 (*.json)")
        if p:
            self.open_path(p)

    def open_project_dialog(self):
        d = self.settings.value("last_dir", "", type=str)
        p, _ = QFileDialog.getOpenFileName(self, "작업 열기", d, "작업 파일 (*.ikt.json *.json)")
        if p:
            self.open_path(p)

    def open_path(self, p: str):
        if not self._confirm_discard():
            return
        self.settings.setValue("last_dir", os.path.dirname(p))
        ext = os.path.splitext(p)[1].lower()
        try:
            if ext == ".json":
                doc, font = Document.load_project(p)
                if font and font in R.available_fonts():
                    self.font_combo.setCurrentText(font)
                self.project_path = p
            elif ext == ".pdf":
                doc = Document.from_pdf(p, self.settings.value("dpi", 200, type=int))
                self.project_path = None
            elif ext in IMG_EXT:
                doc = Document.from_image(p)
                self.project_path = None
            else:
                QMessageBox.warning(self, "열기", "지원하지 않는 형식임.")
                return
        except Exception as e:
            QMessageBox.critical(self, "열기 실패", str(e))
            return
        self._set_doc(doc)
        self.setWindowTitle(f"이미지·PDF 한글화 편집기 v{__version__} — {os.path.basename(p)}")
        self.dirty = False

    def paste_image(self):
        q = QGuiApplication.clipboard().image()
        if q.isNull():
            QMessageBox.information(self, "붙여넣기", "클립보드에 이미지가 없음. 화면 캡처(Win+Shift+S) 후 다시 시도.")
            return
        if not self._confirm_discard():
            return
        self.project_path = None
        self._set_doc(Document.from_pil(qimage_to_pil(q)))
        self.setWindowTitle(f"이미지·PDF 한글화 편집기 v{__version__} — 클립보드 이미지")
        self.dirty = False

    def _set_doc(self, doc: Document):
        self.doc = doc
        self.cur = 0
        self.undo_stack.clear()
        self.redo_stack.clear()
        self.page_combo.blockSignals(True)
        self.page_combo.clear()
        for i in range(doc.page_count):
            self.page_combo.addItem(f"{i + 1} / {doc.page_count}")
        self.page_combo.blockSignals(False)
        self.goto_page(0, force=True)
        self.canvas.fit()
        self._update_enabled()
        if doc.kind == "pdf":
            self.statusBar().showMessage(f"PDF {doc.page_count}쪽. 번역할 페이지로 이동한 뒤 ① 글자 인식(F5).")
        elif not doc.pages[0].recognized:
            self.statusBar().showMessage("① 글자 인식(F5)을 누르세요.")

    def goto_page(self, i: int, force=False):
        if not self.doc or not (0 <= i < self.doc.page_count):
            return
        if i == self.cur and not force:
            return
        self.cur = i
        self._ck_key = None
        self.page_combo.blockSignals(True)
        self.page_combo.setCurrentIndex(i)
        self.page_combo.blockSignals(False)
        self.doc.image(i)  # PDF 페이지 렌더링
        self.refresh_all()
        if not force:
            self.canvas.fit()

    def set_dpi(self):
        v, ok = QInputDialog.getInt(self, "PDF 해상도", "PDF 페이지를 이미지로 바꿀 때 해상도(DPI)\n"
                                    "높을수록 글자 인식이 정확하지만 느림 (권장 200)",
                                    self.settings.value("dpi", 200, type=int), 72, 400, 25)
        if ok:
            self.settings.setValue("dpi", v)
            QMessageBox.information(self, "PDF 해상도", "다음에 여는 PDF 부터 적용됨.")

    # =====================================================================
    # 화면 갱신
    # =====================================================================
    def refresh_all(self):
        self.canvas.set_blocks(self.blocks())
        self.rebuild_table()
        self.schedule_render()
        self._load_editor()

    def schedule_render(self):
        self._render_timer.start(180)

    def do_render(self):
        if not self.doc:
            return
        if self.view_mode == "original":
            img = self.doc.image(self.cur)
        else:
            img = R.render(self.doc.image(self.cur), self.blocks(), self.font_name)
        self.canvas.set_pixmap(pil_to_qpixmap(img))
        self._update_stats()

    def _update_stats(self):
        bl = self.blocks()
        t = [b for b in bl if b.kind != "keep"]
        done = sum(1 for b in t if b.dst.strip() and b.enabled)
        keep = len(bl) - len(t)
        self.lbl_stats.setText(f"번역 대상 {len(t)} · 번역됨 {done} · 보호 {keep}")

    def toggle_view(self, checked):
        self.view_mode = "original" if checked else "result"
        self.do_render()

    def toggle_boxes(self, checked):
        self.canvas.set_boxes_visible(checked)

    def _font_changed(self, name):
        self.font_name = name
        self.settings.setValue("font", name)
        self.schedule_render()

    # ---------------- 표 ----------------
    def _visible_blocks(self):
        show_keep = self.chk_show_keep.isChecked()
        return [(n, b) for n, b in enumerate(self.blocks(), 1) if show_keep or b.kind != "keep"]

    def rebuild_table(self):
        self._loading = True
        sel = set(self.canvas.selected_ids())
        self.table.setRowCount(0)
        rows = self._visible_blocks()
        self.table.setRowCount(len(rows))
        for r, (n, b) in enumerate(rows):
            it = QTableWidgetItem(str(n))
            it.setData(Qt.UserRole, b.id)
            it.setFlags(Qt.ItemIsSelectable | Qt.ItemIsEnabled)
            self.table.setItem(r, 0, it)
            chk = QTableWidgetItem()
            chk.setFlags(Qt.ItemIsSelectable | Qt.ItemIsEnabled | Qt.ItemIsUserCheckable)
            chk.setCheckState(Qt.Checked if b.enabled else Qt.Unchecked)
            self.table.setItem(r, 1, chk)
            s = QTableWidgetItem(b.src)
            s.setFlags(Qt.ItemIsSelectable | Qt.ItemIsEnabled)
            self.table.setItem(r, 2, s)
            d = QTableWidgetItem(b.dst)
            self.table.setItem(r, 3, d)
            kind = {"keep": "보호", "manual": "수동"}.get(b.kind, "")
            m = QTableWidgetItem(kind or b.translated_by or "-")
            m.setFlags(Qt.ItemIsSelectable | Qt.ItemIsEnabled)
            self.table.setItem(r, 4, m)
            self._color_row(r, b)
        self._select_rows([r for r, (n, b) in enumerate(rows) if b.id in sel])
        self.table.resizeRowsToContents()
        self._loading = False
        self._update_stats()

    def _color_row(self, r, b: Block):
        if b.kind == "keep" or not b.enabled:
            col = QColor(150, 150, 150)
        elif not b.dst.strip():
            col = QColor(200, 110, 0)
        else:
            col = QColor(0, 0, 0)
        for c in range(5):
            it = self.table.item(r, c)
            if it:
                it.setForeground(col)

    def _select_rows(self, rows):
        sm = self.table.selectionModel()
        sm.clearSelection()
        model = self.table.model()
        for r in rows:
            sm.select(model.index(r, 0), QItemSelectionModel.Select | QItemSelectionModel.Rows)

    def _row_of(self, bid) -> int:
        for r in range(self.table.rowCount()):
            it = self.table.item(r, 0)
            if it and it.data(Qt.UserRole) == bid:
                return r
        return -1

    def _update_row(self, b: Block):
        r = self._row_of(b.id)
        if r < 0:
            return
        self._loading = True
        self.table.item(r, 1).setCheckState(Qt.Checked if b.enabled else Qt.Unchecked)
        self.table.item(r, 2).setText(b.src)
        self.table.item(r, 3).setText(b.dst)
        kind = {"keep": "보호", "manual": "수동"}.get(b.kind, "")
        self.table.item(r, 4).setText(kind or b.translated_by or "-")
        self._color_row(r, b)
        self._loading = False

    def _table_selected(self):
        if self._loading:
            return
        ids = []
        for idx in self.table.selectionModel().selectedRows():
            it = self.table.item(idx.row(), 0)
            if it:
                ids.append(it.data(Qt.UserRole))
        self.canvas.select_ids(ids, center=True)
        self._load_editor()

    def _table_item_changed(self, it: QTableWidgetItem):
        if self._loading:
            return
        bid = self.table.item(it.row(), 0).data(Qt.UserRole)
        b = self.block(bid)
        if not b:
            return
        if it.column() == 1:
            self.checkpoint()
            b.enabled = it.checkState() == Qt.Checked
        elif it.column() == 3:
            self.checkpoint()
            b.dst = it.text()
            b.translated_by = "수정"
        else:
            return
        self.canvas.update_block(b)
        self._update_row(b)
        self._load_editor()
        self.schedule_render()

    # ---------------- 캔버스 ----------------
    def _canvas_selected(self, ids):
        self._loading = True
        rows = [r for r in (self._row_of(bid) for bid in ids) if r >= 0]
        self._select_rows(rows)
        if rows:
            self.table.scrollToItem(self.table.item(min(rows), 0))
        self._loading = False
        self._load_editor()

    def _canvas_geometry(self, boxes: dict):
        self.checkpoint()
        for bid, box in boxes.items():
            b = self.block(bid)
            if b:
                ow, oh = b.box[2] - b.box[0], b.box[3] - b.box[1]
                nw, nh = box[2] - box[0], box[3] - box[1]
                if b.angle and b.rot_w and ow > 0 and oh > 0 and (abs(nw - ow) > 0.5 or abs(nh - oh) > 0.5):
                    k = ((nw / ow) + (nh / oh)) / 2   # 기울어진 글자 영역도 같은 비율로
                    b.rot_w, b.rot_h = b.rot_w * k, b.rot_h * k
                b.box = [round(v, 1) for v in box]
        self._load_editor()
        self.schedule_render()

    def toggle_add_mode(self, checked):
        if checked:
            self.btn_eye.setChecked(False)
            self.canvas.set_mode("add")
            self.statusBar().showMessage("이미지에서 글자 영역을 드래그하면 그 영역만 인식·번역함. 다시 누르면 종료.")
        else:
            self.canvas.set_mode("select")

    def _box_drawn(self, box):
        img = self.doc.image(self.cur)
        W, H = img.size
        box = [max(0, box[0]), max(0, box[1]), min(W, box[2]), min(H, box[3])]
        arr = S.analyze(img)
        bg = S.estimate_bg(arr, box)
        fg = S.estimate_fg(arr, box, bg)
        b = Block(src="", dst="", box=box, erase_boxes=[list(box)], font_size=max(8.0, (box[3] - box[1]) * 0.6),
                  color=S.to_hex(fg), bg=S.to_hex(bg), kind="manual", source="manual")
        self.checkpoint()
        self.blocks().append(b)
        self.refresh_all()
        self.canvas.select_ids([b.id])
        self._load_editor()

        m = 12  # 글자 검출이 잘 되도록 주변 여백 포함
        ox, oy = int(max(0, box[0] - m)), int(max(0, box[1] - m))
        crop = img.crop((ox, oy, int(min(W, box[2] + m)), int(min(H, box[3] + m))))
        engine = self.engine_combo.currentData()
        gl = self.glossary if self.a_use_gl.isChecked() else None

        def job(w):
            lines = [l for l in OCR.run_ocr(crop)
                     if overlap_ratio([l.x0 + ox, l.y0 + oy, l.x1 + ox, l.y1 + oy], box) > 0.5]
            lines.sort(key=lambda l: (round(l.y0 / 8), l.x0))
            src = " ".join(l.text for l in lines)
            res = {"src": src, "size": None, "rot": None}
            if lines:
                h = sorted((l.rh or l.h) for l in lines)[len(lines) // 2]
                res["size"] = S.font_px_from_line_height(h)
            area = max(1.0, (box[2] - box[0]) * (box[3] - box[1]))
            if (len(lines) == 1 and lines[0].angle
                    and lines[0].w * lines[0].h / area > 0.4):   # 상자 대부분을 차지하는 기울어진 글자 1줄
                l = lines[0]
                quad = [[x + ox, y + oy] for x, y in l.quad]
                bgc, fgc = S.estimate_quad(arr, quad)
                res["rot"] = dict(angle=l.angle, rw=l.rw, rh=l.rh, quad=quad,
                                  bg=S.to_hex(bgc), color=S.to_hex(fgc))
            tmp = Block(src=src)
            if src:
                Document.translate_blocks([tmp], gl, engine)
            res.update(dst=tmp.dst, by=tmp.translated_by, kind=tmp.kind)
            return res

        def done(res):
            if self.block(b.id) is None:
                return
            b.src, b.dst, b.translated_by = res["src"], res["dst"], res["by"]
            if res["kind"] == "keep":
                b.kind, b.enabled, b.erase = "keep", False, False
            if res["size"]:
                b.font_size = res["size"]
            r = res["rot"]
            if r:
                b.angle, b.rot_w, b.rot_h = r["angle"], r["rw"], r["rh"]
                b.erase_polys, b.erase_boxes = [r["quad"]], []
                b.bg, b.color, b.align = r["bg"], r["color"], "center"
            self.canvas.update_block(b)
            self.rebuild_table()
            self.canvas.select_ids([b.id])
            self._load_editor()
            self.schedule_render()
            if not res["src"]:
                self.statusBar().showMessage("글자를 인식하지 못함. 번역 칸에 직접 입력하세요.")
            elif r:
                self.statusBar().showMessage(f"기울어진 글자로 인식함 ({r['angle']:.0f}°).")

        self.run_task(job, done, "추가한 영역 인식")

    def _color_picked(self, x, y):
        img = self.doc.image(self.cur)
        if not (0 <= x < img.width and 0 <= y < img.height):
            return
        c = img.getpixel((int(x), int(y)))
        hexc = S.to_hex(c)
        sel = self.selected_blocks()
        if sel:
            self.checkpoint()
            for b in sel:
                b.bg = hexc
            self._load_editor()
            self.schedule_render()
        self.btn_eye.setChecked(False)

    # ---------------- 편집 패널 ----------------
    def _load_editor(self):
        sel = self.selected_blocks()
        self.editor_box.setEnabled(bool(sel) and not self._busy)
        self._loading = True
        if not sel:
            self.ed_src.setPlainText("")
            self.ed_dst.setPlainText("")
            self._loading = False
            return
        b = sel[0]
        multi = len(sel) > 1
        self.ed_src.setEnabled(not multi)
        self.ed_dst.setEnabled(not multi)
        if self.ed_src.toPlainText() != b.src and not multi:
            self.ed_src.setPlainText(b.src)
        if self.ed_dst.toPlainText() != b.dst and not multi:
            self.ed_dst.setPlainText(b.dst)
        if multi:
            self.ed_src.setPlainText(f"({len(sel)}개 블록 선택됨 — 글자·색 설정은 모두에 적용)")
            self.ed_dst.setPlainText("")
        self.sp_size.setValue(b.font_size)
        self.chk_fit.setChecked(b.auto_fit)
        self.chk_bold.setChecked(b.bold)
        self.cb_align.setCurrentIndex(max(0, self.cb_align.findData(b.align)))
        self.chk_erase.setChecked(b.erase)
        self.chk_enabled.setChecked(b.enabled)
        self._swatch(self.btn_fg, b.color)
        self._swatch(self.btn_bg, b.bg)
        x0, y0, x1, y1 = b.box
        for sp, v in zip(self.sp_xywh, (x0, y0, x1 - x0, y1 - y0)):
            sp.setValue(int(round(v)))
            sp.setEnabled(not multi)
        self.sp_angle.setValue(b.angle)
        self.sp_angle.setEnabled(not multi)
        self.a_split.setEnabled(not multi and len(b.erase_boxes) > 1)
        self.a_merge.setEnabled(multi)
        self._loading = False

    @staticmethod
    def _swatch(btn, hexc):
        c = QColor(hexc)
        fg = "#000" if c.lightness() > 140 else "#fff"
        btn.setStyleSheet(f"background:{hexc}; color:{fg}; border:1px solid #888; padding:3px 8px;")

    def _src_edited(self):
        if self._loading:
            return
        sel = self.selected_blocks()
        if len(sel) != 1:
            return
        b = sel[0]
        self.checkpoint(("src", b.id))
        b.src = self.ed_src.toPlainText()
        self._update_row(b)

    def _dst_edited(self):
        if self._loading:
            return
        sel = self.selected_blocks()
        if len(sel) != 1:
            return
        b = sel[0]
        self.checkpoint(("dst", b.id))
        b.dst = self.ed_dst.toPlainText()
        b.translated_by = "수정"
        self._update_row(b)
        self.canvas.update_block(b)
        self.schedule_render()

    def _style_edit(self, field, value):
        if self._loading:
            return
        sel = self.selected_blocks()
        if not sel:
            return
        self.checkpoint((field, tuple(b.id for b in sel)))
        for b in sel:
            setattr(b, field, value)
            self.canvas.update_block(b)
            if field == "enabled":
                self._update_row(b)
        self.schedule_render()

    def _pick_color(self, field):
        sel = self.selected_blocks()
        if not sel:
            return
        c = QColorDialog.getColor(QColor(getattr(sel[0], field)), self, "색 선택")
        if c.isValid():
            self.checkpoint()
            for b in sel:
                setattr(b, field, c.name())
            self._load_editor()
            self.schedule_render()

    def _xywh_edited(self):
        if self._loading:
            return
        sel = self.selected_blocks()
        if len(sel) != 1:
            return
        b = sel[0]
        self.checkpoint(("xywh", b.id))
        x, y, w, h = [sp.value() for sp in self.sp_xywh]
        b.box = [x, y, x + max(4, w), y + max(4, h)]
        self.canvas.update_block(b)
        self.schedule_render()

    def _angle_edited(self, v):
        if self._loading:
            return
        sel = self.selected_blocks()
        if len(sel) != 1:
            return
        b = sel[0]
        self.checkpoint(("angle", b.id))
        if v and not b.rot_w:          # 가로 블록을 돌리는 경우: 현재 상자 크기를 기준으로
            b.rot_w, b.rot_h = b.box[2] - b.box[0], b.box[3] - b.box[1]
        b.angle = float(v)
        self.schedule_render()

    # =====================================================================
    # ① 인식
    # =====================================================================
    def run_recognize(self):
        if not self.doc:
            return
        if self.blocks():
            r = QMessageBox.question(self, "다시 인식", "이 페이지의 기존 블록과 번역/수정 내용이 모두 바뀜. 계속할까요?")
            if r != QMessageBox.Yes:
                return
        mode = self.mode_combo.currentData()
        page = self.cur

        def job(w):
            w.report(0, 0, "글자 인식 중 (처음 실행 시 수십 초 걸릴 수 있음)…")
            return self.doc.recognize(page, mode)

        def done(blocks):
            if self.a_use_gl.isChecked():
                Document.apply_protect(blocks, self.glossary)
            self._ck_key = None
            self.redo_stack.clear()
            self.mark_dirty()
            self.refresh_all()
            n = sum(1 for b in blocks if b.kind != "keep")
            k = len(blocks) - n
            self.statusBar().showMessage(f"인식 완료: 번역 대상 {n}개, 숫자 등 보호 {k}개.")
            if self.a_auto_tr.isChecked() and self.engine_combo.currentData() != "none":
                self.run_translate(skip_confirm=True)

        # 되돌리기 지점은 인식 전에
        self.undo_stack.append((self.cur, copy.deepcopy(self.blocks())))
        self.run_task(job, done, "글자 인식")

    # =====================================================================
    # ② 번역
    # =====================================================================
    def run_translate(self, skip_confirm=False, blocks=None):
        if not self.doc:
            return
        targets = blocks if blocks is not None else self.blocks()
        if not any(b.kind != "keep" for b in targets):
            QMessageBox.information(self, "번역", "번역할 블록이 없음. 먼저 ① 글자 인식을 하세요.")
            return
        only_empty = False
        if blocks is None and not skip_confirm and any(b.dst.strip() for b in targets if b.kind != "keep"):
            box = QMessageBox(self)
            box.setWindowTitle("자동 번역")
            box.setText("이미 번역된 블록이 있음.")
            b1 = box.addButton("번역 안 된 블록만", QMessageBox.AcceptRole)
            b2 = box.addButton("전부 다시 번역 (수정 내용 덮어씀)", QMessageBox.DestructiveRole)
            box.addButton("취소", QMessageBox.RejectRole)
            box.exec()
            if box.clickedButton() == b1:
                only_empty = True
            elif box.clickedButton() != b2:
                return
        engine = self.engine_combo.currentData()
        gl = self.glossary if self.a_use_gl.isChecked() else None
        self.checkpoint()
        self._ck_key = None

        def job(w):
            return Document.translate_blocks(targets, gl, engine, only_empty,
                                             progress=lambda i, n: w.report(i, n, f"번역 중 {i}/{n}"))

        def done(st):
            self.refresh_all()
            msg = f"번역 완료: 용어집 {st['glossary']}개, 번역기 {st['mt']}개"
            if st["failed"]:
                msg += f", 실패/미번역 {st['failed']}개"
            self.statusBar().showMessage(msg)
            if st["failed"] and engine != "none":
                QMessageBox.warning(
                    self, "일부 번역 실패",
                    f"{st['failed']}개 블록을 번역하지 못함 (인터넷 연결 또는 번역 서버 차단 가능성).\n\n"
                    "· 잠시 후 다시 ② 자동 번역 → '번역 안 된 블록만'\n"
                    "· 번역기를 MyMemory 로 바꿔 보기\n"
                    "· 번역 메뉴 → 원문 내보내기 → 파파고 등에서 번역 → 번역문 붙여넣기")

        self.run_task(job, done, "번역")

    def retranslate_selected(self):
        sel = [b for b in self.selected_blocks() if b.kind != "keep"]
        if not sel:
            return
        engine = self.engine_combo.currentData()
        gl = self.glossary if self.a_use_gl.isChecked() else None
        self.checkpoint()
        self._ck_key = None

        def job(w):
            return Document.translate_blocks(sel, gl, engine)

        def done(st):
            for b in sel:
                self._update_row(b)
                self.canvas.update_block(b)
            self._load_editor()
            self.schedule_render()
            if st["failed"]:
                self.statusBar().showMessage("번역 실패. 직접 입력하거나 다른 번역기를 선택하세요.")

        self.run_task(job, done, "선택 블록 번역")

    def add_to_glossary(self):
        sel = [b for b in self.selected_blocks() if b.src.strip() and (b.dst.strip() or b.kind == "keep")]
        if not sel:
            QMessageBox.information(self, "용어집", "원문과 번역이 모두 있는 블록(또는 보호 블록)을 선택하세요.")
            return
        n = 0
        for b in sel:
            en = " ".join(b.src.split())
            ko = "=" if b.kind == "keep" else " ".join(b.dst.split())   # 보호 블록 → 항상 보호
            self.glossary.items = [(e, k) for e, k in self.glossary.items if e.lower() != en.lower()]
            self.glossary.items.append((en, ko))
            n += 1
        self.glossary.save()
        self.statusBar().showMessage(f"용어집에 {n}개 저장함 (다음 번역부터 적용).")

    def toggle_kind(self):
        sel = self.selected_blocks()
        if not sel:
            return
        self.checkpoint()
        for b in sel:
            if b.kind == "keep":
                b.kind, b.enabled, b.erase = "text", True, True
                if b.dst == b.src:
                    b.dst = ""
            else:
                b.kind, b.enabled, b.erase = "keep", False, False
        self.refresh_all()

    def edit_glossary(self):
        dlg = GlossaryDialog(self.glossary, self)
        if dlg.exec():
            self.statusBar().showMessage(f"용어집 저장: {len(self.glossary.items)}개 항목")

    def _manual_targets(self):
        return [b for b in self.blocks() if b.kind != "keep" and b.enabled and b.src.strip()]

    def export_text(self):
        t = self._manual_targets()
        if not t:
            return
        srcs = [self.glossary.presubstitute(b.src) if self.a_use_gl.isChecked() else b.src for b in t]
        ExportTextDialog(T.export_numbered(srcs), self).exec()

    def import_text(self):
        t = self._manual_targets()
        if not t:
            return
        dlg = ImportTextDialog(self)
        if not dlg.exec():
            return
        res = T.import_numbered(dlg.text(), len(t))
        self.checkpoint()
        n = 0
        for b, r in zip(t, res):
            if r:
                b.dst, b.translated_by = r, "붙여넣기"
                n += 1
        self.refresh_all()
        self.statusBar().showMessage(f"번역문 {n}/{len(t)}개 적용함.")

    # =====================================================================
    # 블록 구조 편집
    # =====================================================================
    def merge_selected(self):
        sel = self.selected_blocks()
        if len(sel) < 2:
            self.statusBar().showMessage("병합하려면 블록을 2개 이상 선택하세요 (Ctrl+클릭 또는 드래그).")
            return
        self.checkpoint()
        m = merge_blocks(sel)
        bl = self.blocks()
        idx = min(bl.index(b) for b in sel)
        for b in sel:
            bl.remove(b)
        bl.insert(idx, m)
        self.refresh_all()
        self.canvas.select_ids([m.id])
        self._load_editor()
        self.statusBar().showMessage("병합함. 번역문이 어색하면 '선택 블록 다시 번역'을 누르세요.")

    def split_selected(self):
        sel = self.selected_blocks()
        if len(sel) != 1 or len(sel[0].erase_boxes) < 2:
            return
        self.checkpoint()
        b = sel[0]
        parts = split_block(self.doc.image(self.cur), b)
        bl = self.blocks()
        idx = bl.index(b)
        bl[idx:idx + 1] = parts
        self.refresh_all()
        self.canvas.select_ids([p.id for p in parts])
        self._load_editor()
        self.statusBar().showMessage("분할함. 필요하면 선택된 블록을 '다시 번역' 하세요.")

    def delete_selected(self):
        sel = self.selected_blocks()
        if not sel:
            return
        self.checkpoint()
        ids = {b.id for b in sel}
        self.doc.pages[self.cur].blocks = [b for b in self.blocks() if b.id not in ids]
        self.refresh_all()

    # =====================================================================
    # 저장 / 내보내기
    # =====================================================================
    def _default_out(self, suffix=".png", page=None):
        page = self.cur if page is None else page
        if self.doc.path:
            d = os.path.dirname(self.doc.path)
            stem = os.path.splitext(os.path.basename(self.doc.path))[0]
        else:
            d, stem = self.settings.value("last_dir", "", type=str), "캡처"
        if self.doc.kind == "pdf":
            stem += f"_p{page + 1}"
        return os.path.join(d, f"{stem}_한글{suffix}")

    def save_image(self):
        if not self.doc:
            return
        p, flt = QFileDialog.getSaveFileName(self, "번역 이미지 저장", self._default_out(),
                                             "PNG (*.png);;JPG (*.jpg)")
        if not p:
            return
        img = R.render(self.doc.image(self.cur), self.blocks(), self.font_name)
        if p.lower().endswith((".jpg", ".jpeg")):
            img.save(p, quality=95)
        else:
            if not p.lower().endswith(".png"):
                p += ".png"
            img.save(p)
        self.statusBar().showMessage(f"저장함: {p}")

    def save_all_images(self):
        pages = [p.index for p in self.doc.pages if p.recognized]
        if not pages:
            QMessageBox.information(self, "저장", "작업한 페이지가 없음.")
            return
        d = QFileDialog.getExistingDirectory(self, "저장할 폴더 선택", os.path.dirname(self._default_out()))
        if not d:
            return
        font = self.font_name

        def job(w):
            out = []
            for k, i in enumerate(pages):
                w.report(k, len(pages), f"{i + 1}쪽 저장 중")
                p = os.path.join(d, os.path.basename(self._default_out(page=i)))
                self.doc.rendered(i, font).save(p)
                out.append(p)
            return out

        self.run_task(job, lambda out: self.statusBar().showMessage(f"{len(out)}개 이미지 저장: {d}"), "이미지 저장")

    def export_pdf(self):
        if not self.doc or self.doc.kind != "pdf":
            return
        pages = [p.index for p in self.doc.pages if p.recognized]
        if not pages:
            QMessageBox.information(self, "PDF 내보내기", "작업(인식)한 페이지가 없음.")
            return
        dlg = PdfExportDialog(pages, self.doc.page_count, self)
        if not dlg.exec():
            return
        placement = dlg.placement()
        p, _ = QFileDialog.getSaveFileName(self, "PDF 저장",
                                           os.path.splitext(self._default_out(".pdf"))[0].rsplit("_p", 1)[0] + "_한글.pdf",
                                           "PDF (*.pdf)")
        if not p:
            return
        if os.path.abspath(p) == os.path.abspath(self.doc.path):
            QMessageBox.warning(self, "PDF 내보내기", "원본 PDF 에 덮어쓸 수 없음. 다른 이름을 지정하세요.")
            return
        font = self.font_name

        def job(w):
            self.doc.export_pdf(p, font, placement, pages,
                                progress=lambda i, n: w.report(i, n, f"PDF 만드는 중 {i}/{n}"))
            return p

        self.run_task(job, lambda out: self.statusBar().showMessage(f"PDF 저장함: {out}"), "PDF 내보내기")

    def save_project(self):
        if not self.doc:
            return
        default = getattr(self, "project_path", None) or os.path.splitext(self._default_out())[0].replace("_한글", "") + ".ikt.json"
        p, _ = QFileDialog.getSaveFileName(self, "작업 저장", default, "작업 파일 (*.ikt.json)")
        if not p:
            return
        if not p.endswith(".json"):
            p += ".ikt.json"
        try:
            self.doc.save_project(p, self.font_name)
        except Exception as e:
            QMessageBox.critical(self, "작업 저장 실패", str(e))
            return
        self.project_path = p
        self.dirty = False
        self.setWindowTitle(self.windowTitle().rstrip(" *"))
        self.statusBar().showMessage(f"작업 저장함: {p}")

    # =====================================================================
    # 일괄 처리
    # =====================================================================
    def run_batch(self):
        n = self.doc.page_count
        text, ok = QInputDialog.getText(self, "여러 페이지 일괄 처리",
                                        f"인식+번역할 페이지 (예: 1-3,5)\n전체 {n}쪽. 이미 작업한 페이지는 건너뜀.",
                                        text=f"1-{n}")
        if not ok:
            return
        try:
            pages = parse_pages(text, n)
        except ValueError:
            QMessageBox.warning(self, "일괄 처리", "페이지 형식이 잘못됨.")
            return
        mode = self.mode_combo.currentData()
        engine = self.engine_combo.currentData()
        gl = self.glossary if self.a_use_gl.isChecked() else None

        def job(w):
            done_pages, failed = [], 0
            for k, i in enumerate(pages):
                if self.doc.pages[i].recognized:
                    continue
                w.report(k, len(pages), f"{i + 1}쪽 인식 중 ({k + 1}/{len(pages)})")
                bl = self.doc.recognize(i, mode)
                Document.apply_protect(bl, gl)
                w.report(k, len(pages), f"{i + 1}쪽 번역 중 ({k + 1}/{len(pages)})")
                st = Document.translate_blocks(bl, gl, engine)
                failed += st["failed"]
                done_pages.append(i)
            return done_pages, failed

        def done(res):
            pages_done, failed = res
            self.mark_dirty()
            self.refresh_all()
            msg = f"{len(pages_done)}쪽 처리 완료."
            if failed:
                msg += f" 번역 실패 블록 {failed}개 (해당 페이지에서 '번역 안 된 블록만' 다시 번역)."
            QMessageBox.information(self, "일괄 처리", msg + "\n각 페이지를 열어 검수한 뒤 PDF 내보내기를 하세요.")

        self.run_task(job, done, "일괄 처리")

    # =====================================================================
    # 기타
    # =====================================================================
    def show_help(self):
        QMessageBox.information(self, "사용법", HELP_TEXT)

    def keyPressEvent(self, e):
        if e.key() == Qt.Key_Escape:
            self.a_add.setChecked(False)
            self.btn_eye.setChecked(False)
            self.canvas.set_mode("select")
        super().keyPressEvent(e)

    def closeEvent(self, e):
        if self.dirty:
            r = QMessageBox.question(self, "종료", "저장하지 않은 작업이 있음. 그래도 종료할까요?\n"
                                                 "(이어서 하려면 '아니오' → 파일 → 작업 저장)")
            if r != QMessageBox.Yes:
                e.ignore()
                return
        e.accept()


HELP_TEXT = """\
【기본 순서】
1. 열기(Ctrl+O): PNG·JPG 이미지 또는 PDF. 화면에 끌어놓기, 캡처 붙여넣기(Ctrl+Shift+V)도 가능
2. ① 글자 인식(F5): 영어를 찾아 문단 단위로 묶음. 숫자만 있는 글자(그래프·표 수치)는 '보호'되어 지우지도 번역하지도 않음
3. ② 자동 번역(F6): 용어집 먼저 적용 → 무료 번역기로 문단 단위 번역
4. 수정: 오른쪽 목록에서 번역문 더블클릭 또는 아래 편집칸에서 수정
   · 글자 크기·색·굵게·정렬·배경색(스포이드) 조정, 상자를 끌어 위치·크기 조정
   · 블록 병합(Ctrl+M) / 줄 단위 분할(Ctrl+D) / 삭제(Del) / 상자 추가(A)
   · 스페이스: 원본 ↔ 결과 비교, B: 상자 숨기기
5. ③ 이미지 저장(Ctrl+S) / PDF 내보내기 / 작업 저장(나중에 이어서 수정)

【번역기가 막힐 때】
번역 메뉴 → 원문 내보내기 → 파파고·DeepL 웹에 붙여넣어 번역 → 결과 복사 → 번역문 붙여넣기

【용어집】
지명·부대명·범례 표기를 고정. 블록을 고친 뒤 '용어집에 추가'를 누르면 다음부터 자동 적용.
파일: 프로그램 폴더의 glossary.csv (엑셀로 편집 가능, UTF-8)

【글꼴】
기본 맑은 고딕. 프로그램 폴더의 fonts 폴더에 TTF/OTF(예: Pretendard)를 넣으면 목록에 추가됨.
"""
