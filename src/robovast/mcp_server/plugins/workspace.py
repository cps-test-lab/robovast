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

Token economics (important for an LLM):

* ``write_project_file`` / ``edit_project_file`` accept **only** ``.vast`` /
  ``.osc`` — the small text you author. ``edit_project_file`` sends a diff, so the
  validate→fix loop stays cheap.
* **Every other file** (run scripts, notebooks, binaries) uses
  ``create_upload`` → a short-lived URL you ``curl -X PUT --data-binary @file``
  into, so its bytes never pass through your context.
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


def write_project_file(workspace_id: str, path: str, content: str) -> dict:
    """Write a ``.vast`` or ``.osc`` file (inline). Other types → ``create_upload``.

    Only these two authored text types may be written inline; the tool returns
    metadata (``path``/``bytes``/``sha256``), never the content.

    Args:
        workspace_id: Target workspace.
        path: Relative path within the workspace (e.g. ``demo.vast``).
        content: File text.
    """
    from robovast.service.interface import WriteFileRequest
    try:
        return _client().write_project_file(WriteFileRequest(
            workspace_id=workspace_id, path=path, content=content)).model_dump()
    except Exception as e:  # noqa: BLE001
        return {"error": str(e)}


def edit_project_file(workspace_id: str, path: str, old_string: str,
                      new_string: str) -> dict:
    """Replace a **unique** substring in a ``.vast``/``.osc`` file (cheap fix loop).

    Send a small diff instead of re-uploading the whole file. ``old_string`` must
    occur exactly once; include surrounding context to disambiguate.
    """
    from robovast.service.interface import EditFileRequest
    try:
        return _client().edit_project_file(EditFileRequest(
            workspace_id=workspace_id, path=path,
            old_string=old_string, new_string=new_string)).model_dump()
    except Exception as e:  # noqa: BLE001
        return {"error": str(e)}


def read_project_file(workspace_id: str, path: str) -> dict:
    """Read a workspace file's text."""
    try:
        return _client().read_project_file(workspace_id, path).model_dump()
    except Exception as e:  # noqa: BLE001
        return {"error": str(e)}


def list_project_files(workspace_id: str) -> dict:
    """List the workspace's files with metadata (path/bytes/sha256/executable)."""
    try:
        return _client().list_project_files(workspace_id).model_dump()
    except Exception as e:  # noqa: BLE001
        return {"error": str(e)}


def delete_project_file(workspace_id: str, path: str) -> dict:
    """Delete a file from the workspace."""
    try:
        return _client().delete_project_file(workspace_id, path).model_dump()
    except Exception as e:  # noqa: BLE001
        return {"error": str(e)}


def create_upload(workspace_id: str, path: str, executable: bool = False) -> dict:
    """Get a one-time, expiring URL to PUT any non-``.vast``/``.osc`` file into the workspace.

    Use for run files, notebooks, custom postprocessing code, and binaries: the
    bytes travel straight to the server (``curl -X PUT --data-binary @<file>
    <url>``) instead of through your context. Set ``executable=True`` for scripts
    (a ``#!`` shebang is also auto-detected). The URL expires after ``expires_in``
    seconds — request a new one if it lapses.

    Returns:
        ``{token, path, expires_in, url}``.
    """
    from robovast.service.interface import CreateUploadRequest
    try:
        return _client().create_upload(CreateUploadRequest(
            workspace_id=workspace_id, path=path, executable=executable)).model_dump()
    except Exception as e:  # noqa: BLE001
        return {"error": str(e)}


# -- Plugin class ------------------------------------------------------------

_TOOLS = [
    create_workspace,
    list_workspaces,
    get_workspace,
    delete_workspace,
    write_project_file,
    edit_project_file,
    read_project_file,
    list_project_files,
    delete_project_file,
    create_upload,
]


class WorkspacePlugin:
    """Expose server-side workspace authoring as MCP tools."""

    name = "workspace"

    def register(self, mcp: FastMCP) -> None:
        for fn in _TOOLS:
            mcp.tool()(fn)
