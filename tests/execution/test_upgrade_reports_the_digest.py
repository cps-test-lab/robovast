# Copyright (C) 2026 Frederik Pasch
# SPDX-License-Identifier: Apache-2.0
"""``upgrade`` reports the digest it rolled onto, not the ref it was given.

With a floating ``:latest`` the Deployment spec is byte-identical before and after, so
"✓ upgraded and ready" was equally true whether new bytes arrived or the same image
restarted. ``imageID`` is what the kubelet actually resolved the pull to, which answers
that regardless of how the ref was spelled.
"""

# pylint: disable=redefined-outer-name  # the pytest fixture idiom

from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

from robovast.execution.cluster_execution import service_deploy


def _pod(name, digest, *, phase="Running", created=1, container="robovast-service"):
    image_id = f"docker.io/freeedlabs/robovast-controller@{digest}" if digest else ""
    return SimpleNamespace(
        metadata=SimpleNamespace(name=name, creation_timestamp=created),
        status=SimpleNamespace(
            phase=phase,
            container_statuses=[SimpleNamespace(name=container, image_id=image_id)]))


@pytest.fixture
def pods(monkeypatch):
    """Serve a pod list to `running_image_digest` without a cluster."""
    listed = []

    core = MagicMock()
    core.list_namespaced_pod.return_value = SimpleNamespace(items=listed)
    api = MagicMock(CoreV1Api=MagicMock(return_value=core))

    monkeypatch.setattr(service_deploy, "_load_kube_config", lambda *a, **k: None)
    monkeypatch.setitem(__import__("sys").modules, "kubernetes",
                        SimpleNamespace(client=api))
    return listed


def test_the_running_pods_digest_is_reported(pods):
    pods.append(_pod("svc-1", "sha256:abc123"))
    assert service_deploy.running_image_digest() == "sha256:abc123"


def test_a_rollout_reads_the_new_pod_not_the_old_one(pods):
    """Both generations are Running mid-rollout, and the old one's digest is precisely
    the wrong answer to "what is running now?"."""
    pods.append(_pod("svc-old", "sha256:old", created=1))
    pods.append(_pod("svc-new", "sha256:new", created=2))

    assert service_deploy.running_image_digest() == "sha256:new"


def test_a_pod_that_is_not_running_is_not_the_answer(pods):
    pods.append(_pod("svc-crash", "sha256:zzz", phase="Pending", created=9))
    assert service_deploy.running_image_digest() == ""


def test_reporting_never_breaks_an_upgrade_that_worked(monkeypatch):
    """The digest line is commentary. An upgrade that rolled correctly must not fail
    because the thing describing it could not read a field."""
    def _explode(*_a, **_k):
        raise RuntimeError("API server said no")

    monkeypatch.setattr(service_deploy, "_load_kube_config", _explode)
    assert service_deploy.running_image_digest() == ""


def test_a_digestless_image_id_is_passed_through(pods):
    """Older kubelets report a bare id with no `@sha256:` part. Returning it is more
    use than returning nothing -- two upgrades still compare."""
    pod = _pod("svc-1", "")
    pod.status.container_statuses[0].image_id = "docker://0123456789ab"
    pods.append(pod)

    assert service_deploy.running_image_digest() == "docker://0123456789ab"


def _run_upgrade(before, after, converged=True):
    from click.testing import CliRunner

    from robovast.execution.cluster_execution.cli import upgrade

    digests = iter([before, after])
    with patch.multiple(
            "robovast.execution.cluster_execution.service_deploy",
            read_service_config_from_cluster=MagicMock(return_value=("rke2", {})),
            published_host=MagicMock(return_value="robovast.example"),
            reconcile_registry_ingress_path=MagicMock(return_value=False),
            deploy_service=MagicMock(),
            wait_for_service_ready=MagicMock(),
            wait_for_rollout=MagicMock(return_value=converged),
            running_image_digest=MagicMock(side_effect=lambda *a, **k: next(digests))), \
            patch("robovast.execution.cluster_execution.cluster_setup."
                  "apply_controller_rbac", MagicMock()), \
            patch("robovast.execution.cluster_execution.kubernetes_kueue."
                  "apply_kueue_queues", MagicMock()):
        return CliRunner().invoke(upgrade, ["-n", "default"])


@pytest.mark.parametrize("before,after,expected", [
    ("sha256:aaaaaaaaaaaaaaaaaaaa", "sha256:bbbbbbbbbbbbbbbbbbbb", "->"),
    ("sha256:aaaaaaaaaaaaaaaaaaaa", "sha256:aaaaaaaaaaaaaaaaaaaa", "unchanged"),
])
def test_upgrade_says_whether_the_bytes_changed(before, after, expected):
    """The whole point: `:latest` in .env, and the output still distinguishes the two."""
    result = _run_upgrade(before, after)

    assert result.exit_code == 0, result.output
    assert expected in result.output, result.output
    assert "✓ upgraded and ready" in result.output


def test_the_digest_is_read_only_after_the_rollout_converges():
    """The bug this shipped with.

    `wait_for_service_ready` returns as soon as one replica is Ready -- which the *old*
    pod satisfies for the whole of a rolling update. Reading the digest there read the
    outgoing pod both times, so a genuine image change reported "image unchanged", and
    the operator had to exec into the pod to find out otherwise. A report that has not
    settled must say so rather than assert something false.
    """
    result = _run_upgrade("sha256:aaaaaaaaaaaaaaaaaaaa", "sha256:bbbbbbbbbbbbbbbbbbbb",
                          converged=False)

    assert result.exit_code == 0, result.output
    assert "unchanged" not in result.output
    assert "->" not in result.output
    assert "not settled" in result.output
    assert "rollout status" in result.output, "must name how to check"
    assert "✓ upgraded and ready" in result.output
