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

Everything here is derived from live objects at call time, so it can never drift from
the code: the ``.vast`` schema comes from the ``ConfigV1`` pydantic model, and the CLI
reference is rendered from the live click command tree (plugin subcommands included).

``get_cli_help`` covers both listing and detail. They were two tools; a caller that has
to call ``list_cli_commands`` before ``get_cli_help`` pays two round trips and two tool
schemas to answer one question, and an empty argument already means "all of them"
everywhere else on this surface.
"""

import logging

from fastmcp import FastMCP

logger = logging.getLogger(__name__)


# -- Config schema -----------------------------------------------------------


def get_config_schema() -> dict:
    """JSON Schema of the ``.vast`` project file — field names, types, what is required.

    A ``variations`` entry dispatches by plugin name, so it appears here only as a
    generic object; for one variation's parameters use
    ``get_plugin_details("robovast.variation_types", <name>)``.
    """
    from robovast.common.config import ConfigV1  # pylint: disable=import-outside-toplevel

    return ConfigV1.model_json_schema()


# -- CLI help ----------------------------------------------------------------

_CLI_LOADED = False


def _root_group():
    """Return the root ``vast`` click group with plugin subcommands attached."""
    global _CLI_LOADED  # pylint: disable=global-statement  (loaded once per process)
    from robovast.client import cli as cli_module  # pylint: disable=import-outside-toplevel

    if not _CLI_LOADED:
        try:
            cli_module.load_plugins()
        except Exception:  # noqa: BLE001 - a broken plugin must not hide the rest
            logger.debug("load_plugins() failed while building CLI reference.", exc_info=True)
        _CLI_LOADED = True
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


def _command_tree() -> list[dict]:
    """Every ``vast`` command path with its short help, from the live click tree."""
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


def get_cli_help(command: str = "") -> dict:
    """``vast`` CLI reference: the command tree, or one command's full ``--help``.

    Args:
        command: Space-separated path, e.g. ``"workspace run"``. Empty lists every
            command with its one-line help.

    Returns:
        ``{commands, total}`` when listing, else ``{command, help}``.
    """
    if not command:
        commands = _command_tree()
        return {"commands": commands, "total": len(commands)}
    cmd, ctx = _resolve_command(command)
    return {"command": command, "help": cmd.get_help(ctx)}


def get_service_info() -> dict:
    """Which robovast-service is answering, which code it runs, and which lanes it offers.

    Call this first when something behaves unexpectedly: a service loads robovast **once,
    at startup**, so after an edit a reachable service may still be running the old code.
    Compare ``code_revision`` with your tree (``git rev-parse --short HEAD``) and restart it
    if they differ.

    **When ``code_revision`` is absent, this check is unavailable** — that deployment could
    not determine its revision, and there is no substitute: probe for the behaviour you
    expect instead. ``code_version`` will not do, and reading it as one is a live trap. It is
    the package semver on a deployed service, so it stays the same across every edit, and it
    used to be reported *in place of* a revision — which made this field silently unable to
    detect the one thing it exists for.

    Returns:
        ``{code_version, api_version, backend, results_address, sources_address}``, plus
        ``code_revision`` when known, or ``{error}``.

        ``backend`` is the lane this service runs, fixed when it started. Use
        ``get_resource_usage()`` to actually touch it before committing a long campaign.

        ``results_root``/``sources_root`` appear only when **you** can open them (a
        local-filesystem service on loopback); then read files directly instead of
        relaying bytes through this interface. ``web_base`` prefixes a route into a
        URL; absent when this deployment has none.

        With a cluster lane: ``kube_context``, ``kube_context_source``, ``namespace``,
        ``in_pod``, ``api_server`` — which cluster a campaign would land in.
        ``in_pod: false`` means campaigns are driven off-cluster through a port-forward:
        fine for a pilot, fragile for a large campaign's result transfers.

        ``can_build_images`` says whether this deployment can build an experiment image at
        all — check it before authoring a container that adds packages, because otherwise
        the refusal arrives at ``start_campaign``, after the push and the workspace.
        ``build_unavailable`` then carries the reason and the fix. Both are **absent** when
        the service did not say; absent is not ``false``.
    """
    from robovast.mcp_server import service_access
    from robovast.mcp_server.service_access import NO_SERVICE
    client = service_access.service_client()
    if client is None:
        return {"error": NO_SERVICE}
    try:
        v = client.version()
    except Exception as e:  # noqa: BLE001
        return {"error": str(e)}
    info = {
        "code_version": v.robovast_version,
        "api_version": v.api_version,
        "backend": v.backend,
        "results_address": v.results_address,
        "sources_address": v.sources_address,
    }
    # Only when it is genuinely known. Absent says "this deployment cannot tell you", which
    # a caller can act on (probe the behaviour); a placeholder would be read as a revision
    # that happens not to match, which is a different and wrong conclusion.
    if getattr(v, "code_revision", ""):
        info["code_revision"] = v.code_revision
    # Only when set: a null root reads as "unknown", when the truthful statement is
    # "this service has no path you can open" — so say nothing rather than say null.
    if v.results_root:
        info["results_root"] = v.results_root
    if v.sources_root:
        info["sources_root"] = v.sources_root
    # Same rule: absent means this deployment has no origin to declare (unpublished, or
    # bound to a wildcard), not an origin that happens to be unknown.
    if v.web_base:
        info["web_base"] = v.web_base
    # Only when there is a cluster lane: on a local-only service these would all be
    # None, and five null fields read as "unknown" rather than "not applicable".
    if v.backend == "kubernetes":
        info.update({
            "kube_context": v.kube_context,
            "kube_context_source": v.kube_context_source,
            "namespace": v.namespace,
            "in_pod": v.in_pod,
            "api_server": v.api_server,
        })
    # Only when the service gave a verdict. `None` means "this service did not say" --
    # an older one has no such field -- and reporting that as False would tell every
    # healthy pre-field deployment to go and fix a registry it does not need fixed.
    if v.can_build_images is not None:
        info["can_build_images"] = v.can_build_images
        if not v.can_build_images and v.build_unavailable:
            info["build_unavailable"] = v.build_unavailable
    return info


# -- Plugin class ------------------------------------------------------------

_TOOLS = [
    get_config_schema,
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
