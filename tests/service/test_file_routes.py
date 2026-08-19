# Copyright (C) 2026 Frederik Pasch
# SPDX-License-Identifier: Apache-2.0
"""The one file address space: ``/results/<campaign>/…`` and ``/sources/<workspace>/…``.

Replaces the old ``/campaigns/{id}/run-files/{config}/{run}/{path}`` route, whose
``run-files`` segment matched no directory. What is asserted here beyond "it serves the
file": that the namespace carries the permission (``/results`` has no write verbs at
all), that each namespace confines against **its own** root, and that a directory and a
file are distinguishable without probing.
"""

import threading

import pytest
from fastapi.testclient import TestClient

from robovast.service.app import build_app
from robovast.service.client import LocalTransport
from robovast.service.workspaces import WorkspaceRegistry, WorkspaceStore


def _transport(tmp_path):
    """A LocalTransport with both roots pinned under *tmp_path*, and disjoint."""
    store = WorkspaceStore(registry=WorkspaceRegistry(root=tmp_path / "workspaces"))
    lt = LocalTransport.__new__(LocalTransport)
    lt._campaigns = {}
    lt._lock = threading.Lock()
    lt.store = store
    lt._campaigns_root = lambda: tmp_path / "results"
    return lt


@pytest.fixture(name="env")
def _env(tmp_path):
    run_dir = tmp_path / "results" / "camp-1" / "nav" / "3" / "scene"
    run_dir.mkdir(parents=True)
    (run_dir / "scene.json").write_text('{"up": "z"}')
    (run_dir / "scene.bin").write_bytes(b"\x00\x01\x02")
    exec_dir = tmp_path / "results" / "camp-1" / "_execution"
    exec_dir.mkdir(parents=True)
    (exec_dir / "controller.log").write_text("\n".join(f"line {i}" for i in range(50)))
    (tmp_path / "secret.txt").write_text("outside every root")

    transport = _transport(tmp_path)
    ws = transport.store.registry.create("demo")["workspace_id"]
    (transport.store.registry.project_dir(ws)).mkdir(parents=True, exist_ok=True)
    with TestClient(build_app(transport)) as client:
        yield client, transport, ws


# -- reading -----------------------------------------------------------------


def test_serves_bytes_with_content_type(env):
    client, _, _ = env
    resp = client.get("/results/camp-1/nav/3/scene/scene.json")
    assert resp.status_code == 200
    assert resp.json() == {"up": "z"}
    assert resp.headers["content-type"].startswith("application/json")

    # The binary sibling a relative scene.json -> scene.bin fetch resolves to. The
    # address is the real on-disk path, so that relative resolution still lands here.
    resp = client.get("/results/camp-1/nav/3/scene/scene.bin")
    assert resp.status_code == 200
    assert resp.content == b"\x00\x01\x02"
    assert resp.headers["content-type"] == "application/octet-stream"


def test_text_view_pages_and_reports_the_total(env):
    client, _, _ = env
    resp = client.get("/results/camp-1/_execution/controller.log",
                      params={"as": "text", "lines": 5, "offset": 10})
    body = resp.json()
    assert body["total_lines"] == 50
    assert body["returned_lines"] == 5
    assert body["content"].splitlines()[0] == "line 10"
    assert body["address"] == "/results/camp-1/_execution/controller.log"


def test_text_view_refuses_binary_rather_than_mangling_it(env):
    client, _, _ = env
    resp = client.get("/results/camp-1/nav/3/scene/scene.bin", params={"as": "text"})
    assert resp.status_code == 400
    assert "binary" in resp.json()["detail"].lower()


def test_missing_file_is_404(env):
    client, _, _ = env
    assert client.get("/results/camp-1/nav/3/scene/missing.json").status_code == 404


def test_unknown_campaign_is_404(env):
    client, _, _ = env
    assert client.get("/results/nope/_execution/outcome.json").status_code == 404


# -- listing -----------------------------------------------------------------


def test_listing_is_non_recursive_by_default(env):
    client, _, _ = env
    body = client.get("/results/camp-1/").json()
    assert sorted(body["entries"]) == ["_execution/", "nav/"]
    assert body["total"] == 2
    assert body["truncated"] is False
    assert body["recursive"] is False


def test_a_bare_owner_lists_without_the_trailing_slash(env):
    """It can only be a directory, and 404-ing the most obvious URL in the address
    space would be a poor way to teach the address space."""
    client, _, ws = env
    assert client.get("/results/camp-1").json()["address"] == "/results/camp-1/"
    assert client.get(f"/sources/{ws}").status_code == 200


