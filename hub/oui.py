"""MAC vendor lookup for network discovery (roadmap #18): which company's network card answered.

A sweep's unmanaged column is mostly printers, access points, phones and the odd smart TV, and
an operator reading `3C:2A:F4:...` cannot tell which of those it is. The first three bytes of a
MAC (the OUI) are assigned by the IEEE to one manufacturer, so the registry turns "something at
.47" into "a Brother device at .47" -- usually enough to know whether it belongs there.

**The table is a committed snapshot, not a live download** (`hub/data/oui.tsv.gz`, rebuilt by
`tools/refresh_oui.py`). A hub that fetched the registry itself would gain an outbound
dependency on standards-oui.ieee.org and a failure path on every refresh; a snapshot cannot
fail at runtime. Its weakness is staleness, and that is the gentle direction here: the IEEE
never reassigns a block, so an old snapshot does not MISLABEL a device -- it just has no name
for a block assigned since. That was the roadmap's worry about this feature ("a stale one
mislabels rather than fails"), and it holds for vendor tables built by hand, not for this one.
Refreshing is one command, run when the snapshot is old enough to notice.

**Longest prefix wins.** Besides the 24-bit blocks (MA-L) the IEEE sells 28-bit (MA-M) and
36-bit (MA-S) ones carved out of blocks it registers to itself, so a MAC is tried against the
36-, then the 28-, then the 24-bit table. Without the longer two, thousands of small vendors'
devices would all read "IEEE Registration Authority".

**A locally administered MAC has no vendor**, and is reported as such rather than looked up.
Bit 1 of the first byte marks an address the device made up -- which is what every current
phone, and Windows' "random hardware addresses", put on Wi-Fi by default. Any registry match for
one would be a coincidence presented as a fact. "Private address" is itself a useful answer:
it is almost always a phone or a laptop, not a printer someone plugged in.

Flask-free and loaded lazily, once per process: discovery_web reads it per scan, and nothing
pays for the table on a hub that never sweeps.
"""
import gzip
import os
import threading

import wake

DATA_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "data", "oui.tsv.gz")

# Prefix lengths in hex digits, longest first: MA-S (36 bits), MA-M (28), MA-L (24).
_PREFIX_DIGITS = (9, 7, 6)

_lock = threading.Lock()
_table = None


def _load(path=None):
    """Read the snapshot into {hex_prefix: vendor}. A missing or unreadable file is an empty
    table, not an error: the vendor line is a convenience, and a hub without it must still
    show its sweeps."""
    table = {}
    try:
        with gzip.open(path or DATA_PATH, "rt", encoding="utf-8") as fh:
            for line in fh:
                if line.startswith("#"):
                    continue
                prefix, _, vendor = line.rstrip("\n").partition("\t")
                if len(prefix) in _PREFIX_DIGITS and vendor:
                    table[prefix.upper()] = vendor
    except (OSError, EOFError, UnicodeDecodeError) as e:
        print(f"[oui] Vendor table unavailable ({e.__class__.__name__}); sweeps will show "
              "MACs without a vendor.")
        return {}
    return table


def _ensure():
    global _table
    if _table is None:
        with _lock:
            if _table is None:
                _table = _load()
    return _table


def use_table(table):
    """Replace the loaded table -- for tests, which must not depend on the snapshot's
    contents changing under them every time it is refreshed."""
    global _table
    with _lock:
        _table = {str(k).upper(): v for k, v in (table or {}).items()}

def is_private(mac):
    """True for a locally administered MAC -- one the device chose itself. See the module
    docstring."""
    normalized = wake.normalize_mac(mac)
    return bool(normalized) and bool(int(normalized[:2], 16) & 0x02)


def lookup(mac):
    """`{"vendor": str|None, "private": bool}` for one MAC.

    `vendor` is None for a private address, for a malformed one, and for a block the snapshot
    does not know -- all three render as "no vendor", and only `private` is worth telling apart.
    """
    normalized = wake.normalize_mac(mac)
    if not normalized:
        return {"vendor": None, "private": False}
    if int(normalized[:2], 16) & 0x02:
        return {"vendor": None, "private": True}
    digits = normalized.replace(":", "")
    table = _ensure()
    for length in _PREFIX_DIGITS:
        vendor = table.get(digits[:length])
        if vendor:
            return {"vendor": vendor, "private": False}
    return {"vendor": None, "private": False}
