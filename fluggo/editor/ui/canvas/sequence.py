# This file is part of the Fluggo Media Library for high-quality
# video and audio processing.
#
# Copyright 2010 Brian J. Crowell <brian@fluggo.com>
#
# This program is free software: you can redistribute it and/or modify
# it under the terms of the GNU General Public License as published by
# the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.
#
# This program is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
# GNU General Public License for more details.
#
# You should have received a copy of the GNU General Public License
# along with this program.  If not, see <http://www.gnu.org/licenses/>.

from ..canvas import *
from .markers import *
from fluggo.editor import model, graph
from fluggo.media import sources, process
from fluggo.media.basetypes import *
from PyQt4 import QtCore, QtGui
from PyQt4.QtCore import Qt
from .thumbnails import ThumbnailPainter

class _ItemLeftController(Controller1D):
    def __init__(self, item, view):
        self.item = item
        self.prev_item = self.item.sequence[self.item.index - 1] if self.item.index > 0 else None
        self.next_item = self.item.sequence[self.item.index + 1] if self.item.index < len(self.item.sequence) - 1 else None
        self.sequence = self.item.sequence

        self.original_x = self.item.x
        self.original_length = self.item.length
        self.original_offset = self.item.offset
        self.original_trans_length = self.item.transition_length
        self.original_seq_x = self.sequence.x

    def move(self, delta):
        # Don't move past the beginning of the clip
        if self.original_offset + delta < 0:
            delta = -self.original_offset

        # Don't make the clip shorter than one frame
        if delta >= self.original_length:
            delta = self.original_length - 1

        if self.next_item:
            # Don't let it get shorter than next_item.transition_length
            if self.original_length - delta < self.next_item.transition_length:
                delta = self.original_length - self.next_item.transition_length

        if self.prev_item:
            # transition_length < 0: Separate into two sequences
            # Above is TODO, here I just prevent it from dropping below 0
            if self.original_trans_length < delta:
                delta = self.original_trans_length

            # transition_length > prev_item.length - prev_item.transition_length: Don't overrun previous item
            if self.original_trans_length - delta > self.prev_item.length - self.prev_item.transition_length:
                delta = self.original_trans_length - (self.prev_item.length - self.prev_item.transition_length)

        self.item.update(
            length=self.original_length - delta,
            offset=self.original_offset + delta,
            transition_length=self.original_trans_length - delta if self.prev_item else 0)

        if not self.prev_item:
            # Adjust the sequence's beginning
            self.sequence.update(x=self.original_seq_x + delta)

class _ItemRightController(Controller1D):
    def __init__(self, handler, view):
        self.item = handler.item
        self.sequence = self.item.sequence
        self.prev_item = self.sequence[self.item.index - 1] if self.item.index > 0 else None
        self.next_item = self.sequence[self.item.index + 1] if self.item.index < len(self.sequence) - 1 else None

        self.original_length = self.item.length
        self.max_length = handler.format.adjusted_length
        self.original_trans_length = self.next_item.transition_length if self.next_item else 0

    def move(self, delta):
        # Don't move past the end of the clip
        if self.original_length + delta > self.max_length:
            delta = self.max_length - self.original_length

        # Don't make the clip shorter than one frame
        if self.original_length + delta < 1:
            delta = 1 - self.original_length

        # Also don't make it shorter than the transition_length
        if self.original_length + delta < self.item.transition_length:
            delta = self.item.transition_length - self.original_length

        if self.next_item:
            # Don't let the next transition_length fall below zero
            # TODO: Let the sequence split if this happens
            if self.original_trans_length + delta < 0:
                delta = -self.original_trans_length

            if self.original_trans_length + delta > self.next_item.length:
                delta = self.next_item.length - self.original_trans_length

        self.item.update(length=self.original_length + delta)

        if self.next_item:
            self.next_item.update(transition_length=self.original_trans_length + delta)

class _SequenceItemHandler(object):
    def __init__(self, item, owner):
        self.owner = owner
        self.item = item
        self._format = None

        self.left_handle = HorizontalHandle(owner, _ItemLeftController, item)
        self.left_handle.setZValue(1)
        self.right_handle = HorizontalHandle(owner, _ItemRightController, self)
        self.right_handle.setZValue(1)

    def added_to_scene(self):
        self.left_handle.setPos(float(self.item.x / self.owner.units_per_second), 0.0)
        self.right_handle.setPos(float((self.item.x + self.item.length) / self.owner.units_per_second), 0.0)

    def view_scale_changed(self, view):
        hx = view.handle_width / float(view.scale_x)

        self.left_handle.setRect(QtCore.QRectF(0.0, 0.0, hx, self.owner.item_display_height))
        self.right_handle.setRect(QtCore.QRectF(-hx, 0.0, hx, self.owner.item_display_height))

    def item_updated(self, **kw):
        if 'x' in kw:
            self.left_handle.setPos(float(self.item.x / self.owner.units_per_second), 0.0)
            self.right_handle.setPos(float((self.item.x + self.item.length) / self.owner.units_per_second), 0.0)
        elif 'length' in kw:
            self.right_handle.setPos(float((self.item.x + self.item.length) / self.owner.units_per_second), 0.0)

    @property
    def format(self):
        if not self._format:
            self._format = self.owner.scene().source_list[self.item.source.source_name].streams[self.item.source.stream_index]

        return self._format

    def kill(self):
        self.owner.scene().removeItem(self.left_handle)

class VideoSequence(VideoItem):
    def __init__(self, sequence):
        VideoItem.__init__(self, sequence, None)
        self.manager = None

        self.item.item_added.connect(self._handle_item_added)
        self.item.items_removed.connect(self._handle_items_removed)
        self.item.item_updated.connect(self._handle_item_updated)

        self.left_handle.hide()
        self.right_handle.hide()
        self.top_handle.setZValue(2)
        self.bottom_handle.setZValue(2)

        self.seq_items = [_SequenceItemHandler(item, self) for item in sequence]
        x = 0

        for seq_item in self.seq_items:
            seq_item.x = x
            x += seq_item.item.length - seq_item.item.transition_length

    @property
    def item_display_height(self):
        return self.item.height

    @property
    def stream(self):
        if not self.manager:
            self.manager = graph.SequenceVideoManager(self.item, self.scene().source_list, self.scene().space.video_format)

        return self.manager

    def _added_to_scene(self):
        VideoItem._added_to_scene(self)

        for item in self.seq_items:
            item.added_to_scene()

    def view_scale_changed(self, view):
        VideoItem.view_scale_changed(self, view)

        for item in self.seq_items:
            item.view_scale_changed(view)

    def _handle_item_added(self, item):
        pass

    def _handle_items_removed(self, start, stop):
        pass

    def _handle_item_updated(self, item, **kw):
        self.seq_items[item.index].item_updated(**kw)


