# Copyright (C) 2026 Frederik Pasch
# SPDX-License-Identifier: Apache-2.0
"""The question both pin refreshers ask before rewriting anything.

Shared rather than copied because the two commands (`refresh_build_pins.py`,
`refresh_source_pins.py`) must answer it identically: the one that drifts is the one that rewrites
a pin nobody agreed to, and a pin is the thing a rebuild a year from now depends on.

Two properties it keeps:

* **The report is the question.** The diff is printed first and the prompt comes after it, because a
  flag decided in advance commits to an answer before its subject is visible.
* **No terminal means no write.** A build script that inherited one of these commands must not move
  a pin because nobody was there to say no, so the fallback is the report -- the same as passing
  nothing -- and it says which flag would have applied it.
"""

import sys


def add_arguments(parser) -> None:
    """The two flags every refresher takes, so `--write` and `--ask` mean one thing here."""
    parser.add_argument("--write", action="store_true",
                        help="Rewrite the pins without asking. Without it, only report.")
    parser.add_argument("--ask", action="store_true",
                        help="Report, then ask on the terminal whether to apply it.")


def apply_or_ask(edits, count, args) -> bool:
    """Write *edits* (a path -> new text mapping) if this run is allowed to.

    Returns whether anything was written, and explains itself when it was not.
    """
    if not args.write:
        if not (args.ask and sys.stdin.isatty()):
            hint = (" -- no terminal to ask on, so add WRITE=1 (or --write) to apply it"
                    if args.ask else " -- add --write")
            print(f"\n{count} pin(s) would change. Nothing written{hint}.")
            return False
        try:
            answer = input(f"\nApply {count} pin change(s)? [y/N] ").strip().lower()
        except EOFError:
            answer = ""
        if answer not in ("y", "yes"):
            print("Nothing written.")
            return False
    for path, text in edits.items():
        path.write_text(text, encoding="utf-8")
    return True
