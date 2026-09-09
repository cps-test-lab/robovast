# Copyright (C) 2026 Frederik Pasch
# SPDX-License-Identifier: Apache-2.0
"""One run left holding two bags must not cost the campaign its other runs."""

import pytest

from robovast.results_processing.data.rosbags_common import find_rosbags


def _tree(tmp_path, *rels):
    for rel in rels:
        (tmp_path / rel).mkdir(parents=True)
    return tmp_path


def test_every_unambiguous_run_still_converts(tmp_path):
    """The reported case: a container restarted mid-record leaves a second bag in one run
    directory. The scan runs in a worker pool, so raising there ended the whole walk and
    the campaign got no topic extraction at all -- for the sake of one run.
    """
    root = _tree(tmp_path,
                 "goal-1/0/rosbag2", "goal-1/1/rosbag2",
                 "goal-2/0/rosbag2", "goal-2/0/rosbag2_2026_07_15-10_30_00")
    seen = {}
    found = find_rosbags(str(root), on_conflict=seen.update)

    # Both unambiguous runs are still there.
    assert sorted(f[len(str(root)) + 1:] for f in found) == [
        "goal-1/0/rosbag2", "goal-1/1/rosbag2"]
    # And the one that is not is named, rather than left to be inferred from a gap.
    assert list(seen) == [str(root / "goal-2" / "0")]
    assert seen[str(root / "goal-2" / "0")] == [
        "rosbag2", "rosbag2_2026_07_15-10_30_00"]


def test_with_nowhere_to_report_it_still_refuses(tmp_path):
    """A caller that passes no handler keeps the old, loud behaviour.

    Returning fewer bags than the tree holds, silently, would be a worse failure than the
    one being fixed: the campaign would look converted and be missing a run.
    """
    root = _tree(tmp_path, "goal-2/0/rosbag2", "goal-2/0/rosbag2_2026_07_15-10_30_00")
    with pytest.raises(ValueError, match="Ambiguous rosbag layout"):
        find_rosbags(str(root))


def test_the_refusal_says_how_many_directories_are_affected(tmp_path):
    """A tree with several conflicts reports the count, not just the first one it met."""
    root = _tree(tmp_path,
                 "goal-1/0/rosbag2", "goal-1/0/rosbag2_2026_07_15-10_30_00",
                 "goal-2/0/rosbag2", "goal-2/0/rosbag2_2026_07_15-11_30_00")
    with pytest.raises(ValueError, match="1 other directory"):
        find_rosbags(str(root))
