# Linux agent release notes

One file per Linux agent release, named `<version>.md`, holding the body published with that
version's `linux-agent-v<version>` GitHub release. The conventions are the Windows agent's --
see [agent/release-notes/README.md](../../agent/release-notes/README.md) -- and above all the
same rule for publishing: pass the path with `-NotesFile`, never the text.

```powershell
.\release.ps1 -Version 0.3.0 -NotesFile .\release-notes\0.3.0.md
```

0.1.0 and 0.2.0 were published without a file here; their bodies live only on their GitHub
releases.
