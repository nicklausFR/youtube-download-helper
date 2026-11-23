"""Pure media format, language, and stream selection rules."""

import re
from urllib.parse import parse_qs, urlparse


def human_size(num):
    if not num:
        return "?"
    n = float(num)
    for unit in ("o", "Ko", "Mo", "Go", "To"):
        if n < 1024 or unit == "To":
            if unit == "o":
                return f"{int(n)} {unit}"
            return f"{n:.1f} {unit}"
        n /= 1024
    return "?"


def codec_name(codec):
    value = (codec or "").lower()
    for prefixes, label in (
        (("avc", "h264"), "H.264"), (("hev", "hvc", "hevc"), "H.265"),
        (("av01", "av1"), "AV1"), (("vp09", "vp9"), "VP9"),
        (("vp08", "vp8"), "VP8"), (("mp4a", "aac"), "AAC"),
        (("opus",), "Opus"), (("vorbis",), "Vorbis"),
    ):
        if value.startswith(prefixes):
            return label
    return codec if codec and codec != "none" else "codec inconnu"


def audio_bitrate(fmt):
    return fmt.get("abr") or (fmt.get("tbr") if fmt.get("vcodec") == "none" else None)


def format_fits_container(fmt, container):
    if container in ("Auto", "MKV", "MKA"):
        return True
    video, audio = fmt.get("vcodec"), fmt.get("acodec")
    allowed = {
        "MP4": ({"H.264", "H.265", "AV1"}, {"AAC"}),
        "M4A": (set(), {"AAC"}),
        "WebM": ({"VP8", "VP9", "AV1"}, {"Opus", "Vorbis"}),
    }
    video_codecs, audio_codecs = allowed[container]
    return ((not video or video == "none" or codec_name(video) in video_codecs)
            and (not audio or audio == "none" or codec_name(audio) in audio_codecs))


def container_args(mode, container):
    if not container:
        return []
    args = ["--remux-video", container]
    if mode in ("video", "video_only"):
        args += ["--merge-output-format", container]
    return args


LANGUAGE_NAMES = {"en": "anglais", "fr": "français", "de": "allemand", "es": "espagnol",
                  "it": "italien", "pt": "portugais", "ja": "japonais", "ko": "coréen",
                  "zh": "chinois", "ar": "arabe", "ru": "russe", "nl": "néerlandais"}


MATROSKA_LANGUAGE_CODES = {
    "en": "eng", "fr": "fra", "de": "deu", "es": "spa", "it": "ita",
    "pt": "por", "ja": "jpn", "ko": "kor", "zh": "zho", "ar": "ara",
    "ru": "rus", "nl": "nld",
}


CODEC_HELP = {"Auto": "Auto : H.264 si disponible, puis VP9, puis AV1.",
              "H.264": "H.264 : largement compatible avec les lecteurs et appareils.",
              "VP9": "VP9 : compression efficace ; vérifier la prise en charge sur les anciens appareils.",
              "AV1": "AV1 : compression efficace ; décodage parfois plus exigeant sur les anciens appareils.",
              "H.265": "H.265 : compression efficace ; compatibilité variable selon le lecteur."}


def language_base(code):
    return (code or "").lower().removesuffix("-orig").split("-")[0]


def language_label(code):
    return f"{LANGUAGE_NAMES.get(language_base(code), code)} ({code})" if code else "non déterminée"


_AUDIO_ROLE_NOTES = {
    "dubbed-auto": r"\bdubbed-auto\b|\bauto[ -]dubbed\b|\bautomatically dubbed\b",
    "descriptive": r"\bdescriptive\b",
    "dubbed": r"\bdubbed\b",
    "original": r"\boriginal\b",
}


def _audio_role_from_url(url):
    query = parse_qs(urlparse(url or '').query)
    for tags in query.get('xtags', []):
        for tag in tags.split(':'):
            name, _, value = tag.partition('=')
            if name == 'acont' and value in _AUDIO_ROLE_NOTES:
                return value
    return None


