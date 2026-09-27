# Copyright (C) 2026 Frederik Pasch
# SPDX-License-Identifier: Apache-2.0

"""The workspace listing, as the fields a caller reads.

The MCP tool ``list_workspaces`` returns it and ``vast workspace list --json`` prints it.
"""


def workspace_listing(client, workspace_id: str = "") -> dict:
    """``{workspaces, total}``: every workspace newest first, or only *workspace_id*.

    Raises whatever the client raises; the caller decides how a failure is reported.
    """
    if workspace_id:
        found = [client.get_workspace(workspace_id).model_dump()]
    else:
        found = [w.model_dump() for w in client.list_workspaces().workspaces]
    return {"workspaces": found, "total": len(found)}
