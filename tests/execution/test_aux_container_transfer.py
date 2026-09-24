# Copyright (C) 2026 Frederik Pasch
#
# SPDX-License-Identifier: Apache-2.0

"""The aux container's workspace transfer: how it moves, and when it is skipped.

These tests are what keep one shape from coming back. Piping a base64 tarball into
``base64 -d | tar xzf -`` and relying on stdin EOF to end it cannot work here: the
Kubernetes stream client can write stdin but cannot half-close it, so the receiver waits
forever, the exec never returns, and ``run()`` hangs. Framing the read with ``head -c
<n>`` is what avoids it.

The transfer goes through the service's data plane instead, the way a campaign Job and
``KubeExecRunner`` stage: a ``curl | tar`` fetch and a ``tar | curl`` delivery, both
exec'd in the pod's transfer container, so no stdin in either direction and that failure
mode is excluded by construction rather than by a correct byte count. What is pinned here
is that *absence*, the empty-workspace short-circuit, and the cleanup — a per-variation
runner that leaked its tree, or its copy inside the pod, would accumulate all campaign
long.
"""

import os

import pytest

from robovast.common.errors import ExecTargetGone
from robovast.common.variation.container_runner import ContainerSpec
from robovast.execution.cluster_execution.container_runner import (TRANSFER_CONTAINER,
                                                                   AuxPodSession,
                                                                   ClusterContainerRunner,
                                                                   aux_pod_name,
                                                                   aux_slot,
                                                                   build_aux_pod_manifest)
from robovast.execution.cluster_execution.pod_access import DATA_URL_ENV, TOKEN_ENV


@pytest.fixture(name="staged")
def _staged(tmp_path):
    """The service's ``staged_dir`` / ``discard_staged`` / ``scoped_token``, over a temp root."""
    root = tmp_path / "_staged"

    class _Staged:
        def __init__(self):
            self.discarded = []

        def stage_dir(self, slot):
            return root / slot

        def discard(self, slot):
            self.discarded.append(slot)
            path = root / slot
            if not path.exists():
                return False
            import shutil
            shutil.rmtree(path)
            return True

        @staticmethod
        def token_for(scope):
            return f"tok({scope})"

    return _Staged()


def _runner(staged, pod="pod-x"):
    spec = ContainerSpec(image="example/img:1", keep_alive_command=["sleep", "infinity"])
    return ClusterContainerRunner(spec, pod, "ns", core_v1=object(),
                                  stage_dir=staged.stage_dir)


def _session(staged, campaign="c-2026-08-06-000000", **kwargs):
    return AuxPodSession(campaign, "ns", stage_dir=staged.stage_dir,
                         discard_staged=staged.discard, token_for=staged.token_for,
                         **kwargs)


def _manifest(staged, spec, **kwargs):
    return build_aux_pod_manifest("c-1", [spec], "ns", stage_dir=staged.stage_dir,
                                  token_for=staged.token_for, **kwargs)["spec"]


class _Recorder:
    """Captures what would have been exec'd, and where, instead of talking to a cluster."""

    def __init__(self):
        self.calls = []

    def __call__(self, command, stdin_data=None, progress_update_callback=None,
                 container=""):
        self.calls.append((command, stdin_data, container))
        return ""

    @property
    def scripts(self):
        return [cmd[2] for cmd, _payload, _container in self.calls if len(cmd) > 2]


# -- the transfer -------------------------------------------------------------


def test_neither_direction_uses_stdin_at_all(monkeypatch, staged):
    """Nothing is written to stdin, so there is no EOF to wait for and nothing to frame.

    Both directions are one ``curl``/``tar`` pipeline against the data plane, run in the
    transfer container.
    """
    runner = _runner(staged)
    with open(os.path.join(runner.workspace, "world.yaml"), "w", encoding="utf-8") as fh:
        fh.write("sim: {}\n")
    rec = _Recorder()
    monkeypatch.setattr(runner, "_exec", rec)

    runner._copy_in()
    runner._copy_out()

    assert [payload for _cmd, payload, _c in rec.calls] == [None, None]
    for script in rec.scripts:
        assert "curl" in script and f"${DATA_URL_ENV}" in script, script


def test_the_workspace_is_the_staged_tree_itself(staged):
    """No copy between the runner and what the pod fetches.

    The workspace lives inside the pod's slot on the service's disk, so the fetch reads
    it as it is and the delivery lands on it directly; and the pod mounts that slot at
    the same absolute path, which is what keeps the plugin's paths valid on both sides.
    """
    runner = _runner(staged)
    slot_dir = staged.stage_dir(aux_slot("pod-x"))
    assert os.path.dirname(runner.workspace) == str(slot_dir)
    assert os.path.isdir(runner.workspace)


