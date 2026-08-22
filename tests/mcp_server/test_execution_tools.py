# Copyright (C) 2026 Frederik Pasch
#
# SPDX-License-Identifier: Apache-2.0

"""Tests for the ``campaign_control`` MCP tools.

The control tools are a **strict client** of a running ``robovast-service``: there
is no local subprocess path. These tests cover the read-only preview tool, routing
through a fake service client, and
that every control tool fails loudly when no service is reachable.
"""

from pathlib import Path
from types import SimpleNamespace

import pytest

from robovast.mcp_server import service_access
from robovast.mcp_server.plugins import authoring, execution, results_lifecycle

_REPO_ROOT = Path(__file__).resolve().parents[2]
_GROWTH_SIM = _REPO_ROOT / "configs" / "examples" / "growth_sim" / "growth_sim.vast"


# -- preview (read-only, serviceless) ---------------------------------------


@pytest.mark.skipif(not _GROWTH_SIM.exists(),
                    reason="growth_sim example not present")
def test_preview_configurations_resolves_without_running():
    """preview_configurations returns resolved per-cell parameters, no execution."""
    result = authoring.preview_configurations(str(_GROWTH_SIM))
    assert "error" not in result
    assert result["configs"] == len(result["configurations"])
    assert result["total_trials"] == result["configs"] * result["runs_per_config"]
    first = result["configurations"][0]
    assert set(first) == {"name", "parameters"}
    assert isinstance(first["parameters"], dict) and first["parameters"]
    # Truncation caps the returned list but keeps the true total.
    capped = authoring.preview_configurations(str(_GROWTH_SIM), limit=1)
    assert len(capped["configurations"]) == 1
    assert capped["truncated"] is True
    assert capped["configs"] == result["configs"]


def test_preview_configurations_bad_path_returns_error():
    assert "error" in authoring.preview_configurations("/no/such/file.vast")


# -- which lane answered: the workspace, or a file on this host ---------------
#
# An absolute filesystem path and a ``/sources`` address both start with ``/``. Telling
# them apart by parsing, not by a prefix test, is what keeps ``/home/me/x.vast`` from
# being sent to the service as workspace ``home``.


class _FakeAuthoringClient:
    """A service stand-in for the workspace lane, recording what it was asked."""

    def __init__(self):
        self.calls = []

    def list_workspaces(self):
        from robovast.service.interface import ListWorkspacesResponse, WorkspaceInfo
        return ListWorkspacesResponse(
            workspaces=[WorkspaceInfo(workspace_id="ws-ab12", name="demo")])

    def validate_project(self, workspace_id, path=""):
        from robovast.service.interface import ValidationReport
        self.calls.append(("validate_project", workspace_id, path))
        return ValidationReport(valid=True, configs=3, runs_per_config=2,
                                total_trials=6)

    def preview_configurations(self, workspace_id, max_configs=0, path=""):
        from robovast.service.interface import PreviewConfiguration, PreviewResponse
        self.calls.append(("preview_configurations", workspace_id, max_configs, path))
        return PreviewResponse(
            configs=1, runs_per_config=2, total_trials=2,
            configurations=[PreviewConfiguration(
                name="cell-0", parameters={"growth_rate": 0.1},
                previews=[{"variation_type": "not for an MCP caller"}])])


@pytest.fixture
def authoring_service(monkeypatch):
    fake = _FakeAuthoringClient()
    monkeypatch.setattr(service_access, "client_or_local", lambda: fake)
    return fake


def test_validate_routes_a_sources_address_to_the_service(authoring_service):
    """With a cluster service the workspace is on no local disk — only it can answer."""
    report = authoring.validate_project("/sources/ws-ab12/demo.vast")
    assert report["valid"] is True and report["lane"] == "workspace"
    assert ("validate_project", "ws-ab12", "demo.vast") in authoring_service.calls


