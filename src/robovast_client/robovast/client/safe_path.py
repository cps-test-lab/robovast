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

"""One confinement check for every path that comes from outside.

A path arrives from a client (an MCP argument, a URL segment), from an archive member, or
from a pod's request, and each place that joins one onto a directory refuses the escapes
with the functions here rather than a check of its own. Which root a path is confined
against stays the caller's decision -- a campaign path must never resolve inside a
workspace, or the read-only results tree would inherit the writable one's permissions.

* :func:`check_segment` -- a name that must be one entry of its root: a campaign id, a
  cell, a job tag.
* :func:`check_relative` -- the path *shapes* never accepted anywhere. On its own only
  where there is no filesystem to resolve against: an object-store key is composed as
  ``prefix + rel`` after it.
* :func:`safe_join` -- that, then the resolve-and-verify only a filesystem can perform,
  so a symlink cannot lead out either.
* :func:`is_inside` -- the verify alone, for a caller that has built the path itself
  (an archive member, a link target).
"""

import os
from pathlib import Path, PurePosixPath


class UnsafePathError(ValueError):
    """A caller-supplied relative path tried to leave its root."""


def check_relative(rel_path: str) -> PurePosixPath:
    """Reject a path shape that must never be joined onto any root.

    The substrate-independent half of :func:`safe_join`: refuses an empty path, an
    absolute path, a ``~`` prefix, and any ``..`` segment. Use it directly only where
    there is no filesystem to resolve against (an object-store key); anywhere a real
    directory exists, use :func:`safe_join`, which also defeats symlink escapes.

    Args:
        rel_path: Caller-supplied path, relative to some root.

    Returns:
        The path as a :class:`~pathlib.PurePosixPath`, ready to be joined or turned
        into an object key.

    Raises:
        UnsafePathError: On an empty, absolute, ``~``-prefixed or ``..``-containing path.
    """
    if not rel_path or not rel_path.strip():
        raise UnsafePathError("path must not be empty")
    if os.path.isabs(rel_path) or rel_path.startswith("~"):
        raise UnsafePathError(f"path must be relative: {rel_path!r}")
    if any(part == ".." for part in Path(rel_path).parts):
        raise UnsafePathError(f"path must not contain '..': {rel_path!r}")
    return PurePosixPath(rel_path)


def check_segment(name: str) -> str:
    """Reject a name that is not exactly one entry of the directory it is joined onto.

    Empty, ``.``, ``..`` or a name carrying a separator would name the root itself, its
    parent, or a path below or beside it.

    Raises:
        UnsafePathError: On any of those.
    """
    if not name or name in (".", "..") or "/" in name or "\\" in name:
        raise UnsafePathError(f"not one path segment: {name!r}")
    return name


def is_inside(root: Path, path) -> bool:
    """Whether *path*, resolved, is the resolved *root* or lies under it.

    Compared by path components, so a sibling sharing the root's prefix (``root2`` beside
    ``root``) is outside. Symlinks that exist are followed, so one planted inside the root
    cannot lead out.
    """
    resolved = Path(path).resolve()
    return resolved == root or root in resolved.parents


def safe_join(base, rel_path: str) -> Path:
    """Resolve *rel_path* inside *base*, refusing any escape.

    Rejects an empty path, an absolute path, a ``~`` prefix, and any ``..`` segment,
    then verifies the **resolved** result is still under *base* so a symlink cannot
    point outside. ``base`` itself is allowed (a path of ``"."``).

    Args:
        base: Root the path must stay within.
        rel_path: Caller-supplied path, relative to *base*.

    Returns:
        The resolved absolute path.

    Raises:
        UnsafePathError: On an empty, absolute, ``~``-prefixed, ``..``-containing, or
            symlink-escaping path. A :class:`ValueError`, so existing
            ``except ValueError`` handlers keep mapping it to a 4xx.
    """
    check_relative(rel_path)

    root = Path(base).resolve()
    resolved = (root / rel_path).resolve()
    if not is_inside(root, resolved):
        raise UnsafePathError(f"path escapes {root}: {rel_path!r}")
    return resolved
