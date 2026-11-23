"""Qt translations at the presentation boundary; model values remain stable.

The engine predates localization and emits French status/format labels. Catalog
templates translate those complete labels without changing persisted selections,
format IDs, or requests. Video metadata and editable text must stay unbound.
"""
from pathlib import Path
import re
import sys
import weakref
import xml.etree.ElementTree as ET
from functools import lru_cache
from dataclasses import dataclass

from PySide6.QtCore import QObject, QCoreApplication, QLibraryInfo, QLocale, QSignalBlocker, QTranslator, Signal


CONTEXT = 'Downloader'
LANGUAGES = {'fr': 'Français', 'en': 'English'}
TRANSLATIONS = Path(getattr(sys, '_MEIPASS', Path(__file__).resolve().parent)) / 'translations'
_manager = None
_bindings = {}
_templates = None


@dataclass(frozen=True)
class UiMessage:
    """A translated template whose plain arguments are preserved verbatim."""
    source: str
    args: tuple


def message(source, *args):
    return UiMessage(source, args)


def _render(source):
    if isinstance(source, UiMessage):
        if not source.args:
            return display_text(source.source)
        args = (_render(arg) if isinstance(arg, UiMessage) else arg for arg in source.args)
        return tr(source.source, *args)
    if isinstance(source, (list, tuple)):
        return [display_text(text) for text in source]
    return display_text(source)


def unbind_text(target, method):
    if entry := _bindings.get(id(target)):
        for key in list(entry[1]):
            if key[0] == method:
                del entry[1][key]


def _legacy_templates():
    global _templates
    if _templates is None:
        # Only catalog messages with fixed words can match legacy formatted
        # output. Anchoring prevents accidental replacement inside URLs/paths.
        root = ET.parse(TRANSLATIONS / 'downloader_en.ts').getroot()
        _templates = []
        for message in root.iter('message'):
            source = message.findtext('source', '')
            parts = re.split(r'(%[1-9]\d*)', source)
            if len(parts) == 1:
                continue
            literals = ''.join(parts[::2])
            if not any(c.isalpha() for c in literals):
                continue
            pattern = ''.join('(.*?)' if part.startswith('%') and part[1:].isdigit()
                              else re.escape(part) for part in parts)
            _templates.append((len(literals), re.compile(pattern, re.DOTALL), source, parts[1::2]))
        _templates.sort(key=lambda item: item[0], reverse=True)
    return _templates


def tr(source, *args):
    """Translate a catalog message and substitute Qt-style %1 placeholders."""
    translated = QCoreApplication.translate(CONTEXT, source)
    values = {str(index): str(value) for index, value in enumerate(args, 1)}
    return re.sub(r'%([1-9]\d*)', lambda match: values.get(match[1], match[0]), translated)


@lru_cache(maxsize=4096)
def display_text(source, _depth=0):
    """Render an application-owned label, never a model key or video title."""
    if not source or _manager is None or _manager.language == 'fr':
        return source
    if re.match(r'^(?:[A-Za-z]:[\\/]|\\\\|https?://)', source):
        return source
    translated = tr(source)
    if translated != source:
        return translated
    if _depth >= 8:
        return source
    if source.startswith('• '):
        return '• ' + display_text(source[2:], _depth + 1)
    for _, pattern, template, slots in _legacy_templates():
        match = pattern.fullmatch(source)
        if match:
            values = dict(zip(slots, match.groups()))
            return re.sub(r'%[1-9]\d*', lambda item: display_text(values[item[0]], _depth + 1), tr(template))
    # Format summaries consist of independent labels separated by these marks.
    # Do not split spaces: unknown words and external error details stay intact.
    parts = re.split(r'(\n| · | — |, | : | → | / |  ▾)', source)
    if len(parts) > 1:
        return ''.join(part if index % 2 else display_text(part, _depth + 1)
                       for index, part in enumerate(parts))
    language = re.fullmatch(r'(.+) (\([a-zA-Z-]{2,12}\))', source)
    if language:
        return display_text(language[1], _depth + 1) + ' ' + language[2]
    return source


def bind_text(target, method, *args):
    """Set and remember a translated Qt property (including indexed item text)."""
    prefix, source = args[:-1], args[-1]
    key = id(target)
    if key not in _bindings:
        _bindings[key] = (weakref.ref(target, lambda _ref, key=key: _bindings.pop(key, None)), {})
    _bindings[key][1][(method, prefix)] = source
    getattr(target, method)(*prefix, _render(source))


def ui(target, **properties):
    """Bind initial properties while constructing a widget or action."""
    for property_name, source in properties.items():
        bind_text(target, 'set' + property_name[0].upper() + property_name[1:], source)
    return target


def add_translated(target, method, text, *args):
    result = getattr(target, method)(text, *args)
    bind_text(result, 'setTitle' if method == 'addMenu' else 'setText', text)
    return result


def retranslate_bindings():
    for key, (reference, properties) in list(_bindings.items()):
        target = reference()
        if target is None:
            continue
        try:
            owner = target if isinstance(target, QObject) else next(
                (getattr(target, name)() for name in ('treeWidget', 'listWidget', 'tableWidget')
                 if hasattr(target, name)), None)
            blocker = QSignalBlocker(owner) if owner is not None else None
            for (method, prefix), source in list(properties.items()):
                getattr(target, method)(*prefix, _render(source))
            if blocker is not None:
                blocker.unblock()
        except RuntimeError:
            # A Qt parent can delete its C++ children before Python collects
            # their wrappers (for example, when clearing a download list).
            _bindings.pop(key, None)


class LanguageManager(QObject):
    languageChanged = Signal(str)

    def __init__(self, app):
        super().__init__(app)
        self.app = app
        self.language = None
        self._translators = []

    def set_language(self, language):
        language = language if isinstance(language, str) and language in LANGUAGES else 'fr'
        if language == self.language:
            return
        translators = []
        if language == 'en':
            translator = QTranslator(self)
            if not translator.load(str(TRANSLATIONS / 'downloader_en.qm')):
                translator.deleteLater()
                raise RuntimeError('Cannot load English Qt translations')
            translators.append(translator)
        qt = QTranslator(self)
        if qt.load('qtbase_' + language, QLibraryInfo.path(QLibraryInfo.LibraryPath.TranslationsPath)):
            translators.insert(0, qt)
        else:
            qt.deleteLater()
        for previous in self._translators:
            self.app.removeTranslator(previous)
            previous.deleteLater()
        self._translators = translators
        self.language = language
        display_text.cache_clear()
        for translator in translators:
            self.app.installTranslator(translator)
        locale = QLocale('fr_FR' if language == 'fr' else 'en_GB')
        QLocale.setDefault(locale)
        for window in self.app.topLevelWidgets():
            window.setLocale(locale)
        retranslate_bindings()
        self.languageChanged.emit(language)


def language_manager():
    global _manager
    if _manager is None:
        app = QCoreApplication.instance()
        if app is None:
            raise RuntimeError('Create QApplication before setting the language')
        _manager = LanguageManager(app)
    return _manager
