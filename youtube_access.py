"""YouTube access policy and diagnostics, independent of Qt."""

import os
import re
from contextlib import contextmanager
from urllib.parse import parse_qs, urlparse
from automatic_session import provider_options
from verified_session import VerifiedCredentials
from windows_adapter import dependency_dir


ACCESS_FALLBACK_SEPARATOR = "\n\nAccès de secours :\n"
POT_VERSION_TIMEOUT_MESSAGE = "BgUtils n’a pas répondu à la vérification de sa version."


def is_youtube_url(value):
    try:
        u = urlparse(value.strip())
        host = (u.netloc or "").lower()
        return (
            u.scheme in ("http", "https")
            and (
                host == "youtu.be"
                or host.endswith(".youtube.com") or host == "youtube.com"
                or host.endswith(".youtube-nocookie.com") or host == "youtube-nocookie.com"
            )
        )
    except Exception:
        return False


def youtube_video_id(value):
    """Read the video ID from direct YouTube links without contacting YouTube."""
    if not is_youtube_url(value):
        return ""
    url = urlparse(value.strip())
    path = url.path.strip("/").split("/")
    if url.hostname == "youtu.be" and len(path) == 1:
        video_id = path[0]
    elif path == ["watch"]:
        video_id = parse_qs(url.query).get("v", [""])[0]
    elif len(path) == 2 and path[0] in ("shorts", "live", "embed", "v"):
        video_id = path[1]
    else:
        return ""
    return video_id if re.fullmatch(r"[A-Za-z0-9_-]{11}", video_id) else ""


def clean_ansi(text):
    ansi_escape = re.compile(r"\x1B(?:[@-Z\\-_]|\[[0-?]*[ -/]*[@-~])")
    return ansi_escape.sub("", text)


def extraction_issue(log):
    first_error, _, last_error = (log or "").rpartition(ACCESS_FALLBACK_SEPARATOR)
    if pot_provider_version_timed_out(last_error):
        return POT_VERSION_TIMEOUT_MESSAGE + " Cliquez sur l’état de l’analyse pour le diagnostic."
    low = last_error.lower()
    if "not a bot" in low or "anti-robot" in low:
        return "YouTube refuse l’accès avec une demande antirobot, malgré le module automatique. Réessayez plus tard."
    if "429" in low or "too many requests" in low:
        return "YouTube limite temporairement les requêtes (HTTP 429)."
    if any(term in low for term in ("no supported javascript", "signature solving failed", "nsig extraction failed", "challenge solving failed")):
        return "Le décodage JavaScript de YouTube a échoué : certains formats peuvent manquer. Cliquez sur l’état de l’analyse pour le diagnostic."
    if any(term in low for term in ("requires a po token", "require a po token", "po token was not provided", "missing po token", "sabr", "formats have been skipped", "formats may be missing")):
        return "Extraction partielle signalée par yt-dlp : certains formats peuvent manquer. Cliquez sur l’état de l’analyse pour le diagnostic."
    if "403" in low or "forbidden" in low:
        return "YouTube refuse l’accès au flux (HTTP 403) ; la cause antirobot n’est pas confirmée. Cliquez sur l’état de l’analyse pour le diagnostic."
    if pot_provider_version_timed_out(first_error):
        return (POT_VERSION_TIMEOUT_MESSAGE + " L’accès de secours a également échoué. "
                "Cliquez sur l’état de l’analyse pour le diagnostic.")
    return ""


def diagnostic_log(log):
    # Keep messages in memory, omitting URL queries and credential header values.
    log = clean_ansi(log or "")
    # Provider exceptions embed their complete command and large session context.
    log = re.sub(r"--innertube-context[^\r\n]*", "--innertube-context [masqué]", log)
    log = re.sub(r"https?://[^\s]+", lambda m: m.group(0).split("?")[0], log)
    log = re.sub(r"(?im)^(.*(?:cookie|authorization)\s*[:=]).*$", r"\1 [masqué]", log)
    return log[-12000:]


ACCESS_CHOICES = ("Automatique", "Sans session", "Session de vérification", "Brave")


def cookie_access_problem(error):
    low = error.lower()
    if "could not copy chrome cookie database" in low:
        return "locked"
    if "failed to decrypt" in low or "dpapi" in low:
        return "protected"
    if "could not find" in low and ("cookie" in low or "brave" in low):
        return "missing"
    if "failed to load cookies" in low:
        return "unavailable"
    return None


def cookie_access_failed(error):
    return cookie_access_problem(error) is not None


def access_args(session=False, analysis=False):
    args = ["--ignore-config", "--encoding", "utf-8", "--socket-timeout", "20",
            "--extractor-retries", "1", "--retries", "0" if analysis else "5",
            "--fragment-retries", "5", "--retry-sleep", "http:exp=1:8",
            "--retry-sleep", "fragment:exp=1:8", "--abort-on-unavailable-fragments"]
    args += ["--cookies-from-browser", "brave"] if session else ["--no-cookies-from-browser", "--no-cookies"]
    args += provider_options(os.path.join(dependency_dir(), "_pot"))
    return args


