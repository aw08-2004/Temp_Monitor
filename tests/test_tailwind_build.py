"""The Tailwind pipeline's shape: pinned, preflight-free, committed, linked.

The silent failures this file exists to catch:

  * **A deployed hub serving no utilities at all.** Hub self-update ships hub/ as plain files
    and never builds anything, so an app.css that was never committed -- or a base.html that
    stopped linking it -- means every Tailwind class in the console has no CSS, and nothing
    errors anywhere.
  * **Preflight sneaking in.** Tailwind's reset restyles every heading, button and border in
    the console at once. components.css still styles most pages, so preflight arriving in a
    routine rebuild would visibly break pages nobody touched.
  * **An unpinned or unverified CLI.** The build script downloads an executable. A version
    without a digest beside it is a binary trusted on first sight.
  * **The binary getting committed.** 110 MB in git history, on every clone, forever.

What this cannot check is whether app.css is up to date with the classes in the templates --
that needs the CLI, which the test environment does not have. Rebuild after changing classes.

Run from the repo root.
"""
import os
import re
import sys

PASS = 0
FAIL = 0

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CSS = os.path.join(ROOT, "hub", "static", "css")


def check(name, cond):
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"  [ok] {name}")
    else:
        FAIL += 1
        print(f"  [XX] {name}")


def read(*parts):
    with open(os.path.join(ROOT, *parts), encoding="utf-8") as fh:
        return fh.read()


def test_the_output_is_committed_and_linked():
    print("\n-- app.css exists and every page loads it --")
    path = os.path.join(CSS, "app.css")
    check("hub/static/css/app.css exists", os.path.isfile(path))
    check("...and is not empty", os.path.isfile(path) and os.path.getsize(path) > 0)
    base = read("hub", "templates", "base.html")
    check("base.html links it", "filename='css/app.css'" in base)
    check("...after components.css, so a utility wins over the rule it adjusts",
          base.find("css/components.css") < base.find("css/app.css"))


def test_no_preflight():
    print("\n-- no preflight while components.css still styles the console --")
    source = read("hub", "static", "css", "tailwind.input.css")
    directives = "\n".join(line for line in source.splitlines()
                           if line.strip().startswith("@"))
    check("the input imports theme and utilities only",
          'tailwindcss/theme.css' in directives and 'tailwindcss/utilities.css' in directives)
    check("...and not the whole framework", not re.search(r'@import\s+"tailwindcss"\s*;', directives))
    check("...nor preflight by name", "preflight" not in directives)
    path = os.path.join(CSS, "app.css")
    if os.path.isfile(path):
        built = read("hub", "static", "css", "app.css")
        # Preflight's signature rule: the universal box-sizing/border reset.
        check("the built file carries no preflight reset",
              not re.search(r"\*,\s*:after,\s*:before|\*,\s*::after,\s*::before", built))


def test_colours_come_from_the_tokens():
    print("\n-- theme colours are tokens.css variables, not a second palette --")
    source = read("hub", "static", "css", "tailwind.input.css")
    check("the theme block is inline, so var() resolves at runtime", "@theme inline" in source)
    tokens = read("hub", "static", "css", "tokens.css")
    for name in re.findall(r"--color-[a-z-]+:\s*var\((--[a-z-]+)\)", source):
        check(f"{name} is defined in tokens.css", f"{name}:" in tokens)


def test_the_cli_is_pinned_and_verified():
    print("\n-- the build script pins a version and verifies its digest --")
    script = read("tools", "build_css.ps1")
    version = re.search(r"\$TailwindVersion\s*=\s*'(v\d+\.\d+\.\d+)'", script)
    digest = re.search(r"\$TailwindSha256\s*=\s*'([0-9a-f]{64})'", script)
    check("an exact version is pinned", version is not None)
    check("...with a SHA-256 beside it", digest is not None)
    check("the digest is checked before the binary is kept", "Test-Digest $partial" in script)
    check("...and again before an existing one is run", "Test-Digest $exe" in script)
    check("the script does not assign PowerShell's automatic $input",
          not re.search(r"^\s*\$input\s*=", script, re.M))
    check("the downloaded binary is gitignored", "tools/.bin/" in read(".gitignore"))


def main():
    test_the_output_is_committed_and_linked()
    test_no_preflight()
    test_colours_come_from_the_tokens()
    test_the_cli_is_pinned_and_verified()
    print(f"\n==== {PASS} passed, {FAIL} failed ====")
    return 1 if FAIL else 0


if __name__ == "__main__":
    sys.exit(main())
