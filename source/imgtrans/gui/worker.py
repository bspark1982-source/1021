"""오래 걸리는 작업(OCR·번역·내보내기)을 화면이 멈추지 않게 백그라운드에서 실행."""
from __future__ import annotations

import traceback

from PySide6.QtCore import QThread, Signal


class Worker(QThread):
    done = Signal(object)
    failed = Signal(str)
    progress = Signal(int, int, str)

    def __init__(self, fn, *args, **kwargs):
        super().__init__()
        self.fn, self.args, self.kwargs = fn, args, kwargs

    def report(self, i: int, n: int, msg: str = ""):
        self.progress.emit(i, n, msg)

    def run(self):
        try:
            self.done.emit(self.fn(self, *self.args, **self.kwargs))
        except Exception as e:  # noqa
            self.failed.emit(f"{e}\n\n{traceback.format_exc()}")
