"""이미지 보기 + 블록 상자(이동·크기조절·선택·추가) 캔버스."""
from __future__ import annotations

from typing import Dict, List, Optional

from PySide6.QtCore import Qt, QRectF, QPointF, Signal
from PySide6.QtGui import QBrush, QColor, QPainter, QPen, QPixmap, QFont
from PySide6.QtWidgets import (QGraphicsItem, QGraphicsPixmapItem, QGraphicsRectItem,
                               QGraphicsScene, QGraphicsSimpleTextItem, QGraphicsView)

from ..model import Block

COL_DONE = QColor(30, 160, 80)       # 번역됨
COL_TODO = QColor(235, 130, 0)       # 번역 전
COL_KEEP = QColor(140, 140, 140)     # 숫자 등 보호
COL_OFF = QColor(180, 60, 60)        # 적용 안 함
COL_SEL = QColor(30, 110, 230)


class BlockItem(QGraphicsRectItem):
    HANDLE = 10  # 화면 픽셀 기준 크기조절 손잡이

    def __init__(self, canvas: "ImageCanvas", block: Block, number: int):
        super().__init__()
        self.canvas = canvas
        self.block_id = block.id
        self._resizing = False
        self._press_rect = None
        self.setFlags(QGraphicsItem.ItemIsSelectable | QGraphicsItem.ItemIsMovable
                      | QGraphicsItem.ItemSendsGeometryChanges)
        self.setAcceptHoverEvents(True)
        self.label = QGraphicsSimpleTextItem(str(number), self)
        f = QFont()
        f.setPointSize(9)
        f.setBold(True)
        self.label.setFont(f)
        self.label.setFlag(QGraphicsItem.ItemIgnoresTransformations, True)
        self.update_from(block, number)

    def update_from(self, b: Block, number: Optional[int] = None):
        x0, y0, x1, y1 = b.box
        self.setPos(x0, y0)
        self.setRect(0, 0, max(2, x1 - x0), max(2, y1 - y0))
        if number is not None:
            self.label.setText(str(number))
        self.label.setPos(0, -14 / max(self.canvas.scale_factor(), 0.01))
        if b.kind == "keep":
            col, style = COL_KEEP, Qt.DotLine
        elif not b.enabled:
            col, style = COL_OFF, Qt.DashLine
        elif b.dst.strip():
            col, style = COL_DONE, Qt.SolidLine
        else:
            col, style = COL_TODO, Qt.SolidLine
        self.base_color = col
        pen = QPen(col, 2, style)
        pen.setCosmetic(True)
        self.setPen(pen)
        self.setBrush(QBrush(QColor(col.red(), col.green(), col.blue(), 28)))
        self.label.setBrush(QBrush(col))

    def paint(self, painter, option, widget=None):
        if self.isSelected():
            pen = QPen(COL_SEL, 3)
            pen.setCosmetic(True)
            painter.setPen(pen)
            painter.setBrush(QBrush(QColor(30, 110, 230, 40)))
            painter.drawRect(self.rect())
            s = self.HANDLE / max(self.canvas.scale_factor(), 0.01)
            r = self.rect()
            painter.setBrush(QBrush(COL_SEL))
            painter.drawRect(QRectF(r.right() - s, r.bottom() - s, s, s))
        else:
            super().paint(painter, option, widget)

    def _in_handle(self, pos: QPointF) -> bool:
        s = self.HANDLE * 1.5 / max(self.canvas.scale_factor(), 0.01)
        r = self.rect()
        return pos.x() > r.right() - s and pos.y() > r.bottom() - s

    def hoverMoveEvent(self, e):
        self.setCursor(Qt.SizeFDiagCursor if self._in_handle(e.pos()) else Qt.SizeAllCursor)
        super().hoverMoveEvent(e)

    def mousePressEvent(self, e):
        self._start_pos = QPointF(self.pos())
        self._press_rect = QRectF(self.rect())
        if e.button() == Qt.LeftButton and self._in_handle(e.pos()):
            self._resizing = True
            self._press_point = e.pos()
            self.setSelected(True)
            e.accept()
            return
        super().mousePressEvent(e)

    def mouseMoveEvent(self, e):
        if self._resizing:
            d = e.pos() - self._press_point
            r = self._press_rect
            self.prepareGeometryChange()
            self.setRect(0, 0, max(4, r.width() + d.x()), max(4, r.height() + d.y()))
            e.accept()
            return
        super().mouseMoveEvent(e)

    def mouseReleaseEvent(self, e):
        changed = self._resizing or (self.pos() != self._start_pos)
        self._resizing = False
        super().mouseReleaseEvent(e)
        if changed:
            # 여러 개를 함께 옮긴 경우 모두 반영
            items = [it for it in self.canvas.scene().selectedItems() if isinstance(it, BlockItem)]
            if self not in items:
                items.append(self)
            self.canvas.geometry_changed.emit({it.block_id: it.box() for it in items})

    def box(self) -> List[float]:
        p, r = self.pos(), self.rect()
        return [p.x(), p.y(), p.x() + r.width(), p.y() + r.height()]