def _audio_role_from_note(fmt):
    note = (fmt.get('format_note') or '').lower()
    return next((role for role, pattern in _AUDIO_ROLE_NOTES.items()
                 if re.search(pattern, note)), None)


def audio_content_type(fmt):
    """Read YouTube's explicit audio marker; another language alone is not proof of auto dubbing."""
    return (fmt.get('_downloader_audio_content_type') or _audio_role_from_url(fmt.get('url'))
            or _audio_role_from_url(fmt.get('manifest_url')) or _audio_role_from_note(fmt))


def audio_track_selector(selector, audio):
    """Require the selected language and any explicit audio role at extraction."""
    language = audio.get("language") or ""
    if re.fullmatch(r"[A-Za-z0-9]+(?:-[A-Za-z0-9]+)*", language):
        selector += f"[language={language}]"
    role = audio_content_type(audio)
    if role not in _AUDIO_ROLE_NOTES:
        return selector

    # Check the same evidence as the catalog. If the evidence changes, the
    # format recovery can compare the freshly extracted metadata directly.
    tag_prefix = r"(?i)[?&]xtags=(?:[^&#]*(?:%3a|:))?acont(?:%3d|=)"
    tag_suffix = r"(?=%3a|:|[&#]|$)"
    expected_tag = tag_prefix + role + tag_suffix
    conflicting_tag = f"{tag_prefix}(?!{role}{tag_suffix})"
    if _audio_role_from_url(audio.get("url")) == role:
        return f"{selector}[url~='{expected_tag}']"
    if _audio_role_from_url(audio.get("manifest_url")) == role:
        return f"{selector}[manifest_url~='{expected_tag}'][url!~=?'{conflicting_tag}']"
    if _audio_role_from_note(audio) != role:
        # Restored transfers retain the role but omit their old signed URLs.
        return (f"({selector}[url~='{expected_tag}']/"
                f"{selector}[manifest_url~='{expected_tag}'][url!~=?'{conflicting_tag}'])")

    selector += (f"[format_note~='(?i){_AUDIO_ROLE_NOTES[role]}']"
                 f"[url!~=?'{conflicting_tag}'][manifest_url!~=?'{conflicting_tag}']")
    higher_priority = list(_AUDIO_ROLE_NOTES).index(role)
    if higher_priority:
        other_notes = "|".join(list(_AUDIO_ROLE_NOTES.values())[:higher_priority])
        selector += f"[format_note!~='(?i){other_notes}']"
    return selector


def guard_audio_tracks(fmt, selection):
    audios = list(selection.get("audios") or ([selection["audio"]] if selection.get("audio") else []))
    video = selection.get("video")
    if video and video.get("acodec") not in (None, "none"):
        audios.append(video)
    by_id = {str(audio.get("format_id")): audio for audio in audios}
    return "".join(audio_track_selector(part, by_id[part]) if part in by_id else part
                   for part in re.split(r"([+,])", fmt))


def original_language_info(data):
    def unique(codes):
        codes = {c for c in codes if isinstance(c, str) and c and c.lower() not in ("und", "unknown", "zxx")}
        if len({language_base(c) for c in codes}) == 1:
            return sorted(codes, key=lambda c: (len(c), c))[0]
        return None
    formats = data.get("formats") or []
    marked = [f.get("language") for f in formats if
              audio_content_type(f) == "original" or ((f.get("language_preference") or 0) >= 10 and audio_content_type(f) not in ("dubbed", "dubbed-auto", "descriptive"))]
    language = unique(marked)
    if language:
        return language, "piste audio marquée d’origine", False
    for key in ("original_language", "default_audio_language", "language"):
        language = unique([data.get(key)])
        if language:
            return language, "métadonnée vidéo : " + key, False
    captions = data.get("automatic_captions") or {}
    language = unique(key.removesuffix("-orig") for key in captions if key.endswith("-orig"))
    if language:
        return language, "transcription automatique d’origine", False
    source_codes = []
    for code, entries in captions.items():
        for entry in entries:
            query = parse_qs(urlparse(entry.get("url", "")).query)
            if query.get("lang"):
                source_codes.extend(query["lang"])
            elif not query.get("tlang"):
                source_codes.append(code)
    language = unique(source_codes)
    if language:
        return language, "langue source de la transcription", False
    language = unique(f.get("language") for f in formats
        if f.get("acodec") not in (None, "none") and (f.get("language_preference") or 0) >= 0
        and audio_content_type(f) not in ("dubbed", "dubbed-auto", "descriptive"))
    if language:
        return language, "seule langue des pistes audio reçues", True
    return None, "aucune métadonnée de langue exploitable reçue", False


