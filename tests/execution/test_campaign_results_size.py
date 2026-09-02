# Copyright (C) 2026 Frederik Pasch
# SPDX-License-Identifier: Apache-2.0
"""The campaign's results size is measured once, at the end, and recorded durably.

The point of measuring here rather than on read is that the figure is then a field
lookup for every later viewer. These cover the three things that has to get right: it
is taken AFTER the finalize upload (so a lane that publishes derived data in that
upload is not under-reported), it is never allowed to cost a campaign that otherwise
finished, and "not recorded" stays distinguishable from zero all the way to the row.
"""

import types

import pytest

from robovast.common import campaign_data
from robovast.execution import controller
from robovast.execution.backends import ExecutionBackend, RunOptions
from robovast.execution.control_server import Phase, Status


def _state(phase=Phase.FINISHED):
    ns = types.SimpleNamespace(stop_requested=False, fields={})
    ns.status = Status(phase=phase)
    ns.update = lambda **kw: (ns.fields.update(kw),
                              [setattr(ns.status, k, v) for k, v in kw.items()])
    ns.set_phase = lambda p, **kw: setattr(ns.status, "phase", p)
    ns.snapshot = lambda: ns.status
    return ns


class _Backend:
    """Records the tail's calls, and answers the size hook however the test needs."""

    def __init__(self, size=1234, boom=False):
        self.calls = []
        self._size = size
        self._boom = boom

    def campaign_results_bytes(self, campaign_root):
        self.calls.append("size")
        if self._boom:
            raise RuntimeError("the store said no")
        return self._size


def _patch_tail(monkeypatch, calls):
    monkeypatch.setattr(controller, "_chain_postprocessing",
                        lambda *a, **k: calls.append("postprocess"))
    monkeypatch.setattr(controller, "_finalize",
                        lambda *a, **k: calls.append("finalize"))
    monkeypatch.setattr(controller, "_record_controller_outcome",
                        lambda *a, **k: calls.append("record"))


def test_size_is_measured_after_the_finalize_upload(monkeypatch):
    """Order is the whole correctness argument on a lane whose home is a store.

    Postprocessing's derived data reaches that home in ``finalize``; a total taken
    before it would omit exactly the artifacts the campaign was postprocessed to
    produce, and would look like a plausible number while doing it.
    """
    backend = _Backend()
    _patch_tail(monkeypatch, backend.calls)
    state = _state()
    controller._finish_campaign(backend, "/root", "camp-1", state, RunOptions())
    assert backend.calls == ["postprocess", "record", "finalize", "size", "record"]
    assert state.fields["results_bytes"] == 1234


def test_size_is_recorded_durably_not_only_in_memory(monkeypatch):
    """The record is what carries the figure to a service that no longer has this driver."""
    backend = _Backend()
    calls = []
    _patch_tail(monkeypatch, calls)
    recorded = []
    monkeypatch.setattr(controller, "_record_controller_outcome",
                        lambda root, cid, state, be: recorded.append(
                            state.snapshot().results_bytes))
    controller._finish_campaign(backend, "/root", "camp-1", _state(), RunOptions())
    # Written once before the measurement (no size yet) and once after it (with).
    assert recorded == [None, 1234]


def test_a_failed_campaign_records_no_size(monkeypatch):
    """A failed campaign never finished projecting its results.

    Its tree is missing pieces, so a size taken from it is a partial total that reads
    as a complete one. ``None`` says "not recorded", which is the truth.
    """
    backend = _Backend()
    _patch_tail(monkeypatch, backend.calls)
    state = _state(phase=Phase.FAILED)
    controller._finish_campaign(backend, "/root", "camp-1", state, RunOptions())
    assert "size" not in backend.calls
    assert state.snapshot().results_bytes is None


def test_a_failed_measurement_never_costs_the_campaign(monkeypatch):
    """A size is a convenience; the runs are the deliverable."""
    backend = _Backend(boom=True)
    _patch_tail(monkeypatch, backend.calls)
    state = _state()
    controller._finish_campaign(backend, "/root", "camp-1", state, RunOptions())
    assert state.snapshot().results_bytes is None
    assert state.snapshot().phase == Phase.FINISHED


