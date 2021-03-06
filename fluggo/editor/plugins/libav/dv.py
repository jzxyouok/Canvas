# This file is part of the Fluggo Media Library for high-quality
# video and audio processing.
#
# Copyright 2010-2 Brian J. Crowell <brian@fluggo.com>
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

from fluggo import logging
from fluggo.media import libav, process
from fluggo.media.basetypes import *
from fluggo.editor import plugins
from PyQt4.QtCore import *
from PyQt4.QtGui import *
import fractions

_log = logging.getLogger(__name__)

# TODO: When you think about it, the only real specialization here is the handling
# of DV video... the audio should probably be generalized

class _DVError(Exception):
    pass

class LibavDvSourcePlugin(plugins.SourcePlugin):
    plugin_urn = 'urn:fluggo.com/canvas/plugins:libav-dv'
    name = 'Libav DV Source'
    description = 'Provides special DV support from Libav'

    @classmethod
    def create_source(cls, name, definition):
        '''Return a source from the given definition.

        The *definition* will be a definition returned by Source.definition().'''
        return _LibavSource.from_definition(name, definition)

    @classmethod
    def create_source_from_file(cls, name, path):
        '''Return a new source if one can be created from the given file. If the plugin
        can't read the file (or doesn't support files), it can raise an error or return None.

        This method is only called to attempt to read new files. Existing
        sources are re-created using create_source().'''
        source = _LibavSource(name, path)
        source.bring_online()

        if not source.offline:
            return source

class LibavDVCodecPlugin(plugins.CodecPlugin):
    plugin_urn = 'urn:fluggo.com/canvas/plugins:libav-dv-codec'
    name = 'Libav DV Codec'
    description = 'Provides codec support for DV using Libav'

    @classmethod
    def get_all_codecs(self):
        '''Return a list of all codecs supported by this plugin.'''
        return [_DVCodec, _PCMCodec]

class _DVCodec(plugins.Codec):
    urn = 'urn:fluggo.com/canvas/codecs:libav-dv-codec'
    format_urns = frozenset(['urn:libav:codec-format:dvvideo'])
    stream_type = 'video'
    can_decode = True
    name = 'Libav DV Video'
    #can_encode = True
    default_priority = 1
    plugin = LibavDVCodecPlugin

    def create_decoder(self, packet_stream, offset, length):
        '''Return a stream object (VideoStream, AudioStream, etc.) to decode the given packet stream and definition.'''
        return _DVVideoDecoder(packet_stream, offset, length)

class _DVVideoDecoder(plugins.VideoStream):
    codec = _DVCodec

    def __init__(self, packet_stream, offset, length):
        # TODO: pixel_aspect_ratio, distinguish between 4:3 and 16:9 video
        # TODO: specify pulldown here, and if so, do we decode before output?
        #   I expect not; we give interlaced and let transform/source filters take care of it
        if offset != 0:
            raise NotImplementedError

        self._pktstream = packet_stream
        base_filter = self.get_static_stream()

        video_format = plugins.VideoFormat(interlaced=True,
            full_frame=box2i(-8, -1, -8 + 720 - 1, -1 + 480 - 1),
            active_area=box2i(0, -1, 704 - 1, -1 + 480 - 1),
            pixel_aspect_ratio=fractions.Fraction(10, 11),
            white_point='D65',
            frame_rate=fractions.Fraction(30000, 1001))

        plugins.VideoStream.__init__(self, base_filter, video_format, (0, length - 1))

    def get_definition(self):
        return {}

    def get_static_stream(self):
        # TODO: Do offset to match defined_range
        decoder = libav.AVVideoDecoder(self._pktstream, 'dvvideo')
        return process.DVReconstructionFilter(decoder)

class _PCMCodec(plugins.Codec):
    urn = 'urn:fluggo.com/canvas/codecs:libav-pcm-codec'
    format_urns = frozenset(['urn:libav:codec-format:pcm_s16le'])
    stream_type = 'audio'
    can_decode = True
    name = 'Libav PCM'
    #can_encode = True
    plugin = LibavDVCodecPlugin

    def create_decoder(self, packet_stream, offset, length):
        '''Return a stream object (VideoStream, AudioStream, etc.) to decode the
        given packet stream.'''
        return _PCMs16leAudioDecoder(packet_stream, offset, length)

class _PCMs16leAudioDecoder(plugins.AudioStream):
    codec = _PCMCodec

    def __init__(self, packet_stream, offset, length):
        # TODO: audio, allow for quad-channel on output
        # TODO: Take sample rate from container; split-stream files may trip
        #   us up with 44.1kHz sample rates
        if offset != 0:
            raise NotImplementedError

        self._pktstream = packet_stream
        base_filter = self.get_static_stream()

        audio_format = plugins.AudioFormat(sample_rate=48000,
            channel_assignment=('FrontLeft', 'FrontRight'))

        plugins.AudioStream.__init__(self, base_filter, audio_format, (0, length - 1))

    def get_definition(self):
        return {}

    def get_static_stream(self):
        # TODO: Do offset to match defined_range
        return libav.AVAudioDecoder(self._pktstream, 'pcm_s16le', 2)


