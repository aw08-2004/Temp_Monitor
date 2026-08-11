# Agent release notes

One file per agent release, named `<version>.md`, holding the body published with that
version's `agent-v<version>` GitHub release. The directory listing is the archive; there is no
separate changelog to keep in sync with it.

Write for the operators who run the fleet, not for whoever changed the code: what they will
notice, what it costs them, and what to do when it misbehaves. An agent update reaches every
machine within about fifteen minutes of the release being signed, so anything in here that
needs a decision needs to be readable before it has already happened everywhere.

## Publishing one

Pass the path, never the text:

```powershell
.\release.ps1 -Version 3.27.0 -NotesFile .\release-notes\3.27.0.md
```

`-NotesFile` hands the path to `gh --notes-file`, so gh reads the bytes and nothing parses
them on the way. That matters more than it sounds. Prose routed through PowerShell's
native-argument binding has broken this twice already:

- The 3.26.0 notes published with `emote-helper.log` for `remote-helper.log`, and a literal
  tab where `type` should have been — `\r` and `\t` eaten as escapes.
- The first 3.27.0 attempt failed outright with ``no matches found for `commands` ``. The
  notes contain the phrase `"Push commands to at most"`, whose embedded quotes split the
  argument; everything positional after the tag is an asset path to `gh release create`, so
  it went looking for a file called `commands`.

`-Notes "one short sentence"` is still fine when there is nothing in it to misparse. Anything
longer goes in a file.

To fix a release that was already published with a mangled body:

```powershell
gh release edit agent-v3.27.0 --repo aw08-2004/Temp_Monitor --notes-file .\release-notes\3.27.0.md
```
