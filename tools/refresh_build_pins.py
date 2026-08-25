#!/usr/bin/env python3
"""Re-resolve the pins an image build starts from, and leave a reviewable diff.

Pins that never move rot into an unbuildable state; pins that move automatically defeat the point.
So this is deliberately manual and deliberately occasional -- the model of ``poetry.lock`` or
``cargo update``. CI builds only from what is committed.

    python3 tools/refresh_build_pins.py            # report what would change
    python3 tools/refresh_build_pins.py --ask      # report, then offer to apply it
    python3 tools/refresh_build_pins.py --write    # rewrite the Dockerfile ARGs, no question asked

``--ask`` is what the Makefile uses; see ``tools/pin_prompt.py`` for why the question comes after
the report and what happens when there is no terminal to ask on.

What it resolves:

``FROM ... @sha256:...``
    every digest-pinned base image, re-resolved from its tag with ``buildx imagetools inspect``
    (no pull). A tag is kept beside each digest precisely so this can find it again.
``ARG UBUNTU_SNAPSHOT`` / ``ARG ROS_SNAPSHOT``
    the dated apt archives every ``apt-get`` in the image resolves against. The ROS date is
    **discovered, not computed**: ROS publishes a fixed set, roughly quarterly, and asking for an
    arbitrary date returns 404 rather than the nearest, so a computed "today minus a month" would
    fail at build time for a reason the Dockerfile does not explain. Ubuntu's service is the
    opposite -- any timestamp since 2023-03-01 -- so that one is derived from the ROS date, keeping
    both archives at the same point in time rather than three months apart. Moving these changes
    what a rebuild installs, so it also has to change the image cache key.

See ``container/pins/README.md`` for the recipe and the four things that only showed up by running
it.
"""

import argparse
import pathlib
import re
import subprocess
import sys
import urllib.request

import pin_prompt

_REPO = pathlib.Path(__file__).resolve().parents[1]
_DOCKERFILES = ("container/robovast/Dockerfile", "container/robovast/Dockerfile.roqsim",
                "container/controller/Dockerfile", "container/sidecar/Dockerfile")

_ROS_SNAPSHOTS = "http://snapshots.ros.org/{distro}/"
_FROM_PIN = re.compile(r"^FROM\s+(?P<ref>[^\s@]+)@(?P<digest>sha256:[0-9a-f]{64})(?P<rest>.*)$",
                       re.MULTILINE)
_ARG_PIN = re.compile(r"^ARG\s+(?P<name>ROS_BASE_DIGEST)=(?P<digest>sha256:[0-9a-f]{64})",
                      re.MULTILINE)
_UBUNTU_SNAPSHOT_PIN = re.compile(r"^ARG\s+UBUNTU_SNAPSHOT=(?P<stamp>\S+)", re.MULTILINE)
_ROS_SNAPSHOT_PIN = re.compile(r"^ARG\s+ROS_SNAPSHOT=(?P<date>\S+)", re.MULTILINE)
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


def _base_created(ref: str) -> "str | None":
    """The base image's creation timestamp as a snapshot stamp, read without pulling it."""
    try:
        result = subprocess.run(
            ["docker", "buildx", "imagetools", "inspect", "--format", "{{.Image.Created}}", ref],
            capture_output=True, text=True, check=False, timeout=_TIMEOUT)
    except (FileNotFoundError, subprocess.TimeoutExpired):
        return None
    if result.returncode != 0:
        return None
    # "2026-08-19 00:30:43.069541076 +0000 UTC" -> "20260819T003043Z"
    match = re.match(r"(\d{4})-(\d{2})-(\d{2})[ T](\d{2}):(\d{2}):(\d{2})", result.stdout.strip())
    return "".join(match.groups()[:3]) + "T" + "".join(match.groups()[3:]) + "Z" if match else None


def _ubuntu_stamp(base_ref: str) -> "str | None":
    """The Ubuntu snapshot timestamp to pin, which is the BASE IMAGE's date -- not the ROS date.

    Deriving it from the ROS date is the obvious thing and it is wrong. ``ros`` is rebuilt
    daily, so its Ubuntu packages are current, while the newest ROS snapshot can be months old;
    an older Ubuntu archive then offers ``udev 255.4-1ubuntu8.16`` against a base already carrying
    ``libudev1 8.17``, and every install that pulls udev in fails with "held broken packages".

    The constraint is one-sided: the Ubuntu snapshot must be at or after the base's own apt state.
    So it is read from the base image itself, which also means refreshing the base digest and this
    stamp cannot drift apart.
    """
    return _base_created(base_ref)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    pin_prompt.add_arguments(parser)
    args = parser.parse_args()

    changes, unresolved = [], []
    # path -> rewritten text, held back until the decision at the end: with --ask the report has to
    # be printed before anything is written, so collecting and applying are two steps.
    edits = {}

    # Resolved once, before the loop: every Dockerfile that pins a snapshot must land on the SAME
    # point in time, and asking the ROS server once per file could straddle a publication.
    distro = _ros_distro((_REPO / _DOCKERFILES[0]).read_text(encoding="utf-8"))
    ros_date = _latest_ros_snapshot(distro)
    if ros_date:
        print(f"newest {distro} snapshot offered by snapshots.ros.org: {ros_date}")
    else:
        unresolved.append(f"could not list snapshots for {distro}")
    ubuntu_stamp = _ubuntu_stamp(f"ros:{distro}-ros-base")
    if ubuntu_stamp:
        print(f"base image's own apt state (the Ubuntu pin):         {ubuntu_stamp}")
    else:
        unresolved.append(f"could not read the creation date of ros:{distro}-ros-base")

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
            # `ros`, not `osrf/ros`: only the former publishes a multi-arch index, and
            # resolving this pin to a single-platform manifest is what silently produced an
            # arm64 image full of amd64 userland. See the FROM in the Dockerfile.
            ref = f"ros:{distro}-ros-base"
            new = _resolve_digest(ref)
            if new is None:
                unresolved.append(f"{rel}: could not reach {ref}")
            elif new != match.group("digest"):
                changes.append(f"{rel}: {ref} (ROS_BASE_DIGEST)\n"
                               f"    {match.group('digest')}\n -> {new}")
                text = text.replace(f"ARG ROS_BASE_DIGEST={match.group('digest')}",
                                    f"ARG ROS_BASE_DIGEST={new}")

        for pattern, group, wanted, name in (
                (_ROS_SNAPSHOT_PIN, "date", ros_date, "ROS_SNAPSHOT"),
                (_UBUNTU_SNAPSHOT_PIN, "stamp", ubuntu_stamp, "UBUNTU_SNAPSHOT")):
            if wanted:
                match = pattern.search(original)
                if match is None or match.group(group) == wanted:
                    continue
                changes.append(f"{rel}: {name}\n    {match.group(group)}\n -> {wanted}")
                text = text.replace(f"ARG {name}={match.group(group)}", f"ARG {name}={wanted}")

        if text != original:
            edits[path] = text

    for change in changes:
        print(change)
    for problem in unresolved:
        print(f"unresolved: {problem}", file=sys.stderr)

    if not changes:
        print("every pin is already current")
    elif pin_prompt.apply_or_ask(edits, len(changes), args):
        print(f"\nrewrote {len(changes)} pin(s). Review the diff, then rebuild and run "
              f"configs/examples/camera_smoke to prove the new pin set works.")
    # Unresolved pins are reported but do not fail: a registry that is briefly unreachable is not a
    # reason to treat the committed pins as wrong.
    return 0


if __name__ == "__main__":
    sys.exit(main())
