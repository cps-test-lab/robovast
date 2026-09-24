# Copyright (C) 2026 Frederik Pasch
# SPDX-License-Identifier: Apache-2.0
"""The campaign log may only ever grow at its end.

A reader holds a cursor into each phase file and continues from it, so rows inserted ahead of a
file it has already read are rows it can never be shown. A fixed phase order breaks that as
soon as a phase runs twice: postprocessing sits before share in such a list, so a postprocess
retriggered on a campaign that had already been shared would land in the middle of the log.

The order therefore cannot be a list of phases. It is the order the work happened in: the
head phases (which run once), then each finished run of a repeatable phase by the sequence
it was given, then the one still running -- always last, because it is the only one whose
rows are still arriving.
"""

from robovast.common.campaign_logs import (disk_section_names, next_section_seq,
                                           ordered_sections, section_name)


def test_a_repeated_phase_lands_after_the_one_that_followed_it_the_first_time():
    """The bug, stated as an order. Postprocess, share, postprocess again: the second
    postprocess must come after the share, not back in the slot the first one used."""
    order = ordered_sections([
        "build.log", "variation.log", "controller.log",
        section_name(1, "postprocessing.log"),
        section_name(2, "share.log"),
        "postprocessing.log",          # the run happening now
    ])

    assert [banner for banner, _name in order] == [
        "BUILD", "VARIATION", "RUN", "POSTPROCESSING", "SHARE", "POSTPROCESSING"]
    # And the live one is last, which is what makes the log append-only.
    assert order[-1] == ("POSTPROCESSING", "postprocessing.log")


def test_a_rerun_lands_after_everything_already_ordered():
    """What a reader had before a rerun is a prefix of what it has after one."""
    names = ["build.log", "controller.log", section_name(1, "postprocessing.log"),
             section_name(2, "share.log")]
    before = ordered_sections(names)
    after = ordered_sections(names + ["postprocessing.log"])
    assert after[:len(before)] == before
    assert after[-1] == ("POSTPROCESSING", "postprocessing.log")


def test_a_campaign_recorded_before_archiving_reads_as_it_always_did():
    """No archived sections: the head, then the repeatable phases in their fixed order.
    Every campaign already on disk is this shape and must keep working."""
    order = ordered_sections(["import.log", "build.log", "plugin_install.log",
                              "variation.log", "controller.log", "postprocessing.log",
                              "share.log"])

    assert [banner for banner, _name in order] == [
        "IMPORT", "BUILD", "PLUGIN INSTALL", "VARIATION", "RUN", "POSTPROCESSING", "SHARE"]


def test_a_phase_that_never_ran_contributes_no_section():
    order = ordered_sections(["build.log", "controller.log"])

    assert [name for _banner, name in order] == ["build.log", "controller.log"]


def test_sections_are_ordered_by_sequence_not_by_name():
    """Ten runs, so a lexical sort would put 10 before 2. The sequence is the order."""
    names = [section_name(n, "postprocessing.log") for n in range(1, 11)]
    order = ordered_sections(["build.log"] + list(reversed(names)))

    assert [name for _banner, name in order][1:] == names


def test_an_unrecognised_file_is_ignored():
    """This decides where every reader's row sequence continues. A stray file that shifted
    it would move every reader's position, so anything unknown is left out rather than
    appended."""
    order = ordered_sections(["build.log", "sections/0001-postprocessing.log",
                              "sections/nonsense.log", "notes.txt",
                              "sections/0002-unknown_phase.log"])

    assert [name for _banner, name in order] == [
        "build.log", "sections/0001-postprocessing.log"]


def test_the_next_sequence_follows_the_highest_used():
    assert next_section_seq([]) == 1
    assert next_section_seq(["sections/0001-postprocessing.log"]) == 2
    # Across phases, because the sequence orders the campaign and not one phase.
    assert next_section_seq(["sections/0001-postprocessing.log",
                             "sections/0002-share.log"]) == 3
    # Unrelated names do not consume a number.
    assert next_section_seq(["build.log", "sections/0007-share.log", "notes.txt"]) == 8


def test_overlapping_listings_may_simply_be_concatenated():
    """A reader unions several listings of one campaign (two local roots, or a local root
    and the store). A name repeated there must not repeat its section: a section counted
    twice is rows inserted mid-stream on the next read."""
    names = ["build.log", section_name(1, "postprocessing.log"), "share.log"]
    order = ordered_sections(names + names)

    assert [name for _banner, name in order] == names


def test_a_disk_listing_names_sections_the_way_the_order_reads_them(tmp_path):
    """The two must agree on the form of a name, or an archived section is invisible to the
    reader that just found it on disk."""
    exec_dir = tmp_path / "_execution"
    (exec_dir / "sections").mkdir(parents=True)
    (exec_dir / "controller.log").write_bytes(b"ran\n")
    (exec_dir / "sections" / "0001-share.log").write_bytes(b"exported\n")

    names = disk_section_names(tmp_path)

    assert sorted(names) == ["controller.log", "sections/0001-share.log"]
    assert ordered_sections(names) == [("RUN", "controller.log"),
                                       ("SHARE", "sections/0001-share.log")]


def test_a_campaign_with_nothing_on_disk_lists_nothing(tmp_path):
    """The normal case for a reader with no local copy, not an error."""
    assert disk_section_names(tmp_path / "never-here") == []
