# Agent release notes

One file per agent release, named `<version>.md`, holding the body published with that
version's `agent-v<version>` GitHub release. The directory listing is the archive; there is no
separate changelog to keep in sync with it.

Write for the operators who run the fleet, not for whoever changed the code: what they will
notice, what it costs them, and what to do when it misbehaves. An agent update reaches every
machine within about fifteen minutes of the release being signed, so anything in here that
needs a decision needs to be readable before it has already happened everywhere.

## Publishing one

Pass the file, never the text. `release.ps1 -Notes` hands its argument to `gh release create`
as a PowerShell string, and that path is what put `emote-helper.log` (for `remote-helper.log`)
and a stray tab where `type` should have been into the published 3.26.0 notes: the `\r` and
`\t` were consumed as escapes before `gh` ever saw them.

```powershell
.\release.ps1 -Version 3.27.0 -Notes (Get-Content -Raw .\release-notes\3.27.0.md)
```

`Get-Content -Raw` reads the file verbatim, so nothing gets a chance to interpret a backslash
on the way through. If a release has already been published with the wrong body, replace it
from the file directly — this path never touches PowerShell argument binding at all:

```powershell
gh release edit agent-v3.27.0 --repo aw08-2004/Temp_Monitor --notes-file .\release-notes\3.27.0.md
```
