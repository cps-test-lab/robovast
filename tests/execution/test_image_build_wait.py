# Copyright (C) 2026 Frederik Pasch
# SPDX-License-Identifier: Apache-2.0
"""``vast image wait`` — the way a caller waits for image builds without holding a request.

The sibling of ``tests/execution/test_cli_wait`` and for the same reason: an agent harness
can background a shell command and be notified when it exits; it cannot do that with an MCP
call, which occupies the conversation for as long as it blocks.

A build was the exception to that for a while, on the argument that minutes are cheap
enough to block on. What killed the exception was the cap: the tool blocked for at most
600s, so a ROS build doing apt + pip + colcon returned unfinished and had to be re-called,
blocking again, precisely where blocking cost most.

The exit code is the contract, as for campaigns — and "the build failed" must not look like
"I stopped waiting", because only one of them means the image is not coming.
"""

import contextlib

import pytest
from click.testing import CliRunner

from robovast.client import cli as client_cli
from robovast.execution.image_build_wait import wait_for_image_builds


class _Status:
    def __init__(self, build_id, phase, done, tag="t", error=None):
        self.build_id, self.phase, self.done = build_id, phase, done
        self.tag, self.error = tag, error


class _Error:
    def __init__(self, phase, message, entry="", fixable_by="agent"):
        self.phase, self.message = phase, message
        self.entry, self.fixable_by = entry, fixable_by


@pytest.fixture
def service(monkeypatch):
    """Point the command at a fake service; *phases* is consumed one per poll, per id."""
    def _install(phases_by_id, error=None):
        seen = []

        class _Client:
            def get_image_build_status(self, build_id):
                phases = phases_by_id[build_id]
                mine = [s for s in seen if s == build_id]
                phase = phases[min(len(mine), len(phases) - 1)]
                seen.append(build_id)
                done = phase in ('succeeded', 'cached', 'failed')
                return _Status(build_id, phase, done,
                               error=error if phase == 'failed' else None)

        @contextlib.contextmanager
        def _client(*_a, **_k):
            yield _Client(), "fake service"

        monkeypatch.setattr(client_cli, "service_client", _client)
        return seen
    return _install


def _run(*build_ids, extra=()):
    return CliRunner().invoke(
        client_cli.cli, ["image", "wait", *build_ids, "--interval", "0.01", *extra])


def test_a_built_image_exits_zero(service):
    service({"b1": ['pulling', 'pip', 'succeeded']})
    result = _run("b1")
    assert result.exit_code == 0
    assert "✓ built" in result.output


def test_a_failed_build_exits_one_and_says_what_to_change(service):
    """``error.entry`` and ``fixable_by`` are the reason to read a status rather than the
    builder log; printing only "failed" sends the reader to the log for what is already
    known here."""
    service({"b1": ['pip', 'failed']},
            error=_Error("pip", "no matching distribution", entry="nav2-smac"))
    result = _run("b1")
    assert result.exit_code == 1
    assert "nav2-smac" in result.output
    assert "fixable_by=agent" in result.output


def test_it_waits_for_every_id(service):
    """A project builds one image per container that adds packages. Returning when the
    first finishes would call the rest built."""
    seen = service({"b1": ['succeeded'], "b2": ['pip', 'pip', 'succeeded']})
    assert _run("b1", "b2").exit_code == 0
    assert seen.count("b2") >= 3


def test_one_failure_among_several_fails_the_wait(service):
    service({"b1": ['succeeded'], "b2": ['failed']},
            error=_Error("apt", "package not found"))
    result = _run("b1", "b2")
    assert result.exit_code == 1
    assert "✓ built" in result.output   # the one that worked is still reported
    assert "b2 failed" in result.output


def test_a_timeout_is_its_own_exit_code(service):
    """Distinct from failure: the build is still going and can be waited on again."""
    service({"b1": ['pip']})
    result = _run("b1", extra=("--timeout", "0.05"))
    assert result.exit_code == 2


def test_waiting_for_nothing_is_refused():
    """An empty id list returning "all done" would report success for a build that was
    never started — the silent-success failure the repo's code rules exist to prevent."""
    class _Client:
        def get_image_build_status(self, build_id):
            raise AssertionError("must not poll")

    with pytest.raises(ValueError):
        wait_for_image_builds([], client=_Client())


def test_a_dropped_poll_does_not_end_the_wait():
    """As in campaign_wait: a service restart mid-build drops one read; the build is
    untouched. Treating that as terminal would report a live build as failed."""
    polls = iter([RuntimeError("connection reset"),
                  _Status("b1", "pip", False),
                  _Status("b1", "succeeded", True)])

    class _Client:
        def get_image_build_status(self, build_id):
            nxt = next(polls)
            if isinstance(nxt, Exception):
                raise nxt
            return nxt

    done = wait_for_image_builds(["b1"], client=_Client(), interval=0)
    assert done["b1"].phase == "succeeded"
