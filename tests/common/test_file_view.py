# Copyright (C) 2026 Frederik Pasch
# SPDX-License-Identifier: Apache-2.0
"""Rendering rules that must not depend on which substrate answered.

The local lane iterates an open file; the object-store lane has only bytes. Both go
through the helpers here, because when they did not, the same file reported a
different line count depending on the backend.
"""

import pytest

from robovast.common import file_view


@pytest.mark.parametrize("raw", [
    "a\nb\nc\n",
    "a\nb\nc",
    "",
    "\n",
    # The characters that made the two lanes disagree: str.splitlines() breaks on form
    # feed, NEL and the Unicode separators; iterating a file does not.
    "a\x0cb\nc\xc2\x85d\n",
    "a b\nc d\n",
    "crlf\r\nlines\r\n",
])
def test_both_lanes_count_the_same_lines(tmp_path, raw):
    path = tmp_path / "log.txt"
    path.write_bytes(raw.encode())
    from_disk = file_view.read_text_page(path, lines=0)
    from_bytes = file_view.text_page(raw, lines=0)
    assert from_disk["total_lines"] == from_bytes["total_lines"], raw
    assert from_disk["content"] == from_bytes["content"], raw


def test_a_trailing_newline_does_not_invent_a_line():
    assert file_view.text_page("a\nb\n")["total_lines"] == 2


def test_lines_zero_means_the_whole_file():
    page = file_view.text_page("a\nb\nc\n", lines=0)
    assert page["returned_lines"] == 3


def test_scan_dir_does_not_descend_into_a_skipped_directory(tmp_path):
    """The skip is consulted before the walk, not after it — a hidden subtree costs
    one call, not one per file. A pinned workspace is a live git checkout, so the
    difference is milliseconds versus most of a second per web-UI listing."""
    (tmp_path / "project").mkdir()
    (tmp_path / "project" / "demo.vast").write_text("x")
    heavy = tmp_path / ".git" / "objects"
    heavy.mkdir(parents=True)
    for i in range(200):
        (heavy / f"obj{i}").write_text("x")

    seen = []

    def skip(rel, _is_dir):
        seen.append(rel)
        return rel.split("/")[0].startswith(".")

    found = file_view.scan_dir(tmp_path, recursive=True, skip=skip)
    assert [name for name, _ in found] == ["project/demo.vast"]
    # ``.git`` was rejected once; nothing underneath it was ever offered.
    assert not any(s.startswith(".git/") for s in seen)


def test_scan_dir_marks_directories_and_lists_one_level(tmp_path):
    (tmp_path / "sub").mkdir()
    (tmp_path / "sub" / "deep.txt").write_text("x")
    (tmp_path / "top.txt").write_text("x")
    assert [n for n, _ in file_view.scan_dir(tmp_path)] == ["sub/", "top.txt"]
    assert [n for n, _ in file_view.scan_dir(tmp_path, recursive=True)] == [
        "sub/deep.txt", "top.txt"]


def test_an_unreadable_subdirectory_does_not_fail_the_listing(tmp_path):
    (tmp_path / "ok.txt").write_text("x")
    locked = tmp_path / "locked"
    locked.mkdir()
    (locked / "hidden.txt").write_text("x")
    locked.chmod(0o000)
    try:
        assert [n for n, _ in file_view.scan_dir(tmp_path, recursive=True)] == ["ok.txt"]
    finally:
        locked.chmod(0o755)


#: A raw PGM the way an occupancy map is written: a text header, then one byte per cell.
#: Open floor is ``0xfe``, so a map of an empty room holds no NUL byte anywhere.
_PGM_OPEN_FLOOR = b"P5\n8 8\n255\n" + b"\xfe" * 64


def test_a_raster_with_no_nul_byte_is_binary(tmp_path):
    """Binary is what the text lane cannot decode, not only what holds a NUL.

    Rendering such a file as text turns every sample into a replacement character and
    hides the byte URL that serves it properly, so the refusal is the useful answer.
    """
    path = tmp_path / "map.pgm"
    path.write_bytes(_PGM_OPEN_FLOOR)
    assert file_view.is_binary_bytes(_PGM_OPEN_FLOOR) is True
    assert file_view.is_binary(path) is True
    with pytest.raises(ValueError, match="binary"):
        file_view.read_text_page(path)


def test_a_multibyte_character_at_the_sample_boundary_is_still_text():
    """The sample cuts the file at a fixed offset, which lands mid-character sooner or
    later; a truncated sequence there must not turn a UTF-8 document into a binary one."""
    filler = "a" * (file_view._SNIFF_BYTES - 1)  # pylint: disable=protected-access
    text = (filler + "ä" + "b" * 100).encode()
    assert file_view.is_binary_bytes(text) is False


@pytest.mark.parametrize("raw", [b"plain\n", "héllo\n".encode(), b"", b"\xef\xbb\xbfbom\n"])
def test_utf8_text_stays_text(raw):
    assert file_view.is_binary_bytes(raw) is False
