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
"succeeded" is not one bit: a campaign archive carries a schema ladder of its own beyond the
``.vast``'s version (``campaign.db``'s ``user_version``), and each can independently be older,
newer, absent or corrupt.

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

import json
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
    stages["completeness"] = _check_completeness(campaign_dir)
    stages["campaign_store"] = _ingest_store(campaign_dir, rebuild=rebuild_store)
    stages["tables"] = _check_tables(campaign_dir)
    blocking = sorted(name for name, stage in stages.items()
                      if stage["verdict"] in BLOCKING_STAGES)
    return {"campaign_id": campaign_dir.name, "ok": not blocking,
            "blocking": blocking, "stages": stages}


def blocking_summary(report: dict) -> str:
    """Why *report* refuses the campaign, in the words each stage already wrote.

    The stage *names* are an index, not a diagnosis: ``config, layout`` is what every
    incomplete archive says, and it says the same whether the ``.vast`` is missing,
    unparseable, or from a robovast that does not exist yet. Each stage already
    composed the sentence that distinguishes them. ``import.log`` and ``import.json`` also
    carry it, but both live *inside* the campaign, which is published only once the import
    succeeds -- so the refusal itself must carry the sentence.
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


def _check_completeness(campaign_dir: Path) -> dict:
    """Was this campaign archived while it was still running?

    Degraded, never blocking: a snapshot is a real campaign with runs missing, and refusing
    it would throw away the only copy somebody may have of a run that has since been lost.
    What it must not do is arrive silently — every other stage would pass on it, because a
    snapshot differs from a finished campaign only in what is *absent*, and absence is what
    no check can see. The marker is the campaign saying so itself.
    """
    from robovast.execution.campaign_archive import \
        SNAPSHOT_MEMBER  # pylint: disable=import-outside-toplevel

    marker = campaign_dir / SNAPSHOT_MEMBER
    if not marker.is_file():
        return _stage(STAGE_OK, "the campaign was archived after it ended")
    try:
        facts = json.loads(marker.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        facts = {}
    tally = ""
    if facts.get("runs_total"):
        tally = f" ({facts.get('runs_completed', '?')}/{facts['runs_total']} runs had finished)"
    captured = facts.get("captured_at")
    return _stage(STAGE_DEGRADED,
                  f"archived while the campaign was still running"
                  f"{f' at {captured}' if captured else ''}{tally}. Runs that had not "
                  f"finished are absent and derived data was not computed, so this campaign "
                  f"is a snapshot: it lists and displays, and its totals are not the "
                  f"campaign's. Re-download it from the service that ran it once it ends.")


def _check_config(campaign_dir: Path) -> dict:
    """Can the frozen ``.vast`` be brought to the current version?

    :func:`~robovast.common.migrations.classify_config` says *what* the config is; this says
    what an import does about it, which is not what a retrigger does. Only ``newer`` is
    non-blocking here: a campaign from a robovast ahead of this one still lists and displays,
    and refusing it would discard a campaign somebody already has for a re-run they may never
    ask for. Every other refusal blocks, because nothing can read the configuration at all.
    """
    from robovast.common.migrations import (CONFIG_CURRENT, CONFIG_NEWER, CONFIG_TOO_OLD,
                                            CONFIG_UNMIGRATABLE, CONFIG_UNVERSIONED,
                                            CONFIG_UPGRADABLE, SUPPORTED_CONFIG_VERSION,
                                            classify_config, read_vast)
    from robovast.common.results_utils import campaign_vast_or_none

    # What to do about each refusal. The message says what is wrong with the file; these say
    # where the campaign can still be read from, which is the part an importer can act on.
    recovery = {
        CONFIG_UNVERSIONED: "repair _config/<name>.vast to state the version it was authored "
                            "against, or re-export the campaign from the service that ran it",
        CONFIG_TOO_OLD: "read it with the robovast_revision its _execution/ records name",
        CONFIG_UNMIGRATABLE: "vast campaign rerun <campaign> --to-workspace <name>, which "
                             "materialises it with every outstanding decision marked",
    }

    vast_path = campaign_vast_or_none(campaign_dir)
    if vast_path is None:
        return _stage(STAGE_FAILED, "no .vast under _config/")
    try:
        raw = read_vast(vast_path)
    except Exception as e:  # pylint: disable=broad-except
        return _stage(STAGE_FAILED, f"{vast_path.name} could not be parsed: {e}")

    found = classify_config(raw)
    if found.state == CONFIG_CURRENT:
        return _stage(STAGE_OK, f"config version {found.version}", version=found.version)
    if found.state == CONFIG_UPGRADABLE:
        return _stage(STAGE_MIGRATED,
                      f"config version {found.version} migrates to {SUPPORTED_CONFIG_VERSION} "
                      f"when read; the archived file is not modified",
                      version=found.version, steps=found.steps)
    if found.state == CONFIG_NEWER:
        return _stage(STAGE_NEWER,
                      f"{found.message} The campaign will display best-effort, but a re-run "
                      f"needs the newer robovast.", version=found.version)
    if found.state == CONFIG_UNVERSIONED:
        # No integer version to carry: what the file states instead is in the message, which
        # is the only place it can be reported without pretending it was a version.
        return _stage(STAGE_FAILED, found.message, recovery=recovery[found.state])
    return _stage(STAGE_FAILED, found.message, version=found.version,
                  recovery=recovery[found.state])


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


def _check_tables(campaign_dir: Path) -> dict:
    """What tables the campaign's records can give. A stage, reported like the others.

    Nothing is loaded: a table is built from the records the first time something names it,
    by the same decoder wherever the campaign is. What this can say at import is whether the
    records give any, which is the question a reader of an import report has -- a campaign
    that lists and opens but whose records give no table at all is the one worth flagging.
    """
    from robovast_decode.build import available_tables  # pylint: disable=import-outside-toplevel
    from robovast_decode.layout import decoder_config  # pylint: disable=import-outside-toplevel

    try:
        tables = available_tables(str(campaign_dir), decoder_config(str(campaign_dir)))
    except Exception as exc:  # pylint: disable=broad-except
        logger.warning("could not read %s's records", campaign_dir.name, exc_info=True)
        return _stage(STAGE_DEGRADED, f"the campaign's records could not be read: {exc}")
    if not tables:
        return _stage(STAGE_ABSENT, "the records give no tables: no recording and no data "
                                    "file in any run")
    return _stage(STAGE_OK, f"{len(tables)} table(s) can be built from the records; each is "
                            "built the first time something names it")
