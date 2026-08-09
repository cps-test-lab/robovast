# Copyright (C) 2026 Frederik Pasch
# SPDX-License-Identifier: Apache-2.0
"""The merged run log: one row per event, whichever source (or sources) carried it.

The numbers in the docstrings come from a real three-container campaign,
``basicnav-sidecar-2026-08-07-17213653``: 521 rosout rows and 526 stdout records merge to
574 rows with 473 matched and **zero** events present twice.
"""

import dataclasses

import pytest

from robovast.results_processing import run_log
from robovast.results_processing.clock_map import NO_CLOCK_MAP, ClockMap

# A nav2 line as it really reaches system.log: the scenario relays it, the launch container
# tags it, and only then comes the node's own stamp.
_RELAYED_ERROR = (
    "[INFO] [1786116213.878257908] [action.976a1f0d]: "
    "[component_container_isolated-2] [ERROR] [1786116213.809044763] "
    "[lifecycle_manager_localization]: CRITICAL FAILURE: SERVER map_server IS DOWN")


def _rosout_rows(tmp_path, rows) -> str:
    path = tmp_path / "rosout.csv"
    header = "timestamp,stamp,level,level_name,name,msg,file,function,line\n"
    body = "".join(
        f"{recv},{stamp},{level_num},{level},{name},\"{msg}\",f.cpp,fn,1\n"
        for recv, stamp, level_num, level, name, msg in rows)
    path.write_text(header + body)
    return str(path)


# -- pass 1: container logs ---------------------------------------------------


def test_the_container_comes_from_the_filename():
    assert run_log.container_of("system.log") == run_log.MAIN_CONTAINER
    assert run_log.container_of("system_sut.log") == "sut"
    assert run_log.container_of("system_simulation.log") == "simulation"
    assert run_log.container_of("rosout.csv") is None


def test_the_innermost_node_and_level_are_taken_not_the_relays():
    """Attributing to the relay would file every nav2 error under the scenario runner, and
    reading the outer INFO as the level would hide it entirely."""
    (rec,) = run_log.parse_container_log([_RELAYED_ERROR], "sut")
    assert rec.node == "lifecycle_manager_localization"
    assert rec.level == "ERROR"
    assert rec.severity == "error"
    assert rec.wall_ts == 1786116213.809044763
    assert rec.time_source == run_log.TIME_EXACT
    assert rec.message == "CRITICAL FAILURE: SERVER map_server IS DOWN"


def test_a_traceback_is_one_event_not_forty():
    """Unstamped continuation lines belong to the event above them. Emitting each as its own
    row would bury the exception under its own stack trace in any severity filter."""
    lines = [
        "[ERROR] [100.5] [rst]: Traceback (most recent call last):",
        '  File "/ws/thing.py", line 3, in <module>',
        "    raise ValueError('nope')",
        "ValueError: nope",
        "[INFO] [100.6] [rst]: carrying on",
    ]
    records = run_log.parse_container_log(lines, "main")
    assert len(records) == 2
    assert records[0].message.count("\n") == 3
    assert "ValueError: nope" in records[0].message
    assert records[1].message == "carrying on"


def test_lines_before_the_first_stamp_report_no_time_rather_than_borrowing_one():
    """The entrypoint's bash lines come before anything stamped. Giving them the *next*
    line's time would claim the container booted at whatever second ROS came up."""
    records = run_log.parse_container_log(
        ["Running as UID: 1000, GID: 1000...", "[INFO] [100.5] [rst]: up"], "main")
    assert len(records) == 2
    assert records[0].wall_ts is None
    assert records[0].time_source == run_log.TIME_NONE
    assert records[1].wall_ts == 100.5


def test_an_unstamped_line_cannot_be_joined_on():
    (rec,) = run_log.parse_container_log(["Entrypoint script initialized"], "main")
    assert rec.join_key is None


# -- pass 2/3: the join -------------------------------------------------------


