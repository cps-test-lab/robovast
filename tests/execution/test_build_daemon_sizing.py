# Copyright (C) 2026 Frederik Pasch
# SPDX-License-Identifier: Apache-2.0
"""How big one image build may be, reached from the two commands that own the deployment.

Builds are solved by one long-lived BuildKit daemon, and its ceilings are one build's
ceilings. A build that needs more memory than is there is not rejected -- the kernel kills
the toolchain part-way through, and that is classified as a resource failure whose owner is
the operator rather than as a package the project forgot. Naming that owner is only worth
doing if they then have a knob, so these assert the knob reaches the daemon: from ``setup``,
from ``upgrade``, and from the environment either reads.

The pod is a Deployment of its own, so nothing here is picked up by a running service: a
value that cannot be applied must fail rather than be reported as applied.
"""

from unittest import mock

import pytest
from click.testing import CliRunner

from robovast.execution.cluster_execution import buildkitd_deploy
from robovast.execution.cluster_execution import cli as cluster_cli
from robovast.execution.cluster_execution import cluster_setup, service_deploy


@pytest.fixture
def converge(monkeypatch):
    """Stub out everything an upgrade touches except the build daemon it converges.

    Returns the kwargs ``apply_buildkitd`` was called with, so a test states the flag it is
    about and reads the setting that reached the daemon.
    """
    from robovast.execution.cluster_execution import image_warm, tailnet_deploy

    applied = {}
    monkeypatch.setattr(buildkitd_deploy, "apply_buildkitd",
                        mock.Mock(side_effect=lambda ns, **kw: applied.update(kw)))
    # A daemon that is already there, with a cache somebody placed deliberately: every
    # converge below has to hand that back untouched while changing the size.
    monkeypatch.setattr(buildkitd_deploy, "buildkitd_storage_from_cluster",
                        lambda *a, **k: {"storage_class": "fast", "memory_limit": "16Gi",
                                         "max_parallelism": 4})
    monkeypatch.setattr(cluster_setup, "apply_controller_rbac", mock.Mock())
    monkeypatch.setattr(image_warm, "warm_family_images", lambda *a, **k: [])
    monkeypatch.setattr(service_deploy, "read_service_config_from_cluster",
                        lambda *a, **k: ("rke2", {"namespace": "default"}))
    monkeypatch.setattr(service_deploy, "published_url", lambda *a, **k: "")
    monkeypatch.setattr(service_deploy, "deploy_service", mock.Mock())
    monkeypatch.setattr(service_deploy, "ensure_registry_htpasswd", lambda *a, **k: "rpw")
    monkeypatch.setattr(service_deploy, "wait_for_service_ready", mock.Mock())
    monkeypatch.setattr(service_deploy, "wait_for_rollout", lambda **k: None)
    monkeypatch.setattr(service_deploy, "running_image_digest", lambda *a, **k: "sha256:abc")
    monkeypatch.setattr(service_deploy, "verify_store_pod_infrastructure", lambda *a, **k: None)
    monkeypatch.setattr(service_deploy, "reconcile_registry_ingress_path", lambda **k: False)
    monkeypatch.setattr(tailnet_deploy, "reconcile_existing", lambda *a, **k: "")
    monkeypatch.setattr(cluster_cli, "_live_campaigns", lambda: [])
    return applied


def test_an_upgrade_raises_the_ceiling_a_build_did_not_fit_under(converge):
    """The move an out-of-memory build asks for, from the command an operator already runs
    to change the deployment."""
    result = CliRunner().invoke(cluster_cli.upgrade, ["--buildkit-memory", "48Gi"])
    assert result.exit_code == 0, result.output
    assert converge["memory_limit"] == "48Gi"


def test_the_cheaper_half_of_the_answer_is_reachable_the_same_way(converge):
    """Peak memory is roughly the concurrent steps times the heaviest compile, so this fits
    a build under a ceiling the nodes cannot raise."""
    result = CliRunner().invoke(cluster_cli.upgrade, ["--buildkit-parallelism", "2"])
    assert result.exit_code == 0, result.output
    assert converge["max_parallelism"] == 2


