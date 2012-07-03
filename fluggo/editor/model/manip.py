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

'''
For all of the manipulators in this module, X is floating-point and refers to
video frames.

For all of the commands, X is integer and in the item (or sequence's) native rate.
'''

import collections, itertools
from .items import *
from .commands import *
from fluggo import logging
from PyQt4.QtCore import *
from PyQt4.QtGui import *

logger = logging.getLogger(__name__)


class ClipManipulator:
    '''Manipulates a lone clip.'''

    def __init__(self, item, grab_x, grab_y):
        self.item = item

        self.original_x = item.x
        self.original_y = item.y
        self.original_space = item.space
        self.offset_x = float(item.x) - float(grab_x)
        self.offset_y = item.y - grab_y

        self.item.update(in_motion=True)

        self.space_move_op = None
        self.seq_mover = None
        self.seq_item = None
        self.space_remove_op = None
        self.seq_add_op = None
        self.seq_move_op = None

    def type(self):
        return self.item.type()

    def set_space_item(self, space, x, y):
        self._undo_sequence()

        target_x = int(round(float(x) + self.offset_x))

        space_move_op = MoveItemCommand(self.item, x=target_x, y=y + self.offset_y)
        space_move_op.redo()

        if self.space_move_op:
            self.space_move_op.mergeWith(space_move_op)
        else:
            self.space_move_op = space_move_op

        return float(target_x) - self.offset_x

    def set_sequence_item(self, sequence, x, operation):
        # TODO: I've realized there's a difference in model here;
        # the old way expected that if we failed to move the item, it would be
        # where we last successfully put it. Here, we back out of changes we made
        # previously. That's not really a bad thing: if we fail to place it, the
        # user should be told it's not going to work out the way they want.
        if self.seq_mover is None:
            self.seq_mover = SequenceOverlapItemsMover.from_clip(self.item)
            self.seq_item = self.seq_mover.items[0]

        target_x = int(round(float(x) + self.offset_x))

        if operation == 'add':
            if self.seq_item.sequence == sequence:
                # Try moving it in place
                offset = target_x - (sequence.x + self.seq_item.x)
                command = None

                try:
                    command = MoveSequenceOverlapItemsInPlaceCommand(self.seq_mover, offset)
                    command.redo()

                    if self.seq_move_op:
                        self.seq_move_op.mergeWith(command)
                    else:
                        self.seq_move_op = command

                    return float(target_x) - self.offset_x
                except NoRoomError:
                    # No room here; back out and try as a clip
                    pass

            if self.seq_item.sequence:
                # Back out so we can add it again
                self._undo_sequence(undo_remove=False)

            if self.item.space:
                space_remove_op = RemoveItemCommand(self.item.space, self.item)
                space_remove_op.redo()
                self.space_remove_op = space_remove_op

            # TODO: If this next line raises a NoRoomError, meaning we haven't
            # placed the item anywhere, finish() needs to fail loudly, and the
            # caller needs to know it will fail
            self.seq_add_op = AddOverlapItemsToSequenceCommand(sequence, self.seq_mover, target_x)
            self.seq_add_op.redo()
            self.seq_move_op = None
            return float(target_x) - self.offset_x

        raise ValueError('Unsupported operation "{0}"'.format(operation))

    def _undo_sequence(self, undo_remove=True):
        if self.seq_move_op:
            self.seq_move_op.undo()
            self.seq_move_op = None

        if self.seq_add_op:
            self.seq_add_op.undo()
            self.seq_add_op = None

        if undo_remove and self.space_remove_op:
            self.space_remove_op.undo()
            self.space_remove_op = None

    def reset(self):
        self._undo_sequence()

        if self.space_move_op:
            self.space_move_op.undo()
            self.space_move_op = None

        self.item.update(in_motion=False)

    def finish(self):
        if self.space_remove_op and not self.seq_add_op:
            # Oops, this wasn't a complete action
            raise RuntimeError('Not in a valid state to finish operation.')

        self.item.update(in_motion=False)

        if self.seq_item:
            self.seq_item.update(in_motion=False)

        # Now return the command that will undo it
        if self.space_move_op and not self.space_remove_op:
            return CompoundCommand(self.space_move_op.text(), [self.space_move_op], done=True)

        commands = []

        if self.space_move_op:
            commands.append(self.space_move_op)

        commands.append(self.space_remove_op)
        commands.append(self.seq_add_op)

        if self.seq_move_op:
            commands.append(self.seq_move_op)

        return CompoundCommand(self.seq_add_op.text(), commands, done=True)


