# Copyright (C) 2026 Frederik Pasch
# SPDX-License-Identifier: Apache-2.0
"""Picking a campaign back up after the service process driving it went away.

The Jobs survive a pod replacement -- they are not children of that process and they write
their own results to the object store -- so what has to be restored is the driver. This is
done by re-launching the campaign under its own id, which is why there is so little here:
everything that makes that safe is a property tested elsewhere (the job partition, the
idempotent campaign row, ``WorkspaceTarget.campaign_id``). What is left is finding the
campaigns owed work, and deciding which of them can be picked up at all.
"""

import logging
import types

import pytest
import yaml

from robovast.execution.cluster_execution import campaign_resume


class _FakeService:
    """A ClusterService stubbed down to what the resume touches."""

    def __init__(self, tmp_path, index, endings=()):
        self.root = tmp_path
        self._index = dict(index)
        self._endings = set(endings)
        self.launched = []
        self.fetched = []

    # -- what campaign_resume reads
    def _campaign_index(self):
        # The pair ClusterService._campaign_index returns, not the bare map: a stub that
        # answers a shape the real collaborator never returns tests nothing. This one
        # returned a plain dict, so every test here passed while the deployed service
        # raised TypeError on its first candidate and resumed nothing.
        return dict(self._index), {cid: "ended" for cid in self._endings}

    def _campaign_dir(self, campaign_id):
        return self.root / campaign_id

    def fetch_campaign(self, campaign_id, force=False, dest=None):
        self.fetched.append((campaign_id, str(dest)))
        return dest

    def _launch_campaign(self, request, target):
        self.launched.append((request, target))
        return types.SimpleNamespace(campaign_id=target.campaign_id)


@pytest.fixture(name="no_store")
def _no_store(monkeypatch):
    """Answer "does this campaign have an ending?" from the fake's own set."""
    def _terminal(service, campaign_id):
        return campaign_id in service._endings
    monkeypatch.setattr(campaign_resume, "_terminal_outcome", _terminal)


def _vast(search=None):
    doc = {"version": 3, "metadata": {"name": "pilot"},
           "configuration": [{"name": "config1"}],
           "execution": {"scenario_file": "scenario.osc", "runs": 2,
                         "containers": {"scenario": {"image": "base:1"}}}}
    if search is not None:
        # Mutually exclusive with `configuration`: a search synthesizes its own.
        doc.pop("configuration")
        doc["search"] = search
    return doc


def _campaign(tmp_path, cid, *, vast=None, launch=None):
    root = tmp_path / cid
    (root / "_config").mkdir(parents=True)
    (root / "_config" / "pilot.vast").write_text(yaml.safe_dump(vast or _vast()))
    (root / "_config" / "scenario.osc").write_text("scenario pilot:\n")
    (root / "_execution").mkdir(parents=True)
    if launch is not None:
        (root / "_execution" / "launch.yaml").write_text(yaml.dump(launch))
    return root


# -- discovery --------------------------------------------------------------------------

def test_a_campaign_with_no_ending_is_owed_work(tmp_path, no_store):
    svc = _FakeService(tmp_path, {"camp-a": "2026-07-17", "camp-b": "2026-07-18"},
                       endings={"camp-b"})
    assert campaign_resume.owed_work(svc) == ["camp-a"]


def test_the_newest_is_picked_up_first(tmp_path, no_store):
    """A service coming back should move the campaign someone is watching first."""
    svc = _FakeService(tmp_path, {"old": "2026-07-01", "new": "2026-07-20",
                                  "mid": "2026-07-10"})
    assert campaign_resume.owed_work(svc) == ["new", "mid", "old"]


def test_one_unreadable_campaign_does_not_hide_the_others(tmp_path, monkeypatch):
    """A service has to start with whatever it can see."""
    def _terminal(service, campaign_id):
        if campaign_id == "bad":
            raise RuntimeError("bucket gone")
        return False
    monkeypatch.setattr(campaign_resume, "_terminal_outcome", _terminal)

    svc = _FakeService(tmp_path, {"bad": "2026-07-20", "good": "2026-07-10"})
    assert campaign_resume.owed_work(svc) == ["good"]


def test_a_fault_in_discovery_does_not_block_startup(tmp_path, monkeypatch):
    svc = _FakeService(tmp_path, {})
    monkeypatch.setattr(campaign_resume, "owed_work",
                        lambda s: (_ for _ in ()).throw(RuntimeError("no store")))
    assert campaign_resume.resume_all(svc) == {}


def test_a_fault_in_discovery_is_reported_as_an_error(tmp_path, monkeypatch, caplog):
    """Starting anyway is right; starting *quietly* is what cost a campaign.

    A bug in discovery resumed nothing and said so once, at warning level, in the voice of
    a routine store outage -- so a service that had picked up no campaign at all looked
    like a service that had come back clean.
    """
    svc = _FakeService(tmp_path, {})
    monkeypatch.setattr(campaign_resume, "owed_work",
                        lambda s: (_ for _ in ()).throw(TypeError("wrong shape")))
    with caplog.at_level(logging.ERROR, logger=campaign_resume.__name__):
        assert campaign_resume.resume_all(svc) == {}
    assert [r for r in caplog.records if r.levelno >= logging.ERROR and r.exc_info]


def test_discovery_reads_the_index_pair_the_service_returns(tmp_path, no_store):
    """Pins ``owed_work`` to ClusterService._campaign_index's real ``(created, finished)``.

    The regression this file missed: iterating that pair as if it were one map raises
    TypeError on the first candidate, and resume_all swallowed it into a start that picked
    up nothing.
    """
    svc = _FakeService(tmp_path, {"camp-a": "2026-07-17"})
    assert isinstance(svc._campaign_index(), tuple)
    assert campaign_resume.owed_work(svc) == ["camp-a"]


