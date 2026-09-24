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

"""What an experiment image's own catalogs say, asked in the image and cached per image.

Two readers share this: the ``image_catalog`` MCP tools (``list_roqsim_plugins`` and friends),
which answer a caller's question about an image, and the world check in ``validate_project``
(:mod:`robovast.service.world_keys`), which compares a world's config keys with what the image's
plugins publish. One cache, keyed by the image's resolved identity
(:meth:`~robovast.service.interface.RobovastInterface.resolve_image`), because a catalog only
changes when the image does -- and so a plugin described for one of them is not asked again for
the other while the process lives.

Everything here is asked through a command in the image, never by importing the simulator: the
catalogs are the image's published answers (``python3 -m roqsim.introspection``, ``python3 -m
scenario_execution.introspection``), and this process may have neither installed.

The callers pass their own ``exec_call`` (an ``ExecRequest`` -> ``ExecResult`` callable) and the
request's addressing, so this works from the MCP server over a client and from the service over its
own transport alike. Exceptions from the exec propagate: what a caller makes of "nothing can run a
command here" is its own business.
"""

import json
import re
import threading
import time

#: The whole-catalog command per group.
#:
#: Where each upstream corpus already sits in the simulator image. Both are whole source trees
#: the image copies in -- roqsim's to /opt/roqsim, scenario-execution's into the ROS workspace --
#: so their ``docs/`` are there to be read. Nothing is added to an image to serve them, and no
#: second pin of the same commit has to be kept in step with the one that built it.
DOCS_ROOTS = {"roqsim": "/opt/roqsim/docs", "osc": "/ws/src/scenario-execution/docs"}

#: Every page from both, each labelled with the corpus it belongs to. The label is carried rather
#: than derived from the path, because the two collide on names (``architecture``, ``index``) and
#: a reader that flattened them would serve one repository's page under the other's question.
#:
#: A missing root yields no pages rather than an error: which corpora an image carries is a fact
#: about that image, and an answer from the ones it has beats no answer at all.
DOCS_COMMAND = (
    "python3 -c 'import json, pathlib; "
    # json.dumps and not repr: the whole command is single-quoted for the shell, and repr
    # would close that quoting on the first dict key.
    f"roots = {json.dumps(DOCS_ROOTS)}; "
    "print(json.dumps({\"items\": ["
    "{\"name\": p.stem, \"source\": s, \"text\": p.read_text(errors=\"replace\")} "
    "for s, d in roots.items() if pathlib.Path(d).is_dir() "
    "for p in sorted(pathlib.Path(d).glob(\"*.rst\"))]}))'")

#: ``python3`` and not ``python``: the only interpreter a DECLARED base image is guaranteed to
#: have. Debian/Ubuntu ship no ``python`` at all (PEP 394 -- the name meant Python 2, and it exists
#: only via the optional ``python-is-python3``), while an image RoboVAST *built* does have one,
#: because the venv at /usr/local provides it. `execution.containers.<name>.image` lets a campaign
#: pin any base, so a command's contract must not depend on a package the substrate cannot
#: guarantee.
CATALOG_COMMANDS = {
    "scenario_actions": "python3 -m scenario_execution.introspection list-actions",
    "roqsim_plugins": "python3 -m roqsim.introspection list",
    # What the image can SPAWN, as against what it can configure: a campaign names a robot
    # and a world by ref before it names anything else.
    "models": "python3 -m roqsim.catalog models",
    "worlds": "python3 -m roqsim.catalog worlds",
    "docs": DOCS_COMMAND,
}

#: How a group answers a request for ONE entry's detail. ``scenario_execution``'s list already
#: carries every field a detail call returns, so that group is answered from the cached list and
#: has no entry here. ``roqsim.introspection list`` is a summary -- name, kind, doc, flags,
#: package -- so a plugin's parameters exist only behind ``describe``.
DETAIL_COMMANDS = {
    "roqsim_plugins": "python3 -m roqsim.introspection describe",
    # A model's components, with their defaults. ``worlds`` has no detail command: its list
    # already carries every field one would return.
    "models": "python3 -m roqsim.catalog model",
}

