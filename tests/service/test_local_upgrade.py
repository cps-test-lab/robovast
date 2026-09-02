# Copyright (C) 2026 Frederik Pasch
# SPDX-License-Identifier: Apache-2.0
"""Whether a local service can restart itself depends on how it was STARTED, not on its lane.

``vast service restart`` used to refuse on the local lane unconditionally, with "this
service is not a Kubernetes Deployment". That was right while the only way to run locally
was a venv, and it stops being right the moment the service runs in a container: it can exit
into a restart policy, and the lane it drives has nothing to do with it.

So the refusal narrows from a statement about the lane to a statement about the deployment,
which is the honest version and the one an operator can act on.

What comes back is the *same image*: Docker re-runs the container it was given, and a
container is pinned to the image id it was created from, so no restart re-resolves a tag.
That is why the message asserted below says so -- the cluster's restart is a roll onto new
bytes and this one is not, and a caller told otherwise would wait for a version change that
cannot arrive.
"""

import threading

import pytest

from robovast.service.client import LocalTransport
from robovast.service.sibling_paths import IN_CONTAINER_ENV
from robovast.service.workspaces import WorkspaceRegistry, WorkspaceStore


@pytest.fixture
def transport(tmp_path):
    # The real constructor: upgrade_info lists campaigns, and the listing reads several
    # caches a hand-built object would have to keep in step with as they change.
    store = WorkspaceStore(registry=WorkspaceRegistry(root=tmp_path / "workspaces"))
    return LocalTransport(store=store, results_dir=str(tmp_path / "results"))


def test_a_venv_service_still_refuses_and_names_the_way_it_is_updated(transport,
                                                                      monkeypatch):
    monkeypatch.delenv(IN_CONTAINER_ENV, raising=False)
    info = transport.upgrade_info()
    assert info.supported is False
    # The refusal must say how this deployment *is* updated, not merely that it cannot be:
    # a caller told only "unsupported" has been moved along, not helped.
    assert "venv" in info.unsupported_reason
    assert "the way it was installed" in info.unsupported_reason

    with pytest.raises(ValueError, match="nothing to roll"):
        transport.upgrade_service()


def test_forcing_does_not_make_a_venv_into_an_image(transport, monkeypatch):
    monkeypatch.delenv(IN_CONTAINER_ENV, raising=False)
    with pytest.raises(ValueError, match="nothing to roll"):
        transport.upgrade_service(force=True)


def test_a_containerised_service_can_roll(transport, monkeypatch):
    monkeypatch.setenv(IN_CONTAINER_ENV, "1")
    info = transport.upgrade_info()
    assert info.supported is True
    assert not info.unsupported_reason


def test_the_roll_defers_its_exit_so_the_reply_gets_out(transport, monkeypatch):
    """Inline exit would drop the connection and report a failure for a restart that worked.

    The cluster needs no equivalent: there the handler stamps an annotation and *Kubernetes*
    replaces the pod, so something other than the responder does the killing.
    """
    monkeypatch.setenv(IN_CONTAINER_ENV, "1")
    torn_down = threading.Event()
    monkeypatch.setattr(transport, "_exit_after_reply", torn_down.set)

    result = transport.upgrade_service()

    assert result.ok is True
    assert "restarting" in result.message
    # The difference from the cluster's roll, in the sentence an operator reads: a restart
    # policy re-runs the container it was given, so this is not how newer bytes arrive.
    assert "SAME image" in result.message
    assert "--pull always" in result.message
    assert torn_down.is_set(), "the teardown must be scheduled, not skipped"


def test_a_live_campaign_blocks_the_roll_unless_forced(transport, monkeypatch):
    from robovast.client.status import Phase
    from robovast.execution.control_server import ControllerState
    from robovast.service.local_transport import _LocalCampaign

    monkeypatch.setenv(IN_CONTAINER_ENV, "1")
    state = ControllerState(campaign_id="camp-live")
    state.set_phase(Phase.RUNNING)
    transport._campaigns["camp-live"] = _LocalCampaign(
        "camp-live", "results", state, workspace_id="ws-1")

    # RuntimeError, not ValueError: 409 rather than 400, because this is a conflict the
    # caller can resolve and retry -- the same shape the cluster gives the same refusal.
    with pytest.raises(RuntimeError, match="camp-live"):
        transport.upgrade_service()

    # Forced, it proceeds — the operator has said they accept losing the run.
    scheduled = threading.Event()
    monkeypatch.setattr(transport, "_exit_after_reply", scheduled.set)
    assert transport.upgrade_service(force=True).ok is True
    assert scheduled.is_set()
