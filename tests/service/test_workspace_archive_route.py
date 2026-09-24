# Copyright (C) 2026 Frederik Pasch
# SPDX-License-Identifier: Apache-2.0
"""``GET /workspaces/{id}/archive`` — a project leaves the service as one file.

A workspace could only be taken off a service file by file, which a browser cannot do at
all. The archive is the one shape that serves every direction: the download button, ``vast
workspace download``, and the bytes the share carries — so what a colleague extracts and
what another deployment imports are the same tree.

Defended here: the single top-level directory, because an import strips exactly one
component and a flat archive would scatter a project across whatever directory it was
unpacked in.
"""

import tarfile
from io import BytesIO

import pytest
from fastapi.testclient import TestClient

from robovast.service.app import build_app
from robovast.service.workspaces import WorkspaceRegistry, WorkspaceStore
from tests.service.null_service import NullService


def _transport(tmp_path) -> NullService:
    store = WorkspaceStore(registry=WorkspaceRegistry(root=tmp_path / "workspaces"))
    lt = NullService(store=store)
    lt._campaigns_root = lambda: tmp_path / "results"
    return lt


@pytest.fixture(name="env")
def _env(tmp_path):
    transport = _transport(tmp_path)
    ws = transport.store.registry.create(name="growth sim")["workspace_id"]
    project = transport.store.registry.project_dir(ws)
    (project / "files").mkdir(parents=True)
    (project / "campaign.vast").write_text("configuration:\n  name: x\n", encoding="utf-8")
    (project / "files" / "params.yaml").write_text("a: 1\n", encoding="utf-8")
    run = project / "run.sh"
    run.write_text("#!/bin/bash\necho hi\n", encoding="utf-8")
    run.chmod(0o755)
    with TestClient(build_app(transport)) as client:
        yield client, ws


def _members(payload: bytes) -> set:
    with tarfile.open(fileobj=BytesIO(payload), mode="r:gz") as tar:
        return set(tar.getnames())


def test_the_project_arrives_under_one_directory(env):
    client, ws = env
    resp = client.get(f"/workspaces/{ws}/archive")
    assert resp.status_code == 200, resp.text
    assert resp.headers["content-disposition"] == f'attachment; filename="{ws}.tar.gz"'

    names = _members(resp.content)
    # One top-level entry, named for the workspace -- the shape an import strips.
    assert {n.split("/")[0] for n in names} == {ws}
    assert f"{ws}/campaign.vast" in names
    assert f"{ws}/files/params.yaml" in names


def test_the_executable_bit_travels_with_the_file(env):
    """A run script that arrives non-executable fails at the point of use, where nothing
    explains it -- so the archive carries the mode rather than the extractor guessing."""
    client, ws = env
    with tarfile.open(fileobj=BytesIO(client.get(f"/workspaces/{ws}/archive").content),
                      mode="r:gz") as tar:
        assert tar.getmember(f"{ws}/run.sh").mode & 0o111


def test_a_name_resolves_and_the_file_is_named_by_the_id(env):
    """The archive is offered under the workspace *id* even when a name was used to ask.

    A name is free text; what a browser saves is whatever the header says, and a name with a
    slash or a quote in it reaches a downloads directory as that.
    """
    client, ws = env
    resp = client.get("/workspaces/growth sim/archive")
    assert resp.status_code == 200, resp.text
    assert resp.headers["content-disposition"] == f'attachment; filename="{ws}.tar.gz"'


def test_an_unknown_workspace_is_refused_before_any_bytes(env):
    """The refusal is the status line, not a truncated body.

    The id is resolved before the stream is opened, because past the first byte the status
    is already 200 and a failure reaches the caller as an archive that unpacks to nothing.
    The code is the one every other workspace route answers for an id it does not have --
    the registry's own refusal, which names what it does hold.
    """
    client, _ = env
    resp = client.get("/workspaces/ws-nope/archive")
    assert resp.status_code == 400, resp.text
    assert "ws-nope" in resp.text
