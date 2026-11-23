import os
os.environ.setdefault('QT_QPA_PLATFORM','offscreen')
import sys
import threading
import unittest
from pathlib import Path
from unittest.mock import patch, MagicMock
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from PySide6.QtCore import Qt, QEvent, QPoint, QPointF, QMimeData, QTimer
from PySide6.QtGui import QDragEnterEvent, QDropEvent, QHelpEvent
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QMainWindow, QApplication, QLabel, QListWidget, QLineEdit, QDialog, QPushButton, QToolTip, QWidget
import youtube_downloader_gui as m
from qt_window import application
from test_features import DATA


class QtInterface(unittest.TestCase):
    def setUp(self):
        self.analysis_patch = patch.object(m.DownloadOrchestrator, 'analyze', return_value=MagicMock())
        self.analysis_patch.start()
        self.addCleanup(self.analysis_patch.stop)
        self.window=m.YoutubeDownloaderApp()
        self.window._process_analysis(DATA)
        self.window.show()
        application().processEvents()

    def tearDown(self):
        self.window.dispose()
        application().processEvents()

    def test_native_qt_and_options_menu(self):
        a=self.window
        self.assertIsInstance(a,QMainWindow)
        self.assertEqual(a.windowTitle(),'Téléchargeur YouTube')
        self.assertFalse(a.windowIcon().isNull())
        self.assertNotIn('tkinter',sys.modules)
        self.assertFalse(hasattr(a,'theme_combo'))
        self.assertEqual(a.options_button.accessibleName(),'Options')
        self.assertEqual([x.text() for x in a.options_menu.actions() if not x.isSeparator()],['Thème','Langue','Dépendances'])
        self.assertFalse(a.options_button.icon().isNull())
        self.assertEqual(a.options_button.text(), '')
        self.assertIs(a.options_button.parentWidget(), a.source_panel)
        flags = a.windowHandle().flags()
        self.assertTrue(flags & Qt.WindowType.WindowTitleHint)
        self.assertTrue(flags & Qt.WindowType.WindowCloseButtonHint)
        self.assertEqual(flags & Qt.WindowType.WindowMinMaxButtonsHint, Qt.WindowType.WindowMinMaxButtonsHint)
        if hasattr(Qt.WindowType, 'ExpandedClientAreaHint'):
            self.assertFalse(flags & Qt.WindowType.ExpandedClientAreaHint)
        self.assertTrue(a.options_button.isVisible())
        QTest.mouseClick(a.options_button, Qt.MouseButton.LeftButton)
        self.assertTrue(a.options_menu.isVisible())
        QTest.keyClick(a.options_menu, Qt.Key.Key_Escape)
        self.assertFalse(a.options_menu.isVisible())
        a.options_button.setFocus()
        QTest.keyClick(a.options_button, Qt.Key.Key_Space)
        self.assertTrue(a.options_menu.isVisible())
        a.options_menu.hide()
        self.assertEqual(a.new_link_button.text(), '+')
        self.assertTrue(a.new_link_button.isVisible())
        self.assertEqual(a.new_link_button.x(), 0)
        self.assertFalse(a.source_panel.findChildren(QLineEdit))
        self.assertFalse(a.source_panel.findChildren(QLabel))
        self.assertNotIn('Pour l’onglet affiché', [label.text() for label in a.findChildren(QLabel)])

    def test_tab_menu_opens_the_diagnostic(self):
        a=self.window
        a._url_link_submitted('https://youtu.be/3y_v1uDrMrA')
        self.assertIn(a.analysis_diagnostic_action, a.queue_tabs.actions())
        self.assertTrue(a.analysis_diagnostic_action.isEnabled())
        with patch.object(QDialog,'show') as show:
            a.analysis_diagnostic_action.trigger()
        show.assert_called_once()

    def test_video_tabs_show_overflow_counts_and_one_arrow_on_each_needed_side(self):
        a = self.window
        a.resize(640, 530)
        for video_id in ('first123456', 'second12345', 'third123456'):
            a._url_link_submitted('https://youtu.be/' + video_id)
        QTest.qWait(30)
        tabs = a.queue_tabs
        strip = a.video_tab_strip
        add = a.new_link_button
        self.assertFalse(tabs.drawBase())
        preview = tabs.tabButton(0, tabs.ButtonPosition.LeftSide)
        self.assertEqual(preview.title.width(), 2 * preview.thumbnail.width())
        self.assertEqual(add.y(), tabs.tabRect(0).top())
        self.assertEqual(add.height(), tabs.tabRect(0).height())
        holder = tabs.tabButton(0, tabs.ButtonPosition.RightSide)
        close = holder.findChild(QPushButton, 'videoTabClose')
        self.assertEqual(close.geometry().top(), 0)
        self.assertEqual(close.geometry().right(), holder.rect().right())
        tabs.setCurrentIndex(0)
        self.assertTrue(strip.left_button.isHidden())
        self.assertTrue(strip.right_button.isVisible())
        self.assertEqual(strip.right_button.text(), '+1 ›')
        self.assertGreater(add.x(), strip.viewport.geometry().right())
        self.assertLess(add.geometry().right(), strip.right_button.x())
        self.assertTrue(strip.left_fade.isHidden())
        self.assertTrue(strip.right_fade.isVisible())

        QTest.mouseClick(strip.right_button, Qt.MouseButton.LeftButton)
        self.assertEqual(tabs.currentIndex(), 0)
        self.assertTrue(strip.left_button.isVisible())
        self.assertEqual(strip.left_button.text(), '‹')
        self.assertEqual(strip.right_button.text(), '+1 ›')
        self.assertTrue(strip.left_fade.isVisible())
        self.assertTrue(strip.right_fade.isHidden())

        tabs.setCurrentIndex(tabs.count() - 1)
        self.assertEqual(strip.left_button.text(), '‹ +1')
        self.assertTrue(strip.right_button.isHidden())
        self.assertTrue(strip.right_fade.isHidden())
        self.assertTrue(strip.left_fade.isVisible())

        QTest.mouseClick(strip.left_button, Qt.MouseButton.LeftButton)
        self.assertEqual(tabs.currentIndex(), tabs.count() - 1)
        self.assertTrue(strip.right_button.isVisible())

        a.resize(1250, 530)
        tabs.setCurrentIndex(0)
        self.assertTrue(strip.right_button.isVisible())
        self.assertEqual(strip.right_button.text(), '›')

        a.resize(1850, 530)
        self.assertTrue(strip.left_button.isHidden())
        self.assertTrue(strip.right_button.isHidden())
        self.assertTrue(strip.left_fade.isHidden())
        self.assertTrue(strip.right_fade.isHidden())
        last_tab = tabs.tabRect(tabs.count() - 1)
        self.assertEqual(add.x(), last_tab.right() + 1 + strip.add_gap)

    def test_scrollbar_and_bottom_cue_show_more_content(self):
        a=self.window
        a.resize(680,560)
        a.advanced_check.click()
        application().processEvents()
        bar=a.scroll_area.verticalScrollBar()
        self.assertGreater(bar.maximum(),0)
        self.assertTrue(a.scroll_fade.isVisible())
        self.assertGreaterEqual(bar.width(),14)
        self.assertEqual(a.scroll_area.horizontalScrollBarPolicy(),Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        bar.setValue(bar.maximum())
        application().processEvents()
        self.assertFalse(a.scroll_fade.isVisible())

    def test_analysis_shows_only_loading_text_then_settings(self):
        a=self.window
        a._url_link_submitted('https://youtu.be/3y_v1uDrMrA')
        application().processEvents()
        self.assertTrue(a.analysis_panel.isVisible())
        self.assertTrue(a.video_settings_panel.isHidden())
        self.assertFalse(a.download_button.isVisible())
        self.assertEqual([label.text() for label in a.analysis_panel.findChildren(QLabel)
                          if label.isVisible()], ['Analyse en cours…'])
        self.assertTrue(a.selected_video_panel.isVisible())
        self.assertEqual(a.selected_video_panel.property('videoUrl'), a.url_var.get())
        preview = a.queue_tabs.tabButton(0, a.queue_tabs.ButtonPosition.LeftSide)
        self.assertTrue(preview.title.text().startswith('https://'))
        spinners = [a.analysis_spinner, preview.spinner]
        self.assertTrue(all(spinner.isVisible() for spinner in spinners))
        frames = [spinner.grab().toImage() for spinner in spinners]
        QTest.qWait(120)
        for spinner, frame in zip(spinners, frames):
            self.assertNotEqual(spinner.grab().toImage(), frame)
        a.after_cancel(a.analysis_job)
        a._start_analysis()
        a.events.put(('analysis_ok', a.analysis_token, dict(DATA, description='Description de test')))
        a._poll_events()
        application().processEvents()
        self.assertTrue(a.analysis_panel.isHidden())
        self.assertFalse(preview.spinner.isVisible())
        self.assertTrue(a.status_label.isHidden())
        self.assertTrue(a.video_settings_panel.isVisible())
        self.assertTrue(a.download_button.isVisible())
        self.assertIsNone(a.video_panel.findChild(QWidget, 'videoDescription'))

    def test_first_paste_creates_a_tab_and_next_drop_joins_the_queue(self):
        a=self.window
        pasted='https://www.youtube.com/watch?v=new1234567'
        clipboard=application().clipboard()
        previous=clipboard.text()
        try:
            clipboard.setText(pasted)
            a.new_link_button.setFocus()
            QTest.keyClick(a.new_link_button,Qt.Key.Key_V,Qt.KeyboardModifier.ControlModifier)
            self.assertEqual(a.url_var.get(),pasted)
            self.assertEqual(a.active_queue_url,pasted)
            self.assertEqual(a.queue_tabs.count(),1)
            self.assertIn('Analyse en cours',a.queue_tabs.accessibleTabName(0))
            dropped='https://youtu.be/drop1234567'
            mime=QMimeData();mime.setText(dropped)
            enter=QDragEnterEvent(QPoint(3,3),Qt.DropAction.CopyAction,mime,
                                  Qt.MouseButton.LeftButton,Qt.KeyboardModifier.NoModifier)
            event=QDropEvent(QPointF(3,3),Qt.DropAction.CopyAction,mime,
                             Qt.MouseButton.LeftButton,Qt.KeyboardModifier.NoModifier)
            QApplication.sendEvent(a.source_panel, enter)
            QApplication.sendEvent(a.source_panel, event)
            self.assertTrue(event.isAccepted())
            self.assertEqual(a.url_var.get(),pasted)
            self.assertEqual(a.pending_urls,[dropped])
            self.assertEqual(a.queue_tabs.count(),2)
            self.assertIn('Analyse prévue',a.queue_tabs.accessibleTabName(1))
        finally:
            clipboard.setText(previous)

    def test_pending_tab_can_be_opened_before_download_starts(self):
        a=self.window
        current='https://youtu.be/current1234'
        queued='https://youtu.be/queued12345'
        a._url_link_submitted(current)
        a._url_link_submitted(queued)

        a.queue_tabs.setCurrentIndex(1)
        a._queue_tab_clicked(1)
        application().processEvents()

        self.assertEqual(a.url_var.get(),queued)
        self.assertEqual(a.active_queue_url,current)
        self.assertEqual(a.pending_urls,[queued])
        self.assertEqual(a.queue_tabs.currentIndex(),1)
        a._queue_tab_clicked(0)
        self.assertEqual(a.url_var.get(),current)
        self.assertEqual(a.queue_tabs.currentIndex(),0)
        self.assertIn('À préparer',a.queue_tabs.accessibleTabName(0))

    def test_first_analysis_survives_switch_to_second_tab(self):
        a=self.window
        first='https://youtu.be/current1234'
        second='https://youtu.be/queued12345'
        a._url_link_submitted(first)
        if a.analysis_job:
            a.after_cancel(a.analysis_job);a.analysis_job=None
        a.active_analysis_key=(first,a.access_var.get())
        a.analysis_running=True
        with patch.object(a,'_schedule_queue_analysis'):
            a._url_link_submitted(second)
            a._queue_tab_clicked(1)
        self.assertEqual(a.url_var.get(),second)
        a.events.put(('analysis_ok',a.analysis_token,DATA))
        a._poll_events()
        self.assertIn((first,a.access_var.get()),a.analysis_cache)
        self.assertEqual(a.url_var.get(),second)
        a._queue_tab_clicked(0)
        self.assertEqual(a.url_var.get(),first)
        self.assertEqual(a.analysis_data,DATA)
        self.assertEqual(a.title_var.get(),DATA['title'])

    def test_switch_before_first_analysis_starts_keeps_it_in_queue(self):
        a=self.window
        first='https://youtu.be/current1234'
        second='https://youtu.be/queued12345'
        with patch.object(m.DownloadOrchestrator,'analyze',return_value=MagicMock()):
            a._url_link_submitted(first)
            self.assertIsNotNone(a.analysis_job)
            a._url_link_submitted(second)
            a._queue_tab_clicked(1)
            self.assertIsNone(a.analysis_job)
            self.assertIn(first,a.queue_analysis_waiting)
            a.queue_analysis_orchestrator.engine.events.put(
                ('analysis_ok',a.queue_analysis_orchestrator.engine.analysis_token,DATA))
            a._poll_queue_analysis_events()
            self.assertEqual(a.queue_analysis_url,first)
            a.queue_analysis_orchestrator.engine.events.put(
                ('analysis_ok',a.queue_analysis_orchestrator.engine.analysis_token,DATA))
            a._poll_queue_analysis_events()
        a._queue_tab_clicked(0)
        self.assertEqual(a.url_var.get(),first)
        self.assertEqual(a.analysis_data,DATA)

    def test_drop_on_video_card_adds_tab(self):
        a=self.window
        first='https://youtu.be/current1234'
        second='https://youtu.be/queued12345'
        a._url_link_submitted(first)
        mime=QMimeData();mime.setText(second)
        enter=QDragEnterEvent(QPoint(3,3),Qt.DropAction.CopyAction,mime,
                             Qt.MouseButton.LeftButton,Qt.KeyboardModifier.NoModifier)
        drop=QDropEvent(QPointF(3,3),Qt.DropAction.CopyAction,mime,
                        Qt.MouseButton.LeftButton,Qt.KeyboardModifier.NoModifier)
        with patch.object(a,'_schedule_queue_analysis') as analyze:
            QApplication.sendEvent(a.video_panel,enter)
            QApplication.sendEvent(a.video_panel,drop)
        self.assertTrue(enter.isAccepted())
        self.assertTrue(drop.isAccepted())
        self.assertEqual(a.url_var.get(),first)
        self.assertEqual(a.pending_urls,[second])
        analyze.assert_called_once_with(second)

    def test_downloading_second_tab_keeps_first_in_queue(self):
        a=self.window
        first='https://youtu.be/current1234'
        second='https://youtu.be/queued12345'
        a._url_link_submitted(first)
        a.analysis_cache[(second,a.access_var.get())]=(m.time.monotonic(),DATA)
        with patch.object(a,'_schedule_queue_analysis'):
            a._url_link_submitted(second)
        a._queue_tab_clicked(1)
        with patch.object(a,'_request_download_folder',return_value=str(Path.cwd())), \
             patch.object(a.orchestrator,'download',return_value=MagicMock()):
            a._start_download()
        self.assertEqual(a.active_queue_url,second)
        self.assertEqual(a.pending_urls,[first])
        self.assertEqual(a.last_download_request[0],second)

    def test_drop_during_download_creates_a_separate_queue_tab(self):
        a=self.window
        current='https://youtu.be/current1234'
        queued='https://youtu.be/queued12345'
        a.url_var.set(current)
        a.last_download_request=(current,'D:/Vidéos','video','137+140',None,'mkv',None)
        a.active_queue_url=current
        a.download_thread=MagicMock()
        a._render_download_queue()
        title=a.title_var.get()

        mime=QMimeData();mime.setText(queued)
        enter=QDragEnterEvent(QPoint(3,3),Qt.DropAction.CopyAction,mime,
                              Qt.MouseButton.LeftButton,Qt.KeyboardModifier.NoModifier)
        event=QDropEvent(QPointF(3,3),Qt.DropAction.CopyAction,mime,
                         Qt.MouseButton.LeftButton,Qt.KeyboardModifier.NoModifier)
        with patch.object(a,'_schedule_queue_analysis') as analyze:
            QApplication.sendEvent(a.queue_tabs, enter)
            QApplication.sendEvent(a.queue_tabs, event)
        application().processEvents()

        self.assertTrue(event.isAccepted())
        self.assertEqual(a.url_var.get(),current)
        self.assertEqual(a.pending_urls,[queued])
        self.assertEqual(a.queue_tabs.count(),2)
        self.assertIn('Téléchargement',a.queue_tabs.accessibleTabName(0))
        self.assertTrue(a.queue_tabs.accessibleTabName(1).startswith('Analyse'))
        self.assertEqual(a.title_var.get(),title)
        analyze.assert_called_once_with(queued)

    def test_link_added_during_download_is_analyzed_immediately(self):
        a=self.window
        current='https://youtu.be/current1234'
        queued='https://youtu.be/queued12345'
        a.url_var.set(current)
        active_data = dict(DATA, title='Vidéo en téléchargement', description='Description du transfert actif')
        a._process_analysis(active_data)
        a.active_download_data = active_data
        a.last_download_request=(current,'D:/Vidéos','video','137+140',None,'mkv',None)
        a.active_queue_url=current
        a.download_thread=MagicMock()

        with patch.object(m.DownloadOrchestrator,'analyze',return_value=MagicMock()) as analyze:
            a._url_link_submitted(queued)

        analyze.assert_called_once()
        self.assertEqual(analyze.call_args.args,(queued,))
        self.assertEqual(a.queue_analysis_url,queued)
        self.assertIn('Analyse',a.queue_tabs.accessibleTabName(1))

        QTest.keyClick(a.queue_tabs, Qt.Key.Key_Right)
        application().processEvents()
        self.assertTrue(a.analysis_panel.isVisible())
        self.assertTrue(a.video_settings_panel.isHidden())

        tab_data = dict(DATA, title='Vidéo consultée')
        a.queue_analysis_orchestrator.engine.events.put(
            ('analysis_ok',a.queue_analysis_orchestrator.engine.analysis_token,tab_data))
        a._poll_queue_analysis_events()

        self.assertIn((queued,a.access_var.get()),a.analysis_cache)
        self.assertIsNone(a.queue_analysis_url)
        self.assertIn('Analysé',a.queue_tabs.accessibleTabName(1))
        self.assertEqual(a.download_queue_list.count(), 0)

        application().processEvents()
        self.assertEqual(a.url_var.get(),queued)
        self.assertEqual(a.queue_tabs.currentIndex(),1)
        self.assertEqual(a.analysis_data,tab_data)
        self.assertIsNotNone(a.download_thread)
        self.assertTrue(a.analysis_panel.isHidden())
        self.assertTrue(a.video_settings_panel.isVisible())
        self.assertTrue(a.queue_tabs.accessibleTabName(1).startswith('Vidéo consultée'))
        self.assertEqual(a.selected_video_title.text(), 'Vidéo consultée')
        self.assertEqual(a.selected_video_panel.property('videoUrl'), queued)
        self.assertEqual(a.download_button.text(), 'Ajouter à la file')
        self.assertFalse(a.transfer_panel.isHidden())
        self.assertEqual(a.transfer_title_label.text(), 'Vidéo en téléchargement')
        self.assertTrue(a.queue_tabs.accessibleTabName(0).startswith('Vidéo en téléchargement'))
        a._show_queue_tab(current)
        self.assertFalse(a.transfer_panel.isHidden())
        self.assertEqual(a.selected_video_title.text(), 'Vidéo en téléchargement')

    def test_selected_video_identity_follows_tabs_and_ignores_other_thumbnails(self):
        from test_thumbnails import image_bytes
        a = self.window
        first, second = 'https://youtu.be/aaaaaaaaaaa', 'https://youtu.be/bbbbbbbbbbb'
        a.url_var.set(first)
        data = dict(DATA, id='aaaaaaaaaaa', title='Première vidéo', channel='Chaîne A', duration=65)
        a._process_analysis(data)
        a.active_queue_url = first
        a.pending_urls = [second]
        a.tab_analysis_data[(second, a.access_var.get())] = dict(
            DATA, id='bbbbbbbbbbb', title='Deuxième vidéo', channel='Chaîne B', duration=125)
        blue = image_bytes('#2050ff')
        a.thumbnail_cache['bbbbbbbbbbb'] = blue
        a._show_queue_tab(second)
        self.assertEqual(a.selected_video_title.text(), 'Deuxième vidéo')
        self.assertEqual(a.selected_video_meta.text(), 'Chaîne B · 2:05')
        self.assertEqual(a.selected_video_url.text(), second)
        self.assertEqual(a.selected_video_panel.property('videoUrl'), second)
        self.assertEqual(a.selected_video_thumbnail._thumbnail_content, blue)
        a.events.put(('thumbnail', 'aaaaaaaaaaa', image_bytes('#ff0000')))
        a._poll_events()
        self.assertEqual(a.selected_video_title.text(), 'Deuxième vidéo')
        self.assertEqual(a.selected_video_thumbnail._thumbnail_content, blue)
        a._show_queue_tab(first)
        self.assertEqual(a.selected_video_title.text(), 'Première vidéo')
        self.assertEqual(a.selected_video_meta.text(), 'Chaîne A · 1:05')
        self.assertEqual(a.selected_video_panel.property('videoUrl'), first)
        a.resize(680, 530)
        application().processEvents()
        self.assertTrue(a.selected_video_title.isVisible())
        self.assertTrue(a.selected_video_url.isHidden())
        self.assertIn(first, a.selected_video_panel.toolTip())
        a.resize(900, 810)
        application().processEvents()
        self.assertTrue(a.selected_video_url.isVisible())
        self.assertEqual(a.selected_video_title.text(), 'Première vidéo')

    def test_all_paste_paths_add_url_during_download_without_changing_worker(self):
        a = self.window
        current = 'https://youtu.be/current1234'
        a.url_var.set(current)
        if a.analysis_job is not None:
            a.after_cancel(a.analysis_job)
            a.analysis_job = None
        a._process_analysis(DATA)
        a.active_queue_url = a.queue_view_url = current
        original = (current, 'D:/Vidéos', 'video', '137+140', None, 'mkv', None)
        a.last_download_request = original
        worker = a.download_thread = MagicMock()
        clipboard = application().clipboard()
        previous = clipboard.text()
        urls = ['https://youtu.be/keyboard123', 'https://youtu.be/context1234']
        try:
            with patch.object(a.orchestrator, 'cancel') as cancel, \
                    patch.object(a.orchestrator, 'download') as restart:
                for index, url in enumerate(urls):
                    with self.subTest(method=index):
                        clipboard.setText(url)
                        a.queue_tabs.setFocus()
                        if index == 0:
                            QTest.keyClick(a.queue_tabs, Qt.Key.Key_V, Qt.KeyboardModifier.ControlModifier)
                        else:
                            self.assertIn(a.paste_video_link_action, a.queue_tabs.actions())
                            a.paste_video_link_action.trigger()
                        application().processEvents()
                        self.assertIn(url, a.pending_urls)
                        self.assertEqual(a.url_var.get(), current)
                        self.assertIs(a.download_thread, worker)
                        self.assertIs(a.last_download_request, original)
                self.assertEqual(a.pending_urls, urls)
                self.assertEqual(a.queue_tabs.count(), 3)
                cancel.assert_not_called()
                restart.assert_not_called()
        finally:
            clipboard.setText(previous)

    def test_typed_url_can_be_added_with_enter_while_downloading(self):
        a = self.window
        current = 'https://youtu.be/current1234'
        queued = 'https://youtu.be/typedlink12'
        a.url_var.set(current)
        a._process_analysis(DATA)
        a.active_queue_url = a.queue_view_url = current
        a.last_download_request = (current, 'D:/Vidéos', 'video', '137+140', None, 'mkv', None)
        worker = a.download_thread = MagicMock()
        def submit_link():
            dialog = QApplication.activeModalWidget()
            entry = dialog.findChild(QLineEdit)
            QTest.keyClicks(entry, queued)
            QTest.keyClick(entry, Qt.Key.Key_Return)
        with patch.object(a.orchestrator, 'cancel') as cancel:
            QTimer.singleShot(0, submit_link)
            QTest.mouseClick(a.new_link_button, Qt.MouseButton.LeftButton)
            application().processEvents()
            self.assertIn(queued, a.pending_urls)
            self.assertEqual(a.url_var.get(), queued)
            self.assertIs(a.download_thread, worker)
            self.assertNotIn(queued, a.queue_tab_settings)
            cancel.assert_not_called()

    def test_new_link_button_is_available_during_download(self):
        a = self.window
        current = 'https://youtu.be/current1234'
        queued = 'https://youtu.be/newdialog12'
        a.url_var.set(current)
        a._process_analysis(DATA)
        a.active_queue_url = a.queue_view_url = current
        a.active_download_data = DATA
        original = (current, 'D:/Vidéos', 'video', '137+140', None, 'mkv', None)
        a.last_download_request = original
        worker = a.download_thread = MagicMock()
        with patch.object(a, '_request_new_link', return_value=(queued, True)), \
                patch.object(a.orchestrator, 'cancel') as cancel, \
                patch.object(a.orchestrator, 'download') as restart:
            self.assertTrue(a.new_link_button.isEnabled())
            QTest.mouseClick(a.new_link_button, Qt.MouseButton.LeftButton)
            application().processEvents()
            self.assertIn(queued, a.pending_urls)
            self.assertEqual(a.url_var.get(), queued)
            self.assertEqual(a.active_queue_url, current)
            self.assertIs(a.download_thread, worker)
            self.assertIs(a.last_download_request, original)
            self.assertIs(a.active_download_data, DATA)
            cancel.assert_not_called()
            restart.assert_not_called()

    def test_transfer_panel_shows_the_cancel_button_during_download(self):
        a=self.window
        a.resize(640, 530)
        a.ytdlp = 'yt-dlp.exe'
        current='https://youtu.be/current1234'
        a.url_var.set(current)
        a._process_analysis(DATA)
        a.last_download_request=(current,'D:/Vidéos','video','137+140',None,'mkv',None)
        a.active_queue_url=current
        a.queue_view_url=current
        a.download_thread=MagicMock()
        a.download_progress.start()
        a.cancel_button.setEnabled(True)
        a._render_download_queue()
        application().processEvents()

        origin = QPoint()
        row = a.transfer_panel
        self.assertLess(a.transfer_title_label.mapTo(row, origin).x(),
                        a.transfer_status_label.mapTo(row, origin).x())
        self.assertLess(a.transfer_status_label.mapTo(row, origin).x(),
                        a.cancel_button.mapTo(row, origin).x())
        self.assertEqual(a.transfer_status_label.mapTo(row, origin).x(),
                         a.download_progress.mapTo(row, origin).x())
        self.assertTrue(a.cancel_button.isVisible())
        self.assertGreaterEqual(a.cancel_button.width(), 110)
        for button in (a.resume_download_button, a.skip_download_button,
                       a.open_download_button, a.pause_download_button, a.cancel_button):
            self.assertEqual(button.height(), a.download_button.height())
        self.assertEqual(a.pause_download_button.width(), a.pause_download_button.height())
        self.assertTrue(a.download_queue_panel.isAncestorOf(a.cancel_button))
        self.assertFalse(a.video_panel.isAncestorOf(a.cancel_button))
        self.assertEqual(a.cancel_button.accessibleName(),'Annuler le téléchargement en cours')
        preview = a.queue_tabs.tabButton(0, a.queue_tabs.ButtonPosition.LeftSide)
        self.assertTrue(preview.state.isHidden())
        self.assertTrue(preview.download_progress.isVisible())
        self.assertLessEqual(preview.download_progress.height(), a.download_progress.height() // 3)
        self.assertLess(preview.download_progress.width(), preview.title.width())
        self.assertFalse(preview.download_progress.isTextVisible())
        self.assertEqual(preview.download_progress.maximum(), 0)
        a.events.put(('download_progress', 42, '42 Mo / 100 Mo'))
        a._poll_events()
        self.assertEqual(preview.download_progress.maximum(), 100)
        self.assertEqual(preview.download_progress.value(), 42)
        self.assertIsNotNone(a.download_thread)
        self.assertEqual(a.transfer_status_label.text(), '42 Mo / 100 Mo')
        with patch.object(a.orchestrator,'cancel') as cancel:
            QTest.mouseClick(a.cancel_button,Qt.MouseButton.LeftButton)
        cancel.assert_called_once_with()
        self.assertFalse(a.cancel_button.isEnabled())
        self.assertEqual(a.cancel_button.text(),'Annulation…')
        a.events.put(('download_cancelled',))
        a._poll_events()
        application().processEvents()
        self.assertTrue(a.transfer_panel.isHidden())
        self.assertTrue(preview.download_progress.isHidden())
        self.assertTrue(a.download_queue_empty_label.isVisible())
        self.assertEqual(a._video_tab_urls(), [current])
        self.assertEqual(a.url_var.get(), current)
        self.assertEqual(a.queue_tabs.count(), 1)
        self.assertTrue(preview.thumbnail.isVisible())
        self.assertTrue(a.video_settings_panel.isVisible())
        self.assertTrue(a.download_button.isEnabled())

    def test_queue_opens_and_analyzes_next_link_after_completion(self):
        a=self.window
        current='https://youtu.be/current1234'
        queued='https://youtu.be/queued12345'
        a.url_var.set(current)
        if a.analysis_job:
            a.after_cancel(a.analysis_job);a.analysis_job=None
        a.last_download_request=(current,str(Path.cwd()),'video','137+140',None,'mkv',None)
        a.active_queue_url=current
        a.pending_urls=[queued]
        a.download_thread=MagicMock()
        a.events.put(('download_done',[],str(Path.cwd())))

        a._poll_events();application().processEvents()

        self.assertEqual(a.url_var.get(),queued)
        self.assertEqual(a.active_queue_url,queued)
        self.assertEqual(a.pending_urls,[])
        self.assertIsNotNone(a.analysis_job)
        self.assertIn('Analyse en cours',a.queue_tabs.accessibleTabName(0))

    def test_url_is_preserved_when_multiple_subtitles_are_selected(self):
        a=self.window
        url='https://www.youtube.com/watch?v=3y_v1uDrMrA'
        a._url_link_submitted(url)
        self.assertEqual(a.url_var.get(),url)
        a._process_analysis(DATA)
        a._set_selected_subtitles(['en-orig','fr'])
        self.assertEqual(a._selected_subtitle(),'en-orig,fr')
        self.assertIn('2 langues',a.subtitle_combo.text())
        self.assertEqual(a.url_var.get(),url)

    def test_subtitle_picker_hides_extra_automatic_tracks_but_searches_them(self):
        a=self.window
        data=dict(DATA,subtitles={'de':[{'url':'https://example.invalid/de','name':'German'}]},
            automatic_captions={
                'en-orig':[{'url':'https://example.invalid/en?lang=en','name':'English'}],
                'fr':[{'url':'https://example.invalid/fr?lang=en&tlang=fr','name':'French'}],
                'es':[{'url':'https://example.invalid/es?lang=en&tlang=es','name':'Spanish'}],
                'it':[{'url':'https://example.invalid/it?lang=en&tlang=it','name':'Italian'}],
                'ja':[{'url':'https://example.invalid/ja?lang=en&tlang=ja','name':'Japanese'}],
            })
        a._process_analysis(data)
        observed={}
        def inspect(dialog):
            subtitle_list=dialog.findChild(QListWidget)
            search=next(field for field in dialog.findChildren(QLineEdit) if 'Rechercher' in field.placeholderText())
            observed['initial']=subtitle_list.count()
            search.setText('espagnol')
            application().processEvents()
            observed['filtered']=[subtitle_list.item(i).text() for i in range(subtitle_list.count())]
            subtitle_list.item(0).setCheckState(Qt.CheckState.Checked)
            return QDialog.DialogCode.Accepted
        with patch.object(QDialog,'exec',inspect):
            a._open_subtitle_picker()
        self.assertLess(observed['initial'],len(a.subtitle_tracks))
        self.assertEqual(len(observed['filtered']),1)
        self.assertIn('espagnol — traduction auto depuis l’anglais',observed['filtered'][0])
        self.assertIn('es',a._selected_subtitle_codes())

    def test_theme_actions_update_palette_without_changing_selection(self):
        a=self.window
        selected=a._selection()['format']
        with patch.object(a.preferences,'setValue'):
            a.theme_actions['Clair'].trigger()
            light=a.palette().window().color().lightness()
            a.theme_actions['Sombre'].trigger()
            dark=a.palette().window().color().lightness()
        self.assertGreater(light,dark)
        self.assertTrue(a.theme_actions['Sombre'].isChecked())
        self.assertEqual(a._selection()['format'],selected)

    def test_compact_window_keeps_download_actions_visible(self):
        a=self.window;a.resize(680,560)
        a.advanced_check.click()
        application().processEvents()
        self.assertFalse(a.advanced_panel.isHidden())
        self.assertFalse(a.audio_frame.isHidden())
        bottom=a.download_button.mapTo(a,a.download_button.rect().bottomRight())
        self.assertLessEqual(bottom.y(),a.height())
        self.assertLessEqual(bottom.x(),a.width())
        self.assertFalse(hasattr(a,'folder_frame'))
        # An odd number of available pixels leaves one column a pixel wider.
        self.assertLessEqual(abs(a.height_combo.width()-a.subtitle_combo.width()),1)
        a.mode_buttons['subtitles'].click()
        self.assertTrue(a.advanced_panel.isHidden())
        self.assertTrue(a.size_label.isHidden())
        self.assertLess(a.download_button.width(),a.width()//2)
        self.assertEqual(a._selected_subtitle(),'en-orig')

    def test_size_estimate_and_download_button_are_fixed_below_video_settings(self):
        a=self.window
        a.resize(880,760)
        a.size_var.set('Taille estimée : ≈ 1.4 Go')
        application().processEvents()
        self.assertIs(a.size_label.parentWidget(), a.video_action_panel)
        self.assertIs(a.download_button.parentWidget(), a.video_action_panel)
        self.assertIs(a.video_action_panel.parentWidget(), a.video_panel)
        self.assertEqual(a.video_action_panel.geometry().top(), a.scroll_area.geometry().bottom() + 1)
        self.assertIs(a.transfer_panel.parentWidget(), a.download_queue_panel)
        self.assertFalse(a.video_panel.isAncestorOf(a.transfer_panel))
        self.assertIs(a.rate_limit_spinbox.parentWidget(), a.download_panel)
        self.assertIs(a.download_queue_panel.parentWidget(), a.download_panel)
        self.assertIs(a.download_panel.parentWidget(), a.centralWidget())
        self.assertEqual(a.download_panel.geometry().top(), a.video_panel.geometry().bottom() + 1)
        self.assertTrue(a.download_queue_panel.isVisible())
        required=a.size_label.fontMetrics().horizontalAdvance(a.size_label.text())
        self.assertGreaterEqual(a.size_label.width(),required)

    def test_speed_limit_remains_editable_during_download(self):
        a=self.window
        try:
            a.download_thread=object()
            with patch.object(a.preferences,'setValue') as save:
                a.rate_unit_combo.setCurrentText('Mbit/s')
                a.rate_limit_spinbox.setValue(0)
                a.rate_limit_spinbox.setValue(7)
                self.assertTrue(a.rate_limit_spinbox.isEnabled())
                self.assertEqual(a.download_rate_bps,875000)
                save.assert_any_call('download_rate_bps',875000)
                self.assertIn('7 Mbit/s',a.progress_detail_var.get())

                revision=a.download_rate_revision
                a.rate_unit_combo.setCurrentText('ko/s')
                self.assertEqual(a.rate_limit_spinbox.value(),875)
                self.assertEqual(a.download_rate_bps,875000)
                self.assertEqual(a.download_rate_revision,revision)
                a.rate_limit_spinbox.setValue(500)
                self.assertEqual(a.download_rate_bps,500000)
                a.rate_unit_combo.setCurrentText('Mo/s')
                self.assertEqual(a.rate_limit_spinbox.value(),0.5)
                a.rate_unit_combo.setCurrentText('Mbit/s')
                self.assertEqual(a.rate_limit_spinbox.value(),4)
                a.rate_unit_combo.setCurrentText('ko/s')
                a.rate_limit_spinbox.setValue(1)
                self.assertEqual(a.rate_limit_spinbox.value(),10)
                self.assertEqual(a.download_rate_bps,10000)
                QTest.mouseClick(a.unlimited_rate_button,Qt.MouseButton.LeftButton)
                self.assertEqual(a.download_rate_bps,0)
                self.assertEqual(a.rate_limit_spinbox.value(),0)
                self.assertIn('illimité',a.progress_detail_var.get())
        finally:
            a.download_thread=None

    def test_download_destination_is_requested_only_on_download(self):
        a=self.window
        self.assertFalse(hasattr(a,'folder_frame'))
        with patch('qt_window.QFileDialog.getExistingDirectory',return_value='D:/Vidéos') as choose:
            self.assertEqual(a._request_download_folder(),'D:/Vidéos')
        choose.assert_called_once()
        self.assertEqual(a.folder_var.get(),'D:/Vidéos')

    def test_video_clip_requires_explicit_keyframe_index(self):
        a=self.window
        a.advanced_check.click();application().processEvents()
        self.assertTrue(a.keyframe_index_button.isVisible())
        self.assertTrue(a.keyframe_index_button.isEnabled())
        self.assertFalse(a.clip_slider.isEnabled())
        self.assertIn('Télécharger l’index',a.keyframe_index_button.text())
        self.assertIn('≈',a.keyframe_index_button.text())
        a.mode_buttons['audio'].click();application().processEvents()
        self.assertFalse(a.keyframe_index_button.isVisible())
        self.assertTrue(a.clip_slider.isEnabled())

    def test_keyframe_progress_is_drawn_in_button_not_help_line(self):
        a=self.window
        a.advanced_check.click();application().processEvents()
        fixed_width=a.keyframe_index_button.width()
        a.keyframe_loading=True
        a.keyframe_note='ancien statut'
        a.events.put(('keyframe_progress',a.keyframe_token,'Téléchargement de l’index',42.4))
        a._poll_events();application().processEvents()
        self.assertEqual(a.keyframe_index_button.text(),'Téléchargement de l’index')
        self.assertAlmostEqual(a.keyframe_index_button.progress(),42.4)
        self.assertFalse(a.keyframe_index_button.isIndeterminate())
        self.assertEqual(a.clip_note_var.get(),'')
        self.assertFalse(a.keyframe_index_button.isEnabled())
        self.assertEqual(a.keyframe_index_button.width(),fixed_width)
        a.events.put(('keyframe_progress',a.keyframe_token,'Téléchargement de l’index',None))
        a._poll_events();application().processEvents()
        self.assertEqual(a.keyframe_index_button.width(),fixed_width)
        self.assertAlmostEqual(a.keyframe_index_button.progress(),42.4)
        a.events.put(('keyframe_progress',a.keyframe_token,'Connexion à YouTube','indeterminate'))
        a._poll_events();application().processEvents()
        self.assertTrue(a.keyframe_index_button.isIndeterminate())
        self.assertIsNone(a.keyframe_index_button.progress())
        self.assertNotIn('…',a.keyframe_index_button.text())

    def test_progress_widgets_follow_worker_events(self):
        a=self.window
        self.assertTrue(a.download_progress.isTextVisible())
        self.assertGreaterEqual(a.download_progress.height(),18)
        a.events.put(('download_phase','Téléchargement précis du flux <vidéo>…'))
        a.events.put(('download_progress',42.5,'13.7 Mo / 182.9 Mo', 125))
        a._poll_events()
        self.assertEqual(a.download_progress.value(),42)
        self.assertFalse(a.transfer_panel.isHidden())
        self.assertEqual(a.progress_text_var.get(),'13.7 Mo / 182.9 Mo')
        self.assertEqual(a.download_progress.text(), '42 % · 02:05')
        self.assertNotIn('Journal', [button.text() for button in a.transfer_panel.findChildren(QPushButton)])
        tooltip = a.download_progress.toolTip()
        self.assertIn('13.7 Mo / 182.9 Mo', tooltip)
        self.assertIn('Téléchargement précis du flux &lt;vidéo&gt;', tooltip)
        center = a.download_progress.rect().center()
        event = QHelpEvent(QEvent.Type.ToolTip, center, a.download_progress.mapToGlobal(center))
        QApplication.sendEvent(a.download_progress, event)
        self.assertEqual(QToolTip.text(), tooltip)
        QToolTip.hideText()
        a.events.put(('download_wait',10,60,'Essai de secours · attente réseau 10/60 s'))
        a._poll_events()
        self.assertEqual(a.download_progress.value(),42)
        self.assertEqual(a.download_progress.text(), '42 %')
        self.assertEqual(a.progress_text_var.get(),'13.7 Mo / 182.9 Mo')
        self.assertIn('Essai de secours',a.progress_detail_var.get())
        before=a.progress_detail_var.get()
        a.events.put(('download_wait',0,60,'Attente incorrecte 0/60 s'))
        a._poll_events()
        self.assertEqual(a.progress_detail_var.get(),before)
        for index in range(30):
            a.events.put(('download_phase',f'Ligne technique {index}'))
        a._poll_events();application().processEvents()
        tooltip = a.download_progress.toolTip()
        self.assertIn('Ligne technique 29', tooltip)
        self.assertNotIn('Ligne technique 0', tooltip)
        a.events.put(('download_done',));a._poll_events()
        self.assertEqual(a.download_progress.value(),100)
        self.assertEqual(a.download_progress.text(), '100 %')
        self.assertFalse(a.cancel_button.isEnabled())
        self.assertEqual(a.progress_text_var.get(),'Téléchargement terminé')
        self.assertTrue(a.open_download_button.isVisible())
        self.assertIs(a.open_download_button.menu(), a.download_result_menu)
        self.assertFalse(a.download_button.isEnabled())

    def test_cancel_interrupts_the_native_connection_without_blocking(self):
        a=self.window
        a.download_thread=MagicMock()
        a.native_reader=MagicMock()
        interrupted = threading.Event()
        a.native_reader.cancel.side_effect = interrupted.set
        a.cancel_button.setEnabled(True)
        a._cancel_download()
        self.assertTrue(interrupted.wait(5))
        a.native_reader.cancel.assert_called_once_with()
        self.assertTrue(a.cancel_requested)
        self.assertFalse(a.cancel_button.isEnabled())
        self.assertEqual(a.cancel_button.text(),'Annulation…')

    def test_recovered_download_warning_does_not_replace_the_current_phase(self):
        a=self.window
        a.progress_text_var.set('Connexion à YouTube…')
        a.events.put(('download_status','WARNING: Unable to download webpage: HTTP Error 429'))
        a._poll_events()
        self.assertEqual(a.progress_text_var.get(),'Connexion à YouTube…')
        a.events.put(('download_status','[info] Downloading 1 format(s): 136+251'))
        a._poll_events()
        self.assertEqual(a.progress_text_var.get(),'Connexion à YouTube…')
        self.assertIn('Préparation des flux…',a.progress_detail_var.get())
        a.events.put(('download_phase','Coupe sans réencodage…'))
        a._poll_events()
        self.assertEqual(a.download_progress.maximum(),100)
        self.assertEqual(a.progress_text_var.get(),'Connexion à YouTube…')
        self.assertIn('Coupe sans réencodage…',a.progress_detail_var.get())

    def test_settings_summary_keeps_its_position_between_media_modes(self):
        a=self.window
        self.assertGreaterEqual(a.selection_frame.height(),a.filter_frame.sizeHint().height())
        self.assertGreaterEqual(a.selection_frame.height(),a.subtitle_frame.sizeHint().height())
        positions={}
        settings_widths={}
        for mode in ('video','video_only','audio'):
            a.mode_var.set(mode);a._mode_changed();application().processEvents()
            positions[mode]=a.auto_summary_label.mapTo(a,a.auto_summary_label.rect().topLeft()).y()
            settings_widths[mode]=a.video_settings_panel.width()
        self.assertEqual(max(positions.values()),min(positions.values()))
        self.assertEqual(len(set(settings_widths.values())),1)


if __name__=='__main__':unittest.main(verbosity=2)
