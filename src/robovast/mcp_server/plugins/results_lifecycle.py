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

"""MCP plugin: what happens to a campaign's results after it has run.

Re-deriving them (postprocessing), publishing them (share), and disposing of them
(cleanup, delete, download). Separate from :mod:`execution` because these act on a
campaign that has already finished, and separate from :mod:`results` because they *change*
or move the results rather than read them.
"""

import logging

from fastmcp import FastMCP

from robovast.mcp_server import service_access
from robovast.mcp_server.service_access import NO_SERVICE

logger = logging.getLogger(__name__)


def get_postprocessing(campaign_id: str) -> dict:
    """Show a campaign's effective analysis-postprocessing entries + edit history.

    Raw rosbags are always preserved, so postprocessing can be edited and re-run
    to compute *different* metrics later without re-executing the campaign. The
    immutable ``_config/`` snapshot is never changed; edits are versioned
    overrides. Pair with :func:`update_postprocessing` + :func:`run_postprocessing`.

    Returns:
        ``{campaign_id, source, entries, revisions}`` or ``{error}``.
    """
    try:
        return service_access.client_or_local() \
            .get_postprocessing(campaign_id).model_dump()
    except Exception as e:  # noqa: BLE001
        return {"error": str(e)}


def update_postprocessing(campaign_id: str, entries: list) -> dict:
    """Replace a campaign's analysis-postprocessing entries (a new versioned override).

    ``entries`` is a list of postprocessing commands — a bare plugin name
    (``"rosbags_to_csv"``) or a single-key dict with params
    (``{"command": {"script": "postprocess.sh"}}``). Validated before writing;
    the ``_config/`` snapshot is untouched. Call :func:`run_postprocessing` to apply.

    Returns:
        ``{campaign_id, revision, entries}`` or ``{error}``.
    """
    from robovast.service.interface import UpdatePostprocessingRequest
    try:
        return service_access.client_or_local() \
            .update_postprocessing(UpdatePostprocessingRequest(
                campaign_id=campaign_id, entries=entries)).model_dump()
    except Exception as e:  # noqa: BLE001
        return {"error": str(e)}


def run_postprocessing(campaign_id: str, force: bool = False,
                       skip: list | None = None) -> dict:
    """(Re)run analysis postprocessing for one campaign, rebuilding ``data.db``.

    **Dispatched in the background** — returns as soon as the run is started (it can take
    minutes to hours). The campaign enters the ``postprocessing`` phase; poll
    :func:`get_campaign_status` for progress and the outcome (``postprocessed`` /
    ``postprocessing_error``). Reprocesses just this campaign (not its siblings), reading
    its own ``_config/<name>.vast``. Returns ``{ok, message}`` where *message* confirms
    the dispatch, or ``ok=false`` if an operation is already running for the campaign.

    Args:
        campaign_id: The campaign to (re)process.
        force: Bypass per-rosbag caches and reprocess all bags.
        skip: Plugin names to skip (e.g. ``["rosbags_to_webm"]``).
    """
    from robovast.service.interface import RunPostprocessingRequest
    try:
        return service_access.client_or_local() \
            .run_postprocessing(RunPostprocessingRequest(
                campaign_id=campaign_id, force=force, skip=skip or [])).model_dump()
    except Exception as e:  # noqa: BLE001
        return {"error": str(e)}


def run_share(campaign_id: str) -> dict:
    """(Re)trigger the upload-to-share of one finished campaign's raw archive.

    **Dispatched in the background** — returns as soon as the upload is started; the
    campaign enters the ``sharing`` phase, so poll :func:`get_campaign_status` for the
    outcome (``share_error`` on failure). Works from disk with no live campaign (usable
    after a `vast serve` restart). The target provider comes from the service environment
    (``ROBOVAST_SHARE_TYPE`` + credentials): adjust it and re-trigger to upload to a
    different provider. Fails loudly if no share provider is configured.

    Args:
        campaign_id: The finished campaign to (re)upload.
    """
    from robovast.service.interface import RunShareRequest
    try:
        return service_access.client_or_local() \
            .run_share(RunShareRequest(campaign_id=campaign_id)).model_dump()
    except Exception as e:  # noqa: BLE001
        return {"error": str(e)}


