# Copyright (C) 2026 Frederik Pasch
# SPDX-License-Identifier: Apache-2.0

"""What a ``vast`` command prints when it fails.

A refusal is its message; a bug is its type, its message and the frames it was raised
in -- printed, not pointed at, because the frames are in hand and the person reading
the terminal is the one who has to fix the line. What a service answered is complete as
it arrives: a bug on its side comes rendered, and the frames on this side are the
transport's.
"""

import pytest

from robovast.client.errors import handle_cli_exception
from robovast.common.errors import CampaignConfigError
from robovast.service.interface import ServiceError


def _boom():
    raise KeyError("no such slot")


def _report(raise_it, capsys):
    """What the CLI prints for the exception *raise_it* raises, caught as a verb would."""
    with pytest.raises(SystemExit) as exit_info:
        try:
            raise_it()
        except Exception as e:  # noqa: BLE001 - the handler under test takes them all
            handle_cli_exception(e)
    assert exit_info.value.code == 1
    return capsys.readouterr().err


def test_a_bug_prints_its_type_message_and_frames(capsys):
    err = _report(_boom, capsys)
    assert err.startswith("Error: KeyError: 'no such slot'\n")
    assert "in _boom" in err
    assert 'raise KeyError("no such slot")' in err
    assert "DEBUG" not in err


def test_a_refusal_prints_its_message_alone(capsys):
    def refuse():
        raise CampaignConfigError("the .vast names no scenario file")
    assert _report(refuse, capsys) == "Error: the .vast names no scenario file\n"


def test_what_a_service_said_is_printed_as_it_arrived(capsys):
    """The detail may itself carry frames, rendered on the service; nothing is added."""
    detail = ("Variation failed. Broken: 'x'\n\nTraceback (most recent call last):\n"
              "  File \"p.py\", line 3, in variation")

    def answer():
        raise ServiceError(400, detail)
    assert _report(answer, capsys) == f"Error: {detail}\n"
