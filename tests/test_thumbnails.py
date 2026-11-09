"""Thumbnail selection with real Qt images and deterministic network responses."""
import copy
import io
import json
import os
os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')
from pathlib import Path
import sys
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from test_features import DATA, request_tab_preview
import youtube_downloader_gui as m
from PySide6.QtCore import QByteArray, QBuffer, QIODevice
from PySide6.QtGui import QColor, QImage
from qt_window import application

FIRST = 'https://youtu.be/aaaaaaaaaaa'
SECOND = 'https://youtu.be/bbbbbbbbbbb'


def image_bytes(color):
    data = QByteArray()
    buffer = QBuffer(data)
    buffer.open(QIODevice.OpenModeFlag.WriteOnly)
    image = QImage(16, 9, QImage.Format.Format_RGB32)
    image.fill(QColor(color))
    image.save(buffer, 'PNG')
    return bytes(data)


class Thumbnails(unittest.TestCase):
    def setUp(self):
        self.a = m.YoutubeDownloaderApp()
        self.a._schedule_queue_analysis = lambda url: None
        self.a.active_queue_url = FIRST
        self.a.pending_urls = [SECOND]
        for url in (FIRST, SECOND):
            data = copy.deepcopy(DATA)
            data.update(id=url.rsplit('/', 1)[-1], title=url, webpage_url=url)
            self.a.analysis_cache[(url, self.a.access_var.get())] = (m.time.monotonic(), data)
        self.red = image_bytes('#ff0000')
        self.blue = image_bytes('#0000ff')

    def tearDown(self):
        self.a.dispose()
        application().processEvents()

    def thumbnail(self, index):
        tabs = self.a.queue_tabs
        return tabs.tabButton(index, tabs.ButtonPosition.LeftSide).thumbnail

    def color(self, index):
        return self.thumbnail(index).pixmap().toImage().pixelColor(0, 0).name()

    def test_link_gets_tab_title_and_thumbnail_before_formats(self):
        self.a._request_tab_preview = request_tab_preview.__get__(self.a)
        self.a.analysis_cache.clear()
        self.a.pending_urls.clear()
        self.a.active_queue_url = None
        url = 'https://www.youtube.com/shorts/aaaaaaaaaaa?si=example'
        tabs = self.a.queue_tabs

        with patch.object(m.threading, 'Thread') as thread:
            self.a._url_link_submitted(url)
            self.a._render_download_queue()
        self.assertEqual(tabs.count(), 1)
        self.assertEqual(thread.call_count, 2)
        self.assertIsNone(self.a.analysis_data)
        image_worker, title_worker = [call.kwargs['target'] for call in thread.call_args_list]

        long_title = 'Titre YouTube 🎬 pour une vidéo assez longue pour occuper plusieurs lignes dans l’onglet'
        with patch.object(m.urllib.request, 'urlopen',
                          return_value=io.BytesIO(json.dumps({'title': long_title}).encode())) as request:
            title_worker()
        self.assertIn('watch%3Fv%3Daaaaaaaaaaa', request.call_args.args[0])
        self.a._poll_events()
        self.assertTrue(tabs.accessibleTabName(0).startswith('Titre YouTube'))
        preview = tabs.tabButton(0, tabs.ButtonPosition.LeftSide)
        self.assertEqual(len(preview.title.text().splitlines()), 2)
        self.assertEqual(preview.title.font().pixelSize(), 15)

        with patch.object(m.urllib.request, 'urlopen', return_value=io.BytesIO(self.red)) as request:
            image_worker()
        self.assertIn('/aaaaaaaaaaa/mqdefault.jpg', request.call_args.args[0])
        self.a._poll_events()
        self.assertEqual(self.color(0), '#ff0000')
        self.assertGreaterEqual(self.thumbnail(0).width(), 100)
        self.assertGreaterEqual(self.thumbnail(0).height(), 56)
        self.assertIsNone(self.a.analysis_data)

        self.a._process_analysis(dict(DATA, id='aaaaaaaaaaa', title='Titre analysé'))
        self.a.events.put(('tab_preview_title', 'aaaaaaaaaaa', 'Ancien titre'))
        self.a._poll_events()
        self.assertTrue(tabs.accessibleTabName(0).startswith('Titre analysé'))
        self.assertEqual(self.color(0), '#ff0000')

    def test_cached_thumbnail_changes_immediately_with_and_without_download(self):
        self.a.thumbnail_cache.update(aaaaaaaaaaa=self.red, bbbbbbbbbbb=self.blue)
        for downloading in (False, True):
            with self.subTest(downloading=downloading):
                self.a.download_thread = object() if downloading else None
                self.a._show_queue_tab(FIRST)
                self.assertEqual(self.color(0), '#ff0000')
                self.a._show_queue_tab(SECOND)
                self.assertEqual(self.color(1), '#0000ff')
                self.a._show_queue_tab(FIRST)
                self.assertEqual(self.color(0), '#ff0000')

    def test_late_other_tab_response_is_cached_without_replacing_visible_image(self):
        self.a.thumbnail_cache['aaaaaaaaaaa'] = self.red
        for downloading in (False, True):
            with self.subTest(downloading=downloading):
                self.a.download_thread = object() if downloading else None
                self.a.thumbnail_cache.pop('bbbbbbbbbbb', None)
                self.a._show_queue_tab(FIRST)
                with patch.object(m.threading, 'Thread') as thread:
                    self.a._request_thumbnail('bbbbbbbbbbb')
                    self.a._show_queue_tab(SECOND)
                    fetch = thread.call_args.kwargs['target']
                self.assertEqual(self.thumbnail(1).pixmap().width(), 28)
                self.a._show_queue_tab(FIRST)
                with patch.object(m.urllib.request, 'urlopen', return_value=io.BytesIO(self.blue)):
                    fetch()
                self.a._poll_events()
                self.assertEqual(self.color(0), '#ff0000')
                self.assertIn('bbbbbbbbbbb', self.a.thumbnail_cache)
                self.a._show_queue_tab(SECOND)
                self.assertEqual(self.color(1), '#0000ff')

    def test_unrelated_analysis_does_not_discard_current_thumbnail(self):
        with patch.object(m.threading, 'Thread') as thread:
            self.a._show_queue_tab(FIRST)
            self.a._request_thumbnail('aaaaaaaaaaa')
            fetch = thread.call_args.kwargs['target']
        self.a.analysis_token += 1
        with patch.object(m.urllib.request, 'urlopen', return_value=io.BytesIO(self.red)):
            fetch()
        self.a._poll_events()
        self.assertEqual(self.color(0), '#ff0000')

    def test_failed_load_keeps_the_placeholder_for_an_unanalyzed_tab(self):
        self.a.thumbnail_cache['aaaaaaaaaaa'] = self.red
        self.a._show_queue_tab(FIRST)
        self.a.analysis_cache.pop((SECOND, self.a.access_var.get()))
        with patch.object(m.threading, 'Thread') as thread:
            self.a._request_thumbnail('bbbbbbbbbbb')
            self.a._show_queue_tab(SECOND)
            fetch = thread.call_args.kwargs['target']
        with patch.object(m.urllib.request, 'urlopen', side_effect=OSError('offline')):
            fetch()
        self.a._poll_events()
        self.assertEqual(self.thumbnail(1).pixmap().width(), 28)
        self.assertEqual(self.color(0), '#ff0000')

    def test_oversized_or_invalid_image_keeps_the_placeholder(self):
        self.a._show_queue_tab(FIRST)
        for content in (b'x' * 2_000_001, b'invalid image'):
            with self.subTest(size=len(content)):
                self.a.thumbnail_cache.clear()
                with patch.object(m.threading, 'Thread') as thread:
                    self.a._request_thumbnail('aaaaaaaaaaa')
                    fetch = thread.call_args.kwargs['target']
                with patch.object(m.urllib.request, 'urlopen', return_value=io.BytesIO(content)):
                    fetch()
                self.a._poll_events()
                self.assertEqual(self.thumbnail(0).pixmap().width(), 28)


if __name__ == '__main__':
    unittest.main(verbosity=2)
