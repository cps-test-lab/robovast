#!/usr/bin/env python3
"""Decide whether a ``.vast`` config version bump was warranted -- and refuse either way.

    python3 tools/check_config_version.py [--base origin/main]

Two failure directions, both real:

* the schema broke and the version did **not** move -- old configs silently mean something
  new; and
* the version moved and the schema did **not** break -- the ladder grows for nothing, and
  every campaign gets migrated through a step that does nothing.

The second is the one nobody catches by review, which is why it is checked here rather than
left to judgement. Classification comes from ``compat/config_fields.json``; see
``tools/config_fields.py`` for why that file and not rendered JSON Schema.

A third direction, checked against the working tree alone and so on every run: the number a
*reader* is shown. Every ``version:`` a shipped example or a documentation sample declares
must be the supported one, because strict authoring accepts no other -- a sample left behind
hands the reader a starting point ``vast`` refuses. The docs are checked here rather than by
a substitution in ``docs/conf.py`` because reStructuredText does not expand substitutions
inside a literal block, which is where every one of those samples lives.
"""

import argparse
import json
import pathlib
import re
import subprocess
import sys

_REPO = pathlib.Path(__file__).resolve().parents[1]
_SNAPSHOT_REL = "compat/config_fields.json"
_LADDER_REL = "src/robovast/common/migrations/config/__init__.py"
_FIXTURES_REL = "src/robovast/common/migrations/fixtures"

#: Where a ``version:`` is declared for a reader to copy: the shipped examples, and the
#: YAML samples in the documentation.
_DECLARING_GLOBS = (("configs/examples", "*.vast"), ("docs", "*.rst"))

#: A ``version:`` declaration, anchored to a line of its own so it matches what a reader
#: would copy -- not the word in prose, and not a dotted package version such as
#: ``version: 2.1.0``, which the anchored integer group cannot match.
_DECLARED_RE = re.compile(r"^[ \t]*version:[ \t]*(\d+)[ \t]*$", re.MULTILINE)


#: Annotations that never described a file that could exist, and the reason each is a
#: correction rather than a change. Entries are removed once the release they correct has
#: shipped -- this is not a place to park a breaking change.
#:
#: The third failure direction the two above do not cover: a field whose declared type
#: disagrees with what the ladder that produces it always emitted. Nothing valid can carry
#: the old shape, because the only route to that version is the step itself, so widening or
#: narrowing the annotation to match cannot change what any file means. A version bump here
#: would add a step that migrates nothing, which the README refuses for good reason.
_ANNOTATION_CORRECTIONS = {
    ("SearchConfig", "parameters"):
        "v4's own ladder rewrites search.parameters into channels, so no v4 file has ever "
        "carried the list this was annotated as",
}


def _git(*args, allow_fail: bool = False):
    result = subprocess.run(["git", *args], cwd=_REPO, capture_output=True, text=True,
                            check=False)
    if result.returncode != 0:
        if allow_fail:
            return None
        sys.exit(f"git {' '.join(args)} failed: {result.stderr.strip()}")
    return result.stdout


def _at(ref: str, path: str):
    """A file's contents at *ref*, or None when it did not exist there."""
    return _git("show", f"{ref}:{path}", allow_fail=True)


def _supported_version(text: str):
    match = re.search(r"^SUPPORTED_CONFIG_VERSION\s*=\s*(\d+)", text or "", re.MULTILINE)
    return int(match.group(1)) if match else None


def _flatten(snapshot: dict) -> dict:
    """``{(model, field): (required, type)}`` so a diff is one set operation."""
    return {(model, entry[0]): (entry[1], entry[2])
            for model, entries in (snapshot or {}).items() for entry in entries}


