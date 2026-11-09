"""Automatic analysis chaining without selecting tabs or launching downloads."""
import copy
import os
os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')
from pathlib import Path
import sys
import unittest
from unittest.mock import MagicMock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from test_features import DATA
import youtube_downloader_gui as m
from qt_window import application

FIRST = 'https://youtu.be/aaaaaaaaaaa'
SECOND = 'https://youtu.be/bbbbbbbbbbb'
THIRD = 'https://youtu.be/ccccccccccc'


class AnalysisQueue(unittest.TestCase):
    def setUp(self):
        self.a = m.YoutubeDownloaderApp()
        self.a.ytdlp = 'yt-dlp.exe'
        self.analyze_patch = patch.object(m.DownloadOrchestrator, 'analyze', return_value=MagicMock())
        self.analyze = self.analyze_patch.start()

    def tearDown(self):
        self.a.dispose()
        self.analyze_patch.stop()
        application().processEvents()

    def data(self, url):
        data = copy.deepcopy(DATA)
        data.update(id=url.rsplit('/', 1)[-1], title=url, webpage_url=url)
        return data

    def start_first(self):
        self.a._url_link_submitted(FIRST)
        self.a.after_cancel(self.a.analysis_job)
        self.a._start_analysis()

    def finish_background(self, kind='analysis_ok'):
        url = self.a.queue_analysis_url
        engine = self.a.queue_analysis_orchestrator.engine
        value = self.data(url) if kind == 'analysis_ok' else 'Test network failure'
        engine.events.put((kind, engine.analysis_token, value))
        self.a._poll_events()

    def requested(self):
        return [call.args[0] for call in self.analyze.call_args_list]

    def test_first_analysis_automatically_starts_second_then_third(self):
        self.start_first()
        self.a._url_link_submitted(SECOND)
        self.a._url_link_submitted(THIRD)
        self.assertEqual(self.requested(), [FIRST])
        self.a.events.put(('analysis_ok', self.a.analysis_token, self.data(FIRST)))
        self.a._poll_events()
        self.assertEqual(self.requested(), [FIRST, SECOND])
        self.assertEqual(self.a.url_var.get(), FIRST)
        self.finish_background()
        self.assertEqual(self.requested(), [FIRST, SECOND, THIRD])
        self.assertEqual(self.a.url_var.get(), FIRST)
        self.finish_background()
        self.assertIsNone(self.a.queue_analysis_orchestrator)
        self.assertFalse(self.a.queue_analysis_waiting)
        self.assertEqual(self.a.url_var.get(), FIRST)
        self.assertIn('Analysé', self.a.queue_tabs.accessibleTabName(1))
        self.assertIn('Analysé', self.a.queue_tabs.accessibleTabName(2))

    def test_failed_analysis_does_not_block_following_tab_during_download(self):
        self.start_first()
        self.a.events.put(('analysis_ok', self.a.analysis_token, self.data(FIRST)))
        self.a._poll_events()
        self.a.download_thread = object()
        self.a._url_link_submitted(SECOND)
        self.a._url_link_submitted(THIRD)
        self.a._show_queue_tab(SECOND)
        self.assertFalse(self.a.analysis_panel.isHidden())
        self.finish_background('analysis_error')
        self.assertTrue(self.a.analysis_panel.isHidden())
        self.assertTrue(self.a.video_settings_panel.isHidden())
        self.assertEqual(self.a.status_label.text(), 'Échec de l’analyse')
        self.assertTrue(self.a.retry_analysis_action.isEnabled())
        self.assertEqual(self.a.queue_analysis_url, THIRD)
        self.assertIn(SECOND, self.a.queue_analysis_errors)
        self.a._show_queue_tab(FIRST)
        self.finish_background()
        self.a._poll_events()
        self.assertEqual(self.requested(), [FIRST, SECOND, THIRD])
        self.assertIsNotNone(self.a.download_thread)
        self.assertEqual(self.a.url_var.get(), FIRST)

    def test_waiting_analysis_restarts_after_dependency_operation_without_click(self):
        self.start_first()
        self.a.events.put(('analysis_ok', self.a.analysis_token, self.data(FIRST)))
        self.a._poll_events()
        self.a.dependency_busy = True
        self.a._url_link_submitted(SECOND)
        self.a._url_link_submitted(THIRD)
        self.assertEqual(self.requested(), [FIRST])
        self.a.events.put(('dependency_updates_done', {}, []))
        self.a._poll_events()
        self.assertEqual(self.a.queue_analysis_url, SECOND)
        self.finish_background()
        self.assertEqual(self.a.queue_analysis_url, THIRD)

    def test_pending_tab_missing_from_waiting_list_is_still_analyzed(self):
        self.start_first()
        self.a.pending_urls.extend([SECOND, THIRD])
        self.a.events.put(('analysis_ok', self.a.analysis_token, self.data(FIRST)))
        self.a._poll_events()
        self.assertEqual(self.a.queue_analysis_url, SECOND)
        self.finish_background()
        self.assertEqual(self.a.queue_analysis_url, THIRD)

    def test_completed_tabs_do_not_restart_after_cache_expiration_or_eviction(self):
        self.start_first()
        self.a._url_link_submitted(SECOND)
        self.a._url_link_submitted(THIRD)
        self.a.events.put(('analysis_ok', self.a.analysis_token, self.data(FIRST)))
        self.a._poll_events()
        self.finish_background()
        self.finish_background()
        key = (SECOND, self.a.access_var.get())
        self.a.analysis_cache[key] = (0, self.data(SECOND))
        self.a.analysis_cache.pop((THIRD, self.a.access_var.get()))
        self.a._poll_events()
        self.assertEqual(self.requested(), [FIRST, SECOND, THIRD])
        self.assertIsNone(self.a.queue_analysis_orchestrator)
        self.a._render_download_queue()
        self.assertIn('Analysé', self.a.queue_tabs.accessibleTabName(1))
        self.assertIn('Analysé', self.a.queue_tabs.accessibleTabName(2))


if __name__ == '__main__':
    unittest.main(verbosity=2)