def test_a_rosout_row_and_its_stdout_twin_become_one_row(tmp_path):
    """The whole point: /rosout and the container's stdout carry the same event, so
    concatenating them would report most of a run twice."""
    path = _rosout_rows(tmp_path, [
        (1786116213.809163, 1786116213.8090448, 40, "ERROR",
         "lifecycle_manager_localization", "CRITICAL FAILURE: SERVER map_server IS DOWN"),
    ])
    stats = run_log.MergeStats()
    merged = run_log.merge_records(
        run_log.parse_container_log([_RELAYED_ERROR], "sut"),
        run_log.read_rosout(path), stats)
    assert len(merged) == 1
    assert stats.matched == 1


def test_the_join_uses_the_producers_stamp_not_the_bags_receive_time(tmp_path):
    """The measured failure: keying rosout on ``timestamp`` (when the recorder *saw* the
    message) matched 0 of 521 rows, because the transport delay puts every pair on a
    different nanosecond. ``stamp`` is what the producing node's clock said — the same
    number rcutils printed into stdout."""
    stamp = 1786116213.809044763
    path = _rosout_rows(tmp_path, [
        (stamp + 0.0002, stamp, 40, "ERROR", "lifecycle_manager_localization",
         "CRITICAL FAILURE: SERVER map_server IS DOWN")])
    (rosout_rec,) = run_log.read_rosout(path)
    assert rosout_rec.wall_ts == stamp, "the receive time must not become the row's time"
    stats = run_log.MergeStats()
    run_log.merge_records(run_log.parse_container_log([_RELAYED_ERROR], "sut"),
                          [rosout_rec], stats)
    assert stats.matched == 1


def test_the_container_of_a_rosout_row_comes_from_its_twin(tmp_path):
    """/rosout names the node but never the container it ran in, and the container is what a
    reader filters by. The stdout twin is the only place that fact exists."""
    path = _rosout_rows(tmp_path, [
        (1786116213.809163, 1786116213.8090448, 40, "ERROR",
         "lifecycle_manager_localization", "CRITICAL FAILURE: SERVER map_server IS DOWN")])
    merged = run_log.merge_records(
        run_log.parse_container_log([_RELAYED_ERROR], "sut"),
        run_log.read_rosout(path))
    assert merged[0].container == "sut"
    assert merged[0].source == run_log.SRC_ROSOUT
    assert merged[0].file == "f.cpp"  # rosout's structured fields survive the join


def test_a_rosout_row_with_no_evidence_at_all_keeps_an_unknown_container(tmp_path):
    """A node whose container's stdout was not captured *anywhere* is still part of the log;
    claiming a container for it would be a guess. No twin and no other line from that node
    means nothing places it."""
    path = _rosout_rows(tmp_path, [
        (100.0, 100.0, 20, "INFO", "rosbag2_recorder", "Press SPACE for pausing/resuming")])
    merged = run_log.merge_records([], run_log.read_rosout(path))
    assert merged[0].container == ""


def test_a_rosout_row_with_no_twin_takes_the_container_its_own_node_was_seen_in(tmp_path):
    """`ros2 bag record`'s output is relayed, so a write that carried no newline leaves the
    next line's stamp unpeelable and that one row loses its twin -- while the node's other
    rows join normally. The node ran in one container either way, which is the only reason
    the blank can be filled without guessing.

    Measured: `rosbag2_recorder` is attributed 22 times and blank 11 times *in one run*.
    """
    stamp = 1786259468.903038464
    path = _rosout_rows(tmp_path, [
        (stamp + 0.0002, stamp, 20, "INFO", "rosbag2_recorder",
         "Press SPACE for pausing/resuming"),
        (200.0002, 200.0, 20, "INFO", "rosbag2_recorder", "Recording...")])
    # Only the second line kept a peelable stamp of its own, so only it finds a twin.
    stdout = run_log.parse_container_log(
        ["[INFO] [1786259468.904814817] [scenario_execution_ros]: stdin is not a terminal "
         f"device.[INFO] [{stamp}] [rosbag2_recorder]: Press SPACE for pausing/resuming",
         "[INFO] [200.0] [rosbag2_recorder]: Recording..."], "robovast")
    stats = run_log.MergeStats()
    merged = run_log.merge_records(stdout, run_log.read_rosout(path), stats)
    assert stats.matched == 1, "the mangled line must not find a twin"
    assert stats.node_attributed == 1
    by_msg = {r.message: r for r in merged if r.source == run_log.SRC_ROSOUT}
    assert by_msg["Press SPACE for pausing/resuming"].container == "robovast"


