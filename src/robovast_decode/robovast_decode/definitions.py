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

"""Message definitions for a recording, from what travels with it.

A ROS 2 message is CDR bytes; decoding it needs its definition. Three places can supply one,
tried in this order, and the order is the design:

1. **The recording itself.** rosbag2 writes each topic's full concatenated definition into
   the mcap schema record, which is what makes a bag decodable where the types were never
   installed -- including a stack's own custom messages.
2. **The definitions sidecar** (:data:`SIDECAR_NAME`), written beside the bag by the run's
   own container, where the types exist. rosbag2 leaves the schema record *empty* for types
   it cannot find a ``.msg`` file for, which is every action-derived type
   (``<Action>_FeedbackMessage``, ``action_msgs/GoalStatusArray`` on an action topic); the
   sidecar is what covers them.
3. **The distro's own types**, as ``rosbags`` ships them, for anything standard neither of
   the first two named.

A type none of the three cover is not skipped quietly: :meth:`TypeCatalog.missing` names it,
and the topics that carry it are reported as recorded and not tabulated, with that reason.
"""

from __future__ import annotations

import json
import os
from typing import Iterable, Optional

from rosbags.typesys import Stores, get_types_from_idl, get_types_from_msg, get_typestore

#: The sidecar's name, beside the ``*.mcap`` files of one bag directory.
SIDECAR_NAME = "message_definitions.json"

#: The distro whose standard types back the recording's own. Jazzy is what campaigns run.
DEFAULT_STORE = Stores.ROS2_JAZZY


class TypeCatalog:
    """The types one recording can be decoded with, and the ones it cannot."""

    def __init__(self):
        self.store = get_typestore(Stores.EMPTY)
        self._failed: dict = {}
        self._distro_store = None

    def add_definition(self, typename: str, encoding: str, text: str) -> bool:
        """Register *typename* from a definition text; ``False`` when there was none to use.

        An empty definition is what rosbag2 writes for a type it could not find, so it is not
        an error here -- the next source may cover the type. A definition that is present
        and does not parse *is* recorded, because that is a recording no source will fix.
        """
        if not text or not text.strip():
            return False
        try:
            if encoding == "ros2idl":
                types = get_types_from_idl(text)
            else:
                types = get_types_from_msg(text, typename)
        except Exception as exc:  # noqa: BLE001 - reported per type, never raised past here
            self._failed[typename] = f"its definition does not parse: {exc}"
            return False
        new = {name: value for name, value in types.items() if name not in self.store.fielddefs}
        if new:
            self.store.register(new)
        self._failed.pop(typename, None)
        return True

    def add_sidecar(self, bag_dir: str) -> int:
        """Register the definitions in *bag_dir*'s sidecar; how many types it named."""
        path = os.path.join(bag_dir, SIDECAR_NAME)
        if not os.path.isfile(path):
            return 0
        with open(path, encoding="utf-8") as fh:
            definitions = json.load(fh)
        for typename, text in definitions.items():
            if typename not in self.store.fielddefs:
                self.add_definition(typename, "ros2msg", text)
        return len(definitions)

    def _distro(self):
        if self._distro_store is None:
            self._distro_store = get_typestore(DEFAULT_STORE)
        return self._distro_store

    def ensure(self, typename: str) -> bool:
        """Whether *typename* can be decoded, filling it in from the distro if need be.

        Called when a message of the type is about to be decoded, not up front: in a file
        that is still being written, a topic's schema record arrives with its first message,
        and a definition the recording carries must win over the distro's -- a stack built
        against a different revision of a standard message recorded *that* revision.
        """
        if typename in self.store.fielddefs:
            return True
        distro = self._distro()
        if typename not in distro.fielddefs:
            return False
        wanted, stack = {}, [typename]
        while stack:
            name = stack.pop()
            if name in wanted or name in self.store.fielddefs:
                continue
            wanted[name] = distro.fielddefs[name]
            stack.extend(_referenced_types(wanted[name]))
        self.store.register(wanted)
        return True

    def knows(self, typename: str) -> bool:
        return typename in self.store.fielddefs

    def missing(self, typenames: Iterable[str]) -> dict:
        """``{typename: reason}`` for every type in *typenames* that cannot be decoded."""
        out = {}
        for name in typenames:
            if not self.ensure(name):
                out[name] = self._failed.get(
                    name, "no definition in the recording, its sidecar or the distro's types")
        return out

    def deserialize(self, data: bytes, typename: str):
        return self.store.deserialize_cdr(data, typename)

    def fields(self, typename: str) -> list:
        """``[(name, node)]`` of *typename*, in declaration order (``rosbags`` fielddefs)."""
        return self.store.fielddefs[typename][1]


def catalog_for(schemas: Iterable, bag_dir: Optional[str] = None) -> TypeCatalog:
    """A catalog from the recording's schema records and its sidecar; the distro fills in lazily."""
    catalog = TypeCatalog()
    for schema in schemas:
        if schema.encoding in ("ros2msg", "ros2idl"):
            text = schema.data.decode("utf-8", errors="replace") if schema.data else ""
            catalog.add_definition(schema.name, schema.encoding, text)
    if bag_dir:
        catalog.add_sidecar(bag_dir)
    return catalog


def _referenced_types(fielddef) -> list:
    """The message types a ``rosbags`` field definition refers to, one level deep."""
    from rosbags.typesys.msg import Nodetype  # pylint: disable=import-outside-toplevel
    out = []
    for _name, node in fielddef[1]:
        kind, info = node
        while kind in (Nodetype.ARRAY, Nodetype.SEQUENCE):
            kind, info = info[0]
        if kind == Nodetype.NAME:
            out.append(info)
    return out


__all__ = ["DEFAULT_STORE", "SIDECAR_NAME", "TypeCatalog", "catalog_for"]