def test_validate_resolves_a_workspace_name(authoring_service):
    """A name works where an id does, as it does for every workspace-taking tool."""
    authoring.validate_project("/sources/demo/demo.vast")
    assert ("validate_project", "ws-ab12", "demo.vast") in authoring_service.calls


@pytest.mark.skipif(not _GROWTH_SIM.exists(),
                    reason="growth_sim example not present")
def test_validate_reads_a_filesystem_path_locally(authoring_service):
    """A bare path is authoring before a workspace exists; it must not reach the service."""
    report = authoring.validate_project(str(_GROWTH_SIM))
    assert report["lane"] == "local file"
    assert not authoring_service.calls


def test_validate_refuses_a_results_address(authoring_service):
    """Results are immutable and are not a project; say so instead of half-answering."""
    report = authoring.validate_project("/results/camp-2026-01-01-000000/_config/x.vast")
    assert report["valid"] is False
    assert "immutable" in report["problems"][0]["message"]
    assert not authoring_service.calls


def test_preview_strips_web_previews_and_reports_the_lane(authoring_service):
    """``previews`` is Module-Federation asset data for the web UI, not for a caller."""
    result = authoring.preview_configurations("/sources/ws-ab12/demo.vast", limit=5)
    assert result["lane"] == "workspace"
    assert result["configurations"] == [{"name": "cell-0",
                                         "parameters": {"growth_rate": 0.1}}]
    assert ("preview_configurations", "ws-ab12", 5, "demo.vast") \
        in authoring_service.calls


# -- client-server routing (a reachable service) ----------------------------


class _FakeClient:
    """A RobovastInterface stand-in recording calls, for the service path."""

    def __init__(self):
        self.calls = []

    def create_campaign(self, request):
        from robovast.service.interface import CampaignRef
        self.calls.append(("create_campaign", request))
        return CampaignRef(campaign_id="svc-campaign-1")

    def retrigger_campaign(self, campaign_id):
        from robovast.service.interface import CampaignRef
        self.calls.append(("retrigger_campaign", campaign_id))
        return CampaignRef(campaign_id="svc-campaign-2")

    def get_status(self, campaign_id):
        from robovast.service.interface import Status
        self.calls.append(("get_status", campaign_id))
        return Status(phase="running", campaign_id=campaign_id,
                      runs={"completed": 3, "total": 8})

    def stop(self, campaign_id):
        from robovast.service.interface import ActionResult
        self.calls.append(("stop", campaign_id))
        return ActionResult(ok=True, message="stop requested")

    def stop_job(self, campaign_id, job_name, reason=None, source="api"):
        from robovast.service.interface import ActionResult
        self.calls.append(("stop_job", campaign_id, job_name, reason, source))
        return ActionResult(ok=True, message=f"killed job {job_name}")

    def list_campaigns(self, request=None):
        from robovast.service.interface import CampaignSummary, ListCampaignsResponse
        self.calls.append(("list_campaigns", request))
        return ListCampaignsResponse(total=2, campaigns=[
            CampaignSummary(campaign_id="svc-running", phase="running"),
            CampaignSummary(campaign_id="svc-done", phase="finished")])

    def resource_usage(self):
        from robovast.service.interface import ResourceUsage
        self.calls.append(("resource_usage",))
        return ResourceUsage(backend="docker", cpu_capacity=8, cpu_used=1,
                             memory_capacity_bytes=16, memory_used_bytes=2,
                             parallel_runs=False)

    def build_image(self, request):
        from robovast.service.interface import ImageBuildRef
        self.calls.append(("build_image", request))
        return ImageBuildRef(build_id="b1", tag="t", cached=True)


@pytest.fixture
def service(monkeypatch):
    """Route the control tools through a fake robovast-service client."""
    fake = _FakeClient()
    monkeypatch.setattr(service_access, "service_client", lambda: fake)
    return fake


