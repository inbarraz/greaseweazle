# greaseweazle/tools/diag/__init__.py
#
# Greaseweazle control script: Interactive live disk/drive diagnostic.
#
# Written & released by Keir Fraser <keir.xen@gmail.com>
#
# This is free and unencumbered software released into the public domain.
# See the file COPYING for more details, or visit <http://unlicense.org>.

description = "Interactive live disk/drive diagnostic."

import os, sys, time
from typing import List, Optional, Tuple

from greaseweazle import error
from greaseweazle import usb as USB
# See decode.py for why codec.codec must be imported before ibm.ibm.
from greaseweazle.codec import codec  # noqa: F401
from greaseweazle.codec.ibm.ibm import Mode
from greaseweazle.tools import util
from greaseweazle.tools.diag import pinmap, decode

if os.name == 'nt':
    import msvcrt

KEYLEGEND = """\
Keys: 0-9=goto track N0  +/-/<-/->=step 1  r=recalibrate
      h=head  m=motor  d=density-select  q/Esc=quit"""

# (rate_kbps, nominal_rpm) -> (sectors/track, description). Sector count
# depends on rate *and* rpm together -- e.g. 500kbps is 15 sec/trk at
# 360rpm (1.2MB 5.25" HD) but 18 sec/trk at 300rpm (1.44MB 3.5" HD) -- so
# rate alone can't disambiguate it. Used both for the startup cheat sheet
# and to guess --secs when the user doesn't supply it.
STANDARD_FORMATS = {
    (250, 300): (9, '360KB 5.25" DD / 720KB 3.5" DD'),
    (500, 360): (15, '1.2MB 5.25" HD'),
    (500, 300): (18, '1.44MB 3.5" HD'),
    (1000, 300): (36, '2.88MB 3.5" ED'),
}


def cheatsheet() -> str:
    lines = ['Typical formats (rate @ rpm = sec/trk):']
    for (rate, rpm), (secs, desc) in sorted(STANDARD_FORMATS.items()):
        lines.append('  %5d kbps @ %3d rpm = %2d sec/trk  (%s)' %
                     (rate, rpm, secs, desc))
    return '\n'.join(lines)


def guess_secs(rate: int, rpm: Optional[float]) -> Optional[int]:
    if rpm is None:
        return None
    nominal_rpm = 360 if rpm >= 330 else 300
    entry = STANDARD_FORMATS.get((rate, nominal_rpm))
    return entry[0] if entry else None


_ARROW_MAP = {b'H': 'up', b'P': 'down', b'K': 'left', b'M': 'right'}


class State:
    def __init__(self, args) -> None:
        self.args = args
        self.cyl = 0
        self.head = 0
        self.motor = True
        self.density = False
        self.last_rpm: Optional[float] = None  # self-corrects the read window


def read_key() -> Optional[str]:
    ch = msvcrt.getch()
    if ch in (b'\x00', b'\xe0'):
        return _ARROW_MAP.get(msvcrt.getch())
    if ch in (b'\r', b'\n'):
        return 'enter'
    if ch == b'\x1b':
        return 'esc'
    if ch == b'\x08':
        return 'backspace'
    try:
        return ch.decode('ascii')
    except UnicodeDecodeError:
        return None


def try_seek(usb: USB.Unit, st: State, new_cyl: int) -> None:
    # No upper clamp -- the user can deliberately probe past the declared
    # --cyls (e.g. to find a drive's real mechanical limit); usb.seek()
    # itself rejects nonsense values, and a too-far seek on real hardware
    # surfaces as a CmdError/Fatal we catch below rather than a crash.
    new_cyl = max(0, new_cyl)
    try:
        usb.seek(new_cyl, st.head)
        st.cyl = new_cyl
    except (error.Fatal, USB.CmdError) as e:
        print(str(e))
        return
    if st.args.gen_tg43:
        st.density = st.cyl < pinmap.TG43_TRACK_THRESHOLD
        usb.set_pin(pinmap.DENSITY_SELECT_PIN, st.density)


def recalibrate(usb: USB.Unit, st: State) -> None:
    print('Recalibrating to track 0')
    prior = st.cyl
    try_seek(usb, st, 0)
    try_seek(usb, st, prior)


def handle_key(usb: USB.Unit, st: State, key: Optional[str]) -> bool:
    """Returns False to request quit."""

    if key in ('q', 'esc'):
        return False
    elif key in ('left', ',', '-'):
        try_seek(usb, st, st.cyl - 1)
    elif key in ('right', '.', '+'):
        try_seek(usb, st, st.cyl + 1)
    elif key is not None and key.isdigit():
        try_seek(usb, st, int(key) * 10)
    elif key == 'h':
        if st.args.heads == 2:
            st.head = 1 - st.head
    elif key == 'r':
        recalibrate(usb, st)
    elif key == 'm':
        st.motor = not st.motor
        usb.drive_motor(st.args.drive.unit_id, st.motor)
    elif key == 'd':
        if not st.args.gen_tg43:  # pin 2 is auto-tracked, 'd' is a no-op
            st.density = not st.density
            usb.set_pin(pinmap.DENSITY_SELECT_PIN, st.density)
    return True


def drive_label(drive: util.Drive) -> str:
    if drive.bus == USB.BusType.IBMPC:
        return 'B' if drive.unit_id == 1 else 'A'
    return str(drive.unit_id)


