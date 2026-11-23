"""Exercise the list through the real Qt controller without contacting YouTube."""

import copy
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import MagicMock, patch

os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')

from PySide6.QtCore import QPoint, QPointF, Qt, QUrl
from PySide6.QtGui import QDesktopServices
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QHeaderView, QLineEdit
from test_features import DATA, m, restore_checkpoint, save_checkpoint
from test_thumbnails import image_bytes
from qt_download_list import AUDIO, DEFAULT_FORMATS, ROLE, SUBTITLES, VIDEO
from qt_window import application


FIRST = 'https://youtu.be/current1234'
SECOND = 'https://youtu.be/queued12345'
THIRD = 'https://youtu.be/unplanned12'
FOURTH = 'https://youtu.be/fourth12345'


class DownloadList(unittest.TestCase):
    def setUp(self):
        self.output = tempfile.TemporaryDirectory(prefix='yd-list-tests-')
        self.addCleanup(self.output.cleanup)
        with patch.object(m.YoutubeDownloaderApp, '_read_format_defaults', return_value=DEFAULT_FORMATS.copy()):
            self.a = m.YoutubeDownloaderApp(list_view=True)
        self.a.ytdlp = 'yt-dlp.exe'
        self.a.ffmpeg = 'ffmpeg.exe'
        self.tree = self.a.download_table.tree
        self.table = self.a.download_table
        self.rows = self.a.download_table.rows
        self.download = self.mock(self.a.orchestrator, 'download', return_value=MagicMock())
        self.mock(self.a, '_schedule_queue_analysis')
        self.mock(self.a, '_request_download_folder', return_value=self.output.name)
        self.mock(m.messagebox, 'showerror')
        self.a.show()
        application().processEvents()

    def tearDown(self):
        self.a.dispose()
        application().processEvents()

    def mock(self, target, method, **kwargs):
        patcher = patch.object(target, method, **kwargs)
        self.addCleanup(patcher.stop)
        return patcher.start()

    def prepare(self, url, fixture=DATA):
        self.a._url_link_submitted(url)
        if self.a.analysis_job is not None:
            self.a.after_cancel(self.a.analysis_job)
            self.a.analysis_job = None
        data = copy.deepcopy(fixture)
        data.update(id=url.rsplit('/', 1)[-1], title=f'Vidéo {len(self.rows)}')
        self.a.tab_analysis_data[(url, self.a.access_var.get())] = data
        self.tree.setCurrentItem(self.rows[url])
        self.a._show_queue_tab(url)
        application().processEvents()

    def event(self, *event):
        self.a.events.put(event)
        self.a._poll_events()
        application().processEvents()
        application().processEvents()

    def reanalyze(self, url):
        menu = self.table.line_menu(self.rows[url])
        action = next(action for action in menu.actions() if action.text() == 'Relancer l’analyse')
        self.assertTrue(action.isEnabled())
        with patch.object(m.DownloadOrchestrator, 'analyze', return_value=MagicMock()) as analyze:
            action.trigger()
        self.assertEqual(analyze.call_args.args[0], url)
        other_menu = self.table.line_menu(self.rows[url])
        self.assertFalse(next(action for action in other_menu.actions()
                              if action.text() == 'Relancer l’analyse').isEnabled())
        return self.a.queue_analysis_orchestrator.engine

    def analysis_result(self, engine, kind, value):
        engine.events.put((kind, engine.analysis_token, value))
        self.a._poll_queue_analysis_events()
        application().processEvents()

    def open_streams(self, url, column):
        self.table.stream_popup.close()
        index = self.tree.indexFromItem(self.rows[url], column)
        self.tree.scrollTo(index)
        rect = self.tree.visualRect(index)
        QTest.mouseClick(self.tree.viewport(), Qt.MouseButton.LeftButton, pos=rect.center())
        self.assertTrue(self.table.stream_popup.isVisible())
        self.assertEqual(self.table.stream_url, url)
        group = self.table.stream_tree.topLevelItem(0)
        return {group.data(0, ROLE): group, **self.items(group)}

    def items(self, parent):
        items = {}
        for index in range(parent.childCount()):
            child = parent.child(index)
            if key := child.data(0, ROLE):
                items[key] = child
            items.update(self.items(child))
        return items

    def test_column_stream_choices_change_the_real_request(self):
        self.prepare(FIRST)
        for column, kind, current, desired in ((VIDEO, 'video', '720', '1080'),
                                               (AUDIO, 'audio', 'source129', 'dub128'),
                                               (SUBTITLES, 'subtitle', None, 'fr')):
            children = self.open_streams(FIRST, column)
            self.assertEqual(children['group', kind].checkState(0),
                             Qt.CheckState.Checked if current else Qt.CheckState.Unchecked)
            if current:
                self.assertEqual(children[kind, current].checkState(0), Qt.CheckState.Checked)
            self.assertTrue(children[kind, desired].flags() & Qt.ItemFlag.ItemIsUserCheckable)
            self.assertTrue(self.table.stream_tree.visualItemRect(children[kind, desired]).isValid())
            children[kind, desired].setCheckState(0, Qt.CheckState.Checked)
        self.table.stream_popup.close()
        menu = self.table.line_menu(self.rows[FIRST])
        self.assertEqual(menu.actions()[0].text(), 'Télécharger')
        menu.actions()[0].trigger()
        request = self.download.call_args.args[0]
        self.assertEqual(request.format_id, '1080+source129+dub128')
        self.assertEqual(request.subtitle_languages, 'fr')
        self.assertEqual(request.container, 'mkv')
        children = self.open_streams(FIRST, VIDEO)
        self.assertNotIn(Qt.ItemFlag.ItemIsUserCheckable, children['video', '1080'].flags())

    def test_estimated_size_is_visible_after_background_analysis_and_stays_with_the_row(self):
        self.prepare(FIRST)
        self.assertEqual(self.rows[FIRST].text(2), '≈ 600 o')
        self.a.download_defaults = dict(container='MKV', video='1080', audio='fr', subtitles=['fr'])
        self.a._url_link_submitted(SECOND)
        self.assertEqual(self.rows[SECOND].text(2), '—')
        with patch.object(m.DownloadOrchestrator, 'analyze', return_value=MagicMock()):
            m.YoutubeDownloaderApp._schedule_queue_analysis(self.a, SECOND)
        engine = self.a.queue_analysis_orchestrator.engine
        self.analysis_result(engine, 'analysis_ok', dict(DATA, id='queued12345', title='Vidéo suivante'))
        self.assertEqual(self.table.selected_url(), FIRST)
        self.assertIn('1080p', self.rows[SECOND].text(VIDEO))
        self.assertEqual(self.rows[SECOND].text(2), '≈ 1.1 Ko + sous-titres')

        self.tree.setCurrentItem(self.rows[SECOND])
        self.assertEqual(self.rows[FIRST].text(2), '≈ 600 o')
        self.assertEqual(self.rows[SECOND].text(2), '≈ 1.1 Ko + sous-titres')
        children = self.open_streams(SECOND, VIDEO)
        children['video', '720'].setCheckState(0, Qt.CheckState.Checked)
        self.table.stream_popup.close()
        self.assertEqual(self.rows[SECOND].text(2), '≈ 599 o + sous-titres')
        self.tree.setCurrentItem(self.rows[FIRST])
        self.assertEqual(self.rows[SECOND].text(2), '≈ 599 o + sous-titres')

    def test_completed_row_survives_queue_advancement_and_restart(self):
        self.prepare(FIRST, dict(DATA, description='Présentation complète\n\nChapitres et détails.'))
        self.a._start_download()
        self.prepare(SECOND)
        self.a._start_download()
        self.prepare(THIRD)
        item = self.rows[FIRST]
        self.event('download_progress', 42.0, 'Détail de test', 12, 1234567)
        self.assertEqual(item.data(4, ROLE), 42.0)
        self.assertTrue(item.text(5).endswith('/s'))
        media = Path(self.output.name) / 'résultat.mkv'
        media.write_bytes(b'media')
        self.event('download_done', [str(media)], self.output.name)
        self.assertIs(self.rows[FIRST], item)
        self.assertEqual(item.data(3, ROLE), 'done')
        self.assertEqual(item.data(4, ROLE), 100)
        self.assertEqual(self.download.call_args.args[0].url, SECOND)
        self.assertEqual(self.a.download_table.selected_url(), THIRD)
        self.tree.setCurrentItem(item)
        self.assertTrue(self.a.selected_video_panel.isVisible())
        self.assertIn('Chapitres et détails.', self.a.selected_video_description.toPlainText())

        with patch.object(self.a.preferences, 'setValue') as save, patch.object(self.a.preferences, 'sync'):
            save_checkpoint(self.a, flush=True)
        payload = save.call_args.args[1]
        self.assertNotIn('timedtext', payload)
        saved = json.loads(payload)
        self.assertEqual(saved['active']['request'][0], SECOND)
        restored = m.YoutubeDownloaderApp(list_view=True)
        try:
            with patch.object(restored.preferences, 'value', return_value=payload):
                restore_checkpoint(restored)
            self.assertTrue(restored.download_retry_pending)
            self.assertEqual(restored.download_table.selected_url(), FIRST)
            self.assertEqual(restored.download_table.rows[FIRST].data(3, ROLE), 'done')
            self.assertEqual(restored.download_table.rows[SECOND].data(3, ROLE), 'retry')
            self.assertIn('Chapitres et détails.', restored.selected_video_description.toPlainText())
            restored.download_table.clear.click()
            self.assertNotIn(FIRST, restored.download_table.rows)
            self.assertTrue(media.is_file())
        finally:
            restored.dispose()

    def test_pause_and_resume_keep_the_request_and_row(self):
        self.prepare(FIRST)
        self.a._start_download()
        first_request = self.download.call_args.args[0]
        item = self.rows[FIRST]
        self.event('download_progress', 38, '', None, 2000)
        self.assertTrue(self.a.download_panel.isHidden())
        menu = self.table.line_menu(item)
        self.assertEqual([action.text() for action in menu.actions()[:2]],
                         ['Arrêter le téléchargement', 'Mettre en pause'])
        with patch.object(self.a.orchestrator, 'cancel'):
            menu.actions()[1].trigger()
        self.event('download_cancelled')
        self.assertIs(self.rows[FIRST], item)
        self.assertEqual(item.data(3, ROLE), 'retry')
        self.assertEqual(item.data(4, ROLE), 38)
        menu = self.table.line_menu(item)
        self.assertEqual(menu.actions()[0].text(), 'Reprendre')
        menu.actions()[0].trigger()
        self.assertEqual(self.download.call_args.args[0], first_request)
        self.assertEqual(item.data(3, ROLE), 'active')

    def test_drag_order_drives_downloads_and_the_saved_queue(self):
        self.prepare(FIRST)
        self.a._start_download()
        for url in (SECOND, THIRD, FOURTH):
            self.prepare(url)
        for url in (FOURTH, THIRD, SECOND):
            self.a._show_queue_tab(url)
            self.a._start_download()
        self.assertEqual(list(self.a.queued_downloads), [SECOND, THIRD, FOURTH])
        worker = self.a.download_thread

        drop = MagicMock()
        drop.source.return_value = self.tree
        rect = self.tree.visualItemRect(self.rows[SECOND])
        drop.position.return_value = QPointF(rect.left() + 10, rect.top() + 1)
        self.tree.dragged_url = FOURTH
        self.tree.dropEvent(drop)
        self.tree.dragged_url = None

        drop.accept.assert_called_once()
        self.assertEqual([self.tree.topLevelItem(i).data(0, ROLE) for i in range(4)], [FIRST, FOURTH, SECOND, THIRD])
        self.assertIs(self.a.download_thread, worker)
        self.assertEqual(self.download.call_count, 1)
        with patch.object(self.a.preferences, 'setValue') as save, patch.object(self.a.preferences, 'sync'):
            save_checkpoint(self.a, flush=True)
        payload = json.loads(save.call_args.args[1])
        self.assertEqual([job['request'][0] for job in payload['waiting']], [FOURTH, SECOND, THIRD])
        self.event('download_done', [], self.output.name)
        self.assertEqual(self.download.call_args.args[0].url, FOURTH)
        self.event('download_done', [], self.output.name)
        self.assertEqual(self.download.call_args.args[0].url, SECOND)

    def test_rate_limit_is_in_the_toolbar_before_defaults(self):
        rate_controls = self.a.rate_limit_spinbox.parentWidget()
        self.assertTrue(self.a.rate_limit_spinbox.isVisible())
        self.assertEqual(self.table.toolbar.indexOf(rate_controls) + 1, self.table.toolbar.indexOf(self.table.defaults))
        shutdown = self.a.shutdown_after_downloads_check
        self.assertEqual(self.table.toolbar.indexOf(shutdown) + 1, self.table.toolbar.indexOf(rate_controls))
        self.assertTrue(shutdown.isVisible())
        self.assertFalse(shutdown.isChecked())
        self.assertTrue(self.a.options_button.isVisible())
        self.assertEqual(self.table.toolbar.indexOf(self.a.options_button), self.table.toolbar.count() - 1)
        self.assertFalse(self.table.findChildren(QLineEdit, options=Qt.FindChildOption.FindDirectChildrenOnly))
        self.assertTrue(self.a.download_panel.isHidden())

    def test_reanalysis_of_an_active_video_keeps_its_request_and_refreshes_its_description(self):
        self.prepare(FIRST, dict(DATA, description='Première description'))
        self.a._start_download()
        worker, job = self.a.download_thread, self.a.active_download_job
        self.event('download_progress', 40, '', None, None)
        engine = self.reanalyze(FIRST)
        self.assertIn('Nouvelle analyse en cours', self.rows[FIRST].toolTip(3))
        self.analysis_result(engine, 'analysis_ok', dict(DATA, description='Nouvelle description'))
        self.assertEqual(self.a.selected_video_description.toPlainText(), 'Nouvelle description')
        self.assertIs(self.a.download_thread, worker)
        self.assertIs(self.a.active_download_job, job)
        self.assertEqual(job['data']['description'], 'Première description')
        self.assertEqual(self.a.download_progress_value, 40)
        self.download.assert_called_once()
        self.event('download_done', [], self.output.name)
        self.assertEqual(self.a.completed_downloads[FIRST]['data']['description'], 'Nouvelle description')

    def test_reanalysis_error_keeps_a_programmed_video_while_another_download_runs(self):
        self.prepare(FIRST)
        self.a._start_download()
        self.prepare(SECOND, dict(DATA, description='Description conservée'))
        self.a._start_download()
        queued = self.a.queued_downloads[SECOND]
        engine = self.reanalyze(SECOND)
        self.analysis_result(engine, 'analysis_error', 'Erreur de test')
        self.assertIn('Relance de l’analyse échouée', self.rows[SECOND].toolTip(3))
        self.assertEqual(self.a.selected_video_description.toPlainText(), 'Description conservée')
        self.assertIs(self.a.queued_downloads[SECOND], queued)
        engine = self.reanalyze(SECOND)
        self.analysis_result(engine, 'analysis_ok', dict(DATA, description='Description actualisée'))
        self.assertEqual(self.a.selected_video_description.toPlainText(), 'Description actualisée')
        self.assertIs(self.a.queued_downloads[SECOND], queued)

    def test_reanalysis_can_refresh_a_completed_row_without_replacing_its_results(self):
        self.prepare(FIRST)
        self.a._start_download()
        engine = self.reanalyze(FIRST)
        self.event('download_done', [], self.output.name)
        self.assertEqual(self.rows[FIRST].data(3, ROLE), 'done')
        record = self.a.completed_downloads[FIRST]
        self.analysis_result(engine, 'analysis_ok', dict(DATA, description='Description après la fin', formats=[]))
        self.assertEqual(self.rows[FIRST].data(3, ROLE), 'done')
        updated = self.a.completed_downloads[FIRST]
        self.assertEqual(updated['data']['formats'], record['data']['formats'])
        self.assertEqual(updated['paths'], record['paths'])
        self.assertEqual(self.a.selected_video_description.toPlainText(), 'Description après la fin')
        engine = self.reanalyze(FIRST)
        self.analysis_result(engine, 'analysis_ok', dict(DATA, description='Description depuis le menu Terminé'))
        self.assertEqual(self.a.selected_video_description.toPlainText(), 'Description depuis le menu Terminé')

    def test_configures_the_selected_row_and_sets_the_container_from_its_menu(self):
        self.prepare(FIRST)
        self.prepare(SECOND)
        self.assertFalse(self.tree.rootIsDecorated())
        self.assertFalse(self.tree.itemsExpandable())
        self.assertEqual(self.rows[FIRST].childCount(), 0)
        self.assertEqual([self.tree.headerItem().text(i) for i in (VIDEO, AUDIO, SUBTITLES)],
                         ['Vidéo', 'Audio', 'Sous-titres'])
        self.assertTrue(self.a.video_settings_panel.isHidden())
        children = self.open_streams(FIRST, VIDEO)
        self.assertEqual(self.a.queue_view_url, FIRST)
        children['video', '1080'].setCheckState(0, Qt.CheckState.Checked)
        self.assertEqual(children['video', '720'].checkState(0), Qt.CheckState.Unchecked)
        self.assertIn('1080p', self.rows[FIRST].text(VIDEO))
        self.table.stream_popup.close()

        self.assertTrue(self.a.video_action_panel.isHidden())
        menu = self.table.line_menu(self.rows[FIRST])
        container = next(action.menu() for action in menu.actions() if action.text().startswith('Conteneur'))
        next(action for action in container.actions() if action.text() == 'MP4').trigger()
        self.assertEqual(self.a.container_var.get(), 'MP4')
        children = self.open_streams(FIRST, AUDIO)
        children['group', 'audio'].setCheckState(0, Qt.CheckState.Unchecked)
        self.assertEqual(children['audio', 'source129'].checkState(0), Qt.CheckState.Unchecked)
        self.assertTrue(self.rows[FIRST].text(AUDIO).startswith('Aucun'))
        self.table.stream_popup.close()

        self.a.download_button.click()
        request = self.download.call_args.args[0]
        self.assertEqual((request.url, request.mode, request.format_id, request.container),
                         (FIRST, 'video_only', '1080', 'mp4'))

    def test_title_column_can_be_resized_and_the_video_details_show_the_full_description(self):
        description = 'Chapitre 1\n\n' + 'Description complète de cette vidéo.\n' * 30
        self.prepare(FIRST, dict(DATA, description=description))
        header = self.tree.header()
        self.assertEqual(header.sectionResizeMode(0), QHeaderView.ResizeMode.Interactive)
        with patch.object(self.a.preferences, 'setValue') as write:
            header.resizeSection(0, 420)
        self.assertEqual(self.tree.columnWidth(0), 420)
        write.assert_called_with('download_title_column_width', 420)
        self.assertTrue(self.a.selected_video_description.isReadOnly())
        self.assertEqual(self.a.selected_video_description.toPlainText(), description.strip())
        self.assertFalse(any(button.text().startswith('Transfert') for button in self.table.findChildren(type(self.table.defaults))))

    def test_thumbnail_tracks_the_splitter_and_the_link_opens_in_the_default_browser(self):
        self.prepare(FIRST, dict(DATA, description='Description de test.'))
        self.event('thumbnail', FIRST.rsplit('/', 1)[-1], image_bytes('#388fc4'))
        splitter = self.a.list_detail_pane.parentWidget()
        thumbnail = self.a.selected_video_thumbnail
        identity = self.a.selected_video_panel.layout()
        heights = []
        for detail_height in (240, 350):
            splitter.setSizes([splitter.height() - detail_height, detail_height])
            application().processEvents()
            heights.append(thumbnail.height())
            self.assertEqual(thumbnail.height(), identity.geometry().height())
            self.assertAlmostEqual(thumbnail.width(), thumbnail.height() * 16 / 9, delta=1)
        self.assertGreater(heights[1], heights[0])
        link = self.a.selected_video_url
        self.assertIn(f'href="{FIRST}"', link.text())
        with patch.object(QDesktopServices, 'openUrl') as open_url:
            QTest.mouseClick(link, Qt.MouseButton.LeftButton, pos=QPoint(35, link.height() // 2))
            open_url.assert_called_once_with(QUrl(FIRST))

    def test_saved_defaults_apply_only_to_new_links_and_drive_the_request(self):
        self.prepare(FIRST)
        self.assertIn('720p', self.rows[FIRST].text(VIDEO))
        preset = dict(container='MKV', video='1080', audio='fr', subtitles=['fr', 'en'])
        with patch.object(self.a.preferences, 'setValue') as saved, patch.object(self.a.preferences, 'sync'):
            self.a._save_format_defaults(preset)
        key, payload = saved.call_args.args
        self.assertEqual(key, 'format_defaults')
        with patch.object(self.a.preferences, 'value', return_value=payload):
            self.assertEqual(self.a._read_format_defaults(), preset)

        self.prepare(SECOND)
        second = self.rows[SECOND]
        self.assertIn('1080p', second.text(VIDEO))
        self.assertIn('Français', second.text(AUDIO))
        self.assertIn('Français', second.text(SUBTITLES))
        self.assertIn('Anglais', second.text(SUBTITLES))
        self.assertIn('720p', self.rows[FIRST].text(VIDEO))
        self.a.download_button.click()
        request = self.download.call_args.args[0]
        self.assertEqual(request.format_id, '1080+dub128')
        self.assertEqual(set(request.subtitle_languages.split(',')), {'fr', 'en-orig'})
        self.assertEqual(request.container, 'mkv')

    def test_audio_and_subtitle_provenance_and_subtitle_group_toggle(self):
        fixture = copy.deepcopy(DATA)
        fixture['formats'].append(dict(fixture['formats'][-1], format_id='auto-es', language='es',
                                       format_note='automatically dubbed'))
        fixture['subtitles'] = {'de': [{'url': 'https://example.invalid/de.vtt', 'name': 'Deutsch'}]}
        self.prepare(FIRST, fixture)
        for key, heading in (
            (('audio', 'source129'), 'Pistes d’origine et doublages classiques'),
            (('audio', 'auto-es'), 'Doublages automatiques'),
            (('subtitle', 'de'), 'Sous-titres publiés'),
            (('subtitle', 'en-orig'), 'Transcriptions automatiques'),
            (('subtitle', 'fr'), 'Traductions automatiques'),
        ):
            children = self.open_streams(FIRST, AUDIO if key[0] == 'audio' else SUBTITLES)
            self.assertEqual(children[key].parent().text(0), heading)
        subtitles = children['group', 'subtitle']
        subtitles.setCheckState(0, Qt.CheckState.Checked)
        self.assertTrue(self.a._selected_subtitle())
        subtitles.setCheckState(0, Qt.CheckState.Unchecked)
        self.assertIsNone(self.a._selected_subtitle())


if __name__ == '__main__':
    unittest.main()
