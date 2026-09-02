# Copyright (C) 2026 Frederik Pasch
# SPDX-License-Identifier: Apache-2.0
"""Re-attaching, at startup, to the postprocessing Jobs a previous service process left.

The Job outlives the service process on purpose, and only the *waiting* process writes the
campaign's postprocessing verdict. So a restart mid-postprocess leaves a Job that converts
every bag and finishes, and a campaign whose record still carries the previous attempt's
message -- a fully derived campaign marked as carrying no derived data, whose work is then
redone by hand.

Two properties carry the whole of it, and each has a test here because each is a way for
the fix to be worse than the defect:

* it must cover a campaign whose ``outcome.json`` is already terminal, which is what
  ``campaign_resume.owed_work`` excludes on purpose;
* it must never turn "could not establish" into a verdict.
"""

import types
from unittest.mock import MagicMock

import pytest

from robovast.execution.cluster_execution import postprocess_job, postprocess_reattach
from robovast.execution.cluster_execution.cluster_service import ClusterService
from robovast.service.interface import ActionResult

_CAMPAIGN = "camp-2026-08-27-120000"


@pytest.fixture
def svc():
    return ClusterService(namespace="ns", cluster_config_name="x",
                          cluster_config_kwargs={}, reap_on_start=False)


class _FakeService:
    """A ClusterService stubbed down to what discovery and the re-attach loop touch."""

    def __init__(self, index):
        self.namespace = "ns"
        self.kube_context = "local"
        self._index = dict(index)
        self.reattached = []

    def _campaign_index(self):
        # The pair the real method returns, not a bare map: a double answering a shape the
        # collaborator never returns tests nothing.
        return dict(self._index), {}

    def reattach_postprocessing(self, campaign_id, job_name):
        self.reattached.append((campaign_id, job_name))
        return True


def _jobs(*named):
    """A ``list_namespaced_job`` result holding one active Job per ``(label, name)``."""
    items = [types.SimpleNamespace(
        metadata=types.SimpleNamespace(name=name,
                                       labels={"jobgroup": "postprocessing",
                                               "campaign-id": label}),
        status=types.SimpleNamespace(active=1)) for label, name in named]
    return types.SimpleNamespace(items=items)


@pytest.fixture
def listed(monkeypatch):
    """Answer the labelled Job listing from whatever a test puts in ``box["jobs"]``."""
    box = {"jobs": _jobs(), "selector": None}

    class _Batch:
        def list_namespaced_job(self, namespace=None, label_selector=None):
            box["selector"] = label_selector
            box["namespace"] = namespace
            return box["jobs"]

    monkeypatch.setattr("robovast.execution.cluster_execution.kube_client.load_kube_config",
                        lambda context=None, **kw: None)
    monkeypatch.setattr("kubernetes.client.BatchV1Api", _Batch)
    return box


# -- discovery ---------------------------------------------------------------


def test_live_jobs_are_found_by_label_rather_than_by_guessing_campaign_ids(listed):
    """One labelled listing is the query; the label is then resolved against the store.

    Asking the cluster what is running is what answers for a campaign this fresh process
    has never heard of, and it costs one call whatever the store holds. The label is the
    *sanitized* id, so it is matched against the campaigns the index lists rather than
    used as an id.
    """
    from robovast.execution.cluster_execution.cluster_execution import _label_safe_campaign

    listed["jobs"] = _jobs((_label_safe_campaign(_CAMPAIGN),
                            postprocess_job.campaign_job_name(_CAMPAIGN)))
    service = _FakeService({_CAMPAIGN: "2026-08-27T12:00:00+00:00",
                            "other-2026-08-01-120000": "2026-08-01T12:00:00+00:00"})

    found = postprocess_reattach.live_campaign_postprocessing(service)

    assert found == {_CAMPAIGN: postprocess_job.campaign_job_name(_CAMPAIGN)}
    assert listed["selector"] == "jobgroup=postprocessing"
    assert listed["namespace"] == "ns"


def test_a_campaign_with_a_terminal_outcome_is_still_reattached(listed, monkeypatch):
    """The retrigger case, and the reason this is not part of ``campaign_resume``.

    A postprocess retriggered on a finished campaign runs against a terminal
    ``outcome.json``, which ``owed_work`` excludes so that a campaign that recorded an
    ending is never restarted. That exclusion is right and stays: what is owed here is a
    verdict, not work.
    """
    from robovast.execution.cluster_execution import campaign_resume

    listed["jobs"] = _jobs((_CAMPAIGN, postprocess_job.campaign_job_name(_CAMPAIGN)))
    service = _FakeService({_CAMPAIGN: "2026-08-27T12:00:00+00:00"})
    monkeypatch.setattr(campaign_resume, "_terminal_outcome", lambda svc, cid: True)

    assert campaign_resume.owed_work(service) == []
    assert postprocess_reattach.live_campaign_postprocessing(service) == {
        _CAMPAIGN: postprocess_job.campaign_job_name(_CAMPAIGN)}


def test_a_discriminated_conversion_job_is_not_the_campaigns_postprocess(listed):
    """A per-batch conversion carries the same labels and answers to its batch's driver.

    Its outcome is not the campaign's: a search converts once per repetitions-group, and
    recording one of those as the campaign's postprocessing verdict would mark a campaign
    postprocessed off the back of a fraction of it.
    """
    listed["jobs"] = _jobs((_CAMPAIGN,
                            postprocess_job._short_job_name(  # noqa: SLF001
                                "robovast-postproc-", _CAMPAIGN, discriminator="g2")))
    service = _FakeService({_CAMPAIGN: "2026-08-27T12:00:00+00:00"})

    assert postprocess_reattach.live_campaign_postprocessing(service) == {}


