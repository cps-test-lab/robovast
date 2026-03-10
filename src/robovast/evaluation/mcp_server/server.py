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

"""RoboVAST MCP server.

Start via the VAST CLI::

    vast eval mcp-server                                      # stdio (default)
    vast eval mcp-server --transport sse                      # legacy SSE on 0.0.0.0:8000
    vast eval mcp-server --transport streamable-http          # modern HTTP (Open WebUI etc.)
    vast eval mcp-server --transport streamable-http --host 127.0.0.1 --port 9000
    vast eval mcp-server --transport streamable-http --debug  # human-readable request/reply log

All tools are provided by plugins registered under the
``robovast.mcp_plugins`` entry-point group.
"""

import json
import logging

from mcp.server.fastmcp import FastMCP

from .registry import load_plugins

logger = logging.getLogger(__name__)

DEFAULT_HOST = "0.0.0.0"
DEFAULT_PORT = 8000
_MAX_REPR = 400  # max chars for logged values


def _extract_result(value: object) -> object:
    """Extract a plain Python value from an MCP ContentBlock result list.

    call_tool returns a list of TextContent / ImageContent / etc. objects.
    For logging we just want the decoded payload.
    """
    if not isinstance(value, (list, tuple)):
        return value
    texts = []
    for item in value:
        text = getattr(item, "text", None)
        if text is not None:
            # The text is often JSON-encoded – decode it for readability.
            try:
                texts.append(json.loads(text))
            except (ValueError, TypeError):
                texts.append(text)
        else:
            texts.append(repr(item))
    return texts[0] if len(texts) == 1 else texts


def _short(value: object) -> str:
    """Return a concise, single-line representation of *value*."""
    try:
        text = json.dumps(value, ensure_ascii=False, default=repr)
    except Exception:
        text = repr(value)
    if len(text) > _MAX_REPR:
        text = text[:_MAX_REPR] + "…"
    return text


def _install_debug_logging(mcp: FastMCP) -> None:
    """Wrap ToolManager.call_tool to emit human-readable request/reply log lines.

    mcp.call_tool is captured by reference during _setup_handlers(), so patching
    the instance attribute has no effect.  Patching the ToolManager instead works
    because FastMCP.call_tool delegates to it at call time.
    """
    tm = mcp._tool_manager  # pylint: disable=protected-access
    original = tm.call_tool

    async def logged_call_tool(name: str, arguments: dict, **kwargs: object) -> object:
        args_repr = ", ".join(f"{k}={_short(v)}" for k, v in (arguments or {}).items())
        logger.debug("→ %s(%s)", name, args_repr)
        try:
            result = await original(name, arguments, **kwargs)
            logger.debug("← %s → %s", name, _short(_extract_result(result)))
            return result
        except Exception as exc:
            logger.debug("← %s ✗ %s: %s", name, type(exc).__name__, exc)
            raise

    tm.call_tool = logged_call_tool  # type: ignore[method-assign]


def create_server(
    host: str = DEFAULT_HOST,
    port: int = DEFAULT_PORT,
    debug: bool = False,
) -> FastMCP:
    """Create and configure the MCP server instance.

    Parameters
    ----------
    host:
        Host to bind when using an HTTP transport.
    port:
        Port to bind when using an HTTP transport.
    debug:
        When *True*, wrap ``call_tool`` to emit human-readable request/reply
        log lines at ``DEBUG`` level.
    """
    mcp = FastMCP("robovast", host=host, port=port, log_level="CRITICAL")

    plugins = load_plugins(mcp)
    plugin_names = [p.name for p in plugins]

    logger.info(
        f"Started MCP server: host={host}, port={port}, debug={debug}, plugins=[{', '.join(plugin_names)}]"
    )

    if debug:
        _install_debug_logging(mcp)

    return mcp


if __name__ == "__main__":
    raise SystemExit("Use 'vast eval mcp-server' to start the MCP server.")
