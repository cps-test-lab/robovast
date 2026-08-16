# Copyright (C) 2026 Frederik Pasch
# SPDX-License-Identifier: Apache-2.0

"""The half of the workspace vocabulary a client needs.

:mod:`robovast.service.workspaces` is the server side -- the registry, the store, the
upload tokens -- and it is not something a client ever constructs. ``is_skipped`` is, and
it is why the module had to be split rather than moved: a client pushing a directory must
agree with the service about which files are part of a project.

Where a *local service* keeps its workspaces is deliberately **not** here. That is
``ROBOVAST_WORKSPACES_ROOT``, it describes a service this install does not have, and a
client that knows it has learned something it cannot use.

**One definition, deliberately**: it is shared by the listing and the push, because a
listing and a push that disagree make ``prune`` delete files it would not restore.
"""

from pathlib import Path

#: Dir names hidden from a *pinned* (read-only) workspace listing / .vast lookup --
#: campaign outputs, not project inputs (mirrors the CLI ``workspace init`` skip).
PINNED_SKIP_DIRS = {"results"}


def is_skipped(rel_path, skip_dirs=frozenset()) -> bool:
    """Whether a workspace-relative path is hidden from listings and pushes alike.

    Hidden files/dirs (``.git``, ``.cache``) and anything under a name in *skip_dirs*
    (campaign outputs, typically ``results/``). One definition because a listing and a
    push that disagree make ``prune`` delete files it would not restore.
    """
    parts = rel_path.split("/") if isinstance(rel_path, str) else Path(rel_path).parts
    return any(p.startswith(".") or p in skip_dirs for p in parts)
