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

import json

import pytest

from robovast.execution.cluster_execution import postprocess_job as pj

from .image_steps_helper import steps

from robovast.common.index_db import DSN_ENV
from robovast.execution.cluster_execution.node_placement import CAMPAIGN_NODE_TOLERATIONS
from robovast.execution.cluster_execution import pod_access
from robovast.execution.cluster_execution.postprocess_host import ENV_COMMANDS, ENV_FORCE
from robovast.execution.cluster_execution.postprocess_job import (CAMPAIGN_MOUNT,
                                                                  HOST_CONTAINER,
                                                                  STAGE_CONTAINER,
                                                                  build_manifest)

_CMDS = steps("camp-2026-08-27-12000000")


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
    m = build_manifest("camp-2026-08-27-12000000", "img:1", rosbag_cmds, "robovast", **kw)
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


def test_a_batch_job_has_the_same_shape_and_is_told_not_to_complete():
    """``complete_campaign=False`` is the search's per-batch path, and it is not a
    different pod -- it is the same pod asked for less.

    What a batch must not do is COMPLETE the campaign: the index ingest, the metadata and
    the provenance record describe a finished campaign, and this runs per batch on one that
    is still growing. What it must still do is derive and deliver, and the host container
    is the only one holding the campaign's token -- the conversion runs the campaign's own
    image and is given nothing. A Job that ended at its conversion would leave every CSV in
    the pod's emptyDir, so the batch could never be scored, and nothing would report an
    error: the Job succeeds, nothing lands, and the search spends its budget scoring no
    cells.
    """
    cmds = [{"nav2_bt_tree": {"bt_xml": "files/bt.xml"}}]
    spec = _pod_spec(role=pj.JobRole.search_batch("", cmds))

    assert [c["name"] for c in spec["initContainers"]] == [STAGE_CONTAINER, "convert"]
    assert [c["name"] for c in spec["containers"]] == [HOST_CONTAINER]
    host_env = {e["name"]: e["value"] for e in _by_name(spec)[HOST_CONTAINER]["env"]
                if "value" in e}
    # The batch's OWN list, carried in. `search.postprocessing` and
    # `results_processing.postprocessing` are different blocks, so a Job left to look its
    # own up would derive the campaign-level one.
    assert json.loads(host_env[ENV_COMMANDS]) == cmds


def test_a_campaign_level_job_looks_its_own_pass_up():
    """No commands passed: the host runs the whole `results_processing` pass and completes."""
    host_env = {e["name"]: e["value"] for e in _by_name(_pod_spec())[HOST_CONTAINER]["env"]
                if "value" in e}
    assert ENV_COMMANDS not in host_env


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
    with_bags = _by_name(_pod_spec())[STAGE_CONTAINER]["command"][-1]
    without = _by_name(_pod_spec(rosbag_cmds=[]))[STAGE_CONTAINER]["command"][-1]

    # The selection is a query on the archive the stage fetches: decided where the bytes
    # are, so what the pod is not given it does not pay to download.
    assert "skip_bags=false" in with_bags
    assert "skip_bags=true" in without


# -- what each container is trusted with -------------------------------------


def test_only_the_host_container_is_given_the_index_and_only_ours_the_token():
    """The conversion runs an arbitrary user image, so it holds no credential at all.

    It is the campaign's own image -- the system under test's -- and the only reason it is
    in this pod is that custom ROS2 types deserialize nowhere else. It reads and writes the
    shared campaign mount and nothing more, so anything that would let it reach the data
    plane or the index is a credential handed to a stranger for no purpose. The index DSN
    goes to the host container alone, because that is the only container that writes the
    index; the campaign's token goes to the two containers that move its bytes.
    """
    containers = _by_name(_pod_spec())

    # The property is that it holds no CREDENTIAL, not that it holds no environment: it is
    # also the container whose progress output the live log is read from, so it carries the
    # one variable that gets that output out unbuffered.
    convert_env = {e["name"] for e in containers["convert"].get("env") or []}
    assert not (convert_env - {"PYTHONUNBUFFERED"}), (
        f"the conversion runs a stranger's image and was handed {convert_env}")

    host_env = {e["name"] for e in containers["host"]["env"]}
    assert DSN_ENV in host_env
    assert {pod_access.DATA_URL_ENV, pod_access.TOKEN_ENV, pod_access.CAMPAIGN_ID_ENV,
            ENV_FORCE} <= host_env

    # The stage container reaches the data plane too, but never the index: it fetches.
    stage_env = {e["name"] for e in containers[STAGE_CONTAINER]["env"]}
    assert pod_access.TOKEN_ENV in stage_env and DSN_ENV not in stage_env



@pytest.mark.parametrize("role", [pj.JobRole.campaign(), pj.JobRole.for_part("part-1", []),
                                  pj.JobRole.reduce(stage_bags=False)],
                         ids=["campaign", "part", "reduce"])
