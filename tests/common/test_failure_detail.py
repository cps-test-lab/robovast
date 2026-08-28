# Copyright (C) 2026 Frederik Pasch
# SPDX-License-Identifier: Apache-2.0
"""``failure_detail`` must state a failure's reason exactly once.

It writes the string the UI shows in the campaign's error box. Building that
string as ``message + format_exception(...)`` — whose last line *is* the
message — prints every recorded failure's reason twice, and a long message (the
version-1 config migration text runs some twenty lines) fills the whole
``tail_lines`` budget with the duplicate, leaving no room for the frames the
tail exists to show.
"""

import pytest

from robovast.client.status import failure_detail
from robovast.common.errors import CampaignConfigError


def _raise(exc):  # pylint: disable=inconsistent-return-statements
    """Return *exc* with a real ``__traceback__`` attached.

    The ``try`` body always raises, so the ``except`` is the only way out -- which pylint
    cannot see, and reads as a path that falls through without returning.
    """
    try:
        raise exc
    except type(exc) as e:
        return e


def test_message_appears_once():
    detail = failure_detail(_raise(ValueError("boom: the widget is unplugged")))
    assert detail.count("boom: the widget is unplugged") == 1


def test_frames_are_still_recorded():
    """The message is not all that is kept — a genuine bug still says where."""
    detail = failure_detail(_raise(RuntimeError("internal invariant broken")))
    assert "test_failure_detail.py" in detail
    assert "_raise" in detail


def test_long_message_does_not_crowd_out_the_frames():
    """The tail budget goes to frames, not to a re-print of a twenty-line message."""
    message = "\n".join(f"migration line {i}" for i in range(30))
    detail = failure_detail(_raise(ValueError(message)))
    assert detail.count("migration line 29") == 1
    assert "test_failure_detail.py" in detail


def test_clean_user_error_keeps_message_only():
    """``include_traceback = False`` still means no frames at all."""
    detail = failure_detail(_raise(CampaignConfigError("no such config: 'typo'")))
    assert detail == "no such config: 'typo'"


def test_chained_cause_is_preserved():
    """A cause names a *different* failure; dropping only the final line keeps it.

    Asserted on the ``Type: message`` lines rather than on the bare text: a frame
    whose source line is ``raise RuntimeError("outer failure")`` legitimately shows
    the message again, and that is the code, not a second report of the failure.
    """
    def _chained():
        try:
            raise KeyError("underlying_key")
        except KeyError as cause:
            raise RuntimeError("outer failure") from cause

    with pytest.raises(RuntimeError) as excinfo:
        _chained()
    detail = failure_detail(excinfo.value, tail_lines=100)
    assert detail.startswith("outer failure")
    assert "RuntimeError: outer failure" not in detail
    assert "KeyError: 'underlying_key'" in detail


@pytest.mark.parametrize("tail_lines", [1, 5, 20])
def test_never_duplicates_at_any_tail_length(tail_lines):
    detail = failure_detail(_raise(ValueError("single reason")), tail_lines=tail_lines)
    assert detail.count("single reason") == 1
