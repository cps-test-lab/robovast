#!/usr/bin/env python3
# Copyright (C) 2026 Frederik Pasch
# SPDX-License-Identifier: Apache-2.0
"""The changelog's raw material, its completeness check, and its release body.

Every merge on ``main`` is a squash commit whose subject is the pull request's title with
its number and whose body is the pull request's description, so git alone holds what a
changelog is written from; nothing here needs a forge or a token.

    collect   one block per merge since the last ``v*`` tag: number, title, first paragraph
              of the description, and the areas of the tree the diff touched
    check     ``CHANGELOG.md`` has a section for the version: a flat list of at most
              fifteen short entries, each a bold topic and a line on it, citing merges in
              the range only, each as a link to its pull request (or commit)
    section   the section's body, for the release notes

A merge without a pull request number is keyed by its short sha instead, and is cited as
that.
"""

from __future__ import annotations

import argparse
import re
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path

CHANGELOG = "CHANGELOG.md"
#: A description's first paragraph says what a merge is for; past this, it is saying how.
SUMMARY_CHARS = 400
#: The changes a user of the previous version needs to hear about; the rest is in git.
MAX_ENTRIES = 15
#: An entry's text, citations aside: a topic and one line on it.
MAX_ENTRY_CHARS = 160
ENTRY = re.compile(r"^- \*\*[^*]+\*\* — \S")
#: A citation as the changelog writes it: ``[#123](<repo>/pull/123)``, or
#: ``[abcdef12](<repo>/commit/<sha>)`` for a merge pushed without a pull request.
LINK = re.compile(r"\[(?:#(\d+)|([0-9a-f]{8}))\]"
                  r"\((https://github\.com/([^/\s()]+/[^/\s()]+)/(pull|commit)/([0-9a-f]+))\)")
_LINK = LINK.pattern
CITATIONS = re.compile(rf"\s*\((?:{_LINK})(?:, (?:{_LINK}))*\)\s*$")
NUMBER = re.compile(r"\(#(\d+)\)\s*$")
CITATION = re.compile(r"#(\d+)\b|\b([0-9a-f]{8})\b")
GITHUB_REMOTE = re.compile(r"github\.com[:/]([^/\s]+/[^/\s]+?)(?:\.git)?/?$")
HEADING = re.compile(r"^## (\S+)\s*$")


@dataclass(frozen=True)
class Merge:
    sha: str
    number: int | None
    subject: str
    summary: str
    areas: tuple[str, ...]

    @property
    def key(self) -> str:
        """How the changelog cites it: ``#123``, or the short sha of a direct push."""
        return f"#{self.number}" if self.number is not None else self.sha[:8]


def git(root: Path, *args: str) -> str:
    return subprocess.run(["git", *args], cwd=root, capture_output=True, text=True,
                          check=True).stdout


def repository_root(start: Path) -> Path:
    return Path(git(start, "rev-parse", "--show-toplevel").strip())


def github_repository(root: Path) -> str | None:
    """``owner/name`` of the GitHub repository ``origin`` points at, or None."""
    remote = subprocess.run(["git", "remote", "get-url", "origin"], cwd=root,
                            capture_output=True, text=True, check=False).stdout.strip()
    match = GITHUB_REMOTE.search(remote)
    return match.group(1) if match else None


def link(merge: "Merge", repository: str) -> str:
    """The citation of one merge, as a link a reader of the rendered changelog can follow."""
    if merge.number is not None:
        return f"[#{merge.number}](https://github.com/{repository}/pull/{merge.number})"
    return f"[{merge.sha[:8]}](https://github.com/{repository}/commit/{merge.sha})"


def last_tag(root: Path, head: str) -> str:
    """The newest ``v*`` tag reachable from ``head``: the previous release."""
    return git(root, "describe", "--tags", "--abbrev=0", "--match", "v*", head).strip()


def area(path: str) -> str:
    """The part of the tree a path belongs to, at the depth a reader thinks in.

    A distribution under ``src/`` is one area; the core package is split one level
    further, since ``src/robovast`` alone says nothing about what changed.
    """
    parts = path.split("/")
    if parts[0] == "src" and len(parts) > 2:
        depth = 3 if parts[1] == "robovast" and len(parts) > 3 else 2
        return "/".join(parts[:depth])
    return parts[0]


def merges(root: Path, since: str, head: str) -> list[Merge]:
    log = git(root, "log", "--first-parent", "--reverse", "--format=%H%x00%s%x00%b%x01",
              f"{since}..{head}")
    found = []
    for record in log.split("\x01"):
        if not record.strip():
            continue
        sha, subject, body = record.strip("\n").split("\x00", 2)
        match = NUMBER.search(subject)
        number = int(match.group(1)) if match else None
        title = NUMBER.sub("", subject).strip()
        paragraphs = [p.strip() for p in re.split(r"\n\s*\n", body.strip()) if p.strip()]
        summary = " ".join(paragraphs[0].split()) if paragraphs else ""
        if len(summary) > SUMMARY_CHARS:
            summary = summary[:SUMMARY_CHARS].rsplit(" ", 1)[0] + " ..."
        touched = git(root, "diff-tree", "--no-commit-id", "--name-only", "-r", sha).split()
        areas = tuple(sorted({area(p) for p in touched}))
        found.append(Merge(sha, number, title, summary, areas))
    return found


