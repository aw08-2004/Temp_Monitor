"""Static check on hub/app.py: every background thread is started after the last function the
module defines.

The silent failure this exists to catch is **a background thread whose first pass dies of a
NameError on every hub start**. Each start_*() launches a thread that runs its first pass at
once, while app.py is still being imported -- so a call placed above a function the thread
uses races the import, and loses. The rule evaluator did exactly that until hub 1.141.0: it was
started six hundred lines above _recent_sensors_for, and the first evaluation after every
restart failed for every rule over machine data, with nothing in the console to show for it but
one line in the service log. The order of definitions in a 6,000-line file is not something
anyone checks by eye, so this does.
"""
import ast
import os
import sys

PASS = 0
FAIL = 0

APP = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "hub", "app.py")


def check(name, cond):
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"  [ok] {name}")
    else:
        FAIL += 1
        print(f"  [XX] {name}")


def main():
    with open(APP, encoding="utf-8") as fh:
        tree = ast.parse(fh.read(), filename=APP)

    last_def = max(node.lineno for node in tree.body
                   if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)))
    starts = [node for node in tree.body
              if isinstance(node, ast.Expr) and isinstance(node.value, ast.Call)
              and isinstance(node.value.func, ast.Name)
              and node.value.func.id.startswith("start_")]

    check("app.py starts its background threads at module level", len(starts) >= 10)
    for node in starts:
        check(f"{node.value.func.id}() (line {node.lineno}) runs after the last definition "
              f"(line {last_def})", node.lineno > last_def)

    print(f"\n==== {PASS} passed, {FAIL} failed ====")
    return 1 if FAIL else 0


if __name__ == "__main__":
    sys.exit(main())
