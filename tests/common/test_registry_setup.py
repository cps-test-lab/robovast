# Copyright (C) 2026 Frederik Pasch
# SPDX-License-Identifier: Apache-2.0
"""Registry config wiring in `vast exec cluster setup` (service_deploy).

Registry details are delivered to the in-cluster service via the same envFrom
credential-Secret pattern as share/ntfy; auth (when given) becomes a
dockerconfigjson Secret used for push (build Job) and pull (campaign pods).
"""

import json

import pytest

from robovast.execution.cluster_execution import service_deploy as sd

_REG_VARS = ["ROBOVAST_REGISTRY_PREFIX", "ROBOVAST_REGISTRY_SERVER",
             "ROBOVAST_REGISTRY_USERNAME", "ROBOVAST_REGISTRY_PASSWORD",
             "ROBOVAST_BASE_EXPERIMENT_IMAGE", "ROBOVAST_REGISTRY_CA_FILE",
             "ROBOVAST_REGISTRY_INSECURE"]


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch):
    for v in _REG_VARS:
        monkeypatch.delenv(v, raising=False)
    # _load_setup_dotenv may read a project .env; neutralize it for the test.
    monkeypatch.setattr(sd, "_load_setup_dotenv", lambda: None)


def test_disabled_by_default():
    assert sd._registry_env_from_host() is None
    assert sd._registry_dockerconfig_manifest("default") is None
    assert sd.REGISTRY_CONFIG_SECRET_NAME in [n for n, _ in sd.ENV_SECRET_SOURCES]


def test_insecure_registry_prefix_only(monkeypatch):
    monkeypatch.setenv("ROBOVAST_REGISTRY_PREFIX", "registry.default.svc:5000/rv")
    env = sd._registry_env_from_host()
    assert env == {"ROBOVAST_REGISTRY_PREFIX": "registry.default.svc:5000/rv"}
    # No auth → no push secret referenced, no dockerconfigjson Secret.
    assert "ROBOVAST_REGISTRY_PUSH_SECRET" not in env
    assert sd._registry_dockerconfig_manifest("default") is None


def test_external_registry_with_auth(monkeypatch):
    monkeypatch.setenv("ROBOVAST_REGISTRY_PREFIX", "ghcr.io/org")
    monkeypatch.setenv("ROBOVAST_REGISTRY_SERVER", "ghcr.io")
    monkeypatch.setenv("ROBOVAST_REGISTRY_USERNAME", "u")
    monkeypatch.setenv("ROBOVAST_REGISTRY_PASSWORD", "p")
    env = sd._registry_env_from_host()
    assert env["ROBOVAST_REGISTRY_PUSH_SECRET"] == sd.REGISTRY_PUSH_SECRET_NAME
    assert env["ROBOVAST_REGISTRY_PULL_SECRET"] == sd.REGISTRY_PUSH_SECRET_NAME

    secret = sd._registry_dockerconfig_manifest("default")
    assert secret["type"] == "kubernetes.io/dockerconfigjson"
    assert secret["metadata"]["name"] == sd.REGISTRY_PUSH_SECRET_NAME
    auths = json.loads(secret["stringData"][".dockerconfigjson"])["auths"]
    assert "ghcr.io" in auths


def test_registry_ca_configmap(monkeypatch, tmp_path):
    ca = tmp_path / "ca.pem"
    ca.write_text("-----BEGIN CERTIFICATE-----\nMIIB...\n-----END CERTIFICATE-----\n")
    monkeypatch.setenv("ROBOVAST_REGISTRY_PREFIX", "harbor.example/robovast")
    monkeypatch.setenv("ROBOVAST_REGISTRY_CA_FILE", str(ca))
    # env references the CA ConfigMap so the service's get_registry_config picks it up
    env = sd._registry_env_from_host()
    assert env["ROBOVAST_REGISTRY_CA_CONFIGMAP"] == sd.REGISTRY_CA_CONFIGMAP_NAME
    # and the ConfigMap carries the CA under key ca.pem
    cm = sd._registry_ca_manifest("default")
    assert cm["kind"] == "ConfigMap"
    assert cm["metadata"]["name"] == sd.REGISTRY_CA_CONFIGMAP_NAME
    assert "BEGIN CERTIFICATE" in cm["data"]["ca.pem"]


def test_registry_insecure_passthrough(monkeypatch):
    monkeypatch.setenv("ROBOVAST_REGISTRY_PREFIX", "reg.local:5000/rv")
    monkeypatch.setenv("ROBOVAST_REGISTRY_INSECURE", "true")
    assert sd._registry_env_from_host()["ROBOVAST_REGISTRY_INSECURE"] == "true"
