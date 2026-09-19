# Copyright (C) 2026 Frederik Pasch
# SPDX-License-Identifier: Apache-2.0
"""What the host container sends back, and what it must not.

The pod's filesystem does not outlive it, so anything the Job derived exists nowhere until
it is delivered -- one tar, ``PUT`` to the campaign's outputs route on the service's data
plane -- and the campaign it derived it from was staged into the same tree. The delivery
therefore has to tell one from the other: new or changed since the snapshot is output,
untouched is the service's own copy of itself.

The stat-diff alone cannot see the CONVERSION's output, which is why the record it writes
is the second half of the rule; that ordering is pinned here because nothing in a
same-container test reproduces it. The "everything new" half is right for what the stages
produce and wrong for scratch and for what the driver owns, so those exceptions are pinned
here too.
"""

import io
import json
import tarfile

import pytest
import requests

from robovast.execution.cluster_execution import pod_access, postprocess_host

CACHE = ".robovast_rosbags_process_cache"
DATA_URL = "http://robovast.ns.svc:8080/data"
TOKEN = "campaign:camp.0123abcd"


class _DataPlane:
    """A fake of the outputs route: takes each PUT's body as a tar and records it.

    *answers* is what each successive request meets -- a status code, or an exception
    raised in place of a response -- so a test can say "the service was being rolled for
    the first attempt".
    """

    def __init__(self, answers=(200,)):
        self.answers = list(answers)
        self.requests = []
        self.members = []          # per PUT: {rel: bytes}

    def put(self, url, data=None, headers=None, timeout=None):
        body = b"".join(data)
        answer = self.answers.pop(0) if len(self.answers) > 1 else self.answers[0]
        self.requests.append({"url": url, "headers": headers, "body": body})
        if isinstance(answer, Exception):
            raise answer  # pylint: disable=raising-bad-type - an int never reaches here
        return self._respond(int(answer), body)

    def _respond(self, status: int, body: bytes):
        with tarfile.open(fileobj=io.BytesIO(body), mode="r:gz") as tar:
            self.members.append({m.name: tar.extractfile(m).read()
                                 for m in tar.getmembers() if m.isfile()})
        response = requests.Response()
        response.status_code = status
        response._content = (json.dumps({"files": len(self.members[-1]), "bytes": 0,
                                         "refused": []}).encode()
                             if status < 400 else b"nope")
        return response

    @property
    def sent(self):
        """Every campaign-relative path delivered, across all PUTs, sorted."""
        return sorted(rel for members in self.members for rel in members)


@pytest.fixture
def plane(monkeypatch):
    fake = _DataPlane()
    monkeypatch.setattr(requests, "put", fake.put)
    monkeypatch.setattr(postprocess_host, "_DELIVERY_RETRY_S", 0)
    return fake


@pytest.fixture
def campaign(tmp_path):
    """A staged campaign as the host container finds it: run data, a bag, the driver's
    records, and no outputs."""
    root = tmp_path / "camp"
    (root / "_execution").mkdir(parents=True)
    (root / "_execution" / "execution.yaml").write_text("image: img:1\n")
    (root / "_execution" / "controller.log").write_text("the driver's log\n")
    (root / "campaign.db").write_bytes(b"sqlite")
    bag = root / "cfg" / "0" / "rosbag2"
    bag.mkdir(parents=True)
    (bag / "rosbag2_0.mcap").write_bytes(b"bag bytes")
    return root


def _upload(campaign, before):
    return postprocess_host._upload_derived(str(campaign), before, DATA_URL, TOKEN, "camp")


def _convert(campaign, *outputs):
    """Write *outputs* and the record naming them, as the conversion initContainer does.

    Before the snapshot, because that is the ordering: the conversion is an initContainer
    and the host container starts only once it has exited.
    """
    from robovast.results_processing.postprocessing import STAGED_PROVENANCE

    for rel in outputs:
        path = campaign / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("x,y\n")
    record = campaign / STAGED_PROVENANCE
    record.parent.mkdir(parents=True, exist_ok=True)
    record.write_text(json.dumps({"entries": [
        {"output": rel, "sources": ["cfg/0/rosbag2"], "plugin": "rosbags_process/to_csv"}
        for rel in outputs
    ]}))


