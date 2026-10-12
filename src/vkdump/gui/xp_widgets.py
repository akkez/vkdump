from __future__ import annotations

import sys

from PySide6.QtCore import QPoint, QRect, QRectF, Qt, Signal
from PySide6.QtGui import QColor, QFont, QGuiApplication, QLinearGradient, QPainter, QPen, QRegion
from PySide6.QtSvg import QSvgRenderer
from PySide6.QtWidgets import (
    QDialog, QFrame, QHBoxLayout, QLabel, QPushButton, QStyle, QVBoxLayout, QWidget,
)

from ..core.db import resource_dir


XP_FONT = QFont('Tahoma')
XP_FONT.setFamilies(['Tahoma', 'Arial'])
XP_FONT.setPixelSize(13)


class XPButton(QPushButton):
    def __init__(self, text: str, parent: QWidget | None = None):
        super().__init__(text, parent)
        self.setFont(XP_FONT)
        self.setFixedHeight(29)
        self.setMinimumWidth(94)
        self.setAttribute(Qt.WidgetAttribute.WA_Hover)
        self.setAttribute(Qt.WidgetAttribute.WA_MacShowFocusRect, False)

    def paintEvent(self, event):
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing, False)
        rect = QRectF(self.rect()).adjusted(.5, .5, -.5, -.5)
        enabled = self.isEnabled()
        gradient = QLinearGradient(0, 0, 0, self.height())
        if not enabled:
            gradient.setColorAt(0, QColor('#f5f4ea'))
            gradient.setColorAt(1, QColor('#ece9d8'))
        elif self.isDown():
            for stop, color in ((0, '#cdcac3'), (.08, '#e3e3db'), (.94, '#e5e5de'), (1, '#f2f2f1')):
                gradient.setColorAt(stop, QColor(color))
        else:
            gradient.setColorAt(0, QColor('#ffffff'))
            gradient.setColorAt(.86, QColor('#ecebe5'))
            gradient.setColorAt(1, QColor('#d8d0c4'))
        painter.setBrush(gradient)
        painter.setPen(QColor('#c9c7ba' if not enabled else '#003c74'))
        painter.drawRoundedRect(rect, 3, 3)
        inner = rect.adjusted(1.5, 1.5, -1.5, -1.5)
        accent = QLinearGradient(0, 0, 0, self.height())
        colors = ('#e8e4d6', '#e8e4d6', '#e8e4d6') if self.isDown() else ('#fff', '#fff', '#e6dfcf')
        width = 1
        if enabled and not self.isDown():
            if self.underMouse():
                colors = ('#fff0cf', '#fbc761', '#e5a01a')
                width = 2
            elif self.isDefault() or self.hasFocus():
                colors = ('#cee7ff', '#b0c9e9', '#6982ee')
                width = 2
        for stop, color in zip((0, .5, 1), colors):
            accent.setColorAt(stop, QColor(color))
        painter.setPen(QPen(accent, width))
        painter.setBrush(Qt.BrushStyle.NoBrush)
        painter.drawRoundedRect(inner, 2, 2)
        painter.setFont(self.font())
        painter.setPen(QColor('#aca899' if not enabled else '#000000'))
        text_rect = self.rect().adjusted(6, 0, -6, 0)
        if self.isDown():
            text_rect.translate(1, 1)
        painter.drawText(text_rect, Qt.AlignmentFlag.AlignCenter | Qt.TextFlag.TextHideMnemonic, self.text())
        if enabled and self.hasFocus():
            painter.setRenderHint(QPainter.RenderHint.Antialiasing, False)
            painter.setPen(QPen(QColor('#000000'), 1, Qt.PenStyle.DotLine))
            painter.drawRect(self.rect().adjusted(5, 5, -6, -6))