def test_service_start_routes_to_client(service):
    started = execution.start_campaign(config_filter="hospital*", runs=5)
    assert started == {"campaign_id": "svc-campaign-1",
                       # The launch hands back the command that waits for it, id
                       # filled in. Left to the tool description alone it was simply not
                       # run — the whole reason a campaign's end went unnoticed. Its exact
                       # wording is tested in test_wait_tools; here it must just be there.
                       "next_step": execution._wait_next_step("svc-campaign-1")}
    name, req = service.calls[-1]
    assert name == "create_campaign"
    assert req.config_filter == "hospital*" and req.runs == 5


def test_from_campaign_retriggers_instead_of_creating(service):
    out = execution.start_campaign(from_campaign="pilot-2026-08-08-120000")
    assert out == {"campaign_id": "svc-campaign-2",
                   "retriggered_from": "pilot-2026-08-08-120000",
                   "next_step": execution._wait_next_step("svc-campaign-2")}
    name, cid = service.calls[-1]
    assert name == "retrigger_campaign" and cid == "pilot-2026-08-08-120000"


@pytest.mark.parametrize("kwargs", [
    {"workspace_id": "ws-1"},
    {"config_path": "p.vast"},
    {"config_filter": "config1*"},
    {"runs": 5},
    {"campaign_name": "again"},
    {"upload_to_share": True},
    {"show_gui": True},
    {"description": "retrying the flake"},
])
def test_from_campaign_refuses_arguments_it_would_have_to_ignore(service, kwargs):
    """A retrigger takes these from the record, so accepting them would answer a different
    question than the caller asked while looking like it had worked. That is not hypothetical:
    ``runs`` being silently substituted is how a 25-trial sweep once "succeeded" with 5.
    """
    out = execution.start_campaign(from_campaign="pilot-2026-08-08-120000", **kwargs)
    assert "error" in out
    assert next(iter(kwargs)) in out["error"]
    # Refused before anything was launched.
    assert not any(name == "retrigger_campaign" for name, _ in service.calls)


def test_service_start_passes_description(service):
    execution.start_campaign(description="pilot: 5 reps DWB vs MPPI on open_space")
    _name, req = service.calls[-1]
    assert req.description == "pilot: 5 reps DWB vs MPPI on open_space"


def test_start_without_description_sends_empty(service):
    execution.start_campaign()
    _name, req = service.calls[-1]
    assert req.description == ""


def test_start_unescapes_html_escaped_description(service):
    """A client that HTML-escapes prompt text must not leave "&gt;" in the stored
    description, where it would show up verbatim in list_campaigns and the web UI."""
    execution.start_campaign(description="wheels rebuilt post SIM_SUITE_-&gt;ROQSIM_")
    _name, req = service.calls[-1]
    assert req.description == "wheels rebuilt post SIM_SUITE_->ROQSIM_"


def test_start_description_unescape_is_single_level(service):
    """An escaped ampersand yields the literal entity, not a second round of decoding."""
    execution.start_campaign(description="a &amp;gt; b &amp;&amp; c")
    _name, req = service.calls[-1]
    assert req.description == "a &gt; b && c"


def test_start_rejects_overlong_description(service):
    """Refused before launch, with the limit named — not a pydantic traceback."""
    from robovast.service.interface import DESCRIPTION_MAX_LEN
    res = execution.start_campaign(description="x" * (DESCRIPTION_MAX_LEN + 1))
    assert "error" in res and str(DESCRIPTION_MAX_LEN) in res["error"]
    assert not service.calls  # never reached the service


def test_service_status_maps_from_status_model(service):
    st = execution.get_campaign_status("svc-campaign-1")
    assert st["backend"] == "service"
    assert st["status"] == "running"
    assert st["batch_runs_done"] == 3 and st["batch_runs_total"] == 8


def test_service_stop_routes_to_client(service):
    res = execution.stop_campaign("svc-campaign-1")
    assert res["stopped"] is True and res["status"] == "stopping"
    assert ("stop", "svc-campaign-1") in service.calls


