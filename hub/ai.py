"""Natural language over the rules engine -- roadmap #24.

**The one job of this module is to refuse a rule a language model got wrong.** An operator
types "alert me when any drive drops below 10 GB free"; a model answers with an expression;
and the only reason that answer is safe to show anybody is that it goes through the SAME
validators a hand-typed rule goes through -- `rules.parse_expression`, `rules.validate_target`,
`rules.validate_actions` -- before it is ever rendered as a draft. A draft that fails them is
a refusal carrying the validator's own words, never a rule.

The silent failure this file exists to catch: **a rule that looks valid because a model said
so.** `cpu.temp_c` and `proc.cpu_percent` read like variables this hub has, the source plan for
this feature used both, and neither exists. A rule shape accepted on trust would have stored
them, and the rule would have sat in the console looking correct and evaluating to UNKNOWN on
every machine forever -- which rules.py's own docstring calls the fastest way to make an
alerting feature untrustworthy. So nothing here trusts the model's output for anything except
its text.

Three design choices worth the words:

1. **The model emits the engine's own one-line expression, not a JSON condition tree.** That
   language already has a tokenizer, a parser bounded on depth and node count, and a validator
   that knows which operators each variable kind accepts. A second authoring format would need
   a second validator, and two validators over one engine eventually disagree. Rejected on
   those grounds, and recorded in ROADMAP.MD #24.

2. **The catalog is assembled at request time from `rules.catalog()`**, never typed into a
   prompt. A hand-written variable list is a list that rots the first time an operator adds a
   custom field, a probe or a derived variable -- and the rot is invisible, because the model
   happily invents something plausible from a stale list.

3. **The human-readable summary is deterministic.** It is `rules.format_expression` plus the
   resolved target count, not a second completion. If the prose came back from the model too,
   the operator would be confirming a sentence rather than the rule that will run -- and the
   two can differ with nothing in the console revealing it.

Three more entry points sit on the same provider path and answer rather than author --
`answer_machine_question`, `fleet_query` and `daily_summary`. Each of them draws the same line
in a different place, and the line is always between what the hub computed and what the model
said: the machine panel sends a snapshot built here and hands it BACK with the answer, so the
prose and the readings it claims to be about are on one screen; the fleet query lets the model
write an expression and lets the EVALUATOR answer it, because generated SQL cannot be passed
through `access.filter_machines()`; and the summary computes every figure from two local
tables and asks only for the sentence wrapped around them. **Nothing in this module lets a
model's assertion reach an operator as a fact of this hub's.**

Provider-wise there is ONE wire shape here, OpenAI-compatible `/v1/chat/completions`, because
that is also what Ollama, vLLM, LM Studio and OpenRouter speak: a LAN-only deployment and a
hosted one then differ by a base url rather than by a code path. #17's wording layer is meant
to call `complete()` rather than grow a second provider -- one configuration, one audit trail,
one off switch. Off by default, and every call is recorded in `ai_requests` whether it
succeeded or not.

Kept free of Flask AND of settings, the same house rule rules.py follows: the caller passes
the configuration dict in (app.py's `_ai_config()`, read fresh per request so the off switch
takes effect on the next call rather than at the next restart), and passes `resolve_vars` in
the way rules_web.py already does for its preview endpoint. That is what lets this module be
exercised against a temp database with no Flask, no environment and no provider.
"""
import json
import re
import socket
import sqlite3
import time
import uuid
from collections import namedtuple
from ipaddress import ip_address
from urllib.parse import urlparse

import requests

import alerts
import rules
import scripts

# ---------------------------------------------------------------------------------------
# Providers
# ---------------------------------------------------------------------------------------

# One wire shape, deliberately. "openai_chat" names the REQUEST FORMAT, not a vendor -- it is
# what Ollama, vLLM, LM Studio, OpenRouter and OpenAI itself all accept. #17 calls for "no
# vendor privileged in the schema"; a second wire shape here (say an Anthropic Messages
# adapter) is additive and changes nothing else in this file, which is the point.
WIRE_OPENAI_CHAT = "openai_chat"
WIRE_SHAPES = (WIRE_OPENAI_CHAT,)

# Kept as the old name because settings.py and the tests import it, and because the wire
# shape is still what a provider IS from this module's point of view.
PROVIDER_OPENAI_CHAT = WIRE_OPENAI_CHAT

# The presets an operator picks from, and the reason the setting stopped being a free-text
# url. "Point FleetHub at OpenRouter" is the question somebody actually has; "which base url
# does OpenRouter's OpenAI-compatible endpoint live at" is trivia they should not have to
# look up, and a typo in it fails as a DNS error rather than as a wrong answer.
#
# **The preset names a URL; it does not name a vendor's privileges.** Every entry here speaks
# the same wire shape, none gets special handling anywhere in this file, and `custom` exists
# so the list is a convenience rather than a gate -- anything OpenAI-compatible still works by
# typing its address, which is what keeps a self-hosted endpoint a first-class option.
Preset = namedtuple("Preset", "name base_url wire")

PRESET_CUSTOM = "custom"
PROVIDER_PRESETS = (
    # First, and the default, so the list never implies this hub prefers a vendor.
    Preset(PRESET_CUSTOM, "", WIRE_OPENAI_CHAT),
    # The three that run on the operator's own hardware. Their ports are the projects' own
    # defaults; an operator who moved one picks `custom`.
    Preset("ollama", "http://127.0.0.1:11434", WIRE_OPENAI_CHAT),
    Preset("lm_studio", "http://127.0.0.1:1234", WIRE_OPENAI_CHAT),
    Preset("vllm", "http://127.0.0.1:8000", WIRE_OPENAI_CHAT),
    # The hosted two. Both need AI_API_KEY in .env; neither can work without it, which is
    # what the status endpoint says rather than letting the first draft fail as a 401.
    Preset("openai", "https://api.openai.com", WIRE_OPENAI_CHAT),
    Preset("openrouter", "https://openrouter.ai/api", WIRE_OPENAI_CHAT),
)
PRESETS_BY_NAME = {preset.name: preset for preset in PROVIDER_PRESETS}
PROVIDERS = tuple(preset.name for preset in PROVIDER_PRESETS)


def preset_for(name):
    """The preset a stored `ai.provider` names, falling back to `custom`.

    Tolerant on purpose. The setting briefly held a WIRE SHAPE ("openai_chat") rather than a
    preset name, and an unrecognised value must leave the hub configurable -- falling back to
    custom means the base url an operator already typed still governs, which is the reading
    that loses nobody's configuration.
    """
    return PRESETS_BY_NAME.get(str(name or ""), PRESETS_BY_NAME[PRESET_CUSTOM])


def resolved_base_url(config):
    """The address to call, given the chosen preset and the typed url.

    The preset wins where it has one, so picking OpenRouter needs no second field filled in
    and cannot drift from a stale url left behind by a previous choice. `custom` has no url of
    its own, so it reads `ai.base_url` -- which is the only state in which that field means
    anything.
    """
    preset = preset_for((config or {}).get("provider"))
    if preset.base_url:
        return preset.base_url
    return str((config or {}).get("base_url") or "").strip().rstrip("/")

# How many times a draft may be handed back to the model with the validator's complaint
# attached. ONE. An unbounded repair loop is an unbounded bill on a hosted provider and an
# unbounded wait on a local one, and in practice a model that cannot fix an "unknown variable"
# on the first correction is not going to.
MAX_REPAIR_ATTEMPTS = 1

# Bounds on what may cross this module. The request cap is not about cost -- it is about the
# prompt: the catalog alone is a few thousand characters, and a request arriving longer than
# this is not a description of one rule.
MAX_REQUEST_CHARS = 2000
MAX_RESPONSE_CHARS = 20000
# The bound on what is READ, as opposed to what is kept. Generous next to a rule envelope of a
# few hundred bytes, small next to what an unbounded read costs: this is the number standing
# between a misbehaving endpoint on the LAN and the hub's memory.
MAX_RESPONSE_BYTES = 256 * 1024
# The same bound for a provider's MODEL LIST, which is a different kind of answer and needs a
# different number. A chat reply is a few hundred bytes of rule; a catalogue carries a
# description, pricing and architecture block for every model it serves. OpenRouter's was
# 718 KB for 445 models when this was measured (2026-09-14), so reusing the reply cap refused
# every refresh with "too large to read" -- the cap was doing its job against the wrong
# document. Eight megabytes is ten times that, room for the catalogue to keep growing, and
# still a bound on what a misbehaving endpoint on the LAN can make the hub hold.
MAX_MODELS_BYTES = 8 * 1024 * 1024
MAX_DRAFTS_PER_ACTOR = 25
MAX_REFUSAL_CHARS = 400
# How much PROSE may come back from the three answering entry points -- the machine panel, the
# fleet query's refusal and the summary's covering note. Small next to MAX_RESPONSE_CHARS
# because this text is rendered into a console rather than parsed: an answer that runs past a
# screen is one nobody reads to the end, and the readings it was built from are on the same
# page anyway.
MAX_ANSWER_CHARS = 2000
# How many matching machines a fleet query hands back. A question answered with four hundred
# hostnames is a list nobody reads; the TALLY is the answer at that size, and it is counted
# over every machine rather than over this slice.
MAX_QUERY_RESULTS = 200
# How many rules one sentence may draft. An escalation is a SET of rules -- the engine has no
# schedule, so "warn after five days, restart after ten" is two conditions over the same
# variable and cannot be one rule -- and a drafter that could only answer with one silently
# dropped a stage, which is the failure this number exists alongside. Five, because a request
# needing six stages is a policy document rather than a rule, and an unbounded list is an
# unbounded number of validator runs and of rows somebody has to read before committing.
MAX_RULES_PER_DRAFT = 5
# The longest report window a summary will compute. Thirty days because `data.retention_days`
# prunes history at thirty by default, so a longer window would count what SURVIVED rather
# than what happened -- a figure that shrinks as the pruner runs is worse than no figure.
MAX_SUMMARY_WINDOW_DAYS = 30
# How many rules and machines the summary NAMES before it stops naming them. The counts stay
# whole; only the list is cut, and it says so.
SUMMARY_TOP_N = 8

KIND_DRAFT_RULE = "draft_rule"
KIND_REFINE_RULE = "refine_rule"
KIND_MACHINE_ASK = "machine_ask"
KIND_FLEET_QUERY = "fleet_query"
KIND_SUMMARY = "summary"
REQUEST_KINDS = (KIND_DRAFT_RULE, KIND_REFINE_RULE, KIND_MACHINE_ASK, KIND_FLEET_QUERY,
                 KIND_SUMMARY)