def test_campaign_totals_carry_every_counter_a_job_reported():
    """The summary is the regression signal, so a counter that does not reach the totals
    reports zero however much the merge did. Written out field by field, the fold silently
    omits any counter added to the class afterwards; asserted over the dataclass instead, so
    a counter added later is covered without anyone remembering to come back here."""
    job = run_log.MergeStats()
    for spec in dataclasses.fields(job):
        if spec.name == "containers":
            job.containers.append("sut")
        else:
            setattr(job, spec.name, 7)

    totals = run_log.MergeStats()
    totals.add_job(job)
    totals.add_job(job)

    for spec in dataclasses.fields(totals):
        if spec.name == "containers":
            assert totals.containers == ["sut"], "a container must not be counted twice"
        elif spec.name in run_log.MergeStats._PER_RUN_FIELDS:
            assert getattr(totals, spec.name) == 0, f"{spec.name} is the caller's to count"
        else:
            assert getattr(totals, spec.name) == 14, f"{spec.name} never reached the totals"


def test_a_node_seen_in_two_containers_stays_blank(tmp_path):
    """`entrypoint` really does run in every container, so its blank rows have no one answer.
    A filterable wrong container is worse than an honest blank."""
    path = _rosout_rows(tmp_path, [(300.0002, 300.0, 20, "INFO", "entrypoint", "Uploading")])
    stdout = (run_log.parse_container_log(["[INFO] [100.0] [entrypoint]: starting"], "sut")
              + run_log.parse_container_log(["[INFO] [101.0] [entrypoint]: starting"],
                                            "simulation"))
    stats = run_log.MergeStats()
    merged = run_log.merge_records(stdout, run_log.read_rosout(path), stats)
    assert stats.node_attributed == 0
    assert [r for r in merged if r.message == "Uploading"][0].container == ""


def test_repeats_pair_in_time_order(tmp_path):
    """A message logged 84 times must not collapse onto its first occurrence."""
    lines = [f"[WARN] [{100 + i}.0] [ctrl]: Passing new path to controller" for i in range(3)]
    path = _rosout_rows(tmp_path, [
        (100 + i + 0.0001, float(100 + i), 30, "WARN", "ctrl",
         "Passing new path to controller") for i in range(3)])
    stats = run_log.MergeStats()
    merged = run_log.merge_records(run_log.parse_container_log(lines, "sut"),
                                  run_log.read_rosout(path), stats)
    assert stats.matched == 3
    assert len(merged) == 3


def test_the_same_words_at_a_different_time_stay_two_events(tmp_path):
    path = _rosout_rows(tmp_path, [
        (100.0, 100.0, 30, "WARN", "ctrl", "the same words")])
    merged = run_log.merge_records(
        run_log.parse_container_log(["[WARN] [200.0] [ctrl]: the same words"], "sut"),
        run_log.read_rosout(path))
    assert len(merged) == 2


def test_a_stamp_of_zero_falls_back_to_the_receive_time(tmp_path):
    """A message published before its node's clock was ready carries stamp 0 — an absence,
    not an instant in 1970, which would sort the whole run behind it."""
    path = _rosout_rows(tmp_path, [(100.5, 0.0, 20, "INFO", "n", "early")])
    (rec,) = run_log.read_rosout(path)
    assert rec.wall_ts == 100.5


def test_rows_are_ordered_by_time(tmp_path):
    path = _rosout_rows(tmp_path, [(102.0, 102.0, 20, "INFO", "n", "later")])
    merged = run_log.merge_records(
        run_log.parse_container_log(["[INFO] [101.0] [m]: earlier"], "main"),
        run_log.read_rosout(path))
    assert [r.message for r in merged] == ["earlier", "later"]


