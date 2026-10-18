from __future__ import annotations

from PySide6.QtCore import Qt, Signal, QUrl
from PySide6.QtGui import QDesktopServices, QKeySequence, QPixmap, QShortcut
from PySide6.QtWidgets import (
    QFormLayout, QFrame, QHBoxLayout, QLabel, QRadioButton, QSpinBox,
    QStackedWidget, QVBoxLayout, QWidget, QWizard,
)

from ..core import app_config
from ..core.db import connection, resource_dir
from .android_setup import AndroidIntroPage, AndroidDownloadPage
from .task_panel import TaskPanel
from .xp_widgets import XPButton, XPCaption, XPFrame, XPRadioButton


ARCHIVE_URL = 'https://vk.ru/data_protection?section=rules#archive'
WELCOME, SOURCE, IMPORT, DOWNLOAD, ANDROID_INTRO, ANDROID_DOWNLOAD = range(6)
WIZARD_WIDTH, WIZARD_HEIGHT = 640, 463


def _icon(name: str, size: int = 48) -> QPixmap:
    path = resource_dir() / 'assets' / 'wizard' / name
    return QPixmap(str(path)).scaled(size, size, Qt.AspectRatioMode.KeepAspectRatio,
                                    Qt.TransformationMode.FastTransformation)


def _app_icon(size: int = 16) -> QPixmap:
    return QPixmap(str(resource_dir() / 'assets' / 'icon.png')).scaled(
        size, size, Qt.AspectRatioMode.KeepAspectRatio, Qt.TransformationMode.SmoothTransformation)


def _text(text: str) -> QLabel:
    label = QLabel(text)
    label.setWordWrap(True)
    label.setTextFormat(Qt.TextFormat.PlainText)
    return label


def _page(title: str) -> tuple[QWidget, QVBoxLayout]:
    page = QWidget()
    layout = QVBoxLayout(page)
    layout.setContentsMargins(23, 18, 22, 18)
    layout.setSpacing(0)
    heading = _text(title)
    heading.setStyleSheet('font-size: 18px; font-weight: bold;')
    layout.addWidget(heading)
    layout.addSpacing(32)
    return page, layout


class _RadioDescription(QLabel):
    def __init__(self, text: str, radio: QRadioButton):
        super().__init__(text)
        self._radio = radio
        self.setWordWrap(True)
        self.setTextFormat(Qt.TextFormat.PlainText)

    def mouseReleaseEvent(self, event):
        if event.button() == Qt.MouseButton.LeftButton and self.rect().contains(event.position().toPoint()):
            self._radio.setFocus(Qt.FocusReason.MouseFocusReason)
            self._radio.click()
            event.accept()
        else:
            super().mouseReleaseEvent(event)


class _SourcePage(QWidget):
    completeChanged = Signal()

    def __init__(self):
        super().__init__()
        self.authorized = False
        self.archive = XPRadioButton('I already have a VK archive')
        self.account = XPRadioButton("I don't have a VK archive")
        layout = QVBoxLayout(self)
        layout.setContentsMargins(23, 18, 22, 18)
        layout.setSpacing(0)
        title = _text('Select a message source')
        title.setStyleSheet('font-size: 18px; font-weight: bold;')
        layout.addWidget(title)
        layout.addSpacing(30)
        layout.addWidget(_text('Choose how you want to get your VK messages.'))
        layout.addSpacing(26)
        for radio, description in (
            (self.archive, 'Import a ZIP file or an extracted archive from VK.'),
            (self.account, 'Authorize with VK and download all your conversations and messages.'),
        ):
            layout.addWidget(radio)
            layout.addSpacing(7)
            radio.setAccessibleDescription(description)
            copy = _RadioDescription(description, radio)
            copy.setContentsMargins(22, 0, 0, 0)
            layout.addWidget(copy)
            layout.addSpacing(24)
            radio.toggled.connect(self.completeChanged)
        layout.addStretch(1)
        archive_link = QLabel(f"Don't have a copy? <a style='color:#0000ee' href='{ARCHIVE_URL}'>Request a VK archive</a>")
        self.archive_link = archive_link
        archive_link.setFocusPolicy(Qt.FocusPolicy.StrongFocus)
        archive_link.setOpenExternalLinks(True)
        layout.addWidget(archive_link)
        layout.addSpacing(7)
        layout.addWidget(_text('We recommend requesting an archive from VK anyway.'))
        layout.addStretch(1)
        layout.addWidget(_text('To continue, click Next.'))

    def isComplete(self) -> bool:
        return self.archive.isChecked() or self.account.isChecked()

    def nextId(self) -> int:
        if self.archive.isChecked():
            return IMPORT
        return DOWNLOAD if self.authorized else ANDROID_INTRO


