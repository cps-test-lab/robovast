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

# Generated/cache artefacts that must not be pushed as project inputs. ``results`` is
# here for the same reason ``vast workspace init`` excludes it: it is a campaign's
# *output*, and pushing it uploads every past campaign on disk as project input on every
# launch. Only the default name is known — a project whose ``.vast_project`` names a
# different results dir still uploads it, and there is no way to learn that name from the
# ``.vast`` alone.
_SKIP_DIRS = {".cache", ".preprocessed", "resolved", "_execution", "_transient",
              "_config", "_control", "_jobs", "__pycache__", ".git", "results"}


def _is_generated(rel: Path) -> bool:
    """True if *rel* is a generated/cache/hidden artefact rather than authored input.

    The same predicate on both sides of a push: locally these are build leftovers not
    worth uploading, and **inside a workspace they belong to the service** — a campaign
    writes ``.cache/`` (config generation), ``.robovast_plugins/`` and ``resolved/``
    into the project dir it runs from. So a mirroring push must neither send them nor
    delete them; pruning the service's own cache forces a full regeneration on every
    relaunch, and does it while a campaign may still be reading it.
    """
    return any(p in _SKIP_DIRS or p.startswith(".") for p in rel.parts)


def _is_project_input(rel: Path, main_vast: str) -> bool:
    """True if *rel* is a real project input to push.

    Excludes generated/cache/hidden directories, hidden files, and any ``.vast``
    other than the one being run (*main_vast*) — so generated/variation ``.vast``
    files don't violate the one-``.vast``-per-workspace rule.
    """
    if _is_generated(rel):
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
        # The client's own session, not a bare requests.put: the upload route is
        # behind the same authentication as everything else, and a fresh request
        # would carry no credentials.
        client.session.put(grant.url, data=data, timeout=120).raise_for_status()
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


def push_project_files(client, workspace_id: str, config_path: str, *,
                       prune: bool = False, echo=None) -> dict:
    """Push the project rooted at *config_path*'s directory into an existing workspace.

    ``.vast``/``.osc`` files go inline; everything else (run files, notebooks,
    binaries) streams through the PUT side channel with the executable bit
    preserved. With *prune*, workspace files absent from the project are deleted, so
    the workspace mirrors the directory — what a *launch* wants, since a stale run
    file left from an earlier push would be copied into the campaign.

    Deliberately not :func:`sync_directory_to_workspace`: the predicate here is
    :func:`_is_project_input`, which additionally drops ``__pycache__``, ``_config/``,
    ``_transient/`` and every ``.vast`` other than the one being run — the last of
    which is what keeps the one-``.vast``-per-workspace rule.

    Prune covers everything :func:`_is_generated` does *not* claim — so a stale run file
    goes, and so does a ``.vast`` renamed since the last push (which would otherwise
    survive as a second one and make the workspace unlaunchable), while the service's
    own ``.cache/`` and staged plugins are left where they are.

    Returns ``{"written", "uploaded", "pruned"}`` counts.
    """
    config_path = Path(config_path).resolve()
    project_dir = config_path.parent
    main_vast = config_path.name
    stats = {"written": 0, "uploaded": 0, "pruned": 0}
    local_rels: set[str] = set()

    for path in sorted(project_dir.rglob("*")):
        if not path.is_file():
            continue
        rel_path = path.relative_to(project_dir)
        if not _is_project_input(rel_path, main_vast):
            continue
        rel_str = rel_path.as_posix()
        kind = push_file(client, format_address(SOURCES, workspace_id, rel_str), path)
        stats["written" if kind == "written" else "uploaded"] += 1
        local_rels.add(rel_str)
        if echo:
            echo(f"  + {rel_str}")

    if prune:
        existing = client.list_files(format_address(SOURCES, workspace_id),
                                     recursive=True, limit=0).entries
        for rel_str in sorted(existing):
            if rel_str in local_rels or _is_generated(Path(rel_str)):
                continue
            client.delete_file(format_address(SOURCES, workspace_id, rel_str))
            stats["pruned"] += 1
            if echo:
                echo(f"  - {rel_str} (pruned)")

    logger.info("Pushed project %s into workspace %s (%s)",
                project_dir, workspace_id, stats)
    return stats


def push_project_to_workspace(client, config_path: str, name: str = "") -> str:
    """Upload the project rooted at *config_path*'s directory into a **new** workspace.

    Returns the new ``workspace_id``. To reuse the workspace a project already has,
    see :func:`workspace_for_project`.
    """
    from robovast.service.interface import CreateWorkspaceRequest

    project_dir = Path(config_path).resolve().parent
    workspace_id = client.create_workspace(
        CreateWorkspaceRequest(name=name or project_dir.name)).workspace_id
    push_project_files(client, workspace_id, config_path)
    return workspace_id


