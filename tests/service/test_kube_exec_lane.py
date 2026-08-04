# Copyright (C) 2026 Frederik Pasch
# SPDX-License-Identifier: Apache-2.0
"""Regression tests for the in-cluster exec lane.

Each of these pins a bug found by running the lane against a live cluster, all three of
which were invisible to the local lane:

- the kube context was ignored, so exec talked to whichever cluster the *kubeconfig*
  pointed at rather than the one the service runs campaigns on;
- ``stop_held`` returned while the pod was still ``Terminating``, so the next start hit
  ``AlreadyExists``;
- the "is anything running?" probe counted its own helper processes, so a pod was never
  idle and never idle-reaped.

No cluster is needed here: these check the manifests, the argv and the call sequence.
"""

import pytest

from robovast.service import container_exec as ce
from robovast.service.kube_exec_lane import KubeExecLane, _config_payload


def _spec(tmp_path, command="ls", image="img:1"):
    config = tmp_path / "config"
    config.mkdir()
    (config / "entrypoint.sh").write_text("#!/bin/bash\n")
    (config / "scenario.config").write_text("{}\n")
    nested = config / "files"
    nested.mkdir()
    (nested / "node.py").write_text("print(1)\n")
    return ce.ExecSpec(image=image, command=command, config_dir=str(config),
                       env={"OUTPUT_DIR": ce.OUTPUT_DIR}, config_name="c1")


def test_the_service_kube_context_is_honoured(monkeypatch):
    """Without this, exec runs against the kubeconfig's current context.

    That is not a small inconvenience: the answer would come from a different cluster
    than the campaigns run on, while looking perfectly valid.
    """
    seen = {}

    def fake_load(context=None):
        seen["context"] = context

    monkeypatch.setattr("robovast.common.kube.load_kube_config", fake_load)
    monkeypatch.setattr("kubernetes.client.CoreV1Api", lambda: object())
    KubeExecLane("ns", kube_context="local")._client()
    assert seen["context"] == "local"


def test_the_cluster_service_passes_its_own_context():
    import inspect
    from robovast.service.cluster_service import ClusterService
    source = inspect.getsource(ClusterService._exec_lane)
    assert "kube_context=self.kube_context" in source


def test_stopping_waits_for_the_objects_to_actually_be_gone():
    """A Kubernetes delete returns while the pod is still terminating.

    ``stop_held`` must offer the local lane's contract — ``docker rm -f`` is synchronous —
    or the single-container rule breaks: the next start collides with the corpse.
    """
    import inspect
    source = inspect.getsource(KubeExecLane.stop_held)
    assert "_wait_gone" in source
    wait = inspect.getsource(KubeExecLane._wait_gone)
    assert "read_namespaced_pod" in wait and "read_namespaced_config_map" in wait
    assert "404" in wait, "absence is how it knows deletion finished"


def test_the_process_probe_spawns_nothing_of_its_own():
    """The probe must not count its own helpers.

    The first version piped ``ls`` into ``wc`` and saw four processes in an *idle* pod,
    so ``held_workload_running`` was permanently true and nothing was ever idle-reaped.
    Shell builtins only, and PID 1 / ``$$`` / ``$PPID`` excluded, so idle reads 0.
    """
    probe = KubeExecLane._PROCESS_COUNT_SH
    for spawned in ("ls ", "wc", "ps ", "pgrep", "awk", "grep"):
        assert spawned not in probe, f"the probe spawns {spawned!r} and would count it"
    assert '[ "$pid" = 1 ]' in probe
    assert '[ "$pid" = "$$" ]' in probe
    assert '[ "$pid" = "$PPID" ]' in probe


def test_the_probe_threshold_treats_zero_as_idle():
    import inspect
    assert "count > 0" in inspect.getsource(KubeExecLane.held_workload_running)


# -- staging into a ConfigMap ------------------------------------------------


def test_nested_config_paths_survive_the_configmap(tmp_path):
    # ConfigMap keys cannot contain '/', so a run file at files/node.py is flattened and
    # restored by the pod's init step. Losing it would break any .osc referencing it.
    payload = _config_payload(str(_spec(tmp_path).config_dir))
    assert "files__node.py" in payload
    assert payload["files__node.py"] == "print(1)\n"
    assert "entrypoint.sh" in payload


def test_an_oversized_config_is_refused_with_a_reason(tmp_path):
    # Better than the API rejecting it with a message that names neither the limit nor
    # what to do instead.
    config = tmp_path / "big"
    config.mkdir()
    (config / "huge.bin").write_text("x" * (1024 * 1024))
    with pytest.raises(ValueError, match="ConfigMap"):
        _config_payload(str(config))


def test_the_pod_carries_the_managers_deadline_and_an_idle_pid_one(tmp_path):
    from robovast.service.kube_exec_lane import _pod_manifest
    manifest = _pod_manifest(_spec(tmp_path), 930, "ns", None)
    spec = manifest["spec"]
    # The manager's own deadline, so the pod cannot outlive its intent even if the
    # reaper never runs — and not a hardcoded 300, which would truncate a long scenario.
    assert spec["activeDeadlineSeconds"] == 930
    assert spec["containers"][0]["command"] == ["/bin/bash", "-c", "exec sleep 930"]
    assert spec["initContainers"][0]["name"] == "restore-config"
    assert spec["restartPolicy"] == "Never"


def test_the_pod_mounts_no_results_volume(tmp_path):
    from robovast.service.kube_exec_lane import _pod_manifest
    manifest = _pod_manifest(_spec(tmp_path), 300, "ns", None)
    mounts = [m["mountPath"] for c in manifest["spec"]["containers"]
              for m in c.get("volumeMounts", [])]
    assert "/config" in mounts
    assert "/out" not in mounts, "a diagnostic must never mount the results dir"