def test_copy_in_fetches_this_workspace_from_the_pods_slot(monkeypatch, staged):
    runner = _runner(staged)
    with open(os.path.join(runner.workspace, "world.yaml"), "w", encoding="utf-8") as fh:
        fh.write("sim: {}\n")
    rec = _Recorder()
    monkeypatch.setattr(runner, "_exec", rec)

    runner._copy_in()

    (cmd, _payload, container), = rec.calls
    assert container == TRANSFER_CONTAINER, "the aux image is asked for nothing"
    script = cmd[2]
    name = os.path.basename(runner.workspace)
    assert f'"${DATA_URL_ENV}/staged/{aux_slot("pod-x")}?path={name}"' in script
    assert f"tar -x -C {runner.workspace}" in script
    assert f"chmod -R a+rwX {runner.workspace}" in script, \
        "root fetched it; the aux user has to be able to write into it"


def test_copy_in_carries_an_empty_output_directory_with_the_files(monkeypatch, staged):
    """The shape every two-step generator stages: inputs, plus an empty output directory.

    A tar carries a directory entry whether or not anything is in it, so the workspace
    travels whole and the directory the command was told to write into is there when it
    runs -- with nothing to create separately.
    """
    runner = _runner(staged)
    with open(os.path.join(runner.workspace, "hexagon.fpm"), "w", encoding="utf-8") as fh:
        fh.write("floorplan\n")
    os.makedirs(os.path.join(runner.workspace, "artifacts", "hexagon"))
    rec = _Recorder()
    monkeypatch.setattr(runner, "_exec", rec)

    runner._copy_in()

    script, = rec.scripts
    assert "curl" in script and "tar -x " in script
    assert "artifacts" not in script, "nothing is created by hand; the tar carries it"


def test_copy_out_delivers_the_workspace_under_its_own_name(monkeypatch, staged):
    """Several runners share one pod, and one pod carries one token for one slot.

    So the delivery has to keep the runner's directory name, or two workspaces delivered
    to the slot would land on top of each other -- and this one would not land where it
    was fetched from.
    """
    runner = _runner(staged)
    rec = _Recorder()
    monkeypatch.setattr(runner, "_exec", rec)

    runner._copy_out()

    (cmd, _payload, container), = rec.calls
    assert container == TRANSFER_CONTAINER
    script = cmd[2]
    parent, name = os.path.split(runner.workspace)
    assert f"tar -C {parent} -cf - {name} |" in script
    assert f'-X PUT -T - -H "Content-Type: application/x-tar" "${DATA_URL_ENV}/staged/{aux_slot("pod-x")}"' in script


def test_copy_in_of_an_empty_workspace_transfers_nothing(monkeypatch, staged):
    """A generator whose inputs all live in its image stages nothing.

    Round-tripping zero bytes is pure latency per ``run()``. The directory still has to
    exist in the pod, because the generator was handed that path.
    """
    runner = _runner(staged)
    rec = _Recorder()
    monkeypatch.setattr(runner, "_exec", rec)

    runner._copy_in()

    (command, payload, container), = rec.calls
    assert payload is None, "nothing to send, so nothing is sent"
    assert container == TRANSFER_CONTAINER
    assert "curl" not in command[2], "nothing reaches the data plane"
    assert f"mkdir -p {runner.workspace}" in command[2], "but the workspace still exists"
    assert f"chmod 0777 {runner.workspace}" in command[2], "and the aux user can write into it"


def test_a_workspace_holding_only_an_empty_dir_still_travels(monkeypatch, staged):
    """The shape ``stage_for_container`` produces for a generator with no inputs: one
    empty output directory. It is not empty, and a tar carries it, so it goes -- and the
    directory arrives on the other side because the tar says so, not because the runner
    listed it."""
    runner = _runner(staged)
    os.makedirs(os.path.join(runner.workspace, "out"))
    rec = _Recorder()
    monkeypatch.setattr(runner, "_exec", rec)

    runner._copy_in()

    script, = rec.scripts
    assert "curl" in script
    assert "mkdir -p " + os.path.join(runner.workspace, "out") not in script


def test_a_runner_without_a_stage_dir_refuses_at_construction():
    """There is no unstaged mode: a runner that could not place its workspace would run the
    command against nothing and look like a pass."""
    spec = ContainerSpec(image="example/img:1")
    with pytest.raises(TypeError, match="stage_dir"):
        ClusterContainerRunner(spec, "pod-x", "ns", core_v1=object())  # pylint: disable=missing-kwoa


# -- isolation and cleanup ----------------------------------------------------


def test_two_runners_on_one_pod_never_share_a_workspace(staged):
    """A runner is built per *variation*, so two of them sharing one aux pod would
    share a tree — and whichever closed first would delete the other's files."""
    a, b = _runner(staged), _runner(staged)
    assert a.workspace != b.workspace
    assert os.path.dirname(a.workspace) == os.path.dirname(b.workspace), \
        "but they sit in the same slot, which is the pod's"


