#!/usr/bin/env python3
"""Re-resolve the pins that say WHICH OF OUR SOURCES an image bakes, and leave a reviewable diff.

The companion to ``refresh_build_pins.py``, deliberately a separate command because the two answer
different questions. Build pins are third-party ground -- base-image digests, dated apt archives --
and refreshing them takes whatever upstream published. These pins name commits of code we write, so
moving one is a release decision: it changes what the image *does*, and the diff it produces is the
record of that decision.

    python3 tools/refresh_source_pins.py            # report what would change
    python3 tools/refresh_source_pins.py --ask      # report, then offer to apply it
    python3 tools/refresh_source_pins.py --write    # rewrite the Dockerfile ARGs, no question asked
    python3 tools/refresh_source_pins.py --check    # fail if one source is pinned twice, differently
    python3 tools/refresh_source_pins.py --set ROQSIM_REF=<sha|branch|tag>   # one pin, everywhere

``--ask`` is what the Makefile uses, because the decision needs the diff in front of it: the report
IS the question, and answering it from a flag means committing to the answer before seeing what it
applies to. Without a terminal to ask on it degrades to the report rather than assuming yes -- a
build script that inherited this command must not move a pin because nobody was there to say no.

Every pin is resolved with ``git ls-remote <repo> refs/heads/<branch>``, so what lands is by
construction a commit on a durable ref. That is the one property the pins must have and the one they
have lost before: a pin taken from a feature branch became unreachable when the branch was deleted,
and every clean build then failed with "fatal: reference is not a tree". Resolving a *branch head*
rather than accepting a sha from a caller makes that failure unreachable.

What it finds: each ``ARG <NAME>_REF=<sha>`` that has a matching ``ARG <NAME>_REPO=<url>`` beside
it, in any of the container Dockerfiles. Nothing is hardcoded here, so a fourth source added in the
same idiom is refreshed without touching this file -- and a source pinned in some *other* idiom is
invisible to it, which is why the inline clones were promoted to ARGs.

Note that a refreshed ``ROQSIM_REF`` only decides what a plain ``docker build`` clones:
``container/release_images.sh`` defaults ``--roqsim-ref main``, and a caller naming a ref there
overrides the pin. ``SCENARIO_EXECUTION_REF`` has no such override in the release path, so for
that one this command is the only way the image moves.
"""

import argparse
import pathlib
import re
import subprocess
import sys

import pin_prompt

_REPO = pathlib.Path(__file__).resolve().parents[1]
_DOCKERFILES = ("container/robovast/Dockerfile", "container/robovast/Dockerfile.roqsim",
                "container/controller/Dockerfile", "container/sidecar/Dockerfile")

_REF_PIN = re.compile(r"^ARG\s+(?P<name>[A-Z0-9_]+)_REF=(?P<sha>[0-9a-f]{40})\s*$", re.MULTILINE)
_TIMEOUT = 60


def _repo_url(text: str, name: str) -> "str | None":
    """The ``<NAME>_REPO`` default that belongs to a ``<NAME>_REF`` pin.

    A ref without a repo beside it is not refreshable -- there is nowhere to ask -- and is reported
    rather than skipped, because a pin this command silently ignores is a pin that goes stale while
    the output says everything is current.
    """
    match = re.search(rf"^ARG\s+{re.escape(name)}_REPO=(?P<url>\S+)\s*$", text, re.MULTILINE)
    return match.group("url") if match else None


def _resolve_head(url: str, branch: str) -> "str | None":
    """The commit *branch* points at in *url*, without cloning it."""
    try:
        result = subprocess.run(["git", "ls-remote", url, f"refs/heads/{branch}"],
                                capture_output=True, text=True, check=False, timeout=_TIMEOUT)
    except (FileNotFoundError, subprocess.TimeoutExpired):
        return None
    if result.returncode != 0:
        return None
    for line in result.stdout.splitlines():
        sha, _, ref = line.partition("\t")
        if ref.strip() == f"refs/heads/{branch}" and re.fullmatch(r"[0-9a-f]{40}", sha):
            return sha
    return None


def _resolve_any(url: str, ref: str) -> "str | None":
    """The commit *ref* names in *url*: a sha as given, otherwise a branch or tag resolved."""
    if re.fullmatch(r"[0-9a-f]{40}", ref):
        return ref
    try:
        result = subprocess.run(["git", "ls-remote", url, ref, f"refs/heads/{ref}",
                                 f"refs/tags/{ref}"],
                                capture_output=True, text=True, check=False, timeout=_TIMEOUT)
    except (FileNotFoundError, subprocess.TimeoutExpired):
        return None
    if result.returncode != 0:
        return None
    # A tag can resolve to the tag object and to what it points at; the dereferenced line
    # (`^{}`) is the commit, so it wins where both are listed.
    best = None
    for line in result.stdout.splitlines():
        sha, _, name = line.partition("\t")
        if not re.fullmatch(r"[0-9a-f]{40}", sha):
            continue
        if name.strip().endswith("^{}"):
            return sha
        best = best or sha
    return best


