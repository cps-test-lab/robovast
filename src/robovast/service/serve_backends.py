# Copyright (C) 2026 Frederik Pasch
# SPDX-License-Identifier: Apache-2.0

"""Which service implementation ``vast serve`` runs, resolved rather than imported.

The core registers none: the implementation ships in ``robovast-cluster``, and importing it
directly would make the core uninstallable without the cluster code. Implementations register
in the ``robovast.execution_backends`` entry-point group and are loaded through
:func:`robovast.common.plugin_ref.load_ref`, as simulators and variation types are.

**An implementation must import without the thing it drives**: this module is imported to
list what is installed, in a process that may have no kubeconfig. Reaching for one belongs
in :meth:`ServeBackend.build`.
"""

from __future__ import annotations

from typing import Protocol, runtime_checkable

#: Entry-point group service implementations register in.
SERVE_BACKEND_GROUP = "robovast.execution_backends"


@runtime_checkable
class ServeBackend(Protocol):
    """Builds the :class:`~robovast.service.interface.RobovastInterface` implementation."""

    def build(self, *, in_pod: bool, store, workspace_dir=None, results_dir=None):
        """Return the service implementation.

        Args:
            in_pod: The service is running inside the cluster it dispatches to; which
                cluster is read from its own deployment.
            store: A prepared :class:`~robovast.service.workspaces.WorkspaceStore`, or
                ``None`` to let the implementation make its own.
            workspace_dir: A directory pinned in place instead of uploaded.
            results_dir: Where campaigns land, or ``None`` for the default.
        """

    #: One word for the storage this implementation uses, for the startup line.
    storage: str


def available() -> dict[str, str]:
    """Registered name -> the entry point's target, without importing any of them."""
    from importlib.metadata import entry_points  # pylint: disable=import-outside-toplevel
    return {ep.name: ep.value for ep in entry_points(group=SERVE_BACKEND_GROUP)}


def resolve() -> "tuple[str, ServeBackend]":
    """Load the one installed implementation and say which.

    None installed, or several, is refused by name rather than served quietly or guessed.
    """
    from robovast.common.plugin_ref import load_ref  # pylint: disable=import-outside-toplevel
    have = available()
    if not have:
        raise ValueError(
            "no service implementation is installed, so there is nothing to run campaigns "
            "on. It ships as the robovast-cluster distribution; install it beside robovast.")
    if len(have) > 1:
        raise ValueError(
            f"several service implementations are installed ({', '.join(sorted(have))}); "
            f"install exactly one.")
    name = next(iter(have))
    loaded = load_ref(name, SERVE_BACKEND_GROUP)
    backend = loaded() if isinstance(loaded, type) else loaded
    if not isinstance(backend, ServeBackend):
        raise ValueError(
            f"service implementation {name!r} is a {type(backend).__name__}, which does not "
            f"implement build(); see robovast.service.serve_backends.ServeBackend")
    return name, backend
