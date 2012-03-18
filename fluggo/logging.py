# This file is part of the Fluggo Media Library for high-quality
# video and audio processing.
#
# Copyright 2010-1 Brian J. Crowell <brian@fluggo.com>
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

# This extends ordinary Python logging to support new-style format strings

from __future__ import absolute_import
from logging import *

class _DeferredFormat(object):
    __slots__ = ('format', 'args', 'kw')

    def __init__(self, format, *args, **kw):
        self.format = format
        self.args = args
        self.kw = kw

    def __str__(self):
        return self.format.format(*self.args, **self.kw)

_baseLog = getLoggerClass()

class _logger(_baseLog):
    def warning(self, *args, **kw):
        _baseLog.warning(self, *args, **kw)

    def makeRecord(self, name, lvl, fn, lno, msg, args, exc_info, func=None, extra=None):
        return _baseLog.makeRecord(self, name, lvl, fn, lno, _DeferredFormat(msg, *args, **(extra or {})), [], exc_info, func=func, extra=extra)

setLoggerClass(_logger)
