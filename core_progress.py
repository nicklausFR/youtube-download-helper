"""Downloader progress protocol and parser, independent of Qt."""

import json
import math
import re
from core_formats import estimated_bytes, human_size, language_label


def progress_labels(data):
    labels = {}
    for item in (data or {}).get("formats") or []:
        format_id = str(item.get("format_id") or "")
        if not format_id:
            continue
        has_video = item.get("vcodec") not in (None, "none")
        has_audio = item.get("acodec") not in (None, "none")
        if has_video:
            label = "Vidéo"
            if item.get("height"):
                label += f" {item['height']}p"
            if has_audio:
                label += " + audio"
        elif has_audio:
            label = "Audio"
            if item.get("language"):
                label += " " + language_label(item.get("language"))
        else:
            continue
        labels[format_id] = label
    return labels


PROGRESS_PREFIX = "YD_PROGRESS:"


PROGRESS_TEMPLATE = 'download:' + PROGRESS_PREFIX + '{"id":%(info.format_id)j,"status":%(progress.status|null)j,"downloaded":%(progress.downloaded_bytes|null)j,"total":%(progress.total_bytes|null)j,"estimate":%(progress.total_bytes_estimate|null)j,"speed":%(progress.speed|null)j,"eta":%(progress.eta|null)j,"fragment":%(progress.fragment_index|null)j,"fragments":%(progress.fragment_count|null)j}'


RESULT_PREFIX = 'YD_RESULT:'


RESULT_TEMPLATE = 'after_move:' + RESULT_PREFIX + '%(filepath)j'


CLIP_INFO_PREFIX = 'YD_CLIP_INFO:'


def remaining_time_label(seconds):
    minutes, seconds = divmod(max(1, round(seconds)), 60)
    hours, minutes = divmod(minutes, 60)
    return f"{hours:02}:{minutes:02}:{seconds:02}" if hours else f"{minutes:02}:{seconds:02}"


class DownloadProgress:
    def __init__(self, formats, mode="video", labels=None, data=None):
        self.ids = [item for item in re.split(r"[+,]", formats) if item]
        if not self.ids:
            self.ids = [formats]
        self.mode = mode
        self.labels = labels or {}
        self.fractions = {}
        self.percent = 0.0
        self.eta_seconds = None
        self.speed_bps = None
        self.detail_line = None
        self.detail_milestones = {}
        data = data or {}
        self.sizes = {str(item.get("format_id")): estimated_bytes(item, data.get("duration"))
                      for item in data.get("formats") or []}

    def _label(self, reported_fid, selector_fid):
        known = self.labels.get(reported_fid)
        if known:
            return known
        if self.mode == "audio":
            return "Audio"
        if self.mode == "video_only":
            return "Vidéo"
        if len(self.ids) == 1:
            return "Vidéo + audio"
        return "Vidéo" if self.ids.index(selector_fid) == 0 else "Audio"

    def parse(self, line):
        self.detail_line = None
        self.eta_seconds = None
        self.speed_bps = None
        if not line.startswith(PROGRESS_PREFIX):
            return None
        try:
            data = json.loads(line[len(PROGRESS_PREFIX):])
        except (ValueError, TypeError):
            return None
        if not isinstance(data, dict):
            return None
        def number(key):
            value = data.get(key)
            return max(0, value) if isinstance(value, (int, float)) and math.isfinite(value) else 0
        reported_fid = str(data.get("id") or "inconnu")
        fid = reported_fid
        if fid not in self.ids:
            fid = next((key for key in self.ids if self.fractions.get(key, 0) < 1), self.ids[-1])
        downloaded = number("downloaded")
        total = number("total") or number("estimate")
        speed = number("speed")
        eta = number("eta")
        finished = data.get("status") == "finished"
        if not finished and speed:
            self.speed_bps = speed
        fraction = downloaded / total if total else None
        if fraction is None and number("fragments"):
            fraction = number("fragment") / number("fragments")
        if finished:
            fraction = 1.0
        if fraction is not None:
            self.fractions[fid] = max(self.fractions.get(fid, 0), min(1, fraction))
            self.percent = max(self.percent, min(99, 99 * sum(self.fractions.values()) / len(self.ids)))
        percent = self.percent if self.fractions else None
        details = human_size(downloaded) + " / " + (
            human_size(total) if total else "—")
        if not finished:
            remaining = eta or ((total - downloaded) / speed if speed and total > downloaded else 0)
            other_streams = [key for key in self.ids if key != fid and self.fractions.get(key, 0) < 1]
            if remaining and not other_streams:
                self.eta_seconds = remaining
            elif remaining and speed and all(self.sizes.get(key) for key in other_streams):
                pending_bytes = sum(self.sizes[key] * (1 - self.fractions.get(key, 0)) for key in other_streams)
                self.eta_seconds = remaining + pending_bytes / speed
        milestone = int(min(4, max(0, (fraction or 0) * 4)))
        previous_milestone = self.detail_milestones.get(fid, -1)
        if finished:
            milestone = 4
        if milestone > previous_milestone:
            self.detail_milestones[fid] = milestone
            received = human_size(downloaded) if downloaded else "0 o"
            parts = [
                self._label(reported_fid, fid),
                f"{milestone * 25} %",
                received + " / " + (human_size(total) if total else "taille inconnue"),
            ]
            if speed:
                parts.append(human_size(speed) + "/s")
            if eta:
                parts.append("reste " + remaining_time_label(eta))
            fragment = number("fragment")
            fragments = number("fragments")
            if fragment and fragments:
                parts.append(f"fragment {int(fragment)}/{int(fragments)}")
            if finished:
                parts.append("terminé")
            self.detail_line = " · ".join(parts)
        return percent, details
