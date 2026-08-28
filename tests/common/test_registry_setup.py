# Copyright (C) 2026 Frederik Pasch
# SPDX-License-Identifier: Apache-2.0
"""Registry config wiring in `vast cluster setup` (service_deploy).

Two registries meet in this config and are easily conflated, which is what these tests
pin apart:

* the **build target** is the registry running in the service pod, so its prefix is
  *derived* from the service's own Ingress host and cannot be configured to somewhere the
  cluster could not pull from;
* the **pull credential** is configured, because only the operator knows the credentials
  for a private registry a ``.vast`` happens to name.

Delivered to the in-cluster service through the same envFrom credential-Secret pattern as
share/ntfy.
"""

import json

import pytest

from robovast.execution.cluster_execution import registry_deploy
from robovast.execution.cluster_execution import service_deploy as sd

_REG_VARS = ["ROBOVAST_REGISTRY_PREFIX", "ROBOVAST_REGISTRY_SERVER",
             "ROBOVAST_REGISTRY_USERNAME", "ROBOVAST_REGISTRY_PASSWORD",
             "ROBOVAST_BASE_EXPERIMENT_IMAGE", "ROBOVAST_REGISTRY_CA_FILE",
             "ROBOVAST_REGISTRY_INSECURE", "ROBOVAST_EXTRA_HOST_ALIASES"]

HOST = "robovast.example.org"


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch):
    for v in _REG_VARS:
        monkeypatch.delenv(v, raising=False)


def test_nothing_configured_and_no_ingress_means_no_registry_config():
    assert sd._registry_env() is None
    assert sd._registry_dockerconfig_manifest("default") is None
    assert sd.REGISTRY_CONFIG_SECRET_NAME in [n for n, _ in sd.ENV_SECRET_SOURCES]


def test_the_build_prefix_is_the_ingress_host():
    """No ``ROBOVAST_REGISTRY_PREFIX`` anywhere -- the published host *is* the prefix."""
    env = sd._registry_env(HOST)
    assert env == {"ROBOVAST_REGISTRY_PREFIX": HOST}


def test_a_configured_prefix_cannot_override_the_in_pod_registry(monkeypatch):
    """The old escape hatch is gone on purpose.

    Pointing builds at an external registry is what required push credentials and made a
    registry a site prerequisite. The prefix now follows the Ingress, so a stale
    ``ROBOVAST_REGISTRY_PREFIX`` in someone's .env cannot quietly redirect pushes.
    """
    monkeypatch.setenv("ROBOVAST_REGISTRY_PREFIX", "ghcr.io/someone-else")
    # pylint: disable-next=unsubscriptable-object  -- _registry_env returns the dict for a real host; None is only the empty-host case, which has its own test.
    assert sd._registry_env(HOST)["ROBOVAST_REGISTRY_PREFIX"] == HOST


def test_without_an_ingress_there_is_no_build_prefix():
    """An unpublished service has no registry the node could pull from.

    Better to have no prefix -- builds then refuse -- than one that produces refs which
    fail at pull time, after the build has already been paid for.
    """
    monkeypatch_free = sd._registry_env("")
    # pylint: disable-next=unsupported-membership-test  -- the None case is handled on this very line
    assert monkeypatch_free is None or "ROBOVAST_REGISTRY_PREFIX" not in monkeypatch_free


def test_credentials_wire_a_pull_secret_but_never_a_push_one(monkeypatch):
    monkeypatch.setenv("ROBOVAST_REGISTRY_SERVER", "harbor.example.org")
    monkeypatch.setenv("ROBOVAST_REGISTRY_USERNAME", "u")
    monkeypatch.setenv("ROBOVAST_REGISTRY_PASSWORD", "p")
    env = sd._registry_env(HOST)
    # pylint: disable-next=unsubscriptable-object  -- _registry_env returns the dict for a real host; None is only the empty-host case, which has its own test.
    assert env["ROBOVAST_REGISTRY_PULL_SECRET"] == sd.REGISTRY_PUSH_SECRET_NAME
    # pylint: disable-next=unsupported-membership-test  -- _registry_env returns the dict for a real host; None is only the empty-host case, which has its own test.
    assert "ROBOVAST_REGISTRY_PUSH_SECRET" not in env, (
        "the in-pod registry is open; a push credential would be a leftover")

    secret = sd._registry_dockerconfig_manifest("default")
    assert secret["type"] == "kubernetes.io/dockerconfigjson"
    auths = json.loads(secret["stringData"][".dockerconfigjson"])["auths"]
    assert "harbor.example.org" in auths


def test_a_base_experiment_image_still_passes_through(monkeypatch):
    monkeypatch.setenv("ROBOVAST_BASE_EXPERIMENT_IMAGE", "ghcr.io/org/base:latest")
    # pylint: disable-next=unsubscriptable-object  -- _registry_env returns the dict for a real host; None is only the empty-host case, which has its own test.
    assert sd._registry_env(HOST)["ROBOVAST_BASE_EXPERIMENT_IMAGE"] == \
        "ghcr.io/org/base:latest"


def test_host_aliases_still_pass_through(monkeypatch):
    monkeypatch.setenv("ROBOVAST_EXTRA_HOST_ALIASES", "a.example=10.0.0.1")
    # pylint: disable-next=unsubscriptable-object  -- _registry_env returns the dict for a real host; None is only the empty-host case, which has its own test.
    assert sd._registry_env(HOST)["ROBOVAST_EXTRA_HOST_ALIASES"] == "a.example=10.0.0.1"


def test_removing_credentials_deletes_the_secret_rather_than_leaving_it_deployed():
    """Rotation always worked; removal was silently ignored.

    The Secret is discovered *by existence* and wired to the Deployment as an
    imagePullSecret, so deleting the password from .env and re-running upgrade reported
    success while the credential stayed deployed and in use -- the opposite of what an
    operator revoking access has just asked for.
    """
    from unittest import mock

    core = mock.Mock()
    # Nothing configured, so this deploy builds no credential objects at all.
    sd._delete_unconfigured_credentials(core, "default", secrets=[], configmaps=[])

    deleted = {c.args[0] for c in core.delete_namespaced_secret.call_args_list}
    assert sd.REGISTRY_PUSH_SECRET_NAME in deleted
    assert sd.GIT_SECRET_NAME in deleted
    deleted_cms = {c.args[0] for c in core.delete_namespaced_config_map.call_args_list}
    assert sd.REGISTRY_CA_CONFIGMAP_NAME in deleted_cms


def test_a_credential_this_deploy_built_is_never_deleted():
    from unittest import mock

    core = mock.Mock()
    secret = {"metadata": {"name": sd.REGISTRY_PUSH_SECRET_NAME}}
    sd._delete_unconfigured_credentials(core, "default", secrets=[secret], configmaps=[])

    deleted = {c.args[0] for c in core.delete_namespaced_secret.call_args_list}
    assert sd.REGISTRY_PUSH_SECRET_NAME not in deleted


def test_the_registry_prefix_is_a_bare_host():
    """A registry lives at the root of its host's /v2 namespace, so the ref carries no
    path component -- ``<host>/<tag>:<hash>``."""
    assert registry_deploy.registry_prefix("robovast.example.org") == "robovast.example.org"
    assert registry_deploy.registry_prefix("") == ""
    assert registry_deploy.registry_prefix(None) == ""
