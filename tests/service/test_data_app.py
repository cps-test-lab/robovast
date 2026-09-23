# Copyright (C) 2026 Frederik Pasch
# SPDX-License-Identifier: Apache-2.0
"""The data plane: tar streams in and out of the results tree, gated by scope.

Two processes serve these routes -- ``vast serve``'s one app and the cluster's separate
data container -- and both are built here, over the same tree, so what one accepts the
other does. The properties: a pod's inputs land flat, carry its own job documents and no
other job's, and a cell's file lands on the campaign's; an upload streams into the campaign and refuses what the driver owns; a
token scoped to one campaign reaches that campaign's routes and nothing else.
"""

import io
import os
import tarfile

import pytest
from fastapi.testclient import TestClient

from robovast.client.status import Phase, Status
from robovast.common.campaign_data import write_execution_outcome
from robovast.service import auth
from robovast.service.app import build_app
from robovast.service.data_app import build_data_app
from robovast.service.interface import Routes
from robovast.service.workspaces import WorkspaceRegistry, WorkspaceStore
from tests.service.null_service import NullService

from .conftest import TEST_TOKEN

_CAMPAIGN = "camp-2026-01-01-000000"
_OTHER = "camp-2026-01-02-000000"
#: What every pod's inputs request carries: the job it is for.
_JOB = {"job": ["job-1"]}


def _campaign(root, campaign_id=_CAMPAIGN):
    campaign = root / campaign_id
    (campaign / "_config").mkdir(parents=True)
    (campaign / "_config" / "campaign.vast").write_text("configuration:\n  name: x\n")
    (campaign / "_config" / "tool.sh").write_text("#!/bin/sh\n")
    os.chmod(campaign / "_config" / "tool.sh", 0o755)
    (campaign / "_transient").mkdir()
    (campaign / "_transient" / "configurations.yaml").write_text("- name: cell-a\n")
    for tag in ("job-1", "job-2", "probe-n1"):
        (campaign / "_transient" / f"{tag}.params.yaml").write_text(f"tag: {tag}\n")
    (campaign / "_transient" / "job-1.sim.yaml").write_text("sim: 1\n")
    (campaign / "_transient" / "job-2.sim.yaml").write_text("sim: 2\n")
    (campaign / "cell-a" / "_config").mkdir(parents=True)
    (campaign / "cell-a" / "_config" / "campaign.vast").write_text("configuration:\n  name: cell-a\n")
    (campaign / "cell-a" / "_config" / "scenario.config").write_text("record\n")
    (campaign / "_execution").mkdir()
    (campaign / "_execution" / "controller.log").write_text("driver's\n")
    return campaign


def _tar(members, *, gz: bool = False) -> bytes:
    """A tar of *members*, plain as a pod sends it, or gzipped with *gz*."""
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz" if gz else "w") as tar:
        for name, payload in members:
            info = tarfile.TarInfo(name)
            info.size = len(payload)
            tar.addfile(info, io.BytesIO(payload))
    return buf.getvalue()


_GZIP_MAGIC = b"\x1f\x8b"


def _names(payload: bytes, *, gz: bool = False):
    """The members of *payload*, which must be gzipped exactly when *gz* says so."""
    assert payload.startswith(_GZIP_MAGIC) == gz, "compressed" if not gz else "plain"
    with tarfile.open(fileobj=io.BytesIO(payload), mode="r:gz" if gz else "r:") as tar:
        return {m.name: m for m in tar.getmembers()}


@pytest.fixture(name="root")
def _root(tmp_path):
    root = tmp_path / "results"
    _campaign(root)
    return root


@pytest.fixture(name="standalone")
def _standalone(root):
    """The data container's app: the routes over the tree, nothing else."""
    with TestClient(build_data_app(root, TEST_TOKEN)) as client:
        yield client


@pytest.fixture(name="mounted")
def _mounted(root, tmp_path):
    """``vast serve``'s one app, with the same routes mounted."""
    store = WorkspaceStore(registry=WorkspaceRegistry(root=tmp_path / "workspaces"))
    lt = NullService(store=store)
    lt._campaigns_root = lambda: root
    with TestClient(build_app(lt, mount_mcp=False)) as client:
        yield client


@pytest.fixture(params=["standalone", "mounted"])
def client(request):
    return request.getfixturevalue(request.param)


