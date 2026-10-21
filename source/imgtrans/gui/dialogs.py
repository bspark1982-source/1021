"""보조 대화상자: 용어집 편집, 수작업 번역(내보내기/가져오기), PDF 내보내기."""
from __future__ import annotations

import csv

from PySide6.QtGui import QGuiApplication
from PySide6.QtWidgets import (QAbstractItemView, QComboBox, QDialog, QDialogButtonBox,
                               QFileDialog, QFormLayout, QHBoxLayout, QHeaderView, QLabel,
                               QLineEdit, QMessageBox, QPlainTextEdit, QPushButton,
                               QTableWidget, QTableWidgetItem, QVBoxLayout)

from ..glossary import Glossary


class GlossaryDialog(QDialog):
    def __init__(self, glossary: Glossary, parent=None):
        super().__init__(parent)
        self.g = glossary
        self.setWindowTitle("용어집 편집")
        self.resize(720, 560)
        lay = QVBoxLayout(self)
        tip = QLabel("원문 전체가 용어와 같으면(지명·범례 등) 그대로 한글로 바꾸고, 문장 속 용어는 번역 전에 "
                     "한글로 치환해 표기를 고정함. 대소문자가 같은 항목을 먼저 찾고, 긴 용어가 먼저 적용됨.\n"
                     "한글 칸에 = 를 넣으면 번역하지 않고 원본 그대로 둠(ISW 로고 글자 등).")
        tip.setWordWrap(True)
        lay.addWidget(tip)

        top = QHBoxLayout()
        self.search = QLineEdit()
        self.search.setPlaceholderText("검색 (영어 또는 한글)")
        self.search.textChanged.connect(self._filter)
        top.addWidget(self.search)
        lay.addLayout(top)

        self.table = QTableWidget(0, 2)
        self.table.setHorizontalHeaderLabels(["영어 (원문)", "한글 (고정 번역)"])
        self.table.horizontalHeader().setSectionResizeMode(QHeaderView.Stretch)
        self.table.setSelectionBehavior(QAbstractItemView.SelectRows)
        lay.addWidget(self.table)
        for en, ko in self.g.items:
            self._add_row(en, ko)

        row = QHBoxLayout()
        for text, fn in (("행 추가", lambda: self._add_row("", "", focus=True)),
                         ("선택 행 삭제", self._del_rows),
                         ("CSV 가져오기(추가)", self._import),
                         ("CSV 내보내기", self._export)):
            b = QPushButton(text)
            b.clicked.connect(fn)
            row.addWidget(b)
        row.addStretch()
        lay.addLayout(row)

        bb = QDialogButtonBox(QDialogButtonBox.Save | QDialogButtonBox.Cancel)
        bb.button(QDialogButtonBox.Save).setText("저장")
        bb.button(QDialogButtonBox.Cancel).setText("취소")
        bb.accepted.connect(self._save)
        bb.rejected.connect(self.reject)
        lay.addWidget(bb)

    def _add_row(self, en, ko, focus=False):
        r = self.table.rowCount()
        self.table.insertRow(r)
        self.table.setItem(r, 0, QTableWidgetItem(en))
        self.table.setItem(r, 1, QTableWidgetItem(ko))
        if focus:
            self.table.scrollToBottom()
            self.table.editItem(self.table.item(r, 0))

    def _del_rows(self):
        rows = sorted({i.row() for i in self.table.selectedIndexes()}, reverse=True)
        for r in rows:
            self.table.removeRow(r)

    def _filter(self, t):
        t = t.strip().lower()
        for r in range(self.table.rowCount()):
            a = (self.table.item(r, 0).text() if self.table.item(r, 0) else "").lower()
            b = (self.table.item(r, 1).text() if self.table.item(r, 1) else "").lower()
            self.table.setRowHidden(r, bool(t) and t not in a and t not in b)

    def _rows(self):
        out = []
        for r in range(self.table.rowCount()):
            en = self.table.item(r, 0).text().strip() if self.table.item(r, 0) else ""
            ko = self.table.item(r, 1).text().strip() if self.table.item(r, 1) else ""
            if en and ko:
                out.append((en, ko))
        return out

    def _import(self):
        p, _ = QFileDialog.getOpenFileName(self, "용어집 CSV 가져오기", "", "CSV (*.csv)")
        if not p:
            return
        existing = {en.lower() for en, _ in self._rows()}
        n = 0
        for enc in ("utf-8-sig", "cp949"):
            try:
                with open(p, newline="", encoding=enc) as f:
                    rows = list(csv.reader(f))
                break
            except UnicodeDecodeError:
                continue
        else:
            QMessageBox.warning(self, "가져오기", "파일 인코딩을 읽을 수 없음 (UTF-8 또는 CP949 필요)")
            return
        for r in rows:
            if len(r) >= 2 and r[0].strip() and r[0].strip().lower() not in ("en", "english", "영어") \
                    and r[0].strip().lower() not in existing:
                self._add_row(r[0].strip(), r[1].strip())
                n += 1
        QMessageBox.information(self, "가져오기", f"{n}개 항목을 추가함 (중복 제외). 저장을 눌러야 반영됨.")

    def _export(self):
        p, _ = QFileDialog.getSaveFileName(self, "용어집 CSV 내보내기", "glossary.csv", "CSV (*.csv)")
        if not p:
            return
        with open(p, "w", newline="", encoding="utf-8-sig") as f:
            w = csv.writer(f)
            w.writerow(["en", "ko"])
            w.writerows(self._rows())

    def _save(self):
        self.g.items = self._rows()
        self.g.save()
        self.accept()


