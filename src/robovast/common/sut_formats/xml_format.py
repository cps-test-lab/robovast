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

"""XML: the format a behaviour tree, a DDS profile and many a robot description are in.

The path grammar is **XPath**, which is the point of letting a format own its own syntax
rather than defining one centrally. A behaviour tree names three different nodes
``RecoveryNode``, so ``//RecoveryNode/@number_of_retries`` is ambiguous and
``//RecoveryNode[@name='NavigateRecovery']/@number_of_retries`` is the destination someone
actually means. No flat dotted grammar can say that, and a channel that imposed one would
have made XML second class from the start.

Two additions on top of what ElementTree evaluates:

* a trailing ``/@name`` selects an **attribute** -- ElementTree has no attribute node, and
  neither has any XPath engine a way to *assign* to one, so the segment is split off here;
* a path with no ``/@`` selects an **element**, and setting it replaces that element with
  a parsed fragment, which is how a factor swaps a whole subtree.

Standard-library ElementTree, deliberately: the one library that would give fuller XPath
is not a declared dependency of this package, and reaching for a transitive one is how a
public package acquires an install that works here and fails on a clean clone. Comments
are preserved (:class:`~xml.etree.ElementTree.TreeBuilder` takes them since 3.8); exact
whitespace and self-closing style are not, and the file is consumed by the stack rather
than read by a person.
"""

import re
import xml.etree.ElementTree as ET

from . import CannotAnswer, SutConfigFormat

#: Start of the first *element*, i.e. the end of the prolog. ``<?xml ...?>``, a doctype and
#: any comment before the root all begin ``<?``, ``<!`` -- only an element starts with a
#: name character.
_FIRST_ELEMENT = re.compile(r"<[A-Za-z_]")


def _split_attribute(path: str):
    """``//Node[@name='x']/@retries`` -> ``("//Node[@name='x']", "retries")``."""
    text = path.strip()
    marker = text.rfind("/@")
    if marker == -1:
        return text, None
    return text[:marker], text[marker + 2:]


def _to_et(path: str) -> str:
    """An absolute-looking XPath as ElementTree wants it, relative to the root element.

    ``//Node`` is written the way everyone writes it; ElementTree spells the same thing
    ``.//Node`` and rejects the leading slash outright.
    """
    if path.startswith("//"):
        return "." + path
    if path.startswith("/"):
        return "./" + path.lstrip("/")
    return path


class _Doc:
    """A parsed document: the tree, its prolog, and the parent map ElementTree drops.

    Replacing or deleting an element needs its parent, and ``Element`` has no link back to
    one -- so the map is built at load and refreshed whenever the tree changes shape.

    *prolog* is the text before the root element. ElementTree parses it and then discards
    it, so a stack file's header -- frequently the line saying where the file came from or
    that it is generated -- would be dropped from the copy the campaign actually runs.
    Comments *inside* the root survive on their own (``insert_comments``); this is only
    about the ones no tree can hold.
    """

    def __init__(self, tree, prolog=""):
        self.tree = tree
        self.prolog = prolog
        self.parents = {}
        self.reindex()

    def reindex(self):
        self.parents = {child: parent
                        for parent in self.tree.iter()
                        for child in parent}

    @property
    def root(self):
        return self.tree.getroot()

    def find(self, et_path):
        if et_path in (".", ""):
            return self.root
        # ElementTree's find is relative to the root element, so "./root" asks for a CHILD
        # named root. When the name is the root's own, that is what the author meant --
        # without this, "/root" silently resolves to nothing rather than to the document.
        if et_path.startswith("./") and et_path[2:] == self.root.tag:
            return self.root
        return self.root.find(et_path)


class XmlFormat(SutConfigFormat):
    """An XML document addressed by XPath."""

    EXTENSIONS = (".xml",)

    def load(self, path: str):
        with open(path, encoding="utf-8") as handle:
            text = handle.read()
        match = _FIRST_ELEMENT.search(text)
        prolog = text[:match.start()] if match else ""
        parser = ET.XMLParser(target=ET.TreeBuilder(insert_comments=True))
        return _Doc(ET.ElementTree(ET.fromstring(text, parser=parser)), prolog)

    def can_address(self, doc, path: str) -> bool:
        """Whether the element the path names exists (and, for an attribute, its element).

        The element is the parent an attribute or a replacement is written into, so this is
        the same "the parent must exist" rule the mapping formats apply -- an attribute the
        file leaves unset is still addressable, a misspelled element is not.
        """
        element_path, _attribute = _split_attribute(path)
        try:
            return doc.find(_to_et(element_path)) is not None
        except SyntaxError as exc:
            raise CannotAnswer(f"not a valid XPath: {path!r} ({exc})") from None

    def set(self, doc, path: str, value):
        """Set an attribute, or replace the element with a parsed fragment."""
        element_path, attribute = _split_attribute(path)
        element = doc.find(_to_et(element_path))
        if element is None:
            raise KeyError(f"no element at '{element_path}'")
        if attribute is not None:
            element.set(attribute, str(value))
            return

        parent = doc.parents.get(element)
        if parent is None:
            raise ValueError(
                f"'{path}' names the root element; replacing the whole document is not a "
                "variation of it")
        replacement = value if ET.iselement(value) else ET.fromstring(str(value))
        parent[list(parent).index(element)] = replacement
        doc.reindex()

    def remove(self, doc, path: str):
        """Delete an attribute, or an element from its parent."""
        element_path, attribute = _split_attribute(path)
        element = doc.find(_to_et(element_path))
        if element is None:
            raise KeyError(f"nothing to remove at '{path}'")
        if attribute is not None:
            if attribute not in element.attrib:
                raise KeyError(f"nothing to remove at '{path}'")
            del element.attrib[attribute]
            return
        parent = doc.parents.get(element)
        if parent is None:
            raise ValueError(f"'{path}' names the root element, which cannot be removed")
        parent.remove(element)
        doc.reindex()

    def dump(self, doc, path: str):
        body = ET.tostring(doc.root, encoding="unicode")
        with open(path, "w", encoding="utf-8") as handle:
            handle.write(doc.prolog + body)
            if not body.endswith("\n"):
                handle.write("\n")
