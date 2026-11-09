"""Replaceable access and download routes for the existing Windows engine."""

from contextlib import contextmanager
from dataclasses import dataclass
from typing import Any, Protocol

from verified_session import VerifiedCredentials


class AccessStrategy(Protocol):
    name: str

    def accepts(self, session: Any) -> bool: ...
    def options(self, session: Any, analysis: bool): ...


class AccessRegistry:
    def __init__(self, strategies=()):
        self._strategies: list[AccessStrategy] = list(strategies)

    def register(self, strategy: AccessStrategy, *, first=False):
        if first:
            self._strategies.insert(0, strategy)
        else:
            self._strategies.append(strategy)

    def copy(self):
        return AccessRegistry(self._strategies)

    def resolve(self, session: Any) -> AccessStrategy:
        for strategy in self._strategies:
            if strategy.accepts(session):
                return strategy
        raise LookupError(f"Aucune stratégie d’accès pour {type(session).__name__}")

    @contextmanager
    def options(self, session=False, analysis=False):
        with self.resolve(session).options(session, analysis) as options:
            yield options


@dataclass(frozen=True)
class VerifiedAccess:
    name: str = "verified"

    def accepts(self, session):
        return isinstance(session, VerifiedCredentials)

    @contextmanager
    def options(self, session, analysis):
        # Import here to keep the policy module independent of process setup.
        from youtube_access import access_args
        with session.cookie_file() as path:
            options = [item for item in access_args(False, analysis)
                       if item not in ("--no-cookies", "--no-cookies-from-browser")]
            options += ["--no-cookies-from-browser", "--cookies", path]
            if session.user_agent:
                options += ["--user-agent", session.user_agent]
            yield options


@dataclass(frozen=True)
class BraveAccess:
    name: str = "brave"

    def accepts(self, session):
        return session is True

    @contextmanager
    def options(self, session, analysis):
        from youtube_access import access_args
        yield access_args(True, analysis)


@dataclass(frozen=True)
class AutomaticAccess:
    name: str = "automatic"

    def accepts(self, session):
        return not session

    @contextmanager
    def options(self, session, analysis):
        from youtube_access import access_args
        yield access_args(False, analysis)


DEFAULT_ACCESS = AccessRegistry((VerifiedAccess(), BraveAccess(), AutomaticAccess()))


class DownloadStrategy(Protocol):
    name: str

    def accepts(self, mode: str, clip: tuple | None) -> bool: ...
    def run(self, engine, url, folder, mode, fmt, subtitle_lang,
            merge_format, clip, options, session, subtitle_completion): ...


class DownloadRegistry:
    def __init__(self, strategies=()):
        self._strategies: list[DownloadStrategy] = list(strategies)

    def register(self, strategy: DownloadStrategy, *, first=False):
        if first:
            self._strategies.insert(0, strategy)
        else:
            self._strategies.append(strategy)

    def copy(self):
        return DownloadRegistry(self._strategies)

    def resolve(self, mode, clip=None) -> DownloadStrategy:
        for strategy in self._strategies:
            if strategy.accepts(mode, clip):
                return strategy
        raise LookupError(f"Aucune stratégie de téléchargement pour {mode}")


@dataclass(frozen=True)
class SubtitleStrategy:
    name: str = "subtitles"

    def accepts(self, mode, clip):
        return mode in ("subtitles", "subtitles_retry")

    def run(self, engine, url, folder, mode, fmt, subtitle_lang,
            merge_format, clip, options, session, subtitle_completion):
        engine._download_subtitles_only(
            url, folder, subtitle_lang, options, session, clip,
            retry=mode == "subtitles_retry")


@dataclass(frozen=True)
class ExcerptStrategy:
    name: str = "excerpt"

    def accepts(self, mode, clip):
        return bool(clip) and mode in ("video", "video_only", "audio")

    def run(self, engine, url, folder, mode, fmt, subtitle_lang,
            merge_format, clip, options, session, subtitle_completion):
        return engine._download_with_access(
            url, folder, mode, fmt, subtitle_lang, merge_format, clip, options,
            subtitle_completion=subtitle_completion)


@dataclass(frozen=True)
class FullMediaStrategy:
    name: str = "full_media"

    def accepts(self, mode, clip):
        return not clip and mode in ("video", "video_only", "audio")

    def run(self, engine, url, folder, mode, fmt, subtitle_lang,
            merge_format, clip, options, session, subtitle_completion):
        return engine._download_with_access(
            url, folder, mode, fmt, subtitle_lang, merge_format, None, options,
            subtitle_completion=subtitle_completion)


DEFAULT_DOWNLOADS = DownloadRegistry(
    (SubtitleStrategy(), ExcerptStrategy(), FullMediaStrategy()))
