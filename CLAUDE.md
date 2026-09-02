# FleetHub

Windows fleet management / RMM for one IT helpdesk group. Three shipped components on three
independent version lines, each with a real updater running in the field — so a version
mistake here reaches real machines, or silently reaches none of them.

- `hub/` — Flask + Socket.IO console and API. The only thing an operator sees.
- `agent/` — C#/.NET 10 Windows Service, LocalSystem, installed on every managed PC.
- `app/` — Flutter desktop client (roadmap #11).
- `tests/` — the hub's Python suite.
- `install.ps1` — unified menu-driven installer for all three.

Four canonical docs already exist. Go to them by question, not by default:

| Question | File |
|---|---|
| What a feature does, or how to operate it | `README.md` — grep it, don't read it |
| Is this planned or shipped, and what was rejected | `ROADMAP.MD` — features `#1`–`#21` |
| Anything about a version number | `VERSIONING.md` — 120 lines, read it whole |
| Auth, capabilities, threat model | `SECURITY.MD` |

## Search hygiene

- **Exclude `.claude/worktrees/`** from every Glob and Grep — it is a full duplicate checkout
  of this repo and doubles every hit.
- **Exclude `flutter_sdk/`** — a vendored Flutter SDK checkout, gitignored, never edited.
- Also not yours: `logs/`, `.venv/`, `agent/**/bin/`, `agent/**/obj/`, `agent/dist/`, `.env`,
  `sign_release.py`.

## Versioning — the rules that break production

- **Bump `HUB_VERSION` (`hub/app.py:110`) on every push to `main` that touches `hub/`.** Do it
  as part of the change, unasked; the comment two lines above it says the same thing. The hub
  compares `main`'s value against the running one, so a push without a bump never reaches a
  deployed hub. Docs-only and `tests/`-only pushes need no bump.
- **Exactly `MAJOR.MINOR.PATCH`, three numeric components, no suffix.** No `-rc1`, no `+build`,
  never two or four components — `parse_hub_version()` matches digits and dots only, so a
  suffix parses as `None` and the hub silently stops discovering updates fleet-wide.
- **Strictly increasing. Never reuse or lower a number.** Every updater asks "is the other side
  *strictly* newer", so a repeated or lowered number means nothing happens anywhere while the
  release looks shipped. This has bitten twice.
- **Agent and client versions move only when cutting a release, and only via the scripts** —
  `agent/release.ps1 -Version x.y.z` and `python release_client.py --set-version x.y.z`. Each
  has a two-file pair that must agree, and the scripts are what keep them in sync. Unreleased
  source on `main` keeps the old number.
- **Agent MINOR has teeth.** Anything the hub might gate on needs one, because the hub hardcodes
  the minor that introduced a feature — `MIN_PTY_AGENT`, `MIN_PROCESS_AGENT`, `MIN_OPEN_AGENT`
  and friends in `hub/static/js/`. Those constants are a compatibility API, not trivia.
- **Deploy order is always hub first, then agent**, which propagates in about 15 minutes. The
  reverse leaves the console offering a feature no machine answers.
- Everything else about versions → `VERSIONING.md`.

## Don't "clean this up"

Each of these looks like leftover mess and is load-bearing.

- **`Temp_Monitor` / `TempMonitorAgent` / `TempMonitorHub`** in code, paths, service names and
  namespaces are **correct**. The product was renamed FleetHub; the GitHub repo rename was
  cancelled. Do not rename them.
- **Never remove the `-text` pins in `.gitattributes`**, and never let a tool rewrite line
  endings on the signed manifests or their `.sig` files. A signature covers exact bytes, so a
  rewritten file is indistinguishable from a tampered one and the fleet rejects every update
  with no obvious cause.
- **Command signing was removed deliberately.** `ALLOWED_EMAILS` plus permission groups are the
  whole perimeter. Don't reintroduce signing, and don't weaken a route's gate.
- **No linter or formatter config exists anywhere.** That is deliberate — don't add one.

## Testing

- Hub: `python tests/run_all.py` (`-q` for a summary). It runs **each module in its own fresh
  interpreter on purpose** — modules set up a temp DB, import `app` (which starts a
  process-lifetime `db_writer` daemon and keeps in-memory caches) and assert on wall-clock
  online/offline state. `pytest tests/` shares one process and goes flaky. Don't "fix" the
  runner into a plain pytest call.
- Editing one module: `pytest tests/test_x.py` works — `tests/conftest.py` adapts the house
  pattern.
- Agent: `dotnet test agent/TempMonitorAgent.slnx`.
  Publish: `dotnet publish agent/src/TempMonitorAgent/TempMonitorAgent.csproj -c Release -o agent/dist`.
- **Trap:** an agent `dotnet test` run rewrites `tests/fixtures/*.fhb` in place. Check
  `git status` before staging.
- **No CI runs these tests** — only CodeQL and a Claude PR review. If you didn't run them,
  nobody did.
- A new test module follows the house pattern: module-level `PASS`/`FAIL` counters, a
  `check(name, cond)` helper, a `main()` printing `==== N passed, M failed ====`, and
  `if __name__ == "__main__": sys.exit(main())`. Its docstring names the *silent failure* the
  file exists to catch, not the function it covers.

## Code conventions

**Hub.** `x.py` is the model half and is strictly **Flask-free** — pure functions over SQLite,
unit-testable standalone. `x_web.py` is the HTTP surface, exposed as a
`create_<x>_blueprint(...)` factory and registered from `app.py`. Keep the split when adding to
a pair; a new feature area gets a new pair, not a route in `app.py`. `app.py` is the composition
root only — config, DB init, Socket.IO, OAuth, `/api/report` ingest, self-update, blueprint
registration.

**Authorization is two gates, and a machine route needs both.** They come from the `access`
object built in `hub/permissions_web.py`: `access.require(cap)` for a capability-only route,
**`access.require_machine(cap)`** for anything naming a machine (capability *and* scope), and
`access.in_scope()` / `filter_rows()` / `filter_machines()` for scoping reads. `hub/wake_web.py`
is the canonical example — copy the gate from a sibling `*_web.py` rather than inventing one.

**Frontend.** Vanilla JS, IIFE + `'use strict'`, **no framework and no build step**. Scripts in
`hub/static/js/`, templates in `hub/templates/` and `templates/partials/`, CSS custom properties
in `hub/static/css/tokens.css`. Don't introduce a bundler, a framework, or npm.

**Agent.** `net10.0-windows`, published self-contained single-file win-x64, runs as a Windows
Service under LocalSystem. `Worker.cs` is six independent concurrent loops. Class suffixes are
load-bearing: `*Executor.cs` implements `ICommandExecutor` for one fleet command type (its
`Type` must match the hub's `COMMAND_TYPE`; routed by `Fleet/CommandDispatcher.cs`),
`*Reporter.cs` pushes change-only data on the heartbeat, `*Reader.cs` is a local read-only
probe. File-scoped namespaces, primary constructors, `sealed` by default, XML doc comments.

## i18n is enforced by tests, not by convention

- All user-facing English lives in `hub/locales/en.json`, and `de.json` + `es.json` must be
  updated in the **same change**. `tests/test_i18n.py::test_key_parity_with_english` fails on
  missing *and* extra keys, and placeholders must match English exactly.
- Only **literal** `t('key')` calls are scanned, so prefer literal keys over computed ones —
  `t('a.' + kind)` is invisible to the test that would have caught the typo.
- Server-supplied UI text counts too: a new `settings.REGISTRY` entry, a new
  `permissions.CAPABILITIES` entry, an enum choice, a unit slug. No catalog entry, no passing
  test.

## House style

The most distinctive thing about this codebase, and what a generic model gets wrong by default.

- **Comments are design rationale, not description.** Every module opens with a docstring
  answering why it exists and what silent failure it prevents, usually citing a roadmap number
  (`roadmap #14`). Match the density of the file you're editing, and don't strip comments you
  didn't write.
- **Write down the alternatives you rejected, and why.** Record history when something has
  bitten before ("This has bitten twice already"). That is the point of the density.
- `**bold**` inside docstrings for the load-bearing claim. Use `--`, not em dashes, in Python
  and C# source.
- With no linter, style comes from imitating the surrounding file. Read it before adding to it.
- **Commit messages use the same voice**: a title stating the change in plain language, a body
  explaining **why**, not what. For calibration: *"Narrow the maintenance-window read, and stop
  the winget parse eating the source column"*.
- **Don't create new top-level `.md` files** for summaries, notes or plans. Findings go in the
  reply; durable decisions go in `ROADMAP.MD`.

## Roadmap upkeep

`ROADMAP.MD` is the live board — features `#1`–`#21` marked ✅ done / 🚧 partial / 📋 planned.
Update an entry when a feature's status actually changes, and record the decision and the
alternatives you rejected there. Code comments cite these numbers, so a stale board makes them
lie.

## Before you call it done

```
[ ] Touched hub/?           -> bump HUB_VERSION in hub/app.py
[ ] New user-facing string? -> en.json + de.json + es.json, same change
[ ] python tests/run_all.py -q      (+ dotnet test if agent/ changed)
[ ] Ran agent tests?        -> git status for rewritten tests/fixtures/*.fhb
[ ] New machine route?      -> access.require_machine(), not just access.require()
[ ] Feature status changed? -> ROADMAP.MD
[ ] Commit message says WHY
```
