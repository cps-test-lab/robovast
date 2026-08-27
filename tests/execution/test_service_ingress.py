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

from robovast.execution.cluster_execution.service_deploy import (PUBLIC_URL_ENV, SERVICE_NAME,
                                                                 SERVICE_PORT, IngressRefused,
                                                                 public_url, service_manifests)


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
    # No issuer, so cert-manager must not be asked to manage this cert -- it would try to
    # replace a certificate the operator supplied. Checked by key rather than by "no
    # annotations at all", since the registry's upload limits live here too.
    assert "cert-manager.io/cluster-issuer" not in ingress["metadata"]["annotations"]


def test_the_ingress_routes_everything_to_the_service():
    """One backend for the app: the SPA, the REST API and /mcp share a port."""
    ingress = _ingress(issuer="ca", ingress_class="nginx")
    rule = ingress["spec"]["rules"][0]
    assert rule["host"] == "robovast.example.org"
    path = next(p for p in rule["http"]["paths"] if p["path"] == "/")
    assert path["pathType"] == "Prefix"
    assert path["backend"]["service"]["name"] == SERVICE_NAME
    assert path["backend"]["service"]["port"]["number"] == SERVICE_PORT
    assert ingress["spec"]["ingressClassName"] == "nginx"


def test_the_ingress_raises_nginx_upload_limits_for_the_registry():
    """Reachable is not the same as usable.

    ingress-nginx caps a request body at 1m by default, and an image layer is far larger,
    so a push died on a 413 from the proxy -- after the build had been paid for, and
    naming nothing RoboVAST owns. Found by pushing to the deployed registry, not by any
    test, which is why it is pinned here.
    """
    from robovast.execution.cluster_execution import registry_deploy

    ingress = _ingress(issuer="ca", ingress_class="nginx")
    annotations = ingress["metadata"]["annotations"]
    for key, value in registry_deploy.REGISTRY_INGRESS_ANNOTATIONS.items():
        assert annotations[key] == value
    assert annotations["nginx.ingress.kubernetes.io/proxy-body-size"] == "0"
    # The issuer annotation must survive alongside them, or cert-manager stops renewing.
    assert annotations["cert-manager.io/cluster-issuer"] == "ca"


def test_the_registry_is_published_ahead_of_the_catch_all():
    """``/v2`` must win over ``/``, which matches everything.

    The service mounts its SPA at ``/``, so if the registry rule came second a
    ``docker pull`` would be answered with the web UI. Order is the whole guarantee
    here -- both rules are Prefix, and nginx resolves by specificity, but relying on
    that silently is what this pins.
    """
    from robovast.execution.cluster_execution import registry_deploy

    paths = _ingress(issuer="ca", ingress_class="nginx")["spec"]["rules"][0]["http"]["paths"]
    assert [p["path"] for p in paths] == [registry_deploy.REGISTRY_INGRESS_PATH, "/"]
    registry = paths[0]
    assert registry["backend"]["service"]["name"] == SERVICE_NAME
    assert registry["backend"]["service"]["port"]["number"] == registry_deploy.REGISTRY_PORT


def test_deploy_service_forwards_the_ingress_options(monkeypatch):
    """Regression: the flags reached service_manifests but not deploy_service.

    `service_manifests` was tested directly, so the gap was invisible until
    `vast cluster setup --ingress-host` was run against a real cluster and died on
    an unexpected-keyword TypeError before it reached the API server.
    """
    import inspect

    from robovast.execution.cluster_execution import service_deploy

    accepted = inspect.signature(service_deploy.deploy_service).parameters
    for option in ("ingress_host", "ingress_class", "tls_secret", "issuer",
                   "insecure_http"):
        assert option in accepted, f"deploy_service drops {option}"


def test_an_ingress_is_applied_not_just_built(monkeypatch):
    """The manifest existing is not the same as the cluster being told about it."""
    import inspect

    from robovast.execution.cluster_execution import service_deploy

    source = inspect.getsource(service_deploy.deploy_service)
    assert "create_namespaced_ingress" in source, (
        "deploy_service builds an Ingress manifest but never applies it")


def test_the_refusal_happens_before_anything_is_installed(monkeypatch):
    """A pure argument error must not cost a half-set-up cluster.

    The check used to live only inside the manifest builder, which runs after Kueue is
    installed and the flavor's storage deployed — so `--ingress-host` without TLS
    modified the cluster and *then* refused.
    """
    from unittest import mock

    from robovast.execution.cluster_execution import cluster_setup, service_deploy

    installed = []
    monkeypatch.setattr(cluster_setup, "install_kueue_helm",
                        lambda *a, **k: installed.append("kueue"))
    monkeypatch.setattr(service_deploy, "read_service_config_from_cluster",
                        lambda *a, **k: (None, None))
    monkeypatch.setattr(cluster_setup, "get_cluster_config", mock.Mock())

    with pytest.raises(IngressRefused):
        cluster_setup.setup_server(
            config_name="rke2",
            service_kwargs={"ingress_host": "robovast.example.org"})

    assert installed == [], "the cluster was modified before the arguments were checked"