def test_close_drops_the_local_workspace(staged):
    runner = _runner(staged)
    workspace = runner.workspace
    runner.close()
    assert not os.path.exists(workspace)


def test_close_also_drops_the_copy_inside_the_pod(monkeypatch, staged):
    """The fetched workspace is removed from the pod, not only from here.

    Each runner's workspace has a path of its own, so a copy left behind is never
    overwritten by the next runner's: it accumulates for the pod's whole life, on an
    emptyDir, which is ephemeral storage the Pod reserves none of. A composition long
    enough to fill the node has the kubelet evict the aux Pod it is composing against.
    """
    runner = _runner(staged)
    with open(os.path.join(runner.workspace, "world.yaml"), "w", encoding="utf-8") as fh:
        fh.write("sim: {}\n")
    rec = _Recorder()
    monkeypatch.setattr(runner, "_exec", rec)
    runner._copy_in()
    workspace = runner.workspace
    rec.calls.clear()

    runner.close()

    (cmd, _payload, container), = rec.calls
    assert container == TRANSFER_CONTAINER
    assert f"rm -rf {workspace}" in cmd[2]


def test_close_execs_nothing_when_nothing_was_transferred(monkeypatch, staged):
    """A runner may be built and closed without ever reaching the pod -- the query it
    was made for failed before it ran, say -- and then there is no pod to remove anything
    from, and possibly no pod at all."""
    runner = _runner(staged)
    rec = _Recorder()
    monkeypatch.setattr(runner, "_exec", rec)

    runner.close()

    assert rec.calls == []


def test_a_failing_remove_in_the_pod_does_not_fail_the_variation(monkeypatch, staged):
    """The pod being gone is one of the ways a composition ends; a teardown that raised
    over it would replace the real failure with its own."""
    runner = _runner(staged)
    with open(os.path.join(runner.workspace, "world.yaml"), "w", encoding="utf-8") as fh:
        fh.write("sim: {}\n")
    monkeypatch.setattr(runner, "_exec", _Recorder())
    runner._copy_in()

    def _gone(*_args, **_kwargs):
        raise RuntimeError("could not open an exec stream")

    monkeypatch.setattr(runner, "_exec", _gone)
    runner.close()   # must not raise


def test_the_session_sweeps_what_a_crashed_runner_left(monkeypatch, staged):
    """``close`` runs in a ``finally``, but not if the process died between them: the
    pod's whole slot goes with the pod."""
    session = _session(staged, core_v1=object())
    pod = aux_pod_name("c-2026-08-06-000000", "aux-img")
    session._created = {pod}
    session._pods = {"aux-img": pod}
    monkeypatch.setattr(session, "_client",
                        lambda: type("C", (), {
                            "delete_namespaced_pod": lambda *a, **k: None})())
    session.__exit__(None, None, None)
    assert staged.discarded == [aux_slot(pod)]


def test_a_failing_discard_does_not_fail_the_campaign(monkeypatch, staged):
    def _broken(slot):
        raise RuntimeError("disk is gone")

    session = AuxPodSession("c-1", "ns", core_v1=object(), stage_dir=staged.stage_dir,
                            discard_staged=_broken, token_for=staged.token_for)
    session._created = {"pod-x"}
    monkeypatch.setattr(session, "_client",
                        lambda: type("C", (), {
                            "delete_namespaced_pod": lambda *a, **k: None})())
    session.__exit__(None, None, None)   # must not raise


# -- a container that went away -----------------------------------------------


class _Remade:
    """A pod's owner that makes it again under its own name, counting the times."""

    def __init__(self):
        self.count = 0

    def __call__(self, spec):
        self.count += 1
        return "pod-x"


class _Vanishing:
    """Execs that report the container gone until it has been made again *times* times.

    A replacement answers to the same name -- the name is derived from the span and the
    spec, never from the attempt -- so what tells the attempts apart is the owner's count.
    """

    def __init__(self, runner, remade, times=1):
        self.runner, self.remade, self.times, self.calls = runner, remade, times, []

    def __call__(self, command, stdin_data=None, progress_update_callback=None,
                 container=""):
        self.calls.append((self.remade.count, command))
        if self.remade.count < self.times:
            raise ExecTargetGone(
                f"could not open an exec stream into {self.runner._pod}/aux: "
                "the container was not there to exec into")
        return ""

    def scripts_after(self, remakes):
        return [cmd[2] for seen, cmd in self.calls if seen == remakes and len(cmd) > 2]


def _staged_runner(staged, reprovision=None):
    runner = _runner(staged)
    runner._reprovision = reprovision
    with open(os.path.join(runner.workspace, "world.yaml"), "w", encoding="utf-8") as fh:
        fh.write("sim: {}\n")
    return runner


