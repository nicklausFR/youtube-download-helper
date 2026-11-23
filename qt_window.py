"""PySide6 presentation, layout, dialogs and main-thread timers."""
from qt_i18n import (bind_text, ui, add_translated, display_text, language_manager,
                     LANGUAGES, tr, message, unbind_text)
import os
import re
import sys
import unicodedata
from html import escape
from PySide6.QtCore import Qt, QDeadlineTimer, QTimer, QSettings, QEvent, QPoint, QRect, QRectF, QSize, QUrl, Signal
from PySide6.QtGui import (QPixmap, QFont, QPalette, QColor, QIcon, QPainter, QPen,
                           QAction, QActionGroup, QDesktopServices, QKeySequence, QPainterPath,
                           QTextLayout, QTextOption)
from PySide6.QtWidgets import (QApplication, QMainWindow, QWidget, QFrame, QLabel,
    QPushButton, QCheckBox, QLineEdit, QComboBox, QVBoxLayout, QHBoxLayout,
    QGridLayout, QScrollArea, QSizePolicy, QButtonGroup, QDialog, QTableWidget,
    QTableWidgetItem, QHeaderView, QAbstractItemView, QPlainTextEdit, QFileDialog,
    QMessageBox, QStyle, QMenu, QSlider, QListWidget, QListWidgetItem, QTabBar, QInputDialog,
    QDialogButtonBox, QStyleOptionButton, QStylePainter, QDoubleSpinBox, QAbstractSpinBox, QLayout,
    QSplitter)
from download_rate import MAX_RATE_BPS, MIN_RATE_BPS, RATE_UNITS
from qt_controls import Value, Choice, Progress, ArrowCombo, messagebox
from qt_download_list import DownloadTable
from youtube_access import is_youtube_url, youtube_video_id

_application = None
DOWNLOAD_VIDEO_ID_ROLE = Qt.ItemDataRole.UserRole + 1


class RateSpinBox(QDoubleSpinBox):
    def textFromValue(self, value):
        formatted = self.locale().toString(value, 'f', self.decimals())
        if not self.decimals():
            return formatted
        decimal = self.locale().decimalPoint()
        return formatted.rstrip('0').rstrip(decimal)


class DetailThumbnail(QWidget):
    def __init__(self, placeholder):
        super().__init__()
        self.image = QPixmap()
        self.content = None
        self.placeholder = placeholder
        self.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Ignored)
        bind_text(self, 'setAccessibleName', 'Vignette de la vidéo sélectionnée')

    def aspect_ratio(self):
        return 16 / 9 if self.image.isNull() else self.image.width() / self.image.height()

    def set_content(self, content):
        if self.content is content:
            return
        self.content = content
        self.image = QPixmap()
        if content:
            self.image.loadFromData(content)
        self.updateGeometry()
        self.update()

    def paintEvent(self, event):
        painter = QPainter(self)
        if self.image.isNull():
            painter.drawPixmap((self.width() - self.placeholder.width()) // 2,
                               (self.height() - self.placeholder.height()) // 2, self.placeholder)
        else:
            ratio = self.aspect_ratio()
            height = min(self.height(), self.width() / ratio)
            width = height * ratio
            target = QRectF((self.width() - width) / 2, (self.height() - height) / 2, width, height)
            painter.setRenderHint(QPainter.RenderHint.SmoothPixmapTransform)
            painter.drawPixmap(target, self.image, QRectF(self.image.rect()))
        painter.end()


class VideoIdentityLayout(QHBoxLayout):
    def setGeometry(self, rect):
        QLayout.setGeometry(self, rect)
        if self.count() != 2:
            return
        thumbnail, details = self.itemAt(0), self.itemAt(1)
        area = self.contentsRect()
        gap = self.spacing()
        right = area.x() + area.width()
        available_width = max(0, right - rect.x() - gap - details.minimumSize().width())
        width = min(available_width, round(rect.height() * thumbnail.widget().aspect_ratio()))
        thumbnail.setGeometry(QRect(rect.x(), rect.y(), width, rect.height()))
        left = rect.x() + width + gap if width else area.x()
        details.setGeometry(QRect(left, area.y(), max(0, right - left), area.height()))


def bundled_asset(name):
    root = getattr(sys, '_MEIPASS', os.path.dirname(os.path.abspath(__file__)))
    return os.path.join(root, 'assets', name)


class MediaRangeSlider(QSlider):
    """Horizontal range selector with independently draggable boundaries."""
    selectionChanged = Signal(int, int)

    def __init__(self, parent=None):
        super().__init__(Qt.Orientation.Horizontal, parent)
        self._selection_start = 0
        self._selection_end = 0
        self._active_boundary = None
        self._allowed_values = None
        self.setMinimumHeight(38)
        self.setMouseTracking(True)
        bind_text(self, 'setAccessibleName', 'Sélection de l’extrait')

    def setSelection(self, start, end):
        maximum = max(0, self.maximum())
        start = max(0, min(int(start), maximum))
        end = max(start, min(int(end), maximum))
        if self._allowed_values:
            start = self.snapValue(start, 'previous')
            end = self.snapValue(end, 'next')
            if end < start:
                end = start
        changed = (start, end) != (self._selection_start, self._selection_end)
        self._selection_start, self._selection_end = start, end
        if changed:
            self.selectionChanged.emit(start, end)
        self.update()

    def selectionStart(self):
        return self._selection_start

    def selectionEnd(self):
        return self._selection_end

    def setAllowedValues(self, values):
        if values:
            maximum = max(0, self.maximum())
            self._allowed_values = sorted({
                max(0, min(int(value), maximum)) for value in values
            })
        else:
            self._allowed_values = None
        self.setSelection(self._selection_start, self._selection_end)
        self.update()

    def allowedValues(self):
        return tuple(self._allowed_values or ())

    def snapValue(self, value, direction='nearest'):
        if not self._allowed_values:
            return int(value)
        if direction == 'previous':
            candidates = [allowed for allowed in self._allowed_values if allowed <= int(value)]
            return candidates[-1] if candidates else self._allowed_values[0]
        if direction == 'next':
            candidates = [allowed for allowed in self._allowed_values if allowed >= int(value)]
            return candidates[0] if candidates else self._allowed_values[-1]
        return min(self._allowed_values, key=lambda allowed: abs(allowed - int(value)))

    def _value_to_x(self, value):
        margin = 9
        usable = max(1, self.width() - margin * 2)
        return margin + value / max(1, self.maximum()) * usable

    def _x_to_value(self, x):
        margin = 9
        usable = max(1, self.width() - margin * 2)
        ratio = max(0.0, min(1.0, (x - margin) / usable))
        return round(ratio * max(1, self.maximum()))

    def paintEvent(self, _event):
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        start_x = self._value_to_x(self._selection_start)
        end_x = self._value_to_x(self._selection_end)
        center_y = self.height() // 2
        track = self.palette().color(QPalette.ColorRole.Mid)
        accent = self.palette().color(QPalette.ColorRole.Highlight)
        if not self.isEnabled():
            accent = track
        painter.setPen(QPen(track, 4, Qt.PenStyle.SolidLine, Qt.PenCapStyle.RoundCap))
        painter.drawLine(9, center_y, self.width() - 9, center_y)
        if self._allowed_values and len(self._allowed_values) <= 500:
            painter.setPen(QPen(track.lighter(135), 1))
            for value in self._allowed_values:
                x = round(self._value_to_x(value))
                painter.drawLine(x, center_y - 6, x, center_y + 6)
        painter.setPen(QPen(accent, 5, Qt.PenStyle.SolidLine, Qt.PenCapStyle.RoundCap))
        painter.drawLine(round(start_x), center_y, round(end_x), center_y)
        for x, boundary in ((start_x, 'start'), (end_x, 'end')):
            color = accent.darker(125) if self._active_boundary == boundary else accent
            painter.setPen(QPen(color, 4, Qt.PenStyle.SolidLine, Qt.PenCapStyle.RoundCap))
            painter.drawLine(round(x), 4, round(x), self.height() - 4)
            painter.setBrush(self.palette().color(QPalette.ColorRole.Base))
            painter.setPen(QPen(color, 2))
            painter.drawEllipse(round(x) - 6, center_y - 6, 12, 12)
        painter.end()

    def mousePressEvent(self, event):
        if not self.isEnabled():
            return super().mousePressEvent(event)
        x = event.position().x()
        start_distance = abs(x - self._value_to_x(self._selection_start))
        end_distance = abs(x - self._value_to_x(self._selection_end))
        self._active_boundary = 'start' if start_distance <= end_distance else 'end'
        self._move_boundary(x)
        event.accept()

    def mouseMoveEvent(self, event):
        if self._active_boundary:
            self._move_boundary(event.position().x())
            event.accept()
        else:
            super().mouseMoveEvent(event)

    def mouseReleaseEvent(self, event):
        self._active_boundary = None
        self.update()
        event.accept()

    def _move_boundary(self, x):
        value = self._x_to_value(x)
        if self._active_boundary == 'start':
            self.setSelection(min(value, self._selection_end), self._selection_end)
        elif self._active_boundary == 'end':
            self.setSelection(self._selection_start, max(value, self._selection_start))


class IndexProgressButton(QPushButton):
    """Push button with a task progress fill drawn inside its normal frame."""
    def __init__(self, text='', parent=None):
        super().__init__(text, parent)
        self._progress = None
        self._indeterminate = False
        self._animation_position = 0
        self._animation_timer = QTimer(self)
        self._animation_timer.setInterval(35)
        self._animation_timer.timeout.connect(self._advance_animation)

    def setProgress(self, value):
        self._progress = None if value is None else max(0.0, min(100.0, float(value)))
        if value is not None:
            self.setIndeterminate(False)
        self.update()

    def progress(self):
        return self._progress

    def setIndeterminate(self, active):
        active = bool(active)
        if active == self._indeterminate:
            return
        self._indeterminate = active
        self._animation_position = 0
        if active:
            self._animation_timer.start()
        else:
            self._animation_timer.stop()
        self.update()

    def isIndeterminate(self):
        return self._indeterminate

    def _advance_animation(self):
        self._animation_position = (self._animation_position + 3) % 101
        self.update()

    def paintEvent(self, _event):
        option = QStyleOptionButton()
        self.initStyleOption(option)
        text = option.text
        option.text = ''
        painter = QStylePainter(self)
        painter.drawControl(QStyle.ControlElement.CE_PushButton, option)
        if self._indeterminate:
            inner = self.rect().adjusted(2, 2, -2, -2)
            chunk_width = max(12, round(inner.width() * 0.28))
            travel = inner.width() + chunk_width
            left = inner.left() - chunk_width + round(
                travel * self._animation_position / 100)
            fill = inner.intersected(
                inner.adjusted(left - inner.left(), 0,
                               left + chunk_width - inner.right(), 0))
            path = QPainterPath()
            path.addRoundedRect(inner, 5, 5)
            painter.save()
            painter.setClipPath(path)
            color = self.palette().color(QPalette.ColorRole.Highlight)
            color.setAlpha(115)
            painter.fillRect(fill, color)
            painter.restore()
        elif self._progress is not None and self._progress > 0:
            inner = self.rect().adjusted(2, 2, -2, -2)
            fill = inner.adjusted(0, 0, -round(inner.width() * (1 - self._progress / 100)), 0)
            path = QPainterPath()
            path.addRoundedRect(inner, 5, 5)
            painter.save()
            painter.setClipPath(path)
            color = self.palette().color(QPalette.ColorRole.Highlight)
            color.setAlpha(115)
            painter.fillRect(fill, color)
            painter.restore()
        painter.setPen(self.palette().color(QPalette.ColorRole.ButtonText))
        painter.drawText(self.rect(), Qt.AlignmentFlag.AlignCenter, text)


class ClickableLabel(QLabel):
    clicked = Signal()

    def mouseReleaseEvent(self, event):
        if event.button() == Qt.MouseButton.LeftButton:
            self.clicked.emit()
            event.accept()
            return
        super().mouseReleaseEvent(event)


def youtube_link_from_mime(mime):
    if mime is None:
        return None
    for item in mime.urls():
        value = item.toString().strip()
        if is_youtube_url(value):
            return value
    for match in re.finditer(r'https?://[^\s<>"\']+', mime.text(), re.IGNORECASE):
        value = match.group(0).rstrip('.,;:!?)]}')
        if is_youtube_url(value):
            return value
    return None


class CircularActivity(QWidget):
    def __init__(self):
        super().__init__()
        self.setFixedSize(18, 18)
        self._angle = 0
        self._timer = QTimer(self)
        self._timer.setInterval(40)
        self._timer.timeout.connect(self._rotate)

    def showEvent(self, event):
        super().showEvent(event)
        self._timer.start()

    def hideEvent(self, event):
        self._timer.stop()
        super().hideEvent(event)

    def _rotate(self):
        self._angle = (self._angle + 15) % 360
        self.update()

    def paintEvent(self, event):
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        painter.setPen(QPen(
            self.palette().color(QPalette.ColorRole.Highlight), 1.5,
            Qt.PenStyle.SolidLine, Qt.PenCapStyle.RoundCap, Qt.PenJoinStyle.RoundJoin))
        painter.translate(self.width() / 2, self.height() / 2)
        painter.rotate(self._angle)
        arc = QRectF(-5, -5, 10, 10)
        arrow = QPainterPath()
        arrow.arcMoveTo(arc, 155)
        arrow.arcTo(arc, 155, -125)
        tip = arrow.currentPosition()
        arrow.lineTo(1.5, -2.8)
        arrow.moveTo(tip)
        arrow.lineTo(4.6, -5.4)
        painter.drawPath(arrow)
        painter.rotate(180)
        painter.drawPath(arrow)


def _wrapped_title(text, label, width):
    text = ' '.join(text.split())
    if not text:
        return ''
    width = max(1, width)
    layout = QTextLayout(text, label.font())
    options = QTextOption()
    options.setWrapMode(QTextOption.WrapMode.WrapAtWordBoundaryOrAnywhere)
    layout.setTextOption(options)
    layout.beginLayout()
    line = layout.createLine()
    line.setLineWidth(width)
    first_line_size = line.textLength() * 2
    layout.endLayout()
    # Qt reports the line length in UTF-16 units, including for emoji.
    encoded = text.encode('utf-16-le')
    if first_line_size >= len(encoded):
        return text
    first = encoded[:first_line_size].decode('utf-16-le').rstrip()
    rest = encoded[first_line_size:].decode('utf-16-le').lstrip()
    rest = label.fontMetrics().elidedText(rest, Qt.TextElideMode.ElideRight, width)
    return first + '\n' + rest


class VideoTabStrip(QWidget):
    button_width = 48
    gutter = button_width + 4
    add_gap = 8
    empty_height = 56

    def __init__(self, add_button):
        super().__init__()
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)
        self.setFixedHeight(self.empty_height)
        self._offset = 0
        self._content_width = 0
        self.viewport = QWidget(self)
        self.tabs = QTabBar(self.viewport)
        self.tabs.setUsesScrollButtons(False)
        self.tabs.currentChanged.connect(self.reveal_tab)
        self.add_button = add_button
        self.add_button.setParent(self)
        self.left_button = QPushButton(self)
        self.right_button = QPushButton(self)
        for button in (self.left_button, self.right_button):
            button.setObjectName('tabOverflow')
            button.setFixedSize(self.button_width, 28)
            button.setCursor(Qt.CursorShape.PointingHandCursor)
            button.hide()
        self.left_button.clicked.connect(self._scroll_left)
        self.right_button.clicked.connect(self._scroll_right)
        self.left_fade = QFrame(self.viewport)
        self.right_fade = QFrame(self.viewport)
        self.left_fade.setObjectName('videoTabLeftFade')
        self.right_fade.setObjectName('videoTabRightFade')
        for fade in (self.left_fade, self.right_fade):
            fade.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents)
            fade.hide()

    def refresh(self):
        size = self.tabs.sizeHint()
        self._content_width = size.width()
        self.setFixedHeight(max(self.empty_height, size.height()))
        self.reveal_tab(self.tabs.currentIndex())

    def resizeEvent(self, event):
        super().resizeEvent(event)
        self.reveal_tab(self.tabs.currentIndex())

    def _available_width(self):
        return self.width() - self.add_button.width() - self.add_gap

    def _place_tabs(self):
        available = self._available_width()
        overflow = self._content_width > available
        # At either end the viewport only needs to leave room for one button.
        end = self._content_width - available + self.gutter if overflow else 0
        self._offset = max(0, min(self._offset, end))
        left = self._offset > 0
        right = self._offset < end
        x = self.gutter if left else 0
        width = max(0, min(self._content_width, available - x - (self.gutter if right else 0)))
        self.viewport.setGeometry(x, 0, width, self.height())
        self.tabs.setGeometry(-self._offset, 0, max(width, self._content_width), self.height())
        add_x = x + width + (self.add_gap if width else 0)
        self.add_button.setGeometry(add_x, 0, self.add_button.width(), self.height())
        rects = [self.tabs.tabRect(index) for index in range(self.tabs.count())]
        right_edge = self._offset + width
        hidden_left = [rect for rect in rects if rect.left() < self._offset]
        hidden_right = [rect for rect in rects if rect.right() >= right_edge]
        left_count = sum(rect.right() < self._offset for rect in hidden_left)
        right_count = sum(rect.left() >= right_edge for rect in hidden_right)
        bind_text(self.left_button, 'setText', f'‹ +{left_count}' if left_count else '‹')
        bind_text(self.right_button, 'setText', f'+{right_count} ›' if right_count else '›')
        bind_text(self.left_button, 'setToolTip', f'Onglets masqués à gauche · {left_count}' if left_count
                                    else 'Défiler vers la gauche')
        bind_text(self.right_button, 'setToolTip', f'Onglets masqués à droite · {right_count}' if right_count
                                     else 'Défiler vers la droite')
        y = max(0, (self.height() - self.left_button.height()) // 2)
        self.left_button.move(0, y)
        self.right_button.move(self.width() - self.button_width, y)
        self.left_button.setVisible(left)
        self.right_button.setVisible(right)
        # Fade only the visible part of an edge tab that is actually clipped.
        left_clip = hidden_left[-1].right() + 1 - self._offset if hidden_left else 0
        right_clip = right_edge - hidden_right[0].left() if hidden_right else 0
        left_fade_width = max(0, min(56, width, left_clip))
        right_fade_width = max(0, min(56, width, right_clip))
        self.left_fade.setGeometry(0, 0, left_fade_width, self.height())
        self.right_fade.setGeometry(width - right_fade_width, 0, right_fade_width, self.height())
        self.left_fade.setVisible(left_fade_width > 0)
        self.right_fade.setVisible(right_fade_width > 0)

    def reveal_tab(self, index):
        self._place_tabs()
        if not 0 <= index < self.tabs.count():
            return
        rect = self.tabs.tabRect(index)
        if rect.left() < self._offset:
            self._offset = rect.left()
        elif rect.right() >= self._offset + self.viewport.width():
            self._offset = rect.right() + 1 - max(1, self._available_width() - 2 * self.gutter)
        else:
            return
        self._place_tabs()

    def _scroll_left(self):
        for index in reversed(range(self.tabs.count())):
            if self.tabs.tabRect(index).left() < self._offset:
                self.reveal_tab(index)
                return

    def _scroll_right(self):
        for index in range(self.tabs.count()):
            if self.tabs.tabRect(index).right() >= self._offset + self.viewport.width():
                self.reveal_tab(index)
                return


class VideoTabPreview(QWidget):
    def __init__(self):
        super().__init__()
        self.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents)
        row = QHBoxLayout(self)
        row.setContentsMargins(0, 0, 0, 0)
        row.setSpacing(10)
        self.thumbnail = QLabel()
        self.thumbnail.setObjectName('tabThumbnail')
        self.thumbnail.setFixedSize(112, 63)
        self.thumbnail.setAlignment(Qt.AlignmentFlag.AlignCenter)
        row.addWidget(self.thumbnail)
        labels = QVBoxLayout()
        labels.setSpacing(2)
        self.title = QLabel()
        self.title.setObjectName('tabTitle')
        self.title.setTextFormat(Qt.TextFormat.PlainText)
        self.title.setFixedWidth(2 * self.thumbnail.width())
        labels.addWidget(self.title)
        state_row = QHBoxLayout()
        state_row.setSpacing(4)
        self.spinner = CircularActivity()
        self.spinner.hide()
        state_row.addWidget(self.spinner)
        self.state = QLabel()
        self.state.setObjectName('tabState')
        state_row.addWidget(self.state, 1)
        self.download_progress = Progress(compact=True)
        self.download_progress.setFixedWidth(104)
        bind_text(self.download_progress, 'setAccessibleName', 'Progression du téléchargement')
        self.download_progress.hide()
        state_row.addWidget(self.download_progress, 1, Qt.AlignmentFlag.AlignLeft)
        labels.addLayout(state_row)
        row.addLayout(labels)

    def set_title(self, title):
        self.title.ensurePolished()
        self.title.setText(_wrapped_title(title, self.title, self.title.width()))