#: Which container answers each group. ``roqsim`` lives in the *simulator's* image, not the
#: scenario's.
CATALOG_CONTAINERS = {
    "scenario_actions": "scenario",
    "roqsim_plugins": "simulation",
    "models": "simulation",
    "worlds": "simulation",
    "docs": "simulation",
}

#: The keys roqsim lets any component carry without its own list naming them, because something
#: other than the world's author put them there (a manifest's ``prefix``, a transport scope, a fault
#: block). ``roqsim.schema.INJECTED_KEYS`` is roqsim's own definition of that set; it is read from
#: the image rather than restated here, so it is the set THAT image applies. Printed as one JSON
#: object, the shape every other reader here parses.
INJECTED_KEYS_COMMAND = (
    "python3 -c 'import json; from roqsim.schema import INJECTED_KEYS; "
    "print(json.dumps({\"injected_keys\": sorted(INJECTED_KEYS)}))'")

#: An entry-point name, which is all a detail command is ever given. The name reaches a shell in
#: the container, so it is checked against this rather than quoted: a name outside it is a
#: mistake in the call, and refusing is a better answer than escaping it and asking anyway.
ENTRY_NAME_RE = re.compile(r"[A-Za-z_][A-Za-z0-9_.-]*")

#: A model ref, ``<provider>:<name>``. Its own pattern rather than a widened
#: :data:`ENTRY_NAME_RE`: one that admitted both would admit ``task.py:Task`` as well, which
#: names a plugin the campaign carries and which ``world_keys`` asks no image about.
MODEL_REF_RE = re.compile(r"[A-Za-z_][A-Za-z0-9_.-]*:[A-Za-z_][A-Za-z0-9_.-]*")

#: The name pattern each catalog's detail command accepts.
DETAIL_NAME_RE = {"models": MODEL_REF_RE}

#: Separates one entry's answer from the next when several are asked for in one exec. Long and
#: specific because it is matched against the command's own output.
_ENTRY_MARKER = "@@robovast-catalog-entry "

CACHE_LOCK = threading.Lock()
#: (image, group) -> flattened items. Process-lifetime only.
LIST_CACHE: dict = {}
#: (image, group, name) -> one entry's full detail, for the group whose list cannot carry it.
DETAIL_CACHE: dict = {}
#: image -> the injected-key list that image's roqsim applies.
INJECTED_CACHE: dict = {}


class CatalogUnavailable(RuntimeError):
    """The command ran, and what it printed is not the catalog. The message says what it was."""


def catalog_json(stdout: str) -> dict:
    """The JSON document in *stdout*, ignoring whatever else the container printed.

    The catalog command's own output is clean JSON, but it is not the only thing on the
    stream: an image's entrypoint announces itself there too. That makes a whole-stream
    parse the wrong reading of the output -- and a silent one, because an entrypoint line
    opens with ``[``, which is a well-formed array start, so the failure arrives as a JSON
    error about the second character rather than as anything naming the banner.

    So the document is located rather than assumed: the first offset a complete JSON
    *object* decodes from. An object and not any value, because every catalog returns one --
    accepting a bare array would let a bracketed log line win over the real document.

    Raises :class:`ValueError` when the output carries no JSON object at all.
    """
    decoder = json.JSONDecoder()
    for index, char in enumerate(stdout or ""):
        if char != "{":
            continue
        try:
            payload, _ = decoder.raw_decode(stdout, index)
        except ValueError:
            continue
        if isinstance(payload, dict):
            return payload
    raise ValueError("no JSON object in the output")


def _request(request_kwargs: dict, command: str, container: str):
    from robovast.service.interface import ExecRequest

    # A read-only introspection of the image: it belongs in the service's query pool, never in
    # the caller's container, which a one-shot exec would discard.
    return ExecRequest(**request_kwargs, command=command, container=container, query=True)


