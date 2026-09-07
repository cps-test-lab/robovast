#!/usr/bin/env python3
"""Generate or verify the committed list of authored example files.

    python3 tools/examples_manifest.py --write   # regenerate  (make examples-manifest)
    python3 tools/examples_manifest.py --check   # verify      (make check-examples-manifest)

``get_example`` exposes authored files and never generated ones, and in a checkout it
asks git which is which. A container image has no checkout: the source is copied in and
``.git`` is not, so git can answer nothing there and the tool has to be told. This file
is that answer -- the same list, committed, so it travels with the source into any build
without a build step to forget.

It is a snapshot of ``git ls-files``, so it can drift; ``--check`` is the guard, and runs
in CI beside the other drift guards. The manifest lists itself out: it is a file about
the examples, not one of them.
"""

import argparse
import pathlib
import subprocess
import sys

_REPO = pathlib.Path(__file__).resolve().parents[1]
_EXAMPLES = _REPO / "configs" / "examples"
_MANIFEST = _EXAMPLES / "MANIFEST"

_HEADER = (
    "# Authored files under configs/examples/, for builds that carry the source without\n"
    "# a git checkout. Generated -- run `make examples-manifest` after adding, removing\n"
    "# or renaming an example file.\n"
)


def manifest() -> str:
    """The manifest as it should be on disk, from the git index."""
    out = subprocess.run(
        ["git", "ls-files", "-z"],
        cwd=_EXAMPLES, capture_output=True, text=True, check=True,
    ).stdout
    names = sorted(p for p in out.split("\0") if p and p != _MANIFEST.name)
    return _HEADER + "".join(f"{n}\n" for n in names)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--write", action="store_true", help="regenerate the manifest")
    group.add_argument("--check", action="store_true",
                       help="fail if the manifest disagrees with the git index")
    args = parser.parse_args()

    current = manifest()

    if args.write:
        _MANIFEST.write_text(current, encoding="utf-8")
        print(f"wrote {_MANIFEST.relative_to(_REPO)}")
        return 0

    if not _MANIFEST.exists():
        print(f"{_MANIFEST.relative_to(_REPO)} is missing. Run: make examples-manifest",
              file=sys.stderr)
        return 1
    if _MANIFEST.read_text(encoding="utf-8") != current:
        print(f"{_MANIFEST.relative_to(_REPO)} is out of date with the git index.\n"
              f"Run: make examples-manifest", file=sys.stderr)
        return 1
    print(f"{_MANIFEST.relative_to(_REPO)} matches the git index")
    return 0


if __name__ == "__main__":
    sys.exit(main())
