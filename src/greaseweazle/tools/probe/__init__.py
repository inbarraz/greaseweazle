# greaseweazle/tools/probe/__init__.py
#
# Greaseweazle control script: Probe drive parameters and feature support.
#
# This is free and unencumbered software released into the public domain.
# See the file COPYING for more details, or visit <http://unlicense.org>.

description = "Probe drive parameters and feature support."

import sys
from typing import Any, Dict

from greaseweazle import error
from greaseweazle import usb as USB
from greaseweazle.tools import util
from greaseweazle.tools.probe import max_track, max_track_write


def _report_max_track(result: max_track.Result) -> None:
    print()
    print('Max Track:')
    if result.status == max_track.OK:
        max_cylinder, cylinders = result.max_cylinder, result.cylinders
        assert max_cylinder is not None and cylinders is not None
        if result.saturated:
            print('  At least %d cylinders (0-%d)' % (cylinders, max_cylinder))
            print('  The head reached the probe cylinder, so the drive may go'
                  ' further.')
            print('  Re-run with a higher --max-cylinder to find the stop.')
        else:
            print('  %d cylinders (0-%d)' % (cylinders, max_cylinder))
            if result.observations:
                print('  Measured at probe cylinders %s -> travel %s'
                      % (','.join(str(c) for c, _ in result.observations),
                         ','.join(str(t) for _, t in result.observations)))
    elif result.status == max_track.NO_MOVEMENT:
        print('  UNKNOWN - the head did not move.')
        print('  Check the drive select and step lines, and that the drive is'
              ' powered.')
    elif result.status == max_track.FW_LIMIT:
        print('  UNKNOWN - the firmware would not seek that far.')
        print('  This is a Greaseweazle firmware limit, not a drive limit.')
    elif result.status == max_track.NO_TRK0:
        print('  UNKNOWN - no Track 0 signal.')
        print('  This probe measures against the Track 0 sensor, so it cannot'
              ' report a')
        print('  limit without one.')
    print('  (%s)' % result.detail)


def _report_max_track_write(result: max_track_write.Result,
                            stepping_answer: int) -> None:
    print()
    print('Max Track (write confirmation):')
    if result.status == max_track_write.OK:
        max_cylinder, cylinders = result.max_cylinder, result.cylinders
        assert max_cylinder is not None and cylinders is not None
        print('  %d cylinders (0-%d)' % (cylinders, max_cylinder))
        if result.max_cylinder == stepping_answer:
            print('  AGREES with the step-counting measurement.')
        else:
            print('  DISAGREES with the step-counting measurement (%d).'
                  % stepping_answer)
            print('  Trust this one: it does not count steps, so a stalled')
            print('  stepper cannot inflate it.')
    elif result.status == max_track_write.SKIPPED:
        print('  Not run.')
    elif result.status == max_track_write.WRPROT:
        print('  Not measured - the disk is write protected.')
    else:
        print('  Inconclusive.')
    print('  (%s)' % result.detail)
    if result.readings:
        print('  Cylinder -> marker read back: %s'
              % ', '.join('%d->%s' % (c, 'none' if m is None else m)
                          for c, m in result.readings))


def probe(usb: USB.Unit, args, results: Dict[str, Any]) -> None:
    """Run the probes and report, collecting results by probe name.

    Results are gathered into the caller's dict rather than returned, so
    that they survive a probe raising part-way through. The drive profile
    (a later task) is what will consume them.
    """

    print('Probing drive (this steps the head repeatedly)...')
    result = max_track.run(usb, args.max_cylinder)
    results[max_track.name] = result.as_dict()
    _report_max_track(result)

    if not args.write_test:
        return
    if result.status != max_track.OK or result.max_cylinder is None:
        print()
        print('Skipping the write confirmation: the non-destructive probe')
        print('found no cylinder limit to confirm.')
        results[max_track_write.name] = max_track_write.Result(
            max_track_write.SKIPPED, None,
            'Nothing to confirm.').as_dict()
        return

    confirm = max_track_write.run(usb, result.max_cylinder,
                                  assume_yes=args.yes)
    results[max_track_write.name] = confirm.as_dict()
    _report_max_track_write(confirm, result.max_cylinder)


def main(argv) -> None:

    epilog = (util.drive_desc + '''
Probes measure the drive itself, not a disk. Remove any disk before running:
repeatedly stepping the head across stationary media can score it. If a disk
must stay in the drive, use --motor-on so the media is turning.''')

    parser = util.ArgumentParser(usage='%(prog)s [options]', epilog=epilog)
    parser.add_argument("--device", help="device name (COM/serial port)")
    parser.add_argument("--drive", type=util.Drive(), default='A',
                        help="drive to probe")
    parser.add_argument("--max-cylinder", type=util.uint, metavar="N",
                        help="optional ceiling on how far the head is driven"
                        " (default: search until the drive stops it)")
    parser.add_argument("--motor-on", action="store_true",
                        help="probe with the drive motor running")
    parser.add_argument("--write-test", action="store_true",
                        help="also confirm the cylinder limit by writing"
                        " markers to the disk (DESTROYS the disk contents)")
    parser.add_argument("--yes", action="store_true",
                        help="approve the write test without prompting")
    parser.description = description
    parser.prog += ' ' + argv[1]
    args = parser.parse_args(argv[2:])

    # Writing markers needs the media turning, so the write test implies the
    # motor regardless of what was asked for.
    motor = args.motor_on or args.write_test

    results: Dict[str, Any] = {}
    try:
        usb = util.usb_open(args.device)
        util.with_drive_selected(lambda: probe(usb, args, results),
                                 usb, args.drive, motor=motor)
    except USB.CmdError as err:
        print("Command Failed: %s" % err)


if __name__ == "__main__":
    main(sys.argv)

# Local variables:
# python-indent: 4
# End:
