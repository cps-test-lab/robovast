# Copyright (C) 2026 Frederik Pasch
#
# SPDX-License-Identifier: Apache-2.0

"""What a scene build says it is doing while somebody waits for it.

The panel's whole defence against a two-minute wait looking like a hang is that the stage is
*named*, so a stage that is wrong is worse than none: a pull the cluster can never finish -- a
campaign imported from another cluster, whose recorded image this registry does not serve -- looked
exactly like an ordinary compile, for the five minutes until the build's deadline ran out. What is
pinned here is therefore that every step is reported by whoever performs it, that the vocabulary is
closed at both ends, and that the reason a wait is not ending travels with it.
"""

import contextlib
import re
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

from robovast.service import scene_cache
from robovast.service.local_transport import LocalTransport

KEY = "k"


@pytest.fixture(autouse=True)
def _clean_stages():
    scene_cache._stages.clear()
    yield
    scene_cache._stages.clear()


@pytest.fixture
def stages(tmp_path, monkeypatch):
    """``generate`` with its container run faked, returning the stages it passed through.

    Only the *run* is faked: the lock, the stage reporting, the completeness check and the cache
    entry are the real ones, which is what makes the order below the order a viewer polls through.
    """
    monkeypatch.setenv("ROBOVAST_SCENE_CACHE", str(tmp_path))
    monkeypatch.setattr(scene_cache, "_generate_entry",
                        lambda identity, key, max_tex: {"shell": {"out": key}})
    seen = []

    def run(*_args, **_kwargs):
        seen.append(scene_cache.current_stage(KEY)[0])
        out = Path(scene_cache.entry_dir(KEY))
        out.mkdir(parents=True, exist_ok=True)
        (out / "scene.json").write_text("{}", encoding="utf-8")
        (out / "scene.bin").write_bytes(b"x")

    monkeypatch.setattr("robovast.common.input_generation.run_input_generators", run)
    return seen


def test_a_cluster_build_reports_the_wait_for_its_pod_apart_from_the_compile(stages):
    """Entering the runner context is where the image is pulled, and it is minutes to the compile's
    seconds -- so it cannot be reported as the compile."""
    @contextlib.contextmanager
    def runner_context():
        stages.append(scene_cache.current_stage(KEY)[0])
        yield None

    scene_cache.generate({"image": "x"}, KEY, runner_context=runner_context)

    assert stages == [scene_cache.STAGE_QUEUED, scene_cache.STAGE_COMPILING]
    assert scene_cache.current_stage(KEY) == ("", "")


def test_a_local_build_claims_no_pull_it_cannot_see(stages):
    """There is no separable pull on the local lane: ``docker run`` pulls inside the run itself, so
    naming a stage for it would be a guess a viewer cannot check."""
    scene_cache.generate({"image": "x"}, KEY)

    assert stages == [scene_cache.STAGE_COMPILING]


def test_a_failed_build_leaves_no_stage_behind(tmp_path, monkeypatch):
    """A stage outliving its build is read as a build in flight -- the status reports the two
    together -- so the panel would spin on a build that ended minutes ago."""
    monkeypatch.setenv("ROBOVAST_SCENE_CACHE", str(tmp_path))
    monkeypatch.setattr(scene_cache, "_generate_entry",
                        lambda identity, key, max_tex: {"shell": {"out": key}})
    monkeypatch.setattr("robovast.common.input_generation.run_input_generators",
                        lambda *a, **k: (_ for _ in ()).throw(RuntimeError("no such image")))

    with pytest.raises(scene_cache.SceneUnavailable):
        scene_cache.generate({"image": "x"}, KEY)

    assert scene_cache.current_stage(KEY) == ("", "")


def test_the_service_reports_the_stage_the_build_named_with_its_reason():
    scene_cache.set_stage(KEY, scene_cache.STAGE_PULLING, "ImagePullBackOff: unauthorized")

    assert LocalTransport._scene_stage(KEY) == (scene_cache.STAGE_PULLING,
                                                "ImagePullBackOff: unauthorized")


def test_a_build_that_has_not_named_a_step_yet_is_compiling():
    """The gap between taking the key's lock -- which is what makes the status say a build is in
    flight -- and the build naming its first step."""
    assert LocalTransport._scene_stage("never-started") == (scene_cache.STAGE_COMPILING, "")


def test_the_ui_names_every_stage_and_no_others():
    """Both directions on purpose. A stage with no text renders as a generic spinner, and a text
    for a stage nothing emits is a wait the panel promises to explain and never does -- which is
    how ``transferring`` and a node-side ``pulling`` sat in the table while the service only ever
    said ``compiling``.
    """
    source = (Path(__file__).parents[2]
              / "frontend/ui/src/lib/scene3d/useSceneGeometry.ts").read_text(encoding="utf-8")
    block = re.search(r"export const STAGE_TEXT[^{]*\{(.*?)\n\}", source, re.S)
    assert block, "STAGE_TEXT is not declared in useSceneGeometry.ts"
    named = set(re.findall(r"^\s*(\w+):", block.group(1), re.M))

    assert named == set(scene_cache.STAGES)


# -- the cluster's half: only the pod knows which wait it is on ---------------------------------

def _pod(phase="Pending", reason="", message=""):
    waiting = SimpleNamespace(reason=reason, message=message) if reason else None
    status = SimpleNamespace(state=SimpleNamespace(waiting=waiting))
    return SimpleNamespace(status=SimpleNamespace(
        phase=phase, init_container_statuses=None,
        container_statuses=[status] if reason else None))


@pytest.mark.parametrize("reason,expected", [
    ("", scene_cache.STAGE_QUEUED),
    ("ContainerCreating", scene_cache.STAGE_PULLING),
    ("ImagePullBackOff: Back-off pulling image", scene_cache.STAGE_PULLING),
    ("ErrImagePull: unauthorized", scene_cache.STAGE_PULLING),
    ("CreateContainerConfigError: secret not found", scene_cache.STAGE_STARTING),
])
def test_a_pending_pod_is_classified_by_what_it_is_waiting_for(reason, expected):
    """The bytes not being on the node and the container not coming up with them are different
    fixes, and the kubelet's wording is the only thing that tells them apart."""
    from robovast.execution.cluster_execution.cluster_service import _pod_wait_reporter

    seen = []
    _pod_wait_reporter(lambda stage, detail: seen.append((stage, detail)))(reason)

    assert seen == [(expected, reason)]


def test_nothing_is_read_from_the_pod_when_nobody_is_listening():
    """A screenshot render takes the same aux pod and has no viewer polling a status."""
    from robovast.execution.cluster_execution.cluster_service import _pod_wait_reporter

    assert _pod_wait_reporter(None) is None


def test_the_ready_wait_reports_the_reason_while_it_is_still_waiting(monkeypatch):
    """The reason is on the pod within seconds of the pod existing; reporting it only with the
    failure withholds it for the whole timeout, which is the wait it exists to explain."""
    from robovast.execution.cluster_execution import kube_client

    monkeypatch.setattr(time, "sleep", lambda _s: None)
    pods = iter([_pod(reason="ImagePullBackOff", message="unauthorized"),
                 _pod(reason="ImagePullBackOff", message="unauthorized"),
                 _pod(phase="Running")])
    core = SimpleNamespace(read_namespaced_pod=lambda name, namespace: next(pods))
    seen = []

    kube_client.wait_pod_ready(core, "ns", "p", timeout_s=30, on_pending=seen.append)

    assert seen == ["ImagePullBackOff: unauthorized"] * 2
