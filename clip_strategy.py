"""Native, volume-limited downloads of indexed YouTube media streams.

The normal yt-dlp downloader receives signed Google Video URLs.  For an
extract, we keep that acquisition path, read the media container index, and
request only the byte ranges containing the selected media fragments.  This
avoids the FFmpeg network path used by ``--download-sections``.
"""

from __future__ import annotations
from app_logging import log_event

from dataclasses import dataclass
import http.cookiejar
import os
import re
import ssl
import struct
import threading
import time
import urllib.error
import urllib.request


INITIAL_PROBE_BYTES = 256 * 1024
TAIL_PROBE_BYTES = 2 * 1024 * 1024
READ_BLOCK_BYTES = 256 * 1024


class NativeClipUnsupported(RuntimeError):
    """The selected stream cannot be sliced with its native container index."""


@dataclass(frozen=True)
class SegmentReference:
    start: float
    end: float
    offset: int
    size: int
    raw: bytes = b""


@dataclass(frozen=True)
class Mp4Index:
    box_start: int
    box_end: int
    media_start: int
    version: int
    timescale: int
    earliest: int
    first_offset: int
    references: tuple[SegmentReference, ...]


def _be32(data, offset):
    return struct.unpack_from(">I", data, offset)[0]


def _be64(data, offset):
    return struct.unpack_from(">Q", data, offset)[0]


def parse_mp4_sidx(data):
    """Parse a top-level MP4 SIDX box from an initial byte-range response."""
    position = 0
    length = len(data)
    while position + 8 <= length:
        size = _be32(data, position)
        kind = data[position + 4:position + 8]
        header = 8
        if size == 1:
            if position + 16 > length:
                break
            size = _be64(data, position + 8)
            header = 16
        elif size == 0:
            size = length - position
        if size < header:
            raise NativeClipUnsupported("Index MP4 invalide.")
        end = position + size
        if end > length:
            raise NativeClipUnsupported(
                f"L’index MP4 nécessite au moins {end} octets.")
        if kind != b"sidx":
            position = end
            continue

        payload = position + header
        version = data[payload]
        timescale = _be32(data, payload + 8)
        if not timescale:
            raise NativeClipUnsupported("Échelle de temps MP4 invalide.")
        cursor = payload + 12
        if version == 0:
            earliest = _be32(data, cursor)
            first_offset = _be32(data, cursor + 4)
            cursor += 8
        elif version == 1:
            earliest = _be64(data, cursor)
            first_offset = _be64(data, cursor + 8)
            cursor += 16
        else:
            raise NativeClipUnsupported(f"Version SIDX MP4 non gérée : {version}.")
        count = struct.unpack_from(">H", data, cursor + 2)[0]
        cursor += 4
        media_start = end + first_offset
        byte_offset = media_start
        timestamp = earliest
        references = []
        for _ in range(count):
            raw = data[cursor:cursor + 12]
            if len(raw) != 12:
                raise NativeClipUnsupported("Index MP4 tronqué.")
            size_field, duration = struct.unpack_from(">II", raw, 0)
            if size_field >> 31:
                raise NativeClipUnsupported("Index MP4 hiérarchique non géré.")
            fragment_size = size_field & 0x7FFFFFFF
            start = timestamp / timescale
            timestamp += duration
            references.append(SegmentReference(
                start, timestamp / timescale, byte_offset, fragment_size, raw))
            byte_offset += fragment_size
            cursor += 12
        if not references:
            raise NativeClipUnsupported("L’index MP4 ne contient aucun fragment.")
        return Mp4Index(
            position, end, media_start, version, timescale, earliest,
            first_offset, tuple(references))
    raise NativeClipUnsupported("Aucun index SIDX n’a été trouvé dans le flux MP4.")


def select_segments(references, start, end):
    """Return the contiguous media segments overlapping a requested interval."""
    start = max(0.0, float(start or 0))
    end = None if end is None else max(start, float(end))
    selected = [
        item for item in references
        if item.end > start and (end is None or item.start < end)
    ]
    if not selected:
        raise NativeClipUnsupported("Aucun fragment ne couvre l’extrait demandé.")
    return tuple(selected)


