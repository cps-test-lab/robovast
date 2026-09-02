# Copyright (C) 2026 Frederik Pasch
# SPDX-License-Identifier: Apache-2.0
"""A resumed campaign's directory is incomplete until something asks for the whole of it.

``campaign_resume`` restores only a campaign's control plane -- launch record, frozen config,
``campaign.db``, per-run verdicts -- because it runs before ``vast serve`` binds its port and
a campaign's artifacts are gigabytes. Nothing between there and postprocessing reads the rest:
job adoption reads ``test.xml``, run counts come from the store's own table, and a resumed
search replays its earlier evaluations out of ``campaign.db``.

Postprocessing is the exception -- ``data.db`` is *derived* from the tree -- so the controller
completes the root immediately before it. These tests pin the ordering, because getting it
wrong is silent: a truncated tree yields a truncated ``data.db`` and no error anywhere.
"""

import types

from robovast.execution import controller
from robovast.execution.backends import ExecutionBackend, RunOptions
from robovast.execution.control_server import Phase
from robovast.execution.cluster_execution import postprocess_job


class _State:
    """The campaign state, recording the phase changes and stage lines it is given."""

    def __init__(self, calls):
        self.calls = calls
        self.stage = None

    def set_phase(self, phase, **_kw):
        self.calls.append(("phase", phase))

    def update(self, **fields):
        if "stage" in fields:
            self.stage = fields["stage"]
            self.calls.append(("stage", fields["stage"]))


class _Backend:
    """A cluster-lane backend stubbed to what ``_chain_postprocessing`` touches."""

    def __init__(self):
        self.cluster_config = object()
        self.calls = []

    def ensure_campaign_root_complete(self, campaign_root):
        self.calls.append(("complete", campaign_root))

    def publish_execution_records(self, campaign_root):
        # The other direction: what the driver alone wrote has to reach the campaign's
        # durable home before a reader that is not this process stages it from there.
        self.calls.append(("publish", campaign_root))


def test_the_root_is_completed_before_postprocessing_reads_it(monkeypatch):
    backend = _Backend()

    def _postprocess(_cfg, campaign_id, campaign_root, _ns, **_kw):
        backend.calls.append(("postprocess", campaign_root))
        return True, "done"

    # Patched where ``_chain_postprocessing``'s deferred import resolves it, not on the
    # controller: a patch the code under test never looks at leaves the real call to fail
    # into the broad ``except`` below it, and the test then passes without ordering anything.
    monkeypatch.setattr(postprocess_job, "postprocess_campaign", _postprocess)

    controller._chain_postprocessing(backend, "/results/camp-a", "camp-a",
                                     state=None, options=RunOptions(postprocess=True))

    # The ORDER, not the exact sequence: other work legitimately sits between these two
    # (the driver's records are published to the durable home here too), and a test that
    # pins the whole list fails on any such addition while guarding nothing more.
    kinds = [kind for kind, _root in backend.calls]
    assert kinds.index("complete") < kinds.index("postprocess"), (
        "the root has to be whole BEFORE data is derived from it; afterwards is a "
        "truncated campaign that reports success")
    assert all(root == "/results/camp-a" for _kind, root in backend.calls)


def test_a_local_campaign_is_never_asked_to_complete_its_root(monkeypatch):
    """The Docker lane writes every artifact straight into the root -- there is nothing to do.

    Guarded because the hook reaches the object store: calling it on a lane that has none
    would turn "no cluster configured" into a campaign-ending error at the finish tail.
    """
    backend = _Backend()
    backend.cluster_config = None

    controller._chain_postprocessing(backend, "/results/camp-a", "camp-a",
                                     state=None, options=RunOptions(postprocess=True))

    assert backend.calls == []


def test_postprocessing_not_requested_completes_nothing(monkeypatch):
    """No postprocessing, no reader -- and ``finalize_campaign`` only ever re-uploads.

    What is missing locally is simply not re-sent, and the object store keeps the copy the
    jobs put there, so a partial root cannot lose a campaign's results.
    """
    backend = _Backend()

    controller._chain_postprocessing(backend, "/results/camp-a", "camp-a",
                                     state=None, options=RunOptions(postprocess=False))

    assert backend.calls == []


def test_every_backend_answers_the_hook():
    """A default no-op on the ABC, so a lane that needs nothing inherits the right behaviour
    instead of each one remembering to define it."""
    assert hasattr(ExecutionBackend, "ensure_campaign_root_complete")
    assert ExecutionBackend.ensure_campaign_root_complete(
        types.SimpleNamespace(), "/results/camp-a") is None


