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

"""Introspect a plugin's parameter schema from its Pydantic config model.

A plugin declares the parameters it accepts by pointing a class attribute at a
Pydantic model — ``CONFIG_CLASS`` for variation types, ``PARAMS_MODEL`` for
search strategies. That model is the authoritative description of the field
names, their types, and which are required, but it is not reachable from the
top-level ``.vast`` JSON Schema: variations dispatch dynamically by entry-point
key off an empty ``VariationConfig`` base, so an author authoring a ``.vast``
file has no way to see, say, that ``ParameterVariationList.values`` is a
``list[float | int | bool | dict | list]`` before ``validate`` rejects it.

This module turns that model into a compact, human- and machine-readable field
list. It is the shared core behind both the ``get_plugin_details`` MCP tool and
the ``vast configuration plugin-info`` CLI command, so the two can never drift.
"""

import logging
from importlib.metadata import entry_points

from pydantic import BaseModel
from pydantic_core import PydanticUndefined

logger = logging.getLogger(__name__)

#: Class attributes a plugin may use to point at its Pydantic parameter model,
#: in resolution order (variation types first, then search strategies).
_SCHEMA_ATTRS = ("CONFIG_CLASS", "PARAMS_MODEL")


def describe_pydantic_model(model) -> list[dict] | None:
    """Render *model*'s fields as ``[{name, type, required, default?, description?}]``.

    *model* is a Pydantic ``BaseModel`` subclass. ``type`` is the annotation's
    string form (e.g. ``"str | list[str]"``). ``default`` is included only when
    the field has one; ``description`` only when set. Returns ``None`` if *model*
    is not a usable model or introspection fails.
    """
    if not (isinstance(model, type) and issubclass(model, BaseModel)):
        return None
    try:
        fields = []
        for name, field in model.model_fields.items():
            entry: dict = {
                "name": name,
                "type": _annotation_str(field.annotation),
                "required": field.is_required(),
            }
            if field.default is not PydanticUndefined:
                entry["default"] = field.default
            if field.description:
                entry["description"] = field.description
            fields.append(entry)
        return fields
    except Exception as exc:  # noqa: BLE001 - never let introspection break a caller
        logger.debug("Could not introspect %r: %s", model, exc)
        return None


def plugin_parameter_schema(group: str, name: str) -> list[dict] | None:
    """Return the parameter field schema for entry point *name* in *group*.

    Loads the plugin, looks for a Pydantic model on ``CONFIG_CLASS`` or
    ``PARAMS_MODEL`` (in that order), and describes it via
    :func:`describe_pydantic_model`. Returns ``None`` when the plugin cannot be
    loaded or declares no parameter model.
    """
    matches = [ep for ep in entry_points(group=group) if ep.name == name]
    if not matches:
        return None
    try:
        obj = matches[0].load()
    except Exception as exc:  # noqa: BLE001 - a broken plugin must not raise here
        logger.debug("Could not load %r for schema extraction: %s", name, exc)
        return None
    return schema_from_object(obj)


def schema_from_object(obj) -> list[dict] | None:
    """Describe the parameter model carried by an already-loaded plugin *obj*.

    Checks each attribute in :data:`_SCHEMA_ATTRS` and returns the first that
    resolves to a usable Pydantic model, or ``None``.
    """
    for attr in _SCHEMA_ATTRS:
        described = describe_pydantic_model(getattr(obj, attr, None))
        if described is not None:
            return described
    return None


def _annotation_str(annotation) -> str:
    """Return a readable string for a type annotation (e.g. ``str | list[str]``)."""
    text = str(annotation)
    # Strip the ``typing.``/``<class '...'>`` noise Python adds for bare types.
    if text.startswith("<class '") and text.endswith("'>"):
        return getattr(annotation, "__name__", text)
    return text.replace("typing.", "")
