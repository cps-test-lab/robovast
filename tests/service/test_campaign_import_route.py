# Copyright (C) 2026 Frederik Pasch
# SPDX-License-Identifier: Apache-2.0
"""Taking a campaign in over HTTP: upload the archive, import it, and it is a campaign here.

``tests/service/test_ingest.py`` covers the staging verdicts against an already-extracted
directory. What this file defends is the part only the wire and the worker can show:

* **An older archive imports.** The migration ladders are exercised elsewhere against
  directories, which is not the same claim: what a user does is upload a ``.tar.gz`` somebody
  produced two robovast versions ago, and every layer between the PUT and ``campaign.db`` has
  to carry the old version through. So this runs the genuinely-old committed fixtures through
  the real routes.
* **Import registers, it does not merely extract.** Listings answer from the store, so a
  campaign that extracted but did not register lists blank -- which looks like success to
  everything except the person reading it.
* **A refusal happens before any bytes move.** A bad archive, a name that is not a campaign
  id, and a collision with a campaign already here are all synchronous errors on the POST. If
  any of them slipped into the worker instead, the caller would get a ref for an import that
  was never going to happen, and would learn about it only from a failed campaign row.
* **The service deletes only what it staged.** An uploaded copy is the service's to clean up;
  a path the caller named is not, and deleting it would be unrecoverable.

The import itself is a *tracked background operation*: the POST answers with a ``CampaignRef``
as soon as the work is dispatched, so every assertion about the outcome has to wait for the
campaign to leave its live phases (:func:`_settle`) rather than read the response.

Not to be merged with ``test_import_campaign.py``, which looks similar and is not: that file
drives the transport and asserts the *behaviour* of a tracked import (named before extraction,
raw archives postprocessed after, a failure leaving no half-campaign). This one goes over the
wire and asserts what only the wire can carry -- the status code a refusal maps to, and the
upload side channel, which has no transport-level existence at all. The two cases that appear
in both are deliberate: there, that a clash is refused before anything transfers; here, that it
reaches the caller as a 409 rather than a 500 or a success.
"""

import json
import shutil
import tarfile
import time
from pathlib import Path

import pytest
from starlette.testclient import TestClient

from robovast.client.status import TERMINAL_PHASES
from robovast.service.app import build_app
from robovast.service.client import LocalTransport
from robovast.service.interface import Routes
from robovast.service.workspaces import WorkspaceRegistry, WorkspaceStore

_FIXTURES = Path(__file__).resolve().parents[1] / "fixtures" / "historic_campaigns"

#: How long an import of a committed fixture may take. Generous, because it is a timeout for a
#: hang and not a performance assertion: these archives are a few kilobytes.
_SETTLE_TIMEOUT_S = 30


def _transport(tmp_path):
    """A fully constructed LocalTransport with its results root pinned under *tmp_path*.

    Really constructed, not ``__new__``-ed past its ``__init__``: these tests drive the listing
    and the background dispatcher, both of which read state the constructor sets up, so a
    half-built transport fails on bookkeeping that has nothing to do with importing.
    """
    store = WorkspaceStore(registry=WorkspaceRegistry(root=tmp_path / "workspaces"))
    lt = LocalTransport(store=store)
    lt._campaigns_root = lambda: tmp_path / "results"
    return lt


@pytest.fixture(name="env")
def _env(monkeypatch, tmp_path):
    # No CWD project, so nothing outside tmp_path can be discovered as the results root.
    # Postprocessing is a different subject with its own tests, and a raw fixture would drag a
    # whole pipeline into every case here. Stubbed to a no-op so what is under test is the
    # import: the chain itself is asserted once, in its own test below.
    monkeypatch.setattr(LocalTransport, "_postprocess_after_import",
                        lambda self, state, campaign_id, target: state.set_phase("finished"))
    transport = _transport(tmp_path)
    with TestClient(build_app(transport)) as client:
        yield client, transport, tmp_path


def _archive(campaign_dir: Path, out: Path) -> Path:
    """Tar *campaign_dir* the way the exporter does: one top-level entry, its own name."""
    out.parent.mkdir(parents=True, exist_ok=True)
    with tarfile.open(out, "w:gz") as tar:
        tar.add(campaign_dir, arcname=campaign_dir.name)
    return out


def _fixtures():
    return sorted(d for d in _FIXTURES.iterdir() if d.is_dir())


