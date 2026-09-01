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

"""Take in a campaign somebody else produced -- an archive or a directory -- and say whether
it worked.

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

The same question is asked on the way *out*, by :func:`missing_for_import`: an export that
writes an archive no deployment could ingest has produced a failure that can only surface at
the far end of a transfer, on somebody else's service. Both sides ask one predicate so they
cannot drift apart.

The steps above :func:`ingest_campaign` are here too, and separate rather than one
``import_archive``: :func:`read_campaign_id`, :func:`claim_campaign_dir`, :func:`extract_archive`.
The importer interleaves other work between them -- it opens the campaign's ``import.log`` once
the directory is claimed, and downloads from the share before extracting -- which a single
do-everything call cannot allow. They live here rather than in a CLI because extraction is where
an archive is decided to be untrusted input, and every client reaches them through one service op
(:meth:`~robovast.service.interface.RobovastInterface.import_campaign`) rather than
re-implementing the sequence.
"""

import logging
import shutil
import sqlite3
import tarfile
from pathlib import Path

logger = logging.getLogger(__name__)

STAGE_OK = "ok"
STAGE_MIGRATED = "migrated"
#: For readability in callers, not a *verdict*: how the store came to exist is orthogonal
#: to whether it is healthy. See the ``rebuilt`` field.
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


def _top_level_entries(names) -> set:
    """The distinct first path segments in *names*, ignoring a ``./`` root.

    ``tar czf x.tar.gz -C <results> .`` writes every member under ``./``, so reading the
    first segment literally reports one top-level entry called ``.`` -- which then resolves
    to the results root itself. A one-entry archive is exactly the shape this function is
    asked to recognize, so that mistake looks like success right up to the point where the
    "campaign" being replaced is every campaign there is.
    """
    tops = set()
    for name in names:
        if not name or name.startswith('/'):
            continue
        parts = [part for part in name.split('/') if part not in ('', '.')]
        if parts:
            tops.add(parts[0])
    return tops


def _checked_campaign_name(name: str) -> str:
    """Refuse a top-level name that is not a campaign directory's.

    Two different dangers, both silent:

    * A traversal name (``.``, ``..``, anything with a separator left in it) resolves
      outside the campaign it claims to be, and ``force`` deletes whatever it resolved to.
    * A name that is merely *not campaign-shaped* imports and registers fine and then never
      appears: the local listing keeps only directories matching ``is_campaign_dir``, and
      deletion checks the same thing. So it would be an import that reports every stage ok
      and produces a campaign nobody can see or remove -- which is worse than a refusal.
    """
    from robovast.common.execution import is_campaign_dir

    if name in ('.', '..') or '/' in name or '\\' in name:
        raise ValueError(
            f"the archive's top-level entry {name!r} is not a campaign directory name")
    if not is_campaign_dir(name):
        raise ValueError(
            f"{name!r} is not a campaign directory name (expected "
            f"'<name>-YYYY-MM-DD-HHMMSS'). Campaigns are listed and deleted by that shape, "
            f"so importing this would register something no listing would ever show. If this "
            f"really is a campaign, rename its directory inside the archive.")
    return name


def read_campaign_id(archive_path) -> str:
    """The campaign id an archive holds, read from its member list alone.

    Known before anything is extracted, which is what lets an import be a *tracked*
    operation: the campaign is registered under this id and shows in the campaign view at
    phase ``importing`` while the bytes are still moving. Reading it costs the tar's index,
    not its contents.

    ``ValueError`` -- the interface's vocabulary for "this input is wrong", mapped to 400 by
    the HTTP layer -- when the archive is not exactly one campaign.
    """
    archive_path = Path(archive_path)
    try:
        with tarfile.open(archive_path, 'r:*') as tar:
            tops = _top_level_entries(tar.getnames())
    except (tarfile.TarError, OSError) as e:
        raise ValueError(f"could not read {archive_path.name}: {e}") from e
    if len(tops) != 1:
        raise ValueError(
            f"archive holds {len(tops)} top-level entries; expected one campaign "
            f"directory: {sorted(tops)[:5]}")
    return _checked_campaign_name(tops.pop())


