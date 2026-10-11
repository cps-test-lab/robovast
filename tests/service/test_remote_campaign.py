# Copyright (C) 2026 Frederik Pasch
# SPDX-License-Identifier: Apache-2.0
"""A campaign on a service answers a notebook's calls as the same campaign on disk does.

The requests go through the service's own routes (describe, and the uncapped CSV query);
only the socket is replaced, by routing the one function that opens a request into the
app's test client, so the routes, their auth and their errors are the real ones.
"""

import urllib.error
import urllib.parse

import pytest
from fastapi.testclient import TestClient

from robovast.service.app import build_app
from robovast.service.workspaces import WorkspaceRegistry, WorkspaceStore
from robovast_data import Campaign, QueryError, RemoteCampaign, open_data, read_table
from robovast_data import remote as remote_module
from tests.service.null_service import NullService

from ..results_processing.conftest import write_campaign_db, write_results_tree
from .conftest import TEST_TOKEN

CID = "camp-2026-08-10-07150919"
URL = f"http://robovast.example.org/campaigns/{CID}"


class _Response:
    def __init__(self, content):
        self._content = content

    def read(self):
        return self._content

    def close(self):
        pass

    def __enter__(self):
        return self

    def __exit__(self, *_exc):
        return False


@pytest.fixture(name="served")
def _served(tmp_path, monkeypatch):
    """The campaign on disk, and the same campaign behind a service's routes."""
    root = tmp_path / "results" / CID
    write_results_tree(root)
    write_campaign_db(root, CID)
    store = WorkspaceStore(registry=WorkspaceRegistry(root=tmp_path / "workspaces"))
    transport = NullService(store=store)
    transport._campaigns_root = lambda: tmp_path / "results"   # noqa: SLF001
    client = TestClient(build_app(transport, mount_mcp=False))

    def _open(request, timeout):
        del timeout
        url = urllib.parse.urlsplit(request.full_url)
        headers = dict(request.header_items())
        path = f"{url.path}?{url.query}" if url.query else url.path
        if request.data is not None:
            resp = client.post(path, content=request.data, headers=headers)
        else:
            resp = client.get(path, headers=headers)
        if resp.status_code >= 400:
            raise urllib.error.HTTPError(request.full_url, resp.status_code, "", {},
                                         _Response(resp.content))
        return _Response(resp.content)

    monkeypatch.setattr(remote_module, "_open", _open)
    return root


def test_a_url_opens_a_campaign_on_the_service(served):
    del served
    assert isinstance(Campaign(URL, token=TEST_TOKEN), RemoteCampaign)
    assert isinstance(open_data(URL, token=TEST_TOKEN), RemoteCampaign)


def test_the_same_calls_give_the_same_rows(served):
    local = Campaign(str(served))
    remote = Campaign(URL, token=TEST_TOKEN)

    assert list(remote.runs["config_name"]) == list(local.runs["config_name"])
    assert list(remote.runs["run_id"]) == list(local.runs["run_id"])

    mine = local.table("landing_error", config="cfg-a", run=1)
    theirs = remote.table("landing_error", config="cfg-a", run=1)
    assert list(theirs["error"]) == list(mine["error"]) == [0.9]

    with_params = remote.table("landing_error", with_params=True)
    assert "param_wind" in with_params.columns and len(with_params) == 4

    query = "SELECT config_name, avg(error) AS e FROM landing_error GROUP BY 1 ORDER BY 1"
    assert remote.sql(query)["e"].round(6).tolist() == local.sql(query)["e"].round(6).tolist()
    assert read_table(URL, "landing_error", token=TEST_TOKEN)["error"].notna().all()


def test_the_catalog_lists_what_the_service_can_build(served):
    del served
    tables = Campaign(URL, token=TEST_TOKEN).tables
    row = tables.set_index("name").loc["landing_error"]
    assert (row["kind"], row["runs"]) == ("table", 4)
    assert "campaign.campaign" in set(tables["name"])


def test_a_refused_query_is_a_query_error(served):
    del served
    with pytest.raises(QueryError):
        Campaign(URL, token=TEST_TOKEN).sql("DELETE FROM runs")


def test_a_wrong_token_says_how_to_get_the_right_one(served):
    del served
    with pytest.raises(PermissionError, match="token="):
        _ = Campaign(URL, token="not-the-token").runs


def test_what_a_service_does_not_serve_is_refused_by_name(served):
    del served
    campaign = Campaign(URL, token=TEST_TOKEN)
    with pytest.raises(NotImplementedError, match="download the campaign"):
        campaign.config("cfg-a")
    with pytest.raises(QueryError, match="no parameters"):
        campaign.sql("SELECT * FROM runs WHERE run_id = ?", [0])


def test_a_url_that_names_no_campaign_is_refused_before_any_request():
    with pytest.raises(ValueError, match="/campaigns/<campaign_id>"):
        Campaign("https://robovast.example.org/", token=TEST_TOKEN)