def test_stop_job_routes_to_client_and_names_its_surface(service):
    res = execution.stop_job("svc-campaign-1", "cfgA/1", reason="wedged in recovery")
    assert res["stopped"] is True and res["job_name"] == "cfgA/1"
    # ``source`` is what the record shows months later, so the tool stamps its own name
    # rather than leaving the service to guess which client called.
    assert ("stop_job", "svc-campaign-1", "cfgA/1", "wedged in recovery", "mcp") \
        in service.calls


def test_stop_job_sends_no_reason_rather_than_an_empty_one(service):
    """An omitted reason must reach the record as absent, not as ``""``."""
    execution.stop_job("svc-campaign-1", "cfgA/1")
    assert ("stop_job", "svc-campaign-1", "cfgA/1", None, "mcp") in service.calls


def test_stop_job_reports_a_refusal_as_an_error(monkeypatch):
    """The service refuses a job that is not running; the tool must not swallow that."""
    class _Refusing(_FakeClient):
        def stop_job(self, campaign_id, job_name, reason=None, source="api"):
            raise RuntimeError(f"job {job_name!r} is completed, not running")

    monkeypatch.setattr(service_access, "service_client", lambda: _Refusing())
    res = execution.stop_job("svc-campaign-1", "cfgA/0")
    assert "not running" in res["error"]


def test_stop_job_without_a_service_says_so(monkeypatch):
    monkeypatch.setattr(service_access, "service_client", lambda: None)
    assert "error" in execution.stop_job("svc-campaign-1", "cfgA/0")


# -- fail loudly when no service is reachable (no local fallback) ------------


@pytest.fixture
def no_service(monkeypatch):
    monkeypatch.setattr(service_access, "service_client", lambda: None)


def test_start_without_service_fails_loudly(no_service):
    res = execution.start_campaign()
    assert "error" in res and "no robovast-service" in res["error"]


def test_status_without_service_fails_loudly(no_service):
    assert "no robovast-service" in execution.get_campaign_status("x")["error"]


def test_stop_without_service_fails_loudly(no_service):
    assert "no robovast-service" in execution.stop_campaign("x")["error"]


# -- the download link follows the campaign's lane, not the service's default ---------


@pytest.fixture
def dual_lane(monkeypatch):
    """A service whose reported default lane is not the lane a campaign ran on.

    Kept after the lane branching was removed, because "the answer does not depend on the
    lane" is only worth asserting against a service where the two disagree.
    """
    from robovast.mcp_server import data_access

    class _Dual:
        base_url = "http://127.0.0.1:8800"

        def version(self):
            from robovast.service.interface import VersionInfo

            return VersionInfo(robovast_version="x", backend="docker",
                               backends=["local", "cluster"])

    monkeypatch.setattr(service_access, "service_client", _Dual)
    recorded = {}

    def _rows(campaign_id, sql, max_rows=5000):
        return [{"execution_type": recorded[campaign_id]}] if campaign_id in recorded \
            else []
    monkeypatch.setattr(data_access, "rows", _rows)
    return recorded


@pytest.mark.parametrize("lane", ["cluster", "local", None])
def test_download_offers_the_url_whatever_lane_the_campaign_ran_on(dual_lane, lane):
    """Every lane serves the archive, so the tool no longer asks which one ran it.

    Two bugs died with that question, and this is here so neither can come back. It used to
    read the *service's* default backend, so on a dev host every cluster campaign was told its
    results were on the local filesystem — a real capability denied and a place to look that
    holds nothing. Reading the campaign's own record fixed that but kept the branch, which
    then denied the download to genuinely local campaigns; those are now served too (the local
    lane tars its own results directory), so the branch had nothing left to decide.

    ``lane=None`` is a campaign with no execution record yet: previously the case that fell
    back to the service's default, and now simply not a question that gets asked.
    """
    if lane:
        dual_lane["camp-x"] = lane
    result = results_lifecycle.get_campaign_download("camp-x")
    assert result["url"].endswith("/campaigns/camp-x/archive")
    assert "error" not in result


