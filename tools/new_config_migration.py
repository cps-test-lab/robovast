#!/usr/bin/env python3
"""Scaffold a ``.vast`` config migration step.

Writes the step module, wires it into the ladder, bumps the supported version, and seeds
the golden -- the several places a step otherwise has to touch by hand, which is where one
gets forgotten.

    python3 tools/new_config_migration.py                 # scaffold the next step
    python3 tools/new_config_migration.py --regenerate N  # golden for the N->N+1 step

Then implement the transform in the generated ``vN_to_vM.py``. See
``src/robovast/common/migrations/README.md`` for the rules a step must follow, and for when
a version bump is warranted at all -- this script scaffolds, it does not judge that.
"""

import argparse
import pathlib
import sys

_REPO = pathlib.Path(__file__).resolve().parents[1]
_MIGRATIONS = _REPO / "src" / "robovast" / "common" / "migrations"
_LADDER = _MIGRATIONS / "config" / "__init__.py"
_FIXTURES = _MIGRATIONS / "fixtures"

#: Insertion points the ladder module declares for this script. Markers rather than
#: pattern-matching on code, so a reformat of the ladder cannot silently break scaffolding.
_IMPORT_MARKER = "# <new-migration-import>"
_ENTRY_MARKER = "# <new-migration-entry>"

_STEP_TEMPLATE = '''"""Config version {frm} -> {to}: <one line saying what changed>.

<Why this needed a version bump: which key was removed, renamed, or given new meaning. An
added *optional* key does not need one -- see migrations/README.md.>

**Pure ``dict`` -> ``dict``.** Nothing here may import :mod:`robovast.common.config`;
``test_migration_purity`` enforces it. Deep-copy the input and mutate the copy -- never
``dict(raw)``, which drops a ruamel ``CommentedMap``'s comments.
"""

import copy


def migrate(raw: dict) -> dict:
    """Return *raw* restructured as a version {to} config. Does not mutate the input."""
    out = copy.deepcopy(raw)

    # TODO: the transform.
    #
    # If something cannot be carried forward, raise UnmigratableConfig naming the
    # capability and the last version that supported it -- never drop the key. A config
    # that loads cleanly but runs a *different* experiment is the worst outcome available.

    out["version"] = {to}
    return out
'''

_GOLDEN_PLACEHOLDER = '''# PLACEHOLDER -- not yet produced by the step. Regenerate once migrate() is implemented:
#   python3 tools/new_config_migration.py --regenerate {frm}
# Left failing on purpose: a golden that looks authoritative but was never produced by the
# step would pin the wrong behaviour forever.
'''


def _ladder():
    """Import the ladder module fresh from the working tree."""
    if str(_REPO / "src") not in sys.path:
        sys.path.insert(0, str(_REPO / "src"))
    from robovast.common.migrations import config  # pylint: disable=import-outside-toplevel
    return config


def _wire_into_ladder(frm: int, to: int) -> None:
    """Add the import, the ``_MIGRATIONS`` entry, and bump the supported version."""
    text = _LADDER.read_text(encoding="utf-8")
    module = f"v{frm}_to_v{to}"

    for marker in (_IMPORT_MARKER, _ENTRY_MARKER):
        if marker not in text:
            sys.exit(f"marker {marker} missing from {_LADDER.relative_to(_REPO)} -- wire "
                     f"the step in by hand, then restore the marker.")

    # Each marker is a comment on its own line. Replacing it with "<new line>\n<marker>"
    # inserts above it and carries the marker forward for the next step.
    text = text.replace(
        _IMPORT_MARKER,
        f"from . import {module}  # noqa: F401\n{_IMPORT_MARKER}", 1)
    text = text.replace(_ENTRY_MARKER, f"{module}.migrate,\n    {_ENTRY_MARKER}", 1)

    old_supported = f"SUPPORTED_CONFIG_VERSION = {frm}"
    if old_supported not in text:
        sys.exit(f"could not find '{old_supported}' in {_LADDER.relative_to(_REPO)}")
    text = text.replace(old_supported, f"SUPPORTED_CONFIG_VERSION = {to}", 1)

    _LADDER.write_text(text, encoding="utf-8")


def _regenerate(frm: int) -> None:
    """Write the golden for the *frm* -> *frm*+1 step from its actual output."""
    import yaml  # pylint: disable=import-outside-toplevel

    ladder = _ladder()
    to = frm + 1
    source = (_FIXTURES / f"v{frm}.vast" if frm == ladder.BASELINE_CONFIG_VERSION
              else _FIXTURES / f"v{frm - 1}_to_v{frm}.expected.vast")
    if not source.exists():
        sys.exit(f"missing ladder input {source.relative_to(_REPO)}")
    index = frm - ladder.BASELINE_CONFIG_VERSION
    steps = ladder._MIGRATIONS  # pylint: disable=protected-access
    if not 0 <= index < len(steps):
        sys.exit(f"no step for version {frm} -> {to}")

    raw = list(yaml.safe_load_all(source.read_text(encoding="utf-8")))[0]
    golden = _FIXTURES / f"v{frm}_to_v{to}.expected.vast"
    golden.write_text(
        "# GENERATED GOLDEN -- do not hand-edit.\n"
        f"# The exact output of migrations/config/v{frm}_to_v{to}.py applied to {source.name}.\n"
        "# Regenerate ONLY while implementing that step; editing it afterwards means\n"
        "# editing history, which is what test_step_golden exists to refuse.\n"
        + yaml.safe_dump(steps[index](raw), sort_keys=False, default_flow_style=False),
        encoding="utf-8")
    print(f"regenerated {golden.relative_to(_REPO)}")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--regenerate", type=int, metavar="FROM",
                        help="Regenerate the golden for the FROM->FROM+1 step, after "
                             "implementing its transform.")
    args = parser.parse_args()

    if args.regenerate is not None:
        _regenerate(args.regenerate)
        return 0

    frm = _ladder().SUPPORTED_CONFIG_VERSION
    to = frm + 1
    step_file = _MIGRATIONS / "config" / f"v{frm}_to_v{to}.py"
    if step_file.exists():
        sys.exit(f"{step_file.relative_to(_REPO)} already exists")

    step_file.write_text(_STEP_TEMPLATE.format(frm=frm, to=to), encoding="utf-8")
    _wire_into_ladder(frm, to)
    golden = _FIXTURES / f"v{frm}_to_v{to}.expected.vast"
    golden.write_text(_GOLDEN_PLACEHOLDER.format(frm=frm), encoding="utf-8")

    print(f"created  {step_file.relative_to(_REPO)}")
    print(f"created  {golden.relative_to(_REPO)}   (placeholder -- tests fail until regenerated)")
    print(f"bumped   SUPPORTED_CONFIG_VERSION {frm} -> {to}")
    print()
    print("Next:")
    print(f"  1. implement migrate() in {step_file.relative_to(_REPO)}")
    print(f"  2. python3 tools/new_config_migration.py --regenerate {frm}")
    print("  3. make config-fields    # the snapshot this bump is checked against")
    print("  4. pytest tests/common/test_config_migrations.py")
    return 0


if __name__ == "__main__":
    sys.exit(main())
