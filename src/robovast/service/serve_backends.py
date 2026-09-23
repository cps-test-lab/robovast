# Copyright (C) 2026 Frederik Pasch
# SPDX-License-Identifier: Apache-2.0

"""Which execution lane ``vast serve`` runs, resolved rather than imported.

A service runs one lane, fixed when it starts. The core registers none: the cluster lane
ships in ``robovast-cluster``, and reaching into it directly would make the core
uninstallable without the cluster code -- an install that legitimately has no Kubernetes
at all (a client with the results tooling) would fail on an import from a module the user
never named. A core with no lane installed serves nothing, and says so by name.

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

    def build(self, *, in_pod: bool, store, workspace_dir=None, results_dir=None):
        """Return the service implementation for this lane.

        Args:
            in_pod: The service is running inside the cluster it dispatches to. A lane
                that dispatches elsewhere reads where from its own deployment, not from
                arguments: a caller naming a cluster it is not running in would be
                naming one its campaigns' pods cannot reach back to.
            store: A prepared :class:`~robovast.service.workspaces.WorkspaceStore`, or
                ``None`` to let the lane make its own.
            workspace_dir: A directory pinned in place instead of uploaded.
            results_dir: Where campaigns land, or ``None`` for the lane's default.
                Campaigns of either lane live under it.
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


def resolve(name: "str | None" = None) -> "tuple[str, ServeBackend]":
    """Load the lane called *name* -- or the only one installed -- and say which.

    The error is the point. Without it, ``vast serve`` on an install with no lane
    package would either serve nothing quietly or raise ``ModuleNotFoundError`` naming
    a module the caller never mentioned, which reads as a broken install rather than a
    missing one.
    """
    from robovast.common.plugin_ref import load_ref  # pylint: disable=import-outside-toplevel
    have = available()
    if not have:
        raise ValueError(
            "no execution lane is installed, so there is nothing for this service to run "
            "campaigns on. The cluster lane ships as the robovast-cluster distribution; "
            "install it beside robovast.")
    if name is None:
        if len(have) > 1:
            raise ValueError(
                f"several execution lanes are installed ({', '.join(sorted(have))}); "
                f"name the one to run with --backend.")
        name = next(iter(have))
    elif name not in have:
        raise ValueError(
            f"no execution lane named {name!r} is installed. Available: "
            f"{', '.join(sorted(have))}.")
    loaded = load_ref(name, SERVE_BACKEND_GROUP)
    backend = loaded() if isinstance(loaded, type) else loaded
    if not isinstance(backend, ServeBackend):
        raise ValueError(
            f"execution lane {name!r} is a {type(backend).__name__}, which does not "
            f"implement build(); see robovast.service.serve_backends.ServeBackend")
    return name, backend
