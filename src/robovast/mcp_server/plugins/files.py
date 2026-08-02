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

"""File access over one address space.

Every file RoboVAST can reach has a single address, which is also the URL that serves
it. Two namespaces, and the namespace carries the permission:

* ``/results/<campaign_id>/<path>`` — a campaign's outputs, **read-only**.
* ``/sources/<workspace_id>/<path>`` — a workspace's authored inputs, writable.

This replaced roughly a dozen per-scope tools — one reader and one lister for each of
campaign run files, campaign transient files, configuration config/transient files, run
output files, and workspace project files — which were the same two operations behind
different argument names and different prefix conventions. Their names are deliberately
not repeated anywhere in the source or docs: a retired tool name in a docstring is one
an LLM will try to call.
"""

import logging

from fastmcp import FastMCP

logger = logging.getLogger(__name__)

#: The campaign layout, carried by ``list_files``. It is the discoverability the retired
#: per-scope tool *names* used to provide — a tool called "list the campaign's transient
#: files" told a reader that transient files exist and where. One table costs a fraction
#: of the ten tool schemas it replaces, and unlike them it also covers what has no tool.
#:
#: Attached to ``list_files`` alone, not to both discovery tools: every tool description
#: is sent on every request, so a table repeated across two of them is paid for twice per
#: turn, and the tool that *finds* a path is the one that needs it.
_LAYOUT = """
Under ``/results/<campaign_id>/``:
  _config/              scenario.osc, <name>.vast, run files, notebooks
  _execution/           outcome.json (why it ended), execution.yaml, controller.log,
                        postprocessing.log, data.db (query with SQL, do not read)
  _transient/           configurations.yaml, entrypoint.sh
  _jobs/job-N/          sysinfo.yaml, resource_usage_*.csv, logs/system.log
  <config_name>/        _config/ (config.yaml, maps/), _transient/, one dir per run
  <config_name>/<run>/  test.xml (JUnit), out.csv, rosbag2/, scene/scene.json

Under ``/sources/<workspace_id>/``: whatever the project author wrote.
"""


def _client():
    """A client for the file operations: the service when one answers, else local disk.

    The control tools require a service because they need an execution authority. Files
    do not: a campaign directory on this host is readable with no service running, which
    is how ``vast results``/``vast eval`` have always worked. So the fallback is an
    explicit in-process ``LocalTransport`` — constructed deliberately here rather than
    obtained by passing an empty URL to ``RobovastClient``, where "no service" would be
    substituted for a reachable one without anyone deciding it.
    """
    from robovast.mcp_server import service_access
    return service_access.client_or_local()


def list_files(address: str, recursive: bool = False, offset: int = 0,
               limit: int = 100) -> dict:
    """List a directory. **This is how you find a campaign's configuration names.**

    SQL knows only configurations that produced runs, so on a stopped or partly-run
    campaign the directory listing is the complete one.

    Args:
        address: ``/results/<campaign_id>/<path>`` (read-only) or
            ``/sources/<workspace_id>/<path>``. A campaign root is
            ``/results/<campaign_id>/``.
        recursive: Walk the subtree (files only). Off by default — a campaign root has a
            directory per configuration and per run, so recursion means thousands.
        offset: First entry to return (entry index).
        limit: Maximum entries; ``total`` reports how many there were.

    Returns:
        ``{address, entries, total, truncated, recursive}``. Directory entries end in
        ``/``; each is relative to ``address``, so the next address is ``address + entry``.
    """
    try:
        r = _client().list_files(address, recursive=recursive, offset=offset, limit=limit)
        return r.model_dump(exclude={"detailed"})
    except Exception as e:  # noqa: BLE001
        return {"error": str(e)}


def read_file(address: str, limit: int = 200, offset: int = 0) -> dict:
    """Read a page of a text file, by its address. ``list_files`` shows what is there.

    Args:
        address: ``/results/<campaign_id>/<path>`` (read-only) or
            ``/sources/<workspace_id>/<path>``, e.g.
            ``/results/nav-2026-03-04-152130/_execution/outcome.json``.
        limit: Maximum lines to return (``0`` = the whole file).
        offset: First line to return (line index).

    Returns:
        ``{address, total_lines, returned_lines, offset, content}``. Binary files are
        refused rather than mangled — fetch those over HTTP or with ``vast files get``.
    """
    try:
        return _client().read_file(address, lines=limit, offset=offset).model_dump()
    except Exception as e:  # noqa: BLE001
        return {"error": str(e)}


def write_file(address: str, content: str) -> dict:
    """Write a ``.vast``/``.osc`` file. ``/sources`` only — results are immutable.

    Only these two authored text types may be written inline; anything else (run
    scripts, notebooks, binaries) uses ``create_upload`` so its bytes never pass through
    your context. Returns metadata (``address``/``bytes``/``sha256``), never the content.

    Args:
        address: ``/sources/<workspace_id>/<path>``, e.g. ``/sources/ws-ab12/demo.vast``.
        content: File text.
    """
    from robovast.service.interface import WriteFileRequest
    try:
        return _client().write_file(
            WriteFileRequest(address=address, content=content)).model_dump()
    except Exception as e:  # noqa: BLE001
        return {"error": str(e)}


def edit_file(address: str, old_string: str, new_string: str) -> dict:
    """Replace a **unique** substring in a ``.vast``/``.osc`` file (cheap fix loop).

    Send a small diff instead of re-uploading the whole file. ``old_string`` must occur
    exactly once; include surrounding context to disambiguate.

    Args:
        address: ``/sources/<workspace_id>/<path>``.
        old_string: Text to replace; must be unique in the file.
        new_string: Replacement text.
    """
    from robovast.service.interface import EditFileRequest
    try:
        return _client().edit_file(EditFileRequest(
            address=address, old_string=old_string,
            new_string=new_string)).model_dump()
    except Exception as e:  # noqa: BLE001
        return {"error": str(e)}


def delete_file(address: str) -> dict:
    """Delete a file. ``/sources`` only.

    Args:
        address: ``/sources/<workspace_id>/<path>``.
    """
    try:
        return _client().delete_file(address).model_dump()
    except Exception as e:  # noqa: BLE001
        return {"error": str(e)}


list_files.__doc__ = f"{list_files.__doc__}\n{_LAYOUT}"


# -- Plugin class ------------------------------------------------------------

_TOOLS = [
    list_files,
    read_file,
    write_file,
    edit_file,
    delete_file,
]


class FilesPlugin:
    """Expose the file address space as MCP tools."""

    name = "files"

    def register(self, mcp: FastMCP) -> None:
        for fn in _TOOLS:
            mcp.tool()(fn)
