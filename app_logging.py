"""Persistent, bounded diagnostics without cookies, tokens or signed media URLs."""
import atexit
from datetime import datetime
import json
import logging
from logging.handlers import RotatingFileHandler
import os
from pathlib import Path
import platform
import re
import subprocess
import sys
import threading
from urllib.parse import urlsplit, urlunsplit


_LOGGER = logging.getLogger('youtube_download_helper')
_LOG_PATH = None
_URL = re.compile(r'https?://[^\s<>\'"\]\)]+', re.I)
_SECRET_KEY = re.compile(
    r'cookie|authorization|(?:^|[_-])(?:token|secret|password)(?:$|[_-])|'
    r'potoken|visitor.?(?:data|id)|innertube.?context|sapisid|^__secure|'
    r'^(?:sid|hsid|ssid|apisid|sidcc|login_info)$', re.I)


def redact(text):
    text = str(text)
    text = re.sub(r'\x1b\[[0-?]*[ -/]*[@-~]', '', text)
    # Provider exceptions may include a whole command and session JSON.
    text = re.sub(r'(?i)--innertube-context[^\r\n]*', '--innertube-context [masqué]', text)
    text = re.sub(
        r'(?im)((?:[\w-]*(?:cookie|authorization|token|password|visitor[_-]?(?:data|id))[\w-]*|'
        r'sid|hsid|ssid|apisid|sidcc|login_info|__secure[\w-]*)[\'"\s]*[:=]\s*)[^\r\n]*',
        r'\1[masqué]', text)
    def url(match):
        try:
            value = urlsplit(match.group())
            host = value.hostname or ''
            netloc = host + (f':{value.port}' if value.port else '')
            path = '/[flux masqué]' if host == 'googlevideo.com' or host.endswith('.googlevideo.com') else value.path
            return urlunsplit((value.scheme, netloc, path, '', ''))
        except ValueError:
            return '[URL masquée]'
    return _URL.sub(url, text)


