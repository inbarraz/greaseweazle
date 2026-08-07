# greaseweazle/tools/probe/max_track_write.py
#
# Probe: confirm the drive's cylinder limit by writing and reading markers.
#
# This is free and unencumbered software released into the public domain.
# See the file COPYING for more details, or visit <http://unlicense.org>.

# An independent check on max_track.py, and a stronger one, because it never
# counts steps. Counting steps is what makes the /TRK0 method vulnerable to a
# stalled stepper slipping poles and inventing travel that never happened.
#
# Instead: write a marker identifying each cylinder across a window spanning
# the suspected stop, then read them back. Below the stop each cylinder is its
# own physical track and reads back its own marker. At the stop and beyond,
# every write lands on the same physical track, so each overwrites the last
# and only the final one survives. The first cylinder that reads back somebody
# else's marker is therefore the stop itself.
#
#     write pass    79 -> trk79   80 -> trk80  ...  83 -> trk83
#                   84 -> trk83   85 -> trk83  ...  91 -> trk83  (all pile up)
#     read pass     79 reads 79   80 reads 80  ...  83 reads 91  <-- stop is 83
#
# Reads happen while stepping outward from a recalibrated cylinder 0, so no
# reversal is involved and there is no slip to confuse.
#
# DESTRUCTIVE: this erases the window it writes. It runs only behind
# consent.confirm().

import statistics
from typing import Any, Callable, Dict, List, NamedTuple, Optional, Tuple

from greaseweazle import error
from greaseweazle import usb as USB
from greaseweazle.tools.probe import consent

name = 'max-track-write'
summary = 'Confirm the cylinder limit by writing and reading back markers'

# Outcomes.
OK = 'ok'                       # Found the stop.
BEYOND_WINDOW = 'beyond-window' # Every cylinder read back its own marker.
AT_WINDOW_START = 'at-start'    # Mismatched immediately: window started late.
UNREADABLE = 'unreadable'       # A marker could not be decoded.
WRPROT = 'write-protected'      # Disk is protected; nothing was measured.
SKIPPED = 'skipped'             # User declined, or not requested.

# A marker is just a uniform flux period, unique per cylinder, which avoids
# pulling in any sector codec to write an identifier. The range stays well
# inside what an ordinary drive can resolve.
MARKER_BASE_US = 4.0
MARKER_STEP_US = 0.6

# How far a read-back period may drift and still be accepted. Deliberately
# well under half a step: a period that lands between two markers belongs to
# neither, and an unwritten or damaged track must decode to nothing rather
# than to whichever marker happens to be nearest.
MARKER_TOLERANCE_US = 0.2

# Keep every marker inside a span drives resolve comfortably. Long periods
# read back short: on the bench drive a track written at 14us came back near
# 12us, and narrowing the span did not cure it -- 10us still came back near
# 8.8us, consistently around 12% low. The likely cause is the read channel's
# gain control manufacturing transitions in the long gaps between real ones,
# which drags the median interval down.
#
# That is survivable, because the test only asks whether a cylinder holds its
# OWN marker; a misdecoded foreign marker answers that just as well as a
# correctly decoded one. What would NOT be survivable is a period drifting so
# far it decodes as nothing at all, since that reads as unreadable media. The
# span stays narrow to keep that margin, not because it makes the identities
# accurate. Do not start trusting which marker came back.
MARKER_MAX_US = 12.0

# How far either side of the suspected stop to write. Below it, enough
# cylinders to show markers reading back correctly; above it, enough to make
# the pile-up unmistakable.
WINDOW_BELOW = 4
WINDOW_ABOVE = 6


class Result(NamedTuple):
    status: str
    max_cylinder: Optional[int]
    detail: str
    readings: Tuple[Tuple[int, Optional[int]], ...] = ()

    @property
    def cylinders(self) -> Optional[int]:
        if self.max_cylinder is None:
            return None
        return self.max_cylinder + 1

    def as_dict(self) -> Dict[str, Any]:
        return {
            'status': self.status,
            'max_cylinder': self.max_cylinder,
            'cylinders': self.cylinders,
            'detail': self.detail,
            'readings': [[c, m] for c, m in self.readings],
        }


def marker_us(cylinder: int, lowest: int) -> float:
    '''Flux period that identifies this cylinder. Pure.'''
    return MARKER_BASE_US + (cylinder - lowest) * MARKER_STEP_US


def window_fits(lowest: int, highest: int) -> bool:
    '''True if every cylinder in the window gets a well-resolved marker.'''
    return marker_us(highest, lowest) <= MARKER_MAX_US


