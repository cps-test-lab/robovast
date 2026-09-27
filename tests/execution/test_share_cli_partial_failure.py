# Copyright (C) 2026 Frederik Pasch
# SPDX-License-Identifier: Apache-2.0
"""``vast share download|upload|remove`` on several archives: one that fails does not stop
the others, is reported on its own line, and makes the command exit 1 at the end."""

from click.testing import CliRunner

from robovast.execution import share_cli

_A, _B = "a-2026-09-01-10000000", "b-2026-09-01-10000000"


class _Share:
    """Every transfer succeeds except the one for campaign ``a``."""

    def __init__(self):
        self.done = []

    def _act(self, object_name):
        if object_name.startswith(_A):
            raise KeyError("no such object")
        self.done.append(object_name)

    def download_archive(self, object_name, dest, _progress, resume_offset=0):
        del resume_offset
        self._act(object_name)
        with open(dest, "wb") as fh:
            fh.write(b"archive")

    def remove_archive(self, object_name):
        self._act(object_name)


def _on_share(monkeypatch):
    share = _Share()
    monkeypatch.setattr(share_cli, "_provider", lambda: ("fake", share))
    monkeypatch.setattr(share_cli, "_archives", lambda _p: [
        (f"{_A}.raw.tar.gz", _A, "raw", 7), (f"{_B}.raw.tar.gz", _B, "raw", 7)])
    return share


def test_a_failed_download_is_reported_and_the_next_one_still_lands(monkeypatch, tmp_path):
    share = _on_share(monkeypatch)
    result = CliRunner().invoke(share_cli.share, ["download", _A, _B, "-o", str(tmp_path)])

    assert share.done == [f"{_B}.raw.tar.gz"]
    assert "no such object" in result.stderr
    assert result.exit_code == 1
    assert "1 of 2" in result.stderr


def test_a_failed_removal_is_reported_and_the_next_one_still_goes(monkeypatch):
    share = _on_share(monkeypatch)
    result = CliRunner().invoke(share_cli.share,
                                ["remove", "--campaign", _A, "--campaign", _B, "--yes"])

    assert share.done == [f"{_B}.raw.tar.gz"]
    assert "no such object" in result.stderr
    assert result.exit_code == 1
    assert "1 of 2" in result.stderr
