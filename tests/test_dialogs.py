"""The console's own confirm and notice dialogs (hub 1.142.0), and nothing native beside them.

The silent failures this file exists to catch:

  * **A native browser box creeping back.** window.confirm(), alert() and prompt() were on
    every destructive button until 1.142.0; they read "localhost:5000 says" over an OK that
    named nothing, and their chrome cannot be translated. One copied snippet brings the
    pattern back, and nothing else in the suite would notice -- the page still works.
  * **A destructive dialog whose focus sits on the destructive button.** confirmDialog's
    danger mode focuses Cancel so a reflexive Enter does nothing. Flip the index and the wipe
    confirmation on machine-secure.js is one keystroke from erasing a PC.
  * **Messages set as markup.** They quote machine, group and file names an operator typed.
  * **A dialog called but never awaited.** `if (!confirmDialog(...))` is a Promise, always
    truthy, so the action would be cancelled every time and no error says why.

Static checks over hub/static/js; no app import, so this module needs no temp DB.
"""
import os
import re
import sys

PASS = 0
FAIL = 0

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
JS = os.path.join(ROOT, "hub", "static", "js")


def check(name, cond):
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"  PASS  {name}")
    else:
        FAIL += 1
        print(f"  FAIL  {name}")


def read(path):
    with open(path, encoding="utf-8") as f:
        return f.read()


def code_only(js):
    """Comments removed -- several headers name confirm() to explain why it is gone."""
    js = re.sub(r"/\*.*?\*/", "", js, flags=re.S)
    return re.sub(r"//.*$", "", js, flags=re.M)


def scripts():
    for name in sorted(os.listdir(JS)):
        if name.endswith(".js"):
            yield name, code_only(read(os.path.join(JS, name)))


def test_no_native_dialogs():
    print("\n-- no native confirm/alert/prompt in any console script --")
    native = re.compile(r"(?<![\w.])(?:window\.)?(?:confirm|alert|prompt)\s*\(")
    offenders = [name for name, js in scripts() if native.search(js)]
    check(f"none found (offenders: {offenders})", not offenders)


def test_every_call_is_awaited():
    print("\n-- every confirmDialog() is awaited --")
    bare = []
    for name, js in scripts():
        if name == "common.js":
            continue
        for m in re.finditer(r"confirmDialog\s*\(", js):
            before = js[max(0, m.start() - 12):m.start()]
            if not re.search(r"await\s*$", before):
                bare.append(name)
    check(f"no un-awaited confirmDialog (offenders: {bare})", not bare)


def test_the_helper():
    print("\n-- the shared helper in common.js --")
    common = read(os.path.join(JS, "common.js"))
    check("confirmDialog() and noticeDialog() are common.js globals",
          "function confirmDialog(" in common and "function noticeDialog(" in common)
    fn = common.split("function _openDialog(", 1)[1].split("\n}", 1)[0]
    check("built on showModal()", "showModal()" in fn)
    check("messages are set as text", "textContent = message" in fn and "innerHTML" not in fn)
    check("Escape is answered through the dialog's cancel event",
          "addEventListener('cancel'" in fn)
    check("the dialog is removed once answered", "dialog.remove()" in fn)
    confirm = common.split("function confirmDialog(", 1)[1].split("\n}", 1)[0]
    check("danger focuses Cancel (index 0), otherwise the confirm button",
          "initialFocus: danger ? 0 : 1" in confirm)
    check("danger draws the destructive button style", "btn btn--danger" in confirm)


def main():
    test_no_native_dialogs()
    test_every_call_is_awaited()
    test_the_helper()
    print(f"\n==== {PASS} passed, {FAIL} failed ====")
    return 1 if FAIL else 0


if __name__ == "__main__":
    sys.exit(main())
