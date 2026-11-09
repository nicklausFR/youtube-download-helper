"""Application media models and selection rules; no Qt or process dependency."""

from dataclasses import dataclass
from typing import Any

from core_formats import (
    audio_bitrate, codec_name, estimated_bytes, format_fits_container,
    human_size, language_base,
)
from core_subtitles import parse_timecode
import re


Format = dict[str, Any]


@dataclass(frozen=True)
class MediaCatalog:
    metadata: Format
    videos_by_height: dict[int, list[Format]]
    audio_formats: list[Format]

    @classmethod
    def from_analysis(cls, data: Format) -> "MediaCatalog":
        videos: dict[int, list[Format]] = {}
        audios: list[Format] = []
        for fmt in data.get("formats") or []:
            vcodec, acodec = fmt.get("vcodec"), fmt.get("acodec")
            height = fmt.get("height")
            if vcodec == "none" and acodec and acodec != "none":
                audios.append(fmt)
            if vcodec and vcodec != "none" and height:
                videos.setdefault(int(height), []).append(fmt)
        return cls(data, videos, audios)


@dataclass(frozen=True)
class DownloadRequest:
    url: str
    folder: str
    mode: str
    format_id: str
    subtitle_languages: str | None = None
    container: str | None = None
    clip: tuple[float, float | None] | None = None
    session: Any = False
    partial_path: str | None = None


@dataclass(frozen=True)
class VideoOptions:
    profiles: list[tuple[int, float]]
    labels: list[str]
    selected_label: str
    codecs: list[str]
    selected_codec: str
    formats: list[Format]


def audio_options(formats: list[Format], mode: str, container: str,
                  automatic: bool, original_language: str | None) -> list[Format]:
    ordered = sorted(
        [fmt for fmt in formats
         if format_fits_container(dict(fmt, vcodec="none") if mode == "audio" else fmt, container)
         and (not automatic or codec_name(fmt.get("acodec")) == "AAC")],
        key=lambda fmt: (
            bool(original_language and language_base(fmt.get("language")) == language_base(original_language)),
            fmt.get("abr") or fmt.get("tbr") or 0, str(fmt.get("format_id"))),
        reverse=True,
    )
    if automatic:
        ordered.sort(key=lambda fmt: (
            not bool(original_language and language_base(fmt.get("language")) == language_base(original_language)),
            -(fmt.get("language_preference") or 0),
            abs((audio_bitrate(fmt) or 128) - 128),
            fmt.get("protocol") != "https",
        ))
    return ordered


def video_options(formats_by_height: dict[int, list[Format]], mode: str,
                  container: str, automatic: bool, selected_text: str,
                  preferred_height: int | None, selected_codec: str) -> VideoOptions:
    groups = {}
    for height, formats in formats_by_height.items():
        for fmt in formats:
            if mode == "video_only" and fmt.get("acodec") not in (None, "none"):
                continue
            if automatic and codec_name(fmt.get("vcodec")) != "H.264":
                continue
            if automatic and fmt.get("acodec") not in (None, "none") and codec_name(fmt.get("acodec")) != "AAC":
                continue
            if not format_fits_container(fmt, container):
                continue
            key = (height, codec_name(fmt.get("vcodec")), fmt.get("fps") or 0,
                   fmt.get("dynamic_range") or "", fmt.get("acodec") or "none")
            rank = (fmt.get("protocol") == "https", fmt.get("tbr") or 0)
            if key not in groups or rank > (
                    groups[key].get("protocol") == "https", groups[key].get("tbr") or 0):
                groups[key] = fmt
    videos = sorted(groups.values(), key=lambda fmt: (
        int(fmt["height"]), codec_name(fmt.get("vcodec")), fmt.get("fps") or 0,
        str(fmt.get("format_id"))))

    def profile_of(fmt):
        fps = fmt.get("fps")
        return int(fmt["height"]), float(fps) if isinstance(fps, (int, float)) else 0.0

    def label(profile):
        height, fps = profile
        return f"{height}p · {fps:g} i/s" if fps else f"{height}p · cadence inconnue"

    profiles = sorted({profile_of(fmt) for fmt in videos})
    labels = [label(profile) for profile in profiles]
    chosen = profiles[labels.index(selected_text)] if selected_text in labels else None
    if chosen is None:
        match = re.match(r"^(\d+)p", selected_text or "")
        requested_height = int(match.group(1)) if match else preferred_height
        matching = [profile for profile in profiles if profile[0] == requested_height]
        if matching:
            chosen = max(matching, key=lambda profile: profile[1])
    if chosen is None and profiles:
        heights = {profile[0] for profile in profiles}
        height = next((item for item in (720, 1080, 480, 360) if item in heights), max(heights))
        chosen = max((profile for profile in profiles if profile[0] == height),
                     key=lambda profile: profile[1])
    selected = [fmt for fmt in videos if chosen and profile_of(fmt) == chosen]
    codecs = sorted({codec_name(fmt.get("vcodec")) for fmt in selected})
    if selected_codec not in codecs:
        selected_codec = next((item for item in ("H.264", "VP9", "AV1", "H.265")
                               if item in codecs), codecs[0] if codecs else "")
    if selected_codec != "Auto":
        selected = [fmt for fmt in selected if codec_name(fmt.get("vcodec")) == selected_codec]
    else:
        available = {codec_name(fmt.get("vcodec")) for fmt in selected}
        chosen_codec = next((item for item in ("H.264", "VP9", "AV1", "H.265")
                             if item in available), next(iter(sorted(available)), None))
        selected = [fmt for fmt in selected if codec_name(fmt.get("vcodec")) == chosen_codec]
    return VideoOptions(profiles, labels, label(chosen) if chosen else "",
                        codecs, selected_codec, selected)


