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
        return service_access.client_or_local().create_workspace(
            CreateWorkspaceRequest(name=name)).model_dump()
    except Exception as e:  # noqa: BLE001
        return {"error": str(e)}


def list_workspaces() -> dict:
    """List all workspaces (newest first)."""
    try:
        return service_access.client_or_local().list_workspaces().model_dump()
    except Exception as e:  # noqa: BLE001
        return {"error": str(e)}


def get_workspace(workspace_id: str) -> dict:
    """Return one workspace's info."""
    try:
        return service_access.client_or_local().get_workspace(workspace_id).model_dump()
    except Exception as e:  # noqa: BLE001
        return {"error": str(e)}


def delete_workspace(workspace_id: str) -> dict:
    """Delete a workspace and its inputs. Existing campaigns are unaffected."""
    try:
        return service_access.client_or_local().delete_workspace(workspace_id).model_dump()
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
        client = service_access.client_or_local()
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
        return service_access.client_or_local().create_upload(CreateUploadRequest(
            address=address, executable=executable)).model_dump()
    except Exception as e:  # noqa: BLE001
        return {"error": str(e)}


def validate_project(config_path: str) -> dict:
    """Validate a RoboVAST project (``.vast`` file), reporting ALL problems at once.

    A ``.vast`` file defines a *project* (a campaign is one execution of it). This
    checks the whole file — YAML, schema, the scenario file, scenario-parameter
    references, and every plugin reference (variation types and their parameters,
    the ``results_processing``/``search`` postprocessing commands, and the search
    strategy/extractor), whether installed entry-point names or local
    ``./path.py:Class`` file refs — and returns **every** problem it finds in one
    pass, each tagged with the config block and field, so the file can be fixed in
    as few iterations as possible. When valid, it also returns the config/run
    counts (same math as ``vast config info``). Same collect-all core as the
    ``vast configuration validate`` CLI command.

    Reads the ``.vast`` straight off disk — no workspace, no service, and no
    initialized project needed, so it works before anything else exists.

    Args:
        config_path: Path to the ``.vast`` file. Required: there is no server-side
            "current project" to fall back to, and guessing one would validate a
            different file than the caller named.

    Returns:
        ``{valid, configs, runs_per_config, total_trials, problems}`` where each
        problem is ``{stage, config, field, message}``.
    """
    from robovast.common.config_validation import validate_project_file
    try:
        return validate_project_file(config_path)
    except Exception as e:  # noqa: BLE001 - surface any resolution error to the client
        return {"valid": False, "configs": 0, "runs_per_config": 0,
                "total_trials": 0,
                "problems": [{"stage": "project", "config": None,
                              "field": None, "message": str(e)}]}


def preview_configurations(config_path: str, max_configs: int = 0) -> dict:
    """Preview the resolved configurations a ``.vast`` would generate — WITHOUT running.

    ``validate_project`` returns only the counts; this returns the actual resolved
    per-configuration parameter sets, so you can eyeball what each variation cell
    expands to before starting a campaign (the read-only, in-memory equivalent of
    ``vast configuration generate`` / ``vast exec local prepare-run``, which stage
    the same tree to disk). Nothing is executed and nothing is written.

    Reads the ``.vast`` straight off disk — no workspace, service, or initialized
    project needed.

    Args:
        config_path: Path to the ``.vast`` file. Required, for the same reason as
            ``validate_project``: there is no server-side "current project".
        max_configs: Cap the number of configurations returned (``0`` = all). The
            ``configs`` count always reflects the true total; ``truncated`` marks
            when the returned list was shortened.

    Returns:
        ``{configs, runs_per_config, total_trials, configurations, truncated}``
        where each configuration is ``{name, parameters}`` and ``parameters`` is
        the resolved parameter-name → value mapping for that cell. On failure,
        ``{error}``.
    """
    from robovast.common.config_generation import generate_scenario_variations
    try:
        campaign_data, _ = generate_scenario_variations(
            variation_file=config_path, output_dir=None)
        configs = campaign_data["configs"]
        runs = campaign_data.get("execution", {}).get("runs", 1)
        items = [{"name": c["name"], "parameters": c.get("config", {})}
                 for c in configs]
        truncated = bool(max_configs) and len(items) > max_configs
        if truncated:
            items = items[:max_configs]
        return {
            "configs": len(configs),
            "runs_per_config": runs,
            "total_trials": len(configs) * runs,
            "configurations": items,
            "truncated": truncated,
        }
    except Exception as e:  # noqa: BLE001 - surface any resolution error to the client
        return {"error": str(e)}


# -- Plugin class ------------------------------------------------------------

# A workspace's *files* are not here: they are written and read through the one address
# space (``write_file`` / ``read_file`` over ``/sources/<workspace_id>/<path>``), so there
# is a single way to name a file rather than one per scope.

_TOOLS = [
    create_workspace,
    list_workspaces,
    get_workspace,
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