def read_section(text: str, version: str) -> str | None:
    """The body under ``## <version>``, up to the next ``## `` heading; None if absent."""
    lines = text.splitlines()
    start = next((i for i, line in enumerate(lines)
                  if (m := HEADING.match(line)) and m.group(1) == version), None)
    if start is None:
        return None
    end = next((i for i in range(start + 1, len(lines)) if HEADING.match(lines[i])),
               len(lines))
    return "\n".join(lines[start + 1:end]).strip("\n")


def cited(section: str) -> list[str]:
    """Every citation in the section, in order and with repeats."""
    return [f"#{n}" if n else sha for n, sha in CITATION.findall(section)]


def entries(section: str) -> tuple[list[str], list[str]]:
    """The section's entries, each joined onto one line, and any line that is not part of one.

    An entry is a ``- `` item at the margin; an indented line continues the entry above it.
    """
    found: list[str] = []
    stray: list[str] = []
    for line in section.splitlines():
        if not line.strip():
            continue
        if line.startswith("- "):
            found.append(line.strip())
        elif line.startswith(" ") and found:
            found[-1] += " " + line.strip()
        else:
            stray.append(line.strip())
    return found, stray


# -- the commands -----------------------------------------------------------------------

def command_collect(root: Path, since: str, head: str, repository: str | None) -> int:
    found = merges(root, since, head)
    print(f"{since}..{head}: {len(found)} merges\n")
    for merge in found:
        print(f"{merge.key:>10}  {merge.subject}")
        if repository:
            print(f"{'':>10}  cite: {link(merge, repository)}")
        print(f"{'':>10}  areas: {', '.join(merge.areas) or '-'}")
        if merge.summary:
            print(f"{'':>10}  {merge.summary}")
        print()
    return 0


def link_problems(section: str, repository: str) -> list[str]:
    """Citations that are not a link, or a link that does not lead to what it names."""
    problems = []
    for number, sha, _, repo, kind, target in LINK.findall(section):
        wanted = ("pull", number) if number else ("commit", None)
        if repo != repository:
            problems.append(f"links into {repo}, not {repository}: {number or sha}")
        elif kind != wanted[0] or (number and target != number) or (
                sha and not target.startswith(sha)):
            problems.append(f"the link for {'#' + number if number else sha} leads to "
                            f"{kind}/{target}")
    bare = [f"#{n}" if n else s for n, s in CITATION.findall(LINK.sub("", section))]
    if bare:
        problems.append("cited without a link to it: " + ", ".join(bare))
    return problems


def command_check(root: Path, version: str, since: str, head: str,
                  repository: str | None) -> int:
    if not repository:
        return fail("cannot tell which GitHub repository the links lead into: origin is not "
                    "one; pass --repository owner/name")
    path = root / CHANGELOG
    if not path.exists():
        return fail(f"{CHANGELOG} does not exist")
    section = read_section(path.read_text(encoding="utf-8"), version)
    if section is None:
        return fail(f"{CHANGELOG} has no '## {version}' section")
    expected = {m.key for m in merges(root, since, head)}
    found, stray = entries(section)
    problems = []
    if not found:
        problems.append("the section has no entries")
    if len(found) > MAX_ENTRIES:
        problems.append(f"{len(found)} entries; at most {MAX_ENTRIES}, and fewer where fewer "
                        "matter -- keep the changes a user must know about, the rest is in git")
    if stray:
        problems.append("not an entry (the section is one flat list, no headings or prose): "
                        + "; ".join(stray))
    for entry in found:
        if not ENTRY.match(entry):
            problems.append(f"not '- **Topic** — what changed': {entry}")
        text = CITATIONS.sub("", entry)
        if len(text) > MAX_ENTRY_CHARS:
            problems.append(f"{len(text)} characters; at most {MAX_ENTRY_CHARS}: {entry}")
    problems += link_problems(section, repository)
    foreign = sorted(k for k in set(cited(section)) if k not in expected)
    if foreign:
        problems.append("cited but not merged in the range (an issue number, or a typo): "
                        + ", ".join(foreign))
    if problems:
        for problem in problems:
            print(f"FAIL  {problem}")
        return 1
    print(f"ok    {version}: {len(found)} entries, {len(expected)} merges since {since}")
    return 0


def command_section(root: Path, version: str) -> int:
    path = root / CHANGELOG
    section = read_section(path.read_text(encoding="utf-8"), version) if path.exists() else None
    if section is None:
        return fail(f"{CHANGELOG} has no '## {version}' section")
    print(section)
    return 0


def fail(message: str) -> int:
    print(f"FAIL  {message}")
    return 1


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n", 1)[0])
    parser.add_argument("--root", type=Path, default=None,
                        help="the repository (default: the one the cwd is in)")
    commands = parser.add_subparsers(dest="command", required=True)
    for name in ("collect", "check"):
        sub = commands.add_parser(name)
        sub.add_argument("--since", help="the previous release tag (default: newest v* tag)")
        sub.add_argument("--head", default="origin/main")
        sub.add_argument("--repository", help="owner/name the citations link into "
                                              "(default: the GitHub repository origin names)")
    commands.choices["check"].add_argument("--version", required=True)
    commands.add_parser("section").add_argument("--version", required=True)
    args = parser.parse_args(argv)

    root = repository_root(args.root or Path.cwd())
    if args.command == "section":
        return command_section(root, args.version)
    since = args.since or last_tag(root, args.head)
    repository = args.repository or github_repository(root)
    if args.command == "collect":
        return command_collect(root, since, args.head, repository)
    return command_check(root, args.version, since, args.head, repository)


if __name__ == "__main__":
    sys.exit(main())
