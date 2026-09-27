# Copyright (C) 2026 Frederik Pasch
# SPDX-License-Identifier: Apache-2.0
"""``vast campaign download A B``: one campaign that fails does not stop the others."""

import contextlib

from click.testing import CliRunner

from robovast.client import campaign_cli


def test_a_failed_campaign_is_reported_and_the_next_one_still_downloads(monkeypatch, tmp_path):
    import robovast.service.project_push as project_push

    def _download(_client, campaign_id, dest, progress_callback=None):
        if campaign_id == "a-2026-09-01-10000000":
            raise KeyError("no such campaign")
        with open(dest, "wb") as fh:
            fh.write(b"archive")
        return dest

    @contextlib.contextmanager
    def _service(*_a, **_k):
        yield object(), "fake service"

    monkeypatch.setattr(project_push, "download_campaign_archive", _download)
    monkeypatch.setattr(campaign_cli, "service_client", _service)
    result = CliRunner().invoke(campaign_cli.campaign, [
        "download", "a-2026-09-01-10000000", "b-2026-09-01-10000000", "-o", str(tmp_path)])

    assert (tmp_path / "b-2026-09-01-10000000.tar.gz").read_bytes() == b"archive"
    assert "no such campaign" in result.output
    assert "Downloaded 1" in result.output
    # Something did not land, so the command did not succeed.
    assert result.exit_code == 1
    assert "1 of 2" in result.output
