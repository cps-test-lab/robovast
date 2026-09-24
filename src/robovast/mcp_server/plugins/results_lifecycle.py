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
an archive is routinely gigabytes, so both deal in paths and links. An export is the third
way out: the campaign's tables as files, built on request, for an analysis away from the
service.

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
    """
    try:
        return service_access.require_service() \
            .get_postprocessing(campaign_id).model_dump()
    except Exception as e:  # noqa: BLE001
        return {"error": str(e)}


def update_postprocessing(campaign_id: str, entries: list) -> dict:
    """Replace a campaign's analysis-postprocessing entries (a new versioned override).

    ``entries`` is a list of postprocessing commands — a bare plugin name
    (``"rosbags_to_csv"``) or a single-key dict with params
    (``{"command": {"script": "postprocess.sh"}}``). Validated before writing;
    the ``_config/`` snapshot is untouched. Call :func:`run_postprocessing` to apply.
    """
    from robovast.service.interface import UpdatePostprocessingRequest
    try:
        return service_access.require_service() \
            .update_postprocessing(UpdatePostprocessingRequest(
                campaign_id=campaign_id, entries=entries)).model_dump()
    except Exception as e:  # noqa: BLE001
        return {"error": str(e)}


def run_postprocessing(campaign_id: str, force: bool = False,
                       skip: list | None = None, replay: bool = False) -> dict:
    """(Re)run analysis postprocessing for one campaign: its steps, declared tables, grades.

    **Dispatched in the background** — returns as soon as the run is started. The campaign
    enters the ``postprocessing`` phase; background ``vast campaign wait <campaign_id>``
    until it is over, then read the outcome (``postprocessed`` / ``postprocessing_error``).
    Reads the campaign's own ``_config/<name>.vast``. Returns ``{ok, message}``, or
    ``ok=false`` if an operation is already running for the campaign.

    Args:
        campaign_id: The campaign to (re)process.
        force: Clear the campaign's built tables first, so what it declares is built again.
        skip: Plugin names to skip.
        replay: Clear the campaign's built tables and build every table its records can
            give, for every run, before the campaign-end pass -- the rows a live watcher
            wrote as the runs went, built again from the records.
    """
    from robovast.service.interface import RunPostprocessingRequest
    try:
        return service_access.require_service() \
            .run_postprocessing(RunPostprocessingRequest(
                campaign_id=campaign_id, force=force, replay=replay,
                skip=skip or [])).model_dump()
    except Exception as e:  # noqa: BLE001
        return {"error": str(e)}


def build_campaign_tables(campaign_id: str, tables: list | None = None) -> dict:
    """Build a finished campaign's tables for every run now, in the background.

    **Not needed for any answer**: every table is built the first time a query names it.
    Use it only before a long analysis of a whole large campaign. Progress is in the
    campaign log's TABLES section; ``describe_campaign_data`` reports each table as built
    for M of M runs when done.

    Args:
        campaign_id: The finished campaign.
        tables: Table names to build; every table its records can give when omitted.
    """
    from robovast.service.interface import BuildCampaignTablesRequest
    try:
        return service_access.require_service().build_campaign_tables(
            BuildCampaignTablesRequest(campaign_id=campaign_id,
                                       tables=list(tables or []))).model_dump()
    except Exception as e:  # noqa: BLE001
        return {"error": str(e)}


def clear_campaign_tables(campaign_id: str) -> dict:
    """Remove one campaign's built tables to free storage; each is built again on use.

    Loses nothing but the time to build again. Refused while the campaign runs or its
    tables are being built. Returns ``{campaign_id, freed_bytes}``.
    """
    try:
        return service_access.require_service().clear_campaign_tables(campaign_id).model_dump()
    except Exception as e:  # noqa: BLE001
        return {"error": str(e)}


def run_share(campaign_id: str) -> dict:
    """(Re)trigger the upload-to-share of one finished campaign.

    **Dispatched in the background**: the campaign enters the ``sharing`` phase, so
    background ``vast campaign wait <campaign_id>`` until it is over, then read
    ``share_error`` on failure. Works from disk after a restart. ``ok=false`` means another
    operation is already running for it. **The variant is read off the campaign, not
    chosen**: ``<id>.raw.tar.gz`` before postprocessing, ``<id>.postprocessed.tar.gz``
    after. The provider comes from the service environment (``ROBOVAST_SHARE_TYPE`` +
    credentials); fails loudly when none is configured.

    Args:
        campaign_id: The finished campaign to (re)upload.
    """
    from robovast.service.interface import RunShareRequest
    try:
        return service_access.require_service() \
            .run_share(RunShareRequest(campaign_id=campaign_id)).model_dump()
    except Exception as e:  # noqa: BLE001
        return {"error": str(e)}


def delete_campaign(campaign_id: str | list[str]) -> dict:
    """Irreversibly remove campaigns: each one's directory (its built tables with it), the
    local archives beside it, and on a cluster its leftover Jobs.

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
    so an unpacked archive lists blank. A **raw** archive (no postprocessing record — what
    the share holds) is postprocessed once it lands; an archive carries no tables either
    way, and every table is built from the records the first time something names it.
    Returns immediately; the campaign is already listed at phase ``importing``.

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
        ref = service_access.require_service().import_campaign(ImportCampaignRequest(
            archive_path=archive_path, share_archive=share_archive,
            force=force, rebuild_store=rebuild_store))
        return {"campaign_id": ref.campaign_id, "note": ref.note}
    except Exception as e:  # noqa: BLE001
        return {"error": str(e)}


