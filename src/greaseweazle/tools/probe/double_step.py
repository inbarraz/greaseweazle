# greaseweazle/tools/probe/double_step.py
#
# Probe: does this disk need double-stepping in this drive?
#
# This is free and unencumbered software released into the public domain.
# See the file COPYING for more details, or visit <http://unlicense.org>.

# A disk written at half the drive's track pitch -- 48tpi media in a 96tpi
# drive -- puts one written track under two of the drive's cylinder
# positions. Read cylinders 4 and 5 and the same data comes back twice. That
# is the signature, and reading for it is the whole probe.
#
# NOT "48 vs 96 tpi", though the two get conflated. Tracks per inch is a
# physical distance and nothing at this interface can measure distance: no
# reading tells you how far the head moved, only what it found there. What
# CAN be established is whether this disk, in this drive, needs stepping two
# cylinders at a time -- which is the question anyone actually has, and which
# is a property of the COMBINATION rather than of the drive alone. The same
# drive gives a different answer with a different disk, and rightly so.
#
# NON-DESTRUCTIVE, necessarily. Writing anything would destroy the very thing
# being measured: the pitch of whatever is already on the disk. A writing
# version of this probe would answer only about tracks it had just written
# itself, which is no question at all.
#
# COMPARING TRACKS is the hard part, and a first attempt was thrown away. It
# matched flux intervals position by position and looked convincing until a
# control -- the same track against itself shifted by one interval -- scored
# exactly as high as an honest reread. Intervals on MFM media are quantised
# to a few values with one of them dominant, so position matching mostly
# measures how often the common value coincides with itself. It also assumed
# two reads start at the same place in the sequence, which they do not.
#
# What works is to ignore the sequence and look at where flux SITS around the
# revolution. Divide a revolution into segments by time from the index pulse
# and count transitions in each: the same track gives the same profile
# whatever the alignment, and a different track does not. Measured on written
# test tracks:
#
#     same track, read twice              0.001
#     two cylinders, identical content    0.001
#     two cylinders, different content    0.429
#
# A blank track has no profile to compare -- every segment holds the same
# count -- so tracks are checked for structure before being compared at all.
# Two blank tracks are identical in every way that can be measured, and
# reporting that as half-pitch media would be a lie. This is the false
# positive the whole probe has to avoid.
#
# PAIRS ARE EVEN-ALIGNED. A wide track covers cylinders 2n and 2n+1, so a
# pair must start on an even cylinder to fall inside one. Sampling 5 and 6
# would straddle two written tracks and show a difference on exactly the
# media this exists to detect.

from typing import (Any, Callable, Dict, List, NamedTuple, Optional,
                    Sequence, Tuple)

from greaseweazle import error
from greaseweazle import usb as USB
from greaseweazle.tools.probe import profile

name = 'double-step'
title = 'Double-Step'
summary = 'Whether the loaded disk needs double-stepping in this drive'
depends_on = ('index-sensor',)
destructive = False
needs_motor = True
wears_drive = False

tolerances = {
    'pairs': profile.IGNORED,
    'detail': profile.IGNORED,
}

# Outcomes.
MATCHED = 'matched'        # Adjacent cylinders differ: pitch matches.
HALF_PITCH = 'half-pitch'  # Adjacent cylinders identical: double-step.
UNCLEAR = 'unclear'        # Pairs disagreed with each other.
NO_DATA = 'no-data'        # Nothing on the disk to compare.

# Even-aligned pairs, kept low because nothing may assume how many cylinders
# the drive has and they run from about 37 upward.
SAMPLE_PAIRS = ((4, 5), (8, 9), (12, 13))

# Segments to divide a revolution into. Enough that a track's profile is
# distinctive, few enough that each holds a solid count of transitions.
SEGMENTS = 200

# A track's segment counts must vary by at least this fraction of their mean
# before it carries a profile worth comparing. Blank media measured 0.01 and
# written data 0.70, so this sits far from both.
STRUCTURE_MIN = 0.10

# Mean segment-count difference, relative, below which two tracks hold the
# same data and above which they hold different data. Measured: 0.001 for
# identical, 0.429 for different. The gap between the two thresholds is
# reported as unclear rather than forced to an answer.
IDENTICAL_MAX = 0.05
DIFFERENT_MIN = 0.15


class Pair(NamedTuple):
    lower: int
    upper: int
    # Segment-count variation on each track: below STRUCTURE_MIN there is
    # nothing to compare.
    structure_lower: float
    structure_upper: float
    difference: Optional[float] = None

    @property
    def comparable(self) -> bool:
        return (self.structure_lower >= STRUCTURE_MIN
                and self.structure_upper >= STRUCTURE_MIN)

    @property
    def verdict(self) -> Optional[str]:
        '''"same", "different", or None if it could not be told.'''
        if not self.comparable or self.difference is None:
            return None
        if self.difference <= IDENTICAL_MAX:
            return 'same'
        if self.difference >= DIFFERENT_MIN:
            return 'different'
        return None


