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


def _report(exc, capsys):
    with pytest.raises(SystemExit) as exit_info:
        handle_cli_exception(exc)
    assert exit_info.value.code == 1
    return capsys.readouterr().err


def test_a_bug_prints_its_type_message_and_frames(capsys):
    try:
        _boom()
    except KeyError as e:
        err = _report(e, capsys)
    assert err.startswith("Error: KeyError: 'no such slot'\n")
    assert "in _boom" in err
    assert 'raise KeyError("no such slot")' in err
    assert "DEBUG" not in err


def test_a_refusal_prints_its_message_alone(capsys):
    try:
        raise CampaignConfigError("the .vast names no scenario file")
    except CampaignConfigError as e:
        err = _report(e, capsys)
    assert err == "Error: the .vast names no scenario file\n"


def test_what_a_service_said_is_printed_as_it_arrived(capsys):
    """The detail may itself carry frames, rendered on the service; nothing is added."""
    detail = "Variation failed. Broken: 'x'\n\nTraceback (most recent call last):\n  File \"p.py\", line 3, in variation"
    try:
        raise ServiceError(400, detail)
    except ServiceError as e:
        err = _report(e, capsys)
    assert err == f"Error: {detail}\n"