def status_line(usb: USB.Unit, st: State) -> str:

    args = st.args

    sigs = {}
    wp_level: Optional[bool] = None
    for label, pin, ambiguous in pinmap.SIGNALS:
        try:
            level = usb.get_pin(pin)
        except USB.CmdError:
            sigs[label] = '%d:?' % pin
            continue
        sigs[label] = '%d:%s%s' % (pin, 'H' if level else 'L',
                                   '?' if ambiguous else '')
        if label == 'WP':
            wp_level = level

    wp_str = sigs['WP']
    if wp_level is not None:
        # This interface is active-low: WP asserted (L) == write-protected.
        wp_str += ' Unprot' if wp_level else ' Prot'

    rpm_str, rpm_val, sect = 'off', None, 0
    off_track: List[Tuple[int, int]] = []
    if st.motor:
        # Bound the capture by *time*, not by index pulses: with no disk
        # inserted there is never an index pulse, so usb.read_track(revs=N)
        # would block waiting for one that will never come. Sizing the
        # window from the last known RPM (falling back to --rpm, then a
        # generic guess) keeps it self-correcting once a real disk is in.
        assumed_rpm = args.rpm or st.last_rpm or 250.0
        ticks = int(usb.sample_freq * (60 / assumed_rpm) * 2.0)
        try:
            flux = usb.read_track(revs=0, ticks=ticks)
            tpr = flux.index_list[-1] / flux.sample_freq
            rpm_val = 60 / tpr
            rpm_str = '%.2f' % rpm_val
            st.last_rpm = rpm_val
            time_per_rev = (60 / args.rpm) if args.rpm else tpr
            mode = Mode.MFM if args.encoding == 'mfm' else Mode.FM
            sect, off_track = decode.decode_tick(
                flux, st.cyl, st.head, mode, args.rate, time_per_rev)
        except USB.CmdError:
            rpm_str = 'ERR'
        except Exception:
            # No disk / no index found, or garbage flux -- never let this
            # kill the session, just report nothing decoded this tick.
            rpm_str = 'ERR'
            sect, off_track = 0, []

    ot_str = ('NO' if not off_track else
             ','.join('T%d/S%d' % (c, n) for c, n in off_track))

    secs = args.secs if args.secs is not None else guess_secs(args.rate, rpm_val)
    secs_str = str(secs) if secs is not None else '?'

    return ('Drive %s, RPM %s, Kbps %d, T%d, H%d, S%d/%s, OT %s, '
            'WP %s, DC %s, TK0 %s, Density %d:%s' %
            (drive_label(args.drive), rpm_str, args.rate, st.cyl, st.head,
             sect, secs_str, ot_str, wp_str, sigs['DC'], sigs['TK0'],
             pinmap.DENSITY_SELECT_PIN, 'H' if st.density else 'L'))


def run(usb: USB.Unit, args) -> None:

    if os.name != 'nt':
        raise error.Fatal(
            'gw diag requires Windows (uses msvcrt for keyboard input)')

    st = State(args)
    print(cheatsheet())
    print(KEYLEGEND)
    if args.gen_tg43:
        print('TG43 auto-tracking enabled on pin 2 (threshold T%d); '
              'the d key is disabled' % pinmap.TG43_TRACK_THRESHOLD)
    try_seek(usb, st, 0)  # known starting position for the session

    next_tick = time.monotonic()
    while True:
        if msvcrt.kbhit():
            key = read_key()
            if key is not None and not handle_key(usb, st, key):
                break

        now = time.monotonic()
        if now >= next_tick:
            print(status_line(usb, st))
            next_tick = now + 0.5
        else:
            time.sleep(0.02)


def main(argv) -> None:

    epilog = (util.drive_desc)
    parser = util.ArgumentParser(usage='%(prog)s [options]', epilog=epilog)
    parser.add_argument("--device", help="greaseweazle device name")
    parser.add_argument("--drive", type=util.Drive(), default='A',
                        help="drive to diagnose")
    parser.add_argument("--cyls", type=util.min_int(1), default=84,
                        metavar="N", help="number of cylinders")
    parser.add_argument("--heads", type=int, choices=[1, 2], default=2,
                        help="number of heads")
    parser.add_argument("--encoding", choices=['mfm', 'fm'], default='mfm',
                        help="track encoding")
    parser.add_argument("--rate", type=util.min_int(1), required=True,
                        metavar="KBPS", help="data rate, in kbps")
    parser.add_argument("--secs", type=util.min_int(1), default=None,
                        metavar="N", help="expected sectors per track "
                        "(omit to guess from rate/rpm for standard formats)")
    parser.add_argument("--rpm", type=float, default=None,
                        help="fixed spindle speed, in rpm "
                        "(omit to track the live measurement)")
    parser.add_argument("--gen-tg43", action="store_true",
                        help="auto-drive pin 2 as a TG43 signal for "
                        "8-inch drives (low from T%d up, high below), "
                        "matching --gen-tg43 in read/write/align. "
                        "Disables the d key, since pin 2 is then "
                        "under automatic control" % pinmap.TG43_TRACK_THRESHOLD)
    parser.description = description
    parser.prog += ' ' + argv[1]
    args = parser.parse_args(argv[2:])

    try:
        usb = util.usb_open(args.device)
        usb.power_on_reset()
        util.with_drive_selected(lambda: run(usb, args), usb, args.drive)
    except USB.CmdError as err:
        print("Command Failed: %s" % err)


# Local variables:
# python-indent: 4
# End:
