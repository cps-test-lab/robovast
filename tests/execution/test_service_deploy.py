# Copyright (C) 2026 Frederik Pasch
#
# SPDX-License-Identifier: Apache-2.0

"""Unit tests for robovast-service deployment manifest generation.

These check the manifest *shapes* without a cluster. Server-side validation
against a real API server is exercised via ``deploy_service(dry_run=True)`` in
the cluster e2e path, not here.
"""

import pytest

from robovast.execution.cluster_execution import service_deploy as sd


@pytest.fixture(autouse=True)
def _no_host_secrets(monkeypatch):
    """Keep manifest shape deterministic regardless of a CI GITHUB_TOKEN / share config."""
    for var in sd._GIT_TOKEN_HOST_ENVS:
        monkeypatch.delenv(var, raising=False)
    monkeypatch.delenv("ROBOVAST_SHARE_TYPE", raising=False)
    for var in ("ROBOVAST_NTFY_TOPIC", "ROBOVAST_NTFY_SERVER", "ROBOVAST_NTFY_TOKEN"):
        monkeypatch.delenv(var, raising=False)


def test_manifests_have_expected_kinds_and_names():
    ms = sd.service_manifests(namespace="default", image="example/robovast:test")
    kinds = [(m["kind"], m["metadata"]["name"]) for m in ms]
    assert kinds == [
        ("ServiceAccount", sd.SERVICE_ACCOUNT),
        ("Role", sd.SERVICE_ACCOUNT),
        ("RoleBinding", sd.SERVICE_ACCOUNT),
        # Cluster-scoped read for the /usage endpoint (nodes are not namespaced).
        ("ClusterRole", f"{sd.SERVICE_ACCOUNT}-usage-default"),
        ("ClusterRoleBinding", f"{sd.SERVICE_ACCOUNT}-usage-default"),
        ("Deployment", sd.SERVICE_NAME),
        ("Service", sd.SERVICE_NAME),
    ]
    # No git token → no Secret injected.
    assert not any(m["kind"] == "Secret" for m in ms)


def test_usage_cluster_role_grants_the_kubelet_proxy_read():
    """The disk meter reads each kubelet's Summary API through ``nodes/proxy``.

    Asserted on the *rules*, not just the object's existence: the same grant also happens
    to come from the controller-nodes ClusterRole, so without this the dependency could be
    dropped here and the disk meter would quietly stop working on a cluster where that
    other role was pruned.
    """
    ms = sd.service_manifests(namespace="default", image="example/robovast:test")
    role = next(m for m in ms if m["kind"] == "ClusterRole")
    grants = {(r["apiGroups"][0], res, verb)
              for r in role["rules"] for res in r["resources"] for verb in r["verbs"]}
    assert ("", "nodes/proxy", "get") in grants
    assert ("", "nodes", "list") in grants


def test_git_token_injects_secret_and_file_mount_not_env():
    ms = sd.service_manifests(namespace="default", image="x", git_token="ghp_example")
    secret = next(m for m in ms if m["kind"] == "Secret")
    assert secret["metadata"]["name"] == sd.GIT_SECRET_NAME
    assert secret["stringData"][sd.GIT_SECRET_KEY] == "ghp_example"

    dep = next(m for m in ms if m["kind"] == "Deployment")
    pod = dep["spec"]["template"]["spec"]
    container = pod["containers"][0]
    # The token is NOT exposed as an env var (would be inherited by children).
    assert not any(e["name"] == "ROBOVAST_GIT_TOKEN" for e in container["env"])
    # It is mounted read-only as a file at the path config_plugins reads.
    mount = next(m for m in container["volumeMounts"] if m["name"] == "git-credentials")
    assert mount["readOnly"] is True
    assert mount["mountPath"] == sd.GIT_TOKEN_MOUNT_DIR
    vol = next(v for v in pod["volumes"] if v["name"] == "git-credentials")
    assert vol["secret"]["secretName"] == sd.GIT_SECRET_NAME
    # Mount dir + secret key must equal the file config_plugins reads.
    from robovast.common.config_plugins import GIT_TOKEN_FILE
    assert f"{sd.GIT_TOKEN_MOUNT_DIR}/{sd.GIT_SECRET_KEY}" == GIT_TOKEN_FILE


