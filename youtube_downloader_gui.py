from qt_i18n import bind_text, message
import json
import io
import logging
from app_logging import log_event, log_path, setup_logging, catalog_summary
import urllib.request
from contextlib import contextmanager
from http.client import HTTPException
from automatic_session import provider_options
import os
from pathlib import Path
import queue
import re
import shutil
import subprocess
import sys
import tempfile
import threading
import time
from qt_controls import Value, messagebox
from qt_window import DownloaderWindow, ShutdownCountdown, application
from qt_download_list import DEFAULT_FORMATS
from PySide6.QtCore import QSettings, QSignalBlocker
from PySide6.QtWidgets import QDialog
from urllib.parse import urlencode

from dependency_manager import DependencyManager
from download_rate import MIN_RATE_BPS, RATE_UNITS, rate_to_bps
from clip_strategy import NativeClipUnsupported, download_indexed_stream
from verified_session import VerifiedCredentials, VerificationBrowser
from core_subtitles import (
    parse_timecode, format_timecode, SRT_TIMING, format_srt_time,
    trim_srt_text, trim_srt_file, clean_rolling_srt_text,
    clean_rolling_srt_file,
)
from core_formats import (
    human_size, codec_name, audio_bitrate, format_fits_container,
    LANGUAGE_NAMES, MATROSKA_LANGUAGE_CODES, CODEC_HELP, language_base,
    language_label, audio_content_type, original_language_info,
    original_language, available_video_heights, suspicious_low_quality,
    subtitle_choices, estimated_bytes, _codec_prefix,
    _direct_stream_selector, clip_format_selector, container_args,
)
from core_chapters import _arranged_clip_chapters, _ffmetadata_escape
from core_selection import (
    MediaCatalog, format_size, codec_rank, best_video_for_height, best_audio,
    select_formats, estimate_size_label, clip_validation_error, clip_range,
    DownloadRequest, audio_options, video_options,
)
from core_progress import (
    PROGRESS_PREFIX, PROGRESS_TEMPLATE, RESULT_PREFIX, RESULT_TEMPLATE,
    CLIP_INFO_PREFIX, DownloadProgress, progress_labels, remaining_time_label,
)
from partial_recovery import estimate_saved_progress, matches_saved_download, partial_name
from windows_adapter import (
    CREATE_NO_WINDOW, shutdown_windows, terminate_process_tree, app_dir, dependency_dir,
    find_executable, _first_video_frame_time, _video_keyframe_times,
    _media_streams,
)
from youtube_access import (
    is_youtube_url, youtube_video_id, clean_ansi, extraction_issue, diagnostic_log,
    ACCESS_CHOICES, cookie_access_problem, cookie_access_failed,
    access_args, readable_error, needs_verification,
    without_forced_mweb, subtitle_retryable, request_access_options,
)
from windows_engine import WindowsEngine, WindowsEngineMixin
from orchestrator import DownloadOrchestrator
from strategies import DEFAULT_ACCESS, DEFAULT_DOWNLOADS

APP_TITLE = "Téléchargeur YouTube"
ANALYZE_DELAY_MS = 650
DOWNLOAD_STALL_SECONDS = 60
CLIP_STALL_SECONDS = 20
SUBTITLE_STALL_SECONDS = 45
SUBTITLE_MAX_SECONDS = 180
SUBTITLE_RETRY_DELAYS = (4, 8, 12, 20)
SUBTITLE_ATTEMPTS = len(SUBTITLE_RETRY_DELAYS) + 1
# YouTube currently rate-limits auto-translated timedtext URLs when they are
# requested immediately after extraction.  yt-dlp's upstream workaround is to
# let that extraction session age before the subtitle request.
TRANSLATED_SUBTITLE_WAIT_SECONDS = 60

