# -*- coding: utf-8 -*-
# This file is part of the Fluggo Media Library for high-quality
# video and audio processing.
#
# Copyright 2009 Brian J. Crowell <brian@fluggo.com>
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

import collections
import fractions
import process
import yaml
from fluggo import signal
from formats import *

class SourceList(collections.MutableMapping):
    '''
    Maps source names to formats.MediaContainers or container-like objects.
    '''

    def __init__(self, muxers, sources=None):
        self.muxers = muxers
        self.sources = sources or {}
        self.added = signal.Signal()
        self.renamed = signal.Signal()
        self.removed = signal.Signal()

    def __getitem__(self, name):
        return self.sources[name]

    def __setitem__(self, name, value):
        had_it = name in self.sources

        self.sources[name] = value

        if had_it:
            self.removed(name)

        self.added(name)

    def __delitem__(self, name):
        del self.sources[name]
        self.removed(name)

    def __len__(self):
        return len(self.sources)

    def __iter__(self):
        return self.sources.__iter__()

    def get_default_streams(self, name):
        '''
        Get a list of streams considered "default" for the given source.
        '''
        # For now, first video stream and first audio stream
        source = self[name]
        video_streams = [stream for stream in source.streams if stream.type == 'video']
        audio_streams = [stream for stream in source.streams if stream.type == 'audio']

        return video_streams[0:1] + audio_streams[0:1]

    def get_stream(self, name, stream_index):
        container = self.sources.get(name)

        if not container:
            raise KeyError('Could not find source "' + str(name) + '".')

        for muxer in self.muxers:
            if container.muxer in muxer.supported_muxers:
                return muxer.get_stream(container, stream_index)

        return None

class VideoSource(process.VideoPassThroughFilter):
    def __init__(self, source, format):
        self.format = format
        self.length = self.format.adjusted_length

        process.VideoPassThroughFilter.__init__(self, source)

class AudioSource(process.AudioPassThroughFilter):
    def __init__(self, source, format):
        self.format = format
        self.length = self.format.adjusted_length

        process.AudioPassThroughFilter.__init__(self, source)