def rebuild_mp4_sidx(data, index, selected):
    """Replace SIDX with an index containing only the downloaded references."""
    original = data[index.box_start:index.box_end]
    header_size = 16 if _be32(original, 0) == 1 else 8
    payload = header_size
    version = original[payload]
    # FullBox/ref-id/timescale + earliest/offset + reserved/count.
    fixed_end = payload + (24 if version == 0 else 32)
    fixed = bytearray(original[:fixed_end])
    first = selected[0]
    earliest = round(first.start * index.timescale)
    time_offset = payload + 12
    if version == 0:
        struct.pack_into(">II", fixed, time_offset, earliest, index.first_offset)
    else:
        struct.pack_into(">QQ", fixed, time_offset, earliest, index.first_offset)
    struct.pack_into(">H", fixed, fixed_end - 2, len(selected))
    rebuilt = fixed + b"".join(item.raw for item in selected)
    if header_size == 8:
        struct.pack_into(">I", rebuilt, 0, len(rebuilt))
    else:
        struct.pack_into(">Q", rebuilt, 8, len(rebuilt))
    return bytes(rebuilt)


def _ebml_vint(data, offset, *, keep_marker=False):
    if offset >= len(data):
        raise NativeClipUnsupported("Index WebM tronqué.")
    first = data[offset]
    mask = 0x80
    width = 1
    while width <= 8 and not first & mask:
        mask >>= 1
        width += 1
    if width > 8 or offset + width > len(data):
        raise NativeClipUnsupported("Entier EBML invalide.")
    value = first if keep_marker else first & (mask - 1)
    for byte in data[offset + 1:offset + width]:
        value = (value << 8) | byte
    return value, width


def _ebml_elements(data, start=0, end=None):
    cursor = start
    limit = len(data) if end is None else min(len(data), end)
    while cursor < limit:
        element_id, id_width = _ebml_vint(data, cursor, keep_marker=True)
        size, size_width = _ebml_vint(data, cursor + id_width)
        payload = cursor + id_width + size_width
        element_end = payload + size
        if element_end > limit:
            # A ranged head probe normally contains only the beginning of the
            # top-level Segment.  Its children are still parseable in-buffer.
            if payload <= limit:
                yield element_id, cursor, payload, limit, id_width, size_width
            break
        yield element_id, cursor, payload, element_end, id_width, size_width
        cursor = element_end


def _ebml_uint(data, start, end):
    value = 0
    for byte in data[start:end]:
        value = (value << 8) | byte
    return value