@pytest.mark.parametrize("rosbag_cmds", [[], _CMDS], ids=["host-only", "with-conversion"])
def test_the_host_finds_the_git_token_where_the_plugin_install_looks(role, rosbag_cmds):
    """The host re-installs the campaign's ``plugins:``, so it needs what the service has.

    A ``git+https`` spec for a private repository is cloned by that install, and without the
    token the clone asks for a username it has no terminal to read. The service pod had the
    Secret and every postprocessing Job did not, so a campaign that composed and ran got
    its plugin install refused the moment its postprocessing moved into Jobs of its own.
    Every role, because every role's host runs that install.
    """
    from robovast.common.config_plugins import GIT_TOKEN_FILE
    from robovast.execution.cluster_execution.service_deploy import (GIT_SECRET_KEY,
                                                                     GIT_SECRET_NAME)

    spec = _pod_spec(rosbag_cmds=rosbag_cmds, role=role)
    volume = next(v for v in spec["volumes"] if v["name"] == "git-credentials")
    assert volume["secret"]["secretName"] == GIT_SECRET_NAME
    # Optional: the Secret exists only where setup was given a token, and a required one
    # naming nothing holds the pod in ContainerCreating instead of letting the install say
    # which token is missing.
    assert volume["secret"]["optional"] is True

    mount = next(m for m in _by_name(spec)[HOST_CONTAINER]["volumeMounts"]
                 if m["name"] == "git-credentials")
    assert mount["readOnly"] is True
    assert f"{mount['mountPath']}/{GIT_SECRET_KEY}" == GIT_TOKEN_FILE


def test_only_the_host_container_mounts_the_git_token():
    """The conversion runs the campaign's own image, and the stage only fetches bytes.

    Neither installs a plugin, so neither has a use for the token -- and the conversion is
    a stranger's image, the one container in this pod that must hold no credential.
    """
    for name, container in _by_name(_pod_spec()).items():
        mounts = {m["name"] for m in container.get("volumeMounts", [])}
        assert ("git-credentials" in mounts) == (name == HOST_CONTAINER), name


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
    for shape in ({}, {"batch_commands": []}, {"rosbag_cmds": []}):
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
    for shape in ({}, {"batch_commands": []}, {"rosbag_cmds": []}):
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


def test_one_tree_is_writable_by_containers_that_run_as_different_users():
    """The stage container creates the campaign tree and the conversion writes its outputs
    INTO it, as a different user: the sidecar and controller images run as root, an
    execution image as its own unprivileged user. Without a shared group AND group-writable
    modes, the conversion fails on its first output file with EACCES -- after staging the
    whole campaign, so the cost is paid before the failure.

    Both halves are asserted because either alone is useless: a group that cannot write is
    not access, and a group-writable mode on a tree the other user is not in the group of
    is not either. The stage extracts with `tar`, which as root restores the archive's
    owner and mode, so it hands the tree over explicitly; the host creates files itself,
    so its umask is what decides.
    """
    spec = _pod_spec()

    assert spec["securityContext"]["fsGroup"] == pj.CAMPAIGN_TREE_GID
    assert pj.CAMPAIGN_TREE_GID in spec["securityContext"]["supplementalGroups"]
    containers = _by_name(spec)
    stage = " ".join(containers[STAGE_CONTAINER]["command"])
    assert f"chgrp {pj.CAMPAIGN_TREE_GID}" in stage and "chmod g+rwX" in stage
    assert "umask 0002" in " ".join(containers[HOST_CONTAINER]["command"])


def test_every_python_container_runs_unbuffered():
    """The campaign log is read from the pod's stdout, and stdout here is a pipe.

    Python block-buffers a pipe, so a step's output would reach the log in ~8 KB clumps long
    after it happened -- and publishing a running postprocess exists precisely so someone can
    watch it. The conversion container matters most: it is the longest step and the source of
    the progress output, and it is the one container deliberately given no other environment,
    which makes it the easiest to leave out. The stage runs no Python -- `curl | tar` --
    so it has nothing to unbuffer.
    """
    containers = _by_name(_pod_spec())

    assert containers, "the manifest defines no containers"
    for name, container in sorted(containers.items()):
        env = {e["name"]: e.get("value") for e in container.get("env") or []}
        if name == STAGE_CONTAINER:
            assert "PYTHONUNBUFFERED" not in env
            continue
        assert env.get("PYTHONUNBUFFERED") == "1", (
            f"container {name} buffers its output, so the live log lags behind it")


def test_a_batch_stages_only_its_own_jobs():
    """Every batch's bags sit under the same campaign prefix, and bags are the bulk.

    Without narrowing, batch N stages batches 0..N as well as its own, so a search's
    staging grows with the campaign while the work per batch does not. The tag is the one
    the batch's runs were already written under, so there is one naming of a batch rather
    than two.
    """
    spec = _pod_spec(role=pj.JobRole.search_batch("batch-3/reps-5", [{"nav2_bt_tree": {}}]))

    fetch = _by_name(spec)[STAGE_CONTAINER]["command"][-1]
    assert "batch_jobs=batch-3%2Freps-5" in fetch


def test_a_campaign_level_pass_stages_every_batch():
    """It derives the whole campaign, so narrowing it to one batch would hide the rest."""
    fetch = _by_name(_pod_spec())[STAGE_CONTAINER]["command"][-1]
    assert "batch_jobs" not in fetch