def original_language(data):
    return original_language_info(data)[0]


def available_video_heights(data):
    return sorted({
        int(fmt["height"])
        for fmt in (data.get("formats") or [])
        if fmt.get("vcodec") not in (None, "none")
        and isinstance(fmt.get("height"), (int, float))
    })


def suspicious_low_quality(data):
    """A 360p-only answer is often a degraded YouTube extraction, but can be genuine."""
    if data.get("is_live") or data.get("live_status") in ("is_live", "is_upcoming"):
        return False
    heights = available_video_heights(data)
    return bool(heights) and max(heights) <= 360


def subtitle_choices(data, origin):
    """Prefer published captions and native transcriptions over same-language translations."""
    tracks = []
    manual = data.get("subtitles") or {}
    automatic = data.get("automatic_captions") or {}
    published_languages = {code.casefold() for code, items in manual.items() if items}
    native_languages = set()
    for code, items in automatic.items():
        if items and any(not parse_qs(urlparse(item.get("url", "")).query).get("tlang")
                         for item in items):
            native_languages.add(code.removesuffix("-orig").casefold())

    def caption_language(code, items):
        language = code.removesuffix("-orig")
        name = LANGUAGE_NAMES.get(language_base(language))
        if not name:
            name = re.sub(r"\s*\((?:Original|auto-generated)\)$", "",
                          items[0].get("name") or language, flags=re.I)
        # Regional variants must remain distinguishable without exposing -orig.
        return f"{name} ({language})" if "-" in language else name

    for group, entries in (("manual", manual), ("auto", automatic)):
        for code, items in entries.items():
            if not items or code == "live_chat":
                continue
            queries = [parse_qs(urlparse(item.get("url", "")).query) for item in items]
            translated = group == "auto" and all(q.get("tlang") for q in queries)
            language = code.removesuffix("-orig")
            if group == "auto":
                if language.casefold() in published_languages:
                    continue
                # yt-dlp exposes its native transcription under en and en-orig.
                # en may also contain translations from another audio language;
                # prefer the unambiguous native entry whenever it is available.
                if not code.endswith("-orig") and automatic.get(code + "-orig"):
                    continue
                if translated and language.casefold() in native_languages:
                    continue
            is_origin = bool(origin and language_base(code) == language_base(origin) and not translated)
            kind = "" if group == "manual" else "traduction auto" if translated else "transcription auto"
            provenance = kind or "publié"
            sources = {q["lang"][-1] for q in queries if q.get("lang")}
            source = next(iter(sources)) if len(sources) == 1 else None
            if translated and source:
                source_name = LANGUAGE_NAMES.get(language_base(source), source)
                article = "l’" if source_name[0].lower() in "aeiouéèêàâîôû" else "le "
                provenance += f" depuis {article}{source_name}"
            marker = " · langue d’origine" if is_origin else ""
            label = f"{caption_language(code, items)} — {provenance}{marker}"
            tooltip = f"{label}\nCode : {code}"
            tracks.append({"code": code, "label": label, "original": is_origin,
                           "kind": kind, "tooltip": tooltip})
    return sorted(tracks, key=lambda t: (not t["original"], bool(t["kind"]),
                                       t["kind"] == "traduction auto", t["label"].casefold()))


