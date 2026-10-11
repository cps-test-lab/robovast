# Copyright (C) 2026 Frederik Pasch
# SPDX-License-Identifier: Apache-2.0
"""``vast campaign export`` — create, wait, download, print the file.

The command is the client's half of an export: it hands the request to the service as it
was typed, polls the status until the export is over, fetches the file through the same
transfer the archive download uses, and prints where it landed. A failed export is a
failure here, with the service's reason, and no file.
"""

import contextlib
from types import SimpleNamespace

import pytest
from click.testing import CliRunner

from robovast.client import campaign_cli
from robovast.service.interface import ExportRef, ExportStatus


class _Resp:
    headers = {"Content-Disposition":
               'attachment; filename="camp-1-export-0123456789ab.tar.gz"'}

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    @staticmethod
    def iter_content(chunk_size=0):
        yield b"gz"


@pytest.fixture(name="fake")
def _fake(monkeypatch):
    """A service that starts an export, reports it building once, then done."""
    seen = SimpleNamespace(requests=[], polls=0, urls=[], error="")

    class _Client:
        base_url = "http://service.example.com"
        session = SimpleNamespace(get=lambda url, **kw: seen.urls.append(url) or _Resp())

        @staticmethod
        def raise_for_status(resp):
            pass

        @staticmethod
        def create_export(campaign_id, request):
            seen.requests.append((campaign_id, request))
            return ExportRef(export_id="0123456789ab", url="/data/x")

        @staticmethod
        def get_export_status(campaign_id, export_id):
            seen.polls += 1
            if seen.polls == 1:
                return ExportStatus(export_id=export_id, tables={"runs": 3})
            return ExportStatus(export_id=export_id, done=True, error=seen.error, bytes=2,
                                tables={"runs": 3, "poses": 40})

    @contextlib.contextmanager
    def _client(*_a, **_k):
        yield _Client(), "fake service"

    monkeypatch.setattr(campaign_cli, "service_client", _client)
    monkeypatch.setattr("time.sleep", lambda s: None)
    return seen


def _run(*args):
    return CliRunner().invoke(campaign_cli.campaign, list(args))


def test_the_default_export_is_every_table_as_parquet_with_the_records(fake, tmp_path,
                                                                         monkeypatch):
    monkeypatch.chdir(tmp_path)
    result = _run('export', 'camp-1')
    assert result.exit_code == 0, result.output
    campaign_id, request = fake.requests[0]
    assert campaign_id == "camp-1"
    assert request.model_dump() == {"tables": None, "format": "parquet", "bags": "none",
                                    "records": True}
    assert fake.polls == 2
    assert fake.urls == ["http://service.example.com/data/campaigns/camp-1/exports/0123456789ab"]
    landed = tmp_path / "camp-1-export-0123456789ab.tar.gz"
    assert landed.read_bytes() == b"gz"
    assert result.output.strip().splitlines()[-1] == str(landed)


def test_every_option_reaches_the_request_and_the_file_lands_where_asked(fake, tmp_path):
    out = tmp_path / "sub" / "mine.tar.gz"
    result = _run('export', 'camp-1', '--tables', 'poses, run_log', '--format', 'csv',
                  '--bags', 'sqlite3', '--no-records', '-o', str(out))
    assert result.exit_code == 0, result.output
    _campaign_id, request = fake.requests[0]
    assert request.model_dump() == {"tables": ["poses", "run_log"], "format": "csv",
                                    "bags": "sqlite3", "records": False}
    assert out.read_bytes() == b"gz"
    assert result.output.strip().splitlines()[-1] == str(out)


def test_a_failed_export_is_reported_and_nothing_is_downloaded(fake, tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    fake.error = "topic /thing of type custom_msgs/msg/Thing has no message definition"
    result = _run('export', 'camp-1', '--bags', 'sqlite3')
    assert result.exit_code != 0
    assert "custom_msgs/msg/Thing" in result.output
    assert fake.urls == []
    assert not list(tmp_path.iterdir())


def test_a_bag_choice_the_service_does_not_offer_is_refused_here(fake):
    result = _run('export', 'camp-1', '--bags', 'db3')
    assert result.exit_code != 0
    assert fake.requests == []
