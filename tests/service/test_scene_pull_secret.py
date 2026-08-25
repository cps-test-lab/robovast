# Copyright (C) 2026 Frederik Pasch
# SPDX-License-Identifier: Apache-2.0
"""Aux/exec pods must carry the pull credential when there is one.

``_scene_pull_secret`` (now ``_registry_pull_secret``, since the diagnostic exec pod needs
the same credential and had the same omission) imported ``REGISTRY_PUSH_SECRET_NAME`` from
``cluster_execution.cluster_execution``, which does not define it. A bare
``except Exception`` swallowed the resulting ImportError, so the function always returned
``""`` and the aux pods it feeds never got an ``imagePullSecret`` -- the precise failure
its own docstring says it was written to prevent.

Nothing caught it because it fails *open*: a public aux image needs no credential, and a
node that has already cached the campaign image does not need one either
(``imagePullPolicy: IfNotPresent``). It surfaces on a fresh node pulling a private image,
which is the worst place to find out. So this asserts the non-empty case, which the bug
made unreachable, rather than only the absent-Secret case, which it accidentally got right.

The lookup now lives in the image store -- which Secret pulls from this registry is the
registry's business -- so these drive it through the service the way its callers do.
"""

from unittest import mock

import pytest
from kubernetes.client.rest import ApiException

from robovast.execution.cluster_execution.cluster_service import ClusterService
from robovast.execution.cluster_execution.service_deploy import REGISTRY_PUSH_SECRET_NAME


def _service(k8s):
    from robovast.execution.cluster_config.base_config import RegistryConfig

    class _Cfg:
        def get_registry_config(self):
            # A deployment that named nothing explicitly: the store fills the Secret in by
            # looking for it, which is the behaviour under test.
            return RegistryConfig(registry_prefix="registry.local:5000/robovast")

    service = object.__new__(ClusterService)
    service.namespace = "default"
    service._k8s = lambda: k8s
    service._cluster_config = _Cfg
    return service


def test_an_existing_secret_is_returned():
    k8s = mock.Mock()
    k8s.read_namespaced_secret.return_value = object()
    assert _service(k8s)._registry_pull_secret() == REGISTRY_PUSH_SECRET_NAME
    k8s.read_namespaced_secret.assert_called_once_with(
        REGISTRY_PUSH_SECRET_NAME, "default")


def test_an_absent_secret_means_no_credential():
    """A public image legitimately needs none -- the one case that may return ""."""
    k8s = mock.Mock()
    k8s.read_namespaced_secret.side_effect = ApiException(status=404)
    assert _service(k8s)._registry_pull_secret() == ""


def test_an_unexpected_error_is_not_swallowed():
    """The bug was a too-wide except. A broken cluster connection is not "no credential":
    reporting it as such produces a pod that cannot pull, with nothing said about why."""
    k8s = mock.Mock()
    k8s.read_namespaced_secret.side_effect = RuntimeError("connection refused")
    with pytest.raises(RuntimeError):
        _service(k8s)._registry_pull_secret()
