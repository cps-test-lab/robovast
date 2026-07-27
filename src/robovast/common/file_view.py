# Copyright (C) 2026 Frederik Pasch
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
# http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing,
# software distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions
# and limitations under the License.
#
# SPDX-License-Identifier: Apache-2.0

"""How a file or directory is *rendered* to a caller, once its address is resolved.

Separate from :mod:`robovast.common.file_address` (which decides *where* a path points
and whether it may be written) because these are substrate concerns: paging text,
refusing binary, and turning a directory into a bounded listing. The service applies
them, not the client — a caller asking for 100 lines of a cluster log must transfer 100
lines, not the file.
"""

import logging
import os
from pathlib import Path
from typing import Callable, Optional

logger = logging.getLogger(__name__)

#: Bytes sampled when deciding whether a file is binary.
_SNIFF_BYTES = 8192


def is_binary_bytes(data: bytes) -> bool:
    """Whether *data* looks binary (a NUL byte in its first :data:`_SNIFF_BYTES`)."""
    return b"\x00" in data[:_SNIFF_BYTES]


def is_binary(path: Path) -> bool:
    """Whether *path* looks binary. Unreadable counts as binary — refusing to render
    it as text is the safe answer either way."""
    try:
        with open(path, "rb") as f:
            return is_binary_bytes(f.read(_SNIFF_BYTES))
    except OSError:
        return True


def binary_refused(name: str) -> ValueError:
    """The one refusal both substrates raise, so the advice does not depend on which
    lane answered."""
    return ValueError(
        f"{name} is a binary file — read it as bytes (GET the address without "
        "'as=text', or 'vast files get'), or download the campaign archive.")


def split_lines(text: str) -> list[str]:
    """Split *text* the way iterating an opened text file does.

    Deliberately **not** ``str.splitlines()``: that also breaks on form feed, NEL and
    the Unicode separators, so a lane using it reported a different ``total_lines``
    than the lane that iterated an open file — same file, same call, two answers
    depending on which backend served it. Universal-newline translation is applied
    here so a ``\\r\\n`` object reads like the same file on disk, and a trailing
    newline does not invent a final empty line.
    """
    normalized = text.replace("\r\n", "\n").replace("\r", "\n")
    out = normalized.split("\n")
    if out and out[-1] == "":
        out.pop()
    return out


def text_page(text: str, lines: int = 200, offset: int = 0) -> dict:
    """Window *text* into ``{total_lines, returned_lines, offset, content}``."""
    return _window(split_lines(text), lines, offset)


def _window(all_lines: list[str], lines: int, offset: int) -> dict:
    end = offset + lines if lines > 0 else None
    selected = all_lines[offset:end]
    return {
        "total_lines": len(all_lines),
        "returned_lines": len(selected),
        "offset": offset,
        "content": "\n".join(selected),
    }


def read_text_page(path: Path, lines: int = 200, offset: int = 0) -> dict:
    """Return ``{total_lines, returned_lines, offset, content}`` for a slice of *path*.

    Streams the file rather than materializing it: a 200-line window of a 2 GB log
    costs the window plus a line count, not the log. The total is still exact, which is
    what tells a caller whether to page again.

    ``lines <= 0`` means "no limit" — the whole file from *offset*. That is the editor's
    case (it needs the file, not a page) and it matches :func:`paginate`, so one
    convention covers both.

    Raises:
        ValueError: If the file is binary. Callers get the byte URL instead — mangling
            binary into "text" would be a wrong answer that looks like a right one.
    """
    if is_binary(path):
        raise binary_refused(path.name)
    total = 0
    window: list[str] = []
    end = offset + lines if lines > 0 else None
    with open(path, encoding="utf-8", errors="replace") as f:
        for i, line in enumerate(f):
            total = i + 1
            if i >= offset and (end is None or i < end):
                window.append(line.rstrip("\n"))
    return {
        "total_lines": total,
        "returned_lines": len(window),
        "offset": offset,
        "content": "\n".join(window),
    }


def scan_dir(directory: Path, recursive: bool = False,
             skip: Optional[Callable[[str, bool], bool]] = None) -> list[tuple[str, Path]]:
    """List *directory* as ``(name, path)`` pairs, sorted, directories suffixed ``/``.

    Non-recursive (the default) lists this level only — files **and** directories, so a
    caller can walk down. Recursive lists files only, at paths relative to *directory*:
    the intermediate directories are implied by the paths, and repeating them doubles
    the listing for no information.

    *skip* takes ``(relative_posix_path, is_dir)`` and is consulted **before** a
    directory is descended into, so a hidden subtree costs one call rather than one per
    file underneath it. That matters: a pinned workspace is a live git checkout, and
    walking it before rejecting ``.git`` took ~650 ms per listing on the web UI's
    config page — 6000× the cost of the entries it actually returns.

    Uses :func:`os.scandir`, whose entries carry the type from ``readdir`` — no
    ``stat`` per name, which is what makes this cheap over NFS and overlayfs too.
    """
    if not directory.is_dir():
        return []
    out: list[tuple[str, Path]] = []

    def _walk(current: Path, prefix: str) -> None:
        try:
            with os.scandir(current) as entries:
                for entry in entries:
                    rel = f"{prefix}{entry.name}"
                    is_dir = entry.is_dir(follow_symlinks=False)
                    if skip is not None and skip(rel, is_dir):
                        continue
                    if not recursive:
                        out.append((f"{rel}/" if is_dir else rel, Path(entry.path)))
                    elif is_dir:
                        _walk(Path(entry.path), f"{rel}/")
                    elif entry.is_file():
                        out.append((rel, Path(entry.path)))
        except OSError:
            # An unreadable subdirectory is not a reason to fail the whole listing;
            # it simply contributes nothing.
            logger.debug("skipped unreadable directory during scan: %s", current)

    _walk(directory, "")
    return sorted(out, key=lambda e: e[0])


def paginate(entries: list, offset: int = 0, limit: int = 100) -> tuple[list, int, bool]:
    """Return ``(page, total, truncated)``.

    ``total`` counts what was there **before** the window, so a truncated listing still
    reports how much it left out — the difference between "that is all of it" and "that
    is the first hundred".
    """
    total = len(entries)
    offset = max(offset, 0)
    page = entries[offset:offset + limit] if limit and limit > 0 else entries[offset:]
    return page, total, (offset + len(page)) < total


def build_listing(model, address: str, entries: list, *,
                  recursive: bool, detail: bool, offset: int, limit: int,
                  detail_fn) -> object:
    """Assemble a listing response from ``(name, handle)`` pairs.

    Shared by both substrates so the response *shape* — that ``total`` counts what was
    there before the window, that directory entries keep their ``/`` in ``entries`` but
    not in ``detailed``, that the echoed address is the directory's — has one
    definition. Two copies of this had already drifted apart once.

    *detail_fn* builds one ``entry_model`` from a ``(name, handle)`` pair; only it knows
    what the handle is (a filesystem path, or an object size).
    """
    page, total, truncated = paginate(entries, offset, limit)
    listing = model(address=address, total=total, truncated=truncated,
                    recursive=recursive)
    if detail:
        listing.detailed = [detail_fn(name, handle) for name, handle in page]
    else:
        listing.entries = [name for name, _ in page]
    return listing