class _FinalPage(QWidget):
    completeChanged = Signal()

    def __init__(self):
        super().__init__()
        self.ready = True

    def isComplete(self) -> bool:
        return self.ready


class _StartWizard(QWidget):
    currentIdChanged = Signal(int)

    def __init__(self, owner: 'StartView'):
        super().__init__(owner)
        self._owner = owner
        self._history = [WELCOME]
        self._pages = QStackedWidget()
        self._pages.setFrameShape(QFrame.Shape.NoFrame)
        self._buttons = {}
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)
        content = QWidget()
        content.setFixedHeight(368)
        content.setStyleSheet('background: white;')
        row = QHBoxLayout(content)
        row.setContentsMargins(0, 0, 0, 0)
        row.setSpacing(0)
        sidebar = QLabel()
        sidebar.setFixedSize(164, 368)
        sidebar.setPixmap(QPixmap(str(resource_dir() / 'assets' / 'wizard' / 'internet-sidebar.png')))
        row.addWidget(sidebar)
        row.addWidget(self._pages, 1)
        layout.addWidget(content, 1)
        self._fox = QLabel(content)
        self._fox.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents)
        self._fox.setStyleSheet('background: transparent;')
        fox = QPixmap(str(resource_dir() / 'assets' / 'wizard' / 'lis420.png'))
        fox = fox.scaled(
            154, 154, Qt.AspectRatioMode.KeepAspectRatio, Qt.TransformationMode.SmoothTransformation)
        self._fox.setPixmap(fox)
        self._fox.setGeometry(57, 214, 154, 154)
        self._fox.hide()
        footer = QWidget()
        footer.setObjectName('wizardFooter')
        footer.setFixedHeight(59)
        footer.setStyleSheet('#wizardFooter { background: #ece9d8; border-top: 1px solid #aca899; }')
        buttons = QHBoxLayout(footer)
        buttons.setContentsMargins(15, 13, 14, 14)
        buttons.setSpacing(8)
        buttons.addStretch()
        for kind, text, action in (
            (QWizard.WizardButton.BackButton, '< &Back', self.back),
            (QWizard.WizardButton.NextButton, '&Next >', self.next),
            (QWizard.WizardButton.FinishButton, '&Import >', self.accept),
            (QWizard.WizardButton.CancelButton, 'Cancel', self.reject),
        ):
            button = XPButton(text)
            button.setFixedWidth(94)
            button.clicked.connect(action)
            buttons.addWidget(button)
            self._buttons[kind] = button
        layout.addWidget(footer)
        self._return_shortcuts = []
        for key in ('Return', 'Enter'):
            shortcut = QShortcut(QKeySequence(key), self)
            shortcut.activated.connect(self._advance)
            self._return_shortcuts.append(shortcut)
        self._escape = QShortcut(QKeySequence('Escape'), self)
        self._escape.activated.connect(self.reject)

    def setPage(self, page_id: int, page: QWidget) -> None:
        assert self._pages.addWidget(page) == page_id
        if hasattr(page, 'completeChanged'):
            page.completeChanged.connect(self._sync)

    def button(self, kind: QWizard.WizardButton) -> XPButton:
        return self._buttons[kind]

    def currentId(self) -> int:
        return self._history[-1]

    def restart(self) -> None:
        self._history = [WELCOME]
        self._show_page()

    def next(self) -> None:
        current = self.currentId()
        if current == WELCOME:
            self._history.append(SOURCE)
        elif current == SOURCE and self._owner._source_page.isComplete():
            self._history.append(self._owner._source_page.nextId())
        elif current == ANDROID_INTRO and self._owner._android_intro.supported:
            self._history.append(ANDROID_DOWNLOAD)
        elif current == ANDROID_DOWNLOAD:
            if not self._owner._android_download.busy:
                self._owner._android_download.start()
            return
        else:
            return
        self._show_page()
        if self.currentId() == ANDROID_DOWNLOAD and not self._owner._android_download.started:
            self._owner._android_download.start()

    def back(self) -> None:
        if self._owner._android_download.busy:
            return
        if len(self._history) > 1:
            self._history.pop()
            self._show_page()

    def _show_page(self) -> None:
        self._pages.setCurrentIndex(self.currentId())
        self._fox.setVisible(self.currentId() != WELCOME)
        self._fox.raise_()
        self._sync()
        self.currentIdChanged.emit(self.currentId())
        if self.currentId() == SOURCE:
            page = self._owner._source_page
            target = page.account if page.account.isChecked() else page.archive
        elif self.currentId() == DOWNLOAD:
            target = self._owner._auth_btn
        elif self.currentId() == ANDROID_DOWNLOAD and self._owner._android_download.busy:
            target = self._owner._android_download.pause_button
        else:
            target = self.button(QWizard.WizardButton.FinishButton if self.currentId() == IMPORT
                                 else QWizard.WizardButton.NextButton)
        target.setFocus(Qt.FocusReason.OtherFocusReason)

    def focusNextPrevChild(self, next: bool) -> bool:
        controls = []
        if self.currentId() == SOURCE:
            page = self._owner._source_page
            controls.extend((page.archive, page.account, page.archive_link))
        elif self.currentId() == DOWNLOAD:
            controls.append(self._owner._auth_btn)
        elif self.currentId() == ANDROID_DOWNLOAD:
            page = self._owner._android_download
            controls.extend((page.folder_link, page.pause_button, page.apk_button, page.folder_button))
        elif self.currentId() == ANDROID_INTRO:
            controls.append(self._owner._android_intro.folder_link)
        controls.extend(self._buttons.values())
        controls = [control for control in controls if control.isVisible() and control.isEnabled()]
        if not controls:
            return False
        focused = self.window().focusWidget()
        if focused in controls:
            index = (controls.index(focused) + (1 if next else -1)) % len(controls)
        else:
            index = 0 if next else -1
        controls[index].setFocus(Qt.FocusReason.TabFocusReason if next else Qt.FocusReason.BacktabFocusReason)
        return True

    def _sync(self) -> None:
        current = self.currentId()
        android = self._owner._android_download
        final = current in (IMPORT, DOWNLOAD) or (current == ANDROID_DOWNLOAD and android.ready)
        back = self.button(QWizard.WizardButton.BackButton)
        next_button = self.button(QWizard.WizardButton.NextButton)
        finish = self.button(QWizard.WizardButton.FinishButton)
        back.setEnabled(current != WELCOME and not android.busy)
        next_button.setVisible(not final)
        next_button.setText('&Start >' if current == ANDROID_INTRO else
                            '&Resume >' if current == ANDROID_DOWNLOAD else '&Next >')
        next_button.setEnabled(
            self._owner._source_page.isComplete() if current == SOURCE else
            self._owner._android_intro.supported if current == ANDROID_INTRO else
            not android.busy if current == ANDROID_DOWNLOAD else True)
        next_button.setDefault(not final)
        finish.setVisible(final)
        finish.setText('&Import >' if current == IMPORT else '&Done' if current == ANDROID_DOWNLOAD else '&Download >')
        finish.setEnabled(current == IMPORT or (current == ANDROID_DOWNLOAD and android.ready)
                          or (current == DOWNLOAD and self._owner._download_page.isComplete()))
        finish.setDefault(final)
        for button in self._buttons.values():
            button.update()

    def _advance(self) -> None:
        if self.currentId() == SOURCE and self._owner._source_page.archive_link.hasFocus():
            QDesktopServices.openUrl(QUrl(ARCHIVE_URL))
            return
        if self.currentId() == ANDROID_DOWNLOAD:
            page = self._owner._android_download
            if page.folder_link.hasFocus():
                page.folder_link._open('')
                return
            for control in (page.pause_button, page.apk_button, page.folder_button):
                if control.hasFocus() and control.isVisible() and control.isEnabled():
                    control.click()
                    return
        if self.currentId() == ANDROID_INTRO and self._owner._android_intro.folder_link.hasFocus():
            self._owner._android_intro.folder_link._open('')
            return
        final = self.currentId() in (IMPORT, DOWNLOAD) or (
            self.currentId() == ANDROID_DOWNLOAD and self._owner._android_download.ready)
        kind = QWizard.WizardButton.FinishButton if final else QWizard.WizardButton.NextButton
        button = self.button(kind)
        if button.isEnabled():
            button.click()

    def accept(self) -> None:
        if self.currentId() == IMPORT:
            self._owner.import_requested.emit()
        elif self.currentId() == DOWNLOAD and self._owner._download_page.isComplete():
            self._owner.download_requested.emit()
        elif self.currentId() == ANDROID_DOWNLOAD and self._owner._android_download.ready:
            self.back()

    def reject(self) -> None:
        self._owner.exit_requested.emit()