class SequenceItemGroupManipulator:
    '''Manipulates a set of sequence items.'''
    def __init__(self, items, grab_x, grab_y):
        self.items = items
        self.mover = SequenceItemsMover(items)
        self.original_sequence = items[0].sequence
        self.original_x = items[0].x + self.original_sequence.x
        self.offset_x = float(self.original_x) - float(grab_x)
        self.offset_y = self.original_sequence.y - grab_y
        self.seq_item_op = None
        self.space_item = None

        self.length = items[-1].x + items[-1].length - items[0].x
        self.remove_command = None
        self.space_insert_command = None
        self.seq_move_op = None
        self.seq_manip = None

        for item in self.items:
            item.update(in_motion=True)

    def type(self):
        return self.original_sequence.type()

    def set_space_item(self, space, x, y):
        target_x = int(round(float(x) + self.offset_x))

        if self.seq_move_op:
            self.seq_move_op.undo()
            self.seq_move_op = None

        if not self.seq_manip:
            self.remove_command = RemoveAdjacentItemsFromSequenceCommand(self.items)
            self.remove_command.redo()

            self.space_item = self.mover.to_item(
                x=target_x, y=y + self.offset_y,
                height=self.original_sequence.height)

            self.space_insert_command = InsertItemCommand(space, self.space_item, 0)
            self.space_insert_command.redo()

            if isinstance(self.space_item, Clip):
                self.seq_manip = ClipManipulator(self.space_item,
                    float(target_x) - self.offset_x, y)
            else:
                self.seq_manip = SequenceManipulator(self.space_item,
                    float(target_x) - self.offset_x, y)

        return self.seq_manip.set_space_item(space, x, y)

    def set_sequence_item(self, sequence, x, operation):
        if self.seq_manip:
            return self.seq_manip.set_sequence_item(sequence, x, operation)

        target_x = int(round(float(x) + self.offset_x))

        if operation == 'add':
            if self.items[0].sequence == sequence:
                # Try moving it in place
                offset = target_x - (sequence.x + self.items[0].x)
                command = None

                try:
                    command = MoveSequenceItemsInPlaceCommand(self.mover, offset)
                    command.redo()

                    if self.seq_move_op:
                        self.seq_move_op.mergeWith(command)
                    else:
                        self.seq_move_op = command

                    return float(target_x) - self.offset_x
                except NoRoomError:
                    # No room here; back out and try new insert
                    pass

        self.set_space_item(sequence.space, 0, 0)
        return self.seq_manip.set_sequence_item(sequence, x, operation)

    def reset(self):
        if self.seq_manip:
            self.seq_manip.reset()
            self.seq_manip = None

        if self.space_insert_command:
            self.space_insert_command.undo()
            self.space_insert_command = None

        if self.remove_command:
            self.remove_command.undo()
            self.remove_command = None

        if self.seq_move_op:
            self.seq_move_op.undo()
            self.seq_move_op = None

        for item in self.items:
            item.update(in_motion=False)

    def finish(self):
        for item in self.items:
            item.update(in_motion=False)

        if not self.seq_manip and not self.seq_move_op:
            return None

        if self.seq_move_op and not self.seq_manip:
            return CompoundCommand(self.seq_move_op.text(), [self.seq_move_op], done=True)

        commands = []

        if self.seq_move_op:
            commands.append(self.seq_move_op)

        seq_command = self.seq_manip.finish()
        commands.extend([self.remove_command, self.space_insert_command, seq_command])

        return CompoundCommand(seq_command.text(), commands, done=True)