def test_inputs_land_flat_and_a_cells_file_lands_on_the_campaigns(client):
    resp = client.get(Routes.campaign_inputs(_CAMPAIGN),
                      params={"job": ["job-1"], "config_file": ["cell-a:campaign.vast"]})
    assert resp.status_code == 200, resp.text
    members = _names(resp.content)
    # `_config/x` is `x`: the pod extracts into its /config.
    assert "campaign.vast" in members and "job-1.params.yaml" in members
    assert not any(n.startswith("_config") for n in members)
    # The cell's copy is the later member, so it wins on extraction.
    with tarfile.open(fileobj=io.BytesIO(resp.content), mode="r:") as tar:
        copies = [m for m in tar.getmembers() if m.name == "campaign.vast"]
        assert len(copies) == 2
        assert tar.extractfile(copies[-1]).read() == b"configuration:\n  name: cell-a\n"
    # The cell's RECORDS are not staged: only what was named.
    assert "scenario.config" not in members
    # The executable bit rides in the tar.
    assert members["tool.sh"].mode & 0o100


def test_a_pod_is_sent_its_own_job_documents_and_no_other_jobs(client):
    """The composer writes a pair of documents per job, so a pod sent every job's would
    download more the larger the campaign; the campaign-wide files still reach every pod."""
    members = _names(client.get(Routes.campaign_inputs(_CAMPAIGN),
                                params={"job": ["job-1"]}).content)
    assert {"job-1.params.yaml", "job-1.sim.yaml", "configurations.yaml"} <= set(members)
    assert not {"job-2.params.yaml", "job-2.sim.yaml", "probe-n1.params.yaml"} & set(members)


def test_a_probe_is_sent_its_own_parameters_and_its_base_jobs_simulator_document(client):
    """A probe runs a real job's manifest with its own parameter document, so it names both."""
    members = _names(client.get(Routes.campaign_inputs(_CAMPAIGN),
                                params={"job": ["job-2", "probe-n1"]}).content)
    assert {"job-2.sim.yaml", "probe-n1.params.yaml"} <= set(members)
    assert "job-1.params.yaml" not in members


def test_inputs_that_name_no_job_are_refused(client):
    assert client.get(Routes.campaign_inputs(_CAMPAIGN)).status_code == 422


def test_inputs_for_a_job_the_campaign_does_not_have_are_a_404(client):
    """Before any byte is sent: an empty stream would start a pod with nothing to run."""
    resp = client.get(Routes.campaign_inputs(_CAMPAIGN), params={"job": ["job-1", "job-9"]})
    assert resp.status_code == 404, resp.text
    assert "job-9" in resp.json()["detail"]


def test_a_job_tag_cannot_reach_outside_the_campaigns_documents(client):
    for tag in ("../job-1", "a/b", ".."):
        resp = client.get(Routes.campaign_inputs(_CAMPAIGN), params={"job": [tag]})
        assert resp.status_code == 400, (tag, resp.text)


def test_outputs_stream_into_the_campaign_and_the_driver_keeps_its_log(client, root):
    payload = _tar([("cell-a/1/test.xml", b"<testsuite/>"),
                    ("cell-a/1/logs/system.log", b"ran\n"),
                    ("_execution/controller.log", b"a stale copy\n"),
                    ("campaign.db", b"not yours")])
    resp = client.put(Routes.campaign_outputs(_CAMPAIGN), content=payload)
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["files"] == 2
    assert sorted(body["refused"]) == ["_execution/controller.log", "campaign.db"]
    assert (root / _CAMPAIGN / "cell-a" / "1" / "test.xml").read_bytes() == b"<testsuite/>"
    assert (root / _CAMPAIGN / "_execution" / "controller.log").read_text() == "driver's\n"


def test_outputs_for_a_campaign_that_is_not_here_are_a_404_before_the_body(client):
    resp = client.put(Routes.campaign_outputs("camp-2026-09-09-000000"),
                      content=_tar([("x", b"y")]))
    assert resp.status_code == 404


def test_a_finished_campaign_still_takes_its_last_pods_outputs(client, root):
    """A stop tears pods down while their uploaders flush; what they flush is evidence."""
    write_execution_outcome(root / _CAMPAIGN, Status(phase=Phase.STOPPED))
    resp = client.put(Routes.campaign_outputs(_CAMPAIGN),
                      content=_tar([("cell-a/2/test.xml", b"<late/>")]))
    assert resp.status_code == 200, resp.text
    assert (root / _CAMPAIGN / "cell-a" / "2" / "test.xml").exists()