def test_the_environment_supplies_it_like_every_other_deployment_setting(converge,
                                                                        monkeypatch):
    """A ``.vast`` cannot say how big its build is, so the size belongs to the deployment --
    and the deployment's settings live in its ``.env`` rather than in one operator's shell
    history. Unset, the daemon keeps what it has."""
    monkeypatch.setenv("ROBOVAST_BUILDKIT_PARALLELISM", "2")
    monkeypatch.setenv("ROBOVAST_BUILDKIT_MEMORY", "48Gi")
    result = CliRunner().invoke(cluster_cli.upgrade, [])
    assert result.exit_code == 0, result.output
    assert converge["max_parallelism"] == 2
    assert converge["memory_limit"] == "48Gi"


def test_an_upgrade_that_says_nothing_changes_nothing(converge):
    """The recovered settings, not the defaults: a ceiling raised because a build did not
    fit under it must survive the next version bump, and so must the cache."""
    result = CliRunner().invoke(cluster_cli.upgrade, [])
    assert result.exit_code == 0, result.output
    assert converge["memory_limit"] == "16Gi"
    assert converge["max_parallelism"] == 4
    assert converge["storage_class"] == "fast"


def test_changing_the_size_leaves_the_cache_where_it_is(converge):
    """The store, the GC budget and the node pin arrive as `setup` flags and are recorded
    nowhere else, so a converge that re-rendered from defaults would hand a PVC-backed cache
    a fresh empty hostPath while the old claim still holds its space."""
    result = CliRunner().invoke(cluster_cli.upgrade, ["--buildkit-memory", "48Gi"])
    assert result.exit_code == 0, result.output
    assert converge["storage_class"] == "fast"


def test_no_restart_refuses_a_ceiling_it_would_have_dropped(converge):
    """`--no-restart` returns before the daemon is converged, so a size typed beside it
    would be accepted and then ignored -- an upgrade reporting success while the ceiling the
    operator came to raise is still the old one."""
    result = CliRunner().invoke(
        cluster_cli.upgrade, ["--no-restart", "--buildkit-memory", "48Gi"])
    assert result.exit_code != 0
    assert "--buildkit-memory" in result.output and "ignored" in result.output
    assert not converge


def test_a_standing_environment_value_does_not_block_no_restart(converge, monkeypatch):
    """The refusal is about what this run asked for. A deployment whose `.env` carries a
    ceiling has not asked for anything by running `--no-restart`, and refusing there would
    make the flag unusable on exactly the deployments that set one."""
    monkeypatch.setenv("ROBOVAST_BUILDKIT_MEMORY", "48Gi")
    result = CliRunner().invoke(cluster_cli.upgrade, ["--no-restart"])
    assert result.exit_code == 0, result.output
    assert not converge, "--no-restart must not converge the daemon"


@pytest.mark.parametrize("flag,value", [("--buildkit-memory", "32 gigs"),
                                        ("--buildkit-cpu", "plenty"),
                                        ("--buildkit-parallelism", "0")])
def test_a_size_that_is_not_one_is_refused_before_the_cluster_is_touched(converge, flag,
                                                                        value):
    """The API server's own answer is a 422 quoting the whole manifest, and on `setup` it
    arrives after the service is already up -- so the deployment is left half built over a
    typo. The message names the flag that was typed."""
    result = CliRunner().invoke(cluster_cli.upgrade, [flag, value])
    assert result.exit_code != 0
    assert flag in result.output
    assert not converge


def test_setup_takes_the_same_three():
    """Where the deployment is first described, so a cluster whose nodes are large can say
    so once instead of being upgraded immediately afterwards."""
    import inspect

    params = {p.name for p in cluster_cli.setup.params}
    assert {"buildkit_memory", "buildkit_cpu", "buildkit_parallelism"} <= params
    # And they reach the daemon: `setup` hands `apply_buildkitd` its buildkit_kwargs whole,
    # so the contract that matters is that these are names it accepts.
    accepted = set(inspect.signature(buildkitd_deploy.apply_buildkitd).parameters)
    assert {"memory_limit", "cpu_limit", "max_parallelism"} <= accepted
