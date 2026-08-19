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

"""Take in a campaign directory somebody else produced, and say whether it worked.

A downloaded campaign -- from a colleague, or from a published dataset -- has to become
something this deployment can list, display and re-run. That is not one operation, so
"succeeded" is not one bit: a campaign archive carries **two schema ladders of its own** beyond
the ``.vast``'s version (``campaign.db``'s ``user_version`` and the analysis DB's), and each can
independently be older, newer, absent or corrupt.

So ingestion reports per stage, and every stage that is not ``ok`` carries a recovery action.
The interesting property is that most of the failure modes are *recoverable*, and only saying
"ingest failed" would hide which: an absent store is rebuilt from the results tree, a corrupt one
is rebuilt on request, and an older one migrates on open. Only a store from a newer robovast
cannot be brought back, because a schema cannot be migrated downwards.

This module observes and reports; it does not re-implement the migrations. ``CampaignStore``
already upgrades on open through its own append-only ladder, and deliberately reads a *newer*
store best-effort rather than refusing -- its queries name columns explicitly, so unknown ones
are ignored. That decision is respected here and surfaced as a caveat rather than overridden.
"""

import logging
import sqlite3
from pathlib import Path

logger = logging.getLogger(__name__)

STAGE_OK = "ok"
STAGE_MIGRATED = "migrated"
#: Retained for readability in callers: no longer a *verdict*, since how the store came
#: to exist is orthogonal to whether it is healthy. See the ``rebuilt`` field.
STAGE_REBUILT = "rebuilt"
STAGE_ABSENT = "absent"
STAGE_DEGRADED = "degraded"
STAGE_NEWER = "newer"
STAGE_FAILED = "failed"

#: Stages that make the campaign unusable. ``degraded`` is not among them: a campaign that lists
#: but under-reports is a real, useful outcome, and refusing it would discard data somebody
#: already has. It must be *flagged*, not dropped -- which is why it has its own verdict.
BLOCKING_STAGES = (STAGE_FAILED,)


def _stage(verdict: str, detail: str, **extra) -> dict:
    return {"verdict": verdict, "detail": detail, **extra}


def ingest_campaign(campaign_dir, *, rebuild_store: bool = False) -> dict:
    """Register *campaign_dir* with this deployment, reporting each stage.

    Returns ``{campaign_id, ok, blocking, stages: {...}}``. ``ok`` is False only when a stage
    genuinely blocks; a degraded ingest is reported as usable-but-incomplete, because throwing
    away a campaign somebody already has in order to keep a boolean clean is the wrong trade.

    *rebuild_store* forces ``campaign.db`` to be reconstructed from the results tree. It is the
    documented recovery for a corrupt store, so it is a parameter rather than something a caller
    has to reach around this function to do.
    """
    campaign_dir = Path(campaign_dir)
    stages = {
        "layout": _check_layout(campaign_dir),
        "config": _check_config(campaign_dir),
    }
    stages["campaign_store"] = _ingest_store(campaign_dir, rebuild=rebuild_store)
    stages["analysis_db"] = _check_analysis_db(campaign_dir)
    blocking = sorted(name for name, stage in stages.items()
                      if stage["verdict"] in BLOCKING_STAGES)
    return {"campaign_id": campaign_dir.name, "ok": not blocking,
            "blocking": blocking, "stages": stages}


def _check_layout(campaign_dir: Path) -> dict:
    """Is this a campaign directory at all?

    Checked first and separately, because "not a campaign" and "a campaign with problems" need
    different answers -- registering a half-campaign would make every later reader fail on it
    instead of the import saying so once.
    """
    if not campaign_dir.is_dir():
        return _stage(STAGE_FAILED, f"{campaign_dir} is not a directory")
    missing = [name for name in ("_config", "_execution") if not (campaign_dir / name).is_dir()]
    if "_config" in missing:
        return _stage(STAGE_FAILED,
                      "no _config/ directory, so this is not a campaign this deployment can "
                      "reconstruct anything from. A raw archive of run outputs is not enough: "
                      "the frozen configuration is what makes it re-runnable.")
    if missing:
        return _stage(STAGE_DEGRADED,
                      f"missing {', '.join(missing)}. The campaign will list and display, but "
                      f"its provenance -- which robovast, which image -- is unknown, so it "
                      f"cannot be verified or re-run.")
    return _stage(STAGE_OK, "_config/ and _execution/ present")