def _upload(client, archive: Path) -> str:
    """Stage *archive* through the grant + PUT, returning where it landed."""
    grant = client.post(Routes.CAMPAIGN_ARCHIVES)
    assert grant.status_code == 200, grant.text
    put = client.put(grant.json()["url"], content=archive.read_bytes())
    assert put.status_code == 200, put.text
    staged = put.json()
    assert staged["size"] == archive.stat().st_size
    return staged["path"]


def _import(client, path: str, **kwargs):
    return client.post(Routes.CAMPAIGN_IMPORT, json={"archive_path": path, **kwargs})


def _phase(client, campaign_id: str):
    listing = client.get(Routes.CAMPAIGNS)
    assert listing.status_code == 200, listing.text
    for row in listing.json()["campaigns"]:
        if row["campaign_id"] == campaign_id:
            return row.get("phase")
    return None


def _settle(client, campaign_id: str) -> str:
    """Wait until *campaign_id* stops working, and return the phase it stopped in.

    Polled through the listing rather than by joining the worker thread, because the listing is
    what the web UI and the CLI watch -- so a campaign that finished internally but never
    reported it would still fail here, which is the useful failure.
    """
    deadline = time.monotonic() + _SETTLE_TIMEOUT_S
    while time.monotonic() < deadline:
        phase = _phase(client, campaign_id)
        if phase in TERMINAL_PHASES:
            return phase
        time.sleep(0.05)
    raise AssertionError(
        f"{campaign_id} was still at phase {_phase(client, campaign_id)!r} after "
        f"{_SETTLE_TIMEOUT_S}s")


def _report(transport, campaign_id: str) -> dict:
    """The stage report the worker wrote for *campaign_id*."""
    path = transport._campaigns_root() / campaign_id / "_execution" / "import.json"
    assert path.is_file(), f"no import.json for {campaign_id}"
    return json.loads(path.read_text(encoding="utf-8"))


# -- the whole way through ---------------------------------------------------

@pytest.mark.parametrize("fixture", _fixtures(), ids=lambda d: d.name)
def test_a_historic_archive_uploads_imports_and_then_lists(env, fixture, tmp_path):
    """Every shipped config version survives upload -> import -> listing.

    Parameterized over the fixture directory rather than pinned to one version, so a new
    ladder step's fixture is covered here the moment it is committed -- the same contract
    ``test_historic_campaigns.py`` states for the versions themselves.
    """
    client, transport, _ = env
    archive = _archive(fixture, tmp_path / "out" / f"{fixture.name}.tar.gz")

    started = _import(client, _upload(client, archive))
    assert started.status_code == 200, started.text
    # The ref names the campaign before a byte is unpacked -- that is what puts the row in the
    # view while the import is still running.
    assert started.json()["campaign_id"] == fixture.name
    assert _phase(client, fixture.name) is not None, \
        "the campaign must be listed while it is importing, not only once it is done"

    assert _settle(client, fixture.name) == "finished"

    report = _report(transport, fixture.name)
    assert report["ok"], report["stages"]
    # The config stage says what happened to the old version: either it is current, or it
    # migrated on read. What it must never be for a committed fixture is `failed`.
    assert report["stages"]["config"]["verdict"] in ("ok", "migrated")

    # Registered, not merely extracted: the listing answers from campaign.db.
    assert fixture.name in {c["campaign_id"]
                            for c in client.get(Routes.CAMPAIGNS).json()["campaigns"]}
    assert (transport._campaigns_root() / fixture.name / "campaign.db").is_file()


def test_an_older_archive_reports_the_migration_it_needed(env, tmp_path):
    """The v1 fixture migrates, and the report says so with the steps it applied.

    The verdict is the user-visible half of the migration: an import that quietly succeeded
    would leave somebody unable to tell a current campaign from one brought forward.
    """
    client, transport, _ = env
    v1 = next(d for d in _fixtures() if d.name.startswith("v1-"))
    archive = _archive(v1, tmp_path / "out" / "v1.tar.gz")

    _import(client, _upload(client, archive))
    _settle(client, v1.name)

    stage = _report(transport, v1.name)["stages"]["config"]
    assert stage["verdict"] == "migrated"
    assert stage["version"] == 1
    assert stage["steps"], "a migration that names no step cannot be reviewed"