OUTCOME_OK = "ok"
OUTCOME_REFUSED = "refused"          # the model answered and the validators rejected it
OUTCOME_PROVIDER_ERROR = "error"     # the provider never answered usefully
OUTCOME_DISABLED = "disabled"
OUTCOMES = (OUTCOME_OK, OUTCOME_REFUSED, OUTCOME_PROVIDER_ERROR, OUTCOME_DISABLED)


# ---------------------------------------------------------------------------------------
# DB SETUP
# ---------------------------------------------------------------------------------------
def get_conn(db_path):
    conn = sqlite3.connect(db_path, timeout=30)
    conn.row_factory = sqlite3.Row
    return conn


def init_ai_db(db_path):
    """Create the two AI tables. Idempotent -- safe to call on every hub start next to
    app.init_db().

    Two tables and not one, because they answer different questions and have different
    lifetimes. `ai_requests` is the audit trail: who asked what, of which provider, and what
    came of it. It is the row somebody wants when the bill arrives or when a colleague asks
    whether machine names left the building, so it outlives the draft. `ai_drafts` is
    work-in-progress and is pruned.

    **The original English is stored on the draft, not just the generated rule.** A rule whose
    provenance is "a model wrote this" is unauditable; one that keeps the sentence it came from
    can be read six months later by somebody deciding whether it still means what was wanted.
    """
    with get_conn(db_path) as conn:
        conn.execute("PRAGMA journal_mode=WAL;")
        conn.execute("""CREATE TABLE IF NOT EXISTS ai_requests (
                            id           INTEGER PRIMARY KEY AUTOINCREMENT,
                            created_at   REAL NOT NULL,
                            actor        TEXT NOT NULL DEFAULT '',
                            kind         TEXT NOT NULL,
                            provider     TEXT NOT NULL DEFAULT '',
                            model        TEXT NOT NULL DEFAULT '',
                            machine      TEXT NOT NULL DEFAULT '',
                            prompt_chars INTEGER NOT NULL DEFAULT 0,
                            outcome      TEXT NOT NULL,
                            error        TEXT NOT NULL DEFAULT ''
                        )""")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_ai_requests_created "
                     "ON ai_requests(created_at)")
        conn.execute("""CREATE TABLE IF NOT EXISTS ai_drafts (
                            id           TEXT PRIMARY KEY,
                            created_at   REAL NOT NULL,
                            updated_at   REAL NOT NULL,
                            actor        TEXT NOT NULL DEFAULT '',
                            source_text  TEXT NOT NULL DEFAULT '',
                            payload_json TEXT NOT NULL,
                            provider     TEXT NOT NULL DEFAULT '',
                            model        TEXT NOT NULL DEFAULT ''
                        )""")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_ai_drafts_actor ON ai_drafts(actor)")
        # The model list a provider last reported, keyed by preset. Cached rather than
        # fetched on demand for the reason sharing.py caches a peer's catalogue: the Settings
        # page must render whether or not a third party is answering right now, and a network
        # call on every page load is a page that hangs when the provider does. Keyed by
        # provider so switching from Ollama to OpenRouter and back does not show one's models
        # under the other's name.
        conn.execute("""CREATE TABLE IF NOT EXISTS ai_models (
                            provider   TEXT NOT NULL,
                            model_id   TEXT NOT NULL,
                            cached_at  REAL NOT NULL,
                            PRIMARY KEY (provider, model_id)
                        )""")


# ---------------------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------------------
# The shape app.py's `_ai_config()` returns, and the shape every test builds by hand. Listed
# here rather than only in app.py so that one function and this module cannot drift apart
# silently -- a missing key becomes a KeyError in _ai_config's own test, not a feature that is
# quietly off in production.
CONFIG_KEYS = ("enabled", "provider", "base_url", "model", "max_tokens", "timeout_seconds",
               "allow_private_endpoint", "send_machine_names", "draft_retention_days")


def is_enabled(config):
    """Whether the feature is switched on at all. Checked before anything else in every entry
    point, so a hub with no provider configured behaves like a hub without the feature rather
    than like one that is broken."""
    return bool((config or {}).get("enabled"))


def provider_config(config):
    """Settings -> (error, resolved). The one place a half-configured provider is caught.

    `config` is injected rather than read from settings here, the rule rules.py follows: this
    module stays unit-testable against a dict, and the caller re-reads the settings per request
    so an admin switching the feature off is obeyed on the next call.

    **The API key is deliberately not in what this returns.** It used to be, and the shape was
    wrong in a way that took a scanner to notice: the resolved dict travels a long way -- into
    `/api/ai/status`'s response builder, into `answer_machine_question`'s prompt decisions --
    while the error string travels back to an operator's screen, and both come out of the same
    `return`. Anything reading this function has to prove by hand that the second half never
    reaches the first. It no longer can: `complete()` and `refresh_models()` take the key as
    their own parameter, which is where the one header that needs it is built. See #24.

    This function never asks about a key at all, which is also why **an empty key is not an
    error** anywhere: a local Ollama needs none, and refusing to run without one would make the
    LAN-only deployment -- the one with no data-egress question attached at all -- the hardest
    of the three to configure.
    """
    config = config or {}
    if not is_enabled(config):
        return "the AI features are switched off (Settings -> AI)", None
    preset = preset_for(config.get("provider"))
    base_url = resolved_base_url(config)
    model = str(config.get("model") or "").strip()
    if not base_url:
        return "no AI provider is configured (pick one in Settings -> AI)", None
    if not model:
        return "no AI model is configured (pick one in Settings -> AI)", None
    error = check_provider_url(base_url, bool(config.get("allow_private_endpoint")))
    if error:
        return error, None
    return None, {
        "provider": preset.name,
        "wire": preset.wire,
        "base_url": base_url,
        "model": model,
        "max_tokens": int(config.get("max_tokens") or 1024),
        "timeout": int(config.get("timeout_seconds") or 60),
        "send_machine_names": bool(config.get("send_machine_names")),
    }


def _unwrap_v4(address):
    """An IPv6 address that carries an IPv4 one inside it -> that IPv4 address.

    **Without this, the hard-coded metadata block below is bypassable.** CPython puts the
    whole of `::ffff:0:0/96` in its private-networks table but does NOT report those addresses
    as link-local, reserved or multicast -- so `::ffff:169.254.169.254` is the cloud metadata
    endpoint wearing a costume: the unconditional refusal never fires, and it lands in the
    merely-"private" bucket that `ai.allow_private_endpoint` waves through by default.

    `sixtofour` (2002::/16) and `teredo` (2001::/32) embed an IPv4 address the same way and
    are unwrapped for the same reason. Cheap, and the alternative is a second list of special
    ranges to keep in step with the stdlib's.
    """
    for attribute in ("ipv4_mapped", "sixtofour"):
        inner = getattr(address, attribute, None)
        if inner is not None:
            return inner
    teredo = getattr(address, "teredo", None)
    if teredo:
        # (server, client). The CLIENT is where a packet would actually go.
        return teredo[1]
    return address


def endpoint_config(config):
    """Everything provider_config resolves EXCEPT the model. Returns (error, resolved).

    Its own function because `provider_config` refuses when no model is set, and the one
    request that most needs to work in that state is "list the models" -- an operator asks for
    the list precisely because they have not picked one yet. Sharing the refusal would have
    made the picker unusable until somebody had typed a model id by hand, which is the problem
    the picker exists to remove.
    """
    config = config or {}
    if not is_enabled(config):
        return "the AI features are switched off (Settings -> AI)", None
    preset = preset_for(config.get("provider"))
    base_url = resolved_base_url(config)
    if not base_url:
        return "no AI provider is configured (pick one in Settings -> AI)", None
    error = check_provider_url(base_url, bool(config.get("allow_private_endpoint")))
    if error:
        return error, None
    return None, {
        "provider": preset.name,
        "wire": preset.wire,
        "base_url": base_url,
        "timeout": int(config.get("timeout_seconds") or 60),
    }


def check_provider_url(url, allow_private):
    """Refuse a provider endpoint this hub should not be calling. Returns an error or None.

    **Not notify.check_webhook_url, and the difference is the point.** That function mandates
    https, which is right for a webhook and wrong here: the recommended deployment for this
    feature is Ollama on `http://127.0.0.1:11434`, where TLS buys nothing because nothing
    leaves the box. Its private-address verdict is inverted here too -- a webhook aimed at
    169.254.169.254 is almost always an SSRF attempt, while a provider aimed at loopback is
    the configuration with no data-egress question attached at all.

    So the rule is about the PAIR rather than about either half: plaintext is allowed only to
    an address that never leaves the machine or the LAN, and anything reached over the
    internet must be https. A plaintext endpoint on a routable address would put every prompt
    -- hostnames, logged-in users, serial numbers -- on the wire in clear, which is the one
    outcome this check exists to prevent.

    The DNS-rebinding caveat notify.py is honest about applies here verbatim: we resolve, then
    hand the url to requests, which resolves again. This is defence in depth around an input
    only a MANAGE_SETTINGS holder can set, not a perimeter.
    """
    parsed = urlparse(str(url or "").strip())
    if parsed.scheme not in ("http", "https"):
        return "the AI base URL must start with http:// or https://"
    if not parsed.hostname:
        return "the AI base URL has no host"

    try:
        infos = socket.getaddrinfo(parsed.hostname,
                                   parsed.port or (443 if parsed.scheme == "https" else 80),
                                   proto=socket.IPPROTO_TCP)
    except socket.gaierror as exc:
        # The resolver's own text does NOT go back to the caller. It is written by the OS,
        # it varies by platform, and this string is rendered in a browser -- the same split
        # rules_web.py spends a paragraph on. The hostname is the admin's own input, so
        # naming it is useful and safe; everything else goes to the hub log.
        print(f"[ai] Could not resolve the AI provider host {parsed.hostname}: {exc}")
        return (f"cannot resolve {parsed.hostname}; see the hub log for what the resolver "
                "said")

    # link_local covers 169.254.0.0/16, where the cloud metadata endpoints live -- the single
    # most valuable target an SSRF has, and the one address that is never a model server.
    local = []
    for info in infos:
        address = _unwrap_v4(ip_address(info[4][0]))
        if address.is_link_local or address.is_reserved or address.is_multicast:
            return (f"{parsed.hostname} resolves to {address}, which is not somewhere this "
                    "hub will send a prompt")
        local.append(bool(address.is_private or address.is_loopback))

    if any(local) and not allow_private:
        return (f"{parsed.hostname} resolves to a private address; switch on "
                "\"allow a provider on this network\" in Settings -> AI if that is intended")
    if parsed.scheme == "http" and not all(local):
        return (f"{parsed.hostname} is reachable over the internet, so its URL must be "
                "https -- a plaintext prompt carries hostnames and user names in clear")
    return None


