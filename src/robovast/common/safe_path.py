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

"""One confinement check for every caller-supplied relative path.

Paths arrive from clients — an MCP argument, a URL segment — so each place that
joins one onto a directory must refuse the escapes. That check existed three times,
each a little weaker than the last:

* ``WorkspaceStore._safe_join`` — rejected absolute / ``~`` / ``..`` **and** verified
  the resolved path, so symlinks could not point out either.
* ``LocalTransport.get_job_log`` — only ``campaign_dir.resolve() not in
  run_dir.parents``, with no up-front ``..``/``~`` rejection.
* ``LocalTransport.get_run_file`` — only a ``str.startswith`` test on the resolved
  path.

:func:`safe_join` is the strongest of the three, made root-agnostic so the workspace
tree, the campaign results tree, and anything added later share it. Which root a path
is confined against stays the caller's decision — a campaign path must never resolve
inside a workspace, or the read-only results tree would inherit the writable one's
permissions.
"""

import os
from pathlib import Path


class UnsafePathError(ValueError):
    """A caller-supplied relative path tried to leave its root."""


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
    if not rel_path or not rel_path.strip():
        raise UnsafePathError("path must not be empty")
    if os.path.isabs(rel_path) or rel_path.startswith("~"):
        raise UnsafePathError(f"path must be relative: {rel_path!r}")
    if any(part == ".." for part in Path(rel_path).parts):
        raise UnsafePathError(f"path must not contain '..': {rel_path!r}")

    root = Path(base).resolve()
    # resolve(strict=False) collapses symlinks for the parts that exist, so a link
    # planted inside the root cannot redirect the result outside it.
    resolved = (root / rel_path).resolve()
    if resolved != root and root not in resolved.parents:
        raise UnsafePathError(f"path escapes {root}: {rel_path!r}")
    return resolved
