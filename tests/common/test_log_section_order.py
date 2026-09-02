# Copyright (C) 2026 Frederik Pasch
# SPDX-License-Identifier: Apache-2.0
"""The assembled campaign log may only ever grow at its end.

A reader streams it by byte offset, so bytes inserted ahead of a section it has already
consumed are bytes it can never be shown. A fixed phase order breaks that as soon as a
phase runs twice: postprocessing sits before share in such a list, so a postprocess
retriggered on a campaign that had already been shared inserted its whole section in the
middle of the stream. It was recorded correctly and published live, and the view stayed
frozen on the last line of the share that preceded it.

The order therefore cannot be a list of phases. It is the order the work happened in: the
head phases (which run once), then each finished run of a repeatable phase by the sequence
it was given, then the one still running -- always last, because it is the only one whose
bytes are still arriving.
"""

from robovast.common.campaign_logs import (assemble_log, next_section_seq,
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
    # And the live one is last, which is what makes the stream append-only.
    assert order[-1] == ("POSTPROCESSING", "postprocessing.log")


def test_the_stream_grows_only_at_its_end_across_a_rerun():
    """The property, checked as bytes: what a reader consumed before a rerun must still be
    a prefix of what it reads after one. This is the assertion the old order fails."""
    files = {"build.log": b"built\n", "controller.log": b"ran\n",
             section_name(1, "postprocessing.log"): b"first postprocess\n",
             section_name(2, "share.log"): b"shared\n"}
    before, offset, _ = assemble_log(files.get, 0,
                                     sections=ordered_sections(list(files)))

    # A postprocess is retriggered and starts writing.
    files["postprocessing.log"] = b"staging...\n"
    after, _, _ = assemble_log(files.get, 0, sections=ordered_sections(list(files)))

    assert after.startswith(before), "the stream must remain an extension of itself"
    # And polling from where the reader stopped yields exactly the new lines.
    tail, _, _ = assemble_log(files.get, offset,
                              sections=ordered_sections(list(files)))
    assert "staging..." in tail


def test_a_campaign_recorded_before_archiving_reads_as_it_always_did():
    """No archived sections: the head, then the repeatable phases in their fixed order.
    Every campaign already on disk is this shape and must keep working."""
    order = ordered_sections(["build.log", "plugin_install.log", "variation.log",
                              "controller.log", "postprocessing.log", "share.log"])

    assert [banner for banner, _name in order] == [
        "BUILD", "PLUGIN INSTALL", "VARIATION", "RUN", "POSTPROCESSING", "SHARE"]


def test_a_phase_that_never_ran_contributes_no_section():
    order = ordered_sections(["build.log", "controller.log"])

    assert [name for _banner, name in order] == ["build.log", "controller.log"]


def test_sections_are_ordered_by_sequence_not_by_name():
    """Ten runs, so a lexical sort would put 10 before 2. The sequence is the order."""
    names = [section_name(n, "postprocessing.log") for n in range(1, 11)]
    order = ordered_sections(["build.log"] + list(reversed(names)))

    assert [name for _banner, name in order][1:] == names


def test_an_unrecognised_file_is_ignored():
    """This decides what a byte offset means. A stray file that shifted it would corrupt
    every reader's position, so anything unknown is left out rather than appended."""
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
