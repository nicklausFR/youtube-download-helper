"""Compile Qt Linguist catalogs. Run after editing translations/*.ts."""
from pathlib import Path
import subprocess
import sys


def compile_translations():
    import PySide6
    root = Path(__file__).resolve().parent
    executable = Path(PySide6.__file__).resolve().parent / ('lrelease.exe' if sys.platform == 'win32' else 'lrelease')
    for source in sorted((root / 'translations').glob('downloader_*.ts')):
        subprocess.run([str(executable), '-silent', '-nounfinished', str(source),
                        '-qm', str(source.with_suffix('.qm'))], check=True)


if __name__ == '__main__':
    compile_translations()