class ImageCanvas(QGraphicsView):
    selection_changed = Signal(list)          # [block_id]
    geometry_changed = Signal(dict)           # {block_id: box}
    box_drawn = Signal(list)                  # 새 상자 [x0,y0,x1,y1]
    color_picked = Signal(float, float)       # 스포이드 좌표
    files_dropped = Signal(list)

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setScene(QGraphicsScene(self))
        self.setRenderHints(QPainter.Antialiasing | QPainter.SmoothPixmapTransform)
        self.setDragMode(QGraphicsView.RubberBandDrag)
        self.setTransformationAnchor(QGraphicsView.AnchorUnderMouse)
        self.setBackgroundBrush(QBrush(QColor(70, 72, 76)))
        self.setAcceptDrops(True)
        self.pix_item: Optional[QGraphicsPixmapItem] = None
        self.items_by_id: Dict[int, BlockItem] = {}
        self.mode = "select"          # select | add | pick
        self._draw_start = None
        self._draw_item = None
        self._mute = False
        self.show_boxes = True
        self.scene().selectionChanged.connect(self._on_sel)

    # ---------- 이미지·블록 ----------
    def scale_factor(self) -> float:
        return self.transform().m11()

    def set_pixmap(self, pm: QPixmap, keep_view=True):
        if self.pix_item is None:
            self.pix_item = self.scene().addPixmap(pm)
            self.pix_item.setZValue(-10)
            self.pix_item.setTransformationMode(Qt.SmoothTransformation)
        else:
            self.pix_item.setPixmap(pm)
        self.scene().setSceneRect(QRectF(pm.rect()).adjusted(-50, -50, 50, 50))

    def set_blocks(self, blocks: List[Block]):
        self._mute = True
        sel = {i for i, it in self.items_by_id.items() if it.isSelected()}
        for it in self.items_by_id.values():
            self.scene().removeItem(it)
        self.items_by_id = {}
        for n, b in enumerate(blocks, 1):
            it = BlockItem(self, b, n)
            it.setVisible(self.show_boxes)
            self.scene().addItem(it)
            if b.id in sel:
                it.setSelected(True)
            self.items_by_id[b.id] = it
        self._mute = False

    def update_block(self, b: Block):
        it = self.items_by_id.get(b.id)
        if it:
            it.update_from(b)
            it.update()

    def set_boxes_visible(self, v: bool):
        self.show_boxes = v
        for it in self.items_by_id.values():
            it.setVisible(v)

    def select_ids(self, ids: List[int], center=False):
        self._mute = True
        self.scene().clearSelection()
        for i in ids:
            it = self.items_by_id.get(i)
            if it:
                it.setSelected(True)
        self._mute = False
        if center and ids and ids[0] in self.items_by_id:
            self.ensureVisible(self.items_by_id[ids[0]], 80, 80)

    def selected_ids(self) -> List[int]:
        return [it.block_id for it in self.scene().selectedItems() if isinstance(it, BlockItem)]

    def _on_sel(self):
        if not self._mute:
            self.selection_changed.emit(self.selected_ids())

    # ---------- 확대/축소 ----------
    def fit(self):
        if self.pix_item:
            self.fitInView(self.pix_item, Qt.KeepAspectRatio)
            self._relabel()

    def zoom(self, f: float):
        s = self.scale_factor() * f
        if 0.03 < s < 20:
            self.scale(f, f)
            self._relabel()

    def zoom_100(self):
        self.resetTransform()
        self._relabel()

    def _relabel(self):
        for it in self.items_by_id.values():
            it.label.setPos(0, -14 / max(self.scale_factor(), 0.01))

    def wheelEvent(self, e):
        if e.modifiers() & Qt.ControlModifier:
            self.zoom(1.15 if e.angleDelta().y() > 0 else 1 / 1.15)
            e.accept()
        else:
            super().wheelEvent(e)

    # ---------- 모드 ----------
    def set_mode(self, mode: str):
        self.mode = mode
        if mode == "select":
            self.setDragMode(QGraphicsView.RubberBandDrag)
            self.viewport().setCursor(Qt.ArrowCursor)
        else:
            self.setDragMode(QGraphicsView.NoDrag)
            self.viewport().setCursor(Qt.CrossCursor)

    def mousePressEvent(self, e):
        if e.button() == Qt.MiddleButton:
            self._pan = e.position()
            return
        if self.mode == "pick" and e.button() == Qt.LeftButton:
            p = self.mapToScene(e.position().toPoint())
            self.color_picked.emit(p.x(), p.y())
            return
        if self.mode == "add" and e.button() == Qt.LeftButton:
            self._draw_start = self.mapToScene(e.position().toPoint())
            pen = QPen(COL_SEL, 2, Qt.DashLine)
            pen.setCosmetic(True)
            self._draw_item = self.scene().addRect(QRectF(self._draw_start, self._draw_start), pen)
            return
        super().mousePressEvent(e)

    def mouseMoveEvent(self, e):
        if getattr(self, "_pan", None) is not None:
            d = e.position() - self._pan
            self._pan = e.position()
            self.horizontalScrollBar().setValue(self.horizontalScrollBar().value() - int(d.x()))
            self.verticalScrollBar().setValue(self.verticalScrollBar().value() - int(d.y()))
            return
        if self._draw_item is not None:
            p = self.mapToScene(e.position().toPoint())
            self._draw_item.setRect(QRectF(self._draw_start, p).normalized())
            return
        super().mouseMoveEvent(e)

    def mouseReleaseEvent(self, e):
        if getattr(self, "_pan", None) is not None and e.button() == Qt.MiddleButton:
            self._pan = None
            return
        if self._draw_item is not None:
            r = self._draw_item.rect()
            self.scene().removeItem(self._draw_item)
            self._draw_item = None
            if r.width() > 4 and r.height() > 4:
                self.box_drawn.emit([r.left(), r.top(), r.right(), r.bottom()])
            return
        super().mouseReleaseEvent(e)

    # ---------- 끌어놓기 ----------
    def dragEnterEvent(self, e):
        if e.mimeData().hasUrls():
            e.acceptProposedAction()

    def dragMoveEvent(self, e):
        if e.mimeData().hasUrls():
            e.acceptProposedAction()

    def dropEvent(self, e):
        files = [u.toLocalFile() for u in e.mimeData().urls() if u.isLocalFile()]
        if files:
            self.files_dropped.emit(files)
