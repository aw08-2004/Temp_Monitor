"""Translation catalogs and language negotiation (roadmap #7).

Flask-free, like every other model module here -- `app.py` and the templates are the only
things that know about requests. Everything below is a pure function of a language code
plus the JSON files in `locales/`.

WHY AN IN-HOUSE STRING TABLE RATHER THAN flask-babel / gettext
--------------------------------------------------------------
Three reasons, in order of weight:

1. **Most of this console's user-facing text is in JavaScript, not Jinja.** The templates
   are ~1.5k lines; `static/js/` is ~9.4k lines of vanilla DOM building. gettext solves
   only the server half, so the client half would need a second mechanism regardless --
   two catalogs, two extraction paths, and a guarantee they drift. One JSON catalog feeds
   both: Jinja calls `t()` (registered as a template global) and the browser gets the same
   merged dict embedded in the page.

2. **`.mo` files are a build step, and this hub has no build step.** Deployment is a
   whole-directory file mirror (see the self-update path) with no compile stage anywhere.
   A binary artifact that must be regenerated after every edit is precisely the kind of
   thing that goes silently stale in a files-only update -- the hub would serve last
   week's translations with nothing on screen to say so. JSON is the source and the
   runtime format, so there is no stale-artifact state to be in.

3. It matches the precedent already set here (hand-rolled SigV4 rather than 80 MB of
   botocore on a 0.3 MB sparse install). This module is ~200 lines and adds no dependency.

What we give up: gettext's tooling (poedit, translation memory) and its plural-rule
database. The plural machinery is replaced by `plural()` below, which implements the
one/other rule -- correct for all three shipped languages and explicitly not for
languages with more forms. See that function before adding one.

FALLBACK IS THE LOAD-BEARING PROPERTY
-------------------------------------
A key missing from `es.json` renders the ENGLISH string, never a raw `nav.dashboard` on
screen. That is what makes it safe to add a string to the app and translate it later:
partial catalogs degrade to mixed-language, which is ugly but usable, instead of to
visible key names, which is broken. `tests/test_i18n.py` is where a missing translation
is supposed to be noticed -- not by an operator.
"""

import json
import os
import re
import threading

# --------------------------------------------------------------- what we ship

# Code -> endonym (the language's own name for itself). Endonyms, not English names:
# a picker reading "German" is no use to the person who needs it to say "Deutsch".
LANGUAGES = (
    ("en", "English"),
    ("es", "Español"),
    ("de", "Deutsch"),
)

LANGUAGE_CODES = tuple(code for code, _ in LANGUAGES)

# "No opinion -- use the browser's." Deliberately NOT a member of LANGUAGE_CODES, so it
# has no catalog and can never be resolved to: `is_supported("auto")` is False, which is
# what makes `resolve()` fall past it.
#
# It exists because "unset" and "English" have to be distinguishable and a plain enum
# cannot express that. `hub.default_language` ships as "auto" rather than "en" for exactly
# that reason -- shipping "en" made the Accept-Language branch below unreachable on every
# hub where nobody had touched the setting, i.e. all of them, so a German operator on a
# German browser got an English console and the only fix was to set it per user.
AUTO = "auto"

# Every other language overlays this one, and every lookup falls back to it, so the
# English catalog is the schema: a key that is not in en.json does not exist.
DEFAULT_LANGUAGE = "en"

LOCALES_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "locales")

_cache = {}
_cache_lock = threading.Lock()


def is_supported(lang):
    return lang in LANGUAGE_CODES


def endonym(lang):
    for code, name in LANGUAGES:
        if code == lang:
            return name
    return lang


# --------------------------------------------------------------- catalog loading

def _read_locale_file(lang):
    """The raw JSON for one language, flattened to dotted keys. Missing file -> {}.

    Catalogs are authored as NESTED objects because that is what keeps a 500-key file
    reviewable, but every lookup is by dotted path, so flattening happens once here
    rather than on every `t()` call.
    """
    path = os.path.join(LOCALES_DIR, f"{lang}.json")
    try:
        with open(path, "r", encoding="utf-8") as handle:
            raw = json.load(handle)
    except FileNotFoundError:
        return {}
    return _flatten(raw)


def _flatten(node, prefix=""):
    flat = {}
    for key, value in node.items():
        # `_`-prefixed keys are authoring notes for whoever opens the file (JSON has no
        # comment syntax). Dropped here so they never reach a lookup, the key-parity test
        # or the catalog embedded in the page.
        if key.startswith("_"):
            continue
        path = f"{prefix}{key}"
        if isinstance(value, dict):
            flat.update(_flatten(value, path + "."))
        else:
            flat[path] = str(value)
    return flat


