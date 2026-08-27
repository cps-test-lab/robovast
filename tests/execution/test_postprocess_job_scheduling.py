# Copyright (C) 2026 Frederik Pasch
# SPDX-License-Identifier: Apache-2.0
"""The conversion Job's scheduling contract.

It is created outside the admission queue -- directly, by the driver -- which is exactly why
what it declares matters. Nothing arbitrates for it, so the manifest is the whole of what
keeps it from colliding with the campaign it is post-processing.
"""

from robovast.execution.cluster_execution.node_placement import CAMPAIGN_NODE_TOLERATIONS
from robovast.execution.cluster_execution.postprocess_job import build_manifest


def _pod_spec(**kw):
    m = build_manifest("camp-2026-08-27-12000000", "img:1",
                       [{"plugins": [{"type": "rosout_to_csv"}]}],
                       ("http://s3.example.com", "ak", "sk", "bucket", "prefix/"),
                       "robovast", **kw)
    return m["spec"]["template"]["spec"]


def test_the_conversion_job_tolerates_the_campaign_taint():
    """A deployment that dedicates nodes to campaigns has nowhere else to put this.

    Kueue's flavor used to inject the toleration; when it was retired the job pods learned to
    carry it themselves and this Job -- created outside the admission path -- was missed. The
    symptom is not an error but a Pending pod that gives up after three hours.
    """
    assert _pod_spec()["tolerations"] == list(CAMPAIGN_NODE_TOLERATIONS)


def test_every_container_reserves_something():
    """A pod with no requests is invisible to the capacity reading.

    The budget provider counts the REQUESTS of bound pods, so a container declaring none
    contributes zero while consuming real cores on the nodes the trials are running on --
    admission promising room that a BestEffort neighbour is already spending. A run's figures
    then depend on what else happened to be on the machine, which is the measurement-validity
    failure the governor work exists to remove.
    """
    spec = _pod_spec()
    containers = spec["containers"] + spec["initContainers"]
    for container in containers:
        requests = container["resources"]["requests"]
        assert requests["cpu"] and requests["memory"], container["name"]


def test_the_bags_volume_is_backed_by_a_storage_request():
    """The mirror pulls a whole campaign's rosbags into an emptyDir.

    Unreserved, that is a node filling up under pods nobody warned -- disk pressure evicts
    the campaign pods running beside it, and the runs are lost to a cause that appears
    nowhere near it.
    """
    spec = _pod_spec()
    init = spec["initContainers"][0]
    assert init["resources"]["requests"]["ephemeral-storage"]
    assert spec["containers"][0]["resources"]["requests"]["ephemeral-storage"]
