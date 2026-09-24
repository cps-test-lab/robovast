# Copyright (C) 2026 Frederik Pasch
# SPDX-License-Identifier: Apache-2.0
"""The campaign log may only ever grow at its end.

A reader holds a cursor into each phase file, so a repeatable phase run again must land
*after* everything already written. A repeatable phase writes the same filename every time,
so each finished run is archived under ``_execution/sections/<seq>-<phase>.log`` before the
next one starts -- otherwise the new run replaces rows behind a position a live viewer has
already read, and a shorter run makes the reader start the file over.
"""

import pytest

from robovast.service.campaign_log import read_rows
from tests.service.null_service import NullService
from robovast.service.workspaces import WorkspaceRegistry, WorkspaceStore
from tests.service.null_service import NullService


@pytest.fixture
def transport(tmp_path):
    store = WorkspaceStore(registry=WorkspaceRegistry(root=tmp_path / "workspaces"))
    return NullService(store=store, results_dir=str(tmp_path / "results"))


def _exec_dir(transport, campaign_id="camp-1"):
    path = transport.campaign_dir(campaign_id) / "_execution"  # noqa: SLF001
    path.mkdir(parents=True, exist_ok=True)
    return path


def _sections(transport, campaign_id="camp-1"):
    return sorted(p.name for p in (_exec_dir(transport, campaign_id) / "sections").iterdir())


def test_a_rerun_archives_the_finished_section_before_it_starts(transport):
    """Moved aside, so the section a reader has already consumed keeps its bytes and its
    offset, and the new run writes into a file nobody has read yet."""
    exec_dir = _exec_dir(transport)
    (exec_dir / "postprocessing.log").write_text("the first postprocess\n")
    (exec_dir / "share.log").write_text("the export that followed it\n")

    transport._archive_repeatable_sections("camp-1")  # noqa: SLF001

    assert (exec_dir / "sections" / "0001-postprocessing.log").read_text() == \
        "the first postprocess\n"
    assert (exec_dir / "sections" / "0002-share.log").read_text() == \
        "the export that followed it\n"
    # Gone, not emptied: the next run creates its own file, and a zero-length one left
    # here would be read as a live phase that has written nothing yet.
    assert not (exec_dir / "postprocessing.log").exists()
    assert not (exec_dir / "share.log").exists()


def test_a_second_rerun_continues_the_sequence(transport):
    """The sequence is the campaign's order, so it counts across phases and across runs --
    a name reused would overwrite a finished section."""
    exec_dir = _exec_dir(transport)
    (exec_dir / "sections").mkdir()
    (exec_dir / "sections" / "0001-postprocessing.log").write_text("first\n")
    (exec_dir / "postprocessing.log").write_text("second\n")

    transport._archive_repeatable_sections("camp-1")  # noqa: SLF001

    assert (exec_dir / "sections" / "0002-postprocessing.log").read_text() == "second\n"
    assert (exec_dir / "sections" / "0001-postprocessing.log").read_text() == "first\n"


def test_a_phase_that_never_ran_burns_no_sequence_number(transport):
    """A gap in the numbering would say something happened between two sections that did
    not, and the sequence is the only record of the order."""
    exec_dir = _exec_dir(transport)
    (exec_dir / "postprocessing.log").write_text("a postprocess, no export\n")

    transport._archive_repeatable_sections("camp-1")  # noqa: SLF001
    (exec_dir / "postprocessing.log").write_text("another postprocess\n")
    transport._archive_repeatable_sections("camp-1")  # noqa: SLF001

    assert _sections(transport) == ["0001-postprocessing.log", "0002-postprocessing.log"]


def test_a_rerun_reads_after_the_phase_that_followed_it(transport):
    """Postprocess, share, postprocess again. The second postprocess must read *after* the
    share, not back in the slot the first one occupied: a fixed phase order would put its
    bytes ahead of an offset the reader had consumed, and a live viewer would never see
    them."""
    exec_dir = _exec_dir(transport)
    (exec_dir / "controller.log").write_text("ran the campaign\n")
    (exec_dir / "postprocessing.log").write_text("first postprocess\n")

    transport._archive_repeatable_sections("camp-1")  # noqa: SLF001
    (exec_dir / "share.log").write_text("the export\n")
    transport._archive_repeatable_sections("camp-1")  # noqa: SLF001
    (exec_dir / "postprocessing.log").write_text("second postprocess\n")

    messages = [row.message for row in
                read_rows(transport.campaign_dir("camp-1"), final=True).rows]
    assert messages == ["ran the campaign", "first postprocess", "the export",
                        "second postprocess"]


def test_the_log_never_shrinks_under_a_reader_watching_it(transport):
    """The property the archiving exists for, asserted on the rows: a shorter second run
    must not make a reader lose what it had, nor read it twice."""
    exec_dir = _exec_dir(transport)
    (exec_dir / "controller.log").write_text("ran the campaign\n")
    (exec_dir / "postprocessing.log").write_text("a long first postprocess\n" * 5)

    campaign_dir = transport.campaign_dir("camp-1")  # noqa: SLF001
    before = read_rows(campaign_dir, final=True)

    transport._archive_repeatable_sections("camp-1")  # noqa: SLF001
    (exec_dir / "postprocessing.log").write_text("short\n")

    after = read_rows(campaign_dir, before.cursor, final=True)
    assert [row.message for row in after.rows] == ["short"]
    assert [row.message for row in read_rows(campaign_dir, final=True).rows] == (
        ["ran the campaign"] + ["a long first postprocess"] * 5 + ["short"])


def test_a_campaign_with_no_directory_yet_is_not_an_error(transport):
    """An import is dispatched through the same path, and it has nothing to archive."""
    transport._archive_repeatable_sections("not-a-campaign-yet")  # noqa: SLF001