def safe_value(value, key=''):
    if _SECRET_KEY.search(str(key)):
        return '[masqué]'
    if isinstance(value, dict):
        return {str(k): safe_value(v, str(k)) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [safe_value(item) for item in value]
    if isinstance(value, Path):
        return str(value)
    if isinstance(value, str):
        return redact(value)
    if value is None or isinstance(value, (bool, int, float)):
        return value
    # In particular, never repr() a VerifiedCredentials object.
    return f'<{type(value).__name__}>'


def safe_command(command):
    result = []
    hide_next = False
    for value in command:
        value = str(value)
        if hide_next:
            result.append('[masqué]')
            hide_next = False
            continue
        option = value.split('=', 1)[0]
        if option in ('--cookies', '--add-header', '--password', '--username',
                      '--video-password', '--proxy', '--innertube-context'):
            result.append(option)
            hide_next = '=' not in value
            if '=' in value:
                result.append('[masqué]')
        elif _SECRET_KEY.search(value):
            result.append('[argument masqué]')
        else:
            result.append(redact(value))
    return result


class _SafeFormatter(logging.Formatter):
    def formatException(self, exc_info):
        return redact(super().formatException(exc_info))

    def format(self, record):
        text = super().format(record)
        # Structured fields were sanitized before JSON escaping. Re-sanitizing
        # an escaped multiline field would truncate everything after a header.
        return text if getattr(record, 'diagnostics_safe', False) else redact(text)


def log_path():
    return _LOG_PATH


def log_event(event, **fields):
    if _LOG_PATH is None:
        return
    try:
        _LOGGER.debug('%s %s', event, json.dumps(safe_value(fields), ensure_ascii=False),
                      extra={'diagnostics_safe': True})
    except Exception:
        # Diagnostics must never stop an analysis or a transfer.
        pass


def log_exception(event, **fields):
    if _LOG_PATH is not None:
        _LOGGER.exception('%s %s', event, json.dumps(safe_value(fields), ensure_ascii=False),
                          extra={'diagnostics_safe': True})


def setup_logging(base=None):
    global _LOG_PATH
    if _LOG_PATH is not None:
        return _LOG_PATH
    base = Path(base) if base else Path(sys.executable if getattr(sys, 'frozen', False) else __file__).parent
    fallback = Path(os.environ.get('LOCALAPPDATA', Path.home())) / 'YouTubeDownloadHelper'
    for root in (base, fallback):
        try:
            folder = root / 'logs'
            folder.mkdir(parents=True, exist_ok=True)
            stamp = datetime.now().strftime('%Y%m%d-%H%M%S-%f')
            path = folder / f'youtube-download-helper-{stamp}-{os.getpid()}.log'
            handler = RotatingFileHandler(path, maxBytes=5 * 1024 * 1024,
                                          backupCount=2, encoding='utf-8')
            break
        except OSError:
            continue
    else:
        return None
    handler.setFormatter(_SafeFormatter(
        '%(asctime)s.%(msecs)03d %(levelname)-7s pid=%(process)d thread=%(threadName)s '
        '%(name)s %(message)s', datefmt='%Y-%m-%d %H:%M:%S'))
    handler.addFilter(lambda record: record.name.startswith('youtube_download_helper')
                      or record.levelno >= logging.WARNING)
    _LOGGER.setLevel(logging.DEBUG)
    logging.getLogger().addHandler(handler)
    logging.raiseExceptions = False
    _LOG_PATH = path
    # Keep the ten most recent launches and their rolled files.
    sessions = sorted(folder.glob('youtube-download-helper-*.log'), reverse=True)
    for old in sessions[10:]:
        for item in (old, Path(str(old) + '.1'), Path(str(old) + '.2')):
            try:
                item.unlink(missing_ok=True)
            except OSError:
                pass
    original_exception = sys.excepthook
    original_thread_exception = threading.excepthook
    def exception_hook(kind, value, traceback):
        _LOGGER.critical('Unhandled exception', exc_info=(kind, value, traceback))
        original_exception(kind, value, traceback)
    def thread_exception_hook(args):
        _LOGGER.critical('Unhandled worker exception in %s', args.thread.name,
                         exc_info=(args.exc_type, args.exc_value, args.exc_traceback))
        original_thread_exception(args)
    sys.excepthook = exception_hook
    threading.excepthook = thread_exception_hook
    atexit.register(lambda: log_event('application.exit'))
    log_event('application.start', executable=sys.executable, frozen=bool(getattr(sys, 'frozen', False)),
              python=platform.python_version(), platform=platform.platform(), cwd=os.getcwd(),
              application_dir=base, argv=safe_command(sys.argv), log_file=path)
    return path


def verbose_options(options):
    return [*options, '--verbose'] if _LOG_PATH is not None and '--verbose' not in options else list(options)


def start_process(command, **kwargs):
    log_event('process.start', command=safe_command(command), cwd=kwargs.get('cwd'))
    try:
        process = subprocess.Popen(command, **kwargs)
    except Exception:
        log_exception('process.start_failed', command=safe_command(command))
        raise
    log_event('process.started', pid=process.pid, executable=str(command[0]))
    return process


def process_result(process, *, output=None, phase='', timeout=False):
    log_event('process.result', phase=phase, pid=process.pid, returncode=process.returncode,
              timeout=timeout, output=output)


def catalog_summary(data):
    formats = data.get('formats') or []
    return dict(id=data.get('id'), title=data.get('title'), duration=data.get('duration'),
                channel=data.get('channel'), live=data.get('is_live'), format_count=len(formats),
                formats=[{k: f.get(k) for k in ('format_id', 'height', 'fps', 'vcodec', 'acodec',
                          'language', 'protocol', 'filesize', 'filesize_approx')} for f in formats],
                subtitles=list((data.get('subtitles') or {}).keys()),
                automatic_captions=list((data.get('automatic_captions') or {}).keys()),
                access_recovery=data.get('_downloader_access_recovery'))


def engine_event(engine, event):
    if _LOG_PATH is None:
        return
    values = event[1:]
    if event[0] == 'analysis_ok':
        values = (event[1], catalog_summary(event[2]))
    elif event[0] == 'download_formats_changed':
        values = (catalog_summary(event[1]) if event[1] else None, event[2])
    elif event[0] == 'keyframe_ready':
        values = ('keyframe index ready',)
    log_event('engine.event', engine=hex(id(engine)), kind=event[0], values=values)