# ---------------------------------------------------------------------------------------
# The completion call
# ---------------------------------------------------------------------------------------
def complete(config, messages, *, api_key="", max_tokens=None, timeout=None):
    """One completion. Returns (error, text), and NEVER raises.

    Never raising is not a defensive habit -- it is the contract every caller here is written
    against. A provider is a third party on a network: it times out, it rate-limits, it returns
    HTML from a proxy, it returns 200 with a body that has no choices in it. Each of those has
    to become an error string an operator can read, because the alternative is a stack trace in
    the hub log and a spinner that never stops in the console.

    The response is read INCREMENTALLY and abandoned once it passes MAX_RESPONSE_BYTES. That
    is the difference between a cap and a claim: `response.json()` buffers and parses the whole
    body first, so truncating its result afterwards bounds what we keep and not what we read.
    A provider answering with a gigabyte is either broken or hostile, and `base_url` is allowed
    to point at something on the LAN by default -- so "the admin configured it" is a reason to
    trust the intent, not the bytes.
    """
    error, resolved = provider_config(config)
    if error:
        return error, None

    headers = {"Content-Type": "application/json", "User-Agent": "FleetHub-AI/1.0"}
    # Straight from the parameter, not out of `resolved` -- see provider_config's docstring.
    # This function and refresh_models are the only two places the key is read at all.
    key = str(api_key or "")
    if key:
        headers["Authorization"] = f"Bearer {key}"
    body = {
        "model": resolved["model"],
        "messages": list(messages or []),
        "max_tokens": int(max_tokens or resolved["max_tokens"] or 1024),
        # Zero, because this is a translation task with one right answer shape. A rule builder
        # that returns a different expression for the same sentence twice is one nobody can
        # learn to trust.
        "temperature": 0,
    }
    try:
        # stream=True so the body arrives in chunks we can stop taking. Closed either way by
        # the context manager, which a streamed response needs and a buffered one does not.
        with requests.post(
                f"{resolved['base_url']}/v1/chat/completions",
                json=body, headers=headers,
                timeout=int(timeout or resolved["timeout"] or 60),
                stream=True,
                # No redirects, for the reason notify.py gives: a 302 walks straight past
                # every check check_provider_url just made.
                allow_redirects=False) as response:
            if response.status_code >= 400:
                return f"the AI provider returned HTTP {response.status_code}", None
            raw = bytearray()
            for chunk in response.iter_content(chunk_size=8192):
                raw.extend(chunk)
                if len(raw) > MAX_RESPONSE_BYTES:
                    # Abandoned mid-body rather than truncated and parsed: half a JSON
                    # document is not a smaller answer, it is a different failure wearing the
                    # same clothes.
                    return "the AI provider's answer was too large to read", None
    except requests.Timeout:
        return "the AI provider did not answer in time", None
    except requests.ConnectionError:
        return "the AI provider could not be reached", None
    except requests.RequestException:
        # OUR words for every one of these, not the library's and not the exception's type
        # name. A requests exception carries the full url, and that url carries a key in a
        # query string on some gateways; the type name leaks less but is still text this hub
        # did not write, rendered in a browser. What actually went wrong is in the hub log
        # via the traceback the caller prints -- this is the sentence an operator can act on.
        return "the AI provider answered in a way this hub could not use", None

    try:
        payload = json.loads(bytes(raw).decode("utf-8", "replace"))
        text = payload["choices"][0]["message"]["content"]
    except (ValueError, KeyError, IndexError, TypeError):
        return "the AI provider returned a response this hub could not read", None
    text = str(text or "")
    if not text.strip():
        return "the AI provider returned an empty response", None
    return None, text[:MAX_RESPONSE_CHARS]


# ---------------------------------------------------------------------------------------
# The model list
# ---------------------------------------------------------------------------------------

# How long a cached list is presented without a caveat. Three hours, because a provider's
# catalogue changes on the order of weeks and the cost of a stale entry is one failed draft
# with a readable error, not a wrong answer. Past it the list is still SHOWN and merely
# flagged -- the same choice sharing.py makes about a peer's catalogue, and for the same
# reason: an operator with a stale list can still work, and one with an empty list cannot.
MODELS_STALE_SECONDS = 3 * 3600
MAX_MODELS = 2000


def list_models(db_path, provider, now=None):
    """What this provider last reported. Returns {"models": [...], "cached_at", "stale"}.

    Never goes to the network. The Settings page reads this on every render, and a page that
    reaches a third party to draw itself is a page that hangs when that third party does.
    """
    with get_conn(db_path) as conn:
        rows = conn.execute(
            "SELECT model_id, cached_at FROM ai_models WHERE provider = ? "
            "ORDER BY model_id", (str(provider or ""),)).fetchall()
    if not rows:
        return {"models": [], "cached_at": None, "stale": True}
    cached_at = max(row["cached_at"] for row in rows)
    return {"models": [row["model_id"] for row in rows],
            "cached_at": cached_at,
            "stale": (float(now or time.time()) - cached_at) > MODELS_STALE_SECONDS}


def model_choices(db_path, provider):
    """The cached ids alone, for a picker. Separate from list_models so a caller that only
    wants the vocabulary does not have to know about staleness."""
    return list_models(db_path, provider)["models"]


def _store_models(db_path, provider, models, now=None):
    """Replace this provider's cached list wholesale.

    Wholesale rather than merged, for the reason sharing.replace_borrowed gives: the
    provider's catalogue is authoritative, and a model missing from it has been withdrawn.
    Merging would leave a retired model in the picker forever, and the failure would arrive
    much later as a 404 from a draft nobody could explain.
    """
    stamp = float(now or time.time())
    with get_conn(db_path) as conn:
        conn.execute("DELETE FROM ai_models WHERE provider = ?", (str(provider),))
        conn.executemany(
            "INSERT OR REPLACE INTO ai_models (provider, model_id, cached_at) VALUES (?,?,?)",
            [(str(provider), str(model), stamp) for model in models[:MAX_MODELS]])
    return len(models[:MAX_MODELS])


def refresh_models(db_path, config, *, api_key="", now=None):
    """Ask the provider what it serves, and replace the cache. Returns (error, listing).

    GET `{base}/v1/models`, the companion of the chat-completions endpoint every preset here
    speaks, so this needs no per-vendor branch. Same discipline as complete(): the endpoint is
    re-checked through check_provider_url first, redirects are refused, the body is read under
    a cap, and every failure becomes a sentence this hub wrote.

    **A failure leaves the previous list in place.** An operator who cannot reach the provider
    for a minute should not also lose the picker they were using -- and an empty list looks
    identical to "this provider serves nothing", which is a lie with no way to notice it.
    """
    error, resolved = endpoint_config(config)
    if error:
        return error, None

    headers = {"Accept": "application/json", "User-Agent": "FleetHub-AI/1.0"}
    key = str(api_key or "")
    if key:
        headers["Authorization"] = f"Bearer {key}"
    try:
        with requests.get(f"{resolved['base_url']}/v1/models", headers=headers,
                          timeout=resolved["timeout"], stream=True,
                          allow_redirects=False) as response:
            if response.status_code == 401 or response.status_code == 403:
                # Named separately because it is the one failure with an obvious fix, and
                # "HTTP 401" sends an operator to the wrong place.
                return ("the AI provider rejected the credentials -- check the API key in "
                        "Settings -> AI (AI_API_KEY)"), None
            if response.status_code >= 400:
                return f"the AI provider returned HTTP {response.status_code}", None
            raw = bytearray()
            for chunk in response.iter_content(chunk_size=8192):
                raw.extend(chunk)
                if len(raw) > MAX_MODELS_BYTES:
                    return "the AI provider's model list was too large to read", None
    except requests.Timeout:
        return "the AI provider did not answer in time", None
    except requests.ConnectionError:
        return "the AI provider could not be reached", None
    except requests.RequestException:
        return "the AI provider answered in a way this hub could not use", None

    try:
        payload = json.loads(bytes(raw).decode("utf-8", "replace"))
        entries = payload["data"]
    except (ValueError, KeyError, TypeError):
        return "the AI provider's model list was not in a shape this hub could read", None

    models = sorted({str(entry.get("id") or "").strip()
                     for entry in entries if isinstance(entry, dict)} - {""})
    if not models:
        return "the AI provider listed no models", None
    _store_models(db_path, resolved["provider"], models, now=now)
    return None, list_models(db_path, resolved["provider"], now=now)


# ---------------------------------------------------------------------------------------
# What the model is allowed to know about
# ---------------------------------------------------------------------------------------
def catalog_prompt(db_path, disks=None):
    """The variable namespace, as compact prompt text.

    Built from rules.catalog() so custom fields, probes and derived variables are all present
    -- the same reason rules.all_extra_variables() exists. One line per variable carrying its
    kind and unit, because the kind decides which operators are legal and a model given only
    names writes `sys.online > 5`.
    """
    lines = []
    for var in rules.catalog(db_path, disks):
        unit = f" [{var.unit}]" if getattr(var, "unit", "") else ""
        lines.append(f"{var.name} ({var.kind}){unit}")
    return "\n".join(lines)


def target_prompt(db_path):
    """The target kinds, and the custom fields a selector may name.

    **Spells out that there are no tags**, because `tag:server` is what a model trained on
    other RMM products reaches for first -- the source plan for this feature used it in seven
    of eight examples. A selector over a custom field is the real equivalent here, so the
    fields are listed by name rather than left to be guessed.
    """
    fields = [f["name"] for f in rules.list_fields(db_path)]
    listed = ", ".join(sorted(fields)) if fields else "(none defined)"
    return ("Target kinds: " + ", ".join(rules.TARGET_KINDS) + ".\n"
            "There are NO TAGS. A group of machines is an explicit list, an Active Directory\n"
            "OU, or a custom field selector. Custom fields defined on this hub: " + listed
            + ".")


