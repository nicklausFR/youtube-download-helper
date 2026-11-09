"""Identify and validate a yt-dlp partial before reusing its bytes."""

import os
from pathlib import Path
import re
import time
import unicodedata
from urllib.request import Request, urlopen

from core_formats import estimated_bytes


def partial_name(path):
    """Split a final name or a yt-dlp merge component such as title.f137.mp4.part."""
    name = Path(path).name[:-len('.part')]
    stem, extension = os.path.splitext(name)
    match = re.fullmatch(r'(.+)\.f([0-9][\w-]*)', stem, re.ASCII)
    if not match:
        match = re.fullmatch(r'(.+) \[([0-9]{1,5}(?:-[\w-]+)?)\]', stem, re.ASCII)
    return (match[1] if match else stem, extension[1:].lower(),
            match[2] if match else None)


def matches_saved_download(path, job):
    _, folder, mode, selector, _, _, clip = job['request']
    if clip or mode == 'subtitles' or path.parent.resolve() != Path(folder).resolve():
        return False
    title, extension, format_id = partial_name(path)
    selection = job['selection']
    formats = [selection.get('video'), selection.get('audio'), *(selection.get('audios') or [])]
    formats = [item for item in formats if item and item.get('ext') == extension]
    if format_id is not None:
        formats = [item for item in formats if str(item.get('format_id')) == format_id]
    elif '+' in selector or ',' in selector:
        return False

    # yt-dlp substitutes punctuation that Windows does not allow in filenames.
    def comparable(value):
        normalized = unicodedata.normalize('NFKC', value).casefold()
        return ''.join(char for char in normalized if char.isalnum())

    return bool(formats and comparable(title) and comparable(title) == comparable(job['title']))


def estimate_saved_progress(job):
    """Estimate progress from matching partial files in a legacy checkpoint."""
    _, folder, mode, selector, _, _, clip = job['request']
    if clip or mode == 'subtitles' or not selector or not folder:
        return 0.0
    selection = job['selection']
    streams = [selection.get('video'), selection.get('audio'), *(selection.get('audios') or [])]
    expected = {
        str(stream['format_id']): estimated_bytes(stream, job['data'].get('duration'))
        for stream in streams if stream and stream.get('format_id') is not None
    }
    if not expected or not all(expected.values()):
        return 0.0
    received = {}
    try:
        for path in Path(folder).glob('*.part'):
            if not path.is_file() or not matches_saved_download(path, job):
                continue
            _, _, format_id = partial_name(path)
            format_id = format_id or selector
            if format_id in expected:
                received[format_id] = max(received.get(format_id, 0), path.stat().st_size)
    except OSError:
        pass
    fractions = [min(1, received.get(key, 0) / size) for key, size in expected.items()]
    return 99 * sum(fractions) / len(fractions)


def find_resumable_format(path, data, cancelled):
    """Match a direct stream against both ends of the already saved data."""
    _, extension, format_id = partial_name(path)
    size = path.stat().st_size
    sample_size = min(32 * 1024, size)
    with path.open('rb') as source:
        first = source.read(sample_size)
        source.seek(size - sample_size)
        last = source.read(sample_size)

    deadline = time.monotonic() + 20
    for item in reversed(data.get('formats') or []):
        if (item.get('ext') != extension or item.get('protocol') not in ('http', 'https')
                or not item.get('url') or (format_id and str(item.get('format_id')) != format_id)
                or (item.get('filesize') and item['filesize'] < size)):
            continue
        if cancelled() or time.monotonic() >= deadline:
            return None
        if (_matches_range(item, 0, first, size, deadline)
                and (size == sample_size
                     or _matches_range(item, size - sample_size, last, size, deadline))):
            return item
    return None


def _matches_range(item, start, expected, size, deadline):
    remaining = deadline - time.monotonic()
    if remaining <= 0:
        return False
    end = start + len(expected) - 1
    headers = {key: value for key, value in (item.get('http_headers') or {}).items()
               if key.lower() not in ('range', 'accept-encoding', 'cookie', 'authorization')}
    headers.update(Range=f'bytes={start}-{end}', **{'Accept-Encoding': 'identity'})
    try:
        with urlopen(Request(item['url'], headers=headers), timeout=min(3, remaining)) as response:
            content_range = re.fullmatch(r'bytes (\d+)-(\d+)/(\d+)', response.headers.get('Content-Range', ''))
            return (response.status == 206 and content_range is not None
                    and tuple(map(int, content_range.groups()[:2])) == (start, end)
                    and int(content_range[3]) >= size
                    and response.read(len(expected)) == expected)
    except (OSError, ValueError):
        return False
