# Copyright (C) 2026 Frederik Pasch
# SPDX-License-Identifier: Apache-2.0

"""What does not depend on the cluster is on ``ServiceBase``; what does is an abstract hook.

``ClusterService`` answers every hook in production, ``NullService`` in the suite, which is
what lets the base's own code be tested without a cluster. An implementation that does not
offer an operation refuses it in its own class with ``UnsupportedOperation``.
"""

import inspect

from robovast.execution.cluster_execution.cluster_service import ClusterService
from robovast.service.service_base import ServiceBase
from tests.service.null_service import NullService


def test_every_hook_is_answered_in_each_implementation():
    """Each implementation answers every abstract hook in its own class."""
    for impl in (ClusterService, NullService):
        assert not ServiceBase.__abstractmethods__ - set(vars(impl)), (
            f"{impl.__name__} leaves unanswered: "
            f"{sorted(ServiceBase.__abstractmethods__ - set(vars(impl)))}")
        assert not impl.__abstractmethods__


def test_the_base_has_no_body_that_names_a_driver():
    """The base reaches no driver; an implementation reaches its own inside the hook that
    needs it."""
    source = inspect.getsource(ServiceBase)
    for token in ("subprocess.", "\"docker\"", "import kubernetes", "from kubernetes",
                  "psutil", "KubernetesBackend("):
        assert token not in source, f"the shared base names a driver: {token!r}"


def test_a_refusal_is_written_in_the_implementation_that_refuses():
    """The base refuses nothing; a refusal is written in the implementation that declines."""
    assert "UnsupportedOperation(" not in inspect.getsource(ServiceBase)
    assert "UnsupportedOperation(" in inspect.getsource(NullService)
