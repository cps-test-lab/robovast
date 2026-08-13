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
(delete, download). Separate from :mod:`execution` because these act on a campaign that
has already finished, and separate from :mod:`results` because they *change* or move the
results rather than read them.

Removal is one verb with a scope flag rather than two tools. A caller facing
``delete_campaign`` beside ``cleanup_campaign_data`` has to know that one erases the
campaign and the other only frees its buckets — a distinction the names do not carry,
between two irreversible operations. ``data_only`` states the scope at the call site.
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
    minutes to hours). The campaign enters the ``postprocessing`` phase; background
    ``vast exec wait <campaign_id>`` until it is over, then read the outcome
    (``postprocessed`` / ``postprocessing_error``). Reprocesses just this campaign (not its siblings), reading
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
    campaign enters the ``sharing`` phase, so background ``vast exec wait <campaign_id>``
    until it is over, then read the outcome (``share_error`` on failure). Works from disk with no
    live campaign (usable
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


def delete_campaign(campaign_id: str = "", data_only: bool = False,
                    force: bool = False) -> dict:
    """Irreversibly remove a campaign, or just free the storage its results occupy.

    Runs through the robovast-service, which holds the object-store credentials and the
    authoritative live-campaign set — no kubeconfig, S3 keys or namespaces here. A
    running campaign is refused; stop it first. The external share copy is never touched.

    Args:
        campaign_id: The campaign to remove. Required unless ``data_only`` — with
            ``data_only`` an empty id sweeps **all** finished campaigns.
        data_only: Free the object-store bucket(s) only, keeping the campaign itself.
            Use once results are downloaded or published. Cluster campaigns only; a
            local service has no object store.
        force: Act on a named campaign the service still considers live.

    Returns:
        ``{ok, message}`` — for ``data_only``, how many buckets were removed — or
        ``{error}``.
    """
    client = service_access.service_client()
    if client is None:
        return {"error": f"{NO_SERVICE}. The campaign lives with the service, not "
                          "on this host."}
    # An empty id means "every finished campaign" only for the bucket sweep, which is
    # what cleanup has always meant. Wholesale deletion has no such form, and letting an
    # empty id fall through to it would erase the entire corpus from a missing argument.
    if not campaign_id and not data_only:
        return {"error": "campaign_id is required to delete a campaign. (An empty id "
                         "is only meaningful with data_only=True, which sweeps every "
                         "finished campaign's object-store data.)"}
    try:
        if data_only:
            from robovast.service.interface import CleanupDataRequest
            res = client.cleanup_campaign_data(
                CleanupDataRequest(campaign_id=campaign_id or None, force=force))
        else:
            res = client.delete_campaign(campaign_id)
        return {"ok": res.ok, "message": res.message}
    except Exception as e:  # noqa: BLE001
        return {"error": str(e)}


def _campaign_lane(campaign_id: str, client) -> str:
    """``"cluster"`` or ``"local"`` — the lane **this campaign** ran on.

    Read from the campaign's own record rather than from the service's default backend.
    A ``vast serve --backend local+cluster`` reports ``version().backend == "docker"``
    while offering both lanes, so asking the service told every cluster campaign on a
    dev host that its results were on the local filesystem — a capability denied, and a
    place to look that holds nothing.

    Falls back to the service's backend for a campaign with no record yet (it has not
    reached execution), which is the only case where there is nothing better to ask.
    """
    from robovast.mcp_server import data_access
    try:
        rows = data_access.rows(
            campaign_id, "SELECT execution_type FROM campaign.campaign LIMIT 1",
            max_rows=1)
    except Exception:  # noqa: BLE001 - a provenance read must not fail the answer
        logger.debug("could not read the lane of %s from its record.", campaign_id,
                     exc_info=True)
        rows = []
    recorded = (rows[0].get("execution_type") if rows else None)
    if recorded:
        return "cluster" if recorded == "cluster" else "local"
    return "cluster" if client.version().backend == "kubernetes" else "local"


def get_campaign_download(campaign_id: str) -> dict:
    """Where to download a campaign — a link for a human to open, not a file fetched here.

    Never writes to the MCP-server host, which may not be a machine you can reach.

    Args:
        campaign_id: The campaign to download.

    Returns:
        Cluster service: ``{campaign_id, url, path, note}`` — the postprocessed
        ``tar.gz`` streamed from the object store. Local service: ``{campaign_id, note}``
        — the results are already on the service host's filesystem, so there is no HTTP
        download. Or ``{error}``.
    """
    client = service_access.service_client()
    if client is None:
        return {"error": f"{NO_SERVICE}. The campaign lives with the service, not "
                          "on this host."}
    try:
        lane = _campaign_lane(campaign_id, client)
    except Exception as e:  # noqa: BLE001
        return {"error": f"could not reach the service: {e}"}

    path = f"/campaigns/{campaign_id}/archive"
    if lane == "cluster":
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
        "note": ("This campaign ran on the local lane — its results are already on the "
                 "service host's filesystem; there is no HTTP download."),
    }


# -- Plugin class ------------------------------------------------------------

_TOOLS = [
    get_postprocessing,
    update_postprocessing,
    run_postprocessing,
    run_share,
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
