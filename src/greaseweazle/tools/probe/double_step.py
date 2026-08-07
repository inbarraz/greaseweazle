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
# A SECOND ATTEMPT WAS ALSO THROWN AWAY, and it is worth knowing why. It
# counted transitions per angular segment and compared the density profiles:
# convincing on test patterns and on one partly-written disk, and useless on
# a normally formatted one. Write a disk with random data and every track
# carries the same sector layout at the same density, so the profiles match
# whether or not the tracks do. It announced that a freshly formatted 360k
# disk in its own 360k drive needed double-stepping. Density says nothing
# about data.
#
# What works is comparing WHERE INDIVIDUAL TRANSITIONS FALL. Take the time of
# each transition from the index pulse and ask what fraction of one track's
# transitions have a partner on the other within a fraction of a microsecond.
# The same track answers nearly all; a different track answers only as often
# as chance allows.
#
#     REAL FORMATTED MEDIA (a 360k disk in its own drive, random data)
#       same track, read twice       94.9%
#       adjacent cylinders           56.6%
#       cylinders far apart          55.7%
#
# The 56% is the chance rate: with transitions every few microseconds and a
# tolerance under one, that many coincide by luck. It is the floor, not a
# similarity, which is why the bar for "different" sits above it rather than
# near zero.
#
# Alignment is per segment. Two reads are cued to the index, but a spindle
# holding speed to a hundredth of a percent still drifts tens of microseconds
# across a revolution -- far more than the tolerance -- so each segment finds
# its own offset instead of trusting one for the whole track.
#
# A blank track must not be compared at all. Blank media reads as a regular
# grid of synthesised flux, so two blank tracks match perfectly and would be
# reported as half-pitch media. Tracks are therefore checked for VARIETY in
# their intervals first: real data mixes interval lengths, a blank track or a
# uniformly written one does not. This is the false positive the whole probe
# has to avoid.
#
# THE PREMISE ABOVE IS WRONG FOR REAL HALF-PITCH MEDIA, and the experiment
# that showed it is worth recording. A 360k disk formatted in its own 40-track
# drive was read in an 80-track one: 48tpi media in a 96tpi drive, the case
# this probe exists for. Adjacent cylinders did NOT read alike.
#
#     adjacent, even/odd     68% - 72% shared
#     two apart, even/even   59% - 62% shared
#     odd cylinders          2-3% more transitions, variety 0.59 to 0.85
#     even cylinders         variety 0.61 to 0.62, remarkably steady
#
# A 96tpi step is half a 48tpi pitch, so an even cylinder sits on a written
# track while the ODD one sits BETWEEN two of them and reads a mixture: more
# transitions than either neighbour, erratic interval variety, and only a
# partial share with the track beside it. Nothing like the near-identity the
# thresholds were built for.
#
# There is a signal in that -- adjacent pairs share about ten points more than
# distant ones, and odd cylinders are measurably noisier -- but it is far
# weaker than assumed and no threshold here captures it. Rather than tune a
# third metric against one drive and one disk, the probe now declines: every
# comparable pair must give the same definite answer or the result is
# unclear, which is what this media produces. See the task list.
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

# Segments of the revolution to sample, and how long each is. A handful
# spread around the track is plenty and costs far less than comparing all of
# it, since each segment pays for its own alignment search.
SEGMENT_US = 5000.0
SEGMENTS_SAMPLED = 8

# How close two transitions must fall to count as the same one, and how far
# apart the two reads may have drifted within a segment.
MATCH_US = 0.6
DRIFT_US = 40.0

# Fraction of intervals which must differ from the track's median before it
# counts as carrying data. Blank media is a regular grid and scores zero, as
# does a uniformly written test track; real data mixes interval lengths.
VARIETY_MIN = 0.05

# Fraction of transitions finding a partner. The same track re-read scored
# 94.9% and different tracks 56%, the latter being the rate at which
# transitions coincide by chance rather than any similarity. The gap between
# these is reported as unclear rather than forced to an answer.
SAME_MIN = 0.85
DIFFERENT_MAX = 0.70


class Pair(NamedTuple):
    lower: int
    upper: int
    # Interval variety on each track: below VARIETY_MIN there is no data to
    # compare, only a regular grid.
    variety_lower: float
    variety_upper: float
    # Fraction of transitions which found a partner on the other track.
    similarity: Optional[float] = None

    @property
    def comparable(self) -> bool:
        return (self.variety_lower >= VARIETY_MIN
                and self.variety_upper >= VARIETY_MIN)

    @property
    def verdict(self) -> Optional[str]:
        '''"same", "different", or None if it could not be told.'''
        if not self.comparable or self.similarity is None:
            return None
        if self.similarity >= SAME_MIN:
            return 'same'
        if self.similarity <= DIFFERENT_MAX:
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
                       'variety_lower': p.variety_lower,
                       'variety_upper': p.variety_upper,
                       'similarity': p.similarity,
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
                out('  cyl %2d/%2d: nothing to compare (variety %.2f/%.2f)'
                    % (p.lower, p.upper, p.variety_lower, p.variety_upper))
            else:
                out('  cyl %2d/%2d: %.1f%% of transitions shared -> %-9s '
                    '(variety %.2f/%.2f)'
                    % (p.lower, p.upper, (p.similarity or 0.0) * 100,
                       p.verdict or 'unclear',
                       p.variety_lower, p.variety_upper))
        out('  (%s)' % self.detail)


