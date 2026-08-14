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

"""MCP plugin reporting what a specific experiment image offers a scenario or a world.

Two catalogs, one tool pair each — mirroring ``list_plugins``/``get_plugin_details``'s own
vocabulary (``query`` for substring/glob, ``name`` for one exact item, list-vs-detail split)
without touching those tools: this is a different question (what can *this specific image*
offer, not what does robovast itself have), always needs an address, and can cost a
container round trip — three reasons this is its own pair, not a mode of the existing one.

* ``list_scenario_actions``/``get_scenario_action_details`` -- every action/modifier/
  actor/struct a ``.osc`` file can reference in the image (``python -m
  scenario_execution.introspection list-actions``, run inside it).
* ``list_roqsim_plugins``/``get_roqsim_plugin_details`` -- every ``roqsim.plugins`` entry
  a world YAML's ``plugins:`` list can add in the image (``python -m roqsim.introspection
  list``, run inside it).

**Caching.** The catalog only changes when the image does, so a fetched catalog is kept in
this process's memory, keyed by ``(resolved image, group)`` -- a real cache, but scoped to
this MCP server process rather than the (potentially remote, potentially shared-by-many)
``robovast-service`` it talks to: every *other* MCP session paying for its own first fetch
per image is a known, accepted narrowing, not an oversight. Resolving the image is itself a
service call (:meth:`~robovast.service.interface.RobovastInterface.resolve_image`) that
starts no container, so a cache hit costs one cheap round trip, not zero.
"""

import json
import logging
import threading
import time

from fastmcp import FastMCP

from robovast.mcp_server import service_access
from robovast.mcp_server.service_access import NO_SERVICE

logger = logging.getLogger(__name__)

_CATALOG_COMMANDS = {
    "scenario_actions": "python -m scenario_execution.introspection list-actions",
    "roqsim_plugins": "python -m roqsim.introspection list",
}

_cache_lock = threading.Lock()
#: (image, group) -> flattened items. Process-lifetime only -- see module docstring.
_cache: dict[tuple, list] = {}


def _flatten(group: str, payload: dict) -> list:
    """The container's raw JSON into one flat item list, whatever its native shape.

    ``scenario_execution.introspection list-actions`` buckets by kind
    (``{"actions": [...], "modifiers": [...], ...}``); each item already carries its own
    ``kind``, so flattening loses nothing. ``roqsim.introspection list`` is already flat
    (``{"items": [...]}``).
    """
    if group == "scenario_actions":
        items = []
        for bucket in payload.values():
            items.extend(bucket)
        return items
    return payload.get("items", [])


def _address_to_request_kwargs(address: str) -> dict:
    """``/sources/<workspace_id>/<path>`` -> kwargs for :class:`ExecRequest`, or raise.

    No local-file fallback here, unlike ``validate_project``'s: there is no image to
    resolve for a path the service cannot see, and a container-backed catalog is
    meaningless without one.
    """
    from robovast.mcp_server.plugins.authoring import _address_lane
    from robovast.service.project_push import _resolve_workspace_id
    target = _address_lane(address)
    if target is None:
        raise ValueError(
            "this needs a workspace address (/sources/<workspace_id>/<path>): the "
            "catalog is answered by the image this project resolves to, which only "
            "the service knows how to reach")
    workspace_id, rel_path = target
    client = service_access.service_client()
    if client is None:
        raise ValueError(NO_SERVICE)
    return {"workspace_id": _resolve_workspace_id(client, workspace_id), "config_path": rel_path}


def _fetch(group: str, address: str) -> dict:
    """The full catalog for *group* in *address*'s resolved image, cached by image.

    Returns ``{items, image, cache: {hit, seconds}}`` or ``{"error": "..."}``.
    """
    from robovast.service.interface import ExecRequest

    try:
        request_kwargs = _address_to_request_kwargs(address)
    except ValueError as e:
        return {"error": str(e)}
    client = service_access.service_client()
    if client is None:
        return {"error": NO_SERVICE}

    try:
        resolved = client.resolve_image(ExecRequest(**request_kwargs))
    except Exception as e:  # noqa: BLE001
        return {"error": str(e)}
    image = resolved.image

    key = (image, group)
    with _cache_lock:
        cached = _cache.get(key)
    if cached is not None:
        return {"items": cached, "image": image, "cache": {"hit": True, "seconds": 0.0}}

    started = time.monotonic()
    try:
        result = client.exec_in_container(
            ExecRequest(**request_kwargs, command=_CATALOG_COMMANDS[group]))
    except Exception as e:  # noqa: BLE001
        return {"error": str(e)}
    elapsed = time.monotonic() - started
    if result.exit_code != 0:
        detail = (result.stderr or result.stdout or "").strip()[:400]
        return {"error": f"introspecting {group} in {image} failed: {detail or '(no output)'}"}
    try:
        payload = json.loads(result.stdout)
    except json.JSONDecodeError as e:
        return {"error": f"could not parse {group} catalog output from {image}: {e}"}

    items = _flatten(group, payload)
    with _cache_lock:
        _cache[key] = items
    return {"items": items, "image": image, "cache": {"hit": False, "seconds": elapsed}}


def _list(group: str, address: str, query: str) -> dict:
    fetched = _fetch(group, address)
    if "error" in fetched:
        return fetched
    items = fetched["items"]
    if query:
        needle = query.lower()
        import fnmatch  # pylint: disable=import-outside-toplevel
        if any(c in query for c in ("*", "?", "[")):
            items = [i for i in items if fnmatch.fnmatch(i["name"].lower(), needle)]
        else:
            items = [i for i in items if needle in i["name"].lower()]
    summaries = [{"name": i["name"], "kind": i.get("kind"), "doc": i.get("doc")} for i in items]
    return {"items": summaries, "total": len(summaries),
            "image": fetched["image"], "cache": fetched["cache"]}


def _details(group: str, address: str, name: str) -> dict:
    fetched = _fetch(group, address)
    if "error" in fetched:
        return fetched
    for item in fetched["items"]:
        if item["name"] == name:
            return {**item, "image": fetched["image"], "cache": fetched["cache"]}
    return {"error": f"no {group.replace('_', ' ')} entry named {name!r} in {fetched['image']}"}


def list_scenario_actions(address: str, query: str = "") -> dict:
    """`.osc` action/modifier/actor/struct catalog, one line each. `{items, total,
    image, cache}`.
    """
    return _list("scenario_actions", address, query)


def get_scenario_action_details(address: str, name: str) -> dict:
    """One catalog entry's detail: parameters, source, doc, resolvability."""
    return _details("scenario_actions", address, name)


def list_roqsim_plugins(address: str, query: str = "") -> dict:
    """`roqsim.plugins` catalog, one line each. Same shape as `list_scenario_actions`."""
    return _list("roqsim_plugins", address, query)


def get_roqsim_plugin_details(address: str, name: str) -> dict:
    """One plugin's detail, parsed from its `Config::` block. Same shape as
    `get_scenario_action_details`.
    """
    return _details("roqsim_plugins", address, name)


_TOOLS = [
    list_scenario_actions,
    get_scenario_action_details,
    list_roqsim_plugins,
    get_roqsim_plugin_details,
]


class ImageCatalogPlugin:
    name = "image_catalog"

    def register(self, mcp: FastMCP) -> None:
        for fn in _TOOLS:
            mcp.tool()(fn)