def catalog_for(lang):
    """The merged, flat catalog for `lang`: English overlaid with that language.

    Merged rather than chained at lookup time so the dict handed to the browser is
    complete -- the client-side `t()` then needs no fallback logic of its own, and cannot
    develop a different one from this module's.
    """
    lang = lang if is_supported(lang) else DEFAULT_LANGUAGE
    with _cache_lock:
        hit = _cache.get(lang)
        if hit is not None:
            return hit

    base = _read_locale_file(DEFAULT_LANGUAGE)
    merged = dict(base)
    if lang != DEFAULT_LANGUAGE:
        merged.update(_read_locale_file(lang))

    with _cache_lock:
        _cache[lang] = merged
    return merged


def invalidate():
    """Drop the catalog cache. Mirrors settings.invalidate(); used by the tests, and by
    anything that edits a locale file on a running hub."""
    with _cache_lock:
        _cache.clear()


# --------------------------------------------------------------- lookup

class _SafeFormat(dict):
    """A placeholder with no matching argument renders as itself (`{name}`) instead of
    raising KeyError.

    Deliberate: a translator who mistypes `{maquina}` for `{machine}` must not be able to
    500 a console page. The wrong-looking literal is visible, survivable, and caught by
    the placeholder test in tests/test_i18n.py -- a stack trace on the Alerts page is
    none of those.
    """

    def __missing__(self, key):
        return "{" + key + "}"


def translate(key, lang=DEFAULT_LANGUAGE, **params):
    """One string. Unknown key -> the key itself, which is deliberately ugly: it is a
    programming error (a typo in a template), it is caught by the tests, and rendering
    blank instead would hide it until someone noticed an empty button."""
    text = catalog_for(lang).get(key)
    if text is None:
        return key
    if not params:
        return text
    try:
        return text.format_map(_SafeFormat(params))
    except (IndexError, ValueError):
        # Malformed braces in a translation ("{" alone, or "{0}" with no positional
        # args). Same reasoning as _SafeFormat: show the untouched string, never raise.
        return text


def plural(key, count, lang=DEFAULT_LANGUAGE, **params):
    """Pick `<key>.one` or `<key>.other` by `count`, which is also passed in as {count}.

    THE RULE IS `count == 1`, and that is correct for exactly the three languages shipped
    (English, Spanish, German all have a two-form one/other split with the same
    boundary). It is NOT correct for e.g. Polish, Russian or Arabic. Adding one of those
    means adding a per-language rule function here and extending the catalogs to carry
    the extra forms -- it is not a translation-only change, which is the whole reason
    this note exists rather than a silent `== 1`.
    """
    form = "one" if count == 1 else "other"
    return translate(f"{key}.{form}", lang, count=count, **params)


# --------------------------------------------------------------- negotiation

_ACCEPT_LANGUAGE = re.compile(r"([A-Za-z]{1,8}(?:-[A-Za-z0-9]{1,8})*)\s*(?:;\s*q\s*=\s*([0-9.]+))?")


def negotiate(accept_language):
    """Best supported language for an `Accept-Language` header, or None.

    Matches on the PRIMARY SUBTAG, so `es-MX`, `es-419` and `de-AT` all resolve -- a
    hub that only honoured exact `es` would ignore the header for most of the world.
    Ordered by q-value, and a q of 0 is an explicit refusal, not a weak preference.
    """
    if not accept_language:
        return None
    candidates = []
    for index, match in enumerate(_ACCEPT_LANGUAGE.finditer(str(accept_language))):
        tag, raw_q = match.group(1), match.group(2)
        try:
            quality = float(raw_q) if raw_q is not None else 1.0
        except ValueError:
            quality = 1.0
        if quality <= 0:
            continue
        # `index` keeps the header's own order as the tiebreak between equal q-values,
        # which is what the spec means by "listed in order of preference".
        candidates.append((-quality, index, tag.lower()))
    for _, _, tag in sorted(candidates):
        primary = tag.split("-")[0]
        if is_supported(primary):
            return primary
    return None


