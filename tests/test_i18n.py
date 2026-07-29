"""Tests i18n.py: catalog integrity, lookup/fallback, plurals, and negotiation.

THE POINT OF THIS FILE is the three catalog-integrity tests -- key parity, placeholder
parity, and the key scan over templates/JS. Everything the i18n engine can get wrong at
runtime is silent by design: a missing key renders the English string, a typo'd key
renders the key name, a dropped {placeholder} renders a sentence with a hole in it. None
of those raise, none appear in a log, and all three reach an operator's screen looking
merely odd. This is where they are supposed to be caught instead.

Run from the repo root so `import i18n` resolves.
"""
import json
import os
import re
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "hub"))

import i18n

HUB = os.path.join(ROOT, "hub")

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


def _raw(lang):
    """One catalog on its own -- NOT merged with English, which is what makes a missing
    translation visible here while being invisible at runtime."""
    path = os.path.join(i18n.LOCALES_DIR, f"{lang}.json")
    with open(path, "r", encoding="utf-8") as handle:
        return i18n._flatten(json.load(handle))


_PLACEHOLDER = re.compile(r"\{(\w+)\}")


# ============================== catalog integrity ==============================

def test_every_language_has_a_catalog():
    for code in i18n.LANGUAGE_CODES:
        path = os.path.join(i18n.LOCALES_DIR, f"{code}.json")
        check(f"{code}.json exists", os.path.isfile(path))
    check("English is the fallback language", i18n.DEFAULT_LANGUAGE == "en")


def test_key_parity_with_english():
    """Every catalog carries exactly English's key set.

    Missing keys are the interesting half -- they are what "translate it later" turns
    into, and at runtime they fall back to English silently, so without this test a
    catalog can rot indefinitely while looking fine. EXTRA keys matter too: a key no
    longer in en.json is dead weight shipped to every browser, and usually means a string
    was renamed and one catalog was not.
    """
    english = set(_raw("en"))
    for code in i18n.LANGUAGE_CODES:
        if code == "en":
            continue
        keys = set(_raw(code))
        missing = sorted(english - keys)
        extra = sorted(keys - english)
        check(f"{code}: no untranslated keys ({len(missing)} missing: "
              f"{missing[:5]}{'...' if len(missing) > 5 else ''})", not missing)
        check(f"{code}: no stale keys ({extra[:5]})", not extra)


def test_placeholder_parity():
    """A translation's {placeholders} must match the English entry's exactly.

    Dropping one is the nastiest bug this system can produce: "{machine} is running hot"
    translated without {machine} yields a perfectly grammatical alert that never says
    WHICH machine. Adding one is just as bad -- _SafeFormat renders the unmatched name
    literally, so the operator sees a raw {maquina} in the middle of a sentence.
    """
    english = _raw("en")
    for code in i18n.LANGUAGE_CODES:
        if code == "en":
            continue
        catalog = _raw(code)
        bad = []
        for key, text in catalog.items():
            if key not in english:
                continue
            if set(_PLACEHOLDER.findall(text)) != set(_PLACEHOLDER.findall(english[key])):
                bad.append(key)
        check(f"{code}: placeholders match English ({bad[:5]})", not bad)


def test_catalogs_are_plain_text():
    """No markup in a catalog. Jinja autoescapes t() and the JS side assigns through
    textContent, so a <b> in a translation renders as visible angle brackets rather than
    as bold -- and a translator who tried would get no feedback that it did not work."""
    for code in i18n.LANGUAGE_CODES:
        offenders = [k for k, v in _raw(code).items() if "<" in v or ">" in v]
        check(f"{code}: no markup in values ({offenders[:5]})", not offenders)


def test_keys_used_in_the_app_exist():
    """Scan templates and JS for literal t('...') keys and assert each is in en.json.

    This is the typo net. A mistyped key does not raise anywhere -- it renders its own
    name onto the page -- so nothing but a scan like this notices before an operator
    does. Only LITERAL keys are checked; a computed key (t('a.' + kind)) is invisible
    here and is the reason to prefer literals in new code.
    """
    english = set(_raw("en"))
    patterns = (
        # Jinja: {{ t('nav.alerts') }} / t('x', foo=1)
        re.compile(r"\bt\(\s*'([a-z0-9_.]+)'"),
        re.compile(r"\bt\(\s*\"([a-z0-9_.]+)\""),
    )
    scanned = 0
    unknown = set()
    roots = (os.path.join(HUB, "templates"), os.path.join(HUB, "static", "js"))
    for root in roots:
        for dirpath, _dirs, files in os.walk(root):
            for name in files:
                if not name.endswith((".html", ".js")):
                    continue
                # i18n.js defines t() rather than calling it, and its own examples in
                # comments are not app strings.
                if name == "i18n.js":
                    continue
                with open(os.path.join(dirpath, name), "r", encoding="utf-8") as handle:
                    text = handle.read()
                for pattern in patterns:
                    for key in pattern.findall(text):
                        scanned += 1
                        # A plural call names `<key>.one`/`.other` only at runtime.
                        if key in english or f"{key}.one" in english:
                            continue
                        unknown.add(f"{name}:{key}")
    check(f"scanned {scanned} literal t() keys", scanned > 0)
    check(f"every literal t() key exists in en.json ({sorted(unknown)[:5]})", not unknown)


