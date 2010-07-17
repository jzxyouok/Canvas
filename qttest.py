
from PyQt4.QtCore import *
from PyQt4.QtGui import *
from PyQt4.QtOpenGL import *
from fluggo import signal, sortlist
from fluggo.media import process, timecode, qt, formats, sources
from fluggo.media.basetypes import *
import sys, fractions, array, collections
from fluggo.editor import ui, canvas

from fluggo.media.muxers.ffmpeg import FFMuxPlugin

class VideoWorkspaceManager(object):
    class ItemWatcher(object):
        def __init__(self, owner, canvas_item, workspace_item):
            self.owner = owner
            self.canvas_item = canvas_item
            self.workspace_item = workspace_item
            self.canvas_item.updated.connect(self.handle_updated)
            self._z_order = 0

        def handle_updated(self, **kw):
            # Raise the frames_updated signal if the content of frames changed
            if 'x' in kw or 'width' in kw or 'offset' in kw:
                old_x, old_width, old_offset = self.workspace_item.x, self.workspace_item.width, self.workspace_item.offset
                new_x, new_width, new_offset = kw.get('x', old_x), kw.get('width', old_width), kw.get('offset', old_offset)
                old_right, new_right = old_x + old_width, new_x + new_width

                self.workspace_item.update(
                    x=kw.get('x', old_x),
                    width=kw.get('width', old_width),
                    offset=kw.get('offset', old_offset)
                )

                # Update the currently displayed frame if it's in a changed region
                if old_x != new_x:
                    self.owner.frames_updated(min(old_x, new_x), max(old_x, new_x) - 1)

                if old_right != new_right:
                    self.owner.frames_updated(min(old_right, new_right), max(old_right, new_right) - 1)

                if old_x - old_offset != new_x - new_offset:
                    self.owner.frames_updated(max(old_x, new_x), min(old_right, new_right) - 1)

            if 'y' in kw or 'z' in kw:
                self.owner.watchers_sorted.move(self.z_order)

        @property
        def z_order(self):
            return self._z_order

        @z_order.setter
        def z_order(self, value):
            self._z_order = value

            if value != self.workspace_item.z:
                self.workspace_item.update(z=value)
                self.owner.frames_updated(self.workspace_item.x, self.workspace_item.x + self.workspace_item.width - 1)

        def unwatch(self):
            self.canvas_item.updated.disconnect(self.handle_updated)

    def __init__(self, canvas_space, source_list):
        self.workspace = process.Workspace()
        self.canvas_space = canvas_space
        self.canvas_space.item_added.connect(self.handle_item_added)
        self.canvas_space.item_removed.connect(self.handle_item_removed)
        self.source_list = source_list
        self.frames_updated = signal.Signal()
        self.watchers = {}
        self.watchers_sorted = sortlist.SortedList(keyfunc=lambda a: a.canvas_item.z_sort_key(), index_attr='z_order')

        for item in canvas_space:
            if item.type() == 'video':
                self.handle_item_added(item)

    def handle_item_added(self, item):
        if item.type() != 'video':
            return

        source = self.source_list.get_stream(item.source_name, item.source_stream_id)

        workspace_item = self.workspace.add(x=item.x, width=item.width, z=item.z, offset=item.offset, source=source)

        watcher = self.ItemWatcher(self, item, workspace_item)
        self.watchers[id(item)] = watcher
        self.watchers_sorted.add(watcher)

    def handle_item_removed(self, item):
        if item.type() != 'video':
            return

        watcher = self.watchers.pop(id(item))
        watcher.unwatch()
        self.watchers_sorted.remove(watcher)
        self.workspace.remove(watcher.workspace_item)

class SourceSearchModel(QAbstractTableModel):
    def __init__(self, source_list):
        QAbstractTableModel.__init__(self)
        self.source_list = source_list
        self.current_list = []
        self.source_list.added.connect(self._item_added)
        self.source_list.removed.connect(self._item_removed)
        self.setSupportedDragActions(Qt.LinkAction)

        self.search_string = None
        self.search('')

    def _item_added(self, name):
        print 'Added ' + name
        if self._match(name):
            length = len(self.current_list)
            self.beginInsertRows(QModelIndex(), length, length)
            self.current_list.append(name)
            self.endInsertRows()

    def _item_removed(self, name):
        print 'Removed ' + name
        if self._match(name):
            index = self.current_list.index(name)
            self.beginRemoveRows(QModelIndex(), index, index)
            del self.current_list[index]
            self.endRemoveRows()

    def _match(self, name):
        return self.search_string in name.lower()

    def search(self, search_string):
        self.search_string = search_string.lower()

        self.beginResetModel()
        self.current_list = [name for name in self.source_list.iterkeys() if self._match(name)]
        self.endResetModel()

    def data(self, index, role=Qt.DisplayRole):
        if role == Qt.DisplayRole:
            if index.column() == 0:
                return self.current_list[index.row()]

    def mimeData(self, indexes):
        index = indexes[0]
        data = QMimeData()
        data.source_name = self.current_list[index.row()]

        return data

    def flags(self, index):
        return Qt.ItemIsSelectable | Qt.ItemIsEnabled | Qt.ItemIsDragEnabled

    def rowCount(self, parent):
        if parent.isValid():
            return 0

        return len(self.current_list)

    def columnCount(self, parent):
        if parent.isValid():
            return 0

        return 1

class SourceSearchWidget(QDockWidget):
    def __init__(self, source_list):
        QDockWidget.__init__(self, 'Sources')
        self.source_list = source_list
        self.model = SourceSearchModel(source_list)

        widget = QWidget()

        self.view = QListView(self)
        self.view.setModel(self.model)
        self.view.setDragEnabled(True)

        layout = QVBoxLayout(widget)
        layout.addWidget(self.view)

        self.setWidget(widget)

