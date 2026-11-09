import sys
from pathlib import Path
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
import hashlib,io,json,os,sys,tempfile,unittest,zipfile
from pathlib import Path
from unittest.mock import patch
sys.stdout.reconfigure(encoding='utf-8')
from dependency_manager import DependencyManager,Dependency

class Dependencies(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory(prefix='yd-test-');self.root=Path(self.temp.name)
        self.manager=DependencyManager(self.root)
    def tearDown(self):self.temp.cleanup()
    def test_missing_components_detected(self):
        self.assertFalse(self.manager.check('node').ok)
        self.assertFalse(self.manager.check('provider').ok)
        self.assertFalse(self.manager.check('plugin').ok)
        self.assertTrue(self.manager.check('python').ok)
    def test_corrupt_plugin_detected(self):
        p=self.root/'_pot/plugins/bgutil-ytdlp-pot-provider.zip';p.parent.mkdir(parents=True);p.write_bytes(b'invalid')
        self.assertFalse(self.manager.check('plugin').ok)
    def test_checksum_rejects_wrong_download(self):
        body=b'invalid binary'
        response=io.BytesIO(body);response.headers={}
        with patch('dependency_manager.urllib.request.urlopen',return_value=response):
            with self.assertRaisesRegex(RuntimeError,'SHA256'):
                self.manager.download('https://example.com/file',self.root/'file',hashlib.sha256(b'expected').hexdigest())
        self.assertFalse((self.root/'yt-dlp.exe').exists())
    def test_unsafe_archive_rejected(self):
        archive=self.root/'bad.zip'
        with zipfile.ZipFile(archive,'w') as z:z.writestr('../outside.txt','bad')
        with self.assertRaises(RuntimeError):self.manager.extract(archive,self.root/'extract')
        self.assertFalse((self.root/'outside.txt').exists())
    def test_replacement_preserves_backup_and_other_files(self):
        target=self.root/'yt-dlp.exe';target.write_bytes(b'old')
        unrelated=self.root/'personal.txt';unrelated.write_text('preserve')
        source=self.root/'replacement';source.write_bytes(b'new')
        self.manager.commit(source,'yt-dlp.exe')
        self.assertEqual(target.read_bytes(),b'new')
        self.assertEqual(next((self.root/'_dependency_backups').iterdir()).read_bytes(),b'old')
        self.assertEqual(unrelated.read_text(),'preserve')
        with self.assertRaises(RuntimeError):self.manager.commit(source,'../outside.exe')
    def test_repair_orders_runtime_before_provider(self):
        calls=[]
        def checker(key):return Dependency(key,key,key in calls,'ok' if key in calls else 'Absent')
        with patch.object(self.manager,'check',side_effect=checker),patch.object(self.manager,'check_all',return_value=[]),\
             patch.object(self.manager,'install_node',side_effect=lambda work:calls.append('node')),\
             patch.object(self.manager,'install_provider',side_effect=lambda work:calls.append('provider')),\
             patch.object(self.manager,'install_plugin',side_effect=lambda work:calls.append('plugin')):
            self.manager.repair(['plugin','provider','node'])
        self.assertEqual(calls,['node','provider','plugin'])
    def test_ytdlp_installer_checks_then_replaces(self):
        source=b'fixture executable';digest=hashlib.sha256(source).hexdigest()
        release={'assets':[{'name':'yt-dlp.exe','digest':'sha256:'+digest,'browser_download_url':'https://example.com/yt-dlp.exe'}]}
        response=io.BytesIO(source);response.headers={}
        work=self.root/'work';work.mkdir()
        with patch.object(self.manager,'get_json',return_value=release),patch('dependency_manager.urllib.request.urlopen',return_value=response),patch.object(self.manager,'run',return_value='2026.08.19') as run:
            self.manager.install_ytdlp(work)
        run.assert_called_once()
        self.assertEqual((self.root/'yt-dlp.exe').read_bytes(),source)
    def test_available_updates_compares_official_versions(self):
        with patch.object(self.manager,'executable',side_effect=lambda name:str(self.root/name)),\
             patch.object(self.manager,'run',side_effect=['2026.07.04','ffmpeg version N-121761-git']),\
             patch.object(self.manager,'get_json',return_value={'tag_name':'2026.08.19'}),\
             patch.object(self.manager,'get_text',return_value='9.0.1'):
            updates,errors=self.manager.available_updates()
        self.assertFalse(errors)
        self.assertEqual(updates['ytdlp'],('2026.07.04','2026.08.19'))
        self.assertEqual(updates['ffmpeg'],('N-121761-git','9.0.1'))

if __name__=='__main__':unittest.main(verbosity=2)