def test_a_body_that_is_not_a_tar_is_a_400(client):
    resp = client.put(Routes.campaign_outputs(_CAMPAIGN), content=b"this is not a tar")
    assert resp.status_code == 400, resp.text


def test_the_archive_carries_the_records_and_never_the_table_cache(client, root):
    campaign = root / _CAMPAIGN
    (campaign / "_calibration").mkdir()
    (campaign / "_calibration" / "probe.mcap").write_bytes(b"probe")
    (campaign / ".cache" / "tables").mkdir(parents=True)
    (campaign / ".cache" / "tables" / "poses.parquet").write_bytes(b"PAR1")

    names = _names(client.get(Routes.campaign_archive(_CAMPAIGN)).content, gz=True)
    assert f"{_CAMPAIGN}/_calibration/probe.mcap" in names
    assert f"{_CAMPAIGN}/_config/campaign.vast" in names
    assert not [n for n in names if "/.cache" in n]


def test_the_standalone_plane_reads_liveness_from_the_tree(standalone, root):
    """No registry in the data container: the terminal record is what says a campaign is over."""
    from robovast.execution.campaign_archive import SNAPSHOT_MEMBER
    resp = standalone.get(Routes.campaign_archive(_CAMPAIGN))
    assert "incomplete" in resp.headers["content-disposition"]
    assert f"{_CAMPAIGN}/{SNAPSHOT_MEMBER}" in _names(resp.content, gz=True)

    write_execution_outcome(root / _CAMPAIGN, Status(phase=Phase.FINISHED))
    resp = standalone.get(Routes.campaign_archive(_CAMPAIGN))
    assert resp.headers["content-disposition"] == f'attachment; filename="{_CAMPAIGN}.tar.gz"'
    assert f"{_CAMPAIGN}/{SNAPSHOT_MEMBER}" not in _names(resp.content, gz=True)


def test_a_staged_slot_round_trips_and_is_confined(client, root):
    slot = "image-builds/build-1"
    resp = client.put(Routes.staged(slot), content=_tar([("Dockerfile", b"FROM x\n"),
                                                          ("src/a.py", b"print()\n")]))
    assert resp.status_code == 200, resp.text
    assert (root / "_staged" / slot / "src" / "a.py").exists()

    members = _names(client.get(Routes.staged(slot)).content)
    assert set(members) == {"Dockerfile", "src", "src/a.py"}
    members = _names(client.get(Routes.staged(slot), params={"path": "src"}).content)
    assert set(members) == {"a.py"}

    assert client.get(Routes.staged("image-builds/none")).status_code == 404
    # A path within the slot is confined too: `..` cannot reach the campaigns beside it.
    assert client.get(Routes.staged(slot), params={"path": "../.."}).status_code == 400


def test_a_slot_name_cannot_leave_the_staging_tree(root):
    """The URL path is normalised before it reaches a route, so this is the plane's own
    check on the slot it is handed by any caller in this process."""
    from robovast.service.data_app import DataPlane
    with pytest.raises(ValueError):
        DataPlane(root).staged_dir("../" + _CAMPAIGN)


def test_a_scoped_token_reaches_its_campaign_and_nothing_else(root):
    token = auth.scoped_token(TEST_TOKEN, auth.scope_for_campaign(_CAMPAIGN))
    _campaign(root, _OTHER)
    headers = {"Authorization": f"Bearer {token}"}
    with TestClient(build_data_app(root, TEST_TOKEN), headers=headers) as client:
        assert client.get(Routes.campaign_inputs(_CAMPAIGN), params=_JOB).status_code == 200
        assert client.put(Routes.campaign_outputs(_CAMPAIGN),
                          content=_tar([("cell-a/1/x", b"y")])).status_code == 200
        assert client.get(Routes.campaign_archive(_CAMPAIGN)).status_code == 200
        # Another campaign, a slot, the health check's neighbours: outside the scope.
        assert client.get(Routes.campaign_inputs(_OTHER), params=_JOB).status_code == 403
        assert client.get(Routes.staged("image-builds/b")).status_code == 403
    # The control plane refuses it everywhere but the data routes it mounts.
    store = WorkspaceStore(registry=WorkspaceRegistry(root=root.parent / "ws"))
    lt = NullService(store=store)
    lt._campaigns_root = lambda: root
    with TestClient(build_app(lt, mount_mcp=False), headers=headers) as client:
        assert client.get(Routes.campaign_archive(_CAMPAIGN)).status_code == 200
        assert client.get(Routes.CAMPAIGNS).status_code == 403
        assert client.get(Routes.VERSION).status_code == 403


