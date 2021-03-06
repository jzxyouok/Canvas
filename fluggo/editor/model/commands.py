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

import collections, itertools
from .items import *
from fluggo import logging
from PyQt4.QtCore import *
from PyQt4.QtGui import *

logger = logging.getLogger(__name__)

_Placement = collections.namedtuple('_Placement', 'min max index')

def _split_sequence_items_by_overlap(items):
    '''Splits the *items* (which should all belong to the same sequence and be
    sorted by index) into a list of lists of items, where items in each list overlap,
    but do not overlap with items in the other lists (and thus each list can be
    moved independently).'''
    if not items:
        return []

    next_list = [items[0]]
    result = [next_list]

    for item in items[1:]:
        if item.index != next_list[-1].index + 1 or next_list[-1].transition_length >= 0:
            next_list = [item]
            result.append(next_list)
        else:
            next_list.append(item)

    return result

def _split_sequence_items_by_adjacency(items):
    '''Splits the *items* (which should all belong to the same sequence and be
    sorted by index) into a list of lists of items, where items in each list are
    adjacent (have indexes that differ by one)'''
    if not items:
        return []

    next_list = [items[0]]
    result = [next_list]
    start_offset = items[0].x

    for item in items[1:]:
        if item.index != next_list[-1].index + 1:
            next_list = [item]
            result.append(next_list)
        else:
            next_list.append(item)

    return result

class SequenceItemsMover:
    '''Class for moving any group of sequence items.

    The items should either all belong to the same sequence or not belong to a
    sequence at all. If they don't belong to a sequence, they need to already be
    in the right order.'''
    def __init__(self, items):
        if items[0].sequence:
            # Sort and set positions by index and x
            items = sorted(items, key=lambda a: a.index)
            base_x = items[0].x

            self.overlap_movers = list(
                SequenceOverlapItemsMover(group, group[0].x - base_x) for group in
                _split_sequence_items_by_overlap(items))
        else:
            # Update _x attributes before we go into this
            x = 0
            index = 0

            for item in items:
                if index != 0:
                    x -= item.transition_length

                item._x = x
                item._index = index

                x += item.length
                index += 1

            self.overlap_movers = list(
                SequenceOverlapItemsMover(group, group[0].x) for group in
                _split_sequence_items_by_overlap(items))

    def to_item(self, height=10.0, x=0, y=0):
        '''Return a space Item for containing the items from this SequenceItemsMover.
        If there is one item, this will be a Clip. Otherwise, it will be a Sequence.

        The items will be cloned for moving to the new sequence.'''
        if len(self.overlap_movers) == 1 and len(self.overlap_movers[0].items) == 1:
            # Make a clip
            item = self.overlap_movers[0].items[0]

            return Clip(
                x=x, y=y,
                length=item.length,
                height=height,
                type=item.type(),
                source=item.source,
                offset=item.offset,
                in_motion=item.in_motion,
                anchor=item.anchor)

        seq_items = []
        last_x = 0

        for group in self.overlap_movers:
            items = group.clone_items()
            items[0].update(transition_length=-(group.offset - last_x))
            seq_items.extend(items)
            last_x = group.offset + group.length

        return Sequence(x=x, y=y, type=seq_items[0].type(), items=seq_items,
            height=height, in_motion=self.overlap_movers[0].items[0].in_motion)