def _codec_prefix(codec, video=False):
    """Return the stable yt-dlp codec family used across YouTube clients."""
    value = str(codec or "").lower()
    for known in (
            "avc1", "h264", "vp09", "vp9", "vp8", "av01",
            "hev1", "hvc1", "hevc", "mp4a", "aac", "opus", "vorbis"):
        if value.startswith(known):
            return known
    label = codec_name(codec)
    families = {
        "H.264": "avc1", "VP9": "vp09", "AV1": "av01",
        "H.265": "hev", "AAC": "mp4a", "Opus": "opus",
        "Vorbis": "vorbis", "VP8": "vp8",
    }
    prefix = families.get(label)
    if prefix:
        return prefix
    match = re.match(r"[a-z0-9]+", value)
    return match.group(0) if match else ("" if video else value)


def _direct_stream_selector(kind, fmt, include_rate=False):
    """Describe a direct stream semantically instead of by client-specific ID."""
    field = "vcodec" if kind == "video" else "acodec"
    prefix = _codec_prefix((fmt or {}).get(field), video=kind == "video")
    selector = "bestvideo" if kind == "video" else "bestaudio"
    filters = ["[protocol=https]"]
    if prefix:
        filters.append(f"[{field}^={prefix}]")
    if kind == "video" and isinstance((fmt or {}).get("height"), (int, float)):
        filters.append(f"[height={int(fmt['height'])}]")
    if kind == "audio" and include_rate:
        rate = audio_bitrate(fmt or {})
        if isinstance(rate, (int, float)) and rate > 0:
            tolerance = max(4, rate * 0.12)
            filters += [f"[abr>={rate - tolerance:.3f}]", f"[abr<={rate + tolerance:.3f}]"]
    selector += "".join(filters)
    return audio_track_selector(selector, fmt or {}) if kind == "audio" else selector


def clip_format_selector(mode, video, audio, audios=None):
    """Keep the chosen quality while allowing IDs to differ on the clip route."""
    audios = list(audios or ([audio] if audio else []))
    if mode == "audio":
        if len(audios) > 1:
            return ",".join(
                _direct_stream_selector("audio", item, include_rate=True)
                for item in audios)
        variants = [
            _direct_stream_selector("audio", audio, include_rate=rate)
            for rate in (True, False)
        ]
        return "/".join(dict.fromkeys(variants))

    video_selector = _direct_stream_selector("video", video)
    if mode == "video_only":
        return video_selector

    # A progressive format already contains its audio track.
    if video and video.get("acodec") not in (None, "none") and not audio:
        combined = video_selector.replace("bestvideo", "best", 1)
        audio_prefix = _codec_prefix(video.get("acodec"))
        if audio_prefix:
            combined += f"[acodec^={audio_prefix}]"
        return audio_track_selector(combined, video)

    if len(audios) > 1:
        return video_selector + "+" + "+".join(
            _direct_stream_selector("audio", item, include_rate=True)
            for item in audios)

    audio_variants = [
        _direct_stream_selector("audio", audio, include_rate=rate)
        for rate in (True, False)
    ]
    return "/".join(
        video_selector + "+" + item
        for item in dict.fromkeys(audio_variants)
    )


def estimated_bytes(fmt, duration=None):
    for key in ("filesize", "filesize_approx"):
        if isinstance(fmt.get(key), (int, float)) and fmt[key] > 0:
            return int(fmt[key])
    duration = fmt.get("duration") or duration
    bitrate = fmt.get("tbr") or (fmt.get("abr") if fmt.get("vcodec") == "none" else fmt.get("vbr"))
    if isinstance(duration, (int, float)) and isinstance(bitrate, (int, float)) and duration > 0 and bitrate > 0:
        return int(duration * bitrate * 1000 / 8)
    return None