def test_the_stage_report_is_written_where_a_client_can_read_it(env, tmp_path):
    """``_execution/import.json`` is the only route to the stage detail.

    The import answers with a ref, not a report, so if this file were missing the migration and
    degradation verdicts would exist nowhere a user could see them.
    """
    client, transport, _ = env
    fixture = _fixtures()[0]
    _import(client, _upload(client, _archive(fixture, tmp_path / "out" / "c.tar.gz")))
    _settle(client, fixture.name)

    report = _report(transport, fixture.name)
    assert set(report["stages"]) == {"layout", "config", "campaign_store", "analysis_db"}
    assert report["campaign_id"] == fixture.name
    # Served over the file route too, which is how the web UI reads it.
    served = client.get(f"/results/{fixture.name}/_execution/import.json?as=text&lines=0")
    assert served.status_code == 200, served.text


# -- who owns the archive ----------------------------------------------------

def test_an_uploaded_archive_is_removed_once_imported(env, tmp_path):
    """The service staged it, so the service cleans it up."""
    client, _, _ = env
    fixture = _fixtures()[0]
    staged = _upload(client, _archive(fixture, tmp_path / "out" / "c.tar.gz"))

    _import(client, staged)
    _settle(client, fixture.name)
    assert not Path(staged).exists()


def test_a_caller_named_archive_is_left_alone(env, tmp_path):
    """Importing somebody's own file must not delete it.

    This is the path the MCP tool and a host-side caller take. Deleting here would be
    unrecoverable, and would make importing a shared dataset destroy it for everyone else.
    """
    client, _, _ = env
    fixture = _fixtures()[0]
    archive = _archive(fixture, tmp_path / "mine" / "c.tar.gz")

    _import(client, str(archive))
    _settle(client, fixture.name)
    assert archive.is_file()


# -- refused before anything moves -------------------------------------------

def test_a_second_import_is_refused_until_force(env, tmp_path):
    """A campaign already here is evidence; replacing it is asked for, not assumed."""
    client, transport, _ = env
    fixture = _fixtures()[0]
    archive = _archive(fixture, tmp_path / "out" / "c.tar.gz")

    _import(client, str(archive))
    _settle(client, fixture.name)

    marker = transport._campaigns_root() / fixture.name / "_imported-once"
    marker.write_text("x", encoding="utf-8")

    clash = _import(client, str(archive))
    assert clash.status_code == 409
    assert "already here" in clash.json()["detail"]
    assert marker.exists(), "a refused import must not have touched what is here"

    assert _import(client, str(archive), force=True).status_code == 200
    _settle(client, fixture.name)
    assert not marker.exists(), "force replaces the campaign rather than merging into it"


def test_a_file_that_is_not_an_archive_is_a_400(env, tmp_path):
    client, _, _ = env
    junk = tmp_path / "not-a-tarball.tar.gz"
    junk.write_bytes(b"this is not a tarball")
    assert _import(client, str(junk)).status_code == 400


def test_an_archive_of_two_campaigns_is_refused(env, tmp_path):
    """One archive is one campaign: two top-level entries name no campaign id."""
    client, _, _ = env
    both = tmp_path / "two.tar.gz"
    with tarfile.open(both, "w:gz") as tar:
        for fixture in _fixtures()[:2]:
            tar.add(fixture, arcname=fixture.name)

    refused = _import(client, str(both))
    assert refused.status_code == 400
    assert "top-level" in refused.json()["detail"]


def test_a_dot_rooted_archive_of_one_campaign_still_imports(env, tmp_path):
    """``tar -C <dir> .`` names every member under ``./`` — that is still one campaign."""
    client, _, _ = env
    fixture = _fixtures()[0]
    staging = tmp_path / "staging"
    staging.mkdir()
    shutil.copytree(fixture, staging / fixture.name)
    dotted = tmp_path / "dot.tar.gz"
    with tarfile.open(dotted, "w:gz") as tar:
        tar.add(staging, arcname=".")

    started = _import(client, str(dotted))
    assert started.status_code == 200, started.text
    assert started.json()["campaign_id"] == fixture.name