class SequenceOverlapItemsMover:
    '''Mover for overlapping items belonging to the same sequence.'''
    def __init__(self, items, offset=None):
        '''Creates an overlapping items mover with the given *items*.
        *offset* can be used to store the group's offset from other groups.'''
        self.items = items
        self.offset = offset

        # The items may not have x values, or they may not be accurate
        # Calculate the long way
        self.length = sum(items[i].length - (items[i].transition_length if i > 0 else 0)
            for i in range(len(items)))

        # max_fadeout_length: The maximum value for the next item's
        # transition_length, which is the distance from the end of our last item
        # to max(start of item, end of item's transition); since these items are
        # overlapping, the last item must have a positive transition_length
        self.max_fadeout_length = items[-1].length

        # Ditto, but at the beginning of the items
        self.max_fadein_length = items[0].length

        if len(items) > 1:
            self.max_fadeout_length -= items[-1].transition_length
            self.max_fadein_length -= items[1].transition_length

    def clone_items(self):
        '''Clones all of this mover's items. The cloned items will be homeless.'''
        return [item.clone() for item in self.items]

    def clone(self):
        '''Clones this mover and all of its items. The cloned items will be homeless.'''
        return SequenceOverlapItemsMover(self.clone_items(), offset=self.offset)

    @classmethod
    def from_clip(cls, clip):
        seq_item = SequenceItem(source=clip.source,
            length=clip.length,
            offset=clip.offset,
            transition_length=0,
            type=clip.type(),
            in_motion=clip.in_motion)

        return cls([seq_item])

class NoRoomError(Exception):
    def __init__(self, message='There is no room for the item.', *args, **kw):
        Exception.__init__(self, message, *args, **kw)

class AddOverlapItemsToSequenceCommand(QUndoCommand):
    def __init__(self, sequence, mover, x, parent=None):
        '''This is where the real add work is done. To add a clip to a sequence,
        convert it to a sequence item and add it to a SequenceOverlapItemsMover.
        *x* is space-relative. The clips should not belong to a sequence already.

        If the given mover can be placed at two indexes, it it is put at the
        lower index.'''
        QUndoCommand.__init__(self, 'Add overlapping items to sequence', parent)
        self.sequence = sequence
        self.mover = mover
        self.x = x

        if self.sequence.type() != self.mover.items[0].type():
            raise NoRoomError('The item type is incompatible with the sequence type.')

        # We do a check here, but we'll be doing it again at the actual insert
        if self.where_can_fit(x) is None:
            raise NoRoomError

        # BJC: Note that we don't calculate the new transition_lengths or keep
        # the next item here; this is because the nearby items are allowed to
        # change (in certain ways) between the creation of this command and its
        # execution
        self.orig_transition_length = self.mover.items[0].transition_length

    def redo(self):
        index = self.where_can_fit(self.x)

        if index is None:
            raise NoRoomError

        self.index = index

        x = self.x - self.sequence.x
        self.orig_sequence_x = self.sequence.x

        # at_index - Item at the insertion index
        at_index = self.sequence[index] if index < len(self.sequence) else None
        at_start = at_index and not at_index.previous_item()
        prev_item = at_index.previous_item() if at_index else self.sequence[-1]
        removed_x = 0

        old_x = at_index.x if at_index else self.sequence.length
        self.orig_next_item = index < len(self.sequence) and self.sequence[index] or None
        self.orig_next_item_trans_length = self.orig_next_item and self.orig_next_item.transition_length

        # Hit the mark "x"
        self.mover.items[0].update(transition_length=
            0 if at_start else old_x - x + (at_index.transition_length if at_index else 0))
        self.sequence[index:index] = self.mover.items

        if self.orig_next_item:
            # Retain this item's position in spite of any removals/insertions in self.item.insert
            self.orig_next_item.update(transition_length=self.mover.length - (old_x - x) - removed_x)

        if at_start:
            # Move the sequence to compensate for insertions at the beginning
            self.sequence.update(x=self.sequence.x - (old_x - x) - removed_x)

    def undo(self):
        # Pop the items out of the sequence (they will be homeless)
        del self.sequence[self.index:self.index + len(self.mover.items)]

        if self.sequence.x != self.orig_sequence_x:
            self.sequence.update(x=self.orig_sequence_x)

        # Reset the before-and-after
        self.mover.items[0].update(transition_length=self.orig_transition_length)

        if self.orig_next_item:
            self.orig_next_item.update(transition_length=self.orig_next_item_trans_length)

        del self.index
        del self.orig_next_item
        del self.orig_next_item_trans_length

    def determine_range(self, index):
        '''Determine the range where a clip will fit. These are tuples of (min, max, mark).
        *min* and *max* are offsets from the beginning of the scene. *mark* is a
        left-gravity mark in the sequence at the index. If the
        item can't fit at all at an index, None might be returned.'''
        if index < 0 or index > len(self.sequence):
            raise IndexError('index out of range')

        if index < len(self.sequence):
            seq_item = self.sequence[index]

            if seq_item.transition_length > 0 and seq_item.index > 0:
                # Previous item is locked in a transition with us and is here to stay
                return None

            # If the item before that is in motion, we have to ignore prev_item's
            # transition_length (which would otherwise be zero or less)
            prev_item = seq_item.previous_item()
            prev_prev_item = prev_item and prev_item.previous_item()

            # Find next_item so we can get its transition_length
            next_item = seq_item.next_item()

            _min = max(
                # Previous item's position plus any transition_length it has
                (prev_item.x +
                    (max(0, prev_item.transition_length) if prev_prev_item else 0))
                    # Or the space before the sequence if this item is first
                    # (but really, it could go as far back as it wants)
                    if prev_item else -self.mover.length,
                # The beginning of this clip (or the gap before it)
                seq_item.x + min(0, seq_item.transition_length)
                    - (self.mover.max_fadein_length if prev_item else self.mover.length))

            _max = (
                # At the item's start
                seq_item.x

                # But back to the beginning of the mover, so they don't overlap
                - self.mover.length

                # How much they can overlap
                + min(self.mover.max_fadeout_length,
                    seq_item.length - (next_item.transition_length if next_item else 0)))

            _min += self.sequence.x
            _max += self.sequence.x

            if not prev_item:
                _min = None
            elif _max < _min:
                return None

            return _Placement(_min, _max, index)
        else:
            # Final index
            prev_item = self.sequence[-1]

            # If the item before that is in motion, we have to ignore prev_item's
            # transition_length (which would otherwise be zero or less)
            prev_prev_item = prev_item and prev_item.previous_item()

            _min = max(
                # Previous item's position plus any transition_length it has
                prev_item.x +
                    (max(0, prev_item.transition_length) if prev_prev_item else 0),
                # End of the sequence minus how much fadein we can give it
                prev_item.x + prev_item.length - self.mover.max_fadein_length)

            _min += self.sequence.x

            return _Placement(_min, None, index)

    def where_can_fit(self, x):
        '''Returns index where the item would be inserted if it can fit, None if it won't.
        "x" is space-relative.'''
        # TODO: This would be faster as a binary search
        # Or a simple optimization would be to skip indexes where X is too low
        for _range in (self.determine_range(i) for i in range(len(self.sequence) + 1)):
            if not _range:
                continue

            if (_range.min is None or x >= _range.min) and (_range.max is None or x <= _range.max):
                return _range.index

        return None

