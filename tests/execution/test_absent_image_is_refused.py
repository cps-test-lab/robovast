# Copyright (C) 2026 Frederik Pasch
# SPDX-License-Identifier: Apache-2.0
"""A campaign whose image digest cannot be read is refused, not scheduled.

Every image a campaign runs is fixed to a digest before any pod starts, with the pull credential
the kubelet uses a moment later. A ref that could not be fixed would leave the campaign running
bytes nothing recorded, and every replay of it unable to say what it repeats -- so the launch
is refused, whatever the reason: a registry that does not have the image, one that does not
answer, and one that will not name the bytes.

The reasons still differ in what the reader has to do, so the refusal carries the registry's
own answer for each ref, and names every one at once.
"""

import pytest

from robovast.execution.cluster_execution import kubernetes_backend as kb
from robovast.execution.cluster_execution.registry_client import ABSENT, PRESENT, UNKNOWN


def _runner(monkeypatch, states):
    """A runner stubbed to just what the refusal reads."""
    runner = kb.BatchJobRunner.__new__(kb.BatchJobRunner)
    runner.campaign = "camp-2026-09-26-120000"
    runner.cluster_config = type("C", (), {"get_registry_config": staticmethod(lambda: object())})()
    runner._registry_ca_file = ""
    monkeypatch.setattr(kb.BatchJobRunner, "_registry_dockerconfig", lambda self, r: "")
    monkeypatch.setattr(kb.BatchJobRunner, "_registry_ca_path", lambda self, r: "")
    monkeypatch.setattr("robovast.execution.cluster_execution.registry_client.manifest_state",
                        lambda ref, **kw: states[ref])
    return runner


def _refusal(runner, refs):
    with pytest.raises(kb.CampaignConfigError) as excinfo:
        runner._refuse_unpinned_images({ref: [f"container {ref.split('/')[-1]!r}"]
                                        for ref in refs})
    return str(excinfo.value)


def test_an_image_the_registry_does_not_have_refuses_the_campaign(monkeypatch):
    runner = _runner(monkeypatch, {"reg.example.com/sut:abc123": ABSENT})

    message = _refusal(runner, ["reg.example.com/sut:abc123"])

    assert "reg.example.com/sut:abc123" in message
    assert "does not have it" in message
    # It has to say what to DO. The failure it replaces sent readers to the cluster.
    assert "rebuild" in message.lower()
    assert "before any pod was created" in message


def test_an_unreachable_registry_refuses_too(monkeypatch):
    """No digest, no launch: a tag run in its place names nothing a replay could repeat. The
    message says the registry did not answer, so the image is not blamed for it."""
    runner = _runner(monkeypatch, {"reg.example.com/sut:abc123": UNKNOWN})

    message = _refusal(runner, ["reg.example.com/sut:abc123"])

    assert "did not answer" in message
    assert "does not have it" not in message


def test_a_registry_that_will_not_name_the_bytes_refuses(monkeypatch):
    """Present, but no ``Docker-Content-Digest``: there is still no digest to record."""
    runner = _runner(monkeypatch, {"reg.example.com/sut:abc123": PRESENT})

    assert "Docker-Content-Digest" in _refusal(runner, ["reg.example.com/sut:abc123"])


def test_every_unfixed_ref_is_named_with_what_runs_it(monkeypatch):
    """One refusal listing them all, so a reader fixes every one rather than relaunching to
    find the next."""
    runner = _runner(monkeypatch, {"reg.example.com/a:1": ABSENT,
                                   "reg.example.com/b:2": ABSENT,
                                   "ghcr.example.com/c:3": UNKNOWN})

    message = _refusal(runner, ["reg.example.com/a:1", "reg.example.com/b:2",
                                "ghcr.example.com/c:3"])

    for ref in ("reg.example.com/a:1", "reg.example.com/b:2", "ghcr.example.com/c:3"):
        assert ref in message
    assert "container 'c:3'" in message


def test_nothing_unresolved_asks_the_registry_nothing(monkeypatch):
    """The ordinary case: every ref fixed. This must add no round trips to it."""
    asked = []
    runner = _runner(monkeypatch, {})
    monkeypatch.setattr("robovast.execution.cluster_execution.registry_client.manifest_state",
                        lambda ref, **kw: asked.append(ref) or PRESENT)

    runner._refuse_unpinned_images({})

    assert asked == []


def test_a_registry_that_raises_still_refuses_and_says_so(monkeypatch):
    """Not knowing is not a digest, however the not-knowing arrives."""
    runner = _runner(monkeypatch, {})
    monkeypatch.setattr("robovast.execution.cluster_execution.registry_client.manifest_state",
                        lambda ref, **kw: (_ for _ in ()).throw(RuntimeError("boom")))

    assert "boom" in _refusal(runner, ["reg.example.com/sut:abc123"])


def test_no_registry_configured_refuses_and_says_so(monkeypatch):
    runner = _runner(monkeypatch, {})

    def _none():
        raise RuntimeError("no registry configured")

    runner.cluster_config = type("C", (), {"get_registry_config": staticmethod(_none)})()

    assert "no registry to ask" in _refusal(runner, ["reg.example.com/sut:abc123"])
