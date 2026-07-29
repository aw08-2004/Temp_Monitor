"""Pins the wiring behind the searchable dropdowns (autocomplete.js).

Every long <select> and every <datalist>-bound input in the console is upgraded to the
same search-as-you-type combobox, automatically, by a document-level sweep. Nothing on
the pages themselves opts in -- which is the point, but it also means the whole feature
hangs off two joins that no other test would notice breaking:

  * base.html must load autocomplete.js on every page (it used to be a permissions-only
    include), and must load it before the per-page scripts that call attachAutocomplete.
  * the class names the sweep applies must be the ones components.css styles. Rename one
    side and the dropdown still works but renders as an unstyled text box with an
    invisible native <select> stacked on it.

Like test_mobile_nav.py, this asserts the joins, not the behaviour -- there is no browser
harness in this repo. The interaction itself still needs eyes on a page.
"""
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


def read(path):
    with open(path, encoding="utf-8") as fh:
        return fh.read()


def main():
    base = read(os.path.join(TEMPLATES, "base.html"))
    js = read(os.path.join(STATIC, "js", "autocomplete.js"))
    css = read(os.path.join(STATIC, "css", "components.css"))

    check("base.html loads autocomplete.js", "js/autocomplete.js" in base)
    # The per-page scripts block renders after it, so anything calling attachAutocomplete
    # at parse time (permissions.js does) finds the global already defined.
    check(
        "autocomplete.js loads before the page_scripts block",
        base.index("js/autocomplete.js") < base.index("page_scripts"),
    )

    # Loading it twice would install a second document observer doing the same sweep.
    dupes = [
        name
        for name in os.listdir(TEMPLATES)
        if name.endswith(".html")
        and name != "base.html"
        and "js/autocomplete.js" in read(os.path.join(TEMPLATES, name))
    ]
    check(f"no page re-includes autocomplete.js (found: {dupes})", not dupes)

    for symbol in ("attachAutocomplete", "enhanceSelect", "enhanceSelectsIn"):
        check(f"autocomplete.js exports {symbol}", f"window.{symbol} = " in js)

    for cls in ("select-search", "select-search__native", "select-search__input"):
        check(f"components.css styles .{cls}", f".{cls}" in css)
        check(f"autocomplete.js applies {cls}", cls in js)

    # The native control has to stay in the DOM: callers keep reading and writing
    # select.value and rebuilding options. Collapsing it (not display:none, not removing
    # it) is what keeps that contract.
    native = re.search(r"\.select-search__native\s*\{([^}]*)\}", css)
    check("the native <select> is collapsed, not removed", bool(native) and "opacity: 0" in native.group(1))
    check("picking an option dispatches a bubbling change", "new Event('change', { bubbles: true })" in js)

    # Opt-outs are the only escape hatch pages have; both must remain honoured.
    check("data-no-search opts a control out", "noSearch" in js)
    check("data-search opts a short control in", "dataset.search" in js)

    print(f"\n{PASS} passed, {FAIL} failed")
    return 1 if FAIL else 0


if __name__ == "__main__":
    sys.exit(main())
