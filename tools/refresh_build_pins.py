#!/usr/bin/env python3
"""Re-resolve the pins an image build starts from, and leave a reviewable diff.

Pins that never move rot into an unbuildable state; pins that move automatically defeat the point.
So this is deliberately manual and deliberately occasional -- the model of ``poetry.lock`` or
``cargo update``. CI builds only from what is committed.

    python3 tools/refresh_build_pins.py            # report what would change
    python3 tools/refresh_build_pins.py --write    # rewrite the Dockerfile ARGs

What it resolves:

``FROM ... @sha256:...``
    every digest-pinned base image, re-resolved from its tag with ``buildx imagetools inspect``
    (no pull). A tag is kept beside each digest precisely so this can find it again.
the snapshot dates
    **Reported, not rewritten** -- the Dockerfiles do not pin apt to a dated archive yet, so there
    is no ARG to update; what this prints is the date to use when they do. The ROS date is
    **discovered, not computed**: ROS publishes a fixed set, roughly quarterly, and asking for an
    arbitrary date returns 404 rather than the nearest, so a computed "today minus a month" would
    fail at build time for a reason the Dockerfile does not explain. Ubuntu's service is the
    opposite -- any timestamp since 2023-03-01 -- so that one is derived from the ROS date, keeping
    both archives at the same point in time rather than three months apart.

See ``container/pins/README.md`` for the recipe and the four things that only showed up by running
it.
"""

import argparse
import pathlib
import re
import subprocess
import sys
import urllib.request

_REPO = pathlib.Path(__file__).resolve().parents[1]
_DOCKERFILES = ("container/robovast/Dockerfile", "container/robovast/Dockerfile.roqsim",
                "container/controller/Dockerfile", "container/sidecar/Dockerfile")

_ROS_SNAPSHOTS = "http://snapshots.ros.org/{distro}/"
_FROM_PIN = re.compile(r"^FROM\s+(?P<ref>[^\s@]+)@(?P<digest>sha256:[0-9a-f]{64})(?P<rest>.*)$",
                       re.MULTILINE)
_ARG_PIN = re.compile(r"^ARG\s+(?P<name>ROS_BASE_DIGEST)=(?P<digest>sha256:[0-9a-f]{64})",
                      re.MULTILINE)
_TIMEOUT = 60


def _resolve_digest(ref: str) -> "str | None":
    """The current digest for *ref*, without pulling it.

    ``buildx imagetools inspect`` reads the registry manifest directly -- the same call
    ``container/image_digests.sh`` uses, and the reason a tag is worth keeping beside a digest.
    """
    try:
        result = subprocess.run(
            ["docker", "buildx", "imagetools", "inspect", "--format",
             "{{.Manifest.Digest}}", ref],
            capture_output=True, text=True, check=False, timeout=_TIMEOUT)
    except (FileNotFoundError, subprocess.TimeoutExpired):
        return None
    digest = result.stdout.strip()
    return digest if result.returncode == 0 and digest.startswith("sha256:") else None


def _latest_ros_snapshot(distro: str) -> "str | None":
    """The newest dated snapshot the ROS server actually lists for *distro*.

    Discovered rather than computed: the dates are a fixed, roughly quarterly set, and a date the
    server does not have is a 404, not the nearest one. Computing "today minus a month" would
    produce a pin that fails at build time for a reason the Dockerfile does not explain.
    """
    try:
        with urllib.request.urlopen(_ROS_SNAPSHOTS.format(distro=distro),
                                    timeout=_TIMEOUT) as response:
            body = response.read().decode("utf-8", "replace")
    except Exception:  # pylint: disable=broad-except
        return None
    dates = sorted(set(re.findall(r"\d{4}-\d{2}-\d{2}", body)))
    return dates[-1] if dates else None


def _ros_distro(text: str) -> str:
    match = re.search(r"^ARG\s+ROS_DISTRO=(\S+)", text, re.MULTILINE)
    return match.group(1) if match else "jazzy"


def _ubuntu_stamp(ros_date: str) -> str:
    """The Ubuntu snapshot timestamp matching a ROS snapshot date.

    Derived from the ROS date rather than from today, so the two archives are read at the same
    point in time. Pinning Ubuntu to now and ROS to a three-month-old snapshot would install a
    combination nobody ever tested.
    """
    return ros_date.replace("-", "") + "T000000Z"


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--write", action="store_true",
                        help="Rewrite the pins. Without it, only report.")
    args = parser.parse_args()

    changes, unresolved = [], []
    for rel in _DOCKERFILES:
        path = _REPO / rel
        if not path.exists():
            continue
        text = original = path.read_text(encoding="utf-8")

        for match in _FROM_PIN.finditer(original):
            ref, old = match.group("ref"), match.group("digest")
            # ${VAR} in a ref cannot be resolved from here; the ARG form below covers that case.
            if "${" in ref:
                continue
            new = _resolve_digest(ref)
            if new is None:
                unresolved.append(f"{rel}: could not reach {ref}")
            elif new != old:
                changes.append(f"{rel}: {ref}\n    {old}\n -> {new}")
                text = text.replace(f"{ref}@{old}", f"{ref}@{new}")

        distro = _ros_distro(original)
        for match in _ARG_PIN.finditer(original):
            ref = f"osrf/ros:{distro}-desktop-full"
            new = _resolve_digest(ref)
            if new is None:
                unresolved.append(f"{rel}: could not reach {ref}")
            elif new != match.group("digest"):
                changes.append(f"{rel}: {ref} (ROS_BASE_DIGEST)\n"
                               f"    {match.group('digest')}\n -> {new}")
                text = text.replace(f"ARG ROS_BASE_DIGEST={match.group('digest')}",
                                    f"ARG ROS_BASE_DIGEST={new}")

        if args.write and text != original:
            path.write_text(text, encoding="utf-8")

    distro = _ros_distro((_REPO / _DOCKERFILES[0]).read_text(encoding="utf-8"))
    ros_date = _latest_ros_snapshot(distro)
    if ros_date:
        print(f"newest {distro} snapshot offered by snapshots.ros.org: {ros_date}")
        print(f"  matching Ubuntu stamp:                              {_ubuntu_stamp(ros_date)}")
    else:
        unresolved.append(f"could not list snapshots for {distro}")

    for change in changes:
        print(change)
    for problem in unresolved:
        print(f"unresolved: {problem}", file=sys.stderr)

    if not changes:
        print("every base digest is already current")
    elif not args.write:
        print(f"\n{len(changes)} pin(s) would change. Nothing written -- add --write.")
    else:
        print(f"\nrewrote {len(changes)} pin(s). Review the diff, then rebuild and run "
              f"configs/examples/camera_smoke to prove the new pin set works.")
    # Unresolved pins are reported but do not fail: a registry that is briefly unreachable is not a
    # reason to treat the committed pins as wrong.
    return 0


if __name__ == "__main__":
    sys.exit(main())