def cleanup_campaign_data(campaign_id: str = "", force: bool = False) -> dict:
    """Delete campaign result data (object-store bucket(s)) for a cluster campaign.

    Frees storage once results have been downloaded or published and are no longer
    needed. This goes **through the robovast-service**, which owns the object-store
    credentials and knows which campaigns are still live — so there is **no
    infrastructure to deal with** here: no kubeconfig, no S3 keys, no namespaces.

    Args:
        campaign_id: The campaign whose data to delete. Empty string deletes **all**
            finished campaigns' data (campaigns still running are always skipped).
        force: Delete a named campaign even if the service still considers it live.

    Returns:
        ``{ok, message}`` (``message`` reports how many buckets were removed), or
        ``{error}`` if no service is reachable / the backend has no object store.
    """
    from robovast.service.interface import CleanupDataRequest
    client = service_access.service_client()
    if client is None:
        return {"error": f"{NO_SERVICE}. The data to clean up lives with the "
                          "service, not on this host."}
    try:
        res = client.cleanup_campaign_data(
            CleanupDataRequest(campaign_id=campaign_id or None, force=force))
        return {"ok": res.ok, "message": res.message}
    except Exception as e:  # noqa: BLE001
        return {"error": str(e)}


def delete_campaign(campaign_id: str) -> dict:
    """Permanently delete **one** campaign wholesale, through the robovast-service.

    Removes the campaign's durable home — its local directory on a local service,
    or its object-store data (plus any leftover Kubernetes Jobs and the service's
    cache) on a cluster service. This is the full "forget this campaign" action, as
    opposed to :func:`cleanup_campaign_data`, which only frees object-store buckets.

    The service refuses a campaign that is still running — stop it first with
    :func:`stop_campaign`. The external share copy (if any) is never touched. This
    is irreversible.

    Args:
        campaign_id: The campaign to delete.

    Returns:
        ``{ok, message}`` on success, or ``{error}`` if no service is reachable or
        the campaign is still running.
    """
    client = service_access.service_client()
    if client is None:
        return {"error": NO_SERVICE}
    try:
        res = client.delete_campaign(campaign_id)
        return {"ok": res.ok, "message": res.message}
    except Exception as e:  # noqa: BLE001
        return {"error": str(e)}


def get_campaign_download(campaign_id: str) -> dict:
    """Return **where to download** a campaign — a web link, not a file on this host.

    Downloading is a browser action: the campaign archive is served by the
    robovast-service (and its web UI) at a fixed path, so this returns that URL for
    **you** to open where your robovast web UI runs — it never writes a file onto the
    MCP-server host (which you may not be able to reach if the server runs elsewhere).

    Args:
        campaign_id: The campaign id to download.

    Returns:
        For a **cluster** service: ``{campaign_id, url, path, note}`` — ``url`` is the
        postprocessed ``tar.gz`` (full campaign, incl. derived data) streamed from the
        object store. For a **local** service: ``{campaign_id, note}`` — the results
        already live on the service host's filesystem, so there is no HTTP download.
        ``{error}`` when no service is reachable.
    """
    client = service_access.service_client()
    if client is None:
        return {"error": f"{NO_SERVICE}. The campaign lives with the service, not "
                          "on this host."}
    try:
        backend = client.version().backend
    except Exception as e:  # noqa: BLE001
        return {"error": f"could not reach the service: {e}"}

    path = f"/campaigns/{campaign_id}/archive"
    if backend == "kubernetes":
        return {
            "campaign_id": campaign_id,
            "url": f"{client.base_url}{path}",
            "path": path,
            "note": ("Open this in the browser where your robovast web UI runs "
                     "(or Monitor → Download), or run "
                     f"'vast results download -i {campaign_id}' on your own machine "
                     "('--variant raw' for the pre-postprocess archive from the share)."),
        }
    return {
        "campaign_id": campaign_id,
        "note": ("This is a local service — the campaign results are already on the "
                 "service host's filesystem; there is no HTTP download."),
    }


# -- Plugin class ------------------------------------------------------------

_TOOLS = [
    get_postprocessing,
    update_postprocessing,
    run_postprocessing,
    run_share,
    cleanup_campaign_data,
    delete_campaign,
    get_campaign_download,
]


class ResultsLifecyclePlugin:
    """MCP plugin: what happens to a campaign's results after it has run."""

    name = "results_lifecycle"

    def register(self, mcp: FastMCP) -> None:
        """Register all tool functions with the MCP server."""
        for fn in _TOOLS:
            mcp.tool()(fn)