def test_resource_usage_without_service_fails_loudly(no_service):
    assert "no robovast-service" in execution.get_resource_usage()["error"]


# -- get_campaign_download — returns a web link, never writes a file ---------


def _fake_download_client(backend, base_url="http://127.0.0.1:8800"):
    from robovast.service.interface import VersionInfo
    return SimpleNamespace(
        base_url=base_url,
        version=lambda: VersionInfo(robovast_version="test", backend=backend))


def test_get_campaign_download_cluster_returns_url(monkeypatch):
    monkeypatch.setattr(service_access, "service_client",
                        lambda: _fake_download_client("kubernetes"))
    res = results_lifecycle.get_campaign_download("camp-2026-01-01-000000")
    assert res["url"] == "http://127.0.0.1:8800/campaigns/camp-2026-01-01-000000/archive"
    assert res["path"] == "/campaigns/camp-2026-01-01-000000/archive"
    assert res["next_step"] == "vast results download camp-2026-01-01-000000"
    assert "error" not in res


def test_get_campaign_download_local_also_returns_a_url(monkeypatch):
    """A local service serves the archive too, so the two lanes answer identically."""
    monkeypatch.setattr(service_access, "service_client",
                        lambda: _fake_download_client("docker"))
    res = results_lifecycle.get_campaign_download("camp-2026-01-01-000000")
    assert res["url"] == "http://127.0.0.1:8800/campaigns/camp-2026-01-01-000000/archive"


def test_get_campaign_download_says_nothing_about_the_share(monkeypatch):
    """Whether a share copy exists is not a fact this service records.

    The note used to name ``vast share download`` unconditionally, but a campaign only has
    a share copy if it was uploaded to one -- ``upload_to_share`` is a create-time request
    flag, and the only thing travelling with a campaign afterwards is ``share_error``, a
    failure. Advertising the copy anyway is AGENTS.md §4's "capability the caller cannot
    use", and the honest check (``list_share_archives``) is a different system with
    different credentials.
    """
    monkeypatch.setattr(service_access, "service_client",
                        lambda: _fake_download_client("kubernetes"))
    res = results_lifecycle.get_campaign_download("camp-2026-01-01-000000")
    assert "share" not in " ".join(str(v) for v in res.values())


def test_get_campaign_download_without_a_transport_omits_the_url(monkeypatch):
    """The mounted MCP holds the implementation, which has no ``base_url`` to read.

    The regression this exists for: the tool read ``client.base_url`` directly, so on the
    deployment that mounts the MCP inside the service -- every published one -- it raised
    ``AttributeError`` instead of answering. Every fake here had a ``base_url``, which is
    the one shape production never has.
    """
    from robovast.service.interface import VersionInfo
    impl = SimpleNamespace(          # no base_url, and nothing declared
        version=lambda: VersionInfo(robovast_version="test", backend="kubernetes"))
    monkeypatch.setattr(service_access, "service_client", lambda: impl)
    res = results_lifecycle.get_campaign_download("camp-2026-01-01-000000")
    assert "url" not in res          # omitted, not empty
    assert res["path"] == "/campaigns/camp-2026-01-01-000000/archive"
    assert res["next_step"] == "vast results download camp-2026-01-01-000000"
    assert "error" not in res


def test_get_campaign_download_no_service_errors(monkeypatch):
    monkeypatch.setattr(service_access, "service_client", lambda: None)
    res = results_lifecycle.get_campaign_download("camp-2026-01-01-000000")
    assert "error" in res and "no robovast-service" in res["error"]


# -- get_campaign_log is served by the service, not by the local results dir ------


