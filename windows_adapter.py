"""Replaceable Windows process and FFprobe adapter."""

import json
import os
import re
import shutil
import subprocess
import sys
from app_logging import log_event

CREATE_NO_WINDOW = 0x08000000 if os.name == "nt" else 0


def shutdown_windows():
    executable = os.path.join(os.environ.get("SystemRoot", r"C:\Windows"), "System32", "shutdown.exe")
    # A Windows timeout greater than zero would imply forced application closure.
    subprocess.run([executable, "/s", "/t", "0"], check=True, timeout=10,
                   capture_output=True, creationflags=CREATE_NO_WINDOW)


def terminate_process_tree(process):
    """Stop the downloader and any FFmpeg/Node child it launched."""
    if process is None or process.poll() is not None:
        return
    log_event('process.terminate_tree', pid=process.pid)
    if os.name == "nt":
        try:
            subprocess.run(
                ["taskkill.exe", "/PID", str(process.pid), "/T", "/F"],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                creationflags=CREATE_NO_WINDOW,
                timeout=8,
            )
        except Exception:
            pass
    if process.poll() is None:
        try:
            process.kill()
        except OSError:
            pass


def app_dir():
    if getattr(sys, "frozen", False):
        return os.path.dirname(sys.executable)
    return os.path.dirname(os.path.abspath(__file__))


def dependency_dir():
    return os.path.join(app_dir(), "_dependencies")


def find_executable(names):
    base = dependency_dir()

    for name in names:
        local = os.path.join(base, name)
        if os.path.isfile(local):
            return local

    for name in names:
        scripts = os.path.join(os.path.dirname(sys.executable), "Scripts", name)
        if os.path.isfile(scripts): return scripts
        found = shutil.which(name)
        if found:
            return found

    return None


def ffprobe_for(ffmpeg):
    """Locate the probe shipped beside FFmpeg, then a configured fallback."""
    name = "ffprobe.exe" if os.name == "nt" else "ffprobe"
    if ffmpeg:
        sibling = os.path.join(os.path.dirname(ffmpeg), name)
        if os.path.isfile(sibling):
            return sibling
    return find_executable(["ffprobe.exe", "ffprobe"])


def _first_video_frame_time(ffprobe, path):
    """Return the first frame yt-dlp/FFmpeg actually managed to decode."""
    completed = subprocess.run(
        [
            ffprobe, "-v", "error", "-select_streams", "v:0",
            "-read_intervals", "%+60", "-show_entries",
            "frame=best_effort_timestamp_time", "-of", "json", path,
        ],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        creationflags=CREATE_NO_WINDOW,
    )
    if completed.returncode != 0:
        raise RuntimeError((completed.stderr or "FFprobe n’a pas pu lire l’extrait.").strip())
    frames = (json.loads(completed.stdout or "{}").get("frames") or [])
    for frame in frames:
        value = frame.get("best_effort_timestamp_time")
        if value is not None:
            return max(0.0, float(value))
    raise RuntimeError("Aucune image vidéo décodable dans les 60 premières secondes de l’extrait.")


def _video_keyframe_times(ffprobe, path, start=None, end=None, headers=None, timeout=45):
    """Read keyframe packet timestamps without decoding or encoding the video."""
    command = [
        ffprobe, "-v", "error", "-select_streams", "v:0",
    ]
    if str(path).startswith("https://"):
        # The portable FFmpeg build has no access to Windows' certificate store.
        # The URL itself is short-lived and signed by YouTube; this option is only
        # used for the lightweight, read-only keyframe index request.
        command += ["-tls_verify", "0"]
    if headers:
        safe_headers = []
        for name, value in headers.items():
            if re.fullmatch(r"[A-Za-z0-9-]+", str(name)) and not any(
                    char in str(value) for char in "\r\n"):
                safe_headers.append(f"{name}: {value}")
        if safe_headers:
            command += ["-headers", "\r\n".join(safe_headers) + "\r\n"]
    if start is not None or end is not None:
        interval_start = max(0.0, float(start or 0) - 15.0)
        interval_end = "" if end is None else f"{float(end) + 15.0:.6f}"
        command += ["-read_intervals", f"{interval_start:.6f}%{interval_end}"]
    command += [
        "-show_packets", "-show_entries", "packet=pts_time,flags",
        "-of", "json", path,
    ]
    try:
        completed = subprocess.run(
            command, capture_output=True, text=True, encoding="utf-8",
            errors="replace", creationflags=CREATE_NO_WINDOW, timeout=timeout,
        )
    except subprocess.TimeoutExpired as error:
        raise RuntimeError("Le repérage des images clés a dépassé 45 secondes.") from error
    if completed.returncode != 0:
        raise RuntimeError((completed.stderr or "FFprobe n’a pas pu repérer les images clés.").strip())
    packets = json.loads(completed.stdout or "{}").get("packets") or []
    keyframes = []
    for packet in packets:
        if "K" not in str(packet.get("flags") or ""):
            continue
        try:
            value = float(packet["pts_time"])
        except (KeyError, TypeError, ValueError):
            continue
        if value >= 0:
            keyframes.append(value)
    keyframes = sorted(set(keyframes))
    if not keyframes:
        raise RuntimeError("Aucune image clé exploitable n’a été trouvée.")
    return keyframes


def _media_streams(ffprobe, path):
    completed = subprocess.run(
        [ffprobe, "-v", "error", "-show_entries", "stream=codec_type,codec_name",
         "-of", "json", path],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        creationflags=CREATE_NO_WINDOW,
    )
    if completed.returncode != 0:
        raise RuntimeError((completed.stderr or "FFprobe n’a pas pu lire les pistes.").strip())
    return json.loads(completed.stdout or "{}").get("streams") or []