class _CaptionButton(QPushButton):
    def __init__(self, kind: str):
        super().__init__()
        self.kind = kind
        self.setAccessibleName(kind.title())
        self.setToolTip(kind.title())
        self.setFixedSize(26, 26)
        self.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        self.setAttribute(Qt.WidgetAttribute.WA_Hover)
        if kind == 'maximize':
            self.setEnabled(False)
        root = resource_dir() / 'assets' / 'wizard' / 'xp'
        self._renderers = {
            state: QSvgRenderer(str(root / f'{kind}{suffix}.svg'), self)
            for state, suffix in (('normal', ''), ('hover', '-hover'), ('pressed', '-active'))
        }

    def paintEvent(self, event):
        painter = QPainter(self)
        state = 'pressed' if self.isDown() else 'hover' if self.underMouse() else 'normal'
        if not self.isEnabled():
            painter.setOpacity(.45)
            state = 'normal'
        self._renderers[state].render(painter, QRectF(self.rect()))


class XPCaption(QWidget):
    close_requested = Signal()
    minimize_requested = Signal()

    def __init__(self, title: str, icon, *, controls: bool = True):
        super().__init__()
        self._title = title
        self._drag_origin: QPoint | None = None
        self.setFixedHeight(33)
        layout = QHBoxLayout(self)
        layout.setContentsMargins(5, 2, 3, 3)
        layout.setSpacing(3)
        layout.addStretch()
        self.minimize_button = _CaptionButton('minimize')
        self.maximize_button = _CaptionButton('maximize')
        self.close_button = _CaptionButton('close')
        if controls:
            layout.addWidget(self.minimize_button)
            layout.addWidget(self.maximize_button)
        layout.addWidget(self.close_button)
        self.minimize_button.clicked.connect(self.minimize_requested)
        self.close_button.clicked.connect(self.close_requested)

    def paintEvent(self, event):
        painter = QPainter(self)
        gradient = QLinearGradient(0, 0, 0, self.height())
        for stop, color in ((0, '#3d95ff'), (.06, '#0974f6'), (.14, '#0054e3'),
                            (.48, '#0054e3'), (.76, '#005cf4'), (.90, '#0468fc'),
                            (.96, '#0055ea'), (1, '#003dd7')):
            gradient.setColorAt(stop, QColor(color))
        painter.fillRect(self.rect(), gradient)
        painter.setPen(QColor('#6ba4ff'))
        painter.drawLine(5, 0, self.width() - 6, 0)
        font = QFont('Trebuchet MS')
        font.setPixelSize(16)
        font.setBold(True)
        painter.setFont(font)
        rect = self.rect().adjusted(5, 0, -90, 0)
        painter.setPen(QColor('#0f1089'))
        painter.drawText(rect.translated(1, 1), Qt.AlignmentFlag.AlignVCenter, self._title)
        painter.setPen(Qt.GlobalColor.white)
        painter.drawText(rect, Qt.AlignmentFlag.AlignVCenter, self._title)

    def mousePressEvent(self, event):
        if event.button() == Qt.MouseButton.LeftButton:
            self._drag_origin = event.globalPosition().toPoint() - self.window().pos()
            event.accept()

    def mouseMoveEvent(self, event):
        if self._drag_origin is not None and event.buttons() & Qt.MouseButton.LeftButton:
            self.window().move(event.globalPosition().toPoint() - self._drag_origin)
            event.accept()

    def mouseReleaseEvent(self, event):
        self._drag_origin = None


class XPFrame(QFrame):
    def __init__(self, parent: QWidget | None = None):
        super().__init__(parent)
        self.setObjectName('xpFrame')
        self.setFrameShape(QFrame.Shape.NoFrame)
        self.setFont(XP_FONT)
        self.setStyleSheet('''
            QFrame#xpFrame { background: #ece9d8; border: none; }
            QLabel, QRadioButton { color: #000; background: transparent; font-size: 13px; }
            QSpinBox { color: #000; background: #fff; border: 1px solid #7f9db9;
                min-height: 25px; padding-left: 4px; font-size: 13px; }
            QSpinBox:disabled { color: #aca899; background: #ece9d8; }
            QRadioButton { spacing: 7px; }
            QRadioButton::indicator { width: 14px; height: 14px; border: 1px solid #1c5180;
                border-radius: 8px; background: qlineargradient(x1:0,y1:0,x2:1,y2:1,stop:0 #dce0df,stop:1 #fff); }
            QRadioButton::indicator:checked { background: qradialgradient(cx:0.5,cy:0.5,radius:0.5,
                fx:0.5,fy:0.5,stop:0 #269b21,stop:0.36 #269b21,stop:0.44 #fff,stop:1 #fff); }
            QRadioButton::indicator:hover { border: 1px solid #e5a544; }
        ''')

    def paintEvent(self, event):
        painter = QPainter(self)
        painter.fillRect(self.rect(), QColor('#ece9d8'))
        for inset, left, right in ((0, '#0831d9', '#00138c'), (1, '#166aee', '#001ea0'), (2, '#0855dd', '#003bda')):
            painter.fillRect(inset, 0, 1, self.height() - inset, QColor(left))
            painter.fillRect(self.width() - 1 - inset, 0, 1, self.height() - inset, QColor(right))
            painter.fillRect(inset, self.height() - 1 - inset, self.width() - 2 * inset, 1, QColor(right))


