# Copyright (C) 2026 Frederik Pasch
# SPDX-License-Identifier: Apache-2.0
"""A run whose recorder restarted mid-trial holds two bags, and one of them is the run.

Converting the last attempt is the whole point: everything else in a run directory exists
once and belongs to that attempt, so converting an earlier bag would put another attempt's
trajectory under this run's verdict. Dropping both -- which is what refusing the directory
amounts to -- loses the trial that did run.
"""

from robovast.results_processing import campaign_ingest
from robovast.results_processing.data.rosbags_common import (BAG_ATTEMPTS_CSV, bag_attempts_note,
                                                            find_rosbags, resolve_bag_attempts,
                                                            write_bag_attempts)

METADATA = """rosbag2_bagfile_information:
  version: 5
  storage_identifier: mcap
  duration:
    nanoseconds: 12000000000
  starting_time:
    nanoseconds_since_epoch: {start}
  message_count: 42
  files:
    - path: rosbag2_0.mcap
      starting_time:
        nanoseconds_since_epoch: {start}
"""


def _bag(root, rel, start=None):
    """A bag directory at *rel*, with a sidecar when *start* is given (else unfinalized)."""
    bag = root / rel
    bag.mkdir(parents=True)
    if start is not None:
        (bag / "metadata.yaml").write_text(METADATA.format(start=start), encoding="utf-8")
    return bag


def _rel(root, paths):
    return sorted(str(p)[len(str(root)) + 1:] for p in paths)


def test_the_last_attempt_is_converted_and_the_others_are_not(tmp_path):
    _bag(tmp_path, "goal-1/0/rosbag2", start=1_789_000_000_000_000_000)
    _bag(tmp_path, "goal-1/1/rosbag2", start=1_789_000_100_000_000_000)
    _bag(tmp_path, "goal-2/0/rosbag2_2026_07_15-10_30_00", start=1_789_000_200_000_000_000)
    last = _bag(tmp_path, "goal-2/0/rosbag2_2026_07_15-10_34_00",
                start=1_789_000_300_000_000_000)

    reported = []
    found = find_rosbags(str(tmp_path), on_multiple_attempts=reported.extend)

    assert _rel(tmp_path, found) == ["goal-1/0/rosbag2", "goal-1/1/rosbag2",
                                     "goal-2/0/rosbag2_2026_07_15-10_34_00"], \
        "every run converts, the repeat one from its last attempt"
    assert len(reported) == 1
    attempts = reported[0]
    assert attempts.run_dir == str(tmp_path / "goal-2" / "0")
    assert attempts.converted == str(last)
    assert _rel(tmp_path, attempts.superseded) == ["goal-2/0/rosbag2_2026_07_15-10_30_00"]
    assert attempts.dated_by == "metadata"


def test_the_sidecar_outranks_the_recorded_name(tmp_path):
    """The name is the recorder's local clock; the sidecar is the bag's own epoch start.

    They can disagree -- a tree copied across a zone, a clock stepped between attempts --
    and the bag's own record of when it started is the one that cannot be re-derived wrong.
    """
    earlier_name = _bag(tmp_path, "goal-1/0/rosbag2_2026_07_15-10_30_00",
                        start=1_789_000_900_000_000_000)
    _bag(tmp_path, "goal-1/0/rosbag2_2026_07_15-11_30_00", start=1_789_000_100_000_000_000)

    attempts = resolve_bag_attempts(str(tmp_path / "goal-1" / "0"),
                                    [str(p) for p in (tmp_path / "goal-1" / "0").iterdir()])

    assert attempts.converted == str(earlier_name)
    assert attempts.dated_by == "metadata"


def test_an_unfinalized_last_attempt_is_still_the_run(tmp_path):
    """The last attempt's recorder died before writing a sidecar, so it has no start time.

    It is still this run's trial, and the earlier bag is still another attempt's. The name
    orders them; the unreadable bag is then reported as unreadable by the conversion, and
    the run's data is genuinely missing -- which is the true answer, not the earlier bag.
    """
    _bag(tmp_path, "goal-1/0/rosbag2_2026_07_15-10_30_00", start=1_789_000_200_000_000_000)
    unfinalized = _bag(tmp_path, "goal-1/0/rosbag2_2026_07_15-10_34_00")

    reported = []
    found = find_rosbags(str(tmp_path), on_multiple_attempts=reported.extend)

    assert found == [str(unfinalized)]
    assert reported[0].dated_by == "name"


