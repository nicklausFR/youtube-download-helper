"""Windows yt-dlp, FFmpeg, subtitle, and indexed clip engine."""

import json
from dataclasses import dataclass
from functools import wraps
from app_logging import (log_event, log_exception, engine_event, start_process,
                         process_result, verbose_options)
import os
from pathlib import Path
import queue
import re
import shutil
import subprocess
import tempfile
import threading
import time
import urllib.request
from contextlib import contextmanager
from clip_strategy import NativeClipUnsupported, download_indexed_stream
from download_rate import MAX_RATE_BPS, MIN_RATE_BPS
from core_chapters import _arranged_clip_chapters, _ffmetadata_escape
from core_formats import (human_size, language_base, language_label, MATROSKA_LANGUAGE_CODES, clip_format_selector, container_args)
from core_progress import (DownloadProgress, PROGRESS_PREFIX, PROGRESS_TEMPLATE, RESULT_PREFIX, RESULT_TEMPLATE, CLIP_INFO_PREFIX, progress_labels)
from core_subtitles import clean_rolling_srt_file, trim_srt_file, format_timecode
from partial_recovery import find_resumable_format
from verified_session import VerifiedCredentials
from windows_adapter import (CREATE_NO_WINDOW, terminate_process_tree, find_executable, ffprobe_for, _video_keyframe_times, _first_video_frame_time, _media_streams)
from youtube_access import (access_args, cookie_access_failed, diagnostic_log, needs_verification, request_access_options, clean_ansi, subtitle_retryable, format_access_unavailable, is_youtube_url)
from strategies import DEFAULT_ACCESS, DEFAULT_DOWNLOADS
from youtube_access import (
    ACCESS_FALLBACK_SEPARATOR, POT_VERSION_TIMEOUT_MESSAGE, pot_provider_failed,
    pot_provider_version_timed_out, without_pot_provider,
    transient_network_error,
    alternate_youtube_options,
)

ANALYSIS_TIMEOUT_SECONDS = 90
AUTOMATIC_ANALYSIS_TIMEOUT_SECONDS = 40
DOWNLOAD_STALL_SECONDS = 60
CLIP_STALL_SECONDS = 20
SUBTITLE_STALL_SECONDS = 45
SUBTITLE_MAX_SECONDS = 180
SUBTITLE_RETRY_DELAYS = (4, 8, 12, 20)
SUBTITLE_ATTEMPTS = len(SUBTITLE_RETRY_DELAYS) + 1
TRANSLATED_SUBTITLE_WAIT_SECONDS = 60
DOWNLOAD_RETRY_DELAYS = (2, 5, 10)

_DOWNLOAD_TERMINAL_EVENTS = {
    "download_done", "download_error", "download_cancelled",
    "subtitle_retry_done", "subtitle_retry_failed", "verification_required",
}
_worker_events = threading.local()
_RATE_RESTART = object()


@dataclass(frozen=True)
class _DownloadRetry:
    reason: str
    kind: str = "network"
    options: tuple | None = None


def _after_download_cleanup(method):
    """Publish completion only after subprocesses and parallel subtitles stop."""
    @wraps(method)
    def run(self, *args, **kwargs):
        pending = []
        _worker_events.sink = (self, pending)
        try:
            return method(self, *args, **kwargs)
        finally:
            del _worker_events.sink
            for event in pending:
                self.events.put(event)
    return run