muxers = (FFMuxPlugin,)

class MainWindow(QMainWindow):
    def __init__(self):
        QMainWindow.__init__(self)

        self.source_list = sources.SourceList(muxers)

        # Only one space for now, we'll do multiple later
        self.space = canvas.Space()
        #self.space.append(clip)

        self.workspace_manager = VideoWorkspaceManager(self.space, self.source_list)
        self.workspace_manager.frames_updated.connect(self.handle_update_frames)

        # Set up canvas
        self.clock = process.SystemPresentationClock()
        self.frame_rate = fractions.Fraction(24000, 1001)

        self.view = ui.canvas.View(self.clock, self.space, self.source_list)
        #self.view.setViewport(QGLWidget())
        self.view.setBackgroundBrush(QBrush(QColor.fromRgbF(0.5, 0.5, 0.5)))

        format = QGLFormat()
        self.video_dock = QDockWidget('Video Preview', self)
        self.video_widget = qt.VideoWidget(format, self.video_dock)
        self.video_dock.setWidget(self.video_widget)

        self.video_widget.setDisplayWindow(box2i(0, -1, 719, 478))

        self.video_widget.setRenderingIntent(1.5)
        self.video_widget.setPixelAspectRatio(640.0/704.0)
        self.video_widget.setPresentationClock(self.clock)
        self.video_widget.setVideoSource(self.workspace_manager.workspace)

        self.clock.seek(0)

        self.addDockWidget(Qt.BottomDockWidgetArea, self.video_dock)

        self.search_dock = SourceSearchWidget(self.source_list)
        self.addDockWidget(Qt.BottomDockWidgetArea, self.search_dock)

        # Set up UI
        self.create_actions()
        self.create_menus()

        center_widget = QWidget(self)
        layout = QVBoxLayout(center_widget)
        layout.addWidget(self.view)

        transport_toolbar = QToolBar(self)

        for action in self.transport_group.actions():
            transport_toolbar.addAction(action)

        layout.addWidget(transport_toolbar)
        layout.setSpacing(0)
        layout.setContentsMargins(0, 0, 0, 0)
        center_widget.setLayout(layout)

        self.setCentralWidget(center_widget)

        # FOR TESTING
        #self.open_file('test.yaml')

    def create_actions(self):
        self.open_space_action = QAction('&Open...', self,
            statusTip='Open a Canvas file', triggered=self.open_space)
        self.quit_action = QAction('&Quit', self, shortcut=QKeySequence.Quit,
            statusTip='Quit the application', triggered=self.close, menuRole=QAction.QuitRole)

        self.view_video_preview = self.video_dock.toggleViewAction()
        self.view_video_preview.setText('Video &Preview')
        self.view_source_list = self.search_dock.toggleViewAction()
        self.view_source_list.setText('&Sources')

        self.transport_group = QActionGroup(self)
        self.transport_rewind_action = QAction('Rewind', self.transport_group,
            statusTip='Play the current timeline backwards', triggered=self.transport_rewind,
            icon=self.style().standardIcon(QStyle.SP_MediaSeekBackward), checkable=True)
        self.transport_play_action = QAction('Play', self.transport_group,
            statusTip='Play the current timeline', triggered=self.transport_play,
            icon=self.style().standardIcon(QStyle.SP_MediaPlay), checkable=True)
        self.transport_pause_action = QAction('Pause', self.transport_group,
            statusTip='Pause the current timeline', triggered=self.transport_pause,
            icon=self.style().standardIcon(QStyle.SP_MediaPause), checked=True, checkable=True)
        self.transport_fastforward_action = QAction('Rewind', self.transport_group,
            statusTip='Play the current timeline at double speed', triggered=self.transport_fastforward,
            icon=self.style().standardIcon(QStyle.SP_MediaSeekForward), checkable=True)

    def create_menus(self):
        self.file_menu = self.menuBar().addMenu('&File')
        self.file_menu.addAction(self.open_space_action)
        self.file_menu.addSeparator()
        self.file_menu.addAction(self.quit_action)

        self.view_menu = self.menuBar().addMenu('&View')
        self.view_menu.addAction(self.view_source_list)
        self.view_menu.addAction(self.view_video_preview)

    def handle_update_frames(self, min_frame, max_frame):
        # If the current frame was in this set, re-seek to it
        speed = self.clock.get_speed()

        if speed.numerator:
            return

        time = self.clock.get_presentation_time()
        frame = process.get_time_frame(self.frame_rate, time)

        # FIXME: There's a race condition here where we might request
        # a repaint multiple times, but get one of the states in the middle,
        # not the final state
        if frame >= min_frame and frame <= max_frame:
            self.clock.seek(process.get_frame_time(self.frame_rate, int(frame)))

    def open_space(self):
        path = QFileDialog.getOpenFileName(self, "Open File", filter='YAML Files (*.yaml)')

        if path:
            self.open_file(path)

    def open_file(self, path):
        sources, space = None, None

        with file(path) as stream:
            (sources, space) = yaml.load_all(stream)

        self.space[:] = []
        self.source_list.clear()
        self.source_list.update(sources)
        self.space[:] = space[:]

    def transport_play(self):
        self.clock.play(1)
        self.transport_play_action.setChecked(True)

    def transport_pause(self):
        self.clock.stop()
        self.transport_pause_action.setChecked(True)

    def transport_fastforward(self):
        self.clock.play(2)
        self.transport_fastforward_action.setChecked(True)

    def transport_rewind(self):
        self.clock.play(-2)
        self.transport_rewind_action.setChecked(True)

app = QApplication(sys.argv)

window = MainWindow()
window.show()

quit(app.exec_())

