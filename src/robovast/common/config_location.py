# Copyright (C) 2026 Frederik Pasch
# SPDX-License-Identifier: Apache-2.0
"""Where in a ``.vast`` a thing is written, for a message that names it.

A ``.vast`` is read into plain dicts, which keep no line numbers; the one time a line is
worth having is when a message has to point a reader back into the file. So this reads the
file again, as a node tree, only on that path.
"""

import yaml


def _scalar(node):
    return node.value if isinstance(node, yaml.ScalarNode) else None


def _mapping_value(node, key):
    """The value node under *key* in a mapping node, or ``None``."""
    if not isinstance(node, yaml.MappingNode):
        return None
    for key_node, value_node in node.value:
        if _scalar(key_node) == key:
            return value_node
    return None


def _variation_key_line(variations, ref):
    """The line of the entry naming *ref* in a ``variations:`` sequence node, or ``None``."""
    if not isinstance(variations, yaml.SequenceNode):
        return None
    for item in variations.value:
        if not isinstance(item, yaml.MappingNode):
            continue
        for key_node, _value in item.value:
            if _scalar(key_node) == ref:
                return key_node.start_mark.line + 1
    return None


def _walk(node):
    yield node
    if isinstance(node, yaml.MappingNode):
        for _key, value in node.value:
            yield from _walk(value)
    elif isinstance(node, yaml.SequenceNode):
        for item in node.value:
            yield from _walk(item)


def variation_line(vast_path, config_name, ref):
    """The line on which the variation *ref* of the configuration block *config_name* is
    written in the ``.vast`` at *vast_path*, or ``None`` when the file does not say.

    The block's own ``variations:`` list first. A variation a block takes from a preset
    (``use:``) or a search takes from its template is written once, elsewhere in the same
    file, so failing that any ``variations:`` list naming *ref* is the answer -- the line
    the reader has to edit is the same whichever block ran it. ``None`` for a file that
    cannot be composed or a variation the file never names (one handed in by code), and
    never an error: this is only ever asked while a failure is being reported.
    """
    try:
        with open(vast_path, encoding="utf-8") as f:
            root = yaml.compose(f, Loader=yaml.SafeLoader)  # nosec B506 - not yaml.Loader
    except (OSError, yaml.YAMLError):
        return None
    if root is None:
        return None
    for block in getattr(_mapping_value(root, "configuration"), "value", []) or []:
        if _scalar(_mapping_value(block, "name")) != config_name:
            continue
        line = _variation_key_line(_mapping_value(block, "variations"), ref)
        if line is not None:
            return line
    for node in _walk(root):
        line = _variation_key_line(_mapping_value(node, "variations"), ref)
        if line is not None:
            return line
    return None