def workspace_for_project(client, config_path: str, name: str = "",
                          *, on_exists=None) -> tuple[str, str]:
    """The workspace to push *config_path*'s project into; ``(workspace_id, action)``.

    Named after the project directory (or *name*), and **reused** when it is already
    there: launching the same project twice must not leave ``myproj``, ``myproj-2``,
    ``myproj-3`` behind — the server auto-suffixes a colliding name, so creating
    unconditionally accumulates one workspace per launch.

    *on_exists* ``(name, workspace_id) -> bool`` is asked before an existing workspace
    is reused, since reusing it overwrites its files. A false answer raises, so nothing
    is pushed or launched. Absent, the workspace is reused.

    ``action`` is ``"created"`` or ``"reused"``, for the caller to report.
    """
    from robovast.service.interface import CreateWorkspaceRequest

    project_dir = Path(config_path).resolve().parent
    wanted = name or project_dir.name

    matches = [w for w in client.list_workspaces().workspaces if w.name == wanted]
    if len(matches) > 1:
        # The registry auto-suffixes, so same-named rows only exist if they were made
        # by hand. Refuse rather than pick one and overwrite the wrong project.
        ids = ", ".join(w.workspace_id for w in matches)
        raise ValueError(
            f"{len(matches)} workspaces are named {wanted!r} ({ids}); "
            "pass an explicit workspace name")
    if matches:
        workspace_id = matches[0].workspace_id
        # Before the prompt, because this one is not the caller's to wave through: a
        # campaign reads its project out of the workspace for its whole life, so a push
        # now would change an experiment that is still running. Refuse and name it.
        running = list(getattr(matches[0], "running_campaigns", None) or [])
        if running:
            raise ValueError(
                f"workspace {wanted!r} ({workspace_id}) is being read by "
                f"{', '.join(running)} — pushing to it now would change a running "
                "campaign's project. Wait for it, stop it, or launch into another "
                "workspace")
        if on_exists is not None and not on_exists(wanted, workspace_id):
            raise ValueError(f"declined to overwrite workspace {wanted!r} ({workspace_id})")
        return workspace_id, "reused"

    created = client.create_workspace(CreateWorkspaceRequest(name=wanted))
    return created.workspace_id, "created"


def run_project_via_service(client, config_path: str,
                            config_filter: str = "", runs: int = 0,
                            feedback=None, upload_to_share: bool = False,
                            campaign_name: str = "", description: str = "",
                            workspace_name: str = "", on_exists=None) -> str:
    """Push the local project through *client* and start a campaign. Returns id.

    ``runs=0`` means "whatever the ``.vast`` declares": the service maps a non-positive
    count to ``None`` and falls back to ``execution.runs``. Any other substitute for
    "unset" is an override nobody asked for, and it shrinks the campaign silently —
    fewer repetitions is not a failure any later stage can notice.
    """
    from robovast.service.interface import CreateCampaignRequest

    say = feedback or logger.info
    workspace_id, action = workspace_for_project(
        client, config_path, workspace_name, on_exists=on_exists)
    say(f"Pushing project to robovast-service ({action} workspace {workspace_id}) ...")
    push_project_files(client, workspace_id, config_path, prune=True)
    say(f"Uploaded to workspace {workspace_id}; starting campaign ...")
    ref = client.create_campaign(CreateCampaignRequest(
        workspace_id=workspace_id, config_filter=config_filter,
        campaign_name=campaign_name, description=description,
        runs=runs if runs and runs > 0 else 0,
        upload_to_share=upload_to_share))
    return ref.campaign_id


def download_campaign_via_service(client, campaign_id: str,
                                  results_dir: str, feedback=None) -> str:
    """Download a campaign's ``tar.gz`` through *client* and extract it locally.

    The service streams the campaign from the object store (no external share).
    Returns the local campaign directory path.
    """
    import tarfile

    from robovast.service.interface import Routes

    say = feedback or logger.info
    url = f"{client.base_url}{Routes.CAMPAIGNS}/{campaign_id}/archive"
    say(f"Downloading {campaign_id} from robovast-service ...")
    os.makedirs(results_dir, exist_ok=True)
    # Stream-extract straight off the socket (mode "r|gz") so a large (up to ~1TB)
    # campaign never has to be buffered in memory on the client.
    with client.session.get(url, timeout=600, stream=True) as resp:
        resp.raise_for_status()
        resp.raw.decode_content = True
        with tarfile.open(fileobj=resp.raw, mode="r|gz") as tar:
            tar.extractall(results_dir)  # noqa: S202 - trusted service, arcname=campaign_id
    dest = os.path.join(results_dir, campaign_id)
    say(f"Extracted to {dest}")
    return dest
