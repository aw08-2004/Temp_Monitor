"""Pins the wiring behind the device map (roadmap #23 phase C).

Like test_select_search.py and test_mobile_nav.py, this asserts the JOINS rather than the
behaviour -- there is no browser harness in this repo, so the map itself still needs eyes on a
page. What it does catch is the set of ways a map silently stops existing:

  * **`L is not defined`.** Leaflet must load before `location-map.js`, which must load before
    the page script that calls it. A wrong order is a blank card and one console error nobody
    is looking at, on a page whose other nine sections work perfectly.
  * **A renamed id.** `machine-location.js` finds its card, its map host and its button by id.
    Change one in the template and the fold simply never appears -- there is no error, because
    the script's own first line is a null check that returns quietly. That is the correct
    behaviour for a page without the card and indistinguishable from the bug.
  * **A missing stylesheet.** Leaflet with no CSS renders its tiles stacked in a column down
    the page rather than as a map, and a `.map` container with no height renders as nothing at
    all -- which is the single most common way a Leaflet map "does not appear".
  * **`innerHTML` creeping into a popup.** A popup carries a machine NAME, which is arbitrary
    text from a remote machine over an unauthenticated endpoint. The rest of the console builds
    DOM with textContent for exactly that reason.

It also pins the two vendored files against `static/vendor/README.md`, so a Leaflet upgrade
that forgets the checksum table fails here rather than making that table quietly untrue.
"""
import hashlib
import os
import re
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
STATIC = os.path.join(ROOT, "hub", "static")
TEMPLATES = os.path.join(ROOT, "hub", "templates")

PASS = 0
FAIL = 0


def check(name, cond):
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"  [ok] {name}")
    else:
        FAIL += 1
        print(f"  [XX] {name}")


def read(*parts):
    with open(os.path.join(*parts), encoding="utf-8") as fh:
        return fh.read()


def order(haystack, *needles):
    """True when every needle appears, in this order."""
    at = -1
    for needle in needles:
        found = haystack.find(needle, at + 1)
        if found <= at:
            return False
        at = found
    return True