def claim_campaign_dir(results_root, campaign_id: str, *, force: bool = False) -> Path:
    """Make ``<results_root>/<campaign_id>`` ready to be extracted into; return it.

    Settles the conflict with whatever is already there **before** any bytes are fetched, so
    an import that was never going to be allowed does not first spend an hour downloading.
    ``RuntimeError`` (409 at the HTTP layer) when a campaign of this id is here and *force*
    was not asked for.

    Creates the campaign's ``_execution/`` directory, because the importer's log lives there
    and it must be open before the slow part starts -- an import whose account of itself only
    begins after the download is an import with no account of the download.
    """
    root = Path(results_root)
    root.mkdir(parents=True, exist_ok=True)
    target = root / _checked_campaign_name(campaign_id)
    if target.exists():
        if not force:
            raise RuntimeError(
                f"{campaign_id} is already here. Refusing to overwrite a campaign "
                f"that is already present -- its records are evidence. Import it again "
                f"with force to replace it.")
        shutil.rmtree(target)
    (target / "_execution").mkdir(parents=True, exist_ok=True)
    return target


def extract_archive(archive_path, results_root, *, remove_archive: bool = False) -> None:
    """Unpack a campaign archive into *results_root*.

    *remove_archive* deletes *archive_path* afterwards. It is for a copy the service staged
    -- from an upload, or from the share -- and now owns; a path the caller named is never
    deleted, because deleting somebody's own file as a side effect of importing it is not
    something a caller can undo.
    """
    archive_path = Path(archive_path)
    try:
        with tarfile.open(archive_path, 'r:*') as tar:
            # `filter='data'` refuses absolute paths and ../ escapes. An archive from elsewhere is
            # untrusted input, and the default became an error in newer Pythons for that reason.
            # A campaign's `job` symlinks point within the campaign, so they survive it.
            tar.extractall(path=Path(results_root), filter='data')
    except (tarfile.TarError, OSError) as e:
        raise ValueError(f"could not read {archive_path.name}: {e}") from e

    if remove_archive:
        try:
            archive_path.unlink()
        except OSError as e:
            # The campaign is out of the archive; a leftover staging file is a housekeeping
            # problem, not a reason to report the import as failed.
            logger.warning("Could not remove the staged archive %s: %s", archive_path, e)


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
    stages["index"] = _ingest_index(campaign_dir)
    stages["analysis_db"] = _check_analysis_db(campaign_dir)
    blocking = sorted(name for name, stage in stages.items()
                      if stage["verdict"] in BLOCKING_STAGES)
    return {"campaign_id": campaign_dir.name, "ok": not blocking,
            "blocking": blocking, "stages": stages}


def blocking_summary(report: dict) -> str:
    """Why *report* refuses the campaign, in the words each stage already wrote.

    The stage *names* are an index, not a diagnosis: ``config, layout`` is what every
    incomplete archive says, and it says the same whether the ``.vast`` is missing,
    unparseable, or from a robovast that does not exist yet. Each stage already
    composed the sentence that distinguishes them -- and until this existed that sentence
    reached only ``import.log`` and ``import.json``, both of which live *inside* the
    campaign and are therefore unreadable on a lane that publishes a campaign only once
    the import succeeds. So the one place the reason was guaranteed to be visible carried
    the one form of it that says nothing.
    """
    return " ".join(f"{name}: {report['stages'][name]['detail']}"
                    for name in report["blocking"])


