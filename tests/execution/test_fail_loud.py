# Copyright (C) 2026 Frederik Pasch
# SPDX-License-Identifier: Apache-2.0
"""Regression tests for the no-fallback contract of the cluster execution path.

Each case pins a spot that could silently degrade (swallow an error and proceed
with a quietly-wrong configuration) and asserts it fails loudly instead.
"""

import json
import os
from unittest import mock

import pytest

# -- A1: the auxiliary container a composition needs -------------------------

# Whether a composition needs an auxiliary container is answered by asking for one, so there is no
# spec list here that could be wrong -- see tests/execution/test_aux_containers_are_created_on_demand.py.
# What this file pins is the other direction: a container that cannot be provided must not be
# answered around.


def test_an_unanswerable_input_files_query_is_not_swallowed_into_an_empty_list():
    """The simulator alone can enumerate what a world is made of; a guess is not a substitute.

    ``_backend_run_files`` swallows a backend it cannot resolve, on purpose -- validation reports
    that properly elsewhere. It must NOT swallow "the backend answered, and nothing here could
    ask": the campaign would then stage the one file the `.vast` names, pull its image, schedule
    its pod and die on a parent world that never travelled.
    """
    from robovast.common.config_generation import _backend_run_files
    from robovast.common.errors import AuxContainerUnavailable
    from robovast.common.simulators import ContainerQuery
    from robovast.common.variation.container_runner import ContainerSpec

    query = ContainerQuery(ContainerSpec(image="family:robovast-roqsim"), ["enumerate"])
    parameters = {"execution": {"containers": {"simulation": {"backend": "stub"}}}}

    class _Backend:
        CONFIG_CLASS = None

        @staticmethod
        def input_files(_cfg, _execution, _vast_dir):
            return query

    with mock.patch("robovast.common.simulators.resolve_backend", return_value=_Backend()), \
            mock.patch("robovast.common.config_generation._backend_cfg", return_value=object()), \
            mock.patch("robovast.common.config_generation._run_input_files_query",
                       side_effect=AuxContainerUnavailable("no runner here")):
        with pytest.raises(AuxContainerUnavailable):
            _backend_run_files("/tmp", parameters)


def test_a_query_that_could_not_be_asked_at_all_is_not_swallowed_either():
    """Not having a runner is one way of failing to ask, and not the likely one.

    The service installs a runner factory unconditionally, so the likely failures are that
    the factory builds the aux pod and the pod does not come up, or the query runs and prints
    nothing a caller can read. Those must propagate for exactly the reason the missing runner
    does: the difference between "this world is one file" and "nobody could ask" is invisible
    until the run opens a parent that never travelled.
    """
    from robovast.common.config_generation import _backend_run_files
    from robovast.common.simulators import ContainerQuery
    from robovast.common.variation.container_runner import ContainerSpec

    query = ContainerQuery(ContainerSpec(image="family:robovast-roqsim"), ["enumerate"])
    parameters = {"execution": {"containers": {"simulation": {"backend": "stub"}}}}

    class _Backend:
        CONFIG_CLASS = None

        @staticmethod
        def input_files(_cfg, _execution, _vast_dir):
            return query

    with mock.patch("robovast.common.simulators.resolve_backend", return_value=_Backend()), \
            mock.patch("robovast.common.config_generation._backend_cfg", return_value=object()), \
            mock.patch("robovast.common.config_generation._run_input_files_query",
                       side_effect=RuntimeError("aux pod never became ready")):
        with pytest.raises(RuntimeError, match="never became ready"):
            _backend_run_files("/tmp", parameters)


def test_an_unresolvable_backend_is_still_swallowed():
    """The other half of the same line, so the fix above cannot quietly widen.

    A campaign that never mentions a simulator must not fail composition because a backend
    it does not use cannot be imported; validation reports that where it can be acted on.
    """
    from robovast.common.config_generation import _backend_run_files

    parameters = {"execution": {"containers": {"simulation": {"backend": "stub"}}}}
    with mock.patch("robovast.common.simulators.resolve_backend",
                    side_effect=RuntimeError("no such backend")):
        assert _backend_run_files("/tmp", parameters) == []


