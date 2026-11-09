"""Subtitle and timecode rules, independent of Qt and the downloader."""

import os
import re


def parse_timecode(value):
    """Return seconds for SS, MM:SS or HH:MM:SS.mmm input."""
    text = str(value or "").strip()
    if not text:
        return None
    parts = text.split(":")
    if not 1 <= len(parts) <= 3 or any(not part.isdigit() for part in parts[:-1]):
        raise ValueError("Utilisez le format HH:MM:SS.mmm.")
    if not re.fullmatch(r"\d+(?:[.,]\d{1,3})?", parts[-1]):
        raise ValueError("Utilisez le format HH:MM:SS.mmm.")
    seconds = float(parts[-1].replace(',', '.'))
    if seconds < 0 or len(parts) > 1 and seconds >= 60:
        raise ValueError("Les secondes doivent être comprises entre 00 et 59.")
    minutes = int(parts[-2]) if len(parts) > 1 else 0
    hours = int(parts[-3]) if len(parts) > 2 else 0
    if len(parts) > 2 and minutes >= 60:
        raise ValueError("Les minutes doivent être comprises entre 00 et 59.")
    return hours * 3600 + minutes * 60 + seconds


def format_timecode(seconds):
    milliseconds = max(0, round(float(seconds) * 1000))
    minutes, milliseconds = divmod(milliseconds, 60_000)
    hours, minutes = divmod(minutes, 60)
    seconds, milliseconds = divmod(milliseconds, 1000)
    fraction = f".{milliseconds:03}" if milliseconds else ""
    return f"{hours:02}:{minutes:02}:{seconds:02}{fraction}"


SRT_TIMING = re.compile(
    r"^(?P<start>\d+:\d{2}:\d{2}[,.]\d{3})\s*-->\s*"
    r"(?P<end>\d+:\d{2}:\d{2}[,.]\d{3})(?P<settings>.*)$")


def format_srt_time(seconds):
    milliseconds = max(0, round(float(seconds) * 1000))
    minutes, milliseconds = divmod(milliseconds, 60_000)
    hours, minutes = divmod(minutes, 60)
    seconds, milliseconds = divmod(milliseconds, 1000)
    return f"{hours:02}:{minutes:02}:{seconds:02},{milliseconds:03}"


def trim_srt_text(text, clip_start, clip_end=None):
    """Crop cues to a media extract and move its timeline back to zero."""
    clip_start = max(0.0, float(clip_start or 0))
    clip_end = None if clip_end is None else max(clip_start, float(clip_end))
    output = []
    normalized = str(text or "").replace("\r\n", "\n").replace("\r", "\n")
    for block in re.split(r"\n{2,}", normalized.strip()):
        lines = block.split("\n")
        timing_index = next(
            (index for index, line in enumerate(lines) if SRT_TIMING.match(line.strip())),
            None)
        if timing_index is None:
            continue
        match = SRT_TIMING.match(lines[timing_index].strip())
        cue_start = parse_timecode(match.group("start"))
        cue_end = parse_timecode(match.group("end"))
        if cue_start is None or cue_end is None or cue_end <= clip_start:
            continue
        if clip_end is not None and cue_start >= clip_end:
            continue
        new_start = max(cue_start, clip_start) - clip_start
        new_end = (min(cue_end, clip_end) if clip_end is not None else cue_end) - clip_start
        if new_end <= new_start:
            continue
        timing = (
            f"{format_srt_time(new_start)} --> {format_srt_time(new_end)}"
            f"{match.group('settings')}")
        payload = "\n".join(lines[timing_index + 1:]).strip("\n")
        output.append(f"{len(output) + 1}\n{timing}\n{payload}".rstrip())
    return "\n\n".join(output) + ("\n" if output else "")


def trim_srt_file(path, clip_start, clip_end=None):
    with open(path, "r", encoding="utf-8-sig", errors="replace") as source:
        content = source.read()
    synchronized = trim_srt_text(content, clip_start, clip_end)
    temporary = path + ".sync.tmp"
    try:
        with open(temporary, "w", encoding="utf-8", newline="\n") as target:
            target.write(synchronized)
        os.replace(temporary, path)
    finally:
        try:
            os.remove(temporary)
        except FileNotFoundError:
            pass


