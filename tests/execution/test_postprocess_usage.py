# Copyright (C) 2026 Frederik Pasch
# SPDX-License-Identifier: Apache-2.0
"""What the postprocessing pod cost: one row per step, from the sampler that already reads it.

The reading itself is ``monitor_resources``' and is tested with the sampler
(``test_resource_monitor_*``). What is tested here is the part that is new: that the figures
are taken **once** rather than sampled, that they reach the campaign, and that measuring a
step can never change that step's outcome.
"""

import csv
import subprocess

import pytest

from robovast.execution.cluster_execution import postprocess_job as pj
from robovast.execution.cluster_execution import postprocess_usage as pu
from robovast.execution.data import monitor_resources


def test_it_is_the_container_level_half_and_says_so():
    """``system_usage``, not ``resource_usage``.

    ``resource_usage`` is per *process* by contract and every reader aggregates it that way,
    so a container-level figure filed there is summed as though it were a process and shows
    up in the UI as a process nobody can find. This has no pid, so it is the other half --
    and the name is the only thing that tells a later reader which aggregation is legitimate.
    """
    assert pu.USAGE_REL.endswith("postprocess_system_usage.csv")


def test_the_record_lands_where_the_upload_already_goes():
    """``_execution/`` is uploaded wholesale by the host step, so these figures need no
    upload of their own -- and a file under a *run* directory would be swept into that run's
    metric tables and read as something the run itself consumed."""
    assert pu.USAGE_REL.startswith("_execution/")


def test_the_columns_are_the_samplers_own(tmp_path):
    """Not a second column list.

    ``system_usage`` is column-generic precisely so that adding a counter is a change to the
    sampler and to nothing else; a copy of its column names here would be free to disagree
    about names and units, and the postprocessing figures would stop being comparable with a
    trial's.
    """
    probes = monitor_resources.start_probes()
    expected = [monitor_resources.ONCE_LABEL_COLUMN] + [
        c for _, cols in probes for c in cols]
    row = pu.record(str(tmp_path), "convert")
    assert list(row) == expected
    assert row["step"] == "convert"


def test_each_step_appends_its_own_row(tmp_path):
    """Three containers in sequence, so no one of them can write the whole file."""
    for step in ("stage", "convert", "host"):
        pu.record(str(tmp_path), step)
    with open(tmp_path / "_execution" / "postprocess_system_usage.csv",
              encoding="utf-8") as fh:
        rows = list(csv.DictReader(fh))
    assert [r["step"] for r in rows] == ["stage", "convert", "host"]


def test_recording_never_fails_the_step_it_measures(tmp_path):
    """This measures the postprocessing; it is not part of it. A campaign whose results were
    derived correctly must not fail because the record of what that cost could not be
    written."""
    blocked = tmp_path / "nope"
    blocked.write_text("I am a file, not a directory")
    assert pu.record(str(blocked), "host") == {}


# -- the one-shot mode on the sampler ------------------------------------------------


def test_one_shot_writes_a_header_once_and_a_row_per_call(tmp_path):
    path = tmp_path / "usage.csv"
    monitor_resources.write_once(str(path), "stage")
    monitor_resources.write_once(str(path), "host")
    lines = path.read_text(encoding="utf-8").strip().splitlines()
    assert len(lines) == 3
    assert lines[0].startswith("step,")
    assert lines[1].startswith("stage,") and lines[2].startswith("host,")


def test_one_shot_takes_only_the_container_level_probes(tmp_path):
    """There is no per-process question here, and a per-process file would invite the
    aggregation ``resource_usage`` readers do to a table that is not per-process."""
    path = tmp_path / "usage.csv"
    monitor_resources.write_once(str(path), "convert")
    header = path.read_text(encoding="utf-8").splitlines()[0]
    assert "pid" not in header.split(",")
    assert "name" not in header.split(",")


def test_one_shot_needs_no_psutil(monkeypatch, tmp_path):
    """It runs in the campaign's own image, where psutil may not be installed.

    Which is why the sampler imports psutil inside the per-process loop rather than at the
    top: a top-level import would make the container-level counters unreadable in exactly
    the container that has no other way to report them.
    """
    import builtins  # noqa: PLC0415

    real_import = builtins.__import__

    def _refuse_psutil(name, *args, **kwargs):
        if name == "psutil":
            raise ImportError("no psutil in this image")
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", _refuse_psutil)
    monitor_resources.write_once(str(tmp_path / "usage.csv"), "convert")
    assert (tmp_path / "usage.csv").exists()


def test_the_one_shot_cli_reports_a_failure_without_failing(tmp_path, capsys):
    """``--once`` exits 0 even when it could not write: the caller is a step whose work is
    worth more than the record of what it cost."""
    blocked = tmp_path / "nope"
    blocked.write_text("not a directory")
    assert monitor_resources._run_once([str(blocked / "usage.csv"), "convert"]) == 0
    assert "could not record" in capsys.readouterr().err


def test_the_one_shot_cli_refuses_nothing_and_explains(capsys):
    assert monitor_resources._run_once([]) == 0
    assert "usage:" in capsys.readouterr().err


# -- the conversion container, which can import none of the above --------------------


def test_the_sampler_travels_to_the_conversion_container():
    """It is mounted, not reimplemented. The conversion container runs the campaign's own
    image and can import nothing of robovast, so without the sampler on ``/scripts`` there
    is no way for that step -- the expensive one -- to report what it cost."""
    payload = pj.scripts_configmap_manifest("camp-1", "ns")["data"]
    assert "monitor_resources.py" in payload
    assert "def write_once" in payload["monitor_resources.py"]


def test_the_conversion_records_its_usage_even_when_it_failed():
    """A conversion killed for exceeding its memory is exactly the case this record exists
    for, so the record sits outside the block whose failure it explains -- and cannot change
    the conversion's own exit status."""
    script = pj._conversion_script([{"plugins": [{"type": "to_csv"}]}], False,
                                   campaign_id="camp-1")
    assert subprocess.run(["bash", "-n"], input=script, text=True, check=False,
                          capture_output=True).returncode == 0
    assert script.index("tee -a") < script.index("monitor_resources.py --once")
    assert script.rstrip().endswith("exit $rc")


# -- how it reads in the log ---------------------------------------------------------


def test_the_summary_line_says_how_close_the_step_came():
    line = pu.summary_line({
        "step": "convert", "memory_peak": 3221225472, "memory_max": 4294967296,
        "cpu_usage_usec": 16300000, "nr_periods": 1000, "nr_throttled": 40})
    assert "convert used 3.00GiB peak of 4.00GiB" in line
    assert "16.3s cpu" in line
    assert "throttled 4.0% of enforcement periods" in line


def test_an_oom_kill_is_called_out_and_a_zero_is_not():
    """The normal case is zero, and saying so every time trains the reader to skip the line
    that matters."""
    base = {"step": "convert", "memory_peak": 1, "memory_max": 2}
    assert "OOM-KILLED" not in pu.summary_line(base | {"oom_kills": 0})
    assert "OOM-KILLED 2x" in pu.summary_line(base | {"oom_kills": 2})


@pytest.mark.parametrize("row", [
    {},
    {"step": "stage"},
    {"step": "stage", "memory_peak": "", "memory_max": "", "nr_periods": ""},
])
def test_the_summary_line_survives_what_a_kernel_did_not_report(row):
    """The sampler decides what a container can report, so a counter absent on some kernel
    must not turn a log line into an error."""
    assert pu.summary_line(row)
