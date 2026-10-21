"""이미지·PDF 한글화 편집기 실행 파일."""
import os
import sys
import traceback
import datetime

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
os.makedirs(os.path.join(HERE, "fonts"), exist_ok=True)


def _excepthook(etype, value, tb):
    msg = "".join(traceback.format_exception(etype, value, tb))
    try:
        with open(os.path.join(HERE, "error_log.txt"), "a", encoding="utf-8") as f:
            f.write(f"\n===== {datetime.datetime.now()} =====\n{msg}")
    except Exception:
        pass
    try:
        from PySide6.QtWidgets import QMessageBox
        QMessageBox.critical(None, "오류", "예상치 못한 오류가 발생함. 작업은 계속할 수 있음.\n"
                             "자세한 내용은 error_log.txt 참고.\n\n" + msg[-1500:])
    except Exception:
        print(msg)


def main():
    from PySide6.QtGui import QFont
    from PySide6.QtWidgets import QApplication
    from imgtrans.gui.main_window import MainWindow

    sys.excepthook = _excepthook
    app = QApplication(sys.argv)
    app.setApplicationName("ImageKoTranslator")
    app.setStyle("Fusion")
    f = QFont("Malgun Gothic")
    f.setPointSize(9)
    app.setFont(f)
    w = MainWindow()
    w.show()
    if len(sys.argv) > 1 and os.path.exists(sys.argv[1]):
        w.open_path(sys.argv[1])
    sys.exit(app.exec())


if __name__ == "__main__":
    main()
