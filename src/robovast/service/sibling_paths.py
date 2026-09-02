# Copyright (C) 2026 Frederik Pasch
# SPDX-License-Identifier: Apache-2.0

"""Paths a containerised service hands to the host's Docker daemon.

The local lane shells out to ``docker`` (see :mod:`robovast.common.cli.checks`). When the
service itself runs in a container it is a **sibling** of the containers it starts, sharing
the host's daemon through a mounted socket -- it never runs a daemon of its own. So every
path it passes as a bind source is resolved by that daemon **on the host**, not inside the
service container, while the same path is *also* traversed by the service's own code and by
the generated ``run.sh``.

Both readings agree only when the path means the same thing on both sides, which is what an
**identity mount** (``-v /path:/path``) buys. The trick is not new here:
:mod:`robovast.common.variation.container_runner` already mounts its scratch workspace that
way, "so the plugin's absolute workspace paths are valid on both sides". This module
generalises it and, more importantly, *checks* it.

**Why check rather than document.** A path that exists inside the container and not on the
host does not raise: the daemon happily creates an empty directory at that path and bind
mounts it, so the run produces a campaign directory with nothing in it, or a container that
starts and immediately finds no config. Both are hours to diagnose and neither names the
cause. A startup check that refuses, naming the path, costs one comparison.
"""

from __future__ import annotations

import os
from pathlib import Path

#: Set by the container entrypoint to the host paths mounted at the same absolute location.
#: A colon-separated list, so it reads like ``PATH`` and can be built up by a compose file.
IDENTITY_MOUNTS_ENV = "ROBOVAST_IDENTITY_MOUNTS"

#: Set by the container entrypoint when the service runs as a sibling container. Its value
#: is not read -- only its presence, which is the one thing the service cannot infer.
IN_CONTAINER_ENV = "ROBOVAST_IN_CONTAINER"


def in_sibling_container() -> bool:
    """Whether this service runs in a container driving the **host's** Docker daemon.

    Declared by the entrypoint rather than sniffed. ``/.dockerenv`` and cgroup inspection
    both answer "am I in a container", which is the wrong question: an in-pod cluster
    service is in a container too and has no daemon to alias paths with. What matters is
    whether the daemon this process talks to shares a filesystem with it, and only whoever
    started the container knows that.
    """
    return bool(os.environ.get(IN_CONTAINER_ENV))


def identity_mounts() -> list[Path]:
    """The host paths mounted at the same absolute location inside this container."""
    raw = os.environ.get(IDENTITY_MOUNTS_ENV, "")
    return [Path(p).resolve() for p in raw.split(os.pathsep) if p]


def _is_identity_mapped(path: Path, mounts: list[Path]) -> bool:
    resolved = Path(path).resolve()
    return any(resolved == m or m in resolved.parents for m in mounts)


def require_identity_mapped(path, *, what: str) -> None:
    """Refuse a path the host daemon would resolve differently, naming it.

    A no-op unless the service is a sibling container: on a host service, and in a pod
    (whose lane creates Kubernetes Jobs and binds nothing from here), every path already
    means one thing.

    Args:
        path: The path about to be handed to the daemon as a bind source.
        what: What it is, for the message -- "the results directory", "the workspace".

    Raises:
        ValueError: When *path* is not under an identity mount. Mapped to 400 by the HTTP
            layer like every other refusal, and raised at *startup* where it can name the
            remedy, rather than at run time where it would surface as an empty directory.
    """
    if not in_sibling_container():
        return
    mounts = identity_mounts()
    if _is_identity_mapped(path, mounts):
        return
    listed = ", ".join(str(m) for m in mounts) or "(none declared)"
    raise ValueError(
        f"{what} is {path}, which this service can see but the Docker daemon cannot "
        f"resolve to the same place: the daemon runs on the host and this service runs in "
        f"a container beside it, so a bind source must exist at the SAME absolute path on "
        f"both. Mount it as '-v {path}:{path}' and add it to "
        f"{IDENTITY_MOUNTS_ENV}. Currently declared: {listed}. "
        f"Left unchecked this does not fail loudly -- the daemon creates an empty directory "
        f"at that path and mounts it, so the campaign runs and produces nothing.")
