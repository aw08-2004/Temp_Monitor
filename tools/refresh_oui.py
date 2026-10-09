"""Rebuild hub/data/oui.tsv.gz from the IEEE registry -- the MAC vendor snapshot network
discovery reads (hub/oui.py, roadmap #18).

    python tools/refresh_oui.py              # download the three registries and rebuild
    python tools/refresh_oui.py --from DIR   # rebuild from oui.csv/mam.csv/oui36.csv in DIR

Run it when the snapshot is old enough to notice -- a new vendor's devices showing no name in a
sweep. The IEEE never reassigns a block, so an old snapshot is incomplete, never wrong (see
hub/oui.py), and there is no schedule to keep. Commit the result like any other change to
`hub/`, with a HUB_VERSION bump: hub self-update ships the file, a deployed hub never runs this.

**The output is deterministic** -- sorted by prefix, gzip with no timestamp or filename in its
header, and no build date inside it either -- so a refresh that found nothing new produces a
byte-identical file and no diff. Without that, every run would look like a change and the
snapshot's history would be noise. When it was last refreshed is what `git log` on the file is
for.

Three registries, because hub/oui.py matches the longest prefix: MA-L (24-bit, oui.csv), MA-M
(28-bit, mam.csv) and MA-S (36-bit, oui36.csv). Only the prefix and organisation name are
kept; the postal addresses are most of the download and nothing on the console shows them.
"""
import argparse
import csv
import gzip
import io
import os
import sys
import urllib.request

SOURCES = {
    "oui.csv": "https://standards-oui.ieee.org/oui/oui.csv",
    "mam.csv": "https://standards-oui.ieee.org/oui28/mam.csv",
    "oui36.csv": "https://standards-oui.ieee.org/oui36/oui36.csv",
}
OUT = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                   "hub", "data", "oui.tsv.gz")
MAX_VENDOR_CHARS = 80


def parse(text):
    """{hex_prefix: organisation} out of one IEEE CSV. The columns are
    Registry, Assignment, Organization Name, Organization Address."""
    out = {}
    for row in csv.DictReader(io.StringIO(text)):
        prefix = (row.get("Assignment") or "").strip().upper()
        name = " ".join((row.get("Organization Name") or "").split())[:MAX_VENDOR_CHARS]
        if len(prefix) in (6, 7, 9) and name and all(c in "0123456789ABCDEF" for c in prefix):
            out[prefix] = name
    return out


def build(texts):
    """The snapshot's bytes from the three CSV texts. Pure, so the determinism is testable."""
    table = {}
    for text in texts:
        table.update(parse(text))
    lines = [f"# IEEE MA-L/MA-M/MA-S registry snapshot, {len(table)} prefixes"]
    lines += [f"{prefix}\t{name}" for prefix, name in sorted(table.items())]
    raw = ("\n".join(lines) + "\n").encode("utf-8")
    buffer = io.BytesIO()
    # mtime=0 and no filename: the gzip header otherwise carries both, and the output would
    # differ on every run even when the registry did not.
    with gzip.GzipFile(filename="", mode="wb", fileobj=buffer, mtime=0, compresslevel=9) as gz:
        gz.write(raw)
    return buffer.getvalue(), len(table)


def fetch(url):
    request = urllib.request.Request(url, headers={"User-Agent": "FleetHub-refresh-oui"})
    with urllib.request.urlopen(request, timeout=120) as response:
        return response.read().decode("utf-8", errors="replace")


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--from", dest="source_dir",
                        help="read oui.csv, mam.csv and oui36.csv from this directory")
    args = parser.parse_args(argv)

    texts = []
    for name, url in SOURCES.items():
        if args.source_dir:
            with open(os.path.join(args.source_dir, name), encoding="utf-8",
                      errors="replace") as fh:
                texts.append(fh.read())
        else:
            print(f"downloading {url}")
            texts.append(fetch(url))
    data, count = build(texts)
    if count < 30000:
        # The MA-L registry alone is well over 30,000 blocks. Fewer means a truncated
        # download or an error page parsed as CSV, and writing it would quietly strip vendor
        # names off most of the fleet's sweeps.
        print(f"refusing to write: only {count} prefixes parsed")
        return 1
    os.makedirs(os.path.dirname(OUT), exist_ok=True)
    with open(OUT, "wb") as fh:
        fh.write(data)
    print(f"wrote {OUT}: {count} prefixes, {len(data)} bytes")
    return 0


if __name__ == "__main__":
    sys.exit(main())