def parse_webm_index(head, tail, total_size):
    """Read WebM cue points and return segment metadata plus cluster references."""
    segment = next((item for item in _ebml_elements(head) if item[0] == 0x18538067), None)
    if not segment:
        raise NativeClipUnsupported("Segment WebM introuvable.")
    _, segment_start, segment_data, _, id_width, size_width = segment

    timecode_scale = 1_000_000
    for element_id, _, payload, element_end, _, _ in _ebml_elements(head, segment_data):
        if element_id == 0x1549A966:  # Info
            for child_id, _, child_payload, child_end, _, _ in _ebml_elements(
                    head, payload, element_end):
                if child_id == 0x2AD7B1:
                    timecode_scale = _ebml_uint(head, child_payload, child_end)
                    break
        if element_id == 0x1F43B675:  # first Cluster
            break

    cue_marker = b"\x1c\x53\xbb\x6b"
    cue_at = tail.rfind(cue_marker)
    if cue_at < 0:
        raise NativeClipUnsupported("Table des fragments WebM introuvable.")
    cues = next((item for item in _ebml_elements(tail, cue_at) if item[0] == 0x1C53BB6B), None)
    if not cues:
        raise NativeClipUnsupported("Table des fragments WebM tronquée.")
    cue_points = []
    for element_id, _, payload, element_end, _, _ in _ebml_elements(tail, cues[2], cues[3]):
        if element_id != 0xBB:
            continue
        cue_time = None
        cluster_position = None
        for child_id, _, child_payload, child_end, _, _ in _ebml_elements(
                tail, payload, element_end):
            if child_id == 0xB3:
                cue_time = _ebml_uint(tail, child_payload, child_end)
            elif child_id == 0xB7:
                for pos_id, _, pos_payload, pos_end, _, _ in _ebml_elements(
                        tail, child_payload, child_end):
                    if pos_id == 0xF1:
                        cluster_position = _ebml_uint(tail, pos_payload, pos_end)
                        break
        if cue_time is not None and cluster_position is not None:
            cue_points.append((cue_time * timecode_scale / 1_000_000_000,
                               segment_data + cluster_position))
    cue_points = sorted(set(cue_points))
    if not cue_points:
        raise NativeClipUnsupported("La table WebM ne contient aucun fragment.")
    references = []
    for index, (timestamp, offset) in enumerate(cue_points):
        next_time, next_offset = (
            cue_points[index + 1] if index + 1 < len(cue_points)
            else (float("inf"), total_size))
        references.append(SegmentReference(
            timestamp, next_time, offset, max(0, next_offset - offset)))
    return {
        "segment_start": segment_start,
        "segment_data": segment_data,
        "header_end": references[0].offset,
        "size_offset": segment_start + id_width,
        "size_width": size_width,
        "references": tuple(references),
    }


def _unknown_ebml_size(width):
    if not 1 <= width <= 8:
        raise NativeClipUnsupported("Largeur de segment WebM invalide.")
    return bytes([0xFF >> (width - 1)]) + b"\xFF" * (width - 1)


