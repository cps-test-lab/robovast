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

#: Entries a campaign writes into its own results directory, at that directory's top
#: level. Used to recognise a results tree by what it CONTAINS rather than by what it is
#: called -- see :func:`is_campaign_results_dir`.
CAMPAIGN_RESULT_MARKERS = ("metadata.yaml", "metadata.prov.json", "campaign.db",
                           "_execution", "_transient", "_jobs")

#: How many markers must be present. More than one because a project may legitimately
#: own a ``metadata.yaml``; a project that also has ``_execution/`` beside it is a
#: results tree whatever its name.
_MARKERS_REQUIRED = 2


def is_campaign_results_dir(path) -> bool:
    """Whether *path* is a campaign's output directory, judged by its contents.

    **By content and not by name, deliberately.** A name-based rule can only know the
    names we happen to use: ``results/`` is skipped, but ``vast results download`` and
    ``exec cluster run --wait-and-download`` land a campaign under its *campaign id*
    (``<name>-<timestamp>``), and those were uploaded as project input -- hundreds of
    megabytes of a past campaign's bags pushed back at the service as though someone had
    authored them. Extending the name list would not fix it either: the next naming
    convention is not in the list, and a list of the names we use is stale the moment one
    changes.

    Requires :data:`_MARKERS_REQUIRED` of :data:`CAMPAIGN_RESULT_MARKERS` so a project
    that merely owns a ``metadata.yaml`` is not mistaken for a campaign.

    Cheap on purpose: it stats a handful of fixed names in one directory and never
    recurses, so it can be asked about every directory of a walk.
    """
    d = Path(path)
    if not d.is_dir():
        return False
    found = 0
    for marker in CAMPAIGN_RESULT_MARKERS:
        if (d / marker).exists():
            found += 1
            if found >= _MARKERS_REQUIRED:
                return True
    return False


def is_skipped(rel_path, skip_dirs=frozenset()) -> bool:
    """Whether a workspace-relative path is hidden from listings and pushes alike.

    Hidden files/dirs (``.git``, ``.cache``) and anything under a name in *skip_dirs*
    (campaign outputs, typically ``results/``). One definition because a listing and a
    push that disagree make ``prune`` delete files it would not restore.
    """
    parts = rel_path.split("/") if isinstance(rel_path, str) else Path(rel_path).parts
    return any(p.startswith(".") or p in skip_dirs for p in parts)
