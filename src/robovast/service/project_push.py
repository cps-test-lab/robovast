# Copyright (C) 2026 Frederik Pasch
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
# http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing,
# software distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions
# and limitations under the License.
#
# SPDX-License-Identifier: Apache-2.0

"""Push a **local** project to a service workspace, then run it via the service.

This is how a CLI client with local project files (``vast exec cluster run``)
drives a remote/cluster ``robovast-service``: it uploads the project into a
server-side workspace (``.vast``/``.osc`` inline, everything else via the HTTP
PUT side channel, preserving executables), then calls ``create_campaign``.

Reused by the CLI; the LocalTransport/HTTP client itself stays transport-agnostic.
"""

import logging
import os
from pathlib import Path

from robovast.common.file_address import SOURCES, format_address
from robovast.service.workspaces import is_skipped as _should_skip

logger = logging.getLogger(__name__)

_INLINE_EXTS = (".vast", ".osc")

# Generated/cache artefacts that must not be pushed as project inputs.
_SKIP_DIRS = {".cache", ".preprocessed", "resolved", "_execution", "_transient",
              "_config", "_control", "_jobs", "__pycache__", ".git"}


def _is_project_input(rel: Path, main_vast: str) -> bool:
    """True if *rel* is a real project input to push.

    Excludes generated/cache/hidden directories, hidden files, and any ``.vast``
    other than the one being run (*main_vast*) — so generated/variation ``.vast``
    files don't violate the one-``.vast``-per-workspace rule.
    """
    if any(p in _SKIP_DIRS or p.startswith(".") for p in rel.parts):
        return False
    if rel.suffix.lower() == ".vast" and rel.name != main_vast:
        return False
    return True


def push_file(client, address: str, path: Path) -> str:
    """Push one local file to a ``/sources`` *address*. Returns ``"written"``/``"uploaded"``.

    ``.vast``/``.osc`` go inline (last-write-wins, so this both creates and
    overwrites); everything else streams through the PUT side channel with the
    executable bit preserved. The one place that knows about both transports: the
    HTTP client issues an absolute PUT URL (``grant.url``), the in-process
    ``LocalTransport`` exposes ``client.store`` for a direct write.

    Public and address-taking because ``vast files put`` needs exactly this and
    should not have to reach for a private helper or re-derive the address itself.
    """
    from robovast.service.interface import CreateUploadRequest, WriteFileRequest

    if path.suffix.lower() in _INLINE_EXTS:
        client.write_file(WriteFileRequest(
            address=address, content=path.read_text(encoding="utf-8")))
        return "written"

    grant = client.create_upload(CreateUploadRequest(
        address=address, executable=os.access(path, os.X_OK)))
    data = path.read_bytes()
    if grant.url:  # HTTP service issued an absolute PUT URL
        import requests
        requests.put(grant.url, data=data, timeout=120).raise_for_status()
    elif hasattr(client, "store"):  # in-process LocalTransport
        client.store.write_upload(grant.token, data)
    else:
        raise RuntimeError(
            f"cannot upload {address!r}: this client has no upload channel")
    return "uploaded"


def _resolve_workspace_id(client, ref: str) -> str:
    """Resolve a workspace id-or-name to a concrete ``workspace_id``.

    A ``ws-…`` id is returned as-is; anything else is matched by name against the
    service's workspaces. Fails loudly on no match or an ambiguous name, so a typo
    never silently targets the wrong workspace.
    """
    if ref.startswith("ws-"):
        return ref
    matches = [w for w in client.list_workspaces().workspaces if w.name == ref]
    if not matches:
        raise ValueError(f"no workspace named {ref!r}")
    if len(matches) > 1:
        raise ValueError(
            f"workspace name {ref!r} is ambiguous ({len(matches)} matches); "
            "use the ws-… id")
    return matches[0].workspace_id