def decode_marker(median_us: float, lowest: int, highest: int) -> Optional[int]:
    '''Recover a cylinder number from a measured flux period. Pure.

    Returns None if the period matches no marker in the window, which is the
    honest answer for an unwritten or unreadable track.
    '''
    cylinder = lowest + round((median_us - MARKER_BASE_US) / MARKER_STEP_US)
    if not lowest <= cylinder <= highest:
        return None
    if abs(median_us - marker_us(cylinder, lowest)) > MARKER_TOLERANCE_US:
        return None
    return cylinder


def interpret(readings: List[Tuple[int, Optional[int]]]) -> Result:
    '''Find the stop from marker read-backs. Pure.

    'readings' is (cylinder, marker read back) in ascending cylinder order.
    '''
    error.check(len(readings) > 0, 'max-track-write: no readings')

    for position, (cylinder, marker) in enumerate(readings):
        if marker is None:
            return Result(
                UNREADABLE, None,
                'Cylinder %d read back no recognisable marker, so the disk '
                'or drive could not carry the test.' % cylinder,
                tuple(readings))
        if marker == cylinder:
            continue
        # Somebody else's marker: every write from here out landed on this
        # same physical track.
        if position == 0:
            return Result(
                AT_WINDOW_START, cylinder,
                'The very first cylinder tested already showed the pile-up, '
                'so the stop is at or below cylinder %d.' % cylinder,
                tuple(readings))
        return Result(
            OK, cylinder,
            'Cylinder %d read back a marker that was not its own (decoded as '
            '%d): writes beyond it all landed on the same track.'
            % (cylinder, marker),
            tuple(readings))

    return Result(
        BEYOND_WINDOW, None,
        'Every cylinder tested read back its own marker, so the stop lies '
        'beyond cylinder %d.' % readings[-1][0],
        tuple(readings))


def _write_marker(usb: USB.Unit, cylinder: int, lowest: int,
                  rev_ticks: float) -> None:
    period = round(marker_us(cylinder, lowest) * 1e-6 * usb.sample_freq)
    # Overfill by a margin: the write is cut off at the index pulse, and
    # coming up short would leave the tail of the previous marker in place.
    count = int(rev_ticks / period) + 64
    usb.write_track([period] * count, terminate_at_index=True)


def _read_marker(usb: USB.Unit, lowest: int,
                 highest: int) -> Optional[int]:
    flux = usb.read_track(1)
    if len(flux.list) < 2:
        return None
    median_us = statistics.median(flux.list) / usb.sample_freq * 1e6
    return decode_marker(median_us, lowest, highest)


def run(usb: USB.Unit, suspected_stop: int,
        assume_yes: bool = False,
        prompt: Optional[Callable[[str], str]] = None,
        report: Callable[[str], None] = print) -> Result:
    '''Confirm a suspected cylinder limit by writing markers around it.'''

    error.check(suspected_stop >= 0, 'suspected stop must not be negative')

    if not consent.confirm('The max-track write confirmation',
                           assume_yes=assume_yes, prompt=prompt):
        return Result(SKIPPED, None, 'User declined the write test.')

    lowest = max(0, suspected_stop - WINDOW_BELOW)
    highest = suspected_stop + WINDOW_ABOVE
    error.check(window_fits(lowest, highest),
                'max-track-write: cylinders %d-%d need markers beyond %.1fus, '
                'which will not read back reliably'
                % (lowest, highest, MARKER_MAX_US))

    try:
        usb.seek(0, 0)
        flux = usb.read_track(1)
        error.check(len(flux.index_list) > 0,
                    'No index pulse: cannot time a track to write markers.')
        rev_ticks = flux.index_list[-1]

        report('  Writing markers to cylinders %d-%d...' % (lowest, highest))
        for cylinder in range(lowest, highest + 1):
            usb.seek(cylinder, 0, check_trk0=False)
            _write_marker(usb, cylinder, lowest, rev_ticks)

        report('  Reading markers back...')
        usb.seek(0, 0)
        readings: List[Tuple[int, Optional[int]]] = []
        for cylinder in range(lowest, highest + 1):
            usb.seek(cylinder, 0, check_trk0=False)
            readings.append((cylinder, _read_marker(usb, lowest, highest)))
            # Once a cylinder shows the pile-up there is nothing further to
            # learn, and every cylinder past it reads the same track.
            if readings[-1][1] != cylinder:
                break

        return interpret(readings)

    except USB.CmdError as err:
        if consent.is_write_protected(err):
            return Result(
                WRPROT, None,
                'The disk is write protected, so nothing was measured. Open '
                'the write-protect tab and re-run.')
        raise
    finally:
        try:
            usb.seek(0, 0)
        except (USB.CmdError, error.Fatal):
            report('  Warning: could not return the head to cylinder 0.')

# Local variables:
# python-indent: 4
# End:
