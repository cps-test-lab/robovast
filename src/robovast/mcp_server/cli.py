#!/usr/bin/env python3
# Copyright (C) 2025 Frederik Pasch
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

"""CLI for the RoboVAST MCP server."""

import asyncio
import json

import click


@click.group()
def mcp():
    """Run the RoboVAST MCP server.

    Exposes RoboVAST over the Model Context Protocol so that AI assistants
    can both drive campaigns (validate, start, monitor, stop) and analyze
    their results through a single interface.
    """


@mcp.command(name='serve')
@click.option('--transport', type=click.Choice(['stdio', 'sse', 'streamable-http']),
              default='sse', show_default=True,
              help='Transport to use.')
@click.option('--host', default='127.0.0.1', show_default=True,
              help='Host to bind when using an HTTP transport. Defaults to '
                   'localhost; the control tools launch real compute and the '
                   'server has no authentication, so only bind a routable '
                   'address on a network you trust.')
@click.option('--port', default=8801, show_default=True, type=int,
              help='Port to bind when using an HTTP transport.')
@click.option('--debug', is_flag=True,
              help='Enable DEBUG logging for all MCP messages.')
def serve(transport, host, port, debug):
    """Start the RoboVAST MCP server.

    Exposes RoboVAST tools via the Model Context Protocol so that AI
    assistants (e.g. Claude, Open WebUI) can drive campaigns and interact
    with run results and documentation.

    Examples::

      vast mcp serve                                      # sse (default)
      vast mcp serve --transport stdio                    # stdio
      vast mcp serve --transport streamable-http          # HTTP transport
    """
    from robovast.mcp_server.server import create_server  # pylint: disable=import-outside-toplevel

    import logging  # pylint: disable=import-outside-toplevel

    logging.basicConfig(level=logging.WARNING)
    logging.getLogger("mcp").setLevel(logging.CRITICAL)

    if debug:
        # Enable only our own human-readable wrapper; keep MCP internals quiet.
        logging.getLogger("robovast.mcp_server").setLevel(logging.DEBUG)

    mcp_server = create_server(host=host, port=port, debug=debug)

    try:
        if transport in ("sse", "streamable-http"):
            mcp_server.run(transport=transport, host=host, port=port)
        else:
            mcp_server.run(transport=transport)
    except KeyboardInterrupt:
        pass


# -- Introspective test client -----------------------------------------------
#
# ``tools`` and ``call`` are a generic, schema-driven client: they discover the
# available tools and their argument schemas at runtime via ``list_tools()`` and
# never hardcode any tool name or parameter, so they keep working as plugins are
# added or changed. By default they connect to an in-process server (no port, no
# network) which exercises the real registration + dispatch path; pass ``--url``
# to test a running server instead.


def _connect(url):
    """Return a FastMCP ``Client`` for ``url`` (HTTP) or an in-memory server."""
    from fastmcp import Client  # pylint: disable=import-outside-toplevel
    if url:
        return Client(url)
    from robovast.mcp_server.server import create_server  # pylint: disable=import-outside-toplevel
    return Client(create_server())


def _quiet_logging():
    import logging  # pylint: disable=import-outside-toplevel
    logging.basicConfig(level=logging.WARNING)
    logging.getLogger("mcp").setLevel(logging.CRITICAL)
    logging.getLogger("robovast.mcp_server").setLevel(logging.WARNING)


def _coerce(value, prop_schema):
    """Coerce a ``key=value`` string to the JSON type declared in the tool schema."""
    json_type = (prop_schema or {}).get("type")
    try:
        if json_type == "integer":
            return int(value)
        if json_type == "number":
            return float(value)
        if json_type == "boolean":
            return value.strip().lower() in ("1", "true", "yes", "on")
        if json_type in ("array", "object"):
            return json.loads(value)
    except (ValueError, json.JSONDecodeError) as e:
        raise click.ClickException(
            f"Cannot parse {value!r} as {json_type}: {e}") from e
    # Unknown/string: try JSON for convenience, else pass through as a string.
    if json_type is None:
        try:
            return json.loads(value)
        except (ValueError, json.JSONDecodeError):
            return value
    return value


def _print_tool_params(tool):
    """Print a tool's parameters from its JSON Schema."""
    props = (tool.inputSchema or {}).get("properties", {})
    required = set((tool.inputSchema or {}).get("required", []))
    for name, spec in props.items():
        flag = "required" if name in required else "optional"
        default = spec.get("default")
        default_txt = "" if default is None else f", default={default!r}"
        line = f"    {name}: {spec.get('type', 'any')} ({flag}{default_txt})"
        desc = spec.get("description")
        if desc:
            line += f" — {desc}"
        click.echo(line)


