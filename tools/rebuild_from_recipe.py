#!/usr/bin/env python3
"""Rebuild an image from the recipe it recorded, and check the software matches.

    python3 tools/rebuild_from_recipe.py --image <ref>       # rebuild it from its own recipe
    python3 tools/rebuild_from_recipe.py --image <ref> --plan-only

A campaign records what its image was built FROM -- the base by digest, the dated apt archives
it installed from, the source commits -- because a digest is reproducible only while the
registry keeps the manifest, and a rebuild is what outlives that.

That recording is a **claim**: that those pins are the complete set of inputs. Nothing tested
it. ``tools/check_recipe.py`` asks two weaker questions -- are the pins all present, do the
archives still answer -- and neither can tell whether something reaches the network unpinned.
Only rebuilding does, which is why this exists and why it is not cheap.

**Why the lock and not the digest.** Docker builds are not bit-reproducible: timestamps and
layer ordering shift, so a perfect rebuild still has a different digest. The build lock is the
surface that matters, because it is the *software* -- ``dpkg-query`` output, ``pip freeze``, and
the commit each floating git ref resolved to -- and software is what an experiment depends on.

**Self-check.** A published image carries both halves: the recipe in its labels and the lock at
``/etc/robovast/build-manifest/``. So it can be rebuilt from its own recipe and compared with
itself, needing no archived campaign as a fixture, and testing exactly the property claimed.

What it cannot do is prove sufficiency forever -- only at the moment it runs. It is a rot
detector on a longer timescale than ``check_recipe``, not a standing guarantee.
"""

import argparse
import json
import pathlib
import subprocess
import sys

#: The checkout to build. The **working directory**, not this file's location: the caller may
#: copy this script out of the tree it is checking -- the rebuild workflow does exactly that, so
#: that a fix to the checker is not itself pinned to the revision being checked -- and where the
#: script happens to sit then says nothing about which checkout to build.
_REPO = pathlib.Path.cwd()

#: Recipe label -> the build ARG it was baked from. The values are what a rebuild must be given
#: back; a pin recorded but not passed reproduces the shape and not the software.
RECIPE_ARGS = {
    "org.robovast.ubuntu-snapshot": "UBUNTU_SNAPSHOT",
    "org.robovast.ros-snapshot": "ROS_SNAPSHOT",
    "org.robovast.scenario-execution-ref": "SCENARIO_EXECUTION_REF",
    "org.robovast.scenario-execution-server-ref": "SCENARIO_EXECUTION_SERVER_REF",
}

_BASE_LABEL = "org.robovast.base-image"
_REVISION_LABEL = "org.opencontainers.image.revision"
_MANIFEST_DIR = "/etc/robovast/build-manifest"
_MANIFEST_FILES = ("apt.txt", "pip.txt", "vcs.txt")
_SEPARATORS = {"apt": "=", "pip": "==", "vcs": "->"}

#: How long a cold rebuild may take before it is called a failure. Generous, because that is
#: what it is: no build cache is used (a cached rebuild would reuse the layers being checked
#: and confirm nothing), so this installs the whole stack from the dated archives every time.
BUILD_TIMEOUT_S = 150 * 60


def parse_lock(kind: str, text: str) -> dict:
    """One manifest file as ``{name: version}``. Mirrors ``image_build.\\_parse_manifest``."""
    separator = _SEPARATORS[kind]
    out = {}
    for line in (text or "").splitlines():
        line = line.strip()
        if not line or separator not in line:
            continue
        left, _, right = line.partition(separator)
        out[left.strip()] = right.strip()
    return out


def diff_locks(recorded: dict, rebuilt: dict) -> list:
    """Every way the rebuilt software differs from what was recorded.

    Three kinds, kept apart because they mean different things:

    * **changed** -- the rebuild resolved a different version. The recipe named an input that
      is not actually pinned, which is the failure this whole check exists to find.
    * **missing** -- recorded then, absent now. A package that has left the archive, or a
      dependency that stopped being pulled in.
    * **added** -- absent then, present now. Usually the same cause seen from the other side.

    Returns an empty list when the rebuild reproduces the recording, which is the claim.
    """
    out = []
    for kind in sorted(set(recorded) | set(rebuilt)):
        was, now = recorded.get(kind) or {}, rebuilt.get(kind) or {}
        for name in sorted(set(was) | set(now)):
            before, after = was.get(name), now.get(name)
            if before == after:
                continue
            if before is None:
                out.append({"kind": kind, "name": name, "how": "added", "now": after})
            elif after is None:
                out.append({"kind": kind, "name": name, "how": "missing", "was": before})
            else:
                out.append({"kind": kind, "name": name, "how": "changed",
                            "was": before, "now": after})
    return out