def test_a_dot_rooted_archive_of_a_whole_results_tree_is_refused(env, tmp_path):
    """The regression that mattered: ``.`` must never be read as the campaign's name.

    ``tar czf x.tar.gz -C <results> .`` puts everything under ``./``, so taking the first path
    segment literally yields ONE top-level entry called ``.`` -- which resolves to the results
    root. It passed the one-entry check, reported the root as "already here", and under force
    would have deleted every campaign on the service to make room for itself.
    """
    client, transport, _ = env
    staging = tmp_path / "many"
    staging.mkdir()
    for fixture in _fixtures():
        shutil.copytree(fixture, staging / fixture.name)
    whole = tmp_path / "whole.tar.gz"
    with tarfile.open(whole, "w:gz") as tar:
        tar.add(staging, arcname=".")

    # A campaign already here, to prove force did not take the results root with it.
    kept = transport._campaigns_root() / "kept-2026-01-01-000000"
    kept.mkdir(parents=True)
    (kept / "marker").write_text("x", encoding="utf-8")

    refused = _import(client, str(whole), force=True)
    assert refused.status_code == 400
    assert "top-level" in refused.json()["detail"]
    assert (kept / "marker").exists(), \
        "force on a misread archive must not have deleted the results root"


def test_a_name_that_is_not_campaign_shaped_is_refused(env, tmp_path):
    """An unlisted campaign is worse than a refusal.

    The local listing keeps only directories matching ``is_campaign_dir``, and deletion checks
    the same shape. So a differently-named directory would import, report every stage ok, and
    then be invisible and impossible to delete -- a success message over nothing.
    """
    client, _, _ = env
    staging = tmp_path / "odd"
    staging.mkdir()
    shutil.copytree(_fixtures()[0], staging / "not-a-campaign-name")
    odd = tmp_path / "odd.tar.gz"
    with tarfile.open(odd, "w:gz") as tar:
        tar.add(staging / "not-a-campaign-name", arcname="not-a-campaign-name")

    refused = _import(client, str(odd))
    assert refused.status_code == 400
    detail = refused.json()["detail"]
    assert "not a campaign directory name" in detail
    assert "listed and deleted by that shape" in detail, \
        "the refusal has to say why, or it reads as an arbitrary rule"


def test_a_missing_path_is_a_404(env, tmp_path):
    client, _, _ = env
    assert _import(client, str(tmp_path / "nope.tar.gz")).status_code == 404


def test_naming_no_source_or_both_is_a_400(env, tmp_path):
    """An import names exactly one source. Neither, and both, are the caller's mistake."""
    client, _, _ = env
    assert client.post(Routes.CAMPAIGN_IMPORT, json={}).status_code == 400
    both = client.post(Routes.CAMPAIGN_IMPORT,
                       json={"archive_path": str(tmp_path / "a.tar.gz"),
                             "share_archive": "b"})
    assert both.status_code == 400


def test_an_abandoned_upload_is_swept_but_a_refused_one_is_kept(env, tmp_path):
    """Uploads nobody imported are cleaned up; the one a retry needs is not.

    Both halves matter. An upload that was never imported -- a refused pre-flight, or a browser
    that went away between the PUT and the POST -- is a campaign archive sitting on the results
    volume, so gigabytes accumulate per attempt if nothing sweeps it. But the answer to the
    commonest refusal is to import that *same* staged archive again with force, which is what
    the web UI's "Replace existing" does, so cleaning up on refusal would silently turn that
    retry into a full re-upload.
    """
    import os

    from robovast.service.local_transport import STAGED_ARCHIVE_MAX_AGE_S

    client, _transport, _ = env
    fixture = _fixtures()[0]
    archive = _archive(fixture, tmp_path / "out" / "c.tar.gz")

    # An upload refused because the campaign is already here: still there for the retry.
    _import(client, str(archive))
    _settle(client, fixture.name)
    kept = Path(_upload(client, archive))
    assert _import(client, str(kept)).status_code == 409
    assert kept.is_file(), "a refused upload must survive for the force retry"

    # Age it past the window, then let the next grant sweep it.
    old = time.time() - STAGED_ARCHIVE_MAX_AGE_S - 60
    os.utime(kept, (old, old))
    fresh = Path(_upload(client, archive))

    assert not kept.exists(), "an upload nobody imported must not be kept forever"
    assert fresh.is_file(), "the sweep must not touch the upload that just arrived"


def test_an_upload_grant_is_one_time(env, tmp_path):
    """A replayed PUT is a 404, not a second write to a path already being imported."""
    client, _, _ = env
    archive = _archive(_fixtures()[0], tmp_path / "out" / "c.tar.gz")
    grant = client.post(Routes.CAMPAIGN_ARCHIVES).json()

    assert client.put(grant["url"], content=archive.read_bytes()).status_code == 200
    assert client.put(grant["url"], content=archive.read_bytes()).status_code == 404