# ============================== lookup ==============================

def test_lookup_and_interpolation():
    i18n.invalidate()
    check("english lookup", i18n.translate("nav.alerts", "en") == "Alerts")
    check("spanish lookup", i18n.translate("nav.alerts", "es") == "Alertas")
    check("german lookup", i18n.translate("nav.alerts", "de") == "Warnungen")
    check("interpolates named params",
          i18n.translate("topbar.hub_version", "en", version="1.51.0") == "Hub v1.51.0")


def test_missing_key_returns_the_key():
    check("unknown key is returned verbatim",
          i18n.translate("nope.not.here", "en") == "nope.not.here")


def test_missing_translation_falls_back_to_english():
    """The property that makes partial catalogs safe. Simulated by asking for a key that
    exists in en.json against a language whose file cannot contain it -- so this stays
    true no matter how complete the shipped catalogs happen to be today."""
    i18n.invalidate()
    english = i18n.catalog_for("en")
    spanish = i18n.catalog_for("es")
    check("merged catalog has every english key",
          set(english).issubset(set(spanish)))
    check("unsupported language falls back wholesale",
          i18n.translate("nav.alerts", "kl") == "Alerts")


def test_bad_placeholders_never_raise():
    """_SafeFormat's whole reason to exist: a translator's typo must not 500 a page."""
    i18n.invalidate()
    out = i18n.translate("common.high_temp_body", "en", machine="PC-1")
    check("missing param renders as itself, does not raise", "{temp}" in out)
    check("supplied param still interpolated", "PC-1" in out)
    check("extra unused params are ignored",
          i18n.translate("nav.alerts", "en", nonsense=1) == "Alerts")


def test_plural():
    # Uses a catalog injected directly, so the rule is tested independently of whether
    # the app currently happens to have a pluralised string.
    i18n.invalidate()
    i18n._cache["en"] = {"x.one": "{count} machine", "x.other": "{count} machines"}
    check("one", i18n.plural("x", 1, "en") == "1 machine")
    check("other (0)", i18n.plural("x", 0, "en") == "0 machines")
    check("other (5)", i18n.plural("x", 5, "en") == "5 machines")
    i18n.invalidate()


def test_catalog_json_is_script_safe():
    blob = i18n.catalog_json("en")
    check("no raw < in the embedded catalog", "<" not in blob)
    check("still parses as JSON", json.loads(blob)["nav.alerts"] == "Alerts")
    check("non-ascii is preserved, not escaped away",
          "Warnungen" in i18n.catalog_json("de"))


# ============================== negotiation ==============================

def test_negotiate():
    check("plain match", i18n.negotiate("es") == "es")
    check("region subtag matches the primary language",
          i18n.negotiate("es-MX") == "es")
    check("q-values are ordered", i18n.negotiate("fr;q=0.9, de;q=1.0") == "de")
    check("header order breaks q ties", i18n.negotiate("de, es") == "de")
    check("q=0 is a refusal, not a weak preference",
          i18n.negotiate("de;q=0, es;q=0.5") == "es")
    check("unsupported languages -> None", i18n.negotiate("fr, ja") is None)
    check("empty header -> None", i18n.negotiate("") is None)
    check("garbage does not raise", i18n.negotiate("!!!;q=x") is None)


def test_resolve_precedence():
    check("user choice wins over everything",
          i18n.resolve("de", "es", "en") == "de")
    check("fleet default beats the browser",
          i18n.resolve(None, "es", "de-DE") == "es")
    check("browser is used when nothing else is set",
          i18n.resolve(None, None, "de-AT") == "de")
    check("falls back to english",
          i18n.resolve(None, None, "fr") == "en")
    check("an unsupported stored choice is ignored, not honoured",
          i18n.resolve("kl", "de", None) == "de")


def test_settings_choices_match_the_shipped_catalogs():
    """hub.default_language's enum must offer exactly the languages that have catalogs --
    an option with no locale file would be selectable and then silently render English."""
    import settings as settings_module
    setting = next(s for s in settings_module.REGISTRY
                   if s.key == "hub.default_language")
    check("setting choices == LANGUAGE_CODES",
          tuple(setting.choices) == i18n.LANGUAGE_CODES)
    check("setting default is the fallback language",
          setting.default == i18n.DEFAULT_LANGUAGE)


if __name__ == "__main__":
    test_every_language_has_a_catalog()
    test_key_parity_with_english()
    test_placeholder_parity()
    test_catalogs_are_plain_text()
    test_keys_used_in_the_app_exist()
    test_lookup_and_interpolation()
    test_missing_key_returns_the_key()
    test_missing_translation_falls_back_to_english()
    test_bad_placeholders_never_raise()
    test_plural()
    test_catalog_json_is_script_safe()
    test_negotiate()
    test_resolve_precedence()
    test_settings_choices_match_the_shipped_catalogs()
    print(f"\n==== {PASS} passed, {FAIL} failed ====")
    sys.exit(1 if FAIL else 0)
