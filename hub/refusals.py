"""One place where a refusal becomes an HTTP response.

Every module in this hub validates before it acts, and every validator says what is wrong in
a sentence somebody can act on: "path may not contain '..'", "that schedule has already
run", "a folder needs somewhere to be and a name". Those sentences are the product. An
operator who typed a bad path is told what is wrong with it and fixes it; a generic "400 Bad
Request" would send them to a colleague instead.

The route layer has been spelling that out by hand, eighty-odd times:

    except ValueError as e:
        return jsonify({"error": str(e)}), 400

which is correct in every one of those places and states the policy in none of them. This
module is that line, once. `refuse(e)` is what a route calls when a validator has already
decided the answer, and what it does with the exception is now a decision recorded in one
docstring rather than a habit copied down a file.

**What this deliberately does NOT change is which message goes out.** The response bodies are
byte-identical to what they were, because the messages are the useful part and eighty-two
routes quietly starting to say "invalid request" instead would be a regression dressed as a
hardening. What changes is that there is now a single place to tighten this if that call is
ever made -- a message allowlist, a length cap, a distinction between our own refusals and
someone else's ValueError -- instead of eighty-two.

**On CodeQL's py/stack-trace-exposure.** It flags every one of these, here and in every other
Flask app, because it cannot tell an exception carrying an authored sentence from one
carrying a traceback. Nothing reaching a client through this function is a traceback: the
exception types the routes catch are the hub's own refusal classes plus ValueError and
PermissionError from its own validators. The alert is a true statement about the shape of the
code and a false one about the risk, and it is worth having said in one place that a reviewer
can read rather than in eighty-two places nobody reads twice.

Flask-dependent by design, unlike the modules whose refusals it renders -- it IS the HTTP
layer, and jsonify is the thing it exists to call.
"""
from flask import jsonify


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
    message = str(exc).strip()
    # An exception raised with no message at all -- ValueError() -- would otherwise answer
    # with an empty string, which renders in the console as a refusal with no reason and
    # reads as a broken hub rather than a rejected request. This is the one case where the
    # message is invented here, because there is none to pass on.
    if not message:
        message = "That request was refused."
    return jsonify({"error": message}), status
