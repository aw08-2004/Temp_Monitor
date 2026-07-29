# Vendored third-party assets

The hub has no bundler and no npm step, and its pages must work on an isolated LAN with no
internet egress, so browser dependencies are checked in as pre-built UMD files rather than
pulled from a CDN at page load. Serving them ourselves is also what keeps the Terminal tab
from depending on a third party being reachable (and from being a place a third party could
inject script into a page that runs code as SYSTEM).

Everything here is downloaded verbatim from jsDelivr at a PINNED version. Nothing in this
directory is hand-edited -- if a file needs changing, bump the version and re-download, so
the checksums below stay the record of what is actually being served.

| File                 | Package             | Version | SHA-256 |
|----------------------|---------------------|---------|---------|
| `xterm.js`           | `@xterm/xterm`      | 5.5.0   | `1f991ac3b4b283ebf96e60ae23a00a52765dd3a2e46fa6fdda9f1aab032f7495` |
| `xterm.css`          | `@xterm/xterm`      | 5.5.0   | `ba8e6985669488981ccf40c0cefe3aba80722cb6c92de7ad628b0bd717faf2b6` |
| `xterm-addon-fit.js` | `@xterm/addon-fit`  | 0.10.0  | `bdaefa370b1bfc42ee88d46fe6072400902a4d4b2d45cd93438dda9b23c97089` |

## Refreshing

```sh
curl -L -o xterm.js           https://cdn.jsdelivr.net/npm/@xterm/xterm@5.5.0/lib/xterm.js
curl -L -o xterm.css          https://cdn.jsdelivr.net/npm/@xterm/xterm@5.5.0/css/xterm.css
curl -L -o xterm-addon-fit.js https://cdn.jsdelivr.net/npm/@xterm/addon-fit@0.10.0/lib/addon-fit.js
sha256sum xterm.js xterm.css xterm-addon-fit.js   # must match the table above
```

## Why xterm.js

The Terminal tab drives a real Windows pseudoconsole (ConPTY) on the agent, so what comes
back is a VT/ANSI byte stream, not lines of text: cursor addressing, erase-in-line, SGR
colour, alternate screen buffer. Rendering that correctly is a terminal emulator's whole
job -- a hand-rolled `<pre>` renderer handles colour and newlines and then falls apart on
exactly the interactive programs (installers, `Read-Host` prompts, progress bars) the PTY
exists to support.
