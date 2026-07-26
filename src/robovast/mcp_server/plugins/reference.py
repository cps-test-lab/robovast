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

"""MCP plugin exposing generated reference material.

Everything here is derived from live objects at call time, so it can never
drift from the code:

* :func:`get_config_schema` — the ``.vast`` project schema, generated from the
  ``ConfigV1`` pydantic model as JSON Schema.
* :func:`list_cli_commands` / :func:`get_cli_help` — the ``vast`` command
  reference, rendered from the live click command tree (including plugin
  subcommands).
"""

import logging

from fastmcp import FastMCP

logger = logging.getLogger(__name__)


# -- Config schema -----------------------------------------------------------


def get_config_schema() -> dict:
    """Return the RoboVAST ``.vast`` project configuration schema (JSON Schema).

    Generated from the ``ConfigV1`` pydantic model, so it always matches the
    version the server validates against. Use it to author or check a ``.vast``
    file's structure, field names, types, and which fields are required.

    Note: variation entries (the ``variations`` block) dispatch dynamically by
    plugin name and so appear here only as a generic object. Each variation's
    accepted parameter fields are exposed by ``get_plugin_details(group=
    "robovast.variation_types", name=...)`` instead.
    """
    from robovast.common.config import ConfigV1  # pylint: disable=import-outside-toplevel

    return ConfigV1.model_json_schema()


# -- CLI help ----------------------------------------------------------------

_cli_loaded = False


def _root_group():
    """Return the root ``vast`` click group with plugin subcommands attached."""
    global _cli_loaded
    from robovast.common.cli import cli as cli_module  # pylint: disable=import-outside-toplevel

    if not _cli_loaded:
        try:
            cli_module.load_plugins()
        except Exception:  # noqa: BLE001 - a broken plugin must not hide the rest
            logger.debug("load_plugins() failed while building CLI reference.", exc_info=True)
        _cli_loaded = True
    return cli_module.cli


def _resolve_command(command: str):
    """Navigate the click tree to *command* (space-separated path).

    Returns ``(cmd, ctx)``. Raises ``ValueError`` for an unknown path.
    """
    import click  # pylint: disable=import-outside-toplevel

    cmd = _root_group()
    ctx = click.Context(cmd, info_name="vast")
    for part in command.split():
        sub = cmd.get_command(ctx, part) if isinstance(cmd, click.Group) else None
        if sub is None:
            raise ValueError(f"Unknown command {command!r} (failed at {part!r}).")
        ctx = click.Context(sub, info_name=part, parent=ctx)
        cmd = sub
    return cmd, ctx


def list_cli_commands() -> list[dict]:
    """List the ``vast`` CLI commands with their one-line help.

    Walks the live click command tree (plugin subcommands included). Returns
    records with the full ``command`` path and its short ``help``; pass a path
    to :func:`get_cli_help` for full usage.
    """
    import click  # pylint: disable=import-outside-toplevel

    out: list[dict] = []
    visited: set[int] = set()

    def _walk(cmd, ctx, prefix: str) -> None:
        if not isinstance(cmd, click.Group) or id(cmd) in visited:
            return
        visited.add(id(cmd))
        for name in sorted(cmd.list_commands(ctx)):
            sub = cmd.get_command(ctx, name)
            if sub is None:
                continue
            path = f"{prefix} {name}".strip()
            out.append({"command": path, "help": sub.get_short_help_str(limit=120)})
            _walk(sub, click.Context(sub, info_name=name, parent=ctx), path)

    root = _root_group()
    _walk(root, click.Context(root, info_name="vast"), "vast")
    return out


def get_cli_help(command: str = "") -> str:
    """Return the full ``--help`` text for a ``vast`` command.

    Args:
        command: Space-separated command path (e.g. ``"exec cluster run"``).
            Empty for the top-level ``vast`` help. Use :func:`list_cli_commands`
            to discover paths.
    """
    cmd, ctx = _resolve_command(command)
    return cmd.get_help(ctx)


def get_service_info() -> dict:
    """Report which robovast-service is answering, and which code it is running.

    A service is a long-lived process: it loads robovast's code and its plugin entry
    points **once, at startup**. After editing robovast or installing a plugin, a
    reachable service is not necessarily a current one — it keeps behaving like the
    code it started with, which surfaces as a fix that "did not work". ``code_version``
    is the git revision that process is running (with ``+dirty`` when its working tree
    had uncommitted changes); compare it with the tree you just edited, and restart the
    service if they differ.

    Returns:
        ``{code_version, api_version, backend, backends, mcp_plugins}``; ``{error}``
        when no service answers. ``mcp_plugins`` are the plugin groups *this MCP
        process* loaded — same staleness caveat, different process.
    """
    from importlib.metadata import entry_points

    from robovast.mcp_server.plugins.campaign_control import (_NO_SERVICE,
                                                              _service_client)
    client = _service_client()
    if client is None:
        return {"error": _NO_SERVICE}
    try:
        v = client.version()
    except Exception as e:  # noqa: BLE001
        return {"error": str(e)}
    return {
        "code_version": v.robovast_version,
        "api_version": v.api_version,
        "backend": v.backend,
        "backends": v.backends,
        "mcp_plugins": sorted(
            ep.name for ep in entry_points(group="robovast.mcp_plugins")),
    }


# -- Plugin class ------------------------------------------------------------

_TOOLS = [
    get_config_schema,
    list_cli_commands,
    get_cli_help,
    get_service_info,
]


class ReferencePlugin:
    """Expose generated config-schema and CLI reference as MCP tools."""

    name = "reference"

    def register(self, mcp: FastMCP) -> None:
        """Register the reference tools with the MCP server."""
        for fn in _TOOLS:
            mcp.tool()(fn)