def interpret(pairs: Sequence[Pair]) -> Result:
    '''Decide from the sampled cylinder pairs. Pure.'''

    error.check(len(pairs) > 0, 'double-step: no pairs sampled')

    comparable = [p for p in pairs if p.comparable]
    verdicts = [p.verdict for p in comparable]

    if not comparable:
        return Result(
            NO_DATA,
            'No pair of cylinders carried data that could be compared. A '
            'blank or uniformly written disk looks the same at every '
            'cylinder, which would answer this question wrongly rather than '
            'not at all, so it is left unanswered. Load a disk with data on '
            'it and run this again.', tuple(pairs))

    if all(v is None for v in verdicts):
        # Quite different from having nothing to compare: there WAS data, and
        # it landed between the two thresholds, which is worth saying plainly
        # rather than dressing up as an absent disk.
        return Result(
            UNCLEAR,
            'Data was found and compared, but every pair fell between the '
            'thresholds -- too alike to be two tracks, too different to be '
            'one. Shared fractions were %s, against %.0f%% for the same '
            'track and %.0f%% for different ones.'
            % (', '.join('%.0f%%' % ((p.similarity or 0.0) * 100)
                         for p in comparable),
               SAME_MIN * 100, DIFFERENT_MAX * 100),
            tuple(pairs))

    # Every comparable pair must give the same definite answer. Deciding on
    # a majority, or on whichever pairs happened to be definite, once let a
    # single pair out of three settle a question the other two had declined
    # to answer.
    if any(v is None for v in verdicts):
        return Result(
            UNCLEAR,
            'Some pairs could not be told either way: %s. One pair that did '
            'answer is not enough to settle it while the others did not.'
            % ', '.join('%d/%d %s' % (p.lower, p.upper, p.verdict or 'unclear')
                        for p in comparable),
            tuple(pairs))

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


def _transitions(usb: USB.Unit, cylinder: int) -> List[float]:
    """Transition times in microseconds from the index pulse, one revolution."""
    usb.seek(cylinder, 0, check_trk0=False)
    # The first read after a seek catches the head still settling and comes
    # back short; measured at about 30% low. Thrown away.
    usb.read_track(revs=1)
    flux = usb.read_track(revs=1)
    error.check(len(flux.index_list) >= 2,
                'double-step: need a full revolution between index pulses')

    start, span = flux.index_list[0], flux.index_list[1]
    out: List[float] = []
    total = 0.0
    for interval in flux.list:
        total += interval
        if total < start:
            continue
        if total - start > span:
            break
        out.append((total - start) / flux.sample_freq * 1e6)
    return out


def variety(times: Sequence[float]) -> float:
    """Fraction of intervals differing from the median interval. Pure.

    Near zero for blank media, which reads as a regular grid of synthesised
    flux, and for a uniformly written track. Real data mixes interval
    lengths. A track without variety cannot be told from any other track
    without variety, so it must not be compared at all.
    """
    if len(times) < 3:
        return 0.0
    intervals = [b - a for a, b in zip(times, times[1:])]
    intervals.sort()
    median = intervals[len(intervals) // 2]
    if median <= 0:
        return 0.0
    return sum(1 for i in intervals
               if abs(i - median) > MATCH_US) / len(intervals)


def _best_overlap(here: Sequence[float], there: Sequence[float]) -> int:
    """Most of 'here' that can be paired with 'there' at any offset.

    Two passes: a coarse sweep to find roughly where the segment sits, then a
    fine one around it. Searching the whole range finely would cost twenty
    times as much for the same answer.
    """
    best = 0
    for coarse in range(-int(DRIFT_US), int(DRIFT_US) + 1, 2):
        best = max(best, _overlap(here, there, coarse))
    centre = 0.0
    for coarse in range(-int(DRIFT_US), int(DRIFT_US) + 1, 2):
        if _overlap(here, there, coarse) == best:
            centre = coarse
            break
    fine = centre - 2.0
    while fine <= centre + 2.0:
        best = max(best, _overlap(here, there, fine))
        fine += MATCH_US / 3
    return best


def _overlap(here: Sequence[float], there: Sequence[float],
             offset: float) -> int:
    """How many of 'here' have a partner in 'there', shifted by 'offset'."""
    buckets = set()
    for t in there:
        buckets.add(int((t + offset) / MATCH_US))
    return sum(1 for t in here
               if int(t / MATCH_US) in buckets
               or int(t / MATCH_US) - 1 in buckets
               or int(t / MATCH_US) + 1 in buckets)


def similarity(here: Sequence[float], there: Sequence[float]) -> float:
    """Fraction of transitions shared between two tracks. Pure.

    Each segment is aligned separately: a spindle holding speed to a
    hundredth of a percent still drifts tens of microseconds across a
    revolution, far more than transitions are being matched to.
    """
    if not here or not there:
        return 0.0
    span = min(here[-1], there[-1])
    if span <= 0:
        return 0.0

    matched = counted = 0
    for n in range(SEGMENTS_SAMPLED):
        low = span * n / SEGMENTS_SAMPLED
        high = low + SEGMENT_US
        if high > span:
            break
        mine = [t for t in here if low <= t < high]
        theirs = [t for t in there if low - DRIFT_US <= t < high + DRIFT_US]
        if len(mine) < 20 or not theirs:
            continue
        matched += _best_overlap(mine, theirs)
        counted += len(mine)

    return matched / counted if counted else 0.0


def measure(usb: USB.Unit,
            sample_pairs: Sequence[Tuple[int, int]] = SAMPLE_PAIRS) -> Result:
    '''Compare each even-aligned pair of adjacent cylinders.'''

    error.check(all(lower % 2 == 0 and upper == lower + 1
                    for lower, upper in sample_pairs),
                'double-step: pairs must be an even cylinder and its successor')

    try:
        pairs = []
        for lower, upper in sample_pairs:
            below, above = _transitions(usb, lower), _transitions(usb, upper)
            pair = Pair(lower, upper, variety(below), variety(above))
            if pair.comparable:
                pair = pair._replace(similarity=similarity(below, above))
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