def test_git_token_read_from_host_env(monkeypatch):
    monkeypatch.setenv("GITHUB_TOKEN", "ghp_from_env")
    ms = sd.service_manifests(namespace="default", image="x")
    secret = next(m for m in ms if m["kind"] == "Secret")
    assert secret["stringData"][sd.GIT_SECRET_KEY] == "ghp_from_env"


def test_share_env_injects_secret_and_envfrom():
    # An explicit share_env is materialised into a Secret and pulled in via envFrom
    # (env vars — the in-driver upload reads them from os.environ).
    share_env = {"ROBOVAST_SHARE_TYPE": "gcs", "ROBOVAST_GCS_BUCKET": "b",
                 "ROBOVAST_GCS_KEY_JSON": "{}"}
    ms = sd.service_manifests(namespace="default", image="x", share_env=share_env)
    secret = next(m for m in ms if m["kind"] == "Secret")
    assert secret["metadata"]["name"] == sd.SHARE_SECRET_NAME
    assert secret["stringData"] == share_env

    dep = next(m for m in ms if m["kind"] == "Deployment")
    container = dep["spec"]["template"]["spec"]["containers"][0]
    assert container["envFrom"] == [{"secretRef": {"name": sd.SHARE_SECRET_NAME}}]


def test_share_env_read_from_host_provider(monkeypatch):
    # ROBOVAST_SHARE_TYPE + provider vars on the host → resolved via build_pod_env.
    monkeypatch.setenv("ROBOVAST_SHARE_TYPE", "webdav")
    monkeypatch.setenv("ROBOVAST_WEBDAV_URL", "https://dav.example/col")
    monkeypatch.setenv("ROBOVAST_WEBDAV_USER", "u")
    monkeypatch.setenv("ROBOVAST_WEBDAV_PASSWORD", "p")
    ms = sd.service_manifests(namespace="default", image="x")
    secret = next(m for m in ms if m["kind"] == "Secret")
    assert secret["metadata"]["name"] == sd.SHARE_SECRET_NAME
    data = secret["stringData"]
    assert data["ROBOVAST_SHARE_TYPE"] == "webdav"
    assert data["ROBOVAST_WEBDAV_PASSWORD"] == "p"


def test_unknown_share_type_fails_fast(monkeypatch):
    import click
    monkeypatch.setenv("ROBOVAST_SHARE_TYPE", "nope-not-a-provider")
    with pytest.raises(click.UsageError):
        sd.service_manifests(namespace="default", image="x")


def test_no_share_means_no_secret_or_envfrom():
    ms = sd.service_manifests(namespace="default", image="x")  # env cleared by fixture
    assert not any(m["kind"] == "Secret" for m in ms)
    dep = next(m for m in ms if m["kind"] == "Deployment")
    assert "envFrom" not in dep["spec"]["template"]["spec"]["containers"][0]


def test_ntfy_env_injects_secret_and_envfrom(monkeypatch):
    # ROBOVAST_NTFY_TOPIC (+ optional token) on the host → a ntfy-credentials Secret
    # pulled in via envFrom, so the in-service Notifier.from_env picks it up.
    monkeypatch.setenv("ROBOVAST_NTFY_TOPIC", "robovast-alice")
    monkeypatch.setenv("ROBOVAST_NTFY_TOKEN", "tk_xxx")
    ms = sd.service_manifests(namespace="default", image="x")
    secret = next(m for m in ms if m["kind"] == "Secret")
    assert secret["metadata"]["name"] == sd.NTFY_SECRET_NAME
    assert secret["stringData"] == {
        "ROBOVAST_NTFY_TOPIC": "robovast-alice", "ROBOVAST_NTFY_TOKEN": "tk_xxx"}

    dep = next(m for m in ms if m["kind"] == "Deployment")
    container = dep["spec"]["template"]["spec"]["containers"][0]
    assert container["envFrom"] == [{"secretRef": {"name": sd.NTFY_SECRET_NAME}}]