def action_prompt(db_path):
    """The action types, the commands a rule may issue, and the scripts that exist.

    Only `rules.offered_commands()` is listed, which is ALL_COMMANDS minus everything a rule
    is forbidden to issue -- the scheduled commands, session and remote control, process
    kills, locate and wipe. Listing a forbidden command would produce a draft the validator
    then rejects, which is a worse experience than a drafter that never offers it; and the two
    sentences at the end exist because "reboot every Sunday" and "kill that process" are the
    two requests most likely to arrive first.
    """
    names = [s["name"] for s in scripts.list_scripts(db_path)]
    listed = ", ".join(sorted(names)) if names else "(none defined)"
    return ("Action types: " + ", ".join(rules.ACTION_TYPES) + ".\n"
            "Commands a rule may issue: " + ", ".join(rules.offered_commands()) + ".\n"
            "Scripts in the library: " + listed + ".\n"
            "A rule has no schedule and cannot kill a process, and there is no action for\n"
            "disabling USB or locking an account. If the request needs one of those, put one\n"
            "sentence in `refusal` instead of inventing an action.")


# The engine's expression language, described once. Both prompts in this file quote it -- the
# drafter's and the fleet query's -- and the two describing it separately is how one of them
# ends up a release behind the parser, teaching an operator a syntax that no longer parses.
EXPRESSION_SYNTAX = (
    "Expression syntax: and, or, not, parentheses, comparisons (>, >=, <, <=, ==, !=), "
    "contains, not_contains, starts_with, ends_with, matches, in, not_in, `is known`, "
    "`is unknown`. Durations are written 30s, 5m, 2h, 7d. Message templates interpolate "
    "{{variable.name}}.")


# The action shapes the prompt teaches, and the ONLY place they are written down.
#
# **Every one of these is asserted against rules.validate_actions by tests/test_ai.py.** That
# is not belt-and-braces: the first draft of this file told the model an alert takes
# `{"severity", "message"}`, which the engine rejects -- it takes `{"text"}` -- so the drafter
# failed on every well-formed request and the failure looked like a bad model. A prompt that
# teaches an invalid shape is a prompt that spends one repair round per request and then gives
# up, so the examples are tested rather than trusted.
#
# The `show_message` entry is here for a second reason. Without it the drafter's whole
# vocabulary for "get the machine restarted" was a bare `command`, so every request that said
# *ask the user first* came back as an unannounced reboot -- the engine has had the round trip
# since #14 and the prompt was the only thing that did not know. `on_response` is a SIBLING of
# `params`, not a key inside it, which is exactly the kind of detail a model gets wrong from
# memory and the reason these shapes are asserted rather than described.
ACTION_EXAMPLES = (
    {"type": "alert", "params": {"text": "Only {{disk.min_free_gb}} GB free"}},
    {"type": "command", "params": {"command_type": "restart", "params": {}}},
    {"type": "snooze", "params": {"seconds": 3600}},
    {"type": "show_message",
     "params": {"title": "Restart needed",
                "body": "This PC has been on for {{sys.uptime_days}} days. Restart now?",
                "style": "dialog", "buttons": [{"id": "yes"}, {"id": "later"}],
                "default_button": "yes", "timeout_seconds": 3600},
     "on_response": {"yes": [{"type": "command",
                              "params": {"command_type": "restart", "params": {}}}],
                     "later": [{"type": "snooze", "params": {"seconds": 14400}}]}},
)

# The envelope. Strict, and short on purpose: every field maps onto something
# rules.validate_rule already checks, so there is nowhere for the model to put anything else.
_ENVELOPE = """Answer with one JSON object and nothing else:

{"rules": [<one or more rule objects>], "refusal": ""}

A rule object:

{"name": "<short rule name>",
 "condition_text": "<one-line expression>",
 "target": {"include": [{"kind": "all"}]},
 "actions": [<one or more of the action shapes below>],
 "for_seconds": 0,
 "cooldown_seconds": 0}

**One rule is one stage.** A rule has no schedule and no escalation timer, so a request with
two thresholds is two rules with different conditions -- "warn after 5 days of uptime and
restart after 10" is `sys.uptime_days > 5` with a message and `sys.uptime_days > 10` with a
restart, never one rule. Order them gentlest first. At most %d rules, and only as many as the
request actually asked for: one threshold is one rule.

A stage that asks the person at the machine before acting is `show_message` with an
`on_response` map from the button they pressed to what happens next. A stage that acts without
asking is a bare `command`.

Action shapes, exactly as written -- an alert carries `text`, not a message or a severity, and
`on_response` sits beside `params` rather than inside it:
%s

Set `refusal` to one plain sentence and leave `rules` empty when the request cannot be
expressed with the variables and actions listed above. A refusal is the correct answer more
often than an approximation is.""" % (MAX_RULES_PER_DRAFT, "\n".join(
    " " + json.dumps(example) for example in ACTION_EXAMPLES))


def system_prompt(db_path, disks=None):
    """The whole prompt the drafter runs with: the namespace, the targets, the actions and the
    envelope.

    Assembled per request rather than cached, because all four of its inputs -- custom fields,
    probes, derived variables and scripts -- are operator-editable while the hub runs. A cache
    here would hand somebody a stale namespace minutes after they added the field they are
    writing the rule about, and the failure would look like a model that cannot count.
    """
    return "\n\n".join([
        "You translate an IT operator's sentence into rules for the FleetHub rules engine. One\nrule per stage: a request that escalates is a set of rules, not one rule with the\nharshest action.",
        "Variables you may reference. Use these names EXACTLY; there are no others:\n"
        + catalog_prompt(db_path, disks),
        EXPRESSION_SYNTAX,
        target_prompt(db_path),
        action_prompt(db_path),
        _ENVELOPE,
    ])


# ---------------------------------------------------------------------------------------
# Natural language -> a validated draft
# ---------------------------------------------------------------------------------------
# A model asked for "one JSON object and nothing else" returns a fenced code block often
# enough that stripping one is cheaper than spending a repair round on it.
_FENCE_RE = re.compile(r"^\s*```(?:json)?\s*(.*?)\s*```\s*$", re.DOTALL)

# The one sentence every unreadable answer becomes. Fixed text, because the model's own output
# is untrusted input and must not be echoed into a console as though it were a hub message.
UNREADABLE = "the AI provider did not answer with a rule this hub could read"


def _parse_envelope(text):
    """The model's text -> (error, [rule payload, ...]). Tolerant about wrapping, strict about
    shape.

    **A bare single-rule object is accepted and wrapped**, rather than corrected in a repair
    round. The old envelope asked for exactly that shape, every small local model has seen a
    thousand examples of it, and spending the one repair attempt teaching a model to put its
    one rule in a list would leave nothing for the error the attempt is actually for.

    A refusal travels as a one-element list whose single entry carries only `refusal`, so the
    caller has one thing to iterate and validated_draft keeps its existing contract.

    **The stage CAP is not enforced here.** This function answers one question -- is this a
    readable envelope -- and the cap is a policy of this hub's, checked in validated_rules
    where both callers already go. Keeping it out of the parse is also what lets draft_rule
    see how many stages the model actually asked for and aim the repair round at that.
    """
    raw = str(text or "").strip()
    match = _FENCE_RE.match(raw)
    if match:
        raw = match.group(1).strip()
    start, end = raw.find("{"), raw.rfind("}")
    if start >= 0 and end > start:
        raw = raw[start:end + 1]
    try:
        payload = json.loads(raw)
    except ValueError:
        return UNREADABLE, None
    if not isinstance(payload, dict):
        return UNREADABLE, None

    refusal = str(payload.get("refusal") or "").strip()
    if refusal:
        return None, [{"refusal": refusal}]

    entries = payload.get("rules")
    if entries is None:
        # The old shape: the rule fields sit at the top level.
        entries = [payload]
    if not isinstance(entries, list) or not entries:
        return UNREADABLE, None
    if not all(isinstance(entry, dict) for entry in entries):
        return UNREADABLE, None
    return None, entries


def _int_field(payload, key, cap):
    """One bounded integer out of the envelope. A model that answers "5m" where a number was
    asked for is a normal occurrence, not an error worth a repair round, so a value that will
    not convert becomes zero rather than a refusal."""
    try:
        value = int(payload.get(key) or 0)
    except (TypeError, ValueError):
        return 0
    return max(0, min(int(cap), value))


def validated_draft(db_path, payload, extra, *, allow_command=True):
    """One envelope -> (error, draft). **The gate this whole module exists for.**

    Runs the ENGINE's validators, in the engine's own order, and returns their messages
    verbatim on failure -- "unknown variable: cpu.temp_c" is the single most useful sentence
    this feature can produce. rules_web.py's docstring already argues the general form of
    this: validation text written by us goes back to the caller, and text from code we did not
    write does not.

    `allow_command` is the caller's ISSUE_COMMANDS capability, threaded through to
    rules.validate_actions exactly as the rules editor does it. A drafter that could quietly
    author a reboot for somebody who may only raise alerts would be a way around that gate,
    and the nested on_response case is why the check belongs in validate_actions rather than
    here.
    """
    refusal = str(payload.get("refusal") or "").strip()
    if refusal:
        return refusal[:MAX_REFUSAL_CHARS], None

    condition_text = str(payload.get("condition_text") or "").strip()
    if not condition_text:
        return "the AI provider answered without a condition", None
    error, condition = rules.parse_expression(condition_text, extra)
    if error:
        return error, None

    error, target = rules.validate_target(
        payload.get("target") or {"include": [{"kind": "all"}]}, extra)
    if error:
        return error, None

    error, actions = rules.validate_actions(
        payload.get("actions"), extra, allow_command=allow_command,
        script_specs=scripts.specs(db_path))
    if error:
        return error, None

    return None, {
        "name": str(payload.get("name") or "").strip()[:120] or "Drafted rule",
        # Both forms travel: the AST because the commit path saves it, and the canonical text
        # because that is what the operator reads and what /api/rules/preview accepts. The
        # text comes from format_expression rather than from the model, so what is shown is
        # what actually parsed -- not what was typed at it.
        "condition": condition,
        "condition_text": rules.format_expression(condition),
        "target": target,
        "actions": actions,
        "for_seconds": _int_field(payload, "for_seconds", rules.MAX_FOR_SECONDS),
        "cooldown_seconds": _int_field(payload, "cooldown_seconds",
                                       rules.MAX_COOLDOWN_SECONDS),
        "variables": rules.condition_variables(condition),
    }