class ExportTextDialog(QDialog):
    """번호 붙인 원문 → 파파고/DeepL/구글 웹에 붙여넣어 번역."""

    def __init__(self, text: str, parent=None):
        super().__init__(parent)
        self.setWindowTitle("원문 내보내기 (수작업 번역용)")
        self.resize(760, 560)
        lay = QVBoxLayout(self)
        lab = QLabel("아래 내용을 복사해 파파고·DeepL·구글 번역 웹사이트에 붙여넣고, 번역 결과를 통째로 복사한 뒤 "
                     "[번역 → 번역문 붙여넣기]로 가져오면 됨. [번호]는 지우지 말 것.")
        lab.setWordWrap(True)
        lay.addWidget(lab)
        self.edit = QPlainTextEdit(text)
        self.edit.setReadOnly(True)
        lay.addWidget(self.edit)
        row = QHBoxLayout()
        b = QPushButton("전체 복사")
        b.clicked.connect(lambda: (QGuiApplication.clipboard().setText(text), b.setText("복사됨 ✓")))
        row.addWidget(b)
        row.addStretch()
        c = QPushButton("닫기")
        c.clicked.connect(self.accept)
        row.addWidget(c)
        lay.addLayout(row)


class ImportTextDialog(QDialog):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.setWindowTitle("번역문 붙여넣기")
        self.resize(760, 560)
        lay = QVBoxLayout(self)
        lab = QLabel("[1] 번역문\n[2] 번역문 … 형식의 번역 결과를 붙여넣기. 번호가 같은 블록에 들어감.")
        lay.addWidget(lab)
        self.edit = QPlainTextEdit()
        lay.addWidget(self.edit)
        row = QHBoxLayout()
        p = QPushButton("클립보드에서 붙여넣기")
        p.clicked.connect(lambda: self.edit.setPlainText(QGuiApplication.clipboard().text()))
        row.addWidget(p)
        row.addStretch()
        bb = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
        bb.button(QDialogButtonBox.Ok).setText("적용")
        bb.button(QDialogButtonBox.Cancel).setText("취소")
        bb.accepted.connect(self.accept)
        bb.rejected.connect(self.reject)
        row.addWidget(bb)
        lay.addLayout(row)

    def text(self) -> str:
        return self.edit.toPlainText()


class PdfExportDialog(QDialog):
    def __init__(self, translated_pages, total, parent=None):
        super().__init__(parent)
        self.setWindowTitle("번역 PDF 내보내기")
        lay = QFormLayout(self)
        pages_txt = ", ".join(str(i + 1) for i in translated_pages) or "(없음)"
        lay.addRow("작업한 페이지:", QLabel(pages_txt))
        self.mode = QComboBox()
        self.mode.addItem("원본 페이지를 번역 페이지로 교체", "replace")
        self.mode.addItem("원본 페이지 뒤에 번역 페이지 삽입 (대조용)", "after")
        self.mode.addItem("번역한 페이지만 모아서 저장", "only")
        lay.addRow("방식:", self.mode)
        note = QLabel(f"전체 {total}쪽 중 작업한 페이지만 이미지로 바뀜. 나머지는 원본 그대로 유지.")
        note.setWordWrap(True)
        lay.addRow(note)
        bb = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
        bb.button(QDialogButtonBox.Ok).setText("저장 위치 선택…")
        bb.button(QDialogButtonBox.Cancel).setText("취소")
        bb.accepted.connect(self.accept)
        bb.rejected.connect(self.reject)
        lay.addRow(bb)

    def placement(self) -> str:
        return self.mode.currentData()


def parse_pages(text: str, n: int):
    """'1-3,5' → [0,1,2,4]"""
    out = set()
    for part in text.replace(" ", "").split(","):
        if not part:
            continue
        if "-" in part:
            a, b = part.split("-", 1)
            a = int(a) if a else 1
            b = int(b) if b else n
            out.update(range(max(1, a), min(n, b) + 1))
        else:
            v = int(part)
            if 1 <= v <= n:
                out.add(v)
    return sorted(i - 1 for i in out)