def test_a_vanished_container_is_made_again_and_the_command_repeated(monkeypatch, staged):
    """An eviction mid-composition costs a container, not the composition.

    A pod can end without the span that created it ending, and the exec that notices is
    the one in the middle of work that may have been running for hours.
    """
    remade = _Remade()
    runner = _staged_runner(staged, reprovision=remade)
    execs = _Vanishing(runner, remade)
    monkeypatch.setattr(runner, "_exec", execs)

    runner.run(["roqsim", "scenes", "inputs", "/config/world.yaml"])

    assert remade.count == 1
    ran = [cmd for seen, cmd in execs.calls if seen == 1 and cmd[:1] == ["roqsim"]]
    assert ran == [["roqsim", "scenes", "inputs", "/config/world.yaml"]]


def test_the_whole_transfer_is_repeated_not_just_the_exec(monkeypatch, staged):
    """A new container has an empty workspace and empty mounts, so re-running the command
    alone would run it against nothing. This is the half that is easy to leave out."""
    remade = _Remade()
    runner = _staged_runner(staged, reprovision=remade)
    runner.expose(runner.workspace, "/config")
    execs = _Vanishing(runner, remade)
    monkeypatch.setattr(runner, "_exec", execs)

    runner.run(["true"])

    on_new = execs.scripts_after(1)
    assert any("curl" in script for script in on_new), "the workspace travels again"
    assert any("/config" in script for script in on_new), "the mounts are filled again"


def test_a_second_vanishing_is_reported_rather_than_sat_out(monkeypatch, staged):
    """Once. A container that keeps going away is not something to keep waiting for, and a
    loop here would hide a cluster that cannot hold one at all."""
    remade = _Remade()
    runner = _staged_runner(staged, reprovision=remade)
    monkeypatch.setattr(runner, "_exec", _Vanishing(runner, remade, times=2))

    with pytest.raises(ExecTargetGone):
        runner.run(["true"])
    assert remade.count == 1


def test_without_a_way_to_replace_the_pod_the_failure_is_reported(monkeypatch, staged):
    """A runner whose caller does not own the pod's lifetime cannot invent one, and must
    say what happened rather than retry against the same dead name."""
    runner = _staged_runner(staged)
    monkeypatch.setattr(runner, "_exec", _Vanishing(runner, _Remade()))

    with pytest.raises(ExecTargetGone):
        runner.run(["true"])


def test_a_replacement_under_another_name_is_reported_not_used(monkeypatch, staged):
    """The workspace is staged in the pod's slot, so a pod made again under a different
    name would leave the tree behind, and the first fetch would report a slot that holds
    nothing. Both owners derive the name from the span and the spec; this pins that a
    provider which does not is refused rather than trusted."""
    remade = _Remade()
    runner = _staged_runner(staged, reprovision=lambda spec: "pod-y")
    monkeypatch.setattr(runner, "_exec", _Vanishing(runner, remade))

    with pytest.raises(RuntimeError, match="own name"):
        runner.run(["true"])


def test_a_command_that_ran_and_failed_is_never_retried(monkeypatch, staged):
    """The caller's question, answered. Repeating it would run a plugin's command twice
    for a reason that has nothing to do with the container."""
    import subprocess

    remade = _Remade()
    runner = _staged_runner(staged, reprovision=remade)
    calls = []

    def _exec(command, stdin_data=None, progress_update_callback=None, container=""):
        calls.append(command)
        if command[:1] == ["roqsim"]:
            raise subprocess.CalledProcessError(2, command, output="bad world")
        return ""

    monkeypatch.setattr(runner, "_exec", _exec)

    with pytest.raises(subprocess.CalledProcessError):
        runner.run(["roqsim"])

    assert len([c for c in calls if c[:1] == ["roqsim"]]) == 1
    assert remade.count == 0, "no pod was replaced"


def test_the_session_forgets_a_pod_it_replaces(staged):
    """The memo is what keeps a second ask from paying a create and an image pull, and
    exactly what makes it wrong once the pod it names has ended."""
    session = _session(staged, core_v1=object())
    spec = ContainerSpec(image="example/img:1")
    created = []

    def _create(_spec, pod_name):
        created.append(pod_name)
        return pod_name

    session._create_pod = _create

    first = session._pod_for(spec)
    assert session._pod_for(spec) == first and len(created) == 1, "memoised"

    again = session.replace(spec)

    assert again == first, "the name is derived from the campaign, so it is the same one"
    assert len(created) == 2, "and it was created again rather than handed out again"