def _detail_command(group: str, names: list) -> str:
    """One entry's command, or -- for several -- each behind a marker line, in one script.

    Several in one exec because the world check asks for every plugin a world uses at once, and a
    round trip per plugin is what the cache is there to avoid. Newline-separated rather than
    ``&&``-joined: ``describe`` exits non-zero for an unknown name, and that must not stop the
    names after it from being answered.
    """
    base = DETAIL_COMMANDS[group]
    if len(names) == 1:
        return f"{base} {names[0]}"
    return "\n".join(f"echo '{_ENTRY_MARKER}{name}'\n{base} {name}" for name in names)


def _split_answers(stdout: str, names: list) -> dict:
    """``{name: that entry's slice of stdout}``; one name owns the whole stream."""
    if len(names) == 1:
        return {names[0]: stdout or ""}
    answers, current = {}, None
    for line in (stdout or "").splitlines(keepends=True):
        if line.startswith(_ENTRY_MARKER):
            current = line[len(_ENTRY_MARKER):].strip()
            answers[current] = ""
        elif current is not None:
            answers[current] += line
    return answers


def fetch_details(exec_call, *, group: str, image: str, request_kwargs: dict,
                  names) -> dict:
    """``{"items": {name: detail}, "cache": {hit, seconds}}`` for each of *names* in *image*.

    A detail is the entry's own JSON -- including ``{"error": ...}`` for a name the image does not
    have, which ``describe`` reports on stdout while exiting non-zero, so the output is read and
    the exit code is not the verdict. Only what is not cached is asked for, in ONE exec.

    Raises :class:`ValueError` for a name that is not an entry-point name (before anything runs),
    :class:`CatalogUnavailable` when a name's answer carries no JSON, and whatever *exec_call*
    raises.
    """
    names = list(dict.fromkeys(names))
    pattern = DETAIL_NAME_RE.get(group, ENTRY_NAME_RE)
    for name in names:
        if not pattern.fullmatch(name):
            raise ValueError(f"{name!r} is not an entry-point name")
    items, missing = {}, []
    with CACHE_LOCK:
        for name in names:
            cached = DETAIL_CACHE.get((image, group, name))
            if cached is None:
                missing.append(name)
            else:
                items[name] = cached
    if not missing:
        return {"items": items, "cache": {"hit": True, "seconds": 0.0}}

    started = time.monotonic()
    result = exec_call(_request(request_kwargs, _detail_command(group, missing),
                                CATALOG_CONTAINERS[group]))
    elapsed = time.monotonic() - started
    answers = _split_answers(result.stdout, missing)
    for name in missing:
        try:
            payload = catalog_json(answers.get(name, ""))
        except ValueError:
            detail = (result.stderr or answers.get(name) or "").strip()[:400]
            raise CatalogUnavailable(
                f"describing {name!r} in {image} failed: {detail or '(no output)'}") from None
        items[name] = payload
        if "error" not in payload:
            with CACHE_LOCK:
                DETAIL_CACHE[(image, group, name)] = payload
    return {"items": items, "cache": {"hit": False, "seconds": elapsed}}


def fetch_injected_keys(exec_call, *, image: str, request_kwargs: dict) -> list:
    """The keys *image*'s roqsim accepts on any component (``roqsim.schema.INJECTED_KEYS``).

    Raises :class:`CatalogUnavailable` when the image does not answer with the list -- a roqsim
    that predates the set has no such name, and that is not the same as an empty set.
    """
    with CACHE_LOCK:
        cached = INJECTED_CACHE.get(image)
    if cached is not None:
        return cached
    result = exec_call(_request(request_kwargs, INJECTED_KEYS_COMMAND,
                                CATALOG_CONTAINERS["roqsim_plugins"]))
    try:
        payload = catalog_json(result.stdout)
        keys = payload["injected_keys"]
    except (ValueError, KeyError):
        detail = (result.stderr or result.stdout or "").strip()[:300]
        raise CatalogUnavailable(
            f"{image} does not say which keys its roqsim injects into every component "
            f"(roqsim.schema.INJECTED_KEYS): {detail or '(no output)'}") from None
    if not isinstance(keys, list) or not all(isinstance(k, str) for k in keys):
        raise CatalogUnavailable(
            f"{image} answered the injected-key question with {keys!r}, not a list of names")
    with CACHE_LOCK:
        INJECTED_CACHE[image] = keys
    return keys