class Result(NamedTuple):
    status: str
    detail: str
    pairs: Tuple[Pair, ...] = ()

    @property
    def double_step(self) -> Optional[bool]:
        if self.status == HALF_PITCH:
            return True
        if self.status == MATCHED:
            return False
        return None

    @property
    def ok(self) -> bool:
        return self.status in (MATCHED, HALF_PITCH)

    def as_dict(self) -> Dict[str, Any]:
        return {
            'status': self.status,
            'ok': self.ok,
            'detail': self.detail,
            'double_step': self.double_step,
            'pairs': [{'lower': p.lower, 'upper': p.upper,
                       'structure_lower': p.structure_lower,
                       'structure_upper': p.structure_upper,
                       'difference': p.difference,
                       'verdict': p.verdict} for p in self.pairs],
        }

    def report(self, out: Callable[[str], None]) -> None:
        if self.status == HALF_PITCH:
            out('  Double-stepping needed for this disk.')
        elif self.status == MATCHED:
            out('  No double-stepping needed for this disk.')
        elif self.status == NO_DATA:
            out('  UNKNOWN - nothing on the disk to compare.')
        else:
            out('  UNCLEAR - the cylinder pairs disagreed.')
        for p in self.pairs:
            if not p.comparable:
                out('  cyl %2d/%2d: no data (structure %.2f/%.2f)'
                    % (p.lower, p.upper, p.structure_lower, p.structure_upper))
            else:
                out('  cyl %2d/%2d: difference %.3f -> %s'
                    % (p.lower, p.upper, p.difference or 0.0,
                       p.verdict or 'unclear'))
        out('  (%s)' % self.detail)


def interpret(pairs: Sequence[Pair]) -> Result:
    '''Decide from the sampled cylinder pairs. Pure.'''

    error.check(len(pairs) > 0, 'double-step: no pairs sampled')

    verdicts = [p.verdict for p in pairs if p.verdict is not None]

    if not verdicts:
        return Result(
            NO_DATA,
            'No pair of cylinders carried data that could be compared. A '
            'blank or uniformly written disk looks the same at every '
            'cylinder, which would answer this question wrongly rather than '
            'not at all, so it is left unanswered. Load a disk with data on '
            'it and run this again.', tuple(pairs))

    if all(v == 'same' for v in verdicts):
        return Result(
            HALF_PITCH,
            'Each pair of adjacent cylinders read back the same data, so one '
            'written track covers two of this drive\'s cylinder positions. '
            'The disk was written at half this drive\'s pitch and needs '
            'stepping two cylinders at a time.', tuple(pairs))

    if all(v == 'different' for v in verdicts):
        return Result(
            MATCHED,
            'Adjacent cylinders held different data, so the disk was written '
            'at this drive\'s own pitch and wants single stepping.',
            tuple(pairs))

    return Result(
        UNCLEAR,
        'The pairs disagreed: %s. That fits neither a disk at this drive\'s '
        'pitch nor one at half it, so nothing is claimed.'
        % ', '.join('%d/%d %s' % (p.lower, p.upper, p.verdict or 'unclear')
                    for p in pairs),
        tuple(pairs))


def _signature(usb: USB.Unit, cylinder: int,
               segments: int = SEGMENTS) -> List[int]:
    '''Transitions per angular segment of one revolution.

    Keyed to time from the index pulse rather than to position in the flux
    sequence, so two reads of a track line up without having to be aligned.
    '''
    usb.seek(cylinder, 0, check_trk0=False)
    # The first read after a seek catches the head still settling and comes
    # back short; measured at about 30% low. Thrown away.
    usb.read_track(revs=1)
    flux = usb.read_track(revs=1)
    error.check(len(flux.index_list) >= 2,
                'double-step: need a full revolution between index pulses')

    start, span = flux.index_list[0], flux.index_list[1]
    counts = [0] * segments
    total = 0.0
    for interval in flux.list:
        total += interval
        if total < start:
            continue
        position = (total - start) / span
        if position >= 1.0:
            break
        counts[int(position * segments)] += 1
    return counts


def structure(counts: Sequence[int]) -> float:
    '''How much the segment counts vary, relative to their mean. Pure.

    Near zero for a blank or uniformly written track, which has no profile
    to compare and must not be compared.
    '''
    if not counts:
        return 0.0
    mean = sum(counts) / len(counts)
    if mean <= 0:
        return 0.0
    return (max(counts) - min(counts)) / mean


def difference(a: Sequence[int], b: Sequence[int]) -> float:
    '''Mean segment-count difference, relative to the mean count. Pure.'''
    error.check(len(a) == len(b) and len(a) > 0,
                'double-step: signatures must be the same non-zero length')
    mean = (sum(a) + sum(b)) / (len(a) + len(b))
    if mean <= 0:
        return 0.0
    return sum(abs(x - y) for x, y in zip(a, b)) / len(a) / mean


def measure(usb: USB.Unit,
            sample_pairs: Sequence[Tuple[int, int]] = SAMPLE_PAIRS) -> Result:
    '''Compare each even-aligned pair of adjacent cylinders.'''

    error.check(all(lower % 2 == 0 and upper == lower + 1
                    for lower, upper in sample_pairs),
                'double-step: pairs must be an even cylinder and its successor')

    try:
        pairs = []
        for lower, upper in sample_pairs:
            below, above = _signature(usb, lower), _signature(usb, upper)
            pair = Pair(lower, upper, structure(below), structure(above))
            if pair.comparable:
                pair = pair._replace(difference=difference(below, above))
            pairs.append(pair)
        return interpret(pairs)
    finally:
        try:
            usb.seek(0, 0, check_trk0=False)
        except USB.CmdError:
            pass


def run(ctx) -> Result:
    ctx.report('  Comparing adjacent cylinders at %s...'
               % ', '.join('%d/%d' % p for p in SAMPLE_PAIRS))
    return measure(ctx.usb)

# Local variables:
# python-indent: 4
# End:
