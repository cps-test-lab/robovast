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

Mounted by ``vast serve`` at ``/mcp`` on the service's own port, so one port (and one
token) reaches the web UI, the REST API and the MCP tools together. There is no separate
process to start: a client registers the URL, which ``vast serve``, ``vast login`` and
``vast exec cluster token`` each print.

All tools are provided by plugins registered under the ``robovast.mcp_plugins``
entry-point group.
"""

import contextvars
import json
import logging

from fastmcp import FastMCP
from mcp.types import Icon

from .registry import load_plugins

logger = logging.getLogger(__name__)

DEFAULT_HOST = "127.0.0.1"
DEFAULT_PORT = 8801
_MAX_REPR = 400  # max chars for logged values

#: The robovast logger subtree whose WARNING+ records are forwarded into tool
#: results. Third-party warnings are intentionally excluded.
_CAPTURED_LOGGER = "robovast"

#: Per-call sink for captured warnings. Set by the forwarding middleware for the
#: duration of each tool call; contextvars are task-local, so concurrent calls
#: never see each other's warnings.
_warning_sink: contextvars.ContextVar[list | None] = contextvars.ContextVar(
    "robovast_mcp_warning_sink", default=None
)


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


class _WarningCaptureHandler(logging.Handler):
    """Collect WARNING+ messages into the active per-call sink, if any.

    Installed once on the ``robovast`` logger. It only *records* messages; the
    logger's own console handlers still emit them as before, so normal output is
    unchanged.
    """

    def __init__(self):
        super().__init__(level=logging.WARNING)

    def emit(self, record: logging.LogRecord) -> None:
        sink = _warning_sink.get()
        if sink is not None and record.levelno >= logging.WARNING:
            sink.append(record.getMessage())


def _install_warning_forwarding(mcp: FastMCP) -> None:
    """Forward WARNING+ logs emitted during a tool call into that call's result.

    Non-fatal advisories (e.g. an ``execution.run_files`` pattern that matched
    nothing) are logged, never returned, so an MCP client never sees them. This
    middleware captures them generically for *every* tool and appends them to the
    result as an extra text block plus a ``meta.warnings`` list — no per-tool code.
    """
    from fastmcp.server.middleware import Middleware  # pylint: disable=import-outside-toplevel
    from fastmcp.server.middleware import MiddlewareContext
    from mcp.types import TextContent  # pylint: disable=import-outside-toplevel

    logging.getLogger(_CAPTURED_LOGGER).addHandler(_WarningCaptureHandler())

    class _WarningForwardingMiddleware(Middleware):
        async def on_call_tool(self, context: MiddlewareContext, call_next):  # type: ignore[override]
            sink: list[str] = []
            token = _warning_sink.set(sink)
            try:
                result = await call_next(context)
            finally:
                _warning_sink.reset(token)
            if sink:
                header = f"⚠ {len(sink)} warning(s) during {context.message.name}:"
                text = "\n".join([header, *(f"  - {m}" for m in sink)])
                try:
                    result.content = list(result.content) + [TextContent(type="text", text=text)]
                    result.meta = {**(result.meta or {}), "warnings": list(sink)}
                except Exception:  # noqa: BLE001 - never let attaching warnings break a call
                    logger.debug("Could not attach warnings to result.", exc_info=True)
            return result

    mcp.add_middleware(_WarningForwardingMiddleware())


def _install_debug_logging(mcp: FastMCP, level: int) -> None:
    """Install middleware to emit human-readable request/reply log lines.

    ``level`` >= 1 logs each tool call with its arguments; ``level`` >= 2 also
    logs the (successful) result. Errors are always logged when any level is on.
    """
    from fastmcp.server.middleware import Middleware  # pylint: disable=import-outside-toplevel
    from fastmcp.server.middleware import MiddlewareContext

    class _DebugLoggingMiddleware(Middleware):
        async def on_call_tool(self, context: MiddlewareContext, call_next):  # type: ignore[override]
            args_repr = ", ".join(
                f"{k}={_short(v)}" for k, v in (context.message.arguments or {}).items()
            )
            logger.debug("→ %s(%s)", context.message.name, args_repr)
            try:
                result = await call_next(context)
                if level >= 2:
                    logger.debug("← %s → %s", context.message.name, _short(_extract_result(result)))
                return result
            except Exception as exc:
                logger.debug("← %s ✗ %s: %s", context.message.name, type(exc).__name__, exc)
                raise

    mcp.add_middleware(_DebugLoggingMiddleware())


#: What every MCP client injects into the model's system prompt. This is the only text
#: read before any tool is chosen, so it is where the server says what it is *for*.
#:
#: It used to read "This server provides access to the results created by RoboVAST" —
#: true, and the reason agents kept running experiments by hand on the host and then
#: coming here to read files. A server that introduces itself as an archive is not
#: offered as a place to run anything; the execution half of the surface went unused
#: while ``docker compose`` runs produced results with no pinned image, no recorded
#: provenance and no repetitions, which cannot be compared with anything.
_INSTRUCTIONS = """\
RoboVAST runs robotics experiments and keeps what they produced.

**Run experiments here, not on this host.** A campaign executes in a pinned container
image on a local Docker or Kubernetes lane, repeats each configuration, and records its
provenance, so its results are comparable and reproducible. A `docker compose`, a
`pytest`, or a simulator started by hand has none of that: it answers a different
question and its output cannot be compared with a campaign's. If a task needs a
simulation run, a sweep, or a repeated trial, that is `start_campaign`.

The loop:
1. `create_workspace`, then `write_file` to put a `.vast` in it.
2. `validate_project` — reports every problem at once, before any compute is spent.
3. `build_experiment_image` when a container adds packages, then
   `wait_for_image_build`, then `exec_in_container` to check that image — an import,
   `ros2 pkg list`, a file check, or one config's scenario. Seconds here, and it produces
   no campaign data; the same mistake found by a campaign costs the campaign.
4. `preview_configurations` — what the sweep actually expands to.
5. `get_resource_usage` — does this lane have room, and is it reachable?
6. `start_campaign` — **pilot one configuration first** (`config_filter`, `runs=1`),
   then the full sweep. Always pass `description`.
7. **Wait for it** — background `vast wait <campaign_id>`, the shell command
   `start_campaign` hands back in `next_step`. It exits when the campaign is genuinely
   over (past postprocessing), so you stay free meanwhile instead of holding a tool call
   open for a run that may take days. `get_campaign_status` is the single-read version.
   A campaign nobody waits for is one whose end nobody notices; if you will not wait, say
   so and say that ntfy announces the end instead.
8. Read results with SQL: `describe_campaign_data`, then `query_campaign_data_sql`.

If no service is reachable, every control tool says so. **Stop and report that** — do
not substitute a local run, which silently answers a different question.

Files live at `/results/<campaign_id>/<path>` (read-only) and
`/sources/<workspace_id>/<path>` (writable) — one address space, five tools.
"""


def create_server(
    host: str = DEFAULT_HOST,
    port: int = DEFAULT_PORT,
    debug: int = 0,
) -> FastMCP:
    """Create and configure the MCP server instance.

    Parameters
    ----------
    host:
        Host to bind when using an HTTP transport.
    port:
        Port to bind when using an HTTP transport.
    debug:
        Verbosity for the human-readable request/reply log at ``DEBUG`` level.
        ``0`` disables it, ``1`` logs each tool call with its arguments, and
        ``2`` also logs the result.
    """
    mcp = FastMCP(name="RoboVAST", instructions=_INSTRUCTIONS,
                icons=[
                    Icon(
                        src="https://raw.githubusercontent.com/cps-test-lab/robovast/refs/heads/main/docs/images/icon.png",
                        mimeType="image/png",
                        sizes=["any"]
                    ),
                ])

    plugins = load_plugins(mcp)
    plugin_names = [p.name for p in plugins]

    _install_warning_forwarding(mcp)

    logger.info(
        f"Started MCP server: host={host}, port={port}, debug={debug}, plugins=[{', '.join(plugin_names)}]"
    )

    if debug:
        _install_debug_logging(mcp, debug)

    return mcp


if __name__ == "__main__":
    raise SystemExit("The MCP server is mounted by 'vast serve' at /mcp; "
                     "there is no separate process to start.")