class SequenceManipulator:
    '''Manipulates an entire existing sequence.'''

    def __init__(self, item, grab_x, grab_y):
        self.item = item

        self.original_x = item.x
        self.original_y = item.y
        self.original_space = item.space
        self.offset_x = item.x - grab_x
        self.offset_y = item.y - grab_y

        self.item.update(in_motion=True)

        self.space_move_op = None
        self.seq_mover = None
        self.seq_item = None
        self.space_remove_op = None
        self.seq_add_op = None
        self.seq_move_op = None

    def type(self):
        return self.item.type()

    def set_space_item(self, space, x, y):
        self._undo_sequence()

        target_x = int(round(float(x) + self.offset_x))

        space_move_op = MoveItemCommand(self.item, x=target_x, y=y + self.offset_y)
        space_move_op.redo()

        if self.space_move_op:
            self.space_move_op.mergeWith(space_move_op)
        else:
            self.space_move_op = space_move_op

        return float(target_x) - self.offset_x

    def set_sequence_item(self, sequence, x, operation):
        if self.seq_mover is None:
            self.seq_mover = SequenceItemsMover(self.item)
            self.seq_item = self.seq_mover.overlap_movers[0].items[0]

        target_x = int(round(float(x) + self.offset_x))

        if operation == 'add':
            if self.seq_item.sequence == sequence:
                # Try moving it in place
                offset = target_x - (sequence.x + self.seq_item.x)
                command = None

                try:
                    command = MoveSequenceItemsInPlaceCommand(self.seq_mover, offset)
                    command.redo()

                    if self.seq_move_op:
                        self.seq_move_op.mergeWith(command)
                    else:
                        self.seq_move_op = command

                    return float(target_x) - self.offset_x
                except NoRoomError:
                    # No room here; back out and try as a clip
                    pass

            if self.seq_item.sequence:
                # Back out so we can add it again
                self._undo_sequence(undo_remove=False)

            space_remove_op = None

            if self.item.space:
                space_remove_op = RemoveItemCommand(self.item.space, self.item)
                space_remove_op.redo()

            # If this next line raises a NoRoomError, meaning we haven't
            # placed the item anywhere, finish() needs to fail loudly, and the
            # caller needs to know it will fail
            self.seq_add_op = AddSequenceToSequenceCommand(sequence, self.seq_mover, target_x)
            self.seq_add_op.redo()
            self.seq_move_op = None
            self.space_remove_op = space_remove_op or self.space_remove_op
            return float(target_x) - self.offset_x

        raise ValueError('Unsupported operation "{0}"'.format(operation))

    def _undo_sequence(self, undo_remove=True):
        if self.seq_move_op:
            self.seq_move_op.undo()
            self.seq_move_op = None

        if self.seq_add_op:
            self.seq_add_op.undo()
            self.seq_add_op = None

        if undo_remove and self.space_remove_op:
            self.space_remove_op.undo()
            self.space_remove_op = None

    def reset(self):
        self._undo_sequence()

        if self.space_move_op:
            self.space_move_op.undo()
            self.space_move_op = None

        self.item.update(in_motion=False)

    def finish(self):
        if self.space_remove_op and not self.seq_add_op:
            # Oops, this wasn't a complete action
            raise RuntimeError('Not in a valid state to finish operation.')

        self.item.update(in_motion=False)

        if self.seq_mover:
            for mover in self.seq_mover.overlap_movers:
                for item in mover.items:
                    item.update(in_motion=False)

        # Now return the command that will undo it
        if self.space_move_op and not self.space_remove_op:
            return CompoundCommand(self.space_move_op.text(), [self.space_move_op], done=True)

        commands = []

        if self.space_move_op:
            commands.append(self.space_move_op)

        commands.append(self.space_remove_op)
        commands.append(self.seq_add_op)

        if self.seq_move_op:
            commands.append(self.seq_move_op)

        return CompoundCommand(self.seq_move_op.text(), commands, done=True)

