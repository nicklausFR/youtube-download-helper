"""Exercise live Qt translation without changing model data or user preferences."""
import copy
import os
os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')
from pathlib import Path
import re
import sys
import tempfile
import unittest
from unittest.mock import patch
import xml.etree.ElementTree as ET

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from PySide6.QtCore import QCoreApplication, QSettings
from PySide6.QtTest import QSignalSpy
from PySide6.QtWidgets import QDialogButtonBox, QListWidget, QLineEdit, QDialog
from qt_i18n import CONTEXT, TRANSLATIONS, display_text, language_manager, tr
from qt_window import application
from qt_download_list import FormatDefaultsDialog
from youtube_downloader_gui import YoutubeDownloaderApp
from test_features import DATA


URL = 'https://youtu.be/urFIHf5coxE'


class Translations(unittest.TestCase):
    def setUp(self):
        application()
        self.temporary = tempfile.TemporaryDirectory(prefix='yd-i18n-')
        self.addCleanup(self.temporary.cleanup)
        self.preferences = QSettings(str(Path(self.temporary.name) / 'settings.ini'), QSettings.Format.IniFormat)
        self.preferences.setValue('language', 'fr')
        patcher = patch('qt_window.QSettings', return_value=self.preferences)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.qt_errors = []
        patcher = patch('sys.excepthook', side_effect=lambda *error: self.qt_errors.append(error))
        patcher.start()
        self.addCleanup(patcher.stop)
        patcher = patch.object(YoutubeDownloaderApp, '_request_tab_preview')
        patcher.start()
        self.addCleanup(patcher.stop)
        self.windows = []

    def tearDown(self):
        for window in self.windows:
            window.download_thread = None
            window.dispose()
        language_manager().set_language('fr')
        application().processEvents()
        self.assertFalse(self.qt_errors, str(self.qt_errors))

    def window(self, list_view=False):
        window = YoutubeDownloaderApp(list_view=list_view)
        self.windows.append(window)
        window._url_link_submitted(URL)
        if window.analysis_job:
            window.after_cancel(window.analysis_job)
            window.analysis_job = None
        data = copy.deepcopy(DATA)
        # These are also valid translation sources. They must remain metadata.
        data.update(title='Télécharger', channel='Français', description='Aucun')
        window.tab_analysis_data[(URL, window.access_var.get())] = data
        window._process_analysis(data)
        window._render_download_queue()
        return window

    def test_switching_both_views_preserves_selection_metadata_and_worker(self):
        for list_view in (False, True):
            with self.subTest(list_view=list_view):
                window = self.window(list_view)
                window._set_selected_subtitles(['en-orig', 'fr'])
                before = copy.deepcopy(window._capture_tab_settings())
                selection = copy.deepcopy(window._selection())
                spy = QSignalSpy(window.height_combo.currentIndexChanged)
                worker = window.download_thread = object()
                window.language_actions['en'].trigger()
                self.assertEqual(window.windowTitle(), 'YouTube Downloader')
                self.assertEqual(window.download_button.text(), 'Download')
                self.assertIn('fps', window.height_combo.itemText(0))
                self.assertIn('i/s', window.height_combo.sourceText(0))
                self.assertEqual(window.selected_video_title.text(), 'Télécharger')
                self.assertIn('Français', window.selected_video_meta.text())
                self.assertIn('Télécharger', window.queue_tabs.accessibleTabName(0))
                self.assertIn('Analyzed', window.queue_tabs.accessibleTabName(0))
                self.assertEqual(window._capture_tab_settings(), before)
                self.assertEqual(window._selection(), selection)
                self.assertIs(window.download_thread, worker)
                self.assertEqual(spy.count(), 0)
                if list_view:
                    self.assertEqual(window.selected_video_description.toPlainText(), 'Aucun')
                    self.assertEqual(window.download_table.rows[URL].text(0), 'Télécharger')
                    self.assertEqual(window.download_table.rows[URL].text(3), 'Analyzed')
                    self.assertEqual(window.download_table.tree.headerItem().text(1), 'Duration')
                window.language_actions['fr'].trigger()
                self.assertEqual(window.windowTitle(), 'Téléchargeur YouTube')
                self.assertEqual(window.download_button.text(), 'Télécharger')
                self.assertEqual(window._capture_tab_settings(), before)
                self.assertEqual(window.selected_video_title.text(), 'Télécharger')

    def test_language_is_saved_and_loaded_for_next_window(self):
        window = self.window()
        window._set_language('en')
        self.assertEqual(self.preferences.value('language'), 'en')
        language_manager().set_language('fr')
        next_window = self.window(True)
        self.assertEqual(next_window.windowTitle(), 'YouTube Downloader')
        self.assertTrue(next_window.language_actions['en'].isChecked())
        self.assertFalse(next_window.language_actions['fr'].isChecked())
        self.assertEqual([a.text() for a in next_window.options_menu.actions() if not a.isSeparator()],
                         ['Theme', 'Language', 'Dependencies'])

    def test_translated_choice_still_selects_the_same_format_and_rate_unit(self):
        window = self.window()
        window._set_language('en')
        window.height_combo.current(1)
        window._refresh_resolution_choices()
        self.assertEqual(window.height_var.get(), '1080p · 30 i/s')
        self.assertEqual(window._selection()['format'], '1080+source129')
        window.rate_unit_combo.setCurrentText('Mo/s')
        self.assertEqual(window.rate_unit_combo.currentSourceText(), 'Mo/s')
        self.assertEqual(window.rate_unit_combo.currentText(), 'MB/s')
        self.assertEqual(window.rate_unit_combo.itemText(window.rate_unit_combo.currentIndex()), 'MB/s')
        window.rate_limit_spinbox.setValue(2)
        window._apply_rate_limit()
        self.assertEqual(window.download_rate_bps, 2_000_000)

    def test_new_statuses_and_default_dialog_are_translated(self):
        window = self.window(True)
        window._set_language('en')
        window.status_var.set('Réponse incomplète de YouTube : seulement 360p reçu. Vous pouvez réessayer.')
        self.assertEqual(window.status_label.text(), 'Incomplete YouTube response: only 360p received. You can retry.')
        dialog = FormatDefaultsDialog(window, window.download_defaults)
        self.assertEqual(dialog.windowTitle(), 'Default settings')
        self.assertEqual(dialog.video.itemText(1), '360p if available')
        dialog.audio.setCurrentIndex(dialog.audio.findData('fr'))
        self.assertEqual(dialog.audio.itemText(dialog.audio.currentIndex()), 'French if available')
        choices = dialog.choices()
        window._set_language('fr')
        self.assertEqual(dialog.windowTitle(), 'Réglages par défaut')
        self.assertEqual(dialog.audio.itemText(dialog.audio.currentIndex()), 'Français si disponible')
        self.assertEqual(dialog.choices(), choices)

    def test_subtitle_picker_searches_english_names_and_keeps_language_codes(self):
        window = self.window()
        window._set_language('en')
        def inspect(dialog):
            self.assertEqual(dialog.windowTitle(), 'Choose subtitles')
            dialog.findChild(QLineEdit).setText('French')
            items = dialog.findChild(QListWidget)
            self.assertEqual(items.count(), 1)
            self.assertIn('French', items.item(0).text())
            return QDialog.DialogCode.Rejected
        with patch.object(QDialog, 'exec', inspect):
            window._open_subtitle_picker()

    def test_catalogs_and_qt_standard_buttons(self):
        language_manager().set_language('en')
        sources = set()
        for entry in ET.parse(TRANSLATIONS / 'downloader_en.ts').getroot().iter('message'):
            source, translated = entry.findtext('source'), entry.findtext('translation')
            self.assertNotIn(source, sources)
            sources.add(source)
            self.assertEqual(sorted(re.findall(r'%[1-9]\d*', source)), sorted(re.findall(r'%[1-9]\d*', translated)))
            self.assertEqual(QCoreApplication.translate(CONTEXT, source), translated, source)
        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Cancel)
        self.assertEqual(buttons.button(QDialogButtonBox.StandardButton.Cancel).text().replace('&', ''), 'Cancel')
        language_manager().set_language('fr')
        application().processEvents()
        self.assertEqual(buttons.button(QDialogButtonBox.StandardButton.Cancel).text().replace('&', ''), 'Annuler')

    def test_nested_legacy_labels_and_placeholder_arguments(self):
        language_manager().set_language('en')
        for source, expected in (
                ('français — traduction auto depuis l’anglais', 'French — automatic translation from English'),
                ('anglais — transcription auto · langue d’origine', 'English — automatic transcription · original language'),
                ('• anglais (en) — traduction automatique', '• English (en) — automatic translation'),
                ('1080p · 30 i/s', '1080p · 30 fps'),
                ('Taille estimée : ≈ 20.5 Mo', 'Estimated size: ≈ 20.5 MB'),
                ('≈ 351.0 Mo + sous-titres', '≈ 351.0 MB + subtitles'),
                ('2 pistes  ▾', '2 tracks  ▾'),
                ('L’ordinateur s’arrêtera dans 29 s.', 'The computer will shut down in 29 s.')):
            self.assertEqual(display_text(source), expected)
        self.assertEqual(tr('Dossier : %1', 'C:/%2/Français'), 'Folder: C:/%2/Français')
        self.assertEqual(display_text('https://example.com/français'), 'https://example.com/français')
        self.assertEqual(display_text('C:/Téléchargements/2 langues'), 'C:/Téléchargements/2 langues')


if __name__ == '__main__':
    unittest.main()
