# Copyright (C) 2026 Frederik Pasch
# SPDX-License-Identifier: Apache-2.0

"""Which execution lane ``vast serve`` runs, resolved rather than imported.

A service runs one lane, fixed when it starts. ``vast serve`` used to reach into the
cluster service directly to build that one, so the core could not be installed without the
cluster code: an install that legitimately has no Kubernetes at all would have failed on an
import, from a module the user never named.

Lanes register in the ``robovast.execution_backends`` entry-point group instead, exactly
as simulators, variation types and panel types already do — and through the same resolver
(:func:`robovast.common.plugin_ref.load_ref`), so there is one spelling to learn and a
``<file>.py:<Class>`` reference works here too.

**A lane must import without the thing it drives.** ``robovast.simulators`` states the
same rule for the same reason: this module is imported to *list* what is available, in a
process that may have no Docker and no kubeconfig. Reaching for either belongs in
:meth:`ServeBackend.build`, which runs only once a caller has asked for that lane by name.
"""

from __future__ import annotations

from typing import Protocol, runtime_checkable

#: Entry-point group execution lanes register in.
SERVE_BACKEND_GROUP = "robovast.execution_backends"


@runtime_checkable
class ServeBackend(Protocol):
    """Builds the :class:`~robovast.service.interface.RobovastInterface` for one lane."""

    def build(self, *, in_pod: bool, context: str | None, namespace: str, store,
              workspace_dir=None, results_dir=None):
        """Return the service implementation for this lane.

        Args:
            in_pod: The service is running inside the cluster it dispatches to.
            context: Kubernetes context to use, when the lane needs one.
            namespace: Kubernetes namespace, when the lane needs one.
            store: A prepared :class:`~robovast.service.workspaces.WorkspaceStore`, or
                ``None`` to let the lane make its own.
            workspace_dir: A directory pinned in place instead of uploaded.
            results_dir: Where local campaigns land, or ``None`` for the lane's default.
                Only the local lane has one; a cluster campaign's results live in the
                object store.
        """

    #: One word for the storage this lane uses, for the startup line.
    storage: str


def available() -> dict[str, str]:
    """Registered lane name -> the entry point's target, without importing any of them.

    Listing must stay cheap and safe: this is what a caller uses to say "cluster is not
    installed" rather than raising an ImportError from inside a lane it never chose.
    """
    from importlib.metadata import entry_points  # pylint: disable=import-outside-toplevel
    return {ep.name: ep.value for ep in entry_points(group=SERVE_BACKEND_GROUP)}


def resolve(name: str) -> ServeBackend:
    """Load the lane called *name*, or say which ones this install actually has.

    The error is the point. Without it, ``vast serve --backend cluster`` on an install
    with no cluster package raises ``ModuleNotFoundError`` naming a module the caller
    never mentioned — which reads as a broken install rather than a missing one.
    """
    from robovast.common.plugin_ref import load_ref  # pylint: disable=import-outside-toplevel
    have = available()
    if name not in have:
        listed = ", ".join(sorted(have)) or "(none)"
        raise ValueError(
            f"no execution lane named {name!r} is installed. Available: {listed}. "
            f"The cluster lane ships separately -- install it, or run "
            f"'vast serve --backend local'.")
    loaded = load_ref(name, SERVE_BACKEND_GROUP)
    backend = loaded() if isinstance(loaded, type) else loaded
    if not isinstance(backend, ServeBackend):
        raise ValueError(
            f"execution lane {name!r} is a {type(backend).__name__}, which does not "
            f"implement build(); see robovast.service.serve_backends.ServeBackend")
    return backend