def validated_rules(db_path, entries, extra, *, allow_command=True):
    """A parsed envelope -> (error, [draft rule, ...]). The set form of validated_draft.

    **All or nothing.** One rejected stage fails the whole answer, and the error names which
    stage it was so the repair round has somewhere to aim. Half an escalation is worse than
    none: a set committed with its enforcement stage missing looks configured and never acts,
    and a set missing its warning stage reboots people without asking -- which is the exact
    behaviour this feature was changed to stop.
    """
    entries = entries or []
    if len(entries) > MAX_RULES_PER_DRAFT:
        return (f"that would need {len(entries)} rules; this hub drafts at most "
                f"{MAX_RULES_PER_DRAFT} at a time"), None
    drafted = []
    for index, entry in enumerate(entries):
        error, draft = validated_draft(db_path, entry, extra, allow_command=allow_command)
        if error:
            # The index is only worth saying when there is more than one, otherwise it reads
            # as machine noise on top of a sentence an operator was meant to act on.
            if len(entries) > 1:
                return f"rule {index + 1}: {error}", None
            return error, None
        drafted.append(draft)
    if not drafted:
        return UNREADABLE, None
    return None, drafted


def _stamp(config):
    """The provider and model to stamp on an audit row. One helper so a call site cannot
    record half of the pair -- a row naming a model but not the endpoint it went to answers
    neither of the two questions that table exists for."""
    config = config or {}
    return {"provider": str(config.get("provider") or ""),
            "model": str(config.get("model") or "")}


def draft_rule(db_path, config, text, *, extra=None, api_key="", actor="", now=None,
               allow_command=True, disks=None):
    """An operator's sentence -> (error, draft). Saves nothing and fires nothing.

    One repair round, and one only: if the validators reject the first answer, the model is
    told what they said and asked again. See MAX_REPAIR_ATTEMPTS for why that number is 1.

    Every outcome lands in `ai_requests`, including the refusals -- a drafter that quietly
    fails half the time looks identical to one nobody is using, and the difference matters when
    deciding whether the provider is the wrong one.
    """
    request = str(text or "").strip()
    if not request:
        return "describe the rule you want", None
    if len(request) > MAX_REQUEST_CHARS:
        return f"that description is too long (limit {MAX_REQUEST_CHARS} characters)", None
    if extra is None:
        extra = rules.all_extra_variables(db_path)

    messages = [{"role": "system", "content": system_prompt(db_path, disks)},
                {"role": "user", "content": request}]
    last_error = None
    for attempt in range(MAX_REPAIR_ATTEMPTS + 1):
        error, answer = complete(config, messages, api_key=api_key)
        if error:
            # On a RETRY, the useful sentence is the validator's complaint from the first
            # answer, not "the provider could not be reached" -- the operator can act on
            # "unknown variable: cpu.temp_c" and cannot act on a transport failure that
            # happened while trying to fix it. A first-call failure has no such complaint to
            # prefer, so the provider error stands.
            error = last_error or error
            record_request(db_path, actor=actor, kind=KIND_DRAFT_RULE, **_stamp(config),
                           prompt_chars=len(request),
                           outcome=(OUTCOME_DISABLED if "switched off" in error
                                    else OUTCOME_PROVIDER_ERROR),
                           error=error, now=now)
            return error, None
        error, entries = _parse_envelope(answer)
        if not error:
            error, drafted = validated_rules(db_path, entries, extra,
                                             allow_command=allow_command)
            if not error:
                draft = {"rules": drafted, "source_text": request}
                record_request(db_path, actor=actor, kind=KIND_DRAFT_RULE, **_stamp(config),
                               prompt_chars=len(request), outcome=OUTCOME_OK, now=now)
                return None, draft
        last_error = error
        if attempt < MAX_REPAIR_ATTEMPTS:
            # A model that answered with too many stages cannot fix that by keeping every
            # stage it had, and there is only one repair round to spend: told the usual
            # thing, it re-sends the same over-cap set and the retry buys nothing for the one
            # case it could have saved. So the instruction follows the actual failure.
            if entries and len(entries) > MAX_RULES_PER_DRAFT:
                fix = (f"Answer again with FEWER rules -- merge or drop stages until there"
                       f" are at most {MAX_RULES_PER_DRAFT}.")
            else:
                fix = ("Answer again with the same JSON object, corrected, keeping every"
                       " stage you already had.")
            messages.append({"role": "assistant", "content": answer})
            messages.append({"role": "user", "content":
                             "The rules engine rejected that: " + str(error) + "\n" + fix
                             + " If the request cannot be expressed with the listed variables"
                               " and actions, set `refusal` instead."})
    record_request(db_path, actor=actor, kind=KIND_DRAFT_RULE, **_stamp(config),
                   prompt_chars=len(request), outcome=OUTCOME_REFUSED,
                   error=str(last_error), now=now)
    return last_error, None


def refine_draft(db_path, config, draft, text, *, extra=None, api_key="", actor="",
                 now=None, allow_command=True, disks=None):
    """"Make it 100 instead" -> (error, draft).

    The previous draft's canonical expression is re-sent as context, **not the conversation
    that produced it**. A refinement is an edit to a rule that already parsed, and re-sending
    the model's earlier prose invites it to re-litigate a decision the operator has already
    accepted -- while the expression is the thing they are actually looking at on screen.
    """
    request = str(text or "").strip()
    if not request:
        return "describe the change you want", None
    if len(request) > MAX_REQUEST_CHARS:
        return f"that description is too long (limit {MAX_REQUEST_CHARS} characters)", None
    if extra is None:
        extra = rules.all_extra_variables(db_path)

    current = draft or {}
    # Every stage goes back, not just the one somebody is looking at: "make it 7 days" after a
    # two-stage escalation means the warning stage, and a model shown one rule would answer
    # with one rule and quietly drop the other.
    previous = json.dumps({"rules": [{
        "name": rule.get("name", ""),
        "condition_text": rule.get("condition_text", ""),
        "target": rule.get("target"),
        "actions": rule.get("actions"),
        "for_seconds": rule.get("for_seconds", 0),
        "cooldown_seconds": rule.get("cooldown_seconds", 0),
    } for rule in draft_rules(current)]}, indent=1, sort_keys=True)
    messages = [{"role": "system", "content": system_prompt(db_path, disks)},
                {"role": "user", "content":
                 "The current draft is:\n" + previous + "\n\nChange it: " + request}]

    error, answer = complete(config, messages, api_key=api_key)
    if error:
        record_request(db_path, actor=actor, kind=KIND_REFINE_RULE, **_stamp(config),
                       prompt_chars=len(request), outcome=OUTCOME_PROVIDER_ERROR,
                       error=error, now=now)
        return error, None
    error, entries = _parse_envelope(answer)
    refined = None
    if not error:
        error, drafted = validated_rules(db_path, entries, extra,
                                         allow_command=allow_command)
        if not error:
            refined = {"rules": drafted}
    if error:
        record_request(db_path, actor=actor, kind=KIND_REFINE_RULE, **_stamp(config),
                       prompt_chars=len(request), outcome=OUTCOME_REFUSED,
                       error=str(error), now=now)
        return error, None

    # The English accumulates rather than being replaced. "Alert on low disk" followed by
    # "make it 5 GB" only makes sense as a pair, and the pair is what an audit needs to read.
    # Joined with a pipe rather than a newline: this ends up in the rule's one-line
    # description, where a newline collapses and leaves a stray marker mid-sentence.
    refined["source_text"] = " | ".join(
        part for part in (str(current.get("source_text") or "").strip(), request) if part)
    record_request(db_path, actor=actor, kind=KIND_REFINE_RULE, **_stamp(config),
                   prompt_chars=len(request), outcome=OUTCOME_OK, now=now)
    return None, refined


def draft_rules(draft):
    """The stages of a draft, as a list. One reader for both shapes.

    Drafts written before staging landed are a single rule at the top level of
    `payload_json`, and they are still sitting in `ai_drafts` on every hub that had this
    feature on. Reading them here rather than migrating the table keeps the old row readable
    with no schema change and no upgrade step -- and there is exactly one place that has to
    know both shapes.
    """
    draft = draft or {}
    staged = draft.get("rules")
    if isinstance(staged, list):
        return [rule for rule in staged if isinstance(rule, dict)]
    return [draft] if draft.get("condition") else []


def rule_payload(rule, source_text=""):
    """One drafted stage -> the payload rules.save_rule expects. Pure, and deliberately narrow.

    Nothing from the model reaches a stored rule except through this function, so what a draft
    can turn into is auditable by reading ten lines rather than by tracing the drafter.

    **`enabled` is False.** A rule that started firing the moment it was committed would make
    the confirmation step a formality -- the operator would be agreeing to a rule and
    discovering its blast radius afterwards, which is the wrong order for a feature whose whole
    safety argument is the human in the middle.
    """
    return {
        "name": rule.get("name") or "Drafted rule",
        "condition": rule.get("condition"),
        "target": rule.get("target"),
        "actions": rule.get("actions"),
        "for_seconds": rule.get("for_seconds") or 0,
        "cooldown_seconds": rule.get("cooldown_seconds") or 0,
        "enabled": False,
        # The English is the DRAFT's, not the stage's: a stage of an escalation only makes
        # sense read against the sentence that asked for the whole escalation, and that is
        # what somebody finding this rule in six months needs on it.
        "description": ("Drafted from: " + str(
            source_text or rule.get("source_text") or ""))[:500],
    }


def summarise_draft(db_path, draft, in_scope=None):
    """A one-line plain-language reading of a draft. **No model call.**

    Deterministic on purpose: the sentence an operator confirms has to be generated from the
    same AST that will be evaluated. A summary written by the model could describe a rule the
    engine is not going to run, and nothing in the console would reveal the difference.

    The machine COUNT is resolved here rather than left to the browser, because "this affects
    47 machines" is the part of a confirmation people actually read, and a count that arrives
    in a second request is a count that can be missing when the button is pressed.

    **`in_scope` is not optional in practice.** Without it a scoped operator is shown the
    fleet-wide count -- both a number they cannot act on and a statement of how many machines
    exist -- and the sentence would overstate the blast radius of the very rule they are being
    asked to approve. Defaulted to None only so this stays callable with no access layer to
    hand; every HTTP caller passes `access.in_scope`, the way preview_targets does.
    """
    rule = draft or {}
    parts = ["When " + rules.format_expression(rule.get("condition"))]
    if rule.get("for_seconds"):
        parts.append(f"has held for {int(rule['for_seconds'])}s")
    machines = rules.resolve_targets(db_path, rule.get("target"))
    if in_scope is not None:
        machines = [m for m in machines if in_scope(m)]
    parts.append(f"on {len(machines)} machine(s)")
    kinds = [str(a.get("type")) for a in (rule.get("actions") or [])]
    # A message's follow-ups are named too. "then show_message" reads as though nothing
    # happens to the machine, when the whole point of the stage is what the Yes button does.
    for action in rule.get("actions") or []:
        for outcome, followups in sorted((action.get("on_response") or {}).items()):
            kinds.append(f"{outcome} -> " + ", ".join(
                str(f.get("type")) for f in followups))
    parts.append("then " + (", ".join(kinds) if kinds else "nothing"))
    if rule.get("cooldown_seconds"):
        parts.append(f"at most once every {int(rule['cooldown_seconds'])}s")
    return ", ".join(parts) + "."