def xp_window_region(rect: QRect) -> QRegion:
    region = QRegion(rect)
    for width, y in ((5, 0), (3, 1), (2, 2), (1, 3), (1, 4)):
        region -= QRegion(rect.x(), rect.y() + y, width, 1)
        region -= QRegion(rect.right() - width + 1, rect.y() + y, width, 1)
    return region


def disable_window_animation(widget: QWidget) -> None:
    if sys.platform != 'darwin' or QGuiApplication.platformName() != 'cocoa':
        return
    import ctypes
    import ctypes.util

    objc = ctypes.CDLL(ctypes.util.find_library('objc'))
    objc.sel_registerName.restype = ctypes.c_void_p
    objc.sel_registerName.argtypes = [ctypes.c_char_p]
    send = ctypes.CFUNCTYPE(ctypes.c_void_p, ctypes.c_void_p, ctypes.c_void_p)(('objc_msgSend', objc))
    window = send(int(widget.winId()), objc.sel_registerName(b'window'))
    if window:
        set_behavior = ctypes.CFUNCTYPE(None, ctypes.c_void_p, ctypes.c_void_p, ctypes.c_long)(('objc_msgSend', objc))
        set_behavior(window, objc.sel_registerName(b'setAnimationBehavior:'), 2)


class XPExitDialog(QDialog):
    def __init__(self, parent: QWidget, icon):
        super().__init__(parent, Qt.WindowType.Dialog | Qt.WindowType.FramelessWindowHint)
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground)
        self.setWindowTitle('Exit VK Dump Wizard')
        self.setWindowModality(Qt.WindowModality.ApplicationModal)
        self.setFixedSize(460, 180)
        self.setMask(xp_window_region(self.rect()))
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        frame = XPFrame()
        layout.addWidget(frame)
        frame.setMask(xp_window_region(self.rect()))
        body = QVBoxLayout(frame)
        body.setContentsMargins(3, 0, 3, 3)
        body.setSpacing(0)
        caption = XPCaption('Exit VK Dump Wizard', icon, controls=False)
        caption.close_requested.connect(self.reject)
        body.addWidget(caption)
        message = QHBoxLayout()
        message.setContentsMargins(18, 18, 18, 12)
        question = QLabel()
        question.setPixmap(self.style().standardIcon(QStyle.StandardPixmap.SP_MessageBoxQuestion).pixmap(36, 36))
        message.addWidget(question)
        message.addSpacing(12)
        text = QLabel('Do you really want to leave the wizard?')
        text.setWordWrap(True)
        message.addWidget(text, 1)
        body.addLayout(message, 1)
        buttons = QHBoxLayout()
        buttons.setContentsMargins(0, 0, 16, 14)
        buttons.setSpacing(8)
        buttons.addStretch()
        yes = XPButton('&Yes')
        no = XPButton('&No')
        no.setDefault(True)
        yes.clicked.connect(self.accept)
        no.clicked.connect(self.reject)
        buttons.addWidget(yes)
        buttons.addWidget(no)
        body.addLayout(buttons)
        no.setFocus()
        center = parent.frameGeometry().center()
        self.move(center.x() - self.width() // 2, center.y() - self.height() // 2)

    def showEvent(self, event):
        disable_window_animation(self)
        super().showEvent(event)

    @classmethod
    def confirm(cls, parent: QWidget, icon) -> bool:
        return cls(parent, icon).exec() == QDialog.DialogCode.Accepted