def test_the_provenance_marker_is_delivered(plane, campaign):
    """``_transient/postprocessing.yaml`` is the proof that the ingest ran.

    Every reader asks the campaign whether it has been postprocessed by looking for that
    file, so a campaign whose marker stayed in the pod reads back as un-postprocessed --
    and gets postprocessed again, forever, while its tables are already complete.
    """
    before = postprocess_host._snapshot(str(campaign))
    (campaign / "_transient").mkdir()
    (campaign / "_transient" / "postprocessing.yaml").write_text("entries: []\n")

    assert _upload(campaign, before) == 1
    assert plane.sent == ["_transient/postprocessing.yaml"]
    assert plane.members[0]["_transient/postprocessing.yaml"] == b"entries: []\n"


def test_the_delivery_is_one_put_of_a_tar_to_the_campaigns_outputs_route(plane, campaign):
    """The address from the env, the route the data plane serves for exactly this, the
    campaign's scoped token as the bearer, and a gzip tar as the body: what every pod
    delivering outputs sends, so the service has one writing half."""
    before = postprocess_host._snapshot(str(campaign))
    (campaign / "cfg" / "0" / "poses.csv").write_text("x,y\n")

    _upload(campaign, before)

    (request,) = plane.requests
    assert request["url"] == f"{DATA_URL}/campaigns/camp/outputs"
    assert request["headers"]["Authorization"] == f"Bearer {TOKEN}"
    assert request["headers"]["Content-Type"] == "application/gzip"
    assert request["body"][:2] == b"\x1f\x8b"


def test_the_staged_rosbags_are_not_written_back_over_themselves(plane, campaign):
    """They came FROM the service, unchanged, and they are the bulk of a campaign.

    Sending them back would make every postprocess re-upload the campaign it was
    postprocessing -- the transfer paid twice, in the direction where it also risks
    replacing a good file with a copy of itself.
    """
    before = postprocess_host._snapshot(str(campaign))
    (campaign / "cfg" / "0" / "poses.csv").write_text("x,y\n")

    _upload(campaign, before)

    assert plane.sent == ["cfg/0/poses.csv"]


def test_the_conversions_own_output_is_delivered(plane, campaign):
    """It predates the snapshot, and the service has never held it.

    The conversion is an initContainer, so every CSV it derives is on disk before this
    container starts and the stat-diff reads all of it as staged data. Diffed alone, the
    tables an analysis reads -- poses, the per-action feedback and status, the costmaps --
    would be ingested into the index and then deleted with the pod, so a campaign's
    download carried none of them while its index carried all of them.
    """
    _convert(campaign, "cfg/0/poses.csv", "cfg/0/action_navigate_to_pose_status.csv")
    before = postprocess_host._snapshot(str(campaign))

    _upload(campaign, before)

    assert plane.sent == ["cfg/0/action_navigate_to_pose_status.csv", "cfg/0/poses.csv"]


def test_the_conversions_output_is_delivered_with_the_host_stages(plane, campaign):
    """One delivery carries both halves of what the Job derived.

    The host stage's own steps read the conversion's files -- ``nav2_bt_tree`` reconstructs
    ``nav2_behaviors.csv`` from the raw transitions -- so a campaign that keeps only the
    second is one whose derived data cannot be recomputed or checked against its source.
    """
    _convert(campaign, "cfg/0/nav2_behavior_tree.csv")
    before = postprocess_host._snapshot(str(campaign))
    (campaign / "cfg" / "0" / "nav2_behaviors.csv").write_text("b\n")

    _upload(campaign, before)

    assert len(plane.requests) == 1
    assert plane.sent == ["cfg/0/nav2_behavior_tree.csv", "cfg/0/nav2_behaviors.csv"]


def test_a_campaign_with_no_conversion_still_delivers_by_diff(plane, campaign):
    """No record is the normal case for a batch Job and for a campaign with no bags.

    The diff is then the whole answer, so a missing record must not cost the host stage's
    own outputs their delivery.
    """
    before = postprocess_host._snapshot(str(campaign))
    (campaign / "cfg" / "0" / "run_log.csv").write_text("l\n")

    _upload(campaign, before)

    assert plane.sent == ["cfg/0/run_log.csv"]


