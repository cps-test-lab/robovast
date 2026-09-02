# Copyright (C) 2026 Frederik Pasch
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
# http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing,
# software distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions
# and limitations under the License.
#
# SPDX-License-Identifier: Apache-2.0

"""Tests for the unified campaign infrastructure-log assembler."""

import logging

from robovast.common import campaign_logs

from robovast.client.logging_config import add_campaign_log_handler, remove_campaign_log_handler
from robovast.common.campaign_logs import (INFRA_PHASES, assemble_log, assemble_log_from_dir,
                                           phase_banner)

#: Every phase file, so a test can populate a source that is complete by construction —
#: adding a phase must not quietly turn a "consulted nothing further" assertion green.
_ALL_PHASE_FILES = INFRA_PHASES


def _store_reader(store):
    return lambda name: store.get(name)


def test_missing_phases_yield_empty_stream():
    text, next_offset, eof = assemble_log(_store_reader({}), offset=0, eof=True)
    assert text == ""
    assert next_offset == 0
    assert eof is True


def test_single_phase_has_banner_and_content():
    store = {"variation.log": b"gen A\ngen B\n"}
    text, next_offset, _ = assemble_log(_store_reader(store), offset=0)
    assert text == phase_banner("VARIATION") + "gen A\ngen B\n"
    assert next_offset == len(text.encode("utf-8"))


def test_phases_are_ordered_variation_run_postprocessing():
    store = {
        "postprocessing.log": b"db built\n",
        "controller.log": b"batch 1\n",
        "variation.log": b"gen\n",
    }
    text, _, _ = assemble_log(_store_reader(store), offset=0, eof=True)
    assert text.index("VARIATION") < text.index("RUN") < text.index("POSTPROCESSING")


def test_offset_resume_is_stable_and_append_only():
    store = {"variation.log": b"gen A\ngen B\n"}
    reader = _store_reader(store)

    first, off, _ = assemble_log(reader, offset=0)
    # Re-polling from the returned offset with no new bytes yields an empty, stable tail.
    tail, off2, _ = assemble_log(reader, offset=off)
    assert tail == ""
    assert off2 == off

    # A later phase appends without disturbing the already-consumed prefix.
    store["controller.log"] = b"batch 1\n"
    appended, off3, _ = assemble_log(reader, offset=off)
    assert appended == phase_banner("RUN") + "batch 1\n"
    assert off3 == off + len(appended.encode("utf-8"))
    # The full stream from 0 equals prefix + appended (append-only invariant).
    full, _, _ = assemble_log(reader, offset=0)
    assert full == first + appended


def test_offset_past_end_does_not_move_backwards():
    store = {"variation.log": b"x\n"}
    _, end, _ = assemble_log(_store_reader(store), offset=0)
    tail, off, _ = assemble_log(_store_reader(store), offset=end + 100)
    assert tail == ""
    assert off == end + 100


def test_written_line_is_live_before_handler_close(tmp_path):
    """Liveness invariant that the SSE stream depends on: a record written through
    the real controller-log handler is observable by the assembler *immediately* —
    without closing the handler — because the ``FileHandler`` flushes per record. If
    a change ever buffered these writes, the web UI's live log would go dark and this
    test would fail."""
    log_path = tmp_path / "_execution" / "controller.log"
    robovast_logger = logging.getLogger("robovast")
    prev_level = robovast_logger.level
    robovast_logger.setLevel(logging.INFO)  # runtime configures this; a bare test doesn't
    handler = add_campaign_log_handler(str(log_path))
    try:
        logging.getLogger("robovast.execution.controller").info("live line one")
        text, _, _ = assemble_log_from_dir(tmp_path, offset=0)
        assert "live line one" in text
        # A second record appends and is visible on the next poll — still no close.
        logging.getLogger("robovast.execution.controller").info("live line two")
        text2, _, _ = assemble_log_from_dir(tmp_path, offset=0)
        assert "live line two" in text2
        assert text2.index("live line one") < text2.index("live line two")
    finally:
        remove_campaign_log_handler(handler)
        robovast_logger.setLevel(prev_level)


