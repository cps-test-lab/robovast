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


def push_project_to_workspace(client, config_path: str, name: str = "") -> str:
    """Upload the project rooted at *config_path*'s directory into a new workspace.

    ``.vast``/``.osc`` files go inline; everything else (run files, notebooks,
    binaries) streams through the PUT side channel with the executable bit
    preserved. Returns the new ``workspace_id``.
    """
    import requests
    from robovast.service.interface import (CreateUploadRequest,
                                            CreateWorkspaceRequest,
                                            WriteFileRequest)

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
        rel = str(rel_path)
        if path.suffix.lower() in _INLINE_EXTS:
            client.write_project_file(WriteFileRequest(
                workspace_id=workspace_id, path=rel,
                content=path.read_text(encoding="utf-8")))
        else:
            executable = bool(path.stat().st_mode & 0o111)
            grant = client.create_upload(CreateUploadRequest(
                workspace_id=workspace_id, path=rel, executable=executable))
            resp = requests.put(grant.url, data=path.read_bytes(), timeout=120)
            resp.raise_for_status()
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
