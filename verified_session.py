"""Small, isolated browser window for user-completed YouTube verification."""
from contextlib import contextmanager
from dataclasses import dataclass, field
from http.cookiejar import Cookie, MozillaCookieJar
from pathlib import Path
import json, os, shutil, subprocess, sys, tempfile, time
from urllib.parse import urlparse
import urllib.request

sys.path.insert(0, str(Path(__file__).parent / "_downloader_vendor"))
import websocket

YOUTUBE_HOSTS = ("youtube.com", "google.com")
def allowed_cookie_domain(domain):
    host = domain.lstrip(".").lower()
    return any(host == base or host.endswith("." + base) for base in YOUTUBE_HOSTS)

def valid_youtube_url(url):
    u = urlparse(url)
    return u.scheme == "https" and (u.hostname in ("youtu.be", "youtube.com", "www.youtube.com", "m.youtube.com"))

@dataclass(repr=False)
class VerifiedCredentials:
    cookies: list = field(repr=False)
    user_agent: str = ""
    created: float = field(default_factory=time.monotonic)

    @contextmanager
    def cookie_file(self):
        fd, name = tempfile.mkstemp(prefix="youtube-downloader-session-", suffix=".txt")
        os.close(fd)
        jar = MozillaCookieJar(name)
        try:
            for c in self.cookies:
                domain = str(c.get("domain", ""))
                if not allowed_cookie_domain(domain):
                    continue
                values = [domain, str(c.get("name", "")), str(c.get("value", "")), str(c.get("path", "/"))]
                if any("\n" in value or "\r" in value or "\t" in value for value in values):
                    continue
                expires = c.get("expires", -1)
                expires = int(expires) if isinstance(expires, (int, float)) and expires > 0 else None
                jar.set_cookie(Cookie(0, values[1], values[2], None, False, domain,
                    domain.startswith("."), domain.startswith("."), values[3], True,
                    bool(c.get("secure")), expires, expires is None, None, None,
                    {"HTTPOnly": ""} if c.get("httpOnly") else {}, False))
            jar.save(ignore_discard=True, ignore_expires=True)
            yield name
        finally:
            try: os.unlink(name)
            except FileNotFoundError: pass

class BrowserClosed(Exception):
    pass