def test_the_per_bag_hash_cache_is_not_campaign_data(plane, campaign):
    """It is new, so the diff calls it output -- and it is scratch, so it is not.

    ``rosbags_process`` keeps this cache beside the bag it describes, at a path fixed in
    the script. It is rebuildable by definition and describes a pod that no longer exists,
    so delivering it would add a file per bag to every campaign for a cache no later reader
    can use.
    """
    before = postprocess_host._snapshot(str(campaign))
    (campaign / "cfg" / "0" / "rosbag2" / CACHE).write_text("{}")
    (campaign / "cfg" / "0" / "poses.csv").write_text("x,y\n")

    _upload(campaign, before)

    assert plane.sent == ["cfg/0/poses.csv"]
    assert CACHE in postprocess_host.NOT_CAMPAIGN_DATA


def test_execution_is_not_sent_wholesale_only_what_this_job_wrote(plane, campaign):
    """``_execution/`` is under the same rule as everything else, and the reason is the
    driver's log: it is appended to for the campaign's whole life, and the pod holds a
    snapshot of it from the moment it was staged. Sent wholesale, that snapshot would land
    on the service and truncate the record to that moment. What the Job itself wrote there
    -- its log, the conversion's provenance, the usage record -- differs from the snapshot
    and goes; what it was handed stays where it is.
    """
    before = postprocess_host._snapshot(str(campaign))
    (campaign / "_execution" / "postprocessing.log").write_text("converted 3 bags\n")
    (campaign / "_execution" / "rosbags_provenance.json").write_text('{"entries": []}')
    (campaign / "_execution" / "postprocess_system_usage.csv").write_text("step,x\n")

    _upload(campaign, before)

    assert plane.sent == ["_execution/postprocess_system_usage.csv",
                          "_execution/postprocessing.log",
                          "_execution/rosbags_provenance.json"]
    assert "_execution/execution.yaml" not in plane.sent
    assert "_execution/controller.log" not in plane.sent


def test_the_drivers_own_files_are_never_sent_even_when_they_changed(plane, campaign):
    """The service refuses these per member; the pod does not offer them. A step that
    touched the driver's log in the pod -- an appended line, a rewritten copy -- has
    changed a file that has one writer, and that writer is not this pod."""
    before = postprocess_host._snapshot(str(campaign))
    (campaign / "_execution" / "controller.log").write_text("the driver's log\nplus a line\n")
    (campaign / "_execution" / "variation.log").write_text("new\n")
    (campaign / "_execution" / "build.log").write_text("new\n")
    (campaign / "cfg" / "0" / "poses.csv").write_text("x,y\n")

    _upload(campaign, before)

    assert plane.sent == ["cfg/0/poses.csv"]


def test_the_campaigns_store_is_never_sent(plane, campaign):
    """``campaign.db`` is the driver's, held open for the campaign's whole life, and the
    index ingest reads it rather than writing it. A copy from the pod -- changed by a
    step, or merely stat-different -- would land on a database another process has open.
    """
    before = postprocess_host._snapshot(str(campaign))
    (campaign / "campaign.db").write_bytes(b"sqlite, rewritten")
    (campaign / "campaign.db-journal").write_bytes(b"journal")
    (campaign / "campaign.db-wal").write_bytes(b"wal")
    (campaign / "cfg" / "0" / "poses.csv").write_text("x,y\n")

    _upload(campaign, before)

    assert plane.sent == ["cfg/0/poses.csv"]
    assert not [rel for rel in plane.sent if "campaign.db" in rel]


def test_a_run_that_derived_nothing_sends_nothing(plane, campaign):
    """A batch that derived no rows, or a host pass whose every step was cached, has left
    the campaign as it was: no PUT, and no failure."""
    before = postprocess_host._snapshot(str(campaign))

    assert _upload(campaign, before) == 0
    assert plane.requests == []


# -- a service being rolled while the postprocess runs -------------------------


