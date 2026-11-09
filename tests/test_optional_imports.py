import sys,builtins
from pathlib import Path
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
original=builtins.__import__
def guarded(name,*args,**kwargs):
    if name.split('.')[0] in ('PIL','tkinter'):
        raise ImportError('Legacy dependency must not be required')
    return original(name,*args,**kwargs)
builtins.__import__=guarded
import youtube_downloader_gui as m
from qt_window import application
m.YoutubeDownloaderApp._prefill_clipboard=lambda self:None
m.YoutubeDownloaderApp._check_dependencies=lambda self:None
a=m.YoutubeDownloaderApp();a.hide()
a._show_dependencies();a.dependency_dialog.hide()
assert 'tkinter' not in sys.modules
assert 'PIL' not in sys.modules
assert a.dependency_dialog is not None
a.dispose();application().processEvents()
print('Qt interface, dependency dialog and isolated verification support load without Tkinter or Pillow.')
