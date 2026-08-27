# Copyright (C) 2026 Frederik Pasch
# SPDX-License-Identifier: Apache-2.0

"""The local Docker lane, as a registered ``vast serve`` backend.

Ships with the core for now; when the Docker lane becomes its own distribution this
module goes with it and nothing else changes, which is the point of the entry point.
"""

from __future__ import annotations


class LocalServeBackend:
    """In-process service driving local Docker."""

    #: Named in the startup line, so a reader can tell where results will land.
    storage = "local filesystem"

    def build(self, *, in_pod: bool, context: str | None, namespace: str, store,
              workspace_dir=None, results_dir=None):  # noqa: ARG002 - the lane ignores the cluster ones
        # Imported here, not at module level: listing the available lanes must not pull
        # in the in-process server (see robovast.service.serve_backends).
        from robovast.service.local_transport import \
            LocalTransport  # pylint: disable=import-outside-toplevel
        return LocalTransport(workspace_dir=workspace_dir, results_dir=results_dir)
