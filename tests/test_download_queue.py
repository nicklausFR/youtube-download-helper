"""Exercise programmed downloads without running network or media workers."""
import os
os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')

import copy
import json
import sys
import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch, sentinel

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from test_features import DATA, save_checkpoint, restore_checkpoint
from test_thumbnails import image_bytes
import youtube_downloader_gui as m
from PySide6.QtCore import QEvent, QMimeData, QPoint, QPointF, Qt, QUrl
from PySide6.QtGui import QDragEnterEvent, QDropEvent
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QPushButton
from qt_window import application


FIRST = 'https://youtu.be/current1234'
SECOND = 'https://youtu.be/queued12345'
THIRD = 'https://youtu.be/third123456'
UNSCHEDULED = 'https://youtu.be/unplanned12'


class DownloadQueue(unittest.TestCase):
    def setUp(self):
        self.output = tempfile.TemporaryDirectory(prefix='yd-queue-tests-')
        self.addCleanup(self.output.cleanup)
        self.a = m.YoutubeDownloaderApp()
        self.a.hide()
        self.a.ytdlp = 'yt-dlp.exe'
        self.a.ffmpeg = 'ffmpeg.exe'
        self.analysis = patch.object(self.a, '_schedule_queue_analysis').start()
        patch.object(m.DownloadOrchestrator, 'analyze', return_value=MagicMock()).start()
        self.addCleanup(patch.stopall)
        self.download = patch.object(
            self.a.orchestrator, 'download',
            side_effect=lambda request, **kwargs: MagicMock(name=request.url),
        ).start()
        self.folder = patch.object(
            self.a, '_request_download_folder', side_effect=self.choose_folder,
        ).start()
        self.errors = patch.object(m.messagebox, 'showerror').start()
        self.next_folder = str(Path(self.output.name) / 'first')

    def tearDown(self):
        self.a.dispose()
        application().processEvents()

    def choose_folder(self):
        self.a.folder_var.set(self.next_folder)
        return self.next_folder

    def prepare(self, url):
        data = copy.deepcopy(DATA)
        video_id = url.rsplit('/', 1)[-1]
        data.update(id=video_id, title='Vidéo ' + video_id, webpage_url=url)
        self.a._url_link_submitted(url)
        if self.a.analysis_job is not None:
            self.a.after_cancel(self.a.analysis_job)
            self.a.analysis_job = None
        self.a.analysis_cache[(url, self.a.access_var.get())] = (
            m.time.monotonic(), data,
        )
        self.a._show_queue_tab(url)
        return data

    def test_dropping_the_active_partial_resumes_the_original_request(self):
        self.prepare(FIRST)
        self.a._start_download()
        job = self.a.active_download_job
        request = job['request']
        video = job['selection']['video']
        component = f".f{video['format_id']}" if '+' in request[3] else ''
        partial = Path(request[1]) / f"{job['title']}{component}.{video['ext']}.part"
        partial.write_bytes(b'partial')
        self.a.download_thread = None
        self.a._hold_failed_download()
        mime = QMimeData()
        mime.setUrls([QUrl.fromLocalFile(str(partial))])
        enter = QDragEnterEvent(QPoint(3, 3), Qt.DropAction.CopyAction, mime,
                                Qt.MouseButton.LeftButton, Qt.KeyboardModifier.NoModifier)
        drop = QDropEvent(QPointF(3, 3), Qt.DropAction.CopyAction, mime,
                          Qt.MouseButton.LeftButton, Qt.KeyboardModifier.NoModifier)
        with patch.object(self.a, '_request_partial_link') as prompt:
            application().sendEvent(self.a.source_panel, enter)
            application().sendEvent(self.a.source_panel, drop)
        prompt.assert_not_called()
        self.assertTrue(enter.isAccepted())
        self.assertTrue(drop.isAccepted())
        self.assertEqual(self.download.call_count, 2)
        self.assertEqual(self.download.call_args_list[0], self.download.call_args_list[1])
        self.assertEqual(partial.read_bytes(), b'partial')

    def test_dropping_an_unrecognized_partial_uses_the_queue_and_source_link(self):
        self.prepare(FIRST)
        self.a._start_download()
        worker = self.a.download_thread
        partial = Path(self.output.name) / 'Other video [queued12345].mp4.part'
        partial.write_bytes(b'partial')
        with patch.object(self.a, '_request_partial_link', return_value=(SECOND, True)) as prompt:
            self.a._resume_dropped_partial(str(partial))
        prompt.assert_called_once_with(partial.name, SECOND)
        self.assertIs(self.a.download_thread, worker)
        self.download.assert_called_once()
        self.assertEqual(self.a.queued_downloads[SECOND]['partial_path'], str(partial))
        self.assertEqual(self.a.queued_downloads[SECOND]['request'][1], str(partial.parent))

    def start(self, url=FIRST, folder='first'):
        self.prepare(url)
        self.next_folder = str(Path(self.output.name) / folder)
        self.a._start_download()

    def finish(self, kind='download_done'):
        if kind == 'download_done':
            event = (kind, [], self.a.last_download_request[1])
        elif kind == 'download_error':
            event = (kind, 'Test failure')
        else:
            event = (kind,)
        self.a.events.put(event)
        self.a._poll_events()
        application().processEvents()
        application().processEvents()

    def requested_urls(self):
        return [call.args[0].url for call in self.download.call_args_list]

    def listed_urls(self):
        items = self.a.download_queue_list
        return [items.item(index).data(Qt.ItemDataRole.UserRole)
                for index in range(items.count())]

    def download_row(self, index):
        items = self.a.download_queue_list
        return items.itemWidget(items.item(index))

    def make_subtitles_pending(self):
        first = self.a.last_download_request
        media = Path(first[1]) / 'First video.mkv'
        media.write_bytes(b'media')
        retry = (first[0], first[1], 'subtitles_retry', '', 'fr', None, first[6])
        self.a.events.put(('subtitle_warning', 'SRT test failure', retry,
                           ('en-orig',), ('fr',)))
        self.a.events.put(('download_done', [str(media)], first[1]))
        with patch.object(self.a, '_offer_failed_subtitle_retry'):
            self.a._poll_events()
            application().processEvents()
        self.assertTrue(self.a.subtitle_retry_pending)
        return media, retry

    def test_download_area_lists_programmed_jobs_with_settings_and_destination(self):
        self.a.thumbnail_cache['current1234'] = image_bytes('#ee2211')
        self.start()
        self.assertEqual(self.a.transfer_title_label.text(), 'Vidéo current1234')
        self.assertFalse(self.a.transfer_panel.isHidden())
        self.assertTrue(self.a.download_queue_empty_label.isHidden())
        self.assertEqual(self.a.transfer_thumbnail_label.pixmap().toImage().pixelColor(0, 0).name(), '#ee2211')
        self.prepare(UNSCHEDULED)
        self.assertFalse(self.listed_urls())
        self.start(SECOND, 'second')
        self.start(THIRD, 'third')
        self.assertEqual(self.listed_urls(), [SECOND, THIRD])
        self.assertEqual(self.a._selected_scheduled_url(), THIRD)
        first_item = self.a.download_queue_list.item(0)
        row = self.download_row(0)
        self.assertEqual(row.title.text(), 'Vidéo queued12345')
        self.assertEqual(row.status.itemAt(0).widget().text(), 'En attente')
        self.assertIn('720p', row.status.itemAt(1).widget().text())
        self.assertIn('Vidéo et audio', row.status.itemAt(1).widget().text())
        self.assertIn(str(Path(self.output.name) / 'second'), first_item.toolTip())
        self.a.resize(640, 530)
        self.a.show()
        application().processEvents()
        status = row.status.itemAt(0).widget()
        self.assertLess(row.up_button.mapTo(row, QPoint()).x(), row.thumbnail.x())
        self.assertLess(row.title.x(), status.x())
        self.assertLess(status.x(), row.remove_button.mapTo(row, QPoint()).x())
        self.assertEqual(row.actions.count(), 2)  # Stretch and Retirer.
        self.assertEqual(row.width(), self.a.download_queue_list.viewport().width())
        self.assertLess(row.remove_button.mapTo(row, QPoint()).x() + row.remove_button.width(), row.width())
        self.a.events.put(('thumbnail', 'queued12345', image_bytes('#123456')))
        self.a._poll_events()
        self.assertIs(self.a.download_queue_list.item(0), first_item)
        self.assertEqual(self.a._selected_scheduled_url(), THIRD)
        self.assertEqual(row.thumbnail.pixmap().toImage().pixelColor(0, 0).name(), '#123456')
        self.assertIs(self.a.download_queue_list.parentWidget(), self.a.download_queue_panel)
        self.finish()
        self.assertEqual(self.listed_urls(), [THIRD])
        self.assertEqual(self.a.transfer_title_label.text(), 'Vidéo queued12345')

    def test_long_queue_scrolls_inside_the_main_window_footer(self):
        self.a.resize(640, 530)
        self.a.show()
        self.start()
        for url in (SECOND, THIRD, UNSCHEDULED):
            self.start(url)
        application().processEvents()

        items = self.a.download_queue_list
        self.assertIs(items.window(), self.a)
        self.assertTrue(self.a.download_queue_panel.rect().contains(items.geometry()))
        self.assertLess(items.height(), sum(items.sizeHintForRow(i) for i in range(items.count())))
        self.assertGreater(items.verticalScrollBar().maximum(), 0)
        items.scrollToBottom()
        self.assertTrue(items.viewport().rect().contains(items.visualItemRect(items.item(2))))
        self.assertTrue(self.a.queued_video_panel.isVisible())
        self.assertFalse(self.a.video_action_panel.isVisible())
        self.assertGreater(self.a.scroll_area.viewport().height(), 0)
        self.a.resize(640, 650)
        # The list's new height posts a layout request through both enclosing panels.
        for _ in range(2):
            application().sendPostedEvents(None, QEvent.Type.LayoutRequest)
        self.assertEqual(items.height(), 2 * items.sizeHintForRow(0))
        self.assertTrue(self.a.download_queue_panel.rect().contains(items.geometry()))

    def test_moving_list_item_changes_the_actual_next_download(self):
        self.start()
        self.start(SECOND, 'second')
        second_row = self.download_row(0)
        tabs = self.a.queue_tabs
        second_preview = tabs.tabButton(1, tabs.ButtonPosition.LeftSide)
        self.start(THIRD, 'third')
        self.assertIs(self.download_row(0), second_row)
        self.assertIs(tabs.tabButton(1, tabs.ButtonPosition.LeftSide), second_preview)
        third_row = self.download_row(1)
        self.a._show_queue_tab(FIRST)
        self.a.download_queue_list.setCurrentRow(0)
        self.assertTrue(self.download_row(1).up_button.isEnabled())
        self.download_row(1).up_button.click()
        self.assertEqual(self.listed_urls(), [THIRD, SECOND])
        self.assertIs(self.download_row(0), third_row)
        self.assertIs(self.download_row(1), second_row)
        self.assertEqual(self.a._video_tab_urls(), [FIRST, SECOND, THIRD])
        self.assertEqual(self.a.url_var.get(), FIRST)
        self.assertEqual(self.a.queue_tabs.currentIndex(), 0)
        self.assertEqual(self.a._selected_scheduled_url(), THIRD)
        self.assertTrue(self.download_row(0).up_button.isEnabled())
        self.assertTrue(self.download_row(0).down_button.isEnabled())
        self.finish()
        self.assertEqual(self.requested_urls(), [FIRST, THIRD])
        self.assertEqual(self.listed_urls(), [SECOND])
        self.assertEqual(self.a._video_tab_urls(), [SECOND, THIRD])
        self.finish()
        self.assertEqual(self.requested_urls(), [FIRST, THIRD, SECOND])

    def test_prioritizing_first_waiting_job_pauses_and_then_resumes_the_active_job(self):
        self.start()
        first_request = self.download.call_args.args[0]
        self.start(SECOND, 'second')
        self.start(THIRD, 'third')
        self.a._show_queue_tab(FIRST)
        self.a.resize(900, 650)
        self.a.show()
        application().processEvents()
        self.a.events.put(('download_progress', 42, '42 Mo / 100 Mo'))
        self.a._poll_events()
        interrupted = threading.Event()
        calling_threads = []
        process = self.a.download_process = sentinel.transfer_process

        def record_interrupt(_):
            calling_threads.append(threading.current_thread())
            interrupted.set()

        with patch('windows_engine.terminate_process_tree', side_effect=record_interrupt) as interrupt:
            QTest.mouseClick(self.download_row(0).up_button, Qt.MouseButton.LeftButton)
            self.assertTrue(interrupted.wait(5))
            interrupt.assert_called_once_with(process)
            self.assertIsNot(calling_threads[0], threading.current_thread())
        self.a.download_process = None
        self.assertTrue(self.a.download_pause_requested)
        self.assertEqual(self.requested_urls(), [FIRST])
        self.assertEqual(self.a.url_var.get(), FIRST)
        with patch.object(self.a.preferences, 'setValue') as save, patch.object(self.a.preferences, 'sync'):
            save_checkpoint(self.a, flush=True)
        checkpoint = json.loads(save.call_args.args[1])
        self.assertIsNone(checkpoint['active'])
        self.assertEqual([job['request'][0] for job in checkpoint['waiting']], [SECOND, FIRST, THIRD])
        self.assertEqual([tab['url'] for tab in checkpoint['tabs']], [FIRST, SECOND, THIRD])
        self.finish('download_cancelled')
        self.assertEqual(self.requested_urls(), [FIRST, SECOND])
        self.assertEqual(self.listed_urls(), [FIRST, THIRD])
        tabs = self.a.queue_tabs
        self.assertEqual([tabs.tabData(i)[0] for i in range(tabs.count())], [FIRST, SECOND, THIRD])
        self.assertEqual(self.a.url_var.get(), FIRST)
        self.assertEqual(tabs.currentIndex(), 0)
        self.a.events.put(('download_progress', 64, '64 Mo / 100 Mo'))
        self.a._poll_events()
        previews = [tabs.tabButton(i, tabs.ButtonPosition.LeftSide) for i in range(tabs.count())]
        self.assertTrue(previews[0].download_progress.isHidden())
        self.assertEqual(previews[1].download_progress.value(), 64)
        row = self.download_row(0)
        self.assertEqual(row.status.itemAt(0).widget().text(), 'En pause')
        self.assertEqual(row.status.itemAt(1).widget().value(), 42)
        self.finish()
        self.assertEqual(self.requested_urls(), [FIRST, SECOND, FIRST])
        self.assertEqual(self.download.call_args.args[0], first_request)
        self.assertEqual(self.a.download_progress.value(), 42)
        self.assertEqual(self.listed_urls(), [THIRD])

    def test_removing_list_item_leaves_its_tab_ready_without_downloading_it(self):
        self.start()
        self.start(SECOND, 'second')
        self.start(THIRD, 'third')
        self.a.resize(900, 650)
        self.a.show()
        application().processEvents()
        self.a.download_queue_list.setCurrentRow(0)
        self.assertEqual(self.a.url_var.get(), THIRD)
        QTest.mouseClick(self.download_row(0).thumbnail, Qt.MouseButton.LeftButton)
        self.assertEqual(self.a.url_var.get(), SECOND)
        self.assertEqual(self.a.queue_tabs.currentIndex(), 1)
        self.assertTrue(self.a.download_queue_list.item(0).isSelected())
        self.assertFalse(self.a.transfer_panel.property('selected'))
        QTest.mouseClick(self.a.transfer_title_label, Qt.MouseButton.LeftButton)
        self.assertEqual(self.a.url_var.get(), FIRST)
        self.assertTrue(self.a.transfer_panel.property('selected'))
        self.assertFalse(self.a.download_queue_list.selectedItems())
        QTest.mouseClick(self.download_row(0).title, Qt.MouseButton.LeftButton)
        self.assertEqual(self.a.url_var.get(), SECOND)
        self.assertTrue(self.a.download_queue_list.item(0).isSelected())
        self.assertFalse(self.a.transfer_panel.property('selected'))
        self.download_row(0).remove_button.click()
        self.assertEqual(self.listed_urls(), [THIRD])
        self.assertIn(SECOND, self.a.pending_urls)
        self.assertEqual(self.a.download_button.text(), 'Ajouter à la file')
        self.assertIn('Analysé', self.a.queue_tabs.accessibleTabName(1))
        self.finish()
        self.assertEqual(self.requested_urls(), [FIRST, THIRD])

    def test_programmed_downloads_start_in_order_with_captured_requests(self):
        self.start()
        self.start(SECOND, 'second')
        second = self.a.queued_downloads[SECOND]['request']
        self.start(THIRD, 'third')
        third = self.a.queued_downloads[THIRD]['request']
        self.prepare(UNSCHEDULED)

        self.assertEqual(self.requested_urls(), [FIRST])
        self.assertEqual(list(self.a.queued_downloads), [SECOND, THIRD])
        self.assertEqual(self.folder.call_count, 3)
        self.finish()
        self.assertEqual(self.requested_urls(), [FIRST, SECOND])
        request = self.download.call_args.args[0]
        self.assertEqual((request.url, request.folder, request.mode,
                          request.format_id, request.subtitle_languages,
                          request.container, request.clip), second)
        self.assertEqual(self.a.active_queue_url, SECOND)
        self.finish()
        self.assertEqual(self.requested_urls(), [FIRST, SECOND, THIRD])
        self.assertEqual(self.download.call_args.args[0].folder, third[1])
        self.finish()
        self.assertEqual(self.requested_urls(), [FIRST, SECOND, THIRD])
        self.assertEqual(self.folder.call_count, 3)
        self.assertIsNone(self.a.download_thread)
        self.assertEqual(self.a.active_queue_url, UNSCHEDULED)

    def test_shutdown_countdown_waits_for_the_last_download_and_can_be_cancelled(self):
        check = self.a.shutdown_after_downloads_check
        self.assertFalse(check.isChecked())
        check.setChecked(True)
        self.start()
        self.start(SECOND)
        with patch.object(m, 'shutdown_windows') as shutdown:
            self.finish()
            self.assertIsNone(self.a._shutdown_dialog)
            self.finish()
            dialog = self.a._shutdown_dialog
            self.assertTrue(dialog.isVisible())
            self.assertIn('30 s', dialog.informativeText())
            QTest.mouseClick(dialog.button(dialog.StandardButton.Cancel), Qt.MouseButton.LeftButton)
            self.assertIsNone(self.a._shutdown_dialog)
            self.assertFalse(check.isChecked())
            shutdown.assert_not_called()

    def test_shutdown_is_requested_only_after_the_countdown_expires(self):
        check = self.a.shutdown_after_downloads_check
        check.setChecked(True)
        with patch.object(m, 'shutdown_windows') as shutdown:
            self.start()
            self.finish('download_cancelled')
            self.assertIsNone(self.a._shutdown_dialog)
            self.start(SECOND)
            self.finish('download_error')
            self.assertIsNone(self.a._shutdown_dialog)
            self.a.resume_download_button.click()
            self.finish()
            dialog = self.a._shutdown_dialog
            shutdown.assert_not_called()
            dialog._deadline.setRemainingTime(0)
            dialog._refresh_countdown()
            shutdown.assert_called_once_with()
            self.assertFalse(check.isChecked())

    def test_button_programs_other_tab_and_disables_current_transfer(self):
        self.start()
        self.assertFalse(self.a.download_button.isEnabled())
        self.prepare(SECOND)
        self.assertTrue(self.a.download_button.isEnabled())
        self.assertEqual(self.a.download_button.text(), 'Ajouter à la file')
        self.a.download_button.click()
        self.assertIn(SECOND, self.a.queued_downloads)
        self.assertEqual(self.listed_urls(), [SECOND])
        self.assertEqual(self.a._selected_scheduled_url(), SECOND)
        self.assertFalse(self.a.download_queue_list.isHidden())
        self.assertEqual(self.requested_urls(), [FIRST])
        self.assertEqual(self.a.download_button.text(), 'Mettre à jour la file')

    def test_queued_request_does_not_change_when_tab_is_edited(self):
        self.start()
        self.start(SECOND, 'second')
        captured = self.a.queued_downloads[SECOND]['request']
        self.a._select_mode('subtitles')
        self.a._set_selected_subtitles(['fr'])
        self.a.folder_var.set(str(Path(self.output.name) / 'changed'))
        self.finish()
        request = self.download.call_args.args[0]
        self.assertEqual(request.mode, captured[2])
        self.assertEqual(request.format_id, captured[3])
        self.assertEqual(request.subtitle_languages, captured[4])
        self.assertEqual(request.folder, captured[1])
        self.assertEqual(self.folder.call_count, 2)

    def test_update_queued_tab_keeps_order_and_replaces_request(self):
        self.start()
        self.start(SECOND, 'second')
        self.start(THIRD, 'third')
        self.a._show_queue_tab(SECOND)
        self.assertFalse(self.a.queued_video_panel.isHidden())
        self.assertTrue(self.a.video_settings_panel.isHidden())
        self.assertIn('720p', self.a.queued_video_summary.text())
        self.a.edit_queued_video_button.click()
        self.assertTrue(self.a.queued_video_panel.isHidden())
        self.assertFalse(self.a.video_settings_panel.isHidden())
        self.a._select_mode('subtitles')
        self.a._set_selected_subtitles(['fr'])
        self.next_folder = str(Path(self.output.name) / 'updated')
        self.a.download_button.click()
        self.assertFalse(self.a.queued_video_panel.isHidden())
        summary = self.a.queued_video_summary.text()
        self.assertIn('Vidéo : Aucune\nAudio : Aucun\nSous-titres : SRT · français', summary)
        self.assertIn('traduction auto', summary)
        self.assertEqual(list(self.a.queued_downloads), [SECOND, THIRD])
        self.assertEqual(self.requested_urls(), [FIRST])
        self.finish()
        request = self.download.call_args.args[0]
        self.assertEqual(request.url, SECOND)
        self.assertEqual(request.mode, 'subtitles')
        self.assertEqual(request.subtitle_languages, 'fr')
        self.assertEqual(request.folder, self.next_folder)

    def test_removing_programmed_download_keeps_its_prepared_tab(self):
        self.start()
        self.start(SECOND, 'second')
        self.a._remove_selected_download_from_queue()
        self.assertNotIn(SECOND, self.a.queued_downloads)
        self.assertIn(SECOND, self.a.pending_urls)
        self.assertEqual(self.a.download_button.text(), 'Ajouter à la file')
        self.finish()
        self.assertEqual(self.requested_urls(), [FIRST])
        self.assertEqual(self.a.active_queue_url, SECOND)

    def test_closing_programmed_tab_removes_request_and_restores_active_tab(self):
        self.start()
        self.start(SECOND, 'second')
        self.start(THIRD, 'third')
        self.a._show_queue_tab(SECOND)
        self.a._remove_queued_url(1)
        self.assertNotIn(SECOND, self.a.pending_urls)
        self.assertNotIn(SECOND, self.a.queued_downloads)
        self.assertNotIn(SECOND, self.a.queue_tab_settings)
        self.assertEqual(self.a.url_var.get(), FIRST)
        self.assertEqual(self.a.active_queue_url, FIRST)
        self.finish()
        self.assertEqual(self.requested_urls(), [FIRST, THIRD])

    def test_closing_first_tab_cancels_the_transfer_and_clears_the_video_view(self):
        self.start()
        tabs = self.a.queue_tabs
        close = tabs.tabButton(0, tabs.ButtonPosition.RightSide).findChild(
            QPushButton, 'videoTabClose')
        close.click()
        self.assertTrue(self.a.cancel_requested)
        self.finish('download_cancelled')
        self.assertEqual(self.a._video_tab_urls(), [])
        self.assertEqual(self.a.url_var.get(), '')
        self.assertTrue(self.a.video_settings_panel.isHidden())
        self.assertTrue(self.a.transfer_panel.isHidden())
        self.assertFalse(self.listed_urls())

    def test_cancelled_folder_does_not_program_or_start_another_download(self):
        self.start()
        self.prepare(SECOND)
        self.next_folder = ''
        self.a._start_download()
        self.assertEqual(self.requested_urls(), [FIRST])
        self.assertFalse(self.a.queued_downloads)

    def test_folder_creation_failure_does_not_program_download(self):
        self.start()
        self.prepare(SECOND)
        with patch.object(m.os, 'makedirs', side_effect=PermissionError('denied')):
            self.a._start_download()
        self.assertFalse(self.a.queued_downloads)
        self.assertEqual(self.requested_urls(), [FIRST])
        self.errors.assert_called_once()

    def test_missing_ffmpeg_does_not_program_download(self):
        self.start()
        self.prepare(SECOND)
        self.a.ffmpeg = None
        self.a._start_download()
        self.assertFalse(self.a.queued_downloads)
        self.assertEqual(self.requested_urls(), [FIRST])
        self.errors.assert_called_once()

    def test_tabs_restore_audio_languages_bitrate_and_excerpt(self):
        self.prepare(FIRST)
        self.a._select_mode('audio')
        self.a._toggle_advanced(True)
        self.a.container_var.set('M4A')
        self.a._refresh_resolution_choices()
        language = next(label for label in self.a._audio_language_choices()
                        if label.startswith('fr'))
        self.a._set_selected_audio_languages([language])
        self.a.audio_rate_var.set('128 kbit/s')
        self.a._audio_options_changed()
        self.a.clip_start_var.set('00:00:02.000')
        self.a.clip_end_var.set('00:00:06.000')
        self.a._apply_clip_time_entries()
        self.a.folder_var.set(str(Path(self.output.name) / 'audio'))
        self.prepare(SECOND)
        self.a._select_mode('subtitles')
        self.a._set_selected_subtitles(['en-orig', 'fr'])
        self.a._show_queue_tab(FIRST)
        self.assertEqual(self.a.mode_var.get(), 'audio')
        self.assertEqual(self.a.settings_var.get(), 'manual')
        self.assertEqual(self.a.container_var.get(), 'M4A')
        self.assertEqual(self.a._selected_audio_language_labels(), (language,))
        self.assertEqual(self.a.audio_rate_var.get(), '128 kbit/s')
        self.assertEqual(self.a._selection()['format'], 'dub128')
        self.assertEqual(self.a._clip_range(), (2.0, 6.0))
        self.assertEqual(self.a.folder_var.get(), str(Path(self.output.name) / 'audio'))
        self.a._show_queue_tab(SECOND)
        self.assertEqual(self.a.mode_var.get(), 'subtitles')
        self.assertEqual(self.a._selected_subtitle(), 'en-orig,fr')

    def configure_video_tab(self):
        self.a._toggle_advanced(True)
        self.a.container_var.set('MKV')
        self.a.codec_var.set('H.264')
        self.a.height_var.set('1080p · 30 i/s')
        self.a._refresh_resolution_choices()
        self.a._set_selected_subtitles(['fr'])
        self.a.keyframe_times = [0.0, 2.0, 6.0, 10.0]
        self.a.keyframe_index_codec = 'H.264'
        self.a._update_clip_index_controls()
        self.a.clip_start_var.set('00:00:02.000')
        self.a.clip_end_var.set('00:00:06.000')
        self.a._apply_clip_time_entries()
        self.a.folder_var.set(str(Path(self.output.name) / 'configured'))
        return self.a._capture_tab_settings(), self.a._selection()

    def test_configured_unstarted_tabs_survive_expiration_and_cache_eviction(self):
        first_data = self.prepare(FIRST)
        first_settings, first_selection = self.configure_video_tab()
        second_data = self.prepare(SECOND)
        self.a._select_mode('audio')
        self.a._toggle_advanced(True)
        self.a.container_var.set('M4A')
        self.a._refresh_resolution_choices()
        self.a.clip_start_var.set('00:00:01.000')
        self.a.clip_end_var.set('00:00:08.000')
        self.a._apply_clip_time_entries()
        second_settings = self.a._capture_tab_settings()
        second_selection = self.a._selection()
        self.a.analysis_cache[(FIRST, self.a.access_var.get())] = (0, first_data)
        self.analysis.reset_mock()
        for evict_cache in (False, True):
            with self.subTest(evict_cache=evict_cache):
                if evict_cache:
                    self.a.analysis_cache.clear()
                self.a._show_queue_tab(FIRST)
                self.assertIs(self.a.analysis_data, first_data)
                self.assertEqual(self.a._capture_tab_settings(), first_settings)
                self.assertEqual(self.a._selection(), first_selection)
                self.assertFalse(self.a.analysis_panel.isVisible())
                self.a._show_queue_tab(SECOND)
                self.assertIs(self.a.analysis_data, second_data)
                self.assertEqual(self.a._capture_tab_settings(), second_settings)
                self.assertEqual(self.a._selection(), second_selection)
        self.analysis.assert_not_called()
        self.download.assert_not_called()

    def test_clicking_current_configured_tab_does_not_reinitialize_it(self):
        self.prepare(FIRST)
        settings, selection = self.configure_video_tab()
        with patch.object(self.a, '_process_analysis') as rebuild:
            self.a._show_queue_tab(FIRST)
        rebuild.assert_not_called()
        self.assertEqual(self.a._capture_tab_settings(), settings)
        self.assertEqual(self.a._selection(), selection)
        self.download.assert_not_called()

    def test_fresh_background_result_keeps_visible_tab_settings(self):
        data = self.prepare(FIRST)
        settings, selection = self.configure_video_tab()
        updated = dict(data, title='Fresh metadata')
        self.a.queue_analysis_url = FIRST
        self.a.queue_analysis_key = (FIRST, self.a.access_var.get())
        background = m.DownloadOrchestrator(m.WindowsEngine('yt-dlp.exe'))
        self.a.queue_analysis_orchestrator = background
        background.engine.events.put(('analysis_ok', background.engine.analysis_token, updated))
        self.a._poll_queue_analysis_events()
        self.assertEqual(self.a.title_var.get(), 'Fresh metadata')
        self.assertIs(self.a.analysis_data, updated)
        self.assertEqual(self.a._capture_tab_settings(), settings)
        self.assertEqual(self.a._selection(), selection)

    def test_explicit_reanalysis_keeps_other_tabs_and_current_settings(self):
        first_data = self.prepare(FIRST)
        settings, selection = self.configure_video_tab()
        second_data = self.prepare(SECOND)
        self.a._show_queue_tab(FIRST)
        self.a._retry_analysis_now()
        self.assertNotIn((FIRST, self.a.access_var.get()), self.a.tab_analysis_data)
        self.assertIs(self.a.tab_analysis_data[(SECOND, self.a.access_var.get())], second_data)
        self.assertIn((SECOND, self.a.access_var.get()), self.a.analysis_cache)
        updated = dict(first_data, title='Refreshed first tab')
        self.a.events.put(('analysis_ok', self.a.analysis_token, updated))
        self.a._poll_events()
        self.assertIs(self.a.analysis_data, updated)
        self.assertEqual(self.a._capture_tab_settings(), settings)
        self.assertEqual(self.a._selection(), selection)

    def test_closing_a_tab_releases_its_saved_analysis(self):
        self.prepare(FIRST)
        self.prepare(SECOND)
        self.a._remove_queued_url(1)
        self.assertTrue(all(key[0] != SECOND for key in self.a.tab_analysis_data))

    def test_tabs_restore_video_index_resolution_subtitles_and_excerpt(self):
        self.prepare(FIRST)
        self.a._toggle_advanced(True)
        self.a.container_var.set('MKV')
        self.a.codec_var.set('H.264')
        self.a.height_var.set('1080p · 30 i/s')
        self.a._refresh_resolution_choices()
        self.a._set_selected_subtitles(['fr'])
        self.a.keyframe_times = [0.0, 2.0, 6.0, 10.0]
        self.a.keyframe_index_codec = 'H.264'
        self.a._update_clip_index_controls()
        self.a.clip_start_var.set('00:00:02.000')
        self.a.clip_end_var.set('00:00:06.000')
        self.a._apply_clip_time_entries()
        self.prepare(SECOND)
        self.a._select_mode('audio')
        self.a._show_queue_tab(FIRST)
        self.assertEqual(self.a.mode_var.get(), 'video')
        self.assertEqual(self.a._selected_video()['format_id'], '1080')
        self.assertEqual(self.a._selected_subtitle(), 'fr')
        self.assertEqual(self.a.keyframe_times, [0.0, 2.0, 6.0, 10.0])
        self.assertEqual(self.a.keyframe_index_codec, 'H.264')
        self.assertTrue(self.a.clip_slider.isEnabled())
        self.assertEqual(self.a._clip_range(), (2.0, 6.0))

    def test_cancelled_download_advances_to_next_programmed_request(self):
        self.start()
        self.start(SECOND, 'second')
        self.finish('download_cancelled')
        self.assertEqual(self.requested_urls(), [FIRST, SECOND])
        self.assertEqual(self.folder.call_count, 2)
        self.assertEqual(self.a.transfer_title_label.text(), 'Vidéo queued12345')
        self.assertNotIn(FIRST, self.listed_urls())
        self.assertEqual(self.a._video_tab_urls(), [FIRST, SECOND])
        self.a._queue_tab_clicked(0)
        self.assertEqual(self.a.title_var.get(), 'Vidéo current1234')
        self.assertEqual(self.a.download_button.text(), 'Ajouter à la file')

    def test_cancelled_download_is_not_saved_while_worker_stops(self):
        self.start()
        self.start(SECOND)
        with (
            patch.object(self.a.preferences, 'setValue') as save,
            patch.object(self.a.preferences, 'sync'),
            patch.object(self.a, '_save_download_checkpoint', lambda: save_checkpoint(self.a, flush=True)),
            patch.object(self.a.orchestrator, 'cancel') as cancel,
        ):
            self.a._cancel_download()
        cancel.assert_called_once()
        self.assertIsNotNone(self.a.download_thread)
        saved = json.loads(save.call_args.args[1])
        self.assertIsNone(saved['active'])
        self.assertEqual([job['request'][0] for job in saved['waiting']], [SECOND])

    def test_pause_preserves_progress_and_waiting_jobs_across_restart(self):
        self.start()
        request = self.download.call_args.args[0]
        audio = self.a.active_download_job['selection']['audio']
        audio.update(language=None, asr=48000, audio_channels=2)
        self.start(SECOND, 'second')
        self.a._show_queue_tab(FIRST)
        self.a.events.put(('download_progress', 42, '42 Mo / 100 Mo'))
        self.a._poll_events()
        with (
            patch.object(self.a.preferences, 'setValue') as save,
            patch.object(self.a.preferences, 'sync'),
            patch.object(self.a, '_save_download_checkpoint', lambda: save_checkpoint(self.a, flush=True)),
        ):
            self.assertFalse(self.a.pause_download_button.isHidden())
            self.a.pause_download_button.click()
            self.assertTrue(self.a.cancel_requested)
            self.assertFalse(self.a.pause_download_button.isEnabled())
            self.assertFalse(self.a.cancel_button.isEnabled())
            self.assertEqual(json.loads(save.call_args.args[1])['active']['progress'], 42)
            self.finish('download_cancelled')
        self.assertTrue(self.a.download_retry_pending)
        self.assertFalse(self.a.cancel_requested)
        self.assertTrue(self.a.pause_download_button.isHidden())
        self.assertFalse(self.a.resume_download_button.isHidden())
        self.assertEqual(self.a.download_progress.value(), 42)
        self.assertEqual(self.requested_urls(), [FIRST])
        self.assertEqual(self.listed_urls(), [SECOND])
        self.assertFalse(self.a.queued_video_panel.isHidden())
        self.assertTrue(self.a.video_settings_panel.isHidden())
        self.assertTrue(self.a.video_action_panel.isHidden())
        self.errors.assert_not_called()

        restored = m.YoutubeDownloaderApp()
        try:
            with patch.object(restored.preferences, 'value', return_value=save.call_args.args[1]):
                restore_checkpoint(restored)
            self.assertTrue(restored.download_retry_pending)
            self.assertEqual(restored.download_progress.value(), 42)
            self.assertEqual(list(restored.queued_downloads), [SECOND])
            self.assertEqual(restored.queue_tabs.currentIndex(), 0)
            self.assertFalse(restored.queued_video_panel.isHidden())
            summary = restored.queued_video_summary.text()
            self.assertIn('Vidéo : 720p · 30 i/s · H.264', summary)
            self.assertIn('Audio : AAC', summary)
            self.assertIn('48 kHz · 2 canaux · langue inconnue', summary)
            self.assertIn('Sous-titres : Aucun', summary)
            self.assertTrue(restored.edit_queued_video_button.isHidden())
            self.assertTrue(restored.video_settings_panel.isHidden())
            self.assertTrue(restored.video_action_panel.isHidden())
            with patch.object(restored.orchestrator, 'download', return_value=MagicMock()) as resume:
                restored.resume_download_button.click()
            self.assertEqual(resume.call_args.args[0], request)
            self.assertFalse(restored.queued_video_panel.isHidden())
        finally:
            restored.dispose()

    def test_pausing_an_excerpt_keeps_its_request_when_the_worker_reports_an_error(self):
        self.start()
        job = self.a.active_download_job
        job['request'] = job['request'][:6] + ((2.0, 6.0),)
        self.a.events.put(('download_progress', 42, '42 Mo / 100 Mo'))
        self.a._poll_events()
        self.a.pause_download_button.click()
        self.finish('download_error')
        self.assertTrue(self.a.download_retry_pending)
        self.assertEqual(self.a.download_progress.value(), 0)
        self.assertIs(self.a.active_download_job, job)
        self.a.resume_download_button.click()
        self.assertEqual(self.download.call_args.args[0].clip, (2.0, 6.0))
        self.errors.assert_not_called()

    def test_failed_download_keeps_queue_and_resumes_exact_request(self):
        self.start()
        first = self.download.call_args.args[0]
        self.start(SECOND, 'second')
        self.a.events.put(('download_progress', 42.5, '42 Mo / 100 Mo'))
        self.finish('download_error')
        self.assertEqual(self.requested_urls(), [FIRST])
        self.assertTrue(self.a.download_retry_pending)
        self.assertIn(SECOND, self.a.queued_downloads)
        self.assertFalse(self.a.resume_download_button.isHidden())
        self.assertTrue(self.a.transfer_status_label.isHidden())
        self.assertFalse(self.a.download_progress.isHidden())
        self.assertEqual(self.a.download_progress.value(), 42)
        # Another tab and different visible settings must not alter recovery.
        self.a._select_mode('audio')
        self.a.resume_download_button.click()
        self.assertEqual(self.a.download_progress.value(), 42)
        self.assertEqual(self.requested_urls(), [FIRST, FIRST])
        self.assertEqual(self.download.call_args.args[0], first)
        self.assertEqual(self.folder.call_count, 2)
        self.errors.assert_called_once()
        self.finish()
        self.assertEqual(self.requested_urls(), [FIRST, FIRST, SECOND])

    def test_failed_download_can_be_skipped_without_losing_next_jobs(self):
        self.start()
        self.start(SECOND, 'second')
        self.start(THIRD, 'third')
        self.finish('download_error')
        self.a.skip_download_button.click()
        application().processEvents()
        self.assertEqual(self.requested_urls(), [FIRST, SECOND])
        self.assertIn(THIRD, self.a.queued_downloads)
        self.assertIn(FIRST, self.a._video_tab_urls())

    def test_checkpoint_restores_interrupted_job_and_queue_without_media_urls(self):
        prepared_data = self.prepare(UNSCHEDULED)
        self.start()
        first = self.a.last_download_request
        self.a.active_download_job['data']['formats'][0]['url'] = 'https://secret.invalid/token'
        self.start(SECOND, 'second')
        self.start(THIRD, 'third')
        self.a._move_scheduled_download(THIRD, -1)
        self.a._show_queue_tab(UNSCHEDULED)
        prepared_data['automatic_captions']['fr'][0]['url'] += '&signature=private-signature'
        self.a._select_mode('subtitles')
        self.a._set_selected_subtitles(['fr'])
        self.a.events.put(('download_progress', 37, '37 Mo / 100 Mo'))
        self.a._poll_events()
        with patch.object(self.a.preferences, 'setValue') as save, \
                patch.object(self.a.preferences, 'sync'):
            save_checkpoint(self.a, flush=True)
        key, payload = save.call_args.args
        self.assertEqual(key, 'download_checkpoint')
        self.assertNotIn('secret.invalid', payload)
        self.assertNotIn('private-signature', payload)
        saved = json.loads(payload)
        self.assertEqual([tab['url'] for tab in saved['tabs']], [UNSCHEDULED, FIRST, SECOND, THIRD])
        self.assertEqual(saved['selected'], UNSCHEDULED)
        self.assertEqual(len(saved['waiting']), 2)
        self.assertEqual(saved['active']['progress'], 37)
        restored = m.YoutubeDownloaderApp()
        try:
            restored.download_thread = None
            with patch.object(restored.preferences, 'value', return_value=payload):
                restore_checkpoint(restored)
            self.assertTrue(restored.download_retry_pending)
            self.assertEqual(restored.last_download_request, first)
            self.assertEqual(list(restored.queued_downloads), [THIRD, SECOND])
            self.assertEqual(restored._video_tab_urls(), [UNSCHEDULED, FIRST, SECOND, THIRD])
            self.assertEqual(restored.url_var.get(), UNSCHEDULED)
            self.assertEqual(restored.queue_tabs.currentIndex(), 0)
            restored.show()
            application().processEvents()
            tabs = restored.queue_tabs
            self.assertEqual(tabs.count(), 4)
            self.assertGreater(tabs.tabRect(0).height(), 0)
            self.assertGreaterEqual(tabs.height(), tabs.tabRect(0).height())
            self.assertTrue(restored.queue_tabs.accessibleTabName(0).startswith('Vidéo unplanned12'))
            self.assertEqual(restored.queue_analysis_waiting, [])
            self.assertFalse(restored.analysis_panel.isVisible())
            self.assertTrue(restored.transfer_status_label.isHidden())
            self.assertEqual(restored.transfer_status_label.text(), '')
            self.assertEqual(restored.progress_text_var.get(), '')
            self.assertFalse(restored.download_progress.isHidden())
            self.assertEqual(restored.download_progress.value(), 37)
            preview = restored.queue_tabs.tabButton(1, restored.queue_tabs.ButtonPosition.LeftSide)
            self.assertTrue(preview.state.isHidden())
            self.assertEqual(preview.download_progress.value(), 37)
            self.assertEqual(restored.skip_download_button.text(), 'Annuler')
            self.assertEqual(restored.mode_var.get(), 'subtitles')
            self.assertEqual(restored._selected_subtitle(), 'fr')
            self.assertTrue(any(track['code'] == 'fr' and track['kind'] == 'traduction auto'
                                for track in restored.subtitle_tracks))
            with patch.object(restored.orchestrator, 'download', return_value=MagicMock()) as run, \
                    patch.object(restored, '_request_download_folder') as folder:
                restored.resume_download_button.click()
                self.assertEqual(run.call_args.args[0].url, FIRST)
                self.assertEqual(run.call_args.args[0].folder, first[1])
                self.assertEqual(restored.download_progress.value(), 37)
                folder.assert_not_called()
        finally:
            restored.dispose()

    def test_closing_with_only_prepared_tabs_saves_order_and_selection(self):
        self.prepare(FIRST)
        self.prepare(SECOND)
        self.a._select_mode('audio')
        with (
            patch.object(self.a.preferences, 'setValue') as save,
            patch.object(self.a.preferences, 'sync'),
            patch.object(self.a, '_save_download_checkpoint', save_checkpoint.__get__(self.a)),
            patch.object(self.a, '_finish_close') as close,
        ):
            self.a._close_app()
        close.assert_called_once()
        payload = save.call_args.args[1]
        saved = json.loads(payload)
        self.assertIsNone(saved['active'])
        self.assertEqual(saved['waiting'], [])
        restored = m.YoutubeDownloaderApp()
        try:
            with patch.object(restored.preferences, 'value', return_value=payload):
                restore_checkpoint(restored)
            self.assertEqual(restored._video_tab_urls(), [FIRST, SECOND])
            self.assertEqual(restored.url_var.get(), SECOND)
            self.assertFalse(restored.download_retry_pending)
            self.assertEqual(restored.analysis_data['title'], 'Vidéo queued12345')
            self.assertEqual(restored.queue_analysis_waiting, [])
            self.assertEqual(restored.mode_var.get(), 'audio')
        finally:
            restored.dispose()

    def test_checkpoint_writer_keeps_latest_queue_during_a_slow_save(self):
        self.start()
        self.start(SECOND)
        self.start(THIRD)
        settings = MagicMock()
        saving = threading.Event()
        release = threading.Event()

        def sync():
            if not saving.is_set():
                saving.set()
                release.wait(5)

        settings.sync.side_effect = sync
        with (
            patch.object(m, 'QSettings', return_value=settings),
            patch.object(self.a, '_save_download_checkpoint', save_checkpoint.__get__(self.a)),
        ):
            self.a._save_download_checkpoint()
            self.assertIsNone(self.a._checkpoint_thread)
            QTest.qWait(200)
            worker = self.a._checkpoint_thread
            try:
                self.assertTrue(saving.wait(5))
                self.assertIsNotNone(worker)
                self.a._move_scheduled_download(THIRD, -1)
                self.a._remove_scheduled_download(SECOND)
                self.assertEqual(self.listed_urls(), [THIRD])
            finally:
                release.set()
                if worker is not None:
                    worker.join(5)
        self.assertFalse(worker.is_alive())
        saved = [json.loads(call.args[1]) for call in settings.setValue.call_args_list]
        self.assertEqual([[job['request'][0] for job in state['waiting']] for state in saved],
                         [[SECOND, THIRD], [THIRD]])

    def test_legacy_checkpoint_restores_active_transfer_and_waiting_list(self):
        self.start()
        self.start(SECOND)
        job = self.a.active_download_job
        video = job['selection']['video']
        folder = Path(job['request'][1])
        partial = folder / f"{job['title']}.f{video['format_id']}.{video['ext']}.part"
        partial.write_bytes(b'x' * (video['filesize'] // 2))
        legacy = json.dumps([self.a._download_job_snapshot(self.a.active_download_job),
                             self.a._download_job_snapshot(self.a.queued_downloads[SECOND])])
        restored = m.YoutubeDownloaderApp()
        try:
            with patch.object(restored.preferences, 'value', return_value=legacy):
                restore_checkpoint(restored)
            self.assertTrue(restored.download_retry_pending)
            self.assertEqual(restored.download_progress.value(), 25)
            self.assertEqual(restored._video_tab_urls(), [FIRST, SECOND])
            self.assertEqual(list(restored.queued_downloads), [SECOND])
            self.assertEqual(restored.queue_analysis_waiting, [FIRST, SECOND])
        finally:
            restored.dispose()

    def test_waiting_only_checkpoint_keeps_prepared_tab_when_queue_starts(self):
        self.start()
        self.start(SECOND)
        self.prepare(UNSCHEDULED)
        self.a._finish_active_queue_item()
        with patch.object(self.a.preferences, 'setValue') as save, patch.object(self.a.preferences, 'sync'):
            save_checkpoint(self.a, flush=True)
        payload = save.call_args.args[1]
        restored = m.YoutubeDownloaderApp()
        try:
            with patch.object(restored.preferences, 'value', return_value=payload):
                restore_checkpoint(restored)
            self.assertFalse(restored.download_retry_pending)
            self.assertEqual(list(restored.queued_downloads), [SECOND])
            with (
                patch.object(restored.orchestrator, 'download', return_value=MagicMock()) as run,
                patch.object(restored, '_start_next_queue_analysis'),
            ):
                restored._dependencies_finished([], False)
            self.assertEqual(run.call_args.args[0].url, SECOND)
            self.assertEqual(restored.url_var.get(), UNSCHEDULED)
            self.assertEqual(restored._video_tab_urls(), [FIRST, SECOND, UNSCHEDULED])
        finally:
            restored.dispose()

    def test_rate_applies_to_active_job_and_following_jobs_from_other_tab(self):
        self.start()
        self.start(SECOND, 'second')
        self.a.rate_unit_combo.setCurrentText('ko/s')
        self.a.rate_limit_spinbox.lineEdit().setText('500')
        with patch.object(self.a.preferences, 'setValue'):
            self.a.apply_rate_button.click()
        self.assertEqual(self.a.download_rate_bps, 500_000)
        revision = self.a.download_rate_revision
        self.finish()
        self.assertEqual(self.a.active_queue_url, SECOND)
        self.assertEqual(self.a.download_rate_bps, 500_000)
        self.assertEqual(self.a.download_rate_revision, revision)

    def test_verification_uses_active_request_while_other_tab_is_visible(self):
        self.start()
        first = self.a.last_download_request
        self.start(SECOND, 'second')
        with patch.object(self.a, '_ask_download_verification', return_value=True), \
                patch.object(m, 'VerificationBrowser') as browser, \
                patch.object(m.threading, 'Thread') as thread:
            browser.return_value.verify.return_value = sentinel.credentials
            self.a.events.put(('verification_required', None, 'not a bot', first))
            self.a._poll_events()
            self.assertEqual(self.a.url_var.get(), SECOND)
            self.assertTrue(self.a._transfer_busy())
            self.assertTrue(self.a.download_verification_pending)
            self.a._activate_next_queued_url()
            self.assertEqual(self.requested_urls(), [FIRST])
            thread.call_args.kwargs['target']()
            self.assertEqual(browser.return_value.verify.call_args.args[0], FIRST)
            self.a._poll_events()
        self.assertEqual(self.requested_urls(), [FIRST, FIRST])
        resumed = self.download.call_args.args[0]
        self.assertEqual(resumed.folder, first[1])
        self.assertEqual(resumed.format_id, first[3])
        self.assertIs(resumed.session, sentinel.credentials)
        self.assertIn(SECOND, self.a.queued_downloads)
        self.assertFalse(self.a.download_verification_pending)
        self.finish()
        self.assertEqual(self.requested_urls(), [FIRST, FIRST, SECOND])

    def assert_repeated_verification_preserves_request(self, credentials):
        self.start()
        first = self.a.last_download_request
        self.start(SECOND, 'second')
        if credentials:
            self.a.verified_credentials = sentinel.credentials
        else:
            self.a.verification_attempted.add(FIRST)
        with patch.object(self.a, '_ask_download_verification') as ask, \
                patch.object(self.a, '_open_verification') as verify:
            self.a.events.put(('verification_required', None, 'not a bot', first))
            self.a._poll_events()
            application().processEvents()
            application().processEvents()
        ask.assert_not_called()
        verify.assert_not_called()
        self.errors.assert_called_once()
        self.assertEqual(self.requested_urls(), [FIRST])
        self.assertEqual(self.a.active_queue_url, FIRST)
        self.assertTrue(self.a.download_retry_pending)
        self.assertIn(SECOND, self.a.queued_downloads)
        self.assertFalse(self.a.download_verification_pending)

    def test_download_rejected_after_verified_session_stays_resumable_without_verification_loop(self):
        self.assert_repeated_verification_preserves_request(credentials=True)

    def test_download_rejected_after_attempted_verification_stays_resumable_without_verification_loop(self):
        self.assert_repeated_verification_preserves_request(credentials=False)

    def test_declining_verification_keeps_resume_and_queue_available(self):
        self.start()
        first = self.a.last_download_request
        self.start(SECOND, 'second')
        with patch.object(self.a, '_ask_download_verification', return_value=False):
            self.a.events.put(('verification_required', None, 'not a bot', first))
            self.a._poll_events()
        application().processEvents()
        self.assertEqual(self.requested_urls(), [FIRST])
        self.assertTrue(self.a.download_retry_pending)
        self.assertIn(SECOND, self.a.queued_downloads)

    def test_active_clip_adjustment_does_not_change_other_tabs_excerpt(self):
        self.prepare(FIRST)
        self.a._toggle_advanced(True)
        self.a.keyframe_times = [0.0, 2.0, 8.0, 10.0]
        self.a.keyframe_index_codec = m.codec_name(self.a._selected_video()['vcodec'])
        self.a._update_clip_index_controls()
        self.a.clip_start_var.set('00:00:02.000')
        self.a.clip_end_var.set('00:00:08.000')
        self.a._apply_clip_time_entries()
        self.a._start_download()
        self.prepare(SECOND)
        self.a._select_mode('audio')
        self.a._toggle_advanced(True)
        self.a.clip_start_var.set('00:00:03.000')
        self.a.clip_end_var.set('00:00:07.000')
        self.a._apply_clip_time_entries()
        previous_entries = (self.a.clip_start_var.get(), self.a.clip_end_var.get())
        previous_slider = (self.a.clip_slider.selectionStart(), self.a.clip_slider.selectionEnd())
        self.a.events.put(('clip_snapped', 1.5, 8.5))
        self.a._poll_events()
        self.assertEqual(self.a.url_var.get(), SECOND)
        self.assertEqual((self.a.clip_start_var.get(), self.a.clip_end_var.get()), previous_entries)
        self.assertEqual((self.a.clip_slider.selectionStart(), self.a.clip_slider.selectionEnd()), previous_slider)
        self.assertEqual(self.a._clip_range(), (3.0, 7.0))
        self.assertEqual(m.parse_timecode(self.a.queue_tab_settings[FIRST]['clip_start']), 1.5)
        self.assertEqual(m.parse_timecode(self.a.queue_tab_settings[FIRST]['clip_end']), 8.5)

    def test_switching_tabs_preserves_pending_subtitle_media_and_active_popup_labels(self):
        self.start()
        self.a.active_download_tracks = (
            {'code': 'en-orig', 'label': 'Anglais de la vidéo active'},
            {'code': 'fr', 'label': 'Français de la vidéo active'},
        )
        media, retry = self.make_subtitles_pending()
        self.prepare(SECOND)
        self.assertEqual(self.a.download_results, [str(media)])
        self.assertEqual(self.a.download_result_folder, retry[1])
        self.assertTrue(self.a._transfer_busy())
        self.a._activate_next_queued_url()
        self.assertEqual(self.requested_urls(), [FIRST])
        observed = {}
        def read_dialog(dialog):
            observed['detail'] = dialog.informativeText()
        with patch('qt_window.QMessageBox.exec', read_dialog):
            self.a._ask_subtitle_retry('SRT test failure', None, ('en-orig',), ('fr',))
        self.assertIn('Anglais de la vidéo active', observed['detail'])
        self.assertIn('Français de la vidéo active', observed['detail'])
        self.assertEqual(self.a.download_results, [str(media)])

    def test_declining_pending_subtitle_retry_releases_next_programmed_download(self):
        self.start()
        self.start(SECOND, 'second')
        self.make_subtitles_pending()
        self.a._activate_next_queued_url()
        self.assertEqual(self.requested_urls(), [FIRST])
        with patch.object(self.a, '_ask_subtitle_retry', return_value=False) as ask:
            self.a._offer_failed_subtitle_retry()
            application().processEvents()
            application().processEvents()
        ask.assert_called_once()
        self.assertEqual(self.requested_urls(), [FIRST, SECOND])
        self.assertFalse(self.a.subtitle_retry_pending)

    def test_successful_subtitle_retry_keeps_media_then_releases_next_download(self):
        self.start()
        self.start(SECOND, 'second')
        media, retry = self.make_subtitles_pending()
        with patch.object(self.a, '_ask_subtitle_retry', return_value=True):
            self.a._offer_failed_subtitle_retry()
        self.assertEqual(self.requested_urls(), [FIRST, FIRST])
        request = self.download.call_args.args[0]
        self.assertEqual(request.mode, 'subtitles_retry')
        self.assertEqual(request.subtitle_languages, 'fr')
        self.assertEqual(request.folder, retry[1])
        self.a._activate_next_queued_url()
        self.assertEqual(self.requested_urls(), [FIRST, FIRST])
        subtitle = media.with_suffix('.fr.srt')
        subtitle.write_text('subtitle', encoding='utf-8')
        self.a.events.put(('subtitle_retry_done', [str(subtitle)], retry[1]))
        self.a._poll_events()
        self.assertEqual(self.a.download_results, [str(media), str(subtitle)])
        application().processEvents()
        application().processEvents()
        self.assertEqual(self.requested_urls(), [FIRST, FIRST, SECOND])
        self.assertFalse(self.a.subtitle_retry_pending)

    def test_duplicate_finish_callbacks_do_not_skip_unscheduled_tabs(self):
        self.start()
        self.prepare(SECOND)
        self.prepare(THIRD)
        self.a.download_thread = None
        self.a._finish_active_queue_item()
        self.a._finish_active_queue_item()
        application().processEvents()
        application().processEvents()
        self.assertEqual(self.a.active_queue_url, SECOND)
        self.assertEqual(self.a.pending_urls, [THIRD])
        self.a._finish_active_queue_item()
        application().processEvents()
        self.assertEqual(self.a.active_queue_url, SECOND)
        self.assertEqual(self.a.pending_urls, [THIRD])
        self.assertEqual(self.requested_urls(), [FIRST])

    def test_folder_dialog_preserves_clicked_tab_when_next_download_starts_inside_it(self):
        self.start()
        self.start(SECOND, 'second')
        data = self.prepare(THIRD)
        self.a._select_mode('video_only')
        self.a._toggle_advanced(True)
        self.a.container_var.set('MP4')
        self.a.codec_var.set('H.264')
        self.a.height_var.set('1080p · 30 i/s')
        self.a._refresh_resolution_choices()
        self.a._set_selected_subtitles(['en-orig', 'fr'])
        self.a.keyframe_times = [0.0, 2.0, 6.0, 10.0]
        self.a.keyframe_index_codec = 'H.264'
        self.a._update_clip_index_controls()
        self.a.clip_start_var.set('00:00:02.000')
        self.a.clip_end_var.set('00:00:06.000')
        self.a._apply_clip_time_entries()
        selection = copy.deepcopy(self.a._selection())
        tracks = tuple(copy.deepcopy(self.a.subtitle_tracks))
        settings = self.a._capture_tab_settings()
        destination = str(Path(self.output.name) / 'third-after-dialog')
        settings['folder'] = destination
        expected_format = m.clip_format_selector(
            'video_only', selection.get('video'), selection.get('audio'),
            selection.get('audios'),
        )
        def complete_current_transfer_inside_dialog():
            self.finish()
            self.assertEqual(self.a.active_queue_url, SECOND)
            self.assertEqual(self.a.url_var.get(), SECOND)
            self.assertEqual(self.a.mode_var.get(), 'video')
            self.a.folder_var.set(destination)
            return destination
        with patch.object(self.a, '_request_download_folder',
                          side_effect=complete_current_transfer_inside_dialog):
            self.a._start_download()
        self.assertEqual(self.requested_urls(), [FIRST, SECOND])
        job = self.a.queued_downloads[THIRD]
        self.assertEqual(job['request'], (
            THIRD, destination, 'video_only', expected_format,
            'en-orig,fr', 'mkv', (2.0, 6.0),
        ))
        self.assertEqual(job['settings'], settings)
        self.assertEqual(job['selection'], selection)
        self.assertEqual(job['tracks'], tracks)
        self.assertIs(job['data'], data)
        self.assertEqual(job['title'], data['title'])
        self.assertEqual(self.listed_urls(), [THIRD])
        item = self.a.download_queue_list.item(0)
        row = self.download_row(0)
        summary = row.status.itemAt(1).widget().text()
        self.assertEqual(data['title'], row.title.text())
        self.assertIn('Vidéo seule', summary)
        self.assertIn('1080p', summary)
        self.assertIn('SRT', summary)
        self.assertIn('Extrait', summary)
        self.assertIn(destination, item.toolTip())
        self.finish()
        self.assertEqual(self.requested_urls(), [FIRST, SECOND, THIRD])
        request = self.download.call_args.args[0]
        self.assertEqual(request.mode, 'video_only')
        self.assertEqual(request.clip, (2.0, 6.0))
        self.assertEqual(request.subtitle_languages, 'en-orig,fr')
        self.assertEqual(request.folder, destination)
        self.assertIs(self.a.active_download_data, data)
        self.assertEqual(self.a.active_download_tracks, tracks)


if __name__ == '__main__':
    unittest.main()