def test_assemble_from_dir_reads_execution_files(tmp_path):
    exec_dir = tmp_path / "_execution"
    exec_dir.mkdir()
    (exec_dir / "variation.log").write_text("composed\n")
    (exec_dir / "controller.log").write_text("ran\n")
    text, _, eof = assemble_log_from_dir(tmp_path, offset=0, eof=True)
    assert "composed" in text and "ran" in text
    assert text.index("VARIATION") < text.index("RUN")
    assert "POSTPROCESSING" not in text  # absent phase file → no section
    assert eof is True


# -- splitting the assembled stream back into phases ------------------------
#
# A reader that wants one phase (the BUILD aside, say) slices the assembled stream. The
# sections must tile it exactly, or a filtered read would renumber the lines of the parts
# it kept and report a different total than an unfiltered one.

def test_sections_tile_the_stream_exactly():
    from robovast.common.campaign_logs import split_phases
    store = {"build.log": b"layer 1\nlayer 2\n",
             "variation.log": b"gen A\n",
             "controller.log": b"batch 0\nrun 0\n"}
    text, _, _ = assemble_log(_store_reader(store), offset=0)

    sections = split_phases(text)
    assert [name for name, _ in sections] == ["BUILD", "VARIATION", "RUN"]
    assert "".join(body for _, body in sections) == text


def test_selecting_a_subset_splices_without_shifting_lines():
    from robovast.common.campaign_logs import split_phases
    store = {"build.log": b"layer 1\n", "controller.log": b"batch 0\n"}
    with_build, _, _ = assemble_log(_store_reader(store), offset=0)
    without, _, _ = assemble_log(
        _store_reader({"controller.log": b"batch 0\n"}), offset=0)

    kept = "".join(body for name, body in split_phases(with_build) if name != "BUILD")
    assert kept == without


def test_a_log_line_shaped_like_a_banner_does_not_invent_a_phase():
    from robovast.common.campaign_logs import split_phases
    store = {"controller.log": b"===== NOT A PHASE =====\nbatch 0\n"}
    text, _, _ = assemble_log(_store_reader(store), offset=0)

    assert [name for name, _ in split_phases(text)] == ["RUN"]


def test_content_before_the_first_divider_is_not_dropped():
    from robovast.common.campaign_logs import split_phases
    text = "stray prologue\n" + phase_banner("RUN") + "batch 0\n"
    sections = split_phases(text)
    assert sections[0][0] == ""
    assert "".join(body for _, body in sections) == text


def test_build_is_the_first_phase():
    """First because it happens first — and because appending it last would insert bytes
    *ahead of* later phases as they appear, shifting every byte offset a poller holds.
    """
    assert INFRA_PHASES[0][0] == "BUILD"


# -- layering two byte sources ----------------------------------------------
#
# Phases are written by different processes into different places: on the cluster the
# controller's files land in the service's scratch while postprocessing works in its own
# fetched campaign root and publishes to the object store. Reading one alone drops whole
# phases, and does it silently — a missing phase file is also how "not run yet" looks.

def test_a_phase_missing_from_the_first_source_comes_from_the_second():
    """The bug this exists for: a tracked cluster campaign served a log with no
    POSTPROCESSING section at all — pass or fail — while the durable copy sat in the
    object store the whole time.
    """
    from robovast.common.campaign_logs import layered_get_bytes
    scratch = _store_reader({"controller.log": b"ran\n"})
    store = _store_reader({"controller.log": b"stale\n",
                           "postprocessing.log": b"converted 8 bags\n"})
    text, _, _ = assemble_log(layered_get_bytes(scratch, store), offset=0, eof=True)
    assert "converted 8 bags" in text
    assert "ran" in text and "stale" not in text  # first source wins where it has the file


def test_a_live_phase_file_is_never_displaced_by_the_durable_copy():
    """Fallback is on absence, not on length. The durable copy of a phase still being
    written lags it, so preferring the longer one would let a poll return FEWER bytes
    than the last — leaving the client's offset past the end of the stream.
    """
    from robovast.common.campaign_logs import layered_get_bytes
    store = _store_reader({"controller.log": b"a\nb\nc\nd\n"})  # longer, but frozen

    growing = {"controller.log": b"a\n"}
    get_bytes = layered_get_bytes(_store_reader(growing), store)
    first, next_offset, _ = assemble_log(get_bytes, offset=0)
    growing["controller.log"] = b"a\nb\n"
    second, _, _ = assemble_log(get_bytes, offset=next_offset)

    assert first.endswith("a\n") and second == "b\n"  # append-only across the two polls


