# Copyright (C) 2026 Frederik Pasch
# SPDX-License-Identifier: Apache-2.0
"""RoboVAST's own short-lived pods declare what they reserve and prefer the build node.

A pod that requests nothing is invisible to the scheduler's accounting and to admission's,
so a campaign that filled its node leaves it no cycles; and the per-node reserve is held on
the build node only, so these pods have to prefer it to find that room.
"""

import pathlib

from robovast.common.variation.container_runner import ContainerSpec
from robovast.execution.cluster_execution.cluster_image_build import build_job_manifest
from robovast.execution.cluster_execution.container_runner import (TRANSFER_CONTAINER,
                                                                   build_aux_pod_manifest)
from robovast.execution.cluster_execution.kube_exec_runner import _pod_manifest
from robovast.execution.cluster_execution.node_placement import (BUILD_CLIENT_RESOURCES,
                                                                 BUILD_NODE_LABEL,
                                                                 TRANSIENT_POD_RESOURCES)
from robovast.service import container_exec as ce


def _prefers_the_build_node(pod_spec):
    terms = pod_spec["affinity"]["nodeAffinity"][
        "preferredDuringSchedulingIgnoredDuringExecution"]
    assert "requiredDuringSchedulingIgnoredDuringExecution" not in \
        pod_spec["affinity"]["nodeAffinity"], "a full build node must not keep it from running"
    return any(e["key"] == BUILD_NODE_LABEL
               for t in terms for e in t["preference"]["matchExpressions"])


def test_an_aux_pod_reserves_what_its_queries_run_in(monkeypatch):
    monkeypatch.setenv("ROBOVAST_PROJECT", "registry.example.com/robovast")
    monkeypatch.setenv("ROBOVAST_PROJECT_TAG", "2026-08-28")
    manifest = build_aux_pod_manifest(
        "aux-pod", [ContainerSpec(image="alpine:latest")], "default",
        stage_dir=lambda slot: pathlib.Path("/results/_staged") / slot,
        token_for=lambda scope: "tok")
    spec = manifest["spec"]
    queried = [c for c in spec["containers"] if c["name"] != TRANSFER_CONTAINER]
    assert queried and all(c["resources"] == TRANSIENT_POD_RESOURCES for c in queried)
    assert _prefers_the_build_node(spec)


def test_an_exec_session_reserves_what_its_commands_run_in(tmp_path):
    spec = ce.ExecSpec(image="alpine:latest", command="", config_dir=str(tmp_path),
                       env={}, config_name="c1")
    pod = _pod_manifest(spec, 300, "ns", None, "tok")["spec"]
    assert pod["containers"][0]["resources"] == TRANSIENT_POD_RESOURCES
    assert _prefers_the_build_node(pod)


def test_an_image_build_client_reserves_little_and_prefers_the_build_node():
    manifest = build_job_manifest(
        build_id="imgbuild-x-abc", image_ref="reg.local:5000/x:abc",
        campaign_label="imgbuild-x-abc", token="tok", push_secret_name="push",
        namespace="ns", daemon_addr="tcp://robovast-buildkitd.ns.svc:1234")
    pod = manifest["spec"]["template"]["spec"]
    assert pod["containers"][0]["resources"] == BUILD_CLIENT_RESOURCES
    assert _prefers_the_build_node(pod)
