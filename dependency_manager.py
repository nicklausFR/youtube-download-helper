"""Checks and local, verified repairs for downloader dependencies (stdlib only)."""
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from pathlib import Path
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import urllib.request
import uuid
import zipfile

NODE_VERSION = 'v24.21.0'
PROVIDER_VERSION = '2.0.0'
PROVIDER_REPO = 'https://github.com/Brainicism/bgutil-ytdlp-pot-provider'
FLAGS = subprocess.CREATE_NO_WINDOW if os.name == 'nt' else 0


@dataclass
class Dependency:
    key: str
    name: str
    ok: bool
    detail: str
    repairable: bool = True
    path: str = ''


class DependencyManager:
    names = {'python': 'Python et interface', 'ytdlp': 'Téléchargement (yt-dlp)',
             'ffmpeg': 'Fusion et sous-titres (FFmpeg)', 'node': 'Moteur JavaScript (Node)',
             'provider': 'Accès automatique (BgUtils)', 'plugin': 'Connexion BgUtils / yt-dlp',
             'images': 'Miniatures (Qt)'}

    def __init__(self, base, report=None):
        self.base = Path(base).resolve()
        self.report = report or (lambda message: None)
        self.cancel = threading.Event()

    def executable(self, name):
        candidates = [self.base / name, Path(sys.executable).parent / 'Scripts' / name]
        found = shutil.which(name)
        if found: candidates.append(Path(found))
        return next((str(p) for p in candidates if p.is_file()), None)

    def run(self, args, timeout=45, cwd=None, env=None):
        if self.cancel.is_set(): raise RuntimeError('Opération annulée')
        proc = subprocess.Popen([str(a) for a in args], stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
            stdin=subprocess.DEVNULL, text=True, encoding='utf-8', errors='replace',
            creationflags=FLAGS, cwd=cwd, env=env)
        deadline = time.monotonic() + timeout
        try:
            while True:
                try:
                    output, _ = proc.communicate(timeout=0.3)
                    break
                except subprocess.TimeoutExpired:
                    if self.cancel.is_set() or time.monotonic() > deadline:
                        raise RuntimeError('Opération annulée' if self.cancel.is_set() else 'Le composant ne répond pas dans le délai prévu')
            if proc.returncode:
                raise RuntimeError(output.strip()[-1800:] or f'Échec du composant ({proc.returncode})')
            return output.strip()
        finally:
            if proc.poll() is None:
                if os.name == 'nt':
                    subprocess.run(['taskkill', '/PID', str(proc.pid), '/T', '/F'], stdout=subprocess.DEVNULL,
                                   stderr=subprocess.DEVNULL, creationflags=FLAGS, timeout=10)
                else: proc.kill()
                proc.wait(timeout=10)
            if proc.stdout: proc.stdout.close()

    def check(self, key):
        name = self.names[key]
        try:
            path = ''
            if key == 'python':
                from PySide6 import __version__ as qt_version
                from PySide6.QtWidgets import QWidget
                return Dependency(key, name, True, f'{sys.version.split()[0]} · PySide6 {qt_version}', False, sys.executable)
            if key in ('ytdlp', 'ffmpeg'):
                path = self.executable('yt-dlp.exe' if key == 'ytdlp' else 'ffmpeg.exe')
                if not path: raise RuntimeError('Absent')
                output = self.run([path, '--version' if key == 'ytdlp' else '-version'], timeout=10)
                if key == 'ytdlp' and output.splitlines()[0] < '2025.05.22':
                    raise RuntimeError('Version trop ancienne pour les jetons automatiques')
                detail = output.splitlines()[0]
            elif key == 'node':
                path = self.base / '_pot/runtime/node.exe'
                if not path.is_file(): raise RuntimeError('Absent')
                detail = self.run([path, '--version'], timeout=10)
                if tuple(map(int, detail.lstrip('v').split('.')[:2])) < (24, 0):
                    raise RuntimeError('Node 24 ou supérieur requis pour cette version du générateur')
            elif key == 'provider':
                root = self.base / '_pot'
                path = root / 'build/generate_once.js'
                if not path.is_file(): raise RuntimeError('Générateur absent')
                if not (root / 'runtime/node.exe').is_file(): raise RuntimeError('Vérification impossible : moteur Node absent')
                env = os.environ.copy()
                with tempfile.TemporaryDirectory(prefix='yd-check-') as cache:
                    env['XDG_CACHE_HOME'] = cache
                    detail = self.run([root / 'runtime/node.exe', path, '--version'], env=env)
                if detail != PROVIDER_VERSION: raise RuntimeError('Version du générateur incompatible avec le connecteur')
            elif key == 'plugin':
                path = self.base / '_pot/plugins/bgutil-ytdlp-pot-provider.zip'
                if not path.is_file(): raise RuntimeError('Connecteur absent')
                with zipfile.ZipFile(path) as archive:
                    if archive.testzip(): raise RuntimeError('Archive du connecteur endommagée')
                    source = archive.read('yt_dlp_plugins/extractor/getpot_bgutil.py').decode()
                    if not re.search(r"__version__\s*=\s*['\"]2\.0\.0['\"]", source):
                        raise RuntimeError('Version du connecteur incompatible')
                detail = PROVIDER_VERSION
            elif key == 'images':
                from PySide6.QtGui import QImage
                image = QImage(1, 1, QImage.Format.Format_RGB32)
                if image.isNull(): raise RuntimeError('Chargement des images Qt impossible')
                from PySide6.QtCore import QByteArray, QBuffer, QIODevice
                image.fill(0xff205080)
                for image_format in ('PNG', 'JPEG'):
                    encoded = QByteArray()
                    buffer = QBuffer(encoded); buffer.open(QIODevice.OpenModeFlag.WriteOnly)
                    if not image.save(buffer, image_format) or QImage.fromData(encoded).isNull():
                        raise RuntimeError('Composant image Qt absent : ' + image_format)
                detail = 'PNG et JPEG intégrés à Qt'
            else:
                raise ValueError(key)
            return Dependency(key, name, True, detail, path=str(path))
        except Exception as error:
            return Dependency(key, name, False, str(error), key not in ('python', 'images'))


    def check_all(self):
        with ThreadPoolExecutor(max_workers=4) as pool:
            return list(pool.map(self.check, self.names))

    def get_json(self, url):
        request = urllib.request.Request(url, headers={'User-Agent': 'YouTube-Downloader-dependencies'})
        with urllib.request.urlopen(request, timeout=30) as response:
            return json.load(response)

    def get_text(self, url):
        request = urllib.request.Request(url, headers={'User-Agent': 'YouTube-Downloader-dependencies'})
        with urllib.request.urlopen(request, timeout=30) as response:
            return response.read().decode('utf-8').strip()

    def available_updates(self):
        """Return lightweight upstream updates for independently updated tools."""
        updates = {}
        errors = []
        try:
            path = self.executable('yt-dlp.exe')
            if path:
                installed = self.run([path, '--version'], timeout=10).splitlines()[0].strip()
                release = self.get_json('https://api.github.com/repos/yt-dlp/yt-dlp/releases/latest')
                available = str(release.get('tag_name') or release.get('name') or '').lstrip('v')
                if available and installed != available:
                    updates['ytdlp'] = (installed, available)
        except Exception as error:
            errors.append('yt-dlp : ' + str(error))
        try:
            path = self.executable('ffmpeg.exe')
            if path:
                installed_line = self.run([path, '-version'], timeout=10).splitlines()[0]
                installed = installed_line.removeprefix('ffmpeg version ').split()[0]
                available = self.get_text(
                    'https://www.gyan.dev/ffmpeg/builds/ffmpeg-release-essentials.zip.ver')
                if available and available not in installed:
                    updates['ffmpeg'] = (installed, available)
        except Exception as error:
            errors.append('FFmpeg : ' + str(error))
        return updates, errors

    def download(self, url, path, expected=None):
        if not url.startswith('https://'): raise ValueError('Téléchargement HTTPS requis')
        digest = hashlib.sha256()
        request = urllib.request.Request(url, headers={'User-Agent': 'YouTube-Downloader-dependencies'})
        self.report('Téléchargement…')
        last = time.monotonic()
        downloaded = 0
        with urllib.request.urlopen(request, timeout=30) as response, open(path, 'wb') as output:
            total = int(response.headers.get('Content-Length') or 0)
            while chunk := response.read(256 * 1024):
                if self.cancel.is_set(): raise RuntimeError('Installation annulée')
                output.write(chunk); digest.update(chunk); downloaded += len(chunk)
                if time.monotonic() - last >= 0.5:
                    self.report(f'Téléchargement : {downloaded / 1048576:.1f} Mo' + (f' / {total / 1048576:.1f} Mo' if total else ''))
                    last = time.monotonic()
        if expected and digest.hexdigest().lower() != expected.lower():
            raise RuntimeError('Somme SHA256 incorrecte : le fichier téléchargé ne sera pas installé')
        return Path(path)

    @staticmethod
    def extract(archive, destination):
        destination = Path(destination).resolve()
        with zipfile.ZipFile(archive) as z:
            for member in z.infolist():
                path = (destination / member.filename).resolve()
                if not path.is_relative_to(destination) or ':' in member.filename or '\\' in member.filename:
                    raise RuntimeError('Chemin non sûr dans l’archive')
                if (member.external_attr >> 16) & 0o170000 == 0o120000:
                    raise RuntimeError('Lien symbolique non autorisé dans l’archive')
            z.extractall(destination)

    def commit(self, source, relative):
        """Replace a single dependency with rollback; keep its old copy as backup."""
        target = (self.base / relative).resolve()
        if not target.is_relative_to(self.base) or target == self.base:
            raise RuntimeError('Destination de réparation non autorisée')
        target.parent.mkdir(parents=True, exist_ok=True)
        incoming = target.with_name(target.name + '.new-' + uuid.uuid4().hex[:8])
        source = Path(source)
        if source.is_dir(): shutil.copytree(source, incoming)
        else: shutil.copy2(source, incoming)
        backup = self.base / '_dependency_backups' / (target.name + '-' + uuid.uuid4().hex[:8])
        backup.parent.mkdir(parents=True, exist_ok=True)
        moved = False
        try:
            if target.exists(): target.rename(backup); moved = True
            incoming.rename(target)
        except Exception:
            if moved and not target.exists(): backup.rename(target)
            raise

    def install_ytdlp(self, work):
        release = self.get_json('https://api.github.com/repos/yt-dlp/yt-dlp/releases/latest')
        asset = next(a for a in release['assets'] if a['name'] == 'yt-dlp.exe')
        checksum = asset.get('digest', '').removeprefix('sha256:')
        if not re.fullmatch('[0-9a-fA-F]{64}', checksum):
            sums = next(a for a in release['assets'] if a['name'] == 'SHA2-256SUMS')
            sums_path = self.download(sums['browser_download_url'], work / 'sums.txt')
            checksum = next(line.split()[0] for line in sums_path.read_text().splitlines() if line.split()[-1] == 'yt-dlp.exe')
        exe = self.download(asset['browser_download_url'], work / 'yt-dlp.exe', checksum)
        self.run([exe, '--version'], timeout=10)
        self.commit(exe, 'yt-dlp.exe')

    def install_ffmpeg(self, work):
        base = 'https://www.gyan.dev/ffmpeg/builds/ffmpeg-release-essentials.zip'
        sums = self.download(base + '.sha256', work / 'ffmpeg.sha256').read_text()
        checksum = re.search(r'\b[0-9a-fA-F]{64}\b', sums).group(0)
        archive = self.download(base, work / 'ffmpeg.zip', checksum)
        unpack = work / 'ffmpeg'; self.extract(archive, unpack)
        for name in ('ffmpeg.exe', 'ffprobe.exe'):
            exe = next(unpack.rglob(name))
            self.run([exe, '-version'], timeout=10)
            self.commit(exe, name)
        for license_file in unpack.rglob('LICENSE*'):
            if license_file.is_file(): self.commit(license_file, '_licenses/ffmpeg-' + license_file.name)

    def install_node(self, work):
        name = f'node-{NODE_VERSION}-win-x64.zip'
        base = f'https://nodejs.org/dist/{NODE_VERSION}/'
        sums = self.download(base + 'SHASUMS256.txt', work / 'node-shasums.txt').read_text()
        checksum = next(line.split()[0] for line in sums.splitlines() if line.split()[-1] == name)
        archive = self.download(base + name, work / 'node.zip', checksum)
        unpack = work / 'node'; self.extract(archive, unpack)
        runtime = unpack / f'node-{NODE_VERSION}-win-x64'
        self.run([runtime / 'node.exe', '--version'], timeout=10)
        self.commit(runtime, '_pot/runtime')

    def install_plugin(self, work):
        release = self.get_json('https://api.github.com/repos/Brainicism/bgutil-ytdlp-pot-provider/releases/tags/' + PROVIDER_VERSION)
        asset = next(a for a in release['assets'] if a['name'] == 'bgutil-ytdlp-pot-provider.zip')
        checksum = asset.get('digest', '').removeprefix('sha256:')
        if not re.fullmatch('[0-9a-fA-F]{64}', checksum): raise RuntimeError('Empreinte du connecteur absente de la publication')
        archive = self.download(asset['browser_download_url'], work / asset['name'], checksum)
        with zipfile.ZipFile(archive) as z:
            if z.testzip(): raise RuntimeError('Connecteur endommagé')
        self.commit(archive, '_pot/plugins/' + asset['name'])

    def install_provider(self, work):
        runtime = self.base / '_pot/runtime'
        npm = runtime / 'node_modules/npm/bin/npm-cli.js'
        if not self.check('node').ok or not npm.is_file():
            self.report('Préparation de Node et de son installateur de modules…')
            self.install_node(work)
        archive = self.download('https://codeload.github.com/Brainicism/bgutil-ytdlp-pot-provider/zip/refs/tags/' + PROVIDER_VERSION, work / 'provider.zip')
        unpack = work / 'source'; self.extract(archive, unpack)
        repo = unpack / ('bgutil-ytdlp-pot-provider-' + PROVIDER_VERSION)
        # Short build path avoids Windows path-length problems in node_modules.
        build = work / 'b'
        shutil.copytree(repo / 'server', build)
        env = os.environ.copy()
        env['PATH'] = str(runtime) + os.pathsep + env.get('PATH', '')
        env['XDG_CACHE_HOME'] = str(work / 'cache')
        node = runtime / 'node.exe'
        self.report('Installation des modules du générateur…')
        common = ['--cache', str(work / 'npm-cache'), '--no-audit', '--no-fund']
        self.run([node, npm, 'ci', *common], cwd=build, env=env, timeout=900)
        self.report('Compilation du générateur…')
        self.run([node, build / 'node_modules/typescript/bin/tsc'], cwd=build, env=env, timeout=120)
        self.run([node, npm, 'prune', '--omit=dev', *common], cwd=build, env=env, timeout=180)
        self.run([node, build / 'build/generate_once.js', '--version'], cwd=build, env=env)
        self.report('Installation des fichiers vérifiés…')
        for name in ('build', 'node_modules', 'src', 'types', 'package.json', 'package-lock.json', 'tsconfig.json'):
            self.commit(build / name, '_pot/' + name)
        self.commit(repo / 'LICENSE', '_pot/LICENSE')


    def repair(self, keys):
        keys = set(keys)
        with tempfile.TemporaryDirectory(prefix='yd-') as work:
            work = Path(work)
            for key in ('ytdlp', 'ffmpeg', 'node', 'provider', 'plugin'):
                if key not in keys: continue
                if self.cancel.is_set(): raise RuntimeError('Installation annulée')
                if self.check(key).ok: continue
                self.report('Installation : ' + self.names[key])
                folder = work / key; folder.mkdir()
                getattr(self, 'install_' + key)(folder)
                state = self.check(key)
                if not state.ok: raise RuntimeError(f'{self.names[key]} : {state.detail}')
        self.report('Installation terminée. Vérification complète…')
        return self.check_all()

    def update(self, keys):
        """Force replacement of tools previously reported as outdated."""
        keys = set(keys)
        with tempfile.TemporaryDirectory(prefix='yd-update-') as work:
            work = Path(work)
            for key in ('ytdlp', 'ffmpeg'):
                if key not in keys:
                    continue
                if self.cancel.is_set():
                    raise RuntimeError('Mise à jour annulée')
                self.report('Mise à jour : ' + self.names[key])
                folder = work / key; folder.mkdir()
                getattr(self, 'install_' + key)(folder)
                state = self.check(key)
                if not state.ok:
                    raise RuntimeError(f'{self.names[key]} : {state.detail}')
        self.report('Mise à jour terminée. Vérification complète…')
        return self.check_all()
