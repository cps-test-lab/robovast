# Copyright (C) 2026 Frederik Pasch
# SPDX-License-Identifier: Apache-2.0

"""The two execution lanes are siblings: neither inherits the other's answer.

``LocalTransport`` and ``ClusterService`` each subclass ``ServiceBase`` and nothing else
of each other's. Where one lane subclasses the other, whatever it does not override is
answered by the other lane's body -- an image store reaching a Docker daemon inside a pod,
a build capability vouched for without a registry, a container teardown one predicate away
from running in a controller pod -- and nothing says so until a caller reaches it on the
lane that inherited it.

What the lanes share is on the base and correct for any lane; what differs is an abstract
hook there, answered in each lane's own class. A lane that does not offer an operation
refuses it in its own class with ``UnsupportedOnLane``. These tests pin that shape, so a
local default is written where the cluster cannot inherit it.
"""

import inspect

from robovast.execution.cluster_execution.cluster_service import ClusterService
from robovast.service.local_transport import LocalTransport
from robovast.service.service_base import ServiceBase


def test_the_lanes_are_siblings_over_the_base():
    assert issubclass(LocalTransport, ServiceBase)
    assert issubclass(ClusterService, ServiceBase)
    assert not issubclass(ClusterService, LocalTransport)
    assert not issubclass(LocalTransport, ClusterService)


def test_every_hook_is_answered_in_each_lane():
    """The base's guarantee, made visible: a lane missing a hook cannot be constructed,
    and the name of what it lacks is in the error rather than at the first call site."""
    assert not ServiceBase.__abstractmethods__ - set(vars(LocalTransport)), (
        f"the local lane leaves unanswered: "
        f"{sorted(ServiceBase.__abstractmethods__ - set(vars(LocalTransport)))}")
    assert not ServiceBase.__abstractmethods__ - set(vars(ClusterService)), (
        f"the cluster lane leaves unanswered: "
        f"{sorted(ServiceBase.__abstractmethods__ - set(vars(ClusterService)))}")
    assert not LocalTransport.__abstractmethods__
    assert not ClusterService.__abstractmethods__


def test_the_base_has_no_body_that_names_a_driver():
    """Correct for any lane means reaching neither Docker nor Kubernetes: a lane reaches
    its driver inside the hook that needs it, in its own class."""
    source = inspect.getsource(ServiceBase)
    for token in ("subprocess.", "\"docker\"", "import kubernetes", "from kubernetes",
                  "psutil", "DockerBackend(", "DockerExecLane(", "LocalDockerImageStore("):
        assert token not in source, f"the shared base names a driver: {token!r}"


def test_the_local_teardown_lives_in_the_local_class_alone():
    """The ``docker rm -f`` and the container it names are the local lane's answer to
    exiting, written where the cluster lane cannot reach them."""
    for name in ("_kill_scenario_container", "_CONTAINER_NAME", "_SHUTDOWN_JOIN_SECONDS",
                 "_job_container", "_disk_space"):
        assert name in vars(LocalTransport)
        assert not hasattr(ClusterService, name), f"the cluster lane has {name}"
        assert not hasattr(ServiceBase, name), f"the shared base has {name}"


def test_a_refusal_is_written_in_the_lane_that_refuses():
    """``grep UnsupportedOnLane`` lists the asymmetry between the lanes in full: the base
    refuses nothing, because a refusal is a fact about one lane."""
    assert "UnsupportedOnLane(" not in inspect.getsource(ServiceBase)
    assert "UnsupportedOnLane(" in inspect.getsource(LocalTransport)
    assert "UnsupportedOnLane(" in inspect.getsource(ClusterService)
