# greaseweazle/tools/diag/pinmap.py
#
# 34-pin floppy interface signals displayed by the interactive diagnostic.
#
# Written & released by Keir Fraser <keir.xen@gmail.com>
#
# This is free and unencumbered software released into the public domain.
# See the file COPYING for more details, or visit <http://unlicense.org>.

from typing import List, Tuple

# Density-select is an output we drive ourselves (no read-back pin exists
# in this firmware), so it's tracked in software rather than polled here.
DENSITY_SELECT_PIN = 2

# Input signals polled once per tick, in status-line display order.
# (label, pin, ambiguous)
#
# Pin numbers are the standard Shugart/IBM-PC 34-pin assignments and don't
# differ between the two cabling conventions for these particular signals.
# Pin 34's *meaning* does vary by drive family (Disk-Change on IBM/PC,
# often Ready on Shugart, remapped further on some drives e.g. Amiga) --
# hence 'ambiguous': show the raw level, not an asserted semantic.
SIGNALS: List[Tuple[str, int, bool]] = [
    ('WP',  28, False),
    ('DC',  34, True),
    ('TK0', 26, False),
]
