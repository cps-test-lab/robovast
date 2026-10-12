# Copyright (C) 2026 Frederik Pasch
# SPDX-License-Identifier: Apache-2.0
"""The web UI is served from the siblings ``npm run build`` precompresses.

The bundle is several megabytes of JavaScript that compresses to about a quarter, and
compressing it per request would cost the control plane's event loop the same work on every
first load. So the build writes a ``.br`` and a ``.gz`` beside each file once
(``frontend/ui/precompress.ts``) and the mount picks the one the request accepts -- the
same bytes whether the service is reached directly or through the cluster's front, which
leaves a response that already names its coding alone.
"""

import gzip

import pytest
from starlette.testclient import TestClient

from robovast.service.app import accepts_encoding, build_app
from tests.service.null_service import NullService

SOURCE = b'export const x = "a repetitive chunk";\n' * 200


@pytest.fixture
def client(tmp_path, monkeypatch):
    dist = tmp_path / "dist"
    (dist / "assets").mkdir(parents=True)
    (dist / "index.html").write_text("<!doctype html>", encoding="utf-8")
    asset = dist / "assets" / "index-AAAA.js"
    asset.write_bytes(SOURCE)
    (dist / "assets" / "index-AAAA.js.gz").write_bytes(gzip.compress(SOURCE))
    # Opaque bytes are enough: the mount names the coding, it never decodes.
    (dist / "assets" / "index-AAAA.js.br").write_bytes(b"brotli-bytes")
    (dist / "assets" / "plain-BBBB.js").write_bytes(SOURCE)
    monkeypatch.setenv("ROBOVAST_UI_DIST", str(dist))
    with TestClient(build_app(NullService(), mount_mcp=False)) as test_client:
        yield test_client


def _get(client, path, accept):
    return client.get(path, headers={"Accept-Encoding": accept})


def test_gzip_is_served_from_its_sibling(client):
    response = _get(client, "/assets/index-AAAA.js", "gzip")
    assert response.status_code == 200
    assert response.headers["content-encoding"] == "gzip"
    assert response.headers["content-type"].startswith("text/javascript")
    assert response.headers["vary"] == "Accept-Encoding"
    assert response.headers["cache-control"] == "public, max-age=31536000, immutable"
    assert response.content == SOURCE  # the test client decodes the sibling


def test_brotli_is_preferred_when_accepted(client):
    with client.stream("GET", "/assets/index-AAAA.js",
                       headers={"Accept-Encoding": "gzip, deflate, br"}) as response:
        assert response.headers["content-encoding"] == "br"
        assert b"".join(response.iter_raw()) == b"brotli-bytes"


def test_a_client_that_accepts_neither_gets_the_original(client):
    for accept in ("identity", "gzip;q=0, br;q=0", ""):
        response = _get(client, "/assets/index-AAAA.js", accept)
        assert "content-encoding" not in response.headers, accept
        assert response.content == SOURCE, accept
        assert response.headers["vary"] == "Accept-Encoding", accept


def test_each_representation_revalidates_against_its_own_etag(client):
    """A browser that kept the gzip body sends that ETag back and gets a 304."""
    first = _get(client, "/assets/index-AAAA.js", "gzip")
    plain = _get(client, "/assets/index-AAAA.js", "identity")
    assert first.headers["etag"] != plain.headers["etag"]
    again = client.get("/assets/index-AAAA.js",
                       headers={"Accept-Encoding": "gzip", "If-None-Match": first.headers["etag"]})
    assert again.status_code == 304
    assert again.headers["vary"] == "Accept-Encoding"


def test_a_file_without_siblings_is_served_as_it_is(client):
    response = _get(client, "/assets/plain-BBBB.js", "gzip, br")
    assert "content-encoding" not in response.headers
    assert "vary" not in response.headers
    assert response.content == SOURCE


@pytest.mark.parametrize("header, coding, expected", [
    ("gzip, deflate, br", "br", True),
    ("gzip;q=0.5", "gzip", True),
    ("gzip;q=0", "gzip", False),
    ("br;q=0, *", "br", False),
    ("*", "br", True),
    ("*;q=0", "gzip", False),
    ("identity", "gzip", False),
    ("", "gzip", False),
    ("GZIP", "gzip", True),
    ("gzip;q=bogus", "gzip", False),
])
def test_accept_encoding(header, coding, expected):
    assert accepts_encoding(header, coding) is expected
