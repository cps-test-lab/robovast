# Copyright (C) 2026 Frederik Pasch
# SPDX-License-Identifier: Apache-2.0
"""Publishing the service refuses the two configurations that would expose it.

Both mistakes are invisible once made and both are made by *omission*, which is why
they are refusals in code rather than warnings in documentation:

* an Ingress with no access token publishes an unauthenticated RoboVAST — and a campaign
  names its own container image, so reaching it is enough to run containers in the
  cluster;
* an Ingress over plain HTTP sends the shared token in clear text, and the session
  cookie is ``Secure``, so the login would not work at all.
"""

import pytest

from robovast.execution.cluster_execution.service_deploy import (SERVICE_NAME,
                                                                 SERVICE_PORT,
                                                                 IngressRefused,
                                                                 service_manifests)


def _kinds(manifests):
    return [m["kind"] for m in manifests]


def _ingress(**kwargs):
    kwargs.setdefault("auth_token", "tok")
    kwargs.setdefault("ingress_host", "robovast.example.org")
    manifests = service_manifests(**kwargs)
    return next(m for m in manifests if m["kind"] == "Ingress")


def test_no_host_means_no_ingress():
    """Unchanged default: a cluster nobody asked to publish stays ClusterIP-only."""
    assert "Ingress" not in _kinds(service_manifests(auth_token="tok"))


def test_an_ingress_without_a_token_is_refused():
    with pytest.raises(IngressRefused) as excinfo:
        service_manifests(ingress_host="robovast.example.org", issuer="letsencrypt")
    message = str(excinfo.value)
    assert "no access token" in message
    # The reason has to name the actual stake, not just "insecure".
    assert "container image" in message


def test_plain_http_is_refused_unless_forced():
    with pytest.raises(IngressRefused) as excinfo:
        service_manifests(auth_token="tok", ingress_host="robovast.example.org")
    assert "clear text" in str(excinfo.value)


def test_plain_http_is_allowed_when_explicitly_accepted():
    ingress = _ingress(insecure_http=True)
    assert "tls" not in ingress["spec"]


def test_a_cert_manager_issuer_is_annotated_and_names_a_tls_secret():
    ingress = _ingress(issuer="robovast-ca")
    assert ingress["metadata"]["annotations"]["cert-manager.io/cluster-issuer"] == "robovast-ca"
    assert ingress["spec"]["tls"][0]["hosts"] == ["robovast.example.org"]
    assert ingress["spec"]["tls"][0]["secretName"] == f"{SERVICE_NAME}-tls"


def test_an_existing_tls_secret_is_used_as_given():
    ingress = _ingress(tls_secret="my-cert")
    assert ingress["spec"]["tls"][0]["secretName"] == "my-cert"
    assert "annotations" not in ingress["metadata"]


def test_the_ingress_routes_everything_to_the_service():
    """One backend: the SPA, the REST API and /mcp are all on the same port."""
    ingress = _ingress(issuer="ca", ingress_class="nginx")
    rule = ingress["spec"]["rules"][0]
    assert rule["host"] == "robovast.example.org"
    path = rule["http"]["paths"][0]
    assert (path["path"], path["pathType"]) == ("/", "Prefix")
    assert path["backend"]["service"]["name"] == SERVICE_NAME
    assert path["backend"]["service"]["port"]["number"] == SERVICE_PORT
    assert ingress["spec"]["ingressClassName"] == "nginx"