def get_campaign_download(campaign_id: str) -> dict:
    """Where to download a campaign as the service holds it -- a link, never a file fetched here.

    A campaign **still running** downloads too, as a snapshot named
    ``<campaign-id>.incomplete.tar.gz`` that an import reports as degraded: runs that had
    not finished are absent. For the tables as files, ``export_campaign``.

    Args:
        campaign_id: The campaign to download.

    Returns:
        ``{campaign_id, path, next_step}`` plus ``url`` when this service declares an
        origin. Or ``{error}``.
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


def export_campaign(campaign_id: str, tables: list | None = None, format: str = "parquet",  # pylint: disable=redefined-builtin
                    bags: str = "none", records: bool = True) -> dict:
    """Export a finished campaign as one tar.gz, for a laptop analysis or a hand-off.

    Its tables as one file each (parquet is the tables as they are, for pandas or DuckDB;
    csv the same rows as text), its records (what ``robovast-data`` opens) and, if asked,
    its recordings. Built in the background: poll ``get_export_status``, then fetch
    ``url`` or run ``next_step`` on your own machine.

    Args:
        campaign_id: The finished campaign.
        tables: Table names to write (``describe_campaign_data`` lists them); every table
            the records can give when omitted. ``runs`` is always written.
        format: ``parquet`` or ``csv``.
        bags: ``none``; ``mcap`` copies the recordings as they are; ``sqlite3`` rewrites
            each rosbag2 bag in sqlite3 storage, for a ROS 2 without the mcap plugin.
        records: Whether the campaign's records ship too.

    Returns:
        ``{campaign_id, export_id, status, path, next_step}`` plus ``url`` when this
        service declares an origin; or ``{error}``.
    """
    from robovast.service.interface import ExportRequest
    client = service_access.service_client()
    if client is None:
        return {"error": f"{NO_SERVICE}. The campaign lives with the service, not "
                          "on this host."}
    try:
        request = ExportRequest(tables=list(tables) if tables is not None else None,
                                format=format, bags=bags, records=records)
        ref = client.create_export(campaign_id, request)
        status = client.get_export_status(campaign_id, ref.export_id)
    except Exception as e:  # noqa: BLE001
        return {"error": str(e)}
    url = service_access.web_url(client, ref.url)
    options = [f"--format {format}", f"--bags {bags}"]
    if tables is not None:
        options.insert(0, f"--tables {','.join(tables)}")
    if not records:
        options.append("--no-records")
    return {
        "campaign_id": campaign_id,
        "export_id": ref.export_id,
        "status": status.model_dump(),
        **({"url": url} if url else {}),
        "path": ref.url,
        "next_step": f"vast campaign export {campaign_id} {' '.join(options)}",
    }


def get_export_status(campaign_id: str, export_id: str) -> dict:
    """Where an export started by :func:`export_campaign` has got to.

    ``done`` with an empty ``error`` means its file is ready at the ``url`` that call gave;
    ``tables`` fills with row counts as they are written; ``bytes``, ``started_at`` and
    ``finished_at`` say the rest.
    """
    try:
        return service_access.require_service() \
            .get_export_status(campaign_id, export_id).model_dump()
    except Exception as e:  # noqa: BLE001
        return {"error": str(e)}


# -- Plugin class ------------------------------------------------------------

_TOOLS = [
    get_postprocessing,
    update_postprocessing,
    run_postprocessing,
    build_campaign_tables,
    clear_campaign_tables,
    run_share,
    delete_campaign,
    get_campaign_download,
    export_campaign,
    get_export_status,
    import_campaign,
]


class ResultsLifecyclePlugin:
    """MCP plugin: what happens to a campaign's results after it has run."""

    name = "results_lifecycle"

    def register(self, mcp: FastMCP) -> None:
        """Register all tool functions with the MCP server."""
        for fn in _TOOLS:
            mcp.tool()(fn)