def test_recursive_listing_returns_files_only(env):
    client, _, _ = env
    body = client.get("/results/camp-1/", params={"recursive": 1}).json()
    assert "nav/3/scene/scene.json" in body["entries"]
    assert not any(e.endswith("/") for e in body["entries"])


def test_listing_truncation_still_reports_the_total(env):
    client, _, _ = env
    body = client.get("/results/camp-1/", params={"limit": 1}).json()
    assert len(body["entries"]) == 1
    assert body["total"] == 2
    assert body["truncated"] is True


def test_a_file_is_not_a_directory(env):
    client, _, _ = env
    resp = client.get("/results/camp-1/_execution/controller.log/")
    assert resp.status_code == 400
    assert "not a directory" in resp.json()["detail"]


def test_a_directory_is_not_a_file(env):
    """The mirror case, and a 400 rather than a 404: the thing exists, the question
    was the wrong one — so the answer points at the listing."""
    _client, transport, _ = env
    with pytest.raises(ValueError, match="not a file"):
        transport.read_file_bytes("/results/camp-1/nav")
    with pytest.raises(ValueError, match="not a file"):
        transport.read_file("/results/camp-1/nav")


# -- the namespace is the permission -----------------------------------------


@pytest.mark.parametrize("method", ["put", "post", "delete"])
def test_results_has_no_write_verbs_at_all(env, method):
    """Read-only by *registration*, so this is a 405 from the router — not a check
    inside a handler that a later handler could forget."""
    client, _, _ = env
    resp = client.request(method.upper(), "/results/camp-1/nav/3/x.txt",
                          json={"content": "x"})
    assert resp.status_code == 405


def test_sources_round_trips_write_edit_read_delete(env):
    client, _, ws = env
    address = f"/sources/{ws}/demo.vast"
    assert client.put(address, json={"content": "a: 1\n"}).status_code == 200
    assert client.post(address, json={"old_string": "a: 1",
                                      "new_string": "a: 2"}).status_code == 200
    assert client.get(address, params={"as": "text"}).json()["content"] == "a: 2"
    assert client.delete(address).status_code == 200
    assert client.get(address).status_code == 404


def test_sources_refuses_a_non_inline_type(env):
    client, _, ws = env
    resp = client.put(f"/sources/{ws}/run.py", json={"content": "print()"})
    assert resp.status_code == 400
    assert "create_upload" in resp.json()["detail"]


def test_upload_grant_round_trips_over_http(env):
    """The non-inline write path, end to end.

    Only the in-process transport discards the redeem result, so a missing field here
    is invisible locally and fails every HTTP client — ``vast workspace init`` against
    a remote serve, the UI's directory upload, ``vast files put`` for anything that is
    not a ``.vast``/``.osc``.
    """
    client, _, ws = env
    address = f"/sources/{ws}/files/run.sh"
    grant = client.post("/uploads", json={"address": address, "executable": True})
    assert grant.status_code == 200
    token = grant.json()["token"]

    written = client.put(f"/uploads/{token}", content=b"#!/bin/sh\necho hi\n")
    assert written.status_code == 200, written.text
    assert written.json()["address"] == address
    assert written.json()["executable"] is True

    assert client.get(address, params={"as": "text"}).json()["content"].startswith("#!")


def test_an_upload_grant_cannot_be_redeemed_twice(env):
    client, _, ws = env
    token = client.post("/uploads",
                        json={"address": f"/sources/{ws}/a.bin"}).json()["token"]
    assert client.put(f"/uploads/{token}", content=b"x").status_code == 200
    assert client.put(f"/uploads/{token}", content=b"x").status_code == 400


# -- confinement: each namespace against its own root ------------------------


def test_path_escape_is_rejected(env):
    client, transport, _ = env
    # httpx normalizes ../ in URLs, so exercise the transport guard directly too.
    resp = client.get("/results/camp-1/..%2F..%2Fsecret.txt")
    assert resp.status_code in (400, 404)
    with pytest.raises(ValueError):
        transport.read_file_bytes("/results/camp-1/../../secret.txt")


def test_a_results_address_cannot_reach_a_workspace(env):
    _, transport, ws = env
    with pytest.raises(KeyError):
        # The workspace id is not a campaign, and resolving it as one must not fall
        # through to the workspace tree: the read-only namespace would otherwise
        # inherit the writable one's contents.
        transport.list_files(f"/results/{ws}/")


