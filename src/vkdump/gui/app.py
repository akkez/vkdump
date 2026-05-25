import sys
from PySide6.QtWidgets import QApplication

from ..core.db import apply_migrations
from .main_window import MainWindow


def main() -> int:
    apply_migrations()
    qt_app = QApplication(sys.argv)
    window = MainWindow()
    window.show()
    return qt_app.exec()


if __name__ == "__main__":
    sys.exit(main())