class CompoundCommand(QUndoCommand):
    '''A command consisting of other commands. This lets us create a compound
    command (which QUndoCommand already supports) after its constituent commands
    have already been created and done.'''
    def __init__(self, text, commands, done=False, parent=None):
        QUndoCommand.__init__(self, text, parent)
        self._commands = commands
        self._done = done

    def redo(self):
        if not self._done:
            for command in self._commands:
                command.redo()

            self._done = True

    def undo(self):
        if self._done:
            for command in reversed(self._commands):
                command.undo()

            self._done = False

class UpdateItemPropertiesCommand(QUndoCommand):
    '''Updates the given properties of an item. This can be used to move the item
    around.'''

    def __init__(self, item, parent=None, **properties):
        QUndoCommand.__init__(self, 'Update item properties', parent)

        self.item = item
        self.orig_values = {name: getattr(item, name) for name in properties}
        self.new_values = properties
        self.done = False

    def mergeWith(self, next):
        '''This command *can* be merged, but only manually.'''
        if not isinstance(next, UpdateItemPropertiesCommand):
            return False

        self.new_values.update(next.new_values)
        return True

    def redo(self):
        if not self.done:
            self.item.update(**self.new_values)
            self.done = True

    def undo(self):
        if self.done:
            self.item.update(**self.orig_values)
            self.done = False