class RangeReader:
    """HTTP range reader carrying yt-dlp's signed URL, headers and cookies."""

    def __init__(self, url, headers=None, cookie_file=None, timeout=20,
                 rate_limit=None):
        self.url = url
        self.headers = {
            str(name): str(value) for name, value in (headers or {}).items()
            if str(name).lower() not in {"range", "accept-encoding"}
        }
        self.timeout = timeout
        self.rate_limit = rate_limit or (lambda: 0)
        self._rate_started = time.monotonic()
        self._rate_bytes = 0
        self._last_rate = 0
        self.total_size = None
        self._response = None
        self._response_lock = threading.Lock()
        self.cookie_jar = None
        if cookie_file and os.path.isfile(cookie_file):
            jar = http.cookiejar.MozillaCookieJar()
            try:
                jar.load(cookie_file, ignore_discard=True, ignore_expires=True)
                self.cookie_jar = jar
            except (OSError, http.cookiejar.LoadError):
                pass
        self.opener = self._opener(ssl.create_default_context())

    def _opener(self, context):
        handlers = [urllib.request.HTTPSHandler(context=context)]
        if self.cookie_jar is not None:
            handlers.insert(0, urllib.request.HTTPCookieProcessor(self.cookie_jar))
        return urllib.request.build_opener(*handlers)

    def _open(self, request):
        log_event('range.request', url=self.url, byte_range=request.get_header('Range'), timeout=self.timeout)
        try:
            response = self.opener.open(request, timeout=self.timeout)
            log_event('range.response', status=getattr(response, 'status', None),
                      content_range=response.headers.get('Content-Range'))
            return response
        except urllib.error.HTTPError as error:
            log_event('range.http_error', code=error.code, error=str(error))
            # An HTTP refusal is not a TLS problem. Retrying it immediately with
            # certificate checks disabled only doubles the same CDN request and
            # can aggravate a 429 response.
            raise NativeClipUnsupported(
                f"YouTube a refusé le fragment demandé : {error}") from error
        except (urllib.error.URLError, OSError) as error:
            log_event('range.network_error', error=str(error))
            reason = getattr(error, "reason", error)
            tls_error = isinstance(reason, (ssl.SSLError, ssl.CertificateError))
            if str(self.url).startswith("https://") and tls_error:
                try:
                    return self._opener(ssl._create_unverified_context()).open(
                        request, timeout=self.timeout)
                except (urllib.error.URLError, OSError) as retry_error:
                    raise NativeClipUnsupported(
                        f"YouTube a refusé le fragment demandé : {retry_error}") from retry_error
            raise NativeClipUnsupported(f"Lecture réseau impossible : {error}") from error

    def cancel(self):
        """Interrupt an active CDN body read from the GUI thread."""
        log_event('range.cancel', url=self.url)
        with self._response_lock:
            response = self._response
            self._response = None
        if response is not None:
            try:
                response.close()
            except OSError:
                pass

    def _pace(self, size, cancelled):
        """Pace received bytes; a changed limit takes effect during the wait."""
        self._rate_bytes += size
        while True:
            if cancelled and cancelled():
                raise InterruptedError("Téléchargement annulé.")
            limit = max(0, int(self.rate_limit()))
            if limit != self._last_rate:
                self._last_rate = limit
                self._rate_started = time.monotonic()
                self._rate_bytes = size
            if not limit:
                self._rate_bytes = 0
                return
            remaining = self._rate_bytes / limit - (time.monotonic() - self._rate_started)
            if remaining <= 0:
                return
            time.sleep(min(remaining, 0.1))

    def read(self, start, end, progress=None, cancelled=None, advertise_to=None):
        if end < start:
            return b""
        first = int(start)
        wanted_end = int(end)
        current = first
        chunks = []
        while current <= wanted_end:
            if cancelled and cancelled():
                raise InterruptedError("Téléchargement annulé.")
            headers = dict(self.headers)
            # Ask for the normal remainder of the media stream. Google Video is
            # free to cap a response to one CDN block; when it does, continue at
            # the following byte instead of treating that normal split as a
            # truncated fragment.
            request_end = max(wanted_end, int(advertise_to or wanted_end))
            headers["Range"] = f"bytes={current}-{request_end}"
            request = urllib.request.Request(self.url, headers=headers)
            response = self._open(request)
            with self._response_lock:
                self._response = response
            try:
                with response:
                    status = getattr(response, "status", response.getcode())
                    content_range = response.headers.get("Content-Range", "")
                    match = re.match(
                        r"bytes\s+(\d+)-(\d+)/(\d+|\*)", content_range,
                        flags=re.IGNORECASE)
                    if status != 206 or not match or int(match.group(1)) != current:
                        raise NativeClipUnsupported(
                            "Le serveur n’a pas accepté la lecture partielle du flux.")
                    response_end = int(match.group(2))
                    if match.group(3) != "*":
                        self.total_size = int(match.group(3))
                    expected_now = min(wanted_end, response_end) - current + 1
                    if expected_now <= 0:
                        raise NativeClipUnsupported(
                            "YouTube a renvoyé une plage de données incohérente.")
                    received_now = 0
                    while received_now < expected_now:
                        if cancelled and cancelled():
                            raise InterruptedError("Téléchargement annulé.")
                        limit = max(0, int(self.rate_limit()))
                        block_size = (min(READ_BLOCK_BYTES, max(16 * 1024, 2 * limit))
                                      if limit else READ_BLOCK_BYTES)
                        block = response.read(min(
                            block_size, expected_now - received_now))
                        if not block:
                            break
                        chunks.append(block)
                        received_now += len(block)
                        current += len(block)
                        self._pace(len(block), cancelled)
                        if progress:
                            progress(current - first, wanted_end - first + 1)
                    if received_now != expected_now:
                        if cancelled and cancelled():
                            raise InterruptedError("Téléchargement annulé.")
                        raise NativeClipUnsupported(
                            f"Fragment réseau incomplet ({received_now}/{expected_now} octets).")
                    if cancelled and cancelled():
                        raise InterruptedError("Téléchargement annulé.")
                    if response_end < current - 1:
                        raise NativeClipUnsupported(
                            "YouTube a renvoyé une plage de données incohérente.")
            except (OSError, ValueError) as error:
                if cancelled and cancelled():
                    raise InterruptedError("Téléchargement annulé.") from error
                raise NativeClipUnsupported(
                    f"Lecture du fragment interrompue : {error}") from error
            finally:
                with self._response_lock:
                    if self._response is response:
                        self._response = None
            if current <= wanted_end and response_end >= wanted_end:
                raise NativeClipUnsupported(
                    "Le flux s’est interrompu avant la fin du fragment demandé.")
        log_event('range.read_complete', start=first, end=wanted_end, received=current-first)
        return b"".join(chunks)


