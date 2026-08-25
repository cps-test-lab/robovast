# Copyright (C) 2026 Frederik Pasch
# SPDX-License-Identifier: Apache-2.0
"""``vast image build`` — what a human is told about a project that builds SEVERAL images.

A project builds one image per container that adds packages, and the returned handle names
only one of them. Both output lines used to name that one, so a project building a scenario
image and a `sut` image printed a single cache-hit line for the scenario and never mentioned
the `sut` at all: a reader could not tell whether the second image was covered, still
building, or missing entirely. That was observed in practice and read as "the sut image is
not being built", which is a different and much more alarming thing than what had happened.

The MCP surface had the same defect in `cached`/`next_step`; this is the human half of it.
"""

import contextlib

import pytest
from click.testing import CliRunner

from robovast.client import cli as client_cli


class _Ref:
    def __init__(self, tag, cached, builds, cached_builds=None):
        self.tag = tag
        self.build_id = builds[tag]
        self.cached = cached
        self.builds = builds
        if cached_builds is not None:
            self.cached_builds = cached_builds


@pytest.fixture
def build(monkeypatch):
    """Point ``vast image build`` at a fake service returning *ref*; return the waited ids."""
    def _install(ref):
        waited = []

        class _Client:
            def build_image(self, request):
                return ref

        @contextlib.contextmanager
        def _client(*_a, **_k):
            yield _Client(), "fake service"

        monkeypatch.setattr(client_cli, "service_client", _client)
        monkeypatch.setattr(client_cli, "_wait_for_builds",
                            lambda client, ids, **kw: waited.extend(ids))
        return waited
    return _install


def _run(*args):
    return CliRunner().invoke(client_cli.cli,
                              ["image", "build", "--workspace-id", "ws1", *args])


def test_every_cached_image_is_named(build):
    build(_Ref("scenario", True, {"scenario": "b-scenario", "sut": "b-sut"},
               {"scenario": True, "sut": True}))
    out = _run().output
    assert "build:scenario" in out
    assert "build:sut" in out, "the sut image was covered and must be said so"


def test_a_building_sibling_is_not_hidden_by_a_cache_hit(build):
    """The observed case. The scenario image was a hit and the expensive sut image was not;
    only the hit was printed, so the sut looked absent rather than in progress."""
    waited = build(_Ref("scenario", False, {"scenario": "b-scenario", "sut": "b-sut"},
                        {"scenario": True, "sut": False}))
    out = _run().output
    assert "✓ image 'build:scenario' already up to date" in out
    assert "building 'build:sut' (build_id=b-sut)" in out
    # And the wait covers the one that is actually building, not the one already there.
    assert waited == ["b-sut"]


def test_nothing_is_waited_on_when_everything_is_cached(build):
    waited = build(_Ref("scenario", True, {"scenario": "b-scenario"},
                        {"scenario": True}))
    assert waited == []


def test_a_service_without_per_container_verdicts_still_reports_once(build):
    """An older service sends no ``cached_builds``. One line about the handle's tag is all
    that can honestly be said, and every id is waited on rather than a subset guessed."""
    waited = build(_Ref("scenario", False, {"scenario": "b-scenario", "sut": "b-sut"}))
    out = _run().output
    assert "building 'build:scenario'" in out
    assert sorted(waited) == ["b-scenario", "b-sut"]
