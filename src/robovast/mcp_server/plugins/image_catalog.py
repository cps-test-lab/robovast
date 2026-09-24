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

One pair, ``list_image_catalog``/``get_image_catalog_entry``, over both catalogs a
``catalog`` argument selects. They were four tools calling these same two helpers with a
different constant, so the surface carried four descriptions of one question -- and the
two that named their catalog in the tool name were the two callers most often got wrong,
because nothing about them said they still needed an ``address``.

* ``scenario_actions`` -- every action/modifier/actor/struct a ``.osc`` file can reference
  in the image (``python3 -m scenario_execution.introspection list-actions``, run inside it).
* ``roqsim_plugins`` -- every ``roqsim.plugins`` entry a world YAML's ``components:`` list
  can add in the image (``python3 -m roqsim.introspection list``, run inside it).

**Caching.** The catalog only changes when the image does, so a fetched catalog is kept in
this process's memory, keyed by ``(resolved image, group)`` -- and, where a group's list is
only a summary, one entry's detail by ``(resolved image, group, name)``. The cache is
:mod:`robovast.service.image_catalog`'s, shared with the world check in ``validate_project``,
so with the MCP app mounted in the service a plugin one of them described is not asked again
for the other. The first request for a given plugin's detail costs a round trip, which is what
a list fat enough to carry every plugin's parameters would have charged every caller of the
*list* instead. An MCP server running as its own process has its own copy and pays for its
own first fetch per image -- a known, accepted narrowing. Resolving the image is itself a
service call (:meth:`~robovast.service.interface.RobovastInterface.resolve_image`) that
starts no container, so a cache hit costs one cheap round trip, not zero.
"""

import logging
import time

from fastmcp import FastMCP

from robovast.mcp_server import service_access
from robovast.mcp_server.service_access import NO_SERVICE
# The catalog commands, their parsing and the per-image cache live in the service, where the
# world check in validate_project reads the same catalogs: one cache for both readers.
from robovast.service.image_catalog import (CACHE_LOCK, CATALOG_COMMANDS, CATALOG_CONTAINERS,
                                            DETAIL_COMMANDS, LIST_CACHE, CatalogUnavailable,
                                            catalog_json, fetch_details)

logger = logging.getLogger(__name__)


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
        resolved = client.resolve_image(
            ExecRequest(**request_kwargs, container=CATALOG_CONTAINERS[group]))
    except Exception as e:  # noqa: BLE001
        return {"error": str(e)}
    image = resolved.image

    key = (image, group)
    with CACHE_LOCK:
        cached = LIST_CACHE.get(key)
    if cached is not None:
        return {"items": cached, "image": image, "cache": {"hit": True, "seconds": 0.0}}

    started = time.monotonic()
    try:
        result = client.exec_in_container(ExecRequest(
            **request_kwargs, command=CATALOG_COMMANDS[group],
            container=CATALOG_CONTAINERS[group],
            # A read-only introspection of the image: it belongs in the service's query
            # pool, never in the caller's container. Without this every catalog call
            # stopped whatever they were holding -- a one-shot exec discards the held
            # container by design -- so listing scenario actions destroyed their debugging
            # session and anything they had written in it.
            query=True))
    except Exception as e:  # noqa: BLE001
        # error_result rather than {"error": str(e)}: the catalog is answered by a command
        # in a container, so "nothing can run one here" is one of the answers, and it is a
        # fact about the deployment rather than about this image.
        return service_access.error_result(e)
    elapsed = time.monotonic() - started
    if result.exit_code != 0:
        detail = (result.stderr or result.stdout or "").strip()[:400]
        return {"error": f"introspecting {group} in {image} failed: {detail or '(no output)'}"}
    try:
        payload = catalog_json(result.stdout)
    except ValueError:
        # The captured output, not just the decoder's complaint: the command exited 0, so
        # whatever is on the stream is the only evidence of what happened, and a message
        # that withholds it leaves the caller with nothing to act on.
        seen = " ".join((result.stdout or "").split())[:300] or "(no output)"
        return {"error": (
            f"no {group} catalog in the output from {image} -- the command exited 0 but "
            f"printed no JSON object. Run it yourself to see the whole stream: "
            f"exec_in_container(container={CATALOG_CONTAINERS[group]!r}, "
            f"command={CATALOG_COMMANDS[group]!r}). Output began: {seen}")}

    items = _flatten(group, payload)
    with CACHE_LOCK:
        LIST_CACHE[key] = items
    return {"items": items, "image": image, "cache": {"hit": False, "seconds": elapsed}}


#: What one line of each catalog carries. A listing is read to CHOOSE, so the fields are the ones a
#: choice turns on -- and they differ by catalog. A model's `components` is the capability answer
#: (a `turtlebot4` carries `diff_drive`, `lidar`, `oakd_camera`; a `piracer` carries
#: `ackermann_drive` and no lidar), and `ref` is what a world actually writes, so projecting a
#: model onto `kind`/`doc` would return a name and two nulls.
_SUMMARY_FIELDS = {
    "models": ("name", "ref", "provider", "components"),
    "worlds": ("name", "ref", "kind", "summary"),
}
#: For a catalog that declares none: the shape both introspection catalogs return.
_DEFAULT_SUMMARY_FIELDS = ("name", "kind", "doc")


def _fetch_catalog(group: str, address: str) -> dict:
    """:func:`_fetch` for a reader outside this module -- the docs corpus is one."""
    return _fetch(group, address)


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
    fields = _SUMMARY_FIELDS.get(group, _DEFAULT_SUMMARY_FIELDS)
    summaries = [{f: i.get(f) for f in fields} for i in items]
    return {"items": summaries, "total": len(summaries),
            "image": fetched["image"], "cache": fetched["cache"]}


def _fetch_detail(group: str, address: str, name: str) -> dict:
    """One entry's full detail, asked of the image BY NAME and cached per name.

    For the group whose list is a summary. Costs a container round trip the first time a given
    name is asked for, which is what the alternative -- a list fat enough to carry every
    plugin's parameters -- would have charged every caller of the list instead.
    """
    from robovast.service.image_catalog import DETAIL_NAME_RE, ENTRY_NAME_RE
    from robovast.service.interface import ExecRequest

    if not DETAIL_NAME_RE.get(group, ENTRY_NAME_RE).fullmatch(name):
        return {"error": f"{name!r} is not an entry-point name"}
    try:
        request_kwargs = _address_to_request_kwargs(address)
    except ValueError as e:
        return {"error": str(e)}
    client = service_access.service_client()
    if client is None:
        return {"error": NO_SERVICE}
    try:
        resolved = client.resolve_image(
            ExecRequest(**request_kwargs, container=CATALOG_CONTAINERS[group]))
    except Exception as e:  # noqa: BLE001
        return service_access.error_result(e)
    image = resolved.image

    try:
        fetched = fetch_details(client.exec_in_container, group=group, image=image,
                                request_kwargs=request_kwargs, names=[name])
    except CatalogUnavailable as e:
        return {"error": str(e)}
    except Exception as e:  # noqa: BLE001
        return service_access.error_result(e)
    payload = fetched["items"][name]
    if "error" in payload:
        return {"error": f"no {group.replace('_', ' ')} entry named {name!r} in {image}"}
    return {"item": payload, "image": image, "cache": fetched["cache"]}


def _details(group: str, address: str, name: str) -> dict:
    if group in DETAIL_COMMANDS:
        fetched = _fetch_detail(group, address, name)
        if "error" in fetched:
            return fetched
        return {**fetched["item"], "image": fetched["image"], "cache": fetched["cache"]}
    fetched = _fetch(group, address)
    if "error" in fetched:
        return fetched
    for item in fetched["items"]:
        if item["name"] == name:
            return {**item, "image": fetched["image"], "cache": fetched["cache"]}
    return {"error": f"no {group.replace('_', ' ')} entry named {name!r} in {fetched['image']}"}


#: The catalogs an experiment image carries. One vocabulary, so a caller learns the pair of
#: calls once rather than a pair per catalog.
CATALOGS = ("scenario_actions", "roqsim_plugins", "models", "worlds")


def _bad_catalog(catalog: str) -> dict:
    return {"error": f"unknown catalog {catalog!r}; known catalogs: {', '.join(CATALOGS)}"}


def list_image_catalog(address: str, catalog: str = "scenario_actions",
                       query: str = "") -> dict:
    """What an experiment image can express, one line per entry.

    ``scenario_actions``: what a scenario may use. ``roqsim_plugins``: what a world may
    declare. ``models``: what it can spawn, each with the components it carries.
    ``worlds``: the scenes it ships. A catalog belongs to a built image, so *address*
    (``/sources/<workspace_id>/<path>``) names which to read.
    """
    if catalog not in CATALOGS:
        return _bad_catalog(catalog)
    return _list(catalog, address, query)


def get_image_catalog_entry(address: str, name: str,
                            catalog: str = "scenario_actions") -> dict:
    """One entry in full. Same *address* and *catalog* as ``list_image_catalog``.

    An action: parameters, source library, doc, resolvability. A plugin: its config keys and,
    where it declares one, a typed schema -- what a world `components:` entry accepts. A model:
    its components with their defaults. ``worlds`` has no detail; its list carries every field.
    """
    if catalog not in CATALOGS:
        return _bad_catalog(catalog)
    return _details(catalog, address, name)


_TOOLS = [
    list_image_catalog,
    get_image_catalog_entry,
]


class ImageCatalogPlugin:
    name = "image_catalog"

    def register(self, mcp: FastMCP) -> None:
        for fn in _TOOLS:
            mcp.tool()(fn)
