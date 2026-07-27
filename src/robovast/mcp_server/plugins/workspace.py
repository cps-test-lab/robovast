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

"""MCP plugin: **workspaces** — author a project the server can run.

A workspace is a server-side folder of editable project *inputs* (no client
filesystem, so the server may live on another host). These tools map 1:1 onto
the :class:`~robovast.service.interface.RobovastInterface` workspace ops via a
:func:`~robovast.service.client.RobovastClient`, so they work whether the server
is in-process (local Docker) or a remote/cluster ``robovast-service`` reached over
a tunnel auto-detected on the conventional local port.

A workspace's **files** are addressed as ``/sources/<workspace_id>/<path>`` and read or
written with the generic file tools (``read_file``, ``write_file``, ``edit_file``,
``delete_file``, ``list_files``).

Token economics (important for an LLM):

* ``write_file`` / ``edit_file`` accept **only** ``.vast`` / ``.osc`` — the small text
  you author. ``edit_file`` sends a diff, so the validate→fix loop stays cheap.
* **Every other file** (run scripts, notebooks, binaries) uses ``create_upload`` → a
  short-lived URL you ``curl -X PUT --data-binary @file`` into, so its bytes never pass
  through your context.
"""

import logging

from fastmcp import FastMCP

logger = logging.getLogger(__name__)


def _client():
    from robovast.common.cli.service_target import detected_service_url
    from robovast.service.client import RobovastClient
    return RobovastClient(detected_service_url())


def create_workspace(name: str = "") -> dict:
    """Create a new workspace to author a project (``.vast`` + scenario + files).

    A workspace holds only editable inputs and is **independent of campaigns**:
    once you run a campaign it is self-contained, so editing or deleting the
    workspace later never affects existing results.

    Args:
        name: Optional human-friendly label.

    Returns:
        ``{workspace_id, name, created_at}``.
    """
    from robovast.service.interface import CreateWorkspaceRequest
    try:
        return _client().create_workspace(CreateWorkspaceRequest(name=name)).model_dump()
    except Exception as e:  # noqa: BLE001
        return {"error": str(e)}


def list_workspaces() -> dict:
    """List all workspaces (newest first)."""
    try:
        return _client().list_workspaces().model_dump()
    except Exception as e:  # noqa: BLE001
        return {"error": str(e)}


def get_workspace(workspace_id: str) -> dict:
    """Return one workspace's info."""
    try:
        return _client().get_workspace(workspace_id).model_dump()
    except Exception as e:  # noqa: BLE001
        return {"error": str(e)}


def delete_workspace(workspace_id: str) -> dict:
    """Delete a workspace and its inputs. Existing campaigns are unaffected."""
    try:
        return _client().delete_workspace(workspace_id).model_dump()
    except Exception as e:  # noqa: BLE001
        return {"error": str(e)}


def update_workspace(workspace_id: str, directory: str, prune: bool = False) -> dict:
    """Re-sync a local DIRECTORY into an existing workspace (id or name).

    Reads files from *directory* **on the host running this MCP server** and pushes
    them to the service — ``.vast``/``.osc`` inline, everything else via the upload
    side channel — so the file bytes never pass through your context. This is the
    cheap way to refresh a whole project at once instead of looping
    ``write_file`` / ``create_upload`` per file. Hidden files/dirs and
    ``results/`` are skipped; existing files are overwritten in place.

    Args:
        workspace_id: Target workspace ``ws-…`` id, or a unique workspace name.
        directory: Local project directory on the MCP-server host.
        prune: Also delete workspace files absent from *directory* (full mirror).

    Returns:
        ``{workspace_id, written, uploaded, pruned}`` counts.
    """
    from robovast.service.project_push import (_resolve_workspace_id,
                                               sync_directory_to_workspace)
    try:
        client = _client()
        wid = _resolve_workspace_id(client, workspace_id)
        stats = sync_directory_to_workspace(
            client, wid, directory, skip_dirs={"results"}, prune=prune)
        return {"workspace_id": wid, **stats}
    except Exception as e:  # noqa: BLE001
        return {"error": str(e)}


def create_upload(address: str, executable: bool = False) -> dict:
    """Get a one-time, expiring URL to PUT any non-``.vast``/``.osc`` file into a workspace.

    Use for run files, notebooks, custom postprocessing code, and binaries: the
    bytes travel straight to the server (``curl -X PUT --data-binary @<file>
    <url>``) instead of through your context. Set ``executable=True`` for scripts
    (a ``#!`` shebang is also auto-detected). The URL expires after ``expires_in``
    seconds — request a new one if it lapses.

    Args:
        address: ``/sources/<workspace_id>/<path>`` — the same address ``write_file``
            takes. (The returned ``url`` is a one-time capability, not an address.)
        executable: Set the executable bit on the stored file.

    Returns:
        ``{token, path, expires_in, url}``.
    """
    from robovast.service.interface import CreateUploadRequest
    try:
        return _client().create_upload(CreateUploadRequest(
            address=address, executable=executable)).model_dump()
    except Exception as e:  # noqa: BLE001
        return {"error": str(e)}


# -- Plugin class ------------------------------------------------------------

# Reading and writing a workspace's files is not here: it is the ``/sources`` half of
# the one address space (``read_file`` / ``write_file`` / ``edit_file`` / ``delete_file``
# / ``list_files``). What stays is the workspace *as an object* — create, list, delete,
# bulk-sync — plus the upload grant, which is a capability rather than a file operation.
_TOOLS = [
    create_workspace,
    list_workspaces,
    get_workspace,
    delete_workspace,
    update_workspace,
    create_upload,
]


class WorkspacePlugin:
    """Expose server-side workspace authoring as MCP tools."""

    name = "workspace"

    def register(self, mcp: FastMCP) -> None:
        for fn in _TOOLS:
            mcp.tool()(fn)
