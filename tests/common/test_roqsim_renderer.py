# Copyright (C) 2026 Frederik Pasch
# SPDX-License-Identifier: Apache-2.0
"""A roqsim campaign names the renderer its cameras draw through, and the job gets what it needs.

``renderer: {name, gpu}`` is a renderer drawing in roqsim's process (a Gaussian-splat renderer
on CUDA): the world's ``sim.renderer`` and a GPU on the container running the simulator.
``renderer: {name, service}`` is a renderer in a container of its own, reached over roqsim's socket
renderer at an address on the job's shared ``/ipc``: the container, and the address in the world.
"""

from importlib.metadata import entry_points

import pytest

from robovast.common.containers import plan_containers
from robovast.common.simulators import SIMULATOR_GROUP, apply_backend

pytestmark = pytest.mark.skipif(
    "roqsim" not in {ep.name for ep in entry_points().select(group=SIMULATOR_GROUP)},
    reason="the 'roqsim' extra is not installed (pip install 'robovast[roqsim]')")


def _execution(mode="base", renderer=None, **sim):
    block = {"backend": "roqsim", "config": "world.yaml", **sim}
    if renderer is not None:
        block["renderer"] = renderer
    return {"mode": mode, "runs": 1, "containers": {"simulation": block}}


def _document(execution):
    from robovast.common.simulators import resolve_backend, _validated_cfg

    backend = resolve_backend("roqsim")
    block = execution["containers"]["simulation"]
    return backend.sim_document(_validated_cfg(backend, block, "roqsim"), execution)


def test_an_in_process_renderer_is_the_world_s_renderer_and_gets_its_gpu_stepped():
    ex = _execution("base", {"name": "splat", "gpu": 1})
    applied = apply_backend(dict(ex))
    plan = plan_containers(applied)
    assert plan.names() == ["scenario"]
    assert applied["containers"]["scenario"]["resources"]["gpu"] == 1
    assert _document(ex) == {"sim": {"renderer": "splat"}}
    # The stepped simulator reads the overrides from the file the job mounts.
    assert applied["_backend_env"]["ROQSIM_WORLD_OVERRIDES"] == "/config/sim.overrides.yaml"


def test_in_the_ros_shape_the_gpu_is_on_the_simulation_container():
    ex = _execution("ros2", {"name": "splat", "gpu": 1})
    applied = apply_backend(dict(ex))
    sim = applied["containers"]["simulation"]
    assert sim["resources"] == {"gpu": 1}
    assert "--override" in sim["command"]
    assert "resources" not in applied["containers"].get("scenario", {})


def test_the_author_s_resources_keep_their_keys_and_gain_the_gpu():
    ex = _execution("base", {"name": "splat", "gpu": 1}, resources={"cpu": 4, "memory": "16Gi"})
    applied = apply_backend(dict(ex))
    assert applied["containers"]["scenario"]["resources"] == {"cpu": 4, "memory": "16Gi", "gpu": 1}


def test_the_author_s_overrides_are_kept_beside_the_renderer():
    ex = _execution("base", {"name": "splat", "gpu": 1},
                    overrides={"sim": {"renderers": {"splat": {"min_opacity": 0.05}}}})
    assert _document(ex) == {
        "sim": {"renderer": "splat", "renderers": {"splat": {"min_opacity": 0.05}}}}


def test_a_renderer_service_runs_in_its_own_container_and_the_world_reaches_it():
    service = {"image": "registry.example.org/renderer:1",
               "command": ["serve", "--listen", "{address}"],
               "provenance": {"source": "https://example.org/renderer", "revision": "1"},
               "resources": {"gpu": 1, "memory": "24Gi"}}
    ex = _execution("ros2", {"name": "raytracer", "service": service})
    applied = apply_backend(dict(ex))
    plan = plan_containers(applied)
    assert "renderer" in plan.names()
    renderer = applied["containers"]["renderer"]
    assert renderer["image"] == "registry.example.org/renderer:1"
    assert renderer["command"] == ["serve", "--listen", "unix:/ipc/roqsim-render.sock"]
    assert renderer["resources"] == {"gpu": 1, "memory": "24Gi"}
    # The simulator draws nothing itself, so it needs no GPU.
    assert "resources" not in applied["containers"]["simulation"]
    assert _document(ex) == {"sim": {"renderer": "raytracer", "renderers": {"raytracer": {
        "address": "unix:/ipc/roqsim-render.sock", "command": None}}}}


@pytest.mark.parametrize(("renderer", "overrides", "message"), [
    ({"name": "splat", "gpu": 1}, {"sim": {"renderer": "mujoco"}},
     "overrides set sim.renderer to 'mujoco' and renderer.name is 'splat'"),
    ({"name": "raytracer", "gpu": 1, "service": {"image": "r:1", "command": ["serve"]}}, None,
     "a renderer service draws in its own container"),
    ({"name": "raytracer", "service": {"image": "r:1", "command": ["serve"]}},
     {"sim": {"renderers": {"raytracer": {"address": "tcp://elsewhere:1"}}}},
     "overrides set sim.renderers.raytracer.address"),
    ({"name": "splat", "gpus": 1}, None, "gpus"),
])
def test_a_renderer_that_contradicts_itself_is_refused_at_composition(
        renderer, overrides, message):
    ex = _execution("base", renderer, **({"overrides": overrides} if overrides else {}))
    with pytest.raises(ValueError, match=message):
        apply_backend(dict(ex))
