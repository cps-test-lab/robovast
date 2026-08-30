# Copyright (C) 2026 Frederik Pasch
# SPDX-License-Identifier: Apache-2.0
"""The service rolls its own Deployment -- and refuses to, in the cases that matter.

A roll from inside the cluster replaces the pod the campaign controller runs in, and it
reconciles nothing else. Both facts are load-bearing, so both are pinned here: the refusal
while campaigns are live, and that the patch touches the restart annotation and nothing
more -- the regression that guards against is somebody reaching for ``deploy_service``,
which re-renders the whole manifest from the pod's own baked-in environment.
"""

# pylint: disable=redefined-outer-name  # the pytest fixture idiom

from types import SimpleNamespace

import pytest

from robovast.execution.cluster_execution import service_deploy
from robovast.execution.cluster_execution.cluster_service import ClusterService
from robovast.service.interface import CampaignSummary, ListCampaignsResponse


class _Service(ClusterService):
    """A ClusterService with only what ``upgrade_*`` reads, and no cluster behind it.

    A real subclass, and ``__init__`` is deliberately not called: the methods under test go
    through ``super().upgrade_info()``, so the base class's campaign list and its
    ``is_terminal`` filter are the ones exercised here rather than a stub of them. What is
    replaced is only the two things that would need a cluster -- listing campaigns, and the
    image store.
    """

    def __init__(self, campaigns=(), results_dir=""):  # pylint: disable=super-init-not-called
        self.namespace, self.kube_context = "default", None
        self._campaigns = list(campaigns)
        self.published = "repo/robovast-controller@sha256:aaa"
        # Where the roll's "could the replacement pick this up?" check looks for a
        # campaign's records. Empty by default: a campaign with nothing on disk is one
        # nothing could re-launch, which is the case the refusal exists for.
        self._results_dir = results_dir or "/nonexistent"

    def list_campaigns(self, request=None):
        del request
        return ListCampaignsResponse(campaigns=self._campaigns)

    @property
    def _images(self):
        return SimpleNamespace(published_digest=lambda ref: self.published)


@pytest.fixture
def svc(monkeypatch):
    """Build a service whose cluster reads are answered from arguments."""
    def build(campaigns=(), in_pod=True, image="repo/robovast-controller:latest",
              running="sha256:aaa", published="repo/robovast-controller@sha256:aaa",
              denied=False):
        s = _Service(campaigns)
        s.published = published
        s.patched = []
        if in_pod:
            monkeypatch.setenv("KUBERNETES_SERVICE_HOST", "10.0.0.1")
        else:
            monkeypatch.delenv("KUBERNETES_SERVICE_HOST", raising=False)
        monkeypatch.setattr(service_deploy, "deployment_image_ref",
                            lambda *a, **k: ("" if denied else image, denied))
        monkeypatch.setattr(service_deploy, "running_image_digest", lambda *a, **k: running)
        monkeypatch.setattr(service_deploy, "patch_restart_annotation",
                            lambda ns, ctx=None: (s.patched.append((ns, ctx)), "STAMP")[1])
        return s
    return build


def _live(campaign_id="nav-2026-01-01-000000", phase="running"):
    return CampaignSummary(campaign_id=campaign_id, phase=phase)


def test_it_refuses_for_a_campaign_the_replacement_could_not_pick_up(svc):
    """Live is no longer reason enough — a campaign the successor adopts survives a roll.

    What is left to protect is the campaigns it could *not* adopt, and the refusal names
    each one's reason rather than its phase, because the reason is what an operator would
    have to change.
    """
    s = svc(campaigns=[_live()])   # no records on disk, so nothing could re-launch it
    with pytest.raises(RuntimeError) as excinfo:
        s.upgrade_service()
    message = str(excinfo.value)
    assert "nav-2026-01-01-000000" in message, (
        "the refusal has to name what it protected, or it reads as an arbitrary no")
    assert "launch.yaml" in message, "and why, or the operator cannot act on it"
    assert not s.patched, "nothing may be patched when the roll was refused"


def test_a_campaign_that_survives_the_roll_is_not_a_reason_to_refuse(svc, monkeypatch):
    """The point of the whole change: rolling mid-campaign stops being a data-loss event."""
    from robovast.execution.cluster_execution import campaign_resume

    monkeypatch.setattr(campaign_resume, "plan_for",
                        lambda service, cid, root: (object(), object(), None))
    s = svc(campaigns=[_live()])

    assert s.upgrade_service().ok
    assert s.patched == [("default", None)]