def test_a_session_without_the_data_plane_wiring_is_refused_at_construction(staged):
    """A pod built without a way to stage, discard or reach its slot fails at the first
    run(), deep inside a plugin, instead of here where the cause is legible."""
    with pytest.raises(TypeError, match="token_for"):
        AuxPodSession("c-1", "ns", stage_dir=staged.stage_dir,  # pylint: disable=missing-kwoa
                      discard_staged=staged.discard)


# -- the pod manifest ---------------------------------------------------------


def test_the_transfer_container_is_the_sidecar_not_the_aux_image(staged):
    """The aux image belongs to a plugin author (``scenery_builder``); we cannot add
    tools to it, so the transfer runs in a container of ours beside it, sharing its
    mounts."""
    from robovast.common.execution import resolve_sidecar_image
    spec = ContainerSpec(image="example/img:1")
    m = _manifest(staged, spec)
    assert "initContainers" not in m
    aux, transfer = m["containers"]
    assert transfer["name"] == TRANSFER_CONTAINER
    assert transfer["image"] == resolve_sidecar_image()
    assert transfer["command"][-1].endswith("exec sleep " + str(m["activeDeadlineSeconds"]))
    assert aux["image"] == "example/img:1"
    assert {v["mountPath"] for v in aux["volumeMounts"]} == \
        {v["mountPath"] for v in transfer["volumeMounts"]}, "one set of mounts, shared"


def test_the_pod_mounts_its_slot_where_the_service_has_it(staged):
    """The workspace has one absolute path on both sides: the slot's directory."""
    spec = ContainerSpec(image="example/img:1")
    m = _manifest(staged, spec, pod_name="pod-x")
    root = str(staged.stage_dir(aux_slot("pod-x")))
    for container in m["containers"]:
        assert root in {v["mountPath"] for v in container["volumeMounts"]}
    declared = {v["name"] for v in m["volumes"]}
    assert {v["name"] for v in m["containers"][0]["volumeMounts"]} <= declared


def test_every_volume_name_is_one_kubernetes_accepts_however_deep_the_scratch_is():
    """A volume name is a DNS label, at most 63 characters, and the API server rejects the
    whole pod otherwise -- so an aux pod that cannot be created fails every variation that
    needs one. The workspace is mounted at the service's own scratch path, which in the
    deployed layout is deep under the results root; its name must not follow that path."""
    import re

    from robovast.execution.cluster_execution.service_deploy import RESULTS_DATA_DIR

    def stage_dir(slot):
        return f"{RESULTS_DATA_DIR}/_staged/{slot}"

    spec = ContainerSpec(image="example/img:1")
    m = build_aux_pod_manifest("camp-big-map-2026-09-19-141738", [spec], "ns",
                               stage_dir=stage_dir, token_for=lambda scope: "tok",
                               pod_name="robovast-exec-q73431e4cff")["spec"]
    label = re.compile(r"^[a-z0-9]([-a-z0-9]*[a-z0-9])?$")
    names = [v["name"] for v in m["volumes"]]
    names += [mount["name"] for c in m["containers"] for mount in c["volumeMounts"]]
    for name in names:
        assert len(name) <= 63 and label.match(name), name
    declared = {v["name"] for v in m["volumes"]}
    for container in m["containers"]:
        assert {mount["name"] for mount in container["volumeMounts"]} <= declared


def test_the_transfer_container_carries_the_slots_access_and_nothing_else_does(staged):
    """What a pod is given to reach the data plane: its address and a token scoped to the
    pod's own slot, on the transfer container and nowhere else."""
    spec = ContainerSpec(image="example/img:1", env={"MY_VAR": "1"})
    m = _manifest(staged, spec, pod_name="pod-x")
    aux, transfer = m["containers"]
    env = {e["name"]: e["value"] for e in transfer["env"]}
    assert set(env) == {DATA_URL_ENV, TOKEN_ENV}
    assert env[TOKEN_ENV] == staged.token_for("staged:" + aux_slot("pod-x"))
    aux_env = {e["name"]: e["value"] for e in aux["env"]}
    assert aux_env == {"MY_VAR": "1"}, "the spec's env reaches the aux container, and only it"


def test_every_shared_mount_is_made_world_writable(staged):
    """An emptyDir belongs to root, and a spec's ``run_as_user`` means the container that
    has to write into it may be nobody in particular."""
    from robovast.execution.cluster_execution.container_runner import AUX_MOUNTABLE_PATHS
    spec = ContainerSpec(image="example/img:1", run_as_user="1000:1000")
    m = _manifest(staged, spec, pod_name="pod-x")
    script = m["containers"][-1]["command"][-1]
    for path in (*AUX_MOUNTABLE_PATHS, str(staged.stage_dir(aux_slot("pod-x")))):
        assert f"chmod 0777 {path}" in script
    assert script.index("chmod") < script.index("sleep"), "before it idles"