def readable_error(error):
    first_error, separator, final_error = error.rpartition(ACCESS_FALLBACK_SEPARATOR)
    if separator:
        cause = (POT_VERSION_TIMEOUT_MESSAGE if pot_provider_version_timed_out(first_error)
                 else "L’accès automatique a échoué.")
        return cause + " L’accès de secours a également échoué.\n\n" + readable_error(final_error)
    if format_access_unavailable(error):
        return "Le format sélectionné est indisponible."
    low=error.lower()
    if pot_provider_version_timed_out(error):
        return POT_VERSION_TIMEOUT_MESSAGE + "\n\n" + diagnostic_log(error)[-1000:]
    if pot_provider_failed(error):
        return ("Le générateur de jetons YouTube (BgUtils) n’a pas répondu ou a échoué.\n\n"
                "La génération du jeton n’a pas abouti. Réessayez plus tard.\n\n"
                + diagnostic_log(error)[-1000:])
    problem = cookie_access_problem(error)
    if problem == "locked":
        return ("yt-dlp ne peut pas copier la base de cookies de Brave. "
                "Brave peut encore être ouvert ou actif en arrière-plan.\n\n"
                "Quittez complètement Brave depuis son menu, puis cliquez sur Réessayer. "
                "Fermer un onglet ne suffit pas.\n\n"
                "Pour garder Brave ouvert, choisissez Sans session ; YouTube peut toutefois refuser cet accès.")
    if problem == "protected":
        return ("yt-dlp ne peut pas déchiffrer les cookies de Brave. "
                "Il s’agit d’un problème de protection des cookies ; fermer Brave peut ne pas suffire. "
                "Vous pouvez essayer Sans session.\n\nDétail : " + error[-600:])
    if problem == "missing":
        return "Le profil ou la base de cookies Brave est introuvable.\n\nDétail : " + error[-600:]
    if problem:
        return "La lecture des cookies Brave a échoué.\n\nDétail : " + error[-600:]
    if "not a bot" in low or "429" in low:
        return extraction_issue(error) + "\n\n" + diagnostic_log(error)[-1200:]
    return error[-1500:]


def needs_verification(error):
    if format_access_unavailable(error):
        return False
    low = error.lower()
    return any(text in low for text in ("not a bot", "anti-robot", "429", "youtube limite"))


def format_access_unavailable(error):
    """Identify the final yt-dlp format error independently of earlier warnings."""
    final_error = clean_ansi(error or "").rsplit("ERROR:", 1)[-1].lower()
    return "requested format is not available" in final_error


def without_forced_mweb(options):
    """Keep the same access stack but let yt-dlp choose another YouTube client."""
    result = []
    index = 0
    while index < len(options):
        if (
                options[index] == "--extractor-args"
                and index + 1 < len(options)
                and str(options[index + 1]).startswith("youtube:player_client=mweb")):
            index += 2
            continue
        result.append(options[index])
        index += 1
    return result


def alternate_youtube_options(options):
    """Prefer yt-dlp's current clients; keep mweb with tokens as a fallback."""
    result = without_forced_mweb(options)
    if result != list(options) or '--plugin-dirs' not in options:
        return result
    return [*result, '--extractor-args', 'youtube:player_client=mweb']


def pot_provider_version_timed_out(error):
    # BgUtils 2.0 lets this timeout escape before the token-generation handler.
    return bool(re.search(r"""\bgenerate_once\.js['"].*['"]--version['"].*\btimed out\b""",
                          clean_ansi(error or ""), re.IGNORECASE))


def pot_provider_failed(error):
    low = (error or "").lower()
    return ("_get_pot_via_script failed" in low
            or "failed while generating pot" in low
            or ("error fetching po token" in low and "bgutil" in low)
            or pot_provider_version_timed_out(error))


def without_pot_provider(options):
    """Keep runtime and credentials while bypassing a failed external provider."""
    options = without_forced_mweb(options)
    result = []
    index = 0
    while index < len(options):
        if (options[index] == "--plugin-dirs"
                or (options[index] == "--extractor-args" and index + 1 < len(options)
                    and str(options[index + 1]).startswith("youtubepot-bgutil"))):
            index += 2
            continue
        result.append(options[index])
        index += 1
    return [*result, "--no-plugin-dirs"]


def subtitle_retryable(error):
    low = (error or "").lower()
    return any(text in low for text in (
        "429", "too many requests", "403", "forbidden", "timed out",
        "timeout", "unable to download video subtitles", "http error 5"))


def transient_network_error(error):
    """Retry transport failures, without retrying access refusals or local errors."""
    # Earlier warnings can mention network failures even when the actual error
    # is a removed video, a full disk, or an unavailable format.
    final_error = clean_ansi(error or "").rsplit("ERROR:", 1)[-1].lower()
    if re.search(r"\b(?:http(?: error)?|status(?: code)?)\s*[:=]?\s*(?:403|404|429)\b", final_error):
        return False
    return bool(re.search(r"\bhttp(?: error)?\s*[:=]?\s*(?:408|500|502|503|504)\b", final_error)) or any(
        text in final_error for text in (
            "timed out", "timeout", "connection reset", "connection aborted",
            "remote end closed", "remote disconnected", "incomplete read", "incompleteread",
            "content too short", "bytes read", "temporary failure in name resolution",
            "failed to resolve", "getaddrinfo failed", "network is unreachable",
            "connection refused", "connection closed", "winerror 10054",
        ))


@contextmanager
def request_access_options(session=False, analysis=False):
    from strategies import DEFAULT_ACCESS
    with DEFAULT_ACCESS.options(session, analysis) as options:
        yield options
