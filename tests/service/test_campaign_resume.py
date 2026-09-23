# Copyright (C) 2026 Frederik Pasch
# SPDX-License-Identifier: Apache-2.0
"""Picking a campaign back up after the service process driving it went away.

The Jobs survive a pod replacement -- they are not children of that process and each one's
uploader delivers its results into the campaign -- so what has to be restored is the driver. This is
done by re-launching the campaign under its own id, which is why there is so little here:
everything that makes that safe is a property tested elsewhere (the job partition, the
idempotent campaign row, ``WorkspaceTarget.campaign_id``). What is left is finding the
campaigns owed work, and deciding which of them can be picked up at all.
"""

import logging
import types
from pathlib import Path

import pytest
import yaml

from robovast.execution.cluster_execution import campaign_resume


class _FakeService:
    """A ClusterService stubbed down to what the resume touches."""

    def __init__(self, tmp_path):
        self.root = Path(tmp_path)
        self.launched = []

    # -- what campaign_resume reads
    def _campaigns_root(self):
        return self.root

    def campaign_dir(self, campaign_id):
        return self.root / campaign_id

    def _launch_campaign(self, request, target):
        self.launched.append((request, target))
        return types.SimpleNamespace(campaign_id=target.campaign_id)


@pytest.fixture(name="endings")
def _endings(monkeypatch):
    """The set of campaigns that recorded an ending, as discovery reads it.

    Patched rather than written as an ``outcome.json`` per campaign so a discovery test
    states only what it is about: which ids came back, and in which order.
    """
    ended: set = set()

    def _terminal(campaign_root):
        return Path(campaign_root).name in ended
    monkeypatch.setattr(campaign_resume, "_terminal_outcome", _terminal)
    return ended


def _vast(search=None):
    doc = {"version": 5, "metadata": {"name": "pilot"},
           "configuration": [{"name": "config1"}],
           "execution": {"scenario_file": "scenario.osc", "runs": 2,
                         "containers": {"scenario": {"image": "base:1"}}}}
    if search is not None:
        # Mutually exclusive with `configuration`: a search synthesizes its own.
        doc.pop("configuration")
        doc["search"] = search
    return doc


#: Ids are campaign-shaped because discovery recognises a campaign by its name.
_A = "camp-a-2026-07-17-120000"
_B = "camp-b-2026-07-18-120000"


def _campaign(tmp_path, cid, *, vast=None, launch=None):
    root = Path(tmp_path) / cid
    (root / "_config").mkdir(parents=True)
    (root / "_config" / "pilot.vast").write_text(yaml.safe_dump(vast or _vast()))
    (root / "_config" / "scenario.osc").write_text("scenario pilot:\n")
    (root / "_execution").mkdir(parents=True)
    if launch is not None:
        (root / "_execution" / "launch.yaml").write_text(yaml.dump(launch))
    return root


# -- discovery --------------------------------------------------------------------------

def test_a_campaign_with_no_ending_is_owed_work(tmp_path, endings):
    _campaign(tmp_path, _A, launch={"runs": 1})
    _campaign(tmp_path, _B, launch={"runs": 1})
    endings.add(_B)

    assert campaign_resume.owed_work(_FakeService(tmp_path)) == [_A]


def test_the_newest_is_picked_up_first(tmp_path, endings):
    """A service coming back should move the campaign someone is watching first."""
    ids = ["p-2026-07-01-120000", "p-2026-07-20-120000", "p-2026-07-10-120000"]
    for cid in ids:
        _campaign(tmp_path, cid, launch={"runs": 1})

    assert campaign_resume.owed_work(_FakeService(tmp_path)) == sorted(ids, reverse=True)


def test_a_directory_no_driver_launched_is_not_owed_work(tmp_path, endings):
    """An import in progress, or a tree put there by hand: nothing says what to run."""
    _campaign(tmp_path, _A, launch=None)

    assert campaign_resume.owed_work(_FakeService(tmp_path)) == []


