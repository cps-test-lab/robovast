# Copyright (C) 2026 Frederik Pasch
# SPDX-License-Identifier: Apache-2.0

"""The service half and the lane: what is shared is on the base, what differs is a hook.

``ServiceBase`` holds everything correct for any lane; each thing that depends on where
the runs happen is an abstract hook there, answered in the lane's own class. The cluster
lane is the one production implementer; the suite's null lane is the other, which is what
lets the base's own code be tested without a cluster. A lane that does not offer an
operation refuses it in its own class with ``UnsupportedOnLane``. These tests pin that
shape.
"""

import inspect

from robovast.execution.cluster_execution.cluster_service import ClusterService
from robovast.service.service_base import ServiceBase
from tests.service.null_lane import NullLane


def test_every_hook_is_answered_in_each_lane():
    """The base's guarantee, made visible: a lane missing a hook cannot be constructed,
    and the name of what it lacks is in the error rather than at the first call site."""
    for lane in (ClusterService, NullLane):
        assert not ServiceBase.__abstractmethods__ - set(vars(lane)), (
            f"{lane.__name__} leaves unanswered: "
            f"{sorted(ServiceBase.__abstractmethods__ - set(vars(lane)))}")
        assert not lane.__abstractmethods__


def test_the_base_has_no_body_that_names_a_driver():
    """Correct for any lane means reaching no driver: a lane reaches its own inside the
    hook that needs it, in its own class."""
    source = inspect.getsource(ServiceBase)
    for token in ("subprocess.", "\"docker\"", "import kubernetes", "from kubernetes",
                  "psutil", "KubernetesBackend("):
        assert token not in source, f"the shared base names a driver: {token!r}"


def test_a_refusal_is_written_in_the_lane_that_refuses():
    """``grep UnsupportedOnLane`` lists what a lane declines in full: the base refuses
    nothing, because a refusal is a fact about one lane."""
    assert "UnsupportedOnLane(" not in inspect.getsource(ServiceBase)
    assert "UnsupportedOnLane(" in inspect.getsource(NullLane)
