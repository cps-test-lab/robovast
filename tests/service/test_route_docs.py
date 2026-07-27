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

from robovast.service.app import (ROUTE_TAG_ORDER, api_routes, build_app,
                                 route_description)


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