def classify(base: dict, head: dict) -> "tuple[bool, list[str], list[str]]":
    """Return ``(breaking, breaking_reasons, additive_notes)``."""
    old, new = _flatten(base), _flatten(head)
    breaking, additive = [], []

    for key in sorted(set(old) - set(new)):
        breaking.append(f"removed {key[0]}.{key[1]}")
    # A model the base snapshot did not have at all is new, and every field on it is
    # additive however required it is: no existing config can contain a block of a shape
    # that did not exist, so nothing old can fail to carry the field. Only a required field
    # added to a model that was already there rejects configs that used to load. Without
    # this, introducing any nested block with required keys (a container's `ros_packages`
    # entry, say) demanded a version bump and a migration step that would migrate nothing.
    new_models = set(head or {}) - set(base or {})
    for key in sorted(set(new) - set(old)):
        required, _type = new[key]
        if key[1] == "<extra>":
            continue
        breaks = required and key[0] not in new_models
        (breaking if breaks else additive).append(
            f"added {'required' if required else 'optional'} {key[0]}.{key[1]}"
            + (" (on a new model)" if required and not breaks else ""))
    for key in sorted(set(old) & set(new)):
        was_required, was_type = old[key]
        is_required, is_type = new[key]
        if key[1] == "<extra>":
            # allow -> forbid rejects configs that used to load; the reverse cannot.
            if (was_type, is_type) == ("allow", "forbid"):
                breaking.append(f"{key[0]} now forbids extra keys")
            elif was_type != is_type:
                # `ignore -> forbid` also rejects a file that used to load, and is
                # classified additive on purpose: an ignored key never reached the model, so
                # such a file was already not doing what it reads, and the bump test is
                # whether a *valid* config stops meaning what it meant. The refusal is the
                # point of the change rather than a casualty of it. Reading an archived
                # campaign is kept working by `validate_config(strict=False)`, not by a
                # version step -- a migration cannot help there, because the key it would
                # have to drop is one no schema ever described.
                additive.append(f"{key[0]} extra: {was_type} -> {is_type}")
            continue
        if was_type != is_type:
            if key in _ANNOTATION_CORRECTIONS:
                additive.append(
                    f"{key[0]}.{key[1]} type {was_type} -> {is_type} "
                    f"(correction: {_ANNOTATION_CORRECTIONS[key]})")
            else:
                breaking.append(f"{key[0]}.{key[1]} type {was_type} -> {is_type}")
        if is_required and not was_required:
            breaking.append(f"{key[0]}.{key[1]} became required")
        elif was_required and not is_required:
            additive.append(f"{key[0]}.{key[1]} became optional")
    return bool(breaking), breaking, additive


def _fixture_changes(base: str) -> "tuple[list[str], list[str]]":
    """``(added, touched)`` golden fixture paths between *base* and the working tree."""
    out = _git("diff", "--name-status", base, "--", _FIXTURES_REL) or ""
    added, touched = [], []
    for line in out.splitlines():
        parts = line.split("\t")
        if len(parts) < 2:
            continue
        status, path = parts[0], parts[-1]
        if status == "A":
            added.append(path)
        else:
            touched.append(f"{status} {path}")
    return added, touched


def _stale_declarations(version: int) -> list:
    """One failure naming every shipped ``version:`` that is not *version*, or ``[]``."""
    stale = []
    for directory, pattern in _DECLARING_GLOBS:
        for path in sorted((_REPO / directory).rglob(pattern)):
            if "_build" in path.parts:
                continue
            text = path.read_text(encoding="utf-8")
            for match in _DECLARED_RE.finditer(text):
                if int(match.group(1)) != version:
                    line = text.count("\n", 0, match.start()) + 1
                    stale.append(f"{path.relative_to(_REPO)}:{line} declares "
                                 f"version {match.group(1)}")
    if not stale:
        return []
    return [f"SUPPORTED_CONFIG_VERSION is {version}, but a shipped file declares another "
            f"version:\n    "
            + "\n    ".join(stale)
            + "\n  Authoring accepts the supported version only, so each of these is a "
              "starting point that would be refused."]


