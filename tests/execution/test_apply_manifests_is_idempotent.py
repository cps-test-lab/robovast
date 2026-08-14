# Copyright (C) 2026 Frederik Pasch
# SPDX-License-Identifier: Apache-2.0
"""``apply_manifests`` must survive objects that are already there.

``vast exec cluster setup --force`` is the documented way to move a live cluster to a
new version, and it re-applies each flavor's MinIO manifest over the pod it created the
previous time. The 409 handler logged ``"already exists, skipping creation"`` and then
re-raised regardless -- the ``raise`` sat outside its ``if`` -- so every re-run against a
deployed cluster died with ``pods "robovast" already exists`` before it reached the
service deploy, and the log said the opposite of what the code did.

A unit test is the right level here: it needs no cluster, and the live failure it caused
cost a full setup run to discover.

**These tests raise ``FailToCreateError``, because that is what the real dependency
raises.** ``kubernetes.utils.create_from_dict`` never lets an ``ApiException`` escape --
it collects them and wraps them. A first attempt at this fix caught ``ApiException`` and
was tested with a mock that raised ``ApiException``, so the test passed against a
fiction while the cluster failed exactly as before. The mock's exception type is the
whole point of the test.
"""

from unittest.mock import patch

import pytest
from kubernetes.client.exceptions import ApiException
from kubernetes.utils import FailToCreateError

from robovast.execution.cluster_execution.kubernetes import apply_manifests

POD = {"kind": "Pod", "apiVersion": "v1", "metadata": {"name": "robovast"}}
SERVICE = {"kind": "Service", "apiVersion": "v1", "metadata": {"name": "robovast"}}


def _conflict(status=409, reason="Conflict"):
    """What ``create_from_dict`` actually raises for a single failed object."""
    return FailToCreateError([ApiException(status=status, reason=reason)])


def test_an_existing_object_does_not_abort_the_apply(caplog):
    """The regression: a 409 on the first object stopped setup entirely."""
    with patch("robovast.execution.cluster_execution.kubernetes.utils.create_from_dict",
               side_effect=_conflict()):
        apply_manifests(None, [POD, SERVICE], namespace="default")

    # ...and it says so, because the kept object is not updated to match the manifest.
    assert "already exists" in caplog.text
    assert "cleanup" in caplog.text, "the warning must name how to get the new spec applied"


def test_every_remaining_object_is_still_attempted():
    """A conflict on one object must not skip the ones after it.

    The MinIO manifest is a Pod *and* a Service; aborting on the Pod left the cluster
    without the Service that fronts it.
    """
    seen = []

    def _create(_client, body, **_kwargs):
        seen.append(body["kind"])
        raise _conflict()

    with patch("robovast.execution.cluster_execution.kubernetes.utils.create_from_dict",
               side_effect=_create):
        apply_manifests(None, [POD, SERVICE], namespace="default")

    assert seen == ["Pod", "Service"]


@pytest.mark.parametrize("status, reason", [(403, "Forbidden"), (422, "Invalid"),
                                            (500, "InternalError")])
def test_any_other_api_error_still_fails_loudly(status, reason):
    """Only "it is already there" is benign. A refused or malformed apply is not.

    Swallowing those would let setup report success over a cluster it never configured --
    exactly the silent-weaker-outcome the 409 fix must not generalize into.
    """
    with patch("robovast.execution.cluster_execution.kubernetes.utils.create_from_dict",
               side_effect=_conflict(status, reason)):
        with pytest.raises(RuntimeError, match=reason):
            apply_manifests(None, [POD], namespace="default")


def test_a_conflict_mixed_with_a_real_failure_is_not_swallowed():
    """One object already there, another genuinely refused: that is still a failure.

    ``create_from_dict`` batches a list of exceptions, so "all of them are 409" is the
    only benign case -- checking merely the first would let a Forbidden ride along
    behind a Conflict and report success.
    """
    batched = FailToCreateError([ApiException(status=409, reason="Conflict"),
                                 ApiException(status=403, reason="Forbidden")])
    with patch("robovast.execution.cluster_execution.kubernetes.utils.create_from_dict",
               side_effect=batched):
        with pytest.raises(RuntimeError, match="Forbidden"):
            apply_manifests(None, [POD], namespace="default")