def _check_config(campaign_dir: Path) -> dict:
    """Can the frozen ``.vast`` be brought to the current version?"""
    from robovast.common.migrations import (SUPPORTED_CONFIG_VERSION, ConfigVersionError,
                                            config_version, needs_upgrade, upgrade_config)
    from robovast.service.retrigger import _read_vast

    vast_files = sorted((campaign_dir / "_config").glob("*.vast")) \
        if (campaign_dir / "_config").is_dir() else []
    if not vast_files:
        return _stage(STAGE_FAILED, "no .vast under _config/")
    try:
        raw = _read_vast(vast_files[0])
    except Exception as e:  # pylint: disable=broad-except
        return _stage(STAGE_FAILED, f"{vast_files[0].name} could not be parsed: {e}")

    version = config_version(raw)
    if not needs_upgrade(raw):
        if version == SUPPORTED_CONFIG_VERSION:
            return _stage(STAGE_OK, f"config version {version}", version=version)
        return _stage(STAGE_NEWER,
                      f"config version {version} is newer than this robovast supports "
                      f"({SUPPORTED_CONFIG_VERSION}). It will display best-effort, but a "
                      f"re-run needs a newer robovast -- a format cannot be migrated "
                      f"backwards.", version=version)
    try:
        _, applied = upgrade_config(raw)
    except ConfigVersionError as e:
        return _stage(STAGE_FAILED, str(e), version=version)
    return _stage(STAGE_MIGRATED,
                  f"config version {version} migrates to {SUPPORTED_CONFIG_VERSION} when read; "
                  f"the archived file is not modified",
                  version=version, steps=applied)


def _ingest_store(campaign_dir: Path, *, rebuild: bool) -> dict:
    """Make ``campaign.db`` usable, reporting which of the four cases applied.

    Absent is the *normal* case for a raw archive, not an error: ``build_campaign_store``
    exists precisely to reconstruct a store by scanning a finished results tree.
    """
    from robovast.common.campaign_index import build_campaign_store
    from robovast.common.store import SCHEMA_VERSION, STORE_FILENAME

    store_path = campaign_dir / STORE_FILENAME
    existed = store_path.exists()

    if existed and not rebuild:
        try:
            with sqlite3.connect(f"file:{store_path}?mode=ro", uri=True) as conn:
                found = conn.execute("PRAGMA user_version").fetchone()[0]
                conn.execute("SELECT count(*) FROM campaign").fetchone()
        except sqlite3.DatabaseError as e:
            return _stage(STAGE_FAILED,
                          f"{STORE_FILENAME} is present but unreadable ({e}). It can be "
                          f"reconstructed from the results tree -- re-run with --rebuild-store.",
                          recovery="--rebuild-store")
        if found > SCHEMA_VERSION:
            return _stage(STAGE_NEWER,
                          f"{STORE_FILENAME} is schema v{found}, newer than this robovast "
                          f"supports (v{SCHEMA_VERSION}). A schema cannot be migrated "
                          f"downwards, so upgrade robovast; queries will otherwise read it "
                          f"best-effort and silently omit whatever the newer schema added.",
                          schema_version=found)

    try:
        built = build_campaign_store(campaign_dir, force=rebuild)
    except Exception as e:  # pylint: disable=broad-except
        return _stage(STAGE_FAILED,
                      f"could not register the campaign: {e}. Without a store it will not "
                      f"appear in listings or the web UI, which answer from it rather than "
                      f"from the results tree.")

    try:
        with sqlite3.connect(f"file:{built}?mode=ro", uri=True) as conn:
            now = conn.execute("PRAGMA user_version").fetchone()[0]
            runs = conn.execute("SELECT count(*) FROM run").fetchone()[0]
    except sqlite3.DatabaseError as e:
        return _stage(STAGE_FAILED, f"the store was written but is unreadable: {e}")

    # `rebuilt` is a provenance fact, not a health verdict, and the two are independent: a store
    # can be reconstructed *and* thin, or recorded live *and* thin. Collapsing them into one
    # verdict lost whichever came second -- so the health verdict is the verdict, and how the
    # store came to exist rides alongside it. A reconstructed store is derived from the results
    # tree rather than written live by the controller, which is the difference between a recorded
    # fact and a recovered one, and a reader comparing two campaigns should be able to see it.
    rebuilt = bool(rebuild or not existed)
    if runs == 0:
        return _stage(STAGE_DEGRADED,
                      f"registered at schema v{now}, but it indexes no runs. The campaign will "
                      f"list and report nothing, which usually means the results tree was "
                      f"archived without its run directories.",
                      schema_version=now, runs=runs, rebuilt=rebuilt)
    return _stage(STAGE_OK, f"registered at schema v{now}, indexing {runs} run(s)"
                            + (" (reconstructed from the results tree)" if rebuilt else ""),
                  schema_version=now, runs=runs, rebuilt=rebuilt)


def _check_analysis_db(campaign_dir: Path) -> dict:
    """Whether the postprocessed analysis database is present.

    Absent is expected for a *raw* (pre-postprocess) archive and is recoverable by running
    postprocessing, so it is reported with that action rather than as a failure.
    """
    from robovast.common.analysis.db import DATA_DB_SCHEMA_VERSION

    candidates = sorted(campaign_dir.glob("**/*.duckdb")) + sorted(campaign_dir.glob("**/data.db"))
    if not candidates:
        return _stage(STAGE_ABSENT,
                      "no analysis database. Expected for a raw, pre-postprocess archive; "
                      "regenerate it with 'vast results postprocess'.")
    return _stage(STAGE_OK, f"{len(candidates)} analysis database(s) present "
                            f"(current schema v{DATA_DB_SCHEMA_VERSION})")