def main():
    machine_html = read(TEMPLATES, "machine.html")
    map_html = read(TEMPLATES, "map.html")
    shared_js = read(STATIC, "js", "location-map.js")
    machine_js = read(STATIC, "js", "machine-location.js")
    fleet_js = read(STATIC, "js", "fleet-map.js")
    map_css = read(STATIC, "css", "map.css")

    print("== Leaflet is vendored, and the README says what is served ==")
    vendor_readme = read(STATIC, "vendor", "README.md")
    for name in ("leaflet.js", "leaflet.css"):
        path = os.path.join(STATIC, "vendor", name)
        check(f"{name} is checked in", os.path.exists(path))
        with open(path, "rb") as fh:
            digest = hashlib.sha256(fh.read()).hexdigest()
        # The table is the record of what is actually being served; a file replaced without
        # updating it makes that record a lie, silently.
        check(f"...and its SHA-256 is the one the vendor README publishes",
              digest in vendor_readme)
    check("the README pins the version too", "1.9.4" in vendor_readme)
    # The images/ directory is deliberately absent -- circleMarker and no layers control mean
    # leaflet.css's two image rules are unreachable. If somebody adds a default marker later,
    # this is the assertion that should start failing.
    check("no marker images are vendored, matching the README's reasoning",
          not os.path.isdir(os.path.join(STATIC, "vendor", "images")))

    print("\n== Load order: Leaflet, then the shared map, then the page ==")
    check("the machine page loads leaflet before the shared map module",
          order(machine_html, "vendor/leaflet.js", "js/location-map.js"))
    check("...then its own location script",
          order(machine_html, "js/location-map.js", "js/machine-location.js"))
    # collapse.js restores a fold by setting `open`, so anything hanging off `toggle` has to
    # have registered by then. machine-location.js does exactly that.
    check("...and both before collapse.js, which fires the toggle they listen for",
          order(machine_html, "js/machine-location.js", "js/collapse.js"))
    check("the map page loads leaflet, the shared module, then the page script",
          order(map_html, "vendor/leaflet.js", "js/location-map.js", "js/fleet-map.js"))

    print("\n== Stylesheets ==")
    for name, html in (("machine.html", machine_html), ("map.html", map_html)):
        check(f"{name} loads leaflet.css", "vendor/leaflet.css" in html)
        # Leaflet's own CSS gives the container no height; .map in ours does. Without it the
        # map is zero pixels tall and renders as nothing.
        check(f"{name} loads the console's map.css", "css/map.css" in html)
    check("map.css gives .map a height, which Leaflet cannot do for itself",
          re.search(r"\.map\s*\{[^}]*height:", map_css) is not None)
    check("...and a visible ground for a map whose tiles never load",
          re.search(r"\.map\s*\{[^}]*background:", map_css) is not None)
    check("the tileless state has a style of its own, so 'no tile source' does not read as "
          "'still loading'",
          ".map--tileless" in map_css and "map--tileless" in shared_js)

    print("\n== The ids the scripts look for exist in the markup ==")
    # The failure this catches: machine-location.js returns quietly when its card is missing,
    # which is correct for every other page and indistinguishable from a renamed id.
    for element_id in sorted(set(re.findall(r"getElementById\('([^']+)'\)", machine_js))):
        check(f"machine.html carries #{element_id}", f'id="{element_id}"' in machine_html)
    for element_id in sorted(set(re.findall(r"getElementById\('([^']+)'\)", fleet_js))):
        check(f"map.html carries #{element_id}", f'id="{element_id}"' in map_html)

    print("\n== The fold behaves like every other section on the machine page ==")
    check("the Location card is a fold with its own storage key",
          'data-fold-key="machine:overview:location"' in machine_html)
    check("...and ships HIDDEN, because most machines cannot locate at all",
          re.search(r'id="card-location"[^>]*hidden', machine_html) is not None)
    check("...and is NOT open by default, unlike Storage and Cooling",
          re.search(r'id="card-location"[^>]*\sopen[\s>]', machine_html) is None)
    check("the script re-measures the map when the fold opens, which Leaflet cannot notice",
          "invalidate()" in machine_js and "'toggle'" in machine_js)
    check("...and the shared module defers that measure past the browser's own layout",
          "requestAnimationFrame" in shared_js)

    print("\n== Popups are built as DOM, never as markup ==")
    # The ASSIGNMENT, not the word: these files talk about innerHTML in their own comments,
    # and a test that banned the string would be one somebody deletes rather than satisfies.
    writes_markup = re.compile(r"innerHTML\s*=|insertAdjacentHTML|outerHTML\s*=")
    for name, source in (("location-map.js", shared_js), ("machine-location.js", machine_js),
                         ("fleet-map.js", fleet_js)):
        check(f"{name} never assigns innerHTML", writes_markup.search(source) is None)
        check(f"...and builds text with textContent", "textContent" in source)
    check("the shared module builds its popup as an element, not a string",
          "createElement" in shared_js and "bindPopup(popup(" in shared_js)

    print("\n== The map never claims more precision than the fix had ==")
    check("an accuracy radius is drawn as a real circle in metres",
          "L.circle(" in shared_js and "radius: fix.accuracy_m" in shared_js)
    check("...and only when the device stated one",
          "if (fix.accuracy_m)" in shared_js)
    check("a stale fix is drawn differently from a fresh one",
          "STALE" in shared_js and "fix.stale ? STALE" in shared_js)
    check("...and the popup says which it is",
          "location.popup.last_known" in shared_js and "location.popup.fixed" in shared_js)
    check("the coordinates are always shown, for a hub with no tile source at all",
          "toFixed(5)" in shared_js)

    print("\n== The tile source comes from the server, not from the page ==")
    # Hardcoding a tile URL here would quietly undo hub/settings.py's map.tile_url and with it
    # the only way an air-gapped site has to point somewhere else.
    for name, source in (("location-map.js", shared_js), ("fleet-map.js", fleet_js),
                         ("machine-location.js", machine_js)):
        check(f"{name} hardcodes no tile host", "openstreetmap" not in source.lower())
    check("the shared module reads the tile URL from its config",
          "settings.tile_url" in shared_js)
    check("...and draws no tile layer at all when there is none",
          "if (settings.tile_url)" in shared_js)
    for name, source in (("fleet-map.js", fleet_js), ("machine-location.js", machine_js)):
        check(f"{name} passes the server's map block through", "data.map" in source)

    print(f"\n==== {PASS} passed, {FAIL} failed ====")
    return 1 if FAIL else 0


if __name__ == "__main__":
    sys.exit(main())