def summarise_rules(db_path, draft, in_scope=None):
    """Every stage of a draft, summarised in order. Still no model call.

    The stage numbering is part of the sentence rather than left to the browser, because the
    order is the escalation: reading "stage 2 of 2" next to a forced restart is what tells an
    operator the gentle one exists and is meant to be created too.
    """
    staged = draft_rules(draft)
    total = len(staged)
    summaries = []
    for index, rule in enumerate(staged):
        text = summarise_draft(db_path, rule, in_scope=in_scope)
        summaries.append(text if total == 1 else f"Stage {index + 1} of {total}: {text}")
    return summaries


# ---------------------------------------------------------------------------------------
# Draft storage
# ---------------------------------------------------------------------------------------
def _decode_draft(row):
    draft = json.loads(row["payload_json"])
    draft.update({"id": row["id"], "actor": row["actor"], "created_at": row["created_at"],
                  "updated_at": row["updated_at"], "source_text": row["source_text"],
                  "provider": row["provider"], "model": row["model"]})
    return draft


def save_draft(db_path, draft, *, draft_id=None, actor="", now=None, provider="", model=""):
    """Store or replace a draft. Returns the stored draft, with its id.

    The per-actor cap is enforced on WRITE rather than by the pruner, and per actor rather
    than globally: a drafter is an easy way to fill a table with plausible rows, the pruner
    runs daily, and one operator's afternoon of experiments must not push a colleague's
    unfinished draft out of the list.
    """
    stamp = float(now or time.time())
    draft_id = str(draft_id or uuid.uuid4().hex)
    payload = {k: v for k, v in dict(draft or {}).items()
               if k not in ("id", "actor", "created_at", "updated_at", "provider", "model")}
    with get_conn(db_path) as conn:
        conn.execute("""INSERT INTO ai_drafts (id, created_at, updated_at, actor, source_text,
                                               payload_json, provider, model)
                        VALUES (?,?,?,?,?,?,?,?)
                        ON CONFLICT(id) DO UPDATE SET
                            updated_at=excluded.updated_at,
                            source_text=excluded.source_text,
                            payload_json=excluded.payload_json""",
                     (draft_id, stamp, stamp, str(actor or ""),
                      str((draft or {}).get("source_text") or ""),
                      json.dumps(payload, sort_keys=True), str(provider), str(model)))
        conn.execute("""DELETE FROM ai_drafts WHERE actor = ? AND id NOT IN (
                            SELECT id FROM ai_drafts WHERE actor = ?
                            ORDER BY updated_at DESC LIMIT ?)""",
                     (str(actor or ""), str(actor or ""), MAX_DRAFTS_PER_ACTOR))
    return get_draft(db_path, draft_id)


def get_draft(db_path, draft_id):
    with get_conn(db_path) as conn:
        row = conn.execute("SELECT * FROM ai_drafts WHERE id = ?",
                           (str(draft_id),)).fetchone()
    return _decode_draft(row) if row else None


def list_drafts(db_path, actor=None, limit=50):
    """Drafts, newest first. Filtered by actor when one is given.

    A draft is somebody's unfinished sentence rather than fleet configuration, which is why
    the endpoint passes the caller's own address in. There is no reason for half-written
    English to appear in a colleague's console, and no gate that would make it appropriate.
    """
    query = "SELECT * FROM ai_drafts"
    params = []
    if actor is not None:
        query += " WHERE actor = ?"
        params.append(str(actor))
    query += " ORDER BY updated_at DESC LIMIT ?"
    params.append(max(1, min(500, int(limit or 50))))
    with get_conn(db_path) as conn:
        return [_decode_draft(r) for r in conn.execute(query, params).fetchall()]


def delete_draft(db_path, draft_id):
    with get_conn(db_path) as conn:
        return conn.execute("DELETE FROM ai_drafts WHERE id = ?",
                            (str(draft_id),)).rowcount > 0


def prune_drafts(db_path, retention_days, now=None):
    """Drop drafts older than the retention window. Returns how many went.

    Called from the same background pruner as everything else in `data`, so a hub that is
    never restarted does not accumulate a year of abandoned drafts.
    """
    cutoff = float(now or time.time()) - max(1, int(retention_days or 7)) * 86400
    with get_conn(db_path) as conn:
        return conn.execute("DELETE FROM ai_drafts WHERE updated_at < ?", (cutoff,)).rowcount


def record_request(db_path, *, actor="", kind="", provider="", model="", machine="",
                   prompt_chars=0, outcome=OUTCOME_OK, error="", now=None):
    """Append one row to the AI audit trail. Never raises -- auditing must not be able to
    break the thing it records, the same rule fleet.audit follows.

    **The prompt's LENGTH is recorded, not the prompt.** The request text is already kept on
    the draft, where the person who typed it can see their own; copying every sentence anybody
    tried into a second table with a different retention window is a privacy liability with no
    reader.
    """
    try:
        with get_conn(db_path) as conn:
            conn.execute("""INSERT INTO ai_requests (created_at, actor, kind, provider, model,
                                                     machine, prompt_chars, outcome, error)
                            VALUES (?,?,?,?,?,?,?,?,?)""",
                         (float(now or time.time()), str(actor or ""), str(kind or ""),
                          str(provider or ""), str(model or ""), str(machine or ""),
                          int(prompt_chars or 0), str(outcome or ""),
                          str(error or "")[:500]))
    except sqlite3.Error:
        pass


def list_requests(db_path, limit=100):
    """The AI audit trail, newest first. Read behind `view_audit_log`, like every other
    record of who did what."""
    with get_conn(db_path) as conn:
        rows = conn.execute("SELECT * FROM ai_requests ORDER BY created_at DESC LIMIT ?",
                            (max(1, min(1000, int(limit or 100))),)).fetchall()
    return [dict(r) for r in rows]


# ---------------------------------------------------------------------------------------
# What may leave this hub about ONE machine
# ---------------------------------------------------------------------------------------
# `ai.send_machine_names` is off by default, and honouring it is the whole reason the machine
# panel waited for a release of its own. A question about one PC carries, with no help at all,
# the hostname, the person signed into it, its serial number, its MAC address and the OU it
# sits in -- five identifiers, and the ones that matter identify a PERSON rather than a
# computer the moment somebody has the directory open beside them.
#
# **The filter is an allow-list, and that is the decision.** A deny-list naming "the
# identifying variables" has to be extended every time an operator adds a custom field, a
# probe or a derived variable, and the cost of forgetting once is an owner's name arriving at
# a hosted provider with nothing in the console to notice it by. An allow-list fails the other
# way: a new text variable is withheld until somebody decides it is safe, which shows up as a
# slightly thinner answer and never as a disclosure. Rejected: redacting by matching values
# against the machine list, which misses every identifier the hub did not think of first.
#
# Numbers and booleans pass unconditionally. A temperature, a disk percentage and "the link is
# up" identify nobody, and they are the entire substance of "why is this machine slow" -- a
# redaction that took them out would leave the panel with a privacy setting and no feature.
REDACTED = "(withheld)"

# The text variables that describe what a machine IS rather than whose it is. Deliberately
# short: `bios.support` is a vendor support state, `ad.os` an operating system name, and
# everything absent here -- hostname, serial, service tag, asset tag, signed-in account,
# domain, IP, MAC, OU, DN, owner, every custom field, every probe -- is withheld while the
# setting is off.
SHAREABLE_TEXT = frozenset((
    "sys.status", "sys.agent_version",
    "hw.model", "hw.manufacturer",
    "bios.version", "bios.vendor", "bios.support",
    "ad.os",
))


def _shareable(name, value, send_names):
    """Whether one resolved variable's VALUE may be sent to the provider.

    The NAME always may -- `session.user` is a schema, not a person -- which is why a withheld
    entry still reaches the prompt as a name with `(withheld)` next to it. A model told the
    variable exists and is not being shown stops asking for it; a model told nothing invents
    a hostname to make its sentence read well.
    """
    if send_names:
        return True
    if value.kind in (rules.KIND_NUMBER, rules.KIND_BOOL):
        return True
    return str(name) in SHAREABLE_TEXT


def machine_snapshot(resolved, *, send_names=False):
    """{name: Value} -> the rows the panel shows AND the prompt is built from. No model call.

    **One list, rendered twice on purpose.** The operator sees exactly what was sent, marked
    entry by entry, so "did this machine's name leave the building" is answerable from the
    screen rather than by reading this file. A second list built for display would be the one
    that drifts.
    """
    rows = []
    for name in sorted(resolved or {}):
        value = resolved[name]
        known = bool(getattr(value, "known", False))
        shared = _shareable(name, value, send_names)
        rows.append({
            "name": name,
            "kind": value.kind,
            "value": (rules.format_value(value.value) if known and shared
                      else (REDACTED if known else rules.UNKNOWN_PLACEHOLDER)),
            "age_seconds": value.age_seconds,
            "known": known,
            "withheld": known and not shared,
        })
    return rows


def snapshot_prompt(rows):
    """A snapshot as prompt text. Three sections, and the last two earn their bytes.

    **The AGE is on every line.** A machine that stopped reporting four hours ago still has a
    complete-looking set of numbers, and an answer that reads them as current is wrong in
    exactly the case where being wrong costs somebody an afternoon.

    The unknown names are listed because "the hub has no CPU reading for this PC" is a real
    answer to "why is it slow", and a model shown only the variables that resolved will reach
    for the ones that did instead. The withheld names are listed for the reason `_shareable`
    gives.
    """
    lines = []
    for row in rows:
        if not row["known"] or row["withheld"]:
            continue
        age = row.get("age_seconds")
        stamp = "" if age is None else f"   [{int(age)}s old]"
        lines.append(f"{row['name']} = {row['value']}{stamp}")
    body = "\n".join(lines)
    unknown = [row["name"] for row in rows if not row["known"]]
    if unknown:
        body += "\n\nNot reported by this machine: " + ", ".join(unknown)
    withheld = [row["name"] for row in rows if row["withheld"]]
    if withheld:
        body += ("\n\nWithheld by this hub on purpose -- do not ask for these and do not guess"
                 " at them: " + ", ".join(withheld))
    return body