class ItemManipulator:
    # TODO
    # Identify adjacent items in a sequence and manipulate them as a unit
    # Identify groups and manipulate them as a unit
    # Find good algorithm for turning loose items into sequences
    #  ^    Scratch: Only the item/sequence directly grabbed (listed first in self.items)
    #       is placed in a sequence, and the rest arranged around it accordingly
    '''Moves clips, sequence items, and sequences.'''

    def __init__(self, items, grab_x, grab_y):
        '''
        Create an ItemManipulator for the given *items*.

        The first item in *items* is considered the primary item (the item under
        the mouse cursor); it is placed first, and then all of the other items
        are placed around it.

        *grab_x* and *grab_y* are the position of the mouse cursor at the
        beginning of the operation. *grab_x* is a float, and in units of seconds.
        '''

        # The new ItemManipulator: No longer a one-size-fits-all solution.
        # We take into account what kinds of items are selected, primary and secondary.

        primary = items[0]
        space = primary.sequence.space if isinstance(primary, SequenceItem) else primary.space
        self.space = space

        items = set(items)

        # Get all the anchored items
        anchored_items = set(itertools.chain.from_iterable(space.find_anchored_items(target) for target in items))
        items.update(anchored_items)

        # If a sequence is selected, we don't want to try to move the sequence items too
        items.difference_update(frozenset(
            itertools.chain.from_iterable(seq for seq in items if isinstance(seq, Sequence))))

        if isinstance(primary, SequenceItem) and primary not in items:
            # Sequence item was the primary and the sequence got picked up too
            primary = primary.sequence

        items.remove(primary)

        # Grab up the sequence items
        seq_items = set(item for item in items if isinstance(item, SequenceItem))
        items = set(item for item in items if isinstance(item, Item))

        sequences = []

        # Sort and combine the sequence items
        for seq, itemlist in itertools.groupby(sorted(seq_items, key=lambda a: (a.sequence, a.index)), key=lambda a: a.sequence):
            list_ = list(itemlist)

            if len(seq) == len(list_):
                # We've really got the whole thing, add it to items instead
                if isinstance(primary, SequenceItem) and primary.sequence == seq:
                    primary = SequenceManipulator(seq, grab_x * float(space.rate(seq.type())), grab_y)
                else:
                    items.add(seq)
            else:
                mover = SequenceItemGroupManipulator(list_, grab_x * float(space.rate(seq.type())), grab_y)

                if isinstance(primary, SequenceItem) and primary.sequence == seq:
                    primary = mover
                else:
                    sequences.append(mover)

        if isinstance(primary, Clip):
            primary = ClipManipulator(primary, grab_x * float(space.rate(primary.type())), grab_y)
        elif isinstance(primary, Sequence):
            primary = SequenceManipulator(primary, grab_x * float(space.rate(primary.type())), grab_y)
        elif isinstance(primary, SequenceItem):
            primary = SequenceItemGroupManipulator([primary], grab_x * float(space.rate(primary.type())), grab_y)

        self.primary = primary
        self.sequences = sequences

        self.items = []

        for item in items:
            if isinstance(item, Clip):
                self.items.append(ClipManipulator(item, grab_x * float(space.rate(item.type())), grab_y))
            else:
                self.items.append(SequenceManipulator(item, grab_x * float(space.rate(item.type())), grab_y))

    def set_space_item(self, space, x, y):
        x = float(x)

        # New rules:
        if isinstance(self.primary, ClipManipulator) or isinstance(self.primary, SequenceManipulator):
            # Place the primary item
            target_x = self.primary.set_space_item(space,
                x * float(space.rate(self.primary.type())), y)
            #print('x: {0}, target_x: {1}, rate: {2}'.format(x, target_x, float(space.rate(self.primary.type()))))

            # Translate its new position back so we can place the other objects in relation
            x = float(target_x) / float(space.rate(self.primary.type()))
            #print('new_x: {0}'.format(x))

            for seq in self.sequences:
                try:
                    seq.set_sequence_item(seq.original_sequence, x * float(self.space.rate(seq.type())), 'add')
                except NoRoomError:
                    seq.set_space_item(space, x * float(space.rate(seq.type())), y)

            for item in self.items:
                item.set_space_item(space, x * float(space.rate(item.type())), y)
        elif isinstance(self.primary, SequenceItemGroupManipulator):
            # Place the primary item
            target_x = self.primary.set_space_item(space,
                x * float(space.rate(self.primary.type())), y)

            # Translate its new position back so we can place the other objects in relation
            x = float(target_x) / float(space.rate(self.primary.type()))

            for seq in self.sequences:
                seq.set_space_item(space, x * float(space.rate(seq.type())), y)

            for item in self.items:
                item.set_space_item(space, x * float(space.rate(item.type())), y)

    def set_sequence_item(self, sequence, x, y, operation):
        x = float(x)

        try:
            # Place the primary item
            target_x = self.primary.set_sequence_item(sequence,
                x * float(sequence.space.rate(self.primary.type())), operation)

            # Translate its new position back so we can place the other objects in relation
            new_x = float(target_x) / float(sequence.space.rate(self.primary.type()))

            for seq in self.sequences:
                seq.set_space_item(sequence.space, new_x * float(sequence.space.rate(seq.type())), y)

            for item in self.items:
                item.set_space_item(sequence.space, new_x * float(sequence.space.rate(item.type())), y)
        except NoRoomError:
            self.set_space_item(sequence.space, x, y)

    def reset(self):
        self.primary.reset()

        for seq in self.sequences:
            seq.reset()

        for item in self.items:
            item.reset()

    def finish(self):
        commands = []
        text = 'Move item'

        primary_command = self.primary.finish()

        if primary_command:
            commands.append(self.primary.finish())
            text = commands[0].text()

        commands.extend([cmd for cmd in (seq.finish() for seq in self.sequences) if cmd])
        commands.extend([cmd for cmd in (item.finish() for item in self.items) if cmd])

        if not commands:
            return None

        return CompoundCommand(text, commands, done=True)