class MoveItemCommand(QUndoCommand):
    # In recognition that moving an item is likely to get more complicated.

    def __init__(self, item, x, y, parent=None):
        QUndoCommand.__init__(self, 'Move item', parent)
        self.item = item
        self.command = UpdateItemPropertiesCommand(item, x=x, y=y, parent=self)

    def mergeWith(self, next):
        '''This command *can* be merged, but only manually.'''
        if not isinstance(next, MoveItemCommand):
            return False

        self.command.mergeWith(next.command)
        return True

    def redo(self):
        if self.item.space is None:
            raise RuntimeError('Item must belong to a space to use MoveItemCommand.')

        self.command.redo()

    def undo(self):
        self.command.undo()

class AddSequenceToSequenceCommand(QUndoCommand):
    def __init__(self, sequence, mover, x, parent=None):
        QUndoCommand.__init__(self, 'Add sequence to sequence', parent)
        '''Adds a given SequenceItemsMover to a *sequence* at the given scene-relative *x*.
        The mover's items are added directly, and therefore should not belong to
        a sequence; if you don't want this, you should produce a copy first.

        If the constructor raises a NoRoomError, the addition isn't possible.'''
        for group in mover.overlap_groups:
            AddOverlapItemsToSequenceCommand(sequence, group, x + group.offset)

class MoveSequenceOverlapItemsInPlaceCommand(QUndoCommand):
    def __init__(self, mover, offset, parent=None):
        '''Moves the given SequenceOverlapItemsMover back and forth in a sequence.
        This command does not change the index of the items, just their distance
        to the previous and next items. As such, you'll get a NoRoomError if you
        try to move them too far. The NoRoomError does not occur until redo(),
        but you can call check_room() early if you want.

        This command can be merged with another MoveSequenceOverlapItemsInPlaceCommand,
        provided they refer to the same *mover*.
        '''
        QUndoCommand.__init__(self, 'Move overlapping sequence items in place', parent)
        self.mover = mover
        self.offset = offset
        self.sequence = self.mover.items[0].sequence

        if not self.sequence:
            raise ValueError('The given items are not in a sequence.')

    def id(self):
        return id(MoveSequenceOverlapItemsInPlaceCommand)

    def mergeWith(self, command):
        if not isinstance(command, MoveSequenceOverlapItemsInPlaceCommand):
            return False

        if self.mover is not command.mover:
            return False

        # For future reference-- not that it matters here-- the order of events
        # is *this* command followed by the command given as a parameter.
        self.offset += command.offset

    def check_room(self):
        # TODO: We do not consider whether the items around us are in motion,
        # leading to an inefficiency that we don't know if all the items *can*
        # be moved until all the items are moved; this can be improved
        next_item = self.mover.items[-1].next_item()
        previous_item = self.mover.items[0].previous_item()

        if self.offset > 0 and next_item:
            next_next_item = next_item.next_item()

            max_offset = min(
                # How much room is left in the next item
                next_item.length
                    - max(next_next_item.transition_length if next_next_item else 0, 0)
                    - next_item.transition_length,
                # How much room is left in the max_fadeout_length
                self.mover.max_fadeout_length - next_item.transition_length)

            if self.offset > max_offset:
                raise NoRoomError

        if self.offset < 0 and previous_item:
            min_offset = -min(
                # How much room is left in the previous item
                previous_item.length
                    - self.mover.items[0].transition_length
                    - max(previous_item.transition_length, 0),
                # How much room is left in the max_fadein_length
                self.mover.max_fadein_length - self.mover.items[0].transition_length)

            if self.offset < min_offset:
                raise NoRoomError

    def redo(self):
        self.check_room()

        next_item = self.mover.items[-1].next_item()

        if next_item:
            next_item.update(transition_length=next_item.transition_length + self.offset)

        if self.mover.items[0].index == 0:
            # First index-- move the sequence
            self.sequence.update(x=self.sequence.x + self.offset)
        else:
            # Update our own transition_length
            self.mover.items[0].update(
                transition_length=self.mover.items[0].transition_length - self.offset)

    def undo(self):
        next_item = self.mover.items[-1].next_item()

        if next_item:
            next_item.update(transition_length=next_item.transition_length - self.offset)

        if self.mover.items[0].index == 0:
            # First index-- move the sequence
            self.sequence.update(x=self.sequence.x - self.offset)
        else:
            # Update our own transition_length
            self.mover.items[0].update(
                transition_length=self.mover.items[0].transition_length + self.offset)