@pytest.mark.parametrize("secret", ["", "harbor-pull"])
def test_aux_pod_pull_secret_is_set_only_when_named(secret, staged):
    """A public aux image needs no secret; a spec naming the campaign's own image does.

    ``imagePullPolicy: IfNotPresent`` hides a missing secret on any node that already cached the
    image, so this fails first on a *fresh* node -- the worst place to discover it.
    """
    spec = ContainerSpec(image="harbor.example/robovast/campaign@sha256:abc")
    m = _manifest(staged, spec, pull_secret=secret)
    if secret:
        assert m["imagePullSecrets"] == [{"name": secret}]
    else:
        assert "imagePullSecrets" not in m


# -- the exec bound (shared with KubeExecRunner) ------------------------------


def test_a_hung_helper_does_not_hang_the_campaign_forever(monkeypatch, staged):
    """``_exec`` runs on ``KubeExecRunner``'s bounded stream, so a helper that never ends
    times out, and the timeout is reported as the same failure type as any non-zero exit.
    """
    import subprocess

    runner = _runner(staged)
    monkeypatch.setattr(
        "robovast.execution.cluster_execution.kube_client.exec_stream",
        lambda *a, **k: (124, "", "", True))
    with pytest.raises(subprocess.CalledProcessError) as excinfo:
        runner._exec(["sleep", "infinity"])
    assert "exceeded" in str(excinfo.value.output)


def test_the_exec_bound_is_passed_through_not_ignored(monkeypatch, staged):
    from robovast.execution.cluster_execution.container_runner import AUX_EXEC_LIMIT_S
    seen = {}

    def fake_stream(*_a, **kwargs):
        seen.update(kwargs)
        return 0, "ok", "", False

    runner = _runner(staged)
    monkeypatch.setattr("robovast.execution.cluster_execution.kube_client.exec_stream", fake_stream)
    assert runner._exec(["true"]) == "ok"
    assert seen["limit_s"] == AUX_EXEC_LIMIT_S


def test_a_terminating_pod_is_waited_out_rather_than_adopted(monkeypatch, staged):
    """A 409 does not mean "already exists -> reuse it".

    The name is derived from the campaign id, so the pod it collides with is this
    campaign's previous one -- usually still ``Terminating``, and a Terminating pod never
    becomes Running. Adopting it means waiting out the full ready timeout for a corpse.
    """
    from kubernetes.client.rest import ApiException

    events = []

    class _Core:
        def create_namespaced_pod(self, namespace, manifest):
            events.append("create")
            if events.count("create") == 1:
                raise ApiException(status=409, reason="AlreadyExists")

        def delete_namespaced_pod(self, name, namespace, **kwargs):
            events.append("delete")

        def read_namespaced_pod(self, name, namespace):
            raise AssertionError("should go through the shared helpers")

    monkeypatch.setattr(
        "robovast.execution.cluster_execution.kube_client.wait_pod_gone",
        lambda *a, **k: events.append("wait_gone"))
    monkeypatch.setattr(
        "robovast.execution.cluster_execution.kube_client.wait_pod_ready",
        lambda *a, **k: events.append("wait_ready"))
    monkeypatch.setattr(
        "robovast.execution.cluster_execution.container_runner."
        "service_pod_owner_reference", lambda *a, **k: None)

    spec = ContainerSpec(image="example/img:1")
    session = _session(staged, "c-1", core_v1=_Core())
    # provision, not enter: the pod is made when something asks for the container
    session.provision(spec)

    assert events == ["create", "delete", "wait_gone", "create", "wait_ready"]


# -- which cluster this talks to ----------------------------------------------


def test_the_session_honours_the_service_context(monkeypatch, staged):
    """Without it, an aux pod lands in whichever cluster the *host* kubeconfig points at.

    The campaign's helper containers would then run somewhere else entirely while looking
    perfectly valid.
    """
    seen = {}
    monkeypatch.setattr("robovast.execution.cluster_execution.kube_client.load_kube_config",
                        lambda context=None: seen.update(context=context))
    monkeypatch.setattr("kubernetes.client.CoreV1Api", lambda: object())
    _session(staged, "c-1", kube_context="local")._client()
    assert seen["context"] == "local"


def test_the_runner_honours_the_service_context(monkeypatch, staged):
    seen = {}
    monkeypatch.setattr("robovast.execution.cluster_execution.kube_client.load_kube_config",
                        lambda context=None: seen.update(context=context))
    monkeypatch.setattr("kubernetes.client.CoreV1Api", lambda: object())
    spec = ContainerSpec(image="example/img:1")
    ClusterContainerRunner(spec, "pod-x", "ns", kube_context="local",
                           stage_dir=staged.stage_dir)._client()
    assert seen["context"] == "local"


