# Copyright (C) 2026 Frederik Pasch
# SPDX-License-Identifier: Apache-2.0
"""The HTTP route table is generated, so the app must stay describable.

``docs/http_api.rst`` renders its route table by introspecting the real application
(``.. http-routes::``). That only stays honest if every route carries a tag to group it and
a description to explain it — asserted here, where a failure names the offending route,
rather than only in the docs build.
"""

import pathlib
import re
import sys

import pytest

from robovast.service.app import ROUTE_TAG_ORDER, api_routes, build_app, route_description

# ``tools/`` is a sibling of ``src/``, not a package on the path.
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[2]))


class _Stub:
    """Stands in for a RobovastInterface: build_app never calls it at construction."""


def _routes():
    return api_routes(build_app(_Stub()))


def test_every_route_is_tagged():
    """A tag is what groups a route in the generated table (and in ``/docs``).

    An untagged route still renders, but last and unordered — a silent demotion of a new
    route to the bottom of the page, which is the drift a generated table should prevent.
    """
    untagged = [f"{sorted(r.methods)} {r.path}" for r in _routes() if not (r.tags or [])]
    assert not untagged, f"routes without a tag: {untagged}"


def _routes_produces() -> dict:
    """Every path :class:`Routes` can produce, with FastAPI-style placeholders."""
    import inspect

    from robovast.service.interface import Routes
    paths = {}
    for name in dir(Routes):
        if name.startswith("_"):
            continue
        member = getattr(Routes, name)
        if isinstance(member, str):
            paths.setdefault(member, name)
        elif callable(member):
            params = list(inspect.signature(member).parameters)
            paths.setdefault(member(*[f"{{{p}}}" for p in params]), name)
    return paths


def test_every_route_path_comes_from_routes():
    """``Routes`` binds the app, not just the client — or it binds nothing.

    Its docstring says it exists so "the service app and the HTTP client cannot drift",
    and it was half true: 27 of 54 registrations restated their path as a literal.
    ``Routes.campaign_status``, ``campaign_stop``, ``campaign_query``,
    ``campaign_describe`` and ``workspace_validate`` were each used by ``http_client`` and
    **zero times** here, so renaming one would have moved the client and left the server
    answering the old path, with nothing to catch it.
    """
    from robovast.service.interface import Routes
    produced = _routes_produces()
    # The two content namespaces are mounted, not enumerated: the path after the owner is
    # an arbitrary file path, so what has to be shared with the client is the prefix, and
    # ``Routes.RESULTS``/``SOURCES`` are what both sides use.
    namespaces = (Routes.RESULTS, Routes.SOURCES)

    orphans = []
    for route in _routes():
        # FastAPI's greedy converter is a routing detail, not part of the path's identity.
        path = route.path.replace(":path", "")
        if path in produced or path.startswith(namespaces):
            continue
        # Endpoint plugins register their own routes by design (see endpoint_plugin) —
        # core cannot name a path it does not know about.
        if "plugin-endpoints" in (route.tags or []):
            continue
        orphans.append(f"{sorted(route.methods)} {route.path}")

    assert not orphans, (
        "routes whose path is not produced by a Routes member — add one and use it, so "
        f"the client and the app keep sharing a single string: {orphans}")


def test_every_route_tag_is_known_to_the_documented_order():
    used = {(r.tags or [None])[0] for r in _routes()}
    assert used <= set(ROUTE_TAG_ORDER), \
        f"tags missing from ROUTE_TAG_ORDER: {sorted(used - set(ROUTE_TAG_ORDER))}"


def test_every_route_has_a_description():
    """Resolvable from the decorator, the handler docstring, or the interface method.

    A blank description is worse than a missing row: it makes a route look documented.
    Most handlers are one-line delegations, so the interface method's docstring is the
    intended source — this asserts the chain resolves for every route, and the docs build
    fails on the same condition.
    """
    undescribed = [f"{sorted(r.methods)} {r.path}" for r in _routes()
                   if not route_description(r)]
    assert not undescribed, (
        f"routes with no resolvable description: {undescribed} — add a handler "
        "docstring, a summary=, or a docstring on the matching RobovastInterface method")


def test_framework_and_spa_routes_are_not_documented_as_api():
    """``/docs``, ``/openapi.json`` and the SPA mount are not this service's API."""
    paths = {r.path for r in _routes()}
    assert not paths & {"/openapi.json", "/docs", "/redoc"}
    assert "/" not in paths


def test_http_api_page_uses_the_generated_directive():
    """The page must invoke the argument-free directive, not hand-list endpoints.

    A hand-maintained endpoint list is how the retired synthetic run-file route came to
    look documented while matching no directory on disk.
    """
    page = (pathlib.Path(__file__).resolve().parents[2]
            / "docs" / "http_api.rst").read_text(encoding="utf-8")
    assert re.search(r"^\.\. http-routes::\s*$", page, re.MULTILINE), \
        "http_api.rst should invoke the argument-free `.. http-routes::` directive"
    assert "http_api" in (pathlib.Path(__file__).resolve().parents[2]
                          / "docs" / "index.rst").read_text(encoding="utf-8"), \
        "http_api must be in the toctree, or the -W docs build fails on an orphan page"


def test_the_dev_proxy_covers_every_served_prefix():
    """`npm run dev` proxies the API by top-level prefix; a missing one breaks a feature.

    The list had drifted: ``/results``, ``/sources``, ``/usage`` and ``/panel_types`` were
    absent, so under the dev server the file browser, the editor's load and save, uploads,
    run-view artifacts, the capacity meter and remote panel assets all failed — against a
    service answering them correctly in production, which is the sort of gap that gets
    debugged as an app bug.
    """
    import re

    config = (pathlib.Path(__file__).resolve().parents[2] / "frontend" / "ui" / "vite.config.ts") \
        .read_text(encoding="utf-8")
    block = config.split("const API_PREFIXES")[1].split("\n]")[0]
    proxied = set(re.findall(r"'/([A-Za-z0-9_.-]+)'", block))

    served = {r.path.split("/")[1] for r in _routes() if r.path != "/"}
    missing = sorted(served - proxied)
    assert not missing, (
        f"top-level API prefixes the dev server would not proxy: {missing}. Add them to "
        "API_PREFIXES in frontend/ui/vite.config.ts.")


def test_the_generated_ui_client_is_up_to_date():
    """``frontend/ui/src/lib/api.generated.ts`` must match the schema the app publishes now.

    The UI's ~40 response types used to be hand-written mirrors of the pydantic models
    with nothing tying them together, so a renamed or newly-optional field stayed
    "correct" in TypeScript until something broke at runtime. They are generated now; this
    is what makes that real, because a generated file nobody regenerates is just a slower
    hand-written one.

    Regenerate with ``cd frontend/ui && npm run generate:api``.
    """
    import json

    ui = pathlib.Path(__file__).resolve().parents[2] / "frontend" / "ui"
    committed = ui / "openapi.json"
    if not committed.is_file():
        pytest.skip("frontend/ui/openapi.json not present (no web UI checkout)")

    from tools.dump_openapi import _mark_response_fields_required  # noqa: PLC0415

    from robovast.service.app import build_app

    current = build_app(_Stub()).openapi()
    _mark_response_fields_required(current)
    assert json.loads(committed.read_text()) == current, (
        "frontend/ui/openapi.json is stale — the service's schema changed. Run "
        "`cd frontend/ui && npm run generate:api` and commit the result.")