class VerificationBrowser:
    def __init__(self, profile=None):
        self.base = Path(tempfile.gettempdir()).resolve()
        self.profile = Path(profile) if profile else Path(tempfile.mkdtemp(prefix="youtube-downloader-browser-"))
        self.owns_profile = profile is None
        self.proc = None
        self.ws = None
        self.counter = 0
        self.session_id = None

    @staticmethod
    def executable():
        candidates = [
            Path(os.environ.get("ProgramFiles(x86)", "C:/Program Files (x86)")) / "Microsoft/Edge/Application/msedge.exe",
            Path(os.environ.get("ProgramFiles", "C:/Program Files")) / "Microsoft/Edge/Application/msedge.exe",
            Path(os.environ.get("ProgramFiles", "C:/Program Files")) / "BraveSoftware/Brave-Browser/Application/brave.exe",
        ]
        return next((str(p) for p in candidates if p.is_file()), None)

    def launch(self, url, headless=False, allow_local_test=False):
        if not valid_youtube_url(url) and not (allow_local_test and urlparse(url).hostname == "127.0.0.1"):
            raise ValueError("Cette fenêtre accepte uniquement les liens YouTube HTTPS.")
        exe = self.executable()
        if not exe:
            raise RuntimeError("Edge ou Brave est nécessaire pour ouvrir la fenêtre de vérification.")
        self.profile.mkdir(parents=True, exist_ok=True)
        active = self.profile / "DevToolsActivePort"
        if active.exists(): active.unlink()
        cmd = [exe, "--user-data-dir=" + str(self.profile), "--remote-debugging-port=0",
               "--remote-debugging-address=127.0.0.1", "--no-first-run", "--no-default-browser-check",
               "--disable-background-mode", "--window-size=720,640",
               "--app=" + url]
        if headless:
            cmd = [arg for arg in cmd if not arg.startswith("--app=")] + ["--headless=new", url]
        self.proc = subprocess.Popen(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                                     creationflags=0 if not headless else 0x08000000)
        deadline = time.monotonic() + 20
        while time.monotonic() < deadline:
            if active.is_file():
                lines = active.read_text().splitlines()
                if len(lines) >= 2:
                    self.ws = websocket.create_connection("ws://127.0.0.1:" + lines[0] + lines[1],
                        timeout=5, suppress_origin=True, http_no_proxy=["127.0.0.1", "localhost"])
                    break
            time.sleep(.1)
        if not self.ws:
            raise RuntimeError("La fenêtre de vérification n’a pas démarré.")
        for _ in range(40):
            targets = self.call("Target.getTargets").get("targetInfos", [])
            target = next((t for t in targets if t.get("type") == "page" and (valid_youtube_url(t.get("url","")) or allow_local_test)), None)
            if target:
                self.session_id = self.call("Target.attachToTarget", {"targetId": target["targetId"], "flatten": True})["sessionId"]
                return
            time.sleep(.1)
        raise RuntimeError("La page YouTube n’est pas disponible dans la fenêtre.")

    def call(self, method, params=None, page=False):
        self.counter += 1
        message = {"id": self.counter, "method": method, "params": params or {}}
        if page: message["sessionId"] = self.session_id
        self.ws.send(json.dumps(message))
        while True:
            raw = self.ws.recv()
            if not raw: raise BrowserClosed("La fenêtre a été fermée.")
            result = json.loads(raw)
            if result.get("id") == self.counter:
                if "error" in result: raise RuntimeError(result["error"].get("message", "Erreur navigateur"))
                return result.get("result", {})

    def credentials(self):
        result = self.call("Network.getCookies", {"urls": [
            "https://www.youtube.com/", "https://youtube.com/", "https://accounts.google.com/"]}, page=True)
        agent = self.call("Browser.getVersion").get("userAgent", "")
        return VerifiedCredentials([c for c in result.get("cookies", []) if allowed_cookie_domain(c.get("domain",""))], agent)

    def close(self):
        if self.ws:
            try: self.call("Browser.close")
            except Exception: pass
            try: self.ws.close()
            except Exception: pass
            self.ws = None
        if self.proc:
            try: self.proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                # Only the process started for our isolated profile.
                self.proc.terminate()
                try: self.proc.wait(timeout=3)
                except subprocess.TimeoutExpired: pass
        if self.owns_profile:
            resolved = self.profile.resolve()
            if resolved.parent == self.base and resolved.name.startswith("youtube-downloader-browser-"):
                for _ in range(30):
                    try:
                        shutil.rmtree(resolved)
                        break
                    except FileNotFoundError:
                        break
                    except OSError:
                        time.sleep(.1)

    def verify(self, url, cancel, force_continue, notify):
        try:
            self.launch(url)
            notify("Vérification YouTube : effectuez la vérification ou la connexion dans la petite fenêtre.")
            started = time.monotonic()
            ready_since = None
            while time.monotonic() - started < 600:
                if cancel.wait(.8): raise BrowserClosed("Vérification annulée.")
                result = self.call("Runtime.evaluate", {"expression": READY_SCRIPT, "returnByValue": True}, page=True)
                state = result.get("result", {}).get("value") or {}
                ready = state.get("ready") or state.get("confirmed") or force_continue.is_set()
                if ready:
                    ready_since = ready_since or time.monotonic()
                    if state.get("confirmed") or force_continue.is_set() or time.monotonic() - ready_since >= 6:
                        return self.credentials()
                else:
                    ready_since = None
            raise BrowserClosed("Vérification expirée. Vous pouvez rouvrir la fenêtre.")
        finally:
            self.close()

# This only observes page readiness and provides a user-operated Continue button.
# It never clicks a challenge, enters credentials, or solves a CAPTCHA.
READY_SCRIPT = r"""(() => {
 const host=location.hostname;
 if (!(host==='youtube.com'||host.endsWith('.youtube.com'))) return {ready:false};
 if (!document.getElementById('downloader-verification-bar') && document.body) {
   const bar=document.createElement('div'); bar.id='downloader-verification-bar';
   bar.style.cssText='position:fixed;bottom:10px;left:10px;right:10px;z-index:2147483647;background:#171717;color:white;padding:10px 12px;border-radius:9px;font:13px system-ui;box-shadow:0 4px 18px #0007;display:flex;align-items:center;gap:10px';
   const text=document.createElement('span');
   text.textContent='Terminez le contrôle Google. La reprise sera automatique.';
   const button=document.createElement('button'); button.textContent='Reprendre';
   button.style.cssText='padding:8px 12px;cursor:pointer;background:#ff0033;color:white;border:0;border-radius:7px;font-weight:600';
   button.onclick=()=>{window.__downloaderContinue=true};
   bar.append(text,button);document.body.append(bar);
 }
 const video=document.querySelector('video');
 return {ready:!!video && video.readyState>=2 && !video.error, confirmed:window.__downloaderContinue===true};
})()"""