class QueueLabel(QLabel):
    def __init__(self, text='', name='', *, wrap=False):
        super().__init__(text)
        self._wrap = wrap
        self.setObjectName(name)
        self.setTextFormat(Qt.TextFormat.PlainText)
        self.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Fixed)
        self.setFixedHeight(38 if wrap else 20)

    def paintEvent(self, event):
        rect = self.contentsRect()
        metrics = self.fontMetrics()
        if self._wrap:
            lines = _wrapped_title(self.text(), self, rect.width()).split('\n')
        else:
            lines = [metrics.elidedText(' '.join(self.text().split()),
                                       Qt.TextElideMode.ElideRight, rect.width())]
        height = metrics.height()
        line_rect = QRect(rect.x(), rect.y() + (rect.height() - height * len(lines)) // 2,
                          rect.width(), height)
        painter = QStylePainter(self)
        for line in lines:
            painter.drawItemText(line_rect, Qt.AlignmentFlag.AlignVCenter | Qt.AlignmentFlag.AlignLeft,
                                 self.palette(), self.isEnabled(), line, self.foregroundRole())
            line_rect.translate(0, height)


class DownloadQueueRow(QFrame):
    clicked = Signal()

    def __init__(self, title=''):
        super().__init__()
        self.setMinimumHeight(52)
        self.setSizePolicy(QSizePolicy.Policy.Preferred, QSizePolicy.Policy.Fixed)
        row = QHBoxLayout(self)
        row.setContentsMargins(6, 4, 6, 4)
        row.setSpacing(8)
        self.order = QVBoxLayout()
        self.order.setContentsMargins(0, 0, 0, 0)
        self.order.setSpacing(1)
        ordering = QWidget()
        ordering.setFixedWidth(30)
        ordering.setLayout(self.order)
        row.addWidget(ordering)

        identity = QHBoxLayout()
        identity.setSpacing(7)
        self.thumbnail = QLabel()
        self.thumbnail.setObjectName('tabThumbnail')
        self.thumbnail.setFixedSize(64, 36)
        self.thumbnail.setAlignment(Qt.AlignmentFlag.AlignCenter)
        identity.addWidget(self.thumbnail)
        self.title = QueueLabel(title, 'fieldLabel', wrap=True)
        identity.addWidget(self.title, 1)
        row.addLayout(identity, 3)

        self.status = QVBoxLayout()
        self.status.setSpacing(1)
        row.addLayout(self.status, 2)
        actions = QWidget()
        actions.setFixedWidth(224)
        self.actions = QHBoxLayout(actions)
        self.actions.setContentsMargins(0, 0, 0, 0)
        self.actions.setSpacing(4)
        self.actions.addStretch()
        row.addWidget(actions)

    def mouseReleaseEvent(self, event):
        position = event.position().toPoint()
        if (event.button() == Qt.MouseButton.LeftButton and self.rect().contains(position)
                and not isinstance(self.childAt(position), QPushButton)):
            self.clicked.emit()
        super().mouseReleaseEvent(event)


class ShutdownCountdown(QMessageBox):
    def __init__(self, parent):
        super().__init__(QMessageBox.Icon.Warning, 'Arrêt de l’ordinateur',
                         'Tous les téléchargements sont terminés.',
                         QMessageBox.StandardButton.Cancel, parent)
        bind_text(self, 'setWindowTitle', 'Arrêt de l’ordinateur')
        bind_text(self, 'setText', 'Tous les téléchargements sont terminés.')
        cancel = self.button(QMessageBox.StandardButton.Cancel)
        bind_text(cancel, 'setText', 'Annuler l’arrêt')
        self.setDefaultButton(cancel)
        self.setEscapeButton(cancel)
        self._deadline = QDeadlineTimer(30_000, Qt.TimerType.PreciseTimer)
        self._clock = QTimer(self)
        self._clock.timeout.connect(self._refresh_countdown)
        self.finished.connect(self._clock.stop)
        self._refresh_countdown()
        self._clock.start(250)

    def _refresh_countdown(self):
        seconds = (self._deadline.remainingTime() + 999) // 1000
        if seconds:
            bind_text(self, 'setInformativeText', f'L’ordinateur s’arrêtera dans {seconds} s.')
        else:
            self.accept()


def application():
    global _application
    if _application is None:
        _application = QApplication.instance() or QApplication(sys.argv[:1])
        _application.setStyle('Fusion')
        _application.setApplicationName('Téléchargeur YouTube')
        _application.setOrganizationName('YouTubeDownloadHelper')
        bind_text(_application, 'setApplicationDisplayName', 'Téléchargeur YouTube')
        icon_path = bundled_asset('app-icon.ico')
        if os.path.isfile(icon_path):
            _application.setWindowIcon(QIcon(icon_path))
    return _application


