# greaseweazle/tools/probe/__init__.py
#
# Greaseweazle control script: Probe drive parameters and feature support.
#
# This is free and unencumbered software released into the public domain.
# See the file COPYING for more details, or visit <http://unlicense.org>.

description = "Probe drive parameters and feature support."

import sys
from typing import Any, Dict, List, Optional

from greaseweazle import error
from greaseweazle import usb as USB
from greaseweazle.tools import util
from greaseweazle.tools.probe import max_track, max_track_write, trk0


def _report_trk0(result: trk0.Result) -> None:
    print()
    print('Track 0 Sensor:')
    if result.status == trk0.OK:
        print('  Working.')
    elif result.status == trk0.ABSENT_AT_HOME:
        print('  NO SIGNAL at cylinder 0.')
    elif result.status == trk0.STUCK_ASSERTED:
        print('  FAULTY - stuck asserted.')
    elif result.status == trk0.NO_REASSERT:
        print('  FAULTY - does not re-assert on return.')
    else:
        print('  FAULTY - intermittent.')
    print('  (%s)' % result.detail)
    if result.outward:
        print('  Out:  %s' % _trk0_trace(result.outward))
        print('  Back: %s' % _trk0_trace(result.homeward))


def _trk0_trace(samples) -> str:
    return ' '.join('%d:%s' % (cyl, 'ASSERT' if t else '-')
                    for cyl, t in samples)


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


# The probes, in the order they must run: an entry may depend on earlier
# ones, never on later ones. Dependencies are declared rather than implied by
# the ordering alone, so that selecting a probe on its own still pulls in
# whatever qualifies its result.
ORDER = [trk0.name, max_track.name, max_track_write.name]

SUMMARIES = {
    trk0.name: trk0.summary,
    max_track.name: max_track.summary,
    max_track_write.name: max_track_write.summary + ' (writes to the disk)',
}

DEPENDS_ON = {
    # Head position is measured against /TRK0, so a max-track figure taken
    # without validating the sensor would be unqualified.
    max_track.name: [trk0.name],
    # The write confirmation checks a limit that max-track must first find.
    max_track_write.name: [trk0.name, max_track.name],
}


def resolve(only: Optional[List[str]], write_test: bool) -> List[str]:
    '''Which probes to run, in order. Pure.

    'only' names the probes explicitly asked for, or None for all of them.
    Prerequisites are added automatically: running a probe without whatever
    qualifies its result would produce a number nobody should trust.
    '''
    if only is None:
        chosen = set(ORDER)
        # Destructive probes are opt-in, never part of a plain run.
        if not write_test:
            chosen.discard(max_track_write.name)
    else:
        unknown = [name for name in only if name not in ORDER]
        error.check(not unknown,
                    'Unknown probe(s): %s\nAvailable: %s'
                    % (', '.join(unknown), ', '.join(ORDER)))
        chosen = set(only)
        for name in list(chosen):
            chosen.update(DEPENDS_ON.get(name, []))

    return [name for name in ORDER if name in chosen]


def probe(usb: USB.Unit, args, results: Dict[str, Any]) -> None:
    """Run the selected probes and report, collecting results by name.

    Results are gathered into the caller's dict rather than returned, so
    that they survive a probe raising part-way through. The drive profile
    (a later task) is what will consume them.
    """

    selected = resolve(args.only, args.write_test)
    if args.only is not None:
        added = [name for name in selected if name not in args.only]
        if added:
            print('Also running %s, which the selection depends on.'
                  % ', '.join(added))

    print('Probing drive (this steps the head repeatedly)...')

    sensor = None
    if trk0.name in selected:
        sensor = trk0.run(usb)
        results[trk0.name] = sensor.as_dict()
        _report_trk0(sensor)
        if not sensor.usable:
            print()
            print('Skipping the remaining probes: they measure head position')
            print('against the Track 0 sensor, and it cannot be trusted.')
            return

    if max_track.name not in selected:
        return

    result = max_track.run(usb, args.max_cylinder)
    results[max_track.name] = result.as_dict()
    _report_max_track(result)

    if max_track_write.name not in selected:
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
    parser.add_argument("--only", action="append", metavar="PROBE",
                        help="run only this probe (repeatable); probes it"
                        " depends on are run too")
    parser.add_argument("--list-probes", action="store_true",
                        help="list the available probes and exit")
    parser.description = description
    parser.prog += ' ' + argv[1]
    args = parser.parse_args(argv[2:])

    if args.list_probes:
        for name in ORDER:
            print('  %-18s%s' % (name, SUMMARIES[name]))
        return

    # Selecting the write confirmation is itself a request to run it; the
    # consent prompt, not the flag, is what guards the disk.
    if args.only is not None and max_track_write.name in args.only:
        args.write_test = True

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