class StartView(XPFrame):
    import_requested = Signal()
    download_requested = Signal()
    account_changed = Signal()
    exit_requested = Signal()
    minimize_requested = Signal()

    def __init__(self, download_panel: TaskPanel, parent: QWidget | None = None):
        super().__init__(parent)
        self._download_panel = download_panel
        self._wizard = _StartWizard(self)
        self._wizard.setObjectName('startWizard')
        welcome, welcome_layout = _page('Welcome to the VK Dump Wizard')
        welcome_layout.addWidget(_text('This wizard will help you save your VK messages.'))
        welcome_layout.addSpacing(22)
        welcome_layout.addWidget(_text('You can use this wizard to:'))
        welcome_layout.addSpacing(8)
        welcome_layout.addWidget(_text('•  Import an existing VK archive\n•  Download conversations from your account\n•  Save their complete message histories'))
        welcome_layout.addStretch()
        welcome_layout.addWidget(_text('To continue, click Next.'))
        self._wizard.setPage(WELCOME, welcome)
        self._source_page = _SourcePage()
        self._wizard.setPage(SOURCE, self._source_page)

        archive_page = _FinalPage()
        archive_layout = QVBoxLayout(archive_page)
        archive_layout.setContentsMargins(23, 18, 22, 18)
        archive_layout.setSpacing(0)
        title = _text('Import your VK archive')
        title.setStyleSheet('font-size: 18px; font-weight: bold;')
        archive_layout.addWidget(title)
        archive_layout.addSpacing(32)
        archive_layout.addWidget(_text('You can import the ZIP file downloaded from VK or the folder where you extracted it.'))
        archive_layout.addStretch()
        archive_layout.addWidget(_text('To choose your archive, click Import.'))
        self._wizard.setPage(IMPORT, archive_page)

        self._download_page = _FinalPage()
        download_layout = QVBoxLayout(self._download_page)
        download_layout.setContentsMargins(23, 18, 22, 18)
        download_layout.setSpacing(0)
        title = _text('Authorize your VK account')
        title.setStyleSheet('font-size: 18px; font-weight: bold;')
        download_layout.addWidget(title)
        download_layout.addSpacing(26)
        self._auth_btn = XPButton('Click to authorize through VK')
        self._auth_btn.setMinimumWidth(192)
        self._auth_btn.clicked.connect(self._authorize)
        download_layout.addWidget(self._auth_btn, 0, Qt.AlignmentFlag.AlignLeft)
        download_layout.addSpacing(12)
        self._account_label = _text('')
        download_layout.addWidget(self._account_label)
        download_layout.addSpacing(22)
        self._options = QWidget()
        form = QFormLayout(self._options)
        form.setContentsMargins(0, 0, 0, 0)
        form.setHorizontalSpacing(12)
        form.setVerticalSpacing(10)
        form.setRowWrapPolicy(QFormLayout.RowWrapPolicy.DontWrapRows)
        form.setFieldGrowthPolicy(QFormLayout.FieldGrowthPolicy.AllNonFixedFieldsGrow)
        self._threads = QSpinBox()
        self._threads.setRange(1, 2_000_000_000)
        self._calls = QSpinBox()
        self._calls.setRange(1, 25)
        self._threads.setValue(download_panel._editors['threads'].value())
        self._calls.setValue(download_panel._editors['per_request'].value())
        form.addRow('Parallel threads:', self._threads)
        form.addRow('Calls per execute (1..25):', self._calls)
        for control, name in ((self._threads, 'threads'), (self._calls, 'per_request')):
            editor = download_panel._editors[name]
            control.valueChanged.connect(editor.setValue)
            editor.valueChanged.connect(control.setValue)
        download_layout.addWidget(self._options)
        self._options.hide()
        download_layout.addStretch()
        download_layout.addWidget(_text('To save your conversations and messages, click Download.'))
        self._wizard.setPage(DOWNLOAD, self._download_page)
        self._android_intro = AndroidIntroPage()
        self._wizard.setPage(ANDROID_INTRO, self._android_intro)
        self._android_download = AndroidDownloadPage()
        self._wizard.setPage(ANDROID_DOWNLOAD, self._android_download)
        self._wizard.currentIdChanged.connect(lambda _: self.refresh())
        self._wizard.restart()
        self._import_btn = self._wizard.button(QWizard.WizardButton.FinishButton)
        self._download_btn = self._import_btn

        self.caption = XPCaption('VK Dump Wizard', None)
        self.caption.close_requested.connect(self.exit_requested)
        self.caption.minimize_requested.connect(self.minimize_requested)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(3, 0, 3, 3)
        layout.setSpacing(0)
        layout.addWidget(self.caption)
        layout.addWidget(self._wizard, 1)
        self.refresh()

    def reset(self) -> None:
        for radio in (self._source_page.archive, self._source_page.account):
            radio.setAutoExclusive(False)
            radio.setChecked(False)
            radio.setAutoExclusive(True)
        self._wizard.restart()

    def refresh(self):
        active = app_config.get('active_account_id') or ''
        with connection() as conn:
            row = conn.execute(
                "SELECT vk_id, display_name, vk_access_token IS NOT NULL "
                "AND vk_access_token != '' AS authorized FROM accounts "
                "WHERE provider='vk' AND vk_id=?", (active,),
            ).fetchone()
        authorized = bool(row and row['authorized'])
        self._source_page.authorized = authorized
        self._account_label.setText(
            f"Authorized as {row['display_name'] or 'VK user'} (id{row['vk_id']})"
            if authorized else 'Authorize to download your conversations and messages.'
        )
        self._auth_btn.setText('Reauthorize through VK' if authorized else 'Click to authorize through VK')
        self._options.setEnabled(authorized)
        self._download_page.ready = authorized and not self._download_panel.has_active_run()
        self._download_page.completeChanged.emit()

    def _authorize(self):
        self._wizard._history.append(ANDROID_INTRO)
        self._wizard._show_page()
