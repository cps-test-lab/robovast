# Copyright (C) 2026 Frederik Pasch
# SPDX-License-Identifier: Apache-2.0
"""The shared build daemon: the store it keeps, and the build client that dials it.

Most of what is asserted here fails *silently* if it is wrong -- a daemon whose store is not
really persistent still builds, still passes its probes, and is indistinguishable from this
whole component not existing except that the base image keeps being pulled. So the properties
are pinned rather than left to a review that cannot see them either.
"""

import pytest

from robovast.execution.cluster_execution.buildkitd_deploy import (
    BUILDKITD_NAME, BUILDKITD_PORT, BUILDKITD_STORE_DIR, buildkitd_address,
    buildkitd_deployment_manifest, buildkitd_node_selector, buildkitd_pvc_manifest,
    buildkitd_service_manifest, buildkitd_toml, buildkitd_volume)


def _dep(**kw):
    kw.setdefault("namespace", "ns")
    return buildkitd_deployment_manifest(**kw)


def _container(dep):
    return dep["spec"]["template"]["spec"]["containers"][0]


# ---------------------------------------------------------------------------
# The store — the one that fails invisibly
# ---------------------------------------------------------------------------

def test_the_store_is_mounted_where_the_config_says_it_is():
    """The single most likely way this ships broken.

    The rootless image keeps its store under ``$HOME/.local/share/buildkit`` and the rootful
    one under ``/var/lib/buildkit``. Mount one and configure the other and the daemon writes to
    the container's writable layer: volume mounted, pod healthy, builds fine, nothing kept. The
    only symptom is that base images keep being pulled -- which looks exactly like this
    component not existing.
    """
    dep = _dep()
    mounts = {m["name"]: m["mountPath"] for m in _container(dep)["volumeMounts"]}
    assert mounts["buildkit-store"] == BUILDKITD_STORE_DIR
    assert f'root = "{BUILDKITD_STORE_DIR}"' in buildkitd_toml()


def test_the_store_is_never_an_emptydir():
    """An emptyDir store is precisely what the per-build daemon already had."""
    host = buildkitd_volume("/data/robovast-buildkit", "")
    claim = buildkitd_volume("/data/robovast-buildkit", "premium-rwo")
    assert host["hostPath"]["path"] == "/data/robovast-buildkit"
    assert claim["persistentVolumeClaim"]["claimName"] == BUILDKITD_NAME
    assert "emptyDir" not in host and "emptyDir" not in claim


def test_a_claim_is_only_rendered_when_there_is_a_storage_class():
    """Stock RKE2 ships none, and a PVC there stays Pending forever."""
    assert buildkitd_pvc_manifest("ns", "") is None
    pvc = buildkitd_pvc_manifest("ns", "premium-rwo", "200Gi")
    assert pvc["spec"]["storageClassName"] == "premium-rwo"
    assert pvc["spec"]["accessModes"] == ["ReadWriteOnce"]
    assert pvc["spec"]["resources"]["requests"]["storage"] == "200Gi"


def test_one_replica_recreated_never_rolled():
    """ReadWriteOnce cannot be mounted twice, so a rolling update would deadlock: the new pod
    waits for a volume the old pod will not release until the new one is ready."""
    dep = _dep()
    assert dep["spec"]["replicas"] == 1
    assert dep["spec"]["strategy"] == {"type": "Recreate"}


# ---------------------------------------------------------------------------
# Bounding what persists
# ---------------------------------------------------------------------------

def test_gc_is_configured_with_the_keys_this_buildkit_understands():
    """Persistence without a ceiling fills a disk the daemon shares with other things.

    The key names are version-coupled -- the older ``gckeepstorage`` was replaced by this
    triple -- which is half the reason BUILDKIT_IMAGE is pinned. If the pin moves, re-read the
    schema before trusting this.
    """
    toml = buildkitd_toml()
    assert "gc = true" in toml
    assert "[[worker.oci.gcpolicy]]" in toml
    for key in ("reservedSpace", "maxUsedSpace", "minFreeSpace"):
        assert key in toml
    assert "gckeepstorage" not in toml, "that key is gone in the pinned BuildKit"


def test_the_default_budget_cannot_overrun_a_disk_it_did_not_choose():
    """The hostPath default lands on a node disk of unknown size, shared with other things.

    A fixed ceiling larger than that disk is not a ceiling: the store grows until the *node*
    runs out, and the kubelet answers DiskPressure by evicting pods. The daemon is pinned to one
    node and the service pod may be pinned to the same one, so the failure is the API going down
    for a reason with no visible connection to a build. A percentage cannot be wrong that way.
    """
    from robovast.execution.cluster_execution.buildkitd_deploy import (
        DEFAULT_BUILDKITD_GC_MAX_USED, DEFAULT_BUILDKITD_GC_MIN_FREE)

    assert DEFAULT_BUILDKITD_GC_MAX_USED.endswith("%"), (
        "an absolute default ceiling is only safe on a disk we sized ourselves")
    assert DEFAULT_BUILDKITD_GC_MIN_FREE, "the backstop for a disk already full of other things"


def test_the_generated_config_is_valid_toml():
    """It is assembled as text, so nothing else would catch a malformed one until the daemon
    refused to start -- which surfaces as a CrashLoopBackOff with no obvious cause."""
    import tomllib

    parsed = tomllib.loads(buildkitd_toml(registry_host="registry.example.com"))
    assert parsed["root"] == BUILDKITD_STORE_DIR
    assert parsed["worker"]["oci"]["gc"] is True
    assert parsed["worker"]["oci"]["gcpolicy"][0]["reservedSpace"]
    assert parsed["registry"]["registry.example.com"]["ca"]


def test_the_daemon_reserves_something_and_caps_itself():
    """Requests are subtracted from Kueue's campaign quota, so they must be deliberate --
    and asking for nothing is not free: BestEffort QoS is evicted first, i.e. mid-build."""
    res = _container(_dep())["resources"]
    assert res["requests"]["cpu"] and res["requests"]["memory"]
    assert res["limits"]["cpu"] and res["limits"]["memory"]


def test_parallelism_is_bounded():
    """Unset, it defaults to the node's CPU count -- capacity Kueue promised to campaigns."""
    dep = _dep()
    rendered = " ".join(_container(dep).get("args") or _container(dep)["command"])
    assert "--oci-max-parallelism" in rendered


# ---------------------------------------------------------------------------
# Placement and reachability
# ---------------------------------------------------------------------------

def test_the_daemon_is_pinnable_because_a_hostpath_store_is_node_local():
    assert buildkitd_node_selector("") is None
    assert buildkitd_node_selector("node-a") == {"kubernetes.io/hostname": "node-a"}
    assert _dep(node_name="node-a")["spec"]["template"]["spec"]["nodeSelector"] == {
        "kubernetes.io/hostname": "node-a"}


def test_clients_dial_a_service_name_not_a_pod():
    """The pod is replaced by every upgrade and by any reschedule; a build submitted just
    before that must not be holding an address that has stopped existing."""
    addr = buildkitd_address("robovast")
    assert addr == f"tcp://{BUILDKITD_NAME}.robovast.svc:{BUILDKITD_PORT}"
    svc = buildkitd_service_manifest("robovast")
    assert svc["spec"]["selector"] == {"app": BUILDKITD_NAME}
    assert svc["spec"]["ports"][0]["port"] == BUILDKITD_PORT
    # No type: ClusterIP is the default and the endpoint is unauthenticated — publishing it
    # would hand arbitrary in-cluster builds to anyone who found it.
    assert "type" not in svc["spec"]


def test_the_daemon_keeps_the_privileges_the_client_gave_up():
    """rootlesskit's mount namespace is what needs these, and only the daemon creates one."""
    sec = _container(_dep())["securityContext"]
    assert sec["seccompProfile"] == {"type": "Unconfined"}
    assert sec["appArmorProfile"] == {"type": "Unconfined"}
    assert sec["runAsUser"] == 1000


def test_overriding_the_entrypoint_does_not_drop_rootlesskit():
    """Setting `command` replaces the image's entrypoint, which is ["rootlesskit","buildkitd"].

    Rootless BuildKit cannot run outside the user namespace rootlesskit creates, so an override
    that forgets it produces a daemon that dies at startup -- and only on deployments that have
    a private registry CA, which is the branch nobody exercises locally.
    """
    plain = _container(_dep())
    assert "command" not in plain, "with no override the image's own entrypoint must be used"
    assert plain["args"][0] == "--addr"

    with_ca = _container(_dep(ca_configmap_name="robovast-registry-ca"))
    assert "rootlesskit buildkitd" in with_ca["command"][-1]