def test_one_unreadable_campaign_does_not_hide_the_others(tmp_path, monkeypatch):
    """A service has to start with whatever it can see."""
    bad, good = "bad-2026-07-20-120000", "good-2026-07-10-120000"
    _campaign(tmp_path, bad, launch={"runs": 1})
    _campaign(tmp_path, good, launch={"runs": 1})

    def _terminal(campaign_root):
        if Path(campaign_root).name == bad:
            raise RuntimeError("unreadable")
        return False
    monkeypatch.setattr(campaign_resume, "_terminal_outcome", _terminal)

    assert campaign_resume.owed_work(_FakeService(tmp_path)) == [good]


def test_a_fault_in_discovery_does_not_block_startup(tmp_path, monkeypatch):
    svc = _FakeService(tmp_path)
    monkeypatch.setattr(campaign_resume, "owed_work",
                        lambda s: (_ for _ in ()).throw(RuntimeError("unreadable root")))
    assert campaign_resume.resume_all(svc) == {}


def test_a_fault_in_discovery_is_reported_as_an_error(tmp_path, monkeypatch, caplog):
    """Starting anyway is right; starting *quietly* is what cost a campaign.

    A bug in discovery resumed nothing and said so once, at warning level, in the voice of
    a routine store outage -- so a service that had picked up no campaign at all looked
    like a service that had come back clean.
    """
    svc = _FakeService(tmp_path)
    monkeypatch.setattr(campaign_resume, "owed_work",
                        lambda s: (_ for _ in ()).throw(TypeError("wrong shape")))
    with caplog.at_level(logging.ERROR, logger=campaign_resume.__name__):
        assert campaign_resume.resume_all(svc) == {}
    assert [r for r in caplog.records if r.levelno >= logging.ERROR and r.exc_info]


# -- the decision -----------------------------------------------------------------------

def test_a_batch_campaign_with_its_records_is_picked_up(tmp_path):
    root = _campaign(tmp_path, _A,
                     launch={"runs": 2, "config_filter": "pilot*", "postprocess": True,
                             "images": {"scenario": "reg.example.com/e@sha256:a"}})
    svc = _FakeService(tmp_path)

    target, request, refusal = campaign_resume.plan_for(svc, _A, root)

    assert refusal is None
    assert target.campaign_id == _A               # adopted, not minted
    assert target.pinned_images == {"scenario": "reg.example.com/e@sha256:a"}
    assert request.runs == 2 and request.config_filter == "pilot*"


def test_a_campaign_with_no_launch_record_is_left_alone(tmp_path):
    """Launched by a service that published no records; nothing says what to run."""
    root = _campaign(tmp_path, _A, launch=None)
    _, _, refusal = campaign_resume.plan_for(_FakeService(tmp_path), _A, root)
    assert "launch.yaml" in refusal and "import" in refusal


def test_a_campaign_with_no_frozen_config_is_left_alone(tmp_path):
    root = tmp_path / "camp-a"
    (root / "_execution").mkdir(parents=True)
    (root / "_execution" / "launch.yaml").write_text(yaml.dump({"runs": 1}))

    _, _, refusal = campaign_resume.plan_for(_FakeService(tmp_path), _A, root)
    assert "_config/" in refusal


def _search(seed=7, strategy="random"):
    return {"strategy": strategy, "per_batch": 2, "budget": [{"batches": 3}], "seed": seed,
            "extract": {"plugin": "failure_rate"},
            "objectives": [{"name": "t", "direction": "minimize"}],
            "search_space": {"a": {"type": "float", "low": 0.0, "high": 1.0}}}


def test_a_seeded_search_is_picked_up(tmp_path):
    """It resumes by re-driving its strategy through the batches its store recorded."""
    root = _campaign(tmp_path, _A, launch={"runs": 2}, vast=_vast(search=_search()))

    target, _, refusal = campaign_resume.plan_for(_FakeService(tmp_path), _A, root)

    assert refusal is None
    assert target.campaign_id == _A


