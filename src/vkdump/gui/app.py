import signal
import sys
from PySide6.QtWidgets import QApplication

from ..core.db import apply_migrations
from .main_window import MainWindow


def main() -> int:
    # Ctrl+C while a QPropertyAnimation is mid-tick lands inside a
    # Python property setter and triggers a libpyside "metacall
    # WriteProperty" traceback. SIG_DFL terminates immediately before
    # the next tick re-enters Python.
    signal.signal(signal.SIGINT, signal.SIG_DFL)

    apply_migrations()
    qt_app = QApplication(sys.argv)
    window = MainWindow()
    window.show()
    return qt_app.exec()


if __name__ == "__main__":
    sys.exit(main())
