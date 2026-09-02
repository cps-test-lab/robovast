# Copyright (C) 2026 Frederik Pasch
# SPDX-License-Identifier: Apache-2.0
"""A campaign whose image the registry does not have is refused, not scheduled.

Pinning asks the registry, with the pull credential, what each ref resolves to -- the same
question the kubelet asks a moment later. A ref it could not resolve was treated as a missed
optimisation and the campaign went ahead. When the reason was that the image does not exist,
every job of the batch then died at once on ``ErrImagePull ... NotFound``, already scheduled,
and the campaign reported "none of this batch's jobs could start" -- a message about the
cluster for a fact about the registry, which was in hand one step earlier.

The distinction this turns on is the one the module's own verdicts are built around: a 404 is
an answer, and an unreachable registry is not. Refusing on the second would blame the artifact
for a problem reaching it, which is the mistake the image store's ``present`` exists to
prevent -- so only a definite absence refuses.
"""

import pytest

from robovast.execution.cluster_execution import kubernetes_backend as kb
from robovast.execution.cluster_execution.registry_client import ABSENT, PRESENT, UNKNOWN


def _runner(monkeypatch, states):
    """A runner stubbed to just what the refusal reads."""
    runner = kb.BatchJobRunner.__new__(kb.BatchJobRunner)
    runner.cluster_config = type("C", (), {"get_registry_config": staticmethod(lambda: object())})()
    runner._registry_ca_file = ""
    monkeypatch.setattr(kb.BatchJobRunner, "_registry_dockerconfig", lambda self, r: "")
    monkeypatch.setattr(kb.BatchJobRunner, "_registry_ca_path", lambda self, r: "")
    monkeypatch.setattr("robovast.execution.cluster_execution.registry_client.manifest_state",
                        lambda ref, **kw: states[ref])
    return runner


def test_an_image_the_registry_does_not_have_refuses_the_campaign(monkeypatch):
    runner = _runner(monkeypatch, {"reg.example.com/sut:abc123": ABSENT})

    with pytest.raises(kb.CampaignConfigError) as excinfo:
        runner._refuse_absent_images(["reg.example.com/sut:abc123"])

    message = str(excinfo.value)
    assert "reg.example.com/sut:abc123" in message
    # It has to say what to DO. The failure it replaces sent readers to the cluster.
    assert "rebuild" in message.lower()
    assert "before any pod was created" in message


def test_an_unreachable_registry_does_not_refuse(monkeypatch):
    """The mistake this must not make. ``UNKNOWN`` is not a synonym for absent: refusing on it
    would stop every campaign whenever the registry blinks, and blame the image for it."""
    runner = _runner(monkeypatch, {"reg.example.com/sut:abc123": UNKNOWN})

    runner._refuse_absent_images(["reg.example.com/sut:abc123"])


def test_a_ref_that_is_there_does_not_refuse(monkeypatch):
    """A ref can be unpinnable and present -- a registry that omits the digest header. That
    is the missed optimisation the warning is for, not a reason to refuse."""
    runner = _runner(monkeypatch, {"reg.example.com/sut:abc123": PRESENT})

    runner._refuse_absent_images(["reg.example.com/sut:abc123"])


def test_every_absent_ref_is_named(monkeypatch):
    """One refusal listing them all, so a reader fixes both rather than relaunching to find
    the second."""
    runner = _runner(monkeypatch, {"reg.example.com/a:1": ABSENT,
                                   "reg.example.com/b:2": ABSENT,
                                   "ghcr.example.com/c:3": UNKNOWN})

    with pytest.raises(kb.CampaignConfigError) as excinfo:
        runner._refuse_absent_images(["reg.example.com/a:1", "reg.example.com/b:2",
                                      "ghcr.example.com/c:3"])

    message = str(excinfo.value)
    assert "reg.example.com/a:1" in message and "reg.example.com/b:2" in message
    # The one that could not be asked about must not be reported as missing.
    assert "ghcr.example.com/c:3" not in message


def test_nothing_unresolved_asks_the_registry_nothing(monkeypatch):
    """The ordinary case: every ref pinned. This must add no round trips to it."""
    asked = []
    runner = _runner(monkeypatch, {})
    monkeypatch.setattr("robovast.execution.cluster_execution.registry_client.manifest_state",
                        lambda ref, **kw: asked.append(ref) or PRESENT)

    runner._refuse_absent_images([])

    assert asked == []


def test_a_registry_that_raises_never_refuses(monkeypatch):
    """Not knowing is not a refusal, however the not-knowing arrives."""
    runner = _runner(monkeypatch, {})
    monkeypatch.setattr("robovast.execution.cluster_execution.registry_client.manifest_state",
                        lambda ref, **kw: (_ for _ in ()).throw(RuntimeError("boom")))

    runner._refuse_absent_images(["reg.example.com/sut:abc123"])