def test_no_ntfy_topic_means_no_secret_or_envfrom(monkeypatch):
    # Only the optional server set (no topic) → notifications disabled, no Secret.
    monkeypatch.setenv("ROBOVAST_NTFY_SERVER", "https://ntfy.sh")
    ms = sd.service_manifests(namespace="default", image="x")
    assert not any(m["kind"] == "Secret" for m in ms)
    dep = next(m for m in ms if m["kind"] == "Deployment")
    assert "envFrom" not in dep["spec"]["template"]["spec"]["containers"][0]


def test_share_and_ntfy_both_configured_carry_both_secretrefs(monkeypatch):
    monkeypatch.setenv("ROBOVAST_NTFY_TOPIC", "robovast-alice")
    share_env = {"ROBOVAST_SHARE_TYPE": "gcs", "ROBOVAST_GCS_BUCKET": "b"}
    ms = sd.service_manifests(namespace="default", image="x", share_env=share_env)
    secret_names = {m["metadata"]["name"] for m in ms if m["kind"] == "Secret"}
    assert secret_names == {sd.SHARE_SECRET_NAME, sd.NTFY_SECRET_NAME}

    dep = next(m for m in ms if m["kind"] == "Deployment")
    container = dep["spec"]["template"]["spec"]["containers"][0]
    assert container["envFrom"] == [
        {"secretRef": {"name": sd.SHARE_SECRET_NAME}},
        {"secretRef": {"name": sd.NTFY_SECRET_NAME}},
    ]


def test_no_git_token_means_no_volume_or_mount():
    """The registry's own volume is always there; the git one must not be.

    Checked by name rather than by counting: the pod gained a second container and its
    storage volume when the registry moved in, so "the pod has no volumes" stopped being
    the way to say "no git token was configured". The service container then gained an
    always-present mount of its own (the workspace store), which retired the last
    count-based form of this check — "no mounts at all" — for the same reason.
    """
    ms = sd.service_manifests(namespace="default", image="x")  # env cleared by fixture
    dep = next(m for m in ms if m["kind"] == "Deployment")
    pod = dep["spec"]["template"]["spec"]
    assert "git-credentials" not in [v["name"] for v in pod.get("volumes", [])]
    service_container = pod["containers"][0]
    assert "git-credentials" not in [
        m["name"] for m in service_container.get("volumeMounts", [])]


def test_workspace_store_is_mounted_so_an_upgrade_does_not_discard_it():
    """The workspace store must be on a volume, not the container's writable layer.

    Regression: it was on the writable layer, and since every upgrade restarts the pod
    (see ``RESTART_ANNOTATION``), one ``vast exec cluster upgrade`` deleted every pushed
    project while reporting success. Campaign results live in the object store and were
    untouched, which is what made it easy to miss.

    The env var is asserted alongside the mount because the mount alone fixes nothing:
    the store's default location comes from ``HOME`` inside the container, so the two
    have to agree for the mount to be covering the directory actually written to.
    """
    ms = sd.service_manifests(namespace="default", image="x")
    dep = next(m for m in ms if m["kind"] == "Deployment")
    pod = dep["spec"]["template"]["spec"]
    service_container = pod["containers"][0]

    volume = next(v for v in pod["volumes"] if v["name"] == sd.WORKSPACES_VOLUME_NAME)
    assert "emptyDir" not in volume
    assert volume["hostPath"]["path"] == sd.DEFAULT_WORKSPACES_HOST_PATH

    mount = next(m for m in service_container["volumeMounts"]
                 if m["name"] == sd.WORKSPACES_VOLUME_NAME)
    assert mount["mountPath"] == sd.WORKSPACES_DATA_DIR
    assert {"name": sd.WORKSPACES_ROOT_ENV,
            "value": sd.WORKSPACES_DATA_DIR} in service_container["env"]


