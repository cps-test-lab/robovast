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

    vast eval mcp-server                                      # legacy SSE on 0.0.0.0:8000 (default)
    vast eval mcp-server --transport stdio                    # stdio (default)
    vast eval mcp-server --transport streamable-http          # modern HTTP (Open WebUI etc.)
    vast eval mcp-server --transport streamable-http --host 127.0.0.1 --port 9000
    vast eval mcp-server --transport streamable-http --debug  # human-readable request/reply log

All tools are provided by plugins registered under the
``robovast.mcp_plugins`` entry-point group.
"""

import json
import logging

from fastmcp import FastMCP
from mcp.types import Icon

from .registry import load_plugins

logger = logging.getLogger(__name__)

DEFAULT_HOST = "0.0.0.0"
DEFAULT_PORT = 8000
_MAX_REPR = 400  # max chars for logged values


def _extract_result(value: object) -> object:
    """Extract a plain Python value from a ToolResult or ContentBlock list.

    call_tool returns a ToolResult with a .content list of ContentBlock objects.
    For logging we just want the decoded payload.
    """
    # FastMCP v3: ToolResult has a .content attribute
    content = getattr(value, "content", None)
    if content is not None:
        value = content
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
    """Install middleware to emit human-readable request/reply log lines."""
    from fastmcp.server.middleware import Middleware, MiddlewareContext  # pylint: disable=import-outside-toplevel

    class _DebugLoggingMiddleware(Middleware):
        async def on_call_tool(self, context: MiddlewareContext, call_next):  # type: ignore[override]
            args_repr = ", ".join(
                f"{k}={_short(v)}" for k, v in (context.message.arguments or {}).items()
            )
            logger.debug("→ %s(%s)", context.message.name, args_repr)
            try:
                result = await call_next(context)
                logger.debug("← %s → %s", context.message.name, _short(_extract_result(result)))
                return result
            except Exception as exc:
                logger.debug("← %s ✗ %s: %s", context.message.name, type(exc).__name__, exc)
                raise

    mcp.add_middleware(_DebugLoggingMiddleware())


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
    mcp = FastMCP(name="RoboVAST Results API", instructions="""
                This server provides access to the results created by RoboVAST.
                """,
                icons=[
                    Icon(
                        src="https://raw.githubusercontent.com/cps-test-lab/robovast/refs/heads/main/docs/images/icon.png",
                        mimeType="image/png",
                        sizes=["any"]
                    ),
                ])

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
