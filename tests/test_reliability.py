"""Recovery across extraction, transfer and silent local postprocessing."""
import io
import http.server
import json
from pathlib import Path
import sys
import tempfile
import threading
import time
import unittest
from unittest.mock import Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import windows_engine as module
from windows_engine import WindowsEngine
from youtube_access import access_args, alternate_youtube_options, transient_network_error


class Reliability(unittest.TestCase):
    def setUp(self):
        self.engine = WindowsEngine('yt-dlp.exe')
        self.engine.analysis_token = 1

    def events(self):
        events = []
        while not self.engine.events.empty():
            events.append(self.engine.events.get_nowait())
        return events

    @staticmethod
    def process(text='', code=1):
        return Mock(stdout=io.StringIO(text), wait=Mock(return_value=code),
                    poll=Mock(return_value=code), returncode=code)

    def test_analysis_recovers_from_temporary_api_failure(self):
        failed = Mock(returncode=1)
        failed.communicate.return_value = ('', 'ERROR: Unable to download API page: Read timed out.')
        success = Mock(returncode=0)
        success.communicate.return_value = (json.dumps({'id': 'test', 'formats': []}), '')
        with patch.object(module.subprocess, 'Popen', side_effect=[failed, success]) as start:
            self.engine._analyze_worker('https://youtu.be/test', 1)
        self.assertEqual(start.call_count, 2)
        self.assertEqual([e[0] for e in self.events()], ['analysis_ok'])
        self.assertIsNone(self.engine.analysis_process)

    def test_analysis_cancellation_during_retry_never_restarts(self):
        failed = Mock(returncode=1)
        failed.communicate.return_value = ('', 'ERROR: Read timed out.')
        def cancel(_):
            self.engine.analysis_token += 1
        with patch.object(module.subprocess, 'Popen', return_value=failed) as start, \
                patch.object(module.time, 'sleep', side_effect=cancel):
            self.engine._analyze_worker('https://youtu.be/test', 1)
        start.assert_called_once()
        self.assertEqual(self.events(), [])

    def test_access_does_not_load_user_config_or_skip_missing_fragments(self):
        options = access_args()
        self.assertIn('--ignore-config', options)
        self.assertIn('--abort-on-unavailable-fragments', options)
        self.assertNotIn('youtube:player_client=mweb', options)

    def test_analysis_tries_token_client_after_a_default_client_refusal(self):
        failed = Mock(returncode=1)
        failed.communicate.return_value = ('', 'ERROR: Sign in to confirm you are not a bot')
        success = Mock(returncode=0)
        success.communicate.return_value = (json.dumps({'id': 'test', 'formats': []}), '')
        with patch.object(module.subprocess, 'Popen', side_effect=[failed, success]) as start:
            self.engine._analyze_worker('https://youtu.be/test', 1)
        first, retry = [call.args[0] for call in start.call_args_list]
        self.assertNotIn('youtube:player_client=mweb', first)
        self.assertIn('youtube:player_client=mweb', retry)
        self.assertIn('--plugin-dirs', retry)
        kind, _, data = self.events()[0]
        self.assertEqual(kind, 'analysis_ok')
        self.assertEqual(data['_downloader_access_recovery'], 'mweb')
        self.engine.analysis_data = data
        with self.engine._access_options() as options:
            self.assertIn('youtube:player_client=mweb', options)

    def test_mweb_fallback_keeps_credentials_and_works_in_both_directions(self):
        options = access_args() + ['--cookies', 'verified.cookies', '--user-agent', 'verified-agent']
        alternate = alternate_youtube_options(options)
        self.assertEqual(alternate_youtube_options(alternate), options)
        self.assertIn('verified.cookies', alternate)
        self.assertIn('verified-agent', alternate)

    def test_transport_detection_uses_final_error(self):
        for error in ('ERROR: HTTP Error 503: Service Unavailable',
                      'ERROR: Connection reset by peer',
                      'ERROR: 500 bytes read, 2000 more expected',
                      'ERROR: 404 bytes read, 2000 more expected',
                      'ERROR: Content too short'):
            self.assertTrue(transient_network_error(error), error)
        for error in ('WARNING: Read timed out\nERROR: Video unavailable',
                      'ERROR: HTTP Error 403: Forbidden', 'ERROR: HTTP Error 429',
                      'ERROR: No space left on device',
                      'ERROR: Requested format is not available'):
            self.assertFalse(transient_network_error(error), error)

    def test_transfer_retries_same_format_and_preserves_partial(self):
        with tempfile.TemporaryDirectory() as folder:
            partial = Path(folder) / 'media.mp4.part'
            partial.write_bytes(b'original bytes')
            failed = self.process('ERROR: connection reset by peer\n')
            success = self.process('', 0)
            with patch.object(module.subprocess, 'Popen', side_effect=[failed, success]) as start, \
                    patch.object(module, 'DOWNLOAD_RETRY_DELAYS', (0, 0, 0)):
                self.engine._download_with_access('https://youtu.be/test', folder,
                    'video_only', '137', None, 'mkv', None, [])
            original, retry = [call.args[0] for call in start.call_args_list]
            for flag in ('-f', '-o'):
                self.assertEqual(original[original.index(flag)+1], retry[retry.index(flag)+1])
            self.assertIn('--continue', retry)
            self.assertEqual(partial.read_bytes(), b'original bytes')
        terminal = [e for e in self.events() if e[0] in ('download_done', 'download_error')]
        self.assertEqual([e[0] for e in terminal], ['download_done'])

    def test_transport_retry_budget_is_bounded(self):
        with tempfile.TemporaryDirectory() as folder, \
                patch.object(module.subprocess, 'Popen', side_effect=lambda *a, **k:
                             self.process('ERROR: HTTP Error 503: Service Unavailable\n')) as start, \
                patch.object(module, 'DOWNLOAD_RETRY_DELAYS', (0, 0, 0)):
            self.engine._download_with_access('https://youtu.be/test', folder,
                'video_only', '137', None, None, None, [])
        self.assertEqual(start.call_count, 4)
        self.assertEqual([e[0] for e in self.events() if e[0] in
                          ('download_error', 'download_done', 'verification_required')], ['download_error'])

    def test_failed_provider_is_bypassed_for_the_transfer(self):
        failed = self.process('ERROR: _get_pot_via_script failed\n')
        success = self.process('', 0)
        options = ['--plugin-dirs', 'plugins', '--extractor-args', 'youtube:player_client=mweb',
                   '--extractor-args', 'youtubepot-bgutilscript:server_home=provider',
                   '--cookies', 'verified.cookies', '--js-runtimes', 'node:portable']
        with tempfile.TemporaryDirectory() as folder, \
                patch.object(module.subprocess, 'Popen', side_effect=[failed, success]) as start:
            self.engine._download_with_access('https://youtu.be/test', folder,
                'video_only', '137', None, None, None, options)
        retry = start.call_args.args[0]
        self.assertIn('--no-plugin-dirs', retry)
        self.assertNotIn('youtube:player_client=mweb', retry)
        self.assertIn('verified.cookies', retry)
        self.assertIn('node:portable', retry)
        self.assertEqual([e[0] for e in self.events() if e[0].startswith('download_')][-1], 'download_done')

    def test_missing_hls_fragment_never_finishes_as_a_success(self):
        manifest = (b'#EXTM3U\n#EXT-X-TARGETDURATION:2\n#EXT-X-MEDIA-SEQUENCE:0\n'
                    b'#EXTINF:2,\nfirst.ts\n#EXTINF:2,\nmissing.ts\n#EXT-X-ENDLIST\n')
        class Handler(http.server.BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass
            def do_GET(self):
                if self.path == '/missing.ts':
                    self.send_error(404)
                    return
                body = manifest if self.path == '/index.m3u8' else b'media fragment' * 100
                self.send_response(200)
                self.send_header('Content-Length', str(len(body)))
                self.end_headers()
                self.wfile.write(body)
        server = http.server.ThreadingHTTPServer(('127.0.0.1', 0), Handler)
        serving = threading.Thread(target=server.serve_forever, daemon=True)
        serving.start()
        engine = self.engine
        engine.ytdlp = str(Path(__file__).resolve().parents[1] / '_dependencies/yt-dlp.exe')
        original = module.subprocess.Popen
        try:
            with tempfile.TemporaryDirectory() as folder:
                base = f'http://127.0.0.1:{server.server_port}'
                info = Path(folder) / 'info.json'
                info.write_text(json.dumps(dict(
                    id='hls-fixture', title='Missing fragment', extractor='generic',
                    extractor_key='Generic', webpage_url=base,
                    formats=[dict(format_id='video', url=base+'/index.m3u8', ext='mp4',
                                  protocol='m3u8_native', vcodec='avc1', acodec='none')],
                )), encoding='utf-8')
                def launch(command, **kwargs):
                    if command[0] == engine.ytdlp:
                        command = command[:-1] + ['--load-info-json', str(info)]
                    return original(command, **kwargs)
                with patch.object(module.subprocess, 'Popen', side_effect=launch):
                    engine._download_with_access(base, folder, 'video_only', 'video',
                        None, None, None, ['--ignore-config', '--no-plugin-dirs',
                            '--fragment-retries', '0', '--retries', '0', '--socket-timeout', '3'])
                self.assertFalse((Path(folder) / 'Missing fragment.mp4').exists())
            terminal = [e for e in self.events() if e[0] in ('download_done', 'download_error')]
            self.assertEqual([e[0] for e in terminal], ['download_error'])
            self.assertIn('fragment', terminal[0][1].lower())
        finally:
            engine.cancel()
            server.shutdown()
            server.server_close()
            serving.join(3)

    def test_silent_local_merge_outlives_the_network_timeout(self):
        self.exercise_silent_postprocessing('[Merger] Merging formats into result.mkv')

    def test_silent_container_fixup_outlives_the_network_timeout(self):
        self.exercise_silent_postprocessing('[FixupM3u8] Fixing MPEG-TS in MP4 container')

    def exercise_silent_postprocessing(self, phase):
        ready, finish = threading.Event(), threading.Event()
        class Output:
            def __iter__(self):
                yield phase + '\n'
                ready.set()
                finish.wait(3)
            def close(self):
                pass
        process = Mock(stdout=Output(), poll=Mock(return_value=None), wait=Mock(return_value=0))
        with tempfile.TemporaryDirectory() as folder, \
                patch.object(module.subprocess, 'Popen', return_value=process), \
                patch.object(module, 'DOWNLOAD_STALL_SECONDS', 0.1), \
                patch.object(module, 'terminate_process_tree') as stop:
            worker = threading.Thread(target=self.engine._download_with_access, args=(
                'https://youtu.be/test', folder, 'video', '137+140', None, 'mkv', None, []))
            worker.start()
            try:
                self.assertTrue(ready.wait(2))
                time.sleep(0.6)
                stop.assert_not_called()
                self.assertTrue(worker.is_alive())
            finally:
                finish.set()
                worker.join(3)
        self.assertFalse(worker.is_alive())
        self.assertEqual([e[0] for e in self.events()], ['download_status', 'download_done'])


if __name__ == '__main__':
    unittest.main()
