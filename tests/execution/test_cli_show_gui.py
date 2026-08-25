# Copyright (C) 2026 Frederik Pasch
# SPDX-License-Identifier: Apache-2.0
"""A GUI *default* degrades to headless; an explicit GUI *request* is refused.

Collapsing these into one rule is wrong either way:

- Through the service, ``show_gui=True`` is something the caller asked for, so a host with
  no display must refuse — accepting it produces a run that looks fine and draws nothing
  (see ``tests/service/test_show_gui.py``).
- On ``vast execution local run``, GUI is the default. A build machine runs it with no
  display and no ``--no-gui``; refusing there would break unattended use over a flag nobody
  set.

So the CLI downgrades — but not silently, and it must stop selecting
``execution.local.gui.parameter_overrides`` when it does, or the run would carry a
``headless: "False"`` no display can honour.
"""

import pytest

from robovast.common.host_display import gui_by_default


@pytest.fixture
def no_display(monkeypatch, tmp_path):
    monkeypatch.delenv("DISPLAY", raising=False)
    monkeypatch.setattr("robovast.common.host_display._X11_SOCKET_GLOB",
                        str(tmp_path / "X*"))


@pytest.fixture
def a_display(monkeypatch):
    monkeypatch.setenv("DISPLAY", ":9")


def test_a_headless_host_runs_anyway(no_display):
    said = []
    assert gui_by_default(False, notify=said.append) is False
    assert said, "a downgrade with no explanation leaves 'I expected a window' unanswered"
    assert "headless" in said[0]


def test_a_host_with_a_display_runs_windowed_and_says_nothing(a_display):
    said = []
    assert gui_by_default(False, notify=said.append) is True
    assert said == []


def test_no_gui_wins_over_an_available_display_without_a_notice(a_display):
    # --no-gui was the caller's choice; narrating it would be noise.
    said = []
    assert gui_by_default(True, notify=said.append) is False
    assert said == []