def test_workspace_store_honours_an_explicitly_configured_root():
    """An explicit ``ROBOVAST_WORKSPACES_ROOT`` in the env wins over the default.

    Otherwise the deployer would append a second entry for the same variable, and which
    one takes effect is decided by Kubernetes rather than by the caller.
    """
    env = [{"name": sd.WORKSPACES_ROOT_ENV, "value": "/somewhere/else"}]
    dep = sd._deployment_manifest(  # pylint: disable=protected-access
        namespace="default", image="x", env=env, restarted_at="FIXED")
    container = dep["spec"]["template"]["spec"]["containers"][0]
    roots = [e for e in container["env"] if e["name"] == sd.WORKSPACES_ROOT_ENV]
    assert roots == [{"name": sd.WORKSPACES_ROOT_ENV, "value": "/somewhere/else"}]


def test_deployment_runs_vast_serve_on_service_port():
    ms = sd.service_manifests(namespace="ns1", image="example/robovast:test")
    dep = next(m for m in ms if m["kind"] == "Deployment")
    container = dep["spec"]["template"]["spec"]["containers"][0]
    assert container["image"] == "example/robovast:test"
    assert container["command"] == ["vast", "serve", "--host", "0.0.0.0",
                                    "--port", str(sd.SERVICE_PORT)]
    assert container["ports"][0]["containerPort"] == sd.SERVICE_PORT
    assert container["readinessProbe"]["httpGet"]["path"] == "/healthz"
    # binds to the service account that can launch controllers
    assert dep["spec"]["template"]["spec"]["serviceAccountName"] == sd.SERVICE_ACCOUNT
    # namespace threaded through every object
    assert all(m["metadata"].get("namespace", "ns1") == "ns1"
               for m in ms if m["kind"] != "ClusterRole")


def test_deploy_context_stamped_into_service_env():
    # Per-cluster resource lists are keyed by kubeconfig context name; in-cluster
    # there is no kubeconfig, so deploy records the context for the in-pod driver.
    ms = sd.service_manifests(namespace="default", image="x", config_name="rke2",
                              kube_context="gcp-c4")
    dep = next(m for m in ms if m["kind"] == "Deployment")
    env = {e["name"]: e["value"] for e in
           dep["spec"]["template"]["spec"]["containers"][0]["env"]}
    assert env["ROBOVAST_KUBE_CONTEXT"] == "gcp-c4"


def test_no_context_stamped_when_deploy_uses_active_context():
    ms = sd.service_manifests(namespace="default", image="x", config_name="rke2")
    dep = next(m for m in ms if m["kind"] == "Deployment")
    names = {e["name"] for e in
             dep["spec"]["template"]["spec"]["containers"][0]["env"]}
    assert "ROBOVAST_KUBE_CONTEXT" not in names


def test_service_is_clusterip_selecting_the_deployment():
    ms = sd.service_manifests(namespace="default", image="x")
    svc = next(m for m in ms if m["kind"] == "Service")
    assert svc["spec"]["type"] == "ClusterIP"
    assert svc["spec"]["selector"] == {"app": sd.SERVICE_NAME}
    assert svc["spec"]["ports"][0]["port"] == sd.SERVICE_PORT


def test_service_rbac_can_manage_jobs_pods_and_exec():
    ms = sd.service_manifests(namespace="default", image="x")
    role = next(m for m in ms if m["kind"] == "Role")
    resources = {r for rule in role["rules"] for r in rule["resources"]}
    # The service drives campaigns in-process now (no controller pod), so it needs
    # everything that pod's ServiceAccount used to hold: it creates/monitors the
    # scenario + postprocessing Jobs, their pods/logs, and the per-campaign aux pods
    # it execs into.
    assert {"jobs", "jobs/status"} <= resources
    assert {"pods", "pods/log", "pods/exec"} <= resources


def test_default_image_resolves_from_controller_image():
    # No explicit image → falls back to resolve_controller_image()
    from robovast.common.execution import resolve_controller_image
    ms = sd.service_manifests(namespace="default")
    dep = next(m for m in ms if m["kind"] == "Deployment")
    assert dep["spec"]["template"]["spec"]["containers"][0]["image"] == \
        resolve_controller_image()