def test_get_campaign_log_is_served_by_the_service(monkeypatch):
    """The campaign log comes from the service, which knows where it lives.

    Regression: this tool read the local results dir directly, so on a cluster
    service -- where the durable log is in the object store and the live one is pod
    scratch -- it reported an empty log even though
    ``ClusterService.get_campaign_logs`` already served both.
    """

    class _Chunk:
        text = "===== RUN =====\nline one\nline two\n"

    class _Fake:
        def __init__(self):
            self.asked = None

        def get_campaign_logs(self, campaign_id, offset=0):
            self.asked = (campaign_id, offset)
            return _Chunk()

    fake = _Fake()
    monkeypatch.setattr(service_access, "service_client", lambda: fake)
    # Would raise if the local disk path were taken: no such campaign anywhere.
    monkeypatch.setattr(
        execution.results_resolver, "resolve_campaign_path",
        lambda *a, **k: pytest.fail("must not read the local results dir"))

    out = execution.get_campaign_log("camp-2026-01-01-000000")
    assert fake.asked == ("camp-2026-01-01-000000", 0)
    assert "line one" in out["content"]
    assert out["total_lines"] == 3


def test_get_campaign_log_falls_back_to_disk_with_no_service(monkeypatch, tmp_path):
    """With no service, an archived results tree is still readable offline."""

    campaign = tmp_path / "camp-2026-01-01-000000"
    (campaign / "_execution").mkdir(parents=True)
    (campaign / "_execution" / "controller.log").write_text("from disk\n")

    monkeypatch.setattr(service_access, "service_client", lambda: None)
    monkeypatch.setattr(execution.results_resolver, "resolve_campaign_path",
                        lambda *a, **k: campaign)
    assert "from disk" in execution.get_campaign_log("camp-2026-01-01-000000")["content"]


# -- the hanging run, end to end -------------------------------------------------
#
# The incident this closes: a campaign reported ``running, progress: 0`` indefinitely
# while its bridge rejected TF wholesale. A severity grep matched 18226 times and the
# returned lines read as ordinary noise, so the count -- the actual finding -- was
# never seen.


def _flooded_log(n=18226):
    """A campaign log dominated by one repeated warning, as in the incident."""
    return "===== RUN =====\n" + "".join(
        f"robovast  | [WARN] [17850922{i:05d}.1] [tf_bridge]: TF_OLD_DATA ignoring "
        f"data from the past for frame base_link at time {i}.5\n"
        for i in range(n))


def _service_with_log(monkeypatch, text):

    class _Chunk:
        def __init__(self, text):
            self.text = text
            self.next_offset = len(text)
            self.eof = False

        def model_dump(self):
            return {"text": self.text, "next_offset": self.next_offset,
                    "eof": self.eof}

    class _Fake:
        def get_campaign_logs(self, campaign_id, offset=0):
            return _Chunk(text)

        def get_job_log(self, campaign_id, job_name, offset=0):
            return _Chunk(text)

    monkeypatch.setattr(service_access, "service_client", lambda: _Fake())


def test_a_flooded_campaign_log_summarizes_to_one_counted_line(monkeypatch):
    """18226 lines of noise for ~20 tokens, with the count as the finding."""

    _service_with_log(monkeypatch, _flooded_log())
    out = execution.get_campaign_log("camp-2026-01-01-000000", summarize=True)
    # The flood, plus the ``===== RUN =====`` phase divider — which is a real line in
    # the assembled stream and says which phase the flood is in, so it is counted
    # rather than filtered.
    assert out["patterns_total"] == 2
    assert out["patterns"][0]["count"] == 18226
    assert "TF_OLD_DATA" in out["patterns"][0]["pattern"]
    assert out["severity_counts"]["warn"] == 18226
    # A summary is not a shorter log: the line keys must not linger.
    assert "content" not in out and "returned_lines" not in out