def missing_for_import(rel_paths) -> list:
    """What these **campaign-relative** paths lack that an import would refuse them for.

    Paths rather than a directory, because the two callers hold different things: a local
    export walks a tree, a cluster export lists object keys and has no tree to stat. Each
    strips its own prefix -- only the caller knows whether it has archive members, object
    keys or a directory walk -- and what arrives here is what a reader would find *inside*
    the campaign.

    This is the export side of the question :func:`ingest_campaign` asks on the way in, and
    it exists so the two cannot disagree about what a campaign is. An archive written
    without this check is one no deployment can ever take in: it uploads, lists and
    downloads fine and then fails at the far end, where nobody can do anything about it.
    Refusing to write it is the cheaper failure by a whole transfer.

    Deliberately only what *blocks*. A campaign with no derived data is raw, not broken, and
    raw is the normal thing to share.
    """
    rels = {str(path).replace('\\', '/').lstrip('/') for path in rel_paths}
    if not any(rel == "_config" or rel.startswith("_config/") for rel in rels):
        return ["_config/, so this is not a campaign anything can be reconstructed from. "
                "A raw archive of run outputs is not enough: the frozen configuration is "
                "what makes it re-runnable."]
    if not any(rel.startswith("_config/") and rel.endswith(".vast") for rel in rels):
        return ["_config/<name>.vast, the frozen campaign configuration. Without it an "
                "import can list the archive's files and nothing else."]
    return []


def missing_for_import_in(campaign_root) -> list:
    """:func:`missing_for_import` for a campaign on disk, without walking it.

    Only ``_config/`` decides the answer, so only ``_config/`` is read. An export of a
    campaign with tens of thousands of run artifacts must not pay a full tree walk to
    learn whether one directory is there.
    """
    config = Path(campaign_root) / "_config"
    if not config.is_dir():
        return missing_for_import([])
    return missing_for_import(["_config"] + [f"_config/{p.name}" for p in config.iterdir()])


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
    migrated_from = None

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
        if found < SCHEMA_VERSION:
            # An archived store has to be walked up the ladder explicitly, and this is the
            # only place that can. The checks around it are read-only, and the ladder runs
            # on a read-*write* open; and ``build_campaign_store`` below will not rebuild
            # it either, because its freshness shortcut compares mtimes and tar preserves
            # them -- so a store archived alongside its own tree always looks up to date.
            # The result was an import that failed on ``no such table: run`` for every
            # campaign old enough to predate that table, which is exactly the population
            # that most needs importing.
            #
            # Migrated in place rather than rebuilt: the ladder keeps the rows the
            # controller recorded live, and ``backfill_run_rows`` then fills the run table
            # a v1 store never had from the results tree beside it.
            try:
                _migrate_store_in_place(campaign_dir, store_path)
            except Exception as e:  # pylint: disable=broad-except
                return _stage(STAGE_FAILED,
                              f"{STORE_FILENAME} is schema v{found} and could not be "
                              f"migrated ({e}). It can be reconstructed from the results "
                              f"tree instead -- re-run with --rebuild-store.",
                              schema_version=found, recovery="--rebuild-store")
            migrated_from = found

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
    # Migration is provenance too, for the same reason `rebuilt` is: it says how the store
    # came to be usable, not whether it is healthy. A v1 store that migrates cleanly and
    # still indexes nothing is degraded, and saying only "migrated" would hide that.
    origin = ""
    if migrated_from is not None:
        origin = (f" (migrated from schema v{migrated_from} in place; the rows the "
                  f"controller recorded live are kept)")
    elif rebuilt:
        origin = " (reconstructed from the results tree)"

    if runs == 0:
        return _stage(STAGE_DEGRADED,
                      f"registered at schema v{now}, but it indexes no runs. The campaign will "
                      f"list and report nothing, which usually means the results tree was "
                      f"archived without its run directories." + origin,
                      schema_version=now, runs=runs, rebuilt=rebuilt, version=migrated_from)
    return _stage(STAGE_OK, f"registered at schema v{now}, indexing {runs} run(s)" + origin,
                  schema_version=now, runs=runs, rebuilt=rebuilt, version=migrated_from)


