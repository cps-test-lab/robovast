# Copyright (C) 2026 Frederik Pasch
# SPDX-License-Identifier: Apache-2.0
"""Emptying the node directories a cleanup deliberately keeps.

The only irreversible thing ``vast cluster cleanup`` can be asked to do, so what it will
touch is checked before a container ever sees it.
"""

import pytest

from robovast.execution.cluster_execution import data_purge


@pytest.mark.parametrize("path", ["/", "/var", "", "relative/path", "/a/../../etc"])
def test_a_path_too_shallow_to_be_meant_is_refused(path):
    """Every path here is read from a live object rather than typed.

    So this guards against a bug in what read it, not against an operator -- and the bug it
    guards against empties a node's root filesystem, which no message afterwards can undo.
    """
    with pytest.raises(ValueError) as excinfo:
        data_purge.refuse_an_unsafe_path(path)
    assert "refusing to empty" in str(excinfo.value)


@pytest.mark.parametrize("path", ["/var/lib/robovast-store", "/media/data/results"])
def test_a_real_data_directory_passes(path):
    data_purge.refuse_an_unsafe_path(path)


def test_the_job_mounts_every_path_and_is_pinned_to_the_data_node():
    """Unpinned, it would land wherever the scheduler liked and empty the wrong node's disk."""
    manifest = data_purge.purge_manifest(
        "default", ["/media/data/store", "/media/data/results"],
        {"robovast.io/data-node": "true"})

    spec = manifest["spec"]["template"]["spec"]
    assert [v["hostPath"]["path"] for v in spec["volumes"]] == [
        "/media/data/store", "/media/data/results"]
    assert spec["nodeSelector"] == {"robovast.io/data-node": "true"}
    assert spec["tolerations"] == [{"operator": "Exists"}], (
        "the data node may be tainted; the bytes are there and nowhere else")
    assert manifest["spec"]["backoffLimit"] == 0, (
        "a retry re-runs a deletion that already happened")


def test_the_job_empties_the_directories_rather_than_removing_them():
    """A mount point that vanished would leave the next setup writing to a plain directory
    over a disk nobody noticed had gone."""
    manifest = data_purge.purge_manifest("default", ["/media/data/store"])
    script = manifest["spec"]["template"]["spec"]["containers"][0]["command"][-1]

    assert "rm -rf /purge/0/" in script
    assert "rmdir" not in script and "rm -rf /purge/0 " not in script


def test_an_unsafe_path_never_reaches_a_manifest():
    with pytest.raises(ValueError):
        data_purge.purge_manifest("default", ["/media/data/store", "/"])