def test_a_forged_scope_is_not_authenticated(standalone):
    forged = f"{auth.scope_for_campaign(_CAMPAIGN)}.{'0' * 64}"
    resp = standalone.get(Routes.campaign_inputs(_CAMPAIGN), params=_JOB,
                          headers={"Authorization": f"Bearer {forged}"})
    assert resp.status_code == 401


def test_a_full_disk_is_a_507(client, monkeypatch):
    import errno

    from robovast.service import tar_io

    def _full(*_a, **_k):
        raise OSError(errno.ENOSPC, "No space left on device")
    monkeypatch.setattr(tar_io, "_write_atomic", _full)
    resp = client.put(Routes.campaign_outputs(_CAMPAIGN), content=_tar([("cell-a/1/x", b"y")]))
    assert resp.status_code == 507, resp.text


def test_a_write_the_service_could_not_make_is_a_500_not_a_bad_upload(client, monkeypatch):
    """A disk gone read-only or failing is the service's fault, and an uploader retries a 5xx.
    Answered as 400 -- "not a readable tar" -- it would give up on output that was sound."""
    import errno

    from robovast.service import tar_io

    def _read_only(*_a, **_k):
        raise OSError(errno.EROFS, "Read-only file system")
    monkeypatch.setattr(tar_io, "_write_atomic", _read_only)
    resp = client.put(Routes.campaign_outputs(_CAMPAIGN), content=_tar([("cell-a/1/x", b"y")]))
    assert resp.status_code == 500, resp.text
    assert "could not write the upload" in resp.json()["detail"]


def test_an_upload_that_is_not_a_tar_is_a_400(client):
    """Including gzip's own complaint, which Python raises as an ``OSError`` like any failed
    write -- the one case where an ``OSError`` is the sender's to fix."""
    for body in (b"certainly not a tar", b"\x1f\x8b\x08\x00" + b"\x00" * 40):
        resp = client.put(Routes.campaign_outputs(_CAMPAIGN), content=body)
        assert resp.status_code == 400, (body[:4], resp.text)
        assert "not a readable tar" in resp.json()["detail"]


def test_the_control_plane_mints_tokens_the_data_plane_honours(root):
    """The scope a pod carries is minted by the transport from the secret its gate enforces."""
    store = WorkspaceStore(registry=WorkspaceRegistry(root=root.parent / "ws"))
    lt = NullService(store=store)
    lt._campaigns_root = lambda: root
    build_app(lt, mount_mcp=False, auth_token="a-configured-secret")
    token = lt.scoped_token(auth.scope_for_campaign(_CAMPAIGN))
    with TestClient(build_data_app(root, "a-configured-secret"),
                    headers={"Authorization": f"Bearer {token}"}) as client:
        assert client.get(Routes.campaign_inputs(_CAMPAIGN), params=_JOB).status_code == 200


def test_only_an_archive_that_leaves_the_cluster_is_compressed(client, root):
    """gzip costs a core per stream and saves little on run output, so every stream a pod
    reads is a plain tar; a download keeps gzip, and names its file for what it is."""
    resp = client.get(Routes.campaign_archive(_CAMPAIGN))
    assert resp.headers["content-type"] == "application/gzip"
    assert resp.headers["content-disposition"].endswith('.tar.gz"')
    _names(resp.content, gz=True)

    resp = client.get(Routes.campaign_inputs(_CAMPAIGN), params=_JOB)
    assert resp.headers["content-type"] == "application/x-tar"
    _names(resp.content)


def test_an_upload_is_read_compressed_or_not(client, root):
    """The reader detects the format, so a pod's plain tar and a gzipped one both land."""
    for n, gz in ((1, False), (2, True)):
        resp = client.put(Routes.campaign_outputs(_CAMPAIGN),
                          content=_tar([(f"cell-a/{n}/test.xml", b"<testsuite/>")], gz=gz))
        assert resp.status_code == 200, resp.text
        assert (root / _CAMPAIGN / "cell-a" / str(n) / "test.xml").exists()