class DownloaderWindow(QMainWindow):
    def __init__(self):
        application()
        super().__init__()
        self._timers = set()
        self._disposed = False
        self._closing = False
        self.preferences = QSettings()
        language_manager().set_language(self.preferences.value('language', 'fr'))
        language_manager().languageChanged.connect(self._language_changed)
        # Keep the native caption. Qt 6.9's expanded client area creates a
        # separate window for the buttons that can stay visible when minimized.
        self.resize(880, 760)
        self.setMinimumSize(640, 530)
        bind_text(self, 'setWindowTitle', 'Téléchargeur YouTube')
        self.setWindowIcon(application().windowIcon())
        self.setAcceptDrops(True)

    def after(self, milliseconds, callback):
        timer = QTimer(self)
        timer.setSingleShot(True)
        self._timers.add(timer)
        def fire():
            self._timers.discard(timer)
            timer.deleteLater()
            if not self._disposed: callback()
        timer.timeout.connect(fire)
        timer.start(milliseconds)
        return timer

    def after_cancel(self, timer):
        if timer in self._timers:
            self._timers.discard(timer)
            timer.stop()
            timer.deleteLater()

    def after_idle(self, callback):
        return self.after(0, callback)

    def resizeEvent(self, event):
        super().resizeEvent(event)
        if hasattr(self, 'selected_video_panel'):
            self._update_selected_video_layout()
        if hasattr(self, 'download_queue_list') and not self.list_view:
            self._fit_download_queue_height()

    def dispose(self):
        self._disposed = True
        application().removeEventFilter(self)
        for timer in tuple(self._timers): self.after_cancel(timer)
        self.hide()
        self.deleteLater()

    def closeEvent(self, event):
        if self._closing:
            event.accept()
            return
        if self.download_thread is not None:
            answer = QMessageBox.question(self, tr('Téléchargement en cours'),
                tr('Interrompre le téléchargement et fermer le programme ?'),
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
                QMessageBox.StandardButton.No)
            if answer != QMessageBox.StandardButton.Yes:
                event.ignore(); return
        event.ignore()
        self._close_app()

    def _finish_close(self):
        self.preferences.setValue('folder', self.folder_var.get())
        self._closing = True
        for timer in tuple(self._timers): self.after_cancel(timer)
        self.close()

    @staticmethod
    def _layout(widget, kind=QVBoxLayout, margins=0, spacing=12):
        layout = kind(widget)
        layout.setContentsMargins(margins, margins, margins, margins)
        layout.setSpacing(spacing)
        return layout

    def _label(self, variable=None, text='', name='', wrap=False, translate=True):
        label = ui(QLabel(), text=text) if translate else QLabel(text)
        label.setObjectName(name)
        label.setWordWrap(wrap)
        label.setTextFormat(Qt.TextFormat.PlainText)
        label.setMinimumWidth(0)
        if variable is not None:
            bind_text(label, 'setText', variable.get())
            variable.changed.connect(lambda source: bind_text(label, 'setText', source))
        return label

    def _field(self, label, variable, name):
        widget = QWidget()
        layout = self._layout(widget, spacing=6)
        caption = self._label(text=label, name='fieldLabel')
        combo = Choice(variable, widget)
        bind_text(combo, 'setAccessibleName', label)
        caption.setBuddy(combo)
        layout.addWidget(caption); layout.addWidget(combo)
        setattr(self, name, combo)
        return widget

    def _subtitle_field(self):
        widget = QWidget()
        layout = self._layout(widget, spacing=6)
        caption = self._label(text='Sous-titres', name='fieldLabel')
        self.subtitle_combo = ui(QPushButton(), text=self.subtitle_var.get())
        self.subtitle_combo.setObjectName('subtitlePicker')
        bind_text(self.subtitle_combo, 'setAccessibleName', 'Choisir plusieurs sous-titres')
        bind_text(self.subtitle_combo, 'setToolTip', 'Choisir une ou plusieurs langues de sous-titres.')
        self.subtitle_combo.setMinimumWidth(0)
        self.subtitle_combo.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Fixed)
        self.subtitle_combo.clicked.connect(self._open_subtitle_picker)
        self.subtitle_var.changed.connect(lambda source: bind_text(self.subtitle_combo, 'setText', source))
        caption.setBuddy(self.subtitle_combo)
        layout.addWidget(caption)
        layout.addWidget(self.subtitle_combo)
        return widget

    def _audio_language_field(self):
        widget = QWidget()
        layout = self._layout(widget, spacing=6)
        caption = self._label(text='Langues audio', name='fieldLabel')
        self.audio_language_picker = ui(QPushButton(), text=self.audio_language_summary_var.get())
        self.audio_language_picker.setObjectName('subtitlePicker')
        bind_text(self.audio_language_picker, 'setAccessibleName', 'Choisir plusieurs langues audio')
        self.audio_language_picker.setMinimumWidth(0)
        self.audio_language_picker.setSizePolicy(
            QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Fixed)
        self.audio_language_picker.clicked.connect(self._open_audio_language_picker)
        self.audio_language_summary_var.changed.connect(
            lambda source: bind_text(self.audio_language_picker, 'setText', source))
        caption.setBuddy(self.audio_language_picker)
        layout.addWidget(caption)
        layout.addWidget(self.audio_language_picker)
        # Internal compatibility selector: format synchronization still uses
        # the same stable model without exposing a second language control.
        self.audio_language_combo = Choice(self.audio_language_var, widget)
        self.audio_language_combo.hide()
        return widget

    def _open_audio_language_picker(self):
        choices = list(self._audio_language_choices())
        if not choices:
            return
        selected = set(self._selected_audio_language_labels())
        working = set(selected)
        dialog = QDialog(self)
        bind_text(dialog, 'setWindowTitle', 'Choisir les langues audio')
        dialog.resize(560, 430)
        layout = self._layout(dialog, margins=18, spacing=10)
        search = QLineEdit()
        bind_text(search, 'setPlaceholderText', 'Rechercher une langue : français, anglais…')
        search.setClearButtonEnabled(True)
        layout.addWidget(search)
        audio_list = QListWidget()
        audio_list.setAlternatingRowColors(True)
        audio_list.setSelectionMode(QAbstractItemView.SelectionMode.NoSelection)
        layout.addWidget(audio_list, 1)
        info = self._label(name='muted', wrap=True)
        layout.addWidget(info)
        buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok |
            QDialogButtonBox.StandardButton.Cancel)
        clear = add_translated(buttons, 'addButton', 'Tout effacer', QDialogButtonBox.ButtonRole.ResetRole)
        bind_text(buttons.button(QDialogButtonBox.StandardButton.Ok), 'setText', 'Valider')
        bind_text(buttons.button(QDialogButtonBox.StandardButton.Cancel), 'setText', 'Annuler')
        layout.addWidget(buttons)

        def update_info():
            count = len(working)
            bind_text(info, 'setText', 'Aucune langue sélectionnée' if not count else
                '1 langue sélectionnée' if count == 1 else
                f'{count} langues sélectionnées')

        def item_changed(item):
            label = item.data(Qt.ItemDataRole.UserRole)
            if item.checkState() == Qt.CheckState.Checked:
                working.add(label)
            else:
                working.discard(label)
            update_info()

        def rebuild():
            query = self._subtitle_filter_text(search.text())
            audio_list.blockSignals(True)
            audio_list.clear()
            for label in choices:
                if query and query not in self._subtitle_filter_text(label + ' ' + display_text(label)):
                    continue
                item = ui(QListWidgetItem(), text=label)
                item.setData(Qt.ItemDataRole.UserRole, label)
                item.setFlags(item.flags() | Qt.ItemFlag.ItemIsUserCheckable)
                item.setCheckState(
                    Qt.CheckState.Checked if label in working
                    else Qt.CheckState.Unchecked)
                audio_list.addItem(item)
            audio_list.blockSignals(False)
            update_info()

        def clear_all():
            working.clear()
            rebuild()

        audio_list.itemChanged.connect(item_changed)
        search.textChanged.connect(rebuild)
        clear.clicked.connect(clear_all)
        buttons.accepted.connect(dialog.accept)
        buttons.rejected.connect(dialog.reject)
        rebuild()
        search.setFocus()
        if dialog.exec() == QDialog.DialogCode.Accepted:
            self._set_selected_audio_languages(
                [label for label in choices if label in working])

    @staticmethod
    def _subtitle_filter_text(value):
        value = unicodedata.normalize('NFKD', value or '')
        return ''.join(char for char in value if not unicodedata.combining(char)).casefold()

    def _open_subtitle_picker(self):
        tracks = list(self.subtitle_tracks)
        if not tracks:
            return
        selected = set(self._selected_subtitle_codes())
        working = set(selected)
        dialog = QDialog(self)
        bind_text(dialog, 'setWindowTitle', 'Choisir les sous-titres')
        dialog.resize(590, 500)
        layout = self._layout(dialog, margins=18, spacing=10)
        search = QLineEdit()
        bind_text(search, 'setPlaceholderText', 'Rechercher une langue : français, fr, anglais…')
        search.setClearButtonEnabled(True)
        layout.addWidget(search)
        show_all = ui(QCheckBox(), text='Afficher les autres traductions automatiques')
        layout.addWidget(show_all)
        subtitle_list = QListWidget()
        subtitle_list.setAlternatingRowColors(True)
        subtitle_list.setSelectionMode(QAbstractItemView.SelectionMode.NoSelection)
        layout.addWidget(subtitle_list, 1)
        info = QLabel()
        info.setObjectName('muted')
        info.setWordWrap(True)
        layout.addWidget(info)
        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel)
        clear = add_translated(buttons, 'addButton', 'Tout effacer', QDialogButtonBox.ButtonRole.ResetRole)
        bind_text(buttons.button(QDialogButtonBox.StandardButton.Ok), 'setText', 'Valider')
        bind_text(buttons.button(QDialogButtonBox.StandardButton.Cancel), 'setText', 'Annuler')
        layout.addWidget(buttons)

        ordered = sorted(tracks, key=lambda track: (
            not track.get('original'), bool(track.get('kind')),
            track.get('kind') == 'traduction auto',
            self._subtitle_filter_text(track.get('label', '')),
        ))
        hidden_count = [0]

        def update_info(hidden=0):
            chosen = len(working)
            selection = 'Aucune piste sélectionnée' if not chosen else (
                '1 piste sélectionnée' if chosen == 1 else f'{chosen} pistes sélectionnées')
            suffix = (f' · {hidden} traduction masquée' if hidden == 1 else
                      f' · {hidden} traductions masquées' if hidden else '')
            bind_text(info, 'setText', selection + suffix)

        def item_changed(item):
            code = item.data(Qt.ItemDataRole.UserRole)
            if item.checkState() == Qt.CheckState.Checked:
                working.add(code)
            else:
                working.discard(code)
            update_info(hidden_count[0])

        def rebuild():
            query = self._subtitle_filter_text(search.text())
            subtitle_list.blockSignals(True)
            subtitle_list.clear()
            hidden = 0
            for track in ordered:
                code = track['code']
                base = code.lower().removesuffix('-orig').split('-')[0]
                translated = track.get('kind') == 'traduction auto'
                haystack = self._subtitle_filter_text(code + ' ' + track['label'] + ' ' + display_text(track['label']))
                recommended = (not translated or track.get('original') or base in ('fr', 'en') or code in working)
                visible = query in haystack if query else (show_all.isChecked() or recommended)
                if not visible:
                    if translated:
                        hidden += 1
                    continue
                item = ui(QListWidgetItem(), text=track['label'])
                item.setData(Qt.ItemDataRole.UserRole, code)
                item.setFlags(item.flags() | Qt.ItemFlag.ItemIsUserCheckable)
                item.setCheckState(Qt.CheckState.Checked if code in working else Qt.CheckState.Unchecked)
                bind_text(item, 'setToolTip', track.get('tooltip', track['label'] + '\nCode : ' + code))
                subtitle_list.addItem(item)
            subtitle_list.blockSignals(False)
            hidden_count[0] = hidden
            update_info(hidden)

        def clear_all():
            working.clear()
            rebuild()

        subtitle_list.itemChanged.connect(item_changed)
        search.textChanged.connect(rebuild)
        show_all.toggled.connect(rebuild)
        clear.clicked.connect(clear_all)
        buttons.accepted.connect(dialog.accept)
        buttons.rejected.connect(dialog.reject)
        rebuild()
        search.setFocus()
        if dialog.exec() == QDialog.DialogCode.Accepted:
            self._set_selected_subtitles([track['code'] for track in ordered if track['code'] in working])

    def _button(self, text, callback, name='', icon=None):
        button = ui(QPushButton(), text=text)
        button.setObjectName(name)
        if icon is not None: button.setIcon(self.style().standardIcon(icon))
        button.clicked.connect(callback)
        return button

    @staticmethod
    def _size_download_action(button, *, compact=False):
        button.setProperty('downloadAction', True)
        button.setFixedHeight(40)
        if compact:
            button.setFixedWidth(40)
        else:
            button.setMinimumWidth(110)

    def _build_options_menu(self):
        self.options_button = QPushButton(self)
        self.options_button.setObjectName('optionsButton')
        bind_text(self.options_button, 'setAccessibleName', 'Options')
        bind_text(self.options_button, 'setToolTip', 'Thème, langue et dépendances')
        self.options_button.setFixedSize(36, 36)
        self.options_button.setIconSize(QSize(20, 20))

        self.options_menu = QMenu(self)
        self.options_button.clicked.connect(self._open_options_menu)
        themes = add_translated(self.options_menu, 'addMenu', 'Thème')
        self.theme_actions = {}
        theme_group = QActionGroup(self); theme_group.setExclusive(True)
        for theme in ('Système','Clair','Sombre'):
            action = add_translated(themes, 'addAction', theme); action.setCheckable(True)
            action.setChecked(theme == self.theme_value); theme_group.addAction(action)
            action.triggered.connect(lambda checked=False,value=theme:self._theme_changed(value))
            self.theme_actions[theme] = action
        languages = add_translated(self.options_menu, 'addMenu', 'Langue')
        self.language_actions = {}
        language_group = QActionGroup(self)
        language_group.setExclusive(True)
        for code, name in LANGUAGES.items():
            action = languages.addAction(name)
            action.setCheckable(True)
            action.setChecked(code == language_manager().language)
            language_group.addAction(action)
            action.triggered.connect(lambda checked=False, value=code: self._set_language(value))
            self.language_actions[code] = action
        self.options_menu.addSeparator()
        add_translated(self.options_menu, 'addAction', 'Dépendances', self._show_dependencies)

    def _set_language(self, language):
        language_manager().set_language(language)
        self.preferences.setValue('language', language_manager().language)

    def _language_changed(self, language):
        for code, action in getattr(self, 'language_actions', {}).items():
            action.setChecked(code == language)

    def _open_options_menu(self):
        origin = self.options_button.mapToGlobal(QPoint(0, self.options_button.height()))
        self.options_menu.popup(origin)

    def _build_ui(self):
        self.theme_value = self.preferences.value('theme', 'Système')
        self._build_options_menu()
        central = QWidget(); self.setCentralWidget(central)
        outer = self._layout(central, spacing=0)
        self.scroll_area = QScrollArea(); self.scroll_area.setWidgetResizable(True)
        self.scroll_area.setFrameShape(QFrame.Shape.NoFrame)
        self.scroll_area.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        # Reserve the scrollbar width in every mode so wrapped descriptions do not shift.
        self.scroll_area.setVerticalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOn)
        content = QWidget(); content.setObjectName('content')
        body = self._layout(content, margins=16, spacing=12)
        self.scroll_area.setWidget(content)
        self.scroll_area.viewport().installEventFilter(self)
        self.scroll_fade = QFrame(self.scroll_area.viewport())
        self.scroll_fade.setObjectName('scrollFade')
        self.scroll_fade.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents)
        self.scroll_fade.setFixedHeight(34)
        scrollbar = self.scroll_area.verticalScrollBar()
        scrollbar.rangeChanged.connect(self._update_scroll_cue)
        scrollbar.valueChanged.connect(self._update_scroll_cue)
        self.source_panel = QFrame(); self.source_panel.setObjectName('sourcePanel')
        source_layout = self._layout(self.source_panel, QHBoxLayout, margins=12, spacing=8)
        self.new_link_button = self._button('+', self._new_link, 'addVideo')
        self.new_link_button.setFixedWidth(52)
        bind_text(self.new_link_button, 'setAccessibleName', 'Ajouter une vidéo')
        bind_text(self.new_link_button, 'setToolTip', 'Ajouter un lien YouTube')
        self.video_tab_strip = VideoTabStrip(self.new_link_button)
        self.queue_tabs = self.video_tab_strip.tabs
        self.queue_tabs.setObjectName('videoTabs')
        self.queue_tabs.setDocumentMode(True)
        self.queue_tabs.setDrawBase(False)
        self.queue_tabs.setExpanding(False)
        bind_text(self.queue_tabs, 'setAccessibleName', 'Vidéos ouvertes')
        self.queue_tabs.tabCloseRequested.connect(self._remove_queued_url)
        self.queue_tabs.currentChanged.connect(self._queue_tab_clicked)
        self.paste_video_link_action = ui(QAction(self.source_panel), text='Coller un lien')
        self.paste_video_link_action.triggered.connect(self._paste_video_link)
        self.retry_analysis_action = ui(QAction(self.source_panel), text='Relancer l’analyse')
        self.retry_analysis_action.triggered.connect(self._retry_analysis)
        self.retry_analysis_action.setEnabled(False)
        self.analysis_diagnostic_action = ui(QAction(self.source_panel), text='Diagnostic de l’analyse')
        self.analysis_diagnostic_action.triggered.connect(self._show_diagnostic)
        self.analysis_diagnostic_action.setEnabled(False)
        for widget in (self.source_panel, self.queue_tabs):
            widget.setContextMenuPolicy(Qt.ContextMenuPolicy.ActionsContextMenu)
            widget.addAction(self.paste_video_link_action)
            widget.addAction(self.retry_analysis_action)
            widget.addAction(self.analysis_diagnostic_action)
        source_layout.addWidget(self.video_tab_strip, 1)
        outer.addWidget(self.source_panel)
        if self.list_view:
            self.source_panel.hide()
            self.resize(1270, 850)
            self.setMinimumSize(780, 570)
            splitter = QSplitter(Qt.Orientation.Vertical)
            self.download_table = DownloadTable(self)
            self.download_table.toolbar.addWidget(self.options_button)
            splitter.addWidget(self.download_table)
            outer.addWidget(splitter, 1)
        else:
            source_layout.addWidget(self.options_button)

        self.video_panel = QWidget()
        video = self._layout(self.video_panel, spacing=0)
        self.selected_video_panel = QFrame()
        self.selected_video_panel.setObjectName('selectedVideo')
        bind_text(self.selected_video_panel, 'setAccessibleName', 'Vidéo sélectionnée')
        layout_class = VideoIdentityLayout if self.list_view else QHBoxLayout
        identity = self._layout(self.selected_video_panel, layout_class, margins=14, spacing=12)
        if self.list_view:
            self.selected_video_thumbnail = DetailThumbnail(self.windowIcon().pixmap(28, 28))
        else:
            self.selected_video_thumbnail = QLabel()
            self.selected_video_thumbnail.setObjectName('tabThumbnail')
            self.selected_video_thumbnail.setFixedSize(96, 54)
            self.selected_video_thumbnail.setAlignment(Qt.AlignmentFlag.AlignCenter)
        identity.addWidget(self.selected_video_thumbnail)
        labels = QVBoxLayout(); labels.setSpacing(3)
        self.selected_video_heading = self._label(text='Vidéo sélectionnée', name='selectedVideoHeading')
        self.selected_video_title = self._label(name='selectedVideoTitle', wrap=True, translate=False)
        self.selected_video_title.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        self.selected_video_title.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Preferred)
        self.selected_video_meta = self._label(name='muted', wrap=True, translate=False)
        self.selected_video_url = self._label(name='muted', translate=False)
        self.selected_video_url.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        if self.list_view:
            self.selected_video_url.setTextInteractionFlags(Qt.TextInteractionFlag.LinksAccessibleByMouse
                                                            | Qt.TextInteractionFlag.LinksAccessibleByKeyboard)
            self.selected_video_url.linkActivated.connect(self._open_video_link)
        for label in (self.selected_video_heading, self.selected_video_title,
                      self.selected_video_meta, self.selected_video_url):
            labels.addWidget(label)
        if self.list_view:
            self.selected_video_description = QPlainTextEdit()
            self.selected_video_description.setObjectName('selectedVideoDescription')
            bind_text(self.selected_video_description, 'setAccessibleName', 'Description de la vidéo sélectionnée')
            self.selected_video_description.setReadOnly(True)
            self.selected_video_description.setMinimumHeight(64)
            self.selected_video_description.hide()
            labels.addWidget(self.selected_video_description, 1)
        identity.addLayout(labels, 1)
        video.addWidget(self.selected_video_panel, 1 if self.list_view else 0)
        self.selected_video_panel.hide()
        video.addWidget(self.scroll_area, 0 if self.list_view else 1)
        if self.list_view:
            self.scroll_area.setMaximumHeight(100)
            self.list_detail_pane = QWidget()
            detail_layout = QVBoxLayout(self.list_detail_pane)
            detail_layout.setContentsMargins(0, 0, 0, 0)
            detail_layout.addWidget(self.video_panel, 1)
            splitter.addWidget(self.list_detail_pane)
            splitter.setCollapsible(0, False)
            splitter.setStretchFactor(0, 3)
            splitter.setStretchFactor(1, 2)
            splitter.setSizes([510, 270])
        else:
            outer.addWidget(self.video_panel, 1)
        self.analysis_panel = QWidget()
        self._analysis_indicator_active = False
        analysis_row = self._layout(self.analysis_panel, QHBoxLayout, spacing=6)
        self.analysis_spinner = CircularActivity()
        analysis_row.addWidget(self.analysis_spinner)
        analysis_row.addWidget(self._label(text='Analyse en cours…', name='analysisTitle'))
        self.analysis_panel.hide()
        body.addWidget(self.analysis_panel)

        self.analysis_result_panel = QWidget()
        result = self._layout(self.analysis_result_panel, spacing=10)
        status_layout = QHBoxLayout(); status_layout.setSpacing(10)
        self.status_label = ClickableLabel()
        self.status_label.setObjectName('status')
        self.status_label.setWordWrap(True)
        self.status_label.setTextFormat(Qt.TextFormat.PlainText)
        self.status_label.setCursor(Qt.CursorShape.PointingHandCursor)
        bind_text(self.status_label, 'setToolTip', 'Afficher le diagnostic de l’analyse')
        bind_text(self.status_label, 'setAccessibleName', 'État de l’analyse — ouvrir le diagnostic')
        self.status_var.changed.connect(self._show_analysis_status)
        self.status_label.clicked.connect(self._show_diagnostic)
        self.analysis_retry_button = self._button('Réessayer l’accès', self._retry_analysis_now, 'retryAnalysis')
        self.analysis_retry_button.hide()
        self.verification_button = self._button(
            'Vérification Google…', self._open_verification, 'verificationAccess')
        self.verification_button.hide()
        status_layout.addWidget(self.status_label, 1)
        status_layout.addWidget(self.analysis_retry_button)
        status_layout.addWidget(self.verification_button)
        result.addLayout(status_layout)
        self.status_label.hide()

        self.video_settings_panel = QFrame(); self.video_settings_panel.setObjectName('options')
        self.video_settings_panel.setSizePolicy(QSizePolicy.Policy.Preferred, QSizePolicy.Policy.Fixed)
        settings = self._layout(self.video_settings_panel, margins=12, spacing=8)
        settings.addWidget(self._label(text='Configuration du téléchargement', name='fieldLabel', wrap=True))
        segmented = QFrame(); segmented.setObjectName('segments')
        segments = self._layout(segmented, QHBoxLayout, margins=4, spacing=4)
        self.mode_group = QButtonGroup(self); self.mode_group.setExclusive(True)
        self.mode_buttons = {}
        for label, mode in [('Tout','video'),('Vidéo seule','video_only'),('Audio seul','audio'),('SRT uniquement','subtitles')]:
            button = ui(QPushButton(), text=label); button.setCheckable(True); button.setObjectName('segment')
            button.clicked.connect(lambda checked=False, value=mode: self._select_mode(value))
            self.mode_buttons[mode] = button; self.mode_group.addButton(button); segments.addWidget(button, 1)
        self.mode_var.changed.connect(self._mark_mode)
        self._mark_mode(self.mode_var.get()); settings.addWidget(segmented)

        self.selection_frame = QWidget(); grid = self._layout(self.selection_frame, QGridLayout, spacing=16)
        selection_policy = self.selection_frame.sizePolicy()
        selection_policy.setVerticalPolicy(QSizePolicy.Policy.Fixed)
        selection_policy.setRetainSizeWhenHidden(True)
        self.selection_frame.setSizePolicy(selection_policy)
        grid.setColumnStretch(0, 1); grid.setColumnStretch(1, 1)
        self.filter_frame = self._field('Résolution', self.height_var, 'height_combo')
        self.subtitle_frame = self._subtitle_field()
        grid.addWidget(self.filter_frame, 0, 0); grid.addWidget(self.subtitle_frame, 0, 1)
        settings.addWidget(self.selection_frame)
        self.settings_row = QWidget(); advanced_row = self._layout(self.settings_row, QHBoxLayout)
        self.settings_row.setSizePolicy(QSizePolicy.Policy.Preferred, QSizePolicy.Policy.Fixed)
        self.advanced_check = ui(QCheckBox(), text='Réglages avancés')
        self.advanced_check.toggled.connect(self._toggle_advanced)
        advanced_row.addWidget(self.advanced_check); advanced_row.addStretch()
        settings.addWidget(self.settings_row)
        self.auto_summary_label = self._label(self.auto_summary_var, name='muted', wrap=True)
        self.auto_summary_label.setSizePolicy(QSizePolicy.Policy.Preferred, QSizePolicy.Policy.Fixed)
        settings.addWidget(self.auto_summary_label)

        self.advanced_panel = QWidget(); advanced = self._layout(self.advanced_panel, QGridLayout, spacing=16)
        advanced.setColumnStretch(0,1); advanced.setColumnStretch(1,1)
        self.container_frame = self._field('Conteneur', self.container_var, 'container_combo')
        self.container_combo['values'] = ('Auto','MP4','MKV','WebM')
        self.video_codec_frame = self._field('Codec vidéo', self.codec_var, 'codec_combo')
        advanced.addWidget(self.container_frame,0,0); advanced.addWidget(self.video_codec_frame,0,1)
        self.audio_frame = QWidget(); audio_grid = self._layout(self.audio_frame, QGridLayout, spacing=16)
        audio_grid.setColumnStretch(0,1); audio_grid.setColumnStretch(1,1)
        audio_grid.addWidget(self._field('Codec audio',self.audio_codec_var,'audio_codec_combo'),0,0)
        audio_grid.addWidget(self._field('Débit audio',self.audio_rate_var,'audio_rate_combo'),0,1)
        self.audio_language_frame = self._audio_language_field()
        audio_grid.addWidget(self.audio_language_frame,1,0,1,2)
        advanced.addWidget(self.audio_frame,1,0,1,2)
        self.clip_frame = QWidget(); clip_grid = self._layout(self.clip_frame, QGridLayout, spacing=8)
        clip_grid.setColumnStretch(0,1)
        clip_header = QHBoxLayout(); clip_header.setSpacing(10)
        clip_title = self._label(text='Extrait du média', name='fieldLabel', wrap=True)
        clip_header.addWidget(clip_title); clip_header.addStretch(1)
        self.keyframe_index_button = IndexProgressButton(
            'Télécharger l’index', self)
        self.keyframe_index_button.setObjectName('quiet')
        self.keyframe_index_button.clicked.connect(self._request_keyframe_index)
        clip_header.addWidget(self.keyframe_index_button)
        clip_grid.addLayout(clip_header,0,0)
        clip_row = QHBoxLayout(); clip_row.setSpacing(12)
        self.clip_start_entry = QLineEdit('00:00:00.000')
        bind_text(self.clip_start_entry, 'setAccessibleName', 'Début de l’extrait')
        self.clip_start_entry.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.clip_start_entry.setFixedWidth(116)
        bind_text(self.clip_start_entry, 'setToolTip', 'Début — secondes ou HH:MM:SS.mmm')
        self.clip_end_entry = QLineEdit('00:00:00.000')
        bind_text(self.clip_end_entry, 'setAccessibleName', 'Fin de l’extrait')
        self.clip_end_entry.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.clip_end_entry.setFixedWidth(116)
        bind_text(self.clip_end_entry, 'setToolTip', 'Fin — secondes ou HH:MM:SS.mmm')
        self.clip_slider = MediaRangeSlider()
        self.clip_slider.setRange(0, 0)
        self.clip_slider.setEnabled(False)
        self.clip_start_entry.setEnabled(False); self.clip_end_entry.setEnabled(False)
        self.clip_start_entry.textChanged.connect(self.clip_start_var.set)
        self.clip_end_entry.textChanged.connect(self.clip_end_var.set)
        self.clip_slider.selectionChanged.connect(self._clip_slider_changed)
        clip_row.addWidget(self.clip_start_entry)
        clip_row.addWidget(self.clip_slider,1)
        clip_row.addWidget(self.clip_end_entry)
        clip_grid.addLayout(clip_row,1,0)
        self.clip_note_label = self._label(self.clip_note_var,name='muted',wrap=True)
        self.clip_note_label.setVisible(bool(self.clip_note_var.get()))
        self.clip_note_var.changed.connect(lambda text:self.clip_note_label.setVisible(bool(text)))
        clip_grid.addWidget(self.clip_note_label,2,0)
        advanced.addWidget(self.clip_frame,2,0,1,2)
        bind_text(self.codec_combo, 'setToolTip', self.format_help_var.get())
        self.format_help_var.changed.connect(self.codec_combo.setToolTip)
        settings.addWidget(self.advanced_panel)
        settings.addStretch()
        self.video_settings_panel.hide()
        result.addWidget(self.video_settings_panel)
        body.addWidget(self.analysis_result_panel)

        self.queued_video_panel = QFrame()
        self.queued_video_panel.setObjectName('queuedVideo')
        queued_video = self._layout(self.queued_video_panel, margins=12, spacing=8)
        queued_heading = QHBoxLayout()
        queued_heading.addWidget(self._label(text='Dans la file', name='fieldLabel'), 1)
        self.edit_queued_video_button = self._button(
            'Modifier', self._edit_queued_video, 'queueAction')
        self._size_download_action(self.edit_queued_video_button)
        queued_heading.addWidget(self.edit_queued_video_button)
        queued_video.addLayout(queued_heading)
        self.queued_video_summary = self._label(name='muted', wrap=True)
        queued_video.addWidget(self.queued_video_summary)
        self.queued_video_panel.hide()
        body.addWidget(self.queued_video_panel)
        # Transport variants remain internal choices, never visible controls.
        self.resolution_combo = Choice(self.resolution_var, self); self.resolution_combo.hide()
        self.audio_combo = Choice(self.audio_var, self); self.audio_combo.hide()

        body.addStretch()
        self.video_action_panel = QFrame()
        self.video_action_panel.setObjectName('videoActions')
        actions = self._layout(self.video_action_panel, QHBoxLayout, margins=16, spacing=10)
        actions.setContentsMargins(16, 8, 16, 8)
        if self.list_view:
            self.list_clip_dialog = QDialog(self)
            bind_text(self.list_clip_dialog, 'setWindowTitle', 'Extrait du média')
            self.list_clip_dialog.resize(650, 200)
            clip_dialog_layout = self._layout(self.list_clip_dialog, margins=16)
            clip_dialog_layout.addWidget(self.clip_frame)
            clip_buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Close)
            bind_text(clip_buttons.button(QDialogButtonBox.StandardButton.Close), 'setText', 'Fermer')
            clip_buttons.rejected.connect(self.list_clip_dialog.reject)
            clip_dialog_layout.addWidget(clip_buttons)
        self.size_label = self._label(self.size_var, name='size', wrap=True)
        actions.addWidget(self.size_label, 1)
        actions.addStretch()
        self.download_button = self._button(
            'Télécharger', self._start_download, 'primary', QStyle.StandardPixmap.SP_ArrowDown)
        self._size_download_action(self.download_button)
        self.download_button.setEnabled(False)
        actions.addWidget(self.download_button)
        self.video_action_panel.hide()
        video.addWidget(self.video_action_panel)
        self.folder_var.set(self.preferences.value('folder',self.folder_var.get()))

        self.pause_download_button = self._button('', self._pause_download, 'pauseTransfer')
        self._size_download_action(self.pause_download_button, compact=True)
        bind_text(self.pause_download_button, 'setAccessibleName', 'Mettre le téléchargement en pause')
        bind_text(self.pause_download_button, 'setToolTip', 'Mettre en pause')
        self.pause_download_button.hide()
        self.cancel_button = self._button('Annuler', self._cancel_download, 'cancelTransfer')
        bind_text(self.cancel_button, 'setAccessibleName', 'Annuler le téléchargement en cours')
        self.cancel_button.setEnabled(False)
        self.cancel_button.hide()
        self.open_download_button = ui(QPushButton(), text='Ouvrir')
        self.open_download_button.setIcon(self.style().standardIcon(QStyle.StandardPixmap.SP_DialogOpenButton))
        self.open_download_button.hide()
        self.download_result_menu = QMenu(self.open_download_button)
        self.open_result_action = add_translated(self.download_result_menu, 'addAction', 'Ouvrir le fichier', self._open_download_result)
        self.reveal_result_action = add_translated(self.download_result_menu, 'addAction', 'Afficher dans le dossier', self._reveal_download_result)
        self.retry_subtitles_action = add_translated(self.download_result_menu, 'addAction', 'Réessayer les sous-titres', self._retry_failed_subtitles)
        self.retry_subtitles_action.setVisible(False)
        self.download_result_menu.addSeparator()
        add_translated(self.download_result_menu, 'addAction', 'Télécharger à nouveau', self._download_again)
        self.open_download_button.setMenu(self.download_result_menu)
        self.transfer_panel = DownloadQueueRow()
        self.transfer_panel.setObjectName('activeDownload')
        self.transfer_panel.clicked.connect(self._open_active_download_tab)
        self.transfer_thumbnail_label = self.transfer_panel.thumbnail
        self.transfer_title_label = self.transfer_panel.title
        self.resume_download_button = self._button(
            'Reprendre', self._resume_failed_download, 'primary')
        bind_text(self.resume_download_button, 'setAccessibleName', 'Reprendre le téléchargement interrompu')
        self.skip_download_button = self._button(
            'Annuler', self._skip_failed_download, 'cancelPausedTransfer')
        bind_text(self.skip_download_button, 'setAccessibleName', 'Annuler le téléchargement interrompu')
        bind_text(self.skip_download_button, 'setToolTip', 'Annuler et passer au téléchargement suivant')
        self.resume_download_button.hide()
        self.skip_download_button.hide()
        for button in (self.resume_download_button, self.skip_download_button,
                       self.cancel_button, self.open_download_button):
            self._size_download_action(button)
        for button in (self.resume_download_button, self.skip_download_button, self.pause_download_button,
                       self.cancel_button, self.open_download_button):
            self.transfer_panel.actions.addWidget(button)
        self.transfer_status_label = QueueLabel(name='muted')
        self.transfer_panel.status.addWidget(self.transfer_status_label)
        self.download_progress = Progress(); self.download_progress.setObjectName('downloadProgress')
        self.transfer_panel.status.addWidget(self.download_progress)
        self.transfer_panel.hide()
        self.progress_text_var.changed.connect(self._update_transfer_visibility)
        self.progress_detail_var.changed.connect(self._update_download_tooltip)

        self.download_panel = QFrame(); self.download_panel.setObjectName('footer')
        bottom = self._layout(self.download_panel, margins=16, spacing=8)
        bottom.setContentsMargins(16, 10, 16, 12)
        rate_label = ui(QLabel(), text='Débit max')
        self.rate_limit_spinbox = RateSpinBox()
        bind_text(self.rate_limit_spinbox, 'setAccessibleName', 'Limite de téléchargement')
        bind_text(self.rate_limit_spinbox, 'setSpecialValueText', 'Illimité')
        self.rate_limit_spinbox.setKeyboardTracking(False)
        self.rate_limit_spinbox.setMinimumWidth(100)
        self.rate_unit_combo = ArrowCombo()
        bind_text(self.rate_unit_combo, 'setAccessibleName', 'Unité de la limite de téléchargement')
        self.rate_unit_combo.addItems(RATE_UNITS)
        unit = str(self.preferences.value('download_rate_unit', 'Mbit/s'))
        self.rate_unit_combo.setCurrentText(unit if unit in RATE_UNITS else 'Mbit/s')
        self._display_rate_limit(self.rate_unit_combo.currentSourceText())
        bind_text(self.rate_limit_spinbox, 'setToolTip', 'Limite commune au téléchargement en cours et à toute la file. Validez avec Appliquer ou Entrée. 0 = aucune limite.')
        self.apply_rate_button = self._button('Appliquer', self._apply_rate_limit, 'quiet')
        bind_text(self.apply_rate_button, 'setAccessibleName', 'Appliquer la limite à toute la file')
        self.unlimited_rate_button = self._button(
            'Illimité', lambda: self.rate_limit_spinbox.setValue(0), 'quiet')
        bind_text(self.unlimited_rate_button, 'setAccessibleName', 'Supprimer la limite de téléchargement')
        bind_text(self.unlimited_rate_button, 'setToolTip', 'Supprimer immédiatement la limite, même pendant un téléchargement.')
        if self.list_view:
            rate_controls = QWidget()
            rate_row = self._layout(rate_controls, QHBoxLayout, spacing=6)
            rate_row.setContentsMargins(8, 0, 0, 0)
            self.rate_limit_spinbox.setFixedWidth(90)
            self.rate_unit_combo.setFixedWidth(116)
        else:
            rate_row = QHBoxLayout()
            rate_row.setSpacing(6)
            rate_row.addWidget(self._label(text='Téléchargements', name='sectionTitle'))
            rate_row.addStretch(1)
        rate_row.addWidget(rate_label)
        rate_row.addWidget(self.rate_limit_spinbox)
        rate_row.addWidget(self.rate_unit_combo)
        rate_row.addWidget(self.apply_rate_button)
        rate_row.addWidget(self.unlimited_rate_button)
        if not self.list_view:
            bottom.addLayout(rate_row)

        self.download_queue_panel = QFrame(); self.download_queue_panel.setObjectName('transferCard')
        scheduled = self._layout(self.download_queue_panel, margins=9, spacing=5)
        scheduled.addWidget(self.transfer_panel)
        self.download_queue_list = QListWidget()
        self.download_queue_list.setObjectName('scheduledDownloads')
        bind_text(self.download_queue_list, 'setAccessibleName', 'Téléchargements programmés dans l’ordre')
        self.download_queue_list.setResizeMode(QListWidget.ResizeMode.Adjust)
        self.download_queue_list.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.download_queue_list.verticalScrollBar().rangeChanged.connect(self._align_download_rows)
        for signal in (self.download_queue_list.itemClicked, self.download_queue_list.itemActivated):
            signal.connect(lambda item: self._open_download_tab(item.data(Qt.ItemDataRole.UserRole)))
        self.download_queue_list.itemSelectionChanged.connect(self._highlight_transfer_row)
        scheduled.addWidget(self.download_queue_list)
        self.download_queue_empty_label = self._label(
            text='Aucun téléchargement programmé.', name='muted', wrap=True)
        scheduled.addWidget(self.download_queue_empty_label)
        bottom.addWidget(self.download_queue_panel)
        self.shutdown_after_downloads_check = ui(QCheckBox(), text='Éteindre le PC à la fin' if self.list_view
                                                      else 'Arrêter après la fin des téléchargements')
        if self.list_view:
            self.download_panel.setParent(self)
            self.download_panel.hide()
            bind_text(self.shutdown_after_downloads_check, 'setAccessibleName', 'Éteindre le PC à la fin des téléchargements')
            bind_text(self.shutdown_after_downloads_check, 'setToolTip', 'Éteindre le PC après le dernier téléchargement réussi. Décompte annulable de 30 secondes.')
            self.download_table.set_rate_controls(rate_controls, self.shutdown_after_downloads_check)
            self.download_table.refresh([])
        else:
            bottom.addWidget(self.shutdown_after_downloads_check, alignment=Qt.AlignmentFlag.AlignLeft)
            outer.addWidget(self.download_panel)
            self._render_scheduled_downloads()
        self._apply_theme()
        self._align_download_rows()
        # Reserve enough room for every live status before the window is shown.
        # The layout must not move as the button text changes during retries.
        original_text = self.keyframe_index_button.text()
        index_statuses = (
            original_text,
            'Connexion à YouTube',
            'Téléchargement de l’index',
            'Télécharger l’index · ≈ 999.9 Mo',
            'Analyse de l’index',
            'Réessayer l’index',
        )
        status_widths = []
        for status in index_statuses:
            bind_text(self.keyframe_index_button, 'setText', status)
            status_widths.append(self.keyframe_index_button.sizeHint().width())
        bind_text(self.keyframe_index_button, 'setText', original_text)
        self.keyframe_index_button.setFixedWidth(max(status_widths))
        QApplication.styleHints().colorSchemeChanged.connect(self._system_theme_changed)

    def _selected_scheduled_url(self):
        item = self.download_queue_list.currentItem()
        return item.data(Qt.ItemDataRole.UserRole) if item else None

    def _render_scheduled_downloads(self, selected_url=None):
        selected = selected_url or self._selected_scheduled_url()
        if selected not in self.queued_downloads:
            selected = next(iter(self.queued_downloads), None)
        items = self.download_queue_list
        items.blockSignals(True)
        existing = {}
        for index in range(items.count() - 1, -1, -1):
            item = items.item(index)
            url = item.data(Qt.ItemDataRole.UserRole)
            row = items.itemWidget(item)
            if self.queued_downloads.get(url) is row.job:
                existing[url] = item
            else:
                items.takeItem(index)
        count = len(self.queued_downloads)
        can_prioritize = self._can_prioritize_download()
        for position, (url, job) in enumerate(self.queued_downloads.items(), 1):
            item = existing.get(url)
            target = position - 1
            if item is None:
                item = self._insert_scheduled_download(target, url, job)
            else:
                source = items.row(item)
                # Every earlier position has already been placed.
                if source > target:
                    model = items.model()
                    root = items.rootIndex()
                    model.moveRow(root, source, root, target)
            row = items.itemWidget(item)
            row.up_button.setEnabled(position > 1 or can_prioritize)
            row.down_button.setEnabled(position < count)
            item.setData(Qt.ItemDataRole.AccessibleTextRole, f'{position}. {row.description}')
            if url == selected:
                items.setCurrentItem(item)
        if self._transfer_busy() and self._viewing_transfer():
            items.clearSelection()
        items.blockSignals(False)
        self._highlight_transfer_row()
        items.setVisible(bool(count))
        self._fit_download_queue_height()
        if selected_url and items.currentItem():
            items.scrollToItem(items.currentItem())
        self._update_transfer_visibility()
        if not self.transfer_panel.isHidden():
            self._set_transfer_thumbnail(self.thumbnail_cache.get(self._transfer_video_id()))

    def _insert_scheduled_download(self, index, url, job):
        title = job['title'] or self._compact_queue_url(url)
        summary = self._queued_download_summary(job)
        video_id = str((job.get('data') or {}).get('id') or youtube_video_id(url))
        row = DownloadQueueRow(title)
        row.job = job
        row.description = f'{title} · {summary}'
        self._set_thumbnail(row.thumbnail, self.thumbnail_cache.get(video_id))
        if 'progress' in job:
            row.status.addWidget(ui(QueueLabel(name='fieldLabel'), text='En pause'))
            progress = job['progress']
            bar = Progress()
            if isinstance(progress, (int, float)) and 0 <= progress <= 100:
                bar.configure(value=progress)
            row.status.addWidget(bar)
        else:
            row.status.addWidget(ui(QueueLabel(name='fieldLabel'), text='En attente'))
            row.status.addWidget(ui(QueueLabel(name='muted'), text=summary))
        for text, label, delta in (('↑', 'Monter', -1), ('↓', 'Descendre', 1)):
            button = self._button(
                text, lambda checked=False, delta=delta: self._move_scheduled_download(url, delta), 'queueArrow')
            bind_text(button, 'setAccessibleName', label)
            bind_text(button, 'setToolTip', label)
            button.setFixedHeight(19)
            row.order.addWidget(button)
            if delta < 0:
                row.up_button = button
            else:
                row.down_button = button
        row.remove_button = self._button(
            'Retirer', lambda checked=False: self._remove_scheduled_download(url), 'queueAction')
        self._size_download_action(row.remove_button)
        row.actions.addWidget(row.remove_button)
        item = QListWidgetItem()
        item.setSizeHint(QSize(0, row.minimumHeight()))
        item.setData(Qt.ItemDataRole.UserRole, url)
        item.setData(DOWNLOAD_VIDEO_ID_ROLE, video_id)
        tooltip = message('%1\n%2\n%3\n%4', title, message(summary),
                          message('Dossier : %1', job['request'][1]), url)
        bind_text(item, 'setToolTip', tooltip)
        bind_text(row, 'setToolTip', tooltip)
        self.download_queue_list.insertItem(index, item)
        self.download_queue_list.setItemWidget(item, row)
        return item

    def _highlight_transfer_row(self):
        selected = (self._transfer_busy() and self._viewing_transfer()
                    and not self.download_queue_list.selectedItems())
        row = self.transfer_panel
        if row.property('selected') != selected:
            row.setProperty('selected', selected)
            row.style().unpolish(row)
            row.style().polish(row)
            row.update()

    def _fit_download_queue_height(self):
        items = self.download_queue_list
        if items.count():
            row_height = items.sizeHintForRow(0)
            # Keep the video settings usable when the window is at its minimum height.
            visible_rows = 2 if self.height() >= self.minimumHeight() + row_height else 1
            height = min(items.count(), visible_rows) * row_height
            if items.minimumHeight() != height or items.maximumHeight() != height:
                items.setFixedHeight(height)
                # Propagate the new minimum through the footer before the next
                # paint, including when the selected-video banner also resizes.
                for widget in (self.download_queue_panel, self.download_panel, self.centralWidget()):
                    widget.layout().invalidate()
                    widget.updateGeometry()
                self.layout().invalidate()
                self.layout().activate()
                self.centralWidget().layout().activate()
                self.download_panel.layout().activate()
                self.download_queue_panel.layout().activate()

    def _align_download_rows(self, *_):
        items = self.download_queue_list
        scrollbar = items.verticalScrollBar()
        gutter = scrollbar.sizeHint().width()
        items.setViewportMargins(0, 0, gutter if scrollbar.maximum() == 0 else 0, 0)
        self.transfer_panel.layout().setContentsMargins(6, 4, 6 + gutter, 4)

    def _update_transfer_visibility(self, *_):
        busy = self._transfer_busy()
        if self.list_view:
            self.open_download_button.setVisible(busy and bool(self.download_result_folder) and self.download_thread is None)
            self._update_download_tooltip()
            return
        visible = bool(busy or self.progress_text_var.get())
        self.transfer_panel.setVisible(visible)
        self.download_progress.setVisible(busy or self.download_progress.value() > 0)
        self.transfer_status_label.setVisible(not self.download_retry_pending)
        self.open_download_button.setVisible(bool(self.download_result_folder) and self.download_thread is None)
        if visible:
            url = self.last_download_request[0] if self.last_download_request else self._current_download_url()
            title = ((self.active_download_job or {}).get('title')
                     or (self.active_download_data or {}).get('title')
                     or self._compact_queue_url(url))
            state = '' if self.download_retry_pending else self.progress_text_var.get() or 'En cours'
            self.transfer_title_label.setText(title)
            self.transfer_title_label.setToolTip(title)
            bind_text(self.transfer_status_label, 'setText', state)
            self._update_download_tooltip()
        count = self.download_queue_list.count()
        self.download_queue_empty_label.setVisible(not count and not visible)

    def _transfer_video_id(self):
        url = self.last_download_request[0] if self.last_download_request else self._current_download_url()
        return str((self.active_download_data or {}).get('id') or youtube_video_id(url))

    def _set_transfer_thumbnail(self, content):
        self._set_thumbnail(self.transfer_thumbnail_label, content)

    def _update_download_queue_thumbnail(self, video_id, content):
        if video_id == self._transfer_video_id():
            self._set_transfer_thumbnail(content)
        items = self.download_queue_list
        for index in range(items.count()):
            item = items.item(index)
            if item.data(DOWNLOAD_VIDEO_ID_ROLE) == video_id:
                thumbnail = items.itemWidget(item).thumbnail
                self._set_thumbnail(thumbnail, content)

    def _update_download_tooltip(self, *_):
        lines = self.progress_detail_var.get().splitlines()[-8:]
        state = self.progress_text_var.get() if self.list_view else self.transfer_status_label.text()
        if state:
            lines.insert(0, state)
        tooltip = '<qt>' + '<br>'.join(escape(line) for line in lines) + '</qt>' if lines else ''
        if self.list_view:
            row = self.download_table.rows.get(self.active_queue_url)
            if row and self.active_download_job:
                bind_text(row, 'setToolTip', 4, tooltip)
            return
        bind_text(self.download_progress, 'setToolTip', tooltip)
        bind_text(self.transfer_status_label, 'setToolTip', tooltip)

    def eventFilter(self, watched, event):
        event_type = event.type()
        drag_events = (QEvent.Type.DragEnter, QEvent.Type.DragMove, QEvent.Type.Drop)
        if (event_type in (*drag_events, QEvent.Type.KeyPress)
                and isinstance(watched, QWidget) and watched.window() is self):
            if event_type in drag_events:
                mime = event.mimeData()
                partials = [item.toLocalFile() for item in mime.urls()
                            if item.isLocalFile() and item.toLocalFile().lower().endswith('.part')]
                if len(partials) == 1 and event.possibleActions() & Qt.DropAction.CopyAction:
                    event.setDropAction(Qt.DropAction.CopyAction)
                    event.accept()
                    if event.type() == QEvent.Type.Drop:
                        self._resume_dropped_partial(partials[0])
                    return True
                url = youtube_link_from_mime(mime)
                if url:
                    if event.type() == QEvent.Type.Drop:
                        self._url_link_submitted(url)
                    event.acceptProposedAction()
                    return True
            elif (not isinstance(watched, (QLineEdit, QPlainTextEdit, QAbstractSpinBox))
                    and event.matches(QKeySequence.StandardKey.Paste)):
                if self._paste_video_link():
                    event.accept()
                    return True
        if (event_type in (QEvent.Type.Resize, QEvent.Type.Show) and hasattr(self, 'scroll_area')
                and watched is self.scroll_area.viewport()):
            self.after_idle(self._update_scroll_cue)
        return super().eventFilter(watched, event)

    def _update_scroll_cue(self, *_):
        if not hasattr(self, 'scroll_fade'):
            return
        viewport = self.scroll_area.viewport()
        bar = self.scroll_area.verticalScrollBar()
        visible = bar.maximum() > 0 and bar.value() < bar.maximum()
        self.scroll_fade.setVisible(visible)
        if visible:
            height = self.scroll_fade.height()
            self.scroll_fade.setGeometry(0, max(0, viewport.height()-height), viewport.width(), height)
            self.scroll_fade.raise_()

    def _theme_changed(self, value):
        self.theme_value = value
        self.theme_actions[value].setChecked(True)
        self.preferences.setValue('theme', value)
        self._apply_theme()

    def _system_theme_changed(self, *_):
        if not self._disposed and self.theme_value == 'Système':
            self._apply_theme()

    def _apply_theme(self):
        system_dark = QApplication.styleHints().colorScheme() == Qt.ColorScheme.Dark
        dark = self.theme_value == 'Sombre' or (self.theme_value == 'Système' and system_dark)
        bg, surface, ink, muted, line, accent, tint = ('#16191f','#20252d','#edf1f7','#a9b5c6','#394351','#90b4ff','#293c5c') if dark else ('#f6f8fb','#ffffff','#202b3a','#58677b','#dce3ed','#2563cf','#edf3ff')
        palette = QPalette()
        for role,color in [(QPalette.ColorRole.Window,bg),(QPalette.ColorRole.WindowText,ink),(QPalette.ColorRole.Base,surface),(QPalette.ColorRole.AlternateBase,bg),(QPalette.ColorRole.Text,ink),(QPalette.ColorRole.Button,surface),(QPalette.ColorRole.ButtonText,ink),(QPalette.ColorRole.Highlight,accent),(QPalette.ColorRole.HighlightedText,'#14233a' if dark else '#ffffff'),(QPalette.ColorRole.ToolTipBase,surface),(QPalette.ColorRole.ToolTipText,ink)]:palette.setColor(role,QColor(color))
        palette.setColor(QPalette.ColorRole.Link, QColor(accent))
        self.setPalette(palette)
        icon = QPixmap(20,20); icon.fill(Qt.GlobalColor.transparent)
        painter = QPainter(icon); painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        painter.setPen(QPen(QColor(ink),1.6))
        for y in (5,10,15):painter.drawLine(3,y,17,y)
        painter.end(); self.options_button.setIcon(QIcon(icon))
        pause_icon = QPixmap(16, 16)
        pause_icon.fill(Qt.GlobalColor.transparent)
        painter = QPainter(pause_icon)
        for x in (4, 9):
            painter.fillRect(x, 3, 3, 10, QColor(ink))
        painter.end()
        self.pause_download_button.setIcon(QIcon(pause_icon))
        self.setStyleSheet(f'''
            QWidget {{font-family:'Segoe UI';font-size:13px;color:{ink};}}
            QMainWindow, QWidget#content {{background:{bg};}}
            QFrame#videoActions {{background:{bg};border-top:1px solid {line};}}
            QFrame#sourcePanel, QFrame#footer {{background:{surface};border:0;}}
            QFrame#sourcePanel {{border-bottom:1px solid {line};}}
            QFrame#footer {{border-top:1px solid {line};}}
            QLabel {{background:transparent;}}
            QLabel#brand {{font-size:17px;font-weight:600;}}
            QLabel#sectionTitle {{font-size:16px;font-weight:700;}}
            QLabel#muted {{color:{muted};}}
            QLabel#analysisTitle {{font-size:14px;font-weight:600;color:{ink};}}
            QLabel#size {{color:{muted};}}
            QLabel#fieldLabel {{font-weight:500;}}
            QLabel#status {{color:{muted};}}
            QLabel#dependencyAlert {{background:#8f1d2c;color:#ffffff;border-radius:9px;padding:14px;font-size:15px;font-weight:600;}}
            QFrame#options, QFrame#queuedVideo {{background:{surface};border:1px solid {line};border-radius:10px;}}
            QFrame#transferCard {{background:{bg};border:1px solid {line};border-radius:8px;}}
            QFrame#scrollFade {{border:0;background:qlineargradient(x1:0,y1:0,x2:0,y2:1,stop:0 rgba(0,0,0,0),stop:0.58 {bg},stop:1 {bg});}}
            QLineEdit,QComboBox {{background:{surface};border:1px solid {line};border-radius:7px;padding:9px 11px;min-height:19px;selection-background-color:{accent};}}
            QLineEdit:focus,QComboBox:focus {{border:1px solid {accent};}}
            QDoubleSpinBox {{background:{surface};border:1px solid {line};border-radius:7px;padding:5px;min-height:19px;}}
            QDoubleSpinBox:focus {{border-color:{accent};}}
            QComboBox {{padding-right:28px;}}
            QComboBox::drop-down {{border:0;width:25px;}}
            QComboBox::down-arrow {{image:none;}}
            QComboBox QAbstractItemView {{background:{surface};color:{ink};selection-background-color:{tint};selection-color:{ink};border:1px solid {line};outline:0;}}
            QComboBox QAbstractItemView::item {{min-height:28px;padding:3px 8px;}}
            QTabBar#videoTabs {{background:transparent;}}
            QTabBar#videoTabs::tab {{background:{surface};border:2px solid {line};border-bottom:4px solid {line};border-radius:8px;padding:6px 10px 6px 8px;margin-right:6px;}}
            QTabBar#videoTabs::tab:selected {{background:{tint};border-color:{accent};}}
            QFrame#selectedVideo {{background:{bg};border:0;border-left:4px solid {accent};border-bottom:1px solid {line};}}
            QLabel#selectedVideoHeading {{color:{accent};font-size:12px;font-weight:600;}}
            QLabel#selectedVideoTitle {{font-size:16px;font-weight:600;}}
            QFrame#videoTabLeftFade {{background:qlineargradient(x1:0,y1:0,x2:1,y2:0,stop:0 {surface},stop:0.15 {surface},stop:1 transparent);border:0;}}
            QFrame#videoTabRightFade {{background:qlineargradient(x1:0,y1:0,x2:1,y2:0,stop:0 transparent,stop:0.85 {surface},stop:1 {surface});border:0;}}
            QPushButton#videoTabClose {{background:{bg};color:{muted};border:1px solid {line};border-radius:4px;padding:0;min-height:0;font-size:15px;}}
            QPushButton#videoTabClose:hover {{background:{tint};color:{ink};border-color:{line};}}
            QPushButton#tabOverflow {{background:{tint};color:{accent};border:1px solid transparent;border-radius:14px;padding:0;min-height:0;font-size:13px;font-weight:600;}}
            QPushButton#tabOverflow:hover, QPushButton#tabOverflow:focus {{border-color:{accent};}}
            QLabel#tabThumbnail {{background:{tint};border-radius:4px;}}
            QLabel#tabTitle {{font-size:15px;font-weight:600;}}
            QLabel#tabState {{font-size:12px;color:{muted};}}
            QFrame#activeDownload {{background:{surface};border-radius:7px;}}
            QFrame#activeDownload[selected="true"] {{background:{tint};}}
            QListWidget#scheduledDownloads {{background:{surface};border:0;outline:0;}}
            QListWidget#scheduledDownloads::item {{border-bottom:1px solid {line};}}
            QListWidget#scheduledDownloads::item:selected {{background:{tint};color:{ink};}}
            QTreeWidget#videoList {{background:{surface};alternate-background-color:{bg};border:1px solid {line};border-radius:6px;selection-background-color:{tint};selection-color:{ink};}}
            QTreeWidget#videoList::item {{min-height:29px;border-bottom:1px solid {line};padding:1px 4px;}}
            QTreeWidget#videoList::item:selected {{background:{tint};color:{ink};}}
            QTreeWidget#videoList::indicator:unchecked {{background:{surface};border:1px solid {muted};border-radius:3px;}}
            QTreeWidget#videoList QHeaderView::section {{border-right:1px solid {line};}}
            QFrame#streamChoices {{background:{surface};border:1px solid {line};border-radius:7px;}}
            QListWidget#defaultSubtitleLanguages {{background:{surface};alternate-background-color:{bg};border:1px solid {line};}}
            QListWidget#defaultSubtitleLanguages::item {{min-height:22px;padding:2px 4px;}}
            QListWidget#defaultSubtitleLanguages::item:selected {{background:{tint};color:{ink};}}
            QFrame#downloadTable {{background:{bg};}}
            QPlainTextEdit#selectedVideoDescription {{background:{surface};border:1px solid {line};padding:6px;}}
            QPushButton {{background:{surface};border:1px solid {line};border-radius:7px;padding:9px 14px;min-height:19px;}}
            QPushButton:hover {{background:{tint};border-color:{accent};}}
            QPushButton#addVideo {{background:{tint};color:{accent};border-color:{line};padding:0;font-size:32px;font-weight:500;}}
            QPushButton#addVideo:hover, QPushButton#addVideo:focus {{border-color:{accent};}}
            QPushButton#optionsButton {{background:transparent;border:1px solid transparent;border-radius:4px;padding:0;min-height:0;}}
            QPushButton#optionsButton:hover {{background:{tint};border-color:transparent;}}
            QPushButton#optionsButton:focus {{border-color:{ink};}}
            QMenu {{background:{surface};color:{ink};border:1px solid {line};padding:5px;}}
            QMenu::item {{padding:7px 28px 7px 20px;border-radius:4px;}}
            QMenu::item:selected {{background:{tint};}}
            QMenu::separator {{height:1px;background:{line};margin:5px;}}
            QPushButton:focus {{border-color:{accent};}}
            QPushButton:disabled {{color:{muted};background:{bg};border-color:{line};}}
            QPushButton#primary {{background:{accent};color:{'#14233a' if dark else '#ffffff'};border:1px solid transparent;}}
            QPushButton#primary:disabled {{background:{line};color:{muted};}}
            QPushButton[downloadAction="true"] {{padding:0 12px;min-height:38px;font-weight:600;}}
            QPushButton#pauseTransfer {{padding:0;}}
            QPushButton#queueArrow {{padding:0;min-height:0;font-size:14px;}}
            QPushButton#quiet {{background:transparent;border:1px solid transparent;color:{muted};}}
            QPushButton#quiet:hover {{background:{tint};color:{ink};}}
            QPushButton#quiet:disabled {{color:{'#738297' if dark else '#8793a5'};}}
            QPushButton#retryAnalysis, QPushButton#verificationAccess {{background:{tint};border-color:{accent};color:{ink};font-weight:600;}}
            QFrame#segments {{background:{bg};border-radius:8px;}}
            QPushButton#segment {{background:transparent;border:1px solid transparent;padding:9px 6px;color:{muted};}}
            QPushButton#segment:checked {{background:{surface};border-color:{line};color:{accent};font-weight:600;}}
            QPushButton#segment:disabled {{color:{muted};}}
            QCheckBox {{spacing:9px;padding:3px 0;}}
            QCheckBox::indicator {{width:17px;height:17px;}}
            QProgressBar {{background:{line};border:0;border-radius:3px;text-align:center;color:{ink};font-size:11px;font-weight:600;}}
            QProgressBar::chunk {{background:{accent};border-radius:3px;}}
            QScrollArea {{border:0;background:{bg};}}
            QScrollBar:vertical {{background:{surface};width:14px;margin:0;border-left:1px solid {line};}}
            QScrollBar::handle:vertical {{background:{muted};min-height:42px;border-radius:5px;margin:2px 3px;}}
            QScrollBar::handle:vertical:hover {{background:{accent};}}
            QScrollBar::add-line:vertical,QScrollBar::sub-line:vertical {{height:0;border:0;background:transparent;}}
            QScrollBar::add-page:vertical,QScrollBar::sub-page:vertical {{background:transparent;}}
            QDialog {{background:{bg};}}
            QTableWidget,QPlainTextEdit {{background:{surface};border:1px solid {line};border-radius:6px;selection-background-color:{tint};selection-color:{ink};}}
            QHeaderView::section {{background:{bg};padding:8px;border:0;border-bottom:1px solid {line};font-weight:500;}}
        ''')

    def _mark_mode(self, mode):
        if mode in self.mode_buttons: self.mode_buttons[mode].setChecked(True)

    @staticmethod
    def _format_clip_milliseconds(value):
        hours, remainder = divmod(max(0, int(value)), 3_600_000)
        minutes, remainder = divmod(remainder, 60_000)
        seconds, milliseconds = divmod(remainder, 1000)
        return f'{hours:02}:{minutes:02}:{seconds:02}.{milliseconds:03}'

    def _clip_slider_changed(self, start, end):
        start_text = self._format_clip_milliseconds(start)
        end_text = self._format_clip_milliseconds(end)
        self.clip_start_entry.setText(start_text)
        self.clip_end_entry.setText(end_text)
        self.clip_start_var.set(start_text)
        self.clip_end_var.set(end_text)

    def _select_mode(self, mode):
        self.mode_var.set(mode); self._mode_changed()

    def _toggle_advanced(self, checked):
        self.settings_var.set('manual' if checked else 'auto'); self._settings_changed()

    def _apply_settings_layout(self):
        mode = self.mode_var.get(); advanced = self.settings_var.get() == 'manual'
        video = mode in ('video','video_only'); audio = mode in ('video','audio')
        self.advanced_check.blockSignals(True); self.advanced_check.setChecked(advanced); self.advanced_check.blockSignals(False)
        self.filter_frame.setVisible(video); self.subtitle_frame.setVisible(video or mode == 'subtitles')
        # Preserve this row as an empty spacer in audio mode so every description
        # keeps exactly the same vertical position between tabs.
        # Compute it here, after the theme has supplied the controls' padding.
        self.selection_frame.setFixedHeight(max(
            self.filter_frame.sizeHint().height(), self.subtitle_frame.sizeHint().height()))
        self.selection_frame.setVisible(True)
        self.settings_row.setVisible(mode != 'subtitles')
        self.size_label.setVisible(mode != 'subtitles')
        self.advanced_panel.setVisible(advanced and mode != 'subtitles')
        self.container_frame.setVisible(advanced and mode != 'subtitles')
        self.video_codec_frame.setVisible(video and advanced)
        self.audio_frame.setVisible(audio and advanced)
        self.clip_frame.setVisible(self.list_view or (advanced and mode != 'subtitles'))
        self._update_auto_summary()
        self.auto_summary_label.setVisible(bool(self.auto_summary_var.get()))
        self._mark_mode(mode)

    def _fit_window_to_content(self):
        # The scroll area handles advanced settings; the window does not jump or resize.
        self.centralWidget().layout().activate()

    def _bind_events(self):
        self.rate_limit_spinbox.valueChanged.connect(self._rate_limit_changed)
        self.rate_unit_combo.currentIndexChanged.connect(
            lambda _index: self._rate_unit_changed(self.rate_unit_combo.currentSourceText()))
        self.url_var.changed.connect(self._on_url_changed)
        if self.list_view:
            self.container_combo.activated.connect(self._list_set_container)
        else:
            self.container_combo.activated.connect(lambda _:self._refresh_resolution_choices())
        self.resolution_combo.activated.connect(lambda _:self._update_download_button_state())
        self.audio_combo.activated.connect(lambda _:self._audio_changed())
        for combo in (self.audio_codec_combo,self.audio_rate_combo):
            combo.activated.connect(lambda _:self._audio_options_changed())
        for combo in (self.height_combo,self.codec_combo):
            combo.activated.connect(lambda _:self._refresh_resolution_choices())
        self.clip_start_var.changed.connect(self._clip_changed)
        self.clip_end_var.changed.connect(self._clip_changed)
        self.clip_start_entry.editingFinished.connect(self._apply_clip_time_entries)
        self.clip_end_entry.editingFinished.connect(self._apply_clip_time_entries)

    def _display_rate_limit(self, unit):
        factor, decimals = RATE_UNITS[unit]
        blocked = self.rate_limit_spinbox.blockSignals(True)
        self.rate_limit_spinbox.setDecimals(decimals)
        self.rate_limit_spinbox.setRange(0, MAX_RATE_BPS / factor)
        self.rate_limit_spinbox.setSingleStep(MIN_RATE_BPS / factor)
        self.rate_limit_spinbox.setValue(round(self.download_rate_bps / factor, decimals))
        self.rate_limit_spinbox.blockSignals(blocked)

    def _prefill_clipboard(self):
        url = QApplication.clipboard().text().strip()
        if not self.url_var.get() and is_youtube_url(url):
            self._url_link_submitted(url)

    def _paste_video_link(self):
        url = youtube_link_from_mime(QApplication.clipboard().mimeData())
        if not url:
            return False
        self._url_link_submitted(url)
        return True

    def _request_new_link(self):
        return QInputDialog.getText(self, tr('Ajouter une vidéo'), tr('Lien YouTube'))

    def _request_partial_link(self, filename, suggested_url):
        return QInputDialog.getText(
            self, tr('Reprendre un fichier partiel'),
            filename + '\n' + tr('Collez le lien YouTube d’origine pour identifier le flux :'),
            text=suggested_url)

    def _set_analysis_indicator(self, active):
        self._analysis_indicator_active = active
        self._update_video_view()

    def _update_video_view(self):
        url = self.url_var.get().strip()
        active_job = self.active_download_job if url == self.active_queue_url else None
        job = active_job or self.queued_downloads.get(url)
        show_summary = job is not None and (active_job is not None or self.editing_queued_url != url)
        self.queued_video_panel.setVisible(show_summary)
        active = self._analysis_indicator_active
        ready = not active and self.analysis_data is not None
        if self.list_view:
            self.queued_video_panel.hide()
            self.video_settings_panel.hide()
            self.analysis_panel.setVisible(active and not show_summary)
            self.analysis_result_panel.setVisible(not active and not show_summary)
            self._update_list_details()
            return
        self.analysis_panel.setVisible(active and not show_summary)
        self.analysis_result_panel.setVisible(not active and not show_summary)
        self.video_settings_panel.setVisible(ready and not show_summary)
        self.video_action_panel.setVisible(ready and not show_summary)
        if show_summary:
            bind_text(self.queued_video_summary, 'setText', self._queued_download_summary(job, detailed=True))
            self.edit_queued_video_button.setVisible(
                active_job is None and ready and not job.get('partial_path'))

    def _show_analysis_status(self, text):
        bind_text(self.status_label, 'setText', text)
        self.status_label.setVisible(
            bool(text) and text != 'Analyse terminée' and not text.startswith('En direct · '))
        if self.list_view and hasattr(self, 'list_clip_dialog'):
            self._update_list_details()

    def _update_list_details(self):
        show_status = (self._analysis_indicator_active or not self.status_label.isHidden()
                       or not self.analysis_retry_button.isHidden() or not self.verification_button.isHidden())
        self.scroll_area.setVisible(not self.completed_selection and show_status)

    def _open_list_clip(self):
        url = self.queue_view_url
        if not self._list_start_editing(url):
            return
        self.settings_var.set('manual')
        self._apply_settings_layout()
        self._update_clip_index_controls()
        self.list_clip_dialog.exec()
        if url == self.queue_view_url:
            self._save_download_checkpoint()

    def _thumbnail_pixmap(self, content, size):
        image = QPixmap()
        if content and image.loadFromData(content):
            return image.scaled(size, Qt.AspectRatioMode.KeepAspectRatio,
                                Qt.TransformationMode.SmoothTransformation)
        width = min(28, size.height())
        return self.windowIcon().pixmap(width, width)

    def _set_thumbnail(self, label, content):
        if getattr(label, '_thumbnail_content', False) is content:
            return
        label.setPixmap(self._thumbnail_pixmap(content, label.size()))
        label._thumbnail_content = content

    def _set_selected_video_identity(self, url, title, state, data, content, *, placeholder=False):
        self.selected_video_panel.setVisible(bool(url))
        self.selected_video_panel.setProperty('videoUrl', url)
        bind_text(self.selected_video_heading, 'setText', 'Vidéo sélectionnée · ' + state if state else 'Vidéo sélectionnée')
        if placeholder:
            bind_text(self.selected_video_title, 'setText', title if url else '')
            bind_text(self.selected_video_title, 'setToolTip', title)
        else:
            unbind_text(self.selected_video_title, 'setText')
            unbind_text(self.selected_video_title, 'setToolTip')
            self.selected_video_title.setText(title if url else '')
            self.selected_video_title.setToolTip(title)
        bind_text(self.selected_video_title, 'setAccessibleName', 'Titre de la vidéo sélectionnée')
        video_id = youtube_video_id(url)
        link = 'https://youtu.be/' + video_id if video_id else url
        if self.list_view and is_youtube_url(link):
            target = escape(link, quote=True)
            self.selected_video_url.setTextFormat(Qt.TextFormat.RichText)
            self.selected_video_url.setText(f'<a href="{target}">{target}</a>')
        else:
            self.selected_video_url.setTextFormat(Qt.TextFormat.PlainText)
            self.selected_video_url.setText(link)
        self.selected_video_url.setToolTip(url)
        details = [str(data.get('channel') or data.get('uploader') or '').strip()]
        duration = data.get('duration')
        if isinstance(duration, (int, float)) and duration > 0:
            minutes, seconds = divmod(int(duration), 60)
            hours, minutes = divmod(minutes, 60)
            details.append(f'{hours}:{minutes:02}:{seconds:02}' if hours else f'{minutes}:{seconds:02}')
        self.selected_video_meta.setText(' · '.join(value for value in details if value))
        if self.list_view:
            self.selected_video_thumbnail.set_content(content)
            description = data.get('description')
            text = description.strip() if isinstance(description, str) else ''
            if data and not text:
                text = ('Description indisponible. Relancez l’analyse pour la récupérer.'
                        if 'description' not in data and not self.completed_selection
                        else 'Aucune description disponible.')
            widget = self.selected_video_description
            if getattr(self, '_video_description_url', None) != url or getattr(self, '_video_description_text', None) != text:
                if description:
                    unbind_text(widget, 'setPlainText')
                    widget.setPlainText(text)
                else:
                    bind_text(widget, 'setPlainText', text)
                self._video_description_url, self._video_description_text = url, text
            widget.setVisible(bool(url and text))
        else:
            self._set_thumbnail(self.selected_video_thumbnail, content)
        self.selected_video_panel.setToolTip('\n'.join((title, self.selected_video_meta.text(), url)))
        self._update_selected_video_layout()

    @staticmethod
    def _open_video_link(link):
        if is_youtube_url(link):
            QDesktopServices.openUrl(QUrl(link))

    def _update_selected_video_layout(self):
        if self.list_view:
            self.selected_video_meta.setVisible(bool(self.selected_video_meta.text()))
            return
        compact = self.height() < self.minimumHeight() + 200
        self.selected_video_meta.setVisible(not compact and bool(self.selected_video_meta.text()))
        self.selected_video_url.setVisible(not compact)
        if compact == getattr(self, '_selected_video_compact', None):
            return
        self._selected_video_compact = compact
        self.selected_video_panel.layout().setContentsMargins(*(12, 7, 12, 7) if compact else (14, 14, 14, 14))
        self.selected_video_title.setMaximumHeight(
            2 * self.selected_video_title.fontMetrics().lineSpacing() if compact else 16777215)
        thumbnail = self.selected_video_thumbnail
        content = getattr(thumbnail, '_thumbnail_content', None)
        thumbnail.setFixedSize(64, 36) if compact else thumbnail.setFixedSize(96, 54)
        thumbnail._thumbnail_content = False
        self._set_thumbnail(thumbnail, content)

    def _set_video_tab_preview(self, index, title, state, content, *, analyzing, downloading):
        side = QTabBar.ButtonPosition.LeftSide
        preview = self.queue_tabs.tabButton(index, side)
        if preview is None:
            preview = VideoTabPreview()
            self.queue_tabs.setTabButton(index, side, preview)
        appearance = (title, state, analyzing, downloading)
        if getattr(preview, '_appearance', None) != appearance:
            preview.set_title(title)
            bind_text(preview.state, 'setText', state)
            preview.state.setVisible(not downloading)
            preview.spinner.setVisible(analyzing)
            preview.download_progress.setVisible(downloading)
            preview._appearance = appearance
        if downloading:
            self._update_tab_download_progress(index)
        self._set_thumbnail(preview.thumbnail, content)

    def _add_video_tab_close(self, index, url):
        holder = QWidget()
        holder.setFixedSize(20, 63)
        layout = self._layout(holder, spacing=0)
        button = self._button('×', lambda: self._close_video_tab(url), 'videoTabClose')
        button.setFixedSize(18, 18)
        bind_text(button, 'setAccessibleName', 'Fermer l’onglet')
        bind_text(button, 'setToolTip', 'Fermer l’onglet')
        layout.addWidget(button, 0, Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignTop)
        layout.addStretch()
        self.queue_tabs.setTabButton(index, self.queue_tabs.ButtonPosition.RightSide, holder)

    def _update_tab_download_progress(self, index=None):
        tabs = self.queue_tabs
        if index is None:
            for index in range(tabs.count()):
                if tabs.tabData(index)[0] == self.active_queue_url:
                    break
            else:
                return
        preview = tabs.tabButton(index, QTabBar.ButtonPosition.LeftSide)
        if preview is not None and not preview.download_progress.isHidden():
            preview.download_progress.setRange(0, self.download_progress.maximum())
            preview.download_progress.setValue(self.download_progress.value())

    def _show_download_result_actions(self, paths, folder):
        self.download_results = list(paths)
        self.download_result_folder = folder
        self.open_result_action.setEnabled(bool(self.download_results))
        self.reveal_result_action.setEnabled(bool(folder))
        self._update_transfer_visibility()
        self._update_download_button_state()

    def _set_subtitle_retry_available(self, available):
        self.retry_subtitles_action.setVisible(bool(available))

    def _clear_download_result_actions(self):
        if not self._transfer_busy():
            self.download_results = []
            self.download_result_folder = ''
        bind_text(self.download_button, 'setText', 'Télécharger')
        self._update_transfer_visibility()

    def _request_download_folder(self):
        url = self.url_var.get().strip()
        folder = QFileDialog.getExistingDirectory(
            self, tr('Choisir le dossier de téléchargement'), self.folder_var.get())
        if folder:
            if self.url_var.get().strip() == url:
                self.folder_var.set(folder)
            self.preferences.setValue('folder',folder)
        return folder

    def _ask_download_verification(self):
        dialog = QMessageBox(self)
        dialog.setIcon(QMessageBox.Icon.Warning)
        bind_text(dialog, 'setWindowTitle', 'Vérification YouTube requise')
        bind_text(dialog, 'setText', 'YouTube a refusé l’accès nécessaire.')
        bind_text(dialog, 'setInformativeText', 'Ouvrir maintenant la vérification Google ? Une fois le contrôle terminé, '
            'la même opération reprendra automatiquement.')
        verify = add_translated(dialog, 'addButton', 'Vérifier maintenant', QMessageBox.ButtonRole.AcceptRole)
        add_translated(dialog, 'addButton', 'Plus tard', QMessageBox.ButtonRole.RejectRole)
        dialog.setDefaultButton(verify)
        dialog.exec()
        return dialog.clickedButton() is verify

    def _subtitle_language_labels(self, codes):
        available = (self.active_download_tracks if self._transfer_busy()
                     and self.active_download_tracks is not None else self.subtitle_tracks)
        tracks = {
            track.get('code'): track.get('label', track.get('code', ''))
            for track in available
        }
        return [tracks.get(code, code) for code in (codes or ())]

    def _ask_subtitle_retry(
            self, error, clip=None, completed_languages=(), missing_languages=()):
        dialog = QMessageBox(self)
        dialog.setIcon(QMessageBox.Icon.Warning)
        if completed_languages:
            bind_text(dialog, 'setWindowTitle', 'Sous-titres partiellement téléchargés')
            bind_text(dialog, 'setText', 'Certaines langues de sous-titres ont été téléchargées, mais d’autres manquent.')
            obtained = '\n'.join(
                '• ' + label
                for label in self._subtitle_language_labels(completed_languages))
            missing = '\n'.join(
                '• ' + label
                for label in self._subtitle_language_labels(missing_languages))
            detail = 'Langues obtenues :\n' + obtained
            if missing:
                detail += '\n\nLangues à réessayer :\n' + missing
            detail += '\n\nRéessayer uniquement les langues manquantes ?'
        else:
            bind_text(dialog, 'setWindowTitle', 'Sous-titres non téléchargés')
            bind_text(dialog, 'setText', 'La vidéo a été téléchargée, mais aucun fichier de sous-titres n’a été obtenu.')
            detail = 'Réessayer uniquement les sous-titres ?'
        if clip:
            detail += (
                ' Lorsqu’ils seront reçus, ils seront automatiquement découpés '
                'et recalés sur l’extrait.')
        if error:
            detail += '\n\n' + str(error)
        bind_text(dialog, 'setInformativeText', detail)
        retry = add_translated(dialog, 'addButton', 'Réessayer les sous-titres', QMessageBox.ButtonRole.AcceptRole)
        add_translated(dialog, 'addButton', 'Plus tard', QMessageBox.ButtonRole.RejectRole)
        dialog.setDefaultButton(retry)
        dialog.exec()
        return dialog.clickedButton() is retry

    def _show_diagnostic(self):
        dialog = QDialog(self); bind_text(dialog, 'setWindowTitle', 'Diagnostic YouTube'); dialog.resize(760,510)
        layout = self._layout(dialog,margins=18)
        text = QPlainTextEdit(self._diagnostic_text()); text.setReadOnly(True); layout.addWidget(text)
        row = QHBoxLayout(); row.addStretch()
        row.addWidget(self._button('Copier',lambda:QApplication.clipboard().setText(text.toPlainText())))
        row.addWidget(self._button('Fermer',dialog.close)); layout.addLayout(row)
        dialog.setAttribute(Qt.WidgetAttribute.WA_DeleteOnClose); dialog.show()

    def _show_dependencies(self):
        if self.dependency_dialog is not None:
            self.dependency_dialog.show(); self.dependency_dialog.raise_(); self.dependency_dialog.activateWindow(); return
        dialog = self.dependency_dialog = QDialog(self)
        bind_text(dialog, 'setWindowTitle', 'Composants requis'); dialog.resize(840,500)
        frame = self._layout(dialog,margins=18)
        self.dependency_alert_label = self._label(
            text='Des composants indispensables manquent. Installez-les pour continuer.',
            name='dependencyAlert',wrap=True)
        frame.addWidget(self.dependency_alert_label)
        frame.addWidget(self._label(text='Composants du programme',name='brand'))
        self.dependency_tree = QTableWidget(0,3)
        bind_text(self.dependency_tree, 'setHorizontalHeaderLabels', ['Composant','État','Détail'])
        self.dependency_tree.horizontalHeader().setSectionResizeMode(0,QHeaderView.ResizeMode.ResizeToContents)
        self.dependency_tree.horizontalHeader().setSectionResizeMode(1,QHeaderView.ResizeMode.ResizeToContents)
        self.dependency_tree.horizontalHeader().setSectionResizeMode(2,QHeaderView.ResizeMode.Stretch)
        self.dependency_tree.verticalHeader().hide(); self.dependency_tree.setShowGrid(False)
        self.dependency_tree.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.dependency_tree.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.dependency_tree.itemSelectionChanged.connect(self._dependency_selected)
        frame.addWidget(self.dependency_tree,1)
        frame.addWidget(self._label(self.dependency_detail_var,wrap=True))
        frame.addWidget(self._label(text='Les composants manquants sont installés dans _dependencies.',name='muted',wrap=True))
        row = QHBoxLayout()
        self.dependency_check_button = self._button('Vérifier',self._check_dependencies)
        self.dependency_install_button = self._button('Installer / réparer les éléments manquants',self._install_dependencies)
        self.dependency_update_button = self._button('Rechercher les mises à jour',self._dependency_update_clicked)
        self.dependency_cancel_button = self._button('Annuler',self.dependency_manager.cancel.set,'quiet')
        for button in (self.dependency_check_button,self.dependency_install_button,
                       self.dependency_update_button,self.dependency_cancel_button):row.addWidget(button)
        frame.addLayout(row); self._render_dependencies(); dialog.show()
        if not self.dependency_states and not self.dependency_busy:self._check_dependencies()

    def _dependency_selected(self):
        row = self.dependency_tree.currentRow()
        if 0 <= row < len(self.dependency_states):
            state = self.dependency_states[row]
            self.dependency_detail_var.set(state.detail + ('\n'+state.path if state.path else ''))

    def _render_dependencies(self):
        if self.dependency_dialog is None:return
        tree = self.dependency_tree; tree.blockSignals(True); tree.setRowCount(len(self.dependency_states))
        for row,state in enumerate(self.dependency_states):
            update = self.dependency_updates.get(state.key)
            status = 'Mise à jour' if update else ('Prêt' if state.ok else 'À réparer')
            detail = state.detail + (f'  →  {update[1]}' if update else '')
            for col,text in enumerate((state.name,status,detail)):
                item = ui(QTableWidgetItem(), text=text); bind_text(item, 'setToolTip', text); tree.setItem(row,col,item)
        tree.blockSignals(False)
        active = self.analysis_running or self.download_thread is not None
        missing_states = [state for state in self.dependency_states if not state.ok]
        self.dependency_alert_label.setVisible(bool(missing_states))
        self.dependency_dialog.setModal(bool(missing_states))
        bind_text(self.dependency_dialog, 'setWindowTitle', 'Composants requis manquants' if missing_states else 'Dépendances')
        self.dependency_check_button.setEnabled(not self.dependency_busy and not active)
        missing = any(not state.ok and state.repairable for state in self.dependency_states)
        self.dependency_install_button.setEnabled(missing and not self.dependency_busy and not active)
        bind_text(self.dependency_update_button, 'setText', f'Mettre à jour ({len(self.dependency_updates)})' if self.dependency_updates
            else 'Rechercher les mises à jour')
        self.dependency_update_button.setEnabled(not self.dependency_busy and not active)
        self.dependency_cancel_button.setEnabled(self.dependency_busy)