def test_a_flooded_job_log_summarizes_and_keeps_the_poll_offset(monkeypatch):
    """`next_offset` refers to the unfiltered stream, so summarizing must not break
    an incremental poll loop."""

    text = _flooded_log(50)
    _service_with_log(monkeypatch, text)
    out = execution.get_job_log("camp-2026-01-01-000000", "job-0", summarize=True)
    assert out["patterns"][0]["count"] == 50
    assert out["next_offset"] == len(text)
    assert "text" not in out


def test_min_severity_uses_the_shared_classifier_not_a_hand_written_grep(monkeypatch):
    """An INFO line mentioning "error" must not be returned as an error -- which is
    exactly what the hand-written severity grep in the interim procedure did."""

    _service_with_log(monkeypatch, "===== RUN =====\n"
                      "[INFO] [1.0] [nav2]: error_code: 0, goal reached\n"
                      "[ERROR] [2.0] [ctrl]: Failed to make progress\n")
    out = execution.get_campaign_log("camp-2026-01-01-000000", min_severity="error")
    assert "Failed to make progress" in out["content"]
    assert "goal reached" not in out["content"]


def test_an_invalid_filter_is_reported_rather_than_silently_ignored(monkeypatch):

    _service_with_log(monkeypatch, "===== RUN =====\nline\n")
    cid = "camp-2026-01-01-000000"
    assert "unknown severity" in execution.get_campaign_log(
        cid, min_severity="critical")["error"]
    assert "not a valid regular expression" in execution.get_campaign_log(
        cid, grep="[")["error"]


# -- the status now carries the liveness signal ----------------------------------


def test_the_status_reports_a_stall_and_names_the_next_call():
    """The status must be able to say "unhealthy" on its own; previously the only way
    to learn it was to know which log to grep."""
    import time

    from robovast.client.status import Status

    st = Status(phase="running", mode="batch", runs={"completed": 0, "total": 10},
                progress_deadline_s=600, progress_since=time.time() - 700)
    out = execution._status_to_dict("camp", "service", st)
    assert out["stalled"] is True
    assert out["progress_age_s"] >= 700
    assert "summarize=True" in out["stall_reason"]


def test_the_status_says_it_cannot_judge_rather_than_saying_healthy():
    """Without a declared ``execution.timeout`` the tool must return ``stalled: null``
    and say why -- ``false`` would read as a clean bill of health for a run that has
    not moved in a day."""
    import time

    from robovast.client.status import Status

    st = Status(phase="running", mode="batch", runs={"completed": 0, "total": 10},
                progress_since=time.time() - 99999)
    out = execution._status_to_dict("camp", "service", st)
    assert out["stalled"] is None
    assert "execution.timeout" in out["stall_verdict"]
    assert out["progress_age_s"] > 0


# -- the BUILD phase of the campaign log ------------------------------------
#
# A campaign that waits for an experiment image copies that build's output into its own
# log, so it is reachable with the campaign id alone and survives the build Job's TTL.
# It is part of a default read like every other phase: it was once held back as a copy of
# shared work, which left a still-building campaign answering "what are you doing?" with
# an empty log. Narrowing (``phase=``) is the caller's move, not the default's.

def _log_with_build(build_lines=400):
    from robovast.common.campaign_logs import phase_banner

    # Shaped like real BuildKit output: one repeated step whose only variation is
    # standalone numbers, so the summarizer collapses it the way it collapses a run's
    # flood. (A trailing token like ``pkg7`` would not normalize — numbers glued to a
    # word are part of the word.)
    build = "".join(f"#{i} {i}.5 Unpacking libfoo over ({i}) ...\n"
                    for i in range(build_lines))
    return (phase_banner("BUILD") + f"waiting for image sim:v3 (build b-1)\n{build}"
            + phase_banner("RUN") + "batch 0 starting\nrun 0 finished\n")


