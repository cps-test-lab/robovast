#!/usr/bin/env python3
"""Generate or verify the committed ``.vast`` config field snapshot.

    python3 tools/config_fields.py --write     # regenerate  (make config-fields)
    python3 tools/config_fields.py --check     # verify      (make check-config-fields)

The snapshot is what decides whether a config version bump is *warranted*: adding an
optional field must not bump, while removing or renaming one, adding a required one, or
narrowing a type must. ``tools/check_config_version.py`` classifies the diff of this file
between a PR and its base and asserts the version delta matches.

**Why the field table and not ``model_json_schema()``.** Diffing rendered JSON Schema drags
in ``anyOf``, ``$ref``, ``enum`` narrowing, ``additionalProperties`` and default handling --
a compatibility linter's worth of edge cases -- for the same information. ``model_fields``
is pydantic's own answer and is a fraction of the surface. ``get_config_schema()`` still
serves the web editor and MCP; it is simply not the comparison basis.

**What it cannot see:** plugin-resolved regions -- an open variation config, entry-point
panel types, the ``metadata`` blob. It governs the *declared* schema, which is exactly what
``version:`` versions. A plugin's own parameter model is that plugin's business.
"""

import argparse
import json
import pathlib
import sys
import typing

_REPO = pathlib.Path(__file__).resolve().parents[1]
_SNAPSHOT = _REPO / "compat" / "config_fields.json"


def _type_name(annotation) -> str:
    """A stable, readable name for a field annotation.

    Stable matters more than precise: this string is compared across commits, so it must
    not change when an unrelated import is reordered or a module is renamed. Nested model
    names are kept (they carry the shape) while module paths are dropped.
    """
    if annotation is None:
        return "None"
    origin = typing.get_origin(annotation)
    if origin is None:
        return getattr(annotation, "__name__", str(annotation))
    args = typing.get_args(annotation)
    name = getattr(origin, "__name__", str(origin).replace("typing.", ""))
    if not args:
        return name
    return f"{name}[{', '.join(_type_name(arg) for arg in args)}]"


def _nested_models(annotation, found: set) -> None:
    """Collect every pydantic model reachable from *annotation* into *found*."""
    from pydantic import BaseModel  # pylint: disable=import-outside-toplevel

    if isinstance(annotation, type) and issubclass(annotation, BaseModel):
        if annotation not in found:
            found.add(annotation)
            for field in annotation.model_fields.values():
                _nested_models(field.annotation, found)
        return
    for arg in typing.get_args(annotation):
        _nested_models(arg, found)


def snapshot() -> dict:
    """``{model: [[field, required, type], ...]}`` for every model reachable from the root."""
    sys.path.insert(0, str(_REPO / "src"))
    from robovast.common.config import ConfigV1  # pylint: disable=import-outside-toplevel

    models: set = set()
    _nested_models(ConfigV1, models)

    out = {}
    for model in models:
        out[model.__name__] = sorted(
            [name, bool(field.is_required()), _type_name(field.annotation)]
            for name, field in model.model_fields.items())
    # ``extra`` is part of the contract: a model that forbids extra keys rejects a config an
    # allow-model accepts, so a change to it is a compatibility change like any other.
    for model in models:
        out[model.__name__].append(
            ["<extra>", False, str(model.model_config.get("extra", "ignore"))])
    return dict(sorted(out.items()))


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--write", action="store_true", help="regenerate the snapshot")
    group.add_argument("--check", action="store_true",
                       help="fail if the snapshot disagrees with the models")
    args = parser.parse_args()

    current = json.dumps(snapshot(), indent=2, sort_keys=True) + "\n"

    if args.write:
        _SNAPSHOT.parent.mkdir(parents=True, exist_ok=True)
        _SNAPSHOT.write_text(current, encoding="utf-8")
        print(f"wrote {_SNAPSHOT.relative_to(_REPO)}")
        return 0

    if not _SNAPSHOT.exists():
        print(f"{_SNAPSHOT.relative_to(_REPO)} is missing. Run: make config-fields",
              file=sys.stderr)
        return 1
    if _SNAPSHOT.read_text(encoding="utf-8") != current:
        print(f"{_SNAPSHOT.relative_to(_REPO)} is out of date with the config models.\n"
              f"Run: make config-fields\n"
              f"Then check whether the change needs a config version bump -- see "
              f"src/robovast/common/migrations/README.md.", file=sys.stderr)
        return 1
    print(f"{_SNAPSHOT.relative_to(_REPO)} matches the config models")
    return 0


if __name__ == "__main__":
    sys.exit(main())