def test_a_gce_ingress_gets_container_native_load_balancing():
    """GKE's built-in controller cannot route to a plain ClusterIP.

    Without the NEG annotation the Ingress is created, never becomes healthy, and the
    reason surfaces in the load balancer rather than anywhere a RoboVAST user looks.
    """
    manifests = service_manifests(auth_token="tok",
                                  ingress_host="robovast.example.org",
                                  ingress_class="gce", issuer="ca")
    service = next(m for m in manifests if m["kind"] == "Service")
    assert service["metadata"]["annotations"]["cloud.google.com/neg"] == '{"ingress": true}'


def test_nginx_needs_no_such_annotation():
    """The tested path stays exactly as it was."""
    manifests = service_manifests(auth_token="tok",
                                  ingress_host="robovast.example.org",
                                  ingress_class="nginx", issuer="ca")
    service = next(m for m in manifests if m["kind"] == "Service")
    assert "annotations" not in service["metadata"]


# -- the published origin, carried to the service that must report it -------


def _service_env(**kwargs):
    kwargs.setdefault("auth_token", "tok")
    # An https Ingress is refused without one of these (see above); irrelevant to the env.
    if kwargs.get("ingress_host") and not kwargs.get("insecure_http"):
        kwargs.setdefault("tls_secret", "robovast-tls")
    deployment = next(m for m in service_manifests(**kwargs) if m["kind"] == "Deployment")
    container = deployment["spec"]["template"]["spec"]["containers"][0]
    return {e["name"]: e["value"] for e in container["env"] if "value" in e}


def test_the_published_origin_reaches_the_pod():
    """The service cannot work this out for itself.

    It is given no RBAC to read its own Ingress -- deliberately -- so the origin it reports
    to clients has to be baked in at setup, exactly like the build registry's prefix.
    """
    env = _service_env(ingress_host="robovast.example.org")
    assert env[PUBLIC_URL_ENV] == "https://robovast.example.org"


def test_plain_http_is_declared_as_plain_http():
    env = _service_env(ingress_host="robovast.example.org", insecure_http=True)
    assert env[PUBLIC_URL_ENV] == "http://robovast.example.org"


def test_a_stated_origin_is_written_whatever_the_ingress_arguments_say():
    """How an ``upgrade`` sets it: it holds no TLS arguments, so it reads the whole origin
    off the live Ingress and states it. Deriving one from the host it passes as
    ``registry_host`` would publish https:// over a plain-HTTP deployment."""
    env = _service_env(registry_host="robovast.example.org",
                       public_origin="http://robovast.example.org")
    assert env[PUBLIC_URL_ENV] == "http://robovast.example.org"


def test_a_stated_empty_origin_clears_it():
    """Stating ``""`` is not the same as saying nothing. An upgrade reads the live Ingress,
    so it can tell "not published" from "I do not know" -- and a service that has just been
    unpublished must stop advertising an origin rather than keep a dead one."""
    env = _service_env(public_origin="")
    assert env[PUBLIC_URL_ENV] == ""


def test_a_call_that_cannot_know_the_origin_leaves_it_alone():
    """Omitted, not emitted empty, and that distinction is the point.

    A merge patch preserves what it does not mention. The calls that arrive without
    ``--ingress-host`` are exactly the ones that must not overwrite the origin: ``upgrade``
    and a ``setup`` re-run recover the published *host* from the live Ingress but hold none
    of the TLS arguments it was created with, so they know the host and not the scheme.
    Emitting "" would erase a still-correct origin on every upgrade; rendering
    ``https://<host>`` from the host alone would publish the wrong scheme for a deployment
    set up with ``--insecure-http``.
    """
    assert PUBLIC_URL_ENV not in _service_env()


def test_the_host_alone_is_never_enough_to_render_an_origin():
    """The regression this exists for: an upgrade passes the published host as
    ``registry_host`` -- never as ``ingress_host``, which would try to recreate the Ingress
    and be refused for want of TLS arguments. Rendering the origin from the host it does
    pass looked equivalent and was not: the scheme would be a guess. Silence here is what
    forces the caller to state one.
    """
    assert PUBLIC_URL_ENV not in _service_env(registry_host="robovast.example.org")


def test_a_caller_supplied_origin_is_not_overwritten():
    """Same rule as every other env var here: what setup composed upstream wins."""
    env = _service_env(ingress_host="robovast.example.org",
                       env=[{"name": PUBLIC_URL_ENV, "value": "https://decided.example.org"}])
    assert env[PUBLIC_URL_ENV] == "https://decided.example.org"


def test_public_url_follows_the_scheme_the_ingress_was_validated_for():
    assert public_url("h.example.org") == "https://h.example.org"
    assert public_url("h.example.org", insecure_http=True) == "http://h.example.org"
    assert public_url("") == ""
    assert public_url("", insecure_http=True) == ""
