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
only a summary, one entry's detail by ``(resolved image, group, name)``. The
first request for a given plugin's detail therefore costs a round trip, which is what a list fat
enough to carry every plugin's parameters would have charged every caller of the *list* instead -- a real cache, but scoped to
this MCP server process rather than the (potentially remote, potentially shared-by-many)
``robovast-service`` it talks to: every *other* MCP session paying for its own first fetch
per image is a known, accepted narrowing, not an oversight. Resolving the image is itself a
service call (:meth:`~robovast.service.interface.RobovastInterface.resolve_image`) that
starts no container, so a cache hit costs one cheap round trip, not zero.
"""

import json
import logging
import re
import threading
import time

from fastmcp import FastMCP

from robovast.mcp_server import service_access
from robovast.mcp_server.service_access import NO_SERVICE

logger = logging.getLogger(__name__)

_CATALOG_COMMANDS = {
    # ``python3`` and not ``python``: the only interpreter a DECLARED base image is
    # guaranteed to have. Debian/Ubuntu ship no ``python`` at all (PEP 394 -- the name meant
    # Python 2, and it exists only via the optional ``python-is-python3``), while an image
    # RoboVAST *built* does have one, because the venv at /usr/local provides it. So a bare
    # ``python`` worked for a project that builds its scenario image and failed with
    # "python: command not found" for every project that declares one -- which is the common
    # case, and why this went unnoticed. Adding the alias to our own images would have fixed
    # only our images: `execution.containers.<name>.image` lets a campaign pin any base, so a
    # tool's contract must not depend on a package the substrate cannot guarantee.
    "scenario_actions": "python3 -m scenario_execution.introspection list-actions",
    "roqsim_plugins": "python3 -m roqsim.introspection list",
    # What the image can SPAWN, as against what it can configure. A campaign names a robot and a
    # world by ref, and until these were served the only way to learn a ref was to read the
    # simulator's source -- so the two decisions a world makes first were the two nothing answered.
    "models": "python3 -m roqsim.catalog models",
    "worlds": "python3 -m roqsim.catalog worlds",
}

#: How a group answers a request for ONE entry's detail. ``scenario_execution``'s list already
#: carries every field a detail call returns, so that group is answered from the cached list and
#: has no entry here. ``roqsim.introspection list`` is a summary -- name, kind, doc, flags,
#: package -- so a plugin's parameters exist only behind ``describe``, and filtering the list for
#: them returned an entry with no parameters at all.
_DETAIL_COMMANDS = {
    "roqsim_plugins": "python3 -m roqsim.introspection describe",
    # A model's detail is where its components are -- which is the capability answer: a
    # `turtlebot4` carries `diff_drive`, `lidar` and `oakd_camera`, a `piracer` carries
    # `ackermann_drive` and no lidar. `worlds` has no detail command; its list already carries
    # every field one would return.
    "models": "python3 -m roqsim.catalog model",
}

#: What a detail command is ever given: an entry-point name, or a `<provider>:<name>` model ref.
#: The name reaches a shell in the container, so it is checked against this rather than quoted: a
#: name outside it is a mistake in the call, and refusing is a better answer than escaping it and
#: asking anyway.
_ENTRY_NAME_RE = re.compile(r"[A-Za-z_][A-Za-z0-9_.-]*(?::[A-Za-z_][A-Za-z0-9_.-]*)?")

#: Which container answers each group. ``roqsim`` lives in the *simulator's* image, not the
#: scenario's, and asking the default container for it got "roqsim: command not found" on
#: any project whose simulator image comes from the family.
_CATALOG_CONTAINERS = {
    "scenario_actions": "scenario",
    "roqsim_plugins": "simulation",
    "models": "simulation",
    "worlds": "simulation",
}

_cache_lock = threading.Lock()
#: (image, group) -> flattened items. Process-lifetime only -- see module docstring.
_cache: dict[tuple, list] = {}
#: (image, group, name) -> one entry's full detail, for the group whose list cannot carry it.
_detail_cache: dict[tuple, dict] = {}


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


def _catalog_json(stdout: str) -> dict:
    """The JSON document in *stdout*, ignoring whatever else the container printed.

    The catalog command's own output is clean JSON, but it is not the only thing on the
    stream: an image's entrypoint announces itself there too. That makes a whole-stream
    parse the wrong reading of the output -- and a silent one, because an entrypoint line
    opens with ``[``, which is a well-formed array start, so the failure arrives as a JSON
    error about the second character rather than as anything naming the banner.

    So the document is located rather than assumed: the first offset a complete JSON
    *object* decodes from. An object and not any value, because both catalogs return one --
    accepting a bare array would let a bracketed log line win over the real document.

    Raises :class:`ValueError` when the output carries no JSON object at all.
    """
    decoder = json.JSONDecoder()
    for index, char in enumerate(stdout):
        if char != "{":
            continue
        try:
            payload, _ = decoder.raw_decode(stdout, index)
        except ValueError:
            continue
        if isinstance(payload, dict):
            return payload
    raise ValueError("no JSON object in the output")


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
            ExecRequest(**request_kwargs, container=_CATALOG_CONTAINERS[group]))
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
        result = client.exec_in_container(ExecRequest(
            **request_kwargs, command=_CATALOG_COMMANDS[group],
            container=_CATALOG_CONTAINERS[group],
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
        payload = _catalog_json(result.stdout)
    except ValueError:
        # The captured output, not just the decoder's complaint: the command exited 0, so
        # whatever is on the stream is the only evidence of what happened, and a message
        # that withholds it leaves the caller with nothing to act on.
        seen = " ".join((result.stdout or "").split())[:300] or "(no output)"
        return {"error": (
            f"no {group} catalog in the output from {image} -- the command exited 0 but "
            f"printed no JSON object. Run it yourself to see the whole stream: "
            f"exec_in_container(container={_CATALOG_CONTAINERS[group]!r}, "
            f"command={_CATALOG_COMMANDS[group]!r}). Output began: {seen}")}

    items = _flatten(group, payload)
    with _cache_lock:
        _cache[key] = items
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
    from robovast.service.interface import ExecRequest

    if not _ENTRY_NAME_RE.fullmatch(name):
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
            ExecRequest(**request_kwargs, container=_CATALOG_CONTAINERS[group]))
    except Exception as e:  # noqa: BLE001
        return service_access.error_result(e)
    image = resolved.image

    key = (image, group, name)
    with _cache_lock:
        cached = _detail_cache.get(key)
    if cached is not None:
        return {"item": cached, "image": image, "cache": {"hit": True, "seconds": 0.0}}

    started = time.monotonic()
    try:
        result = client.exec_in_container(ExecRequest(
            **request_kwargs, command=f"{_DETAIL_COMMANDS[group]} {name}",
            container=_CATALOG_CONTAINERS[group], query=True))
    except Exception as e:  # noqa: BLE001
        return service_access.error_result(e)
    elapsed = time.monotonic() - started

    # The exit code is not the verdict here: an unknown name exits non-zero AND prints the
    # reason as JSON, so the output is read first and the code only consulted when it carries
    # nothing.
    try:
        payload = _catalog_json(result.stdout)
    except ValueError:
        detail = (result.stderr or result.stdout or "").strip()[:400]
        return {"error": f"describing {name!r} in {image} failed: {detail or '(no output)'}"}
    if "error" in payload:
        return {"error": f"no {group.replace('_', ' ')} entry named {name!r} in {image}"}

    with _cache_lock:
        _detail_cache[key] = payload
    return {"item": payload, "image": image, "cache": {"hit": False, "seconds": elapsed}}


def _details(group: str, address: str, name: str) -> dict:
    if group in _DETAIL_COMMANDS:
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