def sync_directory_to_workspace(client, workspace_id: str, directory, *,
                                skip_dirs=frozenset(), prune: bool = False,
                                echo=None) -> dict:
    """Re-sync a local *directory* into an **existing** workspace.

    Uploads every non-hidden file under *directory* (``.vast``/``.osc`` inline,
    the rest via the PUT side channel), overwriting in place. Hidden files/dirs
    and any directory named in *skip_dirs* are skipped. With *prune*, workspace
    files absent from *directory* are deleted (full mirror). *echo* (e.g.
    ``click.echo``) receives one ``+``/``-`` line per change. Returns
    ``{"written", "uploaded", "pruned"}`` counts.
    """
    root = Path(directory).resolve()
    stats = {"written": 0, "uploaded": 0, "pruned": 0}
    local_rels: set[str] = set()
    for path in sorted(root.rglob("*")):
        if not path.is_file():
            continue
        rel = path.relative_to(root)
        if _should_skip(rel, skip_dirs):
            continue
        rel_str = rel.as_posix()
        kind = push_file(client, format_address(SOURCES, workspace_id, rel_str), path)
        stats["written" if kind == "written" else "uploaded"] += 1
        local_rels.add(rel_str)
        if echo:
            echo(f"  + {rel_str}")

    if prune:
        existing = client.list_files(format_address(SOURCES, workspace_id),
                                     recursive=True, limit=0).entries
        for rel_str in sorted(existing):
            if rel_str in local_rels or _should_skip(Path(rel_str), skip_dirs):
                continue
            client.delete_file(format_address(SOURCES, workspace_id, rel_str))
            stats["pruned"] += 1
            if echo:
                echo(f"  - {rel_str} (pruned)")
    logger.info("Synced %s into workspace %s (%s)", root, workspace_id, stats)
    return stats


def push_project_to_workspace(client, config_path: str, name: str = "") -> str:
    """Upload the project rooted at *config_path*'s directory into a new workspace.

    ``.vast``/``.osc`` files go inline; everything else (run files, notebooks,
    binaries) streams through the PUT side channel with the executable bit
    preserved. Returns the new ``workspace_id``.
    """
    from robovast.service.interface import CreateWorkspaceRequest

    config_path = Path(config_path).resolve()
    project_dir = config_path.parent
    main_vast = config_path.name
    workspace_id = client.create_workspace(
        CreateWorkspaceRequest(name=name or project_dir.name)).workspace_id

    for path in sorted(project_dir.rglob("*")):
        if not path.is_file():
            continue
        rel_path = path.relative_to(project_dir)
        if not _is_project_input(rel_path, main_vast):
            continue
        push_file(client, format_address(SOURCES, workspace_id,
                                        rel_path.as_posix()), path)
    logger.info("Pushed project %s into workspace %s", project_dir, workspace_id)
    return workspace_id


def run_project_via_service(client, config_path: str,
                            config_filter: str = "", runs: int = 1,
                            feedback=None, upload_to_share: bool = False,
                            campaign_name: str = "") -> str:
    """Push the local project through *client* and start a campaign. Returns id."""
    from robovast.service.interface import CreateCampaignRequest

    say = feedback or logger.info
    say("Pushing project to robovast-service ...")
    workspace_id = push_project_to_workspace(client, config_path)
    say(f"Uploaded to workspace {workspace_id}; starting campaign ...")
    ref = client.create_campaign(CreateCampaignRequest(
        workspace_id=workspace_id, config_filter=config_filter,
        campaign_name=campaign_name,
        runs=runs if runs and runs > 0 else 1,
        upload_to_share=upload_to_share))
    return ref.campaign_id


def download_campaign_via_service(client, campaign_id: str,
                                  results_dir: str, feedback=None) -> str:
    """Download a campaign's ``tar.gz`` through *client* and extract it locally.

    The service streams the campaign from the object store (no external share).
    Returns the local campaign directory path.
    """
    import tarfile

    import requests
    from robovast.service.interface import Routes

    say = feedback or logger.info
    url = f"{client.base_url}{Routes.CAMPAIGNS}/{campaign_id}/archive"
    say(f"Downloading {campaign_id} from robovast-service ...")
    os.makedirs(results_dir, exist_ok=True)
    # Stream-extract straight off the socket (mode "r|gz") so a large (up to ~1TB)
    # campaign never has to be buffered in memory on the client.
    with requests.get(url, timeout=600, stream=True) as resp:
        resp.raise_for_status()
        resp.raw.decode_content = True
        with tarfile.open(fileobj=resp.raw, mode="r|gz") as tar:
            tar.extractall(results_dir)  # noqa: S202 - trusted service, arcname=campaign_id
    dest = os.path.join(results_dir, campaign_id)
    say(f"Extracted to {dest}")
    return dest
