#!/usr/bin/env python3
"""Is the recipe an image records still enough to rebuild it?

    python3 tools/check_recipe.py --dockerfile container/robovast/Dockerfile
    python3 tools/check_recipe.py --image ghcr.io/cps-test-lab/robovast:latest

A campaign records what its image was built FROM -- the base by digest, and the dated apt
archives it installed from -- because a digest is reproducible only as long as the registry
keeps it, and a rebuild is what outlives that. That recording is worth exactly as much as the
things it names still being obtainable, and nothing in the build notices when they stop:
a dated archive that has been pruned fails at `apt-get update` inside a rebuild nobody runs
until the year-old campaign someone needs.

So this asks the two questions that can be asked cheaply, and says which one failed.

**Is the recipe complete?** Every input the Dockerfile pins has to reach the image as a label,
or a rebuild is missing a pin and reproduces the shape rather than the software. An *empty*
label is the failure mode worth naming: a forgotten `--build-arg` produces one, and an empty
label looks present to anything only checking the key.

**Are its archives still served?** One request each, against the snapshot the recipe names.

What this does NOT do is rebuild. That is the only thing that proves the recipe *sufficient*,
and it costs a full image build; this is the cheap check that runs often, not a replacement for
the expensive one that runs rarely.
"""

import argparse
import pathlib
import re
import subprocess
import sys
import urllib.error
import urllib.request

_REPO = pathlib.Path(__file__).resolve().parents[1]

#: Label -> the build ARG it comes from, for the Dockerfile-reading mode.
_RECIPE = {
    "org.robovast.base-image": None,          # composed from two ARGs; see _from_dockerfile
    "org.robovast.ubuntu-snapshot": "UBUNTU_SNAPSHOT",
    "org.robovast.ros-snapshot": "ROS_SNAPSHOT",
}

#: Checked only when reading a built IMAGE. The Dockerfile cannot declare it -- CI passes the
#: revision as a build arg -- but an image that does not carry it is missing a recipe input all
#: the same: the Dockerfile is an input to a rebuild, and this is the only record of which one.
#: The base image shipped with this empty for a long time, because that job passed neither the
#: build arg nor metadata-action's labels.
_IMAGE_ONLY = ("org.opencontainers.image.revision",)

#: Ubuntu release the pinned ROS distro sits on. Named here because the Dockerfile reads it from
#: the base image's /etc/os-release at build time, which is not answerable from outside a build.
_CODENAME = "noble"

_TIMEOUT = 20


def _arg_default(text: str, name: str) -> str:
    found = re.search(rf"^ARG {re.escape(name)}=(\S+)", text, re.MULTILINE)
    return found.group(1) if found else ""


def _from_dockerfile(path: pathlib.Path) -> dict:
    text = path.read_text(encoding="utf-8")
    distro = _arg_default(text, "ROS_DISTRO")
    digest = _arg_default(text, "ROS_BASE_DIGEST")
    return {
        "org.robovast.base-image": f"ros:{distro}-ros-base@{digest}" if distro and digest else "",
        "org.robovast.ubuntu-snapshot": _arg_default(text, "UBUNTU_SNAPSHOT"),
        "org.robovast.ros-snapshot": _arg_default(text, "ROS_SNAPSHOT"),
        "_ros_distro": distro,
    }


def _from_image(ref: str) -> dict:
    """Labels as the registry reports them -- the same surface the runtime reads."""
    result = subprocess.run(
        ["docker", "buildx", "imagetools", "inspect", "--format", "{{json .Image}}", ref],
        capture_output=True, text=True, check=False)
    if result.returncode != 0:
        sys.exit(f"could not inspect {ref}: {result.stderr.strip()}")
    import json
    payload = json.loads(result.stdout.strip() or "{}")
    candidates = [payload] if "config" in payload else [
        v for v in payload.values() if isinstance(v, dict)]
    labels: dict = {}
    for candidate in candidates:
        found = ((candidate.get("config") or {}).get("Labels") or {})
        if found:
            labels = found
            break
    base = labels.get("org.robovast.base-image", "")
    distro = ""
    if base.startswith("ros:") and "-ros-base@" in base:
        distro = base[len("ros:"):].split("-ros-base@", 1)[0]
    out = {key: labels.get(key, "") for key in _RECIPE}
    out.update({key: labels.get(key, "") for key in _IMAGE_ONLY})
    out["_ros_distro"] = distro
    return out


def _serves(url: str) -> str:
    """``""`` when *url* answers, else why not."""
    request = urllib.request.Request(url, method="GET")
    try:
        with urllib.request.urlopen(request, timeout=_TIMEOUT) as response:
            if response.status != 200:
                return f"HTTP {response.status}"
            return ""
    except urllib.error.HTTPError as e:
        return f"HTTP {e.code}"
    except Exception as e:  # noqa: BLE001 - any failure to reach it is the answer
        return str(e)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    source = parser.add_mutually_exclusive_group(required=True)
    source.add_argument("--dockerfile", help="read the pins from this Dockerfile's ARG defaults")
    source.add_argument("--image", help="read the recipe off a built image's labels")
    parser.add_argument("--skip-network", action="store_true",
                        help="check completeness only; do not ask whether the archives serve")
    args = parser.parse_args()

    if args.dockerfile:
        recipe = _from_dockerfile(pathlib.Path(args.dockerfile))
        where = args.dockerfile
    else:
        recipe = _from_image(args.image)
        where = args.image

    problems = []
    expected = tuple(_RECIPE) + (_IMAGE_ONLY if args.image else ())
    print(f"Recipe recorded by {where}:")
    for key in expected:
        value = recipe.get(key, "")
        print(f"  {key} = {value or '<empty>'}")
        if not value:
            # Named as empty rather than missing: a forgotten --build-arg produces a label that
            # is present and blank, which a key check would pass.
            problems.append(f"{key} is empty -- a rebuild would be missing this pin")

    distro = recipe.get("_ros_distro") or ""
    ubuntu, ros = (recipe.get("org.robovast.ubuntu-snapshot"),
                   recipe.get("org.robovast.ros-snapshot"))
    if not args.skip_network:
        checks = []
        if ubuntu:
            checks.append((
                "ubuntu snapshot",
                f"https://snapshot.ubuntu.com/ubuntu/{ubuntu}/dists/{_CODENAME}/Release"))
        if ros and distro:
            checks.append((
                "ros snapshot",
                f"http://snapshots.ros.org/{distro}/{ros}/ubuntu/dists/{_CODENAME}/Release"))
        for name, url in checks:
            why = _serves(url)
            print(f"  {name}: {'serves' if not why else 'UNREACHABLE (' + why + ')'}  {url}")
            if why:
                problems.append(
                    f"the {name} this recipe names no longer serves ({why}). Every campaign "
                    f"recorded against it can no longer be rebuilt from its recipe -- only from "
                    f"an image that still exists.")

    if problems:
        for problem in problems:
            print(f"::error::{problem}")
        return 1
    print("Recipe is complete and its archives still serve.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