def format_size(fmt: Format) -> int | None:
    for key in ("filesize", "filesize_approx"):
        if fmt.get(key):
            return int(fmt[key])
    return None


def codec_rank(fmt: Format) -> tuple[int, float]:
    ext = (fmt.get("ext") or "").lower()
    codec = (fmt.get("vcodec") or "").lower()
    if "vp9" in codec or ext == "webm":
        score = 3
    elif "avc" in codec or "h264" in codec:
        score = 2
    elif "av01" in codec or "av1" in codec:
        score = 1
    else:
        score = 0
    return score, fmt.get("tbr") or fmt.get("vbr") or 0


def best_video_for_height(formats: list[Format]) -> Format | None:
    if not formats:
        return None
    video_only = [fmt for fmt in formats if fmt.get("vcodec") not in (None, "none")
                  and fmt.get("acodec") == "none"]
    return max(video_only or formats, key=codec_rank)


def best_audio(formats: list[Format]) -> Format | None:
    if not formats:
        return None

    def rank(fmt: Format) -> tuple[int, float]:
        bitrate = audio_bitrate(fmt) or 0
        ext = (fmt.get("ext") or "").lower()
        codec = (fmt.get("acodec") or "").lower()
        return int("opus" in codec or ext == "webm"), bitrate

    return max(formats, key=rank)


def select_formats(mode: str, video: Format | None = None,
                   audios: list[Format] | None = None,
                   subtitle_languages: str | None = None) -> Format | None:
    audios = list(audios or [])
    if mode == "subtitles":
        return {"format": "", "video": None, "audio": None} if subtitle_languages else None
    if mode == "audio":
        if not audios:
            return None
        return {"format": ",".join(str(item.get("format_id")) for item in audios),
                "video": None, "audio": audios[0], "audios": audios}
    if not video:
        return None
    if mode == "video_only":
        return {"format": str(video.get("format_id")), "video": video, "audio": None}
    if video.get("acodec") not in (None, "none") and len(audios) <= 1:
        return {"format": str(video.get("format_id")), "video": video, "audio": None}
    if not audios:
        return None
    return {"format": "+".join([str(video.get("format_id"))]
                               + [str(item.get("format_id")) for item in audios]),
            "video": video, "audio": audios[0], "audios": audios}


def estimate_size_label(selection: Format | None, mode: str,
                        duration: float | None, clip: tuple | None = None,
                        subtitles: bool = False) -> str:
    if not selection:
        return "Taille estimée : —"
    if mode == "subtitles":
        return ""
    streams = ([selection["video"]] if selection.get("video") else [])
    streams += list(selection.get("audios") or
                    ([selection["audio"]] if selection.get("audio") else []))
    clip_duration = None
    if clip:
        start, end = clip
        effective_end = end if end is not None else duration
        if isinstance(effective_end, (int, float)):
            clip_duration = max(0, effective_end - start)
    if mode == "audio" and any(
            item.get("vcodec") not in (None, "none")
            for item in selection.get("audios", [selection["audio"]])):
        sizes = [estimated_bytes({"vcodec": "none", "abr": audio.get("abr")},
                                 clip_duration or duration)
                 for audio in selection.get("audios", [selection["audio"]])]
    else:
        sizes = [estimated_bytes(fmt, duration) for fmt in streams]
        if clip_duration is not None and isinstance(duration, (int, float)) and duration > 0:
            sizes = [int(size * clip_duration / duration) if size is not None else None
                     for size in sizes]
    if not sizes or any(size is None for size in sizes):
        return "Taille estimée : indisponible pour ce choix"
    suffix = " + sous-titres" if subtitles else ""
    return f"Taille estimée : ≈ {human_size(sum(sizes))}{suffix}"


def clip_validation_error(settings: str, mode: str, start_text: str,
                          end_text: str, duration: float | None) -> str:
    if settings != "manual" or mode == "subtitles":
        return ""
    start_text, end_text = start_text.strip(), end_text.strip()
    if not start_text and not end_text:
        return ""
    try:
        start = parse_timecode(start_text) if start_text else 0
        end = parse_timecode(end_text) if end_text else None
    except ValueError as error:
        return str(error)
    if end is None and isinstance(duration, (int, float)):
        end = float(duration)
    if end is not None and end <= start:
        return "La fin doit être postérieure au début."
    if isinstance(duration, (int, float)) and start >= duration:
        return "Le début doit être placé avant la fin de la vidéo."
    if isinstance(duration, (int, float)) and end is not None and end > duration:
        return "La fin dépasse la durée de la vidéo."
    return ""


def clip_range(settings: str, mode: str, start_text: str, end_text: str,
               duration: float | None) -> tuple[float, float | None] | None:
    if clip_validation_error(settings, mode, start_text, end_text, duration):
        return None
    if settings != "manual" or mode == "subtitles":
        return None
    start_text, end_text = start_text.strip(), end_text.strip()
    if not start_text and not end_text:
        return None
    start = parse_timecode(start_text) if start_text else 0
    end = parse_timecode(end_text) if end_text else None
    if end is None and isinstance(duration, (int, float)):
        end = float(duration)
    if isinstance(duration, (int, float)) and start <= 0 and end is not None and abs(end - duration) < .001:
        return None
    return start, end