class MoveSequenceItemsInPlaceCommand(QUndoCommand):
    def __init__(self, mover, offset, parent=None):
        '''Moves the given SequenceItemsMover back and forth in a sequence.
        This command does not change the index of the items, just their distance
        to the previous and next items. As such, you'll get a NoRoomError if you
        try to move them too far. The NoRoomError does not occur until redo(),
        but you can call check_room() early if you want.

        This command can be merged with another MoveSequenceItemsInPlaceCommand, provided
        they refer to the same *mover*.
        '''
        # This can be seen as just a series of MoveOverlapSequenceItemsInPlaceCommand,
        # and that's just what we do, but there's a catch: without our original
        # in_motion checker algorithm (which I'd rather not go back to), these
        # commands must happen in the right order, and pretty much need to be
        # executed to see if they'll work. Someone in the future can work out a
        # shortcut algorithm to check the moves before we attempt them.
        QUndoCommand.__init__(self, 'Move sequence items in place', parent)
        self.mover = mover
        self.offset = offset
        self.sequence = self.mover.overlap_movers[0].items[0].sequence

        if not self.sequence:
            raise ValueError('The given items are not in a sequence.')

        if offset < 0:
            self.commands = [MoveSequenceOverlapItemsInPlaceCommand(overlap_mover, offset)
                for overlap_mover in mover.overlap_movers]
        else:
            self.commands = [MoveSequenceOverlapItemsInPlaceCommand(overlap_mover, offset)
                for overlap_mover in reversed(mover.overlap_movers)]

    def id(self):
        return id(MoveSequenceItemsInPlaceCommand)

    def mergeWith(self, command):
        if not isinstance(command, MoveSequenceItemsInPlaceCommand):
            return False

        if self.mover is not command.mover:
            return False

        # Combine commands
        if (self.offset < 0) != (command.offset < 0):
            for c1, c2 in zip(reversed(self.commands), command.commands):
                c1.mergeWith(c2)
        else:
            for c1, c2 in zip(self.commands, command.commands):
                c1.mergeWith(c2)

        # Reverse our commands if we're now going the other way
        if (self.offset < 0) != (self.offset + command.offset < 0):
            self.commands.reverse()

        self.offset += command.offset

    def check_room(self):
        # If redo() fails, redo() will roll itself back and raise an exception.
        # If redo() succeeds, we undo() to roll it back, and there is no exception.
        # TODO: Really, there's probably an algorithm we can use here to avoid
        # moving anything.
        self.redo()
        self.undo()

    def redo(self):
        cmd_index = -1

        try:
            for i in range(len(self.commands)):
                self.commands[i].redo()
                cmd_index = i
        except:
            for i in range(cmd_index, -1, -1):
                self.commands[i].undo()

            raise

    def undo(self):
        for command in reversed(self.commands):
            command.undo()

