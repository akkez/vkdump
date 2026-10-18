from __future__ import annotations

import threading
import time
from html import escape
from pathlib import Path

from PySide6.QtCore import QObject, QRunnable, QThreadPool, Qt, Signal, Slot, QUrl
from PySide6.QtGui import QDesktopServices
from PySide6.QtWidgets import QFileDialog, QHBoxLayout, QLabel, QMessageBox, QProgressBar, QVBoxLayout, QWidget

from ..core.formatting import ProgressEta, format_bytes
from ..core.progress import Cancelled
from ..provision.catalog import ApkRequired, PreparationError, download_directory, host_target, manifest
from ..provision.prepare import PreparationProgress, prepare
from .xp_widgets import XPButton


def _label(text=''):
    label = QLabel(text)
    label.setWordWrap(True)
    label.setTextFormat(Qt.TextFormat.PlainText)
    return label


def _bar():
    bar = QProgressBar()
    bar.setRange(0, 1000)
    bar.setValue(0)
    bar.setFixedHeight(21)
    bar.setStyleSheet('''
        QProgressBar { border: 1px solid #686868; border-radius: 2px;
            background: #fff; color: #000; text-align: center; font-size: 12px; }
        QProgressBar::chunk { background: #3cba3c; width: 8px; margin: 1px; }
    ''')
    return bar


class _TemporaryFolderLink(QLabel):
    def __init__(self):
        super().__init__()
        self.directory = None
        self.setTextFormat(Qt.TextFormat.RichText)
        self.setFocusPolicy(Qt.FocusPolicy.StrongFocus)
        self.linkActivated.connect(self._open)
        try:
            self.set_directory(download_directory(host_target()))
        except PreparationError:
            self.hide()

    def set_directory(self, directory):
        self.directory = directory
        url = escape(QUrl.fromLocalFile(str(directory)).toString(), quote=True)
        self.setText(f'Files are saved in a <a style="color:#0000ee" href="{url}">temporary folder</a>.')
        self.setToolTip(str(directory))
        self.setAccessibleName(f'Open temporary download folder: {directory}')

    def _open(self, _url):
        if self.directory is None:
            return
        try:
            self.directory.mkdir(parents=True, exist_ok=True, mode=0o700)
            if not QDesktopServices.openUrl(QUrl.fromLocalFile(str(self.directory))):
                raise OSError('The system could not open the folder.')
        except OSError as exc:
            QMessageBox.warning(self, 'Could not open temporary folder', str(exc))


def _progress_row(layout, label, eta):
    row = QHBoxLayout()
    row.addWidget(label, 1)
    eta.setAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
    eta.setVisible(False)
    row.addWidget(eta)
    layout.addLayout(row)


class AndroidIntroPage(QWidget):
    def __init__(self):
        super().__init__()
        layout = QVBoxLayout(self)
        layout.setContentsMargins(23, 18, 22, 18)
        layout.setSpacing(13)
        title = _label('Prepare Android for VK')
        title.setStyleSheet('font-size: 18px; font-weight: bold;')
        layout.addWidget(title)
        layout.addWidget(_label(
            'We will download an Android emulator and the VK app. '
            'You will then sign in yourself in the Android app.'))
        layout.addWidget(_label(
            'The next stage will read the app’s access token locally to download your messages. '
            'Your password is entered only in VK.'))
        layout.addWidget(_label(
            'This approach is less likely to break if VK/Mail.ru '
            '(контора солнышек) decides to do something about it.'))
        self.details = _label()
        layout.addWidget(self.details)
        self.folder_link = _TemporaryFolderLink()
        layout.addWidget(self.folder_link)
        layout.addStretch()
        layout.addWidget(_label('Click Start to download the files.'))
        try:
            target = host_target()
            config = manifest()['platforms'][target]
            total = sum(item['size'] for item in config['artifacts']) + manifest()['apk']['size']
            self.details.setText(
                f'Download: about {format_bytes(total)} for {target}. '
                'You can pause and resume the download.')
            self.supported = True
        except PreparationError as exc:
            self.details.setText(str(exc))
            self.supported = False


class _Signals(QObject):
    progress = Signal(object)
    finished = Signal(str, str, bool)


class _DownloadWorker(QRunnable):
    def __init__(self, local_apk):
        super().__init__()
        self.signals = _Signals()
        self.cancelled = threading.Event()
        self.local_apk = local_apk
        self._last_update = 0
        self._last_message = ''

    def _progress(self, event):
        now = time.monotonic()
        if (now - self._last_update > .1 or event.message != self._last_message
                or event.overall_current == event.overall_total):
            self.signals.progress.emit(event)
            self._last_update = now
            self._last_message = event.message

    @Slot()
    def run(self):
        try:
            directory = prepare(self._progress, self.cancelled, self.local_apk)
        except Cancelled:
            self.signals.finished.emit('', 'Download paused. Click Resume to continue.', False)
        except ApkRequired as exc:
            self.signals.finished.emit('', str(exc), True)
        except Exception as exc:
            self.signals.finished.emit('', str(exc), False)
        else:
            self.signals.finished.emit(str(directory), '', False)


