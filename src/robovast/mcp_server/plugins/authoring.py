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

"""MCP plugin: authoring a campaign — the workspace and the ``.vast`` in it.

Everything before a campaign runs. A workspace is the service's only project binding, so
these tools create one, put files in it (through the address space), check the ``.vast``
they wrote, and see what configurations it would expand to. Nothing here starts work.
"""

import logging

from fastmcp import FastMCP

from robovast.mcp_server import service_access

logger = logging.getLogger(__name__)


def create_workspace(name: str = "") -> dict:
    """Create a workspace — the only project binding a campaign can be started from.

    Holds editable inputs only, and is independent of campaigns: a started campaign is
    self-contained, so editing or deleting the workspace never affects its results.
    Put files in it with ``write_file`` / ``update_workspace`` / ``create_upload``.

    Args:
        name: Optional human-friendly label.

    Returns:
        ``{workspace_id, name, created_at}``.
    """
    from robovast.service.interface import CreateWorkspaceRequest
    try:
        return service_access.client_or_local().create_workspace(
            CreateWorkspaceRequest(name=name)).model_dump()
    except Exception as e:  # noqa: BLE001
        return {"error": str(e)}


def list_workspaces(workspace_id: str = "") -> dict:
    """List the workspaces (newest first), or return one.

    Args:
        workspace_id: Return just this workspace. Empty lists all of them.

    Returns:
        ``{workspaces, total}`` of ``{workspace_id, name, created_at, read_only}``,
        or ``{error}``. A ``read_only`` workspace is a directory pinned with
        ``vast serve --workspace-dir``: edit it on the serve host, not through this API.
    """
    try:
        client = service_access.client_or_local()
        if workspace_id:
            found = [client.get_workspace(workspace_id).model_dump()]
        else:
            found = [w.model_dump()
                     for w in client.list_workspaces().workspaces]
        return {"workspaces": found, "total": len(found)}
    except Exception as e:  # noqa: BLE001
        return {"error": str(e)}


def delete_workspace(workspace_id: str) -> dict:
    """Delete a workspace and its inputs. Existing campaigns are unaffected."""
    try:
        return service_access.client_or_local().delete_workspace(workspace_id).model_dump()
    except Exception as e:  # noqa: BLE001
        return {"error": str(e)}


def update_workspace(workspace_id: str, directory: str, prune: bool = False) -> dict:
    """Push a whole local DIRECTORY into an existing workspace — the cheap bulk write.

    Reads *directory* on the **MCP-server host** and sends it to the service
    (``.vast``/``.osc`` inline, the rest via the upload side channel), so the bytes never
    enter your context. Prefer this over looping ``write_file``/``create_upload``. Hidden
    files and ``results/`` are skipped; existing files are overwritten in place.

    Args:
        workspace_id: Target ``ws-…`` id, or a unique workspace name.
        directory: Local project directory on the MCP-server host.
        prune: Also delete workspace files absent from *directory* (full mirror).

    Returns:
        ``{workspace_id, written, uploaded, pruned}`` counts.
    """
    from robovast.service.project_push import (_resolve_workspace_id,
                                               sync_directory_to_workspace)
    try:
        client = service_access.client_or_local()
        wid = _resolve_workspace_id(client, workspace_id)
        stats = sync_directory_to_workspace(
            client, wid, directory, skip_dirs={"results"}, prune=prune)
        return {"workspace_id": wid, **stats}
    except Exception as e:  # noqa: BLE001
        return {"error": str(e)}


def create_upload(address: str, executable: bool = False) -> dict:
    """One-time URL to PUT a file whose bytes should not pass through your context.

    For run files, notebooks, postprocessing code and binaries — anything
    ``write_file`` refuses (it takes only ``.vast``/``.osc``). PUT the bytes yourself:
    ``curl -X PUT --data-binary @<file> <url>``.

    Args:
        address: ``/sources/<workspace_id>/<path>`` — the address ``write_file`` takes.
        executable: Set the executable bit (a ``#!`` shebang is also auto-detected).

    Returns:
        ``{token, path, expires_in, url}``; the URL lapses after ``expires_in`` seconds,
        so request a new one rather than reusing a stale grant.
    """
    from robovast.service.interface import CreateUploadRequest
    try:
        return service_access.client_or_local().create_upload(CreateUploadRequest(
            address=address, executable=executable)).model_dump()
    except Exception as e:  # noqa: BLE001
        return {"error": str(e)}


#: Shared note for the two tools that take *address*. Written once: the two make the same
#: choice, and stating it twice is how the two halves drift apart — and every tool
#: description is sent on every request, so a duplicated paragraph is paid for twice.
#:
#: The lane matters beyond tidiness. Against a cluster or ``--attach`` service the
#: workspace is not on this host at all, so a filesystem read would check a different
#: file, or none, and report the verdict as if it were about the one the campaign runs.
_ADDRESS_LANE = """
A ``/sources/<workspace_id>/<path>`` address is checked **through the service**, so this
is the file the campaign will actually run. Anything else is read as a path on the
MCP-server host — for authoring before a workspace exists, and the only lane with no
service running. ``lane`` says which answered.
"""