def clean_rolling_srt_text(text):
    """Turn YouTube's two-line rolling automatic captions into stable cues."""
    normalized = str(text or "").replace("\r\n", "\n").replace("\r", "\n")
    cues = []
    for block in re.split(r"\n{2,}", normalized.strip()):
        lines = block.split("\n")
        timing_index = next(
            (index for index, line in enumerate(lines)
             if SRT_TIMING.match(line.strip())), None)
        if timing_index is None:
            continue
        match = SRT_TIMING.match(lines[timing_index].strip())
        payload = [line.strip() for line in lines[timing_index + 1:] if line.strip()]
        if not payload:
            continue
        cues.append({
            "start": parse_timecode(match.group("start")),
            "end": parse_timecode(match.group("end")),
            "settings": match.group("settings"),
            "lines": payload,
        })
    if len(cues) < 2:
        return normalized

    def comparable(line):
        return re.sub(r"\s+", " ", re.sub(r"<[^>]+>", "", line)).strip().casefold()

    overlaps = [0]
    rolling_found = False
    temporal_overlaps = 0
    for previous, current in zip(cues, cues[1:]):
        before = [comparable(line) for line in previous["lines"]]
        after = [comparable(line) for line in current["lines"]]
        overlap = 0
        for count in range(min(len(before), len(after)), 0, -1):
            if before[-count:] == after[:count]:
                overlap = count
                break
        # An exact repeated cue is handled by merging, but is not enough on its
        # own to classify an authored subtitle as a rolling YouTube transcript.
        if overlap and len(after) > overlap:
            rolling_found = True
        if (
                previous["end"] > current["start"] + 0.05
                and current["start"] >= previous["start"]):
            temporal_overlaps += 1
        overlaps.append(overlap)
    # Some automatic YouTube tracks roll one phrase at a time instead of
    # repeating the previous line. Their text gives us no overlap to detect,
    # but their consecutive time ranges still overlap heavily. In a player,
    # that makes the old phrase remain visible beside the new one. Treat a
    # repeated timing pattern as rolling captions too, while leaving an
    # isolated overlap in authored subtitles untouched.
    transitions = len(cues) - 1
    if (
            len(cues) >= 3
            and temporal_overlaps >= max(2, (transitions + 3) // 4)):
        rolling_found = True
    if not rolling_found:
        return normalized

    stable = []
    for index, cue in enumerate(cues):
        overlap = overlaps[index]
        lines = cue["lines"][overlap:] if overlap < len(cue["lines"]) else []
        if not lines:
            if stable:
                stable[-1]["end"] = max(stable[-1]["end"], cue["end"])
            continue
        payload = "\n".join(lines)
        if stable and comparable(stable[-1]["payload"]) == comparable(payload):
            stable[-1]["end"] = max(stable[-1]["end"], cue["end"])
            continue
        if stable and stable[-1]["end"] > cue["start"]:
            stable[-1]["end"] = cue["start"]
            if stable[-1]["end"] <= stable[-1]["start"]:
                stable.pop()
        stable.append({
            "start": cue["start"], "end": cue["end"],
            "settings": cue["settings"], "payload": payload,
        })

    output = []
    for cue in stable:
        if cue["end"] <= cue["start"]:
            continue
        output.append(
            f"{len(output) + 1}\n{format_srt_time(cue['start'])} --> "
            f"{format_srt_time(cue['end'])}{cue['settings']}\n{cue['payload']}")
    return "\n\n".join(output) + ("\n" if output else "")


def clean_rolling_srt_file(path):
    with open(path, "r", encoding="utf-8-sig", errors="replace") as source:
        content = source.read()
    cleaned = clean_rolling_srt_text(content)
    if cleaned == content:
        return False
    temporary = path + ".clean.tmp"
    try:
        with open(temporary, "w", encoding="utf-8", newline="\n") as target:
            target.write(cleaned)
        os.replace(temporary, path)
    finally:
        try:
            os.remove(temporary)
        except FileNotFoundError:
            pass
    return True