def test_an_empty_phase_file_counts_as_present():
    """Present-but-empty is a phase that started and has written nothing yet. Falling
    through to a durable copy there would make the stream jump backwards once the live
    file does get its first line.
    """
    from robovast.common.campaign_logs import layered_get_bytes
    get_bytes = layered_get_bytes(_store_reader({"controller.log": b""}),
                                  _store_reader({"controller.log": b"from the store\n"}))
    text, _, _ = assemble_log(get_bytes, offset=0, eof=True)
    assert "from the store" not in text


def test_a_later_source_is_not_consulted_when_the_first_has_every_phase():
    """This layering sits behind the log SSE stream, which re-polls while a user watches.
    A campaign whose phase files are all on scratch must not pay a store round-trip per
    poll just to be told the store also has them.
    """
    from robovast.common.campaign_logs import layered_get_bytes
    asked = []

    def _tracking(name):
        asked.append(name)
        # Falls through to None: this layer never has the bytes.

    scratch = _store_reader({name: b"x\n" for _, name in _ALL_PHASE_FILES})
    assemble_log(layered_get_bytes(scratch, _tracking), offset=0, eof=True)
    assert asked == []


def test_a_phase_is_read_from_whoever_writes_it():
    """A local copy wins by default, and loses for a phase file the caller names as
    written elsewhere.

    On the cluster lane a postprocess writes its log into a fetched root and publishes it,
    so the copy under the tracked root is whatever an earlier attempt left behind. Present,
    frozen and wrong is the one combination an absence-only fallback cannot see past -- and
    it inverts the answer, showing a postprocess that succeeded as the failure before it.
    """
    local = {"controller.log": b"live run", "postprocessing.log": b"an older attempt"}
    remote = {"controller.log": b"lagging run", "postprocessing.log": b"what the pod wrote"}

    get_bytes = campaign_logs.layered_by_writer(local.get, remote.get,
                                                {"postprocessing.log"})

    assert get_bytes("controller.log") == b"live run"
    assert get_bytes("postprocessing.log") == b"what the pod wrote"


def test_nothing_is_read_remotely_unless_the_caller_says_so():
    """Empty by default, because which files those are is a fact about one operation on one
    lane and not about the phase: the same postprocessing log is written into the tracked
    root locally and into a fetched one on the cluster. A set fixed in this module would
    make every reader pay two store round-trips per poll for the one case it applies to,
    behind an SSE stream that re-polls while a user watches.
    """
    remote_calls = []

    def _remote(filename):
        remote_calls.append(filename)
        return b"durable"

    get_bytes = campaign_logs.layered_by_writer(
        {f: b"local" for _p, f in campaign_logs.INFRA_PHASES}.get, _remote)

    for _phase, filename in campaign_logs.INFRA_PHASES:
        assert get_bytes(filename) == b"local", filename
    assert remote_calls == []


def test_either_source_still_covers_the_other_absence():
    """Changing which copy is believed must not cost the coverage: a phase present in only
    one place is still served, whichever place that is. Reading one location alone drops
    whole phases silently, since a missing phase file is also the normal "has not run yet".
    """
    get_bytes = campaign_logs.layered_by_writer(
        {"controller.log": b"only local"}.get,
        {"postprocessing.log": b"only remote"}.get,
        {"postprocessing.log"})

    assert get_bytes("controller.log") == b"only local"
    assert get_bytes("postprocessing.log") == b"only remote"
    assert get_bytes("variation.log") is None


def test_the_source_of_a_phase_cannot_change_mid_campaign():
    """The choice is by writer, never by which copy is longer or newer: a size or mtime
    comparison would flip as a file grows, and a poll that returned fewer bytes than the
    last one leaves the client's offset past the end of the stream.
    """
    for local_len, remote_len in ((1, 500), (500, 1)):
        get_bytes = campaign_logs.layered_by_writer(
            {"postprocessing.log": b"x" * local_len}.get,
            {"postprocessing.log": b"y" * remote_len}.get,
            {"postprocessing.log"})

        assert get_bytes("postprocessing.log") == b"y" * remote_len
