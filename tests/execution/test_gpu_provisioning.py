# Copyright (C) 2025 Frederik Pasch
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
# http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing,
# software distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions
# and limitations under the License.
#
# SPDX-License-Identifier: Apache-2.0

"""Provisioning GPU scheduling -- and, above all, not disturbing a cluster without GPUs.

The governing requirement is asymmetric: a GPU that is present must be used with no flags
and no config, and a cluster without one must behave *exactly* as it did before. So most of
what is pinned here is absence -- no helm call, no error, no manifest change.
"""

import pytest

from robovast.execution.cluster_execution import kubernetes_gpu as kg


@pytest.fixture
def helm(monkeypatch):
    """Record helm invocations instead of running them."""
    calls = []

    def _run_helm(args, check=True):
        calls.append(list(args))
        return True, ""

    monkeypatch.setattr(kg, "_run_helm", _run_helm)
    monkeypatch.setattr(kg.time, "sleep", lambda _s: None)
    return calls


def _cluster(monkeypatch, *, gpus=0, runtime_class=False, ours=False, deployed=None):
    monkeypatch.setattr(kg, "get_cluster_allocatable_gpus", lambda **_k: gpus)
    monkeypatch.setattr(kg, "nvidia_runtime_class_present", lambda **_k: runtime_class)
    monkeypatch.setattr(kg, "helm_release_exists", lambda *_a, **_k: ours)
    monkeypatch.setattr(kg, "_deployed_replicas", lambda **_k: deployed)


# -- a cluster with no GPU: the ordinary case --------------------------------------


def test_no_gpu_installs_nothing_and_does_not_fail(monkeypatch, helm, caplog):
    """The invariant the whole change rests on. Not "degrades gracefully" -- untouched."""
    _cluster(monkeypatch)
    assert kg.ensure_nvidia_device_plugin() is None
    assert helm == [], "a CPU-only cluster must not have helm run against it"


def test_no_gpu_is_an_error_only_when_gpus_were_demanded(monkeypatch, helm):
    """Implicit provisioning is opportunistic; an explicit --gpu-replicas is a requirement.
    Silently handing software rendering to someone who asked for a GPU is the failure this
    distinction exists to prevent."""
    _cluster(monkeypatch)
    with pytest.raises(RuntimeError) as excinfo:
        kg.ensure_nvidia_device_plugin(gpu_replicas=24)
    assert "--no-gpu" in str(excinfo.value), "the error must name the way out"
    assert helm == []


def test_no_gpu_flag_skips_everything(monkeypatch, helm):
    _cluster(monkeypatch, runtime_class=True)
    assert kg.ensure_nvidia_device_plugin(skip=True) is None
    assert helm == []


# -- installing ---------------------------------------------------------------------


def _values_of(call):
    return next(a.split("=", 1)[1] for a in call if a.startswith("--values="))


def test_a_toolkit_host_gets_the_plugin_at_the_default_replica_count(monkeypatch, helm,
                                                                    tmp_path):
    written = {}

    real_format = kg.format_plugin_values

    def _capture(replicas):
        written["replicas"] = replicas
        return real_format(replicas)

    monkeypatch.setattr(kg, "format_plugin_values", _capture)
    _cluster(monkeypatch, runtime_class=True)
    monkeypatch.setattr(kg, "_wait_for_gpu_capacity",
                        lambda expected, **_k: expected)

    assert kg.ensure_nvidia_device_plugin() == kg.DEFAULT_GPU_REPLICAS
    assert written["replicas"] == kg.DEFAULT_GPU_REPLICAS
    assert helm and helm[0][:2] == ["upgrade", "--install"], helm
    # --repo rather than `helm repo add`: setup runs on an operator's own machine and must
    # not mutate their global helm config.
    assert any(a.startswith("--repo=") for a in helm[0])
    assert f"--version={kg.NVIDIA_PLUGIN_VERSION}" in helm[0], "the chart must be pinned"