# -- pass 4: sim time and the run window --------------------------------------


def _one_record(wall: float) -> list:
    return run_log.parse_container_log([f"[INFO] [{wall}] [n]: hello"], "main")


def test_sim_time_comes_from_the_clock_map():
    clock = ClockMap([(100.0, 0.0), (110.0, 13.69)])
    (row,) = run_log.rows_for_window(_one_record(105.0), clock)
    assert float(row["sim_time"]) == pytest.approx(6.845)


def test_a_line_outside_the_maps_range_has_no_sim_time_but_keeps_its_wall_time():
    """Boot and bring-up happen before the simulator publishes /clock. That output is real
    and must stay readable; what it does not have is a sim time."""
    clock = ClockMap([(100.0, 0.0), (110.0, 10.0)])
    (row,) = run_log.rows_for_window(_one_record(90.0), clock)
    assert row["sim_time"] == ""
    assert float(row["wall_ts"]) == 90.0


def test_with_no_clock_map_every_row_is_wall_only():
    (row,) = run_log.rows_for_window(_one_record(105.0), NO_CLOCK_MAP)
    assert row["sim_time"] == ""


def test_a_line_outside_the_runs_window_is_kept_and_flagged():
    """In a packed job the lines between two runs are the simulator being reset. They belong
    to some run, so they are attributed rather than dropped — and flagged, so a query can
    tell "during the trial" from "getting ready for it"."""
    records = _one_record(100.0) + _one_record(150.0)
    rows = run_log.rows_for_window(records, NO_CLOCK_MAP,
                                   start_epoch=120.0, end_epoch=200.0)
    assert [r["in_window"] for r in rows] == [0, 1]


def test_without_a_window_everything_counts_as_in_window():
    """A run that never wrote test.xml (killed mid-flight) has no window, and its log is the
    one most worth reading."""
    (row,) = run_log.rows_for_window(_one_record(100.0), NO_CLOCK_MAP)
    assert row["in_window"] == 1


def test_the_csv_has_a_header_even_when_a_run_logged_nothing(tmp_path):
    """So the table exists and the panel can say "no lines" instead of "no table"."""
    path = tmp_path / "run_log.csv"
    run_log.write_run_log(str(path), [])
    assert path.read_text().strip() == ",".join(run_log.FIELDNAMES)


def test_unstamped_lines_do_not_all_collapse_into_one_row():
    """An rst run's whole container log is unstamped bash output: 46 lines are 46 things
    that happened. Folding each into the previous one made the entire log a single row —
    caught by running the merge over a real non-ROS job."""
    lines = ["Running as UID: 1000, GID: 1000...",
             "Setting up ROS2 environment...",
             "Collecting system information..."]
    records = run_log.parse_container_log(lines, "main")
    assert len(records) == 3
    assert all(r.time_source == run_log.TIME_NONE for r in records)


def test_a_continuation_still_folds_under_a_stamped_line():
    """The distinction is what precedes the line: unstamped output after an *event* belongs
    to it (a traceback can be any number of lines), while unstamped output with no event
    before it stands alone.

    Getting this wrong in the other direction swallowed a whole log once: folding into an
    *unstamped* predecessor turned the entrypoint's 46 bash lines into a single row.
    """
    records = run_log.parse_container_log(
        ["bare line before anything stamped",
         "[ERROR] [100.5] [rst]: Traceback (most recent call last):",
         '  File "/ws/t.py", line 3',
         "ValueError: nope"], "main")
    assert len(records) == 2
    assert records[0].message == "bare line before anything stamped"
    assert records[1].message.count("\n") == 2
    assert "ValueError" in records[1].message


# -- unstamped output, bracketed rather than dropped ---------------------------
#
# Producers stamp their own lines now (the entrypoints, scenario-execution, rclpy), so a
# stamp is the normal case. These pin what happens to output that still has none -- a gz
# warning, a vanilla sidecar, a future tool nobody has written yet. The rule is that it is
# always readable and never silently gone.