def test_a_sources_address_cannot_reach_a_campaign(env):
    _, transport, _ = env
    with pytest.raises(Exception) as excinfo:
        transport.list_files("/sources/camp-1/")
    assert "camp-1" in str(excinfo.value)


def test_unknown_namespace_names_the_valid_ones(env):
    client, _, _ = env
    resp = client.get("/campaigns/camp-1/run-files/nav/3/scene/scene.json")
    # The retired route is simply gone; nothing answers on it.
    assert resp.status_code == 404


# -- large files are streamed, not buffered ----------------------------------


def test_a_binary_read_is_streamed_and_seekable(env):
    """Served with FileResponse, so ``Range`` works and the service holds no copy.

    The route used to read the whole file into memory and return it as one Response.
    A campaign's rosbag is tens of megabytes and up, so every request cost that much
    service memory to hand back bytes it never inspects — and without ``Range`` a browser
    had to download a whole ``.webm`` before it could play a second of it.
    """
    client, _lt, _ws = env
    url = "/results/camp-1/nav/3/scene/scene.bin"

    whole = client.get(url)
    assert whole.status_code == 200
    assert whole.content == b"\x00\x01\x02"
    assert whole.headers["accept-ranges"] == "bytes"

    part = client.get(url, headers={"Range": "bytes=1-2"})
    assert part.status_code == 206, "a ranged read must not return the whole file"
    assert part.content == b"\x01\x02"


def test_a_missing_file_still_refuses_by_address(env):
    """The streaming path must keep the address-space error, not leak a filesystem one."""
    client, _lt, _ws = env
    res = client.get("/results/camp-1/nav/3/scene/nope.bin")
    assert res.status_code == 404
    assert "nope.bin" in res.json()["detail"]


# -- the streaming path is per-lane -------------------------------------------


def test_a_cluster_binary_read_fetches_only_that_object(tmp_path):
    """A ``/results`` read on the cluster lane materializes ONE object, and still seeks.

    The route cannot pick a lane by asking whether the transport has ``local_file``:
    ``ClusterService`` subclasses ``LocalTransport``, so it always does. When that check
    was believed to be meaningful, a cluster campaign fell through to the *local*
    resolver, whose ``_data_dir`` is ``fetch_campaign`` — pulling the whole campaign,
    every rosbag included, to hand back one file. A ``<video>`` tag paid for gigabytes on
    first play and nothing in the request said so.
    """
    from robovast.execution.cluster_execution.cluster_service import ClusterService

    cache = tmp_path / "cache"
    fetched: list[tuple] = []

    class _Cluster(ClusterService):
        def __init__(self):  # pylint: disable=super-init-not-called
            self._campaigns = {}
            self._lock = threading.Lock()

        def _materialize(self, campaign_id, rel_paths, subject, *, interactive=False):
            rel_paths = list(rel_paths)
            fetched.append((campaign_id, tuple(rel_paths), interactive))
            for rel in rel_paths:
                dst = cache / rel
                dst.parent.mkdir(parents=True, exist_ok=True)
                dst.write_bytes(b"\x00\x01\x02")
            return cache

        # a double that refuses whatever it is sent
        def fetch_campaign(self, campaign_id, **_):  # pylint: disable=arguments-differ
            raise AssertionError(
                "serving one file must not pull the whole campaign")

    with TestClient(build_app(_Cluster())) as client:
        url = "/results/camp-1/nav/3/rosbag2_cam.webm"
        whole = client.get(url)
        assert whole.status_code == 200
        assert whole.content == b"\x00\x01\x02"
        assert whole.headers["accept-ranges"] == "bytes"

        part = client.get(url, headers={"Range": "bytes=1-2"})
        assert part.status_code == 206, "the cluster lane must seek, not buffer"
        assert part.content == b"\x01\x02"

    assert fetched, "the object was never fetched"
    assert all(paths == ("nav/3/rosbag2_cam.webm",) for _, paths, _ in fetched), fetched
    assert all(interactive for *_, interactive in fetched), \
        "a browser waiting on media is not a batch transfer"


def test_a_transport_that_serves_no_files_says_so(tmp_path):
    """``local_file`` refuses by name rather than vanishing as a missing attribute.

    The default on the interface exists for implementations that only *call* a service
    and have no local file to offer; the route asks for it outright, so the refusal has
    to be a sentence rather than an ``AttributeError``.
    """
    from robovast.service.interface import RobovastInterface

    with pytest.raises(NotImplementedError, match="serves no local files"):
        RobovastInterface.local_file(object(), "/results/camp-1/x.bin")
