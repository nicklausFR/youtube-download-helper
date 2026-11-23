"""Download list and the format choices available from each row."""
from qt_i18n import bind_text, ui, add_translated, message

from PySide6.QtCore import QPoint, QSize, QSignalBlocker, Qt
from PySide6.QtGui import QColor, QDrag, QFont, QIcon
from PySide6.QtWidgets import (
    QAbstractItemView, QDialog, QDialogButtonBox, QFormLayout, QFrame, QHBoxLayout, QHeaderView,
    QListWidget, QListWidgetItem, QMenu, QStyle,
    QStyledItemDelegate, QStyleOptionProgressBar, QTreeWidget, QTreeWidgetItem, QTreeWidgetItemIterator,
)

from core_formats import (LANGUAGE_NAMES, audio_bitrate, audio_content_type, codec_name, estimated_bytes,
                          human_size, language_base, original_language_info, subtitle_choices)
from core_progress import remaining_time_label
from core_selection import clip_range, select_formats
from qt_controls import ArrowCombo
from youtube_access import youtube_video_id


ROLE = Qt.ItemDataRole.UserRole
CHECKED = Qt.CheckState.Checked
UNCHECKED = Qt.CheckState.Unchecked
DEFAULT_FORMATS = {'container': 'Auto', 'video': 'auto', 'audio': 'original', 'subtitles': []}
MEDIA_GROUPS = {'video': 'Vidéo', 'audio': 'Audio', 'subtitle': 'Sous-titres (SRT)'}
VIDEO, AUDIO, SUBTITLES = 7, 8, 9
STREAM_COLUMNS = dict(zip((VIDEO, AUDIO, SUBTITLES), MEDIA_GROUPS))


def language_name(code):
    return LANGUAGE_NAMES.get(language_base(code), code).capitalize() if code else 'Langue inconnue'


class FormatDefaultsDialog(QDialog):
    def __init__(self, window, preset):
        super().__init__(window)
        bind_text(self, 'setWindowTitle', 'Réglages par défaut')
        self.resize(460, 410)
        layout = window._layout(self, margins=16, spacing=10)
        layout.addWidget(window._label(text='Ces choix s’appliquent aux prochains liens ajoutés.', name='muted', wrap=True))
        fields = QFormLayout()
        layout.addLayout(fields)
        self.container = ArrowCombo()
        self.video = ArrowCombo()
        self.video.addItem('Automatique', 'auto')
        for height in (360, 480, 720, 1080, 1440, 2160, 4320):
            self.video.addItem(f'{height}p si disponible', str(height))
        self.video.addItem('Aucune vidéo', 'off')
        self.audio = ArrowCombo()
        self.audio.addItem('Piste originale', 'original')
        self.audio.addItem('Aucun audio', 'off')
        languages = sorted(LANGUAGE_NAMES, key=language_name)
        for code in languages:
            self.audio.addItem(language_name(code) + ' si disponible', code)
        for field, label, key in ((self.container, 'Conteneur', 'container'),
                                  (self.video, 'Vidéo', 'video'), (self.audio, 'Audio', 'audio')):
            bind_text(field, 'setAccessibleName', label + ' par défaut')
            fields.addRow(label, field)
            field.setCurrentIndex(max(0, field.findData(preset[key])))
        layout.addWidget(window._label(text='Sous-titres', name='fieldLabel'))
        self.subtitles = QListWidget()
        self.subtitles.setObjectName('defaultSubtitleLanguages')
        bind_text(self.subtitles, 'setAccessibleName', 'Langues des sous-titres par défaut')
        self.subtitles.setAlternatingRowColors(True)
        for code in ('original', *languages):
            item = ui(QListWidgetItem(self.subtitles), text='Langue d’origine' if code == 'original' else language_name(code))
            item.setData(ROLE, code)
            item.setCheckState(CHECKED if code in preset['subtitles'] else UNCHECKED)
        layout.addWidget(self.subtitles, 1)
        self.validation = window._label(text='Choisissez au moins un média ou une langue de sous-titres.', name='muted', wrap=True)
        layout.addWidget(self.validation)
        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Save | QDialogButtonBox.StandardButton.Cancel)
        self.save = buttons.button(QDialogButtonBox.StandardButton.Save)
        bind_text(self.save, 'setText', 'Enregistrer')
        bind_text(buttons.button(QDialogButtonBox.StandardButton.Cancel), 'setText', 'Annuler')
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)
        self.update_containers(preferred=preset['container'])
        self.video.currentIndexChanged.connect(self.update_containers)
        self.audio.currentIndexChanged.connect(self.update_containers)
        self.subtitles.itemChanged.connect(self.validate)

    def update_containers(self, *_args, preferred=None):
        previous = preferred or self.container.currentData()
        audio_only = self.video.currentData() == 'off'
        srt_only = audio_only and self.audio.currentData() == 'off'
        choices = ('Auto',) if srt_only else (
            ('Auto', 'M4A', 'MKA', 'WebM') if audio_only else ('Auto', 'MKV', 'MP4', 'WebM'))
        self.container.clear()
        for value in choices:
            self.container.addItem(value, value)
        self.container.setCurrentIndex(max(0, self.container.findData(previous)))
        self.container.setEnabled(not srt_only)
        self.validate()

    def validate(self, *_args):
        choices = self.choices()
        valid = choices['audio'] != 'off' or choices['video'] != 'off' or bool(choices['subtitles'])
        self.validation.setVisible(not valid)
        self.save.setEnabled(valid)

    def choices(self):
        return dict(container=self.container.currentData(), video=self.video.currentData(), audio=self.audio.currentData(),
                    subtitles=[self.subtitles.item(index).data(ROLE) for index in range(self.subtitles.count())
                               if self.subtitles.item(index).checkState() == CHECKED])


