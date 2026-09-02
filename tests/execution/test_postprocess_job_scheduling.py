# Copyright (C) 2026 Frederik Pasch
# SPDX-License-Identifier: Apache-2.0
"""The postprocessing Job's shape: which containers, in which order, with what.

It is created outside the admission queue -- directly, by the driver -- which is exactly why
what it declares matters. Nothing arbitrates for it, so the manifest is the whole of what
keeps it from colliding with the campaign it is post-processing.

It is also the whole of the orchestration. One pod stages the campaign once and runs both
stages against that one copy, and nothing in the code sequences them: initContainers run to
completion in declaration order before the main container starts, so the *order of the
lists in this manifest* is the schedule.
"""

import pytest

from robovast.common.index_db import DSN_ENV
from robovast.execution.cluster_execution.node_placement import CAMPAIGN_NODE_TOLERATIONS
from robovast.execution.cluster_execution.postprocess_host import ENV_FORCE
from robovast.execution.cluster_execution.postprocess_job import (CAMPAIGN_MOUNT,
                                                                  HOST_CONTAINER,
                                                                  STAGE_CONTAINER,
                                                                  build_manifest)
from robovast.execution.cluster_execution.postprocess_stage import ENV_SKIP_BAGS

_CMDS = [{"plugins": [{"type": "rosout_to_csv"}]}]


@pytest.fixture(autouse=True)
def _the_index_is_configured(monkeypatch):
    """The host container IS the index ingest, so no manifest is built without a DSN.

    ``build_manifest`` refuses rather than staging a campaign's worth of data onto a node
    before failing on config the submitter could already see -- so without this every test
    here would fail on that refusal instead of on the shape it is named for.
    """
    monkeypatch.setenv(DSN_ENV, "host=index.example.com dbname=robovast user=robovast")


def _pod_spec(rosbag_cmds=None, **kw):
    # Not a default argument: a shared mutable default is one a caller can edit for every
    # later test in the file.
    rosbag_cmds = _CMDS if rosbag_cmds is None else rosbag_cmds
    m = build_manifest("camp-2026-08-27-12000000", "img:1", rosbag_cmds,
                       ("http://s3.example.com", "ak", "sk", "bucket", "prefix/"),
                       "robovast", **kw)
    return m["spec"]["template"]["spec"]


def _by_name(spec):
    return {c["name"]: c for c in spec.get("initContainers", []) + spec["containers"]}


def test_the_conversion_job_tolerates_the_campaign_taint():
    """A deployment that dedicates nodes to campaigns has nowhere else to put this.

    The campaign's own job pods carry the toleration themselves, and this Job -- created
    outside the admission path -- was missed. The symptom is not an error but a Pending pod
    that gives up after three hours.
    """
    assert _pod_spec()["tolerations"] == list(CAMPAIGN_NODE_TOLERATIONS)


# -- who runs, in what order -------------------------------------------------


def test_the_containers_run_stage_then_convert_then_host():
    """The declaration order IS the schedule; nothing else sequences these steps.

    initContainers run sequentially to completion in declaration order and the main
    container starts only once they all succeed. So a convert declared before stage would
    read an empty mount, and a host promoted to an initContainer would ingest before the
    conversion it is meant to follow -- neither of which any code would catch, because
    there is no code: the list is the orchestration.
    """
    spec = _pod_spec()

    assert [c["name"] for c in spec["initContainers"]] == [STAGE_CONTAINER, "convert"]
    assert [c["name"] for c in spec["containers"]] == [HOST_CONTAINER]


def test_a_conversion_only_job_makes_the_conversion_the_main_container():
    """``host_stage=False`` is the search's per-batch path, and it has no host step.

    The host step writes the campaign's provenance marker and drives the index ingest, so
    running it per batch would mark a partial campaign postprocessed, repeatedly. With it
    off the conversion has to be the pod's *main* container: a pod needs one to complete,
    and it is also where ``get_job_log`` looks. A conversion left as an initContainer with
    no main container behind it is a Job that never finishes.
    """
    spec = _pod_spec(host_stage=False)

    assert [c["name"] for c in spec["initContainers"]] == [STAGE_CONTAINER]
    assert [c["name"] for c in spec["containers"]] == ["convert"]
    assert HOST_CONTAINER not in _by_name(spec)


def test_a_campaign_with_nothing_to_convert_never_reaches_for_an_image():
    """No conversion container, no scripts ConfigMap, and no execution image resolved.

    A host-only campaign needs none of the three, and each is a way for it to fail on
    something irrelevant to it: an execution image that has since gone from the registry
    would hold the pod in ImagePullBackOff, and a declared ConfigMap volume whose source
    was never created holds it in ContainerCreating. Passing ``None`` as the image is what
    a caller that cannot know does, and it must be enough.
    """
    spec = _pod_spec(rosbag_cmds=[])

    containers = _by_name(spec)
    assert set(containers) == {STAGE_CONTAINER, HOST_CONTAINER}
    assert [v["name"] for v in spec["volumes"] if v["name"] == "scripts"] == []
    # And the image is never consulted at all, so None is not a hole in the manifest.
    none_image = _pod_spec(rosbag_cmds=[])
    assert "img:1" not in str(none_image)
    for container in _by_name(none_image).values():
        for mount in container["volumeMounts"]:
            assert mount["name"] != "scripts"


