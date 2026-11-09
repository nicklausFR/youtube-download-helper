"""Windows executable entry point, including isolated build probes."""
import json
import os
from pathlib import Path
import sys
import time


def main():
    base = Path(sys.executable).parent if getattr(sys, 'frozen', False) else Path(__file__).parent
    from youtube_downloader_gui import YoutubeDownloaderApp
    from qt_window import application
    if len(sys.argv) == 3 and sys.argv[1] == '--self-test':
        from dataclasses import asdict
        result = {'ok': False, 'frozen': bool(getattr(sys, 'frozen', False))}
        app = None
        try:
            # Exercise the real window and startup checks without clipboard/network analysis.
            YoutubeDownloaderApp._prefill_clipboard = lambda self: None
            app = YoutubeDownloaderApp()
            app.hide()
            deadline = time.monotonic() + 90
            while not app.dependency_states or app.dependency_busy:
                if time.monotonic() > deadline: raise RuntimeError('Startup checks timed out')
                application().processEvents()
                time.sleep(0.03)
            result['dependencies'] = [asdict(item) for item in app.dependency_states]
            result['ok'] = all(item.ok for item in app.dependency_states)
            result['window'] = app.windowTitle()
            result['interface'] = 'PySide6'
            result['no_tkinter'] = 'tkinter' not in sys.modules
            result['base'] = str(base)
        except Exception as error:
            result['error'] = str(error)
        finally:
            if app is not None: app.dispose()
            Path(sys.argv[2]).write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding='utf-8')
        return 0 if result['ok'] else 1
    app = YoutubeDownloaderApp()
    app.show()
    return application().exec()


if __name__ == '__main__':
    raise SystemExit(main())