# The rules the answering model works under. Every line is here because the alternative is a
# failure somebody would have to catch by reading the answer carefully, which is precisely
# what nobody does with a panel that usually sounds right.
MACHINE_ANSWER_RULES = """You are answering an IT operator's question about ONE Windows PC managed by FleetHub.
Everything this hub knows about that PC is listed below as variables, with each reading's age.

Rules:
* Answer ONLY from those readings. Nothing you know about other machines, other products or
  typical values is evidence about this one.
* Never state a number that is not in the readings, and never round one into a different one.
* Name the variables you used, spelled exactly as they are written.
* A reading that is hours old is evidence about the past. Say so rather than reading it as now.
* If the readings do not answer the question, say which reading is missing and stop. That is
  a useful answer; a plausible guess is not.
* At most six sentences of plain English. No markup, no lists, no headings."""


def answer_machine_question(db_path, config, machine, text, *, resolve_vars,
                            api_key="", actor="", now=None):
    """"Why is this machine slow?" -> (error, answer). The machine chat panel.

    The seam is `resolve_vars`: the caller hands in the same per-machine resolver rules_web.py
    builds for its preview endpoint, so the model answers over the variable namespace rather
    than over raw tables. That keeps the answer citable -- every number in it has a variable
    name and an age -- and keeps this function out of Flask and out of app.py.

    **The snapshot travels back with the answer, not just into the prompt.** The console shows
    both, which is the only honest way to render a paragraph a language model wrote: the
    operator reads the sentence, and the numbers it was supposedly built from sit underneath
    it. An answer that cites `metric.cpu_load_pct = 96` next to a row reading 12 is caught by
    the person reading it, and there is no other mechanism that would catch it -- a second
    model call checking the first is two guesses, not a check.

    Redaction is `machine_snapshot`'s, driven by `ai.send_machine_names`. Nothing here decides
    it per request: a per-question override would make "what leaves this hub" a property of
    whoever asked rather than of the hub's configuration.
    """
    request = str(text or "").strip()
    if not request:
        return "ask a question about this machine", None
    if len(request) > MAX_REQUEST_CHARS:
        return f"that question is too long (limit {MAX_REQUEST_CHARS} characters)", None

    # Resolved here as well as inside complete(), because `send_machine_names` decides what the
    # prompt may contain and the prompt is built before the call. Cheap, and the alternative --
    # reading the raw config dict for that one key -- is a second place that has to know how a
    # half-configured provider resolves.
    error, resolved_config = provider_config(config)
    if error:
        record_request(db_path, actor=actor, kind=KIND_MACHINE_ASK, **_stamp(config),
                       machine=str(machine), prompt_chars=len(request),
                       outcome=(OUTCOME_DISABLED if "switched off" in error
                                else OUTCOME_PROVIDER_ERROR),
                       error=error, now=now)
        return error, None

    send_names = bool(resolved_config["send_machine_names"])
    rows = machine_snapshot(resolve_vars(machine) or {}, send_names=send_names)
    subject = str(machine) if send_names else "this PC"
    messages = [
        {"role": "system",
         "content": MACHINE_ANSWER_RULES + f"\n\nReadings for {subject}:\n"
                    + snapshot_prompt(rows)},
        {"role": "user", "content": request},
    ]
    error, answer = complete(config, messages, api_key=api_key)
    record_request(db_path, actor=actor, kind=KIND_MACHINE_ASK, **_stamp(config),
                   machine=str(machine), prompt_chars=len(request),
                   outcome=(OUTCOME_PROVIDER_ERROR if error else OUTCOME_OK),
                   error=str(error or ""), now=now)
    if error:
        return error, None
    return None, {
        "machine": str(machine),
        "question": request,
        "answer": str(answer).strip()[:MAX_ANSWER_CHARS],
        "snapshot": rows,
        # Both stated, rather than left to be inferred from the rows: the panel prints a line
        # saying what was sent, and a console that had to count `withheld` flags to write it
        # would be one sentence away from claiming the opposite of the truth.
        "sent_machine_name": send_names,
        "withheld": sum(1 for row in rows if row["withheld"]),
    }


# ---------------------------------------------------------------------------------------
# Natural language -> a question the evaluator answers
# ---------------------------------------------------------------------------------------
# **Not text to SQL**, which reads as the safe place to start and is the most dangerous thing
# in this feature area: generated SQL cannot be passed through `access.filter_machines()`, so
# an operator scoped to one department asks a question and is answered from the whole fleet --
# a failure that looks exactly like a working feature. Rejected in ROADMAP.MD #24 and this is
# the code that does the other thing.
_QUERY_ENVELOPE = """Answer with one JSON object and nothing else:

{"condition_text": "<one-line expression>",
 "target": {"include": [{"kind": "all"}]},
 "refusal": ""}

The expression is a QUESTION, not a rule. It is evaluated on every machine the operator can
see, and the machines it is true for are the answer -- so there are no actions, no thresholds
to escalate between, and no second stage.

Leave `target` as every machine unless the question itself names a group, in which case use
the same selectors a rule uses.

Set `refusal` to one plain sentence and leave `condition_text` empty when the question cannot
be asked with the variables listed above. Three kinds of question cannot: one about what
happened earlier (these variables are the CURRENT reading and nothing else), one about an
individual process or a piece of installed software, and one asking why something is so
rather than which machines it is true of."""


def query_prompt(db_path, disks=None):
    """The whole prompt a fleet query runs with.

    Shares the namespace, the syntax and the target vocabulary with `system_prompt` and
    deliberately not the envelope: a question has no actions, and a prompt that offered them
    would produce a draft rule in answer to "which machines are hot", which is the wrong
    answer delivered convincingly.
    """
    return "\n\n".join([
        "You turn an IT operator's question about a fleet of Windows PCs into ONE expression\n"
        "for the FleetHub rules engine. The hub evaluates it and reports which machines it is\n"
        "true for; you never see the answer and must not guess at it.",
        "Variables you may reference. Use these names EXACTLY; there are no others:\n"
        + catalog_prompt(db_path, disks),
        EXPRESSION_SYNTAX,
        target_prompt(db_path),
        _QUERY_ENVELOPE,
    ])


def validated_query(payload, extra):
    """One query envelope -> (error, question). The gate, in the query's shape.

    The same discipline `validated_draft` applies, minus the actions a question does not have:
    the expression goes through the engine's parser, the target through the engine's target
    validator, and what comes back is the CANONICAL text rather than the model's, so the
    console shows what parsed instead of what was typed at it.
    """
    refusal = str(payload.get("refusal") or "").strip()
    if refusal:
        return refusal[:MAX_REFUSAL_CHARS], None
    condition_text = str(payload.get("condition_text") or "").strip()
    if not condition_text:
        return "the AI provider answered without a question this hub could evaluate", None
    error, condition = rules.parse_expression(condition_text, extra)
    if error:
        return error, None
    error, target = rules.validate_target(
        payload.get("target") or {"include": [{"kind": "all"}]}, extra)
    if error:
        return error, None
    return None, {"condition": condition,
                  "condition_text": rules.format_expression(condition),
                  "target": target,
                  "variables": rules.condition_variables(condition)}


def evaluate_query(db_path, question, *, in_scope, resolve_vars, limit=None):
    """Run a validated question across the fleet. **No model call, and no provider needed.**

    `in_scope` filters the machine list BEFORE evaluation rather than filtering the results
    afterwards, so a machine outside somebody's reach is never resolved at all -- its values
    are not read, and it cannot appear in a tally as a number that narrows a guess about how
    many machines exist.

    A machine whose values will not resolve counts as UNKNOWN rather than aborting the run.
    Three hundred machines answered and one that failed is a useful answer; a stack trace is
    not, and the same reasoning is written out at length in rules_web.preview_condition.
    """
    try:
        limit = int(limit or MAX_QUERY_RESULTS)
    except (TypeError, ValueError):
        # A console sending nonsense in `limit` should get an answer, not a 500. Same
        # reasoning as _int_field: the limit is a display preference, not part of the
        # question, so a value that will not convert becomes the default.
        limit = MAX_QUERY_RESULTS
    limit = max(1, min(MAX_QUERY_RESULTS, limit))
    machines = rules.resolve_targets(db_path, question.get("target"))
    if in_scope is not None:
        machines = [m for m in machines if in_scope(m)]
    tally = {"true": 0, "false": 0, "unknown": 0}
    matched = []
    for machine in machines:
        try:
            resolved = resolve_vars(machine)
            outcome = rules._result_name(rules.evaluate(question["condition"], resolved))
        except Exception:                             # noqa: BLE001
            tally["unknown"] += 1
            continue
        tally[outcome] += 1
        if outcome == "true" and len(matched) < limit:
            # The operand values travel with the hostname. "PC-07" alone invites the next
            # question; "PC-07, because disk.min_free_gb is 3" answers it in the same breath,
            # and it is the same `explain` the rule preview shows.
            matched.append({"machine": machine,
                            "detail": rules.explain(question["condition"], resolved)})
    return {"targeted": len(machines), "matched": tally["true"], "tally": tally,
            "results": matched, "truncated": tally["true"] > len(matched)}