def test_bags_are_staged_exactly_when_something_in_the_pod_opens_one():
    """Nothing else in this pod reads a rosbag, and they are the bulk of a campaign.

    The host step reads the derived tables and the run metadata, never a bag, so with no
    conversion container the whole download and the whole node disk would be spent on data
    nothing reads. Set when it should not be, a campaign's conversion is handed a tree with
    no bags in it and reports every one of them missing.
    """
    with_bags = _by_name(_pod_spec())[STAGE_CONTAINER]["env"]
    without = _by_name(_pod_spec(rosbag_cmds=[]))[STAGE_CONTAINER]["env"]

    assert ENV_SKIP_BAGS not in [e["name"] for e in with_bags]
    assert {"name": ENV_SKIP_BAGS, "value": "1"} in without


# -- what each container is trusted with -------------------------------------


def test_only_the_host_container_is_given_the_index_and_the_store():
    """The conversion runs an arbitrary user image, so it holds no credential at all.

    It is the campaign's own image -- the system under test's -- and the only reason it is
    in this pod is that custom ROS2 types deserialize nowhere else. It reads and writes the
    shared campaign mount and nothing more, so anything that would let it reach the store
    or the index is a credential handed to a stranger for no purpose. The index DSN goes
    to the host container alone, because that is the only container that writes the index.
    """
    containers = _by_name(_pod_spec())

    assert "env" not in containers["convert"] or containers["convert"]["env"] == []

    host_env = {e["name"] for e in containers["host"]["env"]}
    assert DSN_ENV in host_env
    assert {"S3_ENDPOINT", "S3_ACCESS_KEY", "S3_SECRET_KEY", ENV_FORCE} <= host_env

    # The stage container talks to the store too, but never to the index: it fetches.
    stage_env = {e["name"] for e in containers[STAGE_CONTAINER]["env"]}
    assert "S3_ACCESS_KEY" in stage_env and DSN_ENV not in stage_env


# -- what the pod reserves ---------------------------------------------------


def test_every_container_in_every_shape_reserves_cpu_and_memory():
    """A pod with no requests is invisible to the capacity reading.

    The budget provider counts the REQUESTS of bound pods, so a container declaring none
    contributes zero while consuming real cores on the nodes the trials are running on --
    admission promising room that a BestEffort neighbour is already spending. A run's figures
    then depend on what else happened to be on the machine, which is the measurement-validity
    failure the governor work exists to remove.

    Every shape, because the shapes differ in which containers exist: a container that
    declares nothing is only ever missed in the shape nobody checked.
    """
    for shape in ({}, {"host_stage": False}, {"rosbag_cmds": []}):
        for name, container in _by_name(_pod_spec(**shape)).items():
            requests = container["resources"]["requests"]
            assert requests["cpu"] and requests["memory"], (shape, name)


def test_the_shared_campaign_volume_is_backed_by_a_storage_request():
    """Staging pulls a whole campaign into an ``emptyDir`` on somebody's node.

    Unreserved, that is a node filling up under a pod nobody warned -- disk pressure evicts
    the campaign pods running beside it, and the runs are lost to a cause that appears
    nowhere near it. The pod is charged for that one volume whichever of its containers is
    running, so every container has to declare the reservation: the scheduler reads the
    pod's requests as the max over initContainers and the sum over the rest, and a
    container that omits it drops the reservation for as long as it is the one running.
    """
    for shape in ({}, {"host_stage": False}, {"rosbag_cmds": []}):
        spec = _pod_spec(**shape)
        # One emptyDir, mounted everywhere: there is exactly one copy of the data.
        assert {"name": "campaign", "emptyDir": {}} in spec["volumes"]
        for name, container in _by_name(spec).items():
            assert container["resources"]["requests"]["ephemeral-storage"], (shape, name)
            assert {"name": "campaign", "mountPath": CAMPAIGN_MOUNT} \
                in container["volumeMounts"], (shape, name)


@pytest.mark.parametrize("rosbag_cmds", [[], _CMDS], ids=["host-only", "with-conversion"])
def test_a_volume_is_declared_only_where_a_container_mounts_it(rosbag_cmds):
    """A pod spec carrying a volume nothing mounts leaves a reader to work out whether the
    missing mount is the bug or the volume is. Each shape declares exactly what it uses:
    the conversion's scratch belongs to the conversion, so it appears when that container
    does and not otherwise."""
    spec = _pod_spec(rosbag_cmds=rosbag_cmds)

    declared = {v["name"] for v in spec["volumes"]}
    mounted = {m["name"]
               for c in spec.get("initContainers", []) + spec["containers"]
               for m in c.get("volumeMounts", [])}

    assert declared == mounted
