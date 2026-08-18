"""Tests scripts.py: the saved-script store a rule's `script` action references.

WHAT THIS FILE IS REALLY GUARDING is not CRUD. It is the trust boundary. A script body is
code that a rule runs as SYSTEM, unattended, on every machine it targets, and there are two
ways to reach that from a lower privilege than `issue_commands`:

  * writing the body directly -- closed by the capability gate, tested in test_rules_web.py;
  * getting text of your choosing INTERPOLATED into somebody else's body -- closed here, by
    refusing `{{field.*}}` and other operator-typed namespaces at save time. `manage_rules`
    can set a custom field's value and is deliberately not enough to issue commands, so a
    script reading `{{field.owner}}` would hand that account SYSTEM code execution.

Run from the repo root so `import scripts` resolves.
"""
import os
import sys
import tempfile

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "hub"))

import scripts

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


def fresh_db():
    path = os.path.join(tempfile.mkdtemp(prefix="hub-scripts-test-"), "t.db")
    scripts.init_scripts_db(path)
    return path


# Stands in for rules.lookup_variable: the real one is injected by rules_web so this module
# never imports `rules`. Only these two names exist as far as these tests are concerned.
KNOWN = {"sys.machine", "metric.cpu_temp"}


def known_variable(name):
    return name in KNOWN


def save(db, **kwargs):
    payload = dict(name="s1", label="", description="", shell="powershell",
                   body="Write-Host hi", inputs=[], timeout_seconds=600)
    payload.update(kwargs)
    return scripts.save_script(db, known_variable=known_variable, actor="op@x.com", **payload)


# ------------------------------------------------------------------------ shape
def test_names_and_limits():
    print("\n-- a script's name, shell, body and timeout --")
    db = fresh_db()

    err, script = save(db, name="clear_spooler")
    check("a valid script saves", err is None and script["name"] == "clear_spooler")

    check("a name with spaces is refused", save(db, name="clear spooler")[0] is not None)
    check("a name starting with a digit is refused", save(db, name="1st")[0] is not None)
    check("an empty name is refused", save(db, name="")[0] is not None)
    # Uppercase is normalised rather than refused: an operator typing a capital has made a
    # typo, not a request, and a store that silently held two scripts differing only in case
    # would let a rule reference the wrong one.
    err, script = save(db, name="MixedCase")
    check("a name is lowercased, not refused", err is None and script["name"] == "mixedcase")

    check("an empty body is refused", save(db, body="   ")[0] is not None)
    check("a body over the cap is refused",
          save(db, body="x" * (scripts.MAX_BODY_CHARS + 1))[0] is not None)
    check("a body at exactly the cap is accepted",
          save(db, body="x" * scripts.MAX_BODY_CHARS)[0] is None)

    check("an unknown shell is refused", save(db, shell="bash")[0] is not None)
    check("cmd is accepted", save(db, shell="cmd")[0] is None)

    check("a zero timeout is refused", save(db, timeout_seconds=0)[0] is not None)
    check("a timeout past 24h is refused",
          save(db, timeout_seconds=scripts.MAX_TIMEOUT_SECONDS + 1)[0] is not None)
    check("a non-numeric timeout is refused", save(db, timeout_seconds="soon")[0] is not None)


# ------------------------------------------------------------------------ inputs
def test_declared_inputs():
    print("\n-- declared inputs --")
    db = fresh_db()

    err, script = save(db, body='Stop-Service "{{input.service_name}}"',
                       inputs=[{"name": "service_name", "label": "Service", "required": True}])
    check("an input referenced and declared saves", err is None)
    check("the input round-trips",
          err is None and script["inputs"][0]["name"] == "service_name"
          and script["inputs"][0]["required"] is True)

    # The packages.validate_steps discipline: every variable bound at save time, so a typo is
    # caught while editing rather than by a fire record a week later.
    err, _ = save(db, body="Stop-Service {{input.typo}}",
                  inputs=[{"name": "service_name"}])
    check("an undeclared {{input.x}} is refused", err is not None and "typo" in err)

    err, _ = save(db, inputs=[{"name": "a"}, {"name": "a"}])
    check("a duplicate input name is refused", err is not None)
    err, _ = save(db, inputs=[{"name": "Not Valid"}])
    check("an input name with spaces is refused", err is not None)
    err, _ = save(db, inputs=[{"name": f"i{n}"} for n in range(scripts.MAX_INPUTS + 1)])
    check("too many inputs is refused", err is not None)

    # An input that is declared but never used is fine -- the rule still supplies it, and
    # refusing would make removing one line of a script a two-step edit.
    check("a declared but unused input is allowed",
          save(db, body="Get-Date", inputs=[{"name": "spare", "required": False}])[0] is None)


