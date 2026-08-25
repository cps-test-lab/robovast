# Copyright (C) 2026 Frederik Pasch
# SPDX-License-Identifier: Apache-2.0
"""Where the resource monitor writes, on both lanes, is a contract.

``ResourceUsage`` reads ``_jobs/[<batch>/]job-N/resource_usage_<container>.csv`` and maps
each filename back to a container name. Nothing in the running system checks that the
monitor still writes there under that name: the postprocessing step just finds no files and
reports a campaign that recorded nothing, which is indistinguishable from a fleet of
sidecars without psutil.

So this reads the shipped scripts and the two lane builders and pins the three facts the
plugin depends on: the filename pattern, that it is written under ``$OUTPUT_DIR``, and that
both lanes point ``OUTPUT_DIR`` at the job's artifact directory.
"""

from importlib.resources import files

import pytest

from robovast.common.execution import job_artifact_rel
from robovast.results_processing import run_slices

SCRIPTS = ("entrypoint.sh", "secondary_entrypoint.sh")


def _script(name: str) -> str:
    return files("robovast.execution.data").joinpath(name).read_text()


def _source(module) -> str:
    import inspect
    return inspect.getsource(module)


@pytest.mark.parametrize("name", SCRIPTS)
def test_the_monitor_writes_into_the_jobs_artifact_dir(name):
    """Not the run dir. A job serves several runs, so the sample stream is a job artifact."""
    script = _script(name)
    assert 'monitor_resources.py "${OUTPUT_DIR}/resource_usage_' in script


def test_the_main_container_names_its_file_main_and_a_sidecar_names_its_own():
    """The two halves of the naming ``run_slices.container_of`` inverts."""
    assert '${OUTPUT_DIR}/resource_usage_main.csv' in _script("entrypoint.sh")
    assert ('${OUTPUT_DIR}/resource_usage_${CONTAINER_NAME}.csv'
            in _script("secondary_entrypoint.sh"))


@pytest.mark.parametrize("name,container", [
    ("resource_usage_main.csv", "robovast"),
    ("resource_usage_sut.csv", "sut"),
    ("resource_usage_simulation.csv", "simulation"),
])
def test_every_name_the_scripts_can_produce_maps_back_to_a_container(name, container):
    assert run_slices.container_of(name) == container


def test_both_lanes_point_output_dir_at_the_same_job_artifact_path():
    """``job_artifact_rel`` is the one definition of the ``_jobs/`` layout, and the plugin
    resolves through it. A lane that computed its own path would put a whole campaign's
    samples somewhere the manifest does not point."""
    from robovast.execution.cluster_execution import kubernetes_backend
    from robovast.execution.execution_utils import execute_local

    for module in (execute_local, kubernetes_backend):
        source = _source(module)
        assert "job_artifact_rel(" in source, f"{module.__name__} derives its own _jobs path"
        assert "OUTPUT_DIR" in source

    # And that definition is what the reader inverts.
    assert job_artifact_rel(3, "batch-0") == "batch-0/job-3"
    assert job_artifact_rel(3) == "job-3"
