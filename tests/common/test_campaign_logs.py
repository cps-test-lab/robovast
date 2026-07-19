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

from robovast.common.campaign_logs import (assemble_log, assemble_log_from_dir,
                                           phase_banner)


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