# ------------------------------------------------------------------------ the trust boundary
def test_template_namespace_is_restricted():
    print("\n-- what a script body may interpolate --")
    db = fresh_db()

    check("a machine-reported variable is allowed",
          save(db, body="Write-Host {{sys.machine}}")[0] is None)
    check("a metric is allowed",
          save(db, body="Write-Host {{metric.cpu_temp}}")[0] is None)

    # THE test this module exists for. `field.*` is writable with manage_rules, which is
    # deliberately NOT enough to issue commands -- so interpolating one would let that account
    # choose text that runs as SYSTEM.
    err, _ = save(db, body="Write-Host {{field.owner}}")
    check("a custom field is REFUSED", err is not None and "field.owner" in err)
    check("...and the refusal explains why, naming the permission",
          err is not None and "Issue commands" in err)
    err, _ = save(db, body="Write-Host {{var.anything}}")
    check("a derived variable is refused (it may wrap a field)", err is not None)

    err, _ = save(db, body="Write-Host {{sys.nonsense}}")
    check("an unknown variable in an allowed family is refused", err is not None)

    # Without the callback the name check cannot run; the family check still must.
    err, _ = scripts.save_script(db, "nocb", "", "", "powershell",
                                 "Write-Host {{field.owner}}", [], 600, actor="op@x.com")
    check("the family check holds even with no known_variable callback", err is not None)


# ------------------------------------------------------------------------ store
def test_store_and_visibility():
    print("\n-- listing, reading and deleting --")
    db = fresh_db()
    save(db, name="one", label="First", body="Write-Host {{sys.machine}}")
    save(db, name="two", label="Second", body="Get-Date")

    listed = scripts.list_scripts(db)
    check("both are listed, by name", [s["name"] for s in listed] == ["one", "two"])
    # The metadata view is what a `view`-only operator gets: enough to reference a script from
    # a rule, not the SYSTEM-privileged code itself.
    check("the list carries NO body", all("body" not in s for s in listed))
    check("...but says how big it is", listed[0]["body_chars"] > 0)
    check("...and which variables it reads", listed[0]["variables"] == ["sys.machine"])
    check("include_body returns it for a caller that may see it",
          "body" in scripts.list_scripts(db, include_body=True)[0])

    check("get_script returns the body", scripts.get_script(db, "one")["body"].startswith("Write-Host"))
    check("an unknown name reads as None", scripts.get_script(db, "nope") is None)

    # Re-saving keeps the original authorship: an edit is not a new script.
    first = scripts.get_script(db, "one")
    save(db, name="one", label="Renamed", body="Get-Date")
    again = scripts.get_script(db, "one")
    check("re-saving replaces in place", len(scripts.list_scripts(db)) == 2)
    check("...keeping created_at", again["created_at"] == first["created_at"])
    check("...and updating the label", again["label"] == "Renamed")

    check("deleting a script nobody uses works", scripts.delete_script(db, "two") == (None, True))
    check("deleting it again reports nothing deleted",
          scripts.delete_script(db, "two") == (None, False))

    # The invariant lives in the store, not only in its caller.
    err, deleted = scripts.delete_script(db, "one", in_use=[{"id": 3, "name": "Nightly reboot"}])
    check("a script in use cannot be deleted", err is not None and deleted is False)
    check("...and the refusal names the rule", "Nightly reboot" in err)
    check("...and it is still there", scripts.get_script(db, "one") is not None)


# ------------------------------------------------------------------------ references
def test_rule_references():
    print("\n-- a rule's reference to a script --")
    db = fresh_db()
    save(db, name="restart_svc", body='Restart-Service "{{input.service_name}}"',
         inputs=[{"name": "service_name", "required": True},
                 {"name": "wait", "required": False, "default": "5"}])
    save(db, name="off", body="Get-Date", enabled=False)
    spec = scripts.specs(db)

    err, clean = scripts.validate_reference(spec, "restart_svc", {"service_name": "Spooler"})
    check("a complete reference validates", err is None)
    check("...and an unsupplied optional input falls back to its default",
          err is None and clean == {"service_name": "Spooler", "wait": "5"})

    check("a missing required input is refused",
          scripts.validate_reference(spec, "restart_svc", {})[0] is not None)
    check("an input the script does not declare is refused",
          scripts.validate_reference(spec, "restart_svc",
                                     {"service_name": "x", "nope": "y"})[0] is not None)
    check("a reference to no script at all is refused",
          scripts.validate_reference(spec, "", {})[0] is not None)
    check("a reference to a missing script is refused",
          scripts.validate_reference(spec, "ghost", {})[0] is not None)
    # A disabled script is refused at SAVE time as well as skipped at fire time: a rule
    # pointing at something switched off is a mistake worth catching while it is being made.
    check("a reference to a disabled script is refused",
          scripts.validate_reference(spec, "off", {})[0] is not None)


if __name__ == "__main__":
    test_names_and_limits()
    test_declared_inputs()
    test_template_namespace_is_restricted()
    test_store_and_visibility()
    test_rule_references()
    print(f"\n==== scripts: {PASS} passed, {FAIL} failed ====")
    sys.exit(1 if FAIL else 0)
