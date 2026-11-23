"""Windows executable entry point, including isolated build probes."""
import json
import os
from pathlib import Path
import sys
import time


def main():
    base = Path(sys.executable).parent if getattr(sys, 'frozen', False) else Path(__file__).parent
    from app_logging import setup_logging, log_event
    setup_logging(base)
    from youtube_downloader_gui import YoutubeDownloaderApp
    from qt_window import application
    if len(sys.argv) == 3 and sys.argv[1] == '--self-test':
        from dataclasses import asdict
        result = {'ok': False, 'frozen': bool(getattr(sys, 'frozen', False))}
        app = None
        try:
            # Exercise the real window and startup checks without clipboard/network analysis.
            YoutubeDownloaderApp._prefill_clipboard = lambda self: None
            YoutubeDownloaderApp._restore_download_checkpoint = lambda self: None
            YoutubeDownloaderApp._save_download_checkpoint = lambda self, **kwargs: None
            app = YoutubeDownloaderApp(list_view=True)
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
            result['list_view'] = not app.download_table.isHidden()
            result['no_tkinter'] = 'tkinter' not in sys.modules
            from qt_i18n import language_manager
            language = language_manager().language
            language_manager().set_language('en')
            english = (app.windowTitle() == 'YouTube Downloader'
                       and app.download_table.tree.headerItem().text(1) == 'Duration'
                       and [action.text() for action in app.options_menu.actions() if not action.isSeparator()]
                           == ['Theme', 'Language', 'Dependencies'])
            language_manager().set_language('fr')
            result['translations'] = (english and app.windowTitle() == 'Téléchargeur YouTube'
                                      and [action.text() for action in app.options_menu.actions() if not action.isSeparator()]
                                          == ['Thème', 'Langue', 'Dépendances'])
            language_manager().set_language(language)
            result['base'] = str(base)
            log_event('application.self_test', ok=result['ok'], dependencies=result['dependencies'])
        except Exception as error:
            result['error'] = str(error)
        finally:
            if app is not None: app.dispose()
            Path(sys.argv[2]).write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding='utf-8')
        return 0 if result['ok'] else 1
    app = YoutubeDownloaderApp(list_view='--onglets' not in sys.argv)
    app.show()
    return application().exec()


if __name__ == '__main__':
    raise SystemExit(main())
