#!/usr/bin/env python3
"""Ask whether a change to the host<->container contract bumped ``COMPAT_VERSION``.

    python3 tools/check_compat_version.py [--base origin/main]

The protocol version is a **claim**: it says "an image built before this does not satisfy what
the host now needs". Nothing verifies it, and its own docstring admits as much -- which means
the way it fails is by being forgotten. Somebody changes what the container must provide, the
constant stays where it was, every host keeps believing old images are fine, and the mismatch
surfaces later as a run failing inside the container instead of a refusal before it started.

So this asks the question at the only moment anyone can answer it: in the change itself.

**A prompt, not a proof.** It cannot tell a contract change from a comment edit in the same
file, and it cannot see a contract change made somewhere it is not looking -- host code that
starts assuming a path no image provides would slip straight past. What it does is make
"did this change what the container must provide?" a question that gets *asked*, on the changes
most likely to be one.

That is also why the escape hatch is a commit trailer rather than a flag: the answer belongs in
history next to the change it is about, where the next person reading ``git log`` finds it.

    Compat-Unchanged: moved a comment; nothing the host relies on moved

Only one direction is checked, unlike its sibling ``check_config_version.py``. Bumping the
config version for nothing grows a migration ladder that every campaign then walks. Bumping
COMPAT_VERSION for nothing only widens the window -- ``MIN_IMAGE_COMPAT`` is what drops support,
and raising *that* is the deliberate act. An unnecessary bump costs nothing, so it is not worth
a gate that would then need its own escape hatch.
"""

import argparse
import pathlib
import re
import subprocess
import sys

_REPO = pathlib.Path(__file__).resolve().parents[1]
_CONSTANT_REL = "src/robovast/common/execution.py"

#: Changing any of these plausibly changes what an image must provide, or what the host will
#: ask of it. Deliberately short: every path here is one that can raise a false alarm, and a
#: check that cries wolf is one whose escape hatch becomes reflex.
#:
#: ``docker_exec.sh`` is NOT here even though it drives a container. It runs on the host, and it
#: is where the compat check itself lives -- so it would fire on every change to this mechanism
#: and teach everyone to bypass it.
_CONTRACT_PATHS = (
    "container/robovast/Dockerfile",
    "container/robovast/Dockerfile.roqsim",
    "src/robovast/execution/data/entrypoint.sh",
    "src/robovast/execution/data/secondary_entrypoint.sh",
    "src/robovast/results_processing/data/ros2_exec.sh",
)

_TRAILER = "Compat-Unchanged:"


def _git(*args, allow_fail: bool = False):
    result = subprocess.run(["git", *args], cwd=_REPO, capture_output=True, text=True,
                            check=False)
    if result.returncode != 0:
        if allow_fail:
            return None
        sys.exit(f"git {' '.join(args)} failed: {result.stderr.strip()}")
    return result.stdout


def _compat_version(ref: str | None) -> str | None:
    """``COMPAT_VERSION`` as of *ref*, or of the working tree when *ref* is None."""
    if ref is None:
        text = (_REPO / _CONSTANT_REL).read_text(encoding="utf-8")
    else:
        shown = _git("show", f"{ref}:{_CONSTANT_REL}", allow_fail=True)
        if shown is None:
            return None
        text = shown
    found = re.search(r"^COMPAT_VERSION\s*=\s*(\d+)", text, re.MULTILINE)
    return found.group(1) if found else None


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--base", default="origin/main",
                        help="what to compare against (default: origin/main)")
    args = parser.parse_args()

    merge_base = (_git("merge-base", args.base, "HEAD", allow_fail=True) or "").strip()
    if not merge_base:
        print(f"No merge base with {args.base}; nothing to compare. Skipping.")
        return 0

    changed = [line for line in
               (_git("diff", "--name-only", merge_base, "HEAD", "--", *_CONTRACT_PATHS) or
                "").splitlines() if line.strip()]
    if not changed:
        print("No change to the host<->container contract surface.")
        return 0

    before, after = _compat_version(merge_base), _compat_version(None)
    if before != after:
        print(f"Contract surface changed and COMPAT_VERSION moved {before} -> {after}. Good.")
        return 0

    log = _git("log", f"{merge_base}..HEAD", "--format=%B") or ""
    excused = [line.strip() for line in log.splitlines() if line.strip().startswith(_TRAILER)]
    if excused:
        print("Contract surface changed, COMPAT_VERSION did not, and that is recorded:")
        for line in excused:
            print(f"  {line}")
        return 0

    print("::error::A file defining the host<->container contract changed, but "
          f"COMPAT_VERSION (currently {after}) did not.")
    for path in changed:
        print(f"::error::  changed: {path}")
    print("::error::")
    print("::error::If an image built before this change no longer satisfies what the host "
          "needs, bump COMPAT_VERSION in " + _CONSTANT_REL + ". Widening the window is cheap: "
          "MIN_IMAGE_COMPAT is what drops support for older images, and it is left alone.")
    print("::error::")
    print(f"::error::If nothing the host relies on moved, say so in a commit message:")
    print(f"::error::    {_TRAILER} <why this does not change the contract>")
    return 1


if __name__ == "__main__":
    sys.exit(main())