def _set_pin(spec: str) -> int:
    """Write one ``<NAME>_REF`` to every Dockerfile that pins it. ``NAME_REF=<sha|branch|tag>``.

    The pin is repeated because each Dockerfile has to build on its own, so changing it by hand
    means changing every copy and remembering which files those are. This is that edit as one
    command: the copies are an implementation detail of the build, not something to maintain.
    """
    name, _, ref = spec.partition("=")
    name = name.removesuffix("_REF")
    if not name or not ref:
        print(f"--set takes NAME_REF=<sha|branch|tag>, got {spec!r}", file=sys.stderr)
        return 2

    targets, url = [], None
    for rel in _DOCKERFILES:
        path = _REPO / rel
        if not path.exists():
            continue
        text = path.read_text(encoding="utf-8")
        for match in _REF_PIN.finditer(text):
            if match.group("name") == name:
                targets.append((path, rel, match.group("sha")))
                url = url or _repo_url(text, name)
    if not targets:
        print(f"no {name}_REF pin in any of: {', '.join(_DOCKERFILES)}", file=sys.stderr)
        return 1
    if url is None:
        print(f"{name}_REF has no {name}_REPO beside it, so {ref!r} cannot be resolved",
              file=sys.stderr)
        return 1

    sha = _resolve_any(url, ref)
    if sha is None:
        print(f"cannot resolve {ref!r} in {url}", file=sys.stderr)
        return 1

    for path, rel, old in targets:
        if old == sha:
            print(f"{rel}: {name}_REF already {sha}")
            continue
        path.write_text(path.read_text(encoding="utf-8").replace(
            f"ARG {name}_REF={old}", f"ARG {name}_REF={sha}"), encoding="utf-8")
        print(f"{rel}: {name}_REF {old} -> {sha}")
    return 0


def _disagreements() -> "list[str]":
    """One source pinned at two commits, as lines to report.

    A source can be pinned in more than one Dockerfile -- each must build on its own, and
    ``Dockerfile.roqsim`` is built with an empty context, so there is no shared file to read a
    pin from. Two copies that disagree bake two commits of one source into one image family,
    which is what this refuses. ``--write`` cannot produce it (one resolve per repo), but an
    edit by hand can.
    """
    seen: "dict[str, dict[str, list[str]]]" = {}
    for rel in _DOCKERFILES:
        path = _REPO / rel
        if not path.exists():
            continue
        text = path.read_text(encoding="utf-8")
        for match in _REF_PIN.finditer(text):
            name, sha = match.group("name"), match.group("sha")
            url = _repo_url(text, name)
            if url is not None:
                seen.setdefault(url, {}).setdefault(sha, []).append(f"{rel} ({name}_REF)")
    problems = []
    for url, by_sha in sorted(seen.items()):
        if len(by_sha) > 1:
            where = "\n".join(f"    {sha}  {', '.join(sorted(files))}"
                               for sha, files in sorted(by_sha.items()))
            problems.append(f"{url} is pinned at {len(by_sha)} different commits:\n{where}")
    return problems


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    pin_prompt.add_arguments(parser)
    parser.add_argument("--branch", default="main",
                        help="Branch to resolve in every source repo (default: main).")
    parser.add_argument("--set", dest="set_pin", metavar="NAME_REF=REF",
                        help="Set one pin in every Dockerfile that carries it. REF is a sha, "
                             "a branch or a tag; everything else is left alone.")
    parser.add_argument("--check", action="store_true",
                        help="Fail if one source is pinned at two different commits. "
                             "Offline: it reads the committed pins and asks no remote.")
    args = parser.parse_args()

    if args.set_pin:
        return _set_pin(args.set_pin)

    if args.check:
        problems = _disagreements()
        for problem in problems:
            print(problem, file=sys.stderr)
        if problems:
            print("\nRun: python3 tools/refresh_source_pins.py --write", file=sys.stderr)
            return 1
        print("every source is pinned at one commit")
        return 0

    changes, current, unresolved = [], [], []
    # path -> rewritten text, held back until the decision below: with --ask the report has to be
    # printed before anything is written, so collecting the edits and applying them is two steps.
    edits: "dict[pathlib.Path, str]" = {}
    # One repo can be pinned in more than one Dockerfile, and two ls-remote calls could straddle a
    # push -- which would bake two different commits of one source into one image family.
    resolved: "dict[str, str | None]" = {}

    for rel in _DOCKERFILES:
        path = _REPO / rel
        if not path.exists():
            continue
        text = original = path.read_text(encoding="utf-8")

        for match in _REF_PIN.finditer(original):
            name, old = match.group("name"), match.group("sha")
            url = _repo_url(original, name)
            if url is None:
                unresolved.append(f"{rel}: {name}_REF has no {name}_REPO beside it")
                continue
            if url not in resolved:
                resolved[url] = _resolve_head(url, args.branch)
            new = resolved[url]
            if new is None:
                unresolved.append(f"{rel}: could not resolve {args.branch} in {url}")
            elif new == old:
                current.append(f"{name}_REF is already {args.branch}: {old}")
            else:
                changes.append(f"{rel}: {name}_REF ({url}, {args.branch})\n"
                               f"    {old}\n -> {new}")
                text = text.replace(f"ARG {name}_REF={old}", f"ARG {name}_REF={new}")

        if text != original:
            edits[path] = text

    for line in current:
        print(line)
    for change in changes:
        print(change)
    for problem in unresolved:
        print(f"unresolved: {problem}", file=sys.stderr)

    if not changes:
        print(f"every source pin already carries {args.branch}")
    elif pin_prompt.apply_or_ask(edits, len(changes), args):
        print(f"\nrewrote {len(changes)} pin(s). Commit the diff, then build: the pin is only real "
              f"once it is committed, and 'make release-images' bakes what is committed here.")
    # An unreachable remote is reported but does not fail: a network blip is not a reason to treat
    # the committed pins as wrong. A pin with no repo beside it IS a defect in the Dockerfile,
    # though -- it can never be refreshed -- so that one fails.
    return 1 if any("has no" in problem for problem in unresolved) else 0


if __name__ == "__main__":
    sys.exit(main())