def build_command(recipe: dict, *, tag: str, dockerfile: str, context: str) -> list:
    """The exact ``docker buildx build`` a rebuild from *recipe* is.

    Every recorded pin becomes the ARG it came from. The base is not an ARG substitution but
    part of the ``FROM``, so it is passed as ``ROS_BASE_DIGEST`` plus the distro the ref names.
    """
    args = []
    base = recipe.get(_BASE_LABEL, "")
    if "@" in base:
        repo, _, digest = base.partition("@")
        args += ["--build-arg", f"ROS_BASE_DIGEST={digest}"]
        # `ros:<distro>-ros-base` -- the distro is what the FROM interpolates beside the digest.
        name = repo.split(":", 1)[1] if ":" in repo else ""
        if name.endswith("-ros-base"):
            args += ["--build-arg", f"ROS_DISTRO={name[:-len('-ros-base')]}"]
    for label, arg in RECIPE_ARGS.items():
        value = recipe.get(label)
        if value:
            args += ["--build-arg", f"{arg}={value}"]
    # --progress=plain: the default renderer redraws in place, which a log file records as a
    # single unreadable line. Plain output is what makes a long build followable.
    return (["docker", "buildx", "build", "--progress=plain", "--load", "-t", tag,
             "-f", dockerfile] + args + [context])


def _run(command, **kwargs):
    return subprocess.run(command, capture_output=True, text=True, check=False, **kwargs)


def image_labels(ref: str, *, local_only: bool) -> dict:
    """Labels for *ref*, from the local daemon or from the registry.

    ``local_only`` is not a convenience. The lock can only be read by *running* the image, so it
    always comes from the local daemon -- and a tag means different bytes locally and remotely
    the moment the registry moves ahead. Reading the recipe from one and the lock from the other
    would compare two images and call the difference a finding. So a real comparison insists on
    one image, and only ``--plan-only``, which reads no lock, may ask the registry.
    """
    if local_only:
        local = _run(["docker", "inspect", "--format", "{{json .Config.Labels}}", ref])
        if local.returncode != 0:
            sys.exit(f"{ref} is not present locally, and its lock can only be read by running "
                     f"it. Pull it first:\n    docker pull {ref}")
        try:
            return json.loads(local.stdout.strip() or "{}") or {}
        except ValueError:
            return {}
    remote = _run(["docker", "buildx", "imagetools", "inspect", "--format",
                   "{{json .Image}}", ref])
    if remote.returncode != 0:
        sys.exit(f"cannot read labels for {ref}: {remote.stderr.strip()}")
    payload = json.loads(remote.stdout.strip() or "{}")
    candidates = [payload] if "config" in payload else [
        v for v in payload.values() if isinstance(v, dict)]
    for candidate in candidates:
        found = ((candidate.get("config") or {}).get("Labels") or {})
        if found:
            return found
    return {}


