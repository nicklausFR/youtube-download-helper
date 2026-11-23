"""Build and verify a portable Windows application. Run with Python 3.11+."""
from pathlib import Path
import importlib.util
import json
import os
import shutil
import subprocess
import sys
import tempfile
import argparse

ROOT = Path(__file__).resolve().parent
NAME = 'youtube-download-helper'
DEPS = ROOT / '_dependencies'


def run(args, **kwargs):
    subprocess.run([str(arg) for arg in args], check=True, **kwargs)


def main():
    parser = argparse.ArgumentParser(description='Compiler et vérifier le téléchargeur portable.')
    parser.add_argument('--output-exe', default=NAME + '.exe',
                        help='Nom de l’EXE de sortie, à côté des dépendances ; permet un correctif séparé.')
    args = parser.parse_args()
    dest = (ROOT / args.output_exe).resolve()
    if dest.parent != ROOT or dest.suffix.lower() != '.exe':
        raise SystemExit('La sortie doit être un fichier .exe à la racine du projet.')
    if sys.platform != 'win32' or sys.version_info < (3, 11):
        raise SystemExit('Windows et Python 3.11+ sont nécessaires pour compiler.')
    for module, package in [('PyInstaller', 'PyInstaller==6.13.0'), ('PySide6', 'PySide6==6.9.1')]:
        if importlib.util.find_spec(module) is None:
            run([sys.executable, '-m', 'pip', 'install', package])
    from compile_translations import compile_translations
    from PySide6.QtCore import QLibraryInfo
    compile_translations()
    qt_translations = Path(QLibraryInfo.path(QLibraryInfo.LibraryPath.TranslationsPath))
    ffmpeg = DEPS / 'ffmpeg.exe'
    ffprobe = DEPS / 'ffprobe.exe'
    if not ffmpeg.is_file() or not ffprobe.is_file():
        raise SystemExit('Installer FFmpeg via la section Dépendances avant la compilation.')
    for path in [DEPS / 'yt-dlp.exe', DEPS / '_pot/build/generate_once.js', DEPS / '_pot/runtime/node.exe']:
        if not path.is_file(): raise SystemExit('Composant absent : ' + str(path))
    for script in ('test_architecture.py', 'test_features.py', 'test_dependencies.py',
                   'test_optional_imports.py', 'test_qt_interface.py',
                   'test_subtitle_picker.py', 'test_download_queue.py', 'test_download_list.py',
                   'test_transfer_recovery.py', 'test_thumbnails.py',
                   'test_analysis_queue.py', 'test_reliability.py', 'test_logging.py', 'test_i18n.py'):
        run([sys.executable, ROOT / 'tests' / script], cwd=ROOT)
    # Build in a fresh staging directory. A failed build never touches the working EXE.
    # Windows may briefly lock copied binaries after the startup probe. A
    # best-effort staging cleanup must not invalidate an installed, verified EXE.
    with tempfile.TemporaryDirectory(prefix='yd-exe-', ignore_cleanup_errors=True) as work:
        work = Path(work)
        stage = work / 'dist'
        stage_deps = stage / '_dependencies'
        stage_deps.mkdir(parents=True)
        run([sys.executable, '-m', 'PyInstaller', '--noconfirm', '--onefile', '--windowed',
             '--name', NAME, '--distpath', stage, '--workpath', work / 'build',
             '--specpath', work, '--icon', ROOT / 'assets/app-icon.ico',
             '--add-data', str(ROOT / 'assets/app-icon.ico') + ';assets',
             '--add-data', str(ROOT / 'translations') + ';translations',
             '--add-data', str(qt_translations / 'qtbase_fr.qm') + ';PySide6/translations',
             '--paths', ROOT / '_downloader_vendor',
             '--hidden-import', 'PySide6.QtWidgets',
             '--hidden-import', 'websocket',
             '--exclude-module', 'tkinter', '--exclude-module', 'PIL',
             '--exclude-module', 'PyQt5', '--exclude-module', 'PyQt6',
             '--exclude-module', 'numpy',
             '--exclude-module', 'matplotlib', '--exclude-module', 'IPython',
             '--exclude-module', 'pytest', ROOT / 'exe_entry.py'], cwd=ROOT)
        print('Copie des composants portables...', flush=True)
        shutil.copy2(DEPS / 'yt-dlp.exe', stage_deps / 'yt-dlp.exe')
        shutil.copytree(DEPS / '_pot', stage_deps / '_pot', ignore=shutil.ignore_patterns('__pycache__', '*.pyc'))
        for executable in (ffmpeg, ffprobe):
            source = Path(executable)
            shutil.copy2(source, stage_deps / source.name)
            # Shared FFmpeg distributions need their sibling DLLs.
            for dll in source.parent.glob('*.dll'):
                if not (stage_deps / dll.name).exists(): shutil.copy2(dll, stage_deps / dll.name)
        if (DEPS / '_licenses').is_dir(): shutil.copytree(DEPS / '_licenses', stage_deps / '_licenses')
        # No machine-specific yt-dlp.conf: the app supplies its portable Node path.
        print('Verification de la version compilee...', flush=True)
        env = os.environ.copy()
        env['PATH'] = str(Path(os.environ['SystemRoot']) / 'System32')
        env.pop('PYTHONPATH', None)
        report = work / 'build-check.json'
        run([stage / (NAME + '.exe'), '--self-test', report], cwd=work, env=env, timeout=120)
        result = json.loads(report.read_text(encoding='utf-8'))
        if not all(result.get(key) for key in ('ok', 'frozen', 'no_tkinter', 'list_view', 'translations')):
            raise RuntimeError(str(result))
        # Verify the copied multimedia binaries with Python/system FFmpeg removed from PATH.
        run([stage_deps / 'ffmpeg.exe', '-v', 'error', '-i', ROOT / 'tests/fixtures/video.mp4',
             '-i', ROOT / 'tests/fixtures/audio.m4a', '-map', '0:v', '-map', '1:a',
             '-c', 'copy', work / 'merged.mkv'], cwd=work, env=env, timeout=30)
        streams = subprocess.check_output([str(stage_deps / 'ffprobe.exe'), '-v', 'error',
                  '-show_entries', 'stream=codec_type', '-of', 'json', str(work / 'merged.mkv')], env=env)
        assert {item['codec_type'] for item in json.loads(streams)['streams']} == {'video', 'audio'}
        incoming = dest.with_name(dest.name + '.new')
        shutil.copy2(stage / (NAME + '.exe'), incoming)
        previous = None
        if dest.exists():
            backup = ROOT / '_build_backups' / __import__('datetime').datetime.now().strftime('%Y%m%d-%H%M%S-%f')
            backup.mkdir(parents=True)
            previous = backup / dest.name
            # Windows can rename a running EXE even when replacing it is forbidden.
            dest.rename(previous)
        try:
            os.replace(incoming, dest)
        except Exception:
            if previous is not None and not dest.exists(): previous.rename(dest)
            raise
        print('EXE prêt : ' + str(dest), flush=True)
        print('Interface, 7 dépendances et fusion audio/vidéo vérifiées.', flush=True)


if __name__ == '__main__':
    main()
