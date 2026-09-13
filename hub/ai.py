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
from ipaddress import ip_address
from urllib.parse import urlparse

import requests

import rules
import scripts

# ---------------------------------------------------------------------------------------
# Providers
# ---------------------------------------------------------------------------------------

# One wire shape, deliberately. "openai_chat" names the REQUEST FORMAT, not a vendor -- it is
# what Ollama, vLLM, LM Studio, OpenRouter and OpenAI itself all accept, so the enum is a
# protocol choice rather than a privileged vendor. #17 calls for "no vendor privileged in the
# schema"; a second entry here (say an Anthropic Messages adapter) is additive and changes
# nothing else in this file, which is the point of the indirection.
PROVIDER_OPENAI_CHAT = "openai_chat"
PROVIDERS = (PROVIDER_OPENAI_CHAT,)

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
MAX_DRAFTS_PER_ACTOR = 25
MAX_REFUSAL_CHARS = 400

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


def provider_config(config, api_key=""):
    """Settings -> (error, resolved). The one place a half-configured provider is caught.

    `config` is injected rather than read from settings here, the rule rules.py follows: this
    module stays unit-testable against a dict, and the caller re-reads the settings per request
    so an admin switching the feature off is obeyed on the next call.

    `api_key` comes from the environment via app.py for the same reason AGENT_ENROLLMENT_SECRET
    does -- settings.py is not a secret store, and a module that reads os.environ itself is one
    whose configuration a test cannot vary.

    **An empty key is not an error.** A local Ollama needs none, and refusing to run without
    one would make the LAN-only deployment -- the one with no data-egress question attached at
    all -- the hardest of the three to configure.
    """
    config = config or {}
    if not is_enabled(config):
        return "the AI features are switched off (Settings -> AI)", None
    base_url = str(config.get("base_url") or "").strip().rstrip("/")
    model = str(config.get("model") or "").strip()
    if not base_url:
        return "no AI provider is configured (set the base URL in Settings -> AI)", None
    if not model:
        return "no AI model is configured (set the model in Settings -> AI)", None
    error = check_provider_url(base_url, bool(config.get("allow_private_endpoint")))
    if error:
        return error, None
    return None, {
        "provider": str(config.get("provider") or PROVIDER_OPENAI_CHAT),
        "base_url": base_url,
        "model": model,
        "api_key": str(api_key or ""),
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

    Response text is capped rather than trusted. A provider that streams back a megabyte is
    either broken or hostile, and no rule description needs more than MAX_RESPONSE_CHARS.
    """
    error, resolved = provider_config(config, api_key)
    if error:
        return error, None

    headers = {"Content-Type": "application/json", "User-Agent": "FleetHub-AI/1.0"}
    if resolved["api_key"]:
        headers["Authorization"] = f"Bearer {resolved['api_key']}"
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
        response = requests.post(
            f"{resolved['base_url']}/v1/chat/completions",
            json=body, headers=headers,
            timeout=int(timeout or resolved["timeout"] or 60),
            # No redirects, for the reason notify.py gives: a 302 walks straight past every
            # check check_provider_url just made.
            allow_redirects=False)
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

    if response.status_code >= 400:
        return f"the AI provider returned HTTP {response.status_code}", None
    try:
        payload = response.json()
        text = payload["choices"][0]["message"]["content"]
    except (ValueError, KeyError, IndexError, TypeError):
        return "the AI provider returned a response this hub could not read", None
    text = str(text or "")
    if not text.strip():
        return "the AI provider returned an empty response", None
    return None, text[:MAX_RESPONSE_CHARS]


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


# The action shapes the prompt teaches, and the ONLY place they are written down.
#
# **Every one of these is asserted against rules.validate_actions by tests/test_ai.py.** That
# is not belt-and-braces: the first draft of this file told the model an alert takes
# `{"severity", "message"}`, which the engine rejects -- it takes `{"text"}` -- so the drafter
# failed on every well-formed request and the failure looked like a bad model. A prompt that
# teaches an invalid shape is a prompt that spends one repair round per request and then gives
# up, so the examples are tested rather than trusted.
ACTION_EXAMPLES = (
    {"type": "alert", "params": {"text": "Only {{disk.min_free_gb}} GB free"}},
    {"type": "command", "params": {"command_type": "restart", "params": {}}},
    {"type": "snooze", "params": {"seconds": 3600}},
)

# The envelope. Strict, and short on purpose: every field maps onto something
# rules.validate_rule already checks, so there is nowhere for the model to put anything else.
_ENVELOPE = """Answer with one JSON object and nothing else:

{"name": "<short rule name>",
 "condition_text": "<one-line expression>",
 "target": {"include": [{"kind": "all"}]},
 "actions": [<one or more of the action shapes below>],
 "for_seconds": 0,
 "cooldown_seconds": 0,
 "refusal": ""}

Action shapes, exactly as written -- an alert carries `text`, not a message or a severity:
%s

Set `refusal` to one plain sentence and leave the other fields empty when the request cannot
be expressed with the variables and actions listed above. A refusal is the correct answer more
often than an approximation is.""" % "\n".join(
    " " + json.dumps(example) for example in ACTION_EXAMPLES)


def system_prompt(db_path, disks=None):
    """The whole prompt the drafter runs with: the namespace, the targets, the actions and the
    envelope.

    Assembled per request rather than cached, because all four of its inputs -- custom fields,
    probes, derived variables and scripts -- are operator-editable while the hub runs. A cache
    here would hand somebody a stale namespace minutes after they added the field they are
    writing the rule about, and the failure would look like a model that cannot count.
    """
    return "\n\n".join([
        "You translate an IT operator's sentence into ONE rule for the FleetHub rules engine.",
        "Variables you may reference. Use these names EXACTLY; there are no others:\n"
        + catalog_prompt(db_path, disks),
        "Expression syntax: and, or, not, parentheses, comparisons (>, >=, <, <=, ==, !=), "
        "contains, not_contains, starts_with, ends_with, matches, in, not_in, `is known`, "
        "`is unknown`. Durations are written 30s, 5m, 2h, 7d. Message templates interpolate "
        "{{variable.name}}.",
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
    """The model's text -> (error, dict). Tolerant about wrapping, strict about shape."""
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
    return None, payload


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
        error, payload = _parse_envelope(answer)
        if not error:
            error, draft = validated_draft(db_path, payload, extra,
                                           allow_command=allow_command)
            if not error:
                draft["source_text"] = request
                record_request(db_path, actor=actor, kind=KIND_DRAFT_RULE, **_stamp(config),
                               prompt_chars=len(request), outcome=OUTCOME_OK, now=now)
                return None, draft
        last_error = error
        if attempt < MAX_REPAIR_ATTEMPTS:
            messages.append({"role": "assistant", "content": answer})
            messages.append({"role": "user", "content":
                             "The rules engine rejected that: " + str(error)
                             + "\nAnswer again with the same JSON object, corrected. If the"
                               " request cannot be expressed with the listed variables and"
                               " actions, set `refusal` instead."})
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
    previous = json.dumps({
        "name": current.get("name", ""),
        "condition_text": current.get("condition_text", ""),
        "target": current.get("target"),
        "actions": current.get("actions"),
        "for_seconds": current.get("for_seconds", 0),
        "cooldown_seconds": current.get("cooldown_seconds", 0),
    }, indent=1, sort_keys=True)
    messages = [{"role": "system", "content": system_prompt(db_path, disks)},
                {"role": "user", "content":
                 "The current draft is:\n" + previous + "\n\nChange it: " + request}]

    error, answer = complete(config, messages, api_key=api_key)
    if error:
        record_request(db_path, actor=actor, kind=KIND_REFINE_RULE, **_stamp(config),
                       prompt_chars=len(request), outcome=OUTCOME_PROVIDER_ERROR,
                       error=error, now=now)
        return error, None
    error, payload = _parse_envelope(answer)
    refined = None
    if not error:
        error, refined = validated_draft(db_path, payload, extra,
                                         allow_command=allow_command)
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


def rule_payload(draft):
    """A draft -> the payload rules.save_rule expects. Pure, and deliberately narrow.

    Nothing from the model reaches a stored rule except through this function, so what a draft
    can turn into is auditable by reading ten lines rather than by tracing the drafter.

    **`enabled` is False.** A rule that started firing the moment it was committed would make
    the confirmation step a formality -- the operator would be agreeing to a rule and
    discovering its blast radius afterwards, which is the wrong order for a feature whose whole
    safety argument is the human in the middle.
    """
    return {
        "name": draft.get("name") or "Drafted rule",
        "condition": draft.get("condition"),
        "target": draft.get("target"),
        "actions": draft.get("actions"),
        "for_seconds": draft.get("for_seconds") or 0,
        "cooldown_seconds": draft.get("cooldown_seconds") or 0,
        "enabled": False,
        "description": ("Drafted from: " + str(draft.get("source_text") or ""))[:500],
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
    parts = ["When " + rules.format_expression(draft.get("condition"))]
    if draft.get("for_seconds"):
        parts.append(f"has held for {int(draft['for_seconds'])}s")
    machines = rules.resolve_targets(db_path, draft.get("target"))
    if in_scope is not None:
        machines = [m for m in machines if in_scope(m)]
    parts.append(f"on {len(machines)} machine(s)")
    kinds = [str(a.get("type")) for a in (draft.get("actions") or [])]
    parts.append("then " + (", ".join(kinds) if kinds else "nothing"))
    if draft.get("cooldown_seconds"):
        parts.append(f"at most once every {int(draft['cooldown_seconds'])}s")
    return ", ".join(parts) + "."


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
# Not built yet -- roadmap #24's later halves
# ---------------------------------------------------------------------------------------
# Three stubs, each with its seam named, because for these the SHAPE is the decision and the
# code is not. What is deliberately absent matters as much: anomaly baselining and alert
# correlation belong to #17, which says a per-machine baseline is a statistic over the history
# table rather than a model. A stub for them in this file would make the board lie.
def answer_machine_question(db_path, config, machine, text, *, resolve_vars,
                            api_key="", actor="", now=None):
    """"Why is this machine slow?" -> (error, answer). The machine chat panel.

    The seam is `resolve_vars`: the caller hands in the same per-machine resolver rules_web.py
    already builds for its preview endpoint, so the model answers over the variable namespace
    rather than over raw tables. That keeps the answer citable -- every number in it has a
    variable name and an age -- and keeps this function out of Flask and out of app.py.

    Not built. The work is the redaction pass `ai.send_machine_names` implies: a question about
    one PC carries its hostname, its logged-in user and its serial number unless something
    takes them out first, and deciding what "took them out" means is not a detail.
    """
    raise NotImplementedError("roadmap #24: the machine chat panel is not built yet")


def fleet_query(db_path, config, text, *, in_scope, api_key="", actor="", now=None):
    """"Which machines are over 90 degrees?" -> (error, rows).

    **Not text to SQL.** The model maps the question onto a rules expression and the existing
    evaluator answers it, because generated SQL cannot be passed through
    `access.filter_machines()` -- a scoped operator would be answered from the whole fleet, and
    that is the one failure in this feature area that looks exactly like a working feature.
    Rejected explicitly in ROADMAP.MD #24.

    `in_scope` is the caller's scope predicate, applied to the machine list BEFORE evaluation
    rather than to the results, so a machine outside somebody's reach is never resolved at all.

    Not built.
    """
    raise NotImplementedError("roadmap #24: natural-language fleet query is not built yet")


def daily_summary(db_path, config, *, window_days=1, api_key="", now=None):
    """The fleet health summary -- what was raised, remediated and left outstanding.

    The seam is that the FIGURES are computed here, deterministically, from alerts.py's
    episodes and rules.py's `rule_fires`; only the prose wrapping them goes to the model. Same
    division #17 draws, and for the same reason: a summary that invents a number is worse than
    no summary, because it will be quoted into a change ticket.

    Not built. Its real prerequisite is #17's correlation, without which the summary is a
    restatement of the alert list in longer words.
    """
    raise NotImplementedError("roadmap #24: the fleet summary is not built yet")
