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
from robovast.execution.cluster_execution import postprocess_job


class _Backend:
    """A cluster-lane backend stubbed to what ``_chain_postprocessing`` touches."""

    def __init__(self):
        self.cluster_config = object()
        self.calls = []

    def ensure_campaign_root_complete(self, campaign_root):
        self.calls.append(("complete", campaign_root))


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

    assert backend.calls == [("complete", "/results/camp-a"),
                             ("postprocess", "/results/camp-a")], (
        "the root has to be whole BEFORE data.db is derived from it; afterwards is a "
        "truncated campaign that reports success")


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
