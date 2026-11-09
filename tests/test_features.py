import sys,json,unittest,io,os,threading,http.server,functools,subprocess,time
from pathlib import Path
from unittest.mock import patch,MagicMock
sys.stdout.reconfigure(encoding='utf-8')
import tempfile,atexit
PROJECT=Path(__file__).resolve().parents[1]
FIXTURES=Path(__file__).parent/"fixtures"
_output=tempfile.TemporaryDirectory(prefix="yd-tests-")
atexit.register(_output.cleanup)
ROOT=Path(_output.name)
sys.path.insert(0,str(PROJECT/"_dependencies"/"_python"))
sys.path.insert(0,str(PROJECT))
from qt_window import application
import youtube_downloader_gui as m
import clip_strategy as clips
from automatic_session import provider_options
m.YoutubeDownloaderApp._prefill_clipboard=lambda self:None
m.YoutubeDownloaderApp._fit_window_to_content=lambda self:None
request_tab_preview = m.YoutubeDownloaderApp._request_tab_preview
m.YoutubeDownloaderApp._request_tab_preview=lambda self,video_id,fetch_title:None
m.YoutubeDownloaderApp._check_dependencies=lambda self:None
save_checkpoint = m.YoutubeDownloaderApp._save_download_checkpoint
restore_checkpoint = m.YoutubeDownloaderApp._restore_download_checkpoint
# Tests must never read or overwrite the user's interrupted downloads.
m.YoutubeDownloaderApp._save_download_checkpoint=lambda self:None
m.YoutubeDownloaderApp._restore_download_checkpoint=lambda self:None
def audio(fid,lang,rate,size,pref=10):
    return dict(format_id=fid,vcodec='none',acodec='mp4a.40.2',language=lang,abr=rate,filesize=size,ext='m4a',language_preference=pref)
def video(fid,height,size,codec='avc1.64001f',ext='mp4'):
    return dict(format_id=fid,vcodec=codec,acodec='none',height=height,filesize=size,fps=30,ext=ext,protocol='https')
DATA={'id':'urFIHf5coxE','title':'Vidéo test','channel':'Chaîne test','duration':10,
      'formats':[video('720',720,500),video('1080',1080,1000),video('4k',2160,2000,'vp09','webm'),
                 audio('source129','en',129,100),audio('dub128','fr',128,99,-1),audio('source64','en',64,50)],
      'automatic_captions':{'en-orig':[{'url':'https://www.youtube.com/api/timedtext?lang=en&kind=asr','name':'English'}],
                            'fr':[{'url':'https://www.youtube.com/api/timedtext?lang=en&tlang=fr','name':'French'}]}}
