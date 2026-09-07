# Copyright (C) 2026 Frederik Pasch
# SPDX-License-Identifier: Apache-2.0
"""Reading a ``.vast`` where a key written twice is refused rather than resolved.

YAML says nothing useful about a repeated key and PyYAML keeps the last one, silently. For a
document that describes what a trial is configured with, that is the worst available answer:
the losing block is still in the file, still reads as the configuration, and is applied to
nothing. A campaign runs, reports every cell normally, and its results were produced by
settings nobody wrote.

It is not a hypothetical shape. A block indented at a list's level lands on the entry *above*
it, so a section written in the wrong order silently overwrites the entry before it -- and the
file still looks like the one the author meant.

**Strict for authoring, lenient for an archive.** A campaign already stored with a duplicate
ran with the last value; refusing to read it back would lose the results without catching
anything, so that path takes the same value PyYAML would have and says which one it took.
"""

import logging

import yaml

logger = logging.getLogger(__name__)


class DuplicateKeyError(ValueError):
    """A mapping in a ``.vast`` states one key twice."""


def _duplicate_message(key, first, second, path=None) -> str:
    where = f"{path}: " if path else ""
    return (
        f"{where}the key {key!r} is set twice in one mapping, at line {first.line + 1} and "
        f"line {second.line + 1}. YAML keeps the last of the two, so the first is in the file "
        f"and applied to nothing -- which is indistinguishable from a campaign that is "
        f"configured. Keep one of them, or move the second to the block it belongs to.")


#: The merge key. Excluded from the check because the pairs it brings in are appended to the
#: mapping by ``flatten_mapping``, so an explicit key overriding a merged one would otherwise
#: read as a repeat -- which is the whole point of a merge, not a mistake.
_MERGE = "tag:yaml.org,2002:merge"


def _duplicates(loader, node, deep=False):
    """The repeated keys of *node*, as ``(key, first_mark, second_mark)``, in file order.

    Called BEFORE ``flatten_mapping``, so what it sees is what the author wrote in this
    mapping and nothing a merge brought in.
    """
    seen, found = {}, []
    for key_node, _value in node.value:
        if getattr(key_node, "tag", None) == _MERGE:
            continue
        try:
            key = loader.construct_object(key_node, deep=deep)
        except yaml.YAMLError:
            continue                       # an unconstructable key is the base loader's to report
        try:
            hash(key)
        except TypeError:
            continue                       # likewise for an unhashable one
        if key in seen:
            found.append((key, seen[key], key_node.start_mark))
        else:
            seen[key] = key_node.start_mark
    return found


class StrictLoader(yaml.SafeLoader):  # pylint: disable=too-many-ancestors
    """:class:`yaml.SafeLoader` that refuses a mapping stating one key twice.

    Merge keys are flattened first, so ``<<:`` and aliases keep working exactly as they do
    without this -- what is refused is the same key written out twice in one mapping.
    """

    def construct_mapping(self, node, deep=False):
        # Before flattening: see the author's own keys, not the merged-in ones.
        duplicates = _duplicates(self, node, deep=deep)
        self.flatten_mapping(node)
        for key, first, second in duplicates:
            raise DuplicateKeyError(_duplicate_message(key, first, second))
        return super().construct_mapping(node, deep=deep)


class LenientLoader(yaml.SafeLoader):  # pylint: disable=too-many-ancestors
    """Takes the last of a repeated key, as PyYAML does, and says that it did.

    For reading a campaign that already ran: the value it ran with is the one this keeps.
    """

    def construct_mapping(self, node, deep=False):
        # Before flattening: see the author's own keys, not the merged-in ones.
        duplicates = _duplicates(self, node, deep=deep)
        self.flatten_mapping(node)
        for key, first, second in duplicates:
            logger.warning(
                "%s The campaign ran with the value from line %d; that is what is read here.",
                _duplicate_message(key, first, second), second.line + 1)
        return super().construct_mapping(node, deep=deep)


def load_all(stream, path=None, strict: bool = True):
    """Every YAML document in *stream*, refusing (or reporting) a repeated key.

    *path* only names the file in the message; the loader has no other use for it.
    """
    loader = StrictLoader if strict else LenientLoader
    try:
        return list(yaml.load_all(stream, Loader=loader))  # nosec B506 - not yaml.Loader
    except DuplicateKeyError as e:
        raise DuplicateKeyError(f"{path}: {e}" if path else str(e)) from None


def load(stream, path=None, strict: bool = True):
    """The first document of *stream*, or ``None`` when it holds none."""
    documents = load_all(stream, path=path, strict=strict)
    return documents[0] if documents else None