@mcp.command(name="tools")
@click.argument("tool_name", required=False)
@click.option("--url", default=None,
              help="Connect to a running server at this URL instead of in-memory.")
@click.option("--schema", is_flag=True, help="Also print each tool's parameters.")
def tools_cmd(tool_name, url, schema):
    """List the MCP tools exposed by the server (discovered at runtime).

    With no argument, lists every tool. Pass a TOOL_NAME to print just that
    tool's full description and parameter schema.
    """
    _quiet_logging()

    async def _run():
        async with _connect(url) as client:
            return await client.list_tools()

    discovered = asyncio.run(_run())

    if tool_name:
        tool = next((t for t in discovered if t.name == tool_name), None)
        if tool is None:
            names = ", ".join(sorted(t.name for t in discovered))
            raise click.ClickException(
                f"Unknown tool {tool_name!r}. Available: {names}")
        click.echo(tool.name)
        if tool.description:
            click.echo(tool.description.strip())
        click.echo("Parameters:")
        _print_tool_params(tool)
        return

    for tool in sorted(discovered, key=lambda t: t.name):
        summary = (tool.description or "").strip().splitlines()
        click.echo(f"{tool.name}\t{summary[0] if summary else ''}")
        if schema:
            _print_tool_params(tool)


@mcp.command(name="call",
             context_settings={"ignore_unknown_options": True})
@click.argument("tool")
@click.argument("params", nargs=-1)
@click.option("--json", "json_payload", default=None,
              help='Full argument object as a JSON string (merged under key=value pairs).')
@click.option("--url", default=None,
              help="Connect to a running server at this URL instead of in-memory.")
@click.option("--raw", is_flag=True, help="Print raw content blocks, not the structured result.")
def call_cmd(tool, params, json_payload, url, raw):
    """Call an MCP tool. Arguments are ``key=value`` pairs coerced from the tool schema.

    The tool's parameter schema is fetched at runtime, so the accepted keys and
    their types are always in sync with the server.

    Examples::

      vast mcp call validate_project
      vast mcp call start_campaign backend=local runs=1
      vast mcp call get_campaign_status campaign_id=demo-2026-07-15-16575012
    """
    _quiet_logging()

    async def _run():
        async with _connect(url) as client:
            tool_list = await client.list_tools()
            spec = next((t for t in tool_list if t.name == tool), None)
            if spec is None:
                names = ", ".join(sorted(t.name for t in tool_list))
                raise click.ClickException(
                    f"Unknown tool {tool!r}. Available: {names}")
            props = (spec.inputSchema or {}).get("properties", {})
            required = set((spec.inputSchema or {}).get("required", []))

            args = {}
            if json_payload:
                try:
                    args.update(json.loads(json_payload))
                except json.JSONDecodeError as e:
                    raise click.ClickException(f"--json is not valid JSON: {e}") from e
            for pair in params:
                if "=" not in pair:
                    raise click.ClickException(
                        f"Expected key=value, got {pair!r}")
                key, value = pair.split("=", 1)
                if key not in props:
                    click.echo(f"Warning: {key!r} is not a declared parameter "
                               f"of {tool!r}", err=True)
                args[key] = _coerce(value, props.get(key))

            # Fail fast with a helpful message if a required argument is missing,
            # rather than surfacing the server's validation traceback.
            missing = [k for k in required if k not in args]
            if missing:
                lines = [f"Missing required argument(s) for {tool!r}: "
                         f"{', '.join(sorted(missing))}.", "Parameters:"]
                for name, pspec in props.items():
                    flag = "required" if name in required else "optional"
                    lines.append(f"    {name}: {pspec.get('type', 'any')} ({flag})")
                lines.append(f"Example: vast mcp call {tool} "
                             + " ".join(f"{k}=<value>" for k in sorted(required)))
                raise click.ClickException("\n".join(lines))

            try:
                return await client.call_tool(tool, args)
            except click.ClickException:
                raise
            except Exception as e:  # noqa: BLE001 - present tool errors cleanly
                raise click.ClickException(f"Tool {tool!r} failed: {e}") from e

    result = asyncio.run(_run())
    if raw:
        for block in result.content:
            click.echo(getattr(block, "text", block))
        return
    data = result.data if result.data is not None else result.structured_content
    click.echo(json.dumps(data, indent=2, default=str))
    # Non-zero exit on a protocol error or the plugins' ``{"error": ...}``
    # convention, so scripts and test harnesses can detect failures.
    if result.is_error or (isinstance(data, dict) and "error" in data):
        raise SystemExit(1)
