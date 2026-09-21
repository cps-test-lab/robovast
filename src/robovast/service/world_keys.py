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

"""Does the campaign's image know every config key its world sets?

A campaign runs a pinned image, and a world can be newer than it: a key a later plugin reads is,
to the image's older plugin, a key nobody reads. Most plugins do not refuse an unknown key, so the
run starts, the key does nothing, and the result looks configured. Only the image can say which
keys its plugins take, so the answer is asked of it -- its own ``roqsim scenes describe`` (which
components the world declares, and every config path each ends up with) against its own plugin
catalog (``python3 -m roqsim.introspection describe <plugin>``: ``parameters``, and ``schema`` /
``strict_keys`` where the plugin declares them). Both are the image's published CLI answers;
nothing here imports the simulator.

What is compared, and what is not:

* Only components the world **document** declares (``origin: "document"``). A component a model's
  manifest adds ships in the same image as the plugin that reads it, so the two cannot disagree.
* Only a component's **top-level** keys. A plugin's published list flattens its nested keys into
  one list of names, so a nested path is not attributable to a level.
* The keys roqsim lets any component carry (``roqsim.schema.INJECTED_KEYS`` -- a manifest's
  ``prefix``, a transport scope, a fault block) are known to every plugin. They are read from the
  image, which is what applies them.
* A plugin that publishes no keys at all is skipped: silence is not a statement that it takes none.
* A plugin loaded by path from beside the world (``file.py:Class``) is the campaign's own, not the
  image's, and has no catalog entry.

Every finding is **advice**. A plugin's published list is its documented ``Config::`` block unless
it declares a schema, and a documented block can leave a key out that the plugin does read -- so
for such a plugin the finding says what the catalog shows and what follows if it is complete. A
plugin with ``strict_keys`` has a complete list by its own declaration, and refuses the key.
"""

import logging

from robovast.service.image_catalog import (CATALOG_CONTAINERS, ENTRY_NAME_RE,
                                            CatalogUnavailable, fetch_details,
                                            fetch_injected_keys)

logger = logging.getLogger(__name__)

_GROUP = "roqsim_plugins"
#: The container the catalog is read from, from the catalog's own map rather than a second
#: spelling of it: the group decides which container answers, and two answers would disagree
#: the moment one moved.
_CONTAINER = CATALOG_CONTAINERS[_GROUP]
#: Where a reader edits the key this advice is about.
_FIELD = f"execution.containers.{_CONTAINER}.config"


def _advice(message: str, config) -> dict:
    """One advisory finding. Advice, not ``unchecked``: this check is not part of the verdict a
    caller asks for, and a check that could not run must say so rather than pass in silence."""
    return {"stage": "world", "config": config, "field": _FIELD,
            "message": message, "severity": "advice"}


def _top_level_keys(component: dict) -> list:
    """The component's own config keys, from the dotted paths ``scenes describe`` lists."""
    prefix = f"components.{component.get('address')}."
    keys = []
    for path in component.get("paths") or []:
        if isinstance(path, str) and path.startswith(prefix):
            key = path[len(prefix):].split(".", 1)[0]
            if key and key not in keys:
                keys.append(key)
    return keys


def _published(detail: dict) -> set:
    names = {p.get("name") for p in detail.get("parameters") or [] if isinstance(p, dict)}
    names |= {f.get("name") for f in detail.get("schema") or [] if isinstance(f, dict)}
    names.discard(None)
    return names


def _quoted(keys: list) -> str:
    quoted = [f"'{k}'" for k in keys]
    if len(quoted) == 1:
        return quoted[0]
    return ", ".join(quoted[:-1]) + f" or {quoted[-1]}"


def _finding(image: str, ref: str, address: str, keys: list, strict: bool) -> str:
    it = "it" if len(keys) == 1 else "them"
    where = f"{ref} (components.{address})"
    if strict:
        return (f"{image}'s {where} does not know {_quoted(keys)}; that image will refuse "
                f"{it}.")
    return (f"{image}'s {where} does not publish {_quoted(keys)} as a config key; unless it "
            f"reads {it} without documenting {it}, that image will ignore {it}.")


def _declared(payload: dict) -> list:
    """The document's components whose plugin the image's catalog can describe."""
    found = []
    for component in payload.get("components") or []:
        if not isinstance(component, dict) or component.get("origin") != "document":
            continue
        ref = component.get("ref")
        if isinstance(ref, str) and ENTRY_NAME_RE.fullmatch(ref):
            found.append(component)
    return found


def unknown_key_advice(payload: dict, *, image: str, exec_call, resolve_call,
                       request_kwargs: dict, config=None) -> list:
    """Advice for each world component setting a key *image*'s plugin does not publish.

    *payload* is the image's ``roqsim scenes describe`` answer; *image* is the campaign's image as
    it names it, used in the messages. *resolve_call* resolves the image's identity, which keys the
    catalog cache shared with the ``image_catalog`` MCP tools. Returns ``[]`` when every key is
    published; a check that could not be made is itself one advice problem, never silence.
    """
    components = payload.get("components")
    if not components:
        return []
    if not any(isinstance(c, dict) and "origin" in c and "paths" in c for c in components):
        return [_advice(
            f"the config keys of this world's components were not checked against {image}: its "
            "`roqsim scenes describe` does not report each component's origin and config paths.",
            config)]
    declared = _declared(payload)
    if not declared:
        return []

    from robovast.service.interface import ExecRequest
    try:
        identity = resolve_call(ExecRequest(**request_kwargs, container=_CONTAINER)).image
        injected = set(fetch_injected_keys(exec_call, image=identity,
                                           request_kwargs=request_kwargs))
        details = fetch_details(exec_call, group=_GROUP, image=identity,
                                request_kwargs=request_kwargs,
                                names=sorted({c["ref"] for c in declared}))["items"]
    except (CatalogUnavailable, ValueError) as exc:
        return [_advice(
            f"the config keys of this world's components were not checked against {image}'s "
            f"plugin catalog: {exc}", config)]
    except Exception as exc:  # noqa: BLE001 - the key check failing is not a bad campaign
        logger.warning("the world key check did not run: %s", exc)
        return [_advice(
            f"the config keys of this world's components were not checked against {image}'s "
            f"plugin catalog: asking the image failed ({exc}).", config)]

    problems = []
    for component in declared:
        ref = component["ref"]
        detail = details.get(ref) or {}
        if "error" in detail:
            problems.append(_advice(
                f"{image} has no catalog entry for {ref} (components."
                f"{component.get('address')}), so its keys were not checked: {detail['error']}",
                config))
            continue
        published = _published(detail)
        if not published:
            continue
        unknown = [k for k in _top_level_keys(component)
                   if k not in published and k not in injected]
        if unknown:
            problems.append(_advice(
                _finding(image, ref, component.get("address"), unknown,
                         strict=bool(detail.get("schema")) and bool(detail.get("strict_keys"))),
                config))
    return problems