class ProgressDelegate(QStyledItemDelegate):
    def paint(self, painter, option, index):
        progress = index.data(ROLE)
        if progress is None:
            return super().paint(painter, option, index)
        style = option.widget.style()
        cell = type(option)(option)
        self.initStyleOption(cell, index)
        cell.text = ''
        style.drawControl(QStyle.ControlElement.CE_ItemViewItem, cell, painter, option.widget)
        bar = QStyleOptionProgressBar()
        bar.rect = option.rect.adjusted(6, 6, -6, -6)
        bar.palette = option.palette
        bar.minimum, bar.maximum, bar.progress = 0, 100, int(progress)
        bar.text, bar.textVisible = f'{int(progress)} %', True
        bar.textAlignment = Qt.AlignmentFlag.AlignCenter
        style.drawControl(QStyle.ControlElement.CE_ProgressBar, bar, painter, option.widget)


class DownloadOrderTree(QTreeWidget):
    def __init__(self, move_download):
        super().__init__()
        self.move_download = move_download
        self.dragged_url = None
        self.setDragDropMode(QAbstractItemView.DragDropMode.InternalMove)
        self.setDefaultDropAction(Qt.DropAction.MoveAction)
        self.setDropIndicatorShown(True)
        self.setDragDropOverwriteMode(False)

    def startDrag(self, _actions):
        item = self.currentItem()
        if item is None:
            return
        # The controller moves the row on drop. The default QTreeWidget drag can
        # also remove the source item after a MoveAction.
        drag = QDrag(self)
        drag.setMimeData(self.model().mimeData([self.indexFromItem(item, 0)]))
        rect = self.visualItemRect(item)
        rect.setWidth(self.columnWidth(0))
        drag.setPixmap(self.viewport().grab(rect))
        drag.setHotSpot(QPoint(16, rect.height() // 2))
        self.dragged_url = item.data(0, ROLE)
        try:
            drag.exec(Qt.DropAction.MoveAction)
        finally:
            self.dragged_url = None

    def dropEvent(self, event):
        if event.source() is not self or self.dragged_url is None:
            event.ignore()
            return
        source = next((i for i in range(self.topLevelItemCount())
                       if self.topLevelItem(i).data(0, ROLE) == self.dragged_url), None)
        if source is None:
            event.ignore()
            return
        point = event.position().toPoint()
        target = self.itemAt(point)
        destination = self.topLevelItemCount()
        if target is not None:
            destination = self.indexOfTopLevelItem(target)
            if point.y() >= self.visualItemRect(target).center().y():
                destination += 1
        if destination > source:
            destination -= 1
        self.move_download(self.dragged_url, destination)
        event.setDropAction(Qt.DropAction.MoveAction)
        event.accept()


class DownloadTable(QFrame):
    def __init__(self, window):
        super().__init__(window)
        self.window = window
        self.rows = {}
        self.stream_refresh_pending = False
        self.setObjectName('downloadTable')
        layout = window._layout(self, margins=12, spacing=7)
        self.toolbar = window._layout(None, kind=QHBoxLayout, spacing=6)
        layout.addLayout(self.toolbar)
        self.toolbar.addWidget(window._button('+ Ajouter un lien', window._new_link, 'primary'))
        self.toolbar.addStretch(1)
        self.defaults = window._button('Réglages par défaut…', self.open_defaults)
        self.toolbar.addWidget(self.defaults)
        self.clear = window._button('Effacer les terminés', self.clear_completed, 'quiet')
        self.toolbar.addWidget(self.clear)

        self.tree = DownloadOrderTree(window._move_download_list_item)
        self.tree.setObjectName('videoList')
        bind_text(self.tree, 'setAccessibleName', 'Téléchargements. Glissez une ligne pour changer son ordre. Les colonnes Vidéo, Audio et Sous-titres ouvrent les flux.')
        bind_text(self.tree, 'setHeaderLabels', ['Nom', 'Durée', 'Taille', 'État', 'Progression', 'Vitesse', 'Restant',
                                   'Vidéo', 'Audio', 'Sous-titres'])
        self.tree.setRootIsDecorated(False)
        self.tree.setItemsExpandable(False)
        self.tree.setIconSize(QSize(32, 18))
        self.tree.setAlternatingRowColors(True)
        self.tree.setExpandsOnDoubleClick(False)
        self.tree.setAllColumnsShowFocus(True)
        self.tree.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectItems)
        self.tree.setTextElideMode(Qt.TextElideMode.ElideRight)
        self.tree.setItemDelegateForColumn(4, ProgressDelegate(self.tree))
        header = self.tree.header()
        header.setSectionResizeMode(0, QHeaderView.ResizeMode.Interactive)
        try:
            title_width = int(window.preferences.value('download_title_column_width', 320))
        except (ValueError, TypeError):
            title_width = 320
        self.tree.setColumnWidth(0, max(header.minimumSectionSize(), min(4000, title_width)))
        for column, width in enumerate((63, 86, 126, 94, 73, 64, 137, 142, 133), 1):
            self.tree.setColumnWidth(column, width)
        for position, column in enumerate(STREAM_COLUMNS, 1):
            header.moveSection(header.visualIndex(column), position)
        header.setStretchLastSection(True)
        header.sectionResized.connect(self.save_title_column_width)
        self.tree.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        self.tree.customContextMenuRequested.connect(self.open_menu)
        self.tree.currentItemChanged.connect(self.select)
        self.tree.itemClicked.connect(self.open_stream_cell)
        self.tree.itemActivated.connect(self.open_stream_cell)
        layout.addWidget(self.tree, 1)
        self.empty = window._label(text='Ajoutez un lien YouTube pour commencer.', name='muted')
        layout.addWidget(self.empty)

        self.stream_popup = QFrame(self, Qt.WindowType.Popup)
        self.stream_popup.setObjectName('streamChoices')
        self.stream_popup.hide()
        popup_layout = window._layout(self.stream_popup, margins=6, spacing=0)
        self.stream_tree = QTreeWidget()
        self.stream_tree.setObjectName('videoList')
        bind_text(self.stream_tree, 'setHeaderLabels', ['Flux disponibles', 'Taille', 'État'])
        self.stream_tree.setIndentation(16)
        self.stream_tree.setRootIsDecorated(False)
        self.stream_tree.setItemsExpandable(False)
        self.stream_tree.setAlternatingRowColors(True)
        self.stream_tree.header().setStretchLastSection(False)
        self.stream_tree.header().setSectionResizeMode(0, QHeaderView.ResizeMode.Stretch)
        self.stream_tree.setColumnWidth(1, 90)
        self.stream_tree.setColumnWidth(2, 106)
        self.stream_tree.itemChanged.connect(self.change_stream)
        popup_layout.addWidget(self.stream_tree)
        self.stream_url = None
        self.stream_kind = None
        self.stream_source = None
        self.completed_details_url = None

    def save_title_column_width(self, section, _previous, width):
        if section == 0:
            self.window.preferences.setValue('download_title_column_width', width)

    def selected_url(self):
        item = self.tree.currentItem()
        return item.data(0, ROLE) if item else None

    def state(self, url, data):
        w = self.window
        if url == w.active_queue_url and w.active_download_job:
            if w.download_retry_pending:
                return 'retry', 'En pause / À reprendre'
            if w.download_verification_pending:
                return 'active', 'Vérification'
            if w.subtitle_retry_pending:
                return 'active', 'Sous-titres à reprendre'
            if w.download_pause_requested:
                return 'active', 'Mise en pause…'
            return 'active', 'En cours'
        if url in w.queued_downloads:
            position = list(w.queued_downloads).index(url) + 1
            return 'waiting', f'En attente · {position}'
        if url in w.completed_downloads:
            record = w.completed_downloads[url]
            return 'done', 'Terminé · SRT manquants' if record.get('partial') else '✓ Terminé'
        if url in w.queue_analysis_errors:
            return 'ready', 'Analyse échouée'
        if url in (w.queue_analysis_url, w.active_analysis_key_url()) or (
                url == w.url_var.get().strip() and w._analysis_indicator_active):
            return 'ready', 'Analyse en cours…'
        return ('ready', 'Analysé') if data else ('ready', 'À préparer')

    def row_data(self, url):
        w = self.window
        job = w.queued_downloads.get(url)
        if url == w.active_queue_url and w.active_download_job:
            job = w.active_download_job
        data = w.tab_analysis_data.get((url, w.access_var.get()))
        if job and not data:
            data = job['data']
        if not job and url in w.completed_downloads:
            data = w.completed_downloads[url]['data']
        return data or {}, job

    def refresh(self, order):
        w = self.window
        with QSignalBlocker(self.tree):
            for url in self.rows.keys() - set(order):
                item = self.rows.pop(url)
                self.tree.takeTopLevelItem(self.tree.indexOfTopLevelItem(item))
            previous = self.tree.currentItem()
            column = self.tree.currentColumn()
            for index, url in enumerate(order):
                item = self.rows.get(url)
                if item is None:
                    item = self.rows[url] = QTreeWidgetItem()
                    item.setData(0, ROLE, url)
                    item.setFlags((item.flags() | Qt.ItemFlag.ItemIsDragEnabled) & ~Qt.ItemFlag.ItemIsDropEnabled)
                    font = QFont(self.tree.font())
                    font.setWeight(QFont.Weight.DemiBold)
                    item.setFont(0, font)
                    self.tree.insertTopLevelItem(index, item)
                current_index = self.tree.indexOfTopLevelItem(item)
                if current_index != index:
                    self.tree.takeTopLevelItem(current_index)
                    self.tree.insertTopLevelItem(index, item)
                self.refresh_row(item, url)
            url = w.completed_selection or w.queue_view_url
            if item := self.rows.get(url):
                self.tree.setCurrentItem(item, max(0, column) if previous is item else 0)
            else:
                self.tree.setCurrentItem(None)
        self.empty.setVisible(not order)
        self.clear.setEnabled(bool(w.completed_downloads))
        self.refresh_details()
        if self.stream_popup.isVisible():
            self.refresh_stream_popup()

    def refresh_row(self, item, url):
        w = self.window
        data, job = self.row_data(url)
        state, label = self.state(url, data)
        feedback = w._list_reanalysis_status(url)
        video_id = data.get('id') or youtube_video_id(url)
        title = data.get('title') or (job or {}).get('title') or w.tab_preview_titles.get(video_id) or url
        duration = data.get('duration')
        item.setText(0, title)
        bind_text(item, 'setToolTip', 0, message('%1\n%2\n%3', title, url,
                  message('Glissez la ligne pour changer sa position.')))
        pixmap = w._thumbnail_pixmap(w.thumbnail_cache.get(video_id), QSize(32, 18))
        item.setIcon(0, QIcon(pixmap) if pixmap is not None and not pixmap.isNull() else QIcon())
        bind_text(item, 'setText', 1, remaining_time_label(duration) if isinstance(duration, (int, float)) and duration > 0 else '—')
        streams = self.selected_streams(url, data, job) if data else None
        size = '—'
        if state == 'done':
            size = w.completed_downloads[url].get('size') or '—'
        elif job:
            size = w._list_job_size(job)
        elif streams:
            mode, video, audios, subtitles = streams
            selection = select_formats(mode, video, audios, ','.join(subtitles))
            saved = w.queue_tab_settings.get(url, {})
            clip = w._clip_range() if w._list_can_configure(url) else clip_range(
                saved.get('settings', 'auto'), mode, saved.get('clip_start', ''), saved.get('clip_end', ''), duration)
            size = w._list_size(selection, mode, duration, clip, bool(subtitles))
        bind_text(item, 'setText', 2, size)
        bind_text(item, 'setToolTip', 2, size if size != '—' else '')
        bind_text(item, 'setText', 3, feedback if state == 'ready' and feedback else label)
        bind_text(item, 'setToolTip', 3, '\n'.join(filter(None, (label, feedback))))
        item.setData(3, ROLE, state)
        if state == 'done':
            color = '#6dd6a4' if self.palette().base().color().lightness() < 128 else '#167147'
            item.setForeground(3, QColor(color))
        else:
            item.setData(3, Qt.ItemDataRole.ForegroundRole, None)
        self.refresh_stream_cells(item, streams)
        self.refresh_progress(item)

    def refresh_progress(self, item=None):
        w = self.window
        if item is None:
            item = self.rows.get(w.active_queue_url)
        if item is None:
            return
        state = item.data(3, ROLE)
        job = w.queued_downloads.get(item.data(0, ROLE), {})
        progress = 100 if state == 'done' else (
            w.download_progress_value if state in ('active', 'retry') else job.get('progress'))
        item.setData(4, ROLE, progress)
        bind_text(item, 'setText', 4, f'{int(progress)} %' if progress is not None else '—')
        speed = w.list_transfer_speed if state == 'active' else None
        eta = w.list_transfer_eta if state == 'active' else None
        bind_text(item, 'setText', 5, human_size(speed) + '/s' if speed else '—')
        bind_text(item, 'setText', 6, remaining_time_label(eta) if eta else '—')

    def open_stream_cell(self, item, column):
        kind = STREAM_COLUMNS.get(column)
        url = item.data(0, ROLE)
        if not kind or not self.row_data(url)[0] or self.stream_popup.isVisible():
            return
        self.tree.setCurrentItem(item, column)
        self.stream_url, self.stream_kind = url, kind
        self.stream_source = None
        bind_text(self.stream_tree, 'setAccessibleName', MEDIA_GROUPS[kind] + ' · flux disponibles')
        self.refresh_stream_popup()
        rect = self.tree.visualRect(self.tree.indexFromItem(item, column))
        viewport = self.tree.viewport()
        anchor = viewport.mapToGlobal(rect.bottomLeft())
        top = viewport.mapToGlobal(rect.topLeft())
        screen = self.tree.screen().availableGeometry()
        width = min(760, screen.width())
        count = 0
        iterator = QTreeWidgetItemIterator(self.stream_tree)
        while iterator.value():
            count += 1
            iterator += 1
        row_height = max(30, self.stream_tree.sizeHintForRow(0))
        height = min(430, (count + 1) * row_height + 20, screen.height())
        y = anchor.y() if anchor.y() + height <= screen.bottom() else top.y() - height
        self.stream_popup.setGeometry(min(anchor.x(), screen.right() - width + 1), max(screen.top(), y), width, height)
        self.stream_popup.show()
        self.stream_tree.setFocus()

    def refresh_stream_popup(self):
        url, kind = self.stream_url, self.stream_kind
        if url != self.selected_url() or url not in self.rows:
            self.stream_popup.hide()
            return
        data, job = self.row_data(url)
        if not data:
            self.stream_popup.hide()
            return
        with QSignalBlocker(self.stream_tree):
            if data is not self.stream_source:
                self.stream_tree.clear()
                group = self.stream_heading(self.stream_tree, MEDIA_GROUPS[kind], ('group', kind))
                self.populate_streams(group, kind, data)
                self.stream_tree.expandAll()
                self.stream_tree.scrollToTop()
                self.stream_source = data
            self.mark_streams(self.stream_tree.topLevelItem(0), url, data, job)

    def populate_streams(self, group, kind, data):
        if kind == 'subtitle':
            tracks = data.get('_saved_subtitle_tracks')
            if tracks is None:
                language, _, inferred = data.get('_saved_language_info') or original_language_info(data)
                tracks = subtitle_choices(data, None if inferred else language)
            for provenance, label in (('', 'Sous-titres publiés'), ('transcription auto', 'Transcriptions automatiques'),
                                      ('traduction auto', 'Traductions automatiques')):
                entries = [track for track in tracks if (track.get('kind') or '') == provenance]
                if entries:
                    heading = self.stream_heading(group, label)
                    for track in entries:
                        self.add_stream(heading, (kind, track['code']), track['label'], '—')
            return

        formats = sorted((fmt for fmt in data.get('formats') or () if fmt.get('format_id')),
                         key=lambda fmt: (-(fmt.get('height') or 0), -(audio_bitrate(fmt) or 0)))
        audio_sections = {'normal': [], 'automatic': []}
        for fmt in formats:
            height, video, audio = fmt.get('height'), fmt.get('vcodec'), fmt.get('acodec')
            if kind == 'video' and height and video not in (None, 'none'):
                label = f'{height}p · {codec_name(video)}'
                if fps := fmt.get('fps'):
                    label += f' · {fps:g} i/s'
                if fmt.get('dynamic_range') not in (None, 'SDR'):
                    label += f" · {fmt['dynamic_range']}"
                if audio not in (None, 'none'):
                    label += f' · audio {codec_name(audio)} inclus'
            elif kind == 'audio' and video == 'none' and audio not in (None, 'none'):
                label = f'{self.window._audio_language_label(fmt)} · {codec_name(audio)}'
                if rate := audio_bitrate(fmt):
                    label += f' · {rate:.0f} kbit/s'
            else:
                continue
            if ext := fmt.get('ext'):
                label += f' · {ext.upper()}'
            size = estimated_bytes(fmt, data.get('duration'))
            row = ((kind, str(fmt['format_id'])), label, f'≈ {human_size(size)}' if size else '—')
            if kind == 'video':
                self.add_stream(group, *row)
            else:
                section = 'automatic' if audio_content_type(fmt) == 'dubbed-auto' else 'normal'
                audio_sections[section].append(row)
        for section, label in (('normal', 'Pistes d’origine et doublages classiques'),
                               ('automatic', 'Doublages automatiques')):
            if audio_sections[section]:
                heading = self.stream_heading(group, label)
                for row in audio_sections[section]:
                    self.add_stream(heading, *row)

    def stream_heading(self, parent, label, key=None):
        item = QTreeWidgetItem(parent, [label])
        bind_text(item, 'setText', 0, label)
        font = QFont(self.stream_tree.font())
        font.setWeight(QFont.Weight.DemiBold)
        item.setFont(0, font)
        if key:
            item.setData(0, ROLE, key)
        return item

    @staticmethod
    def add_stream(parent, stream_key, label, size):
        item = QTreeWidgetItem(parent, [label, size])
        bind_text(item, 'setText', 0, label)
        bind_text(item, 'setText', 1, size)
        item.setData(0, ROLE, stream_key)
        bind_text(item, 'setToolTip', 0, label)

    def selected_streams(self, url, data, job):
        w = self.window
        record = w.completed_downloads.get(url) if not job else None
        editing = w._list_can_configure(url) and (not job or w.editing_queued_url == url)
        if editing:
            video, audios = w._list_selected_formats()
            mode = w.mode_var.get()
            subtitles = (w._selected_subtitle() or '').split(',')
        elif job:
            selection = job['selection']
            video = selection.get('video')
            audios = selection.get('audios') or [selection.get('audio')]
            mode = job['request'][2]
            subtitles = (job['request'][4] or '').split(',')
        else:
            saved = w.queue_tab_settings.get(url, {})
            if not record and (not saved or 'list_defaults' in saved):
                return w._list_default_streams(data, saved.get('list_defaults', DEFAULT_FORMATS))
            mode = record.get('mode') if record else saved.get('mode')
            if record:
                selected_ids = set(record.get('formats', ()))
            else:
                selected_ids = set(saved.get('audio_ids', ())) if mode in ('video', 'audio') else set()
                if mode in ('video', 'video_only') and saved.get('video_id'):
                    selected_ids.add(saved['video_id'])
            selected = [fmt for fmt in data.get('formats') or () if str(fmt.get('format_id')) in selected_ids]
            video = next((fmt for fmt in selected if fmt.get('height')), None)
            audios = [fmt for fmt in selected if fmt.get('vcodec') == 'none']
            if record and not mode:
                mode = ('video' if video and (audios or video.get('acodec') not in (None, 'none'))
                        else 'video_only' if video else 'audio' if audios else 'subtitles')
            subtitles = (record or saved).get('subtitles', ()) if mode != 'audio' else ()
        return mode, video, [fmt for fmt in audios if fmt], list(dict.fromkeys(filter(None, subtitles)))

    def refresh_stream_cells(self, item, streams):
        if streams is None:
            labels = ('—', '—', '—')
        else:
            mode, video, audios, subtitles = streams
            video_label = f"{video['height']}p · {codec_name(video.get('vcodec'))}" if video else 'Aucune'
            if len(audios) > 1:
                audio_label = f'{len(audios)} pistes'
            elif audios:
                audio = audios[0]
                detail = 'Auto' if audio_content_type(audio) == 'dubbed-auto' else codec_name(audio.get('acodec'))
                audio_label = f"{language_name(audio.get('language'))} · {detail}"
            else:
                audio_label = 'Inclus' if mode == 'video' and video and video.get('acodec') not in (None, 'none') else 'Aucun'
            names = ['Original' if code == 'original' else language_name(code) for code in subtitles]
            subtitle_label = ('Aucun' if not names else ', '.join(names) if len(names) <= 2 else f'{len(names)} pistes')
            labels = video_label, audio_label, subtitle_label
        for column, label in zip(STREAM_COLUMNS, labels):
            bind_text(item, 'setText', column, label + ('  ▾' if streams else ''))
            bind_text(item, 'setToolTip', column, label + ('\nAfficher les flux' if streams else ''))

    def mark_streams(self, group, url, data, job):
        w = self.window
        mode, video, audios, subtitles = self.selected_streams(url, data, job)
        can_edit = w._list_can_configure(url)
        kind = self.stream_kind
        checked = (mode in ('video', 'video_only') if kind == 'video' else
                   mode in ('video', 'audio') if kind == 'audio' else bool(subtitles))
        can_toggle = can_edit and w._list_can_toggle_group(kind, not checked)
        self.mark_checkbox(group, checked, can_toggle)
        formats = {str(fmt.get('format_id')) for fmt in (video, *audios) if fmt}
        editable = w._list_editable_streams() if can_edit else set()
        selected_count = 0
        for leaf in self.stream_leaves(group):
            key = leaf.data(0, ROLE)
            chosen = key[1] in (subtitles if kind == 'subtitle' else formats)
            selected_count += chosen
            selectable = key in editable
            self.mark_checkbox(leaf, chosen, selectable)
            bind_text(leaf, 'setText', 2, 'Sélectionné' if chosen else 'Disponible')
            bind_text(leaf, 'setToolTip', 0, leaf.text(0) + ('\nNon sélectionnable avec les réglages actuels.'
                            if can_edit and not selectable and not chosen else ''))
        embedded = kind == 'audio' and checked and video and video.get('acodec') not in (None, 'none')
        bind_text(group, 'setText', 2, 'Audio intégré' if embedded and not selected_count else
                      '1 flux retenu' if selected_count == 1 else f'{selected_count} flux retenus' if selected_count else
                      'Aucun flux' if not group.childCount() else '')
        hint = group.text(0)
        if can_edit and kind == 'subtitle' and mode == 'audio':
            hint = 'Les SRT sont disponibles avec une vidéo ou seuls.'
        elif can_edit and kind == 'video' and checked and not can_toggle and subtitles:
            hint = 'Décochez Audio ou Sous-titres avant de retirer la vidéo.'
        bind_text(group, 'setToolTip', 0, hint)

    @staticmethod
    def mark_checkbox(item, checked, editable):
        item.setCheckState(0, CHECKED if checked else UNCHECKED)
        flags = item.flags() & ~Qt.ItemFlag.ItemIsUserCheckable
        item.setFlags(flags | Qt.ItemFlag.ItemIsUserCheckable if editable else flags)

    @staticmethod
    def stream_leaves(group):
        for index in range(group.childCount()):
            child = group.child(index)
            if child.data(0, ROLE):
                yield child
            else:
                for leaf in range(child.childCount()):
                    yield child.child(leaf)

    def schedule_stream_refresh(self):
        if not self.stream_refresh_pending:
            self.stream_refresh_pending = True
            self.window.after_idle(self.refresh_stream_selection)

    def refresh_stream_selection(self):
        self.stream_refresh_pending = False
        with QSignalBlocker(self.tree):
            url = self.window.queue_view_url
            if url and (item := self.rows.get(url)) and item.data(3, ROLE) in ('ready', 'waiting'):
                self.refresh_row(item, url)
        if self.stream_popup.isVisible():
            self.refresh_stream_popup()

    def open_defaults(self):
        dialog = FormatDefaultsDialog(self.window, self.window.download_defaults)
        if dialog.exec() == QDialog.DialogCode.Accepted:
            self.window._save_format_defaults(dialog.choices())
        dialog.deleteLater()

    def change_stream(self, item, column):
        if column != 0 or not item.flags() & Qt.ItemFlag.ItemIsUserCheckable:
            return
        key = item.data(0, ROLE)
        if not key:
            return
        url = self.stream_url
        checked = item.checkState(0) == CHECKED
        if key[0] == 'group':
            self.window._list_toggle_group(url, key[1], checked)
        else:
            self.window._list_choose_stream(url, key, checked)
        self.refresh_stream_selection()

    def select(self, item, _previous):
        if not item:
            return
        url = item.data(0, ROLE)
        w = self.window
        if url in w.completed_downloads and self.rows[url].data(3, ROLE) == 'done':
            w.completed_selection = url
            self.refresh_details()
            self.schedule_stream_refresh()
            w._save_download_checkpoint()
        elif url != w.queue_view_url or w.completed_selection:
            w.completed_selection = None
            w._open_download_tab(url)

    def refresh_details(self):
        w = self.window
        completed_url = w.completed_selection
        record = w.completed_downloads.get(completed_url)
        w.list_detail_pane.setVisible(bool(record or w.queue_view_url or (not self.rows and w.analysis_data)))
        if record:
            data = record['data']
            video_id = data.get('id') or youtube_video_id(completed_url)
            if completed_url != self.completed_details_url:
                w._request_tab_preview(video_id, fetch_title=False)
            state = 'Terminé · SRT manquants' if record.get('partial') else 'Terminé'
            state = ' · '.join(filter(None, (state, w._list_reanalysis_status(completed_url))))
            w._set_selected_video_identity(completed_url, data.get('title') or completed_url, state,
                                           data, w.thumbnail_cache.get(video_id))
        self.completed_details_url = completed_url if record else None
        w._update_list_details()

    def set_rate_controls(self, rate_controls, shutdown_check):
        index = self.toolbar.indexOf(self.defaults)
        self.toolbar.insertWidget(index, shutdown_check)
        self.toolbar.insertWidget(index + 1, rate_controls)

    def clear_completed(self, url=None):
        w = self.window
        if isinstance(url, str):
            w._forget_list_reanalysis(url)
            w.completed_downloads.pop(url, None)
        else:
            for completed_url in w.completed_downloads:
                w._forget_list_reanalysis(completed_url)
            w.completed_downloads.clear()
        if w.completed_selection not in w.completed_downloads:
            w.completed_selection = None
        w._render_download_queue()
        w._save_download_checkpoint()

    def open_menu(self, position):
        menu = self.line_menu(self.tree.itemAt(position))
        menu.exec(self.tree.viewport().mapToGlobal(position))
        menu.deleteLater()

    def line_menu(self, item):
        w = self.window
        menu = QMenu(self)
        if item is None:
            menu.addAction(w.paste_video_link_action)
            return menu
        self.tree.setCurrentItem(item)
        url = item.data(0, ROLE)
        state = item.data(3, ROLE)
        if state == 'done':
            record = w.completed_downloads[url]
            add_translated(menu, 'addAction', 'Ouvrir le fichier', lambda: w._open_download_result(record)).setEnabled(bool(record['paths']))
            add_translated(menu, 'addAction', 'Afficher dans le dossier', lambda: w._reveal_download_result(record))
            add_translated(menu, 'addAction', 'Préparer à nouveau', lambda: w._list_prepare_again(url))
            menu.addSeparator()
            self.add_reanalysis_action(menu, url)
            add_translated(menu, 'addAction', 'Retirer de la liste', lambda: self.clear_completed(url))
            return menu

        if state in ('active', 'retry'):
            for button, text in ((w.resume_download_button, 'Reprendre'), (w.cancel_button, 'Arrêter le téléchargement'),
                                 (w.pause_download_button, 'Mettre en pause'), (w.skip_download_button, 'Annuler la reprise')):
                if not button.isHidden():
                    add_translated(menu, 'addAction', text, button.click).setEnabled(button.isEnabled())
            if not w.open_download_button.isHidden():
                w.download_result_menu.setTitle('Fichiers obtenus')
                menu.addMenu(w.download_result_menu)
        else:
            if state == 'ready' or w.editing_queued_url == url:
                menu.addAction(w.download_button.text(), w.download_button.click).setEnabled(w.download_button.isEnabled())
            if state == 'waiting':
                if w._can_prioritize_download():
                    add_translated(menu, 'addAction', 'Télécharger en priorité', lambda: w._prioritize_list_download(url))
                add_translated(menu, 'addAction', 'Déprogrammer le téléchargement', lambda: w._remove_scheduled_download(url))
        if menu.actions():
            menu.addSeparator()
        if w._list_can_configure(url) and w.mode_var.get() != 'subtitles':
            self.add_container_menu(menu, url)
            add_translated(menu, 'addAction', 'Configurer l’extrait…', w._open_list_clip)
        self.add_reanalysis_action(menu, url)
        menu.addAction(w.analysis_diagnostic_action)
        add_translated(menu, 'addAction', 'Retirer de la liste', lambda: w._close_video_tab(url))
        menu.addAction(w.paste_video_link_action)
        return menu

    def add_reanalysis_action(self, menu, url):
        add_translated(menu, 'addAction', 'Relancer l’analyse', lambda: self.window._reanalyze_list_video(url)).setEnabled(
            self.window._can_reanalyze_list_video(url))

    def add_container_menu(self, menu, url):
        w = self.window
        forced = w.mode_var.get() in ('video', 'video_only') and bool(w._selected_subtitle())
        selected = 'MKV' if forced else w.container_var.get()
        choices = ui(QMenu(menu), title=f'Conteneur · {selected}')
        menu.addMenu(choices)
        if forced:
            add_translated(choices, 'addAction', 'MKV · sous-titres intégrés').setEnabled(False)
            return
        for index, option in enumerate(w.container_combo['values']):
            action = add_translated(choices, 'addAction', option, lambda _checked=False, i=index: w._list_set_container(i)
                                       if w.queue_view_url == url else None)
            action.setCheckable(True)
            action.setChecked(option == selected)