# -- the service's own image pull -------------------------------------------
#
# The service hands the registry pull Secret to every campaign pod it creates, which is
# exactly why its OWN pod spec was easy to forget: a cluster whose controller image sits
# in a private registry got a service that could pull images for everyone but itself.
# Setup printed "completed successfully" and the pod sat in ImagePullBackOff.


def _pod_spec(manifests):
    dep = next(m for m in manifests if m["kind"] == "Deployment")
    return dep["spec"]["template"]["spec"]


def test_the_service_pod_references_the_registry_pull_secret():
    ms = sd.service_manifests(namespace="ns1", image="private.example/robovast:dev",
                              pull_secret=sd.REGISTRY_PUSH_SECRET_NAME)
    assert _pod_spec(ms)["imagePullSecrets"] == [{"name": sd.REGISTRY_PUSH_SECRET_NAME}]


def test_no_pull_secret_means_no_reference():
    """A public image needs none, and referencing a Secret that does not exist would
    make every deployment log a spurious pull warning."""
    ms = sd.service_manifests(namespace="ns1", image="public.example/robovast:dev")
    assert "imagePullSecrets" not in _pod_spec(ms)


def test_creating_the_registry_secret_also_wires_it_into_the_deployment(monkeypatch):
    """When setup creates the Secret in this same run, the Deployment must reference it
    without needing a cluster lookup -- the two are built from one manifest list."""
    monkeypatch.setenv("ROBOVAST_REGISTRY_SERVER", "private.example")
    monkeypatch.setenv("ROBOVAST_REGISTRY_USERNAME", "robot")
    monkeypatch.setenv("ROBOVAST_REGISTRY_PASSWORD", "secret")
    ms = sd.service_manifests(namespace="ns1", image="private.example/robovast:dev")
    assert any(m["kind"] == "Secret"
               and m["metadata"]["name"] == sd.REGISTRY_PUSH_SECRET_NAME for m in ms)
    assert _pod_spec(ms)["imagePullSecrets"] == [{"name": sd.REGISTRY_PUSH_SECRET_NAME}]


def test_service_rbac_can_write_the_postprocessing_configmap():
    """Postprocessing ships its scripts into the Job as a ConfigMap it creates.

    The grant used to name secrets and configmaps in one read-only rule, which reads as
    deliberate ("read-only, by name") and was right for the Secret. So every cluster
    campaign RAN and then failed postprocessing with a 403 -- after the compute was
    spent, which is the expensive place to discover a missing verb.
    """
    ms = sd.service_manifests(namespace="default", image="x")
    role = next(m for m in ms if m["kind"] == "Role")
    verbs = {v for rule in role["rules"] if "configmaps" in rule["resources"]
             for v in rule["verbs"]}
    # create + replace + delete are the three postprocess_job.py actually calls.
    assert {"create", "get", "update", "delete"} <= verbs

    secret_verbs = {v for rule in role["rules"] if "secrets" in rule["resources"]
                    for v in rule["verbs"]}
    assert secret_verbs == {"get"}, "widening configmaps must not widen secrets"


# The family variables are applied with a strategic-merge patch, whose merge key for
# `containers[].env` is the variable NAME -- so a variable the patch omits is preserved, not
# removed. While these were emitted only when set, an operator who once set
# ROBOVAST_PROJECT_TAG could never unset it: deleting it from ./.env left it out of the next
# patch and the stale value kept resolving the family. A deployment spent an afternoon pulling
# images at a tag that appeared in no file on the machine.


def test_family_env_is_carried_even_when_unset(monkeypatch):
    # Empty, not absent: an absent entry is what the merge patch preserves.
    monkeypatch.delenv("ROBOVAST_PROJECT", raising=False)
    monkeypatch.delenv("ROBOVAST_PROJECT_TAG", raising=False)
    env = {e["name"]: e["value"] for e in
           _pod_spec(sd.service_manifests(namespace="default", image="x"))["containers"][0]["env"]}
    assert env["ROBOVAST_PROJECT"] == ""
    assert env["ROBOVAST_PROJECT_TAG"] == ""


