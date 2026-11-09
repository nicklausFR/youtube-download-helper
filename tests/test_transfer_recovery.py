"""Real yt-dlp transfers against a range-capable local HTTP server."""
import hashlib
import http.server
import json
from pathlib import Path
import queue
import socket
import sys
import tempfile
import threading
import time
import unittest
from unittest.mock import Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from windows_engine import WindowsEngine
import windows_engine as module

ROOT = Path(__file__).resolve().parents[1]


class TransferRecovery(unittest.TestCase):
    def test_import_leaves_partial_intact_after_two_network_timeouts(self):
        engine = WindowsEngine('yt-dlp.exe')
        proc = Mock(returncode=1)
        proc.communicate.return_value = ('', 'ERROR: Unable to download API page: Read timed out.')
        with tempfile.TemporaryDirectory() as work, patch.object(
                module.subprocess, 'Popen', return_value=proc) as launch:
            partial = Path(work) / 'Test.mp4.part'
            original = b'partial content' * 512
            partial.write_bytes(original)
            engine._download_worker('https://youtu.be/aaaaaaaaaaa', work, 'video_only',
                                    '', None, None, partial_path=str(partial))
            self.assertEqual(launch.call_count, 2)
            self.assertEqual(partial.read_bytes(), original)
            self.assertFalse(partial.with_suffix('').exists())
        events = []
        while not engine.events.empty():
            events.append(engine.events.get_nowait())
        self.assertEqual(events[-1][0], 'download_error')
        self.assertIn('après deux tentatives', events[-1][1])
        self.assertNotIn('Read timed out', events[-1][1])
        self.assertIsNone(engine.download_process)

    def test_import_resumes_only_the_matching_stream(self):
        payload = b'correct stream ' * 16000
        prefix_size = 70000
        requests = []

        class Handler(http.server.BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass

            def do_GET(self):
                content = payload if self.path == '/correct' else b'?' * len(payload)
                range_header = self.headers.get('Range')
                requests.append((self.path, range_header))
                start, end = 0, len(content) - 1
                if range_header:
                    first, last = range_header.split('=')[1].split('-')
                    start, end = int(first), int(last) if last else end
                self.send_response(206 if range_header else 200)
                if range_header:
                    self.send_header('Content-Range', f'bytes {start}-{end}/{len(content)}')
                self.send_header('Content-Type', 'video/mp4')
                self.send_header('Content-Length', str(end - start + 1))
                self.end_headers()
                self.wfile.write(content[start:end + 1])

        server = http.server.ThreadingHTTPServer(('127.0.0.1', 0), Handler)
        serving = threading.Thread(target=server.serve_forever, daemon=True)
        serving.start()
        engine = WindowsEngine(str(ROOT / '_dependencies/yt-dlp.exe'))
        original = module.subprocess.Popen
        try:
            with tempfile.TemporaryDirectory(prefix='yd-import-') as work:
                folder = Path(work)
                partial = folder / 'Dropped 100% video.mp4.part'
                partial.write_bytes(payload[:prefix_size])
                base = f'http://127.0.0.1:{server.server_port}'
                info = folder / 'probe.json'
                metadata_commands = []

                def run(*names):
                    info.write_text(json.dumps(dict(
                        id='import-fixture', title='Original title', extractor='generic',
                        extractor_key='Generic', webpage_url=base,
                        formats=[dict(format_id=name, url=f'{base}/{name}', ext='mp4',
                                      vcodec='avc1', acodec='none', protocol='http',
                                      filesize=len(payload)) for name in names],
                    )), encoding='utf-8')
                    engine._download_worker(base, work, 'video_only', '', None, None,
                                            partial_path=str(partial))
                    while not engine.events.empty():
                        event = engine.events.get_nowait()
                        if event[0] in ('download_done', 'download_error'):
                            return event
                    self.fail('No terminal recovery event')

                def launch(command, **kwargs):
                    if command[0] == engine.ytdlp and '--dump-single-json' in command:
                        metadata_commands.append(command)
                        if len(metadata_commands) == 2:
                            slow = Mock(returncode=1)
                            slow.communicate.return_value = (
                                '', 'ERROR: Unable to download API page: Read timed out. (read timeout=10.0)')
                            return slow
                        command = command[:-1] + ['--load-info-json', str(info)]
                    return original(command, **kwargs)

                with patch.object(module.subprocess, 'Popen', side_effect=launch):
                    result = run('incorrect')
                    self.assertEqual(result[0], 'download_error', result)
                    self.assertEqual(partial.read_bytes(), payload[:prefix_size])
                    result = run('correct', 'incorrect')
                    self.assertEqual(result[0], 'download_done', result)
                self.assertEqual(len(metadata_commands), 3)
                self.assertEqual(metadata_commands[-1][-5:-3], ['--socket-timeout', '20'])
                self.assertIn(('/correct', f'bytes={prefix_size}-'), requests)
                self.assertEqual(partial.with_suffix('').read_bytes(), payload)
                self.assertFalse(partial.exists())
        finally:
            engine.cancel()
            server.shutdown()
            server.server_close()
            serving.join(3)

    def test_adding_urls_keeps_real_download_running(self):
        import functools
        from contextlib import contextmanager
        from test_qt_interface import QtInterface
        from qt_window import application
        from PySide6.QtCore import QMimeData, QPoint, QPointF, Qt, QUrl
        from PySide6.QtGui import QDragEnterEvent, QDropEvent
        from PySide6.QtTest import QTest
        from core_selection import DownloadRequest

        case = QtInterface()
        case.setUp()
        self.addCleanup(case.doCleanups)
        self.addCleanup(case.tearDown)
        window = case.window
        source = ROOT / 'tests/fixtures/video.mp4'
        class Handler(http.server.SimpleHTTPRequestHandler):
            def log_message(self, *args):
                pass
        server = http.server.ThreadingHTTPServer(('127.0.0.1', 0),
            functools.partial(Handler, directory=str(source.parent)))
        serving = threading.Thread(target=server.serve_forever, daemon=True)
        serving.start()
        current = 'https://youtu.be/aaaaaaaaaaa'
        added = ['https://youtu.be/bbbbbbbbbbb', 'https://youtu.be/ccccccccccc',
                 'https://youtu.be/ddddddddddd', 'https://youtu.be/eeeeeeeeeee']
        original = module.subprocess.Popen
        with tempfile.TemporaryDirectory(prefix='yd-add-during-transfer-') as work:
            info = Path(work) / 'info.json'
            base = f'http://127.0.0.1:{server.server_port}'
            data = dict(id='add-link-test', title='Adding links fixture', extractor='generic',
                extractor_key='Generic', webpage_url=base,
                formats=[dict(format_id='video', url=base+'/video.mp4', ext='mp4',
                    vcodec='avc1', acodec='none', protocol='http', filesize=source.stat().st_size)])
            info.write_text(json.dumps(data), encoding='utf-8')
            window.url_var.set(current)
            if window.analysis_job is not None:
                window.after_cancel(window.analysis_job)
                window.analysis_job = None
            window._process_analysis(data)
            window.active_queue_url = window.queue_view_url = current
            window.active_download_data = data
            window.last_download_request = (current, work, 'video_only', 'video', None, None, None)
            window.set_download_rate_bps(10_000)
            @contextmanager
            def access(*args, **kwargs):
                yield ['--ignore-config', '--no-plugin-dirs', '--socket-timeout', '3', '--retries', '0']
            commands = []
            def launch(command, **kwargs):
                if command[0] == window.ytdlp:
                    commands.append(list(command))
                    command = command[:-1] + ['--load-info-json', str(info)]
                return original(command, **kwargs)
            clipboard = application().clipboard()
            previous = clipboard.text()
            try:
                with patch.object(window, '_access_options', access), \
                        patch.object(module.subprocess, 'Popen', side_effect=launch):
                    worker = window.orchestrator.download(DownloadRequest(
                        current, work, 'video_only', 'video'), validate=False)
                    window.download_thread = worker
                    deadline = time.monotonic() + 30
                    while window.download_progress_value <= 0 and time.monotonic() < deadline:
                        application().processEvents()
                        time.sleep(0.02)
                    self.assertTrue(worker.is_alive(), 'Transfer ended before the input test')
                    self.assertGreater(window.download_progress_value, 0)
                    process = window.download_process
                    self.assertIsNotNone(process)
                    clipboard.setText(added[0])
                    window.new_link_button.setFocus()
                    QTest.keyClick(window.new_link_button, Qt.Key.Key_V, Qt.KeyboardModifier.ControlModifier)
                    clipboard.setText(added[1])
                    window.paste_video_link_action.trigger()
                    mime = QMimeData()
                    mime.setUrls([QUrl('https://example.invalid/page'), QUrl(added[2])])
                    enter = QDragEnterEvent(QPoint(3, 3), Qt.DropAction.CopyAction, mime,
                                            Qt.MouseButton.LeftButton, Qt.KeyboardModifier.NoModifier)
                    drop = QDropEvent(QPointF(3, 3), Qt.DropAction.CopyAction, mime,
                                      Qt.MouseButton.LeftButton, Qt.KeyboardModifier.NoModifier)
                    application().sendEvent(window.video_panel, enter)
                    application().sendEvent(window.video_panel, drop)
                    self.assertTrue(enter.isAccepted())
                    self.assertTrue(drop.isAccepted())
                    with patch.object(window, '_request_new_link', return_value=(added[3], True)):
                        QTest.mouseClick(window.new_link_button, Qt.MouseButton.LeftButton)
                    self.assertEqual(window.pending_urls, added)
                    self.assertIs(window.download_thread, worker)
                    self.assertIs(window.download_process, process)
                    self.assertIsNone(process.poll())
                    self.assertEqual(len(commands), 1, 'Adding URLs restarted yt-dlp')
                    while window.download_thread is not None and time.monotonic() < deadline:
                        application().processEvents()
                        time.sleep(0.02)
                    worker.join(3)
                    self.assertFalse(worker.is_alive())
                    result = Path(work) / 'Adding links fixture.mp4'
                    self.assertEqual(result.read_bytes(), source.read_bytes())
                    self.assertEqual(len(commands), 1)
            finally:
                clipboard.setText(previous)
                window.orchestrator.cancel()
                if window.orchestrator.download_thread is not None:
                    window.orchestrator.download_thread.join(5)
                server.shutdown()
                server.server_close()
                serving.join(3)

    def test_disconnect_resume_and_two_live_rate_changes_keep_partial_bytes(self):
        payload = bytes(range(256)) * 8192
        ranges = []
        class Handler(http.server.BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass
            def do_GET(self):
                header = self.headers.get('Range', '')
                start = int(header.split('=')[1].split('-')[0]) if header else 0
                ranges.append(start)
                self.send_response(206 if header else 200)
                self.send_header('Content-Type', 'video/mp4')
                self.send_header('Accept-Ranges', 'bytes')
                self.send_header('Content-Length', str(len(payload) - start))
                if header:
                    self.send_header('Content-Range', f'bytes {start}-{len(payload)-1}/{len(payload)}')
                self.end_headers()
                body = payload[start:]
                if len(ranges) == 1:
                    body = body[:256 * 1024]
                try:
                    self.wfile.write(body)
                    self.wfile.flush()
                    if len(ranges) == 1:
                        self.connection.shutdown(socket.SHUT_RDWR)
                except (BrokenPipeError, ConnectionResetError, OSError):
                    pass

        server = http.server.ThreadingHTTPServer(('127.0.0.1', 0), Handler)
        server.daemon_threads = True
        serving = threading.Thread(target=server.serve_forever, daemon=True)
        serving.start()
        engine = WindowsEngine(str(ROOT / '_dependencies/yt-dlp.exe'))
        original = module.subprocess.Popen
        workers = []
        with tempfile.TemporaryDirectory(prefix='yd-real-resume-') as work:
            folder = Path(work)
            base = f'http://127.0.0.1:{server.server_port}'
            info = folder / 'info.json'
            info.write_text(json.dumps(dict(
                id='resume-fixture', title='Resume fixture', extractor='generic',
                extractor_key='Generic', webpage_url=base,
                formats=[dict(format_id='video', url=base+'/media.mp4', ext='mp4',
                    vcodec='avc1', acodec='none', protocol='http', filesize=len(payload))],
            )), encoding='utf-8')

            commands = []
            def launch(command, **kwargs):
                if command[0] == engine.ytdlp:
                    commands.append(list(command))
                    command = command[:-1] + ['--load-info-json', str(info)]
                return original(command, **kwargs)

            def start():
                worker = threading.Thread(target=engine._download_with_access, args=(
                    base, work, 'video_only', 'video', None, None, None,
                    ['--ignore-config', '--no-plugin-dirs', '--retries', '0', '--socket-timeout', '3']),
                    daemon=True)
                workers.append(worker)
                worker.start()
                return worker

            def terminal(on_progress=None):
                deadline = time.monotonic() + 40
                while time.monotonic() < deadline:
                    try:
                        event = engine.events.get(timeout=1)
                    except queue.Empty:
                        continue
                    if event[0] == 'download_progress' and on_progress:
                        on_progress(event)
                    if event[0] in ('download_done', 'download_error', 'verification_required'):
                        return event
                self.fail('Local transfer did not finish within 40 seconds')

            try:
                with patch.object(module.subprocess, 'Popen', side_effect=launch):
                    worker = start()
                    self.assertEqual(terminal()[0], 'download_error')
                    worker.join(3)
                    partial = folder / 'Resume fixture.mp4.part'
                    self.assertTrue(partial.is_file())
                    self.assertGreater(partial.stat().st_size, 0)
                    partial_bytes = partial.stat().st_size
                    engine.set_download_rate_bps(100_000)
                    changed = []
                    def change_rate(event):
                        if len(changed) == 0:
                            engine.set_download_rate_bps(200_000)
                            changed.append(200_000)
                        elif len(commands) >= 3 and len(changed) == 1:
                            engine.set_download_rate_bps(0)
                            changed.append(0)
                    worker = start()
                    result = terminal(change_rate)
                    worker.join(3)
                    self.assertEqual(result[0], 'download_done', result)
                    self.assertEqual(changed, [200_000, 0])
                    self.assertGreaterEqual(len(commands), 4)
                    self.assertEqual(ranges[1], partial_bytes, ranges)
                    self.assertTrue(any(start >= partial_bytes for start in ranges[1:]), ranges)
                    final = folder / 'Resume fixture.mp4'
                    self.assertEqual(hashlib.sha256(final.read_bytes()).digest(),
                                     hashlib.sha256(payload).digest())
                    self.assertFalse(partial.exists())
                    self.assertIsNone(engine.download_process)
            finally:
                engine.cancel()
                for worker in workers:
                    worker.join(5)
                server.shutdown()
                server.server_close()
                serving.join(3)


if __name__ == '__main__':
    unittest.main(verbosity=2)