def fleet_query(db_path, config, text, *, in_scope, resolve_vars, extra=None, api_key="",
                actor="", now=None, limit=None, disks=None):
    """"Which machines are over 90 degrees?" -> (error, answer).

    Two halves with a validator between them: the model turns a sentence into an expression,
    and the EVALUATOR -- the one that runs rules on this fleet -- answers it. Nothing the
    model says reaches the answer except an expression that parsed and a refusal it was asked
    to write in plain language.

    One repair round, for the reason `draft_rule` gives: a model that cannot fix "unknown
    variable: cpu.temp_c" on the first correction will not fix it on the fourth, and an
    unbounded loop is an unbounded bill.
    """
    request = str(text or "").strip()
    if not request:
        return "ask a question about the fleet", None
    if len(request) > MAX_REQUEST_CHARS:
        return f"that question is too long (limit {MAX_REQUEST_CHARS} characters)", None
    if extra is None:
        extra = rules.all_extra_variables(db_path)

    messages = [{"role": "system", "content": query_prompt(db_path, disks)},
                {"role": "user", "content": request}]
    last_error = None
    for attempt in range(MAX_REPAIR_ATTEMPTS + 1):
        error, answer = complete(config, messages, api_key=api_key)
        if error:
            error = last_error or error
            record_request(db_path, actor=actor, kind=KIND_FLEET_QUERY, **_stamp(config),
                           prompt_chars=len(request),
                           outcome=(OUTCOME_DISABLED if "switched off" in error
                                    else OUTCOME_PROVIDER_ERROR),
                           error=error, now=now)
            return error, None
        # The drafter's parser, reused rather than reimplemented. Its no-`rules`-key branch
        # returns the top-level object as the one entry, which is exactly this envelope's
        # shape, and its refusal branch already travels the same way -- a second parser here
        # would be a second place for the fence-stripping and brace-trimming to drift.
        error, entries = _parse_envelope(answer)
        if not error:
            error, question = validated_query(entries[0], extra)
            if not error:
                outcome = evaluate_query(db_path, question, in_scope=in_scope,
                                         resolve_vars=resolve_vars, limit=limit)
                record_request(db_path, actor=actor, kind=KIND_FLEET_QUERY, **_stamp(config),
                               prompt_chars=len(request), outcome=OUTCOME_OK, now=now)
                return None, {"question": request, **question, **outcome}
        last_error = error
        if attempt < MAX_REPAIR_ATTEMPTS:
            messages.append({"role": "assistant", "content": answer})
            messages.append({"role": "user", "content":
                             "The rules engine rejected that: " + str(error)
                             + "\nAnswer again with the same JSON object, corrected. If the"
                               " question cannot be asked with the listed variables, set"
                               " `refusal` instead."})
    record_request(db_path, actor=actor, kind=KIND_FLEET_QUERY, **_stamp(config),
                   prompt_chars=len(request), outcome=OUTCOME_REFUSED,
                   error=str(last_error), now=now)
    return last_error, None


# ---------------------------------------------------------------------------------------
# The fleet summary
# ---------------------------------------------------------------------------------------
# **The figures are computed here and the model only writes the sentence around them.** Same
# division #17 draws, and for the same reason: a summary that invents a number is worse than
# no summary at all, because this one gets quoted into a change ticket and outlives the day it
# describes. So `summary_figures` takes no config and makes no call, `summary_lines` renders
# it with no model either, and `daily_summary` adds prose on top of both.
#
# That ordering has a second consequence worth stating: a provider that is off, unreachable or
# out of credit costs the covering note and nothing else. The report still arrives.
#
# What is NOT here is correlation -- "these nine alerts are one switch" -- which belongs to
# #17 and is a statistic over the history table rather than a model's opinion. When it lands
# it becomes another section of `summary_figures`, computed the same way; it is deliberately
# not faked in the meantime, because a made-up grouping is the one error in a report nobody
# can check.


def _alert_machines(alert):
    """Every machine an alert row names. Two shapes, because two kinds of alert.

    A per-machine alert carries `machine`; a duplicate-serial one carries a LIST and no
    machine at all. Scoping has to see both, or a scoped operator's report silently counts a
    duplicate-serial alert raised on somebody else's hardware.
    """
    if alert.get("machine"):
        return [str(alert["machine"])]
    return [str(m) for m in (alert.get("machines") or []) if m]


def summary_figures(db_path, *, window_days=1, in_scope=None, now=None):
    """What happened in the window, counted. **No model call, and no provider needed.**

    **`in_scope` is the same gate a fleet query gets, and for a sharper reason.** A count is
    not obviously scoped when you read it -- "14 alerts today" looks like an answer rather
    than like a statement about a fleet somebody cannot see -- so an unscoped count handed to
    a scoped operator both misinforms them and tells them how much else exists. Alerts and
    fires are filtered by the machines they name; an alert naming no machine at all is counted
    only for an operator with no scope, which is the fail-closed reading.
    """
    window_days = max(1, min(MAX_SUMMARY_WINDOW_DAYS, int(window_days or 1)))
    end = int(now or time.time())
    start = end - window_days * 86400

    def visible(machines):
        if in_scope is None:
            return True
        return bool(machines) and any(in_scope(m) for m in machines)

    raised, cleared, kinds, touched = 0, 0, {}, set()
    for alert in alerts.episodes_between(db_path, start, end):
        named = _alert_machines(alert)
        if not visible(named):
            continue
        touched.update(named)
        created = int(alert.get("created_at") or 0)
        ended = alert.get("episode_ended_at")
        # Closed at both ends, the same window alerts.episodes_between selected on: `end` is
        # now, and an exclusive end drops whatever happened in the second the report was run.
        if start <= created <= end:
            raised += 1
            kinds[alert.get("kind", "")] = kinds.get(alert.get("kind", ""), 0) + 1
        if ended is not None and start <= int(ended) <= end:
            cleared += 1

    open_now = sum(1 for alert in alerts.list_open(db_path)
                   if visible(_alert_machines(alert)))

    names = {rule["id"]: rule.get("name") or f"rule {rule['id']}"
             for rule in rules.list_rules(db_path)}
    fires, by_rule, by_machine, outcomes = 0, {}, {}, {}
    for fire in rules.fires_between(db_path, start, end):
        machine = str(fire.get("machine") or "")
        if in_scope is not None and not (machine and in_scope(machine)):
            continue
        fires += 1
        # A fire whose rule has since been deleted cannot happen -- rules.py deletes the fires
        # with it -- but the name is looked up defensively anyway, because a report that dies
        # on a missing key is a report nobody gets.
        label = names.get(fire.get("rule_id"), f"rule {fire.get('rule_id')}")
        by_rule[label] = by_rule.get(label, 0) + 1
        by_machine[machine] = by_machine.get(machine, 0) + 1
        outcome = str(fire.get("outcome") or "")
        if outcome:
            outcomes[outcome] = outcomes.get(outcome, 0) + 1
        touched.add(machine)

    def top(counts):
        ranked = sorted(counts.items(), key=lambda pair: (-pair[1], pair[0]))
        return [{"name": name, "count": count} for name, count in ranked[:SUMMARY_TOP_N]]

    return {
        "window_days": window_days, "from": start, "to": end,
        "raised": raised, "raised_by_kind": top(kinds),
        "cleared": cleared, "open_now": open_now,
        "fires": fires, "fires_by_rule": top(by_rule), "fires_by_machine": top(by_machine),
        "fire_outcomes": top(outcomes),
        "machines_affected": len(touched - {""}),
        "rules_named": len(by_rule), "machines_named": len(by_machine),
        "scoped": in_scope is not None,
    }


def summary_lines(figures):
    """The figures as sentences, with no model anywhere near them.

    This is the report. `daily_summary`'s prose is a covering note ON it, which is why these
    lines are generated first and travel whether or not the provider answers -- and why the
    console renders both rather than the paragraph alone.
    """
    days = int(figures.get("window_days") or 1)
    span = "the last 24 hours" if days == 1 else f"the last {days} days"
    lines = [f"In {span}: {figures['raised']} alert episode(s) raised, "
             f"{figures['cleared']} cleared, {figures['open_now']} open now."]
    if figures["raised_by_kind"]:
        lines.append("Raised by kind: " + ", ".join(
            f"{entry['name']} {entry['count']}" for entry in figures["raised_by_kind"]))
    lines.append(f"{figures['fires']} rule fire(s) across "
                 f"{figures['machines_affected']} machine(s).")
    if figures["fires_by_rule"]:
        lines.append("Rules that fired: " + ", ".join(
            f"{entry['name']} x{entry['count']}" for entry in figures["fires_by_rule"])
            + (" (and more)" if figures["rules_named"] > len(figures["fires_by_rule"]) else ""))
    if figures["fire_outcomes"]:
        lines.append("Command outcomes: " + ", ".join(
            f"{entry['name']} {entry['count']}" for entry in figures["fire_outcomes"]))
    return lines


SUMMARY_RULES = """You are writing the covering note on an IT fleet's status report. The hub computed the
figures below; your job is to say what they mean to somebody skimming, in prose.

Rules:
* Use ONLY these figures. Do not introduce a number that is not among them, and do not
  re-round one into a different number.
* Do not guess at causes. You cannot see the machines, and a cause invented here is read as
  a finding.
* Say what is still outstanding last, because that is the part somebody has to act on.
* At most five sentences of plain English. No markup, no lists, no headings.
* If every figure is zero, say the fleet was quiet and stop."""


def daily_summary(db_path, config, *, window_days=1, in_scope=None, api_key="", actor="",
                  now=None):
    """The fleet health summary -- what was raised, remediated and left outstanding.

    Returns (error, summary), and **an unavailable provider is not an error here.** Every
    other entry point in this module refuses when the feature is off, because every other one
    has nothing to say without a model. This one has the whole report: the figures are
    arithmetic over two local tables, and refusing to compute them because a third party is
    unreachable would withhold the part that was never a guess in the first place. So the
    provider's failure arrives as `prose_error` next to a complete set of figures, and the
    console prints both.

    The error return is kept for a caller that hands in something unusable, and so this reads
    like its two siblings.
    """
    figures = summary_figures(db_path, window_days=window_days, in_scope=in_scope, now=now)
    summary = {"figures": figures, "lines": summary_lines(figures),
               "prose": "", "prose_error": ""}
    if not is_enabled(config):
        # Not recorded in `ai_requests`: nothing was requested of a provider, and a row saying
        # otherwise would make the audit trail overcount what this hub sent.
        summary["prose_error"] = "the AI features are switched off (Settings -> AI)"
        return None, summary

    body = "\n".join(summary["lines"])
    error, answer = complete(config, [{"role": "system", "content": SUMMARY_RULES},
                                      {"role": "user", "content": body}], api_key=api_key)
    record_request(db_path, actor=actor, kind=KIND_SUMMARY, **_stamp(config),
                   prompt_chars=len(body),
                   outcome=(OUTCOME_PROVIDER_ERROR if error else OUTCOME_OK),
                   error=str(error or ""), now=now)
    if error:
        summary["prose_error"] = error
        return None, summary
    summary["prose"] = str(answer).strip()[:MAX_ANSWER_CHARS]
    return None, summary
