# greaseweazle/tools/diag/decode.py
#
# Per-tick flux decode for the interactive diagnostic: counts sectors whose
# IDAM cylinder matches the head's current (expected) cylinder versus
# sectors that decoded cleanly but came from a different cylinder.
#
# Written & released by Keir Fraser <keir.xen@gmail.com>
#
# This is free and unencumbered software released into the public domain.
# See the file COPYING for more details, or visit <http://unlicense.org>.

from typing import Tuple

# Importing codec.codec first forces its module-level imports (which load
# ibm.py in full) to run before we touch ibm.py directly -- ibm.py and
# codec.hp.hp_mmfm import each other, so a direct `from ...ibm import ...`
# as the first touch of this package raises a partial-init ImportError.
from greaseweazle.codec import codec  # noqa: F401
from greaseweazle.codec.ibm.ibm import IBMTrack, Mode
from greaseweazle.flux import Flux


def decode_tick(
        flux: Flux,
        cyl: int,
        head: int,
        mode: Mode,
        rate_kbps: int,
        time_per_rev: float
) -> Tuple[int, int]:
    """Decode one tick's flux capture.

    Returns (sect, off_track): sect = cleanly-decoded sectors whose IDAM
    cylinder matches `cyl`; off_track = cleanly-decoded sectors whose IDAM
    cylinder doesn't (head mistracking).

    A fresh IBMTrack is built every call: IBMTrack.decode_raw() appends to
    self.sectors across calls without ever clearing it, so reusing one
    instance across ticks would accumulate stale sectors from earlier
    reads/cylinders.
    """
    t = IBMTrack(cyl, head, mode)
    t.clock = 5e-4 / rate_kbps
    t.time_per_rev = time_per_rev
    t.decode_flux(flux)

    sect = sum(1 for s in t.sectors if s.crc == 0 and s.idam.c == cyl)
    off_track = sum(1 for s in t.sectors if s.crc == 0 and s.idam.c != cyl)
    return sect, off_track
