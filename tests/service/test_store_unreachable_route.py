# Copyright (C) 2026 Frederik Pasch
# SPDX-License-Identifier: Apache-2.0
"""An unreachable object store is a 503 with a sentence, not an ASGI 500.

Every cluster read (a status probe, a listing, a file read) ends at the object store,
so when the store stops answering the failure reaches ``_guard`` on many routes at
once. A raw botocore transport error reaching it becomes a ~90-line traceback in the
service log and a bare 500 for the caller, with the message the operator needs ("the
store did not answer, here is the endpoint") buried at the bottom.
"""

import pytest
from fastapi.testclient import TestClient

from robovast.common.errors import ObjectStoreUnreachableError
from robovast.service.app import build_app
from robovast.service.interface import Routes


class _UnreachableStore:
    """Impl whose data-status probe fails the way a dropped port-forward fails."""

    def campaign_data_status(self, campaign_id: str):
        raise ObjectStoreUnreachableError(
            "Object store at http://localhost:18080 is unreachable while checking "
            "s3://camp-1/_execution/data.db: Connection was closed.")

    def shutdown(self):
        pass


@pytest.fixture(name="client")
def _client():
    with TestClient(build_app(_UnreachableStore())) as client:
        yield client


def test_unreachable_store_is_503_carrying_the_message(client):
    resp = client.get(Routes.campaign_data_status("camp-1"))

    # 503, not the 409 its RuntimeError base would otherwise map to: nothing is in
    # conflict, and a client (the web UI's poll, the CLI) may simply try again.
    assert resp.status_code == 503
    detail = resp.json()["detail"]
    assert "http://localhost:18080" in detail
    assert "_execution/data.db" in detail


def test_a_real_conflict_still_maps_to_409(client):
    """The new arm sits above ``except RuntimeError`` — the general mapping is unchanged."""
    class _Busy(_UnreachableStore):
        def campaign_data_status(self, campaign_id: str):
            raise RuntimeError("a fetch for this campaign is already running")

    with TestClient(build_app(_Busy())) as busy:
        resp = busy.get(Routes.campaign_data_status("camp-1"))
    assert resp.status_code == 409
