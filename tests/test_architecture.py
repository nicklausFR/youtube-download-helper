"""Contract tests for the Qt-free core and the internal application API."""

import queue
import subprocess
import sys
import unittest
import threading
from contextlib import contextmanager
from unittest.mock import patch
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from core_selection import MediaCatalog, DownloadRequest, audio_options, video_options
from download_rate import rate_to_bps
from orchestrator import DownloadOrchestrator
from strategies import DEFAULT_ACCESS, DownloadRegistry, ExcerptStrategy, FullMediaStrategy, SubtitleStrategy
from windows_engine import WindowsEngine


class Architecture(unittest.TestCase):
    def test_completion_waits_for_access_cleanup_before_next_job(self):
        engine = WindowsEngine('yt-dlp.exe')
        app = DownloadOrchestrator(engine)
        cleaning = threading.Event()
        release = threading.Event()
        @contextmanager
        def access(*args):
            try:
                yield []
            finally:
                cleaning.set()
                release.wait(3)
        class Strategy:
            def run(self, engine, *args):
                engine._emit_event(('download_done', [], '.'))
        class Registry:
            def resolve(self, *args):
                return Strategy()
        engine.download_strategies = Registry()
        with patch.object(engine, '_access_options', access):
            worker = app.download(DownloadRequest('https://youtu.be/test123', '.', 'audio', '140'), validate=False)
            try:
                self.assertTrue(cleaning.wait(2))
                self.assertTrue(engine.events.empty(), 'Completion escaped before cleanup')
                release.set()
                self.assertEqual(app.next_event(timeout=3)[0], 'download_done')
                self.assertFalse(worker.is_alive())
                app.download(DownloadRequest('https://youtu.be/next123', '.', 'audio', '140'), validate=False)
                self.assertEqual(app.next_event(timeout=3)[0], 'download_done')
            finally:
                release.set()
                worker.join(3)

    def test_download_rate_units_share_the_same_byte_limit(self):
        self.assertEqual(rate_to_bps(10, "ko/s"), 10_000)
        self.assertEqual(rate_to_bps(0.01, "Mo/s"), 10_000)
        self.assertEqual(rate_to_bps(0.08, "Mbit/s"), 10_000)
        self.assertEqual(rate_to_bps(0, "ko/s"), 0)
        self.assertEqual(rate_to_bps(1, "ko/s"), 10_000)

    def test_headless_import_does_not_load_qt(self):
        subprocess.run(
            [sys.executable, "-c",
             "import orchestrator, core_selection, strategies, windows_engine, sys; "
             "assert 'PySide6' not in sys.modules"],
            check=True, cwd=Path(__file__).resolve().parents[1])

    def test_catalog_and_format_policy_are_usable_without_widgets(self):
        audio = {"format_id": "140", "vcodec": "none", "acodec": "mp4a", "language": "fr",
                 "language_preference": 10, "abr": 128, "ext": "m4a"}
        video = {"format_id": "137", "vcodec": "avc1", "acodec": "none",
                 "height": 1080, "fps": 30, "ext": "mp4"}
        catalog = MediaCatalog.from_analysis({"formats": [audio, video]})
        self.assertEqual(catalog.audio_formats, [audio])
        self.assertEqual(catalog.videos_by_height[1080], [video])
        self.assertEqual(audio_options(catalog.audio_formats, "video", "MKV", True, "fr"), [audio])
        plan = video_options(catalog.videos_by_height, "video", "MKV", True,
                             "", None, "H.264")
        self.assertEqual(plan.selected_label, "1080p · 30 i/s")
        self.assertEqual(plan.formats, [video])

    def test_strategy_registry_can_replace_a_route(self):
        registry = DownloadRegistry((SubtitleStrategy(), ExcerptStrategy(), FullMediaStrategy()))
        self.assertEqual(registry.resolve("video", None).name, "full_media")
        self.assertEqual(registry.resolve("video", (1, 2)).name, "excerpt")
        self.assertEqual(registry.resolve("subtitles", None).name, "subtitles")

        class Replacement:
            name = "replacement"
            def accepts(self, mode, clip):
                return mode == "video" and clip is None
            def run(self, *args):
                return "replacement"

        registry.register(Replacement(), first=True)
        self.assertEqual(registry.resolve("video", None).name, "replacement")
        self.assertEqual(DEFAULT_ACCESS.resolve(False).name, "automatic")
        self.assertEqual(DEFAULT_ACCESS.resolve(True).name, "brave")

    def test_orchestrator_runs_existing_worker_and_follows_events(self):
        class Engine:
            ytdlp = "yt-dlp.exe"
            analysis_token = 0
            cancel_requested = False
            events = queue.Queue()
            def _analyze_worker(self, url, token, session, fallback):
                self.events.put(("analysis_ok", token, {"title": "Fixture", "formats": []}))
            def _download_worker(self, url, folder, mode, fmt, language, container, clip, session):
                self.events.put(("download_done", [folder + "/fixture.mp4"], folder))
            def cancel(self):
                self.cancel_requested = True

        engine = Engine()
        app = DownloadOrchestrator(engine)
        app.analyze("https://youtu.be/test123")
        event = next(app.follow(timeout=2))
        self.assertEqual(event.kind, "analysis_ok")
        self.assertEqual(app.catalog.metadata["title"], "Fixture")
        request = DownloadRequest("https://youtu.be/test123", str(Path.cwd()), "video", "137+140")
        app.download(request)
        event = next(app.follow(timeout=2))
        self.assertEqual(event.kind, "download_done")
        self.assertEqual(app.last_request, request)
        app.cancel()
        self.assertTrue(engine.cancel_requested)

    def test_orchestrator_rejects_invalid_extract_before_starting_worker(self):
        app = DownloadOrchestrator(WindowsEngine("yt-dlp.exe", "ffmpeg.exe"))
        with self.assertRaisesRegex(ValueError, "Plage"):
            app.download(DownloadRequest(
                "https://youtu.be/test123", str(Path.cwd()), "video", "137+140",
                clip=(5, 2)))
        self.assertIsNone(app.download_thread)

    def test_headless_windows_engine_owns_process_state(self):
        engine = WindowsEngine("yt-dlp.exe", "ffmpeg.exe")
        self.assertIsNone(engine.analysis_process)
        self.assertIsNone(engine.download_process)
        self.assertEqual(engine._download_progress_labels(), {})
        engine.cancel()
        self.assertTrue(engine.cancel_requested)

    def test_orchestrator_does_not_preempt_qt_result_handling(self):
        class WindowEngine:
            events = queue.Queue()
            download_results = ["previous.mkv"]

        engine = WindowEngine()
        app = DownloadOrchestrator(engine)
        engine.events.put(("subtitle_retry_done", ["new.fr.srt"], "."))
        app.next_event()
        self.assertEqual(engine.download_results, ["previous.mkv"])


if __name__ == "__main__":
    unittest.main()
