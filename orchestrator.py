"""Internal application API shared by Qt and future local transports."""

from dataclasses import dataclass
import os
import queue
import threading
from threading import Thread as WorkerThread
from typing import Any, Iterator

from core_selection import DownloadRequest, MediaCatalog
from windows_engine import WindowsEngine
from youtube_access import is_youtube_url


TERMINAL_EVENTS = {
    "analysis_ok", "analysis_error", "analysis_cancelled", "download_done", "download_error",
    "download_cancelled", "download_formats_changed", "subtitle_retry_done", "subtitle_retry_failed",
    "verification_required",
}


@dataclass(frozen=True)
class OrchestratorEvent:
    kind: str
    values: tuple[Any, ...]

    @classmethod
    def from_legacy(cls, event: tuple) -> "OrchestratorEvent":
        return cls(event[0], tuple(event[1:]))

    def as_dict(self) -> dict[str, Any]:
        return {"kind": self.kind, "values": self.values}


class DownloadOrchestrator:
    """Owns one engine and one event stream; it never imports Qt."""

    def __init__(self, engine=None):
        self.engine = engine or WindowsEngine()
        self.analysis_thread: threading.Thread | None = None
        self.download_thread: threading.Thread | None = None
        self.catalog: MediaCatalog | None = None
        self.last_request: DownloadRequest | None = None
        self.last_event: OrchestratorEvent | None = None

    def analyze(self, url: str, *, session=False, fallback=False,
                token: int | None = None) -> threading.Thread:
        if not is_youtube_url(url):
            raise ValueError("URL YouTube non reconnue")
        if not self.engine.ytdlp:
            raise RuntimeError("yt-dlp est absent")
        if self.analysis_thread is not None and self.analysis_thread.is_alive():
            raise RuntimeError("Une analyse est déjà active")
        if token is None:
            self.engine.analysis_token += 1
            token = self.engine.analysis_token
        else:
            self.engine.analysis_token = token
        self.engine.current_url = url
        worker = threading.Thread(
            target=self.engine._analyze_worker,
            args=(url, token, session, fallback), daemon=True)
        self.analysis_thread = worker
        worker.start()
        return worker

    def download(self, request: DownloadRequest, *, validate=True) -> threading.Thread:
        if validate:
            if not is_youtube_url(request.url):
                raise ValueError("URL YouTube non reconnue")
            if not os.path.isdir(request.folder):
                raise ValueError("Dossier de destination introuvable")
            if request.mode in ("video", "video_only", "audio") and not request.format_id:
                raise ValueError("Aucun format média sélectionné")
            if request.mode in ("subtitles", "subtitles_retry") and not request.subtitle_languages:
                raise ValueError("Aucune langue de sous-titres sélectionnée")
            if request.clip is not None:
                start, end = request.clip
                if start < 0 or (end is not None and end <= start):
                    raise ValueError("Plage d’extrait invalide")
        if request.mode not in ("video", "video_only", "audio", "subtitles", "subtitles_retry"):
            raise ValueError("Mode de téléchargement inconnu")
        if not self.engine.ytdlp:
            raise RuntimeError("yt-dlp est absent")
        if self.download_thread is not None and self.download_thread.is_alive():
            raise RuntimeError("Un téléchargement est déjà actif")
        self.engine.cancel_requested = False
        self.engine.current_url = request.url
        self.last_request = request
        worker = threading.Thread(
            target=self.engine._download_worker,
            args=(request.url, request.folder, request.mode, request.format_id,
                  request.subtitle_languages, request.container, request.clip,
                  request.session),
            kwargs={'partial_path': request.partial_path} if request.partial_path else {},
            daemon=True)
        self.download_thread = worker
        worker.start()
        return worker

    def cancel(self) -> None:
        analysis_active = self.analysis_thread is not None and self.analysis_thread.is_alive()
        download_active = self.download_thread is not None and self.download_thread.is_alive()
        self.engine.cancel()
        if analysis_active and not download_active:
            self.engine.events.put(("analysis_cancelled", self.engine.analysis_token))

    def next_event(self, timeout: float | None = None) -> tuple:
        if timeout is None:
            event = self.engine.events.get_nowait()
        else:
            event = self.engine.events.get(timeout=timeout)
        return self._record_event(event)

    def _record_event(self, event: tuple) -> tuple:
        if (event[0] in ("download_done", "download_error", "download_cancelled", "download_formats_changed",
                         "subtitle_retry_done", "subtitle_retry_failed")
                or (event[0] == "verification_required" and event[1] is None)):
            # Real engines defer these events until cleanup is complete. Join
            # the last few instructions before allowing the next queued job.
            worker = self.download_thread
            if isinstance(worker, WorkerThread) and worker is not threading.current_thread():
                worker.join()
        self.last_event = OrchestratorEvent.from_legacy(event)
        if event[0] == "analysis_ok":
            self.catalog = MediaCatalog.from_analysis(event[2])
            if isinstance(self.engine, WindowsEngine):
                self.engine.analysis_data = event[2]
        elif isinstance(self.engine, WindowsEngine) and len(event) > 1:
            if event[0] == "download_done":
                self.engine.download_results = list(event[1])
            elif event[0] in ("subtitle_retry_done", "subtitle_retry_failed"):
                additions = event[1] if event[0] == "subtitle_retry_done" else (
                    event[3] if len(event) > 3 else [])
                self.engine.download_results.extend(
                    path for path in additions if path not in self.engine.download_results)
        if event[0] in ("analysis_ok", "analysis_error", "analysis_cancelled"):
            self.analysis_thread = None
        elif event[0] == "verification_required":
            if len(event) > 1 and event[1] is None:
                self.download_thread = None
            else:
                self.analysis_thread = None
        elif event[0] in ("download_done", "download_error", "download_cancelled", "download_formats_changed",
                          "subtitle_retry_done", "subtitle_retry_failed"):
            self.download_thread = None
        return event

    def follow(self, timeout: float | None = None) -> Iterator[OrchestratorEvent]:
        """Yield events until a terminal result; timeout raises queue.Empty."""
        while True:
            event = (self.next_event(timeout) if timeout is not None
                     else self._record_event(self.engine.events.get()))
            structured = OrchestratorEvent.from_legacy(event)
            yield structured
            if structured.kind in TERMINAL_EVENTS:
                return