def test_a_private_registry_ca_is_configured_on_the_daemon():
    """The daemon resolves, pulls and pushes, so it makes the TLS connection to the registry."""
    toml = buildkitd_toml(registry_host="registry.example.com")
    assert '[registry."registry.example.com"]' in toml
    assert "ca = " in toml
    dep = _dep(ca_configmap_name="robovast-registry-ca")
    assert any(v.get("configMap", {}).get("name") == "robovast-registry-ca"
               for v in dep["spec"]["template"]["spec"]["volumes"])


# ---------------------------------------------------------------------------
# The build Job, now a client
# ---------------------------------------------------------------------------

def _job(**over):
    from robovast.execution.cluster_execution.cluster_image_build import build_job_manifest
    kwargs = {"build_id": "imgbuild-x-abc", "image_ref": "reg.local:5000/x:abc",
              "campaign_label": "imgbuild-x-abc", "init_env": [],
              "push_secret_name": "push", "namespace": "ns",
              "daemon_addr": "tcp://robovast-buildkitd.ns.svc:1234"}
    kwargs.update(over)
    return build_job_manifest(**kwargs)


def _job_container(job):
    return job["spec"]["template"]["spec"]["containers"][0]


def test_the_build_job_dials_the_daemon_instead_of_spawning_one():
    cmd = _job_container(_job())["command"][-1]
    assert "buildctl --addr tcp://robovast-buildkitd.ns.svc:1234 build" in cmd
    assert "buildctl-daemonless.sh" not in cmd


def test_the_context_is_still_the_jobs_own_directory():
    """`--local` resolves client-side and streams over the session, which is what lets the
    staged context, the init container and the whole Job survive this change untouched."""
    job = _job()
    cmd = _job_container(job)["command"][-1]
    assert "--local context=/context" in cmd
    init = job["spec"]["template"]["spec"]["initContainers"][0]
    assert init["name"] == "context-fetch"
    assert any(v["name"] == "context" and "emptyDir" in v
               for v in job["spec"]["template"]["spec"]["volumes"])


def test_the_client_drops_the_privileges_it_no_longer_needs():
    """All of these existed for rootlesskit's mount namespace. A client creates none."""
    job = _job()
    container = _job_container(job)
    assert container["securityContext"] == {"runAsUser": 1000, "runAsGroup": 1000}
    assert "BUILDKITD_FLAGS" not in {e["name"] for e in container.get("env") or []}
    annotations = job["spec"]["template"]["metadata"].get("annotations", {})
    assert not any("apparmor" in k for k in annotations)


def test_the_client_still_names_its_container_buildkit():
    """`_build_log_text` and the pull-failure hints both select on this name; renaming it
    makes them stop matching without anything failing."""
    assert _job_container(_job())["name"] == "buildkit"


def test_the_job_cannot_wait_forever_on_a_daemon_that_never_answers():
    """`ttlSecondsAfterFinished` only starts once a Job is terminal, and with backoffLimit 0
    a client wedged against an unreachable daemon leaves both counters at zero -- so without a
    deadline the Job stays active forever and is never collected. The build could not hang on
    anything outside itself before this change."""
    assert _job()["spec"]["activeDeadlineSeconds"] > 0


@pytest.mark.parametrize("log", [
    "failed to solve: failed to dial gRPC: connection refused",
    'error: transport: Error while dialing dial tcp: i/o timeout',
    "context deadline exceeded",
])
def test_an_unreachable_daemon_is_infra_not_the_projects_fault(log):
    """Unclassified this fell through to advice pointing at the project's `build:` section,
    sending whoever hit it to edit a `.vast` over a cluster fault."""
    from robovast.service.image_build import classify_build_error

    err = classify_build_error(log)
    assert err.phase == "builder"
    assert err.fixable_by == "infra"


def test_a_real_package_failure_is_still_the_projects_fault():
    """The transport matcher runs first and is deliberately broad, so it has to be shown not
    to swallow the failures the classifier exists for."""
    from robovast.service.image_build import classify_build_error

    err = classify_build_error("E: Unable to locate package ros-jazzy-nope\n")
    assert err.phase == "apt"
    assert err.fixable_by == "agent"