def test_the_session_hands_its_context_to_the_runners_it_makes(monkeypatch, staged):
    """The factory is where the two are joined; a runner that built its own client from
    the default context would reintroduce the bug one layer down."""
    session = _session(staged, "c-1", core_v1=object(), kube_context="local")
    # The factory creates the spec's pod on the way to the runner, so the pod is already
    # accounted for here; what this measures is the runner it hands back.
    monkeypatch.setattr(session, "_pod_for", lambda spec: "pod-x")
    runner = session.runner_factory()(ContainerSpec(image="example/img:1"))
    assert runner._kube_context == "local"


@pytest.mark.parametrize("method", ["_aux_runner_context", "_scene_runner_context",
                                    "_held_aux_runners"])
def test_the_cluster_service_passes_its_own_context(method):
    """Every place a runner or its pod is made, because only one of them missing it is the bug.

    Two open an ``AuxPodSession``; the third builds a ``ClusterContainerRunner`` over a pod the
    exec manager holds, which is the same mistake one layer down — the runner would then make
    its own client from whatever context the kubeconfig points at.
    """
    import inspect

    from robovast.execution.cluster_execution.cluster_service import ClusterService
    source = inspect.getsource(getattr(ClusterService, method))
    assert "kube_context=self.kube_context" in source


def test_the_pod_declares_the_paths_a_runner_can_expose_a_tree_at(staged):
    """A Pod's mounts are fixed when it is created, long before a runner stages anything.

    So the mountable paths are declared up front, on the aux container and on the
    transfer container that fills them.
    """
    from robovast.execution.cluster_execution.container_runner import AUX_MOUNTABLE_PATHS
    spec = ContainerSpec(image="example/img:1")
    m = _manifest(staged, spec)
    for container in m["containers"]:
        mounted = {v["mountPath"] for v in container["volumeMounts"]}
        assert set(AUX_MOUNTABLE_PATHS) <= mounted
    declared = {v["name"] for v in m["volumes"]}
    assert {v["name"] for v in m["containers"][0]["volumeMounts"]} <= declared


def test_a_runner_refuses_a_path_the_pod_never_mounted():
    """Discovering it inside the tool would look like the tool's own failure."""
    runner = ClusterContainerRunner.__new__(ClusterContainerRunner)
    runner._exposed = {}
    with pytest.raises(ValueError, match="AUX_MOUNTABLE_PATHS"):
        runner.expose("/tmp/staged", "/somewhere-else")


def test_a_tree_outside_the_workspace_is_staged_into_it(tmp_path):
    """Only the workspace travels, so exposing a path elsewhere on this host cannot work.

    The copy that fills the mount runs INSIDE the container, against a source the fetch
    put there. Handed a project directory of the service's own -- which is what the
    simulator's query for a world's inputs asks for -- the container had no such path and
    the copy failed with a host path in its message. Copied in here instead, so the single
    transport holds without every caller knowing it has to.
    """
    project = tmp_path / "project"
    (project / "world").mkdir(parents=True)
    (project / "world" / "child.yaml").write_text("extends: parent.yaml\n", encoding="utf-8")

    runner = ClusterContainerRunner.__new__(ClusterContainerRunner)
    runner._exposed = {}
    runner.workspace = str(tmp_path / "ws")
    os.makedirs(runner.workspace)

    runner.expose(str(project), "/config")

    staged = runner._exposed["/config"]
    assert staged.startswith(runner.workspace + os.sep), staged
    assert os.path.isfile(os.path.join(staged, "world", "child.yaml"))


def test_the_copy_inside_the_container_names_a_path_the_fetch_carries(tmp_path):
    """The defect this closes, at the level it showed up: the script, not the bookkeeping.

    ``_place_exposed`` runs in the container, so its source has to be a path the fetch put
    there. Handed a directory of the service's own it named that host path, and the copy
    exited 1 -- taking a campaign's composition with it, with a host path as the whole
    message.
    """
    project = tmp_path / "project"
    project.mkdir()
    (project / "child.yaml").write_text("extends: parent.yaml\n", encoding="utf-8")

    runner = ClusterContainerRunner.__new__(ClusterContainerRunner)
    runner._exposed = {}
    runner.workspace = str(tmp_path / "ws")
    os.makedirs(runner.workspace)
    scripts = []
    runner._exec = lambda command, **kwargs: scripts.append(command[-1])

    runner.expose(str(project), "/config")
    runner._place_exposed()

    assert len(scripts) == 1
    source = scripts[0].split("cp -R '")[1].split("'")[0]
    assert source.startswith(runner.workspace + os.sep), source
    assert str(project) not in scripts[0]


