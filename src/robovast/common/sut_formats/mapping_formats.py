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

"""YAML and JSON: the formats whose documents are nested mappings and lists.

One path grammar, because one document model: dotted keys, ``[i]`` for a list index, and
``['...']`` for a key a dot would otherwise split. That last form is not decoration -- a
ROS parameter file addresses QoS settings with keys like ``qos_overrides./tf``, so a
grammar without it could not reach a real stack's configuration.
"""

import json
import re

import yaml

from . import CannotAnswer, SutConfigFormat

#: One path segment: a bracketed index, a bracketed quoted key, or a bare dotted key.
_SEGMENT = re.compile(r"""
    \[\s*(?P<index>-?\d+)\s*\]        # [0], [-1]
  | \[\s*'(?P<squoted>[^']*)'\s*\]    # ['qos_overrides./tf']
  | \[\s*"(?P<dquoted>[^"]*)"\s*\]    # ["a.b"]
  | (?P<key>[^.\[\]]+)                # bare key
""", re.VERBOSE)


def parse_path(path: str) -> list:
    """``a.b[0].c`` -> ``['a', 'b', 0, 'c']``; raise ``ValueError`` on nonsense."""
    tokens, pos = [], 0
    text = path.strip()
    if not text:
        raise ValueError("empty path")
    while pos < len(text):
        if text[pos] == ".":
            pos += 1
            continue
        match = _SEGMENT.match(text, pos)
        if not match or match.end() == pos:
            raise ValueError(f"cannot parse path at offset {pos}: {path!r}")
        if match.group("index") is not None:
            tokens.append(int(match.group("index")))
        else:
            tokens.append(next(g for g in (match.group("squoted"),
                                           match.group("dquoted"),
                                           match.group("key")) if g is not None))
        pos = match.end()
    return tokens


def _descend(doc, tokens):
    """The node *tokens* names, or ``None`` if any step is missing."""
    node = doc
    for token in tokens:
        if isinstance(token, int):
            if not isinstance(node, list) or not -len(node) <= token < len(node):
                return None
            node = node[token]
        else:
            if not isinstance(node, dict) or token not in node:
                return None
            node = node[token]
    return node


class _MappingFormat(SutConfigFormat):
    """Shared behaviour; the subclasses differ only in how bytes become a document."""

    def can_address(self, doc, path: str) -> bool:
        """Whether the **parent** exists and can hold the final segment.

        The parent, not the leaf: a factor may legitimately set a key the file leaves at
        its default, and refusing that would reject a correct campaign. Checking the parent
        still catches a typo in every segment but the last, which is what a pre-check is
        worth.
        """
        try:
            tokens = parse_path(path)
        except ValueError as exc:
            raise CannotAnswer(str(exc)) from None
        parent = _descend(doc, tokens[:-1])
        last = tokens[-1]
        if isinstance(last, int):
            return isinstance(parent, list) and -len(parent) <= last < len(parent)
        return isinstance(parent, dict)

    def addresses(self, doc):
        """Every addressable path in *doc*, for error messages and completion."""
        found = set()

        def walk(node, prefix):
            if isinstance(node, dict):
                for key, value in node.items():
                    bracketed = "." in str(key) or "[" in str(key)
                    step = f"['{key}']" if bracketed else str(key)
                    if not prefix:
                        path = step
                    elif bracketed:
                        path = prefix + step
                    else:
                        path = f"{prefix}.{step}"
                    found.add(path)
                    walk(value, path)
            elif isinstance(node, list):
                for i, value in enumerate(node):
                    path = f"{prefix}[{i}]"
                    found.add(path)
                    walk(value, path)

        walk(doc, "")
        return found

    def set(self, doc, path: str, value):
        tokens = parse_path(path)
        node = doc
        for token, nxt in zip(tokens[:-1], tokens[1:]):
            if isinstance(token, int):
                node = node[token]
                continue
            if not isinstance(node.get(token), (dict, list)):
                # Only reached for a path can_address refused; kept so a format used
                # directly still produces a document rather than a TypeError.
                node[token] = [] if isinstance(nxt, int) else {}
            node = node[token]
        node[tokens[-1]] = value

    def remove(self, doc, path: str):
        tokens = parse_path(path)
        parent = _descend(doc, tokens[:-1])
        last = tokens[-1]
        if isinstance(last, int):
            if not isinstance(parent, list) or not -len(parent) <= last < len(parent):
                raise KeyError(f"nothing to remove at '{path}'")
            del parent[last]
            return
        if not isinstance(parent, dict) or last not in parent:
            raise KeyError(f"nothing to remove at '{path}'")
        del parent[last]


class YamlFormat(_MappingFormat):
    """A YAML document -- what a ROS 2 stack's parameters are written in.

    Round-tripped through PyYAML, so key order survives and comments do not. The file is
    consumed by the stack rather than read by a person, and the campaign's own record of
    what changed is the resolved ``sut`` block, not this file.
    """

    EXTENSIONS = (".yaml", ".yml")

    def load(self, path: str):
        with open(path, encoding="utf-8") as handle:
            return yaml.safe_load(handle) or {}

    def dump(self, doc, path: str):
        with open(path, "w", encoding="utf-8") as handle:
            yaml.safe_dump(doc, handle, default_flow_style=False, sort_keys=False)


class JsonFormat(_MappingFormat):
    """A JSON document."""

    EXTENSIONS = (".json",)

    def load(self, path: str):
        with open(path, encoding="utf-8") as handle:
            return json.load(handle)

    def dump(self, doc, path: str):
        with open(path, "w", encoding="utf-8") as handle:
            json.dump(doc, handle, indent=2)
            handle.write("\n")