def test_family_env_carries_what_the_environment_says(monkeypatch):
    monkeypatch.setenv("ROBOVAST_PROJECT", "ghcr.io/example-org")
    monkeypatch.setenv("ROBOVAST_PROJECT_TAG", "2026-08-20")
    env = {e["name"]: e["value"] for e in
           _pod_spec(sd.service_manifests(namespace="default", image="x"))["containers"][0]["env"]}
    assert env["ROBOVAST_PROJECT"] == "ghcr.io/example-org"
    assert env["ROBOVAST_PROJECT_TAG"] == "2026-08-20"


def test_the_pod_carries_the_setup_hosts_timezone(monkeypatch):
    # Campaign ids are minted from datetime.now() in this pod, so without TZ every
    # campaign directory is named in UTC (see _host_timezone).
    monkeypatch.setattr(sd, "_host_timezone", lambda: "Europe/Berlin")
    env = {e["name"]: e["value"] for e in
           _pod_spec(sd.service_manifests(namespace="default", image="x"))["containers"][0]["env"]}
    assert env["TZ"] == "Europe/Berlin"


def test_an_undeterminable_timezone_is_carried_as_empty_not_omitted(monkeypatch):
    # "" is UTC to libc; an omitted entry is what the merge patch preserves, so a host
    # that cannot name its zone must still be able to reset a pod back to UTC.
    monkeypatch.setattr(sd, "_host_timezone", lambda: "")
    env = {e["name"]: e["value"] for e in
           _pod_spec(sd.service_manifests(namespace="default", image="x"))["containers"][0]["env"]}
    assert env["TZ"] == ""


def test_host_timezone_prefers_the_localtime_symlink(monkeypatch, tmp_path):
    etc = tmp_path / "etc"
    zone = tmp_path / "usr/share/zoneinfo/Europe/Berlin"
    zone.parent.mkdir(parents=True)
    zone.write_bytes(b"")
    etc.mkdir()
    (etc / "localtime").symlink_to(zone)
    (etc / "timezone").write_text("Etc/UTC\n")
    monkeypatch.setattr(sd, "_TZ_PATHS", (etc / "localtime", etc / "timezone"))
    assert sd._host_timezone() == "Europe/Berlin"


def test_host_timezone_falls_back_to_etc_timezone(monkeypatch, tmp_path):
    # /etc/localtime is a copy, not a link — Debian's /etc/timezone is all that is left.
    etc = tmp_path / "etc"
    etc.mkdir()
    (etc / "localtime").write_bytes(b"")
    (etc / "timezone").write_text("Europe/Berlin\n")
    monkeypatch.setattr(sd, "_TZ_PATHS", (etc / "localtime", etc / "timezone"))
    assert sd._host_timezone() == "Europe/Berlin"


def test_an_unresolvable_zone_name_is_utc_rather_than_a_broken_tz(monkeypatch, tmp_path):
    # libc would silently fall back to UTC for a name the pod cannot resolve, leaving a
    # configured-looking TZ producing the very UTC names it was meant to replace.
    etc = tmp_path / "etc"
    etc.mkdir()
    (etc / "timezone").write_text("Mars/Olympus_Mons\n")
    monkeypatch.setattr(sd, "_TZ_PATHS", (etc / "localtime", etc / "timezone"))
    assert sd._host_timezone() == ""


def test_an_explicit_env_still_wins(monkeypatch):
    # setup composes its own env; this must not overwrite a value decided upstream.
    monkeypatch.setenv("ROBOVAST_PROJECT", "from-the-shell")
    given = [{"name": "ROBOVAST_PROJECT", "value": "from-the-caller"}]
    env = {e["name"]: e["value"] for e in
           _pod_spec(sd.service_manifests(namespace="default", image="x",
                                          env=given))["containers"][0]["env"]}
    assert env["ROBOVAST_PROJECT"] == "from-the-caller"
