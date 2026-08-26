"""refusals.py -- the one place a validator's refusal becomes an HTTP response.

Small module, small test, and the point of both is the same: this is now the only line in the
hub that decides what a rejected request is told, so what it does needs to be stated
somewhere a reviewer can check rather than inferred from eighty call sites.

What is actually asserted:

  * **The message survives.** That is the whole reason the routes pass the exception rather
    than a status code -- "path may not contain '..'" is what makes a 400 actionable, and a
    refactor that quietly genericised it would pass every other test in this suite while
    making the console useless.
  * **The status is the caller's.** Most refusals are 400 and a handful are genuinely 403,
    409, 502 or 503; guessing from the exception type would be a lookup table that lies.
  * **An empty exception still says something.** ValueError() renders as a refusal with no
    reason, which reads as a broken hub rather than a rejected request.

Run from the repo root so `import refusals` resolves.
"""
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "hub"))

from flask import Flask

import refusals

PASS = 0
FAIL = 0

# jsonify needs an application context; nothing else here does.
app = Flask(__name__)


def check(name, cond):
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"  [ok] {name}")
    else:
        FAIL += 1
        print(f"  [XX] {name}")


def answer(exc, *args):
    """The (payload, status) a route would return for this refusal."""
    with app.app_context():
        response, status = refusals.refuse(exc, *args)
        return response.get_json(), status


def test_the_message_is_what_reaches_the_operator():
    print("\n== A refusal answers with the sentence the validator wrote ==")
    payload, status = answer(ValueError("path may not contain '..'"))
    check("the validator's own words, unaltered",
          payload == {"error": "path may not contain '..'"})
    check("...and 400 by default", status == 400)

    # The hub's own refusal classes, not just ValueError: firmware.PayloadRejected,
    # wake.WakeRejected, bios.ChangeRejected and the rest all arrive here the same way.
    class PayloadRejected(Exception):
        pass

    payload, _ = answer(PayloadRejected("that image is for another model"))
    check("a module's own refusal class reads the same",
          payload == {"error": "that image is for another model"})

    payload, _ = answer(PermissionError("you may not issue commands to PC-3"))
    check("...and so does a PermissionError", "PC-3" in payload["error"])

    # Surrounding whitespace comes from multi-line refusal text in a couple of modules and
    # would render as a gap before the sentence.
    payload, _ = answer(ValueError("  that schedule has already run\n"))
    check("whitespace around the message is trimmed",
          payload == {"error": "that schedule has already run"})


def test_the_status_is_the_callers_to_choose():
    print("\n== The status is passed, never inferred from the exception ==")
    check("400 is the default", answer(ValueError("no"))[1] == 400)
    for status in (403, 409, 502, 503):
        check(f"...and {status} is carried through",
              answer(ValueError("no"), status)[1] == status)
    # Same exception type, two different statuses: a lookup keyed on the type could not do
    # this, which is why there isn't one.
    check("one exception type can mean two different statuses",
          answer(ValueError("scope"), 403)[1] != answer(ValueError("scope"), 409)[1])


def test_an_exception_with_no_message():
    print("\n== A refusal with nothing to say still says something ==")
    payload, status = answer(ValueError())
    check("the console gets a sentence rather than an empty string",
          payload["error"] and payload["error"].strip() == payload["error"])
    check("...still at the caller's status", status == 400)
    payload, _ = answer(ValueError("   "))
    check("a whitespace-only message counts as none", payload["error"].strip() != "")


def main():
    test_the_message_is_what_reaches_the_operator()
    test_the_status_is_the_callers_to_choose()
    test_an_exception_with_no_message()
    print(f"\n==== {PASS} passed, {FAIL} failed ====")
    sys.exit(1 if FAIL else 0)


if __name__ == "__main__":
    main()
