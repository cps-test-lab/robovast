# Copyright (C) 2026 Frederik Pasch
#
# SPDX-License-Identifier: Apache-2.0

"""An aux container is created by asking for one -- nothing decides in advance whether to.

Three kinds of thing reach for a helper image while a campaign composes: a variation plugin, an
``execution.generate`` input generator, and the simulator backend's query that resolves what a
world is made of. The list is open, and every one of them runs *inside* composition.

So the lane does not predict it. It installs a container-runner factory unconditionally, and
:class:`AuxPodSession` creates a pod when that factory is first called for a spec. Deciding
beforehand -- reading the ``.vast``, collecting specs, installing a factory only if the list came
back non-empty -- is a second implementation of the same enumeration, and whatever it does not
cover is a campaign that fails while composing on a container it declared: in the service pod
there is no ``docker`` to fall back on, so a missing factory is a refusal rather than a slow path.

These tests hold the invariant from both ends -- the lane always installs a factory, and the
session creates nothing until it is asked -- rather than enumerating who may ask. A new asker
needs no change here, which is the point.

:func:`test_the_lane_does_not_read_the_project_to_decide` asserts on the *shape* of the code
rather than on behaviour, because what matters here is an absence: a re-introduced spec list makes
every behavioural test below pass while the campaigns it cannot enumerate fail, and a branch that
restores the old source together with its old tests leaves nothing to notice.
"""

import contextlib
import inspect
from types import SimpleNamespace

import pytest

from robovast.common.variation.container_runner import ContainerSpec
from robovast.execution.cluster_execution.container_runner import AuxPodSession, aux_pod_name


@pytest.fixture(name="kube")
def _kube(monkeypatch):
    """A fake core API that records pod creates and deletes, with the waits stubbed out."""
    events = []

    class _Core:
        def create_namespaced_pod(self, namespace, manifest):
            events.append(("create", manifest["metadata"]["name"]))
            return SimpleNamespace(metadata=SimpleNamespace(name=manifest["metadata"]["name"]))

        def delete_namespaced_pod(self, name, namespace, **kwargs):
            events.append(("delete", name))

    monkeypatch.setattr(
        "robovast.execution.cluster_execution.kube_client.wait_pod_ready",
        lambda *a, **k: events.append(("ready", a[2] if len(a) > 2 else "")))
    monkeypatch.setattr(
        "robovast.execution.cluster_execution.container_runner.service_pod_owner_reference",
        lambda *a, **k: None)
    return SimpleNamespace(core=_Core(), events=events)


def _session(kube):
    return AuxPodSession("c-2026-09-08-120000", "ns", core_v1=kube.core)


# -- the session creates what it is asked for, and nothing else ----------------------


def test_entering_creates_nothing(kube):
    """What makes an unconditional factory affordable.

    A pod created on entry would have to be a pod somebody decided was needed, which is the
    prediction this replaces.
    """
    with _session(kube):
        pass
    assert kube.events == []


def test_asking_for_a_spec_creates_its_pod_and_addresses_the_runner_at_it(kube):
    spec = ContainerSpec(image="family:robovast-roqsim")
    with _session(kube) as session:
        runner = session.runner_factory()(spec)
        try:
            expected = aux_pod_name("c-2026-09-08-120000", spec.container_name())
            assert ("create", expected) in kube.events
            assert runner._pod == expected
            assert runner._container == spec.container_name()
        finally:
            runner.close()


def test_a_spec_no_caller_declared_is_served_all_the_same(kube):
    """The invariant, rather than a list of askers.

    A variation, an input generator, a simulator backend's world query: the factory does not
    know which asked, and does not have to.
    """
    with _session(kube) as session:
        factory = session.runner_factory()
        runner = factory(ContainerSpec(image="ghcr.io/example/some-tool:1"))
        try:
            assert runner is not None
        finally:
            runner.close()


def test_the_same_spec_twice_is_one_pod(kube):
    """A composition asking a second time must not pay a second create and image pull."""
    spec = ContainerSpec(image="family:robovast-roqsim")
    with _session(kube) as session:
        factory = session.runner_factory()
        first, second = factory(spec), factory(spec)
        try:
            assert [e for e in kube.events if e[0] == "create"] == [
                ("create", aux_pod_name("c-2026-09-08-120000", spec.container_name()))]
            assert first._pod == second._pod
        finally:
            first.close()
            second.close()