class YoutubeDownloaderApp(WindowsEngineMixin, DownloaderWindow):
    _arranged_clip_chapters = staticmethod(_arranged_clip_chapters)
    _ffmetadata_escape = staticmethod(_ffmetadata_escape)

    @property
    def download_stall_seconds(self):
        return DOWNLOAD_STALL_SECONDS

    @property
    def subtitle_stall_seconds(self):
        return SUBTITLE_STALL_SECONDS

    @property
    def subtitle_max_seconds(self):
        return SUBTITLE_MAX_SECONDS

    @staticmethod
    def _read_media_streams(ffprobe, path):
        return _media_streams(ffprobe, path)

    @staticmethod
    def _download_indexed_stream(*args, **kwargs):
        return download_indexed_stream(*args, **kwargs)

    def _current_url_for_engine(self):
        return self.url_var.get().strip()

    def cancel(self):
        super().cancel(background=True)

    def __init__(self, *, list_view=False):
        super().__init__()
        log_event('ui.window_created')

        self.list_view = list_view
        self.download_defaults = self._read_format_defaults() if list_view else DEFAULT_FORMATS.copy()
        self.completed_downloads = {}
        self.download_list_order = []
        self.completed_selection = None
        self.list_transfer_eta = None
        self.list_transfer_speed = None

        self.download_rate_bps = 0
        self.download_rate_revision = 0
        try:
            saved_rate = self.preferences.value('download_rate_bps')
            if saved_rate is None:
                self.set_download_rate_mbps(
                    self.preferences.value('download_rate_mbps', 0))
            else:
                self.set_download_rate_bps(saved_rate)
        except (TypeError, ValueError):
            self.set_download_rate_bps(0)


        self.ytdlp = find_executable(["yt-dlp.exe", "yt-dlp"])
        self.ffmpeg = find_executable(["ffmpeg.exe", "ffmpeg"])

        self.analysis_job = None
        self.analysis_token = 0
        self.analysis_running = False
        self.analysis_process = None
        self.analysis_lock = threading.Lock()
        self.analysis_cache = {}
        # Open tabs own their analyzed data for their entire lifetime. The
        # short-lived extraction cache must never discard a configured tab.
        self.tab_analysis_data = {}
        self.active_analysis_key = None
        self.partial_retry_attempted = set()
        self.rate_limit_retries = {}
        self.quality_retries = {}
        self.analysis_without_verified_session = set()
        self.keyframe_token = 0
        self.keyframe_times = []
        self.keyframe_cache = {}
        self.keyframe_note = ""
        self.keyframe_index_codec = None
        self.keyframe_loading = False
        self.keyframe_progress_value = None
        self.keyframe_progress_text = ""
        self.keyframe_process = None
        self.brave_unavailable = False
        self.analysis_data = None
        self.formats_by_height = {}
        self.audio_formats = []
        self.selected_audio_language_labels = []
        self.subtitle_tracks = []
        self.selected_subtitle_codes = []
        self.resolution_heights = []
        self.video_display_formats = []
        self.audio_display_formats = []

        self.download_process = None
        self.native_reader = None
        self.subtitle_process = None
        self.download_thread = None
        self.cancel_requested = False
        self.download_pause_requested = False
        self.priority_download_url = None
        self.closing_transfer_tab_url = None
        self.failed_subtitle_request = None
        self.failed_subtitle_error = ""
        self.failed_subtitle_completed = ()
        self.failed_subtitle_missing = ()

        self.events = queue.Queue()
        self.access_strategies = DEFAULT_ACCESS.copy()
        self.download_strategies = DEFAULT_DOWNLOADS.copy()
        self.orchestrator = DownloadOrchestrator(self)
        self.analysis_diagnostics = ""
        self.dependency_manager = DependencyManager(dependency_dir(), lambda message: self.events.put(("dependency_progress", message)))
        self.dependency_states = []
        self.dependency_updates = {}
        self.dependency_update_errors = []
        self.dependency_busy = False
        self.dependency_blocked = False
        self.dependency_dialog = None
        self.dependency_summary_var = Value(value="Vérification au démarrage…")
        self.dependency_detail_var = Value(value="")

        self.url_var = Value()
        self.access_var = Value(value=ACCESS_CHOICES[0])
        self.access_note_var = Value(value="Accès automatique YouTube · préparation des jetons en arrière-plan.")
        self.mode_var = Value(value="video")
        self.container_var = Value(value="Auto")
        self.height_var = Value()
        self.resolution_profiles = []
        self.codec_var = Value(value="Auto")
        self.settings_var = Value(value="auto")
        self.auto_summary_var = Value(value="MKV · H.264 · AAC ≈ 128 kbit/s · langue d’origine")
        self.size_var = Value(value="Taille estimée : —")
        self.thumbnail_cache = {}
        self.thumbnail_pending = set()
        self.tab_preview_titles = {}
        self.verified_credentials = None
        self.verification_thread = None
        self.verification_cancel = threading.Event()
        self.verification_continue = threading.Event()
        self.verification_attempted = set()
        self.verification_resume = None
        self.last_download_request = None
        self.active_download_job = None
        self.download_retry_pending = False
        self.resume_download_on_start = False
        self.download_results = []
        self.download_result_folder = ''
        self._shutdown_dialog = None
        self.pending_urls = []
        self._video_tab_order = []
        self.queued_downloads = {}
        self.editing_queued_url = None
        self.queue_tab_settings = {}
        self.active_download_data = None
        self.active_download_tracks = None
        self.download_verification_pending = False
        self.subtitle_retry_pending = False
        self._queue_finished_request = None
        self.active_queue_url = None
        self.queue_view_url = None
        self._queue_tab_switching = False
        self.queue_analysis_orchestrator = None
        self.queue_analysis_url = None
        self.queue_analysis_key = None
        self.queue_analysis_waiting = []
        self.queue_analysis_errors = {}
        self.queue_analysis_completed = set()
        self.queue_reanalysis_waiting = set()
        self.queue_analysis_reanalyzing = False
        self.queue_reanalysis_failed = set()
        self.format_help_var = Value()
        self.original_language = None
        self.resolution_var = Value()
        self.audio_var = Value()
        self.audio_codec_var = Value()
        self.audio_rate_var = Value()
        self.audio_language_var = Value()
        self.audio_language_summary_var = Value(value="Aucune")
        self.subtitle_var = Value(value="Aucun")
        self.clip_start_var = Value()
        self.clip_end_var = Value()
        self.clip_note_var = Value()
        self.status_var = Value(value="")
        self.folder_var = Value(value=os.path.join(os.path.expanduser("~"), "Downloads"))
        self.progress_text_var = Value(value="")
        self.progress_detail_var = Value(value="")
        self.download_detail_lines = []
        self.download_detail_started = None
        self.download_progress_value = 0.0
        self._download_checkpoint_at = 0.0
        self._checkpoint_lock = threading.Lock()
        self._checkpoint_pending = None
        self._checkpoint_thread = None
        self._checkpoint_timer = None
        self.title_var = Value(value="")

        self._build_ui()
        self.default_tab_settings = self._capture_tab_settings()
        self._restore_download_checkpoint()
        application().installEventFilter(self)
        self._bind_events()
        self._apply_settings_layout()
        self._fit_window_to_content()
        self.after(0, self._check_dependencies)
        self.after(100, self._prefill_clipboard)
        self.after(100, self._poll_events)

    def _rate_limit_changed(self, value):
        unit = self.rate_unit_combo.currentSourceText()
        self.set_download_rate_bps(rate_to_bps(value, unit))
        if value and value * RATE_UNITS[unit][0] < MIN_RATE_BPS:
            self._display_rate_limit(unit)
        self.preferences.setValue('download_rate_bps', self.download_rate_bps)
        if self.download_thread is not None:
            self._append_download_detail(
                'Débit maximal : ' + (f'{value:g} {unit}' if value else 'illimité')
                + '.')

    def _apply_rate_limit(self):
        self.rate_limit_spinbox.interpretText()
        self._rate_limit_changed(self.rate_limit_spinbox.value())

    def _rate_unit_changed(self, unit):
        self._display_rate_limit(unit)
        self.preferences.setValue('download_rate_unit', unit)

    @staticmethod
    def _format_snapshot(item):
        saved = {key: value for key, value in (item or {}).items()
                 if key in ('format_id', 'height', 'width', 'fps', 'vcodec', 'acodec',
                            'ext', 'language', 'language_preference', 'abr', 'tbr', 'vbr',
                            'asr', 'audio_channels',
                            'filesize', 'filesize_approx', 'protocol', 'format_note', 'dynamic_range')}
        if item and (content_type := audio_content_type(item)):
            saved['_downloader_audio_content_type'] = content_type
        return saved

    def _analysis_snapshot(self, data):
        language_info = data.get('_saved_language_info') or original_language_info(data)
        language, _, inferred = language_info
        tracks = data.get('_saved_subtitle_tracks')
        if tracks is None:
            tracks = subtitle_choices(data, None if inferred else language)
        saved = {key: data[key] for key in (
            'title', 'id', 'duration', 'language', 'is_live', 'live_status',
            '_downloader_access_recovery', '_downloader_format_refresh',
        ) if key in data}
        saved.update(self._video_description_snapshot(data))
        saved['formats'] = [self._format_snapshot(item) for item in data.get('formats') or []]
        saved['_saved_language_info'] = language_info
        saved['_saved_subtitle_tracks'] = tracks
        return saved

    @staticmethod
    def _video_description_snapshot(data):
        return {key: data[key] for key in ('channel', 'uploader', 'description')
                if isinstance(data.get(key), str)}

    def _download_job_snapshot(self, job):
        sel = job['selection']
        data = job['data'] or {}
        saved = dict(request=job['request'], title=job['title'], settings=job['settings'],
            tracks=[{key: track[key] for key in ('code', 'kind', 'label', 'language', 'name')
                     if key in track} for track in job['tracks']],
            selection=dict(format=sel['format'], video=self._format_snapshot(sel.get('video')) or None,
                audio=self._format_snapshot(sel.get('audio')) or None,
                audios=[self._format_snapshot(item) for item in sel.get('audios') or []]),
            data=dict(title=job['title'], id=data.get('id'), duration=data.get('duration'),
                language=data.get('language'),
                formats=[self._format_snapshot(item) for item in data.get('formats') or []],
                _downloader_access_recovery=data.get('_downloader_access_recovery'),
                _downloader_format_refresh=data.get('_downloader_format_refresh'),
                **self._video_description_snapshot(data)))
        if job.get('partial_path'):
            saved['partial_path'] = job['partial_path']
        if 'progress' in job:
            saved['progress'] = job['progress']
        return saved

    def _save_download_checkpoint(self, *, flush=False):
        """Persist open tabs and requests without signed media URLs or credentials."""
        self._save_tab_settings()
        access = self.access_var.get()
        tabs = []
        urls = self._video_tab_urls()
        if self.list_view:
            self._sync_download_list_order(urls)
        for url in urls:
            if url != self.closing_transfer_tab_url and is_youtube_url(url):
                data = self.tab_analysis_data.get((url, access), {})
                title = data.get('title') or self.tab_preview_titles.get(youtube_video_id(url) or url, '')
                tab = dict(url=url, title=title, settings=self.queue_tab_settings.get(url, {}))
                if data and not (data.get('is_live') or data.get('live_status') in ('is_live', 'is_upcoming')):
                    tab['analysis'] = data
                tabs.append(tab)
        active = None
        if self.active_download_job and (not self.cancel_requested or self.download_pause_requested):
            restarts_clip = (self.download_pause_requested
                             and self._download_restarts_clip(self.active_download_job))
            active = dict(self.active_download_job,
                          progress=0.0 if restarts_clip else self.download_progress_value)
        # Capture the ordering and UI state here. Published analysis data and
        # download requests are replaced, rather than edited, after capture.
        waiting = self.queued_downloads.copy()
        if active and self.priority_download_url in waiting:
            priority = waiting.pop(self.priority_download_url)
            waiting = {self.priority_download_url: priority, active['request'][0]: active, **waiting}
            active = None
        saved = dict(
            tabs=tabs, selected=self.queue_view_url,
            active=active,
            resume_on_start=bool(active and (self.resume_download_on_start or (
                self.download_thread is not None and not self.download_pause_requested
                and not self.subtitle_retry_pending and not self.download_verification_pending))),
            waiting=list(waiting.values()),
            completed=self.completed_downloads.copy(),
            list_order=list(self.download_list_order),
            completed_selection=self.completed_selection,
        )
        if active:
            self._download_checkpoint_at = time.monotonic()
        if flush:
            # No older save may overwrite the final snapshot when closing.
            self.after_cancel(self._checkpoint_timer)
            self._checkpoint_timer = None
            with self._checkpoint_lock:
                self._checkpoint_pending = None
                worker = self._checkpoint_thread
            if worker is not None:
                worker.join()
            self._write_download_checkpoint(self.preferences, saved)
            return
        with self._checkpoint_lock:
            self._checkpoint_pending = saved
            if self._checkpoint_thread is None and self._checkpoint_timer is None:
                # Combine nearby changes and let Qt paint before starting the write.
                self._checkpoint_timer = self.after(150, self._start_checkpoint_writer)

    def _start_checkpoint_writer(self):
        self._checkpoint_timer = None
        with self._checkpoint_lock:
            self._checkpoint_thread = threading.Thread(
                target=self._write_pending_checkpoints, daemon=True)
            self._checkpoint_thread.start()

    def _write_download_checkpoint(self, settings, captured):
        tabs = []
        for tab in captured['tabs']:
            if 'analysis' in tab:
                tab = dict(tab, analysis=self._analysis_snapshot(tab['analysis']))
            tabs.append(tab)
        saved = dict(captured, tabs=tabs,
                     active=self._download_job_snapshot(captured['active']) if captured['active'] else None,
                     waiting=[self._download_job_snapshot(job) for job in captured['waiting']])
        settings.setValue('download_checkpoint', json.dumps(saved, ensure_ascii=False))
        settings.sync()

    def _write_pending_checkpoints(self):
        # Use a separate QSettings instance owned by this thread.
        settings = QSettings()
        while True:
            with self._checkpoint_lock:
                saved = self._checkpoint_pending
                self._checkpoint_pending = None
                if saved is None:
                    self._checkpoint_thread = None
                    return
            try:
                self._write_download_checkpoint(settings, saved)
            except Exception:
                logging.getLogger(__name__).exception('Could not save the download checkpoint')

    def _read_download_job_snapshot(self, job):
        if not isinstance(job, dict):
            return None
        request = job.get('request')
        if not isinstance(request, list) or len(request) != 7 or not is_youtube_url(request[0]):
            return None
        if (not isinstance(job.get('selection'), dict) or 'format' not in job['selection']
                or not isinstance(job.get('data'), dict) or not isinstance(job.get('tracks'), list)):
            return None
        clip = request[6]
        if clip is not None and (not isinstance(clip, list) or len(clip) != 2):
            return None
        try:
            title = job.get('title')
            return dict(job, request=tuple(request[:6]) + (tuple(clip) if clip else None,),
                        title=title if isinstance(title, str) else '',
                        tracks=tuple(job['tracks']),
                        settings={**self.default_tab_settings, **job['settings']})
        except (TypeError, KeyError):
            return None

    def _restore_download_checkpoint(self):
        try:
            saved = json.loads(self.preferences.value('download_checkpoint', '{}'))
        except (TypeError, ValueError):
            return
        if isinstance(saved, list):
            jobs = [job for item in saved if (job := self._read_download_job_snapshot(item))]
            active = jobs[0] if jobs else None
            waiting = jobs[1:]
            saved = {}
        elif isinstance(saved, dict):
            active = self._read_download_job_snapshot(saved.get('active'))
            entries = saved.get('waiting')
            waiting = [job for item in (entries if isinstance(entries, list) else ())
                       if (job := self._read_download_job_snapshot(item))]
        else:
            return

        self._restore_completed_downloads(saved)
        active_url = active['request'][0] if active else None
        self.queued_downloads = {job['request'][0]: job for job in waiting if job['request'][0] != active_url}
        jobs = {active_url: active} if active else {}
        jobs.update(self.queued_downloads)
        for url, job in jobs.items():
            self.queue_tab_settings[url] = job['settings']
            if job.get('title'):
                self.tab_preview_titles[youtube_video_id(url) or url] = job['title']

        tabs = saved.get('tabs')
        urls = []
        for tab in tabs if isinstance(tabs, list) else ():
            url = tab.get('url') if isinstance(tab, dict) else None
            if (not is_youtube_url(url) or url in urls
                    or (url in self.completed_downloads and url not in jobs)):
                continue
            urls.append(url)
            if isinstance(tab.get('title'), str) and tab['title']:
                self.tab_preview_titles[youtube_video_id(url) or url] = tab['title']
            if isinstance(tab.get('settings'), dict) and tab['settings']:
                self.queue_tab_settings[url] = {**self.default_tab_settings, **tab['settings']}
            data = tab.get('analysis')
            if (isinstance(data, dict) and isinstance(data.get('formats'), list)
                    and isinstance(data.get('_saved_subtitle_tracks'), list)
                    and isinstance(data.get('_saved_language_info'), list)
                    and len(data['_saved_language_info']) == 3):
                self.tab_analysis_data[(url, self.access_var.get())] = data

        urls.extend(url for url in jobs if url not in urls)
        if not urls:
            self._render_download_queue()
            return
        self._video_tab_order = urls
        self.active_queue_url = active_url or urls[0]
        self.pending_urls = [url for url in urls if url != self.active_queue_url]
        selected = saved.get('selected')
        if selected not in urls:
            selected = self.active_queue_url
        if active:
            self.active_download_job = active
            self.last_download_request = active['request']
            self.active_download_data = active['data']
            self.active_download_tracks = active['tracks']
            self.download_retry_pending = True
            self.resume_download_on_start = saved.get('resume_on_start') is True
            progress = active.get('progress')
            self.download_progress_value = (
                progress if isinstance(progress, (int, float)) and 0 < progress <= 100
                else estimate_saved_progress(active)
            )
            self.download_progress.configure(mode='determinate', value=self.download_progress_value)
        # Analyze only tabs without a reusable catalog after the dependency checks.
        self.queue_analysis_waiting = [
            url for url in dict.fromkeys([selected, *urls])
            if (url, self.access_var.get()) not in self.tab_analysis_data
            and not jobs.get(url, {}).get('partial_path')
        ]
        self._show_queue_tab(selected)

    def _restore_completed_downloads(self, saved):
        completed = saved.get('completed')
        if isinstance(completed, dict):
            for url, record in completed.items():
                if (not is_youtube_url(url) or not isinstance(record, dict)
                        or not isinstance(record.get('folder'), str)
                        or not isinstance(record.get('paths'), list)
                        or not isinstance(record.get('data'), dict)):
                    continue
                self.completed_downloads[url] = record
        order = saved.get('list_order')
        if isinstance(order, list):
            self.download_list_order = list(dict.fromkeys(url for url in order if is_youtube_url(url)))
        selected = saved.get('completed_selection')
        if self.list_view and isinstance(selected, str) and selected in self.completed_downloads:
            self.completed_selection = selected

    def _check_dependencies(self):
        if self.dependency_busy or self.download_thread is not None or self.analysis_running: return
        self.dependency_busy = True
        self.dependency_manager.cancel.clear()
        self.dependency_summary_var.set("Vérification en cours…")
        self.dependency_detail_var.set("Vérification des versions et du démarrage des composants…")
        self._render_dependencies()
        self._update_download_button_state()
        def check():
            try: self.events.put(("dependencies_done", self.dependency_manager.check_all(), False))
            except Exception as error: self.events.put(("dependency_error", str(error)))
        threading.Thread(target=check, daemon=True).start()

    def _install_dependencies(self):
        if self.dependency_busy or self.download_thread is not None or self.analysis_running: return
        missing = [state.key for state in self.dependency_states if not state.ok and state.repairable]
        if not missing: return
        self.dependency_busy = True
        self.dependency_manager.cancel.clear()
        self.dependency_summary_var.set("Installation des composants manquants…")
        self._render_dependencies()
        self._update_download_button_state()
        def install():
            try: self.events.put(("dependencies_done", self.dependency_manager.repair(missing), True))
            except Exception as error: self.events.put(("dependency_error", str(error)))
        threading.Thread(target=install, daemon=True).start()

    def _dependency_update_clicked(self):
        if self.dependency_busy or self.download_thread is not None or self.analysis_running:
            return
        if self.dependency_updates:
            self.dependency_busy = True
            self.dependency_manager.cancel.clear()
            keys = tuple(self.dependency_updates)
            self.dependency_summary_var.set('Installation des mises à jour…')
            self._render_dependencies()
            def update():
                try: self.events.put(('dependencies_done', self.dependency_manager.update(keys), True))
                except Exception as error: self.events.put(('dependency_error', str(error)))
            threading.Thread(target=update, daemon=True).start()
            return
        self.dependency_busy = True
        self.dependency_manager.cancel.clear()
        self.dependency_summary_var.set('Recherche des mises à jour…')
        self.dependency_detail_var.set('Interrogation des publications officielles de yt-dlp et FFmpeg…')
        self._render_dependencies()
        def check_updates():
            try:
                updates, errors = self.dependency_manager.available_updates()
                self.events.put(('dependency_updates_done', updates, errors))
            except Exception as error:
                self.events.put(('dependency_updates_done', {}, [str(error)]))
        threading.Thread(target=check_updates, daemon=True).start()

    def _dependencies_finished(self, states, repaired):
        log_event('dependencies.checked', repaired=repaired,
                  states=[dict(key=item.key, ok=item.ok, detail=item.detail) for item in states])
        self.dependency_busy = False
        if repaired:
            self.dependency_updates = {}
            self.dependency_update_errors = []
        self.dependency_states = states
        missing = [state for state in states if not state.ok]
        self.dependency_blocked = bool(missing)
        self.ytdlp = self.dependency_manager.executable("yt-dlp.exe")
        self.ffmpeg = self.dependency_manager.executable("ffmpeg.exe")
        if missing:
            self.dependency_summary_var.set(f"{len(missing)} composant(s) à installer ou réparer")
            self.dependency_detail_var.set("Cliquez sur Installer / réparer les éléments manquants.")
            self._show_dependencies()
        else:
            self.dependency_summary_var.set(f"Tous les composants sont prêts ({len(states)}/{len(states)})")
            self.dependency_detail_var.set("Installation et vérification terminées." if repaired else "Vérification terminée.")
            if repaired:
                self.analysis_cache.clear()
        self._render_dependencies()
        self._update_download_button_state()
        if not missing:
            if self.resume_download_on_start:
                self._resume_failed_download()
            if self.queued_downloads and not self._transfer_busy():
                viewed_url = self.queue_view_url
                self._activate_next_queued_url(keep_prepared=True)
                if viewed_url in self.pending_urls:
                    self._show_queue_tab(viewed_url)
                    self._save_download_checkpoint()
            self._start_next_queue_analysis()
        url = self.url_var.get().strip()
        if (not missing and not self.download_retry_pending and self.analysis_data is None
                and is_youtube_url(url) and url != self.queue_analysis_url
                and url not in self.queue_analysis_waiting):
            if self.analysis_job is not None:
                try: self.after_cancel(self.analysis_job)
                except RuntimeError: pass
                self.analysis_job = None
            self._start_analysis()


    def _show_missing_ytdlp(self):
        messagebox.showerror(
            APP_TITLE,
            "yt-dlp introuvable.\n\nInstallez yt-dlp depuis la section Dépendances "
            "ou ajoute yt-dlp au PATH Windows.",
        )


    def _retry_analysis(self):
        url = self.url_var.get().strip()
        if self.list_view:
            self._reanalyze_list_video(self.completed_selection or url)
            return
        if self._transfer_busy():
            if url in self.queue_analysis_errors:
                self._schedule_queue_analysis(url)
                self._show_queue_tab(url)
            return
        self.brave_unavailable = False
        self.queue_analysis_errors.pop(url, None)
        self._save_tab_settings()
        self.verification_attempted.discard(url)
        self.partial_retry_attempted.discard(url)
        self.rate_limit_retries.pop(url, None)
        self.quality_retries.pop(url, None)
        self.analysis_without_verified_session.discard(url)
        key = (url, self.access_var.get())
        self.analysis_cache.pop(key, None)
        self.tab_analysis_data.pop(key, None)
        self._on_url_changed()

    def _retry_analysis_now(self):
        if self.list_view:
            self._reanalyze_list_video(self.url_var.get().strip())
            return
        if self.analysis_running or self.dependency_busy or self.dependency_blocked:
            return
        self._save_tab_settings()
        if self.analysis_job is not None:
            try: self.after_cancel(self.analysis_job)
            except Exception: pass
            self.analysis_job = None
        key = (self.url_var.get().strip(), self.access_var.get())
        url = self.url_var.get().strip()
        self.quality_retries.pop(url, None)
        self.analysis_without_verified_session.discard(url)
        self.analysis_cache.pop(key, None)
        self.tab_analysis_data.pop(key, None)
        self.analysis_retry_button.hide()
        self.verification_button.hide()
        self._clear_download_result_actions()
        self.status_var.set('Nouvelle tentative…')
        self._set_analysis_indicator(True)
        self._start_analysis()

    def _session_requested(self):
        return self.verified_credentials or False

    def _current_download_url(self):
        if self.download_thread is not None and self.last_download_request:
            return self.last_download_request[0]
        if self.active_queue_url:
            return self.active_queue_url
        return self.last_download_request[0] if self.last_download_request else ""

    def _viewing_transfer(self):
        url = self.last_download_request[0] if self.last_download_request else self._current_download_url()
        return self.url_var.get().strip() == url

    @staticmethod
    def _compact_queue_url(url):
        value = re.sub(r'^https?://(?:www\.)?', '', str(url or ''), flags=re.I)
        return value if len(value) <= 34 else value[:31] + '…'

    def _capture_tab_settings(self):
        names = ('mode', 'settings', 'container', 'height', 'codec',
                 'resolution', 'audio_codec', 'audio_rate', 'audio_language',
                 'clip_start', 'clip_end', 'folder')
        state = {name: getattr(self, name + '_var').get() for name in names}
        state['audio_languages'] = tuple(self.selected_audio_language_labels)
        state['subtitles'] = tuple(self.selected_subtitle_codes)
        state['video_id'] = str((self._selected_video() or {}).get('format_id', ''))
        state['keyframes'] = tuple(self.keyframe_times)
        state['index_codec'] = self.keyframe_index_codec
        state['keyframe_note'] = self.keyframe_note
        if self.list_view:
            _, audios = self._list_selected_formats()
            state['audio_ids'] = [str(fmt['format_id']) for fmt in audios if fmt and fmt.get('format_id')]
        return state

    def _save_tab_settings(self):
        url = self.url_var.get().strip()
        if self.analysis_data is not None and is_youtube_url(url):
            self.queue_tab_settings[url] = self._capture_tab_settings()

    def _restore_tab_settings(self, url):
        state = self.queue_tab_settings.get(url, self.default_tab_settings)
        self.mode_var.set(state['mode'])
        self.settings_var.set(state['settings'])
        self.selected_subtitle_codes = list(state['subtitles'])
        self.selected_audio_language_labels = list(state['audio_languages'])
        self._mode_changed()
        for name in ('container', 'height', 'codec'):
            getattr(self, name + '_var').set(state[name])
        self._refresh_resolution_choices()
        for name in ('audio_codec', 'audio_rate', 'audio_language'):
            getattr(self, name + '_var').set(state[name])
        self.selected_audio_language_labels = list(state['audio_languages'])
        self._sync_audio_options(choose=True)
        self._refresh_resolution_labels(preferred_height=self._selected_height())
        for index, fmt in enumerate(self.video_display_formats):
            if str(fmt.get('format_id')) == state['video_id']:
                self.resolution_combo.current(index)
                break
        self._set_selected_subtitles(state['subtitles'], refresh=False)
        self.keyframe_times = list(state['keyframes'])
        self.keyframe_index_codec = state['index_codec']
        self.keyframe_note = state['keyframe_note']
        if self.keyframe_times:
            duration = (self.analysis_data or {}).get('duration') or 0
            self.clip_slider.setAllowedValues(
                [0, round(duration * 1000)]
                + [round(value * 1000) for value in self.keyframe_times])
        self._update_clip_index_controls()
        if url in self.queue_tab_settings:
            self.clip_start_var.set(state['clip_start'])
            self.clip_end_var.set(state['clip_end'])
            self._apply_clip_time_entries()
            self.folder_var.set(state['folder'])
        if self.list_view and 'list_defaults' in state:
            self._apply_format_defaults(state['list_defaults'])
            if is_youtube_url(url):
                self.queue_tab_settings[url] = self._capture_tab_settings()
        self._update_auto_summary()
        self._update_download_button_state()

    def _transfer_busy(self):
        return (self.download_thread is not None
                or self.download_retry_pending
                or self.download_verification_pending
                or self.subtitle_retry_pending)

    def _render_video_tabs(self, urls):
        tabs = self.queue_tabs
        selected = self.queue_view_url if self.queue_view_url in urls else self.active_queue_url
        if selected not in urls:
            selected = urls[0] if urls else None
        show_identity = not (self.list_view and self.completed_selection)
        if selected is None and show_identity:
            self._set_selected_video_identity('', '', '', {}, None)
        previous_index = tabs.currentIndex()
        previous_tabs = [tabs.tabData(index) for index in range(tabs.count())]
        previous_previews = dict(previous_tabs)
        positions = {url: position for position, url in enumerate(self.queued_downloads, 1)}
        analyzing_urls = {self.queue_analysis_url, self.active_analysis_key_url()}
        if self.analysis_job is not None and not self.analysis_panel.isHidden():
            analyzing_urls.add(self.url_var.get().strip())
        tabs.blockSignals(True)
        rebuilding = [url for url, _ in previous_tabs] != urls
        if rebuilding:
            wanted = set(urls)
            for index in range(len(previous_tabs) - 1, -1, -1):
                if previous_tabs[index][0] not in wanted:
                    tabs.removeTab(index)
            for index, url in enumerate(urls):
                if url not in previous_previews:
                    tabs.insertTab(index, '')
                    tabs.setTabData(index, (url, None))
                    self._add_video_tab_close(index, url)
                elif tabs.tabData(index)[0] != url:
                    source = next(i for i in range(index + 1, tabs.count())
                                  if tabs.tabData(i)[0] == url)
                    tabs.moveTab(source, index)
        for index, url in enumerate(urls):
            key = (url, self.access_var.get())
            cached = self.analysis_cache.get(key)
            tab_data = self.tab_analysis_data.get(key, cached[1] if cached else {})
            job = self.queued_downloads.get(url)
            analyzing = False
            if url == self.active_queue_url and self._transfer_busy():
                job = self.active_download_job
                if not self.list_view or not tab_data:
                    tab_data = self.active_download_data or tab_data
                if self.download_verification_pending:
                    state = "Vérification"
                elif self.subtitle_retry_pending:
                    state = "Sous-titres"
                else:
                    state = "Téléchargement"
            elif job:
                state = f"Programmé n°{positions[url]}"
            elif url in analyzing_urls:
                state = "Analyse en cours"
                analyzing = True
            elif url in self.queue_analysis_errors:
                state = "Analyse échouée"
            elif key in self.queue_analysis_completed or tab_data:
                state = "Analysé"
            elif url == self.active_queue_url:
                state = "À préparer"
            else:
                state = "Analyse prévue"
            video_id = str(tab_data.get('id') or youtube_video_id(url))
            job_title = (job or {}).get('title')
            analysis_title = tab_data.get('title')
            title = analysis_title if self.list_view and analysis_title else job_title or analysis_title
            title = str(title or '').strip()
            preview_key = (video_id, bool(tab_data))
            if video_id and preview_key != previous_previews.get(url):
                self._request_tab_preview(video_id, fetch_title=not title)
            title = title or self.tab_preview_titles.get(video_id or url)
            if preview_key != previous_previews.get(url):
                tabs.setTabData(index, (url, preview_key))
            tab_label = (message('%1 · %2', title, message(state)) if title else
                         message('%1 · %2', message(state), self._compact_queue_url(url)))
            bind_text(tabs, 'setAccessibleTabName', index, tab_label)
            bind_text(tabs, 'setTabToolTip', index, message('%1\n%2\n%3', title or url, message(state), url))
            self._set_video_tab_preview(
                index, title or url, state, self.thumbnail_cache.get(video_id),
                analyzing=analyzing, downloading=state == 'Téléchargement')
            if url == selected and show_identity:
                feedback = self._list_reanalysis_status(url) if self.list_view else ''
                self._set_selected_video_identity(
                    url, title or 'Vidéo en cours d’identification…', ' · '.join(filter(None, (state, feedback))),
                    tab_data, self.thumbnail_cache.get(video_id), placeholder=not title)
                identity = (url, title, state, self.url_var.get().strip())
                if identity != getattr(self, '_logged_video_identity', None):
                    self._logged_video_identity = identity
                    log_event('ui.selection', video_id=video_id, selected_url=url,
                              title=title, state=state, displayed_url=identity[-1])
        if tabs.count():
            tabs.setCurrentIndex(urls.index(selected) if selected in urls else 0)
        tabs.blockSignals(False)
        if rebuilding:
            self.video_tab_strip.refresh()
        elif tabs.currentIndex() != previous_index:
            self.video_tab_strip.reveal_tab(tabs.currentIndex())

    def _video_tab_urls(self):
        open_urls = ([self.active_queue_url] if self.active_queue_url else []) + self.pending_urls
        # A new active transfer can change which URLs are pending without moving a tab.
        membership = set(open_urls)
        surviving = [url for url in self._video_tab_order if url in membership]
        self._video_tab_order = list(dict.fromkeys(surviving + open_urls))
        return self._video_tab_order

    def _sync_download_list_order(self, urls=None):
        if urls is None:
            urls = self._video_tab_urls()
        wanted = dict.fromkeys([*urls, *self.queued_downloads, *self.completed_downloads])
        order = [url for url in self.download_list_order if url in wanted]
        seen = set(order)
        order.extend(url for url in wanted if url not in seen)
        self.download_list_order = order
        self.queued_downloads = {url: self.queued_downloads[url] for url in order if url in self.queued_downloads}

    def _move_download_list_item(self, url, destination):
        order = self.download_list_order
        if url not in order or not 0 <= destination < len(order) or order[destination] == url:
            return
        order.remove(url)
        order.insert(destination, url)
        self._render_download_queue()
        self._save_download_checkpoint()

    def _prioritize_list_download(self, url):
        if url not in self.queued_downloads or not self._can_prioritize_download():
            return
        order = self.download_list_order
        target = min(order.index(self.active_queue_url), order.index(next(iter(self.queued_downloads))))
        self._move_download_list_item(url, target)
        self.priority_download_url = url
        if self.download_retry_pending:
            self._start_priority_download()
        else:
            self._pause_download()

    def _render_download_queue(self, *, selected_download=None):
        if not hasattr(self, "queue_tabs"):
            return
        self._update_video_view()
        urls = self._video_tab_urls()
        if self.list_view:
            self._sync_download_list_order(urls)
        self._render_video_tabs(urls)
        url = self.url_var.get().strip()
        self.retry_analysis_action.setEnabled(
            self._can_reanalyze_list_video(self.completed_selection or url) if self.list_view else
            is_youtube_url(url) and (not self._transfer_busy() or url in self.queue_analysis_errors))
        self.analysis_diagnostic_action.setEnabled(is_youtube_url(url))

        self.resume_download_button.setVisible(self.download_retry_pending)
        self.skip_download_button.setVisible(self.download_retry_pending)
        self.pause_download_button.setVisible(self._download_can_pause())
        self.pause_download_button.setEnabled(not (self.download_pause_requested or self.cancel_requested))
        self.cancel_button.setVisible(self.download_thread is not None)
        if self.list_view:
            self.download_table.refresh(self.download_list_order)
            self._update_transfer_visibility()
        else:
            self._render_scheduled_downloads(selected_download)

    @staticmethod
    def _download_audio_characteristics(job):
        mode = job['request'][2]
        if mode not in ('video', 'audio'):
            return []
        selection = job['selection']
        video = selection.get('video') or {}
        tracks = selection.get('audios') or []
        if not tracks:
            audio = selection.get('audio') or (video if video.get('acodec') not in (None, 'none') else {})
            tracks = [audio]
        provenance = {
            'original': 'langue d’origine', 'dubbed-auto': 'traduction automatique',
            'dubbed': 'doublage', 'descriptive': 'audiodescription',
        }
        descriptions = []
        for track in tracks:
            parts = []
            codec = track.get('acodec')
            if codec not in (None, 'none', ''):
                parts.append(codec_name(codec))
            if rate := audio_bitrate(track):
                parts.append(f'{rate:.0f} kbit/s')
            if sample_rate := track.get('asr'):
                parts.append(f'{sample_rate / 1000:g} kHz')
            if channels := track.get('audio_channels'):
                parts.append(f'{channels} canaux' if channels != 1 else '1 canal')
            language = track.get('language')
            parts.append(language_label(language) if language and language_base(language) != 'und'
                         else 'langue inconnue')
            if kind := provenance.get(audio_content_type(track)):
                parts.append(kind)
            descriptions.append(' · '.join(parts))
        return descriptions

    def _queued_download_summary(self, job, *, detailed=False):
        if job.get('partial_path'):
            return 'Reprise de fichier partiel'
        request = job['request']
        mode = request[2]
        video = job['selection'].get('video') or {}
        audios = self._download_audio_characteristics(job)
        subtitle_codes = request[4].split(',') if request[4] else ()
        if detailed:
            video_parts = []
            if height := video.get('height'):
                resolution = f'{height}p'
                if width := video.get('width'):
                    resolution += f' ({width} × {height})'
                video_parts.append(resolution)
            if fps := video.get('fps'):
                video_parts.append(f'{fps:g} i/s')
            if codec := video.get('vcodec'):
                video_parts.append(codec_name(codec))
            if dynamic_range := video.get('dynamic_range'):
                video_parts.append(dynamic_range)
            rate = video.get('vbr') or (video.get('tbr') if video.get('acodec') == 'none' else None)
            if rate:
                video_parts.append(f'{rate:.0f} kbit/s')
            subtitle_labels = {track['code']: track.get('label') for track in job['tracks']}
            subtitles = [subtitle_labels.get(code) or language_label(code.removesuffix('-orig'))
                         for code in subtitle_codes]
            parts = [
                'Vidéo : ' + (' · '.join(video_parts) or ('Incluse' if video else 'Aucune')),
                'Audio : ' + (' / '.join(audios) or 'Aucun'),
                'Sous-titres : ' + ('SRT · ' + ' / '.join(subtitles) if subtitles else 'Aucun'),
            ]
        else:
            parts = [{'video': 'Vidéo et audio', 'video_only': 'Vidéo seule',
                      'audio': 'Audio seul', 'subtitles': 'SRT uniquement'}[mode]]
            if video:
                parts.append(f'{video.get("height") or "?"}p · {codec_name(video.get("vcodec"))}')
            if audios:
                parts.append('Audio : ' + ' / '.join(audios))
            if subtitle_codes:
                parts.append('SRT : ' + ', '.join(
                    LANGUAGE_NAMES.get(language_base(code), code) for code in subtitle_codes))
        if request[6]:
            start, end = request[6]
            parts.append(f'Extrait : {format_timecode(start)} → '
                         + (format_timecode(end) if end is not None else 'fin'))
        if mode != 'subtitles':
            container = request[5].upper() if request[5] else ''
            parts.append(f'Conteneur : {container or "source"}' if detailed
                         else (container or 'Conteneur source'))
        return ('\n' if detailed else ' · ').join(parts)

    def _open_active_download_tab(self):
        if self.last_download_request:
            self._open_download_tab(self.last_download_request[0])

    def _open_download_tab(self, url):
        if url in self._video_tab_urls():
            self.completed_selection = None
            self._show_queue_tab(url)
            self._save_download_checkpoint()

    def _edit_queued_video(self):
        url = self.url_var.get().strip()
        if url in self.queued_downloads and self.analysis_data is not None:
            self.editing_queued_url = url
            self._update_video_view()
            self._update_download_button_state()
            if self.list_view:
                self.download_table.refresh_stream_selection()

    def _remove_scheduled_download(self, url):
        self.queued_downloads.pop(url, None)
        self._save_download_checkpoint()
        self._render_download_queue()
        self._update_download_button_state()

    def _move_scheduled_download(self, url, delta):
        urls = list(self.queued_downloads)
        if url not in urls:
            return
        index = urls.index(url)
        destination = index + delta
        if destination == -1 and self._can_prioritize_download():
            self.priority_download_url = url
            if self.download_retry_pending:
                self._start_priority_download()
            else:
                self._pause_download()
                self._render_download_queue(selected_download=url)
            return
        if not 0 <= destination < len(urls):
            return
        urls[index], urls[destination] = urls[destination], urls[index]
        self.queued_downloads = {key: self.queued_downloads[key] for key in urls}
        self._save_download_checkpoint()
        self._render_download_queue(selected_download=url)

    def _can_prioritize_download(self):
        return (self.active_download_job is not None and not self.download_pause_requested
                and self.closing_transfer_tab_url is None and self.verification_thread is None
                and (self.download_retry_pending
                     or (self._download_can_pause() and not self.cancel_requested)))

    def _start_priority_download(self):
        url = self.priority_download_url
        self.priority_download_url = None
        priority = self.queued_downloads.pop(url, None)
        if priority is None:
            self._hold_failed_download()
            return
        previous = dict(self.active_download_job, progress=self.download_progress_value)
        self.queued_downloads = {url: priority, previous['request'][0]: previous, **self.queued_downloads}
        self.active_download_job = None
        self.download_retry_pending = False
        self._save_download_checkpoint()
        self._activate_next_queued_url(keep_prepared=True, select_tab=False)

    def _queue_tab_clicked(self, index):
        urls = self._video_tab_urls()
        if 0 <= index < len(urls):
            self._show_queue_tab(urls[index])
            self._save_download_checkpoint()

    def _show_queue_tab(self, url):
        """Display a queue item while leaving the active transfer untouched."""
        log_event('ui.tab_open', url=url, previous_url=self.url_var.get().strip())
        url = str(url or "").strip()
        if not url:
            return
        visible_url = self.url_var.get().strip()
        self._save_tab_settings()
        if visible_url != url:
            self.editing_queued_url = None
            self.keyframe_token += 1
            if self.keyframe_process is not None:
                terminate_process_tree(self.keyframe_process)
                self.keyframe_process = None
            self.keyframe_times = []
            self.keyframe_index_codec = None
            self.keyframe_loading = False
            self.keyframe_note = ''
        if (visible_url != url and self.analysis_data is not None
                and is_youtube_url(visible_url)):
            self.analysis_cache[(visible_url, self.access_var.get())] = (
                time.monotonic(), self.analysis_data)
        if (visible_url != url and self.analysis_job is not None
                and not self.analysis_running):
            self.after_cancel(self.analysis_job)
            self.analysis_job = None
            self._schedule_queue_analysis(visible_url)
        self.queue_view_url = url
        self._queue_tab_switching = True
        try:
            self.url_var.set(url)
        finally:
            self._queue_tab_switching = False
        key = (url, self.access_var.get())
        cached = self.analysis_cache.get(key)
        data = self.tab_analysis_data.get(key)
        if data is None and cached:
            data = cached[1]
        job = self.active_download_job if url == self.active_queue_url else None
        job = job or self.queued_downloads.get(url)
        partial_job = job and job.get('partial_path')
        if data is not None and visible_url == url and self.analysis_data is data:
            self._render_download_queue()
            return
        self._clear_download_result_actions()
        if data is not None:
            self._process_analysis(data)
            self.status_var.set(self._selection_status())
        else:
            self.analysis_data = None
            self.title_var.set(job['title'] if partial_job else "")
            self.size_var.set("Taille estimée : —")
            self.download_button.setEnabled(False)
            if partial_job:
                self._set_analysis_indicator(False)
                self.status_var.set("Reprise du fichier partiel")
            elif url in self.queue_analysis_errors:
                self.analysis_diagnostics = diagnostic_log(self.queue_analysis_errors[url])
                self._set_analysis_indicator(False)
                self.status_var.set("Échec de l’analyse")
            else:
                self._set_analysis_indicator(True)
                self.status_var.set("")
                if self.download_thread is None and url != self.active_analysis_key_url():
                    self._schedule_queue_analysis(url)
        self._render_download_queue()

    def active_analysis_key_url(self):
        return self.active_analysis_key[0] if self.analysis_running and self.active_analysis_key else None

    def _remove_queued_url(self, index):
        urls = self._video_tab_urls()
        if 0 <= index < len(urls):
            self._close_video_tab(urls[index])

    def _close_video_tab(self, url):
        if url == self.active_queue_url:
            if self._transfer_busy():
                if self.download_retry_pending:
                    self.closing_transfer_tab_url = url
                    self._finish_active_queue_item()
                elif self.download_thread and not (self.cancel_requested or self.download_pause_requested):
                    self.closing_transfer_tab_url = url
                    self._cancel_download()
                return
            self.active_queue_url = self.pending_urls.pop(0) if self.pending_urls else None
        elif url in self.pending_urls:
            self.pending_urls.remove(url)
        else:
            return
        self.queued_downloads.pop(url, None)
        self._forget_video_tab(url)
        if self.queue_view_url == url:
            if self.active_queue_url:
                self._show_queue_tab(self.active_queue_url)
                self.queue_tab_settings.pop(url, None)
            else:
                self.queue_view_url = None
                self.url_var.set('')
        self._render_download_queue()
        self._save_download_checkpoint()

    def _forget_video_tab(self, url):
        self.tab_analysis_data = {key: data for key, data in self.tab_analysis_data.items() if key[0] != url}
        self.queue_analysis_waiting = [queued for queued in self.queue_analysis_waiting if queued != url]
        self.queue_analysis_errors.pop(url, None)
        self.queue_reanalysis_waiting.discard(url)
        self.queue_reanalysis_failed.discard(url)
        self.queue_tab_settings.pop(url, None)
        if url == self.queue_analysis_url and self.queue_analysis_orchestrator is not None:
            self.queue_analysis_orchestrator.cancel()

    def _remove_selected_download_from_queue(self):
        if self.queued_downloads.pop(self.queue_view_url, None) is not None:
            self._save_download_checkpoint()
        self._render_download_queue()
        self._update_download_button_state()

    def _new_link(self):
        url, accepted = self._request_new_link()
        if not accepted:
            return
        url = str(url).strip()
        if not is_youtube_url(url):
            messagebox.showerror(APP_TITLE, 'Entrez une URL YouTube valide.')
            return
        self._url_link_submitted(url)
        if url in self.pending_urls:
            self._show_queue_tab(url)
            self._save_download_checkpoint()

    def _resume_dropped_partial(self, filename):
        partial = Path(filename)
        if partial.suffix.lower() != '.part' or not partial.is_file():
            messagebox.showerror(APP_TITLE, 'Le fichier partiel est introuvable.')
            return
        jobs = [self.active_download_job, *self.queued_downloads.values()]
        matches = [job for job in jobs if job and (
            Path(job['partial_path']) == partial if job.get('partial_path')
            else matches_saved_download(partial, job))]
        if len(matches) == 1:
            job = matches[0]
            if job is self.active_download_job and self.download_retry_pending:
                self._resume_failed_download()
            else:
                self._show_queue_tab(job['request'][0])
            return

        title, _, _ = partial_name(partial)
        embedded_id = re.search(r' \[([\w-]{11})\]$', title, re.ASCII)
        suggested_url = f'https://youtu.be/{embedded_id[1]}' if embedded_id else ''
        url, accepted = self._request_partial_link(partial.name, suggested_url)
        if not accepted:
            return
        url = url.strip()
        if not is_youtube_url(url):
            messagebox.showerror(APP_TITLE, 'Entrez le lien YouTube d’origine du fichier partiel.')
            return
        if self.dependency_busy or self.dependency_blocked:
            self._show_dependencies()
            return
        if url in self.queued_downloads or (self._transfer_busy() and url == self.active_queue_url):
            messagebox.showwarning(APP_TITLE, 'Un transfert de ce lien est déjà en cours ou dans la file.')
            return
        folder = str(partial.parent)
        settings = {**self.default_tab_settings, 'folder': folder}
        job = {
            'request': (url, folder, 'video_only', '', None, None, None),
            'selection': {'format': '', 'video': None, 'audio': None},
            'data': {'title': title},
            'tracks': (),
            'title': title,
            'settings': settings,
            'partial_path': str(partial),
        }
        if url != self.active_queue_url and url not in self.pending_urls:
            self.pending_urls.append(url)
        self._queue_or_launch_job(job)

    def _url_link_submitted(self, url):
        url = str(url or "").strip()
        if not is_youtube_url(url):
            self.url_var.set(url)
            return
        if self.list_view:
            if url in self.completed_downloads and url not in self._video_tab_urls():
                self.completed_selection = url
                self._render_download_queue()
                self._save_download_checkpoint()
                return
            self.completed_selection = None
            if url not in self._video_tab_urls() and url not in self.queue_tab_settings:
                self.queue_tab_settings[url] = {**self.default_tab_settings, 'list_defaults': self.download_defaults.copy()}
        current = self.active_queue_url
        if current and url != current:
            self._enqueue_url(url)
            return
        if not self.active_queue_url:
            self.active_queue_url = url
            self.queue_view_url = url
        self.url_var.set(url)
        self._render_download_queue()
        self._save_download_checkpoint()

    def _enqueue_url(self, url):
        url = str(url or "").strip()
        if not is_youtube_url(url) or url == self._current_download_url():
            return False
        if url not in self.pending_urls:
            self.pending_urls.append(url)
            self._schedule_queue_analysis(url)
            self._save_download_checkpoint()
        self._render_download_queue()
        return True

    def _schedule_queue_analysis(self, url):
        """Analyze a queued link without disturbing the active download UI."""
        url = str(url or "").strip()
        key = (url, self.access_var.get())
        cached = self.analysis_cache.get(key)
        if (not is_youtube_url(url)
                or key in self.tab_analysis_data
                or (cached and time.monotonic() - cached[0] < 300)
                or url == self.queue_analysis_url
                or url in self.queue_analysis_waiting):
            return
        self.queue_analysis_errors.pop(url, None)
        self.queue_analysis_waiting.append(url)
        self._start_next_queue_analysis()

    def _can_reanalyze_list_video(self, url):
        return (url in self.download_list_order and is_youtube_url(url)
                and url != self.closing_transfer_tab_url and bool(self.ytdlp)
                and not self.dependency_busy and not self.dependency_blocked
                and url not in self.queue_analysis_waiting
                and url not in (self.queue_analysis_url, self.active_analysis_key_url())
                and not (url == self.url_var.get().strip() and self.analysis_job is not None))

    def _reanalyze_list_video(self, url):
        if not self._can_reanalyze_list_video(url):
            return
        self.queue_reanalysis_failed.discard(url)
        self.queue_analysis_errors.pop(url, None)
        self.queue_reanalysis_waiting.add(url)
        self.queue_analysis_waiting.append(url)
        self._start_next_queue_analysis()
        if url != self.queue_analysis_url:
            self._render_download_queue()

    def _list_reanalysis_status(self, url):
        if self.queue_analysis_reanalyzing and url == self.queue_analysis_url:
            return 'Nouvelle analyse en cours…'
        if url in self.queue_reanalysis_waiting:
            return 'Nouvelle analyse en attente…'
        if url in self.queue_reanalysis_failed:
            return 'Relance de l’analyse échouée'
        return ''

    def _forget_list_reanalysis(self, url):
        if url in self.queue_reanalysis_waiting:
            self.queue_reanalysis_waiting.discard(url)
            self.queue_analysis_waiting = [queued for queued in self.queue_analysis_waiting if queued != url]
        self.queue_reanalysis_failed.discard(url)
        if self.queue_analysis_reanalyzing and url == self.queue_analysis_url:
            self.queue_analysis_orchestrator.cancel()

    def _start_next_queue_analysis(self):
        if (self.queue_analysis_orchestrator is not None or self.analysis_running
                or (self.analysis_job is not None and not self._transfer_busy())
                or self.dependency_busy
                or self.dependency_blocked or not self.ytdlp):
            return
        while self.queue_analysis_waiting:
            url = self.queue_analysis_waiting.pop(0)
            reanalyze = url in self.queue_reanalysis_waiting
            self.queue_reanalysis_waiting.discard(url)
            if url not in self.pending_urls and url != self.active_queue_url and not (reanalyze and url in self.completed_downloads):
                continue
            key = (url, self.access_var.get())
            cached = self.analysis_cache.get(key)
            if not reanalyze and (key in self.tab_analysis_data or (cached and time.monotonic() - cached[0] < 300)):
                continue
            engine = WindowsEngine(self.ytdlp, self.ffmpeg)
            engine.access_strategies = self.access_strategies.copy()
            orchestrator = DownloadOrchestrator(engine)
            self.queue_analysis_orchestrator = orchestrator
            self.queue_analysis_url = url
            self.queue_analysis_key = key
            self.queue_analysis_reanalyzing = reanalyze
            orchestrator.analyze(
                url, session=self._session_requested(),
                fallback=self.access_var.get() == ACCESS_CHOICES[0])
            self._render_download_queue()
            return

    def _poll_queue_analysis_events(self):
        orchestrator = self.queue_analysis_orchestrator
        if orchestrator is None:
            return
        try:
            while True:
                event = orchestrator.next_event()
                kind = event[0]
                if kind not in ('analysis_ok', 'analysis_error', 'verification_required', 'analysis_cancelled'):
                    continue
                url, key = self.queue_analysis_url, self.queue_analysis_key
                reanalyze = self.queue_analysis_reanalyzing
                self.queue_analysis_orchestrator = None
                self.queue_analysis_url = None
                self.queue_analysis_key = None
                self.queue_analysis_reanalyzing = False
                if reanalyze and (url not in self.download_list_order or url == self.closing_transfer_tab_url):
                    self._start_next_queue_analysis()
                    return
                record = self.completed_downloads.get(url) if reanalyze else None
                bound_job = reanalyze and (url in self.queued_downloads or (
                    url == self.active_queue_url and self.active_download_job))
                if kind == "analysis_ok":
                    data = event[2]
                    if record:
                        self.completed_downloads[url] = dict(record, data=self._with_video_identity(record['data'], data))
                    elif key:
                        self.tab_analysis_data[key] = data
                        self.queue_analysis_completed.add(key)
                        self.analysis_cache[key] = (time.monotonic(), data)
                        if len(self.analysis_cache) > 20:
                            self.analysis_cache.pop(next(iter(self.analysis_cache)))
                    self.queue_analysis_errors.pop(url, None)
                    if url == self.queue_view_url and not record and not self.completed_selection:
                        self._show_queue_tab(url)
                    self._save_download_checkpoint()
                else:
                    error = event[2] if len(event) > 2 else "Analyse interrompue"
                    if reanalyze:
                        self.queue_reanalysis_failed.add(url)
                    if url and not (reanalyze and (record or bound_job or key in self.tab_analysis_data or key in self.analysis_cache)):
                        self.queue_analysis_errors[url] = str(error)
                        if url == self.queue_view_url and not self.completed_selection:
                            self._show_queue_tab(url)
                self._render_download_queue()
                self._start_next_queue_analysis()
                return
        except queue.Empty:
            pass

    def _continue_tab_analyses(self):
        """Keep one analysis running until every pending tab has an outcome."""
        for url in self.pending_urls:
            key = (url, self.access_var.get())
            cached = self.analysis_cache.get(key)
            scheduled = self.queued_downloads.get(url)
            if scheduled and scheduled.get('partial_path'):
                continue
            if (url == self.active_analysis_key_url() or url == self.queue_analysis_url
                    or url in self.queue_analysis_waiting or url in self.queue_analysis_errors
                    or key in self.queue_analysis_completed or cached):
                continue
            self.queue_analysis_waiting.append(url)
        self._start_next_queue_analysis()

    def _activate_next_queued_url(self, *, keep_prepared=False, select_tab=True):
        if self._transfer_busy() or self.verification_thread is not None:
            return
        if self.list_view:
            self._sync_download_list_order()
        if self.queued_downloads:
            url = next(iter(self.queued_downloads))
            job = self.queued_downloads.pop(url)
            self.queue_tab_settings[url] = job['settings']
            if select_tab and not job.get('partial_path'):
                self._show_queue_tab(url)
            if not keep_prepared:
                self.active_queue_url = None
            progress = job.get('progress', 0.0)
            self._launch_download_request(job, select_tab=select_tab, progress=(
                progress if isinstance(progress, (int, float)) and 0 <= progress <= 100 else 0.0))
            return
        if keep_prepared:
            if self.queue_view_url:
                self._show_queue_tab(self.queue_view_url)
            return
        self.active_queue_url = None
        if not self.pending_urls:
            self.queue_view_url = None
            if self.closing_transfer_tab_url:
                with QSignalBlocker(self.url_var):
                    self.url_var.set('')
                self.analysis_data = None
                self.status_var.set("")
                self._set_analysis_indicator(False)
                self._update_download_button_state()
            self._render_download_queue()
            self._save_download_checkpoint()
            return
        viewed = self.queue_view_url
        index = self.pending_urls.index(viewed) if not select_tab and viewed in self.pending_urls else 0
        url = self.pending_urls.pop(index)
        if url not in self.queue_reanalysis_waiting:
            self.queue_analysis_waiting = [queued for queued in self.queue_analysis_waiting if queued != url]
        self.active_queue_url = url
        self.queue_view_url = url
        self._render_download_queue()
        self.url_var.set(url)
        self._save_download_checkpoint()

    def _finish_active_queue_item(self, *, keep_tab=False, succeeded=False, partial=False,
                                  keep_selection=False):
        self.download_retry_pending = False
        self.resume_download_on_start = False
        self.download_verification_pending = False
        self.subtitle_retry_pending = False
        completed = self.last_download_request
        if self._queue_finished_request is completed:
            return
        self._queue_finished_request = completed
        if (succeeded or partial) and self.active_download_job and not self.closing_transfer_tab_url:
            self._remember_completed_download(self.active_download_job, partial=partial)
        self.active_download_job = None
        self._save_download_checkpoint()
        if self.closing_transfer_tab_url:
            self._forget_video_tab(self.closing_transfer_tab_url)
            self._activate_next_queued_url()
            self.closing_transfer_tab_url = None
            return
        def advance():
            if self.last_download_request is completed:
                preserve_view = (
                    (self.list_view and self.queue_view_url in self.pending_urls)
                    or (keep_selection and self.queue_view_url == completed[0]))
                self._activate_next_queued_url(keep_prepared=keep_tab, select_tab=not preserve_view)
                if succeeded and self._can_shutdown_after_downloads(completed):
                    self._show_shutdown_countdown(completed)
        self.after_idle(advance)

    def _remember_completed_download(self, job, *, partial=False):
        url = job['request'][0]
        data = self._analysis_snapshot(self.active_download_data or job['data'] or {})
        data['title'] = job['title'] or data.get('title') or url
        if self.list_view:
            current = self.tab_analysis_data.get((url, self.access_var.get()), {})
            data = self._with_video_identity(data, current)
        selection = job['selection']
        format_ids = [str(fmt['format_id']) for fmt in (
            selection.get('video'), *(selection.get('audios') or [selection.get('audio')]))
            if fmt and fmt.get('format_id') is not None]
        total_size = 0
        for path in self.download_results:
            try:
                total_size += os.path.getsize(path)
            except OSError:
                continue
        self.completed_downloads[url] = {
            'folder': self.download_result_folder or job['request'][1],
            'paths': list(self.download_results),
            'data': data,
            'formats': format_ids,
            'subtitles': (job['request'][4] or '').split(','),
            'summary': self._queued_download_summary(job, detailed=True),
            'size': human_size(total_size) if total_size else self._list_job_size(job),
            'partial': partial,
            'mode': job['request'][2],
        }
        if self.list_view and self.download_table.selected_url() == url:
            self.completed_selection = url

    def _with_video_identity(self, saved, analyzed):
        identity = {key: analyzed[key] for key in ('id', 'title', 'duration') if analyzed.get(key) is not None}
        return {**saved, **identity, **self._video_description_snapshot(analyzed)}

    def _list_job_size(self, job):
        _, _, mode, _, subtitles, _, clip = job['request']
        return self._list_size(job['selection'], mode, (job['data'] or {}).get('duration'), clip, bool(subtitles))

    @staticmethod
    def _list_size(selection, mode, duration, clip, subtitles):
        size = estimate_size_label(selection, mode, duration, clip, subtitles).removeprefix('Taille estimée : ')
        return size if size.startswith('≈') else '—'

    def _list_prepare_again(self, url):
        if url not in self.completed_downloads:
            return
        self.completed_downloads.pop(url)
        self.completed_selection = None
        self._clear_download_result_actions()
        self._url_link_submitted(url)
        self._open_download_tab(url)
        self._update_download_button_state()

    @staticmethod
    def _normalize_format_defaults(values):
        if not isinstance(values, dict):
            values = {}
        container, video, audio = (values.get(key) for key in ('container', 'video', 'audio'))
        subtitles = values.get('subtitles')
        language_choices = ('original', *LANGUAGE_NAMES)
        return {
            'container': container if container in ('Auto', 'MP4', 'MKV', 'WebM', 'M4A', 'MKA') else 'Auto',
            'video': video if video in ('auto', 'off', '360', '480', '720', '1080', '1440', '2160', '4320') else 'auto',
            'audio': audio if audio in ('off', *language_choices) else 'original',
            'subtitles': list(dict.fromkeys(code for code in subtitles if code in language_choices))
            if isinstance(subtitles, list) else [],
        }

    def _read_format_defaults(self):
        try:
            values = json.loads(self.preferences.value('format_defaults', '{}'))
        except (ValueError, TypeError):
            values = {}
        return self._normalize_format_defaults(values)

    def _save_format_defaults(self, values):
        self.download_defaults = self._normalize_format_defaults(values)
        self.preferences.setValue('format_defaults', json.dumps(self.download_defaults))
        self.preferences.sync()

    @staticmethod
    def _default_media_mode(defaults, has_audio):
        if defaults['video'] == 'off':
            return 'subtitles' if defaults['audio'] == 'off' or (defaults['subtitles'] and not has_audio) else 'audio'
        return 'video_only' if defaults['audio'] == 'off' else 'video'

    @staticmethod
    def _default_automatic_settings(defaults):
        return (defaults['container'] == 'Auto' and not defaults['video'].isdecimal()
                and defaults['audio'] in ('original', 'off'))

    def _list_default_streams(self, data, defaults):
        catalog = MediaCatalog.from_analysis(data)
        mode = self._default_media_mode(defaults, bool(catalog.audio_formats))
        automatic = self._default_automatic_settings(defaults)
        original, _, inferred = data.get('_saved_language_info') or original_language_info(data)
        container = defaults['container']
        if container not in self._mode_containers(mode):
            container = 'Auto'
        video = None
        if mode in ('video', 'video_only'):
            height = f"{defaults['video']}p" if defaults['video'].isdecimal() else ''
            options = video_options(catalog.videos_by_height, mode, container, automatic, height, None, 'H.264')
            video = max(options.formats, key=lambda fmt: (fmt.get('acodec') == 'none', codec_rank(fmt)), default=None)
        audios = []
        if mode in ('video', 'audio'):
            options = audio_options(catalog.audio_formats, mode, container, automatic, original)
            if options:
                preferred = self._preferred_audio_format(
                    fmt for fmt in options if language_base(fmt.get('language')) == defaults['audio'])
                choice = preferred or options[0]
                codec = codec_name(choice.get('acodec'))
                language = self._audio_label_for_language(choice, original)
                rate = self._audio_rate_label(choice)
                audios = [self._preferred_audio_format(fmt for fmt in options if (
                    codec_name(fmt.get('acodec')) == codec and self._audio_rate_label(fmt) == rate
                    and self._audio_label_for_language(fmt, original) == language))]
        subtitles = []
        if defaults['subtitles'] and mode != 'audio':
            tracks = data.get('_saved_subtitle_tracks')
            if tracks is None:
                tracks = subtitle_choices(data, None if inferred else original)
            subtitles = self._list_default_subtitle_codes(defaults['subtitles'], tracks=tracks)
        return mode, video, audios, subtitles

    def _apply_format_defaults(self, values):
        defaults = self._normalize_format_defaults(values)
        mode, video, audios, subtitles = self._list_default_streams(self.analysis_data, defaults)
        container = defaults['container']
        self.settings_var.set('auto' if self._default_automatic_settings(defaults) else 'manual')
        self.mode_var.set(mode)
        self._mode_changed()
        if container in self.container_combo['values'] and container != 'Auto':
            self.container_var.set(container)
        if video:
            height, fps = video['height'], video.get('fps')
            self.height_var.set(f'{height}p · {fps:g} i/s' if fps else f'{height}p · cadence inconnue')
            self.codec_var.set(codec_name(video.get('vcodec')))
        self._refresh_resolution_choices()
        if video:
            index = next((i for i, fmt in enumerate(self.video_display_formats) if fmt == video), None)
            if index is not None:
                self.resolution_combo.current(index)
        if audios:
            audio = audios[0]
            label = self._audio_language_label(audio)
            self.audio_codec_var.set(codec_name(audio.get('acodec')))
            self.audio_rate_var.set(self._audio_rate_label(audio))
            self.selected_audio_language_labels = [label]
            self.audio_language_var.set(label)
            self._sync_audio_options(choose=True)
        if subtitles or mode in ('video', 'video_only'):
            self._set_selected_subtitles(subtitles)

    def _list_default_subtitle_codes(self, languages, *, tracks=None):
        tracks = self.subtitle_tracks if tracks is None else tracks
        codes = []
        for language in languages:
            matches = [track for track in tracks if (
                track.get('original') if language == 'original' else language_base(track['code']) == language)]
            if matches:
                best = min(matches, key=lambda track: (not track.get('original'), bool(track.get('kind')),
                                                       track.get('kind') == 'traduction auto'))
                codes.append(best['code'])
        return codes

    def _list_selected_formats(self):
        selection = self._selection()
        if selection is not None:
            return selection.get('video'), selection.get('audios') or ()
        mode = self.mode_var.get()
        video = self._selected_video() if mode in ('video', 'video_only') else None
        return video, self._selected_audios() if mode in ('video', 'audio') else ()

    def _list_can_configure(self, url):
        job = self.queued_downloads.get(url)
        return bool(url and url == self.queue_view_url and self.analysis_data and not self.completed_selection
                    and not (url == self.active_queue_url and self.active_download_job)
                    and not (job and job.get('partial_path')))

    def _list_start_editing(self, url):
        if not self._list_can_configure(url):
            return False
        if url in self.queued_downloads and self.editing_queued_url != url:
            self._edit_queued_video()
        return True

    def _list_media_mode(self, kind, include):
        mode = self.mode_var.get()
        can_split_video = self.mode_buttons['video_only'].isEnabled()
        if kind == 'video':
            if include:
                candidate = 'video' if mode == 'audio' or not can_split_video else 'video_only'
            elif mode == 'video' and self.audio_formats:
                candidate = 'audio' if not self._selected_subtitle() else None
            else:
                candidate = 'subtitles'
        else:
            candidate = ('video' if mode == 'video_only' else 'audio') if include else (
                'video_only' if mode == 'video' else 'subtitles')
            if include and candidate == 'video' and not self.audio_formats and not any(
                    fmt.get('acodec') not in (None, 'none')
                    for formats in self.formats_by_height.values() for fmt in formats):
                return None
        return candidate if {
            'video': bool(self.formats_by_height),
            'video_only': can_split_video,
            'audio': bool(self.audio_formats),
            'subtitles': True,
        }.get(candidate) else None

    def _list_can_toggle_group(self, kind, include):
        if kind == 'subtitle':
            return bool(self.subtitle_tracks) and self.mode_var.get() != 'audio'
        return self._list_media_mode(kind, include) is not None

    def _list_toggle_group(self, url, kind, include):
        if not self._list_can_toggle_group(kind, include) or not self._list_start_editing(url):
            return
        if kind == 'subtitle':
            codes = []
            if include:
                codes = self._list_default_subtitle_codes(self.download_defaults['subtitles']) or self._selected_subtitle_codes()
                if not codes:
                    codes = [self.subtitle_tracks[0]['code']]
            self._set_selected_subtitles(codes)
        else:
            mode = self._list_media_mode(kind, include)
            subtitles = self._selected_subtitle_codes() if self._selected_subtitle() else ()
            self._select_mode(mode)
            if mode != 'audio':
                self._set_selected_subtitles(subtitles)
        self._save_download_checkpoint()

    def _list_set_container(self, index):
        url = self.queue_view_url
        if not self._list_start_editing(url):
            return
        self.settings_var.set('manual')
        self.container_var.set(self.container_combo.sourceText(index))
        self._refresh_resolution_choices()
        self._save_download_checkpoint()

    def _list_editable_streams(self):
        if not self.analysis_data:
            return set()
        mode = self.mode_var.get()
        container = self.container_var.get()
        video = self._selected_video() if mode == 'video' else None
        key = (id(self.analysis_data), mode, container, (video or {}).get('format_id'))
        cached = getattr(self, '_list_stream_options', None)
        if cached and cached[0] == key:
            return cached[1]
        options = set()
        if mode in ('video', 'video_only'):
            for height, formats in self.formats_by_height.items():
                profiles = {(fmt.get('fps') or 0, codec_name(fmt.get('vcodec'))) for fmt in formats}
                for fps, codec in profiles:
                    label = f'{height}p · {fps:g} i/s' if fps else f'{height}p · cadence inconnue'
                    plan = video_options({height: formats}, mode, container, False, label, height, codec)
                    options.update(('video', str(fmt['format_id'])) for fmt in plan.formats if fmt.get('format_id'))
        video_has_audio = video and video.get('acodec') not in (None, 'none')
        if mode in ('video', 'audio') and (not video_has_audio or self._list_video_without_audio(video)):
            grouped = {}
            for fmt in audio_options(self.audio_formats, mode, container, False, self.original_language):
                group = (codec_name(fmt.get('acodec')), self._audio_language_label(fmt), self._audio_rate_label(fmt))
                grouped.setdefault(group, []).append(fmt)
            options.update(('audio', str(fmt['format_id'])) for group in grouped.values()
                           if (fmt := self._preferred_audio_format(group)) and fmt.get('format_id'))
        if mode in ('video', 'video_only', 'subtitles'):
            options.update(('subtitle', track['code']) for track in self.subtitle_tracks)
        self._list_stream_options = key, options
        return options

    def _list_choose_stream(self, url, key, checked):
        if key not in self._list_editable_streams() or not self._list_start_editing(url):
            return
        kind, identifier = key
        if kind == 'subtitle':
            codes = set(self._selected_subtitle_codes())
            if checked:
                codes.add(identifier)
            else:
                codes.discard(identifier)
            self._set_selected_subtitles(codes)
        elif kind == 'video':
            if not checked:
                self._list_toggle_group(url, kind, False)
                return
            fmt = next(fmt for formats in self.formats_by_height.values() for fmt in formats
                       if str(fmt.get('format_id')) == identifier)
            self._list_choose_video(fmt)
            self._update_download_button_state()
        elif kind == 'audio':
            _, audios = self._list_selected_formats()
            if not checked and len(audios) == 1 and str(audios[0].get('format_id')) == identifier:
                self._list_toggle_group(url, kind, False)
                return
            self._list_choose_audio(identifier, checked)
        self._save_download_checkpoint()

    def _list_choose_video(self, fmt):
        self.settings_var.set('manual')
        height, fps = fmt['height'], fmt.get('fps')
        self.height_var.set(f'{height}p · {fps:g} i/s' if fps else f'{height}p · cadence inconnue')
        self.codec_var.set(codec_name(fmt.get('vcodec')))
        self._refresh_resolution_choices()
        for index, option in enumerate(self.video_display_formats):
            if option['format_id'] == fmt['format_id']:
                self.resolution_combo.current(index)
                break

    def _list_video_without_audio(self, video):
        height = int(video['height'])
        candidates = [fmt for fmt in self.formats_by_height.get(height, ())
                      if fmt.get('acodec') in (None, 'none')
                      and codec_name(fmt.get('vcodec')) == codec_name(video.get('vcodec'))
                      and (fmt.get('fps') or 0) == (video.get('fps') or 0)
                      and (fmt.get('dynamic_range') or '') == (video.get('dynamic_range') or '')
                      and format_fits_container(fmt, self.container_var.get())]
        return max(candidates, key=lambda fmt: (fmt.get('protocol') == 'https', fmt.get('tbr') or 0),
                   default=None)

    def _list_choose_audio(self, identifier, checked):
        fmt = next(fmt for fmt in self.audio_formats if str(fmt.get('format_id')) == identifier)
        codec = codec_name(fmt.get('acodec'))
        label = self._audio_language_label(fmt)
        labels = list(self._selected_audio_language_labels()) if codec == self.audio_codec_var.get() else []
        if checked and label not in labels:
            labels.append(label)
        elif not checked and label in labels and len(labels) > 1:
            labels.remove(label)
        elif not checked:
            return
        self.settings_var.set('manual')
        video = self._selected_video() if self.mode_var.get() == 'video' else None
        alternative = (self._list_video_without_audio(video) if video
                       and video.get('acodec') not in (None, 'none') else None)
        if alternative:
            self._list_choose_video(alternative)
        else:
            self._refresh_resolution_choices()
        self.audio_codec_var.set(codec)
        self.audio_rate_var.set(self._audio_rate_label(fmt))
        self.selected_audio_language_labels = labels
        self.audio_language_var.set(labels[0])
        self._audio_options_changed()

    def _can_shutdown_after_downloads(self, completed):
        return (self.shutdown_after_downloads_check.isChecked()
                and not self._closing and not self._disposed
                and self.last_download_request is completed
                and self.active_download_job is None and not self.queued_downloads
                and not self._transfer_busy())

    def _show_shutdown_countdown(self, completed):
        if self._shutdown_dialog is not None:
            return
        dialog = ShutdownCountdown(self)
        self._shutdown_dialog = dialog

        def finished(result):
            self._shutdown_dialog = None
            dialog.deleteLater()
            shutdown = (result == QDialog.DialogCode.Accepted
                        and self._can_shutdown_after_downloads(completed))
            self.shutdown_after_downloads_check.setChecked(False)
            if shutdown:
                self._save_download_checkpoint(flush=True)
                try:
                    shutdown_windows()
                except (OSError, subprocess.SubprocessError):
                    messagebox.showerror(APP_TITLE, 'Windows n’a pas pu lancer l’arrêt de l’ordinateur.')

        dialog.finished.connect(finished)
        dialog.open()

    def _on_url_changed(self, *_):
        if self._queue_tab_switching or self._transfer_busy():
            return
        value = self.url_var.get().strip()
        if (self.active_queue_url and value) or youtube_video_id(value):
            self.active_queue_url = value
            self.queue_view_url = value
        self.analysis_diagnostics = ""
        self.verification_resume = None
        if self.keyframe_process is not None:
            terminate_process_tree(self.keyframe_process)
            self.keyframe_process = None
        if self.verification_thread is not None:
            self.verification_cancel.set()
        with self.analysis_lock:
            self.analysis_token += 1
            if self.analysis_process is not None:
                terminate_process_tree(self.analysis_process)
        self.analysis_running = False
        self.analysis_retry_button.hide()
        self.verification_button.hide()
        self._clear_download_result_actions()
        if self.analysis_job is not None:
            try:
                self.after_cancel(self.analysis_job)
            except Exception:
                pass
            self.analysis_job = None

        self.analysis_data = None
        self.keyframe_token += 1
        self.keyframe_times = []
        self.keyframe_note = ""
        self.keyframe_index_codec = None
        self.keyframe_loading = False
        self.keyframe_progress_value = None
        self.keyframe_progress_text = ""
        self.formats_by_height = {}
        self.audio_formats = []
        self.selected_audio_language_labels = []
        self.subtitle_tracks = []
        self.selected_subtitle_codes = []
        self.title_var.set("")
        self.size_var.set("Taille estimée : —")
        self.original_language = None
        for combo, variable in ((self.height_combo, self.height_var), (self.codec_combo, self.codec_var)):
            combo["values"] = []
            variable.set("")
        self.resolution_profiles = []
        self.format_help_var.set("")
        self.resolution_combo["values"] = []
        self.resolution_var.set("")
        self.audio_combo["values"] = []
        self.audio_var.set("")
        self.audio_language_summary_var.set("Aucune")
        self.subtitle_var.set("Aucun")
        bind_text(self.subtitle_combo, 'setToolTip', "Cliquez pour choisir une ou plusieurs langues.")
        self._configure_clip_controls(None)
        self.resolution_heights = []
        self.video_display_formats = []
        self.audio_display_formats = []
        self.download_button.setEnabled(False)

        self._sync_type_availability()
        if not is_youtube_url(value):
            self._set_analysis_indicator(False)
            self.status_var.set("" if not value else "URL YouTube non reconnue")
            self._render_download_queue()
            return

        data = self.tab_analysis_data.get((value, self.access_var.get()))
        if data is not None:
            self._process_analysis(data)
            self.status_var.set(self._selection_status())
            return
        self.status_var.set("")
        self._set_analysis_indicator(True)
        self.analysis_job = self.after(ANALYZE_DELAY_MS, self._start_analysis)
        self._render_download_queue()

    def _start_analysis(self):
        self.analysis_job = None
        if self.analysis_running:
            return
        if self.dependency_busy or self.dependency_blocked:
            self.status_var.set("Vérifiez les composants dans la section Dépendances.")
            if self.dependency_blocked:
                self._set_analysis_indicator(False)
                self._render_video_tabs(self._video_tab_urls())
            return
        if not self.ytdlp:
            self._set_analysis_indicator(False)
            self._render_video_tabs(self._video_tab_urls())
            return

        url = self.url_var.get().strip()
        if not is_youtube_url(url):
            return
        if not self.active_queue_url:
            self.active_queue_url = self.queue_view_url = url

        if url == self.queue_analysis_url:
            self.analysis_job = self.after(250, self._start_analysis)
            return

        self.analysis_job = None
        key = (url, self.access_var.get())
        cached = self.analysis_cache.get(key)
        data = self.tab_analysis_data.get(key)
        if data is None and cached and time.monotonic() - cached[0] < 300:
            data = cached[1]
        if data is not None:
            self._process_analysis(data)
            self.status_var.set(self._selection_status())
            return
        self.analysis_token += 1
        token = self.analysis_token
        self.active_analysis_key = key
        self.analysis_running = True
        self._set_analysis_indicator(True)
        self.status_var.set("")
        self._render_download_queue()

        analysis_session = (
            False if url in self.analysis_without_verified_session
            else self._session_requested()
        )
        self.orchestrator.analyze(
            url, token=token, session=analysis_session,
            fallback=self.access_var.get() == ACCESS_CHOICES[0])



    def _process_analysis(self, data):
        log_event('ui.catalog_displayed', url=self.url_var.get().strip(), catalog=catalog_summary(data))
        self.analysis_data = data
        url = self.url_var.get().strip()
        if is_youtube_url(url):
            self.tab_analysis_data[(url, self.access_var.get())] = data
            self.queue_analysis_errors.pop(url, None)
        self.queue_analysis_completed.add((self.url_var.get().strip(), self.access_var.get()))
        self.analysis_diagnostics = data.get("_downloader_diagnostics", "")
        self.title_var.set(data.get("title") or "")
        self._configure_clip_controls(data.get("duration"))
        self.original_language, _, inferred = data.get('_saved_language_info') or original_language_info(data)

        catalog = MediaCatalog.from_analysis(data)
        self.formats_by_height = catalog.videos_by_height
        self.audio_formats = catalog.audio_formats
        self.subtitle_tracks = []

        self._sync_type_availability()
        tracks = data.get('_saved_subtitle_tracks')
        self.subtitle_tracks = (tracks if tracks is not None else
                                subtitle_choices(data, None if inferred else self.original_language))

        if not self.audio_formats:
            self.status_var.set("Aucun flux audio exploitable trouvé")
        elif not self.formats_by_height:
            self.status_var.set("Aucun flux vidéo exploitable trouvé")
        else:
            self.status_var.set("Analyse terminée")

        self._refresh_resolution_choices()
        self._restore_tab_settings(self.url_var.get().strip())
        issue = extraction_issue(self.analysis_diagnostics)
        if self._selection() is not None and needs_verification(self.analysis_diagnostics):
            issue = ""
        low_quality = suspicious_low_quality(data)
        if low_quality:
            highest = max(available_video_heights(data))
            self.status_var.set(
                f"Réponse incomplète de YouTube : seulement {highest}p reçu. "
                "Vous pouvez réessayer.")
        self.analysis_retry_button.setVisible(
            issue.startswith('Extraction partielle') or low_quality)
        self._set_analysis_indicator(False)
        self._render_download_queue()
        self.after_idle(self._fit_window_to_content)

    def _schedule_partial_retry(self, data):
        diagnostics = data.get('_downloader_diagnostics', '')
        if not extraction_issue(diagnostics).startswith('Extraction partielle'):
            return False
        url = self.url_var.get().strip()
        if not url or url in self.partial_retry_attempted:
            return False
        self.partial_retry_attempted.add(url)
        self.analysis_running = False
        self.analysis_retry_button.hide()
        self._set_analysis_indicator(True)
        self.status_var.set('Extraction incomplète · nouvelle tentative automatique…')
        self.analysis_job = self.after(1200, self._start_analysis)
        return True

    def _schedule_quality_retry(self, data):
        if not suspicious_low_quality(data):
            return False
        url = self.url_var.get().strip()
        if not url:
            return False
        attempt = self.quality_retries.get(url, 0)
        if attempt >= 2:
            return False
        self.quality_retries[url] = attempt + 1
        # A verified browser session can unlock the page while returning only the
        # 360p combined stream. Retry extraction through the automatic PO-token
        # route, which can expose the separate HD streams for the same video.
        if self.verified_credentials is not None:
            self.analysis_without_verified_session.add(url)
        delay = (1500, 5000)[attempt]
        self.analysis_running = False
        self.analysis_retry_button.show()
        self._set_analysis_indicator(True)
        self.status_var.set(
            f"Liste de résolutions probablement incomplète · tentative {attempt + 1}/2")
        self.analysis_job = self.after(delay, self._start_analysis)
        return True

    def _schedule_rate_limit_retry(self, error):
        low = (error or '').lower()
        if '429' not in low and 'too many requests' not in low:
            return False
        url = self.url_var.get().strip()
        attempt = self.rate_limit_retries.get(url, 0)
        self.analysis_running = False
        self.analysis_retry_button.show()
        if attempt >= 2:
            return False
        delay = (5000, 15000)[attempt]
        self.rate_limit_retries[url] = attempt + 1
        seconds = delay // 1000
        self._set_analysis_indicator(True)
        self.status_var.set(f'HTTP 429 · nouvel essai automatique dans {seconds} s')
        self.analysis_job = self.after(delay, self._start_analysis)
        return True

    def _selection_status(self):
        data = self.analysis_data or {}
        issue = extraction_issue(self.analysis_diagnostics)
        selection = self._selection()
        if data.get("live_status") == "is_upcoming":
            return "Direct à venir : les flux ne sont pas encore disponibles."
        if suspicious_low_quality(data):
            highest = max(available_video_heights(data))
            return (
                f"Réponse incomplète de YouTube : seulement {highest}p reçu. "
                "Utilisez Réessayer pour rechercher les flux HD.")
        if selection is not None and needs_verification(self.analysis_diagnostics):
            issue = ""
        if issue:
            return issue
        if selection is not None:
            if data.get("is_live") or data.get("live_status") == "is_live":
                return "En direct · audio intégré au flux vidéo" if not self.audio_formats else "En direct · flux disponibles"
            return "Analyse terminée"
        mode = self.mode_var.get()
        if mode == "subtitles":
            return "Aucun sous-titre disponible pour cette vidéo."
        formats = data.get("formats") or []
        separate = [f for f in formats if f.get("vcodec") == "none" and f.get("acodec") not in (None, "none")]
        muxed = [f for f in formats if f.get("vcodec") not in (None, "none") and f.get("acodec") not in (None, "none")]
        if mode in ("video", "audio") and not separate:
            if muxed:
                return "L’audio est intégré aux flux vidéo ; aucune piste audio séparée n’a été reçue. Cliquez sur l’état de l’analyse pour le diagnostic."
            return "Aucun flux audio reçu de YouTube. Un blocage antirobot n’est pas confirmé. Cliquez sur l’état de l’analyse pour le diagnostic."
        if mode in ("video", "audio") and not self.audio_display_formats:
            available = ", ".join(sorted({codec_name(f.get("acodec")) for f in separate}))
            return f"Audio disponible en {available}, incompatible avec le choix actuel. Modifiez les réglages avancés."
        if mode in ("video", "video_only") and not self.formats_by_height:
            return "Aucun flux vidéo reçu de YouTube. Cliquez sur l’état de l’analyse pour le diagnostic."
        return "Aucun flux vidéo compatible avec la résolution, le codec ou le conteneur choisi. Modifiez les réglages avancés."

    def _diagnostic_text(self):
        data = self.analysis_data or {}
        formats = data.get("formats") or []
        audio = [f for f in formats if f.get("vcodec") == "none" and f.get("acodec") not in (None, "none")]
        muxed = [f for f in formats if f.get("vcodec") not in (None, "none") and f.get("acodec") not in (None, "none")]
        codecs = ", ".join(sorted({codec_name(f.get("acodec")) for f in audio+muxed})) or "aucun reçu"
        lang, source, inferred = data.get('_saved_language_info') or original_language_info(data)
        access = ("Secours sans BgUtils" if data.get("_downloader_access_recovery") == "without_provider"
                  else "BgUtils PO Token Provider 2.0.0, exécuté à la demande")
        return "\n".join([
            self._selection_status(), "", f"Pistes audio séparées reçues : {len(audio)}",
            f"Flux vidéo avec audio intégré : {len(muxed)}", f"Codecs audio reçus : {codecs}",
            f"Flux vidéo reçus : {sum(f.get('vcodec') not in (None, 'none') for f in formats)}",
            f"Langue : {language_label(lang)}", f"Accès : {access}", f"Source de la langue : {source}" + (" (origine non confirmée)" if inferred else ""),
            "", f"Journal détaillé : {log_path() or 'non activé dans cette session de test'}",
            "", "Messages yt-dlp :", self.analysis_diagnostics or "Aucun avertissement reçu.",
            "", "L’absence d’un format ou d’une langue ne prouve pas un contrôle antirobot.",
            "Une demande explicite de confirmation antirobot permet de l’identifier. HTTP 429 indique une limitation ; HTTP 403 un refus d’accès.",
        ])


    def _settings_changed(self):
        if self.settings_var.get() == "manual":
            self.codec_var.set("Auto")
        self._refresh_resolution_choices()
        self._apply_settings_layout()
        self.after_idle(self._fit_window_to_content)


    def _update_auto_summary(self):
        if self.mode_var.get() == "subtitles":
            self.auto_summary_var.set("")
            return
        if self.mode_var.get() == "video_only" and self.settings_var.get() == "auto":
            self.auto_summary_var.set("MKV · H.264 · sans audio")
            return
        if self.settings_var.get() != "auto":
            self.auto_summary_var.set("")
            return
        audios = self._selected_audios()
        audio = (audios[0] if audios else None) or self._selected_video()
        bitrate = audio_bitrate(audio or {})
        rate = f" ≈ {bitrate:.0f} kbit/s" if bitrate else " · débit non renseigné"
        lang = (
            f"{len(audios)} langues audio"
            if len(audios) > 1 else self._audio_language_label(audio or {}))
        prefix = "MKV · H.264" if self.mode_var.get() in ("video", "video_only") else "M4A"
        self.auto_summary_var.set(f"{prefix} · AAC{rate} · {lang}")

    def _request_tab_preview(self, video_id, fetch_title):
        if not re.fullmatch(r"[A-Za-z0-9_-]{11}", video_id):
            return
        self._request_thumbnail(video_id)
        if not fetch_title or video_id in self.tab_preview_titles:
            return
        self.tab_preview_titles[video_id] = None

        def fetch():
            title = None
            params = urlencode({
                "url": f"https://www.youtube.com/watch?v={video_id}",
                "format": "json",
            })
            try:
                with urllib.request.urlopen(f"https://www.youtube.com/oembed?{params}", timeout=4) as response:
                    payload = response.read(65_537)
                data = json.loads(payload) if len(payload) <= 65_536 else None
                if isinstance(data, dict) and isinstance(data.get("title"), str):
                    title = data["title"].strip() or None
            except (OSError, ValueError, HTTPException):
                pass
            self.events.put(("tab_preview_title", video_id, title))

        threading.Thread(target=fetch, daemon=True).start()

    def _request_thumbnail(self, video_id):
        if video_id in self.thumbnail_cache or video_id in self.thumbnail_pending:
            return
        self.thumbnail_pending.add(video_id)

        def fetch_image():
            content = None
            try:
                url = f"https://i.ytimg.com/vi/{video_id}/mqdefault.jpg"
                with urllib.request.urlopen(url, timeout=8) as response:
                    image = response.read(2_000_001)
                if len(image) <= 2_000_000:
                    content = image
            except (OSError, ValueError, HTTPException):
                pass
            self.events.put(("thumbnail", video_id, content))

        threading.Thread(target=fetch_image, daemon=True).start()

    def _remember_tab_preview(self, cache, video_id, value):
        cache[video_id] = value
        if len(cache) <= 32:
            return
        tabs = self.queue_tabs
        open_ids = {tabs.tabData(index)[1][0] for index in range(tabs.count())}
        for cached_id in tuple(cache):
            if cached_id not in open_ids:
                del cache[cached_id]
                if len(cache) <= 32:
                    break

    def _sync_type_availability(self):
        pending = self.analysis_data is None
        available = {
            "audio": pending or bool(self.audio_formats),
            "video_only": pending or any(
                f.get("vcodec") not in (None, "none") and f.get("acodec") == "none"
                for formats in self.formats_by_height.values() for f in formats
            ),
        }
        for mode, enabled in available.items():
            self.mode_buttons[mode].setEnabled(enabled)
        if not self.list_view and not available.get(self.mode_var.get(), True):
            self.mode_var.set("video")
            self.container_combo["values"] = ("Auto", "MP4", "MKV", "WebM")
            if self.container_var.get() not in self.container_combo["values"]:
                self.container_var.set("Auto")

    @staticmethod
    def _mode_containers(mode):
        return ("Auto", "MP4", "MKV", "WebM") if mode in ("video", "video_only") else ("Auto", "M4A", "MKA", "WebM")

    def _mode_changed(self):
        self._sync_type_availability()
        choices = self._mode_containers(self.mode_var.get())
        self.container_combo["values"] = choices
        if self.container_var.get() not in choices:
            self.container_var.set("Auto")
        self._refresh_resolution_choices()
        self._apply_settings_layout()
        self.after_idle(self._fit_window_to_content)

    def _refresh_resolution_choices(self):
        if not self.analysis_data:
            return

        mode = self.mode_var.get()
        if mode == "subtitles":
            selected = list(self._selected_subtitle_codes())
            if not selected and self.subtitle_tracks:
                preferred = next((track for track in self.subtitle_tracks if track["original"]), None)
                preferred = preferred or next((track for track in self.subtitle_tracks if not track["kind"]), None)
                preferred = preferred or self.subtitle_tracks[0]
                selected = [preferred["code"]]
            self._set_selected_subtitles(selected, refresh=False)
            self._apply_settings_layout()
            self.status_var.set("Analyse terminée" if self.subtitle_tracks else "Aucun sous-titre disponible pour cette vidéo")
            self._update_download_button_state()
            return
        automatic = self.settings_var.get() == "auto"
        if automatic:
            self.container_var.set("MKV" if mode in ("video", "video_only") else "M4A")
            self.codec_var.set("H.264")
            self.selected_audio_language_labels = []
            self.audio_language_var.set("")
            self.audio_rate_var.set("")

        # Keep current selections where possible.
        current_audio = None if automatic else self._selected_audio()
        current_height = self._selected_height()

        ordered_audio = audio_options(
            self.audio_formats, mode, self.container_var.get(), automatic,
            self.original_language)
        self.audio_display_formats = ordered_audio
        audio_values = []
        for f in ordered_audio:
            abr = audio_bitrate(f)
            bitrate = f"{int(round(abr))} kb/s" if abr else "débit inconnu"
            size = self._format_size(f)
            size_label = f"~{human_size(size)}" if size else "taille inconnue"
            language = f.get("language") or "langue inconnue"
            note = f.get("format_note") or ""
            audio_values.append(f"{codec_name(f.get('acodec'))} · {language} · {bitrate} · {size_label} · {note} [{f.get('format_id')}]")

        self.audio_combo["values"] = audio_values
        audio_index = 0
        if current_audio is not None:
            current_id = str(current_audio.get("format_id"))
            for i, f in enumerate(ordered_audio):
                if str(f.get("format_id")) == current_id:
                    audio_index = i
                    break
        if audio_values:
            self.audio_combo.current(audio_index)
        else:
            self.audio_var.set("")

        self._sync_audio_options()

        if mode != "audio":
            self._refresh_resolution_labels(preferred_height=current_height)
            self._set_selected_subtitles(self.selected_subtitle_codes, refresh=False)

        self._apply_settings_layout()
        self.status_var.set(self._selection_status())
        self._update_auto_summary()
        self._update_download_button_state()
        selected_video = self._selected_video()
        selected_codec = codec_name((selected_video or {}).get("vcodec")) if selected_video else None
        if selected_codec != self.keyframe_index_codec and (
                self.keyframe_times or self.keyframe_loading):
            self.keyframe_token += 1
            self.keyframe_times = []
            self.keyframe_index_codec = None
            self.keyframe_loading = False
            self.keyframe_progress_value = None
            self.keyframe_progress_text = ""
            self.clip_slider.setAllowedValues(None)
            self.keyframe_note = ""
        self._update_clip_index_controls()

    def _refresh_resolution_labels(self, preferred_height=None):
        if self.mode_var.get() not in ("video", "video_only"):
            return
        previous = self._selected_video()
        audio = self._selected_audio()
        automatic = self.settings_var.get() == "auto"
        plan = video_options(
            self.formats_by_height, self.mode_var.get(), self.container_var.get(),
            automatic, self.height_var.get(), preferred_height, self.codec_var.get())
        self.resolution_profiles = plan.profiles
        self.height_combo["values"] = plan.labels
        self.height_var.set(plan.selected_label)
        self.codec_combo["values"] = plan.codecs
        self.codec_var.set(plan.selected_codec)
        videos = plan.formats
        codec_help = CODEC_HELP.get(self.codec_var.get(), self.codec_var.get())
        self.format_help_var.set(codec_help)
        bind_text(self.codec_combo, 'setToolTip', codec_help)
        self.video_display_formats = videos
        self.resolution_heights = [int(f["height"]) for f in videos]
        values = []
        for f in videos:
            size = self._format_size(f)
            if f.get("acodec") == "none" and audio and self.mode_var.get() != "video_only":
                audio_size = self._format_size(audio)
                size = size + audio_size if size and audio_size else None
            size_label = f"~{human_size(size)}" if size else "taille inconnue"
            fps = f" · {f['fps']:g} i/s" if f.get("fps") else ""
            included = f" · audio {codec_name(f['acodec'])} inclus" if f.get("acodec") not in (None, "none") else ""
            values.append(f"{f['height']}p · {codec_name(f.get('vcodec'))}{fps} · {size_label}{included} [{f.get('format_id')}]")
        self.resolution_combo["values"] = values
        if not videos:
            self.resolution_var.set("")
            return
        index = next((i for i, f in enumerate(videos) if previous and f.get("format_id") == previous.get("format_id")), None)
        if index is None:
            heights = self.resolution_heights
            height = preferred_height if preferred_height in heights else next((h for h in (720, 1080, 480, 360) if h in heights), heights[-1])
            candidates = [(i, f) for i, f in enumerate(videos) if int(f["height"]) == height]
            index = max(candidates, key=lambda pair: (pair[1].get("acodec") == "none", self._codec_rank(pair[1])))[0]
        self.resolution_combo.current(index)
        self._update_auto_summary()

    def _selected_video(self):
        index = self.resolution_combo.current()
        if 0 <= index < len(self.video_display_formats):
            return self.video_display_formats[index]
        return None

    def _selected_height(self):
        video = self._selected_video()
        return int(video["height"]) if video else None

    def _selected_audio(self):
        selected = self._selected_audios()
        if selected:
            return selected[0]
        if not self.audio_display_formats:
            return None
        idx = self.audio_combo.current()
        if 0 <= idx < len(self.audio_display_formats):
            return self.audio_display_formats[idx]
        return None

    def _audio_language_choices(self):
        formats = self.audio_display_formats
        codec = self.audio_codec_var.get()
        if codec:
            formats = [
                item for item in formats
                if codec_name(item.get("acodec")) == codec]
        return tuple(dict.fromkeys(
            self._audio_language_label(item) for item in formats))

    def _selected_audio_language_labels(self):
        available = set(self._audio_language_choices())
        return tuple(
            label for label in self.selected_audio_language_labels
            if label in available)

    def _set_selected_audio_languages(self, labels, refresh=True):
        requested = set(labels or ())
        self.selected_audio_language_labels = [
            label for label in self._audio_language_choices()
            if label in requested]
        self.audio_language_var.set(
            self.selected_audio_language_labels[0]
            if self.selected_audio_language_labels else "")
        self._sync_audio_options()
        if refresh:
            self._audio_changed()

    def _update_audio_language_summary(self):
        selected = self._selected_audio_language_labels()
        if not selected:
            summary = "Aucune"
        elif len(selected) == 1:
            summary = selected[0]
        else:
            short = [label.split(" — ", 1)[0] for label in selected]
            summary = f"{len(selected)} langues · " + ", ".join(short[:3])
            if len(short) > 3:
                summary += "…"
        self.audio_language_summary_var.set(summary)
        if hasattr(self, "audio_language_picker"):
            bind_text(self.audio_language_picker, 'setToolTip', "\n".join(selected)
                if selected else "Cliquez pour choisir une ou plusieurs langues audio.")

    def _selected_audios(self):
        if not self.audio_display_formats:
            return []
        codec = self.audio_codec_var.get()
        labels = self._selected_audio_language_labels()
        if not labels:
            requested = self.audio_language_var.get()
            if requested in self._audio_language_choices():
                labels = (requested,)
        rate = self.audio_rate_var.get()
        selected = []
        for label in labels:
            candidates = [
                item for item in self.audio_display_formats
                if (not codec or codec_name(item.get("acodec")) == codec)
                and self._audio_language_label(item) == label]
            exact = [
                item for item in candidates
                if self._audio_rate_label(item) == rate]
            pool = exact or candidates
            choice = self._preferred_audio_format(pool)
            if choice is not None:
                selected.append(choice)
        return selected

    @staticmethod
    def _preferred_audio_format(formats):
        return max(formats, key=lambda item: (
            item.get('language_preference') or 0,
            item.get('protocol') == 'https',
            'drc' not in str(item.get('format_id')),
            audio_bitrate(item) or 0), default=None)

    @staticmethod
    def _audio_rate_label(fmt):
        rate = audio_bitrate(fmt)
        return f"{rate:.0f} kbit/s" if rate else "Débit inconnu"

    def _audio_language_label(self, fmt):
        return self._audio_label_for_language(fmt, self.original_language)

    @staticmethod
    def _audio_label_for_language(fmt, original_language):
        language = fmt.get("language")
        label = language_label(language)
        kind = audio_content_type(fmt)
        if kind == "dubbed-auto":
            label += " — traduction automatique"
        elif kind == "dubbed":
            label += " — doublage"
        elif kind == "descriptive":
            label += " — audiodescription"
        elif kind == "original" or (original_language and language_base(language) == language_base(original_language)):
            label += " — langue d’origine"
        return label

    def _sync_audio_options(self, choose=False):
        formats = self.audio_display_formats
        current = self._selected_audio()
        codecs = sorted({codec_name(f.get("acodec")) for f in formats})
        self.audio_codec_combo["values"] = codecs
        codec = self.audio_codec_var.get() if choose else codec_name((current or {}).get("acodec"))
        if codec not in codecs:
            codec = "AAC" if "AAC" in codecs else (codecs[0] if codecs else "")
        self.audio_codec_var.set(codec)
        matching = [f for f in formats if codec_name(f.get("acodec")) == codec]
        languages = list(dict.fromkeys(self._audio_language_label(f) for f in matching))
        self.audio_language_combo["values"] = languages
        selected = [
            label for label in self.selected_audio_language_labels
            if label in languages]
        requested = self.audio_language_var.get()
        if choose and requested in languages and requested not in selected:
            selected = [requested]
        if not selected:
            current_label = self._audio_language_label(current or {})
            language = current_label if current_label in languages else next(
                (lang for lang in languages if "langue d’origine" in lang),
                languages[0] if languages else "")
            selected = [language] if language else []
        self.selected_audio_language_labels = selected
        self.audio_language_var.set(selected[0] if selected else "")
        selected_formats = [
            f for f in matching
            if self._audio_language_label(f) in selected]
        rates = list(dict.fromkeys(
            self._audio_rate_label(f)
            for f in sorted(selected_formats, key=lambda f: audio_bitrate(f) or 0)))
        self.audio_rate_combo["values"] = rates
        rate = self.audio_rate_var.get() if choose else self._audio_rate_label(current or {})
        if rate not in rates:
            nearest = min(
                selected_formats,
                key=lambda f: abs((audio_bitrate(f) or 128)-128), default=None)
            rate = self._audio_rate_label(nearest) if nearest else ""
        self.audio_rate_var.set(rate)
        chosen = self._selected_audios()
        if chosen:
            self.audio_combo.current(formats.index(chosen[0]))
        self._update_audio_language_summary()

    def _audio_options_changed(self):
        self._sync_audio_options(choose=True)
        self._audio_changed()

    def _audio_changed(self):
        if self.audio_display_formats:
            index = self.audio_combo.current()
            current = (
                self.audio_display_formats[index]
                if 0 <= index < len(self.audio_display_formats) else None)
            selected_ids = {
                str(item.get("format_id")) for item in self._selected_audios()}
            if current is not None and str(current.get("format_id")) not in selected_ids:
                label = self._audio_language_label(current)
                self.selected_audio_language_labels = [label]
                self.audio_language_var.set(label)
                self.audio_rate_var.set(self._audio_rate_label(current))
                self._update_audio_language_summary()
        if self.mode_var.get() in ("video", "video_only"):
            self._refresh_resolution_labels(preferred_height=self._selected_height())
        self.status_var.set(self._selection_status())
        self._update_download_button_state()

    @staticmethod
    def _format_size(f):
        return format_size(f)

    @staticmethod
    def _codec_rank(f):
        return codec_rank(f)

    def _best_video_for_height(self, height):
        return best_video_for_height(self.formats_by_height.get(height, []))

    def _best_audio(self):
        return best_audio(self.audio_formats)

    def _selection(self):
        if not self.analysis_data:
            return None
        mode = self.mode_var.get()
        return select_formats(
            mode,
            self._selected_video() if mode in ("video", "video_only") else None,
            self._selected_audios() if mode != "subtitles" else None,
            self._selected_subtitle() if mode == "subtitles" else None,
        )

    def _selected_subtitle_codes(self):
        available = {track["code"] for track in self.subtitle_tracks}
        return tuple(code for code in self.selected_subtitle_codes if code in available)

    def _set_selected_subtitles(self, codes, refresh=True):
        requested = set(codes or ())
        self.selected_subtitle_codes = [
            track["code"] for track in self.subtitle_tracks if track["code"] in requested
        ]
        selected = self._selected_subtitle_codes()
        if not selected:
            summary = "Aucun"
        elif len(selected) == 1:
            code = selected[0]
            name = LANGUAGE_NAMES.get(language_base(code), code)
            summary = name[:1].upper() + name[1:]
        else:
            shown = ", ".join(LANGUAGE_NAMES.get(language_base(code), code) for code in selected[:3])
            summary = f"{len(selected)} langues · {shown}" + ("…" if len(selected) > 3 else "")
        self.subtitle_var.set(summary)
        if hasattr(self, "subtitle_combo"):
            labels = [track.get("tooltip", track["label"]) for track in self.subtitle_tracks if track["code"] in selected]
            bind_text(self.subtitle_combo, 'setToolTip', "\n".join(labels) if labels else "Cliquez pour choisir une ou plusieurs langues.")
        if refresh:
            self.status_var.set(self._selection_status())
            self._update_auto_summary()
            self._update_download_button_state()

    def _selected_subtitle(self):
        if self.mode_var.get() not in ("video", "video_only", "subtitles"):
            return None
        selected = self._selected_subtitle_codes()
        return ",".join(selected) if selected else None

    def _update_download_button_state(self):
        self._update_size()
        url = self.url_var.get().strip()
        busy = self._transfer_busy()
        viewing_other = bool(busy and url != self.active_queue_url)
        ready = (
            not self.dependency_busy and not self.dependency_blocked
            and self.ytdlp is not None
            and self.analysis_data is not None
            and self._selection() is not None
            and not self._clip_validation_error()
            and (viewing_other or (self.download_process is None and not busy))
            and not (self.download_result_folder and self._viewing_transfer())
            and not self.keyframe_loading
            and self.verification_thread is None
        )
        bind_text(self.download_button, 'setText', 'Mettre à jour la file' if url in self.queued_downloads else
            'Ajouter à la file' if viewing_other else 'Télécharger')
        self.download_button.setEnabled(ready)
        self._render_dependencies()
        if self.list_view:
            self.download_table.schedule_stream_refresh()

    def _update_size(self):
        self.size_var.set(estimate_size_label(
            self._selection(), self.mode_var.get(),
            (self.analysis_data or {}).get("duration"), self._clip_range(),
            bool(self._selected_subtitle()),
        ))

    def _clip_validation_error(self):
        return clip_validation_error(
            self.settings_var.get(), self.mode_var.get(),
            self.clip_start_var.get(), self.clip_end_var.get(),
            (self.analysis_data or {}).get("duration"),
        )

    def _clip_range(self):
        return clip_range(
            self.settings_var.get(), self.mode_var.get(),
            self.clip_start_var.get(), self.clip_end_var.get(),
            (self.analysis_data or {}).get("duration"),
        )

    def _clip_changed(self, *_):
        error = self._clip_validation_error()
        self.clip_note_var.set(error or self.keyframe_note)
        self._update_download_button_state()

    def _configure_clip_controls(self, duration):
        available = isinstance(duration, (int, float)) and duration > 0
        self.clip_slider.setAllowedValues(None)
        if not available:
            self.clip_slider.setRange(0, 0)
            self.clip_slider.setSelection(0, 0)
            self.clip_start_entry.setText("00:00:00.000")
            self.clip_end_entry.setText("00:00:00.000")
            self._update_clip_index_controls()
            return
        maximum = max(1, round(float(duration) * 1000))
        self.clip_slider.setRange(0, maximum)
        self.clip_slider.setSelection(0, maximum)
        self._update_clip_index_controls()

    def _update_clip_index_controls(self):
        duration = (self.analysis_data or {}).get("duration")
        available = isinstance(duration, (int, float)) and duration > 0
        video_mode = self.mode_var.get() in ("video", "video_only")
        indexed = bool(self.keyframe_times and self.keyframe_index_codec)
        selection_enabled = available and (not video_mode or indexed)
        self.clip_slider.setEnabled(selection_enabled)
        self.clip_start_entry.setEnabled(selection_enabled)
        self.clip_end_entry.setEnabled(selection_enabled)
        if hasattr(self, "keyframe_index_button"):
            self.keyframe_index_button.setVisible(video_mode)
            self.keyframe_index_button.setEnabled(available and not self.keyframe_loading and not indexed)
            if self.keyframe_loading:
                bind_text(self.keyframe_index_button, 'setText', self.keyframe_progress_text or "Connexion à YouTube")
                self.keyframe_index_button.setProgress(self.keyframe_progress_value)
                self.keyframe_index_button.setIndeterminate(
                    self.keyframe_progress_value is None)
            elif indexed:
                bind_text(self.keyframe_index_button, 'setText', "Index chargé")
                self.keyframe_index_button.setIndeterminate(False)
                self.keyframe_index_button.setProgress(None)
            elif self.keyframe_note.startswith("Index distant indisponible"):
                size = self._keyframe_source_size()
                bind_text(self.keyframe_index_button, 'setText', "Réessayer l’index" + (f" · ≈ {human_size(size)}" if size else ""))
                self.keyframe_index_button.setIndeterminate(False)
                self.keyframe_index_button.setProgress(None)
            else:
                size = self._keyframe_source_size()
                bind_text(self.keyframe_index_button, 'setText', "Télécharger l’index" + (f" · ≈ {human_size(size)}" if size else ""))
                self.keyframe_index_button.setIndeterminate(False)
                self.keyframe_index_button.setProgress(None)
        if video_mode and available and not indexed and not self.keyframe_loading and not self.keyframe_note:
            self.keyframe_note = (
                "Pour choisir un extrait sans réencodage, téléchargez d’abord "
                "l’index des images-clés.")
        elif not video_mode:
            self.keyframe_note = ""
        if hasattr(self, "clip_note_var"):
            error = self._clip_validation_error()
            self.clip_note_var.set(error or self.keyframe_note)

    def _request_keyframe_index(self):
        if not self.analysis_data or self.keyframe_loading:
            return
        self._start_keyframe_index(self.analysis_data)

    def _keyframe_source(self, data=None):
        data = data or self.analysis_data or {}
        selected_video = self._selected_video()
        selected_codec = (
            codec_name((selected_video or {}).get("vcodec"))
            if selected_video else None)
        formats = [
            fmt for fmt in (data.get("formats") or [])
            if fmt.get("vcodec") not in (None, "none")
            and isinstance(fmt.get("height"), (int, float))
            and fmt.get("format_id") is not None
        ]
        same_codec = [
            fmt for fmt in formats
            if codec_name(fmt.get("vcodec")) == selected_codec
        ]
        formats = same_codec or formats
        if not formats:
            return None
        return min(formats, key=lambda fmt: (
            estimated_bytes(fmt, data.get("duration")) or float("inf"),
            fmt.get("tbr") or float("inf"), fmt.get("height") or float("inf")))

    def _keyframe_source_size(self):
        source = self._keyframe_source()
        return estimated_bytes(
            source, (self.analysis_data or {}).get("duration")) if source else None

    def _start_keyframe_index(self, data):
        """Index keyframes from the smallest stream so the range control can snap."""
        self.keyframe_token += 1
        token = self.keyframe_token
        self.keyframe_times = []
        self.clip_slider.setAllowedValues(None)
        duration = data.get("duration")
        selected_video = self._selected_video()
        selected_codec = codec_name((selected_video or {}).get("vcodec")) if selected_video else None
        self.keyframe_index_codec = selected_codec
        self.keyframe_loading = True
        self.keyframe_progress_value = None
        self.keyframe_progress_text = "Connexion à YouTube"
        source = self._keyframe_source(data)
        if source is None or not isinstance(duration, (int, float)) or duration <= 0:
            self.keyframe_loading = False
            self.keyframe_index_codec = None
            self.keyframe_note = "Index des images-clés indisponible pour ce flux."
            self._update_clip_index_controls()
            return
        cache_key = (str(data.get("id") or ""), str(source.get("format_id") or ""))
        cached = self.keyframe_cache.get(cache_key)
        if cached:
            self.events.put(("keyframes_ready", token, cached, float(duration)))
            self._update_clip_index_controls()
            return
        self.keyframe_note = ""
        self._update_clip_index_controls()
        url = data.get("webpage_url") or self.url_var.get().strip()

        def worker():
            self._keyframe_index_worker(
                url,
                str(source.get("format_id")), token, float(duration), cache_key,
                self._session_requested())

        threading.Thread(target=worker, daemon=True).start()



    def _apply_clip_time_entries(self):
        if not self.clip_slider.isEnabled() or self._clip_validation_error():
            return
        start = parse_timecode(self.clip_start_var.get()) or 0
        end = parse_timecode(self.clip_end_var.get())
        if end is None:
            end = self.clip_slider.maximum() / 1000
        self.clip_slider.setSelection(round(start * 1000), round(end * 1000))

    def _close_app(self):
        log_event('ui.close_requested', download_active=self.download_thread is not None,
                  queued_downloads=len(self.queued_downloads), tabs=len(self._video_tab_urls()))
        self.shutdown_after_downloads_check.setChecked(False)
        if self._shutdown_dialog is not None:
            self._shutdown_dialog.reject()
        self.dependency_manager.cancel.set()
        if self.dependency_busy:
            self.dependency_summary_var.set("Arrêt de l’opération en cours…")
            self.after(100, self._close_app)
            return
        self._save_download_checkpoint(flush=True)
        self.cancel_requested = True
        self.verification_cancel.set()
        if self.queue_analysis_orchestrator is not None:
            self.queue_analysis_orchestrator.cancel()
        with self.analysis_lock:
            if self.analysis_process:
                terminate_process_tree(self.analysis_process)
        if self.download_process:
            terminate_process_tree(self.download_process)
        if self.native_reader:
            self.cancel_requested = True
            self.native_reader.cancel()
        if self.subtitle_process:
            terminate_process_tree(self.subtitle_process)
        if self.keyframe_process:
            terminate_process_tree(self.keyframe_process)
        self._finish_close()

    def _open_verification(self, resume=None):
        """Open one isolated, user-operated YouTube window and resume afterward."""
        if self.verification_thread is not None:
            return
        if resume is None:
            resume = self.verification_resume
        original_url = (resume[0] if resume and len(resume) > 1
                        else self.url_var.get().strip())
        if not is_youtube_url(original_url):
            self.status_var.set("Collez d’abord le lien d’une vidéo YouTube.")
            return
        url = "https://" + original_url[7:] if original_url.startswith("http://") else original_url
        self.verification_attempted.add(original_url)
        self.verification_resume = resume
        self.verification_cancel = threading.Event()
        self.verification_continue = threading.Event()
        token = self.analysis_token
        self.analysis_running = False
        self._set_analysis_indicator(False)
        self.verification_button.setEnabled(False)
        self.access_note_var.set("Vérification YouTube dans une petite fenêtre…")
        self.status_var.set("Terminez le contrôle Google dans la fenêtre YouTube.")

        def worker():
            try:
                credentials = VerificationBrowser().verify(
                    url, self.verification_cancel, self.verification_continue,
                    lambda message: self.events.put(("verification_note", token, message)))
                self.events.put(("verification_done", token, credentials, resume))
            except Exception as error:
                self.events.put(("verification_failed", token, str(error)))

        self.verification_thread = threading.Thread(target=worker, daemon=True)
        self.verification_thread.start()
        self._update_download_button_state()

    def _finish_verification_ui(self):
        self.verification_thread = None
        self.verification_button.setEnabled(True)
        self._update_download_button_state()

    def _resume_verified_download(self, request):
        self.download_verification_pending = False
        self._queue_finished_request = None
        progress = (self.download_progress_value
                    if self.active_download_job and self.active_download_job['request'] == request else 0.0)
        self.last_download_request = request
        self.cancel_requested = False
        self._start_download_progress(progress)
        self._reset_download_log("Connexion à YouTube avec la session vérifiée.")
        bind_text(self.cancel_button, 'setText', "Annuler")
        self.cancel_button.setEnabled(True)
        self.download_thread = self.orchestrator.download(DownloadRequest(
            request[0], request[1], request[2], request[3], request[4],
            request[5], request[6], self.verified_credentials), validate=False)
        self._save_download_checkpoint()
        self._update_download_button_state()
        self._render_download_queue()


    def _start_download(self):
        log_event('ui.download_requested', url=self.url_var.get().strip())
        url = self.url_var.get().strip()
        if self._transfer_busy() and url == self.active_queue_url:
            return
        if self.dependency_busy or self.dependency_blocked:
            self._show_dependencies()
            return
        sel = self._selection()
        if not sel or self._clip_validation_error() or self.keyframe_loading:
            return

        # The folder dialog runs a Qt event loop: the active download can finish
        # and switch tabs before it returns. Capture the requested tab first.
        mode = self.mode_var.get()
        container = self.container_var.get()
        clip = self._clip_range()
        subtitle_lang = self._selected_subtitle()
        data = self.analysis_data
        tracks = tuple(self.subtitle_tracks)
        title = self.title_var.get()
        settings = self._capture_tab_settings()
        folder = self._request_download_folder()
        if not folder:
            return

        try:
            os.makedirs(folder, exist_ok=True)
        except Exception as e:
            messagebox.showerror(APP_TITLE, f"Impossible de créer le dossier :\n{e}")
            return

        if ("+" in sel["format"] or container != "Auto" or clip) and not self.ffmpeg:
            messagebox.showerror(
                APP_TITLE,
                "FFmpeg est nécessaire pour fusionner les flux ou changer de conteneur.\n\n"
                "Installez FFmpeg depuis la section Dépendances "
                "ou ajoute FFmpeg au PATH Windows.",
            )
            return

        fmt = sel["format"]

        # Format IDs belong to the YouTube client used during analysis.  A
        # partial download may have to use another client, where the same
        # 720p/H.264/AAC streams have different IDs.  Match their properties
        # and require direct HTTPS streams; never downgrade the chosen quality.
        if clip and mode != "subtitles":
            fmt = clip_format_selector(
                mode, sel.get("video"), sel.get("audio"), sel.get("audios"))

        # Capture every interface-dependent value on the main thread.
        video_ext = (sel.get("video") or {}).get("ext")
        audio_ext = (sel.get("audio") or {}).get("ext")
        audio_count = len(sel.get("audios") or (
            [sel["audio"]] if sel.get("audio") else []))

        if mode == "subtitles":
            merge_format = None
        elif mode in ("video", "video_only") and subtitle_lang:
            # Matroska accepts native SRT tracks and multiple subtitle
            # languages without re-encoding the video or audio.
            merge_format = "mkv"
        elif container != "Auto":
            merge_format = container.lower()
        elif mode == "video" and audio_count > 1:
            merge_format = "mkv"
        elif mode in ("video", "video_only") and "+" in fmt:
            if video_ext == "webm" and audio_ext in (None, "webm"):
                merge_format = "webm"
            else:
                merge_format = "mkv"
        else:
            merge_format = None

        request = (url, folder, mode, fmt, subtitle_lang, merge_format, clip)
        settings['folder'] = folder
        self.queue_tab_settings[url] = settings
        job = {'request': request, 'selection': sel,
               'data': data, 'tracks': tracks, 'title': title, 'settings': settings}
        self._queue_or_launch_job(job)

    def _queue_or_launch_job(self, job):
        if self._shutdown_dialog is not None:
            self._shutdown_dialog.reject()
        url = job['request'][0]
        if self.editing_queued_url == url:
            self.editing_queued_url = None
        if self._transfer_busy() or self.queued_downloads:
            self.queued_downloads[url] = job
            self._save_download_checkpoint()
            self._render_download_queue(selected_download=url)
            self._update_download_button_state()
            if not self._transfer_busy():
                self.after_idle(self._activate_next_queued_url)
            return
        self._launch_download_request(job)

    def _launch_download_request(self, job, *, progress=0.0, select_tab=True):
        request = job['request']
        url, folder, mode, fmt, subtitle_lang, merge_format, clip = request
        self.completed_downloads.pop(url, None)
        if self.completed_selection == url:
            self.completed_selection = None
        sel = job['selection']
        self.active_download_job = job
        self.download_retry_pending = False
        self.resume_download_on_start = False
        self.active_download_data = job['data']
        self.active_download_tracks = job['tracks']
        self.last_download_request = request
        self._queue_finished_request = None
        self.cancel_requested = False
        self.download_verification_pending = False
        self.subtitle_retry_pending = False
        self.failed_subtitle_request = None
        self.failed_subtitle_error = ""
        self.failed_subtitle_completed = ()
        self.failed_subtitle_missing = ()
        self._set_subtitle_retry_available(False)
        self._clear_download_result_actions()
        self._start_download_progress(progress)
        self._reset_download_log("Connexion à YouTube.")
        self.download_button.setEnabled(False)
        bind_text(self.cancel_button, 'setText', "Annuler")
        self.cancel_button.setEnabled(True)
        if url in self.pending_urls:
            self.pending_urls.remove(url)
            if self.active_queue_url and self.active_queue_url != url:
                self.pending_urls.insert(0, self.active_queue_url)
        self.active_queue_url = url
        if select_tab:
            self.queue_view_url = url
        partial_path = job.get('partial_path')
        if partial_path:
            self._append_download_detail(f"Fichier partiel : {Path(partial_path).name}.")
            if select_tab:
                self._show_queue_tab(url)
        else:
            self._append_download_detail(f"Mode demandé : {mode}.")
            self._append_download_detail(f"Sélection yt-dlp : {fmt}.")
            self._append_download_detail(
                "Conteneur final : " + (merge_format or "conteneur source") + ".")
        selected_video = sel.get("video")
        if selected_video:
            self._append_download_detail(
                f"Vidéo : {selected_video.get('height') or '?'}p, "
                f"{selected_video.get('fps') or '?'} i/s, "
                f"codec {selected_video.get('vcodec') or 'inconnu'}, "
                f"format {selected_video.get('format_id') or 'inconnu'}.")
        for audio_index, audio in enumerate(sel.get("audios") or (
                [sel["audio"]] if sel.get("audio") else []), start=1):
            self._append_download_detail(
                f"Audio {audio_index} : {self._audio_language_label(audio)}, "
                f"codec {audio.get('acodec') or 'inconnu'}, "
                f"{self._audio_rate_label(audio)}, "
                f"format {audio.get('format_id') or 'inconnu'}.")
        if clip:
            self._append_download_detail(
                f"Extrait demandé : {format_timecode(clip[0])} → "
                f"{format_timecode(clip[1]) if clip[1] is not None else 'fin'}.")
        if subtitle_lang:
            self._append_download_detail(
                f"Sous-titres demandés : {subtitle_lang}.")
        self.download_thread = self.orchestrator.download(DownloadRequest(
            url, folder, mode, fmt, subtitle_lang, merge_format, clip,
            self._session_requested(), partial_path=partial_path), validate=False)
        self._save_download_checkpoint()
        self._render_download_queue()
        self._update_download_button_state()

    def _start_download_progress(self, progress):
        self.download_progress_value = progress
        self.list_transfer_eta = None
        self.list_transfer_speed = None
        self.download_progress.setFormat("%p %")
        self.download_progress.configure(
            mode='determinate' if progress else 'indeterminate', value=progress)
        self.progress_text_var.set("" if progress else "0 o / —")

    def _hold_failed_download(self):
        """Keep the captured request and its partial files until an explicit choice."""
        if self.active_download_job is None:
            self._finish_active_queue_item()
            return
        self.download_retry_pending = True
        self.download_verification_pending = False
        self.download_progress.setFormat("%p %")
        self.download_progress.configure(mode='determinate', value=self.download_progress_value)
        self.progress_text_var.set("")
        self._save_download_checkpoint()
        self._render_download_queue()
        self._update_download_button_state()

    def _resume_failed_download(self):
        if not self.download_retry_pending or self.download_thread is not None:
            return
        if self.dependency_busy or self.dependency_blocked:
            self._show_dependencies()
            return
        job = self.active_download_job
        # The visible tab may belong to another queued video. Use the saved
        # request, including its exact destination, formats and clip boundaries.
        viewed_url = self.url_var.get().strip()
        self._launch_download_request(job, progress=self.download_progress_value)
        # Keep the tab the user was inspecting selected while the saved job
        # resumes in the transfer area.
        if viewed_url in self.pending_urls and viewed_url != self.url_var.get().strip():
            self._show_queue_tab(viewed_url)
        self.queue_view_url = self.url_var.get().strip()
        self._render_download_queue()
        if self._download_restarts_clip(job):
            self._append_download_detail("Reprise de l’extrait depuis son début, dans le même dossier.")
        else:
            self._append_download_detail("Reprise dans le même dossier, avec les fichiers partiels conservés.")

    def _skip_failed_download(self):
        if self.download_retry_pending:
            self._finish_active_queue_item(keep_tab=True)
            self._render_download_queue()
            self._update_download_button_state()

    def _show_updated_download_formats(self, catalog, diagnostic):
        self.download_thread = None
        self.download_progress.stop()
        self.cancel_button.setEnabled(False)
        if diagnostic:
            self._append_download_detail(diagnostic)
        job = self.active_download_job
        if not catalog or not job:
            self._hold_failed_download()
            messagebox.showwarning(
                APP_TITLE,
                "Le format choisi est indisponible et la liste des flux n’a pas pu être actualisée. "
                "Cliquez sur Reprendre pour réessayer.",
            )
            return

        url = job["request"][0]
        key = (url, self.access_var.get())
        self.analysis_cache[key] = (time.monotonic(), catalog)
        self.tab_analysis_data[key] = catalog
        self._finish_active_queue_item(keep_tab=True, keep_selection=True)
        self._show_queue_tab(url)
        self.status_var.set("Formats actualisés · choisissez à nouveau vos flux.")
        self._update_download_button_state()
        messagebox.showinfo(
            APP_TITLE,
            "Le format choisi est indisponible. Les formats de cette vidéo ont été actualisés. "
            "Choisissez les flux souhaités, puis relancez le téléchargement.",
        )

    def _download_progress_labels(self):
        return progress_labels(self.active_download_data or self.analysis_data)

    def _subtitle_is_auto_translated(self, code):
        tracks = (self.active_download_tracks if self.active_download_tracks is not None
                  else self.subtitle_tracks)
        return any(track.get('code') == code and track.get('kind') == 'traduction auto'
                   for track in tracks)


    @staticmethod
    def _download_restarts_clip(job):
        request = job['request']
        return request[6] is not None and request[2] != 'subtitles'

    def _download_can_pause(self):
        return (self.download_thread is not None and self.active_download_job is not None
                and not self.subtitle_retry_pending)

    def _pause_download(self):
        log_event('ui.pause_requested', url=self._current_download_url())
        if not self._download_can_pause() or self.download_pause_requested or self.cancel_requested:
            return
        self.download_pause_requested = True
        self.pause_download_button.setEnabled(False)
        self.cancel_button.setEnabled(False)
        self.download_progress.setFormat("%p %")
        self.progress_text_var.set("Mise en pause…")
        self.orchestrator.cancel()
        self._save_download_checkpoint()

    def _cancel_download(self):
        log_event('ui.cancel_requested', url=self._current_download_url())
        if not self.download_thread or self.download_pause_requested:
            return

        self.cancel_requested = True
        self.pause_download_button.setEnabled(False)
        self.download_progress.setFormat("%p %")
        self.orchestrator.cancel()
        self._save_download_checkpoint()
        self.cancel_button.setEnabled(False)
        bind_text(self.cancel_button, 'setText', "Annulation…")
        self._append_download_detail(
            "Annulation demandée : arrêt de la connexion et des traitements en cours.")


    def _reset_download_log(self, first_line):
        self.download_detail_started = time.monotonic()
        self.download_detail_lines = []
        self.progress_detail_var.set("")
        self._append_download_detail(first_line)

    def _append_download_detail(self, message):
        cleaned = diagnostic_log(str(message or "")).strip()
        if not cleaned:
            return
        elapsed = max(0, int(time.monotonic() - (
            self.download_detail_started or time.monotonic())))
        minutes, seconds = divmod(elapsed, 60)
        for line in cleaned.splitlines():
            line = line.strip()
            if not line:
                continue
            entry = f"[{minutes:02}:{seconds:02}] {line}"
            if self.download_detail_lines and self.download_detail_lines[-1] == entry:
                continue
            self.download_detail_lines.append(entry)
        self.download_detail_lines = self.download_detail_lines[-100:]
        self.progress_detail_var.set("\n".join(self.download_detail_lines))

    def _safe_download_results(self, paths, folder):
        base = os.path.realpath(folder)
        results = []
        for path in paths:
            candidate = os.path.realpath(path)
            try:
                inside = os.path.commonpath((base,candidate)) == base
            except ValueError:
                inside = False
            if inside and os.path.isfile(candidate) and candidate not in results:
                results.append(candidate)
        return results

    def _open_download_result(self, record=None):
        paths = (self._safe_download_results(record['paths'], record['folder'])
                 if record else self.download_results)
        path = next((item for item in paths if os.path.isfile(item)), None)
        if not path:
            self._reveal_download_result(record)
            return
        try:
            if os.name == 'nt': os.startfile(path)
            else: subprocess.Popen(['xdg-open',path])
        except Exception as error:
            messagebox.showerror(APP_TITLE,f'Impossible d’ouvrir le fichier :\n{error}')

    def _reveal_download_result(self, record=None):
        paths = (self._safe_download_results(record['paths'], record['folder'])
                 if record else self.download_results)
        path = next((item for item in paths if os.path.isfile(item)), None)
        folder = os.path.dirname(path) if path else (record['folder'] if record else self.download_result_folder)
        try:
            if os.name == 'nt' and path:
                subprocess.Popen(['explorer.exe','/select,',path],creationflags=CREATE_NO_WINDOW)
            elif os.name == 'nt':
                os.startfile(folder)
            else:
                subprocess.Popen(['xdg-open',folder])
        except Exception as error:
            messagebox.showerror(APP_TITLE,f'Impossible d’ouvrir le dossier :\n{error}')

    def _download_again(self):
        self.failed_subtitle_request = None
        self.failed_subtitle_error = ""
        self.failed_subtitle_completed = ()
        self.failed_subtitle_missing = ()
        self._set_subtitle_retry_available(False)
        self._clear_download_result_actions()
        self._update_download_button_state()
        self._start_download()

    def _offer_failed_subtitle_retry(self):
        request = self.failed_subtitle_request
        if not request or self.download_thread is not None:
            return
        if self._ask_subtitle_retry(
                self.failed_subtitle_error,
                request[6] if len(request) > 6 else None,
                self.failed_subtitle_completed,
                self.failed_subtitle_missing):
            self._retry_failed_subtitles()
        else:
            self._finish_active_queue_item(partial=bool(self.download_results))

    def _retry_failed_subtitles(self):
        request = self.failed_subtitle_request
        if not request or self.download_thread is not None:
            return
        self.subtitle_retry_pending = True
        self._queue_finished_request = None
        self.cancel_requested = False
        self._start_download_progress(0.0)
        self._reset_download_log(
            "Nouvelle série d’essais auprès de YouTube pour les sous-titres.")
        bind_text(self.download_button, 'setText', "Sous-titres…")
        self.download_button.setEnabled(False)
        bind_text(self.cancel_button, 'setText', "Annuler")
        self.cancel_button.setEnabled(True)
        self.download_thread = self.orchestrator.download(DownloadRequest(
            request[0], request[1], request[2], request[3], request[4],
            request[5], request[6], self._session_requested()), validate=False)
        self._render_download_queue()

    def _poll_events(self):
        self._poll_queue_analysis_events()
        try:
            while True:
                event = self.orchestrator.next_event()
                kind = event[0]
                if kind.startswith(('dependency', 'dependencies', 'verification')):
                    log_event('ui.event', kind=kind)

                if self.closing_transfer_tab_url and (
                        kind in ('download_error', 'download_formats_changed')
                        or (kind == 'verification_required' and event[1] is None)):
                    kind = 'download_cancelled'
                if (kind in ('download_done', 'download_cancelled', 'download_error',
                             'download_formats_changed',
                             'subtitle_retry_done', 'subtitle_retry_failed')
                        or (kind == 'verification_required' and event[1] is None)):
                    self.download_progress.setFormat("%p %")

                if self.download_pause_requested:
                    stopped = (kind in ('download_cancelled', 'download_error', 'download_formats_changed')
                               or (kind == 'verification_required' and event[1] is None))
                    if stopped:
                        self.download_pause_requested = False
                        self.cancel_requested = False
                        self.download_thread = None
                        self.cancel_button.setEnabled(False)
                        if self._download_restarts_clip(self.active_download_job):
                            self.download_progress_value = 0.0
                        self._append_download_detail("Téléchargement mis en pause.")
                        if self.priority_download_url:
                            self._start_priority_download()
                        else:
                            self._hold_failed_download()
                        continue
                    if kind in ('download_done', 'subtitle_retry_done', 'subtitle_retry_failed'):
                        self.download_pause_requested = False
                        self.cancel_requested = False
                        self.priority_download_url = None

                if kind == "dependency_progress":
                    self.dependency_detail_var.set(event[1])
                    self.dependency_summary_var.set(event[1])
                    continue
                elif kind == "dependencies_done":
                    self._dependencies_finished(event[1], event[2])
                    continue
                elif kind == "dependency_updates_done":
                    _, self.dependency_updates, self.dependency_update_errors = event
                    self.dependency_busy = False
                    if self.dependency_updates:
                        count = len(self.dependency_updates)
                        self.dependency_summary_var.set(f'{count} mise(s) à jour disponible(s)')
                        self.dependency_detail_var.set(
                            'Sélectionnez Mettre à jour pour installer les versions officielles vérifiées.')
                    elif self.dependency_update_errors:
                        self.dependency_summary_var.set('Recherche des mises à jour incomplète')
                        self.dependency_detail_var.set('\n'.join(self.dependency_update_errors))
                    else:
                        self.dependency_summary_var.set('yt-dlp et FFmpeg sont à jour')
                        self.dependency_detail_var.set(
                            'Node et BgUtils suivent la version compatible intégrée à l’application.')
                    self._render_dependencies()
                    continue
                elif kind == "dependency_error":
                    self.dependency_busy = False
                    self.dependency_blocked = True
                    self.dependency_summary_var.set("Installation interrompue — ouvrez Gérer pour réessayer")
                    self.dependency_detail_var.set(event[1])
                    self._render_dependencies()
                    self._update_download_button_state()
                    continue

                if kind == "thumbnail":
                    _, video_id, content = event
                    self.thumbnail_pending.discard(video_id)
                    if content:
                        self._remember_tab_preview(self.thumbnail_cache, video_id, content)
                        if self.list_view:
                            self._render_download_queue()
                        else:
                            self._render_video_tabs(self._video_tab_urls())
                            self._update_download_queue_thumbnail(video_id, content)

                elif kind == "tab_preview_title":
                    _, video_id, title = event
                    self._remember_tab_preview(self.tab_preview_titles, video_id, title)
                    if title:
                        if self.list_view:
                            self._render_download_queue()
                        else:
                            self._render_video_tabs(self._video_tab_urls())

                elif kind == "verification_note":
                    _, token, message = event
                    if token == self.analysis_token:
                        self.access_note_var.set(message)

                elif kind == "verification_done":
                    _, token, credentials, resume = event
                    self._finish_verification_ui()
                    if token == self.analysis_token:
                        self.verified_credentials = credentials
                        self.analysis_cache.clear()
                        self.verification_button.hide()
                        self.access_note_var.set("Contrôle terminé · reprise avec la session vérifiée…")
                        if resume == ("keyframes",):
                            self._request_keyframe_index()
                        elif resume:
                            self._resume_verified_download(resume)
                        else:
                            self._start_analysis()

                elif kind == "verification_failed":
                    _, token, error = event
                    self._finish_verification_ui()
                    if token == self.analysis_token:
                        self.access_note_var.set(error)
                        self.status_var.set("Vérification interrompue · vous pouvez la rouvrir.")
                        self.analysis_retry_button.show()
                        self.verification_button.show()
                        if self.download_verification_pending:
                            self._hold_failed_download()

                elif kind == "verification_required":
                    _, token, error, request = event
                    if token is not None and token != self.analysis_token: continue
                    if (token is not None and self.active_analysis_key
                            and self.active_analysis_key[0] != self.url_var.get().strip()):
                        self.analysis_running = False
                        self.queue_analysis_errors[self.active_analysis_key[0]] = str(error)
                        self._render_download_queue()
                        continue
                    already_verified = bool(
                        request is not None
                        and (
                            self.verified_credentials is not None
                            or request[0] in self.verification_attempted
                        )
                    )
                    self.analysis_diagnostics = diagnostic_log(error)
                    if token is not None and self._schedule_rate_limit_retry(error):
                        continue
                    self.status_var.set(extraction_issue(error))
                    self.analysis_running = False
                    self._set_analysis_indicator(False)
                    if token is not None:
                        self.analysis_retry_button.show()
                    self.download_progress.stop()
                    self.download_progress.configure(mode="determinate")
                    self.cancel_button.setEnabled(False)
                    self.download_thread = None
                    if request is not None:
                        self.download_verification_pending = True
                    self.status_var.set(extraction_issue(error))
                    self.access_note_var.set("L’accès automatique n’a pas levé le refus YouTube. Détails dans Diagnostic.")
                    self._update_download_button_state()
                    self.verification_resume = request
                    if already_verified:
                        self.progress_text_var.set("YouTube refuse encore le téléchargement après la vérification.")
                        self.access_note_var.set(
                            "CAPTCHA validé, mais la limitation YouTube persiste. La vérification ne sera pas relancée en boucle.")
                        self.verification_button.hide()
                        messagebox.showerror(
                            APP_TITLE,
                            "Le CAPTCHA a bien été validé, mais YouTube refuse encore ce téléchargement.\n\n"
                            "La fenêtre de vérification ne sera pas rouverte automatiquement.\n\n"
                            "Diagnostic réel :\n" + self.analysis_diagnostics[-1000:],
                        )
                        self._hold_failed_download()
                        continue
                    self.verification_button.show()
                    if request is not None and self._ask_download_verification():
                        self._open_verification(request)
                    elif request is not None:
                        self._hold_failed_download()
                    self._render_download_queue()

                elif kind == "session_unavailable":
                    _, token = event
                    if token == self.analysis_token:
                        self.brave_unavailable = True
                        self.access_note_var.set("Brave inaccessible : accès sans session. Fermez Brave puis Réessayer pour utiliser sa session.")

                elif kind == "analysis_ok":
                    _, token, data = event
                    if token == self.analysis_token:
                        self.analysis_running = False
                        if (self.active_analysis_key
                                and self.active_analysis_key[0] != self.url_var.get().strip()):
                            self.analysis_cache[self.active_analysis_key] = (
                                time.monotonic(), data)
                            self.tab_analysis_data[self.active_analysis_key] = data
                            self.queue_analysis_completed.add(self.active_analysis_key)
                            self._render_download_queue()
                            continue
                        self.rate_limit_retries.pop(self.url_var.get().strip(), None)
                        self.analysis_retry_button.hide()
                        self.verification_button.hide()
                        if self._schedule_partial_retry(data):
                            continue
                        if self._schedule_quality_retry(data):
                            continue
                        url = self.url_var.get().strip()
                        low_quality = suspicious_low_quality(data)
                        if not low_quality:
                            self.quality_retries.pop(url, None)
                            self.analysis_without_verified_session.discard(url)
                        if self.active_analysis_key:
                            self.analysis_cache[self.active_analysis_key] = (time.monotonic(), data)
                            if len(self.analysis_cache) > 20:
                                self.analysis_cache.pop(next(iter(self.analysis_cache)))
                        self._process_analysis(data)
                        self._save_download_checkpoint()
                        if not self.brave_unavailable:
                            if low_quality:
                                attempts = self.quality_retries.get(url, 0)
                                self.access_note_var.set(
                                    f"Avertissement : YouTube ne fournit toujours que 360p "
                                    f"après {attempts} nouvelles tentatives.")
                            elif data.get('_downloader_access_recovery') == 'without_provider':
                                self.access_note_var.set("Accès de secours YouTube · générateur de jetons indisponible.")
                            elif needs_verification(self.analysis_diagnostics):
                                self.access_note_var.set("Accès rétabli par BgUtils après la limitation YouTube.")
                            else:
                                self.access_note_var.set("Accès automatique YouTube · génération de jetons activée.")

                elif kind == "analysis_error":
                    _, token, error = event
                    if token == self.analysis_token:
                        if (self.active_analysis_key
                                and self.active_analysis_key[0] != self.url_var.get().strip()):
                            self.analysis_running = False
                            self.queue_analysis_errors[self.active_analysis_key[0]] = str(error)
                            self._render_download_queue()
                            continue
                        self.analysis_diagnostics = diagnostic_log(error)
                        self.analysis_running = False
                        self._set_analysis_indicator(False)
                        self.status_var.set("Échec de l'analyse")
                        self.title_var.set("")
                        self.download_button.setEnabled(False)
                        self.queue_analysis_errors[self.url_var.get().strip()] = str(error)
                        self._render_download_queue()
                        # Keep error concise, detailed error available via dialog.
                        messagebox.showerror(APP_TITLE, readable_error(error))

                elif kind == "keyframes_ready":
                    _, token, values, duration, *rest = event
                    if token == self.keyframe_token:
                        self.keyframe_loading = False
                        self.keyframe_progress_value = None
                        self.keyframe_progress_text = ""
                        self.keyframe_times = list(values)
                        if rest:
                            self.keyframe_cache[rest[0]] = list(values)
                            if len(self.keyframe_cache) > 12:
                                self.keyframe_cache.pop(next(iter(self.keyframe_cache)))
                        allowed = [0, round(float(duration) * 1000)]
                        allowed += [round(float(value) * 1000) for value in values]
                        self.clip_slider.setAllowedValues(allowed)
                        self.keyframe_note = (
                            f"Sélection calée sur {len(values)} images clés · "
                            "début arrondi avant, fin arrondie après.")
                        self._update_clip_index_controls()

                elif kind == "keyframe_progress":
                    _, token, text, progress = event
                    if token == self.keyframe_token:
                        self.keyframe_progress_text = text
                        if progress == "indeterminate":
                            self.keyframe_progress_value = None
                        elif progress is not None:
                            self.keyframe_progress_value = progress
                        self.keyframe_note = ""
                        self._update_clip_index_controls()

                elif kind == "keyframe_verification_required":
                    _, token, error = event
                    if token == self.keyframe_token:
                        self.keyframe_loading = False
                        self.keyframe_progress_value = None
                        self.keyframe_progress_text = ""
                        self.keyframe_index_codec = None
                        self.keyframe_note = (
                            "YouTube refuse l’index malgré trois essais · "
                            "vérification Google proposée.")
                        self._update_clip_index_controls()
                        if self._ask_download_verification():
                            self._open_verification(("keyframes",))

                elif kind == "keyframes_failed":
                    _, token, error = event
                    if token == self.keyframe_token:
                        self.keyframe_loading = False
                        self.keyframe_progress_value = None
                        self.keyframe_progress_text = ""
                        self.keyframe_times = []
                        self.keyframe_index_codec = None
                        self.keyframe_note = (
                            "Index distant indisponible · réessayez pour pouvoir "
                            "sélectionner une partie de la vidéo.")
                        self._update_clip_index_controls()
                        messagebox.showwarning(
                            APP_TITLE,
                            "Le téléchargement de l’index a échoué après trois tentatives.\n\n"
                            + readable_error(error))

                elif kind == "clip_snapped":
                    _, start, end = event
                    if (self.last_download_request and self.url_var.get().strip()
                            != self.last_download_request[0]):
                        state = self.queue_tab_settings.get(self.last_download_request[0])
                        if state is not None:
                            state['clip_start'] = format_timecode(start)
                            state['clip_end'] = format_timecode(end) if end is not None else ''
                        continue
                    if end is not None:
                        allowed = list(self.clip_slider.allowedValues())
                        allowed += [round(float(start) * 1000), round(float(end) * 1000)]
                        self.clip_slider.setAllowedValues(allowed)
                        self.clip_slider.setSelection(
                            round(float(start) * 1000), round(float(end) * 1000))
                        self.keyframe_note = (
                            f"Extrait ajusté aux images clés : "
                            f"{format_timecode(start)} → {format_timecode(end)}.")
                        self._clip_changed()

                elif kind == "download_progress":
                    _, percent, details, *estimate = event
                    if percent is not None:
                        self.download_progress.stop()
                        self.download_progress.configure(mode="determinate")
                        # A direct cut can be followed by a full-source fallback.
                        # Keep one monotonic task-level bar instead of visually
                        # restarting it when the internal strategy changes.
                        self.download_progress_value = max(
                            self.download_progress_value, max(0, min(100, percent)))
                        self.download_progress["value"] = self.download_progress_value
                        if (self.active_download_job and not self.cancel_requested
                                and self.download_progress_value > 0
                                and time.monotonic() - self._download_checkpoint_at >= 10):
                            self._save_download_checkpoint()
                    if not self.download_pause_requested:
                        remaining = estimate[0] if estimate else None
                        self.list_transfer_eta = remaining
                        self.list_transfer_speed = estimate[1] if len(estimate) > 1 else None
                        self.download_progress.setFormat(
                            "%p % · " + remaining_time_label(remaining) if remaining else "%p %")
                        self.progress_text_var.set(details)
                    self._update_tab_download_progress()
                    if self.list_view:
                        self.download_table.refresh_progress()

                elif kind == "download_phase":
                    _, text = event
                    self._append_download_detail(text)

                elif kind == "subtitle_phase":
                    _, text = event
                    self._append_download_detail(text)

                elif kind == "download_wait":
                    _, elapsed, maximum, text = event
                    if elapsed >= 5 and (elapsed % 5 == 0 or elapsed >= maximum):
                        self.download_progress.setFormat("%p %")
                        self._append_download_detail(text)

                elif kind == "download_status":
                    _, text = event

                    low = text.lower()
                    # yt-dlp can recover from a transient 429 or a cache warning. Keep
                    # those lines for the final diagnostic, but do not leave a resolved
                    # warning presented as the current transfer state.
                    if (
                        text.startswith(("WARNING:", "ERROR:"))
                        or "error writing cache" in low
                        or text.lstrip().startswith("at ")
                    ):
                        self._append_download_detail(text)
                        continue

                    if "[Merger]" in text:
                        display = "Fusion audio / vidéo…"
                    elif "[ExtractAudio]" in text:
                        display = "Conversion audio…"
                    elif text.startswith("[youtube]"):
                        display = "Connexion à YouTube…"
                    elif text.startswith("[info]"):
                        display = "Préparation des flux…"
                    elif text.startswith("[download]"):
                        display = "Téléchargement des flux…"
                    else:
                        display = text

                    self._append_download_detail(display)

                elif kind == "subtitle_warning":
                    _, error, *rest = event
                    request = rest[0] if rest else None
                    completed = tuple(rest[1]) if len(rest) > 1 else ()
                    missing = tuple(rest[2]) if len(rest) > 2 else ()
                    if request is None and self.last_download_request:
                        url, folder, _, _, language, _, clip = self.last_download_request
                        request = (
                            url, folder, "subtitles_retry", "", language,
                            None, clip)
                    self.failed_subtitle_request = request
                    self.failed_subtitle_error = error
                    self.failed_subtitle_completed = completed
                    self.failed_subtitle_missing = missing
                    self._set_subtitle_retry_available(bool(request))

                elif kind == "subtitle_retry_failed":
                    _, error, request, *rest = event
                    paths = rest[0] if rest else []
                    completed = tuple(rest[1]) if len(rest) > 1 else ()
                    missing = tuple(rest[2]) if len(rest) > 2 else ()
                    completed = tuple(dict.fromkeys(
                        self.failed_subtitle_completed + completed))
                    self.download_progress.stop()
                    self.download_progress.configure(mode="determinate", value=100)
                    self.download_progress_value = 100.0
                    self.progress_text_var.set(
                        "Sous-titres partiellement téléchargés"
                        if completed else
                        "Vidéo téléchargée · sous-titres non obtenus")
                    self._append_download_detail(
                        "Sous-titres incomplets : une nouvelle tentative reste disponible.")
                    self.cancel_button.setEnabled(False)
                    self.download_thread = None
                    self.subtitle_retry_pending = True
                    self.failed_subtitle_request = request
                    self.failed_subtitle_error = error
                    self.failed_subtitle_completed = completed
                    self.failed_subtitle_missing = missing
                    additions = self._safe_download_results(
                        paths, request[1] if request else self.download_result_folder)
                    self.download_results.extend(
                        path for path in additions if path not in self.download_results)
                    self._set_subtitle_retry_available(True)
                    self._show_download_result_actions(
                        self.download_results,
                        self.download_result_folder
                        or (request[1] if request else ""))
                    self.after_idle(self._offer_failed_subtitle_retry)

                elif kind == "subtitle_retry_done":
                    _, paths, folder = event
                    self.download_progress.stop()
                    self.download_progress.configure(mode="determinate", value=100)
                    self.download_progress_value = 100.0
                    self.progress_text_var.set("Sous-titres téléchargés et synchronisés")
                    self._append_download_detail(
                        "Sous-titres téléchargés et synchronisés.")
                    self.cancel_button.setEnabled(False)
                    self.download_thread = None
                    additions = self._safe_download_results(paths, folder)
                    combined = list(self.download_results)
                    combined.extend(path for path in additions if path not in combined)
                    self.failed_subtitle_request = None
                    self.failed_subtitle_error = ""
                    self.failed_subtitle_completed = ()
                    self.failed_subtitle_missing = ()
                    self._set_subtitle_retry_available(False)
                    self._show_download_result_actions(
                        combined, self.download_result_folder or folder)
                    self._finish_active_queue_item(succeeded=not self.cancel_requested)

                elif kind == "download_done":
                    self.download_progress.stop()
                    self.download_progress.configure(mode="determinate", value=100)
                    self.download_progress_value = 100.0
                    self.progress_text_var.set("Téléchargement terminé")
                    self._append_download_detail(
                        "Téléchargement, assemblage et métadonnées terminés.")
                    bind_text(self.cancel_button, 'setText', "Annuler")
                    self.cancel_button.setEnabled(False)
                    self.download_thread = None
                    paths = event[1] if len(event) > 1 else []
                    folder = event[2] if len(event) > 2 else (
                        self.last_download_request[1] if self.last_download_request else self.folder_var.get())
                    paths = self._safe_download_results(paths,folder)
                    self._show_download_result_actions(paths,folder)
                    if self.failed_subtitle_request and not self.closing_transfer_tab_url:
                        self.subtitle_retry_pending = True
                        self._set_subtitle_retry_available(True)
                        self.after_idle(self._offer_failed_subtitle_retry)
                    else:
                        self._finish_active_queue_item(succeeded=not self.cancel_requested)

                elif kind == "download_cancelled":
                    self.download_progress.stop()
                    self.download_progress.configure(mode="determinate")
                    self._append_download_detail("Téléchargement annulé.")
                    bind_text(self.cancel_button, 'setText', "Annuler")
                    self.cancel_button.setEnabled(False)
                    self.download_thread = None
                    self.progress_text_var.set("")
                    self._clear_download_result_actions()
                    self._finish_active_queue_item(keep_tab=not self.closing_transfer_tab_url)
                    self._render_download_queue()
                    self._update_download_button_state()

                elif kind == "download_formats_changed":
                    self._show_updated_download_formats(event[1], event[2])

                elif kind == "download_error":
                    self.download_progress.stop()
                    self.download_progress.configure(mode="determinate")
                    _, error = event
                    self.progress_text_var.set("Erreur")
                    self._append_download_detail(f"Échec : {error}")
                    bind_text(self.cancel_button, 'setText', "Annuler")
                    self.cancel_button.setEnabled(False)
                    self.download_thread = None
                    self._update_download_button_state()
                    self._hold_failed_download()
                    messagebox.showerror(APP_TITLE, readable_error(error)
                        + "\n\nCliquez sur Reprendre le téléchargement pour réessayer dans le même dossier. La file d’attente est conservée.")

        except queue.Empty:
            pass

        self._continue_tab_analyses()
        self.after(100, self._poll_events)


if __name__ == "__main__":
    setup_logging(app_dir())
    app = YoutubeDownloaderApp(list_view='--onglets' not in sys.argv)
    app.show()
    raise SystemExit(application().exec())