def _migrate_store_in_place(campaign_dir: Path, store_path: Path) -> None:
    """Walk an archived ``campaign.db`` up the schema ladder, then fill in its runs.

    Opening a :class:`CampaignStore` read-write is what runs the ladder -- there is no
    separate migrate entry point, and deliberately so: every reader gets the upgrade by
    opening. A v1 store arrives at v2+ with an empty ``run`` table, which is precisely the
    case :func:`~robovast.common.campaign_index.backfill_run_rows` was written for.
    """
    from robovast.common.campaign_index import backfill_run_rows
    from robovast.common.store import CampaignStore

    with CampaignStore(store_path):
        pass
    backfill_run_rows(campaign_dir)


def _ingest_index(campaign_dir: Path) -> dict:
    """Load the campaign's rows into the central index. A stage, not a separate command.

    Ingestion is not something a user asks for -- it happens wherever a campaign's results
    ARRIVE, and this function is the arrival for two of the three ways they do: an import
    from a share, and a user upload. (The third is a campaign finishing, which
    ``results_processing.postprocessing`` covers.) Anything else leaves a campaign that
    lists, opens and has files, and answers nothing when queried.

    Before this existed, the stage below merely *reported* that the rows were absent and
    told the reader to run postprocessing -- which re-runs the plugin pipeline against the
    campaign's own image to regenerate derived files that the archive already contains.
    The ingest reads those files, so an imported campaign is queryable without resolving a
    single plugin.

    Not blocking when the index is unreachable, deliberately, and consistent with the rest
    of this module: the campaign itself imported fine and its files are intact, so
    discarding it to keep a boolean clean is the wrong trade. Degraded says the queryable
    copy is missing and names the remedy, and re-importing (or postprocessing) supplies it
    later. The one thing this must not do is stay silent -- a campaign that lists but
    cannot be queried, with nothing saying why, is the failure this whole stage exists to
    make visible.
    """
    from robovast.common import index_db  # pylint: disable=import-outside-toplevel
    from robovast.common.errors import \
        IndexUnreachableError  # pylint: disable=import-outside-toplevel
    from robovast.results_processing import \
        campaign_ingest  # pylint: disable=import-outside-toplevel

    try:
        with index_db.connect() as conn:
            totals = campaign_ingest.ingest_campaign(conn, str(campaign_dir),
                                                     campaign_dir.name)
    except IndexUnreachableError as exc:
        return _stage(STAGE_DEGRADED,
                      f"the campaign is imported but not queryable: {exc}. It is loaded "
                      f"by re-importing once the index is reachable, or by "
                      f"'vast campaign postprocess {campaign_dir.name}'.")
    except Exception as exc:  # pylint: disable=broad-except
        logger.warning("index ingest failed for %s", campaign_dir.name, exc_info=True)
        return _stage(STAGE_DEGRADED,
                      f"the campaign is imported but its rows could not be loaded: {exc}")
    rows = sum(totals.values())
    if not rows:
        # Recorded, so "ingested and empty" stays distinguishable from "never ingested".
        return _stage(STAGE_ABSENT,
                      "no rows to load -- the archive carries no derived data. Run "
                      f"'vast campaign postprocess {campaign_dir.name}' to produce it.")
    return _stage(STAGE_OK,
                  f"{rows} row(s) across {len(totals)} table(s) loaded into the index")


def _check_analysis_db(campaign_dir: Path) -> dict:
    """Whether the campaign carries a per-campaign analysis database.

    Historical only: postprocessed rows now stream into the central index, so a modern
    campaign directory carries none and *absent* is the ordinary answer -- recoverable, as
    it always was, by running postprocessing, which is what loads the campaign into the
    index. Reported rather than dropped because an archive from before the index still has
    such a file, and saying so is how a reader knows where its metrics are.
    """
    candidates = sorted(campaign_dir.glob("**/*.duckdb")) + sorted(campaign_dir.glob("**/data.db"))
    if not candidates:
        return _stage(STAGE_ABSENT,
                      "no per-campaign analysis database. Expected -- postprocessed rows "
                      "live in the central index, and the 'index' stage above reports "
                      "whether this campaign's are loaded.")
    return _stage(STAGE_OK, f"{len(candidates)} legacy per-campaign analysis database(s) "
                            f"present; the queryable copy is the central index")