def download_indexed_stream(
        stream, destination, clip_start, clip_end, *, cookie_file=None,
        progress=None, cancelled=None, reader_ready=None, rate_limit=None):
    """Download only indexed fragments for one MP4/M4A/WebM stream."""
    url = stream.get("url")
    extension = str(stream.get("ext") or "").lower()
    total_size = stream.get("filesize") or stream.get("filesize_approx")
    if not isinstance(url, str) or not url.startswith(("http://", "https://")):
        raise NativeClipUnsupported("Le flux sélectionné n’a pas d’URL directe.")
    if not isinstance(total_size, (int, float)) or total_size <= 0:
        raise NativeClipUnsupported("YouTube n’a pas fourni la taille du flux.")
    total_size = int(total_size)
    reader = RangeReader(url, stream.get("http_headers"), cookie_file,
                         rate_limit=rate_limit)
    if reader_ready:
        reader_ready(reader)
    head_end = min(total_size - 1, INITIAL_PROBE_BYTES - 1)
    head = reader.read(
        0, head_end, cancelled=cancelled, advertise_to=total_size - 1)
    if reader.total_size:
        total_size = reader.total_size

    if extension in {"mp4", "m4a", "m4v"}:
        try:
            index = parse_mp4_sidx(head)
        except NativeClipUnsupported as error:
            match = str(error)
            needed = None
            if "au moins " in match and " octets" in match:
                try:
                    needed = int(match.split("au moins ", 1)[1].split(" octets", 1)[0])
                except ValueError:
                    pass
            if not needed or needed > 8 * 1024 * 1024:
                raise
            head = reader.read(
                0, needed - 1, cancelled=cancelled,
                advertise_to=total_size - 1)
            index = parse_mp4_sidx(head)
        selected = select_segments(index.references, clip_start, clip_end)
        selected_start = selected[0].offset
        selected_end = selected[-1].offset + selected[-1].size - 1
        media = reader.read(
            selected_start, selected_end, progress=progress, cancelled=cancelled,
            advertise_to=total_size - 1)
        patched = rebuild_mp4_sidx(head, index, selected)
        gap = head[index.box_end:index.media_start]
        with open(destination, "wb") as target:
            target.write(head[:index.box_start])
            target.write(patched)
            target.write(gap)
            target.write(media)
        return selected[0].start, selected[-1].end, len(head) + len(media)

    if extension == "webm":
        tail_start = max(0, total_size - TAIL_PROBE_BYTES)
        tail = reader.read(tail_start, total_size - 1, cancelled=cancelled)
        parsed = parse_webm_index(head, tail, total_size)
        selected = select_segments(parsed["references"], clip_start, clip_end)
        selected_start = selected[0].offset
        selected_end = selected[-1].offset + selected[-1].size - 1
        media = reader.read(
            selected_start, selected_end, progress=progress, cancelled=cancelled,
            advertise_to=total_size - 1)
        header_end = parsed["header_end"]
        if header_end > len(head):
            raise NativeClipUnsupported("L’en-tête WebM dépasse la sonde initiale.")
        prefix = bytearray(head[:header_end])
        size_offset = parsed["size_offset"]
        size_width = parsed["size_width"]
        prefix[size_offset:size_offset + size_width] = _unknown_ebml_size(size_width)
        with open(destination, "wb") as target:
            target.write(prefix)
            target.write(media)
        return selected[0].start, selected[-1].end, len(head) + len(tail) + len(media)

    raise NativeClipUnsupported(
        f"Le conteneur {extension or 'inconnu'} ne fournit pas d’index exploitable.")
