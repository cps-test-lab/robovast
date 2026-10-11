#!/usr/bin/env python3
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

"""Write the message definitions of every recorded type beside its bag.

A bag is decoded wherever its message definitions are known. rosbag2 embeds each topic's
definition in the recording, which is what lets a bag be read without the system under test
installed -- but for a type it finds no ``.msg`` file for it embeds *nothing*, and that is
every action-derived type: ``<Action>_FeedbackMessage``, whose definition exists only inside
an ``.action`` file. A navigation run's goal feedback is exactly such a topic.

So at the end of a run, in the container where the types are installed, this writes
``message_definitions.json`` into each bag directory under the given root: for every type the
bag's ``metadata.yaml`` lists, its full definition in the form rosbag2 embeds (the type's own
text, then each type it uses, separated by a line of ``=`` and a ``MSG: <type>`` header).
A type this cannot resolve is left out and named on stderr; nothing here fails a run.

Runs in the campaign's own image, as a standalone script: standard library only, and the ROS
install found through ``AMENT_PREFIX_PATH``.

Usage::

    dump_message_definitions.py [ROOT]      # default: $SCENARIO_OUTPUT_DIR, else /out
"""

import json
import os
import re
import sys

SIDECAR_NAME = "message_definitions.json"
SEPARATOR = "=" * 80

PRIMITIVES = frozenset({
    "bool", "byte", "char", "float32", "float64", "int8", "uint8", "int16", "uint16", "int32",
    "uint32", "int64", "uint64", "string", "wstring",
})

#: The type-name suffixes an ``.action`` file generates, and the part of the file each is.
_ACTION_SUFFIXES = ("_FeedbackMessage", "_Feedback", "_Goal", "_Result")


def _prefixes():
    return [p for p in os.environ.get("AMENT_PREFIX_PATH", "").split(os.pathsep) if p]


def _read_interface(pkg, kind, name):
    """The text of ``<pkg>/<kind>/<name>.<kind>``, from the first prefix that has it."""
    for prefix in _prefixes():
        path = os.path.join(prefix, "share", pkg, kind, f"{name}.{kind}")
        if os.path.isfile(path):
            with open(path, encoding="utf-8") as fh:
                return fh.read()
    raise KeyError(f"{pkg}/{kind}/{name}")


def own_text(typename):
    """``(text, pkg, kind, action)`` of one type; action-derived types synthesised.

    ``action`` is the action's base name for a type generated from an ``.action`` file: a
    reference to one of its generated siblings (``NavigateToPose_Feedback``) resolves under
    ``action/``, and every other relative reference under ``msg/``, as it does in the file.
    """
    pkg, kind, name = typename.split("/")
    if kind != "action":
        return _read_interface(pkg, kind, name), pkg, kind, None
    for suffix in _ACTION_SUFFIXES:
        if name.endswith(suffix):
            base = name[: -len(suffix)]
            parts = _read_interface(pkg, "action", base).split("---")
            if len(parts) != 3:
                raise KeyError(typename)
            goal, result, feedback = (part.strip("\n") for part in parts)
            text = {
                "_Goal": goal,
                "_Result": result,
                "_Feedback": feedback,
                "_FeedbackMessage": f"unique_identifier_msgs/UUID goal_id\n{base}_Feedback feedback",
            }[suffix]
            return text, pkg, "action", base
    raise KeyError(typename)


def referenced_types(text, pkg, action=None):
    """The fully-qualified message types a definition text uses, in order of appearance."""
    out = []
    for raw in text.splitlines():
        line = raw.split("#", 1)[0].strip()
        if not line:
            continue
        fields = line.split()
        if len(fields) < 2 or "=" in fields[1]:
            continue                                # a constant, or not a field at all
        typ = re.sub(r"\[.*\]$", "", fields[0])
        typ = re.sub(r"<=\d+$", "", typ)
        if typ in PRIMITIVES:
            continue
        if typ == "Header":
            out.append("std_msgs/msg/Header")
        elif "/" in typ:
            parts = typ.split("/")
            out.append(typ if len(parts) == 3 else f"{parts[0]}/msg/{parts[1]}")
        elif action and typ in (f"{action}_Goal", f"{action}_Result", f"{action}_Feedback"):
            out.append(f"{pkg}/action/{typ}")
        else:
            out.append(f"{pkg}/msg/{typ}")
    return out


def full_definition(typename):
    """*typename*'s definition as rosbag2 embeds it: its own text, then its dependencies."""
    text, pkg, _kind, action = own_text(typename)
    seen, order = {typename}, []
    stack = referenced_types(text, pkg, action)
    while stack:
        dep = stack.pop(0)
        if dep in seen:
            continue
        seen.add(dep)
        dep_text, dep_pkg, _dep_kind, dep_action = own_text(dep)
        order.append((dep, dep_text))
        stack.extend(referenced_types(dep_text, dep_pkg, dep_action))
    body = text
    for dep, dep_text in order:
        p, k, n = dep.split("/")
        label = n if k == "msg" else f"{k}/{n}"
        body += f"\n{SEPARATOR}\nMSG: {p}/{label}\n{dep_text}"
    return body


def recorded_types(bag_dir):
    """The types a finished bag's ``metadata.yaml`` lists (read without a YAML library)."""
    with open(os.path.join(bag_dir, "metadata.yaml"), encoding="utf-8") as fh:
        return sorted(set(re.findall(r"^\s+type:\s*(\S+)\s*$", fh.read(), re.MULTILINE)))


def bag_dirs(root):
    for dirpath, _dirnames, filenames in os.walk(root):
        if "metadata.yaml" in filenames and any(f.endswith(".mcap") for f in filenames):
            yield dirpath


def write_sidecar(bag_dir):
    """Write *bag_dir*'s sidecar; the types that could not be resolved."""
    definitions, missing = {}, []
    for typename in recorded_types(bag_dir):
        try:
            definitions[typename] = full_definition(typename)
        except (KeyError, OSError, ValueError):
            missing.append(typename)
    tmp = os.path.join(bag_dir, SIDECAR_NAME + ".incoming")
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(definitions, fh, indent=1, sort_keys=True)
    os.replace(tmp, os.path.join(bag_dir, SIDECAR_NAME))
    return missing


def main(argv=None):
    argv = sys.argv[1:] if argv is None else argv
    root = argv[0] if argv else os.environ.get("SCENARIO_OUTPUT_DIR") or "/out"
    for bag_dir in bag_dirs(root):
        try:
            missing = write_sidecar(bag_dir)
        except OSError as exc:
            print(f"[definitions] {bag_dir}: not written: {exc}", file=sys.stderr)
            continue
        if missing:
            print(f"[definitions] {bag_dir}: no definition found for {', '.join(missing)}",
                  file=sys.stderr)
        else:
            print(f"[definitions] wrote {os.path.join(bag_dir, SIDECAR_NAME)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