def test_force_rolls_over_a_campaign_that_would_be_lost(svc):
    s = svc(campaigns=[_live()])
    assert s.upgrade_service(force=True).ok
    assert s.patched == [("default", None)]


def test_a_terminal_campaign_is_not_a_reason_to_refuse(svc):
    """``active_campaigns`` is the live set: the base class filters on ``is_terminal``."""
    s = svc(campaigns=[_live(phase="finished"), _live("other-2026-01-01-000001", "failed")])
    assert s.upgrade_info().active_campaigns == []
    assert s.upgrade_service().ok


def test_an_unreadable_registry_is_unknown_never_up_to_date(svc):
    """``""`` from the registry means it did not answer, which is not "nothing is newer".

    Reporting that as False is the one wrong answer available here: it tells a reader that
    a fix they have just published is not there.
    """
    assert svc(published="").upgrade_info().upgrade_available is None


def test_the_two_digest_spellings_compare(svc):
    """The registry answers ``repo@sha256:..``; the kubelet's imageID is bare ``sha256:..``."""
    newer = svc(running="sha256:old", published="repo/robovast-controller@sha256:new")
    assert newer.upgrade_info().upgrade_available is True
    current = svc(running="sha256:same", published="repo/robovast-controller@sha256:same")
    assert current.upgrade_info().upgrade_available is False


def test_a_403_on_the_deployment_read_names_the_no_restart_fix(svc):
    """The missed migration: a deployment older than the apps/deployments grant."""
    s = svc(denied=True)
    info = s.upgrade_info()
    assert info.supported is False
    assert "--no-restart" in info.unsupported_reason, (
        "the fix reconciles RBAC without rolling, which is the only one available "
        "mid-campaign -- so it has to be the one named")
    with pytest.raises(ValueError):
        s.upgrade_service()


def test_a_service_outside_the_cluster_has_no_deployment_of_its_own(svc):
    info = svc(in_pod=False).upgrade_info()
    assert info.supported is False
    assert "outside" in info.unsupported_reason


@pytest.mark.parametrize("kwargs, expected", [
    ({"in_pod": False}, "outside"),
    ({"denied": True}, "--no-restart"),
])
def test_a_containerised_service_does_not_inherit_permission_to_roll(
        svc, monkeypatch, kwargs, expected):
    """Each cluster refusal has to write ``supported`` too, not only the reason.

    The base answer stopped being a refusal in every case: a service running from a
    container image reports that it *can* roll itself. So a branch here that set only
    ``unsupported_reason`` would leave a ``supported=True`` carrying one -- and
    ``upgrade_service`` reads ``supported`` alone, so it would patch a Deployment this
    process is not running in, or one it has just been told it may not even read.
    """
    monkeypatch.setenv("ROBOVAST_IN_CONTAINER", "1")
    s = svc(**kwargs)
    info = s.upgrade_info()

    assert info.supported is False
    assert expected in info.unsupported_reason
    with pytest.raises(ValueError):
        s.upgrade_service()
    assert not s.patched, "nothing may be patched when the roll was refused"


def test_the_roll_patches_the_annotation_and_nothing_else(monkeypatch):
    """``patch_restart_annotation`` must not grow into a re-deploy.

    ``deploy_service`` re-renders every manifest from the caller's environment -- and here
    the caller is the pod, whose environment is whatever was baked in at the last setup. So
    a roll that reached for it would look like an upgrade while quietly reverting anything
    changed out of band, and could not rebuild the credential Secrets at all.
    """
    captured = {}

    class _Apps:
        def patch_namespaced_deployment(self, name, namespace, body):
            captured.update(name=name, namespace=namespace, body=body)

    monkeypatch.setattr(service_deploy, "_load_kube_config", lambda *a, **k: None)
    monkeypatch.setitem(__import__("sys").modules, "kubernetes",
                        SimpleNamespace(client=SimpleNamespace(AppsV1Api=_Apps)))
    stamped = service_deploy.patch_restart_annotation("ns")

    assert captured["name"] == service_deploy.SERVICE_NAME
    assert captured["namespace"] == "ns"
    annotations = captured["body"]["spec"]["template"]["metadata"]["annotations"]
    assert list(captured["body"]["spec"]["template"].keys()) == ["metadata"], (
        "the patch reached past the pod template's metadata")
    assert annotations == {service_deploy.RESTART_ANNOTATION: stamped}