def _report(failures: list) -> int:
    """Print every failure at once and return the exit code."""
    if not failures:
        return 0
    print("config version check FAILED\n", file=sys.stderr)
    for failure in failures:
        print(f"  - {failure}\n", file=sys.stderr)
    return 1


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--base", default="origin/main",
                        help="Base ref to compare against (default: origin/main)")
    args = parser.parse_args()

    head_ladder = (_REPO / _LADDER_REL).read_text(encoding="utf-8")
    head_version = _supported_version(head_ladder)
    if head_version is None:
        sys.exit(f"could not read SUPPORTED_CONFIG_VERSION from {_LADDER_REL}")

    # Reads the working tree only, so it holds however this run ends: the delta checks below
    # need a base ref and a landed snapshot, and both of those are skippable.
    failures = _stale_declarations(head_version)

    if _git("rev-parse", "--verify", args.base, allow_fail=True) is None:
        print(f"base ref {args.base!r} not found; skipping the delta check (nothing to "
              f"compare against)")
        return _report(failures)

    base_version = _supported_version(_at(args.base, _LADDER_REL))
    if base_version is None:
        print(f"{_LADDER_REL} is new on this branch; skipping the delta check")
        return _report(failures)

    delta = head_version - base_version
    base_raw = _at(args.base, _SNAPSHOT_REL)
    if base_raw is None:
        # The snapshot is being introduced on this branch. Classifying against an empty
        # baseline reads every existing field as newly added and reports a breaking change
        # that is not one, so there is genuinely nothing to compare and saying so is the
        # only honest answer.
        print(f"{_SNAPSHOT_REL} does not exist at {args.base}; skipping the schema "
              f"classification (nothing to compare against)")
        if delta != 0:
            failures.append(
                f"SUPPORTED_CONFIG_VERSION moved {base_version} -> {head_version} in the "
                f"same change that introduces {_SNAPSHOT_REL}. Land the snapshot first, so "
                f"the bump can be classified.")
        return _report(failures)
    base_snapshot = json.loads(base_raw)
    head_path = _REPO / _SNAPSHOT_REL
    if not head_path.exists():
        failures.append(f"{_SNAPSHOT_REL} is missing. Run: make config-fields")
        return _report(failures)
    head_snapshot = json.loads(head_path.read_text(encoding="utf-8"))

    breaking, reasons, additive = classify(base_snapshot, head_snapshot)
    added_goldens, touched_goldens = _fixture_changes(args.base)

    if breaking and delta == 0:
        failures.append(
            "the config schema changed in a way old configs cannot survive, but "
            f"SUPPORTED_CONFIG_VERSION is still {head_version}:\n    "
            + "\n    ".join(reasons)
            + "\n  Add a migration step: python3 tools/new_config_migration.py")
    if not breaking and delta > 0:
        detail = "\n    ".join(additive) if additive else "no schema change at all"
        failures.append(
            f"SUPPORTED_CONFIG_VERSION moved {base_version} -> {head_version}, but nothing "
            f"in the schema requires it:\n    " + detail
            + "\n  An added optional field never needs a bump, and a rename can usually use "
              "pydantic AliasChoices instead. See migrations/README.md.")
    if delta < 0:
        failures.append(f"SUPPORTED_CONFIG_VERSION went backwards: "
                        f"{base_version} -> {head_version}")
    if delta > 0 and len(added_goldens) != delta:
        failures.append(
            f"{delta} version step(s) added but {len(added_goldens)} golden fixture(s) "
            f"added. Each step needs exactly one: "
            f"python3 tools/new_config_migration.py --regenerate <from-version>")
    if touched_goldens:
        failures.append(
            "an existing golden fixture was modified or deleted:\n    "
            + "\n    ".join(touched_goldens)
            + "\n  Goldens are append-only -- editing one changes what an already-migrated "
              "campaign becomes.")

    if failures:
        return _report(failures)

    summary = "breaking" if breaking else ("additive" if additive else "unchanged")
    print(f"config version check OK: schema {summary}, "
          f"SUPPORTED_CONFIG_VERSION {base_version} -> {head_version}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