def test_a_wholly_unstamped_log_is_still_every_line():
    """The no-swallow contract, for a producer that stamps nothing at all. There is no stamp
    to bracket against, so the rows have no time -- but there is one row per line, and the
    panel and search still see all of it. Folding these into one row (or dropping them) is
    the failure this guards."""
    lines = [f"plain line {i}" for i in range(5)]
    records = run_log.parse_container_log(lines, "sim")
    assert [r.message for r in records] == lines
    assert all(r.time_source == run_log.TIME_NONE for r in records)


def test_an_unstamped_tail_inherits_the_stamp_above_it():
    """A traceback's frames belong to the ERROR line that introduced them: one event."""
    records = run_log.parse_container_log(
        ["[ERROR] [100.0] [rst]: Traceback (most recent call last):",
         '  File "/ws/t.py", line 3',
         "ValueError: nope"], "main")
    assert len(records) == 1
    assert records[0].message.count("\n") == 2


# -- attributing a rosout row to its container --------------------------------


def test_the_main_container_is_named_what_the_live_log_calls_it():
    """`robovast`, not `main` and not `scenario`: the live job log tags its lines with the real
    runtime container name, and a campaign that reads as two different sets of containers
    depending on which surface you opened is worse than either name alone."""
    from robovast.common.log_tail import MAIN_CONTAINER
    assert run_log.container_of("system.log") == MAIN_CONTAINER == "robovast"


def _job(tmp_path, logs: dict) -> str:
    d = tmp_path / "logs"
    d.mkdir(exist_ok=True)
    for name, body in logs.items():
        (d / name).write_text(body)
    (d / "rosout.csv").write_text(
        "timestamp,stamp,level,level_name,name,msg,file,function,line\n"
        "200.0,200.0,20,INFO,recorder,no twin anywhere,f.cpp,fn,1\n")
    return str(tmp_path)


def test_a_single_container_campaign_attributes_every_row_to_it(tmp_path):
    """With one container there is only one place the output can have come from, so a rosout row
    with no stdout twin is attributed rather than left as "?"."""
    job = _job(tmp_path, {"system.log": "[INFO] [100.0] [rst]: only line\n"})
    records = run_log.collect_job_records(job, sole_container="robovast")
    assert {r.container for r in records} == {"robovast"}


def test_without_a_sole_container_an_untwinned_row_stays_unattributed(tmp_path):
    """The caller passes `sole_container` only when the campaign *declares* one. A wrong
    container is worse than a missing one, because it is filterable and believable."""
    job = _job(tmp_path, {"system.log": "[INFO] [100.0] [a]: main line\n"})
    records = run_log.collect_job_records(job)
    assert [r.container for r in records if r.message == "no twin anywhere"] == [""]


def test_a_vanilla_sidecar_cannot_steal_another_containers_rows(tmp_path):
    """The hole this guards: a sidecar may be a vanilla image that never runs RoboVAST's
    entrypoint, so it writes no ``system_<name>.log``. Counting log files would then call such a
    campaign single-container and label the sidecar's /rosout lines with the one that did log."""
    job = _job(tmp_path, {"system.log": "[INFO] [100.0] [a]: main line\n"})
    # The campaign declares three containers, so the caller passes no sole_container at all.
    records = run_log.collect_job_records(job, sole_container=None)
    assert [r.container for r in records if r.message == "no twin anywhere"] == [""]


def test_logs_from_several_containers_are_never_overridden(tmp_path):
    """Belt and braces: even given a sole_container, a job that plainly logged two containers
    keeps what the files said."""
    job = _job(tmp_path, {"system.log": "[INFO] [100.0] [a]: main line\n",
                          "system_sut.log": "[INFO] [101.0] [b]: sut line\n"})
    records = run_log.collect_job_records(job, sole_container="robovast")
    assert [r.container for r in records if r.message == "no twin anywhere"] == [""]