def test_a_finished_job_is_not_reattached_to(listed):
    """Only ``status.active`` counts: a finished Job's outcome was recorded by its waiter,
    or is a verdict nobody in this process observed."""
    jobs = _jobs((_CAMPAIGN, postprocess_job.campaign_job_name(_CAMPAIGN)))
    jobs.items[0].status = types.SimpleNamespace(active=None, succeeded=1)
    listed["jobs"] = jobs

    assert postprocess_reattach.live_campaign_postprocessing(
        _FakeService({_CAMPAIGN: "2026-08-27T12:00:00+00:00"})) == {}


# -- what the waiter records -------------------------------------------------


def _dispatch_capture(svc, monkeypatch):
    seen = {}

    def _dispatch(campaign_id, *, phase, work, elsewhere_written_phase_files=frozenset()):
        seen.update(campaign_id=campaign_id, phase=phase, work=work,
                    elsewhere=frozenset(elsewhere_written_phase_files))
        return ActionResult(ok=True, message="dispatched")

    monkeypatch.setattr(svc, "_dispatch_background", _dispatch)
    return seen


def _recording(svc, monkeypatch):
    """Stub everything the recording touches, and report what it recorded."""
    recorded = {}
    monkeypatch.setattr(svc, "_materialize",
                        lambda cid, paths, subject, **kw: "/nonexistent")
    monkeypatch.setattr(svc, "_cluster_config", lambda: object())
    monkeypatch.setattr(svc, "_publish_execution", lambda cid, root: None)
    monkeypatch.setattr(svc, "_notifier", lambda cid: MagicMock())
    monkeypatch.setattr(
        "robovast.execution.status_recovery.record_step_outcome",
        lambda root, **kw: recorded.update(kw) or MagicMock(postprocessed=True,
                                                            postprocessing_error=None))
    return recorded


def test_a_reattached_job_records_the_outcome_it_waited_for(svc, monkeypatch):
    """The verdict of a Job this process never submitted is still written to the campaign.

    That is the whole of the fix: the Job finishes either way, and the record is the only
    thing a restart takes away.
    """
    seen = _dispatch_capture(svc, monkeypatch)
    recorded = _recording(svc, monkeypatch)
    monkeypatch.setattr(postprocess_job, "reattach_conversion_job",
                        lambda *a, **kw: (True, "postprocessing complete"))

    assert svc.reattach_postprocessing(_CAMPAIGN, "robovast-postproc-x") is True
    seen["work"](MagicMock())

    assert recorded == {"postprocessing": (True, "postprocessing complete")}
    assert seen["phase"] == "postprocessing"
    # The Job writes postprocessing.log into its own staged tree, so the copy under the
    # tracked root is an earlier attempt's and the campaign log must not believe it.
    assert seen["elsewhere"] == frozenset({"postprocessing.log"})


def test_a_job_that_cannot_be_read_leaves_the_record_alone(svc, monkeypatch):
    """"Could not establish" is not a verdict.

    A campaign whose conversion succeeded must not be marked failed because the API server
    was briefly unreadable -- the standing record being stale is recoverable, a fabricated
    failure is not.
    """
    seen = _dispatch_capture(svc, monkeypatch)
    recorded = _recording(svc, monkeypatch)
    monkeypatch.setattr(postprocess_job, "reattach_conversion_job",
                        lambda *a, **kw: (None, "the job could not be read"))

    svc.reattach_postprocessing(_CAMPAIGN, "robovast-postproc-x")
    state = MagicMock()
    seen["work"](state)

    assert recorded == {}
    state.set_phase.assert_called_once()


# -- startup -----------------------------------------------------------------


def test_startup_reattaches_to_what_it_finds(monkeypatch):
    """The wiring: a service that comes up asks, before it answers anything."""
    monkeypatch.setattr(ClusterService, "reap_orphans", lambda self: 0)
    monkeypatch.setattr(ClusterService, "resume_interrupted_campaigns", lambda self: {})
    monkeypatch.setattr(postprocess_reattach, "live_campaign_postprocessing",
                        lambda service: {_CAMPAIGN: "robovast-postproc-x"})
    attached = []
    monkeypatch.setattr(ClusterService, "reattach_postprocessing",
                        lambda self, cid, job: attached.append((cid, job)) or True)

    ClusterService(namespace="ns", cluster_config_name="x", cluster_config_kwargs={},
                   reap_on_start=True)

    assert attached == [(_CAMPAIGN, "robovast-postproc-x")]


def test_a_reattach_that_throws_does_not_stop_the_service_coming_up(monkeypatch):
    """A service must start whatever it could not find out about the cluster.

    A Job nobody re-attached to still finishes and still uploads what it produced; a
    service that will not start takes every campaign with it.
    """
    monkeypatch.setattr(ClusterService, "reap_orphans", lambda self: 0)
    monkeypatch.setattr(ClusterService, "resume_interrupted_campaigns", lambda self: {})

    def _boom(service):
        raise RuntimeError("the cluster said no")

    monkeypatch.setattr(postprocess_reattach, "live_campaign_postprocessing", _boom)

    service = ClusterService(namespace="ns", cluster_config_name="x",
                             cluster_config_kwargs={}, reap_on_start=True)

    assert service.version().backend == "kubernetes"
