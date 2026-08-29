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

"""Formats of the configuration files a system under test reads.

The ``sut:`` channel addresses a value inside one of the stack's own configuration
files. *Which* files those are is declared by the campaign; *how* a path addresses
something inside one is the business of the file's format, and formats are plugins
(entry-point group :data:`FORMAT_GROUP`) for the same reason simulators and variation
types are: the set of ways a robotics stack can be configured is not closable, and a
stack shipping a bespoke descriptor should not need a change here.

**A format owns its own path syntax.** RoboVAST splits a destination once, on the first
``.``, to find the source name; everything after that is handed to the format verbatim.
So the mapping formats take dotted keys with ``[i]`` indexing and the XML format takes
XPath, and neither grammar is written down in RoboVAST. This is the same hand-off
:func:`~robovast.common.simulators.resolve_sim_path` makes to a simulator backend.

**The format performs the pre-check**, through :meth:`SutConfigFormat.can_address`. The
component that owns the schema is the one that says what is addressable -- which is what
all three channels have in common, and what keeps this channel from being validated
against a hand-maintained list of legal keys. Asking instead for a *set* of addresses
would quietly make XML second class: a document's writable keys are enumerable, an
XPath's are not, so every XPath destination would fall through to "cannot answer" by
construction.
"""

import logging
import os
from importlib.metadata import entry_points

logger = logging.getLogger(__name__)

#: Entry-point group a config format registers under. The built-ins below are registered
#: through it as well, with no privileged internal path -- if they took a shortcut, the
#: extension route would be the one that rots, and it would rot silently because nothing
#: in this repository exercises it.
FORMAT_GROUP = "robovast.sut_formats"


class SutConfigFormat:
    """Read one of the stack's configuration files, address values inside it, write it back.

    A format is stateless: every method takes the document it operates on. ``doc`` is
    whatever :meth:`load` returned and is opaque to everyone else.
    """

    #: File extensions this format claims when the campaign does not say ``format:``.
    #: Lowercase, with the dot (``(".yaml", ".yml")``).
    EXTENSIONS: tuple = ()

    def load(self, path: str):
        """Parse the file at *path* into a document."""
        raise NotImplementedError

    def can_address(self, doc, path: str) -> bool:
        """Whether *path* names something this document can have written at it.

        The pre-check, run at composition so a misspelled destination is refused before a
        campaign spends compute. It asks whether the path is *writable*, not whether a
        value is already there: a factor may legitimately add a key the file leaves at its
        default, so what must exist is the parent. The same rule the ``sim:`` channel
        states, where only the component key is verified.

        Raise :class:`CannotAnswer` if the format cannot decide. The destination is then
        left unchecked, and the caller says so at warning level -- never silently.
        """
        raise NotImplementedError

    def addresses(self, doc):
        """Every address in *doc*, for error messages and editor completion, or ``None``.

        Optional, and ``None`` is a legitimate answer: a format whose address space is not
        enumerable (any XPath expression is a potential address) loses only the listing,
        never the check, which is :meth:`can_address`'s job.
        """
        del doc
        return None

    def set(self, doc, path: str, value):
        """Put *value* at *path*, creating the node or replacing what is there.

        Not assign-a-leaf: *value* may be a scalar, a mapping, a list or -- for a markup
        format -- a fragment, and a factor that swaps a whole behaviour subtree is the same
        call as one that changes a number.
        """
        raise NotImplementedError

    def remove(self, doc, path: str):
        """Delete the node at *path*.

        Not expressible as a :meth:`set`: a configuration block that is present and empty
        is not a block that is absent, and stacks tell the two apart.
        """
        raise NotImplementedError

    def dump(self, doc, path: str):
        """Write *doc* to *path*."""
        raise NotImplementedError


class CannotAnswer(Exception):
    """A format cannot decide whether a path is addressable.

    Distinct from "no": a refusal fails the campaign, this leaves the destination
    unchecked and is reported. Kept separate so a format that simply has not implemented
    the check cannot look like one that examined the path and rejected it.
    """


class UnknownFormat(ValueError):
    """No format is registered under the name, or claims the extension."""


def load_formats() -> dict:
    """Every registered format, ``{name: instance}``.

    Built-ins and third-party formats come from the same entry-point group and are loaded
    by the same code. A format whose import fails is skipped with a warning rather than
    taking down every campaign, including the ones that never name it.
    """
    formats = {}
    for entry in entry_points(group=FORMAT_GROUP):
        try:
            formats[entry.name] = entry.load()()
        except Exception as exc:  # noqa: BLE001 - one broken plugin must not hide the rest
            logger.warning("config format '%s' could not be loaded: %s", entry.name, exc)
    return formats


def resolve_format(file_path: str, declared: str = "", formats: dict = None):
    """The format for *file_path*: *declared* if given, else inferred from the extension.

    An extension no format claims, with no ``format:`` beside it, is **refused naming the
    formats that are registered** rather than guessed at. Defaulting -- to YAML, say --
    would hand a stack's file to the wrong reader, and the bad case is not the one that
    fails: it is the one that parses, into a document whose addresses are all wrong.
    """
    formats = load_formats() if formats is None else formats
    known = ", ".join(sorted(formats)) or "(none)"
    if declared:
        if declared not in formats:
            raise UnknownFormat(
                f"'{declared}' is not a registered config format; registered: {known}")
        return formats[declared]

    ext = os.path.splitext(file_path)[1].lower()
    for name in sorted(formats):
        if ext in getattr(formats[name], "EXTENSIONS", ()):
            return formats[name]
    raise UnknownFormat(
        f"no config format claims '{ext or file_path}', and none was declared: write "
        f"'{{file: {file_path}, format: <name>}}' naming one of: {known}")