class RemoveAdjacentItemsFromSequenceCommand(QUndoCommand):
    '''Removes adjacent (or single) items from a sequence, trying not to disturb
    the timing in the sequence.

    This command may move the sequence or adjust the transition lengths of items
    to retain the sequence's timing.'''

    def __init__(self, items, parent=None):
        # Items supplied to this command need to be adjacent in the same sequence
        # TODO: How does this kind of command, which hangs onto old clips,
        # interact with asset name changes?
        #   If the user does change an asset name as it is, at the very least when
        #   they undo over this step, the graph manager will look for an asset
        #   that's not there (or worse, a different asset with the same name!).
        #   (1) Perhaps this can be solved with a kind of "global" command, one that
        #       appears on all stacks and undoes the global action when traversed.
        #   (2) Or we can reach into the undo stack and commit name changes there,
        #       too? Certain commands will listen to the asset list and modify items
        #       they hold?
        #   (3) Maybe there is only one undo stack to begin with? That kind of
        #       stack could undo name changes. -- IXNAY, users won't like that.
        #   (4) Or, as above, accept that we can't track all asset name changes,
        #       and leave it up to the user to do something smart. This at least
        #       can hold until I get a better idea on what to do.

        QUndoCommand.__init__(self, 'Delete adjacent item(s) from sequence', parent)

        for i in range(0, len(items) - 1):
            if items[i].index != items[i+1].index - 1:
                raise ValueError('This operation is only supported on adjacent items.')

        self.items = items
        self.original_sequence = items[0].sequence

        # Original position X in scene
        self.original_x = items[0].x + self.original_sequence.x

        self.length = items[-1].x + items[-1].length - items[0].x
        self.original_sequence = items[0].sequence
        self.original_sequence_index = items[0].index
        self.original_next = items[-1].next_item()
        self.original_next_trans_length = self.original_next and self.original_next.transition_length
        self.orig_trans_length = items[0].transition_length

    def redo(self):
        del self.original_sequence[self.original_sequence_index:self.original_sequence_index + len(self.items)]

        if self.original_sequence_index == 0:
            self.original_sequence.update(x=self.original_sequence.x + self.length
                - self.original_next.transition_length if self.original_next else 0)

        if self.original_next:
            self.original_next.update(transition_length=0 if self.original_sequence_index == 0 else (self.original_next_trans_length - self.length + self.orig_trans_length))

    def undo(self):
        self.original_sequence[self.original_sequence_index:self.original_sequence_index] = self.items
        self.items[0].update(transition_length=self.orig_trans_length)

        if self.original_sequence_index == 0:
            self.original_sequence.update(x=self.original_x)

        if self.original_next:
            self.original_next.update(transition_length=self.original_next_trans_length)

class RemoveItemCommand(QUndoCommand):
    '''Removes an item from its container.

    This really works for any item in any mutable list, so long as the list's
    index method can find it. But this means it can also work for spaces.

    Sequences have special requirements as far as keeping items where they are.
    Use the RemoveItemsFromSequenceCommand to handle those.'''
    def __init__(self, list_, item, parent=None):
        QUndoCommand.__init__(self, 'Delete item', parent)
        self.list = list_
        self.item = item

    def redo(self):
        self.index = self.list.index(self.item)
        del self.list[self.index]

    def undo(self):
        self.list.insert(self.index, self.item)

class InsertItemCommand(QUndoCommand):
    '''Inserts an item into a list.

    This really works for any item in any mutable list, but it can also work for
    spaces.

    Sequences have special requirements as far as keeping items where they are.
    Use the AddOverlapItemsToSequenceSequenceCommand to handle those.'''
    def __init__(self, list_, item, index, parent=None):
        QUndoCommand.__init__(self, 'Insert item', parent)
        self.list = list_
        self.item = item
        self.index = index

    def redo(self):
        self.list.insert(self.index, self.item)

    def undo(self):
        del self.list[self.index]

class RemoveItemsFromSequenceCommand(QUndoCommand):
    '''Removes any set of items from a sequence. Note that each item needs to
    belong to the same sequence and must be specified only once.

    If all the items of a sequence are specified, the whole sequence is removed.'''
    def __init__(self, items, parent=None):
        QUndoCommand.__init__(self, 'Delete item(s) from sequence', parent)

        if len(items) == len(items[0].sequence):
            # Just remove the whole sequence
            RemoveItemCommand(items[0].sequence.space, items[0].sequence, self)
        else:
            items = sorted(items, key=lambda a: a.index)

            for group in _split_sequence_items_by_adjacency(items):
                RemoveAdjacentItemsFromSequenceCommand(group, parent=self)