def test_the_phase_moves_before_the_root_is_completed(monkeypatch):
    """Completing the root IS postprocessing's first step, so the phase has to say so.

    Left in `running` for the transfer, a resumed campaign was measured against the per-run
    budget by ``client.status.stall_report`` and reported as
    ``stalled: true, "no progress ... the run is not merely slow"`` -- sending a reader to
    diagnose a run that had already finished. That verdict is suppressed off the running
    phase; ordering the transition ahead of the fetch is what makes the suppression apply.
    """
    backend = _Backend()
    state = _State(backend.calls)

    def _postprocess(_cfg, campaign_id, campaign_root, _ns, **_kw):
        backend.calls.append(("postprocess", campaign_root))
        return True, "done"

    monkeypatch.setattr(postprocess_job, "postprocess_campaign", _postprocess)

    controller._chain_postprocessing(backend, "/results/camp-a", "camp-a",
                                     state=state, options=RunOptions(postprocess=True))

    kinds = [c[0] for c in backend.calls]
    assert kinds.index("phase") < kinds.index("complete"), (
        "a multi-GB restore attributed to the running phase reads as a stalled run")
    assert backend.calls[kinds.index("phase")][1] == Phase.POSTPROCESSING


class _FakeStorage:
    """Enough of a StorageClient for the hook: a listing and a per-file download."""

    def __init__(self, objects):
        self.objects = objects

    def download_prefix(self, _bucket, _prefix, _local, force=False, on_file=None,
                        on_progress=None, include=None):
        # The hook must ASK for a denominator; a bare running count cannot say how far
        # along a multi-GB transfer is, which is the whole point of narrating it.
        assert on_progress is not None
        total = len(self.objects)
        total_bytes = sum(self.objects.values())
        done = done_bytes = 0
        for size in self.objects.values():
            done += 1
            done_bytes += size
            if on_file is not None:
                on_file()
            on_progress(done, total, done_bytes, total_bytes)
        return done


def test_the_restore_narrates_itself_on_the_campaign_stage(monkeypatch, tmp_path):
    """Someone watching the campaign must see the transfer, not a frozen marker.

    This step has no run counter, so its own narration is the only thing separating a long
    step from a stuck one. Without it the stage sat on whatever the resume had last written
    while gigabytes moved -- which is precisely how it read the first time it ran for real.
    """
    from robovast.execution.cluster_execution import in_pod_storage, kubernetes_backend

    calls = []
    backend = kubernetes_backend.KubernetesBackend.__new__(
        kubernetes_backend.KubernetesBackend)
    backend.cluster_config = object()
    backend._state = _State(calls)

    monkeypatch.setattr(in_pod_storage, "campaign_storage_location",
                        lambda _cfg, cid: ("bucket", f"{cid}/"))
    monkeypatch.setattr(in_pod_storage, "storage_client_for",
                        lambda _cfg, **_kw: _FakeStorage({"a": 1024, "b": 2048}))

    backend.ensure_campaign_root_complete(str(tmp_path / "camp-a"))

    stages = [c[1] for c in calls if c[0] == "stage"]
    assert stages, "the transfer left the campaign's stage marker untouched"
    assert "restoring campaign root" in stages[-1]
    # The denominator is the part that distinguishes "nearly done" from "just started".
    assert "2/2" in stages[-1]


def test_a_backend_with_no_state_still_restores(tmp_path, monkeypatch):
    """Narration is a courtesy; the fetch is not. A backend built without a state -- a bare
    `vast run` rather than a campaign the service drives -- must still complete the root."""
    from robovast.execution.cluster_execution import in_pod_storage, kubernetes_backend

    backend = kubernetes_backend.KubernetesBackend.__new__(
        kubernetes_backend.KubernetesBackend)
    backend.cluster_config = object()
    backend._state = None

    monkeypatch.setattr(in_pod_storage, "campaign_storage_location",
                        lambda _cfg, cid: ("bucket", f"{cid}/"))
    monkeypatch.setattr(in_pod_storage, "storage_client_for",
                        lambda _cfg, **_kw: _FakeStorage({"a": 1}))

    backend.ensure_campaign_root_complete(str(tmp_path / "camp-a"))


def test_both_directions_happen_before_postprocessing_reads_the_campaign(monkeypatch):
    """The root is completed FROM the durable home and the driver's records are published
    TO it, and postprocessing needs both: it reads the run tree the completion fetches and
    stages the campaign the publish fills in. Either one after the submit is no better than
    neither."""
    backend = _Backend()

    def _postprocess(_cfg, campaign_id, campaign_root, _ns, **_kw):
        backend.calls.append(("postprocess", campaign_root))
        return True, "done"

    monkeypatch.setattr(postprocess_job, "postprocess_campaign", _postprocess)

    controller._chain_postprocessing(backend, "/results/camp-1", "camp-1",
                                     state=None, options=RunOptions(postprocess=True))

    kinds = [kind for kind, _root in backend.calls]
    assert kinds.index("complete") < kinds.index("postprocess")
    assert kinds.index("publish") < kinds.index("postprocess")