class WindowsEngineMixin:
    """Operational methods shared by the Qt adapter and headless engine."""

    _arranged_clip_chapters = staticmethod(_arranged_clip_chapters)
    _ffmetadata_escape = staticmethod(_ffmetadata_escape)

    def _emit_event(self, event):
        engine_event(self, event)
        sink = getattr(_worker_events, "sink", None)
        if (sink is not None and sink[0] is self
                and event[0] in _DOWNLOAD_TERMINAL_EVENTS):
            sink[1].append(event)
        else:
            self.events.put(event)

    def set_download_rate_bps(self, limit):
        """Set the media transfer cap in bytes per second; zero is unlimited."""
        limit = int(limit)
        if not 0 <= limit <= MAX_RATE_BPS or (limit and limit < MIN_RATE_BPS):
            raise ValueError("Limite de débit invalide")
        if limit != getattr(self, "download_rate_bps", 0):
            log_event('download.rate_changed', engine=hex(id(self)), old_bps=getattr(self, 'download_rate_bps', 0), new_bps=limit)
            self.download_rate_bps = limit
            self.download_rate_revision = getattr(self, "download_rate_revision", 0) + 1

    def set_download_rate_mbps(self, value):
        """Compatibility entry point for callers using whole Mbit/s."""
        value = int(value)
        if not 0 <= value <= 1000:
            raise ValueError("La limite de débit doit être entre 0 et 1000 Mbit/s")
        self.set_download_rate_bps(value * 125_000)

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
        return self.current_url

    @contextmanager
    def _access_options(self, session=False, analysis=False):
        registry = getattr(self, "access_strategies", None)
        context = (registry.options(session, analysis) if registry is not None
                   else request_access_options(session, analysis))
        with context as options:
            data = ((getattr(self, "active_download_data", None) if not analysis else None)
                    or getattr(self, "analysis_data", None) or {})
            if not analysis and data.get('_downloader_access_recovery') == 'without_provider':
                options = without_pot_provider(options)
            elif not analysis and data.get('_downloader_access_recovery') == 'mweb':
                options = alternate_youtube_options(options)
            log_event('access.selected', engine=hex(id(self)), analysis=analysis,
                      session='verified' if isinstance(session, VerifiedCredentials) else ('browser' if session else 'anonymous'),
                      provider='--plugin-dirs' in options, recovery=data.get('_downloader_access_recovery'))
            yield verbose_options(options)

    def cancel(self, *, background=False):
        """Interrupt all active I/O owned by this engine."""
        log_event('engine.cancel', engine=hex(id(self)), background=background)
        self.cancel_requested = True
        self.analysis_token += 1
        self.keyframe_token += 1
        reader = self.native_reader
        processes = tuple(process for process in (
            self.analysis_process, self.keyframe_process, self.download_process, self.subtitle_process
        ) if process is not None)

        def interrupt():
            if reader is not None:
                reader.cancel()
            for process in processes:
                terminate_process_tree(process)

        if background and (reader is not None or processes):
            threading.Thread(target=interrupt, daemon=True).start()
        else:
            interrupt()


    def _analyze_worker(self, url, token, session=False, fallback=False):
        from youtube_access import youtube_video_id
        log_event('analysis.start', engine=hex(id(self)), video_id=youtube_video_id(url),
                  url=url, generation=token, timeout=ANALYSIS_TIMEOUT_SECONDS)
        deadline = time.monotonic() + ANALYSIS_TIMEOUT_SECONDS
        provider_fallback = False
        provider_error = ""
        network_retries = 0
        client_fallback = False
        try:
            while True:
                if token != self.analysis_token:
                    return
                with self._access_options(session, analysis=True) as options:
                    if provider_fallback:
                        options = without_pot_provider(options)
                    elif client_fallback:
                        options = alternate_youtube_options(options)
                    uses_provider = "--plugin-dirs" in options
                    remaining = deadline - time.monotonic()
                    if remaining <= 0:
                        raise TimeoutError(f"L’analyse dépasse {ANALYSIS_TIMEOUT_SECONDS} secondes. Réessayez plus tard.")
                    attempt_timeout = (min(AUTOMATIC_ANALYSIS_TIMEOUT_SECONDS, remaining)
                                       if uses_provider else remaining)
                    timed_out = False
                    log_event('analysis.attempt', engine=hex(id(self)), generation=token,
                              timeout=attempt_timeout, provider=uses_provider,
                              provider_fallback=provider_fallback, client_fallback=client_fallback,
                              network_retries=network_retries)
                    cmd = [self.ytdlp, *options, "--dump-single-json", "--no-playlist", url]
                    with self.analysis_lock:
                        if token != self.analysis_token:
                            return
                        proc = start_process(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                            text=True, encoding="utf-8", errors="replace", creationflags=CREATE_NO_WINDOW)
                        self.analysis_process = proc
                    stdout = stderr = ''
                    try:
                        stdout, stderr = proc.communicate(timeout=attempt_timeout)
                    except subprocess.TimeoutExpired as expired:
                        # Node inherits stderr while generating tokens. Killing only
                        # yt-dlp leaves that pipe open and communicate() can hang.
                        terminate_process_tree(proc)
                        stderr = expired.stderr or ""
                        try:
                            _, drained_stderr = proc.communicate(timeout=2)
                            stderr = drained_stderr or stderr
                        except subprocess.TimeoutExpired:
                            pass
                        if isinstance(stderr, bytes):
                            stderr = stderr.decode("utf-8", errors="replace")
                        timed_out = True
                        stdout = ""
                        message = (f"L’accès automatique n’a pas répondu dans les {AUTOMATIC_ANALYSIS_TIMEOUT_SECONDS} secondes prévues."
                                   if uses_provider else
                                   f"L’analyse dépasse {ANALYSIS_TIMEOUT_SECONDS} secondes. Réessayez plus tard.")
                        stderr = message + ("\n\n" + diagnostic_log(stderr) if stderr else "")
                    finally:
                        process_result(proc, phase='analysis', output=stderr, timeout=timed_out)
                        with self.analysis_lock:
                            if self.analysis_process is proc:
                                self.analysis_process = None
                if token != self.analysis_token:
                    return
                if not timed_out and proc.returncode == 0:
                    data = json.loads(stdout)
                    data["_downloader_diagnostics"] = diagnostic_log(stderr)
                    if provider_fallback:
                        data["_downloader_access_recovery"] = "without_provider"
                        reason = (POT_VERSION_TIMEOUT_MESSAGE if pot_provider_version_timed_out(provider_error)
                                  else "Échec ou délai dépassé de l’accès automatique.")
                        data["_downloader_diagnostics"] = (
                            f"Accès de secours sans BgUtils. {reason}\n"
                            + data["_downloader_diagnostics"])
                    elif client_fallback:
                        data['_downloader_access_recovery'] = 'mweb'
                    self._emit_event(("analysis_ok", token, data))
                    return
                error = stderr.strip() or stdout.strip() or "Erreur d’analyse"
                if (uses_provider and (pot_provider_failed(error) or timed_out)
                        and time.monotonic() < deadline):
                    provider_error = diagnostic_log(error)
                    provider_fallback = True
                    continue
                if session and fallback and cookie_access_failed(error):
                    session, fallback = False, False
                    self._emit_event(("session_unavailable", token))
                    continue
                if (uses_provider and not provider_fallback and not client_fallback
                        and (needs_verification(error) or 'http error 403' in error.lower())
                        and time.monotonic() < deadline):
                    client_fallback = True
                    continue
                if (not timed_out and transient_network_error(error)
                        and network_retries < 2 and deadline - time.monotonic() > 5):
                    network_retries += 1
                    # A temporary CDN/API failure needs a fresh extraction, not
                    # a verification prompt. The same global deadline still applies.
                    wait_until = min(deadline, time.monotonic() + network_retries)
                    while time.monotonic() < wait_until:
                        if token != self.analysis_token:
                            return
                        time.sleep(min(0.1, max(0, wait_until - time.monotonic())))
                    continue
                verification_required = needs_verification(error)
                if provider_error:
                    error = provider_error + ACCESS_FALLBACK_SEPARATOR + error
                if verification_required:
                    self._emit_event(("verification_required", token, error, None))
                else:
                    self._emit_event(("analysis_error", token, error))
                return
        except Exception as error:
            log_exception('analysis.exception', engine=hex(id(self)), generation=token)
            if token == self.analysis_token:
                self._emit_event(("analysis_error", token, str(error)))


    def _fetch_keyframe_source(self, url, format_id, folder, options, token, attempt):
        self._emit_event((
            "keyframe_progress", token, "Connexion à YouTube", "indeterminate"))
        output = os.path.join(folder, "%(id)s.%(ext)s")
        command = [
            self.ytdlp, "--newline", "--no-playlist", "--progress",
            "--progress-template", PROGRESS_TEMPLATE, "--print", RESULT_TEMPLATE,
            "--progress-delta", "0.2", "-f", format_id, "-o", output,
            *options, url,
        ]
        process = start_process(
            command, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
            text=True, encoding="utf-8", errors="replace", bufsize=1,
            creationflags=CREATE_NO_WINDOW,
        )
        self.keyframe_process = process
        assert process.stdout is not None
        line_queue = queue.Queue()
        finished = object()

        def reader():
            try:
                for line in process.stdout:
                    line_queue.put(line)
            except (OSError, ValueError):
                pass
            finally:
                line_queue.put(finished)

        read_thread = threading.Thread(target=reader, daemon=True)
        read_thread.start()
        last_output = time.monotonic()
        progress = DownloadProgress(format_id)
        result_path = None
        output_lines = []
        stalled = False
        while True:
            try:
                raw = line_queue.get(timeout=0.25)
            except queue.Empty:
                if token != self.keyframe_token:
                    terminate_process_tree(process)
                    break
                waiting = time.monotonic() - last_output
                self._emit_event((
                    "keyframe_progress", token,
                    "Connexion à YouTube", None))
                if waiting >= self.download_stall_seconds:
                    stalled = True
                    terminate_process_tree(process)
                    break
                if process.poll() is not None and not read_thread.is_alive():
                    break
                continue
            if raw is finished:
                break
            last_output = time.monotonic()
            line = clean_ansi(raw.strip())
            if line:
                output_lines.append(line)
                if len(output_lines) > 40:
                    output_lines.pop(0)
            if line.startswith(RESULT_PREFIX):
                try:
                    result_path = json.loads(line[len(RESULT_PREFIX):])
                except (TypeError, ValueError):
                    pass
                continue
            update = progress.parse(line)
            if update and update[0] is not None:
                self._emit_event((
                    "keyframe_progress", token,
                    "Téléchargement de l’index", update[0]))
        try:
            return_code = process.wait(timeout=8)
        except Exception:
            terminate_process_tree(process)
            return_code = -1
        process_result(process, phase='keyframe_index', output='\n'.join(output_lines), timeout=stalled)
        read_thread.join(timeout=2)
        try:
            process.stdout.close()
        except OSError:
            pass
        if self.keyframe_process is process:
            self.keyframe_process = None
        if token != self.keyframe_token:
            return None, "Opération remplacée."
        if return_code == 0:
            if isinstance(result_path, str) and os.path.isfile(result_path):
                return result_path, ""
            candidates = [
                os.path.join(folder, name) for name in os.listdir(folder)
                if os.path.isfile(os.path.join(folder, name))
                and not name.endswith((".part", ".ytdl"))
            ]
            if candidates:
                return max(candidates, key=os.path.getmtime), ""
        details = "\n".join(output_lines[-12:]).strip()
        if stalled:
            details = (
                f"Aucune donnée reçue pendant {self.download_stall_seconds} secondes.\n" + details)
        return None, details or f"yt-dlp a quitté avec le code {return_code}"


    def _keyframe_index_worker(self, url, format_id, token, duration, cache_key, session=False):
        temporary = tempfile.mkdtemp(prefix="youtube-keyframe-index-")
        last_error = ""
        try:
            access_attempts = [session, False, False]
            for attempt, access in enumerate(access_attempts, start=1):
                if attempt > 1:
                    delay = (4, 12)[attempt - 2]
                    for remaining in range(delay, 0, -1):
                        if token != self.keyframe_token:
                            return
                        self._emit_event((
                            "keyframe_progress", token,
                            f"Nouvelle tentative dans {remaining} s",
                            "indeterminate"))
                        time.sleep(1)
                try:
                    with self._access_options(access) as options:
                        source, last_error = self._fetch_keyframe_source(
                            url, format_id, temporary, options, token, attempt)
                except Exception as error:
                    source, last_error = None, str(error)
                if token != self.keyframe_token:
                    return
                if not source:
                    continue
                ffprobe = ffprobe_for(self.ffmpeg)
                if not ffprobe:
                    raise RuntimeError("FFprobe est absent.")
                self._emit_event((
                    "keyframe_progress", token,
                    "Analyse de l’index", 100))
                values = _video_keyframe_times(ffprobe, source, timeout=30)
                self._emit_event((
                    "keyframes_ready", token, values, duration, cache_key))
                return
            if (
                    needs_verification(last_error)
                    and not isinstance(session, VerifiedCredentials)
                    and self._current_url_for_engine() not in self.verification_attempted):
                self._emit_event(("keyframe_verification_required", token, last_error))
            else:
                self._emit_event(("keyframes_failed", token, last_error))
        except Exception as error:
            self._emit_event(("keyframes_failed", token, str(error)))
        finally:
            shutil.rmtree(temporary, ignore_errors=True)


    @_after_download_cleanup
    def _download_worker(self, url, folder, mode, fmt, subtitle_lang, merge_format,
                         clip=None, session=False, *, partial_path=None):
        from youtube_access import youtube_video_id
        log_event('download.start', engine=hex(id(self)), video_id=youtube_video_id(url),
                  url=url, folder=folder, mode=mode, format_id=fmt, subtitles=subtitle_lang,
                  container=merge_format, clip=clip, partial_path=partial_path,
                  rate_bps=getattr(self, 'download_rate_bps', 0))
        subtitle_job = None
        try:
            with self._access_options(session) as options:
                if partial_path:
                    self._resume_partial_file(url, Path(partial_path), options)
                    return
                strategy = getattr(self, "download_strategies", DEFAULT_DOWNLOADS).resolve(mode, clip)
                if mode in ("subtitles", "subtitles_retry"):
                    strategy.run(self, url, folder, mode, fmt, subtitle_lang,
                                 merge_format, clip, options, session, None)
                    return
                if mode in ("video", "video_only") and subtitle_lang:
                    stop_event = threading.Event()
                    subtitle_job = {
                        "stop": stop_event,
                    }

                    def download_subtitles_in_parallel():
                        subtitle_job["result"] = self._download_subtitle_files(
                            url, folder, subtitle_lang, list(options), clip,
                            stop_event=stop_event)

                    subtitle_job["thread"] = threading.Thread(
                        target=download_subtitles_in_parallel, daemon=True)
                    subtitle_job["thread"].start()

                    def finish_parallel_subtitles():
                        subtitle_job["thread"].join()
                        results, error = subtitle_job.get("result", ([], ""))
                        if error and not self.cancel_requested:
                            completed, missing = self._subtitle_language_outcome(
                                results, subtitle_lang)
                            retry_languages = ",".join(missing) or subtitle_lang
                            request = (
                                url, folder, "subtitles_retry", "", retry_languages,
                                None, clip)
                            self._emit_event((
                                "subtitle_warning", error, request,
                                completed, missing))
                        return results
                else:
                    finish_parallel_subtitles = None
                strategy.run(self, url, folder, mode, fmt, subtitle_lang,
                             merge_format, clip, options, session,
                             finish_parallel_subtitles)
        except Exception as error:
            log_exception('download.exception', engine=hex(id(self)))
            self._emit_event(("download_error", str(error)))
        finally:
            if subtitle_job and subtitle_job["thread"].is_alive():
                subtitle_job["stop"].set()
                if self.subtitle_process:
                    terminate_process_tree(self.subtitle_process)
                subtitle_job["thread"].join(timeout=10)

    def _read_partial_metadata(self, url, options):
        """Retry a transient extraction failure once before inspecting the partial."""
        for attempt in range(2):
            if self.cancel_requested:
                return None
            cmd = [self.ytdlp, *options, '--dump-single-json', '--no-playlist', url]
            proc = start_process(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                    text=True, encoding='utf-8', errors='replace',
                                    creationflags=CREATE_NO_WINDOW)
            self.download_process = proc
            timed_out = False
            stdout = stderr = ''
            try:
                if self.cancel_requested:
                    terminate_process_tree(proc)
                    return None
                stdout, stderr = proc.communicate(timeout=AUTOMATIC_ANALYSIS_TIMEOUT_SECONDS)
            except subprocess.TimeoutExpired as expired:
                timed_out = True
                terminate_process_tree(proc)
                stderr = expired.stderr or ""
                if isinstance(stderr, bytes):
                    stderr = stderr.decode('utf-8', errors='replace')
            finally:
                process_result(proc, phase='partial_metadata', output=stderr, timeout=timed_out)
                self.download_process = None

            if self.cancel_requested:
                return None
            if not timed_out and proc.returncode == 0:
                return json.loads(stdout)

            error = diagnostic_log(stderr).strip()
            final_error = error.rsplit('ERROR:', 1)[-1].lower()
            network_timeout = timed_out or 'timed out' in final_error or 'timeout' in final_error
            provider_failed = '--plugin-dirs' in options and (timed_out or pot_provider_failed(error))
            if error:
                self._emit_event(("download_phase", error[-1000:]))
            if attempt == 0 and (network_timeout or provider_failed):
                self._emit_event(("download_phase",
                                  "YouTube répond trop lentement · nouvel essai…"
                                  if network_timeout else "Accès automatique indisponible · nouvel essai…"))
                if provider_failed:
                    options = without_pot_provider(options)
                options = [*options, '--socket-timeout', '20']
                continue
            if network_timeout:
                raise TimeoutError("YouTube n’a pas répondu après deux tentatives. "
                                   "Le fichier partiel est conservé.")
            raise ValueError("Impossible d’identifier la vidéo du fichier partiel.\n" + error[-1000:])

    def _resume_partial_file(self, url, partial, options):
        if not partial.is_absolute() or partial.suffix.lower() != '.part':
            raise ValueError("Le chemin du fichier partiel est invalide.")
        output = partial.with_suffix('')
        if output.exists():
            raise ValueError("Un fichier terminé porte déjà ce nom dans le dossier.")
        original = partial.stat()
        if original.st_size < 4096 or partial.with_suffix('.ytdl').exists():
            raise ValueError("Ce fichier nécessite la demande de téléchargement d’origine "
                             "pour pouvoir être repris.")
        self._emit_event(("download_phase", "Identification du flux du fichier partiel…"))
        options = ['--ignore-config', *options]
        data = self._read_partial_metadata(url, options)
        if self.cancel_requested or data is None:
            self._emit_event(("download_cancelled",))
            return
        stream = find_resumable_format(partial, data, lambda: self.cancel_requested)
        if self.cancel_requested:
            self._emit_event(("download_cancelled",))
            return
        if stream is None:
            raise ValueError("Aucun flux compatible n’a pu être vérifié pour ce fichier. "
                             "Le fichier partiel est conservé.")
        current = partial.stat()
        if (current.st_size, current.st_mtime_ns) != (original.st_size, original.st_mtime_ns) or output.exists():
            raise ValueError("Le fichier a changé pendant la vérification. Réessayez une fois le transfert arrêté.")

        # Pin the download to the exact CDN stream whose bytes matched.
        with tempfile.TemporaryDirectory(prefix='yd-partial-') as folder:
            info = Path(folder) / 'stream.json'
            data['formats'] = [stream]
            data.pop('requested_downloads', None)
            info.write_text(json.dumps(data), encoding='utf-8')
            self._emit_event(("download_phase", "Flux vérifié · reprise du fichier partiel…"))
            self._download_with_access(
                url, str(output.parent), 'video_only', str(stream['format_id']), None, None,
                None, options, resume_source=(str(output), str(info)))


    def _wait_subtitle_retry(self, seconds, attempt, stop_event=None):
        for remaining in range(seconds, 0, -1):
            if self.cancel_requested or (stop_event and stop_event.is_set()):
                return False
            self._emit_event((
                "subtitle_phase",
                f"Sous-titres · nouvel essai {attempt}/{SUBTITLE_ATTEMPTS} "
                f"dans {remaining} s…"))
            for _ in range(10):
                if self.cancel_requested or (stop_event and stop_event.is_set()):
                    return False
                time.sleep(0.1)
        return True


    def _subtitle_is_auto_translated(self, code):
        return any(
            track.get("code") == code and track.get("kind") == "traduction auto"
            for track in self.subtitle_tracks)


    def _run_subtitle_download(
            self, url, output, subtitle_lang, options, stop_event=None):
        """Download languages separately so a successful one is never retried."""
        languages = tuple(
            code.strip() for code in str(subtitle_lang or "").split(",")
            if code.strip())
        if not languages:
            return False, "Aucune langue de sous-titres n’a été sélectionnée."

        errors = []
        for code in languages:
            success, error = self._run_subtitle_language(
                url, output, code, options,
                translated=self._subtitle_is_auto_translated(code),
                stop_event=stop_event)
            if not success:
                errors.append(f"{code} : {error}")
            if self.cancel_requested or (stop_event and stop_event.is_set()):
                break
        return not errors, "\n\n".join(errors)


    def _run_subtitle_language(
            self, url, output, subtitle_lang, options, translated=False,
            stop_event=None):
        """Try one caption track; a verified session can hurt timedtext access."""
        automatic_options = access_args(False, analysis=False)
        attempts = [list(options)] + [automatic_options] * len(SUBTITLE_RETRY_DELAYS)
        last_error = ""
        for index, attempt_options in enumerate(attempts, start=1):
            if index > 1:
                delay = SUBTITLE_RETRY_DELAYS[index - 2]
                if not self._wait_subtitle_retry(delay, index, stop_event):
                    return False, "Téléchargement des sous-titres annulé."
            self._emit_event((
                "subtitle_phase",
                f"Sous-titres · tentative {index}/{SUBTITLE_ATTEMPTS} "
                f"pour {subtitle_lang} auprès de YouTube"
                + (f" · stabilisation ~{TRANSLATED_SUBTITLE_WAIT_SECONDS} s…"
                   if translated else "…")))
            command = [
                self.ytdlp, *attempt_options, "--no-playlist", "--skip-download",
                "--write-subs", "--write-auto-subs", "--sub-format", "srt/vtt/best",
                "--convert-subs", "srt",
                *(["--ffmpeg-location", self.ffmpeg] if self.ffmpeg else []),
                *(["--sleep-subtitles", str(TRANSLATED_SUBTITLE_WAIT_SECONDS)]
                  if translated else []),
                "--sub-langs", subtitle_lang, "-o", output, url,
            ]
            process = start_process(
                command, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                text=True, encoding="utf-8", errors="replace",
                bufsize=1,
                creationflags=CREATE_NO_WINDOW,
            )
            self.subtitle_process = process

            line_queue = queue.Queue()
            reader_finished = object()

            def read_output():
                try:
                    for line in process.stdout:
                        line_queue.put(line)
                except (OSError, ValueError):
                    pass
                finally:
                    line_queue.put(reader_finished)

            reader = threading.Thread(target=read_output, daemon=True)
            reader.start()
            output_lines = []
            attempt_started = time.monotonic()
            last_output = attempt_started
            timeout_error = ""
            try:
                while True:
                    try:
                        line = line_queue.get(timeout=0.25)
                    except queue.Empty:
                        if self.cancel_requested or (stop_event and stop_event.is_set()):
                            terminate_process_tree(process)
                            break
                        if process.poll() is not None and not reader.is_alive():
                            break
                        now = time.monotonic()
                        stall_seconds = max(
                            self.subtitle_stall_seconds,
                            TRANSLATED_SUBTITLE_WAIT_SECONDS + 20
                            if translated else 0)
                        if now - last_output >= stall_seconds:
                            timeout_error = (
                                "Le serveur de sous-titres n’a transmis aucune nouvelle "
                                f"donnée pendant {stall_seconds:g} secondes "
                                "(timeout d’inactivité).")
                            terminate_process_tree(process)
                            break
                        if now - attempt_started >= self.subtitle_max_seconds:
                            timeout_error = (
                                "La tentative de sous-titres n’a pas terminé après "
                                f"{self.subtitle_max_seconds:g} secondes (timeout total).")
                            terminate_process_tree(process)
                            break
                        continue
                    if line is reader_finished:
                        break
                    last_output = time.monotonic()
                    cleaned = clean_ansi(line.strip())
                    if cleaned:
                        output_lines.append(cleaned)
                        if len(output_lines) > 80:
                            output_lines.pop(0)

                try:
                    process.wait(timeout=8)
                except Exception:
                    terminate_process_tree(process)
                reader.join(timeout=2)
                if process.stdout:
                    process.stdout.close()
                stdout = "\n".join(output_lines)
                last_error = timeout_error or stdout.strip()
            finally:
                process_result(process, phase='subtitles', output='\n'.join(output_lines), timeout=bool(timeout_error))
                self.subtitle_process = None
            if self.cancel_requested or (stop_event and stop_event.is_set()):
                return False, "Téléchargement des sous-titres annulé."
            if process.returncode == 0:
                return True, ""
            if not subtitle_retryable(last_error):
                break
        useful = [
            line for line in clean_ansi(last_error).splitlines()
            if any(term in line.lower() for term in (
                "error", "warning", "429", "403", "forbidden", "unable"))
        ]
        details = "\n".join(useful[-6:]).strip()
        return False, (
            f"YouTube refuse toujours les sous-titres après "
            f"{SUBTITLE_ATTEMPTS} tentatives.\n\n"
            + (details or "Le serveur timedtext n’a pas fourni de fichier SRT."))


    def _download_subtitle_files(
            self, url, folder, subtitle_lang, options, clip=None, stop_event=None):
        started = time.time()
        output = os.path.join(folder, "%(title)s.%(ext)s")
        success, error = self._run_subtitle_download(
            url, output, subtitle_lang, options, stop_event)
        results = []
        for name in os.listdir(folder):
            path = os.path.join(folder, name)
            if (
                    os.path.isfile(path)
                    and name.lower().endswith(".srt")
                    and os.path.getmtime(path) >= started - 1):
                clean_rolling_srt_file(path)
                if clip:
                    trim_srt_file(path, clip[0], clip[1])
                results.append(path)
        results.sort()
        if not results:
            if not success:
                return [], error
            return [], (
                "YouTube a terminé la requête, mais aucun nouveau fichier SRT "
                "n’a été créé.")
        return results, "" if success else error


    @staticmethod
    def _subtitle_language_outcome(paths, subtitle_lang):
        requested = tuple(
            code.strip() for code in str(subtitle_lang or "").split(",")
            if code.strip())
        names = [os.path.basename(path).lower() for path in paths]
        completed = tuple(
            code for code in requested
            if any(name.endswith("." + code.lower() + ".srt") for name in names))
        if len(requested) == 1 and paths and not completed:
            completed = requested
        missing = tuple(code for code in requested if code not in completed)
        return completed, missing


    def _download_subtitles_only(
            self, url, folder, subtitle_lang, options, session, clip=None,
            retry=False):
        results, error = self._download_subtitle_files(
            url, folder, subtitle_lang, options, clip)
        if self.cancel_requested:
            self._emit_event(("download_cancelled",))
            return
        completed, missing = self._subtitle_language_outcome(
            results, subtitle_lang)
        delivered_results = results
        if retry and results:
            media_path = next((
                path for path in self.download_results
                if os.path.isfile(path)
                and os.path.splitext(path)[1].lower() == ".mkv"
                and os.path.realpath(os.path.dirname(path)) == os.path.realpath(folder)
            ), None)
            if media_path:
                self._mux_subtitles_into_mkv(media_path, results)
                delivered_results = [media_path]
        if error:
            retry_languages = ",".join(missing) or subtitle_lang
            request = (
                url, folder,
                "subtitles_retry" if retry or results else "subtitles", "",
                retry_languages, None, clip)
            if results:
                self._emit_event((
                    "subtitle_retry_failed", error, request,
                    delivered_results, completed, missing))
            elif needs_verification(error) and not isinstance(session, VerifiedCredentials):
                self._emit_event(("verification_required", None, error, request))
            elif retry:
                self._emit_event((
                    "subtitle_retry_failed", error, request,
                    results, completed, missing))
            else:
                self._emit_event(("download_error", error))
            return
        self._emit_event((
            "subtitle_retry_done" if retry else "download_done",
            delivered_results, folder))


    @staticmethod
    def _available_output_path(source, folder):
        name = os.path.basename(source)
        stem, extension = os.path.splitext(name)
        candidate = os.path.join(folder, name)
        index = 1
        while os.path.exists(candidate):
            candidate = os.path.join(folder, f"{stem} ({index}){extension}")
            index += 1
        return candidate


    @staticmethod
    def _safe_media_name(title):
        value = re.sub(r'[<>:"/\\|?*\x00-\x1f]', '_', str(title or "Vidéo")).strip(' .')
        return value[:180].rstrip(' .') or "Vidéo"


    @staticmethod
    def _cookie_file_from_options(options):
        try:
            index = options.index("--cookies")
            return options[index + 1]
        except (ValueError, IndexError):
            return None


    def _native_clip_info(self, url, fmt, options):
        """Resolve signed streams through yt-dlp without starting a media download."""
        template = CLIP_INFO_PREFIX + (
            '{"id":%(id|null)j,"title":%(title|null)j,"duration":%(duration|null)j,'
            '"chapters":%(chapters|null)j,'
            '"sponsorblock":%(sponsorblock_chapters|null)j,'
            '"requested":%(requested_formats|null)j,'
            '"single":{"url":%(url|null)j,"ext":%(ext|null)j,'
            '"protocol":%(protocol|null)j,"filesize":%(filesize|null)j,'
            '"filesize_approx":%(filesize_approx|null)j,'
            '"vcodec":%(vcodec|null)j,"acodec":%(acodec|null)j,'
            '"language":%(language|null)j,'
            '"http_headers":%(http_headers|null)j}}')
        command = [
            self.ytdlp, "--newline", "--no-playlist", "--skip-download",
            "--sponsorblock-mark", "all", "-f", fmt,
            "--print", "before_dl:" + template, *options, url,
        ]
        process = start_process(
            command, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
            text=True, encoding="utf-8", errors="replace", bufsize=1,
            creationflags=CREATE_NO_WINDOW)
        self.download_process = process
        lines = []
        info = None
        assert process.stdout is not None
        line_queue = queue.Queue()
        reader_finished = object()

        def read_output():
            try:
                for raw_line in process.stdout:
                    line_queue.put(raw_line)
            except (OSError, ValueError):
                pass
            finally:
                line_queue.put(reader_finished)

        reader = threading.Thread(target=read_output, daemon=True)
        reader.start()
        last_output = time.monotonic()
        stalled = False
        while True:
            try:
                raw = line_queue.get(timeout=0.25)
            except queue.Empty:
                if self.cancel_requested:
                    terminate_process_tree(process)
                    break
                if process.poll() is not None and not reader.is_alive():
                    break
                if time.monotonic() - last_output >= CLIP_STALL_SECONDS:
                    stalled = True
                    terminate_process_tree(process)
                    break
                continue
            if raw is reader_finished:
                break
            last_output = time.monotonic()
            line = clean_ansi(raw.strip())
            if line:
                lines.append(line)
                if len(lines) > 40:
                    lines.pop(0)
            if line.startswith(CLIP_INFO_PREFIX):
                try:
                    info = json.loads(line[len(CLIP_INFO_PREFIX):])
                except (TypeError, ValueError):
                    info = None
            elif line:
                self._emit_event(("download_status", line))
            if self.cancel_requested:
                terminate_process_tree(process)
                break
        return_code = process.wait()
        process_result(process, phase='native_clip_metadata',
                       output='\n'.join(line for line in lines if not line.startswith(CLIP_INFO_PREFIX)), timeout=stalled)
        reader.join(timeout=2)
        process.stdout.close()
        self.download_process = None
        if self.cancel_requested:
            raise InterruptedError("Téléchargement annulé.")
        if return_code != 0 or not isinstance(info, dict):
            details = "\n".join(lines[-12:]).strip()
            if stalled:
                details = (
                    f"Aucune réponse lors de la préparation pendant "
                    f"{CLIP_STALL_SECONDS:g} secondes.\n" + details)
            raise NativeClipUnsupported(
                details or "yt-dlp n’a pas fourni les flux signés de l’extrait.")
        streams = info.get("requested")
        if not isinstance(streams, list) or not streams:
            streams = [info.get("single")]
        streams = [item for item in streams if isinstance(item, dict)]
        if not streams:
            raise NativeClipUnsupported("Aucun flux direct n’a été sélectionné.")
        info["streams"] = streams
        return info


    def _run_cancellable_media_command(self, command):
        process = start_process(
            command, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
            text=True, encoding="utf-8", errors="replace",
            creationflags=CREATE_NO_WINDOW)
        self.download_process = process
        output = ''
        try:
            output, _ = process.communicate()
        finally:
            process_result(process, phase='ffmpeg', output=output)
            if self.download_process is process:
                self.download_process = None
        if self.cancel_requested:
            raise InterruptedError("Téléchargement annulé.")
        return process.returncode, output or ""


    def _embed_clip_chapters(self, path, chapters, clip_start):
        if not chapters:
            return path
        metadata = path + ".chapters.txt"
        temporary = path + ".chapters" + os.path.splitext(path)[1]
        lines = [";FFMETADATA1"]
        for chapter in chapters:
            start = max(0, round((chapter["start_time"] - clip_start) * 1000))
            end = max(start + 1, round((chapter["end_time"] - clip_start) * 1000))
            lines += [
                "[CHAPTER]", "TIMEBASE=1/1000", f"START={start}", f"END={end}",
                "title=" + self._ffmetadata_escape(chapter["title"]),
            ]
        try:
            with open(metadata, "w", encoding="utf-8", newline="\n") as target:
                target.write("\n".join(lines) + "\n")
            command = [
                self.ffmpeg, "-y", "-hide_banner", "-loglevel", "warning",
                "-i", path, "-f", "ffmetadata", "-i", metadata,
                "-map", "0", "-map_metadata", "0", "-map_chapters", "1",
                "-c", "copy", temporary,
            ]
            return_code, output = self._run_cancellable_media_command(command)
            if return_code != 0 or not os.path.isfile(temporary):
                raise RuntimeError((output or "Impossible d’intégrer les chapitres.").strip())
            os.replace(temporary, path)
            return path
        finally:
            for item in (metadata, temporary):
                try:
                    os.remove(item)
                except FileNotFoundError:
                    pass


    def _mux_subtitles_into_mkv(self, media_path, subtitle_paths):
        """Add corrected SRT files as selectable MKV tracks, without re-encoding."""
        subtitle_paths = [
            path for path in subtitle_paths
            if os.path.isfile(path) and path.lower().endswith(".srt")]
        if not subtitle_paths:
            return media_path
        if os.path.splitext(media_path)[1].lower() != ".mkv":
            raise RuntimeError(
                "Les sous-titres intégrés nécessitent un fichier final MKV.")

        ffprobe = ffprobe_for(self.ffmpeg)
        if not ffprobe:
            raise RuntimeError(
                "FFprobe est nécessaire pour identifier les pistes de sous-titres "
                "déjà présentes dans le MKV.")
        existing_subtitle_count = sum(
            stream.get("codec_type") == "subtitle"
            for stream in self._read_media_streams(ffprobe, media_path))

        temporary = media_path + ".subtitles.tmp.mkv"
        command = [
            self.ffmpeg, "-y", "-hide_banner", "-loglevel", "warning",
            "-i", media_path,
        ]
        for subtitle_path in subtitle_paths:
            command += ["-i", subtitle_path]
        command += ["-map", "0"]
        for input_index in range(1, len(subtitle_paths) + 1):
            command += ["-map", f"{input_index}:0"]
        command += ["-map_metadata", "0", "-map_chapters", "0", "-c", "copy"]
        for added_index, subtitle_path in enumerate(subtitle_paths):
            subtitle_index = existing_subtitle_count + added_index
            language = os.path.splitext(subtitle_path)[0].rsplit(".", 1)[-1]
            base = language_base(language)
            command += [
                f"-metadata:s:s:{subtitle_index}",
                "language=" + MATROSKA_LANGUAGE_CODES.get(base, base or "und"),
                f"-metadata:s:s:{subtitle_index}",
                "title=" + language_label(language),
                f"-disposition:s:{subtitle_index}",
                "default" if subtitle_index == 0 else "0",
            ]
        command += [temporary]
        self._emit_event((
            "subtitle_phase",
            f"Muxage de {len(subtitle_paths)} piste(s) de sous-titres dans le MKV…"))
        try:
            return_code, output = self._run_cancellable_media_command(command)
            if return_code != 0 or not os.path.isfile(temporary):
                raise RuntimeError((
                    output or "Impossible d’intégrer les sous-titres dans le MKV.").strip())
            os.replace(temporary, media_path)
            for subtitle_path in subtitle_paths:
                try:
                    os.remove(subtitle_path)
                except OSError as error:
                    self._emit_event((
                        "subtitle_phase",
                        f"SRT intégré mais non supprimé : {os.path.basename(subtitle_path)} "
                        f"({error})."))
            self._emit_event((
                "subtitle_phase",
                f"{len(subtitle_paths)} piste(s) de sous-titres intégrée(s) au MKV."))
            return media_path
        finally:
            try:
                os.remove(temporary)
            except FileNotFoundError:
                pass


    def _normalize_native_streams(
            self, sources, stream_info, spans, temporary, title, extension, mode):
        destination = os.path.join(temporary, self._safe_media_name(title) + "." + extension)
        common_start = min(span[0] for span in spans)
        command = [self.ffmpeg, "-y", "-hide_banner", "-loglevel", "warning"]
        for source, span in zip(sources, spans):
            offset = max(0.0, span[0] - common_start)
            if offset > 0.0005:
                command += ["-itsoffset", f"{offset:.6f}"]
            command += ["-i", source]
        if mode == "audio":
            command += ["-map", "0:a:0"]
        elif mode == "video_only":
            command += ["-map", "0:v:0"]
        elif len(sources) == 1:
            command += ["-map", "0:v:0"]
            # Progressive streams already contain audio.
            command += ["-map", "0:a:0?"]
        else:
            video_index = next((index for index, item in enumerate(stream_info)
                                if item.get("vcodec") not in (None, "none")), 0)
            audio_indices = [
                index for index, item in enumerate(stream_info)
                if item.get("acodec") not in (None, "none")]
            command += ["-map", f"{video_index}:v:0"]
            for audio_index in audio_indices:
                command += ["-map", f"{audio_index}:a:0"]
        command += ["-c", "copy", "-avoid_negative_ts", "make_zero", destination]
        return_code, output = self._run_cancellable_media_command(command)
        if return_code != 0 or not os.path.isfile(destination):
            raise NativeClipUnsupported((
                output or "FFmpeg n’a pas pu assembler les fragments locaux.").strip())
        return destination, common_start


    def _finalize_audio_clip(self, source, folder, title, extension, start, duration):
        stub = os.path.join(folder, self._safe_media_name(title) + "." + extension)
        destination = self._available_output_path(stub, folder)
        command = [
            self.ffmpeg, "-y", "-hide_banner", "-loglevel", "warning",
            "-ss", f"{max(0.0, start):.6f}", "-i", source,
            "-map", "0:a:0", "-c", "copy", "-avoid_negative_ts", "make_zero",
        ]
        if duration is not None:
            command += ["-t", f"{duration:.6f}"]
        command += [destination]
        return_code, output = self._run_cancellable_media_command(command)
        if return_code != 0 or not os.path.isfile(destination):
            raise NativeClipUnsupported((
                output or "FFmpeg n’a pas pu finaliser l’extrait audio.").strip())
        return destination


    def _download_native_clip(
            self, url, folder, mode, fmt, merge_format, clip, options,
            subtitle_completion=None, emit_done=True):
        """Native indexed strategy: yt-dlp resolves access, Python reads ranges."""
        self._emit_event((
            "download_phase",
            "Extrait natif · préparation des flux YouTube comme un téléchargement normal…"))
        info = self._native_clip_info(url, fmt, options)
        self._emit_event((
            "download_phase",
            f"Accès aux médias résolu : {len(info['streams'])} flux direct(s) sélectionné(s)."))
        temporary = tempfile.mkdtemp(prefix=".youtube-native-clip-", dir=folder)
        try:
            streams = list(info["streams"])
            cookie_file = self._cookie_file_from_options(options)
            sources = []
            spans = []
            stream_count = len(streams)
            for index, stream in enumerate(streams):
                extension = str(stream.get("ext") or "bin").lower()
                source = os.path.join(temporary, f"stream-{index}.{extension}")
                media_kind = (
                    "vidéo" if stream.get("vcodec") not in (None, "none")
                    else "audio")
                codec = (
                    stream.get("vcodec") if media_kind == "vidéo"
                    else stream.get("acodec"))
                language = stream.get("language")
                size = stream.get("filesize") or stream.get("filesize_approx")
                self._emit_event((
                    "download_phase",
                    f"Flux {index + 1}/{stream_count} : {media_kind} {codec or 'codec inconnu'}"
                    + (f", langue {language}" if language else "")
                    + f", conteneur {extension}"
                    + (f", source {human_size(size)}" if size else "") + "."))

                def progress(received, expected, stream_index=index):
                    fraction = received / expected if expected else 0
                    percent = min(84.0, 5 + 79 * (stream_index + fraction) / stream_count)
                    self._emit_event((
                        "download_progress", percent,
                        f"{human_size(received)} / {human_size(expected)}"))

                try:
                    span_start, span_end, received_size = self._download_indexed_stream(
                        stream, source, clip[0], clip[1], cookie_file=cookie_file,
                        progress=progress, cancelled=lambda: self.cancel_requested,
                        rate_limit=lambda: getattr(self, "download_rate_bps", 0),
                        reader_ready=lambda reader: setattr(
                            self, "native_reader", reader))
                except NativeClipUnsupported:
                    audio_only = (
                        stream.get("vcodec") in (None, "none")
                        and stream.get("acodec") not in (None, "none"))
                    if not audio_only or extension != "webm":
                        raise
                    # YouTube's standalone Opus/WebM streams frequently omit
                    # Cues, so their byte-to-time map cannot be reconstructed.
                    # Resolve the matching direct M4A stream and slice its SIDX
                    # instead. This remains a stream copy, never a re-encode.
                    language = language_base(stream.get("language"))
                    audio_selector = "bestaudio[protocol=https][ext=m4a]"
                    if language:
                        audio_selector += f"[language^={language}]"
                    self._emit_event((
                        "download_phase",
                        "Extrait natif · piste WebM non indexée, utilisation de "
                        "la piste audio M4A directe…"))
                    audio_info = self._native_clip_info(
                        url, audio_selector, options)
                    replacement = next((
                        item for item in audio_info["streams"]
                        if item.get("vcodec") in (None, "none")
                        and item.get("acodec") not in (None, "none")
                    ), None)
                    if replacement is None:
                        raise NativeClipUnsupported(
                            "Aucune piste audio M4A indexée équivalente n’est disponible.")
                    stream = replacement
                    streams[index] = stream
                    extension = str(stream.get("ext") or "m4a").lower()
                    source = os.path.join(temporary, f"stream-{index}.{extension}")
                    span_start, span_end, received_size = self._download_indexed_stream(
                        stream, source, clip[0], clip[1], cookie_file=cookie_file,
                        progress=progress, cancelled=lambda: self.cancel_requested,
                        rate_limit=lambda: getattr(self, "download_rate_bps", 0),
                        reader_ready=lambda reader: setattr(
                            self, "native_reader", reader))
                sources.append(source)
                spans.append((span_start, span_end))
                self._emit_event((
                    "download_phase",
                    f"Flux {index + 1}/{stream_count} reçu : {human_size(received_size)}, "
                    f"plage média {format_timecode(span_start)} → {format_timecode(span_end)}."))
            if self.cancel_requested:
                raise InterruptedError("Téléchargement annulé.")

            if merge_format:
                extension = merge_format
            elif mode == "audio":
                extension = str(streams[0].get("ext") or "m4a")
            elif len(streams) == 1:
                extension = str(streams[0].get("ext") or "mkv")
            else:
                extension = "mkv"
            self._emit_event((
                "download_phase",
                f"Assemblage local de {len(sources)} flux par FFmpeg, copie directe des pistes."))
            normalized, common_start = self._normalize_native_streams(
                sources, streams, spans, temporary, info.get("title"), extension, mode)
            duration = clip[1] - clip[0] if clip[1] is not None else None
            relative_start = max(0.0, clip[0] - common_start)
            self._emit_event((
                "download_phase", "Extrait natif · finalisation locale sans réencodage…"))
            if mode == "audio":
                result = self._finalize_audio_clip(
                    normalized, folder, info.get("title"), extension,
                    relative_start, duration)
            else:
                result = self._finalize_video_clip(
                    normalized, folder, duration, source_start=relative_start,
                    timeline_offset=common_start)
            chapters = self._arranged_clip_chapters(
                info.get("chapters"), info.get("sponsorblock"), clip[0], clip[1])
            self._emit_event((
                "download_phase",
                f"Métadonnées : {len(chapters)} chapitre(s), incluant les repères SponsorBlock disponibles."))
            result = self._embed_clip_chapters(result, chapters, clip[0])
            self._emit_event((
                "download_phase",
                f"Fichier final créé : {os.path.basename(result)}."))

            result_paths = [result]
            if subtitle_completion and not self.cancel_requested:
                subtitle_paths = subtitle_completion()
                if subtitle_paths:
                    self._mux_subtitles_into_mkv(result, subtitle_paths)
            if self.cancel_requested:
                raise InterruptedError("Téléchargement annulé.")
            if emit_done:
                self._emit_event(("download_done", result_paths, folder))
            return result_paths
        finally:
            self.native_reader = None
            shutil.rmtree(temporary, ignore_errors=True)


    def _finalize_video_clip(
            self, source, folder, duration, source_start=0.0, timeline_offset=0.0):
        ffprobe = ffprobe_for(self.ffmpeg)
        if not ffprobe:
            raise RuntimeError("FFprobe est nécessaire pour corriger précisément l’extrait vidéo.")

        streams = self._read_media_streams(ffprobe, source)
        video = next((item for item in streams if item.get("codec_type") == "video"), None)
        audio = next((item for item in streams if item.get("codec_type") == "audio"), None)
        if not video:
            raise RuntimeError("Aucune piste vidéo trouvée dans l’extrait téléchargé.")

        target_start = max(0.0, float(source_start or 0))
        target_end = target_start + float(duration) if duration is not None else None
        keyframes = _video_keyframe_times(
            ffprobe, source, start=target_start, end=target_end, timeout=30)
        previous = [value for value in keyframes if value <= target_start]
        start_keyframe = previous[-1] if previous else keyframes[0]
        end_candidates = [value for value in keyframes if value > start_keyframe]
        if target_end is not None and end_candidates:
            following = [value for value in end_candidates if value >= target_end]
            end_keyframe = following[0] if following else end_candidates[-1]
            copy_duration = end_keyframe - start_keyframe
        elif duration is not None:
            copy_duration = float(duration)
            end_keyframe = start_keyframe + copy_duration
        else:
            copy_duration = None
            end_keyframe = None

        snapped_start = timeline_offset + start_keyframe
        snapped_end = timeline_offset + end_keyframe if end_keyframe is not None else None
        self._emit_event(("clip_snapped", snapped_start, snapped_end))

        destination = self._available_output_path(source, folder)
        start = f"{start_keyframe:.6f}"
        length = f"{copy_duration:.6f}" if copy_duration is not None else None
        command = [
            self.ffmpeg, "-y", "-hide_banner", "-loglevel", "warning",
            "-ss", start, "-i", source, "-map", "0:v:0",
        ]
        if audio:
            command += ["-map", "0:a:0"]
        command += ["-map_metadata", "0", "-map_chapters", "0"]
        command += ["-c", "copy", "-avoid_negative_ts", "make_zero"]
        if length:
            command += ["-t", length]
        command += [
            "-max_muxing_queue_size", "4096", "-progress", "pipe:1", "-nostats",
            destination,
        ]

        self._emit_event((
            "download_phase",
            "Coupe sans réencodage · copie directe entre les images clés…"))
        output_lines = []
        process = start_process(
            command,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            encoding="utf-8",
            errors="replace",
            bufsize=1,
            creationflags=CREATE_NO_WINDOW,
        )
        self.download_process = process
        assert process.stdout is not None
        for raw_line in process.stdout:
            line = clean_ansi(raw_line.strip())
            if line:
                output_lines.append(line)
                if len(output_lines) > 20:
                    output_lines.pop(0)
            if length and line.startswith(("out_time_us=", "out_time_ms=")):
                try:
                    elapsed = int(line.partition("=")[2]) / 1_000_000
                    percent = min(99.0, 100 * elapsed / duration)
                    self._emit_event((
                        "download_progress", percent,
                        f"Coupe sans réencodage · {percent:.0f} %",
                    ))
                except (TypeError, ValueError, ZeroDivisionError):
                    pass
            if self.cancel_requested:
                terminate_process_tree(process)
                break
        return_code = process.wait()
        process_result(process, phase='ffmpeg_clip', output='\n'.join(output_lines))
        process.stdout.close()
        self.download_process = None
        if self.cancel_requested:
            try:
                os.remove(destination)
            except OSError:
                pass
            return None
        if return_code != 0 or not os.path.isfile(destination):
            try:
                os.remove(destination)
            except OSError:
                pass
            details = "\n".join(output_lines[-10:]).strip()
            raise RuntimeError(details or f"FFmpeg a quitté avec le code {return_code}")
        return destination


    def _download_with_access(
            self, url, folder, mode, fmt, subtitle_lang, merge_format, clip, options,
            alternate_client=False, subtitle_completion=None, resume_source=None):
        # A rate change restarts the same request iteratively. Repeated changes
        # must not accumulate recursive calls or let an old attempt close a new one.
        network_retries = 0
        while True:
            if self.cancel_requested:
                self._emit_event(("download_cancelled",))
                return
            result = self._download_with_access_attempt(
                url, folder, mode, fmt, subtitle_lang, merge_format, clip, options,
                alternate_client=alternate_client,
                subtitle_completion=subtitle_completion, resume_source=resume_source,
                retry_network=network_retries < len(DOWNLOAD_RETRY_DELAYS))
            if isinstance(result, _DownloadRetry):
                if result.options is not None:
                    options = list(result.options)
                if result.kind == "route":
                    alternate_client = True
                if result.kind == "network":
                    delay = DOWNLOAD_RETRY_DELAYS[network_retries]
                    network_retries += 1
                    message = (
                        f"Connexion interrompue · reprise automatique {network_retries}/"
                        f"{len(DOWNLOAD_RETRY_DELAYS)} dans {delay} s · {result.reason}")
                else:
                    delay = 0
                    message = result.reason
                self._emit_event(("download_phase", message))
                wait_until = time.monotonic() + delay
                while time.monotonic() < wait_until and not self.cancel_requested:
                    time.sleep(min(0.1, max(0, wait_until - time.monotonic())))
                continue
            if result is not _RATE_RESTART:
                return result
            if self.cancel_requested:
                self._emit_event(("download_cancelled",))
                return

    def _download_with_access_attempt(
            self, url, folder, mode, fmt, subtitle_lang, merge_format, clip, options,
            alternate_client=False, subtitle_completion=None, resume_source=None,
            retry_network=False):
        if clip and mode != "subtitles" and is_youtube_url(url):
            native_attempts = [list(options)]
            alternate_options = alternate_youtube_options(options)
            if alternate_options != list(options):
                native_attempts.append(alternate_options)
            native_errors = []
            for attempt_index, attempt_options in enumerate(native_attempts, start=1):
                if attempt_index > 1:
                    self._emit_event((
                        "download_phase",
                        "Extrait natif · nouvel essai par une autre route YouTube…"))
                try:
                    if mode == "audio" and "," in fmt:
                        combined_paths = []
                        selectors = [
                            item.strip() for item in fmt.split(",")
                            if item.strip()]
                        self._emit_event((
                            "download_phase",
                            f"Mode audio multilingue : {len(selectors)} fichier(s) "
                            "seront créés séparément."))
                        for selector_index, selector in enumerate(selectors, start=1):
                            self._emit_event((
                                "download_phase",
                                f"Piste audio {selector_index}/{len(selectors)} : "
                                "résolution et découpe du flux."))
                            paths = self._download_native_clip(
                                url, folder, mode, selector, merge_format, clip,
                                attempt_options, subtitle_completion=None,
                                emit_done=False)
                            combined_paths.extend(paths)
                        self._emit_event((
                            "download_done", combined_paths, folder))
                        return combined_paths
                    return self._download_native_clip(
                        url, folder, mode, fmt, merge_format, clip,
                        attempt_options, subtitle_completion=subtitle_completion)
                except InterruptedError:
                    self._emit_event(("download_cancelled",))
                    return
                except NativeClipUnsupported as error:
                    native_errors.append(str(error).strip())
            details = "\n\n".join(
                f"Route {index + 1} : {error}"
                for index, error in enumerate(native_errors) if error)
            details = (
                "La coupe native a échoué. L’ancienne coupe FFmpeg n’a pas été "
                "relancée, puisqu’elle reproduirait le refus déjà constaté.\n\n"
                + (details or "YouTube n’a fourni aucun flux indexé exploitable."))
            self._emit_event(("download_status", details))
            request = (url, folder, mode, fmt, subtitle_lang, merge_format, clip)
            if needs_verification(details):
                self._emit_event((
                    "verification_required", None, details, request))
            else:
                self._emit_event(("download_error", details))
            return

        precise_video_clip = bool(clip and mode in ("video", "video_only"))
        clip_temp = tempfile.mkdtemp(prefix=".youtube-download-helper-", dir=folder) if precise_video_clip else None
        output_folder = clip_temp or folder
        multi_audio_files = mode == "audio" and "," in fmt
        output_name = (
            "%(title)s [%(format_id)s].%(ext)s"
            if multi_audio_files else "%(title)s.%(ext)s")
        output = (resume_source[0].replace('%', '%%') if resume_source
                  else os.path.join(output_folder, output_name))
        download_started = time.time()

        cmd = [
            self.ytdlp,
            "--newline",
            "--no-playlist",
            "--continue",
            "--part",
            "--no-force-overwrites",
            "--abort-on-unavailable-fragments",
            "--progress",
            "--progress-template",
            PROGRESS_TEMPLATE,
            "--print",
            RESULT_TEMPLATE,
            "--progress-delta", "0.2",
            "-f",
            fmt,
            "-o",
            output,
        ]
        started_rate_revision = getattr(self, "download_rate_revision", 0)
        rate_limit = getattr(self, "download_rate_bps", 0)
        if rate_limit:
            cmd += ["--limit-rate", str(rate_limit)]
            if rate_limit < 64_000:
                # Keep progress flowing even at 1 ko/s: a growing yt-dlp
                # buffer could otherwise outlast the idle watchdog.
                cmd += ["--buffer-size", "16K", "--no-resize-buffer"]

        # Preserve the chapters published by the creator and add SponsorBlock
        # segments as chapter markers.  This changes metadata only: no segment
        # is removed and the media is never split into separate chapter files.
        if mode != "subtitles" and not resume_source:
            cmd += ["--embed-chapters", "--sponsorblock-mark", "all"]
            if mode == "video" and fmt.count("+") >= 2:
                cmd += ["--audio-multistreams"]

        if mode == "subtitles":
            cmd = [self.ytdlp, "--newline", "--no-playlist", "--skip-download",
                   "--write-subs", "--write-auto-subs", "--sub-langs", subtitle_lang,
                   "--sub-format", "srt/vtt/best", "--convert-subs", "srt",
                   "--print", RESULT_TEMPLATE, "-o", output]
        cmd += options
        if clip and mode != "subtitles":
            start, end = clip
            section = f"*{format_timecode(start)}-{format_timecode(end) if end is not None else 'inf'}"
            cmd += ["--download-sections", section]
            # yt-dlp delegates time ranges to FFmpeg. Bound a stalled CDN read;
            # otherwise FFmpeg can wait forever after a YouTube 429 response.
            # The portable FFmpeg build cannot consult the Windows certificate
            # store.  These are short-lived, signed media URLs already obtained
            # and validated by yt-dlp; disable FFmpeg's second TLS check for the
            # direct range request only.
            ffmpeg_input_args = "-rw_timeout 15000000"
            if is_youtube_url(url):
                ffmpeg_input_args = "-tls_verify 0 " + ffmpeg_input_args
            cmd += ["--downloader-args", "ffmpeg_i:" + ffmpeg_input_args]
        # Remux also applies to a single stream; merging alone would ignore that case.
        if mode != "subtitles":
            cmd += container_args(mode, merge_format)
        if self.ffmpeg:
            cmd += ["--ffmpeg-location", self.ffmpeg]

        if resume_source:
            cmd += ['--load-info-json', resume_source[1]]
        else:
            cmd.append(url)

        try:
            output_lines = []
            result_paths = []
            data = getattr(self, "active_download_data", None) or getattr(self, "analysis_data", None)
            progress = DownloadProgress(
                fmt, mode=mode, labels=self._download_progress_labels(), data=data)

            # Before the first progress event we are still connecting. A rate
            # change must take effect then too, but never interrupt a remux.
            postprocessing = False
            media_processing = False
            rate_changed = False

            proc = start_process(
                cmd,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
                encoding="utf-8",
                errors="replace",
                bufsize=1,
                creationflags=CREATE_NO_WINDOW,
            )
            self.download_process = proc

            assert proc.stdout is not None
            line_queue = queue.Queue()
            reader_finished = object()

            def read_output():
                nonlocal postprocessing, media_processing
                try:
                    for item in proc.stdout:
                        # Observe the live subprocess phase here. The UI event
                        # queue may still contain older download progress while
                        # FFmpeg has already begun merging the completed streams.
                        line = clean_ansi(item.strip())
                        if line.startswith(PROGRESS_PREFIX):
                            try:
                                status = json.loads(line[len(PROGRESS_PREFIX):]).get('status')
                                if status == 'downloading':
                                    postprocessing = False
                                    media_processing = False
                                elif status == 'finished':
                                    postprocessing = True
                            except (ValueError, TypeError, AttributeError):
                                pass
                        elif line.startswith(('[Merger]', '[VideoRemuxer]', '[VideoConvertor]',
                                              '[Metadata]', '[EmbedSubtitle]', '[Fixup',
                                              '[ExtractAudio]', RESULT_PREFIX)):
                            postprocessing = True
                            media_processing = True
                        line_queue.put(item)
                except (OSError, ValueError):
                    pass
                finally:
                    line_queue.put(reader_finished)

            reader = threading.Thread(target=read_output, daemon=True)
            reader.start()
            last_output = time.monotonic()
            last_wait_second = -1
            stalled = False
            stall_limit = (
                CLIP_STALL_SECONDS if precise_video_clip
                else self.download_stall_seconds)

            while True:
                if (not postprocessing and not self.cancel_requested
                        and mode != "subtitles"
                        and getattr(self, "download_rate_revision", 0) != started_rate_revision
                        and proc.poll() is None):
                    rate_changed = True
                    terminate_process_tree(proc)
                    break
                try:
                    raw_line = line_queue.get(timeout=0.25)
                except queue.Empty:
                    if self.cancel_requested:
                        break
                    if proc.poll() is not None and not reader.is_alive():
                        break
                    # Merging/remuxing large files can be silent for minutes.
                    # The network watchdog must never kill that local work.
                    if media_processing:
                        continue
                    waiting = time.monotonic() - last_output
                    wait_second = int(waiting)
                    if (
                            wait_second >= 5
                            and wait_second % 5 == 0
                            and wait_second != last_wait_second):
                        last_wait_second = wait_second
                        if "--cookies" in options:
                            wait_text = (
                                "Session Google vérifiée · "
                                + (f"route {2 if alternate_client else 1}/2 · "
                                   if precise_video_clip else "")
                                + f"aucune donnée depuis {wait_second} s · "
                                + f"arrêt automatique à {stall_limit:g} s")
                        else:
                            wait_text = (
                                ("Coupe directe · " if clip else "Téléchargement · ")
                                + (f"route {2 if alternate_client else 1}/2 · "
                                   if precise_video_clip else "")
                                + f"aucune donnée depuis {wait_second} s · "
                                + f"arrêt automatique à {stall_limit:g} s")
                        self._emit_event((
                            "download_wait", wait_second, stall_limit, wait_text))
                    if waiting >= stall_limit:
                        stalled = True
                        terminate_process_tree(proc)
                        break
                    continue
                if raw_line is reader_finished:
                    break
                if last_wait_second >= 5:
                    self._emit_event((
                        "download_phase",
                        f"Réception reprise après {last_wait_second} s sans donnée."))
                last_wait_second = -1
                last_output = time.monotonic()
                line = clean_ansi(raw_line.strip())

                if line:
                    output_lines.append(line)
                    if len(output_lines) > 40:
                        output_lines.pop(0)

                if line.startswith(RESULT_PREFIX):
                    try:
                        result_path = json.loads(line[len(RESULT_PREFIX):])
                        if isinstance(result_path,str) and result_path not in result_paths:
                            result_paths.append(result_path)
                    except (ValueError,TypeError):
                        pass
                    continue
                update = progress.parse(line)
                if update is not None:
                    eta, speed = progress.eta_seconds, progress.speed_bps
                    metrics = (eta, speed) if speed else (eta,) if eta is not None else ()
                    self._emit_event(("download_progress", *update, *metrics))
                    if progress.detail_line:
                        self._emit_event((
                            "download_phase", progress.detail_line))
                elif line:
                    self._emit_event(("download_status", line))

                if self.cancel_requested:
                    break

            if self.cancel_requested:
                terminate_process_tree(proc)
                try: proc.wait(timeout=8)
                except Exception: pass
                reader.join(timeout=2)
                self._emit_event(("download_cancelled",))
                return

            if rate_changed:
                try: proc.wait(timeout=8)
                except Exception: pass
                reader.join(timeout=2)
                proc.stdout.close()
                self.download_process = None
                self._emit_event((
                    "download_phase",
                    "Nouvelle limite de débit · reprise du fichier partiel…"))
                return _RATE_RESTART

            if stalled:
                try: proc.wait(timeout=8)
                except Exception: pass
                reader.join(timeout=2)
                proc.stdout.close()
                self.download_process = None
                if precise_video_clip and not alternate_client:
                    if clip_temp:
                        shutil.rmtree(clip_temp, ignore_errors=True)
                        clip_temp = None
                    return _DownloadRetry(
                        "Coupe directe · première route sans réponse · "
                        "nouvel essai automatique par une autre route YouTube…",
                        kind="route", options=tuple(alternate_youtube_options(options)))
                if not clip and retry_network:
                    return _DownloadRetry(f"aucune donnée depuis {stall_limit:g} s")
                error = (
                    f"YouTube n’a transmis aucune nouvelle donnée pendant "
                    f"{stall_limit:g} secondes. Le téléchargement bloqué "
                    "a été arrêté automatiquement. "
                    + ("La vidéo complète n’a pas été téléchargée.\n\n"
                       if precise_video_clip else "\n\n")
                    + "L’accès automatique n’a pas suffi ; une vérification Google "
                    "peut permettre de reprendre exactement la même opération.")
                if precise_video_clip:
                    self._emit_event((
                        "verification_required", None, error,
                        (url, folder, mode, fmt, subtitle_lang, merge_format, clip),
                    ))
                else:
                    self._emit_event(("download_error",
                        f"Aucune donnée reçue pendant {stall_limit:g} secondes malgré les reprises "
                        "automatiques. Le fichier partiel est conservé ; cliquez sur Reprendre."))
                return

            rc = proc.wait()
            process_result(proc, phase='download', output='\n'.join(output_lines))
            reader.join(timeout=2)
            proc.stdout.close()
            if rc == 0:
                if precise_video_clip:
                    if not result_paths:
                        candidates = []
                        for name in os.listdir(output_folder):
                            path = os.path.join(output_folder, name)
                            if os.path.isfile(path) and not name.endswith((".part", ".ytdl")):
                                candidates.append((os.path.getmtime(path), path))
                        candidates.sort(reverse=True)
                        result_paths = [path for _, path in candidates[:1]]
                    if not result_paths:
                        raise RuntimeError("yt-dlp n’a pas indiqué le fichier d’extrait à finaliser.")
                    duration = clip[1] - clip[0] if clip[1] is not None else None
                    corrected = []
                    for source in result_paths:
                        path = self._finalize_video_clip(
                            source, folder, duration,
                            source_start=0.0,
                            timeline_offset=clip[0],
                        )
                        if path:
                            corrected.append(path)
                    if self.cancel_requested:
                        self._emit_event(("download_cancelled",))
                        return
                    result_paths = corrected

                if subtitle_completion and not self.cancel_requested:
                    self._emit_event((
                        "subtitle_phase",
                        "Vidéo terminée · finalisation des sous-titres en parallèle…"))
                    subtitle_paths = subtitle_completion()
                    if subtitle_paths:
                        media_path = next((
                            path for path in result_paths
                            if os.path.isfile(path)
                            and os.path.splitext(path)[1].lower() == ".mkv"
                        ), None)
                        if media_path is None:
                            raise RuntimeError(
                                "Le MKV final est introuvable pour intégrer les sous-titres.")
                        self._mux_subtitles_into_mkv(media_path, subtitle_paths)
                    if self.cancel_requested:
                        self._emit_event(("download_cancelled",))
                        return

                if not result_paths:
                    extensions = {'.srt'} if mode == 'subtitles' else None
                    candidates = []
                    try:
                        for name in os.listdir(folder):
                            path = os.path.join(folder,name)
                            if not os.path.isfile(path) or name.endswith(('.part','.ytdl')):
                                continue
                            if extensions and os.path.splitext(name)[1].lower() not in extensions:
                                continue
                            modified = os.path.getmtime(path)
                            if modified >= download_started - 1:
                                candidates.append((modified,path))
                    except OSError:
                        pass
                    candidates.sort(reverse=True)
                    result_paths = [path for _, path in (candidates if mode == "subtitles" else candidates[:1])]
                self._emit_event(("download_done",result_paths,folder))
            else:
                raw_details = "\n".join(output_lines[-20:]).strip()
                if (not resume_source and "--plugin-dirs" in options
                        and pot_provider_failed(raw_details)):
                    return _DownloadRetry(
                        "Accès automatique indisponible · reprise sans BgUtils…",
                        kind="provider", options=tuple(without_pot_provider(options)))
                if retry_network and not clip and transient_network_error(raw_details):
                    return _DownloadRetry(diagnostic_log(raw_details)[-500:])
                missing_format = not resume_source and format_access_unavailable(raw_details)
                stream_forbidden = (
                    not resume_source and mode != "subtitles" and is_youtube_url(url)
                    and any("http error 403" in line.lower()
                            for line in output_lines
                            if line.startswith(("ERROR:", "[download]")))
                )
                if (missing_format or stream_forbidden) and not alternate_client:
                    alternate_options = alternate_youtube_options(options)
                    if alternate_options != list(options):
                        self.download_process = None
                        reason = ("Flux refusé par YouTube (HTTP 403)" if stream_forbidden
                                  else "Format momentanément retiré par YouTube")
                        return _DownloadRetry(
                            f"{reason} · essai automatique par une autre route…",
                            kind="route", options=tuple(alternate_options))
                if missing_format:
                    self.download_process = None
                    unavailable = (
                        "Le format choisi n’est disponible par aucune des deux routes YouTube testées."
                        if alternate_client else "YouTube ne fournit plus le format choisi."
                    )
                    details = (
                        unavailable + " Une vérification Google peut rétablir son accès."
                        + (" La vidéo complète ne sera pas téléchargée." if precise_video_clip else "")
                        + "\n\n" + raw_details)
                    self._emit_event((
                        "verification_required", None, details,
                        (url, folder, mode, fmt, subtitle_lang, merge_format, clip),
                    ))
                    return
                # Return useful yt-dlp diagnostics instead of only the exit code.
                useful = []
                for item in output_lines:
                    low = item.lower()
                    if (
                        "error" in low
                        or "warning" in low
                        or "403" in low
                        or "forbidden" in low
                        or "unable" in low
                        or "ffmpeg" in low
                        or "requested format" in low
                    ):
                        useful.append(item)

                if not useful:
                    useful = output_lines[-12:]

                details = "\n".join(useful[-12:]).strip()
                if not details:
                    details = f"yt-dlp a quitté avec le code {rc}"

                if stream_forbidden and alternate_client:
                    details = ("YouTube refuse toujours l’accès au flux après l’essai "
                               "par une autre route.\n\n" + details)

                if precise_video_clip:
                    details = (
                        "YouTube n’a pas permis de récupérer directement cet extrait. "
                        "La vidéo complète n’a pas été téléchargée.\n\n" + details)

                if needs_verification(details):
                    self._emit_event(("verification_required", None, details, (url, folder, mode, fmt, subtitle_lang, merge_format, clip)))
                else:
                    self._emit_event(("download_error", details))

        except Exception as e:
            log_exception('download.attempt_exception', engine=hex(id(self)))
            if "proc" in locals() and proc.poll() is None:
                terminate_process_tree(proc)
                try:
                    proc.wait(timeout=8)
                except subprocess.TimeoutExpired:
                    pass
            self._emit_event(("download_error", str(e)))
        finally:
            if "proc" in locals() and proc.stdout is not None:
                proc.stdout.close()
            if "proc" in locals() and self.download_process is proc:
                self.download_process = None
            if clip_temp:
                shutil.rmtree(clip_temp, ignore_errors=True)


class WindowsEngine(WindowsEngineMixin):
    """Headless owner of the same media workers used by the Qt application."""

    def __init__(self, ytdlp=None, ffmpeg=None):
        self.ytdlp = ytdlp or find_executable(["yt-dlp.exe", "yt-dlp"])
        self.ffmpeg = ffmpeg or find_executable(["ffmpeg.exe", "ffmpeg"])
        self.events = queue.Queue()
        self.analysis_lock = threading.Lock()
        self.analysis_token = 0
        self.analysis_process = None
        self.keyframe_token = 0
        self.keyframe_process = None
        self.download_process = None
        self.subtitle_process = None
        self.native_reader = None
        self.download_rate_bps = 0
        self.download_rate_revision = 0
        self.cancel_requested = False
        self.current_url = ""
        self.verification_attempted = set()
        self.subtitle_tracks = []
        self.download_results = []
        self.progress_labels = {}
        self.analysis_data = None
        self.access_strategies = DEFAULT_ACCESS.copy()
        self.download_strategies = DEFAULT_DOWNLOADS.copy()

    def _download_progress_labels(self):
        return progress_labels(self.analysis_data)
