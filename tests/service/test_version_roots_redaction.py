# Copyright (C) 2026 Frederik Pasch
# SPDX-License-Identifier: Apache-2.0
"""``results_root``/``sources_root`` are only sent to a caller that can open them.

The contract on those two fields (``interface.VersionInfo``) is "non-null **only when the
caller can actually open them**: the service must be backed by a local filesystem *and*
the request must come from loopback". The transport can answer the first half; only the
route can answer the second, and it never did — ``local_transport`` carried a comment
saying "``app.py`` blanks them again for a non-loopback request", describing code that
was not there.

So a local ``vast serve`` reached over a tunnel handed a remote caller absolute paths on
the *service's* disk. Harmless to try and impossible to use, which is the failure the
contract exists to prevent: the MCP surface tells an agent that when these appear it
should "read files directly instead of relaying bytes through this interface".
"""

import pytest
from fastapi.testclient import TestClient

from robovast.service.app import build_app
from robovast.service.interface import VersionInfo

TOKEN = "t"
AUTH = {"Authorization": f"Bearer {TOKEN}"}


class _LocalFilesystemService:
    """A local-lane service: it has real roots and is willing to report them."""

    def version(self):
        return VersionInfo(robovast_version="x", backend="docker",
                           results_root="/srv/results", sources_root="/srv/sources")

    def __getattr__(self, name):
        raise AttributeError(name)


@pytest.fixture
def app():
    return build_app(_LocalFilesystemService(), mount_mcp=False, auth_token=TOKEN)


def _roots(response) -> tuple:
    body = response.json()
    return body.get("results_root"), body.get("sources_root")


def test_a_same_host_caller_gets_the_roots(app):
    """The whole point of the fields: read the files directly, skip the relay."""
    client = TestClient(app, client=("127.0.0.1", 5000))
    assert _roots(client.get("/version", headers=AUTH)) == ("/srv/results", "/srv/sources")


def test_a_remote_caller_does_not(app):
    client = TestClient(app, base_url="http://testserver")  # peer is not an IP at all
    assert _roots(client.get("/version", headers=AUTH)) == (None, None)


def test_a_forwarded_request_does_not_count_as_same_host(app):
    """Behind a proxy the peer *is* loopback and the caller is not. Trusting the peer
    address there would hand the roots to everyone the proxy fronts for."""
    client = TestClient(app, client=("127.0.0.1", 5000))
    response = client.get("/version", headers={**AUTH, "x-forwarded-for": "10.1.2.3"})
    assert _roots(response) == (None, None)


def test_redaction_does_not_disturb_the_rest_of_the_handshake(app):
    """It blanks two fields; the version handshake still has to work."""
    body = TestClient(app, base_url="http://testserver").get("/version", headers=AUTH).json()
    assert body["robovast_version"] == "x" and body["backend"] == "docker"
