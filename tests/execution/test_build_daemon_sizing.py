# Copyright (C) 2026 Frederik Pasch
# SPDX-License-Identifier: Apache-2.0
"""How big one image build may be, and how much cache it keeps: set in the ``.env``.

Builds are solved by one long-lived BuildKit daemon, and its ceilings are one build's
ceilings. A build that needs more memory than is there is not rejected -- the kernel kills
the toolchain part-way through, and that is classified as a resource failure whose owner is
the operator rather than as a package the project forgot. Naming that owner is only worth
doing if they then have a knob. The knobs are the deployment's standing configuration, so they
live where its other settings do -- the ``.env`` -- and both commands that own the deployment
apply them, rather than a flag each run has to repeat.

The pod is a Deployment of its own, so nothing here is picked up by a running service: a
setting that cannot be applied must fail, and one that changes must be said.
"""

from unittest import mock

import pytest
from click.testing import CliRunner

from robovast.execution.cluster_execution import buildkitd_deploy
from robovast.execution.cluster_execution import cli as cluster_cli
from robovast.execution.cluster_execution import cluster_setup, service_deploy

#: What the running daemon reports: a cache placed deliberately, and a ceiling somebody raised.
_LIVE = {"storage_class": "fast", "memory_limit": "48Gi", "cpu_limit": "8",
         "max_parallelism": 4}


@pytest.fixture(autouse=True)
def _no_standing_settings(monkeypatch):
    """Each test states the environment it is about; the developer's own is not it."""
    for var in buildkitd_deploy.SETTINGS_ENV.values():
        monkeypatch.delenv(var, raising=False)


@pytest.fixture
def converge(monkeypatch):
    """Stub out everything an upgrade touches except the build daemon it converges.

    Returns the kwargs ``apply_buildkitd`` was called with, so a test sets the environment it
    is about and reads the setting that reached the daemon.
    """
    from robovast.execution.cluster_execution import image_warm, tailnet_deploy

    applied = {}
    monkeypatch.setattr(buildkitd_deploy, "apply_buildkitd",
                        mock.Mock(side_effect=lambda ns, **kw: applied.update(kw)))
    monkeypatch.setattr(buildkitd_deploy, "buildkitd_storage_from_cluster",
                        lambda *a, **k: dict(_LIVE))
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


def _upgrade(*args):
    return CliRunner().invoke(cluster_cli.upgrade, list(args))


def test_an_upgrade_applies_the_size_the_env_states(converge, monkeypatch):
    """The move an out-of-memory build asks for -- a bigger ceiling, or fewer steps sharing it."""
    monkeypatch.setenv("ROBOVAST_BUILDKIT_MEMORY", "64Gi")
    monkeypatch.setenv("ROBOVAST_BUILDKIT_PARALLELISM", "2")
    result = _upgrade()
    assert result.exit_code == 0, result.output
    assert converge["memory_limit"] == "64Gi"
    assert converge["max_parallelism"] == 2


def test_an_upgrade_applies_the_cache_budget_the_env_states(converge, monkeypatch):
    monkeypatch.setenv("ROBOVAST_BUILDKIT_CACHE_MAX", "70%")
    monkeypatch.setenv("ROBOVAST_BUILDKIT_CACHE_MIN_FREE", "200GB")
    monkeypatch.setenv("ROBOVAST_BUILDKIT_CACHE_RESERVED", "50GB")
    result = _upgrade()
    assert result.exit_code == 0, result.output
    assert (converge["gc_max_used"], converge["gc_min_free"], converge["gc_reserved"]) == (
        "70%", "200GB", "50GB")


def test_an_unset_setting_goes_back_to_its_default_and_says_so(converge):
    """Deleting the line resets the setting, as with every other one in the ``.env`` -- and a
    ceiling going back down is the change that must not pass silently, because the build it no
    longer fits is where it would otherwise surface."""
    result = _upgrade()
    assert result.exit_code == 0, result.output
    assert converge["memory_limit"] == ""        # renders the default
    assert "memory ceiling: 48Gi -> 16Gi (ROBOVAST_BUILDKIT_MEMORY unset)" in result.output


def test_an_upgrade_that_changes_nothing_says_nothing(converge, monkeypatch):
    monkeypatch.setenv("ROBOVAST_BUILDKIT_MEMORY", "48Gi")
    result = _upgrade()
    assert result.exit_code == 0, result.output
    assert not [line for line in result.output.splitlines() if " -> " in line]


def test_the_store_is_kept_as_found(converge, monkeypatch):
    """The store and the node pin are placement, recorded nowhere but the daemon: a converge
    that re-rendered them from defaults would hand a PVC-backed cache a fresh empty hostPath."""
    monkeypatch.setenv("ROBOVAST_BUILDKIT_MEMORY", "64Gi")
    result = _upgrade()
    assert result.exit_code == 0, result.output
    assert converge["storage_class"] == "fast"


def test_no_restart_leaves_the_daemon_as_it_is_and_says_how_to_apply(converge, monkeypatch):
    """`--no-restart` returns before the daemon is converged, and converging replaces its pod."""
    monkeypatch.setenv("ROBOVAST_BUILDKIT_MEMORY", "64Gi")
    result = _upgrade("--no-restart")
    assert result.exit_code == 0, result.output
    assert not converge, "--no-restart must not converge the daemon"
    assert "build daemon's settings" in result.output


@pytest.mark.parametrize("var,value", [("ROBOVAST_BUILDKIT_MEMORY", "32 gigs"),
                                       ("ROBOVAST_BUILDKIT_CPU", "plenty"),
                                       ("ROBOVAST_BUILDKIT_PARALLELISM", "0"),
                                       ("ROBOVAST_BUILDKIT_CACHE_MIN_FREE", "lots")])
def test_a_setting_that_is_not_one_fails_before_the_cluster_is_touched(converge, monkeypatch,
                                                                     var, value):
    """The API server's own answer is a 422 quoting the whole manifest, after part of the
    deployment has already changed. The message names the setting that was written."""
    monkeypatch.setenv(var, value)
    result = _upgrade()
    assert result.exit_code != 0
    assert var in result.output
    assert not converge
    service_deploy.deploy_service.assert_not_called()


def test_setup_applies_the_same_settings(monkeypatch):
    """Where the deployment is first described, from the same ``.env``."""
    seen = {}
    monkeypatch.setattr(cluster_setup, "setup_server",
                        lambda **kw: seen.update(kw["buildkit_kwargs"]) or {})
    monkeypatch.setenv("ROBOVAST_BUILDKIT_MEMORY", "64Gi")
    monkeypatch.setenv("ROBOVAST_BUILDKIT_CACHE_MAX", "300GB")
    result = CliRunner().invoke(cluster_cli.setup, ["rke2"])
    assert result.exit_code == 0, result.output
    assert seen["memory_limit"] == "64Gi"
    assert seen["gc_max_used"] == "300GB"


def test_neither_command_takes_them_as_flags():
    """One place to state them. A flag beside the ``.env`` is a second source that the next
    upgrade, reading only the ``.env``, would silently undo."""
    settings = {f"buildkit_{name}" for name in (
        "memory", "cpu", "parallelism", "cache_max", "cache_min_free", "cache_reserved")}
    for command in (cluster_cli.setup, cluster_cli.upgrade):
        assert not settings & {p.name for p in command.params}