def resolve(user_language=None, fleet_default=None, accept_language=None):
    """The precedence chain, in one place so every caller agrees on it:

        the user's explicit choice
          ->  the fleet default, IF an admin actually chose one
          ->  the browser (Accept-Language)
          ->  English

    A *chosen* fleet default still outranks the browser, and that part has not changed:
    an admin who sets the fleet to German did so because that is the language of the
    office, and a technician whose laptop happens to be an English Windows install should
    still get German until they say otherwise.

    What changed is the middle step. `hub.default_language` used to ship as "en", which is
    indistinguishable from an admin choosing English -- so the browser step below was
    dead code on every hub where nobody had opened Settings, and someone on a German
    browser got an English console with no way to fix it but picking a language by hand,
    on every device. The setting now ships as `AUTO`, which is not a supported language
    and therefore falls through to the header. "Unset" and "English" are different
    answers and the value has to be able to say which one it is.

    A user's own choice is still the top of the chain and is stored on their profile, so
    it follows them to the next browser and the next device.
    """
    for candidate in (user_language, fleet_default):
        # AUTO lands here as "not supported" and is skipped, which is the intent -- but
        # the caller may equally pass None, or a code from a language this hub no longer
        # ships. All three mean "no answer at this level".
        if candidate and is_supported(candidate):
            return candidate
    from_header = negotiate(accept_language)
    if from_header:
        return from_header
    return DEFAULT_LANGUAGE


def catalog_json(lang):
    """The catalog as a JSON string safe to embed in a <script> element.

    `<` is escaped because a literal `</script>` anywhere in a translation would end the
    element early and drop the rest of the page. Catalogs are operator-authored rather
    than user input, so this is defence in depth -- but it costs nothing and the failure
    it prevents is a blank console.
    """
    return json.dumps(catalog_for(lang), ensure_ascii=False).replace("<", "\\u003c")


# ------------------------------------------------- the language of the request in flight

# Installed by app.py at startup. A callable, not an import of app.py's own function:
# this module stays Flask-free (it never learns what a request is), and the bare test
# apps that mount one blueprint can leave it unset and get English.
_language_provider = None


def set_language_provider(provider):
    """Tell this module how to find the CURRENT request's language.

    Server-rendered pages get their language passed down explicitly (see
    `template_context`), which is the right shape there because a template renders once
    in one language. JSON endpoints cannot: a blueprint like `permissions_web` serves
    user-facing text (capability labels) with no template and no language argument
    anywhere in its call chain, and threading one through every blueprint factory would
    add a parameter to each of the ten of them for the two that need it.

    So app.py installs its request-scoped resolver here once, and `current()` below is
    what a web module asks. `provider` must be callable and return a language code.
    """
    global _language_provider
    _language_provider = provider


def current():
    """The language for the request in flight, or English if that cannot be determined.

    Never raises: a failure to resolve a language must degrade to English, not 500 an
    API call. Outside a request (a background thread, a test) there is genuinely no
    answer, and English is the honest one.
    """
    if _language_provider is None:
        return DEFAULT_LANGUAGE
    try:
        lang = _language_provider()
    except Exception:
        return DEFAULT_LANGUAGE
    return lang if is_supported(lang) else DEFAULT_LANGUAGE


def template_context(lang, chosen_language=None):
    """Everything a template needs to render in `lang`, as a dict to merge into a context.

    `chosen_language` is the language the signed-in user PICKED, which is not the same as
    `lang` -- someone who has picked nothing renders in whatever the browser asked for,
    and the topbar picker has to show "Automatic" rather than pre-selecting the language
    that happened to win. Without the distinction the picker claims a choice the user
    never made, and there is then no option left that means "go back to following my
    browser".

    Lives here rather than being spelled out in app.py's context processor because every
    Flask app that renders base.html needs it -- app.py, and each of the bare test apps
    that mount one blueprint against the real templates. When this was inlined in the
    context processor, base.html could only render inside the full app, so a blueprint test
    that touched a page failed with `'t' is undefined` at import-of-template time and had
    to grow its own copy of these lambdas. Three copies of the binding between `t` and a
    language is three chances for a template to render half-translated.

    `t` is a closure over `lang` on purpose: a template calls t('nav.alerts') with no
    language argument, so the language cannot be passed inconsistently from one call site
    to the next within a page.
    """
    return {
        "t": lambda key, **params: translate(key, lang, **params),
        "t_plural": lambda key, count, **params: plural(key, count, lang, **params),
        "current_language": lang,
        "chosen_language": chosen_language if chosen_language in LANGUAGE_CODES else AUTO,
        "auto_language": AUTO,
        "languages": LANGUAGES,
        "i18n_catalog_json": catalog_json(lang),
    }