class Features(unittest.TestCase):
    def setUp(self):
        self.a=m.YoutubeDownloaderApp();self.a.hide();self.a.ytdlp=str(PROJECT/'_dependencies'/'yt-dlp.exe')
        self.a._process_analysis(DATA)
    def tearDown(self):
        self.a.dispose()
        application().processEvents()
    def test_auto_codec_language_bitrate_and_size(self):
        a=self.a
        self.assertEqual(a.container_var.get(),'MKV')
        self.assertEqual(a._selection()['format'],'720+source129')
        self.assertEqual(a.height_combo['values'],('720p · 30 i/s','1080p · 30 i/s'))
        self.assertIn(m.human_size(600),a.size_var.get())
        a.height_var.set('1080p · 30 i/s');a._refresh_resolution_choices()
        self.assertEqual(a._selection()['format'],'1080+source129')
        self.assertIn(m.human_size(1100),a.size_var.get())
        self.assertIn('129',a.auto_summary_var.get())
        self.assertTrue(a.container_frame.isHidden())
        self.assertFalse(hasattr(a,'range_combo'))

    def test_progress_debug_names_stream_size_speed_and_eta(self):
        progress=m.DownloadProgress('137+140', data={'formats': [
            {'format_id': '140', 'filesize': 8 * 1024**2},
        ]})
        line=m.PROGRESS_PREFIX+json.dumps({
            'id':'137','status':'downloading','downloaded':10485760,
            'total':104857600,'speed':2097152,'eta':45,
            'fragment':3,'fragments':12,
        })
        update=progress.parse(line)
        self.assertIsNotNone(update)
        self.assertTrue(progress.detail_line.startswith('Vidéo'))
        self.assertNotIn('Flux',progress.detail_line)
        self.assertNotIn('format 137',progress.detail_line)
        self.assertIn('10.0 Mo / 100.0 Mo',progress.detail_line)
        self.assertIn('2.0 Mo/s',progress.detail_line)
        self.assertIn('reste 00:45',progress.detail_line)
        self.assertIn('fragment 3/12',progress.detail_line)
        self.assertEqual(progress.eta_seconds, 49)
        progress.parse(m.PROGRESS_PREFIX + json.dumps({'id': '137', 'status': 'finished'}))
        self.assertIsNone(progress.eta_seconds)
        progress.parse(m.PROGRESS_PREFIX + json.dumps({
            'id': '140', 'downloaded': 1000, 'total': 6000, 'speed': 1000,
        }))
        self.assertEqual(progress.eta_seconds, 5)
    def test_manual_all_resolutions_and_audio_only(self):
        a=self.a;a.settings_var.set('manual');a._settings_changed()
        self.assertIn('2160p · 30 i/s',a.height_combo['values'])
        a.height_var.set('2160p · 30 i/s');a._refresh_resolution_choices()
        self.assertEqual(a._selection()['video']['format_id'],'4k')
        self.assertIn(m.human_size(2100),a.size_var.get())
        idx=next(i for i,f in enumerate(a.audio_display_formats) if f['format_id']=='source64')
        a.audio_combo.current(idx);a._audio_changed()
        self.assertIn(m.human_size(2050),a.size_var.get())
        a.mode_var.set('audio');a._mode_changed()
        self.assertFalse(a.settings_row.isHidden())
        self.assertFalse(a.audio_frame.isHidden())
        self.assertFalse(a.container_frame.isHidden())
        self.assertEqual(a._selection()['format'],'source64')
        a.settings_var.set('auto');a._settings_changed()
        self.assertEqual(a.container_var.get(),'M4A')
        self.assertEqual(a._selection()['format'],'source129')
        self.assertIn(m.human_size(100),a.size_var.get())
        a.mode_var.set('video');a._mode_changed()
        self.assertEqual(a.settings_var.get(),'auto')
        self.assertFalse(a.settings_row.isHidden())

    def test_resolution_list_distinguishes_frame_rates(self):
        a=self.a
        data=dict(DATA,formats=[
            video('1080-30',1080,1000),
            dict(video('1080-60',1080,1800),fps=60),
            audio('source129','en',129,100),
        ])
        a._process_analysis(data)
        self.assertEqual(
            a.height_combo['values'],
            ('1080p · 30 i/s','1080p · 60 i/s'),
        )
        a.height_var.set('1080p · 30 i/s');a._refresh_resolution_choices()
        self.assertEqual(a._selection()['video']['format_id'],'1080-30')
        a.height_var.set('1080p · 60 i/s');a._refresh_resolution_choices()
        self.assertEqual(a._selection()['video']['format_id'],'1080-60')

    def test_advanced_clip_is_available_for_every_media_mode(self):
        a=self.a;a.settings_var.set('manual');a._settings_changed()
        self.assertEqual(a.clip_slider.maximum(),10000)
        self.assertEqual(a.clip_start_var.get(),'00:00:00.000')
        self.assertEqual(a.clip_end_var.get(),'00:00:10.000')
        for mode in ('video','video_only','audio'):
            a.mode_var.set(mode);a._mode_changed()
            self.assertFalse(a.clip_frame.isHidden(),mode)
        a.mode_var.set('subtitles');a._mode_changed()
        self.assertTrue(a.clip_frame.isHidden())

    def test_clip_validation_and_size_estimate(self):
        a=self.a;a.settings_var.set('manual');a._settings_changed()
        whole=a.size_var.get()
        a.clip_start_entry.setText('00:02');a.clip_end_entry.setText('00:06')
        self.assertEqual(a._clip_range(),(2,6))
        self.assertNotEqual(a.size_var.get(),whole)
        self.assertIn(m.human_size(240),a.size_var.get())
        a.clip_end_entry.setText('00:01')
        self.assertIn('postérieure',a.clip_note_var.get())
        self.assertFalse(a.download_button.isEnabled())

    def test_srt_is_cropped_and_retimed_for_video_extract(self):
        source=(
            '1\r\n00:00:01,000 --> 00:00:03,000\r\nAvant\r\n\r\n'
            '2\r\n00:00:05,000 --> 00:00:07,000\r\nDébut chevauché\r\n\r\n'
            '3\r\n00:00:09,000 --> 00:00:12,000 position:50%\r\nFin chevauchée\r\n')
        result=m.trim_srt_text(source,5.5,10.0)
        self.assertNotIn('Avant',result)
        self.assertIn('00:00:00,000 --> 00:00:01,500',result)
        self.assertIn('00:00:03,500 --> 00:00:04,500 position:50%',result)
        self.assertEqual(result.count(' --> '),2)
        self.assertTrue(result.startswith('1\n'))

    def test_youtube_rolling_srt_is_turned_into_stable_non_overlapping_cues(self):
        source=(
            '1\n00:00:00,000 --> 00:00:04,000\nPremière ligne\n\n'
            '2\n00:00:03,000 --> 00:00:08,000\nPremière ligne\nDeuxième ligne\n\n'
            '3\n00:00:07,000 --> 00:00:12,000\nDeuxième ligne\nTroisième ligne\n')
        result=m.clean_rolling_srt_text(source)
        self.assertEqual(result.count('Première ligne'),1)
        self.assertEqual(result.count('Deuxième ligne'),1)
        self.assertEqual(result.count('Troisième ligne'),1)
        self.assertIn('00:00:00,000 --> 00:00:03,000\nPremière ligne',result)
        self.assertIn('00:00:03,000 --> 00:00:07,000\nDeuxième ligne',result)
        self.assertIn('00:00:07,000 --> 00:00:12,000\nTroisième ligne',result)

    def test_authored_multiline_srt_is_not_rewritten_as_rolling(self):
        source=(
            '1\n00:00:00,000 --> 00:00:03,000\nBonjour\nà tous\n\n'
            '2\n00:00:03,000 --> 00:00:06,000\nPhrase différente\n')
        self.assertEqual(m.clean_rolling_srt_text(source),source)

    def test_youtube_overlapping_phrases_stop_when_the_next_one_starts(self):
        source=(
            '1\n00:00:00,000 --> 00:00:04,000\nPremière phrase\n\n'
            '2\n00:00:03,000 --> 00:00:08,000\nDeuxième phrase\n\n'
            '3\n00:00:07,000 --> 00:00:12,000\nTroisième phrase\n')
        result=m.clean_rolling_srt_text(source)
        self.assertIn(
            '00:00:00,000 --> 00:00:03,000\nPremière phrase',result)
        self.assertIn(
            '00:00:03,000 --> 00:00:07,000\nDeuxième phrase',result)
        self.assertIn(
            '00:00:07,000 --> 00:00:12,000\nTroisième phrase',result)

    def test_late_subtitle_retry_uses_the_remembered_clip(self):
        folder=ROOT/'late-retry';folder.mkdir(exist_ok=True)
        path=folder/'Retry.fr.srt'
        source=(
            '1\n00:00:04,000 --> 00:00:06,000\nAvant et début\n\n'
            '2\n00:00:08,000 --> 00:00:12,000\nDans extrait\n')
        def recovered(*args,**kwargs):
            path.write_text(source,encoding='utf-8')
            return True,''
        with patch.object(self.a,'_run_subtitle_download',side_effect=recovered):
            results,error=self.a._download_subtitle_files(
                'https://youtu.be/test',str(folder),'fr',[],(5.5,10.0))
        self.assertEqual(error,'')
        self.assertEqual(results,[str(path)])
        content=path.read_text(encoding='utf-8')
        self.assertIn('00:00:00,000 --> 00:00:00,500',content)
        self.assertIn('00:00:02,500 --> 00:00:04,500',content)

    def test_subtitles_start_in_parallel_with_the_video(self):
        subtitle_started=threading.Event();media_started=threading.Event()
        def subtitles(*args,**kwargs):
            subtitle_started.set()
            self.assertTrue(media_started.wait(2))
            return [],'refus SRT'
        def media(*args,**kwargs):
            media_started.set()
            self.assertTrue(subtitle_started.wait(2))
            kwargs['subtitle_completion']()
            self.a.events.put(('download_done',[],str(ROOT)))
        with patch.object(self.a,'_download_subtitle_files',side_effect=subtitles), \
             patch.object(self.a,'_download_with_access',side_effect=media):
            self.a._download_worker(
                'https://youtu.be/test',str(ROOT),'video','720','fr',None,
                (5.5,10.0),False)
        events=[]
        while not self.a.events.empty():events.append(self.a.events.get_nowait())
        warning=next(event for event in events if event[0]=='subtitle_warning')
        self.assertEqual(warning[2][2],'subtitles_retry')
        self.assertEqual(warning[2][6],(5.5,10.0))
        self.assertLess(
            events.index(warning),
            next(i for i,event in enumerate(events) if event[0]=='download_done'))

    def test_failed_subtitles_remain_retryable_after_dismissing_popup(self):
        media=ROOT/'media.mkv';media.write_bytes(b'media')
        request=('https://youtu.be/test',str(ROOT),'subtitles_retry','fr','fr',None,(5.5,10.0))
        self.a.download_results=[str(media)]
        self.a.download_result_folder=str(ROOT)
        with patch.object(self.a,'_ask_subtitle_retry',return_value=False):
            self.a.events.put(('subtitle_retry_failed','refus persistant',request))
            self.a._poll_events()
            application().processEvents()
        self.assertEqual(self.a.failed_subtitle_request,request)
        self.assertTrue(self.a.retry_subtitles_action.isVisible())
        with patch.object(self.a,'_ask_subtitle_retry',return_value=True), \
             patch.object(self.a,'_retry_failed_subtitles') as retry:
            self.a._offer_failed_subtitle_retry()
        retry.assert_called_once_with()

    def test_clip_slider_updates_the_precise_time_fields(self):
        a=self.a;a.settings_var.set('manual');a._settings_changed()
        a.clip_slider.setSelection(1250,6750)
        self.assertEqual(a.clip_start_var.get(),'00:00:01.250')
        self.assertEqual(a.clip_end_var.get(),'00:00:06.750')
        self.assertEqual(a._clip_range(),(1.25,6.75))
        self.assertIn('index des images-clés',a.clip_note_var.get())

    def test_clip_slider_and_typed_times_snap_to_keyframes(self):
        a=self.a;a.settings_var.set('manual');a._settings_changed()
        a.clip_slider.setAllowedValues([0,2000,5000,8000,10000])
        a.clip_slider.setEnabled(True)
        a.clip_slider.setSelection(1250,6750)
        self.assertEqual((a.clip_slider.selectionStart(),a.clip_slider.selectionEnd()),(0,8000))
        self.assertEqual(a.clip_start_var.get(),'00:00:00.000')
        self.assertEqual(a.clip_end_var.get(),'00:00:08.000')
        a.clip_start_entry.setText('00:00:04.100')
        a.clip_end_entry.setText('00:00:08.700')
        a._apply_clip_time_entries()
        self.assertEqual((a.clip_slider.selectionStart(),a.clip_slider.selectionEnd()),(2000,10000))

    def test_clip_failure_never_falls_back_to_downloading_the_full_video(self):
        a=self.a
        with patch.object(self.a,'_download_native_clip',side_effect=clips.NativeClipUnsupported('non géré')), \
             patch.object(m.subprocess,'Popen') as popen:
            a._download_with_access('https://youtu.be/test',str(ROOT),'video_only','720',None,None,(2.125,6.75),[])
        popen.assert_not_called()
        events=[]
        while not a.events.empty():events.append(a.events.get_nowait())
        error=next(event for event in events if event[0]=='download_error')
        self.assertIn('ancienne coupe FFmpeg n’a pas été relancée',error[1])
        self.assertIn('non géré',error[1])

    def test_media_download_embeds_native_and_sponsorblock_chapters_without_splitting(self):
        def failed_process(*args,**kwargs):
            process=MagicMock();process.stdout=io.StringIO();process.wait.return_value=1;process.poll.return_value=1
            return process
        with patch.object(m.subprocess,'Popen',side_effect=failed_process) as popen:
            self.a._download_with_access(
                'https://example.invalid/video',str(ROOT),'video_only','720',
                None,None,None,[])
        command=popen.call_args_list[0].args[0]
        self.assertIn('--embed-chapters',command)
        self.assertIn('--sponsorblock-mark',command)
        self.assertEqual(command[command.index('--sponsorblock-mark')+1],'all')
        self.assertNotIn('--sponsorblock-remove',command)
        self.assertNotIn('--split-chapters',command)

    def test_clip_selector_preserves_quality_codec_audio_and_uses_direct_streams(self):
        video={'format_id':'136','height':720,'vcodec':'avc1.4d401f','acodec':'none'}
        audio={'format_id':'140','acodec':'mp4a.40.2','vcodec':'none',
               'abr':129.4,'language':'en-US'}
        selector=m.clip_format_selector('video',video,audio)
        self.assertIn('bestvideo[protocol=https][vcodec^=avc1][height=720]',selector)
        self.assertIn('bestaudio[protocol=https][acodec^=mp4a]',selector)
        self.assertIn('[language^=en]',selector)
        self.assertIn('[abr>=',selector)
        self.assertNotIn('136',selector)
        self.assertNotIn('140',selector)
        self.assertEqual(
            m.clip_format_selector('video_only',video,None),
            'bestvideo[protocol=https][vcodec^=avc1][height=720]')
        audio_only=m.clip_format_selector('audio',None,audio)
        self.assertIn('bestaudio[protocol=https][acodec^=mp4a]',audio_only)
        self.assertNotIn('bestvideo',audio_only)

    def test_silent_downloader_is_stopped_instead_of_spinning_forever(self):
        a=self.a
        original=m.subprocess.Popen
        started=[]
        def silent(cmd,**kwargs):
            if cmd[0] == 'taskkill.exe':
                return original(cmd,**kwargs)
            process=original([sys.executable,'-c','import time;time.sleep(30)'],**kwargs)
            started.append(process)
            return process
        with patch.object(m,'DOWNLOAD_STALL_SECONDS',0.3),patch.object(m.subprocess,'Popen',side_effect=silent):
            a._download_with_access('https://youtu.be/test',str(ROOT),'video_only','720',None,None,None,[])
        events=[]
        while not a.events.empty():events.append(a.events.get_nowait())
        verification=next(event for event in events if event[0]=='verification_required')
        self.assertIn('bloqué a été arrêté automatiquement',verification[2])
        self.assertEqual(verification[3][2],'video_only')
        self.assertIsNone(verification[3][6])
        self.assertFalse(any(event[0]=='download_error' for event in events))
        self.assertTrue(started and started[0].poll() is not None)

    def test_silent_clip_tries_two_routes_before_google_verification(self):
        a=self.a
        options=['--extractor-args','youtube:player_client=mweb']
        with patch.object(
                a,'_download_native_clip',
                side_effect=[clips.NativeClipUnsupported('HTTP Error 429 route 1'),
                             clips.NativeClipUnsupported('HTTP Error 429 route 2')]) as native:
            a._download_with_access(
                'https://youtu.be/test',str(ROOT),'video_only','720',None,None,
                (2,6),options)
        self.assertEqual(native.call_count,2)
        first_options=native.call_args_list[0].args[6]
        retry_options=native.call_args_list[1].args[6]
        self.assertIn('youtube:player_client=mweb',first_options)
        self.assertNotIn('youtube:player_client=mweb',retry_options)
        events=[]
        while not a.events.empty():events.append(a.events.get_nowait())
        self.assertTrue(any(
            event[0]=='download_phase' and 'autre route' in event[1]
            for event in events),events)
        verification=next(event for event in events if event[0]=='verification_required')
        self.assertEqual(verification[3][6],(2,6))
        self.assertFalse(any(event[0]=='download_error' for event in events))

    def test_youtube_clip_uses_native_indexed_strategy_before_legacy(self):
        with patch.object(self.a,'_download_native_clip',return_value=True) as native, \
             patch.object(m.subprocess,'Popen') as popen:
            self.a._download_with_access(
                'https://youtu.be/test',str(ROOT),'video_only','720',None,None,
                (2,6),[])
        native.assert_called_once()
        popen.assert_not_called()

    def test_native_clip_resolves_access_without_asking_ffmpeg_for_a_section(self):
        payload={
            'id':'x','title':'Essai','duration':10,'chapters':[],
            'sponsorblock':[],
            'requested':[{'url':'https://googlevideo.invalid/media','ext':'mp4',
                          'filesize':123,'vcodec':'avc1','acodec':'none',
                          'http_headers':{}}]}
        process=MagicMock()
        process.stdout=io.StringIO(m.CLIP_INFO_PREFIX+json.dumps(payload)+'\n')
        process.wait.return_value=0
        with patch.object(m.subprocess,'Popen',return_value=process) as popen:
            info=self.a._native_clip_info('https://youtu.be/test','720',[])
        command=popen.call_args.args[0]
        self.assertIn('--skip-download',command)
        self.assertIn('--sponsorblock-mark',command)
        self.assertNotIn('--download-sections',command)
        self.assertEqual(info['streams'][0]['filesize'],123)

    def test_native_clip_info_template_is_valid_for_a_single_real_stream(self):
        class Handler(http.server.SimpleHTTPRequestHandler):
            def log_message(self,*args):pass
        server=http.server.ThreadingHTTPServer(
            ('127.0.0.1',0),functools.partial(Handler,directory=str(FIXTURES)))
        thread=threading.Thread(target=server.serve_forever,daemon=True);thread.start()
        try:
            info=self.a._native_clip_info(
                f'http://127.0.0.1:{server.server_port}/video.mp4','best',
                ['--ignore-config'])
        finally:
            server.shutdown();server.server_close();thread.join()
        self.assertEqual(info['title'],'video')
        self.assertEqual(len(info['streams']),1)
        self.assertTrue(info['streams'][0]['url'].startswith('http://127.0.0.1:'))

    def test_native_clip_is_assembled_locally_and_keeps_embedded_chapters(self):
        info={
            'title':'Extrait natif test','chapters':[
                {'start_time':0,'end_time':2,'title':'Créateur'}],
            'sponsorblock':[
                {'start_time':0.8,'end_time':1.0,'title':'Sponsor'}],
            'streams':[{'url':'https://googlevideo.invalid/media','ext':'mp4',
                        'filesize':1000,'vcodec':'avc1','acodec':'none'}]}
        def local_fragment(stream,destination,*args,**kwargs):
            Path(destination).write_bytes((FIXTURES/'video.mp4').read_bytes())
            return 0.0,2.0,Path(destination).stat().st_size
        with patch.object(self.a,'_native_clip_info',return_value=info), \
             patch.object(m,'download_indexed_stream',side_effect=local_fragment):
            self.a._download_native_clip(
                'https://youtu.be/test',str(ROOT),'video_only','720','mkv',
                (0.5,1.5),[])
        events=[]
        while not self.a.events.empty():events.append(self.a.events.get_nowait())
        done=next(event for event in events if event[0]=='download_done')
        result=done[1][0]
        self.assertTrue(os.path.isfile(result))
        probe=json.loads(subprocess.check_output([
            self.a.ffmpeg.replace('ffmpeg.exe','ffprobe.exe'),'-v','error',
            '-show_chapters','-of','json',result],text=True,
            creationflags=m.CREATE_NO_WINDOW))
        titles=[item.get('tags',{}).get('title') for item in probe['chapters']]
        self.assertEqual(titles[0],titles[-1])
        self.assertTrue(titles[0].startswith('Cr'))
        self.assertIn('[SponsorBlock] : Sponsor',titles)

    def test_unindexed_webm_audio_uses_direct_m4a_without_reencoding(self):
        webm={'url':'https://googlevideo.invalid/audio','ext':'webm',
              'filesize':1000,'vcodec':'none','acodec':'opus','language':'fr'}
        m4a={'url':'https://googlevideo.invalid/audio-m4a','ext':'m4a',
             'filesize':900,'vcodec':'none','acodec':'mp4a','language':'fr'}
        main_info={'title':'Essai','chapters':[],'sponsorblock':[],
                   'streams':[webm]}
        replacement_info={'title':'Essai','chapters':[],'sponsorblock':[],
                          'streams':[m4a]}
        calls=[]
        def fragment(stream,destination,*args,**kwargs):
            calls.append(stream)
            if stream is webm:
                raise clips.NativeClipUnsupported('Table des fragments WebM introuvable.')
            Path(destination).write_bytes((FIXTURES/'audio.m4a').read_bytes())
            return 0.0,2.0,Path(destination).stat().st_size
        with patch.object(
                self.a,'_native_clip_info',
                side_effect=[main_info,replacement_info]) as info, \
             patch.object(m,'download_indexed_stream',side_effect=fragment), \
             patch.object(self.a,'_finalize_audio_clip',return_value=str(ROOT/'clip.m4a')), \
             patch.object(self.a,'_embed_clip_chapters',side_effect=lambda path,*args:path):
            self.a._download_native_clip(
                'https://youtu.be/test',str(ROOT),'audio','251',None,(0,1),[])
        self.assertEqual(calls,[webm,m4a])
        self.assertEqual(info.call_args_list[1].args[1],
                         'bestaudio[protocol=https][ext=m4a][language^=fr]')

    def test_native_clip_chapters_include_creator_and_sponsorblock_markers(self):
        chapters=self.a._arranged_clip_chapters(
            [{'start_time':0,'end_time':20,'title':'Introduction'},
             {'start_time':20,'end_time':40,'title':'Suite'}],
            [{'start_time':5,'end_time':8,'title':'Sponsor'}],2,25)
        self.assertEqual(
            [(round(item['start_time']),round(item['end_time']),item['title']) for item in chapters],
            [(2,5,'Introduction'),(5,8,'[SponsorBlock] : Sponsor'),
             (8,20,'Introduction'),(20,25,'Suite')])

    def test_mp4_native_plan_keeps_only_overlapping_fragments(self):
        import struct
        references=[]
        for size,duration in ((100,5000),(120,5000),(140,5000)):
            references.append(struct.pack('>III',size,duration,0x90000000))
        sidx=bytearray(
            struct.pack('>I4s',32+12*len(references),b'sidx')+
            b'\0\0\0\0'+struct.pack('>IIIII',1,1000,0,0,len(references)))
        # The last packed integer contains reserved=0 and count=3.
        sidx[30:32]=struct.pack('>H',len(references))
        sidx=bytes(sidx[:32])+b''.join(references)
        data=struct.pack('>I4s',8,b'ftyp')+sidx
        index=clips.parse_mp4_sidx(data)
        selected=clips.select_segments(index.references,5.5,11)
        self.assertEqual([(item.start,item.end,item.size) for item in selected],
                         [(5.0,10.0,120),(10.0,15.0,140)])
        rebuilt=clips.rebuild_mp4_sidx(data,index,selected)
        parsed=clips.parse_mp4_sidx(data[:8]+rebuilt)
        self.assertEqual(len(parsed.references),2)
        self.assertEqual(parsed.references[0].start,5.0)

    def test_native_range_advertises_the_remaining_stream_but_reads_only_clip_bytes(self):
        class Response(io.BytesIO):
            status=206
            headers={'Content-Range':'bytes 100-999/1000'}
            def getcode(self):return self.status
            def __enter__(self):return self
            def __exit__(self,*args):self.close()
        class Opener:
            request=None
            def open(self,request,timeout=None):
                self.request=request
                return Response(b'x'*900)
        reader=clips.RangeReader('http://example.invalid/media')
        opener=Opener();reader.opener=opener
        received=reader.read(100,199,advertise_to=999)
        self.assertEqual(len(received),100)
        self.assertEqual(opener.request.get_header('Range'),'bytes=100-999')
        self.assertEqual(reader.total_size,1000)

    def test_native_range_applies_new_rate_during_a_read(self):
        class Response(io.BytesIO):
            status=206
            headers={'Content-Range':'bytes 0-99/100'}
            def getcode(self):return self.status
        class Opener:
            def open(self,request,timeout=None):return Response(b'x'*100)
        rate=[100]
        clock=[0.0]
        def sleep(seconds):
            clock[0]+=seconds
            rate[0]=1000
        with patch.object(clips.time,'monotonic',side_effect=lambda:clock[0]), \
             patch.object(clips.time,'sleep',side_effect=sleep):
            reader=clips.RangeReader('http://example.invalid/media',rate_limit=lambda:rate[0])
            reader.opener=Opener()
            self.assertEqual(reader.read(0,99),b'x'*100)
        self.assertGreaterEqual(clock[0],0.1)
        self.assertLess(clock[0],0.3)

    def test_low_native_rate_reports_progress_before_a_large_range_finishes(self):
        class Response(io.BytesIO):
            status=206
            headers={'Content-Range':'bytes 0-39999/40000'}
            def getcode(self):return self.status
        class Opener:
            def open(self,request,timeout=None):return Response(b'x'*40000)
        reader=clips.RangeReader('http://example.invalid/media',rate_limit=lambda:10000)
        reader.opener=Opener()
        progress=[]
        with patch.object(reader,'_pace'):
            reader.read(0,39999,progress=lambda received,total:progress.append((received,total)))
        self.assertEqual(progress,[(20000,40000),(40000,40000)])

    def test_low_ytdlp_rate_keeps_progress_frequent_for_stall_detection(self):
        a=self.a
        a.set_download_rate_bps(10000)
        process=MagicMock()
        process.stdout=io.StringIO()
        process.poll.return_value=0
        process.wait.return_value=1
        with patch.object(m.subprocess,'Popen',return_value=process) as popen:
            a._download_with_access(
                'https://example.invalid/video',str(ROOT),'video_only','720',
                None,None,None,[])
        command=popen.call_args.args[0]
        self.assertEqual(command[command.index('--limit-rate')+1],'10000')
        self.assertEqual(command[command.index('--buffer-size')+1],'16K')
        self.assertIn('--no-resize-buffer',command)

    def test_live_rate_change_restarts_ytdlp_and_resumes_partial_file(self):
        a=self.a
        a.set_download_rate_mbps(0)
        progress=m.PROGRESS_PREFIX+json.dumps({
            'id':'720','status':'downloading','downloaded':1024,'total':2048})+'\n'
        class FirstOutput(io.StringIO):
            def __iter__(self):
                yield progress
                a.set_download_rate_mbps(5)
        first=MagicMock()
        first.stdout=FirstOutput()
        first.poll.return_value=None
        first.wait.return_value=1
        second=MagicMock()
        second.stdout=io.StringIO(
            m.RESULT_PREFIX+json.dumps(str(ROOT/'result.mp4'))+'\n')
        second.poll.return_value=0
        second.wait.return_value=0
        with patch.object(m.subprocess,'Popen',side_effect=[first,second]) as popen, \
             patch('windows_engine.terminate_process_tree') as stop:
            a._download_with_access(
                'https://youtu.be/test',str(ROOT),'video_only','720',None,None,None,[])
        self.assertEqual(popen.call_count,2)
        self.assertEqual(stop.call_count,1)
        self.assertIn('--continue',popen.call_args_list[1].args[0])
        command=popen.call_args_list[1].args[0]
        self.assertEqual(command[command.index('--limit-rate')+1],'625000')
        events=[]
        while not a.events.empty():events.append(a.events.get_nowait())
        self.assertTrue(any(e[0]=='download_done' for e in events),events)
        self.assertFalse(any(e[0]=='download_error' for e in events),events)

    def test_rate_change_does_not_interrupt_merge_with_older_progress_queued(self):
        a = self.a
        a.set_download_rate_bps(0)
        finished = threading.Event()
        class Output:
            def __iter__(self):
                yield m.PROGRESS_PREFIX + json.dumps(dict(
                    id='720', status='downloading', downloaded=100, total=200)) + '\n'
                yield m.PROGRESS_PREFIX + json.dumps(dict(
                    id='720', status='finished', downloaded=200, total=200)) + '\n'
                a.set_download_rate_bps(100_000)
                finished.set()
                yield '[Merger] Merging formats into result.mkv\n'
                yield m.RESULT_PREFIX + json.dumps(str(ROOT/'result.mkv')) + '\n'
            def close(self):
                pass
        process = MagicMock()
        process.stdout = Output()
        process.poll.return_value = None
        process.wait.return_value = 0
        original_start = threading.Thread.start
        def start_reader(reader):
            original_start(reader)
            self.assertTrue(finished.wait(2))
        with patch.object(m.subprocess,'Popen',return_value=process) as start, \
             patch('windows_engine.terminate_process_tree') as stop, \
             patch.object(threading.Thread,'start',start_reader):
            a._download_with_access('https://youtu.be/test',str(ROOT),
                'video_only','720',None,None,None,[])
        start.assert_called_once()
        stop.assert_not_called()
        events = []
        while not a.events.empty():
            events.append(a.events.get_nowait())
        self.assertTrue(any(event[0]=='download_done' for event in events),events)

    def test_native_range_continues_when_youtube_caps_each_response(self):
        class Response(io.BytesIO):
            status=206
            def __init__(self,start,end,total):
                super().__init__(bytes(range(start,end+1)))
                self.headers={'Content-Range':f'bytes {start}-{end}/{total}'}
            def getcode(self):return self.status
            def __enter__(self):return self
            def __exit__(self,*args):self.close()
        class Opener:
            requests=[]
            def open(self,request,timeout=None):
                value=request.get_header('Range').removeprefix('bytes=')
                start=int(value.partition('-')[0])
                self.requests.append(request.get_header('Range'))
                return Response(start,min(start+24,199),200)
        reader=clips.RangeReader('http://example.invalid/media')
        opener=Opener();reader.opener=opener
        received=reader.read(100,174,advertise_to=199)
        self.assertEqual(received,bytes(range(100,175)))
        self.assertEqual(opener.requests,
                         ['bytes=100-199','bytes=125-199','bytes=150-199'])

    def test_native_range_cancel_closes_the_active_response(self):
        started=threading.Event();closed=threading.Event();cancelled=[False]
        class Response:
            status=206
            headers={'Content-Range':'bytes 0-99/100'}
            def getcode(self):return self.status
            def read(self,_size):
                started.set();closed.wait(2)
                raise ValueError('closed')
            def close(self):closed.set()
            def __enter__(self):return self
            def __exit__(self,*args):self.close()
        class Opener:
            def open(self,request,timeout=None):return Response()
        reader=clips.RangeReader('http://example.invalid/media')
        reader.opener=Opener();errors=[]
        worker=threading.Thread(target=lambda: self._capture_error(
            errors,lambda:reader.read(0,99,cancelled=lambda:cancelled[0])))
        worker.start();self.assertTrue(started.wait(1))
        cancelled[0]=True;reader.cancel();worker.join(2)
        self.assertFalse(worker.is_alive())
        self.assertIsInstance(errors[0],InterruptedError)

    @staticmethod
    def _capture_error(errors, callback):
        try:callback()
        except Exception as error:errors.append(error)

    def test_missing_mweb_format_retries_with_another_youtube_client(self):
        def failed_process(*args,**kwargs):
            process=MagicMock()
            process.stdout=io.StringIO(
                'WARNING: Some mweb client https formats have been skipped as they are missing a URL; SABR-only\n'
                'ERROR: Requested format is not available\n')
            process.wait.return_value=1;process.poll.return_value=1
            return process
        options=['--plugin-dirs','plugins','--extractor-args','youtube:player_client=mweb',
                 '--extractor-args','youtubepot-bgutilscript:server_home=provider']
        with patch.object(m.subprocess,'Popen',side_effect=failed_process) as popen:
            self.a._download_with_access(
                'https://youtu.be/test',str(ROOT),'video_only','720',None,None,None,options)
        self.assertEqual(popen.call_count,2)
        first=popen.call_args_list[0].args[0]
        retry=popen.call_args_list[1].args[0]
        self.assertIn('youtube:player_client=mweb',first)
        self.assertNotIn('youtube:player_client=mweb',retry)
        self.assertIn('youtubepot-bgutilscript:server_home=provider',retry)
        events=[]
        while not self.a.events.empty():events.append(self.a.events.get_nowait())
        self.assertTrue(any(
            event[0]=='download_phase' and 'autre route' in event[1]
            for event in events),events)
        verification=next(event for event in events if event[0]=='verification_required')
        self.assertIn('aucune des deux routes',verification[2])
        self.assertEqual(verification[3][3],'720')
        self.assertFalse(any(event[0]=='download_error' for event in events))

    def test_youtube_stream_403_retries_once_with_the_same_format_and_output(self):
        error = "ERROR: unable to download video data: HTTP Error 403: Forbidden\n"
        options = ["--extractor-args", "youtube:player_client=mweb",
                   "--js-runtimes", "node", "--plugin-dirs", "plugins"]
        for retry_code in (0, 1):
            with self.subTest(retry_code=retry_code), tempfile.TemporaryDirectory() as folder:
                first = MagicMock()
                first.stdout = io.StringIO(error)
                first.wait.return_value = 1
                retry = MagicMock()
                retry.stdout = io.StringIO(error if retry_code else "")
                retry.wait.return_value = retry_code
                with patch.object(m.subprocess, "Popen", side_effect=[first, retry]) as popen:
                    self.a._download_with_access(
                        "https://youtu.be/test", folder, "video", "137+140",
                        None, "mkv", None, options)

                original, fallback = [call.args[0] for call in popen.call_args_list]
                for flag in ("-f", "-o", "--js-runtimes", "--plugin-dirs"):
                    self.assertEqual(fallback[fallback.index(flag) + 1],
                                     original[original.index(flag) + 1])
                self.assertIn("--continue", fallback)
                self.assertIn("youtube:player_client=mweb", original)
                self.assertNotIn("youtube:player_client=mweb", fallback)

                events = []
                while not self.a.events.empty():
                    events.append(self.a.events.get_nowait())
                terminal = [event for event in events if event[0] in
                            ("download_done", "download_error", "verification_required")]
                self.assertEqual(len(terminal), 1, events)
                self.assertEqual(terminal[0][0], "download_error" if retry_code else "download_done")
                if retry_code:
                    self.assertIn("autre route", terminal[0][1])

    def test_download_429_opens_an_attention_popup_and_resumes_verification(self):
        a=self.a
        url='https://www.youtube.com/watch?v='+DATA['id']
        a.url_var.set(url)
        if a.analysis_job:a.after_cancel(a.analysis_job);a.analysis_job=None
        request=(url,str(ROOT),'video_only','720',None,None,(2,6))
        with patch.object(a,'_ask_download_verification',return_value=True) as prompt, \
             patch.object(a,'_open_verification') as verify:
            a.events.put(('verification_required',None,'HTTP Error 429',request))
            a._poll_events()
        prompt.assert_called_once_with()
        verify.assert_called_once_with(request)

    def test_download_refusal_after_successful_verification_does_not_loop(self):
        a=self.a
        url='https://www.youtube.com/watch?v='+DATA['id']
        a.url_var.set(url)
        if a.analysis_job:a.after_cancel(a.analysis_job);a.analysis_job=None
        request=(url,str(ROOT),'video_only','720',None,None,(2,6))
        a.verified_credentials=m.VerifiedCredentials([], 'Agent test')
        a.verification_attempted.add(url)
        with patch.object(a,'_ask_download_verification') as prompt, \
             patch.object(a,'_open_verification') as verify, \
             patch.object(m.messagebox,'showerror') as error:
            a.events.put(('verification_required',None,'HTTP Error 429',request))
            a._poll_events()
        prompt.assert_not_called()
        verify.assert_not_called()
        error.assert_called_once()
        self.assertIn('ne sera pas relancée en boucle',a.access_note_var.get())
        self.assertIn('refuse encore',a.progress_text_var.get())
    def test_authored_subtitle_is_named_before_its_provenance(self):
        tracks=m.subtitle_choices({'subtitles':{'en':[{'url':'https://example.invalid/caption','name':'English'}]},
            'automatic_captions':{
                'en-orig':[{'url':'https://example.invalid/caption?lang=en&kind=asr','name':'English'}],
                'fr':[{'url':'https://example.invalid/caption?lang=en&tlang=fr','name':'French'}]}},'en')
        original=next(t for t in tracks if t['code']=='en')
        translated=next(t for t in tracks if t['code']=='fr')
        self.assertEqual(original['label'],'anglais — publié · langue d’origine')
        self.assertIn('traduction auto',translated['label'])
        self.assertNotIn('en-orig',[t['code'] for t in tracks])

    def test_size_estimates_unknown_and_subtitles(self):
        self.assertEqual(m.estimated_bytes({'vcodec':'none','abr':128},10),160000)
        self.assertEqual(m.estimated_bytes({'filesize_approx':12345}),12345)
        self.assertIsNone(m.estimated_bytes({}))
        a=self.a;a._set_selected_subtitles(['fr']);a._update_download_button_state()
        self.assertEqual(a._selected_subtitle(),'fr')
        self.assertIn('+ sous-titres',a.size_var.get())

    def test_multiple_subtitles_are_selected_and_summarized(self):
        a=self.a
        a._set_selected_subtitles(['en-orig','fr'])
        self.assertEqual(a._selected_subtitle(),'en-orig,fr')
        self.assertEqual(a._selected_subtitle_codes(),('en-orig','fr'))
        self.assertIn('2 langues',a.subtitle_var.get())
        self.assertIn('traduction auto',a.subtitle_combo.toolTip())
        a._set_selected_subtitles([])
        self.assertIsNone(a._selected_subtitle())
        self.assertEqual(a.subtitle_var.get(),'Aucun')

    def test_subtitle_selection_forces_mkv_for_muxing(self):
        a=self.a
        a._set_selected_subtitles(['en-orig','fr'])
        a.container_var.set('MP4')
        with patch.object(a,'_request_download_folder',return_value=str(ROOT)), \
             patch.object(m.threading,'Thread') as thread:
            a._start_download()
        self.assertEqual(a.last_download_request[5],'mkv')
        thread.return_value.start.assert_called_once()

    def test_corrected_subtitles_are_muxed_and_sidecars_removed(self):
        media=ROOT/'Mux subtitles.mkv';media.write_bytes(b'original')
        english=ROOT/'Mux subtitles.en-orig.srt'
        french=ROOT/'Mux subtitles.fr.srt'
        english.write_text('subtitle',encoding='utf-8')
        french.write_text('sous-titre',encoding='utf-8')
        observed={}
        def mux(command):
            observed['command']=command
            Path(command[-1]).write_bytes(b'muxed')
            return 0,''
        with patch.object(m,'_media_streams',return_value=[]), \
             patch.object(self.a,'_run_cancellable_media_command',side_effect=mux):
            result=self.a._mux_subtitles_into_mkv(
                str(media),[str(english),str(french)])
        self.assertEqual(result,str(media))
        self.assertEqual(media.read_bytes(),b'muxed')
        self.assertFalse(english.exists())
        self.assertFalse(french.exists())
        command=observed['command']
        self.assertIn('language=eng',command)
        self.assertIn('language=fra',command)
        self.assertEqual(command.count('-map'),3)

    def test_retry_labels_new_subtitle_after_existing_mkv_subtitle(self):
        media=ROOT/'Retry labels.mkv';media.write_bytes(b'original')
        french=ROOT/'Retry labels.fr.srt'
        french.write_text('sous-titre',encoding='utf-8')
        observed={}
        def mux(command):
            observed['command']=command
            Path(command[-1]).write_bytes(b'muxed')
            return 0,''
        streams=[
            {'codec_type':'video','codec_name':'h264'},
            {'codec_type':'audio','codec_name':'aac'},
            {'codec_type':'subtitle','codec_name':'subrip'},
        ]
        with patch.object(m,'_media_streams',return_value=streams), \
             patch.object(self.a,'_run_cancellable_media_command',side_effect=mux):
            self.a._mux_subtitles_into_mkv(str(media),[str(french)])
        command=observed['command']
        title_index=command.index('-metadata:s:s:1')
        self.assertEqual(command[title_index+1],'language=fra')
        self.assertEqual(command[title_index+3],'title=français (fr)')
        self.assertNotIn('-metadata:s:s:0',command)
        self.assertIn('-disposition:s:1',command)
        self.assertNotIn('default',command)
    def test_manual_audio_controls_and_labels(self):
        a=self.a;a.settings_var.set('manual');a._settings_changed()
        self.assertTrue(a.resolution_combo.isHidden())
        self.assertEqual(a.codec_var.get(),'H.264')
        self.assertFalse(hasattr(a,'format_help_label'))
        self.assertIn('largement compatible',a.codec_combo.toolTip())
        self.assertIs(a.subtitle_frame.layout().itemAt(1).widget(),a.subtitle_combo)
        a.audio_rate_var.set('64 kbit/s');a._audio_options_changed()
        self.assertEqual(a._selection()['audio']['format_id'],'source64')
        self.assertIn(m.human_size(550),a.size_var.get())
        self.assertTrue(all('★' not in t['label'] for t in a.subtitle_tracks))
        a.settings_var.set('auto');a._settings_changed()
        self.assertNotIn('\\n',a.auto_summary_var.get())
        self.assertNotIn('réencodage',a.auto_summary_var.get())
        self.assertEqual(a.advanced_check.text(),'Réglages avancés')
        a.centralWidget().layout().activate()
        self.assertEqual(a.height_combo.width(),a.subtitle_combo.width())

    def test_multiple_audio_languages_are_selected_and_counted(self):
        a=self.a;a.settings_var.set('manual');a._settings_changed()
        labels=a._audio_language_choices()
        english=next(label for label in labels if label.startswith('anglais'))
        french=next(label for label in labels if label.startswith('français'))
        a._set_selected_audio_languages([english,french])
        selection=a._selection()
        self.assertEqual(
            [item['format_id'] for item in selection['audios']],
            ['source129','dub128'])
        self.assertEqual(selection['format'],'720+source129+dub128')
        self.assertIn('2 langues',a.audio_language_summary_var.get())
        self.assertIn(m.human_size(699),a.size_var.get())
        a.mode_var.set('audio');a._mode_changed()
        self.assertEqual(a._selection()['format'],'source129,dub128')

    def test_full_video_multiaudio_enables_ytdlp_audio_multistreams(self):
        def failed_process(*args,**kwargs):
            process=MagicMock();process.stdout=io.StringIO()
            process.wait.return_value=1;process.poll.return_value=1
            return process
        with patch.object(m.subprocess,'Popen',side_effect=failed_process) as popen:
            self.a._download_with_access(
                'https://example.invalid/video',str(ROOT),'video',
                '720+source129+dub128',None,'mkv',None,[])
        command=popen.call_args.args[0]
        self.assertIn('--audio-multistreams',command)

    def test_partial_audio_multilanguage_creates_each_file_separately(self):
        first=str(ROOT/'fr.m4a');second=str(ROOT/'en.m4a')
        with patch.object(
                self.a,'_download_native_clip',
                side_effect=[[first],[second]]) as native, \
             patch.object(m.subprocess,'Popen') as popen:
            self.a._download_with_access(
                'https://youtu.be/test',str(ROOT),'audio','audio-fr,audio-en',
                None,'m4a',(2,6),[])
        self.assertEqual(native.call_count,2)
        self.assertEqual(
            [call.args[3] for call in native.call_args_list],
            ['audio-fr','audio-en'])
        popen.assert_not_called()
        events=[]
        while not self.a.events.empty():events.append(self.a.events.get_nowait())
        done=next(event for event in events if event[0]=='download_done')
        self.assertEqual(done[1],[first,second])
    def test_video_only_and_subtitle_only_selection(self):
        a=self.a;a.mode_var.set('video_only');a._mode_changed()
        self.assertEqual(a._selection()['format'],'720')
        self.assertIsNone(a._selection()['audio'])
        self.assertIn(m.human_size(500),a.size_var.get())
        a.mode_var.set('subtitles');a._mode_changed()
        self.assertEqual(a._selected_subtitle(),'en-orig')
        self.assertEqual(a._selection()['format'],'')
        self.assertEqual(a.auto_summary_var.get(),'')
        self.assertTrue(a.auto_summary_label.isHidden())
        self.assertEqual(a.size_var.get(),'')
        self.assertTrue(a.size_label.isHidden())
        self.assertTrue(a.audio_frame.isHidden())
        self.assertTrue(a.filter_frame.isHidden())
        a._set_selected_subtitles(['fr']);self.assertEqual(a._selected_subtitle(),'fr')
        a._process_analysis({'title':'Sans sous-titres','formats':[]})
        self.assertIsNone(a._selection())
    def test_real_srt_without_media(self):
        class Handler(http.server.BaseHTTPRequestHandler):
            paths=[]
            def log_message(self,*args):pass
            def do_GET(self):
                self.paths.append(self.path)
                body=b'WEBVTT\n\n00:00:00.000 --> 00:00:01.500\nTest sous-titres seuls\n'
                self.send_response(200);self.send_header('Content-Length',str(len(body)));self.end_headers();self.wfile.write(body)
        server=http.server.ThreadingHTTPServer(('127.0.0.1',0),Handler)
        thread=threading.Thread(target=server.serve_forever,daemon=True);thread.start()
        base=f'http://127.0.0.1:{server.server_port}'
        data={'id':'srt-fixture','title':'SRT fixture','extractor':'generic','extractor_key':'Generic','webpage_url':base,
              'formats':[{'format_id':'video','url':base+'/SHOULD-NOT-DOWNLOAD.mp4','ext':'mp4'}],
              'subtitles':{
                  'fr':[{'url':base+'/captions-fr.vtt','ext':'vtt'}],
                  'en':[{'url':base+'/captions-en.vtt','ext':'vtt'}],
              }}
        info=ROOT/'local-srt-info.json';info.write_text(json.dumps(data))
        original=m.subprocess.Popen
        def sample(cmd,**kw):
            self.assertIn('--skip-download',cmd);self.assertIn('--convert-subs',cmd)
            self.assertNotIn('-f',cmd)
            return original(cmd[:-1]+['--ignore-config','--force-overwrites','--load-info-json',str(info)],**kw)
        try:
            with patch.object(m.subprocess,'Popen',side_effect=sample):
                self.a._download_worker(base,str(ROOT),'subtitles','','fr,en',None)
        finally:server.shutdown();server.server_close();thread.join()
        events=[]
        while not self.a.events.empty():events.append(self.a.events.get_nowait())
        done=next(event for event in events if event[0]=='download_done')
        self.assertEqual(len(done[1]),2,events)
        self.assertEqual(set(Handler.paths),{'/captions-fr.vtt','/captions-en.vtt'})
        for language in ('fr','en'):
            content=(ROOT/f'SRT fixture.{language}.srt').read_text(encoding='utf-8')
            self.assertIn('00:00:00,000 --> 00:00:01,500',content)

    def test_subtitle_429_retries_without_verified_session(self):
        failed=MagicMock(returncode=1)
        failed.stdout=io.StringIO('ERROR: Unable to download video subtitles for fr: HTTP Error 429\n')
        failed.poll.return_value=1;failed.wait.return_value=1
        recovered=MagicMock(returncode=0)
        recovered.stdout=io.StringIO('[download] 100%\n')
        recovered.poll.return_value=0;recovered.wait.return_value=0
        with patch.object(m.subprocess,'Popen',side_effect=[failed,recovered]) as popen, \
             patch.object(self.a,'_wait_subtitle_retry',return_value=True):
            success,error=self.a._run_subtitle_download(
                'https://youtu.be/test',str(ROOT/'%(title)s.%(ext)s'),'fr',
                ['--cookies','verified.txt'])
        self.assertTrue(success,error)
        self.assertEqual(popen.call_count,2)
        self.assertIn('--cookies',popen.call_args_list[0].args[0])
        self.assertNotIn('--cookies',popen.call_args_list[1].args[0])
        self.assertIn('--sleep-subtitles',popen.call_args_list[0].args[0])

    def test_original_and_translated_subtitles_use_separate_requests(self):
        completed=[]
        def successful(cmd,**kwargs):
            completed.append(cmd[cmd.index('--sub-langs')+1])
            process=MagicMock(returncode=0)
            process.stdout=io.StringIO('[download] 100%\n')
            process.poll.return_value=0
            process.wait.return_value=0
            return process
        with patch.object(m.subprocess,'Popen',side_effect=successful) as popen:
            success,error=self.a._run_subtitle_download(
                'https://youtu.be/test',str(ROOT/'%(title)s.%(ext)s'),
                'en-orig,fr',[])
        self.assertTrue(success,error)
        self.assertEqual(completed,['en-orig','fr'])
        original,translated=(call.args[0] for call in popen.call_args_list)
        self.assertNotIn('--sleep-subtitles',original)
        self.assertEqual(
            translated[translated.index('--sleep-subtitles')+1],
            str(m.TRANSLATED_SUBTITLE_WAIT_SECONDS))

    def test_subtitles_try_five_times_before_offering_manual_retry(self):
        failures=[]
        for _ in range(5):
            failed=MagicMock(returncode=1)
            failed.stdout=io.StringIO(
                'ERROR: Unable to download video subtitles for fr: HTTP Error 429\n')
            failed.poll.return_value=1
            failed.wait.return_value=1
            failures.append(failed)
        with patch.object(m.subprocess,'Popen',side_effect=failures) as popen, \
             patch.object(self.a,'_wait_subtitle_retry',return_value=True) as wait:
            success,error=self.a._run_subtitle_download(
                'https://youtu.be/test',str(ROOT/'%(title)s.%(ext)s'),'fr',[])
        self.assertFalse(success)
        self.assertEqual(popen.call_count,5)
        self.assertEqual(
            [item.args[:2] for item in wait.call_args_list],
            [(4,2),(8,3),(12,4),(20,5)])
        self.assertIn('après 5 tentatives',error)

    def test_active_subtitle_process_is_not_killed_by_the_idle_timeout(self):
        original=m.subprocess.Popen
        def active(cmd,**kwargs):
            script=(
                'import time\n'
                'for i in range(6):\n'
                ' print(f"activité {i}",flush=True)\n'
                ' time.sleep(0.08)\n')
            return original([sys.executable,'-u','-c',script],**kwargs)
        with patch.object(m,'SUBTITLE_STALL_SECONDS',0.15), \
             patch.object(m,'SUBTITLE_MAX_SECONDS',1.0), \
             patch.object(m.subprocess,'Popen',side_effect=active):
            success,error=self.a._run_subtitle_download(
                'https://youtu.be/test',str(ROOT/'active.%(ext)s'),'fr',[])
        self.assertTrue(success,error)

    def test_silent_subtitle_process_is_stopped_at_the_idle_timeout(self):
        original=m.subprocess.Popen
        started=[]
        def silent(cmd,**kwargs):
            if cmd[0] == 'taskkill.exe':
                return original(cmd,**kwargs)
            process=original(
                [sys.executable,'-u','-c','import time;time.sleep(30)'],**kwargs)
            started.append(process)
            return process
        with patch.object(m,'SUBTITLE_STALL_SECONDS',0.2), \
             patch.object(m,'SUBTITLE_MAX_SECONDS',1.0), \
             patch.object(m.subprocess,'Popen',side_effect=silent), \
             patch.object(self.a,'_wait_subtitle_retry',return_value=False):
            success,error=self.a._run_subtitle_download(
                'https://youtu.be/test',str(ROOT/'silent.%(ext)s'),'fr',[])
        self.assertFalse(success)
        self.assertIn('annulé',error)
        self.assertTrue(started and started[0].poll() is not None)

    def test_successful_subtitle_files_survive_a_partial_language_failure(self):
        folder=ROOT/'partial-subs';folder.mkdir(exist_ok=True)
        path=folder/'Partial.en.srt'
        def partial(*args,**kwargs):
            path.write_text(
                '1\n00:00:00,000 --> 00:00:01,000\nEnglish\n',
                encoding='utf-8')
            return False,'HTTP Error 429 pour fr-en'
        with patch.object(self.a,'_run_subtitle_download',side_effect=partial):
            results,error=self.a._download_subtitle_files(
                'https://youtu.be/test',str(folder),'en,fr-en',[])
        self.assertEqual(results,[str(path)])
        self.assertIn('429',error)

    def test_partial_subtitle_warning_names_languages_and_retries_only_missing(self):
        subtitle_path=str(ROOT/'Video.en.srt')
        def subtitles(*args,**kwargs):
            return [subtitle_path],'HTTP Error 429 pour fr-en'
        def media(*args,**kwargs):
            kwargs['subtitle_completion']()
            self.a.events.put(('download_done',[],str(ROOT)))
        with patch.object(self.a,'_download_subtitle_files',side_effect=subtitles), \
             patch.object(self.a,'_download_with_access',side_effect=media):
            self.a._download_worker(
                'https://youtu.be/test',str(ROOT),'video','720','en,fr-en',None,
                None,False)
        events=[]
        while not self.a.events.empty():events.append(self.a.events.get_nowait())
        warning=next(event for event in events if event[0]=='subtitle_warning')
        self.assertEqual(warning[2][4],'fr-en')
        self.assertEqual(warning[3],('en',))
        self.assertEqual(warning[4],('fr-en',))

    def test_partial_subtitle_popup_lists_obtained_and_missing_languages(self):
        observed={}
        def show(dialog):
            observed['title']=dialog.windowTitle()
            observed['text']=dialog.text()
            observed['detail']=dialog.informativeText()
        with patch('qt_window.QMessageBox.exec',show):
            self.a._ask_subtitle_retry(
                'HTTP Error 429',None,('en-orig',),('fr',))
        self.assertIn('partiellement',observed['title'])
        self.assertIn('anglais',observed['detail'])
        self.assertIn('traduction auto',observed['detail'])
        self.assertIn('langues manquantes',observed['detail'])

    def test_subtitle_only_partial_result_offers_retry_for_missing_languages(self):
        path=ROOT/'Subtitle only.en.srt'
        path.write_text('subtitle',encoding='utf-8')
        with patch.object(
                self.a,'_download_subtitle_files',
                return_value=([str(path)],'HTTP Error 429 pour fr')):
            self.a._download_subtitles_only(
                'https://youtu.be/test',str(ROOT),'en,fr',[],False)
        event=self.a.events.get_nowait()
        self.assertEqual(event[0],'subtitle_retry_failed')
        self.assertEqual(event[2][2],'subtitles_retry')
        self.assertEqual(event[2][4],'fr')
        self.assertEqual(event[4],('en',))
        self.assertEqual(event[5],('fr',))

    def test_late_subtitle_retry_is_muxed_into_the_downloaded_mkv(self):
        media=ROOT/'Late subtitle.mkv';media.write_bytes(b'media')
        subtitle=ROOT/'Late subtitle.fr.srt';subtitle.write_text('subtitle')
        self.a.download_results=[str(media)]
        with patch.object(
                self.a,'_download_subtitle_files',
                return_value=([str(subtitle)],'')), \
             patch.object(self.a,'_mux_subtitles_into_mkv') as mux:
            self.a._download_subtitles_only(
                'https://youtu.be/test',str(ROOT),'fr',[],False,retry=True)
        mux.assert_called_once_with(str(media),[str(subtitle)])
        event=self.a.events.get_nowait()
        self.assertEqual(event[0],'subtitle_retry_done')
        self.assertEqual(event[1],[str(media)])

    def test_partial_retry_popup_keeps_languages_from_previous_attempts(self):
        self.a.failed_subtitle_completed=('en',)
        self.a.download_thread=MagicMock()
        request=('https://youtu.be/test',str(ROOT),'subtitles_retry','',
                 'de',None,None)
        with patch.object(self.a,'after_idle'):
            self.a.events.put((
                'subtitle_retry_failed','HTTP Error 429',request,[],
                ('fr',),('de',)))
            self.a._poll_events()
        self.assertEqual(self.a.failed_subtitle_completed,('en','fr'))
        self.assertEqual(self.a.failed_subtitle_missing,('de',))

    def test_verification_required_and_single_retry(self):
        a=self.a;a.url_var.set('https://www.youtube.com/watch?v='+DATA['id'])
        if a.analysis_job:a.after_cancel(a.analysis_job);a.analysis_job=None
        with patch.object(a,'_open_verification') as open_window:
            a.events.put(('verification_required',a.analysis_token,'not a bot',None));a._poll_events()
            open_window.assert_not_called()
            self.assertFalse(a.verification_button.isHidden())
        self.assertIn('antirobot',a.status_var.get())
        self.assertIn('not a bot',a._diagnostic_text())
        a.verification_attempted.add(a.url_var.get())
        with patch.object(a,'_open_verification') as open_window:
            a.events.put(('verification_required',a.analysis_token,'not a bot',None));a._poll_events()
            open_window.assert_not_called()

    def test_rate_limit_offers_verification_after_automatic_retries(self):
        a=self.a;url='https://www.youtube.com/watch?v='+DATA['id'];a.url_var.set(url)
        if a.analysis_job:a.after_cancel(a.analysis_job);a.analysis_job=None
        a._process_analysis(DATA)
        a.rate_limit_retries[url]=2
        a.events.put(('verification_required',a.analysis_token,'HTTP Error 429',None));a._poll_events()
        self.assertFalse(a.analysis_retry_button.isHidden())
        self.assertFalse(a.verification_button.isHidden())
        self.assertEqual(a.title_var.get(),'Vidéo test')

    def test_verified_credentials_are_passed_as_temporary_cookie_file(self):
        credentials=m.VerifiedCredentials([{
            'domain':'.youtube.com','name':'TEST','value':'ok','path':'/','secure':True,
        }], 'Agent test')
        with m.request_access_options(credentials,analysis=True) as options:
            self.assertIn('--cookies',options)
            path=options[options.index('--cookies')+1]
            self.assertTrue(Path(path).is_file())
            self.assertIn('--user-agent',options)
            self.assertNotIn('--no-cookies',options)
        self.assertFalse(Path(path).exists())
    def test_analysis_timeout_and_unicode(self):
        a=self.a;p=MagicMock();p.communicate.side_effect=[subprocess.TimeoutExpired('yt',40),('','')]
        fallback=MagicMock();fallback.communicate.side_effect=[subprocess.TimeoutExpired('yt',50),('','')]
        with patch.object(m.subprocess,'Popen',side_effect=[p,fallback]) as start, \
             patch('windows_engine.terminate_process_tree') as stop:
            a._analyze_worker('url',a.analysis_token)
        self.assertEqual(start.call_count,2)
        self.assertEqual([call.args[0] for call in stop.call_args_list],[p,fallback])
        self.assertEqual(p.communicate.call_args.kwargs, {'timeout': 2})
        self.assertIn('90 secondes',a.events.get_nowait()[2])
        p=MagicMock();p.returncode=0;p.communicate.return_value=(json.dumps(DATA),'')
        with patch.object(m.subprocess,'Popen',return_value=p):
            a._analyze_worker('url',a.analysis_token)
        a._poll_events();self.assertEqual(a.title_var.get(),'Vidéo test')

    def test_provider_failure_retries_once_without_external_plugin(self):
        failed=MagicMock(returncode=1)
        failed.communicate.return_value=('', 'WARNING: Error fetching PO Token from bgutil provider: _get_pot_via_script failed')
        recovered=MagicMock(returncode=0)
        recovered.communicate.return_value=(json.dumps(DATA), '')
        with patch.object(m.subprocess,'Popen',side_effect=[failed,recovered]) as start:
            self.a._analyze_worker('url',self.a.analysis_token)
        self.assertEqual(start.call_count,2)
        command=start.call_args.args[0]
        self.assertIn('--no-plugin-dirs',command)
        self.assertNotIn('youtube:player_client=mweb',command)
        self.assertIn('--js-runtimes',command)
        event=self.a.events.get_nowait()
        self.assertEqual(event[0],'analysis_ok')
        self.assertEqual(event[2]['_downloader_access_recovery'],'without_provider')

    def test_provider_fallback_does_not_loop_and_keeps_both_errors(self):
        failed=MagicMock(returncode=1)
        failed.communicate.return_value=('', '_get_pot_via_script failed')
        with patch.object(m.subprocess,'Popen',return_value=failed) as start:
            self.a._analyze_worker('url',self.a.analysis_token)
        self.assertEqual(start.call_count,2)
        event=self.a.events.get_nowait()
        self.assertEqual(event[0],'analysis_error')
        self.assertIn('Accès de secours',event[2])

    def test_provider_error_hides_large_session_context(self):
        error="_get_pot_via_script failed: TimeoutExpired(['node', '--innertube-context', '{secret-context}'])"
        message=m.readable_error(error)
        self.assertIn('BgUtils',message)
        self.assertNotIn('secret-context',message)

    def test_provider_timeout_reserves_time_for_successful_fallback(self):
        clock=[100.0]
        failed=MagicMock()
        def timeout(**kwargs):
            clock[0]=145.0  # Includes time spent terminating the process tree.
            raise subprocess.TimeoutExpired('yt',40,stderr=b'bgutil stalled')
        def communicate(**kwargs):
            if kwargs['timeout']==2:
                return '', ''
            return timeout(**kwargs)
        failed.communicate.side_effect=communicate
        recovered=MagicMock(returncode=0)
        recovered.communicate.return_value=(json.dumps(DATA),'')
        with patch.object(m.subprocess,'Popen',side_effect=[failed,recovered]) as start, \
             patch('windows_engine.time.monotonic',side_effect=lambda:clock[0]), \
             patch('windows_engine.terminate_process_tree') as stop:
            self.a._analyze_worker('url',self.a.analysis_token)
        stop.assert_called_once_with(failed)
        self.assertEqual(failed.communicate.call_args_list[0].kwargs['timeout'],40)
        self.assertEqual(recovered.communicate.call_args.kwargs['timeout'],45)
        self.assertIn('--no-plugin-dirs',start.call_args.args[0])
        event=self.a.events.get_nowait()
        self.assertEqual(event[0],'analysis_ok')
        self.assertEqual(event[2]['_downloader_access_recovery'],'without_provider')

    def test_final_fallback_error_controls_message_and_verification(self):
        for first_error,final_error,kind in (
                ('_get_pot_via_script failed','Sign in to confirm you’re not a bot','verification_required'),
                ('_get_pot_via_script failed: HTTP 429','Video unavailable','analysis_error')):
            with self.subTest(final_error=final_error):
                failed=MagicMock(returncode=1)
                failed.communicate.return_value=('',first_error)
                fallback=MagicMock(returncode=1)
                fallback.communicate.return_value=('',final_error)
                with patch.object(m.subprocess,'Popen',side_effect=[failed,fallback]):
                    self.a._analyze_worker('url',self.a.analysis_token)
                event=self.a.events.get_nowait()
                self.assertEqual(event[0],kind)
                self.assertIn(first_error,event[2])
                message=m.readable_error(event[2])
                self.assertNotIn('générateur de jetons',message)
                self.assertNotIn('HTTP 429',message)

    def test_provider_timeout_cancellation_does_not_start_fallback(self):
        failed=MagicMock()
        def communicate(**kwargs):
            if kwargs['timeout']==2:
                self.a.analysis_token+=1
                return '', ''
            raise subprocess.TimeoutExpired('yt',40)
        failed.communicate.side_effect=communicate
        with patch.object(m.subprocess,'Popen',return_value=failed) as start, \
             patch('windows_engine.terminate_process_tree'):
            self.a._analyze_worker('url',self.a.analysis_token)
        start.assert_called_once()
        self.assertTrue(self.a.events.empty())

    def test_download_reuses_recovered_route_and_preserves_verified_credentials(self):
        self.a._process_analysis(dict(DATA,_downloader_access_recovery='without_provider'))
        credentials=m.VerifiedCredentials([])
        with self.a._access_options(credentials) as options:
            self.assertIn('--no-plugin-dirs',options)
            self.assertIn('--cookies',options)
            self.assertNotIn('youtube:player_client=mweb',options)
        with self.a._access_options(credentials,analysis=True) as options:
            self.assertIn('--plugin-dirs',options)
    def test_analysis_timeout_returns_even_when_child_keeps_stderr_open(self):
        a=self.a;p=MagicMock()
        p.communicate.side_effect=[
            subprocess.TimeoutExpired('yt',40,stderr=b'WARNING: token generation stalled'),
            subprocess.TimeoutExpired('yt',2),
        ]
        fallback=MagicMock(returncode=1)
        fallback.communicate.return_value=('', 'Video unavailable')
        with patch.object(m.subprocess,'Popen',side_effect=[p,fallback]) as start, \
             patch('windows_engine.terminate_process_tree') as stop:
            a._analyze_worker('url',a.analysis_token)
        stop.assert_called_once_with(p)
        event=a.events.get_nowait()
        self.assertEqual(event[:2],('analysis_error',a.analysis_token))
        self.assertIn('40 secondes',event[2])
        self.assertIn('token generation stalled',event[2])
        self.assertIn('Video unavailable',event[2])
        self.assertEqual(start.call_count,2)
        self.assertIsNone(a.analysis_process)

    def test_reported_auto_dub_audio_labels_and_selection(self):
        # Metadata shape observed on 3y_v1uDrMrA, with synthetic, unsigned URLs.
        original = dict(audio('140-19','en-US',129.472,100),
            format_note='English (US) original (default), medium',
            url='https://example.invalid/audio?xtags=acont%3Doriginal%3Alang%3Den-US')
        translated = dict(audio('140-4','fr-FR',129.472,100,-1),
            format_note='French (FR), medium',
            url='https://example.invalid/audio?xtags=acont%3Ddubbed-auto%3Alang%3Dfr-FR')
        a=self.a
        a._process_analysis({'language':'en-US','formats':[video('720',720,500),original,translated]})
        self.assertEqual(a._selection()['audio']['format_id'],'140-19')
        self.assertIn('langue d’origine',a.auto_summary_var.get())
        self.assertNotIn('traduction automatique',a.auto_summary_var.get())
        a.settings_var.set('manual');a._settings_changed()
        label='français (fr-FR) — traduction automatique'
        self.assertIn(label,a.audio_language_combo['values'])
        a.audio_language_var.set(label);a._audio_options_changed()
        self.assertEqual(a._selection()['audio']['format_id'],'140-4')
        a.mode_var.set('audio');a._mode_changed()
        a.settings_var.set('manual');a._settings_changed()
        a.audio_language_var.set(label);a._audio_options_changed()
        self.assertEqual(a._selection()['audio']['format_id'],'140-4')
        # When only the translated audio is available, auto mode must disclose it too.
        a.settings_var.set('auto')
        a._process_analysis({'language':'en-US','formats':[video('720',720,500),translated]})
        self.assertIn('traduction automatique',a.auto_summary_var.get())

    def test_audio_dubbing_requires_explicit_marker(self):
        a=self.a;a.original_language='en'
        unknown=audio('fr','fr',128,100,-1)
        self.assertNotIn('traduction automatique',a._audio_language_label(unknown))
        manual=dict(unknown,url='https://example.invalid/audio?xtags=acont%3Ddubbed%3Alang%3Dfr')
        self.assertTrue(a._audio_language_label(manual).endswith(' — doublage'))
        automatic=dict(manual,url='https://example.invalid/audio?xtags=acont%3Ddubbed-auto%3Alang%3Dfr')
        a.original_language='fr'
        self.assertNotIn('langue d’origine',a._audio_language_label(automatic))
        self.assertIn('traduction automatique',a._audio_language_label(automatic))
        self.assertEqual(m.audio_content_type(dict(unknown,format_note='French auto-dubbed')), 'dubbed-auto')
        self.assertIsNone(m.original_language({'formats':[dict(automatic,language_preference=0)]}))

    def test_language_sources_and_uncertainty(self):
        self.assertEqual(m.original_language({'language':'fr'}),'fr')
        self.assertEqual(m.original_language({'automatic_captions':{'de':[{'url':'https://www.youtube.com/api/timedtext?lang=fr&tlang=de'}]}}),'fr')
        data={'formats':[audio('one','fr',128,100,0)]}
        self.assertEqual(m.original_language_info(data),('fr','seule langue des pistes audio reçues',True))
        self.a._process_analysis(data)
        self.assertIn('origine non confirmée',self.a._diagnostic_text())
        self.assertIsNone(m.original_language({'subtitles':{'fr':[{'url':'caption'}]}}))
        self.assertIsNone(m.original_language({'title':'Bonjour à tous','language':'und'}))
        self.assertIsNone(m.original_language({'formats':[audio('dub','fr',128,100,-1)]}))
    def test_diagnostic_distinguishes_codec_missing_audio_and_block(self):
        a=self.a
        a._process_analysis({'formats':[video('720',720,500),dict(audio('opus','fr',128,100),acodec='opus',ext='webm')]})
        self.assertIn('Audio disponible en Opus',a.status_var.get())
        a._process_analysis({'formats':[video('720',720,500)]})
        self.assertIn('Aucun flux audio reçu',a.status_var.get())
        self.assertTrue(a.mode_buttons['video_only'].isEnabled())
        a._process_analysis({'formats':[dict(video('muxed',720,500),acodec='mp4a.40.2')]})
        a.mode_var.set('audio');a._mode_changed()
        self.assertFalse(a.mode_buttons['audio'].isEnabled())
        self.assertEqual(a.mode_var.get(),'video')
        a._process_analysis(DATA)
        self.assertTrue(a.mode_buttons['audio'].isEnabled())
        self.assertIn('antirobot',m.extraction_issue('Sign in to confirm you’re not a bot'))
        self.assertEqual(
            m.extraction_issue('HTTP Error 429'),
            'YouTube limite temporairement les requêtes (HTTP 429).')
        self.assertIn('pas confirmée',m.extraction_issue('HTTP Error 403'))
        self.assertIn('Extraction partielle',m.extraction_issue('WARNING: forcing SABR streaming'))
        a._process_analysis(dict(DATA,_downloader_diagnostics='WARNING: forcing SABR streaming'))
        self.assertIn('SABR',a._diagnostic_text())
        a._process_analysis(dict(DATA,live_status='is_upcoming'))
        self.assertIn('Direct à venir',a.status_var.get())
    def test_success_keeps_extraction_warnings(self):
        a=self.a;p=MagicMock();p.returncode=0
        p.communicate.return_value=(json.dumps(DATA),'WARNING: Some formats have been skipped; SABR streaming')
        with patch.object(m.subprocess,'Popen',return_value=p) as popen:
            a._analyze_worker('url',a.analysis_token)
        self.assertNotIn('--no-warnings',popen.call_args.args[0])
        a._poll_events()
        self.assertIn('SABR',a.analysis_diagnostics)
        self.assertIn('Extraction partielle',a.status_var.get())

    def test_successful_bgutils_recovery_does_not_show_historical_429_as_failure(self):
        a=self.a
        recovered=dict(DATA,_downloader_diagnostics=(
            'WARNING: Unable to download webpage: HTTP Error 429\n'
            '[youtube] [pot:bgutil:script-node] Generating a gvs PO Token\n'
            '[youtube] Retrieved a gvs PO Token'))
        a.events.put(('analysis_ok',a.analysis_token,recovered));a._poll_events()
        self.assertEqual(a.status_var.get(),'Analyse terminée')
        self.assertEqual(a.title_var.get(),'Vidéo test')
        self.assertTrue(a.analysis_retry_button.isHidden())
        self.assertTrue(a.verification_button.isHidden())
        self.assertIn('Accès rétabli par BgUtils',a.access_note_var.get())
    def test_partial_extraction_retries_once_then_offers_manual_retry(self):
        a=self.a
        url='https://www.youtube.com/watch?v=urFIHf5coxE'
        a.url_var.set(url)
        if a.analysis_job:
            a.after_cancel(a.analysis_job);a.analysis_job=None
        partial=dict(DATA,_downloader_diagnostics='WARNING: forcing SABR streaming')
        with patch.object(a,'after',return_value='automatic-retry') as schedule:
            self.assertTrue(a._schedule_partial_retry(partial))
        self.assertEqual(a.analysis_job,'automatic-retry')
        self.assertEqual(schedule.call_args.args[0],1200)
        self.assertFalse(a._schedule_partial_retry(partial))
        a._process_analysis(partial)
        self.assertFalse(a.analysis_retry_button.isHidden())
        a._retry_analysis()
        self.assertNotIn(url,a.partial_retry_attempted)

    def test_360p_only_analysis_retries_twice_then_warns(self):
        a=self.a
        url='https://www.youtube.com/watch?v=urFIHf5coxE'
        a.url_var.set(url)
        if a.analysis_job:
            a.after_cancel(a.analysis_job);a.analysis_job=None
        low=dict(DATA,formats=[video('18',360,400),audio('140','en',128,100)])
        a.verified_credentials=m.VerifiedCredentials([])
        with patch.object(a,'after',side_effect=['quality-1','quality-2']) as schedule:
            self.assertTrue(a._schedule_quality_retry(low))
            self.assertTrue(a._schedule_quality_retry(low))
        self.assertEqual([call.args[0] for call in schedule.call_args_list],[1500,5000])
        self.assertFalse(a._schedule_quality_retry(low))
        self.assertIn(url,a.analysis_without_verified_session)
        a._process_analysis(low)
        self.assertIn('seulement 360p',a.status_var.get())
        self.assertFalse(a.analysis_retry_button.isHidden())

    def test_rate_limit_retries_twice_and_can_run_immediately(self):
        a=self.a
        url='https://www.youtube.com/watch?v=urFIHf5coxE'
        a.url_var.set(url)
        if a.analysis_job:
            a.after_cancel(a.analysis_job);a.analysis_job=None
        with patch.object(a,'after',side_effect=['retry-1','retry-2']) as schedule:
            self.assertTrue(a._schedule_rate_limit_retry('HTTP Error 429: Too Many Requests'))
            self.assertTrue(a._schedule_rate_limit_retry('HTTP Error 429'))
        self.assertEqual([call.args[0] for call in schedule.call_args_list],[5000,15000])
        self.assertFalse(a._schedule_rate_limit_retry('HTTP Error 429'))
        self.assertFalse(a.analysis_retry_button.isHidden())
        a.analysis_job='waiting'
        with patch.object(a,'after_cancel') as cancel,patch.object(a,'_start_analysis') as start:
            a._retry_analysis_now()
        cancel.assert_called_once_with('waiting')
        start.assert_called_once()
    def test_automatic_provider_configuration(self):
        options=m.access_args(False,True)
        self.assertIn('youtube:player_client=mweb',options)
        self.assertIn('--plugin-dirs',options)
        self.assertTrue(any('server_home=' in item for item in options))
        self.assertFalse(hasattr(self.a,'verify_button'))
        self.assertFalse(hasattr(self.a,'access_combo'))
        with self.assertRaises(RuntimeError):provider_options(ROOT/'missing-provider')
        self.assertEqual(m.extraction_issue('Generating a gvs PO Token for mweb client'), '')
    def test_dependencies_offer_repair_and_gate_download(self):
        from dependency_manager import Dependency
        a=self.a
        missing=[Dependency('node','Node',False,'Absent')]
        with patch.object(a,'_check_dependencies'):
            a._dependencies_finished(missing,False)
        self.assertTrue(a.dependency_dialog.isModal())
        self.assertFalse(a.dependency_alert_label.isHidden())
        self.assertFalse(hasattr(a,'dependency_summary_label'))
        a.dependency_dialog.hide()
        self.assertTrue(a.dependency_blocked)
        self.assertFalse(a.download_button.isEnabled())
        self.assertTrue(a.dependency_install_button.isEnabled())
        ready=[Dependency('node','Node',True,'v24.21.0')]
        a._dependencies_finished(ready,False)
        self.assertFalse(a.dependency_blocked)
        self.assertFalse(a.dependency_install_button.isEnabled())
        self.assertIn('1/1',a.dependency_summary_var.get())
    def test_reported_live_metadata(self):
        data=json.loads((FIXTURES/'live-metadata.json').read_text(encoding='utf-8'))
        data['_downloader_diagnostics']=''
        a=self.a;a.mode_var.set('video_only');a._mode_changed();a._process_analysis(data)
        self.assertIsNotNone(a._selection())
        self.assertFalse(a.mode_buttons['audio'].isEnabled())
        self.assertFalse(a.mode_buttons['video_only'].isEnabled())
        self.assertEqual(a.mode_var.get(),'video')
        self.assertIn('En direct',a.status_var.get())
        self.assertIsNone(a.original_language)
        self.assertIn('audio intégré',a.status_var.get())
        a._process_analysis(DATA)
        self.assertTrue(a.mode_buttons['audio'].isEnabled())
        self.assertTrue(a.mode_buttons['video_only'].isEnabled())

    def test_video_clip_finalizer_removes_the_leading_ghost_section(self):
        ghost=ROOT/'ghost-section.mkv'
        subprocess.run([
            self.a.ffmpeg,'-y','-v','error','-itsoffset','0.5','-i',str(FIXTURES/'video.mp4'),
            '-i',str(FIXTURES/'audio.m4a'),'-map','0:v:0','-map','1:a:0','-c','copy',str(ghost),
        ],check=True,creationflags=m.CREATE_NO_WINDOW)
        self.a.cancel_requested=False
        original=m.subprocess.Popen
        commands=[]
        def capture(command,**kwargs):
            commands.append(command)
            return original(command,**kwargs)
        with patch.object(m.subprocess,'Popen',side_effect=capture):
            result=self.a._finalize_video_clip(str(ghost),str(ROOT),1.0)
        command=commands[-1]
        self.assertIn('copy',command)
        self.assertNotIn('libx264',command)
        self.assertNotIn('-filter_complex',command)
        ffprobe=self.a.ffmpeg.replace('ffmpeg.exe','ffprobe.exe')
        probe=json.loads(subprocess.check_output([
            ffprobe,'-v','error','-show_entries','format=start_time,duration',
            '-show_entries','stream=codec_type,start_time','-of','json',result,
        ],text=True,creationflags=m.CREATE_NO_WINDOW))
        self.assertLess(max(float(stream['start_time']) for stream in probe['streams']),0.1)
        self.assertAlmostEqual(float(probe['format']['duration']),1.0,delta=0.2)
        first=json.loads(subprocess.check_output([
            ffprobe,'-v','error','-select_streams','v:0','-read_intervals','%+1',
            '-show_entries','frame=key_frame,best_effort_timestamp_time','-of','json',result,
        ],text=True,creationflags=m.CREATE_NO_WINDOW))['frames'][0]
        self.assertLess(float(first['best_effort_timestamp_time']),0.1)
        self.assertTrue(first['key_frame'])

    def test_real_ytdlp_progress_and_mkv(self):
        fixtures=FIXTURES
        class Handler(http.server.SimpleHTTPRequestHandler):
            def log_message(self,*args):pass
        server=http.server.ThreadingHTTPServer(('127.0.0.1',0),functools.partial(Handler,directory=str(fixtures)))
        thread=threading.Thread(target=server.serve_forever,daemon=True);thread.start()
        base=f'http://127.0.0.1:{server.server_port}'
        data={'id':'feature-fixture','title':'Feature fixture','extractor':'generic','extractor_key':'Generic',
              'webpage_url':base+'/video.mp4','formats':[
                  {'format_id':'video','url':base+'/video.mp4','ext':'mp4','vcodec':'avc1','acodec':'none','protocol':'http'},
                  {'format_id':'audio','url':base+'/audio.m4a','ext':'m4a','vcodec':'none','acodec':'mp4a.40.2','protocol':'http'}]}
        info=ROOT/'local-info.json';info.write_text(json.dumps(data))
        original=m.subprocess.Popen
        def sample(cmd,**kw):
            if os.path.normcase(cmd[0]) == os.path.normcase(self.a.ytdlp):
                cmd=cmd[:-1]+['--ignore-config','--force-overwrites','--load-info-json',str(info)]
            return original(cmd,**kw)
        try:
            with patch.object(m.subprocess,'Popen',side_effect=sample):
                self.a._download_worker(base,str(ROOT),'video','video+audio',None,'mkv',(0.5,1.5))
        finally:server.shutdown();server.server_close();thread.join()
        events=[]
        while not self.a.events.empty():events.append(self.a.events.get_nowait())
        self.assertFalse([e for e in events if e[0]=='download_error'],events)
        progress=[e[1] for e in events if e[0]=='download_progress' and e[1] is not None]
        self.assertGreaterEqual(len(progress),1)
        self.assertEqual(progress,sorted(progress))
        self.assertGreater(progress[-1],0)
        done=next(event for event in events if event[0]=='download_done')
        self.assertTrue(done[1] and done[1][0].endswith('.mkv'),events)
        result=ROOT/'Feature fixture.mkv'
        self.assertTrue(result.is_file())
        probe=subprocess.check_output([self.a.ffmpeg.replace('ffmpeg.exe','ffprobe.exe'),'-v','error',
            '-select_streams','v:0','-show_entries','frame=key_frame,best_effort_timestamp_time',
            '-of','json',str(result)],text=True,creationflags=m.CREATE_NO_WINDOW)
        frames=json.loads(probe)['frames']
        self.assertTrue(frames and frames[0]['key_frame'],frames[:3])
        duration=float(subprocess.check_output([self.a.ffmpeg.replace('ffmpeg.exe','ffprobe.exe'),'-v','error',
            '-show_entries','format=duration','-of','default=nw=1:nk=1',str(result)],
            text=True,creationflags=m.CREATE_NO_WINDOW).strip())
        self.assertLessEqual(duration,1.25)
if __name__=='__main__':unittest.main(verbosity=2)