def test_the_worlds_the_simulator_named_become_campaign_inputs(tmp_path):
    """A world's parent has to end up in `run_files`, or the query bought nothing.

    The simulator answers in the paths it was asked in -- the campaign tree is exposed at
    `/config` and the command names the world there -- so an answer read as this host's paths
    matches nothing and the campaign stages neither the world nor its parent. What it opens
    then depends on a `run_files` glob happening to cover them.
    """
    from robovast.common.config_generation import _run_input_files_query
    from robovast.common.simulators import ContainerQuery
    from robovast.common.variation.container_runner import ContainerSpec

    (tmp_path / "world").mkdir()

    class _Runner:
        workspace = str(tmp_path / "ws")

        def run(self, _command, progress_update_callback=None):
            # What `roqsim scenes inputs` prints: absolute, in the container, one JSON line.
            # The mesh is the packaged case -- it arrives with the image and must not be copied.
            progress_update_callback(json.dumps({
                "world": "/config/world/child.yaml",
                "packaged": False,
                "inputs": ["/config/world/child.yaml", "/config/world/parent.yaml",
                           "/opt/roqsim/scenes/empty_room/floor.stl"]}))

        def expose(self, _host_path, _container_path):
            pass

        def close(self):
            pass

    query = ContainerQuery(ContainerSpec(image="family:robovast-roqsim"),
                           ["roqsim", "scenes", "inputs", "/config/world/child.yaml"])
    with mock.patch("robovast.common.config_generation._make_container_runner",
                    return_value=_Runner()):
        declared = _run_input_files_query(query, str(tmp_path))

    assert declared == [os.path.join("world", "child.yaml"),
                        os.path.join("world", "parent.yaml")], declared


def test_a_configuration_s_world_is_enumerated_or_the_composition_fails(tmp_path):
    """The same contract per configuration, and there without the `sim:` channel either.

    A configuration's world is resolved after the variation loop, and a failure there is
    swallowed unless the campaign writes the ``sim`` channel -- a backend that cannot be
    imported must not break a campaign that never mentions one. "The world extends a file only
    the simulator can resolve, and nothing here could ask it" is not that: dropping it stages
    a world without its parent, whichever way the campaign is written.

    Run against the real roqsim backend on a real world chain, with the local ``docker``
    fallback taken away -- the state a service pod is in.
    """
    import shutil

    from robovast.common.config_generation import _resolve_config_sim_blocks
    from robovast.common.errors import AuxContainerUnavailable

    (tmp_path / "parent.yaml").write_text("components: []\n", encoding="utf-8")
    (tmp_path / "child.yaml").write_text("extends: parent.yaml\ncomponents: []\n",
                                         encoding="utf-8")
    parameters = {"execution": {"containers": {"simulation": {"backend": "roqsim",
                                                              "config": "child.yaml"}}},
                  "configuration": [{"name": "only"}]}
    configs = [{"_config_name": "only", "config": {}}]

    with mock.patch.object(shutil, "which", return_value=None):
        with pytest.raises(AuxContainerUnavailable, match="input-files query"):
            _resolve_config_sim_blocks(configs, parameters, str(tmp_path), [])


def test_cpu_manager_policy_unknown_on_query_failure():
    """A configz read failure is *unknown* (None), never silently reported as "none"."""
    from robovast.common import execution

    client = mock.Mock()
    client.connect_get_node_proxy_with_path.side_effect = RuntimeError("no configz")
    assert execution._check_static_cpu_manager(client, "node-1") is None


def test_cpu_manager_policy_reads_value_when_available():
    from robovast.common import execution

    client = mock.Mock()
    client.connect_get_node_proxy_with_path.return_value = \
        '{"kubeletconfig": {"cpuManagerPolicy": "static"}}'
    assert execution._check_static_cpu_manager(client, "node-1") == "static"


# -- A5: single kube-config loader -------------------------------------------

def test_load_kube_config_raises_when_no_source():
    """Neither in-cluster nor host config available must raise, not proceed silently."""
    from kubernetes import config as kc

    from robovast.execution.cluster_execution.kube_client import load_kube_config
    with mock.patch.object(kc, "load_incluster_config",
                           side_effect=kc.ConfigException("not in cluster")), \
         mock.patch.object(kc, "load_kube_config",
                           side_effect=Exception("no kubeconfig")):
        with pytest.raises(RuntimeError, match="no Kubernetes configuration available"):
            load_kube_config(context="x")


def test_load_kube_config_prefers_in_cluster():
    from kubernetes import config as kc

    from robovast.execution.cluster_execution.kube_client import load_kube_config
    with mock.patch.object(kc, "load_incluster_config", return_value=None):
        assert load_kube_config() == "in-cluster"
