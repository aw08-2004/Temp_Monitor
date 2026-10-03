"""One place where a refusal becomes an HTTP response.

Every module in this hub validates before it acts, and every validator says what is wrong in
a sentence somebody can act on: "path may not contain '..'", "that schedule has already
run", "a folder needs somewhere to be and a name". Those sentences are the product. An
operator who typed a bad path is told what is wrong with it and fixes it; a generic "400 Bad
Request" would send them to a colleague instead.

The route layer had been spelling that out by hand, once per route that answers one:

    except ValueError as e:
        return jsonify({"error": str(e)}), 400

which is correct in every one of those places and states the policy in none of them. This
module is that line, once. `refuse(e)` is what a route calls when a validator has already
decided the answer, and what it does with the exception is now a decision recorded in one
docstring rather than a habit copied down a file.

**What this deliberately does NOT change is which message goes out.** The response bodies are
byte-identical to what they were, because the messages are the useful part and every route
quietly starting to say "invalid request" instead would be a regression dressed as a
hardening. What changes is that there is now a single place to tighten this if that call is
ever made -- a message allowlist, a length cap, a distinction between our own refusals and
someone else's ValueError -- instead of one edit per route.

**On CodeQL's py/stack-trace-exposure (alert #133).** It flagged this function for as long as
it rendered `str(exc)`: the exception object itself flowing into a response, which is the
shape a traceback leak has. The risk was never real -- the routes catch the hub's own refusal
classes and its validators' ValueError and PermissionError -- but "nothing leaks because every
caller is careful" was a promise kept by 129 call sites rather than by this function. So the
function now keeps it itself (PR #106):

  * **It renders the sentence that was written, not the exception.** `exc.args[0]`, and only
    when that is a str. An exception constructed from something else -- another exception,
    a dict, a path object -- answers with the generic sentence, and its repr goes to the log.
  * **A traceback cannot get through.** A message containing "Traceback (most recent call
    last)" or a `File "...", line N` frame is replaced, whatever caught it.
  * **A cap.** A refusal is a sentence; anything past MAX_MESSAGE_CHARS is cut, so an
    exception that swallowed a whole HTTP body or file cannot be echoed wholesale.

Every message the validators actually write passes through byte-identical, which
tests/test_refusals.py pins. Rejected: rendering only the hub's own refusal classes and
genericising ValueError -- that is most of the 129 call sites, and the sentences are the
product, as the paragraph above says.

Flask-dependent by design, unlike the modules whose refusals it renders -- it IS the HTTP
layer, and jsonify is the thing it exists to call.
"""
import re

from flask import jsonify

GENERIC_REFUSAL = "That request was refused."
MAX_MESSAGE_CHARS = 1000
_TRACEBACK = re.compile(r"Traceback \(most recent call last\)|File \"[^\"]*\", line \d+")


def refuse(exc, status=400):
    """The refusal `exc` describes, as a JSON response with `status`.

    Called from an `except` block whose exception type is one the module chose to catch --
    a validator's ValueError, a PermissionError, or one of the hub's own refusal classes
    (firmware.PayloadRejected, wake.WakeRejected, bios.ChangeRejected and the rest). Anything
    a route did not expect should not be caught in the first place: it belongs in the 500 the
    framework already produces, where it is a bug report rather than an answer.

    The status is passed rather than inferred. Most of these are 400 -- the request was
    malformed -- but a handful are genuinely other things: 403 when a scope rule refuses,
    409 when the machine is in the wrong state for what was asked, 502/503 when something the
    hub depends on would not answer. Guessing that from the exception type would be a lookup
    table that lies the first time a module raises ValueError for a conflict.
    """
    return jsonify({"error": _authored_message(exc)}), status


def _authored_message(exc):
    """The sentence a validator wrote into `exc`, or the generic one. See the module
    docstring for why this reads `args[0]` rather than `str(exc)`."""
    args = getattr(exc, "args", None) or ()
    written = args[0] if args else ""
    if not isinstance(written, str):
        print(f"[refusals] refused with a non-text message from {type(exc).__name__}; "
              "answered generically")
        return GENERIC_REFUSAL
    message = written.strip()
    if _TRACEBACK.search(message):
        print(f"[refusals] a {type(exc).__name__} carried a traceback; answered generically")
        return GENERIC_REFUSAL
    # An exception raised with no message at all -- ValueError() -- would otherwise answer
    # with an empty string, which renders in the console as a refusal with no reason and
    # reads as a broken hub rather than a rejected request.
    if not message:
        return GENERIC_REFUSAL
    if len(message) > MAX_MESSAGE_CHARS:
        return message[:MAX_MESSAGE_CHARS].rstrip() + "..."
    return message
