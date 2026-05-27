import signal
import sys

from PySide6.QtGui import QIcon
from PySide6.QtWidgets import QApplication

from ..core.db import apply_migrations, resource_dir
from .main_window import MainWindow


def main() -> int:
    # Ctrl+C while a QPropertyAnimation is mid-tick lands inside a
    # Python property setter and triggers a libpyside "metacall
    # WriteProperty" traceback. SIG_DFL terminates immediately before
    # the next tick re-enters Python.
    signal.signal(signal.SIGINT, signal.SIG_DFL)

    apply_migrations()
    qt_app = QApplication(sys.argv)
    icon_path = resource_dir() / "assets" / "icon.png"
    if icon_path.is_file():
        qt_app.setWindowIcon(QIcon(str(icon_path)))
    window = MainWindow()
    window.show()
    return qt_app.exec()


if __name__ == "__main__":
    sys.exit(main())