def test_a_half_written_sidecar_still_dates_its_bag(tmp_path):
    """The bag that matters here is one a restart cut off, so its sidecar can be truncated
    mid-document. A start time that is present is read; a YAML parse would refuse the file.
    """
    bag = _bag(tmp_path, "goal-1/0/rosbag2_2026_07_15-10_30_00",
               start=1_789_000_200_000_000_000)
    text = (bag / "metadata.yaml").read_text(encoding="utf-8")
    (bag / "metadata.yaml").write_text(text[:text.index("message_count")] + "  mess",
                                       encoding="utf-8")
    _bag(tmp_path, "goal-1/0/rosbag2_2026_07_15-10_34_00", start=1_789_000_300_000_000_000)

    attempts = resolve_bag_attempts(str(tmp_path / "goal-1" / "0"),
                                    [str(p) for p in (tmp_path / "goal-1" / "0").iterdir()])

    assert attempts.dated_by == "metadata", "the truncated sidecar still said when it began"
    assert attempts.converted.endswith("10_34_00")


def test_a_bag_that_cannot_be_dated_leaves_only_that_run_unconverted(tmp_path):
    """A stray unstamped bag with no sidecar beside a stamped one: nothing says which came
    last, and picking anyway would be data that looks right. The rest of the tree converts.
    """
    _bag(tmp_path, "goal-1/0/rosbag2", start=1_789_000_000_000_000_000)
    _bag(tmp_path, "goal-2/0/rosbag2")
    _bag(tmp_path, "goal-2/0/rosbag2_2026_07_15-10_34_00")

    reported = []
    found = find_rosbags(str(tmp_path), on_multiple_attempts=reported.extend)

    assert _rel(tmp_path, found) == ["goal-1/0/rosbag2"]
    assert reported[0].converted is None
    assert reported[0].dated_by == ""
    assert len(reported[0].superseded) == 2
    note = bag_attempts_note(reported[0], str(tmp_path))
    assert "Nothing converted from this run" in note


def test_the_note_names_the_bag_a_reader_will_be_looking_at(tmp_path):
    _bag(tmp_path, "goal-1/0/rosbag2_2026_07_15-10_30_00", start=1_789_000_200_000_000_000)
    _bag(tmp_path, "goal-1/0/rosbag2_2026_07_15-10_34_00", start=1_789_000_300_000_000_000)

    reported = []
    find_rosbags(str(tmp_path), on_multiple_attempts=reported.extend)
    note = bag_attempts_note(reported[0], str(tmp_path))

    assert "goal-1/0" in note
    assert "converting rosbag2_2026_07_15-10_34_00" in note
    assert "rosbag2_2026_07_15-10_30_00" in note


class _RecordingSink:
    """A sink that keeps what was written, like the ingest's real one would store it."""

    def __init__(self):
        self.writes = {}

    def write(self, table, rows, context=None, types=None, source=""):  # noqa: D102
        rows = list(rows)
        self.writes.setdefault(table, []).extend(rows)
        return len(rows)


def test_the_choice_is_the_runs_own_data(tmp_path):
    """The record is a CSV in the run directory, so the campaign ingest keys it on the run:
    "which runs recorded twice, and which bag are their metrics from" is a query, not a
    grep through one step's log.
    """
    run_dir = tmp_path / "goal-1" / "0"
    _bag(tmp_path, "goal-1/0/rosbag2_2026_07_15-10_30_00", start=1_789_000_200_000_000_000)
    _bag(tmp_path, "goal-1/0/rosbag2_2026_07_15-10_34_00", start=1_789_000_300_000_000_000)
    attempts = resolve_bag_attempts(str(run_dir), [str(p) for p in run_dir.iterdir()])

    path = write_bag_attempts(str(run_dir), attempts)
    assert path.endswith(BAG_ATTEMPTS_CSV)

    sink = _RecordingSink()
    written = campaign_ingest.ingest_run(sink, run_dir, "goal-1", 0)

    assert written == {"rosbag_attempts": 2}
    rows = {row["bag"]: row for row in sink.writes["rosbag_attempts"]}
    assert rows["rosbag2_2026_07_15-10_34_00"]["role"] == "converted"
    assert rows["rosbag2_2026_07_15-10_30_00"]["role"] == "superseded"
    assert rows["rosbag2_2026_07_15-10_34_00"]["start_time_ns"] == "1789000300000000000"


def test_rewriting_the_record_does_not_double_it(tmp_path):
    """Postprocessing is re-run over directories that already have results."""
    run_dir = tmp_path / "goal-1" / "0"
    _bag(tmp_path, "goal-1/0/rosbag2_2026_07_15-10_30_00", start=1_789_000_200_000_000_000)
    _bag(tmp_path, "goal-1/0/rosbag2_2026_07_15-10_34_00", start=1_789_000_300_000_000_000)
    attempts = resolve_bag_attempts(str(run_dir), [str(p) for p in run_dir.iterdir()])

    write_bag_attempts(str(run_dir), attempts)
    path = write_bag_attempts(str(run_dir), attempts)

    assert len(open(path, encoding="utf-8").read().strip().splitlines()) == 3
