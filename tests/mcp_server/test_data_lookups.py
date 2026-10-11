# Copyright (C) 2026 Frederik Pasch
# SPDX-License-Identifier: Apache-2.0
"""A lookup that failed is reported with its cause; only data that is not there reads empty."""

import pytest

from robovast.common.store import STORE_FILENAME, CampaignStore
from robovast.mcp_server import data_access, service_access
from robovast.mcp_server.plugins import results
from robovast.results_processing.data_query import DataQueryError
from robovast.service.interface import ServiceUnreachable
from tests.mcp_server.conftest import registered_tools
from tests.mcp_server.test_sql_replacements import _SYSINFO, _write_batch_campaign


@pytest.fixture(name="no_service")
def _no_service(monkeypatch):
    monkeypatch.setattr(service_access, "service_client", lambda: None)


@pytest.fixture(name="unreachable")
def _unreachable(monkeypatch):
    class _Down:
        def query_campaign_data_sql(self, *_args, **_kwargs):
            raise ServiceUnreachable("http://service.example", "connection refused")

    monkeypatch.setattr(service_access, "service_client", _Down)


@pytest.fixture(name="missing")
def _missing(tmp_path):
    """A campaign directory that is not there."""
    return str(tmp_path / "gone")


@pytest.fixture(name="empty_campaign")
def _empty_campaign(tmp_path):
    """A campaign that was created and recorded no run."""
    root = tmp_path / "empty"
    root.mkdir()
    with CampaignStore(root / STORE_FILENAME) as store:
        store.create_campaign("c", {"execution": {"runs": 1}}, mode="batch",
                              config_dir="_config")
    return str(root)


# -- rows() --------------------------------------------------------------------------------

def test_rows_raises_when_the_campaign_cannot_be_found(no_service, missing):
    with pytest.raises(ValueError, match="not found") as excinfo:
        data_access.rows(missing, "SELECT 1")
    assert not isinstance(excinfo.value, DataQueryError)


def test_rows_raises_when_the_service_does_not_answer(unreachable):
    with pytest.raises(ServiceUnreachable):
        data_access.rows("c1", "SELECT 1")


def test_rows_refuses_a_table_the_campaign_does_not_have(no_service, tmp_path):
    campaign = str(_write_batch_campaign(tmp_path / "c", _SYSINFO))
    with pytest.raises(DataQueryError):
        data_access.rows(campaign, "SELECT * FROM videos")


def test_rows_is_empty_only_for_a_query_that_matched_nothing(no_service, empty_campaign):
    assert data_access.rows(empty_campaign, "SELECT config_name FROM run_view") == []


# -- get_campaign_summary ------------------------------------------------------------------

def test_a_summary_of_a_campaign_that_is_not_there_says_so(no_service, missing):
    answer = results.get_campaign_summary(missing)
    assert answer == {"error": f"Campaign directory not found: {missing}"}


def test_a_summary_whose_service_does_not_answer_says_so(unreachable):
    answer = results.get_campaign_summary("c1")
    assert service_access.NO_SERVICE in answer["error"]
    assert "No run data" not in answer["error"]


def test_a_campaign_with_no_runs_is_summarised_as_one(no_service, empty_campaign):
    answer = results.get_campaign_summary(empty_campaign)
    assert answer["error"].startswith("No run data for campaign")


def test_a_campaign_with_runs_is_summarised(no_service, tmp_path):
    campaign = str(_write_batch_campaign(tmp_path / "c", _SYSINFO))
    answer = results.get_campaign_summary(campaign)
    assert "error" not in answer
    assert answer["num_runs"] == 4 and answer["num_failed"] == 1


# -- get_camera_frame ----------------------------------------------------------------------

def _frame(*args) -> dict:
    """``get_camera_frame`` as registered; every case here answers the error document."""
    return registered_tools()["get_camera_frame"].fn(*args)


def test_a_frame_of_a_campaign_that_is_not_there_says_so(no_service, missing):
    answer = _frame(missing, "cfg-a", 0)
    assert answer == {"error": f"Campaign directory not found: {missing}"}


def test_a_frame_whose_service_does_not_answer_says_so(unreachable):
    answer = _frame("c1", "cfg-a", 0)
    assert service_access.NO_SERVICE in answer["error"]
    assert "registered no video" not in answer["error"]


def test_a_run_that_recorded_no_video_says_so(no_service, tmp_path):
    campaign = str(_write_batch_campaign(tmp_path / "c", _SYSINFO))
    answer = _frame(campaign, "cfg-a", 0)
    assert "registered no video" in answer["error"]
