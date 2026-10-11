# Copyright (C) 2026 Frederik Pasch
# SPDX-License-Identifier: Apache-2.0
"""``vast campaign download --extract`` unpacks the archive as it streams: the campaign
lands as a directory, no archive is kept, and a cut transfer leaves no half campaign."""

import gzip
import io
import tarfile
from types import SimpleNamespace

import pytest

from robovast.service.project_push import extract_campaign_archive

CID = "camp-2026-01-01-000000"


def _archive(members) -> bytes:
    out = io.BytesIO()
    with tarfile.open(fileobj=out, mode="w:gz") as tar:
        for name, payload in members:
            info = tarfile.TarInfo(name)
            info.size = len(payload)
            tar.addfile(info, io.BytesIO(payload))
    return out.getvalue()


class _Raw(io.BytesIO):
    """A response's raw stream: what ``requests`` hands a streaming reader."""
    decode_content = True


class _StreamingResponse:
    def __init__(self, payload: bytes, served: str):
        self.raw = _Raw(payload)
        self.status_code = 200
        self.headers = {"Content-Disposition": f'attachment; filename="{served}"'}

    def __enter__(self):
        return self

    def __exit__(self, *_exc):
        return False


class _Client:
    """A client whose one GET streams *payload* under *served* as its file name."""

    def __init__(self, payload: bytes, served: str = f"{CID}.tar.gz"):
        self.base_url = "http://robovast.example.org"
        self.payload = payload
        self.served = served
        self.session = SimpleNamespace(get=self._get)
        self.params = []

    def _get(self, url, timeout, stream, params=None):
        del url, timeout, stream
        self.params.append(params)
        return _StreamingResponse(self.payload, self.served)

    @staticmethod
    def raise_for_status(response):
        del response


def test_the_archive_is_extracted_as_it_streams_and_no_archive_is_kept(tmp_path):
    payload = _archive([(f"{CID}/campaign.db", b"db"), (f"{CID}/cfg/0/test.xml", b"<t/>")])
    seen = []
    landed = extract_campaign_archive(_Client(payload), CID, str(tmp_path),
                                      progress_callback=lambda got, total: seen.append(got))
    assert landed == str(tmp_path / CID)
    assert (tmp_path / CID / "cfg" / "0" / "test.xml").read_bytes() == b"<t/>"
    assert sorted(p.name for p in tmp_path.iterdir()) == [CID], "no archive, no scratch"
    assert seen and seen[-1] == len(payload)


def test_raw_asks_the_route_for_the_records_alone(tmp_path):
    payload = _archive([(f"{CID}/campaign.db", b"db")])
    client = _Client(payload, served=f"{CID}.raw.tar.gz")
    landed = extract_campaign_archive(client, CID, str(tmp_path), raw=True)
    assert client.params == [{"raw": "true"}]
    assert landed == str(tmp_path / f"{CID}.raw")


def test_the_service_names_the_tree(tmp_path):
    payload = _archive([(f"{CID}.incomplete/campaign.db", b"db")])
    landed = extract_campaign_archive(_Client(payload, f"{CID}.incomplete.tar.gz"), CID,
                                      str(tmp_path))
    assert landed == str(tmp_path / f"{CID}.incomplete")


def test_a_cut_transfer_leaves_no_half_campaign(tmp_path):
    payload = _archive([(f"{CID}/campaign.db", b"db"), (f"{CID}/cfg/0/test.xml", b"<t/>")])
    cut = gzip.decompress(payload)[:600]                    # mid-member
    with pytest.raises((tarfile.TarError, EOFError, OSError)):
        extract_campaign_archive(_Client(gzip.compress(cut)), CID, str(tmp_path))
    assert list(tmp_path.iterdir()) == [], "nothing under the real name, no scratch left"


@pytest.mark.parametrize("served", [".tar.gz", "...tar.gz"])
def test_a_served_name_that_names_no_directory_is_refused(tmp_path, served):
    """An empty name would make *out_dir* itself the target, and ``..`` its parent: both are
    removed to make room for the campaign."""
    out = tmp_path / "out"
    out.mkdir()
    (out / "kept.txt").write_text("mine")
    (tmp_path / "beside.txt").write_text("mine too")
    payload = _archive([(f"{CID}/campaign.db", b"db")])
    with pytest.raises(RuntimeError, match="names no directory"):
        extract_campaign_archive(_Client(payload, served), CID, str(out))
    assert (out / "kept.txt").read_text() == "mine"
    assert (tmp_path / "beside.txt").read_text() == "mine too"