class AndroidDownloadPage(QWidget):
    completeChanged = Signal()

    def __init__(self):
        super().__init__()
        self._worker = None
        self._local_apk = None
        self.directory = None
        self.ready = False
        self.started = False
        self._overall_estimate = ProgressEta()
        self._file_estimate = ProgressEta()
        self._transfer_name = None
        layout = QVBoxLayout(self)
        layout.setContentsMargins(23, 18, 22, 18)
        layout.setSpacing(8)
        self.title = _label('Download Android components')
        self.title.setStyleSheet('font-size: 18px; font-weight: bold;')
        layout.addWidget(self.title)
        self.overall_label = _label('Overall progress')
        self.overall_eta = _label()
        _progress_row(layout, self.overall_label, self.overall_eta)
        self.overall = _bar()
        self.overall.setAccessibleName('Overall Android download progress')
        layout.addWidget(self.overall)
        self.file_label = _label('Waiting to start…')
        self.file_label.setMinimumHeight(32)
        layout.addWidget(self.file_label)
        self.file_size = _label()
        self.file_eta = _label()
        _progress_row(layout, self.file_size, self.file_eta)
        self.file = _bar()
        self.file.setAccessibleName('Current file download progress')
        layout.addWidget(self.file)
        self.status = _label('')
        layout.addWidget(self.status)
        self.folder_link = _TemporaryFolderLink()
        layout.addWidget(self.folder_link)
        layout.addStretch()
        row = QHBoxLayout()
        row.setSpacing(8)
        self.pause_button = XPButton('Pause')
        self.pause_button.clicked.connect(self.cancel)
        row.addWidget(self.pause_button)
        self.apk_button = XPButton('Choose VK APK…')
        self.apk_button.setMinimumWidth(144)
        self.apk_button.clicked.connect(self.choose_apk)
        self.apk_button.hide()
        row.addWidget(self.apk_button)
        self.folder_button = XPButton('Open folder')
        self.folder_button.clicked.connect(self.open_folder)
        self.folder_button.hide()
        row.addWidget(self.folder_button)
        row.addStretch()
        layout.addLayout(row)

    @property
    def busy(self):
        return self._worker is not None

    def start(self):
        if self.busy or self.ready:
            return
        self.started = True
        self.title.setText('Download Android components')
        self.status.setText('Downloads can be paused and resumed.')
        self._reset_eta()
        self.folder_button.hide()
        self.apk_button.hide()
        self.pause_button.setEnabled(True)
        self.pause_button.show()
        self.file.setRange(0, 0)
        self._worker = _DownloadWorker(self._local_apk)
        self._worker.signals.progress.connect(self._progress)
        self._worker.signals.finished.connect(self._finished)
        self.completeChanged.emit()
        self.pause_button.setFocus(Qt.FocusReason.OtherFocusReason)
        QThreadPool.globalInstance().start(self._worker)

    def cancel(self):
        if self._worker:
            self._worker.cancelled.set()
            self.pause_button.setEnabled(False)
            self.status.setText('Pausing download…')
            self._reset_eta()

    def _reset_eta(self):
        self._overall_estimate.reset()
        self._file_estimate.reset()
        self._transfer_name = None
        for label in (self.overall_eta, self.file_eta):
            label.clear()
            label.hide()

    def _progress(self, event: PreparationProgress):
        self.overall.setValue(round(1000 * event.overall_current / max(1, event.overall_total)))
        self.overall_label.setText(f'Overall: {format_bytes(event.overall_current)} / {format_bytes(event.overall_total)}')
        self.file_label.setText(event.message)
        if event.file_total:
            self.file.setRange(0, 1000)
            self.file.setValue(round(1000 * event.file_current / event.file_total))
            self.file_size.setText(f'{format_bytes(event.file_current)} / {format_bytes(event.file_total)}')
        else:
            self.file.setRange(0, 0)
            self.file_size.clear()
        if event.downloading and not (self._worker and self._worker.cancelled.is_set()):
            if self._transfer_name != event.message:
                self._reset_eta()
                self._transfer_name = event.message
            for label, estimator, current, total in (
                (self.overall_eta, self._overall_estimate, event.overall_current, event.overall_total),
                (self.file_eta, self._file_estimate, event.file_current, event.file_total),
            ):
                estimate = estimator.update(current, total)
                label.setText(estimate)
                label.setVisible(bool(estimate))
        else:
            self._reset_eta()

    def _finished(self, directory, error, apk_required):
        self._worker = None
        self.pause_button.hide()
        self.file.setRange(0, 1000)
        self._reset_eta()
        self.apk_button.setVisible(apk_required)
        if directory:
            self.directory = Path(directory)
            self.ready = True
            self.title.setText('Android files downloaded')
            self.overall.setValue(1000)
            self.file.setValue(1000)
            self.status.setText(
                'All files passed checksum verification. '
                'Opening Android and signing in will be connected in the next stage.')
            self.folder_button.show()
            self.folder_button.setToolTip(directory)
            self.folder_link.set_directory(self.directory)
        else:
            self.status.setText(error)
        self.completeChanged.emit()
        if directory:
            self.folder_button.setFocus(Qt.FocusReason.OtherFocusReason)
        elif apk_required:
            self.apk_button.setFocus(Qt.FocusReason.OtherFocusReason)

    def choose_apk(self):
        pin = manifest()['apk']
        title = f'Choose VK {pin["version_name"]} / {pin["version_code"]} APK'
        filename, _ = QFileDialog.getOpenFileName(self, title, '', 'Android package (*.apk)')
        if filename:
            self._local_apk = Path(filename)
            self.start()

    def open_folder(self):
        if self.directory:
            QDesktopServices.openUrl(QUrl.fromLocalFile(str(self.directory)))
