<!--
House style: this repo's PR bodies are prose, not checklists with the boxes
ticked. Say what changed, why the obvious alternative was wrong, and how you
know it works. Delete every section that does not apply -- an empty heading is
worse than no heading. Reviewers read this before the diff.
-->

## What

One or two sentences. Which halves does this touch -- hub, agent, client, docs?
If it is hub-only or docs-only, say so here; that is the first thing a reviewer
needs to know, because it decides whether a release has to be cut.

Roadmap item, if any: `#NN` (see [ROADMAP.MD](ROADMAP.MD)).

## Why

The problem, not the patch. What breaks today, how it surfaces to an operator,
and how it was found. If it is a race, a silent failure, or a case that "looks
fine" -- spell out the sequence that goes wrong.

## How

The shape of the fix, and the decisions the diff cannot explain by itself:
what you rejected and why, what is deliberately left alone as out of scope,
and anything a reader would otherwise flag as a mistake.

## Versions

Per [VERSIONING.md](VERSIONING.md) -- `MAJOR.MINOR.PATCH`, three numeric
components, strictly increasing, never reused. Name the new number for each
line this PR moves, or write "unchanged".

| Line | Where | This PR |
|---|---|---|
| Hub | `HUB_VERSION` in `hub/app.py` | `1.x.y` / unchanged |
| Agent | `AgentConfig.Version` **and** csproj `<Version>` | `3.x.y` / unchanged |
| Client | `clientVersion` **and** `pubspec.yaml` | `1.x.y` / unchanged |

- Every hub change bumps `HUB_VERSION`, including a hub-only one.
- The two-file pairs must match; move them with `agent/release.ps1 -Version …`
  or `python release_client.py --set-version …`, not by hand.
- A minor, not a patch, if the hub might gate on it or an operator can see it.
- A **major** means a running deployment breaks. If this is one, say what an
  operator must do by hand, and state the deploy order explicitly: **hub first**,
  then the agent, which reaches the fleet in ~15 minutes.

## Release

Only if this ships an agent or client build.

- [ ] Release note added at `agent/release-notes/<version>.md`
- [ ] Hub deployed before the agent release is cut
- [ ] Any hub-side version gate names the version that actually carries the
      feature (a "built at" number in the roadmap is a *source* version and is
      not always a release)

If no release is needed, write **"No agent release."** and why -- e.g. the
change is entirely hub-side and reuses a verb every agent already answers.

## Verification

Counts, not adjectives. State them the way the log does:

```
python tests/run_all.py -q     # NN/NN hub modules
dotnet test                    # NNN/NNN agent
```

Say which modules gained tests and by how much (`test_patches_web 45 -> 62`),
and what each new case pins. If a bug reached review, add the test that would
have caught it and say why the old assertion passed.

Beyond the suite: anything driven by hand -- a real panel in a browser, a dry
run of the release script, an agent at an older version refusing the new verb.

> `dotnet test` rewrites `agent/tests/fixtures/*.fhb` in place. Check
> `git status` before staging.

## Security

Delete unless the change touches an authorization boundary, the command
surface, signing or release plumbing (see [SECURITY.MD](SECURITY.MD)).

- Which reads are narrowed to the caller's machine scope, and which writes.
- What a scoped operator can now reach that they could not before, or vice versa.
- Anything interpolated into a command line: allow-list, not escaping.
- Whether a compromised hub gains any new destination or verb.

## For the reviewer

Where to look first, what you are least sure of, and any known window you
deliberately left open as a follow-up. Naming it here is cheaper than having it
found.
