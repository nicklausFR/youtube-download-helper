"""Persistent diagnostics must retain useful failure details and redact access data."""
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from app_logging import redact, safe_command, safe_value


class Logging(unittest.TestCase):
    def test_masks_signed_urls_headers_and_provider_commands(self):
        text = ('ERROR HTTP 403 https://r1.googlevideo.com/api/manifest/hls_playlist/signature/SECRET-PATH?token=SECRET-QUERY\n'
                'Cookie: SID=SECRET-COOKIE; other=value\n'
                'Authorization: Bearer SECRET-AUTH\n'
                'po_token=SECRET-TOKEN\n'
                'X-Goog-Visitor-Id: SECRET-VISITOR\n'
                'SID=SECRET-SID\n'
                "--innertube-context '{SECRET-CONTEXT}'\n"
                'proxy https://account:SECRET-PASSWORD@proxy.invalid:1234/path\n')
        result = redact(text)
        self.assertNotIn('SECRET-', result)
        self.assertIn('HTTP 403', result)
        self.assertIn('googlevideo.com', result)

    def test_command_and_nested_credentials_are_never_serialized(self):
        command = ['yt-dlp.exe', '--cookies', 'SECRET-COOKIEFILE', '--proxy=https://SECRET-PROXY',
                   '--extractor-args', 'youtube:po_token=mweb.gvs+SECRET-TOKEN',
                   '--add-header', 'Authorization: Bearer SECRET-AUTH', '-f', '137+140']
        result = json.dumps(safe_command(command))
        self.assertNotIn('SECRET-', result)
        self.assertIn('137+140', result)
        result = json.dumps(safe_value({'headers': {'Cookie': 'SECRET-COOKIE'}, 'po_token': 'SECRET-TOKEN'}))
        self.assertNotIn('SECRET-', result)
        class Credentials:
            def __repr__(self):
                raise AssertionError('Credentials must never be repr()ed')
        self.assertEqual(safe_value(Credentials()), '<Credentials>')

    def test_file_contains_worker_output_errors_and_safe_catalog_after_exit(self):
        # Isolate global exception hooks and handlers from the rest of the tests.
        script = '''
import json, sys, threading
from unittest.mock import Mock, patch
from app_logging import setup_logging, log_event, log_exception, log_path
from windows_engine import WindowsEngine
import windows_engine
setup_logging(sys.argv[1])
engine = WindowsEngine('yt-dlp.exe')
engine.analysis_token = 3
proc = Mock(returncode=1, pid=42)
proc.communicate.return_value = ('', 'ERROR: HTTP 403 https://r1.googlevideo.com/videoplayback?signature=SECRET-SIGNED\\nAuthorization: SECRET-AUTH\\nUseful detail after redaction')
with patch.object(windows_engine.subprocess, 'Popen', return_value=proc):
    engine._analyze_worker('https://youtu.be/aaaaaaaaaaa', 3)
try:
    raise RuntimeError('Cookie: SECRET-ERROR')
except RuntimeError:
    log_exception('test.exception')
def fail():
    raise RuntimeError('worker failure')
t = threading.Thread(target=fail, name='diagnostic-worker'); t.start(); t.join()
log_event('test.nested', payload={'cookie': 'SECRET-NESTED'})
engine._emit_event(('analysis_ok', 3, {'id': 'aaaaaaaaaaa', 'title': 'Title', 'formats': [{'format_id': '137', 'height': 1080, 'url': 'https://media.invalid/SECRET-URL'}]}))
print(log_path())
'''
        with tempfile.TemporaryDirectory() as work:
            result = subprocess.run([sys.executable, '-c', script, work], cwd=ROOT,
                                    capture_output=True, text=True, check=True, timeout=20)
            logs = list((Path(work) / 'logs').glob('*.log'))
            self.assertEqual(len(logs), 1, result.stderr)
            content = logs[0].read_text(encoding='utf-8')
            for marker in ('application.start', 'analysis.start', 'process.start', 'process.result',
                           'HTTP 403', 'engine.event', 'analysis_ok', 'format_count',
                           'Useful detail after redaction',
                           'test.exception', 'Traceback', 'Unhandled worker exception', 'application.exit'):
                self.assertIn(marker, content)
            self.assertNotIn('SECRET-', content)
            self.assertIn('aaaaaaaaaaa', content)
            self.assertIn('"generation": 3', content)
            self.assertIn('"provider_fallback": false', content)


if __name__ == '__main__':
    unittest.main(verbosity=2)
