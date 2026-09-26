#!/usr/bin/env python3
"""Scaffold a campaign archive layout step.

Writes the step module, wires it into the ladder and bumps ``ARCHIVE_LAYOUT`` -- the places
a step otherwise has to touch by hand, which is where one gets forgotten.

    python3 tools/new_archive_migration.py

Then implement the transform in the generated ``vN_to_vM.py`` and give it a test in
``tests/common/test_archive_migrations.py``, which fails for the new step until it has one.
See ``src/robovast/common/migrations/README.md`` for the rules a step must follow.
"""

import pathlib
import re
import sys

_REPO = pathlib.Path(__file__).resolve().parents[1]
_LADDER = _REPO / "src" / "robovast" / "common" / "migrations" / "archive" / "__init__.py"

_IMPORT_MARKER = "# <new-migration-import>"
_ENTRY_MARKER = "# <new-migration-entry>"

_STEP_TEMPLATE = '''"""Archive layout {frm} -> {to}: <one line saying what changed in the tree>.

<Which record moved or changed its format, and why an archive of layout {frm} needs
rewriting to be read.>

**A step takes the extracted campaign directory and rewrites it in place.** It must not
import the models its records are read with now; it touches only paths inside the campaign
directory it is given.
"""

from pathlib import Path


def migrate(campaign_dir: Path) -> None:
    """Carry *campaign_dir* from layout {frm} to {to}, in place."""
    raise NotImplementedError("archive layout step {frm}_to_{to}")
'''


def main() -> int:
    text = _LADDER.read_text(encoding="utf-8")
    match = re.search(r"^ARCHIVE_LAYOUT = (\d+)$", text, re.MULTILINE)
    if not match:
        sys.exit(f"could not find 'ARCHIVE_LAYOUT = <n>' in {_LADDER.relative_to(_REPO)}")
    frm = int(match.group(1))
    to = frm + 1
    module = f"v{frm}_to_v{to}"
    step_file = _LADDER.parent / f"{module}.py"
    if step_file.exists():
        sys.exit(f"{step_file.relative_to(_REPO)} already exists")
    for marker in (_IMPORT_MARKER, _ENTRY_MARKER):
        if marker not in text:
            sys.exit(f"marker {marker} missing from {_LADDER.relative_to(_REPO)}")
    text = text.replace(_IMPORT_MARKER, f"from . import {module}  # noqa: F401\n{_IMPORT_MARKER}", 1)
    text = text.replace(_ENTRY_MARKER, f"{module}.migrate,\n    {_ENTRY_MARKER}", 1)
    text = text.replace(match.group(0), f"ARCHIVE_LAYOUT = {to}", 1)
    step_file.write_text(_STEP_TEMPLATE.format(frm=frm, to=to), encoding="utf-8")
    _LADDER.write_text(text, encoding="utf-8")
    print(f"created  {step_file.relative_to(_REPO)}")
    print(f"bumped   ARCHIVE_LAYOUT {frm} -> {to}")
    print()
    print("Next:")
    print(f"  1. implement migrate() in {step_file.relative_to(_REPO)}")
    print(f"  2. test it in tests/common/test_archive_migrations.py (test_every_step_is_tested)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