def test_an_unseeded_search_is_left_alone_and_says_why(tmp_path):
    """Without a seed the replay rebuilds a *different* search, not a continuation.

    Which is worse than a campaign that plainly says it crashed: nothing downstream would
    ever report that the second half stopped being the same experiment as the first.
    """
    root = _campaign(tmp_path, _A, launch={"runs": 2},
                     vast=_vast(search=_search(seed=None)))

    _, _, refusal = campaign_resume.plan_for(_FakeService(tmp_path), _A, root)

    assert "search.seed" in refusal and "different search" in refusal


def test_a_strategy_that_declares_itself_unresumable_is_left_alone(tmp_path, monkeypatch):
    """The opt-out for a strategy that depends on something a seed does not fix."""
    from robovast.search.strategies import random_search

    monkeypatch.setattr(random_search.RandomSearch, "RESUMABLE", False, raising=False)
    root = _campaign(tmp_path, _A, launch={"runs": 2}, vast=_vast(search=_search()))

    _, _, refusal = campaign_resume.plan_for(_FakeService(tmp_path), _A, root)

    assert "not resumable" in refusal and "random" in refusal


# -- the same decision, asked before a deliberate roll ------------------------------------

def test_a_live_campaign_with_its_records_is_not_lost_by_a_roll(tmp_path):
    """The verdict before the roll has to be the one the successor reaches afterwards, so it
    plans through the same ``plan_for`` over the same campaign directory."""
    _campaign(tmp_path, _A, launch={"runs": 2,
                                    "images": {"scenario": "reg.example.com/e@sha256:a"}})

    assert campaign_resume.would_be_lost(_FakeService(tmp_path), _A) is None


def test_a_campaign_with_no_records_is_reported_as_lost(tmp_path):
    """The refusal has to keep firing where it is right: nothing to re-launch from."""
    (tmp_path / _A).mkdir(parents=True)

    assert "launch.yaml" in campaign_resume.would_be_lost(_FakeService(tmp_path), _A)


# -- end to end through the fake --------------------------------------------------------

def test_a_campaign_is_re_launched_under_its_own_id(tmp_path, endings):
    """Adopted rather than minted: the campaign directory is the working directory the
    controller and the batch runner read, so the jobs that already finished are adopted."""
    _campaign(tmp_path, _A, launch={"runs": 1})
    svc = _FakeService(tmp_path)

    outcomes = campaign_resume.resume_all(svc)

    assert outcomes == {_A: None}
    assert svc.launched[0][1].campaign_id == _A


def test_a_refused_campaign_does_not_stop_the_others(tmp_path, endings):
    good, bad = "good-2026-07-10-120000", "bad-2026-07-20-120000"
    _campaign(tmp_path, good, launch={"runs": 1})
    # A launch record is what discovery reads; without a frozen config the plan refuses.
    bad_root = _campaign(tmp_path, bad, launch={"runs": 1})
    (bad_root / "_config" / "pilot.vast").unlink()
    svc = _FakeService(tmp_path)

    outcomes = campaign_resume.resume_all(svc)

    assert outcomes[good] is None
    assert outcomes[bad] is not None
    assert [t.campaign_id for _, t in svc.launched] == [good]


def test_a_config_this_service_cannot_read_is_a_refusal_not_a_crash(tmp_path):
    """And deliberately not a migration.

    A retrigger may bring an old config forward — it is starting over. A resume may not:
    the campaign's finished jobs ran the config as written, so migrating it mid-flight
    would make the second half a different experiment from the first.
    """
    root = _campaign(tmp_path, _A, launch={"runs": 1},
                     vast={"version": 5, "metadata": {"name": "p"}})

    _, _, refusal = campaign_resume.plan_for(_FakeService(tmp_path), _A, root)

    assert refusal is not None and "different experiment" in refusal