def _address_lane(address: str):
    """``(workspace_id, rel_path)`` for a ``/sources`` address, or ``None`` for a path.

    Returning ``None`` rather than guessing is the point: an absolute filesystem path
    also starts with ``/``, so the two are told apart by whether the string parses as an
    address in a known namespace — never by a prefix test that would send
    ``/home/me/x.vast`` to the service as workspace ``home``.
    """
    from robovast.common.file_address import (RESULTS, SOURCES, AddressError,
                                              parse_address)
    try:
        namespace, owner, rel_path = parse_address(address)
    except AddressError:
        return None
    if namespace == RESULTS:
        raise ValueError(
            f"{address!r} is a campaign result, which is immutable and is not a project "
            f"to validate. Read it with read_file, or copy it into a /{SOURCES}/ "
            "workspace to work on it.")
    return owner, rel_path


def validate_project(address: str) -> dict:
    """Check a ``.vast`` before running it. Reports **every** problem in one pass.

    Covers YAML, schema, the scenario file and its parameter references, and every
    plugin reference (variation types and their parameters, postprocessing commands, the
    search strategy) — installed entry-point names and local ``./path.py:Class`` refs
    alike — each problem tagged with its config block and field, so the file is fixed in
    as few iterations as possible.

    Args:
        address: ``/sources/<workspace_id>/<path>``, or a path on the MCP-server host.

    Returns:
        ``{valid, configs, runs_per_config, total_trials, problems, lane}``, each
        problem ``{stage, config, field, message}``.
    """
    from robovast.common.config_validation import validate_project_file
    from robovast.service.project_push import _resolve_workspace_id
    try:
        target = _address_lane(address)
        if target is None:
            return {**validate_project_file(address), "lane": "local file"}
        client = service_access.client_or_local()
        workspace_id, rel_path = target
        report = client.validate_project(
            _resolve_workspace_id(client, workspace_id), rel_path)
        return {**report.model_dump(), "lane": "workspace"}
    except Exception as e:  # noqa: BLE001 - surface any resolution error to the client
        return {"valid": False, "configs": 0, "runs_per_config": 0,
                "total_trials": 0,
                "problems": [{"stage": "project", "config": None,
                              "field": None, "message": str(e)}]}


def preview_configurations(address: str, limit: int = 0) -> dict:
    """What would this ``.vast`` actually run? The resolved cells, without running them.

    ``validate_project`` gives only the counts; this gives each variation cell's resolved
    parameters. Check the sweep here before spending compute on it. Nothing is executed
    and nothing is written.

    Args:
        address: ``/sources/<workspace_id>/<path>``, or a path on the MCP-server host.
        limit: Maximum configurations to return (``0`` = all). ``configs`` is always the
            true total; ``truncated`` marks a shortened list.

    Returns:
        ``{configs, runs_per_config, total_trials, configurations, truncated, lane}``,
        each configuration ``{name, parameters}``; or ``{error}``.
    """
    from robovast.common.config_generation import generate_scenario_variations
    from robovast.service.project_push import _resolve_workspace_id
    try:
        target = _address_lane(address)
        if target is None:
            campaign_data, _ = generate_scenario_variations(
                variation_file=address, output_dir=None)
            configs = campaign_data["configs"]
            runs = campaign_data.get("execution", {}).get("runs", 1)
            items = [{"name": c["name"], "parameters": c.get("config", {})}
                     for c in configs]
            truncated = bool(limit) and len(items) > limit
            return {
                "configs": len(configs),
                "runs_per_config": runs,
                "total_trials": len(configs) * runs,
                "configurations": items[:limit] if truncated else items,
                "truncated": truncated,
                "lane": "local file",
            }
        client = service_access.client_or_local()
        workspace_id, rel_path = target
        resp = client.preview_configurations(
            _resolve_workspace_id(client, workspace_id), limit, rel_path)
        # ``previews`` carries the web UI's Module-Federation asset refs for rendering a
        # variation; they are useless to an MCP caller and would be the bulk of the reply.
        return {
            "configs": resp.configs,
            "runs_per_config": resp.runs_per_config,
            "total_trials": resp.total_trials,
            "configurations": [{"name": c.name, "parameters": c.parameters}
                               for c in resp.configurations],
            "truncated": resp.truncated,
            "lane": "workspace",
        }
    except Exception as e:  # noqa: BLE001 - surface any resolution error to the client
        return {"error": str(e)}


for _fn in (validate_project, preview_configurations):
    _fn.__doc__ = _fn.__doc__.replace(
        "    Args:\n", f"{_ADDRESS_LANE}\n    Args:\n", 1)


# -- Plugin class ------------------------------------------------------------

# A workspace's *files* are not here: they are written and read through the one address
# space (``write_file`` / ``read_file`` over ``/sources/<workspace_id>/<path>``), so there
# is a single way to name a file rather than one per scope.

_TOOLS = [
    create_workspace,
    list_workspaces,
    delete_workspace,
    update_workspace,
    create_upload,
    validate_project,
    preview_configurations,
]


class AuthoringPlugin:
    """MCP plugin: authoring a campaign — the workspace and the ``.vast`` in it."""

    name = "authoring"

    def register(self, mcp: FastMCP) -> None:
        """Register all tool functions with the MCP server."""
        for fn in _TOOLS:
            mcp.tool()(fn)