def test_a_tree_already_in_the_workspace_is_not_copied_again(tmp_path):
    """What a variation plugin does: stage into the workspace, then expose the staged tree.

    Copying it a second time would double the bytes the transfer carries for no reason.
    """
    runner = ClusterContainerRunner.__new__(ClusterContainerRunner)
    runner._exposed = {}
    runner.workspace = str(tmp_path / "ws")
    inside = os.path.join(runner.workspace, "in", "0", "tree")
    os.makedirs(inside)

    runner.expose(inside, "/config")

    assert runner._exposed["/config"] == inside


def test_a_single_exposed_file_keeps_its_name(tmp_path):
    """A file target names the exact path a command was written for, filename included.

    Staged under a holder rather than as one, because the copy in the container is
    ``cp -R <staged> /aux/name.yaml`` -- a directory there would nest.
    """
    document = tmp_path / "overrides.yaml"
    document.write_text("a: 1\n", encoding="utf-8")

    runner = ClusterContainerRunner.__new__(ClusterContainerRunner)
    runner._exposed = {}
    runner.workspace = str(tmp_path / "ws")
    os.makedirs(runner.workspace)

    runner.expose(str(document), "/aux/overrides.yaml")

    staged = runner._exposed["/aux/overrides.yaml"]
    assert os.path.basename(staged) == "overrides.yaml"
    assert os.path.isfile(staged) and staged.startswith(runner.workspace + os.sep)


def test_an_exposed_tree_is_copied_without_preserving_attributes(monkeypatch, staged):
    """Only the content is wanted; the tree is read by the aux container, never
    re-published, and a mode copied from a private source would be one the aux user
    cannot read. The copy runs in the transfer container, which shares the mount."""
    runner = _runner(staged)
    rec = _Recorder()
    monkeypatch.setattr(runner, "_exec", rec)
    runner.expose(f"{runner.workspace}/in/0/_config", "/config")

    runner._place_exposed()

    (cmd, _payload, container), = rec.calls
    assert container == TRANSFER_CONTAINER
    script = cmd[2]
    assert "cp -a" not in script, "-a preserves attributes on the mount point and fails"
    assert f"cp -R '{runner.workspace}/in/0/_config/.' '/config/'" in script


def test_a_runner_exposes_a_single_file_inside_a_mounted_directory(monkeypatch, staged):
    """A staged FILE has to land at its exact path, filename included.

    `mount_at` names the path the command was written for -- the scene build's
    `--override /aux/roqsim_scene_overrides.yaml` -- and only the directory around it can be a
    volume, an emptyDir the Pod declares: `expose` accepts a file inside a mountable
    directory, and the copy places the file itself (`cp -R 'file/.'` would copy nothing).
    """
    runner = _runner(staged)
    rec = _Recorder()
    monkeypatch.setattr(runner, "_exec", rec)
    document = f"{runner.workspace}/in/2/overrides.yaml"
    runner.expose(document, "/aux/roqsim_scene_overrides.yaml")

    runner._place_exposed()

    (cmd, _payload, _container), = rec.calls
    script = cmd[2]
    assert f"cp -R '{document}' '/aux/roqsim_scene_overrides.yaml'" in script
    assert "/.'" not in script, "a file is not a tree; filling a mount with it copies nothing"
    assert "mkdir -p '/aux'" in script


def test_a_runner_still_refuses_a_file_outside_every_mounted_directory():
    """The allowlist is not widened to "any file": a path nobody mounted is not writable.

    `/tmp/...` is the one this actually happened with, and it must keep failing here rather
    than inside the tool -- an emptyDir over `/tmp` would shadow whatever the aux image keeps
    there, so the answer is a path of ours, not a wider rule.
    """
    runner = ClusterContainerRunner.__new__(ClusterContainerRunner)
    runner._exposed = {}
    with pytest.raises(ValueError, match="AUX_MOUNTABLE_PATHS"):
        runner.expose("/somewhere/staged.yaml", "/tmp/roqsim_scene_overrides.yaml")
    with pytest.raises(ValueError, match="AUX_MOUNTABLE_PATHS"):
        runner.expose("/somewhere/staged.yaml", "/config/nested/deeper/staged.yaml")


def test_the_scene_builds_overrides_mount_is_one_the_cluster_can_declare():
    """`scene_cache` picks the overrides path; the aux Pod declares the mountable ones.

    Whoever moves `_OVERRIDES_MOUNT` has to move it somewhere the Pod mounts.
    """
    from robovast.execution.cluster_execution.container_runner import AUX_MOUNTABLE_PATHS
    from robovast.service.scene_cache import _OVERRIDES_MOUNT
    assert os.path.dirname(_OVERRIDES_MOUNT) in AUX_MOUNTABLE_PATHS, (
        f"the scene build stages its overrides at {_OVERRIDES_MOUNT}, whose directory no aux "
        f"Pod mounts (mountable: {list(AUX_MOUNTABLE_PATHS)})"
    )