def test_a_delivery_the_service_could_not_take_is_retried_whole(monkeypatch, campaign):
    """A streamed body cannot be replayed, so a retry is the whole pipeline again -- and
    each attempt carries the same tar, because what it describes did not change."""
    plane = _DataPlane(answers=[requests.ConnectionError("refused"), 200])
    monkeypatch.setattr(requests, "put", plane.put)
    monkeypatch.setattr(postprocess_host, "_DELIVERY_RETRY_S", 0)
    before = postprocess_host._snapshot(str(campaign))
    (campaign / "cfg" / "0" / "poses.csv").write_text("x,y\n")

    assert _upload(campaign, before) == 1

    assert len(plane.requests) == 2
    assert plane.sent == ["cfg/0/poses.csv"]


def test_a_service_side_failure_is_retried_and_then_reported(monkeypatch, campaign):
    plane = _DataPlane(answers=[503])
    monkeypatch.setattr(requests, "put", plane.put)
    monkeypatch.setattr(postprocess_host, "_DELIVERY_RETRY_S", 0)
    before = postprocess_host._snapshot(str(campaign))
    (campaign / "cfg" / "0" / "poses.csv").write_text("x,y\n")

    with pytest.raises(RuntimeError, match="could not deliver"):
        _upload(campaign, before)

    assert len(plane.requests) == postprocess_host._DELIVERY_ATTEMPTS


def test_a_refusal_is_not_retried(monkeypatch, campaign):
    """A 4xx says the route refused what was sent -- the wrong campaign for this token, a
    campaign that is not there -- and sending it again changes nothing."""
    plane = _DataPlane(answers=[404])
    monkeypatch.setattr(requests, "put", plane.put)
    monkeypatch.setattr(postprocess_host, "_DELIVERY_RETRY_S", 0)
    before = postprocess_host._snapshot(str(campaign))
    (campaign / "cfg" / "0" / "poses.csv").write_text("x,y\n")

    with pytest.raises(RuntimeError, match="refused"):
        _upload(campaign, before)

    assert len(plane.requests) == 1


# -- the entry point -----------------------------------------------------------


def test_the_host_refuses_to_start_without_its_data_plane_access(monkeypatch, capsys):
    """Fail loud on a missing input: a host that ran without a token would derive the
    tables and then have nowhere to send them, reporting a postprocess that looks done."""
    for name in (pod_access.CAMPAIGN_ID_ENV, postprocess_host.ENV_STAGE_DEST,
                 pod_access.DATA_URL_ENV, pod_access.TOKEN_ENV):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv(pod_access.CAMPAIGN_ID_ENV, "camp")
    monkeypatch.setenv(postprocess_host.ENV_STAGE_DEST, "/campaign")
    monkeypatch.setenv(pod_access.DATA_URL_ENV, DATA_URL)

    assert postprocess_host.main() == 2
    assert pod_access.TOKEN_ENV in capsys.readouterr().err


def test_a_host_pass_whose_outputs_could_not_be_delivered_is_a_failed_postprocess(
        monkeypatch, tmp_path, capsys):
    """The work succeeded and its results are in a pod about to be deleted. That is not a
    success with a warning: the campaign carries none of what was derived."""
    campaign = tmp_path / "camp"
    (campaign / "_execution").mkdir(parents=True)
    monkeypatch.setenv(pod_access.CAMPAIGN_ID_ENV, "camp")
    monkeypatch.setenv(postprocess_host.ENV_STAGE_DEST, str(tmp_path))
    monkeypatch.setenv(pod_access.DATA_URL_ENV, DATA_URL)
    monkeypatch.setenv(pod_access.TOKEN_ENV, TOKEN)
    monkeypatch.delenv(postprocess_host.ENV_COMMANDS, raising=False)

    def _derive(dest, campaign_id, force=False, skip=None):
        (campaign / "cfg").mkdir()
        (campaign / "cfg" / "poses.csv").write_text("x\n")
        return True, "postprocessing complete"

    def _refuse(*_a, **_k):
        raise RuntimeError("the data plane was not there")

    monkeypatch.setattr("robovast.execution.cluster_execution.postprocess_job."
                        "run_host_postprocessing", _derive)
    monkeypatch.setattr(postprocess_host, "_deliver", _refuse)

    assert postprocess_host.main() == 1
    assert "could not be delivered" in capsys.readouterr().err
