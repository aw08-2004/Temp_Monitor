# Vendored third-party assets

The hub has no bundler and no npm step, and its pages must work on an isolated LAN with no
internet egress, so browser dependencies are checked in as pre-built UMD files rather than
pulled from a CDN at page load. Serving them ourselves is also what keeps the Terminal tab
from depending on a third party being reachable (and from being a place a third party could
inject script into a page that runs code as SYSTEM).

Everything here is downloaded verbatim from jsDelivr at a PINNED version. Nothing in this
directory is hand-edited -- if a file needs changing, bump the version and re-download, so
the checksums below stay the record of what is actually being served.

| File                                      | Package                     | Version | SHA-256 |
|-------------------------------------------|-----------------------------|---------|---------|
| `xterm.js`                                | `@xterm/xterm`              | 5.5.0   | `1f991ac3b4b283ebf96e60ae23a00a52765dd3a2e46fa6fdda9f1aab032f7495` |
| `xterm.css`                               | `@xterm/xterm`              | 5.5.0   | `ba8e6985669488981ccf40c0cefe3aba80722cb6c92de7ad628b0bd717faf2b6` |
| `xterm-addon-fit.js`                      | `@xterm/addon-fit`          | 0.10.0  | `bdaefa370b1bfc42ee88d46fe6072400902a4d4b2d45cd93438dda9b23c97089` |
| `chart.umd.min.js`                        | `chart.js`                  | 4.5.1   | `48444a82d4edcb5bec0f1965faacdde18d9c17db3063d042abada2f705c9f54a` |
| `chartjs-adapter-date-fns.bundle.min.js`  | `chartjs-adapter-date-fns`  | 3.0.0   | `ea7ab30d26c38dcf1f2d26bb43e73a94537b58f1906f55e1a546dd09321b5615` |
| `chartjs-plugin-zoom.min.js`              | `chartjs-plugin-zoom`       | 2.2.0   | `e4a088e5bab93be6ee47c939eeb9ebaa80e0b39156d4bdfd1af9c844be81b6c4` |
| `socket.io.min.js`                        | `socket.io-client`          | 4.7.2   | `83df4abc7eec941f1d29ae254e80bac0bb82d398fbe2e8ee4ea2a7efc8e704f1` |
| `qrcode.js`                               | `qrcode-generator`          | 2.0.4   | `79ec86f82856005b1c887905cfccfcfbec3821ca61c7fd5a952faa5f778f791c` |
| `qrcode_UTF8.js`                          | `qrcode-generator`          | 2.0.4   | `e522d64003b332e29271fdce4993ed3ae2934c8947f41654bd324ddcfa2de301` |
| `fonts/inter-latin-wght-normal.woff2`     | `@fontsource-variable/inter`| 5.3.0   | `3100e775e8616cd2611beecfa23a4263d7037586789b43f035236a2e6fbd4c62` |
| `fonts/inter-latin-ext-wght-normal.woff2` | `@fontsource-variable/inter`| 5.3.0   | `34b9c504cab7a73e37b746343a449132e56cf7b5481af2cb81dc74dcff25c956` |

`fonts/inter.css` and `fonts/LICENSE` are the exceptions to the no-hand-editing rule in
opposite directions: the CSS is ours (fontsource's own stylesheet points at its package
layout, not ours, so it is written here and commented), and the licence is shipped verbatim
because SIL OFL 1.1 requires it to travel with the font.

## Refreshing

```sh
curl -L -o xterm.js           https://cdn.jsdelivr.net/npm/@xterm/xterm@5.5.0/lib/xterm.js
curl -L -o xterm.css          https://cdn.jsdelivr.net/npm/@xterm/xterm@5.5.0/css/xterm.css
curl -L -o xterm-addon-fit.js https://cdn.jsdelivr.net/npm/@xterm/addon-fit@0.10.0/lib/addon-fit.js
curl -L -o chart.umd.min.js   https://cdn.jsdelivr.net/npm/chart.js@4.5.1/dist/chart.umd.min.js
curl -L -o chartjs-adapter-date-fns.bundle.min.js https://cdn.jsdelivr.net/npm/chartjs-adapter-date-fns@3.0.0/dist/chartjs-adapter-date-fns.bundle.min.js
curl -L -o chartjs-plugin-zoom.min.js             https://cdn.jsdelivr.net/npm/chartjs-plugin-zoom@2.2.0/dist/chartjs-plugin-zoom.min.js
curl -L -o socket.io.min.js   https://cdn.jsdelivr.net/npm/socket.io-client@4.7.2/dist/socket.io.min.js
curl -L -o qrcode.js          https://cdn.jsdelivr.net/npm/qrcode-generator@2.0.4/dist/qrcode.js
curl -L -o qrcode_UTF8.js     https://cdn.jsdelivr.net/npm/qrcode-generator@2.0.4/dist/qrcode_UTF8.js
curl -L -o fonts/inter-latin-wght-normal.woff2     https://cdn.jsdelivr.net/npm/@fontsource-variable/inter@5.3.0/files/inter-latin-wght-normal.woff2
curl -L -o fonts/inter-latin-ext-wght-normal.woff2 https://cdn.jsdelivr.net/npm/@fontsource-variable/inter@5.3.0/files/inter-latin-ext-wght-normal.woff2
curl -L -o fonts/LICENSE      https://cdn.jsdelivr.net/npm/@fontsource-variable/inter@5.3.0/LICENSE
sha256sum xterm.js xterm.css xterm-addon-fit.js chart.umd.min.js \
          chartjs-adapter-date-fns.bundle.min.js chartjs-plugin-zoom.min.js \
          socket.io.min.js qrcode.js qrcode_UTF8.js fonts/*.woff2   # must match the table above
```