def test_a_default_read_includes_the_build_section(monkeypatch):
    """Every phase is in a default read, each announced with its size — so nothing is left
    out silently and no caller has to know a selector exists to see the whole log."""

    _service_with_log(monkeypatch, _log_with_build())
    out = execution.get_campaign_log("camp-2026-01-01-000000", limit=10_000)

    assert "waiting for image sim:v3 (build b-1)" in out["content"]
    assert "batch 0 starting" in out["content"]
    build = next(p for p in out["phases"] if p["name"] == "BUILD")
    assert build["included"] is True and build["lines"] == 401
    assert next(p for p in out["phases"] if p["name"] == "RUN")["included"] is True


def test_a_campaign_that_is_still_building_reads_its_build(monkeypatch):
    """The reason the aside was dropped: BUILD is the *only* section a campaign waiting
    for its image has, so holding it back answered "what is this doing?" with nothing."""
    from robovast.common.campaign_logs import phase_banner

    only_build = phase_banner("BUILD") + "waiting for image sim:v3 (build b-1)\n"
    _service_with_log(monkeypatch, only_build)
    out = execution.get_campaign_log("camp-2026-01-01-000000")

    assert "waiting for image sim:v3 (build b-1)" in out["content"]
    assert next(p for p in out["phases"] if p["name"] == "BUILD")["included"] is True


def test_the_build_section_is_readable_on_request(monkeypatch):

    _service_with_log(monkeypatch, _log_with_build())
    out = execution.get_campaign_log("camp-2026-01-01-000000", phase="build")

    assert "waiting for image sim:v3 (build b-1)" in out["content"]
    assert "batch 0 starting" not in out["content"]
    assert next(p for p in out["phases"] if p["name"] == "RUN")["included"] is False


def test_a_noisy_build_composes_with_summarize(monkeypatch):
    """400 near-identical layer lines for a handful of tokens — the intended read of a
    build that is misbehaving."""

    _service_with_log(monkeypatch, _log_with_build())
    out = execution.get_campaign_log("camp-2026-01-01-000000", phase="build",
                                     summarize=True)
    top = max(out["patterns"], key=lambda p: p["count"])
    assert top["count"] == 400


def test_phase_all_returns_the_stream_verbatim(monkeypatch):

    text = _log_with_build()
    _service_with_log(monkeypatch, text)
    out = execution.get_campaign_log("camp-2026-01-01-000000", phase="all", limit=10_000)
    # Sections tile the stream, so selecting them all reproduces it.
    assert out["content"] == text.rstrip("\n")


def test_an_unknown_phase_is_reported_not_ignored(monkeypatch):
    """A silently ignored selector would read as "that phase produced nothing"."""

    _service_with_log(monkeypatch, _log_with_build())
    out = execution.get_campaign_log("camp-2026-01-01-000000", phase="biuld")
    assert "unknown phase" in out["error"]


def test_status_names_a_killed_run_apart_from_a_lost_one(monkeypatch):
    """``batch_runs_killed`` distinguishes a deliberate stop from a run that vanished.

    A killed run is counted inside ``no_result`` (it delivered nothing), so without its own
    field the reader sees only "1 without result" and goes hunting for a fault that is not
    there. Omitted entirely when nothing was killed.
    """
    class _Killed(_FakeClient):
        def get_status(self, campaign_id):
            from robovast.service.interface import Status
            self.calls.append(("get_status", campaign_id))
            return Status(phase="finished", campaign_id=campaign_id,
                          runs={"completed": 2, "total": 3, "no_result": 1,
                                "failed": 0, "killed": 1})

    monkeypatch.setattr(service_access, "service_client", lambda: _Killed())
    status = execution.get_campaign_status("svc-campaign-1")
    assert status["batch_runs_killed"] == 1
    assert status["batch_runs_failed"] == 0, "a kill is not a failed trial"


def test_status_omits_the_killed_count_when_nothing_was_killed(service):
    assert "batch_runs_killed" not in execution.get_campaign_status("svc-campaign-1")