def test_the_values_are_valid_yaml():
    """A template rendered through str.format can produce something that is not YAML at all
    -- `affinity: {}` had to be written `{{}}` or format() read it as a positional field.
    Parsing is the cheap check that catches that class of mistake."""
    import yaml

    parsed = yaml.safe_load(kg.format_plugin_values(16))
    assert parsed["runtimeClassName"] == "nvidia"
    inner = yaml.safe_load(parsed["config"]["map"]["default"])
    assert inner["sharing"]["timeSlicing"]["resources"][0]["replicas"] == 16


def test_the_values_pin_the_things_a_wrong_default_would_break():
    values = kg.format_plugin_values(16)
    # Needs the driver to read NVML, and nvidia is not RKE2's default runtime.
    assert "runtimeClassName: nvidia" in values
    # Under renameByDefault the node advertises nvidia.com/gpu.shared and a queue covering
    # nvidia.com/gpu would never admit anything -- a permanent hang with no error.
    assert "renameByDefault: false" in values
    assert "replicas: 16" in values
    # A device advertiser must run wherever the GPUs are, including on a tainted node pool.
    assert "operator: Exists" in values
    # The chart's default affinity requires a Node Feature Discovery label. Without NFD no
    # node has one, so the DaemonSet is created with DESIRED 0 while helm reports "deployed"
    # and nothing advertises a GPU -- observed twice on the real cluster.
    #
    # Asserted as a NON-EMPTY term, which is the whole subtlety: the chart wraps the value in
    # a Helm `with`, so `affinity: {}` reads as absent and the default it was meant to replace
    # comes back. This test exists because that failure is completely silent.
    import yaml as _yaml

    terms = (_yaml.safe_load(values)["affinity"]["nodeAffinity"]
             ["requiredDuringSchedulingIgnoredDuringExecution"]["nodeSelectorTerms"])
    assert terms, "an empty affinity is ignored by the chart -- it must be permissive, not absent"
    keys = {expr["key"] for term in terms for expr in term["matchExpressions"]}
    assert not any(k.startswith("feature.node.kubernetes.io/") for k in keys), (
        "the NFD label requirement must be replaced, not inherited")


def test_an_explicit_replica_count_is_used(monkeypatch, helm):
    _cluster(monkeypatch, runtime_class=True)
    monkeypatch.setattr(kg, "_wait_for_gpu_capacity", lambda expected, **_k: expected)
    assert kg.ensure_nvidia_device_plugin(gpu_replicas=24) == 24


def test_oversubscribing_warns_that_vram_is_not_partitioned(monkeypatch, helm, caplog):
    import logging

    _cluster(monkeypatch, runtime_class=True)
    monkeypatch.setattr(kg, "_wait_for_gpu_capacity", lambda expected, **_k: expected)
    with caplog.at_level(logging.WARNING, logger=kg.__name__):
        kg.ensure_nvidia_device_plugin(gpu_replicas=kg.DEFAULT_GPU_REPLICAS + 8)
    assert "VRAM" in caplog.text


def test_a_bare_rerun_preserves_a_deliberate_replica_count(monkeypatch, helm):
    """A plain `setup --force` must not quietly undo `--gpu-replicas 24`. Same courtesy
    setup already extends to the access token."""
    _cluster(monkeypatch, gpus=24, runtime_class=True, ours=True, deployed=24)
    monkeypatch.setattr(kg, "_wait_for_gpu_capacity", lambda expected, **_k: expected)
    assert kg.ensure_nvidia_device_plugin() == 24


# -- someone else's device plugin ---------------------------------------------------


def test_a_foreign_advertiser_is_left_alone(monkeypatch, helm):
    """A managed GPU node pool or the NVIDIA GPU Operator already advertises the resource
    and has no `nvidia` RuntimeClass. Installing a second advertiser would silently
    override whatever time-slicing it chose."""
    _cluster(monkeypatch, gpus=8, runtime_class=False, ours=False)
    assert kg.ensure_nvidia_device_plugin() == 8
    assert helm == []


def test_disagreeing_with_a_foreign_advertiser_is_an_error(monkeypatch, helm):
    _cluster(monkeypatch, gpus=8, ours=False)
    with pytest.raises(RuntimeError) as excinfo:
        kg.ensure_nvidia_device_plugin(gpu_replicas=24)
    assert "does not manage" in str(excinfo.value)
    assert helm == []


# -- the capacity wait --------------------------------------------------------------