def image_lock(ref: str) -> dict:
    """The build lock baked into *ref*, read by starting it. Requires the image locally."""
    out = {}
    for name in _MANIFEST_FILES:
        result = _run(["docker", "run", "--rm", "--pull=never", "--entrypoint", "cat", ref,
                       f"{_MANIFEST_DIR}/{name}"])
        if result.returncode == 0:
            out[name.removesuffix(".txt")] = parse_lock(name.removesuffix(".txt"),
                                                        result.stdout)
    return out


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--image", required=True,
                        help="the image to rebuild from its own recorded recipe")
    parser.add_argument("--dockerfile", default="container/robovast/Dockerfile")
    parser.add_argument("--context", default=".")
    parser.add_argument("--tag", default="robovast-recipe-rebuild:check")
    parser.add_argument("--allow-revision-drift", action="store_true",
                        help="compare even though this checkout is not the revision the image "
                             "was built from")
    parser.add_argument("--print-revision", action="store_true",
                        help="print the revision the image was built from, and nothing else -- "
                             "what a caller needs before it can check out the right Dockerfile")
    parser.add_argument("--plan-only", action="store_true",
                        help="print the rebuild command and the recipe; build nothing")
    args = parser.parse_args()

    labels = image_labels(args.image,
                          local_only=not (args.plan_only or args.print_revision))
    recipe = {k: v for k, v in labels.items()
              if k.startswith("org.robovast") or k == _REVISION_LABEL}

    if args.print_revision:
        revision = recipe.get(_REVISION_LABEL, "")
        if not revision:
            print(f"::error::{args.image} records no {_REVISION_LABEL}, so there is no way to "
                  f"know which Dockerfile built it. A rebuild would be a guess.", file=sys.stderr)
            return 1
        print(revision)
        return 0
    missing = [label for label in (_BASE_LABEL, *RECIPE_ARGS)
               if label not in ("org.robovast.scenario-execution-server-ref",)
               and not recipe.get(label)]
    print(f"Recipe recorded by {args.image}:")
    for label in sorted(recipe):
        print(f"  {label} = {recipe[label]}")
    if missing:
        for label in missing:
            print(f"::error::{label} is missing, so a rebuild would not be given that pin")
        return 1

    # The Dockerfile is an input too, and the recipe does not carry it -- it carries the commit
    # it came from. Rebuilding today's Dockerfile with an old image's pins tests a combination
    # that never existed, and any difference it found would be a fact about the drift rather
    # than about the recipe.
    recorded_revision = recipe.get(_REVISION_LABEL, "")
    here = _run(["git", "rev-parse", "HEAD"], cwd=_REPO).stdout.strip()
    if recorded_revision and here and not here.startswith(recorded_revision) \
            and not recorded_revision.startswith(here):
        print(f"\n  recorded revision: {recorded_revision}")
        print(f"  this checkout:     {here}")
        if not args.allow_revision_drift:
            print("::error::the image was built from a different revision of this repository, "
                  "so a rebuild here would use a Dockerfile that never produced it. Check out "
                  f"{recorded_revision} first, or pass --allow-revision-drift to compare "
                  "anyway and read every difference as drift rather than as a missing pin.")
            return 1
        print("  (--allow-revision-drift: differences below may be the Dockerfile, not a pin)")

    command = build_command(recipe, tag=args.tag, dockerfile=args.dockerfile,
                            context=args.context)
    print("\nRebuild:\n  " + " ".join(command))
    if args.plan_only:
        return 0

    recorded = image_lock(args.image)
    if not recorded:
        print(f"::error::{args.image} carries no build lock at {_MANIFEST_DIR}, so there is "
              f"nothing to compare a rebuild against. Pull it first if it is not local.")
        return 1

    # Streamed, not captured. This is the long step by far -- a cold rebuild of the whole
    # stack -- and a caller watching a silent process for an hour cannot tell a slow build
    # from a wedged one. The timeout is the other half of that: a build that stops making
    # progress must end as a failure with output, not as a job that runs until the runner
    # kills it.
    sys.stdout.flush()
    try:
        built = subprocess.run(command, cwd=_REPO, check=False, timeout=BUILD_TIMEOUT_S)
    except subprocess.TimeoutExpired:
        print(f"::error::the rebuild did not finish within {BUILD_TIMEOUT_S // 60} minutes. "
              f"The output above is where it stopped.")
        return 1
    if built.returncode != 0:
        print("::error::the rebuild failed, so the recorded recipe is not sufficient to "
              "reproduce this image. The output above says which step could not be satisfied.")
        return 1

    differences = diff_locks(recorded, image_lock(args.tag))
    if not differences:
        print("\nThe rebuild installs exactly what was recorded. The recipe is sufficient.")
        return 0
    print(f"\n{len(differences)} difference(s) between the recorded lock and the rebuild:")
    for d in differences[:50]:
        if d["how"] == "changed":
            print(f"  {d['kind']} {d['name']}: {d['was']} -> {d['now']}")
        elif d["how"] == "missing":
            print(f"  {d['kind']} {d['name']}: {d['was']} -> gone")
        else:
            print(f"  {d['kind']} {d['name']}: absent -> {d['now']}")
    print("::error::the rebuild did not reproduce the recorded software, so at least one "
          "input is not pinned by the recipe. Each line above is an input that resolved "
          "differently than it did when the image was built.")
    return 1


if __name__ == "__main__":
    sys.exit(main())