def test_two_different_specs_get_two_pods(kube):
    """A pod's container set is fixed when it is created, so the second spec needs its own.

    Their names differ, which is what keeps the second from colliding with the first: a
    ``family:`` ref is named after its member rather than the word ``family``.
    """
    specs = [ContainerSpec(image="family:robovast-roqsim"),
             ContainerSpec(image="ghcr.io/example/builder")]
    with _session(kube) as session:
        factory = session.runner_factory()
        runners = [factory(s) for s in specs]
        try:
            created = [name for kind, name in kube.events if kind == "create"]
            assert len(set(created)) == 2, created
            assert {r._pod for r in runners} == set(created)
        finally:
            for runner in runners:
                runner.close()


def test_every_pod_it_created_is_deleted_on_the_way_out(kube):
    specs = [ContainerSpec(image="family:robovast-roqsim"),
             ContainerSpec(image="ghcr.io/example/builder")]
    with _session(kube) as session:
        factory = session.runner_factory()
        for spec in specs:
            factory(spec).close()
    deleted = {name for kind, name in kube.events if kind == "delete"}
    created = {name for kind, name in kube.events if kind == "create"}
    assert deleted == created


def test_a_caller_holding_the_spec_may_create_it_up_front(kube):
    """``provision`` is not the prediction: a scene build knows the one image it compiles.

    It pays the pull where it can report it as a stage of the build, and gets the same pod the
    factory would have made.
    """
    spec = ContainerSpec(image="ghcr.io/example/exporter:1")
    with _session(kube) as session:
        pod = session.provision(spec)
        runner = session.runner_factory()(spec)
        try:
            assert runner._pod == pod
            assert [e for e in kube.events if e[0] == "create"] == [("create", pod)]
        finally:
            runner.close()


# -- the lane installs a factory whatever the project says ---------------------------


def _service():
    """A ClusterService with only what ``_aux_runner_context`` touches, and no cluster."""
    from robovast.execution.cluster_execution.cluster_service import ClusterService

    service = object.__new__(ClusterService)
    service.namespace = "ns"
    service.kube_context = "local"
    service._k8s = lambda: object()
    service._registry_pull_secret = lambda: ""
    service._aux_store_kwargs = lambda: {}
    return service


@pytest.mark.parametrize("hold", [False, True])
def test_the_lane_installs_a_factory_for_a_project_that_declares_no_aux_container(monkeypatch,
                                                                                 hold):
    """Both spans -- a campaign's, and the held one an authoring loop previews through.

    A `.vast` with no variation image and no ``execution.generate`` image is the campaign a
    spec list reports as needing nothing, and also one whose world only the simulator can
    enumerate.
    """
    from robovast.common.config_generation import _container_runner_factory
    from robovast.execution.cluster_execution import cluster_service as mod

    sentinel = object()

    @contextlib.contextmanager
    def _held(_self, _tag):
        yield lambda _spec: sentinel

    monkeypatch.setattr(mod.ClusterService, "_held_aux_runners", _held)
    monkeypatch.setattr(
        "robovast.execution.cluster_execution.container_runner.AuxPodSession.runner_factory",
        lambda _self: (lambda _spec: sentinel))

    service = _service()
    project = SimpleNamespace(config_path="/nonexistent/campaign.vast")
    with service._aux_runner_context("c-2026-09-08-120000", project, hold=hold):
        factory = _container_runner_factory.get()
        assert factory is not None, (
            "the lane installed no runner factory, so a composition asking for an auxiliary "
            "container is refused in a pod that has no docker to fall back on"
        )
        assert factory(ContainerSpec(image="family:robovast-roqsim")) is sentinel
    assert _container_runner_factory.get() is None, "the factory outlived its span"


def test_the_lane_does_not_read_the_project_to_decide():
    """Structural, and deliberately so -- see the module docstring."""
    from robovast.execution.cluster_execution.cluster_service import ClusterService

    source = inspect.getsource(ClusterService._aux_runner_context)
    assert "config_path" not in source and "required_container_specs" not in source, (
        "_aux_runner_context reads the project again to decide whether to install a runner "
        "factory. That is a second copy of what composition enumerates, and a campaign fails "
        "while composing for whatever it does not cover; the session creates a pod on demand "
        "instead."
    )