_codec_format_names = {codec_id: name for name, codec_id in libav.__dict__.items() if name.startswith('CODEC_ID_')}

class _LibavSource(plugins.Source):
    translation_context = 'fluggo.editor.plugins.libav._LibavSource'
    plugin = LibavDvSourcePlugin

    def __init__(self, name, path):
        self.path = path
        self._load_alert = None

        # Stream ID -> {urn: Codec URN, definition: definition}
        self._loaded_definitions = {}
        self._streams = []

        plugins.Source.__init__(self, name)

    def bring_online(self):
        if not self.offline:
            return

        if self._load_alert:
            self.hide_alert(self._load_alert)
            self._load_alert = None

        try:
            container = libav.AVContainer(self.path)
            streams = []

            for stream_desc in container.streams:
                # TODO: Take into account relative stream start times

                if stream_desc.type == 'video':
                    video_length = stream_desc.frame_count

                    if not video_length:
                        # We need to give our best guess
                        if stream_desc.duration:
                            video_length = int(round(fractions.Fraction(stream_desc.duration) * stream_desc.time_base * stream_desc.real_frame_rate))
                        elif container.duration:
                            video_length = int(round(fractions.Fraction(container.duration, 1000000) * stream_desc.real_frame_rate))
                    else:
                        video_length = int(video_length)

                    # Find codec
                    # We use the stream index because some formats don't have a
                    # proper stream ID (such as the dv format)
                    stream = self._find_codec(plugins.VideoDecoderConnector, stream_desc, 0, video_length)
                    stream.name = str(stream_desc.index)
                    stream.id = stream_desc.id
                    self.follow_alerts(stream)
                    self._streams.append(stream)
                elif stream_desc.type == 'audio':
                    audio_length = stream_desc.frame_count

                    if not audio_length:
                        # We need to give our best guess
                        if stream_desc.duration and stream_desc.sample_rate:
                            audio_length = int(round(fractions.Fraction(stream_desc.duration) * stream_desc.time_base * stream_desc.sample_rate))
                        elif container.duration:
                            audio_length = int(round(fractions.Fraction(container.duration, 1000000) * stream_desc.sample_rate))
                    else:
                        audio_length = int(audio_length)

                    # Find codec
                    stream = self._find_codec(plugins.AudioDecoderConnector, stream_desc, 0, audio_length)
                    stream.name = str(stream_desc.index)
                    stream.id = stream_desc.id
                    self.follow_alerts(stream)
                    self._streams.append(stream)

            self.offline = False
        except _DVError as ex:
            self._load_alert = plugins.Alert(str(ex), icon=plugins.AlertIcon.Error, source=self.name, actions=[
                QAction('Retry', None, statusTip='Try bringing the source online again', triggered=self._retry_load)])
            self.show_alert(self._load_alert)
        except Exception as ex:
            # TODO: This would probably be easier if we set up specific exceptions that
            # bring_online could throw, and had some other handler call it. That way,
            # we could make specific handlers for specific situations (example: file is
            # missing, so remap to another file) without requiring every source to code it
            # separately.
            self._load_alert = plugins.Alert('Unexpected ' + ex.__class__.__name__ + ': ' + str(ex), icon=plugins.AlertIcon.Error, source=self.name, actions=[
                QAction('Retry', None, statusTip='Try bringing the source online again', triggered=self._retry_load)], exc_info=True)
            self.show_alert(self._load_alert)

    def _find_codec(self, cls, stream_desc, offset, length):
        codec_id = _codec_format_names.get(stream_desc.codec_id)

        if codec_id:
            codec_id = str(codec_id).lower()[9:]
        else:
            codec_id = 'unknown-' + str(stream_desc.codec_id)

        format_urn = 'urn:libav:codec-format:' + codec_id
        demuxer = libav.AVDemuxer(self.path, stream_desc.index)
        loaded_desc = self._loaded_definitions.get(stream_desc.id)
        urn, definition = None, None

        if loaded_desc:
            urn, definition = loaded_desc['urn'], loaded_desc['definition']

        return cls(demuxer, format_urn, offset, length,
            model_obj=self, codec_urn=urn, definition=definition)

    def _retry_load(self, checked):
        self.bring_online()

    @classmethod
    def from_definition(cls, name, definition):
        _log.debug('Producing DV source from definition {0!r}', definition)
        source = cls(name, definition['path'])
        source._loaded_definitions = definition.get('streams') or {}

        return source

    def get_definition(self):
        definition = {'path': self.path}
        streams = {}

        for stream in self._streams:
            if stream.codec:
                # Save the current definition from the codec
                streams[stream.id] = {'urn': stream.codec.urn, 'definition': stream.get_definition()}
            else:
                # Try to preserve the definition we loaded
                loaded_desc = self._loaded_definitions.get(stream.id)

                if loaded_desc:
                    urn, definition = loaded_desc['urn'], loaded_desc['definition']
                    streams[stream.od] = {'urn': urn, 'definition': definition}

        definition['streams'] = streams
        return definition

    @property
    def file_path(self):
        return self.path

    def get_streams(self):
        # TODO: Should this method return length/valid frames as well?
        if self.offline:
            raise plugins.SourceOfflineError

        return self._streams

