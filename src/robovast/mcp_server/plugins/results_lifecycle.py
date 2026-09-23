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

Re-deriving them (postprocessing), publishing them (share), disposing of them (delete),
and moving them between deployments (download, import). Separate from :mod:`execution`
because these act on a campaign that has already finished, and separate from
:mod:`results` because they *change* or move the results rather than read them.

Download and import are the two directions of the same move, so they sit together: one
answers where to fetch a campaign from, the other takes one in. Neither carries bytes --
an archive is routinely gigabytes, so both deal in paths and links.

Removal is one verb: a campaign has one home, and deleting it is deleting that.
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
    """(Re)run analysis postprocessing for one campaign, rebuilding its derived data.

    **Dispatched in the background** — returns as soon as the run is started (it can take
    minutes to hours). The campaign enters the ``postprocessing`` phase; background
    ``vast campaign wait <campaign_id>`` until it is over, then read the outcome
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
    """(Re)trigger the upload-to-share of one finished campaign.

    **Dispatched in the background** — returns as soon as the upload is started; the
    campaign enters the ``sharing`` phase, so background ``vast campaign wait <campaign_id>``
    until it is over, then read the outcome (``share_error`` on failure). Works from disk
    with no live campaign (usable after a `vast serve` restart). ``ok=false`` means an
    operation is already running for it — an upload, postprocessing and the campaign
    itself cannot overlap. **The variant is read off the campaign, not chosen**:
    ``<id>.raw.tar.gz`` before postprocessing (metrics still to compute),
    ``<id>.postprocessed.tar.gz`` after. The target provider comes from the service
    environment (``ROBOVAST_SHARE_TYPE`` + credentials): adjust it and re-trigger to
    upload to a different provider. Fails loudly if no share provider is configured.

    Args:
        campaign_id: The finished campaign to (re)upload.
    """
    from robovast.service.interface import RunShareRequest
    try:
        return service_access.client_or_local() \
            .run_share(RunShareRequest(campaign_id=campaign_id)).model_dump()
    except Exception as e:  # noqa: BLE001
        return {"error": str(e)}


def delete_campaign(campaign_id: str | list[str]) -> dict:
    """Irreversibly remove campaigns: each one's directory, the local archives beside it,
    its index rows, and on a cluster its leftover Jobs.

    Each id is deleted or refused on its own: a running one is refused (stop it first);
    ``partial`` says what it had to leave behind, and deleting again retries it.
    The copy on an external share is never touched.

    Args:
        campaign_id: One campaign id, or a list of them.

    Returns:
        ``{results: [{campaign_id, outcome, ok, message}]}`` or ``{error}``.
    """
    from robovast.service.interface import DeleteCampaignsRequest
    client = service_access.service_client()
    if client is None:
        return {"error": f"{NO_SERVICE}. The campaign lives with the service, not "
                          "on this host."}
    ids = [campaign_id] if isinstance(campaign_id, str) else list(campaign_id)
    if not ids or not all(ids):
        return {"error": "campaign_id is required to delete a campaign."}
    try:
        res = client.delete_campaigns(DeleteCampaignsRequest(campaign_ids=ids))
        return res.model_dump()
    except Exception as e:  # noqa: BLE001
        return {"error": str(e)}


def import_campaign(archive_path: str = "", share_archive: str = "",
                    force: bool = False, rebuild_store: bool = False) -> dict:
    """Take a campaign in — from the service host or the share — and register it.

    Registration, not just extraction: listings and every query answer from ``campaign.db``,
    so an unpacked archive lists blank. A **raw** archive (no metric tables — what the share
    holds) is postprocessed once it lands. Returns immediately; the campaign is already
    listed at phase ``importing``.

    Give exactly one source. Neither carries bytes through this tool — an archive is
    routinely gigabytes. For one on *your own* machine use ``vast campaign import`` or the
    web UI, which upload over a side channel and then call this.

    Args:
        archive_path: A ``.tar.gz`` on the **service host**, not on this machine. Left in
            place; importing it does not consume it.
        share_archive: A campaign id or archive name on the configured share. The service
            fetches it itself.
        force: Replace a campaign of the same id. Destructive.
        rebuild_store: Rebuild ``campaign.db`` from the results tree — the recovery when
            the ``campaign_store`` stage reports a corrupt one.

    Returns:
        ``{campaign_id, note}``; watch it with ``vast campaign wait <campaign_id>``. Or ``{error}``.
        Per-stage verdicts land in the campaign's ``_execution/import.json`` — a *degraded*
        import is usable-but-incomplete, **not** a failure, so read it before discarding a
        campaign you just recovered.
    """
    from robovast.service.interface import ImportCampaignRequest
    try:
        ref = service_access.client_or_local().import_campaign(ImportCampaignRequest(
            archive_path=archive_path, share_archive=share_archive,
            force=force, rebuild_store=rebuild_store))
        return {"campaign_id": ref.campaign_id, "note": ref.note}
    except Exception as e:  # noqa: BLE001
        return {"error": str(e)}


def get_campaign_download(campaign_id: str) -> dict:
    """Where to download a campaign — a link for a human to open, not a file fetched here.

    Never writes to the MCP-server host, which may not be a machine you can reach.

    A campaign that is **still running** downloads too, as a snapshot: the service serves
    it under ``<campaign-id>.incomplete.tar.gz`` and the archive carries a marker an import
    reports as degraded. Runs that had not finished are absent from it, so it answers "what
    does this campaign have so far", never "what did this campaign produce".

    Args:
        campaign_id: The campaign to download.

    Returns:
        ``{campaign_id, path, next_step}`` — the campaign as a ``tar.gz`` — plus ``url``
        when this service declares an origin to reach it on. Or ``{error}``.
    """
    from robovast.service.interface import Routes
    client = service_access.service_client()
    if client is None:
        return {"error": f"{NO_SERVICE}. The campaign lives with the service, not "
                          "on this host."}
    # The route helper, not a second copy of the path: it exists so this link and the
    # endpoint serving it are one string.
    path = Routes.campaign_archive(campaign_id)
    # Omitted rather than empty when there is no origin to build one from -- a deployment
    # that declares none still has a usable answer in `path` + `next_step`.
    url = service_access.web_url(client, path)
    # The command belongs here rather than in prose: it is the whole next move, with the
    # id already filled in. Nothing is said about the share copy -- whether one exists is
    # not a fact this service records (only `share_error`, a failure, travels with a
    # campaign), and `vast share download` is documented where commands are looked up.
    #
    # `campaign download`, not the identical `results download`: this runs on the
    # *caller's* machine, and `vast results` ships only with the full distribution, so an
    # agent driving a remote service over this MCP -- the case the tool exists for -- may
    # not have it. The campaign group is the client's, so this one always resolves.
    return {
        "campaign_id": campaign_id,
        **({"url": url} if url else {}),
        "path": path,
        "next_step": f"vast campaign download {campaign_id}",
    }


# -- Plugin class ------------------------------------------------------------

_TOOLS = [
    get_postprocessing,
    update_postprocessing,
    run_postprocessing,
    run_share,
    delete_campaign,
    get_campaign_download,
    import_campaign,
]


class ResultsLifecyclePlugin:
    """MCP plugin: what happens to a campaign's results after it has run."""

    name = "results_lifecycle"

    def register(self, mcp: FastMCP) -> None:
        """Register all tool functions with the MCP server."""
        for fn in _TOOLS:
            mcp.tool()(fn)