def test_the_wait_demands_the_exact_count(monkeypatch):
    """Not "more than zero". Changing time-slicing restarts the DaemonSet, so capacity goes
    24 -> absent -> 16; a check that accepts any non-zero reading can see the old value and
    size capacity from it."""
    monkeypatch.setattr(kg.time, "sleep", lambda _s: None)
    readings = iter([24, 24, 0, 16])
    monkeypatch.setattr(kg, "get_cluster_allocatable_gpus",
                        lambda **_k: next(readings, 16))
    assert kg._wait_for_gpu_capacity(16, timeout=30) == 24  # 24 >= 16 satisfies it


def test_capacity_that_never_appears_raises_with_a_diagnosis(monkeypatch):
    monkeypatch.setattr(kg.time, "sleep", lambda _s: None)
    monkeypatch.setattr(kg, "get_cluster_allocatable_gpus", lambda **_k: 0)
    with pytest.raises(RuntimeError) as excinfo:
        kg._wait_for_gpu_capacity(16, timeout=0)
    message = str(excinfo.value)
    assert "logs" in message, "must say how to look"
    assert "--no-gpu" in message


def test_a_plugin_that_never_advertises_is_survivable_when_unasked(monkeypatch, helm,
                                                                  caplog):
    """The opportunistic path must not fail setup even when the install goes wrong: the
    cluster is then simply a CPU-only cluster, which is a state that works."""
    _cluster(monkeypatch, runtime_class=True)

    def _never(expected, **_kw):
        raise RuntimeError("the cluster advertises 0 nvidia.com/gpu")

    monkeypatch.setattr(kg, "_wait_for_gpu_capacity", _never)
    assert kg.ensure_nvidia_device_plugin() is None


def test_uninstall_tolerates_a_cluster_that_never_had_it(monkeypatch):
    calls = []
    monkeypatch.setattr(kg, "_run_helm",
                        lambda args, check=True: (calls.append(args), (False, "release: not found"))[1])
    kg.uninstall_nvidia_device_plugin()  # must not raise
    assert calls


# -- "absent" versus "cannot tell" --------------------------------------------------
#
# These collapsed into one answer once, and the result was invisible: a service account
# without runtimeclasses access got a 403, the check reported "no such RuntimeClass", and
# every GPU pod silently lost runtimeClassName -- device attached, quota charged, rendering
# in software. Found only by inspecting a live pod.


def test_a_present_runtime_class_is_named(monkeypatch):
    monkeypatch.setattr(kg, "nvidia_runtime_class_present", lambda **_k: True)
    assert kg.gpu_runtime_class_for() == kg.NVIDIA_RUNTIME_CLASS


def test_a_genuinely_absent_runtime_class_is_omitted(monkeypatch):
    """A managed GPU node pool advertises the resource with no such RuntimeClass, and naming
    one that does not exist makes the API server reject the pod outright."""
    monkeypatch.setattr(kg, "nvidia_runtime_class_present", lambda **_k: False)
    assert kg.gpu_runtime_class_for() is None


def test_an_unreadable_runtime_class_names_it_anyway_and_warns(monkeypatch, caplog):
    """Of the two ways to be wrong, this is the one that announces itself: a wrong name fails
    the pod immediately, while omitting it produces a slow success nobody questions."""
    import logging

    monkeypatch.setattr(kg, "nvidia_runtime_class_present", lambda **_k: None)
    with caplog.at_level(logging.WARNING, logger=kg.__name__):
        assert kg.gpu_runtime_class_for() == kg.NVIDIA_RUNTIME_CLASS
    assert "runtimeclasses" in caplog.text, "the warning must name the missing permission"


def test_the_probe_reports_unknown_rather_than_absent_on_an_error(monkeypatch):
    class _Broken:
        def list_runtime_class(self):
            raise RuntimeError("runtimeclasses is forbidden")

    monkeypatch.setattr(kg.client, "NodeV1Api", lambda: _Broken())
    monkeypatch.setattr(kg, "load_kube_config", lambda **_k: None, raising=False)
    import robovast.execution.cluster_execution.kube_client as kc
    monkeypatch.setattr(kc, "load_kube_config", lambda **_k: None)
    assert kg.nvidia_runtime_class_present() is None