class _AdjustClipHandleCommand(QUndoCommand):
    def __init__(self, text, item, offset, command, parent=None):
        QUndoCommand.__init__(self, text, parent)
        self.item = item
        self.offset = offset
        self.command = command

    def id(self):
        return id(self.__class__)

    def mergeWith(self, next):
        '''This command *can* be merged, but only manually.'''
        if not isinstance(next, self.__class__) or self.item != next.item:
            return False

        self.command.mergeWith(next.command)
        self.offset += next.offset
        return True

    def redo(self):
        if self.item.space is None:
            raise RuntimeError('Item must belong to a space to use ' + str(self.__class__) + '.')

        self.command.redo()

    def undo(self):
        self.command.undo()

class AdjustClipLengthCommand(_AdjustClipHandleCommand):
    '''Adjusts the length of a clip.'''

    def __init__(self, item, offset):
        if item.length + offset <= 0:
            raise NoRoomError

        _AdjustClipHandleCommand.__init__(self,
            'Adjust clip length', item, offset,
            UpdateItemPropertiesCommand(item, length=item.length + offset))

class AdjustClipStartCommand(_AdjustClipHandleCommand):
    '''Adjusts the start of a clip.'''

    def __init__(self, item, offset):
        if item.length - offset <= 0:
            raise NoRoomError

        _AdjustClipHandleCommand.__init__(self,
            'Adjust clip start', item, offset,
            UpdateItemPropertiesCommand(item,
                x=item.x + offset,
                offset=item.offset + offset,
                length=item.length - offset))

class SlipBehindCommand(_AdjustClipHandleCommand):
    '''Adjusts the offset of a clip.'''

    def __init__(self, item, offset):
        _AdjustClipHandleCommand.__init__(self,
            'Slip behind clip', item, offset,
            UpdateItemPropertiesCommand(item,
                offset=item.offset + offset))

class AdjustClipTopCommand(_AdjustClipHandleCommand):
    '''Adjusts the top of a clip.'''

    def __init__(self, item, offset):
        if item.height - offset <= 0.0:
            raise NoRoomError

        _AdjustClipHandleCommand.__init__(self,
            'Adjust clip top', item, offset,
            UpdateItemPropertiesCommand(item,
                y=item.y + offset,
                height=item.height - offset))

class AdjustClipHeightCommand(_AdjustClipHandleCommand):
    '''Adjusts the height of a clip.'''

    def __init__(self, item, offset):
        if item.height + offset <= 0.0:
            raise NoRoomError

        _AdjustClipHandleCommand.__init__(self,
            'Adjust clip height', item, offset,
            UpdateItemPropertiesCommand(item,
                height=item.height + offset))

class AdjustSequenceItemStartCommand(QUndoCommand):
    '''Adjusts the start of a sequence item without affecting the timing of its
    neighbors.'''

    def __init__(self, item, offset):
        if not item.sequence:
            raise RuntimeError('Item needs to belong to a sequence.')

        prev_item = item.previous_item()
        next_item = item.next_item()

        if item.length - offset < 1:
            raise NoRoomError('Cannot set length to zero or less.')

        if prev_item:
            prev_room = (prev_item.length
                # Room taken up by its own transition
                - max(prev_item.transition_length, 0)
                # Room taken up by ours
                - max(item.transition_length - offset, 0))

            if prev_room < 0:
                raise NoRoomError

        if next_item:
            # Don't run past the start of the next item
            if item.length - offset < next_item.transition_length:
                raise NoRoomError('Cannot move point past start of next item.')

        QUndoCommand.__init__(self, 'Adjust sequence clip start')

        self.item = item
        self.offset = offset
        self.item_command = UpdateItemPropertiesCommand(item,
                transition_length=item.transition_length - offset if prev_item else 0,
                offset=item.offset + offset,
                length=item.length - offset)
        self.seq_command = not prev_item and UpdateItemPropertiesCommand(item.sequence,
                x=item.sequence.x + offset)

    def id(self):
        return id(self.__class__)

    def mergeWith(self, next):
        if not isinstance(next, self.__class__) or self.item != next.item:
            return False

        self.item_command.mergeWith(next.item_command)
        self.offset += next.offset

        if self.seq_command:
            self.seq_command.mergeWith(next.seq_command)

        return True

    def redo(self):
        self.item_command.redo()

        if self.seq_command:
            self.seq_command.redo()

    def undo(self):
        if self.seq_command:
            self.seq_command.undo()

        self.item_command.undo()

