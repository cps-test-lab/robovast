# Copyright (C) 2026 Frederik Pasch
# SPDX-License-Identifier: Apache-2.0
"""The service's durable record of what it did.

The gap it exists for: a refused action is composed in the request that refused it, shown once,
and then gone. A campaign's failure is on its card and in its ``outcome.json``; "why wouldn't it
start yesterday?" had no answer but memory.

Durability is the whole point, so it is a database and not a third in-memory ring beside the
service log and the usage samples. Those two say in their own docstrings that they are volatile
and answer "what is it doing now". The events worth keeping are the ones a restart destroys --
and a restart is exactly when they are most worth having.
"""

import time

from robovast.service.event_log import EventLog


def _log(tmp_path):
    return EventLog(tmp_path / "events.db")


def test_an_event_survives_the_process_that_recorded_it(tmp_path):
    """The one property a ring cannot have, and the reason this is on disk."""
    _log(tmp_path).append("campaign.refused", message="cannot retrigger x", severity="error",
                          actor="fred", subject_type="campaign", subject_id="camp-1")
    # A different instance entirely -- as a restarted service would be.
    events = _log(tmp_path).read()
    assert [e.kind for e in events] == ["campaign.refused"]
    assert events[0].message == "cannot retrigger x"
    assert events[0].actor == "fred"
    assert events[0].subject_id == "camp-1"


def test_reading_resumes_from_a_cursor_rather_than_re_reading(tmp_path):
    log = _log(tmp_path)
    for i in range(5):
        log.append("x", message=str(i))
    first = log.read(limit=2)
    assert [e.message for e in first] == ["0", "1"]
    assert [e.message for e in log.read(since=first[-1].seq)] == ["2", "3", "4"]


def test_events_come_back_oldest_first_because_a_reader_is_resuming(tmp_path):
    log = _log(tmp_path)
    for i in range(3):
        log.append("x", message=str(i))
    seqs = [e.seq for e in log.read()]
    assert seqs == sorted(seqs)


def test_a_payload_round_trips_so_a_kind_can_carry_its_own_fields(tmp_path):
    """`kind` is open and `payload` is free-form: adding an event type must not be a migration."""
    log = _log(tmp_path)
    log.append("image.build", payload={"role": "sut", "tries": 2})
    assert log.read()[0].payload == {"role": "sut", "tries": 2}


def test_an_unwritable_location_does_not_take_the_caller_with_it(tmp_path):
    """Everything here describes work; none of it IS the work.

    A log that cannot open must not turn a campaign into a failed one, which is the same rule
    the campaign notifier and the build-manifest capture already follow.
    """
    blocked = tmp_path / "afile"
    blocked.write_text("not a directory")
    log = EventLog(blocked / "events.db")
    log.append("x", message="dropped")          # must not raise
    assert log.read() == []


def test_the_table_is_bounded_so_a_mounted_volume_cannot_fill(tmp_path, monkeypatch):
    """Operational record, not an archive. Pruning is by count here; age is the other bound.

    The real bound is twenty thousand rows; writing that many to assert a DELETE fires makes a
    slow test out of a fast fact, so the bound is lowered rather than the assertion weakened.
    """
    from robovast.service import event_log as module

    monkeypatch.setattr(module, "MAX_ROWS", 50)
    monkeypatch.setattr(module, "_PRUNE_EVERY", 10)
    log = _log(tmp_path)
    for i in range(120):
        log.append("x", message=str(i))
    kept = log.read(since=0, limit=1000)
    assert kept, "pruning must not empty the table"
    assert len(kept) <= 60, f"kept {len(kept)} rows against a bound of 50"
    # The oldest are gone and the cursor has moved past the pruned range.
    assert kept[0].seq > 1


def test_an_old_event_is_pruned_by_age(tmp_path, monkeypatch):
    from robovast.service import event_log as module

    log = _log(tmp_path)
    # The real clock, captured before it is replaced: `module.time` IS the time module, so a
    # lambda calling time.time() here calls itself.
    real = time.time
    monkeypatch.setattr(module.time, "time", lambda: real() - module.MAX_AGE_S - 60)
    log.append("stale", message="long ago")
    monkeypatch.undo()
    monkeypatch.setattr(module, "_PRUNE_EVERY", 5)
    for _ in range(5):
        log.append("fresh")
    assert "stale" not in [e.kind for e in log.read(limit=1000)]


# -- the service records its refusals, and serves them back -------------------------

def test_a_refused_request_is_recorded_and_readable(tmp_path, monkeypatch):
    """End to end, and the whole reason this exists.

    A refusal used to be composed in the request that refused it, rendered once, and gone.
    """
    from fastapi.testclient import TestClient

    from robovast.service.app import build_app
    from robovast.service.local_transport import LocalTransport
    from robovast.service.workspaces import WorkspaceRegistry, WorkspaceStore

    store = WorkspaceStore(registry=WorkspaceRegistry(root=str(tmp_path / "ws")))
    app = build_app(LocalTransport(store=store), mount_mcp=False, auth_token="")
    client = TestClient(app)

    # The refusal this whole record exists for: retriggering something that cannot be.
    refused = client.post("/campaigns/does-not-exist/retrigger")
    assert refused.status_code >= 400, refused.text

    body = client.get("/admin/events").json()
    refusals = [e for e in body["events"] if e["kind"] == "request.refused"]
    assert refusals, f"nothing recorded: {body}"
    assert refusals[-1]["severity"] == "error"
    # The status the caller actually got, not a guessed one: the point is that the record and
    # the response agree about what happened.
    assert refusals[-1]["payload"]["status"] == refused.status_code
    assert body["next_seq"] >= refusals[-1]["seq"]


def test_the_record_outlives_the_process_that_served_it(tmp_path):
    """A second app over the same workspaces root is what a restarted service is."""
    from fastapi.testclient import TestClient

    from robovast.service.app import build_app
    from robovast.service.local_transport import LocalTransport
    from robovast.service.workspaces import WorkspaceRegistry, WorkspaceStore

    def _client():
        store = WorkspaceStore(registry=WorkspaceRegistry(root=str(tmp_path / "ws")))
        return TestClient(build_app(LocalTransport(store=store), mount_mcp=False,
                                    auth_token=""))

    first = _client()
    first.post("/campaigns/gone/retrigger")
    before = len([e for e in first.get("/admin/events").json()["events"]
                  if e["kind"] == "request.refused"])
    assert before

    after = len([e for e in _client().get("/admin/events").json()["events"]
                 if e["kind"] == "request.refused"])
    assert after >= before, "the restart lost the record it exists to keep"


def test_a_successful_read_records_nothing(tmp_path):
    """A record of what did not happen, not a request trace."""
    from fastapi.testclient import TestClient

    from robovast.service.app import build_app
    from robovast.service.local_transport import LocalTransport
    from robovast.service.workspaces import WorkspaceRegistry, WorkspaceStore

    store = WorkspaceStore(registry=WorkspaceRegistry(root=str(tmp_path / "ws")))
    client = TestClient(build_app(LocalTransport(store=store), mount_mcp=False, auth_token=""))
    client.get("/campaigns")
    assert client.get("/admin/events").json()["events"] == []
