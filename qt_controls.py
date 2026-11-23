"""Small Qt bindings for downloader state; no Tk dependency."""
from PySide6.QtCore import QObject, Signal, QSignalBlocker, Qt, QEvent
from PySide6.QtGui import QFont, QPainter, QPen, QPalette
from PySide6.QtWidgets import QApplication, QComboBox, QProgressBar, QMessageBox
from qt_i18n import bind_text, display_text, language_manager


SOURCE_TEXT_ROLE = Qt.ItemDataRole.UserRole + 100


class Value(QObject):
    changed = Signal(str)

    def __init__(self, value=""):
        super().__init__()
        self._value = str(value)

    def get(self):
        return self._value

    def set(self, value):
        value = str(value)
        if value != self._value:
            self._value = value
            self.changed.emit(value)


class ArrowCombo(QComboBox):
    """A visible, palette-aware arrow with the stylesheet-based window theme."""
    def __init__(self, parent=None):
        super().__init__(parent)
        language_manager().languageChanged.connect(self._retranslate)

    def sourceText(self, index):
        source = self.itemData(index, SOURCE_TEXT_ROLE)
        return source if source is not None else self.itemText(index)

    def currentSourceText(self):
        return self.sourceText(self.currentIndex())

    def findText(self, text, flags=Qt.MatchFlag.MatchExactly | Qt.MatchFlag.MatchCaseSensitive):
        for index in range(self.count()):
            if self.sourceText(index) == text:
                return index
        return super().findText(text, flags)

    def setCurrentText(self, text):
        self.setCurrentIndex(self.findText(text))

    def addItem(self, text, userData=None):
        super().addItem(display_text(text), userData)
        self.setItemData(self.count() - 1, text, SOURCE_TEXT_ROLE)

    def addItems(self, texts):
        for text in texts:
            self.addItem(text)

    def _retranslate(self, _language):
        with QSignalBlocker(self):
            for index in range(self.count()):
                text = display_text(self.sourceText(index))
                super().setItemText(index, text)
                self.setItemData(index, text, Qt.ItemDataRole.ToolTipRole)

    def paintEvent(self, event):
        super().paintEvent(event)
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        painter.setPen(QPen(self.palette().color(QPalette.ColorRole.Text), 1.5))
        x, y = self.width() - 17, self.height() // 2
        painter.drawLine(x-4, y-2, x, y+2)
        painter.drawLine(x, y+2, x+4, y-2)
        painter.end()


class Choice(ArrowCombo):
    """Choice list backed by shared state. Only user activation invokes business handlers."""
    def __init__(self, variable, parent=None):
        super().__init__(parent)
        self.variable = variable
        self.key_handler = None
        self.setMinimumWidth(0)
        self.setMinimumContentsLength(10)
        self.setSizeAdjustPolicy(QComboBox.SizeAdjustPolicy.AdjustToMinimumContentsLengthWithIcon)
        self.setMaxVisibleItems(14)
        self.currentIndexChanged.connect(lambda _index: variable.set(self.currentSourceText()))
        variable.changed.connect(self._from_value)
        self.view().installEventFilter(self)

    def _from_value(self, value):
        with QSignalBlocker(self):
            self.setCurrentIndex(self.findText(value))
        bind_text(self, 'setToolTip', value)

    def __getitem__(self, key):
        if key != 'values': raise KeyError(key)
        return tuple(self.sourceText(i) for i in range(self.count()))

    def __setitem__(self, key, values):
        if key != 'values': raise KeyError(key)
        previous = self.variable.get()
        with QSignalBlocker(self):
            self.clear()
            self.addItems([str(value) for value in values])
            self.setCurrentIndex(self.findText(previous))
            for i in range(self.count()):
                self.setItemData(i, self.itemText(i), Qt.ItemDataRole.ToolTipRole)
                if 'langue d’origine' in self.sourceText(i):
                    font = QFont(self.font()); font.setBold(True)
                    self.setItemData(i, font, Qt.ItemDataRole.FontRole)

    def current(self, index=None):
        if index is None: return self.currentIndex()
        self.setCurrentIndex(index)
        self.variable.set(self.currentSourceText())

    def _handle_key(self, event):
        if self.key_handler and not event.modifiers() & (Qt.KeyboardModifier.ControlModifier | Qt.KeyboardModifier.AltModifier):
            key = 'BackSpace' if event.key() == Qt.Key.Key_Backspace else event.text()
            if self.key_handler(event.text(), key) == 'break':
                if self.currentIndex() >= 0:
                    index = self.model().index(self.currentIndex(), 0)
                    self.view().setCurrentIndex(index)
                    self.view().scrollTo(index)
                return True
        return False

    def keyPressEvent(self, event):
        if self._handle_key(event): event.accept()
        else: super().keyPressEvent(event)

    def eventFilter(self, watched, event):
        if watched is self.view() and event.type() == QEvent.Type.KeyPress and self._handle_key(event):
            return True
        return super().eventFilter(watched, event)

    def showPopup(self):
        super().showPopup()
        # Keep long subtitle names readable without enlarging the application window.
        screen = self.screen().availableGeometry()
        width = min(max(self.width(), min(560, self.view().sizeHintForColumn(0) + 38)), screen.width()-32)
        popup = self.view().window()
        popup.resize(width, popup.height())
        popup.move(min(max(screen.left()+8, popup.x()), screen.right()-width-8), popup.y())


class Progress(QProgressBar):
    def __init__(self, parent=None, compact=False):
        super().__init__(parent)
        self.setRange(0, 100)
        self.setTextVisible(not compact)
        self.setFormat("%p %")
        self.setFixedHeight(6 if compact else 18)

    def configure(self, mode=None, value=None):
        if mode is not None: self.setRange(0, 0 if mode == 'indeterminate' else 100)
        if value is not None: self.setValue(round(value))

    def start(self, interval=12):
        self.setRange(0, 0)

    def stop(self):
        if self.maximum() == 0: self.setRange(0, 100)

    def __setitem__(self, key, value):
        if key != 'value': raise KeyError(key)
        self.setValue(round(value))


class Messages:
    @staticmethod
    def showerror(title, text): QMessageBox.critical(QApplication.activeWindow(), display_text(title), display_text(text))
    @staticmethod
    def showwarning(title, text): QMessageBox.warning(QApplication.activeWindow(), display_text(title), display_text(text))
    @staticmethod
    def showinfo(title, text): QMessageBox.information(QApplication.activeWindow(), display_text(title), display_text(text))


messagebox = Messages()