class AdjustSequenceItemLengthCommand(QUndoCommand):
    '''Adjusts the length of a sequence item without affecting the timing of its
    neighbors.'''

    def __init__(self, item, offset):
        if not item.sequence:
            raise RuntimeError('Item needs to belong to a sequence.')

        next_item = item.next_item()
        next_next_item = next_item and next_item.next_item()

        if item.length + offset < 1:
            raise NoRoomError('Cannot set length to zero or less.')

        if next_item:
            next_room = (next_item.length
                # Room taken up by its own transition
                - (next_item.transition_length + offset)
                # Room taken up by the next
                - max(next_next_item.transition_length if next_next_item else 0, 0))

            if next_room < 0:
                raise NoRoomError

        QUndoCommand.__init__(self, 'Adjust sequence clip length')

        self.item = item
        self.offset = offset
        self.item_command = UpdateItemPropertiesCommand(item,
            length = item.length + offset)
        self.next_command = next_item and UpdateItemPropertiesCommand(next_item,
            transition_length=next_item.transition_length + offset)

    def id(self):
        return id(self.__class__)

    def mergeWith(self, next):
        if not isinstance(next, self.__class__) or self.item != next.item:
            return False

        self.item_command.mergeWith(next.item_command)
        self.offset += next.offset

        if self.next_command:
            self.next_command.mergeWith(next.next_command)

        return True

    def redo(self):
        self.item_command.redo()

        if self.next_command:
            self.next_command.redo()

    def undo(self):
        if self.next_command:
            self.next_command.undo()

        self.item_command.undo()

class BringItemForwardCommand(QUndoCommand):
    def __init__(self, item):
        QUndoCommand.__init__(self, 'Bring item forward')

        self.item = item
        self.remove_command = None
        self.insert_command = None

    def redo(self):
        item = self.item
        key = item.z
        overlaps = item.overlap_items()
        above_items = [x.z for x in overlaps if x.z < key]

        if not above_items:
            return

        bottom_z = max(above_items)

        self.remove_command = RemoveItemCommand(item.space, item)
        self.insert_command = InsertItemCommand(item.space, item, bottom_z)

        self.remove_command.redo()
        self.insert_command.redo()

    def undo(self):
        if self.insert_command:
            self.insert_command.undo()
            self.insert_command = None

            self.remove_command.undo()
            self.remove_command = None

class SendItemBackCommand(QUndoCommand):
    def __init__(self, item):
        QUndoCommand.__init__(self, 'Send item back')

        self.item = item
        self.remove_command = None
        self.insert_command = None

    def redo(self):
        item = self.item
        key = item.z
        overlaps = item.overlap_items()
        below_items = [x.z for x in overlaps if x.z > key]

        if not below_items:
            return

        top_z = min(below_items)

        self.remove_command = RemoveItemCommand(item.space, item)
        self.insert_command = InsertItemCommand(item.space, item, top_z)

        self.remove_command.redo()
        self.insert_command.redo()

    def undo(self):
        if self.insert_command:
            self.insert_command.undo()
            self.insert_command = None

            self.remove_command.undo()
            self.remove_command = None

