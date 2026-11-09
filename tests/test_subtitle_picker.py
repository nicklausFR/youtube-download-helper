import os
os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')
import sys
import unittest
from pathlib import Path
from unittest.mock import patch

PROJECT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT / '_dependencies' / '_python'))
sys.path.insert(0, str(PROJECT))

from PySide6.QtCore import Qt
from PySide6.QtWidgets import QCheckBox, QDialog, QLineEdit, QListWidget
from core_formats import subtitle_choices
from qt_window import application
import youtube_downloader_gui as gui
from test_features import DATA


def caption(language, target=None):
    url = f'https://www.youtube.com/api/timedtext?lang={language}&kind=asr'
    if target:
        url += f'&tlang={target}'
    return {'url': url, 'name': language}


class SubtitleChoices(unittest.TestCase):
    def test_native_transcription_replaces_mixed_alias_and_same_language_translation(self):
        tracks = subtitle_choices({'automatic_captions': {
            'en-orig': [caption('en')],
            'en': [caption('en'), caption('fr', 'en')],
            'fr-orig': [caption('fr')],
            'fr': [caption('en', 'fr')],
        }}, 'en')

        self.assertEqual([track['code'] for track in tracks], ['en-orig', 'fr-orig'])
        self.assertEqual(tracks[0]['label'], 'anglais — transcription auto · langue d’origine')
        self.assertEqual(tracks[1]['label'], 'français — transcription auto')
        self.assertFalse(tracks[1]['original'])
        self.assertNotIn('-orig', tracks[0]['label'])
        self.assertIn('Code : en-orig', tracks[0]['tooltip'])

    def test_published_captions_do_not_hide_regional_transcriptions(self):
        tracks = subtitle_choices({
            'subtitles': {'en': [{'url': 'https://example.invalid/en', 'name': 'English'}]},
            'automatic_captions': {
                'en-orig': [caption('en')],
                'en': [caption('fr', 'en')],
                'en-US-orig': [caption('en-US')],
                'en-US': [caption('en-US')],
                'de': [caption('en', 'de')],
            },
        }, 'en')

        self.assertEqual({track['code'] for track in tracks}, {'en', 'en-US-orig', 'de'})
        self.assertEqual(next(track for track in tracks if track['code'] == 'en')['kind'], '')
        self.assertIn('anglais (en-US)', next(
            track for track in tracks if track['code'] == 'en-US-orig')['label'])

    def test_translation_names_its_source_and_keeps_internal_kind(self):
        track = subtitle_choices({'automatic_captions': {
            'fr': [caption('en', 'fr')],
        }}, 'en')[0]

        self.assertEqual(track['label'], 'français — traduction auto depuis l’anglais')
        self.assertEqual(track['kind'], 'traduction auto')
        self.assertFalse(track['original'])

    def test_original_suffix_is_not_proof_of_the_videos_original_language(self):
        tracks = subtitle_choices({'automatic_captions': {
            'en-orig': [caption('en')],
            'fr-orig': [caption('fr')],
        }}, None)

        self.assertTrue(all(not track['original'] for track in tracks))
        self.assertTrue(all('langue d’origine' not in track['label'] for track in tracks))

    def test_live_chat_is_not_offered_as_a_subtitle_language(self):
        self.assertEqual(subtitle_choices({'subtitles': {
            'live_chat': [{'url': 'https://www.youtube.com/watch?v=test', 'ext': 'json'}],
        }}, None), [])


class SubtitlePicker(unittest.TestCase):
    def setUp(self):
        self.window = gui.YoutubeDownloaderApp()
        self.window._process_analysis(dict(DATA, automatic_captions={
            'en-orig': [caption('en')],
            'fr': [caption('en', 'fr')],
            'de-orig': [caption('de')],
            'es': [caption('en', 'es')],
        }))
        self.window.hide()

    def tearDown(self):
        self.window.dispose()
        application().processEvents()

    def test_language_first_rows_show_native_tracks_and_search_hidden_translations(self):
        observed = {}

        def inspect(dialog):
            rows = dialog.findChild(QListWidget)
            search = dialog.findChild(QLineEdit)
            show_all = dialog.findChild(QCheckBox)
            observed['initial'] = [rows.item(i).text() for i in range(rows.count())]
            observed['tooltip'] = rows.item(0).toolTip()
            show_all.setChecked(True)
            observed['all_count'] = rows.count()
            show_all.setChecked(False)
            search.setText('espagnol')
            observed['search'] = [rows.item(i).text() for i in range(rows.count())]
            rows.item(0).setCheckState(Qt.CheckState.Checked)
            search.clear()
            observed['checked_visible'] = [rows.item(i).data(Qt.ItemDataRole.UserRole)
                                            for i in range(rows.count())]
            return QDialog.DialogCode.Accepted

        with patch.object(QDialog, 'exec', inspect):
            self.window._open_subtitle_picker()

        self.assertEqual(observed['initial'][0], 'anglais — transcription auto · langue d’origine')
        self.assertIn('allemand — transcription auto', observed['initial'])
        self.assertEqual(len(observed['initial']), 3)
        self.assertEqual(observed['all_count'], 4)
        self.assertIn('Code : en-orig', observed['tooltip'])
        self.assertEqual(observed['search'], ['espagnol — traduction auto depuis l’anglais'])
        self.assertIn('es', observed['checked_visible'])
        self.assertIn('es', self.window._selected_subtitle_codes())

    def test_cancel_preserves_selection_after_search_and_unchecking(self):
        self.window._set_selected_subtitles(['en-orig', 'fr'])

        def inspect(dialog):
            rows = dialog.findChild(QListWidget)
            for index in range(rows.count()):
                rows.item(index).setCheckState(Qt.CheckState.Unchecked)
            dialog.findChild(QLineEdit).setText('francais')
            self.assertEqual(rows.count(), 1)
            return QDialog.DialogCode.Rejected

        with patch.object(QDialog, 'exec', inspect):
            self.window._open_subtitle_picker()

        self.assertEqual(self.window._selected_subtitle_codes(), ('en-orig', 'fr'))


if __name__ == '__main__':
    unittest.main()
