# Copyright (C) 2026 Frederik Pasch
# SPDX-License-Identifier: Apache-2.0
"""LocalTransport.list_jobs / get_job_log — the mode-1/2 (Docker) job view.

Local runs land on disk as ``<campaign>/<config>/<run>/`` with the run's console
tee'd to ``logs/system.log``. A "job" is a run: ``completed``/``failed`` by its
``test.xml``, ``running`` when the campaign is still live and it has none yet. The
per-job log is that ``system.log`` read from a byte offset — no cluster needed.
"""

from pathlib import Path

import pytest

from robovast.service.client import LocalTransport
from robovast.service.workspaces import WorkspaceRegistry, WorkspaceStore


@pytest.fixture
def transport(monkeypatch, tmp_path):
    monkeypatch.setattr(
        "robovast.common.cli.project_config.ProjectConfig.load",
        staticmethod(lambda *a, **k: None))
    store = WorkspaceStore(registry=WorkspaceRegistry(root=tmp_path / "workspaces"))
    return LocalTransport(store=store)


_PASS_XML = ('<testsuite errors="0" failures="0" tests="1">'
             '<testcase time="1.0"/></testsuite>')
_FAIL_XML = ('<testsuite errors="0" failures="1" tests="1">'
             '<testcase time="1.0"/></testsuite>')


def _run(campaign_dir: Path, config: str, run: str, *, xml=None, log=None) -> Path:
    run_dir = campaign_dir / config / run
    (run_dir / "logs").mkdir(parents=True)
    if xml is not None:
        (run_dir / "test.xml").write_text(xml)
    if log is not None:
        (run_dir / "logs" / "system.log").write_text(log)
    return run_dir


def test_list_jobs_classifies_runs(transport):
    cid = "campaign-2026-07-17-120000"
    cdir = transport._campaigns_root() / cid
    _run(cdir, "cfgA", "0", xml=_PASS_XML)
    _run(cdir, "cfgA", "1", xml=_FAIL_XML)
    _run(cdir, "cfgB", "0")  # no test.xml, campaign not live → failed/incomplete

    resp = transport.list_jobs(cid)
    by_name = {j.job_name: j.status for j in resp.jobs}
    assert by_name == {"cfgA/0": "completed", "cfgA/1": "failed", "cfgB/0": "failed"}
    assert resp.counts.completed == 1 and resp.counts.failed == 2
    assert resp.counts.total == 3
    # display name is human friendly
    assert any(j.display_name == "cfgA · run 0" for j in resp.jobs)


def test_list_jobs_ignores_reserved_dirs(transport):
    cid = "campaign-2026-07-17-121000"
    cdir = transport._campaigns_root() / cid
    _run(cdir, "cfgA", "0", xml=_PASS_XML)
    for reserved in ("_config", "_execution", "_transient"):
        (cdir / reserved / "0").mkdir(parents=True)

    resp = transport.list_jobs(cid)
    assert [j.job_name for j in resp.jobs] == ["cfgA/0"]


def test_get_job_log_reads_system_log_from_offset(transport):
    cid = "campaign-2026-07-17-122000"
    cdir = transport._campaigns_root() / cid
    _run(cdir, "cfgA", "0", xml=_PASS_XML, log="line1\nline2\n")

    chunk = transport.get_job_log(cid, "cfgA/0")
    assert chunk.text == "line1\nline2\n"
    assert chunk.next_offset == len(b"line1\nline2\n")
    assert chunk.eof is True  # run finished (has test.xml) and campaign not live

    tail = transport.get_job_log(cid, "cfgA/0", offset=6)
    assert tail.text == "line2\n"


def test_get_job_log_unknown_job_raises(transport):
    cid = "campaign-2026-07-17-123000"
    (transport._campaigns_root() / cid).mkdir(parents=True)
    with pytest.raises(KeyError):
        transport.get_job_log(cid, "nope/0")


def test_get_job_log_rejects_path_traversal(transport):
    cid = "campaign-2026-07-17-124000"
    (transport._campaigns_root() / cid).mkdir(parents=True)
    with pytest.raises(KeyError):
        transport.get_job_log(cid, "../../etc")
