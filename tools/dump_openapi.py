#!/usr/bin/env python3
# Copyright (C) 2026 Frederik Pasch
#
# SPDX-License-Identifier: Apache-2.0

"""Write the service's OpenAPI schema to a file.

Built from the app itself rather than fetched from a running service, so generating the
web UI's client needs no ``vast serve`` and works in CI. The interface is what the schema
describes; a service happens to serve it.

    python tools/dump_openapi.py ui/openapi.json
"""

import json
import pathlib
import sys


class _Stub:
    """Stands in for a RobovastInterface: build_app never calls it at construction."""


def _mark_response_fields_required(schema: dict) -> None:
    """Make every response model's declared fields ``required``.

    FastAPI omits a pydantic field from ``required`` whenever it has a default, because
    that is right for a *request*: the client may leave it out. For a *response* it is
    wrong — the server serializes the whole model (``exclude_unset`` is off), so the field
    is always there. Left uncorrected, a generated client types every defaulted field as
    ``| undefined`` and the consumer has to guard dozens of values that cannot be missing,
    which buries the handful that genuinely can.

    Optionality that is *real* survives untouched: an ``Optional[...]`` field keeps its
    ``null`` in the type, so a caller still has to handle the case the model actually
    allows. This only removes "might be absent", never "might be null".

    Request models are skipped by name (``…Request``): there a default really does mean
    the field may be omitted.
    """
    for name, model in schema.get("components", {}).get("schemas", {}).items():
        if name.endswith("Request") or model.get("type") != "object":
            continue
        properties = model.get("properties")
        if properties:
            model["required"] = sorted(properties)


def main(argv: list[str]) -> int:
    from robovast.service.app import build_app

    out = pathlib.Path(argv[1] if len(argv) > 1 else "ui/openapi.json")
    schema = build_app(_Stub()).openapi()
    _mark_response_fields_required(schema)
    # Sorted and newline-terminated so a regeneration produces a reviewable diff rather
    # than a reordered blob — the generated client is committed, and a diff nobody can
    # read is the drift moving rather than going away.
    out.write_text(json.dumps(schema, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    print(f"wrote {out} ({len(schema.get('paths', {}))} paths)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