def test_none_from_the_hook_is_left_unrecorded(monkeypatch):
    """A lane that cannot answer must not be recorded as a campaign of zero bytes."""
    backend = _Backend(size=None)
    _patch_tail(monkeypatch, backend.calls)
    state = _state()
    controller._finish_campaign(backend, "/root", "camp-1", state, RunOptions())
    assert state.snapshot().results_bytes is None


def test_zero_is_recorded_and_is_not_none(monkeypatch):
    """Zero is an answer. Only the absence of one is ``None``."""
    backend = _Backend(size=0)
    _patch_tail(monkeypatch, backend.calls)
    state = _state()
    controller._finish_campaign(backend, "/root", "camp-1", state, RunOptions())
    assert state.snapshot().results_bytes == 0


def test_the_figure_survives_the_outcome_record(tmp_path):
    """``outcome.json`` is how a stateless service answers after the driver is gone."""
    campaign_data.write_execution_outcome(
        tmp_path, Status(phase=Phase.FINISHED, results_bytes=987_654))
    assert campaign_data.read_execution_outcome(tmp_path).results_bytes == 987_654


def test_an_older_record_reads_back_as_not_recorded(tmp_path):
    """A campaign that ended before this existed has no figure, and must not gain a zero."""
    campaign_data.write_execution_outcome(tmp_path, Status(phase=Phase.FINISHED))
    outcome_file = tmp_path / "_execution" / "outcome.json"
    outcome_file.write_text(
        outcome_file.read_text().replace('"results_bytes":null,', ""), encoding="utf-8")
    assert campaign_data.read_execution_outcome(tmp_path).results_bytes is None


def test_the_default_hook_measures_the_campaign_tree(tmp_path):
    """The local lane's durable home IS the campaign root, so the default walks it."""

    class _Local(ExecutionBackend):
        def run_batch(self, *a, **k):  # pragma: no cover - not exercised here
            raise NotImplementedError

    (tmp_path / "config-a" / "run-0").mkdir(parents=True)
    (tmp_path / "config-a" / "run-0" / "test.xml").write_bytes(b"x" * 300)
    (tmp_path / "campaign.db").write_bytes(b"y" * 12)
    assert _Local().campaign_results_bytes(str(tmp_path)) == 312


def test_the_default_hook_skips_what_the_archive_skips(tmp_path):
    """It shares the archiver's exclusions, so the two cannot report different campaigns."""

    class _Local(ExecutionBackend):
        def run_batch(self, *a, **k):  # pragma: no cover - not exercised here
            raise NotImplementedError

    (tmp_path / ".cache").mkdir()
    (tmp_path / ".cache" / "big").write_bytes(b"z" * 5000)
    (tmp_path / "campaign.db").write_bytes(b"y" * 12)
    assert _Local().campaign_results_bytes(str(tmp_path)) == 12


def test_the_cluster_hook_measures_the_store_not_the_driver_disk(monkeypatch, tmp_path):
    """The driver's disk is scratch; a resumed campaign holds only its control plane there.

    Walking it would report a fraction of the campaign as the whole, so the override
    asks the store — whatever this driver happens to hold locally.
    """
    kb = pytest.importorskip(
        "robovast.execution.cluster_execution.kubernetes_backend")

    class _Storage:
        def list_entries(self, bucket, prefix, delimited=False):
            return ([(f"{prefix}a/test.xml", 100), (f"{prefix}b/bag.mcap", 900)], [])

    monkeypatch.setattr(kb.in_pod_storage, "campaign_storage_location",
                        lambda cfg, cid: ("bucket", f"{cid}/"))
    monkeypatch.setattr(kb.in_pod_storage, "storage_client_for", lambda cfg: _Storage())

    backend = kb.KubernetesBackend.__new__(kb.KubernetesBackend)
    backend.cluster_config = object()
    # The local tree is deliberately near-empty: the answer must not come from it.
    (tmp_path / "camp-1").mkdir()
    assert backend.campaign_results_bytes(str(tmp_path / "camp-1")) == 1000