# -- the decision -----------------------------------------------------------------------

def test_a_batch_campaign_with_its_records_is_picked_up(tmp_path):
    root = _campaign(tmp_path, "camp-a",
                     launch={"runs": 2, "config_filter": "pilot*", "postprocess": True,
                             "images": {"scenario": "reg.example.com/e@sha256:a"}})
    svc = _FakeService(tmp_path, {})

    target, request, refusal = campaign_resume.plan_for(svc, "camp-a", root)

    assert refusal is None
    assert target.campaign_id == "camp-a"          # adopted, not minted
    assert target.pinned_images == {"scenario": "reg.example.com/e@sha256:a"}
    assert request.runs == 2 and request.config_filter == "pilot*"


def test_a_campaign_with_no_launch_record_is_left_alone(tmp_path):
    """Launched by a service that published no records; nothing says what to run."""
    root = _campaign(tmp_path, "camp-a", launch=None)
    _, _, refusal = campaign_resume.plan_for(_FakeService(tmp_path, {}), "camp-a", root)
    assert "launch.yaml" in refusal and "import" in refusal


def test_a_campaign_with_no_frozen_config_is_left_alone(tmp_path):
    root = tmp_path / "camp-a"
    (root / "_execution").mkdir(parents=True)
    (root / "_execution" / "launch.yaml").write_text(yaml.dump({"runs": 1}))

    _, _, refusal = campaign_resume.plan_for(_FakeService(tmp_path, {}), "camp-a", root)
    assert "_config/" in refusal


def _search(seed=7, strategy="random"):
    return {"strategy": strategy, "per_batch": 2, "budget": [{"batches": 3}], "seed": seed,
            "extract": {"plugin": "failure_rate"},
            "objectives": [{"name": "t", "direction": "minimize"}],
            "search_space": {"a": {"type": "float", "low": 0.0, "high": 1.0}}}


def test_a_seeded_search_is_picked_up(tmp_path):
    """It resumes by re-driving its strategy through the batches its store recorded."""
    root = _campaign(tmp_path, "camp-a", launch={"runs": 2}, vast=_vast(search=_search()))

    target, _, refusal = campaign_resume.plan_for(_FakeService(tmp_path, {}), "camp-a", root)

    assert refusal is None
    assert target.campaign_id == "camp-a"


def test_an_unseeded_search_is_left_alone_and_says_why(tmp_path):
    """Without a seed the replay rebuilds a *different* search, not a continuation.

    Which is worse than a campaign that plainly says it crashed: nothing downstream would
    ever report that the second half stopped being the same experiment as the first.
    """
    root = _campaign(tmp_path, "camp-a", launch={"runs": 2},
                     vast=_vast(search=_search(seed=None)))

    _, _, refusal = campaign_resume.plan_for(_FakeService(tmp_path, {}), "camp-a", root)

    assert "search.seed" in refusal and "different search" in refusal


def test_a_strategy_that_declares_itself_unresumable_is_left_alone(tmp_path, monkeypatch):
    """The opt-out for a strategy that depends on something a seed does not fix."""
    from robovast.search.strategies import random_search

    monkeypatch.setattr(random_search.RandomSearch, "RESUMABLE", False, raising=False)
    root = _campaign(tmp_path, "camp-a", launch={"runs": 2}, vast=_vast(search=_search()))

    _, _, refusal = campaign_resume.plan_for(_FakeService(tmp_path, {}), "camp-a", root)

    assert "not resumable" in refusal and "random" in refusal


# -- end to end through the fake --------------------------------------------------------

def test_the_root_is_restored_before_the_campaign_is_re_launched(tmp_path, no_store):
    """Restored into the driver's own root, not the scratch cache.

    That is what lets the controller and the batch runner read it as the campaign's
    working directory and adopt the jobs that already finished.
    """
    _campaign(tmp_path, "camp-a", launch={"runs": 1})
    svc = _FakeService(tmp_path, {"camp-a": "2026-07-17"})

    outcomes = campaign_resume.resume_all(svc)

    assert outcomes == {"camp-a": None}
    assert svc.fetched == [("camp-a", str(tmp_path / "camp-a"))]
    assert svc.launched[0][1].campaign_id == "camp-a"


def test_a_refused_campaign_does_not_stop_the_others(tmp_path, no_store):
    _campaign(tmp_path, "camp-good", launch={"runs": 1})
    _campaign(tmp_path, "camp-bad", launch=None)
    svc = _FakeService(tmp_path, {"camp-good": "2026-07-10", "camp-bad": "2026-07-20"})

    outcomes = campaign_resume.resume_all(svc)

    assert outcomes["camp-good"] is None
    assert outcomes["camp-bad"] is not None
    assert [t.campaign_id for _, t in svc.launched] == ["camp-good"]


def test_a_config_this_service_cannot_read_is_a_refusal_not_a_crash(tmp_path):
    """And deliberately not a migration.

    A retrigger may bring an old config forward — it is starting over. A resume may not:
    the campaign's finished jobs ran the config as written, so migrating it mid-flight
    would make the second half a different experiment from the first.
    """
    root = _campaign(tmp_path, "camp-a", launch={"runs": 1},
                     vast={"version": 3, "metadata": {"name": "p"}})

    _, _, refusal = campaign_resume.plan_for(_FakeService(tmp_path, {}), "camp-a", root)

    assert refusal is not None and "different experiment" in refusal
