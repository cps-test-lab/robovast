# Copyright (C) 2026 Frederik Pasch
# SPDX-License-Identifier: Apache-2.0
"""The upload bar's denominator must be the numerator's total.

``campaign_source_bytes`` is walked ahead of a streamed upload to get a total, and
``_make_filter``'s ``on_member`` counts the same bytes off as the archive is built. They
are two different walks of the same tree, so a rule that holds in one and not the other
(an exclude that prunes on one side only, a symlink followed on one side only) puts the
bar somewhere other than 100% at the end. These tests pin them to each other.
"""

import os

from robovast.execution import campaign_archive


def _campaign(tmp_path):
    root = tmp_path / "camp-2026-01-01-000000"
    (root / "cfg" / "run-0").mkdir(parents=True)
    (root / "_execution").mkdir()
    (root / "cfg" / "run-0" / "test.xml").write_bytes(b"x" * 500)
    (root / "cfg" / "run-0" / "bag.mcap").write_bytes(b"y" * 4096)
    (root / "_execution" / "outcome.json").write_bytes(b"{}" * 8)
    return root


def _counted(root, exclude=campaign_archive.DEFAULT_EXCLUDE):
    """Bytes the archiver actually reports through ``on_member``."""
    seen = []
    with campaign_archive.campaign_tar_stream(
            str(root), exclude=exclude, on_member=seen.append) as stream:
        while stream.read(65536):
            pass
    return sum(seen)


def test_walk_matches_what_the_archiver_counts(tmp_path):
    root = _campaign(tmp_path)
    assert campaign_archive.campaign_source_bytes(str(root)) == _counted(root)


def test_an_excluded_subtree_is_dropped_by_both(tmp_path):
    root = _campaign(tmp_path)
    cache = root / "cfg" / ".cache"
    cache.mkdir()
    (cache / "hashes.json").write_bytes(b"z" * 9999)

    total = campaign_archive.campaign_source_bytes(str(root))
    assert total == _counted(root)
    # And the cache really is out: it is rebuildable, and it is 10 KB of the fixture.
    assert total == campaign_archive.campaign_source_bytes(str(root), exclude=())- 9999


def test_a_symlink_is_a_member_not_a_path_to_follow(tmp_path):
    """``dereference=False`` means the archiver stores the link and does not recurse.

    A walk that followed it would count the target's bytes twice and leave the bar
    stuck short of 100% — which is exactly what the ``<config>/<run>/job`` links would
    do to every campaign this runs on.
    """
    root = _campaign(tmp_path)
    (root / "_jobs" / "job-0").mkdir(parents=True)
    (root / "_jobs" / "job-0" / "big.log").write_bytes(b"w" * 8192)
    os.symlink(root / "_jobs" / "job-0", root / "cfg" / "run-0" / "job")

    assert campaign_archive.campaign_source_bytes(str(root)) == _counted(root)
