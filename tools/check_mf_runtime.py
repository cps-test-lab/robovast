#!/usr/bin/env python3
# Copyright (C) 2026 Frederik Pasch
# SPDX-License-Identifier: Apache-2.0

"""The web UI and the panel remotes must share one Module-Federation runtime version.

A skew between them fails at *load* time with Module Federation's own assert --
``remoteEntryExports is undefined`` -- which names neither the versions nor the file, and
which its runtime produces by catching the browser's real ``import()`` rejection and
re-throwing this instead. It cost one debugging round already.

Two package trees, so this is neither tree's own test: ``frontend/ui`` pins
``@module-federation/runtime`` directly, while ``src/robovast_nav/web`` gets its runtime
transitively, from whichever version ``@module-federation/vite`` happens to bundle
(1.18.2 -> 2.8.0; 1.20.7 -> 2.8.2). Both are pinned exactly for that reason -- a caret on
either is what lets them drift apart on someone's next ``npm install``.

Run from the repo root: ``python tools/check_mf_runtime.py`` (or ``make check-mf-runtime``).
Exits non-zero with what to change, and skips with a message when the remote's
``node_modules`` is absent -- there is nothing to compare then, and failing would make the
check depend on whether somebody has installed a tree they may not be touching.
"""

import json
import pathlib
import sys

ROOT = pathlib.Path(__file__).resolve().parents[1]
HOST = ROOT / "frontend" / "ui" / "package.json"
REMOTE = ROOT / "src" / "robovast_nav" / "web" / "package.json"
REMOTE_PLUGIN = (ROOT / "src" / "robovast_nav" / "web" / "node_modules"
                 / "@module-federation" / "vite" / "package.json")

RUNTIME = "@module-federation/runtime"


def _deps(path: pathlib.Path) -> dict:
    data = json.loads(path.read_text(encoding="utf-8"))
    return {**(data.get("dependencies") or {}), **(data.get("devDependencies") or {})}


def main() -> int:
    host_pin = _deps(HOST).get(RUNTIME)
    if not host_pin:
        print(f"✗ {HOST.relative_to(ROOT)} does not depend on {RUNTIME}.\n"
              f"  The UI calls init/registerRemotes/loadRemote and runs no MF build plugin, "
              f"so it should depend on the runtime directly -- not reach it through "
              f"@module-federation/enhanced, which is the bundler plugin.")
        return 1
    if host_pin.startswith(("^", "~", ">")):
        print(f"✗ {HOST.relative_to(ROOT)} pins {RUNTIME} as {host_pin!r}.\n"
              f"  It has to equal what the remote bundle embeds, and a range is what lets "
              f"that drift. Pin it exactly.")
        return 1

    remote_range = _deps(REMOTE).get("@module-federation/vite", "")
    if remote_range.startswith(("^", "~", ">")):
        print(f"✗ {REMOTE.relative_to(ROOT)} pins @module-federation/vite as "
              f"{remote_range!r}.\n"
              f"  The runtime it bundles comes with it, so a range here floats the version "
              f"this check is trying to hold. Pin it exactly.")
        return 1

    if not REMOTE_PLUGIN.is_file():
        print(f"• skipped: {REMOTE_PLUGIN.parent.relative_to(ROOT)} is not installed, so "
              f"there is no bundled runtime to compare against.\n"
              f"  Run 'npm ci' in src/robovast_nav/web to include it.")
        return 0

    bundled = _deps(REMOTE_PLUGIN).get(RUNTIME, "").lstrip("^~>=")
    if bundled != host_pin:
        print(f"✗ Module-Federation runtime skew:\n"
              f"    host   frontend/ui                {RUNTIME} {host_pin}\n"
              f"    remote src/robovast_nav/web       {RUNTIME} {bundled} "
              f"(via @module-federation/vite {remote_range})\n\n"
              f"  Panels will fail to load with 'remoteEntryExports is undefined', which "
              f"names none of this.\n"
              f"  Set frontend/ui's pin to {bundled}, or move both sides together.")
        return 1

    print(f"✓ Module-Federation runtime {host_pin} on both sides "
          f"(host pin, and @module-federation/vite {remote_range}'s bundled runtime).")
    return 0


if __name__ == "__main__":
    sys.exit(main())