## Why xterm.js

The Terminal tab drives a real Windows pseudoconsole (ConPTY) on the agent, so what comes
back is a VT/ANSI byte stream, not lines of text: cursor addressing, erase-in-line, SGR
colour, alternate screen buffer. Rendering that correctly is a terminal emulator's whole
job -- a hand-rolled `<pre>` renderer handles colour and newlines and then falls apart on
exactly the interactive programs (installers, `Read-Host` prompts, progress bars) the PTY
exists to support.

## Why the rest of this directory exists

**Everything below xterm was previously loaded from a CDN, which made the paragraph at the
top of this file false.** Chart.js, its date adapter and its zoom plugin came from jsDelivr,
socket.io from cdnjs, and the Inter webfont from Google Fonts on EVERY page including the
login screen. So the isolated-LAN claim held for exactly one tab -- Terminal -- and nowhere
else, and the machine page (charts) and the whole console (font) silently required internet
egress from each operator's browser.

That was not a small gap. A hub on an air-gapped site rendered unstyled and chart-less, and
the failure mode is cosmetic-looking rather than obviously broken, so it reads as "the
console is ugly here" rather than "a dependency is unreachable".

**The `integrity` and `crossorigin` attributes were dropped with the move, deliberately.**
Subresource integrity is how you make a THIRD PARTY's copy trustworthy; on a file served from
our own origin it protects nothing that serving it ourselves has not already protected, and
it introduces a failure the old setup did not have -- a stale hash after a version bump
disables the script silently, in the browser, with nothing in the hub's logs.

**Rejected: keeping the CDN and relying on SRI.** SRI protects integrity, not availability,
and the claim at the top of this file is about availability.

**socket.io moved from cdnjs to jsDelivr** at the same pinned 4.7.2 so this directory has one
source and one refresh idiom. Version 4.7.2 is not arbitrary: it must stay compatible with
the Socket.IO protocol the server's Flask-SocketIO speaks, so this is a pin to match, not a
number to keep current. Upgrade the two together or not at all.

## Why qrcode-generator

Device Owner provisioning (roadmap #23) starts by scanning a QR at a factory-reset device's
setup wizard, so the console has to draw one. A QR encoder is not a thing to hand-roll:
Reed-Solomon error correction, the eight mask patterns and the format/version information
blocks are a specification to implement, not an algorithm to improvise, and the failure mode
of getting one subtly wrong is a code that scans on the phone in your hand and not on the one
at the desk.

**`qrcode.js` is checked in UNMINIFIED**, unlike its neighbours, and that is deliberate rather
than an oversight: the package publishes no minified build, and for 56 KB on one page that
only `manage_settings` holders open, a file somebody can actually read is worth more than the
bytes. It also carries its own MIT licence header, which is why there is no separate licence
file for it here.

`qrcode_UTF8.js` (800 bytes) replaces the default byte encoder with a UTF-8 one. The
provisioning payload is ASCII today, so it changes nothing -- it is here so that an operator
who one day pastes a non-ASCII character into an APK URL gets a QR that encodes it correctly
rather than one that silently encodes something else.

**Rejected: `qrious`**, which is the better-known library and renders straight to a canvas. It
is GPL-3.0, and nothing else in this repo is copyleft.

**The console does not use the library's own `createSvgTag`/`createImgTag` renderers.**
`provisioning.js` reads `isDark(row, col)` and paints a canvas itself: about fifteen lines,
no `innerHTML` anywhere near a payload that contains operator-supplied text, and no `data:`
URI. Same instinct as the rest of the frontend, and see hub/provisioning.py for what is in
that payload.
